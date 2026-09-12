#!/usr/bin/env python3
"""AIOS 真实外部 Agent 接入 Pilot (Phase 2-6) -- 库级别端到端验证。

设计要点（严格遵循 Pilot 规范纪律）:
  * 使用**独立 DB** `uat_v1_2.db`，绝不污染主库，且不提交该文件。
  * 密钥仅从本地 `C:/Users/Administrator/.aios_local_env.ps1` 读取并注入进程 env，
    绝不打印、绝不以任何形式入库。
  * 不 push / 不 PR / 不 merge / 不 stash / 不 git add -A / 不删远程分支。
  * 直接复用生产代码路径: route_task -> build_execution_adapter -> execute_task
    （与 HTTP 端点完全一致的调用顺序），搬运工后台线程驱动 WORKSTATION 文件协议。

执行: python scripts/pilot_external_agent.py --phase all
"""
# ruff: noqa: E402  # sys.path 注入在 import 之前（脚本需在 import aios 前把 src/ 加入路径），E402 为预期
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import threading
import urllib.request
import uuid
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
for _p in (str(_REPO / "src"), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sqlmodel import Session, select  # noqa: E402

from aios.adapters.external import WorkstationAdapter  # noqa: E402
from aios.adapters.factory import build_execution_adapter  # noqa: E402
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
    DelegatedRunStatus,
    DelegationMode,
    Project,
    RoutingMode,
    Task,
    TaskStatus,
)
from aios.scheduler import route_task  # noqa: E402
from scripts.workstation_runner import (  # noqa: E402
    TaskPacket,
    build_executor_for_platform,
    process_task,
)
from scripts.workstation_runner import (
    run as runner_run,
)

LOCAL_ENV = Path(r"C:/Users/Administrator/.aios_local_env.ps1")
OUTBOX = "D:/wb_tmp/pilot_outbox"
INBOX = "D:/wb_tmp/pilot_inbox"
PILOT_DB = "sqlite:///D:/wb_tmp/uat_v1_2.db"
SMARTROUTER_URL = "http://47.90.161.151:8768/v1"


def _load_local_env_file() -> None:
    """读取本地密钥文件并注入进程 env（绝不打印）。"""
    if not LOCAL_ENV.exists():
        return
    txt = LOCAL_ENV.read_text(encoding="utf-8")
    for m in re.finditer(r'\$env:(\w+)\s*=\s*"([^"]*)"', txt):
        os.environ.setdefault(m.group(1), m.group(2))


def _ensure_pilot_env() -> None:
    _load_local_env_file()
    # 强制隔离：本地 env 文件里的 AIOS_DATABASE_URL 指向主库，必须用 `=` 覆盖，
    # 不能用 setdefault（否则被主库 URL 抢走 -> 污染主库）。Pilot 永远只跑独立 DB。
    os.environ["AIOS_DATABASE_URL"] = PILOT_DB
    os.environ["AIOS_EXTERNAL_DELEGATION_ENABLED"] = "true"
    os.environ["AIOS_WORKSTATION_OUTBOX"] = OUTBOX
    os.environ["AIOS_WORKSTATION_INBOX"] = INBOX
    os.environ["AIOS_WS_DEFAULT_PLATFORM"] = "smartrouter"
    os.environ.setdefault("AIOS_WS_SMARTROUTER_BASE_URL", SMARTROUTER_URL)
    os.environ.setdefault("AIOS_WS_SMARTROUTER_MODEL", "deepseek-v4-flash")
    # 第二 Agent 复用同一 8768 端点（不同 identity / capability / role），真实外呼。
    os.environ.setdefault("AIOS_WS_DEEPSEEK_BASE_URL", SMARTROUTER_URL)
    os.environ.setdefault("AIOS_WS_DEEPSEEK_MODEL", "deepseek-v4-flash")
    # 8768 Bearer 鉴权 key：本地 env 文件只定义了 AIOS_AGENT_API_KEY；搬运工按平台读
    # AIOS_WS_<PLATFORM>_API_KEY，必须把 key 映射过去，否则无鉴权 POST 8768 -> 401
    # -> 写 .error 哨兵 -> 但 WorkstationAdapter.status() 只查 result.json -> 永久等待。
    _sr_key = os.environ.get("AIOS_AGENT_API_KEY", "")
    if _sr_key:
        os.environ.setdefault("AIOS_WS_SMARTROUTER_API_KEY", _sr_key)
        os.environ.setdefault("AIOS_WS_DEEPSEEK_API_KEY", _sr_key)
    Path(OUTBOX).mkdir(parents=True, exist_ok=True)
    Path(INBOX).mkdir(parents=True, exist_ok=True)
    # 每次运行前清空搬运工 scratch 目录，保证运行间隔离：避免上一轮被杀留下的
    # .error/.done 任务目录干扰本轮轮询（虽然 runner 会跳过它们，但清掉更干净、
    # 且能让本轮只看当前 run 的产物）。只动 Pilot 自有 scratch（D:/wb_tmp），不碰主库。
    _reset_scratch()


