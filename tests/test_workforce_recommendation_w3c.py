"""Contract tests for Workforce W3-C -- Recommendation + the L4 human gate.

Scope (see ``docs/Workforce_W3C_Recommendation_Approval_Spec_V4.md`` §13):

* the ``EVALUATED -> RECOMMENDED`` edge is opened *only* through the Match gate,
  and ``RECOMMENDED`` has exactly one outbound edge (back to ``EVALUATED``);
* every gate is fail-closed: an unexplainable / drifting / fabricated-dimension
  Match is refused rather than recommended;
* the recommendation is a **snapshot**: score, breakdown and evidence are copied,
  never recomputed;
* W3-C writes ``Candidate.evaluation_context`` zero times and never calls back
  into W3-A / W3-B / budget / execution (§15.2).

Helpers mirror ``tests/test_workforce_benchmark_match_w3b.py`` so this file is
self-contained.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
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
    Job,
    JobVersion,
    Match,
    MatchStatus,
    Recommendation,
    RecommendationStatus,
    RiskLevel,
)
from aios.services import ServiceError
from aios.workforce import (
    CandidateLifecycle,
    compute_match,
    discover_candidates,
    evaluate_candidate,
    list_capability_requirements,
    reject_candidate,
)

ROOT = Path(__file__).resolve().parents[1]
WREC_MODULE = ROOT / "src" / "aios" / "workforce_recommendation.py"

ReqSpec = tuple[str, int, bool]  # (capability_name, min_proficiency, required)

# Built explicitly rather than through ``resolve_owner_actor``: the P2-1 gate is
# about never *defaulting* to owner, and the tests must prove the identity is
# carried by the caller, not manufactured by the service.
OWNER = ActorContext(kind="owner", owner_id="owner")
AGENT = ActorContext(kind="agent", agent_id="agent-1")
SYSTEM = ActorContext.system()


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors test_workforce_benchmark_match_w3b.py)
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
        session.exec(select(AuditLog).where(AuditLog.action == action)).all()
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
    # NOTE: no status assertion here -- some tests deliberately build a BLOCKED
    # Match (capability gap) and assert that for themselves.
    return cand, match, head


# ---------------------------------------------------------------------------
# T-REC-STATE -- controlled state transitions
# ---------------------------------------------------------------------------


def test_recommend_promotes_evaluated_candidate(tmp_path: Path) -> None:
    """T-REC-STATE-1: EVALUATED + COMPUTED Match -> RECOMMENDED + PROPOSED row."""
    from aios.workforce_recommendation import recommend_candidate

    url = f"sqlite:///{(tmp_path / 'state1.db').as_posix()}"
    with _db(url) as session:
        cand, match, _ = _prepared(session)

        rec = recommend_candidate(session, cand.id)
        session.commit()

        assert rec.status == RecommendationStatus.PROPOSED
        assert rec.proposed_action == "hire"
        assert rec.risk_level == RiskLevel.L4
        assert rec.match_id == match.id
        assert rec.match_attempt == 1
        assert rec.job_version_id == cand.job_version_id
        # INV-3: only a human decision may populate these.
        assert rec.decided_by is None
        assert rec.decided_at is None
        assert rec.decision_rationale is None
        # Forward-only column stays NULL in V1.
        assert rec.approval_id is None
        # Unknown dimensions are status flags, never fabricated numbers.
        assert rec.unknown_dimensions == {
            "reliability": {"status": "future_capability", "scored": False},
            "historical": {"status": "future_capability", "scored": False},
            "cost": {
                "status": "unknown",
                "scored": False,
                "advisory_only": True,
            },
        }

        session.refresh(cand)
        assert cand.status == CandidateStatus.RECOMMENDED

        audit = _audits(session, "recommendation.proposed")
        assert len(audit) == 1
        assert audit[0].resource_type == "recommendation"
        assert audit[0].resource_id == rec.id


def test_recommend_refuses_non_evaluated_candidate(tmp_path: Path) -> None:
    """T-REC-STATE-3: POOLED / REJECTED / EVALUATING -> 409, status unchanged."""
    from aios.workforce_recommendation import recommend_candidate

    url = f"sqlite:///{(tmp_path / 'state3.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": 80})
        _, head = _build_chain(session, ("writing", 50, True))
        cand = _pool(session, head)

        # POOLED -- never evaluated, no Match row at all.
        with pytest.raises(ServiceError) as exc:
            recommend_candidate(session, cand.id)
        assert exc.value.status_code == 409
        session.refresh(cand)
        assert cand.status == CandidateStatus.POOLED

        evaluate_candidate(session, cand.id)
        session.commit()
        compute_match(session, cand.id, head.id)
        session.commit()

        # EVALUATED -> REJECTED (W3-A edge) -- no longer eligible.
        reject_candidate(session, cand.id, actor="reviewer")
        session.commit()
        with pytest.raises(ServiceError) as exc2:
            recommend_candidate(session, cand.id)
        assert exc2.value.status_code == 409
        session.refresh(cand)
        assert cand.status == CandidateStatus.REJECTED

        # EVALUATING is a transient half-state -- also refused.
        cand.status = CandidateStatus.EVALUATING
        session.add(cand)
        session.commit()
        with pytest.raises(ServiceError) as exc3:
            recommend_candidate(session, cand.id)
        assert exc3.value.status_code == 409
        session.refresh(cand)
        assert cand.status == CandidateStatus.EVALUATING

        assert _audits(session, "recommendation.proposed") == []


def test_recommended_has_no_outbound_edge_but_evaluated(tmp_path: Path) -> None:
    """T-REC-STATE-4: ALLOWED[RECOMMENDED] == {EVALUATED, TRIALING}.

    W3-C opened the single Match-gate edge to EVALUATED; W3-D (Trial) added the
    controlled second outbound edge to TRIALING. The Match-gate shortcuts stay
    illegal even after both gates are open.
    """
    assert CandidateLifecycle.ALLOWED[CandidateStatus.RECOMMENDED] == {
        CandidateStatus.EVALUATED,
        CandidateStatus.TRIALING,
    }
    # RECOMMENDED -> TRIALING is now legal (W3-D), but the Match-gate shortcuts
    # stay illegal.
    assert CandidateLifecycle.can_transition(
        CandidateStatus.RECOMMENDED, CandidateStatus.TRIALING
    )
    assert not CandidateLifecycle.can_transition(
        CandidateStatus.RECOMMENDED, CandidateStatus.POOLED
    )
    assert not CandidateLifecycle.can_transition(
        CandidateStatus.POOLED, CandidateStatus.RECOMMENDED
    )


# ---------------------------------------------------------------------------
# T-REC-GATE -- fail-closed
# ---------------------------------------------------------------------------


def test_blocked_match_is_refused_409(tmp_path: Path) -> None:
    """T-REC-GATE-5: capability_gap -> 409, candidate stays EVALUATED, no row."""
    from aios.workforce_recommendation import recommend_candidate

    url = f"sqlite:///{(tmp_path / 'gate5.db').as_posix()}"
    with _db(url) as session:
        # min_prof 90 vs priority 80 -> threshold not passed -> BLOCKED.
        cand, match, _ = _prepared(session, min_prof=90, priority=80)
        assert match.status == MatchStatus.BLOCKED
        assert match.match_blocked_reason == "capability_gap"

        with pytest.raises(ServiceError) as exc:
            recommend_candidate(session, cand.id)
        assert exc.value.status_code == 409
        assert "capability_gap" in exc.value.detail

        session.refresh(cand)
        assert cand.status == CandidateStatus.EVALUATED
        assert session.exec(select(Recommendation)).all() == []


def test_missing_match_row_is_refused_422(tmp_path: Path) -> None:
    """T-REC-GATE-6: an EVALUATED candidate with no Match row -> 422."""
    from aios.workforce_recommendation import recommend_candidate

    url = f"sqlite:///{(tmp_path / 'gate6.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": 80})
        _, head = _build_chain(session, ("writing", 50, True))
        cand = _pool(session, head)
        evaluate_candidate(session, cand.id)
        session.commit()
        # Deliberately no compute_match().

        with pytest.raises(ServiceError) as exc:
            recommend_candidate(session, cand.id)
        assert exc.value.status_code == 422
        session.refresh(cand)
        assert cand.status == CandidateStatus.EVALUATED


def test_empty_breakdown_is_refused_422(tmp_path: Path) -> None:
    """T-REC-GATE-7: F-R3 -- an unexplainable Match must not be recommended."""
    from aios.workforce_recommendation import recommend_candidate

    url = f"sqlite:///{(tmp_path / 'gate7.db').as_posix()}"
    with _db(url) as session:
        cand, match, _ = _prepared(session)
        match.breakdown = {}
        session.add(match)
        session.commit()

        with pytest.raises(ServiceError) as exc:
            recommend_candidate(session, cand.id)
        assert exc.value.status_code == 422
        assert "not explainable" in exc.value.detail


def test_empty_evidence_refs_is_refused_422(tmp_path: Path) -> None:
    """T-REC-GATE-8: F-R3 -- empty evidence_refs -> 422."""
    from aios.workforce_recommendation import recommend_candidate

    url = f"sqlite:///{(tmp_path / 'gate8.db').as_posix()}"
    with _db(url) as session:
        cand, match, _ = _prepared(session)
        match.evidence_refs = []
        session.add(match)
        session.commit()

        with pytest.raises(ServiceError) as exc:
            recommend_candidate(session, cand.id)
        assert exc.value.status_code == 422
        assert "not explainable" in exc.value.detail


def test_fabricated_unknown_dimension_is_refused_422(tmp_path: Path) -> None:
    """T-REC-GATE-9: F-R4 -- a numeric reliability status is fabrication."""
    from aios.workforce_recommendation import recommend_candidate

    url = f"sqlite:///{(tmp_path / 'gate9.db').as_posix()}"
    with _db(url) as session:
        cand, _, _ = _prepared(session)
        ctx = dict(cand.evaluation_context or {})
        ctx["reliability_evidence"] = dict(ctx["reliability_evidence"])
        ctx["reliability_evidence"]["status"] = 0.92
        cand.evaluation_context = ctx
        session.add(cand)
        session.commit()

        with pytest.raises(ServiceError) as exc:
            recommend_candidate(session, cand.id)
        assert exc.value.status_code == 422
        assert "fabricated" in exc.value.detail


def test_cost_is_text_advisory_only(tmp_path: Path) -> None:
    """T-REC-GATE-10: F-R5 -- cost never becomes a numeric score component."""
    from aios.workforce_recommendation import recommend_candidate

    url = f"sqlite:///{(tmp_path / 'gate10.db').as_posix()}"
    with _db(url) as session:
        cand, match, _ = _prepared(
            session, cost_policy={"max_cost_per_task": 5, "currency": "CNY"}
        )

        rec = recommend_candidate(session, cand.id)
        session.commit()

        # Advisory is free text only -- and it must not echo the numbers.
        assert isinstance(rec.cost_advisory, str)
        assert rec.cost_advisory
        assert "5" not in rec.cost_advisory
        # score is the Match snapshot: capability_fit only, no cost component.
        assert rec.score == match.score
        assert "cost" not in rec.evaluated_fields
        assert any("cost" in f for f in rec.excluded_fields)
        assert rec.unknown_dimensions["cost"]["advisory_only"] is True


# ---------------------------------------------------------------------------
# T-REC-EXPL -- explainability & audit
# ---------------------------------------------------------------------------


def test_breakdown_is_a_snapshot_not_a_reference(tmp_path: Path) -> None:
    """T-REC-EXPL-13: score == match.score, breakdown deep-copied."""
    from aios.workforce_recommendation import recommend_candidate

    url = f"sqlite:///{(tmp_path / 'expl13.db').as_posix()}"
    with _db(url) as session:
        cand, match, _ = _prepared(session)
        rec = recommend_candidate(session, cand.id)
        session.commit()

        assert rec.score == match.score
        assert rec.breakdown == match.breakdown
        assert rec.breakdown is not match.breakdown
        assert rec.weights_version == match.weights_version

        # Mutating the Match afterwards must not leak into the recommendation.
        match.breakdown = {"tampered": True}
        match.score = 0.0
        session.add(match)
        session.commit()
        session.refresh(rec)
        assert rec.score != 0.0
        assert rec.breakdown != {"tampered": True}


def test_evidence_refs_end_with_match_ref(tmp_path: Path) -> None:
    """T-REC-EXPL-14 / UW-1: unbound needs no ``br:`` ring; ``match:`` is last."""
    from aios.workforce_recommendation import recommend_candidate

    url = f"sqlite:///{(tmp_path / 'expl14.db').as_posix()}"
    with _db(url) as session:
        cand, match, _ = _prepared(session)
        rec = recommend_candidate(session, cand.id)
        session.commit()

        assert rec.evidence_refs[-1] == f"match:{match.id}"
        assert rec.evidence_refs[:-1] == match.evidence_refs
        assert any(r.startswith("cand:") for r in rec.evidence_refs)
        # Unbound JobVersion -> no br: ring, and that is legal (P2-3 / scenario A).
        assert not any(r.startswith("br:") for r in rec.evidence_refs)


def test_proposed_audit_carries_full_payload(tmp_path: Path) -> None:
    """T-REC-EXPL-15: the proposed audit row holds every required field."""
    from aios.workforce_recommendation import recommend_candidate

    url = f"sqlite:///{(tmp_path / 'expl15.db').as_posix()}"
    with _db(url) as session:
        cand, match, _ = _prepared(session)
        recommend_candidate(session, cand.id)
        session.commit()

        (audit,) = _audits(session, "recommendation.proposed")
        after = audit.after_snapshot
        for field in (
            "match_id",
            "match_attempt",
            "score",
            "weights_version",
            "evaluated_fields",
            "evidence_refs",
            "excluded_fields",
            "unknown_dimensions",
            "proposed_action",
            "status",
        ):
            assert field in after, f"audit.after missing {field}"
        assert after["match_id"] == match.id
        assert after["match_attempt"] == 1
        assert audit.project_id is None
        assert audit.task_id is None


# ---------------------------------------------------------------------------
# T-REC-IDEM -- idempotency
# ---------------------------------------------------------------------------


def test_replay_is_idempotent(tmp_path: Path) -> None:
    """T-REC-IDEM-17: same attempt -> same row, unchanged created_at, no audit."""
    from aios.workforce_recommendation import recommend_candidate

    url = f"sqlite:///{(tmp_path / 'idem17.db').as_posix()}"
    with _db(url) as session:
        cand, _, _ = _prepared(session)
        first = recommend_candidate(session, cand.id)
        session.commit()
        created_at = first.created_at

        second = recommend_candidate(session, cand.id)
        session.commit()

        assert second.id == first.id
        assert second.created_at == created_at
        assert len(_audits(session, "recommendation.proposed")) == 1
        session.refresh(cand)
        assert cand.status == CandidateStatus.RECOMMENDED


# ---------------------------------------------------------------------------
# T-REC-BOUNDARY -- do not overreach
# ---------------------------------------------------------------------------


def test_recommendation_never_writes_evaluation_context(tmp_path: Path) -> None:
    """T-REC-BOUNDARY-25: W3-C reads evaluation_context but writes zero times."""
    from aios.workforce_recommendation import recommend_candidate

    url = f"sqlite:///{(tmp_path / 'bound25.db').as_posix()}"
    with _db(url) as session:
        cand, _, _ = _prepared(session)
        before = dict(cand.evaluation_context or {})

        rec = recommend_candidate(session, cand.id)
        session.commit()
        assert rec is not None

        session.refresh(cand)
        assert cand.evaluation_context == before


def test_no_deferred_tables_were_created(tmp_path: Path) -> None:
    """T-REC-BOUNDARY-26 (residual): Employee / training / performance /
    candidate_evaluation stay deferred.

    NOTE: ``trial`` was deferred under W3-C's T-REC-BOUNDARY-26, but W3-D
    implemented it (see ``workforce_trial.py`` + migration
    ``20260903_0001_workforce_trial``), so it is intentionally no longer in
    this list. The genuinely-still-deferred W4+ tables are asserted below.
    """
    url = f"sqlite:///{(tmp_path / 'bound26.db').as_posix()}"
    with _db(url) as _session:
        tables = set(inspect(get_engine(url)).get_table_names())
        for deferred in (
            "employee",
            "training",
            "performance",
            "candidate_evaluation",
        ):
            assert deferred not in tables, f"{deferred} must not exist yet"
        # W3-C's own table exists (C3①: it is no longer deferred) ...
        assert "recommendation" in tables
        # ... and W3-D's ``trial`` table now exists too.
        assert "trial" in tables


def test_recommend_calls_no_w3a_w3b_budget_or_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-REC-BOUNDARY-27: the recommender triggers no recompute / budget / run."""
    from aios import execution, workforce
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'bound27.db').as_posix()}"
    with _db(url) as session:
        cand, _, _ = _prepared(session)

        calls: list[str] = []

        def _spy(name: str):
            def _inner(*args, **kwargs):
                calls.append(name)
                raise AssertionError(f"{name} must not be called by W3-C")

            return _inner

        monkeypatch.setattr(workforce, "compute_match", _spy("compute_match"))
        monkeypatch.setattr(workforce, "run_benchmark", _spy("run_benchmark"))
        monkeypatch.setattr(
            workforce, "evaluate_candidate", _spy("evaluate_candidate")
        )
        monkeypatch.setattr(workforce, "rank_candidates", _spy("rank_candidates"))
        monkeypatch.setattr(execution, "execute_task", _spy("execute_task"))

        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()

        assert calls == []
        assert rec.status == RecommendationStatus.PROPOSED


