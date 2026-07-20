"""Trusted actor identity for knowledge formal actions (Phase A, #67).

An ``ActorContext`` is NEVER built from a request body. It is constructed only
by the trusted resolvers below, each bound to a specific call site:

- ``resolve_owner_actor`` -- owner console / authenticated owner session only.
- ``resolve_agent_actor`` -- the Agent Interop Gateway / delegation adapter only,
  with a registry-validated ``agent_id``.
- ``ActorContext.system`` -- internal service code only.

Because the ``kind`` and identity ids are derived server-side, a caller can
never spoof ``kind="owner"`` or choose an arbitrary ``agent_id``. The display
strings (``submitted_by`` / ``reviewer``) are also server-derived and immutable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Canonical owner identity used when no richer auth context is available.
# The owner console / automated owner flows resolve to this trusted identity.
OWNER_IDENTITY = "owner"


@dataclass(frozen=True)
class ActorContext:
    kind: Literal["owner", "agent", "system"]
    owner_id: str | None = None
    agent_id: str | None = None

    def derive_submitted_by(self) -> str:
        if self.kind == "owner":
            return f"owner:{self.owner_id}" if self.owner_id else "owner"
        if self.kind == "agent":
            return f"agent:{self.agent_id}" if self.agent_id else "agent"
        return "system"

    def derive_reviewer(self) -> str:
        return self.derive_submitted_by()

    @classmethod
    def system(cls) -> ActorContext:
        return cls(kind="system")


def resolve_owner_actor(owner_id: str = OWNER_IDENTITY) -> ActorContext:
    """Trusted owner resolution.

    Only the owner console / authenticated owner session may call this. The
    caller MUST NOT pass ``kind`` or ``owner_id`` from a request body -- doing
    so would mean constructing the ActorContext at the call site, which is
    exactly what we forbid.
    """
    return ActorContext(kind="owner", owner_id=owner_id)


def resolve_agent_actor(agent_id: str) -> ActorContext:
    """Trusted agent resolution (Agent Interop Gateway / delegation only).

    The gateway supplies a registry-validated ``agent_id``; the kind is fixed to
    ``"agent"`` so an external agent can never escalate to owner/system.
    """
    return ActorContext(kind="agent", agent_id=agent_id)
