"""Work-log & knowledge-capture system tests (#88).

Covers plan §11 (docs/issue-88-implementation-plan.md): model/enum additions,
the 20260728_0009 migration (empty-data round trip, fail-closed populated
downgrade, artifact-trigger survival), and -- as the slice progresses -- the
WorkLogService submission/attestation/idempotency/provenance contracts.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from pathlib import Path
from threading import Barrier

import pytest
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from aios.actor import ActorContext, resolve_owner_actor
from aios.audit import AuditLog
from aios.db import get_engine, run_migrations
from aios.knowledge_service import KnowledgeService
from aios.knowledge_tags import CANONICAL_KNOWLEDGE_TAGS, normalize_tags
from aios.models import (
    Agent,
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    ExecutionAssignment,
    KnowledgeCandidate,
    KnowledgeCandidateStatus,
    KnowledgeFact,
    KnowledgeFactStatus,
    KnowledgeReviewDecisionValue,
    Project,
    RiskLevel,
    Task,
)
from aios.review import owner_approve_review
from aios.services import ServiceError
from aios.work_log import (
    ContentFeed,
    ContentValueJudge,
    KnowledgeHarvester,
    WorkLogService,
    map_work_log_tags,
    now_utc,
    storage_idempotency_key,
)
from alembic import command

HEAD = "20260729_0001"
PREV = "20260727_0008"


# ---------------------------------------------------------------------------
# Models / enums (plan §11 "模型/枚举")
# ---------------------------------------------------------------------------


def test_artifact_type_has_work_log() -> None:
    assert ArtifactType.WORK_LOG == "work_log"
    assert ArtifactType("work_log") is ArtifactType.WORK_LOG


def test_agent_has_platform_external_ref() -> None:
    agent = Agent(name="hermes-writer", role="writer", adapter_type="external")
    # Both new fields default to None (pre-existing agents are unaffected).
    assert agent.platform is None
    assert agent.external_ref is None
    tagged = Agent(
        name="coze-bot",
        role="ops",
        adapter_type="external",
        platform="coze",
        external_ref="bot-7412",
    )
    assert tagged.platform == "coze"
    assert tagged.external_ref == "bot-7412"


# ---------------------------------------------------------------------------
# Migration 20260728_0009 (plan §11 "迁移", review point 4: fail-closed)
# ---------------------------------------------------------------------------


def _config(url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _revision(url: str) -> str:
    with Session(get_engine(url)) as session:
        return session.connection().exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()


def _columns(url: str, table: str) -> set[str]:
    with Session(get_engine(url)) as session:
        return {
            row[1]
            for row in session.connection().exec_driver_sql(
                f"PRAGMA table_info({table})"
            )
        }


def _index_names(url: str) -> set[str]:
    with Session(get_engine(url)) as session:
        return {
            row[0]
            for row in session.connection().exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }


def _trigger_names(url: str) -> set[str]:
    with Session(get_engine(url)) as session:
        return {
            row[0]
            for row in session.connection().exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }


def _assert_0009_schema_present(url: str) -> None:
    assert {"platform", "external_ref"}.issubset(_columns(url, "agent"))
    assert "idempotency_key" in _columns(url, "artifact")
    assert "uq_artifact_idempotency" in _index_names(url)
    assert "ix_agent_platform" in _index_names(url)


def _assert_0009_schema_absent(url: str) -> None:
    assert not {"platform", "external_ref"} & _columns(url, "agent")
    assert "idempotency_key" not in _columns(url, "artifact")
    assert "uq_artifact_idempotency" not in _index_names(url)
    assert "ix_agent_platform" not in _index_names(url)


def _assert_0029_schema_present(url: str) -> None:
    assert "bootstrap_token_ref" in _columns(url, "agent")
    assert "uq_agent_platform_external_ref" in _index_names(url)


def _assert_0029_schema_absent(url: str) -> None:
    assert "bootstrap_token_ref" not in _columns(url, "agent")
    assert "uq_agent_platform_external_ref" not in _index_names(url)


def test_work_log_migration_round_trip(tmp_path: Path) -> None:
    """Empty 0009 data: 0008 -> 0009 -> 0008 -> 0009 is a lossless round trip;
    the columns and the partial unique index appear and disappear; the final
    ``head`` upgrade lands on 20260728_0009 (head bump assertion)."""
    url = f"sqlite:///{(tmp_path / 'work_log_rt.db').as_posix()}"
    config = _config(url)

    command.upgrade(config, PREV)
    assert _revision(url) == PREV
    _assert_0009_schema_absent(url)

    command.upgrade(config, HEAD)
    assert _revision(url) == HEAD
    _assert_0009_schema_present(url)

    # No 0009 data was written -> downgrade must be lossless and clean.
    command.downgrade(config, PREV)
    assert _revision(url) == PREV
    _assert_0009_schema_absent(url)

    # And back up to head again.
    command.upgrade(config, "head")
    assert _revision(url) == HEAD
    _assert_0009_schema_present(url)

    # The partial unique index enforces per-key uniqueness while permitting
    # unlimited NULLs (all legacy artifacts keep idempotency_key IS NULL).
    with Session(get_engine(url)) as session:
        project = Project(name="P", objective="O")
        session.add(project)
        session.flush()
        session.add_all(
            [
                Artifact(project_id=project.id, type=ArtifactType.JSON, uri="a1", checksum="c1"),
                Artifact(project_id=project.id, type=ArtifactType.JSON, uri="a2", checksum="c2"),
            ]
        )
        session.add(
            Artifact(
                project_id=project.id,
                type=ArtifactType.WORK_LOG,
                uri="w1",
                checksum="c3",
                idempotency_key="work_log:p:deadbeef",
            )
        )
        session.commit()
    with Session(get_engine(url)) as session:
        project_id = session.connection().exec_driver_sql(
            "SELECT id FROM project"
        ).scalar_one()
        session.add(
            Artifact(
                project_id=project_id,
                type=ArtifactType.WORK_LOG,
                uri="w2",
                checksum="c4",
                idempotency_key="work_log:p:deadbeef",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# Service layer fixtures / helpers (plan §5-§8)
# ---------------------------------------------------------------------------

OWNER = resolve_owner_actor()
AGENT_ACTOR = ActorContext(kind="agent", agent_id="agt_x")

_LOG_FIELDS = {
    "what_done": "完成了公众号文章初稿",
    "why": "本周内容排期",
    "problem": "标题不勾人",
    "solution": "换成数字型标题",
    "new_knowledge": "实验结论：数字型标题的打开率对比提升明显，值得沉淀为固定套路。",
}


@pytest.fixture()
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{(tmp_path / 'work_log_service.db').as_posix()}"
    run_migrations(url)
    return url


@pytest.fixture()
def session(db_url: str):
    with Session(get_engine(db_url)) as s:
        yield s


def _seed_project(session: Session, name: str = "P") -> Project:
    project = Project(name=name, objective="O")
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def _submit(session: Session, project: Project, *, key: str = "k-1", **overrides):
    kwargs = {
        "project_id": project.id,
        "report_type": "daily",
        **_LOG_FIELDS,
        "idempotency_key": key,
        "actor": OWNER,
    }
    kwargs.update(overrides)
    artifact, _created = WorkLogService(session).submit_work_log(**kwargs)
    return artifact


def _attestation_evidence(session: Session, artifact_id: str):
    approvals = list(
        session.exec(
            select(Approval).where(
                Approval.target_artifact_id == artifact_id,
                Approval.action_type == "work_log_attestation",
            )
        )
    )
    audits = list(
        session.exec(
            select(AuditLog).where(
                AuditLog.idempotency_key == f"audit:work_log:attest:{artifact_id}"
            )
        )
    )
    return approvals, audits


def _make_fact(
    session: Session,
    artifact: Artifact,
    *,
    statement: str,
    series_id: str,
    version: int = 1,
    project_id: str | None = None,
    supersedes: str | None = None,
) -> KnowledgeFact:
    service = KnowledgeService(session)
    candidate = service.submit_candidate(
        artifact.id,
        statement,
        project_id=project_id,
        tags=["knowledge_capture"],
        actor=OWNER,
    )
    result = service.review_candidate(
        candidate.id,
        KnowledgeReviewDecisionValue.APPROVE,
        "rationale",
        actor=OWNER,
        series_id=series_id,
        version=version,
        supersedes_fact_id=supersedes,
    )
    assert result.fact is not None
    return result.fact


# ---------------------------------------------------------------------------
# Submission / validation (plan §11 "提交入口 / 校验")
# ---------------------------------------------------------------------------


def test_submit_work_log_creates_unverified_artifact(session: Session) -> None:
    project = _seed_project(session)
    artifact = _submit(session, project)
    assert artifact.type == ArtifactType.WORK_LOG
    assert artifact.review_status == ArtifactReviewStatus.UNVERIFIED
    assert artifact.uri == f"work_log:{artifact.id}"
    assert artifact.idempotency_key == storage_idempotency_key(project.id, "k-1")
    assert artifact.provenance_json["submitted_by"] == "owner:owner"
    assert artifact.provenance_json["legacy_assigned_agent"] is False
    assert artifact.metadata_json["report_type"] == "daily"
    assert "_request_fingerprint" in artifact.metadata_json
    assert "tags" not in artifact.metadata_json


def test_submit_requires_owner_actor(session: Session) -> None:
    project = _seed_project(session)
    with pytest.raises(ServiceError) as excinfo:
        _submit(session, project, actor=AGENT_ACTOR)
    assert excinfo.value.status_code == 403


def test_submit_rejects_unknown_project(session: Session) -> None:
    with pytest.raises(ServiceError) as excinfo:
        _submit(session, Project(id="prj_missing", name="X", objective="O"))
    assert excinfo.value.status_code == 422


def test_submit_rejects_task_project_mismatch(session: Session) -> None:
    project = _seed_project(session)
    other = _seed_project(session, name="Q")
    task = Task(project_id=other.id, title="t", description="d")
    session.add(task)
    session.commit()
    with pytest.raises(ServiceError) as excinfo:
        _submit(session, project, task_ref=task.id)
    assert excinfo.value.status_code == 422


def test_agent_provenance_requires_task_ref(session: Session) -> None:
    project = _seed_project(session)
    agent = Agent(name="a", role="r", adapter_type="external")
    session.add(agent)
    session.commit()
    with pytest.raises(ServiceError) as excinfo:
        _submit(session, project, produced_by_agent_id=agent.id)
    assert excinfo.value.status_code == 422


def _seed_routed_task(session: Session, project: Project):
    agent = Agent(name="worker", role="writer", adapter_type="external", platform="hermes")
    other_agent = Agent(name="fallback", role="writer", adapter_type="external")
    session.add_all([agent, other_agent])
    session.flush()
    task = Task(project_id=project.id, title="t", description="d")
    session.add(task)
    session.flush()
    original = ExecutionAssignment(
        task_id=task.id,
        selected_agent_id=other_agent.id,
        routing_reason="preferred",
        idempotency_key=f"asn:{task.id}:1",
    )
    fallback = ExecutionAssignment(
        task_id=task.id,
        selected_agent_id=agent.id,
        routing_reason="fallback",
        fallback_used=True,
        idempotency_key=f"asn:{task.id}:2",
    )
    session.add_all([original, fallback])
    session.commit()
    for obj in (agent, other_agent, task, original, fallback):
        session.refresh(obj)
    return agent, other_agent, task, original, fallback


def test_agent_provenance_requires_assignment_id_when_routed(session: Session) -> None:
    project = _seed_project(session)
    agent, _, task, _, _ = _seed_routed_task(session, project)
    with pytest.raises(ServiceError) as excinfo:
        _submit(session, project, task_ref=task.id, produced_by_agent_id=agent.id)
    assert excinfo.value.status_code == 422
    assert "assignment id required" in excinfo.value.detail


def test_agent_provenance_accepts_exact_assignment(session: Session) -> None:
    project = _seed_project(session)
    agent, _, task, _, fallback = _seed_routed_task(session, project)
    artifact = _submit(
        session,
        project,
        task_ref=task.id,
        produced_by_agent_id=agent.id,
        execution_assignment_id=fallback.id,
    )
    assert artifact.provenance_json["execution_assignment_id"] == fallback.id
    assert artifact.provenance_json["produced_by_agent_id"] == agent.id
    assert artifact.provenance_json["produced_by_platform"] == "hermes"
    assert artifact.provenance_json["legacy_assigned_agent"] is False


def test_agent_provenance_multiple_assignments_fallback(session: Session) -> None:
    project = _seed_project(session)
    agent, _, task, original, _ = _seed_routed_task(session, project)
    # The original assignment points at the other agent -> wrong-agent claim.
    with pytest.raises(ServiceError) as excinfo:
        _submit(
            session,
            project,
            task_ref=task.id,
            produced_by_agent_id=agent.id,
            execution_assignment_id=original.id,
        )
    assert excinfo.value.status_code == 422
    assert "selected_agent_id mismatch" in excinfo.value.detail


def test_agent_provenance_rejects_cross_task_assignment(session: Session) -> None:
    project = _seed_project(session)
    agent, _, task, _, _ = _seed_routed_task(session, project)
    other_task = Task(project_id=project.id, title="t2", description="d2")
    session.add(other_task)
    session.flush()
    foreign = ExecutionAssignment(
        task_id=other_task.id,
        selected_agent_id=agent.id,
        routing_reason="direct",
        idempotency_key=f"asn:{other_task.id}:1",
    )
    session.add(foreign)
    session.commit()
    session.refresh(foreign)
    with pytest.raises(ServiceError) as excinfo:
        _submit(
            session,
            project,
            task_ref=task.id,
            produced_by_agent_id=agent.id,
            execution_assignment_id=foreign.id,
        )
    assert excinfo.value.status_code == 422
    assert "different task" in excinfo.value.detail


def test_agent_provenance_rejects_preferred_not_selected(session: Session) -> None:
    project = _seed_project(session)
    agent = Agent(name="preferred-only", role="r", adapter_type="external")
    executor = Agent(name="executor", role="r", adapter_type="external")
    session.add_all([agent, executor])
    session.flush()
    task = Task(
        project_id=project.id,
        title="t",
        description="d",
        preferred_agent_id=agent.id,
    )
    session.add(task)
    session.flush()
    assignment = ExecutionAssignment(
        task_id=task.id,
        selected_agent_id=executor.id,
        routing_reason="best_available",
        idempotency_key=f"asn:{task.id}:1",
    )
    session.add(assignment)
    session.commit()
    session.refresh(assignment)
    with pytest.raises(ServiceError) as excinfo:
        _submit(
            session,
            project,
            task_ref=task.id,
            produced_by_agent_id=agent.id,
            execution_assignment_id=assignment.id,
        )
    assert excinfo.value.status_code == 422


def test_agent_provenance_legacy_assigned_agent(session: Session) -> None:
    project = _seed_project(session)
    agent = Agent(name="fixed", role="r", adapter_type="external")
    session.add(agent)
    session.flush()
    task = Task(
        project_id=project.id,
        title="t",
        description="d",
        assigned_agent_id=agent.id,
    )
    session.add(task)
    session.commit()
    session.refresh(agent)
    session.refresh(task)
    artifact = _submit(
        session, project, task_ref=task.id, produced_by_agent_id=agent.id
    )
    assert artifact.provenance_json["legacy_assigned_agent"] is True
    assert artifact.provenance_json["execution_assignment_id"] is None
    # Same scenario WITH an assignment id supplied -> 422.
    with pytest.raises(ServiceError) as excinfo:
        _submit(
            session,
            project,
            key="k-2",
            task_ref=task.id,
            produced_by_agent_id=agent.id,
            execution_assignment_id="asn_bogus",
        )
    assert excinfo.value.status_code == 422


def test_provenance_agent_never_becomes_actor(session: Session) -> None:
    project = _seed_project(session)
    agent, _, task, _, fallback = _seed_routed_task(session, project)
    artifact = _submit(
        session,
        project,
        task_ref=task.id,
        produced_by_agent_id=agent.id,
        execution_assignment_id=fallback.id,
    )
    assert artifact.provenance_json["submitted_by"] == "owner:owner"
    audit = session.exec(
        select(AuditLog).where(
            AuditLog.idempotency_key == f"audit:work_log:submit:{artifact.id}"
        )
    ).one()
    assert audit.actor == "owner:owner"


# ---------------------------------------------------------------------------
# Attestation (plan §7.2)
# ---------------------------------------------------------------------------


def test_attest_work_log_writes_evidence(session: Session) -> None:
    project = _seed_project(session)
    artifact = _submit(session, project)
    attested = WorkLogService(session).attest_work_log(
        artifact_id=artifact.id, actor=OWNER
    )
    assert attested.review_status == ArtifactReviewStatus.APPROVED
    approvals, audits = _attestation_evidence(session, artifact.id)
    assert len(approvals) == 1
    assert approvals[0].status == ApprovalStatus.APPROVED
    assert approvals[0].risk_level == RiskLevel.L1
    assert len(audits) == 1
    assert audits[0].action == "work_log.owner_attested"


def test_attest_work_log_idempotent(session: Session) -> None:
    project = _seed_project(session)
    artifact = _submit(session, project)
    service = WorkLogService(session)
    service.attest_work_log(artifact_id=artifact.id, actor=OWNER)
    again = service.attest_work_log(artifact_id=artifact.id, actor=OWNER)
    assert again.review_status == ArtifactReviewStatus.APPROVED
    approvals, audits = _attestation_evidence(session, artifact.id)
    assert len(approvals) == 1
    assert len(audits) == 1


def test_attest_requires_owner_actor(session: Session) -> None:
    project = _seed_project(session)
    artifact = _submit(session, project)
    with pytest.raises(ServiceError) as excinfo:
        WorkLogService(session).attest_work_log(
            artifact_id=artifact.id, actor=AGENT_ACTOR
        )
    assert excinfo.value.status_code == 403


def test_attest_work_log_concurrent_two_sessions(db_url: str) -> None:
    """Two independent sessions attest the same UNVERIFIED log concurrently.

    BEGIN IMMEDIATE serializes the writers: exactly 1 Approval, 1 AuditLog and
    1 status flip; BOTH sessions return successfully (winner=updated,
    loser=idempotent no-op)."""
    with Session(get_engine(db_url)) as setup:
        project = _seed_project(setup)
        artifact = _submit(setup, project)
        artifact_id = artifact.id

    barrier = Barrier(2)

    def attempt(_: int) -> str:
        with Session(get_engine(db_url)) as s:
            barrier.wait(timeout=30)
            result = WorkLogService(s).attest_work_log(
                artifact_id=artifact_id, actor=OWNER
            )
            return result.review_status


    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, range(2)))
    assert outcomes == [
        ArtifactReviewStatus.APPROVED,
        ArtifactReviewStatus.APPROVED,
    ]
    with Session(get_engine(db_url)) as check:
        stored = check.get(Artifact, artifact_id)
        assert stored is not None
        assert stored.review_status == ArtifactReviewStatus.APPROVED
        approvals, audits = _attestation_evidence(check, artifact_id)
        assert len(approvals) == 1
        assert len(audits) == 1


@pytest.mark.parametrize("missing", ["approval", "audit", "both"])
def test_attest_fail_closed_missing_evidence(session: Session, missing: str) -> None:
    """An APPROVED work log whose attestation evidence is broken must 409 --
    never silently no-op and never backfill the evidence."""
    project = _seed_project(session)
    artifact = _submit(session, project)
    service = WorkLogService(session)
    service.attest_work_log(artifact_id=artifact.id, actor=OWNER)

    if missing in ("approval", "both"):
        session.connection().exec_driver_sql(
            "DELETE FROM approval WHERE target_artifact_id = ? "
            "AND action_type = 'work_log_attestation'",
            (artifact.id,),
        )
    if missing in ("audit", "both"):
        session.connection().exec_driver_sql(
            "DELETE FROM audit_log WHERE idempotency_key = ?",
            (f"audit:work_log:attest:{artifact.id}",),
        )
    session.commit()

    with pytest.raises(ServiceError) as excinfo:
        service.attest_work_log(artifact_id=artifact.id, actor=OWNER)
    assert excinfo.value.status_code == 409
    # Fail-closed also means no evidence backfill happened.
    approvals, audits = _attestation_evidence(session, artifact.id)
    if missing in ("approval", "both"):
        assert approvals == []
    if missing in ("audit", "both"):
        assert audits == []


def test_attest_fail_closed_duplicate_approved_evidence(session: Session) -> None:
    """A single attestation must produce EXACTLY one approved Approval (plan
    §7.2). If a second approved row exists -- corrupted or manually duplicated
    -- re-attesting must 409 fail-closed, not silently accept it."""
    project = _seed_project(session)
    artifact = _submit(session, project)
    service = WorkLogService(session)
    service.attest_work_log(artifact_id=artifact.id, actor=OWNER)

    # Inject a second approved Approval row for the same artifact.
    duplicate = Approval(
        project_id=artifact.project_id,
        task_id=artifact.task_id,
        target_artifact_id=artifact.id,
        action_type="work_log_attestation",
        risk_level=RiskLevel.L1,
        status=ApprovalStatus.APPROVED,
        decided_at=now_utc(),
        rationale="duplicate injected for fail-closed test",
    )
    session.add(duplicate)
    session.commit()

    with pytest.raises(ServiceError) as excinfo:
        service.attest_work_log(artifact_id=artifact.id, actor=OWNER)
    assert excinfo.value.status_code == 409
    # No backfill: still exactly the original one + the injected duplicate.
    approvals, _ = _attestation_evidence(session, artifact.id)
    assert len([a for a in approvals if a.status == ApprovalStatus.APPROVED]) == 2


def test_attest_rejects_non_work_log(session: Session) -> None:
    project = _seed_project(session)
    other = Artifact(
        project_id=project.id, type=ArtifactType.JSON, uri="a", checksum="c"
    )
    session.add(other)
    session.commit()
    session.refresh(other)
    with pytest.raises(ServiceError) as excinfo:
        WorkLogService(session).attest_work_log(artifact_id=other.id, actor=OWNER)
    assert excinfo.value.status_code == 409


def test_owner_approve_review_rejects_work_log(session: Session) -> None:
    """The generic review workflow must never flip a WORK_LOG to APPROVED --
    that is attest_work_log's sole prerogative (plan §7.2 trust boundary)."""
    project = _seed_project(session)
    log = _submit(session, project)
    with pytest.raises(ServiceError) as excinfo:
        owner_approve_review(session, artifact_id=log.id, actor=OWNER)
    assert excinfo.value.status_code == 409


