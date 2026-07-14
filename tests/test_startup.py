from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from aios.api.app import create_app


def test_startup_applies_migrations(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "startup.db"
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")

    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"alembic_version", "project", "task", "event"} <= tables
