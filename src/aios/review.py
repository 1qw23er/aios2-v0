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
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from aios.audit import append_audit
from aios.execution import execute_task
from aios.models import (
    AdapterType,
    Agent,
    AgentTrustLevel,
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactReviewStatus,
    ReviewAssignment,
    ReviewDimension,
    ReviewedFact,
    ReviewedFactStatus,
    ReviewOverall,
    ReviewPolicy,
    ReviewResult,
    ReviewReviewerType,
    ReviewVerdict,
    RiskLevel,
    RoutingMode,
    Task,
    TaskStatus,
    new_id,
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
    review_round: int = 1,
) -> str:
    """Identity hash (artifact + reviewer + policy + round).

    The round is server-derived (target artifact revision_count + 1) and makes a
    reviewer's verdict in round N distinct from round N+1 (idempotency boundary
    #3 / C4): the same reviewer reviewing a revised artifact is a fresh verdict.
    """
    ident = "|".join([
        artifact_id,
        reviewer_type.value,
        reviewer_agent_id or "",
        user_id or "",
        policy_id or "",
        str(review_round),
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
    """Deterministic review aggregation with the owner gate (trust boundary #4 / C1).

    Verification is binding-driven (req 3): instead of counting pass reviewers, we
    load the exact ``ReviewAssignment`` set for this target + round and require
    EVERY assigned (review_task_id, reviewer_agent_id, review_dimension) to have a
    matching ReviewResult (same task, identity, dimension, round). Old-round
    results can never satisfy a new round because we filter by ``review_round`` and
    join through the round-specific ``review_task_id``.

    Status rules:
      - any REJECTED -> REJECTED (escalate to owner)
      - any NEEDS_REVISION -> NEEDS_REVISION (enter the revision path)
      - all assignments APPROVED -> REVIEW_PASSED (open the owner gate; NEVER APPROVED)
      - otherwise -> UNVERIFIED (required reviewers not yet satisfied)
    """
    target = session.get(Artifact, artifact_id)
    if target is None:
        raise ServiceError(404, "Artifact not found")
    current_round = target.revision_count + 1
    assignments = list(
        session.exec(
            select(ReviewAssignment).where(
                ReviewAssignment.target_artifact_id == artifact_id,
                ReviewAssignment.review_round == current_round,
            )
        )
    )
    if not assignments:
        # Legacy / policy-only path: no immutable binding exists (e.g. a direct
        # ``submit_review`` without ``dispatch_reviews_for_artifact``). Fall back
        # to count-based aggregation so existing callers keep working. The runtime
        # protocol ALWAYS creates ``ReviewAssignment`` rows via dispatch, so the
        # exact-binding verification below is what governs the production path
        # (req 3) -- we never aggregate by count alone when a binding is present.
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
                ArtifactReviewStatus.REVIEW_PASSED
                if len(pass_reviewers) >= policy.required_reviewers
                else ArtifactReviewStatus.UNVERIFIED
            )
        if target.review_status != status:
            target.review_status = status
            session.add(target)
            append_audit(
                session,
                actor=actor,
                action="artifact.review_status_aggregated",
                resource_type="artifact",
                resource_id=artifact_id,
                project_id=target.project_id,
                task_id=target.task_id,
                before={},
                after={"review_status": status.value},
                idempotency_key=f"audit:aggregate:{artifact_id}:{current_round}",
            )
            if status == ArtifactReviewStatus.REVIEW_PASSED:
                _ensure_review_gate_approval(
                    session, target, policy_id=policy.id, actor=actor
                )
        session.commit()
        return status

    # Exact expected set of (task, reviewer identity, dimension) from the binding.
    # Old-round results are excluded by the (task_id, review_round) filter.
    results = list(
        session.exec(
            select(ReviewResult).where(
                ReviewResult.review_task_id.in_(
                    [a.review_task_id for a in assignments]
                ),
                ReviewResult.review_round == current_round,
            )
        )
    )
    by_task: dict[str, ReviewResult] = {
        r.review_task_id: r for r in results if r.review_task_id is not None
    }

    verdicts: list[str] = []
    for a in assignments:
        r = by_task.get(a.review_task_id)
        if r is None:
            verdicts.append("missing")
            continue
        # Trust boundary: the result's reviewer identity + dimension MUST match the
        # server binding exactly. A mismatched identity/dimension cannot satisfy
        # the gate (treated as not-yet-valid).
        identity = r.reviewer_agent_id or r.user_id
        if identity != a.reviewer_agent_id:
            verdicts.append("identity_mismatch")
            continue
        if r.review_dimension != a.review_dimension:
            verdicts.append("dimension_mismatch")
            continue
        verdicts.append(r.overall.value)

    if ArtifactReviewStatus.REJECTED.value in verdicts:
        status = ArtifactReviewStatus.REJECTED
    elif ArtifactReviewStatus.NEEDS_REVISION.value in verdicts:
        status = ArtifactReviewStatus.NEEDS_REVISION
    elif len(verdicts) == len(assignments) and all(
        v == ReviewOverall.APPROVED.value for v in verdicts
    ):
        status = ArtifactReviewStatus.REVIEW_PASSED
    else:
        status = ArtifactReviewStatus.UNVERIFIED

    if target.review_status != status:
        target.review_status = status
        session.add(target)
        append_audit(
            session,
            actor=actor,
            action="artifact.review_status_aggregated",
            resource_type="artifact",
            resource_id=artifact_id,
            project_id=target.project_id,
            task_id=target.task_id,
            before={},
            after={"review_status": status.value},
            idempotency_key=f"audit:aggregate:{artifact_id}:{current_round}",
        )
        # C1: only when the gate has PASSED do we open the owner final-approval
        # gate. We never set APPROVED here -- that is the owner's exclusive call.
        if status == ArtifactReviewStatus.REVIEW_PASSED:
            _ensure_review_gate_approval(
                session, target, policy_id=policy.id, actor=actor
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
    review_round: int = 1,
    review_task_id: str | None = None,
    review_dimension: str | None = None,
    review_artifact_id: str | None = None,
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
        review_round=review_round,
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
        # Durable binding provenance (req 6): anchored to the trusted Review Task.
        review_task_id=review_task_id,
        review_round=review_round,
        review_dimension=review_dimension,
    )

    # C3 / Option A provenance (req 3): the independent Review Artifact that
    # produced this verdict is persisted DIRECTLY on the result via
    # ``review_artifact_id`` (unique, server-owned). We deliberately do NOT stash
    # the link in the target Artifact's mutable ``metadata_json`` -- a Review
    # Artifact maps to exactly one ReviewResult, resolved from the result row.
    # The binding runtime path (submit_review_from_artifact) always supplies it;
    # when a binding is in play (review_task_id set) it is mandatory.
    #
    # Pairing invariant (req 3 / DB-integrity follow-up): ``review_task_id`` and
    # ``review_artifact_id`` are a coupled provenance pair -- they must be BOTH
    # non-null (a runtime binding verdict) or BOTH null (legacy pre-runtime
    # ReviewResult, kept for backward compatibility). A partial pair is rejected.
    # NOTE: a DB-level CHECK cannot be added safely here -- a table-level CHECK
    # requires recreating ``review_result`` (batch_alter_table), which would
    # rename ``artifact`` and break the knowledge_candidate_validate_insert
    # trigger; a column-level CHECK cannot express the bidirectional pair
    # because the two columns are added by separate ALTER statements. We enforce
    # the invariant at the service layer instead (DB CHECK was explicitly NOT
    # added to avoid expanding the migration beyond its scope).
    if (review_task_id is None) != (review_artifact_id is None):
        raise ReviewError(
            422,
            "review_task_id 与 review_artifact_id 必须同时非空（runtime 绑定溯源）"
            "或同时为空（兼容旧非 runtime ReviewResult），不允许部分成对",
        )
    result.review_artifact_id = review_artifact_id
    # Stage the result only AFTER the provenance guard passes, so a rejected
    # (partial-pair) call never leaves a stale pending object in the session
    # that a later call could autoflush into a FK/unique violation.
    session.add(result)

    # --- reflect outcome: deterministic aggregation when policy supplied ---
    if policy is not None:
        new_status = aggregate_reviews(session, artifact_id, policy, actor=actor)
    else:
        # Legacy / untracked path: record verdict but NEVER auto-approve (C1).
        # A single reviewer must not promote an artifact to APPROVED; production
        # wiring always passes `policy` so multi-reviewer aggregation + the owner
        # gate are enforced. The artifact stays UNVERIFIED pending a policy-bound
        # review (or an explicit owner action via the dedicated endpoint).
        new_status = ArtifactReviewStatus.UNVERIFIED
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
    try:
        session.commit()
    except IntegrityError:
        # Concurrent submit with the same identity (artifact+reviewer+policy+round):
        # the DB unique constraint on ``idempotency_key`` (and ``review_artifact_id``)
        # resolves the race. Roll back and converge to the committed row.
        session.rollback()
        dupe = session.exec(
            select(ReviewResult).where(ReviewResult.idempotency_key == idem_key)
        ).first()
        if dupe is not None:
            new_sig = _content_signature(
                dimensions, overall, reviewer_score, usefulness
            )
            old_sig = _content_signature(
                dupe.dimensions, dupe.overall, dupe.reviewer_score, dupe.usefulness
            )
            if new_sig == old_sig:
                session.refresh(dupe)
                return dupe
            raise ReviewError(
                409, "该评审者对此 Artifact+Policy 的评审已存在，冲突禁止覆盖"
            ) from None
        raise
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


# --- Runtime wiring for the Review Protocol (#69 / C2-C5) -------------------
# Every identity/target below is server-assigned and persisted. They are NEVER
# trusted from agent output, prompt text, or the client request (C2/C3).

REVIEW_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "overall": {"type": "string", "enum": [v.value for v in ReviewOverall]},
        "reviewer_score": {"type": "number"},
        "dimensions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dim": {"type": "string"},
                    "verdict": {"type": "string", "enum": [v.value for v in ReviewVerdict]},
                    "evidence": {"type": "string"},
                    "score": {"type": "number"},
                },
                "required": ["dim", "verdict"],
            },
        },
    },
    "required": ["overall"],
}


