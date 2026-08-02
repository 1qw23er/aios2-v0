# Content Re-review Idempotency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Permit a new independent content review after an edit while keeping an exact same-revision review replay idempotent and preserving AuditLog uniqueness.

**Architecture:** Keep the existing ContentDraftService, Artifact JSON review record, owner approval binding, BEGIN IMMEDIATE serialization, and immutable AuditLog. Define one logical review identity as the tuple `(artifact_id, source_revision, source_checksum)`, where `source_checksum` is the exact draft checksum observed before the independent review record is attached. Persist that source checksum in the active review record and review audit, while retaining the existing final `reviewed_checksum` as the owner-approval binding.

**Tech Stack:** Python 3.12, SQLModel/SQLAlchemy, SQLite BEGIN IMMEDIATE transactions, pytest, Ruff.

**Issue:** GitHub #119

---

## Contract decisions

1. AuditLog uniqueness remains unchanged.
2. The review audit key becomes:

   ```text
   audit:content_draft:review:<artifact_id>:<source_revision>:<source_checksum>
   ```

3. `source_checksum` is the checksum of the exact UNVERIFIED draft snapshot
   read before the review adapter runs.
4. `reviewed_checksum` remains the final persisted Artifact checksum after the
   independent review record is attached. Owner approval continues to bind to
   `reviewed_checksum + reviewed_revision`.
5. A sequential replay of an already reviewed current revision returns the
   existing Artifact without calling the adapter or writing another audit.
6. A concurrent replay that began from the same source revision/checksum
   returns the winning persisted review after the fenced re-read.
7. A different source checksum or revision is not a replay. After a normal edit,
   the new revision may create exactly one new review and one new audit.
8. APPROVED and REJECTED drafts remain terminal and are never treated as review
   replays.
9. No database migration is required because `source_checksum` is additive JSON
   provenance inside `Artifact.metadata_json` and audit snapshots.

## File map

- Modify: `tests/test_content_draft.py`
  - replace the old duplicate-submit 409 expectation with exact replay
    idempotency;
  - add the complete revision 1 -> revision 2 regression;
  - add concurrent same-revision replay coverage.
- Modify: `src/aios/content_draft.py`
  - add bounded active-review replay matching;
  - add source revision/checksum provenance;
  - derive the revision-specific AuditLog idempotency key;
  - return the winning review for concurrent duplicate submissions.
- No model, migration, API schema, external adapter, or publication changes.

### Task 1: Add sequential replay and revision-loop regressions

**Files:**
- Modify: `tests/test_content_draft.py`

- [ ] **Step 1: Replace the legacy duplicate-submit rejection test**

Replace `test_submit_requires_unverified` with a test that:

```python
def test_submit_same_revision_replay_is_idempotent(session, project):
    artifact = _make_draft(session, project, actor=OWNER)
    first = _submit(session, artifact, OWNER)
    replay = _submit(session, artifact, OWNER)

    assert replay.id == first.id
    assert replay.checksum == first.checksum
    audits = session.exec(
        select(AuditLog).where(
            AuditLog.resource_id == artifact.id,
            AuditLog.action == CONTENT_DRAFT_REVIEW_AUDIT,
        )
    ).all()
    assert len(audits) == 1
```

- [ ] **Step 2: Add the full two-revision regression**

Add one test that:

