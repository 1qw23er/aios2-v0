from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aios.api.app import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    database_path = tmp_path / "api.db"
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    with TestClient(create_app()) as test_client:
        yield test_client


def create_project(client: TestClient, name: str = "Launch") -> dict:
    response = client.post("/projects", json={"name": name, "objective": "Ship V0"})
    assert response.status_code == 201
    return response.json()


def create_task(client: TestClient, project_id: str, title: str = "Research") -> dict:
    response = client.post(
        "/tasks",
        json={
            "project_id": project_id,
            "title": title,
            "description": f"{title} the topic",
        },
    )
    assert response.status_code == 201
    return response.json()


def test_create_project_generates_internal_fields(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={"id": "prj_client", "name": "Launch", "objective": "Ship V0"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["id"].startswith("prj_")
    assert payload["id"] != "prj_client"
    assert payload["status"] == "proposed"


def test_create_task_rejects_cross_project_dependency(client: TestClient) -> None:
    first = create_project(client, "First")
    second = create_project(client, "Second")
    dependency = create_task(client, first["id"])

    response = client.post(
        "/tasks",
        json={
            "project_id": second["id"],
            "title": "Plan",
            "description": "Plan the work",
            "depends_on": [dependency["id"]],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Dependency must belong to the same project"


def test_board_groups_tasks_and_lists_pending_approvals(client: TestClient) -> None:
    project = create_project(client)
    task = create_task(client, project["id"])
    approval_response = client.post(
        "/approvals",
        json={
            "project_id": project["id"],
            "task_id": task["id"],
            "action_type": "publish",
            "risk_level": "L4",
            "rationale": "Ready for review",
        },
    )
    assert approval_response.status_code == 201

    response = client.get(f"/projects/{project['id']}/board")

    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["id"] == project["id"]
    assert payload["tasks_by_status"]["backlog"][0]["id"] == task["id"]
    assert payload["pending_approvals"][0]["status"] == "pending"


def test_l4_submission_only_creates_pending_approval(client: TestClient) -> None:
    project = create_project(client)
    task = create_task(client, project["id"])

    response = client.post(
        "/approvals",
        json={
            "project_id": project["id"],
            "task_id": task["id"],
            "action_type": "publish",
            "risk_level": "L4",
            "rationale": "Ready for review",
            "status": "approved",
        },
    )

    assert response.status_code == 201
    assert response.json()["status"] == "pending"


def test_create_task_reports_missing_project(client: TestClient) -> None:
    response = client.post(
        "/tasks",
        json={"project_id": "prj_missing", "title": "Plan", "description": "Plan"},
    )
    assert response.status_code == 404
