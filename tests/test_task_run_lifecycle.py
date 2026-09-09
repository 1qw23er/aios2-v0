"""Execution Run Lifecycle & Recovery P0-B: durable lease on ``task``.

Closes the second half of the gap the ``DelegatedRun`` lease closed in P0-A:
``Task.status == RUNNING`` was a **dead end**. It is written in exactly one place
(``aios.execution.execute_task``) and cleared only by ``complete_task`` (DONE) or
``_mark_failed`` (FAILED). If the process dies in between, the row stays RUNNING
forever and the department execution rejects it with 409 forever. This slice
makes that permanent zombie retryable again via a fail-closed startup recovery
scan, using the EXISTING ``Task`` record.

Four guarantees, mirroring P0-A:

* **Claim (at-most-once)** -- ``claim_task_for_execution`` is a single conditional
  ``UPDATE ... WHERE status = 'ready'``. Exactly one caller flips READY ->
  RUNNING and takes the lease; every other caller gets ``rowcount == 0`` and is
  rejected with 409 instead of running the adapter a second time.
* **Release** -- the lease is given up by the owner once the task reaches a
  terminal state (success or any failure path), so no abandoned lease survives.
* **Recovery (fail-closed)** -- a startup scan reclaims RUNNING tasks whose lease
  is gone or expired and that are past a safety grace period, moving them to
  FAILED (terminal *and* retryable). It never resumes or retries an execution
  whose outcome is unknown.
* **At-most-once** -- recovery never creates a second logical task and never
  blind-retries an in-flight execution.

Every state change under test is a single conditional UPDATE; these tests assert
the ``rowcount`` outcome of that compare-and-set, which is what makes the
guarantee hold across processes and restarts.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.actor import ActorContext
from aios.audit import AuditEvent, AuditLog
from aios.db import get_database_url, get_engine
from aios.models import (
    Artifact,
    ArtifactType,
    Project,
    RoutingMode,
    Task,
    TaskStatus,
    now_utc,
)
from aios.task_run import (
    STRANDED_TASK_RECOVERY_REASON,
    claim_task_for_execution,
    find_stranded_tasks,
    new_task_lease_owner,
    recover_stranded_tasks,
    release_task_lease,
    task_lease_is_active,
)

# --- helpers ---------------------------------------------------------------


def _naive_now():
    """Naive UTC "now" (SQLite round-trips naive; see ``task_run``)."""
    return now_utc().replace(tzinfo=None)


def _seed_task(
    session: Session,
    *,
    status: TaskStatus = TaskStatus.RUNNING,
    lease_owner: str | None = None,
    lease_expires_at=None,
    age_seconds: float = 3600.0,
) -> tuple[Project, Task]:
    """Insert a Project + Task directly (no adapter), backdated by ``age_seconds``."""
    project = Project(name="p", objective="o", budget_limit=0.0)
    session.add(project)
    session.commit()
    session.refresh(project)

    task = Task(
        project_id=project.id,
        title="t",
        description="d",
        status=status,
        routing_mode=RoutingMode.FIXED,
        output_schema={"type": "object"},
        estimated_cost=0.0,
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        updated_at=_naive_now() - timedelta(seconds=age_seconds),
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return project, task


def _seed_artifact(session: Session, *, task_id: str, project_id: str) -> Artifact:
    art = Artifact(
        project_id=project_id,
        task_id=task_id,
        type=ArtifactType.JSON,
        uri="exec://x",
        checksum="c",
    )
    session.add(art)
    session.commit()
    session.refresh(art)
    return art


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    """Per-test migrated SQLite database, URL scoped to this test only."""
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'task_lifecycle.db'}")
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

    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'task_api.db'}")
    from aios.api.app import create_app
    from aios.api.security import authenticate_owner

    app = create_app()
    app.dependency_overrides[authenticate_owner] = _trusted_owner
    yield app
    app.dependency_overrides.pop(authenticate_owner, None)


# --- Claim / at-most-once -------------------------------------------------


def test_claim_on_ready_task_succeeds(session: Session) -> None:
    _, task = _seed_task(session, status=TaskStatus.READY)
    assert task.lease_owner is None
    assert claim_task_for_execution(session, task_id=task.id, owner="w1") is True
    session.refresh(task)
    assert task.status == TaskStatus.RUNNING
    assert task.lease_owner == "w1"
    assert task.lease_expires_at is not None
    assert task_lease_is_active(task) is True


def test_second_claimer_on_running_task_is_rejected(session: Session) -> None:
    """At-most-once: once READY -> RUNNING, a concurrent caller loses."""
    _, task = _seed_task(session, status=TaskStatus.READY)
    assert claim_task_for_execution(session, task_id=task.id, owner="w1") is True
    session.refresh(task)
    # A second caller (or a second process) must NOT flip it again.
    assert claim_task_for_execution(session, task_id=task.id, owner="w2") is False
    session.refresh(task)
    assert task.status == TaskStatus.RUNNING
    assert task.lease_owner == "w1"


def test_claim_requires_ready_status(session: Session) -> None:
    """A non-READY task (terminal, or already RUNNING) cannot be claimed."""
    _, running = _seed_task(session, status=TaskStatus.RUNNING)
    assert claim_task_for_execution(session, task_id=running.id, owner="w1") is False
    session.refresh(running)
    assert running.lease_owner is None

    _, failed = _seed_task(session, status=TaskStatus.FAILED)
    assert claim_task_for_execution(session, task_id=failed.id, owner="w1") is False


def test_claim_on_unknown_task_fails(session: Session) -> None:
    assert claim_task_for_execution(session, task_id="task_missing", owner="w1") is False


# --- Release --------------------------------------------------------------


def test_release_by_owner_clears_lease(session: Session) -> None:
    _, task = _seed_task(
        session,
        status=TaskStatus.RUNNING,
        lease_owner="w1",
        lease_expires_at=_naive_now() + timedelta(seconds=60),
    )
    assert release_task_lease(session, task_id=task.id, owner="w1") is True
    session.refresh(task)
    assert task.lease_owner is None
    assert task.lease_expires_at is None
    assert task_lease_is_active(task) is False


def test_release_by_non_owner_is_noop(session: Session) -> None:
    _, task = _seed_task(
        session,
        status=TaskStatus.RUNNING,
        lease_owner="w1",
        lease_expires_at=_naive_now() + timedelta(seconds=60),
    )
    assert release_task_lease(session, task_id=task.id, owner="w2") is False
    session.refresh(task)
    assert task.lease_owner == "w1"


# --- Recovery (fail-closed) ----------------------------------------------


def test_stranded_running_task_is_recovered(session: Session) -> None:
    _, task = _seed_task(session, status=TaskStatus.RUNNING)
    report = recover_stranded_tasks(session, owner="recovery-1")
    assert report.scanned == 1
    assert report.recovered_count == 1
    session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.lease_owner is None
    assert task.lease_expires_at is None


def test_running_task_with_active_lease_not_recovered(session: Session) -> None:
    _, task = _seed_task(
        session,
        status=TaskStatus.RUNNING,
        lease_owner="w1",
        lease_expires_at=_naive_now() + timedelta(seconds=3600),
    )
    report = recover_stranded_tasks(session, owner="recovery-1")
    assert report.scanned == 0
    assert report.recovered_count == 0
    session.refresh(task)
    assert task.status == TaskStatus.RUNNING
    assert task.lease_owner == "w1"


def test_fresh_running_task_inside_grace_not_recovered(session: Session) -> None:
    """The grace period stops recovery reclaiming a run a live process owns."""
    _, task = _seed_task(session, status=TaskStatus.RUNNING, age_seconds=1.0)
    assert find_stranded_tasks(session) == []
    assert recover_stranded_tasks(session).recovered_count == 0
    session.refresh(task)
    assert task.status == TaskStatus.RUNNING


def test_terminal_tasks_are_never_recovery_candidates(session: Session) -> None:
    for status in (
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.READY,
        TaskStatus.BACKLOG,
        TaskStatus.REVIEW,
    ):
        _seed_task(session, status=status)
    assert find_stranded_tasks(session, grace_seconds=0) == []


def test_recovery_is_idempotent(session: Session) -> None:
    _, task = _seed_task(session, status=TaskStatus.RUNNING)
    first = recover_stranded_tasks(session, owner="recovery-1")
    second = recover_stranded_tasks(session, owner="recovery-2")
    assert first.recovered_count == 1
    assert second.scanned == 0
    assert second.recovered_count == 0
    session.refresh(task)
    assert task.status == TaskStatus.FAILED


def test_recovery_claim_is_mutually_exclusive(session: Session) -> None:
    """Two claimants over the SAME frozen candidate: exactly one wins."""
    from aios.task_run import _claim_and_fail

    _, task = _seed_task(session, status=TaskStatus.RUNNING)
    candidates = find_stranded_tasks(session, grace_seconds=0)
    assert len(candidates) == 1
    assert _claim_and_fail(session, task_id=task.id, now_naive=_naive_now()) is True
    # w2 races on the same (now stale) candidate list -- the CAS must reject it.
    assert _claim_and_fail(session, task_id=task.id, now_naive=_naive_now()) is False
    session.refresh(task)
    assert task.status == TaskStatus.FAILED


def test_concurrent_recovery_only_one_claimant(session: Session) -> None:
    """Two recovery workers (separate sessions) reclaim one task exactly once."""
    _, task = _seed_task(session, status=TaskStatus.RUNNING)
    engine = get_engine(get_database_url())
    with Session(engine) as s1, Session(engine) as s2:
        assert len(find_stranded_tasks(s1, grace_seconds=0)) == 1
        assert len(find_stranded_tasks(s2, grace_seconds=0)) == 1

        first = recover_stranded_tasks(s1, owner="recovery-A", grace_seconds=0)
        second = recover_stranded_tasks(s2, owner="recovery-B", grace_seconds=0)

    assert first.recovered_count == 1
    assert second.recovered_count == 0
    session.refresh(task)
    assert task.status == TaskStatus.FAILED
    audits = session.exec(
        select(AuditLog).where(AuditLog.action == AuditEvent.TASK_RUN_RECOVERED)
    ).all()
    assert len(audits) == 1
    assert audits[0].after_snapshot["recovery_owner"] == "recovery-A"
    assert audits[0].before_snapshot["status"] == TaskStatus.RUNNING.value


def test_recovery_skips_task_with_artifact(session: Session) -> None:
    """Asset guard: never fail a task that already produced an artifact."""
    project, task = _seed_task(session, status=TaskStatus.RUNNING)
    _seed_artifact(session, task_id=task.id, project_id=project.id)
    report = recover_stranded_tasks(session, owner="recovery-1")
    assert report.scanned == 1
    assert report.recovered_count == 0
    assert task.id in report.skipped_with_artifact
    session.refresh(task)
    assert task.status == TaskStatus.RUNNING  # left intact


def test_recovery_writes_audit(session: Session) -> None:
    previous_owner = "w-gone"
    _, task = _seed_task(
        session,
        status=TaskStatus.RUNNING,
        lease_owner=previous_owner,
        lease_expires_at=_naive_now() - timedelta(seconds=10),
    )
    recover_stranded_tasks(session, owner="recovery-9")

    audits = session.exec(
        select(AuditLog).where(AuditLog.action == AuditEvent.TASK_RUN_RECOVERED)
    ).all()
    assert len(audits) == 1
    audit = audits[0]
    assert audit.resource_type == "task"
    assert audit.resource_id == task.id
    assert audit.before_snapshot["status"] == TaskStatus.RUNNING.value
    assert audit.before_snapshot["lease_owner"] == previous_owner
    assert audit.after_snapshot["status"] == TaskStatus.FAILED.value
    assert audit.after_snapshot["recovery_owner"] == "recovery-9"
    assert audit.before_snapshot["recovery_reason"] == STRANDED_TASK_RECOVERY_REASON
    # No credential material may reach the audit trail.
    blob = f"{audit.before_snapshot}{audit.after_snapshot}"
    for forbidden in ("secret_ref", "context_ref", "callback_url", "token"):
        assert forbidden not in blob


def test_recovery_reaches_terminal_state(session: Session) -> None:
    _, task = _seed_task(session, status=TaskStatus.RUNNING)
    recover_stranded_tasks(session, owner="recovery-1")
    session.refresh(task)
    assert task.status == TaskStatus.FAILED
    # A terminal task is no longer a candidate, so nothing re-reclaims it.
    assert find_stranded_tasks(session, grace_seconds=0) == []


def test_recovery_respects_scan_limit(session: Session) -> None:
    for _ in range(3):
        _seed_task(session, status=TaskStatus.RUNNING)
    report = recover_stranded_tasks(session, owner="recovery-1", limit=2)
    assert report.scanned == 2
    assert report.recovered_count == 2


# --- At-most-once / crash semantics ---------------------------------------


def test_unknown_execution_is_fail_closed_not_resumed(session: Session) -> None:
    """A RUNNING task with no known outcome is FAILED, NEVER blind-retried."""
    _, task = _seed_task(session, status=TaskStatus.RUNNING)
    report = recover_stranded_tasks(session, owner="recovery-1")
    assert report.recovered_count == 1
    session.refresh(task)
    assert task.status == TaskStatus.FAILED
    # No new task was minted for the same logical execution.
    tasks = session.exec(select(Task)).all()
    assert len(tasks) == 1
    assert tasks[0].id == task.id


def test_duplicate_recovery_does_not_create_second_task(session: Session) -> None:
    _, task = _seed_task(session, status=TaskStatus.RUNNING)
    for _ in range(3):
        recover_stranded_tasks(session, owner="recovery-1", grace_seconds=0)
    tasks = session.exec(select(Task)).all()
    assert len(tasks) == 1
    assert tasks[0].id == task.id
    assert tasks[0].status == TaskStatus.FAILED


def test_recovered_task_keeps_its_identity(session: Session) -> None:
    _, task = _seed_task(session, status=TaskStatus.RUNNING)
    original_id = task.id
    recover_stranded_tasks(session, owner="recovery-1")
    session.refresh(task)
    assert task.id == original_id
    assert task.status == TaskStatus.FAILED


# --- Task query surface ----------------------------------------------------


def test_task_execution_surface_exposes_lease(owner_app) -> None:
    with TestClient(owner_app, follow_redirects=False) as client:
        with Session(get_engine(get_database_url())) as s:
            _, task = _seed_task(
                s,
                status=TaskStatus.RUNNING,
                lease_owner="w1",
                lease_expires_at=_naive_now() + timedelta(seconds=3600),
            )
            task_id = task.id

        resp = client.get(f"/tasks/{task_id}/execution")
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == task_id
        assert body["status"] == TaskStatus.RUNNING.value
        assert body["assigned_agent_id"] is None
        assert body["routing_mode"] == RoutingMode.FIXED.value
        assert body["lease"]["owner"] == "w1"
        assert body["lease"]["active"] is True
        # Secret boundary: credential-adjacent fields are never exposed.
        for forbidden in ("secret_ref", "context_ref", "callback_url"):
            assert forbidden not in body

        missing = client.get("/tasks/task_does_not_exist/execution")
        assert missing.status_code == 404


def test_recover_endpoint_reclaims_and_reports(owner_app) -> None:
    with TestClient(owner_app, follow_redirects=False) as client:
        with Session(get_engine(get_database_url())) as s:
            _seed_task(s, status=TaskStatus.RUNNING)  # seeded AFTER startup recovery ran

        resp = client.post("/tasks/recover")
        assert resp.status_code == 200
        body = resp.json()
        assert body["recovered_count"] == 1
        assert body["recovered"][0]["resulting_status"] == TaskStatus.FAILED.value

        # Second call is a no-op (idempotent).
        assert client.post("/tasks/recover").json()["recovered_count"] == 0

        with Session(get_engine(get_database_url())) as s:
            persisted = s.get(Task, body["recovered"][0]["task_id"])
            assert persisted.status == TaskStatus.FAILED


def test_task_surface_requires_owner_auth(tmp_path, monkeypatch) -> None:
    """The lifecycle surface is owner-only (no anonymous execution control)."""
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'task_auth.db'}")
    monkeypatch.setenv("AIOS_OWNER_ID", "owner")
    monkeypatch.setenv("AIOS_OWNER_API_KEY", "k" * 40)
    from aios.api.app import create_app

    app = create_app()  # real authenticate_owner -- no override
    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/tasks/task_x/execution").status_code == 401
        assert client.post("/tasks/recover").status_code == 401


# --- Architecture invariants ----------------------------------------------


def test_no_new_execution_entity_was_introduced() -> None:
    """P0-B adds lease columns to Task -- nothing else."""
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

    columns = {name for name in Task.model_fields}
    for forbidden in ("runtime_id", "execution_id", "worker_id"):
        assert forbidden not in columns
    # The lease is the only addition.
    assert {"lease_owner", "lease_expires_at"} <= columns


def test_task_status_enum_reuses_failed_not_new_state() -> None:
    """No new lifecycle state was invented; FAILED is reused as the terminal."""
    values = {member.value for member in TaskStatus}
    assert "expired" not in values  # explicitly NOT added
    assert "failed" in values  # reused as the retryable terminal


def test_lease_owner_is_an_opaque_token_not_a_domain_identity() -> None:
    """lease_owner must never be an agent / employee / runtime identity."""
    owner = new_task_lease_owner()
    assert owner.startswith("lease_")
    assert "agt" not in owner and "emp" not in owner