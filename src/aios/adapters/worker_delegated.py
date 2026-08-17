"""Generic WorkerClient bridge into the existing delegated lifecycle."""

from __future__ import annotations

from typing import Any

from aios.delegation import DelegatedExecutionAdapter, DelegatedExecutionError
from aios.models import Agent, DelegatedRun, DelegationMode
from aios.worker_contract import (
    ContractValidationError,
    TaskEnvelope,
    UnsupportedCapability,
    WorkerClient,
    WorkerResult,
    WorkerState,
)


class WorkerDelegatedAdapter:
    """Implement DelegatedAdapter hooks without owning the AIOS lifecycle."""

    def __init__(self, *, agent: Agent, client: WorkerClient) -> None:
        self.agent = agent
        self.client = client
        self._context_hash: str | None = None
        self._envelopes: dict[str, TaskEnvelope] = {}
        self._results: dict[str, WorkerResult] = {}

    def as_execution_adapter(self) -> DelegatedExecutionAdapter:
        """Wrap this bridge with the existing lifecycle owner."""
        return _WorkerLifecycleAdapter(bridge=self)

    def set_context_hash(self, context_hash: str) -> None:
        self._context_hash = context_hash

    def discover_capabilities(self) -> dict[str, Any]:
        capabilities = self.client.discover()
        return {
            "worker_id": capabilities.worker_id,
            "protocol_version": capabilities.protocol_version,
            "runtime_name": capabilities.runtime_name,
            "runtime_version": capabilities.runtime_version,
            "cancellation": capabilities.cancellation,
            "checkpoint_resume": capabilities.checkpoint_resume,
        }

    def submit(
        self,
        *,
        delegated_run: DelegatedRun,
        projected_context: dict[str, Any],
        output_schema: dict[str, Any],
        remote_callback_url: str | None,
    ) -> dict[str, Any]:
        del remote_callback_url
        if delegated_run.id in self._envelopes:
            raise DelegatedExecutionError("worker execution already submitted for this attempt")
        if self._context_hash is None:
            raise DelegatedExecutionError("worker execution requires a TaskContext hash")
        try:
            capabilities = self.client.discover()
            capabilities.require_execution_capabilities()
        except ContractValidationError as exc:
            raise DelegatedExecutionError(str(exc)) from exc
        envelope = TaskEnvelope(
            task_id=delegated_run.task_id,
            execution_id=delegated_run.id,
            idempotency_key=delegated_run.idempotency_key,
            context_hash=self._context_hash,
            instructions=str(projected_context.get("instructions", "")),
            structured_context=projected_context,
            expected_output_schema=output_schema,
            permission_profile=self._permission_profile(),
            total_attempt_limit=1,
        )
        try:
            submission = self.client.submit(envelope)
        except Exception as exc:  # noqa: BLE001 - normalize the worker boundary
            raise DelegatedExecutionError(str(exc)) from exc
        if submission.execution_id != envelope.execution_id:
            raise DelegatedExecutionError("worker submission execution_id mismatch")
        self._envelopes[delegated_run.id] = envelope
        return {
            "remote_run_id": f"{submission.runtime_session_id}:{submission.message_id}",
            "remote_status": WorkerState.ACCEPTED.value,
        }

    def status(self, *, delegated_run: DelegatedRun) -> dict[str, Any]:
        try:
            status = self.client.status(delegated_run.id)
        except Exception as exc:  # noqa: BLE001 - existing lifecycle owns retry
            raise DelegatedExecutionError(str(exc)) from exc
        finished = status.state in {
            WorkerState.COMPLETED,
            WorkerState.FAILED,
            WorkerState.CANCELLED,
            WorkerState.INTERRUPTED,
        }
        response: dict[str, Any] = {
            "remote_status": status.state.value,
            "finished": finished,
            "success": status.state is WorkerState.COMPLETED,
        }
        if finished:
            result = self._validated_result(delegated_run.id)
            response["usage"] = result.usage
            if "cost" in result.usage:
                response["cost"] = result.usage["cost"]
            if result.error:
                response["error"] = str(result.error.get("message", "worker failed"))
        return response

    def ingest_result(self, *, delegated_run: DelegatedRun) -> dict[str, Any]:
        result = self._validated_result(delegated_run.id)
        data = result.structured_output
        if data is None:
            data = {"text": result.text}
        artifacts = list(result.artifact_candidates) or [
            {"type": "json", "uri": f"worker://{delegated_run.id}", "data": data}
        ]
        return {"summary": result.text or "delegated worker result", "artifacts": artifacts}

    def cancel(self, *, delegated_run: DelegatedRun) -> UnsupportedCapability:
        return self.client.cancel(delegated_run.id)

    def resume(self, resume_ref: str) -> UnsupportedCapability:
        return self.client.resume(resume_ref)

    def _validated_result(self, execution_id: str) -> WorkerResult:
        cached = self._results.get(execution_id)
        if cached is not None:
            return cached
        envelope = self._envelopes.get(execution_id)
        if envelope is None:
            raise DelegatedExecutionError("worker result requested before submission")
        try:
            result = self.client.result(execution_id)
        except Exception as exc:  # noqa: BLE001 - normalize the worker boundary
            raise DelegatedExecutionError(str(exc)) from exc
        try:
            result.validate_for(envelope)
        except ContractValidationError as exc:
            raise DelegatedExecutionError(str(exc)) from exc
        self._results[execution_id] = result
        return result

    def _permission_profile(self) -> str:
        profiles = [value for value in self.agent.permissions if value]
        if len(profiles) != 1:
            raise DelegatedExecutionError("worker agent requires one explicit permission profile")
        return profiles[0]


class _WorkerLifecycleAdapter(DelegatedExecutionAdapter):
    """Thin hook binding; DelegatedExecutionAdapter still owns all lifecycle work."""

    mode = DelegationMode.REMOTE_API

    def __init__(self, *, bridge: WorkerDelegatedAdapter) -> None:
        self._bridge = bridge
        super().__init__(agent=bridge.agent)

    def _project_context(self, task_context: Any) -> dict[str, Any]:
        context_hash = getattr(task_context, "context_hash", None)
        if not context_hash:
            raise DelegatedExecutionError("worker execution requires a TaskContext hash")
        self._bridge.set_context_hash(context_hash)
        return super()._project_context(task_context)

    def discover_capabilities(self) -> dict[str, Any]:
        return self._bridge.discover_capabilities()

    def submit(self, **kwargs: Any) -> dict[str, Any]:
        return self._bridge.submit(**kwargs)

    def status(self, **kwargs: Any) -> dict[str, Any]:
        return self._bridge.status(**kwargs)

    def cancel(self, **kwargs: Any) -> UnsupportedCapability:
        return self._bridge.cancel(**kwargs)

    def ingest_result(self, **kwargs: Any) -> dict[str, Any]:
        return self._bridge.ingest_result(**kwargs)
