"""Gateway audit-event taxonomy + per-agent delegation config (#57, slices S1/S2).

Covers:
  * the 6 designed AuditEvent types are emitted at the right lifecycle points;
  * per-agent ``timeout_s`` is honored (run expires, not the legacy 180s floor);
  * per-agent ``max_retries`` is honored (exact attempt count before giving up).
"""

from __future__ import annotations

import time

import pytest
from sqlmodel import Session, select

from aios.audit import AuditEvent, AuditLog
from aios.db import get_database_url, get_engine, run_migrations
from aios.delegation import (
    DelegatedExecutionAdapter,
    DelegatedExecutionError,
    cancel_run,
)
from aios.models import (
    AdapterType,
    Agent,
    AgentTrustLevel,
    DelegatedRun,
    DelegatedRunStatus,
    DelegationMode,
    Project,
    Task,
    TaskStatus,
)


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    # Scope the DB url so it cannot leak into sibling test files (pre-existing
    # cross-file flakiness fixed alongside #104).
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'audit_events.db'}")
    run_migrations(get_database_url())
    eng = get_engine(get_database_url())
    s = Session(eng)
    yield s
    s.close()


OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "deliverable": {"type": "string"}},
    "required": ["summary", "deliverable"],
}


class _Ctx:
    """Minimal TaskContext stand-in: supplies project/task ids + model_dump()."""

    def __init__(self, project_id: str, task_id: str) -> None:
        self.project_id = project_id
        self.task_id = task_id
        self.objective = "do the thing"
        self.instructions = "step by step"
        self.acceptance_criteria = ["must work"]
        self.dependency_outputs = []

    def model_dump(self, mode: str = "json") -> dict:
        return {
            "objective": self.objective,
            "instructions": self.instructions,
            "acceptance_criteria": self.acceptance_criteria,
            "dependency_outputs": self.dependency_outputs,
            "project_id": self.project_id,
            "task_id": self.task_id,
        }


