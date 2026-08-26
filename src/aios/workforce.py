"""Workforce Management -- W1 service layer (V1.1 Workforce Architecture).

This module implements the *definition-time* half of the W1 minimum closed loop:

    BusinessGoal -> RequiredWork -> Job -> JobVersion -> CapabilityRequirement

Design rules (must hold; contract tests assert them):

1. Job is the first-class citizen. ``create_job`` is the central entry point and
   every other W1 entity hangs off a Job (directly or transitively).
2. CapabilityRequirement references the Alpha-1 Capability SSoT *only*
   (``capability.id``). A capability slug that does not already exist is rejected
   fail-closed with 422 -- exactly like ``agent_registry._resolve_capabilities``.
   No second capability vocabulary is ever created here.
3. JobVersion is immutable history. Requirements are changed by minting a NEW
   JobVersion (``create_job_version``), never by mutating an old one. Direct
   single-requirement edits (``add_capability_requirement``) are allowed ONLY on
   the head (active) version; editing a historical version is rejected with 409.
4. Referential integrity is enforced at write time (parent-existence checks raise
   ``ServiceError(404)``) and at the DB level (FKs + cascade in the migration).

Out of scope for W1 (W2+): Candidate / Evaluation / Match / Trial / Employee and
the Hire/Replace/Terminate/Promote/Transfer L4 approvals. Those entities and the
owner-approval gate are deliberately absent here -- this module is the stable
foundation they will build on, not the whole system.
"""

from __future__ import annotations

from sqlmodel import Session, select

from aios.models import (
    BusinessGoal,
    BusinessGoalStatus,
    Capability,
    CapabilityRequirement,
    Job,
    JobStatus,
    JobVersion,
    RequiredWork,
    RequiredWorkStatus,
)
from aios.services import ServiceError

# ---------------------------------------------------------------------------
# Capability SSoT resolution (mirrors agent_registry._resolve_capabilities)
# ---------------------------------------------------------------------------


def _resolve_capability_id(session: Session, name: str) -> str:
    """Return the capability id for ``name``, fail-closed on unknown.

    The Alpha-1 ``Capability`` catalog is the single source of truth for the
    capability vocabulary. We never invent capabilities here.
    """
    cap = session.exec(select(Capability).where(Capability.name == name)).first()
    if cap is None:
        raise ServiceError(422, f"unknown capability: {name}")
    return cap.id


def _resolve_capabilities(session: Session, names: list[str]) -> list[Capability]:
    """Resolve capability slugs to their Alpha-1 Capability rows, fail-closed.

    Returns the actual rows (so callers can snapshot ``id`` + ``name``); raises
    ``ServiceError(422)`` on the first unknown slug.
    """
    caps: list[Capability] = []
    for name in names or []:
        cap = session.exec(select(Capability).where(Capability.name == name)).first()
        if cap is None:
            raise ServiceError(422, f"unknown capability: {name}")
        caps.append(cap)
    return caps


# ---------------------------------------------------------------------------
# BusinessGoal
# ---------------------------------------------------------------------------


def create_business_goal(
    session: Session,
    title: str,
    *,
    description: str = "",
    target_outcome: str = "",
    owner: str = "human_ceo",
    priority: int = 50,
    status: BusinessGoalStatus = BusinessGoalStatus.PROPOSED,
) -> BusinessGoal:
    goal = BusinessGoal(
        owner=owner,
        title=title,
        description=description,
        target_outcome=target_outcome,
        status=status,
        priority=priority,
    )
    session.add(goal)
    session.flush()
    return goal


# ---------------------------------------------------------------------------
# RequiredWork
# ---------------------------------------------------------------------------


def create_required_work(
    session: Session,
    business_goal_id: str,
    title: str,
    *,
    description: str = "",
    rationale: str = "",
    priority: int = 50,
    status: RequiredWorkStatus = RequiredWorkStatus.PROPOSED,
) -> RequiredWork:
    goal = session.get(BusinessGoal, business_goal_id)
    if goal is None:
        raise ServiceError(404, f"business goal not found: {business_goal_id}")
    rw = RequiredWork(
        business_goal_id=business_goal_id,
        title=title,
        description=description,
        rationale=rationale,
        priority=priority,
        status=status,
    )
    session.add(rw)
    session.flush()
    return rw


# ---------------------------------------------------------------------------
# Job (first-class citizen) + JobVersion (immutable history)
# ---------------------------------------------------------------------------


def _next_version(session: Session, job_id: str) -> int:
    current = session.exec(
        select(JobVersion.version)
        .where(JobVersion.job_id == job_id)
        .order_by(JobVersion.version.desc())
    ).first()
    return (current or 0) + 1


def _make_capability_requirements(
    session: Session,
    job_version_id: str,
    capability_names: list[str],
) -> list[CapabilityRequirement]:
    """Build CapabilityRequirement rows for ``capability_names`` (SSoT-checked)."""
    rows: list[CapabilityRequirement] = []
    for cap in _resolve_capabilities(session, capability_names):
        rows.append(
            CapabilityRequirement(
                job_version_id=job_version_id,
                capability_id=cap.id,
                capability_name=cap.name,
            )
        )
    return rows