def test_recommend_adds_no_capability_vocabulary(tmp_path: Path) -> None:
    """T-REC-BOUNDARY-28: no second capability SSoT is created."""
    from aios.workforce_recommendation import recommend_candidate

    url = f"sqlite:///{(tmp_path / 'bound28.db').as_posix()}"
    with _db(url) as session:
        cand, _, _ = _prepared(session)
        before = len(session.exec(select(Capability)).all())

        recommend_candidate(session, cand.id)
        session.commit()

        assert len(session.exec(select(Capability)).all()) == before


# ---------------------------------------------------------------------------
# T-REC-EVID -- F-R3b: an unresolvable attempt must never reach NOT NULL (C6)
# ---------------------------------------------------------------------------


def test_unresolvable_attempt_is_refused_422(tmp_path: Path) -> None:
    """T-REC-EVID-47: evidence present but unparsable -> 422, never IntegrityError."""
    from aios.workforce_recommendation import recommend_candidate

    url = f"sqlite:///{(tmp_path / 'evid47.db').as_posix()}"
    with _db(url) as session:
        cand, match, _ = _prepared(session)
        match.evidence_refs = [f"cand:{cand.id}:attempt:not-a-number"]
        session.add(match)
        session.commit()

        with pytest.raises(ServiceError) as exc:
            recommend_candidate(session, cand.id)
        assert exc.value.status_code == 422
        # One uniform message across every path that parses an attempt (C6).
        assert exc.value.detail == "match evidence is not resolvable"

        session.refresh(cand)
        assert cand.status == CandidateStatus.EVALUATED
        assert _audits(session, "recommendation.proposed") == []


