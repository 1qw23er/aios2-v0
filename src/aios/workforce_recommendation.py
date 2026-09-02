"""W3-C: Recommendation + the L4 human gate (service layer).

Companion to ``docs/Workforce_W3C_Recommendation_Approval_Spec_V4.md``.

Why a separate module: ``workforce.py`` is frozen -- spec §15.2 #16 forbids
touching any definition in it apart from the controlled ``CandidateLifecycle``
edges. This module therefore *reuses* the frozen pieces by import
(``CandidateLifecycle``, ``_attempt_from_evidence_refs``) and adds the W3-C
service functions on top.

Gates implemented here are all fail-closed: an unexplainable, drifting or
fabricated-dimension Match is refused, never recommended (§7).
"""

from __future__ import annotations

import copy
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from aios.actor import ActorContext, _assert_owner_actor
from aios.audit import append_audit
from aios.models import (
    Agent,
    ApprovalStatus,
    Candidate,
    CandidateStatus,
    Match,
    MatchStatus,
    Recommendation,
    RecommendationStatus,
    now_utc,
)
from aios.services import ServiceError
from aios.workforce import CandidateLifecycle, _attempt_from_evidence_refs

# ---------------------------------------------------------------------------
# §5.2 -- the Recommendation state machine (single authoritative edge table)
# ---------------------------------------------------------------------------

RECOMMENDATION_ALLOWED: dict[RecommendationStatus, set[RecommendationStatus]] = {
    RecommendationStatus.PROPOSED: {
        RecommendationStatus.APPROVED,
        RecommendationStatus.REJECTED,
        RecommendationStatus.WITHDRAWN,
    },
    RecommendationStatus.APPROVED: {RecommendationStatus.WITHDRAWN},
    # Terminal (fail-closed): fresh evidence never resurrects a human refusal.
    RecommendationStatus.REJECTED: set(),
    RecommendationStatus.WITHDRAWN: {RecommendationStatus.PROPOSED},
}

#: Statuses a F-R8 withdraw may act on (§16.4: one-way transition).
LIVE_STATUSES: frozenset[RecommendationStatus] = frozenset(
    {
        RecommendationStatus.PROPOSED,
        RecommendationStatus.APPROVED,
    }
)

RECOMMENDER = "workforce_recommendation"

# ``ApprovalStatus`` is reused purely as the *input* vocabulary of a decision
# (§6.3); the persisted state stays in ``RecommendationStatus`` (C2 / INV-2).
# PENDING and EXPIRED are not decisions here -- see §4.4 -- so they are 422.
DECISION_WHITELIST: frozenset[ApprovalStatus] = frozenset(
    {
        ApprovalStatus.APPROVED,
        ApprovalStatus.REJECTED,
    }
)

# The three FUTURE dimensions (§4.3): read-only status snapshots. Never a number.
_UNKNOWN_DIMENSIONS: dict[str, tuple[str, bool]] = {
    # dimension -> (evaluation_context key, advisory_only)
    "reliability": ("reliability_evidence", False),
    "historical": ("historical_evidence", False),
    "cost": ("cost_evidence", True),
}


def _transition_status(
    rec: Recommendation, new_status: RecommendationStatus
) -> None:
    """INV-4: the single writer for ``Recommendation.status``.

    Every state change funnels through here so the edge table is the only thing
    that can authorise a transition; an illegal edge raises 409.
    """
    if new_status not in RECOMMENDATION_ALLOWED.get(rec.status, set()):
        raise ServiceError(
            409,
            f"illegal recommendation state transition: "
            f"{rec.status.value} -> {new_status.value}",
        )
    rec.status = new_status


# ---------------------------------------------------------------------------
# Read-only snapshots (§4.3 / F-R4 / F-R5)
# ---------------------------------------------------------------------------


def _snapshot_unknown_dimensions(ctx: dict[str, Any]) -> dict[str, Any]:
    """Copy the three FUTURE dimensions as status flags -- F-R4.

    Anything that is not a non-empty string status is a fabrication attempt
    (e.g. a numeric reliability score) and is refused with 422.
    """
    out: dict[str, Any] = {}
    for dimension, (key, advisory_only) in _UNKNOWN_DIMENSIONS.items():
        raw = ctx.get(key) or {}
        status = raw.get("status") if isinstance(raw, dict) else None
        if not isinstance(status, str) or not status:
            raise ServiceError(422, "unknown dimension would be fabricated")
        entry: dict[str, Any] = {"status": status, "scored": False}
        if advisory_only:
            entry["advisory_only"] = True
        out[dimension] = entry
    return out


