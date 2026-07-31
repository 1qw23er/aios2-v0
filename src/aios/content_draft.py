"""Personal-IP content & monetization workflow (#108-A) -- service layer.

Implements ``docs/issue-108-a-plan.md`` (v3, APPROVED FOR IMPLEMENTATION via PR
#112). This module is ZERO-MIGRATION: it adds only a code value to the existing
``ArtifactType`` StrEnum and reuses existing primitives:

* ``Artifact(type=CONTENT_DRAFT)`` -- the content draft row. Draft business
  fields live in ``metadata_json``; the body markdown lives at ``uri``; the
  frozen ``checksum`` covers both. ``Artifact`` already carries
  ``revision_count`` (used as the revision/round binder), so no schema change.
* ``Approval`` (``uq_approval_gate_round`` unique) -- the terminal owner decision
  (approve or reject). Never created by a non-owner path.
* ``AuditLog`` (UNIQUE ``idempotency_key``, inert: no status/attempt/delivery
  semantics) -- the append-only metrics/retrospective record and all audit
  evidence. Chosen over ``Event`` because ``Event`` carries ``status=PENDING`` /
  ``attempt_count`` / ``processed_at`` delivery semantics that violate the
  inert-metrics contract (plan v3 §2b).

Hard invariants (all locked by owner review, plan v3):

1. An approved CONTENT_DRAFT remains an approved *Artifact*. Approval NEVER
   creates a ``KnowledgeFact`` and NEVER injects content text, marketing claims,
   prices, CTAs, or performance metrics into Agent knowledge. Retrospective
   lessons are submitted later by the owner as independent ``KnowledgeCandidate``
   rows through the existing review path.
2. Owner approval is bound to an EXACT artifact revision: it re-reads the row
   inside a single ``BEGIN IMMEDIATE`` transaction and verifies the current
   ``checksum`` equals the passed review checksum, the current ``revision_count``
   equals the passed review revision, ``review_status == REVIEW_PASSED``, and no
   terminal ``Approval`` exists. A stale review (checksum/revision mismatch)
   returns a stable ``409``. There is NO ``SELECT FOR UPDATE`` and NO generic
   "pessimistic row lock" -- SQLite serializes writers via ``BEGIN IMMEDIATE``.
3. The independent review is genuinely independent: the reviewer identity is
   server-derived and MUST differ from the producer identity. Automated review
   may produce only ``REVIEW_PASSED`` or ``NEEDS_REVISION``; it can NEVER produce
   ``APPROVED``. Errors / malformed output / low confidence always degrade to
   ``NEEDS_REVISION``. The default adapter is a deterministic ``FakeReviewAdapter``
   that performs NO paid model call.
4. All identities (actor / producer / reviewer / owner / project) are
   server-derived from the authentication boundary. No request field may select
   them.

Import note: the ``aios.*`` imports needed only by the HTTP auth dependency are
performed lazily *inside* that function, so importing this module never triggers
``aios.api.app`` (which imports this module) -- avoiding an import cycle.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Annotated, Any, Literal, Protocol

from fastapi import Depends, HTTPException
from fastapi import status as _http_status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from aios.actor import ActorContext, _assert_owner_actor, resolve_agent_actor
from aios.audit import AuditLog, append_audit
from aios.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    RiskLevel,
    now_utc,
)
from aios.services import ServiceError

# Local HTTP credential schemes (isolated from aios.api.security to avoid an
# import cycle at module load; the owner/agent verification logic is still
# delegated to aios.api.security inside the dependency).
_basic_scheme = HTTPBasic(auto_error=False, realm="aios-owner")
_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="Bearer")

# --- Contract constants (plan v3) -------------------------------------------

#: Default content series used only as descriptive metadata on the draft. It is
#: NEVER injected into knowledge and NEVER used to mint a KnowledgeFact.
DEFAULT_SERIES_ID = "黎叔AI创业实验室"

CONTENT_DRAFT_CREATE_AUDIT = "content_draft.create"
CONTENT_DRAFT_UPDATE_AUDIT = "content_draft.update"
CONTENT_DRAFT_REVIEW_AUDIT = "content_draft.independent_review"
CONTENT_DRAFT_APPROVE_AUDIT = "content_draft.approve"
CONTENT_DRAFT_REJECT_AUDIT = "content_draft.reject"
CONTENT_DRAFT_METRIC_AUDIT = "content.review_metric"

CONTENT_DRAFT_APPROVE_ACTION = "content_draft_approve"
CONTENT_DRAFT_REJECT_ACTION = "content_draft_reject"

#: Minimum confidence required for an automated review to flip a draft to
#: ``REVIEW_PASSED``. Anything below this is treated as *low confidence* and
#: fails closed to ``NEEDS_REVISION`` (plan v3 §4 invariant 3: "Errors /
#: malformed output / low confidence always degrade to NEEDS_REVISION"). The
#: default ``FakeReviewAdapter`` returns 1.0, so the happy path is unaffected.
REVIEW_PASS_MIN_CONFIDENCE = 0.5


# --- Pure helpers -----------------------------------------------------------


def canonical_json(payload: dict[str, Any]) -> str:
    """Stable canonical JSON (sorted keys, no whitespace) for checksumming."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _business_payload(artifact: Artifact) -> dict[str, Any]:
    """The exact content the checksum must freeze: body (uri) + metadata.

    The review-binding back-reference ``independent_review.reviewed_checksum``
    is explicitly excluded: it is a pointer to the checksum, not part of the
    reviewed content. Including it would make the checksum non-idempotent -- the
    hash would change only because the pointer was written -- so the persisted
    checksum would no longer represent the persisted payload (plan v3 invariant:
    the frozen checksum covers body + metadata). Excluding it keeps the checksum
    stable and self-consistent across recomputation.
    """
    metadata = dict(artifact.metadata_json or {})
    review = metadata.get("independent_review")
    if isinstance(review, dict) and "reviewed_checksum" in review:
        review = dict(review)
        review.pop("reviewed_checksum", None)
        metadata["independent_review"] = review
    return {"uri": artifact.uri, "metadata": metadata}


