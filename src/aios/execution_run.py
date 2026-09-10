"""Execution Run Lifecycle & Recovery (P0 correctness).

Scope: make the **existing** ``DelegatedRun`` a durable, restart-safe execution
record. Nothing here introduces a new Runtime / Execution / Worker entity, a
``runtime_id``, or a second execution state machine — ``DelegatedRun`` and
``DelegatedRunStatus`` remain the single source of truth for one delegated
execution.

Two problems are solved, and they are deliberately kept distinct:

**Lease** — a durable, fenced ownership token on a run. It answers "which
process is currently allowed to mutate this run?" without any new entity: the
owner is an opaque per-process string (``new_id("lease")``), never an
``agent_id`` / ``employee_id`` / runtime identity.

**Recovery** — a fail-closed startup scan that reclaims *stranded* runs
(``SUBMITTED`` / ``RUNNING`` with no active lease, past a safety grace period)
so AIOS never leaves a permanent zombie execution record behind.

Recovery is NOT resume
----------------------
Recovery fixes **AIOS's own stranded record**. It never assumes the remote
agent/harness can be safely resumed: there is no universal cross-harness
remote-run inspection protocol, so a run with a ``remote_run_id`` is expired
(FAILED/EXPIRED terminal) rather than blindly retried. Blind retry would risk
`remote execution A still running + AIOS starts execution B = duplicate
execution`. Universal remote resume is explicitly out of scope.

Concurrency
-----------
Every state change is a single conditional ``UPDATE ... WHERE`` (compare-and-
set), never ``SELECT -> if -> UPDATE``. The caller inspects ``rowcount``: 1
means this process won the transition, 0 means someone else did (or the
precondition no longer holds). This holds across processes and restarts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, update
from sqlmodel import Session, select

from aios.audit import AuditEvent, append_audit
from aios.models import (
    DelegatedRun,
    DelegatedRunStatus,
    DelegationMode,
    new_id,
    now_utc,
)

# --- Lease -----------------------------------------------------------------

# Default lifetime of one lease. The executing process must renew before this
# elapses (the polling loop renews on every iteration). Generous enough to
# survive a slow poll, short enough that a crashed process releases quickly.
RUN_LEASE_TTL_SECONDS: float = 60.0

# --- Recovery --------------------------------------------------------------

# Safety margin: a run only becomes recoverable once it is BOTH unleased AND
# older than this. It prevents the scan from reclaiming a run that a live
# process is about to touch (e.g. a lease write that has not committed yet).
RECOVERY_GRACE_SECONDS: float = 300.0

# Hard bound on how many runs one recovery pass may reclaim, so startup work
# stays O(limit) no matter how large the backlog is.
RECOVERY_SCAN_LIMIT: int = 200

# Persisted on ``DelegatedRun.error`` when recovery reclaims a run. Prefixed so
# operators can distinguish "we lost track of this" from a remote failure.
STRANDED_RECOVERY_ERROR = "recovered:stranded_without_active_lease"

# Statuses a recovery scan may reclaim (non-terminal "in flight" states).
RECOVERABLE_RUN_STATUSES: tuple[DelegatedRunStatus, ...] = (
    DelegatedRunStatus.SUBMITTED,
    DelegatedRunStatus.RUNNING,
)

# Statuses that must never be overwritten by ``complete_run``: once a run has
# reached a settled outcome it is immutable. ``EXPIRED`` is deliberately NOT in
# this set -- the orchestrator relabels its own "lost track" EXPIRED into
# FAILED for the attempt, which is an existing, tested behaviour.
SETTLED_RUN_STATUSES: tuple[DelegatedRunStatus, ...] = (
    DelegatedRunStatus.SUCCEEDED,
    DelegatedRunStatus.FAILED,
    DelegatedRunStatus.CANCELLED,
)

TERMINAL_RUN_STATUSES: tuple[DelegatedRunStatus, ...] = SETTLED_RUN_STATUSES + (
    DelegatedRunStatus.EXPIRED,
)


def _naive_utc(value: datetime) -> datetime:
    """Strip tzinfo for storage/comparison (SQLite round-trips naive).

    ``now_utc()`` is timezone-aware; SQLite stores and returns naive datetimes.
    Comparing an aware value against a naive column value raises ``TypeError``,
    so every lease/recovery timestamp in this module normalises through this
    helper. Deliberately local (not a change to a shared helper) to keep the
    blast radius inside run lifecycle, mirroring the ``employee_bridge`` and
    ``scheduler`` precedent.
    """
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def new_run_lease_owner() -> str:
    """Return a fresh opaque execution-worker identity for lease fencing.

    This is NOT an ``agent_id``, ``employee_id`` or any runtime entity -- it is
    a per-process token that exists solely so a stale owner cannot mutate a run
    it no longer owns.
    """
    return new_id("lease")


def _lease_is_free(now_naive: datetime) -> Any:
    """SQL predicate: the run currently has no *valid* lease holder.

    True when the lease was never taken, has no expiry, or has expired. A row
    with an owner but a NULL expiry is treated as unowned (inconsistent state
    must fail open toward being reclaimable, never toward a permanent zombie).
    """
    return or_(
        DelegatedRun.lease_owner.is_(None),
        DelegatedRun.lease_expires_at.is_(None),
        DelegatedRun.lease_expires_at <= now_naive,
    )


def lease_is_active(run: DelegatedRun, *, now: datetime | None = None) -> bool:
    """Whether a run currently has a *valid* lease. Computed, never persisted.

    This is the read-side counterpart of the ``_lease_is_free`` predicate and is
    what the query surface reports as ``lease.active``.
    """
    now_naive = _naive_utc(now or now_utc())
    return (
        run.lease_owner is not None
        and run.lease_expires_at is not None
        and _naive_utc(run.lease_expires_at) > now_naive
    )


def _lease_is_held_by(owner: str, now_naive: datetime) -> Any:
    """SQL predicate: ``owner`` currently holds a *valid* lease (the fence)."""
    return (
        (DelegatedRun.lease_owner == owner)
        & DelegatedRun.lease_expires_at.is_not(None)
        & (DelegatedRun.lease_expires_at > now_naive)
    )


# --- Lease operations ------------------------------------------------------
# Each is a single conditional UPDATE and COMMITs immediately: a lease that is
# not durable across transactions cannot fence anything.


def acquire_run_lease(
    session: Session,
    *,
    run_id: str,
    owner: str,
    ttl_seconds: float | None = None,
    now: datetime | None = None,
) -> bool:
    """Take ownership of an unleased (or lease-expired) run. Atomic.

    Returns True only if this process won the lease. Fails for a run currently
    held by *anyone* (including ``owner`` -- use ``renew_run_lease`` to extend
    a lease you already hold).
    """
    now_naive = _naive_utc(now or now_utc())
    ttl = RUN_LEASE_TTL_SECONDS if ttl_seconds is None else float(ttl_seconds)
    stmt = (
        update(DelegatedRun)
        .where(DelegatedRun.id == run_id)
        .where(_lease_is_free(now_naive))
        .values(
            lease_owner=owner,
            lease_expires_at=now_naive + timedelta(seconds=ttl),
        )
    )
    result = session.execute(stmt)
    session.commit()
    return result.rowcount == 1


def renew_run_lease(
    session: Session,
    *,
    run_id: str,
    owner: str,
    ttl_seconds: float | None = None,
    now: datetime | None = None,
) -> bool:
    """Extend a lease the caller already holds. Atomic.

    Returns False when the lease expired (or was taken by someone else) -- the
    caller must then fail closed and stop touching the run.
    """
    now_naive = _naive_utc(now or now_utc())
    ttl = RUN_LEASE_TTL_SECONDS if ttl_seconds is None else float(ttl_seconds)
    stmt = (
        update(DelegatedRun)
        .where(DelegatedRun.id == run_id)
        .where(_lease_is_held_by(owner, now_naive))
        .values(lease_expires_at=now_naive + timedelta(seconds=ttl))
    )
    result = session.execute(stmt)
    session.commit()
    return result.rowcount == 1


def release_run_lease(
    session: Session,
    *,
    run_id: str,
    owner: str,
) -> bool:
    """Give up ownership (used after a run reaches a terminal state).

    Owner-scoped: an owner whose lease was already taken writes nothing.
    """
    stmt = (
        update(DelegatedRun)
        .where(DelegatedRun.id == run_id)
        .where(DelegatedRun.lease_owner == owner)
        .values(lease_owner=None, lease_expires_at=None)
    )
    result = session.execute(stmt)
    session.commit()
    return result.rowcount == 1


def update_run_if_owned(
    session: Session,
    *,
    run_id: str,
    owner: str,
    values: dict[str, Any],
    now: datetime | None = None,
) -> bool:
    """Fenced non-terminal field update (remote ids, cost, usage, ...).

    Only the current valid lease holder may write. Returns False when the fence
    rejects the write, so callers never mutate a run they lost.
    """
    now_naive = _naive_utc(now or now_utc())
    stmt = (
        update(DelegatedRun)
        .where(DelegatedRun.id == run_id)
        .where(_lease_is_held_by(owner, now_naive))
        .values(**values)
    )
    result = session.execute(stmt)
    session.commit()
    return result.rowcount == 1


def complete_run(
    session: Session,
    *,
    run_id: str,
    owner: str,
    status: DelegatedRunStatus,
    error: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Fenced terminal transition (SUCCEEDED / FAILED / EXPIRED / CANCELLED).

    The fence is ``lease_owner == owner AND lease_expires_at > now`` -- a stale
    owner can never complete a run whose lease it lost -- plus a guard that a
    settled run (SUCCEEDED / FAILED / CANCELLED) is immutable. ``EXPIRED`` may
    still be relabelled to ``FAILED`` by its own owner, preserving the existing
    orchestrator behaviour for a timed-out attempt.

    The lease is intentionally NOT cleared here: the caller releases it
    explicitly once it is done with the run (``release_run_lease``).
    """
    now_naive = _naive_utc(now or now_utc())
    values: dict[str, Any] = {
        "status": status,
        "finished_at": now_naive,
    }
    if error is not None:
        values["error"] = error
    stmt = (
        update(DelegatedRun)
        .where(DelegatedRun.id == run_id)
        .where(_lease_is_held_by(owner, now_naive))
        .where(DelegatedRun.status.notin_(SETTLED_RUN_STATUSES))
        .values(**values)
    )
    result = session.execute(stmt)
    if result.rowcount != 1:
        session.commit()
        return False
    # Unified budget accrual participates in the same transaction as the
    # terminal transition. ``accrue_run_budget`` is the SOLE writer of
    # ``Project.budget_used`` (governance invariant); imported lazily to avoid a
    # load-time cycle (delegation imports execution_run). A terminal, chargeable
    # run is charged exactly once -- SUCCEEDED, FAILED (paid), or EXPIRED.
    from aios.delegation import accrue_run_budget

    accrue_run_budget(session, run_id=run_id, now=now_naive)
    session.commit()
    return True


