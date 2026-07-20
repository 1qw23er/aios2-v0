"""Knowledge capability tags + trusted identity (Phase A, #67).

Adds:
- ``knowledge_candidate.tags`` (JSON), ``knowledge_candidate.submitted_by_kind`` /
  ``submitted_by_owner_id`` / ``submitted_by_agent_id``
- ``knowledge_fact.tags`` (JSON)
- ``knowledge_review_decision.reviewer_kind`` / ``reviewer_owner_id`` /
  ``reviewer_agent_id``

Backfills legacy rows: APPROVED facts and DRAFT candidates whose tag array is
empty receive the JSON-safe sentinel ``["__legacy_unclassified__"]`` so they are
excluded from least-privilege projection until the owner classifies them.
Trusted identity columns are backfilled to ``'owner'`` (historical submissions
were all owner-initiated); the typed identity matrix is enforced by the DB
triggers on every INSERT going forward, and by the service layer on every
formal action.

The SQLite triggers are dropped and recreated (augmented) so that:
- candidate INSERT/UPDATE enforce a typed ``submitted_by`` identity matrix,
- fact/candidate UPDATE enforce tag immutability EXCEPT for the one-time
  sentinel -> canonical transition (JSON-safe via ``json_array_length`` +
  ``json_extract``, never ``LIKE``),
- review INSERT enforces a typed ``reviewer`` identity matrix.

Chains off ``20260719_0003`` (the head of ``feat/knowledge-projection``). If the
review-protocol migration (0004) lands on ``main`` first, rebase this branch
onto it and repoint ``down_revision`` to ``20260719_0004``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260720_0005"
down_revision: str | None = "20260719_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SENTINEL = "__legacy_unclassified__"


def _drop_triggers() -> None:
    for name in (
        "knowledge_candidate_validate_insert",
        "knowledge_candidate_validate_update",
        "knowledge_review_reject_update",
        "knowledge_fact_validate_insert",
        "knowledge_fact_validate_update",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


def _create_candidate_insert() -> None:
    op.execute(
        """
        CREATE TRIGGER knowledge_candidate_validate_insert
        BEFORE INSERT ON knowledge_candidate
        BEGIN
            SELECT CASE WHEN TRIM(NEW.statement) = '' OR TRIM(NEW.submitted_by) = ''
                THEN RAISE(ABORT, 'knowledge candidate fields must be non-empty') END;
            SELECT CASE WHEN NEW.status <> 'DRAFT'
                THEN RAISE(ABORT, 'knowledge candidate must start draft') END;
            SELECT CASE WHEN NEW.submitted_by_kind IS NULL
                THEN RAISE(ABORT, 'knowledge candidate requires trusted submitted_by_kind') END;
            SELECT CASE WHEN NEW.submitted_by_kind = 'owner'
                AND (NEW.submitted_by_owner_id IS NULL OR NEW.submitted_by_agent_id IS NOT NULL)
                THEN RAISE(ABORT, 'owner candidate identity inconsistent') END;
            SELECT CASE WHEN NEW.submitted_by_kind = 'agent'
                AND (NEW.submitted_by_agent_id IS NULL OR NEW.submitted_by_owner_id IS NOT NULL)
                THEN RAISE(ABORT, 'agent candidate identity inconsistent') END;
            SELECT CASE WHEN NEW.submitted_by_kind = 'system'
                AND (NEW.submitted_by_owner_id IS NOT NULL OR NEW.submitted_by_agent_id IS NOT NULL)
                THEN RAISE(ABORT, 'system candidate identity inconsistent') END;
            SELECT CASE WHEN NEW.source_project_id IS NULL
                THEN RAISE(ABORT, 'knowledge candidate requires a source campaign') END;
            SELECT CASE WHEN NEW.project_id IS NOT NULL AND NEW.project_id <> NEW.source_project_id
                THEN RAISE(ABORT, 'project-scoped candidate must match its source campaign') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM artifact a
                WHERE a.id = NEW.artifact_id AND a.review_status = 'APPROVED'
                  AND a.project_id IS NEW.source_project_id
            ) THEN RAISE(ABORT,
                'knowledge candidate requires an approved same-campaign artifact') END;
        END
        """
    )


def _create_candidate_update() -> None:
    op.execute(
        """
        CREATE TRIGGER knowledge_candidate_validate_update
        BEFORE UPDATE ON knowledge_candidate
        BEGIN
            SELECT CASE WHEN OLD.artifact_id <> NEW.artifact_id
                OR OLD.project_id IS NOT NEW.project_id
                OR OLD.source_project_id IS NOT NEW.source_project_id
                OR OLD.statement <> NEW.statement
                OR OLD.submitted_by <> NEW.submitted_by
                OR OLD.submitted_by_kind <> NEW.submitted_by_kind
                OR OLD.submitted_by_owner_id IS NOT NEW.submitted_by_owner_id
                OR OLD.submitted_by_agent_id IS NOT NEW.submitted_by_agent_id
                OR OLD.created_at <> NEW.created_at
                THEN RAISE(ABORT, 'knowledge candidate identity is immutable') END;
            SELECT CASE WHEN NOT (
                OLD.tags IS NEW.tags
                OR (json_array_length(OLD.tags) = 1
                    AND json_extract(OLD.tags, '$[0]') = '__legacy_unclassified__')
            ) THEN RAISE(ABORT,
                'knowledge candidate tags are immutable except sentinel classification') END;
            SELECT CASE WHEN OLD.status <> 'DRAFT'
                OR (NEW.status NOT IN ('APPROVED', 'REJECTED')
                    AND NOT (json_array_length(OLD.tags) = 1
                        AND json_extract(OLD.tags, '$[0]') = '__legacy_unclassified__'))
                THEN RAISE(ABORT, 'invalid knowledge candidate lifecycle') END;
        END
        """
    )


def _create_review_reject_update() -> None:
    op.execute(
        """
        CREATE TRIGGER knowledge_review_reject_update
        BEFORE UPDATE ON knowledge_review_decision
        BEGIN
            SELECT RAISE(ABORT, 'knowledge review decision is immutable');
        END
        """
    )


def _create_fact_insert() -> None:
    op.execute(
        """
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
    )


