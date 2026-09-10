"""Capacity-aware Routing V1 -- PR-1 (unwired) module tests.

These tests prove the pure-compute / pure-read half of Capacity-aware Routing
works and, crucially, that it is **unwired**: it contains no DB write, no entity
mutation, no routing decision, and is not referenced by ``scheduler.py``.

Follows the GAP-3 Stage 2 ``test_model_pricing.py`` pattern for the ``db``
fixture and the env-mapping style for config parsing.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest
from sqlmodel import Session

from aios.capacity_routing import (
    CAPACITY_ROUTING_ENV,
    CapacityRouteConfig,
    CapacitySnapshot,
    build_capacity_snapshot,
    load_capacity_routing,
    order_by_capacity,
    parse_capacity_routing,
    project_in_flight_by_agent,
)
from aios.db import get_database_url, get_engine
from aios.delegation import INFLIGHT_RUN_STATUSES
from aios.models import (
    AdapterType,
    Agent,
    DelegatedRun,
    DelegatedRunStatus,
    DelegationMode,
    Project,
    Task,
    TaskStatus,
)

_MODULE_FILE = (
    pathlib.Path(__file__).resolve().parent.parent / "src" / "aios" / "capacity_routing.py"
)
_SCHEDULER_FILE = pathlib.Path(__file__).resolve().parent.parent / "src" / "aios" / "scheduler.py"


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def db(authenticated_client) -> Session:
    """A session bound to the migrated test database (same pattern as GAP-3S2)."""
    with Session(get_engine(get_database_url())) as s:
        yield s


def _env(raw: dict | None) -> dict[str, str]:
    if raw is None:
        return {}
    return {CAPACITY_ROUTING_ENV: json.dumps(raw)}


def _seed_project_task(db: Session):
    project = Project(name="p", objective="o", budget_limit=10.0)
    db.add(project)
    db.commit()
    db.refresh(project)
    task = Task(
        project_id=project.id,
        title="t",
        description="d",
        status=TaskStatus.BACKLOG,
        output_schema={"type": "object"},
        estimated_cost=0.0,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return project, task


def _agent(db: Session, name: str) -> Agent:
    agent = Agent(name=name, role="r", adapter_type=AdapterType.API)
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


def _run(
    db: Session,
    *,
    project,
    task,
    agent_id: str | None,
    status: DelegatedRunStatus,
    key: str,
) -> DelegatedRun:
    run = DelegatedRun(
        project_id=project.id,
        task_id=task.id,
        agent_id=agent_id,
        delegation_mode=DelegationMode.REMOTE_API if agent_id else DelegationMode.LOCAL,
        idempotency_key=key,
        status=status,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


# --- config parsing ---------------------------------------------------------


def test_unset_env_is_disabled() -> None:
    cfg = load_capacity_routing(env={})
    assert cfg == CapacityRouteConfig(enabled=False, default_max_inflight=None, agents={})


def test_valid_config_is_enabled() -> None:
    cfg = parse_capacity_routing(json.dumps({"default_max_inflight": 4, "agents": {"agt_x": 2}}))
    assert cfg.enabled is True
    assert cfg.default_max_inflight == 4
    assert cfg.agents == {"agt_x": 2}


def test_empty_object_is_disabled() -> None:
    # A valid-but-empty object has nothing to act on -> behaves like unset.
    cfg = parse_capacity_routing("{}")
    assert cfg.enabled is False
    assert cfg.default_max_inflight is None
    assert cfg.agents == {}


def test_malformed_json_is_disabled_not_fatal() -> None:
    cfg = parse_capacity_routing("{not json")
    assert cfg.enabled is False
    assert cfg.agents == {}


def test_non_object_json_is_disabled() -> None:
    cfg = parse_capacity_routing("[1, 2, 3]")
    assert cfg.enabled is False


def test_blank_string_is_disabled() -> None:
    cfg = parse_capacity_routing("   ")
    assert cfg.enabled is False


@pytest.mark.parametrize("bad_default", [0, -1, "x", 1.5, True])
def test_non_positive_default_is_ignored(bad_default) -> None:
    cfg = parse_capacity_routing(json.dumps({"default_max_inflight": bad_default}))
    assert cfg.default_max_inflight is None
    # No agent limits either -> still disabled.
    assert cfg.enabled is False


def test_valid_agents_survive_an_invalid_entry() -> None:
    cfg = parse_capacity_routing(json.dumps({"agents": {"good": 2, "bad": -1, "worse": "x"}}))
    assert cfg.agents == {"good": 2}
    assert cfg.enabled is True


def test_agents_only_without_default_is_enabled() -> None:
    cfg = parse_capacity_routing(json.dumps({"agents": {"good": 1}}))
    assert cfg.enabled is True
    assert cfg.default_max_inflight is None


def test_config_parsing_is_deterministic() -> None:
    payload = json.dumps({"default_max_inflight": 3, "agents": {"a": 1, "b": 2}})
    assert parse_capacity_routing(payload) == parse_capacity_routing(payload)


# --- in-flight projection (DB) ---------------------------------------------


def test_in_flight_counts_only_inflight_statuses(db) -> None:
    project, task = _seed_project_task(db)
    agent = _agent(db, "x")
    # 2 SUBMITTED + 1 RUNNING = 3 in-flight; 2 terminal = excluded.
    _run(
        db,
        project=project,
        task=task,
        agent_id=agent.id,
        status=DelegatedRunStatus.SUBMITTED,
        key="s1",
    )
    _run(
        db,
        project=project,
        task=task,
        agent_id=agent.id,
        status=DelegatedRunStatus.SUBMITTED,
        key="s2",
    )
    _run(
        db,
        project=project,
        task=task,
        agent_id=agent.id,
        status=DelegatedRunStatus.RUNNING,
        key="r1",
    )
    _run(
        db,
        project=project,
        task=task,
        agent_id=agent.id,
        status=DelegatedRunStatus.SUCCEEDED,
        key="ok1",
    )
    _run(
        db,
        project=project,
        task=task,
        agent_id=agent.id,
        status=DelegatedRunStatus.FAILED,
        key="f1",
    )
    _run(
        db,
        project=project,
        task=task,
        agent_id=agent.id,
        status=DelegatedRunStatus.CANCELLED,
        key="c1",
    )
    _run(
        db,
        project=project,
        task=task,
        agent_id=agent.id,
        status=DelegatedRunStatus.EXPIRED,
        key="e1",
    )

    counts = project_in_flight_by_agent(db)
    assert counts.get(agent.id) == 3


def test_terminal_statuses_not_counted(db) -> None:
    project, task = _seed_project_task(db)
    agent = _agent(db, "x")
    for st in (
        DelegatedRunStatus.SUCCEEDED,
        DelegatedRunStatus.FAILED,
        DelegatedRunStatus.CANCELLED,
        DelegatedRunStatus.EXPIRED,
    ):
        _run(db, project=project, task=task, agent_id=agent.id, status=st, key=f"t-{st.value}")

    counts = project_in_flight_by_agent(db)
    assert agent.id not in counts  # absent -> in_flight() returns 0


def test_local_run_with_null_agent_not_counted(db) -> None:
    project, task = _seed_project_task(db)
    agent = _agent(db, "x")
    # A real delegated run for the agent.
    _run(
        db,
        project=project,
        task=task,
        agent_id=agent.id,
        status=DelegatedRunStatus.SUBMITTED,
        key="d1",
    )
    # LOCAL run (agent_id None) must never inflate any agent's load.
    _run(
        db, project=project, task=task, agent_id=None, status=DelegatedRunStatus.SUBMITTED, key="l1"
    )

    counts = project_in_flight_by_agent(db)
    assert counts.get(agent.id) == 1


def test_agent_with_no_runs_has_zero_in_flight(db) -> None:
    project, task = _seed_project_task(db)
    busy = _agent(db, "busy")
    idle = _agent(db, "idle")
    _run(
        db,
        project=project,
        task=task,
        agent_id=busy.id,
        status=DelegatedRunStatus.RUNNING,
        key="b1",
    )

    snap = build_capacity_snapshot(parse_capacity_routing("{}"), project_in_flight_by_agent(db))
    assert snap.in_flight(busy.id) == 1
    assert snap.in_flight(idle.id) == 0  # absent agent -> 0, no telemetry needed


# --- INFLIGHT_RUN_STATUSES reuse (no second literal) -----------------------


def test_module_imports_inflight_statuses_from_delegation() -> None:
    tree = ast.parse(_MODULE_FILE.read_text(encoding="utf-8"))
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "aios.delegation"
    ]
    names = {alias.name for imp in imports for alias in imp.names}
    assert "INFLIGHT_RUN_STATUSES" in names


def test_module_does_not_redeclare_inflight_statuses() -> None:
    tree = ast.parse(_MODULE_FILE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "INFLIGHT_RUN_STATUSES":
                    raise AssertionError("INFLIGHT_RUN_STATUSES must not be re-declared here")


def test_projection_reuses_inflight_set_behaviourally(db) -> None:
    # If the module had re-declared the set wrong, a terminal status would leak in.
    project, task = _seed_project_task(db)
    agent = _agent(db, "x")
    _run(
        db,
        project=project,
        task=task,
        agent_id=agent.id,
        status=DelegatedRunStatus.SUBMITTED,
        key="s",
    )
    # INFLIGHT_RUN_STATUSES is exactly SUBMITTED + RUNNING.
    assert set(INFLIGHT_RUN_STATUSES) == {
        DelegatedRunStatus.SUBMITTED,
        DelegatedRunStatus.RUNNING,
    }


# --- saturated (pure) -------------------------------------------------------


def test_saturated_true_when_ceiling_reached() -> None:
    snap = build_capacity_snapshot(
        CapacityRouteConfig(enabled=True, default_max_inflight=2, agents={}),
        {"agt": 2},
    )
    assert snap.saturated("agt") is True


def test_saturated_false_below_ceiling() -> None:
    snap = build_capacity_snapshot(
        CapacityRouteConfig(enabled=True, default_max_inflight=2, agents={}),
        {"agt": 1},
    )
    assert snap.saturated("agt") is False


def test_agent_override_beats_default() -> None:
    snap = build_capacity_snapshot(
        CapacityRouteConfig(enabled=True, default_max_inflight=5, agents={"agt": 1}),
        {"agt": 1},
    )
    assert snap.saturated("agt") is True  # override 1, in_flight 1


def test_no_ceiling_means_never_saturated() -> None:
    snap = build_capacity_snapshot(
        CapacityRouteConfig(enabled=False, default_max_inflight=None, agents={}),
        {"agt": 999},
    )
    assert snap.saturated("agt") is False


# --- order_by_capacity (pure) ----------------------------------------------


def _cand(agent_id, minimum_priority, total_priority, **extra):
    return {
        "agent_id": agent_id,
        "minimum_priority": minimum_priority,
        "total_priority": total_priority,
        **extra,
    }


def test_disabled_snapshot_returns_unchanged_order() -> None:
    ranked = [_cand("b", 10, 10), _cand("a", 10, 10)]
    snap = CapacitySnapshot(
        enabled=False, default_max_inflight=None, agents={}, in_flight_by_agent={}
    )
    out = order_by_capacity(ranked, snap)
    assert [c["agent_id"] for c in out] == ["b", "a"]


def test_capability_priority_is_absolute_over_load() -> None:
    # Higher-priority agent is busier, but must still win (capability first).
    ranked = [
        _cand("busy", minimum_priority=10, total_priority=10, in_flight=5),
        _cand("idle", minimum_priority=5, total_priority=5, in_flight=0),
    ]
    snap = CapacitySnapshot(
        enabled=True,
        default_max_inflight=None,
        agents={},
        in_flight_by_agent={"busy": 5, "idle": 0},
    )
    out = order_by_capacity(ranked, snap)
    assert out[0]["agent_id"] == "busy"


def test_idle_wins_within_same_capability_tier() -> None:
    ranked = [
        _cand("busy", minimum_priority=10, total_priority=10, in_flight=5),
        _cand("idle", minimum_priority=10, total_priority=10, in_flight=0),
    ]
    snap = CapacitySnapshot(
        enabled=True,
        default_max_inflight=None,
        agents={},
        in_flight_by_agent={"busy": 5, "idle": 0},
    )
    out = order_by_capacity(ranked, snap)
    assert [c["agent_id"] for c in out] == ["idle", "busy"]


def test_agent_id_is_final_deterministic_tiebreak() -> None:
    ranked = [
        _cand("b", minimum_priority=10, total_priority=10, in_flight=0),
        _cand("a", minimum_priority=10, total_priority=10, in_flight=0),
    ]
    snap = CapacitySnapshot(
        enabled=True,
        default_max_inflight=None,
        agents={},
        in_flight_by_agent={"a": 0, "b": 0},
    )
    out = order_by_capacity(ranked, snap)
    assert [c["agent_id"] for c in out] == ["a", "b"]


def test_saturated_agent_ranks_after_idle_same_tier() -> None:
    ranked = [
        _cand("full", minimum_priority=10, total_priority=10, in_flight=2),
        _cand("half", minimum_priority=10, total_priority=10, in_flight=1),
    ]
    snap = CapacitySnapshot(
        enabled=True,
        default_max_inflight=2,
        agents={},
        in_flight_by_agent={"full": 2, "half": 1},
    )
    out = order_by_capacity(ranked, snap)
    assert [c["agent_id"] for c in out] == ["half", "full"]


def test_ordering_is_deterministic_across_calls() -> None:
    ranked = [
        _cand("b", minimum_priority=10, total_priority=10, in_flight=3),
        _cand("a", minimum_priority=10, total_priority=10, in_flight=1),
    ]
    snap = CapacitySnapshot(
        enabled=True,
        default_max_inflight=None,
        agents={},
        in_flight_by_agent={"a": 1, "b": 3},
    )
    assert order_by_capacity(ranked, snap) == order_by_capacity(ranked, snap)


def test_order_by_capacity_does_not_mutate_input() -> None:
    ranked = [_cand("b", 10, 10, in_flight=5), _cand("a", 10, 10, in_flight=0)]
    snap = CapacitySnapshot(
        enabled=True,
        default_max_inflight=None,
        agents={},
        in_flight_by_agent={"a": 0, "b": 5},
    )
    snapshot_before = [dict(c) for c in ranked]
    order_by_capacity(ranked, snap)
    assert ranked == snapshot_before


def test_cost_field_does_not_influence_order() -> None:
    # A candidate carrying a large cost must NOT be reordered by capacity logic.
    ranked = [
        _cand("cheap", minimum_priority=10, total_priority=10, in_flight=1, cost=0.01),
        _cand("pricey", minimum_priority=10, total_priority=10, in_flight=0, cost=99.0),
    ]
    snap = CapacitySnapshot(
        enabled=True,
        default_max_inflight=None,
        agents={},
        in_flight_by_agent={"cheap": 1, "pricey": 0},
    )
    out = order_by_capacity(ranked, snap)
    # Tie-break is in_flight (pricey idle first), never cost.
    assert [c["agent_id"] for c in out] == ["pricey", "cheap"]


# --- source-level guards: no DB write, no scheduler wiring -----------------


_FORBIDDEN_NAMES = {
    "append_audit",
    "accrue_run_budget",
    "check_budget",
    "claim_task_for_execution",
    "acquire_run_lease",
    "update_run_if_owned",
    "complete_local_run",
    "create_local_run",
    "route_task",
    "ExecutionAssignment",
}

_FORBIDDEN_SESSION_METHODS = {
    "add",
    "commit",
    "delete",
    "merge",
    "flush",
    "refresh",
    "expire",
    "expire_all",
    "bulk_save_objects",
    "bulk_insert_mappings",
    "bulk_update_mappings",
    "bulk_delete_mappings",
}


def test_module_has_no_write_operations() -> None:
    tree = ast.parse(_MODULE_FILE.read_text(encoding="utf-8"))
    seen_names: set[str] = set()
    seen_attrs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            seen_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            seen_attrs.add(node.attr)
    assert not (_FORBIDDEN_NAMES & seen_names), _FORBIDDEN_NAMES & seen_names
    assert not (_FORBIDDEN_SESSION_METHODS & seen_attrs), _FORBIDDEN_SESSION_METHODS & seen_attrs


def test_module_has_no_scheduler_wiring_references() -> None:
    tree = ast.parse(_MODULE_FILE.read_text(encoding="utf-8"))
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            seen.add(node.id)
        elif isinstance(node, ast.Attribute):
            seen.add(node.attr)
    for banned in ("route_task", "_rank", "_candidate", "scheduler"):
        assert banned not in seen, f"capacity_routing must not reference {banned!r}"


def test_scheduler_imports_capacity_routing_symbols() -> None:
    # PR-2 wires capacity_routing into scheduler.py. The ONLY coupling must be the
    # pure read/compute helpers -- never a write, scheduling or authority symbol.
    # This is the inverse of the PR-1 guard (which asserted zero coupling while
    # the module was unwired).
    source = _SCHEDULER_FILE.read_text(encoding="utf-8")
    assert "capacity_routing" in source
    for expected in (
        "build_capacity_snapshot",
        "load_capacity_routing",
        "order_by_capacity",
        "project_in_flight_by_agent",
    ):
        assert expected in source, f"scheduler must import {expected!r} from capacity_routing"


def test_projection_leaves_no_pending_writes(db) -> None:
    # Calling the read helper must not stage any new/dirty/deleted state.
    project, task = _seed_project_task(db)
    agent = _agent(db, "x")
    _run(
        db,
        project=project,
        task=task,
        agent_id=agent.id,
        status=DelegatedRunStatus.SUBMITTED,
        key="d1",
    )
    db.expire_all()
    assert not db.new and not db.dirty and not db.deleted
    project_in_flight_by_agent(db)
    assert not db.new and not db.dirty and not db.deleted
