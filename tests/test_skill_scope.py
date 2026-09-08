"""Skill System V1 -- scope / workspace isolation tests (Contract §16).

Covers: company vs project isolation, cross-project invisibility, same-name
coexistence across projects, multi-version growth inside one project, and the
deterministic company-vs-project same-name precedence (project wins, §5).
"""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session

from aios.actor import resolve_owner_actor
from aios.db import get_engine, run_migrations
from aios.models import (
    Capability,
    Project,
    Skill,
    SkillCandidateStatus,
    SkillReviewDecisionValue,
    SkillStatus,
)
from aios.skill_service import SkillService


def database(tmp_path: Path, name: str) -> str:
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    run_migrations(url)
    return url


def _project(session: Session, name: str) -> Project:
    project = Project(name=name, objective=f"Objective for {name}")
    session.add(project)
    session.commit()
    return project


def _capability(session: Session, name: str) -> Capability:
    capability = Capability(name=name, description=f"{name} capability")
    session.add(capability)
    session.commit()
    return capability


def _artifact(session: Session, project: Project, name: str):
    from aios.models import Artifact, ArtifactReviewStatus, ArtifactType

    artifact = Artifact(
        project_id=project.id,
        type=ArtifactType.JSON,
        uri=name,
        checksum=f"sha256:{name}",
        review_status=ArtifactReviewStatus.APPROVED,
    )
    session.add(artifact)
    session.commit()
    return artifact


def _submit_and_approve(
    session: Session,
    service: SkillService,
    capability: Capability,
    *,
    name: str = "outline_first",
    project_id: str | None = None,
    source_artifact_id: str | None = None,
    steps: list[dict] | None = None,
) -> Skill:
    candidate = service.submit_candidate(
        name,
        f"Procedure {name}",
        capability.id,
        steps if steps is not None else [{"step": 1, "do": name}],
        {},
        "single_pass",
        project_id=project_id,
        source_artifact_id=source_artifact_id,
        actor=resolve_owner_actor(),
    )
    result = service.review_candidate(
        candidate.id,
        SkillReviewDecisionValue.APPROVE,
        "Looks right",
        actor=resolve_owner_actor(),
    )
    assert result.skill is not None
    assert candidate.status == SkillCandidateStatus.APPROVED
    return result.skill


def test_project_skills_are_invisible_cross_project(tmp_path: Path) -> None:
    url = database(tmp_path, "scope_isolation.db")
    with Session(get_engine(url)) as session:
        service = SkillService(session)
        project_a = _project(session, "Alpha")
        project_b = _project(session, "Beta")
        capability = _capability(session, "drafting")
        artifact = _artifact(session, project_a, "a.json")

        _submit_and_approve(
            session,
            service,
            capability,
            project_id=project_a.id,
            source_artifact_id=artifact.id,
        )

        visible_in_a = service.list_skills(project_id=project_a.id, actor=resolve_owner_actor())
        visible_in_b = service.list_skills(project_id=project_b.id, actor=resolve_owner_actor())
        assert [s.project_id for s in visible_in_a] == [project_a.id]
        assert visible_in_b == [], "a project skill must not leak into another project"


def test_company_skill_is_visible_to_every_project(tmp_path: Path) -> None:
    url = database(tmp_path, "scope_company.db")
    with Session(get_engine(url)) as session:
        service = SkillService(session)
        project = _project(session, "Gamma")
        capability = _capability(session, "drafting")
        artifact = _artifact(session, project, "g.json")

        company_skill = _submit_and_approve(
            session,
            service,
            capability,
            project_id=None,
            source_artifact_id=artifact.id,
        )
        assert company_skill.project_id is None
        assert company_skill.scope if hasattr(company_skill, "scope") else True

        listed = service.list_skills(project_id=project.id, actor=resolve_owner_actor())
        assert [s.id for s in listed] == [company_skill.id]
        # And the un-scoped listing sees it too.
        assert [s.id for s in service.list_skills(actor=resolve_owner_actor())] == [
            company_skill.id
        ]


