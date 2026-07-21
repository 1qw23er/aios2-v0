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

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, text

from aios.db import get_engine
from aios.models import (
    Approval,
    ApprovalStatus,
    ReviewAssignment,
    ReviewOverall,
    ReviewResult,
    ReviewReviewerType,
    RiskLevel,
    Task,
    TaskStatus,
)
from alembic import command

HEAD = "20260720_0006"
BASE = "20260720_0005"


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
    command.upgrade(cfg, HEAD)
    with Session(engine) as session:
        assert "review_assignment" in _tables(session)
        idx = _indexes(session)
        assert "uq_review_assignment_binding" in idx
        assert "uq_review_result_review_artifact_id" in idx
        assert "uq_approval_gate_round" in idx
        assert "ix_task_idempotency_key" in idx
        assert "review_artifact_id" in _columns(session, "review_result")
        assert "idempotency_key" in _columns(session, "task")
        # knowledge triggers still present (0006 must not break them).
        triggers = _triggers(session)
        assert "knowledge_candidate_validate_insert" in triggers
        assert "knowledge_review_validate_insert" in triggers

    # Downgrade back to 0005: everything 0006 added is gone.
    command.downgrade(cfg, BASE)
    with Session(engine) as session:
        assert "review_assignment" not in _tables(session)
        idx = _indexes(session)
        assert "uq_review_assignment_binding" not in idx
        assert "uq_review_result_review_artifact_id" not in idx
        assert "uq_approval_gate_round" not in idx
        assert "ix_task_idempotency_key" not in idx
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
