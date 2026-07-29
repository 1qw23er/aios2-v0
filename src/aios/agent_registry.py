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

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from aios.actor import ActorContext, _assert_owner_actor, resolve_owner_actor
from aios.audit import append_audit
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    AgentStatus,
    AgentTrustLevel,
    Capability,
    DelegationMode,
    new_id,
)
from aios.secrets_store import get_secret_store
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


def list_agents(
    session: Any,
    *,
    include_disabled: bool = True,
    capability: str | None = None,
) -> list[Agent]:
    """Return registry agents, newest first. Disabled agents are included by
    default (``include_disabled``).

    When ``capability`` is given, returns only enabled agents that declare that
    capability (joined through ``AgentCapability``). An unknown capability slug
    -> 422 (fail-closed, same source of truth as self-registration). With no
    ``capability`` the behaviour is unchanged from the legacy signature.
    """
    if capability is None:
        query = select(Agent)
        if not include_disabled:
            query = query.where(Agent.enabled.is_(True))
        return list(session.exec(query.order_by(Agent.id)))

    cap = session.exec(
        select(Capability).where(Capability.name == capability)
    ).first()
    if cap is None:
        raise ServiceError(422, f"unknown capability: {capability}")
    rows = session.exec(
        select(Agent)
        .join(
            AgentCapability,
            AgentCapability.agent_id == Agent.id,
        )
        .where(
            AgentCapability.capability_id == cap.id,
            AgentCapability.enabled.is_(True),
            Agent.enabled.is_(True),
        )
        .order_by(Agent.id)
    ).all()
    return list(rows)


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


# ---------------------------------------------------------------------------
# V4 self-registration primitives (#99/#101)
# ---------------------------------------------------------------------------


def _resolve_capabilities(session: Any, slugs: list[str] | None) -> list[str]:
    """Return the capability ids for the given slugs, fail-closed on unknown.

    The ``Capability`` catalog is the single source of truth for the capability
    vocabulary; a self-registered agent may only declare slugs that already
    exist. Unknown slugs -> 422 (we never invent capabilities here).
    """
    ids: list[str] = []
    for slug in slugs or []:
        cap = session.exec(select(Capability).where(Capability.name == slug)).first()
        if cap is None:
            raise ServiceError(422, f"unknown capability: {slug}")
        ids.append(cap.id)
    return ids


def _coherence_check(adapter_type: AdapterType, delegation_mode: DelegationMode | None) -> None:
    """Reject internally-inconsistent adapter / delegation_mode combinations."""
    is_external = adapter_type in (AdapterType.API, AdapterType.EXTERNAL)
    if is_external and delegation_mode is None:
        raise ServiceError(400, f"{adapter_type.value} 类型 agent 必须指定 delegation_mode")
    if not is_external and delegation_mode is not None:
        raise ServiceError(
            400,
            f"{adapter_type.value} 类型 agent 不应指定 delegation_mode（本地适配器）",
        )


def _reconcile_capabilities(session: Any, agent: Agent, capability_ids: list[str]) -> None:
    """Make the ``AgentCapability`` rows exactly match ``capability_ids``.

    This is the core GAP fix: writing only ``Agent.capabilities`` (JSON) leaves
    the agent invisible to ``route_task`` / knowledge projection, which read the
    ``AgentCapability`` relation rows. We delete stale rows and insert new ones
    so the relation and the JSON stay in sync.
    """
    existing_rows = list(
        session.exec(
            select(AgentCapability).where(AgentCapability.agent_id == agent.id)
        )
    )
    existing_ids = {row.capability_id for row in existing_rows}
    wanted = set(capability_ids)
    for row in existing_rows:
        if row.capability_id not in wanted:
            session.delete(row)
    for cap_id in wanted:
        if cap_id not in existing_ids:
            session.add(
                AgentCapability(
                    agent_id=agent.id,
                    capability_id=cap_id,
                    priority=50,
                    enabled=True,
                )
            )


