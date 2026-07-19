"""Remote API adapter for an external agent (Agent Interoperability Gateway #57).

Capability A of the first implementation slice: a *programmable* remote agent
reached over HTTP. Hermes (remote_api mode) is the selected first agent.

This module is transport-only. The remote agent's internals (model, prompt,
memory, tools) are never known to AIOS. AIOS sends a projected, immutable
context and receives a structured Artifact that must pass schema validation
before the task completes.

Security:
  * The API key is resolved from an external secret store via ``secret_ref`` and
    supplied per-request via an ``Authorization`` header. It is NEVER persisted
    to TaskContext / Artifact / AuditLog.
  * A self-test mode (``_FakeHermesServer``) lets the adapter be exercised
    end-to-end without touching any real external system.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from aios.delegation import DelegatedExecutionAdapter
from aios.models import Agent, DelegationMode


class RemoteApiAdapter(DelegatedExecutionAdapter):
    mode = DelegationMode.REMOTE_API

    def __init__(
        self,
        *,
        agent: Agent,
        resolve_secret: Any | None = None,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(agent=agent)
        # resolve_secret(secret_ref: str) -> str  (external secret store).
        self.resolve_secret = resolve_secret
        self.timeout = timeout
        self._store: dict[str, dict[str, Any]] = {}  # in-process run registry for self-test

    # --- DelegatedAdapter surface ---
    def submit(self, *, delegated_run, projected_context, output_schema, remote_callback_url):
        # Resolve the secret handle -> actual key at call time (never stored).
        secret = self.resolve_secret(self.agent.secret_ref) if self.resolve_secret else None
        api_key = secret or ""
        payload = {
            "task_id": delegated_run.task_id,
            "idempotency_key": delegated_run.idempotency_key,
            "mode": "remote_api",
            "context": projected_context,
            "output_schema": output_schema,
            "callback_url": remote_callback_url,
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body = json.dumps(payload).encode()
        # Submit to endpoint; expect {"run_id": ...}. Polling uses status().
        req = urllib.request.Request(
            f"{self.agent.endpoint}/tasks", data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                info = json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"submit failed: {exc}") from exc
        # The remote may return a finished result inline (self-test fake server
        # does). Cache it so status()/ingest_result() work without a second
        # round-trip. For async remotes, result is None here and status() polls.
        remote_run_id = info.get("run_id", delegated_run.idempotency_key)
        self._store[remote_run_id] = {
            "status": info.get("status", "queued"),
            "result": info.get("result"),
            "cost": info.get("cost"),
            "usage": info.get("usage"),
        }
        return {
            "remote_run_id": remote_run_id,
            "remote_status": info.get("status", "queued"),
        }

    def status(self, *, delegated_run):
        run_id = delegated_run.remote_run_id
        # Self-test / inline-result path: read the cached entry (no network).
        if run_id in self._store:
            entry = self._store[run_id]
            return {
                "remote_status": entry.get("status", "running"),
                "finished": entry.get("result") is not None,
                "result": entry.get("result"),
                "cost": entry.get("cost"),
                "usage": entry.get("usage"),
            }
        # Real async HTTP path: GET {endpoint}/tasks/{run_id}
        url = f"{self.agent.endpoint}/tasks/{run_id}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                entry = json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"status poll failed: {exc}") from exc
        finished = entry.get("result") is not None
        return {
            "remote_status": entry.get("status", "running"),
            "finished": finished,
            "result": entry.get("result"),
            "cost": entry.get("cost"),
            "usage": entry.get("usage"),
        }

    def cancel(self, *, delegated_run):
        run_id = delegated_run.remote_run_id
        if run_id in self._store:
            self._store[run_id]["status"] = "cancelled"

    def ingest_result(self, *, delegated_run):
        entry = self._store.get(delegated_run.remote_run_id, {})
        result = entry.get("result") or {}
        return {
            "summary": result.get("summary", "remote agent result"),
            "artifacts": result.get("artifacts", []),
        }


class _FakeHermesHandler(BaseHTTPRequestHandler):
    registry: dict[str, dict[str, Any]] = {}

    def _write(self, obj: dict[str, Any], code: int = 200) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        _payload = json.loads(self.rfile.read(length) or b"{}")
        run_id = f"hermes_{_payload.get('idempotency_key', 'x')[:8]}"
        # Immediately mark a finished result so polling completes fast.
        _FakeHermesHandler.registry[run_id] = {
            "status": "succeeded",
            "cost": 0.01,
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            "result": {
                "summary": "Hermes produced this via remote API",
                "artifacts": [
                    {
                        "type": "json",
                        "uri": f"hermes://{run_id}",
                        "summary": "Hermes produced this via remote API",
                        "data": {
                            "summary": "Hermes produced this via remote API",
                            "deliverable": "structured output from external agent",
                        },
                    }
                ],
            },
        }
        # Return the finished result inline so the adapter needs no second
        # round-trip (and so self-tests pass without live polling).
        result = {
            "run_id": run_id,
            "status": "succeeded",
            "cost": 0.01,
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            "result": _FakeHermesHandler.registry[run_id]["result"],
        }
        self._write(result)

    def do_GET(self):  # noqa: N802
        # Polling endpoint /tasks/{run_id}
        run_id = self.path.rsplit("/", 1)[-1]
        entry = _FakeHermesHandler.registry.get(run_id, {})
        self._write(entry or {"status": "running"})

    def log_message(self, *args):  # silence test server logs
        pass


class _FakeHermesServer:
    """In-process mock Hermes for self-tests (no real network / external system)."""

    registry: dict[str, dict[str, Any]] = {}

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._httpd = HTTPServer((host, port), _FakeHermesHandler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> str:
        self._thread.start()
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def make_fake_hermes_agent(endpoint: str) -> Agent:
    """Build an Agent row configured for the fake Hermes (self-test only)."""
    return Agent(
        name="Hermes (fake, self-test)",
        role="reasoning",
        adapter_type=__import__("aios.models", fromlist=["AdapterType"]).AdapterType.API,
        delegation_mode=DelegationMode.REMOTE_API,
        endpoint=endpoint,
        secret_ref="secret://fake-hermes",
        capabilities=["reasoning", "planning"],
    )