def _ensure_review_gate_approval(
    session: Session,
    artifact: Artifact,
    *,
    policy_id: str | None = None,
    actor: str = "owner",
) -> None:
    """Create/maintain the single pending Owner Approval for the review gate (C1/C5).

    Bound to the exact (target_artifact_id, review_policy_id, review_round) so an
    old Approval can never approve a new revision round (req 5). Idempotent within
    a round: if a pending ``review_gate`` Approval already exists for this exact
    binding, it is left untouched. The artifact is only ever promoted to APPROVED
    by an explicit owner action (``owner_approve_review``) -- AI reviewers can
    never substitute for the owner final approval.
    """
    current_round = artifact.revision_count + 1
    existing = session.exec(
        select(Approval).where(
            Approval.target_artifact_id == artifact.id,
            Approval.review_policy_id == policy_id,
            Approval.review_round == current_round,
            Approval.action_type == "review_gate",
            Approval.status == ApprovalStatus.PENDING,
        )
    ).first()
    if existing is not None:
        return
    # DB backstop (req 4/5): the unique index uq_approval_gate_round rejects a
    # concurrent duplicate insert for the same (target, policy, round). We wrap the
    # insert in a SAVEPOINT so a rare race rolls back ONLY this duplicate gate (and
    # its audit), leaving the outer transaction (ReviewResult + artifact status)
    # intact. The surviving gate is the other transaction's committed Approval.
    try:
        with session.begin_nested():
            session.add(
                Approval(
                    project_id=artifact.project_id,
                    task_id=artifact.task_id,
                    action_type="review_gate",
                    risk_level=RiskLevel.L2,
                    status=ApprovalStatus.PENDING,
                    rationale="review gate passed; awaiting owner final approval",
                    target_artifact_id=artifact.id,
                    review_policy_id=policy_id,
                    review_round=current_round,
                )
            )
            append_audit(
                session,
                actor=actor,
                action="review.gate_passed",
                resource_type="artifact",
                resource_id=artifact.id,
                project_id=artifact.project_id,
                task_id=artifact.task_id,
                before={"review_status": ArtifactReviewStatus.REVIEW_PASSED.value},
                after={"review_gate": "pending_owner_approval", "review_round": current_round},
                idempotency_key=f"audit:review:gate:{artifact.id}:{current_round}",
            )
    except IntegrityError:
        return


