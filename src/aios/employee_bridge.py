"""W8-v2: the Workforce Execution Bridge -- the ONE sanctioned cross-domain seam.

Companion to the W8-v2 Implementation Design V1 (issue #112). The Workforce
recruitment domain (W1-W7, ``workforce*.py``) is frozen behind the DR-W7-5
boundary: it must never depend on the execution domain. W8-v2 legalises exactly
ONE seam, and this module (plus its thin API surface in
``aios.api.employee_bridge``) is it:

    Owner
      -> existing Employee (canonical Workforce identity, from W4 promote)
      -> EmployeeAgentBinding (current + historical binding, THIS module)
      -> Agent
      -> assign_work_to_employee()
      -> create_task()            (reused verbatim -- fingerprint idempotency,
      -> Task(FIXED)               validation, audit, TaskContext inherited)
      -> existing route_task / execution / governance
      -> Artifact
      -> Task.created_at  ->  historical Employee attribution (THIS module)

Boundary rules (mechanised in ``tests/test_workforce_w7_invariants.py``):

* ``workforce*.py`` must NOT import this module -- the recruitment domain never
  reaches the seam; the seam hangs OFF the domain, owner-facing.
* This module may import the execution / scheduler domains; it must NOT import
  any ``workforce*.py`` recruitment module.
* No second execution engine: routing, governance, idempotency, audit and
  provenance are the frozen production paths, reused verbatim.

Attribution contract (frozen):

    anchor  = Task.created_at
    query   = binding where agent_id == task.assigned_agent_id
              AND effective_from <= anchor
              AND (effective_to IS NULL OR anchor < effective_to)

``Employee.agent_id`` (the immutable promote snapshot) is NEVER consulted for
attribution -- with agent reuse the snapshot is ambiguous (two Employees may
carry the same snapshot ``agent_id``); the binding interval is the only
unambiguous historical fact.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from aios.actor import ActorContext, _assert_owner_actor
from aios.audit import append_audit
from aios.models import (
    Agent,
    Employee,
    EmployeeAgentBinding,
    EmployeeStatus,
    RoutingMode,
    Task,
    TaskStatus,
    now_utc,
)
from aios.scheduler import route_task
from aios.schemas import EmployeeWorkCreate, TaskCreate
from aios.services import ServiceError, create_task


def _naive_utc(value: datetime) -> datetime:
    """Strip tzinfo for storage/comparison (SQLite round-trips naive).

    ``now_utc()`` is timezone-aware; SQLite stores and returns naive
    datetimes. Comparing an aware value against a naive column value raises
    ``TypeError``, so every effective-dated write and comparison in this
    module normalises through this helper. A local helper (not a global
    change) keeps the blast radius inside the bridge, mirroring the W4
    precedent of freezing shared helpers.
    """
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _load_employee(session: Session, employee_id: str) -> Employee:
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise ServiceError(404, "Employee not found")
    return employee


def _load_agent(session: Session, agent_id: str) -> Agent:
    agent = session.get(Agent, agent_id)
    if agent is None:
        raise ServiceError(404, "Agent not found")
    return agent


def _check_active(employee: Employee) -> None:
    """Control-plane gate: only an ACTIVE Employee may hold/execute bindings.

    Invariant-preserving by design: ``EmployeeStatus`` is ACTIVE-only today
    (W7-I12 freeze), so this never fires -- it exists so the gate survives any
    future lifecycle widening without new W8 work. StrEnum -> VARCHAR
    round-trips as a plain ``str`` on read, hence the ``str(...)`` comparison.
    Distinct from ``Agent.enabled`` (execution availability), which is NOT
    duplicated here: a disabled agent is rejected by the existing scheduler
    gate, not by a copied check.
    """
    if str(employee.status) != EmployeeStatus.ACTIVE.value:
        raise ServiceError(409, f"employee_not_active:{employee.status}")


# ---------------------------------------------------------------------------
# Binding lifecycle
# ---------------------------------------------------------------------------


def get_current_employee_binding(session: Session, employee_id: str) -> EmployeeAgentBinding | None:
    """The Employee's current binding (``effective_to IS NULL``), or ``None``."""
    return session.exec(
        select(EmployeeAgentBinding)
        .where(EmployeeAgentBinding.employee_id == employee_id)
        .where(EmployeeAgentBinding.effective_to.is_(None))
    ).first()


def get_employee_binding_history(session: Session, employee_id: str) -> list[EmployeeAgentBinding]:
    """All binding rows for the Employee, most recent first (history is N:M)."""
    return list(
        session.exec(
            select(EmployeeAgentBinding)
            .where(EmployeeAgentBinding.employee_id == employee_id)
            .order_by(EmployeeAgentBinding.effective_from.desc())
        ).all()
    )


def _load_agent_for_write(session: Session, agent_id: str) -> Agent:
    agent = _load_agent(session, agent_id)
    occupied = session.exec(
        select(EmployeeAgentBinding)
        .where(EmployeeAgentBinding.agent_id == agent_id)
        .where(EmployeeAgentBinding.effective_to.is_(None))
    ).first()
    if occupied is not None:
        raise ServiceError(409, "agent_binding_conflict")
    return agent


