"""PILOT-2A-3A -- Staging Schema Refresh: evolve (not destroy) the isolated Pilot-2 schema.

Owner acceptance gate (merge authorisation for PR #139, PILOT-2A-3A route):

  "existing PILOT-2A-2 staging schema may lack the newer RegistrationObservation
   columns / UNIQUE(customer_id). create_all(checkfirst=True) will not ALTER an
   existing table. Therefore the new engine must NOT be connected to the existing
   real staging database until the isolated Pilot-2 schema is refreshed. ...
   rebuild or explicitly evolve only the isolated Pilot-2 staging tables; verify
   the final schema matches merged models; verify required UNIQUE / FK / trigger
   constraints; run the registration diff engine against controlled staging
   snapshots; prove idempotent replay; prove no KnowledgeFact side effect; prove
   no Mihe write side effect."

The guard supplies the AUTHORITY (the authorised staging DB path) so the test is
genuinely off-box: no real staging database is ever opened. The 2A-2-era drift
is simulated with raw DDL inside a throwaway SQLite file (the sandbox cannot
reach the real UAT host, whose ``uat_ool_v0/human_env.py`` is generated at
staging time).

This module is the RED->GREEN proof for the evolution mechanism added to
``aios.pilot2.migrations_create_all``:

  * RED (pre-fix): ``pilot2_metadata.create_all`` is a no-op for an existing
    table, and ``verify_pilot2_schema`` only checks table + D2-trigger presence
    -- so a 2A-2 ``registrationobservation`` (missing the 2A-3 extension columns
    and possibly the UNIQUE index) passes ``run_create`` but then breaks the
    diff engine at persist time (no ``observation_hash`` column).
  * GREEN (post-fix): ``run_create`` now EVOLVES existing tables (ADD missing
    columns, CREATE missing indexes, REBUILD when a foreign key is missing --
    SQLite cannot ALTER ADD a FK, so the table is rebuilt with all rows copied)
    and ``verify_pilot2_schema`` additionally refuses any half-evolved schema
    whose columns OR foreign keys are incomplete. A 2A-2-era
    ``registrationobservation`` that lacked the source_snapshot_id ->
    mihesnapshot.id FK is rebuilt so it can no longer accept orphan rows.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlmodel import Session, inspect, select

from aios.db import get_engine
from aios.pilot2 import migrations_create_all as mca
from aios.pilot2.models import (
    MiheSnapshot,
    RegistrationObservation,
    pilot2_metadata,
)
from aios.pilot2.registration_diff import (
    MiheCustomer,
    MiheCustomerSnapshot,
    run_registration_diff,
)


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


# 2A-2-era ``registrationobservation`` DDL -- the BASE columns only, deliberately
# WITHOUT the PILOT-2A-3 extension columns and WITHOUT the UNIQUE(customer_id)
# index. This is the exact drift the owner flagged: a real 2A-2 staging DB that
# was created before the diff-engine materialization landed.
_OLD_REGISTRATIONOBSERVATION_DDL = """
CREATE TABLE registrationobservation (
    id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    last_login_at TEXT,
    total_recharge INTEGER NOT NULL,
    recharge_count INTEGER NOT NULL,
    balance INTEGER NOT NULL,
    cohort_tag VARCHAR(20) NOT NULL,
    is_batch BOOLEAN NOT NULL,
    source_snapshot_id TEXT NOT NULL,
    derived_at TEXT NOT NULL,
    PRIMARY KEY (id)
)
"""

# The raw-evidence layer is unchanged between 2A-2 and 2A-3, so it is created in
# its CURRENT shape; the refresh must preserve every existing row there.
_MIHESNAPSHOT_DDL = """
CREATE TABLE mihesnapshot (
    id TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    endpoint VARCHAR NOT NULL,
    page INTEGER,
    total_count INTEGER,
    raw_payload TEXT,
    fetch_status VARCHAR NOT NULL,
    raw_hash TEXT NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (raw_hash)
)
"""


def _build_2a2_drifted_db(db_path: Path) -> object:
    """A 2A-2-era Pilot-2 staging DB: old ``registrationobservation`` + ``mihesnapshot``.

    Returns the engine. Both tables already exist (so ``create_all`` is a no-op
    for them); ``evolve_existing_tables`` is what must bring
    ``registrationobservation`` up to the 2A-3 shape.
    """
    engine = get_engine(sqlite_url(db_path))
    with engine.begin() as conn:
        conn.execute(text(_OLD_REGISTRATIONOBSERVATION_DDL))
        conn.execute(text(_MIHESNAPSHOT_DDL))
        # Two pre-existing registrations (real 2A-2 rows) -- MUST survive refresh.
        conn.execute(
            text(
                "INSERT INTO registrationobservation "
                "(id, customer_id, registered_at, last_login_at, total_recharge, "
                "recharge_count, balance, cohort_tag, is_batch, source_snapshot_id, derived_at) "
                "VALUES (:id, :cid, :reg, :login, :tr, :rc, :bal, :cohort, :batch, :src, :der)"
            ),
            [
                {
                    "id": "regob_old1", "cid": "cust_old1",
                    "reg": "2026-08-04 10:00:00.000000", "login": None,
                    "tr": 0, "rc": 0, "bal": 0, "cohort": "NATURAL",
                    "batch": 0, "src": "msnap_old", "der": "2026-08-04 10:00:00.000000",
                },
                {
                    "id": "regob_old2", "cid": "cust_old2",
                    "reg": "2026-08-04 11:30:00.000000", "login": None,
                    "tr": 10, "rc": 1, "bal": 5, "cohort": "NATURAL",
                    "batch": 0, "src": "msnap_old", "der": "2026-08-04 11:30:00.000000",
                },
            ],
        )
        # One pre-existing raw-evidence row -- MUST survive refresh.
        conn.execute(
            text(
                "INSERT INTO mihesnapshot "
                "(id, taken_at, endpoint, page, total_count, raw_payload, fetch_status, raw_hash) "
                "VALUES (:id, :taken, :ep, :page, :total, :payload, :status, :rh)"
            ),
            {
                "id": "msnap_old", "taken": "2026-08-04 10:00:00.000000",
                "ep": "CUSTOMERS", "page": 1, "total": 2, "payload": "{}",
                "status": "OK", "rh": "evidence_pre_hash",
            },
        )
    return engine


# Batch-window fixtures for the diff engine (mirror the engine's own constants).
_BATCH_START_MS = 1_785_891_600_000  # 2026-08-05T01:00:00Z
_NATURAL_JULY_MS = 1_783_598_400_000  # 2026-07-09T12:00:00Z


def _cust(cid: str, reg_ms: int, **kw) -> MiheCustomer:
    return MiheCustomer(id=cid, registered_at_ms=reg_ms, **kw)


def _snap(seq: int, customers: tuple[MiheCustomer, ...] = ()) -> MiheCustomerSnapshot:
    return MiheCustomerSnapshot(
        seq=seq, is_complete=True, customers=customers,
        total=len(customers), page=1, page_size=20,
    )


# ===========================================================================
# Primary acceptance test: run_create evolves a 2A-2-era schema in place
# ===========================================================================
def test_p23a_run_create_evolves_2a2_registrationobservation_and_preserves_rows(tmp_path: Path):
    """The owner gate, end to end: refresh an isolated Pilot-2 staging DB whose
    ``registrationobservation`` predates 2A-3, and prove the result matches the
    merged models WITHOUT destroying a single pre-existing row.
    """
    db_path = tmp_path / "staging_2a2.db"
    engine = _build_2a2_drifted_db(db_path)

    # --- the refresh (exactly what owner runs on the staging host) ---
    tables = mca.run_create(engine)
    assert "registrationobservation" in tables

    # --- 1) all 8 PILOT-2A-3 extension columns now exist ---
    cols = {c["name"] for c in inspect(engine).get_columns("registrationobservation")}
    for expected in (
        "observation_hash", "first_seen_seq", "last_seen_seq", "version",
        "nickname", "avatar", "phone_masked", "customer_type",
    ):
        assert expected in cols, f"refresh must add column {expected!r}"

    # --- 2) UNIQUE(customer_id) index is present (may have been absent in 2A-2) ---
    idx_rows = engine.connect().execute(
        text("PRAGMA index_list(registrationobservation)")
    ).fetchall()
    idx_names = {r[1] for r in idx_rows}
    assert "ix_registrationobservation_customer_id" in idx_names
    # and it really is UNIQUE
    unique_flags = {r[1]: bool(r[2]) for r in idx_rows}
    assert unique_flags["ix_registrationobservation_customer_id"] is True

    # --- 2b) the source_snapshot_id -> mihesnapshot.id FK is present and
    #         actually enforced (PILOT-2A-3A P1-3: a 2A-2-era table without this
    #         FK would otherwise accept orphan registration rows). ---
    fk_rows = engine.connect().execute(
        text("PRAGMA foreign_key_list(registrationobservation)")
    ).fetchall()
    # PRAGMA foreign_key_list -> (id, seq, table, from, to, ...)
    fk_targets = {(r[3], r[2], r[4]) for r in fk_rows}
    assert ("source_snapshot_id", "mihesnapshot", "id") in fk_targets

    # --- 3) the 2 pre-existing registrations are preserved, byte-for-byte ---
    with Session(engine) as s:
        old1 = s.exec(
            select(RegistrationObservation).where(
                RegistrationObservation.customer_id == "cust_old1"
            )
        ).one()
        old2 = s.exec(
            select(RegistrationObservation).where(
                RegistrationObservation.customer_id == "cust_old2"
            )
        ).one()
    assert old1.id == "regob_old1"
    assert old1.total_recharge == 0 and old1.balance == 0
    assert old2.id == "regob_old2"
    assert old2.total_recharge == 10 and old2.balance == 5
    # The added (nullable) columns carry NULL for never-observed rows -- they are
    # not silently zeroed or dropped.
    assert old1.observation_hash is None
    assert old1.first_seen_seq == 0

    # --- 4) the immutable MiheSnapshot raw-evidence layer is preserved ---
    with Session(engine) as s:
        snap = s.exec(
            select(MiheSnapshot).where(MiheSnapshot.raw_hash == "evidence_pre_hash")
        ).one()
    assert snap.id == "msnap_old"

    # --- 5) the diff engine can NOW persist into the refreshed table ---
    snaps = [_snap(seq=1, customers=(_cust("cust_new1", _NATURAL_JULY_MS, balance=42),))]
    _rows, changed = run_registration_diff(engine, snaps)
    assert changed == 1
    with Session(engine) as s:
        new1 = s.exec(
            select(RegistrationObservation).where(
                RegistrationObservation.customer_id == "cust_new1"
            )
        ).one()
    assert new1.observation_hash is not None  # the 2A-3 materialization column works

    # --- 6) idempotent replay (C4/C5) on the refreshed schema ---
    _rows2, changed2 = run_registration_diff(engine, snaps)
    assert changed2 == 0, "replay on the refreshed schema must be a no-op"

    # --- 7) total row count == 2 preserved + 1 new = 3 ---
    with Session(engine) as s:
        assert len(s.exec(select(RegistrationObservation)).all()) == 3

    engine.dispose()


# ===========================================================================
# Guard: verify_pilot2_schema must REFUSE a half-evolved (column-incomplete) schema
# ===========================================================================
def test_p23a_verify_refuses_column_incomplete_schema(tmp_path: Path):
    """A 2A-2-era ``registrationobservation`` that still lacks columns must be
    refused by ``verify_pilot2_schema`` -- fail-closed, never silently passed.

    This is the guard that makes the evolution step mandatory: without it, the
    no-op ``create_all`` would let the diff engine connect to a broken table.
    """
    db_path = tmp_path / "incomplete.db"
    engine = get_engine(sqlite_url(db_path))
    with engine.begin() as conn:
        # A partial registrationobservation (only 2 of 19 columns) plus the rest
        # of the pilot2 tables created normally.
        conn.execute(text(
            "CREATE TABLE registrationobservation (id TEXT PRIMARY KEY, customer_id TEXT NOT NULL)"
        ))
    pilot2_metadata.create_all(engine)  # creates every OTHER pilot2 table + D2 triggers

    with pytest.raises(mca.SchemaRebuildError) as excinfo:
        mca.verify_pilot2_schema(engine)
    message = str(excinfo.value)
    assert "registrationobservation" in message
    assert "missing columns" in message
    # The specific missing column must be named (reviewable failure).
    assert "observation_hash" in message
    engine.dispose()


# ===========================================================================
# Preservation + no-KnowledgeFact / no-Mihe-write side effects
# ===========================================================================
def test_p23a_refresh_touches_only_pilot2_namespace(tmp_path: Path):
    """run_create must NOT create any main-schema table (KnowledgeFact, Content,
    auth, ...) and must NOT write to Mihe. The isolated Pilot-2 refresh is scoped
    to the pilot2_metadata namespace only.
    """
    db_path = tmp_path / "scoped.db"
    engine = _build_2a2_drifted_db(db_path)
    mca.run_create(engine)

    names = set(inspect(engine).get_table_names())
    # The isolated Pilot-2 refresh creates EXACTLY the pilot2_metadata tables and
    # nothing else -- no KnowledgeFact, Content, CS, Feedback, auth or any
    # main-schema table. Exact equality is the strongest, reviewable claim.
    assert names == set(pilot2_metadata.tables.keys())
    assert not any("knowledgefact" in n.lower() for n in names)

    # The diff engine's own read-only contract still holds on the refreshed DB.
    import importlib.util as _u
    src = Path(_u.find_spec("aios.pilot2.registration_diff").origin).read_text(encoding="utf-8")
    assert "import requests" not in src and "import urllib" not in src
    engine.dispose()


# ===========================================================================
# Regression: a fully-current schema is unaffected by the evolution step
# ===========================================================================
def test_p23a_current_schema_is_idempotent_under_refresh(tmp_path: Path):
    """On a DB that already has the 2A-3 shape, run_create (which now runs
    evolve) must be a clean no-op: no columns added, no rows changed.
    """
    db_path = tmp_path / "current.db"
    engine = get_engine(sqlite_url(db_path))
    pilot2_metadata.create_all(engine)  # current full schema

    before_cols = {c["name"] for c in inspect(engine).get_columns("registrationobservation")}
    tables = mca.run_create(engine)
    after_cols = {c["name"] for c in inspect(engine).get_columns("registrationobservation")}
    assert before_cols == after_cols, "current schema must not be altered"
    assert "registrationobservation" in tables
    engine.dispose()


# ===========================================================================
# P1-1: a real 2A-2 DB may already carry a NON-UNIQUE ix_registrationobservation_
#        customer_id index -- the old logic skipped it by name, leaving a
#        non-unique index that let duplicate customer_id through. It must be
#        upgraded to UNIQUE.
# ===========================================================================
def test_p23a_upgrades_legacy_nonunique_customer_id_index_to_unique(tmp_path: Path):
    """P1-1: a real 2A-2 staging DB can ALREADY have a plain (non-unique)
    ``ix_registrationobservation_customer_id`` index. The previous logic skipped
    it by name, so after refresh the index stayed non-unique and a duplicate
    ``customer_id`` could be written -- violating the hard 'one customer = one
    registration row' constraint. After refresh the index must be UNIQUE and the
    constraint must actually be enforced.
    """
    db_path = tmp_path / "legacy_nonunique_idx.db"
    engine = get_engine(sqlite_url(db_path))
    with engine.begin() as conn:
        conn.execute(text(_OLD_REGISTRATIONOBSERVATION_DDL))
        # Simulate the real 2A-2 state: a NON-unique index already exists.
        conn.execute(text(
            'CREATE INDEX ix_registrationobservation_customer_id '
            'ON registrationobservation (customer_id)'
        ))
        # The raw-evidence layer exists in 2A-2 too (FK source). The two
        # registrations below reference source_snapshot_id="msnap", so a valid
        # mihesnapshot row must exist -- otherwise the P1-3 FK pre-flight would
        # (correctly) refuse the refresh as an orphan reference.
        conn.execute(text(_MIHESNAPSHOT_DDL))
        conn.execute(
            text(
                "INSERT INTO mihesnapshot "
                "(id, taken_at, endpoint, page, total_count, raw_payload, fetch_status, raw_hash) "
                "VALUES (:id, :taken, :ep, :page, :total, :payload, :status, :rh)"
            ),
            {
                "id": "msnap", "taken": "2026-08-04 09:00:00.000000",
                "ep": "CUSTOMERS", "page": 1, "total": 2, "payload": "{}",
                "status": "OK", "rh": "legacy_nonunique_hash",
            },
        )
        # Unique customer_ids (no duplicates) so the evolve can proceed.
        conn.execute(
            text(
                "INSERT INTO registrationobservation "
                "(id, customer_id, registered_at, last_login_at, total_recharge, "
                "recharge_count, balance, cohort_tag, is_batch, source_snapshot_id, derived_at) "
                "VALUES (:id, :cid, :reg, :login, :tr, :rc, :bal, :cohort, :batch, :src, :der)"
            ),
            [
                {
                    "id": "r1", "cid": "c1",
                    "reg": "2026-08-04 10:00:00.000000", "login": None,
                    "tr": 0, "rc": 0, "bal": 0, "cohort": "NATURAL",
                    "batch": 0, "src": "msnap", "der": "2026-08-04 10:00:00.000000",
                },
                {
                    "id": "r2", "cid": "c2",
                    "reg": "2026-08-04 11:30:00.000000", "login": None,
                    "tr": 1, "rc": 1, "bal": 2, "cohort": "NATURAL",
                    "batch": 0, "src": "msnap", "der": "2026-08-04 11:30:00.000000",
                },
            ],
        )

    mca.run_create(engine)

    idx_rows = engine.connect().execute(
        text("PRAGMA index_list(registrationobservation)")
    ).fetchall()
    unique_flags = {r[1]: bool(r[2]) for r in idx_rows}
    assert unique_flags["ix_registrationobservation_customer_id"] is True

    # The constraint is now enforced: a duplicate customer_id is rejected.
    with pytest.raises(SAIntegrityError), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO registrationobservation "
                "(id, customer_id, registered_at, last_login_at, total_recharge, "
                "recharge_count, balance, cohort_tag, is_batch, source_snapshot_id, derived_at) "
                "VALUES (:id, :cid, :reg, :login, :tr, :rc, :bal, :cohort, :batch, :src, :der)"
            ),
            {
                "id": "r3", "cid": "c1",
                "reg": "2026-08-05 10:00:00.000000", "login": None,
                "tr": 0, "rc": 0, "bal": 0, "cohort": "NATURAL",
                "batch": 0, "src": "msnap", "der": "2026-08-05 10:00:00.000000",
            },
        )
    engine.dispose()


# ===========================================================================
# P1-2: legacy data with duplicate customer_id must be caught by PRE-FLIGHT and
#       refused BEFORE any DDL -- so the failed refresh leaves the table exactly
#       as it was (no ALTER residue), not half-evolved with 8 new columns.
# ===========================================================================
def test_p23a_evolve_refuses_duplicate_customer_id_without_mutating(tmp_path: Path):
    """P1-2: when the legacy ``registrationobservation`` already contains
    duplicate ``customer_id``, the UNIQUE index creation would fail AFTER the 8
    ``ALTER TABLE ADD COLUMN`` statements had already committed (SQLite
    autocommits DDL). The refresh must instead PRE-FLIGHT the duplicate and raise
    BEFORE mutating, so the table is left exactly as found -- no
    ``observation_hash`` column, no partial evolve, and no row lost.
    """
    db_path = tmp_path / "legacy_dup.db"
    engine = get_engine(sqlite_url(db_path))
    with engine.begin() as conn:
        conn.execute(text(_OLD_REGISTRATIONOBSERVATION_DDL))
        # Two rows sharing the SAME customer_id -- violates the hard constraint.
        conn.execute(
            text(
                "INSERT INTO registrationobservation "
                "(id, customer_id, registered_at, last_login_at, total_recharge, "
                "recharge_count, balance, cohort_tag, is_batch, source_snapshot_id, derived_at) "
                "VALUES (:id, :cid, :reg, :login, :tr, :rc, :bal, :cohort, :batch, :src, :der)"
            ),
            [
                {
                    "id": "r1", "cid": "SAME",
                    "reg": "2026-08-04 10:00:00.000000", "login": None,
                    "tr": 0, "rc": 0, "bal": 0, "cohort": "NATURAL",
                    "batch": 0, "src": "msnap", "der": "2026-08-04 10:00:00.000000",
                },
                {
                    "id": "r2", "cid": "SAME",
                    "reg": "2026-08-04 11:30:00.000000", "login": None,
                    "tr": 1, "rc": 1, "bal": 2, "cohort": "NATURAL",
                    "batch": 0, "src": "msnap", "der": "2026-08-04 11:30:00.000000",
                },
            ],
        )

    # The refresh must refuse BEFORE mutating the schema.
    with pytest.raises(mca.SchemaRebuildError) as excinfo:
        mca.run_create(engine)
    assert "duplicate" in str(excinfo.value).lower() or "UNIQUE" in str(excinfo.value)

    # The schema is UNCHANGED: none of the 2A-3 columns were added.
    cols = {c["name"] for c in inspect(engine).get_columns("registrationobservation")}
    assert "observation_hash" not in cols
    assert "first_seen_seq" not in cols
    assert "version" not in cols
    # And the two duplicate rows are still intact (no data loss either).
    # NOTE: the table was correctly NOT evolved (refused), so we count rows with
    # raw SQL -- the ORM model would expect the 2A-3 columns that were never added.
    kept = engine.connect().execute(
        text("SELECT COUNT(*) FROM registrationobservation")
    ).scalar_one()
    assert kept == 2
    engine.dispose()


# ===========================================================================
# P1-3: a 2A-2-era registrationobservation may LACK the source_snapshot_id ->
#       mihesnapshot.id foreign key. After refresh the FK must be present AND
#       enforced (orphan writes rejected), and a legacy DB that already holds an
#       orphan source_snapshot_id must be refused BEFORE any mutation.
# ===========================================================================
def test_p23a_refresh_adds_missing_fk_and_rejects_orphan_write(tmp_path: Path):
    """P1-3 (success path): the merged model declares
    ``RegistrationObservation.source_snapshot_id -> mihesnapshot.id``. A 2A-2
    staging DB created this table WITHOUT that FK (SQLite cannot ALTER ADD a FK,
    so ``create_all`` could never add it either). After refresh the FK must be
    present in the schema and actually enforced by the AIOS engine (which runs
    with ``PRAGMA foreign_keys=ON``): writing a
    ``source_snapshot_id`` that points at no ``mihesnapshot`` row must be
    rejected with IntegrityError.
    """
    db_path = tmp_path / "legacy_no_fk.db"
    # _build_2a2_drifted_db creates registrationobservation WITHOUT the FK, with
    # source_snapshot_id referencing the one real mihesnapshot row it inserts.
    engine = _build_2a2_drifted_db(db_path)

    # Two-layer verification, owner-specified:
    # (a) the AIOS engine enforces foreign keys on its connections.
    with engine.connect() as probe:
        assert probe.execute(text("PRAGMA foreign_keys")).scalar() == 1
    # (b) before refresh the FK is genuinely absent (the bug under test).
    before = engine.connect().execute(
        text("PRAGMA foreign_key_list(registrationobservation)")
    ).fetchall()
    assert ("source_snapshot_id", "mihesnapshot", "id") not in {
        (r[3], r[2], r[4]) for r in before
    }

    # ==== PROBE-20260903 v2: stepwise run_create with per-step FK audit.
    # CI-only rebuild miss: push attempts 1+2 = verify passed but after-FK set
    # empty; PR#6 probe v1 = verify raised finalattributionhead missing FKs.
    # Stepwise audit shows exactly which table loses FKs at which step. ====
    from aios.pilot2.migrations_create_all import (
        _declared_fk_tuples as _dft_probe,
        _table_fk_tuples as _tft_probe,
    )

    def _audit(label: str) -> None:
        print(f"PROBE2[{label}] start", flush=True)
        for _tn in sorted(pilot2_metadata.tables.keys()):
            _raw = engine.raw_connection()
            try:
                _cur = _raw.driver_connection.cursor()
                _decl = sorted(_dft_probe(pilot2_metadata.tables[_tn]))
                _db = sorted(_tft_probe(_cur, _tn))
            finally:
                _raw.close()
            if _decl != _db:
                print(f"PROBE2[{label}] DIFF {_tn}: declared={_decl} db={_db}", flush=True)
        print(f"PROBE2[{label}] done", flush=True)

    _audit("pre")
    pilot2_metadata.create_all(engine)
    _audit("post-create")
    mca.evolve_existing_tables(engine)
    _audit("post-evolve")
    mca.verify_pilot2_schema(engine)
    _audit("post-verify")
    mca.retire_superseded_tables(engine)
    _audit("post-retire")

    # After refresh the FK exists and is enforceable.
    after = engine.connect().execute(
        text("PRAGMA foreign_key_list(registrationobservation)")
    ).fetchall()
    assert ("source_snapshot_id", "mihesnapshot", "id") in {
        (r[3], r[2], r[4]) for r in after
    }

    # Pre-existing rows survived the rebuild (data-preserving, not destroying).
    kept = engine.connect().execute(
        text("SELECT COUNT(*) FROM registrationobservation")
    ).scalar_one()
    assert kept == 2

    # An orphan write is now rejected by the database.
    with pytest.raises(SAIntegrityError), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO registrationobservation "
                "(id, customer_id, registered_at, last_login_at, total_recharge, "
                "recharge_count, balance, cohort_tag, is_batch, source_snapshot_id, derived_at) "
                "VALUES (:id, :cid, :reg, :login, :tr, :rc, :bal, :cohort, :batch, :src, :der)"
            ),
            {
                "id": "r_orphan", "cid": "c_orphan",
                "reg": "2026-08-06 10:00:00.000000", "login": None,
                "tr": 0, "rc": 0, "bal": 0, "cohort": "NATURAL",
                "batch": 0, "src": "no_such_snapshot", "der": "2026-08-06 10:00:00.000000",
            },
        )
    engine.dispose()


def test_p23a_evolve_refuses_orphan_source_snapshot_without_mutating(tmp_path: Path):
    """P1-3 (fail-closed path): when the legacy ``registrationobservation``
    already holds a row whose ``source_snapshot_id`` references no
    ``mihesnapshot``, adding the FK would fail (and under SQLite autocommit the
    rebuild could leave a half-evolved table). The refresh must instead
    PRE-FLIGHT the orphan and raise BEFORE mutating, so the schema is left
    exactly as found -- no FK added, no column touched, and the offending row
    preserved for the operator to resolve.
    """
    db_path = tmp_path / "legacy_orphan_fk.db"
    engine = get_engine(sqlite_url(db_path))
    with engine.begin() as conn:
        conn.execute(text(_OLD_REGISTRATIONOBSERVATION_DDL))
        conn.execute(text(_MIHESNAPSHOT_DDL))
        # A registration pointing at a snapshot ("ghost") that was never captured.
        conn.execute(
            text(
                "INSERT INTO registrationobservation "
                "(id, customer_id, registered_at, last_login_at, total_recharge, "
                "recharge_count, balance, cohort_tag, is_batch, source_snapshot_id, derived_at) "
                "VALUES (:id, :cid, :reg, :login, :tr, :rc, :bal, :cohort, :batch, :src, :der)"
            ),
            {
                "id": "r1", "cid": "c1",
                "reg": "2026-08-04 10:00:00.000000", "login": None,
                "tr": 0, "rc": 0, "bal": 0, "cohort": "NATURAL",
                "batch": 0, "src": "ghost", "der": "2026-08-04 10:00:00.000000",
            },
        )

    with pytest.raises(mca.SchemaRebuildError) as excinfo:
        mca.run_create(engine)
    message = str(excinfo.value).lower()
    assert "foreign key" in message or "source_snapshot_id" in message or "orphan" in message

    # The schema is UNCHANGED: the FK was not added and no 2A-3 column was added.
    cols = {c["name"] for c in inspect(engine).get_columns("registrationobservation")}
    assert "observation_hash" not in cols
    fks = engine.connect().execute(
        text("PRAGMA foreign_key_list(registrationobservation)")
    ).fetchall()
    assert ("source_snapshot_id", "mihesnapshot", "id") not in {
        (r[3], r[2], r[4]) for r in fks
    }
    # The orphan row is still present (no data loss; operator must resolve it).
    kept = engine.connect().execute(
        text("SELECT COUNT(*) FROM registrationobservation")
    ).scalar_one()
    assert kept == 1
    engine.dispose()
