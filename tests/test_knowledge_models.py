from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from aios.actor import resolve_owner_actor
from aios.db import get_engine, run_migrations
from aios.knowledge_service import KnowledgeService
from aios.models import (
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    KnowledgeCandidate,
    KnowledgeCandidateStatus,
    KnowledgeFact,
    KnowledgeFactStatus,
    KnowledgeReviewDecision,
    KnowledgeReviewDecisionValue,
    Project,
)
from alembic import command

# Lowest revision these ORM-seeding tests upgrade to. The migrations above it
# form a chain of one-way doors:
#   * 20260810_0001 (SalesPlaybook V0)            -> downgrade() raises unconditionally
#   * 20260812_0001 (cs_suggestion evidence flag) -> downgrade() raises unconditionally
#   * 20260820_0001 (series_id)                   -> downgrade() DROPs the column (data-losing)
#   * 20260824_0001 (series_id_json_guard, former head)  -> downgrade() is a deliberate no-op pass
# The two ``raise``-on-downgrade revisions (20260810, 20260812) are the genuine
# one-way doors; the head is NOT (its downgrade is a no-op). This floor already
# carries every column the ORM models below depend on.
LAST_DOWNGRADABLE = "20260731_0001"


def test_knowledge_model_defaults_and_provenance() -> None:
    candidate = KnowledgeCandidate(
        artifact_id="art_one",
        project_id="prj_one",
        statement="Reviewed statement",
        submitted_by_kind="owner",
        submitted_by_owner_id="owner",
        submitted_by="owner:owner",
    )
    decision = KnowledgeReviewDecision(
        candidate_id=candidate.id,
        decision=KnowledgeReviewDecisionValue.APPROVE,
        reviewer_kind="owner",
        reviewer_owner_id="owner",
        reviewer="owner:owner",
        rationale="Verified source",
    )
    fact = KnowledgeFact(
        series_id="series",
        version=1,
        project_id="prj_one",
        statement=candidate.statement,
        source_candidate_id=candidate.id,
        source_artifact_id=candidate.artifact_id,
        review_decision_id=decision.id,
    )
    assert candidate.status == KnowledgeCandidateStatus.DRAFT
    assert fact.status == KnowledgeFactStatus.APPROVED
    assert fact.supersedes_fact_id is None


def _approved_candidate(session: Session) -> KnowledgeCandidate:
    project = Project(name="P", objective="O")
    session.add(project)
    session.flush()
    artifact = Artifact(
        project_id=project.id,
        type=ArtifactType.JSON,
        uri="a",
        checksum="c",
        review_status=ArtifactReviewStatus.APPROVED,
    )
    session.add(artifact)
    session.flush()
    candidate = KnowledgeCandidate(
        artifact_id=artifact.id,
        project_id=project.id,
        source_project_id=project.id,
        statement="S",
        submitted_by_kind="owner",
        submitted_by_owner_id="owner",
        submitted_by="owner:owner",
    )
    session.add(candidate)
    session.flush()
    return candidate


def test_migration_creates_alpha3_tables(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'knowledge.db').as_posix()}"
    run_migrations(url)
    with Session(get_engine(url)) as session:
        session.connection().exec_driver_sql("SELECT id FROM knowledge_candidate")
        session.connection().exec_driver_sql("SELECT id FROM knowledge_review_decision")
        session.connection().exec_driver_sql("SELECT id FROM knowledge_fact")


