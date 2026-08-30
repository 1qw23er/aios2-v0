"""Personal-IP content & monetization workflow tests (#108-A).

Implements the TDD plan from ``docs/issue-108-a-plan.md`` (v3). Covers the
service-layer contracts (creation, locked update, independent review, owner
approval bound to an exact revision, rejection, append-only metrics,
per-Artifact same-project authorization, SQLite ``BEGIN IMMEDIATE`` concurrency,
and the zero-migration proof). A small HTTP smoke subset exercises the FastAPI
surface with dependency overrides so it stays deterministic and offline.

No test performs a paid model call: the default ``FakeReviewAdapter`` is used
everywhere (plan v3 §4).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, select

from aios.actor import ActorContext
from aios.audit import AuditLog
from aios.content_draft import (
    CONTENT_DRAFT_APPROVE_ACTION,
    CONTENT_DRAFT_REVIEW_AUDIT,
    ContentDraftService,
    ContentReviewResult,
    FakeReviewAdapter,
    _compute_checksum,
)
from aios.db import get_engine, run_migrations
from aios.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    DelegatedRun,
    Event,
    KnowledgeFact,
    Project,
    Task,
)
from aios.services import ServiceError

HEAD = "20260827_0002_workforce_candidate"

OWNER = ActorContext(kind="owner", owner_id="owner")
AGENT_PRODUCER = ActorContext(kind="agent", agent_id="producer-1")
AGENT_OTHER = ActorContext(kind="agent", agent_id="other-1")
# The FakeReviewAdapter reviewers every submitted draft (identity is stable and
# must differ from any producer), so a reviewer agent can read its assigned work.
AGENT_REVIEWER = ActorContext(kind="agent", agent_id="content-review-fake")
SYSTEM = ActorContext.system()


class _LowConfidenceAdapter(FakeReviewAdapter):
    """Deterministic adapter that PASSES but with low confidence.

    Exercises plan v3 §4 invariant 3: a low-confidence pass must fail closed to
    NEEDS_REVISION rather than REVIEW_PASSED.
    """

    def review(self, *, artifact, producer_identity):
        return ContentReviewResult(
            result="review_passed", confidence=0.1, bounded_reason="low confidence pass"
        )


class _NeedsRevisionAdapter(FakeReviewAdapter):
    """Deterministic first-round reviewer used by the re-review regression."""

    def review(self, *, artifact, producer_identity):
        return ContentReviewResult(
            result="needs_revision",
            confidence=1.0,
            bounded_reason="revise before approval",
        )


class _BarrierReviewAdapter(FakeReviewAdapter):
    """Hold two exact-review requests until both captured the same revision."""

    def __init__(self, barrier: Barrier):
        self._barrier = barrier

    def review(self, *, artifact, producer_identity):
        self._barrier.wait()
        return super().review(
            artifact=artifact,
            producer_identity=producer_identity,
        )


class _OutOfRangeConfidenceAdapter(FakeReviewAdapter):
    """Passes with a confidence above the documented 0..1 range."""

    def review(self, *, artifact, producer_identity):
        return ContentReviewResult(
            result="review_passed", confidence=2.0, bounded_reason="out of range"
        )


class _NonNumericConfidenceAdapter(FakeReviewAdapter):
    """Returns a non-numeric confidence string."""

    def review(self, *, artifact, producer_identity):
        return ContentReviewResult(
            result="review_passed", confidence="high", bounded_reason="nonnumeric"
        )


class _NaNConfidenceAdapter(FakeReviewAdapter):
    """Passes with a non-finite (NaN) confidence.

    Exercises the non-finite fail-closed guard: `float("nan") < 0.5` and
    `float("nan") > 1.0` are BOTH False, so a naive range check would wrongly
    ACCEPT the review. The validator must require math.isfinite().
    """

    def review(self, *, artifact, producer_identity):
        return ContentReviewResult(
            result="review_passed",
            confidence=float("nan"),
            bounded_reason="nan confidence",
        )


class _WrongTypeResultAdapter(FakeReviewAdapter):
    """Returns a plain dict instead of a ContentReviewResult."""

    def review(self, *, artifact, producer_identity):
        return {"result": "review_passed", "confidence": 1.0}  # NOT a ContentReviewResult


class _BadReviewerAdapter(FakeReviewAdapter):
    """reviewer_identity raises when accessed (P1#3 malformed identity)."""

    @property
    def reviewer_identity(self):
        raise RuntimeError("reviewer resolution failed")


class _NoneReviewerAdapter(FakeReviewAdapter):
    """reviewer_identity is None (P1#3 malformed identity)."""

    reviewer_identity = None  # type: ignore[assignment]


class _RacyUpdateAdapter(FakeReviewAdapter):
    """During review, performs a concurrent edit of the SAME draft (P1#2).

    Simulates the submit/update race on a SEPARATE connection: a real update
    lands (and commits) while we "review", changing the artifact's
    checksum/revision after the review read. Submission must detect the change
    on its own re-read under BEGIN IMMEDIATE and fail closed (409) rather than
    persisting a review of stale content. A separate Session/connection is used
    so SQLAlchemy's identity map does not alias the objects (which would mask
    the race in a single-session test).
    """

    def __init__(self, engine, artifact_id, actor):
        self._engine = engine
        self._artifact_id = artifact_id
        self._actor = actor

    def review(self, *, artifact, producer_identity):
        from sqlmodel import Session

        with Session(self._engine) as s:
            ContentDraftService(s).update_content_draft(
                artifact_id=self._artifact_id, actor=self._actor, body="edited concurrently"
            )
        return ContentReviewResult(
            result="review_passed", confidence=1.0, bounded_reason="racy review"
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path):
    url = f"sqlite:///{(tmp_path / 'cd.db').as_posix()}"
    run_migrations(url)
    return get_engine(url)


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def project(session):
    p = Project(name="p1", objective="obj")
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _make_draft(session, project, actor=OWNER, **kw) -> Artifact:
    return ContentDraftService(session).create_content_draft(
        project_id=project.id, actor=actor, topic="topic", body="body", **kw
    )


def _submit(session, artifact, actor=OWNER):
    return ContentDraftService(session).submit_content_draft(
        artifact_id=artifact.id, actor=actor
    )


# ---------------------------------------------------------------------------
# T1-T2: creation (owner / agent)
# ---------------------------------------------------------------------------


def test_create_draft_owner(session, project):
    a = _make_draft(session, project, actor=OWNER)
    assert a.type == ArtifactType.CONTENT_DRAFT
    assert a.review_status == ArtifactReviewStatus.UNVERIFIED
    assert a.checksum.startswith("sha256:")
    assert (a.metadata_json or {}).get("producer") == "owner:owner"
    assert (a.metadata_json or {}).get("series_id") == "黎叔AI创业实验室"


def test_create_draft_agent(session, project):
    a = _make_draft(session, project, actor=AGENT_PRODUCER)
    assert a.type == ArtifactType.CONTENT_DRAFT
    assert (a.metadata_json or {}).get("producer") == "agent:producer-1"


def test_create_requires_owner_or_agent(session, project):
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).create_content_draft(
            project_id=project.id, actor=SYSTEM, topic="t", body="b"
        )
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# T3-T4: locked update + revision reset after review
# ---------------------------------------------------------------------------


