"""DeepSeek Harness V1 JSON-RPC worker client."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from aios.worker_contract import (
    TaskEnvelope,
    UnsupportedCapability,
    WorkerCapabilities,
    WorkerEvent,
    WorkerResult,
    WorkerState,
    WorkerStatus,
    WorkerSubmission,
)

_REQUIRED_SANDBOX_PLUGINS = frozenset({"dsh-fs-sandbox", "dsh-sandbox-policy"})
_REQUIRED_MANIFEST_PLUGINS = frozenset(
    {
        "@deepseek-ai/dsh-fs-sandbox",
        "@deepseek-ai/dsh-sandbox-policy",
    }
)


class HarnessTransportError(RuntimeError):
    """The Harness process or JSON-RPC stream violated V1 expectations."""


class SandboxVerificationError(HarnessTransportError):
    """The controlled launch configuration cannot prove sandbox enforcement."""


class RpcTransport(Protocol):
    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...
    def drain_notifications(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class DeepSeekHarnessLaunchConfig:
    command: tuple[str, ...]
    cwd: Path
    manifest: Path
    manifest_sha256: str
    active_plugins: tuple[str, ...]

    @classmethod
    def from_manifest(
        cls,
        *,
        command: Sequence[str],
        cwd: Path,
        manifest: Path,
        active_plugins: Sequence[str],
    ) -> DeepSeekHarnessLaunchConfig:
        content = manifest.read_bytes()
        return cls(
            command=tuple(command),
            cwd=cwd.resolve(),
            manifest=manifest.resolve(),
            manifest_sha256=hashlib.sha256(content).hexdigest(),
            active_plugins=tuple(active_plugins),
        )

    def verify(self) -> None:
        if not self.command or not self.cwd.is_dir() or not self.manifest.is_file():
            raise SandboxVerificationError("Harness launch configuration is incomplete")
        command_manifest = Path(self.command[-1])
        if not command_manifest.is_absolute():
            command_manifest = self.cwd / command_manifest
        if command_manifest.resolve() != self.manifest.resolve():
            raise SandboxVerificationError(
                "Harness launch command does not reference the pinned manifest"
            )
        actual = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        if actual != self.manifest_sha256:
            raise SandboxVerificationError("Harness launch manifest changed after approval")
        mounted = _enabled_manifest_plugins(self.manifest.read_text(encoding="utf-8"))
        missing = _REQUIRED_MANIFEST_PLUGINS.difference(mounted)
        if missing:
            raise SandboxVerificationError(
                "Harness manifest does not enable required sandbox plugins: "
                f"{', '.join(sorted(missing))}"
            )


class DeepSeekHarnessWorkerClient:
    """Provider transport with no AIOS lifecycle or retry ownership."""

    def __init__(
        self,
        *,
        config: DeepSeekHarnessLaunchConfig,
        credential_resolver: Callable[[], str],
        rpc: RpcTransport | None = None,
    ) -> None:
        self.config = config
        self._credential_resolver = credential_resolver
        self._rpc = rpc
        self._capabilities: WorkerCapabilities | None = None
        self._envelopes: dict[str, TaskEnvelope] = {}
        self._message_ids: dict[str, str] = {}
        self._events: dict[str, list[WorkerEvent]] = {}
        self._raw_events: dict[str, list[dict[str, Any]]] = {}
        self._states: dict[str, WorkerState] = {}

    def discover(self) -> WorkerCapabilities:
        self._start()
        assert self._capabilities is not None
        return self._capabilities

    def submit(self, envelope: TaskEnvelope) -> WorkerSubmission:
        if envelope.execution_id in self._envelopes:
            raise HarnessTransportError("execution already submitted")
        self._start()
        assert self._rpc is not None
        response = self._rpc.request(
            "session/prompt",
            {
                "sessionId": envelope.execution_id,
                "message": envelope.instructions,
                "metadata": {
                    "taskId": envelope.task_id,
                    "idempotencyKey": envelope.idempotency_key,
                    "contextHash": envelope.context_hash,
                    "permissionProfile": envelope.permission_profile,
                    "structuredContext": envelope.structured_context,
                    "expectedOutputSchema": envelope.expected_output_schema,
                },
            },
        )
        message_id = response.get("messageId")
        if not isinstance(message_id, str) or not message_id:
            raise HarnessTransportError("session/prompt returned no messageId")
        self._envelopes[envelope.execution_id] = envelope
        self._message_ids[envelope.execution_id] = message_id
        self._states[envelope.execution_id] = WorkerState.ACCEPTED
        return WorkerSubmission(envelope.execution_id, envelope.execution_id, message_id)

    def events(self, execution_id: str, after_cursor: str | None = None) -> list[WorkerEvent]:
        self._require_execution(execution_id)
        self._collect_notifications()
        events = self._events.get(execution_id, [])
        if after_cursor is None:
            return list(events)
        return [event for event in events if int(event.cursor) > int(after_cursor)]

    def status(self, execution_id: str) -> WorkerStatus:
        self.events(execution_id)
        return WorkerStatus(self._states[execution_id])

    def result(self, execution_id: str) -> WorkerResult:
        self.events(execution_id)
        state = self._states[execution_id]
        if state not in {
            WorkerState.COMPLETED,
            WorkerState.FAILED,
            WorkerState.INTERRUPTED,
            WorkerState.CANCELLED,
        }:
            raise HarnessTransportError("worker result is not terminal")
        text: str | None = None
        usage: dict[str, Any] = {}
        error: dict[str, Any] | None = None
        for event in self._raw_events.get(execution_id, []):
            event_type = event.get("type")
            data = event.get("data", {})
            if event_type == "assistant/message":
                if isinstance(data.get("text"), str):
                    text = data["text"]
                usage = self._normalize_usage(data.get("usage", {}))
            if event_type == "turn/end" and data.get("reason", {}).get("kind") != "completed":
                error = {"message": str(data.get("reason", {}).get("kind", "worker failed"))}
        structured_output = None
        if state is WorkerState.COMPLETED:
            try:
                structured_output = json.loads(text or "")
            except (TypeError, json.JSONDecodeError) as exc:
                raise HarnessTransportError(
                    "Harness terminal event has no structured result"
                ) from exc
            if not isinstance(structured_output, dict):
                raise HarnessTransportError("Harness structured result must be an object")
        envelope = self._envelopes[execution_id]
        return WorkerResult(
            execution_id=execution_id,
            status=state,
            context_hash=envelope.context_hash,
            text=text,
            structured_output=structured_output,
            usage=usage,
            runtime_metadata={
                "runtime_session_id": execution_id,
                "message_id": self._message_ids[execution_id],
            },
            error=error,
        )

    def cancel(self, execution_id: str) -> UnsupportedCapability:
        return UnsupportedCapability.for_capability("cancellation", worker_id="deepseek-harness")

    def resume(self, resume_ref: str) -> UnsupportedCapability:
        return UnsupportedCapability.for_capability(
            "checkpoint_resume", worker_id="deepseek-harness"
        )

    def _start(self) -> None:
        if self._capabilities is not None:
            return
        self.config.verify()
        credential = self._credential_resolver()
        if not credential:
            raise HarnessTransportError("DeepSeek provider credential is unavailable")
        if self._rpc is None:
            self._rpc = _JsonLineRpc(self.config, credential)
        response = self._rpc.request(
            "initialize", {"protocolVersion": "aios.worker/v1", "client": "aios"}
        )
        server = response.get("serverInfo", {})
        if server.get("name") != "deepseek-harness-sdk-runtime" or not server.get("version"):
            raise HarnessTransportError("unexpected Harness runtime identity")
        attested = response.get("activePlugins")
        if attested is not None and not _REQUIRED_SANDBOX_PLUGINS.issubset(set(attested)):
            raise SandboxVerificationError("runtime plugin attestation contradicts launch manifest")
        self._capabilities = WorkerCapabilities.deepseek_harness_v1(
            worker_id="deepseek-harness", runtime_version=str(server["version"])
        )

    def _collect_notifications(self) -> None:
        assert self._rpc is not None
        for notification in self._rpc.drain_notifications():
            method = notification.get("method")
            params = notification.get("params", {})
            execution_id = params.get("sessionId")
            if execution_id not in self._envelopes:
                raise HarnessTransportError("notification has unknown session identity")
            if method == "session.status":
                raw_state = params.get("status")
                if raw_state == "running":
                    self._states[execution_id] = WorkerState.RUNNING
                continue
            if method != "session.event" or not isinstance(params.get("event"), dict):
                raise HarnessTransportError("malformed Harness notification")
            raw = params["event"]
            raw_events = self._raw_events.setdefault(execution_id, [])
            raw_events.append(raw)
            cursor = str(len(raw_events))
            self._events.setdefault(execution_id, []).append(
                WorkerEvent(
                    kind=str(raw.get("type", "unknown")),
                    execution_id=execution_id,
                    cursor=cursor,
                    metadata={"message_id": self._message_ids[execution_id]},
                )
            )
            if raw.get("type") == "turn/end":
                reason = raw.get("data", {}).get("reason", {}).get("kind")
                self._states[execution_id] = (
                    WorkerState.COMPLETED if reason == "completed" else WorkerState.FAILED
                )
            elif self._states[execution_id] is WorkerState.ACCEPTED:
                self._states[execution_id] = WorkerState.RUNNING

    def _require_execution(self, execution_id: str) -> None:
        if execution_id not in self._envelopes:
            raise HarnessTransportError("unknown worker execution")

    @staticmethod
    def _normalize_usage(raw: dict[str, Any]) -> dict[str, Any]:
        mapping = {
            "inputTokens": "input_tokens",
            "outputTokens": "output_tokens",
            "cacheReadTokens": "cache_read_tokens",
            "reasoningTokens": "reasoning_tokens",
        }
        return {mapping[key]: value for key, value in raw.items() if key in mapping}


class _JsonLineRpc:
    """Minimal JSON-RPC stdio carrier for the fixed Harness SDK server."""

    def __init__(self, config: DeepSeekHarnessLaunchConfig, credential: str) -> None:
        environment = {
            **os.environ,
            "DEEPSEEK_API_KEY": credential,
            "DSH_CORDIS_CONFIG": str(config.manifest),
        }
        self._process = subprocess.Popen(
            config.command,
            cwd=config.cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._request_id = 0
        self._notifications: list[dict[str, Any]] = []
        self._notification_ready = threading.Event()
        self._notification_lock = threading.Lock()
        self._responses: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._reader = threading.Thread(target=self._read_messages, daemon=True)
        self._reader.start()

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._process.poll() is not None or self._process.stdin is None:
            raise HarnessTransportError("Harness runtime crashed")
        self._request_id += 1
        request_id = self._request_id
        self._process.stdin.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                separators=(",", ":"),
            )
            + "\n"
        )
        self._process.stdin.flush()
        response = self._responses.get()
        if isinstance(response, BaseException):
            raise response
        if response.get("id") != request_id:
            raise HarnessTransportError("Harness response correlation mismatch")
        if "error" in response:
            raise HarnessTransportError(str(response["error"].get("message", "RPC error")))
        result = response.get("result")
        if not isinstance(result, dict):
            raise HarnessTransportError("Harness RPC result must be an object")
        return result

    def drain_notifications(self) -> list[dict[str, Any]]:
        if not self._notifications:
            self._notification_ready.wait(timeout=0.05)
        with self._notification_lock:
            notifications, self._notifications = self._notifications, []
            self._notification_ready.clear()
        return notifications

    def _read_messages(self) -> None:
        if self._process.stdout is None:
            self._responses.put(HarnessTransportError("Harness stdout is unavailable"))
            return
        while True:
            line = self._process.stdout.readline()
            if not line:
                self._responses.put(HarnessTransportError("Harness runtime exited"))
                return
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._responses.put(HarnessTransportError("Harness emitted malformed JSON"))
                return
            if "method" in message and "id" not in message:
                with self._notification_lock:
                    self._notifications.append(message)
                    self._notification_ready.set()
            else:
                self._responses.put(message)


def _enabled_manifest_plugins(content: str) -> set[str]:
    """Return enabled top-level Cordis plugin package names."""
    plugins: set[str] = set()
    blocks = re.split(r"(?m)(?=^-\s)", content)
    for block in blocks:
        name = re.search(
            r"(?m)^\s+name:\s*(['\"]?)(@deepseek-ai/dsh-[a-z0-9-]+)\1\s*(?:#.*)?$",
            block,
        )
        if name is None:
            continue
        disabled = re.search(r"(?m)^\s+disabled:\s*([^#\r\n]+)", block)
        if disabled is not None and disabled.group(1).strip().lower() != "false":
            continue
        plugins.add(name.group(2))
    return plugins
