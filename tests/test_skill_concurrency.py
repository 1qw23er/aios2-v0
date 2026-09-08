"""Skill System V1 -- concurrency / idempotency tests (Contract §16).

Strategy: real threads against SQLite are environment-sensitive (the repo
already carries one known WAL flake in test_pilot2_models), so the race
conditions are reproduced *at the constraint layer* -- we seed the exact
conflicting row a racing transaction would have committed, then call the
service and assert the documented IntegrityError -> 409 mapping. That checks
the same guarantee (DB constraint fires, service maps it to 409) without
depending on scheduler timing.

Covers: duplicate submit (UNIQUE -> idempotent return), concurrent review
(one-decision-per-candidate UNIQUE -> 409), concurrent approve of the same
candidate (active-head partial index -> 409), duplicate deactivate (409).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from aios.actor import resolve_owner_actor
from aios.db import get_engine, run_migrations
from aios.models import (
    Capability,
    Project,
    Skill,
    SkillCandidate,
    SkillCandidateStatus,
    SkillReviewDecision,
    SkillReviewDecisionValue,
    SkillStatus,
)
from aios.services import ServiceError
from aios.skill_service import SkillService


def database(tmp_path: Path, name: str) -> str:
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    run_migrations(url)
    return url


def _seed(session: Session) -> tuple[Project, Capability]:
    project = Project(name="Race", objective="Concurrent safety")
    capability = Capability(name="drafting", description="Writes")
    session.add_all([project, capability])
    session.commit()
    return project, capability


def _candidate(session: Session, capability: Capability, project: Project) -> SkillCandidate:
    service = SkillService(session)
    return service.submit_candidate(
        "outline_first",
        "Always outline first",
        capability.id,
        [{"step": 1, "do": "outline"}],
        {},
        "single_pass",
        project_id=project.id,
        actor=resolve_owner_actor(),
    )


def test_duplicate_submit_is_idempotent_not_409(tmp_path: Path) -> None:
    url = database(tmp_path, "conc_submit.db")
    with Session(get_engine(url)) as session:
        project, capability = _seed(session)
        service = SkillService(session)
        kwargs = dict(
            name="outline_first",
            description="Always outline first",
            capability_id=capability.id,
            steps=[{"step": 1, "do": "outline"}],
            tool_bindings={},
            execution_strategy="single_pass",
            project_id=project.id,
        )
        first = service.submit_candidate(**kwargs, actor=resolve_owner_actor())
        # Same (name, content, scope) -> the existing candidate, no second row.
        second = service.submit_candidate(**kwargs, actor=resolve_owner_actor())
        assert second.id == first.id
        rows = list(session.exec(select(SkillCandidate)))
        assert len(rows) == 1


def test_concurrent_review_conflicts_to_409(tmp_path: Path) -> None:
    """Two review decisions on one candidate: the UNIQUE constraint wins."""
    url = database(tmp_path, "conc_review.db")
    with Session(get_engine(url)) as session:
        project, capability = _seed(session)
        candidate = _candidate(session, capability, project)
        # The racing transaction already committed its decision row:
        session.add(
            SkillReviewDecision(
                candidate_id=candidate.id,
                decision=SkillReviewDecisionValue.REJECT,
                rationale="raced",
                reviewer_kind="owner",
                reviewer_owner_id="owner",
                reviewer="owner:owner",
            )
        )
        session.commit()
        with pytest.raises(ServiceError, match="409|conflict"):
            SkillService(session).review_candidate(
                candidate.id,
                SkillReviewDecisionValue.APPROVE,
                "too late",
                actor=resolve_owner_actor(),
            )


def test_concurrent_approve_same_candidate_conflicts_to_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second active head for the same (name, scope) cannot be minted.

    Faithful race simulation: ``_next_version`` is the read-only snapshot the
    approving transaction takes. A racing transaction that commits its head
    between that snapshot and the INSERT leaves our transaction holding a stale
    "(1, no head)" answer while the DB already has an approved v1 -- the exact
    interleaving the single-active-head partial unique index exists for. We
    reproduce it by stubbing the snapshot to the stale value while the row is
    present, and assert the documented IntegrityError -> 409 mapping.
    """
    url = database(tmp_path, "conc_approve.db")
    with Session(get_engine(url)) as session:
        project, capability = _seed(session)
        candidate = _candidate(session, capability, project)
        # Valid FK provenance for the racing head (it mints from its own
        # already-terminal candidate+decision pair, as a real racing approve
        # would have).
        racer_candidate = SkillCandidate(
            name="racer_source",
            description="racing variant",
            capability_id=capability.id,
            steps=[{"step": 1, "do": "raced-first"}],
            tool_bindings={},
            execution_strategy="single_pass",
            project_id=project.id,
            source_project_id=project.id,
            content_hash="e" * 64,
            status=SkillCandidateStatus.APPROVED,
            submitted_by_kind="owner",
            submitted_by_owner_id="owner",
            submitted_by="owner:owner",
        )
        session.add(racer_candidate)
        session.flush()
        racer_decision = SkillReviewDecision(
            candidate_id=racer_candidate.id,
            decision=SkillReviewDecisionValue.APPROVE,
            rationale="raced",
            reviewer_kind="owner",
            reviewer_owner_id="owner",
            reviewer="owner:owner",
        )
        session.add(racer_decision)
        session.flush()
        session.add(
            Skill(
                name=candidate.name,
                description="racing variant",
                capability_id=capability.id,
                steps=[{"step": 1, "do": "raced-first"}],
                tool_bindings={},
                execution_strategy="single_pass",
                project_id=candidate.project_id,
                source_project_id=candidate.source_project_id,
                source_candidate_id=racer_candidate.id,
                review_decision_id=racer_decision.id,
                version=1,
                status=SkillStatus.APPROVED,
                content_hash="f" * 64,  # different content: no 422 shortcut
            )
        )
        session.commit()

        # Stale snapshot: taken before the racing commit.
        monkeypatch.setattr(
            SkillService, "_next_version", lambda self, name, project_id: (1, None)
        )
        with pytest.raises(ServiceError, match="conflicts with current state"):
            SkillService(session).review_candidate(
                candidate.id,
                SkillReviewDecisionValue.APPROVE,
                "mint me too",
                actor=resolve_owner_actor(),
            )
        monkeypatch.undo()
        # The DB is untouched by the losing transaction: still exactly one head.
        heads = list(
            session.exec(select(Skill).where(Skill.status == SkillStatus.APPROVED))
        )
        assert len(heads) == 1