def create_job(
    session: Session,
    required_work_id: str,
    title: str,
    *,
    description: str = "",
    role_summary: str = "",
    capability_names: list[str] | None = None,
) -> Job:
    """Create a Job plus its first (version 1) JobVersion and capability set.

    This is the canonical entry point: a Job is never created without at least
    one JobVersion, and ``head_version_id`` is wired up atomically.
    """
    rw = session.get(RequiredWork, required_work_id)
    if rw is None:
        raise ServiceError(404, f"required work not found: {required_work_id}")

    job = Job(
        required_work_id=required_work_id,
        title=title,
        description=description,
        role_summary=role_summary,
        status=JobStatus.OPEN,
    )
    session.add(job)
    session.flush()

    version = JobVersion(
        job_id=job.id,
        version=1,
        title_snapshot=title,
        description_snapshot=description,
        role_summary_snapshot=role_summary,
    )
    session.add(version)
    session.flush()

    for cr in _make_capability_requirements(session, version.id, capability_names or []):
        session.add(cr)

    job.head_version_id = version.id
    session.add(job)
    session.flush()
    return job


def create_job_version(
    session: Session,
    job_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
    role_summary: str | None = None,
    capability_names: list[str] | None = None,
) -> JobVersion:
    """Mint a new immutable JobVersion for ``job_id`` and advance the head.

    * If ``capability_names`` is None, the new version copies the head version's
      existing requirement set (a pure fork -- requirements unchanged).
    * If ``capability_names`` is given, the new version carries that (SSoT-checked)
      set instead -- this is how a job's requirements evolve over time.

    ``title``/``description``/``role_summary`` default to the head version's
    snapshot when not supplied, so callers can revise just the requirements.
    """
    job = session.get(Job, job_id)
    if job is None:
        raise ServiceError(404, f"job not found: {job_id}")

    head = (
        session.get(JobVersion, job.head_version_id)
        if job.head_version_id
        else None
    )
    if head is None:
        # Should not happen: create_job always wires a v1 head. Defensive only.
        raise ServiceError(409, f"job {job_id} has no head version to fork")

    version = _next_version(session, job_id)
    new_version = JobVersion(
        job_id=job_id,
        version=version,
        title_snapshot=title if title is not None else head.title_snapshot,
        description_snapshot=(
            description if description is not None else head.description_snapshot
        ),
        role_summary_snapshot=(
            role_summary if role_summary is not None else head.role_summary_snapshot
        ),
    )
    session.add(new_version)
    session.flush()

    if capability_names is None:
        # Copy the head version's requirement set (preserve history exactly).
        for cr in session.exec(
            select(CapabilityRequirement).where(
                CapabilityRequirement.job_version_id == head.id
            )
        ).all():
            session.add(
                CapabilityRequirement(
                    job_version_id=new_version.id,
                    capability_id=cr.capability_id,
                    capability_name=cr.capability_name,
                    min_proficiency=cr.min_proficiency,
                    required=cr.required,
                    notes=cr.notes,
                )
            )
    else:
        for cr in _make_capability_requirements(session, new_version.id, capability_names):
            session.add(cr)

    job.head_version_id = new_version.id
    session.add(job)
    session.flush()
    return new_version


def add_capability_requirement(
    session: Session,
    job_version_id: str,
    capability_name: str,
    *,
    min_proficiency: int = 50,
    required: bool = True,
    notes: str = "",
) -> CapabilityRequirement:
    """Add a single CapabilityRequirement to a JobVersion.

    Allowed ONLY on the head (active) version. Editing a historical version would
    corrupt traceability, so it is rejected with 409.
    """
    jv = session.get(JobVersion, job_version_id)
    if jv is None:
        raise ServiceError(404, f"job version not found: {job_version_id}")
    job = session.get(Job, jv.job_id)
    if job is None or job.head_version_id != job_version_id:
        raise ServiceError(
            409, "cannot modify a non-head (historical) job version"
        )
    capability_id = _resolve_capability_id(session, capability_name)
    cap = session.exec(
        select(Capability).where(Capability.id == capability_id)
    ).first()
    assert cap is not None
    cr = CapabilityRequirement(
        job_version_id=job_version_id,
        capability_id=capability_id,
        capability_name=cap.name,
        min_proficiency=min_proficiency,
        required=required,
        notes=notes,
    )
    session.add(cr)
    session.flush()
    return cr


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def get_job(session: Session, job_id: str) -> Job | None:
    return session.get(Job, job_id)


def list_jobs(
    session: Session,
    *,
    required_work_id: str | None = None,
    status: JobStatus | None = None,
) -> list[Job]:
    query = select(Job)
    if required_work_id is not None:
        query = query.where(Job.required_work_id == required_work_id)
    if status is not None:
        query = query.where(Job.status == status)
    return list(session.exec(query))


def get_job_version(session: Session, job_version_id: str) -> JobVersion | None:
    return session.get(JobVersion, job_version_id)


def list_job_versions(session: Session, job_id: str) -> list[JobVersion]:
    return list(
        session.exec(
            select(JobVersion)
            .where(JobVersion.job_id == job_id)
            .order_by(JobVersion.version)
        )
    )


def list_capability_requirements(
    session: Session, job_version_id: str
) -> list[CapabilityRequirement]:
    return list(
        session.exec(
            select(CapabilityRequirement).where(
                CapabilityRequirement.job_version_id == job_version_id
            )
        )
    )
