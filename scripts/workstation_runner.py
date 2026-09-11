"""Workstation delegation runner -- the "搬运工" daemon (PR-3, Agent Interop Gateway #57).

AIOS 把 WORKSTATION 类外部 agent 的任务写成文件包（outbox/{task_id}/task_packet.json
+ output_schema.json + context.md），等外部 agent 把结果放回 inbox/{task_id}.result.json。
本守护进程就是把这条「写出去 / 收回来」的链路真正跑起来：

  poll outbox/  -> 发现待处理任务  -> 按 agent.platform 路由到对应执行器
  -> 调用外部 agent（LLM 执行器 / 人工中转）-> 产出符合 output_schema 的 artifact data
  -> 写回 inbox/{task_id}.result.json（ExternalResult）-> 标记 .done 哨兵

这样 AIOS 的 execute_task 路径就能异步、真实地把任务委派给「另一个 agent」并收回结果，
无需改 execute_task / orchestrator / DAG —— 多外部真实 agent 协同由「每 task 指向不同
外部 agent + orchestrator 调度」自然涌现。

执行器是可插拔的：
  * LLMExecutor  —— 调 OpenAI 兼容 /v1/chat/completions（默认接 workbuddy / marvis 平台
                    各自的端点），产出符合 output_schema 的 JSON，是最容易跑通的「真·外部 agent」。
  * ManualExecutor —— 把任务包打印给操作员，等操作员手动把结果文件放回 inbox（适合完全封闭的
                    无 API agent）。此时写 .pending_manual 哨兵，不阻塞轮询。

本文件既可作为守护进程运行（``python scripts/workstation_runner.py``），也可被测试导入
（``from scripts.workstation_runner import process_task``）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

# 让脚本在「没有把 aios 装进环境」时也能 import（pytest 已通过 pythonpath=["src","."] 处理）。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from aios.adapters.external import ExternalResult, TaskPacket  # noqa: E402

logger = logging.getLogger("workstation_runner")


class Executor(Protocol):
    """外部 agent 执行器：给定任务包 + 上下文 + 输出 schema，返回 artifact 的 data dict。

    data 必须能通过 jsonschema 校验（WorkstationAdapter.ingest_result 会再校验一次）。
    """

    def __call__(
        self, *, task: TaskPacket, context: str, output_schema: dict[str, Any]
    ) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# 结果文件构造
# --------------------------------------------------------------------------- #
def build_result(
    task_id: str,
    artifact_data: dict[str, Any],
    *,
    summary: str = "",
    claims: list[dict[str, Any]] | None = None,
    artifact_uri: str = "",
) -> ExternalResult:
    """把执行器产出的 artifact data 包成 ExternalResult。"""
    return ExternalResult(
        result_id=f"ws-{uuid.uuid4().hex}",
        task_id=task_id,
        summary=summary or f"workstation result for {task_id}",
        claims=claims or [],
        artifacts=[{"uri": artifact_uri, "data": artifact_data}],
    )


# --------------------------------------------------------------------------- #
# LLM 执行器（OpenAI 兼容端点）
# --------------------------------------------------------------------------- #
def make_llm_executor(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 120.0,
) -> Executor:
    """构造一个调 OpenAI 兼容 /v1/chat/completions 的执行器。

    强约束：要求模型只输出符合 output_schema 的 JSON（json_object 模式）；解析失败时抛
    出 RuntimeError，由 process_task 标记为 .error 不阻塞轮询。
    """
    import urllib.request

    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    auth = f"Bearer {api_key}" if api_key else ""

    def _exec(
        *, task: TaskPacket, context: str, output_schema: dict[str, Any]
    ) -> dict[str, Any]:
        user_prompt = (
            f"# 角色\n{task.role}\n\n"
            f"# 任务指令\n{task.instructions}\n\n"
            f"# 上游输入\n{json.dumps(task.inputs, ensure_ascii=False, indent=2)}\n\n"
            f"# 验收标准\n{json.dumps(task.acceptance_criteria, ensure_ascii=False, indent=2)}\n\n"
            f"# 上下文\n{context}\n\n"
            f"# 必须严格输出的 JSON Schema\n"
            f"{json.dumps(output_schema, ensure_ascii=False, indent=2)}\n\n"
            "只输出一个符合上述 schema 的 JSON 对象，不要任何 markdown 代码围栏或额外文字。"
        )
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是被 AIOS 委派的外部 AI agent。"
                        "只输出符合给定 JSON Schema 的 JSON 对象。"
                    ),
                },
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": auth} if auth else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"LLM executor request failed: {exc}") from exc
        content = body["choices"][0]["message"]["content"]
        try:
            return json.loads(content)
        except (KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"LLM executor returned unparsable JSON: {exc}") from exc

    return _exec


# --------------------------------------------------------------------------- #
# 人工中转执行器（无 API 的封闭 agent 的兜底）
# --------------------------------------------------------------------------- #
class AwaitingManualResult(RuntimeError):
    """表示该任务需操作员手动放回结果文件，守护进程不应自动写 .done。"""


def make_manual_executor() -> Executor:
    """把任务包打印给操作员，交由人把结果文件放回 inbox。

    抛 AwaitingManualResult 让 process_task 写 .pending_manual 哨兵、不阻塞其它任务。
    """

    def _exec(
        *, task: TaskPacket, context: str, output_schema: dict[str, Any]
    ) -> dict[str, Any]:
        logger.info(
            "MANUAL task %s (%s): put result at inbox/%s.result.json per output_schema",
            task.task_id,
            task.role,
            task.task_id,
        )
        raise AwaitingManualResult(task.task_id)

    return _exec


# --------------------------------------------------------------------------- #
# 平台路由
# --------------------------------------------------------------------------- #
def build_executor_for_platform(platform: str | None, *, mode: str = "llm") -> Executor:
    """按 agent.platform 选择执行器。

    mode="manual" 时无论平台一律走人工中转（适合完全封闭的 agent）；
    mode="llm" 时 workbuddy / marvis 走各自端点（env 配置），未知平台回落人工中转。
    """
    if mode == "manual" or not platform:
        return make_manual_executor()
    prefix = f"AIOS_WS_{platform.upper()}"
    base_url = os.getenv(f"{prefix}_BASE_URL", "")
    api_key = os.getenv(f"{prefix}_API_KEY", "")
    model = os.getenv(f"{prefix}_MODEL", "")
    if base_url and model:
        return make_llm_executor(base_url=base_url, api_key=api_key, model=model)
    logger.warning("platform %s has no LLM endpoint configured; falling back to manual", platform)
    return make_manual_executor()


# --------------------------------------------------------------------------- #
# 目录扫描与单任务处理
# --------------------------------------------------------------------------- #
def discover_pending(outbox: Path) -> list[Path]:
    """返回 outbox 下所有「有待处理任务包、且未处理过」的任务目录。"""
    if not outbox.exists():
        return []
    pending: list[Path] = []
    for task_dir in sorted(outbox.iterdir()):
        if not task_dir.is_dir():
            continue
        if not (task_dir / "task_packet.json").exists():
            continue
        if (task_dir / ".done").exists() or (task_dir / ".pending_manual").exists():
            continue
        if (task_dir / ".error").exists():
            continue
        pending.append(task_dir)
    return pending


def process_task(outbox_task_dir: Path, inbox: Path, executor: Executor) -> Path:
    """处理单个 outbox 任务目录：调用执行器，写回 inbox 结果，标记哨兵。

    返回写出的结果文件路径。执行器抛 AwaitingManualResult -> 写 .pending_manual；
    其它异常 -> 写 .error（不无限重试）。
    """
    task_id = outbox_task_dir.name
    packet = TaskPacket.model_validate_json(
        (outbox_task_dir / "task_packet.json").read_text(encoding="utf-8")
    )
    schema_path = outbox_task_dir / "output_schema.json"
    output_schema = (
        json.loads(schema_path.read_text(encoding="utf-8"))
        if schema_path.exists()
        else packet.output_schema
    )
    context_path = outbox_task_dir / "context.md"
    context = context_path.read_text(encoding="utf-8") if context_path.exists() else ""

    try:
        artifact_data = executor(task=packet, context=context, output_schema=output_schema)
    except AwaitingManualResult:
        (outbox_task_dir / ".pending_manual").write_text(task_id, encoding="utf-8")
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("task %s executor failed", task_id)
        (outbox_task_dir / ".error").write_text(str(exc), encoding="utf-8")
        raise

    result = build_result(task_id, artifact_data)
    inbox.mkdir(parents=True, exist_ok=True)
    result_path = inbox / f"{task_id}.result.json"
    result_path.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (outbox_task_dir / ".done").write_text(result.result_id, encoding="utf-8")
    logger.info("task %s -> wrote result %s", task_id, result_path)
    return result_path


# --------------------------------------------------------------------------- #
# 守护进程主循环
# --------------------------------------------------------------------------- #
def run(
    *,
    outbox: Path,
    inbox: Path,
    mode: str = "llm",
    interval: float = 5.0,
    once: bool = False,
) -> int:
    """轮询 outbox 并处理待处理任务。返回处理成功的任务数。"""
    outbox.mkdir(parents=True, exist_ok=True)
    inbox.mkdir(parents=True, exist_ok=True)
    handled = 0
    while True:
        for task_dir in discover_pending(outbox):
            # 每个任务目录的平台取自 packet.role 不可靠；用 platform 字段需 Agent 表，
            # 故这里按目录名无法直接拿 platform。runner 通过 env 默认平台 + 目录级
            # override（outbox/{task_id}/.platform）决定路由。
            platform = _platform_for(task_dir)
            executor = build_executor_for_platform(platform, mode=mode)
            try:
                process_task(task_dir, inbox, executor)
                handled += 1
            except AwaitingManualResult:
                continue
            except Exception:  # noqa: BLE001
                continue
        if once:
            break
        time.sleep(interval)
    return handled


def _platform_for(task_dir: Path) -> str | None:
    """任务目录级的平台 override；缺省读 env 默认平台。"""
    override = task_dir / ".platform"
    if override.exists():
        return override.read_text(encoding="utf-8").strip() or None
    return os.getenv("AIOS_WS_DEFAULT_PLATFORM") or None


def _resolve_dirs_from_env() -> tuple[Path, Path]:
    outbox = os.getenv("AIOS_WORKSTATION_OUTBOX")
    inbox = os.getenv("AIOS_WORKSTATION_INBOX")
    if not outbox or not inbox:
        raise SystemExit(
            "AIOS_WORKSTATION_OUTBOX / AIOS_WORKSTATION_INBOX must be set "
            "(same env AIOS uses for WorkstationAdapter)"
        )
    return Path(outbox), Path(inbox)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AIOS workstation delegation runner (搬运工)")
    parser.add_argument(
        "--outbox", type=Path, default=None, help="override AIOS_WORKSTATION_OUTBOX"
    )
    parser.add_argument(
        "--inbox", type=Path, default=None, help="override AIOS_WORKSTATION_INBOX"
    )
    parser.add_argument(
        "--mode",
        choices=["llm", "manual"],
        default=os.getenv("AIOS_WS_MODE", "llm"),
        help="llm=route to platform endpoints; manual=operator drops result files",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.getenv("AIOS_WS_POLL_INTERVAL", "5")),
    )
    parser.add_argument("--once", action="store_true", help="process pending once then exit")
    args = parser.parse_args(argv)

    if args.outbox and args.inbox:
        outbox, inbox = args.outbox, args.inbox
    else:
        outbox, inbox = _resolve_dirs_from_env()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger.info("workstation runner start: outbox=%s inbox=%s mode=%s", outbox, inbox, args.mode)
    return run(outbox=outbox, inbox=inbox, mode=args.mode, interval=args.interval, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
