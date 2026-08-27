"""Migration round-trip + DB-level unique constraints for review binding (0006, #69).

Verifies (req 6 / req 4):
  * 0005 -> 0006 -> 0005 -> 0006 round-trip; Alembic single head preserved.
  * The knowledge_* triggers survive the 0006 migration (and its downgrade).
  * The new unique indexes exist after upgrade and are dropped after downgrade.
  * The new unique constraints physically reject concurrent duplicate data
    (task.idempotency_key, review_result.review_artifact_id,
     review_assignment 5-tuple, approval gate 4-tuple).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select, text

from aios.db import get_engine
from aios.models import (
    Approval,
    ApprovalStatus,
    ReviewAssignment,
    ReviewOverall,
    ReviewPolicy,
    ReviewResult,
    ReviewReviewerType,
    RiskLevel,
    Task,
    TaskStatus,
)
from alembic import command

HEAD = "20260827_0001_workforce_capreq_hardening"
BASE = "20260720_0005"
# Lowest revision these ORM-seeding tests upgrade to. The migrations above it
# form a chain of one-way doors:
#   * 20260810_0001 (SalesPlaybook V0)            -> downgrade() raises unconditionally
#   * 20260812_0001 (cs_suggestion evidence flag) -> downgrade() raises unconditionally
#   * 20260820_0001 (series_id)                   -> downgrade() DROPs the column (data-losing)
#   * 20260824_0001 (series_id_json_guard, former head)  -> downgrade() is a deliberate no-op pass
# The two ``raise``-on-downgrade revisions (20260810, 20260812) are the genuine
# one-way doors; the head is NOT (its downgrade is a no-op). This floor already
# carries every column the ORM models below depend on.
LAST_DOWNGRADABLE = "20260731_0001"


def _config(url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _tables(session: Session) -> set[str]:
    rows = session.exec(text("SELECT name FROM sqlite_master WHERE type='table'")).all()
    return {r[0] for r in rows}


def _triggers(session: Session) -> set[str]:
    rows = session.exec(text("SELECT name FROM sqlite_master WHERE type='trigger'")).all()
    return {row[0] for row in rows}


def _indexes(session: Session) -> set[str]:
    rows = session.exec(
        text("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")
    ).all()
    return {row[0] for row in rows}


def _columns(session: Session, table: str) -> set[str]:
    rows = session.exec(text(f"PRAGMA table_info({table})")).all()
    return {row[1] for row in rows}


def test_alembic_single_head() -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = Config(root / "alembic.ini")
    cfg.set_main_option("script_location", str(root / "alembic"))
    assert ScriptDirectory.from_config(cfg).get_current_head() == HEAD


def test_migration_round_trip_0005_0006_0005_0006(tmp_path) -> None:
    url = f"sqlite:///{(tmp_path / 'rev_mig.db').as_posix()}"
    cfg = _config(url)
    engine = get_engine(url)

    # Start at the pre-binding head (0005).
    command.upgrade(cfg, BASE)
    with Session(engine) as session:
        triggers = _triggers(session)
        assert "knowledge_candidate_validate_insert" in triggers
        assert "knowledge_review_validate_insert" in triggers
        assert "knowledge_fact_validate_update" in triggers
        assert "review_assignment" not in _tables(session)

    # Upgrade to 0006: review binding + unique constraints appear.
    command.upgrade(cfg, LAST_DOWNGRADABLE)
    with Session(engine) as session:
        assert "review_assignment" in _tables(session)
        idx = _indexes(session)
        assert "uq_review_assignment_binding" in idx
        assert "uq_review_result_review_artifact_id" in idx
        assert "uq_approval_gate_round" in idx
        assert "ix_task_idempotency_key" in idx
        assert "uq_review_policy_name" in idx  # 0007 identity constraint
        assert "review_artifact_id" in _columns(session, "review_result")
        assert "idempotency_key" in _columns(session, "task")
        # knowledge triggers still present (0006 must not break them).
        triggers = _triggers(session)
        assert "knowledge_candidate_validate_insert" in triggers
        assert "knowledge_review_validate_insert" in triggers

    # Downgrade back to 0005: everything 0006/0007 added is gone.
    command.downgrade(cfg, BASE)
    with Session(engine) as session:
        assert "review_assignment" not in _tables(session)
        idx = _indexes(session)
        assert "uq_review_assignment_binding" not in idx
        assert "uq_review_result_review_artifact_id" not in idx
        assert "uq_approval_gate_round" not in idx
        assert "ix_task_idempotency_key" not in idx
        assert "uq_review_policy_name" not in idx
        assert "review_artifact_id" not in _columns(session, "review_result")
        assert "idempotency_key" not in _columns(session, "task")
        triggers = _triggers(session)
        assert "knowledge_candidate_validate_insert" in triggers

    # Re-upgrade to 0006: round-trip complete.
    command.upgrade(cfg, HEAD)
    with Session(engine) as session:
        idx = _indexes(session)
        assert "uq_review_assignment_binding" in idx
        assert "uq_review_result_review_artifact_id" in idx
        assert "uq_approval_gate_round" in idx
        assert "review_artifact_id" in _columns(session, "review_result")


def test_db_unique_constraints_reject_duplicates(tmp_path) -> None:
    """req 4: the new unique constraints physically reject concurrent duplicate
    inserts at the DB level (each second insert below is a concurrent write)."""
    url = f"sqlite:///{(tmp_path / 'rev_uniq.db').as_posix()}"
    cfg = _config(url)
    command.upgrade(cfg, HEAD)
    engine = get_engine(url)
    # Disable FK enforcement so we exercise ONLY the unique-index violation
    # (the referenced parent rows are not materialized in this focused test).
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=OFF"))

    with Session(engine) as session:
        # 1) task.idempotency_key unique.
        session.add(Task(id="t1", project_id="p1", title="A", description="d",
                         status=TaskStatus.BACKLOG, idempotency_key="idem-1"))
        session.commit()
        session.add(Task(id="t2", project_id="p1", title="B", description="d",
                         status=TaskStatus.BACKLOG, idempotency_key="idem-1"))
        try:
            session.commit()
            raise AssertionError("duplicate task.idempotency_key was NOT rejected")
        except IntegrityError:
            session.rollback()

        # 2) review_result.review_artifact_id unique (Option A provenance).
        session.add(ReviewResult(id="r1", artifact_id="a1",
                                 reviewer_type=ReviewReviewerType.AGENT,
                                 overall=ReviewOverall.APPROVED,
                                 review_artifact_id="ra1", review_round=1))
        session.commit()
        session.add(ReviewResult(id="r2", artifact_id="a2",
                                 reviewer_type=ReviewReviewerType.AGENT,
                                 overall=ReviewOverall.APPROVED,
                                 review_artifact_id="ra1", review_round=1))
        try:
            session.commit()
            raise AssertionError("duplicate review_result.review_artifact_id was NOT rejected")
        except IntegrityError:
            session.rollback()

        # 3) review_assignment 5-tuple unique.
        session.add(ReviewAssignment(review_task_id="rt1", target_artifact_id="ta1",
                                     review_policy_id="rp1", review_round=1,
                                     reviewer_agent_id="ag1", review_dimension="fact"))
        session.commit()
        session.add(ReviewAssignment(review_task_id="rt2", target_artifact_id="ta1",
                                     review_policy_id="rp1", review_round=1,
                                     reviewer_agent_id="ag1", review_dimension="fact"))
        try:
            session.commit()
            raise AssertionError("duplicate review_assignment binding was NOT rejected")
        except IntegrityError:
            session.rollback()

        # 4) approval gate 4-tuple unique.
        session.add(Approval(id="ap1", project_id="p1", action_type="review_gate",
                             risk_level=RiskLevel.L2, status=ApprovalStatus.PENDING,
                             target_artifact_id="ta1", review_policy_id="rp1", review_round=1))
        session.commit()
        session.add(Approval(id="ap2", project_id="p1", action_type="review_gate",
                             risk_level=RiskLevel.L2, status=ApprovalStatus.PENDING,
                             target_artifact_id="ta1", review_policy_id="rp1", review_round=1))
        try:
            session.commit()
            raise AssertionError("duplicate review_gate Approval was NOT rejected")
        except IntegrityError:
            session.rollback()


def test_review_policy_name_unique_rejects_duplicates(tmp_path: Path) -> None:
    """D3 hardening (#72/#74): the uq_review_policy_name index physically rejects
    a concurrent duplicate-name insert at the DB level."""
    url = f"sqlite:///{(tmp_path / 'rp_uniq.db').as_posix()}"
    cfg = _config(url)
    command.upgrade(cfg, "heads")
    engine = get_engine(url)
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=OFF"))

    with Session(engine) as session:
        session.add(ReviewPolicy(name="editorial-v1"))
        session.commit()
        session.add(ReviewPolicy(name="editorial-v1"))
        try:
            session.commit()
            raise AssertionError("duplicate review_policy.name was NOT rejected")
        except IntegrityError:
            session.rollback()


def test_review_policy_identity_migration_fail_closed_on_duplicate_names(
    tmp_path: Path,
) -> None:
    """The 0007 migration refuses to add the UNIQUE index when duplicate
    review_policy.name rows already exist (fail-closed preflight)."""
    url = f"sqlite:///{(tmp_path / 'rp_preflight.db').as_posix()}"
    cfg = _config(url)
    # Land at 0006 (pre-identity), then seed duplicate names.
    command.upgrade(cfg, "20260720_0006")
    engine = get_engine(url)
    with Session(engine) as session:
        session.add(ReviewPolicy(name="dup-a"))
        session.add(ReviewPolicy(name="dup-a"))
        session.commit()

    # Upgrading into 0007 must abort rather than silently pick a winner.
    with pytest.raises(RuntimeError):
        command.upgrade(cfg, "20260722_0007")


# --- 0007 canonical-consistency preflight (architecture review fix) -----------


def test_migration_0007_rejects_canonical_duplicate_names(tmp_path: Path) -> None:
    """Two distinct raw names that collapse to the SAME trimmed identity
    ('editorial-v1' and ' editorial-v1 ') must abort the migration fail-closed.
    After the failure the DB must still be at 0006, the uq_review_policy_name
    index must NOT exist, and the original rows must be unchanged."""
    url = f"sqlite:///{(tmp_path / 'rp_canon_dup.db').as_posix()}"
    cfg = _config(url)
    command.upgrade(cfg, "20260720_0006")
    engine = get_engine(url)
    with Session(engine) as session:
        session.add(ReviewPolicy(name="editorial-v1"))
        session.add(ReviewPolicy(name=" editorial-v1 "))  # raw differs, canonical == editorial-v1
        session.commit()

    with pytest.raises(RuntimeError):
        command.upgrade(cfg, "20260722_0007")

    # DB still at 0006; index never created; original data intact.
    with Session(engine) as session:
        assert (
            session.exec(text("SELECT version_num FROM alembic_version")).first()[0]
            == "20260720_0006"
        )
        assert "uq_review_policy_name" not in _indexes(session)
        # NOTE: read back via the full entity, not `select(ReviewPolicy.name)`
        # (a SQLModel/SQLAlchemy column-only projection returns a corrupted value
        # for this model); the raw stored value is what we assert is unchanged.
        names = sorted(r.name for r in session.exec(select(ReviewPolicy)).all())
        assert names == [" editorial-v1 ", "editorial-v1"]


def test_migration_0007_rejects_single_non_canonical_name(tmp_path: Path) -> None:
    """A single non-canonical (whitespace) name must abort the migration; the
    migration must NOT silently trim/fix it."""
    url = f"sqlite:///{(tmp_path / 'rp_noncanon.db').as_posix()}"
    cfg = _config(url)
    command.upgrade(cfg, "20260720_0006")
    engine = get_engine(url)
    with Session(engine) as session:
        session.add(ReviewPolicy(name=" editorial-v1 "))
        session.commit()

    with pytest.raises(RuntimeError):
        command.upgrade(cfg, "20260722_0007")

    with Session(engine) as session:
        assert (
            session.exec(text("SELECT version_num FROM alembic_version")).first()[0]
            == "20260720_0006"
        )
        assert "uq_review_policy_name" not in _indexes(session)
        names = [r.name for r in session.exec(select(ReviewPolicy)).all()]
        # Original (non-canonical) value is untouched by the migration.
        assert names == [" editorial-v1 "]


def test_migration_0007_rejects_whitespace_only_name(tmp_path: Path) -> None:
    """A whitespace-only name (trim(name) = '') must abort the migration."""
    url = f"sqlite:///{(tmp_path / 'rp_ws.db').as_posix()}"
    cfg = _config(url)
    command.upgrade(cfg, "20260720_0006")
    engine = get_engine(url)
    with Session(engine) as session:
        session.add(ReviewPolicy(name="   "))
        session.commit()

    with pytest.raises(RuntimeError):
        command.upgrade(cfg, "20260722_0007")

    with Session(engine) as session:
        assert (
            session.exec(text("SELECT version_num FROM alembic_version")).first()[0]
            == "20260720_0006"
        )
        assert "uq_review_policy_name" not in _indexes(session)


def test_migration_0007_succeeds_when_all_names_canonical(tmp_path: Path) -> None:
    """When every stored name is already canonical, the migration adds the
    UNIQUE index and the DB advances to 0007."""
    url = f"sqlite:///{(tmp_path / 'rp_ok.db').as_posix()}"
    cfg = _config(url)
    command.upgrade(cfg, "20260720_0006")
    engine = get_engine(url)
    with Session(engine) as session:
        session.add(ReviewPolicy(name="editorial-v1"))
        session.add(ReviewPolicy(name="risk-v2"))
        session.commit()

    command.upgrade(cfg, "20260722_0007")  # must not raise

    with Session(engine) as session:
        assert (
            session.exec(text("SELECT version_num FROM alembic_version")).first()[0]
            == "20260722_0007"
        )
        assert "uq_review_policy_name" in _indexes(session)
