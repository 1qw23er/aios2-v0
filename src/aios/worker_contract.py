"""Vendor-neutral runtime worker messages used only at adapter boundaries."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class ContractValidationError(ValueError):
    """A worker message violates the versioned adapter protocol."""


class WorkerState(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class WorkerCapabilities:
    worker_id: str
    protocol_version: str
    runtime_name: str
    runtime_version: str
    discover: bool
    submit: bool
    status: bool
    events: bool
    result: bool
    usage: bool
    runtime_reference: bool
    permission_fail_closed: bool
    cancellation: bool
    checkpoint_resume: bool
    result_aggregation: str = "native"

    @classmethod
    def deepseek_harness_v1(
        cls, *, worker_id: str, runtime_version: str
    ) -> WorkerCapabilities:
        return cls(
            worker_id=worker_id,
            protocol_version="aios.worker/v1",
            runtime_name="deepseek-harness",
            runtime_version=runtime_version,
            discover=True,
            submit=True,
            status=True,
            events=True,
            result=True,
            usage=True,
            runtime_reference=True,
            permission_fail_closed=True,
            cancellation=False,
            checkpoint_resume=False,
            result_aggregation="terminal_events",
        )

    def require_execution_capabilities(self) -> None:
        missing = [
            name
            for name in (
                "discover",
                "submit",
                "status",
                "events",
                "result",
                "usage",
                "runtime_reference",
                "permission_fail_closed",
            )
            if not getattr(self, name)
        ]
        if missing:
            raise ContractValidationError(
                f"worker lacks required capabilities: {', '.join(missing)}"
            )


@dataclass(frozen=True)
class TaskEnvelope:
    task_id: str
    execution_id: str
    idempotency_key: str
    context_hash: str
    instructions: str
    structured_context: dict[str, Any]
    expected_output_schema: dict[str, Any]
    permission_profile: str
    total_attempt_limit: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all((self.task_id, self.execution_id, self.idempotency_key, self.context_hash)):
            raise ContractValidationError("task/execution/idempotency/context identity is required")
        if self.total_attempt_limit != 1:
            raise ContractValidationError(
                "one AIOS run attempt permits exactly one worker submission"
            )


@dataclass(frozen=True)
class WorkerSubmission:
    execution_id: str
    runtime_session_id: str
    message_id: str


@dataclass(frozen=True)
class WorkerStatus:
    state: WorkerState
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerEvent:
    kind: str
    execution_id: str
    cursor: str
    metadata: dict[str, Any] = field(default_factory=dict)
    trace_ref: str | None = None


@dataclass(frozen=True)
class WorkerResult:
    execution_id: str
    status: WorkerState
    context_hash: str
    text: str | None = None
    structured_output: dict[str, Any] | None = None
    artifact_candidates: tuple[dict[str, Any], ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)
    runtime_metadata: dict[str, Any] = field(default_factory=dict)
    trace_ref: str | None = None
    error: dict[str, Any] | None = None

    def validate_for(self, envelope: TaskEnvelope) -> None:
        if self.execution_id != envelope.execution_id:
            raise ContractValidationError("result execution_id does not match envelope")
        if self.context_hash != envelope.context_hash:
            raise ContractValidationError("result context_hash does not match envelope")
        if self.status not in {
            WorkerState.COMPLETED,
            WorkerState.FAILED,
            WorkerState.CANCELLED,
            WorkerState.INTERRUPTED,
        }:
            raise ContractValidationError("result status must be terminal")
        if self.status is WorkerState.COMPLETED and not (
            self.text is not None or self.structured_output is not None
        ):
            raise ContractValidationError("completed result requires output")
        for value in self.usage.values():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ContractValidationError("usage values must be numeric")
            if not math.isfinite(float(value)) or value < 0:
                raise ContractValidationError("usage values must be finite and non-negative")


@dataclass(frozen=True)
class UnsupportedCapability:
    capability: str
    worker_id: str
    ok: bool = False
    code: str = "unsupported_capability"
    supported: bool = False

    @classmethod
    def for_capability(cls, capability: str, *, worker_id: str) -> UnsupportedCapability:
        return cls(capability=capability, worker_id=worker_id)


class WorkerClient(Protocol):
    def discover(self) -> WorkerCapabilities: ...
    def submit(self, envelope: TaskEnvelope) -> WorkerSubmission: ...
    def status(self, execution_id: str) -> WorkerStatus: ...
    def events(self, execution_id: str, after_cursor: str | None = None) -> list[WorkerEvent]: ...
    def result(self, execution_id: str) -> WorkerResult: ...
    def cancel(self, execution_id: str) -> UnsupportedCapability: ...
    def resume(self, resume_ref: str) -> UnsupportedCapability: ...
