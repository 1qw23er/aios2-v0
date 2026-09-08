"""Runtime Thin Layer P1 -- liveness pre-filter + deterministic ranking (C4/C5/R5).

The liveness signal is a *pre-filter* applied inside ``_candidate`` (the per-agent
eligibility helper), BEFORE ``_rank``. It must never alter the deterministic
``_rank`` ordering of the healthy candidate set.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session

from aios.db import get_database_url, get_engine, run_migrations
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    Capability,
    DelegationMode,
)
from aios.scheduler import _candidate, _rank


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    url = f"sqlite:///{tmp_path / 'runtime_live.db'}"
    monkeypatch.setenv("AIOS_DATABASE_URL", url)
    run_migrations(get_database_url())
    eng = get_engine(get_database_url())
    s = Session(eng)
    yield s
    s.close()


def _cap(session: Session, name: str) -> Capability:
    c = Capability(name=name)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _agent(session: Session, aid: str, cap: Capability, *, hb=None, enabled=True) -> Agent:
    ag = Agent(
        id=aid,
        name=aid,
        role="worker",
        adapter_type=AdapterType.EXTERNAL,
        delegation_mode=DelegationMode.REMOTE_API,
        capabilities=[cap.name],
        enabled=enabled,
    )
    if hb is not None:
        ag.last_heartbeat_at = hb
    session.add(ag)
    session.add(
        AgentCapability(agent_id=aid, capability_id=cap.id, priority=50, enabled=True)
    )
    session.commit()
    session.refresh(ag)
    return ag


def test_null_heartbeat_agent_stays_eligible(session: Session) -> None:
    # Historical / never-heartbeated agents must NOT be judged stale (fail-open).
    cap = _cap(session, "cap-null")
    ag = _agent(session, "a-null", cap, hb=None)
    cand = _candidate(session, ag, [cap.id])
    assert cand["eligible"] is True
    assert "runtime_stale" not in cand["reasons"]


def test_fresh_heartbeat_agent_eligible(session: Session) -> None:
    cap = _cap(session, "cap-fresh")
    ag = _agent(session, "a-fresh", cap, hb=datetime.now(UTC) - timedelta(seconds=10))
    cand = _candidate(session, ag, [cap.id])
    assert cand["eligible"] is True


def test_stale_heartbeat_agent_excluded(session: Session) -> None:
    cap = _cap(session, "cap-stale")
    ag = _agent(session, "a-stale", cap, hb=datetime.now(UTC) - timedelta(seconds=1000))
    cand = _candidate(session, ag, [cap.id])
    assert cand["eligible"] is False
    assert "runtime_stale" in cand["reasons"]


def test_disabled_agent_excluded_regardless_of_heartbeat(session: Session) -> None:
    cap = _cap(session, "cap-dis")
    ag = _agent(
        session,
        "a-dis",
        cap,
        hb=datetime.now(UTC),
        enabled=False,
    )
    cand = _candidate(session, ag, [cap.id])
    assert cand["eligible"] is False


def test_heartbeat_does_not_change_rank_order(session: Session) -> None:
    # Both agents fresh enough to be healthy; only their heartbeat times differ.
    cap = _cap(session, "cap-rank")
    a = _agent(session, "z-rank", cap, hb=datetime.now(UTC) - timedelta(seconds=10))
    b = _agent(session, "a-rank", cap, hb=datetime.now(UTC) - timedelta(seconds=20))
    ca = _candidate(session, a, [cap.id])
    cb = _candidate(session, b, [cap.id])
    assert ca["eligible"] is True and cb["eligible"] is True
    # Order is by agent_id (deterministic), NOT heartbeat time.
    assert [c["agent_id"] for c in _rank([ca, cb])] == ["a-rank", "z-rank"]


def test_rank_key_ignores_heartbeat_field() -> None:
    # Directly proves the sort key never consults a heartbeat value.
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
