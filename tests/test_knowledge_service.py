from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from sqlmodel import Session, select

from aios.actor import ActorContext, resolve_agent_actor, resolve_owner_actor
from aios.audit import AuditLog
from aios.db import get_engine, run_migrations
from aios.knowledge_service import KnowledgeService
from aios.knowledge_tags import LEGACY_UNCLASSIFIED
from aios.models import (
    AdapterType,
    Agent,
    AgentTrustLevel,
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


def submit(
    session: Session,
    artifact_id: str,
    statement: str,
    *,
    scope: str = "project",
    tags=None,
    actor=None,
):
    if actor is None:
        actor = resolve_owner_actor()
    project_id = None
    if scope != "company":
        art = session.get(Artifact, artifact_id)
        project_id = art.project_id if art is not None else None
    return KnowledgeService(session).submit_candidate(
        artifact_id, statement, project_id=project_id, tags=tags, actor=actor
    )


def make_fact(session: Session, project: Project, artifact: Artifact, *, tags) -> KnowledgeFact:
    """Create a fully-approved KnowledgeFact with the given (canonical) tags."""
    candidate = KnowledgeService(session).submit_candidate(
        artifact.id,
        "Seed statement",
        project_id=project.id,
        tags=list(tags),
        actor=resolve_owner_actor(),
    )
    result = KnowledgeService(session).review_candidate(
        candidate.id,
        KnowledgeReviewDecisionValue.APPROVE,
        "Seed rationale",
        actor=resolve_owner_actor(),
        series_id="seed-series",
        version=1,
    )
    return result.fact


def seed_sentinel_fact(
    session: Session, project: Project, artifact: Artifact
) -> KnowledgeFact:
    """Simulate a legacy backfilled sentinel fact (DRAFT->APPROVED candidate).

    Mirrors the migration backfill: the candidate carries the JSON-safe sentinel
    and is promoted to APPROVED (the trigger permits a sentinel candidate to be
    approved), then a review decision + fact are written with the same sentinel.
    """
    candidate = KnowledgeCandidate(
        artifact_id=artifact.id,
        project_id=project.id,
        source_project_id=project.id,
        statement="Legacy unclassified fact",
        tags=[LEGACY_UNCLASSIFIED],
        submitted_by_kind="owner",
        submitted_by_owner_id="owner",
        submitted_by="owner",
    )
    session.add(candidate)
    session.flush()
    candidate.status = KnowledgeCandidateStatus.APPROVED
    session.add(candidate)
    session.flush()
    review = KnowledgeReviewDecision(
        candidate_id=candidate.id,
        decision=KnowledgeReviewDecisionValue.APPROVE,
        reviewer_kind="owner",
        reviewer_owner_id="owner",
        reviewer="owner",
        rationale="legacy backfill",
    )
    session.add(review)
    session.flush()
    fact = KnowledgeFact(
        series_id="legacy-series",
        version=1,
        project_id=project.id,
        source_project_id=project.id,
        statement="Legacy unclassified fact",
        tags=[LEGACY_UNCLASSIFIED],
        source_candidate_id=candidate.id,
        source_artifact_id=artifact.id,
        review_decision_id=review.id,
        supersedes_fact_id=None,
    )
    session.add(fact)
    session.commit()
    session.refresh(fact)
    return fact


# --- existing service-level behavior (signatures updated) ---------------------


def test_submit_candidate_requires_approved_exact_scope_artifact(tmp_path: Path) -> None:
    url = database(tmp_path, "submit.db")
    with Session(get_engine(url)) as session:
        project, artifact = seed_artifact(session)
        candidate = submit(session, artifact.id, "  Verified statement  ", scope="project")
        assert candidate.statement == "Verified statement"
        assert candidate.submitted_by == "owner:owner"
        assert candidate.submitted_by_kind == "owner"
        assert candidate.status == KnowledgeCandidateStatus.DRAFT
        # Effective scope and source provenance both trace to the artifact's
        # campaign; a project-scoped candidate must match its source campaign
        # (no silent promotion across campaigns).
        assert candidate.project_id == project.id
        assert candidate.source_project_id == project.id
        # A project-scoped candidate for a different campaign is rejected.
        other = Project(name="Other", objective="x")
        session.add(other)
        session.commit()
        with pytest.raises(ServiceError, match="match its source campaign"):
            KnowledgeService(session).submit_candidate(
                artifact.id,
                "Widened",
                project_id=other.id,
                tags=None,
                actor=resolve_owner_actor(),
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
                artifact.id,
                "Statement",
                project_id=project.id,
                tags=None,
                actor=resolve_owner_actor(),
            )
        artifact.review_status = ArtifactReviewStatus.APPROVED
        session.add(artifact)
        session.commit()
        with pytest.raises(ServiceError, match="non-empty"):
            KnowledgeService(session).submit_candidate(
                artifact.id,
                " ",
                project_id=project.id,
                tags=None,
                actor=resolve_owner_actor(),
            )


def test_rejection_is_terminal_idempotent_and_creates_no_fact(tmp_path: Path) -> None:
    url = database(tmp_path, "reject.db")
    with Session(get_engine(url)) as session:
        project, artifact = seed_artifact(session)
        service = KnowledgeService(session)
        candidate = submit(session, artifact.id, "Claim", scope="project")
        first = service.review_candidate(
            candidate.id,
            KnowledgeReviewDecisionValue.REJECT,
            "Not sufficiently supported",
            actor=resolve_owner_actor(),
        )
        replay = service.review_candidate(
            candidate.id,
            KnowledgeReviewDecisionValue.REJECT,
            "Not sufficiently supported",
            actor=resolve_owner_actor(),
        )
        assert replay.decision.id == first.decision.id
        assert replay.fact is None
        assert (
            session.get(type(candidate), candidate.id).status
            == KnowledgeCandidateStatus.REJECTED
        )
        assert list(session.exec(select(KnowledgeFact))) == []
        with pytest.raises(ServiceError, match="conflicts"):
            service.review_candidate(
                candidate.id,
                KnowledgeReviewDecisionValue.APPROVE,
                "Changed mind",
                actor=resolve_owner_actor(),
                series_id="series",
                version=1,
            )


def test_approval_and_supersession_keep_one_head(tmp_path: Path) -> None:
    url = database(tmp_path, "approve.db")
    with Session(get_engine(url)) as session:
        project, artifact = seed_artifact(session)
        service = KnowledgeService(session)
        first_candidate = submit(session, artifact.id, "Fact v1", scope="project")
        first = service.review_candidate(
            first_candidate.id,
            KnowledgeReviewDecisionValue.APPROVE,
            "Verified",
            actor=resolve_owner_actor(),
            series_id="series",
            version=1,
        )
        assert first.fact is not None
        assert first.fact.status == KnowledgeFactStatus.APPROVED
        second_candidate = submit(session, artifact.id, "Fact v2", scope="project")
        second = service.review_candidate(
            second_candidate.id,
            KnowledgeReviewDecisionValue.APPROVE,
            "New evidence",
            actor=resolve_owner_actor(),
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
                select(KnowledgeFact).where(
                    KnowledgeFact.status == KnowledgeFactStatus.APPROVED
                )
            )
        )
        assert [fact.id for fact in heads] == [second.fact.id]


