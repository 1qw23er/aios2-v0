"""W8-v2 Workforce Execution Bridge -- additive 1 table + Employee backfill.

Companion to the W8-v2 Implementation Design V1 (issue #112). This migration is
*purely additive*: it creates ``employee_agent_binding`` and touches NOTHING
else -- the 15 frozen Workforce tables (``business_goal`` ... ``cost_evidence``)
and every execution-plane table (``task`` / ``execution_assignment`` /
``delegated_run`` / ``artifact``) are untouched.

What the table carries
----------------------
``employee_agent_binding`` is the effective-dated Employee <-> Agent execution
binding (the "current execution agent" semantics the frozen
``employee.agent_id`` snapshot deliberately lacks -- F-E19). See the
``EmployeeAgentBinding`` model docstring for the full contract.

* ``employee_id`` -- FK RESTRICT to ``employee.id`` (an Employee is permanent,
  G4; deleting one with bindings must FAIL EXPLICITLY).
* ``agent_id`` -- FK ``NO ACTION`` to ``agent.id``, the same soft Alpha-1
  registry reference policy as ``Employee.agent_id`` (Q6).
* ``effective_from`` / ``effective_to`` -- half-open ``[from, to)`` intervals;
  ``effective_to IS NULL`` marks the CURRENT binding. A CHECK constraint
  (``ck_eab_interval_valid``) keeps every closed interval non-empty.
* Current 1:1 BOTH ways is enforced by the DATABASE via two partial unique
  indexes created with raw DDL (portable SQLite + PostgreSQL):

    uq_eab_employee_current ON (employee_id) WHERE effective_to IS NULL
    uq_eab_agent_current    ON (agent_id)    WHERE effective_to IS NULL

Backfill
--------
Every Employee that already exists at upgrade time receives exactly one
current binding projected from its immutable hire snapshot:
``effective_from = employee.hired_at``, ``agent_id = employee.agent_id``,
``effective_to = NULL``. This matches byte-for-byte what
``promote_to_employee`` writes for every NEW hire (the W8-v2 promote seam), so
the invariant "every ACTIVE Employee has exactly one current binding" holds
immediately after upgrade. Backfill uses plain SQL executed row-by-row through
the connection (no dialect-specific id generation), keeping it portable.

Downgrade is FAIL-CLOSED: if any binding row exists, ``downgrade()`` raises.
Backfilled rows are derivable from the ``employee`` table, but rows written at
runtime (agent replacements, transfers, rehires) are irreplaceable attribution
history -- losing them silently is data loss. An operator must explicitly
purge the table before downgrading.

Fully reversible after an explicit purge: ``downgrade()`` drops the indexes and
the table with no residue.
"""

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "20260906_0002_workforce_agent_binding"
down_revision: str | None = "20260906_0001_recommendation_trust_advisory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "employee_agent_binding",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("employee_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("effective_from", sa.DateTime(), nullable=False),
        # NULL = the current binding.
        sa.Column("effective_to", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            ondelete="RESTRICT",
            name="fk_eab_employee_id",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agent.id"],
            # Soft Alpha-1 registry reference (Q6) -- same as Employee.agent_id.
            ondelete="NO ACTION",
            name="fk_eab_agent_id",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_from < effective_to",
            name="ck_eab_interval_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # Attribution scan + history listing (employee axis).
    op.create_index(
        "ix_eab_employee_effective", "employee_agent_binding", ["employee_id", "effective_from"]
    )
    # Attribution scan (agent axis) + agent-conflict pre-checks.
    op.create_index(
        "ix_eab_agent_effective", "employee_agent_binding", ["agent_id", "effective_from"]
    )
    op.create_index(
        "ix_employee_agent_binding_employee_id", "employee_agent_binding", ["employee_id"]
    )
    op.create_index("ix_employee_agent_binding_agent_id", "employee_agent_binding", ["agent_id"])
    # Current 1:1 both ways -- raw DDL, portable across SQLite and PostgreSQL.
    op.execute(
        "CREATE UNIQUE INDEX uq_eab_employee_current "
        "ON employee_agent_binding(employee_id) WHERE effective_to IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_eab_agent_current "
        "ON employee_agent_binding(agent_id) WHERE effective_to IS NULL"
    )

    # Backfill: every pre-existing Employee gets exactly one current binding
    # projected from its immutable hire snapshot. Row-by-row through the
    # connection so id generation stays dialect-agnostic.
    conn = op.get_bind()
    employees = conn.execute(sa.text("SELECT id, agent_id, hired_at FROM employee")).fetchall()
    for emp_id, agent_id, hired_at in employees:
        conn.execute(
            sa.text(
                "INSERT INTO employee_agent_binding "
                "(id, employee_id, agent_id, effective_from, effective_to, created_at) "
                "VALUES (:id, :employee_id, :agent_id, :effective_from, NULL, :created_at)"
            ),
            {
                "id": f"eab_{uuid4().hex[:12]}",
                "employee_id": emp_id,
                "agent_id": agent_id,
                "effective_from": hired_at,
                "created_at": hired_at,
            },
        )


def downgrade() -> None:
    conn = op.get_bind()
    row_count = conn.execute(sa.text("SELECT COUNT(*) FROM employee_agent_binding")).scalar_one()
    if row_count:
        raise RuntimeError(
            "employee_agent_binding holds "
            f"{row_count} row(s); attribution history is not recoverable. "
            "Purge the table explicitly before downgrading."
        )
    op.execute("DROP INDEX IF EXISTS uq_eab_agent_current")
    op.execute("DROP INDEX IF EXISTS uq_eab_employee_current")
    op.drop_index("ix_employee_agent_binding_agent_id", table_name="employee_agent_binding")
    op.drop_index("ix_employee_agent_binding_employee_id", table_name="employee_agent_binding")
    op.drop_index("ix_eab_agent_effective", table_name="employee_agent_binding")
    op.drop_index("ix_eab_employee_effective", table_name="employee_agent_binding")
    op.drop_table("employee_agent_binding")
