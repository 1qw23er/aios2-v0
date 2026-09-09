"""Task Execution Lease & Recovery P0-B — HTTP surface (owner-only).

Two endpoints over the **existing** ``Task`` record. No new execution entity:
this only makes the task execution lifecycle observable and gives recovery an
explicit invocation point.

* ``POST /tasks/recover``              -- run one fail-closed recovery pass now
  (the same function the application lifespan calls at startup).
* ``GET /tasks/{task_id}/execution``   -- one task's execution state: status,
  assignment, computed lease state, timestamps.

Design contract (mirrors ``api/execution_run.py`` and ``api/employee_cost``):

* Every endpoint depends on ``authenticate_owner``.
* Routes are attached FLAT, one by one, never ``application.include_router``
  (owner_inbox_routes precedent); the router receives the application as its
  ``dependency_overrides_provider`` so tests can override dependencies.
* Route literals avoid the ``WORKFORCE_PREFIXES`` namespace (``/employee...``,
  ``/candidate``) policed by the W6/W7 route guards, and must not shadow an
  existing ``/tasks/{task_id}/...`` literal.

Secret boundary: only opaque, non-credential fields are exposed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from aios.actor import ActorContext
from aios.api.security import authenticate_owner
from aios.db import get_session
from aios.models import Task, TaskStatus
from aios.task_run import recover_stranded_tasks, task_lease_is_active


def task_execution_view(task: Task, *, now: datetime | None = None) -> dict[str, Any]:
    """Project one ``Task`` into its observable execution lifecycle state.

    ``lease.active`` is *computed*, never persisted: a lease is active only when
    an owner is set and its expiry is still in the future.
    """
    expires_at = task.lease_expires_at
    return {
        "task_id": task.id,
        "project_id": task.project_id,
        "status": task.status.value if isinstance(task.status, TaskStatus) else str(task.status),
        "assigned_agent_id": task.assigned_agent_id,
        "routing_mode": task.routing_mode.value
        if hasattr(task.routing_mode, "value")
        else str(task.routing_mode),
        "retry_count": task.retry_count,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "lease": {
            "owner": task.lease_owner,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "active": task_lease_is_active(task, now=now),
        },
    }


def register_task_run_routes(application: Any) -> None:
    """Build and flat-attach the task execution lifecycle routes."""
    router = APIRouter(
        tags=["task-run"],
        dependency_overrides_provider=application,
    )

    @router.get("/tasks/{task_id}/execution", response_model=dict[str, Any])
    def get_task_execution(
        task_id: str,
        session: Session = Depends(get_session),
        _actor: ActorContext = Depends(authenticate_owner),
    ) -> dict[str, Any]:
        task = session.get(Task, task_id)
        if task is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task 不存在")
        return task_execution_view(task)

    @router.post("/tasks/recover", response_model=dict[str, Any])
    def recover_tasks(
        session: Session = Depends(get_session),
        _actor: ActorContext = Depends(authenticate_owner),
    ) -> dict[str, Any]:
        """Run one fail-closed task recovery pass and report what it reclaimed.

        Reclaims tasks left in RUNNING with no valid lease and past the safety
        grace period, by moving them to FAILED (terminal *and* retryable). It
        NEVER resumes or retries an execution whose outcome is unknown, and it
        never touches a task that already produced an artifact.
        """
        return recover_stranded_tasks(session).as_dict()

    for route in router.routes:
        application.router.routes.append(route)