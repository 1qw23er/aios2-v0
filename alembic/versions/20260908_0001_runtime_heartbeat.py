"""Runtime Thin Layer P1: agent liveness heartbeat.

Adds a single nullable ``last_heartbeat_at`` column to the ``agent`` table. This
is the ONLY schema change for Runtime P1 -- there is deliberately NO ``Runtime`` /
``RuntimeRegistration`` table (C1/R1). The column is server-generated on
heartbeat and is never client-set; ``STALE`` is a *computed* state in
``aios.scheduler._candidate`` (C2/C5/R7), not a persisted enum, so no enum
migration is needed.

Reversibility: downgrade drops the column, leaving ``agent`` exactly as before.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260908_0001_runtime_heartbeat"
down_revision = "20260907_0001_skill_system"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent",
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent", "last_heartbeat_at")
