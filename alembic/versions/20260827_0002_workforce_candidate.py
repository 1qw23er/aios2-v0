"""W2 Candidate Discovery -- additive ``candidate`` table (W1 hardening head on top).

W2 introduces the Candidate Pool entity. This migration is *purely additive*: it
only creates the new ``candidate`` table (plus its indexes) and does NOT touch any
existing W1 table. It sits on top of ``20260827_0001_workforce_capreq_hardening``
and becomes the new single head.

Candidate = Agent x Job x Evaluation Context (V1.1, §2). Referential policy:

* ``agent_id`` FK -> ``agent.id`` with ``ondelete="NO ACTION"``. The Alpha-1
  Agent Registry is the single source of truth; we store only the id and never
  copy registry data. A registry deletion must NOT silently wipe discovery history.
* ``job_id`` / ``job_version_id`` FKs -> ``job.id`` / ``job_version.id`` with
  ``ondelete="CASCADE"``. A Job owns its candidate pool; deleting the Job removes
  its candidates (traceability lives *within* the Job lifecycle).
* ``(agent_id, job_id, job_version_id)`` UNIQUE -- re-running discovery for the
  same job version is idempotent (no duplicate candidate rows).

``evaluation_context`` is a JSON bag reserved for W3 Evaluation; W2 leaves it
empty. ``status`` carries the minimal W2 lifecycle (``pooled`` / ``rejected``);
the W3 states are reserved and not enterable in W2.

Fully reversible: downgrade drops the indexes and the table, returning to the W1
hardening head with no residue.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260827_0002_workforce_candidate"
down_revision: str | None = "20260827_0001_workforce_capreq_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "candidate"


def _create_indexes() -> None:
    op.create_index("ix_candidate_agent_id", TABLE, ["agent_id"])
    op.create_index("ix_candidate_job_id", TABLE, ["job_id"])
    op.create_index("ix_candidate_job_version_id", TABLE, ["job_version_id"])
    op.create_index("ix_candidate_status", TABLE, ["status"])


def _drop_indexes() -> None:
    op.drop_index("ix_candidate_status", table_name=TABLE)
    op.drop_index("ix_candidate_job_version_id", table_name=TABLE)
    op.drop_index("ix_candidate_job_id", table_name=TABLE)
    op.drop_index("ix_candidate_agent_id", table_name=TABLE)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("job_version_id", sa.String(), nullable=False),
        sa.Column("evaluation_context", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("discovered_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agent.id"],
            ondelete="NO ACTION",
            name="fk_candidate_agent_id",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            ondelete="CASCADE",
            name="fk_candidate_job_id",
        ),
        sa.ForeignKeyConstraint(
            ["job_version_id"],
            ["job_version.id"],
            ondelete="CASCADE",
            name="fk_candidate_job_version_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_id",
            "job_id",
            "job_version_id",
            name="uq_candidate_agent_job_version",
        ),
    )
    _create_indexes()


def downgrade() -> None:
    _drop_indexes()
    op.drop_table(TABLE)