def _compute_checksum(artifact: Artifact) -> str:
    return "sha256:" + _sha256(canonical_json(_business_payload(artifact)))


def _get_independent_review(artifact: Artifact) -> dict[str, Any] | None:
    """Return the persisted independent-review record, or None if absent.

    The review is stored under ``metadata_json.independent_review`` (never a
    top-level ``reviewer`` key). Owner approval binds to this record so a stale
    or absent review can never flip a draft to APPROVED.
    """
    review = (artifact.metadata_json or {}).get("independent_review")
    return review if isinstance(review, dict) else None


def _reviewer_of(artifact: Artifact) -> str | None:
    """Server-derived reviewer identity stored in the persisted review record."""
    review = _get_independent_review(artifact)
    if review is None:
        return None
    return review.get("reviewer")


# --- Independent review adapter (plan v3 §4) --------------------------------


class ContentReviewResult:
    """Outcome of one automated independent review (never APPROVED)."""

    __slots__ = ("result", "confidence", "bounded_reason")

    def __init__(
        self,
        result: Literal["review_passed", "needs_revision"],
        confidence: float,
        bounded_reason: str,
    ) -> None:
        self.result = result
        self.confidence = confidence
        self.bounded_reason = bounded_reason


class ContentReviewAdapter(Protocol):
    """A reviewer capable of producing an independent review.

    Implementations MUST be deterministic or explicitly gated. The default
    ``FakeReviewAdapter`` performs no network/model call. A real LLM adapter, if
    ever wired in, MUST read credentials from the server environment, perform a
    real model call, and attribute cost to a configured owner budget -- it is
    never invoked by the default execution path.
    """

    reviewer_identity: str

    def review(self, *, artifact: Artifact, producer_identity: str) -> ContentReviewResult:
        ...


class FakeReviewAdapter:
    """Deterministic, zero-cost review adapter (default).

    Always returns ``review_passed`` with full confidence. Performs NO paid model
    call and NEVER contacts a network. Used by default execution and by every
    test so the suite has no external dependencies or cost.
    """

    reviewer_identity = "agent:content-review-fake"

    def review(self, *, artifact: Artifact, producer_identity: str) -> ContentReviewResult:
        return ContentReviewResult(
            result="review_passed",
            confidence=1.0,
            bounded_reason="fake deterministic review passed (no model call)",
        )


#: Server-default adapter. Swap only via explicit, gated configuration.
_DEFAULT_REVIEW_ADAPTER: ContentReviewAdapter = FakeReviewAdapter()


