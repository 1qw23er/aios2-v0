"""Migration proof for PR #124 -- persist owner-inbox cross-thread series_id.

Asserts from several angles:
* the Alembic tree still has a single head (now ``20260820_0001_series_id``);
* exactly one new migration file was added by #124 (chained after
  ``20260812_0001``);
* a freshly migrated DB carries the ``series_id`` column + index on all three
  owner-inbox item tables (``artifact`` / ``cs_suggestion`` / ``knowledge_candidate``);
* the migration's own backfill is correct and deterministic:
    - artifact.series_id  <- json_extract(metadata, '$.series_id') where present
    - knowledge_candidate.series_id <- source artifact.series_id (inherited)
    - cs_suggestion.series_id <- 'series:' || conversation_id
* fail-closed: a row whose series cannot be derived keeps ``series_id = NULL``
  and is NEVER guessed into a group;
* the migration is reversible (downgrade drops the columns, single head kept)
  and restartable (re-upgrade re-applies the backfill);
* the backfill statements are idempotent (re-running is a no-op);
* the persisted ``series_id`` is usable as a cross-inbox grouping key for the
  owner surface.

Mirrors the conventions of ``test_cs_migration.py`` (single head / one-file /
fresh-DB column checks) and ``test_review_protocol.py`` (explicit-revision
upgrade/downgrade round-trip that bypasses the conftest copy-shim).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from aios.db import get_engine, run_migrations
from alembic import command

# Current single leaf of the whole tree (what #124 owns + advances).
HEAD = "20260820_0001_series_id"
SERIES_REVISION = "20260820_0001_series_id"
SERIES_FILE = "20260820_0001_series_id.py"
# Previous leaf: the SalesPlaybook V0 follow-up slice. #124 chains directly after it.
PREV = "20260812_0001"
PREV_FILE = "20260812_0001_cs_suggestion_evidence_flag.py"

# Fixed seed identities (deterministic across the round-trip).
PRJ = "prj_series"
CONV = "conv_series"
ART_S = "art_series_has"   # metadata carries series_id = "S1"
ART_N = "art_series_none"  # metadata has no series_id
CS = "cs_series_1"
KC_S = "kc_series_has"     # linked to ART_S (inherits "S1")
KC_N = "kc_series_none"    # linked to ART_N (stays NULL -> fail-closed)
TS = "2026-08-20 00:00:00"


def _script_dir():
    root = Path(__file__).resolve().parents[1]
    cfg = Config(root / "alembic.ini")
    cfg.set_main_option("script_location", str(root / "alembic"))
    return root, cfg, ScriptDirectory.from_config(cfg)


def _config(url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    cfg = Config(root / "alembic.ini")
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


# ---------------------------------------------------------------------------
# T1a / T1b: single head + exactly one new migration file
# ---------------------------------------------------------------------------


def test_single_alembic_head_advanced():
    _, _, sd = _script_dir()
    assert sd.get_heads() == [HEAD]
    assert sd.get_current_head() == HEAD
    # The #124 revision is a real link of the single chain, not an orphan leaf.
    assert sd.get_revision(SERIES_REVISION) is not None
    assert SERIES_REVISION in {rev.revision for rev in sd.walk_revisions()}


def test_exactly_one_new_migration_file():
    root, _, _ = _script_dir()
    versions = root / "alembic" / "versions"
    files = [
        p.name
        for p in versions.glob("*.py")
        if not p.name.startswith("_") and p.name != "__init__.py"
    ]
    assert SERIES_FILE in files
    assert all(p.startswith("2026") for p in files)
    # #124 added exactly one migration, bounded strictly above the previous leaf
    # file and at-or-below our own file. No unrelated later slice exists yet.
    added_by_series = [p for p in files if PREV_FILE < p <= SERIES_FILE]
    assert added_by_series == [SERIES_FILE]


# ---------------------------------------------------------------------------
# T1c: fresh DB at head carries series_id column + index on all three tables
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_engine(tmp_path):
    url = f"sqlite:///{(tmp_path / 'series_mig.db').as_posix()}"
    run_migrations(url)
    return get_engine(url)


def test_series_id_columns_present_at_head(migrated_engine):
    insp = inspect(migrated_engine)
    for t in ("artifact", "cs_suggestion", "knowledge_candidate"):
        cols = {c["name"] for c in insp.get_columns(t)}
        assert "series_id" in cols, f"{t} missing series_id column"
    for t, ix in (
        ("artifact", "ix_artifact_series_id"),
        ("cs_suggestion", "ix_cs_suggestion_series_id"),
        ("knowledge_candidate", "ix_knowledge_candidate_series_id"),
    ):
        assert any(i["name"] == ix for i in insp.get_indexes(t)), f"{t} missing {ix} index"


# ---------------------------------------------------------------------------
# Seeding helper (at the PREVIOUS head, where series_id does not exist yet)
# ---------------------------------------------------------------------------


def _seed_prev_head(conn: sqlite3.Connection) -> None:
    """Insert a project + the cross-thread fixtures the backfill must reconcile.

    Runs at revision ``20260812_0001`` (no series_id column), so no INSERT lists
    series_id. The ``knowledge_candidate_validate_insert`` trigger requires the
    source artifact to be ``review_status='APPROVED'`` and the candidate to start
    as ``DRAFT`` with a consistent ``submitted_by_kind`` -- satisfied below.
    """
    conn.execute(
        "INSERT INTO project (id, name, objective, description, status, owner, "
        "budget_limit, budget_used, success_metrics, created_at, updated_at) "
        "VALUES (?, 'p', 'o', '', 'PROPOSED', 'human_ceo', 0, 0, '[]', ?, ?)",
        (PRJ, TS, TS),
    )
    conn.execute(
        "INSERT INTO conversation (id, project_id, channel, lead_stage, created_at, updated_at) "
        "VALUES (?, ?, 'mock', 'visitor', ?, ?)",
        (CONV, PRJ, TS, TS),
    )
    # ART_S: metadata carries series_id = "S1"; APPROVED so KC_S may reference it.
    conn.execute(
        "INSERT INTO artifact (id, project_id, type, uri, checksum, metadata, "
        "review_status, revision_count, provenance, created_at) "
        "VALUES (?, ?, 'JSON', 'u', 'c', ?, 'APPROVED', 0, '{}', ?)",
        (ART_S, PRJ, '{"series_id": "S1"}', TS),
    )
    # ART_N: no series_id in metadata -> backfill must leave it NULL (fail-closed).
    conn.execute(
        "INSERT INTO artifact (id, project_id, type, uri, checksum, metadata, "
        "review_status, revision_count, provenance, created_at) "
        "VALUES (?, ?, 'JSON', 'u', 'c', ?, 'APPROVED', 0, '{}', ?)",
        (ART_N, PRJ, '{}', TS),
    )
    # cs_suggestion: each conversation is exactly one series.
    conn.execute(
        "INSERT INTO cs_suggestion (id, conversation_id, project_id, decision, text, "
        "confidence, consumed, created_at, sales_evidence_cited, idempotency_key) "
        "VALUES (?, ?, ?, 'AUTO_SEND', 't', 0.5, 0, ?, 0, 'idem_cs_seed')",
        (CS, CONV, PRJ, TS),
    )
    # KC_S: inherits ART_S.series_id ("S1"). submitted_by_kind='system' keeps the
    # identity-consistency trigger satisfied without owner/agent ids.
    conn.execute(
        "INSERT INTO knowledge_candidate (id, artifact_id, project_id, statement, "
        "status, submitted_by, created_at, updated_at, source_project_id, tags, "
        "submitted_by_kind, submitted_by_owner_id, submitted_by_agent_id) "
        "VALUES (?, ?, ?, 's', 'DRAFT', 'seed', ?, ?, ?, '[]', 'system', NULL, NULL)",
        (KC_S, ART_S, PRJ, TS, TS, PRJ),
    )
    # KC_N: inherits ART_N.series_id (NULL) -> must stay NULL (fail-closed).
    conn.execute(
        "INSERT INTO knowledge_candidate (id, artifact_id, project_id, statement, "
        "status, submitted_by, created_at, updated_at, source_project_id, tags, "
        "submitted_by_kind, submitted_by_owner_id, submitted_by_agent_id) "
        "VALUES (?, ?, ?, 's', 'DRAFT', 'seed', ?, ?, ?, '[]', 'system', NULL, NULL)",
        (KC_N, ART_N, PRJ, TS, TS, PRJ),
    )
    conn.commit()


def _build_prev_head_seeded(tmp_path: Path):
    db_file = tmp_path / "series_seed.db"
    url = f"sqlite:///{db_file.as_posix()}"
    cfg = _config(url)
    # Explicit revision IDs (never "head") -> bypasses the conftest copy-shim so
    # the genuine Alembic engine runs our migration on `url`.
    command.upgrade(cfg, PREV)
    conn = sqlite3.connect(str(db_file))
    _seed_prev_head(conn)
    conn.close()
    return db_file, cfg, url


# ---------------------------------------------------------------------------
# Backfill correctness + reversibility + restartability (round-trip)
# ---------------------------------------------------------------------------


def test_series_id_migration_round_trip(tmp_path: Path) -> None:
    """Upgrade applies the backfill; downgrade drops the columns but keeps rows;
    re-upgrade restores the columns and re-applies the backfill (restartable).
    """
    db_file, cfg, _ = _build_prev_head_seeded(tmp_path)

    def series_of(table: str, row_id: str):
        with sqlite3.connect(str(db_file)) as c:
            row = c.execute(
                f"SELECT series_id FROM {table} WHERE id=?", (row_id,)
            ).fetchone()
            return row[0] if row else None

    def has_series_col(table: str) -> bool:
        with sqlite3.connect(str(db_file)) as c:
            return "series_id" in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}

    # 1) Upgrade to our head -> backfill runs.
    command.upgrade(cfg, SERIES_REVISION)
    with sqlite3.connect(str(db_file)) as c:
        assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == SERIES_REVISION
    # artifact: pulled from metadata.
    assert series_of("artifact", ART_S) == "S1"
    # artifact fail-closed: no derivable series -> NULL.
    assert series_of("artifact", ART_N) is None
    # knowledge_candidate: inherited from source artifact.
    assert series_of("knowledge_candidate", KC_S) == "S1"
    # knowledge_candidate fail-closed: source has no series -> NULL.
    assert series_of("knowledge_candidate", KC_N) is None
    # cs_suggestion: 'series:' || conversation_id.
    assert series_of("cs_suggestion", CS) == f"series:{CONV}"
    # Columns + indexes now present.
    for t in ("artifact", "cs_suggestion", "knowledge_candidate"):
        assert has_series_col(t)
    with sqlite3.connect(str(db_file)) as c:
        idx = {
            r[0]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%' AND tbl_name='artifact'"
            )
        }
        assert "ix_artifact_series_id" in idx

    # 2) Downgrade to previous head -> columns dropped, rows survive, head reset.
    command.downgrade(cfg, PREV)
    with sqlite3.connect(str(db_file)) as c:
        assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == PREV
        assert c.execute("SELECT id FROM artifact WHERE id=?", (ART_S,)).fetchone() is not None
        for t in ("artifact", "cs_suggestion", "knowledge_candidate"):
            assert "series_id" not in {r[1] for r in c.execute(f"PRAGMA table_info({t})")}

    # 3) Re-upgrade -> columns restored AND backfill re-applied (restartable).
    command.upgrade(cfg, SERIES_REVISION)
    with sqlite3.connect(str(db_file)) as c:
        assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == SERIES_REVISION
    assert has_series_col("artifact")
    assert series_of("artifact", ART_S) == "S1"
    assert series_of("artifact", ART_N) is None
    assert series_of("knowledge_candidate", KC_S) == "S1"
    assert series_of("cs_suggestion", CS) == f"series:{CONV}"


# ---------------------------------------------------------------------------
# Idempotency: re-running the backfill statements is a no-op
# ---------------------------------------------------------------------------


def test_backfill_idempotent(tmp_path: Path) -> None:
    db_file, cfg, _ = _build_prev_head_seeded(tmp_path)
    command.upgrade(cfg, SERIES_REVISION)

    # Re-execute the exact migration backfill statements a second time. The kc
    # UPDATE must drop the lifecycle-validation trigger first (same bypass the
    # migration uses) so re-running is a safe no-op.
    with sqlite3.connect(str(db_file)) as c:
        trig = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='knowledge_candidate_validate_update'"
        ).fetchone()
        if trig and trig[0]:
            c.execute("DROP TRIGGER knowledge_candidate_validate_update")
        c.execute(
            "UPDATE artifact SET series_id = json_extract(metadata, '$.series_id') "
            "WHERE series_id IS NULL AND json_extract(metadata, '$.series_id') IS NOT NULL"
        )
        c.execute(
            "UPDATE knowledge_candidate SET series_id = "
            "(SELECT a.series_id FROM artifact a WHERE a.id = knowledge_candidate.artifact_id) "
            "WHERE series_id IS NULL"
        )
        c.execute(
            "UPDATE cs_suggestion SET series_id = 'series:' || conversation_id "
            "WHERE series_id IS NULL"
        )
        if trig and trig[0]:
            c.execute(trig[0])
        c.commit()

    with sqlite3.connect(str(db_file)) as c:
        got = c.execute(
            "SELECT series_id FROM artifact WHERE id=?", (ART_S,)
        ).fetchone()[0]
        assert got == "S1"
        assert c.execute(
            "SELECT series_id FROM artifact WHERE id=?", (ART_N,)
        ).fetchone()[0] is None
        assert c.execute(
            "SELECT series_id FROM knowledge_candidate WHERE id=?", (KC_S,)
        ).fetchone()[0] == "S1"
        assert c.execute(
            "SELECT series_id FROM knowledge_candidate WHERE id=?", (KC_N,)
        ).fetchone()[0] is None
        assert c.execute(
            "SELECT series_id FROM cs_suggestion WHERE id=?", (CS,)
        ).fetchone()[0] == f"series:{CONV}"


# ---------------------------------------------------------------------------
# Cross-inbox aggregation by series_id (owner-surface grouping key)
# ---------------------------------------------------------------------------


def test_cross_inbox_aggregation_by_series_id(tmp_path: Path) -> None:
    db_file = tmp_path / "series_agg.db"
    url = f"sqlite:///{db_file.as_posix()}"
    run_migrations(url)
    with sqlite3.connect(str(db_file)) as c:
        c.execute(
            "INSERT INTO project (id, name, objective, description, status, owner, "
            "budget_limit, budget_used, success_metrics, created_at, updated_at) "
            "VALUES ('prj_agg', 'p', 'o', '', 'PROPOSED', 'human_ceo', 0, 0, '[]', ?, ?)",
            (TS, TS),
        )
        # Two artifacts in S_A, one in S_B, one ungrouped (NULL).
        for aid, sid in (("a1", "S_A"), ("a2", "S_A"), ("a3", "S_B"), ("a4", None)):
            c.execute(
                "INSERT INTO artifact (id, project_id, type, uri, checksum, metadata, "
                "review_status, revision_count, provenance, created_at, series_id) "
                "VALUES (?, 'prj_agg', 'JSON', 'u', 'c', '{}', 'APPROVED', 0, '{}', ?, ?)",
                (aid, TS, sid),
            )
        # Two conversations -> series:C1 / series:C2.
        c.execute(
            "INSERT INTO conversation (id, project_id, channel, lead_stage, "
            "created_at, updated_at) "
            "VALUES ('C1', 'prj_agg', 'mock', 'visitor', ?, ?)",
            (TS, TS),
        )
        c.execute(
            "INSERT INTO conversation (id, project_id, channel, lead_stage, "
            "created_at, updated_at) "
            "VALUES ('C2', 'prj_agg', 'mock', 'visitor', ?, ?)",
            (TS, TS),
        )
        c.execute(
            "INSERT INTO cs_suggestion (id, conversation_id, project_id, decision, text, "
            "confidence, consumed, created_at, sales_evidence_cited, series_id, idempotency_key) "
            "VALUES ('cs1', 'C1', 'prj_agg', 'AUTO_SEND', 't', 0.5, 0, ?, 0, "
            "'series:C1', 'idem_cs1')",
            (TS,),
        )
        c.execute(
            "INSERT INTO cs_suggestion (id, conversation_id, project_id, decision, text, "
            "confidence, consumed, created_at, sales_evidence_cited, series_id, idempotency_key) "
            "VALUES ('cs2', 'C2', 'prj_agg', 'AUTO_SEND', 't', 0.5, 0, ?, 0, "
            "'series:C2', 'idem_cs2')",
            (TS,),
        )
        c.commit()

        # Owner surface: union the three item tables and group by series_id.
        rows = c.execute(
            "SELECT series_id, COUNT(*) FROM ("
            "  SELECT series_id FROM artifact WHERE series_id IS NOT NULL"
            "  UNION ALL SELECT series_id FROM cs_suggestion WHERE series_id IS NOT NULL"
            "  UNION ALL SELECT series_id FROM knowledge_candidate WHERE series_id IS NOT NULL"
            ") GROUP BY series_id ORDER BY series_id"
        ).fetchall()
        buckets = {r[0]: r[1] for r in rows}
        assert buckets == {"S_A": 2, "S_B": 1, "series:C1": 1, "series:C2": 1}, buckets
        # Ungrouped rows are excluded from every bucket.
        nulls = c.execute("SELECT COUNT(*) FROM artifact WHERE series_id IS NULL").fetchone()[0]
        assert nulls == 1
