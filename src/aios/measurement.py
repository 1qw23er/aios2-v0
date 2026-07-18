"""Read-only V1-I6 measurement report builder (Issue #40).

Single source of truth: existing ``Artifact`` / ``AuditLog`` / ``Approval`` /
``Event`` / ``Task`` / ``KnowledgeCandidate`` / ``KnowledgeFact`` / ``get_board``
data. The builder performs **only SELECT queries** — it never writes, never adds
a model, never adds a migration. It exists so the owner can open a measurement
report after five real campaigns without touching the database.

Non-goals (per Issue #40 stop condition): no analytics dashboards, no chart
libraries, no new metrics tables. Anything the system cannot capture (inquiries,
AI觅 visits/registrations, the owner's subjective quality rating) is left for the
owner scorecard and clearly marked "not captured by system".
"""
from __future__ import annotations

from datetime import datetime  # noqa: F401  (kept for type hints / model fields)
from typing import Any

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from aios.audit import AuditLog
from aios.campaign import V1_TASKS
from aios.distribution import get_package_artifact
from aios.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactReviewStatus,
    Event,
    KnowledgeCandidate,
    KnowledgeFact,
    KnowledgeFactStatus,
    Project,
    Task,
    now_utc,
)

# Canonical T1..T9 ordering and a title->key map so task statuses read humanly.
_KEY_ORDER: list[str] = [t["key"] for t in V1_TASKS]
_TITLE_TO_KEY: dict[str, str] = {t["title"]: t["key"] for t in V1_TASKS}


def _task_key(task: Task) -> str:
    return _TITLE_TO_KEY.get(task.title, task.title)


class CampaignMeasurement(BaseModel):
    """Per-campaign metrics derived read-only from existing tables."""

    project_id: str
    name: str
    objective: str
    status: str
    created_at: datetime
    completed_at: datetime | None = None

    task_statuses: dict[str, str] = Field(default_factory=dict)  # key -> status (T1..T9)
    successful_executions: int = 0
    execution_failures: int = 0
    retries: int = 0

    owner_approvals: int = 0
    owner_rejections: int = 0
    owner_revisions: int = 0
    manual_interventions: int = 0

    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    publish_ready_package: bool = False

    knowledge_candidates: int = 0
    approved_knowledge_facts: int = 0
    company_scoped_facts: int = 0
    reused_company_knowledge: bool = False

    owner_operating_seconds: float = 0.0
    developer_intervention: bool = False

    # Epic 9 outcome metrics that are derivable per-campaign
    completion_rate: float = 0.0  # fraction of T1..T9 at DONE
    content_production_seconds: float | None = None

    # Owner-rated (from scorecard; not derivable from system data)
    quality_rating: str | None = None

    def ordered_task_statuses(self) -> list[tuple[str, str]]:
        """T1..T9 in canonical order regardless of dict insertion order."""
        return [(k, self.task_statuses.get(k, "?")) for k in _KEY_ORDER]


class MeasurementReport(BaseModel):
    """Aggregated read-only report across all measured campaigns."""

    generated_at: datetime
    campaigns: list[CampaignMeasurement] = Field(default_factory=list)
    total_campaigns: int = 0

    # Aggregated Epic 9 outcome metrics
    campaign_completion_rate: float = 0.0  # fraction of campaigns fully done
    total_human_interventions: int = 0
    avg_content_production_seconds: float | None = None
    total_revisions: int = 0
    publishable_rate: float = 0.0
    knowledge_reuse_campaigns: int = 0
    developer_assisted_failures: int = 0

    notes: list[str] = Field(default_factory=list)


