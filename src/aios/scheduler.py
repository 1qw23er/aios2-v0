from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from aios.audit import AuditLog, append_audit
from aios.models import (
    Agent,
    AgentCapability,
    AgentStatus,
    ExecutionAssignment,
    RoutingMode,
    Task,
    TaskStatus,
    now_utc,
)
from aios.services import ServiceError, append_event


def _candidate(
    session: Session,
    agent: Agent,
    required_capabilities: list[str],
    *,
    check_capabilities: bool = True,
) -> dict[str, Any]:
    reasons: list[str] = []
    if not agent.enabled:
        reasons.append("disabled")
    if agent.status != AgentStatus.AVAILABLE:
        reasons.append(agent.status.value)
    profiles = list(
        session.exec(
            select(AgentCapability).where(
                AgentCapability.agent_id == agent.id,
                AgentCapability.enabled.is_(True),
            )
        )
    )
    priorities = {
        profile.capability_id: profile.priority
        for profile in profiles
        if profile.capability_id in required_capabilities
    }
    if check_capabilities:
        for capability_id in required_capabilities:
            if capability_id not in priorities:
                reasons.append(f"missing_capability:{capability_id}")
    scores = [
        priorities[capability_id]
        for capability_id in required_capabilities
        if capability_id in priorities
    ]
    return {
        "agent_id": agent.id,
        "eligible": not reasons,
        "priorities": priorities,
        "minimum_priority": min(scores) if scores else 0,
        "total_priority": sum(scores),
        "reasons": reasons,
    }


def _rank(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (candidate for candidate in candidates if candidate["eligible"]),
        key=lambda candidate: (
            -candidate["minimum_priority"],
            -candidate["total_priority"],
            candidate["agent_id"],
        ),
    )


def _record_blocked(
    session: Session,
    task: Task,
    idempotency_key: str,
    considered: list[dict[str, Any]],
    reason: str,
    commit: bool = True,
) -> None:
    try:
        append_audit(
            session,
            actor="scheduler",
            action="routing.blocked",
            resource_type="task",
            resource_id=task.id,
            project_id=task.project_id,
            task_id=task.id,
            before={"assigned_agent_id": task.assigned_agent_id},
            after={
                "routing_mode": task.routing_mode.value,
                "required_capabilities": task.required_capabilities,
                "considered_candidates": considered,
                "selected_agent_id": None,
                "routing_reason": reason,
                "fallback_used": False,
            },
            idempotency_key=f"audit:{idempotency_key}",
        )
        if commit:
            session.commit()
        else:
            session.flush()
    except Exception:
        if commit:
            session.rollback()
        raise


def route_task(
    session: Session,
    task_id: str,
    idempotency_key: str,
    commit: bool = True,
) -> ExecutionAssignment | None:
    existing = session.exec(
        select(ExecutionAssignment).where(ExecutionAssignment.idempotency_key == idempotency_key)
    ).first()
    if existing is not None:
        if existing.task_id != task_id:
            raise ServiceError(409, "Idempotency key conflicts with another task")
        return existing
    prior_audit = session.exec(
        select(AuditLog).where(AuditLog.idempotency_key == f"audit:{idempotency_key}")
    ).first()
    if prior_audit is not None:
        if prior_audit.resource_id != task_id:
            raise ServiceError(409, "Idempotency key conflicts with another task")
        return None

    task = session.get(Task, task_id)
    if task is None:
        raise ServiceError(404, "Task not found")
    if task.status != TaskStatus.READY:
        raise ServiceError(409, "Only ready tasks can be routed")

    considered: list[dict[str, Any]] = []
    selected: Agent | None = None
    reason: str
    fallback_used = False

    if task.routing_mode == RoutingMode.MANUAL:
        _record_blocked(
            session, task, idempotency_key, considered, "manual_assignment_required", commit=commit
        )
        return None

    if task.routing_mode == RoutingMode.FIXED:
        agent = session.get(Agent, task.assigned_agent_id) if task.assigned_agent_id else None
        if agent is None:
            reason = "fixed_agent_missing"
        else:
            candidate = _candidate(
                session,
                agent,
                task.required_capabilities,
                check_capabilities=False,
            )
            considered.append(candidate)
            if candidate["eligible"]:
                selected = agent
                reason = "fixed_agent"
            else:
                reason = "fixed_agent_unavailable"
        if selected is None:
            _record_blocked(session, task, idempotency_key, considered, reason)
            return None
    else:
        if task.routing_mode == RoutingMode.BEST_AVAILABLE and not task.required_capabilities:
            _record_blocked(
                session,
                task,
                idempotency_key,
                considered,
                "required_capabilities_missing",
            )
            return None
        agents = list(session.exec(select(Agent).order_by(Agent.id)))
        considered = [_candidate(session, agent, task.required_capabilities) for agent in agents]
        eligible = _rank(considered)
        if task.routing_mode == RoutingMode.PREFERRED_WITH_FALLBACK:
            preferred = next(
                (
                    candidate
                    for candidate in considered
                    if candidate["agent_id"] == task.preferred_agent_id
                ),
                None,
            )
            if preferred is not None and preferred["eligible"]:
                selected = session.get(Agent, preferred["agent_id"])
                reason = "preferred_agent"
            elif eligible:
                selected = session.get(Agent, eligible[0]["agent_id"])
                reason = "fallback_static_priority"
                fallback_used = task.preferred_agent_id is not None
            else:
                reason = "no_available_capable_agent"
        elif eligible:
            selected = session.get(Agent, eligible[0]["agent_id"])
            reason = "best_available_static_priority"
        else:
            reason = "no_available_capable_agent"
        if selected is None:
            _record_blocked(session, task, idempotency_key, considered, reason, commit=commit)
            return None

    assignment = ExecutionAssignment(
        task_id=task.id,
        selected_agent_id=selected.id,
        routing_reason=reason,
        fallback_used=fallback_used,
        idempotency_key=idempotency_key,
    )
    previous_agent_id = task.assigned_agent_id
    task.assigned_agent_id = selected.id
    task.adapter_type = selected.adapter_type
    task.updated_at = now_utc()
    try:
        session.add_all([assignment, task])
        append_event(
            session,
            project_id=task.project_id,
            task_id=task.id,
            event_type="task.assigned",
            idempotency_key=f"routing:{idempotency_key}:assigned",
            payload={"assignment_id": assignment.id, "selected_agent_id": selected.id},
        )
        append_audit(
            session,
            actor="scheduler",
            action="routing.selected",
            resource_type="execution_assignment",
            resource_id=assignment.id,
            project_id=task.project_id,
            task_id=task.id,
            before={"assigned_agent_id": previous_agent_id},
            after={
                "routing_mode": task.routing_mode.value,
                "required_capabilities": task.required_capabilities,
                "considered_candidates": considered,
                "selected_agent_id": selected.id,
                "routing_reason": reason,
                "fallback_used": fallback_used,
            },
            idempotency_key=f"audit:{idempotency_key}",
        )
        if commit:
            session.commit()
        else:
            session.flush()
    except Exception:
        if commit:
            session.rollback()
        raise
    session.refresh(assignment)
    return assignment


class DeterministicScheduler:
    def __init__(self, session: Session) -> None:
        self.session = session

    def route_task(self, task_id: str, idempotency_key: str) -> ExecutionAssignment | None:
        return route_task(self.session, task_id, idempotency_key)
