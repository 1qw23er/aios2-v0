from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import (
    Body,
    Depends,
    FastAPI,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlmodel import Session, select

from aios.actor import ActorContext
from aios.agent_registry import (
    create_agent_via_bootstrap,
    get_agent,
    list_agents,
    list_capabilities,
    register_agent,
    rotate_credential,
    set_agent_enabled,
    upsert_agent,
)
from aios.api.security import (
    _AGENT_UNAUTH_HEADERS,
    BootstrapClaims,
    authenticate_agent,
    authenticate_bootstrap_token,
    authenticate_owner,
)
from aios.audit import AuditLog
from aios.campaign import CampaignLaunchResult, launch_campaign
from aios.console import (
    build_board_view,
    owner_agents_html,
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
from aios.knowledge_tags import report_unclassified_knowledge
from aios.measurement import MeasurementService
from aios.models import (
    Agent,
    Approval,
    ApprovalStatus,
    Artifact,
    Capability,
    KnowledgeCandidate,
    KnowledgeReviewDecisionValue,
    Project,
    ReviewPolicy,
    ReviewResult,
    Task,
    new_id,
)
from aios.orchestrator import Orchestrator, complete_task
from aios.relay import relay_work_log
from aios.review import (
    create_review_policy,
    dispatch_reviews_for_artifact,
    owner_approve_review,
    request_review_revision,
    submit_review_from_artifact,
)
from aios.schemas import (
    AgentBootstrapResponse,
    AgentEnabledUpdate,
    AgentPublic,
    AgentRegister,
    AgentRegistrationResponse,
    AgentSelfRegister,
    ApprovalCreate,
    ApprovalDecision,
    BoardRead,
    KnowledgeCandidateCreate,
    KnowledgeClassifyRequest,
    KnowledgeReviewRequest,
    OrchestratorProcessResult,
    ProjectCreate,
    ReviewPolicyCreate,
    RevisionRequest,
    TaskCreate,
    WorkLogAttest,
    WorkLogSubmit,
)
from aios.secrets_store import SecretStoreUnavailable, validate_secret_store_config
from aios.services import (
    ServiceError,
    decide_approval,
    ensure_pending_approval,
    request_revision,
)
from aios.services import create_approval as create_approval_service
from aios.services import create_project as create_project_service
from aios.services import create_task as create_task_service
from aios.services import get_board as get_board_service
from aios.work_log import ContentFeed, WorkLogService


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    run_migrations()
    # Fail-closed startup validation: an explicitly selected encrypted_db
    # backend without a valid KEK (or an unknown backend) must crash at boot
    # with a readable message, not serve silent 503s. (issue #103 follow-up)
    validate_secret_store_config()
    yield


def _translate(error: ServiceError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.detail)


def _secret_unavailable() -> HTTPException:
    """Map a secret-store outage to HTTP 503 (issue #103 §6)."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="secret_store_unavailable",
    )


def _key(value: str | None) -> str:
    return value or new_id("idem")


# Fixed audit allowlist (req 2): only these scalar snapshot keys may surface in
# the derived ``safe_delta``. Anything else -- Artifact body, KnowledgeFact
# statement, prompt, LLM I/O, owner revision reason text -- is never returned.
_AUDIT_SAFE_SNAPSHOT_KEYS = {
    # status / outcome changes
    "status",
    "review_status",
    "overall",
    # review round / dimension / reviewer identity (IDs + scalar names)
    "review_round",
    "review_dimension",
    "reviewer",
    "reviewer_agent_id",
    # capability / tag names (controlled vocabulary, not free text)
    "capability",
    "capabilities",
    "tag",
    "tags",
    # fact / artifact counts
    "fact_count",
    "artifact_count",
    # IDs that are themselves safe to expose (traceability, not content)
    "review_target_artifact_id",
    "review_policy_id",
    "assigned_reviewer_agent_id",
    "revision_of",
    "revision_count",
    "max_revisions",
    "escalated",
}


def _audit_safe_delta(snapshot: object) -> dict[str, object]:
    """Lift ONLY allowlisted scalar keys from a before/after snapshot.

    Nested dicts/lists are dropped (they could hide arbitrary content such as an
    Artifact body or a KnowledgeFact statement). The raw snapshot is never
    returned -- only this curated, flat, allowlisted view.
    """
    if not isinstance(snapshot, dict):
        return {}
    out: dict[str, object] = {}
    for key, value in snapshot.items():
        if key in _AUDIT_SAFE_SNAPSHOT_KEYS and not isinstance(value, (dict, list)):
            out[key] = value
    return out


class ReviewDispatchRequest(BaseModel):
    """Owner-supplied dispatch request for the review protocol (#69 / C2).

    Only ``policy_id`` is accepted from the client. The target artifact is the
    URL path; reviewers, round, and bindings are all derived server-side and
    never trusted from this payload (C2/C3).
    """

    policy_id: str
    executor_agent_id: str | None = None


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
        actor: ActorContext = Depends(authenticate_owner),
    ) -> Approval:
        try:
            return decide_approval(
                session, approval_id, decision.decision, decision.rationale, actor=actor
            )
        except ServiceError as error:
            raise _translate(error) from error

    @application.api_route(
        "/artifacts/{artifact_id}/review-status",
        methods=["POST", "PUT", "PATCH"],
        status_code=status.HTTP_410_GONE,
    )
    def set_artifact_review_status_endpoint(artifact_id: str) -> None:
        """Permanently removed (#74, decision 2).

        This route was a direct write to ``Artifact.review_status`` that bypassed
        BOTH the dual-reviewer aggregation AND the owner final gate -- an artifact
        could be forced to APPROVED without any review at all. It is now 410 Gone
        (not merely owner-authenticated): there is intentionally NO owner-only
        version of this shortcut. The only sanctioned path to APPROVED is
        ``POST /tasks/{id}/review/submit`` (dual reviewers aggregate) followed by
        ``POST /artifacts/{id}/reviews/approve`` (owner final gate).
        """
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=(
                "该直改审阅状态的后门已永久移除；请走双评审聚合 + owner 终审门 "
                "(/tasks/{id}/review/submit 后 /artifacts/{id}/reviews/approve)。"
            ),
        )

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
        actor: ActorContext = Depends(authenticate_owner),
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
                session, task.project_id, decision.decision, decision.rationale, actor=actor
            )
        except ServiceError as error:
            raise _translate(error) from error

    @application.post("/tasks/{task_id}/revision", response_model=Task)
    def request_revision_endpoint(
        task_id: str,
        revision: RevisionRequest,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> Task:
        try:
            return request_revision(session, task_id, revision.feedback, actor=actor)
        except ServiceError as error:
            raise _translate(error) from error

    # --- Review Protocol runtime wiring (#69 / C1-C6) ------------------------
    # These endpoints connect the independent review protocol to the live runtime.
    # Every identity (target artifact, reviewer agent, policy, round) is
    # server-assigned and never trusted from the client (C2/C3). The owner final
    # gate is the ONLY path that promotes a reviewed artifact to APPROVED (C1).
    # The GET /audit endpoint is owner-only, strictly read-only, and never returns
    # secrets or un-redacted payloads (C6).

    @application.post(
        "/artifacts/{artifact_id}/reviews/dispatch",
        response_model=list[Task],
        status_code=status.HTTP_200_OK,
    )
    def dispatch_reviews_endpoint(
        artifact_id: str,
        payload: ReviewDispatchRequest,
        session: Session = Depends(get_session),
    ) -> list[Task]:
        """Server-dispatches the required number of Review Tasks for an artifact.

        Reviewers are selected server-side from the policy's trust/capability pool
        (never client-supplied). Each Review Task is bound (target artifact + policy
        + round) via metadata. The artifact stays UNVERIFIED until reviews are
        submitted and aggregated.
        """
        policy = session.get(ReviewPolicy, payload.policy_id)
        if policy is None:
            raise HTTPException(status_code=404, detail="Review policy not found")
        try:
            return dispatch_reviews_for_artifact(
                session,
                target_artifact_id=artifact_id,
                policy=policy,
                executor_agent_id=payload.executor_agent_id,
            )
        except ServiceError as error:
            raise _translate(error) from error

    @application.post(
        "/review-policies",
        response_model=ReviewPolicy,
        status_code=status.HTTP_201_CREATED,
    )
    def create_review_policy_endpoint(
        request: ReviewPolicyCreate,
        response: Response,
        actor: ActorContext = Depends(authenticate_owner),
        session: Session = Depends(get_session),
    ) -> ReviewPolicy:
        """Create a ReviewPolicy (D3 hardening, #72 / #74).

        The ONLY production path that writes a ReviewPolicy row, which addresses
        part of the review-protocol blocker: ``dispatch`` requires an existing
        ``policy_id`` and is intentionally unchanged (no implicit creation).
        Equivalent duplicate requests return the existing policy with HTTP 200;
        a newly created policy returns HTTP 201; a same-name policy with a
        different config returns 409.

        OWNER-AUTHENTICATED. The ``authenticate_owner`` dependency resolves a
        trusted ``ActorContext(kind="owner", owner_id=AIOS_OWNER_ID)``; its
        ``owner_id`` is recorded as the audit actor. No owner credential check
        is bypassed and the production route never constructs an ``ActorContext``
        directly -- access control is enforced entirely by ``authenticate_owner``.
        """
        try:
            policy, created = create_review_policy(
                session,
                actor=actor,
                name=request.name,
                applies_to=request.applies_to,
                dimensions=request.dimensions,
                brand_policy_id=request.brand_policy_id,
                required_reviewer_trust=request.required_reviewer_trust,
                required_capabilities=request.required_capabilities,
                max_revisions=request.max_revisions,
                required_reviewers=request.required_reviewers,
                enabled=request.enabled,
                project_id=request.project_id,
            )
        except ServiceError as error:
            raise _translate(error) from error
        # Distinguish "created new" (201) from "returned existing" (200) so the
        # caller can tell whether a row was actually written.
        if not created:
            response.status_code = status.HTTP_200_OK
        return policy

    @application.post(
        "/tasks/{review_task_id}/review/submit",
        response_model=ReviewResult,
        status_code=status.HTTP_200_OK,
    )
    def submit_review_endpoint(
        review_task_id: str,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> ReviewResult:
        """Map a completed Review Task's Artifact into a trusted ReviewResult (C3).

        Owner-authenticated orchestration command (#74, decision 1): the owner
        TRIGGERS the mapping but is NEVER recorded as the reviewer. The reviewer
        agent identity is taken from the TRUSTED Task assignment
        (``assigned_agent_id``), never the artifact output, the client, or the
        owner. The AuditLog actor is the real ``owner_id``. The owner cannot set
        the reviewer / target / round / dimension. The caller must have executed
        the review task first (so its Artifact exists).
        """
        try:
            return submit_review_from_artifact(
                session, review_task_id=review_task_id, actor=actor
            )
        except ServiceError as error:
            raise _translate(error) from error

    @application.post(
        "/artifacts/{artifact_id}/reviews/approve",
        response_model=Artifact,
        status_code=status.HTTP_200_OK,
    )
    def owner_approve_review_endpoint(
        artifact_id: str,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> Artifact:
        """Owner final gate (C1): the ONLY path that sets a reviewed artifact to
        APPROVED. Requires ``REVIEW_PASSED`` (all required reviewers passed and the
        review gate opened). AI reviewers can never reach this endpoint's effect.
        """
        try:
            return owner_approve_review(session, artifact_id=artifact_id, actor=actor)
        except ServiceError as error:
            raise _translate(error) from error

    @application.post(
        "/tasks/{task_id}/review/revision",
        response_model=Task,
        status_code=status.HTTP_200_OK,
    )
    def request_review_revision_endpoint(
        task_id: str,
        payload: RevisionRequest,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> Task:
        """Owner-requested revision -- PREPARE ONLY, never runs an LLM (req 4).

        Records the owner's real feedback durably, idempotently prepares ONE READY
        revision execution Task (the Content Agent runs it later via the existing
        execute endpoint), invalidates the old pending review-gate Approval, and
        returns. It must NOT synchronously call ``execute_task`` or a remote LLM.
        """
        try:
            return request_review_revision(
                session, task_id=task_id, feedback=payload.feedback, actor=actor
            )
        except ServiceError as error:
            raise _translate(error) from error

    @application.get("/audit", response_model=dict)
    def audit_log_endpoint(
        action: str | None = Query(default=None),
        resource_type: str | None = Query(default=None),
        project_id: str | None = Query(default=None),
        task_id: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None),
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> dict:
        """Owner-only, read-only audit trail query with a FIXED SAFE PROJECTION (C6).

        Filters by ``action`` / ``resource_type`` / ``project_id`` / ``task_id``
        (``task_id`` is an indexed column, not JSON). Reverse-chronological with a
        STABLE cursor (``created_at`` + ``id``) so pagination never skips or
        duplicates rows.

        Security (req 2): the response is a fixed allowlist -- it can NEVER return
        arbitrary snapshot bodies. Only the explicit safe columns are returned:

          ALLOWED: id, actor, action, resource_type, resource_id, project_id,
          task_id, created_at, and a derived ``safe_delta`` containing ONLY
          allowlisted scalar keys lifted from before/after snapshots (status
          changes, review_round, reviewer_agent_id, capability/tag names,
          fact/artifact counts, IDs).

          FORBIDDEN (never returned): Artifact body, KnowledgeFact statement,
          prompt, LLM input/output, owner revision reason full text, and any
          non-allowlisted snapshot field. ``redact_secrets`` is NOT a substitute
          for this allowlist -- the raw snapshots are simply never serialized.

        Owner authentication is enforced by the ``authenticate_owner`` dependency
        (#74): an unauthenticated request never reaches this body.
        """
        stmt = select(AuditLog)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        if resource_type is not None:
            stmt = stmt.where(AuditLog.resource_type == resource_type)
        if project_id is not None:
            stmt = stmt.where(AuditLog.project_id == project_id)
        if task_id is not None:
            stmt = stmt.where(AuditLog.task_id == task_id)
        stmt = stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        if cursor:
            cur = session.get(AuditLog, cursor)
            if cur is not None:
                stmt = stmt.where(
                    or_(
                        AuditLog.created_at < cur.created_at,
                        (AuditLog.created_at == cur.created_at) & (AuditLog.id < cur.id),
                    )
                )
        rows = session.exec(stmt.limit(limit + 1)).all()
        has_more = len(rows) > limit
        items = rows[:limit]
        out = []
        for row in items:
            # Fixed safe projection: only allowlisted columns + a derived,
            # allowlisted-only delta. The raw before/after snapshots are NEVER
            # serialized, so Artifact bodies / KnowledgeFact statements / prompts /
            # LLM I/O / owner reason text can never leak through this endpoint.
            safe = {
                "id": row.id,
                "actor": row.actor,
                "action": row.action,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "project_id": row.project_id,
                "task_id": row.task_id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "safe_delta": {
                    "before": _audit_safe_delta(row.before_snapshot),
                    "after": _audit_safe_delta(row.after_snapshot),
                },
            }
            out.append(safe)
        next_cursor = items[-1].id if (has_more and items) else None
        return {"items": out, "next_cursor": next_cursor, "limit": limit}

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
        _owner: ActorContext = Depends(authenticate_owner),
    ) -> OrchestratorProcessResult:
        """Drive the orchestrator for pending completion events.

        Owner-only for now (#74, decision 3): protected by ``authenticate_owner``.
        There is intentionally NO separate internal-service credential and the
        outbound ``AIOS_AGENT_API_KEY`` is NOT reused for inbound authentication.

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
        actor: ActorContext = Depends(authenticate_owner),
    ) -> dict:
        """Owner submits a reusable knowledge candidate from an APPROVED artifact.

        Reuses ``KnowledgeService.submit_candidate``. The submitter identity is
        ALWAYS the trusted owner actor (never request-controlled); ``scope`` selects
        the effective project_id ("company" => None, "project" => source campaign).
        The service enforces the APPROVED source + exact-scope rule (AC2).
        """
        try:
            project_id: str | None = None
            if payload.scope != "company":
                # project scope: candidate is scoped to its source campaign.
                artifact = session.get(Artifact, payload.artifact_id)
                project_id = artifact.project_id if artifact is not None else None
            candidate = KnowledgeService(session).submit_candidate(
                payload.artifact_id,
                payload.statement,
                project_id=project_id,
                tags=payload.tags,
                actor=actor,
            )
            return {
                "id": candidate.id,
                "artifact_id": candidate.artifact_id,
                "project_id": candidate.project_id,
                "source_project_id": candidate.source_project_id,
                "scope": "company" if candidate.project_id is None else "project",
                "statement": candidate.statement,
                "status": "DRAFT",
                "tags": candidate.tags,
                "submitted_by_kind": candidate.submitted_by_kind,
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
        actor: ActorContext = Depends(authenticate_owner),
    ) -> dict:
        """Owner reviews a knowledge candidate into a versioned KnowledgeFact.

        Reuses ``KnowledgeService.review_candidate`` (versioning / supersede logic).
        The reviewer identity is ALWAYS the trusted owner actor. APPROVE needs
        series_id + version; REJECT needs only decision + rationale.
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
                payload.rationale,
                actor=actor,
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
        "/knowledge/candidates/{candidate_id}/classify",
        response_model=dict,
    )
    def classify_knowledge_candidate(
        candidate_id: str,
        payload: KnowledgeClassifyRequest,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> dict:
        """Owner-only: promote a legacy DRAFT sentinel candidate to canonical tags once."""
        try:
            candidate = KnowledgeService(session).classify_candidate_tags(
                candidate_id,
                payload.tags,
                actor=actor,
            )
            return {
                "id": candidate.id,
                "tags": candidate.tags,
                "status": candidate.status.value,
            }
        except ServiceError as error:
            raise _translate(error) from error

    @application.post(
        "/knowledge/facts/{fact_id}/classify",
        response_model=dict,
    )
    def classify_knowledge_fact(
        fact_id: str,
        payload: KnowledgeClassifyRequest,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> dict:
        """Owner-only: promote a legacy sentinel fact to canonical tags once."""
        try:
            fact = KnowledgeService(session).classify_knowledge(
                fact_id,
                payload.tags,
                actor=actor,
            )
            return {
                "id": fact.id,
                "tags": fact.tags,
                "status": fact.status.value,
            }
        except ServiceError as error:
            raise _translate(error) from error

    @application.post(
        "/knowledge/facts/{fact_id}/deactivate",
        response_model=dict,
    )
    def deactivate_knowledge_fact(
        fact_id: str,
        payload: RevisionRequest,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> dict:
        """Owner-only: deactivate an approved knowledge fact (owner gate)."""
        try:
            fact = KnowledgeService(session).deactivate_fact(
                fact_id,
                payload.feedback,
                actor=actor,
            )
            return {"id": fact.id, "status": fact.status.value}
        except ServiceError as error:
            raise _translate(error) from error

    @application.get(
        "/knowledge/unclassified",
        response_model=list,
    )
    def list_unclassified_knowledge(
        session: Session = Depends(get_session),
        _owner: ActorContext = Depends(authenticate_owner),
    ) -> list[dict]:
        """Owner-visible report of approved facts still carrying the legacy sentinel."""
        return report_unclassified_knowledge(session)

    @application.post(
        "/work-logs",
        response_model=dict,
        status_code=status.HTTP_201_CREATED,
    )
    def submit_work_log_endpoint(
        payload: WorkLogSubmit,
        response: Response,
        session: Session = Depends(get_session),
        idempotency_key: str = Header(alias="Idempotency-Key"),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> dict:
        """Submit an AI-worker work log as ``Artifact(type=WORK_LOG)`` (#88 plan §9).

        OWNER-AUTHENTICATED; the ``Idempotency-Key`` header is REQUIRED (missing
        header -> 422 by FastAPI). Semantics follow plan §5: a brand-new log
        returns 201; replaying the same key with the same payload returns the
        existing log with 200; reusing the key with a different payload -> 409.
        The log is created ``UNVERIFIED`` -- only ``POST /work-logs/{id}/attest``
        can make it APPROVED (plan §7). ``produced_by_agent_id`` is provenance
        only and is proven against a durable ``ExecutionAssignment`` (plan §6);
        it never becomes the actor.
        """
        try:
            artifact, created = WorkLogService(session).submit_work_log(
                project_id=payload.project_id,
                report_type=payload.report_type,
                what_done=payload.what_done,
                why=payload.why,
                problem=payload.problem,
                solution=payload.solution,
                new_knowledge=payload.new_knowledge,
                idempotency_key=idempotency_key,
                actor=actor,
                task_ref=payload.task_ref,
                produced_by_agent_id=payload.produced_by_agent_id,
                execution_assignment_id=payload.execution_assignment_id,
                content_value=payload.content_value,
                should_enter_kb=payload.should_enter_kb,
                content_angle=payload.content_angle,
                source_platform=payload.source_platform,
            )
        except ServiceError as error:
            raise _translate(error) from error
        # 201 only when this call actually inserted a new row; replays and
        # concurrent-duplicate losers get 200. Derived atomically from the
        # service result (no preliminary query -> no TOCTOU, no key-whitespace
        # mismatch, since the service trims the key before hashing).
        if not created:
            response.status_code = status.HTTP_200_OK
        metadata = artifact.metadata_json or {}
        return {
            "id": artifact.id,
            "project_id": artifact.project_id,
            "task_id": artifact.task_id,
            "review_status": artifact.review_status.value,
            "report_type": metadata.get("report_type"),
            "content_value": metadata.get("content_value"),
            "should_enter_kb": metadata.get("should_enter_kb"),
            "content_angle": metadata.get("content_angle"),
            "source_platform": metadata.get("source_platform"),
            "created_at": artifact.created_at.isoformat(),
        }

    @application.post(
        "/work-logs/{artifact_id}/attest",
        response_model=dict,
    )
    def attest_work_log_endpoint(
        artifact_id: str,
        body: WorkLogAttest | None = Body(default=None),
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> dict:
        """Owner attestation: the ONLY path that APPROVEs a work log (#88 plan §7.2).

        OWNER-AUTHENTICATED. Optional body ``{should_enter_kb, content_value}``
        lets the owner decide KB eligibility at attest time (#92 plan §6); an
        empty/no body keeps the submitted metadata values (backward
        compatible). Atomically writes the full evidence chain (Approval
        ``work_log_attestation`` + status flip + AuditLog
        ``work_log.owner_attested`` with a before/after snapshot of the two
        judgement fields) under a database write lock. Idempotent: an
        already-APPROVED log with complete evidence returns 200 no-op; a
        conflicting override on an already-APPROVED log -> 409 fail-closed;
        an APPROVED log with missing/conflicting evidence -> 409 fail-closed.
        """
        try:
            artifact = WorkLogService(session).attest_work_log(
                artifact_id=artifact_id,
                actor=actor,
                should_enter_kb=body.should_enter_kb if body else None,
                content_value=body.content_value if body else None,
            )
        except ServiceError as error:
            raise _translate(error) from error
        metadata = artifact.metadata_json or {}
        return {
            "id": artifact.id,
            "review_status": artifact.review_status.value,
            "should_enter_kb": metadata.get("should_enter_kb"),
            "content_value": metadata.get("content_value"),
        }

    @application.get(
        "/content-feed",
        response_model=None,
    )
    def content_feed_endpoint(
        project_id: str | None = Query(default=None),
        min_value: str = Query(default="medium"),
        limit: int = Query(default=100),
        offset: int = Query(default=0),
        source_platform: str | None = Query(default=None),
        log_limit: int | None = Query(default=None),
        log_offset: int | None = Query(default=None),
        fact_limit: int | None = Query(default=None),
        fact_offset: int | None = Query(default=None),
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> list[dict] | dict:
        """Read-only content feed: allowed-scope work logs + APPROVED facts (#88 §8.3).

        OWNER-AUTHENTICATED. ``project_id=P`` -> P's logs + P-scope facts +
        company-scope facts; omitted -> company view (everything). Parameter
        validation (min_value / limit / offset) is enforced by the service so
        the CLI export shares the exact same contract.

        V2 (#92): ``source_platform`` switches to the structured split view
        ``{work_logs, facts}`` with independent pagination
        (``log_limit``/``log_offset`` for platform-filtered logs,
        ``fact_limit``/``fact_offset`` for facts). Omitting ``source_platform``
        returns the flat merged list unchanged (#88 zero-regression); the split
        pagination params are only meaningful together with ``source_platform``.
        """
        try:
            return ContentFeed(session).get_content_feed(
                actor=actor,
                project_id=project_id,
                min_value=min_value,
                limit=limit,
                offset=offset,
                source_platform=source_platform,
                log_limit=log_limit,
                log_offset=log_offset,
                fact_limit=fact_limit,
                fact_offset=fact_offset,
            )
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
        _owner: ActorContext = Depends(authenticate_owner),
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
        project_id: str,
        session: Session = Depends(get_session),
        _owner: ActorContext = Depends(authenticate_owner),
    ) -> dict:
        """Owner view: the live board for a launched campaign (reuses get_board)."""
        try:
            return get_board_service(session, project_id)
        except ServiceError as error:
            raise _translate(error) from error

    @application.get("/owner/campaigns/{project_id}/measurement")
    def owner_campaign_measurement(
        project_id: str,
        session: Session = Depends(get_session),
        _owner: ActorContext = Depends(authenticate_owner),
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
        request: Request,
        session: Session = Depends(get_session),
        _owner: ActorContext = Depends(authenticate_owner),
    ) -> HTMLResponse:
        """Read-only V1-I6 measurement report across all campaigns (Issue #40)."""
        report = MeasurementService(session).build_report().model_dump(mode="json")
        return HTMLResponse(owner_measurement_html(report))

    # --- Agent Interoperability Gateway: DB-backed agent registry (#57, #61) ---
    # Reuses the same ``agent_registry`` service as the owner console below.

    @application.get("/agents", response_model=list[AgentPublic])
    def list_registered_agents(
        session: Session = Depends(get_session),
        capability: str | None = Query(default=None),
    ) -> list[AgentPublic]:
        """List registry agents. When ``capability`` is supplied, returns only
        enabled agents declaring that capability (fail-closed 422 on an unknown
        slug). The capability catalog is the single source of truth."""
        try:
            return list_agents(session, capability=capability)
        except ServiceError as error:
            raise _translate(error) from error

    @application.post(
        "/agents", response_model=Agent, status_code=status.HTTP_201_CREATED
    )
    def create_agent(
        request: AgentRegister,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> Agent:
        try:
            return register_agent(
                session,
                name=request.name,
                role=request.role,
                adapter_type=request.adapter_type,
                delegation_mode=request.delegation_mode,
                capabilities=request.capabilities,
                endpoint=request.endpoint,
                secret_ref=request.secret_ref,
                callback_url=request.callback_url,
                trust_level=request.trust_level,
                timeout_s=request.timeout_s,
                max_retries=request.max_retries,
                config_ref=request.config_ref,
                limitations=request.limitations,
                enabled=request.enabled,
                actor=actor,
            )
        except ServiceError as error:
            raise _translate(error) from error

    @application.get("/agents/{agent_id}", response_model=AgentPublic)
    def read_agent(
        agent_id: str,
        session: Session = Depends(get_session),
    ) -> Agent:
        try:
            return get_agent(session, agent_id)
        except ServiceError as error:
            raise _translate(error) from error

    @application.put("/agents/{agent_id}/enabled", response_model=Agent)
    def update_agent_enabled(
        agent_id: str,
        request: AgentEnabledUpdate,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> Agent:
        try:
            return set_agent_enabled(session, agent_id, request.enabled, actor=actor)
        except ServiceError as error:
            raise _translate(error) from error

    # --- V4 Agent Platform: self-registration, discovery, relay (#99/#101) ---
    # Inbound surface for the unified Agent platform:
    #   * bootstrap (strict single-use CREATE via a scoped owner-signed token)
    #   * self-update (agent-authenticated, scope-locked, last-writer-wins)
    #   * capability discovery (catalog + filtered agent listing)
    #   * Agent Relay ingest (agent-authenticated work-log intake, #77 port)
    # Trust boundaries are enforced entirely by the security dependencies and
    # the registry primitives; routes never mint an ActorContext and perform at
    # most a single equality scope check at the bootstrap edge.

    @application.post(
        "/agents/bootstrap",
        response_model=AgentBootstrapResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def bootstrap_agent(
        payload: AgentSelfRegister,
        claims: BootstrapClaims = Depends(authenticate_bootstrap_token),
        session: Session = Depends(get_session),
    ) -> AgentBootstrapResponse:
        """Strict single-use agent CREATE via a scoped bootstrap token (plan §3).

        ``authenticate_bootstrap_token`` verifies the owner signature + expiry and
        returns the claims ``(platform, external_ref, jti)``. The request body
        MUST declare the same identity -- a mismatch is a scope violation and is
        rejected with 401 (zero side effects, no audit). Single-use and tuple
        uniqueness are enforced by ``create_agent_via_bootstrap`` (401 on a
        consumed token or a concurrent collision). The one-time credential is
        returned ONLY here.
        """
        if payload.platform != claims.platform or payload.external_ref != claims.external_ref:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="bootstrap scope mismatch: token identity != body identity",
                headers=_AGENT_UNAUTH_HEADERS,
            )
        try:
            agent, credential = create_agent_via_bootstrap(
                session,
                platform=claims.platform,
                external_ref=claims.external_ref,
                jti=claims.jti,
                name=payload.name,
                role=payload.role,
                adapter_type=payload.adapter_type,
                delegation_mode=payload.delegation_mode,
                capabilities=payload.capabilities,
                endpoint=payload.endpoint,
                callback_url=payload.callback_url,
                config_ref=payload.config_ref,
                limitations=payload.limitations,
                timeout_s=payload.timeout_s,
                max_retries=payload.max_retries,
            )
        except SecretStoreUnavailable:
            raise _secret_unavailable() from None
        except ServiceError as error:
            raise _translate(error) from error
        return AgentBootstrapResponse(
            id=agent.id,
            platform=agent.platform,
            external_ref=agent.external_ref,
            name=agent.name,
            role=agent.role,
            adapter_type=agent.adapter_type.value,
            status=agent.status.value,
            capabilities=agent.capabilities or [],
            credential=credential,
        )

    @application.put(
        "/agents/self",
        response_model=AgentRegistrationResponse,
        status_code=status.HTTP_200_OK,
    )
    def agent_self_update(
        payload: AgentSelfRegister,
        actor: ActorContext = Depends(authenticate_agent),
        session: Session = Depends(get_session),
    ) -> AgentRegistrationResponse:
        """Agent-authenticated self-update of the agent's OWN identity (plan §3.2).

        The actor is resolved from the bearer credential (``authenticate_agent``)
        and locked to its own ``(platform, external_ref)`` by ``upsert_agent``. A
        missing / non-agent credential is 401; a scope mismatch is 422. Concurrent
        same-agent updates are serialized (``BEGIN IMMEDIATE``) so the read-modify-
        write is atomic and last-writer-wins.
        """
        try:
            agent = upsert_agent(
                session,
                actor=actor,
                platform=payload.platform,
                external_ref=payload.external_ref,
                name=payload.name,
                role=payload.role,
                adapter_type=payload.adapter_type,
                delegation_mode=payload.delegation_mode,
                capabilities=payload.capabilities,
                endpoint=payload.endpoint,
                callback_url=payload.callback_url,
                config_ref=payload.config_ref,
                limitations=payload.limitations,
                timeout_s=payload.timeout_s,
                max_retries=payload.max_retries,
            )
        except SecretStoreUnavailable:
            raise _secret_unavailable() from None
        except ServiceError as error:
            raise _translate(error) from error
        return AgentRegistrationResponse(
            id=agent.id,
            platform=agent.platform,
            external_ref=agent.external_ref,
            name=agent.name,
            role=agent.role,
            adapter_type=agent.adapter_type.value,
            status=agent.status.value,
            capabilities=agent.capabilities or [],
        )

    @application.post(
        "/agents/{agent_id}/rotate-credential",
        response_model=dict,
        status_code=status.HTTP_200_OK,
    )
    def rotate_agent_credential(
        agent_id: str,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> dict:
        """Owner-only: rotate an agent's self-update credential, invalidating the old.

        Issues a fresh bearer secret through the external secret store; the new
        credential is returned once and must be delivered to the agent out of band.
        A consumed bootstrap token stays 401 forever -- rotation is the owner
        recovery path, not a second bootstrap.
        """
        try:
            credential = rotate_credential(session, agent_id, actor=actor)
        except SecretStoreUnavailable:
            raise _secret_unavailable() from None
        except ServiceError as error:
            raise _translate(error) from error
        return {"agent_id": agent_id, "credential": credential}

    @application.get("/capabilities", response_model=list[Capability])
    def list_capability_catalog(
        session: Session = Depends(get_session),
    ) -> list[Capability]:
        """Read-only capability discovery (plan §4): the full capability catalog."""
        return list_capabilities(session)

    @application.post(
        "/relay/work-logs",
        response_model=dict,
        status_code=status.HTTP_201_CREATED,
    )
    def relay_work_log_endpoint(
        payload: WorkLogSubmit,
        response: Response,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        actor: ActorContext = Depends(authenticate_agent),
        session: Session = Depends(get_session),
    ) -> dict:
        """Agent Relay ingest: agent-authenticated UNVERIFIED work-log intake (#77).

        Reuses the owner's ``submit_work_log`` creation machinery but scopes
        idempotency per agent (``scope="agent:<agent_id>"``) and substitutes the
        authenticated agent identity as provenance (the payload's
        ``produced_by_agent_id`` is ignored). The relay NEVER attests -- the
        owner attests manually. The ``Idempotency-Key`` header is REQUIRED
        (missing -> 422); a replay returns 200, a conflicting payload -> 409.
        """
        agent = session.get(Agent, actor.agent_id)
        if agent is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="agent not found"
            )
        try:
            artifact, created = relay_work_log(
                session,
                payload=payload,
                idempotency_key=idempotency_key,
                actor=actor,
                agent=agent,
            )
        except ServiceError as error:
            raise _translate(error) from error
        if not created:
            response.status_code = status.HTTP_200_OK
        return {
            "id": artifact.id,
            "project_id": artifact.project_id,
            "task_id": artifact.task_id,
            "review_status": artifact.review_status.value,
            "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
        }

    # --- Minimal owner console (server-rendered HTML; no separate frontend) ---
    # Reuses the SAME service layer as the JSON endpoints above:
    #   POST /owner/launch      -> launch_campaign (behind POST /owner/campaigns)
    #   GET  /owner/board/{id}  -> get_board_service (behind GET /owner/campaigns/{id})
    # No campaign-launch domain logic is duplicated in this UI layer.

    @application.get("/owner", response_class=HTMLResponse)
    def owner_home(
        request: Request,
        session: Session = Depends(get_session),
        _owner: ActorContext = Depends(authenticate_owner),
    ) -> HTMLResponse:
        """Launch form. A fresh idempotency key is embedded so a double-click or
        retry reuses the same key and never creates a duplicate campaign."""
        last_campaign_id = request.cookies.get("aios_last_campaign")
        return HTMLResponse(owner_home_html(idem=new_id("idem"), last_campaign_id=last_campaign_id))

    @application.get("/owner/agents", response_class=HTMLResponse)
    def owner_agents(
        request: Request,
        session: Session = Depends(get_session),
        _owner: ActorContext = Depends(authenticate_owner),
    ) -> HTMLResponse:
        """Agent registry console: list + registration form (#57, #61)."""
        agents = list_agents(session)
        return HTMLResponse(owner_agents_html(agents))

    @application.post("/owner/agents/register", response_class=HTMLResponse, response_model=None)
    def owner_agent_register(
        request: Request,
        name: str | None = Form(None),
        role: str | None = Form(None),
        adapter_type: str | None = Form(None),
        delegation_mode: str | None = Form(None),
        capabilities: str | None = Form(None),
        endpoint: str | None = Form(None),
        secret_ref: str | None = Form(None),
        callback_url: str | None = Form(None),
        trust_level: str | None = Form(None),
        timeout_s: str | None = Form("300"),
        max_retries: str | None = Form("3"),
        limitations: str | None = Form(None),
        enabled: str | None = Form(None),
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> HTMLResponse | RedirectResponse:
        def _show_error(msg: str) -> HTMLResponse:
            return HTMLResponse(
                owner_agents_html(list_agents(session), error=msg), status_code=400
            )

        if not name or not name.strip() or not role or not role.strip():
            return _show_error("agent 名称与角色都不能为空。")
        if not adapter_type or not adapter_type.strip():
            return _show_error("必须选择适配器类型。")
        try:
            ts = float(timeout_s or "300")
            mr = int(max_retries or "3")
        except ValueError:
            return _show_error("超时 / 重试次数必须为数字。")
        caps = [c.strip() for c in (capabilities or "").split(",") if c.strip()]
        lims = [lim.strip() for lim in (limitations or "").split(",") if lim.strip()]
        try:
            register_agent(
                session,
                name=name,
                role=role,
                adapter_type=adapter_type,
                delegation_mode=delegation_mode or None,
                capabilities=caps,
                endpoint=endpoint or None,
                secret_ref=secret_ref or None,
                callback_url=callback_url or None,
                trust_level=trust_level or None,
                timeout_s=ts,
                max_retries=mr,
                limitations=lims,
                enabled=enabled is not None,
                actor=actor,
            )
        except ServiceError as error:
            return _show_error(error.detail)
        return RedirectResponse(url="/owner/agents", status_code=303)

    @application.post(
        "/owner/agents/{agent_id}/toggle", response_class=HTMLResponse, response_model=None
    )
    def owner_agent_toggle(
        agent_id: str,
        request: Request,
        enabled: str | None = Form(None),
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> HTMLResponse | RedirectResponse:
        try:
            set_agent_enabled(session, agent_id, enabled is not None, actor=actor)
        except ServiceError as error:
            return HTMLResponse(
                owner_agents_html(list_agents(session), error=error.detail), status_code=400
            )
        return RedirectResponse(url="/owner/agents", status_code=303)

    @application.post("/owner/launch", response_class=HTMLResponse, response_model=None)
    def owner_launch(
        request: Request,
        name: str | None = Form(None),
        objective: str | None = Form(None),
        idem: str | None = Form(None),
        session: Session = Depends(get_session),
        _owner: ActorContext = Depends(authenticate_owner),
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
        _owner: ActorContext = Depends(authenticate_owner),
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
        actor: ActorContext = Depends(authenticate_owner),
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
                decide_publish_gate(
                    session, task.project_id, decision_enum, rationale, actor=actor
                )
            else:
                approval = ensure_pending_approval(
                    session,
                    project_id=task.project_id,
                    task_id=task_id,
                    action_type="owner_gate",
                )
                decide_approval(session, approval.id, decision_enum, rationale, actor=actor)
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
        actor: ActorContext = Depends(authenticate_owner),
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
            request_revision(session, task_id, feedback, actor=actor)
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
        _owner: ActorContext = Depends(authenticate_owner),
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
        _owner: ActorContext = Depends(authenticate_owner),
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
        actor: ActorContext = Depends(authenticate_owner),
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
            decide_publish_gate(session, task.project_id, decision_enum, rationale, actor=actor)
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
        actor: ActorContext = Depends(authenticate_owner),
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
            project_id = None
            if effective_scope != "company":
                source = session.get(Artifact, artifact_id.strip())
                project_id = source.project_id if source is not None else None
            KnowledgeService(session).submit_candidate(
                artifact_id.strip(),
                statement.strip(),
                project_id=project_id,
                tags=None,
                actor=actor,
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
        rationale: str | None = Form(None),
        series_id: str | None = Form(None),
        version: str | None = Form(None),
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
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
                rationale.strip(),
                actor=actor,
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
