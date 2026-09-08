"""Skill System V1 domain service.

A Skill answers "how do we do it", whereas a KnowledgeFact answers "what do we
know". This service is therefore deliberately *isomorphic but separate* from
``knowledge_service``: the same three-stage lifecycle (candidate -> review
decision -> versioned immutable asset), the same owner-only governance, the
same ``IntegrityError -> 409`` concurrency policy and the same audit SSoT --
but its own enums, its own tables and its own content model.

Deliberate deviations from ``knowledge_service`` (Implementation Contract §2.2
/ §4), each recorded because they are intentional, not accidental:

* ``source_artifact_id`` is NULLABLE. A fact must cite an approved artifact; a
  Skill is procedural knowledge an owner may author directly. Provenance then
  falls back to ``submitted_by`` + the audit trail.
* ``version`` is minted server-side. Knowledge accepts a client ``version`` and
  validates ``head + 1``; Skill V1 removes that argument entirely, eliminating
  a whole class of 409 conflicts.
* Approving a candidate whose content hash equals the current head's is a 422,
  so版本号 cannot be inflated by approving byte-identical content.

Boundary: this module must never import ``workforce*`` / ``scheduler`` /
``execution`` / ``delegation`` / ``employee_bridge`` (enforced by the AST guard
in ``tests/test_skill_invariants.py``). Skills reach execution only through
``context_service`` -> ``TaskContext``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, or_, select

from aios.actor import ActorContext
from aios.audit import append_audit
from aios.models import (
    SKILL_NAME_PATTERN,
    Artifact,
    Capability,
    Skill,
    SkillCandidate,
    SkillCandidateStatus,
    SkillExecutionStrategy,
    SkillReviewDecision,
    SkillReviewDecisionValue,
    SkillStatus,
    now_utc,
)
from aios.services import ServiceError

_NAME_RE = re.compile(SKILL_NAME_PATTERN)


@dataclass(frozen=True)
class SkillReviewResult:
    decision: SkillReviewDecision
    skill: Skill | None


def _required(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ServiceError(422, f"{field} must be non-empty")
    return normalized


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_tool_bindings(tool_bindings: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize tool bindings so the content hash is insertion-order stable."""
    if not tool_bindings:
        return {}
    if not isinstance(tool_bindings, dict):
        raise ServiceError(422, "tool_bindings must be an object")
    return json.loads(json.dumps(tool_bindings, sort_keys=True, ensure_ascii=False))


def skill_content_hash(
    *,
    description: str,
    capability_id: str,
    steps: list[dict[str, Any]],
    tool_bindings: dict[str, Any],
    execution_strategy: str,
) -> str:
    """Fingerprint the execution-relevant content of a skill.

    Used for idempotent submission (candidate identity) and for rejecting a
    re-approval whose content is identical to the current approved head. It
    deliberately excludes ``name`` / ``project_id``: those are identity and
    scope, not content.
    """
    payload = {
        "capability_id": capability_id,
        "description": description,
        "execution_strategy": str(execution_strategy),
        "steps": steps,
        "tool_bindings": tool_bindings,
    }
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _assert_skill_owner_actor(actor: ActorContext) -> None:
    """Skill governance is owner-only; there is no agent self-publish path.

    Mirrors ``knowledge_service._assert_knowledge_owner_actor``: a ``system``
    or ``agent`` actor -- or an owner context missing its ``owner_id`` --
    receives 403. A Skill enters the agent prompt, so its governance level is
    at least as strict as a KnowledgeFact's.
    """
    if actor.kind != "owner" or not actor.owner_id:
        raise ServiceError(403, "skill formal actions require owner identity")


def _scope_filter(project_id: str | None):
    """Company scope (project_id IS NULL) is visible to every project."""
    return or_(Skill.project_id.is_(None), Skill.project_id == project_id)


