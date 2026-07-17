from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.api.app import create_app
from aios.campaign import V1_TASKS
from aios.db import get_database_url, get_engine
from aios.execution import (
    ExecutionError,
    ExecutionResult,
    ResultValidationError,
    execute_task,
)
from aios.models import (
    Artifact,
    Event,
    ExecutionAssignment,
    Project,
    RoutingMode,
    TaskContext,
    TaskStatus,
)
from aios.models import (
    Task as TaskModel,
)
from aios.services import ServiceError


@pytest.fixture
def client(tmp_path) -> TestClient:
    import os

    os.environ["AIOS_DATABASE_URL"] = f"sqlite:///{tmp_path / 'exec.db'}"
    # Force the production adapter to be unconfigured so the endpoint returns 503.
    os.environ.pop("AIOS_AGENT_API_KEY", None)
    os.environ.pop("AIOS_AGENT_BASE_URL", None)
    with TestClient(create_app(), follow_redirects=False) as test_client:
        yield test_client


def _session() -> Session:
    return Session(get_engine(get_database_url()))


def _launch(client: TestClient) -> None:
    client.post(
        "/owner/launch",
        data={"name": "真实执行切片", "objective": "把失败的 AI 系统重建成可用的小 AI 公司"},
    )


def _task_by_key(session: Session, key: str) -> TaskModel:
    title = next(t["title"] for t in V1_TASKS if t["key"] == key)
    return session.exec(select(TaskModel).where(TaskModel.title == title)).first()


def _status(session: Session, task: TaskModel) -> TaskStatus:
    return session.get(TaskModel, task.id).status


# --- deterministic, full-protocol adapter (NOT a mock shortcut) ---

