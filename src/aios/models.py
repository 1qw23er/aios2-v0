from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    false,
    text,
)
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


class DelegationMode(StrEnum):
    """How a department agent is reached (Agent Interoperability Gateway, #57).

    - remote_api: HTTP endpoint, async submit + status poll or callback.
    - a2a: remote agent-to-agent task collaboration protocol.
    - mcp: MCP worker bridge (tool/context connectivity, sync tool call).
    - workstation: external closed agent via local export/import package.

    MCP and A2A are deliberately NOT interchangeable: MCP is primarily
    tool/context connectivity; A2A is remote agent task collaboration.
    """

    REMOTE_API = "remote_api"
    A2A = "a2a"
    MCP = "mcp"
    WORKSTATION = "workstation"


class DelegatedRunStatus(StrEnum):
    """Lifecycle of one remote execution delegated to an external agent (#57)."""

    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ArtifactType(StrEnum):
    MARKDOWN = "markdown"
    JSON = "json"
    IMAGE = "image"
    VIDEO = "video"
    DATASET = "dataset"
    LINK = "link"
    # Work-log & knowledge-capture system (#88): a structured work report
    # submitted by an AI employee. The 7 reporting fields live in
    # metadata_json; the body markdown lives at ``uri``. Pure code addition --
    # StrEnum values are stored as plain VARCHAR, so no migration is needed.
    WORK_LOG = "work_log"
    # Personal-IP content & monetization workflow (#108-A): a content draft
    # (topic / outline / body / conversion anchors / review metrics). Pure code
    # addition -- StrEnum values are stored as plain VARCHAR, so no migration is
    # needed.
    CONTENT_DRAFT = "content_draft"
    # Usage-feedback loop (V1.2-C, #110): a user/owner-reported product feedback
    # item (original text / scenario / expected outcome / risk tags / suggested
    # solution). Business fields live in ``metadata_json``; the original text
    # lives at ``uri``; the content checksum covers the A-zone fields (plan
    # §1.3). Pure code addition -- StrEnum values are stored as plain VARCHAR,
    # so no Alembic migration is required (single head ``20260730_0001``).
    FEEDBACK = "feedback"


class ArtifactReviewStatus(StrEnum):
    UNVERIFIED = "unverified"
    APPROVED = "approved"
    REJECTED = "rejected"
    # Independent Review Protocol (#64): reviewer returned NEEDS_REVISION -> the
    # producer re-runs the task (revision loop) producing a new Artifact.
    NEEDS_REVISION = "needs_revision"
    # Owner-gate intermediate (#69/C1): all required reviewers PASSED, aggregation
    # flipped the review gate to PASSED and left a pending Owner Approval. The
    # artifact is NOT yet APPROVED -- only an explicit owner action may promote it
    # to APPROVED. AI reviewers can never substitute for the owner final approval.
    REVIEW_PASSED = "review_passed"


# --- Independent Review Protocol (#64) ---
class ReviewDimension(StrEnum):
    """One axis a reviewer may evaluate an Artifact against."""

    FACT_CORRECTNESS = "fact_correctness"
    ACCEPTANCE_CRITERIA = "acceptance_criteria"
    BRAND_STRATEGY = "brand_strategy"
    RISK = "risk"


class ReviewVerdict(StrEnum):
    """Per-dimension verdict."""

    PASS = "pass"
    FAIL = "fail"
    NEEDS_REVISION = "needs_revision"


class ReviewOverall(StrEnum):
    """Aggregate outcome of a ReviewResult."""

    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVISION = "needs_revision"


class ReviewReviewerType(StrEnum):
    """Who/what produced the review (decision #2: separate reviewer identity)."""

    AGENT = "agent"
    USER = "user"


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


