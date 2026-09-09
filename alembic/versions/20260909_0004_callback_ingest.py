"""Callback / Webhook Ingest P1 — callback evidence on ``delegated_run``.

Two nullable columns on the EXISTING attempt record (no new Callback /
Webhook / Ingest entity, no enum migration):

* ``callback_received_at`` (nullable DateTime) — when an authenticated provider
  callback was acknowledged on this run. ``NULL`` means "no callback received".
* ``callback_payload`` (nullable JSON) — the redacted provider signal
  (status / error / cost / usage / correlation) plus the token ``jti`` used for
  duplicate detection. Evidence only: it is untrusted provider input and is
  never a status, never a cost posting, never a budget entry.

Terminalization and budget accrual remain the exclusive job of the existing
lease-owning execution path, so this migration adds NO state machine and NO
budget semantics -- it only gives that path a place to look for a pushed
signal.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260909_0004_callback_ingest"
down_revision = "20260909_0003_unified_attempt_usage_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("delegated_run") as batch_op:
        batch_op.add_column(
            sa.Column("callback_received_at", sa.DateTime(), nullable=True)
        )
        batch_op.add_column(sa.Column("callback_payload", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("delegated_run") as batch_op:
        batch_op.drop_column("callback_payload")
        batch_op.drop_column("callback_received_at")
