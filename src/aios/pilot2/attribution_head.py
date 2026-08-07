"""Authorised persistence service for PILOT-2A final attribution (contract D2).

Attribution has TWO separate questions, and this package answers them with two
separate structures instead of one mutable flag:

    *What was concluded, and why?*   ``FinalAttributionDecision`` -- immutable,
                                     append-only history. Written once, never
                                     updated, never deleted.
    *What is the answer right now?*  ``FinalAttributionHead`` -- exactly ONE row
                                     per registration, keyed BY the registration
                                     id, pointing at the current decision.

The earlier design encoded currency as a per-row lifecycle flag on a single
table. Adversarial review defeated it with a SPLIT-TRANSACTION attack: commit
the successor row in transaction 1, then demote the predecessor in transaction
2, and the database is left in a committed state with zero current heads. A
per-row flag can always be moved one transaction at a time, so no additional
trigger could repair it.

Under the head model the attack has no verbs left. Appending a decision without
moving the pointer changes nothing (the successor simply is not current), and
the pointer cannot be deleted, nulled, or moved to a decision that does not
supersede the current one. Replacement is therefore ONE transaction that
inserts the successor and advances the single pointer together; if any part
fails, the whole thing rolls back and the previous head survives untouched.
There is no demote step, so there is never a window with no head.

This module is the only authorised writer. The database still enforces every
invariant on its own (see :mod:`aios.pilot2.models`), so a rogue writer using
raw SQL cannot bypass the rules by refusing to call these functions -- the
service exists to make the *legal* transitions convenient and atomic, not to be
the sole line of defence.
"""
from __future__ import annotations

from sqlalchemy import select as sa_select
from sqlmodel import Session, select

from aios.pilot2.models import (
    FinalAttributionDecision,
    FinalAttributionHead,
    RegistrationAttributionLevel,
    new_id,
    now_utc,
)

__all__ = [
    "AttributionHeadError",
    "current_decision",
    "decision_history",
    "finalize_attribution",
    "replace_attribution",
    "unattached_decisions",
]

_DECISION = FinalAttributionDecision.__table__
_HEAD = FinalAttributionHead.__table__


class AttributionHeadError(RuntimeError):
    """A caller asked for a transition the single-head model does not allow."""


def _read_head_decision_id(conn, registration_observation_id: str) -> str | None:
    row = conn.execute(
        sa_select(_HEAD.c.decision_id).where(
            _HEAD.c.registration_observation_id == registration_observation_id
        )
    ).first()
    return None if row is None else row[0]


def finalize_attribution(
    engine,
    *,
    registration_observation_id: str,
    proposal_id: str,
    level: RegistrationAttributionLevel,
    decided_by: str,
    reason: str | None = None,
) -> str:
    """Record the FIRST decision for a registration and make it current.

    One atomic transaction: append the decision, create the single head row.
    Finalizing an already-finalized registration is refused -- every later
    conclusion is a :func:`replace_attribution`, which keeps the predecessor.

    Returns the new decision id.
    """
    level = RegistrationAttributionLevel(level)
    decision_id = new_id("fdec")
    decided_at = now_utc()

    with engine.begin() as conn:
        if _read_head_decision_id(conn, registration_observation_id) is not None:
            raise AttributionHeadError(
                f"registration {registration_observation_id!r} is already finalized; "
                "use replace_attribution() to supersede the current decision"
            )
        conn.execute(
            _DECISION.insert().values(
                id=decision_id,
                proposal_id=proposal_id,
                registration_observation_id=registration_observation_id,
                level=level,
                supersedes_decision_id=None,
                decided_at=decided_at,
                decided_by=decided_by,
                reason=reason,
            )
        )
        conn.execute(
            _HEAD.insert().values(
                registration_observation_id=registration_observation_id,
                decision_id=decision_id,
                updated_at=decided_at,
            )
        )
    return decision_id


def replace_attribution(
    engine,
    *,
    registration_observation_id: str,
    proposal_id: str,
    level: RegistrationAttributionLevel,
    decided_by: str,
    reason: str | None = None,
) -> str:
    """Supersede the current decision with a new one, atomically.

    The predecessor is read first and then PINNED: the successor declares it via
    ``supersedes_decision_id`` and the head pointer is advanced with a
    compare-and-set on that same value. That compare-and-set is the ONLY
    arbiter. Two racing replacements both append a successor of the same
    predecessor; the one that finds the pointer already moved raises and rolls
    its own INSERT back, so exactly one winner is persisted and the loser leaves
    nothing behind.

    An earlier revision also leaned on a unique index over
    ``supersedes_decision_id``. It was removed (see
    :class:`~aios.pilot2.models.FinalAttributionDecision`) because it made the
    supersession claim an exclusive RESERVATION: a writer outside this module
    could append a successor of the current head without moving the head, and
    from then on every call here died on that index -- before the
    compare-and-set -- leaving the head permanently unreplaceable. Now such a
    rival claim neither blocks this function nor gets promoted by it: a fresh
    successor row is appended, the head advances to THAT row, and the rival
    stays inert (never current, never in :func:`decision_history`) and
    auditable through :func:`unattached_decisions`.

    Returns the new decision id.
    """
    level = RegistrationAttributionLevel(level)
    with engine.connect() as conn:
        previous_id = _read_head_decision_id(conn, registration_observation_id)
    if previous_id is None:
        raise AttributionHeadError(
            f"registration {registration_observation_id!r} has no current attribution; "
            "call finalize_attribution() first"
        )

    decision_id = new_id("fdec")
    decided_at = now_utc()

    with engine.begin() as conn:
        conn.execute(
            _DECISION.insert().values(
                id=decision_id,
                proposal_id=proposal_id,
                registration_observation_id=registration_observation_id,
                level=level,
                supersedes_decision_id=previous_id,
                decided_at=decided_at,
                decided_by=decided_by,
                reason=reason,
            )
        )
        moved = conn.execute(
            _HEAD.update()
            .where(_HEAD.c.registration_observation_id == registration_observation_id)
            .where(_HEAD.c.decision_id == previous_id)
            .values(decision_id=decision_id, updated_at=decided_at)
        )
        if moved.rowcount != 1:
            raise AttributionHeadError(
                f"concurrent replacement for {registration_observation_id!r}: the head "
                f"moved away from {previous_id!r}; nothing was persisted"
            )
    return decision_id


