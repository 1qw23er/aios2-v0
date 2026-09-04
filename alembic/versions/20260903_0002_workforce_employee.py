"""W4 Employee -- additive 1 table + 4 columns on ``trial`` (0 explicit indexes).

W4 closes the Workforce loop by giving the Trial an actual lifecycle (the four
columns below) and by adding the terminal ``Employee`` record (head
``20260903_0001_workforce_trial``). This migration is *purely additive*:

* adds ``trial_plan_ref`` / ``started_at`` / ``ended_at`` / ``outcome`` to
  ``trial`` (all nullable, no FK -- plain ``op.add_column`` on SQLite);
* creates ``employee``.

No existing W1/W2/W3-A/B/C/D table is altered beyond those four nullable
columns. Fully reversible: ``downgrade()`` drops the table and the four columns,
returning to the W3-D head with no residue.

Index count is deliberately minimal (R7 ruling 2026-09-02): the only query
patterns are "look up an Employee by its Trial" (covered by the implicit index
of ``uq_employee_trial``) and "look up an Employee by candidate" (a candidate
has at most one employee in V1, so no dedicated index is warranted). ``status``
is a single-valued column in V1, so no explicit index is created.

All FKs except ``agent_id`` are RESTRICT (DR-1 lineage). ``agent_id`` is
``NO ACTION`` -- a soft reference to the Alpha-1 Agent Registry, copied from
the Candidate at promotion time and never re-resolved (F-E19). W4 ships NO
unlock / delete path for an Employee (D-4); delete semantics are a W5
obligation.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260903_0002_workforce_employee"
down_revision: str | None = "20260903_0001_workforce_trial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Four nullable, FK-free columns on ``trial`` -- the Trial lifecycle
    # payload (W4 §5). Plain ADD COLUMN; SQLite handles it without batch mode.
    op.add_column("trial", sa.Column("trial_plan_ref", sa.String(), nullable=True))
    op.add_column("trial", sa.Column("started_at", sa.DateTime(), nullable=True))
    op.add_column("trial", sa.Column("ended_at", sa.DateTime(), nullable=True))
    op.add_column("trial", sa.Column("outcome", sa.String(), nullable=True))

    op.create_table(
        "employee",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("candidate_id", sa.String(), nullable=False),
        sa.Column("trial_id", sa.String(), nullable=False),
        # Soft registry reference -- copied from the Candidate (never re-resolved).
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("job_version_id", sa.String(), nullable=False),
        # V1 has exactly one status and one writer (``promote_to_employee``).
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("hired_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidate.id"],
            ondelete="RESTRICT",
            name="fk_employee_candidate_id",
        ),
        sa.ForeignKeyConstraint(
            ["trial_id"],
            ["trial.id"],
            ondelete="RESTRICT",
            name="fk_employee_trial_id",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agent.id"],
            ondelete="NO ACTION",
            name="fk_employee_agent_id",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            ondelete="RESTRICT",
            name="fk_employee_job_id",
        ),
        sa.ForeignKeyConstraint(
            ["job_version_id"],
            ["job_version.id"],
            ondelete="RESTRICT",
            name="fk_employee_job_version_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        # Idempotency anchor (Q8 / F-E21): one Employee per Trial.
        sa.UniqueConstraint(
            "trial_id",
            name="uq_employee_trial",
        ),
    )


def downgrade() -> None:
    op.drop_table("employee")
    # Drop the four lifecycle columns. SQLite needs batch mode to drop columns;
    # the surrounding table recreation is internal to Alembic and lossless.
    with op.batch_alter_table("trial") as batch_op:
        batch_op.drop_column("outcome")
        batch_op.drop_column("ended_at")
        batch_op.drop_column("started_at")
        batch_op.drop_column("trial_plan_ref")
