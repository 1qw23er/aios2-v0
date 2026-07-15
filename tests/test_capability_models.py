from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from aios.db import get_engine, run_migrations
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    AgentStatus,
    Capability,
    ExecutionAssignment,
    Project,
    RoutingMode,
    Task,
)


def test_capability_routing_models_persist_with_compatible_defaults(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'capabilities.db').as_posix()}"
    run_migrations(url)
    with Session(get_engine(url)) as session:
        project = Project(name="Alpha", objective="Route")
        agent = Agent(name="Writer", role="writer", adapter_type=AdapterType.API)
        capability = Capability(name="writing", description="Writes copy")
        session.add_all([project, agent, capability])
        session.flush()
        profile = AgentCapability(agent_id=agent.id, capability_id=capability.id, priority=90)
        task = Task(project_id=project.id, title="Draft", description="Draft")
        session.add_all([profile, task])
        session.flush()
        assignment = ExecutionAssignment(
            task_id=task.id,
            selected_agent_id=agent.id,
            routing_reason="legacy fixed assignment",
            fallback_used=False,
            idempotency_key="assign-1",
        )
        session.add(assignment)
        session.commit()

        assert agent.status == AgentStatus.AVAILABLE
        assert task.routing_mode == RoutingMode.FIXED
        assert task.required_capabilities == []
        assert session.get(ExecutionAssignment, assignment.id).selected_agent_id == agent.id


def test_agent_capability_priority_is_bounded(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'priority.db').as_posix()}"
    run_migrations(url)
    with Session(get_engine(url)) as session:
        agent = Agent(name="Writer", role="writer", adapter_type=AdapterType.API)
        capability = Capability(name="writing")
        session.add_all([agent, capability])
        session.flush()
        session.add(
            AgentCapability(
                agent_id=agent.id,
                capability_id=capability.id,
                priority=101,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
