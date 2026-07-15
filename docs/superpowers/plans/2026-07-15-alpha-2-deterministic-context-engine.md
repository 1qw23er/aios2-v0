# AIOS Alpha-2 Deterministic Context Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, persisted, versioned TaskContext service from approved AIOS records without using an LLM.

**Architecture:** Add normalized review/lineage source records and immutable-by-update TaskContext snapshots in migration 0004. A focused ContextService selects allowlisted sources in stable order, hashes canonical JSON, and atomically persists the snapshot plus a redacted audit; the external adapter serializes that snapshot without changing legacy packet behavior.

**Tech Stack:** Python 3.12+, SQLModel, SQLAlchemy, Alembic, SQLite, Pydantic, pytest, Ruff.

---

### Task 1: Alpha-2 persistence model and migration

**Files:**
- Modify: `src/aios/models.py`
- Create: `alembic/versions/20260715_0004_context_engine.py`
- Create: `tests/test_context_models.py`

- [ ] **Step 1: Write failing persistence tests**

Create tests that import `ArtifactReviewStatus`, `Decision`, `DecisionStatus`, `Policy`,
`ReviewedFact`, `ReviewedFactStatus`, and `TaskContext`; migrate a fresh SQLite database;
persist every model; and assert existing Project, Artifact, and Agent defaults remain
compatible. Add database-failure assertions for duplicate `(series_id, version)` Decision
and Policy rows.

```python
decision = Decision(series_id="publication", version=1, title="Publish", content="Review")
duplicate = Decision(series_id="publication", version=1, title="Duplicate", content="No")
with pytest.raises(IntegrityError):
    session.add_all([decision, duplicate])
    session.commit()
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_context_models.py -v`

Expected: collection fails because the Alpha-2 models do not exist.

- [ ] **Step 3: Add minimal SQLModel types**

Add the reviewed status enums and models described by the approved design. Use compound
`UniqueConstraint` table arguments for Decision/Policy lineage and TaskContext idempotency.

```python
class TaskContext(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("task_id", "context_hash"),)
    id: str = Field(default_factory=lambda: new_id("ctx"), primary_key=True)
    task_id: str = Field(foreign_key="task.id", index=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    assigned_agent_id: str | None = Field(default=None, foreign_key="agent.id")
    objective: str
    instructions: str
    acceptance_criteria: list[str] = Field(sa_column=Column(JSON))
    project_context: dict[str, Any] = Field(sa_column=Column(JSON))
    dependency_outputs: list[dict[str, Any]] = Field(sa_column=Column(JSON))
    approved_facts: list[dict[str, Any]] = Field(sa_column=Column(JSON))
    relevant_decisions: list[dict[str, Any]] = Field(sa_column=Column(JSON))
    applicable_policies: list[dict[str, Any]] = Field(sa_column=Column(JSON))
    agent_profile: dict[str, Any] = Field(sa_column=Column(JSON))
    source_references: list[dict[str, Any]] = Field(sa_column=Column(JSON))
    context_hash: str = Field(index=True)
    created_at: datetime = Field(default_factory=now_utc)
```

- [ ] **Step 4: Add migration 0004**

Add backward-compatible defaults for `project.description`, `artifact.review_status`, and
`agent.limitations`; create ReviewedFact, Decision, Policy, and TaskContext with foreign
keys and lineage uniqueness. Do not add the TaskContext UPDATE trigger until Task 3 has
first demonstrated the failing immutability test. Downgrade reverses only Alpha-2 schema.

- [ ] **Step 5: Verify GREEN and commit**

Run:

```powershell
python -m pytest tests/test_context_models.py -v
python -m ruff check src tests alembic
git add src/aios/models.py alembic/versions/20260715_0004_context_engine.py tests/test_context_models.py
git commit -m "feat: add Alpha-2 context persistence"
```

Expected: targeted tests and Ruff pass.

### Task 2: Deterministic ContextService assembly

**Files:**
- Create: `src/aios/context_service.py`
- Create: `tests/test_context_service.py`

- [ ] **Step 1: Write failing deterministic assembly tests**

Seed two Projects, completed and incomplete dependencies, approved/unverified/rejected
Artifacts, explicit ReviewedFact rows, versioned Decisions/Policies, an Assignment, and an
Agent capability profile. Assert:

```python
first = ContextService(session).build_context(task.id, assignment.id)
second = ContextService(session).build_context(task.id, assignment.id)
assert first.id == second.id
assert first.context_hash == second.context_hash
assert [fact["statement"] for fact in first.approved_facts] == ["Reviewed fact"]
assert "artifact summary" not in first.approved_facts
```

Add separate tests proving unrelated Project data and unapproved sources are absent, and an
included approved Artifact checksum or reviewed fact statement change produces a different
hash.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_context_service.py -v`

Expected: collection fails because `ContextService` does not exist.

- [ ] **Step 3: Implement canonical helpers and safe projections**

Implement recursive sensitive-key redaction, normalized UTC timestamps, canonical JSON,
SHA-256 helpers, stable source references, and allowlisted projections. Never read `os.environ`,
Agent endpoint/config reference/cost policy, Artifact URI, or arbitrary Artifact metadata.

```python
def canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
```

- [ ] **Step 4: Implement assignment authority**

When an assignment ID is supplied, verify Task ownership, require its Agent, derive the
context Agent only from the Assignment, and reject a conflicting non-null Task Agent.

```python
if assignment.task_id != task.id:
    raise ServiceError(409, "Assignment does not belong to task")
