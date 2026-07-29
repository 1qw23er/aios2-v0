"""Agent Relay ingest (V4, #99/#101) -- agent-authenticated work-log intake.

Ports #77 (Agent Relay) into the unified platform: an agent pushes a work log
through ``POST /relay/work-logs`` using its self-update bearer credential. The
relay reuses the exact same UNVERIFIED-artifact creation + idempotency machinery
as the owner's ``submit_work_log`` (via ``WorkLogService._create_unverified_work_log``),
only substituting the authenticated agent identity for provenance and scoping
the idempotency namespace to the agent so different agents (and the owner) never
converge on each other's rows.

Trust chain preserved (#88/#93): the relay NEVER attests -- it only creates
UNVERIFIED logs; the owner still attests manually. Relay ingestion is FORBIDDEN
from setting KB eligibility: ``should_enter_kb`` is always forced to ``False``
here (never taken from the payload) and eligibility is decided by the owner at
attest time.
"""

from __future__ import annotations

from typing import Any

from aios.actor import ActorContext
from aios.audit import append_audit
from aios.models import Agent, Artifact, ArtifactReviewStatus, Project, Task
from aios.schemas import WorkLogSubmit
from aios.services import ServiceError
from aios.work_log import (
    _CONTENT_VALUE_RANK,
    REPORT_TYPES,
    WorkLogService,
    _request_fingerprint,
    _required_text,
    storage_idempotency_key,
)


def relay_work_log(
    session: Any,
    *,
    payload: WorkLogSubmit,
    idempotency_key: str,
    actor: ActorContext,
    agent: Agent,
) -> tuple[Artifact, bool]:
    """Ingest an UNVERIFIED work log on behalf of an authenticated agent.

    Returns ``(artifact, created)`` (same contract as ``submit_work_log``). The
    provenance is derived from the trusted agent actor -- the payload's
    ``produced_by_agent_id`` (if any) is ignored. Idempotency is scoped per
    agent so a replay returns 200 and a conflicting payload returns 409.

    Audit: writes ``relay.work_log_ingested`` (never ``work_log.submitted``).
    """
    if actor.kind != "agent" or not actor.agent_id:
        raise ServiceError(403, "relay requires an agent identity")

    # Provenance is derived from the authenticated agent (Gate A/D): a body
    # ``produced_by_agent_id`` that conflicts with the real actor is rejected
    # -- an agent may never impersonate another agent or the owner. A matching
    # or absent value is harmless and ignored in favour of the trusted identity.
    if (
        payload.produced_by_agent_id is not None
        and payload.produced_by_agent_id != agent.id
    ):
        raise ServiceError(
            422, "produced_by_agent_id must match the authenticated agent"
        )

    project_id = _required_text(payload.project_id, "project_id")
    project = session.get(Project, project_id)
    if project is None:
        raise ServiceError(422, "unknown project_id")

    task: Task | None = None
    if payload.task_ref:
        task = session.get(Task, payload.task_ref)
        if task is None:
            raise ServiceError(422, "unknown task_ref")
        if task.project_id != project_id:
            raise ServiceError(422, "task_ref does not belong to project_id")

    report_type = _required_text(payload.report_type, "report_type")
    if report_type not in REPORT_TYPES:
        raise ServiceError(422, "report_type must be 'daily' or 'retro'")
    what_done = _required_text(payload.what_done, "what_done")
    why = _required_text(payload.why, "why")
    problem = _required_text(payload.problem, "problem")
    solution = _required_text(payload.solution, "solution")
    new_knowledge = _required_text(payload.new_knowledge, "new_knowledge")
    client_key = _required_text(idempotency_key, "idempotency_key")
    content_value = payload.content_value
    if content_value is not None and content_value not in _CONTENT_VALUE_RANK:
        raise ServiceError(422, "content_value must be one of high|medium|low|none")
    # Trust boundary (Gate D / #88/#93): relay ingestion NEVER sets KB
    # eligibility -- the owner decides eligibility at attest time. Any caller
    # supplied ``should_enter_kb`` is ignored and forced to False here so a
    # relay caller cannot self-promote its logs into the knowledge base.
    should_enter_kb = False
    content_angle = payload.content_angle
    source_platform = payload.source_platform

    business_fields = {
        "project_id": project_id,
        "report_type": report_type,
        "task_ref": payload.task_ref,
        "what_done": what_done,
        "why": why,
        "problem": problem,
        "solution": solution,
        "new_knowledge": new_knowledge,
        "content_value": content_value,
        "should_enter_kb": should_enter_kb,
        "content_angle": content_angle,
        "source_platform": source_platform,
    }
    fingerprint = _request_fingerprint(business_fields)
    scope = f"agent:{actor.agent_id}"
    storage_key = storage_idempotency_key(project_id, client_key, scope=scope)

    artifact, created = WorkLogService(session)._create_unverified_work_log(
        project_id=project_id,
        report_type=report_type,
        what_done=what_done,
        why=why,
        problem=problem,
        solution=solution,
        new_knowledge=new_knowledge,
        task_ref=payload.task_ref,
        produced_by_agent_id=agent.id,
        source_platform=source_platform,
        content_value=content_value,
        should_enter_kb=should_enter_kb,
        content_angle=content_angle,
        task=task,
        agent=agent,
        provenance_assignment_id=None,
        legacy_assigned_agent=False,
        fingerprint=fingerprint,
        storage_key=storage_key,
        actor=actor,
        scope=scope,
    )
    if created:
        append_audit(
            session,
            actor=actor.derive_submitted_by(),
            action="relay.work_log_ingested",
            resource_type="artifact",
            resource_id=artifact.id,
            project_id=project_id,
            task_id=artifact.task_id,
            before={},
            after={
                "review_status": ArtifactReviewStatus.UNVERIFIED.value,
                "agent_id": agent.id,
                "source_platform": source_platform,
            },
            idempotency_key=f"audit:relay:ingest:{artifact.id}",
        )
        session.commit()
    return artifact, created
