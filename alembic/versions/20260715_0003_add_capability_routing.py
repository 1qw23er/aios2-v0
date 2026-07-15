"""Add deterministic capability routing models."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260715_0003"
down_revision: str | None = "20260715_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "capability",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_capability_name", "capability", ["name"], unique=True)
    op.create_table(
        "agent_capability",
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("capability_id", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "priority >= 1 AND priority <= 100",
            name="ck_agent_capability_priority",
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"]),
        sa.ForeignKeyConstraint(["capability_id"], ["capability.id"]),
        sa.PrimaryKeyConstraint("agent_id", "capability_id"),
    )
    with op.batch_alter_table("agent") as batch:
        batch.add_column(
            sa.Column("status", sa.String(), server_default="AVAILABLE", nullable=False)
        )
        batch.create_index("ix_agent_status", ["status"])
    with op.batch_alter_table("task") as batch:
        batch.add_column(sa.Column("preferred_agent_id", sa.String(), nullable=True))
        batch.add_column(
            sa.Column("required_capabilities", sa.JSON(), server_default="[]", nullable=False)
        )
        batch.add_column(
            sa.Column("routing_mode", sa.String(), server_default="FIXED", nullable=False)
        )
        batch.create_foreign_key(
            "fk_task_preferred_agent_id_agent",
            "agent",
            ["preferred_agent_id"],
            ["id"],
        )
        batch.create_index("ix_task_preferred_agent_id", ["preferred_agent_id"])
        batch.create_index("ix_task_routing_mode", ["routing_mode"])
    op.create_table(
        "execution_assignment",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("selected_agent_id", sa.String(), nullable=False),
        sa.Column("routing_reason", sa.String(), nullable=False),
        sa.Column("fallback_used", sa.Boolean(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["selected_agent_id"], ["agent.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_execution_assignment_idempotency_key",
        "execution_assignment",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_execution_assignment_selected_agent_id",
        "execution_assignment",
        ["selected_agent_id"],
    )
    op.create_index(
        "ix_execution_assignment_task_id",
        "execution_assignment",
        ["task_id"],
    )


def downgrade() -> None:
    op.drop_table("execution_assignment")
    with op.batch_alter_table("task") as batch:
        batch.drop_index("ix_task_routing_mode")
        batch.drop_index("ix_task_preferred_agent_id")
        batch.drop_constraint("fk_task_preferred_agent_id_agent", type_="foreignkey")
        batch.drop_column("routing_mode")
        batch.drop_column("required_capabilities")
        batch.drop_column("preferred_agent_id")
    with op.batch_alter_table("agent") as batch:
        batch.drop_index("ix_agent_status")
        batch.drop_column("status")
    op.drop_table("agent_capability")
    op.drop_index("ix_capability_name", table_name="capability")
    op.drop_table("capability")
