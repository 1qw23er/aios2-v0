# Alpha-3 Deterministic Knowledge Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic, human-reviewed KnowledgeFact ingestion and scoped reuse while preserving Alpha-2 behavior.

**Architecture:** Three new immutable/lifecycle-controlled SQLModel records separate candidate submission, terminal human review, and reusable facts. `KnowledgeService` owns validation, atomic promotion/supersession/deactivation, idempotency, and audit; `ContextService` only selects approved company/same-project fact heads and projects provenance into immutable TaskContext snapshots.

**Tech Stack:** Python 3.12, SQLModel/SQLAlchemy, SQLite, Alembic, pytest, Ruff.

---

### Task 1: Persist the approved domain model and lifecycle constraints

**Files:**
- Modify: `src/aios/models.py`
- Create: `tests/test_knowledge_models.py`

- [ ] **Step 1: Write failing model persistence and constraint tests**

Create tests that import `KnowledgeCandidate`, `KnowledgeReviewDecision`, and `KnowledgeFact`, persist exact-scope records, and assert duplicate terminal reviews, lineage versions, provenance IDs, and predecessor IDs fail with `IntegrityError`.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_knowledge_models.py -q`
Expected: collection fails because Alpha-3 models do not exist.

- [ ] **Step 3: Add enums and SQLModel tables**

Define candidate, review-decision, and fact status enums; add the three approved models with unique constraints for terminal review, lineage version, source candidate, review decision, and direct predecessor.

```python
class KnowledgeFactStatus(StrEnum):
    APPROVED = "approved"
    SUPERSEDED = "superseded"
    INACTIVE = "inactive"
```

- [ ] **Step 4: Verify GREEN and commit**

Run: `python -m pytest tests/test_knowledge_models.py -q`
Expected: all model tests pass.

Commit: `feat: add Alpha-3 knowledge models`

### Task 2: Add migration-level head, lineage, and immutability enforcement

**Files:**
- Create: `alembic/versions/20260716_0005_knowledge_engine.py`
- Modify: `tests/test_knowledge_models.py`

- [ ] **Step 1: Write failing raw-SQL database invariant tests**

Test that a first fact with version other than 1, a disconnected later version, scope mismatch, skipped/non-head predecessor, parallel successor, immutable-field UPDATE, invalid lifecycle transition, and two approved heads are rejected without `KnowledgeService`.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_knowledge_models.py -q`
Expected: database invariant tests fail because migration 0005 is absent.

- [ ] **Step 3: Implement migration 0005**

Create all tables/indexes and SQLite triggers. Add a partial expression index equivalent to:

```sql
CREATE UNIQUE INDEX uq_knowledge_fact_approved_head
ON knowledge_fact(series_id, COALESCE(project_id, ''))
WHERE status = 'APPROVED';
```

Triggers enforce first-version/no-predecessor rules, current-head replacement, same series/scope, strict version growth, immutable identity/provenance, and permitted lifecycle transitions. Downgrade drops Alpha-3 objects only.

- [ ] **Step 4: Verify GREEN and migration round trip**

Run model tests and an Alembic `0004 -> 0005 -> 0004 -> 0005` round trip against temporary SQLite.
Expected: tests and all transitions pass.

Commit: `feat: enforce knowledge lineage in database`

### Task 3: Implement candidate submission and terminal review with TDD

**Files:**
- Create: `src/aios/knowledge_service.py`
- Create: `tests/test_knowledge_service.py`

- [ ] **Step 1: Write failing submission tests**

Cover approved Artifact submission, missing/unapproved Artifact, empty fields, exact nullable scope equality, other-project scope, and project-to-company widening rejection.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_knowledge_service.py -q`
Expected: import/API failure because `KnowledgeService` does not exist.

- [ ] **Step 3: Implement minimal `submit_candidate`**

```python
if artifact is None or artifact.review_status != ArtifactReviewStatus.APPROVED:
    raise ServiceError(422, "Source Artifact must be approved")
