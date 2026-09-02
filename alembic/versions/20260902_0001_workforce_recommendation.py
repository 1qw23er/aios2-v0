"""W3-C Recommendation + L4 human gate -- additive 1 table (+2 indexes).

W3-C adds the proposal/decision layer on top of W3-B (head
``20260901_0001_workforce_match_benchmark``). This migration is *purely
additive*:

* creates ``recommendation``
* creates indexes ``ix_recommendation_status`` / ``ix_recommendation_decided_by``

No existing W1/W2/W3-A/W3-B table is altered -- there is not even an
``add_column`` here. Fully reversible: ``downgrade()`` drops the two indexes and
the table, returning to the W3-B head with no residue.

Index count is deliberately minimal (R7 ruling 2026-09-02, resolving the
§4.1 vs §12.1 conflict of Spec V4): only ``status`` and ``decided_by`` are
indexed. ``candidate_id`` is already the leftmost column of
``uq_recommendation_candidate_job_version`` (which SQLite backs with an implicit
index), and ``approval_id`` is a forward-only column that is always NULL in V1.
Further indexes may be added later as their own additive revision.

``match_id`` uses RESTRICT (C4): deleting a Match that still backs a
recommendation would leave a dangling proposal, so the delete is refused.

All THREE parent FKs of ``recommendation`` are RESTRICT (DR-1 correction): the
original §12.4 DR-4 said "RESTRICT only on ``match_id``", but that wording is
internally inconsistent with the hard contract DR-1 ("a recommendation must
survive a job delete"). A recommendation is bound to a ``(candidate,
job_version)`` pair and to a ``match``; a CASCADE on any of those three parents
would orphan/destroy it *before* ``match_id``'s RESTRICT could fire. Concretely:
``job_version_id`` CASCADE alone cascade-deletes the recommendation the moment the
job_version is removed (which happens first in the job-delete cascade), so the
recommendation is destroyed silently. Making ``candidate_id`` AND
``job_version_id`` RESTRICT (alongside ``match_id``) means any delete that would
orphan or destroy a recommendation is refused. The unlock path is
withdraw/reject followed by ``purge_recommendation``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260902_0001_workforce_recommendation"
down_revision: str | None = "20260901_0001_workforce_match_benchmark"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recommendation",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("candidate_id", sa.String(), nullable=False),
        sa.Column("job_version_id", sa.String(), nullable=False),
        sa.Column("match_id", sa.String(), nullable=False),
        # F-R8 drift token (C8): the attempt this row was built from. NOT NULL --
        # F-R3b rejects unresolvable evidence with 422 before a row is written.
        sa.Column("match_attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("proposed_action", sa.String(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("weights_version", sa.String(), nullable=False),
        sa.Column("breakdown", sa.JSON(), nullable=False),
        sa.Column("evaluated_fields", sa.JSON(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("excluded_fields", sa.JSON(), nullable=False),
        sa.Column("unknown_dimensions", sa.JSON(), nullable=False),
        sa.Column("cost_advisory", sa.String(), nullable=True),
        sa.Column("rationale", sa.String(), nullable=False),
        sa.Column("risk_level", sa.String(), nullable=False),
        sa.Column("approval_id", sa.String(), nullable=True),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decision_rationale", sa.String(), nullable=True),
        sa.Column("recommender", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidate.id"],
            ondelete="RESTRICT",
            name="fk_recommendation_candidate_id",
        ),
        sa.ForeignKeyConstraint(
            ["job_version_id"],
            ["job_version.id"],
            ondelete="RESTRICT",
            name="fk_recommendation_job_version_id",
        ),
        sa.ForeignKeyConstraint(
            ["match_id"],
            ["match.id"],
            ondelete="RESTRICT",
            name="fk_recommendation_match_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_id",
            "job_version_id",
            name="uq_recommendation_candidate_job_version",
        ),
    )
    op.create_index(
        "ix_recommendation_status", "recommendation", ["status"]
    )
    op.create_index(
        "ix_recommendation_decided_by", "recommendation", ["decided_by"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_recommendation_decided_by", table_name="recommendation"
    )
    op.drop_index("ix_recommendation_status", table_name="recommendation")
    op.drop_table("recommendation")
