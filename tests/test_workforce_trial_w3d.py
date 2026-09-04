"""Contract tests for Workforce W3-D -- Trial (the W3-C -> W4 hand-off).

Scope (see ``docs/workforce/Workforce_W3D_Trial_Spec_V1.md``):

* the ONLY creator of a ``Trial`` is ``create_trial_from_approval``, and it pushes
  the candidate ``RECOMMENDED -> TRIALING``;
* the single gate is ``assert_trial_eligible`` (W3-C's lazy F-R8 reconcile); a
  non-eligible / drifting recommendation is refused with 409, a missing one with
  404, a non-owner actor with 403;
* all three parent FKs are RESTRICT (no unlock path in V1, Spec §3-Q2);
* the creation writes exactly one ``trial.created`` audit inside the same SAVEPOINT
  as the state writes (INV-T5), and is idempotent (§7);
* W3-D adds NO Employee / Budget / Scheduler / Execution dependency and does NOT
  modify any W3-C definition (T-TRIAL-BOUNDARY).

Helpers mirror ``tests/test_workforce_recommendation_w3c.py`` so this file is
self-contained.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import DateTime as SADateTime
from sqlalchemy import String as SAString
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from aios.actor import ActorContext
from aios.audit import AuditLog
from aios.db import get_engine, run_migrations
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    ApprovalStatus,
    Candidate,
    CandidateStatus,
    Capability,
    Job,
    JobVersion,
    Match,
    Recommendation,
    RecommendationStatus,
    Trial,
    TrialStatus,
)
from aios.services import ServiceError
from aios.workforce import (
    CandidateLifecycle,
    compute_match,
    discover_candidates,
    evaluate_candidate,
    list_capability_requirements,
)
from aios.workforce_recommendation import (
    decide_recommendation,
    recommend_candidate,
)
from aios.workforce_trial import create_trial_from_approval
from alembic import command

ROOT = Path(__file__).resolve().parents[1]

ReqSpec = tuple[str, int, bool]  # (capability_name, min_proficiency, required)

# Built explicitly rather than through ``resolve_owner_actor``: the P2-1 gate is
# about never *defaulting* to owner, and the tests must prove the identity is
# carried by the caller, not manufactured by the service.
OWNER = ActorContext(kind="owner", owner_id="owner")
AGENT = ActorContext(kind="agent", agent_id="agent-1")
SYSTEM = ActorContext.system()


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors test_workforce_recommendation_w3c.py)
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
    *,
    cost_policy: dict[str, object] | None = None,
) -> Agent:
    agent = Agent(name=name, role=name, adapter_type=AdapterType.EXTERNAL)
    if cost_policy is not None:
        agent.cost_policy = dict(cost_policy)
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


def _build_chain(
    session: Session, *specs: ReqSpec
) -> tuple[Job, JobVersion]:
    from aios.workforce import (
        create_business_goal,
        create_job,
        create_required_work,
    )

    goal = create_business_goal(session, "增长北极星", target_outcome="新增注册 +20%")
    rw = create_required_work(
        session, goal.id, "公众号内容生产", rationale="内容带来自然注册"
    )
    job = create_job(
        session,
        rw.id,
        "内容初稿研究员",
        role_summary="把选题做成初稿",
        capability_names=[s[0] for s in specs],
    )
    session.commit()
    head = session.get(JobVersion, job.head_version_id)
    assert head is not None
    existing = {
        r.capability_name: r
        for r in list_capability_requirements(session, head.id)
    }
    for name, min_proficiency, required in specs:
        req = existing[name]
        req.min_proficiency = min_proficiency
        req.required = required
    session.commit()
    return job, head


def _pool(session: Session, head: JobVersion) -> Candidate:
    cands = discover_candidates(session, head.id)
    session.commit()
    assert len(cands) == 1, "fixture expects exactly one matching agent"
    return cands[0]


def _audits(session: Session, action: str) -> list[AuditLog]:
    return list(
        session.exec(
            select(AuditLog).where(AuditLog.action == action)
        ).all()
    )


def _prepared(
    session: Session,
    *,
    cap_name: str = "writing",
    min_prof: int = 50,
    priority: int = 80,
    cost_policy: dict[str, object] | None = None,
) -> tuple[Candidate, Match, JobVersion]:
    """An EVALUATED candidate carrying a COMPUTED Match (the W3-C input state)."""
    _seed_capability(session, cap_name)
    _seed_agent(session, "A", {cap_name: priority}, cost_policy=cost_policy)
    _, head = _build_chain(session, (cap_name, min_prof, True))
    cand = _pool(session, head)
    evaluate_candidate(session, cand.id)
    session.commit()
    match = compute_match(session, cand.id, head.id)
    session.commit()
    return cand, match, head


def _approved(
    session: Session,
    *,
    cap_name: str = "writing",
    min_prof: int = 50,
    priority: int = 80,
) -> tuple[Recommendation, Candidate, Match, JobVersion]:
    """An APPROVED recommendation (the W3-D input state)."""
    cand, match, head = _prepared(
        session, cap_name=cap_name, min_prof=min_prof, priority=priority
    )
    rec = recommend_candidate(session, cand.id)
    session.commit()
    decide_recommendation(session, rec.id, ApprovalStatus.APPROVED, actor=OWNER)
    session.commit()
    return rec, cand, match, head


# ---------------------------------------------------------------------------
# T-TRIAL-GATE -- fail-closed (1-7)
# ---------------------------------------------------------------------------


def test_proposed_recommendation_not_eligible_409(tmp_path: Path) -> None:
    """T-TRIAL-GATE-1: PROPOSED -> 409, candidate unchanged, no Trial row."""
    url = f"sqlite:///{(tmp_path / 'g1.db').as_posix()}"
    with _db(url) as session:
        cand, match, head = _prepared(session)
        rec = recommend_candidate(session, cand.id)
        session.commit()
        assert rec.status == RecommendationStatus.PROPOSED

        with pytest.raises(ServiceError) as exc:
            create_trial_from_approval(session, rec.id, actor=OWNER)
        assert exc.value.status_code == 409

        session.refresh(cand)
        assert cand.status == CandidateStatus.RECOMMENDED
        assert session.exec(select(Trial)).all() == []


def test_rejected_recommendation_not_eligible_409(tmp_path: Path) -> None:
    """T-TRIAL-GATE-2: REJECTED -> 409."""
    url = f"sqlite:///{(tmp_path / 'g2.db').as_posix()}"
    with _db(url) as session:
        cand, match, head = _prepared(session)
        rec = recommend_candidate(session, cand.id)
        session.commit()
        decide_recommendation(session, rec.id, ApprovalStatus.REJECTED, actor=OWNER)
        session.commit()
        assert rec.status == RecommendationStatus.REJECTED

        with pytest.raises(ServiceError) as exc:
            create_trial_from_approval(session, rec.id, actor=OWNER)
        assert exc.value.status_code == 409
        assert session.exec(select(Trial)).all() == []


def test_withdrawn_recommendation_not_eligible_409(tmp_path: Path) -> None:
    """T-TRIAL-GATE-3: WITHDRAWN -> 409."""
    url = f"sqlite:///{(tmp_path / 'g3.db').as_posix()}"
    with _db(url) as session:
        rec, cand, match, head = _approved(session)
        rec.status = RecommendationStatus.WITHDRAWN
        session.add(rec)
        session.commit()

        with pytest.raises(ServiceError) as exc:
            create_trial_from_approval(session, rec.id, actor=OWNER)
        assert exc.value.status_code == 409


def test_approved_without_decider_not_eligible_409(tmp_path: Path) -> None:
    """T-TRIAL-GATE-4: APPROVED but decided_by is empty -> 409 (broken invariant)."""
    url = f"sqlite:///{(tmp_path / 'g4.db').as_posix()}"
    with _db(url) as session:
        rec, cand, match, head = _approved(session)
        rec.decided_by = None
        session.add(rec)
        session.commit()

        with pytest.raises(ServiceError) as exc:
            create_trial_from_approval(session, rec.id, actor=OWNER)
        assert exc.value.status_code == 409


def test_missing_recommendation_404(tmp_path: Path) -> None:
    """T-TRIAL-GATE-5: unknown recommendation_id -> 404."""
    url = f"sqlite:///{(tmp_path / 'g5.db').as_posix()}"
    with _db(url) as session:
        with pytest.raises(ServiceError) as exc:
            create_trial_from_approval(session, "rec_does_not_exist", actor=OWNER)
        assert exc.value.status_code == 404


def test_non_owner_actor_403(tmp_path: Path) -> None:
    """T-TRIAL-GATE-6: agent / system actor -> 403."""
    url = f"sqlite:///{(tmp_path / 'g6.db').as_posix()}"
    with _db(url) as session:
        rec, cand, match, head = _approved(session)
        for actor in (AGENT, SYSTEM):
            with pytest.raises(ServiceError) as exc:
                create_trial_from_approval(session, rec.id, actor=actor)
            assert exc.value.status_code == 403


def test_missing_actor_raises_typeerror(tmp_path: Path) -> None:
    """T-TRIAL-GATE-6b: omitting the keyword-only actor -> TypeError (P2-1)."""
    url = f"sqlite:///{(tmp_path / 'g6b.db').as_posix()}"
    with _db(url) as session, pytest.raises(TypeError):
        create_trial_from_approval(session, "whatever")  # type: ignore[call-arg]


def test_drift_triggers_lazy_withdraw_then_409(tmp_path: Path) -> None:
    """T-TRIAL-GATE-7: drifted evidence -> lazy F-R8 withdraw -> 409 + audit."""
    url = f"sqlite:///{(tmp_path / 'g7.db').as_posix()}"
    with _db(url) as session:
        rec, cand, match, head = _approved(session)
        # Mutate the underlying Match so its attempt no longer matches the rec.
        m = session.get(Match, rec.match_id)
        assert m is not None
        m.evidence_refs = ["cand:none:attempt:99"]
        session.add(m)
        session.commit()

        with pytest.raises(ServiceError) as exc:
            create_trial_from_approval(session, rec.id, actor=OWNER)
        assert exc.value.status_code == 409

        session.refresh(rec)
        assert rec.status == RecommendationStatus.WITHDRAWN
        assert len(_audits(session, "recommendation.withdrawn")) >= 1
        # The lazy reconcile released the candidate (RECOMMENDED -> EVALUATED).
        session.refresh(cand)
        assert cand.status == CandidateStatus.EVALUATED


# ---------------------------------------------------------------------------
# T-TRIAL-STATE -- controlled state transitions (8-11)
# ---------------------------------------------------------------------------


def test_successful_trial_advances_candidate(tmp_path: Path) -> None:
    """T-TRIAL-STATE-8: RECOMMENDED -> TRIALING, exactly one Trial + one audit."""
    url = f"sqlite:///{(tmp_path / 's8.db').as_posix()}"
    with _db(url) as session:
        rec, cand, match, head = _approved(session)
        trial = create_trial_from_approval(session, rec.id, actor=OWNER)
        session.commit()

        assert trial.status == TrialStatus.PROPOSED
        session.refresh(cand)
        assert cand.status == CandidateStatus.TRIALING
        assert len(session.exec(select(Trial)).all()) == 1

        audits = _audits(session, "trial.created")
        assert len(audits) == 1
        assert audits[0].resource_id == trial.id
        assert audits[0].actor == OWNER.owner_id


def test_pooled_to_trialing_illegal(tmp_path: Path) -> None:
    """T-TRIAL-STATE-9: POOLED -> TRIALING shortcut is 409."""
    with pytest.raises(ServiceError) as exc:
        CandidateLifecycle.require_transition(
            CandidateStatus.POOLED, CandidateStatus.TRIALING
        )
    assert exc.value.status_code == 409
    assert not CandidateLifecycle.can_transition(
        CandidateStatus.POOLED, CandidateStatus.TRIALING
    )


def test_evaluated_to_trialing_illegal(tmp_path: Path) -> None:
    """T-TRIAL-STATE-10: EVALUATED -> TRIALING shortcut is 409."""
    with pytest.raises(ServiceError) as exc:
        CandidateLifecycle.require_transition(
            CandidateStatus.EVALUATED, CandidateStatus.TRIALING
        )
    assert exc.value.status_code == 409


def test_trialing_has_no_outbound_edge(tmp_path: Path) -> None:
    """T-TRIAL-STATE-11 (W4-superseded): TRIALING keeps only its two W4 edges.

    W3-D froze TRIALING with an empty outbound set; W4 (R7 D-2/D-3) opened
    exactly TRIALING -> EMPLOYED (promote_to_employee) and TRIALING -> POOLED
    (release_candidate). Every other target remains closed (409).
    """
    assert CandidateLifecycle.ALLOWED[CandidateStatus.TRIALING] == {
        CandidateStatus.EMPLOYED,
        CandidateStatus.POOLED,
    }
    for closed in (
        CandidateStatus.EVALUATED,
        CandidateStatus.REJECTED,
        CandidateStatus.RECOMMENDED,
    ):
        with pytest.raises(ServiceError) as exc:
            CandidateLifecycle.require_transition(CandidateStatus.TRIALING, closed)
        assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# T-TRIAL-IDEM -- idempotency & concurrency (12-15)
# ---------------------------------------------------------------------------


def test_idempotent_replay_returns_same_row(tmp_path: Path) -> None:
    """T-TRIAL-IDEM-12: second call returns the same row, 1 Trial, 1 audit."""
    url = f"sqlite:///{(tmp_path / 'i12.db').as_posix()}"
    with _db(url) as session:
        rec, cand, match, head = _approved(session)
        t1 = create_trial_from_approval(session, rec.id, actor=OWNER)
        session.commit()
        t2 = create_trial_from_approval(session, rec.id, actor=OWNER)
        session.commit()

        assert t1.id == t2.id
        assert len(session.exec(select(Trial)).all()) == 1
        assert len(_audits(session, "trial.created")) == 1


def test_concurrent_first_create_returns_winner(tmp_path: Path) -> None:
    """T-TRIAL-IDEM-13: data-layer injection of the UNIQUE slot -> returns winner.

    Deterministic stand-in for a concurrent first-create: a Trial is inserted
    directly to occupy ``UNIQUE(recommendation_id)``, then the service call must
    replay it (no new row, no second audit, no 500).
    """
    url = f"sqlite:///{(tmp_path / 'i13.db').as_posix()}"
    with _db(url) as session:
        rec, cand, match, head = _approved(session)
        pre = Trial(
            candidate_id=rec.candidate_id,
            job_version_id=rec.job_version_id,
            recommendation_id=rec.id,
        )
        session.add(pre)
        session.commit()

        trial = create_trial_from_approval(session, rec.id, actor=OWNER)
        session.commit()

        assert trial.id == pre.id  # returned the pre-existing winner
        assert len(session.exec(select(Trial)).all()) == 1
        assert len(_audits(session, "trial.created")) == 0  # no second audit


def test_rebuild_same_recommendation_replays_single_trial(tmp_path: Path) -> None:
    """T-TRIAL-IDEM-14: rec withdrawn + rebuilt in-place (same id) + re-approved.

    The ``UNIQUE(recommendation_id)`` slot is unchanged, so the existing Trial is
    returned on replay -- no second Trial.

    Note: we model the "withdraw -> rebuild in-place -> re-approve" cycle with
    direct field sets rather than re-calling ``decide_recommendation``, because
    that function's audit idempotency key is ``rec:{id}:decision:{value}`` and is
    therefore not idempotent across two decisions of the same row (re-deciding the
    same rec would collide on ``audit_log.idempotency_key``). The point under test
    is purely that the Trial's anchor (``recommendation_id``) is stable, so the
    create replays. (The candidate stays TRIALING throughout -- the known C-5
    boundary -- which is exactly why the rebuild cannot go through the rec API.)
    """
    url = f"sqlite:///{(tmp_path / 'i14.db').as_posix()}"
    with _db(url) as session:
        rec, cand, match, head = _approved(session)
        t = create_trial_from_approval(session, rec.id, actor=OWNER)
        session.commit()
        assert len(session.exec(select(Trial)).all()) == 1

        # Withdraw, then rebuild in-place (same id) + re-approve.
        rec.status = RecommendationStatus.WITHDRAWN
        session.add(rec)
        session.commit()
        rec.status = RecommendationStatus.APPROVED
        rec.decided_by = OWNER.owner_id
        rec.decided_at = None
        session.add(rec)
        session.commit()

        t2 = create_trial_from_approval(session, rec.id, actor=OWNER)
        session.commit()
        assert t2.id == t.id
        assert len(session.exec(select(Trial)).all()) == 1


def test_idempotency_key_unique_not_duplicated(tmp_path: Path) -> None:
    """T-TRIAL-IDEM-15: the trial.created idempotency key is written exactly once."""
    url = f"sqlite:///{(tmp_path / 'i15.db').as_posix()}"
    with _db(url) as session:
        rec, cand, match, head = _approved(session)
        create_trial_from_approval(session, rec.id, actor=OWNER)
        session.commit()
        create_trial_from_approval(session, rec.id, actor=OWNER)
        session.commit()

        rows = session.exec(
            select(AuditLog).where(
                AuditLog.idempotency_key == f"trial:{rec.id}"
            )
        ).all()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# T-TRIAL-FK -- RESTRICT enforcement (16-18)
# ---------------------------------------------------------------------------


def test_delete_recommendation_with_trial_refused(tmp_path: Path) -> None:
    """T-TRIAL-FK-16: deleting the backing Recommendation is refused (RESTRICT)."""
    url = f"sqlite:///{(tmp_path / 'fk16.db').as_posix()}"
    with _db(url) as session:
        rec, cand, match, head = _approved(session)
        create_trial_from_approval(session, rec.id, actor=OWNER)
        session.commit()

        session.delete(rec)
        with pytest.raises(IntegrityError):
            session.commit()


def test_delete_candidate_and_job_version_with_trial_refused(tmp_path: Path) -> None:
    """T-TRIAL-FK-17: deleting candidate / job_version is refused (RESTRICT)."""
    url = f"sqlite:///{(tmp_path / 'fk17a.db').as_posix()}"
    with _db(url) as session:
        rec, cand, match, head = _approved(session)
        create_trial_from_approval(session, rec.id, actor=OWNER)
        session.commit()
        session.delete(cand)
        with pytest.raises(IntegrityError):
            session.commit()

    url2 = f"sqlite:///{(tmp_path / 'fk17b.db').as_posix()}"
    with _db(url2) as session:
        rec, cand, match, head = _approved(session)
        create_trial_from_approval(session, rec.id, actor=OWNER)
        session.commit()
        jv = session.get(JobVersion, rec.job_version_id)
        assert jv is not None
        session.delete(jv)
        with pytest.raises(IntegrityError):
            session.commit()


def test_migration_foreign_keys_are_restrict(tmp_path: Path) -> None:
    """T-TRIAL-FK-18: all three trial FKs declare ondelete=RESTRICT."""
    url = f"sqlite:///{(tmp_path / 'fk18.db').as_posix()}"
    run_migrations(url)
    engine = get_engine(url)
    fks = inspect(engine).get_foreign_keys("trial")
    assert len(fks) == 3
    for fk in fks:
        assert fk.get("options", {}).get("ondelete") == "RESTRICT", fk


# ---------------------------------------------------------------------------
# T-TRIAL-AUDIT -- audit & evidence (19-21)
# ---------------------------------------------------------------------------


def test_trial_audit_before_contains_decided_by(tmp_path: Path) -> None:
    """T-TRIAL-AUDIT-19: the decision snapshot enters the evidence chain."""
    url = f"sqlite:///{(tmp_path / 'a19.db').as_posix()}"
    with _db(url) as session:
        rec, cand, match, head = _approved(session)
        create_trial_from_approval(session, rec.id, actor=OWNER)
        session.commit()

        audit = _audits(session, "trial.created")[0]
        assert "decided_by" in audit.before_snapshot
        assert audit.before_snapshot["decided_by"] == rec.decided_by


def test_audit_failure_rolls_back_no_orphan_trial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-TRIAL-AUDIT-20: audit failure inside the SAVEPOINT leaves no Trial."""
    url = f"sqlite:///{(tmp_path / 'a20.db').as_posix()}"
    with _db(url) as session:
        rec, cand, match, head = _approved(session)

        def _boom(*_a: object, **_k: object) -> object:
            raise RuntimeError("audit boom")

        # Patch the module-level append_audit referenced inside create_trial.
        import aios.workforce_trial as wt

        monkeypatch.setattr(wt, "append_audit", _boom)

        with pytest.raises(RuntimeError):
            create_trial_from_approval(session, rec.id, actor=OWNER)

        # Nothing committed: a fresh session sees zero Trials / zero trial audits.
        with Session(get_engine(url)) as verify:
            assert verify.exec(select(Trial)).all() == []
            assert (
                verify.exec(
                    select(AuditLog).where(AuditLog.action == "trial.created")
                ).all()
                == []
            )


