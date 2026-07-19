"""Delegation result provenance persistence (#57, #62).

Guarantees contract point 10: every delegated result lands as an *unverified*
Artifact carrying ``adapter_id`` / ``source`` / a complete, secret-free
``provenance_json`` bundle. Exercises the real ``execute_task`` path with a
``DelegatedExecutionAdapter`` subclass so the provenance is captured exactly as
production would record it.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from aios.db import get_database_url, get_engine, run_migrations
from aios.delegation import DelegatedExecutionAdapter
from aios.execution import execute_task
from aios.models import (
    AdapterType,
    Agent,
    AgentTrustLevel,
    Artifact,
    ArtifactReviewStatus,
    DelegationMode,
    Project,
    RoutingMode,
    Task,
    TaskStatus,
)


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'provenance.db'}")
    run_migrations(get_database_url())
    eng = get_engine(get_database_url())
    s = Session(eng)
    yield s
    s.close()


OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


class FakeDelegatedAdapter(DelegatedExecutionAdapter):
    """Walks the real delegated run() lifecycle; only the remote transport is faked."""

    mode = DelegationMode.WORKSTATION

    def submit(self, *, delegated_run, projected_context, output_schema, remote_callback_url):
        return {"remote_run_id": f"fake:{delegated_run.id}", "remote_status": "running"}

    def status(self, *, delegated_run):
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
                    "data": {"summary": "ok"},
                }
            ],
        }


def _seed(session: Session) -> tuple[Agent, Task]:
    project = Project(name="p", objective="o", budget_limit=0.0)
    session.add(project)
    session.commit()
    session.refresh(project)

    agent = Agent(
        name="ExtWorker",
        role="外部工人",
        adapter_type=AdapterType.EXTERNAL,
        delegation_mode=DelegationMode.WORKSTATION,
        capabilities=["x"],
        trust_level=AgentTrustLevel.VERIFIED_EXTERNAL,
        secret_ref="secret://ext-worker",
        enabled=True,
    )
    session.add(agent)
    session.commit()
    session.refresh(agent)

    task = Task(
        project_id=project.id,
        title="t",
        description="d",
        status=TaskStatus.READY,
        routing_mode=RoutingMode.FIXED,
        assigned_agent_id=agent.id,
        output_schema=OUTPUT_SCHEMA,
        estimated_cost=0.0,
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return agent, task


def test_delegated_result_artifact_carries_full_provenance(session: Session) -> None:
    agent, task = _seed(session)
    adapter = FakeDelegatedAdapter(agent=agent)

    artifact = execute_task(
        session, task.id, "idem-prov", adapter=adapter, actor="agent"
    )

    # Every delegated result enters as an UNVERIFIED artifact (contract rule).
    assert artifact.review_status == ArtifactReviewStatus.UNVERIFIED

    # adapter_id (FK) + source mode are recorded.
    assert artifact.adapter_id == agent.id
    assert artifact.source == "delegated:workstation"

    # Provenance bundle is complete and points back to the producing agent + run.
    prov = artifact.provenance_json
    assert prov["agent_id"] == agent.id
    assert prov["agent_name"] == "ExtWorker"
    assert prov["mode"] == "workstation"
    assert prov["delegated_run_id"]
    assert prov["remote_run_id"] == f"fake:{prov['delegated_run_id']}"
    assert prov["remote_status"] == "succeeded"
    assert prov["attempt"] == 1
    # Timeline captured.
    assert prov["submitted_at"]
    assert prov["finished_at"]
    # Opaque handle only — the secret value is NEVER persisted here.
    assert prov["secret_ref"] == "secret://ext-worker"

    # Exactly one artifact, attributable to this delegated run.
    session.expire_all()
    persisted = session.exec(select(Artifact).where(Artifact.task_id == task.id)).all()
    assert len(persisted) == 1
    assert persisted[0].adapter_id == agent.id
