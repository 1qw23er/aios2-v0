from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest

from aios.adapters.deepseek_harness import (
    DeepSeekHarnessLaunchConfig,
    DeepSeekHarnessWorkerClient,
    HarnessTransportError,
    SandboxVerificationError,
    _JsonLineRpc,
)
from aios.worker_contract import TaskEnvelope, WorkerState


def envelope() -> TaskEnvelope:
    return TaskEnvelope(
        task_id="tsk-1",
        execution_id="run-1",
        idempotency_key="idem-1",
        context_hash="ctx-1",
        instructions="answer",
        structured_context={},
        expected_output_schema={"type": "object"},
        permission_profile="read_only",
        total_attempt_limit=1,
    )


def config(
    tmp_path: Path,
    *,
    plugins=("dsh-fs-sandbox", "dsh-sandbox-policy"),
    manifest_text: str | None = None,
    command_manifest: Path | None = None,
):
    manifest = tmp_path / "cordis.yml"
    if manifest_text is None:
        manifest_text = "\n".join(
            f"- id: {plugin}\n  name: '@deepseek-ai/{plugin}'" for plugin in plugins
        )
    manifest.write_text(manifest_text, encoding="utf-8")
    command_path = manifest if command_manifest is None else command_manifest
    return DeepSeekHarnessLaunchConfig.from_manifest(
        command=("node", "server.js", str(command_path)),
        cwd=tmp_path,
        manifest=manifest,
        active_plugins=plugins,
    )


class Rpc:
    def __init__(self) -> None:
        self.calls = []
        self.notifications = [
            {
                "method": "session.event",
                "params": {
                    "sessionId": "run-1",
                    "event": {
                        "type": "assistant/message",
                        "data": {
                            "text": '{"answer": 42}',
                            "usage": {"inputTokens": 3, "outputTokens": 2},
                        },
                    },
                },
            },
            {
                "method": "session.event",
                "params": {
                    "sessionId": "run-1",
                    "event": {"type": "turn/end", "data": {"reason": {"kind": "completed"}}},
                },
            },
        ]

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "initialize":
            return {"serverInfo": {"name": "deepseek-harness-sdk-runtime", "version": "0.0.1"}}
        if method == "session/prompt":
            return {"messageId": "message-1"}
        raise AssertionError(method)

    def drain_notifications(self):
        notifications, self.notifications = self.notifications, []
        return notifications


def test_sandbox_proof_precedes_credentials_and_remote_prompt(tmp_path: Path) -> None:
    resolved = []
    rpc = Rpc()
    client = DeepSeekHarnessWorkerClient(
        config=config(tmp_path, plugins=("dsh-fs-sandbox",)),
        credential_resolver=lambda: resolved.append(True) or "secret",
        rpc=rpc,
    )

    with pytest.raises(SandboxVerificationError):
        client.submit(envelope())

    assert resolved == []
    assert rpc.calls == []


def test_claimed_plugins_without_manifest_mounts_fail_before_secret_or_process(
    tmp_path: Path, monkeypatch
) -> None:
    resolved = []
    launches = []
    launch = config(
        tmp_path,
        plugins=("dsh-fs-sandbox", "dsh-sandbox-policy"),
        manifest_text="- id: fs-local\n  name: '@deepseek-ai/dsh-fs-local'",
    )
    monkeypatch.setattr(
        "aios.adapters.deepseek_harness.subprocess.Popen",
        lambda *args, **kwargs: launches.append((args, kwargs)),
    )
    client = DeepSeekHarnessWorkerClient(
        config=launch, credential_resolver=lambda: resolved.append(True) or "secret"
    )

    with pytest.raises(SandboxVerificationError, match="manifest"):
        client.submit(envelope())

    assert resolved == []
    assert launches == []


def test_valid_manifest_not_referenced_by_command_fails_before_secret_or_process(
    tmp_path: Path, monkeypatch
) -> None:
    resolved = []
    launches = []
    alternate = tmp_path / "default.cordis.yml"
    alternate.write_text("- id: fs-local\n  name: '@deepseek-ai/dsh-fs-local'", encoding="utf-8")
    launch = config(tmp_path, command_manifest=alternate)
    monkeypatch.setattr(
        "aios.adapters.deepseek_harness.subprocess.Popen",
        lambda *args, **kwargs: launches.append((args, kwargs)),
    )
    client = DeepSeekHarnessWorkerClient(
        config=launch, credential_resolver=lambda: resolved.append(True) or "secret"
    )

    with pytest.raises(SandboxVerificationError, match="command"):
        client.submit(envelope())

    assert resolved == []
    assert launches == []


