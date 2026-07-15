import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlmodel import Session, select

from aios.audit import AuditLog
from aios.context_service import ContextService
from aios.db import get_engine, run_migrations
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    Capability,
    Decision,
    DecisionStatus,
    ExecutionAssignment,
    Policy,
    Project,
    ReviewedFact,
    ReviewedFactStatus,
    RoutingMode,
    Task,
    TaskContext,
    TaskStatus,
    now_utc,
)
from aios.services import ServiceError


@dataclass
class Seed:
    project: Project
    task: Task
    dependency: Task
    artifact: Artifact
    agent: Agent
    assignment: ExecutionAssignment


def database(tmp_path: Path, name: str) -> str:
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    run_migrations(url)
    return url


def seed_sources(session: Session) -> Seed:
    project = Project(
        name="Campaign",
        objective="Publish safely",
        description="Current project",
    )
    unrelated = Project(name="Other", objective="Unrelated")
    agent = Agent(
        id="agt_writer",
        name="Writer",
        role="writer",
        adapter_type=AdapterType.EXTERNAL,
        permissions=["read_artifacts"],
        limitations=["no_publish"],
    )
    capability = Capability(id="cap_write", name="writing", description="Writes")
    session.add_all([project, unrelated, agent, capability])
    session.flush()
    session.add(
        AgentCapability(
            agent_id=agent.id,
            capability_id=capability.id,
            priority=85,
        )
    )
    dependency = Task(
        project_id=project.id,
        title="Research",
        description="Research",
        status=TaskStatus.DONE,
    )
    session.add(dependency)
    session.flush()
    task = Task(
        project_id=project.id,
        title="Draft",
        description="Write the article",
        status=TaskStatus.READY,
        assigned_agent_id=agent.id,
        required_capabilities=[capability.id],
        routing_mode=RoutingMode.BEST_AVAILABLE,
        acceptance_criteria=["Cited", "Safe"],
        depends_on=[dependency.id],
    )
    session.add(task)
    session.flush()
    assignment = ExecutionAssignment(
        task_id=task.id,
        selected_agent_id=agent.id,
        routing_reason="best_available_static_priority",
        fallback_used=False,
        idempotency_key="assign-context",
    )
    approved = Artifact(
        project_id=project.id,
        task_id=dependency.id,
        type=ArtifactType.JSON,
        uri="approved.json",
        checksum="checksum-one",
        review_status=ArtifactReviewStatus.APPROVED,
        metadata_json={
            "summary": "Approved evidence",
            "claims": ["must not become fact"],
            "password": "do-not-copy",
        },
    )
    unverified = Artifact(
        project_id=project.id,
        task_id=dependency.id,
        type=ArtifactType.JSON,
        uri="unverified.json",
        checksum="unverified",
    )
    rejected = Artifact(
        project_id=project.id,
        task_id=dependency.id,
        type=ArtifactType.JSON,
        uri="rejected.json",
        checksum="rejected",
        review_status=ArtifactReviewStatus.REJECTED,
    )
    unrelated_artifact = Artifact(
        project_id=unrelated.id,
        type=ArtifactType.JSON,
        uri="other.json",
        checksum="other",
        review_status=ArtifactReviewStatus.APPROVED,
    )
    session.add_all([assignment, approved, unverified, rejected, unrelated_artifact])
    session.flush()
    session.add_all(
        [
            ReviewedFact(
                artifact_id=approved.id,
                statement="Reviewed fact",
                status=ReviewedFactStatus.APPROVED,
                reviewer="human_ceo",
                reviewed_at=now_utc(),
            ),
            ReviewedFact(
                artifact_id=approved.id,
                statement="Pending fact",
                status=ReviewedFactStatus.PENDING,
                reviewer="human_ceo",
            ),
            Decision(
                series_id="publication",
                project_id=project.id,
                title="Publication v1",
                content="Use review",
                status=DecisionStatus.APPROVED,
                version=1,
            ),
            Decision(
                series_id="publication",
                project_id=project.id,
                title="Publication v2",
                content="Draft replacement",
                status=DecisionStatus.DRAFT,
                version=2,
            ),
            Decision(
                series_id="company-tone",
                title="Tone",
                content="Be clear",
                status=DecisionStatus.APPROVED,
                version=1,
            ),
            Decision(
                series_id="other-decision",
                project_id=unrelated.id,
                title="Other",
                content="Do not include",
                status=DecisionStatus.APPROVED,
                version=1,
            ),
            Policy(
                series_id="safety",
                project_id=project.id,
                name="Safety v1",
                content="Fact check",
                enabled=True,
                version=1,
            ),
            Policy(
                series_id="safety",
                project_id=project.id,
                name="Safety v2",
                content="Disabled replacement",
                enabled=False,
                version=2,
            ),
            Policy(
                series_id="company-policy",
                name="Company",
                content="Protect secrets",
                enabled=True,
                version=1,
            ),
            Policy(
                series_id="other-policy",
                project_id=unrelated.id,
                name="Other",
                content="Do not include",
                version=1,
            ),
        ]
    )
    session.commit()
    return Seed(project, task, dependency, approved, agent, assignment)


