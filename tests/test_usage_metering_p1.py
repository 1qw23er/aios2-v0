"""Usage Metering P1 -- behavioral contract tests.

The contract under test:

    metering is a PURE READ projection over the DelegatedRun SSoT

Specifically these tests prove that:

* project / agent / task projections aggregate run counts and currency cost
  only (no token SUM -- usage schemas are heterogeneous in P1);
* ``cost > 0`` is *measured*, ``cost == 0`` (default/unreported) is *no
  measured cost*, and the word "free" never appears in any response;
* only ACCRUABLE terminal runs (SUCCEEDED / FAILED / EXPIRED) with
  ``cost > 0`` enter ``accrued_measured_spend``; CANCELLED-with-cost is
  reported separately and never folded into the accrued spend;
* budget reconciliation compares ``Project.budget_used`` (read-only) with the
  metering-side accrued spend and REPORTS a discrepancy without correcting it;
* the surface is owner-only; an unauthenticated caller gets 401 on every route;
* time filtering is a half-open ``[from, to)`` interval on
  ``DelegatedRun.created_at``;
* GET is idempotent and side-effect free: runs and ``Project.budget_used``
  are byte-for-byte unchanged after any number of GETs.

DB is provided by ``authenticated_client`` (conftest runs a real Alembic
``upgrade head``; this P1 adds no migration, head stays ``20260909_0004``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.db import get_database_url, get_engine
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

OWNER_KEY = "k" * 40  # >= MIN_OWNER_API_KEY_LENGTH (32)

BASE = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


# --- helpers ---------------------------------------------------------------


@pytest.fixture
def db(authenticated_client) -> Session:
    return Session(get_engine(get_database_url()))


@pytest.fixture
def client(authenticated_client) -> TestClient:
    return authenticated_client


def _seed(session: Session, *, name: str = "p") -> tuple[Project, Agent, Task]:
    project = Project(name=name, objective="o", budget_limit=0.0)
    session.add(project)
    session.commit()
    session.refresh(project)

    agent = Agent(
        name="Fake",
        role="worker",
        adapter_type=AdapterType.EXTERNAL,
        delegation_mode=DelegationMode.WORKSTATION,
        capabilities=["x"],
        enabled=True,
        timeout_s=300.0,
        max_retries=1,
        trust_level=AgentTrustLevel.INTERNAL,
    )
    session.add(agent)
    session.commit()
    session.refresh(agent)

    task = Task(
        project_id=project.id,
        title="t",
        description="d",
        status=TaskStatus.BACKLOG,
        output_schema={"type": "object"},
        estimated_cost=0.0,
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return project, agent, task


def _run(
    session: Session,
    *,
    project_id: str,
    task_id: str,
    agent_id: str | None = None,
    status: DelegatedRunStatus = DelegatedRunStatus.SUBMITTED,
    attempt: int = 1,
    cost: float = 0.0,
    usage: dict | None = None,
    created_at: datetime | None = None,
) -> DelegatedRun:
    run = DelegatedRun(
        project_id=project_id,
        task_id=task_id,
        agent_id=agent_id,
        delegation_mode=DelegationMode.WORKSTATION,
        status=status,
        idempotency_key=f"idem-{uuid4().hex[:12]}",
        attempt=attempt,
        cost=cost,
        usage=usage,
        created_at=created_at or BASE,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _run_snapshot(run: DelegatedRun) -> dict:
    return {
        "id": run.id,
        "status": str(run.status),
        "cost": run.cost,
        "budget_accrued_at": run.budget_accrued_at,
        "callback_received_at": run.callback_received_at,
    }


# --- basic aggregation -----------------------------------------------------


def test_project_usage_basic_aggregation(db, client) -> None:
    project, agent, task = _seed(db)
    _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
         status=DelegatedRunStatus.SUCCEEDED, cost=1.5)
    _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
         status=DelegatedRunStatus.FAILED, cost=0.5)
    _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
         status=DelegatedRunStatus.RUNNING, cost=0.0)

    resp = client.get(f"/usage/projects/{project.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_count"] == 3
    assert body["measured_run_count"] == 2
    assert body["no_measured_cost_run_count"] == 1
    assert body["measured_spend"] == pytest.approx(2.0)
    assert body["accruable_run_count"] == 2
    assert body["accrued_measured_spend"] == pytest.approx(2.0)
    assert body["cancelled_with_cost_run_count"] == 0
    assert body["runs_by_status"]["succeeded"] == 1
    assert body["runs_by_status"]["failed"] == 1
    assert body["runs_by_status"]["running"] == 1


def test_agent_usage_spans_projects_and_project_boundary(db, client) -> None:
    project1, agent, task1 = _seed(db, name="p1")
    project2 = Project(name="p2", objective="o", budget_limit=0.0)
    db.add(project2)
    db.commit()
    db.refresh(project2)
    task2 = Task(
        project_id=project2.id, title="t2", description="d",
        status=TaskStatus.BACKLOG, output_schema={"type": "object"},
        estimated_cost=0.0,
    )
    db.add(task2)
    db.commit()
    db.refresh(task2)

    _run(db, project_id=project1.id, task_id=task1.id, agent_id=agent.id,
         status=DelegatedRunStatus.SUCCEEDED, cost=1.0)
    _run(db, project_id=project2.id, task_id=task2.id, agent_id=agent.id,
         status=DelegatedRunStatus.SUCCEEDED, cost=2.0)

    # Unbounded: the agent dimension aggregates across its projects.
    body = client.get(f"/usage/agents/{agent.id}").json()
    assert body["run_count"] == 2
    assert body["measured_spend"] == pytest.approx(3.0)

    # With the project boundary: only that project's runs.
    body = client.get(
        f"/usage/agents/{agent.id}", params={"project_id": project1.id}
    ).json()
    assert body["run_count"] == 1
    assert body["measured_spend"] == pytest.approx(1.0)


def test_task_usage_scoped_to_task(db, client) -> None:
    project, agent, task = _seed(db)
    other_task = Task(
        project_id=project.id, title="t-other", description="d",
        status=TaskStatus.BACKLOG, output_schema={"type": "object"},
        estimated_cost=0.0,
    )
    db.add(other_task)
    db.commit()
    db.refresh(other_task)

    _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
         status=DelegatedRunStatus.SUCCEEDED, cost=1.25)
    _run(db, project_id=project.id, task_id=other_task.id, agent_id=agent.id,
         status=DelegatedRunStatus.SUCCEEDED, cost=9.0)

    body = client.get(f"/usage/tasks/{task.id}").json()
    assert body["run_count"] == 1
    assert body["measured_spend"] == pytest.approx(1.25)
    # The other task's runs never leak in.
    body_other = client.get(f"/usage/tasks/{other_task.id}").json()
    assert body_other["run_count"] == 1
    assert body_other["measured_spend"] == pytest.approx(9.0)


# --- cost semantics --------------------------------------------------------


def test_cost_semantics_measured_vs_no_measured(db, client) -> None:
    project, agent, task = _seed(db)
    _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
         status=DelegatedRunStatus.SUCCEEDED, cost=0.75, usage={"total_tokens": 10})
    # cost == 0 with NO usage at all (provider reported nothing).
    _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
         status=DelegatedRunStatus.SUCCEEDED, cost=0.0, usage=None)
    # cost == 0 even though usage was reported (provider said zero).
    _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
         status=DelegatedRunStatus.SUCCEEDED, cost=0.0, usage={"total_tokens": 3})

    body = client.get(f"/usage/projects/{project.id}").json()
    assert body["run_count"] == 3
    assert body["measured_run_count"] == 1
    assert body["no_measured_cost_run_count"] == 2
    assert body["measured_spend"] == pytest.approx(0.75)
    # "cost == 0" must never be dressed up as "free".
    assert "free" not in repr(body).lower()
    # No token aggregation is exposed anywhere in the projection.
    assert "total_tokens" not in repr(body)


def test_no_measured_cost_bucket_when_no_runs(db, client) -> None:
    project, _agent, _task = _seed(db)
    body = client.get(f"/usage/projects/{project.id}").json()
    assert body["run_count"] == 0
    assert body["measured_run_count"] == 0
    assert body["no_measured_cost_run_count"] == 0
    assert body["measured_spend"] == 0.0
    assert body["accrued_measured_spend"] == 0.0


# --- status / accrual semantics ---------------------------------------------


def test_accrual_status_semantics(db, client) -> None:
    project, agent, task = _seed(db)
    _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
         status=DelegatedRunStatus.SUCCEEDED, cost=1.0)
    _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
         status=DelegatedRunStatus.FAILED, cost=2.0)
    _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
         status=DelegatedRunStatus.EXPIRED, cost=4.0)
    # Cancelled AFTER the provider already recorded cost: evidence, not budget.
    _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
         status=DelegatedRunStatus.CANCELLED, cost=8.0)
    # Cancelled without cost: invisible to every cost bucket.
    _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
         status=DelegatedRunStatus.CANCELLED, cost=0.0)

    body = client.get(f"/usage/projects/{project.id}").json()
    assert body["run_count"] == 5
    # measured = every cost > 0 run, regardless of status.
    assert body["measured_run_count"] == 4
    assert body["measured_spend"] == pytest.approx(15.0)
    # accrued = ACCRUABLE (succeeded/failed/expired) AND cost > 0.
    assert body["accruable_run_count"] == 3
    assert body["accrued_measured_spend"] == pytest.approx(7.0)
    # cancelled-with-cost is reported separately, never accrued.
    assert body["cancelled_with_cost_run_count"] == 1
    assert body["cancelled_with_cost_spend"] == pytest.approx(8.0)
    # The reconciliation identity: measured = accrued + cancelled-with-cost.
    assert body["measured_spend"] == pytest.approx(
        body["accrued_measured_spend"] + body["cancelled_with_cost_spend"]
    )


# --- budget reconciliation ---------------------------------------------------


def test_budget_reconciliation_reports_without_correcting(db, client) -> None:
    project, agent, task = _seed(db)
    _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
         status=DelegatedRunStatus.SUCCEEDED, cost=1.5)
    _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
         status=DelegatedRunStatus.FAILED, cost=0.5)
    _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
         status=DelegatedRunStatus.CANCELLED, cost=2.0)

    # Align the budget SSoT with the metering projection (test-side write;
    # production writes live exclusively in delegation.accrue_run_budget).
    project.budget_used = 2.0
    db.add(project)
    db.commit()

    resp = client.get(f"/usage/projects/{project.id}/budget-reconciliation")
    assert resp.status_code == 200
    body = resp.json()
    assert body["budget_used"] == pytest.approx(2.0)
    assert body["accrued_measured_spend"] == pytest.approx(2.0)
    assert body["matches"] is True
    assert body["discrepancy"] == pytest.approx(0.0)
    assert body["projection"]["cancelled_with_cost_spend"] == pytest.approx(2.0)

    # Now drift the budget: the endpoint must REPORT, never correct.
    project.budget_used = 2.25
    db.add(project)
    db.commit()

    body = client.get(f"/usage/projects/{project.id}/budget-reconciliation").json()
    assert body["budget_used"] == pytest.approx(2.25)
    assert body["accrued_measured_spend"] == pytest.approx(2.0)
    assert body["matches"] is False
    assert body["discrepancy"] == pytest.approx(0.25)

    # Re-GET: still 2.25 -- no auto-correction happened.
    assert client.get(
        f"/usage/projects/{project.id}/budget-reconciliation"
    ).json()["budget_used"] == pytest.approx(2.25)
    with Session(get_engine(get_database_url())) as s:
        assert s.get(Project, project.id).budget_used == pytest.approx(2.25)


# --- time range --------------------------------------------------------------


def test_time_range_is_half_open(db, client) -> None:
    project, agent, task = _seed(db)
    _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
         status=DelegatedRunStatus.SUCCEEDED, cost=1.0, created_at=BASE)
    _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
         status=DelegatedRunStatus.SUCCEEDED, cost=2.0, created_at=BASE + timedelta(hours=1))
    _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
         status=DelegatedRunStatus.SUCCEEDED, cost=4.0, created_at=BASE + timedelta(hours=2))

    params = {
        "from": (BASE + timedelta(hours=1)).isoformat(),
        "to": (BASE + timedelta(hours=2)).isoformat(),
    }
    body = client.get(f"/usage/projects/{project.id}", params=params).json()
    # [from, to): the run AT `from` is included, the run AT `to` is NOT.
    assert body["run_count"] == 1
    assert body["measured_spend"] == pytest.approx(2.0)

    # from == a run's exact timestamp: inclusive.
    body = client.get(
        f"/usage/projects/{project.id}", params={"from": BASE.isoformat()}
    ).json()
    assert body["run_count"] == 3

    # to == a run's exact timestamp: exclusive.
    body = client.get(
        f"/usage/projects/{project.id}", params={"to": (BASE + timedelta(hours=2)).isoformat()}
    ).json()
    assert body["run_count"] == 2

    # Inverted window is a client error, not an empty 200.
    resp = client.get(
        f"/usage/projects/{project.id}",
        params={"from": (BASE + timedelta(hours=2)).isoformat(), "to": BASE.isoformat()},
    )
    assert resp.status_code == 422


# --- scope isolation & auth ---------------------------------------------------


def test_usage_surface_requires_owner_auth(tmp_path, monkeypatch) -> None:
    """Every usage route is owner-only (no anonymous cost introspection)."""
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'usage_auth.db'}")
    # Configure owner auth so the real dependency answers 401 (bad credentials)
    # rather than 503 (owner auth not configured at all).
    monkeypatch.setenv("AIOS_OWNER_ID", "owner")
    monkeypatch.setenv("AIOS_OWNER_API_KEY", "k" * 40)
    from aios.api.app import create_app

    app = create_app()  # real authenticate_owner -- no override
    with TestClient(app, follow_redirects=False) as unauth:
        assert unauth.get("/usage/projects/p_x").status_code == 401
        assert unauth.get("/usage/agents/a_x").status_code == 401
        assert unauth.get("/usage/tasks/t_x").status_code == 401
        assert unauth.get("/usage/projects/p_x/budget-reconciliation").status_code == 401


def test_usage_scope_isolation_between_projects(db, client) -> None:
    """A project projection never contains another project's runs."""
    project1, agent, task1 = _seed(db, name="iso-p1")
    project2 = Project(name="iso-p2", objective="o", budget_limit=0.0)
    db.add(project2)
    db.commit()
    db.refresh(project2)
    task2 = Task(
        project_id=project2.id, title="t2", description="d",
        status=TaskStatus.BACKLOG, output_schema={"type": "object"},
        estimated_cost=0.0,
    )
    db.add(task2)
    db.commit()
    db.refresh(task2)

    _run(db, project_id=project1.id, task_id=task1.id, agent_id=agent.id,
         status=DelegatedRunStatus.SUCCEEDED, cost=1.0)
    _run(db, project_id=project2.id, task_id=task2.id, agent_id=agent.id,
         status=DelegatedRunStatus.SUCCEEDED, cost=99.0)

    body1 = client.get(f"/usage/projects/{project1.id}").json()
    assert body1["run_count"] == 1
    assert body1["measured_spend"] == pytest.approx(1.0)
    assert body1["runs_by_status"]["succeeded"] == 1

    # The agent's unbounded view spans both, but with the project boundary it
    # is confined to exactly one project's runs.
    scoped = client.get(
        f"/usage/agents/{agent.id}", params={"project_id": project2.id}
    ).json()
    assert scoped["run_count"] == 1
    assert scoped["measured_spend"] == pytest.approx(99.0)


