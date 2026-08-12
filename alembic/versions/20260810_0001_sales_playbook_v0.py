"""SalesPlaybook V0: read-only official sales-script evidence source.

Adds five brand-new tables:

``sales_script_source`` / ``sales_script_entry`` / ``sales_script_segment`` /
``sales_script_fact_binding`` / ``cs_suggestion_sales_evidence``.

No existing table is touched. In particular ``cs_suggestion`` keeps its exact
schema (design D5: evidence lives in an association table, never in a new JSON
column on the suggestion) and the ``artifact`` table -- referenced by the
``knowledge_candidate_validate_insert`` trigger -- is left completely alone, so
the batch-recreate caveat from earlier migrations does not apply here.

DDL strategy (cleanup D): every CHECK / FOREIGN KEY / UNIQUE is declared inside
the ``CREATE TABLE`` statement. SQLite cannot add a CHECK constraint with a
plain ``ALTER TABLE``, and emulating one via batch-recreate would rewrite the
table; declaring the constraints at creation time avoids both.

Downgrade is UNCONDITIONALLY fail-closed (P1-3): ``downgrade()`` raises before
any DDL is emitted, whatever the tables currently hold. Emptiness is not a
safe-to-drop signal -- it is unobservable at the moment the decision is made
(another writer may be mid-import), it is racy against the destructive DDL that
would follow, and a partially-created schema would make the row probe itself
raise an operational error instead of the intended refusal. Rolling this
revision back therefore requires a deliberate, human, out-of-band schema
operation; the migration itself never destroys imported official sales content.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260810_0001"
down_revision: str | None = "20260731_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Vocabulary literals are spelled out here rather than imported from
# ``aios.models``. A migration must describe the schema as it was at THIS
# revision: if a future edit adds an enum member, this historical migration must
# keep producing the historical CHECK, and the new member must arrive through a
# new migration. Importing the live enum would let application code silently
# rewrite history.
_SCOPE_VALUES = "'mihe_1_0', 'mihe_2_0', 'common'"
_SOURCE_STATUS_VALUES = "'active', 'superseded', 'inactive'"
_SEGMENT_TYPE_VALUES = "'text', 'image'"
_FACT_CLASS_VALUES = (
    "'price', 'commission', 'membership', 'capability', 'url', 'promo', 'other'"
)
_FACT_STATUS_VALUES = "'verified_current', 'needs_review', 'stale', 'version_1_only'"
_SOURCE_TYPE_VALUES = "'mihe_ebf'"

_TABLES = (
    "cs_suggestion_sales_evidence",
    "sales_script_fact_binding",
    "sales_script_segment",
    "sales_script_entry",
    "sales_script_source",
)


def upgrade() -> None:
    # --- 1. immutable source generation (D1) --------------------------------
    op.create_table(
        "sales_script_source",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("original_ebf_filename", sa.String(), nullable=False),
        sa.Column("extracted_manifest_filename", sa.String(), nullable=False),
        sa.Column("source_file_hash", sa.String(), nullable=False),
        sa.Column("extraction_manifest_hash", sa.String(), nullable=False),
        sa.Column("source_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("imported_at", sa.DateTime(), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            f"status IN ({_SOURCE_STATUS_VALUES})", name="ck_ssrc_status_gate"
        ),
        sa.CheckConstraint(
            f"source_type IN ({_SOURCE_TYPE_VALUES})", name="ck_ssrc_source_type_gate"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sales_script_source_source_type", "sales_script_source", ["source_type"]
    )
    op.create_index(
        "ix_sales_script_source_source_file_hash",
        "sales_script_source",
        ["source_file_hash"],
    )
    op.create_index(
        "ix_sales_script_source_extraction_manifest_hash",
        "sales_script_source",
        ["extraction_manifest_hash"],
        unique=True,
    )
    # D1: PARTIAL unique index -- at most one ACTIVE generation per source type,
    # enforced by the database rather than by importer discipline.
    op.create_index(
        "uq_ssrc_single_active",
        "sales_script_source",
        ["source_type", "status"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )

    # --- 2. immutable official entry (D2) -----------------------------------
    op.create_table(
        "sales_script_entry",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("source_entry_id", sa.String(), nullable=False),
        sa.Column("product_scope", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("source_hash", sa.String(), nullable=False),
        sa.Column("imported_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sales_script_source.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id", "source_entry_id", name="uq_ss_entry_source_entry"
        ),
        # Target of the composite FK from sales_script_fact_binding (D3).
        sa.UniqueConstraint("id", "product_scope", name="uq_ss_entry_id_scope"),
        sa.CheckConstraint(
            f"product_scope IN ({_SCOPE_VALUES})", name="ck_ss_entry_scope_member"
        ),
    )
    op.create_index(
        "ix_sales_script_entry_source_id", "sales_script_entry", ["source_id"]
    )
    op.create_index(
        "ix_sales_script_entry_source_entry_id",
        "sales_script_entry",
        ["source_entry_id"],
    )
    op.create_index(
        "ix_sales_script_entry_category", "sales_script_entry", ["category"]
    )
    op.create_index(
        "ix_sales_script_entry_source_hash", "sales_script_entry", ["source_hash"]
    )

    # --- 3. sole authority for ordered text/image structure (D2) ------------
    op.create_table(
        "sales_script_segment",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("entry_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("segment_type", sa.String(), nullable=False),
        sa.Column("text_content", sa.String(), nullable=True),
        sa.Column("artifact_id", sa.String(), nullable=True),
        sa.Column("caption", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["entry_id"], ["sales_script_entry.id"], ondelete="CASCADE"
        ),
        # Images reuse the existing artifact table (cleanup C); RESTRICT keeps a
        # referenced image from disappearing under a frozen script entry.
        sa.ForeignKeyConstraint(["artifact_id"], ["artifact.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entry_id", "sequence", name="uq_ssseg_entry_sequence"),
        sa.CheckConstraint(
            f"segment_type IN ({_SEGMENT_TYPE_VALUES})", name="ck_ssseg_type_member"
        ),
        sa.CheckConstraint(
            "(segment_type = 'text'"
            " AND text_content IS NOT NULL AND artifact_id IS NULL)"
            " OR (segment_type = 'image'"
            " AND artifact_id IS NOT NULL AND text_content IS NULL)",
            name="ck_ssseg_type_nullability",
        ),
        sa.CheckConstraint("sequence >= 0", name="ck_ssseg_sequence_non_negative"),
    )
    op.create_index(
        "ix_sales_script_segment_entry_id", "sales_script_segment", ["entry_id"]
    )
    op.create_index(
        "ix_sales_script_segment_artifact_id", "sales_script_segment", ["artifact_id"]
    )

    # --- 4. normalised dynamic-fact binding (D3) ----------------------------
    op.create_table(
        "sales_script_fact_binding",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("entry_id", sa.String(), nullable=False),
        sa.Column("entry_scope", sa.String(), nullable=False),
        sa.Column("fact_key", sa.String(), nullable=False),
        sa.Column("fact_class", sa.String(), nullable=False),
        sa.Column("raw_span", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("reviewed_by", sa.String(), nullable=True),
        sa.Column("binding_hash", sa.String(), nullable=False),
        # Composite FK: the denormalised entry_scope is guaranteed to be the
        # entry's REAL scope, which is what makes ck_ssfb_scope_compat below a
        # genuine cross-version gate instead of a self-reported claim.
        sa.ForeignKeyConstraint(
            ["entry_id", "entry_scope"],
            ["sales_script_entry.id", "sales_script_entry.product_scope"],
            name="fk_ssfb_entry_scope",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "entry_id", "fact_key", "raw_span", name="uq_ssfb_entry_fact_span"
        ),
        sa.CheckConstraint(
            f"fact_class IN ({_FACT_CLASS_VALUES})", name="ck_ssfb_class_gate"
        ),
        sa.CheckConstraint(
            f"status IN ({_FACT_STATUS_VALUES})", name="ck_ssfb_status_gate"
        ),
        sa.CheckConstraint(
            f"scope IN ({_SCOPE_VALUES})", name="ck_ssfb_scope_member"
        ),
        sa.CheckConstraint(
            f"entry_scope IN ({_SCOPE_VALUES})", name="ck_ssfb_entry_scope_member"
        ),
        sa.CheckConstraint(
            "scope = 'common' OR scope = entry_scope", name="ck_ssfb_scope_compat"
        ),
    )
    op.create_index(
        "ix_sales_script_fact_binding_entry_id",
        "sales_script_fact_binding",
        ["entry_id"],
    )
    op.create_index(
        "ix_sales_script_fact_binding_fact_key",
        "sales_script_fact_binding",
        ["fact_key"],
    )
    op.create_index(
        "ix_sales_script_fact_binding_binding_hash",
        "sales_script_fact_binding",
        ["binding_hash"],
        unique=True,
    )

    # --- 5. suggestion <-> evidence association (D5) ------------------------
    op.create_table(
        "cs_suggestion_sales_evidence",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("suggestion_id", sa.String(), nullable=False),
        sa.Column("entry_id", sa.String(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("match_reason", sa.String(), nullable=False),
        sa.Column("fact_safety", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["suggestion_id"], ["cs_suggestion.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["entry_id"], ["sales_script_entry.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "suggestion_id", "entry_id", name="uq_ssev_suggestion_entry"
        ),
        sa.CheckConstraint(
            f"fact_safety IN ({_FACT_STATUS_VALUES})", name="ck_ssev_fact_safety_gate"
        ),
        sa.CheckConstraint("rank >= 0", name="ck_ssev_rank_non_negative"),
    )
    op.create_index(
        "ix_cs_suggestion_sales_evidence_suggestion_id",
        "cs_suggestion_sales_evidence",
        ["suggestion_id"],
    )
    op.create_index(
        "ix_cs_suggestion_sales_evidence_entry_id",
        "cs_suggestion_sales_evidence",
        ["entry_id"],
    )


def downgrade() -> None:
    """Refuse unconditionally, before any DDL, in every state (P1-3).

    There is deliberately no "safe" branch. Dropping these tables destroys
    imported official sales content and the audit links that prove which
    outbound message cited which official entry -- neither is representable in
    the previous schema, so the loss is silent and unrecoverable.

    Emptiness is explicitly NOT accepted as a licence to drop:

    * it is a race -- an importer may commit between the probe and the DDL;
    * it is unobservable in a partially-applied schema, where probing a missing
      table raises an operational error instead of the intended refusal;
    * a genuinely empty schema costs nothing to leave in place.

    Rolling this revision back is therefore a deliberate human out-of-band
    operation, never something a migration runner can do by accident. Schema,
    rows, indexes and the alembic revision stay exactly as they are.
    """
    raise RuntimeError(
        "cannot downgrade migration 20260810_0001: this downgrade is disabled "
        "unconditionally. The tables "
        + ", ".join(_TABLES)
        + " hold imported official sales content and the evidence links that "
        "audit outbound messages; the previous schema cannot represent either, "
        "so dropping them would silently and irrecoverably destroy them. "
        "Emptiness is not accepted as a safe state (it is racy and "
        "unobservable in a partially-applied schema). Roll back deliberately "
        "and out-of-band instead."
    )