```python
def test_review_edit_rereview_and_owner_approve_preserves_history(session, project):
    artifact = _make_draft(session, project, actor=OWNER)
    rev1 = ContentDraftService(session).submit_content_draft(
        artifact_id=artifact.id,
        actor=OWNER,
        adapter=_NeedsRevisionAdapter(),
    )
    rev1_checksum = rev1.checksum
    rev1_revision = rev1.revision_count

    rev2_unverified = ContentDraftService(session).update_content_draft(
        artifact_id=artifact.id,
        actor=OWNER,
        body="revised content",
    )
    assert rev2_unverified.revision_count == rev1_revision + 1

    rev2 = ContentDraftService(session).submit_content_draft(
        artifact_id=artifact.id,
        actor=OWNER,
    )
    assert rev2.review_status == ArtifactReviewStatus.REVIEW_PASSED

    with pytest.raises(ServiceError) as stale:
        ContentDraftService(session).approve_content_draft(
            artifact_id=artifact.id,
            actor=OWNER,
            review_checksum=rev1_checksum,
            review_revision=rev1_revision,
        )
    assert stale.value.status_code == 409

    approved = ContentDraftService(session).approve_content_draft(
        artifact_id=artifact.id,
        actor=OWNER,
        review_checksum=rev2.checksum,
        review_revision=rev2.revision_count,
    )
    assert approved.review_status == ArtifactReviewStatus.APPROVED
```

The test must additionally assert:

- `review_history` contains the revision 1 review;
- two independent-review AuditLog rows exist;
- the two audit keys differ;
- each audit snapshot records its source revision and source checksum.

- [ ] **Step 3: Run the two tests and confirm RED**

Run:

```powershell
python -m pytest -q `
  tests/test_content_draft.py::test_submit_same_revision_replay_is_idempotent `
  tests/test_content_draft.py::test_review_edit_rereview_and_owner_approve_preserves_history
```

Expected:

- sequential replay fails with the existing 409 contract;
- revision 2 review fails with the unique AuditLog idempotency key collision.

### Task 2: Add concurrent exact-replay regression

**Files:**
- Modify: `tests/test_content_draft.py`

- [ ] **Step 1: Add a barrier-backed deterministic adapter**

Add a test-only adapter:

```python
class _BarrierReviewAdapter(FakeReviewAdapter):
    def __init__(self, barrier: Barrier):
        self._barrier = barrier

    def review(self, *, artifact, producer_identity):
        self._barrier.wait()
        return super().review(
            artifact=artifact,
            producer_identity=producer_identity,
        )
```

- [ ] **Step 2: Add concurrent replay test**

Create one UNVERIFIED Artifact, start two sessions with the barrier adapter, and
assert:

```python
assert outcomes == ["ok", "ok"]
assert len(review_audits) == 1
assert persisted.review_status == ArtifactReviewStatus.REVIEW_PASSED
assert persisted.metadata_json["independent_review"]["source_checksum"]
```

Both callers must receive the same Artifact ID/checksum. OperationalError,
IntegrityError, or ServiceError is a test failure because an exact concurrent
replay is required to be idempotent.

- [ ] **Step 3: Run the concurrent test and confirm RED**

Run:

```powershell
python -m pytest -q `
  tests/test_content_draft.py::test_concurrent_same_revision_review_is_idempotent
```

Expected: one caller succeeds and the other currently conflicts or fails to
return the winning review.

### Task 3: Implement the minimal review identity contract

**Files:**
- Modify: `src/aios/content_draft.py`

- [ ] **Step 1: Add a replay matcher**

Add a private helper near the review-record helpers:

```python
def _matches_current_review(
    artifact: Artifact,
    *,
    source_revision: int | None = None,
    source_checksum: str | None = None,
) -> bool:
    if artifact.review_status not in (
        ArtifactReviewStatus.REVIEW_PASSED,
        ArtifactReviewStatus.NEEDS_REVISION,
    ):
        return False
    review = _get_independent_review(artifact)
    if review is None:
        return False
    if review.get("reviewed_revision") != artifact.revision_count:
        return False
    if review.get("reviewed_checksum") != artifact.checksum:
        return False
    if source_revision is not None and review.get("reviewed_revision") != source_revision:
        return False
    if source_checksum is not None and review.get("source_checksum") != source_checksum:
        return False
    return True