def _reset_scratch() -> None:
    """清空搬运工 outbox/inbox 的遗留内容（仅 Pilot 自有 scratch，绝不动主库）。"""
    for d in (Path(OUTBOX), Path(INBOX)):
        if not d.exists():
            continue
        for item in d.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            except OSError:
                pass


def _health_ok() -> bool:
    try:
        req = urllib.request.Request("http://47.90.161.151:8768/health")
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Phase 2: 注册真实 Agent + Capability
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


def phase2_register(session: Session) -> dict:
    for cid, name in [
        ("cap:wechat_writing", "wechat_writing"),
        ("cap:packaging", "packaging"),
        ("cap:video_script", "video_script"),
        ("cap:xhs_adaptation", "xhs_adaptation"),
        ("cap:offline_probe", "offline_probe"),
    ]:
        _upsert_capability(session, cid, name)

    # Agent 1: SmartRouter (8768 真实带鉴权 LLM 服务)
    if session.get(Agent, "agt:smartrouter") is None:
        session.add(
            Agent(
                id="agt:smartrouter",
                name="SmartRouter Agent",
                role="wechat_writing",
                adapter_type=AdapterType.EXTERNAL,
                delegation_mode=DelegationMode.WORKSTATION,
                endpoint=SMARTROUTER_URL,
                platform="smartrouter",
                trust_level=AgentTrustLevel.INTERNAL,
                enabled=True,
                status=AgentStatus.AVAILABLE,
                timeout_s=120.0,
            )
        )
    _upsert_agent_cap(session, "agt:smartrouter", "cap:wechat_writing", 95)
    _upsert_agent_cap(session, "agt:smartrouter", "cap:packaging", 85)
    session.commit()
    return {
        "agents": ["agt:smartrouter"],
        "capabilities": [
            "cap:wechat_writing", "cap:packaging", "cap:video_script", "cap:xhs_adaptation"
        ],
    }


def register_deepseek(session: Session) -> None:
    """Phase 6 第二 Agent：不同 identity / capability / role，真实外呼 8768。"""
    if session.get(Agent, "agt:deepseek_official") is None:
        session.add(
            Agent(
                id="agt:deepseek_official",
                name="DeepSeek Official Agent",
                role="video_script",
                adapter_type=AdapterType.EXTERNAL,
                delegation_mode=DelegationMode.WORKSTATION,
                endpoint=SMARTROUTER_URL,
                platform="deepseek",
                trust_level=AgentTrustLevel.INTERNAL,
                enabled=True,
                status=AgentStatus.AVAILABLE,
                timeout_s=120.0,
            )
        )
    _upsert_agent_cap(session, "agt:deepseek_official", "cap:video_script", 95)
    _upsert_agent_cap(session, "agt:deepseek_official", "cap:xhs_adaptation", 90)
    session.commit()


# --------------------------------------------------------------------------- #
# Phase 3: 独立 Pilot Project + BEST_AVAILABLE Task
# --------------------------------------------------------------------------- #
WECHAT_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "body_markdown": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": [],
}
VIDEO_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "script": {"type": "string"},
        "hook": {"type": "string"},
    },
    "required": [],
}


def phase3_create(session: Session) -> dict:
    proj = Project(name="Pilot-ExternalAgent", objective="验证真实外部 Agent 接入全链路")
    session.add(proj)
    session.commit()
    session.refresh(proj)
    return {"project_id": proj.id}


def _make_task(session: Session, project_id: str, *, caps, title: str, schema: dict) -> Task:
    t = Task(
        project_id=project_id,
        title=title,
        description=title,
        status=TaskStatus.READY,
        required_capabilities=list(caps),
        routing_mode=RoutingMode.BEST_AVAILABLE,
        output_schema=schema,
        acceptance_criteria=["产出符合 output_schema 的 JSON"],
    )
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