def _safe_review(
    adapter: ContentReviewAdapter, *, artifact: Artifact, producer_identity: str
) -> ContentReviewResult:
    """Run a review, degrading ANY failure to NEEDS_REVISION (never APPROVED).

    The entire result-processing path is wrapped so a malformed adapter output
    (wrong type, non-numeric/non-finite confidence, or a confidence outside the
    documented 0..1 range) fails closed instead of raising (500) or being accepted.
    """
    try:
        result = adapter.review(artifact=artifact, producer_identity=producer_identity)
        # Validate the COMPLETE result object; any malformed field degrades.
        if not isinstance(result, ContentReviewResult):
            raise ValueError("review result is not a ContentReviewResult")
        if result.result not in ("review_passed", "needs_revision"):
            raise ValueError("invalid review result")
        if (
            not isinstance(result.confidence, (int, float))
            or isinstance(result.confidence, bool)
            or not math.isfinite(result.confidence)
            or result.confidence < REVIEW_PASS_MIN_CONFIDENCE
            or result.confidence > 1.0
        ):
            raise ValueError("review confidence out of valid 0..1 range")
    except Exception:
        return ContentReviewResult(
            result="needs_revision", confidence=0.0, bounded_reason="review adapter error"
        )
    return result


def _safe_reviewer_identity(adapter: ContentReviewAdapter) -> Any:
    """Best-effort, fail-closed derivation of the server-side reviewer identity.

    Any error (e.g. the adapter raises while resolving its identity) or a
    non-string result degrades to ``None`` so the caller can fail closed to
    NEEDS_REVISION instead of raising 500 or approving a draft without a valid
    independent reviewer (plan v3 invariant 4).
    """
    try:
        identity = adapter.reviewer_identity
    except Exception:
        return None
    return identity


# --- Service ----------------------------------------------------------------


