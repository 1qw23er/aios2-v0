"""Task Execution Lease & Stranded Recovery (Execution Run Lifecycle P0-B).

This closes the second half of the gap ``aios.execution_run`` closed for
``DelegatedRun``: ``Task.status == RUNNING`` is a **dead end**. It is written in
exactly one place (``aios.execution.execute_task``) and cleared only by
``complete_task`` (DONE) or ``_mark_failed`` (FAILED). If the process dies in
between -- crash, deploy, eviction -- the row stays RUNNING forever, and
``execute_task`` then rejects it with 409 forever ("仅 READY（或可恢复的 FAILED）
任务可被部门执行"). Recovery turns that permanent zombie back into a retryable
FAILED task.

Two guarantees, deliberately kept distinct:

**Claim (at-most-once)** -- ``claim_task_for_execution`` is a single conditional
``UPDATE ... WHERE status = 'ready'``. Exactly one caller flips READY -> RUNNING
and takes the lease; every other caller gets ``rowcount == 0`` and is rejected
with 409 instead of running the adapter a second time (duplicate paid model calls
and duplicate artifacts).

**Recovery (fail-closed)** -- a startup scan reclaims RUNNING tasks whose lease
is gone or expired and that are past a safety grace period, moving them to
FAILED (the existing *retryable* terminal) with an audit record + event that
explain why.

Recovery is NOT resume
----------------------
Recovery fixes **AIOS's own lost record**. It never assumes the in-flight model
call can be safely resumed: there is no universal cross-harness execution
inspection protocol, so an unknown execution is *failed* (and therefore
explicitly retryable by the owner), never blindly resumed. Blind resume would
risk "old execution A still running + AIOS starts execution B".

Why FAILED and not a new status
-------------------------------
``TaskStatus`` has no EXPIRED member, and adding one would fork the state machine
consumed by review / services / orchestrator / distribution / employee_bridge.
FAILED is already terminal *and* retryable (``execute_task`` resets FAILED ->
READY on the next idempotency key), so it is the honest outcome: AIOS lost track
of the run and the owner decides whether to spend money retrying it. An artifact
cannot exist for a stranded task -- the artifact and the DONE transition commit
in the same transaction -- so FAILED never discards produced work. The scan
re-checks this and skips (never fails) any RUNNING task that already has one.

No lease renewal in V1
----------------------
Execution is ONE synchronous adapter call bounded by the adapter timeout; there
is no poll loop to renew inside (contrast ``execution_run.renew_run_lease``).
The TTL (default 3600s, ``AIOS_TASK_LEASE_TTL_SECONDS``) must therefore exceed
the worst-case adapter duration, and the scan only runs at startup -- or on
explicit owner request -- when no execution of this process is in flight.

Concurrency
-----------
Every state change is a single conditional ``UPDATE ... WHERE`` (compare-and-set),
never ``SELECT -> if -> UPDATE``; the caller inspects ``rowcount``. This holds
across processes and restarts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, update
from sqlmodel import Session, select

from aios.audit import AuditEvent, append_audit
from aios.models import Artifact, Task, TaskStatus, new_id, now_utc
from aios.services import append_event

# --- Lease -----------------------------------------------------------------

# Default lifetime of one execution lease. Deliberately generous: V1 execution is
# a single synchronous adapter call with no renewal point (see module docstring),
# so this must exceed the worst-case adapter duration (LLM: 3 attempts x 120s
# timeout + backoff; delegated: 300s timeout x retries).
DEFAULT_TASK_LEASE_TTL_SECONDS: float = 3600.0

# --- Recovery --------------------------------------------------------------

# Safety margin: a task only becomes recoverable once it is BOTH unleased AND
# older than this. It stops the scan from reclaiming a task a live process is
# about to touch (e.g. a lease write that has not committed yet).
TASK_RECOVERY_GRACE_SECONDS: float = 300.0

# Hard bound on how many tasks one recovery pass may reclaim, so startup work
# stays O(limit) no matter how large the backlog is.
TASK_RECOVERY_SCAN_LIMIT: int = 200

# Persisted in the recovery event/audit payload. Prefixed so operators can tell
# "AIOS lost track of this" apart from a real adapter failure.
STRANDED_TASK_RECOVERY_REASON = "recovered:stranded_running_without_active_lease"

# The only task status a recovery scan may reclaim.
RECOVERABLE_TASK_STATUS: TaskStatus = TaskStatus.RUNNING

# The status a stranded task is moved to: terminal AND retryable.
RECOVERY_RESULTING_STATUS: TaskStatus = TaskStatus.FAILED


def _naive_utc(value: datetime) -> datetime:
    """Strip tzinfo for storage/comparison (SQLite round-trips naive).

    ``now_utc()`` is timezone-aware; SQLite stores and returns naive datetimes.
    Comparing an aware value against a naive column value raises ``TypeError``,
    so every lease/recovery timestamp in this module normalises through this
    helper. Deliberately local (not a change to a shared helper) to keep the
    blast radius inside task lifecycle, mirroring the ``execution_run``,
    ``employee_bridge`` and ``scheduler`` precedent.
    """
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def task_lease_ttl_seconds() -> float:
    """Lease TTL, env-tunable (``AIOS_TASK_LEASE_TTL_SECONDS``).

    Invalid or non-positive values fall back to the default: a hostile/zero TTL
    must never make a live execution look reclaimable.
    """
    raw = os.getenv("AIOS_TASK_LEASE_TTL_SECONDS")
    if raw is None:
        return DEFAULT_TASK_LEASE_TTL_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TASK_LEASE_TTL_SECONDS
    return value if value > 0 else DEFAULT_TASK_LEASE_TTL_SECONDS


def new_task_lease_owner() -> str:
    """Return a fresh opaque execution-owner identity for lease fencing.

    This is NOT an ``agent_id``, ``employee_id`` or any runtime entity -- it is a
    per-execution token that exists so a stale owner cannot hold a task hostage
    and so the audit trail can name the process that lost it.
    """
    return new_id("lease")


def _lease_is_free(now_naive: datetime) -> Any:
    """SQL predicate: the task currently has no *valid* lease holder.

    True when the lease was never taken, has no expiry, or has expired. A row
    with an owner but a NULL expiry is treated as unowned (inconsistent state
    must fail open toward being reclaimable, never toward a permanent zombie).
    """
    return or_(
        Task.lease_owner.is_(None),
        Task.lease_expires_at.is_(None),
        Task.lease_expires_at <= now_naive,
    )


def task_lease_is_active(task: Task, *, now: datetime | None = None) -> bool:
    """Whether a task currently has a *valid* lease. Computed, never persisted."""
    now_naive = _naive_utc(now or now_utc())
    return (
        task.lease_owner is not None
        and task.lease_expires_at is not None
        and _naive_utc(task.lease_expires_at) > now_naive
    )


# --- Claim / release -------------------------------------------------------
# Each is a single conditional UPDATE that COMMITs immediately: a lease that is
# not durable across transactions cannot fence anything.


def claim_task_for_execution(
    session: Session,
    *,
    task_id: str,
    owner: str,
    ttl_seconds: float | None = None,
    now: datetime | None = None,
) -> bool:
    """Atomically flip READY -> RUNNING and take the execution lease.

    Returns True only if this caller won the transition. Everyone else (a
    concurrent request, or a second process) gets False and must NOT run the
    adapter -- this is the at-most-once guarantee.
    """
    now_naive = _naive_utc(now or now_utc())
    ttl = task_lease_ttl_seconds() if ttl_seconds is None else float(ttl_seconds)
    stmt = (
        update(Task)
        .where(Task.id == task_id)
        .where(Task.status == TaskStatus.READY)
        .values(
            status=TaskStatus.RUNNING,
            lease_owner=owner,
            lease_expires_at=now_naive + timedelta(seconds=ttl),
            updated_at=now_naive,
        )
    )
    result = session.execute(stmt)
    session.commit()
    return result.rowcount == 1


def release_task_lease(
    session: Session,
    *,
    task_id: str,
    owner: str,
) -> bool:
    """Give up ownership once the task reached a terminal state.

    Owner-scoped: an owner whose lease was already taken (or reclaimed by
    recovery) writes nothing.
    """
    stmt = (
        update(Task)
        .where(Task.id == task_id)
        .where(Task.lease_owner == owner)
        .values(lease_owner=None, lease_expires_at=None)
    )
    result = session.execute(stmt)
    session.commit()
    return result.rowcount == 1


# --- Recovery --------------------------------------------------------------


@dataclass
class RecoveredTask:
    """One task reclaimed by the recovery scan."""

    task_id: str
    project_id: str | None
    previous_status: str
    resulting_status: str
    reason: str
    previous_lease_owner: str | None


@dataclass
class TaskRecoveryReport:
    """Outcome of one recovery pass."""

    scanned: int = 0
    recovered: list[RecoveredTask] = field(default_factory=list)
    skipped_with_artifact: list[str] = field(default_factory=list)
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
                    "task_id": item.task_id,
                    "project_id": item.project_id,
                    "previous_status": item.previous_status,
                    "resulting_status": item.resulting_status,
                    "reason": item.reason,
                    "previous_lease_owner": item.previous_lease_owner,
                }
                for item in self.recovered
            ],
            "skipped_with_artifact": list(self.skipped_with_artifact),
        }


def find_stranded_tasks(
    session: Session,
    *,
    grace_seconds: float | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[Task]:
    """Return RUNNING tasks that are unleased and past the safety grace."""
    now_naive = _naive_utc(now or now_utc())
    grace = TASK_RECOVERY_GRACE_SECONDS if grace_seconds is None else float(grace_seconds)
    cutoff = now_naive - timedelta(seconds=grace)
    scan_limit = TASK_RECOVERY_SCAN_LIMIT if limit is None else int(limit)
    stmt = (
        select(Task)
        .where(Task.status == RECOVERABLE_TASK_STATUS)
        .where(Task.updated_at <= cutoff)
        .where(_lease_is_free(now_naive))
        .order_by(Task.updated_at)
        .limit(scan_limit)
    )
    return list(session.exec(stmt))


def _claim_and_fail(
    session: Session,
    *,
    task_id: str,
    now_naive: datetime,
) -> bool:
    """Atomically claim one stranded task and move it to FAILED.

    This single conditional UPDATE is the mutual-exclusion point: when several
    recovery workers scan the same task, exactly one gets ``rowcount == 1``.
    The lease is cleared in the same write, so the task is immediately
    claimable again by a subsequent (owner-approved) execution.
    """
    stmt = (
        update(Task)
        .where(Task.id == task_id)
        .where(Task.status == RECOVERABLE_TASK_STATUS)
        .where(_lease_is_free(now_naive))
        .values(
            status=RECOVERY_RESULTING_STATUS,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=now_naive,
        )
    )
    result = session.execute(stmt)
    return result.rowcount == 1


def _task_has_artifact(session: Session, *, task_id: str) -> bool:
    """Safety guard: never fail a task that already produced an artifact.

    Under normal operation this cannot happen (artifact + DONE commit together),
    but the scan must fail *closed* toward "do nothing and report" if some other
    path ever produced output for a RUNNING task.
    """
    return (
        session.exec(select(Artifact.id).where(Artifact.task_id == task_id).limit(1)).first()
        is not None
    )


def recover_stranded_tasks(
    session: Session,
    *,
    owner: str | None = None,
    grace_seconds: float | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> TaskRecoveryReport:
    """Reclaim tasks stranded in RUNNING. Idempotent, safe to run repeatedly.

    A task is stranded when it is RUNNING, has no *valid* lease, and is older
    than the grace period. Each reclaimed task is moved to FAILED (terminal and
    retryable) by a single compare-and-set write and gets an event + audit
    record; tasks another worker claimed first are silently skipped, and tasks
    that already have an artifact are never touched.

    This never resumes or retries an execution whose outcome is unknown.
    """
    recovery_owner = owner or new_task_lease_owner()
    now_naive = _naive_utc(now or now_utc())
    report = TaskRecoveryReport(owner=recovery_owner)
    candidates = find_stranded_tasks(
        session, grace_seconds=grace_seconds, now=now_naive, limit=limit
    )
    report.scanned = len(candidates)
    for task in candidates:
        previous_status = task.status.value if isinstance(task.status, TaskStatus) else str(
            task.status
        )
        previous_owner = task.lease_owner
        if _task_has_artifact(session, task_id=task.id):
            report.skipped_with_artifact.append(task.id)
            continue
        if not _claim_and_fail(session, task_id=task.id, now_naive=now_naive):
            continue  # another recovery worker won this task
        payload = {
            "before": previous_status,
            "after": RECOVERY_RESULTING_STATUS.value,
            "reason": STRANDED_TASK_RECOVERY_REASON,
            "previous_lease_owner": previous_owner,
            "recovery_owner": recovery_owner,
        }
        append_event(
            session,
            project_id=task.project_id,
            task_id=task.id,
            event_type="task.recovered",
            idempotency_key=f"recovery:task:{task.id}:{new_id('k')}",
            payload=payload,
        )
        # Governance-sensitive: the audit carries the transition, the reason and
        # both owner identities. Only opaque, non-secret fields are included.
        append_audit(
            session,
            actor=recovery_owner,
            action=AuditEvent.TASK_RUN_RECOVERED,
            resource_type="task",
            resource_id=task.id,
            project_id=task.project_id,
            task_id=task.id,
            before={
                "status": previous_status,
                "lease_owner": previous_owner,
                "recovery_reason": STRANDED_TASK_RECOVERY_REASON,
            },
            after={
                "status": RECOVERY_RESULTING_STATUS.value,
                "recovery_owner": recovery_owner,
                "recovered_at": now_naive.isoformat(),
            },
            idempotency_key=f"audit:recovery:task:{task.id}:{new_id('k')}",
        )
        session.commit()
        report.recovered.append(
            RecoveredTask(
                task_id=task.id,
                project_id=task.project_id,
                previous_status=previous_status,
                resulting_status=RECOVERY_RESULTING_STATUS.value,
                reason=STRANDED_TASK_RECOVERY_REASON,
                previous_lease_owner=previous_owner,
            )
        )
    return report


def recover_stranded_tasks_at_startup() -> TaskRecoveryReport:
    """Startup entry point: open a session and run one recovery pass.

    Called from the FastAPI ``lifespan`` hook so a restart never leaves a task
    stuck in RUNNING. It is a one-shot scan at boot -- deliberately NOT a
    daemon, scheduler or background thread.
    """
    from aios.db import get_database_url, get_engine

    with Session(get_engine(get_database_url())) as session:
        return recover_stranded_tasks(session)