def create_agent_via_bootstrap(
    session: Any,
    *,
    platform: str,
    external_ref: str,
    jti: str,
    name: str,
    role: str,
    adapter_type: str,
    delegation_mode: str | None = None,
    capabilities: list[str] | None = None,
    endpoint: str | None = None,
    callback_url: str | None = None,
    config_ref: str | None = None,
    limitations: list[str] | None = None,
    timeout_s: float = 300.0,
    max_retries: int = 3,
) -> tuple[Agent, str]:
    """Strict single-use CREATE of an agent identity via a scoped bootstrap token.

    This is the *bootstrap* path (``POST /agents/bootstrap``): it is NOT an
    upsert. The caller (the endpoint) has already verified the token's
    signature, expiry, and scope; here we enforce the remaining contract:

    - **Single-use**: a row with ``bootstrap_token_ref = jti`` already in the DB
      means the token was consumed -> 401 (no re-issue, no side effects).
    - **Tuple uniqueness**: the ``(platform, external_ref)`` partial unique index
      makes concurrent same-tuple claims collide -> 401 (zero side effects).
    - **Atomic claim**: the agent row and its ``bootstrap_token_ref`` are inserted
      in the same transaction; the credential (external secret store) is issued
      only *after* the row is committed, so a DB rollback never orphans a usable
      credential.

    Returns ``(agent, credential)`` where ``credential`` is the one-time bearer
    secret the agent uses for subsequent self-updates.
    """
    # Single-use guard (DB read, no external state table).
    consumed = session.exec(
        select(Agent).where(Agent.bootstrap_token_ref == jti)
    ).first()
    if consumed is not None:
        raise ServiceError(401, "bootstrap token already consumed")

    if not name or not name.strip():
        raise ServiceError(400, "agent 名称不能为空")
    if not role or not role.strip():
        raise ServiceError(400, "agent 角色不能为空")

    at = _parse_adapter_type(adapter_type)
    dm = _parse_delegation_mode(delegation_mode)
    _coherence_check(at, dm)
    capability_ids = _resolve_capabilities(session, capabilities)

    agent = Agent(
        name=name.strip(),
        role=role.strip(),
        adapter_type=at,
        delegation_mode=dm,
        capabilities=capabilities or [],
        endpoint=endpoint,
        secret_ref=None,  # filled with the opaque handle after commit
        config_ref=config_ref,
        callback_url=callback_url,
        trust_level=AgentTrustLevel.INTERNAL,
        timeout_s=float(timeout_s),
        max_retries=int(max_retries),
        limitations=limitations or [],
        enabled=True,
        status=AgentStatus.AVAILABLE,
        platform=platform,
        external_ref=external_ref,
        bootstrap_token_ref=jti,
    )
    session.add(agent)
    for cap_id in capability_ids:
        session.add(
            AgentCapability(
                agent_id=agent.id, capability_id=cap_id, priority=50, enabled=True
            )
        )
    # Flush so the partial unique index can arbitrate a concurrent collision.
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise ServiceError(401, "agent identity already registered (collision)") from None

    # The one-time credential is issued only after the row + partial unique index
    # are durable at the DB level (flush succeeded). If ANYTHING after issuance
    # fails (audit write or the final commit), the agent row and token claim roll
    # back, so we MUST revoke the already-issued plaintext credential to avoid an
    # orphaned, active bearer secret -- compensation for every post-issuance
    # failure, not just the commit.
    credential = get_secret_store().issue(agent.id)
    agent.secret_ref = f"secret://agent/{agent.id}"
    session.add(agent)
    try:
        append_audit(
            session,
            actor=f"bootstrap:{jti}",
            action="agent.self_registered",
            resource_type="agent",
            resource_id=agent.id,
            project_id=None,
            task_id=None,
            before={},
            after={
                "upserted": False,
                "platform": platform,
                "external_ref": external_ref,
                "capabilities": capabilities or [],
                "name": agent.name,
            },
            idempotency_key=f"audit:agent:self_register:{agent.id}:{new_id('k')}",
        )
        session.commit()
    except Exception:
        # Compensate: the agent row / audit may not have persisted, but the
        # plaintext credential already lives in the secret store. Revoke it so
        # no orphaned, active bearer secret survives a failed bootstrap.
        get_secret_store().revoke(agent.id)
        session.rollback()
        raise
    session.refresh(agent)
    return agent, credential


