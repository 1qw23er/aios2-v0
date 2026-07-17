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
from typing import Any, Protocol

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


class ExecutionAdapter(Protocol):
    def run(
        self,
        *,
        task_id: str,
        task_context: TaskContext,
        output_schema: dict[str, Any],
        idempotency_key: str,
    ) -> ExecutionResult: ...


class ExecutionError(ServiceError):
    """Raised when execution fails (adapter error or invalid output)."""


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
    except ExecutionError:
        _mark_failed(session, task, idempotency_key, "adapter_error", actor)
        raise
    except Exception as exc:  # noqa: BLE001 - surface as readable failure
        _mark_failed(
            session,
            task,
            idempotency_key,
            f"adapter_exception:{type(exc).__name__}",
            actor,
        )
        raise ExecutionError(502, f"部门执行失败：{exc}") from exc

    # Validate each artifact's `data` against the task output_schema.
    try:
        for artifact in result.artifacts:
            validate(instance=artifact.get("data", {}), schema=task.output_schema)
    except JsonSchemaValidationError as exc:
        _mark_failed(session, task, idempotency_key, f"validation:{exc.message}", actor)
        raise ResultValidationError(422, f"执行结果未通过输出校验：{exc.message}") from exc

    checksum = _artifact_checksum(result)
    primary = result.artifacts[0] if result.artifacts else {}
    artifact = Artifact(
        project_id=task.project_id,
        task_id=task.id,
        type=ArtifactType(primary.get("type", "json")),
        uri=primary.get("uri") or f"exec://{task.id}/{idempotency_key}",
        checksum=checksum,
        external_result_id=exec_result_id,
        result_checksum=checksum,
        metadata_json={
            "summary": result.summary,
            "actor": actor,
            "adapter": "execution_protocol",
            "context_hash": context.context_hash,
            "artifacts": result.artifacts,
            "claims": result.claims,
        },
    )
    try:
        session.add(artifact)
        append_audit(
            session,
            actor=actor,
            action="artifact.created",
            resource_type="artifact",
            resource_id=artifact.id,
            project_id=task.project_id,
            task_id=task.id,
            before={},
            after={"external_result_id": exec_result_id, "summary": result.summary},
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
    session: Session, task: Task, idempotency_key: str, reason: str, actor: str
) -> None:
    from aios.audit import append_audit

    before = task.status
    task.status = TaskStatus.FAILED
    task.updated_at = now_utc()
    session.add(task)
    append_event(
        session,
        project_id=task.project_id,
        task_id=task.id,
        event_type="task.failed",
        idempotency_key=f"exec:{idempotency_key}:failed",
        payload={"reason": reason, "before": before.value},
    )
    append_audit(
        session,
        actor=actor,
        action="task.failed",
        resource_type="task",
        resource_id=task.id,
        project_id=task.project_id,
        task_id=task.id,
        before={"status": before.value},
        after={"status": TaskStatus.FAILED.value, "reason": reason},
        idempotency_key=f"audit:exec:{idempotency_key}:failed",
    )
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


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
    ) -> None:
        self.base_url = (
            base_url or os.getenv("AIOS_AGENT_BASE_URL", "https://integrate.api.nvidia.com/v1")
        ).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("AIOS_AGENT_API_KEY")
        self.model = model or os.getenv("AIOS_AGENT_MODEL", "deepseek-ai/deepseek-v4-pro")

    def run(
        self,
        *,
        task_id: str,
        task_context: TaskContext,
        output_schema: dict[str, Any],
        idempotency_key: str,
    ) -> ExecutionResult:
        if not self.api_key:
            raise ExecutionError(503, "执行适配器未配置：请设置 AIOS_AGENT_API_KEY 环境变量")
        prompt = self._build_prompt(task_context, output_schema)
        raw = self._chat(prompt)
        data = self._parse_json(raw)
        if not isinstance(data, dict):
            raise ExecutionError(502, "模型返回的不是 JSON 对象")
        artifacts = [
            {
                "type": "json",
                "uri": f"exec://{task_id}/{idempotency_key}",
                "summary": data.get("summary", "执行产物"),
                "data": data,
            }
        ]
        return ExecutionResult(
            summary=data.get("summary", "执行产物"), claims=[], artifacts=artifacts
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

    def _chat(self, prompt: str) -> str:
        import urllib.request

        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
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
        try:
            with urllib.request.urlopen(request, timeout=120) as resp:
                body = json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            raise ExecutionError(502, f"模型调用失败：{exc}") from exc
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExecutionError(502, f"模型返回结构异常：{exc}") from exc

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
            raise ExecutionError(502, "模型未返回可解析的 JSON") from None
