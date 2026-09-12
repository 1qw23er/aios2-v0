#!/usr/bin/env python3
"""AIOS 真实双 Agent 连续业务任务 Pilot —— WorkBuddy（蟹将）与 Claude Code。

目标：把本机两个**真实封闭 agent CLI** 接进 AIOS 的 WORKSTATION 通道，并跑一条
**连续业务任务链**（公众号 -> 小红书 / 短视频 -> 矩阵汇总），验证：
  * capability SSoT + BEST_AVAILABLE 把不同任务分派到两个不同真实 agent
  * 依赖链自动激活（上游 DONE -> 下游 READY，由 Orchestrator 驱动）
  * 进程外执行（outbox/inbox 文件协议，CLI 真实产出）
  * Artifact 严格对齐 output_schema；provenance/audit 正常；budget 口径不变
  * 失败 fail-fast（PR #47 的 .error 语义）与**同任务恢复重跑**

纪律：
  * 独立 DB `uat_two_agents.db` + 独立 scratch，绝不污染主库
  * 不 push / 不 PR / 不 merge / 不 stash / 不删分支
  * 密钥只在仓库外，绝不打印

执行: python scripts/pilot_two_real_agents.py
"""
# ruff: noqa: E402
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
for _p in (str(_REPO / "src"), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sqlmodel import Session, select  # noqa: E402

from aios.adapters.external import WorkstationAdapter  # noqa: E402
from aios.adapters.factory import build_execution_adapter  # noqa: E402
from aios.audit import AuditLog  # noqa: E402
from aios.db import make_session, run_migrations  # noqa: E402
from aios.execution import execute_task  # noqa: E402
from aios.models import (  # noqa: E402
    AdapterType,
    Agent,
    AgentCapability,
    AgentStatus,
    AgentTrustLevel,
    Capability,
    DelegatedRun,
    DelegationMode,
    Project,
    RoutingMode,
    Task,
    TaskStatus,
    now_utc,
)
from aios.scheduler import route_task  # noqa: E402
from scripts.cli_agent_host import default_argv, run_host  # noqa: E402

LOCAL_ENV = Path(r"C:/Users/Administrator/.aios_local_env.ps1")
# 每次运行使用**独立 scratch 目录**（run-scoped）：天然隔离、零删除。
# 不用「先清空再跑」是因为本机沙箱对递归删除有 bulk-delete 保护，rmtree 会被拦下。
RUN_ID = time.strftime("%Y%m%d_%H%M%S")
OUTBOX = f"D:/wb_tmp/ta_outbox_{RUN_ID}"
INBOX = f"D:/wb_tmp/ta_inbox_{RUN_ID}"
PILOT_DB = "sqlite:///D:/wb_tmp/uat_two_agents.db"

WORKBUDDY = "agt:workbuddy"
CLAUDE = "agt:claude_code"
FLAKY = "agt:flaky"
FLAKY_PLATFORM = "flaky_cli"


# --------------------------------------------------------------------------- #
# 环境
# --------------------------------------------------------------------------- #
def _load_local_env_file() -> None:
    if not LOCAL_ENV.exists():
        return
    txt = LOCAL_ENV.read_text(encoding="utf-8")
    for m in re.finditer(r'\$env:(\w+)\s*=\s*"([^"]*)"', txt):
        os.environ.setdefault(m.group(1), m.group(2))


def _ensure_env() -> None:
    _load_local_env_file()
    # 强制覆盖（不能用 setdefault，否则被本地 env 的主库 URL 抢走 -> 污染主库）
    os.environ["AIOS_DATABASE_URL"] = PILOT_DB
    os.environ["AIOS_EXTERNAL_DELEGATION_ENABLED"] = "true"
    os.environ["AIOS_WORKSTATION_OUTBOX"] = OUTBOX
    os.environ["AIOS_WORKSTATION_INBOX"] = INBOX
    # 不再依赖全局默认平台：每个 task 目录都有 .platform（PR #47）
    os.environ.pop("AIOS_WS_DEFAULT_PLATFORM", None)
    Path(OUTBOX).mkdir(parents=True, exist_ok=True)
    Path(INBOX).mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #
def _upsert_capability(session: Session, cap_id: str, name: str) -> None:
    if session.get(Capability, cap_id) is None:
        session.add(Capability(id=cap_id, name=name))


def _upsert_agent_cap(session: Session, agent_id: str, cap_id: str, prio: int) -> None:
    pk = {"agent_id": agent_id, "capability_id": cap_id}
    if session.get(AgentCapability, pk) is None:
        session.add(
            AgentCapability(agent_id=agent_id, capability_id=cap_id, priority=prio, enabled=True)
        )


def _upsert_agent(
    session: Session,
    *,
    agent_id: str,
    name: str,
    role: str,
    platform: str,
    caps: list[tuple[str, int]],
) -> None:
    if session.get(Agent, agent_id) is None:
        session.add(
            Agent(
                id=agent_id,
                name=name,
                role=role,
                adapter_type=AdapterType.EXTERNAL,
                delegation_mode=DelegationMode.WORKSTATION,
                platform=platform,
                trust_level=AgentTrustLevel.INTERNAL,
                enabled=True,
                status=AgentStatus.AVAILABLE,
                timeout_s=600.0,
            )
        )
    for cap_id, prio in caps:
        _upsert_agent_cap(session, agent_id, cap_id, prio)


def register_agents(session: Session) -> dict:
    for cid, nm in [
        ("cap:wechat_writing", "wechat_writing"),
        ("cap:xhs_adaptation", "xhs_adaptation"),
        ("cap:video_script", "video_script"),
        ("cap:structured_extraction", "structured_extraction"),
        ("cap:flaky_probe", "flaky_probe"),
    ]:
        _upsert_capability(session, cid, nm)

    # 真实 Agent 1：WorkBuddy（蟹将）—— 走 CodeBuddy Code CLI
    _upsert_agent(
        session,
        agent_id=WORKBUDDY,
        name="WorkBuddy (蟹将)",
        role="wechat_writing",
        platform="workbuddy",
        caps=[
            ("cap:wechat_writing", 95),
            ("cap:video_script", 90),
            ("cap:structured_extraction", 80),
        ],
    )
    # 真实 Agent 2：Claude Code —— 走 claude CLI
    _upsert_agent(
        session,
        agent_id=CLAUDE,
        name="Claude Code",
        role="xhs_adaptation",
        platform="claude_code",
        caps=[("cap:xhs_adaptation", 95), ("cap:structured_extraction", 95)],
    )
    # 故障注入 Agent（恢复演练用）
    _upsert_agent(
        session,
        agent_id=FLAKY,
        name="Flaky CLI Agent",
        role="flaky_probe",
        platform=FLAKY_PLATFORM,
        caps=[("cap:flaky_probe", 99)],
    )
    session.commit()
    return {"agents": [WORKBUDDY, CLAUDE, FLAKY]}


# --------------------------------------------------------------------------- #
# 业务任务链定义（真实业务：AI觅 / 公众号黎叔AI创业实验室）
# --------------------------------------------------------------------------- #
WECHAT_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "body_markdown": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "body_markdown", "tags"],
}
XHS_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "body": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "body", "tags"],
}
VIDEO_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "hook": {"type": "string"},
        "script": {"type": "string"},
    },
    "required": ["title", "hook", "script"],
}
MATRIX_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "title": {"type": "string"},
                    "cta": {"type": "string"},
                },
                "required": ["channel", "title", "cta"],
            },
        },
    },
    "required": ["summary", "items"],
}
PROBE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "note": {"type": "string"}},
    "required": ["ok", "note"],
}


