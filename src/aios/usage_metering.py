"""Usage Metering P1 — read-only cost/run projections over the existing
``DelegatedRun`` SSoT.

Design contract (Usage Metering P1 design review, verdict GO WITH CONDITIONS):

* **Pure read projection.** ``DelegatedRun`` is the run-level cost/usage SSoT
  and ``Project.budget_used`` is the budget SSoT (single writer:
  ``delegation.accrue_run_budget`` -- the W6 invariant). This module adds NO
  fourth fact source: no rollup table, no materialized view, no migration, no
  new entity. Every function here only SELECTs.
* **Cost semantics.** ``cost > 0`` -> *measured*; ``cost == 0`` (the column
  default, also what "provider did not report" looks like) -> *no measured
  cost*. The two cases are deliberately NOT distinguishable in the data
  model, so this projection never labels ``cost == 0`` as "free".
* **Accrual semantics.** Budget accrual (``delegation.accrue_run_budget``)
  charges only ACCRUABLE terminal runs (SUCCEEDED / FAILED / EXPIRED) with
  ``cost > 0``. A CANCELLED run that already recorded cost is reported
  separately (``cancelled_with_cost_*``) and NEVER folded into the accrued
  spend -- so a naive ``SUM(DelegatedRun.cost)`` can never masquerade as
  budget reconciliation.
* **Cost boundary (GAP-3 Stage 1).** ``Project.budget_used`` governs
  *delegated* runs that carry a MEASURED currency cost. LOCAL runs
  (``DelegationMode.LOCAL``) record usage but no cost, so they appear in
  ``run_count`` / ``runs_by_status`` / ``no_measured_cost_run_count`` and
  NEVER in ``measured_spend``: visible, but outside the budget scope. A
  non-zero ``no_measured_cost_run_count`` is therefore the meter for that
  scope boundary, not an anomaly. See ``docs/Budget_Cost_Boundary.md``.
* **No token aggregation.** ``usage`` schemas are heterogeneous
  (OpenAI-style / Hermes-style / LOCAL verbatim); P1 aggregates currency
  cost + run counts only and never exposes a cross-provider token SUM.
* **Employee dimension: out of scope** (attribution-window vs CostEvidence
  snapshot semantics conflict -- see design review §5.4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from aios.models import DelegatedRun, DelegatedRunStatus, Project

#: Terminal run statuses that accrue budget. Mirrors the chargeable-terminal
#: contract of ``delegation.accrue_run_budget`` (C: SUCCEEDED / FAILED /
#: EXPIRED are chargeable; CANCELLED is explicitly excluded).
ACCRUABLE_STATUSES = frozenset(
    {
        DelegatedRunStatus.SUCCEEDED,
        DelegatedRunStatus.FAILED,
        DelegatedRunStatus.EXPIRED,
    }
)

#: Every lifecycle status, for a stable ``runs_by_status`` shape.
_ALL_STATUSES = (
    DelegatedRunStatus.SUBMITTED,
    DelegatedRunStatus.RUNNING,
    DelegatedRunStatus.SUCCEEDED,
    DelegatedRunStatus.FAILED,
    DelegatedRunStatus.CANCELLED,
    DelegatedRunStatus.EXPIRED,
)

#: Reconciliation equality tolerance: ``Project.budget_used`` and the
#: metering-side accrued spend are both float accumulations of the same
#: per-run costs, so they should agree to within float noise.
TOLERANCE = 1e-6


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalise a stored/query datetime to UTC for range comparison.

    SQLite round-trips may return naive datetimes; they are UTC by project
    convention (``models.now_utc``), so a naive value is interpreted as UTC.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _in_half_open_range(
    created_at: datetime | None, from_ts: datetime | None, to_ts: datetime | None
) -> bool:
    """Half-open interval test ``[from, to)`` on ``DelegatedRun.created_at``.

    ``from`` is inclusive, ``to`` is exclusive, so adjacent windows never
    double-count a run that sits exactly on the boundary.
    """
    ts = _as_utc(created_at)
    if ts is None:
        return from_ts is None and to_ts is None
    if from_ts is not None and ts < from_ts:
        return False
    return not (to_ts is not None and ts >= to_ts)


def _select_runs(
    session: Session,
    *,
    project_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
) -> list[DelegatedRun]:
    """Read-only SELECT of matching runs. Never mutates anything."""
    stmt = select(DelegatedRun)
    if project_id is not None:
        stmt = stmt.where(DelegatedRun.project_id == project_id)
    if task_id is not None:
        stmt = stmt.where(DelegatedRun.task_id == task_id)
    if agent_id is not None:
        stmt = stmt.where(DelegatedRun.agent_id == agent_id)
    runs = list(session.exec(stmt).all())
    if from_ts is not None or to_ts is not None:
        runs = [run for run in runs if _in_half_open_range(run.created_at, from_ts, to_ts)]
    return runs


def _project_runs(runs: list[DelegatedRun]) -> dict[str, Any]:
    """Aggregate runs into the metering projection shape.

    Identity note: ``DelegatedRunStatus`` is a ``StrEnum``, so membership
    tests work whether the ORM hands back enum members or plain strings.
    """
    measured = [run for run in runs if run.cost is not None and run.cost > 0]
    accrued = [run for run in measured if run.status in ACCRUABLE_STATUSES]
    cancelled_with_cost = [
        run for run in measured if run.status == DelegatedRunStatus.CANCELLED
    ]
    runs_by_status = {
        status.value: sum(1 for run in runs if run.status == status)
        for status in _ALL_STATUSES
    }
    return {
        "run_count": len(runs),
        "measured_run_count": len(measured),
        "no_measured_cost_run_count": len(runs) - len(measured),
        "measured_spend": round(sum(run.cost for run in measured), 10),
        "accruable_run_count": sum(1 for run in runs if run.status in ACCRUABLE_STATUSES),
        "accrued_measured_spend": round(sum(run.cost for run in accrued), 10),
        "cancelled_with_cost_run_count": len(cancelled_with_cost),
        "cancelled_with_cost_spend": round(
            sum(run.cost for run in cancelled_with_cost), 10
        ),
        "runs_by_status": runs_by_status,
    }


def project_usage(
    session: Session,
    project_id: str,
    *,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
) -> dict[str, Any]:
    """Project usage for one project from its ``DelegatedRun`` rows."""
    runs = _select_runs(
        session, project_id=project_id, from_ts=from_ts, to_ts=to_ts
    )
    return _project_runs(runs)


def agent_usage(
    session: Session,
    agent_id: str,
    *,
    project_id: str | None = None,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
) -> dict[str, Any]:
    """Project usage for one agent.

    An agent spans projects by nature, so ``project_id`` is an optional
    boundary: when given, only that project's runs are aggregated.
    """
    runs = _select_runs(
        session,
        agent_id=agent_id,
        project_id=project_id,
        from_ts=from_ts,
        to_ts=to_ts,
    )
    return _project_runs(runs)


def task_usage(
    session: Session,
    task_id: str,
    *,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
) -> dict[str, Any]:
    """Project usage for one task (a task belongs to exactly one project)."""
    runs = _select_runs(session, task_id=task_id, from_ts=from_ts, to_ts=to_ts)
    return _project_runs(runs)


def budget_reconciliation(
    session: Session,
    project_id: str,
    *,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
) -> dict[str, Any]:
    """Compare ``Project.budget_used`` (READ-ONLY) with the metering-side
    accrued measured spend.

    The reconciliation identity follows the accrual contract exactly:
    ACCRUABLE terminal statuses AND ``cost > 0``. CANCELLED-with-cost runs are
    reported separately and never counted as accrued; ``cost == 0`` is never
    interpreted as free spend.

    A discrepancy is REPORTED, never corrected: this function performs no
    write of any kind.

    Scope caveat (GAP-3 Stage 1): ``matches == True`` means accrual and
    ``budget_used`` agree -- it does NOT mean every unit of real spend lies
    inside the budget. LOCAL runs carry no measured cost and are outside the
    budget scope by design; read ``no_measured_cost_run_count`` for the size
    of that blind spot.
    """
    # READ-ONLY access to the budget SSoT. The W6 invariant requires that the
    # only writer of ``Project.budget_used`` lives in delegation.py; this
    # module must never assign it.
    project = session.get(Project, project_id)
    budget_used = float(project.budget_used) if project is not None else 0.0
    projection = project_usage(session, project_id, from_ts=from_ts, to_ts=to_ts)
    accrued = projection["accrued_measured_spend"]
    discrepancy = round(budget_used - accrued, 10)
    return {
        "project_id": project_id,
        "budget_used": budget_used,
        "accrued_measured_spend": accrued,
        "discrepancy": discrepancy,
        "matches": abs(discrepancy) <= TOLERANCE,
        "projection": projection,
    }