if project_id != artifact.project_id:
    raise ServiceError(422, "Candidate scope must exactly match Artifact scope")
```

Persist candidate plus sanitized `knowledge.candidate.created` audit atomically.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/test_knowledge_service.py -q`
Expected: submission tests pass.

- [ ] **Step 5: Write failing review/idempotency tests**

Cover rejection without fact, approval with complete provenance, exact retry returning existing records, conflicting retry, first fact version 1, later explicit current-head replacement, deactivation, and audit rollback.

- [ ] **Step 6: Implement minimal `review_candidate` and `deactivate_fact`**

Use one transaction for candidate transition, review, fact creation, predecessor transition, and AuditLog. Catch concurrency constraint failures, roll back, and translate them to `ServiceError(409, ...)`.

- [ ] **Step 7: Verify GREEN and commit**

Run: `python -m pytest tests/test_knowledge_service.py -q`
Expected: all service tests pass.

Commit: `feat: add deterministic knowledge review service`

### Task 4: Prove atomic single-head behavior under concurrency

**Files:**
- Modify: `tests/test_knowledge_service.py`
- Modify: `src/aios/knowledge_service.py`

- [ ] **Step 1: Write a failing concurrent replacement test**

Create two approved candidates and two independent Sessions against one file-backed SQLite database. Release worker threads simultaneously to replace the same current head. Assert one succeeds, one conflicts, and a fresh Session sees exactly one approved head and one direct successor.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_knowledge_service.py::test_concurrent_replacements_leave_one_approved_head -q`
Expected: failure if database errors leak or partial transitions remain.

- [ ] **Step 3: Add minimal concurrency error translation**

Normalize `IntegrityError` and SQLite lock/serialization conflicts from replacement into a stable 409 after rollback; do not retry against a changed head.

- [ ] **Step 4: Verify GREEN and commit**

Run the concurrency test repeatedly, then the full knowledge service test file.
Expected: every run leaves one approved head.

Commit: `test: prove knowledge head concurrency safety`

### Task 5: Include active knowledge in deterministic TaskContext

**Files:**
- Modify: `src/aios/context_service.py`
- Create: `tests/test_knowledge_context.py`

- [ ] **Step 1: Write failing context selection tests**

Cover company plus same-project inclusion, other-project exclusion, superseded/inactive exclusion, deterministic order/hash, hash changes, and Fact/Candidate/Decision/Artifact source references.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_knowledge_context.py -q`
Expected: approved KnowledgeFacts are missing from TaskContext.

- [ ] **Step 3: Implement `_knowledge_facts` projection**

Select approved facts where scope is null or the task project, sort by scope/series/version/id, append `fact_kind="knowledge_fact"` projections, and add four provenance references. Keep existing ReviewedFact dictionaries unchanged.

- [ ] **Step 4: Verify GREEN and Alpha-2 compatibility**

Run: `python -m pytest tests/test_knowledge_context.py tests/test_context_service.py tests/test_context_external.py -q`
Expected: all new and existing context/export tests pass.

Commit: `feat: add reviewed knowledge to task context`

### Task 6: Full verification and delivery cleanup

**Files:**
- Modify only files required by in-scope failures.

- [ ] **Step 1: Run focused and full tests**

Run: `python -m pytest -q`
Expected: complete suite passes.

- [ ] **Step 2: Run Ruff**

Run: `python -m ruff check src tests alembic`
Expected: `All checks passed!`

- [ ] **Step 3: Verify migration round trip on a fresh database**

Upgrade to 0004, upgrade to 0005, downgrade to 0004, and upgrade to head; inspect current revision after every transition.

- [ ] **Step 4: Review diff and commit final corrections**

Run: `git diff --check`, `git status --short`, and inspect the branch diff against `ec4a4f4`.
Expected: only Alpha-3 design, plan, models, service, context integration, migration, and tests are present; no caches, databases, secrets, or temporary files.
