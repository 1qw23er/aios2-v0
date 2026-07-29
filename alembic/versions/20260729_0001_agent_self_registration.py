"""Agent self-registration support for the unified Agent platform (#99/#101).

Adds the minimal surface required by the V4 self-registration contract
(``docs/issue-99-v4-plan.md`` §0.8 / §7):

1. ``agent.bootstrap_token_ref`` (VARCHAR, nullable, **NO index**) -- the opaque
   handle that records which scoped bootstrap token claimed this agent row. Its
   presence (a row with ``bootstrap_token_ref = :jti``) is the DB-side proof that
   a token has been *consumed*; the claim is committed atomically with the agent
   row so there is no distributed state / compensation. No second index is added
   because ``(platform, external_ref)`` uniqueness is already guaranteed by the
   partial unique index below.
2. A **partial unique index** ``uq_agent_platform_external_ref`` on
   ``agent(platform, external_ref) WHERE external_ref IS NOT NULL`` -- the
   hard DB guarantee that a self-registered identity tuple is globally unique, so
   concurrent same-tuple bootstrap claims produce exactly one agent (the loser
   hits the index and is rejected with 401, zero side effects).

This is the single controlled migration for V4: it introduces **zero new
tables** and advances the Alembic head by exactly one (from 20260728_0009).

DDL strategy:

- ``agent`` is altered via ``op.batch_alter_table`` -- the table has no
  triggers, so a batch rebuild is safe (and preserves the 0009
  ``ix_agent_platform`` index).
- The partial unique index is created with **raw SQL** after the batch block so
  it is not entangled with the rebuild.

Downgrade is fail-closed (same pattern as 20260728_0009):
``bootstrap_token_ref`` is the DB-side single-use consumption record for scoped
bootstrap tokens (plan §3.2). Dropping it would erase which tokens were already
consumed, so a subsequent re-upgrade could let a previously-consumed (replayed)
token claim a *new* agent row -- violating strict single-use. Therefore
``downgrade()`` aborts with a stable ``RuntimeError`` BEFORE touching any DDL if
ANY agent row has a populated ``bootstrap_token_ref``; the schema, rows, index
and revision are all left on 20260729_0001. A lossless downgrade (drop the
partial index + column) is only promised for an empty V4 registration state (no
consumed tokens), matching the empty-data reversible contract of the other
controlled migrations.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "20260729_0001"
down_revision: str | None = "20260728_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # agent: no triggers reference this table, batch rebuild is safe and
    # preserves ix_agent_platform from 0009.
    with op.batch_alter_table("agent") as batch:
        batch.add_column(
            sa.Column("bootstrap_token_ref", sa.String(), nullable=True)
        )
    # Partial unique index: guarantees (platform, external_ref) uniqueness for
    # rows that actually carry an external_ref. NULL external_ref rows are not
    # constrained (pre-existing / owner-seeded agents are unaffected).
    op.execute(
        """
        CREATE UNIQUE INDEX uq_agent_platform_external_ref
        ON agent(platform, external_ref)
        WHERE external_ref IS NOT NULL
        """
    )


def _v4_registration_data_exists() -> bool:
    """True if any agent carries a populated ``bootstrap_token_ref``.

    That column is the DB-side single-use consumption record for scoped
    bootstrap tokens (plan §3.2). Its presence on any row means a token was
    already consumed; dropping the column would erase that record and let a
    previously-consumed token be replayed after a re-upgrade. So it is the
    precise trigger for a fail-closed downgrade.
    """
    bind = op.get_bind()
    consumed = bind.execute(
        text("SELECT COUNT(*) FROM agent WHERE bootstrap_token_ref IS NOT NULL")
    ).fetchone()[0]
    return (consumed or 0) > 0


def downgrade() -> None:
    # Fail-closed: dropping a populated bootstrap_token_ref would silently
    # destroy the single-use consumption record, letting a consumed token be
    # replayed after a re-upgrade (plan §3.2). Abort BEFORE any DDL and leave
    # schema, rows, index and revision on 20260729_0001.
    if _v4_registration_data_exists():
        raise RuntimeError(
            "cannot downgrade migration 20260729_0001: agent.bootstrap_token_ref "
            "holds consumed-token data that the previous schema cannot represent; "
            "downgrading would silently destroy the strict single-use consumption "
            "record and permit token replay after re-upgrade. Clear (NULL) "
            "bootstrap_token_ref on every agent (after backing them up) before "
            "downgrading."
        )
    op.execute("DROP INDEX IF EXISTS uq_agent_platform_external_ref")
    with op.batch_alter_table("agent") as batch:
        batch.drop_column("bootstrap_token_ref")
