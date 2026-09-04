"""W4: Employee appointment -- running a Trial and promoting it to a hire.

Companion to ``docs/workforce/Workforce_W4_Employee_Spec_V1.md``.

W4 closes the Workforce loop (Discovery -> Evaluation -> Match -> Recommendation
-> Trial -> Employee) by giving the Trial an actual lifecycle and by adding the
terminal ``Employee`` record. It is a PURE ADDITION:

* a new module (this one);
* a new table + four columns (one additive migration);
* one controlled edit to ``workforce.CandidateLifecycle`` (the two TRIALING
  outbound edges).

Nothing in W3-C (``workforce_recommendation.py``) or W3-D
(``workforce_trial.py``) is redefined -- W4 only *reads* what they wrote.

The two-stage human gate (D-6) is the heart of the stage:

    complete_trial        -- records a VERDICT (pass / fail).
    promote_to_employee   -- records the owner's HIRING DECISION.

Completing a trial never creates an ``Employee``; promoting never runs without
an explicit owner call. Both are owner-only, keyword-only, with no default
actor (Q7), so a missing ``actor`` raises ``TypeError`` rather than silently
executing as owner.

Out of scope by R7's decision (Spec §12), each deliberately absent here:

* D-1: no cost gate -- the Workforce chain has no Project to bind one to, and
  cost evidence remains a W5 concern.
* D-4: no delete path for an Employee -- a hire is currently a one-way door,
  which is why ``EmployeeStatus`` has exactly one member.
* D-3: ``JobStatus.FILLED`` is never written.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from aios.actor import ActorContext, _assert_owner_actor
from aios.audit import append_audit
from aios.models import (
    Candidate,
    CandidateStatus,
    Employee,
    EmployeeStatus,
    Trial,
    TrialOutcome,
    TrialStatus,
    now_utc,
)
from aios.services import ServiceError
from aios.workforce import CandidateLifecycle


def _iso(value: datetime | None) -> str | None:
    """Audit snapshots are JSON: render datetimes, never serialise them raw.

    Deliberately a local copy of ``workforce_trial._iso`` rather than an import:
    W3-D is frozen, so W4 must not turn a two-line private helper into shared
    API (Spec §9).
    """
    return value.isoformat() if value is not None else None


def _enum_value(value: Any) -> str | None:
    """Render a StrEnum (or the plain ``str`` SQLite hands back) as its value.

    ``trial.status`` / ``trial.outcome`` are plain ``sa.String()`` columns, so a
    row loaded from the database carries ``"completed"``, not
    ``TrialStatus.COMPLETED``. Both compare and hash equal, but an audit
    snapshot must store the bare string.
    """
    return None if value is None else str(value)


class TrialLifecycle:
    """Explicit, boundary-checked Trial state machine (W4, Q3).

    W3-D left ``Trial.status`` at ``PROPOSED`` with a single writer and no
    transitions, so it shipped no edge table. W4 introduces the first
    transition and therefore introduces the vocabulary, this table, and the
    single status writer (``_transition_trial_status``) TOGETHER -- exactly as
    W3-D Spec §12 C-4 requires.

        PROPOSED  --activate-->  ACTIVE
        PROPOSED  --cancel---->  CANCELLED
        ACTIVE    --complete-->  COMPLETED | FAILED
        ACTIVE    --cancel---->  CANCELLED

    COMPLETED / FAILED / CANCELLED are terminal: no outbound edges. There is no
    "reopen a trial" path -- a trial that ended is history, and a fresh attempt
    is a fresh ``Trial`` (W3-D's UNIQUE(recommendation_id) slot permitting).
    """

    ALLOWED: dict[TrialStatus, set[TrialStatus]] = {
        TrialStatus.PROPOSED: {
            TrialStatus.ACTIVE,
            TrialStatus.CANCELLED,
        },
        TrialStatus.ACTIVE: {
            TrialStatus.COMPLETED,
            TrialStatus.FAILED,
            TrialStatus.CANCELLED,
        },
        TrialStatus.COMPLETED: set(),
        TrialStatus.FAILED: set(),
        TrialStatus.CANCELLED: set(),
    }

    @classmethod
    def can_transition(cls, current: TrialStatus, new: TrialStatus) -> bool:
        return new in cls.ALLOWED.get(current, set())

    @classmethod
    def require_transition(cls, current: TrialStatus, new: TrialStatus) -> None:
        if not cls.can_transition(current, new):
            raise ServiceError(
                409,
                f"illegal trial transition: {_enum_value(current)} -> "
                f"{_enum_value(new)}",
            )


def _transition_trial_status(
    trial: Trial, new_status: TrialStatus, *, ts: datetime
) -> None:
    """The single writer of ``trial.status`` (INV-E3).

    Every W4 state change funnels through here, so the edge table above is the
    ONLY authority on which transitions exist and no call site can set
    ``trial.status`` directly. ``updated_at`` is stamped with the same timestamp
    as the payload columns, keeping one clock per operation.
    """
    TrialLifecycle.require_transition(trial.status, new_status)
    trial.status = new_status
    trial.updated_at = ts


def _trial_snapshot(trial: Trial) -> dict[str, Any]:
    return {
        "status": _enum_value(trial.status),
        "trial_plan_ref": trial.trial_plan_ref,
        "started_at": _iso(trial.started_at),
        "ended_at": _iso(trial.ended_at),
        "outcome": _enum_value(trial.outcome),
    }


def _employee_snapshot(emp: Employee) -> dict[str, Any]:
    return {
        "id": emp.id,
        "candidate_id": emp.candidate_id,
        "trial_id": emp.trial_id,
        "agent_id": emp.agent_id,
        "job_id": emp.job_id,
        "job_version_id": emp.job_version_id,
        "status": _enum_value(emp.status),
        "hired_at": _iso(emp.hired_at),
    }


def _load_trial(session: Session, trial_id: str) -> Trial:
    trial = session.get(Trial, trial_id)
    if trial is None:
        raise ServiceError(404, f"trial not found: {trial_id}")
    return trial


def _load_candidate(session: Session, candidate_id: str) -> Candidate:
    cand = session.get(Candidate, candidate_id)
    if cand is None:
        raise ServiceError(404, f"candidate not found: {candidate_id}")
    return cand


def activate_trial(
    session: Session,
    trial_id: str,
    *,
    plan_ref: str | None = None,
    actor: ActorContext,
) -> Trial:
    """Start a PROPOSED trial (F-E2..F-E5, INV-E4).

    Records *that* the trial is running and since when; it deliberately does
    NOT interpret ``plan_ref`` (an opaque reference, ``Agent.config_ref``
    philosophy) and does NOT move the Candidate -- the candidate already went
    ``RECOMMENDED -> TRIALING`` in W3-D, so activation is internal to the Trial.

    Replays are refused with 409, not silently absorbed (Spec §7): activation
    stamps ``started_at``, and rewriting a start timestamp would be a lie.
    """
    _assert_owner_actor(actor)  # F-E1: keyword-only actor, no default

    trial = _load_trial(session, trial_id)  # F-E2
    # F-E3: a non-PROPOSED trial (including an ACTIVE replay) is 409 -- the edge
    # table is the only authority, so no separate status check is needed.
    before = _trial_snapshot(trial)

    ts = now_utc()
    with session.begin_nested():  # INV-E2: state + audit in one SAVEPOINT
        trial.trial_plan_ref = plan_ref  # F-E4
        trial.started_at = ts
        _transition_trial_status(trial, TrialStatus.ACTIVE, ts=ts)
        session.add(trial)
        session.flush()
        append_audit(
            session,
            actor=actor.owner_id,
            action="trial.activated",
            resource_type="trial",
            resource_id=trial.id,
            project_id=None,
            task_id=None,
            before=before,
            after=_trial_snapshot(trial),
            idempotency_key=f"trial:{trial_id}:activate",  # F-E5
        )
        session.flush()

    return trial


def complete_trial(
    session: Session,
    trial_id: str,
    *,
    outcome: TrialOutcome,
    actor: ActorContext,
) -> Trial:
    """Record the verdict of a running trial (F-E6..F-E10, INV-E5).

    ``outcome`` is binary and mandatory: ``pass`` -> COMPLETED, ``fail`` ->
    FAILED. There is no "unknown" -- an un-assessable trial is CANCELLED
    (``cancel_trial``), never recorded as a guessed verdict.

    INV-E5: this does NOT move the Candidate and does NOT create an Employee.
    A verdict is not a hire -- that is ``promote_to_employee``, a separate
    explicit owner decision (D-6).
    """
    _assert_owner_actor(actor)  # F-E1

    if not isinstance(outcome, TrialOutcome) or outcome not in (
        TrialOutcome.PASS,
        TrialOutcome.FAIL,
    ):
        raise ServiceError(422, f"invalid trial outcome: {outcome!r}")  # F-E8

    trial = _load_trial(session, trial_id)  # F-E6
    new_status = (
        TrialStatus.COMPLETED if outcome == TrialOutcome.PASS else TrialStatus.FAILED
    )
    # F-E7: PROPOSED -> * is illegal (a trial must be activated, or cancelled).
    # ACTIVE is the only state that can reach a verdict.
    before = _trial_snapshot(trial)

    ts = now_utc()
    with session.begin_nested():  # INV-E2
        trial.outcome = outcome  # F-E9
        trial.ended_at = ts
        _transition_trial_status(trial, new_status, ts=ts)
        session.add(trial)
        session.flush()
        append_audit(
            session,
            actor=actor.owner_id,
            action="trial.completed",
            resource_type="trial",
            resource_id=trial.id,
            project_id=None,
            task_id=None,
            before=before,
            after=_trial_snapshot(trial),
            # F-E10
            idempotency_key=f"trial:{trial_id}:complete:{_enum_value(outcome)}",
        )
        session.flush()

    return trial


def cancel_trial(
    session: Session,
    trial_id: str,
    *,
    actor: ActorContext,
) -> Trial:
    """Abandon a trial before it reaches a verdict (F-E11..F-E14).

    ``outcome`` is force-cleared to ``None`` and stays ``None``: a cancellation
    is not an assessment, so it must never leave a verdict behind (F-E13).
    """
    _assert_owner_actor(actor)  # F-E1

    trial = _load_trial(session, trial_id)  # F-E11
    # F-E12: a terminal trial (COMPLETED / FAILED / CANCELLED) cannot be
    # cancelled again -- the edge table rejects it.
    before = _trial_snapshot(trial)

    ts = now_utc()
    with session.begin_nested():  # INV-E2
        trial.outcome = None  # F-E13
        trial.ended_at = ts
        _transition_trial_status(trial, TrialStatus.CANCELLED, ts=ts)
        session.add(trial)
        session.flush()
        append_audit(
            session,
            actor=actor.owner_id,
            action="trial.cancelled",
            resource_type="trial",
            resource_id=trial.id,
            project_id=None,
            task_id=None,
            before=before,
            after=_trial_snapshot(trial),
            idempotency_key=f"trial:{trial_id}:cancel",  # F-E14
        )
        session.flush()

    return trial


def promote_to_employee(
    session: Session,
    trial_id: str,
    *,
    actor: ActorContext,
) -> Employee:
    """Appoint the trialled candidate -- the owner's hiring decision (D-6).

    This is the ONLY creator of an ``Employee``, and it is reachable only from a
    COMPLETED trial (INV-E1): an ACTIVE / FAILED / CANCELLED trial carries no
    mandate to hire.

    The Employee + the ``TRIALING -> EMPLOYED`` candidate move are written in
    ONE SAVEPOINT (INV-E6 / F-E18), so there is never an employed candidate
    without an employee row, nor an employee row whose candidate is still
    trialling.

    Idempotent (F-E21 / Q8): a replay returns the existing row instead of
    writing a second one, and a concurrent first-promote is absorbed off the
    ``UNIQUE(trial_id)`` slot with the winner returned.
    """
    _assert_owner_actor(actor)  # F-E1

    trial = _load_trial(session, trial_id)  # F-E15
    if trial.status != TrialStatus.COMPLETED:  # F-E16 / INV-E1
        raise ServiceError(
            409,
            f"trial is not completed, cannot promote: {_enum_value(trial.status)}",
        )

    # Idempotent replay (F-E21): the UNIQUE slot is taken -> return the winner.
    # NOTE: ``select`` MUST come from ``sqlmodel`` (not ``sqlalchemy``): for a
    # table=True class the Core ``select`` returns immutable Rows, not mapped
    # instances.
    existing = session.exec(
        select(Employee).where(Employee.trial_id == trial_id)
    ).first()
    if existing is not None:
        return existing

    cand = _load_candidate(session, trial.candidate_id)
    if cand.status != CandidateStatus.TRIALING:  # F-E17
        raise ServiceError(
            409,
            f"candidate is not trialing, cannot promote: "
            f"{_enum_value(cand.status)}",
        )

    before = _trial_snapshot(trial)
    ts = now_utc()
    emp = Employee(
        candidate_id=cand.id,
        trial_id=trial.id,
        # F-E19: snapshots copied from the Candidate, never re-resolved --
        # re-reading the registry / the Job head here would make the hire depend
        # on state that can move between the trial and this decision.
        agent_id=cand.agent_id,
        job_id=cand.job_id,
        job_version_id=cand.job_version_id,
        # V1 has one status and one writer: this constructor call.
        status=EmployeeStatus.ACTIVE,
        hired_at=ts,
        created_at=ts,
        updated_at=ts,
    )

    try:
        with session.begin_nested():  # INV-E6 / F-E18
            session.add(emp)
            session.flush()
            CandidateLifecycle.require_transition(
                cand.status, CandidateStatus.EMPLOYED
            )
            cand.status = CandidateStatus.EMPLOYED
            cand.updated_at = ts
            session.add(cand)
            session.flush()
            append_audit(
                session,
                actor=actor.owner_id,
                action="employee.hired",
                resource_type="employee",
                resource_id=emp.id,
                project_id=None,
                task_id=None,
                before=before,  # F-E20: the trial the hire rests on
                after=_employee_snapshot(emp),
                idempotency_key=f"employee:{trial_id}",
            )
            session.flush()
    except IntegrityError:
        # Concurrent first-promote: another writer won the UNIQUE slot. Absorb
        # the race and return the authoritative row (mirrors W3-D §8).
        session.expire_all()
        winner = session.exec(
            select(Employee).where(Employee.trial_id == trial_id)
        ).first()
        if winner is not None:
            return winner
        raise

    return emp


def release_candidate(
    session: Session,
    trial_id: str,
    *,
    actor: ActorContext,
) -> Candidate:
    """Return a failed / cancelled trial's candidate to the talent pool (D-2).

    A trial that did not end in a hire must not leave the candidate stranded in
    ``TRIALING`` (W3-D known gap C-5: the F-R8 withdraw path only releases
    RECOMMENDED). This is the explicit release path -- written by an owner, not
    inferred by the system -- and it moves the candidate back to POOLED, from
    where it can be re-evaluated and re-matched. No new terminal state is
    introduced for a failed hire.

    A COMPLETED trial is NOT releasable: it has a verdict of ``pass`` and must
    go through ``promote_to_employee`` (or stay un-promoted forever, which is a
    decision, not a lock).
    """
    _assert_owner_actor(actor)  # F-E1

    trial = _load_trial(session, trial_id)  # F-E22
    if trial.status not in (TrialStatus.FAILED, TrialStatus.CANCELLED):  # F-E23
        raise ServiceError(
            409,
            f"trial is not releasable: {_enum_value(trial.status)}",
        )

    cand = _load_candidate(session, trial.candidate_id)
    # F-E24: only a TRIALING candidate can be released (a second release, or a
    # concurrent promote, is refused rather than silently overwritten).

    before = {
        "status": _enum_value(cand.status),
        "trial_id": trial.id,
        "trial_status": _enum_value(trial.status),
        "trial_outcome": _enum_value(trial.outcome),
    }

    ts = now_utc()
    with session.begin_nested():  # INV-E2
        CandidateLifecycle.require_transition(
            cand.status, CandidateStatus.POOLED
        )  # F-E24 / F-E25
        cand.status = CandidateStatus.POOLED
        cand.updated_at = ts
        session.add(cand)
        session.flush()
        append_audit(
            session,
            actor=actor.owner_id,
            action="candidate.released",
            resource_type="candidate",
            resource_id=cand.id,
            project_id=None,
            task_id=None,
            before=before,
            after={"status": CandidateStatus.POOLED.value, "trial_id": trial.id},
            idempotency_key=f"trial:{trial_id}:release",  # F-E26
        )
        session.flush()

    return cand
