"""Agent secret-store persistence for V4 self-update credentials (#103).

Adds the minimal ``agent_secret`` table (single PK on ``agent_id`` + exactly
one required UNIQUE lookup index on ``token_tag``). Static storage holds ONLY
KEK-derived HMAC tags (``token_tag``, ``row_mac``) -- never the plaintext
bearer and never any reversible ciphertext (issue #103 §4.2).

DDL strategy: a single ``CREATE TABLE`` + ``CREATE UNIQUE INDEX``. The table is
brand new and has no triggers, so there is no batch-rebuild concern (the
``artifact``-table trigger caveat from 0009 does not apply here).

Downgrade is fail-closed and STRICTER than 20260729_0001: ANY row present
(active OR already revoked) represents real stored credential material, so
dropping the table would silently destroy it. We abort BEFORE touching any DDL
if the table holds even a single row. A lossless downgrade is only promised for
a completely empty ``agent_secret`` (matching the empty-data reversible contract
of the other controlled migrations).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "20260730_0001"
down_revision: str | None = "20260729_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_secret",
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("token_tag", sa.LargeBinary(), nullable=False),
        sa.Column("row_mac", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"]),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    # The only index: ``token_tag`` is the access-path lookup key (HMAC tag is
    # one-way, so we MUST index it to resolve a bearer). No second index.
    op.create_index(
        "uq_agent_secret_token_tag", "agent_secret", ["token_tag"], unique=True
    )


def _agent_secret_has_rows() -> bool:
    """True if ``agent_secret`` holds any row (active OR revoked).

    Any stored row is real credential material; dropping the table would
    silently destroy it, so its presence is the precise trigger for a
    fail-closed downgrade (issue #103 §4.4 / §5).
    """
    bind = op.get_bind()
    count = bind.execute(text("SELECT COUNT(*) FROM agent_secret")).fetchone()[0]
    return (count or 0) > 0


def downgrade() -> None:
    # Fail-closed: ANY row (active or revoked) is stored credential material the
    # previous schema cannot represent. Abort BEFORE any DDL and leave schema,
    # rows, index and revision on 20260730_0001.
    if _agent_secret_has_rows():
        raise RuntimeError(
            "cannot downgrade migration 20260730_0001: agent_secret holds stored "
            "credential material (active or revoked) that the previous schema "
            "cannot represent; downgrading would silently destroy it. Clear "
            "(revoke / delete) every agent_secret row (after backing them up) "
            "before downgrading."
        )
    op.drop_index("uq_agent_secret_token_tag", table_name="agent_secret")
    op.drop_table("agent_secret")
