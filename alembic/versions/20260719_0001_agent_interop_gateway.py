"""Agent Interoperability Gateway (#57): extend Agent/Artifact, add DelegatedRun.

Lets AIOS delegate work to external closed-source agents (remote_api / a2a /
mcp / workstation) without learning their internals. Reuses the existing
``Agent`` table (adds delegation_mode / secret_ref / callback_url) rather than
creating a parallel adapter table, and extends ``Artifact`` with provenance so a
delegated result enters as an unverified Artifact and passes schema validation
before task completion. ``DelegatedRun`` is the new table recording one remote
execution lifecycle.

Security note: ``secret_ref`` columns store ONLY an opaque handle to an external
secret store; the secret itself is never written to the DB, TaskContext,
Artifact, or AuditLog payloads.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260719_0001"
down_revision: str | None = "20260718_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # StrEnum fields map to VARCHAR/String in SQLite (no native ENUM support).
    # 1. Extend agent: how it is reached + secret handle (opaque, never the secret).
    with op.batch_alter_table("agent") as batch:
        batch.add_column(sa.Column("delegation_mode", sa.String(), nullable=True))
        batch.add_column(sa.Column("secret_ref", sa.String(), nullable=True))
        batch.add_column(sa.Column("callback_url", sa.String(), nullable=True))
        # Existing config_ref is reused as an opaque config/secret reference handle.

    # 2. Extend artifact: which adapter produced it + immutable provenance bundle.
    with op.batch_alter_table("artifact") as batch:
        batch.add_column(sa.Column("adapter_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("source", sa.String(), nullable=True))
        batch.add_column(sa.Column("provenance", sa.JSON(), nullable=True))  # field provenance_json

    # 3. New table: one delegated remote execution lifecycle.
    op.create_table(
        "delegated_run",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("delegation_mode", sa.String(), nullable=False),
        sa.Column("secret_ref", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("remote_run_id", sa.String(), nullable=True),
        sa.Column("remote_status", sa.String(), nullable=True),
        sa.Column("context_ref", sa.String(), nullable=True),
        sa.Column("callback_url", sa.String(), nullable=True),
        sa.Column("cost", sa.Float(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"]),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_delegated_run_project_id", "delegated_run", ["project_id"])
    op.create_index("ix_delegated_run_task_id", "delegated_run", ["task_id"])
    op.create_index("ix_delegated_run_agent_id", "delegated_run", ["agent_id"])


def downgrade() -> None:
    op.drop_table("delegated_run")
    # SQLite re-validates these knowledge triggers during the artifact batch
    # rename (they reference artifact), so drop them first and recreate after.
    op.execute("DROP TRIGGER IF EXISTS knowledge_candidate_validate_insert")
    op.execute("DROP TRIGGER IF EXISTS knowledge_candidate_validate_update")
    op.execute("DROP TRIGGER IF EXISTS knowledge_review_reject_update")
    op.execute("DROP TRIGGER IF EXISTS knowledge_fact_validate_insert")
    op.execute("DROP TRIGGER IF EXISTS knowledge_fact_validate_update")
    with op.batch_alter_table("artifact") as batch:
        batch.drop_column("provenance")
        batch.drop_column("source")
        batch.drop_column("adapter_id")
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
    with op.batch_alter_table("agent") as batch:
        batch.drop_column("callback_url")
        batch.drop_column("secret_ref")
        batch.drop_column("delegation_mode")
