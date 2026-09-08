"""Skill System V1 -- governance tests (Contract §16).

Skill is executable production material, so governance is owner-only and
fail-closed: no agent submit, no agent review, no agent deactivate. Actor
identity is always derived from the trusted ``authenticate_owner`` actor, never
from request data. Also pins the five audit actions + idempotency keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, select

from aios.actor import ActorContext, resolve_owner_actor
from aios.audit import AuditLog
from aios.db import get_engine, run_migrations
from aios.models import Capability, Project, SkillReviewDecisionValue, SkillStatus
from aios.services import ServiceError
from aios.skill_service import SkillService

AGENT_ACTOR = ActorContext(kind="agent", agent_id="agt_1")


def database(tmp_path: Path, name: str) -> str:
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    run_migrations(url)
    return url


def _seed(session: Session) -> tuple[Project, Capability]:
    project = Project(name="Gov", objective="Governed skills")
    capability = Capability(name="drafting", description="Writes")
    session.add_all([project, capability])
    session.commit()
    return project, capability


def _approve(session: Session, capability: Capability, project: Project):
    service = SkillService(session)
    candidate = service.submit_candidate(
        "outline_first",
        "Always outline first",
        capability.id,
        [{"step": 1, "do": "outline"}],
        {},
        "single_pass",
        project_id=project.id,
        actor=resolve_owner_actor(),
    )
    result = service.review_candidate(
        candidate.id,
        SkillReviewDecisionValue.APPROVE,
        "ok",
        actor=resolve_owner_actor(),
    )
    return service, candidate, result.skill


def test_agent_cannot_submit_candidate(tmp_path: Path) -> None:
    url = database(tmp_path, "gov_submit.db")
    with Session(get_engine(url)) as session:
        project, capability = _seed(session)
        with pytest.raises(ServiceError, match="403|owner"):
            SkillService(session).submit_candidate(
                "outline_first",
                "Always outline first",
                capability.id,
                [{"step": 1, "do": "outline"}],
                {},
                "single_pass",
                project_id=project.id,
                actor=AGENT_ACTOR,
            )


def test_agent_cannot_review(tmp_path: Path) -> None:
    url = database(tmp_path, "gov_review.db")
    with Session(get_engine(url)) as session:
        project, capability = _seed(session)
        service = SkillService(session)
        candidate = service.submit_candidate(
            "outline_first",
            "Always outline first",
            capability.id,
            [{"step": 1, "do": "outline"}],
            {},
            "single_pass",
            project_id=project.id,
            actor=resolve_owner_actor(),
        )
        with pytest.raises(ServiceError, match="403|owner"):
            service.review_candidate(
                candidate.id,
                SkillReviewDecisionValue.APPROVE,
                "self-approve",
                actor=AGENT_ACTOR,
            )


def test_agent_cannot_deactivate(tmp_path: Path) -> None:
    url = database(tmp_path, "gov_deactivate.db")
    with Session(get_engine(url)) as session:
        project, capability = _seed(session)
        service, _candidate, skill = _approve(session, capability, project)
        assert skill is not None
        with pytest.raises(ServiceError, match="403|owner"):
            service.deactivate_skill(skill.id, "rogue", actor=AGENT_ACTOR)


def test_full_lifecycle_writes_complete_audit_trail(tmp_path: Path) -> None:
    url = database(tmp_path, "gov_audit.db")
    with Session(get_engine(url)) as session:
        project, capability = _seed(session)
        service, candidate, skill = _approve(session, capability, project)
        assert skill is not None
        service.deactivate_skill(skill.id, "obsolete", actor=resolve_owner_actor())

        actions = [
            audit.action
            for audit in session.exec(select(AuditLog).order_by(AuditLog.created_at))
        ]
        assert actions == [
            "skill.candidate.created",
            "skill.approved",
            "skill.deactivated",
        ]
        audits = list(session.exec(select(AuditLog)))
        for audit in audits:
            assert audit.idempotency_key
        assert audits[0].idempotency_key == (
            f"audit:skill:candidate:{candidate.id}:created"
        )
        assert audits[1].idempotency_key == f"audit:skill:{skill.id}:approved"
        deactivated = session.exec(
            select(AuditLog).where(AuditLog.action == "skill.deactivated")
        ).first()
        assert deactivated is not None
        assert deactivated.after_snapshot["status"] == SkillStatus.INACTIVE.value


def test_review_reject_is_recorded_and_terminal(tmp_path: Path) -> None:
    url = database(tmp_path, "gov_reject.db")
    with Session(get_engine(url)) as session:
        project, capability = _seed(session)
        service = SkillService(session)
        candidate = service.submit_candidate(
            "outline_first",
            "Always outline first",
            capability.id,
            [{"step": 1, "do": "outline"}],
            {},
            "single_pass",
            project_id=project.id,
            actor=resolve_owner_actor(),
        )
        result = service.review_candidate(
            candidate.id,
            SkillReviewDecisionValue.REJECT,
            "not reusable",
            actor=resolve_owner_actor(),
        )
        assert result.skill is None
        assert candidate.status.value == "rejected"
        # A rejected candidate mints nothing.
        from aios.models import Skill

        assert list(session.exec(select(Skill))) == []
        # And the decision is terminal: flipping it is a 409, never an update.
        with pytest.raises(ServiceError, match="conflicts"):
            service.review_candidate(
                candidate.id,
                SkillReviewDecisionValue.APPROVE,
                "changed my mind",
                actor=resolve_owner_actor(),
            )
