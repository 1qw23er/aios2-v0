"""Add deterministic human-reviewed knowledge ingestion."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260716_0005"
down_revision: str | None = "20260715_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("artifact") as batch:
        batch.alter_column("project_id", existing_type=sa.String(), nullable=True)

    op.create_table(
        "knowledge_candidate",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("artifact_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("statement", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("submitted_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifact.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_candidate_artifact_id", "knowledge_candidate", ["artifact_id"])
    op.create_index("ix_knowledge_candidate_project_id", "knowledge_candidate", ["project_id"])
    op.create_index("ix_knowledge_candidate_status", "knowledge_candidate", ["status"])

    op.create_table(
        "knowledge_review_decision",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("candidate_id", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("reviewer", sa.String(), nullable=False),
        sa.Column("rationale", sa.String(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_id"], ["knowledge_candidate.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_id", name="uq_knowledge_review_candidate"),
    )
    op.create_index(
        "ix_knowledge_review_decision_candidate_id",
        "knowledge_review_decision",
        ["candidate_id"],
    )

    op.create_table(
        "knowledge_fact",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("series_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("statement", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("source_candidate_id", sa.String(), nullable=False),
        sa.Column("source_artifact_id", sa.String(), nullable=False),
        sa.Column("review_decision_id", sa.String(), nullable=False),
        sa.Column("supersedes_fact_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_knowledge_fact_version"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(["review_decision_id"], ["knowledge_review_decision.id"]),
        sa.ForeignKeyConstraint(["source_artifact_id"], ["artifact.id"]),
        sa.ForeignKeyConstraint(["source_candidate_id"], ["knowledge_candidate.id"]),
        sa.ForeignKeyConstraint(["supersedes_fact_id"], ["knowledge_fact.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series_id", "version", name="uq_knowledge_fact_series_version"),
        sa.UniqueConstraint("source_candidate_id", name="uq_knowledge_fact_source_candidate"),
        sa.UniqueConstraint("review_decision_id", name="uq_knowledge_fact_review_decision"),
        sa.UniqueConstraint("supersedes_fact_id", name="uq_knowledge_fact_supersedes"),
    )
    for column in (
        "series_id",
        "project_id",
        "status",
        "source_candidate_id",
        "source_artifact_id",
        "review_decision_id",
        "supersedes_fact_id",
    ):
        op.create_index(f"ix_knowledge_fact_{column}", "knowledge_fact", [column])
    op.execute(
        """
        CREATE UNIQUE INDEX uq_knowledge_fact_approved_head
        ON knowledge_fact(series_id, COALESCE(project_id, ''))
        WHERE status = 'APPROVED'
        """
    )

    op.execute(
        """
        CREATE TRIGGER knowledge_candidate_validate_insert
        BEFORE INSERT ON knowledge_candidate
        BEGIN
            SELECT CASE WHEN TRIM(NEW.statement) = '' OR TRIM(NEW.submitted_by) = ''
                THEN RAISE(ABORT, 'knowledge candidate fields must be non-empty') END;
            SELECT CASE WHEN NEW.status <> 'DRAFT'
                THEN RAISE(ABORT, 'knowledge candidate must start draft') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM artifact a
                WHERE a.id = NEW.artifact_id AND a.review_status = 'APPROVED'
                  AND a.project_id IS NEW.project_id
            ) THEN RAISE(ABORT, 'knowledge candidate requires approved exact-scope artifact') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_candidate_validate_update
        BEFORE UPDATE ON knowledge_candidate
        BEGIN
            SELECT CASE WHEN OLD.artifact_id <> NEW.artifact_id
                OR OLD.project_id IS NOT NEW.project_id OR OLD.statement <> NEW.statement
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
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_candidate c
                JOIN artifact a ON a.id = c.artifact_id
                JOIN knowledge_review_decision r ON r.candidate_id = c.id
                WHERE c.id = NEW.source_candidate_id
                  AND c.status = 'APPROVED'
                  AND c.artifact_id = NEW.source_artifact_id
                  AND c.project_id IS NEW.project_id
                  AND c.statement = NEW.statement
                  AND a.review_status = 'APPROVED'
                  AND a.project_id IS NEW.project_id
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
                THEN RAISE(ABORT, 'existing knowledge series requires predecessor') END;
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
            ) THEN RAISE(ABORT, 'knowledge predecessor was not current head') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_fact_validate_update
        BEFORE UPDATE ON knowledge_fact
        BEGIN
            SELECT CASE WHEN OLD.series_id <> NEW.series_id OR OLD.version <> NEW.version
                OR OLD.project_id IS NOT NEW.project_id OR OLD.statement <> NEW.statement
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
    op.drop_table("knowledge_fact")
    op.drop_table("knowledge_review_decision")
    op.drop_table("knowledge_candidate")
    with op.batch_alter_table("artifact") as batch:
        batch.alter_column("project_id", existing_type=sa.String(), nullable=False)