def owner_approve_review(
    session: Session, *, artifact_id: str, actor: str = "owner"
) -> Artifact:
    """Owner final gate (C1): the ONLY path that sets a reviewed artifact to APPROVED.

    Requires the artifact to be in ``REVIEW_PASSED`` (all required reviewers
    passed and the gate opened). The corresponding pending ``review_gate``
    Approval is marked APPROVED. AI reviewers can never reach this function.
    """
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise ServiceError(404, "Artifact not found")
    if artifact.review_status == ArtifactReviewStatus.APPROVED:
        return artifact  # idempotent no-op
    if artifact.review_status != ArtifactReviewStatus.REVIEW_PASSED:
        raise ServiceError(
            409,
            "只有处于 review_passed 状态的 Artifact 才能由 owner 最终批准"
            "（AI 评审者永不能替代 owner 最终批准）",
        )
    current_round = artifact.revision_count + 1
    # Resolve the governing policy from the exact round's binding (req 5).
    assignment = session.exec(
        select(ReviewAssignment).where(
            ReviewAssignment.target_artifact_id == artifact_id,
            ReviewAssignment.review_round == current_round,
        )
    ).first()
    policy_id = assignment.review_policy_id if assignment is not None else None
    gate = session.exec(
        select(Approval).where(
            Approval.target_artifact_id == artifact_id,
            Approval.review_policy_id == policy_id,
            Approval.review_round == current_round,
            Approval.action_type == "review_gate",
            Approval.status == ApprovalStatus.PENDING,
        )
    ).first()
    if gate is None:
        raise ServiceError(409, "review gate 未通过，无法最终批准")
    gate.status = ApprovalStatus.APPROVED
    gate.decided_at = now_utc()
    gate.rationale = "owner final approval"
    session.add(gate)
    before_status = artifact.review_status
    artifact.review_status = ArtifactReviewStatus.APPROVED
    session.add(artifact)
    append_audit(
        session,
        actor=actor,
        action="review.owner_approved",
        resource_type="artifact",
        resource_id=artifact.id,
        project_id=artifact.project_id,
        task_id=artifact.task_id,
        before={"review_status": before_status.value},
        after={"review_status": ArtifactReviewStatus.APPROVED.value},
        idempotency_key=f"audit:review:owner_approve:{artifact.id}",
    )
    session.commit()
    session.refresh(artifact)
    return artifact


