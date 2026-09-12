"""V1 campaign department tasks route by CAPABILITY (BEST_AVAILABLE), not by name.

Why this file exists
--------------------
``launch_campaign`` used to persist department tasks (T1/T2/T3/T4/T5/T7/T9) as
``RoutingMode.FIXED``, which in ``scheduler.route_task`` resolves to
``task.assigned_agent_id`` with ``check_capabilities=False`` -- i.e. the
department hint is taken on faith and the required capabilities are never
verified. That makes the V1 graph unable to ever select an external / real
agent: capability routing is declared on every task but never exercised.

Switching the launch to ``BEST_AVAILABLE`` makes the declared capability set the
actual routing authority. These tests pin that contract down:

1. The graph is *launched* with ``best_available`` (owner gates stay ``manual``).
2. On a clean (seed-only) database every department task still lands on the SAME
   agent as before -- the capability -> agent mapping is 1:1, so the switch is
   behaviour-preserving. This is the regression guard.
3. The switch actually buys something: an external agent holding the same
   capability at a HIGHER ``AgentCapability.priority`` now wins the task. Under
   FIXED that was impossible by construction.
4. The new failure mode is explicit: a department task that declares NO
   capabilities is blocked with ``required_capabilities_missing`` instead of
   silently falling back to the department agent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.api.app import create_app
from aios.audit import AuditLog
from aios.campaign import route_task
from aios.db import get_database_url, get_engine
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    AgentStatus,
    Capability,
    Task,
    TaskStatus,
)
from aios.orchestrator import Orchestrator, complete_task


@pytest.fixture
def client(trusted_owner_installer, tmp_path: Path, monkeypatch) -> TestClient:
    database_path = tmp_path / "v1_best_available.db"
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    app = create_app()
    trusted_owner_installer(app)
    with TestClient(app) as test_client:
        yield test_client


def _launch(client: TestClient, idem: str = "ba-1") -> dict:
    resp = client.post(
        "/owner/campaigns",
        json={"name": "best available 路由验证", "objective": "验证能力路由真正生效。"},
        headers={"Idempotency-Key": idem},
    )
    assert resp.status_code == 201, resp.text
    return {t["key"]: t["task_id"] for t in resp.json()["tasks"]}


def _register_external_agent(
    session: Session,
    *,
    agent_id: str,
    capability_id: str,
    priority: int,
    platform: str = "workbuddy",
) -> Agent:
    """Register a non-department agent that claims ``capability_id`` at ``priority``."""
    agent = Agent(
        id=agent_id,
        name=f"{agent_id} (external)",
        role=agent_id,
        adapter_type=AdapterType.EXTERNAL,
        capabilities=[capability_id],
        permissions=[],
        cost_policy={},
        enabled=True,
        limitations=[],
        status=AgentStatus.AVAILABLE,
        platform=platform,
    )
    session.add(agent)
    session.add(
        AgentCapability(
            agent_id=agent_id, capability_id=capability_id, priority=priority, enabled=True
        )
    )
    session.commit()
    return agent


def test_department_tasks_launch_as_best_available_gates_stay_manual(
    client: TestClient,
) -> None:
    ids = _launch(client)

    engine = get_engine(get_database_url())
    with Session(engine) as session:
        modes = {key: session.get(Task, task_id).routing_mode.value for key, task_id in ids.items()}

    assert modes == {
        "T1": "best_available",
        "T2": "best_available",
        "T3": "best_available",
        "T4": "best_available",
        "T5": "best_available",
        "T6": "manual",
        "T7": "best_available",
        "T8": "manual",
        "T9": "best_available",
    }


def test_all_department_tasks_route_to_their_capability_owner(client: TestClient) -> None:
    """Regression guard: on a seed-only DB the switch changes NO routing outcome.

    Each V1 capability is owned by exactly one department agent, so best-available
    ranking must reproduce the old FIXED assignment for every department task.
    """
    ids = _launch(client, idem="ba-owner")

    engine = get_engine(get_database_url())
    with Session(engine) as session:
        role_by_key = {
            "T1": "user_research",
            "T2": "positioning",
            "T3": "content_strategy",
            "T4": "content_strategy",
            "T5": "content_production",
            "T7": "content_strategy",
            "T9": "growth",
        }
        selected_by_key: dict[str, str] = {}

        def route(key: str) -> None:
            assignment = route_task(session, ids[key], f"ba-route-{key}", commit=True)
            assert assignment is not None, f"{key} 未被路由"
            assert assignment.routing_reason == "best_available_static_priority"
            selected_by_key[key] = assignment.selected_agent_id

        route("T1")

        complete_task(session, ids["T1"], "ba-c1")
        Orchestrator(session).process_pending()
        route("T2")

        complete_task(session, ids["T2"], "ba-c2")
        Orchestrator(session).process_pending()
        route("T3")

        complete_task(session, ids["T3"], "ba-c3")
        Orchestrator(session).process_pending()
        route("T4")
        route("T5")

        # T6/T8 are owner gates; completing them unlocks T7 and T9.
        complete_task(session, ids["T4"], "ba-c4")
        complete_task(session, ids["T5"], "ba-c5")
        Orchestrator(session).process_pending()
        complete_task(session, ids["T6"], "ba-c6")
        Orchestrator(session).process_pending()
        route("T7")
        route("T9")

        session.expire_all()
        for key, role in role_by_key.items():
            expected = session.exec(select(Agent).where(Agent.role == role)).one()
            assert selected_by_key[key] == expected.id, f"{key} 路由到了错误的 agent"
            # The persisted task now mirrors the capability-selected agent.
            assert session.get(Task, ids[key]).assigned_agent_id == expected.id


def test_higher_priority_external_agent_wins_over_department_agent(
    client: TestClient,
) -> None:
    """The point of the switch: capability routing can now pick a real agent.

    Under FIXED the department hint was absolute and ``check_capabilities`` was
    even skipped, so a better external agent could never be selected.
    """
    ids = _launch(client, idem="ba-external")

    engine = get_engine(get_database_url())
    with Session(engine) as session:
        positioning = session.exec(
            select(Capability).where(Capability.name == "positioning")
        ).one()
        external = _register_external_agent(
            session,
            agent_id="agt:external_positioning",
            capability_id=positioning.id,
            priority=90,  # department agents seed at the default 50
        )
        external_id = external.id

        complete_task(session, ids["T1"], "ba-ext-c1")
        Orchestrator(session).process_pending()

        assignment = route_task(session, ids["T2"], "ba-ext-route-t2", commit=True)

    assert assignment is not None
    assert assignment.selected_agent_id == external_id
    assert assignment.routing_reason == "best_available_static_priority"
    assert assignment.fallback_used is False


def test_department_task_without_capabilities_is_blocked(client: TestClient) -> None:
    """New explicit failure mode: no declared capability => no silent fallback.

    FIXED would have happily used the department agent. BEST_AVAILABLE refuses to
    guess, blocks the task and records the reason in the audit trail.
    """
    ids = _launch(client, idem="ba-nocaps")

    engine = get_engine(get_database_url())
    with Session(engine) as session:
        # T2 only becomes routable once T1 is done.
        complete_task(session, ids["T1"], "ba-nocaps-c1")
        Orchestrator(session).process_pending()

        t2 = session.get(Task, ids["T2"])
        assert t2.status == TaskStatus.READY
        t2.required_capabilities = []
        session.add(t2)
        session.commit()

        assert route_task(session, ids["T2"], "ba-nocaps-route", commit=True) is None

        # Still unrouted and still READY -- nothing was guessed, nothing was burnt.
        t2_after = session.get(Task, ids["T2"])
        assert t2_after.status == TaskStatus.READY

        blocked = session.exec(
            select(AuditLog).where(
                AuditLog.idempotency_key == "audit:ba-nocaps-route",
                AuditLog.action == "routing.blocked",
            )
        ).one()
    assert blocked.after_snapshot["routing_reason"] == "required_capabilities_missing"
    assert blocked.after_snapshot["selected_agent_id"] is None
