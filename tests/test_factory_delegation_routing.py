from __future__ import annotations

import pytest
from sqlmodel import Session

from aios.adapters.external import WorkstationAdapter
from aios.adapters.factory import build_execution_adapter
from aios.adapters.hermes_remote import RemoteApiAdapter
from aios.db import get_database_url, get_engine, run_migrations
from aios.execution import LLMExecutionAdapter
from aios.models import (
    AdapterType,
    Agent,
    DelegationMode,
    Project,
    RoutingMode,
    Task,
    TaskStatus,
)


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'factory.db'}")
    run_migrations(get_database_url())
    value = Session(get_engine(get_database_url()))
    yield value
    value.close()


def _make_task(session: Session, agent: Agent, schema: dict | None = None) -> Task:
    project = Project(name="p", objective="o")
    session.add(project)
    session.commit()
    session.add(agent)
    session.commit()
    task = Task(
        project_id=project.id,
        title="t",
        description="d",
        status=TaskStatus.READY,
        routing_mode=RoutingMode.FIXED,
        assigned_agent_id=agent.id,
        output_schema=schema
        or {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
    )
    session.add(task)
    session.commit()
    return task


def test_local_model_agent_returns_llm_execution_adapter(session: Session) -> None:
    """Regression guard: a MODEL agent with no delegation_mode must keep resolving
    to the in-process LLM adapter (behaviour unchanged from before this feature)."""
    agent = Agent(
        id="agt-llm",
        name="L",
        role="r",
        adapter_type=AdapterType.MODEL,
        delegation_mode=None,
    )
    task = _make_task(session, agent)
    adapter = build_execution_adapter(session, task.id)
    assert isinstance(adapter, LLMExecutionAdapter)


def test_remote_api_agent_returns_remote_adapter(session: Session, monkeypatch) -> None:
    monkeypatch.setenv("AIOS_DEEPSEEK_HARNESS_ENABLED", "false")
    agent = Agent(
        id="agt-remote",
        name="R",
        role="r",
        adapter_type=AdapterType.API,
        delegation_mode=DelegationMode.REMOTE_API,
        endpoint="http://127.0.0.1:1",  # unreachable; only the type is asserted
    )
    task = _make_task(session, agent)
    adapter = build_execution_adapter(session, task.id)
    assert isinstance(adapter, RemoteApiAdapter)


def test_workstation_agent_returns_workstation_adapter(
    session: Session, tmp_path, monkeypatch
) -> None:
    outbox = tmp_path / "outbox"
    inbox = tmp_path / "inbox"
    monkeypatch.setenv("AIOS_WORKSTATION_OUTBOX", str(outbox))
    monkeypatch.setenv("AIOS_WORKSTATION_INBOX", str(inbox))
    agent = Agent(
        id="agt-ws",
        name="W",
        role="r",
        adapter_type=AdapterType.EXTERNAL,
        delegation_mode=DelegationMode.WORKSTATION,
        platform="workbuddy",
    )
    task = _make_task(session, agent)
    adapter = build_execution_adapter(session, task.id)
    assert isinstance(adapter, WorkstationAdapter)


def test_workstation_without_env_falls_back_to_llm(
    session: Session, monkeypatch
) -> None:
    """Fail-safe: a WORKSTATION agent whose outbox/inbox env is unset must NOT
    crash execution -- it falls back to the local LLM adapter."""
    monkeypatch.delenv("AIOS_WORKSTATION_OUTBOX", raising=False)
    monkeypatch.delenv("AIOS_WORKSTATION_INBOX", raising=False)
    agent = Agent(
        id="agt-ws2",
        name="W2",
        role="r",
        adapter_type=AdapterType.EXTERNAL,
        delegation_mode=DelegationMode.WORKSTATION,
        platform="workbuddy",
    )
    task = _make_task(session, agent)
    adapter = build_execution_adapter(session, task.id)
    assert isinstance(adapter, LLMExecutionAdapter)
