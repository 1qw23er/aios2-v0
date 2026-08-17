from __future__ import annotations

import pytest

from aios.worker_contract import (
    ContractValidationError,
    TaskEnvelope,
    UnsupportedCapability,
    WorkerCapabilities,
    WorkerResult,
    WorkerState,
)


def envelope() -> TaskEnvelope:
    return TaskEnvelope(
        task_id="tsk-1",
        execution_id="run-1",
        idempotency_key="idem-1",
        context_hash="ctx-1",
        instructions="Return JSON",
        structured_context={},
        expected_output_schema={"type": "object"},
        permission_profile="read_only",
        total_attempt_limit=1,
    )


def test_v1_negotiates_explicitly_unsupported_cancel_and_resume() -> None:
    capabilities = WorkerCapabilities.deepseek_harness_v1(
        worker_id="dsh", runtime_version="0.0.1"
    )

    assert capabilities.cancellation is False
    assert capabilities.checkpoint_resume is False
    assert capabilities.result_aggregation == "terminal_events"
    assert capabilities.require_execution_capabilities() is None


def test_unsupported_capability_is_never_success() -> None:
    result = UnsupportedCapability.for_capability("cancellation", worker_id="dsh")

    assert result.ok is False
    assert result.code == "unsupported_capability"
    assert result.capability == "cancellation"


def test_result_rejects_execution_or_context_mismatch() -> None:
    result = WorkerResult(
        execution_id="other",
        status=WorkerState.COMPLETED,
        context_hash="ctx-1",
        structured_output={"ok": True},
    )

    with pytest.raises(ContractValidationError, match="execution_id"):
        result.validate_for(envelope())


@pytest.mark.parametrize("usage", [{"input_tokens": -1}, {"cost": float("inf")}])
def test_result_rejects_invalid_usage(usage: dict[str, float]) -> None:
    result = WorkerResult(
        execution_id="run-1",
        status=WorkerState.COMPLETED,
        context_hash="ctx-1",
        structured_output={"ok": True},
        usage=usage,
    )

    with pytest.raises(ContractValidationError, match="usage"):
        result.validate_for(envelope())
