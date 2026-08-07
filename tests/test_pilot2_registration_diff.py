"""PILOT-2A-3 Registration Diff Engine -- acceptance tests.

Acceptance tests covering the 15 hard contracts (design §11 / Issue #134) plus
the three PILOT-2A-3 review fixes (P1-1 / P1-2 / P1-3):

  AC1  C1/C13/C14  engine is read-only and side-effect free (no Mihe write,
                    no network, no filesystem)
  AC2  C2           incomplete (truncated) snapshot -> fail-closed
  AC3  C11          missing snapshot (sequence gap) -> fail-closed
  AC4  C3/C6        stable identity; one registration per customer
  AC5  C7           updates refresh latest values; never create a new row
  AC6  C4/C5        idempotent and byte-equivalent replay
  AC7  C8           UNKNOWN_BATCH_COHORT distinguishes suspected batch accounts
  AC8  C9/C10       batch accounts are never labeled / no invented attribution
  AC9  C12          raw rows are operational, never KnowledgeFact
  AC10 C15/P1-1     result materializes into the approved RegistrationObservation
                    (pilot2-bound, not a disconnected parallel table)
  AC11 C4/C6/C7     persistence upsert is idempotent, version bumps on refresh
  AC12 P1-2         stale (older-seq) replay is a no-op, never reverts state
  AC13 P1-3         truncated pagination metadata -> fail-closed
  AC13b P1-3        consistent pagination metadata -> accepted
  AC14 P1-2         equal-seq replay with a different observation -> fail-closed
  AC15 P1-1         persisted row's source_snapshot_id references a real
                    MiheSnapshot (downstream FK usability proven)

All tests use a throwaway local SQLite file. The real staging database is
never opened and no global ``AIOS_DATABASE_URL`` is mutated.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlmodel import Session, select

from aios.db import get_engine
from aios.pilot2.models import (
    CohortTag,
    MiheSnapshot,
    RegistrationObservation,
    SQLModel,
    pilot2_metadata,
)
from aios.pilot2.registration_diff import (
    MiheCustomer,
    MiheCustomerSnapshot,
    RegistrationDiffRow,
    SnapshotIncompleteError,
    SnapshotPaginationError,
    SnapshotReplayConflictError,
    SnapshotSequenceGapError,
    diff_registrations,
    persist_diffs,
    run_registration_diff,
    serialize_diffs,
)

# Batch window bounds (mirror the engine's internal constants, used only to
# build fixtures that land inside / outside the window).
_BATCH_START_MS = 1_785_891_600_000  # 2026-08-05T01:00:00Z
_BATCH_END_MS = 1_785_891_900_000    # 2026-08-05T01:05:00Z
_BATCH_INSIDE_MS = _BATCH_START_MS + 30_000
_NATURAL_JULY_MS = 1_783_598_400_000  # 2026-07-09T12:00:00Z


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture
def engine(tmp_path: Path):
    db = tmp_path / "pilot2_test.db"
    eng = get_engine(sqlite_url(db))
    pilot2_metadata.create_all(eng)  # creates the (updated) pilot2 tables
    return eng


def _cust(cid: str, reg_ms: int, **kw) -> MiheCustomer:
    return MiheCustomer(id=cid, registered_at_ms=reg_ms, **kw)


def _snap(
    seq: int,
    customers: tuple[MiheCustomer, ...] = (),
    *,
    is_complete: bool = True,
    total: int | None = None,
    page: int = 1,
    page_size: int | None = 20,
) -> MiheCustomerSnapshot:
    """A well-formed snapshot with *consistent* pagination metadata.

    Completeness must be verifiable (P1-3), so every snapshot the engine accepts
    has to carry ``total``. Tests that are not about pagination use this helper
    and get a self-consistent ``total`` for free; the pagination tests build
    ``MiheCustomerSnapshot`` directly so they can supply deliberately broken
    metadata.
    """
    return MiheCustomerSnapshot(
        seq=seq,
        is_complete=is_complete,
        customers=customers,
        total=len(customers) if total is None else total,
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# AC1 -- C1 / C13 / C14 : read-only, no side effects
# ---------------------------------------------------------------------------
def test_ac1_engine_is_read_only_and_side_effect_free(tmp_path: Path):
    # Structural: the engine module must not import any network / subprocess
    # machinery -- proving it cannot write to Mihe or touch the outside world.
    spec = importlib.util.find_spec("aios.pilot2.registration_diff")
    assert spec is not None and spec.origin is not None
    src = Path(spec.origin).read_text(encoding="utf-8")
    forbidden = (
        "import requests",
        "import httpx",
        "import urllib",
        "import aiohttp",
        "import subprocess",
        "socket",
    )
    for token in forbidden:
        assert token not in src, f"engine must not import {token!r}"

    # Behavioural: diff_registrations is a pure function over in-memory data.
    # Running it must not create any file in the temp dir.
    before = set(p.name for p in tmp_path.iterdir())
    snaps = [_snap(seq=1, customers=(_cust("a", _NATURAL_JULY_MS, balance=10),))]
    rows1 = diff_registrations(snaps)
    rows2 = diff_registrations(snaps)
    after = set(p.name for p in tmp_path.iterdir())
    assert before == after, "pure engine must not write files"
    assert rows1 == rows2


# ---------------------------------------------------------------------------
# AC2 -- C2 : incomplete snapshot -> fail-closed
# ---------------------------------------------------------------------------
def test_ac2_incomplete_snapshot_refused():
    snaps = [
        MiheCustomerSnapshot(
            seq=1,
            is_complete=False,  # truncated page
            customers=(_cust("a", _NATURAL_JULY_MS),),
        )
    ]
    with pytest.raises(SnapshotIncompleteError):
        diff_registrations(snaps)


# ---------------------------------------------------------------------------
# AC3 -- C11 : missing snapshot (gap) -> fail-closed
# ---------------------------------------------------------------------------
def test_ac3_missing_snapshot_gap_refused():
    snaps = [
        _snap(seq=1, is_complete=True, customers=(_cust("a", _NATURAL_JULY_MS),)),
        # seq 2 is missing -> gap
        _snap(seq=3, is_complete=True, customers=(_cust("b", _NATURAL_JULY_MS),)),
    ]
    with pytest.raises(SnapshotSequenceGapError):
        diff_registrations(snaps)


# ---------------------------------------------------------------------------
# AC4 -- C3 / C6 : stable identity, single registration per customer
# ---------------------------------------------------------------------------
def test_ac4_stable_identity_single_registration():
    c1_s1 = _cust("c1", _NATURAL_JULY_MS, balance=0)
    c1_s2 = _cust("c1", _NATURAL_JULY_MS, balance=5)
    c1_s3 = _cust("c1", _NATURAL_JULY_MS, balance=9)
    snaps = [
        _snap(seq=1, is_complete=True, customers=(c1_s1,)),
        _snap(seq=2, is_complete=True, customers=(c1_s2,)),
        _snap(seq=3, is_complete=True, customers=(c1_s3,)),
    ]
    rows = diff_registrations(snaps)
    assert len(rows) == 1  # exactly one registration for customer c1
    assert rows[0].customer_id == "c1"
    assert rows[0].first_seen_seq == 1


# ---------------------------------------------------------------------------
# AC5 -- C7 : updates refresh latest, never create a new row
# ---------------------------------------------------------------------------
def test_ac5_update_refreshes_latest_not_new_row():
    c1_s1 = _cust("c1", _NATURAL_JULY_MS, balance=0, total_recharge=0)
    c1_s2 = _cust("c1", _NATURAL_JULY_MS, balance=100, total_recharge=50)
    snaps = [
        _snap(seq=1, is_complete=True, customers=(c1_s1,)),
        _snap(seq=2, is_complete=True, customers=(c1_s2,)),
    ]
    rows = diff_registrations(snaps)
    assert len(rows) == 1
    r = rows[0]
    # latest values are reflected
    assert r.balance == 100
    assert r.total_recharge == 50
    # first-seen registration is frozen
    assert r.first_seen_seq == 1
    assert r.last_seen_seq == 2
    # registration timestamp (first seen) is unchanged by the update
    assert r.registered_at_ms == _NATURAL_JULY_MS


# ---------------------------------------------------------------------------
# AC6 -- C4 / C5 : idempotent and byte-equivalent replay
# ---------------------------------------------------------------------------
def test_ac6_idempotent_and_byte_equivalent():
    snaps = [
        _snap(seq=1, is_complete=True, customers=(
            _cust("a", _NATURAL_JULY_MS, balance=1),
            _cust("b", _BATCH_INSIDE_MS, balance=2),
        )),
        _snap(seq=2, is_complete=True, customers=(
            _cust("a", _NATURAL_JULY_MS, balance=3),
            _cust("b", _BATCH_INSIDE_MS, balance=4),
            _cust("c", _NATURAL_JULY_MS, balance=5),
        )),
    ]
    rows_a = diff_registrations(snaps)
    rows_b = diff_registrations(snaps)
    # C4: same input -> identical rows (incl. deterministic observation_hash)
    assert [r.observation_hash for r in rows_a] == [r.observation_hash for r in rows_b]
    assert rows_a == rows_b
    # C5: byte-equivalent serialized replay
    assert serialize_diffs(rows_a) == serialize_diffs(rows_b)


# ---------------------------------------------------------------------------
# AC7 -- C8 : UNKNOWN_BATCH_COHORT distinguishes batch accounts
# ---------------------------------------------------------------------------
def test_ac7_unknown_batch_cohort_distinguished():
    snaps = [
        _snap(seq=1, is_complete=True, customers=(
            _cust("batch1", _BATCH_INSIDE_MS),  # inside the 5-min window
            _cust("natural1", _NATURAL_JULY_MS),
        )),
    ]
    rows = {r.customer_id: r for r in diff_registrations(snaps)}
    assert rows["batch1"].is_batch is True
    assert rows["batch1"].cohort_tag == CohortTag.UNKNOWN_BATCH_COHORT
    assert rows["natural1"].is_batch is False
    assert rows["natural1"].cohort_tag == CohortTag.NATURAL


# ---------------------------------------------------------------------------
# AC8 -- C9 / C10 : batch accounts never labeled; no invented attribution
# ---------------------------------------------------------------------------
def test_ac8_no_false_labeling_no_invented_attribution():
    snaps = [
        _snap(seq=1, is_complete=True, customers=(
            _cust("batch1", _BATCH_INSIDE_MS),
        )),
    ]
    rows = diff_registrations(snaps)
    assert len(rows) == 1
    r = rows[0]
    # The batch account is distinguished but carries NO traffic source. The
    # output type has no source/channel field at all, so no attribution can be
    # invented -- only the operational cohort tag exists.
    assert r.cohort_tag == CohortTag.UNKNOWN_BATCH_COHORT
    assert r.is_batch is True
    # The deterministic payload contains no attribution source key.
    payload = r._deterministic_payload()
    assert "source" not in payload
    assert "channel" not in payload
    assert "attribution" not in payload


# ---------------------------------------------------------------------------
# AC9 -- C12 : raw rows are operational, never KnowledgeFact
# ---------------------------------------------------------------------------
def test_ac9_raw_rows_operational_not_knowledge_fact():
    # The engine module must not import or instantiate KnowledgeFact -- the
    # output is an operational observation only. Docstring discussion of the
    # contract is allowed; only real code references fail the check.
    spec = importlib.util.find_spec("aios.pilot2.registration_diff")
    assert spec is not None and spec.origin is not None
    src = Path(spec.origin).read_text(encoding="utf-8")
    assert "KnowledgeFact(" not in src, "engine must not instantiate KnowledgeFact"
    import_lines = [ln for ln in src.splitlines() if "import" in ln]
    assert not any("knowledge" in ln for ln in import_lines), (
        "engine must not import the knowledge module"
    )

    snaps = [
        _snap(seq=1, is_complete=True, customers=(_cust("a", _NATURAL_JULY_MS),)),
    ]
    rows = diff_registrations(snaps)
    assert all(isinstance(r, RegistrationDiffRow) for r in rows)


# ---------------------------------------------------------------------------
# AC10 -- C15 / P1-1 : result materializes into the approved RegistrationObservation
# ---------------------------------------------------------------------------
def test_ac10_persists_into_approved_registration_observation():
    # The diff engine materializes into the already-approved canonical
    # RegistrationObservation (the table every attribution FK points at). It does
    # NOT create a disconnected parallel table.
    assert RegistrationObservation.__table__.metadata is pilot2_metadata
    assert RegistrationObservation.__table__.metadata is not SQLModel.metadata


# ---------------------------------------------------------------------------
# AC11 -- C4 / C6 / C7 : persistence upsert is idempotent, version bumps on refresh
# ---------------------------------------------------------------------------
def test_ac11_persist_idempotent_upsert(engine):
    snaps = [
        _snap(seq=1, is_complete=True, customers=(
            _cust("a", _NATURAL_JULY_MS, balance=1),
            _cust("b", _BATCH_INSIDE_MS, balance=2),
        )),
    ]
    rows, changed = run_registration_diff(engine, snaps)
    assert changed == 2
    with Session(engine) as s:
        assert s.exec(select(RegistrationObservation)).all().__len__() == 2

    # Re-run on identical snapshots -> no further changes (idempotent).
    _, changed2 = run_registration_diff(engine, snaps)
    assert changed2 == 0
    with Session(engine) as s:
        assert s.exec(select(RegistrationObservation)).all().__len__() == 2

    # A new snapshot re-shows both customers: 'a' with a changed material value
    # (balance 1 -> 10) and 'b' with unchanged material values but observed in a
    # later snapshot (so its ``last_seen_seq`` legitimately advances 1 -> 2).
    # Both are UPDATES -- no new registration row is created (C6/C7).
    snaps2 = [
        _snap(seq=1, is_complete=True, customers=(
            _cust("a", _NATURAL_JULY_MS, balance=1),
            _cust("b", _BATCH_INSIDE_MS, balance=2),
        )),
        _snap(seq=2, is_complete=True, customers=(
            _cust("a", _NATURAL_JULY_MS, balance=10),  # material update
            _cust("b", _BATCH_INSIDE_MS, balance=2),   # unchanged material, later snapshot
        )),
    ]
    _, changed3 = run_registration_diff(engine, snaps2)
    assert changed3 == 2  # both rows refreshed (a materially, b by last_seen_seq)
    with Session(engine) as s:
        all_rows = s.exec(select(RegistrationObservation)).all()
        assert len(all_rows) == 2  # C6: no new registration row
        a = s.exec(select(RegistrationObservation).where(
            RegistrationObservation.customer_id == "a")).one()
        b = s.exec(select(RegistrationObservation).where(
            RegistrationObservation.customer_id == "b")).one()
        # 'a': materially updated, first-seen registration frozen.
        assert a.balance == 10
        assert a.version == 2  # bumped on material update
        assert a.first_seen_seq == 1
        assert a.last_seen_seq == 2
        # 'b': material unchanged but observed in the later snapshot.
        assert b.balance == 2
        assert b.last_seen_seq == 2  # advanced by later snapshot
        assert b.version == 2  # refreshed (last_seen_seq advanced)


# ---------------------------------------------------------------------------
# AC12 -- P1-2 : stale (older-seq) replay is a no-op, never reverts state
# ---------------------------------------------------------------------------
def test_ac12_stale_replay_does_not_revert_state(engine):
    # Persist a later snapshot first (seq=2, balance=10).
    run_registration_diff(engine, [
        _snap(seq=2, is_complete=True,
                             customers=(_cust("a", _NATURAL_JULY_MS, balance=10),)),
    ])
    # Now replay an OLDER snapshot (seq=1, balance=1) for the same customer.
    _, changed = run_registration_diff(engine, [
        _snap(seq=1, is_complete=True,
                             customers=(_cust("a", _NATURAL_JULY_MS, balance=1),)),
    ])
    assert changed == 0  # stale replay is a no-op (P1-2)
    with Session(engine) as s:
        row = s.exec(select(RegistrationObservation).where(
            RegistrationObservation.customer_id == "a")).one()
        # The observation must NOT be reverted to the older snapshot's state.
        assert row.balance == 10
        assert row.last_seen_seq == 2
        assert row.first_seen_seq == 2
        assert row.version == 1


# ---------------------------------------------------------------------------
# AC13 -- P1-3 : truncated pagination metadata -> fail-closed
# ---------------------------------------------------------------------------
def test_ac13_truncated_pagination_refused():
    # total=100, page=1, page_size=20 but only 1 customer present, claimed
    # complete. The engine must refuse rather than accept a truncated set.
    snaps = [
        MiheCustomerSnapshot(
            seq=1,
            is_complete=True,
            customers=(_cust("a", _NATURAL_JULY_MS),),
            total=100,
            page=1,
            page_size=20,
        )
    ]
    with pytest.raises(SnapshotPaginationError):
        diff_registrations(snaps)


# ---------------------------------------------------------------------------
# AC13b -- P1-3 : consistent pagination metadata -> accepted
# ---------------------------------------------------------------------------
def test_ac13b_valid_pagination_accepted():
    snaps = [
        MiheCustomerSnapshot(
            seq=1,
            is_complete=True,
            customers=(
                _cust("a", _NATURAL_JULY_MS),
                _cust("b", _NATURAL_JULY_MS),
            ),
            total=2,
            page=1,
            page_size=20,
        )
    ]
    rows = diff_registrations(snaps)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# AC14 -- P1-2 : equal-seq replay with a different observation -> fail-closed
# ---------------------------------------------------------------------------
def test_ac14_equal_seq_conflicting_replay_refused(engine):
    # First persist seq=1 with balance=10.
    run_registration_diff(engine, [
        _snap(seq=1, is_complete=True,
                             customers=(_cust("a", _NATURAL_JULY_MS, balance=10),)),
    ])
    # Now replay seq=1 with a DIFFERENT observation (balance=99). Same seq,
    # different content -> corruption signal -> fail-closed.
    with pytest.raises(SnapshotReplayConflictError):
        run_registration_diff(engine, [
            _snap(seq=1, is_complete=True,
                                 customers=(_cust("a", _NATURAL_JULY_MS, balance=99),)),
        ])


# ---------------------------------------------------------------------------
# AC15 -- P1-1 : persisted row's source_snapshot_id references a real MiheSnapshot
# ---------------------------------------------------------------------------
def test_ac15_persisted_row_references_real_mihe_snapshot(engine):
    snaps = [
        _snap(seq=1, is_complete=True, customers=(
            _cust("a", _NATURAL_JULY_MS, balance=1),
            _cust("b", _BATCH_INSIDE_MS, balance=2),
        )),
    ]
    run_registration_diff(engine, snaps)
    with Session(engine) as s:
        obs = s.exec(select(RegistrationObservation)).all()
        assert len(obs) == 2
        for row in obs:
            assert row.source_snapshot_id is not None
            snap = s.get(MiheSnapshot, row.source_snapshot_id)
            # The FK target must exist -- proving the registration can be joined
            # to its raw evidence, i.e. downstream attribution FKs are usable.
            assert snap is not None
            assert snap.endpoint.value == "customers"


# ---------------------------------------------------------------------------
# AC16 -- P1-3 : completeness claim without verifiable metadata -> fail-closed
# ---------------------------------------------------------------------------
def test_ac16_completeness_claim_without_metadata_refused():
    """A snapshot may not simply *assert* completeness.

    Before this guard, omitting ``total`` made ``_expected_page_count`` return
    None and the engine fell straight back to trusting the ``is_complete``
    boolean -- exactly the fail-open hole P1-3 is meant to close. A caller could
    therefore bypass the whole integrity check by supplying no metadata at all.
    Completeness must be *verifiable*, not merely claimed.
    """
    snaps = [
        MiheCustomerSnapshot(
            seq=1,
            is_complete=True,          # claimed complete ...
            customers=(_cust("a", _NATURAL_JULY_MS),),
            total=None,                # ... but unverifiable
            page=1,
            page_size=None,
        )
    ]
    with pytest.raises(SnapshotPaginationError):
        diff_registrations(snaps)


# ---------------------------------------------------------------------------
# AC17 -- P1-1 : persisting a row with no raw evidence in the set -> fail-closed
# ---------------------------------------------------------------------------
def test_ac17_persist_without_matching_raw_evidence_refused(engine):
    """``source_snapshot_id`` must never be silently NULL.

    ``persist_diffs`` resolves the raw ``MiheSnapshot`` by the row's
    ``last_seen_seq``. If a caller passes pre-computed rows that reference a seq
    absent from the snapshot set, the lookup yields None and the engine would
    otherwise try to write NULL into the non-nullable FK -- surfacing as an
    opaque IntegrityError instead of a contract violation. Fail closed with a
    precise error, and write nothing.
    """
    snaps = [
        MiheCustomerSnapshot(
            seq=1,
            is_complete=True,
            customers=(_cust("a", _NATURAL_JULY_MS),),
            total=1,
            page=1,
            page_size=20,
        )
    ]
    orphan = RegistrationDiffRow(
        customer_id="ghost",
        first_seen_seq=7,
        last_seen_seq=7,           # seq 7 is NOT in the snapshot set
        registered_at_ms=_NATURAL_JULY_MS,
        last_login_at_ms=None,
        nickname=None,
        avatar=None,
        phone_masked=None,
        balance=0,
        customer_type=None,
        total_recharge=0,
        recharge_count=0,
        cohort_tag=CohortTag.NATURAL,
        is_batch=False,
    )
    with pytest.raises(SnapshotIncompleteError):
        persist_diffs(engine, snaps, rows=[orphan])

    # Fail-closed must also mean "no partial write".
    with Session(engine) as s:
        assert s.exec(select(RegistrationObservation)).all() == []


# ---------------------------------------------------------------------------
# AC18 -- P1 (re-opened) : a single page must NOT be treated as a complete fetch
# ---------------------------------------------------------------------------
def test_ac18_single_page_not_treated_as_complete_fetch():
    """The fail-open P1 regression.

    total=100, page=1, page_size=20, but only the 20 customers of page 1 are
    present, claimed complete. This is page 1 of 5. The engine must NOT accept
    it merely because the single page is internally consistent -- the only
    acceptable proof of completeness is that the fetch literally contains
    ``total`` customers (len(customers) == total). 20 != 100 -> REJECTED.
    """
    snaps = [
        MiheCustomerSnapshot(
            seq=1,
            is_complete=True,
            customers=tuple(
                _cust(f"c{i}", _NATURAL_JULY_MS) for i in range(20)
            ),
            total=100,
            page=1,
            page_size=20,
        )
    ]
    with pytest.raises(SnapshotPaginationError):
        diff_registrations(snaps)


# ---------------------------------------------------------------------------
# AC19 -- P1 (re-opened) : a correctly aggregated complete fetch is accepted
# ---------------------------------------------------------------------------
def test_ac19_aggregated_complete_fetch_accepted():
    """The fetcher's job is to paginate, merge every page, and hand the engine
    ONE complete snapshot. That aggregated snapshot carries all ``total``
    customers, so len(customers) == total and the engine accepts it.

    Here the aggregated snapshot carries NO page metadata (page_size=None): the
    completeness proof is purely the customer count equaling total.
    """
    snaps = [
        MiheCustomerSnapshot(
            seq=1,
            is_complete=True,
            customers=tuple(
                _cust(f"c{i}", _NATURAL_JULY_MS) for i in range(100)
            ),
            total=100,
            page=1,
            page_size=None,
        )
    ]
    rows = diff_registrations(snaps)
    assert len(rows) == 100


# ---------------------------------------------------------------------------
# AC20 -- P1 (re-opened) : the historical contradiction is resolved
# ---------------------------------------------------------------------------
def test_ac20_aggregated_fetch_with_page_metadata_accepted():
    """The exact case Codex reported as contradictory: the REAL aggregated 100
    customers were being REJECTED because the code demanded that page 1 carry
    exactly 20 customers. Under the corrected model the pagination metadata
    (page=1, page_size=20) is informational only; completeness is proven by
    len(customers) == total (100 == 100). The engine now ACCEPTS, producing
    100 registration rows -- the contradiction is gone.
    """
    snaps = [
        MiheCustomerSnapshot(
            seq=1,
            is_complete=True,
            customers=tuple(
                _cust(f"c{i}", _NATURAL_JULY_MS) for i in range(100)
            ),
            total=100,
            page=1,
            page_size=20,
        )
    ]
    rows = diff_registrations(snaps)
    assert len(rows) == 100
