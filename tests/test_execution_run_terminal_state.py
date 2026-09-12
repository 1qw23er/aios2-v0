"""P0 regression: paid LLM attempts must never dangle in SUBMITTED.

Before this fix, ``LLMExecutionAdapter._parse_json`` raised inside the ``else``
branch of the ``_chat()`` try/except. That exception escaped ``run()`` entirely,
so the already-created ``DelegatedRun`` was never terminalized: it stayed
``SUBMITTED`` with ``model=None`` / ``usage=None`` / ``cost=0`` -- the paid
attempt's attribution was silently lost (the task was still marked FAILED by
``execute_task``'s outer handler, but the run record was orphaned).

These tests drive the REAL ``run()`` path with the model call substituted, and
assert the ``DelegatedRun`` terminal state + attribution on every failure exit.
"""

from __future__ import annotations

import types
from contextlib import contextmanager
from typing import Any

import pytest
from sqlmodel import Session, select

from aios.db import get_engine, run_migrations
from aios.execution import (
    AdapterErrorCategory,
    ExecutionError,
    LLMExecutionAdapter,
    _parse_run_error,
)
from aios.models import DelegatedRun, DelegatedRunStatus, Project, Task, TaskStatus

USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


@pytest.fixture
def engine(tmp_path):
    url = f"sqlite:///{tmp_path / 'term.db'}"
    run_migrations(url)
    eng = get_engine(url)
    with Session(eng) as s:
        s.add(Project(id="p1", name="P0", objective="x"))
        # DelegatedRun.task_id is a FK to task.id, so a task row must exist for
        # create_local_run() to insert the evidence row.
        s.add(
            Task(
                id="t1",
                project_id="p1",
                title="P0 target task",
                description="x",
                status=TaskStatus.READY,
            )
        )
        s.commit()
    yield eng


@pytest.fixture
def patched(monkeypatch, engine):
    @contextmanager
    def _ms():
        with Session(engine) as s:
            yield s

    monkeypatch.setattr("aios.execution.make_session", _ms)
    yield


def _adapter() -> LLMExecutionAdapter:
    return LLMExecutionAdapter(
        model="deepseek-v4-flash", api_key="x", base_url="http://x"
    )


def _ctx() -> Any:
    # run() only reads task_context.project_id (for create_local_run); the prompt
    # is produced by the monkeypatched _build_prompt, so no real TaskContext needed.
    return types.SimpleNamespace(project_id="p1")


def _runs(engine) -> list[DelegatedRun]:
    with Session(engine) as s:
        return list(s.exec(select(DelegatedRun).order_by(DelegatedRun.attempt)))


def _defining_run(runs: list[DelegatedRun]) -> DelegatedRun:
    # The terminal (erroring) run is the last one recorded.
    return runs[-1]


def test_parse_failure_terminalizes_run_failed_with_attribution(
    monkeypatch, patched, engine
) -> None:
    a = _adapter()
    monkeypatch.setattr(a, "_build_prompt", lambda *a_, **k: "")
    monkeypatch.setattr(a, "_chat", lambda *a_, **k: ("not json at all", USAGE))

    def _raise(_t: str) -> Any:
        raise ExecutionError(
            502, "模型返回无法解析为 JSON", category=AdapterErrorCategory.JSON_PARSE
        )

    monkeypatch.setattr(a, "_parse_json", staticmethod(_raise))

    with pytest.raises(ExecutionError):
        a.run(
            task_id="t1",
            task_context=_ctx(),
            output_schema={"type": "object"},
            idempotency_key="k1",
        )

    runs = _runs(engine)
    assert runs, "no DelegatedRun was recorded"
    assert all(r.status != DelegatedRunStatus.SUBMITTED for r in runs)
    run = _defining_run(runs)
    assert run.status == DelegatedRunStatus.FAILED
    # P0 core: the attempted model is attributable even on failure.
    assert run.model == "deepseek-v4-flash"
    # A paid-but-parsed attempt still reports usage (cost can be derived later).
    assert run.usage == USAGE
    phase, error_type, detail = _parse_run_error(run.error)
    assert error_type == AdapterErrorCategory.JSON_PARSE.value
    assert phase == "parse"
    assert "无法解析" in (detail or "")


