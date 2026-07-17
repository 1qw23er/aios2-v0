from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


def now_utc() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class ProjectStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    BACKLOG = "backlog"
    READY = "ready"
    RUNNING = "running"
    WAITING_EXTERNAL = "waiting_external"
    REVIEW = "review"
    APPROVED = "approved"
    REJECTED = "rejected"
    DONE = "done"
    FAILED = "failed"


class AdapterType(StrEnum):
    API = "api"
    CLI = "cli"
    EXTERNAL = "external"
    MODEL = "model"


class ArtifactType(StrEnum):
    MARKDOWN = "markdown"
    JSON = "json"
    IMAGE = "image"
    VIDEO = "video"
    DATASET = "dataset"
    LINK = "link"


class ArtifactReviewStatus(StrEnum):
    UNVERIFIED = "unverified"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewedFactStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class KnowledgeCandidateStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


class KnowledgeReviewDecisionValue(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class KnowledgeFactStatus(StrEnum):
    APPROVED = "approved"
    SUPERSEDED = "superseded"
    INACTIVE = "inactive"



class DecisionStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class RiskLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class EventStatus(StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"


class AgentStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    MAINTENANCE = "maintenance"


class RoutingMode(StrEnum):
    FIXED = "fixed"
    PREFERRED_WITH_FALLBACK = "preferred_with_fallback"
    BEST_AVAILABLE = "best_available"
    MANUAL = "manual"


class Project(SQLModel, table=True):
    __tablename__ = "project"

    id: str = Field(default_factory=lambda: new_id("prj"), primary_key=True)
    name: str
    objective: str
    description: str = ""
    status: ProjectStatus = ProjectStatus.PROPOSED
    owner: str = "human_ceo"
    budget_limit: float = 0.0
    success_metrics: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class Agent(SQLModel, table=True):
    __tablename__ = "agent"

    id: str = Field(default_factory=lambda: new_id("agt"), primary_key=True)
    name: str
    role: str
    adapter_type: AdapterType
    capabilities: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    permissions: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    cost_policy: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    endpoint: str | None = None
    config_ref: str | None = None
    enabled: bool = True
    limitations: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: AgentStatus = Field(default=AgentStatus.AVAILABLE, index=True)


class Capability(SQLModel, table=True):
    __tablename__ = "capability"

    id: str = Field(default_factory=lambda: new_id("cap"), primary_key=True)
    name: str = Field(unique=True, index=True)
    description: str = ""


class AgentCapability(SQLModel, table=True):
    __tablename__ = "agent_capability"

    agent_id: str = Field(foreign_key="agent.id", primary_key=True)
    capability_id: str = Field(foreign_key="capability.id", primary_key=True)
    priority: int = Field(default=50, ge=1, le=100)
    enabled: bool = True


class Task(SQLModel, table=True):
    __tablename__ = "task"

    id: str = Field(default_factory=lambda: new_id("tsk"), primary_key=True)
    project_id: str | None = Field(default=None, foreign_key="project.id", index=True)
    title: str
    description: str
    status: TaskStatus = TaskStatus.BACKLOG
    assigned_agent_id: str | None = Field(default=None, foreign_key="agent.id", index=True)
    preferred_agent_id: str | None = Field(default=None, foreign_key="agent.id", index=True)
    required_capabilities: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    routing_mode: RoutingMode = Field(default=RoutingMode.FIXED, index=True)
    adapter_type: AdapterType = AdapterType.EXTERNAL
    input_context_refs: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    acceptance_criteria: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    output_schema: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    depends_on: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    estimated_cost: float = 0.0
    actual_cost: float = 0.0
    retry_count: int = 0
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class Artifact(SQLModel, table=True):
    __tablename__ = "artifact"

    id: str = Field(default_factory=lambda: new_id("art"), primary_key=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    task_id: str | None = Field(default=None, foreign_key="task.id", index=True)
    type: ArtifactType
    uri: str
    checksum: str
    review_status: ArtifactReviewStatus = Field(default=ArtifactReviewStatus.UNVERIFIED, index=True)
    external_result_id: str | None = Field(default=None, unique=True, index=True)
    result_checksum: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSON))
    created_at: datetime = Field(default_factory=now_utc)


class Approval(SQLModel, table=True):
    __tablename__ = "approval"

    id: str = Field(default_factory=lambda: new_id("apr"), primary_key=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    task_id: str | None = Field(default=None, foreign_key="task.id", index=True)
    action_type: str
    risk_level: RiskLevel
    status: ApprovalStatus = Field(default=ApprovalStatus.PENDING, index=True)
    requested_at: datetime = Field(default_factory=now_utc)
    decided_at: datetime | None = None
    rationale: str | None = None


class Event(SQLModel, table=True):
    __tablename__ = "event"

    id: str = Field(default_factory=lambda: new_id("evt"), primary_key=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    task_id: str | None = Field(default=None, foreign_key="task.id", index=True)
    type: str = Field(index=True)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    idempotency_key: str = Field(unique=True, index=True)
    status: EventStatus = Field(default=EventStatus.PENDING, index=True)
    attempt_count: int = 0
    last_error: str | None = None
    processed_at: datetime | None = None
    created_at: datetime = Field(default_factory=now_utc)


class ExecutionAssignment(SQLModel, table=True):
    __tablename__ = "execution_assignment"

    id: str = Field(default_factory=lambda: new_id("asn"), primary_key=True)
    task_id: str = Field(foreign_key="task.id", index=True)
    selected_agent_id: str = Field(foreign_key="agent.id", index=True)
    routing_reason: str
    fallback_used: bool = False
    idempotency_key: str = Field(unique=True, index=True)
    created_at: datetime = Field(default_factory=now_utc)


class ReviewedFact(SQLModel, table=True):
    __tablename__ = "reviewed_fact"

    id: str = Field(default_factory=lambda: new_id("fact"), primary_key=True)
    artifact_id: str = Field(foreign_key="artifact.id", index=True)
    statement: str
    status: ReviewedFactStatus = Field(default=ReviewedFactStatus.PENDING, index=True)
    reviewer: str
    reviewed_at: datetime | None = None
    created_at: datetime = Field(default_factory=now_utc)


class KnowledgeCandidate(SQLModel, table=True):
    __tablename__ = "knowledge_candidate"

    id: str = Field(default_factory=lambda: new_id("kcand"), primary_key=True)
    artifact_id: str = Field(foreign_key="artifact.id", index=True)
    project_id: str | None = Field(default=None, foreign_key="project.id", index=True)
    statement: str
    status: KnowledgeCandidateStatus = Field(default=KnowledgeCandidateStatus.DRAFT, index=True)
    submitted_by: str
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class KnowledgeReviewDecision(SQLModel, table=True):
    __tablename__ = "knowledge_review_decision"
    __table_args__ = (UniqueConstraint("candidate_id", name="uq_knowledge_review_candidate"),)

    id: str = Field(default_factory=lambda: new_id("krev"), primary_key=True)
    candidate_id: str = Field(foreign_key="knowledge_candidate.id", index=True)
    decision: KnowledgeReviewDecisionValue
    reviewer: str
    rationale: str
    reviewed_at: datetime = Field(default_factory=now_utc)


class KnowledgeFact(SQLModel, table=True):
    __tablename__ = "knowledge_fact"
    __table_args__ = (
        UniqueConstraint("series_id", "version", name="uq_knowledge_fact_series_version"),
        UniqueConstraint("source_candidate_id", name="uq_knowledge_fact_source_candidate"),
        UniqueConstraint("review_decision_id", name="uq_knowledge_fact_review_decision"),
        UniqueConstraint("supersedes_fact_id", name="uq_knowledge_fact_supersedes"),
    )

    id: str = Field(default_factory=lambda: new_id("kfact"), primary_key=True)
    series_id: str = Field(index=True)
    version: int = Field(ge=1)
    project_id: str | None = Field(default=None, foreign_key="project.id", index=True)
    statement: str
    status: KnowledgeFactStatus = Field(default=KnowledgeFactStatus.APPROVED, index=True)
    source_candidate_id: str = Field(foreign_key="knowledge_candidate.id", index=True)
    source_artifact_id: str = Field(foreign_key="artifact.id", index=True)
    review_decision_id: str = Field(foreign_key="knowledge_review_decision.id", index=True)
    supersedes_fact_id: str | None = Field(
        default=None, foreign_key="knowledge_fact.id", index=True
    )
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class Decision(SQLModel, table=True):
    __tablename__ = "decision"
    __table_args__ = (UniqueConstraint("series_id", "version", name="uq_decision_series_version"),)

    id: str = Field(default_factory=lambda: new_id("dec"), primary_key=True)
    series_id: str = Field(index=True)
    project_id: str | None = Field(default=None, foreign_key="project.id", index=True)
    title: str
    content: str
    status: DecisionStatus = Field(default=DecisionStatus.DRAFT, index=True)
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class Policy(SQLModel, table=True):
    __tablename__ = "policy"
    __table_args__ = (UniqueConstraint("series_id", "version", name="uq_policy_series_version"),)

    id: str = Field(default_factory=lambda: new_id("pol"), primary_key=True)
    series_id: str = Field(index=True)
    project_id: str | None = Field(default=None, foreign_key="project.id", index=True)
    name: str
    content: str
    enabled: bool = True
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class TaskContext(SQLModel, table=True):
    __tablename__ = "task_context"
    __table_args__ = (
        UniqueConstraint("task_id", "context_hash", name="uq_task_context_task_hash"),
    )

    id: str = Field(default_factory=lambda: new_id("ctx"), primary_key=True)
    task_id: str = Field(foreign_key="task.id", index=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    assigned_agent_id: str | None = Field(default=None, foreign_key="agent.id", index=True)
    objective: str
    instructions: str
    acceptance_criteria: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    project_context: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    dependency_outputs: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    approved_facts: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    relevant_decisions: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    applicable_policies: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    agent_profile: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    source_references: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    context_hash: str = Field(index=True)
    created_at: datetime = Field(default_factory=now_utc)
