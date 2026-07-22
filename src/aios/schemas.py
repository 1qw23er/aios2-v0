from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aios.models import (
    AdapterType,
    ApprovalStatus,
    ArtifactReviewStatus,
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

