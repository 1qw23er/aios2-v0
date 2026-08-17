from __future__ import annotations

import pytest
from sqlmodel import Session, select

from aios.adapters.worker_delegated import WorkerDelegatedAdapter
from aios.db import get_database_url, get_engine, run_migrations
from aios.delegation import DelegatedExecutionAdapter, DelegatedExecutionError
from aios.execution import execute_task
from aios.models import (
    AdapterType,
    Agent,
    Artifact,
    DelegatedRun,
    DelegationMode,
    Project,
    RoutingMode,
    Task,
    TaskStatus,
)
from aios.worker_contract import (
    WorkerCapabilities,
    WorkerResult,
    WorkerState,
    WorkerStatus,
    WorkerSubmission,
)


class FakeWorker:
    def __init__(self, *, missing_usage: bool = False) -> None:
        self.submissions = []
        self.missing_usage = missing_usage

    def discover(self):
        caps = WorkerCapabilities.deepseek_harness_v1(worker_id="dsh", runtime_version="0.0.1")
        if self.missing_usage:
            return WorkerCapabilities(**{**caps.__dict__, "usage": False})
        return caps

    def submit(self, envelope):
        self.submissions.append(envelope)
        return WorkerSubmission(envelope.execution_id, "session-1", "message-1")

    def status(self, execution_id):
        return WorkerStatus(WorkerState.COMPLETED)

    def events(self, execution_id, after_cursor=None):
        return []

    def result(self, execution_id):
        envelope = self.submissions[0]
        return WorkerResult(
            execution_id=execution_id,
            status=WorkerState.COMPLETED,
            context_hash=envelope.context_hash,
            structured_output={"answer": 42},
            usage={"input_tokens": 3, "output_tokens": 2},
        )


class CrashedWorker(FakeWorker):
    def status(self, execution_id):
        raise RuntimeError("worker process exited")


def agent() -> Agent:
    return Agent(
        id="agt-1",
        name="Harness",
        role="worker",
        adapter_type=AdapterType.API,
        delegation_mode=DelegationMode.REMOTE_API,
        permissions=["read_only"],
    )


def run() -> DelegatedRun:
    return DelegatedRun(
        id="run-1",
        project_id="prj-1",
        task_id="tsk-1",
        agent_id="agt-1",
        delegation_mode=DelegationMode.REMOTE_API,
        idempotency_key="durable-key",
    )


def test_bridge_composes_existing_delegated_lifecycle() -> None:
    bridge = WorkerDelegatedAdapter(agent=agent(), client=FakeWorker())

    lifecycle = bridge.as_execution_adapter()

    assert not isinstance(bridge, DelegatedExecutionAdapter)
    assert isinstance(lifecycle, DelegatedExecutionAdapter)


def test_identity_mapping_and_one_remote_submission_per_attempt() -> None:
    worker = FakeWorker()
    bridge = WorkerDelegatedAdapter(agent=agent(), client=worker)
    bridge.set_context_hash("ctx-1")

    info = bridge.submit(
        delegated_run=run(),
        projected_context={"objective": "do it"},
        output_schema={"type": "object"},
        remote_callback_url=None,
    )

    assert info == {"remote_run_id": "session-1:message-1", "remote_status": "accepted"}
    assert worker.submissions[0].execution_id == "run-1"
    assert worker.submissions[0].idempotency_key == "durable-key"
    with pytest.raises(DelegatedExecutionError, match="already submitted"):
        bridge.submit(
            delegated_run=run(),
            projected_context={},
            output_schema={},
            remote_callback_url=None,
        )
    assert len(worker.submissions) == 1


def test_missing_required_capability_has_no_remote_side_effect() -> None:
    worker = FakeWorker(missing_usage=True)
    bridge = WorkerDelegatedAdapter(agent=agent(), client=worker)
    bridge.set_context_hash("ctx-1")

    with pytest.raises(DelegatedExecutionError, match="required capabilities"):
        bridge.submit(
            delegated_run=run(),
            projected_context={},
            output_schema={},
            remote_callback_url=None,
        )

    assert worker.submissions == []


def test_result_and_usage_flow_through_existing_hooks() -> None:
    worker = FakeWorker()
    bridge = WorkerDelegatedAdapter(agent=agent(), client=worker)
    bridge.set_context_hash("ctx-1")
    delegated_run = run()
    bridge.submit(
        delegated_run=delegated_run,
        projected_context={},
        output_schema={"type": "object"},
        remote_callback_url=None,
    )

    status = bridge.status(delegated_run=delegated_run)
    artifact = bridge.ingest_result(delegated_run=delegated_run)

    assert status["finished"] is True
    assert status["usage"] == {"input_tokens": 3, "output_tokens": 2}
    assert artifact["artifacts"][0]["data"] == {"answer": 42}


def test_runtime_crash_is_normalized_for_existing_retry_owner() -> None:
    bridge = WorkerDelegatedAdapter(agent=agent(), client=CrashedWorker())
    bridge.set_context_hash("ctx-1")
    delegated_run = run()
    bridge.submit(
        delegated_run=delegated_run,
        projected_context={},
        output_schema={},
        remote_callback_url=None,
    )

    with pytest.raises(DelegatedExecutionError, match="worker process exited"):
        bridge.status(delegated_run=delegated_run)


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'worker.db'}")
    run_migrations(get_database_url())
    value = Session(get_engine(get_database_url()))
    yield value
    value.close()


def test_replayed_harness_execution_creates_no_duplicate_artifact(session: Session) -> None:
    project = Project(name="p", objective="o")
    session.add(project)
    session.commit()
    worker_agent = agent()
    session.add(worker_agent)
    session.commit()
    task = Task(
        project_id=project.id,
        title="t",
        description="d",
        status=TaskStatus.READY,
        routing_mode=RoutingMode.FIXED,
        assigned_agent_id=worker_agent.id,
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "integer"}},
            "required": ["answer"],
        },
    )
    session.add(task)
    session.commit()
    worker = FakeWorker()
    lifecycle = WorkerDelegatedAdapter(agent=worker_agent, client=worker).as_execution_adapter()

    first = execute_task(session, task.id, "same", adapter=lifecycle)
    second = execute_task(session, task.id, "same", adapter=lifecycle)

    assert second.id == first.id
    assert len(worker.submissions) == 1
    assert len(session.exec(select(Artifact).where(Artifact.task_id == task.id)).all()) == 1