def make_task(
    session: Session,
    project_id: str,
    *,
    caps: list[str],
    title: str,
    instructions: str,
    schema: dict,
    depends_on: list[str] | None = None,
    status: TaskStatus = TaskStatus.BACKLOG,
) -> Task:
    t = Task(
        project_id=project_id,
        title=title,
        description=instructions[:200],
        status=status,
        required_capabilities=list(caps),
        routing_mode=RoutingMode.BEST_AVAILABLE,
        output_schema=schema,
        acceptance_criteria=["只输出符合 output_schema 的 JSON；中文；不说空话"],
        depends_on=list(depends_on or []),
    )
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


# --------------------------------------------------------------------------- #
# 执行 + 激活
# --------------------------------------------------------------------------- #
def execute_one(session: Session, task: Task, tag: str) -> dict:
    nonce = uuid.uuid4().hex[:10]
    # 可恢复失败：execute_task 支持用新幂等 key 重跑 FAILED 任务，但 route_task 只接受
    # READY，所以重投前必须先把 FAILED 复位为 READY（与 execute_task 的恢复语义一致）。
    if task.status == TaskStatus.FAILED:
        task.status = TaskStatus.READY
        task.updated_at = now_utc()
        session.add(task)
        session.commit()
        session.refresh(task)
    assignment = route_task(session, task.id, f"ta:{tag}:route:{nonce}")
    assert assignment is not None, f"{tag}: route_task 未选中任何 agent"
    selected = assignment.selected_agent_id
    session.commit()

    adapter = build_execution_adapter(session, task.id)
    assert isinstance(adapter, WorkstationAdapter), (
        f"{tag}: adapter={type(adapter).__name__}（期望 WorkstationAdapter）"
    )
    assert adapter.agent.id == selected, f"{tag}: adapter agent 与 route 不一致"

    started = time.time()
    artifact = execute_task(session, task.id, f"ta:{tag}:exec:{nonce}", adapter=adapter)
    elapsed = time.time() - started

    session.refresh(task)
    run = session.exec(select(DelegatedRun).where(DelegatedRun.task_id == task.id)).first()
    audit = session.exec(
        select(AuditLog).where(
            AuditLog.action == "routing.selected", AuditLog.task_id == task.id
        )
    ).first()
    proj = session.get(Project, task.project_id)
    data = (artifact.metadata_json or {}).get("artifacts", [{}])[0].get("data", {})

    # 下游激活：execute_task 内部**已经**调用 Orchestrator.process_pending()
    # （execution.py:443），所以事件在此刻已 PROCESSED。这里改为快照本任务完成后
    # 项目内各任务的状态，作为「依赖自动激活」的证据（比事后 process_pending 更准）。
    statuses = {
        t.title: t.status.value
        for t in session.exec(select(Task).where(Task.project_id == task.project_id)).all()
    }

    headline = data.get("headline") or data.get("title") or data.get("summary") or ""
    return {
        "task_id": task.id,
        "tag": tag,
        "route_selected_agent": selected,
        "adapter_mode": adapter.mode.value,
        "artifact_id": artifact.id,
        "artifact_keys": sorted(data.keys()),
        "run_status": run.status.value if run else None,
        "run_remote_status": run.remote_status if run else None,
        "provenance_mode": (artifact.provenance_json or {}).get("mode"),
        "audit_routing_selected": audit is not None,
        "budget_used": float(proj.budget_used),
        "task_status": task.status.value,
        "elapsed_s": round(elapsed, 1),
        "project_status_after": statuses,
        "content_preview": str(headline)[:80],
    }


