"""Verification that the DB-init optimization preserves migration correctness,
schema completeness, and test isolation while eliminating redundant Alembic runs.

These tests prove:
* a copied test DB is at the latest Alembic head revision;
* every expected table / index / trigger exists after a copy (vs the template
  produced by the real migration, and vs the declared SQLModel metadata);
* the template DB that copies are sourced from is a closed, single-file SQLite
  database (no WAL/SHM siblings), so copies are taken from a stable state;
* each copy is an independent file -- data written into one never appears in
  another, and a sibling test's writes never pollute a fresh test DB;
* and crucially, the *real* production ``run_migrations`` path still works -- it
  is NOT masked by the monkeypatch (control test).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, select

import aios.audit  # noqa: F401  (ensure audit tables are registered on metadata)
from aios.db import get_engine, run_migrations
from aios.models import Project


@pytest.fixture
def optimized_session(tmp_path: Path) -> Session:
    url = f"sqlite:///{tmp_path / 't.db'}"
    run_migrations(url)  # optimized path: copies the migrated template
    engine = get_engine(url)
    with Session(engine) as session:
        yield session


def test_optimized_db_is_at_latest_revision(tmp_path: Path, template_schema) -> None:
    """A copied test DB must report the current Alembic head revision."""
    import sqlite3

    url = f"sqlite:///{tmp_path / 't.db'}"
    run_migrations(url)
    with sqlite3.connect(str(tmp_path / "t.db")) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert version == template_schema["version"]


def test_copied_schema_matches_template(tmp_path: Path, template_schema) -> None:
    """After a copy, every expected table, index and trigger must be present and
    identical to what the real migration produced in the template."""
    import sqlite3

    url = f"sqlite:///{tmp_path / 't.db'}"
    run_migrations(url)
    with sqlite3.connect(str(tmp_path / "t.db")) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )
        }
        triggers = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
    assert tables == template_schema["tables"]
    assert indexes == template_schema["indexes"]
    assert triggers == template_schema["triggers"]


def test_copied_schema_includes_all_model_tables(tmp_path: Path) -> None:
    """The optimized DB must contain at least every table the models declare."""
    import sqlite3

    url = f"sqlite:///{tmp_path / 't.db'}"
    run_migrations(url)
    with sqlite3.connect(str(tmp_path / "t.db")) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = set(SQLModel.metadata.tables.keys())
    missing = expected - tables
    assert not missing, f"optimized DB is missing model tables: {sorted(missing)}"


def test_real_run_migrations_still_works(
    tmp_path: Path, real_run_migrations, template_schema
) -> None:
    """Control test: the genuine production migration path must still produce a
    correct DB. This call is NOT accelerated by the copy shim."""
    import sqlite3

    url = f"sqlite:///{tmp_path / 'real.db'}"
    real_run_migrations(url)  # real Alembic upgrade, no copy shortcut
    with sqlite3.connect(str(tmp_path / "real.db")) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert version == template_schema["version"]
    assert tables == template_schema["tables"]


def test_template_is_clean_single_file(template_db_path: Path) -> None:
    """The template DB that copies are sourced from must be a closed, single-file
    SQLite database (no open WAL/SHM siblings), so copies are taken from a stable
    state."""
    assert template_db_path.exists()
    assert template_db_path.is_file()
    for sibling in ("-wal", "-shm"):
        assert not template_db_path.with_name(template_db_path.name + sibling).exists()


def test_copied_databases_are_independent(tmp_path: Path) -> None:
    """Two separate copies must be distinct files; writing to one must not affect
    the other."""
    import sqlite3

    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    run_migrations(f"sqlite:///{db_a.as_posix()}")
    run_migrations(f"sqlite:///{db_b.as_posix()}")
    assert db_a.exists() and db_b.exists()
    # Write a marker only into A.
    with sqlite3.connect(str(db_a)) as conn:
        conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO marker (id) VALUES (1)")
        conn.commit()
    with sqlite3.connect(str(db_b)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='marker'"
        ).fetchall()
    assert rows == [], "copy B must not contain data written into copy A"


def test_isolation_writer_leaves_no_residue(optimized_session: Session) -> None:
    optimized_session.add(Project(name="writer-proj", objective="test objective"))
    optimized_session.commit()
    assert optimized_session.exec(select(Project)).first() is not None


def test_isolation_reader_sees_empty_database(optimized_session: Session) -> None:
    """Each test gets its own tmp_path -> its own copied DB, so a sibling test
    that inserted data cannot pollute this one."""
    assert optimized_session.exec(select(Project)).all() == []
