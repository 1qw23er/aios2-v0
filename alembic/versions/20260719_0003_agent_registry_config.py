"""agent registry per-agent delegation config (#57).

Adds per-agent tuning columns to ``agent`` so the Agent Interoperability
Gateway can honor a DB-backed registry (design review v2 Q1) without code
changes:

  * ``timeout_s``   -- per-delegation wall-clock ceiling before EXPIRED (default 300s)
  * ``max_retries`` -- retry attempts on transient failure before TASK FAILED (default 3)

Revision ID: 20260719_0003
Revises: 20260719_0002
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260719_0003"
down_revision: str | None = "20260719_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent") as batch:
        batch.add_column(
            sa.Column("timeout_s", sa.Float(), nullable=False, server_default="300.0")
        )
        batch.add_column(
            sa.Column("max_retries", sa.Integer(), nullable=False, server_default="3")
        )


def downgrade() -> None:
    with op.batch_alter_table("agent") as batch:
        batch.drop_column("max_retries")
        batch.drop_column("timeout_s")
