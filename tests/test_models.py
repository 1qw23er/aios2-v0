from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from aios.db import get_engine, run_migrations
from aios.models import (
    AdapterType,
    Agent,
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactType,
    Event,
    Project,
    RiskLevel,
    Task,
)


@pytest.fixture
def session(tmp_path: Path) -> Session:
    url = f"sqlite:///{(tmp_path / 'models.db').as_posix()}"
    run_migrations(url)
    with Session(get_engine(url)) as database_session:
        yield database_session


def test_all_domain_entities_persist(session: Session) -> None:
    project = Project(name="Launch", objective="Ship V0")
    agent = Agent(name="Planner", role="planner", adapter_type=AdapterType.API)
    session.add_all([project, agent])
    session.flush()
    task = Task(
        project_id=project.id,
        title="Plan",
        description="Create the plan",
        assigned_agent_id=agent.id,
    )
    session.add(task)
    session.flush()
    artifact = Artifact(
        project_id=project.id,
        task_id=task.id,
        type=ArtifactType.JSON,
        uri="artifacts/result.json",
        checksum="abc123",
    )
    approval = Approval(
        project_id=project.id,
        task_id=task.id,
        action_type="publish",
        risk_level=RiskLevel.L4,
    )
    session.add_all([artifact, approval])
    session.commit()

    assert session.get(Agent, agent.id) is not None
    assert session.get(Artifact, artifact.id) is not None
    assert session.get(Approval, approval.id).status == ApprovalStatus.PENDING


def test_event_idempotency_key_is_unique(session: Session) -> None:
    project = Project(name="Launch", objective="Ship V0")
    session.add(project)
    session.flush()
    session.add_all(
        [
            Event(
                project_id=project.id,
                type="project.created",
                idempotency_key="project-1",
            ),
            Event(
                project_id=project.id,
                type="project.created",
                idempotency_key="project-1",
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_task_requires_existing_project(session: Session) -> None:
    session.add(Task(project_id="prj_missing", title="Plan", description="Plan"))
    with pytest.raises(IntegrityError):
        session.commit()