def test_replay_produces_no_second_audit(tmp_path: Path) -> None:
    """T-TRIAL-AUDIT-21: replay writes no second ``trial.created`` audit."""
    url = f"sqlite:///{(tmp_path / 'a21.db').as_posix()}"
    with _db(url) as session:
        rec, cand, match, head = _approved(session)
        create_trial_from_approval(session, rec.id, actor=OWNER)
        session.commit()
        before = len(_audits(session, "trial.created"))

        create_trial_from_approval(session, rec.id, actor=OWNER)
        session.commit()
        after = len(_audits(session, "trial.created"))

        assert before == after == 1


# ---------------------------------------------------------------------------
# T-TRIAL-BOUNDARY -- no over-reach (22-26)
# ---------------------------------------------------------------------------


def test_no_employee_table_or_column() -> None:
    """T-TRIAL-BOUNDARY-22 (W4-superseded): Employee exists but W3-D owns none.

    W3-D introduced no Employee entity; W4 (R7 D-3) added it and owns the only
    creator. The W3-D boundary that survives: ``workforce_trial.py`` must not
    reference or create an Employee.
    """
    assert "class Employee" in (ROOT / "src" / "aios" / "models.py").read_text()
    trial_src = (ROOT / "src" / "aios" / "workforce_trial.py").read_text()
    assert "Employee(" not in trial_src
    assert "workforce_employee" not in trial_src


