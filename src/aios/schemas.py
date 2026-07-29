from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aios.models import (
    AdapterType,
    AgentStatus,
    AgentTrustLevel,
    ApprovalStatus,
    ArtifactReviewStatus,
    DelegationMode,
    KnowledgeReviewDecisionValue,
    RiskLevel,
    RoutingMode,
)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    description: str = ""
    owner: str = "human_ceo"
    budget_limit: float = Field(default=0.0, ge=0)
    success_metrics: list[str] = Field(default_factory=list)


class TaskCreate(BaseModel):
    project_id: str
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    assigned_agent_id: str | None = None
    preferred_agent_id: str | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    routing_mode: RoutingMode = RoutingMode.FIXED
    adapter_type: AdapterType = AdapterType.EXTERNAL
    input_context_refs: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    estimated_cost: float = Field(default=0.0, ge=0)


class ApprovalCreate(BaseModel):
    project_id: str
    task_id: str | None = None
    action_type: str = Field(min_length=1)
    risk_level: RiskLevel
    rationale: str | None = None


class ApprovalDecision(BaseModel):
    decision: ApprovalStatus
    rationale: str | None = None


class RevisionRequest(BaseModel):
    feedback: str = Field(min_length=1)


class KnowledgeCandidateCreate(BaseModel):
    """Owner submits a reusable knowledge candidate from an APPROVED source artifact.

    Mirrors ``KnowledgeService.submit_candidate``. ``scope`` is "project" (reusable
    only inside the source campaign) or "company" (reusable by every campaign); it
    selects the effective ``project_id`` the service records. The submitter identity
    is NEVER taken from the request -- it is always derived from the trusted owner
    actor. ``tags`` is the optional initial capability classification (canonical
    tags only); if omitted the candidate carries the legacy sentinel until the owner
    classifies it.
    """

    artifact_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    scope: str = "project"
    tags: list[str] | None = None


class KnowledgeReviewRequest(BaseModel):
    """Owner review of a knowledge candidate -> versioned ``KnowledgeFact``.

    Mirrors ``KnowledgeService.review_candidate``. The reviewer identity is NEVER
    taken from the request -- it is always derived from the trusted owner actor.
    ``series_id``/``version`` are required only on APPROVE (the service enforces
    positive version + series scoping); REJECT needs only ``decision`` + ``rationale``.
    """

    decision: KnowledgeReviewDecisionValue
    rationale: str = Field(min_length=1)
    series_id: str | None = None
    version: int | None = None
    supersedes_fact_id: str | None = None


class KnowledgeClassifyRequest(BaseModel):
    """Owner-only classification of a legacy sentinel candidate/fact -> canonical tags.

    The owner supplies the canonical capability tags; the service enforces the
    one-time sentinel -> canonical transition and rejects any already-classified
    target.
    """

    tags: list[str] = Field(min_length=1)


class WorkLogSubmit(BaseModel):
    """Owner submits an AI-worker work log (#88 plan §9).

    The submitter identity is NEVER taken from this payload -- it is always the
    trusted owner actor injected by ``authenticate_owner``. ``produced_by_agent_id``
    is provenance only (plan §6): it must be proven against a durable
    ``ExecutionAssignment`` (``execution_assignment_id`` required on routed tasks)
    and never becomes the actor. The ``Idempotency-Key`` header is REQUIRED and is
    handled at the endpoint (plan §5), not in this body.
    """

    project_id: str = Field(min_length=1)
    report_type: str = Field(min_length=1)
    what_done: str = Field(min_length=1)
    why: str = Field(min_length=1)
    problem: str = Field(min_length=1)
    solution: str = Field(min_length=1)
    new_knowledge: str = Field(min_length=1)
    task_ref: str | None = None
    produced_by_agent_id: str | None = None
    execution_assignment_id: str | None = None
    content_value: str | None = None
    should_enter_kb: bool = False
    content_angle: str | None = None
    source_platform: str | None = None  # V2 (#92): collection source tag; metadata only


class WorkLogAttest(BaseModel):
    """Optional KB-eligibility override at attest time (#92 plan §6).

    Both fields are optional. ``should_enter_kb`` / ``content_value`` are
    decided by the owner during the human attestation action; omitting them
    keeps the submitted metadata values (backward compatible with #88).
    """

    should_enter_kb: bool | None = None
    content_value: str | None = None


class ArtifactReviewUpdate(BaseModel):
    review_status: ArtifactReviewStatus


class ModelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class BoardRead(BaseModel):
    project: dict
    tasks_by_status: dict[str, list[dict]]
    pending_approvals: list[dict]


