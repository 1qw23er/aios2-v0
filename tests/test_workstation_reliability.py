"""External Workstation reliability — fail-fast on a runner-reported failure.

The workstation runner records a terminal execution failure by dropping an
``.error`` sentinel next to the task packet in the outbox (see
``scripts/workstation_runner.process_task``). Before this slice,
``WorkstationAdapter.status`` only looked for a delivered result, so a failed
external execution was indistinguishable from "still waiting": the delegation
wait loop kept polling until the full per-agent timeout and expired the run
instead of failing it fast.

This module pins the new ``status()`` contract:

* ``.error`` present            -> finished / failed (fail-fast)
* result present                -> finished / succeeded (unchanged)
* neither                       -> not finished / waiting_external (unchanged)
* ``.error`` + result           -> fail closed (the state is ambiguous; the
  error marker wins so a failure is never silently masked by a stale artifact)
* repeated reads                -> deterministic (idempotent)
* empty ``.error``              -> still failed, with a non-empty message
* secret-looking text in ``.error`` -> redacted before it is surfaced

The final test drives the full ``run()`` lifecycle and proves the wait loop
returns *immediately* on a failure marker instead of draining ``timeout_s``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, select

from aios.adapters.external import WorkstationAdapter
from aios.db import get_database_url, get_engine, run_migrations
from aios.delegation import DelegatedExecutionError
from aios.models import (
    AdapterType,
    Agent,
    DelegatedRun,
    DelegatedRunStatus,
    DelegationMode,
    Project,
    Task,
    TaskStatus,
)

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "deliverable": {"type": "string"}},
    "required": ["summary", "deliverable"],
}


class _FakeCtx:
    """Minimal projected-context stand-in (mirrors test_agent_interop)."""

    objective = "o"
    instructions = "do it"
    acceptance_criteria = ["x"]
    dependency_outputs: list[dict[str, Any]] = []
    context_hash = "h"

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        return {
            "objective": self.objective,
            "instructions": self.instructions,
            "acceptance_criteria": self.acceptance_criteria,
            "dependency_outputs": self.dependency_outputs,
        }


def _agent() -> Agent:
    return Agent(
        id="agt-ws-rel",
        name="WS reliability",
        role="copy",
        adapter_type=AdapterType.EXTERNAL,
        delegation_mode=DelegationMode.WORKSTATION,
        platform="smartrouter",
    )


def _run_stub(task_id: str):
    """A stand-in DelegatedRun exposing only what ``status()`` reads."""
    return type(
        "R",
        (),
        {"id": "r1", "task_id": task_id, "project_id": "p1", "agent_id": "agt-ws-rel"},
    )()


def _adapter(tmp_path: Path) -> WorkstationAdapter:
    return WorkstationAdapter(
        agent=_agent(), outbox=tmp_path / "out", inbox=tmp_path / "in"
    )


# --------------------------------------------------------------------------- #
# status() contract — no DB required
# --------------------------------------------------------------------------- #
def test_status_waiting_when_no_markers(tmp_path: Path) -> None:
    ws = _adapter(tmp_path)
    info = ws.status(delegated_run=_run_stub("tsk_none"))
    assert info["finished"] is False
    assert info["remote_status"] == "waiting_external"


def test_status_succeeded_when_result_present(tmp_path: Path) -> None:
    ws = _adapter(tmp_path)
    (tmp_path / "in" / "tsk_ok.result.json").write_text("{}", encoding="utf-8")
    info = ws.status(delegated_run=_run_stub("tsk_ok"))
    assert info["finished"] is True
    assert info["remote_status"] == "succeeded"


def test_status_fails_fast_on_error_sentinel(tmp_path: Path) -> None:
    ws = _adapter(tmp_path)
    err_dir = tmp_path / "out" / "tsk_bad"
    err_dir.mkdir(parents=True)
    (err_dir / ".error").write_text("connection refused", encoding="utf-8")

    info = ws.status(delegated_run=_run_stub("tsk_bad"))
    assert info["finished"] is True, "a failure marker must terminalize the wait loop"
    assert info["remote_status"] == "failed"
    assert info.get("error"), "the failure must carry a non-empty error message"


def test_status_error_sentinel_is_idempotent(tmp_path: Path) -> None:
    """Repeated polls over a persistent ``.error`` stay deterministic."""
    ws = _adapter(tmp_path)
    err_dir = tmp_path / "out" / "tsk_rep"
    err_dir.mkdir(parents=True)
    (err_dir / ".error").write_text("boom", encoding="utf-8")

    first = ws.status(delegated_run=_run_stub("tsk_rep"))
    second = ws.status(delegated_run=_run_stub("tsk_rep"))
    assert first == second
    assert first["finished"] is True and first["remote_status"] == "failed"


def test_status_error_wins_over_result_when_both_present(tmp_path: Path) -> None:
    """Coexisting result + error is ambiguous state -> fail closed."""
    ws = _adapter(tmp_path)
    err_dir = tmp_path / "out" / "tsk_both"
    err_dir.mkdir(parents=True)
    (err_dir / ".error").write_text("boom", encoding="utf-8")
    (tmp_path / "in" / "tsk_both.result.json").write_text("{}", encoding="utf-8")

    info = ws.status(delegated_run=_run_stub("tsk_both"))
    assert info["finished"] is True
    assert info["remote_status"] == "failed", "an error marker must not be masked by a result"


def test_status_empty_error_sentinel_still_fails(tmp_path: Path) -> None:
    ws = _adapter(tmp_path)
    err_dir = tmp_path / "out" / "tsk_empty"
    err_dir.mkdir(parents=True)
    (err_dir / ".error").write_text("", encoding="utf-8")

    info = ws.status(delegated_run=_run_stub("tsk_empty"))
    assert info["finished"] is True
    assert info["remote_status"] == "failed"
    assert info.get("error"), "an empty sentinel must still surface a message"


def test_status_redacts_secret_in_error_text(tmp_path: Path) -> None:
    ws = _adapter(tmp_path)
    err_dir = tmp_path / "out" / "tsk_leak"
    err_dir.mkdir(parents=True)
    fake_key = "sk-" + "a" * 32  # obviously non-real; must still be redacted
    (err_dir / ".error").write_text(
        f"LLM executor request failed: Bearer {fake_key}", encoding="utf-8"
    )

    info = ws.status(delegated_run=_run_stub("tsk_leak"))
    assert info["remote_status"] == "failed"
    surfaced = str(info.get("error", ""))
    assert fake_key not in surfaced
    assert surfaced  # still a message, just sanitized


# --------------------------------------------------------------------------- #
# Full lifecycle — the wait loop must not drain the timeout on a failure marker
# --------------------------------------------------------------------------- #
@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    url = f"sqlite:///{tmp_path / 'ws_rel.db'}"
    monkeypatch.setenv("AIOS_DATABASE_URL", url)
    run_migrations(get_database_url())
    s = Session(get_engine(get_database_url()))
    yield s
    s.close()


def _seed(session: Session) -> tuple[Project, Task]:
    p = Project(name="p", objective="o")
    session.add(p)
    session.commit()
    session.refresh(p)
    task = Task(
        project_id=p.id,
        title="T",
        description="d",
        status=TaskStatus.READY,
        output_schema=SCHEMA,
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return p, task


def test_run_fails_fast_on_error_sentinel_without_draining_timeout(
    session: Session, tmp_path: Path
) -> None:
    _, task = _seed(session)
    # The run row FKs to the agent, so it must exist before run() is driven.
    session.add(_agent())
    session.commit()

    outbox = tmp_path / "out"
    inbox = tmp_path / "in"
    # The runner already gave up before the wait loop's first poll.
    err_dir = outbox / task.id
    err_dir.mkdir(parents=True)
    (err_dir / ".error").write_text("LLM executor request failed: refused", encoding="utf-8")

    ws = WorkstationAdapter(agent=_agent(), outbox=outbox, inbox=inbox)
    ws.max_retries = 1  # single attempt: proves the FIRST poll terminalizes
    ws.timeout_s = 15.0  # draining this would take ~15s; fail-fast is instant

    started = time.monotonic()
    with pytest.raises(DelegatedExecutionError):
        ws.run(
            task_id=task.id,
            task_context=_FakeCtx(),
            output_schema=SCHEMA,
            idempotency_key="ws-rel-fastfail",
        )
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"wait loop drained the timeout instead of failing fast ({elapsed:.1f}s)"
    runs = session.exec(select(DelegatedRun).where(DelegatedRun.task_id == task.id)).all()
    assert len(runs) == 1
    assert runs[0].status == DelegatedRunStatus.FAILED