def test_first_fact_and_head_rules_are_service_enforced(tmp_path: Path) -> None:
    url = database(tmp_path, "rules.db")
    with Session(get_engine(url)) as session:
        project, artifact = seed_artifact(session)
        candidate = submit(session, artifact.id, "Disconnected", scope="project")
        with pytest.raises(ServiceError, match="version 1"):
            KnowledgeService(session).review_candidate(
                candidate.id,
                KnowledgeReviewDecisionValue.APPROVE,
                "Verified",
                actor=resolve_owner_actor(),
                series_id="series",
                version=2,
            )


def test_company_artifact_produces_only_company_candidate(tmp_path: Path) -> None:
    url = database(tmp_path, "company.db")
    with Session(get_engine(url)) as session:
        project = Project(name="Company", objective="Reuse everywhere")
        session.add(project)
        session.flush()
        artifact = Artifact(
            project_id=project.id,
            type=ArtifactType.JSON,
            uri="company.json",
            checksum="sha256:company",
            review_status=ArtifactReviewStatus.APPROVED,
        )
        session.add(artifact)
        session.commit()
        candidate = KnowledgeService(session).submit_candidate(
            artifact.id,
            "Company fact",
            project_id=None,
            tags=None,
            actor=resolve_owner_actor(),
        )
        assert candidate.project_id is None
        assert candidate.source_project_id == project.id


