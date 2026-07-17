from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from aios.campaign import CampaignLaunchResult, launch_campaign
from aios.console import (
    build_board_view,
    owner_board_html,
    owner_error_html,
    owner_home_html,
    owner_not_found_html,
)
from aios.db import get_session, run_migrations
from aios.models import Approval, ApprovalStatus, Artifact, Project, Task, new_id
from aios.orchestrator import Orchestrator, complete_task
from aios.schemas import (
    ApprovalCreate,
    ApprovalDecision,
    ArtifactReviewUpdate,
    BoardRead,
    OrchestratorProcessResult,
    ProjectCreate,
    RevisionRequest,
    TaskCreate,
)
from aios.services import (
    ServiceError,
    decide_approval,
    ensure_pending_approval,
    request_revision,
    set_artifact_review_status,
)
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

    @application.post("/approvals/{approval_id}/decide", response_model=Approval)
    def decide_approval_endpoint(
        approval_id: str,
        decision: ApprovalDecision,
        session: Session = Depends(get_session),
    ) -> Approval:
        try:
            return decide_approval(session, approval_id, decision.decision, decision.rationale)
        except ServiceError as error:
            raise _translate(error) from error

    @application.post(
        "/artifacts/{artifact_id}/review-status",
        response_model=Artifact,
        status_code=status.HTTP_200_OK,
    )
    def set_artifact_review_status_endpoint(
        artifact_id: str,
        update: ArtifactReviewUpdate,
        session: Session = Depends(get_session),
    ) -> Artifact:
        try:
            return set_artifact_review_status(session, artifact_id, update.review_status)
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

    @application.post("/tasks/{task_id}/revision", response_model=Task)
    def request_revision_endpoint(
        task_id: str,
        revision: RevisionRequest,
        session: Session = Depends(get_session),
    ) -> Task:
        try:
            return request_revision(session, task_id, revision.feedback)
        except ServiceError as error:
            raise _translate(error) from error

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

    # --- Minimal owner console (server-rendered HTML; no separate frontend) ---
    # Reuses the SAME service layer as the JSON endpoints above:
    #   POST /owner/launch      -> launch_campaign (behind POST /owner/campaigns)
    #   GET  /owner/board/{id}  -> get_board_service (behind GET /owner/campaigns/{id})
    # No campaign-launch domain logic is duplicated in this UI layer.

    @application.get("/owner", response_class=HTMLResponse)
    def owner_home(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        """Launch form. A fresh idempotency key is embedded so a double-click or
        retry reuses the same key and never creates a duplicate campaign."""
        last_campaign_id = request.cookies.get("aios_last_campaign")
        return HTMLResponse(owner_home_html(idem=new_id("idem"), last_campaign_id=last_campaign_id))

    @application.post("/owner/launch", response_class=HTMLResponse, response_model=None)
    def owner_launch(
        request: Request,
        name: str | None = Form(None),
        objective: str | None = Form(None),
        idem: str | None = Form(None),
        session: Session = Depends(get_session),
    ) -> HTMLResponse | RedirectResponse:
        idem_key = idem or new_id("idem")
        if not name or not name.strip() or not objective or not objective.strip():
            # Empty name/objective: surface a readable Chinese message (no raw JSON).
            return HTMLResponse(
                owner_home_html(
                    idem=idem_key,
                    error="campaign 名称与目标描述都不能为空，请填写后再启动。",
                    last_campaign_id=request.cookies.get("aios_last_campaign"),
                ),
                status_code=400,
            )
        payload = ProjectCreate(name=name, objective=objective)
        try:
            result = launch_campaign(session, payload, idem_key)
        except ServiceError as error:
            # Preserve the same idem so a corrected retry stays a single logical attempt.
            status_code = 400 if error.status_code == 400 else 409
            return HTMLResponse(
                owner_home_html(
                    idem=idem_key,
                    error=error.detail,
                    last_campaign_id=request.cookies.get("aios_last_campaign"),
                ),
                status_code=status_code,
            )
        except Exception:
            # Unexpected failure: return a readable HTML 500 page (never a stack trace).
            # Preserve the same idem so a retry stays part of the same submission lifecycle.
            return HTMLResponse(
                owner_error_html(
                    message="提交时系统出现意外错误，请稍后重试。你的提交标识保持不变。",
                    idem=idem_key,
                    last_campaign_id=request.cookies.get("aios_last_campaign"),
                ),
                status_code=500,
            )
        response = RedirectResponse(url=f"/owner/board/{result.project_id}", status_code=303)
        response.set_cookie(
            "aios_last_campaign", result.project_id, max_age=60 * 60 * 24 * 30
        )
        return response

    @application.get("/owner/board/{project_id}", response_class=HTMLResponse)
    def owner_board(
        project_id: str,
        request: Request,
        session: Session = Depends(get_session),
    ) -> HTMLResponse:
        """Read-only board for a launched campaign (reuses get_board_service)."""
        last_campaign_id = request.cookies.get("aios_last_campaign")
        try:
            view = build_board_view(session, project_id)
        except ServiceError as error:
            if error.status_code == 404:
                return HTMLResponse(owner_not_found_html(project_id), status_code=404)
            # Other handled errors still render as readable HTML, not a JSON body.
            return HTMLResponse(
                owner_error_html(message=error.detail, last_campaign_id=last_campaign_id),
                status_code=error.status_code,
            )
        except Exception:
            # Unexpected failure: readable HTML 500 page, never a stack trace.
            return HTMLResponse(
                owner_error_html(
                    message="读取看板时系统出现意外错误，请稍后重试。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=500,
            )
        return HTMLResponse(owner_board_html(view))

    @application.post(
        "/owner/tasks/{task_id}/decide", response_class=HTMLResponse, response_model=None
    )
    def owner_decide(
        task_id: str,
        request: Request,
        decision: str | None = Form(None),
        rationale: str | None = Form(None),
        session: Session = Depends(get_session),
    ) -> HTMLResponse | RedirectResponse:
        """Owner approves or rejects a gated task (T6 / T8) from the board.

        Lazily creates the L2 gate approval (ensure_pending_approval) then decides it.
        On APPROVED the orchestrator unlocks downstream tasks (T7 / T9 READY).
        """
        last_campaign_id = request.cookies.get("aios_last_campaign")
        task = session.get(Task, task_id)
        if task is None:
            return HTMLResponse(owner_not_found_html(task_id), status_code=404)
        # #47: confine owner actions to the campaign the owner is currently viewing.
        if last_campaign_id is not None and task.project_id != last_campaign_id:
            return HTMLResponse(
                owner_error_html(
                    message="该任务不属于当前看板，无法操作。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=400,
            )
        if decision not in ("approve", "reject"):
            return HTMLResponse(
                owner_error_html(
                    message="请选择「批准」或「驳回」。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=400,
            )
        decision_enum = (
            ApprovalStatus.APPROVED if decision == "approve" else ApprovalStatus.REJECTED
        )
        try:
            approval = ensure_pending_approval(
                session,
                project_id=task.project_id,
                task_id=task_id,
                action_type="owner_gate",
            )
            decide_approval(session, approval.id, decision_enum, rationale)
        except ServiceError as error:
            return HTMLResponse(
                owner_error_html(message=error.detail, last_campaign_id=last_campaign_id),
                status_code=error.status_code,
            )
        except Exception:
            return HTMLResponse(
                owner_error_html(
                    message="处理审批时系统出现意外错误，请稍后重试。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=500,
            )
        return RedirectResponse(url=f"/owner/board/{task.project_id}", status_code=303)

    @application.post(
        "/owner/tasks/{task_id}/revision", response_class=HTMLResponse, response_model=None
    )
    def owner_revision(
        task_id: str,
        request: Request,
        feedback: str | None = Form(None),
        session: Session = Depends(get_session),
    ) -> HTMLResponse | RedirectResponse:
        """Owner requests revision of a returned task; feedback is durably recorded."""
        last_campaign_id = request.cookies.get("aios_last_campaign")
        task = session.get(Task, task_id)
        if task is None:
            return HTMLResponse(owner_not_found_html(task_id), status_code=404)
        # #47: confine owner actions to the campaign the owner is currently viewing.
        if last_campaign_id is not None and task.project_id != last_campaign_id:
            return HTMLResponse(
                owner_error_html(
                    message="该任务不属于当前看板，无法操作。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=400,
            )
        if not feedback or not feedback.strip():
            return HTMLResponse(
                owner_error_html(
                    message="请填写修订意见，说明需要改什么。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=400,
            )
        try:
            request_revision(session, task_id, feedback)
        except ServiceError as error:
            return HTMLResponse(
                owner_error_html(message=error.detail, last_campaign_id=last_campaign_id),
                status_code=error.status_code,
            )
        except Exception:
            return HTMLResponse(
                owner_error_html(
                    message="处理修订时系统出现意外错误，请稍后重试。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=500,
            )
        return RedirectResponse(url=f"/owner/board/{task.project_id}", status_code=303)

    return application


app = create_app()
