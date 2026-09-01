"""Contract tests for Workforce W3-A -- Candidate Evaluation.

Scope: the *Evaluation* half of the W3 loop only (POOLED -> EVALUATING ->
EVALUATED). Match / Benchmark / Recommendation / Trial stay in later stages.

The contracts asserted here (see
``docs/Workforce_W3A_Evaluation_Implementation_Design.md`` §7.2):

1. **No fabricated scores** (R7 condition 2). Only ``capability_fit`` is
   computed. ``benchmark`` / ``cost`` / ``reliability`` / ``historical`` have no
   backing data yet, so they are recorded as ``unknown`` / ``future_capability``
   and NEVER as a numeric score.
2. **Fail-closed on a capability gap** (Spec F1): a required requirement the
   agent does not meet leaves the candidate EVALUATED but marks
   ``recommendation_blocked_reason = "capability_gap"``.
3. **No half-state**: any failure rolls EVALUATING back to POOLED with
   ``evaluation_error`` and re-raises, so the caller always learns it failed.
4. **Idempotency / concurrency**: replaying a completed evaluation is a no-op;
   losing the start-audit race raises 409 instead of silently reporting success.
5. **Zero migration** (contract A): the evidence lives in the W2
   ``Candidate.evaluation_context`` JSON bag -- no table is created and alembic
   keeps its single W2 head.
6. **Auditability** (F4): every attempt writes ``candidate.evaluate.start`` and
   then either ``candidate.evaluate`` or ``candidate.evaluate.error``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlmodel import Session, select

from aios import workforce
from aios.audit import AuditLog, append_audit
from aios.db import get_engine, run_migrations
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    BusinessGoal,
    Candidate,
    CandidateStatus,
    Capability,
    Job,
    JobVersion,
    RequiredWork,
)
from aios.services import ServiceError
from aios.workforce import (
    CandidateLifecycle,
    create_business_goal,
    create_job,
    create_required_work,
    discover_candidates,
    evaluate_candidate,
    list_capability_requirements,
    reject_candidate,
    repool_candidate,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors tests/test_workforce_models.py; rebuilt here to
# keep this file self-contained and avoid cross-file coupling)
# ---------------------------------------------------------------------------

ReqSpec = tuple[str, int, bool]  # (capability_name, min_proficiency, required)


def _db(url: str) -> Session:
    run_migrations(url)
    return Session(get_engine(url))


def _seed_capability(session: Session, name: str) -> Capability:
    cap = Capability(name=name, description=f"{name} capability")
    session.add(cap)
    session.flush()
    return cap


def _seed_agent(
    session: Session,
    name: str,
    capabilities: dict[str, int] | None = None,
    *,
    disabled: tuple[str, ...] = (),
) -> Agent:
    """Register an enabled agent declaring ``capabilities`` as {name: priority}.

    ``disabled`` lists capability names declared with ``enabled=False`` -- the
    AgentCapability row exists but must score as priority 0 (Spec §2.2).
    """
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
                enabled=cap_name not in disabled,
            )
        )
    session.flush()
    return agent


def _build_chain(
    session: Session, *specs: ReqSpec
) -> tuple[BusinessGoal, RequiredWork, Job, JobVersion]:
    """Build goal -> work -> job -> head JobVersion with the spec'd requirements.

    ``create_job`` seeds every requirement with the defaults (min=50,
    required=True); this re-points them at the thresholds each test needs.
    """
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
    return goal, rw, job, head


def _pool(session: Session, head: JobVersion) -> Candidate:
    """Discover + commit, returning the single pooled candidate."""
    cands = discover_candidates(session, head.id)
    session.commit()
    assert len(cands) == 1, "fixture expects exactly one matching agent"
    return cands[0]


def _manual_candidate(
    session: Session, agent: Agent, job: Job, head: JobVersion
) -> Candidate:
    """Pool a candidate directly, bypassing discovery's capability filter.

    Needed for the fail-closed cases discovery would never surface (undeclared or
    disabled capability) -- evaluation must still handle them, not assume the
    pool is pre-filtered.
    """
    cand = Candidate(
        agent_id=agent.id,
        job_id=job.id,
        job_version_id=head.id,
        discovered_by="test_manual",
    )
    session.add(cand)
    session.commit()
    return cand


def _audits(session: Session, action: str) -> list[AuditLog]:
    return list(
        session.exec(select(AuditLog).where(AuditLog.action == action)).all()
    )


# ---------------------------------------------------------------------------
# T-EVAL-1 -- basic closed loop
# ---------------------------------------------------------------------------


def test_evaluate_moves_pooled_to_evaluated_with_capability_evidence(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{(tmp_path / 'eval1.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": 80})
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        cand = _pool(session, head)
        assert cand.status == CandidateStatus.POOLED
        assert cand.evaluation_context == {}

        out = evaluate_candidate(session, cand.id)
        session.commit()

        assert out.status == CandidateStatus.EVALUATED
        ctx = out.evaluation_context
        assert ctx["schema_version"] == "w3a.evaluation.v1"
        assert ctx["evaluator"] == "workforce_evaluation"
        assert ctx["attempt"] == 1
        assert ctx["evaluated_at"]
        assert ctx["evaluated_fields"] == ["capability_fit"]
        assert ctx["evaluation_error"] is None
        assert ctx["recommendation_blocked_reason"] is None

        ev = ctx["capability_evidence"]
        assert ev["status"] == "computed"
        assert ev["threshold_passed"] is True
        assert ev["blocked_requirements"] == []
        # Hand-checkable: (80 - 50) / (100 - 50) == 0.6
        assert ev["capability_fit"] == pytest.approx(0.6)

        req = list_capability_requirements(session, head.id)[0]
        row = ev["requirements"][0]
        # Capability is referenced by SSoT id; the name is a display snapshot.
        assert row["capability_id"] == req.capability_id
        assert row["capability_name"] == "writing"
        assert row["agent_priority"] == 80
        assert row["min_proficiency"] == 50
        assert row["required"] is True
        assert row["declared"] is True
        assert row["capability_enabled"] is True
        assert row["meets_threshold"] is True
        assert row["fit"] == pytest.approx(0.6)


def test_evaluate_never_fabricates_scores_for_unknown_dimensions(
    tmp_path: Path,
) -> None:
    """R7 condition 2 / Spec §2.5 F3: no placeholder numbers for what we cannot compute."""
    url = f"sqlite:///{(tmp_path / 'eval1b.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": 80})
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        cand = _pool(session, head)

        out = evaluate_candidate(session, cand.id)
        session.commit()
        ctx = out.evaluation_context

        assert ctx["capability_evidence"]["status"] == "computed"
        for key in ("benchmark_evidence", "cost_evidence"):
            assert ctx[key]["status"] == "unknown", key
        for key in ("reliability_evidence", "historical_evidence"):
            assert ctx[key]["status"] == "future_capability", key

        # The strong half of the contract: not one numeric score is recorded for
        # a dimension we have no data for (``bool`` excluded -- "waived" is a flag).
        for key in (
            "benchmark_evidence",
            "cost_evidence",
            "reliability_evidence",
            "historical_evidence",
        ):
            numbers = [
                v
                for v in ctx[key].values()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            ]
            assert numbers == [], f"{key} must not carry a numeric score: {numbers}"

        # ...and the component list says so out loud (explainability, Spec §2.1).
        assert ctx["evaluated_fields"] == ["capability_fit"]


def test_evaluate_preferred_requirement_adds_bounded_bonus(tmp_path: Path) -> None:
    """Spec §5.1: preferred requirements nudge, never outvote a hard gap (<=5%)."""
    url = f"sqlite:///{(tmp_path / 'eval1c.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_capability(session, "research")
        _seed_agent(session, "A", {"writing": 80, "research": 100})
        _, _, job, head = _build_chain(
            session, ("writing", 50, True), ("research", 50, False)
        )
        cand = _pool(session, head)

        out = evaluate_candidate(session, cand.id)
        session.commit()
        ev = out.evaluation_context["capability_evidence"]

        # base = 0.6 (writing); preferred mean = 1.0 (research) -> +0.05 capped at 1.0
        assert ev["capability_fit"] == pytest.approx(0.65)
        delta = ev["capability_fit"] - 0.6
        assert delta > 0, "a preferred requirement must contribute a positive nudge"
        # Strict <=5% ceiling (Spec §5.1) -- with float headroom, since the
        # maximum case lands on exactly 0.05 + binary rounding.
        assert delta <= 0.05 + 1e-9, "preferred bonus must be strictly bounded"


@pytest.mark.parametrize(
    ("priority", "expected_fit"),
    [(100, 1.0), (99, 0.0)],
)
def test_evaluate_guards_min_proficiency_100_zero_division(
    tmp_path: Path, priority: int, expected_fit: float
) -> None:
    """min_proficiency=100 zeroes the denominator; the edge is pinned, not raised."""
    url = f"sqlite:///{(tmp_path / f'eval1d{priority}.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": priority})
        _, _, job, head = _build_chain(session, ("writing", 100, True))
        cand = _pool(session, head)

        out = evaluate_candidate(session, cand.id)
        session.commit()
        ev = out.evaluation_context["capability_evidence"]

        assert ev["requirements"][0]["fit"] == pytest.approx(expected_fit)
        assert ev["capability_fit"] == pytest.approx(expected_fit)
        # 99 < 100 is a hard gap even though it is one point short.
        assert ev["threshold_passed"] is (priority >= 100)


# ---------------------------------------------------------------------------
# T-EVAL-2 -- fail-closed on a capability gap
# ---------------------------------------------------------------------------


def test_evaluate_capability_gap_blocks_recommendation(tmp_path: Path) -> None:
    """Spec F1: below threshold -> evaluated but explicitly blocked, never a soft pass."""
    url = f"sqlite:///{(tmp_path / 'eval2.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": 50})  # below min_proficiency=80
        _, _, job, head = _build_chain(session, ("writing", 80, True))
        cand = _pool(session, head)

        out = evaluate_candidate(session, cand.id)
        session.commit()

        assert out.status == CandidateStatus.EVALUATED
        ctx = out.evaluation_context
        assert ctx["recommendation_blocked_reason"] == "capability_gap"
        ev = ctx["capability_evidence"]
        assert ev["threshold_passed"] is False
        assert ev["capability_fit"] == pytest.approx(0.0)
        req = list_capability_requirements(session, head.id)[0]
        assert ev["blocked_requirements"] == [req.id]
        row = ev["requirements"][0]
        assert row["meets_threshold"] is False
        assert row["fit"] == pytest.approx(0.0)


def test_evaluate_undeclared_capability_scores_zero(tmp_path: Path) -> None:
    """An agent that never declared the capability: priority 0, fail-closed."""
    url = f"sqlite:///{(tmp_path / 'eval2b.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        # Agent declares nothing -- discovery would filter it out, so pool manually.
        agent = _seed_agent(session, "A", {})
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        cand = _manual_candidate(session, agent, job, head)

        out = evaluate_candidate(session, cand.id)
        session.commit()
        ev = out.evaluation_context["capability_evidence"]

        row = ev["requirements"][0]
        assert row["declared"] is False
        assert row["capability_enabled"] is False
        assert row["agent_priority"] == 0
        assert row["meets_threshold"] is False
        assert ev["threshold_passed"] is False
        assert out.evaluation_context["recommendation_blocked_reason"] == (
            "capability_gap"
        )


def test_evaluate_disabled_capability_scores_zero(tmp_path: Path) -> None:
    """Spec §2.2: an enabled=False AgentCapability is priority 0, not 'assume average'."""
    url = f"sqlite:///{(tmp_path / 'eval2c.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        agent = _seed_agent(session, "A", {"writing": 90}, disabled=("writing",))
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        cand = _manual_candidate(session, agent, job, head)

        out = evaluate_candidate(session, cand.id)
        session.commit()
        ev = out.evaluation_context["capability_evidence"]

        row = ev["requirements"][0]
        assert row["declared"] is True
        assert row["capability_enabled"] is False
        assert row["agent_priority"] == 0, "disabled must not leak its priority 90"
        assert row["meets_threshold"] is False
        assert out.evaluation_context["recommendation_blocked_reason"] == (
            "capability_gap"
        )


# ---------------------------------------------------------------------------
# T-EVAL-3 -- failure rolls back, never a half-state
# ---------------------------------------------------------------------------


def test_evaluate_failure_rolls_back_to_pooled_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{(tmp_path / 'eval3.db').as_posix()}"

    def boom(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("evidence collection exploded")

    monkeypatch.setattr(workforce, "_build_evaluation_context", boom)

    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": 80})
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        cand = _pool(session, head)

        with pytest.raises(RuntimeError, match="evidence collection exploded"):
            evaluate_candidate(session, cand.id)
        session.commit()

        # The EVALUATING half-state must not survive the call.
        reloaded = session.get(Candidate, cand.id)
        assert reloaded is not None
        assert reloaded.status == CandidateStatus.POOLED
        assert reloaded.evaluation_context["evaluation_error"]["type"] == (
            "RuntimeError"
        )
        assert "exploded" in reloaded.evaluation_context["evaluation_error"]["message"]
        assert reloaded.evaluation_context["attempt"] == 1
        # No successful evaluation was recorded...
        assert _audits(session, "candidate.evaluate") == []
        # ...but the failure is auditable.
        failures = _audits(session, "candidate.evaluate.error")
        assert len(failures) == 1
        assert failures[0].after_snapshot["status"] == "pooled"

        # Nothing lingers in EVALUATING anywhere in the table.
        stuck = session.exec(
            select(Candidate).where(Candidate.status == CandidateStatus.EVALUATING)
        ).all()
        assert stuck == []


# ---------------------------------------------------------------------------
# T-EVAL-4 / T-EVAL-5 -- idempotency and concurrency
# ---------------------------------------------------------------------------


def test_evaluate_replay_is_idempotent(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'eval4.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": 80})
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        cand = _pool(session, head)

        first = evaluate_candidate(session, cand.id)
        session.commit()
        snapshot = dict(first.evaluation_context)
        successes = len(_audits(session, "candidate.evaluate"))
        assert successes == 1

        # Replay: the evaluation is an immutable snapshot, so this is a no-op.
        second = evaluate_candidate(session, cand.id)
        session.commit()

        assert second.id == cand.id
        assert second.evaluation_context == snapshot
        assert second.evaluation_context["attempt"] == 1, "attempt must not bump"
        assert len(_audits(session, "candidate.evaluate")) == successes


def test_evaluate_concurrent_claim_raises_409_without_half_state(
    tmp_path: Path,
) -> None:
    """Losing the start-audit race must fail closed, not report someone else's success."""
    url = f"sqlite:///{(tmp_path / 'eval5.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": 80})
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        cand = _pool(session, head)

        # Simulate a concurrent caller that already claimed (candidate, attempt 1).
        session.add(
            AuditLog(
                actor="other_worker",
                action="candidate.evaluate.start",
                resource_type="candidate",
                resource_id=cand.id,
                project_id=None,
                task_id=None,
                before_snapshot={},
                after_snapshot={},
                idempotency_key=f"evaluate:start:{cand.id}:1",
            )
        )
        session.commit()

        with pytest.raises(ServiceError) as exc:
            evaluate_candidate(session, cand.id)
        assert exc.value.status_code == 409

        # A caller that swallows the 409 and commits still leaves a legal,
        # resumable row -- never an EVALUATING half-state.
        session.commit()
        reloaded = session.get(Candidate, cand.id)
        assert reloaded is not None
        assert reloaded.status == CandidateStatus.POOLED
        assert _audits(session, "candidate.evaluate") == []


