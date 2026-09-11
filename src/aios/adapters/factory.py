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
* External agent delegation (``agent.delegation_mode`` -> ``RemoteApiAdapter`` /
  ``WorkstationAdapter``, Gateway #57) is a SEPARATE opt-in behind
  ``AIOS_EXTERNAL_DELEGATION_ENABLED`` (default off, fail-closed). It is distinct
  from the DeepSeek Harness worker; when unset, ``delegation_mode`` is ignored and
  the agent resolves to the in-process ``LLMExecutionAdapter``.

The in-process ``LLMExecutionAdapter`` remains the legitimate local substrate for
every agent that is not explicitly harness-configured; it is the default path, not
a "wrong adapter" fallback.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from aios.adapters.deepseek_harness import (
    DeepSeekHarnessLaunchConfig,
    DeepSeekHarnessWorkerClient,
    HarnessTransportError,
)
from aios.adapters.external import WorkstationAdapter
from aios.adapters.hermes_remote import RemoteApiAdapter
from aios.adapters.worker_delegated import WorkerDelegatedAdapter
from aios.execution import ExecutionAdapter, LLMExecutionAdapter
from aios.models import Agent, DelegationMode, Task

logger = logging.getLogger(__name__)

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


def _external_delegation_enabled() -> bool:
    """Opt-in gate for ``agent.delegation_mode`` routing (Agent Interoperability
    Gateway #57).

    External delegation (REMOTE_API / WORKSTATION) is fail-closed: unless
    ``AIOS_EXTERNAL_DELEGATION_ENABLED`` is explicitly ``true``, ``delegation_mode``
    is ignored and the agent resolves to the in-process ``LLMExecutionAdapter``.
    This preserves the established invariant that non-LLM execution is opt-in and
    that a disabled external-execution surface never silently reroutes a task.
    """
    return os.getenv("AIOS_EXTERNAL_DELEGATION_ENABLED", "").lower() == "true"


def build_execution_adapter(session: Any, task_id: str) -> ExecutionAdapter:
    """Resolve the execution adapter for a task's assigned agent.

    Deterministic, fail-closed selection (Runtime P1, C6):

    * a harness-configured agent resolves to ``WorkerDelegatedAdapter``;
    * an agent declaring ``delegation_mode`` (REMOTE_API / WORKSTATION) resolves to
      the matching external-agent adapter (Agent Interoperability Gateway #57);
      both implement the same ``DelegatedExecutionAdapter.run()`` surface as
      ``LLMExecutionAdapter``, so the existing ``execute_task`` path works unchanged;
    * every other agent resolves to the in-process ``LLMExecutionAdapter`` (the
      legitimate local substrate).

    The selection key is the explicit feature flag + ``config_ref`` prefix for the
    harness, and ``agent.delegation_mode`` for external delegation -- never
    ``AdapterType``. External delegation is itself opt-in behind
    ``AIOS_EXTERNAL_DELEGATION_ENABLED`` (default off, fail-closed): when unset,
    ``delegation_mode`` is ignored and the agent resolves to the local LLM adapter,
    preserving the "harness disabled -> local LLM" invariant. Misconfigured external
    agents also fall back to the local LLM adapter rather than breaking execution.
    """
    task = session.get(Task, task_id) if session is not None else None
    agent = session.get(Agent, task.assigned_agent_id) if task and task.assigned_agent_id else None
    harness = _resolve_harness_delegated_adapter(session, task, agent)
    if harness is not None:
        return harness
    if _external_delegation_enabled() and agent is not None and agent.delegation_mode is not None:
        routed = _resolve_delegated_adapter(agent)
        if routed is not None:
            return routed
    return LLMExecutionAdapter()


def _resolve_delegated_adapter(agent: Agent) -> ExecutionAdapter | None:
    """Resolve an external-agent adapter from ``agent.delegation_mode``.

    Returns ``None`` when the mode is unsupported or misconfigured, so the caller
    falls back to the in-process ``LLMExecutionAdapter``. Fail-closed by design --
    a misconfigured external agent must never crash or silently reroute execution.
    """
    mode = agent.delegation_mode
    if mode == DelegationMode.REMOTE_API:
        return RemoteApiAdapter(agent=agent, resolve_secret=_resolve_environment_secret)
    if mode == DelegationMode.WORKSTATION:
        outbox = os.getenv("AIOS_WORKSTATION_OUTBOX")
        inbox = os.getenv("AIOS_WORKSTATION_INBOX")
        if not outbox or not inbox:
            logger.warning(
                "agent %s is WORKSTATION but AIOS_WORKSTATION_OUTBOX/INBOX is unset; "
                "falling back to local LLM adapter",
                agent.id,
            )
            return None
        try:
            return WorkstationAdapter(agent=agent, outbox=Path(outbox), inbox=Path(inbox))
        except (OSError, ValueError) as exc:
            logger.warning(
                "agent %s WORKSTATION outbox/inbox invalid (%s); falling back to local LLM",
                agent.id,
                exc,
            )
            return None
    # LOCAL is the LLM path; A2A/MCP are not implemented (delegation.py:4).
    return None


def _resolve_environment_secret(secret_ref: str | None) -> str:
    """Resolve the existing opaque secret handle without persisting its value."""
    prefix = "env://"
    if not secret_ref or not secret_ref.startswith(prefix):
        raise HarnessTransportError("Harness Agent requires an env:// secret_ref")
    name = secret_ref[len(prefix) :]
    if not name or name not in os.environ:
        raise HarnessTransportError("Harness provider credential is unavailable")
    return os.environ[name]