def test_harvest_skips_evidence_less_approved_log(session: Session) -> None:
    """A WORK_LOG flipped to APPROVED without attestation evidence (e.g. via
    the wrong path) must NOT be harvested -- only evidence-backed logs are
    consumable (plan §7.2)."""
    project = _seed_project(session)
    log = Artifact(
        project_id=project.id,
        type=ArtifactType.WORK_LOG,
        review_status=ArtifactReviewStatus.APPROVED,
        uri="work_log:evil",
        checksum="c",
        metadata_json={
            "content_value": "high",
            "should_enter_kb": True,
            "new_knowledge": "沉淀结论",
        },
    )
    session.add(log)
    session.commit()
    session.refresh(log)
    created = KnowledgeHarvester(session).harvest_candidates(actor=OWNER)
    assert created == []
    count = session.connection().exec_driver_sql(
        "SELECT COUNT(*) FROM knowledge_candidate"
    ).scalar_one()
    assert count == 0


# ---------------------------------------------------------------------------
# Idempotency (plan §5)
# ---------------------------------------------------------------------------


def test_submit_replay_same_key_same_payload(session: Session) -> None:
    project = _seed_project(session)
    first = _submit(session, project)
    second = _submit(session, project)
    assert second.id == first.id
    count = session.connection().exec_driver_sql(
        "SELECT COUNT(*) FROM artifact"
    ).scalar_one()
    assert count == 1


