"""PILOT-2A-3 Registration Diff Engine.

Deterministic, idempotent, fail-closed engine that turns a sequence of Mihe
(AI觅) customer snapshots into registration observations.

Hard contracts (design §11 / Issue #134, PILOT-2A-3):
  C1  read-only input (engine never writes to Mihe; no network calls)
  C2  pagination completeness (a partial page -> fail-closed)
  C3  stable Mihe customer identity (the platform customer id is the key)
  C4  idempotent (same input -> same output set)
  C5  byte-equivalent replay (serialized output identical across runs)
  C6  single registration per customer (not per snapshot)
  C7  updates do not create a new registration (only update the observation)
  C8  UNKNOWN_BATCH_COHORT distinction for suspected platform-batch accounts
  C9  do not label the 9 accounts (no false attribution source)
  C10 do not invent attribution (no synthetic source of any kind)
  C11 missing snapshot (a sequence gap) -> fail-closed, no silent skip
  C12 raw rows do not become KnowledgeFact (operational observation only)
  C13 no Mihe writes (input is read-only)
  C14 no external side effects (no network / subprocess / filesystem-on-input)
  C15 no main model redesign (materializes into the already-approved
      ``RegistrationObservation`` pilot2 table; NO disconnected parallel table)

Persistence target (Codex PILOT-2A-3 review, P1-1):
  The deterministic result is persisted directly into ``RegistrationObservation``
  -- the canonical normalized registration entity that every attribution FK
  points at. A previously-proposed parallel ``RegistrationDiff`` table was
  rejected because it stranded those FKs. The engine also materializes the raw
  evidence (``MiheSnapshot``) so ``RegistrationObservation.source_snapshot_id``
  references a real row and the pagination metadata is persisted for downstream
  audit (P1-3).

Last-seen-sequence monotonicity (P1-2):
  ``persist_diffs`` only advances a registration when the incoming
  ``last_seen_seq`` is STRICTLY greater than the stored one. A stale replay
  (older seq) is a no-op; an equal seq with a *different* observation hash is a
  corruption signal and is refused fail-closed. This prevents an old snapshot
  from reverting a newer observation (balance/seq/version rollback).

Pagination integrity (P1-3):
  ``diff_registrations`` cross-checks the per-page customer count against the
  snapshot's pagination metadata (``total`` / ``page`` / ``page_size``) and
  refuses a complete snapshot whose count is internally inconsistent (e.g.
  total=100, page_size=20 but only 1 customer present). Conflicting customer
  identities across snapshots are also refused. Fail-closed.

The engine is a PURE function over an ordered sequence of snapshots. Its output
is fully determined by the inputs; no wall-clock time enters the deterministic
result.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

# NB: we must use ``sqlmodel.select`` (not ``sqlalchemy.select``) so that a
# SQLModel ``table=True`` class is treated as a mapped ORM entity and the query
# returns instances rather than bare ``Row`` objects. A raw
# ``sqlalchemy.select(RegistrationObservation)`` silently degrades to a Core
# table select and yields ``Row``s that lack model attributes (e.g.
# ``observation_hash``), which breaks the idempotent upsert below.
from sqlmodel import Session, select

from aios.pilot2.models import (
    CohortTag,
    FetchStatus,
    MiheEndpoint,
    MiheSnapshot,
    RegistrationObservation,
)

# ---------------------------------------------------------------------------
# Batch-cohort detection window (design §0.2 / D1)
# ---------------------------------------------------------------------------
# The 9 suspected platform-sent accounts were batch-mounted within a 5-minute
# window on 2026-08-05T01:00~01:05Z, 8/9 never logged in. They are DISTINGUISHED
# (UNKNOWN_BATCH_COHORT) but NEVER labeled with a traffic source. The window is
# derived programmatically so the literal timestamps cannot drift.
_BATCH_WINDOW_START_MS = int(
    datetime(2026, 8, 5, 1, 0, 0, tzinfo=UTC).timestamp() * 1000
)
_BATCH_WINDOW_END_MS = int(
    datetime(2026, 8, 5, 1, 5, 0, tzinfo=UTC).timestamp() * 1000
)


class SnapshotIncompleteError(RuntimeError):
    """Raised when a snapshot's pagination was incomplete (C2)."""


