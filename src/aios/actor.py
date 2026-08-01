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
    # Optional project scope for agent actors. When an agent carries a scope,
    # it may only act on resources belonging to that project (fail-closed
    # otherwise). Owner/system actors leave this as None -- they are governed
    # by their own guards. This enables per-project isolation of compute
    # actions (contract B) without changing how the actor is resolved.
    project_id: str | None = None

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


def resolve_agent_actor(agent_id: str, project_id: str | None = None) -> ActorContext:
    """Trusted agent resolution (Agent Interop Gateway / delegation only).

    The gateway supplies a registry-validated ``agent_id``; the kind is fixed to
    ``"agent"`` so an external agent can never escalate to owner/system.

    An optional ``project_id`` scope may be attached. When present, the actor is
    only authorized to act on resources within that project (see
    ``CustomerService._assert_can_act``). When omitted (the default, and what the
    current Agent Interop Gateway supplies), the actor is fail-closed: compute
    actions reject it with 403 unless it is an owner.
    """
    return ActorContext(kind="agent", agent_id=agent_id, project_id=project_id)


def _assert_owner_actor(actor: ActorContext) -> None:
    """Owner-only service guard shared across owner-gated services.

    This mirrors ``knowledge_service._assert_knowledge_owner_actor`` so that all
    owner-only service functions enforce the same invariant: only a trusted
    ``ActorContext`` produced by ``authenticate_owner`` (kind=="owner" with a
    non-empty ``owner_id``) may proceed. A ``system`` or ``agent`` actor -- or an
    owner context missing its ``owner_id`` -- receives 403. Routes MUST NOT
    manufacture an owner ActorContext at the call site; the only trusted source
    is the ``authenticate_owner`` FastAPI dependency (see ``aios.api.security``).

    ``ServiceError`` is imported lazily to avoid a module-level import cycle
    (``services`` -> ... -> ``actor``).
    """
    from aios.services import ServiceError

    if actor.kind != "owner" or not actor.owner_id:
        raise ServiceError(403, "this action requires owner identity")