def _select_reviewers(
    session: Session,
    policy: ReviewPolicy,
    *,
    executor_agent_id: str | None,
    limit: int,
) -> list[Agent]:
    """Server-determined reviewer selection (C2).

    Picks up to ``limit`` distinct enabled agents that satisfy the policy's trust
    floor + (optional) capability extension, excluding the producer (no
    self-review). Selection is purely server-side -- never from client/output.
    """
    agents = list(session.exec(select(Agent).order_by(Agent.id)))
    chosen: list[Agent] = []
    for agent in agents:
        if agent.id == executor_agent_id:
            continue
        try:
            assert_reviewer_independence(policy, None, agent, ReviewReviewerType.AGENT)
        except ReviewError:
            continue
        chosen.append(agent)
        if len(chosen) >= limit:
            break
    return chosen


def create_review_task(
    session: Session,
    *,
    target_artifact_id: str,
    policy_id: str,
    required_capabilities: list[str],
    dimensions: list[str],
    reviewer_agent_id: str,
    review_round: int,
    executor_agent_id: str | None = None,
    dimension: str | None = None,
) -> Task:
    """Create one server-bound Review Task for a target artifact (C2/C3).

    The Review Task is persisted with a FIXED ``assigned_agent_id`` = the trusted
    reviewer and a ``depends_on`` link to the content task that produced the
    target artifact. The reviewer identity is NEVER taken from the client.

    Crucially, the exact binding (target artifact / policy / round / reviewer /
    single dimension) is recorded in the immutable ``ReviewAssignment`` table --
    the single source of truth (req 1/2/6). It is NOT stashed in mutable
    ``Artifact.metadata_json``.
    """
    target = session.get(Artifact, target_artifact_id)
    if target is None:
        raise ServiceError(404, "Target artifact not found")
    content_task_id = target.task_id
    primary_dim = dimension or (dimensions[0] if dimensions else "general")
    task = Task(
        project_id=target.project_id,
        title=f"Review {target_artifact_id} R{review_round} by {reviewer_agent_id}",
        description=f"Independent review of {target_artifact_id} (round {review_round})",
        status=TaskStatus.READY,
        assigned_agent_id=reviewer_agent_id,
        routing_mode=RoutingMode.FIXED,
        adapter_type=AdapterType.EXTERNAL,
        required_capabilities=list(required_capabilities),
        output_schema=REVIEW_VERDICT_SCHEMA,
        depends_on=[content_task_id] if content_task_id else [],
    )
    session.add(task)
    # Flush the Task NOW so its generated id is persisted before the immutable
    # ReviewAssignment row (which FK-references task.id) is added. Without this,
    # SQLAlchemy batches the two ReviewAssignment inserts and may flush them
    # before the second Task is inserted, tripping the review_task_id FK. This is
    # a flush (not a commit); the caller still commits once after all bindings.
    session.flush([task])
    # Immutable server-owned binding row (1:1 with the Review Task). This is the
    # durable source of truth -- never client-supplied, never mutable metadata.
    session.add(
        ReviewAssignment(
            review_task_id=task.id,
            target_artifact_id=target_artifact_id,
            review_policy_id=policy_id,
            review_round=review_round,
            reviewer_agent_id=reviewer_agent_id,
            review_dimension=primary_dim,
        )
    )
    append_audit(
        session,
        actor="system",
        action="review.task_created",
        resource_type="task",
        resource_id=task.id,
        project_id=target.project_id,
        task_id=content_task_id,
        before={},
        after={
            "review_target_artifact_id": target_artifact_id,
            "review_policy_id": policy_id,
            "review_round": review_round,
            "assigned_reviewer_agent_id": reviewer_agent_id,
            "review_dimension": primary_dim,
        },
        idempotency_key=f"audit:review:task:{task.id}",
    )
    # NOTE: no commit here. The caller (dispatch_reviews_for_artifact) commits
    # once after creating all bindings, so the shared ``policy`` ORM object is
    # never accessed across a commit (which would expire its attributes).
    return task


