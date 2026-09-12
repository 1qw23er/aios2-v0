"""Regression tests for heterogeneous-backend model attribution (H2).

Covers the additive surfaces introduced for H2:

* ``Agent.model`` -- per-agent LLM model override (nullable; ``None`` falls back to
  the global ``AIOS_AGENT_MODEL`` env default).
* ``DelegatedRun.model`` -- the model that actually executed a run, so an owner can
  audit WHICH model produced WHICH artifact.
* factory passthrough -- ``build_execution_adapter`` forwards ``agent.endpoint`` and
  ``agent.model`` into the in-process ``LLMExecutionAdapter``.
* ``complete_local_run`` persistence -- the ``model`` threaded from the adapter is
  written to the ``delegated_run`` row (the bug H2 fixed: the column existed via
  migration but the ORM class could not write it, so the value was silently dropped).

IMPORTANT: these tests prove the CONFIGURATION mechanism and auditability that make
heterogeneous backends possible. They do NOT prove physical-provider heterogeneity
(the live 8768 endpoint may resolve multiple aliases to the same physical model);
that is out of scope for this change.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session

from aios.adapters.factory import build_execution_adapter
from aios.db import get_database_url, get_engine, run_migrations
from aios.execution import LLMExecutionAdapter
from aios.execution_run import complete_local_run, create_local_run
from aios.models import (
    AdapterType,
    Agent,
    DelegatedRun,
    DelegatedRunStatus,
    Project,
    RoutingMode,
    Task,
    TaskStatus,
)


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'hetero.db'}")
    run_migrations(get_database_url())
    engine = get_engine(get_database_url())
    with Session(engine) as db:
        yield db


def _make_agent_with_model(
    session: Session,
    *,
    model: str | None,
    endpoint: str | None = None,
    adapter_type: AdapterType = AdapterType.API,
) -> tuple[Agent, Task]:
    project = Project(name="p", objective="o")
    session.add(project)
    session.commit()
    agent = Agent(
        id=f"agt-{model or 'none'}",
        name="A",
        role="r",
        adapter_type=adapter_type,
        endpoint=endpoint,
        model=model,
    )
    session.add(agent)
    session.commit()
    task = Task(
        project_id=project.id,
        title="t",
        description="d",
        status=TaskStatus.READY,
        routing_mode=RoutingMode.FIXED,
        assigned_agent_id=agent.id,
    )
    session.add(task)
    session.commit()
    return agent, task


def test_agent_model_field_persists_and_round_trips(session: Session) -> None:
    """Agent.model is a real mapped nullable column, not silently dropped."""
    agent = Agent(
        id="agt-model-x",
        name="A",
        role="r",
        adapter_type=AdapterType.API,
        model="hermes-primary",
    )
    session.add(agent)
    session.commit()
    reloaded = session.get(Agent, "agt-model-x")
    assert reloaded is not None
    assert reloaded.model == "hermes-primary"

    # None is a valid (fallback) value and round-trips unchanged.
    agent2 = Agent(
        id="agt-model-none",
        name="B",
        role="r",
        adapter_type=AdapterType.API,
        model=None,
    )
    session.add(agent2)
    session.commit()
    assert session.get(Agent, "agt-model-none").model is None


def test_factory_passes_agent_model_to_llm_adapter(session: Session) -> None:
    """build_execution_adapter forwards agent.endpoint/model into LLMExecutionAdapter."""
    agent, task = _make_agent_with_model(
        session, model="hermes-primary", endpoint="http://47.90.161.151:8768/v1"
    )
    adapter = build_execution_adapter(session, task.id)
    assert isinstance(adapter, LLMExecutionAdapter)
    assert adapter.model == "hermes-primary"
    assert adapter.base_url == "http://47.90.161.151:8768/v1"


def test_factory_default_agent_falls_back_to_env_model(
    session: Session, monkeypatch
) -> None:
    """An agent with model=None does not force a value; the adapter resolves the env default."""
    monkeypatch.setenv("AIOS_AGENT_MODEL", "deepseek-v4-flash")
    agent, task = _make_agent_with_model(session, model=None)
    adapter = build_execution_adapter(session, task.id)
    assert isinstance(adapter, LLMExecutionAdapter)
    # model=None on the agent does NOT force a value; the adapter resolves the
    # global AIOS_AGENT_MODEL env default (verified by the monkeypatch below).
    assert adapter.model == "deepseek-v4-flash"


def test_complete_local_run_persists_model_attribution(session: Session) -> None:
    """The model threaded into complete_local_run is written to delegated_run.model."""
    project = Project(name="p", objective="o")
    session.add(project)
    session.commit()
    task = Task(
        project_id=project.id,
        title="t",
        description="d",
        status=TaskStatus.READY,
        routing_mode=RoutingMode.FIXED,
    )
    session.add(task)
    session.commit()
    run = create_local_run(
        session,
        task_id=task.id,
        project_id=project.id,
        attempt=1,
        idempotency_key="ik-1",
    )
    assert run.status == DelegatedRunStatus.SUBMITTED
    assert run.model is None

    ok = complete_local_run(
        session,
        run_id=run.id,
        status=DelegatedRunStatus.SUCCEEDED,
        model="hermes-primary",
    )
    assert ok is True

    reloaded = session.get(DelegatedRun, run.id)
    assert reloaded.status == DelegatedRunStatus.SUCCEEDED
    assert reloaded.model == "hermes-primary"
