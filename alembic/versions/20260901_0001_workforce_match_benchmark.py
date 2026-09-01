"""W3-B Match / Ranking & Benchmark -- additive 4 tables + 1 column.

W3-B adds the Benchmark evidence mechanism and the Match scoring layer on top of
W3-A (frozen at main@c367de9). This migration is *purely additive*:

* creates ``benchmark`` / ``benchmark_version`` / ``benchmark_result`` / ``match``
* adds a nullable ``job_version.benchmark_version_id`` FK (CASCADE) to the existing
  ``job_version`` table, via ``batch_alter_table`` so the FK constraint is actually
  created on SQLite (which cannot ALTER a table to ADD a FOREIGN KEY).

No existing W1/W2/W3-A table is modified. Fully reversible: downgrade drops the
indexes + tables and removes the column, returning to the W3-A head with no residue.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260901_0001_workforce_match_benchmark"
down_revision: str | None = "20260827_0002_workforce_candidate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "benchmark",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_benchmark_name"),
    )

    op.create_table(
        "benchmark_version",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("benchmark_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["benchmark_id"],
            ["benchmark.id"],
            ondelete="CASCADE",
            name="fk_benchmark_version_benchmark_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "benchmark_id", "version", name="uq_benchmark_version"
        ),
    )
    op.create_index(
        "ix_benchmark_version_benchmark_id",
        "benchmark_version",
        ["benchmark_id"],
    )

    op.create_table(
        "benchmark_result",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("candidate_id", sa.String(), nullable=False),
        sa.Column("benchmark_version_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("passed_cases", sa.Integer(), nullable=True),
        sa.Column("total_cases", sa.Integer(), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("input_hash", sa.String(), nullable=False),
        sa.Column("output_ref", sa.String(), nullable=True),
        sa.Column("agent_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("environment", sa.String(), nullable=False),
        sa.Column("reproducibility_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("evaluator", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidate.id"],
            ondelete="CASCADE",
            name="fk_benchmark_result_candidate_id",
        ),
        sa.ForeignKeyConstraint(
            ["benchmark_version_id"],
            ["benchmark_version.id"],
            ondelete="RESTRICT",
            name="fk_benchmark_result_benchmark_version_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_id",
            "benchmark_version_id",
            "run_id",
            name="uq_benchmark_result_run",
        ),
    )
    op.create_index(
        "ix_benchmark_result_candidate_id",
        "benchmark_result",
        ["candidate_id"],
    )
    op.create_index(
        "ix_benchmark_result_benchmark_version_id",
        "benchmark_result",
        ["benchmark_version_id"],
    )

    op.create_table(
        "match",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("candidate_id", sa.String(), nullable=False),
        sa.Column("job_version_id", sa.String(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("weights_version", sa.String(), nullable=False),
        sa.Column("breakdown", sa.JSON(), nullable=False),
        sa.Column("evaluated_fields", sa.JSON(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("benchmark_version_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("match_blocked_reason", sa.String(), nullable=True),
        sa.Column("evaluator", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidate.id"],
            ondelete="CASCADE",
            name="fk_match_candidate_id",
        ),
        sa.ForeignKeyConstraint(
            ["job_version_id"],
            ["job_version.id"],
            ondelete="CASCADE",
            name="fk_match_job_version_id",
        ),
        sa.ForeignKeyConstraint(
            ["benchmark_version_id"],
            ["benchmark_version.id"],
            ondelete="SET NULL",
            name="fk_match_benchmark_version_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_id",
            "job_version_id",
            name="uq_match_candidate_job_version",
        ),
    )
    op.create_index("ix_match_candidate_id", "match", ["candidate_id"])
    op.create_index("ix_match_job_version_id", "match", ["job_version_id"])
    op.create_index(
        "ix_match_benchmark_version_id", "match", ["benchmark_version_id"]
    )

    # Add the optional binding column to the existing job_version table. Batch mode
    # is required so the FK constraint is created on SQLite (which cannot ALTER a
    # table to ADD a FOREIGN KEY).
    with op.batch_alter_table("job_version") as batch_op:
        batch_op.add_column(
            sa.Column("benchmark_version_id", sa.String(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_job_version_benchmark_version_id",
            "benchmark_version",
            ["benchmark_version_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(
            "ix_job_version_benchmark_version_id",
            ["benchmark_version_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("job_version") as batch_op:
        batch_op.drop_index("ix_job_version_benchmark_version_id")
        batch_op.drop_constraint(
            "fk_job_version_benchmark_version_id", type_="foreignkey"
        )
        batch_op.drop_column("benchmark_version_id")

    op.drop_index("ix_match_benchmark_version_id", table_name="match")
    op.drop_index("ix_match_job_version_id", table_name="match")
    op.drop_index("ix_match_candidate_id", table_name="match")
    op.drop_table("match")

    op.drop_index(
        "ix_benchmark_result_benchmark_version_id",
        table_name="benchmark_result",
    )
    op.drop_index(
        "ix_benchmark_result_candidate_id", table_name="benchmark_result"
    )
    op.drop_table("benchmark_result")

    op.drop_index(
        "ix_benchmark_version_benchmark_id", table_name="benchmark_version"
    )
    op.drop_table("benchmark_version")

    op.drop_table("benchmark")