def _build_cost_advisory(session: Session, cand: Candidate) -> str | None:
    """F-R5: cost is advisory *text* only -- never a score component.

    The advisory names the policy keys; it deliberately never echoes any value,
    because a number here would be a pseudo-precise cost signal.
    """
    agent = session.get(Agent, cand.agent_id)
    policy = (agent.cost_policy if agent is not None else None) or {}
    if not policy:
        return None
    keys = ", ".join(sorted(str(key) for key in policy))
    return f"cost policy present (advisory only; never scored): {keys}"


def _build_rationale(
    rec: Recommendation, match: Match
) -> str:
    """Deterministic, templated rationale -- never free-form model prose."""
    return (
        f"candidate matched job_version with score {match.score:.4f} "
        f"(weights {match.weights_version}); "
        f"evaluated={', '.join(match.evaluated_fields) or 'none'}; "
        f"excluded={', '.join(rec.excluded_fields) or 'none'}"
    )


# ---------------------------------------------------------------------------
# F-R8 detection (the withdraw itself lands with the reconcile increment)
# ---------------------------------------------------------------------------


def _detect_drift(
    session: Session, rec: Recommendation
) -> tuple[bool, str | None, int | None]:
    """Has the evidence this recommendation rests on moved? (fail-closed)

    Returns ``(drifted, reason, detected_attempt)``. Unverifiable counts as
    drifted: a missing Match row or an unparsable attempt means the evidence
    can no longer be checked, so it can no longer be trusted (§16.1).
    """
    match = session.get(Match, rec.match_id)
    if match is None:
        return True, "match_missing", None
    detected = _attempt_from_evidence_refs(match.evidence_refs)
    if detected is None:
        return True, "attempt_unresolvable", None
    if detected != rec.match_attempt:
        return True, "match_attempt_drift", detected
    return False, None, detected


# ---------------------------------------------------------------------------
# The gate: EVALUATED + COMPUTED Match -> PROPOSED recommendation
# ---------------------------------------------------------------------------


def _load_match(session: Session, cand: Candidate) -> Match:
    """F-R2: a COMPUTED Match must exist. Missing -> 422, BLOCKED -> 409."""
    match = session.exec(
        select(Match)
        .where(Match.candidate_id == cand.id)
        .where(Match.job_version_id == cand.job_version_id)
    ).first()
    if match is None:
        raise ServiceError(422, "no match computed for this candidate")
    if match.status != MatchStatus.COMPUTED:
        reason = match.match_blocked_reason or match.status.value
        raise ServiceError(409, f"recommendation blocked: {reason}")
    return match


def _excluded_fields(match: Match) -> list[str]:
    """The excluded dimensions live inside ``Match.breakdown["excluded"]``.

    W3-B has no ``excluded_fields`` column (it is frozen); §4.1 defines the
    recommendation's copy as a snapshot of that breakdown entry.
    """
    breakdown = match.breakdown if isinstance(match.breakdown, dict) else {}
    excluded = breakdown.get("excluded") or []
    return list(excluded)


def _assert_explainable(match: Match) -> int:
    """F-R3 + F-R3b: reject an unexplainable Match and return its attempt.

    Only the *required* rings are checked (``cand:`` parsable + ``match:``,
    which W3-C appends itself). The conditional ``br:`` ring is deliberately
    NOT required: an unbound or untrusted benchmark is a legal waived Match in
    W3-B (P2-3 / UW-1).
    """
    if (
        not match.breakdown
        or not match.evaluated_fields
        or not match.evidence_refs
        or not _excluded_fields(match)
    ):
        raise ServiceError(422, "match is not explainable")
    attempt = _attempt_from_evidence_refs(match.evidence_refs)
    if attempt is None:
        # Fail closed: a None here would violate NOT NULL match_attempt and
        # surface as an uncaught IntegrityError (500), never as a 422.
        raise ServiceError(422, "match evidence is not resolvable")
    return attempt


