from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class ExternalResult(BaseModel):
    task_id: str
    summary: str
    claims: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class ExternalWorkstationAdapter:
    def __init__(self, outbox: Path, inbox: Path) -> None:
        self.outbox = outbox
        self.inbox = inbox
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)

    def export_task(self, packet: dict[str, Any]) -> Path:
        task_id = packet["task_id"]
        path = self.outbox / f"{task_id}.task.json"
        path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def import_result(self, path: Path) -> ExternalResult:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ExternalResult.model_validate(payload)
