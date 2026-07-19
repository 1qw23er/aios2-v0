from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, Session, SQLModel

from aios.models import new_id, now_utc


class AuditEvent(StrEnum):
    """Canonical audit event taxonomy for the Agent Interoperability Gateway (#57).

    These are the event ``action`` strings emitted across a delegated run's
    lifecycle. They are deliberately stable, vendor-neutral strings so the
    audit trail stays queryable regardless of which external agent ran.
    """

    AGENT_DISCOVER = "agent.discover"
    AGENT_DELEGATE = "agent.delegate"
    AGENT_RESULT_RECEIVED = "agent.result_received"
    ARTIFACT_VALIDATED = "artifact.validated"
    DELEGATION_FAILED = "delegation.failed"
    DELEGATION_CANCELLED = "delegation.cancelled"

# Dict keys whose *values* are credentials and must never be persisted.
SECRET_KEYS = {"secret", "token", "password", "credential", "api_key", "api-key"}
# Header names whose values are credentials (e.g. {"Authorization": "Bearer x"}).
SECRET_HEADER_KEYS = {"authorization", "proxy-authorization", "cookie", "set-cookie"}
# Loose value patterns that look like secrets even when the key is not recognized
# (so a mislabeled field still cannot leak a real credential into the audit log).
# NOTE: no IGNORECASE flag here on purpose -- the high-entropy branch below relies
# on case-sensitive lookaheads to tell random tokens apart from content hashes.
_SECRET_VALUE_RE = re.compile(
    r"""(?x)
    (?:Bearer\s+[A-Za-z0-9._\-]+)          # Authorization: Bearer <token>
    | (?:Basic\s+[A-Za-z0-9+/=]+)           # Authorization: Basic <b64>
    | (?:sk-[A-Za-z0-9]{8,})                # OpenAI-style keys
    | (?:AKIA[0-9A-Z]{8,})                  # AWS access key ids
    | (?:nvapi-[A-Za-z0-9._\-]{8,})         # NVIDIA NIM keys
    | (?:xox[baprs]-[A-Za-z0-9\-]{8,})      # Slack tokens
    # High-entropy token: 40+ chars spanning lower + UPPER + digit. Pure-hex or
    # pure-lowercase content hashes (e.g. ``context_hash``) are deliberately
    # NOT matched -- they are content addresses, not credentials, and must
    # survive intact in the audit trail.
    | (?:
          (?=[A-Za-z0-9_+\-/]*[a-z])
          (?=[A-Za-z0-9_+\-/]*[A-Z])
          (?=[A-Za-z0-9_+\-/]*[0-9])
          [A-Za-z0-9_+\-/]{40,}
      )
    """,
)


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


def redact_secrets(value: Any) -> Any:
    """Recursively sanitize a payload so no credential leaks into persistence.

    Redacts three classes of secret:
      1. dict values whose key matches ``SECRET_KEYS`` (e.g. ``"api_key"``);
      2. dict values whose key matches ``SECRET_HEADER_KEYS`` (e.g.
         ``"Authorization"`` header bundles);
      3. any string value that *looks* like a secret (Bearer/Basic token,
         ``sk-...`` / ``AKIA...`` / ``nvapi-...`` / ``xox*-...`` keys, or a
         high-entropy 40+ char token spanning lower + UPPER + digit) even when
         the key is unrecognized. Pure-hex / pure-lowercase content hashes
         (e.g. ``context_hash``) are intentionally preserved -- they are
         content addresses, not credentials.
    """
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            k = key.lower() if isinstance(key, str) else key
            if k in SECRET_KEYS or k in SECRET_HEADER_KEYS:
                cleaned[key] = "[REDACTED]"
            else:
                cleaned[key] = redact_secrets(item)
        return cleaned
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        return "[REDACTED]"
    return value


def _sanitize(value: Any) -> Any:
    # Public alias kept for backward compatibility with callers.
    return redact_secrets(value)


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
        before_snapshot=redact_secrets(before),
        after_snapshot=redact_secrets(after),
        idempotency_key=idempotency_key,
    )
    session.add(audit)
    return audit
