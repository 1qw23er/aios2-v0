"""Precheck guard for malformed metadata before the series_id backfill (follow-up #1).

Verifies the standalone ``scripts/precheck_series_id_metadata.py`` logic:
  * a row with non-null, non-JSON ``metadata`` is flagged;
  * NULL / valid-JSON ``metadata`` is NOT flagged;
  * tables without a ``metadata`` column (``cs_suggestion``,
    ``knowledge_candidate``) are skipped without error;
  * the precheck runs cleanly against the real migrated schema and FAILS (exit 1)
    when a malformed-``metadata`` artifact row is present.
"""
from __future__ import annotations

import sys
from pathlib import Path

from alembic.config import Config
from sqlalchemy import text

from aios.db import get_engine
from alembic import command

# The precheck script lives in ``scripts/``; make it importable for this test.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from precheck_series_id_metadata import (  # noqa: E402
    check_metadata_json,
    run_precheck,
)

HEAD = "20260904_0001_workforce_cost_evidence"


def _config(url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_precheck_detects_malformed_metadata_on_minimal_table(tmp_path) -> None:
    url = f"sqlite:///{(tmp_path / 'pc_min.db').as_posix()}"
    engine = get_engine(url)
    with engine.connect() as conn:
        conn.execute(
            text("CREATE TABLE artifact (id TEXT PRIMARY KEY, metadata TEXT)")
        )
        # malformed: non-null, non-JSON
        conn.execute(
            text("INSERT INTO artifact (id, metadata) VALUES (:id, :md)"),
            {"id": "bad", "md": "not json at all"},
        )
        # valid JSON
        conn.execute(
            text("INSERT INTO artifact (id, metadata) VALUES (:id, :md)"),
            {"id": "ok", "md": '{"a":1}'},
        )
        # NULL sentinel -> valid, must be ignored
        conn.execute(
            text("INSERT INTO artifact (id, metadata) VALUES (:id, :md)"),
            {"id": "null", "md": None},
        )
        conn.commit()

    violations = check_metadata_json(url)
    assert len(violations) == 1, violations
    assert "artifact" in violations[0]
    assert "1 row" in violations[0]


def test_precheck_skips_tables_without_metadata_column(tmp_path) -> None:
    url = f"sqlite:///{(tmp_path / 'pc_skip.db').as_posix()}"
    engine = get_engine(url)
    with engine.connect() as conn:
        # Only artifact exists; cs_suggestion / knowledge_candidate do NOT, so
        # they have no metadata column and must be skipped (not error, not flag).
        conn.execute(
            text("CREATE TABLE artifact (id TEXT PRIMARY KEY, metadata TEXT)")
        )
        conn.execute(
            text("INSERT INTO artifact (id, metadata) VALUES (:id, :md)"),
            {"id": "x", "md": "garbage"},
        )
        conn.commit()

    violations = check_metadata_json(url)
    assert any("artifact" in v for v in violations), violations
    assert all(
        "cs_suggestion" not in v and "knowledge_candidate" not in v
        for v in violations
    ), violations


def test_precheck_clean_on_real_schema(tmp_path) -> None:
    url = f"sqlite:///{(tmp_path / 'pc_real_clean.db').as_posix()}"
    cfg = _config(url)
    command.upgrade(cfg, HEAD)
    # Freshly migrated DB has no rows -> clean.
    assert check_metadata_json(url) == []
    # run_precheck returns 0 (OK) on a clean DB.
    assert run_precheck(url) == 0


def test_precheck_flags_malformed_artifact_on_real_schema(tmp_path) -> None:
    url = f"sqlite:///{(tmp_path / 'pc_real_bad.db').as_posix()}"
    cfg = _config(url)
    command.upgrade(cfg, HEAD)
    engine = get_engine(url)
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        # Insert a minimal artifact row with malformed metadata. The artifact
        # table has no artifact-specific insert trigger, so this bypasses ORM
        # validation and exercises the real ``metadata`` column.
        conn.execute(
            text(
                "INSERT INTO artifact (id, project_id, type, uri, checksum, metadata, created_at) "
                "VALUES (:id, :pid, :typ, :uri, :ck, :md, :ca)"
            ),
            {
                "id": "a_bad",
                "pid": "p_x",
                "typ": "NOTE",
                "uri": "uri-x",
                "ck": "ck-x",
                "md": "this is not json",
                "ca": "2026-01-01 00:00:00",
            },
        )
        conn.commit()

    violations = check_metadata_json(url)
    assert any("artifact" in v for v in violations), violations
    # run_precheck returns 1 (FAIL) when malformed metadata is present.
    assert run_precheck(url) == 1
