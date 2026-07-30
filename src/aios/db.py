from __future__ import annotations

import os
from collections.abc import Generator
from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from sqlalchemy import Engine, event
from sqlmodel import Session, create_engine

from alembic import command


def get_database_url() -> str:
    return os.getenv("AIOS_DATABASE_URL", "sqlite:///data/aios.db")


def _ensure_sqlite_parent(database_url: str) -> None:
    prefix = "sqlite:///"
    if database_url.startswith(prefix) and database_url != "sqlite:///:memory:":
        Path(database_url.removeprefix(prefix)).expanduser().parent.mkdir(
            parents=True, exist_ok=True
        )


@lru_cache
def get_engine(database_url: str) -> Engine:
    _ensure_sqlite_parent(database_url)
    # ``timeout`` is SQLite's busy-handler timeout (seconds): concurrent writers
    # that cannot acquire the RESERVED lock immediately (e.g. the BEGIN IMMEDIATE
    # serialization used by attest / V4 self-update) wait up to this long instead
    # of failing with "database is locked". ``check_same_thread=False`` allows the
    # FastAPI threadpool to share the connection.
    sqlite_connect_args = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(
        database_url,
        connect_args=sqlite_connect_args if database_url.startswith("sqlite") else {},
    )
    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def run_migrations(database_url: str | None = None) -> None:
    root = Path(__file__).resolve().parents[2]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url or get_database_url())
    command.upgrade(config, "head")


def get_session() -> Generator[Session, None, None]:
    with Session(get_engine(get_database_url())) as session:
        yield session


def make_session() -> Session:
    """Return a standalone SQLAlchemy ``Session`` bound to the default engine.

    Unlike ``get_session`` (a FastAPI generator dependency meant for the request
    lifecycle), this is a plain factory for use outside request handling -- e.g.
    the encrypted secret store opens its own store-owned transactions via this
    so its commits are independent of the caller's transaction (issue #103 §4.5).
    """
    return Session(get_engine(get_database_url()))
