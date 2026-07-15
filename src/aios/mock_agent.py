from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlmodel import Session, select

from aios.audit import append_audit
from aios.models import Artifact, ArtifactType, Task, TaskStatus, now_utc
from aios.services import ServiceError, append_event


class MockApiAgent:
    def __init__(self, session: Session) -> None:
        self.session = session

    def complete(self, task_id: str, result: dict[str, Any], idempotency_key: str) -> Artifact:
        result_id = f"mock:{idempotency_key}"
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
        checksum = hashlib.sha256(encoded).hexdigest()
        existing = self.session.exec(
            select(Artifact).where(Artifact.external_result_id == result_id)
        ).first()
        if existing is not None:
            if existing.result_checksum != checksum:
                raise ServiceError(409, "Mock result key conflicts with another payload")
            return existing
        task = self.session.get(Task, task_id)
        if task is None:
            raise ServiceError(404, "Task not found")
        artifact = Artifact(
            project_id=task.project_id,
            task_id=task.id,
            type=ArtifactType.JSON,
            uri=f"mock://{task.id}/{idempotency_key}",
            checksum=checksum,
            external_result_id=result_id,
            result_checksum=checksum,
            metadata_json={"mock_result": result},
        )
        before = task.status
        task.status = TaskStatus.DONE
        task.updated_at = now_utc()
        try:
            self.session.add_all([artifact, task])
            append_event(
                self.session,
                project_id=task.project_id,
                task_id=task.id,
                event_type="task.completed",
                idempotency_key=f"mock:{idempotency_key}:completed",
                payload={"artifact_id": artifact.id},
            )
            append_audit(
                self.session,
                actor="mock_api_agent",
                action="task.completed",
                resource_type="task",
                resource_id=task.id,
                project_id=task.project_id,
                task_id=task.id,
                before={"status": before.value},
                after={"status": TaskStatus.DONE.value, "artifact_id": artifact.id},
                idempotency_key=f"audit:mock:{idempotency_key}",
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(artifact)
        return artifact
