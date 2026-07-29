"""External secret store for V4 agent self-update credentials (#99/#101).

Credential *values* are NEVER persisted on the ``Agent`` row, in any DB table,
or in the ``AuditLog`` (zero new tables; ``§0.6`` of the V4 plan). Only an
opaque ``secret_ref`` handle lives on the agent. This module is the "external
secret store" boundary: it maps a bearer credential to an ``agent_id`` and
supports rotation.

The default backend is an in-memory dict -- sufficient for a single-process
deployment and for the entire test suite. A production deployment can swap in a
file- or KMS-backed implementation behind the same ``AgentSecretStore``
interface without touching any caller.

Concurrency: ``issue`` / ``revoke`` / ``resolve`` are plain dict mutations. The
store is only ever read (``resolve``) on the authenticated request path and
written (``issue`` / ``revoke``) inside the same process that owns the DB
transaction, so no cross-process coordination is required for the V4 contract.
"""

from __future__ import annotations

import secrets


def _generate_credential() -> str:
    """Return a fresh, high-entropy bearer credential (never persisted raw)."""
    return "aios_ag_" + secrets.token_urlsafe(32)


class AgentSecretStore:
    """Maps agent bearer credentials to ``agent_id`` (and back) for rotation."""

    def __init__(self) -> None:
        self._by_token: dict[str, str] = {}
        self._by_agent: dict[str, str] = {}

    def issue(self, agent_id: str) -> str:
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
        if not token:
            return None
        return self._by_token.get(token)

    def revoke(self, agent_id: str) -> None:
        """Invalidate any credential currently bound to ``agent_id``."""
        old = self._by_agent.pop(agent_id, None)
        if old is not None:
            self._by_token.pop(old, None)

    def reset(self) -> None:
        """Clear all mappings (test isolation only -- never call in production)."""
        self._by_token.clear()
        self._by_agent.clear()


_STORE: AgentSecretStore | None = None


def get_secret_store() -> AgentSecretStore:
    """Return the process-wide secret store singleton."""
    global _STORE
    if _STORE is None:
        _STORE = AgentSecretStore()
    return _STORE


def reset_secret_store() -> None:
    """Reset the singleton (test isolation)."""
    global _STORE
    _STORE = AgentSecretStore()
