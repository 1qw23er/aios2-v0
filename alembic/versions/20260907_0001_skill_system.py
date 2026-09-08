"""Skill System V1: procedural knowledge ("how to do it") as a first-class domain.

Adds three brand-new tables -- ``skill_candidate``, ``skill_review_decision``,
``skill`` -- plus one column on ``task_context`` (``applicable_skills``).

Scope notes
-----------
* No existing table is modified except ``task_context``, which gains a single
  JSON column with ``server_default='[]'``. Historical rows keep their
  ``context_hash``: a replay only produces a new hash when the skill set
  actually changes, which is the intended behaviour.
* No backfill: the three new tables start empty, and ``task_context``'s
  server default means existing rows need no UPDATE.
* Skill is deliberately NOT wired into routing / execution. The only execution
  seam is ``context_service._select_skills`` -> ``TaskContext.applicable_skills``
  (see Implementation Contract §10).

Uniqueness strategy
-------------------
Two cooperating guards per identity, because SQLite treats every NULL as
distinct and a plain multi-column UNIQUE therefore cannot constrain the
company scope (``project_id IS NULL``). This is the same class of bug that
migration 20260727_0008 fixed for ``knowledge_fact`` (#53):

* ``uq_skill_candidate_identity`` (3 columns) + partial
  ``uq_skillcand_identity_company``;
* ``uq_skill_name_version`` (3 columns) + partial
  ``uq_skill_name_version_company``;

The two partial "single active head" indexes are what make concurrent approval
of the same ``(name, scope)`` fail as a DB ``IntegrityError`` -> HTTP 409
instead of silently producing two live versions.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260907_0001_skill_system"
down_revision = "20260906_0002_workforce_agent_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_candidate",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("capability_id", sa.String(), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("tool_bindings", sa.JSON(), nullable=False),
        sa.Column("execution_strategy", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("source_project_id", sa.String(), nullable=False),
        sa.Column("source_artifact_id", sa.String(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("submitted_by_kind", sa.String(), nullable=False),
        sa.Column("submitted_by_owner_id", sa.String(), nullable=True),
        sa.Column("submitted_by_agent_id", sa.String(), nullable=True),
        sa.Column("submitted_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["capability_id"], ["capability.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(["source_project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(["source_artifact_id"], ["artifact.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "name",
            "content_hash",
            "project_id",
            name="uq_skill_candidate_identity",
        ),
    )
    op.create_index("ix_skill_candidate_name", "skill_candidate", ["name"])
    op.create_index("ix_skill_candidate_capability_id", "skill_candidate", ["capability_id"])
    op.create_index("ix_skill_candidate_project_id", "skill_candidate", ["project_id"])
    op.create_index(
        "ix_skill_candidate_source_project_id", "skill_candidate", ["source_project_id"]
    )
    op.create_index(
        "ix_skill_candidate_source_artifact_id", "skill_candidate", ["source_artifact_id"]
    )
    op.create_index("ix_skill_candidate_content_hash", "skill_candidate", ["content_hash"])
    op.create_index("ix_skill_candidate_status", "skill_candidate", ["status"])
    # Company-scope identity guard (SQLite/PG both treat NULLs as distinct).
    op.create_index(
        "uq_skillcand_identity_company",
        "skill_candidate",
        ["name", "content_hash"],
        unique=True,
        sqlite_where=sa.text("project_id IS NULL"),
        postgresql_where=sa.text("project_id IS NULL"),
    )

    op.create_table(
        "skill_review_decision",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("candidate_id", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("reviewer_kind", sa.String(), nullable=False),
        sa.Column("reviewer_owner_id", sa.String(), nullable=True),
        sa.Column("reviewer_agent_id", sa.String(), nullable=True),
        sa.Column("reviewer", sa.String(), nullable=False),
        sa.Column("rationale", sa.String(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_id"], ["skill_candidate.id"]),
        sa.PrimaryKeyConstraint("id"),
        # One decision per candidate, enforced by the database.
        sa.UniqueConstraint("candidate_id", name="uq_skill_review_candidate"),
    )
    op.create_index(
        "ix_skill_review_decision_candidate_id", "skill_review_decision", ["candidate_id"]
    )

    op.create_table(
        "skill",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("capability_id", sa.String(), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("tool_bindings", sa.JSON(), nullable=False),
        sa.Column("execution_strategy", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("source_project_id", sa.String(), nullable=False),
        sa.Column("source_artifact_id", sa.String(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("source_candidate_id", sa.String(), nullable=False),
        sa.Column("review_decision_id", sa.String(), nullable=False),
        sa.Column("supersedes_skill_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["capability_id"], ["capability.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(["source_project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(["source_artifact_id"], ["artifact.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_candidate_id"], ["skill_candidate.id"]),
        sa.ForeignKeyConstraint(["review_decision_id"], ["skill_review_decision.id"]),
        sa.ForeignKeyConstraint(["supersedes_skill_id"], ["skill.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "version", "project_id", name="uq_skill_name_version"),
        sa.UniqueConstraint("source_candidate_id", name="uq_skill_source_candidate"),
        sa.UniqueConstraint("review_decision_id", name="uq_skill_review_decision"),
        sa.UniqueConstraint("supersedes_skill_id", name="uq_skill_supersedes"),
    )
    op.create_index("ix_skill_name", "skill", ["name"])
    op.create_index("ix_skill_capability_id", "skill", ["capability_id"])
    op.create_index("ix_skill_project_id", "skill", ["project_id"])
    op.create_index("ix_skill_source_project_id", "skill", ["source_project_id"])
    op.create_index("ix_skill_source_artifact_id", "skill", ["source_artifact_id"])
    op.create_index("ix_skill_content_hash", "skill", ["content_hash"])
    op.create_index("ix_skill_status", "skill", ["status"])
    op.create_index("ix_skill_source_candidate_id", "skill", ["source_candidate_id"])
    op.create_index("ix_skill_review_decision_id", "skill", ["review_decision_id"])
    op.create_index("ix_skill_supersedes_skill_id", "skill", ["supersedes_skill_id"])
    # --- partial uniqueness guards ------------------------------------------
    # 1) company-scope (name, version) identity;
    # 2) at most one APPROVED head per (name, project);
    # 3) at most one APPROVED head per name at company scope.
    op.create_index(
        "uq_skill_name_version_company",
        "skill",
        ["name", "version"],
        unique=True,
        sqlite_where=sa.text("project_id IS NULL"),
        postgresql_where=sa.text("project_id IS NULL"),
    )
    op.create_index(
        "uq_skill_active_head_project",
        "skill",
        ["name", "project_id"],
        unique=True,
        sqlite_where=sa.text("status = 'approved' AND project_id IS NOT NULL"),
        postgresql_where=sa.text("status = 'approved' AND project_id IS NOT NULL"),
    )
    op.create_index(
        "uq_skill_active_head_company",
        "skill",
        ["name"],
        unique=True,
        sqlite_where=sa.text("status = 'approved' AND project_id IS NULL"),
        postgresql_where=sa.text("status = 'approved' AND project_id IS NULL"),
    )

    with op.batch_alter_table("task_context") as batch:
        batch.add_column(
            sa.Column("applicable_skills", sa.JSON(), server_default="[]", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("task_context") as batch:
        batch.drop_column("applicable_skills")

    op.drop_index("uq_skill_active_head_company", table_name="skill")
    op.drop_index("uq_skill_active_head_project", table_name="skill")
    op.drop_index("uq_skill_name_version_company", table_name="skill")
    op.drop_table("skill")
    op.drop_table("skill_review_decision")
    op.drop_index("uq_skillcand_identity_company", table_name="skill_candidate")
    op.drop_table("skill_candidate")