# ---------------------------------------------------------------------------
# T-EVAL-6 / T-EVAL-7 -- preconditions and SSoT fail-closed
# ---------------------------------------------------------------------------


def test_evaluate_unknown_candidate_404_and_rejected_is_409(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'eval6.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": 80})
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        cand = _pool(session, head)

        with pytest.raises(ServiceError) as missing:
            evaluate_candidate(session, "cand_does_not_exist")
        assert missing.value.status_code == 404

        # REJECTED must be re-pooled first (R1).
        reject_candidate(session, cand.id)
        session.commit()
        with pytest.raises(ServiceError) as illegal:
            evaluate_candidate(session, cand.id)
        assert illegal.value.status_code == 409


def test_evaluate_missing_agent_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A soft reference to the registry still fails closed when the agent is gone."""
    url = f"sqlite:///{(tmp_path / 'eval7.db').as_posix()}"

    def no_agent(*_args: object, **_kwargs: object) -> None:
        raise ServiceError(404, "agent not found")

    # NOTE: the patch is applied *after* discovery below (see comment there).

    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": 80})
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        cand = _pool(session, head)

        # Patched only AFTER discovery on purpose: discover_candidates also calls
        # get_agent (soft-reference check), and this test targets the evaluation
        # path -- patching earlier would fail the fixture, not the contract.
        monkeypatch.setattr(workforce, "get_agent", no_agent)

        with pytest.raises(ServiceError) as exc:
            evaluate_candidate(session, cand.id)
        assert exc.value.status_code == 404
        session.commit()

        reloaded = session.get(Candidate, cand.id)
        assert reloaded is not None
        assert reloaded.status == CandidateStatus.POOLED
        assert reloaded.evaluation_context["evaluation_error"]["type"] == "ServiceError"


# ---------------------------------------------------------------------------
# T-EVAL-8 -- no required threshold is a 422, never a default score
# ---------------------------------------------------------------------------


def test_evaluate_without_required_requirement_is_422(tmp_path: Path) -> None:
    """With nothing hard to score against, we refuse rather than invent 0.5."""
    url = f"sqlite:///{(tmp_path / 'eval8.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": 80})
        _, _, job, head = _build_chain(session, ("writing", 50, False))
        cand = _pool(session, head)

        with pytest.raises(ServiceError) as exc:
            evaluate_candidate(session, cand.id)
        assert exc.value.status_code == 422
        session.commit()

        reloaded = session.get(Candidate, cand.id)
        assert reloaded is not None
        assert reloaded.status == CandidateStatus.POOLED
        assert "capability_fit" not in reloaded.evaluation_context


# ---------------------------------------------------------------------------
# T-AUDIT-1 -- audit contract (F4)
# ---------------------------------------------------------------------------


def test_evaluate_audit_trail_is_complete_and_redacted(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'eval_audit.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": 80})
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        cand = _pool(session, head)

        evaluate_candidate(session, cand.id)
        session.commit()

        starts = _audits(session, "candidate.evaluate.start")
        dones = _audits(session, "candidate.evaluate")
        assert len(starts) == 1
        assert len(dones) == 1

        start, done = starts[0], dones[0]
        assert start.idempotency_key == f"evaluate:start:{cand.id}:1"
        assert done.idempotency_key == f"evaluate:{cand.id}:1"
        assert start.before_snapshot["status"] == "pooled"
        assert start.after_snapshot["status"] == "evaluating"
        assert done.before_snapshot["status"] == "pooled"
        assert done.after_snapshot["status"] == "evaluated"
        assert done.after_snapshot["evaluated_fields"] == ["capability_fit"]
        assert done.after_snapshot["capability_fit"] == pytest.approx(0.6)
        assert done.after_snapshot["recommendation_blocked_reason"] is None

        # The audit carries the verdict, not the whole evidence bag -- the JSON
        # blob stays in the candidate row (INV-3: no registry data exfiltration).
        assert set(done.after_snapshot) == {
            "status",
            "attempt",
            "evaluated_fields",
            "capability_fit",
            "recommendation_blocked_reason",
        }

        # The audit channel is the shared redact_secrets pipeline -- prove it on
        # the very function evaluate_candidate writes through.
        append_audit(
            session,
            actor="probe",
            action="candidate.evaluate.probe",
            resource_type="candidate",
            resource_id=cand.id,
            project_id=None,
            task_id=None,
            before={},
            after={"api_key": "sk-live-abcdef1234567890"},
            idempotency_key=f"probe:{cand.id}",
        )
        session.commit()
        probe = session.exec(
            select(AuditLog).where(
                AuditLog.idempotency_key == f"probe:{cand.id}"
            )
        ).first()
        assert probe is not None
        assert probe.after_snapshot["api_key"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# T-ZEROMIG-1 -- contract A: W3-A creates no schema change
# ---------------------------------------------------------------------------


def test_w3a_is_zero_migration(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = Config(root / "alembic.ini")
    cfg.set_main_option("script_location", str(root / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    # Still the single W2 head -- W3-A added no revision.
    assert heads == ["20260827_0002_workforce_candidate"]

    url = f"sqlite:///{(tmp_path / 'zeromig.db').as_posix()}"
    run_migrations(url)
    engine = get_engine(url)
    tables = set(inspect(engine).get_table_names())

    assert "candidate" in tables
    # None of the deferred W3-B/C/D tables leaked in.
    for deferred in (
        "benchmark",
        "benchmark_version",
        "benchmark_result",
        "match",
        "recommendation",
        "trial",
        "candidate_evaluation",
    ):
        assert deferred not in tables, f"{deferred} must not exist yet"

    cols = {c["name"] for c in inspect(engine).get_columns("candidate")}
    assert cols == {
        "id",
        "agent_id",
        "job_id",
        "job_version_id",
        "evaluation_context",
        "status",
        "discovered_by",
        "created_at",
        "updated_at",
    }


# ---------------------------------------------------------------------------
# T-REG-1 -- W2 behaviour is untouched
# ---------------------------------------------------------------------------


def test_w2_discovery_and_lifecycle_semantics_unchanged(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'reg1.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": 80})
        _, _, job, head = _build_chain(session, ("writing", 50, True))

        # Discovery still pools with an empty (W3-reserved) context.
        cands = discover_candidates(session, head.id)
        session.commit()
        assert len(cands) == 1
        assert cands[0].status == CandidateStatus.POOLED
        assert cands[0].evaluation_context == {}

        # reject -> repool round-trip still works.
        rejected = reject_candidate(session, cands[0].id, actor="reviewer")
        session.commit()
        assert rejected.status == CandidateStatus.REJECTED
        repooled = repool_candidate(session, cands[0].id)
        session.commit()
        assert repooled.status == CandidateStatus.POOLED

        # ...and an evaluation can then run from the re-pooled state.
        evaluated = evaluate_candidate(session, cands[0].id)
        session.commit()
        assert evaluated.status == CandidateStatus.EVALUATED
        assert CandidateLifecycle.ALLOWED[CandidateStatus.EVALUATED] == {
            CandidateStatus.REJECTED
        }