def current_decision(engine, registration_observation_id: str) -> FinalAttributionDecision | None:
    """The decision the single head points at, or ``None`` if not finalized."""
    with Session(engine) as session:
        head = session.get(FinalAttributionHead, registration_observation_id)
        if head is None:
            return None
        return session.get(FinalAttributionDecision, head.decision_id)


def _load_decisions(
    session, registration_observation_id: str
) -> dict[str, FinalAttributionDecision]:
    rows = session.exec(
        select(FinalAttributionDecision).where(
            FinalAttributionDecision.registration_observation_id == registration_observation_id
        )
    ).all()
    return {row.id: row for row in rows}


def _history_from_head(
    head_decision_id: str,
    rows: dict[str, FinalAttributionDecision],
    registration_observation_id: str,
) -> list[FinalAttributionDecision]:
    chain: list[FinalAttributionDecision] = []
    seen: set[str] = set()
    node_id: str | None = head_decision_id
    while node_id is not None:
        if node_id in seen:
            raise AttributionHeadError(
                f"attribution history of {registration_observation_id!r} contains a cycle at "
                f"{node_id!r}; refusing to report a history that cannot be ordered"
            )
        node = rows.get(node_id)
        if node is None:
            raise AttributionHeadError(
                f"attribution history of {registration_observation_id!r} is broken: decision "
                f"{node_id!r} is missing; refusing to report a partial history"
            )
        seen.add(node_id)
        chain.append(node)
        node_id = node.supersedes_decision_id
    chain.reverse()
    return chain


def decision_history(engine, registration_observation_id: str) -> list[FinalAttributionDecision]:
    """The decisions that were ever CURRENT for a registration, oldest first.

    Reconstructed by walking BACKWARDS from the single head pointer along
    ``supersedes_decision_id``. Backwards is the only deterministic direction:
    a decision has at most one predecessor, whereas several rival decisions may
    legally claim the same predecessor (the exclusive reservation that used to
    forbid that was a denial-of-service vector -- see
    :func:`replace_attribution`). Walking forwards from a root would therefore
    have to choose between siblings; walking backwards never does.

    Rival claims that never became current are NOT history and are deliberately
    not mixed in here -- :func:`unattached_decisions` reports them instead, so
    they are neither hidden nor confused with the real chain.

    Returns ``[]`` for a registration that was never finalized. Raises
    :class:`AttributionHeadError` (fail-closed) if the chain is broken or
    cyclic, rather than returning a partial or arbitrarily-ordered reading.
    """
    with Session(engine) as session:
        head = session.get(FinalAttributionHead, registration_observation_id)
        if head is None:
            return []
        rows = _load_decisions(session, registration_observation_id)
        return _history_from_head(head.decision_id, rows, registration_observation_id)


def unattached_decisions(
    engine, registration_observation_id: str
) -> list[FinalAttributionDecision]:
    """Decisions recorded for a registration that are not part of its history.

    Normal operation returns ``[]``: every decision this module writes either
    becomes the head immediately (:func:`finalize_attribution`) or is written in
    the same transaction that advances the head to it
    (:func:`replace_attribution`).

    A non-empty result means somebody appended to the immutable decision table
    without moving the head -- possible, because the table is an open append-only
    log and the database is not an authorisation boundary. Those rows are inert:
    they are not current, they never appear in :func:`decision_history`, and
    they cannot block a replacement. They are surfaced here, in a deterministic
    order, so a staging operator can audit them on purpose instead of
    discovering them by accident. Nothing in this module ever promotes one.
    """
    with Session(engine) as session:
        rows = _load_decisions(session, registration_observation_id)
        if not rows:
            return []
        head = session.get(FinalAttributionHead, registration_observation_id)
        attached: set[str] = set()
        if head is not None:
            attached = {
                item.id
                for item in _history_from_head(
                    head.decision_id, rows, registration_observation_id
                )
            }
        return sorted(
            (row for row in rows.values() if row.id not in attached),
            key=lambda row: (row.decided_at, row.id),
        )
