"""V1-I4: distribution package assembly + human publish-gate (Issue #35).

The owner's approved platform outputs (T3 WeChat / T4 Xiaohongshu / T5 short-video)
are bundled into ONE distribution ``Artifact`` (type JSON) whose ``metadata_json``
lists the source artifacts + a ``publish_approval_id``. A single L3 ``Approval`` on
the publish-gate task guards readiness: the package is only marked ready
(``review_status == APPROVED``) inside the same transaction that approves the L3
gate. The owner copies the content and posts by hand.

Hard rules (Issue #35): NO network publish; nothing ever emits an ``external.publish``
event; NO new models/migrations (reuse Artifact + metadata_json, Approval + L3,
AuditLog, Orchestrator). The T1..T9 ``key`` is NOT persisted and ``packaging`` sits on
both T3 and T7, so the package/publish-gate tasks are resolved by DEPENDENCY-GRAPH
topology, robust against title edits and capability changes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlmodel import Session, select

from aios.actor import ActorContext, _assert_owner_actor, resolve_owner_actor
from aios.audit import append_audit
from aios.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    RiskLevel,
    RoutingMode,
    Task,
    TaskStatus,
    now_utc,
)
from aios.orchestrator import Orchestrator
from aios.schemas import ApprovalCreate
from aios.services import ServiceError, append_event, create_approval

PUBLISH_GATE_ACTION = "publish_gate"


def _package_result_id(package_task_id: str) -> str:
    """Deterministic, unique id for a project's single distribution package."""
    return f"package:{package_task_id}"


# --- Graph resolution (title/key/capability-independent) -----------------------


def resolve_publish_gate_task(session: Session, project_id: str) -> Task | None:
    """The publish-gate task = the MANUAL task that depends on exactly one FIXED task.

    In the V1 graph two tasks are MANUAL: the human review (T6, depends on T3/T4/T5 ->
    3 deps) and the publish gate (T8, depends on the packaging task T7 -> 1 FIXED dep).
    That single-FIXED-dependency shape uniquely identifies the publish gate without
    relying on the (non-persisted) T-key or the Chinese title.
    """
    tasks = list(session.exec(select(Task).where(Task.project_id == project_id)))
    by_id = {task.id: task for task in tasks}
    candidates: list[Task] = []
    for task in tasks:
        if task.routing_mode != RoutingMode.MANUAL:
            continue
        if len(task.depends_on) != 1:
            continue
        dep = by_id.get(task.depends_on[0])
        if dep is not None and dep.routing_mode != RoutingMode.MANUAL:
            candidates.append(task)
    return candidates[0] if len(candidates) == 1 else None


def resolve_package_task(session: Session, project_id: str) -> Task | None:
    """The packaging task (T7) = the single dependency of the publish-gate task."""
    gate = resolve_publish_gate_task(session, project_id)
    if gate is None or not gate.depends_on:
        return None
    return session.get(Task, gate.depends_on[0])


def resolve_platform_source_tasks(session: Session, project_id: str) -> list[Task]:
    """The platform outputs (T3/T4/T5) = deps of the packaging task's review dep (T6)."""
    package_task = resolve_package_task(session, project_id)
    if package_task is None or not package_task.depends_on:
        return []
    review = session.get(Task, package_task.depends_on[0])
    if review is None:
        return []
    sources: list[Task] = []
    for task_id in review.depends_on:
        source = session.get(Task, task_id)
        if source is not None:
            sources.append(source)
    return sources


def is_publish_gate_task(session: Session, task_id: str) -> bool:
    task = session.get(Task, task_id)
    if task is None:
        return False
    gate = resolve_publish_gate_task(session, task.project_id)
    return gate is not None and gate.id == task_id


def is_package_task(session: Session, task_id: str) -> bool:
    task = session.get(Task, task_id)
    if task is None:
        return False
    package_task = resolve_package_task(session, task.project_id)
    return package_task is not None and package_task.id == task_id


def _latest_artifact(session: Session, task_id: str) -> Artifact | None:
    return session.exec(
        select(Artifact)
        .where(Artifact.task_id == task_id)
        .order_by(Artifact.created_at.desc(), Artifact.id.desc())
    ).first()


def get_package_artifact(session: Session, project_id: str) -> Artifact | None:
    package_task = resolve_package_task(session, project_id)
    if package_task is None:
        return None
    return session.exec(
        select(Artifact).where(
            Artifact.external_result_id == _package_result_id(package_task.id)
        )
    ).first()


