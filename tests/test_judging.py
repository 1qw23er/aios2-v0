"""V3 工作日志 LLM 价值判定测试（Issue #93, plan §8）。

覆盖：
- 判定接口 / 判定器（verdict / heuristic / llm / score 映射 / Gate A/F 回归）
- LlmJudge 双轨 + 回退（mock provider，NVIDIA→DeepSeek→Heuristic）
- 成本 / 连续失败守护（Gate C）
- 审计（Gate E）+ 密钥过删（Gate B）
- 后台重判脚本 / 集成（rejudge：draft-only / 跳过 APPROVED / dry-run / fail-closed /
  幂等 / owner 注入 / Gate A 仅后台）

不引入任何新模型 / 新迁移；Alembic head 仍为 20260728_0009。
"""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session, select

from aios.actor import ActorContext, resolve_owner_actor
from aios.audit import AuditLog
from aios.db import get_engine, run_migrations
from aios.judging import (
    HeuristicJudge,
    JudgeBudget,
    LlmJudge,
    LlmResponse,
    LlmTransport,
    LlmTransportError,
    WorkLogValueJudge,
)
from aios.judging.llm import (
    FALLBACK_MODEL,
    MAX_USER_PROMPT_CHARS,
    NVIDIA_ENDPOINT,
    PRIMARY_MODEL,
    _build_user_prompt,
    _parse_llm_content,
    estimate_deepseek_cost,
)
from aios.judging.verdict import MIN_CONFIDENCE, ValueVerdict, score_to_content_value
from aios.models import Artifact, ArtifactReviewStatus, ArtifactType
from aios.review import owner_approve_review
from aios.services import ServiceError
from aios.work_log import WorkLogService

# 复用 #88 测试的 owner / agent 约定。
OWNER = resolve_owner_actor()
AGENT_ACTOR = ActorContext(kind="agent", agent_id="agt_x")

_LOG_FIELDS = {
    "what_done": "完成了公众号文章初稿",
    "why": "本周内容排期",
    "problem": "标题不勾人",
    "solution": "换成数字型标题",
    "new_knowledge": "实验结论：数字型标题的打开率对比提升明显，值得沉淀为固定套路。",
}


# ---------------------------------------------------------------------------
# Pytest fixtures (与 test_work_log.py 同构)
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_url(tmp_path) -> str:
    url = f"sqlite:///{(tmp_path / 'judging.db').as_posix()}"
    run_migrations(url)
    return url


@pytest.fixture()
def session(db_url: str):
    with Session(get_engine(db_url)) as s:
        yield s


def _seed_project(session: Session, name: str = "P") -> object:
    from aios.models import Project

    project = Project(name=name, objective="O")
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def _submit(session: Session, project, *, key: str = "k-1", **overrides) -> Artifact:
    kwargs = {
        "project_id": project.id,
        "report_type": "daily",
        **_LOG_FIELDS,
        "idempotency_key": key,
        "actor": OWNER,
    }
    kwargs.update(overrides)
    artifact, _created = WorkLogService(session).submit_work_log(**kwargs)
    return artifact


def _make_artifact(metadata: dict | None = None) -> Artifact:
    """构造一个内存态 WORK_LOG Artifact（纯判定器测试用，无需落库）。"""
    return Artifact(
        project_id="P1",
        type=ArtifactType.WORK_LOG,
        review_status=ArtifactReviewStatus.UNVERIFIED,
        uri="work_log:mem",
        checksum="sha256:mem",
        metadata_json=metadata or {"new_knowledge": "实验结论：对比提升明显"},
    )


# ---------------------------------------------------------------------------
# Fake 判定器 / 传输（解耦网络与 DB）
# ---------------------------------------------------------------------------


class FakeJudge(WorkLogValueJudge):
    """可注入的判定器：返回固定 verdict，或在某些 artifact 上抛异常。"""

    def __init__(self, verdict: ValueVerdict | None = None, raise_for: set[str] | None = None):
        self._verdict = verdict or _llm_verdict()
        self._raise_for = raise_for or set()
        self.calls: list[str] = []

    def judge(self, artifact: Artifact) -> ValueVerdict:
        self.calls.append(artifact.id)
        if artifact.id in self._raise_for:
            raise RuntimeError("injected judge failure")
        return self._verdict