def test_identical_content_cannot_inflate_versions(tmp_path: Path) -> None:
    """Version inflation is stopped at TWO independent layers.

    Layer 1 (this test): the candidate identity UNIQUE constraint --
    ``(name, content_hash, project_id)`` -- makes a byte-identical
    re-submission impossible to even store as a second candidate row.
    Layer 2 (covered in test_skill_domain via a direct ``_approve`` call): the
    defensive 422 in the mint path, which is unreachable through the normal
    flow precisely because layer 1 fires first.
    """
    url = database(tmp_path, "conc_identical.db")
    with Session(get_engine(url)) as session:
        project, capability = _seed(session)
        candidate = _candidate(session, capability, project)
        twin = SkillCandidate(
            name=candidate.name,
            description=candidate.description,
            capability_id=capability.id,
            steps=list(candidate.steps),
            tool_bindings=dict(candidate.tool_bindings),
            execution_strategy=candidate.execution_strategy,
            project_id=candidate.project_id,
            source_project_id=candidate.source_project_id,
            content_hash=candidate.content_hash,
            submitted_by_kind="owner",
            submitted_by_owner_id="owner",
            submitted_by="owner:owner",
        )
        session.add(twin)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        rows = list(session.exec(select(SkillCandidate)))
        assert len(rows) == 1


def test_duplicate_deactivate_conflicts_to_409(tmp_path: Path) -> None:
    url = database(tmp_path, "conc_deactivate.db")
    with Session(get_engine(url)) as session:
        project, capability = _seed(session)
        service = SkillService(session)
        candidate = _candidate(session, capability, project)
        result = service.review_candidate(
            candidate.id,
            SkillReviewDecisionValue.APPROVE,
            "good",
            actor=resolve_owner_actor(),
        )
        skill = result.skill
        assert skill is not None
        service.deactivate_skill(skill.id, "obsolete", actor=resolve_owner_actor())
        session.refresh(skill)
        assert skill.status == SkillStatus.INACTIVE
        with pytest.raises(ServiceError, match="Only an approved skill"):
            service.deactivate_skill(skill.id, "again", actor=resolve_owner_actor())


def test_review_idempotent_replay_returns_same_decision(tmp_path: Path) -> None:
    """Replaying the identical review decision is idempotent (no 409)."""
    url = database(tmp_path, "conc_replay.db")
    with Session(get_engine(url)) as session:
        project, capability = _seed(session)
        service = SkillService(session)
        candidate = _candidate(session, capability, project)
        first = service.review_candidate(
            candidate.id,
            SkillReviewDecisionValue.APPROVE,
            "good",
            actor=resolve_owner_actor(),
        )
        replay = service.review_candidate(
            candidate.id,
            SkillReviewDecisionValue.APPROVE,
            "good",
            actor=resolve_owner_actor(),
        )
        assert replay.decision.id == first.decision.id
        assert replay.skill is not None and first.skill is not None
        assert replay.skill.id == first.skill.id
        assert replay.decision.reviewed_at == first.decision.reviewed_at
