"""Review binding immutability (#69, REQUEST CHANGES round).

The architecture review rejected stashing the review binding in the target
Artifact's mutable ``metadata_json["review_binding"]`` (it is neither
immutable nor server-owned). This migration introduces the smallest safe
schema change to make the binding durable and server-owned:

  * new table ``review_assignment`` -- one immutable row per Review Task,
    persisting the exact (review_task_id, target_artifact_id, review_policy_id,
    review_round, reviewer_agent_id, review_dimension). This is the single
    source of truth for review binding (req 1/2/6).
  * ``review_result.review_task_id`` (+ unique index) -- durable 1:1 link from a
    ReviewResult back to its trusted Review Task (provenance, req 6).
  * ``review_result.review_round`` / ``review_result.review_dimension`` -- round
    and the single assigned dimension, so old-round results never satisfy a new
    round and the dimension cannot be forged (req 2/3/6).
  * ``approval.target_artifact_id`` / ``approval.review_policy_id`` /
    ``approval.review_round`` -- bind the owner final-approval gate to the exact
    target + policy + round so an old Approval can never approve a new revision
    (req 5).

DB-level uniqueness (req 4) -- beyond the service layer, the schema itself
rejects duplicate concurrency:

  * ``uq_review_assignment_binding`` -- unique on
    (target_artifact_id, review_policy_id, review_round, reviewer_agent_id,
    review_dimension) so the same binding is never dispatched twice; concurrent
    dispatch converges to one immutable row.
  * ``uq_review_result_review_artifact_id`` -- unique on ``review_artifact_id``
    (Option A provenance, req 3): a Review Artifact maps to exactly one
    ReviewResult, persisted directly (never via mutable metadata_json).
  * ``uq_approval_gate_round`` -- unique on
    (target_artifact_id, review_policy_id, review_round, action_type): one
    review-gate Approval per (target, policy, round).
  * ``ix_task_idempotency_key`` (unique) -- ``task.idempotency_key`` holds the
    server-determined revision identity ``review-revision:{source}:{round}``
    (req 1); duplicate/concurrent revision requests converge to one Task.

Why raw ``ALTER TABLE ... ADD COLUMN ... REFERENCES`` (not ``batch_alter_table``):
a pre-existing trigger (``knowledge_candidate_validate_insert``) references
``main.artifact`` by literal name; SQLite's batch recreate transiently renames
``artifact`` and breaks the trigger ("no such table: main.artifact"). Raw ADD
COLUMN touches no trigger (same rationale as migration 20260719_0004). No backfill
is needed: all new columns are NULL/DEFAULT and every existing ``REVIEW_PASSED``
enum value already exists in the model (VARCHAR) so no data migration is required.

Revision ID: 20260720_0006
Revises: 20260720_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260720_0006"
down_revision: str | None = "20260720_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Immutable server-owned binding table (one row per Review Task).
    op.create_table(
        "review_assignment",
        sa.Column("review_task_id", sa.String(), nullable=False),
        sa.Column("target_artifact_id", sa.String(), nullable=False),
        sa.Column("review_policy_id", sa.String(), nullable=False),
        sa.Column("review_round", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("reviewer_agent_id", sa.String(), nullable=False),
        sa.Column("review_dimension", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["review_task_id"], ["task.id"]),
        sa.ForeignKeyConstraint(["target_artifact_id"], ["artifact.id"]),
        sa.ForeignKeyConstraint(["review_policy_id"], ["review_policy.id"]),
        sa.ForeignKeyConstraint(["reviewer_agent_id"], ["agent.id"]),
        sa.PrimaryKeyConstraint("review_task_id"),
    )
    op.create_index(
        "ix_review_assignment_target_artifact_id",
        "review_assignment",
        ["target_artifact_id"],
    )
    op.create_index(
        "ix_review_assignment_review_policy_id",
        "review_assignment",
        ["review_policy_id"],
    )
    op.create_index(
        "ix_review_assignment_reviewer_agent_id",
        "review_assignment",
        ["reviewer_agent_id"],
    )
    # Binding uniqueness (req 4): identical (target, policy, round, reviewer,
    # dimension) must never be dispatched twice. Enforced at the DB level via a
    # unique index so concurrent dispatch converges to the same immutable row.
    op.create_index(
        "uq_review_assignment_binding",
        "review_assignment",
        [
            "target_artifact_id",
            "review_policy_id",
            "review_round",
            "reviewer_agent_id",
            "review_dimension",
        ],
        unique=True,
    )

    # 2. ReviewResult binding provenance. review_task_id is a unique FK to task.id
    #    (1:1 durable link); review_round + review_dimension are NOT NULL/default.
    #    Raw ADD COLUMN REFERENCES keeps the knowledge_* trigger intact.
    op.execute(
        "ALTER TABLE review_result "
        "ADD COLUMN review_task_id TEXT REFERENCES task(id)"
    )
    op.create_index(
        "uq_review_result_review_task_id",
        "review_result",
        ["review_task_id"],
        unique=True,
    )
    # Provenance (Option A, req 3): the independent Review Artifact that produced
    # this verdict, persisted directly (unique, never via mutable metadata_json).
    op.execute(
        "ALTER TABLE review_result "
        "ADD COLUMN review_artifact_id TEXT REFERENCES artifact(id)"
    )
    op.create_index(
        "uq_review_result_review_artifact_id",
        "review_result",
        ["review_artifact_id"],
        unique=True,
    )
    op.add_column(
        "review_result",
        sa.Column("review_round", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "review_result",
        sa.Column("review_dimension", sa.String(), nullable=True),
    )

    # 3. Approval round binding (req 5). Raw ADD COLUMN REFERENCES for FKs.
    op.execute(
        "ALTER TABLE approval "
        "ADD COLUMN target_artifact_id TEXT REFERENCES artifact(id)"
    )
    op.create_index(
        "ix_approval_target_artifact_id", "approval", ["target_artifact_id"]
    )
    op.execute(
        "ALTER TABLE approval "
        "ADD COLUMN review_policy_id TEXT REFERENCES review_policy(id)"
    )
    op.create_index(
        "ix_approval_review_policy_id", "approval", ["review_policy_id"]
    )
    op.add_column(
        "approval",
        sa.Column("review_round", sa.Integer(), nullable=False, server_default="1"),
    )
    # Gate uniqueness (req 4/5): one review-gate Approval per (target, policy,
    # round). Unique index so an old Approval can never approve a new revision.
    op.create_index(
        "uq_approval_gate_round",
        "approval",
        ["target_artifact_id", "review_policy_id", "review_round", "action_type"],
        unique=True,
    )

    # 4. Task structured idempotency key (req 1): server-determined identity for
    #    owner-requested revision dedup. Raw ADD COLUMN keeps triggers intact.
    op.execute("ALTER TABLE task ADD COLUMN idempotency_key TEXT")
    op.create_index(
        "ix_task_idempotency_key", "task", ["idempotency_key"], unique=True
    )


def downgrade() -> None:
    # 4. Task idempotency key.
    op.drop_index("ix_task_idempotency_key", table_name="task")
    op.execute("ALTER TABLE task DROP COLUMN idempotency_key")

    # 3. Approval round binding + gate uniqueness.
    op.drop_index("uq_approval_gate_round", table_name="approval")
    op.drop_index("ix_approval_review_policy_id", table_name="approval")
    op.execute("ALTER TABLE approval DROP COLUMN review_policy_id")
    op.drop_index("ix_approval_target_artifact_id", table_name="approval")
    op.execute("ALTER TABLE approval DROP COLUMN target_artifact_id")
    op.drop_column("approval", "review_round")

    # 2. ReviewResult binding provenance + artifact provenance.
    op.drop_index("uq_review_result_review_artifact_id", table_name="review_result")
    op.execute("ALTER TABLE review_result DROP COLUMN review_artifact_id")
    op.drop_index("uq_review_result_review_task_id", table_name="review_result")
    op.execute("ALTER TABLE review_result DROP COLUMN review_task_id")
    op.drop_column("review_result", "review_dimension")
    op.drop_column("review_result", "review_round")

    # 1. Immutable binding table (dropping the table removes its unique index).
    op.drop_index(
        "ix_review_assignment_reviewer_agent_id", table_name="review_assignment"
    )
    op.drop_index(
        "ix_review_assignment_review_policy_id", table_name="review_assignment"
    )
    op.drop_index(
        "ix_review_assignment_target_artifact_id", table_name="review_assignment"
    )
    op.drop_table("review_assignment")