class SkillService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ read

    def list_candidates(
        self,
        *,
        project_id: str | None = None,
        status: SkillCandidateStatus | None = None,
        actor: ActorContext,
    ) -> list[SkillCandidate]:
        _assert_skill_owner_actor(actor)
        statement = select(SkillCandidate)
        if project_id is not None:
            statement = statement.where(
                or_(
                    SkillCandidate.project_id.is_(None),
                    SkillCandidate.project_id == project_id,
                )
            )
        if status is not None:
            statement = statement.where(SkillCandidate.status == status)
        return list(self.session.exec(statement.order_by(SkillCandidate.created_at)))

    def list_skills(
        self,
        *,
        project_id: str | None = None,
        capability_id: str | None = None,
        name: str | None = None,
        status: SkillStatus | None = None,
        actor: ActorContext,
    ) -> list[Skill]:
        _assert_skill_owner_actor(actor)
        statement = select(Skill)
        if project_id is not None:
            statement = statement.where(_scope_filter(project_id))
        if capability_id is not None:
            statement = statement.where(Skill.capability_id == capability_id)
        if name is not None:
            statement = statement.where(Skill.name == name)
        if status is not None:
            statement = statement.where(Skill.status == status)
        return list(self.session.exec(statement.order_by(Skill.name, Skill.version)))

    def get_skill(self, skill_id: str, *, actor: ActorContext) -> Skill:
        _assert_skill_owner_actor(actor)
        skill = self.session.get(Skill, skill_id)
        if skill is None:
            raise ServiceError(404, "Skill not found")
        return skill

    # --------------------------------------------------------------- submit

    def submit_candidate(
        self,
        name: str,
        description: str,
        capability_id: str,
        steps: list[dict[str, Any]],
        tool_bindings: dict[str, Any] | None,
        execution_strategy: str,
        *,
        project_id: str | None = None,
        source_artifact_id: str | None = None,
        actor: ActorContext,
    ) -> SkillCandidate:
        """Propose a skill. Owner-only, idempotent by (name, content, scope).

        ``project_id`` is the single source of truth for scope: ``None`` =>
        company-wide, otherwise project-scoped. Provenance is always resolvable:
        a cited artifact supplies ``source_project_id``; otherwise the effective
        project does. A company-scoped skill with no artifact has no provenance
        source at all and is rejected.
        """
        _assert_skill_owner_actor(actor)
        name = _required(name, "name")
        if not _NAME_RE.match(name):
            raise ServiceError(422, "name must be a lowercase slug (3-64 chars)")
        description = _required(description, "description")
        if not isinstance(steps, list) or not steps:
            raise ServiceError(422, "steps must be a non-empty list")
        if any(not isinstance(step, dict) for step in steps):
            raise ServiceError(422, "each step must be an object")
        try:
            strategy = SkillExecutionStrategy(execution_strategy)
        except ValueError as exc:
            raise ServiceError(422, "execution_strategy is invalid") from exc

        capability = self.session.get(Capability, capability_id)
        if capability is None:
            raise ServiceError(404, "Capability not found")

        source_project_id: str | None = None
        if source_artifact_id is not None:
            artifact = self.session.get(Artifact, source_artifact_id)
            if artifact is None:
                raise ServiceError(404, "Source Artifact not found")
            if artifact.project_id is None:
                raise ServiceError(
                    422, "Source Artifact must belong to a project to source a skill"
                )
            source_project_id = artifact.project_id
            if project_id is not None and project_id != source_project_id:
                raise ServiceError(
                    422, "project-scoped candidate must match its source project"
                )
        elif project_id is not None:
            source_project_id = project_id
        else:
            raise ServiceError(
                422,
                "company-scoped skill must cite a source artifact or source project",
            )

        normalized_bindings = _canonical_tool_bindings(tool_bindings)
        content_hash = skill_content_hash(
            description=description,
            capability_id=capability.id,
            steps=steps,
            tool_bindings=normalized_bindings,
            execution_strategy=str(strategy),
        )

        existing = self.session.exec(
            select(SkillCandidate).where(
                SkillCandidate.name == name,
                SkillCandidate.content_hash == content_hash,
                SkillCandidate.project_id == project_id,
            )
        ).first()
        if existing is not None:
            return existing

        candidate = SkillCandidate(
            name=name,
            description=description,
            capability_id=capability.id,
            steps=steps,
            tool_bindings=normalized_bindings,
            execution_strategy=str(strategy),
            project_id=project_id,
            source_project_id=source_project_id,
            source_artifact_id=source_artifact_id,
            content_hash=content_hash,
            submitted_by_kind=actor.kind,
            submitted_by_owner_id=actor.owner_id if actor.kind == "owner" else None,
            submitted_by_agent_id=actor.agent_id if actor.kind == "agent" else None,
            submitted_by=actor.derive_submitted_by(),
        )
        try:
            self.session.add(candidate)
            self.session.flush()
            append_audit(
                self.session,
                actor=candidate.submitted_by,
                action="skill.candidate.created",
                resource_type="skill_candidate",
                resource_id=candidate.id,
                project_id=candidate.project_id,
                task_id=None,
                before={},
                after={
                    "candidate_id": candidate.id,
                    "name": candidate.name,
                    "capability_id": candidate.capability_id,
                    "execution_strategy": candidate.execution_strategy,
                    "content_hash": candidate.content_hash,
                    "scope": "company" if project_id is None else "project",
                    "project_id": candidate.project_id,
                    "source_project_id": candidate.source_project_id,
                    "source_artifact_id": candidate.source_artifact_id,
                    "status": candidate.status.value,
                    "submitted_by_kind": candidate.submitted_by_kind,
                },
                idempotency_key=f"audit:skill:candidate:{candidate.id}:created",
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(candidate)
        return candidate

    # --------------------------------------------------------------- review

    def review_candidate(
        self,
        candidate_id: str,
        decision: SkillReviewDecisionValue | str,
        rationale: str,
        *,
        actor: ActorContext,
    ) -> SkillReviewResult:
        try:
            decision = SkillReviewDecisionValue(decision)
        except ValueError as exc:
            raise ServiceError(422, "Review decision is invalid") from exc
        rationale = _required(rationale, "rationale")
        _assert_skill_owner_actor(actor)
        candidate = self.session.get(SkillCandidate, candidate_id)
        if candidate is None:
            raise ServiceError(404, "Skill candidate not found")
        reviewer = actor.derive_reviewer()
        replay = self._review_replay(candidate, decision, reviewer, rationale)
        if replay is not None:
            return replay
        # Fail-closed: a skill whose capability vanished must never be published
        # (it could never be selected, and it would break provenance).
        if self.session.get(Capability, candidate.capability_id) is None:
            raise ServiceError(409, "Skill capability reference is missing")
        review = SkillReviewDecision(
            candidate_id=candidate.id,
            decision=decision,
            reviewer_kind=actor.kind,
            reviewer_owner_id=actor.owner_id if actor.kind == "owner" else None,
            reviewer_agent_id=actor.agent_id if actor.kind == "agent" else None,
            reviewer=reviewer,
            rationale=rationale,
        )
        if decision == SkillReviewDecisionValue.REJECT:
            return self._reject(candidate, review)
        return self._approve(candidate, review)

    def _review_replay(
        self,
        candidate: SkillCandidate,
        decision: SkillReviewDecisionValue,
        reviewer: str,
        rationale: str,
    ) -> SkillReviewResult | None:
        existing = self.session.exec(
            select(SkillReviewDecision).where(
                SkillReviewDecision.candidate_id == candidate.id
            )
        ).first()
        if existing is None:
            if candidate.status != SkillCandidateStatus.DRAFT:
                raise ServiceError(409, "Skill candidate is already terminal")
            return None
        skill = self.session.exec(
            select(Skill).where(Skill.source_candidate_id == candidate.id)
        ).first()
        matches = (
            existing.decision == decision
            and existing.reviewer == reviewer
            and existing.rationale == rationale
        )
        if not matches:
            # Never silently flip a recorded decision -- that would rewrite history.
            raise ServiceError(409, "Review retry conflicts with terminal decision")
        return SkillReviewResult(existing, skill)

    def _next_version(self, name: str, project_id: str | None) -> tuple[int, str | None]:
        """Return (next_version, current_head_id) for a skill name in a scope."""
        rows = list(
            self.session.exec(
                select(Skill).where(
                    Skill.name == name,
                    Skill.project_id == project_id,
                )
            )
        )
        head = max(
            (row for row in rows if row.status == SkillStatus.APPROVED),
            key=lambda row: row.version,
            default=None,
        )
        return (
            head.version + 1 if head is not None else 1,
            head.id if head is not None else None,
        )

    def _reject(
        self,
        candidate: SkillCandidate,
        review: SkillReviewDecision,
    ) -> SkillReviewResult:
        candidate.status = SkillCandidateStatus.REJECTED
        candidate.updated_at = now_utc()
        try:
            self.session.add_all([candidate, review])
            self.session.flush()
            append_audit(
                self.session,
                actor=review.reviewer,
                action="skill.candidate.rejected",
                resource_type="skill_candidate",
                resource_id=candidate.id,
                project_id=candidate.project_id,
                task_id=None,
                before={"status": SkillCandidateStatus.DRAFT.value},
                after={
                    "status": candidate.status.value,
                    "review_decision_id": review.id,
                    "reviewer_kind": review.reviewer_kind,
                    "rationale": review.rationale,
                },
                idempotency_key=f"audit:skill:candidate:{candidate.id}:rejected",
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(review)
        return SkillReviewResult(review, None)

    def _approve(
        self,
        candidate: SkillCandidate,
        review: SkillReviewDecision,
    ) -> SkillReviewResult:
        version, head_id = self._next_version(candidate.name, candidate.project_id)
        head = self.session.get(Skill, head_id) if head_id else None
        if head is not None and head.content_hash == candidate.content_hash:
            raise ServiceError(
                422, "Skill content is identical to the current approved version"
            )
        candidate.status = SkillCandidateStatus.APPROVED
        candidate.updated_at = now_utc()
        skill = Skill(
            name=candidate.name,
            description=candidate.description,
            capability_id=candidate.capability_id,
            steps=list(candidate.steps),
            tool_bindings=dict(candidate.tool_bindings),
            execution_strategy=candidate.execution_strategy,
            project_id=candidate.project_id,
            source_project_id=candidate.source_project_id,
            source_artifact_id=candidate.source_artifact_id,
            content_hash=candidate.content_hash,
            version=version,
            status=SkillStatus.APPROVED,
            source_candidate_id=candidate.id,
            review_decision_id=review.id,
            supersedes_skill_id=head.id if head is not None else None,
        )
        try:
            # Order is load-bearing: the outgoing head must be demoted (and the
            # single-active-head slot freed) BEFORE the new row is inserted.
            self.session.add_all([candidate, review])
            self.session.flush()
            if head is not None:
                head.status = SkillStatus.SUPERSEDED
                head.updated_at = now_utc()
                self.session.add(head)
                self.session.flush()
            self.session.add(skill)
            self.session.flush()
            action = "skill.superseded" if head is not None else "skill.approved"
            append_audit(
                self.session,
                actor=review.reviewer,
                action=action,
                resource_type="skill",
                resource_id=skill.id,
                project_id=skill.project_id,
                task_id=None,
                before={"predecessor_id": head.id if head is not None else None},
                after={
                    "skill_id": skill.id,
                    "name": skill.name,
                    "version": skill.version,
                    "candidate_id": candidate.id,
                    "review_decision_id": review.id,
                    "capability_id": skill.capability_id,
                    "content_hash": skill.content_hash,
                    "scope": "company" if skill.project_id is None else "project",
                    "project_id": skill.project_id,
                    "supersedes_skill_id": skill.supersedes_skill_id,
                    "reviewer_kind": review.reviewer_kind,
                    "rationale": review.rationale,
                },
                idempotency_key=f"audit:skill:{skill.id}:approved",
            )
            self.session.commit()
        except (IntegrityError, OperationalError) as exc:
            self.session.rollback()
            raise ServiceError(409, "Skill approval conflicts with current state") from exc
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(review)
        self.session.refresh(skill)
        return SkillReviewResult(review, skill)

    # ----------------------------------------------------------- deactivate

    def deactivate_skill(
        self, skill_id: str, rationale: str, *, actor: ActorContext
    ) -> Skill:
        _assert_skill_owner_actor(actor)
        rationale = _required(rationale, "rationale")
        skill = self.session.get(Skill, skill_id)
        if skill is None:
            raise ServiceError(404, "Skill not found")
        if skill.status != SkillStatus.APPROVED:
            raise ServiceError(409, "Only an approved skill can be deactivated")
        # Deactivation mints no new version: it flips the only mutable column.
        skill.status = SkillStatus.INACTIVE
        skill.updated_at = now_utc()
        try:
            self.session.add(skill)
            self.session.flush()
            append_audit(
                self.session,
                actor=actor.derive_reviewer(),
                action="skill.deactivated",
                resource_type="skill",
                resource_id=skill.id,
                project_id=skill.project_id,
                task_id=None,
                before={"status": SkillStatus.APPROVED.value},
                after={
                    "status": skill.status.value,
                    "name": skill.name,
                    "version": skill.version,
                    "reviewer_kind": actor.kind,
                    "rationale": rationale,
                },
                idempotency_key=f"audit:skill:{skill.id}:deactivated",
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(skill)
        return skill