def bind_employee_agent(
    session: Session,
    employee_id: str,
    agent_id: str,
    *,
    actor: ActorContext,
) -> EmployeeAgentBinding:
    """Bind ``agent_id`` as the Employee's execution agent (new current row).

    Requires the Employee to have NO current binding -- a bound Employee must
    go through :func:`replace_employee_agent`. The Employee/agent current-1:1
    pre-checks only produce friendly errors; the two partial unique indexes
    are the final authority (a racing writer is absorbed into a 409).
    """
    _assert_owner_actor(actor)
    employee = _load_employee(session, employee_id)
    _check_active(employee)
    _load_agent(session, agent_id)
    if get_current_employee_binding(session, employee_id) is not None:
        raise ServiceError(409, "employee_binding_conflict")
    _load_agent_for_write(session, agent_id)

    ts = _naive_utc(now_utc())
    binding = EmployeeAgentBinding(
        employee_id=employee_id,
        agent_id=agent_id,
        effective_from=ts,
        effective_to=None,
    )
    try:
        session.add(binding)
        session.flush()
        append_audit(
            session,
            actor=actor.owner_id,
            action="employee.agent_bound",
            resource_type="employee_agent_binding",
            resource_id=binding.id,
            project_id=None,
            task_id=None,
            before=None,
            after={
                "employee_id": employee_id,
                "agent_id": agent_id,
                "effective_from": ts.isoformat(),
            },
            idempotency_key=f"eab:{binding.id}",
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ServiceError(409, "concurrent_binding_conflict") from None
    session.refresh(binding)
    return binding


def replace_employee_agent(
    session: Session,
    employee_id: str,
    new_agent_id: str,
    *,
    actor: ActorContext,
) -> dict[str, Any]:
    """Close the current binding and open a new one -- ONE transaction.

    ``old.effective_to == new.effective_from`` (the same server timestamp), so
    the half-open ``[from, to)`` intervals tile the timeline with no gap and no
    overlap. The timestamp is server-side by contract (V1 §4.B): clients cannot
    pass ``effective_from`` or future-date a binding. Replacing to the agent
    already bound is rejected (it would be a no-op churn of history). The
    close + open + audit write is a single commit -- never
    "close-commit-then-open", which would expose a gap to racing readers.
    """
    _assert_owner_actor(actor)
    employee = _load_employee(session, employee_id)
    _check_active(employee)
    _load_agent(session, new_agent_id)
    old = get_current_employee_binding(session, employee_id)
    if old is None:
        raise ServiceError(409, "no_current_binding")
    if old.agent_id == new_agent_id:
        raise ServiceError(409, "already_bound_to_agent")
    _load_agent_for_write(session, new_agent_id)

    ts = _naive_utc(now_utc())
    if old.effective_from >= ts:  # pragma: no cover - same-instant pathology
        raise ServiceError(409, "binding_timestamp_conflict")
    new = EmployeeAgentBinding(
        employee_id=employee_id,
        agent_id=new_agent_id,
        effective_from=ts,
        effective_to=None,
    )
    try:
        old.effective_to = ts
        session.add(old)
        session.add(new)
        session.flush()
        append_audit(
            session,
            actor=actor.owner_id,
            action="employee.agent_replaced",
            resource_type="employee_agent_binding",
            resource_id=employee_id,
            project_id=None,
            task_id=None,
            before={
                "agent_id": old.agent_id,
                "effective_from": old.effective_from.isoformat(),
                "effective_to": None,
            },
            after={
                "agent_id": new_agent_id,
                "effective_from": ts.isoformat(),
                "effective_to": None,
            },
            idempotency_key=f"eab-replace:{uuid4().hex}",
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ServiceError(409, "concurrent_binding_conflict") from None
    session.refresh(old)
    session.refresh(new)
    return {"old": old, "new": new}


def unbind_employee_agent(
    session: Session,
    employee_id: str,
    *,
    actor: ActorContext,
) -> EmployeeAgentBinding:
    """Close the current binding (the Employee stops receiving bridge work).

    Idempotent in effect: with no current binding the call is a 404 (not a
    second close -- there is nothing to close twice). The Employee row itself
    is untouched (permanent, ACTIVE-only per the W7-I12 freeze); "deactivated"
    is expressed purely as "no current binding".
    """
    _assert_owner_actor(actor)
    _load_employee(session, employee_id)
    old = get_current_employee_binding(session, employee_id)
    if old is None:
        raise ServiceError(404, "no_current_binding")
    ts = _naive_utc(now_utc())
    try:
        old.effective_to = ts
        session.add(old)
        session.flush()
        append_audit(
            session,
            actor=actor.owner_id,
            action="employee.agent_unbound",
            resource_type="employee_agent_binding",
            resource_id=old.id,
            project_id=None,
            task_id=None,
            before={
                "agent_id": old.agent_id,
                "effective_from": old.effective_from.isoformat(),
                "effective_to": None,
            },
            after={
                "agent_id": old.agent_id,
                "effective_from": old.effective_from.isoformat(),
                "effective_to": ts.isoformat(),
            },
            idempotency_key=f"eab-unbind:{uuid4().hex}",
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ServiceError(409, "binding_timestamp_conflict") from None
    session.refresh(old)
    return old


# ---------------------------------------------------------------------------
# Work assignment (reuses create_task + route_task verbatim)
# ---------------------------------------------------------------------------


def assign_work_to_employee(
    session: Session,
    employee_id: str,
    spec: EmployeeWorkCreate,
    *,
    idempotency_key: str,
    actor: ActorContext,
) -> Task:
    """Route work to an Employee through their current Agent.

    Chain (no second Task-creation path, no governance bypass):

        Employee -> current binding -> Agent
          -> create_task(TaskCreate(assigned_agent_id=agent.id, FIXED), ...)
          -> route_task (existing deterministic scheduler)
          -> existing execution / governance

    ``assigned_agent_id`` is frozen at Task creation; the FIXED branch of
    ``route_task`` never re-selects an agent, so the Task's attribution anchor
    (``Task.created_at``) always resolves against the binding that was current
    when the work was assigned -- even across later rebinding.
    """
    _assert_owner_actor(actor)
    employee = _load_employee(session, employee_id)
    _check_active(employee)
    binding = get_current_employee_binding(session, employee_id)
    if binding is None:
        raise ServiceError(409, "no_current_binding")
    agent = _load_agent(session, binding.agent_id)

    request = TaskCreate(
        project_id=spec.project_id,
        title=spec.title,
        description=spec.description,
        assigned_agent_id=agent.id,
        routing_mode=RoutingMode.FIXED,
        required_capabilities=spec.required_capabilities,
        output_schema=spec.output_schema,
        acceptance_criteria=spec.acceptance_criteria,
        input_context_refs=spec.input_context_refs,
        estimated_cost=spec.estimated_cost,
    )
    # Reused verbatim: fingerprint idempotent replay, Project/Agent/capability
    # validation, task.created event + audit. ``commit=False`` keeps creation
    # and the kick-off in one transaction (campaign.launch_campaign pattern).
    task = create_task(session, request, idempotency_key, commit=False)
    # Kick-off (BACKLOG -> READY) follows the sanctioned campaign pattern: a
    # direct status write WITH its own audit record -- no new lifecycle
    # vocabulary, no governance bypass (route_task still owns routing).
    if task.status == TaskStatus.BACKLOG:
        task.status = TaskStatus.READY
        task.updated_at = now_utc()
        session.add(task)
        append_audit(
            session,
            actor=actor.owner_id,
            action="task.ready",
            resource_type="task",
            resource_id=task.id,
            project_id=task.project_id,
            task_id=task.id,
            before={"status": TaskStatus.BACKLOG.value},
            after={"status": TaskStatus.READY.value},
            idempotency_key=f"wf:{idempotency_key}:ready",
        )
    # Existing deterministic scheduler; FIXED honours the frozen agent.
    route_task(session, task.id, f"wf:{idempotency_key}:route", commit=True)
    session.refresh(task)  # route_task committed -> re-load for serialization
    return task


# ---------------------------------------------------------------------------
# Historical attribution (read-only, binding-only)
# ---------------------------------------------------------------------------


def employee_for_task(session: Session, task: Task) -> str | None:
    """The Employee a Task is attributable to, via ``Task.created_at``.

    Canonical query (frozen contract):

        effective_from <= task.created_at
        AND (effective_to IS NULL OR task.created_at < effective_to)
        AND agent_id == task.assigned_agent_id

    The interval is half-open, so a Task created at the exact instant of a
    rebind belongs to the NEW binding -- and a Task created before any rebind
    keeps its original Employee forever, no matter how often the agent moves
    afterwards. ``Employee.agent_id`` is deliberately absent from this query:
    with agent reuse the snapshot is ambiguous, the interval is not.
    """
    if task.assigned_agent_id is None:
        return None
    anchor = _naive_utc(task.created_at)
    row = session.exec(
        select(EmployeeAgentBinding)
        .where(EmployeeAgentBinding.agent_id == task.assigned_agent_id)
        .where(EmployeeAgentBinding.effective_from <= anchor)
        .where(
            or_(
                EmployeeAgentBinding.effective_to.is_(None),
                anchor < EmployeeAgentBinding.effective_to,
            )
        )
    ).first()
    return row.employee_id if row is not None else None


def employee_for_artifact(session: Session, artifact: Any) -> str | None:
    """Attribution for an Artifact, via its Task's ``created_at``.

    The Artifact's own creation time is irrelevant (a long-running Task may
    produce Artifacts long after a rebind); the Task -- whose agent was frozen
    at creation -- is the attribution subject.
    """
    task = session.get(Task, artifact.task_id)
    if task is None:
        return None
    return employee_for_task(session, task)
