"""Single-owner inbound authentication for the AIOS control surface (#74).

The repository previously had NO inbound authentication: ``resolve_owner_actor``
manufactured an owner identity unconditionally, so every "owner-only" route and
the ``GET /audit`` 403 branch were dead code. ``AIOS_AGENT_API_KEY`` is an
*outbound* execution credential (used by ``execution.py`` to call delegated
agents) and is deliberately NOT reused here.

This module provides ``authenticate_owner``, a FastAPI dependency implementing a
single-owner HTTP Basic scheme with fail-closed semantics:

Configuration (read from the environment on every request):
  - ``AIOS_OWNER_ID``      -- the owner username. Non-empty, no ``:`` and no
                              control characters (``:`` is the Basic-auth field
                              separator; control chars are rejected defensively).
  - ``AIOS_OWNER_API_KEY`` -- the owner secret. At least 32 characters.

Behaviour (order matters -- see #74 locked decision 5):
  (a) Validate configuration first.
  (b) If unconfigured OR malformed (missing id, id contains ``:``/control chars,
      key shorter than 32 chars) -> ``503 owner_auth_not_configured``. This is a
      server misconfiguration, never a client error, so it must NOT be 401.
  (c) If configured but the request has no / wrong credentials -> ``401``.
  (d) Every ``401`` carries ``WWW-Authenticate: Basic realm="aios-owner"`` so a
      browser presents its native Basic-auth prompt.

Both the id and the key are compared with ``secrets.compare_digest`` and BOTH
comparisons are always evaluated (no short-circuit), so a wrong id and a wrong
key are indistinguishable to the caller and no timing/behaviour leak reveals
which field failed.

The secret is never logged, never echoed in a response body, and never written
to the AuditLog. On success the dependency returns a trusted
``ActorContext(kind="owner", owner_id=AIOS_OWNER_ID)`` -- the ONLY sanctioned
way for a route to obtain an owner actor. Routes MUST depend on this function
rather than calling ``resolve_owner_actor`` or building an ActorContext inline.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)

from aios.actor import ActorContext, resolve_agent_actor
from aios.secrets_store import SecretStoreUnavailable, get_secret_store

OWNER_ID_ENV = "AIOS_OWNER_ID"
OWNER_API_KEY_ENV = "AIOS_OWNER_API_KEY"

#: Minimum length of the owner API key. A short key is treated as a
#: misconfiguration (503), never as a client auth failure.
MIN_OWNER_API_KEY_LENGTH = 32

#: Realm advertised in the ``WWW-Authenticate`` challenge on 401 responses.
OWNER_REALM = "aios-owner"

#: Realm for agent / bootstrap bearer auth.
AGENT_REALM = "aios-agent"

_UNAUTH_HEADERS = {"WWW-Authenticate": f'Basic realm="{OWNER_REALM}"'}
_AGENT_UNAUTH_HEADERS = {"WWW-Authenticate": f'Bearer realm="{AGENT_REALM}"'}

# ``auto_error=False`` so a *missing* Authorization header does not immediately
# raise FastAPI's default 401 -- we must first decide whether the server is even
# configured (which yields 503, not 401).
_basic_scheme = HTTPBasic(auto_error=False, realm=OWNER_REALM)
# Bearer scheme for agent-self auth and bootstrap-token presentation.
_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="Bearer")


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _load_owner_config() -> tuple[str, str]:
    """Return the validated ``(owner_id, owner_api_key)`` or raise 503.

    A missing or malformed configuration is a *server* problem, so it maps to
    ``503 owner_auth_not_configured`` -- distinct from the ``401`` returned when
    the server is correctly configured but the client fails to authenticate.
    """
    owner_id = os.environ.get(OWNER_ID_ENV, "")
    owner_api_key = os.environ.get(OWNER_API_KEY_ENV, "")

    misconfigured = (
        not owner_id
        or ":" in owner_id
        or _has_control_chars(owner_id)
        or len(owner_api_key) < MIN_OWNER_API_KEY_LENGTH
    )
    if misconfigured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="owner_auth_not_configured",
        )
    return owner_id, owner_api_key


def verify_owner_credentials(username: str, password: str) -> ActorContext:
    """Verify presented owner credentials against the configured environment.

    Single source of the fail-closed comparison logic, shared by the HTTP
    boundary (``authenticate_owner``) and the CLI scripts (#88 §10) so no
    caller can re-implement a weaker check. Raises the same ``HTTPException``
    contract as the HTTP boundary (503 misconfigured / 401 invalid); CLI
    callers translate that into a non-zero exit.
    """
    owner_id, owner_api_key = _load_owner_config()

    # Evaluate BOTH comparisons unconditionally: a wrong id and a wrong key must
    # be indistinguishable to the caller (no short-circuit, no field leak).
    id_ok = secrets.compare_digest(username, owner_id)
    key_ok = secrets.compare_digest(password, owner_api_key)
    if not (id_ok and key_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid owner credentials",
            headers=_UNAUTH_HEADERS,
        )

    return ActorContext(kind="owner", owner_id=owner_id)


def authenticate_owner(
    credentials: HTTPBasicCredentials | None = Depends(_basic_scheme),
) -> ActorContext:
    """FastAPI dependency: authenticate the single owner via HTTP Basic.

    Returns a trusted ``ActorContext(kind="owner", owner_id=AIOS_OWNER_ID)`` on
    success. See the module docstring for the full fail-closed contract.
    """
    # Configuration is validated FIRST (fail-closed): unconfigured -> 503 even
    # when the request carries no credentials at all.
    _load_owner_config()

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="owner authentication required",
            headers=_UNAUTH_HEADERS,
        )

    return verify_owner_credentials(credentials.username, credentials.password)


# ---------------------------------------------------------------------------
# Agent self-authentication (V4, #99/#101)
# ---------------------------------------------------------------------------


def authenticate_agent(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> ActorContext:
    """FastAPI dependency: authenticate an agent via its bearer credential.

    The bearer credential is resolved through the external secret store
    (``aios.secrets_store``) to a trusted ``agent_id``; the resulting
    ``ActorContext(kind="agent", agent_id=...)`` is the ONLY sanctioned way for a
    route to obtain an agent actor. The agent can never escalate to owner/system
    and can only ever touch its own identity (the route layer enforces scope).

    A *missing* / *unknown* / *revoked* / *malformed* credential maps to
    ``401`` -- all indistinguishable (no existence leak, issue #103 §6). A
    *store-unavailable* condition (KEK missing, backend error, or row
    integrity failure) maps to ``503`` via ``SecretStoreUnavailable``; that
    readiness check precedes any token-format short-circuit so it fires for
    *every* input -- including a request that carries no bearer at all (G3).
    """
    # G3 ordering: probe store readiness BEFORE the missing-credential 401
    # branch. ``resolve(None)`` raises ``SecretStoreUnavailable`` (503) when the
    # store is down and returns ``None`` (a no-op readiness confirmation) when
    # ready, so a store outage yields a consistent 503 for every input rather
    # than a token-dependent 401. The readiness probe is format-independent.
    try:
        get_secret_store().resolve(None)
    except SecretStoreUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="secret_store_unavailable",
            headers=_AGENT_UNAUTH_HEADERS,
        ) from None

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="agent authentication required",
            headers=_AGENT_UNAUTH_HEADERS,
        )
    try:
        agent_id = get_secret_store().resolve(credentials.credentials)
    except SecretStoreUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="secret_store_unavailable",
            headers=_AGENT_UNAUTH_HEADERS,
        ) from None
    if agent_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid agent credential",
            headers=_AGENT_UNAUTH_HEADERS,
        )
    return resolve_agent_actor(agent_id)


# ---------------------------------------------------------------------------
# Scoped bootstrap token (V4, #99/#101)
# ---------------------------------------------------------------------------

#: Bootstrap token signing algorithm (HMAC-SHA256 over a compact JWS-like triple).
_BOOTSTRAP_ALG = "HS256"


class BootstrapClaims:
    """Verified payload of a scoped bootstrap token."""

    def __init__(
        self,
        *,
        platform: str,
        external_ref: str,
        jti: str,
        exp: int,
    ) -> None:
        self.platform = platform
        self.external_ref = external_ref
        self.jti = jti
        self.exp = exp


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _owner_signing_key() -> str:
    """The owner API key doubles as the HMAC secret for bootstrap tokens."""
    key = os.environ.get(OWNER_API_KEY_ENV, "")
    if len(key) < MIN_OWNER_API_KEY_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="owner_auth_not_configured",
        )
    return key


def mint_bootstrap_token(
    platform: str,
    external_ref: str,
    *,
    exp: int | None = None,
    jti: str | None = None,
    key: str | None = None,
) -> str:
    """Sign a scoped bootstrap token (owner action).

    The token is a compact, self-describing triple
    ``<header>.<payload>.<signature>`` where the payload is
    ``{"scope": [platform, external_ref], "exp": <unix>, "jti": <unique>}``.
    Verification uses HMAC-SHA256 with the owner API key as the secret.

    ``key`` is injectable for tests; in production it is read from the
    environment so minting and verification share the same secret.
    """
    if key is None:
        key = _owner_signing_key()
    if exp is None:
        exp = int(time.time()) + 3600
    if jti is None:
        jti = uuid.uuid4().hex
    header = {"alg": _BOOTSTRAP_ALG, "typ": "bts"}
    payload = {"scope": [platform, external_ref], "exp": int(exp), "jti": jti}
    header_b64 = _b64url_encode(
        json.dumps(header, separators=(",", ":")).encode("utf-8")
    )
    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{header_b64}.{payload_b64}".encode()
    signature = hmac.new(key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    sig_b64 = _b64url_encode(signature)
    return f"{header_b64}.{payload_b64}.{sig_b64}"


def _verify_bootstrap_token(token: str, key: str) -> BootstrapClaims:
    """Verify a bootstrap token's signature + expiry. Raises 401 on any failure."""
    parts = token.split(".")
    if len(parts) != 3:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bootstrap token",
            headers=_AGENT_UNAUTH_HEADERS,
        )
    header_b64, payload_b64, sig_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}".encode()
    expected = hmac.new(key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    try:
        provided = _b64url_decode(sig_b64)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bootstrap token signature",
            headers=_AGENT_UNAUTH_HEADERS,
        ) from None
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bootstrap token signature",
            headers=_AGENT_UNAUTH_HEADERS,
        )
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bootstrap token payload",
            headers=_AGENT_UNAUTH_HEADERS,
        ) from None
    exp = payload.get("exp")
    if exp is None or int(time.time()) > int(exp):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bootstrap token expired",
            headers=_AGENT_UNAUTH_HEADERS,
        )
    scope = payload.get("scope") or []
    if not isinstance(scope, list) or len(scope) != 2:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bootstrap token scope",
            headers=_AGENT_UNAUTH_HEADERS,
        )
    return BootstrapClaims(
        platform=str(scope[0]),
        external_ref=str(scope[1]),
        jti=payload.get("jti") or uuid.uuid4().hex,
        exp=int(exp),
    )


def authenticate_bootstrap_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> BootstrapClaims:
    """FastAPI dependency: verify a scoped bootstrap token's signature + expiry.

    Scope match against the request body and the single-use (DB) check are
    performed by the registry / endpoint, NOT here -- this dependency only
    establishes that the token is cryptographically valid and unexpired and
    returns its claims. Configuration is fail-closed: an unconfigured owner key
    yields ``503`` (matching ``authenticate_owner``).
    """
    _load_owner_config()
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bootstrap token required",
            headers=_AGENT_UNAUTH_HEADERS,
        )
    key = os.environ.get(OWNER_API_KEY_ENV, "")
    return _verify_bootstrap_token(credentials.credentials, key)
