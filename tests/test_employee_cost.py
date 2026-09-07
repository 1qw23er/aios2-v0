"""Contract tests for Workforce W8-P1 -- the Employee cost closure.

Companion to ``src/aios/employee_cost.py`` and
``docs/workforce/Workforce_W8P1_Cost_Closure_V1.md``. Coverage:

* closure happy path: one ``cost_evidence`` row per bridge Task, anchored to
  ``Employee.job_version_id``, attributed via ``employee_for_task``, carrying
  the SUM of the Task's measured ``DelegatedRun.cost`` values, with the
  ``("employee_bridge_task", task.id)`` provenance pair and the W5 audit row;
* honesty refusals: plain (non-bridge) Tasks 422; unmeasured cost 422 (I6:
  no measurement = no row, never ``amount = 0``); missing Task 404;
* idempotency: replay 409 ``cost_evidence_already_recorded``, still one row;
* attribution correctness: evidence survives rebinding (historical tasks keep
  pointing at the Employee that bore them);
* actor gate: non-owner 403;
* summary projection: count/total/rows, missing Employee 404;
* API: 201 / 409 / 422 / 404 / 200 over the two endpoints.

Helpers mirror ``tests/test_employee_bridge.py`` so this file is
self-contained.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.actor import ActorContext
from aios.audit import AuditLog
from aios.db import get_database_url, get_engine, run_migrations
from aios.employee_bridge import assign_work_to_employee, replace_employee_agent
from aios.employee_cost import (
    SOURCE_EVENT_TYPE,
    employee_cost_summary,
    record_task_cost_evidence,
)
from aios.models import (
    CostEvidence,
    DelegatedRun,
    DelegationMode,
    Project,
    Task,
)
from aios.schemas import EmployeeWorkCreate, TaskCreate
from aios.services import ServiceError, create_task
from aios.workforce import (
    compute_match,
    create_business_goal,
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

OWNER = ActorContext(kind="owner", owner_id="owner")
AGENT_ACTOR = ActorContext(kind="agent", agent_id="agent-1")


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors test_employee_bridge.py)
# ---------------------------------------------------------------------------


def _db(url: str) -> Session:
    run_migrations(url)
    return Session(get_engine(url))


def _seed_capability(session: Session, name: str):
    from aios.models import Capability

    cap = Capability(name=name, description=f"{name} capability")
    session.add(cap)
    session.commit()
    return cap


def _seed_agent(session: Session, name: str, capabilities: dict[str, int] | None = None):
    from aios.models import AdapterType, Agent, AgentCapability, Capability

    agent = Agent(name=name, role=name, adapter_type=AdapterType.EXTERNAL)
    session.add(agent)
    session.flush()
    for cap_name, priority in (capabilities or {}).items():
        cap = session.exec(select(Capability).where(Capability.name == cap_name)).first()
        assert cap is not None, f"capability must be seeded first: {cap_name}"
        session.add(AgentCapability(agent_id=agent.id, capability_id=cap.id, priority=priority))
    session.commit()
    return agent


def _hire(session: Session, cap_name: str = "writing"):
    """The full W1-W4 chain ending in a promoted Employee (with initial binding)."""
    from aios.models import ApprovalStatus, JobVersion, TrialOutcome
    from aios.workforce import create_job as _cj

    _seed_capability(session, cap_name)
    _seed_agent(session, "A", {cap_name: 80})
    goal = create_business_goal(session, "增长北极星", target_outcome="新增注册 +20%")
    rw = create_required_work(session, goal.id, "公众号内容生产", rationale="内容带来自然注册")
    job = _cj(
        session,
        rw.id,
        "内容初稿研究员",
        role_summary="把选题做成初稿",
        capability_names=[cap_name],
    )
    session.commit()
    head = session.get(JobVersion, job.head_version_id)
    assert head is not None
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


def _run(
    session: Session, project: Project, task: Task, agent_id: str, cost: float
) -> DelegatedRun:
    run = DelegatedRun(
        project_id=project.id,
        task_id=task.id,
        agent_id=agent_id,
        delegation_mode=DelegationMode.REMOTE_API,
        idempotency_key=f"test:{uuid4().hex}",
        cost=cost,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _plain_task(session: Session, project: Project) -> Task:
    """A task with NO Workforce lineage (never routed through the bridge)."""
    task = create_task(
        session,
        TaskCreate(project_id=project.id, title="plain", description="no lineage"),
        f"plain:{uuid4().hex}",
        commit=True,
    )
    return task


def _evidence_rows(session: Session) -> list[CostEvidence]:
    return list(session.exec(select(CostEvidence)).all())


def _api_session() -> Session:
    return Session(get_engine(get_database_url()))


# ---------------------------------------------------------------------------
# Service: the closure happy path
# ---------------------------------------------------------------------------


def test_closure_records_one_evidence_row(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'happy.db').as_posix()}"
    with _db(url) as s:
        emp = _hire(s, "writing")
        project = _project(s)
        task = assign_work_to_employee(s, emp.id, _work(s, project),
                                       idempotency_key="w1", actor=OWNER)
        agent_id = task.assigned_agent_id
        _run(s, project, task, agent_id, cost=1.5)

        ce = record_task_cost_evidence(s, task_id=task.id, actor=OWNER)

        assert ce.employee_id == emp.id
        assert ce.job_version_id == emp.job_version_id  # W5 aggregation anchor
        assert ce.amount == 1.5  # measured, not estimated
        assert ce.source_event_type == SOURCE_EVENT_TYPE == "employee_bridge_task"
        assert ce.source_event_id == task.id
        assert ce.idempotency_key == f"employee_bridge_task:{task.id}"
        assert len(_evidence_rows(s)) == 1
        # The W5 writer's audit row exists (shared-savepoint proof).
        audits = list(
            s.exec(select(AuditLog).where(AuditLog.action == "cost_evidence.create")).all()
        )
        assert len(audits) == 1
        assert audits[0].resource_id == ce.id


def test_amount_sums_all_measured_runs(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'sum.db').as_posix()}"
    with _db(url) as s:
        emp = _hire(s, "writing")
        project = _project(s)
        task = assign_work_to_employee(s, emp.id, _work(s, project),
                                       idempotency_key="w1", actor=OWNER)
        agent_id = task.assigned_agent_id
        _run(s, project, task, agent_id, cost=1.25)
        _run(s, project, task, agent_id, cost=2.0)  # e.g. a retry attempt

        ce = record_task_cost_evidence(s, task_id=task.id, actor=OWNER)
        assert ce.amount == 3.25


# ---------------------------------------------------------------------------
# Honesty refusals
# ---------------------------------------------------------------------------


def test_plain_task_is_refused_422(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'plain.db').as_posix()}"
    with _db(url) as s:
        _hire(s, "writing")
        project = _project(s)
        task = _plain_task(s, project)

        with pytest.raises(ServiceError) as ei:
            record_task_cost_evidence(s, task_id=task.id, actor=OWNER)
        assert ei.value.status_code == 422
        assert "task_not_employee_attributable" in ei.value.detail
        assert _evidence_rows(s) == []  # nothing fabricated


def test_unmeasured_cost_is_refused_422(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'unmeasured.db').as_posix()}"
    with _db(url) as s:
        emp = _hire(s, "writing")
        project = _project(s)
        task = assign_work_to_employee(s, emp.id, _work(s, project),
                                       idempotency_key="w1", actor=OWNER)
        agent_id = task.assigned_agent_id
        _run(s, project, task, agent_id, cost=0.0)  # never reported a cost

        with pytest.raises(ServiceError) as ei:
            record_task_cost_evidence(s, task_id=task.id, actor=OWNER)
        assert ei.value.status_code == 422
        assert "no_measured_cost" in ei.value.detail
        assert _evidence_rows(s) == []  # I6: no measurement = no row, not 0


def test_missing_task_is_404(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'missing.db').as_posix()}"
    with _db(url) as s:
        _hire(s, "writing")
        with pytest.raises(ServiceError) as ei:
            record_task_cost_evidence(s, task_id="task_missing", actor=OWNER)
        assert ei.value.status_code == 404


def test_non_owner_actor_is_403(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'actor.db').as_posix()}"
    with _db(url) as s:
        with pytest.raises(ServiceError) as ei:
            record_task_cost_evidence(s, task_id="t", actor=AGENT_ACTOR)
        assert ei.value.status_code == 403


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_replay_is_409_at_most_once(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'replay.db').as_posix()}"
    with _db(url) as s:
        emp = _hire(s, "writing")
        project = _project(s)
        task = assign_work_to_employee(s, emp.id, _work(s, project),
                                       idempotency_key="w1", actor=OWNER)
        agent_id = task.assigned_agent_id
        _run(s, project, task, agent_id, cost=2.0)

        first = record_task_cost_evidence(s, task_id=task.id, actor=OWNER)
        with pytest.raises(ServiceError) as ei:
            record_task_cost_evidence(s, task_id=task.id, actor=OWNER)
        assert ei.value.status_code == 409
        assert "cost_evidence_already_recorded" in ei.value.detail

        rows = _evidence_rows(s)
        assert len(rows) == 1 and rows[0].id == first.id
        # The replayed audit did not land either (shared-savepoint rollback).
        audits = list(
            s.exec(select(AuditLog).where(AuditLog.action == "cost_evidence.create")).all()
        )
        assert len(audits) == 1


# ---------------------------------------------------------------------------
# Attribution correctness across rebinding
# ---------------------------------------------------------------------------


def test_evidence_survives_rebinding(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'rebind.db').as_posix()}"
    with _db(url) as s:
        emp = _hire(s, "writing")  # bound to agent A
        project = _project(s)
        task_before = assign_work_to_employee(s, emp.id, _work(s, project),
                                              idempotency_key="w1", actor=OWNER)
        other = _seed_agent(s, "B")
        replace_employee_agent(s, emp.id, other.id, actor=OWNER)
        task_after = assign_work_to_employee(s, emp.id, _work(s, project),
                                             idempotency_key="w2", actor=OWNER)

        for t in (task_before, task_after):
            _run(s, project, t, t.assigned_agent_id, cost=1.0)

        ce_before = record_task_cost_evidence(s, task_id=task_before.id, actor=OWNER)
        ce_after = record_task_cost_evidence(s, task_id=task_after.id, actor=OWNER)
        # BOTH tasks were borne by the SAME Employee -- the binding interval at
        # each Task's created_at resolves there, regardless of later rebinds.
        assert ce_before.employee_id == emp.id
        assert ce_after.employee_id == emp.id
        assert len(_evidence_rows(s)) == 2


# ---------------------------------------------------------------------------
# Summary projection
# ---------------------------------------------------------------------------


def test_summary_aggregates_an_employee(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'summary.db').as_posix()}"
    with _db(url) as s:
        emp = _hire(s, "writing")
        project = _project(s)
        total = 0.0
        for i in range(2):
            task = assign_work_to_employee(s, emp.id, _work(s, project),
                                           idempotency_key=f"w{i}", actor=OWNER)
            _run(s, project, task, task.assigned_agent_id, cost=1.5)
            record_task_cost_evidence(s, task_id=task.id, actor=OWNER)
            total += 1.5

        summary = employee_cost_summary(s, emp.id)
        assert summary["employee_id"] == emp.id
        assert summary["job_version_id"] == emp.job_version_id
        assert summary["evidence_count"] == 2
        assert summary["total_amount"] == total
        assert {r["source_event_id"] for r in summary["rows"]} == {
            r.source_event_id for r in _evidence_rows(s)
        }


def test_summary_missing_employee_404(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'summary404.db').as_posix()}"
    with _db(url) as s:
        with pytest.raises(ServiceError) as ei:
            employee_cost_summary(s, "emp_missing")
        assert ei.value.status_code == 404


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


def test_api_record_replay_and_summary(authenticated_client: TestClient) -> None:
    client = authenticated_client
    s = _api_session()
    emp = _hire(s, "writing")
    project = _project(s)
    emp_id, project_id = emp.id, project.id
    s.close()

    # Assign work through the bridge API (the only HTTP Task-creation path
    # that carries Workforce lineage).
    r = client.post(
        f"/employees/{emp_id}/work",
        json={"project_id": project_id, "title": "t", "description": "d", "output_schema": {}},
        headers={"Idempotency-Key": "cost-api-1"},
    )
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]

    # A measured run exists (seeded directly; delegation is out of scope here).
    s = _api_session()
    task = s.get(Task, task_id)
    _run(s, project, task, task.assigned_agent_id, cost=2.5)
    s.close()

    # 201 record.
    r = client.post(f"/tasks/{task_id}/cost-evidence")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["employee_id"] == emp_id
    assert body["amount"] == 2.5
    assert body["source_event_id"] == task_id

    # 409 replay.
    r = client.post(f"/tasks/{task_id}/cost-evidence")
    assert r.status_code == 409
    assert r.json()["detail"] == "cost_evidence_already_recorded"

    # 200 summary.
    r = client.get("/cost-evidence", params={"employee_id": emp_id})
    assert r.status_code == 200
    summary = r.json()
    assert summary["evidence_count"] == 1
    assert summary["total_amount"] == 2.5

    # Error mapping.
    assert client.post("/tasks/task_missing/cost-evidence").status_code == 404
    assert client.get("/cost-evidence", params={"employee_id": "emp_missing"}).status_code == 404


def test_api_plain_task_refused_422(authenticated_client: TestClient) -> None:
    client = authenticated_client
    s = _api_session()
    project = _project(s)
    task = _plain_task(s, project)
    task_id = task.id
    s.close()

    r = client.post(f"/tasks/{task_id}/cost-evidence")
    assert r.status_code == 422
    assert "task_not_employee_attributable" in r.json()["detail"]
