from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlmodel import Session, select

from aios.adapters.external import ExternalWorkstationAdapter, ResultValidationError
from aios.audit import append_audit
from aios.models import Artifact, ArtifactType, Task, TaskStatus, now_utc
from aios.services import ServiceError, append_event


def import_external_result(
    session: Session, adapter: ExternalWorkstationAdapter, path: Path
) -> Artifact:
    try:
        result = adapter.import_result(path)
    except ResultValidationError:
        _record_rejection(session, path)
        raise
    normalized = result.model_dump(mode="json")
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    checksum = hashlib.sha256(encoded).hexdigest()
    existing = session.exec(
        select(Artifact).where(Artifact.external_result_id == result.result_id)
    ).first()
    if existing is not None:
        if existing.result_checksum != checksum:
            raise ServiceError(409, "External result ID conflicts with another payload")
        return existing
    task = session.get(Task, result.task_id)
    if task is None:
        raise ServiceError(404, "Task not found")
    artifact = Artifact(
        project_id=task.project_id,
        task_id=task.id,
        type=ArtifactType.JSON,
        uri=str(path),
        checksum=checksum,
        external_result_id=result.result_id,
        result_checksum=checksum,
        metadata_json={"external_result": normalized},
    )
    before = task.status
    task.status = TaskStatus.DONE
    task.updated_at = now_utc()
    try:
        session.add_all([artifact, task])
        append_event(
            session,
            project_id=task.project_id,
            task_id=task.id,
            event_type="task.completed",
            idempotency_key=f"external:{result.result_id}:completed",
            payload={"artifact_id": artifact.id, "result_id": result.result_id},
        )
        append_audit(
            session,
            actor="external_workstation",
            action="external_result.imported",
            resource_type="artifact",
            resource_id=artifact.id,
            project_id=task.project_id,
            task_id=task.id,
            before={"task_status": before.value},
            after={"task_status": TaskStatus.DONE.value, "result_id": result.result_id},
            idempotency_key=f"audit:external:{result.result_id}:imported",
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(artifact)
    return artifact


def _record_rejection(session: Session, path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        task = session.get(Task, payload.get("task_id"))
        if task is None:
            return
        before = task.status
        task.status = TaskStatus.REJECTED
        task.updated_at = now_utc()
        session.add(task)
        append_audit(
            session,
            actor="external_workstation",
            action="external_result.rejected",
            resource_type="task",
            resource_id=task.id,
            project_id=task.project_id,
            task_id=task.id,
            before={"status": before.value},
            after={"status": TaskStatus.REJECTED.value},
            idempotency_key=f"audit:external:rejected:{hashlib.sha256(path.read_bytes()).hexdigest()}",
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
