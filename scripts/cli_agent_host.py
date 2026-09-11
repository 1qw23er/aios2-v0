#!/usr/bin/env python3
"""AIOS CLI agent host —— 把本机两个「封闭 CLI agent」接进 WORKSTATION 通道。

与 `scripts/workstation_runner.py` **同契约**（同一批 outbox/inbox、同一批哨兵文件）：
  * 读 `outbox/{task_id}/task_packet.json` + `context.md` + `output_schema.json`
  * 按 `outbox/{task_id}/.platform` 选择执行器
  * 产出 `{task_id}.result.json` 写回 inbox，并在 outbox 打 `.done`
  * 失败写 `.error`（`WorkstationAdapter.status()` 会 fail-fast 终态化，见 PR #47）

与 runner 的差别只有一点：**执行器不是 HTTP LLM 端点，而是本机两个真实 agent CLI**
  * `workbuddy`   -> CodeBuddy Code CLI  (`@tencent-ai/codebuddy-code`, 即 WorkBuddy/蟹将)
  * `claude_code` -> Claude Code CLI
两者同为 headless print 模式（`-p --output-format json`），prompt 走 **stdin**（绕开
Windows 命令行长度上限），结果从 `result` 字段取出后再解析成 artifact data。

用法：
    python scripts/cli_agent_host.py --outbox <dir> --inbox <dir> [--once] [--interval 2]
也可被 import：`from scripts.cli_agent_host import run_host, make_cli_executor`

可覆盖的环境变量：
    AIOS_CLI_NODE            node 可执行文件（codebuddy CLI 需要）
    AIOS_CLI_CODEBUDDY_BIN   codebuddy CLI 入口
    AIOS_CLI_CLAUDE_BIN      claude CLI 入口
    AIOS_CLI_<PLATFORM>_ARGV 任意平台的 argv JSON 数组覆盖（用于自定义/故障注入）
    AIOS_CLI_TIMEOUT         单次 CLI 调用超时秒数（默认 300）
"""
# ruff: noqa: E402  # sys.path 注入在 import 之前，E402 为预期
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
for _p in (str(_REPO / "src"), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.workstation_runner import (  # noqa: E402
    TaskPacket,
    _platform_for,
    discover_pending,
    process_task,
)

logger = logging.getLogger("cli_agent_host")

# --------------------------------------------------------------------------- #
# 默认 CLI 位置（本机实测路径；均可用 env 覆盖）
# --------------------------------------------------------------------------- #
DEFAULT_NODE = r"C:/Users/Administrator/.workbuddy/binaries/node/versions/22.22.2-3/node.exe"
DEFAULT_CODEBUDDY = (
    r"C:/Users/Administrator/AppData/Local/Programs/WorkBuddy/resources/"
    r"app.asar.unpacked/cli/bin/codebuddy"
)
DEFAULT_CLAUDE = (
    # ⚠️ 必须指向包内的原生二进制 bin/claude.exe，**不能**用 npm 生成的 `claude`
    # （那是 /bin/sh 脚本，Windows 上 subprocess 直接执行会报 WinError 193）。
    r"C:/Users/Administrator/AppData/Roaming/npm/node_modules/"
    r"@anthropic-ai/claude-code/bin/claude.exe"
)

# CLI 的私有工作目录：避免它把 AIOS 仓库当成 project 上下文加载
CLI_CWD = Path(os.getenv("AIOS_CLI_CWD", "D:/wb_tmp/cli_agent_cwd"))

# argv 模板里代表「本任务 output_schema」的占位符（替换后交给 CLI 的 --json-schema）
JSON_SCHEMA_TOKEN = "{JSON_SCHEMA}"

Executor = Callable[..., dict[str, Any]]


# --------------------------------------------------------------------------- #
# prompt 渲染（与 workstation_runner.make_llm_executor 的语义保持一致）
# --------------------------------------------------------------------------- #
def render_prompt(*, task: TaskPacket, context: str, output_schema: dict[str, Any]) -> str:
    schema_json = json.dumps(output_schema, ensure_ascii=False, indent=2)
    return (
        "你必须只输出一个 JSON 对象。第一个字符必须是 `{`，最后一个字符必须是 `}`。"
        "禁止 markdown 代码围栏、禁止解释、禁止前后缀文字。\n\n"
        f"# 角色\n{task.role}\n\n"
        f"# 任务指令\n{task.instructions}\n\n"
        f"# 上游输入\n{json.dumps(task.inputs, ensure_ascii=False, indent=2)}\n\n"
        f"# 验收标准\n{json.dumps(task.acceptance_criteria, ensure_ascii=False, indent=2)}\n\n"
        f"# 上下文\n{context}\n\n"
        f"# 必须严格输出的 JSON Schema\n{schema_json}\n\n"
        "再次强调：只输出符合上述 schema 的单个 JSON 对象。"
    )


# --------------------------------------------------------------------------- #
# 结果解析：兼容两种 CLI 的输出形状
#   * claude      -> 单个 JSON 对象 {"type":"result","result":"...","is_error":false}
#   * codebuddy   -> JSON 数组，末尾元素 type=="result"（同字段）
# --------------------------------------------------------------------------- #
def _result_message(stdout: str) -> dict[str, Any]:
    """取出 CLI 输出里的 `type=="result"` 消息（claude 是对象，codebuddy 是数组）。"""
    raw = stdout.strip()
    if not raw:
        raise RuntimeError("CLI produced no output")
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"CLI output is not JSON: {exc}") from exc

    if isinstance(payload, list):
        picked = None
        for item in reversed(payload):
            if isinstance(item, dict) and item.get("type") == "result":
                picked = item
                break
        if picked is None:
            raise RuntimeError("CLI JSON array contained no 'result' element")
        payload = picked

    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected CLI payload type: {type(payload).__name__}")
    if payload.get("is_error"):
        raise RuntimeError(f"CLI reported an error: {str(payload.get('result'))[:200]}")
    return payload


