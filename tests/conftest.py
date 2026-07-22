"""Pytest bootstrap: build a migrated *template* database once per session, then
make test ``run_migrations`` calls copy that template instead of re-running the
full Alembic upgrade.

This is a pure test-harness optimization. It does NOT modify any production
code, domain model, or migration file.

Safety / determinism guarantees
-------------------------------
* The template is built with the *real* Alembic ``upgrade head`` -- migrations
  genuinely run, exactly once.
* The monkeypatch of ``alembic.command.upgrade`` is *guarded*: it only diverts a
  migration to a copy when ALL of the following hold:
    - the target URL is SQLite,
    - the target file lives under the system temp directory (i.e. a pytest
      temporary database), and
    - the template database has been prepared.
  Any other call -- a non-SQLite backend, a repo-local DB such as
  ``data/aios.db``, or a call made before the template is ready -- falls straight
  through to the real Alembic upgrade, so production-style paths are never
  silently masked.
* After every copy we verify the new database: its ``alembic_version`` equals the
  current head and its tables / indexes / triggers exactly match the template that
  real Alembic produced.

Why patch ``alembic.command.upgrade`` (rather than each fixture):
``aios.db.run_migrations`` calls ``alembic.command.upgrade(config, "head")`` and
the alias is resolved at *call time*, so replacing the attribute on the
``alembic.command`` module intercepts every caller uniformly -- the service
tests that call ``run_migrations(url)`` directly, the FastAPI ``lifespan`` that
calls ``run_migrations()``, and any module that imported ``run_migrations`` by
name.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

import alembic.command
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from aios.actor import ActorContext
from aios.api.security import authenticate_owner

# Populated in ``pytest_sessionstart``; consulted by the upgrade shim.
TEMPLATE_DB_PATH: Path | None = None
_REAL_UPGRADE = None
_HEAD_REVISION: str | None = None
_TEMPLATE_SCHEMA: dict[str, object] | None = None


def _sqlite_file_path(database_url: str) -> Path:
    """Map a SQLAlchemy URL to its on-disk SQLite file path (``Path()`` for non-file DBs)."""
    database = make_url(database_url).database
    if database in (":memory:", "", None):
        return Path()
    path = Path(database)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _has_alembic_version(path: Path) -> bool:
    try:
        with sqlite3.connect(str(path)) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
            ).fetchall()
        return bool(rows)
    except sqlite3.Error:
        return False


def _current_revision(path: Path) -> str | None:
    try:
        with sqlite3.connect(str(path)) as conn:
            return conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    except sqlite3.Error:
        return None


def _introspect(path: Path) -> dict[str, object]:
    with sqlite3.connect(str(path)) as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
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
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    return {"tables": tables, "indexes": indexes, "triggers": triggers, "version": version}


def _verify_copied_database(path: Path) -> None:
    """Assert a freshly copied test DB is consistent with the migrated template."""
    schema = _introspect(path)
    assert schema["version"] == _HEAD_REVISION, (
        f"alembic_version {schema['version']!r} != head {_HEAD_REVISION!r}"
    )
    tpl = _TEMPLATE_SCHEMA or {}
    assert schema["tables"] == tpl.get("tables", set()), (
        f"table mismatch: {schema['tables'] ^ tpl.get('tables', set())}"
    )
    assert schema["indexes"] == tpl.get("indexes", set()), "index mismatch vs template"
    assert schema["triggers"] == tpl.get("triggers", set()), "trigger mismatch vs template"


def _prepare_template() -> Path:
    """Build a single migrated template DB with the REAL Alembic upgrade."""
    import aios.db  # local import keeps collection lightweight

    template_dir = Path(tempfile.mkdtemp(prefix="aios_pytest_template_"))
    template_path = template_dir / "template.db"
    template_url = f"sqlite:///{template_path.as_posix()}"
    # Real migrations run here, exactly once.
    aios.db.run_migrations(template_url)
    # Ensure no lingering WAL/SHM siblings and that the file is a clean, closed
    # single-file database before any copy happens.
    for sibling in ("-wal", "-shm"):
        candidate = template_path.with_name(template_path.name + sibling)
        if candidate.exists():
            candidate.unlink()
    return template_path


def pytest_sessionstart(session) -> None:  # noqa: D401 - pytest hook
    global TEMPLATE_DB_PATH, _REAL_UPGRADE, _HEAD_REVISION, _TEMPLATE_SCHEMA
    root = Path(session.config.rootdir)
    # Current Alembic head revision (read once; immutable for the session).
    cfg = Config(root / "alembic.ini")
    cfg.set_main_option("script_location", str(root / "alembic"))
    _HEAD_REVISION = ScriptDirectory.from_config(cfg).get_current_head()

    template_path = _prepare_template()
    TEMPLATE_DB_PATH = template_path
    _TEMPLATE_SCHEMA = _introspect(template_path)

    # Install the guarded shim (real upgrade is preserved for fall-back).
    _REAL_UPGRADE = alembic.command.upgrade
    temp_root = Path(tempfile.gettempdir()).resolve()

    def _copy_template_upgrade(config: Config, revision, **kwargs) -> None:
        url = config.get_main_option("sqlalchemy.url")
        # Guard: only divert SQLite test databases under the system temp dir,
        # and only once the template is ready. Otherwise use the real Alembic
        # upgrade so production-style paths are never silently masked.
        target = _sqlite_file_path(url)
        # Only divert the *standard* "migrate to head" path. Any explicit
        # non-head revision (e.g. downgrade / round-trip tests that drive
        # Alembic to an intermediate version) must run the real Alembic
        # engine so migration semantics are never masked.
        if (
            revision != "head"
            or target == Path()
            or make_url(url).get_backend_name() != "sqlite"
            or TEMPLATE_DB_PATH is None
            or not TEMPLATE_DB_PATH.exists()
            or not target.resolve().is_relative_to(temp_root)
        ):
            _REAL_UPGRADE(config, revision, **kwargs)
            return
        # revision == "head", SQLite temp DB, template ready.
        # An existing DB already at head is a no-op (mirrors a repeated
        # ``alembic upgrade head``); leave it untouched. An existing DB at an
        # *older* revision must be genuinely upgraded to head by Alembic so
        # intermediate-version tests (e.g. downgrade round-trips) keep working.
        if target.exists() and _has_alembic_version(target):
            if _current_revision(target) == _HEAD_REVISION:
                return
            _REAL_UPGRADE(config, revision, **kwargs)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TEMPLATE_DB_PATH, target)
        _verify_copied_database(target)

    alembic.command.upgrade = _copy_template_upgrade


def pytest_sessionfinish(session) -> None:  # noqa: D401 - pytest hook
    global TEMPLATE_DB_PATH, _REAL_UPGRADE, _HEAD_REVISION, _TEMPLATE_SCHEMA
    if _REAL_UPGRADE is not None:
        alembic.command.upgrade = _REAL_UPGRADE
        _REAL_UPGRADE = None
    TEMPLATE_DB_PATH = None
    _HEAD_REVISION = None
    _TEMPLATE_SCHEMA = None


@pytest.fixture
def real_run_migrations():
    """Run a migration via the genuine Alembic path (bypassing the copy shim).

    Used by control tests that prove the production migration still works.
    """
    import aios.db

    def _run(database_url: str) -> None:
        saved = alembic.command.upgrade
        alembic.command.upgrade = _REAL_UPGRADE
        try:
            aios.db.run_migrations(database_url)
        finally:
            alembic.command.upgrade = saved

    return _run


@pytest.fixture
def template_schema() -> dict[str, object]:
    """Ground-truth schema of the migrated template produced by the real Alembic
    upgrade: tables / indexes / triggers / version."""
    assert _TEMPLATE_SCHEMA is not None
    return _TEMPLATE_SCHEMA


@pytest.fixture
def template_db_path() -> Path:
    """Path of the migrated template DB (used for stability assertions)."""
    assert TEMPLATE_DB_PATH is not None
    return TEMPLATE_DB_PATH


def _trusted_owner_actor() -> ActorContext:
    """Test-only substitute for an authenticated owner.

    Never installed by production code. Tests install it via
    ``app.dependency_overrides[authenticate_owner]`` on the *specific* app they
    build, so the override cannot leak to other apps or other tests.
    """
    return ActorContext(kind="owner", owner_id="owner")


@pytest.fixture
def trusted_owner_installer():
    """Install the test-only trusted-owner override on a caller-built app.

    Old business tests build their own app inline (after setting
    ``AIOS_DATABASE_URL`` and friends), so they call this to make *that* app accept
    a trusted owner. The override is set only on the app object passed in -- it
    never touches the production ``authenticate_owner`` function and never rebinds
    any imported reference. The app is discarded at test end, so the override dies
    with it; callers may also ``pop`` it themselves for explicit teardown.
    """
    def _install(app: FastAPI) -> None:
        app.dependency_overrides[authenticate_owner] = _trusted_owner_actor

    return _install


@pytest.fixture
def authenticated_app(tmp_path: Path, monkeypatch) -> FastAPI:
    """An app whose ``authenticate_owner`` dependency yields a trusted owner.

    For non-auth tests that merely need an authenticated owner context. The
    override lives only on this app object and is cleared on teardown.
    """
    monkeypatch.setenv(
        "AIOS_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'authenticated_app.db').as_posix()}",
    )
    monkeypatch.delenv("AIOS_AGENT_API_KEY", raising=False)
    from aios.api.app import create_app

    app = create_app()
    app.dependency_overrides[authenticate_owner] = _trusted_owner_actor
    yield app
    app.dependency_overrides.pop(authenticate_owner, None)


@pytest.fixture
def authenticated_client(authenticated_app: FastAPI) -> TestClient:
    with TestClient(authenticated_app, follow_redirects=False) as client:
        yield client


@pytest.fixture
def owner_auth_app(tmp_path: Path, monkeypatch) -> FastAPI:
    """An app exercising the REAL ``authenticate_owner`` dependency (no override).

    Used by the owner-auth contract suite so misconfigured / unauthenticated calls
    are rejected exactly as in production, and so the owner-surface inventory test
    fails loudly if any route forgets ``authenticate_owner``.
    """
    monkeypatch.setenv(
        "AIOS_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'owner_auth_app.db').as_posix()}",
    )
    monkeypatch.delenv("AIOS_AGENT_API_KEY", raising=False)
    from aios.api.app import create_app

    app = create_app()
    app.dependency_overrides.pop(authenticate_owner, None)
    yield app
    app.dependency_overrides.pop(authenticate_owner, None)


@pytest.fixture
def owner_auth_client(owner_auth_app: FastAPI) -> TestClient:
    with TestClient(owner_auth_app, follow_redirects=False) as client:
        yield client
