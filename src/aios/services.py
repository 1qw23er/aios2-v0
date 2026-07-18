from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlmodel import Session, SQLModel, select

from aios.audit import append_audit
from aios.models import (
    Agent,
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactReviewStatus,
    Capability,
    Event,
    Project,
    RiskLevel,
    RoutingMode,
    Task,
    TaskStatus,
    new_id,
    now_utc,
)
from aios.schemas import (
    ApprovalCreate,
    ProjectCreate,
    TaskCreate,
)


@dataclass
class ServiceError(Exception):
    status_code: int
    detail: str


def request_fingerprint(request: BaseModel) -> str:
    encoded = json.dumps(
        request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def append_event(
    session: Session,
    *,
    project_id: str,
    task_id: str | None,
    event_type: str,
    idempotency_key: str,
    payload: dict[str, Any],
) -> Event:
    session.flush()
    event = Event(
        project_id=project_id,
        task_id=task_id,
        type=event_type,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    session.add(event)
    return event


def _replay[Resource: SQLModel](
    session: Session,
    *,
    idempotency_key: str,
    operation: str,
    fingerprint: str,
    resource_type: type[Resource],
) -> Resource | None:
    event = session.exec(select(Event).where(Event.idempotency_key == idempotency_key)).first()
    if event is None:
        return None
    if (
        event.payload.get("operation") != operation
        or event.payload.get("fingerprint") != fingerprint
    ):
        raise ServiceError(409, "Idempotency key conflicts with another request")
    resource = session.get(resource_type, event.payload.get("resource_id"))
    if resource is None:
        raise ServiceError(409, "Idempotency record references a missing resource")
    return resource


def create_project(
    session: Session, request: ProjectCreate, idempotency_key: str, commit: bool = True
) -> Project:
    fingerprint = request_fingerprint(request)
    replay = _replay(
        session,
        idempotency_key=idempotency_key,
        operation="project.create",
        fingerprint=fingerprint,
        resource_type=Project,
    )
    if replay is not None:
        return replay
    project = Project(**request.model_dump())
    try:
        session.add(project)
        append_event(
            session,
            project_id=project.id,
            task_id=None,
            event_type="project.created",
            idempotency_key=idempotency_key,
            payload={
                "operation": "project.create",
                "fingerprint": fingerprint,
                "resource_id": project.id,
            },
        )
        append_audit(
            session,
            actor="system",
            action="project.created",
            resource_type="project",
            resource_id=project.id,
            project_id=project.id,
            task_id=None,
            before={},
            after={"status": project.status.value},
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
    session.refresh(project)
    return project


def create_task(
    session: Session, request: TaskCreate, idempotency_key: str, commit: bool = True
) -> Task:
    fingerprint = request_fingerprint(request)
    replay = _replay(
        session,
        idempotency_key=idempotency_key,
        operation="task.create",
        fingerprint=fingerprint,
        resource_type=Task,
    )
    if replay is not None:
        return replay
    if session.get(Project, request.project_id) is None:
        raise ServiceError(404, "Project not found")
    if request.assigned_agent_id and session.get(Agent, request.assigned_agent_id) is None:
        raise ServiceError(404, "Agent not found")
    if request.preferred_agent_id and session.get(Agent, request.preferred_agent_id) is None:
        raise ServiceError(404, "Preferred agent not found")
    for capability_id in request.required_capabilities:
        if session.get(Capability, capability_id) is None:
            raise ServiceError(404, "Capability not found")
    for dependency_id in request.depends_on:
        dependency = session.get(Task, dependency_id)
        if dependency is None:
            raise ServiceError(404, "Dependency not found")
        if dependency.project_id != request.project_id:
            raise ServiceError(422, "Dependency must belong to the same project")
    task = Task(**request.model_dump())
    try:
        session.add(task)
        append_event(
            session,
            project_id=task.project_id,
            task_id=task.id,
            event_type="task.created",
            idempotency_key=idempotency_key,
            payload={
                "operation": "task.create",
                "fingerprint": fingerprint,
                "resource_id": task.id,
            },
        )
        append_audit(
            session,
            actor="system",
            action="task.created",
            resource_type="task",
            resource_id=task.id,
            project_id=task.project_id,
            task_id=task.id,
            before={},
            after={"status": task.status.value},
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
    session.refresh(task)
    return task


def create_approval(
    session: Session, request: ApprovalCreate, idempotency_key: str, commit: bool = True
) -> Approval:
    fingerprint = request_fingerprint(request)
    replay = _replay(
        session,
        idempotency_key=idempotency_key,
        operation="approval.create",
        fingerprint=fingerprint,
        resource_type=Approval,
    )
    if replay is not None:
        return replay
    if session.get(Project, request.project_id) is None:
        raise ServiceError(404, "Project not found")
    if request.task_id:
        task = session.get(Task, request.task_id)
        if task is None:
            raise ServiceError(404, "Task not found")
        if task.project_id != request.project_id:
            raise ServiceError(422, "Task must belong to the same project")
    approval = Approval(**request.model_dump(), status=ApprovalStatus.PENDING)
    try:
        session.add(approval)
        append_event(
            session,
            project_id=approval.project_id,
            task_id=approval.task_id,
            event_type="approval.requested",
            idempotency_key=idempotency_key,
            payload={
                "operation": "approval.create",
                "fingerprint": fingerprint,
                "resource_id": approval.id,
            },
        )
        append_audit(
            session,
            actor="system",
            action="approval.requested",
            resource_type="approval",
            resource_id=approval.id,
            project_id=approval.project_id,
            task_id=approval.task_id,
            before={},
            after={"status": approval.status.value},
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
    session.refresh(approval)
    return approval


def ensure_pending_approval(
    session: Session,
    *,
    project_id: str,
    task_id: str | None,
    action_type: str,
    risk_level: RiskLevel = RiskLevel.L2,
    commit: bool = True,
) -> Approval:
    """Return the task's existing PENDING approval, or create one (L2 gate by default).

    Used by the owner console so a gate task (e.g. T6 / T8) can be decided in one
    click: the approval is created lazily on first decision instead of at launch.
    """
    if task_id is not None:
        existing = session.exec(
            select(Approval).where(
                Approval.project_id == project_id,
                Approval.task_id == task_id,
                Approval.status == ApprovalStatus.PENDING,
            )
        ).first()
        if existing is not None:
            return existing
    request = ApprovalCreate(
        project_id=project_id,
        task_id=task_id,
        action_type=action_type,
        risk_level=risk_level,
        rationale=None,
    )
    return create_approval(session, request, idempotency_key=new_id("idem"), commit=commit)


def decide_approval(
    session: Session,
    approval_id: str,
    decision: ApprovalStatus,
    rationale: str | None = None,
) -> Approval:
    """Owner decision on a pending approval.

    On APPROVED for a gated task, marks the task DONE and unlocks downstream tasks
    (e.g. T6 done -> T7 / T9 READY) via the orchestrator. Every decision is recorded
    in the AuditLog (bypassing it is an invalid state per the Issue stop condition).
    """
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise ServiceError(404, "Approval not found")
    if approval.status != ApprovalStatus.PENDING:
        raise ServiceError(409, "该审批已被处理，不能重复决策")
    # #47: owner decisions are confined to MANUAL gated tasks only.
    if approval.task_id is not None:
        gated_task = session.get(Task, approval.task_id)
        if gated_task is not None and gated_task.routing_mode != RoutingMode.MANUAL:
            raise ServiceError(400, "仅 owner-gate（MANUAL）任务可在此审批/修订")
    approval.status = decision
    approval.decided_at = now_utc()
    approval.rationale = rationale
    try:
        session.add(approval)
        append_audit(
            session,
            actor="owner",
            action="approval.decided",
            resource_type="approval",
            resource_id=approval.id,
            project_id=approval.project_id,
            task_id=approval.task_id,
            before={"status": ApprovalStatus.PENDING.value},
            after={"status": decision.value, "rationale": rationale},
            idempotency_key=f"audit:approval:{approval.id}:{decision.value}",
        )
        if approval.task_id is not None:
            task = session.get(Task, approval.task_id)
            if task is not None:
                if decision == ApprovalStatus.APPROVED and task.status != TaskStatus.DONE:
                    before = task.status
                    task.status = TaskStatus.DONE
                    task.updated_at = now_utc()
                    session.add(task)
                    append_event(
                        session,
                        project_id=task.project_id,
                        task_id=task.id,
                        event_type="task.completed",
                        idempotency_key=new_id("idem"),
                        payload={
                            "before": before.value,
                            "after": TaskStatus.DONE.value,
                            "via": "owner_approval",
                        },
                    )
                    append_audit(
                        session,
                        actor="owner",
                        action="task.completed",
                        resource_type="task",
                        resource_id=task.id,
                        project_id=task.project_id,
                        task_id=task.id,
                        before={"status": before.value},
                        after={"status": TaskStatus.DONE.value},
                        idempotency_key=f"audit:task:{task.id}:done",
                    )
                    # Unlock downstream tasks (T7 / T9 READY) using the orchestrator.
                    from aios.orchestrator import Orchestrator

                    Orchestrator(session).process_pending()
                elif decision == ApprovalStatus.REJECTED and task.status != TaskStatus.REVIEW:
                    # Owner returns the gate output for rework (Issue AC: returns to REVIEW).
                    before = task.status
                    task.status = TaskStatus.REVIEW
                    task.updated_at = now_utc()
                    session.add(task)
                    append_audit(
                        session,
                        actor="owner",
                        action="task.returned",
                        resource_type="task",
                        resource_id=task.id,
                        project_id=task.project_id,
                        task_id=task.id,
                        before={"status": before.value},
                        after={"status": TaskStatus.REVIEW.value},
                        idempotency_key=f"audit:task:{task.id}:returned",
                    )
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(approval)
    return approval


def request_revision(session: Session, task_id: str, feedback: str) -> Task:
    """Re-open a returned task to REVIEW and durably record the feedback.

    The feedback is recorded via Event + AuditLog. Real re-export through
    ExternalWorkstationAdapter is deferred to the agent-execution stage (V1 real
    execution is out of scope); the durable record here is what #37 requires.
    """
    task = session.get(Task, task_id)
    if task is None:
        raise ServiceError(404, "Task not found")
    if task.routing_mode != RoutingMode.MANUAL:
        raise ServiceError(400, "仅 owner-gate（MANUAL）任务可在此请求修订")
    before = task.status
    task.status = TaskStatus.REVIEW
    task.updated_at = now_utc()
    try:
        session.add(task)
        append_event(
            session,
            project_id=task.project_id,
            task_id=task.id,
            event_type="task.revision",
            idempotency_key=new_id("idem"),
            payload={"feedback": feedback, "before": before.value},
        )
        append_audit(
            session,
            actor="owner",
            action="task.revision",
            resource_type="task",
            resource_id=task.id,
            project_id=task.project_id,
            task_id=task.id,
            before={"status": before.value},
            after={"status": TaskStatus.REVIEW.value, "feedback": feedback},
            idempotency_key=f"audit:task:{task.id}:revision",
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(task)
    return task


def set_artifact_review_status(
    session: Session,
    artifact_id: str,
    review_status: ArtifactReviewStatus,
) -> Artifact:
    """Update an artifact's review status (UNVERIFIED / APPROVED / REJECTED) + audit."""
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise ServiceError(404, "Artifact not found")
    before = artifact.review_status
    artifact.review_status = review_status
    try:
        session.add(artifact)
        append_audit(
            session,
            actor="owner",
            action="artifact.review_status",
            resource_type="artifact",
            resource_id=artifact.id,
            project_id=artifact.project_id,
            task_id=artifact.task_id,
            before={"review_status": before.value},
            after={"review_status": review_status.value},
            idempotency_key=f"audit:artifact:{artifact.id}:{review_status.value}",
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(artifact)
    return artifact


def get_board(session: Session, project_id: str) -> dict:
    project = session.get(Project, project_id)
    if project is None:
        raise ServiceError(404, "Project not found")
    tasks = list(session.exec(select(Task).where(Task.project_id == project_id)))
    approvals = list(
        session.exec(
            select(Approval).where(
                Approval.project_id == project_id,
                Approval.status == ApprovalStatus.PENDING,
            )
        )
    )
    tasks_by_status: dict[str, list[dict]] = {status.value: [] for status in TaskStatus}
    for task in tasks:
        tasks_by_status[task.status.value].append(task.model_dump(mode="json"))
    return {
        "project": project.model_dump(mode="json"),
        "tasks_by_status": tasks_by_status,
        "pending_approvals": [approval.model_dump(mode="json") for approval in approvals],
    }