class SnapshotSequenceGapError(RuntimeError):
    """Raised when the snapshot sequence has a gap (C11)."""


class SnapshotPaginationError(RuntimeError):
    """Raised when a snapshot's pagination metadata is internally inconsistent (P1-3)."""


class SnapshotReplayConflictError(RuntimeError):
    """Raised when a replay of the same seq carries a different observation (P1-2)."""


# ---------------------------------------------------------------------------
# Input types (typed Mihe customer snapshot -- read-only)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MiheCustomer:
    """One raw Mihe customer record (read-only engine input).

    Field names mirror the measured AI觅 backend shape
    (see project memory: id / nickname / avatar / phone / balance /
    customerType / registeredAt / lastLoginAt / totalRecharge / rechargeCount).
    ``registered_at_ms`` / ``last_login_at_ms`` are epoch millis as returned by
    the platform.
    """

    id: str
    registered_at_ms: int
    nickname: str | None = None
    avatar: str | None = None
    phone_masked: str | None = None
    balance: int = 0
    customer_type: str | None = None
    last_login_at_ms: int | None = None
    total_recharge: int = 0
    recharge_count: int = 0


@dataclass(frozen=True)
class MiheCustomerSnapshot:
    """One paginated snapshot of the Mihe customer list.

    ``seq`` is the monotonic snapshot sequence number. ``is_complete`` is True
    only when the fetch covered every page (no truncation); the engine refuses
    any snapshot that is not complete (C2). ``total`` / ``page`` / ``page_size``
    are the platform pagination metadata; when present they are cross-checked
    against the customer count (P1-3).
    """

    seq: int
    is_complete: bool
    customers: tuple[MiheCustomer, ...] = field(default_factory=tuple)
    total: int | None = None
    page: int = 1
    page_size: int | None = None


# ---------------------------------------------------------------------------
# Engine output (deterministic, serializable)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RegistrationDiffRow:
    """Deterministic engine output for one customer.

    ``observation_hash`` is derived (in ``__post_init__``) from every other
    field, so identical engine output yields an identical hash -- the basis for
    idempotent persistence (C4/C5). ``first_seen_seq`` / ``last_seen_seq`` are
    included so a later re-observation is a genuine refresh.
    """

    customer_id: str
    first_seen_seq: int
    last_seen_seq: int
    registered_at_ms: int
    last_login_at_ms: int | None
    nickname: str | None
    avatar: str | None
    phone_masked: str | None
    balance: int
    customer_type: str | None
    total_recharge: int
    recharge_count: int
    cohort_tag: CohortTag
    is_batch: bool
    observation_hash: str = field(init=False)

    def __post_init__(self) -> None:
        payload = self._deterministic_payload()
        digest = hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "observation_hash", digest)

    def _deterministic_payload(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "first_seen_seq": self.first_seen_seq,
            "last_seen_seq": self.last_seen_seq,
            "registered_at_ms": self.registered_at_ms,
            "last_login_at_ms": self.last_login_at_ms,
            "nickname": self.nickname,
            "avatar": self.avatar,
            "phone_masked": self.phone_masked,
            "balance": self.balance,
            "customer_type": self.customer_type,
            "total_recharge": self.total_recharge,
            "recharge_count": self.recharge_count,
            "cohort_tag": self.cohort_tag.value,
            "is_batch": self.is_batch,
        }

    def to_json(self) -> str:
        """Byte-stable JSON form (C5)."""
        return json.dumps(self._deterministic_payload(), separators=(",", ":"), sort_keys=True)


