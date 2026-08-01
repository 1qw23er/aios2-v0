"""Customer-service / sales-conversion workflow tables for #109 (V1.2-B).

Adds three brand-new tables used by the customer-service pipeline
(``conversation`` / ``message`` / ``cs_suggestion``). No existing table is
touched -- in particular the ``artifact`` table (referenced by the
``knowledge_candidate_validate_insert`` trigger) is left entirely alone, so the
batch-recreate caveat from earlier migrations does not apply here.

DDL strategy: three ``CREATE TABLE`` + the supporting indexes. All three tables
are new and untriggered.

Downgrade is fail-closed: any row in ``conversation`` / ``message`` /
``cs_suggestion`` is real customer / sales data the previous schema cannot
represent, so dropping the tables would silently destroy it. We abort BEFORE
any DDL if any of the three tables holds a row. A lossless downgrade is only
promised for completely empty tables (matching the empty-data reversible
contract of the other controlled migrations).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "20260731_0001"
down_revision: str | None = "20260730_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("external_conversation_ref", sa.String(), nullable=True),
        sa.Column("customer_ref", sa.String(), nullable=True),
        sa.Column("lead_stage", sa.String(), nullable=False),
        sa.Column("assigned_human", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversation_project_id", "conversation", ["project_id"])

    op.create_table(
        "message",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("sender_type", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("is_auto_sent", sa.Boolean(), nullable=False),
        sa.Column("escalation_flag", sa.Boolean(), nullable=False),
        sa.Column("escalation_categories", sa.JSON(), nullable=True),
        sa.Column("knowledge_fact_refs", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversation.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_message_conversation_id", "message", ["conversation_id"])
    op.create_index("ix_message_project_id", "message", ["project_id"])

    op.create_table(
        "cs_suggestion",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("escalation_categories", sa.JSON(), nullable=True),
        sa.Column("knowledge_fact_refs", sa.JSON(), nullable=True),
        sa.Column("fact_revisions", sa.JSON(), nullable=True),
        sa.Column("consumed", sa.Boolean(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversation.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cs_suggestion_conversation_id", "cs_suggestion", ["conversation_id"]
    )
    op.create_index("ix_cs_suggestion_project_id", "cs_suggestion", ["project_id"])
    op.create_index(
        "ix_cs_suggestion_idempotency_key",
        "cs_suggestion",
        ["idempotency_key"],
        unique=True,
    )


def _cs_tables_have_rows() -> bool:
    """True if any of the three CS tables holds a row.

    Any stored row is real customer / sales data; dropping the tables would
    silently destroy it, so its presence is the precise trigger for a
    fail-closed downgrade.
    """
    bind = op.get_bind()
    for table in ("conversation", "message", "cs_suggestion"):
        count = bind.execute(text(f"SELECT COUNT(*) FROM {table}")).fetchone()[0]
        if (count or 0) > 0:
            return True
    return False


def downgrade() -> None:
    # Fail-closed: any row in the three CS tables is customer / sales data the
    # previous schema cannot represent. Abort BEFORE any DDL and leave schema,
    # rows, indexes and revision on 20260731_0001.
    if _cs_tables_have_rows():
        raise RuntimeError(
            "cannot downgrade migration 20260731_0001: conversation / message / "
            "cs_suggestion hold customer or sales data that the previous schema "
            "cannot represent; downgrading would silently destroy it. Clear "
            "(back up and delete) every row from the three tables before "
            "downgrading."
        )
    op.drop_index("ix_cs_suggestion_idempotency_key", table_name="cs_suggestion")
    op.drop_index("ix_cs_suggestion_project_id", table_name="cs_suggestion")
    op.drop_index("ix_cs_suggestion_conversation_id", table_name="cs_suggestion")
    op.drop_table("cs_suggestion")

    op.drop_index("ix_message_project_id", table_name="message")
    op.drop_index("ix_message_conversation_id", table_name="message")
    op.drop_table("message")

    op.drop_index("ix_conversation_project_id", table_name="conversation")
    op.drop_table("conversation")
