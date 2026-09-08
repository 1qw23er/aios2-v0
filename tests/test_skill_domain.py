"""Skill System V1 -- domain lifecycle tests (Implementation Contract §16).

Covers: candidate creation, slug / step validation, idempotent submission,
reject, approve -> mint v1, same-content re-approval (422), supersession,
deactivate, version immutability and content-hash stability.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, select

from aios.actor import ActorContext, resolve_agent_actor, resolve_owner_actor
from aios.audit import AuditLog
from aios.db import get_engine, run_migrations
from aios.models import (
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
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
from aios.skill_service import SkillService, skill_content_hash


def database(tmp_path: Path, name: str) -> str:
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    run_migrations(url)
    return url


def seed(session: Session) -> tuple[Project, Capability, Artifact]:
    project = Project(name="Skills", objective="Reusable procedures")
    session.add(project)
    session.flush()
    capability = Capability(name="drafting", description="Write structured drafts")
    session.add(capability)
    session.flush()
    artifact = Artifact(
        project_id=project.id,
        type=ArtifactType.JSON,
        uri="skill-source.json",
        checksum="sha256:skill-source",
        review_status=ArtifactReviewStatus.APPROVED,
    )
    session.add(artifact)
    session.commit()
    return project, capability, artifact


def submit(
    session: Session,
    capability_id: str,
    *,
    name: str = "outline_first",
    description: str = "Always draft an outline before writing",
    steps: list[dict] | None = None,
    tool_bindings: dict | None = None,
    execution_strategy: str = "single_pass",
    project_id: str | None = None,
    source_artifact_id: str | None = None,
    actor: ActorContext | None = None,
) -> SkillCandidate:
    return SkillService(session).submit_candidate(
        name,
        description,
        capability_id,
        steps if steps is not None else [{"step": 1, "do": "outline"}],
        tool_bindings or {},
        execution_strategy,
        project_id=project_id,
        source_artifact_id=source_artifact_id,
        actor=actor or resolve_owner_actor(),
    )


def test_submit_candidate_creates_draft_with_typed_identity(tmp_path: Path) -> None:
    url = database(tmp_path, "create.db")
    with Session(get_engine(url)) as session:
        project, capability, artifact = seed(session)
        candidate = submit(
            session,
            capability.id,
            project_id=project.id,
            source_artifact_id=artifact.id,
        )
        assert candidate.status == SkillCandidateStatus.DRAFT
        assert candidate.submitted_by == "owner:owner"
        assert candidate.submitted_by_kind == "owner"
        assert candidate.project_id == project.id
        assert candidate.source_project_id == project.id
        assert len(candidate.content_hash) == 64
        audits = list(session.exec(select(AuditLog)))
        assert [audit.action for audit in audits] == ["skill.candidate.created"]
        assert audits[0].idempotency_key == (
            f"audit:skill:candidate:{candidate.id}:created"
        )


def test_submit_rejects_bad_slug_empty_steps_and_bad_strategy(tmp_path: Path) -> None:
    url = database(tmp_path, "invalid.db")
    with Session(get_engine(url)) as session:
        project, capability, _ = seed(session)
        service = SkillService(session)
        base = dict(capability_id=capability.id, project_id=project.id)
        # Slug: uppercase, too short, leading digit.
        for bad in ("Outline", "ab", "1_outline"):
            with pytest.raises(ServiceError, match="lowercase slug"):
                submit(session, name=bad, **base)
        # Steps: empty list, or a list of non-objects.
        with pytest.raises(ServiceError, match="non-empty list"):
            submit(session, steps=[], **base)
        with pytest.raises(ServiceError, match="must be an object"):
            submit(session, steps=["outline"], **base)
        # Strategy must come from the controlled vocabulary.
        with pytest.raises(ServiceError, match="execution_strategy"):
            submit(session, execution_strategy="yolo", **base)
        # C1: a Skill is "how", not a fact -- no steps means no skill.
        assert service is not None


def test_submit_requires_known_capability_and_resolvable_provenance(
    tmp_path: Path,
) -> None:
    url = database(tmp_path, "refs.db")
    with Session(get_engine(url)) as session:
        project, capability, artifact = seed(session)
        with pytest.raises(ServiceError, match="Capability not found"):
            submit(session, "cap_missing", project_id=project.id)
        with pytest.raises(ServiceError, match="Source Artifact not found"):
            submit(session, capability.id, source_artifact_id="art_missing")
        # Company scope with no artifact and no project has no provenance at all.
        with pytest.raises(ServiceError, match="company-scoped"):
            submit(session, capability.id)
        # A project-scoped candidate must match the project its artifact came
        # from -- no silent cross-project widening.
        other = Project(name="Other", objective="x")
        session.add(other)
        session.commit()
        with pytest.raises(ServiceError, match="match its source project"):
            submit(
                session,
                capability.id,
                project_id=other.id,
                source_artifact_id=artifact.id,
            )


def test_duplicate_submission_is_idempotent(tmp_path: Path) -> None:
    url = database(tmp_path, "dup.db")
    with Session(get_engine(url)) as session:
        project, capability, _ = seed(session)
        first = submit(
            session,
            capability.id,
            project_id=project.id,
            tool_bindings={"alpha": 1, "beta": 2},
        )
        # Key insertion order must not change identity (bindings are canonicalized).
        second = submit(
            session,
            capability.id,
            project_id=project.id,
            tool_bindings={"beta": 2, "alpha": 1},
        )
        assert second.id == first.id
        assert len(list(session.exec(select(SkillCandidate)))) == 1
        # Different content (different hash) is a genuinely new candidate.
        third = submit(
            session,
            capability.id,
            project_id=project.id,
            steps=[{"step": 1, "do": "outline"}, {"step": 2, "do": "draft"}],
        )
        assert third.id != first.id


def test_reject_is_terminal_and_creates_no_skill(tmp_path: Path) -> None:
    url = database(tmp_path, "reject.db")
    with Session(get_engine(url)) as session:
        project, capability, _ = seed(session)
        candidate = submit(session, capability.id, project_id=project.id)
        result = SkillService(session).review_candidate(
            candidate.id,
            SkillReviewDecisionValue.REJECT,
            "Too vague",
            actor=resolve_owner_actor(),
        )
        assert result.skill is None
        assert candidate.status == SkillCandidateStatus.REJECTED
        assert len(list(session.exec(select(Skill)))) == 0
        # Replay of the same decision is a no-op...
        replay = SkillService(session).review_candidate(
            candidate.id,
            SkillReviewDecisionValue.REJECT,
            "Too vague",
            actor=resolve_owner_actor(),
        )
        assert replay.decision.id == result.decision.id
        # ...but a changed decision is a conflict, never a silent flip.
        with pytest.raises(ServiceError, match="conflicts with terminal decision"):
            SkillService(session).review_candidate(
                candidate.id,
                SkillReviewDecisionValue.APPROVE,
                "Actually fine",
                actor=resolve_owner_actor(),
            )
        actions = sorted(a.action for a in session.exec(select(AuditLog)))
        assert actions == ["skill.candidate.created", "skill.candidate.rejected"]


def test_approve_mints_v1_then_supersedes(tmp_path: Path) -> None:
    url = database(tmp_path, "approve.db")
    with Session(get_engine(url)) as session:
        project, capability, _ = seed(session)
        service = SkillService(session)
        candidate = submit(session, capability.id, project_id=project.id)
        result = service.review_candidate(
            candidate.id,
            SkillReviewDecisionValue.APPROVE,
            "Ready",
            actor=resolve_owner_actor(),
        )
        assert result.skill is not None
        v1 = result.skill
        assert v1.version == 1
        assert v1.status == SkillStatus.APPROVED
        assert v1.supersedes_skill_id is None
        assert v1.source_candidate_id == candidate.id
        assert candidate.status == SkillCandidateStatus.APPROVED

        # A second, different candidate for the same name supersedes v1.
        candidate2 = submit(
            session,
            capability.id,
            project_id=project.id,
            description="Outline, then draft, then tighten",
        )
        result2 = service.review_candidate(
            candidate2.id,
            SkillReviewDecisionValue.APPROVE,
            "Improved",
            actor=resolve_owner_actor(),
        )
        v2 = result2.skill
        assert v2 is not None and v2.version == 2
        assert v2.supersedes_skill_id == v1.id
        session.refresh(v1)
        assert v1.status == SkillStatus.SUPERSEDED
        # Exactly one active head per (name, scope).
        heads = [
            row
            for row in session.exec(select(Skill))
            if row.status == SkillStatus.APPROVED
        ]
        assert [row.version for row in heads] == [2]
        actions = sorted(a.action for a in session.exec(select(AuditLog)))
        assert "skill.approved" in actions and "skill.superseded" in actions


def test_identical_content_cannot_inflate_the_version(tmp_path: Path) -> None:
    """Approving byte-identical content must never mint a new version.

    Two DB-level guards cooperate here: ``uq_skill_candidate_identity`` makes a
    duplicate submission return the EXISTING candidate (which is already
    terminal, so review replays instead of re-approving), and ``_approve``
    refuses outright if a same-hash head is somehow reached. Net effect: the
    version number only ever advances for content that actually changed.
    """
    url = database(tmp_path, "identical.db")
    with Session(get_engine(url)) as session:
        project, capability, _ = seed(session)
        service = SkillService(session)
        first = submit(session, capability.id, project_id=project.id)
        v1 = service.review_candidate(
            first.id,
            SkillReviewDecisionValue.APPROVE,
            "Ready",
            actor=resolve_owner_actor(),
        ).skill
        assert v1 is not None and v1.version == 1

        # Re-submitting the exact same content returns the same (terminal)
        # candidate; reviewing it again is an idempotent replay, not a new mint.
        again = submit(session, capability.id, project_id=project.id)
        assert again.id == first.id
        replay = service.review_candidate(
            again.id,
            SkillReviewDecisionValue.APPROVE,
            "Ready",
            actor=resolve_owner_actor(),
        )
        assert replay.skill is not None and replay.skill.id == v1.id
        assert len(list(session.exec(select(Skill)))) == 1

        # The defensive guard itself: a head whose hash equals the candidate's
        # is refused even if the call is reached directly.
        with pytest.raises(ServiceError, match="identical to the current approved"):
            service._approve(
                first,
                SkillReviewDecision(
                    candidate_id=first.id,
                    decision=SkillReviewDecisionValue.APPROVE,
                    reviewer_kind="owner",
                    reviewer_owner_id="owner",
                    reviewer="owner:owner",
                    rationale="Again",
                ),
            )
        assert len(list(session.exec(select(Skill)))) == 1


def test_deactivate_mints_no_version_and_is_final(tmp_path: Path) -> None:
    url = database(tmp_path, "deactivate.db")
    with Session(get_engine(url)) as session:
        project, capability, _ = seed(session)
        service = SkillService(session)
        candidate = submit(session, capability.id, project_id=project.id)
        skill = service.review_candidate(
            candidate.id,
            SkillReviewDecisionValue.APPROVE,
            "Ready",
            actor=resolve_owner_actor(),
        ).skill
        assert skill is not None
        version_before = skill.version
        hash_before = skill.content_hash
        deactivated = service.deactivate_skill(
            skill.id, "No longer accurate", actor=resolve_owner_actor()
        )
        assert deactivated.status == SkillStatus.INACTIVE
        assert deactivated.version == version_before
        assert deactivated.content_hash == hash_before
        # An inactive skill cannot be deactivated again...
        with pytest.raises(ServiceError, match="Only an approved skill"):
            service.deactivate_skill(skill.id, "again", actor=resolve_owner_actor())
        # ...nor reactivated: recovery means a new candidate + new version.
        actions = sorted(a.action for a in session.exec(select(AuditLog)))
        assert "skill.deactivated" in actions


def test_published_skill_has_no_update_path_for_content(tmp_path: Path) -> None:
    url = database(tmp_path, "immutable.db")
    with Session(get_engine(url)) as session:
        project, capability, _ = seed(session)
        service = SkillService(session)
        candidate = submit(session, capability.id, project_id=project.id)
        skill = service.review_candidate(
            candidate.id,
            SkillReviewDecisionValue.APPROVE,
            "Ready",
            actor=resolve_owner_actor(),
        ).skill
        assert skill is not None
        # The service exposes no mutator for content columns: every content
        # column is copied from the candidate at mint time and never touched.
        mutators = [
            name
            for name in dir(SkillService)
            if name.startswith(("update", "patch", "edit", "set_", "change"))
        ]
        assert mutators == []
        assert skill.steps == candidate.steps
        assert skill.execution_strategy == candidate.execution_strategy


def test_governance_is_owner_only(tmp_path: Path) -> None:
    url = database(tmp_path, "governance.db")
    with Session(get_engine(url)) as session:
        project, capability, _ = seed(session)
        service = SkillService(session)
        agent = resolve_agent_actor("agent_1")
        with pytest.raises(ServiceError, match="owner identity"):
            submit(session, capability.id, project_id=project.id, actor=agent)
        candidate = submit(session, capability.id, project_id=project.id)
        with pytest.raises(ServiceError, match="owner identity"):
            service.review_candidate(
                candidate.id, SkillReviewDecisionValue.APPROVE, "Ready", actor=agent
            )
        with pytest.raises(ServiceError, match="owner identity"):
            service.list_skills(project_id=project.id, actor=agent)
        # A system actor is not an owner either (no self-approval path).
        system = ActorContext(kind="system")
        with pytest.raises(ServiceError, match="owner identity"):
            service.review_candidate(
                candidate.id, SkillReviewDecisionValue.APPROVE, "Ready", actor=system
            )


def test_content_hash_is_stable_and_scope_agnostic(tmp_path: Path) -> None:
    url = database(tmp_path, "hash.db")
    with Session(get_engine(url)) as session:
        project, capability, _ = seed(session)
        steps = [{"step": 1, "do": "outline"}]
        args = dict(
            description="D",
            capability_id=capability.id,
            steps=steps,
            tool_bindings={"z": 1, "a": {"n": 2, "m": 1}},
            execution_strategy="tool_first",
        )
        assert skill_content_hash(**args) == skill_content_hash(**args)
        different = dict(args, execution_strategy="iterative")
        assert skill_content_hash(**different) != skill_content_hash(**args)
        # Steps are ORDER-SENSITIVE: order is semantics.
        ordered = [{"step": 1, "do": "outline"}, {"step": 2, "do": "draft"}]
        assert skill_content_hash(**dict(args, steps=ordered)) != skill_content_hash(
            **dict(args, steps=list(reversed(ordered)))
        )


def test_review_decision_is_one_per_candidate(tmp_path: Path) -> None:
    url = database(tmp_path, "one_decision.db")
    with Session(get_engine(url)) as session:
        project, capability, _ = seed(session)
        candidate = submit(session, capability.id, project_id=project.id)
        SkillService(session).review_candidate(
            candidate.id,
            SkillReviewDecisionValue.APPROVE,
            "Ready",
            actor=resolve_owner_actor(),
        )
        assert len(list(session.exec(select(SkillReviewDecision)))) == 1
        assert (
            session.exec(
                select(SkillReviewDecision).where(
                    SkillReviewDecision.candidate_id == candidate.id
                )
            ).first()
            is not None
        )
