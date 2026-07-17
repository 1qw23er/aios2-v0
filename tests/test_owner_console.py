from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.api.app import create_app
from aios.campaign import V1_TASKS
from aios.db import get_database_url, get_engine
from aios.models import Project as ProjectModel
from aios.models import RoutingMode
from aios.models import Task as TaskModel


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    database_path = tmp_path / "owner_console.db"
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    with TestClient(create_app(), follow_redirects=False) as test_client:
        yield test_client


def _count_projects_and_tasks() -> tuple[int, int]:
    with Session(get_engine(get_database_url())) as session:
        projects = len(session.exec(select(ProjectModel)).all())
        tasks = len(session.exec(select(TaskModel)).all())
    return projects, tasks


def _launch_form(
    client: TestClient, name: str, objective: str, idem: str | None = None
) -> TestClient:
    data = {"name": name, "objective": objective}
    if idem is not None:
        data["idem"] = idem
    return client.post("/owner/launch", data=data)


def _set_task_routing_mode(project_id: str, title: str, mode: RoutingMode) -> None:
    """Flip a persisted task's routing_mode (test seam for constraint #7)."""
    with Session(get_engine(get_database_url())) as session:
        task = session.exec(
            select(TaskModel).where(
                TaskModel.project_id == project_id, TaskModel.title == title
            )
        ).first()
        assert task is not None, f"task {title!r} not found in project {project_id}"
        task.routing_mode = mode
        session.add(task)
        session.commit()


def _raise_runtime_error(*_args, **_kwargs) -> None:
    """Simulated unexpected server failure (test seam for constraint #8)."""
    raise RuntimeError("simulated unexpected failure")


# 1. Owner can load the launch page.
def test_owner_can_load_launch_page(client: TestClient) -> None:
    resp = client.get("/owner")
    assert resp.status_code == 200
    text = resp.text
    assert "启动工作流" in text
    assert 'name="name"' in text
    assert 'name="objective"' in text
    # A fresh idempotency key is embedded so retries/double-clicks are safe.
    assert 'name="idem"' in text


# 2. Valid form submission creates exactly one Project and nine Tasks.
def test_valid_submission_creates_one_project_and_nine_tasks(client: TestClient) -> None:
    resp = _launch_form(
        client,
        "把失败的 AI 系统重建成可用的小 AI 公司",
        "验证 AIOS 的端到端多 agent 协作。",
    )
    assert resp.status_code == 303, resp.text
    assert "/owner/board/" in resp.headers["location"]

    projects, tasks = _count_projects_and_tasks()
    assert projects == 1
    assert tasks == 9


# 3. Repeated submission caused by a double click does not duplicate the campaign.
def test_double_click_does_not_duplicate_campaign(client: TestClient) -> None:
    first = _launch_form(
        client,
        "同一目标 campaign",
        "双击测试。",
        idem="double-click-key",
    )
    second = _launch_form(
        client,
        "同一目标 campaign",
        "双击测试。",
        idem="double-click-key",
    )
    assert first.status_code == 303
    assert second.status_code == 303
    # Both submissions must resolve to the same campaign board.
    assert first.headers["location"] == second.headers["location"]

    projects, tasks = _count_projects_and_tasks()
    assert projects == 1, "double-click must not create a second project"
    assert tasks == 9


# 4. Empty name or objective produces a readable Chinese validation message.
@pytest.mark.parametrize(
    "payload",
    [{"name": "", "objective": "有目标"}, {"name": "有名称", "objective": ""}],
)
def test_empty_fields_show_chinese_message(client: TestClient, payload: dict) -> None:
    resp = client.post("/owner/launch", data=payload)
    assert resp.status_code == 400
    assert "不能" in resp.text
    assert "Traceback" not in resp.text
    assert "{" not in resp.text.lstrip()[:1], "must not leak raw JSON"

    projects, _ = _count_projects_and_tasks()
    assert projects == 0, "validation failure must not create a project"


# 5. Campaign board renders T1-T9 and their statuses correctly.
def test_board_renders_t1_t9_and_statuses(client: TestClient) -> None:
    launch = _launch_form(client, "看板渲染测试", "渲染 T1-T9。")
    board_url = launch.headers["location"]
    board = client.get(board_url)
    assert board.status_code == 200
    text = board.text
    for task_def in V1_TASKS:
        assert task_def["title"] in text
    # T1 is kicked off and should be ready on the board.
    assert "已就绪" in text


# 6. T6 and T8 are visibly marked as waiting for owner action.
def test_board_marks_t6_t8_owner_action(client: TestClient) -> None:
    launch = _launch_form(client, "闸门标记测试", "标记 T6/T8。")
    board = client.get(launch.headers["location"])
    text = board.text
    assert "需你处理 / 审批" in text
    # The two owner-gate tasks (T6, T8) carry the gate marker both in the board
    # table row and in the actionable "需要你处理的任务" block below it.
    assert text.count('badge-gate">需你处理 / 审批') == 4