def extract_structured_output(stdout: str) -> dict[str, Any] | None:
    """优先取 CLI 原生结构化输出（`--json-schema` 生效时落在 `structured_output`）。

    ⚠️ 这是**确定性**路径：codebuddy 在 `--tools StructuredOutput` 下会把合规 JSON
    放进 `structured_output`，而 `result` 文本此时是**空串** —— 若只解析文本必失败。
    """
    payload = _result_message(stdout)
    so = payload.get("structured_output")
    if isinstance(so, dict):
        return so
    if isinstance(so, str) and so.strip():
        try:
            parsed = json.loads(so)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def extract_result_text(stdout: str) -> str:
    payload = _result_message(stdout)
    text = payload.get("result") or payload.get("text") or ""
    if isinstance(text, (dict, list)):
        # 有些 CLI/参数组合会把结构化结果直接放在 result（而非字符串）
        return json.dumps(text, ensure_ascii=False)
    if not str(text).strip():
        raise RuntimeError("CLI returned an empty result")
    return str(text)


def parse_json_object(text: str) -> dict[str, Any]:
    """从模型输出里取出第一个 JSON 对象（容忍 ``` 围栏、前后废话、双重编码）。"""
    body = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", body, re.S)
    if fence:
        body = fence.group(1).strip()
    obj: Any = None
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, str):  # 双重编码："{\"a\":1}"
        try:
            obj = json.loads(obj)
        except json.JSONDecodeError:
            obj = None
    if isinstance(obj, dict):
        return obj
    # 平衡括号扫描：截出第一个完整对象
    start = body.find("{")
    if start >= 0:
        depth = 0
        in_str = False
        esc = False
        for idx in range(start, len(body)):
            ch = body[idx]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = body[start : idx + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(f"unparsable JSON object: {exc}") from exc
                    if isinstance(parsed, dict):
                        return parsed
                    break
    raise RuntimeError("model output contained no JSON object")


# --------------------------------------------------------------------------- #
# 子进程调用
# --------------------------------------------------------------------------- #
def _child_env() -> dict[str, str]:
    """CLI 子进程环境。

    ⚠️ 关键：必须剔除 `SERVER__PORT`。本脚本常在 WorkBuddy 应用进程内被调起，
    应用已把 `SERVER__PORT=<app bridge port>` 注入环境；codebuddy CLI 会读取
    `SERVER__*` 并尝试绑定同一端口 -> EADDRINUSE -> unhandled rejection -> **永久挂起**。
    剔除后 CLI 自行选端口，实测 29s 内正常返回。
    """
    env = dict(os.environ)
    env.pop("SERVER__PORT", None)
    return env


def _run_cli(argv: list[str], prompt: str, *, timeout: float) -> str:
    CLI_CWD.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_child_env(),
            cwd=str(CLI_CWD),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"CLI timed out after {timeout:.0f}s: {argv[0]}") from exc
    except OSError as exc:
        raise RuntimeError(f"CLI could not be started: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        raise RuntimeError(f"CLI exited {proc.returncode}: {tail}")
    return proc.stdout


def default_argv(platform: str) -> list[str]:
    """内置两个平台的 CLI 调用模板。

    模板里的 `{JSON_SCHEMA}` 占位符会在执行时替换为该任务的 output_schema，
    经 CLI 原生的 `--json-schema` 强制结构化输出（比纯 prompt 约束可靠得多）。
    """
    node = os.getenv("AIOS_CLI_NODE", DEFAULT_NODE)
    if platform == "workbuddy":
        # ⚠️ `--tools` **不能**留空：`--json-schema` 依赖内建的 `StructuredOutput`
        # 工具；禁用它时模型只会把 JSON 写在散文里（偶发解析失败，见 Pilot run4 T3）。
        # 只放行 StructuredOutput：既保证结构化，又天然沙箱化（无文件/命令工具）。
        return [
            node,
            os.getenv("AIOS_CLI_CODEBUDDY_BIN", DEFAULT_CODEBUDDY),
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            JSON_SCHEMA_TOKEN,
            "--tools",
            "StructuredOutput",
            "-y",
        ]
    if platform == "claude_code":
        return [
            os.getenv("AIOS_CLI_CLAUDE_BIN", DEFAULT_CLAUDE),
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            JSON_SCHEMA_TOKEN,
        ]
    raise RuntimeError(f"no built-in CLI declared for platform {platform!r}")


def argv_for_platform(platform: str, *, json_schema: dict[str, Any] | None = None) -> list[str]:
    """平台 -> argv。支持 `AIOS_CLI_<PLATFORM>_ARGV`（JSON 数组）完全覆盖。

    `{JSON_SCHEMA}` 占位符替换为该任务的 output_schema（紧凑 JSON）。
    """
    key = "AIOS_CLI_" + re.sub(r"[^A-Za-z0-9]+", "_", platform).upper() + "_ARGV"
    raw = os.getenv(key)
    if raw:
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
            raise RuntimeError(f"{key} must be a JSON array of strings")
        argv = list(parsed)
    else:
        argv = default_argv(platform)
    if any(JSON_SCHEMA_TOKEN in a for a in argv):
        schema_str = json.dumps(
            json_schema or {}, ensure_ascii=False, separators=(",", ":")
        )
        argv = [a.replace(JSON_SCHEMA_TOKEN, schema_str) for a in argv]
    return argv


def make_cli_executor(platform: str, *, timeout: float | None = None) -> Executor:
    """构造一个把任务交给本机 CLI agent 执行的 Executor。"""
    _timeout = timeout or float(os.getenv("AIOS_CLI_TIMEOUT", "300"))

    def _exec(
        *, task: TaskPacket, context: str, output_schema: dict[str, Any]
    ) -> dict[str, Any]:
        argv = argv_for_platform(platform, json_schema=output_schema)
        prompt = render_prompt(task=task, context=context, output_schema=output_schema)
        started = time.time()
        stdout = _run_cli(argv, prompt, timeout=_timeout)
        try:
            # 1) CLI 原生结构化输出（确定性）  2) 退回解析 result 文本
            data = extract_structured_output(stdout)
            if data is None:
                data = parse_json_object(extract_result_text(stdout))
        except Exception as exc:  # noqa: BLE001
            # 解析失败时把 CLI 原始输出落盘，便于诊断「模型没按 schema 输出」
            dump = CLI_CWD / f"raw_{platform}_{task.task_id}.log"
            try:
                dump.write_text(stdout, encoding="utf-8")
                suffix = f" (raw output: {dump})"
            except OSError:
                suffix = ""
            raise RuntimeError(f"{exc}{suffix}") from exc
        logger.info(
            "platform=%s task=%s cli=%.1fs keys=%s",
            platform,
            task.task_id,
            time.time() - started,
            sorted(data.keys()),
        )
        return data

    return _exec


# --------------------------------------------------------------------------- #
# 主机循环（与 workstation_runner.run 同形）
# --------------------------------------------------------------------------- #
def run_host(
    *,
    outbox: Path,
    inbox: Path,
    interval: float = 2.0,
    once: bool = False,
    max_tasks: int | None = None,
) -> int:
    outbox.mkdir(parents=True, exist_ok=True)
    inbox.mkdir(parents=True, exist_ok=True)
    handled = 0
    while True:
        for task_dir in discover_pending(outbox):
            platform = _platform_for(task_dir)
            try:
                executor = make_cli_executor(platform) if platform else None
            except Exception as exc:  # noqa: BLE001
                logger.error("task %s: %s", task_dir.name, exc)
                (task_dir / ".error").write_text(
                    f"cli_agent_host could not resolve platform {platform!r}: {exc}",
                    encoding="utf-8",
                )
                continue
            if executor is None:
                (task_dir / ".error").write_text(
                    "cli_agent_host: task has no platform", encoding="utf-8"
                )
                continue
            try:
                process_task(task_dir, inbox, executor)
                handled += 1
            except Exception:  # noqa: BLE001  # process_task 已写 .error
                continue
            if max_tasks is not None and handled >= max_tasks:
                return handled
        if once:
            break
        time.sleep(interval)
    return handled


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AIOS CLI agent host (codebuddy / claude)")
    ap.add_argument("--outbox", type=Path, required=True)
    ap.add_argument("--inbox", type=Path, required=True)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger.info("cli agent host start: outbox=%s inbox=%s", args.outbox, args.inbox)
    return run_host(
        outbox=args.outbox, inbox=args.inbox, interval=args.interval, once=args.once
    )


if __name__ == "__main__":
    raise SystemExit(main())