def test_update_locked_when_approved(session, project):
    a = _make_draft(session, project, actor=OWNER)
    _submit(session, a, OWNER)
    svc = ContentDraftService(session)
    svc.approve_content_draft(
        artifact_id=a.id, actor=OWNER, review_checksum=a.checksum, review_revision=a.revision_count
    )
    with pytest.raises(ServiceError) as exc:
        svc.update_content_draft(artifact_id=a.id, actor=OWNER, body="new")
    assert exc.value.status_code == 409


def test_update_resets_review_status_and_bumps_revision(session, project):
    a = _make_draft(session, project, actor=OWNER)
    _submit(session, a, OWNER)  # -> REVIEW_PASSED, review recorded
    svc = ContentDraftService(session)
    before_rev = a.revision_count
    before_cs = a.checksum
    updated = svc.update_content_draft(artifact_id=a.id, actor=OWNER, body="edited")
    assert updated.review_status == ArtifactReviewStatus.UNVERIFIED
    assert updated.revision_count == before_rev + 1
    md = updated.metadata_json or {}
    assert md.get("independent_review") is None
    assert len(md.get("review_history") or []) == 1
    assert updated.checksum != before_cs


def test_update_forbidden_for_unrelated_agent(session, project):
    a = _make_draft(session, project, actor=AGENT_PRODUCER)
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).update_content_draft(
            artifact_id=a.id, actor=AGENT_OTHER, body="x"
        )
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# T5-T6: submit never APPROVED; independent review recorded
# ---------------------------------------------------------------------------


def test_submit_never_approved(session, project):
    a = _make_draft(session, project, actor=OWNER)
    submitted = _submit(session, a, OWNER)
    assert submitted.review_status != ArtifactReviewStatus.APPROVED
    assert submitted.review_status in (
        ArtifactReviewStatus.REVIEW_PASSED,
        ArtifactReviewStatus.NEEDS_REVISION,
    )


def test_submit_records_independent_review_with_reviewer_not_producer(session, project):
    a = _make_draft(session, project, actor=AGENT_PRODUCER)
    submitted = _submit(session, a, AGENT_PRODUCER)
    review = (submitted.metadata_json or {}).get("independent_review")
    assert review is not None
    assert review["reviewer"] != "agent:producer-1"  # reviewer != producer
    assert review["result"] in ("review_passed", "needs_revision")
    assert review["reviewed_checksum"] == a.checksum
    assert review["reviewed_revision"] == a.revision_count


def test_submit_rejects_spoofed_reviewer(session, project):
    class SpoofAdapter(FakeReviewAdapter):
        reviewer_identity = "agent:producer-1"  # equals producer -> must be rejected

    a = _make_draft(session, project, actor=AGENT_PRODUCER)
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).submit_content_draft(
            artifact_id=a.id, actor=AGENT_PRODUCER, adapter=SpoofAdapter()
        )
    assert exc.value.status_code == 409


def test_submit_same_revision_replay_is_idempotent(session, project):
    a = _make_draft(session, project, actor=OWNER)
    first = _submit(session, a, OWNER)
    replay = _submit(session, a, OWNER)

    assert replay.id == first.id
    assert replay.checksum == first.checksum
    audits = session.exec(
        select(AuditLog).where(
            AuditLog.resource_id == a.id,
            AuditLog.action == CONTENT_DRAFT_REVIEW_AUDIT,
        )
    ).all()
    assert len(audits) == 1


def test_review_edit_rereview_and_owner_approve_preserves_history(session, project):
    a = _make_draft(session, project, actor=OWNER)
    rev1 = ContentDraftService(session).submit_content_draft(
        artifact_id=a.id,
        actor=OWNER,
        adapter=_NeedsRevisionAdapter(),
    )
    rev1_checksum = rev1.checksum
    rev1_revision = rev1.revision_count

    rev2_unverified = ContentDraftService(session).update_content_draft(
        artifact_id=a.id,
        actor=OWNER,
        body="revised content",
    )
    assert rev2_unverified.revision_count == rev1_revision + 1
    assert rev2_unverified.checksum != rev1_checksum

    rev2 = ContentDraftService(session).submit_content_draft(
        artifact_id=a.id,
        actor=OWNER,
    )
    assert rev2.review_status == ArtifactReviewStatus.REVIEW_PASSED

    with pytest.raises(ServiceError) as stale:
        ContentDraftService(session).approve_content_draft(
            artifact_id=a.id,
            actor=OWNER,
            review_checksum=rev1_checksum,
            review_revision=rev1_revision,
        )
    assert stale.value.status_code == 409

    approved = ContentDraftService(session).approve_content_draft(
        artifact_id=a.id,
        actor=OWNER,
        review_checksum=rev2.checksum,
        review_revision=rev2.revision_count,
    )
    assert approved.review_status == ArtifactReviewStatus.APPROVED

    metadata = approved.metadata_json or {}
    history = metadata.get("review_history") or []
    assert len(history) == 1
    assert history[0]["reviewed_revision"] == rev1_revision
    assert history[0]["reviewed_checksum"] == rev1_checksum

    audits = session.exec(
        select(AuditLog)
        .where(
            AuditLog.resource_id == a.id,
            AuditLog.action == CONTENT_DRAFT_REVIEW_AUDIT,
        )
        .order_by(AuditLog.created_at)
    ).all()
    assert len(audits) == 2
    assert audits[0].idempotency_key != audits[1].idempotency_key
    assert [audit.after_snapshot["source_revision"] for audit in audits] == [
        rev1_revision,
        rev2.revision_count,
    ]
    assert all(audit.after_snapshot["source_checksum"] for audit in audits)
    assert [audit.after_snapshot["reviewed_checksum"] for audit in audits] == [
        rev1_checksum,
        rev2.checksum,
    ]