def test_unknown_entities_are_404(db, client) -> None:
    assert client.get("/usage/projects/p_nope").status_code == 404
    assert client.get("/usage/projects/p_nope/budget-reconciliation").status_code == 404
    assert client.get("/usage/agents/a_nope").status_code == 404
    assert client.get("/usage/tasks/t_nope").status_code == 404
    # A scoping project_id that does not exist is also a 404, not silent pass.
    project, agent, _task = _seed(db)
    resp = client.get(f"/usage/agents/{agent.id}", params={"project_id": "p_nope"})
    assert resp.status_code == 404


# --- read-only / idempotency ---------------------------------------------------


def test_get_is_idempotent_and_side_effect_free(db, client) -> None:
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id,
               status=DelegatedRunStatus.SUCCEEDED, cost=3.0, usage={"total_tokens": 5})

    before_runs = [
        _run_snapshot(r) for r in db.exec(select(DelegatedRun)).all()
    ]
    before_budget = db.get(Project, project.id).budget_used

    first = client.get(f"/usage/projects/{project.id}").json()
    second = client.get(f"/usage/projects/{project.id}").json()
    assert first == second
    client.get(f"/usage/agents/{agent.id}")
    client.get(f"/usage/tasks/{task.id}")
    client.get(f"/usage/projects/{project.id}/budget-reconciliation")

    db.expire_all()
    after_runs = [
        _run_snapshot(r) for r in db.exec(select(DelegatedRun)).all()
    ]
    assert after_runs == before_runs
    assert db.get(Project, project.id).budget_used == pytest.approx(before_budget)
    assert run.budget_accrued_at is None
