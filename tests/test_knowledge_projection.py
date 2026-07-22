"""Focused Phase A projection coverage (Issue #67): audit, report, REST owners.

Complements test_knowledge_context.py (gate mechanics) and
test_knowledge_service.py (service-level owner-only / identity / classify).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.actor import resolve_owner_actor
from aios.api.app import create_app
from aios.audit import AuditLog
from aios.db import get_database_url, get_engine
from aios.knowledge_service import KnowledgeService
from aios.knowledge_tags import LEGACY_UNCLASSIFIED, report_unclassified_knowledge
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    AgentTrustLevel,
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    Capability,
    KnowledgeCandidate,
    KnowledgeFact,
    KnowledgeReviewDecision,
    KnowledgeReviewDecisionValue,
    Project,
)
from aios.models import (
    Task as TaskModel,
)
from aios.schemas import KnowledgeClassifyRequest


@pytest.fixture
def client(trusted_owner_installer, tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'kp_proj.db'}")
    monkeypatch.setenv("KNOWLEDGE_LEAST_PRIVILEGE_ENABLED", "1")
    monkeypatch.delenv("AIOS_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("AIOS_AGENT_BASE_URL", raising=False)
    app = create_app()
    trusted_owner_installer(app)
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def _session() -> Session:
    return Session(get_engine(get_database_url()))


def _seed_positioning_fact(client) -> tuple[str, str]:
    """Launch a campaign, seed a company fact tagged 'positioning', return (pid, fact_id)."""
    client.post("/owner/launch", data={"name": "c", "objective": "o"})
    with _session() as session:
        project = session.exec(select(Project).order_by(Project.created_at.desc())).first()
        artifact = Artifact(
            project_id=project.id,
            type=ArtifactType.JSON,
            uri="a.json",
            checksum="sha256:a",
            review_status=ArtifactReviewStatus.APPROVED,
        )
        session.add(artifact)
        session.commit()
        cand = KnowledgeService(session).submit_candidate(
            artifact.id,
            "Positioning insight",
            project_id=None,
            tags=["positioning"],
            actor=resolve_owner_actor(),
        )
        result = KnowledgeService(session).review_candidate(
            cand.id,
            KnowledgeReviewDecisionValue.APPROVE,
            "ok",
            actor=resolve_owner_actor(),
            series_id="positioning",
            version=1,
        )
        return project.id, result.fact.id


def _wire(client, project_id: str, task_key_title: str, tag: str = "positioning") -> None:
    with _session() as session:
        cap = session.exec(select(Capability).where(Capability.name == tag)).first()
        agent = Agent(
            id="agt:internal",
            name="Internal",
            role="tester",
            adapter_type=AdapterType.API,
            trust_level=AgentTrustLevel.INTERNAL,
        )
        session.add(agent)
        session.flush()
        session.add(AgentCapability(agent_id=agent.id, capability_id=cap.id, enabled=True))
        task = session.exec(select(TaskModel).where(TaskModel.title == task_key_title)).first()
        task.assigned_agent_id = agent.id
        task.required_capabilities = [cap.id]
        session.add(task)
        session.commit()


# --- R7: structured, redacted, idempotent projection audit -------------------


def test_projection_audit_is_structured_redacted_idempotent(client) -> None:
    project_id, fact_id = _seed_positioning_fact(client)
    # Find a task title to attach the agent to (use any task in the project).
    with _session() as session:
        task = session.exec(select(TaskModel).where(TaskModel.project_id == project_id)).first()
    _wire(client, project_id, task.title)

    from aios.context_service import ContextService

    with _session() as session:
        ctx = ContextService(session).build_context(task.id)
        replay = ContextService(session).build_context(task.id)
        assert ctx.id == replay.id

        audits = list(
            session.exec(
                select(AuditLog).where(AuditLog.action == "knowledge.fact.projected")
            ).all()
        )
        # Emitted exactly once (the first build); replay returns the existing ctx.
        assert len(audits) == 1
        after = audits[0].after_snapshot
        assert after["fact_id"] == fact_id
        assert after["matched_tags"] == ["positioning"]
        assert after["projection_mode"] == "least_privilege"
        # The knowledge statement text is NEVER persisted to the audit log.
        assert "Positioning insight" not in str(after)


# --- report_unclassified_knowledge (R1 helper) -------------------------------


def test_report_unclassified_knowledge_lists_sentinel_facts(client) -> None:
    client.post("/owner/launch", data={"name": "c", "objective": "o"})
    with _session() as session:
        project = session.exec(select(Project).order_by(Project.created_at.desc())).first()
        artifact = Artifact(
            project_id=project.id,
            type=ArtifactType.JSON,
            uri="a.json",
            checksum="sha256:a",
            review_status=ArtifactReviewStatus.APPROVED,
        )
        session.add(artifact)
        session.commit()
        # Seed a legacy sentinel fact directly (APPROVED + sentinel tags).
        cand = KnowledgeCandidate(
            artifact_id=artifact.id,
            project_id=project.id,
            source_project_id=project.id,
            statement="Legacy",
            tags=[LEGACY_UNCLASSIFIED],
            submitted_by_kind="owner",
            submitted_by_owner_id="owner",
            submitted_by="owner",
        )
        session.add(cand)
        session.flush()
        cand.status = "APPROVED"
        session.add(cand)
        session.flush()
        review = KnowledgeReviewDecision(
            candidate_id=cand.id,
            decision=KnowledgeReviewDecisionValue.APPROVE,
            reviewer_kind="owner",
            reviewer_owner_id="owner",
            reviewer="owner",
            rationale="legacy",
        )
        session.add(review)
        session.flush()
        fact = KnowledgeFact(
            series_id="legacy",
            version=1,
            project_id=project.id,
            source_project_id=project.id,
            statement="Legacy",
            tags=[LEGACY_UNCLASSIFIED],
            source_candidate_id=cand.id,
            source_artifact_id=artifact.id,
            review_decision_id=review.id,
            supersedes_fact_id=None,
        )
        session.add(fact)
        session.commit()

        reported = report_unclassified_knowledge(session)
        assert len(reported) == 1
        assert reported[0]["fact_id"] == fact.id

        # The owner-visible REST report returns the same list.
        resp = client.get("/knowledge/unclassified")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["fact_id"] == fact.id


# --- R1/R2: REST classify + deactivate (owner-only entry points) --------------


def test_rest_classify_endpoints_promote_sentinel(client) -> None:
    client.post("/owner/launch", data={"name": "c", "objective": "o"})
    with _session() as session:
        project = session.exec(select(Project).order_by(Project.created_at.desc())).first()
        artifact = Artifact(
            project_id=project.id,
            type=ArtifactType.JSON,
            uri="a.json",
            checksum="sha256:a",
            review_status=ArtifactReviewStatus.APPROVED,
        )
        session.add(artifact)
        session.commit()
        # A legacy sentinel candidate.
        cand = KnowledgeCandidate(
            artifact_id=artifact.id,
            project_id=project.id,
            source_project_id=project.id,
            statement="Legacy candidate",
            tags=[LEGACY_UNCLASSIFIED],
            submitted_by_kind="owner",
            submitted_by_owner_id="owner",
            submitted_by="owner",
        )
        session.add(cand)
        session.commit()
        cand_id = cand.id

    # Classify the candidate via REST.
    resp = client.post(
        f"/knowledge/candidates/{cand_id}/classify",
        json=KnowledgeClassifyRequest(tags=["positioning"]).model_dump(mode="json"),
    )
    assert resp.status_code == 200
    assert resp.json()["tags"] == ["positioning"]

    # A second classify is rejected (one-time transition).
    resp2 = client.post(
        f"/knowledge/candidates/{cand_id}/classify",
        json=KnowledgeClassifyRequest(tags=["user_research"]).model_dump(mode="json"),
    )
    assert resp2.status_code == 409


def test_rest_deactivate_fact_sets_inactive(client) -> None:
    project_id, fact_id = _seed_positioning_fact(client)
    resp = client.post(
        f"/knowledge/facts/{fact_id}/deactivate",
        json={"feedback": "No longer applicable"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "inactive"