def test_no_budget_scheduler_execution_calls() -> None:
    """T-TRIAL-BOUNDARY-23: no Budget / Scheduler / Execution dependency."""
    src = (ROOT / "src" / "aios" / "workforce_trial.py").read_text()
    for token in (
        "check_budget",
        "create_task",
        "execute_task",
        "Budget",
        "Scheduler",
        "Execution",
    ):
        assert token not in src, f"forbidden token present: {token}"


def test_recommendation_module_definitions_unchanged() -> None:
    """T-TRIAL-BOUNDARY-24: W3-C module is not modified with W3-D logic."""
    src = (ROOT / "src" / "aios" / "workforce_recommendation.py").read_text()
    assert "def create_trial_from_approval" not in src
    # The gate W3-D depends on is still defined, intact.
    assert "def assert_trial_eligible" in src


def test_lifecycle_edges_added_and_trialing_member() -> None:
    """T-TRIAL-BOUNDARY-25 (W4-superseded): ALLOWED edges after the W4 handover.

    W3-D added the TRIALING member with an empty edge set; W4 (R7 D-2/D-3)
    opened exactly two outbound edges. RECOMMENDED and EVALUATING edges are
    unchanged from W3-A/C.
    """
    assert CandidateStatus.TRIALING in CandidateLifecycle.ALLOWED
    assert CandidateLifecycle.ALLOWED[CandidateStatus.RECOMMENDED] == {
        CandidateStatus.EVALUATED,
        CandidateStatus.TRIALING,
    }
    assert CandidateLifecycle.ALLOWED[CandidateStatus.TRIALING] == {
        CandidateStatus.EMPLOYED,
        CandidateStatus.POOLED,
    }
    assert CandidateLifecycle.ALLOWED[CandidateStatus.EMPLOYED] == set()


