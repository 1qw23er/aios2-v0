"""Opt-in execution adapter selection at the application edge.

Runtime Thin Layer P1 (C6/C7): the selection is made EXPLICIT and FAIL-CLOSED
without changing behaviour or introducing a new abstraction:

* Selection is driven by an explicit feature flag (``AIOS_DEEPSEEK_HARNESS_ENABLED``)
  plus the agent's ``config_ref`` prefix -- NOT by ``AdapterType``. ``AdapterType``
  (API / CLI / EXTERNAL / MODEL) is agent-registration metadata for the Agent
  Interoperability Gateway, not the execution-adapter selector, so a
  ``dict[AdapterType, AdapterFactory]`` registry would be the wrong key and is
  deliberately NOT introduced.
* The harness path is resolved through a single, fail-closed helper. Any malformed
  harness configuration raises ``HarnessTransportError`` instead of silently
  falling back to a different adapter (no silent fallback, C6).
* Only statically-imported adapters are ever instantiated -- there is no dynamic
  module loading / arbitrary code execution (R6).

The in-process ``LLMExecutionAdapter`` remains the legitimate local substrate for
every agent that is not explicitly harness-configured; it is the default path, not
a "wrong adapter" fallback.
"""

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


def _resolve_harness_delegated_adapter(
    session: Any, task: Any, agent: Agent | None
) -> ExecutionAdapter | None:
    """Explicit, fail-closed harness adapter resolution.

    Returns a ``WorkerDelegatedAdapter`` when the DeepSeek Harness is enabled and
    the agent is explicitly configured for it; returns ``None`` when the agent
    should run via the in-process ``LLMExecutionAdapter``. Malformed harness
    configuration FAILS CLOSED (``HarnessTransportError``). No dynamic import.
    """
    if agent is None:
        return None
    if os.getenv("AIOS_DEEPSEEK_HARNESS_ENABLED", "").lower() != "true":
        return None
    if not (agent.config_ref or "").startswith(_HARNESS_CONFIG_PREFIX):
        return None
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


def build_execution_adapter(session: Any, task_id: str) -> ExecutionAdapter:
    """Resolve the execution adapter for a task's assigned agent.

    Deterministic, fail-closed selection (Runtime P1, C6): a harness-configured
    agent resolves to ``WorkerDelegatedAdapter``; every other agent resolves to
    the in-process ``LLMExecutionAdapter`` (the legitimate local substrate). The
    selection key is the explicit feature flag + ``config_ref`` prefix, never
    ``AdapterType``.
    """
    task = session.get(Task, task_id) if session is not None else None
    agent = session.get(Agent, task.assigned_agent_id) if task and task.assigned_agent_id else None
    harness = _resolve_harness_delegated_adapter(session, task, agent)
    if harness is not None:
        return harness
    return LLMExecutionAdapter()


def _resolve_environment_secret(secret_ref: str | None) -> str:
    """Resolve the existing opaque secret handle without persisting its value."""
    prefix = "env://"
    if not secret_ref or not secret_ref.startswith(prefix):
        raise HarnessTransportError("Harness Agent requires an env:// secret_ref")
    name = secret_ref[len(prefix) :]
    if not name or name not in os.environ:
        raise HarnessTransportError("Harness provider credential is unavailable")
    return os.environ[name]