# 7. Unknown project produces a readable not-found page.
def test_unknown_project_shows_not_found(client: TestClient) -> None:
    resp = client.get("/owner/board/does-not-exist")
    assert resp.status_code == 404
    assert "未找到" in resp.text
    assert "Traceback" not in resp.text
    assert resp.text.lstrip()[:1] != "{", "must be HTML, not a JSON error body"


# 8. No direct database manipulation is required in the owner flow.
def test_no_direct_db_manipulation_in_owner_flow(client: TestClient) -> None:
    # The entire owner journey is HTTP-only: load form -> submit -> view board.
    home = client.get("/owner")
    assert home.status_code == 200
    launch = _launch_form(client, "纯 HTTP 流程", "通过服务层完成。")
    assert launch.status_code == 303
    board = client.get(launch.headers["location"])
    assert board.status_code == 200

    # The same data is observable via the JSON service endpoint, proving the
    # console went through the shared service layer (get_board) and not a
    # hidden direct-write path.
    project_id = launch.headers["location"].rsplit("/", 1)[-1]
    api_board = client.get(f"/owner/campaigns/{project_id}")
    assert api_board.status_code == 200
    total = sum(len(v) for v in api_board.json()["tasks_by_status"].values())
    assert total == 9


# 3b. Validation redisplay preserves the SAME idempotency key (no new key minted).
def test_validation_redisplay_preserves_idem_key(client: TestClient) -> None:
    resp = client.post(
        "/owner/launch",
        data={"name": "", "objective": "有目标", "idem": "fixed-lifecycle-key"},
    )
    assert resp.status_code == 400
    # The redisplayed form must echo the submitted key, not generate a new one.
    assert 'name="idem" value="fixed-lifecycle-key"' in resp.text
    assert "Traceback" not in resp.text


# 7b. Owner-gate badge is derived from the PERSISTED routing_mode, not V1_TASKS.
def test_gate_badge_derived_from_persisted_routing_mode(client: TestClient) -> None:
    launch = _launch_form(client, "gate 来源测试", "持久化路由。")
    board = client.get(launch.headers["location"])
    # T6 (人工审阅) and T8 (发布闸门) are launched with routing_mode=MANUAL.
    # Each gate task carries the badge both in its board table row and in the
    # actionable "需要你处理的任务" block below it -> 2 tasks x 2 locations = 4.
    assert board.text.count('badge-gate">需你处理 / 审批') == 4

    # Flip T6's persisted routing_mode to FIXED; the badge must disappear for T6
    # (only T8 remains) because the gate is read from persisted state. T8 still
    # shows the badge in both its table row and the gate block -> 2.
    project_id = launch.headers["location"].rsplit("/", 1)[-1]
    _set_task_routing_mode(project_id, "T6 人工审阅", RoutingMode.FIXED)

    board_after = client.get(launch.headers["location"])
    assert board_after.text.count('badge-gate">需你处理 / 审批') == 2
    # T6 is still rendered, just no longer flagged as an owner gate.
    assert "T6 人工审阅" in board_after.text
    assert "Traceback" not in board_after.text


# 8b. Idempotency conflict (same key, different body) returns readable HTML 409.
def test_idempotency_conflict_returns_html(client: TestClient) -> None:
    first = _launch_form(client, "冲突测试 A", "第一次提交。", idem="conflict-key")
    assert first.status_code == 303
    second = _launch_form(client, "冲突测试 B", "第二次不同内容。", idem="conflict-key")
    assert second.status_code == 409
    assert "Traceback" not in second.text
    assert "提交标识" in second.text  # owner-readable Chinese explanation
    # No duplicate project is created by the rejected replay.
    projects, _ = _count_projects_and_tasks()
    assert projects == 1


# 8c. Unexpected server error during launch returns readable HTML 500 (no trace).
def test_unexpected_launch_error_returns_html_500(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("aios.api.app.launch_campaign", _raise_runtime_error)

    resp = _launch_form(client, "意外错误测试", "触发 500。", idem="boom-key")
    assert resp.status_code == 500
    assert "Traceback" not in resp.text
    assert "提交时出现问题" in resp.text
    # The idem key is preserved on the 500 page so a retry stays idempotent.
    assert 'name="idem" value="boom-key"' in resp.text


# 8d. Unexpected server error while reading the board returns readable HTML 500.
def test_unexpected_board_error_returns_html_500(client: TestClient, monkeypatch) -> None:
    launch = _launch_form(client, "看板 500 测试", "触发看板 500。")
    project_id = launch.headers["location"].rsplit("/", 1)[-1]
    monkeypatch.setattr("aios.api.app.build_board_view", _raise_runtime_error)

    resp = client.get(f"/owner/board/{project_id}")
    assert resp.status_code == 500
    assert "Traceback" not in resp.text
    assert "读取看板时" in resp.text