def dispatch_reviews_for_artifact(
    session: Session,
    *,
    target_artifact_id: str,
    policy: ReviewPolicy,
    executor_agent_id: str | None = None,
    review_round: int | None = None,
) -> list[Task]:
    """Server-dispatches the required number of Review Tasks for one artifact (C2).

    Reviewers are selected server-side by ``_select_reviewers`` -- the client
    never supplies them. ``review_round`` is server-derived (target revision_count
    + 1). Each Review Task is server-bound to EXACTLY ONE dimension (req 2), cycled
    across the policy's dimensions. The exact binding is persisted in the immutable
    ``ReviewAssignment`` table (req 1/6). Returns the created Review Tasks (one per
    selected reviewer). Idempotent: re-dispatching the same target+round returns
    the existing Review Tasks without creating duplicates (req 8).
    """
    target = session.get(Artifact, target_artifact_id)
    if target is None:
        raise ServiceError(404, "Target artifact not found")
    if review_round is None:
        review_round = target.revision_count + 1
    # Idempotent guard: if bindings already exist for this target + round, return
    # the existing Review Tasks (no duplicate dispatch).
    existing = list(
        session.exec(
            select(ReviewAssignment).where(
                ReviewAssignment.target_artifact_id == target_artifact_id,
                ReviewAssignment.review_round == review_round,
            )
        )
    )
    if existing:
        tasks = [session.get(Task, a.review_task_id) for a in existing]
        return [t for t in tasks if t is not None]
    # Extract policy primitives BEFORE creating tasks so we never access the ORM
    # ``policy`` object across a commit (which would expire its attributes).
    policy_id = policy.id
    required_capabilities = list(policy.required_capabilities)
    dimensions = list(policy.dimensions or [])
    if not dimensions:
        dimensions = ["general"]
    reviewers = _select_reviewers(
        session, policy, executor_agent_id=executor_agent_id, limit=policy.required_reviewers
    )
    if len(reviewers) < policy.required_reviewers:
        raise ServiceError(
            422,
            f"可选合格评审者不足：需要 {policy.required_reviewers}，"
            f"实际 {len(reviewers)}（检查信任级别/能力/启用状态）",
        )
    tasks: list[Task] = []
    for i, reviewer in enumerate(reviewers):
        # Exactly one dimension per Review Task (req 2): cycle across dimensions.
        dim = dimensions[i % len(dimensions)]
        tasks.append(
            create_review_task(
                session,
                target_artifact_id=target_artifact_id,
                policy_id=policy_id,
                required_capabilities=required_capabilities,
                dimensions=dimensions,
                reviewer_agent_id=reviewer.id,
                review_round=review_round,
                executor_agent_id=executor_agent_id,
                dimension=dim,
            )
        )
    try:
        session.commit()
    except IntegrityError:
        # Concurrent dispatch for the same target+round: the 5-tuple unique index
        # (uq_review_assignment_binding) rejects the duplicate binding. Roll back
        # the uncommitted (orphan) tasks and converge to the already-committed
        # binding row's Task. The caller receives the canonical Review Tasks.
        session.rollback()
        existing = list(
            session.exec(
                select(ReviewAssignment).where(
                    ReviewAssignment.target_artifact_id == target_artifact_id,
                    ReviewAssignment.review_round == review_round,
                )
            ).all()
        )
        if existing:
            tasks = [session.get(Task, a.review_task_id) for a in existing]
            return [t for t in tasks if t is not None]
        raise
    for t in tasks:
        session.refresh(t)
    return tasks


