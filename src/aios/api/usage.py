"""Usage Metering P1 — the read-only HTTP surface (owner-only).

Four GET endpoints over the **existing** ``DelegatedRun`` / ``Project``
records. No new entity, no migration, no write path of any kind:

* ``GET /usage/projects/{project_id}``                            -- project projection
* ``GET /usage/agents/{agent_id}``                                -- agent projection
  (optional ``?project_id=`` boundary)
* ``GET /usage/tasks/{task_id}``                                  -- task projection
* ``GET /usage/projects/{project_id}/budget-reconciliation``      -- budget
  reconciliation (report-only; NEVER corrects ``Project.budget_used``)

Design contract (mirrors ``api/execution_run.py``):

* Every endpoint depends on ``authenticate_owner`` (single-owner system) and
  translates ``ServiceError`` through a local ``_translate`` copy -- importing
  it from ``app`` would create an import cycle.
* Routes are attached FLAT, one by one, never ``application.include_router``;
  the router receives the application as its ``dependency_overrides_provider``
  so tests can override dependencies.
* Route literals avoid the ``WORKFORCE_PREFIXES`` namespace (``/employee...``,
  ``/candidate``) policed by the W6/W7 route guards.
* Time filtering uses the already-defined ``DelegatedRun.created_at`` with a
  half-open ``[from, to)`` interval -- no new timestamp semantics.

Isolation: an unknown project / agent / task is a plain 404 (this surface is
owner-only, so there is no cross-tenant probing concern), and the agent
projection accepts an optional ``project_id`` boundary so an agent view can be
confined to one project.

Cost boundary (GAP-3): the budget figures served here govern runs with a
measured currency cost -- DELEGATED runs from the provider, plus LOCAL runs
(``DelegationMode.LOCAL``) once a model price table is configured (Stage 2,
env ``AIOS_MODEL_PRICING``). A LOCAL run whose model has no price entry stays
visible through ``run_count`` and ``no_measured_cost_run_count`` while sitting
outside ``budget_used`` -- see ``docs/Budget_Cost_Boundary.md``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session

from aios.actor import ActorContext
from aios.api.security import authenticate_owner
from aios.db import get_session
from aios.models import Agent, Project, Task
from aios.services import ServiceError
from aios.usage_metering import agent_usage, budget_reconciliation, project_usage, task_usage


def _translate(error: ServiceError) -> HTTPException:
    """Local copy of ``aios.api.app._translate`` (see module docstring)."""
    return HTTPException(status_code=error.status_code, detail=error.detail)


def _time_range(
    from_ts: datetime | None, to_ts: datetime | None
) -> tuple[datetime | None, datetime | None]:
    """Validate the optional ``[from, to)`` window."""
    if from_ts is not None and to_ts is not None and from_ts >= to_ts:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="from must be earlier than to",
        )
    return from_ts, to_ts


def register_usage_routes(application: Any) -> None:
    """Build and flat-attach the 4 read-only usage-metering routes."""
    router = APIRouter(
        tags=["usage"],
        dependency_overrides_provider=application,
    )

    @router.get("/usage/projects/{project_id}", response_model=dict[str, Any])
    def get_project_usage(
        project_id: str,
        from_ts: datetime | None = Query(default=None, alias="from"),
        to_ts: datetime | None = Query(default=None, alias="to"),
        session: Session = Depends(get_session),
        _actor: ActorContext = Depends(authenticate_owner),
    ) -> dict[str, Any]:
        _from, _to = _time_range(from_ts, to_ts)
        if session.get(Project, project_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="project 不存在"
            )
        return project_usage(session, project_id, from_ts=_from, to_ts=_to)

    @router.get(
        "/usage/projects/{project_id}/budget-reconciliation",
        response_model=dict[str, Any],
    )
    def get_budget_reconciliation(
        project_id: str,
        from_ts: datetime | None = Query(default=None, alias="from"),
        to_ts: datetime | None = Query(default=None, alias="to"),
        session: Session = Depends(get_session),
        _actor: ActorContext = Depends(authenticate_owner),
    ) -> dict[str, Any]:
        _from, _to = _time_range(from_ts, to_ts)
        if session.get(Project, project_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="project 不存在"
            )
        return budget_reconciliation(session, project_id, from_ts=_from, to_ts=_to)

    @router.get("/usage/agents/{agent_id}", response_model=dict[str, Any])
    def get_agent_usage(
        agent_id: str,
        project_id: str | None = Query(default=None),
        from_ts: datetime | None = Query(default=None, alias="from"),
        to_ts: datetime | None = Query(default=None, alias="to"),
        session: Session = Depends(get_session),
        _actor: ActorContext = Depends(authenticate_owner),
    ) -> dict[str, Any]:
        _from, _to = _time_range(from_ts, to_ts)
        if session.get(Agent, agent_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="agent 不存在"
            )
        if project_id is not None and session.get(Project, project_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="project 不存在"
            )
        return agent_usage(
            session, agent_id, project_id=project_id, from_ts=_from, to_ts=_to
        )

    @router.get("/usage/tasks/{task_id}", response_model=dict[str, Any])
    def get_task_usage(
        task_id: str,
        from_ts: datetime | None = Query(default=None, alias="from"),
        to_ts: datetime | None = Query(default=None, alias="to"),
        session: Session = Depends(get_session),
        _actor: ActorContext = Depends(authenticate_owner),
    ) -> dict[str, Any]:
        _from, _to = _time_range(from_ts, to_ts)
        if session.get(Task, task_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="task 不存在"
            )
        return task_usage(session, task_id, from_ts=_from, to_ts=_to)

    for route in router.routes:
        application.router.routes.append(route)