def _pending_publish_approval(
    session: Session, project_id: str, gate_task_id: str
) -> Approval | None:
    return session.exec(
        select(Approval)
        .where(
            Approval.project_id == project_id,
            Approval.task_id == gate_task_id,
            Approval.action_type == PUBLISH_GATE_ACTION,
            Approval.status == ApprovalStatus.PENDING,
        )
        .order_by(Approval.requested_at.desc())
    ).first()


# --- Package assembly (T7) -----------------------------------------------------


def assemble_distribution_package(
    session: Session, project_id: str, idempotency_key: str
) -> Artifact:
    """Bundle the T3/T4/T5 outputs into one distribution ``Artifact`` and open the L3 gate.

    Deterministic (NOT an LLM call): the platform outputs already exist, so this only
    references them. Atomic: the L3 PENDING approval, the package artifact and the
    packaging task's completion land in ONE commit (create_approval(commit=False) +
    complete_task's commit flush everything together); the publish gate is then
    unlocked via the orchestrator (mirrors execution.py's state+unlock pattern).
    """
    package_task = resolve_package_task(session, project_id)
    gate_task = resolve_publish_gate_task(session, project_id)
    if package_task is None or gate_task is None:
        raise ServiceError(404, "未找到打包任务或发布闸门任务（campaign 结构不完整）")

    result_id = _package_result_id(package_task.id)
    # Idempotency: the project's single package already exists -> return it.
    existing = session.exec(
        select(Artifact).where(Artifact.external_result_id == result_id)
    ).first()
    if existing is not None:
        return existing

    # Resolve and cite each platform output (T3/T4/T5).
    sources: list[dict[str, Any]] = []
    for source_task in resolve_platform_source_tasks(session, project_id):
        artifact = _latest_artifact(session, source_task.id)
        if artifact is None:
            raise ServiceError(
                409,
                f"「{source_task.title}」尚无产出，无法打包。请先完成三个平台任务再生成分发包。",
            )
        summary = None
        if isinstance(artifact.metadata_json, dict):
            summary = artifact.metadata_json.get("summary")
        sources.append(
            {
                "task_id": source_task.id,
                "task_title": source_task.title,
                "artifact_id": artifact.id,
                "type": artifact.type.value,
                "checksum": artifact.checksum,
                "summary": summary,
            }
        )
    if not sources:
        raise ServiceError(409, "未解析到任何平台产出（T3/T4/T5），无法打包。")

    # L3 publish gate on the gate task (created eagerly so the package can cite it).
    approval = create_approval(
        session,
        ApprovalCreate(
            project_id=project_id,
            task_id=gate_task.id,
            action_type=PUBLISH_GATE_ACTION,
            risk_level=RiskLevel.L3,
            rationale=None,
        ),
        idempotency_key=f"publish-gate:{gate_task.id}",
        commit=False,
    )

    checksum = hashlib.sha256(
        json.dumps(
            {"sources": sources, "publish_approval_id": approval.id},
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()

    package = Artifact(
        project_id=project_id,
        task_id=package_task.id,
        type=ArtifactType.JSON,
        uri=f"package://{project_id}/{package_task.id}",
        checksum=checksum,
        external_result_id=result_id,
        result_checksum=checksum,
        review_status=ArtifactReviewStatus.UNVERIFIED,
        metadata_json={
            "kind": "distribution_package",
            "actor": "distribution",
            "summary": "分发包：合并 T3/T4/T5 平台产出，等待发布审批（L3）后由所有者手动发布。",
            "publish_approval_id": approval.id,
            "sources": sources,
        },
    )
    try:
        session.add(package)
        append_audit(
            session,
            actor="distribution",
            action="artifact.created",
            resource_type="artifact",
            resource_id=package.id,
            project_id=project_id,
            task_id=package_task.id,
            before={},
            after={
                "external_result_id": result_id,
                "publish_approval_id": approval.id,
                "source_count": len(sources),
            },
            idempotency_key=f"audit:package:{package.id}",
        )
        # Complete the packaging task; its internal commit flushes the approval +
        # package artifact + T7 DONE together as one atomic unit.
        from aios.orchestrator import complete_task

        complete_task(session, package_task.id, f"package:{package_task.id}:complete")
        # Unlock the publish gate (T7 DONE -> T8 READY).
        Orchestrator(session).process_pending()
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(package)
    return package


# --- Publish gate decision (T8) ------------------------------------------------


def decide_publish_gate(
    session: Session,
    project_id: str,
    decision: ApprovalStatus,
    rationale: str | None = None,
    *,
    actor: ActorContext | None = None,
) -> Approval:
    """Owner decision on the L3 publish gate (single approve/reject button).

    APPROVED (atomic, one commit): approval -> APPROVED, gate task -> DONE
    (emits task.completed so T9 unlocks), package Artifact.review_status -> APPROVED
    (ready). REJECTED: approval -> REJECTED, gate task -> REVIEW, package stays
    UNVERIFIED (not ready). Publishing without an assembled package or a PENDING L3
    approval is rejected (409) -- there is no path that marks the package ready
    without an APPROVED L3 gate, and NO ``external.publish`` event is ever emitted.

    Owner-only (#74): the ``actor`` must be a trusted owner ``ActorContext``
    produced by ``authenticate_owner``.
    """
    if actor is None:
        actor = resolve_owner_actor()
    _assert_owner_actor(actor)
    audit_actor = actor.owner_id or "owner"
    if decision not in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
        raise ServiceError(400, "发布决策只能是批准或驳回。")

    gate_task = resolve_publish_gate_task(session, project_id)
    if gate_task is None:
        raise ServiceError(404, "未找到发布闸门任务。")
    package = get_package_artifact(session, project_id)
    approval = _pending_publish_approval(session, project_id, gate_task.id)
    # Issue AC: publishing without an L3 approval (and package) is rejected.
    if package is None or approval is None:
        raise ServiceError(409, "尚未生成分发包或发布审批，无法发布。请先在看板生成分发包。")

    approval.status = decision
    approval.decided_at = now_utc()
    approval.rationale = rationale
    try:
        session.add(approval)
        append_audit(
            session,
            actor=audit_actor,
            action="approval.decided",
            resource_type="approval",
            resource_id=approval.id,
            project_id=project_id,
            task_id=gate_task.id,
            before={"status": ApprovalStatus.PENDING.value},
            after={"status": decision.value, "rationale": rationale},
            idempotency_key=f"audit:publish:{approval.id}:{decision.value}",
        )
        if decision == ApprovalStatus.APPROVED:
            before = gate_task.status
            gate_task.status = TaskStatus.DONE
            gate_task.updated_at = now_utc()
            session.add(gate_task)
            append_event(
                session,
                project_id=project_id,
                task_id=gate_task.id,
                event_type="task.completed",
                idempotency_key=f"publish:{approval.id}:gate-done",
                payload={
                    "before": before.value,
                    "after": TaskStatus.DONE.value,
                    "via": "publish_gate",
                },
            )
            append_audit(
                session,
                actor=audit_actor,
                action="task.completed",
                resource_type="task",
                resource_id=gate_task.id,
                project_id=project_id,
                task_id=gate_task.id,
                before={"status": before.value},
                after={"status": TaskStatus.DONE.value},
                idempotency_key=f"audit:publish:{approval.id}:gate-done",
            )
            # Mark the package READY in the SAME transaction as the L3 approval.
            pkg_before = package.review_status
            package.review_status = ArtifactReviewStatus.APPROVED
            session.add(package)
            append_audit(
                session,
                actor=audit_actor,
                action="artifact.review_status",
                resource_type="artifact",
                resource_id=package.id,
                project_id=project_id,
                task_id=package.task_id,
                before={"review_status": pkg_before.value},
                after={"review_status": ArtifactReviewStatus.APPROVED.value},
                idempotency_key=f"audit:publish:{approval.id}:package-ready",
            )
        else:  # REJECTED: return the gate for rework; package stays NOT ready.
            before = gate_task.status
            gate_task.status = TaskStatus.REVIEW
            gate_task.updated_at = now_utc()
            session.add(gate_task)
            append_audit(
                session,
                actor=audit_actor,
                action="task.returned",
                resource_type="task",
                resource_id=gate_task.id,
                project_id=project_id,
                task_id=gate_task.id,
                before={"status": before.value},
                after={"status": TaskStatus.REVIEW.value},
                idempotency_key=f"audit:publish:{approval.id}:gate-returned",
            )
        session.commit()
    except Exception:
        session.rollback()
        raise
    if decision == ApprovalStatus.APPROVED:
        # Unlock downstream (T8 DONE -> T9 READY); mirrors execution.py's unlock step.
        Orchestrator(session).process_pending()
    session.refresh(approval)
    return approval
