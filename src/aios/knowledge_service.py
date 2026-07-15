from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, select

from aios.audit import append_audit
from aios.models import (
    Artifact,
    ArtifactReviewStatus,
    KnowledgeCandidate,
    KnowledgeCandidateStatus,
    KnowledgeFact,
    KnowledgeFactStatus,
    KnowledgeReviewDecision,
    KnowledgeReviewDecisionValue,
    now_utc,
)
from aios.services import ServiceError


@dataclass(frozen=True)
class KnowledgeReviewResult:
    decision: KnowledgeReviewDecision
    fact: KnowledgeFact | None


def _required(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ServiceError(422, f"{field} must be non-empty")
    return normalized


class KnowledgeService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def submit_candidate(
        self,
        artifact_id: str,
        statement: str,
        project_id: str | None,
        submitted_by: str,
    ) -> KnowledgeCandidate:
        artifact = self.session.get(Artifact, artifact_id)
        if artifact is None:
            raise ServiceError(404, "Source Artifact not found")
        if artifact.review_status != ArtifactReviewStatus.APPROVED:
            raise ServiceError(422, "Source Artifact must be approved")
        if project_id != artifact.project_id:
            raise ServiceError(422, "Candidate scope must exactly match Artifact scope")
        candidate = KnowledgeCandidate(
            artifact_id=artifact.id,
            project_id=project_id,
            statement=_required(statement, "statement"),
            submitted_by=_required(submitted_by, "submitted_by"),
        )
        try:
            self.session.add(candidate)
            self.session.flush()
            append_audit(
                self.session,
                actor=candidate.submitted_by,
                action="knowledge.candidate.created",
                resource_type="knowledge_candidate",
                resource_id=candidate.id,
                project_id=candidate.project_id,
                task_id=None,
                before={},
                after={
                    "candidate_id": candidate.id,
                    "artifact_id": artifact.id,
                    "scope": "company" if project_id is None else "project",
                    "project_id": project_id,
                    "status": candidate.status.value,
                },
                idempotency_key=f"audit:knowledge:candidate:{candidate.id}:created",
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(candidate)
        return candidate

    def review_candidate(
        self,
        candidate_id: str,
        decision: KnowledgeReviewDecisionValue,
        reviewer: str,
        rationale: str,
        *,
        series_id: str | None = None,
        version: int | None = None,
        supersedes_fact_id: str | None = None,
    ) -> KnowledgeReviewResult:
        try:
            decision = KnowledgeReviewDecisionValue(decision)
        except ValueError as exc:
            raise ServiceError(422, "Review decision is invalid") from exc
        reviewer = _required(reviewer, "reviewer")
        rationale = _required(rationale, "rationale")
        candidate = self.session.get(KnowledgeCandidate, candidate_id)
        if candidate is None:
            raise ServiceError(404, "Knowledge candidate not found")
        replay = self._review_replay(
            candidate,
            decision,
            reviewer,
            rationale,
            series_id,
            version,
            supersedes_fact_id,
        )
        if replay is not None:
            return replay
        artifact = self.session.get(Artifact, candidate.artifact_id)
        if artifact is None or artifact.review_status != ArtifactReviewStatus.APPROVED:
            raise ServiceError(409, "Source Artifact is no longer approved")
        if candidate.project_id != artifact.project_id:
            raise ServiceError(409, "Candidate and Artifact scopes conflict")
        review = KnowledgeReviewDecision(
            candidate_id=candidate.id,
            decision=decision,
            reviewer=reviewer,
            rationale=rationale,
        )
        if decision == KnowledgeReviewDecisionValue.REJECT:
            return self._reject(candidate, review)
        normalized_series = _required(series_id or "", "series_id")
        if version is None or version < 1:
            raise ServiceError(422, "version must be a positive integer")
        return self._approve(
            candidate,
            review,
            normalized_series,
            version,
            supersedes_fact_id,
        )

    def _review_replay(
        self,
        candidate: KnowledgeCandidate,
        decision: KnowledgeReviewDecisionValue,
        reviewer: str,
        rationale: str,
        series_id: str | None,
        version: int | None,
        supersedes_fact_id: str | None,
    ) -> KnowledgeReviewResult | None:
        existing = self.session.exec(
            select(KnowledgeReviewDecision).where(
                KnowledgeReviewDecision.candidate_id == candidate.id
            )
        ).first()
        if existing is None:
            if candidate.status != KnowledgeCandidateStatus.DRAFT:
                raise ServiceError(409, "Knowledge candidate is already terminal")
            return None
        fact = self.session.exec(
            select(KnowledgeFact).where(KnowledgeFact.source_candidate_id == candidate.id)
        ).first()
        matches = (
            existing.decision == decision
            and existing.reviewer == reviewer
            and existing.rationale == rationale
        )
        if decision == KnowledgeReviewDecisionValue.APPROVE:
            matches = matches and fact is not None and (
                fact.series_id == (series_id or "").strip()
                and fact.version == version
                and fact.supersedes_fact_id == supersedes_fact_id
            )
        elif fact is not None or any(
            value is not None for value in (series_id, version, supersedes_fact_id)
        ):
            matches = False
        if not matches:
            raise ServiceError(409, "Review retry conflicts with terminal decision")
        return KnowledgeReviewResult(existing, fact)

    def _reject(
        self,
        candidate: KnowledgeCandidate,
        review: KnowledgeReviewDecision,
    ) -> KnowledgeReviewResult:
        candidate.status = KnowledgeCandidateStatus.REJECTED
        candidate.updated_at = now_utc()
        try:
            self.session.add_all([candidate, review])
            self.session.flush()
            append_audit(
                self.session,
                actor=review.reviewer,
                action="knowledge.candidate.rejected",
                resource_type="knowledge_candidate",
                resource_id=candidate.id,
                project_id=candidate.project_id,
                task_id=None,
                before={"status": KnowledgeCandidateStatus.DRAFT.value},
                after={
                    "status": candidate.status.value,
                    "review_decision_id": review.id,
                    "rationale": review.rationale,
                },
                idempotency_key=f"audit:knowledge:candidate:{candidate.id}:rejected",
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(review)
        return KnowledgeReviewResult(review, None)

    def _approve(
        self,
        candidate: KnowledgeCandidate,
        review: KnowledgeReviewDecision,
        series_id: str,
        version: int,
        supersedes_fact_id: str | None,
    ) -> KnowledgeReviewResult:
        rows = list(
            self.session.exec(
                select(KnowledgeFact).where(
                    KnowledgeFact.series_id == series_id,
                    KnowledgeFact.project_id == candidate.project_id,
                )
            )
        )
        head = next((row for row in rows if row.status == KnowledgeFactStatus.APPROVED), None)
        if not rows:
            if version != 1 or supersedes_fact_id is not None:
                raise ServiceError(422, "First fact in a series must use version 1")
        else:
            if head is None:
                raise ServiceError(409, "Knowledge series has no approved head")
            if supersedes_fact_id != head.id:
                raise ServiceError(409, "Replacement must supersede the current approved head")
            if version <= head.version:
                raise ServiceError(422, "Replacement version must be greater than current head")
        candidate.status = KnowledgeCandidateStatus.APPROVED
        candidate.updated_at = now_utc()
        fact = KnowledgeFact(
            series_id=series_id,
            version=version,
            project_id=candidate.project_id,
            statement=candidate.statement,
            source_candidate_id=candidate.id,
            source_artifact_id=candidate.artifact_id,
            review_decision_id=review.id,
            supersedes_fact_id=supersedes_fact_id,
        )
        try:
            self.session.add_all([candidate, review])
            self.session.flush()
            if head is not None:
                head.status = KnowledgeFactStatus.SUPERSEDED
                head.updated_at = now_utc()
                self.session.add(head)
                self.session.flush()
            self.session.add(fact)
            self.session.flush()
            action = "knowledge.fact.superseded" if head else "knowledge.fact.approved"
            append_audit(
                self.session,
                actor=review.reviewer,
                action=action,
                resource_type="knowledge_fact",
                resource_id=fact.id,
                project_id=fact.project_id,
                task_id=None,
                before={"predecessor_id": head.id if head else None},
                after={
                    "fact_id": fact.id,
                    "candidate_id": candidate.id,
                    "artifact_id": candidate.artifact_id,
                    "review_decision_id": review.id,
                    "series_id": series_id,
                    "version": version,
                    "scope": "company" if fact.project_id is None else "project",
                    "project_id": fact.project_id,
                    "supersedes_fact_id": supersedes_fact_id,
                    "rationale": review.rationale,
                },
                idempotency_key=f"audit:knowledge:fact:{fact.id}:approved",
            )
            self.session.commit()
        except (IntegrityError, OperationalError) as exc:
            self.session.rollback()
            raise ServiceError(409, "Knowledge approval conflicts with current state") from exc
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(review)
        self.session.refresh(fact)
        return KnowledgeReviewResult(review, fact)

    def deactivate_fact(self, fact_id: str, actor: str, rationale: str) -> KnowledgeFact:
        actor = _required(actor, "actor")
        rationale = _required(rationale, "rationale")
        fact = self.session.get(KnowledgeFact, fact_id)
        if fact is None:
            raise ServiceError(404, "Knowledge fact not found")
        if fact.status != KnowledgeFactStatus.APPROVED:
            raise ServiceError(409, "Only an approved knowledge fact can be deactivated")
        fact.status = KnowledgeFactStatus.INACTIVE
        fact.updated_at = now_utc()
        try:
            self.session.add(fact)
            self.session.flush()
            append_audit(
                self.session,
                actor=actor,
                action="knowledge.fact.deactivated",
                resource_type="knowledge_fact",
                resource_id=fact.id,
                project_id=fact.project_id,
                task_id=None,
                before={"status": KnowledgeFactStatus.APPROVED.value},
                after={"status": fact.status.value, "rationale": rationale},
                idempotency_key=f"audit:knowledge:fact:{fact.id}:deactivated",
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(fact)
        return fact