def test_no_trial_deletion_path() -> None:
    """T-TRIAL-BOUNDARY-26: no purge_trial / session.delete(trial) (F-T7)."""
    src = (ROOT / "src" / "aios" / "workforce_trial.py").read_text()
    assert "def purge_trial" not in src
    assert "session.delete(" not in src


# ---------------------------------------------------------------------------
# T-TRIAL-MIG -- migration (27-29)
# ---------------------------------------------------------------------------


def test_single_alembic_head() -> None:
    """T-TRIAL-MIG-27: alembic single head == the new W3-D revision."""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["20260904_0001_workforce_cost_evidence"]


def test_trial_table_shape_and_zero_explicit_indexes(tmp_path: Path) -> None:
    """T-TRIAL-MIG-28: columns/types/constraints match the model; 0 explicit idx."""
    url = f"sqlite:///{(tmp_path / 'm28.db').as_posix()}"
    run_migrations(url)
    engine = get_engine(url)
    inspector = inspect(engine)

    cols = {c["name"]: c for c in inspector.get_columns("trial")}
    for name in (
        "id",
        "candidate_id",
        "job_version_id",
        "recommendation_id",
        "status",
        "created_at",
        "updated_at",
    ):
        assert name in cols, f"missing column: {name}"
    assert isinstance(cols["created_at"]["type"], SADateTime)
    assert isinstance(cols["id"]["type"], SAString)

    # 0 explicitly-named (ix_) indexes; the UNIQUE constraint ships an implicit one.
    indexes = inspector.get_indexes("trial")
    assert [i for i in indexes if (i["name"] or "").startswith("ix_")] == []

    uniques = inspector.get_unique_constraints("trial")
    assert any(u["name"] == "uq_trial_recommendation" for u in uniques)


