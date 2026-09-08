"""Skill System V1 HTTP surface (Contract §12): exactly six endpoints.

Governance shape
----------------
``POST /skills`` does NOT exist: "create a skill directly" would be a review
bypass, so the ONLY path from proposal to publication is
``POST /skills/reviews`` with ``decision: approve`` (the single gate; approval
mints the version server-side -- no client version input exists, Contract §4).
Likewise ``/versions`` / ``/retire`` are deliberately absent: version minting
is server-owned and V1 knows only ``deactivate``.

Route-string hard constraint (Contract F-3)
-------------------------------------------
Every path literal below is FULLY QUALIFIED under ``/skills``. A router-level
``prefix="/skills"`` with relative paths would emit bare ``"/candidates"`` /
``"/reviews"`` constants, and ``"/candidates"`` starts with ``"/candidate"`` --
one of the W6 Workforce route-guard prefixes -- tripping an unrelated frozen
invariant (``tests/test_workforce_w6_invariants.py:317``). S4 in
``tests/test_skill_invariants.py`` pins this.

Auth: all six endpoints require the owner actor (Contract §12 -- V1 has no
agent write entrance; governance is human-owner-only). Actor identity is always
derived from ``authenticate_owner``, never from the request body.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session

from aios.actor import ActorContext
from aios.api.security import authenticate_owner
from aios.db import get_session
from aios.models import Skill, SkillCandidate, SkillCandidateStatus, SkillStatus
from aios.schemas import SkillCandidateCreate, SkillDeactivateRequest, SkillReviewRequest
from aios.services import ServiceError
from aios.skill_service import SkillService


def _translate(error: ServiceError) -> HTTPException:
    """Local copy of ``aios.api.app._translate`` (employee_bridge precedent)."""
    return HTTPException(status_code=error.status_code, detail=error.detail)


def register_skill_routes(application: Any) -> None:
    """Build and flat-attach the 6 skill routes (owner_inbox_routes precedent)."""
    router = APIRouter(
        tags=["skills"],
        dependency_overrides_provider=application,
    )

    # ---------------------------------------------------------------- 1
    @router.post(
        "/skills/candidates",
        response_model=SkillCandidate,
        status_code=status.HTTP_201_CREATED,
    )
    def submit_candidate(
        request: SkillCandidateCreate,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> SkillCandidate:
        try:
            return SkillService(session).submit_candidate(
                request.name,
                request.description,
                request.capability_id,
                request.steps,
                request.tool_bindings,
                request.execution_strategy,
                project_id=request.project_id,
                source_artifact_id=request.source_artifact_id,
                actor=actor,
            )
        except ServiceError as error:
            raise _translate(error) from error

    # ---------------------------------------------------------------- 2
    @router.get("/skills/candidates", response_model=list[SkillCandidate])
    def list_candidates(
        project_id: str | None = Query(default=None),
        candidate_status: SkillCandidateStatus | None = Query(default=None, alias="status"),
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> list[SkillCandidate]:
        try:
            return SkillService(session).list_candidates(
                project_id=project_id,
                status=candidate_status,
                actor=actor,
            )
        except ServiceError as error:
            raise _translate(error) from error

    # ---------------------------------------------------------------- 3
    @router.post("/skills/reviews", response_model=dict[str, Any])
    def review_candidate(
        request: SkillReviewRequest,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> dict[str, Any]:
        try:
            result = SkillService(session).review_candidate(
                request.candidate_id,
                request.decision,
                request.rationale,
                actor=actor,
            )
        except ServiceError as error:
            raise _translate(error) from error
        # Single governance entrance: approve => {decision, skill}; reject =>
        # {decision, skill: None}. The response never leaks ORM internals.
        return {
            "decision": result.decision.model_dump(mode="json"),
            "skill": result.skill.model_dump(mode="json") if result.skill else None,
        }

    # ---------------------------------------------------------------- 4
    @router.get("/skills", response_model=list[Skill])
    def list_skills(
        project_id: str | None = Query(default=None),
        capability_id: str | None = Query(default=None),
        name: str | None = Query(default=None),
        skill_status: SkillStatus | None = Query(default=None, alias="status"),
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> list[Skill]:
        try:
            return SkillService(session).list_skills(
                project_id=project_id,
                capability_id=capability_id,
                name=name,
                status=skill_status,
                actor=actor,
            )
        except ServiceError as error:
            raise _translate(error) from error

    # ---------------------------------------------------------------- 5
    @router.get("/skills/{skill_id}", response_model=Skill)
    def get_skill(
        skill_id: str,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> Skill:
        try:
            return SkillService(session).get_skill(skill_id, actor=actor)
        except ServiceError as error:
            raise _translate(error) from error

    # ---------------------------------------------------------------- 6
    @router.post("/skills/{skill_id}/deactivate", response_model=Skill)
    def deactivate_skill(
        skill_id: str,
        request: SkillDeactivateRequest,
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> Skill:
        try:
            return SkillService(session).deactivate_skill(
                skill_id, request.rationale, actor=actor
            )
        except ServiceError as error:
            raise _translate(error) from error

    # Flat-attach (owner_inbox_routes precedent): app.include_router would
    # nest prefix handling; flat attach keeps every path literal exactly as
    # declared above, which is what the W6 guard and S4 scan verify.
    for route in router.routes:
        application.router.routes.append(route)