def _bound_review_task(session: Session, artifact_id: str) -> Task | None:
    """Find a server-bound Review Task for the given target artifact (C2/C3).

    The binding lives in the immutable ``ReviewAssignment`` table (written by
    ``dispatch_reviews_for_artifact``); we never trust client-supplied task
    metadata. Returns the first bound Review Task, if any.
    """
    assignment = session.exec(
        select(ReviewAssignment).where(
            ReviewAssignment.target_artifact_id == artifact_id
        )
    ).first()
    if assignment is None:
        return None
    return session.get(Task, assignment.review_task_id)


def submit_review_from_artifact(
    session: Session,
    *,
    review_task_id: str,
    actor: str = "owner",
) -> ReviewResult:
    """Map a completed Review Task's Artifact into a trusted ReviewResult (C3).

    The reviewer agent executed via the existing ``execute_task`` and produced an
    independent Review Artifact. This server-side step:
      - obtains ``reviewer_agent_id`` from the TRUSTED task assignment, never the
        artifact output or client;
      - resolves the EXACT target artifact from the immutable ``ReviewAssignment``
        (never "content task -> latest artifact");
      - enforces that the reviewer output only submits the ASSIGNED dimension (req 2);
      - calls ``submit_review`` (writing the target Artifact's ReviewResult, linked
        durably to this Review Task via ``review_task_id``).
    It rejects any Review Task that is not server-bound (no client-supplied
    reviewer/target identity is ever accepted).
    """
    review_task = session.get(Task, review_task_id)
    if review_task is None:
        raise ServiceError(404, "Review task not found")
    reviewer_agent_id = review_task.assigned_agent_id
    if reviewer_agent_id is None:
        raise ServiceError(422, "Review Task 缺少可信 assigned_reviewer_agent_id")
    # Trusted binding: the exact target / policy / round / dimension come from the
    # immutable ReviewAssignment, keyed by the Review Task id (req 1/2/6).
    assignment = session.get(ReviewAssignment, review_task_id)
    if assignment is None:
        raise ServiceError(422, "该 Task 不是由服务端绑定的 Review Task")
    target_artifact_id = assignment.target_artifact_id
    review_round = assignment.review_round
    review_dimension = assignment.review_dimension
    policy_id = assignment.review_policy_id
    policy = session.get(ReviewPolicy, policy_id)
    if policy is None:
        raise ServiceError(404, "Review policy not found")

    # The independent Review Artifact produced by execute_task for this task.
    review_artifact = session.exec(
        select(Artifact)
        .where(Artifact.task_id == review_task_id)
        .order_by(Artifact.created_at)
    ).first()
    if review_artifact is None:
        raise ServiceError(422, "Review Task 尚未产出 Review Artifact（请先执行）")
    stored = (review_artifact.metadata_json or {}).get("artifacts") or []
    if not stored:
        raise ServiceError(422, "Review Artifact 缺少结构化输出")
    data = stored[0].get("data") or {}
    overall_raw = data.get("overall")
    overall = ReviewOverall(overall_raw) if overall_raw else None
    if overall is None:
        raise ServiceError(422, "Review 输出缺少 overall verdict")
    dimensions = data.get("dimensions") or []
    # Req 2: the reviewer output may ONLY submit the assigned dimension. Forging a
    # different dimension (e.g. claiming a dimension it was not bound to) is rejected.
    for d in dimensions:
        if d.get("dim") != review_dimension:
            raise ReviewError(
                422,
                f"评审者只能提交其被绑定的维度 {review_dimension!r}，"
                f"不允许提交 {d.get('dim')!r}",
            )
    reviewer_score = data.get("reviewer_score")

    # Executor identity for the independence check (producer must not self-review).
    content_task = session.get(Task, review_task.depends_on[0]) if review_task.depends_on else None
    executor_agent_id = content_task.assigned_agent_id if content_task else None

    return submit_review(
        session,
        artifact_id=target_artifact_id,
        reviewer_type=ReviewReviewerType.AGENT,
        reviewer_agent_id=reviewer_agent_id,
        dimensions=dimensions,
        overall=overall,
        reviewer_score=reviewer_score,
        policy=policy,
        executor_agent_id=executor_agent_id,
        review_round=review_round,
        review_task_id=review_task_id,
        review_dimension=review_dimension,
        review_artifact_id=review_artifact.id,
        actor=actor,
    )


