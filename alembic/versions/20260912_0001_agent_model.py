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
    # Plain ADD/DROP COLUMN, NOT ``batch_alter_table`` -- same choice as
    # 20260908_0001_runtime_heartbeat for this exact table. Batch mode recreates
    # the table on SQLite (CREATE new / copy / DROP old / RENAME), and the
    # ``DROP TABLE agent`` step fails with a FOREIGN KEY constraint error as soon
    # as any child row exists (agent_capability.agent_id, task.assigned_agent_id,
    # employee_agent_binding.agent_id all reference ``agent``). A non-indexed
    # nullable column needs no recreate, so the plain DDL is both sufficient and
    # the only reversible form here.
    op.add_column("agent", sa.Column("model", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent", "model")