# ---------------------------------------------------------------------------
# T-REC-GATE-11 / F-R6 -- the Trial gate reads APPROVED + decided_by (INV-3)
# ---------------------------------------------------------------------------


def test_trial_eligibility_requires_approved_and_decider(tmp_path: Path) -> None:
    """T-REC-GATE-11: only APPROVED *with a decider* may reach Trial (F-R6)."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'gate11.db').as_posix()}"
    with _db(url) as session:
        cand, _, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()

        # PROPOSED -- the human has not spoken yet.
        assert wrec.assert_trial_eligible(session, rec.id) is False

        wrec.decide_recommendation(
            session, rec.id, ApprovalStatus.APPROVED, actor=OWNER
        )
        session.commit()
        assert wrec.assert_trial_eligible(session, rec.id) is True

        # REJECTED / WITHDRAWN are never eligible, whatever the evidence says.
        rejected = _propose_second(session, "rejected")
        wrec.decide_recommendation(
            session, rejected.id, ApprovalStatus.REJECTED, actor=OWNER
        )
        session.commit()
        assert wrec.assert_trial_eligible(session, rejected.id) is False

        withdrawn = _propose_second(session, "withdrawn")
        # WITHDRAWN has no production path yet (it lands with the F-R8
        # reconcile increment); build it through the single status writer.
        wrec._transition_status(withdrawn, RecommendationStatus.WITHDRAWN)
        session.add(withdrawn)
        session.commit()
        assert wrec.assert_trial_eligible(session, withdrawn.id) is False

        assert wrec.assert_trial_eligible(session, "rec_missing") is False


def _propose_second(session: Session, tag: str) -> Recommendation:
    """A further proposal in the same DB (fresh chain -> fresh candidate).

    ``tag`` keeps the capability / agent names unique per call -- the Capability
    vocabulary is a UNIQUE SSoT, so a second proposal cannot reuse a name.
    """
    from aios.workforce_recommendation import recommend_candidate

    cap_name = f"skill_{tag}"
    _seed_capability(session, cap_name)
    _seed_agent(session, f"agent-{tag}", {cap_name: 90})
    _, head = _build_chain(session, (cap_name, 40, True))
    cand = _pool(session, head)
    evaluate_candidate(session, cand.id)
    session.commit()
    compute_match(session, cand.id, head.id)
    session.commit()
    return recommend_candidate(session, cand.id)


# ---------------------------------------------------------------------------
# T-REC-APPROVAL -- the L4 human gate (P2-1: actor is mandatory)
# ---------------------------------------------------------------------------


def test_owner_approval_writes_decided_by_and_audit(tmp_path: Path) -> None:
    """T-REC-APPROVAL-20 + T-REC-EXPL-16: owner decides; audit carries owner."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'appr20.db').as_posix()}"
    with _db(url) as session:
        cand, _, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()

        decided = wrec.decide_recommendation(
            session,
            rec.id,
            ApprovalStatus.APPROVED,
            rationale="能力匹配，同意试用",
            actor=OWNER,
        )
        session.commit()

        assert decided.status == RecommendationStatus.APPROVED
        # INV-3: a decision always names the human who took it.
        assert decided.decided_by == "owner"
        assert decided.decided_at is not None
        assert decided.decision_rationale == "能力匹配，同意试用"
        assert decided.risk_level == RiskLevel.L4

        session.refresh(cand)
        assert cand.status == CandidateStatus.RECOMMENDED

        (audit,) = _audits(session, "recommendation.decided")
        assert audit.actor == "owner"
        assert audit.resource_type == "recommendation"
        assert audit.resource_id == rec.id
        assert audit.after_snapshot["decision"] == "approved"
        assert audit.after_snapshot["decided_by"] == "owner"
        assert audit.before_snapshot["status"] == "proposed"
        assert audit.after_snapshot["status"] == "approved"
        assert audit.project_id is None
        assert audit.task_id is None