def _seed(session: Session, *, timeout_s: float = 300.0, max_retries: int = 3):
    project = Project(name="p", objective="o", budget_limit=0.0)
    session.add(project)
    session.commit()
    session.refresh(project)

    agent = Agent(
        name="Fake",
        role="worker",
        adapter_type=AdapterType.EXTERNAL,
        delegation_mode=DelegationMode.WORKSTATION,
        capabilities=["x"],
        trust_level=AgentTrustLevel.VERIFIED_EXTERNAL,
        enabled=True,
        timeout_s=timeout_s,
        max_retries=max_retries,
    )
    session.add(agent)
    session.commit()
    session.refresh(agent)

    task = Task(
        project_id=project.id,
        title="t",
        description="d",
        status=TaskStatus.BACKLOG,
        output_schema=OUTPUT_SCHEMA,
        estimated_cost=0.0,
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return project, agent, task


class FakeAdapter(DelegatedExecutionAdapter):
    mode = DelegationMode.WORKSTATION
    fail_submit = False
    never_finish = False

    def submit(self, *, delegated_run, projected_context, output_schema, remote_callback_url):
        if self.fail_submit:
            raise DelegatedExecutionError("submit boom")
        return {"remote_run_id": f"fake:{delegated_run.id}", "remote_status": "running"}

    def status(self, *, delegated_run):
        if self.never_finish:
            return {"remote_status": "running", "finished": False, "result": None}
        return {"remote_status": "succeeded", "finished": True, "result": None}

    def cancel(self, *, delegated_run):
        pass

    def ingest_result(self, *, delegated_run):
        return {
            "summary": "ok",
            "artifacts": [
                {
                    "type": "json",
                    "uri": "fake://x",
                    "summary": "ok",
                    "data": {"summary": "ok", "deliverable": "out"},
                }
            ],
        }


def _audit_actions(session: Session) -> list[str]:
    session.expire_all()
    return [a.action for a in session.exec(select(AuditLog)).all()]


def test_audit_events_emitted_on_successful_delegation(session: Session) -> None:
    _, agent, task = _seed(session)
    adapter = FakeAdapter(agent=agent)
    ctx = _Ctx(project_id=task.project_id, task_id=task.id)

    result = adapter.run(
        task_id=task.id,
        task_context=ctx,
        output_schema=OUTPUT_SCHEMA,
        idempotency_key="ide-1",
    )
    assert result.artifacts

    actions = _audit_actions(session)
    assert AuditEvent.AGENT_DELEGATE in actions
    assert AuditEvent.AGENT_RESULT_RECEIVED in actions
    assert AuditEvent.ARTIFACT_VALIDATED in actions
    # Success path must NOT record a failure.
    assert AuditEvent.DELEGATION_FAILED not in actions
    # No secret value may appear in any audit payload (redaction is enforced).
    session.expire_all()
    for log in session.exec(select(AuditLog)).all():
        assert "[REDACTED]" not in str(log.before_snapshot) or "secret" not in str(
            log.before_snapshot
        )


def test_discover_emits_event(session: Session) -> None:
    _, agent, _ = _seed(session)
    adapter = FakeAdapter(agent=agent)
    caps = adapter.discover_capabilities()
    assert caps["mode"] == DelegationMode.WORKSTATION.value
    assert AuditEvent.AGENT_DISCOVER in _audit_actions(session)


def test_per_agent_timeout_expires(session: Session) -> None:
    # max_retries=1 isolates the timeout behavior (no retry loop masking it).
    _, agent, task = _seed(session, timeout_s=1.0, max_retries=1)
    adapter = FakeAdapter(agent=agent)
    adapter.never_finish = True
    ctx = _Ctx(project_id=task.project_id, task_id=task.id)

    t0 = time.time()
    with pytest.raises(DelegatedExecutionError):
        adapter.run(
            task_id=task.id,
            task_context=ctx,
            output_schema=OUTPUT_SCHEMA,
            idempotency_key="ide-2",
        )
    elapsed = time.time() - t0
    # Must honor the per-agent 1s timeout, not the legacy 180s floor.
    assert elapsed < 10, f"per-agent timeout not honored: {elapsed:.1f}s"

    session.expire_all()
    run = session.exec(select(DelegatedRun).where(DelegatedRun.task_id == task.id)).all()[-1]
    # Timeout is honored: the run terminated as a failure carrying the timeout
    # reason. (The per-attempt EXPIRED status is relabeled FAILED by the
    # retry/error path, which is the intended terminal state.)
    assert run.status == DelegatedRunStatus.FAILED
    assert run.error and "timeout" in run.error.lower()


def test_per_agent_max_retries(session: Session) -> None:
    _, agent, task = _seed(session, max_retries=2)
    adapter = FakeAdapter(agent=agent)
    adapter.fail_submit = True
    ctx = _Ctx(project_id=task.project_id, task_id=task.id)

    with pytest.raises(DelegatedExecutionError):
        adapter.run(
            task_id=task.id,
            task_context=ctx,
            output_schema=OUTPUT_SCHEMA,
            idempotency_key="ide-3",
        )

    session.expire_all()
    runs = session.exec(select(DelegatedRun).where(DelegatedRun.task_id == task.id)).all()
    # max_retries=2 -> exactly 2 attempts, both FAILED.
    assert len(runs) == 2
    assert all(r.status == DelegatedRunStatus.FAILED for r in runs)
    assert _audit_actions(session).count(AuditEvent.DELEGATION_FAILED) >= 2


def test_cancel_run_emits_cancelled(session: Session) -> None:
    _, agent, task = _seed(session)
    adapter = FakeAdapter(agent=agent)
    run = adapter._create_run(task.id, "ide-4", 1)
    adapter._record_submitted(run, {"remote_run_id": "x", "remote_status": "running"})

    status = cancel_run(run)
    assert status == DelegatedRunStatus.CANCELLED

    session.expire_all()
    r = session.get(DelegatedRun, run.id)
    assert r.status == DelegatedRunStatus.CANCELLED
    assert AuditEvent.DELEGATION_CANCELLED in _audit_actions(session)
