"""W8-P1: the Employee cost closure -- W8 attribution x W5 evidence ledger.

Companion to ``docs/workforce/Workforce_W8P1_Cost_Closure_V1.md``. This module
composes the two halves that were, until now, dead ends on their own:

* W8-v2 attribution (``employee_bridge.employee_for_task``): a Task created
  through ``assign_work_to_employee`` is genuinely Workforce-attributable --
  the binding interval at ``Task.created_at`` names the Employee, stably,
  forever, across later rebinds.
* W5 evidence ledger (``workforce_cost_evidence.record_cost_evidence``): the
  append-only writer CONTRACT that had no caller, because the repo had no
  Workforce-native cost source event (checkpoint gap G-5).

W8 closes G-5: the source event is **a bridge Task's measured execution
cost**. When runs delegated for an attributable Task report real cost, this
module records ONE ``cost_evidence`` row per Task:

    job_version_id  = Employee.job_version_id  (the frozen hiring version --
                      the aggregation anchor CostEvidence requires NOT NULL)
    employee_id     = employee_for_task(task)  (W8 attribution; None -> refuse)
    amount          = sum of the Task's DelegatedRun.cost values (measured)
    source_event    = ("employee_bridge_task", task.id)

Why ``task.id`` (not ``delegated_run.id``) is legitimate here: the D-1.4
honesty rule bans reusing ``delegated_run.id`` because a plain delegation run
belongs to the ``Task -> Project`` domain and references no Workforce row. A
W8 bridge Task is a DIFFERENT fact: it exists BECAUSE an Employee was bound to
an Agent, so the Task itself carries Workforce lineage. The ban is untouched
-- this module never uses ``delegated_run.id`` as a source event id; the
at-most-once anchor is the Task's own natural identity.

Dependency direction (seam guards): this module sits ABOVE both sides as an
owner-facing composition layer. It imports the bridge (``aios.employee_bridge``)
and the W5 writer (``aios.workforce_cost_evidence``); neither imports back.
It is NOT a ``workforce*.py`` recruitment module and NOT a bridge seam module,
so the W7/W8 seam scans (which only constrain those globs) do not apply to it;
it imports no execution/scheduler engine itself -- reading ``DelegatedRun``
rows is a model-level read, not an engine call.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from aios.actor import ActorContext, _assert_owner_actor
from aios.employee_bridge import employee_for_task
from aios.models import CostEvidence, DelegatedRun, Employee, Task
from aios.services import ServiceError
from aios.workforce_cost_evidence import record_cost_evidence

# The provenance pair written on every closure row (I4: real event identity).
SOURCE_EVENT_TYPE = "employee_bridge_task"


def _load_task(session: Session, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if task is None:
        raise ServiceError(404, "Task not found")
    return task


def _measured_cost(session: Session, task_id: str) -> float:
    """Sum the Task's delegated-run costs; refuse implied zeros (W5 I6).

    ``DelegatedRun.cost`` defaults to 0.0 and is only overwritten when the
    remote agent actually reports a cost (``delegation._wait_for_completion``).
    A total of 0 therefore means "no measurement exists", and per I6 "no
    measurement" must stay "no row" -- never an ``amount = 0`` fact.
    """
    runs = session.exec(
        select(DelegatedRun).where(DelegatedRun.task_id == task_id)
    ).all()
    return sum(float(run.cost) for run in runs if run.cost is not None)


def record_task_cost_evidence(
    session: Session,
    *,
    task_id: str,
    actor: ActorContext,
    note: str | None = None,
) -> CostEvidence:
    """Close the loop for ONE bridge Task: attribution -> measured cost -> row.

    Owner-only (W4 Q7 pattern). Semantics:

    * 404 -- the Task does not exist.
    * 422 ``task_not_employee_attributable`` -- ``employee_for_task`` resolves
      to ``None`` (a plain delegation Task with no Workforce lineage): refusing
      is the honesty constraint -- evidence is never fabricated for work no
      Employee bears.
    * 422 ``no_measured_cost`` -- no delegated run of the Task has reported a
      cost (I6: no measurement = no row, never ``amount = 0``).
    * 409 ``cost_evidence_already_recorded`` -- replay: the idempotency key
      ``employee_bridge_task:<task_id>`` already exists (at-most-once, W5 I5).
      Nothing is double-counted; the caller learns the fact is already booked.

    One row per Task, ever: runs completing AFTER a Task's evidence was
    recorded are not retro-merged (V1 simplification -- record when the Task's
    cost picture is final).
    """
    _assert_owner_actor(actor)  # owner-only, no default

    task = _load_task(session, task_id)

    employee_id = employee_for_task(session, task)
    if employee_id is None:
        raise ServiceError(
            422,
            "task_not_employee_attributable: cost evidence must name the "
            "Employee bearing the cost; this Task has no Workforce binding "
            "lineage at its creation time",
        )
    # Fail-closed anchor (W5 I3 re-check is redundant but keeps this module
    # self-sufficient if the writer contract ever widens).
    employee = session.get(Employee, employee_id)
    if employee is None:  # pragma: no cover - FK-guaranteed
        raise ServiceError(404, "employee not found")

    amount = _measured_cost(session, task.id)
    if amount <= 0:
        raise ServiceError(
            422,
            "no_measured_cost: no delegated run of this Task has reported a "
            "cost; unmeasured work is never recorded as a zero-cost fact (I6)",
        )

    try:
        ce = record_cost_evidence(
            session,
            job_version_id=employee.job_version_id,
            amount=amount,
            source_event_type=SOURCE_EVENT_TYPE,
            source_event_id=task.id,
            actor=actor,
            employee_id=employee.id,
            note=note,
        )
    except IntegrityError:
        # W5 writer: the evidence insert and its audit share ONE savepoint; a
        # replay fails the UNIQUE idempotency_key and rolls back BOTH writes.
        # Roll the outer transaction back too, then surface 409.
        session.rollback()
        raise ServiceError(409, "cost_evidence_already_recorded") from None
    # Persist (the bridge-service convention: API-facing services commit; the
    # request-scoped ``get_session`` dependency never auto-commits).
    session.commit()
    session.refresh(ce)
    return ce


def employee_cost_summary(session: Session, employee_id: str) -> dict[str, Any]:
    """Aggregate one Employee's recorded cost evidence (read-only).

    404 when the Employee does not exist; otherwise the evidence count, the
    total measured amount, and the rows themselves (oldest first). Pure
    projection -- CostEvidence is a ledger, never a budget authority (W7-I8).
    """
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise ServiceError(404, "Employee not found")
    rows = list(
        session.exec(
            select(CostEvidence)
            .where(CostEvidence.employee_id == employee_id)
            .order_by(CostEvidence.recorded_at.asc())
        ).all()
    )
    return {
        "employee_id": employee_id,
        "job_version_id": employee.job_version_id,
        "evidence_count": len(rows),
        "total_amount": sum(float(row.amount) for row in rows if row.amount is not None),
        "rows": [row.model_dump(mode="json") for row in rows],
    }
