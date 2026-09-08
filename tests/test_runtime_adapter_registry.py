"""Runtime Thin Layer P1 -- adapter resolution (C6/C7/R6).

Verifies the execution-adapter selection is deterministic and fail-closed, and
that no dynamic / arbitrary import is performed when resolving an adapter.
"""

from __future__ import annotations

import inspect

import pytest
from sqlmodel import Session

from aios.adapters.deepseek_harness import HarnessTransportError
from aios.adapters.factory import build_execution_adapter
from aios.db import get_database_url, get_engine, run_migrations
from aios.execution import LLMExecutionAdapter
from aios.models import (
    AdapterType,
    Agent,
    DelegationMode,
    Project,
    Task,
    TaskStatus,
)


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    url = f"sqlite:///{tmp_path / 'runtime_adapter.db'}"
    monkeypatch.setenv("AIOS_DATABASE_URL", url)
    run_migrations(get_database_url())
    eng = get_engine(get_database_url())
    s = Session(eng)
    yield s
    s.close()


def _task_for_agent(session: Session, agent: Agent) -> str:
    project = Project(name="p", objective="o")
    session.add(project)
    session.commit()
    session.refresh(project)
    task = Task(
        project_id=project.id,
        title="t",
        description="d",
        status=TaskStatus.READY,
        assigned_agent_id=agent.id,
        output_schema={},
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return task.id


def test_harness_disabled_resolves_to_local_llm(session: Session, monkeypatch) -> None:
    monkeypatch.setenv("AIOS_DEEPSEEK_HARNESS_ENABLED", "false")
    agent = Agent(
        id="ag-llm",
        name="llm",
        role="worker",
        adapter_type=AdapterType.EXTERNAL,
        delegation_mode=DelegationMode.REMOTE_API,
        capabilities=["x"],
        enabled=True,
    )
    session.add(agent)
    session.commit()
    task_id = _task_for_agent(session, agent)
    adapter = build_execution_adapter(session, task_id)
    assert isinstance(adapter, LLMExecutionAdapter)


def test_invalid_harness_config_fails_closed(session: Session, monkeypatch) -> None:
    # Harness enabled but the config_ref points at a non-existent file -> parse
    # error -> fail-closed (raises, never silently falls back to LLM).
    monkeypatch.setenv("AIOS_DEEPSEEK_HARNESS_ENABLED", "true")
    agent = Agent(
        id="ag-bad",
        name="bad",
        role="worker",
        adapter_type=AdapterType.EXTERNAL,
        delegation_mode=DelegationMode.REMOTE_API,
        capabilities=["x"],
        enabled=True,
        config_ref="deepseek-harness+file:///does/not/exist.json",
        secret_ref="env://NOPE",
    )
    session.add(agent)
    session.commit()
    task_id = _task_for_agent(session, agent)
    with pytest.raises(HarnessTransportError):
        build_execution_adapter(session, task_id)


def test_factory_has_no_dynamic_import() -> None:
    import aios.adapters.factory as factory

    src = inspect.getsource(factory)
    assert "__import__" not in src
    assert "import_module" not in src
