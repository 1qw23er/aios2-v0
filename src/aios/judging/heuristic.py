"""HeuristicJudge（Issue #93, plan §3 / §11 step 2）。

升级 #88 ``ContentValueJudge`` 逻辑为 ``WorkLogValueJudge`` 接口实现，作**默认 +
回退**。无网络依赖、永远可用（fail-closed 兜底）。``judge`` 纯判定、不写库。
"""

from __future__ import annotations

from typing import Any

from aios.models import Artifact
from aios.work_log import ContentValueJudge

from .verdict import ValueVerdict, WorkLogValueJudge, score_to_content_value

# 启发式对 medium / low 的（保守）价值分与置信度。启发式不读 LLM，置信度固定中低。
_HEURISTIC_SCORE = {"medium": 0.6, "low": 0.2, "high": 0.8, "none": 0.1}
_HEURISTIC_CONFIDENCE = 0.5


class HeuristicJudge(WorkLogValueJudge):
    """纯启发式判定器（#88 ContentValueJudge 升级版）。

    复用 #88 的 ``ContentValueJudge.judge`` 得到确定性 ``content_value``，再把它
    映射为 ``value_score`` 与 ``suggested_content_value``。``source="heuristic"``、
    ``model=None``、``cost_usd=0``、``fallback=False``。
    """

    def judge(self, artifact: Artifact) -> ValueVerdict:
        metadata: dict[str, Any] = artifact.metadata_json or {}
        raw = {
            "new_knowledge": metadata.get("new_knowledge"),
            "content_value": metadata.get("content_value"),
            "should_enter_kb": metadata.get("should_enter_kb", False),
            "content_angle": metadata.get("content_angle"),
        }
        content_value, _should_enter_kb, _angle = ContentValueJudge.judge(raw)
        score = _HEURISTIC_SCORE.get(content_value, 0.2)
        return ValueVerdict(
            value_score=score,
            confidence=_HEURISTIC_CONFIDENCE,
            reason=f"heuristic baseline: content_value={content_value}",
            source="heuristic",
            provider="heuristic",
            model=None,
            suggested_content_value=score_to_content_value(score, _HEURISTIC_CONFIDENCE),
            fallback=False,
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
            latency_ms=0,
        )
