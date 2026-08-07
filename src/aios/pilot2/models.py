"""PILOT-2A data models (RAW EVIDENCE LAYER + Attribution + taxonomy + experiments).

DESIGN-ONLY intent, but the tables are real and live in an *independent*
SQLAlchemy ``MetaData`` (``pilot2_metadata``) so they never touch the main
AIOS alembic head or production schema. Tables are created only against the
staging database via :mod:`aios.pilot2.migrations_create_all` (fail-closed
guard refuses any non-staging URL).

Layering (design v2 §4.1):
    RAW EVIDENCE LAYER (immutable, append-only)
      MiheSnapshot / PublicationEvent / ClickEvent / PlatformMetricSnapshot
    -> Normalized Observation
      RegistrationObservation (+ trivial normalized views of the above
      when RAW is already structured)
    -> Attribution (AttributionProposal -> FinalAttributionDecision
                    + FinalAttributionHead)
    -> Analysis / KnowledgeFact (later phases; not modelled here)

Hard contracts enforced at the *database boundary* (design §11):
    D1  no VERIFIED_DIRECT / HIGH person-level registration attribution may be
        persisted before G3; the only persisted attribution results are
        EXPERIMENT_ASSOCIATED / AMBIGUOUS / UNATTRIBUTED (CLICK_ASSOCIATED is
        click-EVIDENCE only, never an attribution result).
    D2  attribution currency is STRUCTURAL, not a lifecycle flag: an immutable
        append-only ``FinalAttributionDecision`` history plus exactly one
        ``FinalAttributionHead`` row per registration (the registration id IS
        the head primary key). Two heads, zero heads, forged supersession and
        rewritten history are therefore unrepresentable rather than merely
        guarded, and replacement is one atomic transaction with no intermediate
        state to commit.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import (
    DDL,
    JSON,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    MetaData,
    UniqueConstraint,
    event,
)
from sqlmodel import Field, SQLModel

from aios.pilot2.vocabulary import CONTENT_TAXONOMY_VERSION

# Independent metadata: pilot2 tables are NOT registered on SQLModel.metadata,
# so main migrations / production schema are never affected by this package.
pilot2_metadata = MetaData()


def now_utc() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class Pilot2Base(SQLModel):
    """Base for all pilot2 tables; binds them to ``pilot2_metadata``."""

    metadata = pilot2_metadata


# ---------------------------------------------------------------------------
# DB-boundary constraint helpers
# ---------------------------------------------------------------------------
# STORAGE NOTE: SQLModel maps an ``Enum``-annotated column to ``sqlalchemy.Enum``,
# which persists the member *name* (``'EXPERIMENT_ASSOCIATED'``), not the
# ``StrEnum`` value (``'experiment_associated'``). Every SQL-level gate below
# therefore compares member NAMES, and ``test_pilot2_models`` pins that
# representation so the gates can never be silently disabled by an ORM change.


def _allowed_names_sql(enum_cls: type[StrEnum]) -> str:
    """SQL literal list of the member names an enum column may hold."""
    return ", ".join(f"'{member.name}'" for member in enum_cls)


# D1: tokens that must never appear in a persisted attribution level, in any
# casing. Spelled out literally (not derived) so the contract stays readable at
# review time and survives any future edit to the Python enums.
FORBIDDEN_ATTRIBUTION_TOKENS: tuple[str, ...] = (
    "VERIFIED_DIRECT",
    "verified_direct",
    "DIRECT",
    "direct",
    "HIGH",
    "high",
)

# C1: click evidence is not a person-level registration attribution result.
FORBIDDEN_FINAL_ATTRIBUTION_TOKENS: tuple[str, ...] = (
    *FORBIDDEN_ATTRIBUTION_TOKENS,
    "CLICK_ASSOCIATED",
    "click_associated",
)


def _forbidden_sql(tokens: tuple[str, ...]) -> str:
    return ", ".join(f"'{token}'" for token in tokens)


# ---------------------------------------------------------------------------
# Enums (StrEnum -> stored as plain VARCHAR, mirroring aios.models convention)
# ---------------------------------------------------------------------------
class MiheEndpoint(StrEnum):
    CUSTOMERS = "customers"
    EARNINGS = "earnings"
    FLOW = "flow"


class FetchStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class Channel(StrEnum):
    WECHAT = "wechat"
    WECHAT_GROUP = "wechat_group"
    XHS = "xhs"
    OTHER = "other"


class Platform(StrEnum):
    WECHAT = "wechat"
    XHS = "xhs"


class CohortTag(StrEnum):
    NATURAL = "natural"
    UNKNOWN_BATCH_COHORT = "unknown_batch_cohort"


class AttributionLevel(StrEnum):
    """Proposal-level attribution level (click-EVIDENCE aware).

    ``VERIFIED_DIRECT`` is deliberately absent (D1): until G3 proves Mihe
    registers carry campaign/ref, no HIGH/DIRECT person-level registration
    attribution value may exist in the persisted vocabulary. ``CLICK_ASSOCIATED``
    is allowed here because a *proposal* may record click evidence, but it is
    NEVER a persisted ``FinalAttribution`` result (see ``RegistrationAttributionLevel``
    and C1).
    """

    CLICK_ASSOCIATED = "click_associated"
    EXPERIMENT_ASSOCIATED = "experiment_associated"
    AMBIGUOUS = "ambiguous"
    UNATTRIBUTED = "unattributed"


class RegistrationAttributionLevel(StrEnum):
    """The ONLY legal persisted ``FinalAttribution`` results (D1).

    ``CLICK_ASSOCIATED`` is excluded by construction (C1): it is click-layer
    evidence, not a person-level registration attribution result. The type
    system makes the illegal state unrepresentable, and a DB CHECK constraint
    (``ck_fattr_level_gate``) backs it up at the persistence boundary.
    """

    EXPERIMENT_ASSOCIATED = "experiment_associated"
    AMBIGUOUS = "ambiguous"
    UNATTRIBUTED = "unattributed"


class ExperimentStatus(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    CONCLUDED = "concluded"


# ---------------------------------------------------------------------------
# RAW EVIDENCE LAYER (immutable, append-only)
# ---------------------------------------------------------------------------
class MiheSnapshot(Pilot2Base, table=True):
    id: str = Field(default_factory=lambda: new_id("msnap"), primary_key=True)
    taken_at: datetime = Field(default_factory=now_utc)
    endpoint: MiheEndpoint
    page: int = Field(default=1)
    total_count: int = Field(default=0)
    raw_payload: dict = Field(default_factory=dict, sa_type=JSON)
    fetch_status: FetchStatus = Field(default=FetchStatus.OK)
    raw_hash: str = Field(index=True, unique=True)


class PublicationEvent(Pilot2Base, table=True):
    """A content publication record (V0 hand-entered; owner confirms)."""

    id: str = Field(default_factory=lambda: new_id("pub"), primary_key=True)
    channel: Channel
    published_at: datetime = Field(default_factory=now_utc)
    content_artifact_id: str | None = Field(default=None)
    campaign_link_id: str | None = Field(default=None)
    experiment_id: str | None = Field(default=None)
    title_snapshot: str = Field(default="")
    owner_confirmed: bool = Field(default=False)
    raw_input: dict = Field(default_factory=dict, sa_type=JSON)


class ClickEvent(Pilot2Base, table=True):
    """Campaign-level click. Table modelled now; real population waits 2B-2.

    ``ip_hash`` / ``ua_hash`` store only hashes (non-reversible), no raw PII.
    """

    id: str = Field(default_factory=lambda: new_id("clk"), primary_key=True)
    captured_at: datetime = Field(default_factory=now_utc)
    campaign_code: str = Field(index=True)
    channel: Channel
    link_id: str | None = Field(default=None)
    ip_hash: str | None = Field(default=None)
    ua_hash: str | None = Field(default=None)
    referrer: str | None = Field(default=None)


class PlatformMetricSnapshot(Pilot2Base, table=True):
    """Platform exposure/reading metrics (V0 hand-entered, G4 in design)."""

    id: str = Field(default_factory=lambda: new_id("pms"), primary_key=True)
    platform: Platform
    metric_date: date
    raw_payload: dict = Field(default_factory=dict, sa_type=JSON)
    entered_by: str = Field(default="")
    entered_at: datetime = Field(default_factory=now_utc)
    source: str = Field(default="manual_entry")


# ---------------------------------------------------------------------------
# Normalized Observation (derived, fully recomputable)
# ---------------------------------------------------------------------------
class RegistrationObservation(Pilot2Base, table=True):
    """One registration normalised out of a MiheSnapshot diff.

    ``cohort_tag`` / ``is_batch`` implement the UNKNOWN_BATCH_COHORT rule
    (design §0.2 / D1): batch accounts are excluded from attribution and
    every business count, retained only for audit.

    PILOT-2A-3 materialization (review fix): the deterministic diff engine
    persists its result directly into THIS canonical table -- the one every
    attribution FK points at (``attributionproposal.registration_observation_id``,
    ``finalattributiondecision.registration_observation_id``,
    ``finalattributionhead.registration_observation_id``). A disconnected
    parallel table would strand those FKs, so the engine extends (does NOT
    redesign) this pilot2 table with the fields it needs:

      * ``observation_hash`` -- deterministic idempotency token (sha256 of the
        engine's frozen output), so re-running on identical snapshots is a no-op.
      * ``first_seen_seq`` / ``last_seen_seq`` -- the snapshot sequence window
        the registration was observed in (first-seen frozen, C7).
      * ``version`` -- bumped only when a later, materially-different observation
        is persisted.
      * ``nickname`` / ``avatar`` / ``phone_masked`` / ``customer_type`` --
        descriptive evidence retained for the operational observation.

    ``customer_id`` is UNIQUE: one registration per customer (C6). The diff
    engine writes the raw evidence (``MiheSnapshot``) too, so
    ``source_snapshot_id`` always references a real row.
    """

    id: str = Field(default_factory=lambda: new_id("regob"), primary_key=True)
    customer_id: str = Field(index=True, unique=True)
    registered_at: datetime
    last_login_at: datetime | None = Field(default=None)
    total_recharge: int = Field(default=0)
    recharge_count: int = Field(default=0)
    balance: int = Field(default=0)
    cohort_tag: CohortTag = Field(default=CohortTag.NATURAL)
    is_batch: bool = Field(default=False)
    source_snapshot_id: str = Field(foreign_key="mihesnapshot.id")
    derived_at: datetime = Field(default_factory=now_utc)
    # --- PILOT-2A-3 diff-engine materialization fields (extension, not redesign) ---
    observation_hash: str | None = Field(default=None, index=True)
    first_seen_seq: int = Field(default=0)
    last_seen_seq: int = Field(default=0)
    version: int = Field(default=1)
    nickname: str | None = Field(default=None)
    avatar: str | None = Field(default=None)
    phone_masked: str | None = Field(default=None)
    customer_type: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# Attribution (dual table: recomputable Proposal + immutable Final)
# ---------------------------------------------------------------------------
class AttributionProposal(Pilot2Base, table=True):
    """Recomputable attribution suggestion.

    ``input_hash`` makes recomputation idempotent: same inputs -> same hash ->
    same row (upsert key). Recomputed on every new evidence arrival.

    D1 gate: a proposal may carry ``CLICK_ASSOCIATED`` (click evidence) but the
    persisted vocabulary is checked at the DB boundary (``ck_aprop_level_gate``)
    so a rogue writer cannot persist ``VERIFIED_DIRECT`` (removed from the enum)
    or any other forbidden value.
    """

    id: str = Field(default_factory=lambda: new_id("aprop"), primary_key=True)
    registration_observation_id: str = Field(
        foreign_key="registrationobservation.id", index=True
    )
    content_id: str | None = Field(default=None)
    level: AttributionLevel
    evidence_json: dict = Field(default_factory=dict, sa_type=JSON)
    input_hash: str = Field(index=True, unique=True)
    computed_at: datetime = Field(default_factory=now_utc)

    __table_args__ = (
        # Enables the composite FK below: a proposal is uniquely identified by
        # (id, registration_observation_id) so a FinalAttribution can only
        # finalize a proposal for the registration it was actually proposed for
        # (D2 / C2).
        UniqueConstraint("id", "registration_observation_id", name="uq_aprop_id_reg"),
        # Allow-list gate, derived from the enum so the two can never drift.
        CheckConstraint(
            f"level IN ({_allowed_names_sql(AttributionLevel)})",
            name="ck_aprop_level_gate",
        ),
        # Explicit D1 deny-list: no VERIFIED_DIRECT / DIRECT / HIGH value may
        # ever be written, whatever the enum happens to contain.
        CheckConstraint(
            f"level NOT IN ({_forbidden_sql(FORBIDDEN_ATTRIBUTION_TOKENS)})",
            name="ck_aprop_no_direct_attribution",
        ),
    )


class FinalAttributionDecision(Pilot2Base, table=True):
    """Immutable, append-only attribution DECISION (the HISTORY layer).

    A decision records *what was concluded and why* for one registration. It is
    written once and never mutated: there is no lifecycle column to flip and no
    ``superseded_by`` pointer to forge after the fact. Supersession is declared
    by the SUCCESSOR (``supersedes_decision_id``), so each link of the chain is
    written exactly once, at the moment the successor is created.

    Currency -- "which decision is the answer right now?" -- is deliberately NOT
    stored here. It lives in :class:`FinalAttributionHead`. That separation is
    the whole point of the design: the earlier shape encoded currency as a
    mutable per-row lifecycle flag, and adversarial review defeated it with a
    SPLIT-TRANSACTION attack (commit the successor in transaction 1, demote the
    predecessor in transaction 2, and the committed state has no current head at
    all). A per-row flag can always be moved one transaction at a time; a single
    pointer row cannot.

    DB-boundary guarantees (D2), all enforced by the database itself so a rogue
    writer using raw SQL cannot escape them:

    1. same-registration identity -- composite FK ``fk_fdec_proposal_reg``
       targets ``attributionproposal(id, registration_observation_id)``, so
       finalizing proposal P (computed for registration A) against registration
       B is a foreign-key violation, not a service-layer convention. This is the
       cross-registration protection carried over from the previous design; it
       is NOT weakened to a document-level check.
    2. same-registration supersession -- composite self-FK
       ``fk_fdec_supersedes_same_reg`` targets
       ``finalattributiondecision(id, registration_observation_id)``, so a
       decision may only supersede a decision of its OWN registration.
    3. no self-supersession -- ``ck_fdec_no_self_supersession``.
    4. supersession claims are NOT exclusive -- ``ix_fdec_supersedes`` is a
       PLAIN lookup index. An earlier revision made it a partial UNIQUE index
       ("a predecessor may be superseded at most once") to keep history linear.
       Adversarial review showed that turned a claim into a RESERVATION that any
       writer could take and never release: append a successor of the current
       head WITHOUT moving the head, and the authorised
       :func:`~aios.pilot2.attribution_head.replace_attribution` can never
       insert its own successor of that head again -- every later attempt dies
       on the unique index before the head compare-and-set is even reached, so
       the current head becomes permanently unreplaceable. Availability of the
       authorised path is an integrity property, so the reservation is gone.
       Linearity of the history that actually matters -- the sequence of
       decisions that were ever CURRENT -- does not depend on it: see
       ``trg_fhead_forward_only`` below. A rival claim on the current head is
       therefore representable, inert (it is not current), invisible to the
       reconstructed history, and unable to block anyone. It is reported as an
       UNATTACHED decision by
       :func:`~aios.pilot2.attribution_head.unattached_decisions`.
    5. immutability -- trigger ``trg_fdec_immutable`` aborts EVERY update, so
       history can never be silently rewritten.
    6. retention -- trigger ``trg_fdec_no_delete`` aborts every delete.
    7. D1 / C1 vocabulary gate -- ``ck_fdec_level_gate`` (allow-list derived
       from :class:`RegistrationAttributionLevel`) and
       ``ck_fdec_no_direct_attribution`` (explicit deny-list).
    """

    id: str = Field(default_factory=lambda: new_id("fdec"), primary_key=True)
    proposal_id: str = Field(foreign_key="attributionproposal.id", index=True)
    registration_observation_id: str = Field(
        foreign_key="registrationobservation.id", index=True
    )
    # D1/C1: the only legal persisted registration-attribution results.
    level: RegistrationAttributionLevel
    supersedes_decision_id: str | None = Field(default=None)
    decided_at: datetime = Field(default_factory=now_utc)
    decided_by: str
    reason: str | None = Field(default=None)

    __table_args__ = (
        # Enables the composite FK from the head below: a decision is uniquely
        # identified by (id, registration_observation_id), so the head can only
        # ever cite a decision belonging to its own registration.
        UniqueConstraint(
            "id", "registration_observation_id", name="uq_fdec_id_reg"
        ),
        # D2/C2: a decision must belong to the same registration its proposal was
        # computed for.
        ForeignKeyConstraint(
            ["proposal_id", "registration_observation_id"],
            ["attributionproposal.id", "attributionproposal.registration_observation_id"],
            name="fk_fdec_proposal_reg",
        ),
        # D2: supersession may not cross registrations.
        ForeignKeyConstraint(
            ["supersedes_decision_id", "registration_observation_id"],
            [
                "finalattributiondecision.id",
                "finalattributiondecision.registration_observation_id",
            ],
            name="fk_fdec_supersedes_same_reg",
        ),
        # D1/C1: only the three legal registration-attribution results may be
        # persisted; CLICK_ASSOCIATED / VERIFIED_DIRECT are impossible here.
        CheckConstraint(
            f"level IN ({_allowed_names_sql(RegistrationAttributionLevel)})",
            name="ck_fdec_level_gate",
        ),
        CheckConstraint(
            f"level NOT IN ({_forbidden_sql(FORBIDDEN_FINAL_ATTRIBUTION_TOKENS)})",
            name="ck_fdec_no_direct_attribution",
        ),
        # D2: a decision cannot supersede itself (the self-FK alone would accept
        # it, because SQLite validates FKs after the row exists).
        CheckConstraint(
            "supersedes_decision_id IS NULL OR supersedes_decision_id <> id",
            name="ck_fdec_no_self_supersession",
        ),
        # D2: a PLAIN lookup index, deliberately NOT unique -- see guarantee 4.
        # Making it unique let any writer permanently reserve the current head's
        # only successor slot and strand the authorised replacement path. The
        # index exists so the backward walk from the head (and the unattached
        # -decision audit query) stay cheap, not to arbitrate currency.
        Index("ix_fdec_supersedes", "supersedes_decision_id"),
    )


class FinalAttributionHead(Pilot2Base, table=True):
    """The single CURRENT decision of a registration (the CURRENCY layer).

    Cardinality is proved by identity: ``registration_observation_id`` is the
    PRIMARY KEY, so "two current heads for one registration" is a primary-key
    violation in any transaction, committed or not -- there is no partial index
    to satisfy, no status value to choose, and no ordering of statements that
    produces a legal double head.

    The absence of a row is a legal, meaningful state ("not finalized yet"), but
    a registration that HAS been finalized can never lose its head:

    * ``decision_id`` is NOT NULL, so the pointer cannot be blanked.
    * ``trg_fhead_no_delete`` aborts every delete, so the head cannot be removed
      to manufacture an un-finalized registration.
    * ``trg_fhead_forward_only`` allows the pointer to move only to a decision
      whose ``supersedes_decision_id`` is exactly the decision it is replacing,
      and freezes the registration identity of the row.

    Together with the immutable decision table this removes every verb the
    split-transaction attack needed: appending a successor without moving the
    pointer changes nothing, and there is no separate demote step that could be
    committed on its own to leave zero heads behind.

    This row is also what keeps the EFFECTIVE history linear, which is why the
    decision table no longer needs (and must not have) a unique reservation on
    ``supersedes_decision_id``. Currency moves through exactly one row, one
    UPDATE at a time, and ``trg_fhead_forward_only`` accepts an incoming
    decision only when it supersedes the outgoing one. So however many rival
    decisions claim the same predecessor, at most one of them can ever be
    pointed at, and once the pointer has advanced past a decision no sibling of
    that decision can ever be reached again. The chain reconstructed by walking
    BACKWARDS from this pointer is therefore unique and total-ordered, with no
    help from the decision table at all.
    """

    registration_observation_id: str = Field(
        foreign_key="registrationobservation.id", primary_key=True
    )
    decision_id: str
    updated_at: datetime = Field(default_factory=now_utc)

    __table_args__ = (
        # D2: composite identity -- the head can only cite a decision that was
        # recorded for THIS registration.
        ForeignKeyConstraint(
            ["decision_id", "registration_observation_id"],
            [
                "finalattributiondecision.id",
                "finalattributiondecision.registration_observation_id",
            ],
            name="fk_fhead_decision_reg",
        ),
        # A decision may be the current head of at most one registration.
        UniqueConstraint("decision_id", name="uq_fhead_decision"),
    )


# --- D2: append-only + immutable history, forward-only head -----------------
# Attached to ``after_create`` of each table so every database built from
# ``pilot2_metadata`` (staging or hermetic test DB) gets them.
_TRG_DECISION_IMMUTABLE = DDL(
    """
CREATE TRIGGER IF NOT EXISTS trg_fdec_immutable
BEFORE UPDATE ON finalattributiondecision
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT,
        'finalattributiondecision is immutable: append a successor decision');
