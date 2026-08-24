#!/usr/bin/env python3
"""Pre-upgrade precheck for the series_id backfill migrations (follow-up #1).

The series_id backfill (``20260820_0001_series_id``) and its json_guard
reinforcement (``20260824_0001_series_id_json_guard``) read ``artifact.metadata``
as JSON. A row whose ``metadata`` is a plain string / empty string /
structurally invalid JSON is *safe* under the ``json_valid``-guarded UPDATE,
but only because the migration guards it. Before that guard existed (the
``<= 20260812`` window), such a row would have raised ``malformed JSON`` and
aborted the entire migration transaction.

This script is the operator-facing safety net: it scans every owner-inbox item
table for malformed ``metadata`` JSON *before* the migration runs, so a corrupt
row is surfaced explicitly instead of failing an in-flight upgrade. Tables that
do not carry a ``metadata`` column (``cs_suggestion``, ``knowledge_candidate``)
are skipped -- their ``series_id`` is derived, never parsed from JSON, so they
cannot contain malformed JSON.

Exit codes:
  0  no malformed metadata found (safe to upgrade)
  1  malformed metadata found (do NOT upgrade until remediated)
  2  usage / connection / schema error
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the script runnable both standalone (``python scripts/...``) and as an
# imported module under the ``aios`` package.
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
for _p in (str(_SCRIPT_DIR), str(_REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from aios.db import get_engine  # noqa: E402

# Owner-inbox item tables that *may* carry a ``metadata`` JSON column. Only
# ``artifact`` actually does (models.py:428). The others are listed defensively;
# the precheck skips any table without a ``metadata`` column.
INBOX_TABLES = ("artifact", "cs_suggestion", "knowledge_candidate")


def _has_column(engine, table: str, column: str) -> bool:
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    return any(row[1] == column for row in rows)


def check_metadata_json(db_url: str) -> list[str]:
    """Return human-readable violation messages; empty list means clean.

    Only tables that expose a ``metadata`` column are inspected. A row whose
    ``metadata`` is a non-null, non-JSON string is reported; ``NULL`` metadata
    is treated as valid (it is the "ungrouped" sentinel and is skipped by the
    backfill via ``WHERE series_id IS NULL``).
    """
    from sqlalchemy import text

    engine = get_engine(db_url)
    violations: list[str] = []
    for table in INBOX_TABLES:
        if not _has_column(engine, table, "metadata"):
            # No metadata column => nothing to validate; cannot hold malformed JSON.
            continue
        with engine.connect() as conn:
            # json_valid(NULL) is NULL -> NOT NULL is NULL -> row excluded: only
            # genuinely malformed, non-null metadata is counted.
            bad = conn.execute(
                text(
                    f"SELECT COUNT(*) FROM {table} "
                    f"WHERE metadata IS NOT NULL AND NOT json_valid(metadata)"
                )
            ).scalar_one()
        if bad:
            violations.append(
                f"{table}: {bad} row(s) have non-null metadata that is not valid JSON"
            )
    return violations


def run_precheck(db_url: str) -> int:
    try:
        violations = check_metadata_json(db_url)
    except Exception as exc:  # noqa: BLE001 - surface any connection/schema error
        print(f"[precheck] ERROR: {exc}", file=sys.stderr)
        return 2
    if violations:
        print("[precheck] FAIL: malformed metadata JSON detected:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        print(
            "[precheck] Do NOT run the series_id migrations until these rows are "
            "remediated (set metadata to valid JSON or NULL).",
            file=sys.stderr,
        )
        return 1
    print("[precheck] OK: no malformed metadata JSON; safe to upgrade.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv
    db_url = args[1] if len(args) > 1 else os.getenv(
        "AIOS_DATABASE_URL", "sqlite:///data/aios.db"
    )
    return run_precheck(db_url)


if __name__ == "__main__":
    raise SystemExit(main())