# --- Local (synchronous LLM) attempts ---------------------------------------

# The department ``LLMExecutionAdapter`` runs the model synchronously in-process.
# For unified accounting (C: Unified Attempt + Usage + Budget Accrual) every
# local attempt records the SAME ``DelegatedRun`` entity a remote delegation
# does -- there is no separate ``LocalRun`` / ``RemoteRun`` split. The only
# differences are ``delegation_mode = LOCAL`` and ``agent_id = None`` (no
# department agent is resolved for an in-process call). Local runs carry no
# execution lease: the calling process is the sole writer while it is alive, and
# if it dies mid-call the run is left SUBMITTED with no lease, so recovery
# reclaims it to EXPIRED (fail-closed) -- exactly like a stranded remote run.


def create_local_run(
    session: Session,
    *,
    task_id: str,
    project_id: str,
    attempt: int,
    idempotency_key: str,
) -> DelegatedRun:
    """Persist one local LLM attempt as a ``DelegatedRun`` (``delegation_mode=LOCAL``).

    No lease is taken: a local run is single-process and terminalizes before the
    call returns. Returns the persisted row.
    """
    run = DelegatedRun(
        project_id=project_id,
        task_id=task_id,
        agent_id=None,
        delegation_mode=DelegationMode.LOCAL,
        attempt=attempt,
        idempotency_key=idempotency_key,
        status=DelegatedRunStatus.SUBMITTED,
        lease_owner=None,
        lease_expires_at=None,
    )
    session.add(run)
    session.commit()
    return run


