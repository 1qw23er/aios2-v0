"""SalesPlaybook V0 follow-up: tamper-proof evidence-cited flag on cs_suggestion.

Adds a single Boolean column ``cs_suggestion.sales_evidence_cited`` that records
whether a suggestion was generated WITH SalesPlaybook evidence rows. The send-time
gate (CustomerService._assert_sales_evidence_still_safe, P1-2) fails CLOSED when
those rows are absent but this flag is set, so deleting the citation rows can no
longer silently skip revalidation and let a tampered suggestion through.

This migration ONLY touches ``cs_suggestion`` (an existing table) by ADDING one
column; it does not alter any SalesPlaybook table from 20260810_0001. The existing
table's other columns are unchanged, consistent with that revision's "no existing
table is touched" contract (it added brand-new tables only).

Downgrade is UNCONDITIONALLY fail-closed (one-way-door policy, P1-3): dropping a
tamper-proof safety flag is never performed by the migration. Rolling back requires
a deliberate, human, out-of-band operation.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260812_0001"
down_revision: str | None = "20260810_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cs_suggestion",
        sa.Column(
            "sales_evidence_cited",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    # Backfill: any suggestion that already has evidence rows was generated WITH
    # SalesPlaybook citation, so flag it -- otherwise the gate would miss existing
    # citations that lose their rows later.
    op.execute(
        "UPDATE cs_suggestion SET sales_evidence_cited = 1 "
        "WHERE id IN (SELECT DISTINCT suggestion_id FROM cs_suggestion_sales_evidence)"
    )


def downgrade() -> None:
    # One-way door: never drops the tamper-proof flag (P1-3 policy).
    raise RuntimeError(
        "cannot downgrade migration 20260812_0001: cs_suggestion.sales_evidence_cited "
        "is a tamper-proof safety flag; rolling this back requires a deliberate, "
        "human, out-of-band schema operation"
    )
