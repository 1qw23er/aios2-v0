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
from datetime import datetime
from typing import Any, Protocol

from sqlmodel import Session

from aios.audit import AuditEvent, append_audit, redact_secrets
from aios.callback_ingest import (
    CALLBACK_TTL_GRACE_SECONDS,
    build_callback_url,
    callback_signal,
    mint_callback_token,
)
from aios.execution_run import (
    acquire_run_lease,
    complete_run,
    new_run_lease_owner,
    release_run_lease,
    renew_run_lease,
    update_run_if_owned,
)
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
        # Callback / Webhook Ingest P1: minimal, backwards-compatible extension.
        # The run-scoped HMAC token travels as a SEPARATE field (never inside the
        # URL) so it cannot be captured by access logs / proxies / APM.
        remote_callback_token: str | None = None,
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


# Terminal states that may carry a (possibly positive) cost worth recording
# against the project budget. ``CANCELLED`` is intentionally excluded: a
# cancelled attempt did no billable work (cost is 0 in normal flows), so there
# is nothing to accrue. ``SUCCEEDED`` / ``FAILED`` / ``EXPIRED`` are the
# chargeable terminals -- crucially, a *failed-but-paid* attempt (the remote
# provider billed before failing) is accrued here, fixing the P0 gap where paid
# failures were never charged (C: Unified Attempt + Usage + Budget Accrual).
ACCRUABLE_RUN_STATUSES: tuple[DelegatedRunStatus, ...] = (
    DelegatedRunStatus.SUCCEEDED,
    DelegatedRunStatus.FAILED,
    DelegatedRunStatus.EXPIRED,
)