def test_submit_same_key_different_payload_409(session: Session) -> None:
    project = _seed_project(session)
    _submit(session, project)
    with pytest.raises(ServiceError) as excinfo:
        _submit(session, project, what_done="改了内容但复用同一个键")
    assert excinfo.value.status_code == 409
    assert "idempotency key reuse" in excinfo.value.detail


def test_submit_concurrent_duplicate(session: Session, monkeypatch) -> None:
    """Simulated concurrent duplicate: the pre-insert check misses the winner
    row, the partial unique index raises IntegrityError, and the service
    re-adjudicates by fingerprint instead of leaking the error."""
    project = _seed_project(session)
    winner = _submit(session, project)

    real = WorkLogService._find_by_storage_key
    calls = {"n": 0}

    def flaky(self, storage_key):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # miss the winner -> proceed to a duplicate INSERT
        return real(self, storage_key)

    monkeypatch.setattr(WorkLogService, "_find_by_storage_key", flaky)
    replay = _submit(session, project)
    assert replay.id == winner.id
    count = session.connection().exec_driver_sql(
        "SELECT COUNT(*) FROM artifact WHERE idempotency_key IS NOT NULL"
    ).scalar_one()
    assert count == 1


def test_submit_retry_across_midnight(session: Session, monkeypatch) -> None:
    """The storage key contains no date component, so a retry after UTC
    midnight still replays the original artifact."""
    from datetime import datetime, timedelta

    project = _seed_project(session)
    first = _submit(session, project)

    next_day = datetime.now(UTC) + timedelta(days=1)
    monkeypatch.setattr("aios.work_log.now_utc", lambda: next_day)
    second = _submit(session, project)
    assert second.id == first.id


