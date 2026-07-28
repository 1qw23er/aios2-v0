"""Work-log & knowledge-capture system: agent platform identity + artifact
idempotency (#88).

Adds the three columns required by the work-log MVP
(``docs/issue-88-implementation-plan.md`` §3):

1. ``agent.platform`` (VARCHAR, nullable, indexed) -- which external platform
   the AI employee lives on (chatgpt/codex/workbuddy/hermes/coze/custom).
2. ``agent.external_ref`` (VARCHAR, nullable) -- opaque external identity
   reference; never interpreted by AIOS.
3. ``artifact.idempotency_key`` (VARCHAR, nullable) -- storage key of the
   single work-log idempotency contract
   (``work_log:{project_id}:{sha256(client_key)[:32]}``), guarded by the
   partial unique index ``uq_artifact_idempotency`` (WHERE idempotency_key IS
   NOT NULL). A plain UNIQUE would also work for NULLs in SQLite, but the
   partial index keeps the guard explicit and matches the existing
   ``uq_knowledge_fact_*`` partial-index convention.

DDL strategy (per plan §3.2):

- ``agent`` is altered via ``op.batch_alter_table`` -- the table has no
  triggers, so a batch rebuild is safe.
- ``artifact`` is altered via a **raw** ``ALTER TABLE artifact ADD COLUMN``.
  It must NOT be batch-rebuilt: the external trigger
  ``knowledge_candidate_validate_insert`` references the table with a literal
  ``main.artifact`` name, and a batch rebuild (rename + copy + drop) would
  break or silently drop that trigger.

Chains off 20260727_0008 (current head).

Downgrade is **fail-closed** (same pattern as 0008): if ANY row has
``agent.platform IS NOT NULL`` / ``agent.external_ref IS NOT NULL`` /
``artifact.idempotency_key IS NOT NULL``, the downgrade aborts with a stable
``RuntimeError`` BEFORE touching any DDL (SQLite rejects a bare
``SELECT RAISE()`` outside a trigger program, so we raise from Python) and the
schema, rows, indexes and revision all remain on 20260728_0009. A lossless
downgrade is only promised for empty 0009 data.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "20260728_0009"
down_revision: str | None = "20260727_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # agent: no triggers reference this table, batch rebuild is safe.
    with op.batch_alter_table("agent") as batch:
        batch.add_column(sa.Column("platform", sa.String(), nullable=True))
        batch.add_column(sa.Column("external_ref", sa.String(), nullable=True))
        batch.create_index("ix_agent_platform", ["platform"])
    # artifact: raw ALTER only -- the knowledge_candidate_validate_insert
    # trigger references ``main.artifact`` literally; never batch-rebuild.
    op.execute("ALTER TABLE artifact ADD COLUMN idempotency_key VARCHAR")
    op.execute("DROP INDEX IF EXISTS uq_artifact_idempotency")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_artifact_idempotency
        ON artifact(idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """
    )


def _work_log_data_exists() -> bool:
    """True if any 0009-introduced column holds data (downgrade would lose it)."""
    bind = op.get_bind()
    agent_rows = bind.execute(
        text(
            """
            SELECT COUNT(*) FROM agent
            WHERE platform IS NOT NULL OR external_ref IS NOT NULL
            """
        )
    ).fetchone()[0]
    artifact_rows = bind.execute(
        text("SELECT COUNT(*) FROM artifact WHERE idempotency_key IS NOT NULL")
    ).fetchone()[0]
    return (agent_rows or 0) > 0 or (artifact_rows or 0) > 0


def downgrade() -> None:
    # Fail-closed: dropping populated 0009 columns would silently destroy
    # platform identity / idempotency history. Abort BEFORE any DDL and leave
    # schema, rows, indexes and revision on 20260728_0009.
    if _work_log_data_exists():
        raise RuntimeError(
            "cannot downgrade migration 20260728_0009: agent.platform / "
            "agent.external_ref / artifact.idempotency_key hold data that the "
            "previous schema cannot represent; downgrading would silently "
            "destroy it. Clear (NULL) those columns explicitly after backing "
            "them up before downgrading."
        )
    op.execute("DROP INDEX IF EXISTS uq_artifact_idempotency")
    # SQLite >= 3.35 supports DROP COLUMN via raw ALTER; artifact must never be
    # batch-rebuilt (literal main.artifact reference in the external trigger).
    op.execute("ALTER TABLE artifact DROP COLUMN idempotency_key")
    with op.batch_alter_table("agent") as batch:
        batch.drop_index("ix_agent_platform")
        batch.drop_column("external_ref")
        batch.drop_column("platform")