def has_bound_review_tasks(session: Session, task_id: str) -> bool:
    """True if the task's produced artifact has any server-bound Review Task (C5)."""
    artifact = session.exec(
        select(Artifact)
        .where(Artifact.task_id == task_id)
        .order_by(Artifact.created_at)
    ).first()
    if artifact is None:
        return False
    return (
        session.exec(
            select(ReviewAssignment).where(
                ReviewAssignment.target_artifact_id == artifact.id
            )
        ).first()
        is not None
    )


def _invalidate_review_gate(
    session: Session, source: Artifact, *, actor: str = "owner"
) -> None:
    """Close any pending owner review-gate Approval for ``source`` (req 4/5).

    A revision invalidates the prior gate: the old pending ``review_gate``
    Approval is marked REJECTED so it can never be used to approve the upcoming
    new revision round. The caller marks the source artifact NEEDS_REVISION.
    """
    gate = session.exec(
        select(Approval).where(
            Approval.target_artifact_id == source.id,
            Approval.action_type == "review_gate",
            Approval.status == ApprovalStatus.PENDING,
        )
    ).first()
    if gate is not None:
        gate.status = ApprovalStatus.REJECTED
        gate.decided_at = now_utc()
        gate.rationale = "invalidated by owner revision request"
        session.add(gate)
        append_audit(
            session,
            actor=actor,
            action="review.gate_invalidated",
            resource_type="artifact",
            resource_id=source.id,
            project_id=source.project_id,
            task_id=source.task_id,
            before={"review_gate": "pending_owner_approval"},
            after={"review_gate": "invalidated_by_revision"},
            idempotency_key=f"audit:review:gate_invalidated:{source.id}",
        )


