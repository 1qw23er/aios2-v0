"""W8-P1 HTTP surface of the Employee cost closure (2 endpoints, owner-only).

Companion to ``src/aios/employee_cost.py`` and
``docs/workforce/Workforce_W8P1_Cost_Closure_V1.md``. This is a composition
surface (W8 attribution x W5 evidence), NOT a bridge seam module:

* ``POST /tasks/{task_id}/cost-evidence`` -- record the ONE evidence row for a
  bridge Task (404 / 422 / 409 semantics live in the service).
* ``GET /cost-evidence?employee_id=...`` -- the Employee's aggregated ledger.

Both paths deliberately avoid the ``WORKFORCE_PREFIXES`` route namespace
(``/employee...`` etc.) policed by
``tests/test_workforce_w6_invariants.py::test_no_workforce_http_route_is_registered``
-- that closed set stays bridge-only and untouched.

Design contract (mirrors ``api/employee_bridge.py``):

* Every endpoint depends on ``authenticate_owner`` (the owner-surface
  inventory guard fails otherwise) and translates ``ServiceError`` through a
  local ``_translate`` copy (importing it from ``app`` would create an import
  cycle, because ``create_app`` registers these routes).
* Routes are built inside ``register_employee_cost_routes`` and attached FLAT,
  one by one -- never ``application.include_router`` (owner_inbox_routes
  precedent); the router receives the application as its
  ``dependency_overrides_provider`` so tests can override dependencies.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from aios.actor import ActorContext
from aios.api.security import authenticate_owner
from aios.db import get_session
from aios.employee_cost import employee_cost_summary, record_task_cost_evidence
from aios.services import ServiceError


def _translate(error: ServiceError) -> HTTPException:
    """Local copy of ``aios.api.app._translate`` (see module docstring)."""
    return HTTPException(status_code=error.status_code, detail=error.detail)


def register_employee_cost_routes(application: Any) -> None:
    """Build and flat-attach the 2 cost-closure routes (bridge precedent)."""
    router = APIRouter(
        tags=["employee-cost"],
        dependency_overrides_provider=application,
    )

    @router.post(
        "/tasks/{task_id}/cost-evidence",
        response_model=dict[str, Any],
        status_code=status.HTTP_201_CREATED,
    )
    def record_cost(
        task_id: str,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> dict[str, Any]:
        try:
            ce = record_task_cost_evidence(session, task_id=task_id, actor=actor)
        except ServiceError as error:
            raise _translate(error) from error
        return ce.model_dump(mode="json")

    @router.get("/cost-evidence", response_model=dict[str, Any])
    def cost_summary(
        employee_id: str,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> dict[str, Any]:
        try:
            return employee_cost_summary(session, employee_id)
        except ServiceError as error:
            raise _translate(error) from error

    # Flat attach: hand the finished routes to the application one by one,
    # exactly like their neighbours in app.py / employee_bridge.py.
    for route in router.routes:
        application.router.routes.append(route)
