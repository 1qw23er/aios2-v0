"""Workforce Management -- W1/W2/W3-A service layer (V1.1 Workforce Architecture).

This module implements the W1 minimum closed loop (definition time):

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

W2 (Candidate Discovery) discovers and pools candidates (Agent x Job x Evaluation
Context) by filtering the Agent Registry against a JobVersion's
CapabilityRequirements. It does NOT evaluate, match, score, benchmark, or trial.

W3-A (Evaluation) adds the first half of the evaluation loop:

    Candidate(POOLED) -> EVALUATING -> EVALUATED

``evaluate_candidate`` writes its evidence into ``Candidate.evaluation_context``
(W2's reserved JSON bag -- no new table). Its hard rules:

* The ONLY capability signal is ``AgentCapability.priority`` (Alpha-1 SSoT). The
  denormalized ``Agent.capabilities`` JSON mirror is never read.
* ``benchmark`` / ``cost`` / ``reliability`` / ``historical`` have no backing data
  yet, so they are recorded as ``unknown`` / ``future_capability`` and NEVER as a
  numeric score. Fabricating a placeholder score is a contract violation.
* Any failure rolls the candidate back to POOLED with ``evaluation_error`` -- the
  EVALUATING half-state must never survive the call.

Still absent (later stages): Recommendation (W3-C), Trial (W3-D), and
``EVALUATED -> RECOMMENDED`` (the W3-C Match gate, which consumes the W3-B Match and
must refuse any ``blocked`` one). Employee / Training / Performance stay out of scope.

W3-B (Match / Ranking & Benchmark) adds the *scoring* layer on top of W3-A's frozen
evaluation snapshot, as a side channel that NEVER mutates ``Candidate.status`` or
``Candidate.evaluation_context`` (W3-A is frozen, constraint 1):

    EVALUATED --(compute_match)--> Match(score, breakdown, evidence_refs)
              --(rank_candidates)--> Ranking (query only, no state change)
              --(run_benchmark)--> BenchmarkResult (provenance evidence only)

``compute_match`` reads W3-A's already-computed ``capability_evidence`` and (when a
trusted ``benchmark_result`` exists) folds in ``benchmark_score``. It is fail-closed:
a capability gap marks the Match ``blocked``; an untrusted/absent benchmark waives the
benchmark dimension instead of writing a fake 0. ``reliability`` / ``historical`` /
``cost`` remain ``future_capability`` / ``unknown`` and are excluded from the score
(constraint 2). Benchmark execution is a pluggable ``BenchmarkAdapter`` seam -- V1
ships a deterministic placeholder that records ``unknown`` (no ai-arena wiring,
constraint 8); a real adapter is plugged in later behind the same interface.

Spec: ``docs/Workforce_W3_Evaluation_Matching_Spec_V1.md`` (approved by R7).
Implementation design: ``docs/Workforce_W3A_Evaluation_Implementation_Design.md``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from aios.agent_registry import get_agent, list_agents
from aios.audit import append_audit
from aios.models import (
    AgentCapability,
    Benchmark,
    BenchmarkResult,
    BenchmarkResultStatus,
    BenchmarkVersion,
    BusinessGoal,
    BusinessGoalStatus,
    Candidate,
    CandidateStatus,
    Capability,
    CapabilityRequirement,
    Job,
    JobStatus,
    JobVersion,
    Match,
    MatchStatus,
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
    """Explicit, boundary-checked candidate state machine (W2 + W3-A).

    W2 owns POOLED <-> REJECTED: a REJECTED candidate may be re-pooled since the
    Agent Registry is the SSoT and the agent may have recovered.

    W3-A adds the *Evaluation* half of the lifecycle:

        POOLED -> EVALUATING -> EVALUATED
        EVALUATING -> POOLED          (evaluation failed; rolls back, never a half-state)
        EVALUATED -> REJECTED         (evaluation is an immutable snapshot)

    W3-C opens the *controlled* Match gate on top of ``EVALUATED``:

        EVALUATED -> RECOMMENDED       -- only via ``recommend_candidate``, and
                                          only when the Match is COMPUTED and
                                          every fail-closed gate (F-R1..F-R5)
                                          passes.
        RECOMMENDED -> EVALUATED       -- the ONLY outbound edge: a human
                                          REJECT (``decide_recommendation``) or
                                          a system withdraw (F-R8 drift
                                          reconcile).

    W3-D (Trial) opens one controlled edge on top of the W3-C wiring:

        RECOMMENDED -> TRIALING        -- only via ``create_trial_from_approval``
                                         (owner actor, after the L4 approval in
                                         W3-C). The ``TRIALING`` node had no
                                         outbound edge in W3-D (Trial
                                         activation / completion / cancellation
                                         is W4) -- W4 opened them, see below.
                                         Note C-5: once a candidate is TRIALING,
                                         the F-R8 drift reconcile will NOT
                                         release it back (``_sync_candidate_back``
                                         only releases RECOMMENDED), so a drifted
                                         approval leaves a ``TRIALING`` candidate
                                         behind -- W4 mitigates this partially
                                         with ``release_candidate`` (explicit
                                         release path) without modifying W3-C.

    W4 (Employee) opens the final two edges of the loop:

        TRIALING -> EMPLOYED          -- only via ``promote_to_employee``, and
                                        only when the Trial is COMPLETED
                                        (INV-E1). Completing a trial does NOT
                                        hire anyone (D-6 human gate).
        TRIALING -> POOLED            -- only via ``release_candidate``, and only
                                        when the Trial is FAILED or CANCELLED
                                        (D-2). This is also the *partial*
                                        mitigation of W3-D's known gap C-5: an
                                        explicit release path now exists, but
                                        the F-R8 withdraw path itself is left
                                        untouched (W3-C is frozen).
        EMPLOYED  -> (none)           -- terminal.

    Deliberately still absent:

        POOLED -> RECOMMENDED          -- the Match gate must be passed first.
        RECOMMENDED -> POOLED          -- the only way out is back to EVALUATED.
        TRIALING -> EVALUATED/REJECTED -- a failed trial releases to POOLED and
                                          re-evaluation starts from there.

    ``RECOMMENDED`` is a real ``CandidateStatus`` member (stable vocabulary). It
    was mapped to an empty edge set until W3-C wired the Match gate; the
    illegal-edge tests still assert the shortcut edges above are rejected, so
    the state cannot be reached outside the Match gate.

    Evaluation is treated as immutable history (like W1 JobVersion): re-evaluating
    requires ``EVALUATED -> REJECTED -> POOLED -> EVALUATING``, which keeps every
    attempt auditable.
    """

    ALLOWED: dict[CandidateStatus, set[CandidateStatus]] = {
        CandidateStatus.POOLED: {
            CandidateStatus.REJECTED,
            CandidateStatus.EVALUATING,
        },
        CandidateStatus.REJECTED: {CandidateStatus.POOLED},
        CandidateStatus.EVALUATING: {
            CandidateStatus.EVALUATED,
            CandidateStatus.POOLED,
        },
        CandidateStatus.EVALUATED: {
            CandidateStatus.REJECTED,
            # W3-C: the controlled Match gate. Traversed only by
            # recommend_candidate, and only with a COMPUTED Match (F-R1..F-R5).
            CandidateStatus.RECOMMENDED,
        },
        # W3-C: RECOMMENDED is now reachable, with exactly ONE outbound edge back
        # to EVALUATED (human REJECT / system withdraw). W3-D (Trial) opens a
        # SECOND, controlled outbound edge into TRIALING -- traversed only by
        # ``create_trial_from_approval`` (owner actor, after the L4 approval).
        CandidateStatus.RECOMMENDED: {
            CandidateStatus.EVALUATED,
            CandidateStatus.TRIALING,
        },
        # W4: TRIALING is no longer a dead end -- it has exactly TWO outbound
        # edges, and both are taken by an explicit owner action:
        #   TRIALING -> EMPLOYED  -- only via ``promote_to_employee``, and only
        #                            when the Trial is COMPLETED (INV-E1).
        #   TRIALING -> POOLED    -- only via ``release_candidate``, and only
        #                            when the Trial is FAILED or CANCELLED
        #                            (D-2: release back into the talent pool so
        #                            the candidate can be re-matched later; no
        #                            new terminal state is introduced).
        # There is deliberately NO TRIALING -> EVALUATED / REJECTED shortcut:
        # a failed trial returns to POOLED, and re-evaluation starts from there.
        CandidateStatus.TRIALING: {
            CandidateStatus.EMPLOYED,
            CandidateStatus.POOLED,
        },
        # W4: EMPLOYED is terminal -- zero outbound edges. Consistent with
        # EmployeeStatus having a single member (ACTIVE) and with W4 shipping no
        # delete semantics (D-4): there is no writer for "un-employ", so there
        # is no edge for it.
        CandidateStatus.EMPLOYED: set(),
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


# ---------------------------------------------------------------------------
# W3-A -- Candidate Evaluation (POOLED -> EVALUATING -> EVALUATED)
# ---------------------------------------------------------------------------

EVALUATION_CONTEXT_SCHEMA_V1 = "w3a.evaluation.v1"
"""Version tag stamped into ``Candidate.evaluation_context["schema_version"]``."""

PREFERRED_BONUS_WEIGHT = 0.05
"""Ceiling on the nudge a *preferred* (non-required) requirement can contribute.