def upsert_agent(
    session: Any,
    *,
    actor: ActorContext,
    platform: str,
    external_ref: str,
    name: str,
    role: str,
    adapter_type: str,
    delegation_mode: str | None = None,
    capabilities: list[str] | None = None,
    endpoint: str | None = None,
    callback_url: str | None = None,
    config_ref: str | None = None,
    limitations: list[str] | None = None,
    timeout_s: float = 300.0,
    max_retries: int = 3,
) -> Agent:
    """Idempotent self-update of the agent's OWN identity (``PUT /agents/self``).

    The upsert target is locked to ``actor.agent_id`` -- the request body's
    ``(platform, external_ref)`` must match the agent's existing identity, so an
    agent can never impersonate another or forge owner. Concurrent updates to the
    same agent are serialized with a pessimistic write lock (``BEGIN IMMEDIATE``,
    mirroring ``attest_work_log``) so the read-modify-write of the scalar fields
    + the ``AgentCapability`` relation is atomic and last-writer-wins (never a
    silent dropped update, never a mixed aggregate).

    Returns the updated ``Agent``.
    """
    if actor.kind != "agent" or not actor.agent_id:
        raise ServiceError(403, "self-update requires an agent identity")
    agent_id = actor.agent_id

    if not platform or not external_ref:
        raise ServiceError(422, "platform and external_ref are required")
    if not name or not name.strip():
        raise ServiceError(400, "agent 名称不能为空")
    if not role or not role.strip():
        raise ServiceError(400, "agent 角色不能为空")

    # Pessimistic write lock: serialize concurrent same-agent updates.
    session.rollback()
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    agent = session.get(Agent, agent_id)
    if agent is None:
        raise ServiceError(404, f"agent 不存在: {agent_id}")
    # Scope lock: the caller may only update its own identity tuple.
    if agent.platform != platform or agent.external_ref != external_ref:
        raise ServiceError(
            422, "self-update scope mismatch: identity locked to this agent"
        )

    at = _parse_adapter_type(adapter_type)
    dm = _parse_delegation_mode(delegation_mode)
    _coherence_check(at, dm)
    capability_ids = _resolve_capabilities(session, capabilities)

    agent.name = name.strip()
    agent.role = role.strip()
    agent.adapter_type = at
    agent.delegation_mode = dm
    agent.capabilities = capabilities or []
    agent.endpoint = endpoint
    agent.callback_url = callback_url
    agent.config_ref = config_ref
    agent.limitations = limitations or []
    agent.timeout_s = float(timeout_s)
    agent.max_retries = int(max_retries)
    # Preserve the owner-controlled enable/disable state (Gate: the owner is the
    # sole authority over enable/disable). A self-update must never re-enable an
    # agent the owner disabled, nor flip the disabled flag. ``status`` is derived
    # from the preserved ``enabled`` flag so the row stays internally consistent.
    if not agent.enabled:
        agent.status = AgentStatus.UNAVAILABLE
    else:
        agent.status = AgentStatus.AVAILABLE
    session.add(agent)
    _reconcile_capabilities(session, agent, capability_ids)

    append_audit(
        session,
        actor=actor.derive_submitted_by(),
        action="agent.self_registered",
        resource_type="agent",
        resource_id=agent.id,
        project_id=None,
        task_id=None,
        before={},
        after={
            "upserted": True,
            "platform": platform,
            "external_ref": external_ref,
            "capabilities": capabilities or [],
            "name": agent.name,
        },
        idempotency_key=f"audit:agent:self_update:{agent.id}:{new_id('k')}",
    )
    session.commit()
    session.refresh(agent)
    return agent


def rotate_credential(
    session: Any,
    agent_id: str,
    *,
    actor: ActorContext | None = None,
) -> str:
    """Owner-only: issue a fresh self-update credential, invalidating the old.

    Used when an agent loses its credential. This does NOT go through bootstrap
    (a consumed bootstrap token stays 401 forever); instead the owner rotates the
    credential directly and hands the new one to the agent out of band.
    """
    if actor is None:
        actor = resolve_owner_actor()
    _assert_owner_actor(actor)
    agent = get_agent(session, agent_id)
    credential = get_secret_store().issue(agent.id)
    agent.secret_ref = f"secret://agent/{agent.id}"
    session.add(agent)
    try:
        append_audit(
            session,
            actor=actor.owner_id or "owner",
            action="agent.credential_rotated",
            resource_type="agent",
            resource_id=agent.id,
            project_id=None,
            task_id=None,
            before={},
            after={"agent_id": agent.id},
            idempotency_key=f"audit:agent:rotate:{agent.id}:{new_id('k')}",
        )
        session.commit()
    except Exception:
        # Compensate: a post-issuance failure (audit write OR commit) would
        # otherwise leave the new plaintext credential active while the rotation
        # never persisted. Revoke the already-issued plaintext credential and
        # roll back so no orphaned, active bearer secret survives.
        get_secret_store().revoke(agent.id)
        session.rollback()
        raise
    session.refresh(agent)
    return credential


def list_capabilities(session: Any) -> list[Capability]:
    """Return the full ``Capability`` catalog (discovery endpoint)."""
    return list(session.exec(select(Capability).order_by(Capability.name)))