def test_unique_terminal_review_is_database_enforced(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'unique.db').as_posix()}"
    run_migrations(url)
    with Session(get_engine(url)) as session:
        candidate = _approved_candidate(session)
        session.add_all(
            [
                KnowledgeReviewDecision(
                    candidate_id=candidate.id,
                    decision=KnowledgeReviewDecisionValue.APPROVE,
                    reviewer_kind="owner",
                    reviewer_owner_id="owner",
                    reviewer="owner:owner",
                    rationale="r",
                ),
                KnowledgeReviewDecision(
                    candidate_id=candidate.id,
                    decision=KnowledgeReviewDecisionValue.REJECT,
                    reviewer_kind="owner",
                    reviewer_owner_id="owner",
                    reviewer="owner:owner",
                    rationale="r",
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_alpha3_migration_upgrade_downgrade_round_trip(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'roundtrip.db').as_posix()}"
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)

    def revision() -> str:
        with Session(get_engine(url)) as session:
            return session.connection().exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()

    command.upgrade(config, "20260715_0004")
    assert revision() == "20260715_0004"
    command.upgrade(config, "20260716_0005")
    assert revision() == "20260716_0005"
    command.downgrade(config, "20260715_0004")
    assert revision() == "20260715_0004"
    with Session(get_engine(url)) as session:
        tables = {
            row[0]
            for row in session.connection().exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "knowledge_fact" not in tables
    command.upgrade(config, "head")
    # Head advanced past the Alpha-3 knowledge layer by the Agent Interop
    # Gateway work (#57 / #104 / #57-slices): 20260719_0001 + 20260719_0002 +
    # 20260719_0003, Independent Review Protocol (#64): 20260719_0004, the
    # Phase A knowledge-tags slice (#67): 20260720_0005, Review Binding (#69):
    # 20260720_0006, ReviewPolicy identity (#72/#74): 20260722_0007, and the
    # scope-aware knowledge_fact version uniqueness fix (#53): 20260727_0008,
    # and the work-log & agent-platform slice (#88): 20260728_0009, and the V4
    # agent self-registration slice (#99/#101): 20260729_0001, and the #103
    # agent_secret slice: 20260730_0001. The round-trip mechanics above already
    # prove the knowledge migrations; this final step only confirms we can
    # return to the current head (#109 customer-service workflow slice
    # 20260731_0001, then the SalesPlaybook V0 slice 20260812_0001, extend the
    # chain past the #103 secret-store slice, to the Workforce Management
    # chain (W1--W4: business_goal .. employee, head 20260903_0002) and then
    # to the current W5 Cost Evidence head:
    # 20260904_0001_workforce_cost_evidence.
    assert revision() == "20260904_0001_workforce_cost_evidence"


def test_scope_unique_migration_round_trip(tmp_path: Path) -> None:
    """#53: the scope-aware uniqueness migration upgrades and downgrades cleanly,
    flips the constraint shape in both directions, adds the company-scope partial
    index, and is fail-closed on downgrade when cross-scope data exists."""
    from sqlalchemy import inspect

    url = f"sqlite:///{(tmp_path / 'scope_unique_rt.db').as_posix()}"
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)

    def revision() -> str:
        with Session(get_engine(url)) as session:
            return session.connection().exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()

    def index_names() -> set[str]:
        with Session(get_engine(url)) as session:
            return {
                row[0]
                for row in session.connection().exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }

    def trigger_names() -> set[str]:
        with Session(get_engine(url)) as session:
            return {
                row[0]
                for row in session.connection().exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }

    def constraint_columns() -> list[str]:
        engine = get_engine(url)
        cols = inspect(engine).get_unique_constraints("knowledge_fact")
        target = next(c for c in cols if c["name"] == "uq_knowledge_fact_series_version")
        return target["column_names"]

    def seed_project_artifact(engine, name: str) -> tuple[Project, Artifact]:
        with Session(engine) as session:
            project = Project(name=name, objective=f"objective for {name}")
            session.add(project)
            session.flush()
            artifact = Artifact(
                project_id=project.id,
                type=ArtifactType.JSON,
                uri=f"{name}.json",
                checksum=f"sha256:{name}",
                review_status=ArtifactReviewStatus.APPROVED,
            )
            session.add(artifact)
            session.commit()
            session.refresh(project)
            session.refresh(artifact)
            return project, artifact

    def seed_fact(
        engine,
        artifact: Artifact,
        scope_project_id,
        series: str,
        version: int,
        supersedes_id: str | None = None,
    ) -> KnowledgeFact:
        """Create an APPROVED KnowledgeFact in the given effective scope via the
        validated service path (exercises provenance triggers correctly)."""
        with Session(engine) as session:
            cand = KnowledgeService(session).submit_candidate(
                artifact.id,
                f"{series}-{scope_project_id or 'company'}-{version}",
                project_id=scope_project_id,
                tags=["positioning"],
                actor=resolve_owner_actor(),
            )
            result = KnowledgeService(session).review_candidate(
                cand.id,
                KnowledgeReviewDecisionValue.APPROVE,
                "rationale",
                actor=resolve_owner_actor(),
                series_id=series,
                version=version,
                supersedes_fact_id=supersedes_id,
            )
            fact = result.fact
            session.refresh(fact)
            return fact

    command.upgrade(config, "20260722_0007")
    assert revision() == "20260722_0007"
    assert set(constraint_columns()) == {"series_id", "version"}

    command.upgrade(config, "20260727_0008")
    assert revision() == "20260727_0008"
    assert set(constraint_columns()) == {"series_id", "version", "project_id"}
    # Company-scope partial index added by 0008 (the guard the old 3-col
    # constraint alone could not provide, since SQLite treats NULL as distinct).
    assert "uq_knowledge_fact_company_version" in index_names()
    assert "uq_knowledge_fact_approved_head" in index_names()
    assert "knowledge_fact_validate_insert" in trigger_names()
    assert "knowledge_fact_validate_update" in trigger_names()

    # ORM seeding below requires the full column set the models expect: the
    # Artifact model now carries idempotency_key (added by 20260728_0009,
    # purely additive / nullable), so bring the DB up to LAST_DOWNGRADABLE
    # before inserting ORM rows. Those upper layers stay empty here, so their
    # downgrade legs remain lossless and the fail-closed assertion below still
    # exercises the 0008 -> 0007 gate.
    command.upgrade(config, LAST_DOWNGRADABLE)

    # ---- Seed genuine cross-scope coexistence via the service layer ----
    engine = get_engine(url)
    # Company scope: source campaign = cmp, effective scope = company (NULL).
    cmp_project, cmp_artifact = seed_project_artifact(engine, "company-campaign")
    # Two distinct project scopes for the same series.
    px_project, px_artifact = seed_project_artifact(engine, "px")
    py_project, py_artifact = seed_project_artifact(engine, "py")

    company_v1 = seed_fact(engine, cmp_artifact, None, "shared", 1)
    px_v1 = seed_fact(engine, px_artifact, px_project.id, "shared", 1)
    py_v1 = seed_fact(engine, py_artifact, py_project.id, "shared", 1)
    # (company v1) + (px v1) + (py v1) of the SAME series coexist => #53 fixed.
    assert company_v1.project_id is None
    assert px_v1.project_id == px_project.id
    assert py_v1.project_id == py_project.id

    # ---- Duplicate rejection (same scope) ----
    # The insert trigger (and, defensively, the unique indexes) reject a second
    # version-1 fact in an existing series. Copy an existing fact's provenance but
    # change only its id; insertion must raise IntegrityError.
    def assert_duplicate_rejected(src_fact: KnowledgeFact) -> None:
        with Session(engine) as session:
            src = session.get(KnowledgeFact, src_fact.id)
            dup = KnowledgeFact(
                id=f"dup_{src.id}",
                series_id=src.series_id,
                version=src.version,
                project_id=src.project_id,
                source_project_id=src.source_project_id,
                statement=src.statement,
                tags=list(src.tags),
                status=src.status,
                source_candidate_id=src.source_candidate_id,
                source_artifact_id=src.source_artifact_id,
                review_decision_id=src.review_decision_id,
                created_at=src.created_at,
            )
            session.add(dup)
            with pytest.raises(IntegrityError):
                session.commit()

    assert_duplicate_rejected(company_v1)  # company-scope partial index / trigger
    assert_duplicate_rejected(px_v1)  # project-scope 3-col constraint / trigger

    # ---- Fail-closed downgrade: any (series, version) shared by >1 row ----
    # (here: company v1 + project-A v1 + project-B v1 of the SAME series all
    # coexist, covering company x project AND project x project clashes.)
    with pytest.raises(RuntimeError):
        command.downgrade(config, "20260722_0007")
    # Schema stays on 0008; all rows, both indexes, and triggers remain intact.
    assert revision() == "20260727_0008"
    assert "uq_knowledge_fact_company_version" in index_names()
    assert "uq_knowledge_fact_approved_head" in index_names()
    assert "knowledge_fact_validate_insert" in trigger_names()
    assert "knowledge_fact_validate_update" in trigger_names()
    with Session(engine) as session:
        fact_ids = {
            row[0]
            for row in session.connection().exec_driver_sql(
                "SELECT id FROM knowledge_fact"
            )
        }
    assert {company_v1.id, px_v1.id, py_v1.id}.issubset(fact_ids)


def test_scope_unique_downgrade_fail_closed_two_projects(tmp_path: Path) -> None:
    """#53: fail-closed must catch TWO DIFFERENT projects sharing a (series,
    version) -- NOT only a company x project clash. With no company row present,
    the old ``UNIQUE(series_id, version)`` global index still cannot represent
    project A v1 + project B v1 of the same series, so downgrade must abort.

    This is the exact gap the original precheck missed: it only looked for the
    company (NULL) x project (NOT NULL) intersection and would have let a
    project-A x project-B clash slip through to a mid-rebuild failure.
    """
    url = f"sqlite:///{(tmp_path / 'scope_unique_2proj.db').as_posix()}"
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)

    def revision() -> str:
        with Session(get_engine(url)) as session:
            return session.connection().exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()

    def index_names() -> set[str]:
        with Session(get_engine(url)) as session:
            return {
                row[0]
                for row in session.connection().exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }

    def trigger_names() -> set[str]:
        with Session(get_engine(url)) as session:
            return {
                row[0]
                for row in session.connection().exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }

    def seed_project_artifact(engine, name: str) -> tuple[Project, Artifact]:
        with Session(engine) as session:
            project = Project(name=name, objective=f"objective for {name}")
            session.add(project)
            session.flush()
            artifact = Artifact(
                project_id=project.id,
                type=ArtifactType.JSON,
                uri=f"{name}.json",
                checksum=f"sha256:{name}",
                review_status=ArtifactReviewStatus.APPROVED,
            )
            session.add(artifact)
            session.commit()
            session.refresh(project)
            session.refresh(artifact)
            return project, artifact

    # Apply migrations up to LAST_DOWNGRADABLE: the ORM Artifact model now
    # carries idempotency_key (20260728_0009, additive/nullable), so ORM
    # seeding needs that column set. Those upper layers stay empty, so the
    # downgrade below still reaches (and is stopped by) the 0008 gate.
    command.upgrade(config, LAST_DOWNGRADABLE)

    engine = get_engine(url)
    # Two DISTINCT project scopes for the same series -- NO company row.
    pa_project, pa_artifact = seed_project_artifact(engine, "proj-a")
    pb_project, pb_artifact = seed_project_artifact(engine, "proj-b")

    with Session(engine) as session:
        cand_a = KnowledgeService(session).submit_candidate(
            pa_artifact.id,
            f"shared-{pa_project.id}-1",
            project_id=pa_project.id,
            tags=["positioning"],
            actor=resolve_owner_actor(),
        )
        KnowledgeService(session).review_candidate(
            cand_a.id,
            KnowledgeReviewDecisionValue.APPROVE,
            "rationale",
            actor=resolve_owner_actor(),
            series_id="shared",
            version=1,
        )
        cand_b = KnowledgeService(session).submit_candidate(
            pb_artifact.id,
            f"shared-{pb_project.id}-1",
            project_id=pb_project.id,
            tags=["positioning"],
            actor=resolve_owner_actor(),
        )
        KnowledgeService(session).review_candidate(
            cand_b.id,
            KnowledgeReviewDecisionValue.APPROVE,
            "rationale",
            actor=resolve_owner_actor(),
            series_id="shared",
            version=1,
        )

    # Exactly two rows share (series='shared', version=1), in two distinct
    # project scopes, with no company row -> old global index would conflict.
    with Session(engine) as session:
        n = session.connection().exec_driver_sql(
            "SELECT COUNT(*) FROM knowledge_fact WHERE series_id='shared' AND version=1"
        ).scalar_one()
    assert n == 2

    # Fail-closed: downgrade aborts BEFORE any DDL; schema/rows/indexes/triggers
    # stay intact on 0008.
    with pytest.raises(RuntimeError):
        command.downgrade(config, "20260722_0007")
    assert revision() == "20260727_0008"
    assert "uq_knowledge_fact_company_version" in index_names()
    assert "uq_knowledge_fact_approved_head" in index_names()
    assert "knowledge_fact_validate_insert" in trigger_names()
    assert "knowledge_fact_validate_update" in trigger_names()
    with Session(engine) as session:
        fact_ids = {
            row[0]
            for row in session.connection().exec_driver_sql(
                "SELECT id FROM knowledge_fact"
            )
        }
    assert len(fact_ids) == 2

    # Sanity: upgrading again is a no-op (already on 0008) and the data survives.
    command.upgrade(config, "20260727_0008")
    assert revision() == "20260727_0008"
    with Session(engine) as session:
        assert (
            session.connection()
            .exec_driver_sql("SELECT COUNT(*) FROM knowledge_fact")
            .scalar_one()
            == 2
        )


