"""Migration round-trip + backfill for 20260720_0005_knowledge_tags (Phase A, #67).

The head on this branch is ``20260719_0003`` (the review-protocol migration 0004
has not landed yet), so the round-trip is 0003 -> 0005 -> 0003. The test pins that
chain and the JSON-safe sentinel backfill.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from sqlmodel import Session, text

from aios.db import get_engine
from alembic import command

HEAD = "20260720_0005"
BASE = "20260719_0003"
SENTINEL = "__legacy_unclassified__"


def _config(url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _triggers(session: Session) -> set[str]:
    rows = session.exec(
        text("SELECT name FROM sqlite_master WHERE type='trigger'")
    ).all()
    return {row[0] for row in rows}


def _columns(session: Session, table: str) -> set[str]:
    rows = session.exec(text(f"PRAGMA table_info({table})")).all()
    return {row[1] for row in rows}


def test_migration_round_trip_0003_0005_0003(tmp_path) -> None:
    url = f"sqlite:///{(tmp_path / 'kp_mig.db').as_posix()}"
    cfg = _config(url)
    engine = get_engine(url)

    # Start at the pre-slice head (0003).
    command.upgrade(cfg, BASE)
    with Session(engine) as session:
        assert "tags" not in _columns(session, "knowledge_fact")
        assert "submitted_by_kind" not in _columns(session, "knowledge_candidate")
        assert "reviewer_kind" not in _columns(session, "knowledge_review_decision")

    # Upgrade to the slice head (0005): columns + augmented triggers appear.
    command.upgrade(cfg, HEAD)
    with Session(engine) as session:
        cols = _columns(session, "knowledge_fact")
        assert "tags" in cols
        cand_cols = _columns(session, "knowledge_candidate")
        assert {
            "tags",
            "submitted_by_kind",
            "submitted_by_owner_id",
            "submitted_by_agent_id",
        } <= cand_cols
        rev_cols = _columns(session, "knowledge_review_decision")
        assert {"reviewer_kind", "reviewer_owner_id", "reviewer_agent_id"} <= rev_cols
        triggers = _triggers(session)
        assert "knowledge_candidate_validate_insert" in triggers
        assert "knowledge_review_validate_insert" in triggers
        assert "knowledge_fact_validate_update" in triggers

    # Downgrade back to 0003: everything this migration added is gone, and the
    # pre-slice trigger bodies are restored.
    command.downgrade(cfg, BASE)
    with Session(engine) as session:
        assert "tags" not in _columns(session, "knowledge_fact")
        assert "submitted_by_kind" not in _columns(session, "knowledge_candidate")
        assert "knowledge_review_validate_insert" not in _triggers(session)

    # Re-upgrade to 0005: idempotent re-application works (round-trip complete).
    command.upgrade(cfg, HEAD)
    with Session(engine) as session:
        assert "tags" in _columns(session, "knowledge_fact")
        assert "knowledge_review_validate_insert" in _triggers(session)


def test_backfill_sentinel_is_json_safe(tmp_path) -> None:
    """The one-time backfill promotes legacy empty-tagged rows to the sentinel.

    Uses the exact JSON-safe predicate (json_array_length + json_extract) so an
    empty tag list or a coincidental substring can never match.
    """
    url = f"sqlite:///{(tmp_path / 'kp_backfill.db').as_posix()}"
    cfg = _config(url)
    command.upgrade(cfg, HEAD)
    engine = get_engine(url)

    with Session(engine) as session:
        # Drop the provenance/identity triggers so we can seed a legacy fact
        # directly (mirrors a row that predates the tags column).
        for name in (
            "knowledge_fact_validate_insert",
            "knowledge_fact_validate_update",
            "knowledge_candidate_validate_insert",
            "knowledge_candidate_validate_update",
            "knowledge_review_reject_update",
        ):
            session.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
        session.execute(text("PRAGMA foreign_keys=OFF"))
        # Legacy APPROVED fact with an EMPTY tag array.
        session.execute(
            text(
                "INSERT INTO knowledge_fact "
                "(id, series_id, version, project_id, source_project_id, statement, "
                "source_candidate_id, source_artifact_id, review_decision_id, "
                "supersedes_fact_id, status, tags, created_at, updated_at) "
                "VALUES ('f1','s',1,NULL,NULL,'legacy',"
                "'c1','a1','r1',NULL,'APPROVED','[]','2026-01-01','2026-01-01')"
            )
        )
        # A fact whose tag *contains* the sentinel substring but is NOT the exact
        # sentinel array must NOT be mistaken for a sentinel (proves no LIKE).
        session.execute(
            text(
                "INSERT INTO knowledge_fact "
                "(id, series_id, version, project_id, source_project_id, statement, "
                "source_candidate_id, source_artifact_id, review_decision_id, "
                "supersedes_fact_id, status, tags, created_at, updated_at) "
                "VALUES ('f2','s2',1,NULL,NULL,'real fact',"
                "'c2','a2','r2',NULL,'APPROVED','[\"not_really_sentinel\"]','2026-01-01','2026-01-01')"
            )
        )
        session.commit()

        # Run the exact backfill predicate from the migration.
        session.execute(
            text(
                f"UPDATE knowledge_fact SET tags='[\"{SENTINEL}\"]' "
                f"WHERE status='APPROVED' AND json_array_length(tags)=0"
            )
        )
        session.commit()
        rows = session.exec(
            text("SELECT id, tags FROM knowledge_fact ORDER BY id")
        ).all()
        tags_by_id = {r[0]: r[1] for r in rows}
        # Only the genuinely empty-tagged fact is promoted; the substring-containing
        # tag is left untouched (JSON-safe, never LIKE).
        assert tags_by_id["f1"] == f'["{SENTINEL}"]'
        assert tags_by_id["f2"] == '["not_really_sentinel"]'
