"""Agent Interoperability Gateway hardening (#104): trust level + budget usage.

Builds on #103 (adapter mock self-tests, ``20260719_0001``). This migration adds
the two columns the #104 hardening logic needs:

  * ``agent.trust_level`` — INTERNAL / VERIFIED_EXTERNAL / EXPERIMENTAL. Used at the
    delegation boundary to restrict external-execution capabilities (no generic
    permission framework; a single trust axis on the agent row).
  * ``project.budget_used`` — running total of spend accrued by delegated runs,
    compared against the already-existing ``project.budget_limit`` to HARD-block
    remote execution when the project is over budget.

Neither column carries any credential; ``secret_ref`` remains an opaque handle.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260719_0002"
down_revision: str | None = "20260719_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Agent trust axis (single dimension, not a permission system).
    with op.batch_alter_table("agent") as batch:
        batch.add_column(
            sa.Column("trust_level", sa.String(), nullable=False, server_default="internal")
        )

    # Running budget consumption for hard-block enforcement.
    with op.batch_alter_table("project") as batch:
        batch.add_column(
            sa.Column("budget_used", sa.Float(), nullable=False, server_default="0.0")
        )


def downgrade() -> None:
    with op.batch_alter_table("project") as batch:
        batch.drop_column("budget_used")
    with op.batch_alter_table("agent") as batch:
        batch.drop_column("trust_level")