class AgentTrustLevel(StrEnum):
    """Single trust axis for the Agent Interoperability Gateway (#104).

    NOT a generic permission framework — merely one dimension used at the
    delegation boundary to decide whether an agent may be used for external
    delegation, and with what capability ceiling.

    - internal: AIOS-owned department agent; full trust.
    - verified_external: a vetted external/closed-source agent allowed to run
      delegated tasks, but it can never mutate internal workflow state (that
      restriction is structural, not permission-based).
    - experimental: unvetted external agent. BLOCKED from delegation until
      promoted, so a misconfigured/unknown agent cannot silently execute work.
    """

    INTERNAL = "internal"
    VERIFIED_EXTERNAL = "verified_external"
    EXPERIMENTAL = "experimental"


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
    # Agent Interoperability Gateway hardening (#104): running spend accrued by
    # delegated runs. Compared against ``budget_limit`` to HARD-block remote
    # execution when the project is over budget (0.0 limit = no enforcement).
    budget_used: float = 0.0
    success_metrics: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class Agent(SQLModel, table=True):
    __tablename__ = "agent"

    id: str = Field(default_factory=lambda: new_id("agt"), primary_key=True)
    name: str
    role: str
    adapter_type: AdapterType
    # Agent Interoperability Gateway (#57): how this agent is reached.
    # remote_api / a2a / mcp / workstation. LLMExecutionAdapter-backed agents
    # leave this None (they run in-process via the execution protocol).
    delegation_mode: DelegationMode | None = Field(default=None, index=True)
    capabilities: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    permissions: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    cost_policy: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    endpoint: str | None = None
    # Secret reference handle ONLY (e.g. "secret://hermes-api-key"). The actual
    # secret lives in an external secret store and is NEVER persisted on any
    # TaskContext / Artifact / AuditLog payload. config_ref is reused for the same
    # opaque-reference purpose for non-secret config.
    config_ref: str | None = None
    secret_ref: str | None = Field(default=None, index=True)
    callback_url: str | None = None
    enabled: bool = True
    limitations: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: AgentStatus = Field(default=AgentStatus.AVAILABLE, index=True)
    # Agent Interoperability Gateway hardening (#104): single trust axis used to
    # gate external delegation. Defaults to INTERNAL for pre-existing agents.
    trust_level: AgentTrustLevel = Field(
        default=AgentTrustLevel.INTERNAL, index=True
    )
    # Agent Interoperability Gateway (#57): per-agent delegation tuning.
    #  - timeout_s: per-delegation wall-clock ceiling before the run is marked
    #    EXPIRED and retried / failed (design review v1 §6). Default 300s.
    #  - max_retries: retry attempts on transient failure before TASK FAILED
    #    (reuses the W1 self-heal pattern). Default 3.
    timeout_s: float = 300.0
    max_retries: int = 3
    # Work-log system (#88): which external platform this AI employee lives on
    # (e.g. "chatgpt" | "codex" | "workbuddy" | "hermes" | "coze"). Free-form
    # lowercase string, indexed for per-platform feeds/statistics.
    platform: str | None = Field(default=None, index=True)
    # Opaque external identity reference on that platform (e.g. a bot id or
    # workspace handle). Never interpreted by AIOS; display/debugging only.
    external_ref: str | None = Field(default=None)
    # V4 self-registration (#99/#101): opaque handle recording which scoped
    # bootstrap token claimed this agent row. A row with
    # ``bootstrap_token_ref = :jti`` is the DB-side proof that the token has been
    # *consumed*; it is committed atomically with the agent row. Deliberately
    # has NO dedicated index -- the ``(platform, external_ref)`` partial unique
    # index (migration 20260729_0001) provides tuple uniqueness instead.
    bootstrap_token_ref: str | None = Field(default=None)


class AgentSecret(SQLModel, table=True):
    """Static credential material for one agent's self-update bearer (#103).

    Persisted ONLY by the opt-in ``encrypted_db`` secret-store backend. The
    table holds KEK-derived HMAC tags -- never the plaintext bearer and never
    any reversible ciphertext (issue #103 §4.2):

    * ``token_tag`` -- ``HMAC-SHA256(KEK, token)``, the one-way lookup key.
      Uniqueness is the required access-path index; it cannot be reversed to
      recover the token.
    * ``row_mac`` -- ``HMAC-SHA256(KEK, agent_id || token_tag)``, a
      cryptographic binding of the tag to its owning ``agent_id`` so the tag
      cannot be transplanted to another agent row.

    ``revoked_at`` is ``NULL`` for the single active token and set on revoke
    (kept for forensic history; the strict single-use contract is preserved by
    the bootstrap-token consumption record on ``Agent``).
    """

    __tablename__ = "agent_secret"

    agent_id: str = Field(primary_key=True, foreign_key="agent.id")
    token_tag: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    row_mac: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    created_at: datetime = Field(default_factory=now_utc)
    revoked_at: datetime | None = Field(default=None)


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
    # Structured idempotency key (server-determined identity). Used for the
    # Independent Review Protocol's owner-requested revision dedup (req 1):
    # ``review-revision:{source_artifact_id}:{next_review_round}``. Never derived
    # from title/description/prompt. UNIQUE so duplicate/concurrent requests
    # converge to the same Task via the DB unique constraint (not by title match).
    idempotency_key: str | None = Field(default=None, unique=True, index=True)


class Artifact(SQLModel, table=True):
    __tablename__ = "artifact"

    id: str = Field(default_factory=lambda: new_id("art"), primary_key=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    task_id: str | None = Field(default=None, foreign_key="task.id", index=True)
    # Agent Interoperability Gateway (#57): when produced by a delegated agent,
    # record which adapter/agent produced it and the immutable provenance bundle
    # (remote run id, remote status, usage, model/agent identity, retry attempt).
    adapter_id: str | None = Field(default=None, foreign_key="agent.id", index=True)
    # "execution_protocol" | "delegated:<mode>"
    source: str | None = Field(default=None, index=True)
    provenance_json: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column("provenance", JSON)
    )
    type: ArtifactType
    uri: str
    checksum: str
    review_status: ArtifactReviewStatus = Field(default=ArtifactReviewStatus.UNVERIFIED, index=True)
    # Independent Review Protocol (#64): revision lineage. revision_of points at
    # the Artifact this one revises; revision_count is how many times the producer
    # has re-run for this task (capped by ReviewPolicy.max_revisions).
    # Physical self-referencing FK with ON DELETE SET NULL: if a parent artifact
    # is removed, its children keep their rows but lose the lineage pointer
    # (orphaned) rather than being cascade-deleted. The migration
    # (20260719_0004) creates the matching constraint + index.
    revision_count: int = Field(default=0)
    revision_of: str | None = Field(
        default=None,
        sa_column=Column(
            String,
            ForeignKey("artifact.id", ondelete="SET NULL"),
            index=True,
        ),
    )
    external_result_id: str | None = Field(default=None, unique=True, index=True)
    result_checksum: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSON))
    # Work-log system (#88): single idempotency contract for work-log
    # submission. Storage key = ``work_log:{project_id}:{sha256(client_key)[:32]}``
    # derived from the mandatory ``Idempotency-Key`` header. Uniqueness is
    # enforced by a PARTIAL unique index (WHERE idempotency_key IS NOT NULL)
    # created in migration 20260728_0009 via raw SQL -- deliberately NO
    # unique=True/index=True here, because the artifact table is referenced by
    # the external trigger ``knowledge_candidate_validate_insert`` with a
    # literal ``main.artifact`` name, so schema changes must be raw ALTERs
    # (never batch recreate).
    idempotency_key: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=now_utc)


