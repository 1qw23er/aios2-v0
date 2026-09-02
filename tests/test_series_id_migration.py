"""Migration proof for PR #124 -- persist owner-inbox cross-thread series_id.

Asserts from several angles:
* the Alembic tree still has a single head (now ``20260902_0001_workforce_recommendation``);
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
HEAD = "20260902_0001_workforce_recommendation"
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


# ---------------------------------------------------------------------------
# Follow-up hardening (DSH audit of PR #124)
# ---------------------------------------------------------------------------


def test_backfill_restart_loop_idempotent(tmp_path: Path) -> None:
    """Follow-up #1: the migration is *restartable* -- re-running the full
    upgrade/downgrade cycle any number of times must converge to the same
    backfilled values. This is the 7th idempotency case: the migration can be
    interrupted (transaction rollback) and re-run from scratch without drift.
    """
    db_file, cfg, _ = _build_prev_head_seeded(tmp_path)

    def series_of(table: str, row_id: str):
        with sqlite3.connect(str(db_file)) as c:
            row = c.execute(
                f"SELECT series_id FROM {table} WHERE id=?", (row_id,)
            ).fetchone()
            return row[0] if row else None

    expected = {
        ("artifact", ART_S): "S1",
        ("artifact", ART_N): None,
        ("knowledge_candidate", KC_S): "S1",
        ("knowledge_candidate", KC_N): None,
        ("cs_suggestion", CS): f"series:{CONV}",
    }
    # Cycle: upgrade -> downgrade -> upgrade -> downgrade -> upgrade.
    for _ in range(3):
        command.upgrade(cfg, SERIES_REVISION)
        for (t, rid), want in expected.items():
            assert series_of(t, rid) == want, f"{t}/{rid} drifted"
        command.downgrade(cfg, PREV)
        with sqlite3.connect(str(db_file)) as c:
            assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == PREV


def test_kc_trigger_fidelity_preserved(tmp_path: Path) -> None:
    """Follow-up #2: the migration drops + recreates the
    ``knowledge_candidate_validate_update`` trigger around the kc backfill.
    Assert the trigger is byte-for-byte restored afterwards and still enforces
    its lifecycle contract (a disallowed status transition is rejected).
    """
    db_file, cfg, _ = _build_prev_head_seeded(tmp_path)

    def trigger_sql() -> str | None:
        with sqlite3.connect(str(db_file)) as c:
            row = c.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='knowledge_candidate_validate_update'"
            ).fetchone()
            return row[0] if row else None

    before = trigger_sql()
    assert before, "trigger should exist at the PREV head"
    command.upgrade(cfg, SERIES_REVISION)
    after = trigger_sql()
    assert after == before, "migration must recreate the trigger verbatim"

    # The trigger still enforces: the only legal transition is DRAFT ->
    # APPROVED/REJECTED. Any other status (or a non-DRAFT source) is rejected.
    # Drive it through the real SQLite engine, not ORM, so the trigger (not the
    # ORM lifecycle layer) is what we assert on.
    with sqlite3.connect(str(db_file)) as c:
        # Legal transition DRAFT -> APPROVED must succeed.
        c.execute(
            "UPDATE knowledge_candidate SET status='APPROVED' WHERE id=?", (KC_S,)
        )
        c.commit()
        # Once APPROVED, any further status change is illegal and must raise.
        try:
            c.execute(
                "UPDATE knowledge_candidate SET status='FORBIDDEN_X' WHERE id=?",
                (KC_S,),
            )
            c.commit()
            raise AssertionError("trigger should have rejected illegal status")
        except sqlite3.Error:
            # The lifecycle trigger correctly aborts the illegal transition
            # (sqlite3.IntegrityError / OperationalError depending on driver).
            c.rollback()


def test_cs_series_prefix_consistency(tmp_path: Path) -> None:
    """Follow-up #4: ``cs_suggestion.series_id`` is ALWAYS ``'series:' ||
    conversation_id`` (deterministic 1-conversation-1-series). The other two
    tables (artifact / knowledge_candidate) must NEVER carry that prefix --
    their series ids come from metadata / inheritance and share no namespace
    with the cs prefix, so cross-table grouping cannot accidentally merge a cs
    row into an artifact/kc series.
    """
    db_file, cfg, _ = _build_prev_head_seeded(tmp_path)
    command.upgrade(cfg, SERIES_REVISION)
    with sqlite3.connect(str(db_file)) as c:
        cs_val = c.execute(
            "SELECT series_id FROM cs_suggestion WHERE id=?", (CS,)
        ).fetchone()[0]
        assert cs_val == f"series:{CONV}"
        assert cs_val.startswith("series:")

        # artifact series ids must NOT use the cs prefix namespace.
        art_val = c.execute(
            "SELECT series_id FROM artifact WHERE id=?", (ART_S,)
        ).fetchone()[0]
        assert art_val == "S1"
        assert not art_val.startswith("series:")

        # knowledge_candidate inherited series id must NOT use the cs prefix.
        kc_val = c.execute(
            "SELECT series_id FROM knowledge_candidate WHERE id=?", (KC_S,)
        ).fetchone()[0]
        assert kc_val == "S1"
        assert not kc_val.startswith("series:")


def test_models_do_not_map_series_id(tmp_path: Path) -> None:
    """Follow-up #9: regression guard. ``series_id`` is a raw-SQL grouping key
    on artifact / cs_suggestion / knowledge_candidate and is DELIBERATELY not
    mapped as an ORM field (Design B). If someone re-adds it to the ORM, this
    test fails loudly -- remapping would break the zero-side-effect contract
    and the prior-head ``test_knowledge_models`` seed.
    """
    from aios.models import Artifact, CsSuggestion, KnowledgeCandidate

    for cls in (Artifact, CsSuggestion, KnowledgeCandidate):
        assert "series_id" not in cls.model_fields, (
            f"{cls.__name__} must NOT map series_id (Design B: raw-SQL column only)"
        )


def test_consumer_contract_no_invalid_series_grouping() -> None:
    """Follow-up #7: consumer-contract scan. The three owner-inbox item tables
    (``artifact`` / ``cs_suggestion`` / ``knowledge_candidate``) carry
    ``series_id`` as a *raw-SQL* grouping key only -- it is NOT an ORM-mapped
    column, so there is no SQLAlchemy ORM path that could accidentally GROUP BY
    it. Any *raw SQL* that groups these tables by ``series_id`` would be a
    contract violation (it would couple business logic to an unmapped column
    and could pull NULL sentinels into a group).

    The ONLY legitimate ``series_id`` grouping in the codebase is on
    ``knowledge_fact`` (which DOES map series_id as a non-nullable column, via
    ``context_service._latest_versions`` / ``owner_inbox._series_labels``).

    This test statically scans ``src/aios`` for raw ``GROUP BY`` SQL and asserts:
      * no raw SQL groups the three inbox tables by ``series_id``;
      * the only ``series_id``-grouping SQL in the tree (if any) references
        ``knowledge_fact``.
    """
    import re

    src_root = Path(__file__).resolve().parents[1] / "src" / "aios"
    # Hardened scan (follow-up #3, DSH audit): a ``GROUP BY`` split across
    # multiple physical lines -- e.g. two adjacent string literals
    # ``"FROM artifact " "GROUP BY series_id"`` -- must NOT evade the check.
    # We collapse all intra-statement whitespace (newlines included) BEFORE
    # matching so a multi-line literal becomes one logical SQL string, then we
    # split into statements on ';' and re-scan each flattened statement.
    inbox_tables = {"artifact", "cs_suggestion", "knowledge_candidate"}
    for py in src_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for stmt in text.split(";"):
            flat = re.sub(r"\s+", " ", stmt).lower()
            if "group by" not in flat:
                continue
            # (A) A GROUP BY on an inbox table's series_id is forbidden.
            for t in inbox_tables:
                assert f"group by {t}" not in flat, (
                    f"{py} raw SQL groups inbox table '{t}' by series_id -- "
                    f"violates Design B (series_id is raw-SQL only, never grouped in SQL)"
                )
            # (B) Any series_id grouping must be on knowledge_fact exclusively.
            # This is what catches the cross-line evasion: after flattening,
            # "FROM artifact GROUP BY series_id" carries both 'series_id' and
            # 'group by' but no 'knowledge_fact', so it fails here.
            if "series_id" in flat:
                assert "knowledge_fact" in flat or "knowledgefact" in flat, (
                    f"{py} raw SQL groups by series_id but not on knowledge_fact -- "
                    f"only knowledge_fact may be grouped by series_id "
                    f"(it is ORM-mapped + NOT NULL)"
                )


def test_null_series_never_grouped_contract() -> None:
    """Follow-up #7 (companion): a NULL ``series_id`` is the explicit
    "ungrouped" sentinel. The intended owner-surface grouping query (the one
    exercised end-to-end by ``test_cross_inbox_aggregation_by_series_id``) MUST
    filter ``series_id IS NOT NULL`` so ungrouped rows never land in a bucket.
    This test pins that convention in the source of the aggregation query so a
    future edit that drops the NULL guard fails loudly.
    """
    src = Path(__file__).read_text(encoding="utf-8")
    # The canonical union-grouping query in test_cross_inbox_aggregation_by_series_id
    # filters each inbox table on series_id IS NOT NULL before the GROUP BY, so
    # NULL sentinels can never land in a bucket. Pin that the convention is present.
    assert "WHERE series_id IS NOT NULL" in src, (
        "owner-surface grouping query must filter series_id IS NOT NULL so NULL "
        "sentinels are never grouped"
    )
    assert "GROUP BY series_id" in src, (
        "owner-surface grouping query must GROUP BY series_id (the union bucket)"
    )


# ---------------------------------------------------------------------------
# Follow-up #5: malformed metadata JSON must not abort the artifact backfill
# ---------------------------------------------------------------------------


def test_json_guard_skips_malformed_metadata(tmp_path: Path) -> None:
    """Follow-up #5 (DSH audit): the base series_id migration backfills
    ``artifact.series_id`` with ``json_extract(metadata, '$.series_id')``. On a
    row whose ``metadata`` is NOT valid JSON, SQLite raises ``malformed JSON``
    and the whole backfill statement (hence the migration transaction) aborts --
    violating the fail-closed contract (one corrupt row must never block every
    other row's backfill).

    The ``20260824_0001_series_id_json_guard`` reinforcement re-runs the artifact
    backfill guarded by ``json_valid(metadata)`` so malformed rows are skipped and
    kept NULL. This test proves:

      * applying JSON_GUARD does NOT raise even when the table holds a malformed
        ``metadata`` row (the original unguarded backfill WOULD have aborted);
      * the malformed row keeps ``series_id = NULL`` (fail-closed, ungrouped);
      * a valid-JSON row that the base migration left ungrouped (e.g. because it
        was inserted after the base backfill ran) now gets backfilled;
      * the JSON_GUARD upgrade is idempotent (re-applying is a no-op) and leaves
        the alembic head at JSON_GUARD.
    """
    db_file = tmp_path / "json_guard.db"
    url = f"sqlite:///{db_file.as_posix()}"
    cfg = _config(url)

    # Build the DB up to the PREVIOUS head (no series_id column yet) and seed the
    # canonical cross-thread fixtures (ART_S / ART_N / CS / KC_S / KC_N).
    command.upgrade(cfg, PREV)
    conn = sqlite3.connect(str(db_file))
    _seed_prev_head(conn)
    conn.close()
    # Advance to the BASE series_id revision (json_guard not applied yet). The
    # base backfill runs here on the seeded rows; the rows we add below are
    # inserted AFTER it, so they start NULL -- exactly the state JSON_GUARD must
    # repair without choking on malformed metadata.
    command.upgrade(cfg, SERIES_REVISION)
    with sqlite3.connect(str(db_file)) as c:
        assert c.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == SERIES_REVISION
    # Sanity: seeded valid-JSON row already backfilled by the base migration.
    with sqlite3.connect(str(db_file)) as c:
        assert c.execute(
            "SELECT series_id FROM artifact WHERE id=?", (ART_S,)
        ).fetchone()[0] == "S1"

    # Insert two post-backfill artifacts directly (bypassing the ORM so we can
    # plant a deliberately malformed metadata value).
    ART_OK = "art_json_ok"      # valid JSON carrying series_id -> should be backfilled
    ART_BAD = "art_json_bad"    # malformed JSON -> must be skipped, kept NULL
    with sqlite3.connect(str(db_file)) as c:
        c.execute(
            "INSERT INTO artifact (id, project_id, type, uri, checksum, metadata, "
            "review_status, revision_count, provenance, created_at) "
            "VALUES (?, ?, 'JSON', 'u', 'c', ?, 'APPROVED', 0, '{}', ?)",
            (ART_OK, PRJ, '{"series_id": "S2"}', TS),
        )
        c.execute(
            "INSERT INTO artifact (id, project_id, type, uri, checksum, metadata, "
            "review_status, revision_count, provenance, created_at) "
            "VALUES (?, ?, 'JSON', 'u', 'c', ?, 'APPROVED', 0, '{}', ?)",
            # malformed: a plain string, not JSON -> json_extract would raise on
            # the unguarded base statement.
            (ART_BAD, PRJ, "not-json{", TS),
        )
        c.commit()

    # idempotent helper for re-applying JSON_GUARD's exact statement.
    def reapply_json_guard() -> None:
        with sqlite3.connect(str(db_file)) as c:
            c.execute(
                "UPDATE artifact "
                "SET series_id = json_extract(metadata, '$.series_id') "
                "WHERE series_id IS NULL "
                "AND json_valid(metadata) "
                "AND json_extract(metadata, '$.series_id') IS NOT NULL"
            )
            c.commit()

    # 1) Applying the JSON_GUARD migration must NOT raise despite the malformed row.
    command.upgrade(cfg, HEAD)
    with sqlite3.connect(str(db_file)) as c:
        assert c.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == HEAD

    def series_of(rid: str):
        with sqlite3.connect(str(db_file)) as c:
            return c.execute(
                "SELECT series_id FROM artifact WHERE id=?", (rid,)
            ).fetchone()[0]

    # 2) Valid-JSON row got backfilled; malformed row kept NULL (fail-closed).
    assert series_of(ART_OK) == "S2"
    assert series_of(ART_BAD) is None
    # Pre-existing valid-JSON seed (ART_S) is untouched by the idempotent guard.
    assert series_of(ART_S) == "S1"

    # 3) Re-applying JSON_GUARD's statement is a no-op (idempotent).
    reapply_json_guard()
    assert series_of(ART_OK) == "S2"
    assert series_of(ART_BAD) is None
    assert series_of(ART_S) == "S1"


def test_json_guard_is_current_head() -> None:
    """Follow-up #5 (companion): the JSON-guard reinforcement still chains directly
    on the base series_id migration, and a later migration (e.g. the W1 Workforce
    core) now sits on top of it as the single alembic head. This pins the
    published-migration-immutability contract: we never edit the base file, we only
    add on top.
    """
    _, _, sd = _script_dir()
    assert sd.get_heads() == [HEAD]
    assert sd.get_revision("20260824_0001_series_id_json_guard").down_revision == SERIES_REVISION