def test_scope_unique_downgrade_lossless_without_cross_scope(tmp_path: Path) -> None:
    """#53: when NO company/project (series, version) coexistence exists, the
    0008 -> 0007 downgrade succeeds losslessly and the schema reverts to the
    global UNIQUE(series_id, version)."""
    from sqlalchemy import inspect

    url = f"sqlite:///{(tmp_path / 'scope_unique_down.db').as_posix()}"
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)

    def revision() -> str:
        with Session(get_engine(url)) as session:
            return session.connection().exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()

    def constraint_columns() -> list[str]:
        engine = get_engine(url)
        cols = inspect(engine).get_unique_constraints("knowledge_fact")
        target = next(c for c in cols if c["name"] == "uq_knowledge_fact_series_version")
        return target["column_names"]

    # Fresh DB: only single-scope (project) knowledge, never a company fact
    # sharing (series, version) with a project fact -> downgrade is safe.
    engine = get_engine(url)

    # Apply migrations up to LAST_DOWNGRADABLE (ORM seeding needs that column
    # set; 20260728_0009 is additive/nullable and its data stays empty here, so
    # the 0009 downgrade leg is lossless and the 0008 -> 0007 leg is exercised).
    command.upgrade(config, LAST_DOWNGRADABLE)
    assert set(constraint_columns()) == {"series_id", "version", "project_id"}

    # Seed one project-scoped series only (no company counterpart).
    with Session(engine) as session:
        project = Project(name="solo", objective="solo objective")
        session.add(project)
        session.flush()
        artifact = Artifact(
            project_id=project.id,
            type=ArtifactType.JSON,
            uri="solo.json",
            checksum="sha256:solo",
            review_status=ArtifactReviewStatus.APPROVED,
        )
        session.add(artifact)
        session.commit()
        session.refresh(project)
        session.refresh(artifact)
    with Session(engine) as session:
        cand = KnowledgeService(session).submit_candidate(
            artifact.id,
            "solo statement",
            project_id=project.id,
            tags=["positioning"],
            actor=resolve_owner_actor(),
        )
        KnowledgeService(session).review_candidate(
            cand.id,
            KnowledgeReviewDecisionValue.APPROVE,
            "rationale",
            actor=resolve_owner_actor(),
            series_id="solo-series",
            version=1,
        )

    # Downgrade now succeeds (no cross-scope coexistence).
    command.downgrade(config, "20260722_0007")
    assert revision() == "20260722_0007"
    assert set(constraint_columns()) == {"series_id", "version"}
    # The partial company-scope index from 0008 is dropped on downgrade.
    with Session(engine) as session:
        indexes = {
            row[0]
            for row in session.connection().exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
    assert "uq_knowledge_fact_company_version" not in indexes
    # The project fact row survives the lossless downgrade.
    with Session(engine) as session:
        ids = {
            row[0]
            for row in session.connection().exec_driver_sql(
                "SELECT id FROM knowledge_fact"
            )
        }
    assert len(ids) == 1

    # And it can be upgraded back to 0008 cleanly.
    command.upgrade(config, "20260727_0008")
    assert revision() == "20260727_0008"
    assert set(constraint_columns()) == {"series_id", "version", "project_id"}
