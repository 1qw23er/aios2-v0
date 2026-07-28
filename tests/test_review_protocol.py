"""Independent Review Protocol (#64) -- unit + integration tests.

Covers: multi-dimension review capture, reviewer independence rules (no
self-review / trust floor / capability extension), human feedback, fact
reuse via ReviewedFact, and the capped revision loop with owner escalation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlmodel import Session, select

from aios.actor import ActorContext
from aios.agent_registry import register_agent
from aios.api.app import create_app
from aios.campaign import V1_TASKS
from aios.db import get_database_url, get_engine
from aios.execution import ExecutionResult, execute_task
from aios.models import (
    AdapterType,
    Agent,
    AgentTrustLevel,
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    DelegationMode,
    Project,
    ReviewAssignment,
    ReviewDimension,
    ReviewedFact,
    ReviewOverall,
    ReviewPolicy,
    ReviewResult,
    ReviewReviewerType,
    RiskLevel,
)
from aios.models import (
    Task as TaskModel,
)
from aios.review import (
    ReviewError,
    aggregate_reviews,
    assert_revision_lineage,
    dispatch_reviews_for_artifact,
    human_review_present,
    owner_approve_review,
    request_review_revision,
    submit_review,
    submit_review_from_artifact,
    trigger_revision,
)
from aios.services import ServiceError
from alembic import command

# Owner-only review services (owner_approve_review / submit_review_from_artifact /
# request_review_revision) now take a trusted ``ActorContext`` (#74) instead of a
# bare ``"owner"`` string. This is the trusted owner an authenticated request
# resolves to; ``owner_id="owner"`` keeps the audit ``actor`` value == "owner".
_OWNER = ActorContext(kind="owner", owner_id="owner")


def _sample_for_schema(schema: dict[str, object]) -> object:
    """Generate schema-valid placeholder data (no real/hardcoded content)."""
    t = schema.get("type")
    if t == "object":
        return {k: _sample_for_schema(v) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        item = schema.get("items", {"type": "string"})
        return [_sample_for_schema(item) for _ in range(max(schema.get("minItems", 1), 1))]
    if t == "string":
        return "示例文本" if schema.get("minLength", 0) else "x"
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return True
    return "x"


class ScriptedExecutionAdapter:
    """Deterministic ExecutionAdapter: walks the real protocol, only the model
    call is substituted by schema-valid placeholder data."""

    def run(self, *, task_id, task_context, output_schema, idempotency_key):
        data = _sample_for_schema(output_schema)
        return ExecutionResult(
            summary="脚本化执行产物（测试用）",
            claims=[],
            artifacts=[
                {
                    "type": "json",
                    "uri": f"exec://{task_id}/{idempotency_key}",
                    "summary": "脚本化执行产物（测试用）",
                    "data": data,
                }
            ],
        )


@pytest.fixture
def client(trusted_owner_installer, tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'review.db'}")
    monkeypatch.delenv("AIOS_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("AIOS_AGENT_BASE_URL", raising=False)
    app = create_app()
    trusted_owner_installer(app)
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def _session() -> Session:
    return Session(get_engine(get_database_url()))


def _launch(client: TestClient) -> None:
    client.post(
        "/owner/launch",
        data={"name": "评审协议切片", "objective": "验证独立评审与修订循环"},
    )


def _t1(session: Session) -> TaskModel:
    title = next(t["title"] for t in V1_TASKS if t["key"] == "T1")
    return session.exec(select(TaskModel).where(TaskModel.title == title)).first()


def _editorial_policy(session: Session, *, max_revisions: int = 2) -> ReviewPolicy:
    # One-review-task-one-dimension: required_reviewers MUST equal the number of
    # dimensions (D3 hardening invariant). Here both are 2, matching the two
    # fact_research reviewers registered by the flow helpers below.
    policy = ReviewPolicy(
        name="editorial",
        applies_to="editorial",
        dimensions=[
            ReviewDimension.FACT_CORRECTNESS.value,
            ReviewDimension.ACCEPTANCE_CRITERIA.value,
        ],
        required_reviewer_trust=AgentTrustLevel.VERIFIED_EXTERNAL,
        required_capabilities=["fact_research"],
        required_reviewers=2,
        max_revisions=max_revisions,
    )
    session.add(policy)
    session.commit()
    session.refresh(policy)
    return policy


def _agent(
    session: Session,
    *,
    name: str,
    trust: AgentTrustLevel,
    capabilities: list[str] | None = None,
) -> Agent:
    return register_agent(
        session,
        name=name,
        role="reviewer",
        adapter_type=AdapterType.EXTERNAL,
        delegation_mode=DelegationMode.REMOTE_API,
        capabilities=capabilities or [],
        trust_level=trust,
        enabled=True,
    )


def _produce_draft(client: TestClient, session: Session) -> Artifact:
    task = _t1(session)
    return execute_task(session, task.id, "idem-draft", adapter=ScriptedExecutionAdapter())


# --- review capture ---------------------------------------------------------

def test_submit_review_captures_dimensions_and_sets_status(client: TestClient) -> None:
    """A single AGENT PASS must NOT approve directly (trust boundary #4).

    With ``required_reviewers=2`` the artifact stays UNVERIFIED after one
    reviewer; only after a second distinct PASS reviewer does aggregation reach
    REVIEW_PASSED -- proving AI can never auto-approve: the artifact is gated for
    the owner's explicit final approval, never promoted to APPROVED on its own.
    """
    _launch(client)
    with _session() as session:
        draft = _produce_draft(client, session)
        producer = _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        reviewer1 = _agent(
            session, name="research", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
            capabilities=["fact_research"],
        )
        reviewer2 = _agent(
            session, name="strategy", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
            capabilities=["fact_research"],
        )
        policy = _editorial_policy(session)

        submit_review(
            session,
            artifact_id=draft.id,
            reviewer_type=ReviewReviewerType.AGENT,
            reviewer_agent_id=reviewer1.id,
            dimensions=[
                {"dim": ReviewDimension.FACT_CORRECTNESS.value, "verdict": "pass",
                 "evidence": "事实核对通过", "score": 5},
                {"dim": ReviewDimension.BRAND_STRATEGY.value, "verdict": "pass",
                 "evidence": "符合品牌", "score": 4},
            ],
            overall=ReviewOverall.APPROVED,
            reviewer_score=4.5,
            policy=policy,
            executor_agent_id=producer.id,
        )
        # One reviewer PASS -> NOT APPROVED.
        assert session.get(Artifact, draft.id).review_status == (
            ArtifactReviewStatus.UNVERIFIED
        )
        # A conflicting replay (same reviewer, different verdict) is rejected (409).
        with pytest.raises(ReviewError):
            submit_review(
                session,
                artifact_id=draft.id,
                reviewer_type=ReviewReviewerType.AGENT,
                reviewer_agent_id=reviewer1.id,
                dimensions=[
                    {"dim": ReviewDimension.BRAND_STRATEGY.value, "verdict": "needs_revision",
                     "evidence": "定位偏"},
                ],
                overall=ReviewOverall.NEEDS_REVISION,
                policy=policy,
                executor_agent_id=producer.id,
            )

        # Second distinct PASS reviewer -> aggregated REVIEW_PASSED (gated, not APPROVED).
        result2 = submit_review(
            session,
            artifact_id=draft.id,
            reviewer_type=ReviewReviewerType.AGENT,
            reviewer_agent_id=reviewer2.id,
            dimensions=[
                {"dim": ReviewDimension.FACT_CORRECTNESS.value, "verdict": "pass",
                 "evidence": "复核通过", "score": 5},
            ],
            overall=ReviewOverall.APPROVED,
            reviewer_score=4.8,
            policy=policy,
            executor_agent_id=producer.id,
        )
        assert result2.reviewer_agent_id == reviewer2.id
        assert len(result2.dimensions) == 1
        # Required reviewers satisfied -> aggregated REVIEW_PASSED (owner gate opens).
        # AI never promotes to APPROVED here (C1).
        assert session.get(Artifact, draft.id).review_status == (
            ArtifactReviewStatus.REVIEW_PASSED
        )
        # Exactly two ReviewResults exist (one per reviewer, no duplicate).
        assert len(session.exec(select(ReviewResult)).all()) == 2


def test_fact_dimension_reuses_reviewed_fact(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        draft = _produce_draft(client, session)
        reviewer = _agent(
            session, name="research", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
            capabilities=["fact_research"],
        )
        policy = _editorial_policy(session)

        submit_review(
            session,
            artifact_id=draft.id,
            reviewer_type=ReviewReviewerType.AGENT,
            reviewer_agent_id=reviewer.id,
            dimensions=[
                {"dim": ReviewDimension.FACT_CORRECTNESS.value, "verdict": "pass",
                 "statement": "AI觅 是 AIOS 的公众号品牌", "score": 5},
            ],
            overall=ReviewOverall.APPROVED,
            policy=policy,
        )

        facts = session.exec(
            select(ReviewedFact).where(ReviewedFact.artifact_id == draft.id)
        ).all()
        assert len(facts) == 1
        assert facts[0].statement == "AI觅 是 AIOS 的公众号品牌"
        assert facts[0].status.value == "approved"


def test_human_feedback_via_user_reviewer(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        draft = _produce_draft(client, session)

        submit_review(
            session,
            artifact_id=draft.id,
            reviewer_type=ReviewReviewerType.USER,
            user_id="owner-1",
            dimensions=[
                {"dim": ReviewDimension.RISK.value, "verdict": "pass", "evidence": "无风险"},
            ],
            overall=ReviewOverall.APPROVED,
            reviewer_score=5.0,
            usefulness=4.0,
        )

        stored = session.exec(select(ReviewResult)).first()
        assert stored.reviewer_type == ReviewReviewerType.USER
        assert stored.user_id == "owner-1"
        assert stored.usefulness == 4.0
        # USER reviewers are independent by construction.
        assert human_review_present(session, draft.id) is True


# --- independence rules -----------------------------------------------------

def test_self_review_rejected(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        draft = _produce_draft(client, session)
        agent = _agent(session, name="sole", trust=AgentTrustLevel.INTERNAL)
        policy = _editorial_policy(session)

        with pytest.raises(ReviewError):
            submit_review(
                session,
                artifact_id=draft.id,
                reviewer_type=ReviewReviewerType.AGENT,
                reviewer_agent_id=agent.id,
                dimensions=[],
                overall=ReviewOverall.APPROVED,
                policy=policy,
                executor_agent_id=agent.id,  # same agent -> self-review
            )


def test_experimental_reviewer_rejected(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        draft = _produce_draft(client, session)
        producer = _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        experimental = _agent(
            session, name="exp", trust=AgentTrustLevel.EXPERIMENTAL,
            capabilities=["fact_research"],
        )
        policy = _editorial_policy(session)

        with pytest.raises(ReviewError):
            submit_review(
                session,
                artifact_id=draft.id,
                reviewer_type=ReviewReviewerType.AGENT,
                reviewer_agent_id=experimental.id,
                dimensions=[],
                overall=ReviewOverall.APPROVED,
                policy=policy,
                executor_agent_id=producer.id,
            )


def test_capability_mismatch_rejected(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        draft = _produce_draft(client, session)
        producer = _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        # Verified-external but lacking the required "fact_research" capability.
        no_cap = _agent(
            session, name="nocap", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
            capabilities=["something_else"],
        )
        policy = _editorial_policy(session)

        with pytest.raises(ReviewError):
            submit_review(
                session,
                artifact_id=draft.id,
                reviewer_type=ReviewReviewerType.AGENT,
                reviewer_agent_id=no_cap.id,
                dimensions=[],
                overall=ReviewOverall.APPROVED,
                policy=policy,
                executor_agent_id=producer.id,
            )


# --- revision loop ----------------------------------------------------------

def test_revision_loop_capped_then_escalates(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        task = _t1(session)
        draft = execute_task(session, task.id, "idem-draft", adapter=ScriptedExecutionAdapter())
        producer = _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        reviewer = _agent(
            session, name="research", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
            capabilities=["fact_research"],
        )
        policy = _editorial_policy(session, max_revisions=2)

        # 1st NEEDS_REVISION -> revision #1.
        submit_review(
            session, artifact_id=draft.id,
            reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=reviewer.id,
            dimensions=[{"dim": ReviewDimension.BRAND_STRATEGY.value, "verdict": "needs_revision",
                         "evidence": "定位偏"}],
            overall=ReviewOverall.NEEDS_REVISION, policy=policy,
            executor_agent_id=producer.id,
        )
        rev1 = trigger_revision(
            session, task_id=task.id, adapter=ScriptedExecutionAdapter(),
            source_artifact=session.get(Artifact, draft.id), policy=policy,
        )
        assert rev1 is not None
        assert rev1.revision_count == 1
        assert rev1.revision_of == draft.id

        # 2nd NEEDS_REVISION -> revision #2 (hits the cap).
        submit_review(
            session, artifact_id=rev1.id,
            reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=reviewer.id,
            dimensions=[{"dim": ReviewDimension.BRAND_STRATEGY.value, "verdict": "needs_revision",
                         "evidence": "仍偏"}],
            overall=ReviewOverall.NEEDS_REVISION, policy=policy,
            executor_agent_id=producer.id,
        )
        rev2 = trigger_revision(
            session, task_id=task.id, adapter=ScriptedExecutionAdapter(),
            source_artifact=session.get(Artifact, rev1.id), policy=policy,
        )
        assert rev2 is not None
        assert rev2.revision_count == 2
        assert rev2.revision_of == rev1.id

        # 3rd NEEDS_REVISION -> cap reached -> escalate to owner (no new artifact).
        submit_review(
            session, artifact_id=rev2.id,
            reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=reviewer.id,
            dimensions=[{"dim": ReviewDimension.BRAND_STRATEGY.value, "verdict": "needs_revision",
                         "evidence": "还是偏"}],
            overall=ReviewOverall.NEEDS_REVISION, policy=policy,
            executor_agent_id=producer.id,
        )
        escalated = trigger_revision(
            session, task_id=task.id, adapter=ScriptedExecutionAdapter(),
            source_artifact=session.get(Artifact, rev2.id), policy=policy,
        )
        assert escalated is None  # owner gate, no infinite loop
        from aios.models import Approval
        approvals = session.exec(
            select(Approval).where(Approval.task_id == task.id)
        ).all()
        assert len(approvals) == 1
        assert approvals[0].status.value == "pending"


def test_review_protocol_migration_round_trip(tmp_path: Path) -> None:
    """Independent Review Protocol (#64) migration -- upgrade/downgrade round-trip.

    Proves the physical self-referencing FK (``artifact.revision_of`` ->
    ``artifact.id``) and its index are real, that ``ON DELETE SET NULL`` fires,
    that an existing ``artifact`` row survives a column drop/re-add, and that the
    triggers defined on *other* tables (``knowledge_*``) are untouched by the
    ``artifact`` table recreation.

    Uses explicit revision IDs (never ``"head"``) so the conftest copy-shim is
    bypassed and the genuine Alembic engine runs our migration.
    """
    db_file = tmp_path / "review_roundtrip.db"
    url = f"sqlite:///{db_file.as_posix()}"
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)

    def revision() -> str:
        with Session(get_engine(url)) as s:
            return s.connection().exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()

    def artifact_indexes_and_fks() -> tuple[set[str], list[tuple]]:
        with sqlite3.connect(str(db_file)) as c:
            idx = {
                r[0]
                for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND name NOT LIKE 'sqlite_%' AND tbl_name='artifact'"
                )
            }
            fks = [tuple(row) for row in c.execute("PRAGMA foreign_key_list(artifact)")]
        return idx, fks

    def all_triggers() -> set[str]:
        with sqlite3.connect(str(db_file)) as c:
            return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}

    # 1) Build full schema at head (0004) via the real migration.
    command.upgrade(config, "20260719_0004")
    assert revision() == "20260719_0004"

    # Seed a Project + Artifact row that must survive the round-trip.
    # Raw SQL: the DB is pinned at 20260719_0004, but the ORM Artifact model
    # follows the CURRENT head schema (e.g. idempotency_key from 20260728_0009)
    # and would emit an INSERT with columns this snapshot does not have.
    proj_id, seeded_id = "prj_rt_seed", "art_rt_seed"
    with sqlite3.connect(str(db_file)) as c:
        c.execute(
            "INSERT INTO project (id, name, objective, description, status, owner,"
            " budget_limit, budget_used, success_metrics, created_at, updated_at)"
            " VALUES (?, 'rt', 'rt', '', 'PROPOSED', 'human_ceo', 0, 0, '[]',"
            " '2026-07-28 00:00:00', '2026-07-28 00:00:00')",
            (proj_id,),
        )
        c.execute(
            "INSERT INTO artifact (id, project_id, type, uri, checksum, metadata,"
            " review_status, revision_count, provenance, created_at)"
            " VALUES (?, ?, 'JSON', 'u', 'c', '{}', 'UNVERIFIED', 0, '{}',"
            " '2026-07-28 00:00:00')",
            (seeded_id, proj_id),
        )
        c.commit()

    # 2) At head: the physical self-ref FK + index must exist, with ON DELETE SET NULL.
    idx, fks = artifact_indexes_and_fks()
    assert "ix_artifact_revision_of" in idx
    rev_fks = [f for f in fks if f[3] == "revision_of"]  # f[3] = local column
    assert rev_fks, "expected a foreign key on artifact.revision_of"
    assert rev_fks[0][2] == "artifact"   # f[2] = referenced table
    assert rev_fks[0][4] == "id"          # f[4] = referenced column
    assert (rev_fks[0][6] or "").upper() == "SET NULL"  # f[6] = on_delete

    base_triggers = all_triggers()
    assert base_triggers, "expected triggers (knowledge_*) to be present at head"

    # 3) Prove the ON DELETE SET NULL action actually fires (needs FK enforcement ON).
    parent_id, child_id = "art_rt_parent", "art_rt_child"
    with sqlite3.connect(str(db_file)) as c:
        c.execute(
            "INSERT INTO artifact (id, project_id, type, uri, checksum, metadata,"
            " review_status, revision_count, provenance, created_at)"
            " VALUES (?, ?, 'JSON', 'up', 'cp', '{}', 'UNVERIFIED', 0, '{}',"
            " '2026-07-28 00:00:00')",
            (parent_id, proj_id),
        )
        c.execute(
            "INSERT INTO artifact (id, project_id, type, uri, checksum, metadata,"
            " review_status, revision_count, revision_of, provenance, created_at)"
            " VALUES (?, ?, 'JSON', 'uc', 'cc', '{}', 'UNVERIFIED', 0, ?, '{}',"
            " '2026-07-28 00:00:00')",
            (child_id, proj_id, parent_id),
        )
        c.commit()
    with sqlite3.connect(str(db_file)) as c:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("DELETE FROM artifact WHERE id=?", (parent_id,))
        c.commit()
        child_revision_of = c.execute(
            "SELECT revision_of FROM artifact WHERE id=?", (child_id,)
        ).fetchone()[0]
    assert child_revision_of is None, "ON DELETE SET NULL must null the child pointer"
    with sqlite3.connect(str(db_file)) as c:
        c.execute("DELETE FROM artifact WHERE id=?", (child_id,))
        c.commit()

    # 4) Downgrade to 0003: revision_of column dropped, but the seeded row survives.
    #    Query via raw SQL -- the ORM still declares revision_of and would error.
    command.downgrade(config, "20260719_0003")
    assert revision() == "20260719_0003"
    with sqlite3.connect(str(db_file)) as c:
        row = c.execute("SELECT id FROM artifact WHERE id=?", (seeded_id,)).fetchone()
        assert row is not None, "seeded artifact row must survive the downgrade"
        cols = {r[1] for r in c.execute("PRAGMA table_info(artifact)")}
        assert "revision_of" not in cols

    # 5) Re-upgrade to head: row preserved, FK + index re-created, triggers intact.
    command.upgrade(config, "20260719_0004")
    assert revision() == "20260719_0004"
    with sqlite3.connect(str(db_file)) as c:
        assert c.execute(
            "SELECT id FROM artifact WHERE id=?", (seeded_id,)
        ).fetchone() is not None
    idx2, fks2 = artifact_indexes_and_fks()
    assert "ix_artifact_revision_of" in idx2
    assert any(f[3] == "revision_of" for f in fks2)
    assert all_triggers() == base_triggers, "no trigger may be lost across the round-trip"


def test_review_protocol_migration_previous_head_to_new_head(tmp_path: Path) -> None:
    """Incremental upgrade path: a database already at the *previous* head
    (20260719_0003) must migrate cleanly to the new head (20260719_0004).

    This is the real deployment scenario -- existing installations are stamped
    at the prior head and only the new migration runs. It must create the
    ``revision_of`` physical FK + index and the two review tables, without
    touching any pre-existing object, and the existing triggers must remain.
    """
    db_file = tmp_path / "prev_head.db"
    url = f"sqlite:///{db_file.as_posix()}"
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)

    def artifact_detail() -> tuple[set[str], set[tuple]]:
        with sqlite3.connect(str(db_file)) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(artifact)")}
            fks = [
                tuple(row)
                for row in c.execute("PRAGMA foreign_key_list(artifact)")
                if row[3] == "revision_of"
            ]
        return cols, fks

    def all_triggers() -> set[str]:
        with sqlite3.connect(str(db_file)) as c:
            return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}

    # 1) Migrate only up to the previous head (20260719_0003).
    command.upgrade(config, "20260719_0003")
    with sqlite3.connect(str(db_file)) as c:
        assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "20260719_0003"
        )
        cols = {r[1] for r in c.execute("PRAGMA table_info(artifact)")}
        assert "revision_of" not in cols
        prev_triggers = {
            r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
    assert prev_triggers, "previous head must still carry the knowledge_* triggers"

    # 2) Upgrade the previous head to the new head (incremental -- only 0004 runs).
    command.upgrade(config, "20260719_0004")
    with sqlite3.connect(str(db_file)) as c:
        assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "20260719_0004"
        )
        cols = {r[1] for r in c.execute("PRAGMA table_info(artifact)")}
        assert "revision_of" in cols
        assert "revision_count" in cols
        fks = [
            tuple(row)
            for row in c.execute("PRAGMA foreign_key_list(artifact)")
            if row[3] == "revision_of"
        ]
        assert fks, "physical self-ref FK must exist after incremental upgrade"
        assert fks[0][2] == "artifact" and fks[0][4] == "id"
        # review tables created
        tbls = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"review_policy", "review_result"} <= tbls
        # triggers preserved exactly across the incremental upgrade
        assert {
            r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        } == prev_triggers

    # 3) The CURRENT head schema is immediately writable/readable via the ORM.
    # The ORM models track the real head (e.g. Artifact.idempotency_key from
    # 20260728_0009), so finish the upgrade chain before using the ORM.
    command.upgrade(config, "head")
    with Session(get_engine(url)) as s:
        proj = Project(name="ph", objective="ph")
        s.add(proj)
        s.flush()
        art = Artifact(project_id=proj.id, type=ArtifactType.JSON, uri="u", checksum="c")
        s.add(art)
        s.flush()
        seeded_id = art.id
        s.commit()
    with Session(get_engine(url)) as s:
        assert s.get(Artifact, seeded_id) is not None
        cols, fks = artifact_detail()
        assert "revision_of" in cols and fks


def test_review_revision_lineage_guards(client) -> None:
    """Service layer must reject cross-project, self-reference, and cyclic
    revision_of links even though the DB FK only checks row existence.

    Uses the migrated test DB (head 20260719_0004) via the ``client`` fixture.
    """
    session = _session()
    proj_a = Project(name="A", objective="A")
    proj_b = Project(name="B", objective="B")
    session.add_all([proj_a, proj_b])
    session.flush()

    # (a) self-reference is forbidden.
    art = Artifact(project_id=proj_a.id, type=ArtifactType.JSON, uri="ua", checksum="ca")
    session.add(art)
    session.flush()
    with pytest.raises(ReviewError):
        assert_revision_lineage(session, art, art.id)

    # (b) cross-project link is forbidden.
    pa = Artifact(project_id=proj_a.id, type=ArtifactType.JSON, uri="upa", checksum="cpa")
    pb = Artifact(project_id=proj_b.id, type=ArtifactType.JSON, uri="upb", checksum="cpb")
    session.add_all([pa, pb])
    session.flush()
    with pytest.raises(ReviewError):
        assert_revision_lineage(session, pb, pa.id)

    # (c) a cycle (b.revision_of = c, then linking c.revision_of = b) is forbidden.
    b = Artifact(project_id=proj_a.id, type=ArtifactType.JSON, uri="ub", checksum="cb")
    c = Artifact(project_id=proj_a.id, type=ArtifactType.JSON, uri="uc", checksum="cc")
    session.add_all([b, c])
    session.flush()
    b.revision_of = c.id
    session.add(b)
    session.flush()
    with pytest.raises(ReviewError):
        assert_revision_lineage(session, c, b.id)

    # (d) a valid link to a root artifact in the same project is accepted.
    d = Artifact(project_id=proj_a.id, type=ArtifactType.JSON, uri="ud", checksum="cd")
    session.add(d)
    session.flush()
    assert_revision_lineage(session, d, pa.id)  # must not raise


# --- identity integrity (inverse) + feedback semantics -------------------


def test_agent_reviewer_must_not_carry_user_id(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        draft = _produce_draft(client, session)
        agent = _agent(session, name="r", trust=AgentTrustLevel.VERIFIED_EXTERNAL)
        with pytest.raises(ReviewError):
            submit_review(
                session, artifact_id=draft.id,
                reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=agent.id,
                user_id="some-human", dimensions=[],
                overall=ReviewOverall.APPROVED,
            )


def test_user_reviewer_must_not_carry_agent_id(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        draft = _produce_draft(client, session)
        agent = _agent(session, name="r", trust=AgentTrustLevel.VERIFIED_EXTERNAL)
        with pytest.raises(ReviewError):
            submit_review(
                session, artifact_id=draft.id,
                reviewer_type=ReviewReviewerType.USER, user_id="owner-1",
                reviewer_agent_id=agent.id, dimensions=[],
                overall=ReviewOverall.APPROVED,
            )


def test_agent_and_user_both_set_rejected(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        draft = _produce_draft(client, session)
        agent = _agent(session, name="r", trust=AgentTrustLevel.VERIFIED_EXTERNAL)
        with pytest.raises(ReviewError):
            submit_review(
                session, artifact_id=draft.id,
                reviewer_type=ReviewReviewerType.AGENT,
                reviewer_agent_id=agent.id, user_id="owner-1", dimensions=[],
                overall=ReviewOverall.APPROVED,
            )


def test_agent_cannot_set_usefulness(client: TestClient) -> None:
    """usefulness is a human-in-the-loop signal; agents must not set it."""
    _launch(client)
    with _session() as session:
        draft = _produce_draft(client, session)
        agent = _agent(session, name="r", trust=AgentTrustLevel.VERIFIED_EXTERNAL)
        with pytest.raises(ReviewError):
            submit_review(
                session, artifact_id=draft.id,
                reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=agent.id,
                dimensions=[], overall=ReviewOverall.APPROVED, usefulness=4.0,
            )


def test_user_usefulness_allowed(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        draft = _produce_draft(client, session)
        res = submit_review(
            session, artifact_id=draft.id,
            reviewer_type=ReviewReviewerType.USER, user_id="owner-1",
            dimensions=[{"dim": ReviewDimension.RISK.value, "verdict": "pass"}],
            overall=ReviewOverall.APPROVED, reviewer_score=5.0, usefulness=4.0,
        )
        assert res.usefulness == 4.0
        assert res.reviewer_type == ReviewReviewerType.USER


# --- review idempotency (trust boundary #3) -----------------------------


def test_review_replay_identical_returns_original(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        draft = _produce_draft(client, session)
        producer = _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        reviewer = _agent(
            session, name="research", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
            capabilities=["fact_research"],
        )
        policy = _editorial_policy(session)
        first = submit_review(
            session, artifact_id=draft.id,
            reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=reviewer.id,
            dimensions=[{"dim": ReviewDimension.FACT_CORRECTNESS.value, "verdict": "pass"}],
            overall=ReviewOverall.APPROVED, reviewer_score=4.5, policy=policy,
            executor_agent_id=producer.id,
        )
        # Identical replay -> returns the ORIGINAL row (no duplicate).
        second = submit_review(
            session, artifact_id=draft.id,
            reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=reviewer.id,
            dimensions=[{"dim": ReviewDimension.FACT_CORRECTNESS.value, "verdict": "pass"}],
            overall=ReviewOverall.APPROVED, reviewer_score=4.5, policy=policy,
            executor_agent_id=producer.id,
        )
        assert second.id == first.id
        assert len(session.exec(select(ReviewResult)).all()) == 1


def test_review_replay_conflicting_returns_409(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        draft = _produce_draft(client, session)
        producer = _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        reviewer = _agent(
            session, name="research", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
            capabilities=["fact_research"],
        )
        policy = _editorial_policy(session)
        submit_review(
            session, artifact_id=draft.id,
            reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=reviewer.id,
            dimensions=[{"dim": ReviewDimension.FACT_CORRECTNESS.value, "verdict": "pass"}],
            overall=ReviewOverall.APPROVED, policy=policy,
            executor_agent_id=producer.id,
        )
        # Same identity, different verdict -> conflict, rejected with 409.
        with pytest.raises(ReviewError) as exc:
            submit_review(
                session, artifact_id=draft.id,
                reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=reviewer.id,
                dimensions=[{"dim": ReviewDimension.FACT_CORRECTNESS.value, "verdict": "fail"}],
                overall=ReviewOverall.NEEDS_REVISION, policy=policy,
                executor_agent_id=producer.id,
            )
        assert exc.value.status_code == 409
        # Exactly one ReviewResult remains (no duplicate, no overwrite).
        assert len(session.exec(select(ReviewResult)).all()) == 1


# --- deterministic aggregation (trust boundary #4) -------------------------


def test_aggregate_reject_wins(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        draft = _produce_draft(client, session)
        producer = _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        reviewer1 = _agent(session, name="r1", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
                           capabilities=["fact_research"])
        reviewer2 = _agent(session, name="r2", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
                           capabilities=["fact_research"])
        policy = _editorial_policy(session)
        submit_review(session, artifact_id=draft.id,
                        reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=reviewer1.id,
                        dimensions=[], overall=ReviewOverall.APPROVED, policy=policy,
                        executor_agent_id=producer.id)
        submit_review(session, artifact_id=draft.id,
                        reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=reviewer2.id,
                        dimensions=[], overall=ReviewOverall.REJECTED, policy=policy,
                        executor_agent_id=producer.id)
        assert session.get(Artifact, draft.id).review_status == ArtifactReviewStatus.REJECTED


def test_aggregate_needs_revision_wins(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        draft = _produce_draft(client, session)
        producer = _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        reviewer1 = _agent(session, name="r1", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
                           capabilities=["fact_research"])
        reviewer2 = _agent(session, name="r2", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
                           capabilities=["fact_research"])
        policy = _editorial_policy(session)
        submit_review(session, artifact_id=draft.id,
                        reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=reviewer1.id,
                        dimensions=[], overall=ReviewOverall.APPROVED, policy=policy,
                        executor_agent_id=producer.id)
        submit_review(session, artifact_id=draft.id,
                        reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=reviewer2.id,
                        dimensions=[], overall=ReviewOverall.NEEDS_REVISION, policy=policy,
                        executor_agent_id=producer.id)
        assert session.get(Artifact, draft.id).review_status == (
            ArtifactReviewStatus.NEEDS_REVISION
        )


def test_aggregate_required_reviewers_cannot_be_skipped(client: TestClient) -> None:
    """Single PASS -> UNVERIFIED; one short of required -> UNVERIFIED;
    second distinct PASS -> REVIEW_PASSED (owner gate). Required reviews cannot be
    skipped, and AI never auto-approves (C1)."""
    _launch(client)
    with _session() as session:
        draft = _produce_draft(client, session)
        producer = _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        reviewer1 = _agent(session, name="r1", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
                           capabilities=["fact_research"])
        reviewer2 = _agent(session, name="r2", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
                           capabilities=["fact_research"])
        policy = _editorial_policy(session, max_revisions=2)  # required_reviewers defaults to 2

        submit_review(session, artifact_id=draft.id,
                        reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=reviewer1.id,
                        dimensions=[], overall=ReviewOverall.APPROVED, policy=policy,
                        executor_agent_id=producer.id)
        assert session.get(Artifact, draft.id).review_status == (
            ArtifactReviewStatus.UNVERIFIED
        )

        # A second submit from the SAME reviewer must not count twice.
        submit_review(session, artifact_id=draft.id,
                        reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=reviewer1.id,
                        dimensions=[], overall=ReviewOverall.APPROVED, policy=policy,
                        executor_agent_id=producer.id)
        assert session.get(Artifact, draft.id).review_status == (
            ArtifactReviewStatus.UNVERIFIED
        )

        # A distinct second PASS reviewer satisfies the required count -> REVIEW_PASSED.
        submit_review(session, artifact_id=draft.id,
                        reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=reviewer2.id,
                        dimensions=[], overall=ReviewOverall.APPROVED, policy=policy,
                        executor_agent_id=producer.id)
        assert session.get(Artifact, draft.id).review_status == (
            ArtifactReviewStatus.REVIEW_PASSED
        )


# --- revision / escalation concurrency (trust boundary #5) -----------------


def test_trigger_revision_concurrent_only_one_child(client: TestClient) -> None:
    """Two revision requests for the same source create only ONE next
    artifact/execution (idempotent), and revision_count is server-derived."""
    _launch(client)
    with _session() as session:
        task = _t1(session)
        draft = execute_task(session, task.id, "idem-draft", adapter=ScriptedExecutionAdapter())
        producer = _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        reviewer = _agent(session, name="research", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
                           capabilities=["fact_research"])
        policy = _editorial_policy(session, max_revisions=2)

        submit_review(session, artifact_id=draft.id,
                        reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=reviewer.id,
                        dimensions=[{"dim": ReviewDimension.BRAND_STRATEGY.value,
                                    "verdict": "needs_revision"}],
                        overall=ReviewOverall.NEEDS_REVISION, policy=policy,
                        executor_agent_id=producer.id)

        first = trigger_revision(session, task_id=task.id,
                                 adapter=ScriptedExecutionAdapter(),
                                 source_artifact=session.get(Artifact, draft.id), policy=policy)
        # A concurrent (double) call returns the SAME child, no second execution.
        second = trigger_revision(session, task_id=task.id,
                                  adapter=ScriptedExecutionAdapter(),
                                  source_artifact=session.get(Artifact, draft.id), policy=policy)
        assert second.id == first.id
        assert second.revision_count == 1
        # Exactly: draft + one revision (no duplicate Artifact).
        total = session.exec(
            select(func.count(Artifact.id)).where(Artifact.task_id == task.id)
        ).first()
        assert total == 2


def test_escalation_double_call_single_approval(client: TestClient) -> None:
    """Reaching max_revisions creates exactly ONE pending owner Approval,
    even if escalation is requested twice."""
    _launch(client)
    with _session() as session:
        task = _t1(session)
        capped = Artifact(
            project_id=session.get(TaskModel, task.id).project_id,
            task_id=task.id, type=ArtifactType.JSON, uri="u", checksum="c",
            revision_count=2,
        )
        session.add(capped)
        session.flush()
        policy = _editorial_policy(session, max_revisions=2)

        trigger_revision(session, task_id=task.id, adapter=ScriptedExecutionAdapter(),
                        source_artifact=capped, policy=policy)
        # Second escalation request must not create a duplicate owner gate.
        trigger_revision(session, task_id=task.id, adapter=ScriptedExecutionAdapter(),
                        source_artifact=capped, policy=policy)

        from aios.models import Approval
        approvals = session.exec(
            select(Approval).where(Approval.task_id == task.id)
        ).all()
        assert len(approvals) == 1
        assert approvals[0].status.value == "pending"



# --- #69 runtime wiring (C1-C6): endpoints + service orchestration ---------


class ScriptedReviewAdapter:
    """Deterministic adapter producing a schema-valid review verdict so the Review
    Task's Artifact satisfies ``REVIEW_VERDICT_SCHEMA`` (execute_task validates
    ``data`` against the task output_schema before persisting the artifact)."""

    def __init__(self, overall: str = "approved", score: float = 4.5) -> None:
        self.overall = overall
        self.score = score

    def run(self, *, task_id, task_context, output_schema, idempotency_key):
        # Req 2: emit the dimension this Review Task was server-bound to, looked
        # up from the immutable ReviewAssignment. This makes the verdict pass the
        # dimension enforcement in submit_review_from_artifact (a forged dimension
        # is rejected). Defaults to fact_correctness when no binding exists.
        assigned_dim = "fact_correctness"
        try:
            with _session() as s:
                ra = s.get(ReviewAssignment, task_id)
                if ra is not None:
                    assigned_dim = ra.review_dimension
        except Exception:
            pass
        return ExecutionResult(
            summary="脚本化评审产物（测试用）",
            claims=[],
            artifacts=[
                {
                    "type": "json",
                    "uri": f"exec://{task_id}/{idempotency_key}",
                    "summary": "脚本化评审产物（测试用）",
                    "data": {
                        "overall": self.overall,
                        "reviewer_score": self.score,
                        "dimensions": [
                            {"dim": assigned_dim, "verdict": "pass",
                             "evidence": "核对通过", "score": 5},
                        ],
                    },
                }
            ],
        )


def _wire_review_flow(session: Session, *, with_approve: bool = False):
    """Shared helper: launch -> agents -> policy -> draft -> dispatch -> execute
    -> submit both reviewers -> (optionally) owner approve. Returns (draft, tasks)."""
    producer = _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
    _agent(session, name="r1", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
           capabilities=["fact_research"])
    _agent(session, name="r2", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
           capabilities=["fact_research"])
    policy = _editorial_policy(session)
    task = _t1(session)
    draft = execute_task(session, task.id, "idem-draft", adapter=ScriptedExecutionAdapter())
    tasks = dispatch_reviews_for_artifact(
        session, target_artifact_id=draft.id, policy=policy, executor_agent_id=producer.id
    )
    assert len(tasks) == 2
    for rt in tasks:
        execute_task(session, rt.id, f"idem-review-{rt.id}", adapter=ScriptedReviewAdapter())
        submit_review_from_artifact(session, review_task_id=rt.id, actor=_OWNER)
    assert session.get(Artifact, draft.id).review_status == ArtifactReviewStatus.REVIEW_PASSED
    if with_approve:
        owner_approve_review(session, artifact_id=draft.id, actor=_OWNER)
        assert session.get(Artifact, draft.id).review_status == ArtifactReviewStatus.APPROVED
    return draft, tasks


def test_runtime_full_flow_dispatch_execute_submit_owner_approve(client: TestClient) -> None:
    """End-to-end wiring (C1-C6): dispatch -> execute -> submit -> owner gate."""
    _launch(client)
    with _session() as session:
        draft, tasks = _wire_review_flow(session, with_approve=True)
        # Immutable server-owned binding (req 1/2/6): one ReviewAssignment per
        # Review Task, persisted in its own table -- NOT in mutable artifact
        # metadata. Each task is bound to EXACTLY one dimension.
        for rt in tasks:
            assert rt.assigned_agent_id is not None
            assert draft.task_id in (rt.depends_on or [])
            ra = session.get(ReviewAssignment, rt.id)
            assert ra is not None
            assert ra.target_artifact_id == draft.id
            assert ra.review_round == 1
            assert ra.reviewer_agent_id == rt.assigned_agent_id
            assert ra.review_dimension  # exactly one dimension bound
        # The binding is authoritative: the target artifact carries no review_binding.
        assert "review_binding" not in (draft.metadata_json or {})
        # Exactly one review_gate Approval, bound to (target, policy, round) and
        # now approved by the owner.
        gate = session.exec(
            select(Approval).where(
                Approval.target_artifact_id == draft.id,
                Approval.action_type == "review_gate",
            )
        ).all()
        assert len(gate) == 1
        assert gate[0].status.value == "approved"
        assert gate[0].review_round == 1
        assert gate[0].target_artifact_id == draft.id


def test_runtime_owner_approve_requires_review_passed(client: TestClient) -> None:
    """owner_approve_review refuses an artifact not in REVIEW_PASSED (C1) -- AI
    reviewers can never substitute for the owner final approval."""
    _launch(client)
    with _session() as session:
        task = _t1(session)
        draft = execute_task(session, task.id, "idem-draft", adapter=ScriptedExecutionAdapter())
        with pytest.raises(ServiceError):
            owner_approve_review(session, artifact_id=draft.id, actor=_OWNER)


def test_runtime_submit_from_artifact_idempotent(client: TestClient) -> None:
    """Re-submitting the same completed Review Task returns the SAME ReviewResult
    (idempotency boundary #3 / C4); no duplicate ReviewResult is created."""
    _launch(client)
    with _session() as session:
        producer = _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        _agent(session, name="r1", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        _agent(session, name="r2", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        policy = _editorial_policy(session)
        task = _t1(session)
        draft = execute_task(session, task.id, "idem-draft", adapter=ScriptedExecutionAdapter())
        tasks = dispatch_reviews_for_artifact(
            session, target_artifact_id=draft.id, policy=policy, executor_agent_id=producer.id
        )
        rt = tasks[0]
        execute_task(session, rt.id, f"idem-review-{rt.id}", adapter=ScriptedReviewAdapter())
        first = submit_review_from_artifact(session, review_task_id=rt.id, actor=_OWNER)
        second = submit_review_from_artifact(session, review_task_id=rt.id, actor=_OWNER)
        assert second.id == first.id
        count = session.exec(
            select(func.count(ReviewResult.id)).where(
                ReviewResult.artifact_id == draft.id
            )
        ).first()
        assert count == 1


def test_runtime_request_revision_prepares_only_no_llm(client: TestClient) -> None:
    """req 4: owner revision request is PREPARE-ONLY -- it must NOT synchronously
    call execute_task / a remote LLM. It records the reason, prepares ONE READY
    revision Task, invalidates the old gate, and returns."""
    _launch(client)
    with _session() as session:
        producer = _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        _agent(session, name="r1", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        _agent(session, name="r2", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        policy = _editorial_policy(session)
        task = _t1(session)
        draft = execute_task(session, task.id, "idem-draft", adapter=ScriptedExecutionAdapter())
        tasks = dispatch_reviews_for_artifact(
            session, target_artifact_id=draft.id, policy=policy, executor_agent_id=producer.id
        )
        for rt in tasks:
            execute_task(session, rt.id, f"idem-review-{rt.id}", adapter=ScriptedReviewAdapter())
            submit_review_from_artifact(session, review_task_id=rt.id, actor=_OWNER)
        assert session.get(Artifact, draft.id).review_status == ArtifactReviewStatus.REVIEW_PASSED
        # Sanity: a pending review_gate Approval exists for the old round.
        old_gate = session.exec(
            select(Approval).where(
                Approval.target_artifact_id == draft.id,
                Approval.action_type == "review_gate",
                Approval.status == ApprovalStatus.PENDING,
            )
        ).first()
        assert old_gate is not None

        before_artifacts = session.exec(select(func.count(Artifact.id))).first()

        # The owner requests a revision. This must NOT run the LLM synchronously.
        rev_task = request_review_revision(
            session, task_id=task.id, feedback="需要更精确的数据", actor=_OWNER
        )
        # No new artifact was produced by this call (no execution happened).
        after_artifacts = session.exec(select(func.count(Artifact.id))).first()
        assert after_artifacts == before_artifacts
        # Exactly one READY revision Task was prepared.
        assert rev_task.status.value == "ready"
        assert rev_task.depends_on == [task.id]
        # The old pending gate was invalidated (REJECTED) -- it can no longer
        # approve anything.
        session.refresh(old_gate)
        assert old_gate.status.value == "rejected"
        # The source artifact is in a terminal NEEDS_REVISION state (awaiting the
        # new revision) -- NOT a meaningless UNVERIFIED reset (req 5).
        assert session.get(Artifact, draft.id).review_status == (
            ArtifactReviewStatus.NEEDS_REVISION
        )


# --- req 8 blocking tests -----------------------------------------------------


def test_one_content_task_multiple_revision_artifacts_exact_target(client: TestClient) -> None:
    """req 8 / req 1: a content task may have multiple revision artifacts. The
    review binding must resolve the EXACT target artifact -- not 'content task ->
    latest artifact'. We dispatch reviews on the ORIGINAL artifact A while two
    revision artifacts (B, C) of A also exist, and prove the binding targets A."""
    _launch(client)
    with _session() as session:
        _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        _agent(session, name="r1", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        _agent(session, name="r2", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        policy = _editorial_policy(session)
        task = _t1(session)
        a = execute_task(session, task.id, "idem-a", adapter=ScriptedExecutionAdapter())
        # Two revision artifacts of A, both belonging to the same content task.
        b = Artifact(
            project_id=a.project_id, task_id=task.id, type=ArtifactType.JSON,
            uri="exec://rev-b", checksum="b", revision_of=a.id, revision_count=1,
            review_status=ArtifactReviewStatus.UNVERIFIED,
        )
        c = Artifact(
            project_id=a.project_id, task_id=task.id, type=ArtifactType.JSON,
            uri="exec://rev-c", checksum="c", revision_of=a.id, revision_count=2,
            review_status=ArtifactReviewStatus.UNVERIFIED,
        )
        session.add(b)
        session.add(c)
        session.commit()
        # Dispatch reviews for A specifically.
        tasks = dispatch_reviews_for_artifact(
            session, target_artifact_id=a.id, policy=policy, executor_agent_id=None
        )
        assert len(tasks) == 2
        # The immutable binding targets A exactly -- never B or C.
        for rt in tasks:
            ra = session.get(ReviewAssignment, rt.id)
            assert ra.target_artifact_id == a.id
            assert ra.target_artifact_id != b.id
            assert ra.target_artifact_id != c.id


def test_exact_target_artifact_resolution_via_assignment(client: TestClient) -> None:
    """req 8 / req 1: submit_review_from_artifact resolves the target from the
    immutable ReviewAssignment (exact artifact id), independent of 'latest'."""
    _launch(client)
    with _session() as session:
        _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        _agent(session, name="r1", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        _agent(session, name="r2", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        policy = _editorial_policy(session)
        task = _t1(session)
        a = execute_task(session, task.id, "idem-a2", adapter=ScriptedExecutionAdapter())
        tasks = dispatch_reviews_for_artifact(
            session, target_artifact_id=a.id, policy=policy, executor_agent_id=None
        )
        rt = tasks[0]
        execute_task(session, rt.id, f"idem-review-{rt.id}", adapter=ScriptedReviewAdapter())
        result = submit_review_from_artifact(session, review_task_id=rt.id, actor=_OWNER)
        # The ReviewResult is recorded against the EXACT target artifact A.
        assert result.artifact_id == a.id
        assert result.review_task_id == rt.id
        assert result.review_round == 1


def test_dimension_binding_cannot_be_forged(client: TestClient) -> None:
    """req 8 / req 2: a reviewer output that submits a dimension other than the
    server-assigned one is rejected (422)."""
    _launch(client)
    with _session() as session:
        _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        _agent(session, name="r1", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        _agent(session, name="r2", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        policy = _editorial_policy(session)
        task = _t1(session)
        a = execute_task(session, task.id, "idem-a3", adapter=ScriptedExecutionAdapter())
        tasks = dispatch_reviews_for_artifact(
            session, target_artifact_id=a.id, policy=policy, executor_agent_id=None
        )
        rt = tasks[0]
        # Find the dimension this Review Task was bound to.
        ra = session.get(ReviewAssignment, rt.id)
        forged_dim = (
            ReviewDimension.RISK.value
            if ra.review_dimension != ReviewDimension.RISK.value
            else ReviewDimension.BRAND_STRATEGY.value
        )
        # Forged adapter emits a dimension different from the assignment.
        class ForgedDimAdapter:
            def run(self, *, task_id, task_context, output_schema, idempotency_key):
                return ExecutionResult(
                    summary="forged", claims=[],
                    artifacts=[{
                        "type": "json",
                        "uri": f"exec://{task_id}/{idempotency_key}",
                        "summary": "forged",
                        "data": {
                            "overall": "approved",
                            "reviewer_score": 4.0,
                            "dimensions": [
                                {"dim": forged_dim, "verdict": "pass",
                                 "evidence": "x", "score": 5},
                            ],
                        },
                    }],
                )

        execute_task(session, rt.id, f"idem-forged-{rt.id}", adapter=ForgedDimAdapter())
        with pytest.raises(ServiceError):
            submit_review_from_artifact(session, review_task_id=rt.id, actor=_OWNER)


def test_old_round_results_excluded_from_aggregation(client: TestClient) -> None:
    """req 8 / req 3: old-round ReviewResults must never satisfy a new round. We
    seed round-1 APPROVED results, confirm aggregation reaches REVIEW_PASSED, then
    advance the artifact to round 2 (no results) and confirm aggregation drops
    back to UNVERIFIED (round-1 results are excluded)."""
    _launch(client)
    with _session() as session:
        _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        _agent(session, name="r1", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        _agent(session, name="r2", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        policy = _editorial_policy(session)
        task = _t1(session)
        a = execute_task(session, task.id, "idem-oldround", adapter=ScriptedExecutionAdapter())
        # Round 1: two assignments + APPROVED results.
        tasks = dispatch_reviews_for_artifact(
            session, target_artifact_id=a.id, policy=policy, executor_agent_id=None
        )
        for rt in tasks:
            execute_task(session, rt.id, f"idem-r1-{rt.id}", adapter=ScriptedReviewAdapter())
            submit_review_from_artifact(session, review_task_id=rt.id, actor=_OWNER)
        assert session.get(Artifact, a.id).review_status == ArtifactReviewStatus.REVIEW_PASSED
        # Advance to round 2: new assignments without any results.
        a.revision_count = 1
        session.add(a)
        session.commit()
        dispatch_reviews_for_artifact(
            session, target_artifact_id=a.id, policy=policy,
            executor_agent_id=None, review_round=2,
        )
        # Re-aggregate: only round-2 bindings exist, with no results -> UNVERIFIED.
        status = aggregate_reviews(session, a.id, policy, actor="owner")
        assert status == ArtifactReviewStatus.UNVERIFIED
        assert session.get(Artifact, a.id).review_status == ArtifactReviewStatus.UNVERIFIED


def test_owner_approval_bound_to_exact_round(client: TestClient) -> None:
    """req 8 / req 5: the owner Approval is bound to the exact (target, policy,
    round). A new revision round gets its OWN gate; the old gate cannot approve
    the new revision."""
    _launch(client)
    with _session() as session:
        _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        _agent(session, name="r1", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        _agent(session, name="r2", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        policy = _editorial_policy(session)
        task = _t1(session)
        a = execute_task(session, task.id, "idem-roundA", adapter=ScriptedExecutionAdapter())
        # Round 1 -> REVIEW_PASSED, gate_A pending.
        tasks_a = dispatch_reviews_for_artifact(
            session, target_artifact_id=a.id, policy=policy, executor_agent_id=None
        )
        for rt in tasks_a:
            execute_task(session, rt.id, f"idem-a-{rt.id}", adapter=ScriptedReviewAdapter())
            submit_review_from_artifact(session, review_task_id=rt.id, actor=_OWNER)
        gate_a = session.exec(
            select(Approval).where(
                Approval.target_artifact_id == a.id,
                Approval.action_type == "review_gate",
                Approval.status == ApprovalStatus.PENDING,
            )
        ).first()
        assert gate_a is not None
        assert gate_a.review_round == 1
        # Owner approves round 1.
        owner_approve_review(session, artifact_id=a.id, actor=_OWNER)
        session.refresh(gate_a)
        assert gate_a.status.value == "approved"
        assert session.get(Artifact, a.id).review_status == ArtifactReviewStatus.APPROVED

        # Simulate a new revision round on a NEW artifact B (revision_of A).
        b = Artifact(
            project_id=a.project_id, task_id=task.id, type=ArtifactType.JSON,
            uri="exec://rev-B", checksum="b", revision_of=a.id, revision_count=1,
            review_status=ArtifactReviewStatus.UNVERIFIED,
        )
        session.add(b)
        session.commit()
        # Round 2 -> REVIEW_PASSED, gate_B pending (its OWN gate).
        tasks_b = dispatch_reviews_for_artifact(
            session, target_artifact_id=b.id, policy=policy, executor_agent_id=None
        )
        for rt in tasks_b:
            execute_task(session, rt.id, f"idem-b-{rt.id}", adapter=ScriptedReviewAdapter())
            submit_review_from_artifact(session, review_task_id=rt.id, actor=_OWNER)
        gate_b = session.exec(
            select(Approval).where(
                Approval.target_artifact_id == b.id,
                Approval.action_type == "review_gate",
                Approval.status == ApprovalStatus.PENDING,
            )
        ).first()
        assert gate_b is not None
        assert gate_b.review_round == 2
        # Owner approves round 2 via B's gate. gate_a must be untouched (still
        # approved for A, never reused for B).
        owner_approve_review(session, artifact_id=b.id, actor=_OWNER)
        session.refresh(gate_a)
        session.refresh(gate_b)
        assert gate_a.status.value == "approved"  # unchanged
        assert gate_b.status.value == "approved"
        assert session.get(Artifact, b.id).review_status == ArtifactReviewStatus.APPROVED


def test_duplicate_review_dispatch_idempotent(client: TestClient) -> None:
    """req 8 / req 6: dispatching the same target+round twice returns the SAME
    Review Tasks (no duplicate assignments)."""
    _launch(client)
    with _session() as session:
        _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        _agent(session, name="r1", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        _agent(session, name="r2", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        policy = _editorial_policy(session)
        task = _t1(session)
        a = execute_task(session, task.id, "idem-dup", adapter=ScriptedExecutionAdapter())
        first = dispatch_reviews_for_artifact(
            session, target_artifact_id=a.id, policy=policy, executor_agent_id=None
        )
        second = dispatch_reviews_for_artifact(
            session, target_artifact_id=a.id, policy=policy, executor_agent_id=None
        )
        assert {t.id for t in first} == {t.id for t in second}
        # Exactly two ReviewAssignment rows for this target+round.
        count = session.exec(
            select(func.count(ReviewAssignment.review_task_id)).where(
                ReviewAssignment.target_artifact_id == a.id,
                ReviewAssignment.review_round == 1,
            )
        ).first()
        assert count == 2


def test_duplicate_review_submission_idempotent(client: TestClient) -> None:
    """req 8 / req 6: submitting the same completed Review Task twice returns the
    SAME ReviewResult (no duplicate)."""
    _launch(client)
    with _session() as session:
        _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        _agent(session, name="r1", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        _agent(session, name="r2", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        policy = _editorial_policy(session)
        task = _t1(session)
        a = execute_task(session, task.id, "idem-sub", adapter=ScriptedExecutionAdapter())
        tasks = dispatch_reviews_for_artifact(
            session, target_artifact_id=a.id, policy=policy, executor_agent_id=None
        )
        rt = tasks[0]
        execute_task(session, rt.id, f"idem-sub-{rt.id}", adapter=ScriptedReviewAdapter())
        first = submit_review_from_artifact(session, review_task_id=rt.id, actor=_OWNER)
        second = submit_review_from_artifact(session, review_task_id=rt.id, actor=_OWNER)
        assert second.id == first.id
        # Exactly one ReviewResult for this Review Task (unique review_task_id).
        count = session.exec(
            select(func.count(ReviewResult.id)).where(
                ReviewResult.review_task_id == rt.id
            )
        ).first()
        assert count == 1


def test_duplicate_revision_request_idempotent(client: TestClient) -> None:
    """req 8 / req 4: requesting a revision twice prepares a single READY revision
    Task (no duplicate revision task/artifact)."""
    _launch(client)
    with _session() as session:
        _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        _agent(session, name="r1", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        _agent(session, name="r2", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        policy = _editorial_policy(session)
        task = _t1(session)
        a = execute_task(session, task.id, "idem-rev", adapter=ScriptedExecutionAdapter())
        dispatch_reviews_for_artifact(
            session, target_artifact_id=a.id, policy=policy, executor_agent_id=None
        )
        first = request_review_revision(session, task_id=task.id, feedback="v1", actor=_OWNER)
        second = request_review_revision(session, task_id=task.id, feedback="v2", actor=_OWNER)
        # Same prepared revision Task (idempotent).
        assert second.id == first.id
        assert second.status.value == "ready"
        # Exactly one *revision* Task prepared. Review Tasks also depend_on the
        # content task, so we filter to the revision Task by its deterministic
        # title ("Revise ...") to count revision executions specifically.
        rev_tasks = session.exec(
            select(TaskModel).where(
                TaskModel.depends_on == [task.id],
                TaskModel.title.like("Revise %"),
            )
        ).all()
        assert len(rev_tasks) == 1
        assert rev_tasks[0].id == first.id


def test_existing_db_accepts_review_passed_without_migration(client: TestClient) -> None:
    """req 8: REVIEW_PASSED is a VARCHAR enum value already present in the model,
    so an existing database (no new migration data) accepts it. We confirm a
    pre-0006 database can hold a REVIEW_PASSED artifact. The 0006 migration only
    ADDS tables/columns (no enum/data change), so this holds after upgrade too."""
    _launch(client)
    with _session() as session:
        task = _t1(session)
        a = execute_task(session, task.id, "idem-rp", adapter=ScriptedExecutionAdapter())
        a.review_status = ArtifactReviewStatus.REVIEW_PASSED
        session.add(a)
        session.commit()
        session.refresh(a)
        assert a.review_status.value == "review_passed"


def test_audit_does_not_expose_arbitrary_content(client: TestClient) -> None:
    """req 2 / req 7: GET /audit NEVER returns snapshot bodies -- there is no
    opt-in toggle. Sensitive content (Artifact body / api_key) must be absent
    from the entire response; only the fixed safe projection is returned."""
    _launch(client)
    with _session() as session:
        from aios.audit import append_audit

        # Seed an audit row whose snapshots carry sensitive-looking content.
        append_audit(
            session, actor="owner", action="test.secret",
            resource_type="artifact", resource_id="x",
            project_id=None, task_id=None,
            before={"body": "机密草稿全文...", "api_key": "sk-SUPERSECRET1234567890"},
            after={"body": "修订后的机密全文..."},
            idempotency_key="audit:test:secret2",
        )
        session.commit()
    audit = client.get("/audit", params={"action": "test.secret"})
    assert audit.status_code == 200
    items = audit.json()["items"]
    assert len(items) == 1
    item = items[0]
    # No raw snapshot fields are ever returned.
    assert "before_snapshot" not in item
    assert "after_snapshot" not in item
    # Sensitive content is wholly absent from the response payload.
    payload = str(audit.json())
    assert "机密草稿全文" not in payload
    assert "sk-SUPERSECRET1234567890" not in payload
    # The derived safe_delta carries only allowlisted scalar keys -- never body/secret.
    assert "body" not in item["safe_delta"]["before"]
    assert "api_key" not in item["safe_delta"]["before"]
    # The safe allowlisted columns are still present.
    assert item["action"] == "test.secret"
    assert item["actor"] == "owner"


def test_endpoint_dispatch_requires_valid_policy(client: TestClient) -> None:
    """HTTP dispatch rejects an unknown policy id with 404."""
    _launch(client)
    with _session() as session:
        task = _t1(session)
        draft = execute_task(session, task.id, "idem-draft", adapter=ScriptedExecutionAdapter())
    resp = client.post(
        f"/artifacts/{draft.id}/reviews/dispatch",
        json={"policy_id": "nonexistent", "executor_agent_id": None},
    )
    assert resp.status_code == 404


def test_endpoint_dispatch_returns_bound_tasks(client: TestClient) -> None:
    """HTTP dispatch returns server-bound Review Tasks for a valid policy (C2/C3)."""
    _launch(client)
    with _session() as session:
        _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        _agent(session, name="r1", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        _agent(session, name="r2", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        policy = _editorial_policy(session)
        task = _t1(session)
        draft = execute_task(session, task.id, "idem-draft", adapter=ScriptedExecutionAdapter())
        policy_id = policy.id
        draft_id = draft.id
        draft_task_id = draft.task_id
    resp = client.post(
        f"/artifacts/{draft_id}/reviews/dispatch",
        json={"policy_id": policy_id, "executor_agent_id": None},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    # Server-trusted binding: each returned Review Task has a trusted reviewer
    # assigned and a depends_on link to the content task that produced the draft.
    for t in body:
        assert t["assigned_agent_id"] is not None
        assert draft_task_id in (t.get("depends_on") or [])


def test_endpoint_owner_approve_and_audit_query(client: TestClient) -> None:
    """HTTP owner-approve promotes REVIEW_PASSED -> APPROVED, and GET /audit returns
    the owner_approved event (read-only, no write, stable cursor)."""
    _launch(client)
    with _session() as session:
        draft, _ = _wire_review_flow(session, with_approve=False)
        draft_id = draft.id
        draft_task_id = draft.task_id
    resp = client.post(f"/artifacts/{draft_id}/reviews/approve")
    assert resp.status_code == 200
    assert resp.json()["review_status"] == "approved"
    # GET /audit read-only query by task_id.
    audit = client.get("/audit", params={"task_id": draft_task_id})
    assert audit.status_code == 200
    items = audit.json()["items"]
    actions = {i["action"] for i in items}
    assert "review.owner_approved" in actions
    assert "review.gate_passed" in actions
    # Read-only: a second call returns the same number of rows (no mutation).
    audit2 = client.get("/audit", params={"task_id": draft_task_id})
    assert len(audit2.json()["items"]) == len(items)


def test_endpoint_audit_requires_owner_auth(client: TestClient, monkeypatch) -> None:
    """GET /audit requires owner authentication (#74).

    Before #74 the route manufactured an owner via ``resolve_owner_actor`` and its
    403 branch was dead code. Now it depends on ``authenticate_owner``. With the
    test override removed and owner auth configured, a request WITHOUT credentials
    is rejected with 401 and a Basic-auth challenge.
    """
    from aios.api.security import authenticate_owner

    client.app.dependency_overrides.pop(authenticate_owner, None)
    monkeypatch.setenv("AIOS_OWNER_ID", "owner")
    monkeypatch.setenv("AIOS_OWNER_API_KEY", "x" * 32)
    resp = client.get("/audit")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == 'Basic realm="aios-owner"'


def test_endpoint_audit_never_exposes_secrets(client: TestClient) -> None:
    """req 2 / req 7: GET /audit NEVER returns raw secrets. There is no opt-in
    toggle and the raw before/after snapshots are never serialized, so the secret
    value cannot leak through this endpoint -- it is wholly absent from the body.
    """
    _launch(client)
    with _session() as session:
        from aios.audit import append_audit

        append_audit(
            session, actor="owner", action="test.secret",
            resource_type="artifact", resource_id="x",
            project_id=None, task_id=None,
            before={"api_key": "sk-SUPERSECRET1234567890"},
            after={},
            idempotency_key="audit:test:secret",
        )
        session.commit()  # persist so the owner-only GET /audit can read it
    audit = client.get("/audit", params={"action": "test.secret"})
    assert audit.status_code == 200
    items = audit.json()["items"]
    assert len(items) == 1
    # The secret value is absent from the entire response payload.
    assert "sk-SUPERSECRET1234567890" not in str(audit.json())
    # The derived safe_delta carries no secret-shaped key.
    assert "api_key" not in items[0]["safe_delta"]["before"]


# --- req 7: structured idempotency / provenance / DB constraints ------------


def test_revision_idempotency_independent_of_title(client: TestClient) -> None:
    """req 1 / req 7: revision idempotency is keyed by the structured
    ``idempotency_key`` (``review-revision:{source}:{round}``), NOT the Task
    title. Renaming the existing revision Task must not break dedup -- a second
    request returns the SAME Task."""
    _launch(client)
    with _session() as session:
        _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        _agent(session, name="r1", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        _agent(session, name="r2", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        policy = _editorial_policy(session)
        task = _t1(session)
        a = execute_task(session, task.id, "idem-rev", adapter=ScriptedExecutionAdapter())
        dispatch_reviews_for_artifact(session, target_artifact_id=a.id, policy=policy,
                                      executor_agent_id=None)
        first = request_review_revision(session, task_id=task.id, feedback="v1", actor=_OWNER)
        # Simulate a console-side display rename -- must NOT affect identity.
        first.title = "Renamed revision task (round 1)"
        session.add(first)
        session.commit()
        session.refresh(first)
        second = request_review_revision(session, task_id=task.id, feedback="v2", actor=_OWNER)
        assert second.id == first.id
        assert second.title == "Renamed revision task (round 1)"  # unchanged by dedup
        assert second.idempotency_key == f"review-revision:{a.id}:1"


def test_two_revision_requests_prepare_single_task(client: TestClient) -> None:
    """req 1 / req 7: two normal revision requests (even with different feedback,
    which must NOT participate in identity) prepare exactly ONE revision Task."""
    _launch(client)
    with _session() as session:
        _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        _agent(session, name="r1", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        _agent(session, name="r2", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        policy = _editorial_policy(session)
        task = _t1(session)
        a = execute_task(session, task.id, "idem-rev", adapter=ScriptedExecutionAdapter())
        dispatch_reviews_for_artifact(session, target_artifact_id=a.id, policy=policy,
                                      executor_agent_id=None)
        first = request_review_revision(session, task_id=task.id, feedback="first reason",
                                         actor=_OWNER)
        second = request_review_revision(session, task_id=task.id, feedback="second reason",
                                          actor=_OWNER)
        assert second.id == first.id
        # The owner reason is persisted into the revision input (description) but
        # is NOT part of the dedup identity (a second reason does not fork a new task).
        assert "first reason" in first.description
        assert "second reason" not in first.description
        count = session.exec(
            select(func.count(TaskModel.id)).where(
                TaskModel.idempotency_key == first.idempotency_key
            )
        ).first()
        assert count == 1


def test_review_artifact_result_uniquely_traceable(client: TestClient) -> None:
    """req 3: the independent Review Artifact is durably traceable to exactly one
    ReviewResult via ``review_artifact_id`` (Option A) -- never via a mutable
    ``metadata_json`` forward-link."""
    _launch(client)
    with _session() as session:
        draft, tasks = _wire_review_flow(session)
        for rt in tasks:
            review_artifact = session.exec(
                select(Artifact).where(Artifact.task_id == rt.id)
            ).first()
            result = session.exec(
                select(ReviewResult).where(ReviewResult.review_task_id == rt.id)
            ).first()
            assert result is not None
            assert review_artifact is not None
            # Direct, non-null, durable link from the result back to its Review Artifact.
            assert result.review_artifact_id == review_artifact.id
            # The link is unique: exactly one ReviewResult per Review Artifact.
            linked = session.exec(
                select(ReviewResult).where(
                    ReviewResult.review_artifact_id == review_artifact.id
                )
            ).all()
            assert len(linked) == 1
            assert linked[0].id == result.id


def test_review_result_provenance_pair_invariant(client: TestClient) -> None:
    """DB-integrity follow-up (req 3): ``review_task_id`` and ``review_artifact_id``
    are a coupled provenance pair. A partial pair (one set, the other null) is
    rejected at the service layer with 422 -- they must be BOTH non-null (runtime
    binding verdict) or BOTH null (legacy pre-runtime ReviewResult).

    NOTE: a DB-level CHECK cannot be added safely (table-level CHECK would require
    recreating ``review_result`` and break the knowledge_* triggers; column-level
    CHECK cannot express the bidirectional pair). Service-layer enforcement only.
    """
    _launch(client)
    with _session() as session:
        draft = _produce_draft(client, session)
        reviewer = _agent(session, name="rev", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
                          capabilities=["fact_research"])

        # review_task_id set but review_artifact_id missing -> rejected.
        with pytest.raises(ReviewError):
            submit_review(
                session, artifact_id=draft.id,
                reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=reviewer.id,
                dimensions=[], overall=ReviewOverall.APPROVED,
                review_task_id="rt-orphan", review_artifact_id=None,
            )

        # review_artifact_id set but review_task_id missing -> rejected.
        with pytest.raises(ReviewError):
            submit_review(
                session, artifact_id=draft.id,
                reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=reviewer.id,
                dimensions=[], overall=ReviewOverall.APPROVED,
                review_task_id=None, review_artifact_id="ra-orphan",
            )


def test_one_review_gate_approval_per_round(client: TestClient) -> None:
    """req 4 / req 5: at most one PENDING review_gate Approval per
    (target, policy, round). A direct duplicate insert is rejected by the DB
    unique index (uq_approval_gate_round)."""
    _launch(client)
    with _session() as session:
        draft, _ = _wire_review_flow(session)
        gates = session.exec(
            select(Approval).where(
                Approval.target_artifact_id == draft.id,
                Approval.action_type == "review_gate",
                Approval.status == ApprovalStatus.PENDING,
            )
        ).all()
        assert len(gates) == 1
        # A second gate for the identical (target, policy, round) must be rejected
        # by the database unique constraint -- not merely by a service-layer check.
        from sqlalchemy.exc import IntegrityError

        dup = Approval(
            project_id=draft.project_id,
            task_id=draft.task_id,
            action_type="review_gate",
            risk_level=RiskLevel.L2,
            status=ApprovalStatus.PENDING,
            target_artifact_id=draft.id,
            review_policy_id=gates[0].review_policy_id,
            review_round=gates[0].review_round,
        )
        session.add(dup)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_old_gate_invalidated_after_owner_revision(client: TestClient) -> None:
    """req 5: owner requests a revision after the gate opened -> the pending gate
    is invalidated (REJECTED) and the source becomes NEEDS_REVISION; the old gate
    can NO LONGER approve the (new) round."""
    _launch(client)
    with _session() as session:
        draft, _ = _wire_review_flow(session)  # REVIEW_PASSED; gate PENDING
        gate = session.exec(
            select(Approval).where(
                Approval.target_artifact_id == draft.id,
                Approval.action_type == "review_gate",
                Approval.status == ApprovalStatus.PENDING,
            )
        ).first()
        assert gate is not None
        rev_task = request_review_revision(session, task_id=draft.task_id,
                                            feedback="请修订", actor=_OWNER)
        assert rev_task.status.value == "ready"
        # Source is now a terminal NEEDS_REVISION (not a meaningless UNVERIFIED).
        assert session.get(Artifact, draft.id).review_status == (
            ArtifactReviewStatus.NEEDS_REVISION
        )
        # The OLD gate was invalidated (cannot approve the new round).
        session.refresh(gate)
        assert gate.status.value == "rejected"
        # Re-using the (now rejected) gate to approve the same round is refused and
        # the artifact stays NEEDS_REVISION.
        with pytest.raises(ServiceError):
            owner_approve_review(session, artifact_id=draft.id, actor=_OWNER)
        assert session.get(Artifact, draft.id).review_status == (
            ArtifactReviewStatus.NEEDS_REVISION
        )


def test_audit_allowlist_fields_only(client: TestClient) -> None:
    """req 2 / req 7: GET /audit returns ONLY the fixed safe projection. Sensitive
    snapshot bodies (Artifact body / owner reason text) are never serialized; only
    allowlisted scalar keys surface in ``safe_delta``."""
    _launch(client)
    with _session() as session:
        from aios.audit import append_audit

        append_audit(
            session, actor="owner", action="task.revision_requested",
            resource_type="task", resource_id="t1",
            project_id=None, task_id="t1",
            before={"status": "in_progress"},
            after={"status": "ready", "feedback": "这是机密修订理由全文...",
                   "body": "完整草稿内容..."},
            idempotency_key="audit:test:allowlist",
        )
        session.commit()
    audit = client.get("/audit", params={"action": "task.revision_requested"})
    assert audit.status_code == 200
    item = audit.json()["items"][0]
    # Allowed columns present.
    assert item["action"] == "task.revision_requested"
    assert item["resource_id"] == "t1"
    # Forbidden content absent from the entire payload.
    assert "完整草稿内容" not in str(audit.json())
    assert "这是机密修订理由全文" not in str(audit.json())
    # safe_delta lifts only allowlisted scalar keys (status), never body/feedback.
    assert item["safe_delta"]["after"].get("status") == "ready"
    assert "body" not in item["safe_delta"]["after"]
    assert "feedback" not in item["safe_delta"]["after"]


def test_dispatch_idempotent_returns_existing(client: TestClient) -> None:
    """req 4: re-dispatching the same target+round returns the existing Review
    Tasks (no duplicate binding). One ReviewAssignment per (target, round)."""
    _launch(client)
    with _session() as session:
        _agent(session, name="producer", trust=AgentTrustLevel.INTERNAL)
        _agent(session, name="r1", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        _agent(session, name="r2", trust=AgentTrustLevel.VERIFIED_EXTERNAL,
               capabilities=["fact_research"])
        policy = _editorial_policy(session)
        task = _t1(session)
        a = execute_task(session, task.id, "idem-dispatch", adapter=ScriptedExecutionAdapter())
        first = dispatch_reviews_for_artifact(session, target_artifact_id=a.id,
                                              policy=policy, executor_agent_id=None)
        second = dispatch_reviews_for_artifact(session, target_artifact_id=a.id,
                                               policy=policy, executor_agent_id=None)
        assert [t.id for t in second] == [t.id for t in first]
        assignments = session.exec(
            select(func.count(ReviewAssignment.review_task_id)).where(
                ReviewAssignment.target_artifact_id == a.id,
                ReviewAssignment.review_round == 1,
            )
        ).first()
        assert assignments == len(first)


