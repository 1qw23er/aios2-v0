"""Execution Run Lifecycle & Recovery P0 — correctness tests.

Covers the four guarantees the P0 is about, using the EXISTING ``DelegatedRun``:

* **Lease** — durable, fenced, atomic ownership (acquire / renew / release).
* **Fencing** — a stale owner can never mutate or terminalize a run it lost.
* **Recovery** — stranded runs are reclaimed, exactly once, fail-closed.
* **At-most-once** — recovery never creates a second logical run and never
  blind-retries a remote execution whose state is unknown.

Recovery is deliberately NOT resume: no test here asserts a remote execution is
resumed, only that AIOS stops leaving zombie records behind.

Every state change under test is a single conditional UPDATE; these tests assert
the ``rowcount`` outcome of that compare-and-set, which is what makes the
guarantee hold across processes and restarts.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.actor import ActorContext
from aios.audit import AuditEvent, AuditLog
from aios.db import get_database_url, get_engine
from aios.execution_run import (
    RECOVERABLE_RUN_STATUSES,
    RUN_LEASE_TTL_SECONDS,
    STRANDED_RECOVERY_ERROR,
    acquire_run_lease,
    complete_run,
    find_stranded_runs,
    lease_is_active,
    new_run_lease_owner,
    recover_stranded_runs,
    release_run_lease,
    renew_run_lease,
    update_run_if_owned,
)
from aios.models import (
    AdapterType,
    Agent,
    DelegatedRun,
    DelegatedRunStatus,
    DelegationMode,
    Project,
    Task,
    TaskStatus,
    now_utc,
)

# --- helpers ---------------------------------------------------------------


def _naive_now():
    """Naive UTC "now" (SQLite round-trips naive; see ``execution_run``)."""
    return now_utc().replace(tzinfo=None)


def _seed_identity(session: Session):
    """Project + Task + Agent -- the minimum FK skeleton for a DelegatedRun."""
    project = Project(name="p", objective="o", budget_limit=0.0)
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


def _make_run(
    session: Session,
    *,
    status: DelegatedRunStatus = DelegatedRunStatus.SUBMITTED,
    age_seconds: float = 3600.0,
    lease_owner: str | None = None,
    lease_expires_at=None,
    remote_run_id: str | None = None,
    attempt: int = 1,
) -> DelegatedRun:
    """Insert a run directly (no adapter), backdated by ``age_seconds``."""
    project, agent, task = _seed_identity(session)
    run = DelegatedRun(
        project_id=project.id,
        task_id=task.id,
        agent_id=agent.id,
        delegation_mode=DelegationMode.WORKSTATION,
        status=status,
        idempotency_key=f"idem-{uuid4().hex[:12]}",
        attempt=attempt,
        remote_run_id=remote_run_id,
        submitted_at=_naive_now() - timedelta(seconds=age_seconds),
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    """Per-test migrated SQLite database, URL scoped to this test only."""
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'run_lifecycle.db'}")
    from aios.db import run_migrations

    run_migrations(get_database_url())
    eng = get_engine(get_database_url())
    s = Session(eng)
    yield s
    s.close()


@pytest.fixture
def owner_app(tmp_path, monkeypatch):
    """App with a trusted owner override (startup recovery still runs)."""

    def _trusted_owner() -> ActorContext:
        return ActorContext(kind="owner", owner_id="owner")

    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'run_api.db'}")
    from aios.api.app import create_app
    from aios.api.security import authenticate_owner

    app = create_app()
    app.dependency_overrides[authenticate_owner] = _trusted_owner
    yield app
    app.dependency_overrides.pop(authenticate_owner, None)


# --- Lease -----------------------------------------------------------------


def test_acquire_lease_on_unleased_run(session: Session) -> None:
    run = _make_run(session)
    assert run.lease_owner is None
    assert acquire_run_lease(session, run_id=run.id, owner="w1") is True
    session.refresh(run)
    assert run.lease_owner == "w1"
    assert run.lease_expires_at is not None
    assert lease_is_active(run) is True


def test_acquire_lease_on_expired_lease(session: Session) -> None:
    run = _make_run(
        session,
        lease_owner="w-old",
        lease_expires_at=_naive_now() - timedelta(seconds=1),
    )
    assert lease_is_active(run) is False
    assert acquire_run_lease(session, run_id=run.id, owner="w-new") is True
    session.refresh(run)
    assert run.lease_owner == "w-new"


def test_acquire_lease_fails_when_lease_is_active(session: Session) -> None:
    run = _make_run(
        session,
        lease_owner="w1",
        lease_expires_at=_naive_now() + timedelta(seconds=RUN_LEASE_TTL_SECONDS),
    )
    # Not even the current owner may re-acquire; it must renew instead.
    assert acquire_run_lease(session, run_id=run.id, owner="w1") is False
    assert acquire_run_lease(session, run_id=run.id, owner="w2") is False
    session.refresh(run)
    assert run.lease_owner == "w1"


def test_acquire_lease_fails_for_unknown_run(session: Session) -> None:
    assert acquire_run_lease(session, run_id="run_missing", owner="w1") is False


def test_renew_lease_by_owner(session: Session) -> None:
    run = _make_run(
        session,
        lease_owner="w1",
        lease_expires_at=_naive_now() + timedelta(seconds=5),
    )
    assert renew_run_lease(session, run_id=run.id, owner="w1", ttl_seconds=120) is True
    session.refresh(run)
    assert run.lease_owner == "w1"
    assert lease_is_active(run) is True


def test_renew_lease_by_non_owner_fails(session: Session) -> None:
    run = _make_run(
        session,
        lease_owner="w1",
        lease_expires_at=_naive_now() + timedelta(seconds=60),
    )
    assert renew_run_lease(session, run_id=run.id, owner="w2") is False


def test_renew_lease_after_expiry_fails(session: Session) -> None:
    """A lease that already lapsed cannot be renewed -- it must be re-acquired."""
    run = _make_run(
        session,
        lease_owner="w1",
        lease_expires_at=_naive_now() - timedelta(seconds=1),
    )
    assert renew_run_lease(session, run_id=run.id, owner="w1") is False


def test_release_lease_by_owner(session: Session) -> None:
    run = _make_run(
        session,
        lease_owner="w1",
        lease_expires_at=_naive_now() + timedelta(seconds=60),
    )
    assert release_run_lease(session, run_id=run.id, owner="w1") is True
    session.refresh(run)
    assert run.lease_owner is None
    assert run.lease_expires_at is None
    assert lease_is_active(run) is False


def test_release_lease_by_non_owner_is_noop(session: Session) -> None:
    run = _make_run(
        session,
        lease_owner="w1",
        lease_expires_at=_naive_now() + timedelta(seconds=60),
    )
    assert release_run_lease(session, run_id=run.id, owner="w2") is False
    session.refresh(run)
    assert run.lease_owner == "w1"


# --- Fencing ---------------------------------------------------------------


def test_stale_owner_cannot_complete_after_losing_lease(session: Session) -> None:
    """Contract §IX: fencing. Once the lease is gone the old owner is inert."""
    run = _make_run(
        session,
        lease_owner="w-old",
        lease_expires_at=_naive_now() - timedelta(seconds=1),
    )
    # A new worker took over.
    assert acquire_run_lease(session, run_id=run.id, owner="w-new") is True

    assert (
        complete_run(
            session,
            run_id=run.id,
            owner="w-old",
            status=DelegatedRunStatus.SUCCEEDED,
        )
        is False
    )
    assert (
        update_run_if_owned(
            session, run_id=run.id, owner="w-old", values={"remote_status": "hijack"}
        )
        is False
    )
    session.refresh(run)
    assert run.status == DelegatedRunStatus.SUBMITTED
    assert run.remote_status != "hijack"

    # The legitimate owner still can.
    assert (
        complete_run(
            session,
            run_id=run.id,
            owner="w-new",
            status=DelegatedRunStatus.SUCCEEDED,
        )
        is True
    )
    session.refresh(run)
    assert run.status == DelegatedRunStatus.SUCCEEDED


def test_complete_run_cannot_overwrite_a_settled_status(session: Session) -> None:
    """SUCCEEDED / FAILED / CANCELLED are immutable, even for the lease holder."""
    run = _make_run(
        session,
        lease_owner="w1",
        lease_expires_at=_naive_now() + timedelta(seconds=60),
    )
    assert (
        complete_run(
            session,
            run_id=run.id,
            owner="w1",
            status=DelegatedRunStatus.SUCCEEDED,
        )
        is True
    )
    # A later owner (lease re-acquired after expiry) must not rewrite history.
    run.lease_expires_at = _naive_now() + timedelta(seconds=60)
    session.add(run)
    session.commit()
    assert (
        complete_run(
            session, run_id=run.id, owner="w1", status=DelegatedRunStatus.FAILED
        )
        is False
    )
    session.refresh(run)
    assert run.status == DelegatedRunStatus.SUCCEEDED


def test_expired_run_may_be_relabelled_failed_by_its_owner(session: Session) -> None:
    """Preserved behaviour: a timed-out attempt is relabelled FAILED, not lost."""
    run = _make_run(
        session,
        status=DelegatedRunStatus.EXPIRED,
        lease_owner="w1",
        lease_expires_at=_naive_now() + timedelta(seconds=60),
    )
    assert (
        complete_run(
            session, run_id=run.id, owner="w1", status=DelegatedRunStatus.FAILED,
            error="delegation timeout",
        )
        is True
    )
    session.refresh(run)
    assert run.status == DelegatedRunStatus.FAILED
    assert run.error == "delegation timeout"


# --- Recovery --------------------------------------------------------------


def test_stranded_submitted_run_is_recovered(session: Session) -> None:
    run = _make_run(session, status=DelegatedRunStatus.SUBMITTED)
    report = recover_stranded_runs(session, owner="recovery-1")
    assert report.scanned == 1
    assert report.recovered_count == 1
    session.refresh(run)
    assert run.status == DelegatedRunStatus.EXPIRED
    assert run.error == STRANDED_RECOVERY_ERROR
    assert run.finished_at is not None
    assert run.lease_owner is None


def test_stranded_running_run_is_recovered(session: Session) -> None:
    run = _make_run(session, status=DelegatedRunStatus.RUNNING)
    report = recover_stranded_runs(session, owner="recovery-1")
    assert report.recovered_count == 1
    session.refresh(run)
    assert run.status == DelegatedRunStatus.EXPIRED


def test_run_with_active_lease_is_not_recovered(session: Session) -> None:
    run = _make_run(
        session,
        lease_owner="w1",
        lease_expires_at=_naive_now() + timedelta(seconds=RUN_LEASE_TTL_SECONDS),
    )
    report = recover_stranded_runs(session, owner="recovery-1")
    assert report.scanned == 0
    assert report.recovered_count == 0
    session.refresh(run)
    assert run.status == DelegatedRunStatus.SUBMITTED
    assert run.lease_owner == "w1"


def test_fresh_run_inside_grace_period_is_not_recovered(session: Session) -> None:
    """The grace period stops recovery reclaiming a run a live process owns."""
    run = _make_run(session, age_seconds=1.0)
    assert find_stranded_runs(session) == []
    assert recover_stranded_runs(session).recovered_count == 0
    session.refresh(run)
    assert run.status == DelegatedRunStatus.SUBMITTED


def test_terminal_runs_are_never_recovery_candidates(session: Session) -> None:
    for status in (
        DelegatedRunStatus.SUCCEEDED,
        DelegatedRunStatus.FAILED,
        DelegatedRunStatus.CANCELLED,
        DelegatedRunStatus.EXPIRED,
    ):
        _make_run(session, status=status)
    assert find_stranded_runs(session, grace_seconds=0) == []


def test_recovery_is_idempotent(session: Session) -> None:
    run = _make_run(session)
    first = recover_stranded_runs(session, owner="recovery-1")
    second = recover_stranded_runs(session, owner="recovery-2")
    assert first.recovered_count == 1
    assert second.scanned == 0
    assert second.recovered_count == 0
    session.refresh(run)
    assert run.status == DelegatedRunStatus.EXPIRED


def test_recovery_claim_is_mutually_exclusive(session: Session) -> None:
    """Two claimants over the SAME frozen candidate: exactly one wins."""
    from aios.execution_run import _claim_and_expire

    run = _make_run(session)
    candidates = find_stranded_runs(session, grace_seconds=0)
    assert len(candidates) == 1
    assert _claim_and_expire(session, run_id=run.id, owner="w1") is True
    # w2 races on the same (now stale) candidate list -- the CAS must reject it.
    assert _claim_and_expire(session, run_id=run.id, owner="w2") is False
    session.refresh(run)
    assert run.status == DelegatedRunStatus.EXPIRED


def test_concurrent_recovery_only_one_claimant(session: Session) -> None:
    """Two recovery workers (separate sessions) reclaim one run exactly once."""
    run = _make_run(session)
    engine = get_engine(get_database_url())
    with Session(engine) as s1, Session(engine) as s2:
        # Both workers observe the stranded run before either reclaims it.
        assert len(find_stranded_runs(s1, grace_seconds=0)) == 1
        assert len(find_stranded_runs(s2, grace_seconds=0)) == 1

        first = recover_stranded_runs(s1, owner="recovery-A", grace_seconds=0)
        second = recover_stranded_runs(s2, owner="recovery-B", grace_seconds=0)

    assert first.recovered_count == 1
    assert second.recovered_count == 0
    session.refresh(run)
    assert run.status == DelegatedRunStatus.EXPIRED
    # Only the winner left an audit trail.
    audits = session.exec(
        select(AuditLog).where(AuditLog.action == AuditEvent.DELEGATION_RUN_RECOVERED)
    ).all()
    assert len(audits) == 1
    assert audits[0].after_snapshot["recovery_owner"] == "recovery-A"
    assert audits[0].before_snapshot["status"] == DelegatedRunStatus.SUBMITTED.value


def test_recovery_writes_audit(session: Session) -> None:
    run = _make_run(session, remote_run_id="remote-123")
    previous_owner = "w-gone"
    run.lease_owner = previous_owner
    run.lease_expires_at = _naive_now() - timedelta(seconds=10)
    session.add(run)
    session.commit()

    recover_stranded_runs(session, owner="recovery-9")

    audits = session.exec(
        select(AuditLog).where(AuditLog.action == AuditEvent.DELEGATION_RUN_RECOVERED)
    ).all()
    assert len(audits) == 1
    audit = audits[0]
    assert audit.resource_type == "delegated_run"
    assert audit.resource_id == run.id
    assert audit.before_snapshot["status"] == DelegatedRunStatus.SUBMITTED.value
    assert audit.before_snapshot["lease_owner"] == previous_owner
    assert audit.after_snapshot["status"] == DelegatedRunStatus.EXPIRED.value
    assert audit.after_snapshot["recovery_owner"] == "recovery-9"
    assert audit.before_snapshot["recovery_reason"] == STRANDED_RECOVERY_ERROR
    # No credential material may reach the audit trail.
    blob = f"{audit.before_snapshot}{audit.after_snapshot}"
    for forbidden in ("secret_ref", "context_ref", "callback_url", "token"):
        assert forbidden not in blob


def test_recovery_reaches_terminal_state(session: Session) -> None:
    run = _make_run(session)
    recover_stranded_runs(session, owner="recovery-1")
    session.refresh(run)
    assert run.status not in RECOVERABLE_RUN_STATUSES
    assert run.status == DelegatedRunStatus.EXPIRED
    # A terminal run is no longer a candidate, so nothing re-reclaims it.
    assert find_stranded_runs(session, grace_seconds=0) == []


# --- At-most-once / crash semantics ---------------------------------------


def test_unknown_remote_execution_is_fail_closed(session: Session) -> None:
    """A run with a remote_run_id is expired, NEVER blind-retried."""
    run = _make_run(session, remote_run_id="remote-abc")
    report = recover_stranded_runs(session, owner="recovery-1")
    assert report.recovered_count == 1
    session.refresh(run)
    assert run.status == DelegatedRunStatus.EXPIRED
    # No new run was submitted for the same logical execution.
    runs = session.exec(select(DelegatedRun)).all()
    assert len(runs) == 1
    assert runs[0].id == run.id
    assert runs[0].remote_run_id == "remote-abc"


def test_crash_before_remote_submit_creates_no_second_run(session: Session) -> None:
    """Process died after the row existed but before submit -> no re-submit."""
    run = _make_run(session, remote_run_id=None)
    recover_stranded_runs(session, owner="recovery-1")
    session.refresh(run)
    assert run.status == DelegatedRunStatus.EXPIRED
    assert run.remote_run_id is None
    assert len(session.exec(select(DelegatedRun)).all()) == 1


def test_duplicate_recovery_does_not_create_second_run(session: Session) -> None:
    run = _make_run(session)
    for _ in range(3):
        recover_stranded_runs(session, owner="recovery-1", grace_seconds=0)
    runs = session.exec(select(DelegatedRun)).all()
    assert len(runs) == 1
    assert runs[0].id == run.id
    assert runs[0].status == DelegatedRunStatus.EXPIRED


def test_recovered_run_keeps_its_idempotency_identity(session: Session) -> None:
    """Recovery must not mint a new idempotency key / attempt for a run."""
    run = _make_run(session, attempt=2)
    original_key = run.idempotency_key
    recover_stranded_runs(session, owner="recovery-1")
    session.refresh(run)
    assert run.idempotency_key == original_key
    assert run.attempt == 2


def test_recovery_respects_scan_limit(session: Session) -> None:
    for _ in range(3):
        _make_run(session)
    report = recover_stranded_runs(session, owner="recovery-1", limit=2)
    assert report.scanned == 2
    assert report.recovered_count == 2


# --- Run query surface -----------------------------------------------------


def test_run_query_surface_exposes_lease_and_lifecycle(owner_app) -> None:
    with TestClient(owner_app, follow_redirects=False) as client:
        # Seeded AFTER startup: the lifespan hook is what migrates this database.
        with Session(get_engine(get_database_url())) as s:
            # An active, leased run: startup recovery must not touch it.
            run = _make_run(
                s,
                lease_owner="w1",
                lease_expires_at=_naive_now() + timedelta(seconds=RUN_LEASE_TTL_SECONDS),
            )
            run_id, task_id = run.id, run.task_id

        resp = client.get(f"/runs/{run_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == run_id
        assert body["task_id"] == task_id
        assert body["status"] == DelegatedRunStatus.SUBMITTED.value
        assert body["attempt"] == 1
        assert body["lease"]["owner"] == "w1"
        assert body["lease"]["active"] is True
        assert body["submitted_at"] is not None
        # Secret boundary: credential-adjacent fields are never exposed.
        for forbidden in ("secret_ref", "context_ref", "callback_url"):
            assert forbidden not in body

        listed = client.get(f"/tasks/{task_id}/runs")
        assert listed.status_code == 200
        assert [item["run_id"] for item in listed.json()] == [run_id]

        missing = client.get("/runs/run_does_not_exist")
        assert missing.status_code == 404


def test_recover_endpoint_reclaims_and_reports(owner_app) -> None:
    with TestClient(owner_app, follow_redirects=False) as client:
        with Session(get_engine(get_database_url())) as s:
            run = _make_run(s)  # seeded AFTER startup recovery ran

        resp = client.post("/runs/recover")
        assert resp.status_code == 200
        body = resp.json()
        assert body["recovered_count"] == 1
        assert body["recovered"][0]["run_id"] == run.id
        assert body["recovered"][0]["resulting_status"] == DelegatedRunStatus.EXPIRED.value

        # Second call is a no-op (idempotent).
        assert client.post("/runs/recover").json()["recovered_count"] == 0

        with Session(get_engine(get_database_url())) as s:
            persisted = s.get(DelegatedRun, run.id)
            assert persisted.status == DelegatedRunStatus.EXPIRED


def test_run_surface_requires_owner_auth(tmp_path, monkeypatch) -> None:
    """The lifecycle surface is owner-only (no anonymous execution control)."""
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'run_auth.db'}")
    # Configure owner auth so the real dependency answers 401 (bad credentials)
    # rather than 503 (owner auth not configured at all).
    monkeypatch.setenv("AIOS_OWNER_ID", "owner")
    monkeypatch.setenv("AIOS_OWNER_API_KEY", "k" * 40)
    from aios.api.app import create_app

    app = create_app()  # real authenticate_owner -- no override
    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/runs/run_x").status_code == 401
        assert client.post("/runs/recover").status_code == 401


# --- Architecture invariants ----------------------------------------------


def test_no_new_execution_entity_was_introduced() -> None:
    """P0 adds lease columns to DelegatedRun -- nothing else."""
    import aios.models as models

    for forbidden in (
        "Runtime",
        "RuntimeRegistration",
        "Execution",
        "ExecutionRun",
        "Worker",
        "CapabilityRegistry",
    ):
        assert not hasattr(models, forbidden), f"{forbidden} must not be introduced"

    columns = {name for name in DelegatedRun.model_fields}
    for forbidden in ("runtime_id", "execution_id", "worker_id"):
        assert forbidden not in columns
    # The lease is the only addition.
    assert {"lease_owner", "lease_expires_at"} <= columns


def test_run_status_enum_reuses_existing_states() -> None:
    """No new lifecycle state was invented; EXPIRED is reused as the terminal."""
    values = {member.value for member in DelegatedRunStatus}
    assert values == {"submitted", "running", "succeeded", "failed", "cancelled", "expired"}


def test_lease_owner_is_an_opaque_token_not_a_domain_identity() -> None:
    """lease_owner must never be an agent / employee / runtime identity."""
    owner = new_run_lease_owner()
    assert owner.startswith("lease_")
    assert "agt" not in owner and "emp" not in owner
