from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from sqlmodel import Session, select

from aios.campaign import CampaignLaunchResult, launch_campaign
from aios.db import get_session, run_migrations
from aios.models import Approval, Project, Task, new_id
from aios.orchestrator import Orchestrator, complete_task
from aios.schemas import (
    ApprovalCreate,
    BoardRead,
    OrchestratorProcessResult,
    ProjectCreate,
    TaskCreate,
)
from aios.services import ServiceError
from aios.services import create_approval as create_approval_service
from aios.services import create_project as create_project_service
from aios.services import create_task as create_task_service
from aios.services import get_board as get_board_service


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    run_migrations()
    yield


def _translate(error: ServiceError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.detail)


def _key(value: str | None) -> str:
    return value or new_id("idem")


def create_app() -> FastAPI:
    application = FastAPI(title="AIOS V0", version="0.1.0", lifespan=lifespan)

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/projects", response_model=Project, status_code=status.HTTP_201_CREATED)
    def create_project(
        request: ProjectCreate,
        session: Session = Depends(get_session),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Project:
        try:
            return create_project_service(session, request, _key(idempotency_key))
        except ServiceError as error:
            raise _translate(error) from error

    @application.get("/projects", response_model=list[Project])
    def list_projects(session: Session = Depends(get_session)) -> list[Project]:
        return list(session.exec(select(Project)))

    @application.post("/tasks", response_model=Task, status_code=status.HTTP_201_CREATED)
    def create_task(
        request: TaskCreate,
        session: Session = Depends(get_session),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Task:
        try:
            return create_task_service(session, request, _key(idempotency_key))
        except ServiceError as error:
            raise _translate(error) from error

    @application.get("/projects/{project_id}/tasks", response_model=list[Task])
    def list_tasks(project_id: str, session: Session = Depends(get_session)) -> list[Task]:
        return list(session.exec(select(Task).where(Task.project_id == project_id)))

    @application.get("/projects/{project_id}/board", response_model=BoardRead)
    def get_board(project_id: str, session: Session = Depends(get_session)) -> dict:
        try:
            return get_board_service(session, project_id)
        except ServiceError as error:
            raise _translate(error) from error

    @application.post("/approvals", response_model=Approval, status_code=status.HTTP_201_CREATED)
    def create_approval(
        request: ApprovalCreate,
        session: Session = Depends(get_session),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Approval:
        try:
            return create_approval_service(session, request, _key(idempotency_key))
        except ServiceError as error:
            raise _translate(error) from error

    @application.post(
        "/tasks/{task_id}/complete", response_model=Task, status_code=status.HTTP_200_OK
    )
    def complete_task_endpoint(
        task_id: str,
        session: Session = Depends(get_session),
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> Task:
        """Mark a task done.

        The ``Idempotency-Key`` header is required. Repeating the same key for the
        same task is a no-op (returns the existing completion); omitting the header
        is rejected with 422. Distinct keys never collapse into one operation --
        each creates its own ``task.completed`` event.
        """
        try:
            complete_task(session, task_id, idempotency_key)
        except ServiceError as error:
            raise _translate(error) from error
        task = session.get(Task, task_id)
        return task

    @application.post(
        "/orchestrator/process",
        response_model=OrchestratorProcessResult,
        status_code=status.HTTP_200_OK,
    )
    def process_orchestrator(
        limit: int = Query(
            default=100,
            ge=1,
            le=100,
            description="Maximum number of pending completion events to process in this "
            "invocation (1..100). Out-of-range or non-integer values are rejected with 422.",
        ),
        session: Session = Depends(get_session),
    ) -> OrchestratorProcessResult:
        """Drive the orchestrator for pending completion events.

        ``activated_task_ids`` is strict and invocation-scoped. It is taken
        directly from ``Orchestrator.process_pending(return_detailed=True)``, which
        returns only the tasks THIS call actually moved to READY inside
        ``process_event``. It does NOT use a global READY-set diff and does NOT
        re-derive activations from event-idempotency keys after the fact.

        Consequently, under concurrency: if two requests both claim the same
        pending source event, the loser's ``process_event`` is a no-op (the event
        is already PROCESSED) and reports an empty ``activated_task_ids`` -- it
        cannot leak the winner's activation.
        """
        result = Orchestrator(session).process_pending(limit, return_detailed=True)
        return OrchestratorProcessResult(
            processed_events=len(result.events),
            activated_task_ids=sorted(result.activated_task_ids),
        )

    @application.post(
        "/owner/campaigns",
        response_model=CampaignLaunchResult,
        status_code=status.HTTP_201_CREATED,
    )
    def launch_campaign_endpoint(
        request: ProjectCreate,
        session: Session = Depends(get_session),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> CampaignLaunchResult:
        """Owner entry point: submit a real campaign goal and launch the V1 workflow.

        Reuses ``create_project`` + ``create_task`` + ``depends_on`` to build the
        Project and the T1-T9 task graph, kicks off T1, and routes it via the
        existing capability router. The ``Idempotency-Key`` header makes a repeated
        submission safe (no duplicate Project or Tasks).
        """
        try:
            return launch_campaign(session, request, _key(idempotency_key))
        except ServiceError as error:
            raise _translate(error) from error

    @application.get("/owner/campaigns/{project_id}", response_model=BoardRead)
    def owner_campaign_board(
        project_id: str, session: Session = Depends(get_session)
    ) -> dict:
        """Owner view: the live board for a launched campaign (reuses get_board)."""
        try:
            return get_board_service(session, project_id)
        except ServiceError as error:
            raise _translate(error) from error

    return application


app = create_app()
