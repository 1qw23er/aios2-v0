"""DB-backed agent registry management for the Agent Interoperability Gateway (#57, #61).

Makes the registry owner-operable (design review Q1: "owner can tune without code
change") by exposing register / list / get / enable-disable through a single service
that the REST API and the owner console both reuse.

Design constraints (Epic #57):
  * No vendor-specific logic lives here — only inside the adapters.
  * Secret *values* are NEVER accepted or stored. Only opaque ``secret_ref`` handles
    (e.g. ``secret://hermes-api-key``) referencing an external secret store.
  * Reuses the existing ``Agent`` model and the ``AdapterType`` / ``DelegationMode`` /
    ``AgentTrustLevel`` enums. No forked registry table.
  * Mutations are audited via ``append_audit`` so the registry is fully traceable.
"""

from __future__ import annotations

from typing import Any

from aios.actor import ActorContext, _assert_owner_actor, resolve_owner_actor
from aios.audit import append_audit
from aios.models import (
    AdapterType,
    Agent,
    AgentStatus,
    AgentTrustLevel,
    DelegationMode,
    new_id,
)
from aios.services import ServiceError


def _parse_adapter_type(value: str) -> AdapterType:
    try:
        return AdapterType(value)
    except ValueError as exc:
        raise ServiceError(400, f"未知 adapter_type: {value}") from exc


def _parse_delegation_mode(value: str | None) -> DelegationMode | None:
    if value is None or value == "":
        return None
    try:
        return DelegationMode(value)
    except ValueError as exc:
        raise ServiceError(400, f"未知 delegation_mode: {value}") from exc


def _parse_trust_level(value: str | None) -> AgentTrustLevel:
    try:
        return AgentTrustLevel(value or AgentTrustLevel.INTERNAL.value)
    except ValueError as exc:
        raise ServiceError(400, f"未知 trust_level: {value}") from exc


def list_agents(session: Any, *, include_disabled: bool = True) -> list[Agent]:
    """Return registry agents, newest first. Disabled agents are included by default."""
    from sqlmodel import select

    query = select(Agent)
    if not include_disabled:
        query = query.where(Agent.enabled.is_(True))
    agents = list(session.exec(query.order_by(Agent.id)))
    return agents


def get_agent(session: Any, agent_id: str) -> Agent:
    agent = session.get(Agent, agent_id)
    if agent is None:
        raise ServiceError(404, f"agent 不存在: {agent_id}")
    return agent


def register_agent(
    session: Any,
    *,
    name: str,
    role: str,
    adapter_type: str,
    delegation_mode: str | None = None,
    capabilities: list[str] | None = None,
    endpoint: str | None = None,
    secret_ref: str | None = None,
    callback_url: str | None = None,
    trust_level: str | None = None,
    timeout_s: float = 300.0,
    max_retries: int = 3,
    config_ref: str | None = None,
    limitations: list[str] | None = None,
    enabled: bool = True,
    actor: ActorContext | None = None,
) -> Agent:
    """Register a new agent in the DB-backed registry.

    Accepts only opaque ``secret_ref`` handles — never a raw secret value. The
    ``delegation_mode`` is required for externally-reachable agents (API / EXTERNAL
    adapter types) and must be omitted for in-process adapters (MODEL / CLI).

    Owner-only (#74): the ``actor`` must be a trusted owner ``ActorContext``
    produced by ``authenticate_owner``.
    """
    if actor is None:
        actor = resolve_owner_actor()
    _assert_owner_actor(actor)
    audit_actor = actor.owner_id or "owner"
    if not name or not name.strip():
        raise ServiceError(400, "agent 名称不能为空")
    if not role or not role.strip():
        raise ServiceError(400, "agent 角色不能为空")

    at = _parse_adapter_type(adapter_type)
    dm = _parse_delegation_mode(delegation_mode)
    tl = _parse_trust_level(trust_level)

    # Coherence: external agents must declare how they are reached; local adapters
    # (in-process LLM) must not. Keeps the registry internally consistent.
    is_external = at in (AdapterType.API, AdapterType.EXTERNAL)
    if is_external and dm is None:
        raise ServiceError(400, f"{at.value} 类型 agent 必须指定 delegation_mode")
    if not is_external and dm is not None:
        raise ServiceError(400, f"{at.value} 类型 agent 不应指定 delegation_mode（本地适配器）")

    agent = Agent(
        name=name.strip(),
        role=role.strip(),
        adapter_type=at,
        delegation_mode=dm,
        capabilities=capabilities or [],
        endpoint=endpoint,
        secret_ref=secret_ref,  # opaque handle ONLY
        config_ref=config_ref,
        callback_url=callback_url,
        trust_level=tl,
        timeout_s=float(timeout_s),
        max_retries=int(max_retries),
        limitations=limitations or [],
        enabled=enabled,
        status=AgentStatus.AVAILABLE if enabled else AgentStatus.UNAVAILABLE,
    )
    session.add(agent)
    session.commit()
    session.refresh(agent)

    append_audit(
        session,
        actor=audit_actor,
        action="agent.registered",
        resource_type="agent",
        resource_id=agent.id,
        project_id=None,
        task_id=None,
        before={},
        after={
            "name": agent.name,
            "adapter_type": agent.adapter_type.value,
            "delegation_mode": agent.delegation_mode.value if agent.delegation_mode else None,
            "trust_level": agent.trust_level.value,
            "enabled": agent.enabled,
        },
        idempotency_key=f"audit:agent:register:{agent.id}:{new_id('k')}",
    )
    session.commit()
    return agent


def set_agent_enabled(
    session: Any,
    agent_id: str,
    enabled: bool,
    *,
    actor: ActorContext | None = None,
) -> Agent:
    """Enable or disable an agent.

    Disabling sets ``status=UNAVAILABLE`` (a hard stop — the orchestrator will not
    route work to it); enabling restores ``status=AVAILABLE`` (unless it was in
    MAINTENANCE, which the owner must clear explicitly elsewhere).

    Owner-only (#74): the ``actor`` must be a trusted owner ``ActorContext``
    produced by ``authenticate_owner``.
    """
    if actor is None:
        actor = resolve_owner_actor()
    _assert_owner_actor(actor)
    audit_actor = actor.owner_id or "owner"
    agent = get_agent(session, agent_id)
    before = {"enabled": agent.enabled, "status": agent.status.value}
    agent.enabled = enabled
    agent.status = AgentStatus.AVAILABLE if enabled else AgentStatus.UNAVAILABLE
    session.add(agent)
    session.commit()
    session.refresh(agent)

    append_audit(
        session,
        actor=audit_actor,
        action="agent.enabled_changed",
        resource_type="agent",
        resource_id=agent.id,
        project_id=None,
        task_id=None,
        before=before,
        after={"enabled": agent.enabled, "status": agent.status.value},
        idempotency_key=f"audit:agent:enabled:{agent.id}:{new_id('k')}",
    )
    session.commit()
    return agent
