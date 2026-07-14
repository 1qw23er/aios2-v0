from pathlib import Path

from fastapi.testclient import TestClient

from aios.api.app import create_app


def test_empty_board_has_every_task_status(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "empty-board.db"
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    with TestClient(create_app()) as client:
        project = client.post("/projects", json={"name": "Launch", "objective": "Ship V0"}).json()
        response = client.get(f"/projects/{project['id']}/board")

    assert response.status_code == 200
    assert set(response.json()["tasks_by_status"]) == {
        "backlog",
        "ready",
        "running",
        "waiting_external",
        "review",
        "approved",
        "rejected",
        "done",
        "failed",
    }
