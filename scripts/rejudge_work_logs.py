#!/usr/bin/env python3
"""后台重判工作日志价值（#93 V3, plan §7）。

经 ``_owner_cli`` 认证取得 owner ``actor`` 后，定位「尚未判定 / 低置信度」的
UNVERIFIED WORK_LOG，逐条调用 ``WorkLogValueJudge`` 产出价值判定，仅把结果写入
``Artifact.metadata_json["llm_verdict_draft"]``（**绝不**改 ``review_status`` /
``content_value`` / ``should_enter_kb``），并记 ``AuditLog(action="work_log.judge")``。

设计要点（plan §7 / §5 Gate D/E/F）：
- 默认 ``LlmJudge``（NVIDIA 免费主 → DeepSeek 付费备 → HeuristicJudge 保底）瀑布；
  可注入 ``judge`` 便于测试。
- fail-closed 聚合：单条 ``judge`` 抛异常（含瀑布之外的 unexpected）绝不静默跳过——
  catch 后改产 ``HeuristicJudge`` 保底 verdict（保证每条日志都拿到一条 draft，不丢日志），
  记 ``fallback=True`` 审计，``judge_errors>0`` → 脚本返回非零退出码（cron 重试/告警）。
- ``--dry-run``：只枚举将重判的目标日志摘要，**不调用 judge、不发起 LLM、不写库、
  不写 AuditLog**（Gate E 不适用）。
- Gate F：只写 draft，绝不 attest / 改 review_status / 写 should_enter_kb。

Usage
-----
    python scripts/rejudge_work_logs.py --project-id proj_x
    python scripts/rejudge_work_logs.py --project-id proj_x --agent-ref codex
    python scripts/rejudge_work_logs.py --project-id proj_x --since 2026-01-01T00:00:00Z --limit 100
    python scripts/rejudge_work_logs.py --project-id proj_x --max-cost-usd 0.5 --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlmodel import Session, select

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _owner_cli import add_owner_args, authenticate_owner_cli  # noqa: E402

from aios.actor import ActorContext, _assert_owner_actor  # noqa: E402
from aios.audit import append_audit, redact_secrets  # noqa: E402
from aios.db import get_database_url, get_engine, run_migrations  # noqa: E402
from aios.judging import HeuristicJudge, JudgeBudget, LlmJudge, WorkLogValueJudge  # noqa: E402
from aios.judging.verdict import MIN_CONFIDENCE  # noqa: E402
from aios.models import Artifact, ArtifactReviewStatus, ArtifactType  # noqa: E402
from aios.services import ServiceError  # noqa: E402

# 默认成本门上限（保守）。逐项 DeepSeek 最坏情况估算见 ``estimate_deepseek_cost``。
DEFAULT_MAX_COST_USD = 0.5

# 审计 trail 的 action 常量（plan §5 Gate E）。
JUDGE_AUDIT_ACTION = "work_log.judge"


def _as_aware(value: datetime) -> datetime:
    """把任意 datetime 规整为 aware UTC，便于与 ``now_utc`` 产出值比较。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _needs_rejudge(metadata: dict[str, Any]) -> bool:
    """该日志是否需要重判（plan §7 范围）：无 draft 或 confidence 低于阈值。

    畸形 draft（字符串/列表/标量等非 dict）按「需重判」处理，避免 ``.get`` 抛
    ``AttributeError`` 使整批候选选择崩溃、阻断其余正常日志（P2 修复）。
    """
    draft = metadata.get("llm_verdict_draft")
    if draft is None or not isinstance(draft, dict):
        return True
    try:
        confidence = float(draft.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        return True
    # 非有限值（NaN/Infinity，JSON 可承载）比较恒为 False，会绕过阈值被判为「已高置信」；
    # 视作畸形需重判（P2 修复）。
    if not math.isfinite(confidence):
        return True
    return confidence < MIN_CONFIDENCE


@dataclass
class RejudgeSummary:
    """一次重判运行的聚合结果（供 CLI 打印与测试断言）。"""

    total: int          # 选中的候选日志数（dry-run 下即「将被判定」数）
    judged: int         # 实际写库（写 draft + 审计）的条数
    errors: int         # judge 异常（已兜底 HeuristicJudge）条数
    dry_run: bool       # 是否为 dry-run（不判定/不写库）
    cost_usd: float = 0.0  # 本次运行累计成本（NVIDIA=0，DeepSeek=估算）
    skipped: int = 0    # 已高置信度、不需重判的条数

    def exit_code(self) -> int:
        """fail-closed：有 judge 异常 → 非零退出码（供 cron 重试/告警）。"""
        if self.errors > 0 and not self.dry_run:
            return 1
        return 0


def _select_targets(
    session: Session,
    *,
    project_id: str,
    agent_ref: str | None = None,
    since: datetime | None = None,
    limit: int | None = None,
) -> list[Artifact]:
    """定位重判候选：UNVERIFIED WORK_LOG 且（无 draft 或低置信度）。

    范围筛选在 Python 内完成（plan §7），便于复用 ``_needs_rejudge`` 的阈值语义；
    ``agent_ref``（按 ``Agent.platform``，即 provenance 的 produced_by_platform）、
    ``since``（created_at）、``limit`` 均为可选收窄条件。
    """
    rows = session.exec(
        select(Artifact)
        .where(
            Artifact.type == ArtifactType.WORK_LOG,
            Artifact.review_status == ArtifactReviewStatus.UNVERIFIED,
            Artifact.project_id == project_id,
        )
        .order_by(Artifact.created_at)
    ).all()

    targets: list[Artifact] = []
    for artifact in rows:
        # --limit 0 / 负数：请求上限为零，直接返回空（P2 修复，避免误判一条）。
        if limit is not None and limit <= 0:
            break
        metadata = artifact.metadata_json or {}
        if not _needs_rejudge(metadata):
            continue
        if agent_ref is not None:
            platform = (artifact.provenance_json or {}).get("produced_by_platform")
            if platform != agent_ref:
                continue
        if since is not None and _as_aware(artifact.created_at) < _as_aware(since):
            continue
        targets.append(artifact)
        if limit is not None and len(targets) >= limit:
            break
    return targets


def _write_draft_if_unverified(
    session: Session, artifact_id: str, metadata: dict[str, Any]
) -> bool:
    """原子条件更新：仅当该日志 ``review_status`` 仍为 UNVERIFIED 时才写入 draft。

    与单纯「刷新后判断」不同，这里把「状态校验」与「draft 写入」合并为一条带
    ``WHERE review_status=UNVERIFIED`` 的 UPDATE，使其在数据库层面原子完成（P1 修复）：
    若 owner 在 ``judge→commit`` 窗口内 attest 改了状态，UPDATE 命中 0 行，
    draft 不会被写回已审批日志。返回是否命中（命中即写入成功）。
    """
    result = session.execute(
        text(
            "UPDATE artifact SET metadata = :md "
            "WHERE id = :id AND review_status = :st"
        ),
        {
            "md": json.dumps(metadata),
            "id": artifact_id,
            # NOTE: SQLAlchemy's Enum column stores the member *name* ('UNVERIFIED'),
            # not the value ('unverified'). Match the stored form or the atomic
            # UPDATE will hit 0 rows and every draft silently fails to persist.
            "st": ArtifactReviewStatus.UNVERIFIED.name,
        },
    )
    return result.rowcount > 0


def rejudge(
    session: Session,
    *,
    actor: ActorContext,
    project_id: str,
    agent_ref: str | None = None,
    since: datetime | None = None,
    limit: int | None = None,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
    dry_run: bool = False,
    judge: WorkLogValueJudge | None = None,
) -> RejudgeSummary:
    """重判核心逻辑（可测试）。``judge`` 可注入；为 None 时建 ``LlmJudge``。

    - dry_run：只枚举候选，不判定/不写库/不审计。
    - 实跑：逐条判定（fail-closed 兜底 HeuristicJudge），仅写 draft + 审计，
      不改 review_status/content_value/should_enter_kb（Gate F）。
    """
    _assert_owner_actor(actor)
    targets = _select_targets(
        session, project_id=project_id, agent_ref=agent_ref, since=since, limit=limit
    )
    if dry_run:
        return RejudgeSummary(
            total=len(targets), judged=0, errors=0, dry_run=True, skipped=0
        )

    active_judge = judge or LlmJudge(JudgeBudget(max_usd=max_cost_usd))
    heuristic = HeuristicJudge()
    run_id = datetime.now(UTC).isoformat()
    judged = 0
    errors = 0
    cost_usd = 0.0

    for artifact in targets:
        try:
            verdict = active_judge.judge(artifact)
        except Exception as exc:  # fail-closed 兜底（Gate D）
            verdict = heuristic.judge(artifact)
            verdict.fallback = True
            verdict.fallback_reason = f"unexpected_error:{type(exc).__name__}"
            errors += 1

        # Gate C 对账/报告（P2 修复）：LLM 调用在 judge() 内已发生——无论写库成败、
        # 无论是否回退到启发式，其成本都已产生。失败的付费尝试把可能已被计费的
        # reserved_cost_usd 留在 verdict 上，须计入运行成本，避免漏报真实花费。
        # 因此成本累计放在写库之前，且同时累加 reserved_cost_usd。
        cost_usd += float(verdict.cost_usd or 0.0) + float(verdict.reserved_cost_usd or 0.0)

        # 写库前刷新该日志最新 metadata（attest 可能已改过），仅增 draft 键。
        session.refresh(artifact)
        metadata = dict(artifact.metadata_json or {})
        previous_draft = metadata.get("llm_verdict_draft")
        committed_value = str(metadata.get("content_value") or "none")
        committed_seb = bool(metadata.get("should_enter_kb", False))

        draft = redact_secrets(verdict.to_draft_dict())
        # Gate F：只增 draft 键，绝不改已提交的判定字段；Gate B：draft 也过删，
        # 防止 LLM 把密钥回显进 reason 后落库（AuditLog 同样在 append_audit 内过删）。
        metadata["llm_verdict_draft"] = draft

        # P1 原子化（并发安全 / Gate F）：把「状态校验」与「draft 写入」合并为一条带
        # WHERE review_status=UNVERIFIED 的条件 UPDATE。若 owner 在 judge→commit 窗口内
        # attest 改了状态，UPDATE 命中 0 行，draft 不会被写回已审批日志；审计亦不写。
        if not _write_draft_if_unverified(session, artifact.id, metadata):
            session.rollback()
            continue

        before = {
            "review_status": artifact.review_status.value,
            "content_value": committed_value,
            "should_enter_kb": committed_seb,
            "llm_verdict_draft": previous_draft,
        }
        append_audit(
            session,
            actor=actor.owner_id or "owner",
            action=JUDGE_AUDIT_ACTION,
            resource_type="artifact",
            resource_id=artifact.id,
            project_id=artifact.project_id,
            task_id=artifact.task_id,
            before=before,
            after=draft,
            idempotency_key=f"audit:work_log:judge:{artifact.id}:{run_id}",
        )
        try:
            session.commit()
        except Exception:
            # 单条写库失败：回滚该条、计为错误、继续其余（绝不中断丢日志）。
            # 成本已在上面计入（调用已发生），不在此重复累加。
            session.rollback()
            errors += 1
        else:
            judged += 1

    return RejudgeSummary(
        total=len(targets),
        judged=judged,
        errors=errors,
        dry_run=False,
        cost_usd=cost_usd,
        skipped=0,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-id", required=True, help="限定重判的项目")
    parser.add_argument(
        "--agent-ref",
        default=None,
        help="按 Agent.platform 范围筛选（即 provenance.produced_by_platform）",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="仅重判 created_at >= 该 ISO 时间之后的日志",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="最多重判的日志条数（cron 分批）",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=DEFAULT_MAX_COST_USD,
        help="DeepSeek 付费判定累计成本上限（默认 0.5，NVIDIA 免费不经此门）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只枚举将重判的目标并列出摘要，不判定/不写库/不审计",
    )
    add_owner_args(parser)
    return parser.parse_args()


def _describe(log: Artifact) -> str:
    metadata = log.metadata_json or {}
    what_done = str(metadata.get("what_done", ""))[:60]
    platform = (log.provenance_json or {}).get("produced_by_platform")
    return (
        f"{log.id}  project={log.project_id}  type={metadata.get('report_type')}  "
        f"value={metadata.get('content_value')}  source={metadata.get('source_platform')}  "
        f"agent_platform={platform}  what_done={what_done!r}"
    )


def main() -> int:
    args = _parse_args()
    actor = authenticate_owner_cli(args)

    if args.limit is not None and args.limit <= 0:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2

    since: datetime | None = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since)
        except ValueError:
            print(f"invalid --since (expected ISO8601): {args.since}", file=sys.stderr)
            return 2

    run_migrations()
    with Session(get_engine(get_database_url())) as session:
        try:
            summary = rejudge(
                session,
                actor=actor,
                project_id=args.project_id,
                agent_ref=args.agent_ref,
                since=since,
                limit=args.limit,
                max_cost_usd=args.max_cost_usd,
                dry_run=args.dry_run,
            )
        except ServiceError as error:
            print(f"error {error.status_code}: {error.detail}", file=sys.stderr)
            return 1

        if summary.dry_run:
            print(f"[dry-run] {summary.total} work log(s) would be re-judged:")
            targets = _select_targets(
                session,
                project_id=args.project_id,
                agent_ref=args.agent_ref,
                since=since,
                limit=args.limit,
            )
            for log in targets:
                print(f"  {_describe(log)}")
            return 0

        print(
            f"re-judged {summary.judged}/{summary.total} work log(s); "
            f"errors={summary.errors}; cost_usd={summary.cost_usd:.6f}"
        )
        if summary.errors:
            print(
                "warning: some logs fell back to heuristic judge (see audit trail); "
                "non-zero exit for cron retry.",
                file=sys.stderr,
            )
        return summary.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