class MeasurementService:
    """Read-only builder. Every method only runs SELECTs."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def build_campaign(self, project_id: str) -> CampaignMeasurement:
        project = self.session.get(Project, project_id)
        if project is None:
            raise ValueError(f"Project {project_id} not found")

        tasks = list(
            self.session.exec(select(Task).where(Task.project_id == project_id))
        )
        task_statuses = {_task_key(t): t.status.value for t in tasks}

        # Executions = artifacts produced by the real execution protocol.
        artifacts = list(
            self.session.exec(select(Artifact).where(Artifact.project_id == project_id))
        )
        exec_artifacts = [
            a for a in artifacts if a.metadata_json.get("adapter") == "execution_protocol"
        ]
        successful_executions = len(exec_artifacts)
        artifact_rows = [
            {
                "task_key": (
                    _task_key(self.session.get(Task, a.task_id))
                    if a.task_id
                    else "?"
                ),
                "type": a.type.value,
                "summary": (a.metadata_json.get("summary") or "")[:120],
                "review_status": a.review_status.value,
            }
            for a in artifacts
            if a.task_id is not None
        ]

        # Failures + retries from Event + Task.
        failures = list(
            self.session.exec(
                select(Event).where(
                    Event.project_id == project_id, Event.type == "task.failed"
                )
            )
        )
        execution_failures = len(failures)
        retries = sum(t.retry_count for t in tasks)

        # Owner approvals / rejections (Approval table).
        approvals = list(
            self.session.exec(
                select(Approval).where(Approval.project_id == project_id)
            )
        )
        owner_approvals = sum(1 for a in approvals if a.status == ApprovalStatus.APPROVED)
        owner_rejections = sum(1 for a in approvals if a.status == ApprovalStatus.REJECTED)

        # Owner revisions (AuditLog action=task.revision, actor=owner).
        owner_revisions = len(
            list(
                self.session.exec(
                    select(AuditLog).where(
                        AuditLog.project_id == project_id,
                        AuditLog.action == "task.revision",
                    )
                )
            )
        )

        # Manual interventions outside the console: only developer-actor writes.
        manual_interventions = len(
            list(
                self.session.exec(
                    select(AuditLog).where(
                        AuditLog.project_id == project_id,
                        AuditLog.actor == "developer",
                    )
                )
            )
        )

        # Publish-ready distribution package (T7/T8 -> APPROVED package artifact).
        package = get_package_artifact(self.session, project_id)
        publish_ready_package = (
            package is not None
            and package.review_status == ArtifactReviewStatus.APPROVED
        )

        # Knowledge preserved by this campaign (provenance = this project).
        candidates = list(
            self.session.exec(
                select(KnowledgeCandidate).where(
                    KnowledgeCandidate.source_project_id == project_id
                )
            )
        )
        facts = list(
            self.session.exec(
                select(KnowledgeFact).where(
                    KnowledgeFact.source_project_id == project_id,
                    KnowledgeFact.status == KnowledgeFactStatus.APPROVED,
                )
            )
        )
        company_facts = [f for f in facts if f.project_id is None]
        # Reused company knowledge: a company-scoped fact exists that was NOT
        # produced by this campaign (so it is eligible to enter this campaign's
        # TaskContext via ContextService._knowledge_facts).
        reused = (
            self.session.exec(
                select(KnowledgeFact).where(
                    KnowledgeFact.project_id.is_(None),
                    KnowledgeFact.source_project_id != project_id,
                )
            ).first()
            is not None
        )

        # Owner operating time: span of owner-actor AuditLog entries.
        owner_audits = list(
            self.session.exec(
                select(AuditLog).where(
                    AuditLog.project_id == project_id, AuditLog.actor == "owner"
                )
            )
        )
        owner_operating_seconds = 0.0
        if len(owner_audits) >= 2:
            times = sorted(a.created_at for a in owner_audits)
            owner_operating_seconds = (times[-1] - times[0]).total_seconds()

        # Content production span: first task.running -> last task.completed.
        running = list(
            self.session.exec(
                select(AuditLog).where(
                    AuditLog.project_id == project_id, AuditLog.action == "task.running"
                )
            )
        )
        completed = list(
            self.session.exec(
                select(AuditLog).where(
                    AuditLog.project_id == project_id, AuditLog.action == "task.completed"
                )
            )
        )
        content_production_seconds = None
        if running and completed:
            start = min(a.created_at for a in running)
            end = max(a.created_at for a in completed)
            content_production_seconds = (end - start).total_seconds()

        done_count = sum(1 for t in tasks if t.status.value == "done")
        completion_rate = done_count / len(_KEY_ORDER) if _KEY_ORDER else 0.0

        completed_at = None
        if completion_rate >= 1.0 and tasks:
            completed_at = max(t.updated_at for t in tasks)

        return CampaignMeasurement(
            project_id=project.id,
            name=project.name,
            objective=project.objective,
            status=project.status.value,
            created_at=project.created_at,
            completed_at=completed_at,
            task_statuses=task_statuses,
            successful_executions=successful_executions,
            execution_failures=execution_failures,
            retries=retries,
            owner_approvals=owner_approvals,
            owner_rejections=owner_rejections,
            owner_revisions=owner_revisions,
            manual_interventions=manual_interventions,
            artifacts=artifact_rows,
            publish_ready_package=publish_ready_package,
            knowledge_candidates=len(candidates),
            approved_knowledge_facts=len(facts),
            company_scoped_facts=len(company_facts),
            reused_company_knowledge=reused,
            owner_operating_seconds=owner_operating_seconds,
            developer_intervention=manual_interventions > 0,
            completion_rate=completion_rate,
            content_production_seconds=content_production_seconds,
        )

    def build_report(
        self,
        scorecard: dict[str, dict[str, Any]] | None = None,
    ) -> MeasurementReport:
        """Build the aggregated report. ``scorecard`` maps project_id -> owner-rated
        fields (e.g. {"quality_rating": "publishable"}) supplied out-of-band; the
        builder itself performs no writes."""
        scorecard = scorecard or {}
        projects = list(self.session.exec(select(Project)))
        campaigns = [self.build_campaign(p.id) for p in projects]
        for c in campaigns:
            sc = scorecard.get(c.project_id)
            if sc and "quality_rating" in sc:
                c.quality_rating = sc["quality_rating"]

        total = len(campaigns)
        fully_done = sum(1 for c in campaigns if c.completion_rate >= 1.0)
        campaign_completion_rate = fully_done / total if total else 0.0

        total_human = sum(
            c.owner_approvals + c.owner_rejections + c.owner_revisions for c in campaigns
        )
        total_revisions = sum(c.owner_revisions for c in campaigns)

        prod_times = [
            c.content_production_seconds
            for c in campaigns
            if c.content_production_seconds is not None
        ]
        avg_prod = sum(prod_times) / len(prod_times) if prod_times else None

        packaged = [c for c in campaigns if c.publish_ready_package]
        publishable = sum(
            1 for c in packaged if c.quality_rating in ("publishable", "needs_minor_edit")
        )
        publishable_rate = publishable / len(packaged) if packaged else 0.0

        knowledge_reuse = sum(1 for c in campaigns if c.reused_company_knowledge)
        dev_failures = sum(1 for c in campaigns if c.developer_intervention)

        notes = [
            "Inquiries / qualified leads: not captured by system; owner input required.",
            "AI觅 visits / registrations / activation: not captured by "
            "system; owner input required.",
            "Owner quality rating: from owner scorecard (out-of-band), not system data.",
        ]
        return MeasurementReport(
            generated_at=now_utc(),
            campaigns=campaigns,
            total_campaigns=total,
            campaign_completion_rate=campaign_completion_rate,
            total_human_interventions=total_human,
            avg_content_production_seconds=avg_prod,
            total_revisions=total_revisions,
            publishable_rate=publishable_rate,
            knowledge_reuse_campaigns=knowledge_reuse,
            developer_assisted_failures=dev_failures,
            notes=notes,
        )