class FakeTransport(LlmTransport):
    """可注入的 LLM 传输：按 behaviors 序列返回响应或抛错。

    behaviors 中每个元素要么是 ``LlmResponse``（成功），要么是 ``Exception``
    （抛 LlmTransportError 之外也可直接抛，便于模拟超时）。
    """

    def __init__(self, behaviors: list | None = None, delay_s: float = 0.0):
        self._behaviors = list(behaviors or [])
        self._delay_s = delay_s
        self.calls: list[tuple[str, str]] = []

    def call(self, *, endpoint, api_key, model, system_prompt, user_prompt, timeout_s):
        self.calls.append((endpoint, model))
        if self._delay_s:
            import time

            time.sleep(self._delay_s)
        if not self._behaviors:
            raise LlmTransportError("no behavior configured")
        behavior = self._behaviors.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


# ---------------------------------------------------------------------------
# 判定器构造 helper
# ---------------------------------------------------------------------------


def _llm_verdict(
    score: float = 0.9,
    confidence: float = 0.95,
    provider: str = "nvidia",
    model: str = PRIMARY_MODEL,
) -> ValueVerdict:
    return ValueVerdict(
        value_score=score,
        confidence=confidence,
        reason="llm judged ok",
        source="llm",
        provider=provider,
        model=model,
        suggested_content_value=score_to_content_value(score, confidence),
        prompt_tokens=10,
        completion_tokens=20,
        cost_usd=0.0,
        latency_ms=100,
    )


def _response(model: str, score: float = 0.8, confidence: float = 0.9) -> LlmResponse:
    return LlmResponse(
        model=model,
        prompt_tokens=12,
        completion_tokens=30,
        content=json.dumps(
            {"value_score": score, "confidence": confidence, "reason": "ok"}
        ),
        latency_ms=120,
    )


# ---------------------------------------------------------------------------
# 判定接口 / 判定器（plan §8）
# ---------------------------------------------------------------------------


def test_heuristic_judge_baseline() -> None:
    artifact = _make_artifact({"new_knowledge": "实验结论：对比提升明显值得沉淀"})
    v = HeuristicJudge().judge(artifact)
    assert v.source == "heuristic"
    assert v.model is None
    assert v.cost_usd == 0.0
    assert v.fallback is False
    assert 0.0 <= v.value_score <= 1.0
    assert v.suggested_content_value in ("none", "low", "medium", "high")


def test_llm_judge_returns_verdict(monkeypatch) -> None:
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nvapi-fake")
    transport = FakeTransport(behaviors=[_response(PRIMARY_MODEL, 0.9, 0.95)])
    judge = LlmJudge(JudgeBudget(max_usd=1.0), transport=transport)
    v = judge.judge(_make_artifact())
    assert v.source == "llm"
    assert v.model == PRIMARY_MODEL
    assert v.provider == "nvidia"
    assert v.fallback is False
    assert v.suggested_content_value == score_to_content_value(0.9, 0.95)


def test_parse_llm_content_rejects_non_finite() -> None:
    """P2 回归：JSON 可含 NaN/Inf，比较恒 False 会绕过范围校验；须显式拒非有限值。"""
    nan_payload = json.dumps({"value_score": float("nan"), "confidence": 0.9, "reason": "x"})
    inf_payload = json.dumps({"value_score": 0.9, "confidence": float("inf"), "reason": "x"})
    ok_payload = json.dumps({"value_score": 0.8, "confidence": 0.9, "reason": "ok"})
    with pytest.raises(LlmTransportError):
        _parse_llm_content(nan_payload)
    with pytest.raises(LlmTransportError):
        _parse_llm_content(inf_payload)
    # 正常值仍可被解析
    assert _parse_llm_content(ok_payload) == (0.8, 0.9, "ok")


def test_llm_judge_falls_back_on_non_finite(monkeypatch) -> None:
    """P2 集成回归：provider 回 NaN 时不得持久化非标准分，须回退启发式。"""
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nvapi-fake")
    bad = LlmResponse(
        model=PRIMARY_MODEL,
        prompt_tokens=12,
        completion_tokens=30,
        content=json.dumps({"value_score": float("nan"), "confidence": 0.9, "reason": "x"}),
        latency_ms=10,
    )
    transport = FakeTransport(behaviors=[bad])
    judge = LlmJudge(JudgeBudget(max_usd=1.0), transport=transport)
    v = judge.judge(_make_artifact())
    assert v.source == "heuristic"
    assert v.fallback is True


