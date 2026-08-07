"""Create pilot2 tables against the isolated human-UAT staging database ONLY.

Contract D3 (design ``docs/pilot-2a-leadgen-attribution-plan.md`` §11.3).

The guard does **not** pattern-match the database URL. Substring / "marker"
matching is unsound: it accepts look-alike siblings, backup copies, and URLs
whose only match is inside userinfo or the query string. Instead the guard:

1. parses the URL strictly (scheme, host, port, userinfo, query all checked);
2. resolves the referenced file to a real, absolute, normalised filesystem path;
3. derives the ONE authorised path from the authoritative environment module
   ``human_env`` (attribute ``UAT_DB_PATH``) -- never from a literal repeated
   here (clarification C3);
4. compares the two normalised paths for **exact equality**.

Everything else is refused. The guard is fail-closed in every failure mode:
an unparsable URL, an underivable authoritative path, an ambiguous environment
module, or any mismatch all yield "do not create", and the CLI entry point
exits non-zero. It never degrades to a warning.

Importing this module has no side effects; tables are only ever created by
running it as a program:

    python -m aios.pilot2.migrations_create_all
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateIndex, CreateTable

# ``.../aios_main/src/aios/pilot2/migrations_create_all.py`` -> ``.../aios_main``
_REPO_ROOT = Path(__file__).resolve().parents[3]

# The authoritative environment module is FIXED to the repo-canonical location
# below. Only its NAME is referenced here; the staging path itself is read from
# it at runtime (C3). No environment variable or caller-supplied path may select
# a different authority -- see ``_authoritative_env_module_file`` and FIX 6 / D3.
_ENV_MODULE_NAME = "human_env"
_ENV_MODULE_FILENAME = f"{_ENV_MODULE_NAME}.py"
_ENV_PATH_ATTR = "UAT_DB_PATH"


class StagingGuardError(RuntimeError):
    """Raised when the authorised staging path cannot be established."""


# ---------------------------------------------------------------------------
# Path normalisation
# ---------------------------------------------------------------------------
def normalize_fs_path(path: Path | str) -> str:
    """Absolute + symlink-resolved + case-normalised path, for exact compare.

    ``realpath`` collapses ``..`` segments and symlinks (so a symlinked
    look-alike cannot impersonate the authorised file) and ``normcase``
    normalises separators and case on Windows. Works for paths that do not
    exist yet, which matters because the staging DB may not be created.
    """
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


# ---------------------------------------------------------------------------
# Strict URL parsing
# ---------------------------------------------------------------------------
def parse_sqlite_file_url(database_url: str) -> Path:
    """Return the absolute file path a SQLite URL points at.

    Raises :class:`StagingGuardError` for anything that is not a plain,
    absolute, local SQLite file URL -- including a wrong scheme, a driver
    qualifier, any host/port, any userinfo, any query string, a relative path,
    and the in-memory database.
    """
    try:
        url = make_url(database_url)
    except Exception as exc:  # unparsable -> fail closed
        raise StagingGuardError(f"unparsable database URL: {exc}") from None

    if url.drivername != "sqlite":
        raise StagingGuardError(
            f"scheme {url.drivername!r} is not the plain local 'sqlite' scheme"
        )
    if url.username or url.password:
        raise StagingGuardError("URL carries userinfo; refused")
    if url.host:
        raise StagingGuardError(f"URL carries host {url.host!r}; refused")
    if url.port:
        raise StagingGuardError(f"URL carries port {url.port!r}; refused")
    if url.query:
        raise StagingGuardError(f"URL carries query parameters {dict(url.query)!r}; refused")

    database = url.database
    if not database:
        raise StagingGuardError("URL has no database path")
    if database == ":memory:":
        raise StagingGuardError("in-memory database is not the staging database")

    path = Path(database)
    if not path.is_absolute():
        raise StagingGuardError(f"database path {database!r} is not absolute; refused")
    return path


# ---------------------------------------------------------------------------
# Authoritative staging path (derived, never hardcoded -- C3)
# ---------------------------------------------------------------------------
# The authoritative environment module is pinned to this repo-relative path. It
# is never discovered via an environment variable or a caller-supplied path, so
# a look-alike module or a sandbox-specific override can never become the
# authority (FIX 6 / D3). If the canonical module is absent the guard fails
# closed.
_ENV_MODULE_RELATIVE = ("uat_ool_v0", _ENV_MODULE_FILENAME)


def _authoritative_env_module_file() -> Path:
    """The single, repo-fixed authoritative environment module.

    No environment variable, override, or caller-supplied path can change which
    file supplies ``UAT_DB_PATH`` -- the path below is the only authority.
    """
    candidate = _REPO_ROOT.joinpath(*_ENV_MODULE_RELATIVE)
    if not candidate.is_file():
        raise StagingGuardError(
            f"authoritative module {_ENV_MODULE_FILENAME!r} not found at the "
            f"repo-fixed path {candidate}; refusing"
        )
    return candidate


def _load_env_module(module_file: Path):
    spec = importlib.util.spec_from_file_location("_pilot2_staging_authority", module_file)
    if spec is None or spec.loader is None:
        raise StagingGuardError(f"cannot load authoritative module from {module_file}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise StagingGuardError(f"authoritative module failed to import: {exc}") from None
    return module


def resolve_authorized_staging_path(*, authority_module: Path | str | None = None) -> Path:
    """The single authorised staging DB path, derived from ``human_env`` (C3).

    Without ``authority_module`` the path is loaded from the repo-fixed
    ``uat_ool_v0/human_env.py`` only -- no environment variable or caller-selected
    module can substitute the authority (FIX 6 / D3). ``authority_module`` is a
    TEST-ONLY injection: it loads that one module file and is never passed by any
    production code path (``main`` / ``should_create``).

    Raises :class:`StagingGuardError` when it cannot be established -- callers
    must treat that as "refuse", never as "allow".
    """
    module_file = (
        Path(authority_module) if authority_module is not None
        else _authoritative_env_module_file()
    )
    module = _load_env_module(module_file)
    raw = getattr(module, _ENV_PATH_ATTR, None)
    if raw is None:
        raise StagingGuardError(
            f"authoritative module exposes no {_ENV_PATH_ATTR!r} attribute"
        )
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        raise StagingGuardError(f"authoritative {_ENV_PATH_ATTR} {raw!r} is not absolute")
    return path


# ---------------------------------------------------------------------------
# Authorisation decision
# ---------------------------------------------------------------------------
def authorization_error(database_url: str, *, staging_path: Path | str) -> str | None:
    """``None`` when the URL is exactly the authorised staging DB, else why not.

    ``staging_path`` is injected explicitly so the comparison is a pure
    function: callers in production always pass
    :func:`resolve_authorized_staging_path`.
    """
    try:
        candidate = parse_sqlite_file_url(database_url)
    except StagingGuardError as exc:
        return str(exc)

    actual = normalize_fs_path(candidate)
    expected = normalize_fs_path(staging_path)
    if actual != expected:
        return f"path {actual!r} is not the authorised staging path {expected!r}"
    return None


def is_authorized_staging_url(database_url: str, *, staging_path: Path | str) -> bool:
    """Pure comparator: exact normalised-path match against the given authority."""
    return authorization_error(database_url, staging_path=staging_path) is None


def should_create(database_url: str, *, staging_path: Path | str | None = None) -> bool:
    """True only for the one authorised staging database. Fail-closed.

    ``staging_path`` is an optional injection used by tests; production callers
    (and ``main``) omit it and resolve the repo-fixed authority instead.
    """
    if staging_path is None:
        try:
            staging_path = resolve_authorized_staging_path()
        except StagingGuardError:
            return False
    return is_authorized_staging_url(database_url, staging_path=staging_path)


# ---------------------------------------------------------------------------
# Table creation (only ever reached from ``main``)
# ---------------------------------------------------------------------------
# Tables left behind by a SUPERSEDED pilot2 shape. PILOT-2A owns an ISOLATED
# staging schema and holds no authorisation to touch the main alembic head, so a
# shape change is applied by REBUILDING that isolated schema, never by a main
# migration. ``finalattribution`` was the single-table attribution state machine
# replaced by ``finalattributiondecision`` + ``finalattributionhead``; leaving it
# in place would keep a writable surface that no longer honours the D2
# invariants, so the rebuild retires it (SQLite drops the table's triggers with
# it). Nothing outside the pilot2 namespace is ever touched.
#
# ORDER IS A SAFETY PROPERTY. An earlier revision dropped this table FIRST and
# created the replacement afterwards, each in its own committed transaction.
# That is destroy-before-prove: the head commit of this very branch failed to
# create the new tables on CI (a trigger DDL the CI SQLite build rejected), and
# under the old order that failure mode arrives AFTER the old table and its rows
# are already gone. So the rebuild now (1) creates the replacement schema,
# (2) VERIFIES it -- tables *and* D2 triggers -- and only then (3) retires the
# old shape, refusing outright if retirement would destroy rows unless the
# caller opted in to that explicitly.
RETIRED_TABLES: tuple[str, ...] = ("finalattribution",)


class SchemaRebuildError(RuntimeError):
    """The isolated staging rebuild refused to proceed (fail-closed)."""


def _existing_tables(engine) -> set[str]:
    from sqlalchemy import inspect

    return set(inspect(engine).get_table_names())


def _existing_triggers(engine) -> set[str]:
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'")).all()
    return {row[0] for row in rows}


def _table_column_names(cur, table_name: str) -> set[str]:
    rows = cur.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    return {row[1] for row in rows}


def _table_index_names(cur, table_name: str) -> set[str]:
    rows = cur.execute(f'PRAGMA index_list("{table_name}")').fetchall()
    return {row[1] for row in rows}


def _index_is_unique(cur, table_name: str, index_name: str) -> bool:
    """True when ``index_name`` on ``table_name`` is declared UNIQUE."""
    for row in cur.execute(f'PRAGMA index_list("{table_name}")').fetchall():
        # PRAGMA index_list -> (seq, name, unique, origin, partial)
        if row[1] == index_name:
            return bool(row[2])
    return False


def _index_columns(cur, index_name: str) -> list[str]:
    """Ordered column names that back ``index_name``."""
    return [row[2] for row in cur.execute(f'PRAGMA index_info("{index_name}")').fetchall()]


def _table_fk_tuples(engine_or_cur, table_name: str) -> set[tuple[str, str, str]]:
    """``{(child_col, parent_table, parent_col), ...}`` currently present in the
    DB for ``table_name``, read from ``PRAGMA foreign_key_list``.

    Accepts either a DBAPI cursor (the locked cursor used by ``retire`` /
    ``evolve``) or an SQLAlchemy ``Engine`` (a fresh connection is opened).
    """
    if hasattr(engine_or_cur, "connect"):  # Engine -> open a connection
        with engine_or_cur.connect() as conn:
            rows = conn.execute(text(f'PRAGMA foreign_key_list("{table_name}")')).all()
    else:  # DBAPI cursor
        rows = engine_or_cur.execute(
            f'PRAGMA foreign_key_list("{table_name}")'
        ).fetchall()
    # PRAGMA foreign_key_list -> id, seq, table, from, to, on_update, on_delete, match
    return {(row[3], row[2], row[4]) for row in rows}


def _declared_fk_tuples(table) -> set[tuple[str, str, str]]:
    """``{(child_col, parent_table, parent_col), ...}`` the merged model declares
    on ``table`` (column-level ``ForeignKey`` objects).
    """
    declared: set[tuple[str, str, str]] = set()
    for column in table.columns:
        for fk in column.foreign_keys:
            declared.add((fk.parent.name, fk.column.table.name, fk.column.name))
    return declared


def _table_missing_fks(cur, table) -> bool:
    """True when ``table`` is missing any foreign key the merged model declares.

    SQLite cannot ``ALTER TABLE ... ADD CONSTRAINT`` a foreign key, so a missing
    FK is the one case where evolution must fall back to a full table rebuild
    (see :func:`_rebuild_table_for_missing_fk`).
    """
    return bool(_declared_fk_tuples(table) - _table_fk_tuples(cur, table.name))


def _column_default_literal(column):
    """SQLite ``DEFAULT`` literal for a model column, or ``None`` if unset.

    Handles the scalar defaults the pilot2 models actually use (``int`` /
    ``bool`` / ``str`` / ``StrEnum``). Python-side ``default`` callables (e.g.
    ``now_utc``) are intentionally ignored: they cannot be expressed as a static
    SQLite literal and are never needed for the ADD COLUMN cases here.
    """
    for d in (column.server_default, column.default):
        if d is None:
            continue
        arg = getattr(d, "arg", None)
        if arg is None:
            continue
        if isinstance(arg, bool):
            return "1" if arg else "0"
        if isinstance(arg, (int, float)):
            return str(arg)
        value = getattr(arg, "value", arg)
        return "'" + str(value).replace("'", "''") + "'"
    return None


def _add_column_definition(column) -> str:
    """SQLite ``ALTER TABLE ... ADD COLUMN <definition>`` fragment for a column.

    A NOT NULL column must carry a usable DEFAULT or SQLite refuses to add it to
    a non-empty table; every non-null pilot2 column that this path ever adds has
    one, so the literal is always renderable when required.
    """
    from sqlalchemy.dialects.sqlite import dialect as _sqlite_dialect

    type_str = column.type.compile(dialect=_sqlite_dialect())
    ddl = f'"{column.name}" {type_str}'
    if not column.nullable:
        ddl += " NOT NULL"
        literal = _column_default_literal(column)
        if literal is not None:
            ddl += f" DEFAULT {literal}"
    return ddl


def evolve_existing_tables(engine) -> list[str]:
    """EVOLVE Pilot-2 tables that already exist up to the merged shape.

    ``pilot2_metadata.create_all`` is a no-op for a TABLE that is already present,
    so a 2A-2-era ``registrationobservation`` (missing the PILOT-2A-3 extension
    columns, the UNIQUE(customer_id) index, and/or the source_snapshot_id ->
    mihesnapshot.id foreign key) would survive ``run_create`` untouched and then
    break the diff engine (no ``observation_hash`` column) or accept orphan
    registration rows (a registration pointing at a snapshot that was never
    captured). This function brings those EXISTING isolated Pilot-2 tables up to
    the merged shape.

    Most gaps are closed in place (``ALTER TABLE ADD COLUMN`` / ``CREATE INDEX``).
    The one exception is a MISSING FOREIGN KEY: SQLite has no ALTER-level ADD
    CONSTRAINT, so a table that lacks a declared FK is rebuilt wholesale
    (:func:`_rebuild_table_for_missing_fk`) -- full merged DDL with every row
    copied across. The rebuild is data-preserving, not data-destroying, and only
    runs after a pre-flight proves there are no orphan FK values.

    Design properties (owner: evolve, do not destroy):

    * No table is dropped or recreated. Every pre-existing row -- including the
      immutable ``MiheSnapshot`` raw evidence -- is preserved.
    * Missing columns are appended via ``ALTER TABLE ... ADD COLUMN``; the added
      (nullable) 2A-3 columns default to NULL on legacy rows and the non-null
      ones carry a DEFAULT that fills them.
    * Missing model-declared indexes are created with ``CREATE ... IF NOT
      EXISTS`` -- idempotent and a no-op on an already-evolved table. A legacy
      index that shares a model index NAME but is NON-UNIQUE (or has different
      columns) is dropped and recreated to the merged shape, so a real 2A-2
      staging DB that already carried a plain ``ix_registrationobservation_
      customer_id`` is correctly upgraded to the UNIQUE constraint the diff
      engine relies on (one registration row per customer).
    * Only the ``pilot2_metadata`` namespace is touched; no main-schema,
      KnowledgeFact, Content, CS, Feedback or auth table is ever read or written.
    * Atomicity / fail-closed: a legacy table whose data already violates a
      required UNIQUE constraint (e.g. duplicate ``customer_id``) is caught by a
      PRE-FLIGHT scan that runs BEFORE any DDL, so the refresh raises
      ``SchemaRebuildError`` WITHOUT having mutated the schema -- the table is
      left exactly as found, with no partial ``ALTER`` residue. (SQLite commits
      DDL implicitly, so a mid-evolve error after some ADD COLUMN statements
      could otherwise leave a half-evolved table; the pre-flight removes the only
      data-dependent failure, and every remaining step is additive + idempotent
      per column/index, with ``verify_pilot2_schema`` refusing any half-evolved
      state so a re-run of ``run_create`` simply completes the evolution.)
    """

    from aios.pilot2.models import pilot2_metadata

    raw = engine.raw_connection()
    dbapi = raw.driver_connection
    added: list[str] = []
    try:
        cur = dbapi.cursor()
        existing_tables = {
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

        # ---- PRE-FLIGHT: refuse legacy data that already violates a UNIQUE
        #      constraint the merged model requires, BEFORE any schema mutation.
        #      Raising here is what makes a failed refresh atomic (no ALTER
        #      residue left behind for the diff engine to stumble on). ----
        for table in pilot2_metadata.tables.values():
            if table.name not in existing_tables:
                continue
            for idx in table.indexes:
                if not idx.unique:
                    continue
                cols_sql = ", ".join(f'"{c.name}"' for c in idx.columns)
                dup = cur.execute(
                    f'SELECT 1 FROM "{table.name}" '
                    f'GROUP BY {cols_sql} HAVING COUNT(*) > 1 LIMIT 1'
                ).fetchone()
                if dup is not None:
                    raise SchemaRebuildError(
                        f"cannot evolve {table.name}: the merged model requires "
                        f"UNIQUE({cols_sql}) but the existing data contains "
                        f"duplicate values. Resolve the duplicates before "
                        f"refreshing the staging schema (the diff engine enforces "
                        f"one row per {idx.columns[0].name})."
                    )

        # ---- PRE-FLIGHT: refuse legacy data that already violates a declared
        #      FOREIGN KEY, BEFORE any schema mutation. A 2A-2-era
        #      registrationobservation may carry rows whose source_snapshot_id
        #      points at a mihesnapshot that was never captured. Adding the FK
        #      only AFTER such rows exist would fail the rebuild; raising here
        #      instead leaves the schema untouched (fail-closed, no ALTER
        #      residue) and the offending row is preserved for the operator to
        #      resolve. ----
        for table in pilot2_metadata.tables.values():
            if table.name not in existing_tables:
                continue
            for column in table.columns:
                for fk in column.foreign_keys:
                    parent = fk.column.table.name
                    pcol = fk.column.name
                    ccol = fk.parent.name
                    orphan = cur.execute(
                        f'SELECT 1 FROM "{table.name}" '
                        f'WHERE "{ccol}" IS NOT NULL '
                        f'AND "{ccol}" NOT IN (SELECT "{pcol}" FROM "{parent}") '
                        f'LIMIT 1'
                    ).fetchone()
                    if orphan is not None:
                        raise SchemaRebuildError(
                            f"cannot evolve {table.name}: the merged model requires "
                            f"a foreign key {ccol} -> {parent}.{pcol}, but existing "
                            f"data contains a value that references no {parent} row. "
                            f"Resolve the orphan reference before refreshing the "
                            f"staging schema (refusing so no partial schema is left "
                            f"behind)."
                        )

        # ---- EVOLVE existing Pilot-2 tables. ----
        # A table MISSING a declared foreign key cannot be fixed with ALTER
        # (SQLite has no ADD CONSTRAINT FK); it is rebuilt wholesale -- the full
        # merged DDL (columns + indexes + FK) with every existing row copied
        # across (see :func:`_rebuild_table_for_missing_fk`). Pre-flight above
        # already proved there are no orphan FK values, so the copy cannot
        # violate the new constraint. All other tables are evolved in place with
        # additive ALTER / CREATE statements.
        for table in pilot2_metadata.tables.values():
            if table.name not in existing_tables:
                continue  # create_all creates brand-new tables from scratch
            if _table_missing_fks(cur, table):
                added += _rebuild_table_for_missing_fk(engine, table)
                continue
            present_cols = _table_column_names(cur, table.name)
            for column in table.columns:
                if column.name in present_cols:
                    continue
                cur.execute(
                    f'ALTER TABLE "{table.name}" ADD COLUMN '
                    f'{_add_column_definition(column)}'
                )
                added.append(f"{table.name}.{column.name}")
            present_idx = _table_index_names(cur, table.name)
            for idx in table.indexes:
                if idx.name in present_idx:
                    # An index with this NAME exists. Upgrade it to the merged
                    # shape when it is non-unique or has different columns
                    # (P1-1: a legacy NON-UNIQUE ix_registrationobservation_
                    # customer_id must become UNIQUE). Otherwise leave it.
                    existing_unique = _index_is_unique(cur, table.name, idx.name)
                    existing_cols = _index_columns(cur, idx.name)
                    model_cols = [c.name for c in idx.columns]
                    if existing_unique == idx.unique and existing_cols == model_cols:
                        continue
                    cur.execute(f'DROP INDEX IF EXISTS "{idx.name}"')
                    present_idx.discard(idx.name)
                cols = ", ".join(f'"{c.name}"' for c in idx.columns)
                unique = "UNIQUE " if idx.unique else ""
                cur.execute(
                    f'CREATE {unique}INDEX IF NOT EXISTS "{idx.name}" '
                    f'ON "{table.name}" ({cols})'
                )
                added.append(f"{table.name}::{idx.name}")
        dbapi.commit()
    except Exception:
        dbapi.rollback()
        raise
    finally:
        raw.close()
    return added


def _rebuild_table_for_missing_fk(engine, table) -> list[str]:
    """Recreate ``table`` with its full merged DDL (columns + indexes + FK),
    copying every existing row, when the live table is missing a foreign key the
    merged model requires.

    SQLite cannot ADD a foreign key to an existing table, so a full rebuild is
    the only safe way to introduce one. The caller (``evolve_existing_tables``)
    has already run the pre-flight proving there are no orphan FK values, so the
    COPY cannot violate the new constraint -- the rebuild is data-preserving,
    not data-destroying.

    Properties (owner: evolve, do not destroy):

    * Every pre-existing row is copied; the immutable ``MiheSnapshot`` raw
      evidence and all registration rows survive.
    * Columns missing from the live table (added by the merged model) are filled
      with their model DEFAULT (or NULL when the model allows it) -- never
      silently dropped.
    * The rebuild runs on its own connection with ``PRAGMA foreign_keys=OFF``
      (the pre-flight already guaranteed referential integrity), so it never
      trips over the very constraint it is adding. Application connections
      created through ``get_engine`` still enable ``PRAGMA foreign_keys=ON``.
    * Fail-closed: any error rolls back the rebuild's own transaction and
      re-raises; ``verify_pilot2_schema`` then refuses the half-evolved state so
      a re-run of ``run_create`` simply completes the evolution.
    """
    from sqlalchemy.dialects.sqlite import dialect as _sqlite_dialect

    raw = engine.raw_connection()
    dbapi = raw.driver_connection
    tmp_name = f"{table.name}__p23a_evo"
    changed: list[str] = []
    try:
        cur = dbapi.cursor()
        # The rebuild does not need FK enforcement and must not let the new FK
        # block the DROP of the old table; pre-flight already guaranteed
        # integrity, so turn it off for the duration of the swap.
        cur.execute("PRAGMA foreign_keys=OFF")
        # Drop any temp table left behind by an aborted prior rebuild.
        cur.execute(f'DROP TABLE IF EXISTS "{tmp_name}"')
        # Compile the full merged DDL for the model table (its foreign key
        # resolves through pilot2_metadata) and retarget it at a TEMPORARY name
        # so it does not collide with the live table while both exist in
        # sqlite_master. Cloning via to_metadata would drop the FK into an empty
        # metadata and fail to resolve the referenced parent, so we compile the
        # original and rename only the table / index identifiers (the SQLite
        # dialect renders them unquoted; we cover both quoted and unquoted forms
        # defensively).
        create_ddl = str(CreateTable(table).compile(dialect=_sqlite_dialect()))
        create_ddl = create_ddl.replace(
            f'CREATE TABLE "{table.name}"', f'CREATE TABLE "{tmp_name}"', 1
        ).replace(
            f"CREATE TABLE {table.name}", f"CREATE TABLE {tmp_name}", 1
        )
        cur.execute(create_ddl)
        # Create the model indexes under TEMPORARY names (canonical names are
        # recreated after the swap, once the live table is gone).
        for idx in table.indexes:
            tmp_idx_name = f"__p23a_{idx.name}"
            idx_ddl = str(CreateIndex(idx).compile(dialect=_sqlite_dialect()))
            idx_ddl = (
                idx_ddl.replace(f'CREATE UNIQUE INDEX "{idx.name}"',
                                f'CREATE UNIQUE INDEX "{tmp_idx_name}"', 1)
                .replace(f"CREATE UNIQUE INDEX {idx.name}",
                          f"CREATE UNIQUE INDEX {tmp_idx_name}", 1)
                .replace(f'CREATE INDEX "{idx.name}"',
                          f'CREATE INDEX "{tmp_idx_name}"', 1)
                .replace(f"CREATE INDEX {idx.name}",
                          f"CREATE INDEX {tmp_idx_name}", 1)
                .replace(f'ON "{table.name}"', f'ON "{tmp_name}"', 1)
                .replace(f"ON {table.name} ", f"ON {tmp_name} ", 1)
            )
            cur.execute(idx_ddl)
        # Copy rows column-for-column. Columns present in the live table are
        # copied by name; columns the live table lacks (added by the merged
        # model) are filled with their DEFAULT literal so NOT NULL columns are
        # satisfied.
        present = _table_column_names(cur, table.name)
        select_exprs = []
        for col in table.columns:
            if col.name in present:
                select_exprs.append(f'"{col.name}"')
            else:
                lit = _column_default_literal(col)
                select_exprs.append(lit if lit is not None else "NULL")
        cols = ", ".join(f'"{c.name}"' for c in table.columns)
        select_list = ", ".join(select_exprs)
        cur.execute(
            f'INSERT INTO "{tmp_name}" ({cols}) SELECT {select_list} FROM "{table.name}"'
        )
        # Drop the live table (removes its indexes + the missing FK), then rename
        # the rebuilt copy into its place.
        cur.execute(f'DROP TABLE "{table.name}"')
        cur.execute(f'ALTER TABLE "{tmp_name}" RENAME TO "{table.name}"')
        # Restore the canonical index names: the temp-named indexes are dropped
        # and recreated from the model so the post-rebuild DDL matches the merged
        # shape exactly.
        for idx in table.indexes:
            cur.execute(f'DROP INDEX IF EXISTS "{idx.name}"')
        for idx in table.indexes:
            cur.execute(str(CreateIndex(idx).compile(dialect=_sqlite_dialect())))
        dbapi.commit()
        changed.append(f"{table.name}:rebuilt(fk)")
    except Exception:
        dbapi.rollback()
        raise
    finally:
        raw.close()
    return changed


def verify_pilot2_schema(engine, *, cur=None) -> None:
    """Prove the replacement schema is really there. Fail-closed.

    Checks BOTH halves of the schema, because "the tables exist" is not the same
    claim as "the invariants exist": a pilot2 database whose attribution tables
    were created without their D2 triggers is a fully writable attribution
    surface with every guard missing, and retiring the old table against it
    would be a downgrade, not a rebuild.

    When ``cur`` is supplied it must be a cursor that ALREADY holds the
    ``BEGIN IMMEDIATE`` write lock taken inside :func:`retire_superseded_tables`.
    The checks then run on that locked connection, so the re-verification and the
    subsequent survey/export/drop share one atomic write-locked window. This is
    what closes the "verify-then-lock" TOCTOU: a concurrent writer that DROPS a
    required table or D2 trigger between ``create_all`` and our lock acquisition
    is caught here, under the lock, before any superseded table is dropped. When
    ``cur`` is ``None`` a fresh connection is used (the fast-fail pre-check in
    :func:`run_create`).
    """
    from aios.pilot2.models import D2_TRIGGER_NAMES, pilot2_metadata

    if cur is not None:
        # Read sqlite_master THROUGH the already-locked cursor -- no second
        # connection, no second transaction, so the read sees exactly the schema
        # state that will govern the drop below.
        existing_tables = {
            row[0]
            for row in cur.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        existing_triggers = {
            row[0]
            for row in cur.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }

        def _cols_of(table_name: str) -> set[str]:
            return _table_column_names(cur, table_name)
    else:
        existing_tables = _existing_tables(engine)
        existing_triggers = _existing_triggers(engine)

        def _cols_of(table_name: str) -> set[str]:
            return {c["name"] for c in inspect(engine).get_columns(table_name)}

    missing_tables = sorted(set(pilot2_metadata.tables.keys()) - existing_tables)
    if missing_tables:
        raise SchemaRebuildError(
            f"replacement pilot2 schema is incomplete: missing tables {missing_tables}"
        )
    missing_triggers = sorted(set(D2_TRIGGER_NAMES) - existing_triggers)
    if missing_triggers:
        raise SchemaRebuildError(
            f"replacement pilot2 schema is incomplete: missing D2 triggers {missing_triggers}"
        )

    # PILOT-2A-3A: column completeness. ``create_all`` is a no-op for an
    # existing table, so a 2A-2-era ``registrationobservation`` that lacks the
    # 2A-3 extension columns would otherwise pass the table/trigger checks above
    # and then break the diff engine at persist time (no ``observation_hash``
    # column). Refuse any half-evolved schema whose columns are incomplete;
    # this is what makes the EVOLVE step (``evolve_existing_tables``) mandatory
    # rather than optional.
    missing_columns: list[str] = []
    for table in pilot2_metadata.tables.values():
        if table.name not in existing_tables:
            continue
        present = _cols_of(table.name)
        for column in table.columns:
            if column.name not in present:
                missing_columns.append(f"{table.name}.{column.name}")
    if missing_columns:
        raise SchemaRebuildError(
            f"replacement pilot2 schema is incomplete: missing columns "
            f"{sorted(missing_columns)}"
        )

    # PILOT-2A-3A: foreign-key completeness. SQLite cannot ALTER ADD a FK, so a
    # 2A-2-era registrationobservation that lacks the source_snapshot_id ->
    # mihesnapshot.id foreign key passes the column checks yet still accepts
    # orphan rows (a registration observation pointing at a snapshot that was
    # never captured). Refuse any schema whose declared FKs are not present so
    # the EVOLVE step (which rebuilds the table to add the FK) is mandatory
    # rather than optional. This is what makes "no FK = no connection" hold:
    # the diff engine refuses to persist against a table that cannot enforce its
    # foreign key.
    for table in pilot2_metadata.tables.values():
        if table.name not in existing_tables:
            continue
        actual_fks = _table_fk_tuples(cur if cur is not None else engine, table.name)
        missing_fks = sorted(_declared_fk_tuples(table) - actual_fks)
        if missing_fks:
            raise SchemaRebuildError(
                f"replacement pilot2 schema is incomplete: table {table.name} "
                f"is missing foreign keys {missing_fks} "
                f"(FK presence is enforced for staging referential integrity)"
            )


def retirement_survey(engine) -> dict[str, int]:
    """Superseded pilot2 tables still present, mapped to their row counts."""
    from sqlalchemy import text

    present = [name for name in RETIRED_TABLES if name in _existing_tables(engine)]
    if not present:
        return {}
    survey: dict[str, int] = {}
    with engine.connect() as conn:
        for name in present:
            survey[name] = int(conn.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar_one())
    return survey


def _default_export_dir(engine) -> Path:
    database = getattr(engine.url, "database", None)
    if not database:
        raise SchemaRebuildError(
            "cannot derive an export location for this engine; pass export_dir explicitly"
        )
    return Path(database).resolve().parent


def _table_exists_on(cur, name: str) -> bool:
    """True when ``name`` is a real table, read through an already-open cursor."""
    row = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _export_rows_from_cursor(cur, table_names, export_dir, engine) -> Path:
    """Write every row of ``table_names`` (read via ``cur``) to a JSON sidecar.

    The cursor is the SAME one that holds the write lock during retirement, so
    the rows exported are exactly the rows the survey counted -- there is no
    window in which a concurrent insert could land between the count and the
    read. The sidecar lands next to the staging database by default and is named
    with a UTC timestamp, so repeated rebuilds never overwrite each other.
    """
    target = Path(export_dir) if export_dir is not None else _default_export_dir(engine)
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = target / f"pilot2_retired_rows_{stamp}.json"

    payload: dict[str, object] = {
        "exported_at": datetime.now(UTC).isoformat(),
        "reason": "PILOT-2A isolated staging rebuild: superseded tables retired",
        "tables": {},
    }
    for name in table_names:
        cur.execute(f'SELECT * FROM "{name}"')
        cols = [d[0] for d in cur.description]
        rows = [list(row) for row in cur.fetchall()]
        payload["tables"][name] = {"columns": cols, "rows": rows}
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def export_superseded_rows(engine, table_names, *, export_dir: Path | None = None) -> Path:
    """Write every row of the named tables to a JSON sidecar, and return its path.

    Retirement is irreversible, so the rows leave the database before the table
    does. Delegates to :func:`_export_rows_from_cursor` over a single read
    connection; when invoked from :func:`retire_superseded_tables` the very same
    cursor that holds the write lock is reused, so the export sees precisely the
    rows the survey counted.
    """
    raw = engine.raw_connection()
    try:
        cur = raw.driver_connection.cursor()
        return _export_rows_from_cursor(cur, table_names, export_dir, engine)
    finally:
        raw.close()


def retire_superseded_tables(
    engine,
    *,
    allow_data_loss: bool = False,
    export_dir: Path | None = None,
    lock_timeout_ms: int = 5000,
) -> list[str]:
    """Drop superseded pilot2 tables -- only after the replacement is PROVEN.

    Idempotent: a database that has already been rebuilt has nothing to retire
    and this returns ``[]``.

    Two refusals, both fail-closed:

    * the replacement schema does not verify -- nothing is dropped, so a failed
      rebuild leaves the previous shape (and its rows) exactly where they were;
    * a superseded table still holds rows and the caller did not opt in --
      nothing is dropped. ``allow_data_loss=True`` is the explicit
      staging-reset / data-retention contract: it exports every row to a JSON
      sidecar first (see :func:`export_superseded_rows`) and only then drops.

    Concurrency / TOCTOU closure: the replacement-schema re-verification (tables
    AND D2 triggers), the survey, the necessary export, and the DROP all run
    inside ONE transaction that holds the SQLite write lock (``BEGIN IMMEDIATE``)
    for the entire duration -- and the re-verification runs on the SAME cursor
    that holds the lock, AFTER it is acquired. No other connection can insert a
    row into a superseded table, or drop a required table/trigger, between the
    survey and the drop, so a late write either (a) commits before our lock and
    is therefore counted -- and then refused (or exported under
    ``allow_data_loss``), or (b) is blocked until we commit and then fails loudly
    against a table that is already gone; and a schema corruption that lands
    between ``create_all`` and our lock acquisition is caught by the in-lock
    re-verify and refuses BEFORE any drop. Data is never silently lost. If the
    staging DB is busy with another writer the lock acquisition itself refuses and
    the superseded table and its rows are left completely untouched.
    """
    raw = engine.raw_connection()
    dbapi = raw.driver_connection
    try:
        cur = dbapi.cursor()
        cur.execute(f"PRAGMA busy_timeout={int(lock_timeout_ms)}")
        try:
            cur.execute("BEGIN IMMEDIATE")
        except Exception as exc:  # another writer holds the lock -> refuse closed
            raise SchemaRebuildError(
                f"refusing to retire superseded tables: could not acquire the "
                f"exclusive write lock (staging DB busy): {exc}"
            ) from exc

        survey: dict[str, int] = {}
        exported: Path | None = None
        try:
            # Re-verify the replacement schema on the SAME cursor that now holds
            # the write lock. A concurrent writer may have dropped a required D2
            # trigger (or table) between create_all and our lock acquisition;
            # catching it HERE -- under the lock, before any survey/export/drop --
            # means we refuse instead of retiring the old table against an
            # incomplete replacement schema. Any failure rolls back and preserves
            # the superseded table and its rows.
            verify_pilot2_schema(engine, cur=cur)
            present = [name for name in RETIRED_TABLES if _table_exists_on(cur, name)]
            if not present:
                dbapi.commit()
                return []
            survey = {
                name: int(cur.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
                for name in present
            }
            non_empty = {name: count for name, count in survey.items() if count}
            if non_empty:
                if not allow_data_loss:
                    raise SchemaRebuildError(
                        f"refusing to retire non-empty superseded tables {non_empty!r}: the "
                        "isolated staging rebuild never destroys data implicitly. Re-run with "
                        "allow_data_loss=True (CLI: --allow-retire-nonempty), which exports "
                        "every row to a JSON sidecar before dropping, or migrate the rows out "
                        "first."
                    )
                exported = _export_rows_from_cursor(
                    cur, sorted(non_empty), export_dir, engine
                )
            for name in sorted(survey):
                cur.execute(f'DROP TABLE IF EXISTS "{name}"')
            dbapi.commit()
        except Exception:
            dbapi.rollback()
            raise
    finally:
        raw.close()

    if exported is not None:
        print(f"[pilot2] superseded rows exported before retirement -> {exported}")
    print(f"[pilot2] superseded tables retired (isolated staging rebuild): {sorted(survey)}")
    return sorted(survey)


def run_create(
    engine,
    *,
    retire_superseded: bool = True,
    allow_data_loss: bool = False,
    export_dir: Path | None = None,
    lock_timeout_ms: int = 5000,
) -> list[str]:
    """Create the pilot2 schema, verify it, then retire the superseded shape."""
    from aios.pilot2.models import pilot2_metadata

    pilot2_metadata.create_all(engine)        # create any entirely-missing tables
    evolve_existing_tables(engine)            # ALTER existing tables up to merged shape
    verify_pilot2_schema(engine)             # tables + D2 triggers + COLUMN completeness
    if retire_superseded:
        retire_superseded_tables(
            engine,
            allow_data_loss=allow_data_loss,
            export_dir=export_dir,
            lock_timeout_ms=lock_timeout_ms,
        )
    return sorted(pilot2_metadata.tables.keys())


def seed_taxonomy(engine) -> int:
    """Populate the persisted content-taxonomy reference table (idempotent)."""
    from sqlmodel import Session, select

    from aios.pilot2.models import ContentTaxonomyTerm
    from aios.pilot2.vocabulary import (
        CONTENT_TAXONOMY,
        CONTENT_TAXONOMY_VERSION,
        TaxonomyDimension,
    )

    with Session(engine) as s:
        if s.exec(select(ContentTaxonomyTerm)).first() is not None:
            return len(s.exec(select(ContentTaxonomyTerm)).all())
        for dim in TaxonomyDimension:
            for value in CONTENT_TAXONOMY[dim]:
                s.add(
                    ContentTaxonomyTerm(
                        dimension=dim.value,
                        value=value,
                        version=CONTENT_TAXONOMY_VERSION,
                    )
                )
        s.commit()
        return len(s.exec(select(ContentTaxonomyTerm)).all())


def _refuse(reason: str) -> None:
    print(f"[pilot2] REFUSE (fail-closed): {reason}", file=sys.stderr)
    raise SystemExit(2)


def main() -> None:
    try:
        staging_path = resolve_authorized_staging_path()
    except StagingGuardError as exc:
        _refuse(str(exc))
        return

    if "AIOS_DATABASE_URL" not in os.environ:
        module_file = _authoritative_env_module_file()
        module = _load_env_module(module_file)
        bootstrap = getattr(module, "bootstrap", None)
        if bootstrap is None:
            _refuse("authoritative module exposes no bootstrap(); cannot set up env")
            return
        if str(module_file.parent) not in sys.path:
            sys.path.insert(0, str(module_file.parent))
        bootstrap()

    from aios.db import get_database_url, get_engine

    url = get_database_url()
    reason = authorization_error(url, staging_path=staging_path)
    if reason is not None:
        _refuse(f"{url!r}: {reason}")
        return

    engine = get_engine(url)
    # The ONLY way to authorise destroying rows in a superseded table. Absent
    # this flag the rebuild still succeeds -- it simply refuses the retirement
    # step and says so, leaving the old table (and its data) untouched.
    allow_data_loss = "--allow-retire-nonempty" in sys.argv[1:]
    try:
        tables = run_create(engine, allow_data_loss=allow_data_loss)
    except SchemaRebuildError as exc:
        _refuse(str(exc))
        return
    seeded = seed_taxonomy(engine)
    print(f"[pilot2] create_all OK on authorised staging DB {url!r}")
    print(f"[pilot2] tables ({len(tables)}): {tables}")
    print(f"[pilot2] taxonomy terms seeded: {seeded}")


if __name__ == "__main__":
    main()
