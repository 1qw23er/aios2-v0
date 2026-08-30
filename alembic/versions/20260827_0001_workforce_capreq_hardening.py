"""W1 hardening -- capability_requirement uniqueness + RESTRICT on Capability FK.

Two hardening fixes on the W1 ``capability_requirement`` table (audit P2 items):

1. UNIQUE(job_version_id, capability_id) -- a single JobVersion may require a
   given Alpha-1 Capability at most once. The service ``add_capability_requirement``
   does NOT de-dup, so this constraint is the authoritative guard against silent
   duplicate requirements.

2. ``capability_id`` FK ``ondelete`` CASCADE -> RESTRICT. Retiring/deleting an
   Alpha-1 Capability that is still referenced by any Workforce requirement MUST
   FAIL EXPLICITLY (IntegrityError). We never silently wipe Workforce hiring
   history. The parent ``job_version_id`` FK keeps CASCADE (deleting a JobVersion
   is intentional subtree removal).

WHY TABLE REBUILD (not batch_alter_table):
The W1 core migration created both FKs on ``capability_requirement`` *unnamed*.
Alembic's ``batch_alter_table`` cannot drop/recreate an unnamed FK -- it raises
``ValueError('Constraint must have a name')`` (and ``drop_constraint`` rejects a
``columns=`` kwarg). The only reliable path on SQLite is a controlled table
rebuild: create ``_new`` with the corrected, *named* constraints, copy data
( collapsing any pre-existing duplicates per (job_version_id, capability_id) so
the unique index can build ), drop the old table, rename ``_new`` into place.

INDEX NAMING (SQLite gotcha): SQLite index names are database-global, not
per-table. The old ``capability_requirement`` still owns
``ix_capability_requirement_job_version_id`` / ``..._capability_id`` while the
``_new`` table exists, so we must NOT create those indexes on ``_new``. We build
``_new`` *without* the two secondary indexes, copy, drop+rename, and only THEN
create the indexes on the final (renamed) table -- by which point the old table
and its indexes are already gone.

Fully reversible: downgrade rebuilds the original CASCADE / no-unique shape.

Sits ON TOP of ``20260825_0001_workforce_core`` and becomes the new single head.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260827_0001_workforce_capreq_hardening"
down_revision: str | None = "20260825_0001_workforce_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "capability_requirement"
NEW = "capability_requirement_new"


def _build_table(table_name: str, *, restrict_capability: bool) -> None:
    """Create ``capability_requirement`` (or ``_new``) with the target shape.

    ``restrict_capability=True`` => capability_id FK ondelete RESTRICT + UNIQUE.
    ``restrict_capability=False`` => capability_id FK ondelete CASCADE, no UNIQUE
    (the original W1 core shape, used by downgrade).

    NOTE: secondary indexes are created separately (see ``_create_indexes``) AFTER
    the final rename, to avoid SQLite's global index-name collision with the old
    table that still exists during the rebuild.
    """
    fk_capability_ondelete = "RESTRICT" if restrict_capability else "CASCADE"
    table_args = [
        sa.ForeignKeyConstraint(
            ["job_version_id"],
            ["job_version.id"],
            ondelete="CASCADE",
            name="fk_capability_requirement_job_version_id",
        ),
        sa.ForeignKeyConstraint(
            ["capability_id"],
            ["capability.id"],
            ondelete=fk_capability_ondelete,
            name="fk_capability_requirement_capability_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "min_proficiency >= 1 AND min_proficiency <= 100",
            name="ck_capability_requirement_proficiency",
        ),
    ]
    if restrict_capability:
        table_args.append(
            sa.UniqueConstraint(
                "job_version_id",
                "capability_id",
                name="uq_capability_requirement_job_version_capability",
            )
        )

    op.create_table(
        table_name,
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("job_version_id", sa.String(), nullable=False),
        sa.Column("capability_id", sa.String(), nullable=False),
        sa.Column("capability_name", sa.String(), nullable=False),
        sa.Column("min_proficiency", sa.Integer(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        *table_args,
    )


def _create_indexes(table_name: str) -> None:
    op.create_index("ix_capability_requirement_job_version_id", table_name, ["job_version_id"])
    op.create_index("ix_capability_requirement_capability_id", table_name, ["capability_id"])


def _copy_collapse_duplicates(src: str, dst: str) -> None:
    """Copy rows src -> dst, collapsing duplicate (job_version_id, capability_id).

    Idempotent per-pair: MIN() keeps exactly one row when duplicates exist, so the
    new UNIQUE index can build. For clean data this is identical to a plain copy.
    """
    op.execute(
        sa.text(
            f"""
            INSERT INTO {dst} (
                id, job_version_id, capability_id, capability_name,
                min_proficiency, required, notes, created_at
            )
            SELECT
                MIN(id), job_version_id, capability_id, MIN(capability_name),
                MIN(min_proficiency), MIN(required), MIN(notes), MIN(created_at)
            FROM {src}
            GROUP BY job_version_id, capability_id
            """
        )
    )


def upgrade() -> None:
    _build_table(NEW, restrict_capability=True)
    _copy_collapse_duplicates(OLD, NEW)
    op.drop_table(OLD)
    op.rename_table(NEW, OLD)
    _create_indexes(OLD)


def downgrade() -> None:
    # Rebuild the original W1 core shape: capability_id FK CASCADE, no UNIQUE.
    _build_table(NEW, restrict_capability=False)
    op.execute(
        sa.text(
            f"""
            INSERT INTO {NEW} (
                id, job_version_id, capability_id, capability_name,
                min_proficiency, required, notes, created_at
            )
            SELECT
                id, job_version_id, capability_id, capability_name,
                min_proficiency, required, notes, created_at
            FROM {OLD}
            """
        )
    )
    op.drop_table(OLD)
    op.rename_table(NEW, OLD)
    _create_indexes(OLD)
