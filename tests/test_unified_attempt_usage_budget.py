"""Unified Attempt + Usage + Budget Accrual (C) -- behavioral contract tests.

These verify the C slice's core guarantees without a network:

* The local synchronous LLM path records a real ``DelegatedRun`` (mode ``LOCAL``,
  ``agent_id = None``) -- execution evidence is unified with remote delegation.
* Usage is normalized: ``None`` when the provider reports no token counts, never
  fabricated; real provider usage is stored verbatim.
* Budget accrual is unified and idempotent: a terminal, chargeable run is charged
  to ``Project.budget_used`` AT MOST ONCE, across retries, concurrent
  terminalizations, and recovery re-runs. SUCCEEDED / FAILED (paid) / EXPIRED all
  accrue when ``cost > 0``; zero / unknown cost is never fabricated into a charge.
* ``accrue_run_budget`` remains the SOLE writer of ``Project.budget_used``
  (governance invariant BA-1 / DR-D1-2: one budget authority).
* The per-attempt budget gate re-checks remaining budget on every attempt.

The DB is provided by the ``client`` fixture (conftest runs a real Alembic
``upgrade head``, so the migrated schema -- including ``20260909_0003`` -- is
present). ``make_session`` / ``get_engine(get_database_url())`` then point at the
same migrated temp database.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session

from aios.db import get_database_url, get_engine
from aios.delegation import (
    INFLIGHT_RUN_STATUSES,
    BudgetExceededError,
    accrue_run_budget,
    check_budget,
    projected_inflight_cost,
)
from aios.execution_run import (
    TERMINAL_RUN_STATUSES,
    acquire_run_lease,
    complete_local_run,
    complete_run,
    create_local_run,
    recover_stranded_runs,
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
    now_utc,
)


def _naive_now() -> datetime:
    return now_utc().replace(tzinfo=None)


def _seed_identity(session: Session):
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
    mode: DelegationMode = DelegationMode.WORKSTATION,
    status: DelegatedRunStatus = DelegatedRunStatus.SUBMITTED,
    cost: float = 0.0,
    usage: dict | None = None,
) -> DelegatedRun:
    run = DelegatedRun(
        project_id=project_id,
        task_id=task_id,
        agent_id=agent_id,
        delegation_mode=mode,
        status=status,
        idempotency_key=f"idem-{uuid4().hex[:12]}",
        attempt=1,
        cost=cost,
        usage=usage,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


@pytest.fixture
def db(authenticated_client) -> Session:
    """A session bound to the migrated test database (``authenticated_client``
    sets the URL and runs the Alembic upgrade on its lifespan)."""
    with Session(get_engine(get_database_url())) as s:
        yield s


# --- Local LLM produces a unified DelegatedRun -----------------------------

def test_local_run_records_delegated_run(db) -> None:
    project, agent, task = _seed_identity(db)
    run = create_local_run(
        db,
        task_id=task.id,
        project_id=project.id,
        attempt=1,
        idempotency_key="k-local",
    )
    assert run.delegation_mode == DelegationMode.LOCAL
    assert run.agent_id is None
    assert run.status == DelegatedRunStatus.SUBMITTED
    # The single attempt record is DelegatedRun -- no separate LocalRun table.
    persisted = db.get(DelegatedRun, run.id)
    assert persisted is not None
    assert persisted.delegation_mode == DelegationMode.LOCAL


def test_local_run_success_terminalizes_with_usage(db) -> None:
    project, agent, task = _seed_identity(db)
    run = create_local_run(
        db, task_id=task.id, project_id=project.id, attempt=1, idempotency_key="k-succ"
    )
    provider_usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    ok = complete_local_run(
        db, run_id=run.id, status=DelegatedRunStatus.SUCCEEDED, usage=provider_usage
    )
    assert ok is True
    persisted = db.get(DelegatedRun, run.id)
    assert persisted.status == DelegatedRunStatus.SUCCEEDED
    # Real provider usage is stored verbatim; never fabricated.
    assert persisted.usage == provider_usage


def test_local_run_usage_is_none_when_provider_reports_nothing(db) -> None:
    project, agent, task = _seed_identity(db)
    run = create_local_run(
        db, task_id=task.id, project_id=project.id, attempt=1, idempotency_key="k-nousage"
    )
    complete_local_run(db, run_id=run.id, status=DelegatedRunStatus.SUCCEEDED, usage=None)
    persisted = db.get(DelegatedRun, run.id)
    # None is preserved (distinct from empty dict) -- "no measurement" is NOT
    # silently treated as "zero usage".
    assert persisted.usage is None


def test_local_run_failure_terminalizes_failed(db) -> None:
    project, agent, task = _seed_identity(db)
    run = create_local_run(
        db, task_id=task.id, project_id=project.id, attempt=1, idempotency_key="k-fail"
    )
    complete_local_run(
        db,
        run_id=run.id,
        status=DelegatedRunStatus.FAILED,
        error="redacted detail",
    )
    assert db.get(DelegatedRun, run.id).status == DelegatedRunStatus.FAILED


# --- Unified, idempotent budget accrual (every terminal) --------------------

def test_succeeded_paid_run_accrues_once(db) -> None:
    project, agent, task = _seed_identity(db)
    assert project.budget_used == 0.0
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id, cost=2.5)
    # Terminalizing a remote (leased) run requires a valid lease, exactly as the
    # real delegation path acquires one before complete_run.
    assert acquire_run_lease(db, run_id=run.id, owner="w1")
    assert complete_run(db, run_id=run.id, owner="w1", status=DelegatedRunStatus.SUCCEEDED)
    db.refresh(project)
    assert project.budget_used == pytest.approx(2.5)
    assert db.get(DelegatedRun, run.id).budget_accrued_at is not None


def test_failed_paid_run_accrues(db) -> None:
    # The P0 gap: a paid failure was never charged. Now it is, like a success.
    project, agent, task = _seed_identity(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id, cost=3.0)
    assert acquire_run_lease(db, run_id=run.id, owner="w1")
    assert complete_run(db, run_id=run.id, owner="w1", status=DelegatedRunStatus.FAILED)
    db.refresh(project)
    assert project.budget_used == pytest.approx(3.0)


def test_expired_run_accrues_via_recovery(db) -> None:
    project, agent, task = _seed_identity(db)
    run = _run(
        db,
        project_id=project.id,
        task_id=task.id,
        agent_id=agent.id,
        cost=1.0,
        status=DelegatedRunStatus.SUBMITTED,
    )
    # Backdate so recovery reclaims it; no lease => reclaimable.
    old = _naive_now() - timedelta(seconds=1000)
    run.submitted_at = old
    db.add(run)
    db.commit()
    report = recover_stranded_runs(db, owner="recover", now=_naive_now())
    assert report.recovered_count == 1
    db.refresh(project)
    # The reclaimed EXPIRED run participates in the same accrual path.
    assert project.budget_used == pytest.approx(1.0)
    assert db.get(DelegatedRun, run.id).status == DelegatedRunStatus.EXPIRED


def test_accrual_is_idempotent_across_repeat_calls(db) -> None:
    project, agent, task = _seed_identity(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id, cost=2.0)
    # Terminalize first (complete_run accrues once on the SUCCEEDED transition).
    assert acquire_run_lease(db, run_id=run.id, owner="w1")
    assert complete_run(db, run_id=run.id, owner="w1", status=DelegatedRunStatus.SUCCEEDED)
    # A second accrual (retry / recovery re-run / concurrent terminalization)
    # must NOT double-charge.
    again = accrue_run_budget(db, run_id=run.id, now=_naive_now())
    assert again is False
    db.refresh(project)
    assert project.budget_used == pytest.approx(2.0)


def test_zero_cost_never_fabricated_into_a_charge(db) -> None:
    project, agent, task = _seed_identity(db)
    run = _run(
        db,
        project_id=project.id,
        task_id=task.id,
        agent_id=agent.id,
        cost=0.0,
        usage=None,
    )
    # A terminal, zero-cost run still gets its marker set (so recovery never
    # retries a no-op) but is NEVER charged.
    assert acquire_run_lease(db, run_id=run.id, owner="w1")
    assert complete_run(db, run_id=run.id, owner="w1", status=DelegatedRunStatus.SUCCEEDED)
    # Idempotent: the explicit second call is a no-op.
    assert accrue_run_budget(db, run_id=run.id, now=_naive_now()) is False
    db.refresh(project)
    assert project.budget_used == 0.0
    assert db.get(DelegatedRun, run.id).budget_accrued_at is not None


def test_non_terminal_run_is_not_accrued(db) -> None:
    project, agent, task = _seed_identity(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id, cost=5.0)
    # Run is still SUBMITTED -- accrual must refuse until terminal.
    assert accrue_run_budget(db, run_id=run.id, now=_naive_now()) is False
    db.refresh(project)
    assert project.budget_used == 0.0


# --- Governance invariant: single budget authority --------------------------

def test_accrue_run_budget_is_sole_budget_used_writer() -> None:
    """BA-1 / DR-D1-2: exactly one ``Project.budget_used`` writer, in delegation.py."""
    import ast
    from pathlib import Path

    writers: list[tuple[str, int]] = []
    for path in Path("src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "budget_used":
                    writers.append((path.name, node.lineno))
    assert len(writers) == 1, f"expected exactly one budget_used writer, found {writers}"
    assert writers[0][0] == "delegation.py", (
        f"the only budget_used writer must live in delegation.py, got {writers[0]}"
    )


# --- Per-attempt budget hard gate -------------------------------------------

def test_budget_gate_rechecks_remaining_budget_per_attempt(db) -> None:
    # A project with limit 10 and already-used 9.5: a 1.0 estimated attempt must
    # be hard-blocked. This is the gate ``delegation.run`` now invokes on EVERY
    # attempt (not once before the loop), so a costly retry sequence cannot
    # bypass the cap.
    project = Project(name="p2", objective="o", budget_limit=10.0, budget_used=9.5)
    db.add(project)
    db.commit()
    db.refresh(project)
    with pytest.raises(BudgetExceededError):
        check_budget(db, project, 1.0)
    # A within-budget estimate passes.
    check_budget(db, project, 0.4)  # 9.5 + 0.4 <= 10.0


# --- GAP-2: in-flight (concurrent) spend projection ------------------------
#
# The gate above only sees ACCRUED spend (``Project.budget_used``). Two
# delegations admitted in the same window therefore both read the same
# ``budget_used`` and each conclude it fits -- the concurrent over-commit
# window. GAP-2 closes it by adding a READ-TIME projection of the spend that is
# already in flight but not yet accrued: SUM of ``Task.estimated_cost`` over
# this project's non-terminal ``DelegatedRun``s. No reservation ledger, no
# migration, no second budget writer.


def _seed_budget_project(
    session: Session,
    *,
    budget_limit: float = 10.0,
    budget_used: float = 0.0,
    estimated_cost: float = 0.0,
    name: str = "gap2",
) -> tuple[Project, Task]:
    project = Project(
        name=name, objective="o", budget_limit=budget_limit, budget_used=budget_used
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    task = Task(
        project_id=project.id,
        title="t",
        description="d",
        status=TaskStatus.BACKLOG,
        output_schema={"type": "object"},
        estimated_cost=estimated_cost,
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return project, task


def test_inflight_run_reserves_its_estimate_in_the_budget_gate(db) -> None:
    """An in-flight run's estimate is reserved, so a concurrent second
    delegation can no longer pass the gate on stale ``budget_used`` alone."""
    project, task = _seed_budget_project(db, budget_limit=10.0, estimated_cost=6.0)
    _run(
        db,
        project_id=project.id,
        task_id=task.id,
        status=DelegatedRunStatus.SUBMITTED,
    )
    # 0 used + 6 in flight + 5 requested = 11 > 10 -> hard blocked.
    with pytest.raises(BudgetExceededError):
        check_budget(db, project, 5.0)
    # 0 + 6 + 4 = 10 <= 10 -> admitted.
    check_budget(db, project, 4.0)


def test_running_status_also_counts_as_inflight(db) -> None:
    project, task = _seed_budget_project(db, budget_limit=10.0, estimated_cost=6.0)
    _run(
        db,
        project_id=project.id,
        task_id=task.id,
        status=DelegatedRunStatus.RUNNING,
    )
    with pytest.raises(BudgetExceededError):
        check_budget(db, project, 5.0)
    check_budget(db, project, 4.0)


def test_concurrent_inflight_runs_sum_before_the_gate(db) -> None:
    """The projection is a SUM, not a single-run peek: N concurrent
    delegations each reserve their own estimate."""
    project, task_a = _seed_budget_project(db, budget_limit=10.0, estimated_cost=3.0)
    task_b = Task(
        project_id=project.id,
        title="t2",
        description="d",
        status=TaskStatus.BACKLOG,
        output_schema={"type": "object"},
        estimated_cost=3.0,
    )
    db.add(task_b)
    db.commit()
    db.refresh(task_b)
    _run(db, project_id=project.id, task_id=task_a.id, status=DelegatedRunStatus.SUBMITTED)
    _run(db, project_id=project.id, task_id=task_b.id, status=DelegatedRunStatus.RUNNING)
    with pytest.raises(BudgetExceededError):
        check_budget(db, project, 5.0)  # 0 + 3 + 3 + 5 = 11 > 10
    check_budget(db, project, 4.0)  # 0 + 3 + 3 + 4 = 10


def test_terminal_run_hands_over_from_estimate_to_accrual(db) -> None:
    """No gap, no overlap: while the run is in flight its ESTIMATE is reserved;
    once it terminalizes the reservation is released and the REAL cost arrives
    through the single accrual writer. The projection is continuous."""
    project, task = _seed_budget_project(db, budget_limit=10.0, estimated_cost=6.0)
    run = _run(db, project_id=project.id, task_id=task.id, status=DelegatedRunStatus.SUBMITTED)
    # In flight: 6 reserved.
    assert projected_inflight_cost(db, project_id=project.id) == 6.0
    with pytest.raises(BudgetExceededError):
        check_budget(db, project, 5.0)

    # Terminalize + accrue (the real hand-off, via the single budget writer).
    run.status = DelegatedRunStatus.SUCCEEDED
    run.finished_at = _naive_now()
    run.cost = 6.0
    db.add(run)
    db.commit()
    assert accrue_run_budget(db, run_id=run.id) is True
    db.refresh(project)

    # The run left the projection and its cost entered budget_used: the SAME
    # 6.0 is counted exactly once, never twice.
    assert projected_inflight_cost(db, project_id=project.id) == 0.0
    assert project.budget_used == 6.0
    check_budget(db, project, 4.0)  # 6 + 0 + 4 = 10 <= 10
    with pytest.raises(BudgetExceededError):
        check_budget(db, project, 4.5)


def test_inflight_runs_of_other_projects_are_not_reserved(db) -> None:
    """Budget is project-scoped: another project's in-flight work must never
    consume this project's head-room."""
    other_project, other_task = _seed_budget_project(
        db, budget_limit=10.0, estimated_cost=9.0, name="other"
    )
    _run(
        db,
        project_id=other_project.id,
        task_id=other_task.id,
        status=DelegatedRunStatus.SUBMITTED,
    )
    project, _task = _seed_budget_project(db, budget_limit=10.0, estimated_cost=0.0)
    assert projected_inflight_cost(db, project_id=project.id) == 0.0
    check_budget(db, project, 9.0)  # unaffected