def _create_fact_update() -> None:
    op.execute(
        """
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
    )


def _create_review_insert() -> None:
    op.execute(
        """
        CREATE TRIGGER knowledge_review_validate_insert
        BEFORE INSERT ON knowledge_review_decision
        BEGIN
            SELECT CASE WHEN NEW.reviewer_kind IS NULL
                THEN RAISE(ABORT, 'knowledge review requires trusted reviewer_kind') END;
            SELECT CASE WHEN NEW.reviewer_kind = 'owner'
                AND (NEW.reviewer_owner_id IS NULL OR NEW.reviewer_agent_id IS NOT NULL)
                THEN RAISE(ABORT, 'owner review identity inconsistent') END;
            SELECT CASE WHEN NEW.reviewer_kind = 'agent'
                AND (NEW.reviewer_agent_id IS NULL OR NEW.reviewer_owner_id IS NOT NULL)
                THEN RAISE(ABORT, 'agent review identity inconsistent') END;
            SELECT CASE WHEN NEW.reviewer_kind = 'system'
                AND (NEW.reviewer_owner_id IS NOT NULL OR NEW.reviewer_agent_id IS NOT NULL)
                THEN RAISE(ABORT, 'system review identity inconsistent') END;
            SELECT CASE WHEN NEW.decision NOT IN ('APPROVE', 'REJECT')
                THEN RAISE(ABORT, 'knowledge review decision invalid') END;
        END
        """
    )


def upgrade() -> None:
    # 0. Drop the existing knowledge triggers FIRST. They reference these tables,
    #    and SQLite re-validates trigger bodies during any table rewrite. Dropping
    #    them up front lets the column additions + backfill run without firing the
    #    old (now-invalid) validation, and lets us recreate the augmented versions.
    _drop_triggers()

    # 1. Add capability-projection tags + typed trusted identity columns.
    #    tags / *_kind are NOT NULL with a server default so existing rows stay
    #    valid; the identity matrix is enforced on every future INSERT by the
    #    recreated triggers, and by the service layer on every formal action.
    op.add_column(
        "knowledge_candidate",
        sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "knowledge_candidate",
        sa.Column("submitted_by_kind", sa.String(), nullable=False, server_default="owner"),
    )
    op.add_column(
        "knowledge_candidate",
        sa.Column("submitted_by_owner_id", sa.String(), nullable=True),
    )
    op.add_column(
        "knowledge_candidate",
        sa.Column("submitted_by_agent_id", sa.String(), nullable=True),
    )
    op.add_column(
        "knowledge_fact",
        sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "knowledge_review_decision",
        sa.Column("reviewer_kind", sa.String(), nullable=False, server_default="owner"),
    )
    op.add_column(
        "knowledge_review_decision",
        sa.Column("reviewer_owner_id", sa.String(), nullable=True),
    )
    op.add_column(
        "knowledge_review_decision",
        sa.Column("reviewer_agent_id", sa.String(), nullable=True),
    )

    # 2. Backfill the JSON-safe sentinel for legacy untagged knowledge. Runs while
    #    triggers are dropped, so it is exempt from the normal tags-immutability
    #    rule (it is the REVERSE direction, empty -> sentinel, which the triggers
    #    forbid -- that is intentional: only this one-time migration may do it).
    op.execute(
        f"""
        UPDATE knowledge_fact
        SET tags = '["{SENTINEL}"]'
        WHERE status = 'APPROVED' AND json_array_length(tags) = 0
        """
    )
    op.execute(
        f"""
        UPDATE knowledge_candidate
        SET tags = '["{SENTINEL}"]'
        WHERE status = 'DRAFT' AND json_array_length(tags) = 0
        """
    )

    # 3. Recreate triggers: augmented identity + tags rules, plus the new review
    #    identity matrix.
    _create_candidate_insert()
    _create_candidate_update()
    _create_review_reject_update()
    _create_fact_insert()
    _create_fact_update()
    _create_review_insert()


def downgrade() -> None:
    # Drop the augmented triggers + the new review-insert trigger.
    _drop_triggers()
    op.execute("DROP TRIGGER IF EXISTS knowledge_review_validate_insert")

    # Drop the columns added by this migration.
    op.drop_column("knowledge_review_decision", "reviewer_agent_id")
    op.drop_column("knowledge_review_decision", "reviewer_owner_id")
    op.drop_column("knowledge_review_decision", "reviewer_kind")
    op.drop_column("knowledge_fact", "tags")
    op.drop_column("knowledge_candidate", "submitted_by_agent_id")
    op.drop_column("knowledge_candidate", "submitted_by_owner_id")
    op.drop_column("knowledge_candidate", "submitted_by_kind")
    op.drop_column("knowledge_candidate", "tags")

    # Restore the pre-#67 trigger bodies (authored in 20260719_0001), which do not
    # reference the removed columns and enforce the original identity rules.
    op.execute(
        """
        CREATE TRIGGER knowledge_candidate_validate_insert
        BEFORE INSERT ON knowledge_candidate
        BEGIN
            SELECT CASE WHEN TRIM(NEW.statement) = '' OR TRIM(NEW.submitted_by) = ''
                THEN RAISE(ABORT, 'knowledge candidate fields must be non-empty') END;
            SELECT CASE WHEN NEW.status <> 'DRAFT'
                THEN RAISE(ABORT, 'knowledge candidate must start draft') END;
            SELECT CASE WHEN NEW.source_project_id IS NULL
                THEN RAISE(ABORT, 'knowledge candidate requires a source campaign') END;
            SELECT CASE WHEN NEW.project_id IS NOT NULL AND NEW.project_id <> NEW.source_project_id
                THEN RAISE(ABORT, 'project-scoped candidate must match its source campaign') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM artifact a
                WHERE a.id = NEW.artifact_id AND a.review_status = 'APPROVED'
                  AND a.project_id IS NEW.source_project_id
            ) THEN RAISE(ABORT,
                'knowledge candidate requires an approved same-campaign artifact') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_candidate_validate_update
        BEFORE UPDATE ON knowledge_candidate
        BEGIN
            SELECT CASE WHEN OLD.artifact_id <> NEW.artifact_id
                OR OLD.project_id IS NOT NEW.project_id
                OR OLD.source_project_id IS NOT NEW.source_project_id
                OR OLD.statement <> NEW.statement
                OR OLD.submitted_by <> NEW.submitted_by OR OLD.created_at <> NEW.created_at
                THEN RAISE(ABORT, 'knowledge candidate identity is immutable') END;
            SELECT CASE WHEN OLD.status <> 'DRAFT'
                OR NEW.status NOT IN ('APPROVED', 'REJECTED')
                THEN RAISE(ABORT, 'invalid knowledge candidate lifecycle') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_review_reject_update
        BEFORE UPDATE ON knowledge_review_decision
        BEGIN
            SELECT RAISE(ABORT, 'knowledge review decision is immutable');
        END
        """
    )
    op.execute(
        """
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
    )
    op.execute(
        """
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
            SELECT CASE WHEN OLD.status <> 'APPROVED'
                OR NEW.status NOT IN ('SUPERSEDED', 'INACTIVE')
                THEN RAISE(ABORT, 'invalid knowledge fact lifecycle') END;
        END
        """
    )
