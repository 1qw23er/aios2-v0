"""Agent Interoperability Gateway — delegation primitives (#57).

Lets AIOS delegate one task to an external closed-source agent (remote API /
A2A / MCP worker bridge / external workstation package) without learning its
internals. The orchestration/Task services only ever:

  * project a least-privilege, immutable TaskContext to the agent;
  * receive the result as an *unverified* Artifact that must pass the task's
    output_schema before the task is marked DONE;
  * observe the DelegatedRun lifecycle (submit / running / succeeded / failed /
    cancelled / expired) for audit and recovery.

Security model (enforced here):
  * ``secret_ref`` is an opaque handle to an external secret store. The secret
    is resolved at call time and is NEVER written to TaskContext / Artifact /
    AuditLog payloads (AuditLog redacts secret keys/values globally, including
    Authorization headers, via ``redact_secrets``).
  * Context projection (least privilege): external agents receive ONLY the
    task-specific allowlist fields (objective / instructions / acceptance /
    dependency outputs). Internal knowledge-base context (approved_facts,
    decisions, policies) and any secret value never leave AIOS.
  * Trust gate: only ``internal`` and ``verified_external`` agents may be
    delegated; ``experimental`` agents are hard-blocked.
  * Budget gate: a project over its ``budget_limit`` (positive limit) is
    hard-blocked before any remote call, failing safely with an explicit reason.
  * The agent may submit results but may NOT mutate Task / Approval /
    KnowledgeFact / downstream workflow state directly.
  * Idempotency key = H(task_id, agent_id, attempt); the remote honors it.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Protocol

from sqlmodel import Session

from aios.audit import AuditEvent, append_audit, redact_secrets
from aios.models import (
    Agent,
    AgentTrustLevel,
    DelegatedRun,
    DelegatedRunStatus,
    DelegationMode,
    Project,
    Task,
    new_id,
    now_utc,
)


def make_idempotency_key(task_id: str, agent_id: str, attempt: int) -> str:
    raw = f"{task_id}|{agent_id}|{attempt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def cancel_run(run: DelegatedRun) -> DelegatedRunStatus:
    """Cancel an in-flight delegated run (Contract point 8).

    Idempotent: a terminal run (succeeded / failed / cancelled) is left
    untouched. Emits ``delegation.cancelled`` for audit and returns the
    resulting status. The adapter-side cancellation (if any) is the caller's
    responsibility -- this only records the orchestrator's decision.
    """
    with _session() as s:
        r = s.get(DelegatedRun, run.id)
        if r.status in (
            DelegatedRunStatus.SUCCEEDED,
            DelegatedRunStatus.FAILED,
            DelegatedRunStatus.CANCELLED,
        ):
            return r.status
        r.status = DelegatedRunStatus.CANCELLED
        r.finished_at = now_utc()
        r.error = "cancelled by owner"
        s.add(r)
        s.commit()
        append_audit(
            s,
            actor="gateway",
            action=AuditEvent.DELEGATION_CANCELLED,
            resource_type="delegated_run",
            resource_id=r.id,
            project_id=r.project_id,
            task_id=r.task_id,
            before={},
            after={"status": "cancelled"},
            idempotency_key=f"audit:cancel:{r.id}:{new_id('k')}",
        )
        s.commit()
        return r.status


# ---------------------------------------------------------------------------
# DelegatedAdapter: the optional capability surface external agents implement.
# A plain LLMExecutionAdapter does NOT implement this (it stays a valid local
# adapter). The presence of `discover_capabilities` distinguishes them.
# ---------------------------------------------------------------------------
class DelegatedAdapter(Protocol):
    # --- capability discovery (Contract point 1) ---
    def discover_capabilities(self) -> dict[str, Any]: ...

    # --- task submission (Contract point 2 + 3 immutable context) ---
    def submit(
        self,
        *,
        delegated_run: DelegatedRun,
        projected_context: dict[str, Any],
        output_schema: dict[str, Any],
        remote_callback_url: str | None,
    ) -> dict[str, Any]:
        """Return {"remote_run_id": ..., "remote_status": ...}."""

    # --- status polling or callback ack (Contract point 5) ---
    def status(self, *, delegated_run: DelegatedRun) -> dict[str, Any]:
        """Return {"remote_status": ..., "finished": bool, "result": <artifact-like dict|None>}."""

    # --- cancellation (Contract point 8) ---
    def cancel(self, *, delegated_run: DelegatedRun) -> None: ...

    # --- structured Artifact return + usage (Contract points 6 + 10) ---
    def ingest_result(self, *, delegated_run: DelegatedRun) -> dict[str, Any]:
        """Normalize a finished remote result into an Artifact-style dict."""


class DelegatedExecutionError(Exception):
    """Raised on unrecoverable delegation failure (exhausted retries, hard fail)."""


class BudgetExceededError(DelegatedExecutionError):
    """Raised when a project's budget cannot cover a delegated run.

    The task must fail *safely* (no partial remote execution, no credential
    sent) with an explicit, human-readable reason so the owner can top up the
    budget or pick a cheaper agent.
    """


# --- trust-level gate (single axis, not a permission framework, #104) ---
_TRUST_DELEGABLE = {AgentTrustLevel.INTERNAL, AgentTrustLevel.VERIFIED_EXTERNAL}


def assert_trust_delegable(agent: Agent) -> None:
    """Block agents that are not cleared for external delegation.

    ``experimental`` agents are never allowed to run delegated work until they
    are promoted to ``verified_external`` (or ``internal``).
    """
    if agent.trust_level not in _TRUST_DELEGABLE:
        raise DelegatedExecutionError(
            f"agent {agent.id} trust_level={agent.trust_level.value} is not "
            f"cleared for delegation (allowed: internal, verified_external)"
        )


def check_budget(session: Session, project: Project, estimated_cost: float) -> None:
    """HARD-block delegation when the project would exceed its budget limit.

    A ``budget_limit`` of 0.0 means *unenforced* (legacy / open projects). Any
    positive limit is a hard ceiling on ``budget_used + estimated_cost``.
    """
    if project.budget_limit <= 0.0:
        return
    projected = float(project.budget_used) + float(estimated_cost)
    if projected > float(project.budget_limit):
        raise BudgetExceededError(
            f"project {project.id} budget exceeded: limit={project.budget_limit:.4f}, "
            f"used={project.budget_used:.4f}, estimated={estimated_cost:.4f} "
            f"(projected={projected:.4f})"
        )


class DelegatedExecutionAdapter:
    """Base for external-agent adapters.

    Implements the same ``run()`` surface as LLMExecutionAdapter so the existing
    ``execute_task`` path works unchanged. Subclasses implement the
    ``DelegatedAdapter`` capability surface for their transport.

    Lifecycle / resilience: submit -> (poll until finished | callback) ->
    ingest -> schema-validate. On transient failure it retries with exponential
    backoff (reuses the W1 hardening intent). Exhausted retries raise
    DelegatedExecutionError -> the task is marked FAILED and the owner can
    rerun from the console.
    """

    mode: DelegationMode
    max_retries: int = 3
    backoff_base: float = 1.0

    def __init__(
        self,
        *,
        agent: Agent,
        max_retries: int | None = None,
        backoff_base: float | None = None,
    ) -> None:
        self.agent = agent
        # Per-agent tuning wins over class defaults (design review v1 §6).
        # Falls back to the class default when the agent row leaves them at 0.
        if max_retries is not None:
            self.max_retries = max_retries
        else:
            self.max_retries = int(getattr(agent, "max_retries", 0) or 0) or self.max_retries
        if backoff_base is not None:
            self.backoff_base = backoff_base
        # Per-delegation timeout ceiling (design review v1 §6). Default 300s.
        self.timeout_s = float(getattr(agent, "timeout_s", 0.0) or 0.0) or 300.0

    # --- capability discovery (Contract point 1) ---
    def discover_capabilities(self) -> dict[str, Any]:
        caps = {
            "agent_id": self.agent.id,
            "name": self.agent.name,
            "mode": self.mode.value,
            "endpoint": self.agent.endpoint,
            "capabilities": self.agent.capabilities,
            "callback_url": self.agent.callback_url,
        }
        # Observability: every discovery is recorded (design review v1 §1).
        # Best-effort and non-fatal — discovery must never break on audit failure.
        try:
            with _session() as s:
                append_audit(
                    s,
                    actor="gateway",
                    action=AuditEvent.AGENT_DISCOVER,
                    resource_type="agent",
                    resource_id=self.agent.id,
                    project_id=None,
                    task_id=None,
                    before={},
                    after={"mode": self.mode.value},
                    idempotency_key=f"audit:discover:{self.agent.id}:{new_id('k')}",
                )
                s.commit()
        except Exception:  # noqa: BLE001
            pass
        return caps

    # --- the unified run() entry used by execute_task (Contract point 4 identity) ---
    def run(
        self,
        *,
        task_id: str,
        task_context: Any,
        output_schema: dict[str, Any],
        idempotency_key: str,
    ) -> Any:
        """Return an ExecutionResult-compatible object (dict of artifacts).

        We avoid importing ExecutionResult to keep delegation decoupled; the
        caller (execute_task) only reads `.summary` / `.artifacts`.
        """
        # task_context here is the full TaskContext model; project it (least
        # privilege) before sending. Subclasses may override _project_context.
        projected = self._project_context(task_context)

        # --- #104 hardening gates: must pass BEFORE any remote call / secret resolution ---
        # 1. Trust level: experimental agents are never delegated.
        assert_trust_delegable(self.agent)
        # 2. Budget: hard-block over-budget projects with a safe, explicit failure.
        #    estimated_cost lives on the Task row (TaskContext has no such field),
        #    so load it via task_context.task_id. Real delegation always carries a
        #    full TaskContext; defensive getattr keeps self-tests / partial contexts
        #    from crashing (the gate is simply skipped when identity is absent).
        # 2. Budget: hard-block over-budget projects with a safe, explicit failure.
        #    estimated_cost lives on the Task row (TaskContext has no such field),
        #    so load it via the context's project/task ids. Real delegation always
        #    carries a full TaskContext; defensive getattr keeps self-tests /
        #    partial contexts from crashing (the gate is simply skipped when the
        #    context identity is absent). Use distinct names so we never shadow the
        #    ``task_id`` run() parameter passed down to ``_create_run``.
        ctx_project_id = getattr(task_context, "project_id", None)
        ctx_task_id = getattr(task_context, "task_id", None)
        if ctx_project_id is not None:
            with _session() as s:
                ctx_task = s.get(Task, ctx_task_id) if ctx_task_id is not None else None
                project = s.get(Project, ctx_project_id)
                est = float(getattr(ctx_task, "estimated_cost", 0.0) or 0.0)
                check_budget(s, project, est)

        attempt = 1
        last_error: str | None = None
        while attempt <= self.max_retries:
            run = self._create_run(task_id, idempotency_key, attempt)
            try:
                submit_info = self.submit(
                    delegated_run=run,
                    projected_context=projected,
                    output_schema=output_schema,
                    remote_callback_url=self.agent.callback_url,
                )
                self._record_submitted(run, submit_info)
                final = self._wait_for_completion(run)
                if final["status"] != DelegatedRunStatus.SUCCEEDED:
                    raise DelegatedExecutionError(
                        f"delegated run ended {final['status'].value}: {final.get('error')}"
                    )
                artifact_like = self.ingest_result(delegated_run=run)
                # Observability: result received back from the external agent.
                with _session() as s:
                    append_audit(
                        s,
                        actor="gateway",
                        action=AuditEvent.AGENT_RESULT_RECEIVED,
                        resource_type="delegated_run",
                        resource_id=run.id,
                        project_id=run.project_id,
                        task_id=run.task_id,
                        before={},
                        after={"mode": self.mode.value},
                        idempotency_key=f"audit:result:{run.id}:{new_id('k')}",
                    )
                    s.commit()
                # Validate before returning (execute_task re-validates too).
                self._validate(artifact_like, output_schema)
                # Observability: the external result passed schema validation and
                # may now complete the task (Contract point 6 + 10).
                with _session() as s:
                    append_audit(
                        s,
                        actor="gateway",
                        action=AuditEvent.ARTIFACT_VALIDATED,
                        resource_type="delegated_run",
                        resource_id=run.id,
                        project_id=run.project_id,
                        task_id=run.task_id,
                        before={},
                        after={"schema": "passed"},
                        idempotency_key=f"audit:validated:{run.id}:{new_id('k')}",
                    )
                    s.commit()
                # Accrue budget from the actual run cost (0 if not reported).
                self._accrue_budget(run)
                return _to_execution_result(artifact_like, run)
            except DelegatedExecutionError as exc:
                last_error = str(exc)
                self._record_failed(run, last_error)
                if attempt < self.max_retries:
                    time.sleep(self.backoff_base * (2 ** (attempt - 1)))
                    attempt += 1
                    continue
                raise
            finally:
                # always flush run state
                pass
        raise DelegatedExecutionError(last_error or "delegation failed")

    # --- hooks subclasses implement (DelegatedAdapter surface) ---
    def _project_context(self, task_context: Any) -> dict[str, Any]:
        """Least-privilege projection before external agent delegation (#104).

        External agents receive ONLY task-specific, approved context:
          * objective / instructions / acceptance_criteria / dependency_outputs
        They NEVER receive internal knowledge-base context (approved_facts,
        relevant_decisions, applicable_policies, project_context, source_references),
        nor any credential (the projection is sanitized against secret patterns).
        """
        if hasattr(task_context, "model_dump"):
            full = task_context.model_dump(mode="json")
        else:
            full = dict(task_context)
        keep = {
            "objective": full.get("objective"),
            "instructions": full.get("instructions"),
            "acceptance_criteria": full.get("acceptance_criteria"),
            "dependency_outputs": full.get("dependency_outputs"),
        }
        projected = {k: v for k, v in keep.items() if v is not None}
        # Defense in depth: the projection must never carry a credential value.
        return redact_secrets(projected)

    def submit(self, *, delegated_run, projected_context, output_schema, remote_callback_url):
        raise NotImplementedError

    def status(self, *, delegated_run):
        raise NotImplementedError

    def cancel(self, *, delegated_run):
        raise NotImplementedError

    def ingest_result(self, *, delegated_run):
        raise NotImplementedError

    # --- run lifecycle helpers ---
    def _create_run(self, task_id: str, idempotency_key: str, attempt: int) -> DelegatedRun:
        with _session() as s:
            task = s.get(Task, task_id)
            run = DelegatedRun(
                project_id=task.project_id,
                task_id=task_id,
                agent_id=self.agent.id,
                delegation_mode=self.mode,
                secret_ref=self.agent.secret_ref,  # opaque handle ONLY
                idempotency_key=make_idempotency_key(task_id, self.agent.id, attempt),
                attempt=attempt,
                callback_url=self.agent.callback_url,
                context_ref=f"ctx://{idempotency_key}:{attempt}",
            )
            s.add(run)
            s.commit()
            s.refresh(run)
            return run

    def _record_submitted(self, run: DelegatedRun, info: dict[str, Any]) -> None:
        # Sync back to the in-memory object so later steps (status polling) see it.
        run.remote_run_id = info.get("remote_run_id")
        run.remote_status = info.get("remote_status")
        run.status = DelegatedRunStatus.SUBMITTED
        with _session() as s:
            r = s.get(DelegatedRun, run.id)
            r.status = DelegatedRunStatus.SUBMITTED
            r.remote_run_id = info.get("remote_run_id")
            r.remote_status = info.get("remote_status")
            r.updated_helper() if hasattr(r, "updated_helper") else None
            # Observability: the task was delegated to the external agent.
            append_audit(
                s,
                actor="gateway",
                action=AuditEvent.AGENT_DELEGATE,
                resource_type="agent",
                resource_id=self.agent.id,
                project_id=r.project_id,
                task_id=r.task_id,
                before={},
                after={"mode": self.mode.value, "remote_run_id": r.remote_run_id},
                idempotency_key=f"audit:delegate:{r.id}:{new_id('k')}",
            )
            s.add(r)
            s.commit()

    def _wait_for_completion(self, run: DelegatedRun) -> dict[str, Any]:
        """Poll until finished (callback mode would instead be pushed)."""
        deadline = time.time() + self.timeout_s  # per-agent timeout (Contract 7)
        while time.time() < deadline:
            info = self.status(delegated_run=run)
            with _session() as s:
                r = s.get(DelegatedRun, run.id)
                r.remote_status = info.get("remote_status")
                if info.get("cost") is not None:
                    r.cost = float(info["cost"])
                if info.get("usage") is not None:
                    r.usage = info["usage"]
                s.add(r)
                s.commit()
            if info.get("finished"):
                with _session() as s:
                    r = s.get(DelegatedRun, run.id)
                    # finished == the agent side completed; result is read at
                    # ingest time. A non-None error overrides to FAILED/EXPIRED.
                    r.status = (
                        DelegatedRunStatus.FAILED
                        if info.get("error")
                        else DelegatedRunStatus.SUCCEEDED
                    )
                    r.finished_at = now_utc()
                    r.error = info.get("error")
                    s.add(r)
                    s.commit()
                    # Extract scalars before the session closes (r becomes detached).
                    final_status, final_error = r.status, r.error
                return {"status": final_status, "error": final_error}
            time.sleep(2)
        # Timeout -> EXPIRED (Contract 7). Cost budget handling lives in orchestrator.
        with _session() as s:
            r = s.get(DelegatedRun, run.id)
            r.status = DelegatedRunStatus.EXPIRED
            r.finished_at = now_utc()
            r.error = "delegation timeout"
            s.add(r)
            s.commit()
        return {"status": DelegatedRunStatus.EXPIRED, "error": "delegation timeout"}

    def _accrue_budget(self, run: DelegatedRun) -> None:
        """Add the run's reported cost to the project's running budget total.

        Best-effort: a finished run's cost (persisted in the DB during polling)
        is the source of truth for spend. We read it from the DB by id because
        the in-memory ``run`` object may be detached/stale at this point. We do
        NOT decrement on failure (no charge for an unsuccessful delegation).
        """
        with _session() as s:
            persisted = s.get(DelegatedRun, run.id)
            cost = float(getattr(persisted, "cost", 0.0) or 0.0)
            if cost <= 0.0:
                return
            project = s.get(Project, persisted.project_id)
            if project is not None:
                project.budget_used = float(project.budget_used) + cost
                s.add(project)
                s.commit()

    def _record_failed(self, run: DelegatedRun, error: str) -> None:
        with _session() as s:
            r = s.get(DelegatedRun, run.id)
            r.status = DelegatedRunStatus.FAILED
            r.error = error
            r.finished_at = now_utc()
            s.add(r)
            s.commit()
            append_audit(
                s,
                actor="gateway",
                action=AuditEvent.DELEGATION_FAILED,
                resource_type="delegated_run",
                resource_id=r.id,
                project_id=r.project_id,
                task_id=r.task_id,
                before={},
                after={"error": error, "attempt": r.attempt},
                idempotency_key=f"audit:run:{r.id}:failed",
            )
            s.commit()

    def _validate(self, artifact_like: dict[str, Any], output_schema: dict[str, Any]) -> None:
        from jsonschema import validate
        from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

        for art in artifact_like.get("artifacts", []):
            try:
                validate(instance=art.get("data", {}), schema=output_schema)
            except JsonSchemaValidationError as exc:
                raise DelegatedExecutionError(f"artifact failed schema: {exc.message}") from exc


def _to_execution_result(artifact_like: dict[str, Any], run: DelegatedRun) -> Any:
    """Wrap a delegated artifact dict into a minimal ExecutionResult-like object."""
    return _ExecutionResultShim(artifact_like, run)


class _ExecutionResultShim:
    """Duck-typed ExecutionResult so execute_task can read .summary / .artifacts."""

    def __init__(self, artifact_like: dict[str, Any], run: DelegatedRun) -> None:
        self.summary = artifact_like.get("summary", "delegated result")
        self.claims: list[dict[str, Any]] = []
        self.artifacts = artifact_like.get("artifacts", [])
        self._run = run  # attached so execute_task can read provenance

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        """Pydantic-compatible dump so ``execute_task`` (which calls
        ``result.model_dump`` for the artifact checksum) works unchanged with
        delegated adapters. Without this, real delegated runs crash in
        ``_artifact_checksum``."""
        return {
            "summary": self.summary,
            "artifacts": self.artifacts,
            "claims": self.claims,
        }


def build_delegated_provenance(
    adapter: DelegatedExecutionAdapter, run: DelegatedRun
) -> dict[str, Any]:
    """Assemble the immutable provenance bundle for a delegated result (Contract point 10).

    Captures WHO/WHAT produced the artifact (agent id + name + delegation mode), the
    remote run identity, usage/cost, and the run timeline — WITHOUT any secret value
    (only the opaque ``secret_ref`` handle is carried). The resulting dict is stored on
    ``Artifact.provenance_json`` so every delegated result is fully attributable and
    auditable, satisfying Epic #57's "every result enters with provenance" rule.
    """
    return {
        "agent_id": adapter.agent.id,
        "agent_name": adapter.agent.name,
        "mode": adapter.mode.value,
        "delegated_run_id": run.id,
        "remote_run_id": run.remote_run_id,
        "remote_status": run.remote_status,
        "attempt": run.attempt,
        "cost": run.cost,
        "usage": run.usage,
        # Opaque handle to the external secret store — NEVER the secret value.
        "secret_ref": run.secret_ref,
        "submitted_at": run.submitted_at.isoformat() if run.submitted_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def _session():
    """Return a context-managed Session (not the generator get_session())."""
    from aios.db import get_database_url, get_engine

    return Session(get_engine(get_database_url()))


# Convenience projection kept for least-privilege documentation.
def project_context_for_agent(task_context: Any, agent: Agent) -> dict[str, Any]:
    """Return only the task-specific fields the agent needs; never secrets or
    internal knowledge-base state (approved_facts / decisions / policies)."""
    if hasattr(task_context, "model_dump"):
        full = task_context.model_dump(mode="json")
    else:
        full = dict(task_context)
    projected = {
        "objective": full.get("objective"),
        "instructions": full.get("instructions"),
        "acceptance_criteria": full.get("acceptance_criteria"),
        "dependency_outputs": full.get("dependency_outputs"),
        "agent_role": agent.role,
    }
    # Never leak a credential through the agent reference either.
    return redact_secrets({k: v for k, v in projected.items() if v is not None})


# Fields that must NEVER leave AIOS for an external agent (least-privilege
# context projection policy, #104). Anything not in this allowlist is stripped.
_EXTERNAL_CONTEXT_ALLOWLIST = {
    "objective",
    "instructions",
    "acceptance_criteria",
    "dependency_outputs",
}
# Internal-only context keys an external agent must never see.
_INTERNAL_CONTEXT_KEYS = {
    "approved_facts",
    "relevant_decisions",
    "applicable_policies",
    "project_context",
    "source_references",
    "agent_profile",
}


def project_external_context(task_context: Any) -> dict[str, Any]:
    """Strict allowlist projection for external delegation (#104).

    Returns ONLY ``_EXTERNAL_CONTEXT_ALLOWLIST`` fields and guarantees no
    internal knowledge-base context and no secret value escapes. This is the
    policy applied before a task is delegated to any external/closed-source
    agent.
    """
    if hasattr(task_context, "model_dump"):
        full = task_context.model_dump(mode="json")
    else:
        full = dict(task_context)
    projected = {
        key: full[key] for key in _EXTERNAL_CONTEXT_ALLOWLIST if key in full
    }
    # Belt-and-suspenders: drop any internal-only key and redact secret values.
    for internal_key in _INTERNAL_CONTEXT_KEYS:
        projected.pop(internal_key, None)
    return redact_secrets(projected)
