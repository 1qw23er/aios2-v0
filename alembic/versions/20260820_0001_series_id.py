"""Persist owner-inbox cross-thread series_id (PR #124).

Adds a nullable, indexed ``series_id`` column to the three owner-inbox item
tables that currently lack a *queryable* series key:

* ``artifact``            -- content + feedback items. The series id lived only
  in ``metadata_json`` (unindexed, and unstable to group on after edits/reruns).
* ``cs_suggestion``       -- customer-service items (no series key at all).
* ``knowledge_candidate`` -- pending knowledge items (no series key at all).

``knowledge_fact`` already has a persisted ``series_id`` column, so it is
untouched (reuse boundary -- no existing table/column is altered beyond the
three ADD COLUMNs below).

Backfill (deterministic, restartable -- every statement is guarded by
``WHERE series_id IS NULL``):
* artifact.series_id          <- json_extract(metadata, '$.series_id') where present.
* knowledge_candidate.series_id <- source artifact.series_id (inherited).
* cs_suggestion.series_id     <- 'series:' || conversation_id (each conversation
  is exactly one series, deterministic).

Fail-closed semantics: any row whose series cannot be derived keeps
``series_id = NULL`` and is treated as ungrouped by the owner surface; NULL is
NEVER guessed into a group.

Reversible: downgrade drops the three indexes + columns (single head kept).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260820_0001_series_id"
down_revision: str | None = "20260812_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add the column to all three tables (raw ALTER). artifact's external
    #    trigger ``knowledge_candidate_validate_insert`` references the table, not
    #    specific columns, so adding a column is safe -- no batch recreate.
    op.add_column("artifact", sa.Column("series_id", sa.String(), nullable=True))
    op.add_column("cs_suggestion", sa.Column("series_id", sa.String(), nullable=True))
    op.add_column(
        "knowledge_candidate", sa.Column("series_id", sa.String(), nullable=True)
    )

    # 2. Indexes (explicit; model index=True only affects fresh-table create,
    #    not ALTER of an existing table).
    op.create_index("ix_artifact_series_id", "artifact", ["series_id"])
    op.create_index("ix_cs_suggestion_series_id", "cs_suggestion", ["series_id"])
    op.create_index(
        "ix_knowledge_candidate_series_id", "knowledge_candidate", ["series_id"]
    )

    # 3. Deterministic, restartable backfill (NULL-guarded everywhere so the
    #    migration can be re-run safely).
    #    artifact: pull series_id out of metadata_json where it exists.
    op.execute(
        "UPDATE artifact "
        "SET series_id = json_extract(metadata, '$.series_id') "
        "WHERE series_id IS NULL "
        "AND json_extract(metadata, '$.series_id') IS NOT NULL"
    )
    #    knowledge_candidate: inherit from the source artifact's series_id.
    #    Rows whose source artifact has no series_id keep NULL (fail-closed).
    #    The BEFORE UPDATE trigger ``knowledge_candidate_validate_update`` only
    #    permits status/tag lifecycle transitions, so a benign series_id backfill
    #    on existing DRAFT candidates would be rejected. Drop it for the duration
    #    of this one UPDATE, then recreate it verbatim from sqlite_master (so the
    #    fix stays robust to the trigger's exact definition).
    bind = op.get_bind()
    trig = bind.execute(
        sa.text(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='knowledge_candidate_validate_update'"
        )
    ).fetchone()
    if trig and trig[0]:
        op.execute("DROP TRIGGER knowledge_candidate_validate_update")
        op.execute(
            "UPDATE knowledge_candidate "
            "SET series_id = (SELECT a.series_id FROM artifact a "
            "WHERE a.id = knowledge_candidate.artifact_id) "
            "WHERE series_id IS NULL"
        )
        op.execute(sa.text(trig[0]))
    else:
        op.execute(
            "UPDATE knowledge_candidate "
            "SET series_id = (SELECT a.series_id FROM artifact a "
            "WHERE a.id = knowledge_candidate.artifact_id) "
            "WHERE series_id IS NULL"
        )
    #    cs_suggestion: each conversation is exactly one series.
    op.execute(
        "UPDATE cs_suggestion "
        "SET series_id = 'series:' || conversation_id "
        "WHERE series_id IS NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_candidate_series_id", table_name="knowledge_candidate")
    op.drop_index("ix_cs_suggestion_series_id", table_name="cs_suggestion")
    op.drop_index("ix_artifact_series_id", table_name="artifact")
    op.drop_column("knowledge_candidate", "series_id")
    op.drop_column("cs_suggestion", "series_id")
    op.drop_column("artifact", "series_id")
