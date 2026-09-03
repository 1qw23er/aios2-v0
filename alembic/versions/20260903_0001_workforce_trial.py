"""W3-D Trial -- additive 1 table (0 explicit indexes).

W3-D adds the hand-off record from an APPROVED Recommendation into a trial
(head ``20260902_0001_workforce_recommendation``). This migration is *purely
additive*:

* creates ``trial``

No existing W1/W2/W3-A/W3-B/W3-C table is altered -- there is not even an
``add_column`` here. Fully reversible: ``downgrade()`` drops the table,
returning to the W3-C head with no residue.

Index count is deliberately minimal (R7 ruling 2026-09-02, resolving the
§4.1 vs §12.1 conflict of Spec V4): the ``UNIQUE(recommendation_id)`` constraint
ships with an implicit index that covers the only query pattern (look up a
Trial by its Recommendation). ``status`` has zero selectivity in V1 (a single
value), so no explicit index is created (Spec §3-Q7 / Q7).

All THREE parent FKs of ``trial`` are RESTRICT, mirroring W3-C's DR-1: a trial
is live hiring evidence and must survive a Job / Candidate / Recommendation
delete. RESTRICT is the fail-closed direction -- the delete is refused rather
than silently orphaning or destroying the trial. W3-D ships NO unlock path
(Spec §3-Q2): in V1 a purge could not unblock anything anyway, because the
upstream APPROVED recommendation is already un-purgeable. The unlock is a W4
obligation.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260903_0001_workforce_trial"
down_revision: str | None = "20260902_0001_workforce_recommendation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trial",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("candidate_id", sa.String(), nullable=False),
        sa.Column("job_version_id", sa.String(), nullable=False),
        sa.Column("recommendation_id", sa.String(), nullable=False),
        # Single W3-D state. Stored as a plain string so W4 can add members with
        # zero migration (Spec §3-Q3). V1 writes exactly one value: "proposed".
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidate.id"],
            ondelete="RESTRICT",
            name="fk_trial_candidate_id",
        ),
        sa.ForeignKeyConstraint(
            ["job_version_id"],
            ["job_version.id"],
            ondelete="RESTRICT",
            name="fk_trial_job_version_id",
        ),
        sa.ForeignKeyConstraint(
            ["recommendation_id"],
            ["recommendation.id"],
            ondelete="RESTRICT",
            name="fk_trial_recommendation_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        # Idempotency anchor (Spec §3-Q1): one Trial per approved recommendation.
        sa.UniqueConstraint(
            "recommendation_id",
            name="uq_trial_recommendation",
        ),
    )


def downgrade() -> None:
    op.drop_table("trial")
