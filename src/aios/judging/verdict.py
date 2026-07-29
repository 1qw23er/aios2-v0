"""V3 判定接口与 Verdict（Issue #93, plan §3）。

纯判定契约：``WorkLogValueJudge.judge(artifact) -> ValueVerdict`` 只读
``Artifact.metadata_json`` 的 7 汇报字段，产出结构化 ``ValueVerdict``，**不写库**。

本模块不含任何 LLM / 网络 / DB 代码，可被单测直接驱动（TDD 第一步：接口 + 映射）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aios.models import Artifact

# 确定性映射阈值（test_score_to_content_value_mapping 锁死）。
HIGH_THRESHOLD = 0.75
MEDIUM_THRESHOLD = 0.5
LOW_THRESHOLD = 0.25
# 置信度低于此值的 LLM 判定不可信，回退（plan §5 Gate A 低解析置信度）。
MIN_CONFIDENCE = 0.4


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


@dataclass
class ValueVerdict:
    """一次价值判定的结构化结果（plan §3）。

    纯数据：判定器产出、重判脚本负责落库（写入 ``metadata_json["llm_verdict_draft"]``
    并记 ``AuditLog``）。``attempted_*`` 字段保留付费尝试的可审计痕迹——即便最终
    回退到启发式也要携带，供 Gate C/E 预算对账（见 plan §4 / §5）。
    """

    value_score: float                 # 0.0–1.0，语义价值分
    confidence: float                  # 0.0–1.0，模型/启发式置信度
    reason: str                        # 人类可读理由（短，<200 字）
    source: str                       # "heuristic" | "llm"
    provider: str                     # 实际判定 provider："heuristic" | "nvidia" | "deepseek"
    model: str | None                 # LLM 模型名（heuristic 为 None）
    suggested_content_value: str      # 由 (value_score, confidence) 确定性映射
    fallback: bool = False            # 本次是否由 LLM 回退到启发式（或脚本级异常兜底）
    prompt_tokens: int = 0            # prompt token 数（heuristic=0；Gate E 审计用）
    completion_tokens: int = 0        # completion token 数（heuristic=0；Gate E 审计用）
    cost_usd: float = 0.0             # 本次判定成本（NVIDIA 免费=0；DeepSeek 付费=估算）
    latency_ms: int = 0               # 本次判定延迟
    # —— 付费尝试的可审计痕迹（即便最终回退到启发式也要保留，供 Gate E/C 对账）——
    attempted_provider: str | None = None   # 实际尝试付费 provider（即使回退），如 "deepseek"
    attempted_model: str | None = None      # 尝试的模型名，如 "deepseek-chat"
    reserved_cost_usd: float = 0.0          # 本次付费尝试前 reserve 的 est_cost（可能已被计费）
    attempt_latency_ms: int = 0             # 本次付费尝试（含失败）耗时
    fallback_reason: str | None = None      # 回退原因，如 "deepseek_timeout"

    def to_draft_dict(self) -> dict[str, Any]:
        """序列化为 ``metadata_json["llm_verdict_draft"]`` 的纯 JSON 结构（plan §6）。"""
        return {
            "value_score": self.value_score,
            "confidence": self.confidence,
            "reason": self.reason,
            "source": self.source,
            "provider": self.provider,
            "model": self.model,
            "suggested_content_value": self.suggested_content_value,
            "fallback": self.fallback,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "attempted_provider": self.attempted_provider,
            "attempted_model": self.attempted_model,
            "reserved_cost_usd": self.reserved_cost_usd,
            "attempt_latency_ms": self.attempt_latency_ms,
            "fallback_reason": self.fallback_reason,
            "judged_at": datetime.now(UTC).isoformat(),
        }


class WorkLogValueJudge(ABC):
    """工作日志价值判定接口（plan §3）。

    实现：``HeuristicJudge``（默认 + 回退）、``LlmJudge``（NVIDIA 免费主 + DeepSeek 付费备）。
    重判脚本统一经本接口调用，不直接依赖具体实现。
    """

    @abstractmethod
    def judge(self, artifact: Artifact) -> ValueVerdict:
        """输入一个 WORK_LOG Artifact，产出 ValueVerdict。纯判定，不写库。"""


@dataclass
class JudgeBudget:
    """运行级成本预算（Gate C 共享对象，plan §3 / §4 / §5）。

    由重判脚本创建并注入 ``LlmJudge``。NVIDIA 免费池（cost=0）不经过此预算；
    只有 DeepSeek 付费调用前才 ``can_afford`` 校验 + ``reserve`` 预留。

    ``spent_usd`` 是**保守上界**：失败（可能已被计费）的调用也保留预留，故 spent
    永不低估真实花费，且因每次分发前已被 ``can_afford`` 拦截、预留为最坏上界、
    对账仅退不减，运行累计成本**永不超过** ``max_usd``。
    """

    max_usd: float
    spent_usd: float = 0.0

    def remaining(self) -> float:
        return self.max_usd - self.spent_usd

    def can_afford(self, est_usd: float) -> bool:
        """最坏情况估算成本是否不超剩余预算（预留前校验）。"""
        return est_usd <= self.remaining()

    def reserve(self, est_usd: float) -> None:
        """分发付费调用**前**预留最坏情况成本 est_usd；调用方须先 ``can_afford``。"""
        self.spent_usd += est_usd

    def reconcile(self, est_usd: float, actual_usd: float) -> None:
        """付费调用结束后对账：actual<=est，退还多预留 (est-actual)。

        失败（可能已被计费）**不**调用，保留预留（plan §4）。
        P1 双保险：若 actual>est（极端 token 密度）使累加越过 max_usd，钳制
        spent_usd 不超过 max_usd，杜绝硬预算上限被突破。后续 can_afford 将因此
        拦截，不再发起新付费调用。
        """
        self.spent_usd -= (est_usd - actual_usd)
        if self.spent_usd > self.max_usd:
            self.spent_usd = self.max_usd


def score_to_content_value(score: float, confidence: float = 1.0) -> str:
    """把 (value_score, confidence) 确定性映射为 content_value（plan §3 / §8）。

    阈值单测锁死（高分→high、中→medium、低→low、极低→none）；置信度过低时
    直接判 ``none``（不可信，交由人工/启发式）。**仅作建议**，绝不反向写入
    ``artifact.metadata_json["content_value"]``（Gate F）。
    """
    score = _clamp01(score)
    confidence = _clamp01(confidence)
    if confidence < MIN_CONFIDENCE:
        return "none"
    if score >= HIGH_THRESHOLD:
        return "high"
    if score >= MEDIUM_THRESHOLD:
        return "medium"
    if score >= LOW_THRESHOLD:
        return "low"
    return "none"