def recommend_candidate(
    session: Session,
    candidate_id: str,
    *,
    recommender: str = RECOMMENDER,
) -> Recommendation:
    """Propose a hire for ``candidate_id`` (F-R1a … F-R5).

    The candidate must be ``EVALUATED`` and carry a ``COMPUTED`` Match whose
    breakdown and evidence are intact. On success the candidate advances
    ``EVALUATED -> RECOMMENDED`` and a ``PROPOSED`` recommendation is written
    together with its ``recommendation.proposed`` audit row, in one SAVEPOINT.

    A replay at the same attempt returns the existing row untouched (§8).
    """
    cand = session.get(Candidate, candidate_id)
    if cand is None:
        raise ServiceError(404, f"candidate not found: {candidate_id}")

    existing = session.exec(
        select(Recommendation).where(
            Recommendation.candidate_id == candidate_id
        )
    ).first()

    if existing is not None:
        if existing.status == RecommendationStatus.WITHDRAWN:
            # F-R8 landed: a withdrawn row may be rebuilt from fresh evidence
            # (§16.2 t5). This is the only path that re-proposes an existing row.
            return _rebuild_recommendation(session, cand, existing)
        drifted, _reason, _detected = _detect_drift(session, existing)
        if not drifted:
            # Idempotent replay: no recompute, no audit, no state change.
            return existing
        # A live row whose evidence has drifted cannot be silently reused: the
        # caller (or W3-D) must go through the lazy F-R8 reconcile first.
        raise ServiceError(
            409, "recommendation is stale; it must be reconciled first"
        )

    # F-R1a: the state machine itself is the gate -- every source state other
    # than EVALUATED is an illegal edge into RECOMMENDED.
    CandidateLifecycle.require_transition(
        cand.status, CandidateStatus.RECOMMENDED
    )

    match = _load_match(session, cand)  # F-R2
    attempt = _assert_explainable(match)  # F-R3 / F-R3b
    unknown_dimensions = _snapshot_unknown_dimensions(
        cand.evaluation_context or {}
    )  # F-R4
    cost_advisory = _build_cost_advisory(session, cand)  # F-R5

    rec = Recommendation(
        candidate_id=cand.id,
        job_version_id=cand.job_version_id,
        match_id=match.id,
        match_attempt=attempt,
        status=RecommendationStatus.PROPOSED,
        proposed_action="hire",
        # Snapshots: deep-copied so a later Match recompute cannot mutate the
        # evidence this proposal was justified by (§10.2).
        score=match.score,
        weights_version=match.weights_version,
        breakdown=copy.deepcopy(match.breakdown),
        evaluated_fields=list(match.evaluated_fields),
        evidence_refs=[*match.evidence_refs, f"match:{match.id}"],
        excluded_fields=_excluded_fields(match),
        unknown_dimensions=unknown_dimensions,
        cost_advisory=cost_advisory,
        recommender=recommender,
    )
    rec.rationale = _build_rationale(rec, match)

    try:
        with session.begin_nested():
            session.add(rec)
            session.flush()
            append_audit(
                session,
                actor=recommender,
                action="recommendation.proposed",
                resource_type="recommendation",
                resource_id=rec.id,
                project_id=None,
                task_id=None,
                before={},
                after={
                    "match_id": rec.match_id,
                    "match_attempt": rec.match_attempt,
                    "score": rec.score,
                    "weights_version": rec.weights_version,
                    "evaluated_fields": rec.evaluated_fields,
                    "evidence_refs": rec.evidence_refs,
                    "excluded_fields": rec.excluded_fields,
                    "unknown_dimensions": rec.unknown_dimensions,
                    "proposed_action": rec.proposed_action,
                    "status": rec.status.value,
                },
                idempotency_key=(
                    f"recommend:{cand.id}:{cand.job_version_id}"
                ),
            )
            cand.status = CandidateStatus.RECOMMENDED
            session.add(cand)
            session.flush()
    except IntegrityError:
        # Concurrent first-create: another writer won the UNIQUE slot. Absorb
        # the race and return the authoritative row instead (§8).
        session.expire_all()
        winner = session.exec(
            select(Recommendation).where(
                Recommendation.candidate_id == candidate_id
            )
        ).first()
        if winner is not None:
            return winner
        raise

    return rec


