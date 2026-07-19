from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate
from pydantic import BaseModel, Field

from aios.context_render import render_task_context_markdown, task_context_payload
from aios.delegation import DelegatedExecutionAdapter as _DelegatedExecutionAdapter
from aios.models import Agent as _Agent
from aios.models import DelegationMode as _DelegationMode
from aios.models import TaskContext


class TaskPacket(BaseModel):
    task_id: str
    project: dict[str, Any]
    role: str
    instructions: str
    inputs: list[dict[str, Any]] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    output_schema: dict[str, Any]


class ExternalResult(BaseModel):
    result_id: str
    task_id: str
    summary: str
    claims: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class ExportedTask(BaseModel):
    packet_path: Path
    context_path: Path
    context_json_path: Path | None = None


class ResultValidationError(ValueError):
    pass


class ExternalWorkstationAdapter:
    def __init__(self, outbox: Path, inbox: Path) -> None:
        self.outbox = outbox
        self.inbox = inbox
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)

    def export_task(
        self,
        packet: TaskPacket | dict[str, Any],
        context: str = "",
        *,
        task_context: TaskContext | dict[str, Any] | None = None,
    ) -> ExportedTask:
        normalized = packet if isinstance(packet, TaskPacket) else TaskPacket.model_validate(packet)
        directory = self.outbox / normalized.task_id
        directory.mkdir(parents=True, exist_ok=True)
        packet_path = directory / "task_packet.json"
        context_path = directory / "context.md"
        packet_path.write_text(
            json.dumps(normalized.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        context_json_path = None
        if task_context is None:
            context_path.write_text(context, encoding="utf-8")
        else:
            context_json_path = directory / "task_context.json"
            payload = task_context_payload(task_context)
            context_json_path.write_text(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            context_path.write_text(
                render_task_context_markdown(task_context),
                encoding="utf-8",
            )
        return ExportedTask(
            packet_path=packet_path,
            context_path=context_path,
            context_json_path=context_json_path,
        )

    def import_result(self, path: Path) -> ExternalResult:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            result = ExternalResult.model_validate(payload)
            packet_path = self.outbox / result.task_id / "task_packet.json"
            packet = TaskPacket.model_validate_json(packet_path.read_text(encoding="utf-8"))
            validate(instance=result.model_dump(mode="json"), schema=packet.output_schema)
        except (OSError, ValueError, ValidationError) as error:
            raise ResultValidationError(str(error)) from error
        return result


# --- Agent Interoperability Gateway (#57), capability B: workstation export/import ---
# Reuses the existing ExternalWorkstationAdapter rather than rebuilding it. The
# closed-source agent (e.g. 扣子/Coze no-code bot) is driven by a human/operator
# who exports the task packet, runs it in the external tool, and drops the result
# file back into the inbox. The agent never touches Task/Approval/KnowledgeFact
# state directly; it only produces a validated ExternalResult -> Artifact.


class WorkstationAdapter(_DelegatedExecutionAdapter):
    """Delegated adapter backed by local export/import packages (workstation mode).

    Unlike RemoteApiAdapter, there is no live callback: submission writes the
    task packet to the outbox (WAITING_EXTERNAL), and ingestion reads the result
    file once an operator has placed it in the inbox. The run is marked RUNNING
    until a result appears.
    """

    mode = _DelegationMode.WORKSTATION

    def __init__(self, *, agent: _Agent, outbox: Path, inbox: Path) -> None:
        super().__init__(agent=agent)
        self._ws = ExternalWorkstationAdapter(outbox=outbox, inbox=inbox)

    def submit(self, *, delegated_run, projected_context, output_schema, remote_callback_url):
        packet = {
            "task_id": delegated_run.task_id,
            "project": {"id": delegated_run.project_id},
            "role": self.agent.role,
            "instructions": projected_context.get("instructions", ""),
            "inputs": projected_context.get("dependency_outputs", []),
            "acceptance_criteria": projected_context.get("acceptance_criteria", []),
            "output_schema": output_schema,
        }
        exported = self._ws.export_task(packet, context=projected_context.get("objective", ""))
        # Cache the output schema alongside the packet so ingest_result can
        # re-validate the delivered artifact data without a base-class signature
        # change. (ExternalWorkstationAdapter.import_result validates the whole
        # ExternalResult against output_schema, which is the wrong shape here.)
        schema_path = exported.packet_path.parent / "output_schema.json"
        schema_path.write_text(
            json.dumps(output_schema, ensure_ascii=False), encoding="utf-8"
        )
        return {
            "remote_run_id": f"ws:{delegated_run.task_id}",
            "remote_status": "waiting_external",
            "exported_to": str(exported.packet_path),
        }

    def status(self, *, delegated_run):
        result_file = self._ws.inbox / f"{delegated_run.task_id}.result.json"
        if result_file.exists():
            return {"remote_status": "succeeded", "finished": True, "result": None}
        return {"remote_status": "waiting_external", "finished": False, "result": None}

    def cancel(self, *, delegated_run):
        pass  # workstation runs are operator-driven; nothing to cancel remotely

    def ingest_result(self, *, delegated_run):
        # Read the operator-dropped result, validate each artifact's *data*
        # against the cached output schema, and normalize to an Artifact dict.
        # We intentionally bypass ExternalWorkstationAdapter.import_result because
        # its schema check applies to the top-level ExternalResult, not the
        # per-artifact data the task's output_schema actually describes.
        result_file = self._ws.inbox / f"{delegated_run.task_id}.result.json"
        payload = json.loads(result_file.read_text(encoding="utf-8"))
        external = ExternalResult.model_validate(payload)
        schema_path = (
            self._ws.outbox / delegated_run.task_id / "output_schema.json"
        )
        output_schema = (
            json.loads(schema_path.read_text(encoding="utf-8"))
            if schema_path.exists()
            else None
        )
        if output_schema is not None:
            for art in external.artifacts:
                data = art.get("data", {})
                try:
                    validate(instance=data, schema=output_schema)
                except ValidationError as exc:  # noqa: BLE001
                    raise ResultValidationError(
                        f"artifact {art.get('uri')} failed schema: {exc.message}"
                    ) from exc
        return {
            "summary": external.summary,
            "artifacts": external.artifacts,
        }
