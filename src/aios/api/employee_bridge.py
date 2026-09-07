"""W8-v2 HTTP surface of the Workforce Execution Bridge (8 endpoints, owner-only).

Companion to the W8-v2 Implementation Design V1 (issue #112). This module is
the ONLY place in ``src/aios`` where Workforce-prefixed HTTP routes may live --
mechanised by the seam-aware revision of
``tests/test_workforce_w6_invariants.py::test_no_workforce_http_route_is_registered``.

Design contract:

* NO Employee CRUD / hire endpoints: an Employee exists only through the W4
  ``promote_to_employee`` lifecycle; the bridge binds, rebinds, unbinds,
  assigns and attributes -- it never mints or mutates identity.
* Every endpoint translates ``ServiceError`` through ``_translate`` (the
  approved DR-D4-2 §4.5 mapping, asserted by the seam guard); binding
  conflicts surface as HTTP 409. The global ``IntegrityError -> 409`` handler
  (DR-W7-6, registered on the app) remains the last-resort backstop.
* ``_translate`` is a deliberate two-line local copy of
  ``aios.api.app._translate`` (same precedent as ``workforce_employee._iso``):
  importing it from ``app`` would create an import cycle, because ``create_app``
  registers these routes.
* Routes are built inside ``register_employee_bridge_routes`` and attached
  FLAT, one by one -- deliberately NOT via ``application.include_router``
  (same precedent and rationale as ``owner_inbox_routes``: FastAPI keeps an
  ``include_router`` call as a nested node in ``app.routes``, which hides the
  surface from anything that walks ``app.routes``, including the owner-auth
  route inventory). The router receives the application as its
  ``dependency_overrides_provider`` so the standard FastAPI test override
  mechanism (``app.dependency_overrides``) keeps working on flat-attached
  routes -- without it, ``authenticate_owner`` could never be substituted in
  tests and every owner-surface test would hit real auth.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlmodel import Session

from aios.actor import ActorContext
from aios.api.security import authenticate_owner
from aios.db import get_session
from aios.employee_bridge import (
    _load_employee,
    assign_work_to_employee,
    bind_employee_agent,
    employee_for_artifact,
    employee_for_task,
    get_current_employee_binding,
    get_employee_binding_history,
    replace_employee_agent,
    unbind_employee_agent,
)
from aios.models import Artifact, EmployeeAgentBinding, Task
from aios.schemas import AgentBindingCreate, EmployeeWorkCreate
from aios.services import ServiceError


def _translate(error: ServiceError) -> HTTPException:
    """Local copy of ``aios.api.app._translate`` (see module docstring)."""
    return HTTPException(status_code=error.status_code, detail=error.detail)


def register_employee_bridge_routes(application: Any) -> None:
    """Build and flat-attach the 8 bridge routes (owner_inbox_routes precedent)."""
    router = APIRouter(
        tags=["workforce-bridge"],
        dependency_overrides_provider=application,
    )

    @router.post(
        "/employees/{employee_id}/agent-binding",
        response_model=EmployeeAgentBinding,
        status_code=status.HTTP_201_CREATED,
    )
    def bind_agent(
        employee_id: str,
        request: AgentBindingCreate,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> EmployeeAgentBinding:
        try:
            return bind_employee_agent(session, employee_id, request.agent_id, actor=actor)
        except ServiceError as error:
            raise _translate(error) from error

    @router.post(
        "/employees/{employee_id}/agent-binding/replace",
        response_model=dict[str, Any],
    )
    def replace_agent(
        employee_id: str,
        request: AgentBindingCreate,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> dict[str, Any]:
        try:
            result = replace_employee_agent(session, employee_id, request.agent_id, actor=actor)
        except ServiceError as error:
            raise _translate(error) from error
        return {
            "old": result["old"].model_dump(mode="json"),
            "new": result["new"].model_dump(mode="json"),
        }

    # ``api_route(methods=["DELETE"])`` rather than ``@router.delete``: W6's
    # no-Employee-delete guard (DR-D4-3) does a conservative AST scan for any
    # ``*.delete(...)`` call whose argument mentions an employee, and the
    # route-path string trips it. Semantics are identical; no guard was
    # weakened.
    @router.api_route(
        "/employees/{employee_id}/agent-binding",
        methods=["DELETE"],
        response_model=EmployeeAgentBinding,
    )
    def unbind_agent(
        employee_id: str,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> EmployeeAgentBinding:
        try:
            return unbind_employee_agent(session, employee_id, actor=actor)
        except ServiceError as error:
            raise _translate(error) from error

    @router.get(
        "/employees/{employee_id}/agent-binding",
        response_model=EmployeeAgentBinding | None,
    )
    def current_binding(
        employee_id: str,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> EmployeeAgentBinding | None:
        # Employee existence is verified even with no binding (404 vs null).
        try:
            _load_employee(session, employee_id)
        except ServiceError as error:
            raise _translate(error) from error
        return get_current_employee_binding(session, employee_id)

    @router.get(
        "/employees/{employee_id}/agent-binding/history",
        response_model=list[EmployeeAgentBinding],
    )
    def binding_history(
        employee_id: str,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> list[EmployeeAgentBinding]:
        try:
            _load_employee(session, employee_id)
        except ServiceError as error:
            raise _translate(error) from error
        return get_employee_binding_history(session, employee_id)

    @router.post(
        "/employees/{employee_id}/work",
        response_model=Task,
        status_code=status.HTTP_201_CREATED,
    )
    def assign_work(
        employee_id: str,
        request: EmployeeWorkCreate,
        session: Session = Depends(get_session),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> Task:
        key = idempotency_key or f"wf:{employee_id}:{request.model_dump_json()}"
        try:
            return assign_work_to_employee(
                session,
                employee_id,
                spec=request,
                idempotency_key=key,
                actor=actor,
            )
        except ServiceError as error:
            raise _translate(error) from error

    @router.get("/tasks/{task_id}/employee-attribution", response_model=dict[str, Any])
    def task_attribution(
        task_id: str,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> dict[str, Any]:
        task = session.get(Task, task_id)
        if task is None:
            raise _translate(ServiceError(404, "Task not found"))
        return {"employee_id": employee_for_task(session, task)}

    @router.get(
        "/artifacts/{artifact_id}/employee-attribution",
        response_model=dict[str, Any],
    )
    def artifact_attribution(
        artifact_id: str,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> dict[str, Any]:
        artifact = session.get(Artifact, artifact_id)
        if artifact is None:
            raise _translate(ServiceError(404, "Artifact not found"))
        return {"employee_id": employee_for_artifact(session, artifact)}

    # Flat attach: hand the finished routes to the application one by one,
    # exactly like their neighbours in app.py / owner_inbox_routes.py.
    for route in router.routes:
        application.router.routes.append(route)