# ---------------------------------------------------------------------------
# Engine internals
# ---------------------------------------------------------------------------
class _Acc:
    """Mutable per-customer accumulator used only inside the pure engine."""

    __slots__ = ("first_seen_seq", "last_seen_seq", "first", "last")

    def __init__(self, first_seen_seq: int, first: MiheCustomer) -> None:
        self.first_seen_seq = first_seen_seq
        self.last_seen_seq = first_seen_seq
        self.first = first
        self.last = first


def _classify_cohort(first: MiheCustomer) -> tuple[bool, CohortTag]:
    """Distinguish suspected platform-batch accounts (C8/C9/C10).

    The decision is a PURE function of the registration timestamp window only.
    Batch accounts receive ``UNKNOWN_BATCH_COHORT`` and are flagged
    ``is_batch=True``; they are NEVER assigned a traffic source or channel.
    Natural accounts receive ``NATURAL``. No attribution is invented.
    """
    if _BATCH_WINDOW_START_MS <= first.registered_at_ms <= _BATCH_WINDOW_END_MS:
        return True, CohortTag.UNKNOWN_BATCH_COHORT
    return False, CohortTag.NATURAL


def _validate_sequence(ordered: list[MiheCustomerSnapshot]) -> None:
    """Fail-closed on any gap in the snapshot sequence (C11)."""
    expected = ordered[0].seq
    for snap in ordered:
        if snap.seq != expected:
            raise SnapshotSequenceGapError(
                f"snapshot sequence gap: expected seq={expected} but found seq={snap.seq}; "
                "refusing to diff a missing snapshot (no silent skip)"
            )
        expected += 1


def _validate_pagination_integrity(snapshots: list[MiheCustomerSnapshot]) -> None:
    """P1-3 (re-fixed): completeness is *proven* by the customer count, never
    assumed from a single page.

    A complete fetch must literally contain every customer the platform reports
    via ``total``. The only acceptable proof of completeness is therefore
    ``len(snap.customers) == snap.total`` for each snapshot in the set. This
    closes the fail-open hole where a single consistent page (e.g. total=100,
    page=1, page_size=20, 20 customers) was mistaken for a complete fetch, and
    it resolves the contradiction where a genuinely aggregated 100-customer
    snapshot was wrongly rejected because page 1 "should" have carried only 20.

    The pagination metadata (``page`` / ``page_size``) is informational only --
    it is NOT used to infer an expected per-page count, because a single page's
    length can never substitute for proof of a complete fetch (P1 re-fix).

    On top of the count proof we still:
      * require ``total`` to exist on every snapshot (completeness must be
        verifiable, not merely claimed; without it there is nothing to check
        against, which would let a caller bypass the whole guard);
      * reject the same customer id appearing across snapshots with a conflicting
        ``registered_at`` (duplicate / corrupt identity).

    Note: ``total`` is validated PER SNAPSHOT (len(customers) == total). It is
    deliberately NOT required to be identical across snapshots, because each
    ``seq`` is an independent observation of the customer list over time and the
    population legitimately grows between observations (e.g. seq=1 saw 2
    customers, seq=2 saw 3).
    """
    seen: dict[str, int] = {}  # customer_id -> registered_at_ms (stable identity)
    for snap in snapshots:
        # Completeness must be VERIFIABLE, never merely claimed. Without
        # ``total`` there is nothing to cross-check against, which would let a
        # caller bypass this whole guard by omitting metadata -- refuse instead
        # of trusting the is_complete boolean.
        if snap.total is None:
            raise SnapshotPaginationError(
                f"snapshot seq={snap.seq} page={snap.page} claims is_complete=True "
                "but carries no 'total' pagination metadata, so completeness is "
                "unverifiable; refusing to trust the is_complete flag alone"
            )
        # A complete fetch must contain exactly `total` customers. The
        # pagination metadata (page/page_size) is informational and must NOT be
        # used to relax this requirement: a single page that happens to be the
        # right length for its position is still only one page of the fetch.
        if len(snap.customers) != snap.total:
            raise SnapshotPaginationError(
                f"snapshot seq={snap.seq} page={snap.page}: complete fetch claims "
                f"total={snap.total} but carries only {len(snap.customers)} customers; "
                f"a complete snapshot must contain exactly total customers "
                f"(len(customers) == total); refusing a partial/truncated fetch "
                f"presented as complete"
            )
        # Stable identity: the same customer id must carry a consistent
        # registered_at everywhere it appears (duplicate / corrupt identity).
        for cust in snap.customers:
            prev = seen.get(cust.id)
            if prev is None:
                seen[cust.id] = cust.registered_at_ms
            elif prev != cust.registered_at_ms:
                raise SnapshotPaginationError(
                    f"customer {cust.id} appears in multiple snapshots with "
                    f"conflicting registered_at ({prev} vs {cust.registered_at_ms}); "
                    f"refusing corrupt snapshot set"
                )