# ---------------------------------------------------------------------------
# ContentValueJudge (plan §8.1)
# ---------------------------------------------------------------------------


def test_content_value_judge_should_enter_kb_true() -> None:
    value, enter, angle = ContentValueJudge.judge(
        {
            "new_knowledge": "实验结论：" + "细节" * 30,
            "should_enter_kb": True,
        }
    )
    assert enter is True
    assert value == "medium"  # keyword hit + length > 50
    assert angle == ("实验结论：" + "细节" * 30)[:80]


def test_content_value_judge_short_text_low() -> None:
    value, enter, angle = ContentValueJudge.judge({"new_knowledge": "短记录"})
    assert value == "low"
    assert enter is False
    assert angle == "短记录"


def test_content_value_judge_explicit_wins() -> None:
    value, _, angle = ContentValueJudge.judge(
        {
            "new_knowledge": "x",
            "content_value": "high",
            "content_angle": "自定义角度",
        }
    )
    assert value == "high"
    assert angle == "自定义角度"


# ---------------------------------------------------------------------------
# Harvest (plan §8.2)
# ---------------------------------------------------------------------------


def test_harvest_tags_deterministic() -> None:
    tags = map_work_log_tags(
        {
            "new_knowledge": "公众号标题实验：数字型标题打开率更高",
            "content_angle": "小红书也可以复用",
        }
    )
    assert tags == sorted({"knowledge_capture", "wechat_writing", "xhs_adaptation"})
    assert "knowledge_capture" in tags


