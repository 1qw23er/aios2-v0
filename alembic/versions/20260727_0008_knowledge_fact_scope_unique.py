"""Scope-aware knowledge_fact version uniqueness (fixes #53).

The global ``UNIQUE(series_id, version)`` constraint collides with the per-scope
version numbering in ``KnowledgeService.next_version`` / ``_approve``: a
company-wide fact (``project_id IS NULL``) and a project-scoped fact
(``project_id = '<pid>'``) of the SAME series can both legitimately be version 1,
which raised ``IntegrityError`` on insert -- that is the #53 bug.

This migration establishes two cooperating DB-level guards so that the intended
cross-scope coexistence is allowed while each scope's ``(series, version)``
identity stays unique:

1. ``uq_knowledge_fact_series_version`` is widened to
   ``UNIQUE(series_id, version, project_id)`` -- enforces uniqueness within a
   non-NULL project scope.
2. ``uq_knowledge_fact_company_version`` is added as a *partial* unique index
   ``ON knowledge_fact(series_id, version) WHERE project_id IS NULL`` -- enforces
   uniqueness for the company scope, where a plain 3-column constraint would fail
   because SQLite treats every NULL as distinct in a UNIQUE index.

The existing partial index ``uq_knowledge_fact_approved_head`` is *not* an
equivalent guard: it only protects the single current ``APPROVED`` head, not the
``(series, version)`` identity across ``SUPERSEDED`` / ``INACTIVE`` history.

The ``knowledge_fact`` triggers must be dropped and recreated around the table
rebuild (``op.batch_alter_table`` drops a table's triggers when it recreates the
table). The raw partial indexes are also re-asserted defensively after the
rebuild: ``batch_alter_table`` does not carry over raw ``op.execute`` indexes, and
DROP IF EXISTS + CREATE makes the outcome idempotent.

Chains off 20260722_0007 (current head).

Downgrade is **fail-closed**: migration 0008 legitimately permits the same
``(series, version)`` to exist once per distinct scope -- a company v1, a project A
v1, and a project B v1 of the SAME series can all coexist. Reverting to the old
global ``UNIQUE(series_id, version)`` would then violate that index and silently
destroy data if forced. Instead, ``downgrade()`` first checks for any ``(series_id,
version)`` pair shared by more than one row (which, under the new per-scope
uniqueness, is exactly the cross-scope condition the old global index cannot
represent); if found, it aborts with a stable, intentional error and leaves the
schema (and all rows, triggers, indexes) on revision 20260727_0008. A lossless
downgrade is only possible when every ``(series_id, version)`` pair is unique.
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "20260727_0008"
down_revision: str | None = "20260722_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Latest knowledge_fact trigger bodies (authored in 20260720_0005, unchanged since).
_FACT_INSERT_TRIGGER = """
CREATE TRIGGER knowledge_fact_validate_insert
BEFORE INSERT ON knowledge_fact
BEGIN
    SELECT CASE WHEN NEW.status <> 'APPROVED'
        THEN RAISE(ABORT, 'knowledge fact must start approved') END;
    SELECT CASE WHEN TRIM(NEW.series_id) = '' OR TRIM(NEW.statement) = ''
        THEN RAISE(ABORT, 'knowledge fact fields must be non-empty') END;
    SELECT CASE WHEN NEW.source_project_id IS NULL
        THEN RAISE(ABORT, 'knowledge fact requires a source campaign') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_candidate c
        JOIN artifact a ON a.id = c.artifact_id
        JOIN knowledge_review_decision r ON r.candidate_id = c.id
        WHERE c.id = NEW.source_candidate_id
          AND c.status = 'APPROVED'
          AND c.source_project_id IS NEW.source_project_id
          AND c.artifact_id = NEW.source_artifact_id
          AND c.project_id IS NEW.project_id
          AND c.statement = NEW.statement
          AND a.review_status = 'APPROVED'
          AND a.project_id IS NEW.source_project_id
          AND r.id = NEW.review_decision_id AND r.decision = 'APPROVE'
    ) THEN RAISE(ABORT, 'knowledge fact provenance is invalid') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_fact f
        WHERE f.series_id = NEW.series_id AND f.project_id IS NEW.project_id
    ) AND (NEW.version <> 1 OR NEW.supersedes_fact_id IS NOT NULL)
        THEN RAISE(ABORT, 'first knowledge fact must be version 1') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM knowledge_fact f
        WHERE f.series_id = NEW.series_id AND f.project_id IS NEW.project_id
    ) AND NEW.supersedes_fact_id IS NULL
        THEN RAISE(ABORT, 'existing knowledge series requires a predecessor') END;
    SELECT CASE WHEN NEW.supersedes_fact_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM knowledge_fact p
        WHERE p.id = NEW.supersedes_fact_id AND p.series_id = NEW.series_id
          AND p.project_id IS NEW.project_id AND p.status = 'SUPERSEDED'
          AND NEW.version > p.version
    ) THEN RAISE(ABORT, 'knowledge predecessor is invalid') END;
    SELECT CASE WHEN NEW.supersedes_fact_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM knowledge_fact h
        WHERE h.series_id = NEW.series_id AND h.project_id IS NEW.project_id
          AND h.status = 'APPROVED'
    ) THEN RAISE(ABORT, 'knowledge predecessor was not the current approved head') END;
