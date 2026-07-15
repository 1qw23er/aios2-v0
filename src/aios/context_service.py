from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_
from sqlmodel import Session, select

from aios.audit import append_audit
from aios.models import (
    Agent,
    AgentCapability,
    Artifact,
    ArtifactReviewStatus,
    Capability,
    Decision,
    DecisionStatus,
    ExecutionAssignment,
    KnowledgeCandidate,
    KnowledgeFact,
    KnowledgeFactStatus,
    KnowledgeReviewDecision,
    Policy,
    Project,
    ReviewedFact,
    ReviewedFactStatus,
    Task,
    TaskContext,
    TaskStatus,
)
from aios.services import ServiceError

SENSITIVE_KEYS = {
    "api-key",
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
}


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else _safe(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


def _timestamp(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _reference(
    resource_type: str, resource_id: str, version: str, inclusion_reason: str
) -> dict[str, str]:
    return {
        "resource_type": resource_type,
        "resource_id": resource_id,
        "version": version,
        "inclusion_reason": inclusion_reason,
    }


def _latest_versions(rows: list[Any], project_id: str, eligible: Any) -> list[Any]:
    scoped = [row for row in rows if row.project_id is None or row.project_id == project_id]
    series_ids = {row.series_id for row in scoped}
    for series_id in series_ids:
        if len({row.project_id for row in rows if row.series_id == series_id}) != 1:
            raise ServiceError(409, f"Series {series_id} has inconsistent scope")
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in scoped:
        if eligible(row):
            grouped[row.series_id].append(row)
    return [
        max(grouped[series_id], key=lambda row: (row.version, row.id))
        for series_id in sorted(grouped)
    ]


class ContextService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def build_context(self, task_id: str, assignment_id: str | None = None) -> TaskContext:
        task = self.session.get(Task, task_id)
        if task is None:
            raise ServiceError(404, "Task not found")
        project = self.session.get(Project, task.project_id)
        if project is None:
            raise ServiceError(404, "Project not found")
        assignment, agent = self._resolve_agent(task, assignment_id)
        references = [
            _reference("project", project.id, _timestamp(project.updated_at), "project_scope"),
            _reference("task", task.id, _timestamp(task.updated_at), "task_definition"),
        ]
        project_context: dict[str, Any] = {
            "project_name": project.name,
            "project_description": project.description,
            "project_status": project.status.value,
            "task_status": task.status.value,
            "routing_mode": task.routing_mode.value,
            "dependency_ids": sorted(task.depends_on),
            "input_context_refs": sorted(task.input_context_refs),
        }
        if assignment:
            project_context["assignment"] = {
                "id": assignment.id,
                "routing_reason": assignment.routing_reason,
                "fallback_used": assignment.fallback_used,
                "created_at": _timestamp(assignment.created_at),
            }
            references.append(
                _reference(
                    "execution_assignment",
                    assignment.id,
                    _timestamp(assignment.created_at),
                    "selected_assignment",
                )
            )
        outputs, facts = self._dependency_content(task, references)
        decisions = self._decisions(project.id, references)
        facts.extend(self._knowledge_facts(project.id, references))
        policies = self._policies(project.id, references)
        agent_profile = self._agent_profile(agent, references)
        references.sort(
            key=lambda item: (
                item["resource_type"],
                item["resource_id"],
                item["inclusion_reason"],
                item["version"],
            )
        )
        payload = {
            "task_id": task.id,
            "project_id": project.id,
            "assigned_agent_id": agent.id if agent else None,
            "objective": project.objective,
            "instructions": task.description,
            "acceptance_criteria": list(task.acceptance_criteria),
            "project_context": _safe(project_context),
            "dependency_outputs": outputs,
            "approved_facts": facts,
            "relevant_decisions": decisions,
            "applicable_policies": policies,
            "agent_profile": agent_profile,
            "source_references": references,
        }
        context_hash = canonical_hash(payload)
        existing = self.session.exec(
            select(TaskContext).where(
                TaskContext.task_id == task.id,
                TaskContext.context_hash == context_hash,
            )
        ).first()
        if existing:
            return existing
        context = TaskContext(**payload, context_hash=context_hash)
        try:
            self.session.add(context)
            self.session.flush()
            append_audit(
                self.session,
                actor="context_service",
                action="context.generated",
                resource_type="task_context",
                resource_id=context.id,
                project_id=project.id,
                task_id=task.id,
                before={},
                after={
                    "task_id": task.id,
                    "assignment_id": assignment.id if assignment else None,
                    "context_id": context.id,
                    "context_hash": context_hash,
                    "source_references": references,
                },
                idempotency_key=f"audit:context:{context.id}",
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(context)
        return context

    def _resolve_agent(
        self, task: Task, assignment_id: str | None
    ) -> tuple[ExecutionAssignment | None, Agent | None]:
        if assignment_id:
            assignment = self.session.get(ExecutionAssignment, assignment_id)
            if assignment is None:
                raise ServiceError(404, "Assignment not found")
            if assignment.task_id != task.id:
                raise ServiceError(409, "Assignment does not belong to task")
            agent = self.session.get(Agent, assignment.selected_agent_id)
            if agent is None:
                raise ServiceError(409, "Assignment selected agent is missing")
            if task.assigned_agent_id and task.assigned_agent_id != agent.id:
                raise ServiceError(409, "Task and assignment agents conflict")
            return assignment, agent
        if task.assigned_agent_id is None:
            return None, None
        agent = self.session.get(Agent, task.assigned_agent_id)
        if agent is None:
            raise ServiceError(409, "Task assigned agent is missing")
        return None, agent

    def _dependency_content(
        self, task: Task, references: list[dict[str, str]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        outputs: list[dict[str, Any]] = []
        facts: list[dict[str, Any]] = []
        for dependency_id in sorted(task.depends_on):
            dependency = self.session.get(Task, dependency_id)
            if dependency is None or dependency.project_id != task.project_id:
                raise ServiceError(409, "Dependency reference is invalid")
            if dependency.status != TaskStatus.DONE:
                continue
            references.append(
                _reference(
                    "task", dependency.id, _timestamp(dependency.updated_at), "completed_dependency"
                )
            )
            artifacts = list(
                self.session.exec(
                    select(Artifact)
                    .where(
                        Artifact.task_id == dependency.id,
                        Artifact.project_id == task.project_id,
                        Artifact.review_status == ArtifactReviewStatus.APPROVED,
                    )
                    .order_by(Artifact.id)
                )
            )
            for artifact in artifacts:
                summary = artifact.metadata_json.get("summary", "")
                outputs.append(
                    {
                        "artifact_id": artifact.id,
                        "task_id": dependency.id,
                        "type": artifact.type.value,
                        "checksum": artifact.checksum,
                        "summary": summary if isinstance(summary, str) else "",
                    }
                )
                references.append(
                    _reference(
                        "artifact", artifact.id, artifact.checksum, "completed_dependency_output"
                    )
                )
                reviewed = list(
                    self.session.exec(
                        select(ReviewedFact)
                        .where(
                            ReviewedFact.artifact_id == artifact.id,
                            ReviewedFact.status == ReviewedFactStatus.APPROVED,
                        )
                        .order_by(ReviewedFact.id)
                    )
                )
                for fact in reviewed:
                    facts.append(
                        {
                            "fact_id": fact.id,
                            "statement": fact.statement,
                            "reviewer": fact.reviewer,
                            "reviewed_at": _timestamp(fact.reviewed_at)
                            if fact.reviewed_at
                            else None,
                            "source_artifact_id": artifact.id,
                        }
                    )
                    references.append(
                        _reference(
                            "reviewed_fact",
                            fact.id,
                            _timestamp(fact.reviewed_at or fact.created_at),
                            "approved_fact",
                        )
                    )
        return outputs, facts

    def _knowledge_facts(
        self,
        project_id: str,
        references: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        rows = list(
            self.session.exec(
                select(KnowledgeFact).where(
                    KnowledgeFact.status == KnowledgeFactStatus.APPROVED,
                    or_(
                        KnowledgeFact.project_id.is_(None),
                        KnowledgeFact.project_id == project_id,
                    ),
                )
            )
        )
        rows.sort(
            key=lambda row: (
                0 if row.project_id is None else 1,
                row.series_id,
                row.version,
                row.id,
            )
        )
        result: list[dict[str, Any]] = []
        for fact in rows:
            candidate = self.session.get(KnowledgeCandidate, fact.source_candidate_id)
            review = self.session.get(KnowledgeReviewDecision, fact.review_decision_id)
            artifact = self.session.get(Artifact, fact.source_artifact_id)
            if candidate is None or review is None or artifact is None:
                raise ServiceError(409, "Knowledge fact provenance is missing")
            scope = "company" if fact.project_id is None else "project"
            result.append(
                {
                    "fact_kind": "knowledge_fact",
                    "fact_id": fact.id,
                    "series_id": fact.series_id,
                    "version": fact.version,
                    "scope": scope,
                    "project_id": fact.project_id,
                    "statement": fact.statement,
                    "source_candidate_id": candidate.id,
                    "source_artifact_id": artifact.id,
                    "review_decision_id": review.id,
                }
            )
            fact_reference: dict[str, Any] = _reference(
                "knowledge_fact",
                fact.id,
                f"{fact.series_id}:{fact.version}",
                "approved_reusable_knowledge",
            )
            fact_reference.update({"scope": scope, "project_id": fact.project_id})
            references.extend(
                [
                    fact_reference,
                    _reference(
                        "knowledge_candidate",
                        candidate.id,
                        _timestamp(candidate.updated_at),
                        "knowledge_fact_provenance",
                    ),
                    _reference(
                        "knowledge_review_decision",
                        review.id,
                        _timestamp(review.reviewed_at),
                        "human_approval_provenance",
                    ),
                    _reference(
                        "artifact",
                        artifact.id,
                        artifact.checksum,
                        "knowledge_source_artifact",
                    ),
                ]
            )
        return result

    def _decisions(self, project_id: str, references: list[dict[str, str]]) -> list[dict[str, Any]]:

        rows = list(self.session.exec(select(Decision)))
        selected = _latest_versions(
            rows, project_id, lambda row: row.status == DecisionStatus.APPROVED
        )
        result = []
        for row in selected:
            result.append(
                {
                    "id": row.id,
                    "series_id": row.series_id,
                    "project_id": row.project_id,
                    "title": row.title,
                    "content": row.content,
                    "version": row.version,
                    "updated_at": _timestamp(row.updated_at),
                }
            )
            references.append(
                _reference(
                    "decision", row.id, f"{row.series_id}:{row.version}", "approved_decision"
                )
            )
        return result

    def _policies(self, project_id: str, references: list[dict[str, str]]) -> list[dict[str, Any]]:
        rows = list(self.session.exec(select(Policy)))
        selected = _latest_versions(rows, project_id, lambda row: row.enabled)
        result = []
        for row in selected:
            result.append(
                {
                    "id": row.id,
                    "series_id": row.series_id,
                    "project_id": row.project_id,
                    "name": row.name,
                    "content": row.content,
                    "version": row.version,
                    "updated_at": _timestamp(row.updated_at),
                }
            )
            references.append(
                _reference("policy", row.id, f"{row.series_id}:{row.version}", "applicable_policy")
            )
        return result

    def _agent_profile(
        self, agent: Agent | None, references: list[dict[str, str]]
    ) -> dict[str, Any]:
        if agent is None:
            return {}
        profiles = list(
            self.session.exec(
                select(AgentCapability)
                .where(
                    AgentCapability.agent_id == agent.id,
                    AgentCapability.enabled.is_(True),
                )
                .order_by(AgentCapability.capability_id)
            )
        )
        capabilities = []
        for profile in profiles:
            capability = self.session.get(Capability, profile.capability_id)
            if capability is None:
                raise ServiceError(409, "Agent capability reference is missing")
            projection = {
                "id": capability.id,
                "name": capability.name,
                "description": capability.description,
                "priority": profile.priority,
            }
            capabilities.append(projection)
            references.append(
                _reference(
                    "agent_capability",
                    f"{agent.id}:{capability.id}",
                    canonical_hash(projection),
                    "selected_agent_capability",
                )
            )
        profile_data = {
            "id": agent.id,
            "name": agent.name,
            "role": agent.role,
            "adapter_type": agent.adapter_type.value,
            "status": agent.status.value,
            "permissions": sorted(agent.permissions),
            "limitations": sorted(agent.limitations),
            "capabilities": capabilities,
        }
        references.append(
            _reference("agent", agent.id, canonical_hash(profile_data), "selected_agent_profile")
        )
        return _safe(profile_data)
