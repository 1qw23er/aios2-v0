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

W2 (Candidate Discovery) is implemented in this module as a STRICT SUBSET of the
V1.1 W2: it only *discovers* and *pools* candidates (Agent x Job x Evaluation
Context) by filtering the Agent Registry against a JobVersion's CapabilityRequirements.
It does NOT evaluate, match, score, benchmark, or trial -- those are W3+ and are
deliberately absent here. The Candidate lifecycle is the minimal W2 state machine
(POOLED <-> REJECTED); the W3 states (EVALUATING / EVALUATED / RECOMMENDED) are
reserved and cannot be entered in W2 (``CandidateLifecycle`` rejects them with 409).
Employee / Training / Performance remain out of scope.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from aios.agent_registry import get_agent, list_agents
from aios.audit import append_audit
from aios.models import (
    BusinessGoal,
    BusinessGoalStatus,
    Candidate,
    CandidateStatus,
    Capability,
    CapabilityRequirement,
    Job,
    JobStatus,
    JobVersion,
    RequiredWork,
    RequiredWorkStatus,
    now_utc,
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


# ---------------------------------------------------------------------------
# W2 -- Candidate Discovery (STRICT SUBSET: pool only, no evaluation/match/trial)
# ---------------------------------------------------------------------------


class CandidateLifecycle:
    """Explicit, boundary-checked W2 candidate state machine.

    W2 only knows POOLED <-> REJECTED. The W3 states (EVALUATING / EVALUATED /
    RECOMMENDED) are intentionally NOT part of W2 -- any transition attempting to
    enter them is rejected with 409, so Discovery can never silently drift into
    Evaluation territory. A REJECTED candidate may be re-pooled (returned to the
    pool) since the Agent Registry is the SSoT and the agent may have recovered.
    """

    ALLOWED: dict[CandidateStatus, set[CandidateStatus]] = {
        CandidateStatus.POOLED: {CandidateStatus.REJECTED},
        CandidateStatus.REJECTED: {CandidateStatus.POOLED},
    }

    @classmethod
    def can_transition(
        cls, frm: CandidateStatus, to: CandidateStatus
    ) -> bool:
        return to in cls.ALLOWED.get(frm, set())

    @classmethod
    def require_transition(
        cls, frm: CandidateStatus, to: CandidateStatus
    ) -> None:
        if not cls.can_transition(frm, to):
            raise ServiceError(
                409,
                f"illegal candidate state transition: {frm.value} -> {to.value}",
            )


def discover_candidates(
    session: Session,
    job_version_id: str,
    *,
    discoverer: str = "workforce_discovery",
) -> list[Candidate]:
    """Discover and pool candidates for ``job_version_id`` (W2, no evaluation).

    Discovery reads the JobVersion's CapabilityRequirements, resolves each required
    capability to the Alpha-1 SSoT, and intersects the set of *enabled* agents that
    declare *all* required (enabled) capabilities via the Agent Registry. Each
    matched agent becomes (or already is) a POOLED Candidate referencing the agent
    by id only -- no Agent Registry data is copied.

    Contracts (asserted by contract tests):
    * Unknown / non-existent JobVersion -> 404 fail-closed.
    * A JobVersion with no CapabilityRequirements has nothing to filter by -> 422.
    * Re-running discovery is idempotent: an existing (agent, job, job_version)
      triple is never duplicated (UNIQUE + explicit existence check).
    * Concurrent discovery is also idempotent. Each insert runs inside a SAVEPOINT;
      if another discovery committed the same triple between our initial read and
      our flush (a TOCTOU race), the UNIQUE violation is absorbed -- we read back
      the already-pooled candidate and return the same result a serial re-discovery
      would, never a 500. The final data is strictly unique.
    * No agent satisfies the full requirement set -> returns an empty list (not an
      error). The 422 only fires when there are NO requirements to filter against.
    * Every newly pooled candidate is written to the audit log, traceable to the
      job_version, with an empty ``evaluation_context`` (reserved for W3).
    * Discovery never enters EVALUATING/EVALUATED/RECOMMENDED; it only POOLs.
    """
    jv = session.get(JobVersion, job_version_id)
    if jv is None:
        raise ServiceError(404, f"job version not found: {job_version_id}")
    job = session.get(Job, jv.job_id)
    if job is None:
        raise ServiceError(404, f"job not found: {jv.job_id}")

    reqs = list_capability_requirements(session, job_version_id)
    if not reqs:
        # Nothing to filter against -> fail-closed rather than dumping the registry.
        raise ServiceError(
            422,
            "job version has no capability requirements to discover against",
        )

    # Required capability slugs (denormalized snapshot of the Alpha-1 SSoT names).
    required_names = [r.capability_name for r in reqs]

    # Intersect enabled agents that declare every required (enabled) capability.
    # ``list_agents(capability=...)`` already returns enabled agents with an enabled
    # AgentCapability for that SSoT capability, and fails closed on unknown slugs
    # (cannot happen here since reqs derive from the same SSoT).
    matched: set[str] | None = None
    for name in required_names:
        ids = {a.id for a in list_agents(session, capability=name)}
        matched = ids if matched is None else (matched & ids)
        if not matched:
            break  # no agent can satisfy the full requirement set
    matched = matched or set()

    existing = {
        c.agent_id
        for c in session.exec(
            select(Candidate).where(Candidate.job_version_id == job_version_id)
        ).all()
    }

    # Set when a concurrent discovery committed the same (agent, job_version) triple
    # between our `existing` read and our flush. In that case this session's snapshot
    # may be stale, so we resolve the final list from a fresh connection.
    conflict_absorbed = False

    for agent_id in sorted(matched):
        if agent_id in existing:
            continue  # idempotent: already discovered for this job version
        # Soft-reference check: the agent must still exist in the SSoT registry.
        # (It was returned by list_agents, so it does; kept explicit for clarity.)
        get_agent(session, agent_id)
        # Insert inside a SAVEPOINT so a concurrent discovery -- which may have
        # committed the same (agent_id, job_id, job_version_id) triple between our
        # initial `existing` read and this flush -- only rolls back THIS candidate,
        # never the whole discovery transaction. On a UNIQUE violation the conflict
        # is absorbed: we read back the already-pooled candidate and return a result
        # identical to a serial re-discovery. The operation stays strictly idempotent
        # and never surfaces a 500. (P2-1 hardening.)
        try:
            with session.begin_nested():
                cand = Candidate(
                    agent_id=agent_id,
                    job_id=job.id,
                    job_version_id=job_version_id,
                    status=CandidateStatus.POOLED,
                    discovered_by=discoverer,
                    evaluation_context={},
                )
                session.add(cand)
                session.flush()
                append_audit(
                    session,
                    actor=discoverer,
                    action="candidate.discover",
                    resource_type="candidate",
                    resource_id=cand.id,
                    project_id=None,
                    task_id=None,
                    before={},
                    after={
                        "agent_id": agent_id,
                        "job_id": job.id,
                        "job_version_id": job_version_id,
                        "status": cand.status.value,
                    },
                    idempotency_key=f"discover:{job_version_id}:{agent_id}",
                )
        except IntegrityError:
            # Another concurrent discovery won the race for this triple. The
            # SAVEPOINT has already been rolled back by `begin_nested`, so the outer
            # transaction -- and any other candidates pooled so far -- is intact.
            # Read the existing candidate back (fresh connection, so we see the
            # concurrently-committed row regardless of this session's snapshot) so
            # the returned list matches what a serial re-discovery would return.
            conflict_absorbed = True
            with Session(session.get_bind()) as fresh:
                fresh.exec(
                    select(Candidate)
                    .where(Candidate.agent_id == agent_id)
                    .where(Candidate.job_version_id == job_version_id)
                ).first()
            continue

    if conflict_absorbed:
        # A conflict was absorbed: resolve the authoritative, committed view from a
        # fresh connection so concurrently-inserted candidates are reflected.
        with Session(session.get_bind()) as fresh:
            return list_candidates(fresh, job_version_id=job_version_id)
    return list_candidates(session, job_version_id=job_version_id)


def get_candidate(session: Session, candidate_id: str) -> Candidate | None:
    return session.get(Candidate, candidate_id)


def list_candidates(
    session: Session,
    *,
    job_id: str | None = None,
    job_version_id: str | None = None,
    agent_id: str | None = None,
    status: CandidateStatus | None = None,
) -> list[Candidate]:
    q = select(Candidate)
    if job_id is not None:
        q = q.where(Candidate.job_id == job_id)
    if job_version_id is not None:
        q = q.where(Candidate.job_version_id == job_version_id)
    if agent_id is not None:
        q = q.where(Candidate.agent_id == agent_id)
    if status is not None:
        q = q.where(Candidate.status == status)
    q = q.order_by(Candidate.created_at, Candidate.id)
    return list(session.exec(q))


def reject_candidate(
    session: Session,
    candidate_id: str,
    *,
    actor: str = "workforce_discovery",
) -> Candidate:
    """Move a POOLED candidate to REJECTED (explicit lifecycle boundary)."""
    cand = session.get(Candidate, candidate_id)
    if cand is None:
        raise ServiceError(404, f"candidate not found: {candidate_id}")
    CandidateLifecycle.require_transition(cand.status, CandidateStatus.REJECTED)
    before = {"status": cand.status.value}
    cand.status = CandidateStatus.REJECTED
    cand.updated_at = now_utc()
    session.add(cand)
    session.flush()
    append_audit(
        session,
        actor=actor,
        action="candidate.reject",
        resource_type="candidate",
        resource_id=cand.id,
        project_id=None,
        task_id=None,
        before=before,
        after={"status": cand.status.value},
        idempotency_key=f"reject:{candidate_id}",
    )
    return cand


def repool_candidate(
    session: Session,
    candidate_id: str,
    *,
    actor: str = "workforce_discovery",
) -> Candidate:
    """Return a REJECTED candidate to POOLED (explicit lifecycle boundary)."""
    cand = session.get(Candidate, candidate_id)
    if cand is None:
        raise ServiceError(404, f"candidate not found: {candidate_id}")
    CandidateLifecycle.require_transition(cand.status, CandidateStatus.POOLED)
    before = {"status": cand.status.value}
    cand.status = CandidateStatus.POOLED
    cand.updated_at = now_utc()
    session.add(cand)
    session.flush()
    append_audit(
        session,
        actor=actor,
        action="candidate.repool",
        resource_type="candidate",
        resource_id=cand.id,
        project_id=None,
        task_id=None,
        before=before,
        after={"status": cand.status.value},
        idempotency_key=f"repool:{candidate_id}",
    )
    return cand