def complete_local_run(
    session: Session,
    *,
    run_id: str,
    status: DelegatedRunStatus,
    error: str | None = None,
    usage: dict[str, Any] | None = None,
    cost: float = 0.0,
    now: datetime | None = None,
) -> bool:
    """Terminalize a local ``DelegatedRun`` and accrue budget exactly once.

    A plain id-only fence (the local process is the sole owner) moves the run to
    its terminal state and records normalized usage / real cost. Budget accrual
    then flows through the SAME ``accrue_run_budget`` path as every other
    terminal run (SUCCEEDED / FAILED / EXPIRED), so a *failed-but-paid* local
    attempt is charged exactly like a remote one. ``cost`` is the real provider
    cost when known; a missing/zero cost is never fabricated into a charge.

    COST BOUNDARY (GAP-3 Stage 1): today the only caller
    (``execution.LLMExecutionAdapter._finish_local_run``) passes ``usage`` and
    NO cost, so ``cost`` stays at its 0.0 default and this run is counted in
    the metering ``no_measured_cost_run_count`` bucket -- LOCAL spend is
    *visible but not governed*, i.e. outside ``Project.budget_used`` scope.
    This is a missing price source, not a missing mechanism: as soon as a
    price table exists (Stage 2) passing a derived ``cost`` here is enough,
    because the accrual path is already shared. See
    ``docs/Budget_Cost_Boundary.md``.

    Returns True if the run was terminalized by this call.
    """
    now_naive = _naive_utc(now or now_utc())
    values: dict[str, Any] = {
        "status": status,
        "finished_at": now_naive,
    }
    if error is not None:
        values["error"] = error
    if usage is not None:
        values["usage"] = usage
    if cost:
        values["cost"] = cost
    stmt = (
        update(DelegatedRun)
        .where(DelegatedRun.id == run_id)
        .where(DelegatedRun.status.notin_(SETTLED_RUN_STATUSES))
        .values(**values)
    )
    result = session.execute(stmt)
    if result.rowcount != 1:
        session.commit()
        return False
    from aios.delegation import accrue_run_budget

    accrue_run_budget(session, run_id=run_id, now=now_naive)
    session.commit()
    return True