class CampaignLaunchResult(BaseModel):
    """Response for ``POST /owner/campaigns``.

    A human-readable summary of a launched V1 campaign: the created Project, the
    T1-T9 task graph (with status + assigned department), and an owner-facing message.
    """

    model_config = ConfigDict(from_attributes=True)

    project_id: str
    project_status: str
    task_count: int
    tasks: list[dict[str, Any]]
    message: str


class OrchestratorProcessResult(BaseModel):
    """Response model for ``POST /orchestrator/process``.

    ``activated_task_ids`` is invocation-scoped: it lists only the tasks this
    specific call activated, never a global READY-set snapshot.
    """

    model_config = ConfigDict(from_attributes=True)

    processed_events: int
    activated_task_ids: list[str]


class AgentRegister(BaseModel):
    """Owner registers a new agent in the DB-backed registry (#57, #61).

    Only an opaque ``secret_ref`` handle is accepted — never a raw secret value.
    """

    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    adapter_type: str = Field(min_length=1)
    delegation_mode: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    endpoint: str | None = None
    secret_ref: str | None = None
    callback_url: str | None = None
    trust_level: str = "internal"
    timeout_s: float = 300.0
    max_retries: int = 3
    config_ref: str | None = None
    limitations: list[str] = Field(default_factory=list)
    enabled: bool = True


class AgentEnabledUpdate(BaseModel):
    enabled: bool


class AgentSelfRegister(BaseModel):
    """Agent self-registration payload (V4, #99/#101).

    ``platform`` + ``external_ref`` form the immutable identity tuple (the
    idempotency key for registration). The submitter is NEVER taken from this
    body: for bootstrap it is the scoped token, for self-update it is the
    authenticated agent actor. ``secret_ref`` is deliberately absent -- only an
    opaque handle is ever stored, and credentials are issued out of band.
    """

    platform: str = Field(min_length=1)
    external_ref: str = Field(min_length=1)
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    adapter_type: str = Field(min_length=1)
    delegation_mode: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    endpoint: str | None = None
    callback_url: str | None = None
    config_ref: str | None = None
    limitations: list[str] = Field(default_factory=list)
    timeout_s: float = 300.0
    max_retries: int = 3


class AgentRegistrationResponse(BaseModel):
    """Safe read model for a self-registered agent (V4, #99/#101).

    NEVER includes ``secret_ref`` (credential isolation, plan §0.6). Only
    scheduling-visible fields are exposed.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    platform: str | None = None
    external_ref: str | None = None
    name: str
    role: str
    adapter_type: str
    status: str
    capabilities: list[str] = Field(default_factory=list)


class AgentBootstrapResponse(AgentRegistrationResponse):
    """Bootstrap response -- additionally carries the ONE-TIME credential.

    The ``credential`` is the only place a plaintext bearer secret is returned;
    it is never persisted and never appears in any subsequent response or audit.
    """

    credential: str


class AgentPublic(BaseModel):
    """Safe public read model for any registry agent (V4, #99/#101).

    Used by the read/list registry endpoints (``GET /agents``,
    ``GET /agents/{id}``). Deliberately EXCLUDES ``secret_ref`` and ``config_ref``
    so an opaque credential handle or config pointer can never leak through the
    API (credential isolation, plan §0.6). Enum fields are kept as their enum
    types so the ORM object validates directly and serializes to its value.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    platform: str | None = None
    external_ref: str | None = None
    name: str
    role: str
    adapter_type: AdapterType
    delegation_mode: DelegationMode | None = None
    status: AgentStatus
    enabled: bool
    trust_level: AgentTrustLevel
    capabilities: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    endpoint: str | None = None
    callback_url: str | None = None
    timeout_s: float = 300.0
    max_retries: int = 3


class ReviewPolicyCreate(BaseModel):
    """Creation payload for a ReviewPolicy (D3 scaffolding, #72).

    Reuses the existing ReviewPolicy model; no new migration. Server-side
    validation (in ``aios.review.create_review_policy``) enforces the meaningful
    fields.     Equivalent duplicate requests are idempotent (return the existing
    policy); conflicting configs return 409.

    NOTE: no field here carries an owner credential. The endpoint is
    owner-authenticated at the route level via ``authenticate_owner``; this
    payload is never an access-control boundary.
    """

    name: str
    applies_to: str = ""
    dimensions: list[str] = Field(default_factory=list)
    brand_policy_id: str | None = None
    required_reviewer_trust: str = "verified_external"
    required_capabilities: list[str] = Field(default_factory=list)
    max_revisions: int = 2
    required_reviewers: int = 2
    enabled: bool = True
    project_id: str | None = None