def test_owner_rejection_rolls_candidate_back(tmp_path: Path) -> None:
    """T-REC-APPROVAL-24: REJECTED -> Candidate back to EVALUATED, no Trial."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'appr24.db').as_posix()}"
    with _db(url) as session:
        cand, _, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()
        session.refresh(cand)
        assert cand.status == CandidateStatus.RECOMMENDED

        decided = wrec.decide_recommendation(
            session, rec.id, ApprovalStatus.REJECTED, actor=OWNER
        )
        session.commit()

        assert decided.status == RecommendationStatus.REJECTED
        assert decided.decided_by == "owner"

        session.refresh(cand)
        assert cand.status == CandidateStatus.EVALUATED
        assert wrec.assert_trial_eligible(session, rec.id) is False

        (audit,) = _audits(session, "recommendation.decided")
        assert audit.after_snapshot["decision"] == "rejected"
        assert audit.actor == "owner"


def test_non_owner_actor_is_refused_403(tmp_path: Path) -> None:
    """T-REC-APPROVAL-21 / F-R9: an agent or system actor may never decide."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'appr21.db').as_posix()}"
    with _db(url) as session:
        cand, _, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()

        for actor in (AGENT, SYSTEM):
            with pytest.raises(ServiceError) as exc:
                wrec.decide_recommendation(
                    session, rec.id, ApprovalStatus.APPROVED, actor=actor
                )
            assert exc.value.status_code == 403, f"{actor.kind} must be refused"

        session.refresh(rec)
        assert rec.status == RecommendationStatus.PROPOSED
        assert rec.decided_by is None
        assert _audits(session, "recommendation.decided") == []


def test_repeat_decision_is_refused_409(tmp_path: Path) -> None:
    """T-REC-APPROVAL-22: an already-decided recommendation is terminal."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'appr22.db').as_posix()}"
    with _db(url) as session:
        cand, _, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()

        wrec.decide_recommendation(
            session, rec.id, ApprovalStatus.APPROVED, actor=OWNER
        )
        session.commit()

        for decision in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
            with pytest.raises(ServiceError) as exc:
                wrec.decide_recommendation(session, rec.id, decision, actor=OWNER)
            assert exc.value.status_code == 409

        session.refresh(rec)
        assert rec.status == RecommendationStatus.APPROVED
        assert len(_audits(session, "recommendation.decided")) == 1


def test_approved_without_decided_by_is_unreachable(tmp_path: Path) -> None:
    """T-REC-APPROVAL-23 / INV-3: APPROVED always names its decider.

    Two halves: the *write* path cannot be entered without an actor, and the
    *read* gate refuses a row that was tampered into APPROVED out of band.
    """
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'appr23.db').as_posix()}"
    with _db(url) as session:
        cand, _, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()

        # (1) write path: ``actor`` is keyword-only with no default, so there is
        # no call form that reaches the decision without an identity.
        assert "actor" not in (wrec.decide_recommendation.__kwdefaults__ or {})

        # (2) read path: even an out-of-band APPROVED row with no decider is
        # never treated as trial-eligible, and cannot be "re-decided" either.
        rec.status = RecommendationStatus.APPROVED
        rec.decided_by = None
        session.add(rec)
        session.commit()

        assert wrec.assert_trial_eligible(session, rec.id) is False
        with pytest.raises(ServiceError) as exc:
            wrec.decide_recommendation(
                session, rec.id, ApprovalStatus.APPROVED, actor=OWNER
            )
        assert exc.value.status_code == 409


def test_invalid_decision_vocabulary_is_refused_422(tmp_path: Path) -> None:
    """T-REC-SOT-39: PENDING / EXPIRED are not decisions here (§4.4)."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'sot39.db').as_posix()}"
    with _db(url) as session:
        cand, _, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()

        for decision in (ApprovalStatus.PENDING, ApprovalStatus.EXPIRED):
            with pytest.raises(ServiceError) as exc:
                wrec.decide_recommendation(session, rec.id, decision, actor=OWNER)
            assert exc.value.status_code == 422

        session.refresh(rec)
        assert rec.status == RecommendationStatus.PROPOSED
        assert rec.decided_by is None
        assert _audits(session, "recommendation.decided") == []


# ---------------------------------------------------------------------------
# T-REC-ACTOR -- actor 必填 / 静态闸门（P2-1, INV-4）
# ---------------------------------------------------------------------------


def test_decide_requires_an_actor_argument(tmp_path: Path) -> None:
    """T-REC-ACTOR-44: omitting ``actor`` is a TypeError, never owner rights."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'actor44.db').as_posix()}"
    with _db(url) as session:
        cand, _, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()

        with pytest.raises(TypeError):
            wrec.decide_recommendation(session, rec.id, ApprovalStatus.APPROVED)

        session.refresh(rec)
        assert rec.status == RecommendationStatus.PROPOSED


def test_module_never_resolves_owner_actor() -> None:
    """T-REC-ACTOR-45 / §15.2 #17: no implicit elevation to owner anywhere."""
    source = WREC_MODULE.read_text(encoding="utf-8")
    assert "resolve_owner_actor" not in source


