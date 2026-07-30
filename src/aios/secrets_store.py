"""External secret store for V4 agent self-update credentials (#99/#101/#103).

Credential *values* are NEVER persisted on the ``Agent`` row, in any DB table,
or in the ``AuditLog`` (zero new tables for the default memory backend; #103
adds a minimal ``agent_secret`` table ONLY for the opt-in ``encrypted_db``
backend). Only an opaque ``secret_ref`` handle lives on the agent. This module
is the "external secret store" boundary: it maps a bearer credential to an
``agent_id`` and supports rotation.

Two backends implement the same dual-mode transaction contract:

* ``AgentSecretStore`` (default, in-memory) -- a single-process dict. No KEK,
  no transactions; safe for tests and single-process deployments.
* ``EncryptedDbAgentSecretStore`` (opt-in, ``encrypted_db``) -- persists ONLY
  KEK-derived HMAC tags (``token_tag``, ``row_mac``) to the ``agent_secret``
  table. Never the plaintext. fail-closed: if the KEK is missing or the backend
  is otherwise unavailable, ``issue`` / ``resolve`` raise ``SecretStoreUnavailable``
  (mapped to HTTP 503) -- there is no in-memory fallback and no silent ``None``.

Dual-mode transaction ownership (issue #103 §4.5): both ``issue`` and ``revoke``
accept ``session=None``. ``None`` means "open a store-owned session and commit
independently" (used by ``rotate_credential`` compensation); a concrete
``session`` is the caller's transaction, which the store writes into but does
NOT commit (used by bootstrap so the credential is atomic with the agent row).

401/503 split (issue #103 §6): ``resolve`` performs the KEK/backend readiness
check BEFORE any token-format short-circuit. If the store is unavailable it
raises ``SecretStoreUnavailable`` (503) for EVERY input -- including malformed
tokens -- so no token-format information leaks. Only once the store is ready
does a malformed / unknown / revoked token return ``None`` (401).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from aios.db import make_session
from aios.models import AgentSecret, now_utc

_CREDENTIAL_PREFIX = "aios_ag_"


class SecretStoreUnavailable(Exception):
    """Raised when the secret store cannot issue or resolve credentials.

    Mapped to HTTP 503 at the API boundary. Distinct from a normal
    authentication failure (unknown / revoked / malformed token -> ``None`` ->
    401). Never carries the token or KEK in its message.
    """


class SecretStoreMisconfigured(SecretStoreUnavailable):
    """Raised at startup when the secret-store backend config is invalid.

    A subclass of :class:`SecretStoreUnavailable` so the message family stays
    coherent, but it is thrown during app startup (``lifespan``) rather than at
    request time. It surfaces operator errors -- e.g. selecting ``encrypted_db``
    without a valid KEK, or an unknown backend -- as an explicit boot failure
    instead of silent 503s on every credential check.
    """


# Backend selection + key material (issue #103 §4.1 / §4.3).
SECRET_STORE_BACKEND_ENV = "AIOS_SECRET_STORE_BACKEND"
SECRET_MASTER_KEY_ENV = "AIOS_SECRET_MASTER_KEY"
_DEFAULT_BACKEND = "memory"


def _generate_credential() -> str:
    """Return a fresh, high-entropy bearer credential (never persisted raw)."""
    return _CREDENTIAL_PREFIX + secrets.token_urlsafe(32)


def _load_kek() -> bytes | None:
    """Load the secret master key (KEK) from the environment, or ``None``.

    The frozen plan (issue #103 §4.1) requires the KEK to be exactly 32 bytes,
    supplied as hex or base64 (urlsafe or standard). Any malformed value or a
    key of the wrong length is treated as absent so the caller can fail-closed
    rather than guess (G6). ``urlsafe_b64decode`` has no ``validate`` keyword,
    so we decode then enforce the strict 32-byte length.
    """
    raw = os.environ.get(SECRET_MASTER_KEY_ENV)
    if not raw:
        return None
    raw = raw.strip()
    candidate: bytes | None = None
    # hex path
    try:
        candidate = bytes.fromhex(raw)
    except ValueError:
        candidate = None
    # base64 path (urlsafe first, then standard)
    if candidate is None:
        try:
            try:
                candidate = base64.urlsafe_b64decode(raw)
            except Exception:
                candidate = None
            if candidate is None:
                padding = "=" * (-len(raw) % 4)
                candidate = base64.b64decode(raw + padding)
        except Exception:
            return None
    # Frozen plan: KEK MUST be exactly 32 bytes. A wrong-size key is rejected
    # (fail-closed), never silently coerced or truncated.
    if len(candidate) != 32:
        return None
    return candidate


def _hmac(kek: bytes, message: bytes) -> bytes:
    return hmac.new(kek, message, hashlib.sha256).digest()


def _valid_token_format(token: str | None) -> bool:
    """Reject obviously malformed tokens without touching the store backend."""
    if not isinstance(token, str):
        return False
    if not token.startswith(_CREDENTIAL_PREFIX):
        return False
    payload = token[len(_CREDENTIAL_PREFIX):]
    if not payload:
        return False
    return all(c.isalnum() or c in "-_" for c in payload)


class AgentSecretStore:
    """In-memory default backend. See module docstring for the contract.

    The in-memory backend has no KEK and no transactions, so it is always
    "ready" and never raises ``SecretStoreUnavailable``. The ``session``
    argument is accepted (for interface compatibility) but ignored.
    """

    def __init__(self) -> None:
        self._by_token: dict[str, str] = {}
        self._by_agent: dict[str, str] = {}

    def issue(self, agent_id: str, session: object | None = None) -> str:
        """Revoke any existing credential for ``agent_id`` and mint a new one.

        Returns the plaintext credential that the owner/agent must transmit. The
        previous credential (if any) is immediately invalidated.
        """
        self.revoke(agent_id)
        token = _generate_credential()
        self._by_token[token] = agent_id
        self._by_agent[agent_id] = token
        return token

    def resolve(self, token: str | None) -> str | None:
        """Return the ``agent_id`` bound to ``token``, or ``None`` if unknown."""
        if not _valid_token_format(token):
            return None
        return self._by_token.get(token)

    def revoke(self, agent_id: str, session: object | None = None) -> None:
        """Invalidate any credential currently bound to ``agent_id``."""
        old = self._by_agent.pop(agent_id, None)
        if old is not None:
            self._by_token.pop(old, None)

    def reset(self) -> None:
        """Clear all mappings (test isolation only -- never call in production)."""
        self._by_token.clear()
        self._by_agent.clear()


class EncryptedDbAgentSecretStore:
    """Opt-in persistent backend (#103). Stores only HMAC tags, never plaintext.

    fail-closed: if the KEK is missing the store is unusable and ``issue`` /
    ``resolve`` raise ``SecretStoreUnavailable`` (HTTP 503). There is no
    in-memory fallback and no silent ``None`` on operational failure.
    """

    def __init__(self, kek: bytes, session_factory=make_session) -> None:
        self._kek = kek
        self._session_factory = session_factory

    def _ready(self) -> bool:
        return bool(self._kek)

    def issue(self, agent_id: str, session: object | None = None) -> str:
        if not self._ready():
            raise SecretStoreUnavailable(
                "secret store unavailable: master key not configured"
            )
        own = session is None
        if own:
            # Opening the store session (engine/connection init) is itself a
            # backend operation: a failure here must map to 503 (G3), not escape
            # as an unclassified 500. When a caller session is supplied we don't
            # own session construction.
            try:
                sess = self._session_factory()
            except Exception as exc:
                raise SecretStoreUnavailable(
                    "secret store unavailable: backend error"
                ) from exc
        else:
            sess = session
        try:
            token = _generate_credential()
            token_tag = _hmac(self._kek, token.encode("utf-8"))
            row_mac = _hmac(self._kek, agent_id.encode("utf-8") + token_tag)
            now = now_utc()
            # Concurrency-safe upsert (issue #103 G2): a single atomic statement
            # replaces any existing row for the agent with a fresh active
            # credential. This covers BOTH first-issue (INSERT) and re-issue
            # (UPDATE) without a read-modify-write race, so two concurrent
            # issuances for the same agent can never collide on the agent PK.
            # The previous token's tag is overwritten, so it can no longer
            # resolve (effective revocation; G5 durable).
            sess.execute(
                text(
                    "INSERT INTO agent_secret "
                    "(agent_id, token_tag, row_mac, created_at, revoked_at) "
                    "VALUES (:agent_id, :token_tag, :row_mac, :created_at, NULL) "
                    "ON CONFLICT(agent_id) DO UPDATE SET "
                    "token_tag = :token_tag, row_mac = :row_mac, "
                    "created_at = :created_at, revoked_at = NULL"
                ),
                {
                    "agent_id": agent_id,
                    "token_tag": token_tag,
                    "row_mac": row_mac,
                    "created_at": now,
                },
            )
            if own:
                sess.commit()
            return token
        except SQLAlchemyError as exc:
            # fail-closed (G3): any DB backend failure during issuance must
            # surface as 503, never an unclassified 500.
            if own:
                sess.rollback()
            raise SecretStoreUnavailable(
                "secret store unavailable: backend error"
            ) from exc
        except Exception:
            if own:
                sess.rollback()
            raise
        finally:
            if own:
                sess.close()

    def resolve(self, token: str | None) -> str | None:
        # Readiness check BEFORE any token-format short-circuit (issue #103 §6):
        # if the store (KEK OR backend) is unavailable we must surface 503 for
        # EVERY input -- including malformed tokens -- so no token-format
        # information leaks. The KEK check is first; then a backend probe opens
        # a session and confirms the DB is reachable before any format branch.
        if not self._ready():
            raise SecretStoreUnavailable(
                "secret store unavailable: master key not configured"
            )
        # Opening the store session (engine/connection init) is itself a backend
        # operation; a failure here must map to 503 (G3), not escape as 500.
        try:
            sess = self._session_factory()
        except Exception as exc:
            raise SecretStoreUnavailable(
                "secret store unavailable: backend error"
            ) from exc
        try:
            # Backend readiness probe: confirm the secret table itself is
            # reachable BEFORE any token-format branch. A bare 'SELECT 1' is not
            # enough -- the DB may be up while agent_secret is missing/inaccessible
            # (e.g. migration not applied), which would otherwise let malformed
            # tokens return 401 while valid ones 503, re-introducing the
            # prohibited format-dependent split (G3). By querying the table we
            # guarantee 503 for EVERY input when the store is not fully ready.
            try:
                sess.execute(select(AgentSecret).limit(0))
            except SQLAlchemyError as exc:
                raise SecretStoreUnavailable(
                    "secret store unavailable: backend error"
                ) from exc
            if not _valid_token_format(token):
                return None
            token_tag = _hmac(self._kek, token.encode("utf-8"))
            try:
                row = sess.execute(
                    select(AgentSecret).where(
                        AgentSecret.token_tag == token_tag,
                        AgentSecret.revoked_at.is_(None),
                    )
                ).scalar_one_or_none()
            except SQLAlchemyError as exc:
                raise SecretStoreUnavailable(
                    "secret store unavailable: backend error"
                ) from exc
            if row is None:
                return None
            expected = _hmac(
                self._kek, row.agent_id.encode("utf-8") + token_tag
            )
            if not hmac.compare_digest(expected, row.row_mac):
                # Tampered / transplanted row: the binding check failed. Surface
                # as an operational failure (503), NOT a silent 401.
                raise SecretStoreUnavailable(
                    "secret store unavailable: integrity check failed"
                )
            return row.agent_id
        finally:
            sess.close()

    def revoke(self, agent_id: str, session: object | None = None) -> None:
        from sqlalchemy import update

        own = session is None
        if own:
            # Opening the store session (engine/connection init) is itself a
            # backend operation: a failure here must map to 503 (G3), not escape
            # as an unclassified 500. When a caller session is supplied we don't
            # own session construction.
            try:
                sess = self._session_factory()
            except Exception as exc:
                raise SecretStoreUnavailable(
                    "secret store unavailable: backend error"
                ) from exc
        else:
            sess = session
        try:
            sess.execute(
                update(AgentSecret)
                .where(AgentSecret.agent_id == agent_id)
                .values(revoked_at=now_utc())
            )
            if own:
                sess.commit()
        except Exception:
            if own:
                sess.rollback()
            raise
        finally:
            if own:
                sess.close()


_STORE: AgentSecretStore | EncryptedDbAgentSecretStore | None = None


def get_secret_store() -> AgentSecretStore | EncryptedDbAgentSecretStore:
    """Return the process-wide secret store singleton.

    Backend selection (issue #103 §4.5):
      * ``AIOS_SECRET_STORE_BACKEND`` unset / ``memory`` -> in-memory default.
      * ``encrypted_db`` -> persistent HMAC-tag backend; requires the KEK
        (``AIOS_SECRET_MASTER_KEY``) to be present and valid, otherwise the
        factory FAILS CLOSED and raises ``SecretStoreUnavailable`` (no silent
        memory fallback).
    """
    global _STORE
    if _STORE is None:
        backend = os.environ.get(SECRET_STORE_BACKEND_ENV, _DEFAULT_BACKEND)
        if backend == "encrypted_db":
            kek = _load_kek()
            if not kek:
                raise SecretStoreUnavailable(
                    f"secret store backend 'encrypted_db' requires "
                    f"{SECRET_MASTER_KEY_ENV} to be set"
                )
            _STORE = EncryptedDbAgentSecretStore(kek)
        elif backend == "memory":
            # The only permitted default. Any other explicit value is an
            # unsupported configuration and MUST fail closed (G6) -- never a
            # silent downgrade to memory that would lose credential persistence.
            _STORE = AgentSecretStore()
        else:
            raise SecretStoreUnavailable(
                f"secret store unavailable: unsupported backend {backend!r} "
                f"(permitted: 'memory', 'encrypted_db')"
            )
    return _STORE


def reset_secret_store() -> None:
    """Reset the singleton (test isolation)."""
    global _STORE
    _STORE = AgentSecretStore()


def validate_secret_store_config() -> None:
    """Fail-closed startup check for the secret-store backend configuration.

    Call this once during application startup (``lifespan``). It validates the
    operator's backend choice and key material *before* the app accepts traffic:

      * ``memory`` (default, or unset) -> always valid, no KEK required.
      * ``encrypted_db`` -> requires a valid :data:`SECRET_MASTER_KEY_ENV`
        (exactly 32 bytes, hex or base64); otherwise raises
        :class:`SecretStoreMisconfigured`.
      * any other explicit value -> raises :class:`SecretStoreMisconfigured`.

    This turns a permanent operator mistake into an explicit boot failure with a
    readable message, instead of silent HTTP 503 on every agent authentication.

    Transient backend unavailability (e.g. the DB is momentarily down) is
    intentionally NOT checked here: that remains a request-time 503 (issue #103
    G3) so a brief outage never prevents the process from starting.
    """
    backend = os.environ.get(SECRET_STORE_BACKEND_ENV, _DEFAULT_BACKEND)
    if backend == _DEFAULT_BACKEND:
        return
    if backend == "encrypted_db":
        if not _load_kek():
            raise SecretStoreMisconfigured(
                f"secret store misconfigured: backend 'encrypted_db' requires a "
                f"valid {SECRET_MASTER_KEY_ENV} (exactly 32 bytes, hex or base64)"
            )
        return
    raise SecretStoreMisconfigured(
        "secret store misconfigured: unsupported AIOS_SECRET_STORE_BACKEND "
        "value (permitted: 'memory', 'encrypted_db')"
    )
