from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.api.app import create_app
from aios.db import get_engine
from aios.models import Event


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    database_path = tmp_path / "orchestrator_api.db"
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    with TestClient(create_app()) as test_client:
        yield test_client


def create_project(client: TestClient, name: str = "Launch") -> dict:
    response = client.post("/projects", json={"name": name, "objective": "Ship V0"})
    assert response.status_code == 201
    return response.json()


def create_task(
    client: TestClient,
    project_id: str,
    title: str = "Research",
    **extra: object,
) -> dict:
    body = {
        "project_id": project_id,
        "title": title,
        "description": f"{title} the topic",
    }
    body.update(extra)  # type: ignore[arg-type]
    response = client.post("/tasks", json=body)
    assert response.status_code == 201
    return response.json()


def test_complete_task_endpoint_marks_done_and_is_idempotent(
    client: TestClient, tmp_path: Path
) -> None:
    project = create_project(client)
    task = create_task(client, project["id"])

    first = client.post(
        f"/tasks/{task['id']}/complete", headers={"Idempotency-Key": "idem-complete-1"}
    )
    assert first.status_code == 200
    assert first.json()["status"] == "done"

    second = client.post(
        f"/tasks/{task['id']}/complete", headers={"Idempotency-Key": "idem-complete-1"}
    )
    assert second.status_code == 200
    assert second.json()["status"] == "done"

    url = f"sqlite:///{(tmp_path / 'orchestrator_api.db').as_posix()}"
    with Session(get_engine(url)) as session:
        completed = list(session.exec(select(Event).where(Event.type == "task.completed")))
    assert len(completed) == 1


def test_complete_missing_task_returns_404(client: TestClient) -> None:
    response = client.post(
        "/tasks/tsk_missing/complete", headers={"Idempotency-Key": "idem-x"}
    )
    assert response.status_code == 404


def test_orchestrator_process_activates_ready_task(client: TestClient) -> None:
    project = create_project(client)
    source = create_task(client, project["id"], title="Research")
    downstream = create_task(
        client, project["id"], title="Draft", depends_on=[source["id"]]
    )

    complete = client.post(
        f"/tasks/{source['id']}/complete", headers={"Idempotency-Key": "idem-source"}
    )
    assert complete.status_code == 200

    process = client.post("/orchestrator/process")
    assert process.status_code == 200
    body = process.json()
    assert body["processed_events"] >= 1
    assert downstream["id"] in body["activated_task_ids"]

    board = client.get(f"/projects/{project['id']}/board").json()
    ready_ids = [task["id"] for task in board["tasks_by_status"]["ready"]]
    assert downstream["id"] in ready_ids


def test_orchestrator_process_is_idempotent(client: TestClient) -> None:
    project = create_project(client)
    source = create_task(client, project["id"], title="Research")
    downstream = create_task(
        client, project["id"], title="Draft", depends_on=[source["id"]]
    )

    client.post(
        f"/tasks/{source['id']}/complete", headers={"Idempotency-Key": "idem-source-2"}
    )
    first = client.post("/orchestrator/process").json()
    assert downstream["id"] in first["activated_task_ids"]

    second = client.post("/orchestrator/process").json()
    assert second["activated_task_ids"] == []
    assert second["processed_events"] == 0