def test_every_recommendation_status_write_funnels_through_the_helper() -> None:
    """T-REC-ACTOR-46 / INV-4: ``rec.status`` is written in exactly one place."""
    tree = ast.parse(WREC_MODULE.read_text(encoding="utf-8"))

    def _enclosing_function_names() -> dict[int, str]:
        owner: dict[int, str] = {}
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                lineno = getattr(inner, "lineno", None)
                if lineno is not None:
                    owner[lineno] = node.name
        return owner

    enclosing = _enclosing_function_names()
    writers: dict[int, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if not isinstance(target, ast.Attribute):
                continue
            if target.attr != "status":
                continue
            # Only Recommendation rows are bound to ``rec`` by convention;
            # ``cand.status`` is governed by CandidateLifecycle instead.
            if not (isinstance(target.value, ast.Name) and target.value.id == "rec"):
                continue
            writers[node.lineno] = enclosing.get(node.lineno, "<module>")

    assert writers, "the single status writer must still exist"
    assert set(writers.values()) == {"_transition_status"}


# ---------------------------------------------------------------------------
# F-R8 data-layer injection helper (C7: simulate drift WITHOUT compute_match)
# ---------------------------------------------------------------------------


def _inject_match_attempt(session: Session, match: Match, attempt: int) -> None:
    """C7 / §16.2 t2: rewrite the ``cand:`` evidence attempt via raw data layer.

    This simulates a Match recompute *without* calling ``compute_match`` and
    *without* touching the frozen W3-B state machine (§15.2 #20). Existing
    non-``cand:`` rings (e.g. ``br:``) are preserved so the test stays honest.
    """
    refs = [f"cand:{match.candidate_id}:attempt:{attempt}"]
    refs += [r for r in match.evidence_refs if not r.startswith("cand:")]
    match.evidence_refs = refs
    session.add(match)
    session.commit()


# ---------------------------------------------------------------------------
# T-REC-RECONCILE -- F-R8 lazy reconcile (C1)
# ---------------------------------------------------------------------------


def test_drifted_approved_is_withdrawn_by_lazy_reconcile(tmp_path: Path) -> None:
    """T-REC-RECONCILE-31 + T-REC-GATE-12: APPROVED + drift -> False (12), withdrawn (31)."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'recon31.db').as_posix()}"
    with _db(url) as session:
        cand, match, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()
        wrec.decide_recommendation(
            session, rec.id, ApprovalStatus.APPROVED, actor=OWNER
        )
        session.commit()

        _inject_match_attempt(session, match, 4)  # t2: drift to attempt 4

        assert wrec.assert_trial_eligible(session, rec.id) is False  # t3
        session.commit()

        session.refresh(rec)
        assert rec.status == RecommendationStatus.WITHDRAWN
        assert rec.decided_by == "owner"  # the decision is not erased

        (audit,) = _audits(session, "recommendation.withdrawn")
        assert audit.before_snapshot["status"] == "approved"
        assert audit.before_snapshot["decided_by"] == "owner"
        assert audit.after_snapshot["detected_attempt"] == 4
        assert audit.after_snapshot["reason"] == "match_attempt_drift"

        session.refresh(cand)
        assert cand.status == CandidateStatus.EVALUATED


def test_reconcile_is_idempotent(tmp_path: Path) -> None:
    """T-REC-RECONCILE-32: two reconcile calls -> one audit, no double withdraw."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'recon32.db').as_posix()}"
    with _db(url) as session:
        cand, match, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()
        wrec.decide_recommendation(
            session, rec.id, ApprovalStatus.APPROVED, actor=OWNER
        )
        session.commit()
        _inject_match_attempt(session, match, 4)
        session.commit()

        assert wrec.assert_trial_eligible(session, rec.id) is False
        # Second call must be a pure no-op.
        assert wrec.assert_trial_eligible(session, rec.id) is False
        session.commit()

        assert len(_audits(session, "recommendation.withdrawn")) == 1
        session.refresh(rec)
        assert rec.status == RecommendationStatus.WITHDRAWN
        session.refresh(cand)
        assert cand.status == CandidateStatus.EVALUATED


def test_concurrent_reconcile_writes_at_most_one_audit(tmp_path: Path) -> None:
    """T-REC-RECONCILE-33: two sessions -> exactly one withdrawn audit (CAS)."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'recon33.db').as_posix()}"
    with _db(url) as s1:
        cand, match, _ = _prepared(s1)
        rec = wrec.recommend_candidate(s1, cand.id)
        s1.commit()
        wrec.decide_recommendation(s1, rec.id, ApprovalStatus.APPROVED, actor=OWNER)
        s1.commit()
        _inject_match_attempt(s1, match, 4)
        s1.commit()

        # Session 1 withdraws and commits its result.
        assert (
            wrec._reconcile_drift(s1, s1.get(Recommendation, rec.id)) is True
        )
        s1.commit()

        # Session 2 sees the committed WITHDRAWN row and does nothing.
        with _db(url) as s2:
            rec2 = s2.get(Recommendation, rec.id)
            assert wrec._reconcile_drift(s2, rec2) is False

        assert len(_audits(s1, "recommendation.withdrawn")) == 1


def test_drifted_unresolvable_or_missing_match_is_withdrawn(tmp_path: Path) -> None:
    """T-REC-RECONCILE-34: PROPOSED + unresolvable/missing Match -> WITHDRAWN (fail-closed)."""
    from aios import workforce_recommendation as wrec

    # (a) unresolvable evidence_refs (attempt no longer parses).
    url = f"sqlite:///{(tmp_path / 'recon34a.db').as_posix()}"
    with _db(url) as session:
        cand, match, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)  # PROPOSED, not decided
        session.commit()

        match.evidence_refs = [f"cand:{cand.id}:attempt:not-a-number"]
        session.add(match)
        session.commit()
        assert wrec.assert_trial_eligible(session, rec.id) is False
        session.commit()
        session.refresh(rec)
        assert rec.status == RecommendationStatus.WITHDRAWN
        (audit,) = _audits(session, "recommendation.withdrawn")
        assert audit.after_snapshot["reason"] == "attempt_unresolvable"

    # (b) the referenced Match row is gone -> fail-closed (match_missing).
    # The FK (RESTRICT) forbids persisting an orphan recommendation, so we assert
    # the detector's verdict directly on the recommendation from (a) whose
    # ``match_id`` we point at nothing (in memory only). The full withdraw goes
    # through the identical _reconcile_drift path proven by (a); only the
    # ``reason`` label differs.
    rec.match_id = "match_nonexistent"  # in-memory only; no FK flush
    drifted, reason, detected = wrec._detect_drift(session, rec)
    assert drifted is True
    assert reason == "match_missing"


def test_withdrawn_can_be_rebuilt_from_fresh_evidence(tmp_path: Path) -> None:
    """T-REC-RECONCILE-35: drift -> WITHDRAWN -> rebuild -> PROPOSED + reproposed audit."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'recon35.db').as_posix()}"
    with _db(url) as session:
        cand, match, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()
        wrec.decide_recommendation(
            session, rec.id, ApprovalStatus.APPROVED, actor=OWNER
        )
        session.commit()
        _inject_match_attempt(session, match, 4)  # original attempt was 1
        session.commit()

        assert wrec.assert_trial_eligible(session, rec.id) is False  # withdraw
        session.commit()
        session.refresh(rec)
        assert rec.status == RecommendationStatus.WITHDRAWN
        session.refresh(cand)
        assert cand.status == CandidateStatus.EVALUATED

        rebuilt = wrec.recommend_candidate(session, cand.id)  # rebuild
        session.commit()

        assert rebuilt.id == rec.id  # same UNIQUE slot, updated in place
        assert rebuilt.status == RecommendationStatus.PROPOSED
        assert rebuilt.match_attempt == 4
        assert rebuilt.decided_by is None  # prior decision wiped (INV-3)
        session.refresh(cand)
        assert cand.status == CandidateStatus.RECOMMENDED

        (audit,) = _audits(session, "recommendation.reproposed")
        assert audit.before_snapshot["match_attempt"] == 1
        assert audit.after_snapshot["match_attempt"] == 4


def test_withdrawn_rebuild_fails_when_match_blocked(tmp_path: Path) -> None:
    """T-REC-RECONCILE-36: rebuild with a BLOCKED Match stays WITHDRAWN (fail-closed)."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'recon36.db').as_posix()}"
    with _db(url) as session:
        cand, match, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()
        wrec.decide_recommendation(
            session, rec.id, ApprovalStatus.APPROVED, actor=OWNER
        )
        session.commit()
        _inject_match_attempt(session, match, 4)
        session.commit()
        assert wrec.assert_trial_eligible(session, rec.id) is False
        session.commit()

        # Simulate a recompute that now BLOCKS the Match (data-layer injection;
        # no compute_match call -- §15.2 #20).
        match.status = MatchStatus.BLOCKED
        match.match_blocked_reason = "capability_gap"
        session.add(match)
        session.commit()

        with pytest.raises(ServiceError) as exc:
            wrec.recommend_candidate(session, cand.id)
        assert exc.value.status_code == 409
        session.commit()

        session.refresh(rec)
        assert rec.status == RecommendationStatus.WITHDRAWN
        session.refresh(cand)
        assert cand.status == CandidateStatus.EVALUATED
        # No new PROPOSED was written by the failed rebuild -- only the original.
        assert len(_audits(session, "recommendation.proposed")) == 1


# ---------------------------------------------------------------------------
# T-REC-CAS -- F-R8 CAS 撤销绑定 (C8)
# ---------------------------------------------------------------------------


def test_cas_requires_stored_match_attempt_token(tmp_path: Path) -> None:
    """T-REC-CAS-49: CAS binds match_attempt; a rebuilt token mismatches -> rowcount 0."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'cas49.db').as_posix()}"
    with _db(url) as s1:
        cand, match, _ = _prepared(s1)
        rec = wrec.recommend_candidate(s1, cand.id)
        s1.commit()
        wrec.decide_recommendation(s1, rec.id, ApprovalStatus.APPROVED, actor=OWNER)
        s1.commit()
        _inject_match_attempt(s1, match, 4)
        s1.commit()

        # Another writer already withdrew AND rebuilt with a new attempt (4).
        with _db(url) as s2:
            other = s2.get(Recommendation, rec.id)
            other.status = RecommendationStatus.WITHDRAWN
            other.match_attempt = 4
            s2.add(other)
            s2.commit()

        # s1's reconcile uses its stale token (1), which no longer matches the
        # row (now match_attempt=4) -> CAS rowcount 0 -> no audit, returns False.
        assert wrec._reconcile_drift(s1, s1.get(Recommendation, rec.id)) is False
        assert _audits(s1, "recommendation.withdrawn") == []


def test_cas_rowcount_zero_rejugde_no_write(tmp_path: Path) -> None:
    """T-REC-CAS-50: concurrent withdraw (rowcount 0) -> no audit, re-read -> False."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'cas50.db').as_posix()}"
    with _db(url) as s1:
        cand, match, _ = _prepared(s1)
        rec = wrec.recommend_candidate(s1, cand.id)
        s1.commit()
        wrec.decide_recommendation(s1, rec.id, ApprovalStatus.APPROVED, actor=OWNER)
        s1.commit()
        _inject_match_attempt(s1, match, 4)
        s1.commit()

        # Concurrent winner: another session already withdrew.
        with _db(url) as s2:
            other = s2.get(Recommendation, rec.id)
            other.status = RecommendationStatus.WITHDRAWN
            s2.add(other)
            s2.commit()

        result = wrec._reconcile_drift(s1, s1.get(Recommendation, rec.id))
        assert result is False
        # No withdrawn audit was written by s1; re-read shows the real state.
        assert _audits(s1, "recommendation.withdrawn") == []
        fresh = s1.get(Recommendation, rec.id)
        assert fresh.status == RecommendationStatus.WITHDRAWN


# ---------------------------------------------------------------------------
# T-REC-DRIFT -- F-R8 纵深防御 / 生产不可达 (C7 + P2-3)
# ---------------------------------------------------------------------------


def test_fr8_defense_in_depth_via_data_layer_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-REC-DRIFT-51: F-R8 fires on injected drift; W3-B compute_match never called."""
    from aios import workforce
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'drift51.db').as_posix()}"
    compute_calls: list[int] = []
    monkeypatch.setattr(
        workforce, "compute_match", lambda *a, **k: compute_calls.append(1)
    )
    with _db(url) as session:
        cand, match, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()
        wrec.decide_recommendation(
            session, rec.id, ApprovalStatus.APPROVED, actor=OWNER
        )
        session.commit()

        # Inject drift WITHOUT compute_match (the production-unreachable path).
        _inject_match_attempt(session, match, 4)
        session.commit()

        assert wrec.assert_trial_eligible(session, rec.id) is False
        session.commit()
        session.refresh(rec)
        assert rec.status == RecommendationStatus.WITHDRAWN
        assert compute_calls == []  # F-R8 needed no W3-B to fire


