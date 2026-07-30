from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aios.api.app import create_app
from aios.secrets_store import SecretStoreMisconfigured


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


def test_startup_rejects_encrypted_db_without_kek(tmp_path: Path, monkeypatch) -> None:
    # Selecting the encrypted_db backend without a valid KEK must crash at boot
    # with a readable misconfiguration error, not serve silent 503s. (#103)
    database_path = tmp_path / "startup-bad.db"
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    monkeypatch.setenv("AIOS_SECRET_STORE_BACKEND", "encrypted_db")
    monkeypatch.delenv("AIOS_SECRET_MASTER_KEY", raising=False)

    with pytest.raises(SecretStoreMisconfigured), TestClient(create_app()):
        pass


def test_startup_rejects_unknown_backend(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "startup-unknown.db"
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    monkeypatch.setenv("AIOS_SECRET_STORE_BACKEND", "vault")

    with pytest.raises(SecretStoreMisconfigured), TestClient(create_app()):
        pass

