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
    owner_measurement_html,
    owner_not_found_html,
)
from aios.db import get_session, run_migrations
from aios.distribution import (
    assemble_distribution_package,
    decide_publish_gate,
    is_package_task,
    is_publish_gate_task,
)
from aios.execution import LLMExecutionAdapter, execute_task
from aios.knowledge_service import KnowledgeService
from aios.measurement import MeasurementService
from aios.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    Capability,
    KnowledgeCandidate,
    KnowledgeReviewDecisionValue,
    Project,
    Task,
    new_id,
)
from aios.orchestrator import Orchestrator, complete_task
from aios.schemas import (
    ApprovalCreate,
    ApprovalDecision,
    ArtifactReviewUpdate,
    BoardRead,
    KnowledgeCandidateCreate,
    KnowledgeReviewRequest,
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

    @application.post(
        "/tasks/{task_id}/execute", response_model=Artifact, status_code=status.HTTP_200_OK
    )
    def execute_task_endpoint(
        task_id: str,
        session: Session = Depends(get_session),
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> Artifact:
        """Run the assigned department agent on a READY task via the real execution
        protocol. Reusing one ``ExecutionAdapter`` (model-backed by default); tests
        inject a deterministic adapter. Idempotent on the ``Idempotency-Key`` header.
        """
        # #35: the packaging task is assembled deterministically, not run via the LLM
        # execution protocol.
        if is_package_task(session, task_id):
            raise HTTPException(
                status_code=400,
                detail="打包任务请调用 POST /tasks/{id}/package，不通过部门执行运行。",
            )
        adapter = LLMExecutionAdapter()
        try:
            return execute_task(session, task_id, idempotency_key, adapter=adapter, actor="agent")
        except ServiceError as error:
            raise _translate(error) from error

    @application.post(
        "/tasks/{task_id}/package", response_model=Artifact, status_code=status.HTTP_200_OK
    )
    def assemble_package_endpoint(
        task_id: str,
        session: Session = Depends(get_session),
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> Artifact:
        """Assemble the distribution package (references T3/T4/T5 outputs) and open the
        L3 publish gate. Deterministic + idempotent on the ``Idempotency-Key`` header;
        nothing is posted to any external platform."""
        task = session.get(Task, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if not is_package_task(session, task_id):
            raise HTTPException(status_code=400, detail="该任务不是打包任务。")
        try:
            return assemble_distribution_package(session, task.project_id, idempotency_key)
        except ServiceError as error:
            raise _translate(error) from error

    @application.post("/tasks/{task_id}/publish-gate", response_model=Approval)
    def publish_gate_endpoint(
        task_id: str,
        decision: ApprovalDecision,
        session: Session = Depends(get_session),
    ) -> Approval:
        """Decide the L3 publish gate. APPROVED marks the package ready (owner posts by
        hand); REJECTED keeps it not ready. Rejected when no package / L3 approval
        exists. No external.publish event is ever emitted."""
        task = session.get(Task, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if not is_publish_gate_task(session, task_id):
            raise HTTPException(status_code=400, detail="该任务不是发布闸门。")
        try:
            return decide_publish_gate(
                session, task.project_id, decision.decision, decision.rationale
            )
        except ServiceError as error:
            raise _translate(error) from error

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

    # --- V1-I5 (#38): knowledge preservation (reuses KnowledgeService) ---

    @application.post(
        "/knowledge/candidates",
        response_model=dict,
        status_code=status.HTTP_201_CREATED,
    )
    def create_knowledge_candidate(
        payload: KnowledgeCandidateCreate,
        session: Session = Depends(get_session),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict:
        """Owner submits a reusable knowledge candidate from an APPROVED artifact.

        Reuses ``KnowledgeService.submit_candidate``; the service enforces the
        APPROVED source + exact-scope rule (AC2), so a non-approved source is 422.
        """
        try:
            candidate = KnowledgeService(session).submit_candidate(
                payload.artifact_id,
                payload.statement,
                payload.scope,
                "owner",
            )
            return {
                "id": candidate.id,
                "artifact_id": candidate.artifact_id,
                "project_id": candidate.project_id,
                "source_project_id": candidate.source_project_id,
                "scope": "company" if candidate.project_id is None else "project",
                "statement": candidate.statement,
                "status": candidate.status.value,
            }
        except ServiceError as error:
            raise _translate(error) from error

    @application.post(
        "/knowledge/candidates/{candidate_id}/review",
        response_model=dict,
    )
    def review_knowledge_candidate(
        candidate_id: str,
        payload: KnowledgeReviewRequest,
        session: Session = Depends(get_session),
    ) -> dict:
        """Owner reviews a knowledge candidate into a versioned KnowledgeFact.

        Reuses ``KnowledgeService.review_candidate`` (versioning / supersede logic).
        APPROVE needs series_id + version; REJECT needs only decision + rationale.
        """
        try:
            decision_value = (
                payload.decision.value
                if isinstance(payload.decision, KnowledgeReviewDecisionValue)
                else payload.decision
            )
            version = payload.version
            supersedes = payload.supersedes_fact_id
            # Auto-compute the next contiguous version when the caller omits it.
            if decision_value == KnowledgeReviewDecisionValue.APPROVE.value and version is None:
                candidate = session.get(KnowledgeCandidate, candidate_id)
                if candidate is None:
                    raise ServiceError(404, "Knowledge candidate not found")
                series = payload.series_id or f"series:{candidate.id}"
                version, head_id = KnowledgeService(session).next_version(
                    series, candidate.project_id
                )
                supersedes = head_id
            result = KnowledgeService(session).review_candidate(
                candidate_id,
                decision_value,
                payload.reviewer,
                payload.rationale,
                series_id=payload.series_id,
                version=version,
                supersedes_fact_id=supersedes,
            )
            return {
                "decision": result.decision.decision.value,
                "fact_id": result.fact.id if result.fact else None,
            }
        except ServiceError as error:
            raise _translate(error) from error

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

    @application.get("/owner/campaigns/{project_id}/measurement")
    def owner_campaign_measurement(
        project_id: str, session: Session = Depends(get_session)
    ) -> dict:
        """Read-only per-campaign V1-I6 metrics (Issue #40). No writes."""
        try:
            return MeasurementService(session).build_campaign(project_id).model_dump(
                mode="json"
            )
        except ValueError as error:
            raise _translate(ServiceError(404, str(error))) from error

    @application.get("/owner/measurement", response_class=HTMLResponse)
    def owner_measurement(
        request: Request, session: Session = Depends(get_session)
    ) -> HTMLResponse:
        """Read-only V1-I6 measurement report across all campaigns (Issue #40)."""
        report = MeasurementService(session).build_report().model_dump(mode="json")
        return HTMLResponse(owner_measurement_html(report))

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
            # #35: the publish gate (T8) is decided through the L3 publish path so the
            # distribution package is marked ready atomically -- never via the generic
            # L2 owner-gate decision (which would not flip the package).
            if is_publish_gate_task(session, task_id):
                decide_publish_gate(session, task.project_id, decision_enum, rationale)
            else:
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

    @application.post(
        "/owner/tasks/{task_id}/execute", response_class=HTMLResponse, response_model=None
    )
    def owner_execute(
        task_id: str,
        request: Request,
        session: Session = Depends(get_session),
    ) -> HTMLResponse | RedirectResponse:
        """Owner triggers a department agent to run a READY task from the board.

        Human-controlled (owner clicks), not automatic. Uses the model-backed
        adapter; if AIOS_AGENT_* is unconfigured it returns a readable 503 page.
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
        # #35: the packaging task is assembled deterministically, not run via the LLM
        # execution protocol -- steer the owner to "生成分发包" instead.
        if is_package_task(session, task_id):
            return HTMLResponse(
                owner_error_html(
                    message="打包任务请使用「生成分发包」按钮，不通过部门执行运行。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=400,
            )
        try:
            execute_task(
                session, task_id, new_id("idem"), adapter=LLMExecutionAdapter(), actor="owner"
            )
        except ServiceError as error:
            return HTMLResponse(
                owner_error_html(message=error.detail, last_campaign_id=last_campaign_id),
                status_code=error.status_code,
            )
        except Exception:
            return HTMLResponse(
                owner_error_html(
                    message="部门执行时出现意外错误，请稍后重试。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=500,
            )
        return RedirectResponse(url=f"/owner/board/{task.project_id}", status_code=303)

    @application.post(
        "/owner/tasks/{task_id}/package", response_class=HTMLResponse, response_model=None
    )
    def owner_package(
        task_id: str,
        request: Request,
        session: Session = Depends(get_session),
    ) -> HTMLResponse | RedirectResponse:
        """Owner assembles the distribution package from the T3/T4/T5 outputs.

        Deterministic (no LLM): bundles the approved platform outputs into one
        Artifact + opens the L3 publish gate. No content is ever posted anywhere.
        """
        last_campaign_id = request.cookies.get("aios_last_campaign")
        task = session.get(Task, task_id)
        if task is None:
            return HTMLResponse(owner_not_found_html(task_id), status_code=404)
        if last_campaign_id is not None and task.project_id != last_campaign_id:
            return HTMLResponse(
                owner_error_html(
                    message="该任务不属于当前看板，无法操作。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=400,
            )
        if not is_package_task(session, task_id):
            return HTMLResponse(
                owner_error_html(
                    message="该任务不是打包任务，无法生成分发包。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=400,
            )
        try:
            assemble_distribution_package(session, task.project_id, new_id("idem"))
        except ServiceError as error:
            return HTMLResponse(
                owner_error_html(message=error.detail, last_campaign_id=last_campaign_id),
                status_code=error.status_code,
            )
        except Exception:
            return HTMLResponse(
                owner_error_html(
                    message="生成分发包时系统出现意外错误，请稍后重试。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=500,
            )
        return RedirectResponse(url=f"/owner/board/{task.project_id}", status_code=303)

    @application.post(
        "/owner/tasks/{task_id}/publish", response_class=HTMLResponse, response_model=None
    )
    def owner_publish(
        task_id: str,
        request: Request,
        decision: str | None = Form(None),
        rationale: str | None = Form(None),
        session: Session = Depends(get_session),
    ) -> HTMLResponse | RedirectResponse:
        """Owner decides the L3 publish gate: approve marks the package ready.

        The system only flips the package to ready; the owner copies the content and
        posts by hand. Nothing auto-posts and no external.publish event is emitted.
        """
        last_campaign_id = request.cookies.get("aios_last_campaign")
        task = session.get(Task, task_id)
        if task is None:
            return HTMLResponse(owner_not_found_html(task_id), status_code=404)
        if last_campaign_id is not None and task.project_id != last_campaign_id:
            return HTMLResponse(
                owner_error_html(
                    message="该任务不属于当前看板，无法操作。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=400,
            )
        if not is_publish_gate_task(session, task_id):
            return HTMLResponse(
                owner_error_html(
                    message="该任务不是发布闸门，无法在此发布。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=400,
            )
        if decision not in ("approve", "reject"):
            return HTMLResponse(
                owner_error_html(
                    message="请选择「批准发布」或「驳回」。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=400,
            )
        decision_enum = (
            ApprovalStatus.APPROVED if decision == "approve" else ApprovalStatus.REJECTED
        )
        try:
            decide_publish_gate(session, task.project_id, decision_enum, rationale)
        except ServiceError as error:
            return HTMLResponse(
                owner_error_html(message=error.detail, last_campaign_id=last_campaign_id),
                status_code=error.status_code,
            )
        except Exception:
            return HTMLResponse(
                owner_error_html(
                    message="处理发布闸门时系统出现意外错误，请稍后重试。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=500,
            )
        return RedirectResponse(url=f"/owner/board/{task.project_id}", status_code=303)

    @application.post(
        "/owner/tasks/{task_id}/preserve", response_class=HTMLResponse, response_model=None
    )
    def owner_preserve(
        task_id: str,
        request: Request,
        artifact_id: str | None = Form(None),
        statement: str | None = Form(None),
        scope: str | None = Form(None),
        session: Session = Depends(get_session),
    ) -> HTMLResponse | RedirectResponse:
        """Owner preserves knowledge from an APPROVED source artifact (T9 action).

        Reuses ``KnowledgeService.submit_candidate``; the source must be APPROVED
        (AC2). The candidate then appears in the board's review area for the owner.
        Scope is chosen here (project default; company is explicit opt-in) and is
        read-only at review time -- review can never change it.
        """
        last_campaign_id = request.cookies.get("aios_last_campaign")
        task = session.get(Task, task_id)
        if task is None:
            return HTMLResponse(owner_not_found_html(task_id), status_code=404)
        # #38 B5: only the T9 knowledge-capture task may preserve knowledge.
        # ``required_capabilities`` stores capability IDs (UUIDs), so resolve the
        # canonical "knowledge_capture" capability id and check membership.
        kc_cap = session.exec(
            select(Capability).where(Capability.name == "knowledge_capture")
        ).first()
        if kc_cap is None or kc_cap.id not in (task.required_capabilities or []):
            return HTMLResponse(
                owner_error_html(
                    message="只有 T9 知识沉淀任务可以沉淀知识，请在该任务下操作。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=400,
            )
        if last_campaign_id is not None and task.project_id != last_campaign_id:
            return HTMLResponse(
                owner_error_html(
                    message="该任务不属于当前看板，无法操作。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=400,
            )
        if not artifact_id or not artifact_id.strip() or not statement or not statement.strip():
            return HTMLResponse(
                owner_error_html(
                    message="请选择已批准的来源并填写知识陈述。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=400,
            )
        # #38 B2 (source ownership): the approved artifact must belong to the active
        # campaign, so the candidate's provenance is the campaign the owner is in.
        artifact = session.get(Artifact, artifact_id.strip())
        if (
            artifact is not None
            and last_campaign_id is not None
            and artifact.project_id != last_campaign_id
        ):
            return HTMLResponse(
                owner_error_html(
                    message="来源产物不属于当前 campaign，无法在此沉淀。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=400,
            )
        effective_scope = scope if scope in ("project", "company") else "project"
        try:
            KnowledgeService(session).submit_candidate(
                artifact_id.strip(),
                statement.strip(),
                effective_scope,
                "owner",
            )
        except ServiceError as error:
            return HTMLResponse(
                owner_error_html(message=error.detail, last_campaign_id=last_campaign_id),
                status_code=error.status_code,
            )
        except Exception:
            return HTMLResponse(
                owner_error_html(
                    message="沉淀知识时系统出现意外错误，请稍后重试。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=500,
            )
        return RedirectResponse(url=f"/owner/board/{task.project_id}", status_code=303)

    @application.post(
        "/owner/knowledge/{candidate_id}/review",
        response_class=HTMLResponse,
        response_model=None,
    )
    def owner_review_knowledge(
        candidate_id: str,
        request: Request,
        decision: str | None = Form(None),
        reviewer: str | None = Form(None),
        rationale: str | None = Form(None),
        series_id: str | None = Form(None),
        version: str | None = Form(None),
        session: Session = Depends(get_session),
    ) -> HTMLResponse | RedirectResponse:
        """Owner reviews a knowledge candidate -> versioned KnowledgeFact.

        Reuses ``KnowledgeService.review_candidate``. approve needs series_id +
        version; reject needs only a rationale.
        """
        last_campaign_id = request.cookies.get("aios_last_campaign")
        if decision not in ("approve", "reject"):
            return HTMLResponse(
                owner_error_html(
                    message="请选择「批准」或「驳回」。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=400,
            )
        if not rationale or not rationale.strip():
            return HTMLResponse(
                owner_error_html(
                    message="请填写审阅理由。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=400,
            )
        # #38 B2 (trust boundary): load the candidate and verify it belongs to the
        # active owner campaign's source provenance, and is still pending review.
        # Review can never change scope -- scope was fixed at preserve time.
        if last_campaign_id is None:
            return HTMLResponse(
                owner_error_html(
                    message="无法确认当前 campaign，请先打开对应看板再评审。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=400,
            )
        candidate = session.get(KnowledgeCandidate, candidate_id)
        if candidate is None:
            return HTMLResponse(owner_not_found_html(candidate_id), status_code=404)
        if candidate.source_project_id != last_campaign_id:
            return HTMLResponse(
                owner_error_html(
                    message="该知识候选不属于当前 campaign，无法跨 campaign 评审。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=400,
            )
        decision_value = (
            KnowledgeReviewDecisionValue.APPROVE
            if decision == "approve"
            else KnowledgeReviewDecisionValue.REJECT
        )
        parsed_version = None
        if version and version.strip():
            try:
                parsed_version = int(version.strip())
            except ValueError:
                return HTMLResponse(
                    owner_error_html(
                        message="版本号必须为正整数。",
                        last_campaign_id=last_campaign_id,
                    ),
                    status_code=400,
                )
        # Auto-compute the next contiguous version when the owner leaves it blank,
        # so the owner never has to track versions manually (V1-I5 requirement).
        series = series_id.strip() if series_id else None
        supersedes = None
        if decision_value == KnowledgeReviewDecisionValue.APPROVE and parsed_version is None:
            if not series:
                series = f"series:{candidate.id}"
            parsed_version, head_id = KnowledgeService(session).next_version(
                series, candidate.project_id
            )
            supersedes = head_id
        try:
            KnowledgeService(session).review_candidate(
                candidate.id,
                decision_value,
                reviewer or "owner",
                rationale.strip(),
                series_id=series,
                version=parsed_version,
                supersedes_fact_id=supersedes,
            )
        except ServiceError as error:
            return HTMLResponse(
                owner_error_html(message=error.detail, last_campaign_id=last_campaign_id),
                status_code=error.status_code,
            )
        except Exception:
            return HTMLResponse(
                owner_error_html(
                    message="审阅知识时系统出现意外错误，请稍后重试。",
                    last_campaign_id=last_campaign_id,
                ),
                status_code=500,
            )
        # Return to the board the candidate belongs to (best-effort via cookie).
        target = last_campaign_id or candidate_id
        return RedirectResponse(
            url=f"/owner/board/{target}", status_code=303
        )

    return application


app = create_app()
