"""Real agent execution for V1 department tasks.

One execution protocol; departments are *configurations* (different capabilities,
instructions, acceptance criteria, output_schema) of that protocol -- there is no
six-runtime split. The production adapter calls a configured model endpoint; tests
inject a deterministic adapter that implements the same ``ExecutionAdapter``
protocol and walks the full claim -> context -> run -> validate -> artifact ->
complete -> orchestrate chain (only the model call is substituted, never a mock
shortcut that inserts artifacts directly).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from enum import StrEnum
from typing import Any, NamedTuple, Protocol

from jsonschema import validate
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel
from sqlmodel import Session, select

from aios.context_render import render_task_context_markdown
from aios.context_service import ContextService
from aios.models import (
    Artifact,
    ArtifactType,
    Task,
    TaskContext,
    TaskStatus,
    now_utc,
)
from aios.orchestrator import Orchestrator, complete_task
from aios.scheduler import route_task
from aios.services import ServiceError, append_event


class ExecutionResult(BaseModel):
    """Normalized result an adapter returns for one task.

    ``artifacts`` is a list of {"type", "uri", "summary", "data"}; each ``data``
    dict is validated against the task's ``output_schema``.
    """

    summary: str
    claims: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    # Optional adapter-supplied accounting (e.g. retry attempt count). Never
    # holds secrets; consumed only for auditing, not output-schema validation.
    metadata: dict[str, Any] = {}


class ExecutionAdapter(Protocol):
    def run(
        self,
        *,
        task_id: str,
        task_context: TaskContext,
        output_schema: dict[str, Any],
        idempotency_key: str,
    ) -> ExecutionResult: ...


class AdapterErrorCategory(StrEnum):
    """Recoverable classification for adapter failures (triage A–F).

    Persisted in event/audit ``reason`` as ``adapter_error|<CATEGORY>|<detail>``.
    Never includes provider secret, Authorization header, or raw body.
    """

    CONFIG_MISSING = "CONFIG_MISSING"  # A: no API key configured
    NETWORK = "NETWORK"  # B: connection refused / DNS failure
    TIMEOUT = "TIMEOUT"  # C: request timed out
    PROVIDER_HTTP = "PROVIDER_HTTP"  # D: non-2xx from provider
    PROVIDER_STRUCTURE = "PROVIDER_STRUCTURE"  # E: response parsed but wrong shape
    JSON_PARSE = "JSON_PARSE"  # F: model returned non-JSON
    UNKNOWN = "UNKNOWN"  # fallback


# Retry is intentionally narrow: only *transient, likely-self-healing* transport
# failures are retried. A provider returning 4xx/5xx (PROVIDER_HTTP), a malformed
# body (PROVIDER_STRUCTURE / JSON_PARSE), a missing key (CONFIG_MISSING), or any
# unexpected error (UNKNOWN) is NOT retried — re-sending the identical prompt would
# waste owner-paid tokens and can never succeed. This allowlist is the contract
# Codex required in the PR #56 blocking review (no over-broad retry).
_RETRYABLE_CATEGORIES: frozenset[AdapterErrorCategory] = frozenset(
    {AdapterErrorCategory.TIMEOUT, AdapterErrorCategory.NETWORK}
)

# Default ceiling on *additional* attempts (so MAX_RETRIES=2 means 1 initial +
# 2 retries = 3 total outbound calls). Set AIOS_AGENT_MAX_RETRIES=0 to disable
# retry entirely (1 outbound call, fail-fast). Tunable per environment.
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 2.0

# HARD SAFETY CAP on retries (Issue #55, Codex blocking review #1). A malformed
# or hostile AIOS_AGENT_MAX_RETRIES (e.g. 100000) must NEVER translate into that
# many paid outbound calls. This frozen constant bounds total outbound calls to
# 1 + MAX_RETRIES_HARD_CAP regardless of the env value. It caps *total cost*,
# not just a single sleep.
MAX_RETRIES_HARD_CAP = 5


_SECRET_PATTERN = re.compile(
    r"Bearer\s+[A-Za-z0-9\-._~+/]+"
    r"|Authorization:\s*\S+"
    r"|api[_-]?key[=:]\s*\S+"
    r"|sk-[A-Za-z0-9]+"
    r"|nvapi-[A-Za-z0-9]+"
    r"|AKIA[0-9A-Z]{16}"
    # Generic blob redaction (secret boundary, Issue #80 #5). Per Codex review the
    # 48-63 char pure-alphanumeric relaxation is unsafe: a regex cannot tell a benign
    # trace/content id from an opaque provider/session/access token that lacks a known
    # prefix, and over-redaction on an error path is the documented invariant. So any
    # 48+ char alphanumeric/base64-like run is redacted regardless of appearance.
    r"|[A-Za-z0-9+/]{48,}={0,2}"  # long base64-ish / alphanumeric blobs (secret boundary)
)


def _redact_secrets(text: str) -> str:
    """Redact provider secrets/keys from an error detail before persistence.

    Also redacts the live ``AIOS_AGENT_API_KEY`` value if present. Over-redaction
    is acceptable on an error path; the guarantee is that no secret ever reaches
    the database.

    The generic blob rule redacts any 48+ char alphanumeric/base64-like run. An
    appearance-based relaxation of 48-63 char strings (to spare benign trace ids /
    content hashes) was rejected in Codex review of Issue #80 #5: a regex cannot
    distinguish a benign identifier from an opaque unprefixed provider/session/access
    token, so the secret-boundary invariant (no unknown secret reaches Event/AuditLog)
    takes precedence over diagnostic fidelity. Over-redaction on an error path is
    acceptable.
    """
    if not text:
        return text
    api_key = os.getenv("AIOS_AGENT_API_KEY")
    if api_key and api_key in text:
        text = text.replace(api_key, "***REDACTED***")
    return _SECRET_PATTERN.sub("***REDACTED***", text)


class ExecutionError(ServiceError):
    """Raised when execution fails (adapter error or invalid output).

    Carries a recoverable ``category`` (AdapterErrorCategory) so failures can be
    triaged instead of collapsing into a single generic ``adapter_error``.
    """

    category: AdapterErrorCategory

    # Set by LLMExecutionAdapter.run() on the *final* retry-exhausted failure so
    # execute_task() can persist them. None for non-retry failures / unexpected
    # errors (which have no meaningful attempt accounting).
    attempts: int | None = None
    max_attempts: int | None = None

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        category: AdapterErrorCategory = AdapterErrorCategory.UNKNOWN,
    ) -> None:
        super().__init__(status_code, detail)
        self.category = category


class ResultValidationError(ExecutionError):
    """Output failed schema validation."""


def _artifact_checksum(result: ExecutionResult) -> str:
    encoded = json.dumps(
        result.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def execute_task(
    session: Session,
    task_id: str,
    idempotency_key: str,
    *,
    adapter: ExecutionAdapter,
    actor: str = "agent",
) -> Artifact:
    """Execute one READY department task through the real agent protocol.

    Lifecycle: idempotency guard -> claim (route_task, FIXED) -> RUNNING -> build
    immutable TaskContext -> adapter.run -> validate against output_schema -> Artifact
    with provenance -> complete_task (emits task.completed) -> Orchestrator activates
    downstream. Idempotent on idempotency_key; failures are recoverable (FAILED).
    """
    exec_result_id = f"exec:{idempotency_key}"
    # (a) Idempotency: same execution (same task + key) already produced an artifact
    # -> return it. Scoped to task_id so a reused key on a *different* task cannot
    # return the wrong artifact (route_task also rejects cross-task keys below).
    existing = session.exec(
        select(Artifact).where(
            Artifact.external_result_id == exec_result_id,
            Artifact.task_id == task_id,
        )
    ).first()
    if existing is not None:
        return existing

    task = session.get(Task, task_id)
    if task is None:
        raise ServiceError(404, "Task not found")
    # (b) Already completed: return the existing artifact (no re-run).
    if task.status == TaskStatus.DONE:
        done = session.exec(
            select(Artifact)
            .where(Artifact.task_id == task.id)
            .order_by(Artifact.created_at)
        ).first()
        if done is not None:
            return done

    # Recoverable: a FAILED task can be retried with a (new) idempotency key.
    if task.status == TaskStatus.FAILED:
        task.status = TaskStatus.READY
        task.updated_at = now_utc()
        session.add(task)
        append_event(
            session,
            project_id=task.project_id,
            task_id=task.id,
            event_type="task.reset_for_retry",
            idempotency_key=f"exec:{idempotency_key}:reset",
            payload={"before": TaskStatus.FAILED.value, "after": TaskStatus.READY.value},
        )
        from aios.audit import append_audit

        append_audit(
            session,
            actor=actor,
            action="task.reset_for_retry",
            resource_type="task",
            resource_id=task.id,
            project_id=task.project_id,
            task_id=task.id,
            before={"status": TaskStatus.FAILED.value},
            after={"status": TaskStatus.READY.value},
            idempotency_key=f"audit:exec:{idempotency_key}:reset",
        )
    if task.status != TaskStatus.READY:
        raise ServiceError(409, "仅 READY（或可恢复的 FAILED）任务可被部门执行")

    # (c) Claim: route_task requires READY and assigns the FIXED department agent.
    assignment = route_task(session, task_id, f"exec:{idempotency_key}:route", commit=False)
    if assignment is None:
        raise ServiceError(409, "任务无法认领（可能不是部门任务，或 agent 不可用）")

    # RUNNING (persisted by the upcoming build_context commit).
    before = task.status
    task.status = TaskStatus.RUNNING
    task.updated_at = now_utc()
    session.add(task)
    append_event(
        session,
        project_id=task.project_id,
        task_id=task.id,
        event_type="task.running",
        idempotency_key=f"exec:{idempotency_key}:running",
        payload={"before": before.value, "after": TaskStatus.RUNNING.value},
    )
    from aios.audit import append_audit

    append_audit(
        session,
        actor=actor,
        action="task.running",
        resource_type="task",
        resource_id=task.id,
        project_id=task.project_id,
        task_id=task.id,
        before={"status": before.value},
        after={"status": TaskStatus.RUNNING.value},
        idempotency_key=f"audit:exec:{idempotency_key}:running",
    )

    # Build the immutable TaskContext for the assigned agent (commits the context
    # AND the RUNNING state together).
    context = ContextService(session).build_context(task_id, assignment.id)

    # Run the adapter (the only part a test substitutes).
    try:
        result = adapter.run(
            task_id=task.id,
            task_context=context,
            output_schema=task.output_schema,
            idempotency_key=idempotency_key,
        )
    except ExecutionError as exc:
        category = getattr(exc, "category", AdapterErrorCategory.UNKNOWN).value
        reason = f"adapter_error|{category}|{_redact_secrets(exc.detail)}"
        # Persist structured retry accounting if the adapter supplied it (Codex #2).
        meta = None
        attempts = getattr(exc, "attempts", None)
        if attempts is not None:
            meta = {
                "attempts": attempts,
                "max_attempts": getattr(exc, "max_attempts", None),
            }
        _mark_failed(session, task, idempotency_key, reason, actor, meta=meta)
        raise
    except Exception as exc:  # noqa: BLE001 - surface as readable failure
        _mark_failed(
            session,
            task,
            idempotency_key,
            f"adapter_exception:{type(exc).__name__}",
            actor,
        )
        raise ExecutionError(502, _redact_secrets(f"部门执行失败：{exc}")) from exc

    # Validate each artifact's `data` against the task output_schema.
    try:
        for artifact in result.artifacts:
            validate(instance=artifact.get("data", {}), schema=task.output_schema)
    except JsonSchemaValidationError as exc:
        _mark_failed(session, task, idempotency_key, f"validation:{exc.message}", actor)
        raise ResultValidationError(422, f"执行结果未通过输出校验：{exc.message}") from exc

    checksum = _artifact_checksum(result)
    primary = result.artifacts[0] if result.artifacts else {}
    # Agent Interoperability Gateway (#57): if the adapter is a delegated
    # external agent, record provenance (adapter id, mode, remote run id, usage)
    # so the result is fully attributable and auditable. The secret itself is
    # NEVER written here (only the opaque secret_ref lives on DelegatedRun).
    from aios.delegation import DelegatedExecutionAdapter, build_delegated_provenance

    adapter_id = getattr(adapter, "agent_id", None) or (
        adapter.agent.id if isinstance(adapter, DelegatedExecutionAdapter) else None
    )
    source = (
        f"delegated:{adapter.mode.value}"
        if isinstance(adapter, DelegatedExecutionAdapter)
        else "execution_protocol"
    )
    provenance_json: dict[str, Any] = {}
    if isinstance(adapter, DelegatedExecutionAdapter):
        run = getattr(result, "_run", None)
        if run is not None:
            # The outer `run` object is stale: `_wait_for_completion` updates
            # remote_status/finished_at on a fresh DB-bound row inside its own
            # session. Re-read by id so provenance reflects the *final* run state
            # (Contract point 10). Cheap, idempotent, and avoids detached-object
            # drift between the in-memory run and what the DB actually persisted.
            from aios.models import DelegatedRun

            persisted_run = session.get(DelegatedRun, run.id)
            provenance_json = build_delegated_provenance(
                adapter, persisted_run if persisted_run is not None else run
            )
    # Persist retry accounting from the adapter (Codex #2): only when the adapter
    # actually reported attempts (else omitted to keep metadata clean). These are
    # plain counts — no secret/body/prompt — so safe to store verbatim.
    # Use getattr: the delegated adapter may return a shim (e.g. _ExecutionResultShim)
    # without a `metadata` field.
    attempt_meta = getattr(result, "metadata", None) or {}
    persisted_attempts = attempt_meta.get("attempts")
    artifact_metadata = {
        "summary": result.summary,
        "actor": actor,
        "adapter": source,
        "context_hash": context.context_hash,
        "artifacts": result.artifacts,
        "claims": result.claims,
    }
    if persisted_attempts is not None:
        artifact_metadata["attempts"] = persisted_attempts
        artifact_metadata["max_attempts"] = attempt_meta.get("max_attempts")
    artifact = Artifact(
        project_id=task.project_id,
        task_id=task.id,
        adapter_id=adapter_id,
        source=source,
        provenance_json=provenance_json,
        type=ArtifactType(primary.get("type", "json")),
        uri=primary.get("uri") or f"exec://{task.id}/{idempotency_key}",
        checksum=checksum,
        external_result_id=exec_result_id,
        result_checksum=checksum,
        metadata_json=artifact_metadata,
    )
    try:
        session.add(artifact)
        audit_after: dict[str, Any] = {
            "external_result_id": exec_result_id,
            "summary": result.summary,
        }
        if persisted_attempts is not None:
            audit_after["attempts"] = persisted_attempts
            audit_after["max_attempts"] = attempt_meta.get("max_attempts")
        append_audit(
            session,
            actor=actor,
            action="artifact.created",
            resource_type="artifact",
            resource_id=artifact.id,
            project_id=task.project_id,
            task_id=task.id,
            before={},
            after=audit_after,
            idempotency_key=f"audit:exec:{idempotency_key}:artifact",
        )
        # Complete the task (emits task.completed, idempotent on its own key).
        complete_task(session, task.id, f"exec:{idempotency_key}:complete")
        # Unlock downstream tasks (T1 DONE -> T2 READY, etc.).
        Orchestrator(session).process_pending()
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(artifact)
    return artifact


def _mark_failed(
    session: Session,
    task: Task,
    idempotency_key: str,
    reason: str,
    actor: str,
    *,
    meta: dict[str, Any] | None = None,
) -> None:
    from aios.audit import append_audit

    # Reason format convention (consumed by parse_adapter_error_reason):
    #   adapter_error|<CATEGORY>|<redacted-detail>   (ExecutionError, sanitized)
    #   adapter_exception:<ExceptionType>             (unexpected Python error)
    #   validation:<message>                          (output-schema failure)
    #   adapter_error                                 (pre-classification legacy)
    # The <redacted-detail> portion is safe to persist (no secret/body/prompt).
    # `meta` carries structured, secret-free extras (e.g. retry attempts /
    # max_attempts) merged into the event + audit record so attempt accounting
    # is queryable, not just buried in the human-readable reason string.
    before = task.status
    task.status = TaskStatus.FAILED
    task.updated_at = now_utc()
    session.add(task)
    event_payload: dict[str, Any] = {"reason": reason, "before": before.value}
    if meta:
        event_payload.update(meta)
    append_event(
        session,
        project_id=task.project_id,
        task_id=task.id,
        event_type="task.failed",
        idempotency_key=f"exec:{idempotency_key}:failed",
        payload=event_payload,
    )
    audit_after: dict[str, Any] = {"status": TaskStatus.FAILED.value, "reason": reason}
    if meta:
        audit_after.update(meta)
    append_audit(
        session,
        actor=actor,
        action="task.failed",
        resource_type="task",
        resource_id=task.id,
        project_id=task.project_id,
        task_id=task.id,
        before={"status": before.value},
        after=audit_after,
        idempotency_key=f"audit:exec:{idempotency_key}:failed",
    )
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


class AdapterErrorReason(NamedTuple):
    """Structured view of a persisted ``task.failed`` / audit ``reason``.

    External tooling should parse reasons through ``parse_adapter_error_reason``
    rather than hard-matching the bare literal ``"adapter_error"`` (the format
    changed in PR #79 to carry a recoverable category). See ``_mark_failed``.
    """

    prefix: str  # one of: adapter_error | adapter_exception | validation | <other>
    category: str | None  # AdapterErrorCategory value when present, else None
    detail: str  # redacted human-readable detail (never contains secrets)


def parse_adapter_error_reason(reason: str) -> AdapterErrorReason:
    """Parse a persisted failure ``reason`` into its parts.

    Never raises. Unknown formats are returned with ``prefix=<raw>`` and both
    ``category``/``detail`` as ``None``/``""`` so callers can fall back gracefully.
    """
    if reason.startswith("adapter_error|"):
        parts = reason.split("|", 2)
        category = parts[1] if len(parts) > 1 else None
        detail = parts[2] if len(parts) > 2 else ""
        return AdapterErrorReason("adapter_error", category or None, detail)
    if reason == "adapter_error":
        return AdapterErrorReason("adapter_error", None, "")
    if reason.startswith("adapter_exception:"):
        return AdapterErrorReason("adapter_exception", None, reason.split(":", 1)[1])
    if reason.startswith("validation:"):
        return AdapterErrorReason("validation", None, reason.split(":", 1)[1])
    return AdapterErrorReason(reason, None, "")


class LLMExecutionAdapter:
    """Production adapter: calls a configured OpenAI-compatible chat endpoint.

    Credentials come from the environment and are NEVER committed:
      AIOS_AGENT_BASE_URL  (default https://integrate.api.nvidia.com/v1)
      AIOS_AGENT_API_KEY
      AIOS_AGENT_MODEL     (default deepseek-ai/deepseek-v4-pro)
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        max_retries: int | None = None,
        backoff_seconds: float | None = None,
    ) -> None:
        self.base_url = (
            base_url or os.getenv("AIOS_AGENT_BASE_URL", "https://integrate.api.nvidia.com/v1")
        ).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("AIOS_AGENT_API_KEY")
        self.model = model or os.getenv("AIOS_AGENT_MODEL", "deepseek-ai/deepseek-v4-pro")
        # Retry is ON by default (owner cost approval captured in Issue #55) and
        # tunable: AIOS_AGENT_MAX_RETRIES (0 disables) and AIOS_AGENT_BACKOFF
        # (base seconds; exponential growth capped). Values are clamped so a bad
        # env cannot produce unbounded loops.
        raw_retries = (
            max_retries if max_retries is not None else os.getenv("AIOS_AGENT_MAX_RETRIES")
        )
        try:
            self.max_retries = int(raw_retries) if raw_retries is not None else DEFAULT_MAX_RETRIES
        except (TypeError, ValueError):
            self.max_retries = DEFAULT_MAX_RETRIES
        # HARD CAP (Codex blocking #1): never let a runaway/forged env value blow
        # up paid outbound calls. Clamp to MAX_RETRIES_HARD_CAP from above.
        if self.max_retries > MAX_RETRIES_HARD_CAP:
            self.max_retries = MAX_RETRIES_HARD_CAP
        if self.max_retries < 0:
            self.max_retries = 0
        raw_backoff = (
            backoff_seconds if backoff_seconds is not None else os.getenv("AIOS_AGENT_BACKOFF")
        )
        try:
            self.backoff_seconds = (
                float(raw_backoff) if raw_backoff is not None else DEFAULT_BACKOFF_SECONDS
            )
        except (TypeError, ValueError):
            self.backoff_seconds = DEFAULT_BACKOFF_SECONDS
        if self.backoff_seconds < 0:
            self.backoff_seconds = 0.0

    def run(
        self,
        *,
        task_id: str,
        task_context: TaskContext,
        output_schema: dict[str, Any],
        idempotency_key: str,
    ) -> ExecutionResult:
        if not self.api_key:
            raise ExecutionError(
                503,
                "执行适配器未配置：请设置 AIOS_AGENT_API_KEY 环境变量",
                category=AdapterErrorCategory.CONFIG_MISSING,
            )
        prompt = self._build_prompt(task_context, output_schema)
        # Retry loop: only TIMEOUT / NETWORK (see _RETRYABLE_CATEGORIES) are
        # retried. Non-retryable categories raise on the first attempt with a
        # single outbound call. Attempt accounting is exposed on the result so
        # execute_task can audit it. `last_sanitized` holds the redacted detail of
        # the final failure (never the raw exception / secret).
        last_exc: ExecutionError | None = None
        attempts = 0
        max_attempts = 1 + self.max_retries
        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            try:
                recovery = None if attempt == 1 else self._RETRY_RECOVERY_HINT
                raw = self._chat(prompt, attempt=attempt, recovery_hint=recovery)
            except ExecutionError as exc:
                last_exc = exc
                if exc.category in _RETRYABLE_CATEGORIES and attempt < max_attempts:
                    # Bounded sleep: exponential backoff capped at 30s. No secret
                    # or raw error text is carried into the recovery prompt.
                    delay = min(self.backoff_seconds * (2 ** (attempt - 1)), 30.0)
                    if delay > 0:
                        time.sleep(delay)
                    continue
                break  # non-retryable, or out of attempts
            else:
                data = self._parse_json(raw)
                if not isinstance(data, dict):
                    raise ExecutionError(
                        502,
                        "模型返回的不是 JSON 对象",
                        category=AdapterErrorCategory.PROVIDER_STRUCTURE,
                    )
                artifacts = [
                    {
                        "type": "json",
                        "uri": f"exec://{task_id}/{idempotency_key}",
                        "summary": data.get("summary", "执行产物"),
                        "data": data,
                    }
                ]
                result = ExecutionResult(
                    summary=data.get("summary", "执行产物"),
                    claims=[],
                    artifacts=artifacts,
                    metadata={"attempts": attempts, "max_attempts": max_attempts},
                )
                return result
        # All attempts exhausted (or a non-retryable category on the first try).
        assert last_exc is not None  # loop either raised or stored last_exc
        attempts_made = attempts
        sanitized_detail = _redact_secrets(last_exc.detail)
        err = ExecutionError(
            last_exc.status_code,
            f"执行在 {attempts_made} 次尝试后失败（{last_exc.category.value}）：{sanitized_detail}",
            category=last_exc.category,
        )
        # Persist attempt accounting for execute_task() to record (Codex #2).
        err.attempts = attempts_made
        err.max_attempts = max_attempts
        raise err from last_exc

    # Hint appended to the prompt on retry attempts. Deliberately contains NO
    # error text, exception string, or secret — just instructs the model to
    # retry. This satisfies Codex's "sanitize recovery prompt" requirement.
    _RETRY_RECOVERY_HINT = (
        "\n\n[系统提示] 上一次调用因瞬时网络/超时问题未返回，"
        "请在不改变任务目标的前提下重新生成结果。"
    )

    def _build_prompt(self, task_context: TaskContext, output_schema: dict[str, Any]) -> str:
        md = render_task_context_markdown(task_context)
        return (
            "你是一名严格按 schema 输出 JSON 的部门执行 agent，不要编造事实，"
            "所有结论需基于上下文中的证据。\n"
            "下面是本次任务的不可变上下文：\n\n"
            + md
            + "\n\n请产出符合以下 JSON Schema 的对象，只输出 JSON，不要额外说明：\n"
            + json.dumps(output_schema, ensure_ascii=False, indent=2)
        )

    def _chat(self, prompt: str, *, attempt: int = 1, recovery_hint: str | None = None) -> str:
        # `attempt` is the 1-based call index (for diagnostics). `recovery_hint`
        # (when present, on retries) is the sanitized instruction appended to the
        # prompt — it never contains the raw error or any secret (see
        # _RETRY_RECOVERY_HINT). This keeps the recovery path leak-free.
        effective_prompt = prompt + (recovery_hint or "")
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": effective_prompt}],
            "temperature": 0.3,
            "max_tokens": 4000,
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        raw = ""
        try:
            with urllib.request.urlopen(request, timeout=120) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as exc:
            raise ExecutionError(
                exc.code,
                f"模型返回 HTTP {exc.code}",
                category=AdapterErrorCategory.PROVIDER_HTTP,
            ) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            timed_out = isinstance(reason, (TimeoutError, socket.timeout)) or (
                "timed out" in str(reason).lower()
            )
            if timed_out:
                raise ExecutionError(
                    504, "模型调用超时", category=AdapterErrorCategory.TIMEOUT
                ) from exc
            raise ExecutionError(
                502, f"模型连接失败：{reason}", category=AdapterErrorCategory.NETWORK
            ) from exc
        except UnicodeDecodeError as exc:
            raise ExecutionError(
                502, "模型返回无法解码的响应体", category=AdapterErrorCategory.JSON_PARSE
            ) from exc
        except Exception as exc:  # noqa: BLE001 - transport/runtime error outside HTTP/URL
            # Restore the pre-#81 catch-all for transport exceptions that are not
            # HTTPError / URLError (e.g. a direct TimeoutError, ssl.SSLError, or any
            # other runtime error raised by urlopen). Without this, such failures
            # escape _chat() and reach execute_task's generic branch as
            # 'adapter_exception:<Type>', regressing the adapter-error contract.
            timed_out = isinstance(exc, (TimeoutError, socket.timeout)) or (
                "timed out" in str(getattr(exc, "reason", "")).lower()
            )
            if timed_out:
                raise ExecutionError(
                    504, "模型调用超时", category=AdapterErrorCategory.TIMEOUT
                ) from exc
            raise ExecutionError(
                502, f"模型调用失败：{exc}", category=AdapterErrorCategory.UNKNOWN
            ) from exc
        # Body-level JSON decode failure is categorized JSON_PARSE (uniform with
        # the content-level parse in _parse_json), not the catch-all UNKNOWN.
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExecutionError(
                502, "模型返回非 JSON 响应体", category=AdapterErrorCategory.JSON_PARSE
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ExecutionError(
                502, f"模型调用失败：{exc}", category=AdapterErrorCategory.UNKNOWN
            ) from exc
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExecutionError(
                502,
                f"模型返回结构异常：{exc}",
                category=AdapterErrorCategory.PROVIDER_STRUCTURE,
            ) from exc

    @staticmethod
    def _parse_json(text: str) -> Any:
        text = text.strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.startswith("json"):
                text = text[4:]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end != -1:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    pass
            raise ExecutionError(
                502, "模型未返回可解析的 JSON", category=AdapterErrorCategory.JSON_PARSE
            ) from None
