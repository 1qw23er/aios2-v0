"""Add Skill.required_output_contract for Plan A contract merge.

Plan A (skill adherence) lets a published Skill declare the structured output it
REQUIRES the agent to produce, as a JSON-schema fragment
(``{"type": "object", "properties": {...}, "required": [...]}``). The execution
layer merges ``Task.output_schema`` with every applicable skill's
``required_output_contract`` to (a) render the combined contract in the agent
prompt and (b) machine-check adherence of the produced artifact.

Purely additive nullable JSON column on ``skill``. ``skill`` is only referenced
by its own self-FK (``supersedes_skill_id``), so a plain ``op.add_column`` is
safe -- no ``batch_alter_table`` (which recreates the table and would trip the
FK). Existing rows carry ``required_output_contract = NULL`` -> ORM default {}.

Chains after 20260912_0002_run_model.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import JSON

from alembic import op

revision = "20260913_0001_skill_required_output_contract"
down_revision = "20260912_0002_run_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain ADD COLUMN -- non-indexed nullable JSON, no table recreate, so the
    # self-FK on skill is untouched (see 20260912_0001_agent_model for the
    # rationale behind avoiding batch mode on FK-referenced tables).
    op.add_column(
        "skill",
        sa.Column("required_output_contract", JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("skill", "required_output_contract")