# --------------------------------------------------------------------------- #
# Phase 4: 端到端执行验证
# --------------------------------------------------------------------------- #
def _e2e_execute(session: Session, task: Task, route_key: str, exec_key: str) -> dict:
    """route -> build adapter -> 后台搬运工 -> execute_task -> 断言。"""
    # 每次调用生成唯一 nonce，避免重跑时与历史 ExecutionAssignment / Artifact 的
    # idempotency key 冲突（route_task / execute_task 都按 key 做强约束）。
    nonce = uuid.uuid4().hex[:10]
    rk = f"{route_key}:{nonce}"
    ek = f"{exec_key}:{nonce}"
    assignment = route_task(session, task.id, rk)
    assert assignment is not None, "route_task returned None (no capable agent)"
    selected = assignment.selected_agent_id
    session.commit()

    adapter = build_execution_adapter(session, task.id)
    assert isinstance(adapter, WorkstationAdapter), (
        f"adapter is {type(adapter).__name__}, expected WorkstationAdapter (真实外部通道)"
    )
    assert adapter.agent.id == selected, "adapter agent 与 route 选中不一致"
    print(
        f"[pilot] route -> {selected}; adapter={type(adapter).__name__} "
        f"(outbox={OUTBOX} inbox={INBOX}); 启动搬运工线程",
        flush=True,
    )

    stop = threading.Event()
    runner_thread = threading.Thread(
        target=lambda: runner_run(
            outbox=Path(OUTBOX), inbox=Path(INBOX), mode="llm", interval=2.0, once=False
        ),
        daemon=True,
    )
    runner_thread.start()

    print(f"[pilot] execute_task({task.id}) 阻塞等待外部结果...", flush=True)
    artifact = execute_task(session, task.id, ek, adapter=adapter)
    stop.set()
    print("[pilot] execute_task 返回", flush=True)

    run = session.exec(
        select(DelegatedRun).where(DelegatedRun.task_id == task.id)
    ).first()
    assert run is not None, "DelegatedRun 未创建"
    assert run.status == DelegatedRunStatus.SUCCEEDED, f"run status={run.status}"

    from aios.audit import AuditLog

    routing_audit = session.exec(
        select(AuditLog).where(
            AuditLog.action == "routing.selected", AuditLog.task_id == task.id
        )
    ).first()

    proj = session.get(Project, task.project_id)
    artifact_data = (artifact.metadata_json or {}).get("artifacts", [{}])[0].get("data", {})
    return {
        "task_id": task.id,
        "route_selected_agent": selected,
        "adapter_mode": adapter.mode.value,
        "artifact_id": artifact.id,
        "artifact_type": artifact.type.value,
        "artifact_data_keys": sorted(artifact_data.keys()),
        "run_status": run.status.value,
        "run_remote_status": run.remote_status,
        "has_provenance": bool(artifact.provenance_json),
        "provenance_mode": (artifact.provenance_json or {}).get("mode"),
        "audit_routing_selected": routing_audit is not None,
        "audit_selected_agent": (routing_audit.after_snapshot or {}).get("selected_agent_id")
        if routing_audit
        else None,
        "project_budget_used": float(proj.budget_used),
        "task_status": task.status.value,
    }