def _failure_detail(session: Session, task: Task, exc: Exception) -> dict:
    session.refresh(task)
    run = session.exec(select(DelegatedRun).where(DelegatedRun.task_id == task.id)).first()
    err_file = Path(OUTBOX) / task.id / ".error"
    return {
        "task_id": task.id,
        "raised": type(exc).__name__,
        "task_status": task.status.value,
        "run_status": run.status.value if run else None,
        "error_sentinel": err_file.exists(),
        "error_excerpt": err_file.read_text(encoding="utf-8")[:160] if err_file.exists() else "",
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    _ensure_env()
    run_migrations()

    report: dict = {
        "pilot": "two-real-agents-continuous-business",
        "baseline": _git_head(),
        "scratch": {"run_id": RUN_ID, "outbox": OUTBOX, "inbox": INBOX, "db": PILOT_DB},
        "agents": {},
        "chain": [],
        "recovery": {},
    }
    stop = threading.Event()
    host = threading.Thread(
        target=run_host,
        kwargs={"outbox": Path(OUTBOX), "inbox": Path(INBOX), "interval": 1.5},
        daemon=True,
    )
    host.start()

    with make_session() as session:
        report["agents"] = register_agents(session)
        proj = Project(
            name="AI觅-内容矩阵-真实双Agent",
            objective="用两个真实外部 Agent（WorkBuddy/Claude Code）连续生产内容矩阵",
        )
        session.add(proj)
        session.commit()
        session.refresh(proj)
        pid = proj.id
        report["project_id"] = pid

        t1 = make_task(
            session,
            pid,
            caps=["cap:wechat_writing"],
            title="T1 公众号长文",
            instructions=(
                "写一篇 800-1200 字的公众号文章。主题：中小电商卖家如何用 AI 把商品主图与"
                "详情页文案做到「能过审、能转化」。读者=拼多多/抖店中小卖家，非技术背景。"
                "要求：第一人称「黎叔」口吻、开头有钩子、给 3 个今天就能照做的动作、不要空话套话。"
            ),
            schema=WECHAT_SCHEMA,
            status=TaskStatus.READY,
        )
        t2 = make_task(
            session,
            pid,
            caps=["cap:xhs_adaptation"],
            title="T2 小红书笔记（依赖 T1）",
            instructions=(
                "基于上游公众号文章，改写成 1 篇小红书笔记（≤600 字）。保留干货、换成小红书语感，"
                "标题带钩子与 emoji，正文分点，结尾给互动引导。"
            ),
            schema=XHS_SCHEMA,
            depends_on=[t1.id],
        )
        t3 = make_task(
            session,
            pid,
            caps=["cap:video_script"],
            title="T3 60 秒短视频口播脚本（依赖 T1）",
            instructions=(
                "基于上游公众号文章，写 1 条 60 秒短视频口播脚本："
                "3 秒钩子 + 3 个信息点 + 结尾引导。口语化，可直接照读。"
            ),
            schema=VIDEO_SCHEMA,
            depends_on=[t1.id],
        )
        t4 = make_task(
            session,
            pid,
            caps=["cap:structured_extraction"],
            title="T4 内容矩阵汇总（依赖 T2+T3）",
            instructions=(
                "汇总上游各渠道产物，产出内容矩阵：一句话总结 + 每个渠道的标题与 CTA。"
                "渠道至少覆盖 公众号 / 小红书 / 短视频。"
            ),
            schema=MATRIX_SCHEMA,
            depends_on=[t2.id, t3.id],
        )
        report["chain_plan"] = {
            "T1": [t1.id, "cap:wechat_writing -> workbuddy"],
            "T2": [t2.id, "cap:xhs_adaptation -> claude_code", "depends T1"],
            "T3": [t3.id, "cap:video_script -> workbuddy", "depends T1"],
            "T4": [t4.id, "cap:structured_extraction -> claude_code", "depends T2+T3"],
        }

        t_wall = time.time()
        for task, tag in [(t1, "T1"), (t2, "T2"), (t3, "T3"), (t4, "T4")]:
            try:
                report["chain"].append(execute_one(session, task, tag))
            except Exception as exc:  # noqa: BLE001
                # 单任务失败不中断整条链：记录失败证据后继续（后续任务仍会因依赖未满足而阻塞）
                report["chain"].append(
                    {"tag": tag, "task_id": task.id, "failure": _failure_detail(session, task, exc)}
                )
        report["chain_wall_s"] = round(time.time() - t_wall, 1)

        # ---------------- 恢复演练：故障注入 -> fail-fast -> 同任务重跑成功 ----------------
        rec: dict = {}
        os.environ["AIOS_CLI_" + FLAKY_PLATFORM.upper() + "_ARGV"] = json.dumps(
            ["D:/wb_tmp/no_such_cli_binary.exe"]
        )
        t5 = make_task(
            session,
            pid,
            caps=["cap:flaky_probe"],
            title="T5 恢复演练",
            instructions="输出 {\"ok\": true, \"note\": \"recovered\"}",
            schema=PROBE_SCHEMA,
            status=TaskStatus.READY,
        )
        rec["task_id"] = t5.id
        started = time.time()
        try:
            execute_one(session, t5, "R-fail")
            rec["unexpected_success"] = True
        except Exception as exc:  # noqa: BLE001
            rec["unexpected_success"] = False
            rec["failure"] = _failure_detail(session, t5, exc)
        rec["fail_fast_elapsed_s"] = round(time.time() - started, 1)

        # 运维恢复：修好执行器 + 隔离终止态哨兵（把 .error 改名而不是删除——
        # 本机沙箱对删除有 bulk guard；改名等价于运维「确认已知故障」后重投）
        os.environ["AIOS_CLI_" + FLAKY_PLATFORM.upper() + "_ARGV"] = json.dumps(
            default_argv("workbuddy")
        )
        err_path = Path(OUTBOX) / t5.id / ".error"
        if err_path.exists():
            err_path.rename(err_path.with_name(".error.acknowledged"))
        rec["error_marker_quarantined"] = not err_path.exists()

        # (a) 同一任务重试：execute_task 文档声称 FAILED「可恢复」，实测会撞
        #     delegated_run 幂等键唯一约束（真 GAP，见报告）。
        try:
            rec["same_task_retry"] = execute_one(session, t5, "R-retry")
            rec["same_task_retry_ok"] = True
        except Exception as exc:  # noqa: BLE001
            rec["same_task_retry_ok"] = False
            rec["same_task_retry_error"] = repr(exc)[:260]

        # (b) 今天可用的恢复路径：作为**新任务**重投（新 task_id -> 新 run 幂等键）
        t6 = make_task(
            session,
            pid,
            caps=["cap:flaky_probe"],
            title="T6 重投恢复",
            instructions="输出 {\"ok\": true, \"note\": \"redispatched\"}",
            schema=PROBE_SCHEMA,
            status=TaskStatus.READY,
        )
        try:
            rec["redispatch_result"] = execute_one(session, t6, "R-new")
            rec["redispatch_ok"] = True
        except Exception as exc:  # noqa: BLE001
            rec["redispatch_ok"] = False
            rec["redispatch_error"] = repr(exc)[:260]
        rec["recovered"] = bool(rec.get("redispatch_ok"))
        report["recovery"] = rec

    stop.set()
    report["host_invocations"] = "n/a (thread)"
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


def _git_head() -> str:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%h %s"],
            cwd=str(_REPO),
            capture_output=True,
            text=True,
            timeout=15,
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
