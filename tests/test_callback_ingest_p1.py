"""Callback / Webhook Ingest P1 -- behavioral contract tests.

The contract under test is deliberately narrow, because the design is narrow:

    a callback is EVIDENCE, never AUTHORITY

Specifically these tests prove that:

* a run-scoped HMAC token authenticates the push, and only for (run, attempt,
  agent) -- never across runs, attempts or agents;
* the ingest path writes EXACTLY the evidence columns -- no status, no cost, no
  usage, no ``Project.budget_used``, no artifact, no execution;
* duplicates / late / conflicting / unknown-run deliveries are acknowledged 2xx
  (no 404, no 409) and are distinguishable only in the audit trail;
* budget still accrues exactly once, and only through the existing
  lease-owning completion path -- never from the callback;
* the token never lands in the payload, the audit trail, or an error message.

DB is provided by ``authenticated_client`` (conftest runs a real Alembic
``upgrade head``, so ``20260909_0004`` columns are present).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.audit import AuditLog
from aios.callback_ingest import (
    CALLBACK_MAX_BODY_BYTES,
    CALLBACK_TOKEN_HEADER,
    CallbackAuthError,
    build_callback_url,
    callback_signal,
    ingest_callback,
    mint_callback_token,
    normalise_callback_payload,
    verify_callback_token,
)
from aios.db import get_database_url, get_engine
from aios.execution_run import acquire_run_lease, complete_run
from aios.models import (
    AdapterType,
    Agent,
    AgentTrustLevel,
    DelegatedRun,
    DelegatedRunStatus,
    DelegationMode,
    Project,
    Task,
    TaskStatus,
)

OWNER_KEY = "k" * 40  # >= MIN_OWNER_API_KEY_LENGTH (32)


# --- helpers ---------------------------------------------------------------


def _naive_now() -> datetime:
    return datetime.utcnow()


def _seed(session: Session, *, budget_limit: float = 0.0):
    project = Project(name="p", objective="o", budget_limit=budget_limit)
    session.add(project)
    session.commit()
    session.refresh(project)

    agent = Agent(
        name="Fake",
        role="worker",
        adapter_type=AdapterType.EXTERNAL,
        delegation_mode=DelegationMode.WORKSTATION,
        capabilities=["x"],
        enabled=True,
        timeout_s=300.0,
        max_retries=1,
        trust_level=AgentTrustLevel.INTERNAL,
    )
    session.add(agent)
    session.commit()
    session.refresh(agent)

    task = Task(
        project_id=project.id,
        title="t",
        description="d",
        status=TaskStatus.BACKLOG,
        output_schema={"type": "object"},
        estimated_cost=0.0,
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return project, agent, task


def _run(
    session: Session,
    *,
    project_id: str,
    task_id: str,
    agent_id: str | None = None,
    status: DelegatedRunStatus = DelegatedRunStatus.SUBMITTED,
    attempt: int = 1,
    cost: float = 0.0,
) -> DelegatedRun:
    run = DelegatedRun(
        project_id=project_id,
        task_id=task_id,
        agent_id=agent_id,
        delegation_mode=DelegationMode.WORKSTATION,
        status=status,
        idempotency_key=f"idem-{uuid4().hex[:12]}",
        attempt=attempt,
        cost=cost,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _token(
    *,
    run_id: str,
    agent_id: str,
    attempt: int = 1,
    ttl: float = 600.0,
    jti: str | None = None,
    key: str = OWNER_KEY,
) -> str:
    return mint_callback_token(
        run_id=run_id,
        attempt=attempt,
        agent_id=agent_id,
        ttl_seconds=ttl,
        jti=jti,
        key=key,
    )


def _claims(*, run_id: str, agent_id: str, attempt: int = 1, jti: str = "j1"):
    return verify_callback_token(
        _token(run_id=run_id, agent_id=agent_id, attempt=attempt, jti=jti)
    )


def _payload(
    *,
    status: str = "succeeded",
    error: str | None = None,
    cost: float | None = None,
    usage: dict | None = None,
    remote_run_id: str = "ext-1",
) -> dict:
    return normalise_callback_payload(
        {
            "status": status,
            "error": error,
            "cost": cost,
            "usage": usage,
            "remote_run_id": remote_run_id,
        }
    )


@pytest.fixture(autouse=True)
def _owner_key(monkeypatch) -> None:
    """Token mint/verify read the owner key from the environment."""
    monkeypatch.setenv("AIOS_OWNER_API_KEY", OWNER_KEY)


@pytest.fixture
def db(authenticated_client) -> Session:
    with Session(get_engine(get_database_url())) as s:
        yield s


@pytest.fixture
def client(authenticated_client) -> TestClient:
    return authenticated_client


# --- Authentication --------------------------------------------------------


def test_valid_token_is_accepted(db) -> None:
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    claims = _claims(run_id=run.id, agent_id=agent.id)
    assert claims.run_id == run.id and claims.attempt == 1


def test_invalid_signature_rejected() -> None:
    token = _token(run_id="r1", agent_id="a1", key=OWNER_KEY)
    forged = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    with pytest.raises(CallbackAuthError) as exc:
        verify_callback_token(forged)
    assert exc.value.reason == "invalid_signature"


def test_expired_token_rejected() -> None:
    token = _token(run_id="r1", agent_id="a1", ttl=-10.0)
    with pytest.raises(CallbackAuthError) as exc:
        verify_callback_token(token)
    assert exc.value.reason == "expired"


def test_malformed_token_rejected() -> None:
    with pytest.raises(CallbackAuthError) as exc:
        verify_callback_token("not-a-token")
    assert exc.value.reason == "malformed"


def test_missing_token_rejected() -> None:
    with pytest.raises(CallbackAuthError) as exc:
        verify_callback_token(None)
    assert exc.value.reason == "missing"


def test_token_cannot_cross_attempt(db) -> None:
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id, attempt=1)
    claims = _claims(run_id=run.id, agent_id=agent.id, attempt=2, jti="jx")
    result = ingest_callback(
        db, run_id=run.id, claims=claims, payload=_payload(), now=_naive_now()
    )
    assert result.outcome == "binding_mismatch"
    db.refresh(run)
    assert run.callback_received_at is None
    assert run.status == DelegatedRunStatus.SUBMITTED


def test_token_cannot_cross_agent(db) -> None:
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    claims = _claims(run_id=run.id, agent_id="someone-else", jti="jy")
    result = ingest_callback(
        db, run_id=run.id, claims=claims, payload=_payload(), now=_naive_now()
    )
    assert result.outcome == "binding_mismatch"
    db.refresh(run)
    assert run.callback_payload is None


def test_token_cannot_cross_run(client, db) -> None:
    """A token bound to run A is rejected on run B (HTTP-level binding check)."""
    project, agent, task = _seed(db)
    run_a = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    run_b = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    token = _token(run_id=run_a.id, agent_id=agent.id, jti="jz")
    resp = client.post(
        f"/runs/{run_b.id}/callback",
        json={"status": "succeeded"},
        headers={CALLBACK_TOKEN_HEADER: token},
    )
    assert resp.status_code == 401
    db.refresh(run_b)
    assert run_b.callback_received_at is None


# --- Persistence -----------------------------------------------------------


def test_callback_evidence_is_persisted(db) -> None:
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    result = ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="j-1"),
        payload=_payload(status="failed", error="boom", cost=1.5, usage={"t": 3}),
        now=_naive_now(),
    )
    assert result.outcome == "received"
    db.refresh(run)
    assert run.callback_received_at is not None
    assert run.callback_payload["status"] == "failed"
    assert run.callback_payload["error"] == "boom"
    assert run.callback_payload["cost"] == 1.5
    assert run.callback_payload["usage"] == {"t": 3}
    assert run.callback_payload["jti"] == "j-1"


def test_callback_never_writes_status_cost_or_usage(db) -> None:
    """The core invariant: evidence only."""
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id, cost=0.0)
    ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="j-2"),
        payload=_payload(status="succeeded", cost=9.0, usage={"x": 1}),
        now=_naive_now(),
    )
    db.refresh(run)
    assert run.status == DelegatedRunStatus.SUBMITTED  # NOT succeeded
    assert run.cost == 0.0  # NOT 9.0
    assert run.usage is None  # NOT {"x": 1}
    assert run.finished_at is None


def test_payload_is_redacted(db) -> None:
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    payload = normalise_callback_payload({"status": "succeeded"})
    payload["note"] = "Authorization: Bearer sk-abcdef1234567890"
    ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="j-3"),
        payload=payload,
        now=_naive_now(),
    )
    db.refresh(run)
    assert "sk-abcdef1234567890" not in str(run.callback_payload)


# --- Idempotency -----------------------------------------------------------


def test_duplicate_callback_is_not_reapplied(db) -> None:
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    claims = _claims(run_id=run.id, agent_id=agent.id, jti="dup-1")
    first = ingest_callback(
        db, run_id=run.id, claims=claims, payload=_payload(cost=2.0), now=_naive_now()
    )
    second = ingest_callback(
        db, run_id=run.id, claims=claims, payload=_payload(cost=2.0), now=_naive_now()
    )
    assert first.outcome == "received"
    assert second.outcome == "duplicate"
    db.refresh(run)
    assert run.callback_payload["jti"] == "dup-1"


def test_conflicting_callback_does_not_overwrite_first_evidence(db) -> None:
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="first"),
        payload=_payload(status="succeeded"),
        now=_naive_now(),
    )
    other = ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="second"),
        payload=_payload(status="failed", error="contradiction"),
        now=_naive_now(),
    )
    assert other.outcome == "conflict"
    db.refresh(run)
    assert run.callback_payload["jti"] == "first"
    assert run.callback_payload["status"] == "succeeded"


def test_duplicate_after_terminalization_is_late(db) -> None:
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    acquire_run_lease(db, run_id=run.id, owner="w1")
    complete_run(db, run_id=run.id, owner="w1", status=DelegatedRunStatus.SUCCEEDED)
    claims = _claims(run_id=run.id, agent_id=agent.id, jti="late-1")
    result = ingest_callback(
        db, run_id=run.id, claims=claims, payload=_payload(), now=_naive_now()
    )
    assert result.outcome == "late"


# --- Lifecycle -------------------------------------------------------------


def test_callback_before_completion_leaves_run_open(db) -> None:
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="early"),
        payload=_payload(status="succeeded", cost=1.0),
        now=_naive_now(),
    )
    db.refresh(run)
    assert run.status == DelegatedRunStatus.SUBMITTED
    signal = callback_signal(run)
    assert signal["finished"] is True
    assert signal["status"] == "succeeded"


def test_callback_after_terminalization_never_reopens(db) -> None:
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    acquire_run_lease(db, run_id=run.id, owner="w1")
    complete_run(db, run_id=run.id, owner="w1", status=DelegatedRunStatus.FAILED)
    ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="after"),
        payload=_payload(status="succeeded"),
        now=_naive_now(),
    )
    db.refresh(run)
    assert run.status == DelegatedRunStatus.FAILED


def test_callback_for_cancelled_run_is_late(db) -> None:
    project, agent, task = _seed(db)
    run = _run(
        db,
        project_id=project.id,
        task_id=task.id,
        agent_id=agent.id,
        status=DelegatedRunStatus.CANCELLED,
    )
    result = ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="cxl"),
        payload=_payload(status="succeeded"),
        now=_naive_now(),
    )
    assert result.outcome == "late"


def test_callback_for_expired_run_is_late(db) -> None:
    """Recovery already expired it -- the callback must not resurrect it."""
    project, agent, task = _seed(db)
    run = _run(
        db,
        project_id=project.id,
        task_id=task.id,
        agent_id=agent.id,
        status=DelegatedRunStatus.EXPIRED,
    )
    result = ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="exp"),
        payload=_payload(status="succeeded", cost=5.0),
        now=_naive_now(),
    )
    assert result.outcome == "late"
    db.refresh(project)
    assert project.budget_used == 0.0  # callback is never a budget writer


# --- Late evidence (GAP-4) --------------------------------------------------


def test_late_callback_to_terminal_run_persists_evidence(db) -> None:
    """GAP-4: the first late delivery to an already-terminal run is no longer
    silently dropped -- it stages evidence through the same CAS gate while the
    terminal state, money columns and lease stay untouched."""
    project, agent, task = _seed(db, budget_limit=100.0)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    acquire_run_lease(db, run_id=run.id, owner="w1")
    complete_run(db, run_id=run.id, owner="w1", status=DelegatedRunStatus.FAILED)
    db.refresh(run)
    before = (
        run.status,
        run.finished_at,
        run.cost,
        run.lease_owner,
        run.lease_expires_at,
    )
    result = ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="late-ev"),
        payload=_payload(status="succeeded", cost=5.0, usage={"t": 2}),
        now=_naive_now(),
    )
    assert result.outcome == "late"
    db.refresh(run)
    assert run.callback_received_at is not None
    assert run.callback_payload["jti"] == "late-ev"
    assert run.callback_payload["status"] == "succeeded"
    assert run.callback_payload["cost"] == 5.0
    assert run.callback_payload["usage"] == {"t": 2}
    # Evidence, not authority: nothing about the terminal run moved.
    assert (
        run.status,
        run.finished_at,
        run.cost,
        run.lease_owner,
        run.lease_expires_at,
    ) == before
    assert run.usage is None
    db.refresh(project)
    assert project.budget_used == pytest.approx(0.0)
    late_audits = [
        entry
        for entry in db.exec(
            select(AuditLog).where(AuditLog.resource_id == run.id)
        ).all()
        if entry.after_snapshot.get("outcome") == "late"
    ]
    assert late_audits, "late delivery must be audited as outcome=late"


def test_second_late_callback_same_jti_is_duplicate_first_wins(db) -> None:
    """GAP-4: once the first late delivery has staged evidence on a terminal
    run, a replay of the SAME delivery is a duplicate -- first evidence wins
    and the CAS gate is never reopened."""
    project, agent, task = _seed(db)
    run = _run(
        db,
        project_id=project.id,
        task_id=task.id,
        agent_id=agent.id,
        status=DelegatedRunStatus.EXPIRED,
    )
    claims = _claims(run_id=run.id, agent_id=agent.id, jti="late-dup")
    first = ingest_callback(
        db, run_id=run.id, claims=claims, payload=_payload(cost=2.0), now=_naive_now()
    )
    db.refresh(run)
    first_ts = run.callback_received_at
    second = ingest_callback(
        db, run_id=run.id, claims=claims, payload=_payload(cost=2.0), now=_naive_now()
    )
    assert first.outcome == "late"
    assert second.outcome == "duplicate"
    db.refresh(run)
    assert run.callback_received_at == first_ts
    assert run.callback_payload["jti"] == "late-dup"
    assert run.status == DelegatedRunStatus.EXPIRED


def test_second_late_callback_different_jti_is_conflict(db) -> None:
    """GAP-4: a different-jti late delivery on a terminal run conflicts and
    never overwrites the first evidence."""
    project, agent, task = _seed(db)
    run = _run(
        db,
        project_id=project.id,
        task_id=task.id,
        agent_id=agent.id,
        status=DelegatedRunStatus.EXPIRED,
    )
    first = ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="late-1"),
        payload=_payload(status="succeeded"),
        now=_naive_now(),
    )
    db.refresh(run)
    first_ts = run.callback_received_at
    second = ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="late-2"),
        payload=_payload(status="failed", error="contradiction"),
        now=_naive_now(),
    )
    assert first.outcome == "late"
    assert second.outcome == "conflict"
    db.refresh(run)
    assert run.callback_payload["jti"] == "late-1"
    assert run.callback_payload["status"] == "succeeded"
    assert run.callback_received_at == first_ts
    assert run.status == DelegatedRunStatus.EXPIRED


def test_late_callback_never_accrues_budget_or_terminalizes(db) -> None:
    """GAP-4: even a cost-bearing terminal SUCCEEDED run stays exactly as it
    is -- the callback stores its payload as evidence only and never accrues."""
    project, agent, task = _seed(db, budget_limit=100.0)
    run = _run(
        db,
        project_id=project.id,
        task_id=task.id,
        agent_id=agent.id,
        status=DelegatedRunStatus.SUCCEEDED,
        cost=7.0,
    )
    result = ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="late-money"),
        payload=_payload(status="succeeded", cost=42.0, usage={"tokens": 11}),
        now=_naive_now(),
    )
    assert result.outcome == "late"
    db.refresh(run)
    assert run.status == DelegatedRunStatus.SUCCEEDED
    assert run.cost == 7.0  # NOT the pushed 42.0
    assert run.usage is None
    assert run.callback_payload["cost"] == 42.0  # stored as evidence only
    db.refresh(project)
    assert project.budget_used == 0.0  # never a budget writer, even late


def test_replay_of_preterminal_evidence_after_completion_is_duplicate(db) -> None:
    """GAP-4 semantic change, pinned: evidence staged BEFORE terminalization,
    then the run completes, then the same delivery replays -- it is now
    classified duplicate (existing-evidence semantics) instead of late."""
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    claims = _claims(run_id=run.id, agent_id=agent.id, jti="pre-1")
    assert (
        ingest_callback(
            db,
            run_id=run.id,
            claims=claims,
            payload=_payload(cost=1.0),
            now=_naive_now(),
        ).outcome
        == "received"
    )
    acquire_run_lease(db, run_id=run.id, owner="w1")
    complete_run(db, run_id=run.id, owner="w1", status=DelegatedRunStatus.SUCCEEDED)
    replay = ingest_callback(
        db, run_id=run.id, claims=claims, payload=_payload(cost=1.0), now=_naive_now()
    )
    assert replay.outcome == "duplicate"
    db.refresh(run)
    assert run.status == DelegatedRunStatus.SUCCEEDED
    assert run.callback_payload["jti"] == "pre-1"


def test_http_late_callback_persists_evidence(client, db) -> None:
    """GAP-4 over HTTP: a late delivery is acknowledged with the uniform body
    AND its evidence is persisted; the run is not reopened."""
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    acquire_run_lease(db, run_id=run.id, owner="w1")
    complete_run(db, run_id=run.id, owner="w1", status=DelegatedRunStatus.SUCCEEDED)
    resp = client.post(
        f"/runs/{run.id}/callback",
        json={"status": "succeeded", "cost": 3.0},
        headers={
            CALLBACK_TOKEN_HEADER: _token(
                run_id=run.id, agent_id=agent.id, jti="http-late"
            )
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"received": True}
    db.refresh(run)
    assert run.status == DelegatedRunStatus.SUCCEEDED
    assert run.callback_received_at is not None
    assert run.callback_payload["jti"] == "http-late"


def test_callback_after_lease_expiry_is_staged_but_not_terminal(db) -> None:
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    acquire_run_lease(db, run_id=run.id, owner="w1")
    db.refresh(run)
    run.lease_expires_at = _naive_now() - timedelta(seconds=1)
    db.add(run)
    db.commit()
    result = ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="stale-lease"),
        payload=_payload(status="succeeded"),
        now=_naive_now(),
    )
    assert result.outcome == "received"
    db.refresh(run)
    assert run.status == DelegatedRunStatus.SUBMITTED


# --- HTTP contract ---------------------------------------------------------


def test_http_accepts_authenticated_callback(client, db) -> None:
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    resp = client.post(
        f"/runs/{run.id}/callback",
        json={"status": "succeeded", "cost": 1.0},
        headers={CALLBACK_TOKEN_HEADER: _token(run_id=run.id, agent_id=agent.id)},
    )
    assert resp.status_code == 200
    db.refresh(run)
    assert run.callback_received_at is not None


def test_http_unknown_run_acknowledged_without_existence_leak(client, db) -> None:
    _seed(db)
    token = _token(run_id="run-does-not-exist", agent_id="a1")
    resp = client.post(
        "/runs/run-does-not-exist/callback",
        json={"status": "succeeded"},
        headers={CALLBACK_TOKEN_HEADER: token},
    )
    assert resp.status_code == 200
    assert resp.json() == {"received": True}


def test_http_response_body_is_identical_for_every_outcome(client, db) -> None:
    """No 404, no 409, and no outcome field: nothing is observable externally."""
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    token = _token(run_id=run.id, agent_id=agent.id, jti="same")
    body = {"status": "succeeded"}
    first = client.post(
        f"/runs/{run.id}/callback", json=body, headers={CALLBACK_TOKEN_HEADER: token}
    )
    second = client.post(
        f"/runs/{run.id}/callback", json=body, headers={CALLBACK_TOKEN_HEADER: token}
    )
    terminal = client.post(
        "/runs/nope/callback",
        json=body,
        headers={CALLBACK_TOKEN_HEADER: _token(run_id="nope", agent_id="a1")},
    )
    assert first.json() == second.json() == terminal.json() == {"received": True}
    assert first.status_code == second.status_code == terminal.status_code == 200


def test_http_rejects_unauthenticated_and_oversized_and_malformed(client, db) -> None:
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    url = f"/runs/{run.id}/callback"

    assert client.post(url, json={"status": "succeeded"}).status_code == 401
    assert (
        client.post(
            url,
            json={"status": "succeeded"},
            headers={CALLBACK_TOKEN_HEADER: "garbage"},
        ).status_code
        == 401
    )

    good = {CALLBACK_TOKEN_HEADER: _token(run_id=run.id, agent_id=agent.id)}
    assert client.post(url, json={"status": "bogus"}, headers=good).status_code == 422
    huge = client.post(
        url,
        json={"status": "succeeded", "blob": "x" * (CALLBACK_MAX_BODY_BYTES + 10)},
        headers=good,
    )
    assert huge.status_code == 413


def test_http_never_reveals_token_in_errors(client, db) -> None:
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    token = _token(run_id=run.id, agent_id=agent.id)
    resp = client.post(
        f"/runs/{run.id}/callback",
        json={"status": "nope"},
        headers={CALLBACK_TOKEN_HEADER: token},
    )
    assert token not in resp.text


# --- Security / provenance -------------------------------------------------


def test_token_never_enters_payload_or_audit(client, db) -> None:
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    token = _token(run_id=run.id, agent_id=agent.id, jti="jti-secret")
    client.post(
        f"/runs/{run.id}/callback",
        json={"status": "succeeded"},
        headers={CALLBACK_TOKEN_HEADER: token},
    )
    db.refresh(run)
    assert token not in str(run.callback_payload)
    audits = db.exec(select(AuditLog).where(AuditLog.resource_id == run.id)).all()
    assert audits, "callback must be auditable"
    for entry in audits:
        assert token not in str(entry.before_snapshot)
        assert token not in str(entry.after_snapshot)


def test_audit_records_distinct_outcomes(db) -> None:
    project, agent, task = _seed(db)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="a1"),
        payload=_payload(status="succeeded"),
        now=_naive_now(),
    )
    ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="a1"),
        payload=_payload(status="succeeded"),
        now=_naive_now(),
    )
    ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="a2"),
        payload=_payload(status="failed"),
        now=_naive_now(),
    )
    actions = {
        entry.action
        for entry in db.exec(select(AuditLog).where(AuditLog.resource_id == run.id))
    }
    assert "delegation.callback_received" in actions
    assert "delegation.callback_duplicate" in actions
    assert "delegation.callback_conflict" in actions


# --- Budget ----------------------------------------------------------------


def test_callback_alone_never_writes_budget(db) -> None:
    project, agent, task = _seed(db, budget_limit=100.0)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="b1"),
        payload=_payload(status="succeeded", cost=42.0),
        now=_naive_now(),
    )
    db.refresh(project)
    assert project.budget_used == 0.0


def test_callback_plus_completion_path_accrues_exactly_once(db) -> None:
    project, agent, task = _seed(db, budget_limit=100.0)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    ingest_callback(
        db,
        run_id=run.id,
        claims=_claims(run_id=run.id, agent_id=agent.id, jti="b2"),
        payload=_payload(status="failed", error="paid failure", cost=3.0),
        now=_naive_now(),
    )
    # The lease owner still drives completion (and applies the evidence).
    acquire_run_lease(db, run_id=run.id, owner="w1")
    db.refresh(run)
    run.cost = 3.0
    db.add(run)
    db.commit()
    assert complete_run(db, run_id=run.id, owner="w1", status=DelegatedRunStatus.FAILED)
    db.refresh(project)
    assert project.budget_used == pytest.approx(3.0)
    # A second terminalization attempt (retry / race) must not double-charge.
    complete_run(db, run_id=run.id, owner="w1", status=DelegatedRunStatus.FAILED)
    db.refresh(project)
    assert project.budget_used == pytest.approx(3.0)


def test_duplicate_callbacks_plus_completion_accrue_once(db) -> None:
    project, agent, task = _seed(db, budget_limit=100.0)
    run = _run(db, project_id=project.id, task_id=task.id, agent_id=agent.id)
    claims = _claims(run_id=run.id, agent_id=agent.id, jti="b3")
    for _ in range(3):
        ingest_callback(
            db,
            run_id=run.id,
            claims=claims,
            payload=_payload(status="succeeded", cost=2.0),
            now=_naive_now(),
        )
    acquire_run_lease(db, run_id=run.id, owner="w1")
    db.refresh(run)
    run.cost = 2.0
    db.add(run)
    db.commit()
    complete_run(db, run_id=run.id, owner="w1", status=DelegatedRunStatus.SUCCEEDED)
    db.refresh(project)
    assert project.budget_used == pytest.approx(2.0)


# --- URL shape -------------------------------------------------------------


def test_callback_url_contains_no_token(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_CALLBACK_BASE_URL", "https://aios.example.com")
    url = build_callback_url("run-1")
    assert url == "https://aios.example.com/runs/run-1/callback"
    token = _token(run_id="run-1", agent_id="a1")
    assert token not in url


def test_token_expiry_is_time_bounded() -> None:
    token = _token(run_id="r1", agent_id="a1", ttl=60.0)
    claims = verify_callback_token(token)
    assert claims.exp <= int(time.time()) + 61