def diff_registrations(
    snapshots: Sequence[MiheCustomerSnapshot],
) -> list[RegistrationDiffRow]:
    """Pure, deterministic diff of Mihe snapshots into registration rows.

    Contracts enforced:
      C2  pagination completeness (any incomplete snapshot -> SnapshotIncompleteError)
      C11 missing snapshot (sequence gap -> SnapshotSequenceGapError)
      C3  stable identity (keyed by customer.id)
      C6  single registration per customer
      C7  updates only update the observation, never create a new registration
      C8/C9/C10 UNKNOWN_BATCH_COHORT, never labeled, never invented
      C4/C5 output is sorted by customer_id and every row carries a
             deterministic observation_hash, so replay is byte-equivalent
      P1-3 pagination integrity (inconsistent counts / conflicting identity ->
            SnapshotPaginationError)
    """
    if not snapshots:
        return []

    ordered = sorted(snapshots, key=lambda s: s.seq)
    _validate_sequence(ordered)

    # C2 first: an explicitly-incomplete snapshot is refused as *incomplete*,
    # before the pagination-integrity guard reports it as a metadata problem.
    for snap in ordered:
        if not snap.is_complete:
            raise SnapshotIncompleteError(
                f"snapshot seq={snap.seq} is incomplete (partial page); "
                "refusing to diff truncated data"
            )

    _validate_pagination_integrity(ordered)

    by_customer: dict[str, _Acc] = {}
    for snap in ordered:
        for cust in snap.customers:
            acc = by_customer.get(cust.id)
            if acc is None:
                by_customer[cust.id] = _Acc(first_seen_seq=snap.seq, first=cust)
            else:
                # C7: a later appearance only refreshes the latest-observed
                # values; the registration event (first_seen) is frozen.
                acc.last = cust
                acc.last_seen_seq = snap.seq

    rows: list[RegistrationDiffRow] = []
    for cust_id in sorted(by_customer):  # C4/C5: deterministic order
        acc = by_customer[cust_id]
        first = acc.first
        last = acc.last
        is_batch, cohort = _classify_cohort(first)
        rows.append(
            RegistrationDiffRow(
                customer_id=cust_id,
                first_seen_seq=acc.first_seen_seq,
                last_seen_seq=acc.last_seen_seq,
                registered_at_ms=first.registered_at_ms,
                last_login_at_ms=last.last_login_at_ms,
                nickname=last.nickname,
                avatar=last.avatar,
                phone_masked=last.phone_masked,
                balance=last.balance,
                customer_type=last.customer_type,
                total_recharge=last.total_recharge,
                recharge_count=last.recharge_count,
                cohort_tag=cohort,
                is_batch=is_batch,
            )
        )
    return rows


def serialize_diffs(rows: Sequence[RegistrationDiffRow]) -> str:
    """Byte-stable JSON of a diff result list (C5)."""
    return "[" + ",".join(row.to_json() for row in rows) + "]"


# ---------------------------------------------------------------------------
# Raw-evidence + normalized-observation persistence (C4/C5/C6/C7 + P1-1/P1-2/P1-3)
# ---------------------------------------------------------------------------
def _ms_to_dt(ms: int) -> datetime:
    """Epoch-millis -> timezone-aware UTC ``datetime`` (lossless)."""
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=ms)


