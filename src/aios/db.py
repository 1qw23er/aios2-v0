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
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
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