class DelegatedRun(SQLModel, table=True):
    """One remote execution of a task delegated to an external agent (#57).

    The orchestration/Task services NEVER learn the agent's internal models,
    prompts, memory, or tools. They only see this record + the resulting
    unverified Artifact (which must pass schema validation before task completion).

    Security: ``secret_ref`` is an opaque handle to an external secret store.
    The actual secret is resolved at call time and is NEVER written here, nor into
    TaskContext / Artifact / AuditLog payloads.
    """

    __tablename__ = "delegated_run"

    id: str = Field(default_factory=lambda: new_id("run"), primary_key=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    task_id: str = Field(foreign_key="task.id", index=True)
    agent_id: str = Field(foreign_key="agent.id", index=True)
    delegation_mode: DelegationMode
    # Opaque handle to the external secret store (e.g. "secret://hermes-api-key").
    secret_ref: str | None = Field(default=None, index=True)
    status: DelegatedRunStatus = Field(default=DelegatedRunStatus.SUBMITTED, index=True)
    # Idempotency key: H(task_id, agent_id, attempt). The remote agent honors it so
    # a retried submit never double-executes. Unique per attempt.
    idempotency_key: str = Field(unique=True, index=True)
    attempt: int = Field(default=1, ge=1)
    remote_run_id: str | None = Field(default=None, index=True)
    remote_status: str | None = None
    # Immutable context delivery: the projected, least-privilege context snapshot
    # sent to the agent. Stored so a run is fully auditable and replayable.
    context_ref: str | None = None
    callback_url: str | None = None
    # Cost / usage captured from the agent's return (provenance). In currency units.
    cost: float = 0.0
    usage: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error: str | None = None
    submitted_at: datetime = Field(default_factory=now_utc)
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=now_utc)


