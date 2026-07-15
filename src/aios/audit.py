from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, Session, SQLModel

from aios.models import new_id, now_utc

SECRET_KEYS = {"secret", "token", "password", "credential", "api_key", "api-key"}


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"

    id: str = Field(default_factory=lambda: new_id("aud"), primary_key=True)
    actor: str
    action: str = Field(index=True)
    resource_type: str
    resource_id: str
    project_id: str | None = Field(default=None, index=True)
    task_id: str | None = Field(default=None, index=True)
    before_snapshot: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    after_snapshot: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    idempotency_key: str = Field(unique=True, index=True)
    created_at: datetime = Field(default_factory=now_utc)


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in SECRET_KEYS else _sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def append_audit(
    session: Session,
    *,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str,
    project_id: str | None,
    task_id: str | None,
    before: dict[str, Any],
    after: dict[str, Any],
    idempotency_key: str,
) -> AuditLog:
    audit = AuditLog(
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        project_id=project_id,
        task_id=task_id,
        before_snapshot=_sanitize(before),
        after_snapshot=_sanitize(after),
        idempotency_key=idempotency_key,
    )
    session.add(audit)
    return audit
