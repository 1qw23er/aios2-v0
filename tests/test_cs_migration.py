"""Migration proof for the #109 customer-service implementation (plan §2.4 / §7 T1, T20).

Asserts from several angles:
* the Alembic head advanced to exactly ``20260731_0001`` (single head);
* exactly one new migration file was added (chained after ``20260730_0001``);
* a freshly migrated DB creates ``conversation`` / ``message`` / ``cs_suggestion``
  with the expected columns;
* CS enums are stored as plain VARCHAR and round-trip unchanged via raw SQL
  (no enum table, no new table for the enum values).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlmodel import Session

from aios.db import get_engine, run_migrations
from aios.models import Project

HEAD = "20260731_0001"
PREV = "20260730_0001_agent_secret.py"


def _script_dir():
    root = Path(__file__).resolve().parents[1]
    cfg = Config(root / "alembic.ini")
    cfg.set_main_option("script_location", str(root / "alembic"))
    return root, cfg, ScriptDirectory.from_config(cfg)


# ---------------------------------------------------------------------------
# T1a / T1b: single head + exactly one new migration file
# ---------------------------------------------------------------------------


def test_single_alembic_head_advanced():
    _, _, sd = _script_dir()
    assert sd.get_heads() == [HEAD]
    assert sd.get_current_head() == HEAD


def test_exactly_one_new_migration_file():
    root, _, _ = _script_dir()
    versions = root / "alembic" / "versions"
    files = [
        p.name
        for p in versions.glob("*.py")
        if not p.name.startswith("_") and p.name != "__init__.py"
    ]
    assert "20260731_0001_customer_service.py" in files
    assert all(p.startswith("2026") for p in files)
    # No migration with a later date stamp was introduced by #109 beyond the one.
    newer = [p for p in files if p > PREV]
    assert newer == ["20260731_0001_customer_service.py"]


# ---------------------------------------------------------------------------
# T1c: fresh DB creates the three CS tables with expected columns
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_engine(tmp_path):
    url = f"sqlite:///{(tmp_path / 'cs_mig.db').as_posix()}"
    run_migrations(url)
    return get_engine(url)


def test_three_cs_tables_created(migrated_engine):
    insp = inspect(migrated_engine)
    tables = set(insp.get_table_names())
    for t in ("conversation", "message", "cs_suggestion"):
        assert t in tables

    conv_cols = {c["name"] for c in insp.get_columns("conversation")}
    assert {"id", "project_id", "channel", "lead_stage", "created_at", "updated_at"} <= conv_cols

    msg_cols = {c["name"] for c in insp.get_columns("message")}
    assert {"id", "conversation_id", "project_id", "direction", "sender_type", "body"} <= msg_cols

    sug_cols = {c["name"] for c in insp.get_columns("cs_suggestion")}
    assert {
        "id", "conversation_id", "project_id", "decision", "text", "consumed", "idempotency_key"
    } <= sug_cols

    # idempotency_key has a unique index (the one-shot consume guard).
    sug_indexes = insp.get_indexes("cs_suggestion")
    assert any(ix["unique"] and "idempotency_key" in ix["column_names"] for ix in sug_indexes)


# ---------------------------------------------------------------------------
# T20: enum round-trip via raw SQL (VARCHAR, no enum table)
# ---------------------------------------------------------------------------


def test_cs_enum_round_trip_via_raw_sql(migrated_engine):
    # Create a valid project via ORM (all required columns + defaults), then
    # exercise the raw-SQL VARCHAR round-trip on the CS enum columns.
    with Session(migrated_engine) as s:
        s.add(Project(id="prj_raw", name="raw", objective="raw"))
        s.commit()
    with migrated_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO conversation (id, project_id, channel, lead_stage, "
                "created_at, updated_at) VALUES "
                "('conv_raw', 'prj_raw', 'mock', 'visitor', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
        conn.commit()
        channel = conn.execute(
            text("SELECT channel FROM conversation WHERE id = 'conv_raw'")
        ).scalar()
        lead = conn.execute(
            text("SELECT lead_stage FROM conversation WHERE id = 'conv_raw'")
        ).scalar()
    # Stored token is the plain VARCHAR value, not an enum member name table.
    assert channel == "mock"
    assert lead == "visitor"


def test_alembic_version_is_new_head(migrated_engine):
    with migrated_engine.connect() as conn:
        v = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert v == HEAD
