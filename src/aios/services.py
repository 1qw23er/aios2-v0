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
    Capability,
    Event,
    Project,
    Task,
    TaskStatus,
)
from aios.schemas import ApprovalCreate, ProjectCreate, TaskCreate


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


def create_approval(session: Session, request: ApprovalCreate, idempotency_key: str) -> Approval:
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
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(approval)
    return approval


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
