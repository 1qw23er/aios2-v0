"""Contract tests for Workforce W8-v2 -- the Workforce Execution Bridge.

Companion to the W8-v2 Implementation Design V1 (issue #112). Coverage:

* migration: clean upgrade, backfill of pre-existing Employees, round-trip,
  fail-closed downgrade, DB-level partial unique indexes (the FINAL authority,
  proven with raw inserts that bypass the service), interval CHECK;
* promote seam: the initial binding is born inside the promote savepoint and
  is idempotent-replay safe; agent-reuse at promote time is a clean 409;
* binding lifecycle: bind / replace (continuity, no gap, no overlap) / unbind
  / rebind / history / actor gates / conflict codes;
* attribution: ``Task.created_at`` anchored, binding-only (NEVER the
  ``Employee.agent_id`` snapshot), stable across rebinds, agent transfer,
  agent reuse and agent return, long-running Tasks, Artifacts produced after
  a rebind, independent-session rebuild;
* assignment: ``create_task`` reuse (fingerprint idempotency inherited), FIXED
  routing via the existing scheduler, governance not bypassed (a disabled
  agent is rejected by the SCHEDULER, not by a copied check);
* API: 200/201/404/409/422/401 over the 8 bridge endpoints;
* invariants: EmployeeStatus unchanged, no second execution path.

Helpers mirror ``tests/test_workforce_employee_w4.py`` so this file is
self-contained.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from aios.actor import ActorContext
from aios.audit import AuditLog
from aios.db import get_engine, run_migrations
from aios.employee_bridge import (
    assign_work_to_employee,
    bind_employee_agent,
    employee_for_artifact,
    employee_for_task,
    get_current_employee_binding,
    get_employee_binding_history,
    replace_employee_agent,
    unbind_employee_agent,
)
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    ApprovalStatus,
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    Capability,
    Employee,
    EmployeeAgentBinding,
    EmployeeStatus,
    Job,
    JobVersion,
    Project,
    Task,
    Trial,
    TrialOutcome,
)
from aios.schemas import EmployeeWorkCreate
from aios.services import ServiceError
from aios.workforce import (
    compute_match,
    create_business_goal,
    create_job,
    create_required_work,
    discover_candidates,
    evaluate_candidate,
)
from aios.workforce_employee import (
    activate_trial,
    complete_trial,
    promote_to_employee,
)
from aios.workforce_recommendation import decide_recommendation, recommend_candidate
from aios.workforce_trial import create_trial_from_approval
from alembic import command

ROOT = Path(__file__).resolve().parents[1]
PREV_HEAD = "20260906_0001_recommendation_trust_advisory"

OWNER = ActorContext(kind="owner", owner_id="owner")
AGENT_ACTOR = ActorContext(kind="agent", agent_id="agent-1")


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors test_workforce_employee_w4.py)
# ---------------------------------------------------------------------------


def _db(url: str) -> Session:
    run_migrations(url)
    return Session(get_engine(url))


def _cfg(url: str) -> Config:
    cfg = Config(ROOT / "alembic.ini")
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _seed_capability(session: Session, name: str) -> Capability:
    cap = Capability(name=name, description=f"{name} capability")
    session.add(cap)
    session.commit()
    return cap


def _seed_agent(
    session: Session,
    name: str,
    capabilities: dict[str, int] | None = None,
) -> Agent:
    agent = Agent(name=name, role=name, adapter_type=AdapterType.EXTERNAL)
    session.add(agent)
    session.flush()
    for cap_name, priority in (capabilities or {}).items():
        cap = session.exec(select(Capability).where(Capability.name == cap_name)).first()
        assert cap is not None, f"capability must be seeded first: {cap_name}"
        session.add(AgentCapability(agent_id=agent.id, capability_id=cap.id, priority=priority))
    session.commit()
    return agent


def _build_chain(session: Session, cap_name: str) -> tuple[Job, JobVersion]:
    goal = create_business_goal(session, "增长北极星", target_outcome="新增注册 +20%")
    rw = create_required_work(session, goal.id, "公众号内容生产", rationale="内容带来自然注册")
    job = create_job(
        session,
        rw.id,
        "内容初稿研究员",
        role_summary="把选题做成初稿",
        capability_names=[cap_name],
    )
    session.commit()
    head = session.get(JobVersion, job.head_version_id)
    assert head is not None
    return job, head


def _hire(session: Session, cap_name: str = "writing", *, agent: Agent | None = None) -> Employee:
    """The full W1-W4 chain ending in a promoted Employee (with initial binding).

    Pass ``agent`` to promote a candidate that carries a SPECIFIC agent's
    identity (used for the snapshot-reuse scenarios): the capability is linked
    onto that agent instead of seeding a fresh one, so ``discover_candidates``
    resolves to exactly that agent.
    """
    cap = _seed_capability(session, cap_name)
    if agent is None:
        _seed_agent(session, "A", {cap_name: 80})
    else:
        session.add(AgentCapability(agent_id=agent.id, capability_id=cap.id, priority=80))
        session.commit()
    _, head = _build_chain(session, cap_name)
    cands = discover_candidates(session, head.id)
    session.commit()
    assert len(cands) == 1, "fixture expects exactly one matching agent"
    cand = cands[0]
    evaluate_candidate(session, cand.id)
    session.commit()
    compute_match(session, cand.id, head.id)
    session.commit()
    rec = recommend_candidate(session, cand.id)
    session.commit()
    decide_recommendation(session, rec.id, ApprovalStatus.APPROVED, actor=OWNER)
    session.commit()
    trial = create_trial_from_approval(session, rec.id, actor=OWNER)
    session.commit()
    activate_trial(session, trial.id, actor=OWNER)
    session.commit()
    complete_trial(session, trial.id, outcome=TrialOutcome.PASS, actor=OWNER)
    session.commit()
    return promote_to_employee(session, trial.id, actor=OWNER)


def _project(session: Session) -> Project:
    project = Project(name="P", objective="o", owner="human_ceo", budget_limit=100.0)
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def _work(session: Session, project: Project) -> EmployeeWorkCreate:
    return EmployeeWorkCreate(
        project_id=project.id,
        title="t",
        description="d",
        output_schema={},
    )


def _audits(session: Session, action: str) -> list[AuditLog]:
    return list(session.exec(select(AuditLog).where(AuditLog.action == action)).all())


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


# Raw-SQL seed rows for migration tests: a pre-W8 Employee whose parent rows
# never existed. Inserted through a bare ``sqlite3`` connection (FK pragma
# OFF) so the dangling lineage FKs are tolerated, exactly like a real
# pre-upgrade database. Column lists are the tables' NOT NULL columns.
_AGENT_INSERT = (
    "INSERT INTO agent (id, name, role, adapter_type, capabilities, permissions, "
    "cost_policy, enabled, status, limitations, trust_level, timeout_s, max_retries) "
    "VALUES ('ag_x', 'A', 'A', 'external', '[]', '[]', '{}', 1, 'available', "
    "'[]', 'unknown', 300.0, 3)"
)
_EMPLOYEE_INSERT = (
    "INSERT INTO employee (id, candidate_id, trial_id, agent_id, job_id, "
    "job_version_id, status, hired_at, created_at, updated_at) VALUES ("
    "'emp_x', 'c_x', 't_x', 'ag_x', 'j_x', 'jv_x', 'active', "
    "'2026-02-02 02:02:02.000000', '2026-02-02 02:02:02.000000', "
    "'2026-02-02 02:02:02.000000')"
)
_TS = "2026-02-02 02:02:02.000000"


def _seed_pre_w8_rows(db_path: Path) -> None:
    import sqlite3 as _sq

    conn = _sq.connect(str(db_path))
    try:
        conn.execute(_AGENT_INSERT)
        conn.execute(_EMPLOYEE_INSERT)
        conn.commit()
    finally:
        conn.close()


def test_migration_upgrade_backfills_existing_employees(tmp_path: Path) -> None:
    """Every pre-existing Employee gets exactly one current binding at upgrade."""
    db_path = tmp_path / "backfill.db"
    url = f"sqlite:///{db_path.as_posix()}"
    cfg = _cfg(url)
    command.upgrade(cfg, PREV_HEAD)  # explicit revision -> REAL alembic engine
    _seed_pre_w8_rows(db_path)
    command.upgrade(cfg, "20260906_0002_workforce_agent_binding")
    eng = get_engine(url)
    insp = inspect(eng)
    assert "employee_agent_binding" in insp.get_table_names()
    idx = {i["name"] for i in insp.get_indexes("employee_agent_binding")}
    assert {"uq_eab_employee_current", "uq_eab_agent_current"} <= idx
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT employee_id, agent_id, effective_from, effective_to "
                "FROM employee_agent_binding"
            )
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "emp_x" and rows[0][1] == "ag_x"
    assert rows[0][3] is None  # current binding
    assert "2026-02-02" in str(rows[0][2])
    eng.dispose()


def test_migration_round_trip_and_fail_closed_downgrade(tmp_path: Path) -> None:
    """upgrade -> downgrade (empty: ok) -> upgrade -> downgrade (rows: refused)."""
    db_path = tmp_path / "roundtrip.db"
    url = f"sqlite:///{db_path.as_posix()}"
    cfg = _cfg(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, PREV_HEAD)
    eng = get_engine(url)
    assert "employee_agent_binding" not in inspect(eng).get_table_names()
    command.upgrade(cfg, "head")
    _seed_pre_w8_rows(db_path)
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO employee_agent_binding (id, employee_id, agent_id, "
                "effective_from, effective_to, created_at) VALUES ('eab_x', 'emp_x', "
                "'ag_x', :ts, NULL, :ts)"
            ),
            {"ts": _TS},
        )
    with pytest.raises(RuntimeError, match="not recoverable"):
        command.downgrade(cfg, PREV_HEAD)
    eng.dispose()


def test_partial_unique_indexes_are_the_final_authority(tmp_path: Path) -> None:
    """Raw inserts bypassing the service still hit both current-1:1 indexes.

    Each case is isolated so the failing constraint is unambiguous (all FK
    targets exist; the violated index is the partial unique one)."""
    url = f"sqlite:///{(tmp_path / 'raw.db').as_posix()}"
    with _db(url) as session:
        alice = _hire(session, "writing")  # current binding: agent A
        bob = _hire(session, "editing")  # current binding: agent B
        agent_a, alice_id, bob_id = alice.agent_id, alice.id, bob.id
        unbind_employee_agent(session, bob.id, actor=OWNER)  # bob: no current
    ts = "2026-03-03 03:03:03.000000"

    # (1) employee axis: bob is free, agent A is NOT current -> binding bob->A
    # is legal, so FIRST make alice the blocker: insert (alice, B-open) hits
    # uq_eab_employee_current (alice already current; agent B exists).
    with pytest.raises(IntegrityError), Session(get_engine(url)) as raw:
        raw.execute(
            text(
                "INSERT INTO employee_agent_binding (id, employee_id, "
                "agent_id, effective_from, effective_to, created_at) VALUES "
                "('eab_1', :e, (SELECT agent_id FROM employee WHERE id = '"
                + bob_id
                + "'), :ts, NULL, :ts)"
            ),
            {"e": alice_id, "ts": ts},
        )
        raw.commit()

    # (2) agent axis: bob is free; insert (bob, agentA-open) where agent A is
    # still alice's CURRENT binding -> uq_eab_agent_current alone fires.
    with pytest.raises(IntegrityError), Session(get_engine(url)) as raw:
        raw.execute(
            text(
                "INSERT INTO employee_agent_binding (id, employee_id, "
                "agent_id, effective_from, effective_to, created_at) VALUES "
                "('eab_2', :e, :a, :ts, NULL, :ts)"
            ),
            {"e": bob_id, "a": agent_a, "ts": ts},
        )
        raw.commit()

    # (3) interval CHECK: an empty interval [t, t) is impossible.
    with pytest.raises(IntegrityError), Session(get_engine(url)) as raw:
        raw.execute(
            text(
                "INSERT INTO employee_agent_binding (id, employee_id, "
                "agent_id, effective_from, effective_to, created_at) VALUES "
                "('eab_3', :e, :a, :ts, :ts, :ts)"
            ),
            {"e": bob_id, "a": agent_a, "ts": ts},
        )
        raw.commit()


# ---------------------------------------------------------------------------
# Promote seam (W8-v2 INV-B1)
# ---------------------------------------------------------------------------


def test_promote_creates_initial_binding_inside_savepoint(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'promote.db').as_posix()}"
    with _db(url) as session:
        emp = _hire(session)
        binding = get_current_employee_binding(session, emp.id)
        assert binding is not None
        assert binding.agent_id == emp.agent_id  # the SAME promote snapshot
        assert binding.effective_to is None
        # hired_at is in-session (tz-aware); storage is naive -- same instant.
        assert str(binding.effective_from) == str(emp.hired_at.replace(tzinfo=None))
        # Exactly one binding exists for the fresh hire.
        assert len(get_employee_binding_history(session, emp.id)) == 1


def test_promote_idempotent_replay_never_duplicates_binding(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'replay.db').as_posix()}"
    with _db(url) as session:
        emp = _hire(session, "writing")
        # Discover the trial id via the employee anchor, then replay promote.
        trial_id = session.exec(select(Trial.id).where(Trial.id == emp.trial_id)).first()
        assert trial_id is not None
        again = promote_to_employee(session, trial_id, actor=OWNER)
        assert again.id == emp.id
        assert len(get_employee_binding_history(session, emp.id)) == 1


def test_promote_rejects_agent_currently_bound_elsewhere(tmp_path: Path) -> None:
    """Agent snapshot reuse at promote time: second hire on the same agent is a
    clean 409 while the first Employee still holds the current binding."""
    url = f"sqlite:///{(tmp_path / 'reuse.db').as_posix()}"
    with _db(url) as session:
        alice = _hire(session, "writing")  # alice holds agent A's current binding
        agent_a = session.get(Agent, alice.agent_id)
        assert agent_a is not None
        with pytest.raises(ServiceError) as excinfo:
            _hire(session, "writing2", agent=agent_a)  # bob's snapshot: SAME agent
        assert excinfo.value.status_code == 409
        assert "agent_binding_conflict" in excinfo.value.detail


# ---------------------------------------------------------------------------
# Binding lifecycle
# ---------------------------------------------------------------------------


def test_bind_rejects_employee_that_already_has_a_current_binding(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{(tmp_path / 'bind1.db').as_posix()}"
    with _db(url) as session:
        emp = _hire(session, "writing")
        other = _seed_agent(session, "B")
        with pytest.raises(ServiceError) as excinfo:
            bind_employee_agent(session, emp.id, other.id, actor=OWNER)
        assert excinfo.value.status_code == 409
        assert "employee_binding_conflict" in excinfo.value.detail


def test_bind_rejects_agent_bound_to_another_employee(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'bind2.db').as_posix()}"
    with _db(url) as session:
        alice = _hire(session, "writing")  # agent A
        _hire(session, "editing")  # bob holds agent B
        bob = session.exec(select(Employee).where(Employee.trial_id != alice.trial_id)).first()
        assert bob is not None
        bob_binding = get_current_employee_binding(session, bob.id)
        assert bob_binding is not None
        # Free alice first so the EMPLOYEE axis is clean and the AGENT axis is
        # what fires: alice (unbound) -> bob's agent -> agent_binding_conflict.
        unbind_employee_agent(session, alice.id, actor=OWNER)
        with pytest.raises(ServiceError) as excinfo:
            bind_employee_agent(session, alice.id, bob_binding.agent_id, actor=OWNER)
        assert excinfo.value.status_code == 409
        assert "agent_binding_conflict" in excinfo.value.detail


def test_unbind_then_rebind_opens_new_history_row(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'rebind.db').as_posix()}"
    with _db(url) as session:
        emp = _hire(session, "writing")
        first = get_current_employee_binding(session, emp.id)
        assert first is not None
        closed = unbind_employee_agent(session, emp.id, actor=OWNER)
        assert closed.effective_to is not None
        assert get_current_employee_binding(session, emp.id) is None
        # Repeat unbind: nothing to close -> 404.
        with pytest.raises(ServiceError) as excinfo:
            unbind_employee_agent(session, emp.id, actor=OWNER)
        assert excinfo.value.status_code == 404
        # Rebind the SAME agent: a NEW row (history is N:M, closed rows stay).
        rebound = bind_employee_agent(session, emp.id, first.agent_id, actor=OWNER)
        assert rebound.effective_to is None
        history = get_employee_binding_history(session, emp.id)
        assert len(history) == 2
        assert history[0].id == rebound.id  # most recent first


def test_replace_closes_old_and_opens_new_with_no_gap(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'replace.db').as_posix()}"
    with _db(url) as session:
        emp = _hire(session, "writing")
        new_agent = _seed_agent(session, "B")
        result = replace_employee_agent(session, emp.id, new_agent.id, actor=OWNER)
        old, new = result["old"], result["new"]
        assert old.effective_to is not None
        assert str(old.effective_to) == str(new.effective_from)  # continuity
        assert new.agent_id == new_agent.id and new.effective_to is None
        current = get_current_employee_binding(session, emp.id)
        assert current is not None and current.id == new.id
        assert len(get_employee_binding_history(session, emp.id)) == 2


def test_replace_without_current_binding_rejected(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'replace2.db').as_posix()}"
    with _db(url) as session:
        emp = _hire(session, "writing")
        other = _seed_agent(session, "B")
        unbind_employee_agent(session, emp.id, actor=OWNER)
        with pytest.raises(ServiceError) as excinfo:
            replace_employee_agent(session, emp.id, other.id, actor=OWNER)
        assert excinfo.value.status_code == 409
        assert "no_current_binding" in excinfo.value.detail


def test_replace_to_same_agent_rejected(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'replace3.db').as_posix()}"
    with _db(url) as session:
        emp = _hire(session, "writing")
        with pytest.raises(ServiceError) as excinfo:
            replace_employee_agent(session, emp.id, emp.agent_id, actor=OWNER)
        assert excinfo.value.status_code == 409
        assert "already_bound_to_agent" in excinfo.value.detail


def test_non_owner_actor_and_missing_employee_rejected(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'gate.db').as_posix()}"
    with _db(url) as session:
        emp = _hire(session, "writing")
        other = _seed_agent(session, "B")
        with pytest.raises(ServiceError) as exc403:
            bind_employee_agent(session, emp.id, other.id, actor=AGENT_ACTOR)
        assert exc403.value.status_code == 403
        with pytest.raises(TypeError):
            bind_employee_agent(session, emp.id, other.id)  # no default actor (Q7)
        with pytest.raises(ServiceError) as exc404:
            bind_employee_agent(session, "emp_missing", other.id, actor=OWNER)
        assert exc404.value.status_code == 404


# ---------------------------------------------------------------------------
# Assignment (reuses create_task + route_task; governance not bypassed)
# ---------------------------------------------------------------------------


def test_assign_creates_fixed_task_through_existing_path(tmp_path: Path) -> None:
    from aios.models import ExecutionAssignment

    url = f"sqlite:///{(tmp_path / 'assign.db').as_posix()}"
    with _db(url) as session:
        emp = _hire(session, "writing")
        project = _project(session)
        task = assign_work_to_employee(
            session,
            emp.id,
            _work(session, project),
            idempotency_key="wf-key-1",
            actor=OWNER,
        )
        session.refresh(task)
        assert task.assigned_agent_id == emp.agent_id  # frozen at creation
        assert str(task.routing_mode) == "fixed"
        # The existing deterministic scheduler actually routed it.
        assignment = session.exec(
            select(ExecutionAssignment).where(ExecutionAssignment.task_id == task.id)
        ).first()
        assert assignment is not None
        # create_task's fingerprint idempotency is inherited verbatim.
        replay = assign_work_to_employee(
            session,
            emp.id,
            _work(session, project),
            idempotency_key="wf-key-1",
            actor=OWNER,
        )
        assert replay.id == task.id


def test_assign_without_current_binding_rejected(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'assign2.db').as_posix()}"
    with _db(url) as session:
        emp = _hire(session, "writing")
        project = _project(session)
        unbind_employee_agent(session, emp.id, actor=OWNER)
        with pytest.raises(ServiceError) as excinfo:
            assign_work_to_employee(
                session,
                emp.id,
                _work(session, project),
                idempotency_key="k",
                actor=OWNER,
            )
        assert excinfo.value.status_code == 409
        assert "no_current_binding" in excinfo.value.detail


def test_assign_does_not_bypass_scheduler_governance(tmp_path: Path) -> None:
    """A disabled agent still creates the Task (control plane) but the EXISTING
    scheduler gate refuses routing -- W8 copies no availability check."""
    from aios.models import ExecutionAssignment

    url = f"sqlite:///{(tmp_path / 'gov.db').as_posix()}"
    with _db(url) as session:
        emp = _hire(session, "writing")
        project = _project(session)
        agent = session.get(Agent, emp.agent_id)
        assert agent is not None
        agent.enabled = False
        session.add(agent)
        session.commit()
        task = assign_work_to_employee(
            session,
            emp.id,
            _work(session, project),
            idempotency_key="wf-gov",
            actor=OWNER,
        )
        session.refresh(task)
        assert task.assigned_agent_id == emp.agent_id
        assert (
            session.exec(
                select(ExecutionAssignment).where(ExecutionAssignment.task_id == task.id)
            ).first()
            is None
        ), "routing must stay owned by the existing scheduler gate"


# ---------------------------------------------------------------------------
# Attribution (Task.created_at anchor; binding-only; snapshot never consulted)
# ---------------------------------------------------------------------------


def test_attribution_survives_rebind_and_agent_transfer(tmp_path: Path) -> None:
    """Agent A: Alice (task1) -> transferred to Bob (task2). Historical facts
    never move: task1 stays Alice's, task2 is Bob's."""
    url = f"sqlite:///{(tmp_path / 'attr.db').as_posix()}"
    with _db(url) as session:
        alice = _hire(session, "writing")  # snapshot + binding: agent A
        agent_a = alice.agent_id
        project = _project(session)
        task1 = assign_work_to_employee(
            session,
            alice.id,
            _work(session, project),
            idempotency_key="t1",
            actor=OWNER,
        )
        # Transfer agent A from Alice to Bob: free BOTH sides first (Bob's
        # promote gave him agent B), then bind A to Bob.
        unbind_employee_agent(session, alice.id, actor=OWNER)
        bob = _hire(session, "editing")  # snapshot: agent B
        unbind_employee_agent(session, bob.id, actor=OWNER)
        bind_employee_agent(session, bob.id, agent_a, actor=OWNER)
        task2 = assign_work_to_employee(
            session,
            bob.id,
            _work(session, project),
            idempotency_key="t2",
            actor=OWNER,
        )
        assert employee_for_task(session, task1) == alice.id
        assert employee_for_task(session, task2) == bob.id
        # Both Employees share... no -- snapshots differ (A vs B); the agent is
        # what is REUSED. Artifacts: task1's artifact (created AFTER the
        # transfer, i.e. long-running task) still attributes Alice.
        artifact = Artifact(
            project_id=project.id,
            task_id=task1.id,
            adapter_id=agent_a,
            type=ArtifactType.MARKDOWN,
            uri="s3://bucket/art1",
            checksum="chk1",
            review_status=ArtifactReviewStatus.UNVERIFIED,
        )
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        assert employee_for_artifact(session, artifact) == alice.id


def test_attribution_disambiguates_agent_snapshot_reuse(tmp_path: Path) -> None:
    """The killer scenario: TWO Employees whose immutable snapshots name the
    SAME agent (Alice: A then unbound; Bob promoted on A later). The snapshot
    alone is ambiguous; the binding interval is not. Attribution must resolve
    by era, proving ``Employee.agent_id`` is never consulted."""
    url = f"sqlite:///{(tmp_path / 'snapreuse.db').as_posix()}"
    with _db(url) as session:
        alice = _hire(session, "writing")  # snapshot agent A, binding A
        agent_a = alice.agent_id
        project = _project(session)
        task1 = assign_work_to_employee(
            session,
            alice.id,
            _work(session, project),
            idempotency_key="t1",
            actor=OWNER,
        )
        # Alice leaves the agent: A's current binding closes.
        unbind_employee_agent(session, alice.id, actor=OWNER)
        # Bob is promoted on a candidate carrying the SAME agent A -- now
        # legal, because A has no current binding. Bob's snapshot: agent A too.
        agent_a_obj = session.get(Agent, agent_a)
        assert agent_a_obj is not None
        bob = _hire(session, "writing2", agent=agent_a_obj)
        session.expire_all()
        bob = session.get(Employee, bob.id)
        alice = session.get(Employee, alice.id)
        assert bob is not None and alice is not None
        assert bob.agent_id == agent_a, "fixture expects identical snapshots"
        assert bob.id != alice.id
        task2 = assign_work_to_employee(
            session,
            bob.id,
            _work(session, project),
            idempotency_key="t2",
            actor=OWNER,
        )
        assert employee_for_task(session, task1) == alice.id
        assert employee_for_task(session, task2) == bob.id
        # An agent never bound to anyone attributes to nobody.
        ghost = Agent(name="ghost", role="g", adapter_type=AdapterType.EXTERNAL)
        session.add(ghost)
        session.commit()
        session.refresh(ghost)
        ghost_task = Task(
            project_id=project.id,
            title="t",
            description="d",
            assigned_agent_id=ghost.id,
        )
        session.add(ghost_task)
        session.commit()
        session.refresh(ghost_task)
        assert employee_for_task(session, ghost_task) is None


def test_attribution_rebuilds_from_an_independent_session(tmp_path: Path) -> None:
    """Pure read-path: a brand-new session (no identity-map state) rebuilds the
    same attribution from the binding history alone."""
    url = f"sqlite:///{(tmp_path / 'indep.db').as_posix()}"
    with _db(url) as session:
        emp = _hire(session, "writing")
        project = _project(session)
        task = assign_work_to_employee(
            session,
            emp.id,
            _work(session, project),
            idempotency_key="t1",
            actor=OWNER,
        )
        original = employee_for_task(session, task)
        task_id, emp_id = task.id, emp.id
    with Session(get_engine(url)) as fresh:
        reloaded = fresh.get(Task, task_id)
        assert reloaded is not None
        assert employee_for_task(fresh, reloaded) == original == emp_id


# ---------------------------------------------------------------------------
# API surface (8 endpoints, owner-only, ServiceError -> 409 mapping)
# ---------------------------------------------------------------------------


def _api_session() -> Session:
    from aios.db import get_database_url

    return Session(get_engine(get_database_url()))


def test_api_binding_lifecycle_flow(authenticated_client: TestClient) -> None:
    client = authenticated_client
    s = _api_session()
    emp = _hire(s, "writing")
    other = _seed_agent(s, "B")
    emp_id, other_id = emp.id, other.id
    s.close()
    # 201 bind -> blocked: employee already current (promote owns the first).
    r = client.post(f"/employees/{emp_id}/agent-binding", json={"agent_id": other_id})
    assert r.status_code == 409
    # current -> replace -> history -> unbind -> rebind.
    r = client.get(f"/employees/{emp_id}/agent-binding")
    assert r.status_code == 200 and r.json()["agent_id"] is not None
    r = client.post(f"/employees/{emp_id}/agent-binding/replace", json={"agent_id": other_id})
    assert r.status_code == 200
    body = r.json()
    assert body["old"]["effective_to"] == body["new"]["effective_from"]
    r = client.get(f"/employees/{emp_id}/agent-binding/history")
    assert r.status_code == 200 and len(r.json()) == 2
    r = client.delete(f"/employees/{emp_id}/agent-binding")
    assert r.status_code == 200
    r = client.get(f"/employees/{emp_id}/agent-binding")
    assert r.status_code == 200 and r.json() is None
    r = client.delete(f"/employees/{emp_id}/agent-binding")
    assert r.status_code == 404
    # 404s and 422.
    assert (
        client.post("/employees/emp_missing/agent-binding", json={"agent_id": other_id}).status_code
        == 404
    )
    assert client.post(f"/employees/{emp_id}/agent-binding", json={}).status_code == 422


def test_api_assign_and_attribution_endpoints(authenticated_client: TestClient) -> None:
    client = authenticated_client
    s = _api_session()
    emp = _hire(s, "writing")
    project = _project(s)
    emp_id, project_id = emp.id, project.id
    s.close()
    r = client.post(
        f"/employees/{emp_id}/work",
        json={"project_id": project_id, "title": "t", "description": "d", "output_schema": {}},
        headers={"Idempotency-Key": "api-key-1"},
    )
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]
    r = client.get(f"/tasks/{task_id}/employee-attribution")
    assert r.status_code == 200 and r.json()["employee_id"] == emp_id
    assert client.get("/artifacts/art_missing/employee-attribution").status_code == 404
    assert client.get("/tasks/task_missing/employee-attribution").status_code == 404


def test_api_requires_authentication(owner_auth_client: TestClient, monkeypatch) -> None:
    """Bridge routes sit behind the real ``authenticate_owner`` contract:
    unconfigured owner env -> 503 (server misconfiguration, never 401);
    configured env + wrong Basic credentials -> 401 (same branches the
    owner-auth suite pins for every owner surface)."""
    client = owner_auth_client
    from aios.api.security import OWNER_API_KEY_ENV, OWNER_ID_ENV

    # Unconfigured: the bridge route must behave like every owner surface.
    r = client.get("/employees/emp_x/agent-binding")
    assert r.status_code == 503
    assert r.json()["detail"] == "owner_auth_not_configured"
    # Configured + wrong credentials -> 401.
    monkeypatch.setenv(OWNER_ID_ENV, "owner-real")
    monkeypatch.setenv(OWNER_API_KEY_ENV, "k" * 40)
    r = client.get("/employees/emp_x/agent-binding")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Invariants (fast structural checks)
# ---------------------------------------------------------------------------


def test_employee_status_enum_is_unchanged_and_binding_model_shape() -> None:
    """EmployeeStatus stays ACTIVE-only (W7-I12); the binding model carries
    exactly the V1 schema columns -- no created_by/reason/version creep."""
    assert [m.value for m in EmployeeStatus] == ["active"]
    cols = {c.name for c in EmployeeAgentBinding.__table__.columns}
    assert cols == {
        "id",
        "employee_id",
        "agent_id",
        "effective_from",
        "effective_to",
        "created_at",
    }