def test_context_is_deterministic_scoped_reviewed_and_audited(tmp_path: Path) -> None:
    url = database(tmp_path, "context.db")
    with Session(get_engine(url)) as session:
        seed = seed_sources(session)

        first = ContextService(session).build_context(seed.task.id, seed.assignment.id)
        second = ContextService(session).build_context(seed.task.id, seed.assignment.id)

        assert first.id == second.id
        assert first.context_hash == second.context_hash
        assert first.assigned_agent_id == seed.agent.id
        assert [item["summary"] for item in first.dependency_outputs] == ["Approved evidence"]
        assert [item["statement"] for item in first.approved_facts] == ["Reviewed fact"]
        assert all("claims" not in item for item in first.dependency_outputs)
        assert all("password" not in item for item in first.dependency_outputs)
        assert {item["series_id"] for item in first.relevant_decisions} == {
            "publication",
            "company-tone",
        }
        assert (
            next(item for item in first.relevant_decisions if item["series_id"] == "publication")[
                "version"
            ]
            == 1
        )
        assert {item["series_id"] for item in first.applicable_policies} == {
            "safety",
            "company-policy",
        }
        assert first.agent_profile["limitations"] == ["no_publish"]
        assert first.agent_profile["capabilities"][0]["priority"] == 85
        assert len(list(session.exec(select(TaskContext)))) == 1
        audits = list(session.exec(select(AuditLog).where(AuditLog.action == "context.generated")))
        assert len(audits) == 1
        assert audits[0].after_snapshot["context_hash"] == first.context_hash
        assert "instructions" not in audits[0].after_snapshot

    with Session(get_engine(url)) as session:
        reopened = session.exec(select(TaskContext)).one()
        assert reopened.context_hash == first.context_hash
        assert reopened.approved_facts[0]["statement"] == "Reviewed fact"


def test_included_artifact_change_changes_hash(tmp_path: Path) -> None:
    url = database(tmp_path, "changed.db")
    with Session(get_engine(url)) as session:
        seed = seed_sources(session)
        first = ContextService(session).build_context(seed.task.id, seed.assignment.id)
        artifact = session.get(Artifact, seed.artifact.id)
        artifact.checksum = "checksum-two"
        session.add(artifact)
        session.commit()

        changed = ContextService(session).build_context(seed.task.id, seed.assignment.id)

        assert changed.id != first.id
        assert changed.context_hash != first.context_hash


def test_assignment_mismatch_and_agent_conflict_are_rejected(tmp_path: Path) -> None:
    url = database(tmp_path, "assignment.db")
    with Session(get_engine(url)) as session:
        seed = seed_sources(session)
        other_task = Task(
            project_id=seed.project.id,
            title="Other",
            description="Other",
            status=TaskStatus.READY,
        )
        other_agent = Agent(
            id="agt_other",
            name="Other",
            role="worker",
            adapter_type=AdapterType.API,
        )
        session.add_all([other_task, other_agent])
        session.commit()

        with pytest.raises(ServiceError, match="does not belong"):
            ContextService(session).build_context(other_task.id, seed.assignment.id)

        seed.task.assigned_agent_id = other_agent.id
        session.add(seed.task)
        session.commit()
        with pytest.raises(ServiceError, match="agents conflict"):
            ContextService(session).build_context(seed.task.id, seed.assignment.id)


def test_inconsistent_lineage_scope_is_rejected(tmp_path: Path) -> None:
    url = database(tmp_path, "lineage.db")
    with Session(get_engine(url)) as session:
        seed = seed_sources(session)
        session.add(
            Decision(
                series_id="publication",
                title="Wrong scope",
                content="Wrong scope",
                status=DecisionStatus.APPROVED,
                version=3,
            )
        )
        session.commit()

        with pytest.raises(ServiceError, match="inconsistent scope"):
            ContextService(session).build_context(seed.task.id, seed.assignment.id)


def test_audit_failure_rolls_back_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = database(tmp_path, "rollback.db")
    with Session(get_engine(url)) as session:
        seed = seed_sources(session)

        def fail_audit(*args: object, **kwargs: object) -> None:
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr("aios.context_service.append_audit", fail_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            ContextService(session).build_context(seed.task.id, seed.assignment.id)
        assert list(session.exec(select(TaskContext))) == []


def test_supplied_assignment_is_exclusive_agent_source(tmp_path: Path) -> None:
    url = database(tmp_path, "exclusive-agent.db")
    with Session(get_engine(url)) as session:
        seed = seed_sources(session)
        seed.task.assigned_agent_id = None
        session.add(seed.task)
        session.commit()

        context = ContextService(session).build_context(seed.task.id, seed.assignment.id)

        assert context.assigned_agent_id == seed.assignment.selected_agent_id
        assert context.agent_profile["id"] == seed.assignment.selected_agent_id


def test_supplied_assignment_with_missing_agent_is_rejected(tmp_path: Path) -> None:
    database_path = tmp_path / "missing-agent.db"
    url = database(tmp_path, database_path.name)
    with Session(get_engine(url)) as session:
        seed = seed_sources(session)
        task_id = seed.task.id
        assignment_id = seed.assignment.id
        agent_id = seed.agent.id

    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM agent WHERE id = ?", (agent_id,))
        connection.commit()
    finally:
        connection.close()

    with (
        Session(get_engine(url)) as session,
        pytest.raises(ServiceError, match="selected agent is missing"),
    ):
        ContextService(session).build_context(task_id, assignment_id)