def test_submit_low_confidence_fails_closed(session, project):
    # P1#1: a low-confidence PASS must fail closed to NEEDS_REVISION (plan v3
    # §4 invariant 3). The default FakeReviewAdapter returns 1.0 and passes; a
    # low-confidence adapter must NOT produce REVIEW_PASSED.
    a = _make_draft(session, project, actor=OWNER)
    submitted = ContentDraftService(session).submit_content_draft(
        artifact_id=a.id, actor=OWNER, adapter=_LowConfidenceAdapter()
    )
    assert submitted.review_status == ArtifactReviewStatus.NEEDS_REVISION
    review = (submitted.metadata_json or {}).get("independent_review")
    assert review is not None
    assert review["result"] == "needs_revision"
    assert review["confidence"] == 0.0


@pytest.mark.parametrize(
    "adapter",
    [
        _OutOfRangeConfidenceAdapter(),
        _NonNumericConfidenceAdapter(),
        _NaNConfidenceAdapter(),
        _WrongTypeResultAdapter(),
    ],
)
def test_submit_malformed_review_fails_closed(session, project, adapter):
    # P1#1 (round 2) + NaN (round 5): ANY malformed adapter output -- out-of-range
    # confidence, non-numeric confidence, NON-FINITE (NaN) confidence, or a wrong
    # result type -- must fail closed to NEEDS_REVISION (never raise 500, never
    # REVIEW_PASSED). The full result-processing path is wrapped in try/except.
    a = _make_draft(session, project, actor=OWNER)
    submitted = ContentDraftService(session).submit_content_draft(
        artifact_id=a.id, actor=OWNER, adapter=adapter
    )
    assert submitted.review_status == ArtifactReviewStatus.NEEDS_REVISION
    review = (submitted.metadata_json or {}).get("independent_review")
    assert review is not None
    assert review["result"] == "needs_revision"


@pytest.mark.parametrize("adapter", [_BadReviewerAdapter(), _NoneReviewerAdapter()])
def test_submit_malformed_reviewer_identity_fails_closed(session, project, adapter):
    # P1#3 (round 6): a reviewer identity that raises or is non-string (None)
    # must NOT raise 500 and must NOT yield REVIEW_PASSED. The identity is
    # derived inside the fail-closed path, so any malformation degrades the
    # draft to NEEDS_REVISION with a recorded (placeholder) reviewer.
    a = _make_draft(session, project, actor=OWNER)
    submitted = ContentDraftService(session).submit_content_draft(
        artifact_id=a.id, actor=OWNER, adapter=adapter
    )
    assert submitted.review_status == ArtifactReviewStatus.NEEDS_REVISION
    review = (submitted.metadata_json or {}).get("independent_review")
    assert review is not None
    assert review["result"] == "needs_revision"
    assert review["reviewer"] in (None, "unknown")


def test_submit_checksum_consistent_with_persisted_payload(session, project):
    # P1#1 (round 6): the persisted checksum must exactly represent the
    # persisted payload. In particular, the review-binding back-reference
    # (reviewed_checksum) is excluded from the hash, so re-computing the
    # checksum over the loaded artifact reproduces the stored value and the
    # stored review record binds to it (owner approval's equality check).
    a = _make_draft(session, project, actor=OWNER)
    ContentDraftService(session).submit_content_draft(artifact_id=a.id, actor=OWNER)
    session.refresh(a)
    review = (a.metadata_json or {}).get("independent_review")
    assert review is not None
    # stored review binds to the live artifact checksum
    assert review["reviewed_checksum"] == a.checksum
    # re-computing over the persisted artifact reproduces the stored checksum
    assert a.checksum == _compute_checksum(a)


def test_submit_rejects_concurrent_update(session, engine, project):
    # P1#2 (round 6): submission is serialized under BEGIN IMMEDIATE and
    # re-checks the reviewed content. If a concurrent edit lands during the
    # (slow) review, the checksum/revision differ on re-read and submission
    # fails closed (409) instead of approving stale content.
    a = _make_draft(session, project, actor=OWNER)
    adapter = _RacyUpdateAdapter(engine, a.id, OWNER)
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).submit_content_draft(
            artifact_id=a.id, actor=OWNER, adapter=adapter
        )
    assert exc.value.status_code == 409
    # the concurrent edit won: the draft is back to UNVERIFIED, never reviewed
    session.refresh(a)
    assert a.review_status == ArtifactReviewStatus.UNVERIFIED


