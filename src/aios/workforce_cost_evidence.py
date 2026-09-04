"""W5: cost evidence bookkeeping for the Workforce hiring pipeline.

Companion to ``docs/workforce/Workforce_W5_Design_V1.md``.

W5 V1 is **bookkeeping-only** (G2 / D-1.2): this module records cost FACTS and
enforces nothing. It deliberately does NOT:

* call ``delegation.check_budget`` or any hard budget gate;
* read or write ``Project.budget_used``;
* fabricate a Workforce-native cost source event.

The honesty constraint (D-1.4 / I4) is structural: the repo today has NO
Workforce-native cost source event (the only realized cost, ``DelegatedRun.cost``,
belongs to the delegation domain and is bound ``Task -> Project``, referencing
no Workforce row). ``record_cost_evidence`` therefore exists as the writer
CONTRACT -- schema + idempotency + audit -- and in V1 has **no caller**. A row
is created IFF a real source event occurs and some future stage invokes this
writer with that event's natural identity. ``delegated_run.id`` is never
accepted as a stand-in for a Workforce-attributable event.

Transactional semantics (boundary invariant): the evidence insert and its
audit trail share ONE savepoint -- both commit together or neither does. Replay
of the same source event recomputes the same ``idempotency_key``; the UNIQUE
index rejects the duplicate insert and the ``IntegrityError`` propagates to the
caller, which treats it as already-recorded (at-most-once, no double count,
no silent absorption).
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from aios.actor import ActorContext, _assert_owner_actor
from aios.audit import append_audit
from aios.models import CostEvidence, Employee, JobVersion, now_utc
from aios.services import ServiceError


def _cost_evidence_snapshot(ce: CostEvidence) -> dict[str, Any]:
    return {
        "job_version_id": ce.job_version_id,
        "employee_id": ce.employee_id,
        "amount": ce.amount,
        "source_event_type": ce.source_event_type,
        "source_event_id": ce.source_event_id,
    }


def _load_job_version(session: Session, job_version_id: str) -> JobVersion:
    jv = session.get(JobVersion, job_version_id)
    if jv is None:
        raise ServiceError(404, f"job version not found: {job_version_id}")
    return jv


def _load_employee(session: Session, employee_id: str) -> Employee:
    emp = session.get(Employee, employee_id)
    if emp is None:
        raise ServiceError(404, f"employee not found: {employee_id}")
    return emp


def record_cost_evidence(
    session: Session,
    *,
    job_version_id: str,
    amount: float,
    source_event_type: str,
    source_event_id: str,
    actor: ActorContext,
    employee_id: str | None = None,
    note: str | None = None,
) -> CostEvidence:
    """Create one ``cost_evidence`` row bound to a REAL source event.

    Owner-only, keyword-only, with no default actor (W4 Q7 pattern): a missing
    ``actor`` raises ``TypeError``, a non-owner actor raises 403.

    Idempotent by construction (I5): ``idempotency_key`` is derived from the
    source event's natural key, so replaying the same event fails on the
    UNIQUE index with ``IntegrityError`` -- the caller treats that as
    "already recorded". Duplicate real events with distinct ids produce two
    rows (two distinct cost facts).

    Fail-closed (I3): the referenced ``JobVersion`` (and ``Employee``, when
    given) must exist -- a dangling attribution is a 404, never a silently
    orphaned row.

    No Budget gate (I8): this function never calls ``check_budget``, never
    touches ``Project.budget_used``, and enforces no ceiling. Pure bookkeeping.
    """
    _assert_owner_actor(actor)  # owner-only, no default

    # F-W5-P1: a row cannot be created without a real source event identity --
    # no default, no synthetic value, and an empty identity is a hard reject.
    if not source_event_type or not source_event_id:
        raise ServiceError(
            422,
            "source_event_type and source_event_id are required: cost evidence "
            "must bind a real source event, never a fabricated one",
        )
    # I6: only real measured costs are recorded -- "no measurement" must stay
    # "no row", never an implied zero-cost fact.
    if amount is None:
        raise ServiceError(422, "amount is required: only measured costs are recorded")

    _load_job_version(session, job_version_id)  # fail-closed anchor
    if employee_id is not None:
        _load_employee(session, employee_id)

    idempotency_key = f"{source_event_type}:{source_event_id}"  # I5
    ce = CostEvidence(
        job_version_id=job_version_id,
        employee_id=employee_id,
        amount=amount,
        source_event_type=source_event_type,
        source_event_id=source_event_id,
        idempotency_key=idempotency_key,
        recorded_at=now_utc(),
        note=note,
    )

    # Evidence + audit in ONE savepoint (transactional consistency invariant):
    # a replay fails the evidence UNIQUE on the first flush and rolls back both
    # writes together -- the IntegrityError propagates, nothing is absorbed.
    with session.begin_nested():
        session.add(ce)
        session.flush()
        append_audit(
            session,
            actor=actor.owner_id,
            action="cost_evidence.create",
            resource_type="cost_evidence",
            resource_id=ce.id,
            # Workforce rows have no Project/Task (I10): both stay None.
            project_id=None,
            task_id=None,
            before={},
            after=_cost_evidence_snapshot(ce),
            # Same key as the row -- reuse, not a second key (Design §7).
            idempotency_key=idempotency_key,
        )
        session.flush()

    return ce