def test_zero_estimate_inflight_matches_legacy_behaviour(db) -> None:
    """An in-flight run whose task carries no estimate changes nothing -- the
    gate behaves exactly as it did before GAP-2."""
    project, task = _seed_budget_project(
        db, budget_limit=10.0, budget_used=9.5, estimated_cost=0.0
    )
    _run(db, project_id=project.id, task_id=task.id, status=DelegatedRunStatus.SUBMITTED)
    with pytest.raises(BudgetExceededError):
        check_budget(db, project, 1.0)
    check_budget(db, project, 0.4)


def test_unenforced_project_skips_the_projection(db) -> None:
    """``budget_limit <= 0`` stays an unconditional shortcut (legacy / open
    projects): no projection, no block."""
    project, task = _seed_budget_project(
        db, budget_limit=0.0, budget_used=999.0, estimated_cost=999.0
    )
    _run(db, project_id=project.id, task_id=task.id, status=DelegatedRunStatus.SUBMITTED)
    check_budget(db, project, 999.0)


def test_inflight_statuses_are_the_complement_of_terminal_statuses() -> None:
    """Guard the status partition: every run status is either in flight (spend
    not yet accrued) or terminal (spend already handed to accrual)."""
    assert set(INFLIGHT_RUN_STATUSES).isdisjoint(set(TERMINAL_RUN_STATUSES))
    assert set(INFLIGHT_RUN_STATUSES) | set(TERMINAL_RUN_STATUSES) == set(
        DelegatedRunStatus
    )


def test_projection_is_read_only(db) -> None:
    """The projection must not mutate anything: it is a SELECT, never a
    reservation, so it can never become a second budget writer."""
    project, task = _seed_budget_project(db, budget_limit=10.0, estimated_cost=6.0)
    run = _run(db, project_id=project.id, task_id=task.id, status=DelegatedRunStatus.SUBMITTED)
    before_used = project.budget_used
    before_status, before_cost = run.status, run.cost
    check_budget(db, project, 4.0)
    db.refresh(project)
    db.refresh(run)
    assert project.budget_used == before_used
    assert run.status == before_status
    assert run.cost == before_cost
    assert run.budget_accrued_at is None


def test_budget_error_reports_the_inflight_component(db) -> None:
    """The block reason must be owner-debuggable: it names the reserved
    in-flight amount, not just used + estimate."""
    project, task = _seed_budget_project(db, budget_limit=10.0, estimated_cost=6.0)
    _run(db, project_id=project.id, task_id=task.id, status=DelegatedRunStatus.SUBMITTED)
    with pytest.raises(BudgetExceededError) as exc:
        check_budget(db, project, 5.0)
    message = str(exc.value)
    assert "in_flight=6.0000" in message
