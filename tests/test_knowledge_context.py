"""Capability-aware least-privilege KnowledgeFact projection (Phase A, #67).

These tests exercise ``ContextService.build_context``'s projection gate directly
(the end-to-end scope-reuse / no-leak semantics are covered in
``test_knowledge_preservation.py`` under the flag-on regime).

Locked behaviors:
* Flag OFF (default) -> zero ``KnowledgeFact`` injection, no scope-wide fallback.
* Internal agent + task requiring a capability + fact tagged for it -> least-
  privilege projection with ``matched_tags`` recorded.
* Empty required-capability set -> zero injection (fail-closed).
* External / experimental agent -> no projection.
* Readiness gate refuses activation while a sentinel (unclassified) fact remains.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session

from aios.actor import resolve_owner_actor
from aios.context_service import ContextService
from aios.db import get_engine, run_migrations
from aios.knowledge_service import KnowledgeService
from aios.knowledge_tags import (
    LEGACY_UNCLASSIFIED,
    ensure_knowledge_projection_ready,
    is_projection_enabled,
)
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
    KnowledgeReviewDecisionValue,
    Project,
    Task,
    TaskStatus,
)
from aios.services import ServiceError


def database(tmp_path: Path) -> str:
    url = f"sqlite:///{(tmp_path / 'context.db').as_posix()}"
    run_migrations(url)
    return url


def seed_project(session: Session) -> Project:
    project = Project(name="Current", objective="Use knowledge")
    session.add(project)
    session.commit()
    return project


def seed_capability(session: Session, name: str) -> Capability:
    cap = Capability(name=name, description=f"{name} capability")
    session.add(cap)
    session.commit()
    return cap


def seed_agent(session: Session, *, trust: AgentTrustLevel, cap: Capability) -> Agent:
    agent = Agent(
        id=f"agt:{trust.value}",
        name=f"{trust.value} agent",
        role="tester",
        adapter_type=AdapterType.API,
        trust_level=trust,
    )
    session.add(agent)
    session.flush()
    session.add(AgentCapability(agent_id=agent.id, capability_id=cap.id, enabled=True))
    session.commit()
    return agent


def seed_task(session: Session, project: Project, *, cap: Capability | None) -> Task:
    task = Task(
        project_id=project.id,
        title="Task",
        description="Execute",
        status=TaskStatus.BACKLOG,
        required_capabilities=[cap.id] if cap is not None else [],
    )
    session.add(task)
    session.commit()
    return task


def approved_artifact(session: Session, project: Project, name: str) -> Artifact:
    art = Artifact(
        project_id=project.id,
        type=ArtifactType.JSON,
        uri=f"{name}.json",
        checksum=f"sha256:{name}",
        review_status=ArtifactReviewStatus.APPROVED,
    )
    session.add(art)
    session.commit()
    return art


def make_fact(
    session: Session, project: Project, artifact: Artifact, *, tag: str, series: str
) -> KnowledgeFact:
    cand = KnowledgeService(session).submit_candidate(
        artifact.id,
        f"Fact about {tag}",
        project_id=None,
        tags=[tag],
        actor=resolve_owner_actor(),
    )
    result = KnowledgeService(session).review_candidate(
        cand.id,
        KnowledgeReviewDecisionValue.APPROVE,
        "Verified",
        actor=resolve_owner_actor(),
        series_id=series,
        version=1,
    )
    return result.fact


def project_fact(
    session: Session, project: Project, artifact: Artifact, *, tag: str, series: str
) -> KnowledgeFact:
    cand = KnowledgeService(session).submit_candidate(
        artifact.id,
        f"Project fact about {tag}",
        project_id=project.id,
        tags=[tag],
        actor=resolve_owner_actor(),
    )
    result = KnowledgeService(session).review_candidate(
        cand.id,
        KnowledgeReviewDecisionValue.APPROVE,
        "Verified",
        actor=resolve_owner_actor(),
        series_id=series,
        version=1,
    )
    return result.fact


def test_flag_off_injects_no_knowledge(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_LEAST_PRIVILEGE_ENABLED", "0")
    assert not is_projection_enabled()
    url = database(tmp_path)
    with Session(get_engine(url)) as session:
        project = seed_project(session)
        cap = seed_capability(session, "positioning")
        agent = seed_agent(session, trust=AgentTrustLevel.INTERNAL, cap=cap)
        task = seed_task(session, project, cap=cap)
        task.assigned_agent_id = agent.id
        session.add(task)
        session.commit()
        artifact = approved_artifact(session, project, "src")
        make_fact(session, project, artifact, tag="positioning", series="s1")

        ctx = ContextService(session).build_context(task.id)
        projected = [
            f for f in ctx.approved_facts if f.get("fact_kind") == "knowledge_fact"
        ]
        assert projected == [], "Flag OFF must inject zero KnowledgeFact (fail-closed)"


def test_least_privilege_projection_matches_capability(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_LEAST_PRIVILEGE_ENABLED", "1")
    url = database(tmp_path)
    with Session(get_engine(url)) as session:
        project = seed_project(session)
        cap = seed_capability(session, "positioning")
        agent = seed_agent(session, trust=AgentTrustLevel.INTERNAL, cap=cap)
        task = seed_task(session, project, cap=cap)
        task.assigned_agent_id = agent.id
        session.add(task)
        session.commit()
        artifact = approved_artifact(session, project, "src")
        # Company-scoped fact tagged "positioning" -> matches the task capability.
        fact = make_fact(session, project, artifact, tag="positioning", series="s1")

        ctx = ContextService(session).build_context(task.id)
        projected = [
            f for f in ctx.approved_facts if f.get("fact_kind") == "knowledge_fact"
        ]
        assert len(projected) == 1
        assert projected[0]["fact_id"] == fact.id
        assert projected[0]["matched_tags"] == ["positioning"]
        # The source artifact remains project-scoped (company effective scope).
        assert projected[0]["scope"] == "company"


def test_only_matching_tags_projected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_LEAST_PRIVILEGE_ENABLED", "1")
    url = database(tmp_path)
    with Session(get_engine(url)) as session:
        project = seed_project(session)
        cap = seed_capability(session, "positioning")
        agent = seed_agent(session, trust=AgentTrustLevel.INTERNAL, cap=cap)
        task = seed_task(session, project, cap=cap)
        task.assigned_agent_id = agent.id
        session.add(task)
        session.commit()
        artifact = approved_artifact(session, project, "src")
        # A fact tagged "user_research" must NOT be projected for a positioning task.
        make_fact(session, project, artifact, tag="user_research", series="ur")
        ctx = ContextService(session).build_context(task.id)
        projected = [
            f for f in ctx.approved_facts if f.get("fact_kind") == "knowledge_fact"
        ]
        assert projected == [], "Only facts matching the task capability are projected"


def test_empty_capability_set_zero_injection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_LEAST_PRIVILEGE_ENABLED", "1")
    url = database(tmp_path)
    with Session(get_engine(url)) as session:
        project = seed_project(session)
        cap = seed_capability(session, "positioning")
        agent = seed_agent(session, trust=AgentTrustLevel.INTERNAL, cap=cap)
        # Task requires NO capability -> even a matching fact is not projected.
        task = seed_task(session, project, cap=None)
        task.assigned_agent_id = agent.id
        session.add(task)
        session.commit()
        artifact = approved_artifact(session, project, "src")
        make_fact(session, project, artifact, tag="positioning", series="s1")
        ctx = ContextService(session).build_context(task.id)
        projected = [
            f for f in ctx.approved_facts if f.get("fact_kind") == "knowledge_fact"
        ]
        assert projected == [], "Empty capability set => zero injection (fail-closed)"


def test_external_agent_no_projection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_LEAST_PRIVILEGE_ENABLED", "1")
    url = database(tmp_path)
    with Session(get_engine(url)) as session:
        project = seed_project(session)
        cap = seed_capability(session, "positioning")
        agent = seed_agent(session, trust=AgentTrustLevel.VERIFIED_EXTERNAL, cap=cap)
        task = seed_task(session, project, cap=cap)
        task.assigned_agent_id = agent.id
        session.add(task)
        session.commit()
        artifact = approved_artifact(session, project, "src")
        make_fact(session, project, artifact, tag="positioning", series="s1")
        ctx = ContextService(session).build_context(task.id)
        projected = [
            f for f in ctx.approved_facts if f.get("fact_kind") == "knowledge_fact"
        ]
        assert projected == [], "External agent receives no KnowledgeFact projection"


def test_readiness_gate_blocks_sentinel_fact(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_LEAST_PRIVILEGE_ENABLED", "1")
    url = database(tmp_path)
    with Session(get_engine(url)) as session:
        project = seed_project(session)
        artifact = approved_artifact(session, project, "src")
        # Simulate a legacy backfilled sentinel fact (APPROVED, sentinel tags).
        candidate = KnowledgeCandidate(
            artifact_id=artifact.id,
            project_id=project.id,
            source_project_id=project.id,
            statement="Legacy unclassified",
            tags=[LEGACY_UNCLASSIFIED],
            submitted_by_kind="owner",
            submitted_by_owner_id="owner",
            submitted_by="owner",
        )
        session.add(candidate)
        session.flush()
        candidate.status = "APPROVED"
        session.add(candidate)
        session.flush()
        from aios.models import KnowledgeReviewDecision, KnowledgeReviewDecisionValue

        review = KnowledgeReviewDecision(
            candidate_id=candidate.id,
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
            statement="Legacy unclassified",
            tags=[LEGACY_UNCLASSIFIED],
            source_candidate_id=candidate.id,
            source_artifact_id=artifact.id,
            review_decision_id=review.id,
            supersedes_fact_id=None,
        )
        session.add(fact)
        session.commit()
        # The readiness gate must refuse while the sentinel fact remains.
        with pytest.raises(ServiceError, match="unclassified"):
            ensure_knowledge_projection_ready(session)
