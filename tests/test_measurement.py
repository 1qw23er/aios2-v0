"""Tests for V1-I6 measurement report (Issue #40).

Read-only by design: every test only performs SELECTs via
``MeasurementService``. The campaigns are BUILT through the existing
service / execution / distribution / knowledge layers (the same code the
owner console uses) with a deterministic ``ScriptedExecutionAdapter``, then
read back. No new model, no migration, no raw SQL / curl.

The runner (``scripts/run_v1_measurement.py``) shares this exact driving
logic; these tests lock the MeasurementService contract and the two read-only
API routes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlmodel import Session, select

from aios.api.app import create_app
from aios.campaign import V1_TASKS
from aios.context_service import ContextService
from aios.db import get_database_url, get_engine
from aios.distribution import assemble_distribution_package, decide_publish_gate
from aios.execution import ExecutionResult, execute_task
from aios.knowledge_service import KnowledgeReviewDecisionValue, KnowledgeService
from aios.measurement import MeasurementService
from aios.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactReviewStatus,
    Project,
    Task,
)
from aios.schemas import ProjectCreate
from aios.services import (
    decide_approval,
    ensure_pending_approval,
    set_artifact_review_status,
)


def _sample_for_schema(schema):
    """Deterministic schema-valid placeholder (mirrors runner + preservation tests)."""
    if not isinstance(schema, dict):
        return "x"
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
    """Walks the real execution protocol; only the model call is substituted."""

    def run(self, *, task_id, task_context, output_schema, idempotency_key):
        return ExecutionResult(
            summary=f"平台产物摘要 {task_id}",
            claims=[],
            artifacts=[
                {
                    "type": "json",
                    "uri": f"exec://{task_id}/{idempotency_key}",
                    "summary": f"平台产物摘要 {task_id}",
                    "data": _sample_for_schema(output_schema),
                }
            ],
        )


@pytest.fixture
def client(tmp_path) -> TestClient:
    import os

    os.environ["AIOS_DATABASE_URL"] = f"sqlite:///{tmp_path / 'measurement.db'}"
    os.environ.pop("AIOS_AGENT_API_KEY", None)
    os.environ.pop("AIOS_AGENT_BASE_URL", None)
    with TestClient(create_app(), follow_redirects=False) as test_client:
        yield test_client


def _session() -> Session:
    return Session(get_engine(get_database_url()))


def _launch(client: TestClient, name: str = "campaign") -> str:
    client.post("/owner/launch", data={"name": name, "objective": "measurement test"})
    with _session() as session:
        return (
            session.exec(select(Project).order_by(Project.created_at.desc())).first().id
        )


def _task_by_key(session: Session, key: str, project_id: str | None = None) -> Task:
    title = next(t["title"] for t in V1_TASKS if t["key"] == key)
    stmt = select(Task).where(Task.title == title)
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)
    return session.exec(stmt).first()


def _latest_artifact(session: Session, task_id: str) -> Artifact | None:
    return session.exec(
        select(Artifact).where(Artifact.task_id == task_id).order_by(Artifact.created_at)
    ).first()


def _run_full_campaign(
    session: Session,
    index: int,
    name: str,
    objective: str,
    *,
    company_knowledge: bool = False,
    reject_t6_once: bool = False,
) -> str:
    """Drive one campaign T1..T9 through the real service layer (mirrors runner)."""
    idem = f"v1t:{index}:{name}"
    from aios.campaign import launch_campaign

    result = launch_campaign(
        session, ProjectCreate(name=name, objective=objective), idempotency_key=idem
    )
    pid = result.project_id

    for key in ("T1", "T2", "T3", "T4", "T5"):
        task = _task_by_key(session, key, pid)
        execute_task(session, task.id, f"exec:{pid}:{key}", adapter=ScriptedExecutionAdapter())

    t6 = _task_by_key(session, "T6", pid)
    first = ensure_pending_approval(
        session, project_id=pid, task_id=t6.id, action_type="owner_gate"
    )
    if reject_t6_once:
        decide_approval(session, first.id, ApprovalStatus.REJECTED, "首轮退回")
        second = ensure_pending_approval(
            session, project_id=pid, task_id=t6.id, action_type="owner_gate"
        )
        decide_approval(session, second.id, ApprovalStatus.APPROVED, "修改后通过")
    else:
        decide_approval(session, first.id, ApprovalStatus.APPROVED, "owner 通过")

    t3 = _task_by_key(session, "T3", pid)
    art3 = _latest_artifact(session, t3.id)
    if art3 is not None:
        set_artifact_review_status(session, art3.id, ArtifactReviewStatus.APPROVED)

    t9 = _task_by_key(session, "T9", pid)
    execute_task(session, t9.id, f"exec:{pid}:T9", adapter=ScriptedExecutionAdapter())
    candidate = KnowledgeService(session).submit_candidate(
        art3.id, f"[{index}] {name}：已验证的 V1 增长洞察",
        "company" if company_knowledge else "project", "owner",
    )
    # Per-campaign series avoids the global (series_id, version) unique-constraint
    # inconsistency (tracked as a follow-up defect); reuse is proven by SCOPE.
    series = f"positioning:c{index}"
    KnowledgeService(session).review_candidate(
        candidate.id, KnowledgeReviewDecisionValue.APPROVE, "owner",
        "证据充分，批准为 v1", series_id=series, version=1,
    )

    assemble_distribution_package(session, pid, f"pkg:{pid}")
    decide_publish_gate(session, pid, ApprovalStatus.APPROVED, "owner 批准发布闸门")
    return pid


def _seed_five_campaigns(client, session) -> list[str]:
    specs = [
        ("非技术人重建成失败的 AI 系统", "复盘非技术 owner 重建 AI 项目", True),
        ("为什么大多数 AI agent 团队是空壳", "揭示 agent 团队空壳真相", False),
        ("产出一篇真实微信文章", "产出微信长文核心资产", False),
        ("复用已批准知识后发生了什么", "复用公司级事实对比效率", False),
        ("电商商家 AI觅 用例", "电商商家非打扰式转化", False),
    ]
    pids: list[str] = []
    for i, (name, objective, company_k) in enumerate(specs, start=1):
        pids.append(
            _run_full_campaign(
                session, i, name, objective,
                company_knowledge=company_k, reject_t6_once=(i == 3),
            )
        )
    return pids


# --- Tests ---------------------------------------------------------------

def test_measurement_report_five_campaigns(client) -> None:
    _seed_five_campaigns(client, _session())
    report = MeasurementService(_session()).build_report()
    assert report.total_campaigns == 5
    # All 5 reached DONE on every T1..T9.
    assert report.campaign_completion_rate == 1.0
    for c in report.campaigns:
        assert set(c.task_statuses.keys()) == {t["key"] for t in V1_TASKS}
        assert c.completion_rate == 1.0
        # Every campaign produced + reviewed a knowledge fact and a publish-ready package.
        assert c.approved_knowledge_facts >= 1
        assert c.publish_ready_package is True


def test_knowledge_reuse_counted(client) -> None:
    pids = _seed_five_campaigns(client, _session())
    report = MeasurementService(_session()).build_report()
    # Campaigns #2..#5 inherit the company fact from #1 (reuse by scope).
    assert report.knowledge_reuse_campaigns == 4
    # Explicit reuse proof: campaign #4's T2 context holds #1's company fact.
    t2 = _task_by_key(_session(), "T2", pids[3])
    ctx = ContextService(_session()).build_context(t2.id)
    reused = [
        f for f in ctx.approved_facts
        if f.get("fact_kind") == "knowledge_fact"
        and f.get("scope") == "company"
        and f.get("source_project_id") == pids[0]
    ]
    assert reused, "Campaign #4 must auto-reuse campaign #1's company fact"


def test_reject_review_recovery_recorded(client) -> None:
    _seed_five_campaigns(client, _session())
    report = MeasurementService(_session()).build_report()
    # Campaign #3 exercised the reject -> re-approve path.
    c3 = next(c for c in report.campaigns if "微信文章" in c.name)
    assert c3.owner_rejections >= 1
    assert c3.owner_approvals >= 1
    assert c3.completion_rate == 1.0  # recovered to DONE


def test_measurement_is_read_only(client) -> None:
    """build_report must not mutate the database."""
    _seed_five_campaigns(client, _session())
    s = _session()
    before = {
        "project": s.exec(select(func.count()).select_from(Project)).first(),
        "task": s.exec(select(func.count()).select_from(Task)).first(),
        "artifact": s.exec(select(func.count()).select_from(Artifact)).first(),
        "approval": s.exec(select(func.count()).select_from(Approval)).first(),
    }
    MeasurementService(s).build_report()
    after = {
        "project": s.exec(select(func.count()).select_from(Project)).first(),
        "task": s.exec(select(func.count()).select_from(Task)).first(),
        "artifact": s.exec(select(func.count()).select_from(Artifact)).first(),
        "approval": s.exec(select(func.count()).select_from(Approval)).first(),
    }
    assert before == after, "MeasurementService must not write to the DB"


def test_per_campaign_measurement_api(client) -> None:
    pids = _seed_five_campaigns(client, _session())
    resp = client.get(f"/owner/campaigns/{pids[0]}/measurement")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["task_statuses"].keys()) == {t["key"] for t in V1_TASKS}
    assert body["completion_rate"] == 1.0
    assert body["publish_ready_package"] is True


def test_per_campaign_measurement_missing_project_404(client) -> None:
    resp = client.get("/owner/campaigns/does-not-exist/measurement")
    assert resp.status_code == 404


def test_measurement_report_html_route(client) -> None:
    _seed_five_campaigns(client, _session())
    resp = client.get("/owner/measurement")
    assert resp.status_code == 200
    assert "V1 测量报告" in resp.text
    assert "campaign 完成率" in resp.text
