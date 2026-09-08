"""Runtime Thin Layer P1 -- agent heartbeat (C2/C3/C5).

Covers the liveness signal at both the service layer (``record_agent_heartbeat``,
the function the endpoint delegates to) and the API surface (self-only scope via
the ``authenticate_agent`` dependency override, so no real secret store is needed).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from aios.actor import ActorContext
from aios.agent_registry import record_agent_heartbeat, register_agent
from aios.api.app import create_app
from aios.api.security import authenticate_agent
from aios.db import get_database_url, get_engine, run_migrations
from aios.models import (
    AdapterType,
    Agent,
    AgentStatus,
    AgentTrustLevel,
    DelegationMode,
)
from aios.services import ServiceError


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    url = f"sqlite:///{tmp_path / 'runtime_hb.db'}"
    monkeypatch.setenv("AIOS_DATABASE_URL", url)
    run_migrations(get_database_url())
    eng = get_engine(get_database_url())
    s = Session(eng)
    yield s
    s.close()


def _register(session: Session, name: str, *, enabled: bool = True) -> Agent:
    return register_agent(
        session,
        name=name,
        role="worker",
        adapter_type=AdapterType.EXTERNAL.value,
        delegation_mode=DelegationMode.REMOTE_API.value,
        capabilities=["x"],
        endpoint="https://e.example/run",
        secret_ref="secret://k",
        trust_level=AgentTrustLevel.VERIFIED_EXTERNAL.value,
        enabled=enabled,
    )


def test_heartbeat_sets_server_generated_timestamp(session: Session) -> None:
    agent = _register(session, "hb-set")
    assert agent.last_heartbeat_at is None
    updated = record_agent_heartbeat(session, agent.id)
    assert updated.last_heartbeat_at is not None
    # server-generated, recent (client never supplies a timestamp)
    drift = (
        datetime.now(UTC).replace(tzinfo=None) - updated.last_heartbeat_at
    ).total_seconds()
    assert drift < 5


def test_heartbeat_is_idempotent_in_effect(session: Session) -> None:
    agent = _register(session, "hb-idem")
    first = record_agent_heartbeat(session, agent.id)
    time.sleep(0.01)
    second = record_agent_heartbeat(session, agent.id)
    # repeated heartbeat only advances the timestamp; no error, monotonic effect
    assert second.last_heartbeat_at is not None
    assert second.last_heartbeat_at >= first.last_heartbeat_at


def test_heartbeat_unknown_agent_raises_404(session: Session) -> None:
    with pytest.raises(ServiceError) as exc:
        record_agent_heartbeat(session, "does-not-exist")
    assert exc.value.status_code == 404


def test_heartbeat_does_not_flip_disabled_status(session: Session) -> None:
    agent = _register(session, "hb-disabled", enabled=False)
    updated = record_agent_heartbeat(session, agent.id)
    assert updated.last_heartbeat_at is not None
    # heartbeat is metadata only -- it must not re-enable or change status
    assert updated.enabled is False
    assert updated.status == AgentStatus.UNAVAILABLE


# ---------------------------------------------------------------------------
# API surface: POST /agents/{agent_id}/heartbeat (self-only scope)
# ---------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'runtime_hb_api.db'}"
    monkeypatch.setenv("AIOS_DATABASE_URL", url)
    run_migrations(get_database_url())
    application = create_app()
    yield application


def _seed_agent(app, agent_id: str) -> None:
    eng = get_engine(get_database_url())
    with Session(eng) as s:
        ag = Agent(
            id=agent_id,
            name=agent_id,
            role="worker",
            adapter_type=AdapterType.EXTERNAL,
            delegation_mode=DelegationMode.REMOTE_API,
            capabilities=["x"],
            enabled=True,
        )
        s.add(ag)
        s.commit()


def test_heartbeat_endpoint_self_success(app) -> None:
    _seed_agent(app, "agt-self")
    app.dependency_overrides[authenticate_agent] = (
        lambda: ActorContext(kind="agent", agent_id="agt-self")
    )
    with TestClient(app) as client:
        resp = client.post("/agents/agt-self/heartbeat")
        assert resp.status_code == 200
        assert resp.json()["last_heartbeat_at"] is not None


def test_heartbeat_endpoint_cross_agent_rejected(app) -> None:
    _seed_agent(app, "agt-self")
    _seed_agent(app, "agt-other")
    # authenticated as agt-self but targeting agt-other -> 401, zero side effects
    app.dependency_overrides[authenticate_agent] = (
        lambda: ActorContext(kind="agent", agent_id="agt-self")
    )
    with TestClient(app) as client:
        resp = client.post("/agents/agt-other/heartbeat")
        assert resp.status_code == 401


def test_heartbeat_endpoint_requires_agent_identity(app) -> None:
    # No override -> real authenticate_agent runs. With no bearer it is rejected
    # (401 when the secret store is ready, 503 when it is unavailable); both mean
    # "auth enforced", never a silent 200.
    with TestClient(app) as client:
        resp = client.post("/agents/agt-self/heartbeat")
        assert resp.status_code in (401, 503)