def _sample_for_schema(schema: dict[str, Any]) -> Any:
    """Generate schema-valid placeholder data (no real/hardcoded content)."""
    t = schema.get("type")
    if t == "object":
        return {k: _sample_for_schema(v) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        item = schema.get("items", {"type": "string"})
        return [_sample_for_schema(item) for _ in range(max(schema.get("minItems", 1), 1))]
    if t == "string":
        return "示例文本" if schema.get("minLength", 0) else "x"
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return True
    return "x"


class ScriptedExecutionAdapter:
    """Deterministic ExecutionAdapter: walks the real protocol, only the model
    call is substituted by schema-valid placeholder data."""

    def __init__(self, *, fail_validation: bool = False, error: str | None = None) -> None:
        self.fail_validation = fail_validation
        self.error = error

    def run(self, *, task_id, task_context, output_schema, idempotency_key) -> ExecutionResult:
        if self.error:
            raise ExecutionError(502, self.error)
        data = {} if self.fail_validation else _sample_for_schema(output_schema)
        return ExecutionResult(
            summary="脚本化执行产物（测试用）",
            claims=[],
            artifacts=[
                {
                    "type": "json",
                    "uri": f"exec://{task_id}/{idempotency_key}",
                    "summary": "脚本化执行产物（测试用）",
                    "data": data,
                }
            ],
        )


def test_execute_t1_creates_artifact_and_unlocks_t2(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        t1 = _task_by_key(session, "T1")
        t2 = _task_by_key(session, "T2")
        assert _status(session, t1) == TaskStatus.READY

        artifact = execute_task(session, t1.id, "idem-1", adapter=ScriptedExecutionAdapter())

        # Artifact created with provenance.
        assert artifact.task_id == t1.id
        assert artifact.external_result_id == "exec:idem-1"
        assert artifact.metadata_json["summary"]
        assert artifact.metadata_json["context_hash"]
        # Task completed exactly once.
        assert _status(session, t1) == TaskStatus.DONE
        # Downstream T2 unlocked.
        assert _status(session, t2) == TaskStatus.READY


def test_chain_t1_t2_t3_unlocks_downstream(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        t1, t2, t3, t4 = (
            _task_by_key(session, "T1"),
            _task_by_key(session, "T2"),
            _task_by_key(session, "T3"),
            _task_by_key(session, "T4"),
        )
        execute_task(session, t1.id, "a", adapter=ScriptedExecutionAdapter())
        assert _status(session, t2) == TaskStatus.READY
        execute_task(session, t2.id, "b", adapter=ScriptedExecutionAdapter())
        assert _status(session, t3) == TaskStatus.READY
        execute_task(session, t3.id, "c", adapter=ScriptedExecutionAdapter())
        assert _status(session, t3) == TaskStatus.DONE
        # T3 -> T4 (depends on T3).
        assert _status(session, t4) == TaskStatus.READY


def test_execute_idempotent_same_key_no_duplicate(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        t1 = _task_by_key(session, "T1")
        t2 = _task_by_key(session, "T2")
        a1 = execute_task(session, t1.id, "same", adapter=ScriptedExecutionAdapter())
        before_events = session.exec(select(Event)).all()
        # Re-run with the SAME key -> returns existing artifact, no new work.
        a2 = execute_task(session, t1.id, "same", adapter=ScriptedExecutionAdapter())
        assert a2.id == a1.id
        after_events = session.exec(select(Event)).all()
        # No duplicate completion event or downstream activation.
        assert len(after_events) == len(before_events)
        assert _status(session, t2) == TaskStatus.READY  # single activation, not doubled
        assert (
            session.exec(select(Artifact).where(Artifact.task_id == t1.id)).all().__len__() == 1
        )


def test_execute_retry_new_key_recovers_no_duplicate_downstream(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        t1 = _task_by_key(session, "T1")
        t2 = _task_by_key(session, "T2")
        execute_task(session, t1.id, "first", adapter=ScriptedExecutionAdapter())
        ready_before = session.exec(
            select(Event).where(Event.type == "task.ready", Event.task_id == t2.id)
        ).all()
        # Retry with a NEW key after completion -> no new artifact, no new activation.
        execute_task(session, t1.id, "second", adapter=ScriptedExecutionAdapter())
        ready_after = session.exec(
            select(Event).where(Event.type == "task.ready", Event.task_id == t2.id)
        ).all()
        assert len(ready_after) == len(ready_before) == 1
        assert (
            session.exec(select(Artifact).where(Artifact.task_id == t1.id)).all().__len__() == 1
        )


def test_execute_validation_failure_marks_failed(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        t1 = _task_by_key(session, "T1")
        with pytest.raises(ResultValidationError) as exc:
            execute_task(
                session,
                t1.id,
                "bad",
                adapter=ScriptedExecutionAdapter(fail_validation=True),
            )
        assert "输出校验" in str(exc.value)
        # Readable failure state, no artifact, no completion.
        assert _status(session, t1) == TaskStatus.FAILED
        assert session.exec(select(Artifact).where(Artifact.task_id == t1.id)).first() is None
        assert _status(session, _task_by_key(session, "T2")) == TaskStatus.BACKLOG


def test_execute_failed_task_is_recoverable(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        t1 = _task_by_key(session, "T1")
        t2 = _task_by_key(session, "T2")
        # First attempt fails validation.
        with pytest.raises(ResultValidationError):
            execute_task(
                session,
                t1.id,
                "fail",
                adapter=ScriptedExecutionAdapter(fail_validation=True),
            )
        assert _status(session, t1) == TaskStatus.FAILED
        # Retry with a NEW key and a valid adapter -> recovers to DONE, unlocks T2.
        execute_task(session, t1.id, "recover", adapter=ScriptedExecutionAdapter())
        assert _status(session, t1) == TaskStatus.DONE
        assert _status(session, t2) == TaskStatus.READY
        assert session.exec(select(Artifact).where(Artifact.task_id == t1.id)).first() is not None


def test_execute_claims_task_and_builds_context(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        t1 = _task_by_key(session, "T1")
        execute_task(session, t1.id, "ctx", adapter=ScriptedExecutionAdapter())
        # A READY task was claimed (ExecutionAssignment) and an immutable
        # TaskContext was generated for the assigned agent.
        assert (
            session.exec(
                select(ExecutionAssignment).where(ExecutionAssignment.task_id == t1.id)
            ).first()
            is not None
        )
        ctx = session.exec(select(TaskContext).where(TaskContext.task_id == t1.id)).first()
        assert ctx is not None
        assert ctx.context_hash
        assert ctx.objective  # immutable objective from the project


def test_execute_manual_task_rejected(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        # T6 is an owner gate (MANUAL). After launch it is BACKLOG; move it to READY
        # so we exercise the route_task MANUAL block: a READY MANUAL task must NOT be
        # claimed by a department (route_task returns None -> "无法认领"), never a
        # silent success or a false 409 from the wrong guard.
        t6 = _task_by_key(session, "T6")
        assert t6 is not None
        assert t6.routing_mode == RoutingMode.MANUAL
        t6.status = TaskStatus.READY
        session.add(t6)
        session.commit()

        with pytest.raises(ServiceError) as exc:
            execute_task(session, t6.id, "manual", adapter=ScriptedExecutionAdapter())
        # route_task blocks MANUAL -> readable 409 (not a silent success).
        assert exc.value.status_code == 409
        assert "无法认领" in str(exc.value)


def test_execute_endpoint_requires_adapter_config(client: TestClient) -> None:
    """The production /tasks/{id}/execute uses the model adapter; without creds it
    must return a readable 503, never a stack trace."""
    _launch(client)
    with _session() as session:
        t1 = _task_by_key(session, "T1")
    resp = client.post(f"/tasks/{t1.id}/execute", headers={"Idempotency-Key": "ep-1"})
    assert resp.status_code == 503
    assert "未配置" in resp.text


def test_owner_execute_requires_adapter_config(client: TestClient) -> None:
    """Owner-triggered department run reuses the same model adapter; without creds
    it must return a readable 503 page, never a stack trace or silent success."""
    _launch(client)
    with _session() as session:
        t1 = _task_by_key(session, "T1")
    # No campaign-scope cookie -> scope check skipped; adapter unconfigured -> 503.
    resp = client.post(f"/owner/tasks/{t1.id}/execute")
    assert resp.status_code == 503
    assert "未配置" in resp.text


def test_owner_board_shows_run_button_and_artifact(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        t1 = _task_by_key(session, "T1")
        execute_task(session, t1.id, "board", adapter=ScriptedExecutionAdapter())
    board = client.get(f"/owner/board/{_project_id(client)}")
    # READY department task shows a run button; executed task shows its artifact.
    assert "运行部门任务" in board.text
    assert "执行产物" in board.text


def _project_id(client: TestClient) -> str:
    with _session() as session:
        return session.exec(select(Project)).first().id
