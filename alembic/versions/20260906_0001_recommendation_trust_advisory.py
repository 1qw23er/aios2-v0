"""Debt & hygiene slice -- additive 1 column: ``recommendation.trust_advisory``.

G-E item of the W1--W7 architecture checkpoint debt register
(``docs/workforce/Workforce_Architecture_Checkpoint_W1-W7.md``): candidate
quality signalling did not consult the agent trust axis. This migration adds
ONE nullable advisory text column to ``recommendation``; it is populated live
from the Agent registry (SSoT) by ``workforce_recommendation
._build_trust_advisory`` ONLY when the agent's trust level does not clear the
delegation boundary. Advisory text only -- never a score component, never a
gate (mirrors ``cost_advisory`` / F-R5).

Purely additive: no existing column is touched, no new table, no FK. Fully
reversible: ``downgrade()`` drops the column with no residue.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260906_0001_recommendation_trust_advisory"
down_revision: str | None = "20260904_0001_workforce_cost_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "recommendation",
        # Nullable advisory text: None == "nothing to advise" (trust level
        # clears the delegation boundary, or agent missing -- never fabricated).
        sa.Column("trust_advisory", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("recommendation", "trust_advisory")
