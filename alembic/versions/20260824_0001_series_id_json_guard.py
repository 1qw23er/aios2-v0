"""Harden artifact.series_id backfill against malformed metadata JSON (follow-up #5).

The base series_id migration (``20260820_0001_series_id``) backfills
``artifact.series_id`` with::

    UPDATE artifact
    SET series_id = json_extract(metadata, '$.series_id')
    WHERE series_id IS NULL
    AND json_extract(metadata, '$.series_id') IS NOT NULL

In SQLite, ``json_extract`` on a column that does NOT contain valid JSON raises
``SQLITE_ERROR: malformed JSON``. Rows whose ``metadata`` is a plain string, an
empty string, ``NULL``, or otherwise non-JSON would abort the whole statement
(and therefore the whole migration transaction) instead of being safely left as
ungrouped (NULL). That breaks the fail-closed contract: a single corrupt row
must never block the backfill of every other row.

This reinforcement migration re-runs the artifact backfill with a
``json_valid(metadata)`` guard so malformed rows are skipped and kept NULL::

    UPDATE artifact
    SET series_id = json_extract(metadata, '$.series_id')
    WHERE series_id IS NULL
    AND json_valid(metadata)
    AND json_extract(metadata, '$.series_id') IS NOT NULL

Because the base migration's backfill is idempotent (NULL-guarded), this is a
pure *defensive* re-run: rows that already got a series_id are skipped by the
``WHERE series_id IS NULL`` clause, and rows that were previously skipped due to
malformed JSON now get a chance to be populated only if they are actually valid.

It is also a no-op safety net for the common case: if the base migration already
ran cleanly on valid data, this UPDATE touches zero rows.

Design note (follow-up #5 / DSH audit):
* Only ``artifact`` needs the guard. ``cs_suggestion.series_id`` is derived from
  ``conversation_id`` (never JSON) and ``knowledge_candidate.series_id`` is
  inherited from ``artifact.series_id`` (never parsed from JSON), so neither can
  raise ``malformed JSON``.
* No column/index changes -- this migration only repairs data, so downgrade is a
  deliberate no-op (we must not silently *drop* backfilled series_ids that a
  re-run of the base migration would re-derive; the data is now authoritative).

Boundary: this migration sits ON TOP of ``20260820_0001_series_id`` and becomes
the new single alembic head. It must never alter or replace that migration's
file (published migrations are immutable).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260824_0001_series_id_json_guard"
down_revision: str | None = "20260820_0001_series_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Re-run the artifact backfill, but only on rows whose metadata is valid
    # JSON. Malformed rows are skipped and kept NULL (fail-closed). The
    # NULL-guard makes this idempotent with the base migration.
    op.execute(
        "UPDATE artifact "
        "SET series_id = json_extract(metadata, '$.series_id') "
        "WHERE series_id IS NULL "
        "AND json_valid(metadata) "
        "AND json_extract(metadata, '$.series_id') IS NOT NULL"
    )


def downgrade() -> None:
    # No-op: the series_id values backfilled here are now authoritative data.
    # Dropping them would lose information that a re-run of the base migration
    # cannot guarantee to re-derive (e.g. rows originally skipped as malformed
    # but valid on re-inspection). Reverting to the base migration's state is
    # achieved by the base migration's downgrade, not here.
    pass
