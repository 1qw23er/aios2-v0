"""PILOT-2A (2A-2) model + staging-guard contract tests.

This module lives inside the configured ``testpaths`` (``tests/``), so the plain
repo-root run ``python -m pytest -q`` auto-collects it -- contract D4 / FIX 4.
No ``sys.path`` manipulation is needed: ``pyproject.toml`` already puts ``src``
on ``pythonpath``.

Coverage map (PR #136 review requirements):

===========  ==============================================================
FIX 1 / D1   no DIRECT / HIGH person-level registration attribution result
             may exist in the vocabulary OR be persisted.
D2 (struct)  attribution currency is expressed STRUCTURALLY -- an immutable
             ``FinalAttributionDecision`` history plus exactly one
             ``FinalAttributionHead`` row per registration -- so two heads,
             zero heads and forged supersession are unrepresentable rather
             than merely guarded (see ``test_d2s_*``).
FIX 3 / D3   the staging guard compares exact normalised filesystem paths;
             substring / marker matching is gone; every failure is closed.
FIX 4 / D4   this module is auto-collected by the default pytest run.
C1           ``CLICK_ASSOCIATED`` is never a persisted attribution result.
C2           proposal <-> registration consistency at the write boundary.
C3           the authorised staging path is derived from ``human_env``.
C4           collection proof (``test_fix4_*``).
===========  ==============================================================

Every test is hermetic: temporary SQLite files only. The real staging database
(``uat_ool_v0/staging/human_uat.db``) is never opened and no global
``AIOS_DATABASE_URL`` is mutated at import time.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, select

from aios.db import get_engine
from aios.pilot2 import migrations_create_all as mca
from aios.pilot2.attribution_head import (
    AttributionHeadError,
    current_decision,
    decision_history,
    finalize_attribution,
    replace_attribution,
    unattached_decisions,
)
from aios.pilot2.models import (
    D2_TRIGGER_NAMES,
    FORBIDDEN_ATTRIBUTION_TOKENS,
    FORBIDDEN_FINAL_ATTRIBUTION_TOKENS,
    AttributionLevel,
    AttributionProposal,
    CohortTag,
    ContentTaxonomyTerm,
    FinalAttributionDecision,
    MiheEndpoint,
    MiheSnapshot,
    RegistrationAttributionLevel,
    RegistrationObservation,
    pilot2_metadata,
)
from aios.pilot2.vocabulary import (
    CONTENT_TAXONOMY,
    TaxonomyDimension,
    validate_content_record,
    validate_taxonomy,
)

EXPECTED_TABLES = {
    "mihesnapshot",
    "publicationevent",
    "clickevent",
    "platformmetricsnapshot",
    "registrationobservation",
    "attributionproposal",
    "finalattributiondecision",
    "finalattributionhead",
    "contenttaxonomyterm",
    "experimentregistry",
}

LEGAL_LEVEL = RegistrationAttributionLevel.EXPERIMENT_ASSOCIATED
LEGAL_FINAL_SQL = LEGAL_LEVEL.name

# SQLModel persists ``datetime`` as ``YYYY-MM-DD HH:MM:SS.ffffff`` on SQLite; raw-SQL
# fixtures must use the same representation so the ORM can read the rows back.
TS = "2026-08-06 00:00:00.000000"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def sqlite_url(path: Path) -> str:
    """SQLAlchemy URL for a local SQLite file (correct on POSIX and Windows)."""
    return f"sqlite:///{path.as_posix()}"


def make_pilot2_db(path: Path):
    """Create every pilot2 table (plus triggers) in a fresh SQLite file."""
    engine = get_engine(sqlite_url(path))
    pilot2_metadata.create_all(engine)
    return engine


def seed_chain(engine, *, reg_id: str, proposals: dict[str, AttributionLevel]) -> None:
    """Seed one snapshot, one registration observation and N proposals."""
    snap_id = f"msnap_{reg_id}"
    with Session(engine) as session:
        session.add(
            MiheSnapshot(
                id=snap_id,
                endpoint=MiheEndpoint.CUSTOMERS,
                raw_payload={},
                raw_hash=f"hash_{reg_id}",
            )
        )
        session.add(
            RegistrationObservation(
                id=reg_id,
                customer_id=f"cust_{reg_id}",
                registered_at=datetime(2026, 8, 5, 1, 2, 3),
                source_snapshot_id=snap_id,
            )
        )
        session.commit()
    with Session(engine) as session:
        for proposal_id, level in proposals.items():
            session.add(
                AttributionProposal(
                    id=proposal_id,
                    registration_observation_id=reg_id,
                    level=level,
                    input_hash=f"ih_{proposal_id}",
                )
            )
        session.commit()


def raw_conn(path: Path, *, busy_timeout_ms: int = 3000) -> sqlite3.Connection:
    """Autocommit sqlite3 connection with FK enforcement (explicit BEGIN needed)."""
    conn = sqlite3.connect(str(path), timeout=busy_timeout_ms / 1000, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    return conn


def enable_wal(path: Path) -> None:
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute("PRAGMA journal_mode=WAL")


def head_decision(conn: sqlite3.Connection, reg_id: str) -> str | None:
    """The single current-head decision id for a registration (or None)."""
    row = conn.execute(
        "SELECT decision_id FROM finalattributionhead WHERE registration_observation_id = ?",
        (reg_id,),
    ).fetchone()
    return None if row is None else row[0]


def head_count(conn: sqlite3.Connection, reg_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM finalattributionhead WHERE registration_observation_id = ?",
        (reg_id,),
    ).fetchone()[0]


def decision_ids(conn: sqlite3.Connection, reg_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT id FROM finalattributiondecision WHERE registration_observation_id = ?",
        (reg_id,),
    ).fetchall()
    return sorted(row[0] for row in rows)


def supersedes_of(conn: sqlite3.Connection, decision_id: str) -> str | None:
    return conn.execute(
        "SELECT supersedes_decision_id FROM finalattributiondecision WHERE id = ?",
        (decision_id,),
    ).fetchone()[0]


INSERT_DECISION_SQL = (
    "INSERT INTO finalattributiondecision "
    "(id, proposal_id, registration_observation_id, level, supersedes_decision_id, "
    " decided_at, decided_by, reason) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)

INSERT_DECISION_TEXT = text(
    "INSERT INTO finalattributiondecision "
    "(id, proposal_id, registration_observation_id, level, supersedes_decision_id, "
    " decided_at, decided_by, reason) "
    f"VALUES (:id, :proposal_id, :reg_id, :level, :supersedes, '{TS}', 'test', NULL)"
)

INSERT_HEAD_SQL = (
    "INSERT INTO finalattributionhead "
    "(registration_observation_id, decision_id, updated_at) VALUES (?, ?, ?)"
)

INSERT_PROPOSAL_TEXT = text(
    "INSERT INTO attributionproposal "
    "(id, registration_observation_id, content_id, level, evidence_json, "
    " input_hash, computed_at) "
    "VALUES (:id, :reg_id, NULL, :level, '{}', :input_hash, :computed_at)"
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    """Shared read-mostly pilot2 database for the descriptive (T*) tests."""
    db_path = tmp_path_factory.mktemp("pilot2_shared") / "pilot2.db"
    eng = make_pilot2_db(db_path)
    mca.seed_taxonomy(eng)
    with Session(eng) as session:
        session.add(
            MiheSnapshot(
                id="msnap_fixed",
                endpoint=MiheEndpoint.CUSTOMERS,
                raw_payload={},
                raw_hash="seed_ms_fixed",
                total_count=12,
            )
        )
        session.commit()
    return eng


@pytest.fixture
def pilot2_db(tmp_path):
    """A private, freshly-created pilot2 database (engine, path) per test."""
    db_path = tmp_path / "pilot2_case.db"
    return make_pilot2_db(db_path), db_path


_AUTHORITY_TEMPLATE = '''\
"""Synthetic stand-in for the authoritative human_env module."""
from pathlib import Path

UAT_DB_PATH = Path(r"{db_path}")


def bootstrap() -> None:
    return None
'''


@pytest.fixture
def synthetic_authority(tmp_path) -> Path:
    """A throw-away ``human_env`` module + its UAT_DB_PATH, for off-box testing.

    The guard's *decision* is exercised via ``is_authorized_staging_url`` /
    ``should_create(staging_path=...)``. The env-var override that previously
    selected WHICH authority is gone (FIX 6 / D3): the authority is now the
    repo-fixed module and cannot be substituted by a caller, so the test only
    injects the staging *path*, never a replacement authority module.
    """
    staging_dir = tmp_path / "uat_ool_v0" / "staging"
    staging_dir.mkdir(parents=True)
    db_path = staging_dir / "human_uat.db"
    module_file = tmp_path / "human_env.py"
    module_file.write_text(_AUTHORITY_TEMPLATE.format(db_path=str(db_path)), encoding="utf-8")
    return db_path


# ===========================================================================
# T-series: schema / vocabulary descriptive coverage (carried over from 2A-2)
# ===========================================================================
def test_t1_tables_created(engine):
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES


def test_t2_mihe_snapshot_idempotent(engine):
    with Session(engine) as session:
        session.add(
            MiheSnapshot(endpoint=MiheEndpoint.CUSTOMERS, raw_payload={"a": 1}, raw_hash="hash_abc")
        )
        session.commit()
    with Session(engine) as session:
        session.add(
            MiheSnapshot(endpoint=MiheEndpoint.CUSTOMERS, raw_payload={"a": 1}, raw_hash="hash_abc")
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_t3_registration_observation_deterministic(engine):
    with Session(engine) as session:
        session.add(
            RegistrationObservation(
                customer_id="cuid_123",
                registered_at=datetime(2026, 8, 5, 1, 2, 3),
                cohort_tag=CohortTag.NATURAL,
                is_batch=False,
                source_snapshot_id="msnap_fixed",
            )
        )
        session.commit()
        rid = session.exec(
            select(RegistrationObservation.id).where(
                RegistrationObservation.customer_id == "cuid_123"
            )
        ).one()
    with Session(engine) as session:
        observation = session.get(RegistrationObservation, rid)
        assert observation.customer_id == "cuid_123"
        assert observation.cohort_tag == CohortTag.NATURAL
        assert observation.is_batch is False


def test_t4_unknown_batch_excluded(engine):
    with Session(engine) as session:
        session.add(
            RegistrationObservation(
                customer_id="natural1",
                registered_at=datetime(2026, 7, 9),
                cohort_tag=CohortTag.NATURAL,
                is_batch=False,
                source_snapshot_id="msnap_fixed",
            )
        )
        session.add(
            RegistrationObservation(
                customer_id="batch1",
                registered_at=datetime(2026, 8, 5, 1, 0, 1),
                cohort_tag=CohortTag.UNKNOWN_BATCH_COHORT,
                is_batch=True,
                source_snapshot_id="msnap_fixed",
            )
        )
        session.commit()
    with Session(engine) as session:
        eligible = session.exec(
            select(RegistrationObservation).where(
                RegistrationObservation.is_batch == False  # noqa: E712
            )
        ).all()
        ids = {row.customer_id for row in eligible}
        assert "natural1" in ids
        assert "batch1" not in ids


def test_t5_attribution_proposal_recompute_idempotent(engine):
    with Session(engine) as session:
        if not session.get(RegistrationObservation, "regob_fixed"):
            session.add(
                RegistrationObservation(
                    id="regob_fixed",
                    customer_id="c1",
                    registered_at=datetime(2026, 8, 5),
                    source_snapshot_id="msnap_fixed",
                )
            )
            session.commit()
    input_hash = "inputhash_v1"
    with Session(engine) as session:
        session.add(
            AttributionProposal(
                registration_observation_id="regob_fixed",
                level=AttributionLevel.EXPERIMENT_ASSOCIATED,
                input_hash=input_hash,
            )
        )
        session.commit()
    with Session(engine) as session:
        session.add(
            AttributionProposal(
                registration_observation_id="regob_fixed",
                level=AttributionLevel.EXPERIMENT_ASSOCIATED,
                input_hash=input_hash,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        proposal = session.exec(
            select(AttributionProposal).where(AttributionProposal.input_hash == input_hash)
        ).one()
        assert proposal.level == AttributionLevel.EXPERIMENT_ASSOCIATED


def test_t7_fail_closed_unattributed(engine):
    assert AttributionLevel.UNATTRIBUTED.value == "unattributed"
    assert RegistrationAttributionLevel.UNATTRIBUTED.value == "unattributed"
    with Session(engine) as session:
        if not session.get(RegistrationObservation, "regob_unc"):
            session.add(
                RegistrationObservation(
                    id="regob_unc",
                    customer_id="c3",
                    registered_at=datetime(2026, 8, 7),
                    source_snapshot_id="msnap_fixed",
                )
            )
            session.commit()
        # No proposal yet -> the convention is UNATTRIBUTED (solver fills 2A-4).
        assert (
            session.exec(
                select(AttributionProposal).where(
                    AttributionProposal.registration_observation_id == "regob_unc"
                )
            ).first()
            is None
        )


def test_t8_taxonomy_validation_and_seeded(engine):
    assert validate_taxonomy(TaxonomyDimension.HOOK, "cost") is True
    assert validate_taxonomy(TaxonomyDimension.HOOK, "bogus") is False
    problems = validate_content_record(
        {
            "track": "ip",
            "audience": "apparel",
            "use_case": "scene_image",
            "value_prop": "save_money",
            "format": "before_after",
            "hook": "bogus",
        }
    )
    assert any("hook" in problem for problem in problems)
    with Session(engine) as session:
        seeded = len(session.exec(select(ContentTaxonomyTerm)).all())
        assert seeded == sum(len(values) for values in CONTENT_TAXONOMY.values())


def test_t9_knowledge_tags_untouched():
    import aios.pilot2.vocabulary as voc
    from aios.knowledge_tags import CANONICAL_KNOWLEDGE_TAGS

    before = set(CANONICAL_KNOWLEDGE_TAGS)
    assert "CANONICAL_KNOWLEDGE_TAGS" not in dir(voc)
    assert before == set(CANONICAL_KNOWLEDGE_TAGS)
    flat = {value for values in CONTENT_TAXONOMY.values() for value in values}
    assert not (flat & before)


def test_t10_no_external_tables_and_independent_metadata():
    from sqlmodel import SQLModel

    assert set(pilot2_metadata.tables.keys()) == EXPECTED_TABLES
    assert pilot2_metadata is not SQLModel.metadata
    assert EXPECTED_TABLES.isdisjoint(set(SQLModel.metadata.tables.keys()))


# ===========================================================================
# Storage-representation pin: SQLModel persists enum member NAMES
# ===========================================================================
def test_enum_columns_persist_member_names(pilot2_db):
    """The SQL-level gates compare NAMES; pin that representation.

    If an ORM upgrade ever switched to persisting ``StrEnum`` values, every
    CHECK / partial index / trigger literal in ``models.py`` would silently stop
    matching. This test fails loudly in that case.
    """
    eng, _ = pilot2_db
    seed_chain(eng, reg_id="regob_repr", proposals={"aprop_repr": AttributionLevel.AMBIGUOUS})
    with Session(eng) as session:
        session.add(
            FinalAttributionDecision(
                id="fdec_repr",
                proposal_id="aprop_repr",
                registration_observation_id="regob_repr",
                level=RegistrationAttributionLevel.EXPERIMENT_ASSOCIATED,
                decided_by="owner",
            )
        )
        session.commit()
    with eng.connect() as conn:
        level = conn.execute(text("SELECT level FROM attributionproposal")).scalar_one()
        assert level == "AMBIGUOUS"
        final_level = conn.execute(
            text("SELECT level FROM finalattributiondecision")
        ).scalar_one()
        assert final_level == "EXPERIMENT_ASSOCIATED"


# ===========================================================================
# FIX 1 / D1 -- no DIRECT / HIGH person-level registration attribution
# ===========================================================================
def test_fix1_direct_levels_absent_from_vocabulary():
    proposal_names = {member.name for member in AttributionLevel}
    final_names = {member.name for member in RegistrationAttributionLevel}
    for banned in ("VERIFIED_DIRECT", "DIRECT", "HIGH"):
        assert banned not in proposal_names
        assert banned not in final_names
    assert final_names == {"EXPERIMENT_ASSOCIATED", "AMBIGUOUS", "UNATTRIBUTED"}


@pytest.mark.parametrize("banned", FORBIDDEN_FINAL_ATTRIBUTION_TOKENS)
def test_fix1_persisting_direct_final_attribution_is_rejected(pilot2_db, banned):
    """Raw SQL cannot smuggle a DIRECT / HIGH result past the DB boundary."""
    eng, _ = pilot2_db
    seed_chain(eng, reg_id="regob_d1", proposals={"aprop_d1": AttributionLevel.AMBIGUOUS})
    with eng.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            INSERT_DECISION_TEXT,
            {
                "id": f"fdec_{banned.lower()}",
                "proposal_id": "aprop_d1",
                "reg_id": "regob_d1",
                "level": banned,
                "supersedes": None,
            },
        )


@pytest.mark.parametrize("banned", FORBIDDEN_ATTRIBUTION_TOKENS)
def test_fix1_persisting_direct_proposal_is_rejected(pilot2_db, banned):
    eng, _ = pilot2_db
    seed_chain(eng, reg_id="regob_d1p", proposals={"aprop_d1p": AttributionLevel.AMBIGUOUS})
    with eng.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            INSERT_PROPOSAL_TEXT,
            {
                "id": f"aprop_{banned.lower()}",
                "reg_id": "regob_d1p",
                "level": banned,
                "input_hash": f"ih_{banned.lower()}",
                "computed_at": "2026-08-06T00:00:00",
            },
        )


# ===========================================================================
# C1 -- CLICK_ASSOCIATED is click evidence, never a registration result
# ===========================================================================
def test_c1_click_associated_is_proposal_only():
    assert "CLICK_ASSOCIATED" in {member.name for member in AttributionLevel}
    assert "CLICK_ASSOCIATED" not in {member.name for member in RegistrationAttributionLevel}


def test_c1_click_associated_final_attribution_rejected(pilot2_db):
    eng, _ = pilot2_db
    seed_chain(
        eng,
        reg_id="regob_c1",
        proposals={"aprop_c1": AttributionLevel.CLICK_ASSOCIATED},
    )
    with eng.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            INSERT_DECISION_TEXT,
            {
                "id": "fdec_c1",
                "proposal_id": "aprop_c1",
                "reg_id": "regob_c1",
                "level": "CLICK_ASSOCIATED",
                "supersedes": None,
            },
        )


# ===========================================================================
# D2 (STRUCTURAL) -- immutable decision history + exactly one current head
#
# The previous ACCEPTED / PROVISIONAL / SUPERSEDED state machine was removed
# after adversarial review reproduced a SPLIT-TRANSACTION attack against it:
# commit a PROVISIONAL successor in transaction 1, then commit the
# predecessor's ACCEPTED -> SUPERSEDED demotion in transaction 2, leaving a
# committed state with ZERO accepted heads. PROVISIONAL was therefore not a
# transaction-local staging state at all, and no additional trigger can repair
# a representation that weak.
#
# The replacement makes the illegal states UNREPRESENTABLE instead of guarded:
#
#   finalattributiondecision  append-only, immutable decision history. It has
#                             NO status column, so there is no transient state
#                             to commit and no "superseded" flag to forge.
#   finalattributionhead      ONE row per registration -- the registration id
#                             IS the primary key -- pointing at the current
#                             decision. Two heads is a PK violation in any
#                             transaction, committed or not.
#
# Replacement = a single atomic transaction that INSERTs the successor decision
# and MOVES the one head pointer. There is no demote step, so no window in
# which zero heads exist, and a successor that is committed without moving the
# pointer simply is not current.
# ===========================================================================
def _bootstrap_head(
    engine,
    *,
    reg_id: str,
    proposals: dict[str, AttributionLevel],
    proposal_id: str | None = None,
    reason: str | None = None,
) -> str:
    """Seed snapshot/registration/proposals and finalize the FIRST decision."""
    seed_chain(engine, reg_id=reg_id, proposals=proposals)
    return finalize_attribution(
        engine,
        registration_observation_id=reg_id,
        proposal_id=proposal_id or next(iter(proposals)),
        level=LEGAL_LEVEL,
        decided_by="owner",
        reason=reason,
    )


# --- A1 -------------------------------------------------------------------
def test_d2s_a1_no_provisional_or_transient_state_can_be_committed(pilot2_db):
    """A1: no PROVISIONAL / independent transient state exists to be committed."""
    eng, _ = pilot2_db
    models = importlib.import_module("aios.pilot2.models")
    package = importlib.import_module("aios.pilot2")

    # The state machine is gone from the model layer and from the public API.
    for removed in ("FinalAttribution", "FinalAttributionStatus"):
        assert not hasattr(models, removed), f"{removed} must be removed"
        assert not hasattr(package, removed), f"{removed} must not be exported"
    assert "finalattribution" not in pilot2_metadata.tables

    inspector = inspect(eng)
    assert "finalattribution" not in inspector.get_table_names()
    columns = {col["name"] for col in inspector.get_columns("finalattributiondecision")}
    assert "status" not in columns, "a decision must not carry a mutable status"
    assert "superseded_by" not in columns, "supersession is expressed by the successor"

    # And the vocabulary itself is absent from the whole pilot2 package source.
    package_dir = Path(models.__file__).resolve().parent
    sources = "\n".join(p.read_text(encoding="utf-8") for p in sorted(package_dir.glob("*.py")))
    assert "PROVISIONAL" not in sources


# --- A2 -------------------------------------------------------------------
def test_d2s_a2_proposal_a_finalized_against_registration_b_is_rejected(pilot2_db):
    """A2: cross-registration protection survives the redesign (D2 / C2)."""
    eng, _ = pilot2_db
    seed_chain(eng, reg_id="regob_a2a", proposals={"aprop_a2a": AttributionLevel.AMBIGUOUS})
    seed_chain(eng, reg_id="regob_a2b", proposals={"aprop_a2b": AttributionLevel.AMBIGUOUS})

    with eng.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            INSERT_DECISION_TEXT,
            {
                "id": "fdec_a2",
                "proposal_id": "aprop_a2a",  # proposal belongs to registration A
                "reg_id": "regob_a2b",  # ... finalized against registration B
                "level": LEGAL_FINAL_SQL,
                "supersedes": None,
            },
        )

    with pytest.raises((AttributionHeadError, IntegrityError)):
        finalize_attribution(
            eng,
            registration_observation_id="regob_a2b",
            proposal_id="aprop_a2a",
            level=LEGAL_LEVEL,
            decided_by="owner",
        )
    assert current_decision(eng, "regob_a2b") is None


# --- A3 -------------------------------------------------------------------
def test_d2s_a3_second_head_for_one_registration_is_impossible(pilot2_db):
    """A3: registration identity is the head PK, so two heads cannot exist."""
    eng, db_path = pilot2_db
    head_id = _bootstrap_head(
        eng,
        reg_id="regob_a3",
        proposals={
            "aprop_a3_0": AttributionLevel.AMBIGUOUS,
            "aprop_a3_1": AttributionLevel.AMBIGUOUS,
        },
        proposal_id="aprop_a3_0",
    )
    eng.dispose()

    with closing(raw_conn(db_path)) as conn:
        # A rival decision may be appended -- history is open, currency is not.
        conn.execute(
            INSERT_DECISION_SQL,
            ("fdec_a3_rival", "aprop_a3_1", "regob_a3", LEGAL_FINAL_SQL, None, TS, "rogue", None),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(INSERT_HEAD_SQL, ("regob_a3", "fdec_a3_rival", TS))
        assert head_count(conn, "regob_a3") == 1
        assert head_decision(conn, "regob_a3") == head_id


def test_d2s_a3b_head_cannot_point_at_another_registrations_decision(pilot2_db):
    """The head carries a composite FK, so it can only cite its own decisions."""
    eng, db_path = pilot2_db
    _bootstrap_head(eng, reg_id="regob_a3x", proposals={"aprop_a3x": AttributionLevel.AMBIGUOUS})
    other = _bootstrap_head(
        eng, reg_id="regob_a3y", proposals={"aprop_a3y": AttributionLevel.AMBIGUOUS}
    )
    eng.dispose()

    with closing(raw_conn(db_path)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE finalattributionhead SET decision_id = ? "
            "WHERE registration_observation_id = ?",
            (other, "regob_a3x"),
        )


# --- A4 -------------------------------------------------------------------
def test_d2s_a4_replacement_is_a_single_atomic_transaction(pilot2_db):
    """A4: insert successor + move head, committed together, exactly one head."""
    eng, db_path = pilot2_db
    first = _bootstrap_head(
        eng,
        reg_id="regob_a4",
        proposals={
            "aprop_a4_0": AttributionLevel.AMBIGUOUS,
            "aprop_a4_1": AttributionLevel.AMBIGUOUS,
        },
        proposal_id="aprop_a4_0",
    )
    second = replace_attribution(
        eng,
        registration_observation_id="regob_a4",
        proposal_id="aprop_a4_1",
        level=RegistrationAttributionLevel.AMBIGUOUS,
        decided_by="owner",
        reason="new evidence",
    )
    assert second != first
    eng.dispose()

    with closing(raw_conn(db_path)) as conn:
        assert head_count(conn, "regob_a4") == 1
        assert head_decision(conn, "regob_a4") == second
        assert decision_ids(conn, "regob_a4") == sorted([first, second])
        assert supersedes_of(conn, second) == first
        assert supersedes_of(conn, first) is None


# --- A5 -------------------------------------------------------------------
def test_d2s_a5_failed_replacement_rolls_back_and_keeps_the_old_head(pilot2_db):
    """A5: a rejected replacement leaves the previous head and no orphan row."""
    eng, db_path = pilot2_db
    first = _bootstrap_head(
        eng, reg_id="regob_a5", proposals={"aprop_a5": AttributionLevel.AMBIGUOUS}
    )
    seed_chain(eng, reg_id="regob_a5o", proposals={"aprop_a5o": AttributionLevel.AMBIGUOUS})

    with pytest.raises((AttributionHeadError, IntegrityError)):
        replace_attribution(
            eng,
            registration_observation_id="regob_a5",
            proposal_id="aprop_a5o",  # foreign proposal -> composite FK violation
            level=RegistrationAttributionLevel.AMBIGUOUS,
            decided_by="owner",
        )
    eng.dispose()

    with closing(raw_conn(db_path)) as conn:
        assert head_count(conn, "regob_a5") == 1
        assert head_decision(conn, "regob_a5") == first
        assert decision_ids(conn, "regob_a5") == [first]


# --- A6 -------------------------------------------------------------------
def test_d2s_a6_committed_successor_without_head_move_is_not_current(pilot2_db):
    """A6: half of the split-transaction attack -- and it changes nothing.

    "Changes nothing" is asserted in both directions, because review found the
    second one missing: the orphan must not change the current answer, and it
    must not change what is still POSSIBLE afterwards (see A11).
    """
    eng, db_path = pilot2_db
    first = _bootstrap_head(
        eng,
        reg_id="regob_a6",
        proposals={
            "aprop_a6_0": AttributionLevel.AMBIGUOUS,
            "aprop_a6_1": AttributionLevel.AMBIGUOUS,
        },
        proposal_id="aprop_a6_0",
    )
    eng.dispose()

    with closing(raw_conn(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            INSERT_DECISION_SQL,
            ("fdec_a6_new", "aprop_a6_1", "regob_a6", LEGAL_FINAL_SQL, first, TS, "rogue", None),
        )
        conn.execute("COMMIT")
        assert head_count(conn, "regob_a6") == 1
        assert head_decision(conn, "regob_a6") == first

    engine = get_engine(sqlite_url(db_path))
    try:
        assert current_decision(engine, "regob_a6").id == first
        # The orphan is not history either -- it was never current.
        assert [item.id for item in decision_history(engine, "regob_a6")] == [first]
        # It is not hidden, though: the audit query reports it verbatim.
        assert [item.id for item in unattached_decisions(engine, "regob_a6")] == ["fdec_a6_new"]
    finally:
        engine.dispose()


# --- A11 (P1-1 regression) -------------------------------------------------
def test_d2s_a11_orphan_successor_cannot_strand_the_current_head(pilot2_db):
    """A11: an unattached successor must never make a head unreplaceable.

    Review finding P1-1. The decision table used to carry a partial UNIQUE index
    on ``supersedes_decision_id`` ("one successor per predecessor"). That turned
    a supersession CLAIM into an exclusive RESERVATION: any writer could append a
    successor of the current head without moving the head, and from then on the
    authorised ``replace_attribution`` died on the unique index -- before the
    head compare-and-set -- so the current decision could never be replaced
    again. Availability of the authorised path is an integrity property, so the
    reservation is gone and this test pins the behaviour down.

    Three things must hold at once, and the third is the one that keeps the fix
    honest: no unauthorised decision may be promoted along the way.
    """
    eng, db_path = pilot2_db
    first = _bootstrap_head(
        eng,
        reg_id="regob_a11",
        proposals={
            "aprop_a11_0": AttributionLevel.AMBIGUOUS,
            "aprop_a11_1": AttributionLevel.AMBIGUOUS,
            "aprop_a11_2": AttributionLevel.AMBIGUOUS,
            "aprop_a11_3": AttributionLevel.AMBIGUOUS,
        },
        proposal_id="aprop_a11_0",
    )
    eng.dispose()

    # Two rogue writers each squat on the current head, in separate committed
    # transactions -- the strongest form of the attack.
    with closing(raw_conn(db_path)) as conn:
        for rogue_id, proposal in (
            ("fdec_a11_squat_a", "aprop_a11_1"),
            ("fdec_a11_squat_b", "aprop_a11_2"),
        ):
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                INSERT_DECISION_SQL,
                (rogue_id, proposal, "regob_a11", LEGAL_FINAL_SQL, first, TS, "rogue", None),
            )
            conn.execute("COMMIT")
        assert head_decision(conn, "regob_a11") == first

    engine = get_engine(sqlite_url(db_path))
    try:
        # 1. the authorised path still works -- the head is NOT stranded.
        second = replace_attribution(
            engine,
            registration_observation_id="regob_a11",
            proposal_id="aprop_a11_3",
            level=RegistrationAttributionLevel.UNATTRIBUTED,
            decided_by="owner",
            reason="authorised replacement after the squat",
        )
        # 2. the head advanced to the AUTHORISED decision ...
        current = current_decision(engine, "regob_a11")
        assert current.id == second
        assert current.decided_by == "owner"
        # 3. ... and neither squatter was promoted, nor entered the history.
        assert [item.id for item in decision_history(engine, "regob_a11")] == [first, second]
        assert [item.id for item in unattached_decisions(engine, "regob_a11")] == [
            "fdec_a11_squat_a",
            "fdec_a11_squat_b",
        ]
    finally:
        engine.dispose()

    with closing(raw_conn(db_path)) as conn:
        assert head_count(conn, "regob_a11") == 1
        assert head_decision(conn, "regob_a11") == second
        # Once the head has moved past ``first``, its squatters are frozen out:
        # the forward-only trigger only accepts a successor of the CURRENT head.
        for rogue_id in ("fdec_a11_squat_a", "fdec_a11_squat_b"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE finalattributionhead SET decision_id = ? "
                    "WHERE registration_observation_id = ?",
                    (rogue_id, "regob_a11"),
                )
        assert head_decision(conn, "regob_a11") == second


def test_d2s_a11b_unattached_decisions_is_empty_in_normal_operation(pilot2_db):
    """The audit query is silent unless somebody wrote outside the service."""
    eng, _ = pilot2_db
    _bootstrap_head(
        eng,
        reg_id="regob_a11n",
        proposals={
            "aprop_a11n_0": AttributionLevel.AMBIGUOUS,
            "aprop_a11n_1": AttributionLevel.AMBIGUOUS,
        },
        proposal_id="aprop_a11n_0",
    )
    replace_attribution(
        eng,
        registration_observation_id="regob_a11n",
        proposal_id="aprop_a11n_1",
        level=RegistrationAttributionLevel.AMBIGUOUS,
        decided_by="owner",
    )
    assert unattached_decisions(eng, "regob_a11n") == []


# --- A7 -------------------------------------------------------------------
def test_d2s_a7_no_demotion_can_produce_a_zero_head_committed_state(pilot2_db):
    """A7: the second half of the attack has no operation to perform."""
    eng, db_path = pilot2_db
    first = _bootstrap_head(
        eng,
        reg_id="regob_a7",
        proposals={
            "aprop_a7_0": AttributionLevel.AMBIGUOUS,
            "aprop_a7_1": AttributionLevel.AMBIGUOUS,
        },
        proposal_id="aprop_a7_0",
    )
    eng.dispose()

    with closing(raw_conn(db_path)) as conn:
        # Deleting the head would silently un-finalize a finalized registration.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM finalattributionhead WHERE registration_observation_id = ?",
                ("regob_a7",),
            )
        # Nulling the pointer is not a representable state either.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE finalattributionhead SET decision_id = NULL "
                "WHERE registration_observation_id = ?",
                ("regob_a7",),
            )
        # Nor may the pointer jump to a decision that does not supersede it.
        conn.execute(
            INSERT_DECISION_SQL,
            ("fdec_a7_side", "aprop_a7_1", "regob_a7", LEGAL_FINAL_SQL, None, TS, "rogue", None),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE finalattributionhead SET decision_id = ? "
                "WHERE registration_observation_id = ?",
                ("fdec_a7_side", "regob_a7"),
            )
        assert head_count(conn, "regob_a7") == 1
        assert head_decision(conn, "regob_a7") == first


def test_d2s_a7b_full_split_transaction_attack_is_defeated(pilot2_db):
    """The exact reviewer reproduction, replayed end to end against the new shape."""
    eng, db_path = pilot2_db
    first = _bootstrap_head(
        eng,
        reg_id="regob_a7s",
        proposals={
            "aprop_a7s_0": AttributionLevel.AMBIGUOUS,
            "aprop_a7s_1": AttributionLevel.AMBIGUOUS,
        },
        proposal_id="aprop_a7s_0",
    )
    eng.dispose()

    # Transaction 1: commit the "successor" on its own.
    with closing(raw_conn(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            INSERT_DECISION_SQL,
            (
                "fdec_a7s_new",
                "aprop_a7s_1",
                "regob_a7s",
                LEGAL_FINAL_SQL,
                first,
                TS,
                "rogue",
                None,
            ),
        )
        conn.execute("COMMIT")

    # Transaction 2: try to retire the predecessor separately. There is no
    # status to demote and the head row cannot be removed, so every available
    # verb fails and the registration keeps exactly one head throughout.
    with closing(raw_conn(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM finalattributionhead WHERE registration_observation_id = ?",
                ("regob_a7s",),
            )
        conn.execute("ROLLBACK")
        assert head_count(conn, "regob_a7s") == 1
        assert head_decision(conn, "regob_a7s") == first


# --- A8 -------------------------------------------------------------------
def test_d2s_a8_history_rows_cannot_be_rewritten_or_forged(pilot2_db):
    """A8: forging a superseded predecessor is structurally impossible."""
    eng, db_path = pilot2_db
    first = _bootstrap_head(
        eng, reg_id="regob_a8", proposals={"aprop_a8": AttributionLevel.AMBIGUOUS}, reason="orig"
    )
    eng.dispose()

    with closing(raw_conn(db_path)) as conn:
        # There is no status column, so "mark it SUPERSEDED" has no target.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(finalattributiondecision)")}
        assert "status" not in columns

        # Decisions are immutable ...
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE finalattributiondecision SET reason = 'rewritten' WHERE id = ?",
                (first,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE finalattributiondecision SET supersedes_decision_id = ? WHERE id = ?",
                (first, first),
            )
        # ... and append-only.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM finalattributiondecision WHERE id = ?", (first,))

        row = conn.execute(
            "SELECT reason, supersedes_decision_id FROM finalattributiondecision WHERE id = ?",
            (first,),
        ).fetchone()
        assert row == ("orig", None)


def test_d2s_a8b_self_supersession_is_rejected(pilot2_db):
    eng, db_path = pilot2_db
    _bootstrap_head(eng, reg_id="regob_a8s", proposals={"aprop_a8s": AttributionLevel.AMBIGUOUS})
    eng.dispose()
    with closing(raw_conn(db_path)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            INSERT_DECISION_SQL,
            (
                "fdec_a8s",
                "aprop_a8s",
                "regob_a8s",
                LEGAL_FINAL_SQL,
                "fdec_a8s",
                TS,
                "rogue",
                None,
            ),
        )


def test_d2s_a8c_cross_registration_supersession_is_rejected(pilot2_db):
    eng, db_path = pilot2_db
    foreign = _bootstrap_head(
        eng, reg_id="regob_a8f", proposals={"aprop_a8f": AttributionLevel.AMBIGUOUS}
    )
    _bootstrap_head(eng, reg_id="regob_a8g", proposals={"aprop_a8g": AttributionLevel.AMBIGUOUS})
    eng.dispose()
    with closing(raw_conn(db_path)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            INSERT_DECISION_SQL,
            ("fdec_a8g", "aprop_a8g", "regob_a8g", LEGAL_FINAL_SQL, foreign, TS, "rogue", None),
        )


def test_d2s_a8d_the_effective_history_cannot_fork(pilot2_db):
    """Rival claims on one predecessor are allowed; a forked HISTORY is not.

    This test used to assert the opposite -- that the second claim was rejected
    by a unique index. Review finding P1-1 showed that index was a
    denial-of-service vector (see A11), so exclusivity moved to where it costs
    nothing: the single head pointer. Rival rows may exist, but currency passes
    through one row, one UPDATE at a time, and only ever forwards, so the
    reconstructed history is still a single line.
    """
    eng, db_path = pilot2_db
    first = _bootstrap_head(
        eng,
        reg_id="regob_a8k",
        proposals={
            "aprop_a8k_0": AttributionLevel.AMBIGUOUS,
            "aprop_a8k_1": AttributionLevel.AMBIGUOUS,
            "aprop_a8k_2": AttributionLevel.AMBIGUOUS,
        },
        proposal_id="aprop_a8k_0",
    )
    eng.dispose()
    with closing(raw_conn(db_path)) as conn:
        # Both rival claims are accepted by the table ...
        for rival, proposal in (
            ("fdec_a8k_1", "aprop_a8k_1"),
            ("fdec_a8k_2", "aprop_a8k_2"),
        ):
            conn.execute(
                INSERT_DECISION_SQL,
                (rival, proposal, "regob_a8k", LEGAL_FINAL_SQL, first, TS, "rogue", None),
            )
        assert head_decision(conn, "regob_a8k") == first

        # ... and the head can adopt AT MOST ONE of them. After that the other
        # is unreachable forever: it supersedes a decision that is no longer
        # current, which the forward-only trigger refuses.
        conn.execute(
            "UPDATE finalattributionhead SET decision_id = ? "
            "WHERE registration_observation_id = ?",
            ("fdec_a8k_1", "regob_a8k"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE finalattributionhead SET decision_id = ? "
                "WHERE registration_observation_id = ?",
                ("fdec_a8k_2", "regob_a8k"),
            )
        assert head_count(conn, "regob_a8k") == 1
        assert head_decision(conn, "regob_a8k") == "fdec_a8k_1"

    engine = get_engine(sqlite_url(db_path))
    try:
        # The reading is a single line, and the loser is reported as unattached
        # rather than silently folded into the chain.
        assert [item.id for item in decision_history(engine, "regob_a8k")] == [
            first,
            "fdec_a8k_1",
        ]
        assert [item.id for item in unattached_decisions(engine, "regob_a8k")] == ["fdec_a8k_2"]
    finally:
        engine.dispose()


# --- A9 -------------------------------------------------------------------
def test_d2s_a9_concurrent_replacement_never_leaves_zero_or_two_heads(pilot2_db):
    """A9: two racing replacements -- exactly one wins, one head throughout."""
    eng, db_path = pilot2_db
    seed_chain(
        eng,
        reg_id="regob_a9",
        proposals={
            "aprop_a9_0": AttributionLevel.AMBIGUOUS,
            "aprop_a9_x": AttributionLevel.AMBIGUOUS,
            "aprop_a9_y": AttributionLevel.AMBIGUOUS,
        },
    )
    first = finalize_attribution(
        eng,
        registration_observation_id="regob_a9",
        proposal_id="aprop_a9_0",
        level=LEGAL_LEVEL,
        decided_by="owner",
    )
    eng.dispose()
    enable_wal(db_path)

    barrier = threading.Barrier(2)
    results: dict[str, str | None] = {}

    def racer(tag: str, proposal_id: str) -> None:
        engine = get_engine(sqlite_url(db_path))
        try:
            barrier.wait(timeout=10)
            results[tag] = replace_attribution(
                engine,
                registration_observation_id="regob_a9",
                proposal_id=proposal_id,
                level=RegistrationAttributionLevel.AMBIGUOUS,
                decided_by=tag,
            )
        except Exception:
            results[tag] = None
        finally:
            engine.dispose()

    threads = [
        threading.Thread(target=racer, args=("x", "aprop_a9_x")),
        threading.Thread(target=racer, args=("y", "aprop_a9_y")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "concurrent replacement deadlocked"

    winners = [value for value in results.values() if value is not None]
    assert len(winners) == 1, f"exactly one replacement must win, got {results}"
    with closing(raw_conn(db_path)) as conn:
        assert head_count(conn, "regob_a9") == 1
        assert head_decision(conn, "regob_a9") == winners[0]
        assert decision_ids(conn, "regob_a9") == sorted([first, winners[0]]), (
            "the loser must not have persisted a decision"
        )


# --- A10 ------------------------------------------------------------------
def test_d2s_a10_predecessor_history_remains_queryable(pilot2_db):
    """A10: the full chain is retained, ordered, and readable after replacement."""
    eng, _ = pilot2_db
    first = _bootstrap_head(
        eng,
        reg_id="regob_a10",
        proposals={
            "aprop_a10_0": AttributionLevel.AMBIGUOUS,
            "aprop_a10_1": AttributionLevel.AMBIGUOUS,
            "aprop_a10_2": AttributionLevel.AMBIGUOUS,
        },
        proposal_id="aprop_a10_0",
        reason="first",
    )
    second = replace_attribution(
        eng,
        registration_observation_id="regob_a10",
        proposal_id="aprop_a10_1",
        level=RegistrationAttributionLevel.AMBIGUOUS,
        decided_by="owner",
        reason="second",
    )
    third = replace_attribution(
        eng,
        registration_observation_id="regob_a10",
        proposal_id="aprop_a10_2",
        level=RegistrationAttributionLevel.UNATTRIBUTED,
        decided_by="owner",
        reason="third",
    )

    history = decision_history(eng, "regob_a10")
    assert [item.id for item in history] == [first, second, third]
    assert [item.reason for item in history] == ["first", "second", "third"]
    assert history[0].level is RegistrationAttributionLevel.EXPERIMENT_ASSOCIATED
    assert history[0].proposal_id == "aprop_a10_0"
    assert current_decision(eng, "regob_a10").id == third


# --- authorised-service boundary -----------------------------------------
def test_d2s_finalizing_twice_is_rejected(pilot2_db):
    """A registration is finalized once; every later conclusion is a replacement."""
    eng, _ = pilot2_db
    _bootstrap_head(
        eng,
        reg_id="regob_ft",
        proposals={
            "aprop_ft_0": AttributionLevel.AMBIGUOUS,
            "aprop_ft_1": AttributionLevel.AMBIGUOUS,
        },
        proposal_id="aprop_ft_0",
    )
    with pytest.raises(AttributionHeadError):
        finalize_attribution(
            eng,
            registration_observation_id="regob_ft",
            proposal_id="aprop_ft_1",
            level=LEGAL_LEVEL,
            decided_by="owner",
        )


def test_d2s_replacing_an_unfinalized_registration_is_rejected(pilot2_db):
    """A registration with no head is legal; it just cannot be *replaced*."""
    eng, _ = pilot2_db
    seed_chain(eng, reg_id="regob_nh", proposals={"aprop_nh": AttributionLevel.AMBIGUOUS})
    assert current_decision(eng, "regob_nh") is None
    assert decision_history(eng, "regob_nh") == []
    with pytest.raises(AttributionHeadError):
        replace_attribution(
            eng,
            registration_observation_id="regob_nh",
            proposal_id="aprop_nh",
            level=LEGAL_LEVEL,
            decided_by="owner",
        )


def test_d2s_conflicting_proposals_cannot_both_be_current(pilot2_db):
    """Two conflicting proposals -> at most one may ever be the head."""
    eng, db_path = pilot2_db
    first = _bootstrap_head(
        eng,
        reg_id="regob_cf",
        proposals={
            "aprop_cf_a": AttributionLevel.AMBIGUOUS,
            "aprop_cf_b": AttributionLevel.EXPERIMENT_ASSOCIATED,
        },
        proposal_id="aprop_cf_a",
    )
    with pytest.raises(AttributionHeadError):
        finalize_attribution(
            eng,
            registration_observation_id="regob_cf",
            proposal_id="aprop_cf_b",
            level=LEGAL_LEVEL,
            decided_by="owner",
        )
    eng.dispose()
    with closing(raw_conn(db_path)) as conn:
        assert head_count(conn, "regob_cf") == 1
        assert head_decision(conn, "regob_cf") == first


def test_d2s_trigger_messages_are_single_string_literals():
    """Portability: ``RAISE()``'s message must be a literal, not an expression.

    Older SQLite builds -- including the one shipped with the CI interpreter --
    reject a concatenated message with ``near "||": syntax error``, which would
    make EVERY pilot2 table creation fail. Newer local builds accept it, so this
    difference is invisible without a guard.
    """
    models = importlib.import_module("aios.pilot2.models")
    source = Path(models.__file__).read_text(encoding="utf-8")
    chunks = source.split("RAISE(ABORT,")[1:]
    assert chunks, "the trigger DDL must still contain RAISE(ABORT, ...) guards"
    for chunk in chunks:
        message = chunk.split("')", 1)[0]
        assert "||" not in message, (
            "RAISE() takes a single string literal; a concatenated expression "
            "breaks table creation on older SQLite builds"
        )


# --- isolated staging schema rebuild --------------------------------------
def _legacy_table(engine, *, rows: int = 0) -> None:
    """Recreate the superseded single-table attribution shape, optionally filled."""
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE finalattribution (id TEXT PRIMARY KEY, status TEXT)"))
        for index in range(rows):
            conn.execute(
                text("INSERT INTO finalattribution (id, status) VALUES (:id, :status)"),
                {"id": f"fa_{index}", "status": "ACCEPTED"},
            )


def test_d2s_staging_rebuild_retires_the_superseded_attribution_table(tmp_path):
    """PILOT-2A rebuilds its ISOLATED schema; the old shape must not survive.

    No main alembic migration is authorised for this package, so a shape change
    is applied by rebuilding the isolated pilot-2 staging schema. A leftover
    ``finalattribution`` table would keep a writable attribution surface that no
    longer honours D2, so the rebuild retires it -- but only after the
    replacement schema exists and verifies (see the two tests below).
    """
    db_path = tmp_path / "rebuild.db"
    engine = get_engine(sqlite_url(db_path))
    try:
        _legacy_table(engine)
        assert "finalattribution" in inspect(engine).get_table_names()

        mca.run_create(engine)

        names = set(inspect(engine).get_table_names())
        assert "finalattribution" not in names, "the superseded table must be retired"
        assert {"finalattributiondecision", "finalattributionhead"} <= names
        # Idempotent: a second rebuild on the clean schema retires nothing.
        assert mca.retire_superseded_tables(engine) == []
        assert mca.retirement_survey(engine) == {}
    finally:
        engine.dispose()


def test_d2s_staging_rebuild_keeps_history_when_the_new_schema_cannot_be_created(
    tmp_path, monkeypatch
):
    """P1-2 regression: never destroy the old shape before proving the new one.

    The previous order was ``drop_retired_tables()`` then ``create_all()``, each
    committing on its own. That is destroy-before-prove, and the failure it
    exposes is not hypothetical: the immediately preceding commit on this very
    branch could not create the pilot2 tables on CI at all (a trigger DDL the CI
    SQLite build rejected). Under the old order that run would have left the
    staging database with NO attribution table whatsoever.
    """
    from aios.pilot2.models import pilot2_metadata

    db_path = tmp_path / "create_fails.db"
    engine = get_engine(sqlite_url(db_path))
    try:
        _legacy_table(engine, rows=2)

        def boom(*_args, **_kwargs):
            raise OperationalError("CREATE TRIGGER ...", {}, Exception("near \"||\": syntax error"))

        monkeypatch.setattr(pilot2_metadata, "create_all", boom)
        with pytest.raises(OperationalError):
            mca.run_create(engine)

        names = set(inspect(engine).get_table_names())
        assert "finalattribution" in names, "a failed rebuild must not have destroyed history"
        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM finalattribution")).scalar_one() == 2
        assert "finalattributiondecision" not in names
    finally:
        engine.dispose()


def test_d2s_staging_rebuild_refuses_to_retire_against_an_incomplete_schema(tmp_path):
    """Tables without their D2 triggers are not a replacement -- retirement stops.

    Simulates a partially-applied DDL: the new tables are present but a guard
    trigger is not, so the "replacement" is a fully writable attribution surface
    with the invariants missing. Retiring the old shape against that would be a
    downgrade, so it is refused and the old table survives.
    """
    db_path = tmp_path / "incomplete.db"
    engine = get_engine(sqlite_url(db_path))
    try:
        _legacy_table(engine)
        pilot2_metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TRIGGER trg_fhead_forward_only"))

        with pytest.raises(mca.SchemaRebuildError) as excinfo:
            mca.retire_superseded_tables(engine)
        assert "trg_fhead_forward_only" in str(excinfo.value)
        # The dropped trigger is genuinely one of the D2 guard triggers, so the
        # test is anchored to the canonical set rather than a hardcoded string.
        assert "trg_fhead_forward_only" in D2_TRIGGER_NAMES
        assert "finalattribution" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_d2s_staging_rebuild_never_destroys_rows_implicitly(tmp_path):
    """A non-empty superseded table is a refusal, not a silent data loss."""
    db_path = tmp_path / "nonempty.db"
    engine = get_engine(sqlite_url(db_path))
    try:
        _legacy_table(engine, rows=3)

        with pytest.raises(mca.SchemaRebuildError) as excinfo:
            mca.run_create(engine)
        message = str(excinfo.value)
        assert "finalattribution" in message
        assert "allow_data_loss=True" in message

        names = set(inspect(engine).get_table_names())
        # The replacement schema WAS created (that is the whole point of the new
        # order) -- only the destructive step was refused.
        assert {"finalattributiondecision", "finalattributionhead"} <= names
        assert "finalattribution" in names
        assert mca.retirement_survey(engine) == {"finalattribution": 3}
    finally:
        engine.dispose()


def test_d2s_staging_rebuild_exports_rows_before_an_authorised_retirement(tmp_path):
    """``allow_data_loss=True`` is the explicit contract: export, then drop."""
    db_path = tmp_path / "authorised.db"
    export_dir = tmp_path / "exports"
    engine = get_engine(sqlite_url(db_path))
    try:
        _legacy_table(engine, rows=2)

        mca.run_create(engine, allow_data_loss=True, export_dir=export_dir)

        assert "finalattribution" not in inspect(engine).get_table_names()
        sidecars = sorted(export_dir.glob("pilot2_retired_rows_*.json"))
        assert len(sidecars) == 1, "the rows must leave the database before the table does"
        payload = json.loads(sidecars[0].read_text(encoding="utf-8"))
        exported = payload["tables"]["finalattribution"]
        assert exported["columns"] == ["id", "status"]
        assert [row[0] for row in exported["rows"]] == ["fa_0", "fa_1"]
    finally:
        engine.dispose()


# ===========================================================================
# P1-3 -- TOCTOU closure: survey, export and DROP share one write-locked txn
# ===========================================================================
def _build_retire_scenario(db_path: Path, *, legacy_rows: int = 0):
    """Fresh pilot2 database (new schema + superseded legacy table) for retire tests."""
    engine = get_engine(sqlite_url(db_path))
    pilot2_metadata.create_all(engine)
    _legacy_table(engine, rows=legacy_rows)
    return engine


def test_d2s_p13_late_committed_row_blocks_silent_loss(tmp_path):
    """Codex's exact repro must now REFUSE, never drop with the row gone.

    The old code surveyed empty, a concurrent insert landed, and the table was
    dropped -- ``dropped == ['finalattribution']``, ``legacy_table_exists ==
    False``, ``sidecars == []``. With the write lock held for the whole
    survey->drop window, a row committed before our lock is counted and the
    non-empty refusal fires, so the table and its row survive.
    """
    db_path = tmp_path / "p13_late.db"
    engine = _build_retire_scenario(db_path)  # legacy table, 0 rows

    # A SECOND, independent connection commits a row after the new schema exists
    # but before retirement -- exactly Codex's TOCTOU window.
    late = sqlite3.connect(str(db_path), isolation_level=None)
    late.execute("INSERT INTO finalattribution (id, status) VALUES ('late1', 'ACCEPTED')")
    late.commit()
    late.close()

    with pytest.raises(mca.SchemaRebuildError):
        mca.retire_superseded_tables(engine, allow_data_loss=False)

    # Nothing was silently lost: the table and the late row are both intact.
    assert "finalattribution" in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM finalattribution")).scalar_one() == 1
    engine.dispose()


def test_d2s_p13_authorized_retire_exports_a_concurrent_row(tmp_path):
    """``allow_data_loss=True`` must export a row committed before the drop.

    A second connection commits an extra row before retirement; because the
    survey and the export read the SAME locked snapshot, that row is counted and
    written to the sidecar, not dropped unseen.
    """
    db_path = tmp_path / "p13_export.db"
    export_dir = tmp_path / "sidecars"
    engine = _build_retire_scenario(db_path, legacy_rows=1)

    late = sqlite3.connect(str(db_path), isolation_level=None)
    late.execute("INSERT INTO finalattribution (id, status) VALUES ('concurrent', 'ACCEPTED')")
    late.commit()
    late.close()

    dropped = mca.retire_superseded_tables(engine, allow_data_loss=True, export_dir=export_dir)
    assert dropped == ["finalattribution"]

    sidecars = sorted(export_dir.glob("pilot2_retired_rows_*.json"))
    assert sidecars, "the rows must leave the database before the table does"
    payload = json.loads(sidecars[0].read_text(encoding="utf-8"))
    ids = {row[0] for row in payload["tables"]["finalattribution"]["rows"]}
    assert {"fa_0", "concurrent"} <= ids
    assert "finalattribution" not in inspect(engine).get_table_names()
    engine.dispose()


def test_d2s_p13_rejects_when_another_writer_holds_the_lock(tmp_path):
    """Lock contention is a fail-closed refusal; the table + data stay intact.

    A second connection takes the write lock and leaves it open. Our retire
    cannot acquire it, must refuse (never drop), and the superseded table with
    every one of its rows -- including the row the other writer added -- must
    remain.
    """
    db_path = tmp_path / "p13_locked.db"
    engine = _build_retire_scenario(db_path, legacy_rows=2)

    # A second connection takes the write lock and holds it OPEN (empty
    # transaction) for the duration of the attempt below.
    holder = sqlite3.connect(str(db_path), isolation_level=None, timeout=1)
    holder.execute("BEGIN IMMEDIATE")

    with pytest.raises(mca.SchemaRebuildError) as excinfo:
        mca.retire_superseded_tables(engine, allow_data_loss=True, lock_timeout_ms=800)
    assert "write lock" in str(excinfo.value).lower()

    # The refusal must leave the superseded table and every one of its already
    # committed rows exactly where they were -- nothing is dropped or truncated.
    assert "finalattribution" in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM finalattribution")).scalar_one() == 2
    holder.rollback()
    holder.close()
    engine.dispose()


def test_d2s_p13_export_failure_preserves_table_and_rows(tmp_path):
    """If the export cannot be written, the whole retire rolls back.

    The export target is an existing FILE, so ``mkdir`` raises; the locked
    transaction is rolled back and the superseded table with all its rows is left
    exactly where it was -- no partial destruction.
    """
    db_path = tmp_path / "p13_export_fail.db"
    engine = _build_retire_scenario(db_path, legacy_rows=2)

    with pytest.raises(Exception) as excinfo:
        mca.retire_superseded_tables(
            engine, allow_data_loss=True, export_dir=db_path  # db_path is a file, not a dir
        )
    # The export step failed (the target path is an existing file, so mkdir
    # raises), which must propagate and abort the retire.
    assert isinstance(excinfo.value, OSError)

    assert "finalattribution" in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM finalattribution")).scalar_one() == 2
    engine.dispose()


def test_d2s_p13_refuses_when_a_required_d2_trigger_is_dropped_before_the_lock(tmp_path):
    """Codex P1-3 follow-up: verify-then-corrupt must REFUSE, never downgrade.

    The schema re-verification must run on the SAME cursor that holds the write
    lock, AFTER ``BEGIN IMMEDIATE``. A second, independent connection drops a
    required D2 trigger (``trg_fhead_forward_only``) after the schema was created
    and verified but before retirement takes its lock. The in-lock re-verify must
    catch the missing trigger and refuse -- preserving the superseded
    ``finalattribution`` table and all its rows -- instead of retiring the old
    table against an incomplete replacement schema (the exact failure Codex
    reproduced: finalattribution gone AND trg_fhead_forward_only gone).
    """
    db_path = tmp_path / "p13_corrupt_trigger.db"
    engine = _build_retire_scenario(db_path, legacy_rows=2)

    # A SECOND, independent connection drops a required D2 trigger. The new
    # schema was complete a moment ago; now it is not -- this is the
    # "verify-then-corrupt" window that the write lock must protect against.
    attacker = sqlite3.connect(str(db_path), isolation_level=None)
    attacker.execute("DROP TRIGGER IF EXISTS trg_fhead_forward_only")
    attacker.commit()
    attacker.close()

    with pytest.raises(mca.SchemaRebuildError) as excinfo:
        mca.retire_superseded_tables(engine, allow_data_loss=True)
    assert "D2 triggers" in str(excinfo.value)

    # The refusal preserved the OLD table and its rows -- no silent downgrade.
    assert "finalattribution" in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM finalattribution")).scalar_one() == 2
    # The corruption is real: the required trigger is genuinely gone, so the
    # refusal was caused by it (not a coincidental failure).
    with engine.connect() as conn:
        remaining = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            ).fetchall()
        }
    assert "trg_fhead_forward_only" not in remaining
    assert "trg_fhead_forward_only" in D2_TRIGGER_NAMES
    engine.dispose()


# ===========================================================================
# C2 -- proposal <-> registration consistency at the persistence boundary
# ===========================================================================
def test_c2_proposal_registration_mismatch_rejected(pilot2_db):
    """Finalizing proposal-for-A against registration B is an FK violation."""
    eng, _ = pilot2_db
    seed_chain(eng, reg_id="regob_a", proposals={"aprop_for_a": AttributionLevel.AMBIGUOUS})
    seed_chain(eng, reg_id="regob_b", proposals={"aprop_for_b": AttributionLevel.AMBIGUOUS})
    with eng.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            INSERT_DECISION_TEXT,
            {
                "id": "fdec_mismatch",
                "proposal_id": "aprop_for_a",
                "reg_id": "regob_b",
                "level": LEGAL_FINAL_SQL,
                "supersedes": None,
            },
        )


def test_c2_composite_unique_backs_the_composite_fk(pilot2_db):
    """The composite FK is only enforceable because of UNIQUE(id, reg_id)."""
    eng, _ = pilot2_db
    constraints = {
        item["name"] for item in inspect(eng).get_unique_constraints("attributionproposal")
    }
    assert "uq_aprop_id_reg" in constraints
    foreign_keys = inspect(eng).get_foreign_keys("finalattributiondecision")
    composite = [
        fk
        for fk in foreign_keys
        if set(fk["constrained_columns"]) == {"proposal_id", "registration_observation_id"}
    ]
    assert composite, "a decision must carry a composite FK to the proposal"
    assert composite[0]["referred_table"] == "attributionproposal"

    # The head is bound to its decision by the SAME composite identity, so it can
    # only ever cite a decision that belongs to its own registration.
    head_fks = inspect(eng).get_foreign_keys("finalattributionhead")
    head_composite = [
        fk
        for fk in head_fks
        if set(fk["constrained_columns"]) == {"decision_id", "registration_observation_id"}
    ]
    assert head_composite, "the head must carry a composite FK to the decision"
    assert head_composite[0]["referred_table"] == "finalattributiondecision"


# ===========================================================================
# FIX 3 / D3 + C3 -- staging guard
# ===========================================================================
def test_fix3_substring_marker_logic_is_gone():
    source = Path(mca.__file__).read_text(encoding="utf-8")
    assert "STAGING_MARKERS" not in source
    assert not hasattr(mca, "STAGING_MARKERS")


def test_fix3_authority_is_derived_from_human_env(synthetic_authority, tmp_path):
    """C3: the authorised path comes from ``human_env.UAT_DB_PATH``, not a literal."""
    module_file = tmp_path / "human_env.py"
    resolved = mca.resolve_authorized_staging_path(authority_module=module_file)
    assert mca.normalize_fs_path(resolved) == mca.normalize_fs_path(synthetic_authority)
    source = Path(mca.__file__).read_text(encoding="utf-8")
    assert "human_uat.db" not in source, "the staging filename must not be re-hardcoded"


def test_fix3_exact_staging_path_accepted(synthetic_authority):
    ok = mca.should_create(
        sqlite_url(synthetic_authority), staging_path=synthetic_authority
    )
    assert ok is True


def test_fix3_non_canonical_spelling_of_the_same_file_accepted(synthetic_authority):
    """Normalisation is real: ``a/../a/db`` is the same file, so it is allowed."""
    detoured = synthetic_authority.parent / ".." / synthetic_authority.parent.name
    assert mca.should_create(sqlite_url(detoured / synthetic_authority.name),
                              staging_path=synthetic_authority) is True


def test_fix3_lookalike_paths_rejected(synthetic_authority):
    """The exact cases named in the review: siblings, backups, test doubles."""
    staging_dir = synthetic_authority.parent
    project_dir = staging_dir.parent
    rejected = [
        staging_dir / "staging_test.db",
        staging_dir / "human_uat.db.bak",
        staging_dir / "human_uat_copy.db",
        project_dir / "prod-staging-backup.db",
        project_dir / "human_uat.db",
        project_dir / "staging2" / "human_uat.db",
        project_dir.parent / "human_uat.db",
    ]
    for path in rejected:
        assert mca.should_create(sqlite_url(path), staging_path=synthetic_authority) is False, \
            f"{path} must be refused"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql:///var/lib/aios/human_uat.db",
        "sqlite+pysqlite:////tmp/human_uat.db",
        "sqlite://human_uat:secret@/tmp/other.db",
        "sqlite:////tmp/other.db?cache=human_uat.db",
        "sqlite://evil.example.com/human_uat.db",
        "sqlite:///relative/staging/human_uat.db",
        "sqlite:///:memory:",
        "sqlite://",
        "not-a-url-at-all",
    ],
)
def test_fix3_malformed_or_indirect_urls_rejected(synthetic_authority, url):
    assert mca.should_create(url, staging_path=synthetic_authority) is False
    assert mca.is_authorized_staging_url(url, staging_path=synthetic_authority) is False


def test_fix3_marker_in_userinfo_or_query_never_authorises(synthetic_authority):
    """A marker that only appears in userinfo / query must not grant access."""
    authorised = synthetic_authority.as_posix()
    for url in (
        f"sqlite://human_uat.db:x@/{authorised.lstrip('/')}",
        f"sqlite:///{authorised}?mode=ro",
    ):
        assert mca.should_create(url, staging_path=synthetic_authority) is False


def test_fix3_should_create_fail_closed_without_authority(tmp_path):
    """No derivable authority -> refuse everything; never degrade to a warning."""
    # The repo-fixed module is absent in this checkout, so resolution fails closed.
    with pytest.raises(mca.StagingGuardError):
        mca.resolve_authorized_staging_path()
    assert mca.should_create(sqlite_url(tmp_path / "anything.db")) is False


def test_fix3_authority_without_uat_db_path_is_refused(tmp_path):
    module_file = tmp_path / "human_env.py"
    module_file.write_text("SOMETHING_ELSE = 1\n", encoding="utf-8")
    with pytest.raises(mca.StagingGuardError):
        mca.resolve_authorized_staging_path(authority_module=module_file)
    assert mca.should_create(sqlite_url(tmp_path / "x.db")) is False


# ===========================================================================
# FIX 6 / D3 -- the authority is repo-fixed; no env var / look-alike can replace it
# ===========================================================================
def test_fix6_env_var_override_has_no_effect(tmp_path, monkeypatch):
    """FIX 6 / D3: the removed override variable can no longer redirect authority.

    A fake module that sets ``UAT_DB_PATH`` at a production DB, named via the old
    ``AIOS_PILOT2_STAGING_ENV_MODULE`` variable, must NOT authorise that DB -- the
    variable is no longer read.
    """
    fake_module = tmp_path / "fake_human_env.py"
    fake_module.write_text(
        'UAT_DB_PATH = ' + repr(str(tmp_path / "production.db")) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("AIOS_PILOT2_STAGING_ENV_MODULE", str(fake_module))
    # The function never consults the env var, so the repo-fixed (absent)
    # authority is used and the production DB is refused.
    with pytest.raises(mca.StagingGuardError):
        mca.resolve_authorized_staging_path()
    assert mca.should_create(sqlite_url(tmp_path / "production.db")) is False


def test_fix6_lookalike_module_cannot_replace_authority(tmp_path):
    """FIX 6 / D3: a ``human_env.py`` that exists elsewhere cannot become the authority."""
    lookalike_dir = tmp_path / "staging2"
    lookalike_dir.mkdir()
    lookalike = lookalike_dir / "human_env.py"
    lookalike.write_text(
        'UAT_DB_PATH = ' + repr(str(lookalike_dir / "human_uat.db")) + "\n", encoding="utf-8"
    )
    # The look-alike's path is never authorised: the only authority is the
    # repo-fixed module, resolved independently of any on-disk look-alike.
    assert mca.should_create(sqlite_url(lookalike_dir / "human_uat.db")) is False
    with pytest.raises(mca.StagingGuardError):
        mca.resolve_authorized_staging_path()


def test_fix3_refuse_exits_non_zero():
    with pytest.raises(SystemExit) as excinfo:
        mca._refuse("synthetic reason")
    assert excinfo.value.code == 2


def test_fix3_import_has_no_side_effects(tmp_path, monkeypatch):
    """Importing the module must never create a database or any table."""
    probe = tmp_path / "import_probe.db"
    monkeypatch.setenv("AIOS_DATABASE_URL", sqlite_url(probe))
    importlib.reload(mca)
    assert not probe.exists(), "import triggered create_all() -- forbidden"


# ===========================================================================
# FIX 4 / D4 / C4 -- default collection
# ===========================================================================
def test_fix4_module_is_inside_default_testpaths(pytestconfig):
    """D4: this file sits under the configured ``testpaths`` of the repo root."""
    rootpath = Path(pytestconfig.rootpath).resolve()
    testpaths = pytestconfig.getini("testpaths")
    assert testpaths, "pyproject must configure testpaths"
    roots = [(rootpath / entry).resolve() for entry in testpaths]
    here = Path(__file__).resolve()
    assert any(here.is_relative_to(root) for root in roots), (
        f"{here} is outside the default collection roots {roots}"
    )


def test_fix4_no_sys_path_hack():
    """The ``SRC`` sys.path hack is gone; ``pythonpath`` in pyproject suffices."""
    source = Path(__file__).read_text(encoding="utf-8")
    assert "sys.path" + ".insert" not in source
    assert "sys.path" + ".append" not in source
    assert "src" in Path(__file__).resolve().parents[1].joinpath("pyproject.toml").read_text(
        encoding="utf-8"
    )


def test_fix4_legacy_pilot2_test_package_removed():
    """The old ``src/aios/pilot2/tests`` tree must not come back (it is never collected)."""
    import aios.pilot2

    legacy = Path(aios.pilot2.__file__).resolve().parent / "tests"
    assert not legacy.exists(), f"{legacy} is outside testpaths and would silently not run"