def test_user_prompt_is_capped(monkeypatch) -> None:
    """P1 回归：超大工作日志的 prompt 必须被裁剪，使实际 token 数不超过最坏情况估算。

    否则 actual_cost 可能越过 est_cost，reconcile 会把 spent_usd 推过 max_usd，
    破坏硬预算不变式（Gate C）。
    """
    fields = ("what_done", "why", "problem", "solution", "new_knowledge")
    big = {k: "工作日志内容" * 4000 for k in fields}
    artifact = _make_artifact(metadata=big)
    prompt = _build_user_prompt(artifact)
    assert len(prompt) <= MAX_USER_PROMPT_CHARS
    # 裁剪后最坏情况 token 数（CJK ~1 token/字符）不超过 DEEPSEEK_WORST_CASE_PROMPT_TOKENS，
    # 确保 actual_cost 永不超过 est_cost（硬预算不变式，Gate C）。


def test_score_to_content_value_mapping() -> None:
    assert score_to_content_value(0.9, 0.95) == "high"
    assert score_to_content_value(0.6, 0.95) == "medium"
    assert score_to_content_value(0.3, 0.95) == "low"
    assert score_to_content_value(0.1, 0.95) == "none"
    # 低置信度 → 不可信 → none
    assert score_to_content_value(0.99, 0.1) == "none"
    assert MIN_CONFIDENCE == 0.4


def test_llm_draft_never_sets_committed_content_value(session) -> None:
    from scripts.rejudge_work_logs import rejudge

    project = _seed_project(session)
    artifact = _submit(session, project)
    committed = artifact.metadata_json["content_value"]
    rejudge(session, actor=OWNER, project_id=project.id, judge=FakeJudge(_llm_verdict()))
    session.refresh(artifact)
    # Gate F：draft 进 metadata，但已提交的 content_value 不变。
    assert "llm_verdict_draft" in artifact.metadata_json
    assert artifact.metadata_json["content_value"] == committed


def test_draft_survives_attest_override(session) -> None:
    from scripts.rejudge_work_logs import rejudge

    project = _seed_project(session)
    artifact = _submit(session, project)
    rejudge(session, actor=OWNER, project_id=project.id, judge=FakeJudge(_llm_verdict(0.9, 0.95)))
    session.refresh(artifact)
    draft = artifact.metadata_json["llm_verdict_draft"]
    assert draft["suggested_content_value"] == "high"
    # owner 经 attest 采纳建议（人工裁定路径）
    WorkLogService(session).attest_work_log(
        artifact_id=artifact.id, actor=OWNER, should_enter_kb=True, content_value="high"
    )
    session.refresh(artifact)
    md = artifact.metadata_json
    assert md["content_value"] == "high"  # 仅人工 attest 提交
    assert md["should_enter_kb"] is True
    assert artifact.review_status == ArtifactReviewStatus.APPROVED
    # draft 建议仍保留可追因，未被 attest 覆盖/删除（Gate F + §11）
    assert md["llm_verdict_draft"]["suggested_content_value"] == "high"


def test_owner_approve_review_still_rejects_work_log(session) -> None:
    project = _seed_project(session)
    artifact = _submit(session, project)
    with pytest.raises(ServiceError) as exc:
        owner_approve_review(session, artifact_id=artifact.id, actor=OWNER)
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# LlmJudge 双轨 + 回退（mock provider）
# ---------------------------------------------------------------------------


def test_llm_fallback_chain_nvidia_to_deepseek_to_heuristic(monkeypatch) -> None:
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nvapi-fake")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake")
    transport = FakeTransport(behaviors=[LlmTransportError("n"), LlmTransportError("d")])
    judge = LlmJudge(JudgeBudget(max_usd=1.0), transport=transport)
    v = judge.judge(_make_artifact())
    assert v.source == "heuristic"
    assert v.fallback is True
    models = [m for (_e, m) in transport.calls]
    assert PRIMARY_MODEL in models  # NVIDIA 先尝试
    assert FALLBACK_MODEL in models  # DeepSeek 后尝试
    assert NVIDIA_ENDPOINT in [e for (e, _m) in transport.calls]


