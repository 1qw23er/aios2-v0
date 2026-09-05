"""Contract tests for Workforce W3-B -- Match / Ranking & Benchmark.

Scope: the scoring side channel layered on top of W3-A's frozen evaluation
snapshot (see ``docs/Workforce_W3B_Match_Benchmark_Spec_V1.md`` §6). W3-B MUST:

* never mutate ``Candidate.status`` or ``Candidate.evaluation_context`` (W3-A frozen,
  constraint 1) -- it only reads W3-A's ``capability_evidence`` and writes to the
  four W3-B tables;
* never fabricate scores: ``reliability`` / ``historical`` / ``cost`` stay excluded,
  and an untrusted/absent benchmark *waives* the dimension instead of writing 0
  (constraints 2 & F2);
* mark a capability gap ``blocked`` (F1) so W3-C can refuse to recommend;
* be idempotent (replay is a no-op; re-evaluation recomputes);
* be fully covered by a single-head, reversible Alembic migration (T-MIG-1).

Helpers mirror ``tests/test_workforce_evaluation_w3a.py`` so this file is
self-contained.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlmodel import Session, select

from aios.audit import AuditLog, append_audit
from aios.db import get_engine, run_migrations
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    BenchmarkResult,
    BenchmarkResultStatus,
    BenchmarkVersion,
    BusinessGoal,
    Candidate,
    CandidateStatus,
    Capability,
    Job,
    JobVersion,
    MatchStatus,
    RequiredWork,
)
from aios.services import ServiceError
from aios.workforce import (
    bind_job_version_benchmark,
    compute_match,
    create_benchmark,
    create_benchmark_version,
    evaluate_candidate,
    list_capability_requirements,
    rank_candidates,
    run_benchmark,
    update_benchmark_version_definition,
)
from alembic import command

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors test_workforce_evaluation_w3a.py)
# ---------------------------------------------------------------------------

ReqSpec = tuple[str, int, bool]  # (capability_name, min_proficiency, required)


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
    disabled: tuple[str, ...] = (),
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
                enabled=cap_name not in disabled,
            )
        )
    session.commit()
    return agent


def _build_chain(
    session: Session, *specs: ReqSpec
) -> tuple[BusinessGoal, RequiredWork, Job, JobVersion]:
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
    return goal, rw, job, head


def _pool(session: Session, head: JobVersion) -> Candidate:
    from aios.workforce import discover_candidates

    cands = discover_candidates(session, head.id)
    session.commit()
    assert len(cands) == 1, "fixture expects exactly one matching agent"
    return cands[0]


def _manual_candidate(
    session: Session, agent: Agent, job: Job, head: JobVersion
) -> Candidate:
    cand = Candidate(
        agent_id=agent.id,
        job_id=job.id,
        job_version_id=head.id,
        discovered_by="test_manual",
    )
    session.add(cand)
    session.commit()
    return cand


def _evaluate(session: Session, cand: Candidate) -> Candidate:
    out = evaluate_candidate(session, cand.id)
    session.commit()
    return out


def _audits(session: Session, action: str) -> list[AuditLog]:
    return list(
        session.exec(select(AuditLog).where(AuditLog.action == action)).all()
    )


# ---------------------------------------------------------------------------
# T-BENCH-1 -- benchmark_version immutability
# ---------------------------------------------------------------------------


def test_benchmark_version_is_immutable(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'bench1.db').as_posix()}"
    with _db(url) as session:
        bench = create_benchmark(session, name="write-quality")
        session.commit()
        bv1 = create_benchmark_version(
            session,
            benchmark_id=bench.id,
            definition_json={"cases": [{"id": 1}]},
            version=1,
        )
        session.commit()
        # Re-binding the same (benchmark_id, version) is rejected (UNIQUE).
        with pytest.raises(ServiceError) as exc:
            create_benchmark_version(
                session,
                benchmark_id=bench.id,
                definition_json={"cases": [{"id": 2}]},
                version=1,
            )
        assert exc.value.status_code == 409

        # The immutability guard refuses to mutate an existing version.
        with pytest.raises(ServiceError) as exc2:
            update_benchmark_version_definition(
                session, bv1.id, {"cases": [{"id": 999}]}
            )
        assert exc2.value.status_code == 409
        assert "immutable" in exc2.value.detail.lower()

        # The original definition is untouched (logical rejection held).
        still = session.get(BenchmarkVersion, bv1.id)
        assert still.definition_json == {"cases": [{"id": 1}]}

        # A new version is the only legal way to change the definition.
        bv2 = create_benchmark_version(
            session,
            benchmark_id=bench.id,
            definition_json={"cases": [{"id": 2}]},
            version=2,
        )
        session.commit()
        assert bv2.version == 2
        assert bv2.definition_json == {"cases": [{"id": 2}]}


# ---------------------------------------------------------------------------
# T-BENCH-2 -- fail-closed never writes a fake score
# ---------------------------------------------------------------------------


def test_benchmark_run_fails_closed_without_fabricating(tmp_path: Path) -> None:
        url = f"sqlite:///{(tmp_path / 'bench2.db').as_posix()}"
        with _db(url) as session:
            cap_writing = _seed_capability(session, "writing")
            cap_writing_id = cap_writing.id
            _seed_agent(session, "A", {"writing": 80})
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        cand = _pool(session, head)

        bench = create_benchmark(session, name="write-quality")
        bv = create_benchmark_version(
            session, benchmark_id=bench.id, definition_json={"cases": [1, 2]}
        )
        session.commit()

        # Default adapter has no execution backend -> untrusted -> unknown.
        result = run_benchmark(session, cand.id, bv.id, "run-1")
        session.commit()
        assert result.status == BenchmarkResultStatus.UNKNOWN
        assert result.passed_cases is None
        assert result.total_cases is None
        assert result.quality_score is None
        # Provenance is still recorded (traceable, not bit-exact).
        assert result.reproducibility_hash
        # The agent's capability snapshot is captured as provenance regardless of
        # whether the benchmark outcome is trusted -- this is NOT a fabricated score.
        assert isinstance(result.agent_snapshot_json, list)
        assert len(result.agent_snapshot_json) == 1
        snap0 = result.agent_snapshot_json[0]
        assert snap0["capability_id"] == cap_writing_id
        assert snap0["priority"] == 80
        assert snap0["enabled"] is True

        # An adapter that raises must ALSO land as unknown, never a fake number.
        class _BoomAdapter:
            def run(self, candidate, benchmark_version):
                raise RuntimeError("execution backend exploded")

        result2 = run_benchmark(
            session, cand.id, bv.id, "run-2", adapter=_BoomAdapter()
        )
        session.commit()
        assert result2.status == BenchmarkResultStatus.UNKNOWN
        assert result2.passed_cases is None


# ---------------------------------------------------------------------------
# T-BENCH-3 -- reproducibility hash is deterministic and input-sensitive
# ---------------------------------------------------------------------------


def test_benchmark_reproducibility_hash_is_deterministic(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'bench3.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A1", {"writing": 80})
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        c1 = _pool(session, head)  # exactly one matching agent (agent1)
        # Seed a second agent only AFTER discovery, then attach a manual candidate
        # with a different agent_id to exercise the agent-snapshot axis.
        agent2 = _seed_agent(session, "A2", {"writing": 40})
        c2 = _manual_candidate(session, agent2, job, head)

        bench = create_benchmark(session, name="write-quality")
        bv = create_benchmark_version(
            session, benchmark_id=bench.id, definition_json={"cases": [1]}
        )
        bv2 = create_benchmark_version(
            session, benchmark_id=bench.id, definition_json={"cases": [1, 2]}
        )
        session.commit()

        h_base = run_benchmark(
            session, c1.id, bv.id, "r", input_hash="h1"
        ).reproducibility_hash
        # Same five inputs -> identical hash (idempotent re-run).
        h_same = run_benchmark(
            session, c1.id, bv.id, "r", input_hash="h1"
        ).reproducibility_hash
        assert h_base == h_same

        # Change input_hash -> different hash.
        h_diff_input = run_benchmark(
            session, c1.id, bv.id, "r2", input_hash="h2"
        ).reproducibility_hash
        assert h_diff_input != h_base

        # Change agent (capability snapshot differs) -> different hash.
        h_diff_agent = run_benchmark(
            session, c2.id, bv.id, "r", input_hash="h1"
        ).reproducibility_hash
        assert h_diff_agent != h_base

        # Change benchmark version (case_set_hash differs) -> different hash.
        h_diff_version = run_benchmark(
            session, c1.id, bv2.id, "r", input_hash="h1"
        ).reproducibility_hash
        assert h_diff_version != h_base


# ---------------------------------------------------------------------------
# T-MATCH-1 -- explainability: breakdown + evidence_refs + evaluated_fields
# ---------------------------------------------------------------------------


def test_compute_match_is_explainable_and_scores_capability_fit(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'match1.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": 80})
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        cand = _evaluate(session, _pool(session, head))

        match = compute_match(session, cand.id, head.id)
        session.commit()

        # Unbound JobVersion -> benchmark_score waived, score == capability_fit.
        # (80-50)/(100-50) == 0.6
        assert match.score == pytest.approx(0.6)
        assert match.weights_version == "w3b.match.v1"
        assert match.status == MatchStatus.COMPUTED
        assert match.match_blocked_reason is None

        bd = match.breakdown
        assert bd["weights_version"] == "w3b.match.v1"
        assert bd["capability_fit"]["value"] == pytest.approx(0.6)
        assert bd["capability_fit"]["source"] == "evaluation_context.capability_evidence"
        assert bd["capability_fit"]["threshold_passed"] is True
        assert bd["benchmark_score"]["status"] == "waived"
        assert bd["benchmark_score"]["reason"] == "JobVersion unbound"
        assert "reliability(future_capability)" in bd["excluded"]

        assert match.evidence_refs == [f"cand:{cand.id}:attempt:1"]
        assert match.evaluated_fields == ["capability_fit"]


# ---------------------------------------------------------------------------
# T-MATCH-2 -- unbound normalization: score == capability_fit (single component)
# ---------------------------------------------------------------------------


def test_compute_match_unbound_normalizes_single_component(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'match2.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_capability(session, "research")
        # writing fit 0.6, research fit 1.0 -> aggregate capability_fit 0.8
        _seed_agent(session, "A", {"writing": 80, "research": 100})
        _, _, job, head = _build_chain(
            session, ("writing", 50, True), ("research", 50, True)
        )
        cand = _evaluate(session, _pool(session, head))

        match = compute_match(session, cand.id, head.id)
        session.commit()

        assert match.score == pytest.approx(0.8)  # == capability_fit, no benchmark
        assert match.evaluated_fields == ["capability_fit"]
        assert match.breakdown["benchmark_score"]["status"] == "waived"


# ---------------------------------------------------------------------------
# T-MATCH-3 -- F1: capability gap -> blocked, ranks last
# ---------------------------------------------------------------------------


def test_compute_match_blocks_capability_gap(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'match3.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        # Declared but DISABLED -> priority 0 -> threshold not passed.
        agent = _seed_agent(session, "A", {"writing": 80}, disabled=("writing",))
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        cand = _evaluate(session, _manual_candidate(session, agent, job, head))

        match = compute_match(session, cand.id, head.id)
        session.commit()

        # Blocked: capability_fit is 0, but the Match is produced and marked.
        assert match.status == MatchStatus.BLOCKED
        assert match.match_blocked_reason == "capability_gap"
        assert match.score == pytest.approx(0.0)

        # Ranking still returns it, but last (after computed candidates).
        ranking = rank_candidates(session, head.id)
        assert ranking[-1].id == match.id


# ---------------------------------------------------------------------------
# T-MATCH-4 -- bound + trusted recorded result folds in benchmark_score
# ---------------------------------------------------------------------------


def test_compute_match_folds_in_recorded_benchmark(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'match4.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": 80})
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        cand = _evaluate(session, _pool(session, head))

        bench = create_benchmark(session, name="write-quality")
        bv = create_benchmark_version(
            session, benchmark_id=bench.id, definition_json={"cases": [1]}
        )
        bind_job_version_benchmark(session, head.id, bv.id)
        session.commit()

        # Inject a trusted recorded result: 8/10 passed -> 0.8
        br = BenchmarkResult(
            candidate_id=cand.id,
            benchmark_version_id=bv.id,
            run_id="run-1",
            passed_cases=8,
            total_cases=10,
            quality_score=0.8,
            input_hash="in1",
            agent_snapshot_json=[],
            reproducibility_hash="rh",
            status=BenchmarkResultStatus.RECORDED,
        )
        session.add(br)
        session.commit()

        match = compute_match(session, cand.id, head.id)
        session.commit()

        # score = 0.6*0.6 + 0.4*0.8 = 0.36 + 0.32 = 0.68
        assert match.score == pytest.approx(0.68)
        assert match.evaluated_fields == ["capability_fit", "benchmark_score"]
        assert match.breakdown["benchmark_score"]["status"] == "computed"
        assert match.breakdown["benchmark_score"]["value"] == pytest.approx(0.8)
        assert f"br:{br.id}" in match.evidence_refs


# ---------------------------------------------------------------------------
# T-RANK-1 -- ordering: score desc, tie-break, blocked last
# ---------------------------------------------------------------------------


def test_rank_candidates_orders_by_score_and_puts_blocked_last(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{(tmp_path / 'rank1.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_capability(session, "research")
        # Two computed candidates with identical score (tie) to test agent_id tie-break.
        a_hi = _seed_agent(session, "hi", {"writing": 90})  # fit (90-50)/50 = 0.8
        a_tie_a = _seed_agent(session, "tie_a", {"writing": 80})  # fit 0.6
        a_tie_b = _seed_agent(session, "tie_b", {"writing": 80})  # fit 0.6
        a_block = _seed_agent(
            session, "block", {"writing": 80}, disabled=("writing",)
        )  # blocked
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        session.commit()

        c_hi = _evaluate(session, _pool_via(session, head, a_hi))
        c_tie_a = _evaluate(session, _pool_via(session, head, a_tie_a))
        c_tie_b = _evaluate(session, _pool_via(session, head, a_tie_b))
        c_block = _evaluate(
            session, _manual_candidate(session, a_block, job, head)
        )

        compute_match(session, c_hi.id, head.id)
        compute_match(session, c_tie_a.id, head.id)
        compute_match(session, c_tie_b.id, head.id)
        m_block = compute_match(session, c_block.id, head.id)
        session.commit()

        ranking = rank_candidates(session, head.id)
        ids = [m.id for m in ranking]

        # computed (0.8) first, then the two ties (0.6), blocked strictly last.
        assert ranking[0].candidate_id == c_hi.id
        assert ranking[-1].id == m_block.id
        assert m_block.status == MatchStatus.BLOCKED

        # Tie-break by agent_id (ascending) for the two 0.6 candidates.
        tie_ids = {m.candidate_id for m in ranking[1:3]}
        assert tie_ids == {c_tie_a.id, c_tie_b.id}
        first_tie = ranking[1]
        second_tie = ranking[2]
        # agent_id string order decides; both have score 0.6
        assert first_tie.candidate_id in (c_tie_a.id, c_tie_b.id)
        # verify deterministic ordering by candidate.agent_id
        cand_first = session.get(Candidate, first_tie.candidate_id)
        cand_second = session.get(Candidate, second_tie.candidate_id)
        assert cand_first.agent_id < cand_second.agent_id
        assert ids[0] == ranking[0].id  # sanity


def _pool_via(session: Session, head: JobVersion, agent: Agent) -> Candidate:
    """Pool the single candidate that discovery surfaces for ``agent``.

    Discovery returns one candidate per matching agent; we keep the one whose
    agent_id matches.
    """
    from aios.workforce import discover_candidates

    cands = discover_candidates(session, head.id)
    session.commit()
    match = [c for c in cands if c.agent_id == agent.id]
    assert len(match) == 1, f"expected one candidate for agent {agent.id}"
    return match[0]


# ---------------------------------------------------------------------------
# T-IDEM-1 -- idempotency: replay is a no-op (no extra rows / audits)
# ---------------------------------------------------------------------------


def test_compute_match_and_run_benchmark_are_idempotent(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'idem1.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": 80})
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        cand = _evaluate(session, _pool(session, head))

        m1 = compute_match(session, cand.id, head.id)
        session.commit()
        computed_before = len(_audits(session, "match.computed"))

        m2 = compute_match(session, cand.id, head.id)  # replay
        session.commit()
        computed_after = len(_audits(session, "match.computed"))

        assert m1.id == m2.id
        assert m1.score == m2.score
        assert computed_after == computed_before  # no duplicate audit

        bench = create_benchmark(session, name="write-quality")
        bv = create_benchmark_version(
            session, benchmark_id=bench.id, definition_json={"cases": [1]}
        )
        session.commit()

        r1 = run_benchmark(session, cand.id, bv.id, "run-1")
        session.commit()
        run_before = len(_audits(session, "benchmark.run"))

        r2 = run_benchmark(session, cand.id, bv.id, "run-1")  # replay
        session.commit()
        run_after = len(_audits(session, "benchmark.run"))

        assert r1.id == r2.id
        assert run_after == run_before


# ---------------------------------------------------------------------------
# T-AUDIT-1 -- audit trail + redact_secrets active
# ---------------------------------------------------------------------------


def test_w3b_audit_trail_and_redaction(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'audit1.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": 80})
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        cand = _evaluate(session, _pool(session, head))

        match = compute_match(session, cand.id, head.id)
        session.commit()
        m_audits = _audits(session, "match.computed")
        assert len(m_audits) == 1
        assert m_audits[0].resource_id == match.id
        assert m_audits[0].after_snapshot["score"] == pytest.approx(0.6)

        bench = create_benchmark(session, name="write-quality")
        bv = create_benchmark_version(
            session, benchmark_id=bench.id, definition_json={"cases": [1]}
        )
        session.commit()
        result = run_benchmark(session, cand.id, bv.id, "run-1")
        session.commit()
        b_audits = _audits(session, "benchmark.run")
        assert len(b_audits) == 1
        assert b_audits[0].resource_id == result.id
        assert b_audits[0].after_snapshot["status"] == "unknown"

        # redact_secrets contract: a matching key is redacted on write.
        append_audit(
            session,
            actor="test",
            action="w3b.redact.check",
            resource_type="x",
            resource_id="x",
            project_id=None,
            task_id=None,
            before={},
            after={"api_key": "sk-live-secret-value", "note": "ok"},
            idempotency_key="w3b-redact-1",
        )
        session.commit()
        redacted = _audits(session, "w3b.redact.check")[0]
        assert redacted.after_snapshot["api_key"] == "[REDACTED]"
        assert redacted.after_snapshot["note"] == "ok"


# ---------------------------------------------------------------------------
# T-REG-1 -- W3-B reads but NEVER writes Candidate.status / evaluation_context
# ---------------------------------------------------------------------------


def test_w3b_does_not_mutate_candidate_state_or_context(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'reg1.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", {"writing": 80})
        _, _, job, head = _build_chain(session, ("writing", 50, True))
        cand = _evaluate(session, _pool(session, head))

        assert cand.status == CandidateStatus.EVALUATED
        assert "capability_evidence" in cand.evaluation_context
        frozen_status = cand.status
        frozen_ctx = dict(cand.evaluation_context)

        compute_match(session, cand.id, head.id)
        session.commit()

        # Reload: W3-B must not have touched the candidate row.
        reloaded = session.get(Candidate, cand.id)
        assert reloaded.status == frozen_status == CandidateStatus.EVALUATED
        assert reloaded.evaluation_context == frozen_ctx
        # And the EVALUATED candidate is still queryable / rankable.
        assert rank_candidates(session, head.id)[0].candidate_id == cand.id


# ---------------------------------------------------------------------------
# T-MIG-1 -- single-head, additive, reversible Alembic migration
# ---------------------------------------------------------------------------


def test_w3b_migration_is_single_head_additive_reversible(
    tmp_path: Path, real_run_migrations
) -> None:
    # Single (linear) head; advanced past the W3-D Trial leaf by W5 Cost
    # Evidence. W3-B tables remain additive
    cfg = Config(ROOT / "alembic.ini")
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == ["20260904_0001_workforce_cost_evidence"]

    db_path = tmp_path / "mig.db"
    url = f"sqlite:///{db_path.as_posix()}"
    real_run_migrations(url)

    import sqlite3

    conn = sqlite3.connect(str(db_path))
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        )
    }
    for t in ("benchmark", "benchmark_version", "benchmark_result", "match"):
        assert t in tables, f"missing table {t}"
    jv_cols = [r[1] for r in conn.execute("PRAGMA table_info(job_version)")]
    assert "benchmark_version_id" in jv_cols
    version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert version == "20260904_0001_workforce_cost_evidence"
    conn.close()

    # Reversible: downgrade to the prior W3-A head removes the 4 tables + column.
    cfg.set_main_option("sqlalchemy.url", url)
    command.downgrade(cfg, "20260827_0002_workforce_candidate")

    conn = sqlite3.connect(str(db_path))
    tables_after = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        )
    }
    for t in ("benchmark", "benchmark_version", "benchmark_result", "match"):
        assert t not in tables_after, f"{t} should be dropped"
    jv_cols_after = [r[1] for r in conn.execute("PRAGMA table_info(job_version)")]
    assert "benchmark_version_id" not in jv_cols_after
    version_after = conn.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone()[0]
    assert version_after == "20260827_0002_workforce_candidate"
    conn.close()
