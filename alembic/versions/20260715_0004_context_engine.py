"""Add deterministic TaskContext persistence and reviewed sources."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260715_0004"
down_revision: str | None = "20260715_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("project") as batch:
        batch.add_column(sa.Column("description", sa.String(), server_default="", nullable=False))
    with op.batch_alter_table("agent") as batch:
        batch.add_column(sa.Column("limitations", sa.JSON(), server_default="[]", nullable=False))
    with op.batch_alter_table("artifact") as batch:
        batch.add_column(
            sa.Column(
                "review_status",
                sa.String(),
                server_default="UNVERIFIED",
                nullable=False,
            )
        )
        batch.create_index("ix_artifact_review_status", ["review_status"])

    op.create_table(
        "reviewed_fact",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("artifact_id", sa.String(), nullable=False),
        sa.Column("statement", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reviewer", sa.String(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifact.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reviewed_fact_artifact_id", "reviewed_fact", ["artifact_id"])
    op.create_index("ix_reviewed_fact_status", "reviewed_fact", ["status"])

    op.create_table(
        "decision",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("series_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_decision_version"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series_id", "version", name="uq_decision_series_version"),
    )
    op.create_index("ix_decision_series_id", "decision", ["series_id"])
    op.create_index("ix_decision_project_id", "decision", ["project_id"])
    op.create_index("ix_decision_status", "decision", ["status"])

    op.create_table(
        "policy",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("series_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_policy_version"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series_id", "version", name="uq_policy_series_version"),
    )
    op.create_index("ix_policy_series_id", "policy", ["series_id"])
    op.create_index("ix_policy_project_id", "policy", ["project_id"])

    op.create_table(
        "task_context",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("assigned_agent_id", sa.String(), nullable=True),
        sa.Column("objective", sa.String(), nullable=False),
        sa.Column("instructions", sa.String(), nullable=False),
        sa.Column("acceptance_criteria", sa.JSON(), nullable=False),
        sa.Column("project_context", sa.JSON(), nullable=False),
        sa.Column("dependency_outputs", sa.JSON(), nullable=False),
        sa.Column("approved_facts", sa.JSON(), nullable=False),
        sa.Column("relevant_decisions", sa.JSON(), nullable=False),
        sa.Column("applicable_policies", sa.JSON(), nullable=False),
        sa.Column("agent_profile", sa.JSON(), nullable=False),
        sa.Column("source_references", sa.JSON(), nullable=False),
        sa.Column("context_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["assigned_agent_id"], ["agent.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "context_hash", name="uq_task_context_task_hash"),
    )
    op.create_index("ix_task_context_task_id", "task_context", ["task_id"])
    op.create_index("ix_task_context_project_id", "task_context", ["project_id"])
    op.create_index("ix_task_context_assigned_agent_id", "task_context", ["assigned_agent_id"])
    op.create_index("ix_task_context_context_hash", "task_context", ["context_hash"])
    op.execute(
        """
        CREATE TRIGGER task_context_reject_update
        BEFORE UPDATE ON task_context
        BEGIN
            SELECT RAISE(ABORT, 'task_context is append-only');
        END
        """
    )


def downgrade() -> None:
    op.drop_table("task_context")
    op.drop_table("policy")
    op.drop_table("decision")
    op.drop_table("reviewed_fact")
    with op.batch_alter_table("artifact") as batch:
        batch.drop_index("ix_artifact_review_status")
        batch.drop_column("review_status")
    with op.batch_alter_table("agent") as batch:
        batch.drop_column("limitations")
    with op.batch_alter_table("project") as batch:
        batch.drop_column("description")
