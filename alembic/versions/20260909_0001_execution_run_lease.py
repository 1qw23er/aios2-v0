"""Execution Run Lifecycle & Recovery P0: durable lease on ``delegated_run``.

Adds two nullable columns to the existing ``delegated_run`` table -- the ONLY
schema change for this P0:

* ``lease_owner``      -- opaque execution-worker identity (a per-process token,
  never an ``agent_id`` / ``employee_id`` / runtime entity).
* ``lease_expires_at`` -- UTC deadline after which the lease is void and the run
  may be re-acquired or reclaimed by the fail-closed recovery scan.

There is deliberately NO new ``Runtime`` / ``RuntimeRegistration`` / ``Execution``
/ ``Worker`` table, and no ``runtime_id`` on ``Task`` / ``ExecutionAssignment`` /
``DelegatedRun``. ``DelegatedRun`` + ``DelegatedRunStatus`` remain the single
execution record and state machine; ``EXPIRED`` (already in the enum) is reused
as the recovery terminal state, so no enum migration is needed.

Reversibility: downgrade drops both columns, leaving ``delegated_run`` exactly as
before.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260909_0001_execution_run_lease"
down_revision = "20260908_0001_runtime_heartbeat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "delegated_run",
        sa.Column("lease_owner", sa.String(), nullable=True),
    )
    op.add_column(
        "delegated_run",
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("delegated_run", "lease_expires_at")
    op.drop_column("delegated_run", "lease_owner")
