"""Agent Interoperability Gateway (#57) — delegated adapter self-tests.

Covers the two first-slice capability adapters end-to-end without any real
external system:

* A-slice (Hermes, remote_api): ``RemoteApiAdapter`` submits a projected
  context over HTTP to a ``_FakeHermesServer`` and ingests a structured
  Artifact that passes the task's ``output_schema``.
* B-slice (Coze / 扣子, workstation): ``WorkstationAdapter`` exports a task
  packet, then ingests an operator-dropped result file and validates each
  artifact's ``data`` against the cached ``output_schema``.

Both slices must produce the *same* Artifact contract (type/json, data dict),
proving the gateway normalizes heterogeneous external agents into one shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, select

from aios.adapters.external import WorkstationAdapter
from aios.adapters.hermes_remote import (
    RemoteApiAdapter,
    _FakeHermesServer,
    make_fake_hermes_agent,
)
from aios.db import get_database_url, get_engine
from aios.delegation import make_idempotency_key
from aios.models import (
    AdapterType,
    Agent,
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


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    # Scope AIOS_DATABASE_URL to this fixture only. Restoring on teardown
    # prevents the env var from leaking into sibling test files -- otherwise
    # alembic/env.py would honor the leaked URL and silently migrate the wrong
    # database (see test_db_init_optimization::test_real_run_migrations_still_works).
    url = f"sqlite:///{tmp_path / 'interop.db'}"
    monkeypatch.setenv("AIOS_DATABASE_URL", url)
    # Bootstrap the schema (conftest's copy shim diverts this to the migrated
    # template so migrations genuinely run, once, per session).
    from aios.db import run_migrations

    run_migrations(get_database_url())
    eng = get_engine(get_database_url())
    s = Session(eng)
    yield s
    s.close()


def _seed(session: Session):
    p = Project(name="t", objective="o")
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


def test_a_slice_hermes_remote_api(session: Session) -> None:
    p, task = _seed(session)
    srv = _FakeHermesServer()
    ep = srv.start()
    hermes = make_fake_hermes_agent(ep)
    session.add(hermes)
    session.commit()
    session.refresh(hermes)

    adapter = RemoteApiAdapter(agent=hermes, resolve_secret=lambda ref: "fake-key")
    res = adapter.run(
        task_id=task.id,
        task_context=_FakeCtx(),
        output_schema=SCHEMA,
        idempotency_key="idek1",
    )
    # The artifact must be schema-valid and come from the *external* agent.
    assert res.artifacts[0]["data"]["deliverable"] == "structured output from external agent"
    # The run is recorded in the DelegatedRun table.
    from aios.models import DelegatedRun

    runs = session.exec(select(DelegatedRun).where(DelegatedRun.task_id == task.id)).all()
    assert len(runs) == 1
    assert runs[0].status.value == "succeeded"
    assert runs[0].secret_ref == hermes.secret_ref  # opaque handle only, never the key
    srv.stop()


def test_b_slice_coze_workstation(session: Session) -> None:
    p, task = _seed(session)
    coze = Agent(
        name="Coze",
        role="copy",
        adapter_type=AdapterType.EXTERNAL,
        delegation_mode=DelegationMode.WORKSTATION,
        secret_ref="secret://coze",
        capabilities=["copy"],
    )
    session.add(coze)
    session.commit()
    session.refresh(coze)

    ws_dir = Path(__import__("tempfile").mkdtemp())
    outbox = ws_dir / "out"
    inbox = ws_dir / "in"
    ws = WorkstationAdapter(agent=coze, outbox=outbox, inbox=inbox)

    run_obj = type("R", (), {"id": "r1", "task_id": task.id, "project_id": p.id, "agent_id": coze.id})()  # noqa: E501

    ws.submit(
        delegated_run=run_obj,
        projected_context={
            "instructions": "write copy",
            "acceptance_criteria": [],
            "dependency_outputs": [],
            "objective": "obj",
        },
        output_schema=SCHEMA,
        remote_callback_url=None,
    )
    # Simulate the operator running the task in 扣子 and dropping the result.
    (inbox / f"{task.id}.result.json").write_text(
        json.dumps(
            {
                "result_id": "rr",
                "task_id": task.id,
                "summary": "coze copy",
                "artifacts": [
                    {
                        "type": "json",
                        "uri": "coze://x",
                        "summary": "coze copy",
                        "data": {"summary": "coze copy", "deliverable": "external copy deliverable"},  # noqa: E501
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert ws.status(delegated_run=run_obj)["finished"] is True
    ing = ws.ingest_result(delegated_run=run_obj)
    assert ing["artifacts"][0]["data"]["deliverable"] == "external copy deliverable"


def test_b_slice_rejects_schema_invalid_result(session: Session) -> None:
    """A delivered artifact whose data breaks the task schema must be refused."""
    p, task = _seed(session)
    coze = Agent(
        name="Coze",
        role="copy",
        adapter_type=AdapterType.EXTERNAL,
        delegation_mode=DelegationMode.WORKSTATION,
        secret_ref="secret://coze",
        capabilities=["copy"],
    )
    session.add(coze)
    session.commit()
    session.refresh(coze)

    ws_dir = Path(__import__("tempfile").mkdtemp())
    ws = WorkstationAdapter(agent=coze, outbox=ws_dir / "out", inbox=ws_dir / "in")
    run_obj = type("R", (), {"id": "r1", "task_id": task.id, "project_id": p.id, "agent_id": coze.id})()  # noqa: E501

    ws.submit(
        delegated_run=run_obj,
        projected_context={
            "instructions": "x",
            "acceptance_criteria": [],
            "dependency_outputs": [],
            "objective": "o",
        },
        output_schema=SCHEMA,
        remote_callback_url=None,
    )
    (ws_dir / "in" / f"{task.id}.result.json").write_text(
        json.dumps(
            {
                "result_id": "rr",
                "task_id": task.id,
                "summary": "bad",
                # missing the required "deliverable" key inside data
                "artifacts": [
                    {"type": "json", "uri": "x", "summary": "bad", "data": {"summary": "bad"}}
                ],
            }
        ),
        encoding="utf-8",
    )
    from aios.adapters.external import ResultValidationError

    with pytest.raises(ResultValidationError):
        ws.ingest_result(delegated_run=run_obj)


def test_idempotency_key_is_deterministic() -> None:
    k1 = make_idempotency_key("t1", "a1", 1)
    k2 = make_idempotency_key("t1", "a1", 1)
    k3 = make_idempotency_key("t1", "a1", 2)
    assert k1 == k2
    assert k1 != k3
