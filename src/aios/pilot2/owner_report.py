"""PILOT-2A-4 owner-facing Chinese report (design §5.1 / §5.4 / A4 / D1).

Renders attribution results for the owner using ONLY business language.

Hard contracts:
  A4  owner 视图中：0 个内部 ID、0 个英文 slug、0 个 "HIGH/直连归因" 字样、
      0 个技术术语、0 个百分比幻觉。全中文业务标签。
  D1  仅呈现 ``EXPERIMENT_ASSOCIATED`` / ``AMBIGUOUS`` / ``UNATTRIBUTED`` 三种
      终态的中文业务标签；绝不出现 "直连" / "高置信" 字样。
  §9  缺失数据显示"暂无数据"，绝不填 0、绝不估算。
  §3.5 样本 < 10 时显式标注"样本极少，仅供参考"。

This module deliberately NEVER emits raw ids (``regob_`` / ``aprop_`` /
``fdec_`` / ``exp_``) or enum machine names (``experiment_associated`` /
``AMBIGUOUS`` / ``HIGH``). The internal ``evidence_json`` may carry ids for
audit; the rendered text is what the owner sees and is the audited surface.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlmodel import Session, select

from aios.pilot2.models import (
    AttributionProposal,
    CohortTag,
    ExperimentRegistry,
    RegistrationObservation,
)

# --- owner-facing Chinese label maps (design §2.2 / §5.4) -------------------
LEVEL_OWNER_LABEL: dict[str, str] = {
    "experiment_associated": "实验关联",
    "ambiguous": "待裁定（多实验重叠）",
    "unattributed": "未归因",
    "click_associated": "点击关联（仅证据）",
}

CHANNEL_OWNER_LABEL: dict[str, str] = {
    "wechat": "微信公众号",
    "wechat_group": "微信群",
    "xhs": "小红书",
    "other": "其他渠道",
}

SMALL_SAMPLE_NOTE = "样本极少，仅供参考"
NO_DATA = "暂无数据"


# A4: the owner view must never leak English slugs / internal ids. Experiment
# names coming from the source system can be machine strings (e.g.
# "summer_campaign", "exp_001"); these are sanitized to a neutral Chinese
# placeholder before they can reach the owner-facing report.
_SLUG_RE = re.compile(r"[A-Za-z_]")


def _safe_experiment_label(name: str | None) -> str:
    """Return a Chinese-safe label for an experiment name (design A4 / D1).

    Any name containing Latin letters or underscores is treated as a machine
    slug / internal id and replaced with a neutral placeholder; only names that
    are already Chinese-friendly business strings are shown verbatim.
    """
    if not name:
        return NO_DATA
    if _SLUG_RE.search(name):
        return "实验（名称已脱敏）"
    return name


@dataclass
class DailyReport:
    """Structured owner report for one day (internal; rendered to Chinese text)."""

    period_label: str
    as_of_date: date
    new_registrations: int = 0
    experiment_associated: int = 0
    ambiguous: int = 0
    unattributed: int = 0
    batch_audit: int = 0
    # experiment_id -> (name, channel_value, count) for EXPERIMENT_ASSOCIATED
    experiment_breakdown: dict[str, tuple[str, str, int]] = field(default_factory=dict)
    login_count: int = 0
    recharge_people: int = 0
    recharge_sum: int = 0
    commission: str | None = None  # always 暂无数据 in PILOT-2A-4 (no source)
    small_sample: bool = False

    @property
    def attributed(self) -> int:
        return self.experiment_associated + self.ambiguous


def _resolve_as_of(as_of_date: date | None) -> date:
    if as_of_date is None:
        return (datetime.now(UTC) - timedelta(days=1)).date()
    return as_of_date


def _period_label(as_of: date) -> str:
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
    base = f"{as_of.month}月{as_of.day}日"
    if as_of == yesterday:
        return f"昨天（{base}）"
    return base


def build_daily_report(
    engine,
    *,
    as_of_date: date | None = None,
) -> DailyReport:
    """Compute the owner daily lead-gen report for one day (design §5.1).

    Counts eligible (non-batch) registrations whose ``registered_at`` falls on
    ``as_of_date`` and joins each to its current AttributionProposal. Batch
    accounts are counted separately for audit and never mixed into business
    counts (design §0.2 / §4.2 / D1).
    """
    as_of = _resolve_as_of(as_of_date)
    report = DailyReport(period_label=_period_label(as_of), as_of_date=as_of)

    with Session(engine) as session:
        experiments = session.exec(select(ExperimentRegistry)).all()
        exp_by_id = {exp.id: exp for exp in experiments}

        # Eligible (non-batch) registrations on the day.
        regs = session.exec(
            select(RegistrationObservation).where(
                RegistrationObservation.cohort_tag != CohortTag.UNKNOWN_BATCH_COHORT
            )
        ).all()
        day_regs = [r for r in regs if r.registered_at.date() == as_of]

        # Batch accounts on the day (audit only).
        batch = session.exec(
            select(RegistrationObservation).where(
                RegistrationObservation.cohort_tag == CohortTag.UNKNOWN_BATCH_COHORT
            )
        ).all()
        report.batch_audit = sum(1 for r in batch if r.registered_at.date() == as_of)

        proposals_by_reg: dict[str, AttributionProposal] = {}
        if day_regs:
            props = session.exec(
                select(AttributionProposal).where(
                    AttributionProposal.registration_observation_id.in_(
                        [r.id for r in day_regs]
                    )
                )
            ).all()
            for p in props:
                proposals_by_reg[p.registration_observation_id] = p

        for reg in day_regs:
            prop = proposals_by_reg.get(reg.id)
            report.new_registrations += 1
            if prop is None:
                # A1: every registration must carry a status; if solve() has not
                # been run we treat it as unattributed rather than dropping it.
                # Downstream measured observations are still counted below.
                report.unattributed += 1
            elif prop.level.value == "experiment_associated":
                report.experiment_associated += 1
                active_ids = prop.evidence_json.get("active_experiment_ids", [])
                for eid in active_ids:
                    exp = exp_by_id.get(eid)
                    if exp is None:
                        continue
                    name = _safe_experiment_label(exp.name)
                    ch = exp.channel.value if exp.channel else "other"
                    cur = report.experiment_breakdown.get(eid)
                    if cur is None:
                        report.experiment_breakdown[eid] = (name, ch, 1)
                    else:
                        report.experiment_breakdown[eid] = (cur[0], cur[1], cur[2] + 1)
            elif prop.level.value == "ambiguous":
                report.ambiguous += 1
            else:  # unattributed (and any future non-final token)
                report.unattributed += 1

            # Downstream observation (design §5.1, measured not estimated).
            # Counted for EVERY non-batch registration, independent of whether a
            # proposal exists yet (attribution fallback must not suppress
            # independently-measured login/recharge data -- fixes Codex P2).
            if reg.last_login_at is not None:
                report.login_count += 1
            if reg.total_recharge and reg.total_recharge > 0:
                report.recharge_people += 1
                report.recharge_sum += reg.total_recharge

        # Commission has no source in PILOT-2A-4 (design §4 / §9): show 暂无数据.
        report.commission = NO_DATA
        report.small_sample = report.new_registrations < 10

    return report


def render_report_text(report: DailyReport) -> str:
    """Render the DailyReport to Chinese business text (design §5.1 / A4)."""
    lines: list[str] = []
    lines.append(report.period_label)
    lines.append("")
    lines.append(f"新增注册        {report.new_registrations}")
    lines.append(f"  已归因        {report.attributed}")
    lines.append(f"    · 实验关联  {report.experiment_associated}")
    lines.append(f"    · 待裁定    {report.ambiguous}")
    lines.append(f"  未归因        {report.unattributed}")
    lines.append(f"  批量账户(审计) {report.batch_audit}")
    lines.append("")

    lines.append("带来注册关联的实验")
    if report.experiment_breakdown:
        for _eid, (name, ch, count) in sorted(
            report.experiment_breakdown.items(), key=lambda kv: (-kv[1][2], kv[0])
        ):
            ch_label = CHANNEL_OWNER_LABEL.get(ch, CHANNEL_OWNER_LABEL["other"])
            lines.append(f"  《{name}》    {ch_label}    实验关联 {count} 人")
    else:
        lines.append(f"  {NO_DATA}")
    lines.append("")

    sample_note = SMALL_SAMPLE_NOTE if report.small_sample else "可参考"
    lines.append(f"主题表现（{sample_note}）")
    lines.append(f"  {NO_DATA}")
    lines.append("")
    lines.append(f"钩子表现（{sample_note}）")
    lines.append(f"  {NO_DATA}")
    lines.append("")

    lines.append("下游观测（本阶段不优化）")
    lines.append(f"  登录        {report.login_count} 人")
    lines.append(f"  充值        {report.recharge_people} 人 / ¥{report.recharge_sum}")
    lines.append(f"  佣金        {report.commission}")
    lines.append("")

    return "\n".join(lines)