def request_review_revision(
    session: Session,
    *,
    task_id: str,
    feedback: str,
    actor: str = "owner",
    adapter: Any | None = None,
) -> Task:
    """Owner-requested revision -- PREPARE ONLY, never executes an LLM (req 4).

    This endpoint must NOT synchronously call ``execute_task`` or a remote LLM.
    It only:
      1. records the owner's real feedback durably (Event + Audit);
      2. idempotently creates/prepares ONE READY revision execution Task (the
         Content Agent runs it later via the existing execute endpoint);
      3. invalidates the old pending review-gate Approval and resets the source
         artifact to UNVERIFIED;
      4. returns.

    ``revision_count`` is server-derived (never taken from the client); concurrent
    or repeated calls do not create multiple revision Tasks/Artifacts/Approvals.
    """
    task = session.get(Task, task_id)
    if task is None:
        raise ServiceError(404, "Task not found")
    # The artifact to revise is the LATEST produced by this task.
    source = session.exec(
        select(Artifact)
        .where(Artifact.task_id == task_id)
        .order_by(Artifact.created_at.desc())
    ).first()
    if source is None:
        raise ServiceError(422, "Task 尚未产出 Artifact，无法修订")
    # Trusted binding: the policy comes from the immutable ReviewAssignment.
    assignment = session.exec(
        select(ReviewAssignment)
        .where(ReviewAssignment.target_artifact_id == source.id)
        .order_by(ReviewAssignment.review_round.desc())
    ).first()
    if assignment is None:
        raise ServiceError(422, "该 Artifact 没有绑定的 Review Task，无法确定修订策略")
    policy = session.get(ReviewPolicy, assignment.review_policy_id)
    if policy is None:
        raise ServiceError(422, "无法确定 ReviewPolicy，无法准备修订")

    # 1. Durably record the owner's real revision reason.
    from aios.services import append_event

    append_event(
        session,
        project_id=task.project_id,
        task_id=task.id,
        event_type="task.revision",
        idempotency_key=new_id("idem"),
        payload={"feedback": feedback, "via": "review_protocol"},
    )
    append_audit(
        session,
        actor=actor,
        action="task.revision_requested",
        resource_type="task",
        resource_id=task.id,
        project_id=task.project_id,
        task_id=task.id,
        before={"status": task.status.value},
        after={"status": TaskStatus.READY.value, "feedback": feedback},
        idempotency_key=f"audit:review:revision:{source.id}:{new_id('idem')}",
    )

    # 2. Invalidate the old pending gate + mark source NEEDS_REVISION (req 5).
    #    Intentionally NOT a meaningless UNVERIFIED reset: the source artifact is
    #    now in a terminal "revision requested" state until the Content Agent
    #    produces the next revision. The audit records gate invalidation below.
    _invalidate_review_gate(session, source, actor=actor)
    if source.review_status != ArtifactReviewStatus.NEEDS_REVISION:
        source.review_status = ArtifactReviewStatus.NEEDS_REVISION
        session.add(source)

    # 3. Idempotently prepare ONE READY revision execution (NO execute_task / LLM).
    #    Server-derived revision count (max over the task lineage + 1).
    max_rc = session.exec(
        select(func.max(Artifact.revision_count)).where(Artifact.task_id == task_id)
    ).first()
    new_count = (max_rc or 0) + 1

    # Escalation branch: cap already reached -> dedup owner Approval, no revision.
    if new_count > policy.max_revisions:
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
                        f"修订次数 {source.revision_count} 已达策略上限 "
                        f"{policy.max_revisions}，升级 owner 决策"
                    ),
                )
            )
            append_audit(
                session,
                actor=actor,
                action="revision.escalated",
                resource_type="artifact",
                resource_id=source.id,
                project_id=source.project_id,
                task_id=task_id,
                before={"revision_count": source.revision_count},
                after={"max_revisions": policy.max_revisions, "escalated": True},
                idempotency_key=f"audit:revision:escalate:{source.id}",
            )
        session.commit()
        session.refresh(task)
        return task

    # Idempotent (prepare-only): the durable dedup anchor is a SERVER-DETERMINED
    # structured idempotency key (req 1) -- ``review-revision:{source.id}:{round}``
    # -- NOT the Task title (a display field, never a stable identity). Two normal
    # requests return the SAME revision Task; concurrent requests converge via the
    # DB unique constraint on ``task.idempotency_key``. The owner's reason does NOT
    # participate in identity but is persisted into the revision Task's description
    # (the revision input the Content Agent receives).
    rev_idem = f"review-revision:{source.id}:{new_count}"
    existing_prepared = session.exec(
        select(Task).where(Task.idempotency_key == rev_idem)
    ).first()
    if existing_prepared is not None:
        if existing_prepared.status != TaskStatus.READY:
            existing_prepared.status = TaskStatus.READY
            existing_prepared.updated_at = now_utc()
            session.add(existing_prepared)
        session.commit()
        session.refresh(existing_prepared)
        return existing_prepared

    # Create ONE new READY revision Task (the Content Agent runs it later via the
    # existing execute endpoint). No LLM is invoked here.
    rev_task = Task(
        project_id=task.project_id,
        title=f"Revise {source.id} (round {new_count})",  # display only; identity = idempotency_key
        description=f"Owner-requested revision of {source.id}: {feedback}",
        status=TaskStatus.READY,
        assigned_agent_id=task.assigned_agent_id,
        routing_mode=task.routing_mode,
        adapter_type=task.adapter_type,
        required_capabilities=list(task.required_capabilities),
        output_schema=task.output_schema,
        depends_on=[task_id],
        estimated_cost=task.estimated_cost,
        idempotency_key=rev_idem,
    )
    session.add(rev_task)
    append_audit(
        session,
        actor=actor,
        action="revision.prepared",
        resource_type="artifact",
        resource_id=source.id,
        project_id=source.project_id,
        task_id=task_id,
        before={"revision_of": None, "revision_count": 0},
        after={"revision_of": source.id, "revision_count": new_count},
        idempotency_key=f"audit:revision:prepared:{source.id}:{new_count}",
    )
    try:
        session.commit()
    except IntegrityError:
        # Concurrent revision request for the same (source, round): the unique
        # ``task.idempotency_key`` rejects the duplicate insert; converge to the
        # winning Task rather than raising.
        session.rollback()
        winner = session.exec(
            select(Task).where(Task.idempotency_key == rev_idem)
        ).first()
        if winner is not None:
            if winner.status != TaskStatus.READY:
                winner.status = TaskStatus.READY
                winner.updated_at = now_utc()
                session.add(winner)
                session.commit()
            session.refresh(winner)
            return winner
        raise
    session.refresh(rev_task)
    return rev_task
