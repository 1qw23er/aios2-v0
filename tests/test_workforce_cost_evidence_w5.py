"""Contract tests for Workforce W5 -- cost evidence bookkeeping (V1).

Scope (see ``docs/workforce/Workforce_W5_Design_V1.md`` §15, F-W5-*):

* ``cost_evidence`` is a PURE ADDITION: one new table, the 11 frozen W1--W4
  Workforce tables untouched, no ``Project`` FK, no existing ``ON DELETE``
  altered (S1/S4 + boundary regression);
* both FKs are RESTRICT (fail-closed): deleting a parent with evidence rows
  raises ``IntegrityError``, never a silent cascade (F1/F2);
* ``idempotency_key = f"{source_event_type}:{source_event_id}"`` -- replay of
  the same source event is rejected by the UNIQUE index (at-most-once), while
  distinct real events each produce their own row (I1/I2);
* the writer requires a REAL source event identity (P1) and structurally
  cannot touch Budget machinery or have a caller in V1 (P2 / B1);
* every row is trailed by ``append_audit`` in the SAME savepoint, with
  ``project_id=None`` / ``task_id=None`` (A1);
* the writer is owner-only, keyword-only, with no default actor (W4 Q7
  pattern): missing actor -> ``TypeError``, non-owner -> 403.

Helpers mirror ``tests/test_workforce_employee_w4.py`` so this file is
self-contained.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

import aios.workforce_cost_evidence
from aios.actor import ActorContext
from aios.audit import AuditLog
from aios.db import get_engine, run_migrations
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    ApprovalStatus,
    Capability,
    CostEvidence,
    Employee,
    EmployeeStatus,
    Job,
    JobVersion,
    TrialOutcome,
)
from aios.services import ServiceError
from aios.workforce import (
    compute_match,
    create_business_goal,
    create_job,
    create_required_work,
    discover_candidates,
    evaluate_candidate,
)
from aios.workforce_cost_evidence import record_cost_evidence
from aios.workforce_employee import (
    activate_trial,
    complete_trial,
    promote_to_employee,
)
from aios.workforce_recommendation import (
    decide_recommendation,
    recommend_candidate,
)
from aios.workforce_trial import create_trial_from_approval

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "aios"

# Carried by the caller, never defaulted by the service (W4 Q7 pattern).
OWNER = ActorContext(kind="owner", owner_id="owner")
AGENT = ActorContext(kind="agent", agent_id="agent-1")
SYSTEM = ActorContext.system()

#: The 11 frozen W1--W4 Workforce tables (Design §11) -- boundary regression set.
WORKFORCE_TABLES = {
    "business_goal",
    "required_work",
    "job",
    "job_version",
    "capability_requirement",
    "candidate",
    "benchmark_result",
    "match",
    "recommendation",
    "trial",
    "employee",
}

#: The exact ``employee`` column set frozen by W4 (boundary regression).
EMPLOYEE_COLUMNS = {
    "id",
    "candidate_id",
    "trial_id",
    "agent_id",
    "job_id",
    "job_version_id",
    "status",
    "hired_at",
    "created_at",
    "updated_at",
}


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors test_workforce_employee_w4.py)
# ---------------------------------------------------------------------------


def _db(url: str) -> Session:
    run_migrations(url)
    return Session(get_engine(url))


def _seed_capability(session: Session, name: str) -> Capability:
    cap = Capability(name=name, description=f"{name} capability")
    session.add(cap)
    session.commit()
    return cap


def _seed_agent(
    session: Session,
    name: str,
    capabilities: dict[str, int] | None = None,
) -> Agent:
    agent = Agent(name=name, role=name, adapter_type=AdapterType.EXTERNAL)
    session.add(agent)
    session.flush()
    for cap_name, priority in (capabilities or {}).items():
        cap = session.exec(
            select(Capability).where(Capability.name == cap_name)
        ).first()
        assert cap is not None, f"capability must be seeded first: {cap_name}"
        session.add(
            AgentCapability(
                agent_id=agent.id,
                capability_id=cap.id,
                priority=priority,
            )
        )
    session.commit()
    return agent


def _build_chain(session: Session, cap_name: str) -> tuple[Job, JobVersion]:
    # Idempotent seeding: tests that only need a JobVersion anchor call this
    # directly; the full _hire path seeds the same capability beforehand.
    existing = session.exec(
        select(Capability).where(Capability.name == cap_name)
    ).first()
    if existing is None:
        _seed_capability(session, cap_name)
    goal = create_business_goal(session, "增长北极星", target_outcome="新增注册 +20%")
    rw = create_required_work(
        session, goal.id, "公众号内容生产", rationale="内容带来自然注册"
    )
    job = create_job(
        session,
        rw.id,
        "内容初稿研究员",
        role_summary="把选题做成初稿",
        capability_names=[cap_name],
    )
    session.commit()
    head = session.get(JobVersion, job.head_version_id)
    assert head is not None
    return job, head


def _audits(session: Session, action: str) -> list[AuditLog]:
    return list(
        session.exec(select(AuditLog).where(AuditLog.action == action)).all()
    )


def _hire(session: Session) -> tuple[JobVersion, str]:
    """Run the full W1--W4 chain to a hired Employee; return (head, employee_id)."""
    _seed_capability(session, "writing")
    _seed_agent(session, "A", {"writing": 80})
    _, head = _build_chain(session, "writing")
    cands = discover_candidates(session, head.id)
    session.commit()
    assert len(cands) == 1, "fixture expects exactly one matching agent"
    cand = cands[0]
    evaluate_candidate(session, cand.id)
    session.commit()
    compute_match(session, cand.id, head.id)
    session.commit()
    rec = recommend_candidate(session, cand.id)
    session.commit()
    decide_recommendation(session, rec.id, ApprovalStatus.APPROVED, actor=OWNER)
    session.commit()
    trial = create_trial_from_approval(session, rec.id, actor=OWNER)
    session.commit()
    activate_trial(session, trial.id, actor=OWNER)
    session.commit()
    complete_trial(session, trial.id, outcome=TrialOutcome.PASS, actor=OWNER)
    session.commit()
    emp = promote_to_employee(session, trial.id, actor=OWNER)
    session.commit()
    return head, emp.id


# ---------------------------------------------------------------------------
# Schema / migration (F-W5-S1..S4)
# ---------------------------------------------------------------------------


def test_cost_evidence_table_exists_and_workforce_tables_unchanged(
    template_schema: dict[str, object],
) -> None:
    """F-W5-S1: ``cost_evidence`` added; all 11 frozen Workforce tables intact."""
    tables = template_schema["tables"]
    assert isinstance(tables, set)
    assert "cost_evidence" in tables
    missing = WORKFORCE_TABLES - tables
    assert not missing, f"frozen workforce tables missing after W5: {missing}"


def test_employee_columns_frozen_after_w5(tmp_path: Path) -> None:
    """Boundary regression: ``employee`` columns are exactly the W4 set."""
    url = f"sqlite:///{(tmp_path / 'mig.db').as_posix()}"
    engine = get_engine(url)
    run_migrations(url)
    insp = inspect(engine)
    emp_cols = {c["name"] for c in insp.get_columns("employee")}
    assert emp_cols == EMPLOYEE_COLUMNS


def test_cost_evidence_fks_are_restrict(tmp_path: Path) -> None:
    """F-W5-S2: both FKs RESTRICT; ``employee_id`` nullable, anchor NOT NULL."""
    url = f"sqlite:///{(tmp_path / 'fk.db').as_posix()}"
    engine = get_engine(url)
    run_migrations(url)
    insp = inspect(engine)
    fks = {
        fk["constrained_columns"][0]: fk for fk in insp.get_foreign_keys("cost_evidence")
    }
    assert set(fks) == {"job_version_id", "employee_id"}
    assert fks["job_version_id"]["referred_table"] == "job_version"
    assert fks["employee_id"]["referred_table"] == "employee"
    # SQLite reflection reports ondelete under ``options``.
    assert fks["job_version_id"].get("options", {}).get("ondelete") == "RESTRICT"
    assert fks["employee_id"].get("options", {}).get("ondelete") == "RESTRICT"

    cols = {c["name"]: c for c in insp.get_columns("cost_evidence")}
    assert cols["employee_id"]["nullable"] is True
    assert cols["job_version_id"]["nullable"] is False


def test_cost_evidence_schema_shape(tmp_path: Path) -> None:
    """F-W5-S3: unique idempotency index; nullable float amount; NO currency."""
    url = f"sqlite:///{(tmp_path / 'shape.db').as_posix()}"
    engine = get_engine(url)
    run_migrations(url)
    insp = inspect(engine)

    cols = {c["name"]: c for c in insp.get_columns("cost_evidence")}
    assert set(cols) == {
        "id",
        "job_version_id",
        "employee_id",
        "amount",
        "source_event_type",
        "source_event_id",
        "idempotency_key",
        "recorded_at",
        "note",
    }
    assert cols["amount"]["nullable"] is True
    assert "currency" not in cols  # G3 / D-1.3: no currency column in V1

    indexes = {i["name"]: i for i in insp.get_indexes("cost_evidence")}
    # SQLite reflection yields ``unique`` as 0/1 -- compare truthily.
    assert indexes["ix_cost_evidence_idempotency_key"]["unique"]
    assert not indexes["ix_cost_evidence_job_version_id"]["unique"]
    assert not indexes["ix_cost_evidence_employee_id"]["unique"]


def test_alembic_single_head_is_w5_cost_evidence() -> None:
    """F-W5-S4: single head advanced exactly to the W5 Cost Evidence migration."""
    cfg = Config(ROOT / "alembic.ini")
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == ["20260904_0001_workforce_cost_evidence"]


# ---------------------------------------------------------------------------
# Owner-only gate (W4 Q7 pattern)
# ---------------------------------------------------------------------------


def test_record_rejects_agent_and_system_actor_403(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'g1.db').as_posix()}"
    with _db(url) as session:
        _, head = _build_chain(session, "writing")
        for actor in (AGENT, SYSTEM):
            with pytest.raises(ServiceError) as exc:
                record_cost_evidence(
                    session,
                    job_version_id=head.id,
                    amount=1.0,
                    source_event_type="future_workforce_event",
                    source_event_id="evt-1",
                    actor=actor,
                )
            assert exc.value.status_code == 403
        assert session.exec(select(CostEvidence)).all() == []


def test_record_missing_actor_raises_typeerror(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'g2.db').as_posix()}"
    with _db(url) as session:
        _, head = _build_chain(session, "writing")
        with pytest.raises(TypeError):
            # keyword-only actor with NO default: forgetting it must be loud.
            record_cost_evidence(  # type: ignore[call-arg]
                session,
                job_version_id=head.id,
                amount=1.0,
                source_event_type="future_workforce_event",
                source_event_id="evt-1",
            )


# ---------------------------------------------------------------------------
# Provenance / no-fabrication (F-W5-P1 / P2)
# ---------------------------------------------------------------------------


def test_record_requires_real_source_event_identity(tmp_path: Path) -> None:
    """F-W5-P1: empty identity is a hard reject -- no default, no synthetic value."""
    url = f"sqlite:///{(tmp_path / 'p1.db').as_posix()}"
    with _db(url) as session:
        _, head = _build_chain(session, "writing")
        for src_type, src_id in (("", "evt-1"), ("future_workforce_event", "")):
            with pytest.raises(ServiceError) as exc:
                record_cost_evidence(
                    session,
                    job_version_id=head.id,
                    amount=1.0,
                    source_event_type=src_type,
                    source_event_id=src_id,
                    actor=OWNER,
                )
            assert exc.value.status_code == 422
        assert session.exec(select(CostEvidence)).all() == []


def test_record_requires_measured_amount(tmp_path: Path) -> None:
    """I6: only measured costs are recorded -- ``amount=None`` stays "no row"."""
    url = f"sqlite:///{(tmp_path / 'p1b.db').as_posix()}"
    with _db(url) as session:
        _, head = _build_chain(session, "writing")
        with pytest.raises(ServiceError) as exc:
            record_cost_evidence(
                session,
                job_version_id=head.id,
                amount=None,  # type: ignore[arg-type]
                source_event_type="future_workforce_event",
                source_event_id="evt-1",
                actor=OWNER,
            )
        assert exc.value.status_code == 422
        assert session.exec(select(CostEvidence)).all() == []


def test_record_fail_closed_on_missing_parents(tmp_path: Path) -> None:
    """I3: a dangling attribution is a 404, never a silently orphaned row."""
    url = f"sqlite:///{(tmp_path / 'p1c.db').as_posix()}"
    with _db(url) as session:
        with pytest.raises(ServiceError) as exc:
            record_cost_evidence(
                session,
                job_version_id="jv_missing",
                amount=1.0,
                source_event_type="future_workforce_event",
                source_event_id="evt-1",
                actor=OWNER,
            )
        assert exc.value.status_code == 404

        _, head = _build_chain(session, "writing")
        with pytest.raises(ServiceError) as exc:
            record_cost_evidence(
                session,
                job_version_id=head.id,
                amount=1.0,
                source_event_type="future_workforce_event",
                source_event_id="evt-1",
                employee_id="emp_missing",
                actor=OWNER,
            )
        assert exc.value.status_code == 404
        assert session.exec(select(CostEvidence)).all() == []


def test_writer_structurally_excluded_from_budget_machinery() -> None:
    """F-W5-P2: the writer never references Budget machinery (AST-level scan).

    Docstring PROSE may describe the prohibition; executable code may not
    reference it -- hence an AST scan (names/attributes/imports), not a
    substring match.
    """
    source = Path(aios.workforce_cost_evidence.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    identifiers: set[str] = set()
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            imported_modules.add(node.module or "")
        elif isinstance(node, ast.Import):
            identifiers.update(a.name for a in node.names)
    assert "check_budget" not in identifiers
    assert "budget_used" not in identifiers
    assert not any("delegation" in m for m in imported_modules)


def test_writer_has_no_caller_in_v1() -> None:
    """F-W5-B1 (structural half): no src module imports the W5 writer.

    D-1.4: the repo has no Workforce-native cost source event, so the writer
    must be a dormant contract. Any import from ``src/`` would be a fabricated
    caller.
    """
    for path in SRC.rglob("*.py"):
        if path.name == "workforce_cost_evidence.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "aios.workforce_cost_evidence", (
                    f"fabricated caller in V1: {path}"
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "aios.workforce_cost_evidence", (
                        f"fabricated caller in V1: {path}"
                    )


# ---------------------------------------------------------------------------
# Idempotency / replay (F-W5-I1 / I2)
# ---------------------------------------------------------------------------


def test_record_is_replay_safe_at_most_once(tmp_path: Path) -> None:
    """F-W5-I1: replaying the same source event is rejected; exactly 1 row."""
    url = f"sqlite:///{(tmp_path / 'i1.db').as_posix()}"
    with _db(url) as session:
        _, head = _build_chain(session, "writing")
        record_cost_evidence(
            session,
            job_version_id=head.id,
            amount=12.5,
            source_event_type="future_workforce_event",
            source_event_id="evt-1",
            actor=OWNER,
        )
        session.commit()

        with pytest.raises(IntegrityError):
            record_cost_evidence(
                session,
                job_version_id=head.id,
                amount=12.5,
                source_event_type="future_workforce_event",
                source_event_id="evt-1",
                actor=OWNER,
            )
        session.rollback()

        rows = session.exec(select(CostEvidence)).all()
        assert len(rows) == 1
        assert rows[0].idempotency_key == "future_workforce_event:evt-1"
        assert rows[0].amount == 12.5


def test_idempotency_key_binds_source_event_natural_key(tmp_path: Path) -> None:
    """F-W5-I2: key is deterministically f"{source_event_type}:{source_event_id}"."""
    url = f"sqlite:///{(tmp_path / 'i2.db').as_posix()}"
    with _db(url) as session:
        _, head = _build_chain(session, "writing")
        ce = record_cost_evidence(
            session,
            job_version_id=head.id,
            amount=1.0,
            source_event_type="future_workforce_event",
            source_event_id="evt-42",
            actor=OWNER,
        )
        assert ce.idempotency_key == "future_workforce_event:evt-42"


def test_distinct_source_events_are_distinct_facts(tmp_path: Path) -> None:
    """Two distinct real events -> two rows (two cost facts), two audits."""
    url = f"sqlite:///{(tmp_path / 'i3.db').as_posix()}"
    with _db(url) as session:
        _, head = _build_chain(session, "writing")
        for evt in ("evt-1", "evt-2"):
            record_cost_evidence(
                session,
                job_version_id=head.id,
                amount=3.0,
                source_event_type="future_workforce_event",
                source_event_id=evt,
                actor=OWNER,
            )
            session.commit()
        rows = session.exec(select(CostEvidence)).all()
        assert {r.idempotency_key for r in rows} == {
            "future_workforce_event:evt-1",
            "future_workforce_event:evt-2",
        }
        assert len(_audits(session, "cost_evidence.create")) == 2


# ---------------------------------------------------------------------------
# Audit consistency (F-W5-A1)
# ---------------------------------------------------------------------------


def test_record_trails_audit_in_same_transaction(tmp_path: Path) -> None:
    """F-W5-A1: audit entry matches the row; no Project/Task (I10); same key."""
    url = f"sqlite:///{(tmp_path / 'a1.db').as_posix()}"
    with _db(url) as session:
        _, head = _build_chain(session, "writing")
        ce = record_cost_evidence(
            session,
            job_version_id=head.id,
            amount=7.25,
            source_event_type="future_workforce_event",
            source_event_id="evt-9",
            actor=OWNER,
        )
        session.commit()

        audits = _audits(session, "cost_evidence.create")
        assert len(audits) == 1
        audit = audits[0]
        assert audit.resource_type == "cost_evidence"
        assert audit.resource_id == ce.id
        assert audit.project_id is None
        assert audit.task_id is None
        assert audit.idempotency_key == ce.idempotency_key
        assert audit.actor == "owner"
        assert audit.after_snapshot == {
            "job_version_id": head.id,
            "employee_id": None,
            "amount": 7.25,
            "source_event_type": "future_workforce_event",
            "source_event_id": "evt-9",
        }


# ---------------------------------------------------------------------------
# Fail-closed / lifecycle (F-W5-F1 / F2, F-W5-L1)
# ---------------------------------------------------------------------------


def test_deleting_job_version_with_evidence_is_blocked(tmp_path: Path) -> None:
    """F-W5-F1: RESTRICT -- deleting an anchored JobVersion raises, no cascade."""
    url = f"sqlite:///{(tmp_path / 'f1.db').as_posix()}"
    with _db(url) as session:
        _, head = _build_chain(session, "writing")
        record_cost_evidence(
            session,
            job_version_id=head.id,
            amount=1.0,
            source_event_type="future_workforce_event",
            source_event_id="evt-1",
            actor=OWNER,
        )
        session.commit()

        jv = session.get(JobVersion, head.id)
        assert jv is not None
        session.delete(jv)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
        assert session.get(JobVersion, head.id) is not None
        assert len(session.exec(select(CostEvidence)).all()) == 1


def test_deleting_employee_with_evidence_is_blocked(tmp_path: Path) -> None:
    """F-W5-F2: RESTRICT on ``employee_id`` blocks deletion; Employee permanent."""
    url = f"sqlite:///{(tmp_path / 'f2.db').as_posix()}"
    with _db(url) as session:
        head, emp_id = _hire(session)
        record_cost_evidence(
            session,
            job_version_id=head.id,
            amount=99.0,
            source_event_type="future_workforce_event",
            source_event_id="evt-hire-1",
            employee_id=emp_id,
            actor=OWNER,
        )
        session.commit()

        emp = session.get(Employee, emp_id)
        assert emp is not None
        session.delete(emp)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
        assert session.get(Employee, emp_id) is not None


def test_employee_lifecycle_unchanged_single_active_state() -> None:
    """F-W5-L1: W5 adds no EmployeeStatus member and no lifecycle edge."""
    assert [s.value for s in EmployeeStatus] == ["active"]
    # W4's Trial machine edges are untouched by W5 (regression sanity).
    from aios.models import TrialStatus
    from aios.workforce_employee import TrialLifecycle

    assert TrialLifecycle.can_transition(
        TrialStatus.PROPOSED, TrialStatus.ACTIVE
    )
    assert not TrialLifecycle.can_transition(
        TrialStatus.COMPLETED, TrialStatus.ACTIVE
    )


# ---------------------------------------------------------------------------
# Honest empty population (F-W5-B1)
# ---------------------------------------------------------------------------


def test_v1_population_is_zero_without_a_caller(tmp_path: Path) -> None:
    """F-W5-B1: a fresh migrated DB holds ZERO evidence rows -- documented state.

    The repo has no Workforce-native cost source event (D-1.4), so the table is
    schema-only in V1. This test fails the day someone fabricates a caller
    without introducing a real source event.
    """
    url = f"sqlite:///{(tmp_path / 'b1.db').as_posix()}"
    with _db(url) as session:
        assert session.exec(select(CostEvidence)).all() == []
        # No candidate was ever moved by cost bookkeeping (I9): the W4
        # lifecycle stays fully independent of evidence writes.
        assert _audits(session, "cost_evidence.create") == []