if task.assigned_agent_id and task.assigned_agent_id != assignment.selected_agent_id:
    raise ServiceError(409, "Task and assignment agents conflict")
```

- [ ] **Step 5: Implement deterministic source selection**

Select only completed dependency approved Artifacts; only approved ReviewedFact rows backed
by those Artifacts; the highest approved Decision version per series; the highest enabled
Policy version per series; and safe Agent capability data. Reject series whose rows mix
company and Project scopes. Sort every list by stable IDs/series/version before hashing.

- [ ] **Step 6: Persist idempotently with audit**

Look up `(task_id, context_hash)` before insertion. On a new snapshot, append one
`context.generated` AuditLog containing IDs, hash, optional assignment ID, and source
references, then commit. Roll back on any failure.

- [ ] **Step 7: Verify GREEN and commit**

Run:

```powershell
python -m pytest tests/test_context_service.py -v
python -m pytest tests/test_context_models.py tests/test_context_service.py -q
python -m ruff check src tests alembic
git add src/aios/context_service.py tests/test_context_service.py
git commit -m "feat: build deterministic task contexts"
```

### Task 3: Immutability and controlled retention

**Files:**
- Create: `src/aios/context_retention.py`
- Create: `tests/test_context_retention.py`
- Modify: `alembic/versions/20260715_0004_context_engine.py`

- [ ] **Step 1: Write failing mutation and retention tests**

Write a test asserting SQLAlchemy UPDATE raises IntegrityError; before implementation it
must fail because the database still permits the update. Assert normal
SQL DELETE is not permanently blocked. Test the internal retention function rejects empty
actor/rationale, creates `context.deleted`, deletes the snapshot, excludes full payloads
from audit, and rolls back deletion when audit insertion fails.

```python
delete_context_for_retention(session, context.id, actor="admin", rationale="expired")
assert session.get(TaskContext, context.id) is None
assert audit.action == "context.deleted"
assert "instructions" not in audit.after_snapshot
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_context_retention.py -v`

Expected: retention module is missing and UPDATE is not yet rejected before migration work.

- [ ] **Step 3: Implement controlled deletion**

Add the SQLite BEFORE UPDATE rejection trigger and its downgrade removal to migration
0004. Implement delete_context_for_retention(session, context_id, actor, rationale) as an
internal function. Validate non-empty actor/rationale; append the minimal audit; delete;
commit atomically; and roll back on errors. Do not expose it from FastAPI.

- [ ] **Step 4: Verify GREEN and commit**

Run:

```powershell
python -m pytest tests/test_context_retention.py -v
python -m ruff check src tests alembic
git add src/aios/context_retention.py tests/test_context_retention.py alembic/versions/20260715_0004_context_engine.py
git commit -m "feat: enforce context retention controls"
```

### Task 4: External workstation TaskContext package

**Files:**
- Modify: `src/aios/adapters/external.py`
- Create: `tests/test_context_external.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing backward-compatibility and context export tests**

Keep the current legacy export test unchanged. Add a context-aware export test asserting
the directory contains `task_packet.json`, `task_context.json`, and `context.md`; JSON
round-trips to TaskContext; Markdown includes objective/instructions/facts/provenance; and
existing external result import still succeeds.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_context_external.py -v`

Expected: `export_task` rejects the new `task_context` keyword or no context JSON is written.

- [ ] **Step 3: Extend adapter without changing legacy behavior**

Add `task_context: TaskContext | dict[str, Any] | None = None` as a keyword-only argument.
When absent, preserve the current packet and caller-supplied Markdown behavior. When present,
write canonical structured JSON and use a deterministic renderer for `context.md`.

```python
def export_task(self, packet, context="", *, task_context=None) -> ExportedTask:
    ...
```

Expose optional `context_json_path` on `ExportedTask`; do not add a required TaskPacket field.

- [ ] **Step 4: Document and commit**

Document Alpha-2 snapshot generation, explicit ReviewedFact semantics, lineage, retention,
and external files. Run targeted tests and Ruff, then commit:

```powershell
python -m pytest tests/test_context_external.py tests/test_audit_external.py -q
python -m ruff check src tests alembic
git add src/aios/adapters/external.py tests/test_context_external.py README.md
git commit -m "feat: export structured task contexts"
```

### Task 5: Full verification and delivery

**Files:**
- Verify all changed files

- [ ] **Step 1: Verify migration round trip**

On an isolated SQLite database run migration `0003 -> 0004 -> 0003 -> 0004`, then verify
`python -m alembic current` reports `20260715_0004 (head)`.

- [ ] **Step 2: Run complete quality gates**

```powershell
python -m pytest -q
python -m ruff check .
git diff --check
```

Expected: every P0, Alpha-1, and Alpha-2 test passes; Ruff and whitespace checks are clean.

- [ ] **Step 3: Review scope exclusions**

Search source and tests to confirm no embeddings, vector search, RAG, summarization,
knowledge extraction, token-budget optimization, prompt templating, LLM routing, or UI code
was added.

- [ ] **Step 4: Merge and verify main**

Fast-forward the clean feature branch to main, rerun complete pytest and Ruff on main, push
GitHub, and verify local/remote main hashes match. Report migration, ContextService,
immutability/retention, external integration, audit, test counts, Ruff result, migration
round trip, and the full final commit hash.