def test_concurrent_replacements_leave_one_approved_head(tmp_path: Path) -> None:
    url = database(tmp_path, "concurrent.db")
    with Session(get_engine(url)) as session:
        project, artifact = seed_artifact(session)
        service = KnowledgeService(session)
        root_candidate = submit(session, artifact.id, "Root", scope="project")
        root_result = service.review_candidate(
            root_candidate.id,
            KnowledgeReviewDecisionValue.APPROVE,
            "Root evidence",
            actor=resolve_owner_actor(),
            series_id="concurrent-series",
            version=1,
        )
        first_candidate = submit(session, artifact.id, "Replacement A", scope="project")
        second_candidate = submit(session, artifact.id, "Replacement B", scope="project")
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
                    f"Concurrent evidence {version}",
                    actor=resolve_owner_actor(),
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
            session.exec(
                select(KnowledgeFact).where(
                    KnowledgeFact.supersedes_fact_id == root_id
                )
            )
        )
        assert len(heads) == 1
        assert len(successors) == 1


# --- Phase A: trusted identity ------------------------------------------------


def test_submitted_by_derived_from_trusted_actor(tmp_path: Path) -> None:
    url = database(tmp_path, "identity.db")
    with Session(get_engine(url)) as session:
        project, artifact = seed_artifact(session)
        owner_candidate = submit(session, artifact.id, "Owner claim", scope="project")
        assert owner_candidate.submitted_by_kind == "owner"
        assert owner_candidate.submitted_by == "owner:owner"
        assert owner_candidate.submitted_by_owner_id == "owner"
        assert owner_candidate.submitted_by_agent_id is None

        agent = Agent(
            id="agt:ext1",
            name="External",
            role="ext",
            adapter_type=AdapterType.API,
            trust_level=AgentTrustLevel.INTERNAL,
        )
        session.add(agent)
        session.commit()
        agent_candidate = KnowledgeService(session).submit_candidate(
            artifact.id,
            "Agent claim",
            project_id=project.id,
            tags=None,
            actor=resolve_agent_actor(agent.id),
        )
        assert agent_candidate.submitted_by_kind == "agent"
        assert agent_candidate.submitted_by == f"agent:{agent.id}"
        assert agent_candidate.submitted_by_agent_id == agent.id
        assert agent_candidate.submitted_by_owner_id is None


# --- Phase A: formal actions are owner-only -----------------------------------


def test_formal_actions_reject_non_owner_actor(tmp_path: Path) -> None:
    url = database(tmp_path, "owneronly.db")
    with Session(get_engine(url)) as session:
        project, artifact = seed_artifact(session)
        candidate = submit(session, artifact.id, "Claim", scope="project")
        # review by system actor -> 403
        with pytest.raises(ServiceError, match="owner identity"):
            KnowledgeService(session).review_candidate(
                candidate.id,
                KnowledgeReviewDecisionValue.APPROVE,
                "nope",
                actor=ActorContext.system(),
                series_id="s",
                version=1,
            )
        # review by agent actor -> 403
        with pytest.raises(ServiceError, match="owner identity"):
            KnowledgeService(session).review_candidate(
                candidate.id,
                KnowledgeReviewDecisionValue.APPROVE,
                "nope",
                actor=resolve_agent_actor("agt:x"),
                series_id="s",
                version=1,
            )
        fact = make_fact(session, project, artifact, tags=["positioning"])
        # deactivate by agent actor -> 403
        with pytest.raises(ServiceError, match="owner identity"):
            KnowledgeService(session).deactivate_fact(
                fact.id, "withdraw", actor=resolve_agent_actor("agt:x")
            )
        # classify by system actor -> 403
        sentinel = seed_sentinel_fact(session, project, artifact)
        with pytest.raises(ServiceError, match="owner identity"):
            KnowledgeService(session).classify_knowledge(
                sentinel.id, ["positioning"], actor=ActorContext.system()
            )


