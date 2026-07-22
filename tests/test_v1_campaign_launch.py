from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import aios.campaign as campaign_mod
from aios.api.app import create_app
from aios.audit import AuditLog
from aios.campaign import route_task, seed_v1_agents
from aios.db import get_database_url, get_engine
from aios.models import Agent, Capability, Event, Project, Task, TaskStatus
from aios.orchestrator import Orchestrator, complete_task
from aios.services import ServiceError


@pytest.fixture
def client(trusted_owner_installer, tmp_path: Path, monkeypatch) -> TestClient:
    database_path = tmp_path / "v1_launch.db"
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    app = create_app()
    trusted_owner_installer(app)
    with TestClient(app) as test_client:
        yield test_client


def _launch(client, name, objective, idem=None):
    headers = {"Idempotency-Key": idem} if idem else {}
    return client.post(
        "/owner/campaigns",
        json={"name": name, "objective": objective},
        headers=headers,
    )


def test_owner_launch_happy_path_creates_project_and_nine_tasks(client: TestClient) -> None:
    resp = _launch(
        client,
        "把失败的 AI 系统重建成可用的小 AI 公司",
        "验证 AIOS 的端到端多 agent 协作。",
        idem="owner-1",
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()

    assert data["task_count"] == 9
    assert data["project_status"] == "proposed"
    assert "已经启动" in data["message"]
    assert "T1" in data["message"]

    keys = [t["key"] for t in data["tasks"]]
    assert keys == ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9"]

    by_key = {t["key"]: t for t in data["tasks"]}
    t1_id = by_key["T1"]["task_id"]
    t3_id = by_key["T3"]["task_id"]
    t4_id = by_key["T4"]["task_id"]
    t5_id = by_key["T5"]["task_id"]

    # Dependency wiring
    assert by_key["T2"]["depends_on"] == [t1_id]
    assert set(by_key["T6"]["depends_on"]) == {t3_id, t4_id, t5_id}
    assert by_key["T7"]["depends_on"] == [by_key["T6"]["task_id"]]
    assert by_key["T8"]["depends_on"] == [by_key["T7"]["task_id"]]
    assert by_key["T9"]["depends_on"] == [by_key["T6"]["task_id"]]

    # T1 is kicked off READY and routed to a department agent
    assert by_key["T1"]["status"] == "ready"
    assert by_key["T1"]["assigned_agent_id"] is not None

    # Owner-gate tasks are unassigned and remain backlog until human acts
    assert by_key["T6"]["assigned_agent_id"] is None
    assert by_key["T6"]["status"] == "backlog"
    assert by_key["T8"]["assigned_agent_id"] is None
    assert by_key["T8"]["status"] == "backlog"


def test_owner_launch_is_idempotent(client: TestClient) -> None:
    # A true replay: identical body + identical Idempotency-Key must return the SAME
    # project and the SAME nine tasks (no duplicates), not a 409.
    body = {"name": "campaign A", "objective": "objective A"}
    first = client.post("/owner/campaigns", json=body, headers={"Idempotency-Key": "dup-key"})
    assert first.status_code == 201, first.text
    second = client.post("/owner/campaigns", json=body, headers={"Idempotency-Key": "dup-key"})
    assert second.status_code == 201, second.text

    # Same project, no duplicate tasks
    assert second.json()["project_id"] == first.json()["project_id"]
    assert first.json()["task_count"] == 9
    assert second.json()["task_count"] == 9


def test_owner_launch_rejects_conflicting_duplicate_key(client: TestClient) -> None:
    # Same Idempotency-Key but a *different* body is a conflict (not a silent replay),
    # and the owner must get a readable Chinese message explaining what to do.
    first = client.post(
        "/owner/campaigns",
        json={"name": "campaign A", "objective": "objective A"},
        headers={"Idempotency-Key": "conflict-key"},
    )
    assert first.status_code == 201, first.text

    second = client.post(
        "/owner/campaigns",
        json={"name": "campaign A (changed title)", "objective": "objective A"},
        headers={"Idempotency-Key": "conflict-key"},
    )
    assert second.status_code == 409, second.text
    assert "提交标识" in second.json()["detail"]


def test_owner_launch_rejects_empty_goal_with_readable_message(client: TestClient) -> None:
    # Whitespace-only title / objective must produce a clear, owner-readable 400 (not a stack trace)
    resp_title = _launch(client, "   ", "objective", idem="bad-title")
    assert resp_title.status_code == 400
    assert "不能为空" in resp_title.json()["detail"]

    resp_objective = _launch(client, "real title", "  ", idem="bad-objective")
    assert resp_objective.status_code == 400
    assert "不能为空" in resp_objective.json()["detail"]


def test_dependency_orchestration_activates_downstream(client: TestClient) -> None:
    resp = _launch(client, "campaign B", "objective B", idem="orch-1")
    assert resp.status_code == 201
    data = resp.json()
    t1_id = next(t["task_id"] for t in data["tasks"] if t["key"] == "T1")
    t2_id = next(t["task_id"] for t in data["tasks"] if t["key"] == "T2")

    with Session(get_engine(get_database_url())) as session:
        complete_task(session, t1_id, "orch-complete-t1")
        Orchestrator(session).process_pending()

        t2 = session.get(Task, t2_id)
        assert t2.status == TaskStatus.READY

        # exactly one task should be READY after a single completion
        ready = list(session.exec(select(Task).where(Task.status == TaskStatus.READY)))
        assert {t.id for t in ready} == {t2_id}


def test_campaign_launch_is_atomic_on_midway_failure(client, monkeypatch) -> None:
    # Force task-graph creation to fail after T5 is built (midway through T1..T9).
    # With an atomic launch, NOTHING committed by this attempt may survive.
    real_create_task = campaign_mod.create_task

    def failing_create_task(session, request, idempotency_key, commit=True):
        if request.title.startswith("T6"):
            raise ServiceError(500, "simulated midway task-graph failure")
        return real_create_task(session, request, idempotency_key, commit=commit)

    monkeypatch.setattr(campaign_mod, "create_task", failing_create_task)

    resp = client.post(
        "/owner/campaigns",
        json={"name": "atomic test", "objective": "objective"},
        headers={"Idempotency-Key": "atomic-1"},
    )
    assert resp.status_code != 201, resp.text

    engine = get_engine(get_database_url())
    with Session(engine) as session:
        assert session.exec(select(Project)).all() == [], "partial Project leaked"
        assert session.exec(select(Task)).all() == [], "partial Task leaked"
        assert session.exec(select(Event)).all() == [], "idempotency/Event state leaked"
        assert session.exec(select(AuditLog)).all() == [], "AuditLog leaked"
        # Seeding is part of the same atomic transaction, so it rolls back too.
        assert session.exec(select(Capability)).all() == [], "seeded Capability leaked"
        assert session.exec(select(Agent)).all() == [], "seeded Agent leaked"


def test_sequential_launches_keep_seven_caps_six_agents(client) -> None:
    # Two full launches must share the seed (no duplicate capabilities/agents),
    # while still creating two distinct projects + 18 tasks.
    _launch(client, "campaign one", "objective one", idem="seq-1")
    _launch(client, "campaign two", "objective two", idem="seq-2")

    engine = get_engine(get_database_url())
    with Session(engine) as session:
        assert len(session.exec(select(Capability)).all()) == 7, "capabilities duplicated"
        assert len(session.exec(select(Agent)).all()) == 6, "department agents duplicated"
        assert len(session.exec(select(Project)).all()) == 2
        assert len(session.exec(select(Task)).all()) == 18


def test_concurrent_seed_is_safe(tmp_path, monkeypatch) -> None:
    # Force the check-then-insert race window on the first capability so two launches
    # overlap on the seed. The race-safe upsert must yield exactly 7 capabilities and
    # 6 agents with no crash and no duplicates.

    from aios.db import run_migrations

    database_url = f"sqlite:///{(tmp_path / 'concurrent_seed.db').as_posix()}"
    monkeypatch.setenv("AIOS_DATABASE_URL", database_url)
    engine = get_engine(database_url)
    run_migrations()

    barrier = threading.Barrier(2)
    errors: list[str] = []

    def worker() -> None:
        try:
            session = Session(engine)
            seed_v1_agents(session, commit=True, _probe=lambda: barrier.wait())
            session.close()
        except Exception as exc:  # noqa: BLE001 - captured to assert "no errors"
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"concurrent seed raised errors: {errors}"
    with Session(engine) as session:
        caps = session.exec(select(Capability)).all()
        agents = session.exec(select(Agent)).all()
        assert len(caps) == 7, "capabilities duplicated or missing"
        assert len(agents) == 6, "department agents duplicated or missing"


def test_fixed_routing_is_deterministic_for_department_tasks(client) -> None:
    # After T1 completes and T2 becomes READY, FIXED routing must deterministically
    # assign T2 to the Positioning agent (not best-available / fallback).
    resp = _launch(client, "routing test", "objective", idem="route-1")
    assert resp.status_code == 201
    data = resp.json()
    t1_id = next(t["task_id"] for t in data["tasks"] if t["key"] == "T1")
    t2_id = next(t["task_id"] for t in data["tasks"] if t["key"] == "T2")

    engine = get_engine(get_database_url())
    with Session(engine) as session:
        complete_task(session, t1_id, "complete-t1")
        Orchestrator(session).process_pending()
        t2 = session.get(Task, t2_id)
        assert t2.status == TaskStatus.READY

        positioning = session.exec(select(Agent).where(Agent.role == "positioning")).one()
        assignment = route_task(session, t2_id, "route-t2", commit=True)
        assert assignment is not None
        assert assignment.selected_agent_id == positioning.id
        assert assignment.routing_reason == "fixed_agent"
        assert session.get(Task, t2_id).assigned_agent_id == positioning.id


def test_owner_gates_remain_manual_until_explicit_action(client) -> None:
    # T6 (owner review) and T8 (publish gate) are MANUAL. Nothing in the system may
    # auto-assign them; routing must return None and leave them unassigned + READY.
    resp = _launch(client, "gate test", "objective", idem="gate-1")
    assert resp.status_code == 201
    ids = {t["key"]: t["task_id"] for t in resp.json()["tasks"]}

    engine = get_engine(get_database_url())
    with Session(engine) as session:
        # Drive T1->T2->T3 and T4,T5 so T6 becomes READY.
        for key, idem in [("T1", "c1"), ("T2", "c2"), ("T3", "c3"), ("T4", "c4"), ("T5", "c5")]:
            complete_task(session, ids[key], idem)
            Orchestrator(session).process_pending()
        t6 = session.get(Task, ids["T6"])
        assert t6.status == TaskStatus.READY

        assert route_task(session, ids["T6"], "route-t6", commit=True) is None
        t6_after = session.get(Task, ids["T6"])
        assert t6_after.assigned_agent_id is None
        assert t6_after.status == TaskStatus.READY

        # Simulate the owner approving T6, then T7, so T8 becomes READY.
        complete_task(session, ids["T6"], "c6")
        Orchestrator(session).process_pending()
        complete_task(session, ids["T7"], "c7")
        Orchestrator(session).process_pending()
        t8 = session.get(Task, ids["T8"])
        assert t8.status == TaskStatus.READY

        assert route_task(session, ids["T8"], "route-t8", commit=True) is None
        assert session.get(Task, ids["T8"]).assigned_agent_id is None
