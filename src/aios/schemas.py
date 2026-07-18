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

    Mirrors ``KnowledgeService.submit_candidate``; ``scope`` is "project" (reusable
    only inside the source campaign) or "company" (reusable by every campaign). The
    source-campaign provenance is recorded by the service; this schema only carries
    the owner's effective-scope choice.
    """

    artifact_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    scope: str = "project"


class KnowledgeReviewRequest(BaseModel):
    """Owner review of a knowledge candidate -> versioned ``KnowledgeFact``.

    Mirrors ``KnowledgeService.review_candidate``. ``series_id``/``version`` are
    required only on APPROVE (the service enforces positive version + series scoping);
    REJECT needs only ``decision`` + ``rationale``.
    """

    decision: KnowledgeReviewDecisionValue
    reviewer: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    series_id: str | None = None
    version: int | None = None
    supersedes_fact_id: str | None = None


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