def test_harvest_tags_all_canonical() -> None:
    samples = [
        {},
        {"new_knowledge": "nothing to see"},
        {"new_knowledge": "定位、包装、封面、排版、视频脚本、用户调研、访谈全命中"},
        {"new_knowledge": "WECHAT and XHS and VIDEO SCRIPT and POSITIONING"},
        {"content_angle": "user research + packaging"},
    ]
    for metadata in samples:
        tags = map_work_log_tags(metadata)
        assert set(tags) <= CANONICAL_KNOWLEDGE_TAGS
        assert normalize_tags(tags) == tags  # never 422, already sorted/deduped


def test_harvest_tags_keyword_hits() -> None:
    assert "wechat_writing" in map_work_log_tags({"new_knowledge": "公众号排版技巧"})
    assert "xhs_adaptation" in map_work_log_tags({"new_knowledge": "小红书笔记"})
    assert map_work_log_tags({"new_knowledge": "无关内容"}) == ["knowledge_capture"]


def _approved_log(
    session: Session,
    project: Project,
    *,
    key: str,
    new_knowledge: str,
    content_value: str = "high",
    should_enter_kb: bool = True,
) -> Artifact:
    artifact = _submit(
        session,
        project,
        key=key,
        new_knowledge=new_knowledge,
        content_value=content_value,
        should_enter_kb=should_enter_kb,
    )
    return WorkLogService(session).attest_work_log(
        artifact_id=artifact.id, actor=OWNER
    )


