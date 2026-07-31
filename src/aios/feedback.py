"""Usage-feedback loop (V1.2-C, #110) -- service layer.

Implements ``docs/issue-110-feedback-loop-plan.md`` (v11, APPROVED FOR
IMPLEMENTATION via PR #114). This module is ZERO-MIGRATION: it adds only a code
value to the existing ``ArtifactType`` StrEnum and reuses existing primitives:

* ``Artifact(type=FEEDBACK)`` -- the feedback row. A-zone business fields
  (original_text / scenario / expected_outcome / risk_tags / solution_text) live
  in ``metadata_json``; the original text also lives at the required ``uri``; the
  frozen ``checksum`` covers only the A-zone (plan §1.3). ``revision_count``
  tracks content edits (stage changes do NOT bump it).
* ``Approval`` (``uq_approval_gate_round`` unique) -- the owner's terminal
  decision (APPROVED / REJECTED) binding one solution revision round.
* ``AuditLog`` (UNIQUE ``idempotency_key``, inert: no status/attempt/delivery
  semantics) -- stage transitions, owner approve/reject, invalidate-pending, and
  the deterministic cluster summaries.

Hard invariants (all locked by owner review, plan v11):

1. Endpoints only accept named ``FeedbackTransition`` verbs -- never a bare
   stage string (T3). ``apply_transition`` is the single FSM entry point.
2. Owner approval binds the EXACT solution revision: under a single
   ``BEGIN IMMEDIATE`` it re-reads and verifies ``checksum == pending.checksum``
   and ``revision_count == pending.revision``, and that no SAME-ROUND terminal
   ``Approval`` exists; a stale edit or an already-decided round returns a stable
   409 (T6/T7/T8/T11b).
3. Feedback is untrusted input: UTF-8 NFC normalization, field caps, no secrets
   in ``AuditLog`` (``redact_secrets``), PII redaction in cluster summaries and
   HTTP responses; text is data, never an instruction.
4. Cluster summaries are deterministic, idempotent, append-only ``AuditLog``
   rows; corrections never write back to a ``Feedback`` (T17/T18/T22).
5. No ``KnowledgeFact`` / ``Event`` / ``Task`` / ``DelegatedRun`` /
   ``Publication`` / ``Payment`` / ``Deployment`` side effects are ever created
   (T30).

Concurrency model: the FSM and owner-approval paths serialize writers with a
single SQLite ``BEGIN IMMEDIATE`` transaction (never ``SELECT FOR UPDATE`` /
generic pessimistic row locks), exactly like ``content_draft.py``.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlmodel import Session, select

from aios.actor import ActorContext, _assert_owner_actor
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

try:
    from aios.services import ServiceError
except Exception:  # pragma: no cover - defensive import guard
    class ServiceError(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code: int, detail: str) -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail


# --- Enums (versioned FSM, plan §3) -----------------------------------------


class FeedbackStage(StrEnum):
    """Kanban stages (v1). Values are stable strings for JSON serialization."""

    COLLECTED = "COLLECTED"
    CLARIFY = "CLARIFY"
    SOLUTION = "SOLUTION"
    AWAIT_OWNER_APPROVE = "AWAIT_OWNER_APPROVE"
    DEVELOP = "DEVELOP"
    TEST = "TEST"
    SHIPPED = "SHIPPED"
    DEFERRED = "DEFERRED"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"


class FeedbackTransition(StrEnum):
    """Named transition verbs (v1). The ONLY way to move between stages.

    A bare stage string is never accepted by the API: the request model types
    this field as ``FeedbackTransition`` so FastAPI rejects unknown values with
    422 (plan §3.2).
    """

    CLARIFY_REQUESTED = "CLARIFY_REQUESTED"
    CLARIFIED = "CLARIFIED"
    RETURN_TO_CLARIFY = "RETURN_TO_CLARIFY"
    SUBMIT_FOR_APPROVAL = "SUBMIT_FOR_APPROVAL"
    INVALIDATE_PENDING = "INVALIDATE_PENDING"
    REJECT_SOLUTION = "REJECT_SOLUTION"
    APPROVE_SOLUTION = "APPROVE_SOLUTION"
    START_TEST = "START_TEST"
    TEST_FAILED = "TEST_FAILED"
    SHIP = "SHIP"
    DEFER = "DEFER"
    REOPEN = "REOPEN"
    REJECT_FEEDBACK = "REJECT_FEEDBACK"
    MARK_DUPLICATE = "MARK_DUPLICATE"


# Stages that can never be left once entered.
TERMINAL_STAGES = {
    FeedbackStage.SHIPPED,
    FeedbackStage.REJECTED,
    FeedbackStage.DUPLICATE,
}


@dataclass(frozen=True)
class TransitionRule:
    """One allowed transition (plan §3.2 table)."""

    actor: str  # "owner" | "submitter_or_owner"
    required_stages: tuple[FeedbackStage, ...]  # allowed current stages
    target_stage: FeedbackStage
    audit_action: str
    owner_only: bool


# Versioned allow-list. The key is the FSM version; only "fsm-v1" exists.
ALLOWED_TRANSITIONS: dict[str, dict[FeedbackTransition, TransitionRule]] = {
    "fsm-v1": {
        FeedbackTransition.CLARIFY_REQUESTED: TransitionRule(
            "submitter_or_owner", (FeedbackStage.COLLECTED,), FeedbackStage.CLARIFY,
            "feedback.stage_transition", False,
        ),
        FeedbackTransition.CLARIFIED: TransitionRule(
            "submitter_or_owner", (FeedbackStage.CLARIFY,), FeedbackStage.SOLUTION,
            "feedback.stage_transition", False,
        ),
        FeedbackTransition.RETURN_TO_CLARIFY: TransitionRule(
            "submitter_or_owner", (FeedbackStage.SOLUTION,), FeedbackStage.CLARIFY,
            "feedback.stage_transition", False,
        ),
        FeedbackTransition.SUBMIT_FOR_APPROVAL: TransitionRule(
            "submitter_or_owner", (FeedbackStage.SOLUTION,),
            FeedbackStage.AWAIT_OWNER_APPROVE, "feedback.submit_for_approval", False,
        ),
        FeedbackTransition.INVALIDATE_PENDING: TransitionRule(
            "submitter_or_owner", (FeedbackStage.AWAIT_OWNER_APPROVE,),
            FeedbackStage.SOLUTION, "feedback.invalidate_pending", False,
        ),
        FeedbackTransition.REJECT_SOLUTION: TransitionRule(
            "owner", (FeedbackStage.AWAIT_OWNER_APPROVE,), FeedbackStage.SOLUTION,
            "feedback.owner_reject", True,
        ),
        FeedbackTransition.APPROVE_SOLUTION: TransitionRule(
            "owner", (FeedbackStage.AWAIT_OWNER_APPROVE,), FeedbackStage.DEVELOP,
            "feedback.owner_approve", True,
        ),
        FeedbackTransition.START_TEST: TransitionRule(
            "owner", (FeedbackStage.DEVELOP,), FeedbackStage.TEST,
            "feedback.stage_transition", True,
        ),
        FeedbackTransition.TEST_FAILED: TransitionRule(
            "owner", (FeedbackStage.TEST,), FeedbackStage.DEVELOP,
            "feedback.stage_transition", True,
        ),
        FeedbackTransition.SHIP: TransitionRule(
            "owner", (FeedbackStage.TEST,), FeedbackStage.SHIPPED,
            "feedback.stage_transition", True,
        ),
        FeedbackTransition.DEFER: TransitionRule(
            "owner",
            (
                FeedbackStage.COLLECTED, FeedbackStage.CLARIFY,
                FeedbackStage.SOLUTION, FeedbackStage.AWAIT_OWNER_APPROVE,
                FeedbackStage.DEVELOP, FeedbackStage.TEST,
            ),
            FeedbackStage.DEFERRED, "feedback.stage_transition", True,
        ),
        FeedbackTransition.REOPEN: TransitionRule(
            "owner", (FeedbackStage.DEFERRED,), FeedbackStage.SOLUTION,
            "feedback.stage_transition", True,
        ),
        FeedbackTransition.REJECT_FEEDBACK: TransitionRule(
            "owner", (FeedbackStage.COLLECTED, FeedbackStage.CLARIFY, FeedbackStage.SOLUTION),
            FeedbackStage.REJECTED, "feedback.stage_transition", True,
        ),
        FeedbackTransition.MARK_DUPLICATE: TransitionRule(
            "owner",
            (
                FeedbackStage.COLLECTED, FeedbackStage.CLARIFY,
                FeedbackStage.SOLUTION, FeedbackStage.AWAIT_OWNER_APPROVE,
                FeedbackStage.DEVELOP, FeedbackStage.TEST,
            ),
            FeedbackStage.DUPLICATE, "feedback.stage_transition", True,
        ),
    }
}

FSM_VERSION = "fsm-v1"

# Audit action strings (plan §1.1).
FEEDBACK_CREATE_AUDIT = "feedback.create"
FEEDBACK_AMEND_AUDIT = "feedback.amend"
FEEDBACK_STAGE_AUDIT = "feedback.stage_transition"
FEEDBACK_SUBMIT_AUDIT = "feedback.submit_for_approval"
FEEDBACK_APPROVE_AUDIT = "feedback.owner_approve"
FEEDBACK_REJECT_AUDIT = "feedback.owner_reject"
FEEDBACK_INVALIDATE_AUDIT = "feedback.invalidate_pending"
FEEDBACK_CLUSTER_AUDIT = "feedback.cluster_summary"

# Approval action_type binding a feedback solution revision (plan §2.2).
FEEDBACK_DEVELOP_ACTION = "feedback.develop"

# Deterministic clustering policy version (plan §4).
CLUSTER_POLICY_VERSION = "det-1.0"


# --- Field caps & validation (untrusted input, plan §5) ---------------------


_MAX_ORIGINAL = 16 * 1024
_MAX_SCENARIO = 4 * 1024
_MAX_OUTCOME = 4 * 1024
_MAX_SOLUTION = 16 * 1024
_MAX_RISK_TAGS = 16
_VALID_RISK_TAGS = {
    "data_loss", "ux", "perf", "billing", "security", "other",
}


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _check_field_caps(
    *, original_text, scenario, expected_outcome, solution_text, risk_tags
) -> None:
    if original_text is not None and len(original_text.encode("utf-8")) > _MAX_ORIGINAL:
        raise ServiceError(422, "original_text exceeds 16 KiB")
    if scenario is not None and len(scenario.encode("utf-8")) > _MAX_SCENARIO:
        raise ServiceError(422, "scenario exceeds 4 KiB")
    if expected_outcome is not None and len(expected_outcome.encode("utf-8")) > _MAX_OUTCOME:
        raise ServiceError(422, "expected_outcome exceeds 4 KiB")
    if solution_text is not None and len(solution_text.encode("utf-8")) > _MAX_SOLUTION:
        raise ServiceError(422, "solution_text exceeds 16 KiB")
    if risk_tags is not None:
        if len(risk_tags) > _MAX_RISK_TAGS:
            raise ServiceError(422, "risk_tags exceeds 16 items")
        for tag in risk_tags:
            if tag not in _VALID_RISK_TAGS:
                raise ServiceError(422, f"invalid risk_tag: {tag!r}")


# --- Checksum (A-zone only, plan §1.3) ---------------------------------------


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _feedback_content_payload(artifact: Artifact) -> dict[str, Any]:
    """The exact A-zone content the checksum must freeze (plan §1.3).

    ``original_text`` lives at ``uri``; the remaining A-zone fields live in
    ``metadata_json``. B-zone fields (stage / submitted_by / channel / cluster_id
    / pending_approval / duplicate_of / workflow_revision / transition_seq /
    corrections) are explicitly excluded so a stage change never alters the
    checksum.
    """
    md = dict(artifact.metadata_json or {})
    risk_tags = md.get("risk_tags") or []
    # Sort for deterministic canonicalization (same tags, any order -> same hash).
    risk_tags = sorted(risk_tags)
    return {
        "original_text": artifact.uri,
        "scenario": md.get("scenario"),
        "expected_outcome": md.get("expected_outcome"),
        "risk_tags": risk_tags,
        "solution_text": md.get("solution_text"),
    }


def _compute_feedback_checksum(artifact: Artifact) -> str:
    return "sha256:" + _sha256(_canonical_json(_feedback_content_payload(artifact)))


def _derive_risk_level(risk_tags: list[str] | None) -> RiskLevel:
    tags = set(risk_tags or [])
    if tags & {"security", "data_loss", "billing"}:
        return RiskLevel.L4
    if tags & {"perf"}:
        return RiskLevel.L2
    if tags & {"ux"}:
        return RiskLevel.L1
    return RiskLevel.L2


# --- PII redaction (plan §5) -------------------------------------------------


_PII_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PII_MOBILE = re.compile(r"1[3-9]\d{9}")
_PII_IDCARD = re.compile(r"\b\d{17}[\dXx]\b")
_PII_BANKCARD = re.compile(r"\b(?:\d[ -]?){15,19}\b")


def redact_pii(value: Any) -> Any:
    """Replace high-confidence PII patterns with ``[REDACTED-PII]``.

    Applied to cluster summaries and HTTP responses so cross-user PII can never
    leak through aggregation or list/get endpoints. Source ``original_text`` is
    preserved unredacted in storage (the submitter's own data) -- only derived /
    outward surfaces are redacted (plan §5).
    """
    if isinstance(value, str):
        value = _PII_EMAIL.sub("[REDACTED-PII]", value)
        value = _PII_MOBILE.sub("[REDACTED-PII]", value)
        value = _PII_IDCARD.sub("[REDACTED-PII]", value)
        value = _PII_BANKCARD.sub("[REDACTED-PII]", value)
        return value
    if isinstance(value, list):
        return [redact_pii(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_pii(v) for k, v in value.items()}
    return value


# --- Clustering (deterministic, zero-LLM, plan §4) ---------------------------


@dataclass
class ClusterSummary:
    """Deterministic aggregate of one cluster of feedback items."""

    cluster_key: str
    member_ids: list[str]
    member_revisions: list[dict[str, Any]]
    policy_version: str
    summary: str
    suggested_priority: str | None
    risk_tags: list[str]


def _cluster_key(member_ids: list[str], policy_version: str) -> str:
    """Deterministic cluster identity: sorted member ids + policy version."""
    ordered = sorted(member_ids)
    return _sha256("|".join(ordered) + "|" + policy_version)


def _suggest_priority(risk_tags: list[str]) -> str:
    """Deterministic, read-only priority suggestion from aggregated risk tags.

    Never drives production; never changes a stage (plan §3.2 / §5).
    """
    tags = set(risk_tags or [])
    if tags & {"security", "data_loss", "billing"}:
        return "P0"
    if tags & {"perf"}:
        return "P2"
    if tags & {"ux"}:
        return "P2"
    return "P3"


def cluster_feedback(members: list[Artifact]) -> ClusterSummary:
    """Pure, deterministic clustering summary over the given feedback items.

    Zero LLM / zero paid call. The cluster membership is decided by the caller
    (``record_cluster_run``); this function only summarizes. The result is fully
    determined by the member set, so identical input reproduces identical output
    (plan §4.1). No full feedback text is carried into the summary.
    """
    ids = sorted(m.id for m in members)
    member_revisions = [
        {"id": m.id, "content_checksum": m.checksum, "revision": m.revision_count}
        for m in sorted(members, key=lambda a: a.id)
    ]
    aggregated: list[str] = []
    for m in members:
        aggregated.extend((m.metadata_json or {}).get("risk_tags") or [])
    risk_tags = sorted(set(aggregated))

    parts = [f"cluster of {len(members)} feedback item(s)"]
    if risk_tags:
        parts.append("risk tags: " + ",".join(risk_tags))
    summary = "; ".join(parts)
    summary = redact_pii(summary)[:512]

    return ClusterSummary(
        cluster_key=_cluster_key(ids, CLUSTER_POLICY_VERSION),
        member_ids=ids,
        member_revisions=member_revisions,
        policy_version=CLUSTER_POLICY_VERSION,
        summary=summary,
        suggested_priority=_suggest_priority(risk_tags),
        risk_tags=risk_tags,
    )


# --- Service ----------------------------------------------------------------


class FeedbackService:
    """Feedback lifecycle + kanban FSM + deterministic clustering (plan #110)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- create -------------------------------------------------------------

    def create_feedback(
        self,
        *,
        project_id: str,
        actor: ActorContext,
        original_text: str,
        scenario: str | None = None,
        expected_outcome: str | None = None,
        risk_tags: list[str] | None = None,
        channel: str = "owner_console",
    ) -> Artifact:
        """Create a COLLECTED feedback artifact (owner or agent; plan §2)."""
        if actor.kind not in ("owner", "agent"):
            raise ServiceError(403, "only owner or an agent may submit feedback")
        if not original_text or not original_text.strip():
            raise ServiceError(422, "original_text is required")

        _check_field_caps(
            original_text=original_text,
            scenario=scenario,
            expected_outcome=expected_outcome,
            solution_text=None,
            risk_tags=risk_tags,
        )

        original_text = _nfc(original_text)
        scenario = _nfc(scenario) if scenario is not None else None
        expected_outcome = _nfc(expected_outcome) if expected_outcome is not None else None
        tags = sorted(set(risk_tags or []))

        metadata: dict[str, Any] = {
            # A-zone
            "scenario": scenario,
            "expected_outcome": expected_outcome,
            "risk_tags": tags,
            "solution_text": None,
            # B-zone (workflow metadata, excluded from checksum)
            "stage": FeedbackStage.COLLECTED.value,
            "submitted_by": actor.derive_submitted_by(),
            "channel": channel,
            "cluster_id": None,
            "pending_approval": None,
            "duplicate_of": None,
            "workflow_revision": FSM_VERSION,
            "transition_seq": 0,
            "corrections": [],
        }
        artifact = Artifact(
            project_id=project_id,
            type=ArtifactType.FEEDBACK,
            uri=original_text,
            checksum="",
            review_status=ArtifactReviewStatus.UNVERIFIED,
            revision_count=0,
            metadata_json=metadata,
        )
        artifact.checksum = _compute_feedback_checksum(artifact)
        self.session.add(artifact)
        self.session.flush()
        append_audit(
            self.session,
            actor=actor.derive_submitted_by(),
            action=FEEDBACK_CREATE_AUDIT,
            resource_type="artifact",
            resource_id=artifact.id,
            project_id=artifact.project_id,
            task_id=artifact.task_id,
            before={},
            after={"stage": FeedbackStage.COLLECTED.value, "checksum": artifact.checksum},
            idempotency_key=f"audit:feedback:create:{artifact.id}",
        )
        self.session.commit()
        self.session.refresh(artifact)
        return artifact

    # -- read (per-Artifact same-project authorization, plan §5) -------------

    def _can_view(self, actor: ActorContext, fb: Artifact) -> bool:
        if actor.kind == "owner":
            return True
        submitted = (fb.metadata_json or {}).get("submitted_by")
        return actor.derive_submitted_by() == submitted

    def get_feedback(self, *, artifact_id: str, actor: ActorContext) -> Artifact:
        fb = self.session.get(Artifact, artifact_id)
        if fb is None:
            raise ServiceError(404, "feedback not found")
        if fb.type != ArtifactType.FEEDBACK:
            raise ServiceError(409, "artifact is not feedback")
        # Explicit 403 (not 404, not silent filtering): an unrelated actor --
        # including a same-project unrelated agent -- learns nothing (T27).
        if not self._can_view(actor, fb):
            raise ServiceError(403, "not authorized to view this feedback")
        return fb

    def list_feedback(self, *, project_id: str, actor: ActorContext) -> list[Artifact]:
        rows = self.session.exec(
            select(Artifact).where(
                Artifact.project_id == project_id,
                Artifact.type == ArtifactType.FEEDBACK,
            )
        ).all()
        if actor.kind == "owner":
            return list(rows)
        sub_by = actor.derive_submitted_by()
        visible = [fb for fb in rows if (fb.metadata_json or {}).get("submitted_by") == sub_by]
        if not visible:
            # Unrelated agent (even same project): 403, never a silent empty set.
            raise ServiceError(403, "not authorized to list feedback for this project")
        return visible

    # -- amend (A-zone content edit, plan §2 / §3.3) -------------------------

    def amend_feedback(
        self,
        *,
        artifact_id: str,
        actor: ActorContext,
        reason: str,
        scenario: str | None = None,
        expected_outcome: str | None = None,
        risk_tags: list[str] | None = None,
        solution_text: str | None = None,
    ) -> Artifact:
        """Edit A-zone content (only COLLECTED/CLARIFY/SOLUTION). reason required.

        Does NOT change the stage. Recomputes checksum, bumps revision_count, and
        appends a ``corrections`` entry (no silent overwrite). Terminal / AWAIT
        stages must use the named transition verbs instead (T11).
        """
        if not reason or not reason.strip():
            raise ServiceError(422, "amend requires a reason")
        _check_field_caps(
            original_text=None,
            scenario=scenario,
            expected_outcome=expected_outcome,
            solution_text=solution_text,
            risk_tags=risk_tags,
        )
        solution_text = _nfc(solution_text) if solution_text is not None else None
        scenario = _nfc(scenario) if scenario is not None else None
        expected_outcome = _nfc(expected_outcome) if expected_outcome is not None else None
        tags = sorted(set(risk_tags)) if risk_tags is not None else None

        self.session.rollback()
        self.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            fb = self.session.get(Artifact, artifact_id)
            if fb is None:
                raise ServiceError(404, "feedback not found")
            if fb.type != ArtifactType.FEEDBACK:
                raise ServiceError(409, "artifact is not feedback")
            md = dict(fb.metadata_json or {})
            if actor.kind != "owner" and actor.derive_submitted_by() != md.get("submitted_by"):
                raise ServiceError(403, "only owner or the submitter may amend this feedback")
            stage = md.get("stage")
            if stage in (
                FeedbackStage.AWAIT_OWNER_APPROVE.value,
                FeedbackStage.DEVELOP.value,
                FeedbackStage.TEST.value,
                FeedbackStage.SHIPPED.value,
                FeedbackStage.DEFERRED.value,
                FeedbackStage.REJECTED.value,
                FeedbackStage.DUPLICATE.value,
            ):
                raise ServiceError(409, f"cannot amend feedback in stage {stage}")

            edits = {
                "scenario": scenario,
                "expected_outcome": expected_outcome,
                "risk_tags": tags,
                "solution_text": solution_text,
            }
            corrections: list[dict[str, Any]] = list(md.get("corrections") or [])
            changed = False
            for field, new_value in edits.items():
                if new_value is None:
                    continue
                old_value = md.get(field)
                if old_value != new_value:
                    corrections.append({
                        "field": field,
                        "original_value": old_value,
                        "corrected_value": new_value,
                        "reason": reason,
                        "actor": actor.derive_submitted_by(),
                        "timestamp": now_utc().isoformat(),
                        "revision": (fb.revision_count or 0) + 1,
                    })
                    md[field] = new_value
                    changed = True
            if not changed:
                # reason given but nothing changed: no revision bump, no silent
                # overwrite; still audit the no-op intent is unnecessary.
                self.session.rollback()
                return fb

            fb.revision_count = (fb.revision_count or 0) + 1
            md["corrections"] = corrections
            fb.metadata_json = md
            fb.checksum = _compute_feedback_checksum(fb)
            append_audit(
                self.session,
                actor=actor.derive_submitted_by(),
                action=FEEDBACK_AMEND_AUDIT,
                resource_type="artifact",
                resource_id=fb.id,
                project_id=fb.project_id,
                task_id=fb.task_id,
                before={"revision_count": fb.revision_count - 1},
                after={
                    "revision_count": fb.revision_count,
                    "checksum": fb.checksum,
                    "reason": reason,
                },
                idempotency_key=f"audit:feedback:amend:{fb.id}:{fb.revision_count}",
            )
            self.session.commit()
        except (ServiceError, Exception):
            self.session.rollback()
            raise
        self.session.refresh(fb)
        return fb

    # -- transition (single FSM entry point, plan §3.4) ----------------------

    def apply_transition(
        self,
        *,
        artifact_id: str,
        actor: ActorContext,
        transition: FeedbackTransition,
        reason: str | None = None,
        canonical_feedback_id: str | None = None,
        scenario: str | None = None,
        expected_outcome: str | None = None,
        risk_tags: list[str] | None = None,
        solution_text: str | None = None,
    ) -> Artifact:
        """Apply a named transition verb. The ONLY way to change stage.

        Emits exactly ONE audit record per transition (action =
        ``rule.audit_action``); the business-specific verbs (submit / invalidate /
        reject / approve) embed their binding details in that single record.
        """
        if transition not in ALLOWED_TRANSITIONS[FSM_VERSION]:
            raise ServiceError(422, f"unknown transition: {transition}")
        rule = ALLOWED_TRANSITIONS[FSM_VERSION][transition]

        # Owner-only verbs require a trusted owner identity (403 otherwise).
        if rule.owner_only:
            _assert_owner_actor(actor)

        self.session.rollback()
        self.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            fb = self.session.get(Artifact, artifact_id)
            if fb is None:
                raise ServiceError(404, "feedback not found")
            if fb.type != ArtifactType.FEEDBACK:
                raise ServiceError(409, "artifact is not feedback")
            md = dict(fb.metadata_json or {})
            current_stage = md.get("stage")

            # Authorization for submitter_or_owner verbs.
            if (
                rule.actor == "submitter_or_owner"
                and actor.kind != "owner"
                and actor.derive_submitted_by() != md.get("submitted_by")
            ):
                raise ServiceError(403, "transition requires owner or submitter")

            if current_stage not in [s.value for s in rule.required_stages]:
                raise ServiceError(
                    409,
                    f"transition {transition.value} not allowed from stage {current_stage}",
                )

            handler = {
                FeedbackTransition.SUBMIT_FOR_APPROVAL: self._t_submit_for_approval,
                FeedbackTransition.INVALIDATE_PENDING: self._t_invalidate_pending,
                FeedbackTransition.REJECT_SOLUTION: self._t_reject_solution,
                FeedbackTransition.APPROVE_SOLUTION: self._t_approve_solution,
                FeedbackTransition.DEFER: self._t_defer,
                FeedbackTransition.MARK_DUPLICATE: self._t_mark_duplicate,
            }.get(transition)
            audit_after: dict[str, Any] = {}
            if handler is not None:
                audit_after = handler(
                    fb, md, actor, rule,
                    reason=reason,
                    canonical_feedback_id=canonical_feedback_id,
                    scenario=scenario,
                    expected_outcome=expected_outcome,
                    risk_tags=risk_tags,
                    solution_text=solution_text,
                ) or {}
            else:
                # Generic stage move (CLARIFY_REQUESTED / CLARIFIED / ...).
                md["stage"] = rule.target_stage.value

            # Single committed audit for this transition (plan §3.2 / §3.4).
            seq = int(md.get("transition_seq", 0)) + 1
            md["transition_seq"] = seq
            fb.metadata_json = md
            self.session.add(fb)
            append_audit(
                self.session,
                actor=actor.derive_submitted_by(),
                action=rule.audit_action,
                resource_type="artifact",
                resource_id=fb.id,
                project_id=fb.project_id,
                task_id=fb.task_id,
                before={"stage": current_stage},
                after={
                    "stage": rule.target_stage.value,
                    "transition": transition.value,
                    **audit_after,
                },
                idempotency_key=(
                    f"audit:feedback:stage:{fb.id}:{current_stage}->"
                    f"{rule.target_stage.value}:{seq}"
                ),
            )
            self.session.commit()
        except (ServiceError, Exception):
            self.session.rollback()
            raise
        self.session.refresh(fb)
        return fb

    # -- transition handlers (return audit-after extras) --------------------

    def _t_submit_for_approval(
        self, fb, md, actor, rule, *, reason, canonical_feedback_id,
        scenario, expected_outcome, risk_tags, solution_text,
    ) -> dict[str, Any]:
        if not (md.get("solution_text") or "").strip():
            raise ServiceError(409, "solution_text is required before submission")
        # Server-derived increasing round (plan §2.2): max of existing Approval
        # rounds and any pending round, +1.
        existing_rounds = [
            a.review_round
            for a in self.session.exec(
                select(Approval).where(
                    Approval.target_artifact_id == fb.id,
                    Approval.action_type == FEEDBACK_DEVELOP_ACTION,
                )
            ).all()
        ]
        pending_round = (md.get("pending_approval") or {}).get("review_round", 0)
        new_round = max(existing_rounds + [pending_round]) + 1
        md["stage"] = rule.target_stage.value
        md["pending_approval"] = {
            "checksum": fb.checksum,
            "revision": fb.revision_count,
            "submitted_at": now_utc().isoformat(),
            "submitted_by": actor.derive_submitted_by(),
            "action_type": FEEDBACK_DEVELOP_ACTION,
            "review_round": new_round,
        }
        return {"pending_approval": md["pending_approval"]}

    def _t_invalidate_pending(
        self, fb, md, actor, rule, *, reason, canonical_feedback_id,
        scenario, expected_outcome, risk_tags, solution_text,
    ) -> dict[str, Any]:
        """Named verb carrying its own edit params (plan §3.2).

        reason required; edits A-zone; recomputes checksum; bumps revision;
        clears pending_approval; moves to SOLUTION (deterministic single target,
        never CLARIFY). Does NOT go through ``amend_feedback``.
        """
        if not reason or not reason.strip():
            raise ServiceError(422, "INVALIDATE_PENDING requires a reason")
        _check_field_caps(
            original_text=None,
            scenario=scenario,
            expected_outcome=expected_outcome,
            solution_text=solution_text,
            risk_tags=risk_tags,
        )
        solution_text = _nfc(solution_text) if solution_text is not None else None
        scenario = _nfc(scenario) if scenario is not None else None
        expected_outcome = _nfc(expected_outcome) if expected_outcome is not None else None
        tags = sorted(set(risk_tags)) if risk_tags is not None else None

        edits = {
            "scenario": scenario,
            "expected_outcome": expected_outcome,
            "risk_tags": tags,
            "solution_text": solution_text,
        }
        corrections: list[dict[str, Any]] = list(md.get("corrections") or [])
        for field, new_value in edits.items():
            if new_value is None:
                continue
            old_value = md.get(field)
            if old_value != new_value:
                corrections.append({
                    "field": field,
                    "original_value": old_value,
                    "corrected_value": new_value,
                    "reason": reason,
                    "actor": actor.derive_submitted_by(),
                    "timestamp": now_utc().isoformat(),
                    "revision": (fb.revision_count or 0) + 1,
                })
                md[field] = new_value

        fb.revision_count = (fb.revision_count or 0) + 1
        md["corrections"] = corrections
        md["pending_approval"] = None
        md["stage"] = FeedbackStage.SOLUTION.value  # deterministic single target
        fb.metadata_json = md
        fb.checksum = _compute_feedback_checksum(fb)
        return {"cleared_pending_round": (md.get("pending_approval") or {}).get("review_round")}

    def _t_reject_solution(
        self, fb, md, actor, rule, *, reason, canonical_feedback_id,
        scenario, expected_outcome, risk_tags, solution_text,
    ) -> dict[str, Any]:
        pending = md.get("pending_approval")
        round_no = (pending or {}).get("review_round", 1)
        self.session.add(
            Approval(
                project_id=fb.project_id,
                task_id=fb.task_id,
                target_artifact_id=fb.id,
                review_policy_id=None,
                review_round=round_no,
                action_type=FEEDBACK_DEVELOP_ACTION,
                risk_level=_derive_risk_level(md.get("risk_tags")),
                status=ApprovalStatus.REJECTED,
                decided_at=now_utc(),
                rationale=reason,
            )
        )
        md["pending_approval"] = None
        md["stage"] = rule.target_stage.value
        return {"rejected_round": round_no}

    def _t_approve_solution(
        self, fb, md, actor, rule, *, reason, canonical_feedback_id,
        scenario, expected_outcome, risk_tags, solution_text,
    ) -> dict[str, Any]:
        """Owner approval bound to the exact pending solution revision (plan §2.2).

        all-or-none under the surrounding BEGIN IMMEDIATE.
        """
        pending = md.get("pending_approval")
        if pending is None:
            raise ServiceError(409, "no pending approval to approve")
        if fb.checksum != pending.get("checksum") or fb.revision_count != pending.get("revision"):
            raise ServiceError(409, "stale solution: feedback changed since submission")
        round_no = pending.get("review_round", 1)
        # Same-round no-conflict terminal Approval (re-read under the lock).
        conflict = self.session.exec(
            select(Approval).where(
                Approval.target_artifact_id == fb.id,
                Approval.action_type == FEEDBACK_DEVELOP_ACTION,
                Approval.review_round == round_no,
                Approval.status.in_([ApprovalStatus.APPROVED, ApprovalStatus.REJECTED]),
            )
        ).first()
        if conflict is not None:
            raise ServiceError(409, "round already has a terminal approval")

        self.session.add(
            Approval(
                project_id=fb.project_id,
                task_id=fb.task_id,
                target_artifact_id=fb.id,
                review_policy_id=None,
                review_round=round_no,
                action_type=FEEDBACK_DEVELOP_ACTION,
                risk_level=_derive_risk_level(md.get("risk_tags")),
                status=ApprovalStatus.APPROVED,
                decided_at=now_utc(),
                rationale=reason,
            )
        )
        md["pending_approval"] = None
        md["stage"] = rule.target_stage.value
        return {
            "bound_checksum": fb.checksum,
            "bound_revision": fb.revision_count,
            "review_round": round_no,
        }

    def _t_defer(
        self, fb, md, actor, rule, *, reason, canonical_feedback_id,
        scenario, expected_outcome, risk_tags, solution_text,
    ) -> dict[str, Any]:
        # Clear any lingering pending approval if deferred out of AWAIT (plan §3.2).
        cleared_round = None
        if md.get("stage") == FeedbackStage.AWAIT_OWNER_APPROVE.value:
            cleared_round = (md.get("pending_approval") or {}).get("review_round")
            md["pending_approval"] = None
        md["stage"] = rule.target_stage.value
        return {"cleared_pending_round": cleared_round}

    def _t_mark_duplicate(
        self, fb, md, actor, rule, *, reason, canonical_feedback_id,
        scenario, expected_outcome, risk_tags, solution_text,
    ) -> dict[str, Any]:
        if not canonical_feedback_id:
            raise ServiceError(422, "MARK_DUPLICATE requires canonical_feedback_id")
        canonical = self.session.get(Artifact, canonical_feedback_id)
        if canonical is None:
            raise ServiceError(409, "canonical feedback not found")
        if canonical.type != ArtifactType.FEEDBACK:
            raise ServiceError(409, "canonical feedback is not feedback")
        if canonical.project_id != fb.project_id:
            raise ServiceError(409, "canonical feedback is in another project")
        md["duplicate_of"] = canonical.id
        md["stage"] = rule.target_stage.value
        return {"duplicate_of": canonical.id}

    # -- clustering (deterministic, idempotent, superseding, plan §4) --------

    def _load_and_validate_members(
        self, project_id: str, member_ids: list[str]
    ) -> list[Artifact]:
        if not member_ids:
            raise ServiceError(422, "member_ids is required")
        members: list[Artifact] = []
        for mid in member_ids:
            m = self.session.get(Artifact, mid)
            if m is None:
                raise ServiceError(404, f"feedback member not found: {mid}")
            if m.type != ArtifactType.FEEDBACK:
                raise ServiceError(409, f"member is not feedback: {mid}")
            if m.project_id != project_id:
                raise ServiceError(409, f"member in another project: {mid}")
            members.append(m)
        return members

    def record_cluster_run(
        self,
        *,
        actor: ActorContext,
        project_id: str,
        window_start: str,
        window_end: str,
        member_ids: list[str],
        policy_version: str = CLUSTER_POLICY_VERSION,
    ) -> AuditLog:
        """Run a deterministic clustering over ``member_ids`` and persist (or
        return an existing) ``feedback.cluster_summary`` AuditLog.

        Never mutates any Feedback / Task / Approval / KnowledgeFact / Event.
        Idempotent for identical content; supersedes (new row) when content
        changes (plan §4.1).
        """
        members = self._load_and_validate_members(project_id, member_ids)
        # Per-project per-artifact authorization (plan §5): an unrelated agent --
        # even a same-project one -- may not cluster another submitter's feedback.
        # Owner may cluster anything in the project; a submitter only its own.
        for m in members:
            if not self._can_view(actor, m):
                raise ServiceError(
                    403, f"not authorized to cluster feedback {m.id}"
                )
        cluster = cluster_feedback(members)
        cluster_key = cluster.cluster_key
        base_idem = (
            f"cluster:{project_id}:{window_start}:{window_end}:"
            f"{cluster_key}:{policy_version}"
        )

        # Self-contained write under BEGIN IMMEDIATE (plan §4.2).
        self.session.rollback()
        self.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            existing = self.session.exec(
                select(AuditLog).where(AuditLog.idempotency_key == base_idem)
            ).first()
            if existing is not None:
                if _cluster_content_equal(existing.after_snapshot, cluster):
                    # True idempotency: same (window, cluster, policy) input and
                    # same derived content -> same record, no new row.
                    self.session.rollback()
                    return existing
                # Same window key but content changed -> supersede the global head.
                head = _current_cluster_head(
                    self.session, project_id, cluster_key, policy_version
                )
                supersedes = head.id if head is not None else existing.id
                new_idem = f"{base_idem}:{supersedes}"
                return self._persist_cluster_summary(
                    actor=actor,
                    project_id=project_id,
                    window_start=window_start,
                    window_end=window_end,
                    cluster=cluster,
                    idempotency_key=new_idem,
                    supersedes_audit_id=supersedes,
                )

            # No exact base_idem row: a repeated run for the SAME window whose
            # previous result was a superseding row (key base_idem:<head.id>) must
            # still be idempotent when content is unchanged. Detect that tail.
            head = _current_cluster_head(
                self.session, project_id, cluster_key, policy_version
            )
            if (
                head is not None
                and str(head.idempotency_key).startswith(base_idem)
                and _cluster_content_equal(head.after_snapshot, cluster)
            ):
                self.session.rollback()
                return head

            if head is None:
                return self._persist_cluster_summary(
                    actor=actor,
                    project_id=project_id,
                    window_start=window_start,
                    window_end=window_end,
                    cluster=cluster,
                    idempotency_key=base_idem,
                    supersedes_audit_id=None,
                )
            # Head exists (same cluster, different window) -> supersede.
            new_idem = f"{base_idem}:{head.id}"
            return self._persist_cluster_summary(
                actor=actor,
                project_id=project_id,
                window_start=window_start,
                window_end=window_end,
                cluster=cluster,
                idempotency_key=new_idem,
                supersedes_audit_id=head.id,
            )
            if head is None:
                return self._persist_cluster_summary(
                    actor=actor,
                    project_id=project_id,
                    window_start=window_start,
                    window_end=window_end,
                    cluster=cluster,
                    idempotency_key=base_idem,
                    supersedes_audit_id=None,
                )
            # Head exists (same cluster, different window) -> supersede.
            new_idem = f"{base_idem}:{head.id}"
            return self._persist_cluster_summary(
                actor=actor,
                project_id=project_id,
                window_start=window_start,
                window_end=window_end,
                cluster=cluster,
                idempotency_key=new_idem,
                supersedes_audit_id=head.id,
            )
        except (ServiceError, Exception):
            self.session.rollback()
            raise

    def _persist_cluster_summary(
        self,
        *,
        actor: ActorContext,
        project_id: str,
        window_start: str,
        window_end: str,
        cluster: ClusterSummary,
        idempotency_key: str,
        supersedes_audit_id: str | None,
    ) -> AuditLog:
        """Validate + insert one cluster-summary AuditLog (requires an open
        BEGIN IMMEDIATE; callers open the transaction)."""
        _assert_valid_supersession(
            self.session,
            project_id=project_id,
            cluster_key=cluster.cluster_key,
            policy_version=cluster.policy_version,
            supersedes_audit_id=supersedes_audit_id,
        )
        after = {
            "cluster_key": cluster.cluster_key,
            "member_ids": cluster.member_ids,
            "member_revisions": cluster.member_revisions,
            "policy_version": cluster.policy_version,
            "summary": cluster.summary,
            "suggested_priority": cluster.suggested_priority,
            "risk_tags": cluster.risk_tags,
            "window_start": window_start,
            "window_end": window_end,
            "supersedes_audit_id": supersedes_audit_id,
        }
        audit = append_audit(
            self.session,
            actor=actor.derive_submitted_by(),
            action=FEEDBACK_CLUSTER_AUDIT,
            resource_type="project",
            resource_id=project_id,
            project_id=project_id,
            task_id=None,
            before={},
            after=after,
            idempotency_key=idempotency_key,
        )
        self.session.commit()
        self.session.refresh(audit)
        return audit


# --- Module-level clustering helpers (testable / reusable) ------------------


def _cluster_content_equal(snapshot: dict[str, Any] | None, cluster: ClusterSummary) -> bool:
    if not snapshot:
        return False
    return (
        snapshot.get("member_ids") == cluster.member_ids
        and snapshot.get("member_revisions") == cluster.member_revisions
        and snapshot.get("summary") == cluster.summary
        and snapshot.get("suggested_priority") == cluster.suggested_priority
        and snapshot.get("risk_tags") == cluster.risk_tags
    )


def _current_cluster_head(
    session: Session, project_id: str, cluster_key: str, policy_version: str
) -> AuditLog | None:
    """The unique 'current head' = the row nobody points to (plan §4.2)."""
    rows = session.exec(
        select(AuditLog).where(
            AuditLog.action == FEEDBACK_CLUSTER_AUDIT,
            AuditLog.project_id == project_id,
        )
    ).all()
    candidates = [
        r for r in rows
        if (r.after_snapshot or {}).get("cluster_key") == cluster_key
        and (r.after_snapshot or {}).get("policy_version") == policy_version
    ]
    pointed_to = {
        (r.after_snapshot or {}).get("supersedes_audit_id")
        for r in rows
        if (r.after_snapshot or {}).get("supersedes_audit_id")
    }
    for r in candidates:
        if r.id not in pointed_to:
            return r
    return None


def _assert_valid_supersession(
    session: Session,
    *,
    project_id: str,
    cluster_key: str,
    policy_version: str,
    supersedes_audit_id: str | None,
) -> None:
    """Reject branch / ring / cross-cluster supersession (plan §4.2)."""
    if supersedes_audit_id is None:
        return
    target = session.get(AuditLog, supersedes_audit_id)
    if target is None:
        raise ServiceError(409, "supersede target not found")
    tmd = target.after_snapshot or {}
    if tmd.get("cluster_key") != cluster_key:
        raise ServiceError(409, "cross-cluster supersession")
    if tmd.get("policy_version") != policy_version:
        raise ServiceError(409, "cross-policy supersession")
    if tmd.get("supersedes_audit_id") == target.id:
        raise ServiceError(409, "ring supersession (points to itself)")
    # Branch: target already superseded by another row.
    rows = session.exec(
        select(AuditLog).where(
            AuditLog.action == FEEDBACK_CLUSTER_AUDIT,
            AuditLog.project_id == project_id,
        )
    ).all()
    for r in rows:
        if (r.after_snapshot or {}).get("supersedes_audit_id") == target.id:
            raise ServiceError(409, "branch supersession: target already superseded")