def test_concurrent_same_revision_review_is_idempotent(session, engine, project):
    a = _make_draft(session, project, actor=OWNER)
    artifact_id = a.id
    barrier = Barrier(2)
    outcomes: list[tuple[str, ...]] = []

    def submit():
        with Session(engine) as worker:
            worker.exec(text("PRAGMA busy_timeout=5000"))
            try:
                reviewed = ContentDraftService(worker).submit_content_draft(
                    artifact_id=artifact_id,
                    actor=OWNER,
                    adapter=_BarrierReviewAdapter(barrier),
                )
                outcomes.append(("ok", reviewed.id, reviewed.checksum))
            except Exception as exc:
                outcomes.append(("error", type(exc).__name__))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(submit) for _ in range(2)]
        for future in futures:
            future.result()

    assert len(outcomes) == 2
    assert all(outcome[0] == "ok" for outcome in outcomes), outcomes
    assert {outcome[1] for outcome in outcomes} == {artifact_id}
    assert len({outcome[2] for outcome in outcomes}) == 1

    session.expire_all()
    persisted = session.get(Artifact, artifact_id)
    assert persisted.review_status == ArtifactReviewStatus.REVIEW_PASSED
    review = (persisted.metadata_json or {}).get("independent_review")
    assert review is not None
    assert review["source_checksum"]

    audits = session.exec(
        select(AuditLog).where(
            AuditLog.resource_id == artifact_id,
            AuditLog.action == CONTENT_DRAFT_REVIEW_AUDIT,
        )
    ).all()
    assert len(audits) == 1


# ---------------------------------------------------------------------------
# T7-T9: owner approval / rejection atomic contract
# ---------------------------------------------------------------------------


def _reviewed_and_approved(session, project, actor=OWNER) -> Artifact:
    a = _make_draft(session, project, actor=actor)
    _submit(session, a, actor)
    return ContentDraftService(session).approve_content_draft(
        artifact_id=a.id,
        actor=OWNER,
        review_checksum=a.checksum,
        review_revision=a.revision_count,
    )


def test_approve_triple_creates_approval_audit_no_knowledgefact(session, project):
    a = _reviewed_and_approved(session, project)
    assert a.review_status == ArtifactReviewStatus.APPROVED
    approvals = session.exec(
        select(Approval).where(Approval.target_artifact_id == a.id)
    ).all()
    assert len(approvals) == 1
    assert approvals[0].status == ApprovalStatus.APPROVED
    assert approvals[0].action_type == CONTENT_DRAFT_APPROVE_ACTION
    assert approvals[0].risk_level.value == "L4"
    audits = session.exec(
        select(AuditLog).where(AuditLog.resource_id == a.id)
    ).all()
    assert any(au.action == "content_draft.approve" for au in audits)
    # Hard invariant: approval must NOT mint a KnowledgeFact.
    facts = session.exec(select(KnowledgeFact)).all()
    assert facts == []


def test_approve_requires_review_passed(session, project):
    a = _make_draft(session, project, actor=OWNER)  # UNVERIFIED, no review
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).approve_content_draft(
            artifact_id=a.id, actor=OWNER,
            review_checksum=a.checksum, review_revision=a.revision_count,
        )
    assert exc.value.status_code == 409


def test_approve_idempotent_duplicate_returns_409(session, project):
    a = _reviewed_and_approved(session, project)
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).approve_content_draft(
            artifact_id=a.id, actor=OWNER,
            review_checksum=a.checksum, review_revision=a.revision_count,
        )
    assert exc.value.status_code == 409


def test_approve_stale_review_returns_409(session, project):
    a = _make_draft(session, project, actor=OWNER)
    _submit(session, a, OWNER)  # review at revision 0
    old_checksum, old_revision = a.checksum, a.revision_count
    # Owner edits -> revision bumps, status resets, checksum changes.
    ContentDraftService(session).update_content_draft(artifact_id=a.id, actor=OWNER, body="v2")
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).approve_content_draft(
            artifact_id=a.id, actor=OWNER,
            review_checksum=old_checksum, review_revision=old_revision,
        )
    assert exc.value.status_code == 409


def test_approve_rejects_inconsistent_persisted_review(session, project):
    # P1#2 / T28: owner approval MUST bind to the persisted independent_review
    # record. If the recorded review's checksum/revision no longer matches the
    # artifact (e.g. tampered or out-of-sync) but the caller passes the current
    # artifact checksum, approval must still return 409 -- the persisted record
    # is the source of truth, not the caller's values.
    a = _make_draft(session, project, actor=OWNER)
    _submit(session, a, OWNER)  # REVIEW_PASSED, independent_review bound to a.checksum
    assert a.review_status == ArtifactReviewStatus.REVIEW_PASSED
    # Simulate a desynchronized persisted review (reviewed_checksum no longer
    # matches the frozen artifact checksum) while leaving status REVIEW_PASSED.
    md = dict(a.metadata_json or {})
    md["independent_review"] = dict(md["independent_review"])
    md["independent_review"]["reviewed_checksum"] = "sha256:forged"
    a.metadata_json = md
    session.add(a)
    session.commit()
    session.refresh(a)
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).approve_content_draft(
            artifact_id=a.id,
            actor=OWNER,
            review_checksum=a.checksum,  # caller passes the *real* artifact checksum
            review_revision=a.revision_count,
        )
    assert exc.value.status_code == 409


def test_approve_rejects_when_review_absent(session, project):
    # P1#2: a REVIEW_PASSED draft with no persisted independent_review (should
    # never happen via the service, but defense-in-depth) cannot be approved.
    a = _make_draft(session, project, actor=OWNER)
    _submit(session, a, OWNER)
    md = dict(a.metadata_json or {})
    md["independent_review"] = None
    a.metadata_json = md
    session.add(a)
    session.commit()
    session.refresh(a)
    a.review_status = ArtifactReviewStatus.REVIEW_PASSED
    session.add(a)
    session.commit()
    session.refresh(a)
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).approve_content_draft(
            artifact_id=a.id,
            actor=OWNER,
            review_checksum=a.checksum,
            review_revision=a.revision_count,
        )
    assert exc.value.status_code == 409


def test_reject_terminal(session, project):
    a = _make_draft(session, project, actor=OWNER)
    _submit(session, a, OWNER)
    rejected = ContentDraftService(session).reject_content_draft(
        artifact_id=a.id, actor=OWNER, reason="off-brand"
    )
    assert rejected.review_status == ArtifactReviewStatus.REJECTED
    approvals = session.exec(
        select(Approval).where(Approval.target_artifact_id == a.id)
    ).all()
    assert len(approvals) == 1
    assert approvals[0].status == ApprovalStatus.REJECTED