def test_unbound_waive_not_flagged_as_incomplete(tmp_path: Path) -> None:
    """T-REC-DRIFT-52: unbound (no br: ring) still recommends; cand: parses attempt (P2-3)."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'drift52.db').as_posix()}"
    with _db(url) as session:
        # Default JobVersion is unbound -> no br: ring.
        cand, match, _ = _prepared(session)
        assert not any(r.startswith("br:") for r in match.evidence_refs)

        # F-R3 (explainable) and F-R3b (attempt resolvable) must NOT reject.
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()
        assert rec.status == RecommendationStatus.PROPOSED
        assert rec.match_attempt == 1  # cand: ring parsed fine
        assert any("cost" in f for f in rec.excluded_fields)  # excluded carried


# ---------------------------------------------------------------------------
# T-REC-DECISION-39 -- only APPROVED/REJECTED are decisions (§4.4)
# ---------------------------------------------------------------------------


def test_expired_or_pending_decision_is_refused_422(tmp_path: Path) -> None:
    """T-REC-DECISION-39: ApprovalStatus.EXPIRED/PENDING are not decisions -> 422."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'dec39.db').as_posix()}"
    with _db(url) as session:
        cand, _, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()
        for bad in (ApprovalStatus.EXPIRED, ApprovalStatus.PENDING):
            with pytest.raises(ServiceError) as exc:
                wrec.decide_recommendation(session, rec.id, bad, actor=OWNER)
            assert exc.value.status_code == 422, f"{bad} must be 422, got {exc.value.status_code}"
        # Still PROPOSED and no decision was ever written (INV-3).
        session.refresh(rec)
        assert rec.status == RecommendationStatus.PROPOSED
        assert rec.decided_by is None


# ---------------------------------------------------------------------------
# T-REC-CASCADE -- RESTRICT and its unlock path (C4 / DR-1..DR-5)
# ---------------------------------------------------------------------------


def test_proposed_recommendation_blocks_job_cascade_delete(tmp_path: Path) -> None:
    """T-REC-CASCADE-40: a PROPOSED recommendation RESTRICTs Match; job delete fails (DR-1)."""
    from sqlalchemy.exc import IntegrityError

    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'cascade40.db').as_posix()}"
    with _db(url) as session:
        cand, match, head = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()
        assert rec.status == RecommendationStatus.PROPOSED
        rec_id, cand_id, match_id, job_id = rec.id, cand.id, match.id, head.job_id

        # The RESTRICT on recommendation.candidate_id / match_id blocks the cascade.
        job = session.get(Job, job_id)
        with pytest.raises(IntegrityError), session.begin_nested():
            session.delete(job)
            session.flush()

    # Nothing was deleted: all three rows survive the failed cascade (DR-1).
    with _db(url) as s2:
        assert s2.get(Recommendation, rec_id) is not None
        assert s2.get(Candidate, cand_id) is not None
        assert s2.get(Match, match_id) is not None