def _rebuild_recommendation(
    session: Session, cand: Candidate, rec: Recommendation
) -> Recommendation:
    """F-R8 rebuild (§16.2 t5): re-propose a WITHDRAWN row from fresh evidence.

    The row is UPDATED in place -- the ``UNIQUE(candidate_id, job_version_id)``
    slot is already taken by the withdrawn row, so a second insert would collide.
    Every decision field is cleared and the snapshot re-copied, so a rebuilt row
    is indistinguishable from a fresh proposal except for its id (INV-3/INV-5).
    """
    old_match_attempt = rec.match_attempt

    CandidateLifecycle.require_transition(
        cand.status, CandidateStatus.RECOMMENDED
    )  # F-R1a

    match = _load_match(session, cand)  # F-R2
    attempt = _assert_explainable(match)  # F-R3 / F-R3b
    unknown_dimensions = _snapshot_unknown_dimensions(
        cand.evaluation_context or {}
    )  # F-R4
    cost_advisory = _build_cost_advisory(session, cand)  # F-R5

    _transition_status(rec, RecommendationStatus.PROPOSED)  # INV-4
    rec.proposed_action = "hire"
    rec.match_id = match.id
    rec.match_attempt = attempt
    rec.score = match.score
    rec.weights_version = match.weights_version
    rec.breakdown = copy.deepcopy(match.breakdown)
    rec.evaluated_fields = list(match.evaluated_fields)
    rec.evidence_refs = [*match.evidence_refs, f"match:{match.id}"]
    rec.excluded_fields = _excluded_fields(match)
    rec.unknown_dimensions = unknown_dimensions
    rec.cost_advisory = cost_advisory
    rec.rationale = _build_rationale(rec, match)
    # INV-3: a rebuild wipes the prior human decision.
    rec.decided_by = None
    rec.decided_at = None
    rec.decision_rationale = None
    rec.updated_at = now_utc()

    try:
        with session.begin_nested():
            session.add(rec)
            session.flush()
            append_audit(
                session,
                actor=RECOMMENDER,
                action="recommendation.reproposed",
                resource_type="recommendation",
                resource_id=rec.id,
                project_id=None,
                task_id=None,
                before={
                    "match_attempt": old_match_attempt,
                    "status": RecommendationStatus.WITHDRAWN.value,
                },
                after={
                    "match_attempt": attempt,
                    "status": RecommendationStatus.PROPOSED.value,
                },
                idempotency_key=(
                    f"recommend:{cand.id}:{cand.job_version_id}:rebuild:{attempt}"
                ),
            )
            cand.status = CandidateStatus.RECOMMENDED
            session.add(cand)
            session.flush()
    except IntegrityError:
        # Concurrent rebuild: another writer won the UNIQUE slot. Absorb the race.
        session.expire_all()
        winner = session.exec(
            select(Recommendation).where(
                Recommendation.candidate_id == cand.id
            )
        ).first()
        if winner is not None:
            return winner
        raise

    return rec


# ---------------------------------------------------------------------------
# §6 -- the L4 human gate (INV-6: decide_recommendation is the only decider)
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    """Audit snapshots are JSON: render datetimes, never serialise them raw."""
    return value.isoformat() if value is not None else None


def _sync_candidate_back(session: Session, rec: Recommendation) -> None:
    """Release the candidate: ``RECOMMENDED -> EVALUATED`` (idempotent).

    This is the *only* outbound edge of ``RECOMMENDED`` (§5.1). It is reached
    both when a human rejects and when the system withdraws, so it must be safe
    to call on a candidate that is already back -- hence the guard instead of an
    unconditional ``require_transition``.
    """
    cand = session.get(Candidate, rec.candidate_id)
    if cand is None or cand.status != CandidateStatus.RECOMMENDED:
        return
    CandidateLifecycle.require_transition(
        cand.status, CandidateStatus.EVALUATED
    )
    cand.status = CandidateStatus.EVALUATED
    session.add(cand)