def test_llm_call_timeout_uses_fallback(monkeypatch) -> None:
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nvapi-fake")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake")
    # NVIDIA 超时 → DeepSeek 成功
    transport = FakeTransport(
        behaviors=[LlmTransportError("timeout"), _response(FALLBACK_MODEL, 0.8, 0.9)]
    )
    judge = LlmJudge(JudgeBudget(max_usd=1.0), transport=transport)
    v = judge.judge(_make_artifact())
    assert v.source == "llm"
    assert v.provider == "deepseek"
    assert v.model == FALLBACK_MODEL


def test_fallback_never_raises(monkeypatch) -> None:
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nvapi-fake")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake")
    transport = FakeTransport(behaviors=[LlmTransportError("x"), LlmTransportError("y")])
    judge = LlmJudge(JudgeBudget(max_usd=1.0), transport=transport)
    for _ in range(3):
        v = judge.judge(_make_artifact())  # 必须不抛异常
        assert v.source == "heuristic"
        assert v.fallback is True


def test_credentials_never_persisted(session, monkeypatch) -> None:
    from scripts.rejudge_work_logs import rejudge

    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nvapi-fakekey12345678")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fakekey12345678")
    transport = FakeTransport(behaviors=[_response(PRIMARY_MODEL, 0.9, 0.95)])
    judge = LlmJudge(JudgeBudget(max_usd=1.0), transport=transport)
    project = _seed_project(session)
    artifact = _submit(session, project)
    rejudge(session, actor=OWNER, project_id=project.id, judge=judge)
    session.refresh(artifact)
    md_blob = json.dumps(artifact.metadata_json)
    audits = list(session.exec(select(AuditLog).where(AuditLog.action == "work_log.judge")))
    audit_blob = json.dumps(
        [a.before_snapshot for a in audits] + [a.after_snapshot for a in audits]
    )
    assert "nvapi-" not in md_blob and "sk-" not in md_blob
    assert "nvapi-" not in audit_blob and "sk-" not in audit_blob


