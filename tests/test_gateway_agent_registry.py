"""Agent registry management — owner-operable DB-backed registry (#57, #61).

Covers register / list / get / enable-disable through the single
``aios.agent_registry`` service, plus the validation + audit invariants:
  * external agents (API / EXTERNAL) MUST declare a delegation_mode;
  * local adapters (MODEL / CLI) MUST NOT;
  * only opaque ``secret_ref`` handles are accepted (never raw secrets);
  * every mutation is audited (agent.registered / agent.enabled_changed).
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from aios.agent_registry import (
    get_agent,
    list_agents,
    register_agent,
    set_agent_enabled,
)
from aios.audit import AuditLog
from aios.db import get_database_url, get_engine, run_migrations
from aios.models import (
    AdapterType,
    AgentStatus,
    AgentTrustLevel,
    DelegationMode,
)
from aios.services import ServiceError


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    # Scope the DB url so it cannot leak into sibling test files.
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'registry.db'}")
    run_migrations(get_database_url())
    eng = get_engine(get_database_url())
    s = Session(eng)
    yield s
    s.close()


def _audit_actions(session: Session) -> list[str]:
    session.expire_all()
    return [a.action for a in session.exec(select(AuditLog)).all()]


def test_register_and_list(session: Session) -> None:
    before = len(list_agents(session))
    agent = register_agent(
        session,
        name="Hermes",
        role="内容生产",
        adapter_type=AdapterType.EXTERNAL.value,
        delegation_mode=DelegationMode.REMOTE_API.value,
        capabilities=["写作", "排版"],
        endpoint="https://hermes.example/run",
        secret_ref="secret://hermes-key",
        callback_url="https://hermes.example/cb",
        trust_level=AgentTrustLevel.VERIFIED_EXTERNAL.value,
        timeout_s=120.0,
        max_retries=2,
        enabled=True,
    )
    assert agent.id
    assert agent.adapter_type == AdapterType.EXTERNAL
    assert agent.delegation_mode == DelegationMode.REMOTE_API
    assert agent.trust_level == AgentTrustLevel.VERIFIED_EXTERNAL
    # Opaque handle is stored as-is; the secret value itself is never here.
    assert agent.secret_ref == "secret://hermes-key"
    assert agent.enabled is True
    assert agent.status == AgentStatus.AVAILABLE

    assert len(list_agents(session)) == before + 1
    assert "agent.registered" in _audit_actions(session)


def test_register_rejects_external_without_delegation_mode(session: Session) -> None:
    try:
        register_agent(
            session,
            name="NoMode",
            role="r",
            adapter_type=AdapterType.API.value,  # API is external -> needs mode
            delegation_mode=None,
        )
        raise AssertionError("expected ServiceError")
    except ServiceError as error:
        assert error.status_code == 400


def test_register_rejects_local_with_delegation_mode(session: Session) -> None:
    try:
        register_agent(
            session,
            name="LocalWithMode",
            role="r",
            adapter_type=AdapterType.MODEL.value,  # local adapter -> no mode
            delegation_mode=DelegationMode.REMOTE_API.value,
        )
        raise AssertionError("expected ServiceError")
    except ServiceError as error:
        assert error.status_code == 400


def test_register_rejects_unknown_adapter_type(session: Session) -> None:
    try:
        register_agent(session, name="X", role="r", adapter_type="bogus")
        raise AssertionError("expected ServiceError")
    except ServiceError as error:
        assert error.status_code == 400


def test_get_agent_404(session: Session) -> None:
    try:
        get_agent(session, "agt_does_not_exist")
        raise AssertionError("expected ServiceError")
    except ServiceError as error:
        assert error.status_code == 404


def test_set_agent_enabled_toggles_status(session: Session) -> None:
    agent = register_agent(
        session,
        name="Toggle",
        role="r",
        adapter_type=AdapterType.EXTERNAL.value,
        delegation_mode=DelegationMode.WORKSTATION.value,
        enabled=True,
    )
    assert agent.status == AgentStatus.AVAILABLE

    # Disable -> UNAVAILABLE, audited.
    disabled = set_agent_enabled(session, agent.id, False)
    assert disabled.enabled is False
    assert disabled.status == AgentStatus.UNAVAILABLE
    assert "agent.enabled_changed" in _audit_actions(session)

    # Re-enable -> AVAILABLE.
    re_enabled = set_agent_enabled(session, agent.id, True)
    assert re_enabled.enabled is True
    assert re_enabled.status == AgentStatus.AVAILABLE


def test_set_agent_enabled_404(session: Session) -> None:
    try:
        set_agent_enabled(session, "agt_does_not_exist", False)
        raise AssertionError("expected ServiceError")
    except ServiceError as error:
        assert error.status_code == 404
