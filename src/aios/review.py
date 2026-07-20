"""Independent Review Protocol (#64).

Separates *execution* from *verification*. An Artifact is produced UNVERIFIED,
then independently reviewed along configured dimensions (fact correctness,
acceptance criteria, brand strategy, risk). Reviewers are themselves registry
agents (or humans) routed through the existing Agent Gateway (#57).

Trust boundaries enforced here (per architecture review + merge-confirmation):
1. Policy traceability -- every ReviewResult records policy_id + a snapshot
   hash, so historical review meaning never changes when the policy is edited.
2. Reviewer identity integrity -- AGENT => reviewer_agent_id set & user_id
   null; USER => user_id set & reviewer_agent_id null (mutually exclusive).
3. Review idempotency -- an identity hash (artifact+reviewer+policy) makes a
   replay return the original result; a conflicting replay is rejected (409).
4. Deterministic aggregation -- any REJECTED -> REJECTED; any NEEDS_REVISION
   -> NEEDS_REVISION; all required reviewers PASS -> APPROVED; otherwise the
   artifact stays UNVERIFIED. A single reviewer can NEVER approve directly.
5. Revision/escalation concurrency -- one child revision per source, one
   pending owner Approval per task, revision_count server-derived (no dup
   Artifact/Approval/AuditLog).
6. Feedback semantics -- reviewer_score = artifact quality (any reviewer);
   usefulness = human-in-the-loop signal (USER only), kept distinct.

AI never auto-approves and never promotes agents -- the owner remains the
final decision maker.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, select

from aios.audit import append_audit
from aios.execution import execute_task
from aios.models import (
    Agent,
    AgentTrustLevel,
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactReviewStatus,
    ReviewDimension,
    ReviewedFact,
    ReviewedFactStatus,
    ReviewOverall,
    ReviewPolicy,
    ReviewResult,
    ReviewReviewerType,
    ReviewVerdict,
    RiskLevel,
    Task,
    TaskStatus,
    now_utc,
)
from aios.services import ServiceError

# Single trust axis ordering (higher = more trusted). Mirrors #104's intent:
# experimental agents are blocked from delegation/review until promoted.
_TRUST_RANK: dict[AgentTrustLevel, int] = {
    AgentTrustLevel.EXPERIMENTAL: 0,
    AgentTrustLevel.VERIFIED_EXTERNAL: 1,
    AgentTrustLevel.INTERNAL: 2,
}


class ReviewError(ServiceError):
    """Raised when a review submission violates the protocol."""


def _trust_ok(candidate: AgentTrustLevel, floor: AgentTrustLevel) -> bool:
    return _TRUST_RANK.get(candidate, 0) >= _TRUST_RANK.get(floor, 0)


def assert_reviewer_independence(
    policy: ReviewPolicy,
    executor_agent: Agent | None,
    reviewer_agent: Agent | None,
    reviewer_type: ReviewReviewerType,
) -> None:
    """Enforce reviewer independence rules (architecture decision #3).

    - Structural: an agent cannot review its own work.
    - Trust floor: reviewer trust_level must meet policy.required_reviewer_trust
      (experimental agents are always blocked).
    - Capability extension: if the policy lists required_capabilities, the
      reviewer must declare at least one matching capability.
    USER (human) reviewers are independent by construction, so only the
    agent-level rules apply to them.
    """
    if reviewer_type != ReviewReviewerType.AGENT:
        return
    if reviewer_agent is None:
        raise ReviewError(422, "AGENT 评审者必须提供 reviewer_agent_id")
    if executor_agent is not None and reviewer_agent.id == executor_agent.id:
        raise ReviewError(422, "评审者不能评审自己产出的 Artifact（结构性独立）")
    if not _trust_ok(reviewer_agent.trust_level, policy.required_reviewer_trust):
        raise ReviewError(
            422,
            f"评审者信任级别 {reviewer_agent.trust_level.value} 低于策略要求 "
            f"{policy.required_reviewer_trust.value}",
        )
    if policy.required_capabilities:
        caps = set(reviewer_agent.capabilities or [])
        if not caps.intersection(policy.required_capabilities):
            raise ReviewError(
                422,
                f"评审者能力 {sorted(caps)} 不满足策略要求 "
                f"{policy.required_capabilities}",
            )


def human_review_present(session: Session, artifact_id: str) -> bool:
    """True if at least one USER (human) review exists for the artifact."""
    return (
        session.exec(
            select(ReviewResult)
            .where(ReviewResult.artifact_id == artifact_id)
            .where(ReviewResult.reviewer_type == ReviewReviewerType.USER)
        ).first()
        is not None
    )


def _policy_hash(policy: ReviewPolicy) -> str:
    """Deterministic snapshot hash of a policy's meaningful fields.

    Stored on every ``ReviewResult`` (``policy_hash``) so historical review
    meaning never changes when the policy row is later edited
    (trust boundary #1).
    """
    payload = {
        "name": policy.name,
        "applies_to": policy.applies_to,
        "dimensions": policy.dimensions,
        "brand_policy_id": policy.brand_policy_id,
        "required_reviewer_trust": policy.required_reviewer_trust.value,
        "required_capabilities": policy.required_capabilities,
        "max_revisions": policy.max_revisions,
        "required_reviewers": policy.required_reviewers,
    }
    canon = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _idempotency_key(
    *,
    artifact_id: str,
    reviewer_type: ReviewReviewerType,
    reviewer_agent_id: str | None,
    user_id: str | None,
    policy_id: str | None,
) -> str:
    """Identity hash (artifact + reviewer + policy). Unique per reviewer verdict."""
    ident = "|".join([
        artifact_id,
        reviewer_type.value,
        reviewer_agent_id or "",
        user_id or "",
        policy_id or "",
    ])
    return "review:" + hashlib.sha256(ident.encode("utf-8")).hexdigest()


def _content_signature(
    dimensions: list[dict[str, Any]],
    overall: ReviewOverall,
    reviewer_score: float | None,
    usefulness: float | None,
) -> str:
    """Content hash used to detect *conflicting* replays (same identity,
    different verdict)."""
    canon = json.dumps(
        {"d": dimensions, "o": overall.value, "s": reviewer_score, "u": usefulness},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _verdict_to_status(overall: ReviewOverall) -> ArtifactReviewStatus:
    return {
        ReviewOverall.APPROVED: ArtifactReviewStatus.APPROVED,
        ReviewOverall.REJECTED: ArtifactReviewStatus.REJECTED,
        ReviewOverall.NEEDS_REVISION: ArtifactReviewStatus.NEEDS_REVISION,
    }[overall]


def aggregate_reviews(
    session: Session,
    artifact_id: str,
    policy: ReviewPolicy,
    *,
    actor: str = "agent",
) -> ArtifactReviewStatus:
    """Deterministic review aggregation (trust boundary #4).

    - any REJECTED -> REJECTED (fail / escalate)
    - any NEEDS_REVISION -> NEEDS_REVISION
    - all required reviewers PASS (distinct count >= policy.required_reviewers)
      -> APPROVED
    - otherwise -> UNVERIFIED (required reviewers not yet satisfied; a single
      reviewer can NEVER approve an artifact directly)
    """
    results = session.exec(
        select(ReviewResult).where(ReviewResult.artifact_id == artifact_id)
    ).all()
    if any(r.overall == ReviewOverall.REJECTED for r in results):
        status = ArtifactReviewStatus.REJECTED
    elif any(r.overall == ReviewOverall.NEEDS_REVISION for r in results):
        status = ArtifactReviewStatus.NEEDS_REVISION
    else:
        pass_reviewers = {
            r.reviewer_agent_id or r.user_id
            for r in results
            if r.overall == ReviewOverall.APPROVED
        }
        status = (
            ArtifactReviewStatus.APPROVED
            if len(pass_reviewers) >= policy.required_reviewers
            else ArtifactReviewStatus.UNVERIFIED
        )
    artifact = session.get(Artifact, artifact_id)
    if artifact is not None and artifact.review_status != status:
        artifact.review_status = status
        session.add(artifact)
        append_audit(
            session,
            actor=actor,
            action="artifact.review_status_aggregated",
            resource_type="artifact",
            resource_id=artifact_id,
            project_id=artifact.project_id,
            task_id=artifact.task_id,
            before={},
            after={"review_status": status.value},
            idempotency_key=f"audit:aggregate:{artifact_id}",
        )
    session.commit()
    return status


def submit_review(
    session: Session,
    *,
    artifact_id: str,
    reviewer_type: ReviewReviewerType,
    reviewer_agent_id: str | None = None,
    user_id: str | None = None,
    dimensions: list[dict[str, Any]] | None = None,
    overall: ReviewOverall,
    reviewer_score: float | None = None,
    usefulness: float | None = None,
    policy: ReviewPolicy | None = None,
    executor_agent_id: str | None = None,
    actor: str = "agent",
) -> ReviewResult:
    """Record one independent review and reflect its outcome on the Artifact.

    Creates a ``ReviewResult`` (+ ``ReviewedFact`` rows for any fact_correctness
    dimension that carries a ``statement``), updates ``Artifact.review_status``,
    and emits an audit event. Enforces reviewer independence when ``policy`` is
    supplied. Does NOT auto-approve anything beyond recording the verdict.
    """
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise ServiceError(404, "Artifact not found")
    # --- identity integrity (trust boundary #2) ---
    if reviewer_type == ReviewReviewerType.AGENT:
        if reviewer_agent_id is None:
            raise ReviewError(422, "AGENT 评审者必须提供 reviewer_agent_id")
        if user_id is not None:
            raise ReviewError(422, "AGENT 评审者不得同时携带 user_id（身份互斥）")
    elif reviewer_type == ReviewReviewerType.USER:
        if user_id is None:
            raise ReviewError(422, "USER 评审者必须提供 user_id")
        if reviewer_agent_id is not None:
            raise ReviewError(422, "USER 评审者不得同时携带 reviewer_agent_id（身份互斥）")
    else:
        raise ReviewError(422, "reviewer_type 必须是 AGENT 或 USER")

    reviewer_agent = None
    if reviewer_agent_id is not None:
        reviewer_agent = session.get(Agent, reviewer_agent_id)
        if reviewer_agent is None:
            raise ServiceError(404, "Reviewer agent not found")
    executor_agent = (
        session.get(Agent, executor_agent_id) if executor_agent_id else None
    )
    # --- feedback semantics (trust boundary #6) ---
    # ``usefulness`` is a human-in-the-loop signal; only USER may set it.
    # Agents must not write it, keeping it distinct from agent review scores.
    if usefulness is not None and reviewer_type != ReviewReviewerType.USER:
        raise ReviewError(422, "usefulness 仅限 USER（人类）评审者填写")

    if policy is not None:
        assert_reviewer_independence(
            policy, executor_agent, reviewer_agent, reviewer_type
        )

    dimensions = dimensions or []

    # --- idempotency (trust boundary #3) ---
    policy_id = policy.id if policy is not None else None
    policy_hash = _policy_hash(policy) if policy is not None else None
    idem_key = _idempotency_key(
        artifact_id=artifact_id,
        reviewer_type=reviewer_type,
        reviewer_agent_id=reviewer_agent_id,
        user_id=user_id,
        policy_id=policy_id,
    )
    existing = session.exec(
        select(ReviewResult).where(ReviewResult.idempotency_key == idem_key)
    ).first()
    if existing is not None:
        new_sig = _content_signature(dimensions, overall, reviewer_score, usefulness)
        old_sig = _content_signature(
            existing.dimensions,
            existing.overall,
            existing.reviewer_score,
            existing.usefulness,
        )
        if new_sig == old_sig:
            return existing  # identical replay -> original result
        raise ReviewError(
            409, "该评审者对此 Artifact+Policy 的评审已存在，冲突禁止覆盖"
        )

    result = ReviewResult(
        artifact_id=artifact_id,
        reviewer_type=reviewer_type,
        reviewer_agent_id=reviewer_agent_id,
        user_id=user_id,
        policy_id=policy_id,
        policy_hash=policy_hash,
        idempotency_key=idem_key,
        dimensions=dimensions,
        overall=overall,
        reviewer_score=reviewer_score,
        usefulness=usefulness,
    )
    session.add(result)

    # --- reflect outcome: deterministic aggregation when policy supplied ---
    if policy is not None:
        new_status = aggregate_reviews(session, artifact_id, policy, actor=actor)
    else:
        # Legacy / untracked path: record verdict verbatim. NOTE: a single
        # reviewer must NOT approve directly in production -- always pass `policy`
        # so multi-reviewer aggregation is enforced.
        new_status = _verdict_to_status(overall)
        artifact.review_status = new_status
        session.add(artifact)

    # Reuse ReviewedFact for fact_correctness detail (decision: reuse, not fork).
    for dim in dimensions:
        if (
            dim.get("dim") == ReviewDimension.FACT_CORRECTNESS.value
            and dim.get("statement")
        ):
            status = (
                ReviewedFactStatus.APPROVED
                if dim.get("verdict") == ReviewVerdict.PASS.value
                else ReviewedFactStatus.REJECTED
            )
            session.add(
                ReviewedFact(
                    artifact_id=artifact_id,
                    statement=dim["statement"],
                    status=status,
                    reviewer=reviewer_agent_id or user_id or actor,
                )
            )

    identity = reviewer_agent_id or user_id or actor
    append_audit(
        session,
        actor=actor,
        action="artifact.reviewed",
        resource_type="artifact",
        resource_id=artifact_id,
        project_id=artifact.project_id,
        task_id=artifact.task_id,
        before={"review_status": ArtifactReviewStatus.UNVERIFIED.value},
        after={
            "review_status": new_status.value,
            "overall": overall.value,
            "reviewer": identity,
        },
        idempotency_key=f"audit:review:{result.id}",
    )
    session.commit()
    session.refresh(result)
    return result


def assert_revision_lineage(
    session: Session,
    artifact: Artifact,
    revision_of_id: str | None,
) -> None:
    """Service-layer guards for the ``revision_of`` link.

    The physical FK only enforces *existence* of the referenced row. Project
    ownership, self-reference, and lineage cycles are not database concepts, so
    they must be enforced here. Called before any ``revision_of`` is assigned.

    - no self-reference: an artifact cannot point at itself.
    - existence: the referenced artifact must exist (DB also enforces this).
    - same-project ownership: parent and child must share ``project_id``.
    - no revision cycles: walking parents upward from the referenced artifact
      must terminate (root with ``revision_of IS NULL``) and must never loop
      back to ``artifact``.
    """
    if not revision_of_id:
        return
    if revision_of_id == artifact.id:
        raise ReviewError(422, "Artifact 不能指向自身作为修订来源（禁止自引用）")
    parent = session.get(Artifact, revision_of_id)
    if parent is None:
        raise ReviewError(422, "修订来源 Artifact 不存在")
    if parent.project_id != artifact.project_id:
        raise ReviewError(
            422,
            f"修订来源 project_id {parent.project_id} 与当前 Artifact "
            f"project_id {artifact.project_id} 不一致（禁止跨项目修订链）",
        )
    # Walk the lineage upward; a valid chain terminates at a root and never
    # loops back to this artifact.
    visited: set[str] = set()
    cur: Artifact | None = parent
    while cur is not None and cur.revision_of is not None:
        if cur.id in visited:
            break  # already traversed; avoid infinite loop on malformed data
        visited.add(cur.id)
        if cur.revision_of == artifact.id:
            raise ReviewError(422, "检测到修订链成环，禁止创建循环 lineage")
        cur = session.get(Artifact, cur.revision_of)


def trigger_revision(
    session: Session,
    *,
    task_id: str,
    adapter: Any,
    source_artifact: Artifact,
    policy: ReviewPolicy,
    actor: str = "agent",
) -> Artifact | None:
    """Re-run execution to revise, or escalate to the owner when capped.

    Returns the new Artifact if a revision was produced; returns ``None`` when the
    revision cap (``policy.max_revisions``) was already reached and the case was
    escalated to an owner Approval gate (no infinite loop, no auto-decision).
    """
    task = session.get(Task, task_id)
    if task is None:
        raise ServiceError(404, "Task not found")

    # Escalation branch: cap already reached.
    if source_artifact.revision_count >= policy.max_revisions:
        # Dedup: only one pending escalation Approval per task -- concurrent
        # revision requests must not create duplicate owner gates.
        existing_esc = session.exec(
            select(Approval).where(
                Approval.task_id == task_id,
                Approval.action_type == "revision_escalation",
                Approval.status == ApprovalStatus.PENDING,
            )
        ).first()
        if existing_esc is None:
            session.add(
                Approval(
                    project_id=task.project_id,
                    task_id=task_id,
                    action_type="revision_escalation",
                    risk_level=RiskLevel.L2,
                    status=ApprovalStatus.PENDING,
                    rationale=(
                        f"修订次数 {source_artifact.revision_count} 已达策略上限 "
                        f"{policy.max_revisions}，升级 owner 决策"
                    ),
                )
            )
            append_audit(
                session,
                actor=actor,
                action="revision.escalated",
                resource_type="artifact",
                resource_id=source_artifact.id,
                project_id=source_artifact.project_id,
                task_id=task_id,
                before={"revision_count": source_artifact.revision_count},
                after={"max_revisions": policy.max_revisions, "escalated": True},
                idempotency_key=f"audit:revision:escalate:{source_artifact.id}",
            )
            session.commit()
        return None

    # Idempotent revision: if a child already exists for this source, reuse it.
    # A double trigger_revision call (incl. concurrent) returns the same
    # artifact -- no duplicate execution / Artifact / AuditLog.
    existing_rev = session.exec(
        select(Artifact).where(Artifact.revision_of == source_artifact.id)
    ).first()
    if existing_rev is not None:
        return existing_rev

    # Reset to READY so execute_task can re-run with a fresh idempotency key.
    task.status = TaskStatus.READY
    task.updated_at = now_utc()
    session.add(task)
    session.commit()

    # Server-derived revision_count: max over the task lineage + 1 (never
    # passed in), so two triggers cannot both claim the same count.
    max_rc = session.exec(
        select(func.max(Artifact.revision_count)).where(
            Artifact.task_id == task_id
        )
    ).first()
    new_count = (max_rc or 0) + 1

    new_key = f"revision:{source_artifact.id}:{new_count}"
    new_artifact = execute_task(
        session, task_id, new_key, adapter=adapter, actor=actor
    )
    # Service-layer lineage guards before linking (same-project / no self-ref /
    # no cycle). The DB FK only checks existence; these rules are not DB concepts.
    assert_revision_lineage(session, new_artifact, source_artifact.id)
    new_artifact.revision_of = source_artifact.id
    new_artifact.revision_count = new_count
    new_artifact.review_status = ArtifactReviewStatus.UNVERIFIED
    session.add(new_artifact)
    append_audit(
        session,
        actor=actor,
        action="revision.triggered",
        resource_type="artifact",
        resource_id=new_artifact.id,
        project_id=new_artifact.project_id,
        task_id=task_id,
        before={"revision_of": None, "revision_count": 0},
        after={
            "revision_of": source_artifact.id,
            "revision_count": new_artifact.revision_count,
        },
        idempotency_key=f"audit:revision:triggered:{new_artifact.id}",
    )
    session.commit()
    session.refresh(new_artifact)
    return new_artifact
