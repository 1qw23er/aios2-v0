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

import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from aios.actor import ActorContext

OWNER_ID_ENV = "AIOS_OWNER_ID"
OWNER_API_KEY_ENV = "AIOS_OWNER_API_KEY"

#: Minimum length of the owner API key. A short key is treated as a
#: misconfiguration (503), never as a client auth failure.
MIN_OWNER_API_KEY_LENGTH = 32

#: Realm advertised in the ``WWW-Authenticate`` challenge on 401 responses.
OWNER_REALM = "aios-owner"

_UNAUTH_HEADERS = {"WWW-Authenticate": f'Basic realm="{OWNER_REALM}"'}

# ``auto_error=False`` so a *missing* Authorization header does not immediately
# raise FastAPI's default 401 -- we must first decide whether the server is even
# configured (which yields 503, not 401).
_basic_scheme = HTTPBasic(auto_error=False, realm=OWNER_REALM)


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


def authenticate_owner(
    credentials: HTTPBasicCredentials | None = Depends(_basic_scheme),
) -> ActorContext:
    """FastAPI dependency: authenticate the single owner via HTTP Basic.

    Returns a trusted ``ActorContext(kind="owner", owner_id=AIOS_OWNER_ID)`` on
    success. See the module docstring for the full fail-closed contract.
    """
    owner_id, owner_api_key = _load_owner_config()

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="owner authentication required",
            headers=_UNAUTH_HEADERS,
        )

    # Evaluate BOTH comparisons unconditionally: a wrong id and a wrong key must
    # be indistinguishable to the caller (no short-circuit, no field leak).
    id_ok = secrets.compare_digest(credentials.username, owner_id)
    key_ok = secrets.compare_digest(credentials.password, owner_api_key)
    if not (id_ok and key_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid owner credentials",
            headers=_UNAUTH_HEADERS,
        )

    return ActorContext(kind="owner", owner_id=owner_id)