def test_review_rejects_sentinel_candidate(tmp_path: Path) -> None:
    url = database(tmp_path, "sentinel-reject.db")
    with Session(get_engine(url)) as session:
        project, artifact = seed_artifact(session)
        candidate = KnowledgeCandidate(
            artifact_id=artifact.id,
            project_id=project.id,
            source_project_id=project.id,
            statement="Legacy unclassified",
            tags=[LEGACY_UNCLASSIFIED],
            submitted_by_kind="owner",
            submitted_by_owner_id="owner",
            submitted_by="owner",
        )
        session.add(candidate)
        session.commit()
        with pytest.raises(ServiceError, match="classified before review"):
            KnowledgeService(session).review_candidate(
                candidate.id,
                KnowledgeReviewDecisionValue.APPROVE,
                "try anyway",
                actor=resolve_owner_actor(),
                series_id="s",
                version=1,
            )


# --- Phase A: one-time sentinel -> canonical classification -------------------


def test_classify_knowledge_sentinel_to_canonical_once(tmp_path: Path) -> None:
    url = database(tmp_path, "classify-fact.db")
    with Session(get_engine(url)) as session:
        project, artifact = seed_artifact(session)
        fact = seed_sentinel_fact(session, project, artifact)
        updated = KnowledgeService(session).classify_knowledge(
            fact.id, ["positioning"], actor=resolve_owner_actor()
        )
        assert updated.tags == ["positioning"]
        # A second classification of an already-classified fact is rejected.
        with pytest.raises(ServiceError, match="already classified"):
            KnowledgeService(session).classify_knowledge(
                fact.id, ["user_research"], actor=resolve_owner_actor()
            )


def test_classify_candidate_tags_sentinel_to_canonical_once(tmp_path: Path) -> None:
    url = database(tmp_path, "classify-cand.db")
    with Session(get_engine(url)) as session:
        project, artifact = seed_artifact(session)
        candidate = KnowledgeCandidate(
            artifact_id=artifact.id,
            project_id=project.id,
            source_project_id=project.id,
            statement="Legacy candidate",
            tags=[LEGACY_UNCLASSIFIED],
            submitted_by_kind="owner",
            submitted_by_owner_id="owner",
            submitted_by="owner",
        )
        session.add(candidate)
        session.commit()
        updated = KnowledgeService(session).classify_candidate_tags(
            candidate.id, ["positioning"], actor=resolve_owner_actor()
        )
        assert updated.tags == ["positioning"]
        # A second classification is rejected.
        with pytest.raises(ServiceError, match="already classified"):
            KnowledgeService(session).classify_candidate_tags(
                candidate.id, ["user_research"], actor=resolve_owner_actor()
            )


def test_classify_rejects_unknown_tag(tmp_path: Path) -> None:
    url = database(tmp_path, "classify-bad.db")
    with Session(get_engine(url)) as session:
        project, artifact = seed_artifact(session)
        candidate = KnowledgeCandidate(
            artifact_id=artifact.id,
            project_id=project.id,
            source_project_id=project.id,
            statement="Legacy candidate",
            tags=[LEGACY_UNCLASSIFIED],
            submitted_by_kind="owner",
            submitted_by_owner_id="owner",
            submitted_by="owner",
        )
        session.add(candidate)
        session.commit()
        with pytest.raises(ServiceError, match="unknown knowledge tag"):
            KnowledgeService(session).classify_candidate_tags(
                candidate.id, ["not_a_real_tag"], actor=resolve_owner_actor()
            )


def test_deactivate_fact_owner_only_and_sets_inactive(tmp_path: Path) -> None:
    url = database(tmp_path, "deactivate.db")
    with Session(get_engine(url)) as session:
        project, artifact = seed_artifact(session)
        fact = make_fact(session, project, artifact, tags=["positioning"])
        deactivated = KnowledgeService(session).deactivate_fact(
            fact.id, "No longer applicable", actor=resolve_owner_actor()
        )
        assert deactivated.status == KnowledgeFactStatus.INACTIVE
