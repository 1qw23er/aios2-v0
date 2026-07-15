from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from sqlmodel import Session, select

from aios.audit import AuditLog
from aios.db import get_engine, run_migrations
from aios.knowledge_service import KnowledgeService
from aios.models import (
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    KnowledgeCandidateStatus,
    KnowledgeFact,
    KnowledgeFactStatus,
    KnowledgeReviewDecisionValue,
    Project,
)
from aios.services import ServiceError


def database(tmp_path: Path, name: str) -> str:
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    run_migrations(url)
    return url


def seed_artifact(session: Session, *, approved: bool = True) -> tuple[Project, Artifact]:
    project = Project(name="Knowledge", objective="Reuse reviewed facts")
    session.add(project)
    session.flush()
    artifact = Artifact(
        project_id=project.id,
        type=ArtifactType.JSON,
        uri="source.json",
        checksum="sha256:source",
        review_status=(
            ArtifactReviewStatus.APPROVED if approved else ArtifactReviewStatus.UNVERIFIED
        ),
        metadata_json={"password": "never-audit"},
    )
    session.add(artifact)
    session.commit()
    return project, artifact


def test_submit_candidate_requires_approved_exact_scope_artifact(tmp_path: Path) -> None:
    url = database(tmp_path, "submit.db")
    with Session(get_engine(url)) as session:
        project, artifact = seed_artifact(session)
        candidate = KnowledgeService(session).submit_candidate(
            artifact.id, "  Verified statement  ", project.id, " human_submitter "
        )
        assert candidate.statement == "Verified statement"
        assert candidate.submitted_by == "human_submitter"
        assert candidate.status == KnowledgeCandidateStatus.DRAFT
        with pytest.raises(ServiceError, match="exactly match"):
            KnowledgeService(session).submit_candidate(
                artifact.id, "Widened", None, "human_submitter"
            )
        audits = list(session.exec(select(AuditLog)))
        assert [audit.action for audit in audits] == ["knowledge.candidate.created"]
        assert "password" not in str(audits[0].after_snapshot).lower()


def test_submit_candidate_rejects_unapproved_and_empty_fields(tmp_path: Path) -> None:
    url = database(tmp_path, "invalid.db")
    with Session(get_engine(url)) as session:
        project, artifact = seed_artifact(session, approved=False)
        with pytest.raises(ServiceError, match="approved"):
            KnowledgeService(session).submit_candidate(
                artifact.id, "Statement", project.id, "human"
            )
        artifact.review_status = ArtifactReviewStatus.APPROVED
        session.add(artifact)
        session.commit()
        with pytest.raises(ServiceError, match="non-empty"):
            KnowledgeService(session).submit_candidate(artifact.id, " ", project.id, "human")


def test_rejection_is_terminal_idempotent_and_creates_no_fact(tmp_path: Path) -> None:
    url = database(tmp_path, "reject.db")
    with Session(get_engine(url)) as session:
        project, artifact = seed_artifact(session)
        service = KnowledgeService(session)
        candidate = service.submit_candidate(artifact.id, "Claim", project.id, "submitter")
        first = service.review_candidate(
            candidate.id,
            KnowledgeReviewDecisionValue.REJECT,
            "reviewer",
            "Not sufficiently supported",
        )
        replay = service.review_candidate(
            candidate.id,
            KnowledgeReviewDecisionValue.REJECT,
            "reviewer",
            "Not sufficiently supported",
        )
        assert replay.decision.id == first.decision.id
        assert replay.fact is None
        assert (
            session.get(type(candidate), candidate.id).status == KnowledgeCandidateStatus.REJECTED
        )
        assert list(session.exec(select(KnowledgeFact))) == []
        with pytest.raises(ServiceError, match="conflicts"):
            service.review_candidate(
                candidate.id,
                KnowledgeReviewDecisionValue.APPROVE,
                "reviewer",
                "Changed mind",
                series_id="series",
                version=1,
            )