def test_non_owner_cannot_approve(session, project):
    a = _make_draft(session, project, actor=OWNER)
    _submit(session, a, OWNER)
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).approve_content_draft(
            artifact_id=a.id,
            actor=AGENT_PRODUCER,
            review_checksum=a.checksum,
            review_revision=a.revision_count,
        )
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# T11-T15: append-only metrics (inert AuditLog)
# ---------------------------------------------------------------------------


def test_metrics_recorded_immutable_and_draft_unchanged(session, project):
    a = _reviewed_and_approved(session, project)
    before = a.checksum
    audit = ContentDraftService(session).record_review_metrics(
        artifact_id=a.id,
        actor=OWNER,
        metrics={"exposure": 100, "consult": 5, "conversion": 1},
        idempotency_key="m1",
    )
    assert audit.action == "content.review_metric"
    assert (audit.after_snapshot or {}).get("metrics") == {
        "exposure": 100,
        "consult": 5,
        "conversion": 1,
    }
    reread = session.get(Artifact, a.id)
    assert reread.checksum == before  # draft NOT mutated
    assert reread.review_status == ArtifactReviewStatus.APPROVED


def test_metrics_only_for_approved(session, project):
    a = _make_draft(session, project, actor=OWNER)
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).record_review_metrics(
            artifact_id=a.id, actor=OWNER, metrics={}, idempotency_key="m"
        )
    assert exc.value.status_code == 409


def test_metrics_idempotent_on_duplicate_key(session, project):
    # Exact replay (same artifact + same payload) is idempotent.
    a = _reviewed_and_approved(session, project)
    first = ContentDraftService(session).record_review_metrics(
        artifact_id=a.id, actor=OWNER, metrics={"exposure": 1}, idempotency_key="dup"
    )
    second = ContentDraftService(session).record_review_metrics(
        artifact_id=a.id, actor=OWNER, metrics={"exposure": 1}, idempotency_key="dup"
    )
    assert second.id == first.id
    assert (second.after_snapshot or {}).get("metrics") == {"exposure": 1}


def test_metrics_conflicting_idempotency_key_rejected(session, project):
    # Same key with a DIFFERENT payload on the same draft is a conflicting
    # replay and must be rejected (409), never silently return the old record.
    a = _reviewed_and_approved(session, project)
    ContentDraftService(session).record_review_metrics(
        artifact_id=a.id, actor=OWNER, metrics={"exposure": 1}, idempotency_key="dup2"
    )
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).record_review_metrics(
            artifact_id=a.id, actor=OWNER, metrics={"exposure": 999}, idempotency_key="dup2"
        )
    assert exc.value.status_code == 409


def test_metrics_idempotency_key_reuse_across_drafts_rejected(session, project):
    # Reusing a key across different drafts leaks records; it must be rejected.
    a1 = _reviewed_and_approved(session, project)
    a2 = _reviewed_and_approved(session, project)
    ContentDraftService(session).record_review_metrics(
        artifact_id=a1.id, actor=OWNER, metrics={"exposure": 1}, idempotency_key="shared"
    )
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).record_review_metrics(
            artifact_id=a2.id, actor=OWNER, metrics={"exposure": 1}, idempotency_key="shared"
        )
    assert exc.value.status_code == 409


def test_metrics_correction_via_supersession(session, project):
    a = _reviewed_and_approved(session, project)
    m1 = ContentDraftService(session).record_review_metrics(
        artifact_id=a.id, actor=OWNER, metrics={"exposure": 1}, idempotency_key="c1"
    )
    m2 = ContentDraftService(session).record_review_metrics(
        artifact_id=a.id,
        actor=OWNER,
        metrics={"exposure": 2},
        idempotency_key="c2",
        supersedes_audit_id=m1.id,
    )
    assert (m2.after_snapshot or {}).get("supersedes_audit_id") == m1.id
    # original must NOT itself supersede anything (no chain/cycle).
    assert (m1.after_snapshot or {}).get("supersedes_audit_id") is None


def test_metrics_idempotency_key_reuse_with_different_supersedes_rejected(session, project):
    # Replaying the SAME idempotency key with the SAME metrics but a DIFFERENT
    # correction target (supersedes_audit_id) must be rejected (409) and never
    # silently return the record that corrects a different target.
    a = _reviewed_and_approved(session, project)
    m1 = ContentDraftService(session).record_review_metrics(
        artifact_id=a.id, actor=OWNER, metrics={"exposure": 1}, idempotency_key="k"
    )
    m2 = ContentDraftService(session).record_review_metrics(
        artifact_id=a.id, actor=OWNER, metrics={"exposure": 2},
        idempotency_key="k2", supersedes_audit_id=m1.id,
    )
    with pytest.raises(ServiceError) as exc:
        # Same key + same metrics as m1, but now targets m2 instead of None.
        ContentDraftService(session).record_review_metrics(
            artifact_id=a.id, actor=OWNER, metrics={"exposure": 1},
            idempotency_key="k", supersedes_audit_id=m2.id,
        )
    assert exc.value.status_code == 409


def test_metrics_supersession_chain_rejected(session, project):
    a = _reviewed_and_approved(session, project)
    m1 = ContentDraftService(session).record_review_metrics(
        artifact_id=a.id, actor=OWNER, metrics={"exposure": 1}, idempotency_key="s1"
    )
    m2 = ContentDraftService(session).record_review_metrics(
        artifact_id=a.id,
        actor=OWNER,
        metrics={"exposure": 2},
        idempotency_key="s2",
        supersedes_audit_id=m1.id,
    )
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).record_review_metrics(
            artifact_id=a.id,
            actor=OWNER,
            metrics={"exposure": 3},
            idempotency_key="s3",
            supersedes_audit_id=m2.id,
        )
    assert exc.value.status_code == 409