def test_redact_secrets_on_judge_audit(session) -> None:
    from scripts.rejudge_work_logs import rejudge

    v = _llm_verdict()
    v.reason = "leak nvapi-abcdefghijklmnop and sk-abcdefghijklmnop"
    project = _seed_project(session)
    artifact = _submit(session, project)
    rejudge(session, actor=OWNER, project_id=project.id, judge=FakeJudge(v))
    session.refresh(artifact)
    audit = session.exec(
        select(AuditLog).where(
            AuditLog.action == "work_log.judge", AuditLog.resource_id == artifact.id
        )
    ).first()
    assert audit is not None
    # Gate B：reason 中的密钥在 draft（metadata）与 audit 快照均被过删。
    assert artifact.metadata_json["llm_verdict_draft"]["reason"] == "[REDACTED]"
    assert audit.after_snapshot["reason"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# 成本 / 连续失败守护（Gate C）
# ---------------------------------------------------------------------------


def test_cost_budget_disables_llm(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake")
    est = estimate_deepseek_cost()
    budget = JudgeBudget(max_usd=est * 0.5)  # 剩余 < est → 调用前即拦截
    transport = FakeTransport(behaviors=[_response(FALLBACK_MODEL, 0.8, 0.9)])
    judge = LlmJudge(budget, transport=transport)
    v = judge.judge(_make_artifact())
    assert v.source == "heuristic"
    assert v.fallback is True
    assert v.fallback_reason == "budget_exhausted"
    assert transport.calls == []  # DeepSeek 从未发起
    assert budget.spent_usd == 0.0
    assert budget.remaining() == budget.max_usd


def test_budget_reserved_on_failed_paid_call(session, monkeypatch) -> None:
    from scripts.rejudge_work_logs import rejudge

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake")
    est = estimate_deepseek_cost()
    budget = JudgeBudget(max_usd=est * 2)
    transport = FakeTransport(
        behaviors=[LlmTransportError("request_failed:timeout")], delay_s=0.002
    )
    judge = LlmJudge(budget, transport=transport)

    # —— 单元层：judge() 纯判定 ——
    v = judge.judge(_make_artifact())
    assert v.fallback is True
    assert v.provider == "heuristic"
    assert v.attempted_provider == "deepseek"
    assert v.attempted_model == FALLBACK_MODEL
    assert v.reserved_cost_usd == est
    assert v.attempt_latency_ms > 0
    assert v.fallback_reason.startswith("deepseek_")
    # 失败（可能已被计费）→ 保留预留，未 reconcile 退减
    assert budget.spent_usd == est
    assert budget.remaining() == budget.max_usd - est

    # —— 集成层：rejudge 写库，AuditLog 携带付费尝试痕迹（Gate C/E）——
    project = _seed_project(session)
    artifact = _submit(session, project)
    rejudge(session, actor=OWNER, project_id=project.id, judge=judge)
    session.refresh(artifact)
    audit = session.exec(
        select(AuditLog).where(
            AuditLog.action == "work_log.judge", AuditLog.resource_id == artifact.id
        )
    ).first()
    assert audit is not None
    assert audit.after_snapshot["attempted_provider"] == "deepseek"
    assert audit.after_snapshot["attempted_model"] == FALLBACK_MODEL
    assert audit.after_snapshot["reserved_cost_usd"] == est
    assert audit.after_snapshot["attempt_latency_ms"] > 0
    assert audit.after_snapshot["fallback_reason"].startswith("deepseek_")


def test_consecutive_failure_disables_llm(monkeypatch) -> None:
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nvapi-fake")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake")
    transport = FakeTransport(behaviors=[LlmTransportError("x"), LlmTransportError("y")])
    judge = LlmJudge(
        JudgeBudget(max_usd=10.0), transport=transport, consecutive_failure_threshold=3
    )
    for _ in range(3):
        v = judge.judge(_make_artifact())
        assert v.source == "heuristic"
    assert judge.llm_disabled is True
    calls_after_3 = len(transport.calls)
    # 第 4 次：llm 已停用，直接 heuristic 保底，不再发起任何传输调用
    v = judge.judge(_make_artifact())
    assert v.source == "heuristic"
    assert len(transport.calls) == calls_after_3


def test_budget_never_exceeds_max_on_huge_tokens(monkeypatch) -> None:
    """P1 回归：即便 provider 回报超大 token 数（actual >> est），spent_usd 也不得越过 max_usd。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake")  # 仅走 DeepSeek 付费路径
    budget = JudgeBudget(max_usd=0.01)
    huge = LlmResponse(
        model=FALLBACK_MODEL,
        prompt_tokens=10_000_000,
        completion_tokens=10_000_000,
        content=json.dumps({"value_score": 0.8, "confidence": 0.9, "reason": "ok"}),
        latency_ms=10,
    )
    transport = FakeTransport(behaviors=[huge])
    judge = LlmJudge(budget, transport=transport)
    judge.judge(_make_artifact())
    assert budget.spent_usd <= budget.max_usd


def test_nvidia_failure_with_exhausted_budget_trips_breaker(monkeypatch) -> None:
    """P2a 回归：NVIDIA 失败 + DeepSeek 预算耗尽时，仍须登记失败以触发连续失败门，
    否则每条日志都会重试超时 NVIDIA（provider storm）。
    """
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nvapi-fake")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake")
    budget = JudgeBudget(max_usd=0.0)  # 首次 DeepSeek 即不可负担
    transport = FakeTransport(behaviors=[LlmTransportError("timeout")] * 5)
    judge = LlmJudge(budget, transport=transport, consecutive_failure_threshold=3)
    for _ in range(3):
        judge.judge(_make_artifact())
    assert judge.llm_disabled is True  # 连续失败门已触发，后续不再重试 NVIDIA


# ---------------------------------------------------------------------------
# 审计（Gate E）
# ---------------------------------------------------------------------------


def test_judge_audit_logged(session) -> None:
    from scripts.rejudge_work_logs import rejudge

    project = _seed_project(session)
    a1 = _submit(session, project, key="k-1")
    rejudge(session, actor=OWNER, project_id=project.id, judge=FakeJudge(_llm_verdict(0.9, 0.95)))
    a2 = _submit(session, project, key="k-2")
    fb = _llm_verdict()
    fb.fallback = True
    fb.fallback_reason = "heuristic_baseline"
    rejudge(session, actor=OWNER, project_id=project.id, judge=FakeJudge(fb))

    audits = list(session.exec(select(AuditLog).where(AuditLog.action == "work_log.judge")))
    assert len(audits) == 2
    by_res = {a.resource_id: a for a in audits}
    succ = by_res[a1.id]
    assert succ.after_snapshot["provider"] == "nvidia"
    assert succ.after_snapshot["model"] == PRIMARY_MODEL
    assert succ.after_snapshot["prompt_tokens"] == 10
    assert succ.after_snapshot["completion_tokens"] == 20
    assert "cost_usd" in succ.after_snapshot
    assert "latency_ms" in succ.after_snapshot
    assert by_res[a2.id].after_snapshot["fallback"] is True
    blob = json.dumps([a.before_snapshot for a in audits] + [a.after_snapshot for a in audits])
    assert "nvapi-" not in blob and "sk-" not in blob


def test_draft_write_audited(session) -> None:
    from scripts.rejudge_work_logs import rejudge

    project = _seed_project(session)
    artifact = _submit(session, project)
    rejudge(session, actor=OWNER, project_id=project.id, judge=FakeJudge(_llm_verdict()))
    session.refresh(artifact)
    assert "llm_verdict_draft" in artifact.metadata_json
    audit = session.exec(
        select(AuditLog).where(
            AuditLog.action == "work_log.judge", AuditLog.resource_id == artifact.id
        )
    ).first()
    assert audit is not None
    draft = artifact.metadata_json["llm_verdict_draft"]
    assert audit.after_snapshot["value_score"] == draft["value_score"]


# ---------------------------------------------------------------------------
# 后台重判脚本 / 集成
# ---------------------------------------------------------------------------


def test_needs_rejudge_malformed_draft() -> None:
    """P2b 单元回归：畸形 draft（字符串/列表/标量）与非有限 confidence 必须判为需重判。"""
    from scripts.rejudge_work_logs import _needs_rejudge

    assert _needs_rejudge({"llm_verdict_draft": "corrupted"}) is True
    assert _needs_rejudge({"llm_verdict_draft": 123}) is True
    assert _needs_rejudge({"llm_verdict_draft": ["x"]}) is True
    assert _needs_rejudge({"llm_verdict_draft": None}) is True
    # 非有限 confidence（NaN/Infinity，JSON 可承载）比较恒 False，须视为畸形
    assert _needs_rejudge({"llm_verdict_draft": {"confidence": float("nan")}}) is True
    assert _needs_rejudge({"llm_verdict_draft": {"confidence": float("inf")}}) is True
    # 正常 dict 仍按置信度判定
    assert _needs_rejudge({"llm_verdict_draft": {"confidence": 0.9}}) is False
    assert _needs_rejudge({"llm_verdict_draft": {"confidence": 0.1}}) is True


def test_rejudge_writes_draft_only(session) -> None:
    from scripts.rejudge_work_logs import rejudge

    project = _seed_project(session)
    artifact = _submit(session, project)
    committed_cv = artifact.metadata_json["content_value"]
    committed_seb = artifact.metadata_json["should_enter_kb"]
    rejudge(session, actor=OWNER, project_id=project.id, judge=FakeJudge(_llm_verdict()))
    session.refresh(artifact)
    assert "llm_verdict_draft" in artifact.metadata_json
    assert artifact.metadata_json["content_value"] == committed_cv
    assert artifact.metadata_json["should_enter_kb"] == committed_seb
    assert artifact.review_status == ArtifactReviewStatus.UNVERIFIED  # 不改状态


def test_rejudge_handles_malformed_draft(session) -> None:
    """P2b 集成回归：历史畸形 draft 的日志应被正常重判并覆盖，不中断整批。"""
    from scripts.rejudge_work_logs import rejudge

    project = _seed_project(session)
    artifact = _submit(session, project)
    # 人为写入畸形 draft（字符串）
    metadata = dict(artifact.metadata_json)
    metadata["llm_verdict_draft"] = "corrupted"
    artifact.metadata_json = metadata
    session.add(artifact)
    session.commit()

    summary = rejudge(session, actor=OWNER, project_id=project.id, judge=HeuristicJudge())
    assert summary.judged == 1
    session.refresh(artifact)
    # 畸形 draft 已被覆盖为合法 dict
    assert isinstance(artifact.metadata_json["llm_verdict_draft"], dict)


def test_rejudge_skips_approved_mid_run(session) -> None:
    """P1 回归：owner 在 LLM 调用期间 attest 为 APPROVED，写库前刷新发现状态变更，跳过重判。"""
    from scripts.rejudge_work_logs import rejudge

    class _AttestMidRunJudge(WorkLogValueJudge):
        def judge(self, artifact: Artifact) -> ValueVerdict:
            # 模拟并发 attest：将同一日志改为 APPROVED 并提交
            row = session.get(Artifact, artifact.id)
            row.review_status = ArtifactReviewStatus.APPROVED
            session.commit()
            return _llm_verdict(0.9, 0.95)

    project = _seed_project(session)
    artifact = _submit(session, project)  # UNVERIFIED
    summary = rejudge(session, actor=OWNER, project_id=project.id, judge=_AttestMidRunJudge())
    # 写库前已变 APPROVED → 跳过重判，绝不覆盖已审批日志
    assert summary.judged == 0
    session.refresh(artifact)
    assert artifact.review_status == ArtifactReviewStatus.APPROVED
    assert "llm_verdict_draft" not in artifact.metadata_json


def test_conditional_draft_write_respects_status(session) -> None:
    """P1 原子化回归：条件 UPDATE 仅当状态仍为 UNVERIFIED 时写 draft；已审批则命中 0 行。"""
    from scripts.rejudge_work_logs import _write_draft_if_unverified

    project = _seed_project(session)
    artifact = _submit(session, project)  # UNVERIFIED
    md = {"llm_verdict_draft": {"confidence": 0.9, "value_score": 0.8}}
    assert _write_draft_if_unverified(session, artifact.id, md) is True
    session.commit()
    session.refresh(artifact)
    assert artifact.metadata_json["llm_verdict_draft"]["confidence"] == 0.9

    # 改为 APPROVED 后，原子更新不再命中，draft 不变（commit 窗口内 attest 不被覆盖）
    artifact.review_status = ArtifactReviewStatus.APPROVED
    session.commit()
    md2 = {"llm_verdict_draft": {"confidence": 0.1, "value_score": 0.2}}
    assert _write_draft_if_unverified(session, artifact.id, md2) is False
    session.refresh(artifact)
    assert artifact.metadata_json["llm_verdict_draft"]["confidence"] == 0.9


def test_select_targets_zero_limit(session) -> None:
    """P2b 回归：--limit 0/负数应返回空候选，避免误判一条。"""
    from scripts.rejudge_work_logs import _select_targets

    project = _seed_project(session)
    _submit(session, project, key="k-1")
    assert _select_targets(session, project_id=project.id, limit=0) == []
    assert _select_targets(session, project_id=project.id, limit=-1) == []


def test_rejudge_skips_approved(session) -> None:
    from scripts.rejudge_work_logs import rejudge

    project = _seed_project(session)
    unverified = _submit(session, project, key="k-u")
    approved = _submit(session, project, key="k-a")
    WorkLogService(session).attest_work_log(artifact_id=approved.id, actor=OWNER)
    rejudge(session, actor=OWNER, project_id=project.id, judge=FakeJudge(_llm_verdict()))
    session.refresh(unverified)
    session.refresh(approved)
    assert "llm_verdict_draft" in unverified.metadata_json
    assert "llm_verdict_draft" not in approved.metadata_json  # APPROVED 不触碰


def test_rejudge_dry_run_no_db_write(session) -> None:
    from scripts.rejudge_work_logs import rejudge

    project = _seed_project(session)
    artifact = _submit(session, project)
    judge = FakeJudge(_llm_verdict())
    summary = rejudge(session, actor=OWNER, project_id=project.id, judge=judge, dry_run=True)
    assert summary.dry_run is True
    assert summary.total == 1
    assert judge.calls == []  # judge 未被调用
    session.refresh(artifact)
    assert "llm_verdict_draft" not in artifact.metadata_json  # 未写库
    audits = list(session.exec(select(AuditLog).where(AuditLog.action == "work_log.judge")))
    assert audits == []  # 未写审计


def test_rejudge_one_error_continues(session) -> None:
    from scripts.rejudge_work_logs import rejudge

    project = _seed_project(session)
    a1 = _submit(session, project, key="k-1")
    a2 = _submit(session, project, key="k-2")  # 这条 judge 会抛异常
    a3 = _submit(session, project, key="k-3")
    judge = FakeJudge(_llm_verdict(), raise_for={a2.id})
    summary = rejudge(session, actor=OWNER, project_id=project.id, judge=judge)
    # fail-closed：异常条仍拿到 heuristic 兜底 draft（不丢日志），errors>0
    assert summary.errors == 1
    assert summary.judged == 3
    for a in (a1, a2, a3):
        session.refresh(a)
        assert "llm_verdict_draft" in a.metadata_json
    assert a2.metadata_json["llm_verdict_draft"]["fallback"] is True
    audit = session.exec(
        select(AuditLog).where(
            AuditLog.action == "work_log.judge", AuditLog.resource_id == a2.id
        )
    ).first()
    assert audit is not None
    assert audit.after_snapshot["fallback"] is True


def test_rejudge_cost_includes_failed_paid_attempt(session, monkeypatch) -> None:
    from scripts.rejudge_work_logs import rejudge

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake")
    est = estimate_deepseek_cost()
    transport = FakeTransport(
        behaviors=[LlmTransportError("request_failed:timeout")], delay_s=0.002
    )
    judge = LlmJudge(JudgeBudget(max_usd=est * 2), transport=transport)
    project = _seed_project(session)
    _submit(session, project)
    summary = rejudge(session, actor=OWNER, project_id=project.id, judge=judge)
    assert summary.judged == 1
    # P2 修复：付费尝试失败（可能已被计费）的 reserved_cost_usd 必须计入运行成本，
    # 不能再以 cost_usd=0 漏报真实花费（写库成功与否不影响成本记账）。
    assert summary.cost_usd == est


def test_rejudge_idempotent_overwrites_draft(session) -> None:
    from scripts.rejudge_work_logs import rejudge

    project = _seed_project(session)
    artifact = _submit(session, project)
    # 第一次产出低置信度 draft（confidence < MIN_CONFIDENCE → 仍需重判）
    rejudge(session, actor=OWNER, project_id=project.id, judge=FakeJudge(_llm_verdict(0.3, 0.2)))
    # 第二次重判覆盖第一次（幂等：仅一个 draft 键，judged_at 更新）
    rejudge(session, actor=OWNER, project_id=project.id, judge=FakeJudge(_llm_verdict(0.5, 0.9)))
    session.refresh(artifact)
    md = artifact.metadata_json
    # Gate A 幂等：仅一个 draft 键（第二次覆盖第一次）
    assert list(md.keys()).count("llm_verdict_draft") == 1
    assert md["llm_verdict_draft"]["value_score"] == 0.5  # 第二次覆盖
    assert md["llm_verdict_draft"]["confidence"] == 0.9
    audits = list(
        session.exec(
            select(AuditLog).where(
                AuditLog.action == "work_log.judge", AuditLog.resource_id == artifact.id
            )
        )
    )
    assert len(audits) == 2  # 审计历史按 run 累加（每 run idempotency_key 唯一）


def test_rejudge_injects_owner_actor(session) -> None:
    from scripts.rejudge_work_logs import rejudge

    project = _seed_project(session)
    with pytest.raises(ServiceError) as exc:
        rejudge(session, actor=AGENT_ACTOR, project_id=project.id, judge=FakeJudge(_llm_verdict()))
    # 非 owner actor → 注入边界拒绝
    assert exc.value.status_code == 403


def test_llm_judge_only_in_background_path(session) -> None:
    project = _seed_project(session)
    artifact = _submit(session, project)  # submit 同步路径绝不判定
    session.refresh(artifact)
    assert "llm_verdict_draft" not in artifact.metadata_json
    WorkLogService(session).attest_work_log(artifact_id=artifact.id, actor=OWNER)
    session.refresh(artifact)
    # attest 同步路径也绝不判定（Gate A）
    assert "llm_verdict_draft" not in artifact.metadata_json
