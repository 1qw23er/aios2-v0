from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.api.app import create_app
from aios.db import get_database_url, get_engine
from aios.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    Event,
    RoutingMode,
    TaskStatus,
)
from aios.models import (
    Task as TaskModel,
)
from aios.services import (
    ServiceError,
    decide_approval,
    ensure_pending_approval,
    request_revision,
    set_artifact_review_status,
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    database_path = tmp_path / "owner_approval.db"
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    with TestClient(create_app(), follow_redirects=False) as test_client:
        yield test_client


def _launch(client: TestClient) -> None:
    client.post("/owner/launch", data={"name": "审批测试", "objective": "验证 owner 闭环"})


def _gate_task() -> tuple[str, str]:
    """Return (task_id, project_id) of the first MANUAL-gated task (T6 / T8)."""
    with Session(get_engine(get_database_url())) as session:
        task = session.exec(
            select(TaskModel).where(TaskModel.routing_mode == RoutingMode.MANUAL)
        ).first()
        assert task is not None
        return task.id, task.project_id


def _downstream_ids(project_id: str, source_task_id: str) -> list[str]:
    with Session(get_engine(get_database_url())) as session:
        tasks = session.exec(select(TaskModel).where(TaskModel.project_id == project_id)).all()
        return [t.id for t in tasks if source_task_id in t.depends_on]


# --- service layer (decision + revision + audit) ---


def test_decide_approve_unlocks_downstream(client: TestClient) -> None:
    _launch(client)
    task_id, project_id = _gate_task()
    with Session(get_engine(get_database_url())) as session:
        approval = ensure_pending_approval(
            session, project_id=project_id, task_id=task_id, action_type="owner_gate"
        )
        decide_approval(session, approval.id, ApprovalStatus.APPROVED)
        session.expire_all()
        assert session.get(TaskModel, task_id).status == TaskStatus.DONE
        # Downstream tasks (T7 / T9) that depend on the gate become READY.
        downstream = _downstream_ids(project_id, task_id)
        assert downstream, "expected downstream tasks depending on the gate"
        tasks = session.exec(select(TaskModel).where(TaskModel.project_id == project_id)).all()
        by_id = {t.id: t for t in tasks}
        assert all(by_id[tid].status == TaskStatus.READY for tid in downstream)
        # The decision is durably recorded.
        assert session.get(Approval, approval.id).status == ApprovalStatus.APPROVED


def test_decide_reject_returns_task_to_review(client: TestClient) -> None:
    _launch(client)
    task_id, project_id = _gate_task()
    with Session(get_engine(get_database_url())) as session:
        approval = ensure_pending_approval(
            session, project_id=project_id, task_id=task_id, action_type="owner_gate"
        )
        decide_approval(session, approval.id, ApprovalStatus.REJECTED, rationale="需要补充数据")
        session.expire_all()
        assert session.get(TaskModel, task_id).status == TaskStatus.REVIEW
        assert session.get(Approval, approval.id).status == ApprovalStatus.REJECTED


def test_decide_twice_conflicts(client: TestClient) -> None:
    _launch(client)
    task_id, project_id = _gate_task()
    with Session(get_engine(get_database_url())) as session:
        approval = ensure_pending_approval(
            session, project_id=project_id, task_id=task_id, action_type="owner_gate"
        )
        decide_approval(session, approval.id, ApprovalStatus.APPROVED)
        with pytest.raises(ServiceError):
            decide_approval(session, approval.id, ApprovalStatus.APPROVED)


def test_request_revision_records_feedback(client: TestClient) -> None:
    _launch(client)
    task_id, project_id = _gate_task()
    with Session(get_engine(get_database_url())) as session:
        request_revision(session, task_id, "标题要更抓人")
        session.expire_all()
        assert session.get(TaskModel, task_id).status == TaskStatus.REVIEW
        event = session.exec(
            select(Event).where(Event.task_id == task_id, Event.type == "task.revision")
        ).first()
        assert event is not None
        assert event.payload["feedback"] == "标题要更抓人"


def test_set_artifact_review_status(client: TestClient) -> None:
    _launch(client)
    task_id, project_id = _gate_task()
    with Session(get_engine(get_database_url())) as session:
        artifact = Artifact(
            project_id=project_id,
            task_id=task_id,
            type=ArtifactType.MARKDOWN,
            uri="x",
            checksum="y",
        )
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        set_artifact_review_status(session, artifact.id, ArtifactReviewStatus.APPROVED)
        session.expire_all()
        assert session.get(Artifact, artifact.id).review_status == ArtifactReviewStatus.APPROVED


# --- console UI flow (no curl / SQL / Python in the owner path) ---


def test_console_approve_redirects_to_board_and_unlocks(client: TestClient) -> None:
    _launch(client)
    task_id, project_id = _gate_task()
    resp = client.post(f"/owner/tasks/{task_id}/decide", data={"decision": "approve"})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/owner/board/{project_id}"
    with Session(get_engine(get_database_url())) as session:
        assert session.get(TaskModel, task_id).status == TaskStatus.DONE
        downstream = _downstream_ids(project_id, task_id)
        tasks = session.exec(select(TaskModel).where(TaskModel.project_id == project_id)).all()
        by_id = {t.id: t for t in tasks}
        assert all(by_id[tid].status == TaskStatus.READY for tid in downstream)


def test_console_reject_redirects_and_marks_review(client: TestClient) -> None:
    _launch(client)
    task_id, project_id = _gate_task()
    resp = client.post(
        f"/owner/tasks/{task_id}/decide", data={"decision": "reject", "rationale": "改"}
    )
    assert resp.status_code == 303
    with Session(get_engine(get_database_url())) as session:
        assert session.get(TaskModel, task_id).status == TaskStatus.REVIEW


def test_console_revision_redirects(client: TestClient) -> None:
    _launch(client)
    task_id, project_id = _gate_task()
    resp = client.post(
        f"/owner/tasks/{task_id}/revision", data={"feedback": "修订意见"}
    )
    assert resp.status_code == 303
    with Session(get_engine(get_database_url())) as session:
        assert session.get(TaskModel, task_id).status == TaskStatus.REVIEW


def test_console_missing_decision_is_readable_400(client: TestClient) -> None:
    _launch(client)
    task_id, _ = _gate_task()
    resp = client.post(f"/owner/tasks/{task_id}/decide", data={})
    assert resp.status_code == 400
    assert "批准" in resp.text  # readable Chinese, not a stack trace


def test_console_unknown_task_is_readable_404(client: TestClient) -> None:
    resp = client.post("/owner/tasks/does-not-exist/decide", data={"decision": "approve"})
    assert resp.status_code == 404
    assert "未找到" in resp.text  # readable HTML, no stack trace