``capability_fit`` is driven by the required requirements; preferred ones add at
most this share so they can never outvote a hard capability gap (Spec §5.1:
"微量加成，不喧宾夺主").
"""

_EVALUATION_ERROR_LIMIT = 500
"""Truncation applied to the recorded ``evaluation_error.message``."""


def _capability_fit_value(agent_priority: int, min_proficiency: int) -> float:
    """``clamp((p - min) / (100 - min), 0, 1)`` with an zero-division guard.

    ``CapabilityRequirement.min_proficiency`` is ``ge=1, le=100``, so a threshold
    of 100 makes the denominator zero. That edge is pinned explicitly instead of
    raising: full marks only at priority 100, otherwise nothing.
    """
    if min_proficiency >= 100:
        return 1.0 if agent_priority >= 100 else 0.0
    return max(
        0.0, min(1.0, (agent_priority - min_proficiency) / (100 - min_proficiency))
    )


def _collect_capability_evidence(
    session: Session,
    agent_id: str,
    reqs: list[CapabilityRequirement],
) -> dict[str, Any]:
    """Build the capability-fit evidence block from the Alpha-1 SSoT.

    The ONLY signal consulted is ``AgentCapability.priority``. The denormalized
    ``Agent.capabilities`` JSON mirror is display-only and is never read here.
    An undeclared or disabled capability counts as priority 0 -- fail-closed,
    never "unknown, assume average" (Spec §2.2).
    """
    rows: list[dict[str, Any]] = []
    required_fits: list[float] = []
    preferred_fits: list[float] = []
    blocked: list[str] = []

    for req in reqs:
        ac = session.exec(
            select(AgentCapability)
            .where(AgentCapability.agent_id == agent_id)
            .where(AgentCapability.capability_id == req.capability_id)
        ).first()
        declared = ac is not None
        enabled = bool(ac.enabled) if declared else False
        # Spec §2.2: undeclared OR disabled -> priority 0 -> fail-closed.
        priority = int(ac.priority) if (declared and enabled) else 0
        meets = priority >= req.min_proficiency
        rows.append(
            {
                "capability_id": req.capability_id,
                "capability_name": req.capability_name,
                "required": bool(req.required),
                "min_proficiency": int(req.min_proficiency),
                "agent_priority": priority,
                "declared": declared,
                "capability_enabled": enabled,
                "meets_threshold": meets,
                "fit": _capability_fit_value(priority, int(req.min_proficiency)),
            }
        )
        if req.required:
            required_fits.append(rows[-1]["fit"])
            if not meets:
                blocked.append(req.id)
        else:
            preferred_fits.append(rows[-1]["fit"])

    if not required_fits:
        # Fail closed: with no hard threshold there is nothing to score, and
        # inventing a default (e.g. 0.5) would be a fabricated score. Matches
        # discover_candidates' "nothing to filter against -> 422" discipline.
        raise ServiceError(
            422,
            "job version has no required capability requirements to evaluate against",
        )

    base = sum(required_fits) / len(required_fits)
    if preferred_fits:
        bonus = PREFERRED_BONUS_WEIGHT * (sum(preferred_fits) / len(preferred_fits))
        capability_fit = min(1.0, base + bonus)
    else:
        capability_fit = base

    return {
        "status": "computed",
        "requirements": rows,
        "capability_fit": capability_fit,
        "threshold_passed": not blocked,
        "blocked_requirements": blocked,
    }


def _build_evaluation_context(
    session: Session,
    cand: Candidate,
    *,
    evaluator: str,
    attempt: int,
) -> dict[str, Any]:
    """Assemble the full W3-A evidence bag for ``cand``.

    Only ``capability_evidence`` is genuinely computed here. The other four
    dimensions have NO backing data in the current model (no benchmark table, no
    cost schema, no reliability series, no Employee history), so they are recorded
    as ``unknown`` / ``future_capability`` -- never as a numeric score. That is the
    "no fabricated scores" contract (Spec §2.5 F3, §3.4, §3.6) and it is asserted
    by the contract tests.
    """
    # Soft reference to the Alpha-1 registry: a retired agent is a hard 404.
    get_agent(session, cand.agent_id)
    reqs = list_capability_requirements(session, cand.job_version_id)
    capability = _collect_capability_evidence(session, cand.agent_id, reqs)
    return {
        "schema_version": EVALUATION_CONTEXT_SCHEMA_V1,
        "attempt": attempt,
        "evaluated_at": now_utc().isoformat(),
        "evaluator": evaluator,
        # Spec §2.1: the components actually computed (drives explainability).
        "evaluated_fields": ["capability_fit"],
        "capability_evidence": capability,
        "benchmark_evidence": {
            "status": "unknown",
            "waived": True,
            "reason": "JobVersion has no benchmark_version binding yet (W3-B)",
        },
        "cost_evidence": {
            "status": "unknown",
            "reason": "Agent.cost_policy has no defined schema (W5 Budget domain)",
        },
        "reliability_evidence": {
            "status": "future_capability",
            "reason": "Alpha-1 Agent model has no success-rate / availability series",
        },
        "historical_evidence": {
            "status": "future_capability",
            "reason": "Employee / Performance data is W4+",
        },
        # Spec §2.5 F1: a hard capability gap blocks recommendation. W3-A has no
        # EVALUATED -> RECOMMENDED edge yet; this field is the forward contract
        # the W3-C/D Match gate must honour.
        "recommendation_blocked_reason": (
            None if capability["threshold_passed"] else "capability_gap"
        ),
        "evaluation_error": None,
    }


def evaluate_candidate(
    session: Session,
    candidate_id: str,
    *,
    evaluator: str = "workforce_evaluation",
) -> Candidate:
    """Evaluate a POOLED candidate (W3-A): POOLED -> EVALUATING -> EVALUATED.

    Contracts (asserted by ``tests/test_workforce_evaluation_w3a.py``):

    * An already-EVALUATED candidate is returned unchanged -- replaying the call
      writes no second audit row and does not bump ``attempt`` (idempotent).
    * Entering EVALUATING is legal only from POOLED. REJECTED must be re-pooled
      first and EVALUATED is an immutable snapshot; anything else is a 409.
    * An EVALUATING candidate (residue of a crashed run) is *resumed*: the call
      recomputes from scratch and completes it, so no half-state can survive.
    * Any failure rolls the candidate back to POOLED with ``evaluation_error`` and
      the exception is re-raised -- the caller always learns the evaluation
      failed. The EVALUATING half-state never survives the call.
    * Evidence lands in ``Candidate.evaluation_context``; no table is created.
    """
    cand = session.get(Candidate, candidate_id)
    if cand is None:
        raise ServiceError(404, f"candidate not found: {candidate_id}")

    if cand.status == CandidateStatus.EVALUATED:
        # Idempotent replay: an evaluation is an immutable snapshot, so the
        # existing result IS the answer. No state change, no duplicate audit.
        return cand

    # EVALUATING is resumable (crash recovery); every other source state must be
    # a legal transition target check.
    if cand.status != CandidateStatus.EVALUATING:
        CandidateLifecycle.require_transition(cand.status, CandidateStatus.EVALUATING)

    attempt = int(cand.evaluation_context.get("attempt", 0)) + 1
    before = {"status": cand.status.value, "attempt": attempt}
    # Remembered so a failed *claim* (409) can put the row back exactly where it
    # was. Without this the candidate would stay EVALUATING in the caller's
    # session and a caller that swallows the 409 and commits would persist the
    # half-state -- the same failure mode the evaluation body guards against.
    prior_status = cand.status

    cand.status = CandidateStatus.EVALUATING
    cand.updated_at = now_utc()
    session.add(cand)
    session.flush()
    try:
        with session.begin_nested():
            append_audit(
                session,
                actor=evaluator,
                action="candidate.evaluate.start",
                resource_type="candidate",
                resource_id=cand.id,
                project_id=None,
                task_id=None,
                before=before,
                after={
                    "status": CandidateStatus.EVALUATING.value,
                    "attempt": attempt,
                },
                idempotency_key=f"evaluate:start:{candidate_id}:{attempt}",
            )
            session.flush()
    except IntegrityError as exc:
        # Another evaluation already claimed (candidate, attempt) -- i.e. a
        # concurrent caller. The SAVEPOINT has rolled back so the outer
        # transaction is intact; read the authoritative row from a fresh
        # connection (mirrors the P2-1 pattern in ``discover_candidates``).
        with Session(session.get_bind()) as fresh:
            authoritative = fresh.get(Candidate, candidate_id)
        if (
            authoritative is not None
            and authoritative.status == CandidateStatus.EVALUATED
        ):
            # The other caller completed the work; adopt its committed result.
            session.expire(cand)
            return session.get(Candidate, candidate_id) or cand
        # Still POOLED / EVALUATING: we must never report success for work we did
        # not do -- that would be a silent fail-open. Restore the pre-claim status
        # first so a caller that swallows this 409 and commits still leaves the
        # row in a legal, resumable state (never an EVALUATING half-state).
        cand.status = prior_status
        session.add(cand)
        session.flush()
        raise ServiceError(
            409, f"candidate evaluation already in progress: {candidate_id}"
        ) from exc

    try:
        context = _build_evaluation_context(
            session, cand, evaluator=evaluator, attempt=attempt
        )
    except Exception as exc:
        # Spec §2.4 / F2: never leave the candidate in the EVALUATING half-state.
        cand.status = CandidateStatus.POOLED
        cand.evaluation_context = {
            **cand.evaluation_context,
            "attempt": attempt,
            "evaluation_error": {
                "type": type(exc).__name__,
                "message": str(exc)[:_EVALUATION_ERROR_LIMIT],
            },
        }
        cand.updated_at = now_utc()
        session.add(cand)
        session.flush()
        append_audit(
            session,
            actor=evaluator,
            action="candidate.evaluate.error",
            resource_type="candidate",
            resource_id=cand.id,
            project_id=None,
            task_id=None,
            before=before,
            after={
                "status": CandidateStatus.POOLED.value,
                "attempt": attempt,
                "error": cand.evaluation_context["evaluation_error"],
            },
            idempotency_key=f"evaluate:error:{candidate_id}:{attempt}",
        )
        raise

    cand.evaluation_context = context
    cand.status = CandidateStatus.EVALUATED
    cand.updated_at = now_utc()
    session.add(cand)
    session.flush()
    append_audit(
        session,
        actor=evaluator,
        action="candidate.evaluate",
        resource_type="candidate",
        resource_id=cand.id,
        project_id=None,
        task_id=None,
        before=before,
        after={
            "status": CandidateStatus.EVALUATED.value,
            "attempt": attempt,
            "evaluated_fields": context["evaluated_fields"],
            "capability_fit": context["capability_evidence"]["capability_fit"],
            "recommendation_blocked_reason": context["recommendation_blocked_reason"],
        },
        idempotency_key=f"evaluate:{candidate_id}:{attempt}",
    )
    return cand


# ---------------------------------------------------------------------------
# W3-B -- Match / Ranking & Benchmark (side channel over W3-A's frozen snapshot)
# ---------------------------------------------------------------------------
#
# W3-B NEVER writes Candidate.status or Candidate.evaluation_context (W3-A frozen,
# constraint 1). It consumes the capability_evidence W3-A already computed and adds
# a benchmark provenance layer. All writes go to the four W3-B tables only.
# ---------------------------------------------------------------------------

MATCH_WEIGHTS_VERSION = "w3b.match.v1"
"""Version tag stamped into ``Match.weights_version`` and ``Match.breakdown``."""

MATCH_WEIGHTS_V1: dict[str, float] = {
    "capability_fit": 0.6,
    "benchmark_score": 0.4,  # only counted when bound AND a trusted result exists
}
"""W3-B aggregation weights (defined here, at the point of first use -- D9)."""


# ---------------------------------------------------------------------------
# Benchmark template + version bookkeeping
# ---------------------------------------------------------------------------


def create_benchmark(
    session: Session,
    *,
    name: str,
    description: str = "",
    owner: str = "workforce",
) -> Benchmark:
    """Create a named benchmark template (the version chain hangs off it)."""
    bench = Benchmark(name=name, description=description, owner=owner)
    try:
        with session.begin_nested():
            session.add(bench)
            session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ServiceError(
            409, f"benchmark name already exists: {name}"
        ) from exc
    return bench


def create_benchmark_version(
    session: Session,
    *,
    benchmark_id: str,
    definition_json: dict[str, Any],
    version: int | None = None,
) -> BenchmarkVersion:
    """Mint an immutable BenchmarkVersion.

    ``definition_json`` is frozen on write -- it must NEVER be UPDATE'd (use a new
    version instead, see ``update_benchmark_version_definition`` which refuses). The
    service auto-assigns ``version`` (max+1 per benchmark) when omitted.
    """
    parent = session.get(Benchmark, benchmark_id)
    if parent is None:
        raise ServiceError(404, f"benchmark not found: {benchmark_id}")

    if version is None:
        current = session.exec(
            select(BenchmarkVersion.version)
            .where(BenchmarkVersion.benchmark_id == benchmark_id)
        ).all()
        version = (max(current) if current else 0) + 1

    bv = BenchmarkVersion(
        benchmark_id=benchmark_id,
        version=version,
        definition_json=definition_json,
    )
    try:
        with session.begin_nested():
            session.add(bv)
            session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ServiceError(
            409,
            f"benchmark_version already exists: benchmark={benchmark_id} version={version}",
        ) from exc
    return bv


def _require_benchmark_version(
    session: Session, benchmark_version_id: str
) -> BenchmarkVersion:
    bv = session.get(BenchmarkVersion, benchmark_version_id)
    if bv is None:
        raise ServiceError(
            404, f"benchmark_version not found: {benchmark_version_id}"
        )
    return bv


def update_benchmark_version_definition(
    session: Session, benchmark_version_id: str, new_definition_json: dict[str, Any]
) -> BenchmarkVersion:
    """Refuse to mutate a frozen BenchmarkVersion (immutability guard, T-BENCH-1).

    Changing a benchmark MUST mint a new version, never re-bind the old one -- this
    prevents silent distortion of historical evaluations (W3-Spec §4.4 / risk P1-1).
    """
    _require_benchmark_version(session, benchmark_version_id)
    raise ServiceError(
        409,
        "benchmark_version.definition_json is immutable; mint a new version instead",
    )


def bind_job_version_benchmark(
    session: Session, job_version_id: str, benchmark_version_id: str
) -> JobVersion:
    """Bind a (head) JobVersion to a BenchmarkVersion.

    Only the head version should be bound (W1 immutable-history principle); the
    caller owns that guarantee. Unbinding is ``bind_job_version_benchmark(session,
    jv_id, None)``.
    """
    jv = session.get(JobVersion, job_version_id)
    if jv is None:
        raise ServiceError(404, f"job_version not found: {job_version_id}")
    if benchmark_version_id is not None:
        _require_benchmark_version(session, benchmark_version_id)
    jv.benchmark_version_id = benchmark_version_id
    session.add(jv)
    session.flush()
    return jv


# ---------------------------------------------------------------------------
# Benchmark execution seam (Adapter) + deterministic V1 placeholder
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkOutcome:
    """What a ``BenchmarkAdapter.run`` returns.

    The adapter owns *execution* only; persistence (id, audit, reproducibility
    hash) stays in ``run_benchmark``. ``trusted=False`` is the fail-closed signal:
    ``run_benchmark`` records ``status="unknown"`` and never writes a fake score.
    """

    passed_cases: int | None
    total_cases: int | None
    quality_score: float | None
    output_ref: str | None = None
    environment: str = ""
    trusted: bool = True


class BenchmarkAdapter(Protocol):
    """Execution seam for a benchmark run (design-spec §3.1).

    Implementations plug in behind this interface. V1 ships only a deterministic
    placeholder (no ai-arena / external LLM wiring -- constraint 8); a real adapter
    is added later and must NOT reverse-depend on the Workforce core.
    """

    def run(
        self, candidate: Candidate, benchmark_version: BenchmarkVersion
    ) -> BenchmarkOutcome: ...


@dataclass
class _DefaultBenchmarkAdapter:
    """V1 placeholder: no execution backend exists yet.

    Returns an untrusted outcome so ``run_benchmark`` records ``status="unknown"``
    (fail-closed) rather than fabricating a score. A real adapter replaces this.
    """

    def run(
        self, candidate: Candidate, benchmark_version: BenchmarkVersion
    ) -> BenchmarkOutcome:
        return BenchmarkOutcome(
            passed_cases=None,
            total_cases=None,
            quality_score=None,
            trusted=False,
        )


def _case_set_hash(definition_json: dict[str, Any]) -> str:
    blob = json.dumps(definition_json or {}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _reproducibility_hash(
    benchmark_version_id: str,
    definition_json: dict[str, Any],
    agent_id: str,
    agent_snapshot: list[dict[str, Any]],
    input_hash: str,
) -> str:
    """Hash of the five provenance pillars (constraint 6: traceable, not bit-exact).

    Same five inputs -> same hash; changing any one (agent capability, input,
    version, case set, or agent) -> different hash -> treated as a different run.
    """
    components = {
        "benchmark_version_id": benchmark_version_id,
        "case_set_hash": _case_set_hash(definition_json),
        "agent_id": agent_id,
        "agent_capability_snapshot": agent_snapshot,
        "input_hash": input_hash,
    }
    blob = json.dumps(components, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def run_benchmark(
    session: Session,
    candidate_id: str,
    benchmark_version_id: str,
    run_id: str,
    *,
    adapter: BenchmarkAdapter | None = None,
    environment: str = "",
    input_hash: str = "",
    evaluator: str = "workforce_benchmark",
) -> BenchmarkResult:
    """Run (or re-run idempotently) a benchmark for a candidate.

    Idempotency: replaying the same ``(candidate_id, benchmark_version_id, run_id)``
    returns the existing ``BenchmarkResult`` without re-executing. A concurrent
    first-run race is absorbed by a SAVEPOINT (P2-1): the winning row is adopted,
    otherwise 409 (fail-closed, never silent success).

    fail-closed (F2): if the adapter raises or returns an untrusted / unverifiable
    outcome, the row is still written with ``status="unknown"`` and
    ``passed_cases/quality_score=None`` -- never a fake 0 or estimate.
    """
    cand = session.get(Candidate, candidate_id)
    if cand is None:
        raise ServiceError(404, f"candidate not found: {candidate_id}")
    bv = _require_benchmark_version(session, benchmark_version_id)

    existing = session.exec(
        select(BenchmarkResult).where(
            BenchmarkResult.candidate_id == candidate_id,
            BenchmarkResult.benchmark_version_id == benchmark_version_id,
            BenchmarkResult.run_id == run_id,
        )
    ).first()
    if existing is not None:
        return existing

    caps = session.exec(
        select(AgentCapability).where(AgentCapability.agent_id == cand.agent_id)
    ).all()
    snapshot = [
        {
            "capability_id": c.capability_id,
            "priority": int(c.priority),
            "enabled": bool(c.enabled),
        }
        for c in caps
    ]
    repro_hash = _reproducibility_hash(
        bv.id, bv.definition_json, cand.agent_id, snapshot, input_hash
    )

    adapter_impl = adapter if adapter is not None else _DefaultBenchmarkAdapter()
    outcome: BenchmarkOutcome | None = None
    try:
        outcome = adapter_impl.run(cand, bv)
    except Exception:
        outcome = None  # fail-closed: treat any adapter error as untrusted

    if (
        outcome is not None
        and outcome.trusted
        and outcome.passed_cases is not None
        and outcome.total_cases
        and outcome.total_cases > 0
    ):
        status = BenchmarkResultStatus.RECORDED
        passed_cases = int(outcome.passed_cases)
        total_cases = int(outcome.total_cases)
        quality_score = (
            float(outcome.quality_score)
            if outcome.quality_score is not None
            else None
        )
        output_ref = outcome.output_ref
        env = outcome.environment or environment
    else:
        status = BenchmarkResultStatus.UNKNOWN
        passed_cases = None
        total_cases = None
        quality_score = None
        output_ref = None
        env = environment

    result = BenchmarkResult(
        candidate_id=candidate_id,
        benchmark_version_id=benchmark_version_id,
        run_id=run_id,
        passed_cases=passed_cases,
        total_cases=total_cases,
        quality_score=quality_score,
        input_hash=input_hash,
        output_ref=output_ref,
        agent_snapshot_json=snapshot,
        environment=env,
        reproducibility_hash=repro_hash,
        status=status,
        evaluator=evaluator,
    )
    try:
        with session.begin_nested():
            session.add(result)
            session.flush()
            append_audit(
                session,
                actor=evaluator,
                action="benchmark.run",
                resource_type="benchmark_result",
                resource_id=result.id,
                project_id=None,
                task_id=None,
                before={},
                after={
                    "benchmark_result_id": result.id,
                    "status": status.value,
                    "reproducibility_hash": repro_hash,
                },
                idempotency_key=(
                    f"benchmark:run:{candidate_id}:{benchmark_version_id}:{run_id}"
                ),
            )
    except IntegrityError as exc:
        with Session(session.get_bind()) as fresh:
            auth = fresh.exec(
                select(BenchmarkResult).where(
                    BenchmarkResult.candidate_id == candidate_id,
                    BenchmarkResult.benchmark_version_id == benchmark_version_id,
                    BenchmarkResult.run_id == run_id,
                )
            ).first()
        if auth is not None:
            session.expire(result)
            return auth
        raise ServiceError(
            409,
            f"benchmark run already in progress: "
            f"{candidate_id}:{benchmark_version_id}:{run_id}",
        ) from exc
    return result


# ---------------------------------------------------------------------------
# Match / Ranking (read-only over W3-A's frozen evaluation_context)
# ---------------------------------------------------------------------------


def _attempt_from_evidence_refs(refs: list[str]) -> int | None:
    if refs:
        parts = refs[0].split(":")
        # ["cand", candidate_id, "attempt", N]
        if (
            len(parts) == 4
            and parts[0] == "cand"
            and parts[2] == "attempt"
        ):
            try:
                return int(parts[3])
            except ValueError:
                return None
    return None


def compute_match(
    session: Session,
    candidate_id: str,
    job_version_id: str,
    *,
    evaluator: str = "workforce_match",
) -> Match:
    """Score an EVALUATED candidate against a JobVersion (W3-B side channel).

    Reads W3-A's ``capability_evidence`` from ``Candidate.evaluation_context`` -- it
    does NOT recompute capability_fit, and it NEVER writes ``Candidate.status`` or
    ``evaluation_context`` (W3-A frozen, constraint 1).

    Contracts (asserted by ``tests/test_workforce_benchmark_match_w3b.py``):

    * F3: candidate must be ``EVALUATED`` and carry ``capability_evidence``;
      otherwise ``ServiceError(422, "candidate not evaluable: ...")`` -- no silent
      score.
    * Unbound JobVersion (no ``benchmark_version_id``) -> ``benchmark_score`` is
      waived and ``score == capability_fit`` (single-component normalization).
    * F2: a bound-but-untrusted benchmark (no ``recorded`` result) -> waived, no
      fake 0. A ``recorded`` result contributes ``0.4 * (passed/total)``.
    * F1: ``capability_evidence.threshold_passed is False`` -> the Match is still
      produced (scored to the floor) but marked ``status="blocked"`` with
      ``match_blocked_reason="capability_gap"`` -- W3-C must refuse to recommend it.
    * Idempotent: replay of the same evaluation ``attempt`` returns the existing
      Match (no recompute, no duplicate audit). A re-evaluation (new ``attempt``)
      recomputes and UPDATEs the row, recording ``match.recomputed`` before/after.
    * Concurrency: first-run races are absorbed by a SAVEPOINT (P2-1).
    """
    cand = session.get(Candidate, candidate_id)
    if cand is None:
        raise ServiceError(404, f"candidate not found: {candidate_id}")
    if cand.status != CandidateStatus.EVALUATED:
        raise ServiceError(
            422,
            f"candidate not evaluable: status is {cand.status.value}, "
            "expected evaluated",
        )

    ctx = cand.evaluation_context or {}
    evidence = ctx.get("capability_evidence")
    if not isinstance(evidence, dict) or "capability_fit" not in evidence:
        raise ServiceError(
            422,
            "candidate not evaluable: evaluation_context has no capability_evidence "
            "(run evaluate_candidate first)",
        )

    capability_fit = float(evidence["capability_fit"])
    threshold_passed = bool(evidence.get("threshold_passed", False))
    attempt = int(ctx.get("attempt", 0))

    jv = session.get(JobVersion, job_version_id)
    if jv is None:
        raise ServiceError(404, f"job_version not found: {job_version_id}")
    bound_bv_id = jv.benchmark_version_id

    # Resolve a trusted benchmark_score, if any.
    benchmark_score: float | None = None
    trusted_result: BenchmarkResult | None = None
    if bound_bv_id is not None:
        trusted_result = session.exec(
            select(BenchmarkResult)
            .where(BenchmarkResult.candidate_id == candidate_id)
            .where(BenchmarkResult.benchmark_version_id == bound_bv_id)
            .where(BenchmarkResult.status == BenchmarkResultStatus.RECORDED)
            .order_by(BenchmarkResult.created_at.desc())
        ).first()
        if (
            trusted_result is not None
            and trusted_result.total_cases
            and trusted_result.total_cases > 0
            and trusted_result.passed_cases is not None
        ):
            benchmark_score = trusted_result.passed_cases / trusted_result.total_cases

    benchmark_counted = bound_bv_id is not None and benchmark_score is not None
    if benchmark_counted:
        score = (
            MATCH_WEIGHTS_V1["capability_fit"] * capability_fit
            + MATCH_WEIGHTS_V1["benchmark_score"] * benchmark_score
        )
    else:
        # Single component: capability_fit is normalized to the full weight.
        score = capability_fit
    score = max(0.0, min(1.0, score))

    if not threshold_passed:
        status = MatchStatus.BLOCKED
        blocked_reason = "capability_gap"
    else:
        status = MatchStatus.COMPUTED
        blocked_reason = None

    breakdown: dict[str, Any] = {
        "weights_version": MATCH_WEIGHTS_VERSION,
        "formula": (
            "0.6*capability_fit + 0.4*benchmark_score "
            "(benchmark_score waived if unbound or untrusted)"
        ),
        "capability_fit": {
            "value": capability_fit,
            "weight": MATCH_WEIGHTS_V1["capability_fit"],
            "source": "evaluation_context.capability_evidence",
            "threshold_passed": threshold_passed,
        },
        "benchmark_score": {
            "value": benchmark_score,
            "weight": MATCH_WEIGHTS_V1["benchmark_score"],
            "status": "computed" if benchmark_counted else "waived",
            "reason": (
                None
                if benchmark_counted
                else (
                    "JobVersion unbound"
                    if bound_bv_id is None
                    else "no recorded/trusted benchmark_result"
                )
            ),
        },
        "excluded": [
            "reliability(future_capability)",
            "historical(future_capability)",
            "cost(unknown/advisory)",
        ],
    }
    evidence_refs = [f"cand:{candidate_id}:attempt:{attempt}"]
    if trusted_result is not None:
        evidence_refs.append(f"br:{trusted_result.id}")
    evaluated_fields = ["capability_fit"]
    if benchmark_counted:
        evaluated_fields.append("benchmark_score")

    existing = session.exec(
        select(Match).where(
            Match.candidate_id == candidate_id,
            Match.job_version_id == job_version_id,
        )
    ).first()

    if existing is not None:
        if _attempt_from_evidence_refs(existing.evidence_refs) == attempt:
            # Idempotent replay of the same evaluation: no recompute, no audit.
            return existing
        # Re-evaluation (new attempt): UPDATE in place, keep the audit trail.
        before_score = existing.score
        existing.score = score
        existing.weights_version = MATCH_WEIGHTS_VERSION
        existing.breakdown = breakdown
        existing.evaluated_fields = evaluated_fields
        existing.evidence_refs = evidence_refs
        existing.benchmark_version_id = bound_bv_id
        existing.status = status
        existing.match_blocked_reason = blocked_reason
        existing.evaluator = evaluator
        existing.created_at = now_utc()
        session.add(existing)
        session.flush()
        append_audit(
            session,
            actor=evaluator,
            action="match.recomputed",
            resource_type="match",
            resource_id=existing.id,
            project_id=None,
            task_id=None,
            before={"score": before_score, "status": existing.status.value},
            after={"score": score, "status": status.value},
            idempotency_key=(
                f"match:{candidate_id}:{job_version_id}:{attempt}"
            ),
        )
        return existing

    match = Match(
        candidate_id=candidate_id,
        job_version_id=job_version_id,
        score=score,
        weights_version=MATCH_WEIGHTS_VERSION,
        breakdown=breakdown,
        evaluated_fields=evaluated_fields,
        evidence_refs=evidence_refs,
        benchmark_version_id=bound_bv_id,
        status=status,
        match_blocked_reason=blocked_reason,
        evaluator=evaluator,
    )
    try:
        with session.begin_nested():
            session.add(match)
            session.flush()
            append_audit(
                session,
                actor=evaluator,
                action="match.computed",
                resource_type="match",
                resource_id=match.id,
                project_id=None,
                task_id=None,
                before={},
                after={
                    "score": score,
                    "breakdown": breakdown,
                    "evaluated_fields": evaluated_fields,
                    "status": status.value,
                },
                idempotency_key=f"match:{candidate_id}:{job_version_id}",
            )
    except IntegrityError as exc:
        with Session(session.get_bind()) as fresh:
            auth = fresh.exec(
                select(Match).where(
                    Match.candidate_id == candidate_id,
                    Match.job_version_id == job_version_id,
                )
            ).first()
        if auth is not None:
            session.expire(match)
            return auth
        raise ServiceError(
            409,
            f"match already being computed: {candidate_id}:{job_version_id}",
        ) from exc
    return match


def rank_candidates(
    session: Session, job_version_id: str
) -> list[Match]:
    """Rank an EVALUATED candidate pool for a JobVersion (query only, no state).

    Order: ``score`` desc, tie-break ``capability_fit`` -> ``benchmark_score`` ->
    ``agent_id`` (Spec §2.7, deterministic). ``blocked`` Matches sort after all
    ``computed`` ones (they must never be recommended). Pure read: no table writes,
    no audit (V1 does not materialize a ranking snapshot; that is W3-C's job).
    """
    rows = session.exec(
        select(Match, Candidate.agent_id)
        .join(Candidate, Match.candidate_id == Candidate.id)
        .where(Match.job_version_id == job_version_id)
        .where(Candidate.status == CandidateStatus.EVALUATED)
    ).all()
    agent_of = {m.id: agent_id for m, agent_id in rows}

    def _sort_key(m: Match):
        cap = float(m.breakdown.get("capability_fit", {}).get("value", 0.0))
        bench = m.breakdown.get("benchmark_score", {}).get("value") or 0.0
        blocked_rank = 0 if m.status == MatchStatus.COMPUTED else 1
        # Deterministic tie-break by agent_id (not candidate_id) per Spec §2.7.
        return (blocked_rank, -m.score, -cap, -float(bench), agent_of.get(m.id, ""))

    return sorted((m for m, _ in rows), key=_sort_key)
