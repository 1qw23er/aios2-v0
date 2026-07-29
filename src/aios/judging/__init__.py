"""V3 工作日志 LLM 价值判定包（Issue #93, plan §2）。

对外只暴露判定接口与纯对象；传输/脚本实现细节留在子模块。
"""

from __future__ import annotations

from .heuristic import HeuristicJudge
from .llm import (
    DEEPSEEK_ENDPOINT,
    DEFAULT_CONSECUTIVE_FAILURE_THRESHOLD,
    DEFAULT_TIMEOUT_S,
    FALLBACK_MODEL,
    NVIDIA_ENDPOINT,
    PRIMARY_MODEL,
    LlmJudge,
    LlmResponse,
    LlmTransport,
    LlmTransportError,
    estimate_deepseek_cost,
)
from .verdict import (
    JudgeBudget,
    ValueVerdict,
    WorkLogValueJudge,
    score_to_content_value,
)

__all__ = [
    "ValueVerdict",
    "WorkLogValueJudge",
    "JudgeBudget",
    "score_to_content_value",
    "HeuristicJudge",
    "LlmJudge",
    "LlmTransport",
    "LlmTransportError",
    "LlmResponse",
    "PRIMARY_MODEL",
    "FALLBACK_MODEL",
    "NVIDIA_ENDPOINT",
    "DEEPSEEK_ENDPOINT",
    "estimate_deepseek_cost",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_CONSECUTIVE_FAILURE_THRESHOLD",
]