def test_manifest_hash_mismatch_fails_before_secret_or_process(
    tmp_path: Path, monkeypatch
) -> None:
    resolved = []
    launches = []
    launch = config(tmp_path)
    launch.manifest.write_text("changed", encoding="utf-8")
    monkeypatch.setattr(
        "aios.adapters.deepseek_harness.subprocess.Popen",
        lambda *args, **kwargs: launches.append((args, kwargs)),
    )
    client = DeepSeekHarnessWorkerClient(
        config=launch, credential_resolver=lambda: resolved.append(True) or "secret"
    )

    with pytest.raises(SandboxVerificationError, match="changed"):
        client.submit(envelope())

    assert resolved == []
    assert launches == []


def test_submit_events_result_and_usage_aggregate_in_one_execution(tmp_path: Path) -> None:
    rpc = Rpc()
    client = DeepSeekHarnessWorkerClient(
        config=config(tmp_path), credential_resolver=lambda: "secret", rpc=rpc
    )

    submission = client.submit(envelope())
    events = client.events("run-1")
    status = client.status("run-1")
    result = client.result("run-1")

    assert submission.runtime_session_id == "run-1"
    assert len([call for call in rpc.calls if call[0] == "session/prompt"]) == 1
    assert len(events) == 2
    assert status.state is WorkerState.COMPLETED
    assert result.structured_output == {"answer": 42}
    assert result.usage == {"input_tokens": 3, "output_tokens": 2}


def test_cancel_and_resume_are_explicitly_unsupported_without_rpc(tmp_path: Path) -> None:
    rpc = Rpc()
    client = DeepSeekHarnessWorkerClient(
        config=config(tmp_path), credential_resolver=lambda: "secret", rpc=rpc
    )

    assert client.cancel("run-1").code == "unsupported_capability"
    assert client.resume("resume-1").code == "unsupported_capability"
    assert rpc.calls == []


def test_malformed_terminal_result_fails_explicitly(tmp_path: Path) -> None:
    rpc = Rpc()
    rpc.notifications[0]["params"]["event"]["data"]["text"] = "not-json"
    client = DeepSeekHarnessWorkerClient(
        config=config(tmp_path), credential_resolver=lambda: "secret", rpc=rpc
    )
    client.submit(envelope())

    with pytest.raises(HarnessTransportError, match="structured result"):
        client.result("run-1")


def test_stdio_carrier_collects_notifications_after_rpc_response(
    tmp_path: Path, monkeypatch
) -> None:
    class Process:
        stdin = StringIO()
        stdout = StringIO(
            '{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"x"}}}\n'
            '{"jsonrpc":"2.0","method":"session.event","params":{"sessionId":"run-1"}}\n'
        )
        stderr = StringIO()

        def poll(self):
            return None

    monkeypatch.setattr(
        "aios.adapters.deepseek_harness.subprocess.Popen", lambda *a, **k: Process()
    )
    transport = _JsonLineRpc(config(tmp_path), "secret")

    assert transport.request("initialize", {})["serverInfo"]["name"] == "x"
    assert transport.drain_notifications()[0]["method"] == "session.event"


def test_stdio_carrier_pins_environment_override_to_verified_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    captured = {}

    class Process:
        stdin = StringIO()
        stdout = StringIO(
            '{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"x"}}}\n'
        )
        stderr = StringIO()

        def poll(self):
            return None

    def popen(*args, **kwargs):
        captured.update(kwargs)
        return Process()

    monkeypatch.setenv("DSH_CORDIS_CONFIG", str(tmp_path / "alternate.yml"))
    monkeypatch.setattr("aios.adapters.deepseek_harness.subprocess.Popen", popen)
    launch = config(tmp_path)

    _JsonLineRpc(launch, "secret")

    assert captured["env"]["DSH_CORDIS_CONFIG"] == str(launch.manifest)