def test_downgrade_removes_trial(tmp_path: Path) -> None:
    """T-TRIAL-MIG-29: downgrade returns to W3-C head, trial table gone."""
    url = f"sqlite:///{(tmp_path / 'm29.db').as_posix()}"
    run_migrations(url)
    assert inspect(get_engine(url)).has_table("trial")

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.downgrade(config, "20260902_0001_workforce_recommendation")

    assert not inspect(get_engine(url)).has_table("trial")


# ---------------------------------------------------------------------------
# T-TRIAL-CPL -- coexists with existing suite (30)
# ---------------------------------------------------------------------------


def test_illegal_edges_still_rejected_after_w3d() -> None:
    """T-TRIAL-CPL-30: W3-D did not open any illegal shortcut edge."""
    # Shortcuts that must stay illegal.
    assert not CandidateLifecycle.can_transition(
        CandidateStatus.POOLED, CandidateStatus.RECOMMENDED
    )
    assert not CandidateLifecycle.can_transition(
        CandidateStatus.POOLED, CandidateStatus.TRIALING
    )
    assert not CandidateLifecycle.can_transition(
        CandidateStatus.EVALUATED, CandidateStatus.TRIALING
    )
    # Legal edges preserved.
    assert CandidateLifecycle.can_transition(
        CandidateStatus.EVALUATED, CandidateStatus.RECOMMENDED
    )
    assert CandidateLifecycle.can_transition(
        CandidateStatus.RECOMMENDED, CandidateStatus.EVALUATED
    )
    assert CandidateLifecycle.can_transition(
        CandidateStatus.RECOMMENDED, CandidateStatus.TRIALING
    )
