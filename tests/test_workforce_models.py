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

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from aios.db import get_engine, run_migrations
from aios.models import (
    BusinessGoal,
    Capability,
    CapabilityRequirement,
    Job,
    JobVersion,
    RequiredWork,
)
from aios.services import ServiceError
from aios.workforce import (
    add_capability_requirement,
    create_business_goal,
    create_job,
    create_job_version,
    create_required_work,
    list_capability_requirements,
    list_job_versions,
)


def _db(url: str) -> Session:
    run_migrations(url)
    return Session(get_engine(url))


def _seed_capability(session: Session, name: str) -> Capability:
    cap = Capability(name=name, description=f"{name} capability")
    session.add(cap)
    session.flush()
    return cap


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
    assert heads == ["20260827_0001_workforce_capreq_hardening"]


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
    # ...and the Alpha-1 SSoT table is untouched (proves additivity).
    assert "capability" in tables
    # No second capability vocabulary table was created.
    assert not any(t.startswith("workforce_capability") for t in tables)