def decide_recommendation(
    session: Session,
    recommendation_id: str,
    decision: ApprovalStatus,
    rationale: str | None = None,
    *,
    actor: ActorContext,
) -> Recommendation:
    """The single L4 human gate (§6.2 / F-R7 / F-R9 / INV-3 / INV-6).

    ``actor`` is keyword-only and has **no default** (P2-1): omitting it raises
    ``TypeError`` rather than silently elevating the caller to owner. Only a
    ``PROPOSED`` row may be decided, and only into ``APPROVED`` / ``REJECTED``;
    a rejection also releases the candidate (``RECOMMENDED -> EVALUATED``).

    Status and audit are written in one SAVEPOINT (INV-5), so there is no path
    where a decision exists without its evidence trail.
    """
    # F-R9: an agent or system actor is refused with 403 -- not rewritten.
    _assert_owner_actor(actor)

    try:
        normalized = ApprovalStatus(decision)
    except ValueError:
        raise ServiceError(422, "invalid decision") from None
    if normalized not in DECISION_WHITELIST:
        raise ServiceError(422, "invalid decision")

    rec = session.get(Recommendation, recommendation_id)
    if rec is None:
        raise ServiceError(404, f"recommendation not found: {recommendation_id}")
    if rec.status != RecommendationStatus.PROPOSED:
        # Decided rows are terminal; a withdrawn one must be re-proposed first.
        raise ServiceError(409, "该推荐已被决策，不能重复决策")

    before = {
        "status": rec.status.value,
        "decided_by": rec.decided_by,
        "decided_at": _iso(rec.decided_at),
        "decision_rationale": rec.decision_rationale,
        "match_attempt": rec.match_attempt,
    }
    new_status = RecommendationStatus(normalized.value)
    decided_at = now_utc()

    with session.begin_nested():
        _transition_status(rec, new_status)  # INV-4: the only status writer
        rec.decided_by = actor.owner_id  # INV-3: a decision names its decider
        rec.decided_at = decided_at
        rec.decision_rationale = rationale
        rec.updated_at = decided_at
        session.add(rec)
        session.flush()
        append_audit(
            session,
            actor=actor.owner_id,
            action="recommendation.decided",
            resource_type="recommendation",
            resource_id=rec.id,
            project_id=None,
            task_id=None,
            before=before,
            after={
                "status": new_status.value,
                "decision": normalized.value,
                "decided_by": rec.decided_by,
                "rationale": rationale,
                "match_attempt": rec.match_attempt,
            },
            idempotency_key=f"rec:{rec.id}:decision:{normalized.value}",
        )
        if new_status == RecommendationStatus.REJECTED:
            _sync_candidate_back(session, rec)
        session.flush()

    return rec


# ---------------------------------------------------------------------------
# §11 -- the Trial gate consumed by W3-D / W4 (F-R6 + lazy F-R8 reconcile, C1)
# ---------------------------------------------------------------------------


def _reconcile_drift(session: Session, rec: Recommendation) -> bool:
    """F-R8 lazy reconcile (§16). Returns True iff a withdrawal was applied.

    The *only* writer of the F-R8 ``WITHDRAWN`` transition. Uses a CAS so that
    under concurrency exactly one writer wins the withdrawal (§16.8 / C8). Runs
    inside a SAVEPOINT of the caller's transaction and never commits on its own
    (§16.5): ``recommendation.withdrawn`` + the candidate release are written in
    the same SAVEPOINT, so there is never a "withdrawn but no audit" gap (INV-5).
    """
    if rec.status not in LIVE_STATUSES:
        # REJECTED / WITHDRAWN are terminal: nothing left to reconcile.
        return False

    drifted, reason, detected_attempt = _detect_drift(session, rec)
    if not drifted:
        return False

    # C8: the CAS optimistic-lock token is the value this row carried when the
    # reconcile began -- NOT the Match's current attempt. If another writer has
    # already rebuilt this row (new attempt), the token will not match and the
    # CAS returns rowcount 0, so we never double-withdraw.
    stored_match_attempt = rec.match_attempt
    nested = session.begin_nested()
    try:
        result = session.execute(
            text(
                "UPDATE recommendation "
                "SET status = :withdrawn "
                "WHERE id = :rec_id "
                "AND status IN (:proposed, :approved) "
                "AND match_attempt = :stored"
            ),
            {
                # The ``status`` column is an Enum: SQLAlchemy persists the *member
                # name* ("APPROVED", not "approved"), so the CAS must bind names.
                "withdrawn": RecommendationStatus.WITHDRAWN.name,
                "rec_id": rec.id,
                "proposed": RecommendationStatus.PROPOSED.name,
                "approved": RecommendationStatus.APPROVED.name,
                "stored": stored_match_attempt,
            },
        )
        if result.rowcount == 0:
            # Another writer already reconciled / rebuilt this row. Fail closed:
            # no audit, no state write, then re-read so the caller sees reality.
            nested.rollback()
            session.expire(rec)
            return False

        live_status = rec.status  # still the live value in memory here
        _transition_status(rec, RecommendationStatus.WITHDRAWN)  # INV-4
        session.add(rec)
        session.flush()
        append_audit(
            session,
            actor=rec.decided_by or RECOMMENDER,
            action="recommendation.withdrawn",
            resource_type="recommendation",
            resource_id=rec.id,
            project_id=None,
            task_id=None,
            before={
                "status": live_status.value,
                "decided_by": rec.decided_by,
                "decided_at": _iso(rec.decided_at),
                "decision_rationale": rec.decision_rationale,
                "match_attempt": stored_match_attempt,
            },
            after={
                "status": RecommendationStatus.WITHDRAWN.value,
                "detected_attempt": detected_attempt,
                "reason": reason,
                "match_attempt": stored_match_attempt,
            },
            idempotency_key=(
                f"rec:{rec.id}:withdrawn:{stored_match_attempt}"
            ),
        )
        _sync_candidate_back(session, rec)
        session.flush()
        nested.commit()
        return True
    except Exception:
        nested.rollback()
        raise


