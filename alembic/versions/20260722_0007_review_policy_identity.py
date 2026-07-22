"""ReviewPolicy identity constraint (D3 hardening, #72 / #74).

D3 introduced the ONLY production path that writes a ReviewPolicy row
(``aios.review.create_review_policy``). To make idempotency concurrency-safe
(req 4 / req 8, same rationale as the review_binding unique indexes in
20260720_0006), enforce a single source of truth on ``review_policy.name`` at
the DB level: two concurrent "create same name" requests converge to one row
instead of racing to a duplicate.

  * ``uq_review_policy_name`` -- unique on ``review_policy.name``.

Identity scope (locked decision): ``name`` is GLOBALLY unique. ReviewPolicy is a
reusable governance config that does not belong to a single project; adding a
per-project key (or ``project_id``) would be new domain design outside D3, and
dispatch already references policies by explicit ``policy_id``.

Why a raw unique index add (not ``batch_alter_table``): ``review_policy`` is an
existing table. A raw unique index touches no trigger and matches the
established pattern in 20260720_0006.

Preflight (fail-closed): the application canonicalizes a policy name with
``name.strip()`` (D3 identity rule). The raw ``UNIQUE(name)`` index therefore
only matches the application identity if every stored name is ALREADY canonical
and no two distinct raw names collapse to the same canonical identity. The
migration refuses to run when either condition is violated:
  (A) any ``name`` is not equal to ``trim(name)`` or its trimmed form is empty
      (non-canonical / whitespace-only) -- reported with the offending id+name;
  (B) two distinct raw names share the same ``trim(name)`` (canonical
      duplicate) -- reported with the canonical name + count.
It never auto-deletes rows, auto-trims, auto-renames, or picks a "winner" -- the
operator must resolve these explicitly before re-running.

Revision ID: 20260722_0007
Revises: 20260720_0006
"""

from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision = "20260722_0007"
down_revision = "20260720_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # (A) Non-canonical names. The application canonicalizes a policy name with
    # ``name.strip()`` (D3 identity rule). A stored row whose name is not already
    # trimmed, or whose trimmed form is empty, is corrupt relative to that rule
    # and would collide with a different canonical identity once the raw
    # UNIQUE(name) index is added. Refuse to migrate; never auto-trim, rename, or
    # delete the offending rows -- report the offending id + name.
    non_canonical = bind.execute(
        text(
            "SELECT id, name FROM review_policy "
            "WHERE name IS NULL "
            "   OR name <> trim(name) "
            "   OR trim(name) = ''"
        )
    ).fetchall()
    if non_canonical:
        detail = ", ".join(f"id={row[0]!r} name={row[1]!r}" for row in non_canonical)
        raise RuntimeError(
            "migration 20260722_0007 aborted: non-canonical review_policy.name "
            f"rows exist (leading/trailing whitespace or whitespace-only): "
            f"{detail}. Manually canonicalize (trim) or delete these rows before "
            "re-running the migration. The migration never auto-modifies data."
        )

    # (B) Canonical duplicates. Distinct raw names that collapse to the same
    # trimmed identity are silently conflated by the app-layer canonicalization
    # yet violate the raw UNIQUE(name) contract. Refuse; report the canonical
    # name + count.
    canonical_dupes = bind.execute(
        text(
            "SELECT trim(name) AS cname, COUNT(*) AS c FROM review_policy "
            "GROUP BY trim(name) HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if canonical_dupes:
        detail = ", ".join(f"{row[0]!r} x{row[1]}" for row in canonical_dupes)
        raise RuntimeError(
            "migration 20260722_0007 aborted: canonical-duplicate "
            f"review_policy.name rows exist: {detail}. These distinct raw names "
            "map to the same trimmed identity under the app-layer "
            "canonicalization. Resolve manually before re-running."
        )

    # (C) Only after both preflights pass does the raw UNIQUE(name) index match
    # the application-layer identity rule, so the DB constraint and service-layer
    # identity are finally consistent.
    op.create_index("uq_review_policy_name", "review_policy", ["name"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_review_policy_name")
