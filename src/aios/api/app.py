from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, status
from sqlmodel import Session, select

from aios.db import get_session, run_migrations
from aios.models import Approval, Project, Task, new_id
from aios.schemas import ApprovalCreate, BoardRead, ProjectCreate, TaskCreate
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

    return application


app = create_app()
