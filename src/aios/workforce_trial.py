"""W3-D: Trial -- the hand-off record from an APPROVED Recommendation (service layer).

Companion to ``docs/workforce/Workforce_W3D_Trial_Spec_V1.md``.

W3-D opens exactly one edge of the Workforce loop: it takes a human-approved
Recommendation (the L4 gate, owned by W3-C) and records the hand-off into a
trial, pushing the candidate to ``TRIALING``. It does NOT implement the substance
of the trial (plan / dates / outcome) -- that is W4 -- and it does NOT touch any
W3-C definition; it only *calls* ``assert_trial_eligible`` as its single gate.

Every rule here is fail-closed (Spec §6):

* the only creator of a ``Trial`` is ``create_trial_from_approval``;
* a non-owner actor is refused with 403 (F-T4);
* an ineligible / drifting recommendation is refused with 409 (F-T1 / F-T2);
* a missing recommendation is refused with 404 (F-T5);
* the three parent FKs are RESTRICT, so the trial survives any upstream delete
  (no unlock path in V1, see §3-Q2).

The single gate, ``assert_trial_eligible``, also runs the lazy F-R8 reconcile,
so a drifted approval is withdrawn (and audited) here rather than trusting the
caller to remember a separate reconcile step (§6.1).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from aios.actor import ActorContext, _assert_owner_actor
from aios.audit import append_audit
from aios.models import (
    Candidate,
    CandidateStatus,
    Recommendation,
    Trial,
    now_utc,
)
from aios.services import ServiceError
from aios.workforce import CandidateLifecycle
from aios.workforce_recommendation import assert_trial_eligible


def _iso(value: datetime | None) -> str | None:
    """Audit snapshots are JSON: render datetimes, never serialise them raw."""
    return value.isoformat() if value is not None else None


def create_trial_from_approval(
    session: Session,
    recommendation_id: str,
    *,
    actor: ActorContext,
) -> Trial:
    """Hand an APPROVED recommendation into a Trial (F-T1 / F-T4 / INV-T5).

    ``assert_trial_eligible`` is the single gate: it is False for a missing,
    PROPOSED, REJECTED or WITHDRAWN row, and it runs the lazy F-R8 reconcile
    first, so drifted evidence is withdrawn (and audited) here rather than
    trusting the caller to remember a separate reconcile step.

    Idempotent (§7): a second call for the same (still-eligible) recommendation
    returns the existing ``Trial`` row without writing a second audit or moving
    the candidate again. On a concurrent first-create the ``UNIQUE`` slot is
    absorbed and the winner is returned (mirrors ``recommend_candidate`` §8).
    """
    _assert_owner_actor(actor)  # F-T4: keyword-only actor, no default

    rec = session.get(Recommendation, recommendation_id)
    if rec is None:
        raise ServiceError(404, f"recommendation not found: {recommendation_id}")

    if not assert_trial_eligible(session, recommendation_id):  # F-T1 / F-T2
        raise ServiceError(409, "recommendation is not eligible for trial")

    # Idempotent replay (§7): the UNIQUE slot is taken -> return the winner.
    # NOTE: ``select`` MUST come from ``sqlmodel`` (not ``sqlalchemy``): for a
    # table=True class the Core ``select`` returns immutable Rows, not mapped
    # instances, and ``.recommendation_id`` would raise AttributeError on replay.
    existing = session.exec(
        select(Trial).where(Trial.recommendation_id == recommendation_id)
    ).first()
    if existing is not None:
        return existing

    before = {
        "status": rec.status.value,
        "decided_by": rec.decided_by,
        "decided_at": _iso(rec.decided_at),
        "decision_rationale": rec.decision_rationale,
        "match_attempt": rec.match_attempt,
    }
    cand = session.get(Candidate, rec.candidate_id)
    if cand is None:
        raise ServiceError(404, f"candidate not found: {rec.candidate_id}")

    created_at = now_utc()
    trial = Trial(  # INV-T2: status comes from the constructor default
        candidate_id=rec.candidate_id,
        job_version_id=rec.job_version_id,
        recommendation_id=rec.id,
        created_at=created_at,
        updated_at=created_at,
    )

    try:
        with session.begin_nested():  # INV-T5: state + audit + candidate in one SAVEPOINT
            session.add(trial)
            session.flush()
            CandidateLifecycle.require_transition(
                cand.status, CandidateStatus.TRIALING
            )
            cand.status = CandidateStatus.TRIALING
            session.add(cand)
            session.flush()
            append_audit(
                session,
                actor=actor.owner_id,
                action="trial.created",
                resource_type="trial",
                resource_id=trial.id,
                project_id=None,
                task_id=None,
                before=before,
                after={
                    "candidate_id": trial.candidate_id,
                    "job_version_id": trial.job_version_id,
                    "recommendation_id": trial.recommendation_id,
                    "status": trial.status.value,
                    "candidate_status": CandidateStatus.TRIALING.value,
                    "match_attempt": rec.match_attempt,
                },
                idempotency_key=f"trial:{rec.id}",
            )
            session.flush()
    except IntegrityError:
        # Concurrent first-create: another writer won the UNIQUE slot. Absorb the
        # race and return the authoritative row (mirrors recommend_candidate §8).
        session.expire_all()
        winner = session.exec(
            select(Trial).where(Trial.recommendation_id == recommendation_id)
        ).first()
        if winner is not None:
            return winner
        raise

    return trial