def assert_trial_eligible(session: Session, recommendation_id: str) -> bool:
    """Is this recommendation allowed to become a Trial? (F-R6 / INV-3)

    True exactly when the row is ``APPROVED`` **and** carries the identity of
    the human who approved it -- an approved row with no decider is a broken
    invariant, not a permission. Anything else (missing, PROPOSED, REJECTED,
    WITHDRAWN) is False: fail-closed by construction.

    Per C1 this is also the **lazy F-R8 reconcile trigger**: a live row whose
    evidence has drifted is withdrawn here (and its candidate released) rather
    than trusting the caller to remember a separate reconcile call. The withdraw
    writes its audit in the same transaction, so W3-D can rely on this single
    function as the gate -- no separate reconcile step required (§16.6).
    """
    rec = session.get(Recommendation, recommendation_id)
    if rec is None:
        return False
    _reconcile_drift(session, rec)
    # The CAS may have changed the row via raw SQL; re-read to see reality.
    session.refresh(rec)
    return (
        rec.status == RecommendationStatus.APPROVED and bool(rec.decided_by)
    )


# ---------------------------------------------------------------------------
# §6 / D-R1 -- the purge gate (C4 unlock path; fails closed unless terminal)
# ---------------------------------------------------------------------------


def purge_recommendation(
    session: Session,
    recommendation_id: str,
    *,
    actor: ActorContext,
) -> None:
    """Physically delete a *terminal* recommendation row (D-R1 / F-R10 / F-R11).

    This is the **only** unlock path for the ``recommendation.match_id`` RESTRICT
    (C4): a candidate that still backs a recommendation row cannot be cascade
    deleted with its Job, so the row must be explicitly removed first. To avoid
    destroying live evidence, purge is refused (409, fail-closed) unless the row
    is in a terminal state -- ``WITHDRAWN`` (system-invalidated) or ``REJECTED``
    (human-refused); a live ``PROPOSED`` / ``APPROVED`` row must go through the
    human gate, never be silently erased (DR-3).

    ``actor`` is keyword-only and has **no default** (P2-6 / F-R11): omitting it
    raises ``TypeError`` rather than silently acting as owner; a non-owner actor
    is refused with 403 by ``_assert_owner_actor``. The deletion writes a full
    snapshot audit (``recommendation.deleted``) so the evidence chain survives
    the row removal, with ``actor`` set to the owner's id -- an identity-less
    deletion is never permitted.
    """
    # F-R11: a non-owner actor is refused with 403 -- not rewritten here.
    _assert_owner_actor(actor)

    rec = session.get(Recommendation, recommendation_id)
    if rec is None:
        raise ServiceError(404, f"recommendation not found: {recommendation_id}")
    if rec.status not in (
        RecommendationStatus.WITHDRAWN,
        RecommendationStatus.REJECTED,
    ):
        raise ServiceError(
            409, "recommendation cannot be purged unless it is withdrawn or rejected"
        )

    # Full-column snapshot: the row is about to vanish, so the audit must carry
    # every column as evidence (§9 / F-R11 / P2-6 -- no identity-less erasure).
    snapshot: dict[str, Any] = {}
    for name in Recommendation.model_fields:
        value = getattr(rec, name)
        if isinstance(value, datetime):
            snapshot[name] = _iso(value)
        elif isinstance(value, Enum):
            snapshot[name] = value.value
        else:
            snapshot[name] = value

    with session.begin_nested():
        append_audit(
            session,
            actor=actor.owner_id,
            action="recommendation.deleted",
            resource_type="recommendation",
            resource_id=rec.id,
            project_id=None,
            task_id=None,
            before={},
            after=snapshot,
            idempotency_key=f"rec:{rec.id}:purge:{actor.owner_id}",
        )
        session.delete(rec)
        session.flush()