# --------------------------------------------------------------------------- #
# Phase 5: 失败路径验证（不可达端点 -> 错误哨兵 + 不重复处理 + 密钥不泄露）
# --------------------------------------------------------------------------- #
def _phase5_failure(session: Session) -> dict:
    # 注册一个指向离线端口的坏 Agent（priority 高于 smartrouter，验证会被 route 优先选中）
    if session.get(Agent, "agt:bad") is None:
        session.add(
            Agent(
                id="agt:bad",
                name="Bad Offline Agent",
                role="wechat_writing",
                adapter_type=AdapterType.EXTERNAL,
                delegation_mode=DelegationMode.WORKSTATION,
                platform="bad",
                trust_level=AgentTrustLevel.INTERNAL,
                enabled=True,
                status=AgentStatus.AVAILABLE,
                timeout_s=15.0,
            )
        )
    _upsert_agent_cap(session, "agt:bad", "cap:offline_probe", 99)
    session.commit()
    os.environ["AIOS_WS_BAD_BASE_URL"] = "http://127.0.0.1:9/v1"
    os.environ["AIOS_WS_BAD_API_KEY"] = "should-not-leak"
    os.environ["AIOS_WS_BAD_MODEL"] = "m"

    pid = session.exec(select(Project).where(Project.name == "Pilot-ExternalAgent")).first().id
    t = _make_task(
        session, pid, caps=["cap:offline_probe"], title="P5-失败路径", schema=WECHAT_SCHEMA
    )
    assignment = route_task(session, t.id, f"pilot:p5:route:{uuid.uuid4().hex[:10]}")
    route_selected = assignment.selected_agent_id
    session.commit()

    # (a) 搬运工对离线端点的错误处理：直接构造离线 task 包 + process_task
    bad_dir = Path(OUTBOX) / t.id
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / ".platform").write_text("bad", encoding="utf-8")
    packet = TaskPacket(
        task_id=t.id,
        project={"id": pid},
        role="wechat_writing",
        instructions="produce x",
        inputs=[],
        acceptance_criteria=["has x"],
        output_schema=WECHAT_SCHEMA,
    )
    (bad_dir / "task_packet.json").write_text(packet.model_dump_json(indent=2), encoding="utf-8")
    (bad_dir / "output_schema.json").write_text(json.dumps(WECHAT_SCHEMA), encoding="utf-8")
    (bad_dir / "context.md").write_text("# ctx", encoding="utf-8")

    executor = build_executor_for_platform("bad", mode="llm")
    try:
        process_task(bad_dir, Path(INBOX), executor)
        raised = False
    except Exception:
        raised = True
    error_sentinel = (bad_dir / ".error").exists()
    error_text = (bad_dir / ".error").read_text(encoding="utf-8") if error_sentinel else ""
    secret_leak = any(k in error_text for k in ("sk-", "jy-uz", "should-not-leak"))

    # (b) 不重复处理：.error 哨兵存在时 discover_pending 应跳过
    from scripts.workstation_runner import discover_pending

    still_pending = discover_pending(Path(OUTBOX))
    no_duplicate = t.id not in {d.name for d in still_pending}

    return {
        "route_selected_agent": route_selected,
        "route_selected_offline_agent": route_selected == "agt:bad",
        "executor_raised_on_offline": raised,
        "error_sentinel_present": error_sentinel,
        "error_text_excerpt": error_text[:140],
        "secret_not_leaked": not secret_leak,
        "no_duplicate_processing": no_duplicate,
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def _project_id(session: Session, report: dict) -> str:
    if "phase3" in report.get("phases", {}):
        return report["phases"]["phase3"]["project_id"]
    proj = session.exec(select(Project).where(Project.name == "Pilot-ExternalAgent")).first()
    return proj.id


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="all", help="2,3,4,5,6,all")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    _ensure_pilot_env()
    if not _health_ok():
        print(
            json.dumps(
                {"fatal": "8768 smart_router /health 不可达，无法跑真实外呼 E2E"},
                ensure_ascii=False,
            )
        )
        return 2

    run_migrations()
    report: dict = {"baseline": "main@d5aa6d7 (含 PR#46)", "phases": {}}

    with make_session() as session:
        phase = args.phase
        if phase in ("2", "all"):
            report["phases"]["phase2"] = phase2_register(session)
        if phase in ("3", "all"):
            report["phases"]["phase3"] = phase3_create(session)
        if phase in ("4", "all"):
            pid = _project_id(session, report)
            t4 = _make_task(
                session,
                pid,
                caps=["cap:wechat_writing"],
                title="Pilot-微信文章",
                schema=WECHAT_SCHEMA,
            )
            report["phases"]["phase4"] = _e2e_execute(
                session, t4, "pilot:p4:route", "pilot:p4:exec"
            )
        if phase in ("5", "all"):
            report["phases"]["phase5"] = _phase5_failure(session)
        if phase in ("6", "all"):
            pid = _project_id(session, report)
            register_deepseek(session)
            _n6 = uuid.uuid4().hex[:10]
            tA = _make_task(
                session,
                pid,
                caps=["cap:wechat_writing"],
                title="P6-A-微信",
                schema=WECHAT_SCHEMA,
            )
            aA = route_task(session, tA.id, f"pilot:p6:a:route:{_n6}")
            tB = _make_task(
                session,
                pid,
                caps=["cap:video_script"],
                title="P6-B-短视频",
                schema=VIDEO_SCHEMA,
            )
            aB = route_task(session, tB.id, f"pilot:p6:b:route:{_n6}")
            rA = _e2e_execute(
                session, tA, "pilot:p6:a:route2", "pilot:p6:a:exec"
            )
            rB = _e2e_execute(session, tB, "pilot:p6:b:route2", "pilot:p6:b:exec")
            report["phases"]["phase6"] = {
                "route_A_selected": aA.selected_agent_id,
                "route_B_selected": aB.selected_agent_id,
                "routed_by_capability_not_role": (
                    aA.selected_agent_id == "agt:smartrouter"
                    and aB.selected_agent_id == "agt:deepseek_official"
                ),
                "e2e_A": rA,
                "e2e_B": rB,
            }

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