def test_approval_and_supersession_keep_one_head(tmp_path: Path) -> None:
    url = database(tmp_path, "approve.db")
    with Session(get_engine(url)) as session:
        project, artifact = seed_artifact(session)
        service = KnowledgeService(session)
        first_candidate = service.submit_candidate(artifact.id, "Fact v1", project.id, "submitter")
        first = service.review_candidate(
            first_candidate.id,
            KnowledgeReviewDecisionValue.APPROVE,
            "reviewer",
            "Verified",
            series_id="series",
            version=1,
        )
        assert first.fact is not None
        assert first.fact.status == KnowledgeFactStatus.APPROVED
        second_candidate = service.submit_candidate(artifact.id, "Fact v2", project.id, "submitter")
        second = service.review_candidate(
            second_candidate.id,
            KnowledgeReviewDecisionValue.APPROVE,
            "reviewer",
            "New evidence",
            series_id="series",
            version=2,
            supersedes_fact_id=first.fact.id,
        )
        session.refresh(first.fact)
        assert first.fact.status == KnowledgeFactStatus.SUPERSEDED
        assert second.fact is not None and second.fact.status == KnowledgeFactStatus.APPROVED
        assert second.fact.supersedes_fact_id == first.fact.id
        heads = list(
            session.exec(
                select(KnowledgeFact).where(KnowledgeFact.status == KnowledgeFactStatus.APPROVED)
            )
        )
        assert [fact.id for fact in heads] == [second.fact.id]


def test_first_fact_and_head_rules_are_service_enforced(tmp_path: Path) -> None:
    url = database(tmp_path, "rules.db")
    with Session(get_engine(url)) as session:
        project, artifact = seed_artifact(session)
        candidate = KnowledgeService(session).submit_candidate(
            artifact.id, "Disconnected", project.id, "submitter"
        )
        with pytest.raises(ServiceError, match="version 1"):
            KnowledgeService(session).review_candidate(
                candidate.id,
                KnowledgeReviewDecisionValue.APPROVE,
                "reviewer",
                "Verified",
                series_id="series",
                version=2,
            )


def test_company_artifact_produces_only_company_candidate(tmp_path: Path) -> None:
    url = database(tmp_path, "company.db")
    with Session(get_engine(url)) as session:
        artifact = Artifact(
            project_id=None,
            type=ArtifactType.JSON,
            uri="company.json",
            checksum="sha256:company",
            review_status=ArtifactReviewStatus.APPROVED,
        )
        session.add(artifact)
        session.commit()
        candidate = KnowledgeService(session).submit_candidate(
            artifact.id, "Company fact", None, "company_reviewer"
        )
        assert candidate.project_id is None


def test_concurrent_replacements_leave_one_approved_head(tmp_path: Path) -> None:
    url = database(tmp_path, "concurrent.db")
    with Session(get_engine(url)) as session:
        project, artifact = seed_artifact(session)
        service = KnowledgeService(session)
        root_candidate = service.submit_candidate(artifact.id, "Root", project.id, "submitter")
        root_result = service.review_candidate(
            root_candidate.id,
            KnowledgeReviewDecisionValue.APPROVE,
            "reviewer",
            "Root evidence",
            series_id="concurrent-series",
            version=1,
        )
        first_candidate = service.submit_candidate(
            artifact.id, "Replacement A", project.id, "submitter"
        )
        second_candidate = service.submit_candidate(
            artifact.id, "Replacement B", project.id, "submitter"
        )
        root_id = root_result.fact.id
        candidate_ids = [first_candidate.id, second_candidate.id]

    barrier = Barrier(2)

    def replace(candidate_id: str, version: int) -> str:
        with Session(get_engine(url)) as worker_session:
            barrier.wait(timeout=10)
            try:
                result = KnowledgeService(worker_session).review_candidate(
                    candidate_id,
                    KnowledgeReviewDecisionValue.APPROVE,
                    "reviewer",
                    f"Concurrent evidence {version}",
                    series_id="concurrent-series",
                    version=version,
                    supersedes_fact_id=root_id,
                )
            except ServiceError:
                return "conflict"
            return result.fact.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda args: replace(*args),
                [(candidate_ids[0], 2), (candidate_ids[1], 3)],
            )
        )

    assert results.count("conflict") == 1
    with Session(get_engine(url)) as session:
        heads = list(
            session.exec(
                select(KnowledgeFact).where(
                    KnowledgeFact.series_id == "concurrent-series",
                    KnowledgeFact.status == KnowledgeFactStatus.APPROVED,
                )
            )
        )
        successors = list(
            session.exec(select(KnowledgeFact).where(KnowledgeFact.supersedes_fact_id == root_id))
        )
        assert len(heads) == 1
        assert len(successors) == 1
