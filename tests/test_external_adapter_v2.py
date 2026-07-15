import json
from pathlib import Path

import pytest

from aios.adapters.external import (
    ExternalWorkstationAdapter,
    ResultValidationError,
    TaskPacket,
)

RESULT_SCHEMA = {
    "type": "object",
    "required": ["result_id", "task_id", "summary", "claims", "artifacts"],
    "properties": {
        "result_id": {"type": "string", "minLength": 1},
        "task_id": {"const": "tsk_1"},
        "summary": {"type": "string", "minLength": 1},
        "claims": {"type": "array"},
        "artifacts": {"type": "array"},
    },
}


def test_export_writes_task_packet_and_context(tmp_path: Path) -> None:
    adapter = ExternalWorkstationAdapter(tmp_path / "outbox", tmp_path / "inbox")
    packet = TaskPacket(
        task_id="tsk_1",
        project={"id": "prj_1", "objective": "Publish"},
        role="researcher",
        instructions="Research the market",
        acceptance_criteria=["Cite sources"],
        output_schema=RESULT_SCHEMA,
    )

    exported = adapter.export_task(packet, "# Project context")

    assert exported.packet_path.name == "task_packet.json"
    assert json.loads(exported.packet_path.read_text(encoding="utf-8"))["task_id"] == "tsk_1"
    assert exported.context_path.read_text(encoding="utf-8") == "# Project context"


def test_import_validates_declared_schema(tmp_path: Path) -> None:
    adapter = ExternalWorkstationAdapter(tmp_path / "outbox", tmp_path / "inbox")
    packet = TaskPacket(
        task_id="tsk_1",
        project={"id": "prj_1", "objective": "Publish"},
        role="researcher",
        instructions="Research",
        output_schema=RESULT_SCHEMA,
    )
    adapter.export_task(packet, "context")
    result_path = tmp_path / "inbox" / "bad.result.json"
    result_path.write_text(
        json.dumps(
            {
                "result_id": "res_1",
                "task_id": "tsk_1",
                "summary": "",
                "claims": [],
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ResultValidationError):
        adapter.import_result(result_path)
