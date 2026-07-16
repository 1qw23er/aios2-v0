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


def test_complete_requires_idempotency_key(client: TestClient) -> None:
    project = create_project(client)
    task = create_task(client, project["id"])
    response = client.post(f"/tasks/{task['id']}/complete")
    # Missing required Idempotency-Key header is rejected (FastAPI 422).
    assert response.status_code == 422


def test_process_rejects_out_of_bounds_limit(client: TestClient) -> None:
    for bad in (0, 101, -5):
        response = client.post("/orchestrator/process", params={"limit": bad})
        assert response.status_code == 422, bad


def test_process_rejects_non_integer_limit(client: TestClient) -> None:
    response = client.post("/orchestrator/process", params={"limit": "abc"})
    assert response.status_code == 422


def test_different_key_repeat_completion_creates_distinct_events(
    client: TestClient, tmp_path: Path
) -> None:
    project = create_project(client)
    task = create_task(client, project["id"])

    first = client.post(
        f"/tasks/{task['id']}/complete", headers={"Idempotency-Key": "idem-A"}
    )
    second = client.post(
        f"/tasks/{task['id']}/complete", headers={"Idempotency-Key": "idem-B"}
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == "done"
    assert second.json()["status"] == "done"

    # Distinct idempotency keys must NOT silently collapse: each produces its
    # own task.completed event even when completing the same already-done task.
    url = f"sqlite:///{(tmp_path / 'orchestrator_api.db').as_posix()}"
    with Session(get_engine(url)) as session:
        completed = list(
            session.exec(select(Event).where(Event.type == "task.completed"))
        )
    assert len(completed) == 2


def test_process_response_schema_and_status(client: TestClient) -> None:
    project = create_project(client)
    source = create_task(client, project["id"], title="Research")
    create_task(client, project["id"], title="Draft", depends_on=[source["id"]])

    client.post(
        f"/tasks/{source['id']}/complete", headers={"Idempotency-Key": "idem-src"}
    )
    process = client.post("/orchestrator/process")
    assert process.status_code == 200
    body = process.json()
    assert set(body.keys()) == {"processed_events", "activated_task_ids"}
    assert isinstance(body["processed_events"], int)
    assert isinstance(body["activated_task_ids"], list)
    assert all(isinstance(tid, str) for tid in body["activated_task_ids"])


def test_process_activation_is_invocation_scoped(client: TestClient) -> None:
    project = create_project(client)
    source_a = create_task(client, project["id"], title="Research A")
    down_a = create_task(
        client, project["id"], title="Draft A", depends_on=[source_a["id"]]
    )
    source_b = create_task(client, project["id"], title="Research B")
    down_b = create_task(
        client, project["id"], title="Draft B", depends_on=[source_b["id"]]
    )

    client.post(f"/tasks/{source_a['id']}/complete", headers={"Idempotency-Key": "idem-a"})
    client.post(f"/tasks/{source_b['id']}/complete", headers={"Idempotency-Key": "idem-b"})

    # limit=1 processes exactly one source event and must report only the single
    # task that THIS invocation activated (not a global READY-set diff).
    first = client.post("/orchestrator/process", params={"limit": 1}).json()
    assert first["processed_events"] == 1
    assert len(first["activated_task_ids"]) == 1
    activated = first["activated_task_ids"][0]
    assert activated in {down_a["id"], down_b["id"]}

    second = client.post("/orchestrator/process", params={"limit": 1}).json()
    assert second["processed_events"] == 1
    other = (down_a["id"], down_b["id"])
    assert second["activated_task_ids"] == [tid for tid in other if tid != activated]


def test_process_zero_pending_events_reports_empty_activation(client: TestClient) -> None:
    # Core concurrency-safety property (directly answers: a request that
    # processes zero events cannot return activations created by another).
    project = create_project(client)
    source = create_task(client, project["id"], title="Research")
    downstream = create_task(
        client, project["id"], title="Draft", depends_on=[source["id"]]
    )

    client.post(f"/tasks/{source['id']}/complete", headers={"Idempotency-Key": "idem-src"})
    first = client.post("/orchestrator/process").json()
    assert first["processed_events"] == 1
    assert first["activated_task_ids"] == [downstream["id"]]

    # downstream is now READY, created by the first invocation. The second
    # invocation has zero pending events. Even though downstream is READY, it
    # must NOT appear: activated_task_ids is derived only from this invocation's
    # own activations, never a global READY-set snapshot. So a concurrent loser
    # that processed nothing returns an empty list.
    second = client.post("/orchestrator/process").json()
    assert second["processed_events"] == 0
    assert second["activated_task_ids"] == []


def test_process_does_not_attribute_unrelated_ready_task(client: TestClient) -> None:
    # Invocation-scoping vs a global READY diff: a task already READY from a
    # *prior, different* invocation must never leak into a later response.
    project = create_project(client)
    source1 = create_task(client, project["id"], title="Research 1")
    down1 = create_task(
        client, project["id"], title="Draft 1", depends_on=[source1["id"]]
    )
    source2 = create_task(client, project["id"], title="Research 2")
    down2 = create_task(
        client, project["id"], title="Draft 2", depends_on=[source2["id"]]
    )

    client.post(f"/tasks/{source2['id']}/complete", headers={"Idempotency-Key": "idem-s2"})
    prior = client.post("/orchestrator/process").json()
    assert prior["activated_task_ids"] == [down2["id"]]

    client.post(f"/tasks/{source1['id']}/complete", headers={"Idempotency-Key": "idem-s1"})
    current = client.post("/orchestrator/process").json()
    assert current["processed_events"] == 1
    assert current["activated_task_ids"] == [down1["id"]]
    # down2 was already READY from the prior invocation and must not reappear.
    assert down2["id"] not in current["activated_task_ids"]


def test_process_pending_detailed_returns_own_activations(
    client: TestClient, tmp_path: Path
) -> None:
    # Locks the mechanism that makes the endpoint strictly attributable:
    # process_pending(return_detailed=True) returns exactly the tasks THIS call
    # moved to READY -- which is what the endpoint now reports.
    from aios.db import get_engine
    from aios.orchestrator import Orchestrator

    project = create_project(client)
    source = create_task(client, project["id"], title="Research")
    downstream = create_task(
        client, project["id"], title="Draft", depends_on=[source["id"]]
    )
    client.post(f"/tasks/{source['id']}/complete", headers={"Idempotency-Key": "idem-d"})

    url = f"sqlite:///{(tmp_path / 'orchestrator_api.db').as_posix()}"
    with Session(get_engine(url)) as session:
        result = Orchestrator(session).process_pending(limit=10, return_detailed=True)
    assert len(result.events) == 1
    assert result.activated_task_ids == [downstream["id"]]