END;
"""
)

_TRG_DECISION_NO_DELETE = DDL(
    """
CREATE TRIGGER IF NOT EXISTS trg_fdec_no_delete
BEFORE DELETE ON finalattributiondecision
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT,
        'finalattributiondecision is append-only: history may never be deleted');
END;
"""
)

_TRG_HEAD_NO_DELETE = DDL(
    """
CREATE TRIGGER IF NOT EXISTS trg_fhead_no_delete
BEFORE DELETE ON finalattributionhead
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT,
        'finalattributionhead: a finalized registration may never lose its head');
END;
"""
)

# The head may only ADVANCE: the incoming decision must explicitly supersede the
# outgoing one. This blocks a sideways jump to an unrelated decision, a jump
# backwards to an ancestor, and (together with the composite FK) any pointer at
# another registration's decision. ``IS NOT`` is SQLite's null-safe comparison,
# so a decision with no predecessor can never take over an occupied head.
#
# This trigger -- not any index on the decision table -- is what makes the
# EFFECTIVE history linear. Rival decisions may claim the same predecessor, but
# currency passes through this single row one UPDATE at a time and only ever
# forwards, so at most one rival is ever reachable and none can be revisited
# after the pointer has moved on.
#
# PORTABILITY NOTE: the second argument of ``RAISE`` must be a STRING LITERAL --
# older SQLite builds (including the one shipped with the CI interpreter) reject
# a concatenated expression there with `near "||": syntax error`. Every message
# below is therefore a single literal and must stay that way.
_TRG_HEAD_FORWARD_ONLY = DDL(
    """