def test_unattested_work_log_cannot_be_harvested(session: Session) -> None:
    project = _seed_project(session)
    _submit(session, project, content_value="high", should_enter_kb=True)
    created = KnowledgeHarvester(session).harvest_candidates(actor=OWNER)
    assert created == []
    count = session.connection().exec_driver_sql(
        "SELECT COUNT(*) FROM knowledge_candidate"
    ).scalar_one()
    assert count == 0


def test_harvest_creates_candidate_from_high_value(session: Session) -> None:
    project = _seed_project(session)
    log = _approved_log(
        session, project, key="k-h", new_knowledge="公众号数字标题结论沉淀"
    )
    created = KnowledgeHarvester(session).harvest_candidates(actor=OWNER)
    assert len(created) == 1
    candidate = created[0]
    assert candidate.artifact_id == log.id
    assert candidate.project_id == project.id
    assert candidate.source_project_id == project.id
    assert candidate.statement == "公众号数字标题结论沉淀"
    assert "knowledge_capture" in candidate.tags
    assert "wechat_writing" in candidate.tags


def test_harvest_skips_none_and_false(session: Session) -> None:
    project = _seed_project(session)
    _approved_log(
        session,
        project,
        key="k-skip",
        new_knowledge="不值得沉淀的小事",
        content_value="none",
        should_enter_kb=False,
    )
    assert KnowledgeHarvester(session).harvest_candidates(actor=OWNER) == []


def test_harvest_idempotent(session: Session) -> None:
    project = _seed_project(session)
    _approved_log(session, project, key="k-i", new_knowledge="结论：套路可复用沉淀")
    harvester = KnowledgeHarvester(session)
    first = harvester.harvest_candidates(actor=OWNER)
    second = harvester.harvest_candidates(actor=OWNER)
    assert len(first) == 1
    assert second == []
    count = session.connection().exec_driver_sql(
        "SELECT COUNT(*) FROM knowledge_candidate"
    ).scalar_one()
    assert count == 1


def test_harvest_never_auto_approves(session: Session) -> None:
    project = _seed_project(session)
    _approved_log(session, project, key="k-d", new_knowledge="重要结论沉淀")
    created = KnowledgeHarvester(session).harvest_candidates(actor=OWNER)
    assert all(c.status == KnowledgeCandidateStatus.DRAFT for c in created)
    facts = session.connection().exec_driver_sql(
        "SELECT COUNT(*) FROM knowledge_fact"
    ).scalar_one()
    assert facts == 0


def test_harvest_requires_injected_owner_actor(session: Session) -> None:
    with pytest.raises(ServiceError) as excinfo:
        KnowledgeHarvester(session).harvest_candidates(actor=AGENT_ACTOR)
    assert excinfo.value.status_code == 403


# ---------------------------------------------------------------------------
# ContentFeed (plan §8.3)
# ---------------------------------------------------------------------------