def test_purge_refused_on_live_recommendation_409(tmp_path: Path) -> None:
    """T-REC-CASCADE-41: purge is fail-closed on PROPOSED/APPROVED (DR-3)."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'cascade41.db').as_posix()}"
    with _db(url) as session:
        cand, _, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()

        # PROPOSED -> 409, row untouched.
        with pytest.raises(ServiceError) as exc:
            wrec.purge_recommendation(session, rec.id, actor=OWNER)
        assert exc.value.status_code == 409
        session.refresh(rec)
        assert rec.status == RecommendationStatus.PROPOSED
        assert session.get(Recommendation, rec.id) is not None

        # APPROVED (a live human decision) -> still 409.
        wrec.decide_recommendation(session, rec.id, ApprovalStatus.APPROVED, actor=OWNER)
        session.commit()
        with pytest.raises(ServiceError) as exc2:
            wrec.purge_recommendation(session, rec.id, actor=OWNER)
        assert exc2.value.status_code == 409
        session.refresh(rec)
        assert rec.status == RecommendationStatus.APPROVED


def test_withdrawn_purge_unlocks_job_cascade_delete(tmp_path: Path) -> None:
    """T-REC-CASCADE-42: WITHDRAWN->purge lets job delete CASCADE; full snapshot audit (DR-2)."""
    from sqlalchemy.exc import IntegrityError

    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'cascade42.db').as_posix()}"
    with _db(url) as session:
        cand, match, head = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()
        wrec.decide_recommendation(session, rec.id, ApprovalStatus.APPROVED, actor=OWNER)
        session.commit()
        # Drift the underlying evidence so the approved row is withdrawn (F-R8).
        _inject_match_attempt(session, match, 4)
        session.commit()
        assert wrec.assert_trial_eligible(session, rec.id) is False
        session.commit()
        session.refresh(rec)
        assert rec.status == RecommendationStatus.WITHDRAWN
        rec_id, cand_id, match_id, job_id = rec.id, cand.id, match.id, head.job_id

        # Before purge the RESTRICT still blocks the job delete.
        job = session.get(Job, job_id)
        with pytest.raises(IntegrityError), session.begin_nested():
            session.delete(job)
            session.flush()

        # Purge the terminal row: deletes it and writes a full-snapshot audit.
        wrec.purge_recommendation(session, rec_id, actor=OWNER)
        session.commit()

    # Fresh session: the recommendation row is gone and its audit is full.
    with _db(url) as s2:
        assert s2.get(Recommendation, rec_id) is None
        (del_audit,) = _audits(s2, "recommendation.deleted")
        assert del_audit.actor == "owner"
        snap = del_audit.after_snapshot
        assert snap["status"] == "withdrawn"
        # Full snapshot must carry every column so evidence survives deletion.
        for col in (
            "id",
            "candidate_id",
            "match_id",
            "match_attempt",
            "score",
            "breakdown",
            "evidence_refs",
            "excluded_fields",
            "unknown_dimensions",
            "decided_by",
            "decided_at",
            "risk_level",
        ):
            assert col in snap

        # RESTRICT lifted: deleting the job now cascades to candidate + match.
        job = s2.get(Job, job_id)
        s2.delete(job)
        s2.commit()
        assert s2.get(Candidate, cand_id) is None
        assert s2.get(Match, match_id) is None


def test_job_delete_cascades_when_no_recommendation(tmp_path: Path) -> None:
    """T-REC-CASCADE-43: no recommendation -> candidate CASCADE-deletes with its Job (DR-5)."""
    url = f"sqlite:///{(tmp_path / 'cascade43.db').as_posix()}"
    with _db(url) as session:
        cand, match, head = _prepared(session)
        cand_id, match_id, job_id = cand.id, match.id, head.job_id
        # Deliberately do NOT recommend -> no recommendation row to RESTRICT.
        job = session.get(Job, job_id)
        session.delete(job)
        session.commit()  # must succeed -- the RESTRICT is not in the way.

    with _db(url) as s2:
        assert s2.get(Candidate, cand_id) is None
        assert s2.get(Match, match_id) is None


# ---------------------------------------------------------------------------
# T-REC-PURGE-ACTOR -- purge requires an explicit owner actor (P2-6 / F-R11)
# ---------------------------------------------------------------------------


def test_purge_requires_owner_actor_fr11(tmp_path: Path) -> None:
    """T-REC-PURGE-ACTOR-53: missing actor->TypeError; non-owner->403; audit actor=owner (F-R11)."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'purge53.db').as_posix()}"
    with _db(url) as session:
        cand, _, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()
        # A terminal (REJECTED, human-refused) row so the purge would otherwise be allowed.
        wrec.decide_recommendation(session, rec.id, ApprovalStatus.REJECTED, actor=OWNER)
        session.commit()
        session.refresh(rec)
        assert rec.status == RecommendationStatus.REJECTED

        # (a) omitting the actor -> TypeError (keyword-only, no default).
        with pytest.raises(TypeError):
            wrec.purge_recommendation(session, rec.id)  # type: ignore[call-arg]

        # (b) a non-owner actor (agent / system) -> 403, row untouched.
        for actor in (AGENT, SYSTEM):
            with pytest.raises(ServiceError) as exc:
                wrec.purge_recommendation(session, rec.id, actor=actor)
            assert exc.value.status_code == 403, f"{actor.kind} must be refused"
        session.refresh(rec)
        assert session.get(Recommendation, rec.id) is not None

        # (c) the owner may purge; the deletion audit carries the owner id.
        wrec.purge_recommendation(session, rec.id, actor=OWNER)
        session.commit()
        assert session.get(Recommendation, rec.id) is None
        (del_audit,) = _audits(session, "recommendation.deleted")
        assert del_audit.actor == "owner"
        assert del_audit.after_snapshot["status"] == "rejected"


# ---------------------------------------------------------------------------
# V4 §13 契约补全 -- items 2 / 18 / 19 / 29 / 30 / 37 / 38 / 48
# ---------------------------------------------------------------------------


def test_reject_is_a_legal_round_trip_back_to_evaluated(tmp_path: Path) -> None:
    """T-REC-STATE-2: EVALUATED -> RECOMMENDED -> EVALUATED is a legal cycle."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'state2.db').as_posix()}"
    with _db(url) as session:
        cand, _, _ = _prepared(session)

        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()
        session.refresh(cand)
        assert cand.status == CandidateStatus.RECOMMENDED

        wrec.decide_recommendation(
            session, rec.id, ApprovalStatus.REJECTED, actor=OWNER
        )
        session.commit()
        session.refresh(cand)
        assert cand.status == CandidateStatus.EVALUATED
        # Neither leg wedged the lifecycle: a future slot may re-propose, and a
        # future rejection stays legal (the round trip is not one-way).
        CandidateLifecycle.require_transition(
            cand.status, CandidateStatus.RECOMMENDED
        )
        CandidateLifecycle.require_transition(cand.status, CandidateStatus.REJECTED)


def test_new_attempt_requires_reconcile_then_rebuild_wipes_decision(
    tmp_path: Path,
) -> None:
    """T-REC-IDEM-18: new attempt -> 409, reconcile, rebuild (decided_by wiped)."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'idem18.db').as_posix()}"
    with _db(url) as session:
        cand, match, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()
        wrec.decide_recommendation(
            session, rec.id, ApprovalStatus.APPROVED, actor=OWNER
        )
        session.commit()

        # The evidence moved to a new attempt while the approval was live: the
        # stale row is never silently re-proposed (409, fail-closed).
        _inject_match_attempt(session, match, 2)
        with pytest.raises(ServiceError) as exc:
            wrec.recommend_candidate(session, cand.id)
        assert exc.value.status_code == 409

        # Reconcile first: the new attempt withdraws the stale approval, and the
        # withdrawn audit still names the human who had approved it.
        assert wrec.assert_trial_eligible(session, rec.id) is False
        session.commit()
        session.refresh(rec)
        assert rec.status == RecommendationStatus.WITHDRAWN
        (w_audit,) = _audits(session, "recommendation.withdrawn")
        assert w_audit.before_snapshot["decided_by"] == "owner"

        # Rebuild on the new attempt: re-proposed with zero decision residue.
        rebuilt = wrec.recommend_candidate(session, cand.id)
        session.commit()
        assert rebuilt.id == rec.id
        assert rebuilt.status == RecommendationStatus.PROPOSED
        assert rebuilt.match_attempt == 2
        assert rebuilt.decided_by is None
        assert rebuilt.decided_at is None
        assert rebuilt.decision_rationale is None
        (r_audit,) = _audits(session, "recommendation.reproposed")
        assert r_audit.before_snapshot["match_attempt"] == 1
        assert r_audit.after_snapshot["match_attempt"] == 2


