from pathlib import Path

from sqlmodel import Session

from aios.context_service import ContextService
from aios.db import get_engine, run_migrations
from aios.knowledge_service import KnowledgeService
from aios.models import (
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    KnowledgeReviewDecisionValue,
    Project,
    Task,
)


def database(tmp_path: Path) -> str:
    url = f"sqlite:///{(tmp_path / 'context.db').as_posix()}"
    run_migrations(url)
    return url


def artifact(session: Session, project_id: str | None, name: str) -> Artifact:
    row = Artifact(
        project_id=project_id,
        type=ArtifactType.JSON,
        uri=f"{name}.json",
        checksum=f"sha256:{name}",
        review_status=ArtifactReviewStatus.APPROVED,
    )
    session.add(row)
    session.commit()
    return row


def approve(
    session: Session,
    source: Artifact,
    statement: str,
    series_id: str,
):
    service = KnowledgeService(session)
    candidate = service.submit_candidate(source.id, statement, source.project_id, "submitter")
    return service.review_candidate(
        candidate.id,
        KnowledgeReviewDecisionValue.APPROVE,
        "reviewer",
        "Verified evidence",
        series_id=series_id,
        version=1,
    ).fact


def test_context_includes_only_company_and_same_project_active_knowledge(
    tmp_path: Path,
) -> None:
    url = database(tmp_path)
    with Session(get_engine(url)) as session:
        project = Project(name="Current", objective="Use knowledge")
        other = Project(name="Other", objective="Do not leak")
        session.add_all([project, other])
        session.flush()
        task = Task(project_id=project.id, title="Task", description="Execute")
        session.add(task)
        session.commit()

        company_fact = approve(
            session, artifact(session, None, "company"), "Company fact", "company-series"
        )
        project_fact = approve(
            session,
            artifact(session, project.id, "project"),
            "Project fact",
            "project-series",
        )
        approve(
            session,
            artifact(session, other.id, "other"),
            "Other fact",
            "other-series",
        )
        inactive = approve(
            session,
            artifact(session, project.id, "inactive"),
            "Inactive fact",
            "inactive-series",
        )
        KnowledgeService(session).deactivate_fact(inactive.id, "admin", "No longer applicable")

        first = ContextService(session).build_context(task.id)
        replay = ContextService(session).build_context(task.id)

        knowledge = [
            fact for fact in first.approved_facts if fact.get("fact_kind") == "knowledge_fact"
        ]
        assert [fact["statement"] for fact in knowledge] == ["Company fact", "Project fact"]
        assert [fact["scope"] for fact in knowledge] == ["company", "project"]
        assert first.id == replay.id
        assert first.context_hash == replay.context_hash
        resource_types = {reference["resource_type"] for reference in first.source_references}
        assert {
            "knowledge_fact",
            "knowledge_candidate",
            "knowledge_review_decision",
            "artifact",
        }.issubset(resource_types)

        KnowledgeService(session).deactivate_fact(
            project_fact.id, "admin", "Project fact withdrawn"
        )
        changed = ContextService(session).build_context(task.id)
        assert changed.context_hash != first.context_hash
        changed_ids = {
            fact["fact_id"]
            for fact in changed.approved_facts
            if fact.get("fact_kind") == "knowledge_fact"
        }
        assert changed_ids == {company_fact.id}
