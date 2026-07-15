from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from aios.db import get_engine, run_migrations
from aios.models import (
    AdapterType,
    Agent,
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    Decision,
    DecisionStatus,
    Policy,
    Project,
    ReviewedFact,
    ReviewedFactStatus,
    Task,
    TaskContext,
)


def test_alpha2_models_persist_with_backward_compatible_defaults(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'models.db').as_posix()}"
    run_migrations(url)
    with Session(get_engine(url)) as session:
        project = Project(name="Alpha", objective="Build context")
        agent = Agent(name="Writer", role="writer", adapter_type=AdapterType.API)
        session.add_all([project, agent])
        session.flush()
        task = Task(project_id=project.id, title="Draft", description="Draft")
        session.add(task)
        session.flush()
        artifact = Artifact(
            project_id=project.id,
            task_id=task.id,
            type=ArtifactType.JSON,
            uri="artifact.json",
            checksum="sha256:one",
        )
        session.add(artifact)
        session.flush()
        fact = ReviewedFact(
            artifact_id=artifact.id,
            statement="Reviewed statement",
            status=ReviewedFactStatus.APPROVED,
            reviewer="human_ceo",
        )
        decision = Decision(
            series_id="decision-publication",
            project_id=project.id,
            title="Publication",
            content="Review before publishing",
            status=DecisionStatus.APPROVED,
            version=1,
        )
        policy = Policy(
            series_id="policy-safety",
            name="Safety",
            content="Do not disclose secrets",
            version=1,
        )
        context = TaskContext(
            task_id=task.id,
            project_id=project.id,
            assigned_agent_id=agent.id,
            objective=project.objective,
            instructions=task.description,
            acceptance_criteria=[],
            project_context={},
            dependency_outputs=[],
            approved_facts=[],
            relevant_decisions=[],
            applicable_policies=[],
            agent_profile={},
            source_references=[],
            context_hash="a" * 64,
        )
        session.add_all([fact, decision, policy, context])
        session.commit()

        assert project.description == ""
        assert agent.limitations == []
        assert artifact.review_status == ArtifactReviewStatus.UNVERIFIED
        assert session.get(TaskContext, context.id).context_hash == "a" * 64


@pytest.mark.parametrize("model_name", ["decision", "policy"])
def test_lineage_version_is_unique(tmp_path: Path, model_name: str) -> None:
    url = f"sqlite:///{(tmp_path / f'{model_name}.db').as_posix()}"
    run_migrations(url)
    with Session(get_engine(url)) as session:
        if model_name == "decision":
            rows = [
                Decision(series_id="series", title="One", content="One", version=1),
                Decision(series_id="series", title="Two", content="Two", version=1),
            ]
        else:
            rows = [
                Policy(series_id="series", name="One", content="One", version=1),
                Policy(series_id="series", name="Two", content="Two", version=1),
            ]
        session.add_all(rows)
        with pytest.raises(IntegrityError):
            session.commit()