def accrue_run_budget(
    session: Session,
    *,
    run_id: str,
    now: datetime | None = None,
) -> bool:
    """Idempotently charge a terminal ``DelegatedRun`` to ``Project.budget_used``.

    This is the SOLE writer of ``Project.budget_used`` (governance invariant
    BA-1 / DR-D1-2: a single budget authority -- see
    ``tests/test_workforce_w6_invariants.py::test_budget_used_has_exactly_one_writer``).

    The gate UPDATE sets ``budget_accrued_at`` exactly once; the winning write
    then adds ``cost`` to ``Project.budget_used`` (only when ``cost > 0``). The
    gate makes this safe across retries, concurrent terminalizations, and
    recovery re-runs: at most one caller ever charges the project. ``None`` /
    zero cost is never fabricated into a charge.

    Used by ``execution_run.complete_run`` (every remote terminal transition)
    and recovery, so remote attempts, local LLM attempts, and reclaimed runs all
    accrue through one path. Returns True if this call performed the accrual.
    """
    from sqlalchemy import select, update

    now_naive = (now or now_utc()).replace(tzinfo=None)
    claim = (
        update(DelegatedRun)
        .where(DelegatedRun.id == run_id)
        .where(DelegatedRun.budget_accrued_at.is_(None))
        .where(DelegatedRun.status.in_(ACCRUABLE_RUN_STATUSES))
        .values(budget_accrued_at=now_naive)
    )
    if session.execute(claim).rowcount != 1:
        # Already accrued, or not (yet) a terminal/chargeable state.
        return False
    # Fresh core SELECT: the in-memory run object may be detached/stale, so read
    # the authoritative cost + project_id straight from the DB.
    cost, project_id = session.execute(
        select(DelegatedRun.cost, DelegatedRun.project_id).where(
            DelegatedRun.id == run_id
        )
    ).one()
    if cost > 0 and project_id:
        project = session.get(Project, project_id)
        if project is not None:
            project.budget_used = float(project.budget_used) + cost
            session.add(project)
    session.commit()
    return True


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
        # Execution Run Lifecycle P0: opaque, per-adapter-instance identity used
        # to fence every mutation of a run this adapter drives. NOT an agent_id
        # / employee_id / runtime identity -- see ``execution_run``.
        self._lease_owner = new_run_lease_owner()

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
        #    so load it via the context's project/task ids. Real delegation always
        #    carries a full TaskContext; defensive getattr keeps self-tests /
        #    partial contexts from crashing (the gate is simply skipped when the
        #    context identity is absent). Use distinct names so we never shadow the
        #    ``task_id`` run() parameter passed down to ``_create_run``.
        ctx_project_id = getattr(task_context, "project_id", None)
        ctx_task_id = getattr(task_context, "task_id", None)

        attempt = 1
        last_error: str | None = None
        while attempt <= self.max_retries:
            # Per-attempt hard budget gate (C: Unified Attempt + Usage + Budget
            # Accrual). Re-checked on EVERY attempt -- not once before the loop --
            # so it observes ``Project.budget_used`` grow as prior (paid) attempts
            # accrue. A costly retry sequence is therefore hard-blocked the moment
            # the project goes over budget, closing the old "single pre-check is
            # bypassed by retries" gap. ``est`` is this attempt's estimated cost.
            if ctx_project_id is not None:
                with _session() as s:
                    ctx_task = s.get(Task, ctx_task_id) if ctx_task_id is not None else None
                    project = s.get(Project, ctx_project_id)
                    est = float(getattr(ctx_task, "estimated_cost", 0.0) or 0.0)
                    check_budget(s, project, est)
            run = self._create_run(task_id, idempotency_key, attempt)
            # Callback / Webhook Ingest P1: mint a run-scoped token + the
            # AIOS-hosted callback URL for THIS run/attempt. Best-effort by
            # design -- if the owner signing key is not configured we simply
            # fall back to today's polling-only behaviour, because a callback is
            # an accelerator and never a correctness dependency. The token is
            # never persisted and never placed in the URL.
            callback_url = None
            callback_token = None
            try:
                callback_url = build_callback_url(run.id)
                callback_token = mint_callback_token(
                    run_id=run.id,
                    attempt=attempt,
                    agent_id=self.agent.id,
                    ttl_seconds=self.timeout_s + CALLBACK_TTL_GRACE_SECONDS,
                )
            except Exception:  # noqa: BLE001 - callback is optional, never fatal
                callback_url = None
                callback_token = None
            try:
                submit_info = self.submit(
                    delegated_run=run,
                    projected_context=projected,
                    output_schema=output_schema,
                    remote_callback_url=callback_url,
                    remote_callback_token=callback_token,
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
                # P0: the run reached a validated terminal outcome -- give up
                # the lease so the record no longer looks in-flight. Budget
                # accrual for this SUCCEEDED run is handled once, idempotently,
                # inside ``complete_run`` (which calls ``accrue_run_budget``).
                with _session() as s:
                    release_run_lease(s, run_id=run.id, owner=self._lease_owner)
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

    def submit(
        self,
        *,
        delegated_run,
        projected_context,
        output_schema,
        remote_callback_url,
        remote_callback_token: str | None = None,
    ):
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
            # Execution Run Lifecycle P0: take the durable lease immediately. A
            # freshly inserted row has a NULL lease, so this always succeeds --
            # but it goes through the same conditional UPDATE as any other
            # acquirer, so the run is protected from the recovery scan from the
            # moment it exists.
            acquire_run_lease(s, run_id=run.id, owner=self._lease_owner)
            s.refresh(run)
            return run

    def _record_submitted(self, run: DelegatedRun, info: dict[str, Any]) -> None:
        # Sync back to the in-memory object so later steps (status polling) see it.
        run.remote_run_id = info.get("remote_run_id")
        run.remote_status = info.get("remote_status")
        run.status = DelegatedRunStatus.SUBMITTED
        with _session() as s:
            r = s.get(DelegatedRun, run.id)
            # Fenced (P0): only the current lease holder records the submit
            # result. The write goes through a conditional UPDATE instead of
            # ORM attribute assignment so a stale owner can never persist.
            update_run_if_owned(
                s,
                run_id=run.id,
                owner=self._lease_owner,
                values={
                    "status": DelegatedRunStatus.SUBMITTED,
                    "remote_run_id": info.get("remote_run_id"),
                    "remote_status": info.get("remote_status"),
                },
            )
            s.refresh(r)
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
            s.commit()

    def _completion_signal(self, run: DelegatedRun) -> dict[str, Any] | None:
        """Return the staged callback evidence for ``run``, or ``None``.

        Callback / Webhook Ingest P1. This is a READ: it does not terminalize,
        does not accrue, and does not touch the lease. It only lets the polling
        loop skip one remote ``status()`` round-trip when the provider already
        told us the outcome. Terminalization stays in ``complete_run`` below.
        """
        with _session() as s:
            persisted = s.get(DelegatedRun, run.id)
            signal = callback_signal(persisted) if persisted is not None else None
        if not signal or not signal.get("finished"):
            return None
        status = str(signal.get("status") or "")
        terminal = {
            "succeeded": DelegatedRunStatus.SUCCEEDED,
            "failed": DelegatedRunStatus.FAILED,
            "cancelled": DelegatedRunStatus.CANCELLED,
        }.get(status, DelegatedRunStatus.FAILED)
        return {
            "remote_status": status,
            "finished": True,
            "error": signal.get("error"),
            "cost": signal.get("cost"),
            "usage": signal.get("usage"),
            "terminal_status": terminal,
            "source": "callback",
        }

    def _wait_for_completion(self, run: DelegatedRun) -> dict[str, Any]:
        """Poll until finished (callback mode would instead be pushed).

        Execution Run Lifecycle P0: the durable lease is renewed before every
        poll and every write is fenced on it. If the lease is lost (another
        process reclaimed the run) we fail closed -- we must never mutate a run
        we no longer own; the recovery scan then decides its fate.
        """
        deadline = time.time() + self.timeout_s  # per-agent timeout (Contract 7)
        while time.time() < deadline:
            with _session() as s:
                if not renew_run_lease(s, run_id=run.id, owner=self._lease_owner):
                    raise DelegatedExecutionError(
                        f"delegated run {run.id} lost its execution lease"
                    )
            # Callback / Webhook Ingest P1: prefer already-staged callback
            # EVIDENCE over another remote round-trip. This only substitutes
            # where ``info`` comes from -- terminalization below is still
            # ``complete_run``, still fenced on OUR lease.
            info = self._completion_signal(run)
            if info is None:
                info = self.status(delegated_run=run)
            with _session() as s:
                values: dict[str, Any] = {"remote_status": info.get("remote_status")}
                if info.get("cost") is not None:
                    values["cost"] = float(info["cost"])
                if info.get("usage") is not None:
                    values["usage"] = info["usage"]
                if not update_run_if_owned(
                    s, run_id=run.id, owner=self._lease_owner, values=values
                ):
                    raise DelegatedExecutionError(
                        f"delegated run {run.id} lost its execution lease"
                    )
            if info.get("finished"):
                # finished == the agent side completed; result is read at
                # ingest time. A non-None error overrides to FAILED/EXPIRED.
                terminal = info.get("terminal_status") or (
                    DelegatedRunStatus.FAILED
                    if info.get("error")
                    else DelegatedRunStatus.SUCCEEDED
                )
                with _session() as s:
                    if not complete_run(
                        s,
                        run_id=run.id,
                        owner=self._lease_owner,
                        status=terminal,
                        error=info.get("error"),
                    ):
                        raise DelegatedExecutionError(
                            f"delegated run {run.id} lost its execution lease"
                        )
                    r = s.get(DelegatedRun, run.id)
                    # Extract scalars before the session closes (r becomes detached).
                    final_status, final_error = r.status, r.error
                return {"status": final_status, "error": final_error}
            time.sleep(2)
        # Timeout -> EXPIRED (Contract 7). Cost budget handling lives in orchestrator.
        with _session() as s:
            complete_run(
                s,
                run_id=run.id,
                owner=self._lease_owner,
                status=DelegatedRunStatus.EXPIRED,
                error="delegation timeout",
            )
        return {"status": DelegatedRunStatus.EXPIRED, "error": "delegation timeout"}

    def _record_failed(self, run: DelegatedRun, error: str) -> None:
        with _session() as s:
            r = s.get(DelegatedRun, run.id)
            # Fenced (P0): only the current lease holder may terminalize. An
            # EXPIRED run may still be relabelled FAILED by its own owner (a
            # timed-out attempt), which preserves the existing behaviour.
            if not complete_run(
                s,
                run_id=run.id,
                owner=self._lease_owner,
                status=DelegatedRunStatus.FAILED,
                error=error,
            ):
                # We no longer own this run, or it already reached a settled
                # outcome. Write nothing and leave no misleading audit record --
                # the recovery scan owns its fate from here.
                return
            release_run_lease(s, run_id=run.id, owner=self._lease_owner)
            s.refresh(r)
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