def test_metrics_supersession_branch_rejected(session, project):
    # P1#3: two corrections superseding the SAME target fork the history into a
    # branch. Once m1 is superseded by m2, a third record superseding m1 again
    # must be rejected (409) so corrections stay a single linear chain.
    a = _reviewed_and_approved(session, project)
    m1 = ContentDraftService(session).record_review_metrics(
        artifact_id=a.id, actor=OWNER, metrics={"exposure": 1}, idempotency_key="b1"
    )
    ContentDraftService(session).record_review_metrics(
        artifact_id=a.id,
        actor=OWNER,
        metrics={"exposure": 2},
        idempotency_key="b2",
        supersedes_audit_id=m1.id,
    )
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).record_review_metrics(
            artifact_id=a.id,
            actor=OWNER,
            metrics={"exposure": 3},
            idempotency_key="b3",
            supersedes_audit_id=m1.id,  # m1 already superseded by b2 -> branch
        )
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# T16-T19: per-Artifact, same-project authorization
# ---------------------------------------------------------------------------


def test_get_allowed_for_related_producer(session, project):
    a = _make_draft(session, project, actor=AGENT_PRODUCER)
    got = ContentDraftService(session).get_content_draft(
        artifact_id=a.id, actor=AGENT_PRODUCER
    )
    assert got.id == a.id


def test_get_forbidden_for_unrelated_agent(session, project):
    a = _make_draft(session, project, actor=AGENT_PRODUCER)
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).get_content_draft(artifact_id=a.id, actor=AGENT_OTHER)
    assert exc.value.status_code == 403


def test_list_forbidden_for_unrelated_agent(session, project):
    _make_draft(session, project, actor=AGENT_PRODUCER)
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).list_content_drafts(
            project_id=project.id, actor=AGENT_OTHER
        )
    assert exc.value.status_code == 403


def test_list_owner_sees_all(session, project):
    _make_draft(session, project, actor=AGENT_PRODUCER)
    _make_draft(session, project, actor=AGENT_OTHER)
    rows = ContentDraftService(session).list_content_drafts(
        project_id=project.id, actor=OWNER
    )
    assert len(rows) == 2


def test_list_related_agent_sees_only_own(session, project):
    _make_draft(session, project, actor=AGENT_PRODUCER)
    _make_draft(session, project, actor=AGENT_OTHER)
    rows = ContentDraftService(session).list_content_drafts(
        project_id=project.id, actor=AGENT_PRODUCER
    )
    assert len(rows) == 1
    assert (rows[0].metadata_json or {}).get("producer") == "agent:producer-1"


def test_get_allowed_for_assigned_reviewer(session, project):
    # P1#4: the agent identity that performed the independent review (not the
    # producer) must be able to read the draft it reviewed. The reviewer lives
    # in metadata_json.independent_review.reviewer, never a top-level key.
    a = _make_draft(session, project, actor=AGENT_PRODUCER)
    _submit(session, a, AGENT_PRODUCER)  # reviewer = FakeReviewAdapter identity
    got = ContentDraftService(session).get_content_draft(
        artifact_id=a.id, actor=AGENT_REVIEWER
    )
    assert got.id == a.id


def test_list_includes_reviewer_related(session, project):
    # P1#4: a reviewer agent lists exactly the drafts it reviewed.
    a = _make_draft(session, project, actor=AGENT_PRODUCER)
    _submit(session, a, AGENT_PRODUCER)
    rows = ContentDraftService(session).list_content_drafts(
        project_id=project.id, actor=AGENT_REVIEWER
    )
    assert len(rows) == 1
    assert rows[0].id == a.id


def test_get_forbidden_for_plain_unrelated_reviewer(session, project):
    # An unrelated agent that is neither producer nor reviewer is still 403.
    a = _make_draft(session, project, actor=AGENT_PRODUCER)
    _submit(session, a, AGENT_PRODUCER)
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).get_content_draft(
            artifact_id=a.id, actor=AGENT_OTHER
        )
    assert exc.value.status_code == 403


def test_unauthenticated_actor_rejected(session, project):
    a = _make_draft(session, project, actor=OWNER)
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).get_content_draft(artifact_id=a.id, actor=SYSTEM)
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# T20-T21: SQLite BEGIN IMMEDIATE concurrency
# ---------------------------------------------------------------------------


def test_concurrent_approve_converges_to_one(session, project):
    a = _make_draft(session, project, actor=OWNER)
    _submit(session, a, OWNER)
    cs = a.checksum
    rev = a.revision_count

    barrier = Barrier(2)
    outcomes: list[str] = []

    def attempt():
        with Session(session.get_bind()) as s:
            s.exec(text("PRAGMA busy_timeout=5000"))
            barrier.wait()
            try:
                ContentDraftService(s).approve_content_draft(
                    artifact_id=a.id, actor=OWNER, review_checksum=cs, review_revision=rev
                )
                outcomes.append("ok")
            except (ServiceError, OperationalError, IntegrityError):
                outcomes.append("conflict")

    with ThreadPoolExecutor(max_workers=2) as ex:
        ex.submit(attempt)
        ex.submit(attempt)
    approvals = session.exec(
        select(Approval).where(Approval.target_artifact_id == a.id)
    ).all()
    assert len(approvals) == 1
    session.refresh(a)  # main session cached `a` before the threads committed
    assert session.get(Artifact, a.id).review_status == ArtifactReviewStatus.APPROVED
    # Exactly one succeeded; the other lost the race (409/lock).
    assert outcomes.count("ok") == 1


