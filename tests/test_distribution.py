"""Tests for V1-I4: distribution package assembly + human publish-gate (Issue #35).

Acceptance criteria (from the issue) covered here:
  * The package ``Artifact`` references the T3/T4/T5 outputs.
  * Publishing without an L3 approval (no assembled package) is rejected.
  * With approval, the package ``review_status`` becomes APPROVED (ready).
  * Rejection keeps the package NOT ready and returns the gate for rework.
  * NO ``external.publish`` event is ever emitted (nothing auto-posts).
Plus: idempotency, missing-source guard, and console/API smoke.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.api.app import create_app
from aios.campaign import V1_TASKS
from aios.db import get_database_url, get_engine
from aios.distribution import (
    PUBLISH_GATE_ACTION,
    assemble_distribution_package,
    decide_publish_gate,
    get_package_artifact,
    resolve_package_task,
    resolve_platform_source_tasks,
    resolve_publish_gate_task,
)
from aios.execution import ExecutionResult, execute_task
from aios.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactReviewStatus,
    Event,
    Project,
    RiskLevel,
    RoutingMode,
    TaskStatus,
)
from aios.models import (
    Task as TaskModel,
)
from aios.services import ServiceError, decide_approval, ensure_pending_approval


@pytest.fixture
def client(tmp_path) -> TestClient:
    import os

    os.environ["AIOS_DATABASE_URL"] = f"sqlite:///{tmp_path / 'dist.db'}"
    # Force the production model adapter to be unconfigured (execute endpoint -> 503);
    # the package/publish-gate paths are deterministic and need no model creds.
    os.environ.pop("AIOS_AGENT_API_KEY", None)
    os.environ.pop("AIOS_AGENT_BASE_URL", None)
    with TestClient(create_app(), follow_redirects=False) as test_client:
        yield test_client


def _session() -> Session:
    return Session(get_engine(get_database_url()))


def _launch(client: TestClient) -> None:
    client.post(
        "/owner/launch",
        data={"name": "分发包切片", "objective": "把三个平台产出打包并经发布闸门后手动发布"},
    )


def _task_by_key(session: Session, key: str) -> TaskModel:
    title = next(t["title"] for t in V1_TASKS if t["key"] == key)
    return session.exec(select(TaskModel).where(TaskModel.title == title)).first()


def _status(session: Session, task: TaskModel) -> TaskStatus:
    return session.get(TaskModel, task.id).status


def _project_id(client: TestClient) -> str:
    with _session() as session:
        return session.exec(select(Project)).first().id


# --- deterministic, full-protocol execution adapter (mirrors test_execution) ---


def _sample_for_schema(schema: dict[str, Any]) -> Any:
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
    """Walks the real execution protocol; only the model call is substituted by
    schema-valid placeholder data."""

    def run(self, *, task_id, task_context, output_schema, idempotency_key) -> ExecutionResult:
        data = _sample_for_schema(output_schema)
        return ExecutionResult(
            summary=f"平台产物摘要 {task_id}",
            claims=[],
            artifacts=[
                {
                    "type": "json",
                    "uri": f"exec://{task_id}/{idempotency_key}",
                    "summary": f"平台产物摘要 {task_id}",
                    "data": data,
                }
            ],
        )


def _run_platform_chain(session: Session) -> tuple[TaskModel, TaskModel, TaskModel]:
    """Execute T1..T5 so the three platform outputs (T3/T4/T5) have artifacts.

    Returns (T3, T4, T5). After this, T6 (owner review) is READY but not decided,
    so T7 (package) is still BACKLOG -- assembly does not require T6 approval, only
    that the platform artifacts exist.
    """
    for key in ("T1", "T2", "T3", "T4", "T5"):
        task = _task_by_key(session, key)
        execute_task(session, task.id, f"exec:{key}", adapter=ScriptedExecutionAdapter())
    return _task_by_key(session, "T3"), _task_by_key(session, "T4"), _task_by_key(session, "T5")


def _approve_review_gate(session: Session, project_id: str) -> None:
    """Drive T6 (human review) to DONE so T7 (package) becomes READY -- used by the
    console button test, which requires the package task to be READY."""
    t6 = _task_by_key(session, "T6")
    approval = ensure_pending_approval(
        session,
        project_id=project_id,
        task_id=t6.id,
        action_type="owner_gate",
    )
    decide_approval(session, approval.id, ApprovalStatus.APPROVED, None)


# --- graph resolution (title/key/capability-independent) -----------------------


def test_graph_resolution_identifies_package_and_gate(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        pid = session.exec(select(Project)).first().id
        gate = resolve_publish_gate_task(session, pid)
        package = resolve_package_task(session, pid)
        sources = resolve_platform_source_tasks(session, pid)
        # Resolved purely by dependency-graph topology (T-key is NOT persisted;
        # `packaging` sits on both T3 and T7).
        assert gate is not None and gate.title == _task_by_key(session, "T8").title
        assert gate.routing_mode == RoutingMode.MANUAL
        assert package is not None and package.title == _task_by_key(session, "T7").title
        assert package.routing_mode == RoutingMode.FIXED
        source_titles = {t.title for t in sources}
        assert source_titles == {
            _task_by_key(session, "T3").title,
            _task_by_key(session, "T4").title,
            _task_by_key(session, "T5").title,
        }


# --- AC 1: package Artifact references T3/T4/T5 outputs -------------------------


def test_package_references_t3_t4_t5_outputs(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        pid = session.exec(select(Project)).first().id
        t3, t4, t5 = _run_platform_chain(session)

        package = assemble_distribution_package(session, pid, "pkg-1")

        meta = package.metadata_json
        assert meta["kind"] == "distribution_package"
        assert package.review_status == ArtifactReviewStatus.UNVERIFIED  # not ready yet
        # Sources cite each platform output's *latest* artifact id.
        source_task_ids = {s["task_id"] for s in meta["sources"]}
        assert source_task_ids == {t3.id, t4.id, t5.id}
        for source in meta["sources"]:
            artifact = session.get(Artifact, source["artifact_id"])
            assert artifact is not None and artifact.task_id == source["task_id"]
            assert source["summary"]  # provenance carried from the platform artifact
        # A pending L3 approval was opened and is cited by the package.
        approval = session.get(Approval, meta["publish_approval_id"])
        assert approval is not None
        assert approval.risk_level == RiskLevel.L3
        assert approval.status == ApprovalStatus.PENDING
        assert approval.action_type == PUBLISH_GATE_ACTION
        # The packaging task (T7) is completed by assembly.
        assert _status(session, _task_by_key(session, "T7")) == TaskStatus.DONE


def test_package_missing_source_artifact_is_rejected(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        pid = session.exec(select(Project)).first().id
        # Only T1..T3 executed: T4/T5 have no artifact yet.
        for key in ("T1", "T2", "T3"):
            task = _task_by_key(session, key)
            execute_task(session, task.id, f"exec:{key}", adapter=ScriptedExecutionAdapter())
        with pytest.raises(ServiceError) as exc:
            assemble_distribution_package(session, pid, "pkg-early")
        assert exc.value.status_code == 409
        assert "无法打包" in str(exc.value)
        # No package artifact and no L3 approval were created.
        assert get_package_artifact(session, pid) is None
        assert (
            session.exec(
                select(Approval).where(Approval.action_type == PUBLISH_GATE_ACTION)
            ).first()
            is None
        )


# --- AC 2: publishing without an L3 approval / package is rejected --------------


def test_publish_without_package_is_rejected(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        pid = session.exec(select(Project)).first().id
        _run_platform_chain(session)
        # No package assembled yet -> no L3 approval -> publish is refused.
        with pytest.raises(ServiceError) as exc:
            decide_publish_gate(session, pid, ApprovalStatus.APPROVED, None)
        assert exc.value.status_code == 409
        assert "尚未生成分发包" in str(exc.value)
        # Nothing was marked ready.
        assert get_package_artifact(session, pid) is None


# --- AC 3: with approval, the package becomes ready + T8 done -------------------


def test_publish_approval_marks_package_ready(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        pid = session.exec(select(Project)).first().id
        _run_platform_chain(session)
        package = assemble_distribution_package(session, pid, "pkg-2")
        assert package.review_status == ArtifactReviewStatus.UNVERIFIED

        approval = decide_publish_gate(session, pid, ApprovalStatus.APPROVED, "内容确认无误")

        assert approval.status == ApprovalStatus.APPROVED
        # Package is now ready (owner copies + posts by hand).
        ready = get_package_artifact(session, pid)
        assert ready.review_status == ArtifactReviewStatus.APPROVED
        # The publish gate (T8) is completed.
        assert _status(session, _task_by_key(session, "T8")) == TaskStatus.DONE


# --- AC 4: rejection keeps the package NOT ready, returns the gate --------------


def test_publish_rejection_keeps_package_not_ready(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        pid = session.exec(select(Project)).first().id
        _run_platform_chain(session)
        assemble_distribution_package(session, pid, "pkg-3")

        approval = decide_publish_gate(session, pid, ApprovalStatus.REJECTED, "标题需再打磨")

        assert approval.status == ApprovalStatus.REJECTED
        # Package stays NOT ready.
        assert get_package_artifact(session, pid).review_status == ArtifactReviewStatus.UNVERIFIED
        # Gate returned for rework (not DONE).
        assert _status(session, _task_by_key(session, "T8")) == TaskStatus.REVIEW


# --- AC 5: NO external.publish event is EVER emitted ---------------------------


def test_no_external_publish_event_ever_emitted(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        pid = session.exec(select(Project)).first().id
        _run_platform_chain(session)
        assemble_distribution_package(session, pid, "pkg-4")
        decide_publish_gate(session, pid, ApprovalStatus.APPROVED, None)

        # Scan the ENTIRE event outbox: nothing auto-posts to any platform.
        events = session.exec(select(Event)).all()
        assert events  # sanity: the flow did emit events (task.completed / task.ready)
        assert all(event.type != "external.publish" for event in events)
        assert not any("publish" in event.type for event in events)
        assert not any(event.type.startswith("external.") for event in events)


# --- idempotency & safety -----------------------------------------------------


def test_assemble_is_idempotent(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        pid = session.exec(select(Project)).first().id
        _run_platform_chain(session)
        a1 = assemble_distribution_package(session, pid, "pkg-a")
        # A different key still returns the SAME single package (project has one package).
        a2 = assemble_distribution_package(session, pid, "pkg-b")
        assert a2.id == a1.id
        packages = session.exec(
            select(Artifact).where(Artifact.external_result_id.like("package:%"))
        ).all()
        assert len(packages) == 1
        # Exactly one L3 publish approval exists.
        approvals = session.exec(
            select(Approval).where(Approval.action_type == PUBLISH_GATE_ACTION)
        ).all()
        assert len(approvals) == 1


def test_double_decision_is_rejected(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        pid = session.exec(select(Project)).first().id
        _run_platform_chain(session)
        assemble_distribution_package(session, pid, "pkg-d")
        decide_publish_gate(session, pid, ApprovalStatus.APPROVED, None)
        # The L3 approval is no longer PENDING -> a second decision is refused.
        with pytest.raises(ServiceError) as exc:
            decide_publish_gate(session, pid, ApprovalStatus.APPROVED, None)
        assert exc.value.status_code == 409


def test_invalid_decision_is_rejected(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        pid = session.exec(select(Project)).first().id
        _run_platform_chain(session)
        assemble_distribution_package(session, pid, "pkg-i")
        with pytest.raises(ServiceError) as exc:
            decide_publish_gate(session, pid, ApprovalStatus.PENDING, None)
        assert exc.value.status_code == 400


# --- API smoke ----------------------------------------------------------------


def test_api_package_and_publish_gate_endpoints(client: TestClient) -> None:
    _launch(client)
    pid = _project_id(client)
    with _session() as session:
        _run_platform_chain(session)
        t7 = _task_by_key(session, "T7")
        t8 = _task_by_key(session, "T8")

    # Assemble the package via the JSON endpoint (deterministic, needs no model creds).
    resp = client.post(f"/tasks/{t7.id}/package", headers={"Idempotency-Key": "api-pkg"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["metadata_json"]["kind"] == "distribution_package"
    assert len(body["metadata_json"]["sources"]) == 3

    # Decide the L3 publish gate via the JSON endpoint.
    resp = client.post(f"/tasks/{t8.id}/publish-gate", json={"decision": "approved"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    with _session() as session:
        assert (
            get_package_artifact(session, pid).review_status == ArtifactReviewStatus.APPROVED
        )


def test_api_execute_rejects_package_task(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        t7 = _task_by_key(session, "T7")
    # The packaging task must NOT be run through the LLM execute path.
    resp = client.post(f"/tasks/{t7.id}/execute", headers={"Idempotency-Key": "x"})
    assert resp.status_code == 400
    assert "package" in resp.text


def test_api_publish_gate_without_package_is_rejected(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        t8 = _task_by_key(session, "T8")
    resp = client.post(f"/tasks/{t8.id}/publish-gate", json={"decision": "approved"})
    assert resp.status_code == 409


# --- owner console (HTML) smoke ------------------------------------------------


def test_owner_console_package_and_publish_flow(client: TestClient) -> None:
    _launch(client)  # sets the aios_last_campaign cookie on the client
    pid = _project_id(client)
    with _session() as session:
        _run_platform_chain(session)
        _approve_review_gate(session, pid)  # T6 DONE -> T7 READY -> "生成分发包" shows
        t7 = _task_by_key(session, "T7")
        t8 = _task_by_key(session, "T8")
        assert _status(session, t7) == TaskStatus.READY

    board = client.get(f"/owner/board/{pid}")
    assert "生成分发包" in board.text

    # Assemble via the owner HTML action -> 303 back to the board.
    resp = client.post(f"/owner/tasks/{t7.id}/package")
    assert resp.status_code == 303

    board = client.get(f"/owner/board/{pid}")
    # The publish-gate card now offers approve/reject.
    assert "批准发布闸门" in board.text

    # Approve the publish gate via the owner HTML action.
    resp = client.post(f"/owner/tasks/{t8.id}/publish", data={"decision": "approve"})
    assert resp.status_code == 303

    board = client.get(f"/owner/board/{pid}")
    assert "分发包已就绪" in board.text
    with _session() as session:
        assert (
            get_package_artifact(session, pid).review_status == ArtifactReviewStatus.APPROVED
        )


def test_owner_execute_rejects_package_task(client: TestClient) -> None:
    _launch(client)
    pid = _project_id(client)
    with _session() as session:
        _run_platform_chain(session)
        _approve_review_gate(session, pid)
        t7 = _task_by_key(session, "T7")
    resp = client.post(f"/owner/tasks/{t7.id}/execute")
    assert resp.status_code == 400
    assert "生成分发包" in resp.text
