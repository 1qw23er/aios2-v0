"""AIOS V0 — LOCAL 模式真跑一个任务，验证 DeepSeek 通过 AIOS 执行协议产出结果。

这是"让 AIOS 真正干活"的最小可跑示例。它走的是与生产 HTTP 端点
``POST /tasks/{id}/execute`` **完全相同**的执行路径：

    create_project -> register_agent(MODEL) -> create_task(assigned) -> 置 READY
    -> execute_task(session, task_id, key, adapter=LLMExecutionAdapter())

唯一与生产不同的是：本脚本用包内 service 函数直接驱动（省去 HTTP/campaign 编排），
并显式把任务置为 READY —— 因为本仓库的 orchestrator 只为**有依赖**的下游任务
自动置 READY，无依赖的根任务不会自动激活（见 src/aios/orchestrator.py:95-161 与
src/aios/services.py:152 create_task 默认 BACKLOG）。生产里这一步由 campaign 启动完成。

前置：先设置环境变量（AIOS 不吃 .env，必须 export）：
    AIOS_AGENT_BASE_URL  = https://api.deepseek.com/v1
    AIOS_AGENT_MODEL     = deepseek-chat
    AIOS_AGENT_API_KEY   = sk-你的DeepSeekKey
    AIOS_DATABASE_URL    = sqlite:///./data/aios_local_demo.db   # 可独立库

然后：  .venv\\Scripts\\python.exe scripts/demo_local_deepseek.py
"""
from __future__ import annotations

import os
import sys
import uuid

from sqlmodel import Session

from aios.actor import ActorContext
from aios.agent_registry import register_agent
from aios.db import get_engine, run_migrations
from aios.execution import LLMExecutionAdapter, execute_task
from aios.models import TaskStatus
from aios.schemas import ProjectCreate, TaskCreate
from aios.services import create_project, create_task

# 任务指令：要求模型返回 JSON（LLMExecutionAdapter.run 会解析 JSON 结果）。
TASK_TITLE = "用一句话解释什么是上下文工程"
TASK_DESC = (
    "你是一名资深 AI 工程师。请用简体中文回答，并以 JSON 返回："
    '{"answer": "<一句话>, "level": "<beginner|intermediate|advanced>"}。'
    "只返回 JSON，不要额外解释。"
)


def main() -> int:
    run_id = uuid.uuid4().hex[:8]  # 每次运行用唯一幂等键，避免重跑 409 冲突
    db_url = os.environ.get("AIOS_DATABASE_URL", "sqlite:///./data/aios_local_demo.db")
    if not os.environ.get("AIOS_AGENT_API_KEY") or "REPLACE" in os.environ.get("AIOS_AGENT_API_KEY", ""):
        print("[ERROR] 请先设置真实 AIOS_AGENT_API_KEY（DeepSeek sk-...）。AIOS 不吃 .env。")
        return 2
    if not os.environ.get("AIOS_AGENT_BASE_URL"):
        os.environ["AIOS_AGENT_BASE_URL"] = "https://api.deepseek.com/v1"
    if not os.environ.get("AIOS_AGENT_MODEL"):
        os.environ["AIOS_AGENT_MODEL"] = "deepseek-chat"

    print(f"[1/5] 初始化数据库（迁移到 head）：{db_url}")
    run_migrations(db_url)
    engine = get_engine(db_url)

    with Session(engine) as s:
        print("[2/5] 创建项目 + LOCAL(MODEL) agent")
        project = create_project(
            s, ProjectCreate(name="demo-project", objective="验证 DeepSeek 执行"), f"demo-proj-{run_id}"
        )
        # MODEL 适配器 = 进程内 LLM，不声明 delegation_mode（见 agent_registry.py:152-158）。
        agent = register_agent(
            s,
            name="deepseek-worker",
            role="general-worker",
            adapter_type="model",
            capabilities=[],
            actor=ActorContext(kind="owner", owner_id="demo"),
        )
        print(f"      project={project.id}  agent={agent.id}")

        print("[3/5] 创建任务并置 READY（根任务不会自动激活）")
        task = create_task(
            s,
            TaskCreate(
                project_id=project.id,
                title=TASK_TITLE,
                description=TASK_DESC,
                assigned_agent_id=agent.id,
                routing_mode="fixed",
            ),
            f"demo-task-{run_id}",
        )
        task.status = TaskStatus.READY
        s.add(task)
        s.commit()
        print(f"      task={task.id}  status={task.status.value}")

        print("[4/5] 通过 AIOS 执行协议真实调用 DeepSeek（LLMExecutionAdapter）...")
        artifact = execute_task(
            s, task.id, f"demo-exec-{run_id}", adapter=LLMExecutionAdapter(), actor="agent"
        )
        print("[5/5] 执行完成，产出 Artifact：")
        print(artifact.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
