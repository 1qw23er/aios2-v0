from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.api.app import create_app
from aios.db import get_engine, run_migrations
from aios.models import Event, Project
from aios.schemas import ProjectCreate
from aios.services import create_project


def test_domain_write_and_event_roll_back_together(tmp_path: Path, monkeypatch) -> None:
    url = f"sqlite:///{(tmp_path / 'rollback.db').as_posix()}"
    run_migrations(url)

    def fail_event_append(*_args, **_kwargs) -> None:
        raise RuntimeError("forced event failure")

    monkeypatch.setattr("aios.services.append_event", fail_event_append)
    with Session(get_engine(url)) as session:
        with pytest.raises(RuntimeError, match="forced event failure"):
            create_project(
                session,
                ProjectCreate(name="Launch", objective="Ship V0"),
                "create-project-rollback",
            )
        assert list(session.exec(select(Project))) == []
        assert list(session.exec(select(Event))) == []


def test_duplicate_idempotency_key_returns_existing_project(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "idempotency.db"
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    headers = {"Idempotency-Key": "create-project-1"}
    with TestClient(create_app()) as client:
        first = client.post(
            "/projects", headers=headers, json={"name": "Launch", "objective": "Ship V0"}
        )
        second = client.post(
            "/projects", headers=headers, json={"name": "Launch", "objective": "Ship V0"}
        )

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    with Session(get_engine(f"sqlite:///{database_path.as_posix()}")) as session:
        assert len(list(session.exec(select(Project)))) == 1
        assert len(list(session.exec(select(Event)))) == 1


def test_reused_idempotency_key_with_different_request_conflicts(
    tmp_path: Path, monkeypatch
) -> None:
    database_path = tmp_path / "conflict.db"
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    headers = {"Idempotency-Key": "create-project-1"}
    with TestClient(create_app()) as client:
        client.post("/projects", headers=headers, json={"name": "Launch", "objective": "Ship V0"})
        response = client.post(
            "/projects",
            headers=headers,
            json={"name": "Different", "objective": "Different"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Idempotency key conflicts with another request"
