"""Unified Attempt + Usage + Budget Accrual (C).

Schema changes to ``delegated_run`` -- the single attempt record for BOTH remote
delegations and local synchronous LLM attempts:

* ``agent_id`` becomes NULLABLE. A LOCAL attempt (``delegation_mode = 'local'``)
  runs in-process and resolves no department agent, so its attempt row carries
  ``agent_id = NULL``. Every read filters by task_id / run id, never assuming
  agent_id is non-null, so no query breaks.
* ``usage`` becomes NULLABLE. ``None`` means "provider did not report token
  counts" -- deliberately distinct from an empty dict so "no measurement" is
  never treated as "zero usage". AIOS never fabricates token counts.
* Add ``budget_accrued_at`` (nullable DateTime): the idempotency marker for
  budget accrual. Set exactly once, by the single conditional UPDATE that wins
  the accrual race, so a terminal run is charged to ``Project.budget_used`` at
  most once across retries, concurrent terminalizations, and recovery re-runs.

No new table, no enum migration (``DelegationMode.LOCAL`` reuses the existing
VARCHAR column as the string "local").
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260909_0003_unified_attempt_usage_budget"
down_revision = "20260909_0002_task_run_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("delegated_run") as batch_op:
        batch_op.alter_column(
            "agent_id",
            existing_type=sa.String(),
            nullable=True,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "usage",
            existing_type=sa.JSON(),
            nullable=True,
            existing_nullable=False,
        )
        batch_op.add_column(
            sa.Column("budget_accrued_at", sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("delegated_run") as batch_op:
        batch_op.drop_column("budget_accrued_at")
        batch_op.alter_column(
            "usage",
            existing_type=sa.JSON(),
            nullable=False,
            existing_nullable=True,
        )
        batch_op.alter_column(
            "agent_id",
            existing_type=sa.String(),
            nullable=False,
            existing_nullable=True,
        )
