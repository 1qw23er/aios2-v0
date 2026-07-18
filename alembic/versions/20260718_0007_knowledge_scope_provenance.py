"""Separate effective knowledge scope from source-campaign provenance.

Up to this revision a knowledge candidate / fact used a single ``project_id``
column for BOTH the effective reuse scope (NULL = company-wide) and the source
campaign. That forced a company-scoped fact to also have a NULL source artifact,
which destroyed source-campaign ownership and let provenance be silently lost.

This migration adds ``source_project_id`` (the campaign that produced the source
artifact; never NULL) as a distinct column, while ``project_id`` keeps meaning
the EFFECTIVE scope (NULL = company-wide). The SQLite triggers are rewritten so
provenance is validated against ``source_project_id`` and the effective scope is
only required to be NULL (company) or equal to the source campaign (project).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260718_0007"
down_revision: str | None = "20260717_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 0. Drop the old triggers FIRST. They cross-reference knowledge_candidate /
    #    knowledge_fact, and SQLite re-validates trigger bodies during the batch
    #    table-recreation (ALTER TABLE ... RENAME) below, which would otherwise
    #    fail with "no such table". Dropping them up front lets the batch alters
    #    copy rows without firing the old (now-invalid) validation.
    op.execute("DROP TRIGGER IF EXISTS knowledge_candidate_validate_insert")
    op.execute("DROP TRIGGER IF EXISTS knowledge_candidate_validate_update")
    op.execute("DROP TRIGGER IF EXISTS knowledge_review_reject_update")
    op.execute("DROP TRIGGER IF EXISTS knowledge_fact_validate_insert")
    op.execute("DROP TRIGGER IF EXISTS knowledge_fact_validate_update")

    # 1. Add provenance columns (nullable first so the backfill can run).
    with op.batch_alter_table("knowledge_candidate") as batch:
        batch.add_column(
            sa.Column("source_project_id", sa.String(), nullable=True)
        )
        batch.create_foreign_key(
            "fk_knowledge_candidate_source_project",
            "project",
            ["source_project_id"],
            ["id"],
        )
        batch.create_index(
            "ix_knowledge_candidate_source_project_id", ["source_project_id"]
        )

    with op.batch_alter_table("knowledge_fact") as batch:
        batch.add_column(
            sa.Column("source_project_id", sa.String(), nullable=True)
        )
        batch.create_foreign_key(
            "fk_knowledge_fact_source_project",
            "project",
            ["source_project_id"],
            ["id"],
        )
        batch.create_index(
            "ix_knowledge_fact_source_project_id", ["source_project_id"]
        )

    # 2. Backfill provenance from the source artifact / candidate.
    op.execute(
        """
        UPDATE knowledge_candidate
        SET source_project_id = (
            SELECT a.project_id FROM artifact a WHERE a.id = knowledge_candidate.artifact_id
        )
        WHERE source_project_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE knowledge_fact
        SET source_project_id = (
            SELECT c.source_project_id
            FROM knowledge_candidate c
            WHERE c.id = knowledge_fact.source_candidate_id
        )
        WHERE source_project_id IS NULL
        """
    )

    # 3. Recreate triggers: provenance validated via source_project_id; project_id
    #    is only the effective scope (NULL = company, else == source campaign).
    op.execute(
        """
        CREATE TRIGGER knowledge_candidate_validate_insert
        BEFORE INSERT ON knowledge_candidate
        BEGIN
            SELECT CASE WHEN TRIM(NEW.statement) = '' OR TRIM(NEW.submitted_by) = ''
                THEN RAISE(ABORT, 'knowledge candidate fields must be non-empty') END;
            SELECT CASE WHEN NEW.status <> 'DRAFT'
                THEN RAISE(ABORT, 'knowledge candidate must start draft') END;
            SELECT CASE WHEN NEW.source_project_id IS NULL
                THEN RAISE(ABORT, 'knowledge candidate requires a source campaign') END;
            SELECT CASE WHEN NEW.project_id IS NOT NULL AND NEW.project_id <> NEW.source_project_id
                THEN RAISE(ABORT, 'project-scoped candidate must match its source campaign') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM artifact a
                WHERE a.id = NEW.artifact_id AND a.review_status = 'APPROVED'
                  AND a.project_id IS NEW.source_project_id
            ) THEN RAISE(ABORT,
                'knowledge candidate requires an approved same-campaign artifact') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_candidate_validate_update
        BEFORE UPDATE ON knowledge_candidate
        BEGIN
            SELECT CASE WHEN OLD.artifact_id <> NEW.artifact_id
                OR OLD.project_id IS NOT NEW.project_id
                OR OLD.source_project_id IS NOT NEW.source_project_id
                OR OLD.statement <> NEW.statement
                OR OLD.submitted_by <> NEW.submitted_by OR OLD.created_at <> NEW.created_at
                THEN RAISE(ABORT, 'knowledge candidate identity is immutable') END;
            SELECT CASE WHEN OLD.status <> 'DRAFT'
                OR NEW.status NOT IN ('APPROVED', 'REJECTED')
                THEN RAISE(ABORT, 'invalid knowledge candidate lifecycle') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_review_reject_update
        BEFORE UPDATE ON knowledge_review_decision
        BEGIN
            SELECT RAISE(ABORT, 'knowledge review decision is immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_fact_validate_insert
        BEFORE INSERT ON knowledge_fact
        BEGIN
            SELECT CASE WHEN NEW.status <> 'APPROVED'
                THEN RAISE(ABORT, 'knowledge fact must start approved') END;
            SELECT CASE WHEN TRIM(NEW.series_id) = '' OR TRIM(NEW.statement) = ''
                THEN RAISE(ABORT, 'knowledge fact fields must be non-empty') END;
            SELECT CASE WHEN NEW.source_project_id IS NULL
                THEN RAISE(ABORT, 'knowledge fact requires a source campaign') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_candidate c
                JOIN artifact a ON a.id = c.artifact_id
                JOIN knowledge_review_decision r ON r.candidate_id = c.id
                WHERE c.id = NEW.source_candidate_id
                  AND c.status = 'APPROVED'
                  AND c.source_project_id IS NEW.source_project_id
                  AND c.artifact_id = NEW.source_artifact_id
                  AND c.project_id IS NEW.project_id
                  AND c.statement = NEW.statement
                  AND a.review_status = 'APPROVED'
                  AND a.project_id IS NEW.source_project_id
                  AND r.id = NEW.review_decision_id AND r.decision = 'APPROVE'
            ) THEN RAISE(ABORT, 'knowledge fact provenance is invalid') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_fact f
                WHERE f.series_id = NEW.series_id AND f.project_id IS NEW.project_id
            ) AND (NEW.version <> 1 OR NEW.supersedes_fact_id IS NOT NULL)
                THEN RAISE(ABORT, 'first knowledge fact must be version 1') END;
            SELECT CASE WHEN EXISTS (
                SELECT 1 FROM knowledge_fact f
                WHERE f.series_id = NEW.series_id AND f.project_id IS NEW.project_id
            ) AND NEW.supersedes_fact_id IS NULL
                THEN RAISE(ABORT, 'existing knowledge series requires a predecessor') END;
            SELECT CASE WHEN NEW.supersedes_fact_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM knowledge_fact p
                WHERE p.id = NEW.supersedes_fact_id AND p.series_id = NEW.series_id
                  AND p.project_id IS NEW.project_id AND p.status = 'SUPERSEDED'
                  AND NEW.version > p.version
            ) THEN RAISE(ABORT, 'knowledge predecessor is invalid') END;
            SELECT CASE WHEN NEW.supersedes_fact_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM knowledge_fact h
                WHERE h.series_id = NEW.series_id AND h.project_id IS NEW.project_id
                  AND h.status = 'APPROVED'
            ) THEN RAISE(ABORT, 'knowledge predecessor was not the current approved head') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_fact_validate_update
        BEFORE UPDATE ON knowledge_fact
        BEGIN
            SELECT CASE WHEN OLD.series_id <> NEW.series_id OR OLD.version <> NEW.version
                OR OLD.project_id IS NOT NEW.project_id
                OR OLD.source_project_id IS NOT NEW.source_project_id
                OR OLD.statement <> NEW.statement
                OR OLD.source_candidate_id <> NEW.source_candidate_id
                OR OLD.source_artifact_id <> NEW.source_artifact_id
                OR OLD.review_decision_id <> NEW.review_decision_id
                OR OLD.supersedes_fact_id IS NOT NEW.supersedes_fact_id
                OR OLD.created_at <> NEW.created_at
                THEN RAISE(ABORT, 'knowledge fact identity is immutable') END;
            SELECT CASE WHEN OLD.status <> 'APPROVED'
                OR NEW.status NOT IN ('SUPERSEDED', 'INACTIVE')
                THEN RAISE(ABORT, 'invalid knowledge fact lifecycle') END;
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS knowledge_fact_validate_update")
    op.execute("DROP TRIGGER IF EXISTS knowledge_fact_validate_insert")
    op.execute("DROP TRIGGER IF EXISTS knowledge_review_reject_update")
    op.execute("DROP TRIGGER IF EXISTS knowledge_candidate_validate_update")
    op.execute("DROP TRIGGER IF EXISTS knowledge_candidate_validate_insert")

    with op.batch_alter_table("knowledge_fact") as batch:
        batch.drop_index("ix_knowledge_fact_source_project_id")
        batch.drop_constraint("fk_knowledge_fact_source_project", type_="foreignkey")
        batch.drop_column("source_project_id")

    with op.batch_alter_table("knowledge_candidate") as batch:
        batch.drop_index("ix_knowledge_candidate_source_project_id")
        batch.drop_constraint("fk_knowledge_candidate_source_project", type_="foreignkey")
        batch.drop_column("source_project_id")
