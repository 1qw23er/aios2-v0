"""Contract tests for Workforce W4 -- Employee appointment (Trial lifecycle + hire).

Scope (see ``docs/workforce/Workforce_W4_Employee_Spec_V1.md``):

* the ONLY creator of an ``Employee`` is ``promote_to_employee``, and only from a
  COMPLETED Trial (INV-E1);
* the Trial has an explicit state machine (``TrialLifecycle`` + the single writer
  ``_transition_trial_status``), with PROPOSED -> ACTIVE -> COMPLETED | FAILED
  and PROPOSED | ACTIVE -> CANCELLED;
* the two-stage human gate (D-6): ``complete_trial`` records a verdict and NEVER
  creates an Employee; ``promote_to_employee`` records the owner's hiring
  decision and is a separate, explicit owner call;
* every service function is owner-only, keyword-only, with no default actor
  (Q7): a missing actor raises ``TypeError``, a non-owner actor raises 403;
* failure / cancellation releases the candidate back to POOLED (D-2); no new
  Candidate terminal state is introduced;
* D-1 / D-3 / D-4 are deliberately absent: no cost gate, no ``JobStatus.FILLED``
  write, no Employee delete path.

Helpers mirror ``tests/test_workforce_trial_w3d.py`` so this file is
self-contained.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
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
    Employee,
    EmployeeStatus,
    Job,
    JobStatus,
    JobVersion,
    Match,
    Recommendation,
    Trial,
    TrialOutcome,
    TrialStatus,
)
from aios.services import ServiceError
from aios.workforce import (
    CandidateLifecycle,
    compute_match,
    create_business_goal,
    create_job,
    create_required_work,
    discover_candidates,
    evaluate_candidate,
    list_capability_requirements,
)
from aios.workforce_employee import (
    TrialLifecycle,
    activate_trial,
    cancel_trial,
    complete_trial,
    promote_to_employee,
    release_candidate,
)
from aios.workforce_recommendation import (
    decide_recommendation,
    recommend_candidate,
)
from aios.workforce_trial import create_trial_from_approval

ROOT = Path(__file__).resolve().parents[1]

# Carried by the caller, never defaulted by the service (P2-1 / Q7).
OWNER = ActorContext(kind="owner", owner_id="owner")
AGENT = ActorContext(kind="agent", agent_id="agent-1")
SYSTEM = ActorContext.system()


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors test_workforce_trial_w3d.py)
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


def _build_chain(
    session: Session, *specs: tuple[str, int, bool]
) -> tuple[Job, JobVersion]:
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
        r.capability_name: r for r in list_capability_requirements(session, head.id)
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
        session.exec(select(AuditLog).where(AuditLog.action == action)).all()
    )


def _prepared(
    session: Session,
    *,
    cap_name: str = "writing",
    min_prof: int = 50,
    priority: int = 80,
) -> tuple[Candidate, Match, JobVersion]:
    """An EVALUATED candidate carrying a COMPUTED Match (the W3-C input state)."""
    _seed_capability(session, cap_name)
    _seed_agent(session, "A", {cap_name: priority})
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


def _trial(
    session: Session,
    *,
    cap_name: str = "writing",
    min_prof: int = 50,
    priority: int = 80,
) -> tuple[Trial, Candidate, Recommendation, JobVersion]:
    """A PROPOSED Trial on a TRIALING candidate (the W4 input state)."""
    rec, cand, match, head = _approved(
        session, cap_name=cap_name, min_prof=min_prof, priority=priority
    )
    trial = create_trial_from_approval(session, rec.id, actor=OWNER)
    session.commit()
    return trial, cand, rec, head


# ---------------------------------------------------------------------------
# F-E1 -- owner-only gate (1-5)
# ---------------------------------------------------------------------------


def test_activate_rejects_agent_and_system_actor_403(tmp_path: Path) -> None:
    """F-E1: activate_trial refuses agent / system actors with 403."""
    url = f"sqlite:///{(tmp_path / 'g1.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        for actor in (AGENT, SYSTEM):
            with pytest.raises(ServiceError) as exc:
                activate_trial(session, trial.id, actor=actor)
            assert exc.value.status_code == 403


def test_complete_rejects_non_owner_403(tmp_path: Path) -> None:
    """F-E1: complete_trial refuses a non-owner actor with 403."""
    url = f"sqlite:///{(tmp_path / 'g2.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        with pytest.raises(ServiceError) as exc:
            complete_trial(session, trial.id, outcome=TrialOutcome.PASS, actor=AGENT)
        assert exc.value.status_code == 403


def test_promote_rejects_non_owner_403(tmp_path: Path) -> None:
    """F-E1: promote_to_employee refuses a non-owner actor with 403."""
    url = f"sqlite:///{(tmp_path / 'g3.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        complete_trial(session, trial.id, outcome=TrialOutcome.PASS, actor=OWNER)
        session.commit()
        with pytest.raises(ServiceError) as exc:
            promote_to_employee(session, trial.id, actor=SYSTEM)
        assert exc.value.status_code == 403


def test_release_rejects_non_owner_403(tmp_path: Path) -> None:
    """F-E1: release_candidate refuses a non-owner actor with 403."""
    url = f"sqlite:///{(tmp_path / 'g4.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        complete_trial(session, trial.id, outcome=TrialOutcome.FAIL, actor=OWNER)
        session.commit()
        with pytest.raises(ServiceError) as exc:
            release_candidate(session, trial.id, actor=AGENT)
        assert exc.value.status_code == 403


def test_missing_actor_raises_typeerror(tmp_path: Path) -> None:
    """F-E1 (P2-1): omitting the keyword-only actor -> TypeError."""
    url = f"sqlite:///{(tmp_path / 'g5.db').as_posix()}"
    with _db(url) as session, pytest.raises(TypeError):
        activate_trial(session, "whatever")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# F-E2..F-E5 -- activate (6-8)
# ---------------------------------------------------------------------------


def test_activate_moves_proposed_to_active_and_writes_payload(tmp_path: Path) -> None:
    """F-E2..F-E5: PROPOSED -> ACTIVE, writes plan_ref + started_at, one audit."""
    url = f"sqlite:///{(tmp_path / 'a1.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        assert trial.status == TrialStatus.PROPOSED
        out = activate_trial(session, trial.id, plan_ref="plan://x", actor=OWNER)
        session.commit()

        assert out.status == TrialStatus.ACTIVE
        session.refresh(out)
        assert out.trial_plan_ref == "plan://x"
        assert out.started_at is not None
        assert out.ended_at is None
        assert out.outcome is None
        audits = _audits(session, "trial.activated")
        assert len(audits) == 1
        assert audits[0].actor == OWNER.owner_id
        assert audits[0].resource_id == trial.id


def test_activate_replay_on_active_raises_409(tmp_path: Path) -> None:
    """F-E3: re-activating an ACTIVE trial is refused (no timestamp rewrite)."""
    url = f"sqlite:///{(tmp_path / 'a2.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        session.commit()
        with pytest.raises(ServiceError) as exc:
            activate_trial(session, trial.id, actor=OWNER)
        assert exc.value.status_code == 409


def test_activate_missing_trial_404(tmp_path: Path) -> None:
    """F-E2: unknown trial_id -> 404."""
    url = f"sqlite:///{(tmp_path / 'a3.db').as_posix()}"
    with _db(url) as session:
        with pytest.raises(ServiceError) as exc:
            activate_trial(session, "trial_missing", actor=OWNER)
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# F-E6..F-E10 -- complete (9-13)
# ---------------------------------------------------------------------------


def test_complete_pass_sets_completed_and_outcome(tmp_path: Path) -> None:
    """F-E6/F-E9/F-E10: ACTIVE + pass -> COMPLETED, outcome=pass, ended_at set."""
    url = f"sqlite:///{(tmp_path / 'c1.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        session.commit()
        out = complete_trial(session, trial.id, outcome=TrialOutcome.PASS, actor=OWNER)
        session.commit()

        assert out.status == TrialStatus.COMPLETED
        session.refresh(out)
        assert out.outcome == TrialOutcome.PASS
        assert out.ended_at is not None
        audits = _audits(session, "trial.completed")
        assert len(audits) == 1


def test_complete_fail_sets_failed_and_outcome(tmp_path: Path) -> None:
    """F-E6/F-E9: ACTIVE + fail -> FAILED, outcome=fail."""
    url = f"sqlite:///{(tmp_path / 'c2.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        session.commit()
        out = complete_trial(session, trial.id, outcome=TrialOutcome.FAIL, actor=OWNER)
        session.commit()
        assert out.status == TrialStatus.FAILED
        session.refresh(out)
        assert out.outcome == TrialOutcome.FAIL


def test_complete_before_activation_raises_409(tmp_path: Path) -> None:
    """F-E7: a PROPOSED (never activated) trial cannot be completed."""
    url = f"sqlite:///{(tmp_path / 'c3.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        with pytest.raises(ServiceError) as exc:
            complete_trial(session, trial.id, outcome=TrialOutcome.PASS, actor=OWNER)
        assert exc.value.status_code == 409


def test_complete_invalid_outcome_422(tmp_path: Path) -> None:
    """F-E8: a non-binary outcome is refused with 422."""
    url = f"sqlite:///{(tmp_path / 'c4.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        session.commit()
        with pytest.raises(ServiceError) as exc:
            complete_trial(session, trial.id, outcome="maybe", actor=OWNER)  # type: ignore[arg-type]
        assert exc.value.status_code == 422


def test_complete_terminal_trial_raises_409(tmp_path: Path) -> None:
    """F-E7: completing an already COMPLETED trial is refused."""
    url = f"sqlite:///{(tmp_path / 'c5.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        complete_trial(session, trial.id, outcome=TrialOutcome.PASS, actor=OWNER)
        session.commit()
        with pytest.raises(ServiceError) as exc:
            complete_trial(session, trial.id, outcome=TrialOutcome.PASS, actor=OWNER)
        assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# F-E11..F-E14 -- cancel (14-16)
# ---------------------------------------------------------------------------


def test_cancel_proposed_clears_outcome_and_sets_cancelled(tmp_path: Path) -> None:
    """F-E11/F-E13: PROPOSED -> CANCELLED, outcome stays None."""
    url = f"sqlite:///{(tmp_path / 'x1.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        out = cancel_trial(session, trial.id, actor=OWNER)
        session.commit()
        assert out.status == TrialStatus.CANCELLED
        session.refresh(out)
        assert out.outcome is None
        assert out.ended_at is not None


def test_cancel_active_sets_cancelled(tmp_path: Path) -> None:
    """F-E12: ACTIVE -> CANCELLED."""
    url = f"sqlite:///{(tmp_path / 'x2.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        session.commit()
        out = cancel_trial(session, trial.id, actor=OWNER)
        session.commit()
        assert out.status == TrialStatus.CANCELLED


def test_cancel_terminal_trial_raises_409(tmp_path: Path) -> None:
    """F-E12: cancelling a COMPLETED trial is refused (terminal)."""
    url = f"sqlite:///{(tmp_path / 'x3.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        complete_trial(session, trial.id, outcome=TrialOutcome.PASS, actor=OWNER)
        session.commit()
        with pytest.raises(ServiceError) as exc:
            cancel_trial(session, trial.id, actor=OWNER)
        assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# F-E15..F-E20 -- promote_to_employee (17-22)
# ---------------------------------------------------------------------------


def test_promote_completed_creates_employee_and_employed_candidate(
    tmp_path: Path,
) -> None:
    """F-E15..F-E18: COMPLETED + TRIALING -> Employee + candidate EMPLOYED (one
    SAVEPOINT)."""
    url = f"sqlite:///{(tmp_path / 'p1.db').as_posix()}"
    with _db(url) as session:
        trial, cand, _, head = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        complete_trial(session, trial.id, outcome=TrialOutcome.PASS, actor=OWNER)
        session.commit()

        emp = promote_to_employee(session, trial.id, actor=OWNER)
        session.commit()

        assert isinstance(emp, Employee)
        assert emp.status == EmployeeStatus.ACTIVE
        # No orphan: candidate moved atomically with the Employee row.
        session.refresh(cand)
        assert cand.status == CandidateStatus.EMPLOYED
        emps = session.exec(select(Employee)).all()
        assert len(emps) == 1
        audits = _audits(session, "employee.hired")
        assert len(audits) == 1
        assert audits[0].resource_id == emp.id


def test_promote_snapshots_candidate_context(tmp_path: Path) -> None:
    """F-E19: Employee copies agent_id / job_id / job_version_id from Candidate."""
    url = f"sqlite:///{(tmp_path / 'p2.db').as_posix()}"
    with _db(url) as session:
        trial, cand, _, head = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        complete_trial(session, trial.id, outcome=TrialOutcome.PASS, actor=OWNER)
        session.commit()

        emp = promote_to_employee(session, trial.id, actor=OWNER)
        session.commit()
        assert emp.agent_id == cand.agent_id
        assert emp.job_id == cand.job_id
        assert emp.job_version_id == cand.job_version_id
        assert emp.trial_id == trial.id
        assert emp.candidate_id == cand.id


def test_promote_active_trial_raises_409(tmp_path: Path) -> None:
    """F-E16/INV-E1: an ACTIVE (not completed) trial cannot be promoted."""
    url = f"sqlite:///{(tmp_path / 'p3.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        session.commit()
        with pytest.raises(ServiceError) as exc:
            promote_to_employee(session, trial.id, actor=OWNER)
        assert exc.value.status_code == 409
        assert session.exec(select(Employee)).all() == []


def test_promote_failed_trial_raises_409(tmp_path: Path) -> None:
    """F-E16/INV-E1: a FAILED trial cannot be promoted."""
    url = f"sqlite:///{(tmp_path / 'p4.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        complete_trial(session, trial.id, outcome=TrialOutcome.FAIL, actor=OWNER)
        session.commit()
        with pytest.raises(ServiceError) as exc:
            promote_to_employee(session, trial.id, actor=OWNER)
        assert exc.value.status_code == 409


def test_promote_cancelled_trial_raises_409(tmp_path: Path) -> None:
    """F-E16/INV-E1: a CANCELLED trial cannot be promoted."""
    url = f"sqlite:///{(tmp_path / 'p5.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        cancel_trial(session, trial.id, actor=OWNER)
        session.commit()
        with pytest.raises(ServiceError) as exc:
            promote_to_employee(session, trial.id, actor=OWNER)
        assert exc.value.status_code == 409


def test_promote_non_trialing_candidate_raises_409(tmp_path: Path) -> None:
    """F-E17: a COMPLETED trial whose candidate is no longer TRIALING is refused."""
    url = f"sqlite:///{(tmp_path / 'p6.db').as_posix()}"
    with _db(url) as session:
        trial, cand, _, head = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        complete_trial(session, trial.id, outcome=TrialOutcome.PASS, actor=OWNER)
        # Simulate an inconsistent state: candidate drifted out of TRIALING.
        cand.status = CandidateStatus.POOLED
        session.add(cand)
        session.commit()
        with pytest.raises(ServiceError) as exc:
            promote_to_employee(session, trial.id, actor=OWNER)
        assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# F-E21 -- idempotency (23-24)
# ---------------------------------------------------------------------------


def test_promote_idempotent_returns_same_employee(tmp_path: Path) -> None:
    """F-E21: a replay returns the existing row, writes no second Employee/audit."""
    url = f"sqlite:///{(tmp_path / 'i1.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        complete_trial(session, trial.id, outcome=TrialOutcome.PASS, actor=OWNER)
        session.commit()

        e1 = promote_to_employee(session, trial.id, actor=OWNER)
        session.commit()
        e2 = promote_to_employee(session, trial.id, actor=OWNER)
        session.commit()

        assert e1.id == e2.id
        assert len(session.exec(select(Employee)).all()) == 1
        assert len(_audits(session, "employee.hired")) == 1


def test_promote_pre_existing_employee_returned(tmp_path: Path) -> None:
    """F-E21: a pre-occupied UNIQUE(trial_id) slot yields the existing winner."""
    url = f"sqlite:///{(tmp_path / 'i2.db').as_posix()}"
    with _db(url) as session:
        trial, cand, _, head = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        complete_trial(session, trial.id, outcome=TrialOutcome.PASS, actor=OWNER)
        session.commit()
        # Inject an Employee directly (simulating a concurrent first-promote).
        pre = Employee(
            candidate_id=cand.id,
            trial_id=trial.id,
            agent_id=cand.agent_id,
            job_id=cand.job_id,
            job_version_id=cand.job_version_id,
        )
        session.add(pre)
        session.commit()

        emp = promote_to_employee(session, trial.id, actor=OWNER)
        session.commit()
        assert emp.id == pre.id
        assert len(session.exec(select(Employee)).all()) == 1


# ---------------------------------------------------------------------------
# F-E22..F-E26 -- release_candidate (D-2) (25-27)
# ---------------------------------------------------------------------------


def test_release_failed_candidate_returns_to_pooled(tmp_path: Path) -> None:
    """F-E22/F-E24/F-E25: FAILED + TRIALING -> candidate POOLED, one audit."""
    url = f"sqlite:///{(tmp_path / 'r1.db').as_posix()}"
    with _db(url) as session:
        trial, cand, _, head = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        complete_trial(session, trial.id, outcome=TrialOutcome.FAIL, actor=OWNER)
        session.commit()

        out = release_candidate(session, trial.id, actor=OWNER)
        session.commit()

        session.refresh(out)
        assert out.status == CandidateStatus.POOLED
        audits = _audits(session, "candidate.released")
        assert len(audits) == 1


def test_release_cancelled_candidate_returns_to_pooled(tmp_path: Path) -> None:
    """F-E23/F-E25: CANCELLED + TRIALING -> candidate POOLED."""
    url = f"sqlite:///{(tmp_path / 'r2.db').as_posix()}"
    with _db(url) as session:
        trial, cand, _, head = _trial(session)
        cancel_trial(session, trial.id, actor=OWNER)
        session.commit()
        out = release_candidate(session, trial.id, actor=OWNER)
        session.commit()
        session.refresh(out)
        assert out.status == CandidateStatus.POOLED


def test_release_completed_trial_raises_409(tmp_path: Path) -> None:
    """F-E23: a COMPLETED trial is NOT releasable (must go through promote)."""
    url = f"sqlite:///{(tmp_path / 'r3.db').as_posix()}"
    with _db(url) as session:
        trial, *_ = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        complete_trial(session, trial.id, outcome=TrialOutcome.PASS, actor=OWNER)
        session.commit()
        with pytest.raises(ServiceError) as exc:
            release_candidate(session, trial.id, actor=OWNER)
        assert exc.value.status_code == 409


def test_release_non_trialing_candidate_raises_409(tmp_path: Path) -> None:
    """F-E24: a second release (candidate already POOLED) is refused."""
    url = f"sqlite:///{(tmp_path / 'r4.db').as_posix()}"
    with _db(url) as session:
        trial, cand, _, head = _trial(session)
        cancel_trial(session, trial.id, actor=OWNER)
        session.commit()
        release_candidate(session, trial.id, actor=OWNER)
        session.commit()
        # Candidate now POOLED -> a repeat release must be refused, not overwritten.
        with pytest.raises(ServiceError) as exc:
            release_candidate(session, trial.id, actor=OWNER)
        assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# D-6 -- two-stage decoupling
# ---------------------------------------------------------------------------


def test_complete_trial_does_not_create_employee(tmp_path: Path) -> None:
    """D-6: completing a trial records a verdict but leaves a TRIALING candidate
    and creates NO Employee."""
    url = f"sqlite:///{(tmp_path / 'd6.db').as_posix()}"
    with _db(url) as session:
        trial, cand, _, head = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        complete_trial(session, trial.id, outcome=TrialOutcome.PASS, actor=OWNER)
        session.commit()

        assert session.exec(select(Employee)).all() == []
        session.refresh(cand)
        assert cand.status == CandidateStatus.TRIALING
        # The verdict is recorded, but no hire.
        session.refresh(trial)
        assert trial.status == TrialStatus.COMPLETED


# ---------------------------------------------------------------------------
# D-3 -- no JobStatus.FILLED write
# ---------------------------------------------------------------------------


def test_promote_does_not_write_job_filled(tmp_path: Path) -> None:
    """D-3: promoting an Employee never touches JobStatus.FILLED."""
    url = f"sqlite:///{(tmp_path / 'd3.db').as_posix()}"
    with _db(url) as session:
        trial, cand, _, head = _trial(session)
        activate_trial(session, trial.id, actor=OWNER)
        complete_trial(session, trial.id, outcome=TrialOutcome.PASS, actor=OWNER)
        session.commit()
        promote_to_employee(session, trial.id, actor=OWNER)
        session.commit()

        job = session.get(Job, head.job_id)
        assert job is not None
        assert job.status != JobStatus.FILLED


# ---------------------------------------------------------------------------
# Single status writer / lifecycle invariants (INV-E3 / Q3)
# ---------------------------------------------------------------------------


def test_trial_lifecycle_rejects_illegal_transition() -> None:
    """INV-E3: only the declared edges exist; everything else is 409."""
    assert TrialLifecycle.can_transition(TrialStatus.PROPOSED, TrialStatus.ACTIVE)
    assert TrialLifecycle.can_transition(TrialStatus.PROPOSED, TrialStatus.CANCELLED)
    assert TrialLifecycle.can_transition(TrialStatus.ACTIVE, TrialStatus.COMPLETED)
    assert TrialLifecycle.can_transition(TrialStatus.ACTIVE, TrialStatus.FAILED)
    assert TrialLifecycle.can_transition(TrialStatus.ACTIVE, TrialStatus.CANCELLED)
    # Terminal states have no outbound edges.
    assert TrialLifecycle.ALLOWED[TrialStatus.COMPLETED] == set()
    assert TrialLifecycle.ALLOWED[TrialStatus.FAILED] == set()
    assert TrialLifecycle.ALLOWED[TrialStatus.CANCELLED] == set()
    # Illegal: PROPOSED -> COMPLETED, ACTIVE -> PROPOSED, COMPLETED -> ACTIVE.
    assert not TrialLifecycle.can_transition(
        TrialStatus.PROPOSED, TrialStatus.COMPLETED
    )
    assert not TrialLifecycle.can_transition(TrialStatus.ACTIVE, TrialStatus.PROPOSED)
    assert not TrialLifecycle.can_transition(
        TrialStatus.COMPLETED, TrialStatus.ACTIVE
    )
    with pytest.raises(ServiceError):
        TrialLifecycle.require_transition(
            TrialStatus.PROPOSED, TrialStatus.COMPLETED
        )


def test_candidate_employed_is_terminal_and_reachable_from_trialing() -> None:
    """W4 unfreeze: TRIALING -> {EMPLOYED, POOLED}; EMPLOYED is terminal."""
    assert CandidateStatus.EMPLOYED in CandidateLifecycle.ALLOWED[
        CandidateStatus.TRIALING
    ]
    assert CandidateStatus.POOLED in CandidateLifecycle.ALLOWED[
        CandidateStatus.TRIALING
    ]
    assert CandidateLifecycle.ALLOWED[CandidateStatus.EMPLOYED] == set()


# ---------------------------------------------------------------------------
# Migration / schema (single head, additive shape)
# ---------------------------------------------------------------------------


def test_employee_table_and_trial_columns_exist_after_migration(
    tmp_path: Path,
) -> None:
    """W4 migration: ``employee`` table + 4 new ``trial`` columns, both present."""
    url = f"sqlite:///{(tmp_path / 'mig.db').as_posix()}"
    engine = get_engine(url)
    run_migrations(url)
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert "employee" in tables
    assert "trial" in tables

    trial_cols = {c["name"] for c in insp.get_columns("trial")}
    for col in ("trial_plan_ref", "started_at", "ended_at", "outcome"):
        assert col in trial_cols

    emp_cols = {c["name"] for c in insp.get_columns("employee")}
    for col in (
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
    ):
        assert col in emp_cols


def test_alembic_single_head_is_w4_employee() -> None:
    """Single head advanced exactly to the W4 Employee migration."""
    cfg = Config(ROOT / "alembic.ini")
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == ["20260903_0002_workforce_employee"]