def test_same_name_coexists_across_projects(tmp_path: Path) -> None:
    url = database(tmp_path, "scope_same_name.db")
    with Session(get_engine(url)) as session:
        service = SkillService(session)
        project_a = _project(session, "Alpha")
        project_b = _project(session, "Beta")
        capability = _capability(session, "drafting")
        artifact_a = _artifact(session, project_a, "a.json")
        artifact_b = _artifact(session, project_b, "b.json")

        skill_a = _submit_and_approve(
            session,
            service,
            capability,
            name="outline_first",
            project_id=project_a.id,
            source_artifact_id=artifact_a.id,
        )
        skill_b = _submit_and_approve(
            session,
            service,
            capability,
            name="outline_first",
            project_id=project_b.id,
            source_artifact_id=artifact_b.id,
        )
        assert skill_a.id != skill_b.id
        assert skill_a.project_id == project_a.id
        assert skill_b.project_id == project_b.id


def test_same_project_same_name_grows_versions(tmp_path: Path) -> None:
    url = database(tmp_path, "scope_versions.db")
    with Session(get_engine(url)) as session:
        service = SkillService(session)
        project = _project(session, "Alpha")
        capability = _capability(session, "drafting")
        artifact = _artifact(session, project, "a.json")

        v1 = _submit_and_approve(
            session,
            service,
            capability,
            name="outline_first",
            project_id=project.id,
            source_artifact_id=artifact.id,
            steps=[{"step": 1, "do": "outline"}],
        )
        assert v1.version == 1
        # Different content, same logical identity -> v2 supersedes v1.
        v2 = _submit_and_approve(
            session,
            service,
            capability,
            name="outline_first",
            project_id=project.id,
            source_artifact_id=artifact.id,
            steps=[{"step": 1, "do": "outline"}, {"step": 2, "do": "polish"}],
        )
        assert v2.version == 2
        session.refresh(v1)
        assert v1.status == SkillStatus.SUPERSEDED
        assert v2.status == SkillStatus.APPROVED
        # Only the head is APPROVED for this (name, project).
        heads = [
            s
            for s in service.list_skills(
                project_id=project.id,
                name="outline_first",
                status=SkillStatus.APPROVED,
                actor=resolve_owner_actor(),
            )
        ]
        assert [s.id for s in heads] == [v2.id]


def test_same_name_project_beats_company(tmp_path: Path) -> None:
    """Company and project scopes may both carry a name; project wins (§5)."""
    url = database(tmp_path, "scope_precedence.db")
    with Session(get_engine(url)) as session:
        service = SkillService(session)
        project = _project(session, "Alpha")
        capability = _capability(session, "drafting")
        artifact = _artifact(session, project, "a.json")

        company = _submit_and_approve(
            session,
            service,
            capability,
            name="outline_first",
            project_id=None,
            source_artifact_id=artifact.id,
        )
        tailored = _submit_and_approve(
            session,
            service,
            capability,
            name="outline_first",
            project_id=project.id,
            source_artifact_id=artifact.id,
            steps=[{"step": 1, "do": "tailored-outline"}],
        )
        assert company.version == 1 and tailored.version == 1  # independent series

        # Both rows survive and stay APPROVED: precedence is a projection-time
        # rule, not a mutation.
        all_skills = service.list_skills(actor=resolve_owner_actor())
        assert {s.id for s in all_skills} == {company.id, tailored.id}

        # The projection resolves the conflict deterministically: the project
        # skill is the head for this project (context engine selection).
        from aios.context_service import _skill_heads

        heads = _skill_heads(session, project.id)
        assert [s.id for s in heads] == [tailored.id]
        # Another project (no tailoring of its own) keeps the company skill.
        other = _project(session, "Other")
        heads_other = _skill_heads(session, other.id)
        assert [s.id for s in heads_other] == [company.id]