CREATE TRIGGER IF NOT EXISTS trg_fhead_forward_only
BEFORE UPDATE ON finalattributionhead
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.registration_observation_id <> OLD.registration_observation_id
        THEN RAISE(ABORT,
            'finalattributionhead: registration identity is immutable')
    END;
    SELECT CASE
        WHEN (SELECT supersedes_decision_id FROM finalattributiondecision
              WHERE id = NEW.decision_id) IS NOT OLD.decision_id
        THEN RAISE(ABORT,
            'finalattributionhead: the head may only advance to a superseding decision')
    END;
END;
"""
)

# The D2 triggers attached below, by name. A pilot2 database whose tables exist
# but whose triggers do not is NOT a valid replacement schema -- it is a writable
# attribution surface with every guard removed. The isolated staging rebuild
# checks this list before it is allowed to retire anything; see
# ``aios.pilot2.migrations_create_all.verify_pilot2_schema``.
D2_TRIGGER_NAMES: tuple[str, ...] = (
    "trg_fdec_immutable",
    "trg_fdec_no_delete",
    "trg_fhead_no_delete",
    "trg_fhead_forward_only",
)

for _table, _ddl in (
    (FinalAttributionDecision, _TRG_DECISION_IMMUTABLE),
    (FinalAttributionDecision, _TRG_DECISION_NO_DELETE),
    (FinalAttributionHead, _TRG_HEAD_NO_DELETE),
    (FinalAttributionHead, _TRG_HEAD_FORWARD_ONLY),
):
    event.listen(
        _table.__table__,
        "after_create",
        _ddl.execute_if(dialect="sqlite"),
    )


# ---------------------------------------------------------------------------
# Content taxonomy (independent controlled vocabulary, persisted reference)
# ---------------------------------------------------------------------------
class ContentTaxonomyTerm(Pilot2Base, table=True):
    id: str = Field(default_factory=lambda: new_id("ctt"), primary_key=True)
    dimension: str = Field(index=True)
    value: str = Field(index=True)
    version: int = Field(default=CONTENT_TAXONOMY_VERSION)


# ---------------------------------------------------------------------------
# Experiment registry (pure internal, zero external dependency)
# ---------------------------------------------------------------------------
class ExperimentRegistry(Pilot2Base, table=True):
    id: str = Field(default_factory=lambda: new_id("exp"), primary_key=True)
    name: str = Field(default="")
    track: str = Field(default="leadgen")
    hypothesis: str = Field(default="")
    primary_var: str | None = Field(default=None)
    locked_dims: dict = Field(default_factory=dict, sa_type=JSON)
    control_ref: str | None = Field(default=None)
    channel: Channel = Field(default=Channel.XHS)
    exposure_window_h: int = Field(default=48)
    decision_threshold: str = Field(default="")
    status: ExperimentStatus = Field(default=ExperimentStatus.DRAFT)
    created_at: datetime = Field(default_factory=now_utc)
    concluded_at: datetime | None = Field(default=None)
    conclusion: str | None = Field(default=None)