END
"""

_FACT_UPDATE_TRIGGER = """
CREATE TRIGGER knowledge_fact_validate_update
BEFORE UPDATE ON knowledge_fact
BEGIN
    SELECT CASE WHEN OLD.series_id <> NEW.series_id OR OLD.version <> NEW.version
        OR OLD.project_id IS NOT NEW.project_id
        OR OLD.source_project_id IS NOT NEW.source_project_id
        OR OLD.statement <> NEW.statement
        OR OLD.source_candidate_id <> NEW.source_candidate_id
        OR OLD.source_artifact_id <> NEW.source_artifact_id
        OR OLD.review_decision_id <> NEW.review_decision_id
        OR OLD.supersedes_fact_id IS NOT NEW.supersedes_fact_id
        OR OLD.created_at <> NEW.created_at
        THEN RAISE(ABORT, 'knowledge fact identity is immutable') END;
    SELECT CASE WHEN NOT (
        OLD.tags IS NEW.tags
        OR (json_array_length(OLD.tags) = 1
            AND json_extract(OLD.tags, '$[0]') = '__legacy_unclassified__')
    ) THEN RAISE(ABORT,
        'knowledge fact tags are immutable except sentinel classification') END;
    SELECT CASE WHEN OLD.status <> 'APPROVED'
        OR (NEW.status NOT IN ('SUPERSEDED', 'INACTIVE')
            AND NOT (json_array_length(OLD.tags) = 1
                AND json_extract(OLD.tags, '$[0]') = '__legacy_unclassified__'))
        THEN RAISE(ABORT, 'invalid knowledge fact lifecycle') END;
END
"""


def _drop_fact_triggers() -> None:
    op.execute("DROP TRIGGER IF EXISTS knowledge_fact_validate_insert")
    op.execute("DROP TRIGGER IF EXISTS knowledge_fact_validate_update")


def _recreate_fact_triggers() -> None:
    op.execute(_FACT_INSERT_TRIGGER)
    op.execute(_FACT_UPDATE_TRIGGER)


def _reassert_partial_indexes() -> None:
    # batch_alter_table does not carry over raw op.execute indexes; re-assert both.
    # DROP IF EXISTS is idempotent regardless of whether the rebuild preserved them.
    op.execute("DROP INDEX IF EXISTS uq_knowledge_fact_approved_head")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_knowledge_fact_approved_head
        ON knowledge_fact(series_id, COALESCE(project_id, ''))
        WHERE status = 'APPROVED'
        """
    )
    op.execute("DROP INDEX IF EXISTS uq_knowledge_fact_company_version")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_knowledge_fact_company_version
        ON knowledge_fact(series_id, version)
        WHERE project_id IS NULL
        """
    )


def upgrade() -> None:
    _drop_fact_triggers()
    with op.batch_alter_table("knowledge_fact") as batch:
        batch.drop_constraint("uq_knowledge_fact_series_version", type_="unique")
        batch.create_unique_constraint(
            "uq_knowledge_fact_series_version",
            ["series_id", "version", "project_id"],
        )
    _reassert_partial_indexes()
    _recreate_fact_triggers()


def _global_series_version_conflict_exists() -> bool:
    """True if any (series_id, version) pair is shared by MORE THAN ONE row.

    Migration 0008 legitimately widens uniqueness to a per-scope identity, so
    the same (series, version) MAY legitimately appear once per distinct scope
    (company + project A + project B of the SAME series all at v1). But the old
    global ``UNIQUE(series_id, version)`` (revision 0007 and earlier) forbids
    ANY two rows sharing a (series, version) pair, regardless of scope.

    Because the new 3-column constraint already blocks duplicate
    (series, version, project_id) within a single scope, any (series, version)
    that appears in more than one row necessarily spans two distinct scopes --
    i.e. it cannot be represented by the old global index. Detecting duplicate
    (series, version) pairs therefore catches EVERY fail-closed case:
    company x project, project A x project B, and company x project A x project B.
    SQLite has no boolean type, so we detect via COUNT.
    """
    result = op.get_bind().execute(
        text(
            """
            SELECT COUNT(*) AS n
            FROM (
                SELECT series_id, version
                FROM knowledge_fact
                GROUP BY series_id, version
                HAVING COUNT(*) > 1
            )
            """
        )
    )
    return (result.fetchone()[0] or 0) > 0


def downgrade() -> None:
    # Fail-closed: downgrading to the global UNIQUE(series, version) would
    # corrupt data whenever two rows share a (series, version) pair across
    # scopes -- e.g. a company v1 and a project v1 of the same series, OR two
    # different projects each with v1 of the same series. Abort with a stable,
    # intentional error BEFORE touching any DDL, and leave the schema on 0008.
    # (A bare SELECT RAISE() is rejected by SQLite outside a trigger program, so
    # we raise from Python instead.)
    if _global_series_version_conflict_exists():
        raise RuntimeError(
            "cannot downgrade migration 20260727_0008: KnowledgeFacts share a "
            "(series_id, version) pair across distinct scopes (company and/or "
            "multiple projects), which the old global UNIQUE(series_id, version) "
            "cannot represent; downgrading would silently destroy data. Back up "
            "and re-scope the conflicting facts (e.g. fold the company series "
            "into a dedicated project) before downgrading."
        )
    _drop_fact_triggers()
    with op.batch_alter_table("knowledge_fact") as batch:
        batch.drop_constraint("uq_knowledge_fact_series_version", type_="unique")
        batch.create_unique_constraint(
            "uq_knowledge_fact_series_version",
            ["series_id", "version"],
        )
    # Drop the company-scope partial index added by 0008; keep the head partial
    # index idempotently re-asserted.
    op.execute("DROP INDEX IF EXISTS uq_knowledge_fact_company_version")
    op.execute("DROP INDEX IF EXISTS uq_knowledge_fact_approved_head")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_knowledge_fact_approved_head
        ON knowledge_fact(series_id, COALESCE(project_id, ''))
        WHERE status = 'APPROVED'
        """
    )
    _recreate_fact_triggers()