def _snapshot_raw_hash(snap: MiheCustomerSnapshot) -> str:
    """Deterministic content hash of a raw snapshot (idempotent MiheSnapshot key)."""
    payload = {
        "seq": snap.seq,
        "page": snap.page,
        "page_size": snap.page_size,
        "total": snap.total,
        "customers": [
            {
                "id": c.id,
                "registered_at_ms": c.registered_at_ms,
                "balance": c.balance,
                "total_recharge": c.total_recharge,
            }
            for c in sorted(snap.customers, key=lambda x: x.id)
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _snapshot_payload(snap: MiheCustomerSnapshot) -> dict:
    """Full customer payload stored in ``MiheSnapshot.raw_payload`` for audit."""
    return {
        "seq": snap.seq,
        "page": snap.page,
        "page_size": snap.page_size,
        "total": snap.total,
        "is_complete": snap.is_complete,
        "customers": [
            {
                "id": c.id,
                "registered_at_ms": c.registered_at_ms,
                "nickname": c.nickname,
                "avatar": c.avatar,
                "phone_masked": c.phone_masked,
                "balance": c.balance,
                "customer_type": c.customer_type,
                "last_login_at_ms": c.last_login_at_ms,
                "total_recharge": c.total_recharge,
                "recharge_count": c.recharge_count,
            }
            for c in snap.customers
        ],
    }


def persist_diffs(
    engine,
    snapshots: Sequence[MiheCustomerSnapshot],
    rows: Iterable[RegistrationDiffRow] | None = None,
) -> int:
    """Idempotent materialization of diff rows into ``RegistrationObservation``.

    Same engine output -> same rows -> same ``observation_hash`` -> upsert key,
    so re-running on identical snapshots is a no-op (C4/C5). A changed
    observation for the same customer (new hash, strictly later seq) bumps
    ``version`` and refreshes the latest values while keeping the first-seen
    registration frozen (C6/C7).

    P1-1: target is the already-approved ``RegistrationObservation`` (every
    attribution FK points at it). The raw evidence (``MiheSnapshot``) is also
    materialized so ``source_snapshot_id`` references a real row and the
    pagination metadata is persisted for downstream audit (P1-3).

    P1-2: a registration is only advanced when the incoming ``last_seen_seq`` is
    STRICTLY greater than the stored one. A stale (older) replay is a no-op and
    cannot revert a newer observation; an equal seq with a *different* hash is a
    corruption signal and is refused fail-closed.

    Returns the number of rows inserted or advanced.
    """
    if rows is None:
        rows = diff_registrations(snapshots)
    rows = list(rows)
    if not rows:
        return 0
    changed = 0
    with Session(engine) as session:
        # 1) Materialize raw evidence (MiheSnapshot) per input snapshot,
        #    idempotent by content hash. This populates the raw layer that
        #    RegistrationObservation.source_snapshot_id references and persists
        #    the pagination metadata for downstream audit (P1-3).
        snap_id_by_seq: dict[int, str] = {}
        for snap in sorted(snapshots, key=lambda s: s.seq):
            raw_hash = _snapshot_raw_hash(snap)
            existing_snap = session.exec(
                select(MiheSnapshot).where(MiheSnapshot.raw_hash == raw_hash)
            ).one_or_none()
            if existing_snap is None:
                snap_id = f"msnap_{raw_hash[:16]}"
                session.add(
                    MiheSnapshot(
                        id=snap_id,
                        endpoint=MiheEndpoint.CUSTOMERS,
                        page=snap.page,
                        total_count=snap.total or 0,
                        raw_payload=_snapshot_payload(snap),
                        fetch_status=FetchStatus.OK,
                        raw_hash=raw_hash,
                    )
                )
                snap_id_by_seq[snap.seq] = snap_id
            else:
                snap_id_by_seq[snap.seq] = existing_snap.id

        # 2) Upsert normalized RegistrationObservation (C6/C7 + P1-2).
        for row in rows:
            existing = session.exec(
                select(RegistrationObservation).where(
                    RegistrationObservation.customer_id == row.customer_id
                )
            ).one_or_none()
            src_id = snap_id_by_seq.get(row.last_seen_seq)
            if src_id is None:
                # No raw evidence for this row's snapshot -> the FK would be
                # NULL and the registration could never be joined back to the
                # snapshot it came from. Fail closed with a precise error rather
                # than letting it surface as an opaque IntegrityError.
                raise SnapshotIncompleteError(
                    f"customer {row.customer_id}: diff row references snapshot "
                    f"seq={row.last_seen_seq}, which is absent from the given "
                    "snapshot set; refusing to persist an observation without "
                    "its raw evidence"
                )
            if existing is None:
                session.add(
                    RegistrationObservation(
                        customer_id=row.customer_id,
                        registered_at=_ms_to_dt(row.registered_at_ms),
                        last_login_at=(
                            _ms_to_dt(row.last_login_at_ms)
                            if row.last_login_at_ms is not None
                            else None
                        ),
                        total_recharge=row.total_recharge,
                        recharge_count=row.recharge_count,
                        balance=row.balance,
                        cohort_tag=row.cohort_tag,
                        is_batch=row.is_batch,
                        source_snapshot_id=src_id,
                        observation_hash=row.observation_hash,
                        first_seen_seq=row.first_seen_seq,
                        last_seen_seq=row.last_seen_seq,
                        version=1,
                        nickname=row.nickname,
                        avatar=row.avatar,
                        phone_masked=row.phone_masked,
                        customer_type=row.customer_type,
                    )
                )
                changed += 1
            else:
                # P1-2: monotonic last_seen_seq. An older or equal-seq replay must
                # not revert the observation. Stale (<) is a no-op; equal with an
                # identical hash is idempotent; equal with a DIFFERENT hash is a
                # corruption signal -> fail-closed.
                if row.last_seen_seq < existing.last_seen_seq:
                    continue  # stale replay -> no-op (no revert)
                if row.last_seen_seq == existing.last_seen_seq:
                    if row.observation_hash == existing.observation_hash:
                        continue  # identical, idempotent
                    raise SnapshotReplayConflictError(
                        f"customer {row.customer_id}: snapshot seq={row.last_seen_seq} "
                        f"replayed with a different observation than previously "
                        f"persisted for the same seq; refusing corrupt replay"
                    )
                # row.last_seen_seq > existing.last_seen_seq
                if row.observation_hash != existing.observation_hash:
                    # C7: refresh latest values; first-seen registration frozen.
                    existing.last_login_at = (
                        _ms_to_dt(row.last_login_at_ms)
                        if row.last_login_at_ms is not None
                        else None
                    )
                    existing.nickname = row.nickname
                    existing.avatar = row.avatar
                    existing.phone_masked = row.phone_masked
                    existing.customer_type = row.customer_type
                    existing.balance = row.balance
                    existing.total_recharge = row.total_recharge
                    existing.recharge_count = row.recharge_count
                    existing.cohort_tag = row.cohort_tag
                    existing.is_batch = row.is_batch
                    existing.observation_hash = row.observation_hash
                    existing.version += 1
                # Always advance the pointer to the latest observed seq.
                existing.last_seen_seq = row.last_seen_seq
                existing.source_snapshot_id = src_id
                changed += 1
        session.commit()
    return changed


def run_registration_diff(
    engine, snapshots: Sequence[MiheCustomerSnapshot]
) -> tuple[list[RegistrationDiffRow], int]:
    """Compute the diff and persist it idempotently. Convenience entry point.

    C1/C13/C14: this function performs no writes to Mihe and no network calls;
    it only reads the in-memory ``snapshots`` and writes to the local pilot2
    staging database (raw ``MiheSnapshot`` + normalized ``RegistrationObservation``).
    """
    rows = diff_registrations(snapshots)
    changed = persist_diffs(engine, snapshots, rows)
    return rows, changed