def test_concurrent_approve_reject_one_wins(session, project):
    a = _make_draft(session, project, actor=OWNER)
    _submit(session, a, OWNER)
    cs = a.checksum
    rev = a.revision_count
    barrier = Barrier(2)
    results: list[str] = []

    def approve():
        with Session(session.get_bind()) as s:
            s.exec(text("PRAGMA busy_timeout=5000"))
            barrier.wait()
            try:
                ContentDraftService(s).approve_content_draft(
                    artifact_id=a.id, actor=OWNER, review_checksum=cs, review_revision=rev
                )
                results.append("approve")
            except (ServiceError, OperationalError, IntegrityError):
                results.append("approve_conflict")

    def reject():
        with Session(session.get_bind()) as s:
            s.exec(text("PRAGMA busy_timeout=5000"))
            barrier.wait()
            try:
                ContentDraftService(s).reject_content_draft(
                    artifact_id=a.id, actor=OWNER, reason="x"
                )
                results.append("reject")
            except (ServiceError, OperationalError, IntegrityError):
                results.append("reject_conflict")

    with ThreadPoolExecutor(max_workers=2) as ex:
        ex.submit(approve)
        ex.submit(reject)
    approvals = session.exec(
        select(Approval).where(Approval.target_artifact_id == a.id)
    ).all()
    assert len(approvals) == 1  # exactly one terminal decision
    assert results.count("approve") + results.count("reject") == 1


def test_approve_rolls_back_on_commit_failure(session, project):
    # T21: a mid-transaction commit failure must roll back fully -- no residual
    # Approval, no status flip, and no approve AuditLog row are persisted.
    a = _make_draft(session, project, actor=OWNER)
    _submit(session, a, OWNER)
    svc = ContentDraftService(session)
    original_commit = session.commit

    def failing_commit():
        raise RuntimeError("simulated commit failure")

    session.commit = failing_commit
    with pytest.raises(RuntimeError):
        svc.approve_content_draft(
            artifact_id=a.id,
            actor=OWNER,
            review_checksum=a.checksum,
            review_revision=a.revision_count,
        )
    session.commit = original_commit
    # Full rollback: nothing from the failed approve survived.
    approvals = session.exec(
        select(Approval).where(Approval.target_artifact_id == a.id)
    ).all()
    assert approvals == []
    reread = session.get(Artifact, a.id)
    assert reread.review_status == ArtifactReviewStatus.REVIEW_PASSED
    audits = session.exec(select(AuditLog).where(AuditLog.resource_id == a.id)).all()
    assert all(au.action != "content_draft.approve" for au in audits)


def test_concurrent_update_vs_approve_one_terminal(session, project):
    # T27: an edit and an approve racing (serialized via BEGIN IMMEDIATE) yield
    # exactly one terminal decision and NEVER a partial state.
    a = _make_draft(session, project, actor=OWNER)
    _submit(session, a, OWNER)
    cs = a.checksum
    rev = a.revision_count
    barrier = Barrier(2)
    results: list[str] = []

    def do_update():
        with Session(session.get_bind()) as s:
            s.exec(text("PRAGMA busy_timeout=5000"))
            barrier.wait()
            try:
                ContentDraftService(s).update_content_draft(
                    artifact_id=a.id, actor=OWNER, body="edited"
                )
                results.append("update")
            except (ServiceError, OperationalError, IntegrityError):
                results.append("update_conflict")

    def do_approve():
        with Session(session.get_bind()) as s:
            s.exec(text("PRAGMA busy_timeout=5000"))
            barrier.wait()
            try:
                ContentDraftService(s).approve_content_draft(
                    artifact_id=a.id,
                    actor=OWNER,
                    review_checksum=cs,
                    review_revision=rev,
                )
                results.append("approve")
            except (ServiceError, OperationalError, IntegrityError):
                results.append("approve_conflict")

    with ThreadPoolExecutor(max_workers=2) as ex:
        ex.submit(do_update)
        ex.submit(do_approve)
    # The main session cached `a` before the threads committed; reload it.
    session.refresh(a)
    # Exactly one terminal decision; the other lost the race (409 / lock).
    assert results.count("update") + results.count("approve") == 1
    approvals = session.exec(
        select(Approval).where(Approval.target_artifact_id == a.id)
    ).all()
    if results.count("approve") == 1:
        assert a.review_status == ArtifactReviewStatus.APPROVED
        assert len(approvals) == 1
    else:
        assert a.review_status == ArtifactReviewStatus.UNVERIFIED
        assert approvals == []


# ---------------------------------------------------------------------------
# T22-T24: no knowledge injection / no side effects
# ---------------------------------------------------------------------------


def test_approval_creates_no_knowledge_fact(session, project):
    _reviewed_and_approved(session, project)
    facts = session.exec(select(KnowledgeFact)).all()
    assert facts == []
    # No publish/execute side effect: the only Approval is the content approve.
    approvals = session.exec(select(Approval)).all()
    assert all(ap.action_type == CONTENT_DRAFT_APPROVE_ACTION for ap in approvals)


def test_no_real_model_call_default_adapter(session, project):
    adapter = FakeReviewAdapter()
    a = _make_draft(session, project, actor=OWNER)
    submitted = ContentDraftService(session).submit_content_draft(
        artifact_id=a.id, actor=OWNER, adapter=adapter
    )
    review = (submitted.metadata_json or {}).get("independent_review")
    assert review is not None
    assert review["reviewer"] == adapter.reviewer_identity  # deterministic fake


def test_metrics_insert_creates_no_event_task_delegatedrun(session, project):
    # T29: inserting a content_review_metric AuditLog must create NO Event /
    # Task / DelegatedRun side effects -- the metrics primitive is inert
    # (plan v3 §2b). Exactly one metric audit_log row appears.
    a = _reviewed_and_approved(session, project)
    ev_before = session.exec(select(Event)).all()
    task_before = session.exec(select(Task)).all()
    run_before = session.exec(select(DelegatedRun)).all()
    audit = ContentDraftService(session).record_review_metrics(
        artifact_id=a.id, actor=OWNER, metrics={"exposure": 1}, idempotency_key="t29"
    )
    assert audit.action == "content.review_metric"
    # No orchestration / retry / outbox side effects.
    assert session.exec(select(Event)).all() == ev_before
    assert session.exec(select(Task)).all() == task_before
    assert session.exec(select(DelegatedRun)).all() == run_before
    # Exactly one new metric audit_log row for this artifact.
    metric_audits = [
        au
        for au in session.exec(select(AuditLog).where(AuditLog.resource_id == a.id)).all()
        if au.action == "content.review_metric"
    ]
    assert len(metric_audits) == 1


