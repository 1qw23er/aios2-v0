from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate
from pydantic import BaseModel, Field


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


class ResultValidationError(ValueError):
    pass


class ExternalWorkstationAdapter:
    def __init__(self, outbox: Path, inbox: Path) -> None:
        self.outbox = outbox
        self.inbox = inbox
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)

    def export_task(self, packet: TaskPacket | dict[str, Any], context: str = "") -> ExportedTask:
        normalized = packet if isinstance(packet, TaskPacket) else TaskPacket.model_validate(packet)
        directory = self.outbox / normalized.task_id
        directory.mkdir(parents=True, exist_ok=True)
        packet_path = directory / "task_packet.json"
        context_path = directory / "context.md"
        packet_path.write_text(
            json.dumps(normalized.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        context_path.write_text(context, encoding="utf-8")
        return ExportedTask(packet_path=packet_path, context_path=context_path)

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
