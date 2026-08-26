"""Workforce Management W1 core -- business goal -> job -> capability requirement.

Implements the V1.1 Workforce Architecture minimum closed loop:

    business_goal -> required_work -> job -> job_version -> capability_requirement

Design constraints (enforced by the schema + the ``aios.workforce`` service):

* Purely additive -- only NEW tables, no alteration of any existing table. It
  sits ON TOP of ``20260824_0001_series_id_json_guard`` and becomes the new
  single alembic head. Existing tables (project / agent / capability / task / ...)
  are untouched.
* Job is the first-class citizen: ``required_work``, ``job_version`` and
  ``capability_requirement`` all descend from ``job``.
* ``job_version`` is an immutable snapshot: each requirement change mints a new
  version (unique ``(job_id, version)``). ``job.head_version_id`` points at the
  active version.
* ``capability_requirement.capability_id`` is a FK to the Alpha-1 ``capability``
  SSoT ONLY. No second capability vocabulary is created. The service resolves a
  capability *name* slug to this id and fails closed (422) on unknown slugs.
* FK cascades delete the whole subtree when a BusinessGoal is removed, so no
  orphan rows can linger.

Downgrade drops the five new tables (reverse order). Because this migration only
adds tables (no data backfill), downgrade fully reverses it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260825_0001_workforce_core"
down_revision: str | None = "20260824_0001_series_id_json_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "business_goal",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("target_outcome", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "priority >= 0 AND priority <= 100", name="ck_business_goal_priority"
        ),
    )
    op.create_index("ix_business_goal_owner", "business_goal", ["owner"])

    op.create_table(
        "required_work",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("business_goal_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("rationale", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["business_goal_id"], ["business_goal.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "priority >= 0 AND priority <= 100", name="ck_required_work_priority"
        ),
    )
    op.create_index(
        "ix_required_work_business_goal_id", "required_work", ["business_goal_id"]
    )

    op.create_table(
        "job",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("required_work_id", sa.String(), nullable=False),
        # Back-reference to the active version; plain FK (no cascade): JobVersion
        # rows are never individually deleted, so this is always resolvable.
        sa.Column("head_version_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("role_summary", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["required_work_id"], ["required_work.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["head_version_id"], ["job_version.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_required_work_id", "job", ["required_work_id"])

    op.create_table(
        "job_version",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title_snapshot", sa.String(), nullable=False),
        sa.Column("description_snapshot", sa.String(), nullable=False),
        sa.Column("role_summary_snapshot", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "version", name="uq_job_version_per_job"),
    )
    op.create_index("ix_job_version_job_id", "job_version", ["job_id"])

    op.create_table(
        "capability_requirement",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("job_version_id", sa.String(), nullable=False),
        # FK to the Alpha-1 Capability SSoT ONLY. No second capability table.
        sa.Column("capability_id", sa.String(), nullable=False),
        sa.Column("capability_name", sa.String(), nullable=False),
        sa.Column("min_proficiency", sa.Integer(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_version_id"], ["job_version.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["capability_id"], ["capability.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "min_proficiency >= 1 AND min_proficiency <= 100",
            name="ck_capability_requirement_proficiency",
        ),
    )
    op.create_index(
        "ix_capability_requirement_job_version_id",
        "capability_requirement",
        ["job_version_id"],
    )
    op.create_index(
        "ix_capability_requirement_capability_id",
        "capability_requirement",
        ["capability_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_capability_requirement_capability_id", table_name="capability_requirement"
    )
    op.drop_index(
        "ix_capability_requirement_job_version_id",
        table_name="capability_requirement",
    )
    op.drop_table("capability_requirement")
    op.drop_index("ix_job_version_job_id", table_name="job_version")
    op.drop_table("job_version")
    op.drop_index("ix_job_required_work_id", table_name="job")
    op.drop_table("job")
    op.drop_index("ix_required_work_business_goal_id", table_name="required_work")
    op.drop_table("required_work")
    op.drop_index("ix_business_goal_owner", table_name="business_goal")
    op.drop_table("business_goal")