def test_concurrent_first_create_yields_one_row_no_integrity_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-REC-IDEM-19: racing first creates -> one row; IntegrityError absorbed."""
    from types import SimpleNamespace

    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'idem19.db').as_posix()}"
    # Writer A commits the first recommendation.
    with _db(url) as s1:
        cand, _, _ = _prepared(s1)
        cand_id = cand.id
        winner = wrec.recommend_candidate(s1, cand_id)
        s1.commit()
        winner_id = winner.id

    # Writer B read the pre-A snapshot: candidate still EVALUATED and no row yet.
    # Restore that snapshot (the race window), then let B race into the slot A
    # already holds: B's existing-check must still see the stale empty view so
    # the insert genuinely collides with UNIQUE(candidate_id, job_version_id).
    with _db(url) as s2:
        stale = s2.get(Candidate, cand_id)
        stale.status = CandidateStatus.EVALUATED
        s2.add(stale)
        s2.commit()

        real_exec = s2.exec
        called = {"n": 0}

        def _stale_first_exec(stmt, *args, **kwargs):
            called["n"] += 1
            if called["n"] == 1:
                return SimpleNamespace(first=lambda: None)
            return real_exec(stmt, *args, **kwargs)

        monkeypatch.setattr(s2, "exec", _stale_first_exec)
        rec = wrec.recommend_candidate(s2, cand_id)
        s2.commit()

        # The loser receives the authoritative winner: no duplicate row and no
        # IntegrityError escaped to the caller.
        assert rec.id == winner_id
        rows = s2.exec(
            select(Recommendation).where(Recommendation.candidate_id == cand_id)
        ).all()
        assert len(rows) == 1


def test_w3c_migration_is_single_head_and_reversible(tmp_path: Path) -> None:
    """T-REC-MIG-29 / T-REC-MIG-30: single head; downgrade drops only W3-C."""
    import sqlite3

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from alembic import command

    # 29: the single alembic head is now the W3-D Trial migration
    cfg = Config(ROOT / "alembic.ini")
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    assert ScriptDirectory.from_config(cfg).get_heads() == [
        "20260903_0001_workforce_trial"
    ]

    db_path = tmp_path / "mig_w3c.db"
    url = f"sqlite:///{db_path.as_posix()}"
    run_migrations(url)

    conn = sqlite3.connect(str(db_path))
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        )
    }
    assert "recommendation" in tables
    indexes = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='recommendation'"
        )
    }
    assert {"ix_recommendation_status", "ix_recommendation_decided_by"} <= indexes
    version = conn.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone()[0]
    assert version == "20260903_0001_workforce_trial"
    conn.close()

    # 30: reversible -- downgrade removes the W3-C table + indexes, nothing else.
    cfg.set_main_option("sqlalchemy.url", url)
    command.downgrade(cfg, "20260901_0001_workforce_match_benchmark")

    conn = sqlite3.connect(str(db_path))
    tables_after = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        )
    }
    assert "recommendation" not in tables_after
    for kept in (
        "candidate",
        "match",
        "benchmark",
        "benchmark_version",
        "job_version",
    ):
        assert kept in tables_after, f"{kept} must survive the downgrade"
    version_after = conn.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone()[0]
    assert version_after == "20260901_0001_workforce_match_benchmark"
    conn.close()


def test_recommendation_table_has_no_approval_status_column(
    tmp_path: Path,
) -> None:
    """T-REC-SOT-37: status is the SoT -- no approval_status column (INV-1)."""
    url = f"sqlite:///{(tmp_path / 'sot37.db').as_posix()}"
    with _db(url) as _session:
        cols = {
            c["name"]
            for c in inspect(get_engine(url)).get_columns("recommendation")
        }
        assert "approval_status" not in cols
        assert "status" in cols
        assert "decided_by" in cols


def test_undecided_rows_never_acquire_a_decider(tmp_path: Path) -> None:
    """T-REC-SOT-38: no path writes decided_by outside a decision (INV-3)."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'sot38.db').as_posix()}"
    with _db(url) as session:
        cand, match, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)  # PROPOSED
        session.commit()
        assert rec.decided_by is None
        assert rec.decided_at is None
        assert rec.decision_rationale is None

        # The F-R8 withdraw of a *never-decided* row adds no decider either.
        _inject_match_attempt(session, match, 4)
        assert wrec.assert_trial_eligible(session, rec.id) is False
        session.commit()
        session.refresh(rec)
        assert rec.status == RecommendationStatus.WITHDRAWN
        assert rec.decided_by is None

        # The rebuild path keeps every decision field cleared (INV-3).
        rebuilt = wrec.recommend_candidate(session, cand.id)
        session.commit()
        assert rebuilt.status == RecommendationStatus.PROPOSED
        assert rebuilt.decided_by is None
        assert rebuilt.decided_at is None
        assert rebuilt.decision_rationale is None


def test_withdrawn_rebuild_refuses_unresolvable_evidence_422(
    tmp_path: Path,
) -> None:
    """T-REC-EVID-48: rebuild path -> unresolvable evidence -> 422 (F-R3b)."""
    from aios import workforce_recommendation as wrec

    url = f"sqlite:///{(tmp_path / 'evid48.db').as_posix()}"
    with _db(url) as session:
        cand, match, _ = _prepared(session)
        rec = wrec.recommend_candidate(session, cand.id)
        session.commit()
        _inject_match_attempt(session, match, 4)
        assert wrec.assert_trial_eligible(session, rec.id) is False
        session.commit()
        session.refresh(rec)
        assert rec.status == RecommendationStatus.WITHDRAWN

        # The evidence can no longer be parsed: the rebuild must fail closed
        # with the creation path's exact message (C6), never an IntegrityError.
        match.evidence_refs = [f"cand:{cand.id}:attempt:not-a-number"]
        session.add(match)
        session.commit()
        with pytest.raises(ServiceError) as exc:
            wrec.recommend_candidate(session, cand.id)
        assert exc.value.status_code == 422
        assert exc.value.detail == "match evidence is not resolvable"
        session.commit()
        session.refresh(rec)
        assert rec.status == RecommendationStatus.WITHDRAWN
        session.refresh(cand)
        assert cand.status == CandidateStatus.EVALUATED
        # Only the original proposal exists -- the failed rebuild wrote nothing.
        assert len(_audits(session, "recommendation.proposed")) == 1
