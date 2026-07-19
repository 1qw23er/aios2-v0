"""Agent Interoperability Gateway hardening (#104) — security & cost tests.

Covers the four required guardrails without touching any real external system:

* Secret leakage prevention — ``secret_ref`` stays an opaque handle on
  ``DelegatedRun``; the resolved secret is never written to any DB payload, and
  the least-privilege projection never carries a credential value.
* Audit redaction — tokens / keys / ``Authorization`` header values are
  redacted before an ``AuditLog`` row is persisted.
* Context projection — an external agent receives ONLY the task-specific
  allowlist (objective / instructions / acceptance / dependency outputs), never
  internal knowledge-base context (approved_facts / decisions / policies).
* Budget enforcement — a project over its ``budget_limit`` is HARD-blocked
  before any remote call, failing safely with an explicit reason.
* Trust-level restriction — an ``experimental`` agent is blocked from delegation;
  ``verified_external`` / ``internal`` are allowed.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from aios.adapters.hermes_remote import (
    RemoteApiAdapter,
    _FakeHermesServer,
    make_fake_hermes_agent,
)
from aios.audit import append_audit, redact_secrets
from aios.db import get_database_url, get_engine
from aios.delegation import (
    BudgetExceededError,
    DelegatedExecutionError,
    assert_trust_delegable,
    check_budget,
    project_external_context,
)
from aios.models import (
    AdapterType,
    Agent,
    AgentTrustLevel,
    DelegatedRun,
    Project,
    Task,
    TaskContext,
    TaskStatus,
)

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "deliverable": {"type": "string"}},
    "required": ["summary", "deliverable"],
}


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    # Scope AIOS_DATABASE_URL to this fixture only (restored on teardown) so it
    # cannot leak into sibling test files and make alembic/env.py migrate the
    # wrong database.
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'hardening.db'}")
    from aios.db import run_migrations

    run_migrations(get_database_url())
    eng = get_engine(get_database_url())
    s = Session(eng)
    yield s
    s.close()


def _seed(session: Session, *, budget_limit: float = 0.0, budget_used: float = 0.0):
    p = Project(name="t", objective="o", budget_limit=budget_limit, budget_used=budget_used)
    session.add(p)
    session.commit()
    session.refresh(p)
    task = Task(
        project_id=p.id,
        title="T",
        description="instructions here",
        status=TaskStatus.READY,
        output_schema=SCHEMA,
        estimated_cost=0.05,
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return p, task


def _full_context(task: Task, project: Project) -> TaskContext:
    """A realistic TaskContext that DOES contain internal knowledge + a secret."""
    return TaskContext(
        task_id=task.id,
        project_id=project.id,
        assigned_agent_id=None,
        objective="o",
        instructions="do it",
        acceptance_criteria=["x"],
        project_context={"internal": "notes"},
        dependency_outputs=[{"type": "json", "data": {"k": "v"}}],
        approved_facts=[{"statement": "internal fact"}],  # must NOT leak
        relevant_decisions=[{"title": "internal decision"}],  # must NOT leak
        applicable_policies=[{"name": "internal policy"}],  # must NOT leak
        agent_profile={"secret": "super-secret-value"},  # must NOT leak
        source_references=[],
        context_hash="h",
    )


# ---------------------------------------------------------------------------
# 1. Secret leakage prevention
# ---------------------------------------------------------------------------
def test_secret_ref_stays_opaque_handle_on_run(session: Session) -> None:
    p, task = _seed(session)
    srv = _FakeHermesServer()
    ep = srv.start()
    hermes = make_fake_hermes_agent(ep)
    session.add(hermes)
    session.commit()
    session.refresh(hermes)

    adapter = RemoteApiAdapter(agent=hermes, resolve_secret=lambda ref: "REAL-SECRET-VALUE")
    adapter.run(
        task_id=task.id,
        task_context=_full_context(task, p),
        output_schema=SCHEMA,
        idempotency_key="idek1",
    )
    run = session.exec(select(DelegatedRun).where(DelegatedRun.task_id == task.id)).one()
    # secret_ref is the opaque handle, never the resolved value.
    assert run.secret_ref == hermes.secret_ref
    assert run.secret_ref == "secret://fake-hermes"
    assert "REAL-SECRET-VALUE" not in (run.secret_ref or "")
    srv.stop()


def test_projection_never_carries_secret_value() -> None:
    proj = {
        "objective": "o",
        "instructions": "do it",
        "secret": "top-secret",
        "api_key": "sk-1234567890abcdef",
        "Authorization": "Bearer abcdef-token",
    }
    out = redact_secrets(proj)
    assert "top-secret" not in str(out)
    assert "sk-1234567890abcdef" not in str(out)
    assert "abcdef-token" not in str(out)
    assert out["objective"] == "o"
    assert out["instructions"] == "do it"


# ---------------------------------------------------------------------------
# 2. Audit redaction
# ---------------------------------------------------------------------------
def test_audit_redacts_token_and_authorization_header(session: Session) -> None:
    append_audit(
        session,
        actor="gateway",
        action="test.redaction",
        resource_type="delegated_run",
        resource_id="r1",
        project_id=None,
        task_id=None,
        before={"api_key": "sk-1234567890abcdef", "note": "ok"},
        after={
            "headers": {"Authorization": "Bearer secret-token-xyz", "X-Other": "keep"},
            "password": "hunter2",
        },
        idempotency_key="audit:redact:1",
    )
    session.commit()
    row = session.exec(select(__import__("aios.audit", fromlist=["AuditLog"]).AuditLog)).one()
    blob = str(row.before_snapshot) + str(row.after_snapshot)
    assert "sk-1234567890abcdef" not in blob
    assert "secret-token-xyz" not in blob
    assert "hunter2" not in blob
    assert "Bearer" not in blob
    assert "[REDACTED]" in blob
    assert "keep" in blob  # non-secret header preserved


# ---------------------------------------------------------------------------
# 3. Context projection (least privilege)
# ---------------------------------------------------------------------------
def test_external_context_projection_is_allowlisted(session: Session) -> None:
    p, task = _seed(session)
    ctx = _full_context(task, p)
    out = project_external_context(ctx)
    # Task-specific fields present.
    assert out["objective"] == "o"
    assert out["instructions"] == "do it"
    # Internal knowledge-base context stripped.
    assert "approved_facts" not in out
    assert "relevant_decisions" not in out
    assert "applicable_policies" not in out
    assert "project_context" not in out
    assert "agent_profile" not in out
    assert "source_references" not in out
    # Any credential value redacted.
    assert "super-secret-value" not in str(out)


# ---------------------------------------------------------------------------
# 4. Budget enforcement
# ---------------------------------------------------------------------------
def test_budget_blocked_before_remote_execution(session: Session) -> None:
    # budget_limit=0.10, used=0.08, task estimated_cost=0.05 -> projected 0.13 > 0.10
    p, task = _seed(session, budget_limit=0.10, budget_used=0.08)
    # Assign a trust-clear, budget-clear agent; the budget gate must trip first.
    hermes = make_fake_hermes_agent("http://unused")
    hermes.trust_level = AgentTrustLevel.VERIFIED_EXTERNAL
    session.add(hermes)
    session.commit()
    session.refresh(hermes)

    adapter = RemoteApiAdapter(agent=hermes, resolve_secret=lambda ref: "k")
    with pytest.raises(BudgetExceededError) as exc:
        adapter.run(
            task_id=task.id,
            task_context=_full_context(task, p),
            output_schema=SCHEMA,
            idempotency_key="idek-budget",
        )
    assert "budget exceeded" in str(exc.value).lower()
    # No DelegatedRun was created (blocked before submit).
    runs = session.exec(select(DelegatedRun).where(DelegatedRun.task_id == task.id)).all()
    assert runs == []


def test_budget_unenforced_when_limit_zero(session: Session) -> None:
    # budget_limit=0.0 => no enforcement; should pass the gate.
    p, task = _seed(session, budget_limit=0.0, budget_used=999.0)
    check_budget(session, p, 100.0)  # must NOT raise


def test_budget_accrues_after_success(session: Session) -> None:
    p, task = _seed(session, budget_limit=10.0, budget_used=0.0)
    srv = _FakeHermesServer()
    ep = srv.start()
    hermes = make_fake_hermes_agent(ep)
    session.add(hermes)
    session.commit()
    session.refresh(hermes)
    adapter = RemoteApiAdapter(agent=hermes, resolve_secret=lambda ref: "k")
    # The fake server returns cost=0.01; budget_used should increase by that.
    adapter.run(
        task_id=task.id,
        task_context=_full_context(task, p),
        output_schema=SCHEMA,
        idempotency_key="idek-accrue",
    )
    session.refresh(p)
    assert p.budget_used == pytest.approx(0.01, abs=1e-6)
    srv.stop()


# ---------------------------------------------------------------------------
# 5. Trust-level restriction
# ---------------------------------------------------------------------------
def test_experimental_agent_blocked_from_delegation(session: Session) -> None:
    p, task = _seed(session)
    hermes = make_fake_hermes_agent("http://unused")
    hermes.trust_level = AgentTrustLevel.EXPERIMENTAL
    session.add(hermes)
    session.commit()
    session.refresh(hermes)

    adapter = RemoteApiAdapter(agent=hermes, resolve_secret=lambda ref: "k")
    with pytest.raises(DelegatedExecutionError) as exc:
        adapter.run(
            task_id=task.id,
            task_context=_full_context(task, p),
            output_schema=SCHEMA,
            idempotency_key="idek-exp",
        )
    assert "trust_level" in str(exc.value).lower()
    # No DelegatedRun created — the gate fired before any remote call.
    runs = session.exec(select(DelegatedRun).where(DelegatedRun.task_id == task.id)).all()
    assert runs == []


def test_verified_external_agent_allowed(session: Session) -> None:
    p, task = _seed(session)
    srv = _FakeHermesServer()
    ep = srv.start()
    hermes = make_fake_hermes_agent(ep)
    hermes.trust_level = AgentTrustLevel.VERIFIED_EXTERNAL
    session.add(hermes)
    session.commit()
    session.refresh(hermes)

    adapter = RemoteApiAdapter(agent=hermes, resolve_secret=lambda ref: "k")
    res = adapter.run(
        task_id=task.id,
        task_context=_full_context(task, p),
        output_schema=SCHEMA,
        idempotency_key="idek-ok",
    )
    assert res.artifacts[0]["data"]["deliverable"] == "structured output from external agent"
    srv.stop()


def test_assert_trust_delegable_rejects_experimental() -> None:
    a = Agent(name="x", role="r", adapter_type=AdapterType.API)
    a.trust_level = AgentTrustLevel.EXPERIMENTAL
    with pytest.raises(DelegatedExecutionError):
        assert_trust_delegable(a)
    a.trust_level = AgentTrustLevel.INTERNAL
    assert_trust_delegable(a)  # no raise
