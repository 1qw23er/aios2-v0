from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260715_0002"
down_revision: str | None = "20260715_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("before_snapshot", sa.JSON(), nullable=False),
        sa.Column("after_snapshot", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_project_id", "audit_log", ["project_id"])
    op.create_index("ix_audit_log_task_id", "audit_log", ["task_id"])
    op.create_index("ix_audit_log_idempotency_key", "audit_log", ["idempotency_key"], unique=True)
    with op.batch_alter_table("artifact") as batch:
        batch.add_column(sa.Column("external_result_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("result_checksum", sa.String(), nullable=True))
        batch.create_unique_constraint("uq_artifact_external_result_id", ["external_result_id"])


def downgrade() -> None:
    with op.batch_alter_table("artifact") as batch:
        batch.drop_constraint("uq_artifact_external_result_id", type_="unique")
        batch.drop_column("result_checksum")
        batch.drop_column("external_result_id")
    op.drop_table("audit_log")