```

- [ ] **Step 2: Make sequential replay a no-op**

Before rejecting a non-UNVERIFIED artifact in `submit_content_draft`, return the
current Artifact when `_matches_current_review(artifact)` is true. Do not invoke
the adapter or append an audit.

- [ ] **Step 3: Persist source identity and use it in the audit key**

Capture the original values before review:

```python
source_checksum = artifact.checksum
source_revision = artifact.revision_count
```

Persist them in `independent_review`:

```python
"source_checksum": source_checksum,
"reviewed_revision": source_revision,
```

After computing the final persisted checksum, append the audit with:

```python
after={
    "review_status": fresh.review_status.value,
    "reviewer": review_record["reviewer"],
    "result": result.result,
    "source_revision": source_revision,
    "source_checksum": source_checksum,
    "reviewed_checksum": fresh.checksum,
},
idempotency_key=(
    f"audit:content_draft:review:{fresh.id}:"
    f"{source_revision}:{source_checksum}"
),
```

- [ ] **Step 4: Make concurrent replay return the winner**

Inside the BEGIN IMMEDIATE re-read, if `fresh.review_status` is no longer
UNVERIFIED, test it with both captured source fields. If it matches, roll back
the read transaction and return the freshly loaded winning Artifact. Otherwise
retain the existing 409.

- [ ] **Step 5: Run the three targeted tests**

Run:

```powershell
python -m pytest -q `
  tests/test_content_draft.py::test_submit_same_revision_replay_is_idempotent `
  tests/test_content_draft.py::test_review_edit_rereview_and_owner_approve_preserves_history `
  tests/test_content_draft.py::test_concurrent_same_revision_review_is_idempotent
```

Expected: `3 passed`.

### Task 4: Regression verification and Content Synthetic UAT

**Files:**
- No production file additions.
- UAT output remains outside the repository.

- [ ] **Step 1: Run the full content module**

Run:

```powershell
python -m pytest -q tests/test_content_draft.py
```

Expected: all content tests pass, including authorization, stale binding,
approval, concurrency, rollback, and no-auto-knowledge tests.

- [ ] **Step 2: Run full quality gates**

Run:

```powershell
python -m pytest -q
python -m ruff check src tests alembic
```

Expected: both commands exit 0.

- [ ] **Step 3: Rerun only the Content portion of Synthetic Human UAT**

Use a fresh disposable SQLite database and the same realistic sequence:

```text
revision 1 -> independent review needs revision
-> edit to revision 2 -> independent review passes
-> stale revision 1 approval rejected
-> revision 2 owner approval succeeds
```

Expected:

- journey completes;
- two independent-review audit rows remain;
- no automatic KnowledgeFact;
- no external publication;
- no paid LLM call.

- [ ] **Step 4: Verify worktree scope**

Run:

```powershell
git diff --check
git status --short
git diff --stat github/main...HEAD
```

Expected: only the plan, `src/aios/content_draft.py`, and
`tests/test_content_draft.py` are changed.

### Task 5: Commit, push, PR, and review gates

**Files:**
- Commit only the three scoped files.

- [ ] **Step 1: Commit the plan separately**

```powershell
git add docs/superpowers/plans/2026-08-02-content-rereview-idempotency.md
git commit -m "docs: plan content rereview idempotency fix"
```

- [ ] **Step 2: Commit the TDD implementation**

```powershell
git add src/aios/content_draft.py tests/test_content_draft.py
git commit -m "fix(content): bind review audit identity to revision"
```

- [ ] **Step 3: Push and open one PR**

Push `fix/119-content-rereview-idempotency` and open a PR against `main`.
The PR must reference #119 and include RED evidence, test/Ruff results, Content
Synthetic UAT results, backward compatibility, and no-migration confirmation.

- [ ] **Step 4: Request Codex review and wait for exact-head CI**

Do not set the owner merge gate until:

- Codex review is APPROVE on the exact head;
- all GitHub Actions checks are green on the exact head.

- [ ] **Step 5: Stop at owner merge gate**

Set repository handoff labels according to current conventions and report:

```text
P0 PR #<number> is Codex-approved and exact-head CI-green; owner merge approval required.
```

Do not merge automatically.
