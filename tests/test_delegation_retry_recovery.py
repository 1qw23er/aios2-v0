"""Delegation retry/recovery — a FAILED task must be re-runnable (GAP-A).

``execution.execute_task`` explicitly supports recovery: a task in FAILED is
reset to READY so it can be retried with a *new* idempotency key
(``execution.py``: "a FAILED task can be retried with a (new) idempotency
key"). For **delegated** (external) execution that promise did not hold:

``DelegatedRun.idempotency_key`` was derived as ``H(task_id, agent_id,
attempt)`` only, and ``DelegatedExecutionAdapter.run`` reset ``attempt = 1`` on
every entry. So the second execution of the same task by the same agent recomputed
*exactly* the same key as the first execution's first attempt and hit the UNIQUE
constraint on ``delegated_run.idempotency_key``:

    UNIQUE constraint failed: delegated_run.idempotency_key

The operator-level symptom: after fixing a broken external executor, the task
could not be retried in place — only re-dispatching to a *different* task/agent
worked.

The real fix (this revision) makes ``attempt`` a **globally-monotonic per-task
counter**, derived at run-creation time from the persisted ``DelegatedRun`` rows
(see ``delegation._next_attempt``), so a re-execution continues 1, 2, 3, ... and
the UNIQUE ``idempotency_key`` is never hit:

    attempt = MAX(existing run.attempt for task) + 1   # re-derived, never in-memory

The caller's ``idempotency_key`` (``execution_key``) stays in the hash as
defense-in-depth. No migration: the column already exists and stays unique.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, select

import aios.delegation as _delegation
from aios.adapters.external import WorkstationAdapter
from aios.db import get_database_url, get_engine, run_migrations
from aios.delegation import DelegatedExecutionError, make_idempotency_key
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

PAYLOAD = {"summary": "s", "deliverable": "d"}


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
        id="agt-retry",
        name="Retry probe",
        role="copy",
        adapter_type=AdapterType.EXTERNAL,
        delegation_mode=DelegationMode.WORKSTATION,
        platform="smartrouter",
    )


def _result_file(inbox: Path, task_id: str) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{task_id}.result.json").write_text(
        json.dumps(
            {
                "result_id": f"res:{task_id}",
                "task_id": task_id,
                "summary": "ok",
                "artifacts": [
                    {
                        "type": "json",
                        "uri": "x",
                        "summary": "ok",
                        "data": PAYLOAD,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# key derivation — no DB required
# --------------------------------------------------------------------------- #
def test_key_is_deterministic_within_one_execution() -> None:
    k1 = make_idempotency_key("t1", "a1", 1, "exec-A")
    k2 = make_idempotency_key("t1", "a1", 1, "exec-A")
    k3 = make_idempotency_key("t1", "a1", 2, "exec-A")
    assert k1 == k2, "same execution + same attempt must be idempotent"
    assert k1 != k3, "different attempts within one execution must differ"


def test_key_differs_across_executions() -> None:
    """The whole point of the fix: a re-run must not collide with the old run."""
    first = make_idempotency_key("t1", "a1", 1, "exec-A")
    retry = make_idempotency_key("t1", "a1", 1, "exec-B")
    assert first != retry, (
        "a retried execution must derive a fresh key, otherwise the UNIQUE "
        "constraint on delegated_run.idempotency_key blocks recovery"
    )


def test_key_without_execution_stays_backward_compatible() -> None:
    """Omitting the execution key keeps the historical H(task, agent, attempt)."""
    assert make_idempotency_key("t1", "a1", 1) == make_idempotency_key("t1", "a1", 1)
    assert make_idempotency_key("t1", "a1", 1) != make_idempotency_key("t1", "a1", 2)


# --------------------------------------------------------------------------- #
# behaviour — the delegated run must be creatable twice for the same task
# --------------------------------------------------------------------------- #
@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    url = f"sqlite:///{tmp_path / 'retry_rec.db'}"
    monkeypatch.setenv("AIOS_DATABASE_URL", url)
    run_migrations(get_database_url())
    s = Session(get_engine(get_database_url()))
    yield s
    s.close()


def _seed(session: Session) -> Task:
    p = Project(name="p", objective="o")
    session.add(p)
    session.commit()
    session.refresh(p)
    session.add(_agent())
    session.commit()
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
    return task


def _adapter(tmp_path: Path) -> WorkstationAdapter:
    ws = WorkstationAdapter(
        agent=_agent(), outbox=tmp_path / "out", inbox=tmp_path / "in"
    )
    ws.max_retries = 1  # one attempt per execution keeps the assertion crisp
    ws.timeout_s = 15.0
    return ws


def test_retry_after_failure_creates_a_new_run(session: Session, tmp_path: Path) -> None:
    """Operator flow: external executor broke -> fix it -> retry the same task."""
    task = _seed(session)
    ws = _adapter(tmp_path)
    outbox, inbox = tmp_path / "out", tmp_path / "in"

    # 1) first execution: the runner reports a terminal failure
    err_dir = outbox / task.id
    err_dir.mkdir(parents=True)
    (err_dir / ".error").write_text("LLM executor request failed: refused", encoding="utf-8")

    with pytest.raises(DelegatedExecutionError):
        ws.run(
            task_id=task.id,
            task_context=_FakeCtx(),
            output_schema=SCHEMA,
            idempotency_key="exec-first",
        )

    first_runs = session.exec(select(DelegatedRun).where(DelegatedRun.task_id == task.id)).all()
    assert len(first_runs) == 1
    assert first_runs[0].status == DelegatedRunStatus.FAILED

    # 2) operator fixes the executor and quarantines the failure marker
    (err_dir / ".error").rename(err_dir / ".error.acknowledged")
    _result_file(inbox, task.id)

    # 3) retry the SAME task with the SAME agent under a NEW execution key.
    #    Before the fix this raised IntegrityError (UNIQUE idempotency_key).
    result = ws.run(
        task_id=task.id,
        task_context=_FakeCtx(),
        output_schema=SCHEMA,
        idempotency_key="exec-retry",
    )

    runs = session.exec(
        select(DelegatedRun)
        .where(DelegatedRun.task_id == task.id)
        .order_by(DelegatedRun.created_at)  # type: ignore[arg-type]
    ).all()
    assert len(runs) == 2, "the retry must record a second, independent run"
    assert runs[0].status == DelegatedRunStatus.FAILED
    assert runs[1].status == DelegatedRunStatus.SUCCEEDED
    assert runs[0].idempotency_key != runs[1].idempotency_key
    assert result is not None


def test_attempt_increments_across_two_executions(
    session: Session, tmp_path: Path
) -> None:
    """Two executions of the same task continue the attempt sequence 1, 2 (GAP-A)."""
    task = _seed(session)
    ws = _adapter(tmp_path)
    inbox = tmp_path / "in"
    _result_file(inbox, task.id)

    for key in ("exec-1", "exec-2"):
        ws.run(
            task_id=task.id,
            task_context=_FakeCtx(),
            output_schema=SCHEMA,
            idempotency_key=key,
        )

    runs = session.exec(select(DelegatedRun).where(DelegatedRun.task_id == task.id)).all()
    assert len(runs) == 2
    assert {r.attempt for r in runs} == {1, 2}, "attempt is globally monotonic per task"
    assert len({r.idempotency_key for r in runs}) == 2


def test_attempt_increments_1_2_3_across_three_executions(
    session: Session, tmp_path: Path
) -> None:
    """Re-running a FAILED task records attempts 1, 2, 3; keys distinct; old runs kept.

    Covers GAP-A spec #1 (attempt=1 first), #2 (attempt=2 on retry), #3 (attempt=3
    on second retry), #4 (every attempt has a distinct idempotency_key), #5 (old
    FAILED runs are preserved, never overwritten or deleted).
    """
    task = _seed(session)
    ws = _adapter(tmp_path)
    outbox, inbox = tmp_path / "out", tmp_path / "in"

    # Execution 1: external executor broken -> FAILED run at attempt 1.
    (outbox / task.id).mkdir(parents=True, exist_ok=True)
    (outbox / task.id / ".error").write_text("executor refused", encoding="utf-8")
    with pytest.raises(DelegatedExecutionError):
        ws.run(
            task_id=task.id,
            task_context=_FakeCtx(),
            output_schema=SCHEMA,
            idempotency_key="exec-1",
        )
    (outbox / task.id / ".error").rename(outbox / task.id / ".error.ack1")

    # Execution 2: still broken -> FAILED run at attempt 2.
    (outbox / task.id / ".error").write_text("executor refused", encoding="utf-8")
    with pytest.raises(DelegatedExecutionError):
        ws.run(
            task_id=task.id,
            task_context=_FakeCtx(),
            output_schema=SCHEMA,
            idempotency_key="exec-2",
        )
    (outbox / task.id / ".error").rename(outbox / task.id / ".error.ack2")

    # Execution 3: executor fixed -> SUCCEEDED run at attempt 3.
    _result_file(inbox, task.id)
    ws.run(task_id=task.id, task_context=_FakeCtx(), output_schema=SCHEMA, idempotency_key="exec-3")

    runs = session.exec(
        select(DelegatedRun)
        .where(DelegatedRun.task_id == task.id)
        .order_by(DelegatedRun.created_at)  # type: ignore[arg-type]
    ).all()
    assert [r.attempt for r in runs] == [1, 2, 3], "attempts must be globally monotonic 1,2,3"
    assert len({r.idempotency_key for r in runs}) == 3, "each attempt has a distinct key"
    assert runs[0].status == DelegatedRunStatus.FAILED
    assert runs[1].status == DelegatedRunStatus.FAILED
    assert runs[2].status == DelegatedRunStatus.SUCCEEDED
    assert len(runs) == 3, "old FAILED runs are preserved, never overwritten"


def test_attempt_derived_from_persisted_max_not_in_memory(
    session: Session, tmp_path: Path
) -> None:
    """``_next_attempt`` reads MAX(attempt) from the DB, not an in-memory counter.

    GAP-A spec #9 (constraint-level proof): pre-existing runs with attempts 1, 2, 3
    must cause the next ``_create_run`` to allocate attempt 4 -- proving the value
    is derived from persisted state, which is what keeps concurrent allocations
    from silently producing a duplicate.
    """
    task = _seed(session)
    ws = _adapter(tmp_path)
    for a in (1, 2, 3):
        session.add(
            DelegatedRun(
                project_id=task.project_id,
                task_id=task.id,
                agent_id=ws.agent.id,
                delegation_mode=ws.mode,
                idempotency_key=make_idempotency_key(task.id, ws.agent.id, a, "prior"),
                attempt=a,
            )
        )
    session.commit()

    run = ws._create_run(task.id, "exec-new")
    # run is owned by _create_run's internal session; reload it into the test
    # session to prove it was actually persisted (detached refresh would fail).
    persisted = session.get(DelegatedRun, run.id)
    assert persisted is not None
    assert persisted.attempt == 4, "next attempt must be MAX(persisted)+1"
    assert persisted.idempotency_key != make_idempotency_key(task.id, ws.agent.id, 3, "prior")


def test_attempt_allocation_retries_on_integrity_conflict(
    session: Session, tmp_path: Path, monkeypatch
) -> None:
    """Concurrency safety (GAP-A spec #4 / #9): a UNIQUE collision on
    ``idempotency_key`` is resolved by re-deriving MAX(attempt)+1, and the
    ``IntegrityError`` is NEVER swallowed.

    We deterministically simulate the race a concurrent writer creates: the
    first attempt derivation returns the same attempt number a *concurrent*
    writer has already committed (so the insert hits the UNIQUE constraint).
    ``_create_run`` must catch ``IntegrityError``, re-read ``MAX(attempt)`` from
    the persisted rows, and insert ``attempt + 1`` successfully -- bounded by
    ``_ATTEMPT_ALLOC_RETRIES``, never looping forever and never hiding the error.
    """
    task = _seed(session)
    ws = _adapter(tmp_path)
    # A concurrent writer already claimed attempt 1 with this exact key.
    session.add(
        DelegatedRun(
            project_id=task.project_id,
            task_id=task.id,
            agent_id=ws.agent.id,
            delegation_mode=ws.mode,
            idempotency_key=make_idempotency_key(task.id, ws.agent.id, 1, "exec-X"),
            attempt=1,
        )
    )
    session.commit()

    real_next = _delegation._next_attempt
    calls = {"n": 0}

    def _force_collision_once_then_real(s: Session, tid: str) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return 1  # stale read a racing writer already used -> collision
        return real_next(s, tid)

    monkeypatch.setattr(_delegation, "_next_attempt", _force_collision_once_then_real)
    run = ws._create_run(task.id, "exec-X")
    assert run.attempt == 2, "collision resolved by re-deriving MAX(attempt)+1"
    assert calls["n"] >= 2, "_create_run must have retried after the IntegrityError"
    fresh = session.get(DelegatedRun, run.id)
    assert fresh is not None and fresh.attempt == 2
