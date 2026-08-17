"""Opt-in execution adapter selection at the application edge."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from aios.adapters.deepseek_harness import (
    DeepSeekHarnessLaunchConfig,
    DeepSeekHarnessWorkerClient,
    HarnessTransportError,
)
from aios.adapters.worker_delegated import WorkerDelegatedAdapter
from aios.execution import ExecutionAdapter, LLMExecutionAdapter
from aios.models import Agent, Task

_HARNESS_CONFIG_PREFIX = "deepseek-harness+file://"


def build_execution_adapter(session: Any, task_id: str) -> ExecutionAdapter:
    """Return Harness only for an enabled, explicitly configured Agent."""
    if os.getenv("AIOS_DEEPSEEK_HARNESS_ENABLED", "").lower() != "true":
        return LLMExecutionAdapter()
    task = session.get(Task, task_id) if session is not None else None
    agent = session.get(Agent, task.assigned_agent_id) if task and task.assigned_agent_id else None
    if agent is None or not (agent.config_ref or "").startswith(_HARNESS_CONFIG_PREFIX):
        return LLMExecutionAdapter()
    config_path = Path((agent.config_ref or "")[len(_HARNESS_CONFIG_PREFIX) :])
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        launch = DeepSeekHarnessLaunchConfig(
            command=tuple(raw["command"]),
            cwd=Path(raw["cwd"]).resolve(),
            manifest=Path(raw["manifest"]).resolve(),
            manifest_sha256=str(raw["manifest_sha256"]),
            active_plugins=tuple(raw["active_plugins"]),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HarnessTransportError("invalid DeepSeek Harness config_ref") from exc
    client = DeepSeekHarnessWorkerClient(
        config=launch,
        credential_resolver=lambda: _resolve_environment_secret(agent.secret_ref),
    )
    return WorkerDelegatedAdapter(agent=agent, client=client).as_execution_adapter()


def _resolve_environment_secret(secret_ref: str | None) -> str:
    """Resolve the existing opaque secret handle without persisting its value."""
    prefix = "env://"
    if not secret_ref or not secret_ref.startswith(prefix):
        raise HarnessTransportError("Harness Agent requires an env:// secret_ref")
    name = secret_ref[len(prefix) :]
    if not name or name not in os.environ:
        raise HarnessTransportError("Harness provider credential is unavailable")
    return os.environ[name]
