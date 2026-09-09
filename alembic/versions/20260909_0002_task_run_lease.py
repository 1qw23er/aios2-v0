"""Execution Run Lifecycle P0-B: durable lease on ``task``.

Adds two nullable columns to the existing ``task`` table -- the ONLY schema
change for this slice:

* ``lease_owner``      -- opaque execution-worker identity (a per-process token,
  never an ``agent_id`` / ``employee_id`` / runtime entity).
* ``lease_expires_at`` -- UTC deadline after which the lease is void and the
  task may be reclaimed by the fail-closed recovery scan.

There is deliberately NO new ``Runtime`` / ``Execution`` / ``Worker`` table, and
no ``runtime_id`` on ``Task``. ``Task`` + ``TaskStatus`` remain the single task
record and state machine; ``FAILED`` (already in the enum, and terminal AND
retryable) is reused as the recovery terminal state, so no enum migration is
needed.

Reversibility: downgrade drops both columns, leaving ``task`` exactly as before.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260909_0002_task_run_lease"
down_revision = "20260909_0001_execution_run_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task",
        sa.Column("lease_owner", sa.String(), nullable=True),
    )
    op.add_column(
        "task",
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task", "lease_expires_at")
    op.drop_column("task", "lease_owner")