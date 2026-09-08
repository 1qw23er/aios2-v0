"""Execution Run Lifecycle & Recovery P0 — HTTP surface (owner-only).

Three endpoints over the **existing** ``DelegatedRun`` record. This module adds
no new execution entity: it only makes the run lifecycle observable and gives
recovery an explicit invocation point.

* ``GET /runs/{run_id}``          -- one run: status, attempt, remote identity,
  lease state, timestamps, failure information.
* ``GET /tasks/{task_id}/runs``   -- every run recorded for a task, oldest first.
* ``POST /runs/recover``          -- run one fail-closed recovery pass now
  (the same function the application lifespan calls at startup).

Design contract (mirrors ``api/employee_cost.py``):

* Every endpoint depends on ``authenticate_owner`` and translates
  ``ServiceError`` through a local ``_translate`` copy -- importing it from
  ``app`` would create an import cycle, because ``create_app`` registers these
  routes.
* Routes are attached FLAT, one by one, never ``application.include_router``
  (owner_inbox_routes precedent); the router receives the application as its
  ``dependency_overrides_provider`` so tests can override dependencies.
* Route literals avoid the ``WORKFORCE_PREFIXES`` namespace (``/employee...``,
  ``/candidate``) policed by the W6/W7 route guards.

Secret boundary: the view deliberately omits ``secret_ref``, ``context_ref``
and ``callback_url``. Only opaque, non-credential fields are exposed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from aios.actor import ActorContext
from aios.api.security import authenticate_owner
from aios.db import get_session
from aios.execution_run import (
    lease_is_active,
    recover_stranded_runs,
)
from aios.models import DelegatedRun
from aios.services import ServiceError


def _translate(error: ServiceError) -> HTTPException:
    """Local copy of ``aios.api.app._translate`` (see module docstring)."""
    return HTTPException(status_code=error.status_code, detail=error.detail)


def run_view(run: DelegatedRun, *, now: datetime | None = None) -> dict[str, Any]:
    """Project one ``DelegatedRun`` into its observable lifecycle state.

    ``lease.active`` is *computed*, never persisted: a lease is active only when
    an owner is set and its expiry is still in the future.
    """
    expires_at = run.lease_expires_at
    return {
        "run_id": run.id,
        "project_id": run.project_id,
        "task_id": run.task_id,
        "agent_id": run.agent_id,
        "status": run.status.value if hasattr(run.status, "value") else str(run.status),
        "attempt": run.attempt,
        "idempotency_key": run.idempotency_key,
        "remote_run_id": run.remote_run_id,
        "remote_status": run.remote_status,
        "cost": run.cost,
        "usage": run.usage,
        "error": run.error,
        "submitted_at": run.submitted_at.isoformat() if run.submitted_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "lease": {
            "owner": run.lease_owner,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "active": lease_is_active(run, now=now),
        },
    }


def register_execution_run_routes(application: Any) -> None:
    """Build and flat-attach the 3 execution-run lifecycle routes."""
    router = APIRouter(
        tags=["execution-run"],
        dependency_overrides_provider=application,
    )

    @router.get("/runs/{run_id}", response_model=dict[str, Any])
    def get_run(
        run_id: str,
        session: Session = Depends(get_session),
        _actor: ActorContext = Depends(authenticate_owner),
    ) -> dict[str, Any]:
        run = session.get(DelegatedRun, run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run 不存在")
        return run_view(run)

    @router.get("/tasks/{task_id}/runs", response_model=list[dict[str, Any]])
    def list_task_runs(
        task_id: str,
        session: Session = Depends(get_session),
        _actor: ActorContext = Depends(authenticate_owner),
    ) -> list[dict[str, Any]]:
        runs = session.exec(
            select(DelegatedRun)
            .where(DelegatedRun.task_id == task_id)
            .order_by(DelegatedRun.attempt, DelegatedRun.created_at)
        ).all()
        return [run_view(run) for run in runs]

    @router.post("/runs/recover", response_model=dict[str, Any])
    def recover_runs(
        session: Session = Depends(get_session),
        _actor: ActorContext = Depends(authenticate_owner),
    ) -> dict[str, Any]:
        """Run one fail-closed recovery pass and report what it reclaimed.

        Reclaims non-terminal runs (SUBMITTED / RUNNING) that have no valid
        lease and are past the safety grace period. It NEVER resumes or retries
        a remote execution -- unknown remote state is expired, not retried.
        """
        return recover_stranded_runs(session).as_dict()

    for route in router.routes:
        application.router.routes.append(route)
