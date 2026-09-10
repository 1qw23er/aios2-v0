"""Capacity-aware Routing V1 -- PR-2 (scheduler wiring) integration tests.

These prove the wired behaviour in ``aios.scheduler.route_task``:

  * disabled  -> byte-for-byte identical to the pre-PR-2 scheduler;
  * enabled   -> soft capacity ordering layered AFTER ``_rank`` (capability
                 priority stays absolute), never a hard cap, never a second
                 authority, never a READY deadlock.

Mirrors the DB/agent/task setup of ``test_scheduler.py`` but drives the
``AIOS_CAPACITY_ROUTING`` env directly via ``monkeypatch``.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, select

from aios.audit import AuditLog
from aios.capacity_routing import CAPACITY_ROUTING_ENV
from aios.db import get_engine, run_migrations
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    AgentStatus,
    Capability,
    DelegatedRun,
    DelegatedRunStatus,
    DelegationMode,
    ExecutionAssignment,
    Project,
    RoutingMode,
    Task,
    TaskStatus,
    now_utc,
)
from aios.scheduler import route_task

# --- harness ----------------------------------------------------------------


def _database(tmp_path: Path, name: str) -> str:
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    run_migrations(url)
    return url


def _add_agent(
    session: Session,
    *,
    agent_id: str,
    status: AgentStatus = AgentStatus.AVAILABLE,
    adapter_type: AdapterType = AdapterType.API,
) -> Agent:
    agent = Agent(
        id=agent_id,
        name=agent_id,
        role="worker",
        adapter_type=adapter_type,
        status=status,
    )
    session.add(agent)
    return agent


def _add_profile(session: Session, agent: Agent, capability: Capability, priority: int) -> None:
    session.add(
        AgentCapability(agent_id=agent.id, capability_id=capability.id, priority=priority)
    )


def _set_capacity_env(monkeypatch: pytest.MonkeyPatch, raw: dict) -> None:
    monkeypatch.setenv(CAPACITY_ROUTING_ENV, json.dumps(raw))


def _clear_capacity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CAPACITY_ROUTING_ENV, raising=False)


def _load_project_task(session: Session) -> tuple[Project, Task]:
    """A throwaway project + task used only to anchor DelegatedRuns for load."""
    project = Project(name="load", objective="o")
    session.add(project)
    session.flush()
    task = Task(
        project_id=project.id,
        title="load",
        description="load",
        status=TaskStatus.BACKLOG,
        output_schema={"type": "object"},
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return project, task


def _seed_run(
    session: Session,
    *,
    project: Project,
    task: Task,
    agent_id: str | None,
    status: DelegatedRunStatus,
    key: str,
) -> None:
    session.add(
        DelegatedRun(
            project_id=project.id,
            task_id=task.id,
            agent_id=agent_id,
            delegation_mode=DelegationMode.REMOTE_API if agent_id else DelegationMode.LOCAL,
            idempotency_key=key,
            status=status,
        )
    )
    session.commit()


def _route_reason(session: Session, key: str) -> str:
    return session.exec(
        select(AuditLog).where(AuditLog.idempotency_key == f"audit:{key}")
    ).one().after_snapshot["routing_reason"]


# --- 1. disabled == pre-PR-2 byte-for-byte ---------------------------------


def test_disabled_routes_like_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_capacity_env(monkeypatch)
    url = _database(tmp_path, "disabled.db")
    with Session(get_engine(url)) as s:
        project = Project(name="P", objective="O")
        s.add(project)
        s.flush()
        cap = Capability(id="c", name="c")
        s.add(cap)
        s.flush()
        a = _add_agent(s, agent_id="agt_a")
        b = _add_agent(s, agent_id="agt_b")
        _add_profile(s, a, cap, 80)
        _add_profile(s, b, cap, 80)
        task = Task(
            project_id=project.id,
            title="t",
            description="d",
            status=TaskStatus.READY,
            required_capabilities=[cap.id],
            routing_mode=RoutingMode.BEST_AVAILABLE,
        )
        s.add(task)
        s.commit()

        assignment = route_task(s, task.id, "k")

        assert assignment is not None
        assert assignment.selected_agent_id == "agt_a"
        audit = s.exec(select(AuditLog).where(AuditLog.idempotency_key == "audit:k")).one()
        assert "capacity_routing_enabled" not in audit.after_snapshot
        assert "capacity_reason" not in audit.after_snapshot


def test_unset_env_is_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_capacity_env(monkeypatch)
    url = _database(tmp_path, "unset.db")
    with Session(get_engine(url)) as s:
        project = Project(name="P", objective="O")
        s.add(project)
        s.flush()
        cap = Capability(id="c", name="c")
        s.add(cap)
        s.flush()
        a = _add_agent(s, agent_id="agt_a")
        b = _add_agent(s, agent_id="agt_b")
        _add_profile(s, a, cap, 70)
        _add_profile(s, b, cap, 70)
        task = Task(
            project_id=project.id,
            title="t",
            description="d",
            status=TaskStatus.READY,
            required_capabilities=[cap.id],
            routing_mode=RoutingMode.BEST_AVAILABLE,
        )
        s.add(task)
        s.commit()

        assignment = route_task(s, task.id, "k")
        assert assignment.selected_agent_id == "agt_a"


def test_invalid_env_is_disabled_not_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CAPACITY_ROUTING_ENV, "{not valid json")
    url = _database(tmp_path, "invalid.db")
    with Session(get_engine(url)) as s:
        project = Project(name="P", objective="O")
        s.add(project)
        s.flush()
        cap = Capability(id="c", name="c")
        s.add(cap)
        s.flush()
        a = _add_agent(s, agent_id="agt_a")
        b = _add_agent(s, agent_id="agt_b")
        _add_profile(s, a, cap, 70)
        _add_profile(s, b, cap, 70)
        task = Task(
            project_id=project.id,
            title="t",
            description="d",
            status=TaskStatus.READY,
            required_capabilities=[cap.id],
            routing_mode=RoutingMode.BEST_AVAILABLE,
        )
        s.add(task)
        s.commit()

        assignment = route_task(s, task.id, "k")
        # disabled by invalid config -> pure priority/id tiebreak, no capacity fields.
        assert assignment.selected_agent_id == "agt_a"
        audit = s.exec(select(AuditLog).where(AuditLog.idempotency_key == "audit:k")).one()
        assert "capacity_routing_enabled" not in audit.after_snapshot


# --- 2. enabled: idle preferred over busy ----------------------------------


def test_enabled_idle_agent_preferred_over_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_capacity_env(monkeypatch, {"default_max_inflight": 10})
    url = _database(tmp_path, "idle.db")
    with Session(get_engine(url)) as s:
        project = Project(name="P", objective="O")
        s.add(project)
        s.flush()
        cap = Capability(id="c", name="c")
        s.add(cap)
        s.flush()
        busy = _add_agent(s, agent_id="agt_busy")
        idle = _add_agent(s, agent_id="agt_idle")
        _add_profile(s, busy, cap, 80)
        _add_profile(s, idle, cap, 80)
        load_p, load_t = _load_project_task(s)
        for i in range(3):
            _seed_run(s, project=load_p, task=load_t, agent_id=busy.id,
                      status=DelegatedRunStatus.SUBMITTED, key=f"b{i}")
        task = Task(
            project_id=project.id,
            title="t",
            description="d",
            status=TaskStatus.READY,
            required_capabilities=[cap.id],
            routing_mode=RoutingMode.BEST_AVAILABLE,
        )
        s.add(task)
        s.commit()

        assignment = route_task(s, task.id, "k")
        assert assignment.selected_agent_id == idle.id


# --- 3. capability priority is absolute ------------------------------------


def test_capability_priority_beats_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_capacity_env(monkeypatch, {"default_max_inflight": 100})
    url = _database(tmp_path, "cap.db")
    with Session(get_engine(url)) as s:
        project = Project(name="P", objective="O")
        s.add(project)
        s.flush()
        cap = Capability(id="c", name="c")
        s.add(cap)
        s.flush()
        high = _add_agent(s, agent_id="agt_high")
        low = _add_agent(s, agent_id="agt_low")
        _add_profile(s, high, cap, 100)
        _add_profile(s, low, cap, 50)
        load_p, load_t = _load_project_task(s)
        for i in range(5):
            _seed_run(s, project=load_p, task=load_t, agent_id=high.id,
                      status=DelegatedRunStatus.SUBMITTED, key=f"h{i}")
        task = Task(
            project_id=project.id,
            title="t",
            description="d",
            status=TaskStatus.READY,
            required_capabilities=[cap.id],
            routing_mode=RoutingMode.BEST_AVAILABLE,
        )
        s.add(task)
        s.commit()

        assignment = route_task(s, task.id, "k")
        # Higher priority must win even when saturated.
        assert assignment.selected_agent_id == high.id
        assert _route_reason(s, "k") == "best_available_static_priority"


# --- 4. same capability tier ordered by in_flight -------------------------


def test_same_capability_ordered_by_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_capacity_env(monkeypatch, {"default_max_inflight": 100})
    url = _database(tmp_path, "order.db")
    with Session(get_engine(url)) as s:
        project = Project(name="P", objective="O")
        s.add(project)
        s.flush()
        cap = Capability(id="c", name="c")
        s.add(cap)
        s.flush()
        a3 = _add_agent(s, agent_id="agt_3")  # in_flight 3
        a1 = _add_agent(s, agent_id="agt_1")  # in_flight 1
        a0 = _add_agent(s, agent_id="agt_0")  # in_flight 0
        for ag, load in ((a3, 3), (a1, 1), (a0, 0)):
            _add_profile(s, ag, cap, 80)
            if load:
                load_p, load_t = _load_project_task(s)
                for i in range(load):
                    _seed_run(s, project=load_p, task=load_t, agent_id=ag.id,
                              status=DelegatedRunStatus.RUNNING, key=f"{ag.id}-{i}")
        task = Task(
            project_id=project.id,
            title="t",
            description="d",
            status=TaskStatus.READY,
            required_capabilities=[cap.id],
            routing_mode=RoutingMode.BEST_AVAILABLE,
        )
        s.add(task)
        s.commit()

        assignment = route_task(s, task.id, "k")
        assert assignment.selected_agent_id == a0.id


# --- 5/6/7. saturated still selected, never a READY deadlock --------------


def test_all_saturated_still_selected_no_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_capacity_env(monkeypatch, {"default_max_inflight": 1})
    url = _database(tmp_path, "sat.db")
    with Session(get_engine(url)) as s:
        project = Project(name="P", objective="O")
        s.add(project)
        s.flush()
        cap = Capability(id="c", name="c")
        s.add(cap)
        s.flush()
        x = _add_agent(s, agent_id="agt_x")
        y = _add_agent(s, agent_id="agt_y")
        _add_profile(s, x, cap, 80)
        _add_profile(s, y, cap, 80)
        for ag in (x, y):
            load_p, load_t = _load_project_task(s)
            _seed_run(s, project=load_p, task=load_t, agent_id=ag.id,
                      status=DelegatedRunStatus.SUBMITTED, key=f"{ag.id}-1")
        task = Task(
            project_id=project.id,
            title="t",
            description="d",
            status=TaskStatus.READY,
            required_capabilities=[cap.id],
            routing_mode=RoutingMode.BEST_AVAILABLE,
        )
        s.add(task)
        s.commit()

        # Must return an assignment (NOT leave the task stuck in READY).
        assignment = route_task(s, task.id, "k")
        assert assignment is not None
        assert _route_reason(s, "k") == "best_available_capacity_saturated"


# --- 8. agent_id deterministic tie-break with capacity ---------------------


def test_agent_id_tiebreak_with_capacity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_capacity_env(monkeypatch, {"default_max_inflight": 50})
    url = _database(tmp_path, "tie.db")
    with Session(get_engine(url)) as s:
        project = Project(name="P", objective="O")
        s.add(project)
        s.flush()
        cap = Capability(id="c", name="c")
        s.add(cap)
        s.flush()
        b = _add_agent(s, agent_id="agt_b")
        a = _add_agent(s, agent_id="agt_a")
        _add_profile(s, b, cap, 80)
        _add_profile(s, a, cap, 80)
        task = Task(
            project_id=project.id,
            title="t",
            description="d",
            status=TaskStatus.READY,
            required_capabilities=[cap.id],
            routing_mode=RoutingMode.BEST_AVAILABLE,
        )
        s.add(task)
        s.commit()

        assignment = route_task(s, task.id, "k")
        assert assignment.selected_agent_id == a.id


# --- 9. only in-flight statuses counted (INFLIGHT reuse) -------------------


def test_only_inflight_statuses_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_capacity_env(monkeypatch, {"default_max_inflight": 1})
    url = _database(tmp_path, "inflight.db")
    with Session(get_engine(url)) as s:
        project = Project(name="P", objective="O")
        s.add(project)
        s.flush()
        cap = Capability(id="c", name="c")
        s.add(cap)
        s.flush()
        target = _add_agent(s, agent_id="agt_target")  # 1 SUBMITTED + 1 SUCCEEDED -> 1 in-flight
        idle = _add_agent(s, agent_id="agt_idle")      # 0 in-flight
        _add_profile(s, target, cap, 80)
        _add_profile(s, idle, cap, 80)
        load_p, load_t = _load_project_task(s)
        _seed_run(s, project=load_p, task=load_t, agent_id=target.id,
                  status=DelegatedRunStatus.SUBMITTED, key="sub")
        _seed_run(s, project=load_p, task=load_t, agent_id=target.id,
                  status=DelegatedRunStatus.SUCCEEDED, key="ok")
        task = Task(
            project_id=project.id,
            title="t",
            description="d",
            status=TaskStatus.READY,
            required_capabilities=[cap.id],
            routing_mode=RoutingMode.BEST_AVAILABLE,
        )
        s.add(task)
        s.commit()

        assignment = route_task(s, task.id, "k")
        # target is saturated (1 in-flight == limit 1); idle wins.
        assert assignment.selected_agent_id == idle.id


# --- 10. heartbeat is NOT a capacity signal --------------------------------


def test_heartbeat_not_used_as_capacity_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_capacity_env(monkeypatch, {"default_max_inflight": 50})
    url = _database(tmp_path, "hb.db")
    with Session(get_engine(url)) as s:
        project = Project(name="P", objective="O")
        s.add(project)
        s.flush()
        cap = Capability(id="c", name="c")
        s.add(cap)
        s.flush()
        stale = _add_agent(s, agent_id="agt_stale")   # older heartbeat, still within threshold
        fresh = _add_agent(s, agent_id="agt_fresh")   # fresh heartbeat
        _add_profile(s, stale, cap, 80)
        _add_profile(s, fresh, cap, 80)
        # Both eligible (age < stale threshold). Differing heartbeats must NOT
        # affect ordering -- only in_flight (both 0) and agent_id matter.
        stale.last_heartbeat_at = now_utc() - timedelta(seconds=100)
        fresh.last_heartbeat_at = now_utc()
        s.commit()
        task = Task(
            project_id=project.id,
            title="t",
            description="d",
            status=TaskStatus.READY,
            required_capabilities=[cap.id],
            routing_mode=RoutingMode.BEST_AVAILABLE,
        )
        s.add(task)
        s.commit()

        assignment = route_task(s, task.id, "k")
        # Determined by agent_id tie-break ("agt_fresh" < "agt_stale"), NOT heartbeat.
        assert assignment.selected_agent_id == fresh.id


# --- 11. Task lease is NOT a capacity/routing signal -----------------------


def test_task_lease_not_used_as_signal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_capacity_env(monkeypatch, {"default_max_inflight": 50})
    url = _database(tmp_path, "lease.db")
    with Session(get_engine(url)) as s:
        project = Project(name="P", objective="O")
        s.add(project)
        s.flush()
        cap = Capability(id="c", name="c")
        s.add(cap)
        s.flush()
        a = _add_agent(s, agent_id="agt_a")
        b = _add_agent(s, agent_id="agt_b")
        _add_profile(s, a, cap, 80)
        _add_profile(s, b, cap, 80)
        task = Task(
            project_id=project.id,
            title="t",
            description="d",
            status=TaskStatus.READY,
            required_capabilities=[cap.id],
            routing_mode=RoutingMode.BEST_AVAILABLE,
            # A stale lease on the routed task must NOT displace routing.
            lease_owner="some-other-runtime",
            lease_expires_at=now_utc() - timedelta(hours=1),
        )
        s.add(task)
        s.commit()

        assignment = route_task(s, task.id, "k")
        assert assignment.selected_agent_id == a.id


# --- 12. Task.status == RUNNING is NOT a capacity input ---------------------


def test_task_running_status_not_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_capacity_env(monkeypatch, {"default_max_inflight": 1})
    url = _database(tmp_path, "running.db")
    with Session(get_engine(url)) as s:
        project = Project(name="P", objective="O")
        s.add(project)
        s.flush()
        cap = Capability(id="c", name="c")
        s.add(cap)
        s.flush()
        target = _add_agent(s, agent_id="agt_target")  # 1 SUBMITTED -> saturated
        idle = _add_agent(s, agent_id="agt_idle")
        _add_profile(s, target, cap, 80)
        _add_profile(s, idle, cap, 80)
        load_p, load_t = _load_project_task(s)
        _seed_run(s, project=load_p, task=load_t, agent_id=target.id,
                  status=DelegatedRunStatus.SUBMITTED, key="sub")
        # An orphan RUNNING Task tied to target must NOT inflate its capacity:
        # capacity is sourced from DelegatedRun only.
        orphan = Task(
            project_id=project.id,
            title="orphan",
            description="orphan",
            status=TaskStatus.RUNNING,
            assigned_agent_id=target.id,
            lease_owner="rt",
            lease_expires_at=now_utc() + timedelta(hours=1),
        )
        s.add(orphan)
        s.commit()
        task = Task(
            project_id=project.id,
            title="t",
            description="d",
            status=TaskStatus.READY,
            required_capabilities=[cap.id],
            routing_mode=RoutingMode.BEST_AVAILABLE,
        )
        s.add(task)
        s.commit()

        assignment = route_task(s, task.id, "k")
        # target saturated (1); orphan RUNNING Task did not add load -> idle wins.
        assert assignment.selected_agent_id == idle.id


# --- 13. cost does NOT enter routing ---------------------------------------


def test_estimated_cost_does_not_affect_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_capacity_env(monkeypatch, {"default_max_inflight": 50})
    url = _database(tmp_path, "cost.db")
    with Session(get_engine(url)) as s:
        project = Project(name="P", objective="O")
        s.add(project)
        s.flush()
        cap = Capability(id="c", name="c")
        s.add(cap)
        s.flush()
        a = _add_agent(s, agent_id="agt_a")
        b = _add_agent(s, agent_id="agt_b")
        _add_profile(s, a, cap, 80)
        _add_profile(s, b, cap, 80)
        task = Task(
            project_id=project.id,
            title="t",
            description="d",
            status=TaskStatus.READY,
            required_capabilities=[cap.id],
            routing_mode=RoutingMode.BEST_AVAILABLE,
            estimated_cost=999.0,  # large cost must be irrelevant to routing
        )
        s.add(task)
        s.commit()

        assignment = route_task(s, task.id, "k")
        # Still decided by agent_id tie-break; cost ignored.
        assert assignment.selected_agent_id == a.id


# --- 14. preferred agent unaffected by capacity ----------------------------


def test_preferred_agent_not_displaced_by_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_capacity_env(monkeypatch, {"default_max_inflight": 1})
    url = _database(tmp_path, "pref.db")
    with Session(get_engine(url)) as s:
        project = Project(name="P", objective="O")
        s.add(project)
        s.flush()
        cap = Capability(id="c", name="c")
        s.add(cap)
        s.flush()
        preferred = _add_agent(s, agent_id="agt_pref")
        fallback = _add_agent(s, agent_id="agt_fb")
        _add_profile(s, preferred, cap, 80)
        _add_profile(s, fallback, cap, 80)
        # preferred is saturated; capacity must NOT displace it.
        load_p, load_t = _load_project_task(s)
        _seed_run(s, project=load_p, task=load_t, agent_id=preferred.id,
                  status=DelegatedRunStatus.SUBMITTED, key="p1")
        task = Task(
            project_id=project.id,
            title="t",
            description="d",
            status=TaskStatus.READY,
            preferred_agent_id=preferred.id,
            required_capabilities=[cap.id],
            routing_mode=RoutingMode.PREFERRED_WITH_FALLBACK,
        )
        s.add(task)
        s.commit()

        assignment = route_task(s, task.id, "k")
        assert assignment.selected_agent_id == preferred.id
        assert assignment.fallback_used is False
        assert _route_reason(s, "k") == "preferred_agent"


# --- 15. FIXED workforce path unaffected ------------------------------------


def test_fixed_routing_ignores_capacity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_capacity_env(monkeypatch, {"default_max_inflight": 1})
    url = _database(tmp_path, "fixed.db")
    with Session(get_engine(url)) as s:
        project = Project(name="P", objective="O")
        s.add(project)
        s.flush()
        cap = Capability(id="c", name="c")
        s.add(cap)
        s.flush()
        fixed = _add_agent(s, agent_id="agt_fixed")
        _add_profile(s, fixed, cap, 80)
        # fixed agent saturated; FIXED mode must not consult capacity.
        load_p, load_t = _load_project_task(s)
        _seed_run(s, project=load_p, task=load_t, agent_id=fixed.id,
                  status=DelegatedRunStatus.SUBMITTED, key="f1")
        task = Task(
            project_id=project.id,
            title="t",
            description="d",
            status=TaskStatus.READY,
            assigned_agent_id=fixed.id,
            routing_mode=RoutingMode.FIXED,
        )
        s.add(task)
        s.commit()

        assignment = route_task(s, task.id, "k")
        assert assignment is not None
        assert assignment.selected_agent_id == fixed.id
        audit = s.exec(select(AuditLog).where(AuditLog.idempotency_key == "audit:k")).one()
        # FIXED never wires capacity.
        assert "capacity_routing_enabled" not in audit.after_snapshot


# --- 16. execution authority (claim) unchanged ----------------------------


def test_execution_assignment_created_with_idempotency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_capacity_env(monkeypatch)
    url = _database(tmp_path, "auth.db")
    with Session(get_engine(url)) as s:
        project = Project(name="P", objective="O")
        s.add(project)
        s.flush()
        cap = Capability(id="c", name="c")
        s.add(cap)
        s.flush()
        a = _add_agent(s, agent_id="agt_a")
        _add_profile(s, a, cap, 80)
        task = Task(
            project_id=project.id,
            title="t",
            description="d",
            status=TaskStatus.READY,
            required_capabilities=[cap.id],
            routing_mode=RoutingMode.BEST_AVAILABLE,
        )
        s.add(task)
        s.commit()

        assignment = route_task(s, task.id, "k")
        assert assignment is not None
        assert assignment.idempotency_key == "k"
        assert assignment.selected_agent_id == a.id
        # Replay returns the SAME assignment (at-most-once claim intact).
        replay = route_task(s, task.id, "k")
        assert replay is not None and replay.id == assignment.id
        assert len(list(s.exec(select(ExecutionAssignment)))) == 1


# --- 17. audit records capacity decision -----------------------------------


def test_audit_records_capacity_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_capacity_env(monkeypatch, {"default_max_inflight": 10})
    url = _database(tmp_path, "audit.db")
    with Session(get_engine(url)) as s:
        project = Project(name="P", objective="O")
        s.add(project)
        s.flush()
        cap = Capability(id="c", name="c")
        s.add(cap)
        s.flush()
        busy = _add_agent(s, agent_id="agt_busy")
        idle = _add_agent(s, agent_id="agt_idle")
        _add_profile(s, busy, cap, 80)
        _add_profile(s, idle, cap, 80)
        load_p, load_t = _load_project_task(s)
        _seed_run(s, project=load_p, task=load_t, agent_id=busy.id,
                  status=DelegatedRunStatus.RUNNING, key="b1")
        task = Task(
            project_id=project.id,
            title="t",
            description="d",
            status=TaskStatus.READY,
            required_capabilities=[cap.id],
            routing_mode=RoutingMode.BEST_AVAILABLE,
        )
        s.add(task)
        s.commit()

        assignment = route_task(s, task.id, "k")
        assert assignment.selected_agent_id == idle.id
        audit = s.exec(select(AuditLog).where(AuditLog.idempotency_key == "audit:k")).one()
        after = audit.after_snapshot
        assert after["capacity_routing_enabled"] is True
        assert after["capacity_reason"] == "ordered_by_capacity_within_capability_tier"
        for candidate in after["considered_candidates"]:
            assert "in_flight" in candidate
            assert "saturated" in candidate
            assert "capacity_limit" in candidate


# --- 18. deterministic for a given DB state + config -----------------------


def test_capacity_routing_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_capacity_env(monkeypatch, {"default_max_inflight": 100})

    def _setup(name: str) -> str:
        url = _database(tmp_path, name)
        with Session(get_engine(url)) as s:
            project = Project(name="P", objective="O")
            s.add(project)
            s.flush()
            cap = Capability(id="c", name="c")
            s.add(cap)
            s.flush()
            a = _add_agent(s, agent_id="agt_a")  # in_flight 3
            b = _add_agent(s, agent_id="agt_b")  # in_flight 1
            _add_profile(s, a, cap, 80)
            _add_profile(s, b, cap, 80)
            load_p, load_t = _load_project_task(s)
            for i in range(3):
                _seed_run(s, project=load_p, task=load_t, agent_id=a.id,
                          status=DelegatedRunStatus.SUBMITTED, key=f"a{i}")
            for i in range(1):
                _seed_run(s, project=load_p, task=load_t, agent_id=b.id,
                          status=DelegatedRunStatus.SUBMITTED, key=f"b{i}")
            task = Task(
                project_id=project.id,
                title="t",
                description="d",
                status=TaskStatus.READY,
                required_capabilities=[cap.id],
                routing_mode=RoutingMode.BEST_AVAILABLE,
            )
            s.add(task)
            s.commit()
        return url

    url1 = _setup("det1.db")
    url2 = _setup("det2.db")
    with Session(get_engine(url1)) as s1, Session(get_engine(url2)) as s2:
        t1 = s1.exec(select(Task).where(Task.status == TaskStatus.READY)).one()
        t2 = s2.exec(select(Task).where(Task.status == TaskStatus.READY)).one()
        a1 = route_task(s1, t1.id, "k")
        a2 = route_task(s2, t2.id, "k")
    assert a1.selected_agent_id == a2.selected_agent_id == "agt_b"