class ContentDraftService:
    """Service layer for the personal-IP content workflow (plan v3).

    Every method takes a trusted ``actor: ActorContext`` injected by the
    authentication boundary. This module NEVER resolves an actor itself and
    NEVER accepts identity from a request body.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- create ------------------------------------------------------------

    def create_content_draft(
        self,
        *,
        project_id: str,
        actor: ActorContext,
        topic: str,
        body: str,
        phase: str = "idea",
        outline: list[str] | None = None,
        conversion_anchors: list[dict[str, Any]] | None = None,
        series_id: str = DEFAULT_SERIES_ID,
        task_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Artifact:
        """Create an UNVERIFIED content draft. Owner or any authenticated Agent."""
        if actor.kind not in ("owner", "agent"):
            raise ServiceError(403, "create requires owner or agent identity")
        producer = actor.derive_submitted_by()
        metadata: dict[str, Any] = {
            "topic": topic,
            "phase": phase,
            "outline": outline or [],
            "conversion_anchors": conversion_anchors or [],
            "series_id": series_id,
            "producer": producer,
            "independent_review": None,
            "review_history": [],
        }
        artifact = Artifact(
            project_id=project_id,
            task_id=task_id,
            type=ArtifactType.CONTENT_DRAFT,
            uri=body,
            checksum="",  # set below before commit
            review_status=ArtifactReviewStatus.UNVERIFIED,
            metadata_json=metadata,
            idempotency_key=idempotency_key,
        )
        artifact.checksum = _compute_checksum(artifact)
        self.session.add(artifact)
        self.session.flush()  # populate artifact.id for the audit idempotency key
        append_audit(
            self.session,
            actor=producer,
            action=CONTENT_DRAFT_CREATE_AUDIT,
            resource_type="artifact",
            resource_id=artifact.id,
            project_id=project_id,
            task_id=task_id,
            before={},
            after={"review_status": "unverified", "topic": topic, "series_id": series_id},
            idempotency_key=f"audit:content_draft:create:{artifact.id}",
        )
        self.session.commit()
        self.session.refresh(artifact)
        return artifact

    # -- update (locked after approval) ------------------------------------

    def update_content_draft(
        self,
        *,
        artifact_id: str,
        actor: ActorContext,
        topic: str | None = None,
        body: str | None = None,
        phase: str | None = None,
        outline: list[str] | None = None,
        conversion_anchors: list[dict[str, Any]] | None = None,
        series_id: str | None = None,
    ) -> Artifact:
        """Edit an UNVERIFIED/NEEDS_REVISION draft.

        Atomically: update payload, recompute checksum, increment
        ``revision_count``, reset ``review_status`` to UNVERIFIED, move the
        current independent review into ``review_history`` (preserved but unusable
        for approval) and clear ``independent_review``. APPROVED/REJECTED drafts
        are locked (409). Only the owner or the producer may edit (403).
        """
        self.session.rollback()  # ensure no implicit transaction is open
        self.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            artifact = self.session.get(Artifact, artifact_id)
            if artifact is None:
                raise ServiceError(404, "Artifact not found")
            if artifact.type != ArtifactType.CONTENT_DRAFT:
                raise ServiceError(409, "artifact is not a content draft")
            producer = (artifact.metadata_json or {}).get("producer")
            if not (actor.kind == "owner" or actor.derive_submitted_by() == producer):
                raise ServiceError(403, "only owner or the producer may update this draft")
            if artifact.review_status in (
                ArtifactReviewStatus.APPROVED,
                ArtifactReviewStatus.REJECTED,
            ):
                raise ServiceError(409, "approved/rejected content draft is locked")

            prev_status = artifact.review_status.value
            prev_revision = artifact.revision_count
            metadata = dict(artifact.metadata_json or {})
            if body is not None:
                artifact.uri = body
            if topic is not None:
                metadata["topic"] = topic
            if phase is not None:
                metadata["phase"] = phase
            if outline is not None:
                metadata["outline"] = outline
            if conversion_anchors is not None:
                metadata["conversion_anchors"] = conversion_anchors
            if series_id is not None:
                metadata["series_id"] = series_id

            # Invalidate any prior review: archive it, clear the active one.
            old_review = metadata.get("independent_review")
            if old_review is not None:
                history = list(metadata.get("review_history") or [])
                history.append(old_review)
                metadata["review_history"] = history
                metadata["independent_review"] = None

            artifact.metadata_json = metadata
            artifact.revision_count = (artifact.revision_count or 0) + 1
            artifact.review_status = ArtifactReviewStatus.UNVERIFIED
            artifact.checksum = _compute_checksum(artifact)

            append_audit(
                self.session,
                actor=actor.derive_submitted_by(),
                action=CONTENT_DRAFT_UPDATE_AUDIT,
                resource_type="artifact",
                resource_id=artifact.id,
                project_id=artifact.project_id,
                task_id=artifact.task_id,
                before={"review_status": prev_status, "revision_count": prev_revision},
                after={"review_status": "unverified", "revision_count": artifact.revision_count},
                idempotency_key=(
                    f"audit:content_draft:update:{artifact.id}:{artifact.revision_count}"
                ),
            )
            self.session.commit()
        except ServiceError:
            self.session.rollback()
            raise
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(artifact)
        return artifact

    # -- submit (triggers independent review) ------------------------------

    def submit_content_draft(
        self,
        *,
        artifact_id: str,
        actor: ActorContext,
        adapter: ContentReviewAdapter | None = None,
    ) -> Artifact:
        """Submit an UNVERIFIED draft for independent review.

        Runs the (server-default, deterministic, zero-cost) review adapter. The
        reviewer identity MUST differ from the producer and is derived inside the
        fail-closed path. Sets REVIEW_PASSED or NEEDS_REVISION -- NEVER APPROVED.
        The review record (with the reviewed checksum + revision) is persisted so
        owner approval can be bound to it.

        Serialization: the adapter call runs OUTSIDE the transaction (it may be
        slow), but the read + review + persist is re-checked under BEGIN IMMEDIATE
        so a concurrent edit cannot be reviewed against stale content and then
        approved (plan v3 invariant 2 / Codex P1#2). Any malformed reviewer
        identity fails closed to NEEDS_REVISION (P1#3).
        """
        # --- read + authorization (outside the transaction) ---
        artifact = self.session.get(Artifact, artifact_id)
        if artifact is None:
            raise ServiceError(404, "Artifact not found")
        if artifact.type != ArtifactType.CONTENT_DRAFT:
            raise ServiceError(409, "artifact is not a content draft")
        producer = (artifact.metadata_json or {}).get("producer")
        if not (actor.kind == "owner" or actor.derive_submitted_by() == producer):
            raise ServiceError(403, "only owner or the producer may submit this draft")
        if artifact.review_status != ArtifactReviewStatus.UNVERIFIED:
            raise ServiceError(
                409,
                f"only an UNVERIFIED draft can be submitted "
                f"(current: {artifact.review_status.value})",
            )

        adapter = adapter or _DEFAULT_REVIEW_ADAPTER

        # --- reviewer identity (fail-closed: P1#3) ---
        reviewer_identity = _safe_reviewer_identity(adapter)
        if not isinstance(reviewer_identity, str) or not reviewer_identity:
            # Malformed identity (None / non-string / raised) -> degrade to
            # NEEDS_REVISION, never raise 500 and never approve.
            result: ContentReviewResult = ContentReviewResult(
                result="needs_revision",
                confidence=0.0,
                bounded_reason="reviewer identity malformed",
            )
        elif reviewer_identity == producer:
            # A valid-but-spoofed identity equal to the producer is a trust
            # violation (plan v3 invariant 4), not a transient malformation, so
            # it is rejected outright (test_submit_rejects_spoofed_reviewer).
            raise ServiceError(409, "reviewer identity must differ from producer")
        else:
            # --- run the review (adapter call, outside the transaction) ---
            result = _safe_review(adapter, artifact=artifact, producer_identity=producer)

        reviewed_checksum = artifact.checksum
        reviewed_revision = artifact.revision_count

        # --- serialize the write under BEGIN IMMEDIATE (P1#2) ---
        self.session.rollback()
        self.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            fresh = self.session.get(Artifact, artifact_id)
            if fresh is None:
                raise ServiceError(404, "Artifact not found")
            if fresh.review_status != ArtifactReviewStatus.UNVERIFIED:
                raise ServiceError(409, "content draft already submitted or reviewed")
            # A concurrent update would change checksum/revision; refuse a stale
            # review so unreviewed content can never be approved.
            if fresh.checksum != reviewed_checksum or fresh.revision_count != reviewed_revision:
                raise ServiceError(409, "content changed since review; re-submit")

            metadata = dict(fresh.metadata_json or {})
            review_record = {
                "artifact_id": fresh.id,
                "reviewed_checksum": None,  # bound to the final checksum below
                "reviewed_revision": reviewed_revision,
                "producer": producer,
                "reviewer": reviewer_identity if isinstance(reviewer_identity, str) else "unknown",
                "result": result.result,
                "confidence": result.confidence,
                "bounded_reason": result.bounded_reason,
                "reviewed_at": now_utc().isoformat(),
            }
            metadata["independent_review"] = review_record
            fresh.metadata_json = metadata
            fresh.review_status = (
                ArtifactReviewStatus.REVIEW_PASSED
                if result.result == "review_passed"
                else ArtifactReviewStatus.NEEDS_REVISION
            )
            # Recompute the frozen checksum over content + review record. The
            # back-reference (reviewed_checksum) is excluded from the hash so the
            # persisted checksum exactly represents the persisted payload (P1#1).
            fresh.checksum = _compute_checksum(fresh)
            review_record["reviewed_checksum"] = fresh.checksum
            append_audit(
                self.session,
                actor=actor.derive_submitted_by(),
                action=CONTENT_DRAFT_REVIEW_AUDIT,
                resource_type="artifact",
                resource_id=fresh.id,
                project_id=fresh.project_id,
                task_id=fresh.task_id,
                before={"review_status": "unverified"},
                after={
                    "review_status": fresh.review_status.value,
                    "reviewer": review_record["reviewer"],
                    "result": result.result,
                },
                idempotency_key=f"audit:content_draft:review:{fresh.id}",
            )
            self.session.commit()
        except ServiceError:
            self.session.rollback()
            raise
        except IntegrityError:
            self.session.rollback()
            raise
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(fresh)
        return fresh

    # -- owner approval (BEGIN IMMEDIATE + CAS) ----------------------------

    def approve_content_draft(
        self,
        *,
        artifact_id: str,
        actor: ActorContext,
        review_checksum: str,
        review_revision: int,
    ) -> Artifact:
        """Owner approval: the ONLY path that flips a content draft to APPROVED.

        Serialized via ``BEGIN IMMEDIATE`` so concurrent approvers converge to
        exactly one Approval + one AuditLog + one status flip. Inside the
        transaction the current ``checksum`` must equal ``review_checksum``, the
        current ``revision_count`` must equal ``review_revision``,
        ``review_status`` must be REVIEW_PASSED, and no terminal Approval may
        exist. A stale review (checksum/revision mismatch) or an already-decided
        draft returns a stable 409. NEVER creates a KnowledgeFact.
        """
        _assert_owner_actor(actor)
        try:
            return self._approve_locked(
                artifact_id, actor, review_checksum, review_revision
            )
        except IntegrityError:
            self.session.rollback()
            try:
                return self._approve_locked(
                    artifact_id, actor, review_checksum, review_revision
                )
            except IntegrityError:
                self.session.rollback()
                raise ServiceError(409, "approval conflict: persistent integrity error") from None

    def _approve_locked(
        self,
        artifact_id: str,
        actor: ActorContext,
        review_checksum: str,
        review_revision: int,
    ) -> Artifact:
        self.session.rollback()  # ensure no implicit transaction is open
        self.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            artifact = self.session.get(Artifact, artifact_id)
            if artifact is None:
                raise ServiceError(404, "Artifact not found")
            if artifact.type != ArtifactType.CONTENT_DRAFT:
                raise ServiceError(409, "artifact is not a content draft")

            existing = self._terminal_approval(artifact.id)
            if artifact.review_status == ArtifactReviewStatus.APPROVED:
                raise ServiceError(409, "content draft already approved")
            if artifact.review_status != ArtifactReviewStatus.REVIEW_PASSED:
                raise ServiceError(
                    409,
                    "only a REVIEW_PASSED draft can be approved "
                    f"(current: {artifact.review_status.value})",
                )
            if existing is not None:
                raise ServiceError(409, "content draft already has a terminal approval")
            # Stale-review guard: bind approval to the EXACT persisted review
            # record (plan v3 invariant 2). The persisted record is the source
            # of truth; a missing or mismatched review can never approve.
            review = _get_independent_review(artifact)
            if review is None:
                raise ServiceError(409, "no independent review recorded for this draft")
            reviewed_checksum = review.get("reviewed_checksum")
            reviewed_revision = review.get("reviewed_revision")
            if (
                reviewed_checksum != artifact.checksum
                or reviewed_revision != artifact.revision_count
                or review_checksum != artifact.checksum
                or review_revision != artifact.revision_count
            ):
                raise ServiceError(
                    409, "stale review: draft changed since the recorded review"
                )

            before_status = artifact.review_status
            self.session.add(
                Approval(
                    project_id=artifact.project_id,
                    task_id=artifact.task_id,
                    target_artifact_id=artifact.id,
                    action_type=CONTENT_DRAFT_APPROVE_ACTION,
                    risk_level=RiskLevel.L4,
                    status=ApprovalStatus.APPROVED,
                    decided_at=now_utc(),
                    rationale="owner approval of content draft",
                    review_round=artifact.revision_count,
                )
            )
            artifact.review_status = ArtifactReviewStatus.APPROVED
            self.session.add(artifact)
            append_audit(
                self.session,
                actor=actor.owner_id or "owner",
                action=CONTENT_DRAFT_APPROVE_AUDIT,
                resource_type="artifact",
                resource_id=artifact.id,
                project_id=artifact.project_id,
                task_id=artifact.task_id,
                before={"review_status": before_status.value},
                after={"review_status": "approved"},
                idempotency_key=f"audit:content_draft:approve:{artifact.id}",
            )
            self.session.commit()
        except ServiceError:
            self.session.rollback()
            raise
        except IntegrityError:
            self.session.rollback()
            raise
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(artifact)
        return artifact

    # -- owner rejection (same atomic contract) ----------------------------

    def reject_content_draft(
        self,
        *,
        artifact_id: str,
        actor: ActorContext,
        reason: str | None = None,
    ) -> Artifact:
        """Owner rejection: terminal REJECTED. Same BEGIN IMMEDIATE contract."""
        _assert_owner_actor(actor)
        try:
            return self._reject_locked(artifact_id, actor, reason)
        except IntegrityError:
            self.session.rollback()
            try:
                return self._reject_locked(artifact_id, actor, reason)
            except IntegrityError:
                self.session.rollback()
                raise ServiceError(409, "rejection conflict: persistent integrity error") from None

    def _reject_locked(
        self,
        artifact_id: str,
        actor: ActorContext,
        reason: str | None,
    ) -> Artifact:
        self.session.rollback()
        self.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            artifact = self.session.get(Artifact, artifact_id)
            if artifact is None:
                raise ServiceError(404, "Artifact not found")
            if artifact.type != ArtifactType.CONTENT_DRAFT:
                raise ServiceError(409, "artifact is not a content draft")

            existing = self._terminal_approval(artifact.id)
            if artifact.review_status == ArtifactReviewStatus.REJECTED:
                raise ServiceError(409, "content draft already rejected")
            if existing is not None:
                raise ServiceError(409, "content draft already has a terminal approval")

            before_status = artifact.review_status
            self.session.add(
                Approval(
                    project_id=artifact.project_id,
                    task_id=artifact.task_id,
                    target_artifact_id=artifact.id,
                    action_type=CONTENT_DRAFT_REJECT_ACTION,
                    risk_level=RiskLevel.L4,
                    status=ApprovalStatus.REJECTED,
                    decided_at=now_utc(),
                    rationale=reason or "owner rejected content draft",
                    review_round=artifact.revision_count,
                )
            )
            artifact.review_status = ArtifactReviewStatus.REJECTED
            self.session.add(artifact)
            append_audit(
                self.session,
                actor=actor.owner_id or "owner",
                action=CONTENT_DRAFT_REJECT_AUDIT,
                resource_type="artifact",
                resource_id=artifact.id,
                project_id=artifact.project_id,
                task_id=artifact.task_id,
                before={"review_status": before_status.value},
                after={"review_status": "rejected"},
                idempotency_key=f"audit:content_draft:reject:{artifact.id}",
            )
            self.session.commit()
        except ServiceError:
            self.session.rollback()
            raise
        except IntegrityError:
            self.session.rollback()
            raise
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(artifact)
        return artifact

    # -- append-only metrics (inert AuditLog) ------------------------------

    def record_review_metrics(
        self,
        *,
        artifact_id: str,
        actor: ActorContext,
        metrics: dict[str, Any],
        idempotency_key: str,
        supersedes_audit_id: str | None = None,
    ) -> AuditLog:
        """Append an immutable review-metric record for an APPROVED draft.

        Does NOT mutate the Artifact payload, checksum, review status, or any
        Approval. Uses the inert ``AuditLog`` primitive with a database-enforced
        UNIQUE ``idempotency_key`` (duplicates are idempotent). Corrections are
        expressed via ``supersedes_audit_id`` stored inside the record's
        ``after_snapshot`` (the record itself is never overwritten). Branching
        and cycles are rejected: the superseded record must reference the same
        artifact and must not itself supersede another.
        """
        _assert_owner_actor(actor)
        if not idempotency_key:
            raise ServiceError(422, "idempotency_key is required")
        # Serialize the supersession check + insert so two concurrent owner
        # corrections cannot both read the sibling set and fork the history into
        # a branch (plan v3 §2b). Mirrors the approve/reject BEGIN IMMEDIATE
        # contract used elsewhere in this service.
        self.session.rollback()  # ensure no implicit transaction is open
        self.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            artifact = self.session.get(Artifact, artifact_id)
            if artifact is None:
                raise ServiceError(404, "Artifact not found")
            if artifact.type != ArtifactType.CONTENT_DRAFT:
                raise ServiceError(409, "artifact is not a content draft")
            if artifact.review_status != ArtifactReviewStatus.APPROVED:
                raise ServiceError(409, "metrics may only be recorded for APPROVED content")

            if supersedes_audit_id is not None:
                prev = self.session.get(AuditLog, supersedes_audit_id)
                if (
                    prev is None
                    or prev.action != CONTENT_DRAFT_METRIC_AUDIT
                    or prev.resource_id != artifact.id
                ):
                    raise ServiceError(409, "invalid supersession reference")
                if (prev.after_snapshot or {}).get("supersedes_audit_id") is not None:
                    raise ServiceError(409, "supersession chain/cycle not allowed")
                # Reject branches: the target must not already be superseded by
                # another record. This read happens inside the serialized
                # transaction, so a concurrent correction cannot slip past it.
                siblings = self.session.exec(
                    select(AuditLog).where(
                        AuditLog.action == CONTENT_DRAFT_METRIC_AUDIT,
                        AuditLog.resource_id == artifact.id,
                    )
                ).all()
                for other in siblings:
                    if (
                        (other.after_snapshot or {}).get("supersedes_audit_id")
                        == supersedes_audit_id
                    ):
                        raise ServiceError(409, "supersession branch rejected")

            after: dict[str, Any] = {"metrics": metrics}
            if supersedes_audit_id is not None:
                after["supersedes_audit_id"] = supersedes_audit_id

            audit = append_audit(
                self.session,
                actor=actor.owner_id or "owner",
                action=CONTENT_DRAFT_METRIC_AUDIT,
                resource_type="artifact",
                resource_id=artifact.id,
                project_id=artifact.project_id,
                task_id=artifact.task_id,
                before={},
                after=after,
                idempotency_key=idempotency_key,
            )
            self.session.commit()
        except ServiceError:
            self.session.rollback()
            raise
        except IntegrityError:
            self.session.rollback()
            existing = self.session.exec(
                select(AuditLog).where(AuditLog.idempotency_key == idempotency_key)
            ).first()
            if existing is not None:
                # Idempotency is an EXACT replay only: the key must belong to the
                # same artifact AND carry the identical metrics payload. A key
                # reused on another artifact or with a different payload is a
                # conflicting replay and must be rejected (never return an
                # unrelated record, which would leak data across drafts).
                if (
                    existing.resource_id == artifact.id
                    and existing.action == CONTENT_DRAFT_METRIC_AUDIT
                    and (existing.after_snapshot or {}).get("metrics") == metrics
                    and (existing.after_snapshot or {}).get("supersedes_audit_id")
                    == supersedes_audit_id
                ):
                    return existing
                raise ServiceError(
                    409,
                    "idempotency_key already used for a different record",
                ) from None
            raise
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(audit)
        return audit

    # -- read with per-Artifact, same-project authorization ----------------

    def get_content_draft(self, *, artifact_id: str, actor: ActorContext) -> Artifact:
        artifact = self.session.get(Artifact, artifact_id)
        if artifact is None:
            raise ServiceError(404, "Artifact not found")
        self._enforce_read_access(artifact, actor)
        return artifact

    def list_content_drafts(self, *, project_id: str, actor: ActorContext) -> list[Artifact]:
        rows = self.session.exec(
            select(Artifact).where(
                Artifact.project_id == project_id,
                Artifact.type == ArtifactType.CONTENT_DRAFT,
            )
        ).all()
        if actor.kind == "owner":
            return list(rows)
        actor_id = actor.derive_submitted_by()
        filtered = [
            a
            for a in rows
            if (a.metadata_json or {}).get("producer") == actor_id
            or _reviewer_of(a) == actor_id
        ]
        if not filtered:
            # Unrelated same-project Agent learns nothing: 403, not an empty list.
            raise ServiceError(403, "not authorized to list content drafts in this project")
        return filtered

    # -- internal helpers --------------------------------------------------

    def _terminal_approval(self, artifact_id: str) -> Approval | None:
        return self.session.exec(
            select(Approval).where(
                Approval.target_artifact_id == artifact_id,
                Approval.action_type.in_(
                    [CONTENT_DRAFT_APPROVE_ACTION, CONTENT_DRAFT_REJECT_ACTION]
                ),
                Approval.status.in_([ApprovalStatus.APPROVED, ApprovalStatus.REJECTED]),
            )
        ).first()

    def _enforce_read_access(self, artifact: Artifact, actor: ActorContext) -> None:
        if actor.kind == "owner":
            return
        actor_id = actor.derive_submitted_by()
        md = artifact.metadata_json or {}
        if md.get("producer") == actor_id or _reviewer_of(artifact) == actor_id:
            return
        raise ServiceError(403, "not authorized to access this content draft")


# --- HTTP auth dependency (plan v3 §5) -------------------------------------
# The ``aios.*`` imports below are performed lazily *inside* the dependency so
# that importing this module never triggers ``aios.api.app`` (which imports this
# module) -- avoiding an import cycle via the ``aios.api`` package __init__.


def authenticate_owner_or_agent(
    basic: Annotated[HTTPBasicCredentials | None, Depends(_basic_scheme)],
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> ActorContext:
    """FastAPI dependency: accept a trusted owner (HTTP Basic) OR a trusted agent
    (bearer). The first valid credential wins; a missing/invalid attempt on one
    scheme falls through to the other. All identities are server-derived.
    """
    from aios.api.security import (
        _AGENT_UNAUTH_HEADERS,
        _UNAUTH_HEADERS,
        verify_owner_credentials,
    )
    from aios.secrets_store import SecretStoreUnavailable, get_secret_store

    if basic is not None:
        try:
            return verify_owner_credentials(basic.username, basic.password)
        except Exception:
            # Unconfigured owner (503) or wrong owner creds (401): try agent.
            pass
    if bearer is not None and bearer.credentials:
        try:
            agent_id = get_secret_store().resolve(bearer.credentials)
        except SecretStoreUnavailable:
            raise HTTPException(
                status_code=_http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="secret_store_unavailable",
                headers=_AGENT_UNAUTH_HEADERS,
            ) from None
        if agent_id is None:
            raise HTTPException(
                status_code=_http_status.HTTP_401_UNAUTHORIZED,
                detail="invalid agent credential",
                headers=_AGENT_UNAUTH_HEADERS,
            )
        return resolve_agent_actor(agent_id)
    raise HTTPException(
        status_code=_http_status.HTTP_401_UNAUTHORIZED,
        detail="authentication required",
        headers=_UNAUTH_HEADERS,
    )
