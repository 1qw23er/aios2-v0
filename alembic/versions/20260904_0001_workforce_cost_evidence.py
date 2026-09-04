"""W5 Cost Evidence -- additive 1 table, zero changes to W1--W4 tables.

W5 V1 is bookkeeping-only cost evidence for the Workforce hiring pipeline
(``docs/workforce/Workforce_W5_Design_V1.md``). This migration is *purely
additive*: it creates ``cost_evidence`` and touches NOTHING else.

* ``job_version_id`` -- aggregation anchor (G1 / D-1.1), FK RESTRICT to
  ``job_version.id`` (DR-1 lineage, fail-closed): deleting a JobVersion that
  has cost evidence must FAIL EXPLICITLY, never cascade silently.
* ``employee_id`` -- nullable post-hire attribution, FK RESTRICT to
  ``employee.id``. Moot in practice (Employee is permanent, G4), kept for
  lineage symmetry.
* ``amount`` -- nullable float, mirroring the repo's existing cost columns
  (``Task.estimated_cost`` / ``actual_cost``, ``DelegatedRun.cost`` -- all
  float). A row exists only when a real measured cost exists (I6): "no cost
  yet" = no row, not ``amount = 0``. NO ``currency`` column in V1 (G3/D-1.3).
* ``source_event_type`` / ``source_event_id`` -- provenance of the REAL cost
  source event (I4). V1 has no Workforce-native cost source event in the repo,
  so the table is schema-only and is expected to hold 0 rows until a later
  stage introduces a real, Workforce-attributable source (D-1.4: contract,
  never a fabricated event, and ``delegated_run.id`` is NOT reused as if it
  were Workforce-attributable).
* ``idempotency_key`` -- UNIQUE + indexed, generated as
  ``f"{source_event_type}:{source_event_id}"`` (I5, replay-safe at-most-once).
  Mirrors ``AuditLog.idempotency_key`` (unique index ``ix_audit_log_*``).

No ``Project`` FK is added; no existing ``ON DELETE`` is altered; the 11
frozen Workforce tables (``business_goal`` ... ``employee``) are untouched.
Fully reversible: ``downgrade()`` drops the table with no residue.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260904_0001_workforce_cost_evidence"
down_revision: str | None = "20260903_0002_workforce_employee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cost_evidence",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("job_version_id", sa.String(), nullable=False),
        # Post-hire attribution only; nullable until an Employee bears the cost.
        sa.Column("employee_id", sa.String(), nullable=True),
        # Nullable: only real measured costs are ever recorded (I6).
        sa.Column("amount", sa.Float(), nullable=True),
        # Provenance contract (I4): the real origin event, never fabricated.
        sa.Column("source_event_type", sa.String(), nullable=False),
        sa.Column("source_event_id", sa.String(), nullable=False),
        # Replay-safe at-most-once anchor (I5).
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        # Advisory free text only -- no structured currency/meta column in V1.
        sa.Column("note", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_version_id"],
            ["job_version.id"],
            ondelete="RESTRICT",
            name="fk_cost_evidence_job_version_id",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            ondelete="RESTRICT",
            name="fk_cost_evidence_employee_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # Index naming mirrors the repo convention (``ix_<table>_<column>``), and
    # the idempotency index is UNIQUE exactly like ``ix_audit_log_idempotency_key``.
    op.create_index(
        "ix_cost_evidence_job_version_id",
        "cost_evidence",
        ["job_version_id"],
    )
    op.create_index(
        "ix_cost_evidence_employee_id",
        "cost_evidence",
        ["employee_id"],
    )
    op.create_index(
        "ix_cost_evidence_idempotency_key",
        "cost_evidence",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_cost_evidence_idempotency_key", table_name="cost_evidence")
    op.drop_index("ix_cost_evidence_employee_id", table_name="cost_evidence")
    op.drop_index("ix_cost_evidence_job_version_id", table_name="cost_evidence")
    op.drop_table("cost_evidence")
