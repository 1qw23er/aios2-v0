"""Contract tests for the W1 Workforce core entities (V1.1 minimum closed loop).

These assert the structural + behavioural contracts that W1 must guarantee before
any W2+ work (Candidate / Evaluation / Match / Employee) can be built on top:

1. The derivation chain BusinessGoal -> RequiredWork -> Job -> JobVersion ->
   CapabilityRequirement persists and links correctly.
2. Job is the first-class citizen (creatable/queryable directly; head wired up).
3. JobVersion gives immutable historical traceability of requirement changes.
4. CapabilityRequirement references the Alpha-1 Capability SSoT ONLY; an unknown
   capability slug is rejected fail-closed (422) -- no second vocabulary.
5. Foreign-key cascade deletes the whole subtree from a BusinessGoal.
6. Historical (non-head) JobVersions are immutable; editing them is rejected (409).
7. The alembic migration is additive and remains the single head.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from aios.audit import AuditLog
from aios.db import get_engine, run_migrations
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    BusinessGoal,
    Candidate,
    CandidateStatus,
    Capability,
    CapabilityRequirement,
    Job,
    JobVersion,
    RequiredWork,
)
from aios.services import ServiceError
from aios.workforce import (
    CandidateLifecycle,
    add_capability_requirement,
    create_business_goal,
    create_job,
    create_job_version,
    create_required_work,
    discover_candidates,
    list_capability_requirements,
    list_job_versions,
    reject_candidate,
    repool_candidate,
)


def _db(url: str) -> Session:
    run_migrations(url)
    return Session(get_engine(url))


def _seed_capability(session: Session, name: str) -> Capability:
    cap = Capability(name=name, description=f"{name} capability")
    session.add(cap)
    session.flush()
    return cap


def _seed_agent(session: Session, name: str, *capability_names: str) -> Agent:
    """Register an enabled agent that declares ``capability_names`` (via AgentCapability)."""
    agent = Agent(name=name, role=name, adapter_type=AdapterType.EXTERNAL)
    session.add(agent)
    session.flush()
    for cap_name in capability_names:
        cap = session.exec(
            select(Capability).where(Capability.name == cap_name)
        ).first()
        assert cap is not None, f"capability must be seeded first: {cap_name}"
        session.add(
            AgentCapability(agent_id=agent.id, capability_id=cap.id, enabled=True)
        )
    session.flush()
    return agent


def _build_chain(session: Session, *capability_names: str) -> tuple[
    BusinessGoal, RequiredWork, Job, JobVersion
]:
    goal = create_business_goal(session, "增长北极星", target_outcome="新增注册 +20%")
    rw = create_required_work(
        session, goal.id, "公众号内容生产", rationale="内容带来自然注册"
    )
    job = create_job(
        session,
        rw.id,
        "内容初稿研究员",
        role_summary="把选题做成初稿",
        capability_names=list(capability_names),
    )
    session.commit()
    head = session.get(JobVersion, job.head_version_id)
    assert head is not None
    return goal, rw, job, head


# ---------------------------------------------------------------------------
# 1. Chain persistence
# ---------------------------------------------------------------------------


def test_full_chain_persists_and_links(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'chain.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_capability(session, "research")
        goal, rw, job, head = _build_chain(session, "writing", "research")
        # Capture ids, then close this session entirely.
        goal_id, rw_id, job_id, head_id = goal.id, rw.id, job.id, head.id

    # A fresh session against the same DB proves real persistence + linkage.
    with Session(get_engine(url)) as session2:
        job2 = session2.get(Job, job_id)
        assert job2 is not None
        assert job2.required_work_id == rw_id
        assert job2.head_version_id == head_id

        rw2 = session2.get(RequiredWork, rw_id)
        assert rw2.business_goal_id == goal_id

        vers = list_job_versions(session2, job_id)
        assert len(vers) == 1 and vers[0].version == 1
        crs = list_capability_requirements(session2, vers[0].id)
        assert {c.capability_name for c in crs} == {"writing", "research"}


# ---------------------------------------------------------------------------
# 2. Job is the first-class citizen
# ---------------------------------------------------------------------------


def test_job_is_first_class_citizen(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'job.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        goal = create_business_goal(session, "G1")
        rw = create_required_work(session, goal.id, "RW1")
        job = create_job(session, rw.id, "内容初稿研究员", capability_names=["writing"])
        session.commit()

        # Job can be fetched directly by id (not only via parents).
        direct = session.get(Job, job.id)
        assert direct is not None
        assert direct.title == "内容初稿研究员"
        # And it already owns a head version wired up at creation time.
        assert direct.head_version_id is not None
        head = session.get(JobVersion, direct.head_version_id)
        assert head is not None and head.job_id == direct.id


# ---------------------------------------------------------------------------
# 3. JobVersion historical traceability
# ---------------------------------------------------------------------------


def test_job_version_history_is_traceable(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'history.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_capability(session, "research")
        _seed_capability(session, "editing")
        _, _, job, v1 = _build_chain(session, "writing", "research")

        # Revise: drop "research", add "editing", keep "writing".
        v2 = create_job_version(session, job.id, capability_names=["writing", "editing"])
        session.commit()

        versions = list_job_versions(session, job.id)
        assert [v.version for v in versions] == [1, 2]
        # Head advanced to v2.
        assert job.head_version_id == v2.id

        v1_crs = {c.capability_name for c in list_capability_requirements(session, v1.id)}
        v2_crs = {c.capability_name for c in list_capability_requirements(session, v2.id)}
        # v1 is immutable: still the original requirement set.
        assert v1_crs == {"writing", "research"}
        # v2 reflects the revision.
        assert v2_crs == {"writing", "editing"}

        # Fork (no capability_names) preserves the current requirement set exactly.
        v3 = create_job_version(session, job.id)
        session.commit()
        v3_crs = {c.capability_name for c in list_capability_requirements(session, v3.id)}
        assert v3_crs == {"writing", "editing"}


# ---------------------------------------------------------------------------
# 4. CapabilityRequirement references Alpha-1 Capability SSoT ONLY (422)
# ---------------------------------------------------------------------------


def test_unknown_capability_is_rejected_fail_closed(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'ssoT.db').as_posix()}"
    with _db(url) as session:
        goal = create_business_goal(session, "G")
        rw = create_required_work(session, goal.id, "RW")
        # No capability seeded -> any slug is unknown -> 422 fail-closed.
        with pytest.raises(ServiceError) as exc:
            create_job(session, rw.id, "J", capability_names=["nonexistent_cap"])
        assert exc.value.status_code == 422
        assert "unknown capability" in exc.value.detail


def test_capability_requirement_links_alpha1_capability(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'link.db').as_posix()}"
    with _db(url) as session:
        cap = _seed_capability(session, "writing")
        _, _, job, head = _build_chain(session, "writing")
        crs = list_capability_requirements(session, head.id)
        assert len(crs) == 1
        cr = crs[0]
        # The requirement points at the Alpha-1 capability row, not a local copy.
        assert cr.capability_id == cap.id
        assert cr.capability_name == "writing"
        stored = session.get(Capability, cr.capability_id)
        assert stored is not None and stored.id == cap.id


def test_min_proficiency_bounds_enforced(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'bounds.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        goal = create_business_goal(session, "G")
        rw = create_required_work(session, goal.id, "RW")
        job = create_job(session, rw.id, "J", capability_names=["writing"])
        session.commit()
        head = session.get(JobVersion, job.head_version_id)
        assert head is not None
        # Out-of-range proficiency must be rejected by the DB CHECK constraint.
        with pytest.raises(IntegrityError):
            add_capability_requirement(
                session, head.id, "writing", min_proficiency=101
            )
            session.commit()


# ---------------------------------------------------------------------------
# 5. Cascade delete from BusinessGoal
# ---------------------------------------------------------------------------


def test_cascade_delete_removes_subtree(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'cascade.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")

        goal, rw, job, head = _build_chain(session, "writing")
        crs = list_capability_requirements(session, head.id)
        assert len(crs) == 1
        goal_id, rw_id, job_id, head_id, cr_id = (
            goal.id,
            rw.id,
            job.id,
            head.id,
            crs[0].id,
        )

        # Delete the root; the whole subtree must disappear via FK cascade.
        session.delete(goal)
        session.commit()

        assert session.get(BusinessGoal, goal_id) is None
        assert session.get(RequiredWork, rw_id) is None
        assert session.get(Job, job_id) is None
        assert session.get(JobVersion, head_id) is None
        assert session.get(CapabilityRequirement, cr_id) is None
        # The Alpha-1 capability catalog is independent and must survive.
        assert session.exec(select(func.count()).select_from(Capability)).first() == 1


# ---------------------------------------------------------------------------
# 6. Historical (non-head) JobVersion is immutable
# ---------------------------------------------------------------------------


def test_historical_version_is_immutable(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'immutable.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_capability(session, "editing")
        _, _, job, v1 = _build_chain(session, "writing")
        create_job_version(session, job.id, capability_names=["writing", "editing"])
        session.commit()

        # v1 is now historical (not the head). Editing it must be rejected.
        with pytest.raises(ServiceError) as exc:
            add_capability_requirement(session, v1.id, "editing")
        assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# 8. W1 hardening: UNIQUE(job_version_id, capability_id) + RESTRICT on Capability FK
# ---------------------------------------------------------------------------


def test_duplicate_capability_requirement_is_rejected(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'dup.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _, _, job, head = _build_chain(session, "writing")
        # head already requires "writing" once. A second requirement for the same
        # capability on the same JobVersion must be rejected by the DB UNIQUE
        # constraint (the service does NOT de-dup).
        with pytest.raises(IntegrityError):
            add_capability_requirement(session, head.id, "writing")
            session.commit()


def test_delete_referenced_capability_is_rejected_restrict(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'restrict.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_capability(session, "research")
        _, _, _, head = _build_chain(session, "writing", "research")
        # "writing" is referenced by a Workforce requirement. Retiring it must
        # FAIL EXPLICITLY (RESTRICT) -- we must never silently wipe hiring history.
        referenced = session.exec(
            select(Capability).where(Capability.name == "writing")
        ).first()
        assert referenced is not None
        with pytest.raises(IntegrityError):
            session.delete(referenced)
            session.commit()


def test_delete_unreferenced_capability_succeeds(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'unref.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "editing")  # never referenced by any requirement
        _build_chain(session)  # job with no capability requirements
        orphan = session.exec(
            select(Capability).where(Capability.name == "editing")
        ).first()
        assert orphan is not None
        # RESTRICT only blocks when a child row references the capability, so an
        # unreferenced Capability can be safely retired.
        session.delete(orphan)
        session.commit()
        remaining = session.exec(select(func.count()).select_from(Capability)).first()
        assert remaining == 0


# ---------------------------------------------------------------------------
# 7. Alembic single-head + additive
# ---------------------------------------------------------------------------


def test_alembic_single_head_is_capreq_hardening() -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = Config(root / "alembic.ini")
    cfg.set_main_option("script_location", str(root / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    # Single linear head; W3-B Match/Benchmark advances it past the W2 capreq
    # hardening head (20260827_0002_workforce_candidate) to 20260901_0001.
    assert heads == ["20260901_0001_workforce_match_benchmark"]


def test_migration_creates_workforce_tables_additively(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'mig.db').as_posix()}"
    run_migrations(url)
    engine = get_engine(url)
    tables = set(inspect(engine).get_table_names())
    # New W1 tables present...
    for t in (
        "business_goal",
        "required_work",
        "job",
        "job_version",
        "capability_requirement",
    ):
        assert t in tables, f"missing table: {t}"
    # ...the W2 candidate pool table is present (additive, on top of W1)...
    assert "candidate" in tables
    # ...and the Alpha-1 SSoT table is untouched (proves additivity).
    assert "capability" in tables
    # No second capability vocabulary table was created (W1 + W2 both reuse Alpha-1).
    assert not any(t.startswith("workforce_capability") for t in tables)
    # The candidate table references the Agent Registry SSoT by id only -- it does
    # NOT copy agent columns (no agent_name / agent_role / capabilities blob).
    cols = {c["name"] for c in inspect(engine).get_columns("candidate")}
    assert {
        "id",
        "agent_id",
        "job_id",
        "job_version_id",
        "evaluation_context",
        "status",
        "discovered_by",
        "created_at",
        "updated_at",
    } <= cols
    assert not cols & {"agent_name", "agent_role", "agent_capabilities"}


# ---------------------------------------------------------------------------
# 9. W2 Candidate Discovery (pool only; no evaluation / match / trial)
# ---------------------------------------------------------------------------


def test_discover_pools_only_fully_matching_agents(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'disc.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_capability(session, "research")

        # Three agents: full match / partial / none.
        a_full = _seed_agent(session, "A-full", "writing", "research")
        _seed_agent(session, "B-partial", "writing")
        _seed_agent(session, "C-none")

        _, _, job, head = _build_chain(session, "writing", "research")
        cands = discover_candidates(session, head.id)
        session.commit()

        # Only the agent declaring BOTH required capabilities is pooled.
        assert len(cands) == 1
        c = cands[0]
        assert c.agent_id == a_full.id
        assert c.job_id == job.id
        assert c.job_version_id == head.id
        assert c.status == CandidateStatus.POOLED
        # evaluation_context is reserved for W3 and empty in W2.
        assert c.evaluation_context == {}


def test_discover_is_idempotent(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'idem.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", "writing")
        _, _, job, head = _build_chain(session, "writing")

        first = discover_candidates(session, head.id)
        session.commit()
        first_id = first[0].id

        # Re-run discovery: must NOT create a duplicate candidate row.
        second = discover_candidates(session, head.id)
        session.commit()
        assert len(second) == 1
        assert second[0].id == first_id
        # Exactly one candidate row exists for this job version.
        assert (
            session.exec(
                select(func.count())
                .select_from(Candidate)
                .where(Candidate.job_version_id == head.id)
            ).first()
            == 1
        )


def test_discover_unknown_job_version_404(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'unk.db').as_posix()}"
    with _db(url) as session:
        with pytest.raises(ServiceError) as exc:
            discover_candidates(session, "no_such_job_version")
        assert exc.value.status_code == 404


def test_discover_empty_requirements_422(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'empty.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", "writing")
        # Job with NO capability requirements.
        _, _, job, head = _build_chain(session)
        assert list_capability_requirements(session, head.id) == []
        with pytest.raises(ServiceError) as exc:
            discover_candidates(session, head.id)
        assert exc.value.status_code == 422


def test_discover_audit_trail_traceable_to_job_version(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'audit.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        a = _seed_agent(session, "A", "writing")
        _, _, job, head = _build_chain(session, "writing")
        cands = discover_candidates(session, head.id, discoverer="tester")
        session.commit()
        cand_id = cands[0].id

        log = session.exec(
            select(AuditLog)
            .where(AuditLog.resource_type == "candidate")
            .where(AuditLog.action == "candidate.discover")
            .where(AuditLog.resource_id == cand_id)
        ).first()
        assert log is not None
        assert log.actor == "tester"
        # Traceable back to the originating job version.
        assert log.after_snapshot.get("job_version_id") == head.id
        assert log.after_snapshot.get("agent_id") == a.id
        assert log.after_snapshot.get("status") == CandidateStatus.POOLED.value


def test_discover_does_not_copy_agent_registry_data(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'nocopy.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        a = _seed_agent(session, "A", "writing")
        _, _, job, head = _build_chain(session, "writing")
        cands = discover_candidates(session, head.id)
        session.commit()
        c = cands[0]

        # Candidate references the agent by id only; it has no agent columns.
        assert c.agent_id == a.id
        assert not hasattr(c, "agent_name")
        # Mutating the registry agent does not move/alter the candidate.
        a.name = "A-renamed"
        session.add(a)
        session.commit()
        same = session.get(Candidate, c.id)
        assert same is not None and same.agent_id == a.id
        assert same.status == CandidateStatus.POOLED


def test_candidate_reject_and_repool_boundary(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'rej.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", "writing")
        _, _, job, head = _build_chain(session, "writing")
        cands = discover_candidates(session, head.id)
        session.commit()
        cand_id = cands[0].id

        # POOLED -> REJECTED is allowed.
        rej = reject_candidate(session, cand_id, actor="reviewer")
        session.commit()
        assert rej.status == CandidateStatus.REJECTED

        # REJECTED -> POOLED (re-pool) is allowed.
        rep = repool_candidate(session, cand_id)
        session.commit()
        assert rep.status == CandidateStatus.POOLED


def test_candidate_illegal_transition_rejected_409(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'illegal.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", "writing")
        _, _, job, head = _build_chain(session, "writing")
        cands = discover_candidates(session, head.id)
        session.commit()
        cand_id = cands[0].id

        # REJECTED -> REJECTED is not a defined transition.
        reject_candidate(session, cand_id)
        session.commit()
        with pytest.raises(ServiceError) as exc:
            reject_candidate(session, cand_id)  # already REJECTED
        assert exc.value.status_code == 409

        # The state machine itself rejects re-entering the same state.
        with pytest.raises(ServiceError) as exc2:
            CandidateLifecycle.require_transition(
                CandidateStatus.POOLED, CandidateStatus.POOLED
            )
        assert exc2.value.status_code == 409

        # -- W3-A: edges the evaluation loop ADDED (must be legal) ------------
        # POOLED -> EVALUATING: entering the evaluation loop.
        CandidateLifecycle.require_transition(
            CandidateStatus.POOLED, CandidateStatus.EVALUATING
        )
        # EVALUATING -> EVALUATED: the evaluation completed.
        CandidateLifecycle.require_transition(
            CandidateStatus.EVALUATING, CandidateStatus.EVALUATED
        )
        # EVALUATING -> POOLED: a failed evaluation rolls out of the half-state.
        CandidateLifecycle.require_transition(
            CandidateStatus.EVALUATING, CandidateStatus.POOLED
        )
        # EVALUATED -> REJECTED: a completed evaluation can still be discarded.
        CandidateLifecycle.require_transition(
            CandidateStatus.EVALUATED, CandidateStatus.REJECTED
        )
        # REJECTED -> POOLED: re-pool (W2 edge, still the way back in).
        CandidateLifecycle.require_transition(
            CandidateStatus.REJECTED, CandidateStatus.POOLED
        )

        # -- W3-A: edges that stay illegal (409) ------------------------------
        illegal_edges = [
            # A rejected candidate must be re-pooled before it can be evaluated.
            (CandidateStatus.REJECTED, CandidateStatus.EVALUATING),
            # Evaluation cannot be short-circuited: POOLED must pass EVALUATING.
            (CandidateStatus.POOLED, CandidateStatus.EVALUATED),
            # EVALUATED is an immutable snapshot -- no silent rollback to POOLED.
            # Re-evaluating means EVALUATED -> REJECTED -> POOLED -> EVALUATING.
            (CandidateStatus.EVALUATED, CandidateStatus.POOLED),
            # RECOMMENDED remains unreachable until the W3-C/D Match gate lands.
            (CandidateStatus.EVALUATED, CandidateStatus.RECOMMENDED),
            (CandidateStatus.POOLED, CandidateStatus.RECOMMENDED),
            (CandidateStatus.RECOMMENDED, CandidateStatus.POOLED),
        ]
        for source, target in illegal_edges:
            with pytest.raises(ServiceError) as exc3:
                CandidateLifecycle.require_transition(source, target)
            assert exc3.value.status_code == 409, f"{source} -> {target} must be 409"

        # No self-loop anywhere in the W3-A state set (an evaluation that
        # "completes into itself" would erase the crash-recovery signal).
        for state in (
            CandidateStatus.EVALUATING,
            CandidateStatus.EVALUATED,
            CandidateStatus.RECOMMENDED,
        ):
            with pytest.raises(ServiceError) as exc4:
                CandidateLifecycle.require_transition(state, state)
            assert exc4.value.status_code == 409, f"{state} -> {state} must be 409"


def test_candidate_cascade_on_job_delete(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'cascade2.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_agent(session, "A", "writing")
        _, _, job, head = _build_chain(session, "writing")
        discover_candidates(session, head.id)
        session.commit()
        job_id = job.id
        assert session.exec(select(func.count()).select_from(Candidate)).first() == 1

        # Deleting the owning Job cascades through JobVersion to its candidates
        # (hard FK on candidate.job_version_id). Traceability lives *within* the
        # Job lifecycle, so removing the Job removes its whole candidate pool.
        session.delete(session.get(Job, job_id))
        session.commit()
        assert (
            session.exec(
                select(func.count())
                .select_from(Candidate)
                .where(Candidate.job_id == job_id)
            ).first()
            == 0
        )


def test_candidate_agent_fk_no_action_blocks_agent_delete(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'noaction.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        a = _seed_agent(session, "A", "writing")
        _, _, job, head = _build_chain(session, "writing")
        discover_candidates(session, head.id)
        session.commit()
        agent_id = a.id

        # Candidate references agent with NO ACTION: deleting the (still-pooled)
        # agent must FAIL explicitly (IntegrityError), never silently wipe history.
        with pytest.raises(IntegrityError):
            session.delete(session.get(Agent, agent_id))
            session.commit()
        # The failed delete was rolled back; recover the session before verifying.
        session.rollback()
        # Candidate still present afterwards.
        assert (
            session.exec(
                select(func.count())
                .select_from(Candidate)
                .where(Candidate.agent_id == agent_id)
            ).first()
            == 1
        )


# ---------------------------------------------------------------------------
# 10. W2 hardening -- concurrent discovery is strictly idempotent (P2-1)
# ---------------------------------------------------------------------------


def test_discover_no_matching_agent_returns_empty_list(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'nomatch.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        _seed_capability(session, "research")
        # Agent declares only "writing"; the Job requires BOTH -> no full match.
        _seed_agent(session, "A", "writing")
        _, _, job, head = _build_chain(session, "writing", "research")

        # Must return an empty list (not an error). The 422 only fires when there
        # are NO requirements; here requirements exist, just no qualifying agent.
        cands = discover_candidates(session, head.id)
        session.commit()
        assert cands == []
        # And no candidate row was created.
        assert (
            session.exec(
                select(func.count())
                .select_from(Candidate)
                .where(Candidate.job_version_id == head.id)
            ).first()
            == 0
        )


def test_discover_concurrent_no_duplicate_and_loser_returns_existing(
    tmp_path: Path,
) -> None:
    """Two simultaneous discoveries of the same job version must yield exactly one
    candidate row, and the losing caller must still return the pooled candidate
    (not raise)."""
    url = f"sqlite:///{(tmp_path / 'concurrent.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        a = _seed_agent(session, "A", "writing")
        _, _, job, head = _build_chain(session, "writing")
        head_id = head.id
        agent_id = a.id

    barrier = threading.Barrier(2)
    results: dict[int, object] = {}

    def worker(idx: int) -> None:
        try:
            barrier.wait()
            with Session(get_engine(url)) as s:
                cands = discover_candidates(s, head_id)
                results[idx] = [c.agent_id for c in cands]
        except Exception as exc:  # pragma: no cover - defensive
            results[idx] = ("ERR", repr(exc))

    t1 = threading.Thread(target=worker, args=(0,), daemon=True)
    t2 = threading.Thread(target=worker, args=(1,), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert "ERR" not in str(results.get(0)), results.get(0)
    assert "ERR" not in str(results.get(1)), results.get(1)
    # Both callers return the single pooled candidate (the winner's committed row).
    assert results[0] == [agent_id]
    assert results[1] == [agent_id]
    # Exactly one candidate row exists -- no duplicate despite the race.
    with Session(get_engine(url)) as v:
        assert (
            v.exec(
                select(func.count())
                .select_from(Candidate)
                .where(Candidate.job_version_id == head_id)
            ).first()
            == 1
        )


def test_discover_absorbs_concurrent_duplicate_via_savepoint(
    tmp_path: Path,
) -> None:
    """Deterministic P2-1 check: a held (uncommitted) duplicate row forces the
    second discovery to hit the UNIQUE constraint. The SAVEPOINT rollback + fresh
    read-back must absorb it and return the already-pooled candidate -- never a 500
    and never a second row."""
    url = f"sqlite:///{(tmp_path / 'absorb.db').as_posix()}"
    with _db(url) as session:
        _seed_capability(session, "writing")
        a = _seed_agent(session, "A", "writing")
        _, _, job, head = _build_chain(session, "writing")
        head_id = head.id
        agent_id = a.id
        job_id = job.id

    # Session 1 holds an uncommitted, duplicate candidate row (and the write lock).
    s1 = Session(get_engine(url))
    s1.add(
        Candidate(
            agent_id=agent_id,
            job_id=job_id,
            job_version_id=head_id,
            status=CandidateStatus.POOLED,
            discovered_by="held",
            evaluation_context={},
        )
    )
    s1.flush()  # uncommitted: blocks session 2's flush until we commit

    result: dict[str, object] = {}

    def worker() -> None:
        try:
            with Session(get_engine(url)) as s2:
                cands = discover_candidates(s2, head_id)
                result["list"] = [c.agent_id for c in cands]
        except Exception as exc:  # pragma: no cover - defensive
            result["err"] = repr(exc)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    # Let the worker reach (and block on) its flush, then release the lock.
    time.sleep(0.5)
    s1.commit()
    t.join(timeout=60)
    s1.close()

    assert "err" not in result, result.get("err")
    # The losing caller returns the existing candidate, not a 500 / empty list.
    assert result.get("list") == [agent_id]
    # No duplicate row was created; the held row is the only one.
    with Session(get_engine(url)) as v:
        assert (
            v.exec(
                select(func.count())
                .select_from(Candidate)
                .where(Candidate.job_version_id == head_id)
            ).first()
            == 1
        )
