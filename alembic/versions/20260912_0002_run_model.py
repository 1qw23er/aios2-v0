"""Record the executing model on each DelegatedRun.

The in-process ``LLMExecutionAdapter`` already forwards its resolved ``model``
into ``complete_local_run(..., model=...)`` (see execution.py), but the
``delegated_run`` table had no column to hold it -- so the value was silently
dropped. With heterogeneous backends (a department pinned to a different model /
agent than the global default), that attribution is exactly what an owner needs
to see WHICH model produced which artifact. This is a purely additive nullable
column; existing rows simply have ``model = NULL``.

Chains after 20260912_0001_agent_model.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260912_0002_run_model"
down_revision = "20260912_0001_agent_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain ADD/DROP COLUMN -- the column is non-indexed and nullable, so no
    # table recreate is needed. See 20260912_0001_agent_model for why batch mode
    # is deliberately avoided (SQLite recreate + FK constraints).
    op.add_column("delegated_run", sa.Column("model", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("delegated_run", "model")
