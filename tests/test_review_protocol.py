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

from aios.agent_registry import register_agent
from aios.api.app import create_app
from aios.campaign import V1_TASKS
from aios.db import get_database_url, get_engine
from aios.execution import ExecutionResult, execute_task
from aios.models import (
    AdapterType,
    Agent,
    AgentTrustLevel,
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    DelegationMode,
    Project,
    ReviewDimension,
    ReviewedFact,
    ReviewOverall,
    ReviewPolicy,
    ReviewResult,
    ReviewReviewerType,
)
from aios.models import (
    Task as TaskModel,
)
from aios.review import (
    ReviewError,
    assert_revision_lineage,
    human_review_present,
    submit_review,
    trigger_revision,
)
from alembic import command


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
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'review.db'}")
    monkeypatch.delenv("AIOS_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("AIOS_AGENT_BASE_URL", raising=False)
    with TestClient(create_app(), follow_redirects=False) as test_client:
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
    policy = ReviewPolicy(
        name="editorial",
        applies_to="editorial",
        dimensions=[
            ReviewDimension.FACT_CORRECTNESS.value,
            ReviewDimension.ACCEPTANCE_CRITERIA.value,
            ReviewDimension.BRAND_STRATEGY.value,
            ReviewDimension.RISK.value,
        ],
        required_reviewer_trust=AgentTrustLevel.VERIFIED_EXTERNAL,
        required_capabilities=["fact_research"],
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
    APPROVED -- proving required reviews cannot be skipped.
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

        # Second distinct PASS reviewer -> aggregated APPROVED.
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
        # Required reviewers satisfied -> aggregated APPROVED.
        assert session.get(Artifact, draft.id).review_status == (
            ArtifactReviewStatus.APPROVED
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
    with Session(get_engine(url)) as s:
        proj = Project(name="rt", objective="rt")
        s.add(proj)
        s.flush()
        art = Artifact(project_id=proj.id, type=ArtifactType.JSON, uri="u", checksum="c")
        s.add(art)
        s.flush()
        proj_id = proj.id
        seeded_id = art.id
        s.commit()

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
    with Session(get_engine(url)) as s:
        parent = Artifact(project_id=proj_id, type=ArtifactType.JSON, uri="up", checksum="cp")
        child = Artifact(
            project_id=proj_id, type=ArtifactType.JSON, uri="uc", checksum="cc",
            revision_of=parent.id,
        )
        s.add_all([parent, child])
        s.flush()
        parent_id, child_id = parent.id, child.id
        s.commit()
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
    with Session(get_engine(url)) as s:
        assert s.get(Artifact, seeded_id) is not None
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

    # 3) The new head schema is immediately writable/readable via the ORM.
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
    second distinct PASS -> APPROVED. Required reviews cannot be skipped."""
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

        # A distinct second PASS reviewer satisfies the required count -> APPROVED.
        submit_review(session, artifact_id=draft.id,
                        reviewer_type=ReviewReviewerType.AGENT, reviewer_agent_id=reviewer2.id,
                        dimensions=[], overall=ReviewOverall.APPROVED, policy=policy,
                        executor_agent_id=producer.id)
        assert session.get(Artifact, draft.id).review_status == (
            ArtifactReviewStatus.APPROVED
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


