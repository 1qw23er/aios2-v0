from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260715_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _common_id() -> sa.Column:
    return sa.Column("id", sa.String(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "project",
        _common_id(),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("objective", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("budget_limit", sa.Float(), nullable=False),
        sa.Column("success_metrics", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "agent",
        _common_id(),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("adapter_type", sa.String(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("cost_policy", sa.JSON(), nullable=False),
        sa.Column("endpoint", sa.String(), nullable=True),
        sa.Column("config_ref", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "task",
        _common_id(),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("assigned_agent_id", sa.String(), nullable=True),
        sa.Column("adapter_type", sa.String(), nullable=False),
        sa.Column("input_context_refs", sa.JSON(), nullable=False),
        sa.Column("acceptance_criteria", sa.JSON(), nullable=False),
        sa.Column("depends_on", sa.JSON(), nullable=False),
        sa.Column("estimated_cost", sa.Float(), nullable=False),
        sa.Column("actual_cost", sa.Float(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(["assigned_agent_id"], ["agent.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_project_id", "task", ["project_id"])
    op.create_index("ix_task_assigned_agent_id", "task", ["assigned_agent_id"])
    op.create_table(
        "artifact",
        _common_id(),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("uri", sa.String(), nullable=False),
        sa.Column("checksum", sa.String(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifact_project_id", "artifact", ["project_id"])
    op.create_index("ix_artifact_task_id", "artifact", ["task_id"])
    op.create_table(
        "approval",
        _common_id(),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("action_type", sa.String(), nullable=False),
        sa.Column("risk_level", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("rationale", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_approval_project_id", "approval", ["project_id"])
    op.create_index("ix_approval_task_id", "approval", ["task_id"])
    op.create_index("ix_approval_status", "approval", ["status"])
    op.create_table(
        "event",
        _common_id(),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_event_project_id", "event", ["project_id"])
    op.create_index("ix_event_task_id", "event", ["task_id"])
    op.create_index("ix_event_type", "event", ["type"])
    op.create_index("ix_event_status", "event", ["status"])
    op.create_index("ix_event_idempotency_key", "event", ["idempotency_key"], unique=True)


def downgrade() -> None:
    op.drop_table("event")
    op.drop_table("approval")
    op.drop_table("artifact")
    op.drop_table("task")
    op.drop_table("agent")
    op.drop_table("project")
