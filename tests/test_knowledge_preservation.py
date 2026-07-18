"""Tests for V1-I5: knowledge preservation governance (Issue #38).

Acceptance criteria (from the issue) covered here:
  * An APPROVED company-scoped fact appears in a NEW campaign's
    ``TaskContext.approved_facts`` (AC1), with its source-campaign provenance
    preserved even though the effective scope is company-wide.
  * Submitting from a non-approved (UNVERIFIED) artifact is rejected (AC2).
  * Versioning / supersede works via the existing ``KnowledgeService`` (AC3).

Knowledge-governance boundaries (owner-review of PR #52) covered here:
  * B1 scope: owner preserve chooses project (default) or company (opt-in);
    scope is read-only at review.
  * B2 trust boundary: review loads the candidate, requires DRAFT, and verifies
    source-campaign ownership against the active owner campaign; cross-campaign
    review is rejected.
  * B3 negative: a project-scoped fact never leaks into an unrelated campaign's
    ``approved_facts``.
  * B4 idempotency: repeated preserve submission creates exactly one candidate.
  * B5 T9-only: preserve is allowed only on the knowledge-capture (T9) task.
  * B6 route-level: identical review replay is safe; approve->reject and
    reject->approve conflict; cross-campaign review is rejected.
  * Versioning atomicity: version must be exactly head + 1 (no gaps).

Key model invariant: a company-scoped fact has ``project_id = NULL`` (effective
scope) but ``source_project_id`` is ALWAYS the producing campaign, so source
ownership is never lost. Provenance requires the source artifact to belong to a
campaign, so ``_company_approved_artifact`` is created with a real ``project_id``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.api.app import create_app
from aios.campaign import V1_TASKS
from aios.context_service import ContextService
from aios.db import get_database_url, get_engine
from aios.execution import execute_task
from aios.knowledge_service import KnowledgeService
from aios.models import (
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    KnowledgeCandidate,
    KnowledgeCandidateStatus,
    KnowledgeFact,
    KnowledgeFactStatus,
    KnowledgeReviewDecisionValue,
    Project,
)
from aios.models import (
    Task as TaskModel,
)
from aios.schemas import KnowledgeCandidateCreate, KnowledgeReviewRequest
from aios.services import ServiceError, set_artifact_review_status

pytest_plugins: list[str] = []


def _sample_for_schema(schema):
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
    """Walks the real execution protocol; only the model call is substituted by
    schema-valid placeholder data (mirrors test_distribution.py)."""

    def run(self, *, task_id, task_context, output_schema, idempotency_key):
        from aios.execution import ExecutionResult

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

    os.environ["AIOS_DATABASE_URL"] = f"sqlite:///{tmp_path / 'kp.db'}"
    os.environ.pop("AIOS_AGENT_API_KEY", None)
    os.environ.pop("AIOS_AGENT_BASE_URL", None)
    with TestClient(create_app(), follow_redirects=False) as test_client:
        yield test_client


def _session() -> Session:
    return Session(get_engine(get_database_url()))


def _launch(client: TestClient, name: str = "campaign") -> str:
    client.post(
        "/owner/launch",
        data={"name": name, "objective": "knowledge reuse test"},
    )
    with _session() as session:
        return (
            session.exec(select(Project).order_by(Project.created_at.desc())).first().id
        )


def _task_by_key(session: Session, key: str, project_id: str | None = None) -> TaskModel:
    title = next(t["title"] for t in V1_TASKS if t["key"] == key)
    stmt = select(TaskModel).where(TaskModel.title == title)
    if project_id is not None:
        stmt = stmt.where(TaskModel.project_id == project_id)
    return session.exec(stmt).first()


def _run_platform_chain(session: Session) -> list[str]:
    """Execute T1..T5 so the three platform outputs (T3/T4/T5) have artifacts.

    Returns the task IDs (strings) rather than detached objects, so callers can
    re-query them inside their own session.
    """
    ids = []
    for key in ("T1", "T2", "T3", "T4", "T5"):
        task = _task_by_key(session, key)
        execute_task(session, task.id, f"exec:{key}", adapter=ScriptedExecutionAdapter())
        ids.append(task.id)
    return ids


def _company_approved_artifact(session: Session, project_id: str) -> Artifact:
    """An APPROVED artifact that belongs to a real campaign (``project_id``).

    This is the only source that yields a company-scoped fact visible to a LATER
    (different-project) campaign, while preserving source-campaign provenance via
    ``source_project_id``. The artifact must belong to a campaign so provenance is
    never lost (project_id = NULL on a fact is the EFFECTIVE scope, not provenance).
    """
    art = Artifact(
        project_id=project_id,
        task_id=None,
        type=ArtifactType.JSON,
        uri="company://approved-article",
        checksum="sha256:company-approved",
        review_status=ArtifactReviewStatus.APPROVED,
        metadata_json={"summary": "公司已验证的旗舰定位文章"},
    )
    session.add(art)
    session.commit()
    return art


def _submit_via_service(
    session: Session, artifact: Artifact, statement: str, scope: str = "company"
) -> str:
    cand = KnowledgeService(session).submit_candidate(
        artifact.id, statement, scope, "owner"
    )
    return cand.id


# --- AC1: approved company fact appears in a NEW campaign's approved_facts -----


def test_approved_fact_reused_in_later_campaign_context(client: TestClient) -> None:
    pid1 = _launch(client, "campaign #1")
    with _session() as session:
        artifact = _company_approved_artifact(session, pid1)
        cand_id = _submit_via_service(session, artifact, "AI觅主打个人 IP 增长闭环", "company")

    # Owner reviews the candidate into a versioned, company-scoped fact.
    with _session() as session:
        KnowledgeService(session).review_candidate(
            cand_id,
            KnowledgeReviewDecisionValue.APPROVE,
            "owner",
            "证据充分，批准为 v1",
            series_id="positioning",
            version=1,
        )

    # Launch a SECOND campaign (different project) and read one of its tasks' context.
    _launch(client, "campaign #2")
    with _session() as session:
        t2 = _task_by_key(session, "T2")
        ctx = ContextService(session).build_context(t2.id)
        facts = [f for f in ctx.approved_facts if f.get("fact_kind") == "knowledge_fact"]
        reused = [
            f
            for f in facts
            if f["statement"] == "AI觅主打个人 IP 增长闭环" and f["scope"] == "company"
        ]
        assert reused, "Later campaign must auto-reuse the approved company fact"
        # Provenance is preserved even though the effective scope is company-wide.
        assert reused[0]["source_project_id"] == pid1


# --- AC2: submitting from a non-approved artifact is rejected ------------------


def test_submit_from_unapproved_artifact_rejected(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        _run_platform_chain(session)
        t3 = _task_by_key(session, "T3")
        unapproved = session.exec(
            select(Artifact).where(Artifact.task_id == t3.id)
        ).first()
        assert unapproved.review_status != ArtifactReviewStatus.APPROVED

        with pytest.raises(ServiceError) as exc:
            KnowledgeService(session).submit_candidate(
                unapproved.id, "未批准不可沉淀", "project", "owner"
            )
        assert exc.value.status_code == 422
        assert "approved" in str(exc.value).lower()


def test_submit_from_unapproved_artifact_rejected_via_json(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        _run_platform_chain(session)
        t3 = _task_by_key(session, "T3")
        unapproved = session.exec(
            select(Artifact).where(Artifact.task_id == t3.id)
        ).first()
    resp = client.post(
        "/knowledge/candidates",
        headers={"Idempotency-Key": "kp-unapproved"},
        json={"artifact_id": unapproved.id, "statement": "应被拒", "scope": "project"},
    )
    assert resp.status_code == 422


# --- AC3: versioning / supersede via the existing service ---------------------


def test_review_supersede_creates_new_version(client: TestClient) -> None:
    pid1 = _launch(client, "campaign #1")
    with _session() as session:
        artifact = _company_approved_artifact(session, pid1)
        cand1 = KnowledgeService(session).submit_candidate(
            artifact.id, "v1 定位", "company", "owner"
        )
        KnowledgeService(session).review_candidate(
            cand1.id, KnowledgeReviewDecisionValue.APPROVE, "owner", "v1 ok",
            series_id="positioning", version=1,
        )
        # A second candidate in the same series supersedes v1.
        cand2 = KnowledgeService(session).submit_candidate(
            artifact.id, "v2 定位（迭代）", "company", "owner"
        )
        fact1 = session.exec(select(KnowledgeFact)).first()
        fact2 = KnowledgeService(session).review_candidate(
            cand2.id, KnowledgeReviewDecisionValue.APPROVE, "owner", "v2 ok",
            series_id="positioning", version=2, supersedes_fact_id=fact1.id,
        ).fact

        all_facts = session.exec(select(KnowledgeFact)).all()
        assert len(all_facts) == 2
        assert fact2.version == 2
        # Provenance preserved on both facts.
        assert all(f.source_project_id == pid1 for f in all_facts)
        heads = [f for f in all_facts if f.status == KnowledgeFactStatus.APPROVED]
        assert len(heads) == 1 and heads[0].id == fact2.id
        superseded = [f for f in all_facts if f.status == KnowledgeFactStatus.SUPERSEDED]
        assert len(superseded) == 1


# --- B3 negative: project-scoped fact does NOT leak into unrelated campaign ---


def test_project_scoped_fact_not_in_unrelated_campaign(client: TestClient) -> None:
    pid_a = _launch(client, "campaign A")
    with _session() as session:
        # Project-scoped (default) approved fact inside campaign A.
        art = _company_approved_artifact(session, pid_a)
        cand = KnowledgeService(session).submit_candidate(
            art.id, "仅本 campaign 复用的知识", "project", "owner"
        )
        KnowledgeService(session).review_candidate(
            cand.id, KnowledgeReviewDecisionValue.APPROVE, "owner", "v1 ok",
            series_id="local-only", version=1,
        )

    # Launch an UNRELATED campaign B and read ITS OWN T2 context.
    pid_b = _launch(client, "campaign B")
    with _session() as session:
        t2 = _task_by_key(session, "T2", pid_b)
        ctx = ContextService(session).build_context(t2.id)
        facts = [f for f in ctx.approved_facts if f.get("fact_kind") == "knowledge_fact"]
        # The project-scoped fact from A must NOT appear in B.
        assert not any(
            f["statement"] == "仅本 campaign 复用的知识" for f in facts
        ), "Project-scoped fact must not leak into an unrelated campaign"


# --- owner console + JSON entry points ---------------------------------------


def test_owner_preserve_button_submits_candidate(client: TestClient) -> None:
    pid = _launch(client)
    with _session() as session:
        _run_platform_chain(session)
        t3 = _task_by_key(session, "T3")
        art = session.exec(
            select(Artifact).where(Artifact.task_id == t3.id)
        ).first()
        set_artifact_review_status(session, art.id, ArtifactReviewStatus.APPROVED)
        t9 = _task_by_key(session, "T9")
        t9_id = t9.id
        artifact_id = art.id

    # Owner clicks "沉淀知识" on T9 (project scope by default).
    resp = client.post(
        f"/owner/tasks/{t9_id}/preserve",
        data={"artifact_id": artifact_id, "statement": "从已批准文章提炼的知识"},
    )
    assert resp.status_code == 303
    with _session() as session:
        rows = session.exec(select(KnowledgeCandidate)).all()
        assert len(rows) == 1
        assert rows[0].statement == "从已批准文章提炼的知识"
        assert rows[0].status == KnowledgeCandidateStatus.DRAFT
        # Project scope: effective scope == provenance == the active campaign.
        assert rows[0].project_id == pid
        assert rows[0].source_project_id == pid


def test_owner_preserve_rejects_non_t9_task(client: TestClient) -> None:
    _launch(client)
    with _session() as session:
        _run_platform_chain(session)
        t3 = _task_by_key(session, "T3")
        art = session.exec(
            select(Artifact).where(Artifact.task_id == t3.id)
        ).first()
        set_artifact_review_status(session, art.id, ArtifactReviewStatus.APPROVED)
        t3_id = t3.id
        artifact_id = art.id
    # B5: preserve is only allowed on the T9 knowledge-capture task.
    resp = client.post(
        f"/owner/tasks/{t3_id}/preserve",
        data={"artifact_id": artifact_id, "statement": "不应在 T3 沉淀"},
    )
    assert resp.status_code == 400
    with _session() as session:
        assert session.exec(select(KnowledgeCandidate)).first() is None


def test_owner_preserve_idempotent(client: TestClient) -> None:
    """B4: repeated identical preserve submission creates exactly one candidate."""
    _launch(client)
    with _session() as session:
        _run_platform_chain(session)
        t3 = _task_by_key(session, "T3")
        art = session.exec(
            select(Artifact).where(Artifact.task_id == t3.id)
        ).first()
        set_artifact_review_status(session, art.id, ArtifactReviewStatus.APPROVED)
        t9 = _task_by_key(session, "T9")
        t9_id = t9.id
        artifact_id = art.id
    payload = {"artifact_id": artifact_id, "statement": "幂等沉淀测试"}
    r1 = client.post(f"/owner/tasks/{t9_id}/preserve", data=payload)
    r2 = client.post(f"/owner/tasks/{t9_id}/preserve", data=payload)
    assert r1.status_code == 303 and r2.status_code == 303
    with _session() as session:
        rows = session.exec(select(KnowledgeCandidate)).all()
        assert len(rows) == 1, "Repeated preserve must produce exactly one candidate"


def test_owner_review_candidate_becomes_fact(client: TestClient) -> None:
    pid = _launch(client, "campaign #1")
    with _session() as session:
        artifact = _company_approved_artifact(session, pid)
        cand_id = _submit_via_service(session, artifact, "待审阅知识", "company")
    # Review happens within the source campaign's cookie scope.
    client.cookies.set("aios_last_campaign", pid)
    resp = client.post(
        f"/owner/knowledge/{cand_id}/review",
        data={
            "decision": "approve",
            "reviewer": "owner",
            "rationale": "批准",
            "series_id": "positioning",
            "version": "1",
        },
    )
    assert resp.status_code == 303
    with _session() as session:
        facts = session.exec(select(KnowledgeFact)).all()
        assert len(facts) == 1
        assert facts[0].status == KnowledgeFactStatus.APPROVED
        assert facts[0].series_id == "positioning"
        assert facts[0].source_project_id == pid


def test_json_review_endpoint_returns_fact(client: TestClient) -> None:
    pid = _launch(client, "campaign #1")
    with _session() as session:
        artifact = _company_approved_artifact(session, pid)
        cand_id = _submit_via_service(session, artifact, "JSON 审阅知识", "company")
    body = KnowledgeReviewRequest(
        decision=KnowledgeReviewDecisionValue.APPROVE,
        reviewer="owner",
        rationale="ok",
        series_id="json-series",
        version=1,
    )
    resp = client.post(
        f"/knowledge/candidates/{cand_id}/review",
        json=body.model_dump(mode="json"),
    )
    assert resp.status_code == 200
    assert resp.json()["decision"] == "approve"


def test_json_submit_endpoint_returns_candidate(client: TestClient) -> None:
    pid = _launch(client, "campaign #1")
    with _session() as session:
        artifact_id = _company_approved_artifact(session, pid).id
    body = KnowledgeCandidateCreate(
        artifact_id=artifact_id, statement="JSON 提交知识", scope="company"
    )
    resp = client.post(
        "/knowledge/candidates",
        headers={"Idempotency-Key": "kp-json"},
        json=body.model_dump(mode="json"),
    )
    assert resp.status_code == 201
    assert resp.json()["statement"] == "JSON 提交知识"
    assert resp.json()["scope"] == "company"


# --- B6 route-level replay / conflict / cross-campaign ------------------------


def test_owner_review_identical_replay_safe(client: TestClient) -> None:
    pid = _launch(client, "campaign #1")
    with _session() as session:
        artifact = _company_approved_artifact(session, pid)
        cand_id = _submit_via_service(session, artifact, "重放测试", "company")
    client.cookies.set("aios_last_campaign", pid)
    first = client.post(
        f"/owner/knowledge/{cand_id}/review",
        data={"decision": "approve", "reviewer": "owner", "rationale": "v1",
              "series_id": "replay", "version": "1"},
    )
    second = client.post(
        f"/owner/knowledge/{cand_id}/review",
        data={"decision": "approve", "reviewer": "owner", "rationale": "v1",
              "series_id": "replay", "version": "1"},
    )
    assert first.status_code == 303 and second.status_code == 303
    with _session() as session:
        assert len(session.exec(select(KnowledgeFact)).all()) == 1


def test_owner_review_approve_then_reject_conflicts(client: TestClient) -> None:
    pid = _launch(client, "campaign #1")
    with _session() as session:
        artifact = _company_approved_artifact(session, pid)
        cand_id = _submit_via_service(session, artifact, "先批准后驳回", "company")
    client.cookies.set("aios_last_campaign", pid)
    approve = client.post(
        f"/owner/knowledge/{cand_id}/review",
        data={"decision": "approve", "reviewer": "owner", "rationale": "v1 ok",
              "series_id": "conflict-a", "version": "1"},
    )
    reject = client.post(
        f"/owner/knowledge/{cand_id}/review",
        data={"decision": "reject", "reviewer": "owner", "rationale": "改主意了"},
    )
    assert approve.status_code == 303
    # Candidate is already terminal (APPROVED) -> reject is rejected (409).
    assert reject.status_code == 409


def test_owner_review_reject_then_approve_conflicts(client: TestClient) -> None:
    pid = _launch(client, "campaign #1")
    with _session() as session:
        artifact = _company_approved_artifact(session, pid)
        cand_id = _submit_via_service(session, artifact, "先驳回后批准", "company")
    client.cookies.set("aios_last_campaign", pid)
    reject = client.post(
        f"/owner/knowledge/{cand_id}/review",
        data={"decision": "reject", "reviewer": "owner", "rationale": "暂不支持"},
    )
    approve = client.post(
        f"/owner/knowledge/{cand_id}/review",
        data={"decision": "approve", "reviewer": "owner", "rationale": "改主意了",
              "series_id": "conflict-b", "version": "1"},
    )
    assert reject.status_code == 303
    # Candidate is already terminal (REJECTED) -> approve is rejected (409).
    assert approve.status_code == 409


def test_owner_review_cross_campaign_rejected(client: TestClient) -> None:
    pid_a = _launch(client, "campaign A")
    pid_b = _launch(client, "campaign B")
    with _session() as session:
        artifact = _company_approved_artifact(session, pid_a)
        cand_id = _submit_via_service(session, artifact, "跨 campaign 评审", "company")
    # Owner is viewing campaign B but the candidate belongs to campaign A.
    client.cookies.set("aios_last_campaign", pid_b)
    resp = client.post(
        f"/owner/knowledge/{cand_id}/review",
        data={"decision": "approve", "reviewer": "owner", "rationale": "越权",
              "series_id": "xcamp", "version": "1"},
    )
    # B2: source-campaign ownership mismatch -> rejected (cross-campaign review).
    assert resp.status_code == 400
    with _session() as session:
        # No fact was created by the rejected cross-campaign attempt.
        assert session.exec(select(KnowledgeFact)).first() is None


# --- Versioning atomicity: version must be exactly head + 1 -------------------


def test_version_must_be_head_plus_one(client: TestClient) -> None:
    pid = _launch(client, "campaign #1")
    with _session() as session:
        artifact = _company_approved_artifact(session, pid)
        cand1 = KnowledgeService(session).submit_candidate(
            artifact.id, "v1", "company", "owner"
        )
        KnowledgeService(session).review_candidate(
            cand1.id, KnowledgeReviewDecisionValue.APPROVE, "owner", "v1",
            series_id="gap", version=1,
        )
        cand2 = KnowledgeService(session).submit_candidate(
            artifact.id, "v2（想跳到 v3）", "company", "owner"
        )
        fact1 = session.exec(select(KnowledgeFact)).first()
        # Gap: trying version 3 when head is v1 must be rejected (contiguous only).
        # Pass the correct predecessor so the gap is isolated to the version check.
        with pytest.raises(ServiceError) as exc:
            KnowledgeService(session).review_candidate(
                cand2.id, KnowledgeReviewDecisionValue.APPROVE, "owner", "gap",
                series_id="gap", version=3, supersedes_fact_id=fact1.id,
            )
        assert exc.value.status_code == 422
