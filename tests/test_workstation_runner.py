"""PR-3 tests: workstation_runner 搬运工 daemon 的契约与端到端闭环。

覆盖：
  1. process_task 用 FakeExecutor 写回符合 ExternalResult 的结果文件；
  2. 真实 WorkstationAdapter.ingest_result 能收下该结果（PR-1 接线 + PR-3 产出闭环）；
  3. discover_pending 正确跳过 .done / .pending_manual / .error 哨兵；
  4. ManualExecutor 写 .pending_manual 且不写结果；
  5. LLMExecutor 在无网络下（monkeypatch urlopen）正确解析 JSON。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from aios.adapters.external import ExternalResult, TaskPacket, WorkstationAdapter
from aios.models import Agent, DelegationMode
from scripts.workstation_runner import (
    AwaitingManualResult,
    discover_pending,
    make_llm_executor,
    make_manual_executor,
    process_task,
)

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"x": {"type": "string"}},
    "required": ["x"],
}


def _write_task(outbox: Any, task_id: str, schema: dict) -> Any:
    task_dir = outbox / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    packet = TaskPacket(
        task_id=task_id,
        project={"id": "p1"},
        role="content_production",
        instructions="produce x",
        inputs=[],
        acceptance_criteria=["has x"],
        output_schema=schema,
    )
    (task_dir / "task_packet.json").write_text(
        packet.model_dump_json(indent=2), encoding="utf-8"
    )
    (task_dir / "output_schema.json").write_text(
        json_dumps(schema), encoding="utf-8"
    )
    (task_dir / "context.md").write_text("# task context", encoding="utf-8")
    return task_dir


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, indent=2)


def _fake_executor(expected: dict) -> Any:
    def _exec(*, task, context, output_schema):
        return expected

    return _exec


def test_process_task_writes_external_result(tmp_path: Any) -> None:
    outbox = tmp_path / "outbox"
    inbox = tmp_path / "inbox"
    task_dir = _write_task(outbox, "t1", OUTPUT_SCHEMA)
    process_task(task_dir, inbox, _fake_executor({"x": "hello"}))
    result_file = inbox / "t1.result.json"
    assert result_file.exists()
    result = ExternalResult.model_validate_json(result_file.read_text(encoding="utf-8"))
    assert result.task_id == "t1"
    assert result.artifacts[0]["data"] == {"x": "hello"}
    assert (task_dir / ".done").exists()


def test_runner_result_is_accepted_by_workstation_adapter(tmp_path: Any) -> None:
    """端到端：runner 产出 -> 真实 WorkstationAdapter.ingest_result 收下（PR-1+PR-3 闭环）。"""
    outbox = tmp_path / "outbox"
    inbox = tmp_path / "inbox"
    task_dir = _write_task(outbox, "t2", OUTPUT_SCHEMA)
    process_task(task_dir, inbox, _fake_executor({"x": "world"}))

    agent = Agent(
        id="agt-ws",
        name="W",
        role="content_production",
        adapter_type="external",  # type: ignore[arg-type]
        delegation_mode=DelegationMode.WORKSTATION,
        platform="workbuddy",
    )
    adapter = WorkstationAdapter(agent=agent, outbox=outbox, inbox=inbox)
    ingested = adapter.ingest_result(delegated_run=SimpleNamespace(task_id="t2"))
    assert ingested["summary"]
    assert ingested["artifacts"][0]["data"] == {"x": "world"}


def test_discover_pending_skips_sentinels(tmp_path: Any) -> None:
    outbox = tmp_path / "outbox"
    _write_task(outbox, "pending", OUTPUT_SCHEMA)
    _write_task(outbox, "done", OUTPUT_SCHEMA)
    (outbox / "done" / ".done").write_text("r", encoding="utf-8")
    _write_task(outbox, "manual", OUTPUT_SCHEMA)
    (outbox / "manual" / ".pending_manual").write_text("t", encoding="utf-8")
    _write_task(outbox, "err", OUTPUT_SCHEMA)
    (outbox / "err" / ".error").write_text("boom", encoding="utf-8")

    found = discover_pending(outbox)
    names = {d.name for d in found}
    assert names == {"pending"}


def test_manual_executor_writes_pending_sentinel(tmp_path: Any) -> None:
    outbox = tmp_path / "outbox"
    inbox = tmp_path / "inbox"
    task_dir = _write_task(outbox, "m1", OUTPUT_SCHEMA)
    with pytest.raises(AwaitingManualResult):
        process_task(task_dir, inbox, make_manual_executor())
    assert (task_dir / ".pending_manual").exists()
    assert not (inbox / "m1.result.json").exists()


def test_llm_executor_parses_json_without_network(tmp_path: Any) -> None:
    outbox = tmp_path / "outbox"
    inbox = tmp_path / "inbox"
    task_dir = _write_task(outbox, "llm1", OUTPUT_SCHEMA)

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json_dumps(
                {"choices": [{"message": {"content": json_dumps({"x": "from-llm"})}}]}
            ).encode("utf-8")

    executor = make_llm_executor(base_url="http://x", api_key="k", model="m")
    with patch("urllib.request.urlopen", return_value=_FakeResp()):
        process_task(task_dir, inbox, executor)
    result = ExternalResult.model_validate_json(
        (inbox / "llm1.result.json").read_text(encoding="utf-8")
    )
    assert result.artifacts[0]["data"] == {"x": "from-llm"}