def test_chat_http_error_terminalizes_run_failed(
    monkeypatch, patched, engine
) -> None:
    a = _adapter()
    monkeypatch.setattr(a, "_build_prompt", lambda *a_, **k: "")

    def _chat_fail(*a_, **k):
        raise ExecutionError(
            502, "模型返回 HTTP 502", category=AdapterErrorCategory.PROVIDER_HTTP
        )

    monkeypatch.setattr(a, "_chat", _chat_fail)

    with pytest.raises(ExecutionError):
        a.run(
            task_id="t1",
            task_context=_ctx(),
            output_schema={"type": "object"},
            idempotency_key="k2",
        )

    runs = _runs(engine)
    assert all(r.status != DelegatedRunStatus.SUBMITTED for r in runs)
    run = _defining_run(runs)
    assert run.status == DelegatedRunStatus.FAILED
    assert run.model == "deepseek-v4-flash"
    phase, error_type, detail = _parse_run_error(run.error)
    assert error_type == AdapterErrorCategory.PROVIDER_HTTP.value
    assert phase == "chat"


def test_successful_call_but_non_dict_result_is_terminalized(
    monkeypatch, patched, engine
) -> None:
    a = _adapter()
    monkeypatch.setattr(a, "_build_prompt", lambda *a_, **k: "")
    monkeypatch.setattr(a, "_chat", lambda *a_, **k: ("[1, 2, 3]", USAGE))
    monkeypatch.setattr(a, "_parse_json", staticmethod(lambda t: [1, 2, 3]))

    with pytest.raises(ExecutionError):
        a.run(
            task_id="t1",
            task_context=_ctx(),
            output_schema={"type": "object"},
            idempotency_key="k3",
        )

    runs = _runs(engine)
    run = _defining_run(runs)
    assert run.status == DelegatedRunStatus.FAILED
    assert run.model == "deepseek-v4-flash"
    assert run.usage == USAGE
    phase, error_type, detail = _parse_run_error(run.error)
    assert error_type == AdapterErrorCategory.PROVIDER_STRUCTURE.value
    assert phase == "parse_shape"


def test_unexpected_chat_error_is_terminalized_not_submitted(
    monkeypatch, patched, engine
) -> None:
    a = _adapter()
    monkeypatch.setattr(a, "_build_prompt", lambda *a_, **k: "")

    def _chat_boom(*a_, **k):
        raise RuntimeError("kaboom mid-call")

    monkeypatch.setattr(a, "_chat", _chat_boom)

    with pytest.raises(ExecutionError):
        a.run(
            task_id="t1",
            task_context=_ctx(),
            output_schema={"type": "object"},
            idempotency_key="k4",
        )

    runs = _runs(engine)
    run = _defining_run(runs)
    assert run.status == DelegatedRunStatus.FAILED
    assert run.model == "deepseek-v4-flash"
    phase, error_type, detail = _parse_run_error(run.error)
    assert error_type == AdapterErrorCategory.UNKNOWN.value
    assert phase == "chat"


def test_happy_path_succeeds_with_attribution(
    monkeypatch, patched, engine
) -> None:
    a = _adapter()
    monkeypatch.setattr(a, "_build_prompt", lambda *a_, **k: "")
    monkeypatch.setattr(
        a, "_chat", lambda *a_, **k: ('{"summary": "ok", "body": "x"}', USAGE)
    )
    monkeypatch.setattr(
        a, "_parse_json", staticmethod(lambda t: {"summary": "ok", "body": "x"})
    )

    result = a.run(
        task_id="t1",
        task_context=_ctx(),
        output_schema={"type": "object"},
        idempotency_key="k5",
    )
    assert result.summary == "ok"

    runs = _runs(engine)
    assert len(runs) == 1
    run = runs[0]
    assert run.status == DelegatedRunStatus.SUCCEEDED
    assert run.model == "deepseek-v4-flash"
    assert run.usage == USAGE


def test_run_error_roundtrip_formatting() -> None:
    # The packed error string must be losslessly recoverable.
    from aios.execution import _format_run_error

    packed = _format_run_error(
        "模型返回无法解析为 JSON",
        phase="parse",
        error_type=AdapterErrorCategory.JSON_PARSE.value,
    )
    phase, error_type, detail = _parse_run_error(packed)
    assert phase == "parse"
    assert error_type == AdapterErrorCategory.JSON_PARSE.value
    assert detail == "模型返回无法解析为 JSON"

    # No tags -> unchanged (back-compat with pre-P0 error text).
    assert _format_run_error("plain error") == "plain error"
    assert _parse_run_error("plain error") == (None, None, "plain error")