def test_feed_project_includes_company_facts(session: Session) -> None:
    project = _seed_project(session)
    log = _approved_log(session, project, key="k-f1", new_knowledge="项目内结论沉淀")
    project_fact = _make_fact(
        session, log, statement="项目事实", series_id="s-proj", project_id=project.id
    )
    company_fact = _make_fact(
        session, log, statement="公司事实", series_id="s-comp", project_id=None
    )
    feed = ContentFeed(session).get_content_feed(actor=OWNER, project_id=project.id)
    ids = {(e["kind"], e["id"]) for e in feed}
    assert ("work_log", log.id) in ids
    assert ("fact", project_fact.id) in ids
    assert ("fact", company_fact.id) in ids


def test_feed_no_cross_project_leakage(session: Session) -> None:
    project_a = _seed_project(session, name="A")
    project_b = _seed_project(session, name="B")
    log_a = _approved_log(session, project_a, key="k-a", new_knowledge="A 的结论沉淀")
    log_b = _approved_log(session, project_b, key="k-b", new_knowledge="B 的结论沉淀")
    fact_b = _make_fact(
        session, log_b, statement="B 事实", series_id="s-b", project_id=project_b.id
    )
    feed = ContentFeed(session).get_content_feed(actor=OWNER, project_id=project_a.id)
    ids = {(e["kind"], e["id"]) for e in feed}
    assert ("work_log", log_a.id) in ids
    assert ("work_log", log_b.id) not in ids
    assert ("fact", fact_b.id) not in ids


def test_feed_stable_order_and_pagination(session: Session) -> None:
    project = _seed_project(session)
    for i in range(5):
        _approved_log(
            session, project, key=f"k-p{i}", new_knowledge=f"结论 {i}：值得沉淀的套路"
        )
    feed = ContentFeed(session)
    full = feed.get_content_feed(actor=OWNER, project_id=project.id, limit=100)
    assert len(full) == 5
    keys = [(e["created_at"], e["id"]) for e in full]
    assert keys == sorted(keys, reverse=True)
    paged = []
    for offset in range(0, 5, 2):
        paged.extend(
            feed.get_content_feed(
                actor=OWNER, project_id=project.id, limit=2, offset=offset
            )
        )
    assert [e["id"] for e in paged] == [e["id"] for e in full]


def test_feed_min_value_threshold(session: Session) -> None:
    project = _seed_project(session)
    _approved_log(
        session,
        project,
        key="k-low",
        new_knowledge="低价值记录",
        content_value="low",
        should_enter_kb=False,
    )
    high = _approved_log(
        session, project, key="k-high", new_knowledge="高价值结论沉淀"
    )
    feed = ContentFeed(session).get_content_feed(actor=OWNER, project_id=project.id)
    ids = [e["id"] for e in feed if e["kind"] == "work_log"]
    assert ids == [high.id]


def test_feed_only_approved_facts_single_head(session: Session) -> None:
    project = _seed_project(session)
    log = _approved_log(session, project, key="k-s", new_knowledge="系列结论沉淀 v1")
    v1 = _make_fact(
        session, log, statement="v1", series_id="s-x", project_id=project.id
    )
    v2 = _make_fact(
        session,
        log,
        statement="v2",
        series_id="s-x",
        version=2,
        project_id=project.id,
        supersedes=v1.id,
    )
    feed = ContentFeed(session).get_content_feed(actor=OWNER, project_id=project.id)
    fact_ids = [e["id"] for e in feed if e["kind"] == "fact"]
    assert v2.id in fact_ids
    assert v1.id not in fact_ids
    superseded = session.get(KnowledgeFact, v1.id)
    assert superseded is not None
    assert superseded.status == KnowledgeFactStatus.SUPERSEDED


def test_feed_log_entries_have_no_tags_field(session: Session) -> None:
    project = _seed_project(session)
    _approved_log(session, project, key="k-nt", new_knowledge="结论沉淀无标签")
    feed = ContentFeed(session).get_content_feed(actor=OWNER, project_id=project.id)
    for entry in feed:
        if entry["kind"] == "work_log":
            assert "tags" not in entry
        else:
            assert "tags" in entry


def test_feed_requires_owner_actor(session: Session) -> None:
    with pytest.raises(ServiceError) as excinfo:
        ContentFeed(session).get_content_feed(actor=AGENT_ACTOR)
    assert excinfo.value.status_code == 403


def test_feed_excludes_evidence_less_approved_log(session: Session) -> None:
    """A WORK_LOG at review_status=APPROVED but lacking attestation evidence
    must not appear in the content feed; only evidence-backed logs are
    publishable (plan §7.2)."""
    project = _seed_project(session)
    ghost = Artifact(
        project_id=project.id,
        type=ArtifactType.WORK_LOG,
        review_status=ArtifactReviewStatus.APPROVED,
        uri="work_log:ghost",
        checksum="c",
        metadata_json={
            "content_value": "high",
            "should_enter_kb": True,
            "new_knowledge": "沉淀结论",
        },
    )
    session.add(ghost)
    session.commit()
    feed = ContentFeed(session).get_content_feed(actor=OWNER, project_id=project.id)
    assert all(entry["kind"] != "work_log" for entry in feed)


