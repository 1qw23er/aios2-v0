"""Independent Review Protocol (#64).

Adds revision lineage to ``artifact`` and the two review tables used by the
Agent management/collaboration layer:

  * ``artifact.revision_count`` / ``artifact.revision_of`` -- revision loop links
  * ``review_policy``   -- per-scenario review configuration (dimensions, trust
    floor, optional capability requirements, brand Policy, max_revisions)
  * ``review_result``   -- one independent, multi-dimension review of an Artifact

``artifact.revision_of`` is a *physical* self-referencing foreign key to
``artifact.id`` with ``ON DELETE SET NULL``: if a parent artifact is removed,
its children keep their rows but lose the lineage pointer (they become orphans
rather than being cascade-deleted). An index (``ix_artifact_revision_of``)
backs the link.

The ORM model (aios.models.Artifact.revision_of) declares the same
constraint (sa_column with ForeignKey(ondelete="SET NULL")), so this migration
is the single source of truth for the database schema -- there is no
ORM/migration drift, and referential integrity IS enforced by the database.

Implementation note -- why NOT batch_alter_table:
    A pre-existing trigger (``knowledge_candidate_validate_insert`` from the
    knowledge engine, migration 20260716_0005) references ``main.artifact``
    by literal schema-qualified name. SQLite's batch recreate path renames
    ``artifact`` -> ``_alembic_tmp_artifact`` -> ``artifact``; during the
    transient rename ``main.artifact`` does not exist and the trigger fails to
    validate ("no such table: main.artifact"). A plain
    ``ALTER TABLE ... ADD COLUMN ... REFERENCES`` creates the physical FK and
    the index WITHOUT any table recreation, so the trigger is never disturbed
    and the knowledge_* triggers remain intact across the migration.

Revision ID: 20260719_0004
Revises: 20260719_0003
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260719_0004"
down_revision: str | None = "20260719_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "artifact",
        sa.Column("revision_count", sa.Integer(), nullable=False, server_default="0"),
    )
    # Physical self-referencing FK with deliberate ON DELETE SET NULL.
    # Alembic's ``op.add_column`` routes an inline ``ForeignKey`` to an
    # ``ALTER ... ADD CONSTRAINT`` path, which SQLite's dialect rejects
    # (NotImplementedError: "No support for ALTER of constraints"). SQLite
    # *natively* supports ``ALTER TABLE ... ADD COLUMN ... REFERENCES ...
    # ON DELETE SET NULL``, so we issue the DDL directly. This also avoids
    # ``batch_alter_table``: a pre-existing trigger
    # (``knowledge_candidate_validate_insert`` from the knowledge engine,
    # migration 20260716_0005) references ``main.artifact`` by literal
    # name, and batch's transient rename breaks its validation
    # ("no such table: main.artifact"). Raw ADD COLUMN touches no trigger
    # and the knowledge_* triggers remain intact.
    op.execute(
        "ALTER TABLE artifact "
        "ADD COLUMN revision_of TEXT REFERENCES artifact(id) ON DELETE SET NULL"
    )
    op.create_index("ix_artifact_revision_of", "artifact", ["revision_of"])

    op.create_table(
        "review_policy",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("applies_to", sa.String(), nullable=False, server_default=""),
        sa.Column("dimensions", sa.JSON(), nullable=False),
        sa.Column(
            "brand_policy_id", sa.String(), sa.ForeignKey("policy.id"), nullable=True
        ),
        sa.Column(
            "required_reviewer_trust",
            sa.String(),
            nullable=False,
            server_default="verified_external",
        ),
        sa.Column("required_capabilities", sa.JSON(), nullable=False),
        sa.Column("max_revisions", sa.Integer(), nullable=False, server_default="2"),
        # Trust boundary #4: minimum independent reviewers before the artifact
        # may be aggregated to APPROVED (a single reviewer never approves).
        sa.Column("required_reviewers", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("project_id", sa.String(), sa.ForeignKey("project.id"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "review_result",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("artifact_id", sa.String(), sa.ForeignKey("artifact.id"), nullable=False),
        sa.Column("reviewer_type", sa.String(), nullable=False),
        sa.Column("reviewer_agent_id", sa.String(), sa.ForeignKey("agent.id"), nullable=True),
        sa.Column("user_id", sa.String(), nullable=True),
        # Policy traceability (#1): which policy produced this verdict.
        sa.Column(
            "policy_id", sa.String(), sa.ForeignKey("review_policy.id"), nullable=True
        ),
        # Immutable snapshot hash of the policy at submit time.
        sa.Column("policy_hash", sa.String(), nullable=True),
        # Idempotency (#3): identity hash (artifact + reviewer + policy).
        # Unique per reviewer verdict; NULL only for untracked (no-policy) reviews.
        sa.Column("idempotency_key", sa.String(), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_review_result_idempotency"),
        sa.Column("dimensions", sa.JSON(), nullable=False),
        sa.Column("overall", sa.String(), nullable=False),
        sa.Column("reviewer_score", sa.Float(), nullable=True),
        sa.Column("usefulness", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("review_result")
    op.drop_table("review_policy")
    op.drop_index("ix_artifact_revision_of", table_name="artifact")
    # Drop the physical FK column (DROP COLUMN removes the column and its FK).
    op.execute("ALTER TABLE artifact DROP COLUMN revision_of")
    op.drop_column("artifact", "revision_count")
