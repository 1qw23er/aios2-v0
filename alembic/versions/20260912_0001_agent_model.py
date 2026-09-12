"""Add per-agent model column for heterogeneous LLM backends.

``Agent.model`` (nullable str) lets a department use a DIFFERENT model than the
global ``AIOS_AGENT_MODEL``. ``factory.py`` passes it to
``LLMExecutionAdapter``; ``None`` falls back to the env default, so existing
agents (which leave this unset) keep today's behavior unchanged.

No state machine, no budget semantics -- purely an additive nullable column.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260912_0001_agent_model"
down_revision = "20260909_0004_callback_ingest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent") as batch_op:
        batch_op.add_column(sa.Column("model", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent") as batch_op:
        batch_op.drop_column("model")
