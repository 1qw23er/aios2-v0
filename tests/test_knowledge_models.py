from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from aios.db import get_engine, run_migrations
from aios.models import (
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    KnowledgeCandidate,
    KnowledgeCandidateStatus,
    KnowledgeFact,
    KnowledgeFactStatus,
    KnowledgeReviewDecision,
    KnowledgeReviewDecisionValue,
    Project,
)
from alembic import command


def test_knowledge_model_defaults_and_provenance() -> None:
    candidate = KnowledgeCandidate(
        artifact_id="art_one",
        project_id="prj_one",
        statement="Reviewed statement",
        submitted_by="human",
    )
    decision = KnowledgeReviewDecision(
        candidate_id=candidate.id,
        decision=KnowledgeReviewDecisionValue.APPROVE,
        reviewer="reviewer",
        rationale="Verified source",
    )
    fact = KnowledgeFact(
        series_id="series",
        version=1,
        project_id="prj_one",
        statement=candidate.statement,
        source_candidate_id=candidate.id,
        source_artifact_id=candidate.artifact_id,
        review_decision_id=decision.id,
    )
    assert candidate.status == KnowledgeCandidateStatus.DRAFT
    assert fact.status == KnowledgeFactStatus.APPROVED
    assert fact.supersedes_fact_id is None


def _approved_candidate(session: Session) -> KnowledgeCandidate:
    project = Project(name="P", objective="O")
    session.add(project)
    session.flush()
    artifact = Artifact(
        project_id=project.id,
        type=ArtifactType.JSON,
        uri="a",
        checksum="c",
        review_status=ArtifactReviewStatus.APPROVED,
    )
    session.add(artifact)
    session.flush()
    candidate = KnowledgeCandidate(
        artifact_id=artifact.id,
        project_id=project.id,
        source_project_id=project.id,
        statement="S",
        submitted_by="human",
    )
    session.add(candidate)
    session.flush()
    return candidate


def test_migration_creates_alpha3_tables(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'knowledge.db').as_posix()}"
    run_migrations(url)
    with Session(get_engine(url)) as session:
        session.connection().exec_driver_sql("SELECT id FROM knowledge_candidate")
        session.connection().exec_driver_sql("SELECT id FROM knowledge_review_decision")
        session.connection().exec_driver_sql("SELECT id FROM knowledge_fact")


def test_unique_terminal_review_is_database_enforced(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'unique.db').as_posix()}"
    run_migrations(url)
    with Session(get_engine(url)) as session:
        candidate = _approved_candidate(session)
        session.add_all(
            [
                KnowledgeReviewDecision(
                    candidate_id=candidate.id,
                    decision=KnowledgeReviewDecisionValue.APPROVE,
                    reviewer="a",
                    rationale="r",
                ),
                KnowledgeReviewDecision(
                    candidate_id=candidate.id,
                    decision=KnowledgeReviewDecisionValue.REJECT,
                    reviewer="b",
                    rationale="r",
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_alpha3_migration_upgrade_downgrade_round_trip(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'roundtrip.db').as_posix()}"
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)

    def revision() -> str:
        with Session(get_engine(url)) as session:
            return session.connection().exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()

    command.upgrade(config, "20260715_0004")
    assert revision() == "20260715_0004"
    command.upgrade(config, "20260716_0005")
    assert revision() == "20260716_0005"
    command.downgrade(config, "20260715_0004")
    assert revision() == "20260715_0004"
    with Session(get_engine(url)) as session:
        tables = {
            row[0]
            for row in session.connection().exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "knowledge_fact" not in tables
    command.upgrade(config, "head")
    # Head advanced past the Alpha-3 knowledge layer by the Agent Interop
    # Gateway work (#57 / #104): 20260719_0001 + 20260719_0002. The round-trip
    # mechanics above already prove the knowledge migrations; this final step
    # only confirms we can return to the current head.
    assert revision() == "20260719_0002"