class Approval(SQLModel, table=True):
    __tablename__ = "approval"

    id: str = Field(default_factory=lambda: new_id("apr"), primary_key=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    task_id: str | None = Field(default=None, foreign_key="task.id", index=True)
    # Review-gate binding (#69 C5): the exact target artifact + policy + round this
    # Approval governs. An old Approval must never approve a new revision round.
    target_artifact_id: str | None = Field(
        default=None, foreign_key="artifact.id", index=True
    )
    review_policy_id: str | None = Field(
        default=None, foreign_key="review_policy.id", index=True
    )
    review_round: int = Field(default=1)
    action_type: str
    risk_level: RiskLevel
    status: ApprovalStatus = Field(default=ApprovalStatus.PENDING, index=True)
    requested_at: datetime = Field(default_factory=now_utc)
    decided_at: datetime | None = None
    rationale: str | None = None
    # Gate uniqueness (req 4/5): one review-gate Approval per
    # (target, policy, round). An old Approval can never approve a new revision
    # round. ``target_artifact_id``/``review_policy_id`` are nullable (non-gate
    # approvals leave them NULL, which SQLite treats as distinct), so only
    # review-gate rows are constrained.
    __table_args__ = (
        UniqueConstraint(
            "target_artifact_id",
            "review_policy_id",
            "review_round",
            "action_type",
            name="uq_approval_gate_round",
        ),
    )


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


class ReviewPolicy(SQLModel, table=True):
    """Configuration for the Independent Review Protocol (#64) on a task/scenario.

    Decides which dimensions apply, the reviewer trust floor, optional capability
    requirements, the brand Policy to check against, and the revision cap.
    """

    __tablename__ = "review_policy"

    id: str = Field(default_factory=lambda: new_id("rp"), primary_key=True)
    name: str
    # Match key: task tag / scenario, e.g. "editorial". NULL/empty = global default.
    applies_to: str = ""
    # List of ReviewDimension values that apply to this policy.
    dimensions: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # Brand/strategy rule set (reuses existing Policy) checked for BRAND_STRATEGY.
    brand_policy_id: str | None = Field(default=None, foreign_key="policy.id", index=True)
    # Decision #3: trust floor for any reviewer (experimental agents blocked).
    required_reviewer_trust: AgentTrustLevel = Field(default=AgentTrustLevel.VERIFIED_EXTERNAL)
    # Decision #3: optional capability-based extension (e.g. ["fact_research"]).
    required_capabilities: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # Decision #4: revision cap (default 2), configurable per policy.
    max_revisions: int = Field(default=2, ge=0)
    # Required independent reviewers before the artifact may be aggregated to
    # APPROVED. A single reviewer must never approve directly (trust boundary).
    required_reviewers: int = Field(default=2, ge=1)
    enabled: bool = True
    project_id: str | None = Field(default=None, foreign_key="project.id", index=True)


class ReviewResult(SQLModel, table=True):
    """One independent review of an Artifact (#64).

    Reviewer identity is split (decision #2): an AGENT reviewer carries
    ``reviewer_agent_id`` (and ``user_id`` MUST be null); a USER (human)
    reviewer carries ``user_id`` (and ``reviewer_agent_id`` MUST be null).

    Policy traceability (trust boundary #1): every result records the exact
    ``policy_id`` used and a ``policy_hash`` snapshot of that policy's meaningful
    fields at submit time, so historical review meaning never changes when the
    policy row is later edited.

    Idempotency (trust boundary #3): ``idempotency_key`` is the identity hash
    (artifact + reviewer + policy). An identical replay returns the original
    result; a conflicting replay (same identity, different content) is rejected
    with 409.
    """

    __tablename__ = "review_result"

    id: str = Field(default_factory=lambda: new_id("rev"), primary_key=True)
    artifact_id: str = Field(foreign_key="artifact.id", index=True)
    reviewer_type: ReviewReviewerType
    reviewer_agent_id: str | None = Field(default=None, foreign_key="agent.id", index=True)
    user_id: str | None = None
    # Policy traceability: which ReviewPolicy produced this verdict.
    policy_id: str | None = Field(default=None, foreign_key="review_policy.id", index=True)
    # Immutable snapshot hash of the policy's meaningful fields at submit time.
    policy_hash: str | None = None
    # Identity hash (artifact + reviewer + policy). Unique per reviewer verdict.
    idempotency_key: str | None = Field(
        default=None, unique=True, index=True
    )
    # Per-dimension verdicts: [{dim, verdict, evidence, score}].
    dimensions: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    overall: ReviewOverall
    # Quality score assigned to the artifact BY the reviewer (agent or human).
    reviewer_score: float | None = None
    # Human-in-the-loop usefulness feedback. Distinct from reviewer_score and
    # ONLY meaningful for USER (human) reviews; agents must not set it.
    usefulness: float | None = None
    # Binding provenance (#69 C1/C2/C6): which server-bound Review Task produced
    # this verdict, in which round, and against which single assigned dimension.
    # Anchored to the trusted ``review_task_id`` -- never derived from mutable
    # artifact metadata or agent output (idempotency + trust boundary #3/#6).
    # Unique so a Review Task maps to exactly one ReviewResult (1:1 durable link).
    review_task_id: str | None = Field(
        default=None, foreign_key="task.id", index=True, unique=True
    )
    # Provenance (Option A, req 3): the independent Review Artifact that produced
    # this verdict. Persisted DIRECTLY on the result (never via the target
    # Artifact's mutable ``metadata_json["review_result_id"]`` forward-link). The
    # binding runtime path always sets it non-null; the column is UNIQUE so a
    # Review Artifact maps to exactly one ReviewResult -- a durable, immutable,
    # server-owned trace (trust boundary #3/#6).
    review_artifact_id: str | None = Field(
        default=None, foreign_key="artifact.id", index=True, unique=True
    )
    # Server-derived review round (target artifact revision_count + 1). Old-round
    # results must never satisfy a new round (req 3).
    review_round: int = Field(default=1)
    # The single dimension this Review Task was server-bound to submit (req 2).
    review_dimension: str | None = None
    created_at: datetime = Field(default_factory=now_utc)


class ReviewAssignment(SQLModel, table=True):
    """Immutable server-owned binding between a Review Task and its exact target (#69 C1/C2/C6).

    One row per Review Task. This is the single source of truth for the review
    binding -- it replaces the prior (rejected) approach of stashing the binding
    in the target Artifact's mutable ``metadata_json["review_binding"]``.

    Persists the exact:
      * ``review_task_id``   -- the Review Task (PK, 1:1; never client-supplied)
      * ``target_artifact_id`` -- the exact Artifact under review (NOT derived from
        "content task -> latest artifact"; a content task may have many revisions)
      * ``review_policy_id`` -- the policy that governs this review
      * ``review_round``     -- server-derived round (target.revision_count + 1)
      * ``reviewer_agent_id`` -- the trusted reviewer identity (from Task.assigned_agent_id)
      * ``review_dimension``  -- exactly ONE dimension this Task may submit (req 2)

    Because this table is append-only and server-owned, aggregation can verify the
    exact expected Review Tasks / reviewer identities / dimensions / round without
    trusting agent output or mutable metadata.
    """

    __tablename__ = "review_assignment"

    review_task_id: str = Field(foreign_key="task.id", primary_key=True)
    target_artifact_id: str = Field(foreign_key="artifact.id", index=True)
    review_policy_id: str = Field(foreign_key="review_policy.id", index=True)
    review_round: int = Field(default=1)
    reviewer_agent_id: str = Field(foreign_key="agent.id", index=True)
    review_dimension: str
    created_at: datetime = Field(default_factory=now_utc)
    # Binding uniqueness (req 4): identical (target, policy, round, reviewer,
    # dimension) must never be dispatched twice. Enforced at the DB level so
    # concurrent ``dispatch_reviews_for_artifact`` calls converge to the same
    # immutable binding row (the 5-tuple is the durable send-side identity).
    __table_args__ = (
        UniqueConstraint(
            "target_artifact_id",
            "review_policy_id",
            "review_round",
            "reviewer_agent_id",
            "review_dimension",
            name="uq_review_assignment_binding",
        ),
    )


class KnowledgeCandidate(SQLModel, table=True):
    __tablename__ = "knowledge_candidate"

    id: str = Field(default_factory=lambda: new_id("kcand"), primary_key=True)
    artifact_id: str = Field(foreign_key="artifact.id", index=True)
    # Effective knowledge scope: NULL = company-wide, otherwise the owning project.
    project_id: str | None = Field(default=None, foreign_key="project.id", index=True)
    # Provenance: the campaign that produced the source artifact. NEVER NULL, so
    # source-campaign ownership can always be enforced even for company-scoped facts.
    source_project_id: str = Field(foreign_key="project.id", index=True)
    statement: str
    status: KnowledgeCandidateStatus = Field(default=KnowledgeCandidateStatus.DRAFT, index=True)
    # Capability-aware projection tags (controlled vocabulary, see knowledge_tags.py).
    # Immutable after creation except for the one-time sentinel -> canonical
    # transition performed by owner-only classify_candidate_tags().
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # Typed, server-derived submitter identity (never accepted from request input).
    submitted_by_kind: str
    submitted_by_owner_id: str | None = None
    submitted_by_agent_id: str | None = None
    # Derived display string (owner:<id> / agent:<id> / system); immutable.
    submitted_by: str
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class KnowledgeReviewDecision(SQLModel, table=True):
    __tablename__ = "knowledge_review_decision"
    __table_args__ = (UniqueConstraint("candidate_id", name="uq_knowledge_review_candidate"),)

    id: str = Field(default_factory=lambda: new_id("krev"), primary_key=True)
    candidate_id: str = Field(foreign_key="knowledge_candidate.id", index=True)
    decision: KnowledgeReviewDecisionValue
    # Typed, server-derived reviewer identity (never accepted from request input).
    reviewer_kind: str
    reviewer_owner_id: str | None = None
    reviewer_agent_id: str | None = None
    # Derived display string; immutable (the table itself rejects all UPDATEs).
    reviewer: str
    rationale: str
    reviewed_at: datetime = Field(default_factory=now_utc)


class KnowledgeFact(SQLModel, table=True):
    __tablename__ = "knowledge_fact"
    __table_args__ = (
        # Per-(series, scope) version identity. Two cooperating DB-level guards:
        # 1) uq_knowledge_fact_series_version below covers non-NULL project scopes:
        #    (series_id, version, project_id) must be unique within a project.
        # 2) uq_knowledge_fact_company_version (a partial unique index added by
        #    migration 20260727_0008) covers the company scope (project_id IS NULL):
        #    (series_id, version) must be unique for company-wide facts.
        # SQLite treats every NULL as distinct in a plain UNIQUE constraint, so the
        # 3-column constraint alone would NOT enforce company-scope identity -- the
        # partial index is required. Both guards together allow a company v1 and a
        # project v1 of the same series to coexist (the #53 bug fix) while still
        # forbidding duplicate (series, version) within each scope. Fixes #53.
        UniqueConstraint(
            "series_id", "version", "project_id", name="uq_knowledge_fact_series_version"
        ),
        UniqueConstraint("source_candidate_id", name="uq_knowledge_fact_source_candidate"),
        UniqueConstraint("review_decision_id", name="uq_knowledge_fact_review_decision"),
        UniqueConstraint("supersedes_fact_id", name="uq_knowledge_fact_supersedes"),
    )

    id: str = Field(default_factory=lambda: new_id("kfact"), primary_key=True)
    series_id: str = Field(index=True)
    version: int = Field(ge=1)
    # Effective knowledge scope: NULL = company-wide, otherwise the owning project.
    project_id: str | None = Field(default=None, foreign_key="project.id", index=True)
    # Provenance: the campaign that produced the source artifact. NEVER NULL, so
    # source-campaign ownership can always be enforced even for company-scoped facts.
    source_project_id: str = Field(foreign_key="project.id", index=True)
    statement: str
    # Capability-aware projection tags (controlled vocabulary). Immutable after
    # creation except for the one-time sentinel -> canonical transition performed
    # by owner-only classify_knowledge().
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
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


# --- Customer service / sales-conversion workflow (#109, V1.2-B) ----------
# StrEnum values are stored as plain VARCHAR (see ArtifactType note above):
# on read they come back as plain ``str``, so compare with ``== "value"``
# literals rather than ``.value``.


class CsChannel(StrEnum):
    WECHAT_WORK = "wechat_work"
    WECHAT_PUBLIC = "wechat_public"
    MOCK = "mock"


class LeadStage(StrEnum):
    VISITOR = "visitor"
    LEAD = "lead"
    QUALIFIED = "qualified"
    PROPOSAL = "proposal"
    WON = "won"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class SenderType(StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"
    OWNER = "owner"


class CsSuggestionDecision(StrEnum):
    AUTO_SEND = "auto_send"
    HUMAN_CONFIRM = "human_confirm"
    ESCALATE = "escalate"


class Conversation(SQLModel, table=True):
    __tablename__ = "conversation"

    id: str = Field(default_factory=lambda: new_id("conv"), primary_key=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    channel: CsChannel = Field(default=CsChannel.MOCK, sa_column=Column(String, nullable=False))
    external_conversation_ref: str | None = Field(default=None)
    customer_ref: str | None = Field(default=None)
    lead_stage: LeadStage = Field(
        default=LeadStage.VISITOR, sa_column=Column(String, nullable=False)
    )
    assigned_human: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class Message(SQLModel, table=True):
    __tablename__ = "message"

    id: str = Field(default_factory=lambda: new_id("msg"), primary_key=True)
    conversation_id: str = Field(foreign_key="conversation.id", index=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    direction: MessageDirection = Field(sa_column=Column(String, nullable=False))
    sender_type: SenderType = Field(sa_column=Column(String, nullable=False))
    body: str
    confidence: float | None = Field(default=None)
    is_auto_sent: bool = Field(default=False)
    escalation_flag: bool = Field(default=False)
    escalation_categories: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    knowledge_fact_refs: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now_utc)


class CsSuggestion(SQLModel, table=True):
    __tablename__ = "cs_suggestion"

    id: str = Field(default_factory=lambda: new_id("sug"), primary_key=True)
    conversation_id: str = Field(foreign_key="conversation.id", index=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    decision: CsSuggestionDecision = Field(sa_column=Column(String, nullable=False))
    text: str
    confidence: float | None = Field(default=None)
    escalation_categories: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    knowledge_fact_refs: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    fact_revisions: dict[str, int] = Field(default_factory=dict, sa_column=Column(JSON))
    consumed: bool = Field(default=False)
    # P1-2 tamper-proof flag: True iff this suggestion was generated WITH
    # SalesPlaybook evidence rows. The send-time gate fails CLOSED (409) when
    # these rows are absent, instead of silently skipping revalidation and
    # treating a tampered suggestion as KnowledgeFact-only.
    sales_evidence_cited: bool = Field(
        default=False,
        sa_column=Column(Boolean, server_default=false(), nullable=False),
    )
    idempotency_key: str = Field(unique=True, index=True)
    created_at: datetime = Field(default_factory=now_utc)


# ---------------------------------------------------------------------------
# SalesPlaybook V0 -- read-only official sales-script evidence source
# ---------------------------------------------------------------------------
# Five new tables adapt the extracted Mihe YiWaiWai EBF sales scripts into AIOS
# as a READ-ONLY evidence source for the existing customer-service pipeline
# (``CustomerService.generate_suggestion`` -> ``CsSuggestion`` ->
# ``HUMAN_CONFIRM``). This is NOT a CRM, NOT auto-send and NOT auto-sales.
#
# Reuse boundary (design §0): ``Conversation`` / ``Message`` / ``CsSuggestion``
# / ``Artifact`` / ``KnowledgeCandidate`` / ``KnowledgeFact`` are used as-is and
# none of their columns change. Images reuse ``Artifact(type=IMAGE)`` with the
# existing ``ArtifactReviewStatus`` -- no new Artifact column or enum member.
#
# STORAGE NOTE: unlike ``aios.pilot2.models`` (plain enum annotations ->
# ``sqlalchemy.Enum`` -> persists the member NAME), every enum column here is
# declared as ``Column(String)``, matching the surrounding main-schema style
# (see ``Conversation.channel``). A ``StrEnum`` member is a ``str``, so the
# persisted representation is the member VALUE (``'mihe_1_0'``). All SQL gates
# below therefore compare VALUES, and ``tests/test_sales_playbook_models.py``
# pins that representation so an ORM change cannot silently disable them.


def _enum_values_sql(enum_cls: type[StrEnum]) -> str:
    """SQL literal list of the *values* an enum-backed VARCHAR column may hold."""
    return ", ".join(f"'{member.value}'" for member in enum_cls)


def _sql_literal_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class SalesScriptScope(StrEnum):
    """Persisted product-version domain of a script entry / fact binding."""

    MIHE_1_0 = "mihe_1_0"
    MIHE_2_0 = "mihe_2_0"
    COMMON = "common"


class SalesScriptSourceStatus(StrEnum):
    """Generation lifecycle of an imported source package (design D1)."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    INACTIVE = "inactive"


class SalesScriptSegmentType(StrEnum):
    """Kind of one ordered fragment of an entry (design D2)."""

    TEXT = "text"
    IMAGE = "image"


class SalesScriptFactClass(StrEnum):
    """The mutable-policy fact classes covered by the safety contract (D6)."""

    PRICE = "price"
    COMMISSION = "commission"
    MEMBERSHIP = "membership"
    CAPABILITY = "capability"
    URL = "url"
    PROMO = "promo"
    OTHER = "other"


class SalesScriptFactStatus(StrEnum):
    """Verification state of one dynamic fact occurrence (design §6)."""

    VERIFIED_CURRENT = "verified_current"
    NEEDS_REVIEW = "needs_review"
    STALE = "stale"
    VERSION_1_ONLY = "version_1_only"


class SalesScriptQueryScope(StrEnum):
    """RUNTIME query classification (design §5) -- deliberately NOT persisted.

    ``COMPARE_1_0_2_0`` exists so a legitimate "how is this different from the
    old Coze one?" question is answerable instead of being forced to UNKNOWN.
    ``UNKNOWN`` is fail-closed: no version-specific claim may be retrieved.
    """

    MIHE_1_0 = "mihe_1_0"
    MIHE_2_0 = "mihe_2_0"
    COMPARE_1_0_2_0 = "compare_1_0_2_0"
    UNKNOWN = "unknown"


# The evidence row reports the weakest fact state among an entry's bindings. It
# is deliberately an ALIAS of ``SalesScriptFactStatus`` rather than a second
# four-member enum, so the two vocabularies can never drift apart (D2: no
# duplicated truth).
SalesScriptEvidenceFactSafety = SalesScriptFactStatus

# V0 accepts exactly one source type. Spelled out as data (not derived) so the
# allow-list stays readable at review time.
SALES_SCRIPT_SOURCE_TYPE_MIHE_EBF = "mihe_ebf"
SALES_SCRIPT_SOURCE_TYPES: tuple[str, ...] = (SALES_SCRIPT_SOURCE_TYPE_MIHE_EBF,)


class SalesScriptSource(SQLModel, table=True):
    """One immutable imported generation of the official script package (D1).

    Two INDEPENDENT integrity anchors are stored, because they fail on
    different corruptions:

    * ``source_file_hash`` = SHA256(raw EBF bytes) -- catches an image whose
      content changed while its filename did not.
    * ``extraction_manifest_hash`` = SHA256(canonical export JSON ‖ ordered
      normalised image SHA256 manifest ‖ image-reference mapping) -- catches a
      change in the extracted structure or in the image manifest.

    Activation is atomic: a new package is inserted ACTIVE and the previous
    ACTIVE package of the same ``source_type`` is flipped to SUPERSEDED inside
    the SAME transaction. ``uq_ssrc_single_active`` (a PARTIAL unique index)
    makes "two live generations" unrepresentable at the database boundary, so
    retrieval always resolves exactly one generation and can never mix two.
    """

    __tablename__ = "sales_script_source"

    id: str = Field(default_factory=lambda: new_id("ssrc"), primary_key=True)
    source_type: str = Field(default=SALES_SCRIPT_SOURCE_TYPE_MIHE_EBF, index=True)
    # cleanup B: the raw EBF file and the structured export JSON are two
    # distinct artifacts and therefore two distinct filename fields.
    original_ebf_filename: str
    extracted_manifest_filename: str
    source_file_hash: str = Field(index=True)
    extraction_manifest_hash: str = Field(unique=True, index=True)
    source_version: str
    status: SalesScriptSourceStatus = Field(sa_column=Column(String, nullable=False))
    imported_at: datetime = Field(default_factory=now_utc)
    entry_count: int = Field(default=0)
    # cleanup A: first-package acceptance statistics are AUDIT DATA for this
    # package, never importer constants.
    metadata_json: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column("metadata", JSON)
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({_enum_values_sql(SalesScriptSourceStatus)})",
            name="ck_ssrc_status_gate",
        ),
        CheckConstraint(
            f"source_type IN ({_sql_literal_list(SALES_SCRIPT_SOURCE_TYPES)})",
            name="ck_ssrc_source_type_gate",
        ),
        # D1: at most ONE active generation per source type. A partial unique
        # index is used rather than a service-layer convention so a rogue raw
        # -SQL writer cannot create a second live generation either.
        Index(
            "uq_ssrc_single_active",
            "source_type",
            "status",
            unique=True,
            sqlite_where=text(f"status = '{SalesScriptSourceStatus.ACTIVE.value}'"),
            postgresql_where=text(f"status = '{SalesScriptSourceStatus.ACTIVE.value}'"),
        ),
    )


class SalesScriptEntry(SQLModel, table=True):
    """One official script entry, verbatim and immutable (D2).

    The entry deliberately does NOT store a ``segments`` JSON blob: the ordered
    text/image structure has exactly one authority, :class:`SalesScriptSegment`.
    ``source_hash`` = SHA256(normalised segments ordered by ``sequence``) is the
    immutability proof -- recomputing it on re-import must reproduce the stored
    value byte-for-byte.
    """

    __tablename__ = "sales_script_entry"

    id: str = Field(default_factory=lambda: new_id("ssent"), primary_key=True)
    source_id: str = Field(
        sa_column=Column(
            String,
            ForeignKey("sales_script_source.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        )
    )
    source_entry_id: str = Field(index=True)
    product_scope: SalesScriptScope = Field(sa_column=Column(String, nullable=False))
    category: str = Field(index=True)
    title: str
    source_hash: str = Field(index=True)
    imported_at: datetime = Field(default_factory=now_utc)

    __table_args__ = (
        # One row per official entry id per generation. A duplicate official id
        # inside one package is a corrupt package, and the importer must fail
        # closed on it rather than silently keep two rival copies.
        UniqueConstraint(
            "source_id", "source_entry_id", name="uq_ss_entry_source_entry"
        ),
        # Enables the composite FK from SalesScriptFactBinding below: an entry
        # is uniquely identified by (id, product_scope), so a binding can only
        # ever denormalise the scope the entry ACTUALLY has.
        UniqueConstraint("id", "product_scope", name="uq_ss_entry_id_scope"),
        CheckConstraint(
            f"product_scope IN ({_enum_values_sql(SalesScriptScope)})",
            name="ck_ss_entry_scope_member",
        ),
    )


class SalesScriptSegment(SQLModel, table=True):
    """The SOLE authority for an entry's ordered text/image structure (D2).

    Replaces both the former ``SalesScriptEntry.segments`` JSON column and the
    former ``SalesScriptMedia`` table, so there is exactly one place where the
    truth lives. Image bytes never enter this table: an IMAGE segment points at
    an existing ``Artifact(type=IMAGE)`` row (cleanup C).

    The TEXT/IMAGE split is mutually exclusive at the database boundary:
    ``ck_ssseg_type_nullability`` rejects a TEXT segment carrying an artifact
    and an IMAGE segment carrying inline text.
    """

    __tablename__ = "sales_script_segment"

    id: str = Field(default_factory=lambda: new_id("ssseg"), primary_key=True)
    entry_id: str = Field(
        sa_column=Column(
            String,
            ForeignKey("sales_script_entry.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    sequence: int
    segment_type: SalesScriptSegmentType = Field(
        sa_column=Column(String, nullable=False)
    )
    text_content: str | None = Field(default=None)
    artifact_id: str | None = Field(
        default=None,
        sa_column=Column(
            String,
            ForeignKey("artifact.id", ondelete="RESTRICT"),
            nullable=True,
            index=True,
        ),
    )
    caption: str | None = Field(default=None)

    __table_args__ = (
        UniqueConstraint("entry_id", "sequence", name="uq_ssseg_entry_sequence"),
        CheckConstraint(
            f"segment_type IN ({_enum_values_sql(SalesScriptSegmentType)})",
            name="ck_ssseg_type_member",
        ),
        CheckConstraint(
            f"(segment_type = '{SalesScriptSegmentType.TEXT.value}'"
            " AND text_content IS NOT NULL AND artifact_id IS NULL)"
            f" OR (segment_type = '{SalesScriptSegmentType.IMAGE.value}'"
            " AND artifact_id IS NOT NULL AND text_content IS NULL)",
            name="ck_ssseg_type_nullability",
        ),
        CheckConstraint("sequence >= 0", name="ck_ssseg_sequence_non_negative"),
    )


class SalesScriptFactBinding(SQLModel, table=True):
    """ONE occurrence of a mutable-policy fact inside ONE entry (D3).

    Structurally bound to a real entry by foreign key -- never a JSON list of
    entry ids that nothing validates. ``binding_hash`` =
    SHA256(entry_id ‖ fact_key ‖ normalised raw_span) is the deterministic
    idempotency anchor: re-importing the same package produces the same hash and
    therefore no duplicate row.

    Cross-version binding is impossible, not merely discouraged. ``entry_scope``
    is denormalised from the entry, but ``fk_ssfb_entry_scope`` is a COMPOSITE
    foreign key onto ``uq_ss_entry_id_scope``, so the denormalised value is
    guaranteed to equal the entry's real scope; ``ck_ssfb_scope_compat`` then
    requires the fact's own scope to be COMMON or exactly that scope. (A plain
    CHECK cannot reference another table, so the composite FK is what carries
    the cross-row half of the contract into the database.)

    Import default is ``NEEDS_REVIEW``: a fact is NEVER auto-promoted to
    ``VERIFIED_CURRENT``; only an explicit owner review may do that.
    """

    __tablename__ = "sales_script_fact_binding"

    id: str = Field(default_factory=lambda: new_id("ssfb"), primary_key=True)
    entry_id: str = Field(sa_column=Column(String, nullable=False, index=True))
    entry_scope: SalesScriptScope = Field(sa_column=Column(String, nullable=False))
    fact_key: str = Field(index=True)
    fact_class: SalesScriptFactClass = Field(sa_column=Column(String, nullable=False))
    raw_span: str
    scope: SalesScriptScope = Field(sa_column=Column(String, nullable=False))
    status: SalesScriptFactStatus = Field(sa_column=Column(String, nullable=False))
    reviewed_at: datetime | None = Field(default=None)
    reviewed_by: str | None = Field(default=None)
    binding_hash: str = Field(unique=True, index=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["entry_id", "entry_scope"],
            ["sales_script_entry.id", "sales_script_entry.product_scope"],
            name="fk_ssfb_entry_scope",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "entry_id", "fact_key", "raw_span", name="uq_ssfb_entry_fact_span"
        ),
        CheckConstraint(
            f"fact_class IN ({_enum_values_sql(SalesScriptFactClass)})",
            name="ck_ssfb_class_gate",
        ),
        CheckConstraint(
            f"status IN ({_enum_values_sql(SalesScriptFactStatus)})",
            name="ck_ssfb_status_gate",
        ),
        CheckConstraint(
            f"scope IN ({_enum_values_sql(SalesScriptScope)})",
            name="ck_ssfb_scope_member",
        ),
        CheckConstraint(
            f"entry_scope IN ({_enum_values_sql(SalesScriptScope)})",
            name="ck_ssfb_entry_scope_member",
        ),
        # D3: a 1.0 entry may only carry 1.0 / COMMON facts, a 2.0 entry only
        # 2.0 / COMMON facts.
        CheckConstraint(
            f"scope = '{SalesScriptScope.COMMON.value}' OR scope = entry_scope",
            name="ck_ssfb_scope_compat",
        ),
    )


class CsSuggestionSalesEvidence(SQLModel, table=True):
    """Association row linking a ``CsSuggestion`` to the entries it drew on (D5).

    Deliberately a separate table: the existing ``CsSuggestion`` schema is NOT
    modified (no evidence JSON column), and the double foreign key makes the
    evidence auditable and joinable in both directions.
    """

    __tablename__ = "cs_suggestion_sales_evidence"

    id: str = Field(default_factory=lambda: new_id("ssev"), primary_key=True)
    suggestion_id: str = Field(
        sa_column=Column(
            String,
            ForeignKey("cs_suggestion.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        )
    )
    entry_id: str = Field(
        sa_column=Column(
            String,
            ForeignKey("sales_script_entry.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        )
    )
    rank: int
    match_reason: str
    # Weakest fact state among the entry's bindings, in the SAME vocabulary as
    # SalesScriptFactStatus (see the alias above).
    fact_safety: SalesScriptFactStatus = Field(sa_column=Column(String, nullable=False))
    created_at: datetime = Field(default_factory=now_utc)

    __table_args__ = (
        UniqueConstraint(
            "suggestion_id", "entry_id", name="uq_ssev_suggestion_entry"
        ),
        CheckConstraint(
            f"fact_safety IN ({_enum_values_sql(SalesScriptFactStatus)})",
            name="ck_ssev_fact_safety_gate",
        ),
        CheckConstraint("rank >= 0", name="ck_ssev_rank_non_negative"),
    )
