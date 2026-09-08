"""Runtime Thin Layer P1 -- architecture invariants R1-R8 (C1/C8/C9/C10).

These guard the contract's hard boundaries: no Runtime entity, no runtime_id FK on
core tables, Capability stays the single source of truth, Workforce is untouched,
heartbeat never enters ranking, no arbitrary adapter import, STALE is not a
persisted enum, and Agent remains the only execution identity.
"""

from __future__ import annotations

import inspect

import sqlmodel
from sqlmodel import Session

from aios.models import (
    Agent,
    AgentStatus,
    DelegatedRun,
    Employee,
    EmployeeAgentBinding,
    ExecutionAssignment,
    Task,
)
from aios.scheduler import _rank


def _tables() -> set[str]:
    return set(sqlmodel.SQLModel.metadata.tables.keys())


def test_r1_no_runtime_table_or_class() -> None:
    assert not any("runtime" in name for name in _tables())
    classes = {name for name, _ in inspect.getmembers(sqlmodel.SQLModel, inspect.isclass)}
    # SQLModel base classes are also matched by getmembers; check the models module
    import aios.models as models

    model_classes = {name for name, _ in inspect.getmembers(models, inspect.isclass)}
    assert "Runtime" not in model_classes
    assert "RuntimeRegistration" not in model_classes
    _ = classes


def test_r2_no_runtime_id_fk_on_core_tables() -> None:
    for cls in (Task, ExecutionAssignment, DelegatedRun):
        cols = {c.name for c in cls.__table__.columns}
        assert "runtime_id" not in cols


def test_r3_capability_ssoT_unchanged() -> None:
    import aios.adapters.factory as factory

    src = inspect.getsource(factory)
    assert "Capability" not in src
    assert "AgentCapability" not in src
    assert "capability" in _tables()
    assert "agent_capability" in _tables()


def test_r4_workforce_has_no_runtime_fk() -> None:
    for cls in (Employee, EmployeeAgentBinding):
        cols = {c.name for c in cls.__table__.columns}
        assert not any("runtime" in c for c in cols)


def test_r5_heartbeat_not_in_rank_key() -> None:
    src = inspect.getsource(_rank)
    assert "heartbeat" not in src
    # behavioral: a heartbeat field on the candidate dict is ignored by _rank
    c_old = {
        "agent_id": "b",
        "eligible": True,
        "minimum_priority": 1,
        "total_priority": 1,
        "last_heartbeat_at": "old",
    }
    c_new = {
        "agent_id": "a",
        "eligible": True,
        "minimum_priority": 1,
        "total_priority": 1,
        "last_heartbeat_at": "new",
    }
    assert [c["agent_id"] for c in _rank([c_old, c_new])] == ["a", "b"]


def test_r6_no_arbitrary_import_in_factory() -> None:
    import aios.adapters.factory as factory

    src = inspect.getsource(factory)
    assert "__import__" not in src
    assert "import_module" not in src


def test_r7_stale_not_persisted_in_agent_status() -> None:
    assert "STALE" not in [member.name for member in AgentStatus]


def test_r8_agent_is_only_execution_identity() -> None:
    import aios.models as models

    assert hasattr(Agent, "adapter_type")
    model_classes = {name for name, _ in inspect.getmembers(models, inspect.isclass)}
    assert "Runtime" not in model_classes


# Keep `Session` imported for typing parity with sibling test modules.
_ = Session