# --- Recovery --------------------------------------------------------------


@dataclass
class RecoveredRun:
    """One run reclaimed by the recovery scan."""

    run_id: str
    task_id: str
    previous_status: str
    resulting_status: str
    reason: str
    previous_lease_owner: str | None
    remote_run_id: str | None


@dataclass
class RecoveryReport:
    """Outcome of one recovery pass."""

    scanned: int = 0
    recovered: list[RecoveredRun] = field(default_factory=list)
    owner: str = ""

    @property
    def recovered_count(self) -> int:
        return len(self.recovered)

    def as_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "scanned": self.scanned,
            "recovered_count": self.recovered_count,
            "recovered": [
                {
                    "run_id": item.run_id,
                    "task_id": item.task_id,
                    "previous_status": item.previous_status,
                    "resulting_status": item.resulting_status,
                    "reason": item.reason,
                    "previous_lease_owner": item.previous_lease_owner,
                    "remote_run_id": item.remote_run_id,
                }
                for item in self.recovered
            ],
        }


def find_stranded_runs(
    session: Session,
    *,
    grace_seconds: float | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[DelegatedRun]:
    """Return non-terminal runs that are unleased and past the safety grace."""
    now_naive = _naive_utc(now or now_utc())
    grace = RECOVERY_GRACE_SECONDS if grace_seconds is None else float(grace_seconds)
    cutoff = now_naive - timedelta(seconds=grace)
    scan_limit = RECOVERY_SCAN_LIMIT if limit is None else int(limit)
    stmt = (
        select(DelegatedRun)
        .where(DelegatedRun.status.in_(RECOVERABLE_RUN_STATUSES))
        .where(DelegatedRun.submitted_at <= cutoff)
        .where(_lease_is_free(now_naive))
        .order_by(DelegatedRun.submitted_at)
        .limit(scan_limit)
    )
    return list(session.exec(stmt))


def _claim_and_expire(
    session: Session,
    *,
    run_id: str,
    owner: str,
    now: datetime | None = None,
) -> bool:
    """Atomically claim one stranded run and move it to ``EXPIRED``.

    This single conditional UPDATE is the mutual-exclusion point: when several
    recovery workers scan the same run, exactly one gets ``rowcount == 1``.
    The run is expired (terminal) rather than retried -- see the module
    docstring on why recovery never performs a blind remote retry.
    """
    now_naive = _naive_utc(now or now_utc())
    stmt = (
        update(DelegatedRun)
        .where(DelegatedRun.id == run_id)
        .where(DelegatedRun.status.in_(RECOVERABLE_RUN_STATUSES))
        .where(_lease_is_free(now_naive))
        .values(
            status=DelegatedRunStatus.EXPIRED,
            error=STRANDED_RECOVERY_ERROR,
            finished_at=now_naive,
            lease_owner=None,
            lease_expires_at=None,
        )
    )
    result = session.execute(stmt)
    return result.rowcount == 1


def recover_stranded_runs(
    session: Session,
    *,
    owner: str | None = None,
    grace_seconds: float | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> RecoveryReport:
    """Reclaim stranded delegated runs. Idempotent and safe to run repeatedly.

    A run is stranded when it is non-terminal (SUBMITTED / RUNNING), has no
    *valid* lease, and is older than the grace period. Each reclaimed run is
    moved to ``EXPIRED`` by a single compare-and-set write and gets an audit
    record; runs that another worker claimed first are silently skipped.

    This never inspects or resumes the remote execution.
    """
    recovery_owner = owner or new_run_lease_owner()
    report = RecoveryReport(owner=recovery_owner)
    candidates = find_stranded_runs(
        session, grace_seconds=grace_seconds, now=now, limit=limit
    )
    report.scanned = len(candidates)
    for run in candidates:
        previous_status = (
            run.status.value
            if isinstance(run.status, DelegatedRunStatus)
            else str(run.status)
        )
        previous_owner = run.lease_owner
        remote_run_id = run.remote_run_id
        if not _claim_and_expire(session, run_id=run.id, owner=recovery_owner, now=now):
            continue  # another recovery worker won this run
        # A reclaimed EXPIRED run participates in the same unified accrual path
        # as every other terminal run: if it carried a (paid) cost before
        # stranding, it is charged exactly once here (idle for cost == 0).
        from aios.delegation import accrue_run_budget

        accrue_run_budget(session, run_id=run.id, now=now)
        # Governance-sensitive: the audit carries the transition, the reason and
        # both owner identities. Only opaque, non-secret fields are included --
        # never secret_ref, context_ref, endpoint or credential material.
        append_audit(
            session,
            actor=recovery_owner,
            action=AuditEvent.DELEGATION_RUN_RECOVERED,
            resource_type="delegated_run",
            resource_id=run.id,
            project_id=run.project_id,
            task_id=run.task_id,
            before={
                "status": previous_status,
                "lease_owner": previous_owner,
                "remote_run_id": remote_run_id,
                "recovery_reason": STRANDED_RECOVERY_ERROR,
            },
            after={
                "status": DelegatedRunStatus.EXPIRED.value,
                "recovery_owner": recovery_owner,
                "recovered_at": _naive_utc(now or now_utc()).isoformat(),
                "remote_run_id": remote_run_id,
            },
            idempotency_key=f"audit:recovery:{run.id}:{new_id('k')}",
        )
        session.commit()
        report.recovered.append(
            RecoveredRun(
                run_id=run.id,
                task_id=run.task_id,
                previous_status=previous_status,
                resulting_status=DelegatedRunStatus.EXPIRED.value,
                reason=STRANDED_RECOVERY_ERROR,
                previous_lease_owner=previous_owner,
                remote_run_id=remote_run_id,
            )
        )
    return report


def recover_stranded_runs_at_startup() -> RecoveryReport:
    """Startup entry point: open a session and run one recovery pass.

    Called from the FastAPI ``lifespan`` hook so a restart never leaves zombie
    execution records behind. It is a one-shot scan at boot -- deliberately NOT
    a daemon, scheduler or background thread.
    """
    from aios.db import get_database_url, get_engine

    with Session(get_engine(get_database_url())) as session:
        return recover_stranded_runs(session)