# ---------------------------------------------------------------------------
# T25-T29: zero-migration proof
# ---------------------------------------------------------------------------


def test_single_alembic_head_unchanged():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    cfg = Config(root / "alembic.ini")
    cfg.set_main_option("script_location", str(root / "alembic"))
    assert ScriptDirectory.from_config(cfg).get_current_head() == HEAD


def test_fresh_db_accepts_content_draft_and_single_head(session, project):
    # DB was migrated by the session fixture; assert the head is unchanged.
    with session.get_bind().connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == HEAD
    a = _make_draft(session, project, actor=OWNER)
    assert a.type == ArtifactType.CONTENT_DRAFT


def test_content_draft_round_trip_no_enum_rejection(session, project):
    a = _make_draft(session, project, actor=OWNER)
    # Raw SQL proves the VARCHAR type column accepts the new enum value with no
    # CHECK / trigger rejecting it (storage is the enum name; it round-trips).
    with session.get_bind().connect() as conn:
        stored = conn.execute(
            text("SELECT type FROM artifact WHERE id=:id"), {"id": a.id}
        ).scalar()
    # SQLAlchemy's Enum stores/reads the member NAME; subscript lookup is the
    # correct inverse and proves the new value round-trips without rejection.
    assert ArtifactType[stored] is ArtifactType.CONTENT_DRAFT
    reread = session.get(Artifact, a.id)
    assert reread.type == ArtifactType.CONTENT_DRAFT
    assert ArtifactType("content_draft") is ArtifactType.CONTENT_DRAFT


def test_artifact_revision_count_field_exists(session, project):
    a = _make_draft(session, project, actor=OWNER)
    assert isinstance(a.revision_count, int)
    assert a.revision_count == 0


# ---------------------------------------------------------------------------
# T30-T34: HTTP surface (deterministic, with dependency overrides)
# ---------------------------------------------------------------------------


def test_http_create_and_approve_flow(authenticated_client):
    from aios.content_draft import authenticate_owner_or_agent

    # create/update/submit use owner-or-agent auth; the conftest already
    # overrides authenticate_owner, so we add the OR-agent override too.
    authenticated_client.app.dependency_overrides[authenticate_owner_or_agent] = (
        lambda: OWNER
    )
    pr = authenticated_client.post("/projects", json={"name": "p", "objective": "o"})
    assert pr.status_code == 201, pr.text
    pid = pr.json()["id"]
    r = authenticated_client.post(
        "/content-drafts", json={"project_id": pid, "topic": "t", "body": "b"}
    )
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    r = authenticated_client.post(f"/content-drafts/{aid}/submit")
    assert r.status_code == 200, r.text
    assert r.json()["review_status"] in ("review_passed", "needs_revision")
    cs = r.json()["checksum"]
    rev = r.json()["revision_count"]
    r = authenticated_client.post(
        f"/content-drafts/{aid}/approve",
        json={"review_checksum": cs, "review_revision": rev},
    )
    assert r.status_code == 200, r.text
    assert r.json()["review_status"] == "approved"


def test_http_unrelated_agent_get_forbidden_via_service(session, project):
    # Service-layer expression of the HTTP contract: an unrelated agent cannot
    # read a draft it did not produce/review.
    a = _make_draft(session, project, actor=AGENT_PRODUCER)
    with pytest.raises(ServiceError) as exc:
        ContentDraftService(session).get_content_draft(artifact_id=a.id, actor=AGENT_OTHER)
    assert exc.value.status_code == 403


def test_http_metrics_on_approved(session, project):
    a = _reviewed_and_approved(session, project)
    audit = ContentDraftService(session).record_review_metrics(
        artifact_id=a.id,
        actor=OWNER,
        metrics={"exposure": 7},
        idempotency_key="http-m",
    )
    assert audit.action == "content.review_metric"


def _http_create_draft_as_owner(client, topic="t", body="b"):
    """Create a project + content draft acting as the trusted owner."""
    from aios.content_draft import authenticate_owner_or_agent

    client.app.dependency_overrides[authenticate_owner_or_agent] = lambda: OWNER
    pr = client.post("/projects", json={"name": "p", "objective": "o"})
    assert pr.status_code == 201, pr.text
    pid = pr.json()["id"]
    r = client.post(
        "/content-drafts", json={"project_id": pid, "topic": topic, "body": body}
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_http_unrelated_agent_get_forbidden(authenticated_client):
    # T32: an unrelated same-project agent is rejected (403) on
    # GET /content-drafts/{id} and the body does not leak draft content.
    aid = _http_create_draft_as_owner(authenticated_client)
    from aios.content_draft import authenticate_owner_or_agent

    authenticated_client.app.dependency_overrides[authenticate_owner_or_agent] = (
        lambda: AGENT_OTHER
    )
    r = authenticated_client.get(f"/content-drafts/{aid}")
    assert r.status_code == 403, r.text
    assert "uri" not in r.json() and "metadata" not in r.json()


def test_http_unrelated_agent_patch_forbidden(authenticated_client):
    # T33: an unrelated same-project agent is rejected (403) on
    # PATCH /content-drafts/{id}.
    aid = _http_create_draft_as_owner(authenticated_client)
    from aios.content_draft import authenticate_owner_or_agent

    authenticated_client.app.dependency_overrides[authenticate_owner_or_agent] = (
        lambda: AGENT_OTHER
    )
    r = authenticated_client.patch(f"/content-drafts/{aid}", json={"body": "x"})
    assert r.status_code == 403, r.text


def test_http_unrelated_agent_submit_forbidden(authenticated_client):
    # T34: an unrelated same-project agent is rejected (403) on
    # POST /content-drafts/{id}/submit.
    aid = _http_create_draft_as_owner(authenticated_client)
    from aios.content_draft import authenticate_owner_or_agent

    authenticated_client.app.dependency_overrides[authenticate_owner_or_agent] = (
        lambda: AGENT_OTHER
    )
    r = authenticated_client.post(f"/content-drafts/{aid}/submit")
    assert r.status_code == 403, r.text