@pytest.mark.parametrize("populated", ["agent_platform", "artifact_idempotency"])
def test_migration_0009_downgrade_fail_closed_populated(
    tmp_path: Path, populated: str
) -> None:
    """Seeding (a) agent.platform or (b) artifact.idempotency_key makes the
    downgrade abort with RuntimeError BEFORE any DDL; schema, rows, indexes and
    revision all remain on 20260728_0009."""
    url = f"sqlite:///{(tmp_path / f'work_log_fc_{populated}.db').as_posix()}"
    config = _config(url)
    command.upgrade(config, HEAD)

    with Session(get_engine(url)) as session:
        if populated == "agent_platform":
            session.add(
                Agent(name="a", role="r", adapter_type="external", platform="hermes")
            )
        else:
            project = Project(name="P", objective="O")
            session.add(project)
            session.flush()
            session.add(
                Artifact(
                    project_id=project.id,
                    type=ArtifactType.WORK_LOG,
                    uri="w",
                    checksum="c",
                    idempotency_key="work_log:p:cafebabe",
                )
            )
        session.commit()

    with pytest.raises(RuntimeError):
        command.downgrade(config, PREV)

    # Fail-closed: the 0009 downgrade aborts, so the DB is left exactly on
    # 0009 (the 20260729_0001 downgrade already committed, but 0009's
    # downgrade to PREV raised before any DDL). Schema, rows, indexes and
    # revision all remain on 20260728_0009.
    assert _revision(url) == "20260728_0009"
    _assert_0009_schema_present(url)
    with Session(get_engine(url)) as session:
        if populated == "agent_platform":
            n = session.connection().exec_driver_sql(
                "SELECT COUNT(*) FROM agent WHERE platform = 'hermes'"
            ).scalar_one()
        else:
            n = session.connection().exec_driver_sql(
                "SELECT COUNT(*) FROM artifact WHERE idempotency_key IS NOT NULL"
            ).scalar_one()
    assert n == 1


# ---------------------------------------------------------------------------
# Migration 20260729_0001 (V4 self-registration, plan §11 "迁移" Gate F:
# fail-closed downgrade must preserve the single-use consumption record)
# ---------------------------------------------------------------------------


def test_migration_0029_downgrade_empty_is_lossless(tmp_path: Path) -> None:
    """No consumed bootstrap tokens -> the 0029 downgrade drops the partial
    unique index + ``bootstrap_token_ref`` column cleanly and the revision
    lands on 20260728_0009."""
    url = f"sqlite:///{(tmp_path / 'v4_downgrade_empty.db').as_posix()}"
    config = _config(url)
    command.upgrade(config, HEAD)
    assert _revision(url) == HEAD
    _assert_0029_schema_present(url)

    # Empty V4 registration state -> lossless one-step downgrade.
    command.downgrade(config, "20260728_0009")
    assert _revision(url) == "20260728_0009"
    _assert_0029_schema_absent(url)
    # The 0009 columns/indexes that 0029 did not touch remain intact.
    _assert_0009_schema_present(url)


def test_migration_0029_downgrade_fail_closed_when_tokens_consumed(
    tmp_path: Path,
) -> None:
    """Any agent with a populated ``bootstrap_token_ref`` makes the 0029
    downgrade abort with RuntimeError BEFORE any DDL; the consumption record,
    schema, index and revision all stay on 20260729_0001 (plan §3.2 strict
    single-use permanence)."""
    url = f"sqlite:///{(tmp_path / 'v4_downgrade_fc.db').as_posix()}"
    config = _config(url)
    command.upgrade(config, HEAD)
    assert _revision(url) == HEAD

    # Seed an agent that has consumed a bootstrap token.
    with Session(get_engine(url)) as session:
        session.add(
            Agent(
                name="a",
                role="r",
                adapter_type="external",
                platform="p1",
                external_ref="r1",
                bootstrap_token_ref="jti-consumed",
            )
        )
        session.commit()

    with pytest.raises(RuntimeError):
        command.downgrade(config, "20260728_0009")

    # Fail-closed: nothing was touched. The consumed-token record survives.
    assert _revision(url) == HEAD
    _assert_0029_schema_present(url)
    with Session(get_engine(url)) as session:
        n = session.connection().exec_driver_sql(
            "SELECT COUNT(*) FROM agent WHERE bootstrap_token_ref IS NOT NULL"
        ).scalar_one()
    assert n == 1


def test_artifact_trigger_survives_0009(tmp_path: Path) -> None:
    """The raw ALTER on artifact must not disturb the external trigger
    ``knowledge_candidate_validate_insert`` (it references ``main.artifact``
    literally). After upgrading to 0009 the trigger still exists AND still
    fires: a candidate whose artifact is not APPROVED is rejected."""
    url = f"sqlite:///{(tmp_path / 'trigger_survives.db').as_posix()}"
    run_migrations(url)
    assert _revision(url) == HEAD
    assert "knowledge_candidate_validate_insert" in _trigger_names(url)

    with Session(get_engine(url)) as session:
        project = Project(name="P", objective="O")
        session.add(project)
        session.flush()
        artifact = Artifact(
            project_id=project.id,
            type=ArtifactType.WORK_LOG,
            uri="w",
            checksum="c",
            review_status=ArtifactReviewStatus.UNVERIFIED,
        )
        session.add(artifact)
        session.flush()
        session.add(
            KnowledgeCandidate(
                artifact_id=artifact.id,
                project_id=project.id,
                source_project_id=project.id,
                statement="S",
                submitted_by_kind="owner",
                submitted_by_owner_id="owner",
                submitted_by="owner:owner",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
