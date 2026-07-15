# AIOS Alpha-3 Deterministic Knowledge Engine Design

## Goal and scope

Alpha-3 adds deterministic, human-reviewed knowledge ingestion to the Alpha-2 Context
Engine. An Artifact may be the source of a candidate statement, but neither Artifact
approval nor candidate submission automatically creates reusable knowledge. A human
reviewer must explicitly approve each candidate before AIOS creates a `KnowledgeFact`.

This is an additive increment. It preserves the P0 workflow architecture, Alpha-1 routing,
the existing `ReviewedFact` model, immutable `TaskContext` snapshots, and the external
workstation package contract. It does not redesign Project, Task, Event, Artifact,
Approval, AuditLog, transactional outbox, or execution assignment behavior.

Alpha-3 uses no LLM. Candidate statements are supplied explicitly by a human or a
deterministic caller and are stored without extraction, summarization, or semantic
transformation.

## Core invariants

Reusable knowledge passes two independent gates:

1. The source `Artifact.review_status` must be `approved`.
2. A human reviewer must record an approving `KnowledgeReviewDecision` for the candidate.

Approval of an Artifact alone never promotes its contents to knowledge. A candidate may
have exactly one terminal review decision. Rejected candidates never create facts.

Knowledge has two scopes:

- `project_id = null`: company-wide knowledge.
- `project_id = <project id>`: knowledge belonging to exactly that Project.

A project TaskContext may include approved company-wide facts and approved facts for the
same Project. It must never include facts from another Project. Draft or rejected
candidates and superseded or inactive facts are excluded. `KnowledgeFact` has no rejected
status; rejection exists only at the candidate/review layer.

## Domain model

### KnowledgeCandidate

`KnowledgeCandidate` is a proposed factual statement awaiting human review:

- `id`
- `artifact_id`, foreign key to the source Artifact
- `project_id`, nullable scope
- `statement`, non-empty normalized text supplied by the caller
- `status`: `draft`, `approved`, or `rejected`
- `submitted_by`, non-empty actor identifier
- `created_at`
- `updated_at`

The candidate's Artifact, scope, statement, submitter, and creation timestamp are
immutable after insertion. Only the controlled transition from `draft` to `approved` or
`rejected` is allowed. There is no transition out of a terminal state.

Scope validation is deterministic and exact. `KnowledgeCandidate.project_id` must equal
its source `Artifact.project_id`, including null equality. A project-scoped Artifact
cannot produce a company-wide candidate, and an Artifact cannot produce a candidate for
another Project. Promotion from project knowledge to company knowledge requires a future
workflow with explicit authorization and is outside Alpha-3.

### KnowledgeReviewDecision

`KnowledgeReviewDecision` is the immutable record of the terminal human review:

- `id`
- `candidate_id`, foreign key to KnowledgeCandidate
- `decision`: `approve` or `reject`
- `reviewer`, non-empty actor identifier
- `rationale`, non-empty review explanation
- `reviewed_at`

A database-level unique constraint on `candidate_id` enforces one terminal review per
candidate. The service treats an exact retry of the same terminal decision as idempotent
and returns the existing result; a conflicting retry is rejected. There is no update or
delete API for review decisions.

### KnowledgeFact

`KnowledgeFact` is reusable, reviewed knowledge:

- `id`
- `series_id`, stable logical identity across versions
- `version`, positive integer
- `project_id`, nullable scope
- `statement`
- `status`: `approved`, `superseded`, or `inactive`
- `source_candidate_id`, foreign key to KnowledgeCandidate
- `source_artifact_id`, foreign key to Artifact
- `review_decision_id`, foreign key to KnowledgeReviewDecision
- `supersedes_fact_id`, nullable self-reference
- `created_at`
- `updated_at`

Every fact is born `approved` from an approving review transaction. There is no draft
KnowledgeFact and no route from a rejected review to a fact. Statement, series, version,
scope, provenance, creation timestamp, and supersession pointer are immutable. The only
normal lifecycle transitions are `approved -> superseded` and `approved -> inactive`.

The database enforces uniqueness for `(series_id, version)`, `source_candidate_id`, and
`review_decision_id`. A unique nullable `supersedes_fact_id` permits at most one direct
successor to a fact. These constraints prevent duplicate promotion and divergent direct
Each `(series_id, project scope)` has at most one `approved` head. The database uses a
partial unique index over series and normalized nullable scope for approved rows, in
addition to service validation and lifecycle triggers.

replacement chains.

## Supersession rules

Supersession is explicit: the approving caller supplies `supersedes_fact_id`. It is never
inferred by statement similarity.

For the first fact in a new series and scope:

- `version` must be `1`.
- `supersedes_fact_id` must be null.
- No fact may already exist in that series and scope.

If the series and scope already exist, the new approved fact must explicitly supersede
the current approved head. To supersede that head:

- The previous fact must currently be `approved`.
- The previous fact must be the current approved head; skipped heads are invalid.
- The new fact must use the same `series_id`.
- The new fact must have exactly the same scope, including the same nullable
  `project_id`.
- The new version must be strictly greater than the previous version.
- The source candidate must independently satisfy the Artifact and human-review gates.
- The new fact insertion and previous fact's transition to `superseded` occur in one
  database transaction.

The predecessor pointer is immutable and always points backward to a lower version in the
same series and scope. Combined with the strict version increase, this makes a
supersession cycle impossible. Database constraints and triggers reject violations even
if a caller bypasses normal service validation. The approved-head unique index and
predecessor validation reject parallel approved branches, skipped-head replacements, and
disconnected later versions. A failed insert, status transition, audit write, or outbox
write rolls back the entire operation.

## KnowledgeService boundary

Alpha-3 exposes a service layer only. It adds no REST endpoints or UI.

### submit_candidate

`submit_candidate(artifact_id, statement, project_id, submitted_by) -> KnowledgeCandidate`
performs deterministic validation and persistence:

- the Artifact exists and is approved;
- the statement and actor are non-empty;
- nullable project scope exactly equals the Artifact scope, so widening is prohibited;
- no Artifact payload is parsed and no statement is generated;
- the candidate is stored as `draft`;
- `knowledge.candidate.created` is audited without copying Artifact content.

### review_candidate

`review_candidate(candidate_id, decision, reviewer, rationale, *, series_id=None,
version=None, supersedes_fact_id=None)` records the only terminal review.

For rejection, one transaction inserts the review decision, changes the candidate to
`rejected`, and records `knowledge.candidate.rejected`. It creates no KnowledgeFact.

For approval, one transaction revalidates the source Artifact, inserts the review
decision, changes the candidate to `approved`, creates an `approved` KnowledgeFact, and
records `knowledge.fact.approved`. When superseding, that same transaction validates the
predecessor, changes it to `superseded`, creates the replacement, and records
`knowledge.fact.superseded` with both fact IDs. The service requires explicit series and
version data; it does not guess lineage.

An exact retry returns the existing decision and fact without duplicating records or
audit events. A different reviewer, decision, rationale, fact identity, version, or
supersession request for an already-reviewed candidate is a conflict.

### deactivate_fact

`deactivate_fact(fact_id, actor, rationale) -> KnowledgeFact` changes an `approved` fact to
`inactive` and records `knowledge.fact.deactivated` atomically. The actor and rationale
are required. Superseded or already inactive facts cannot be deactivated again through
the normal service. There is no reactivation in Alpha-3.

## Transaction, idempotency, and error behavior

Knowledge state changes, their AuditLog entries, and any transactional-outbox records use
the existing unit-of-work pattern. The caller either observes the complete transition or
no transition.

Domain errors are explicit for:

- missing or unapproved source Artifact;
- empty statement, actor, reviewer, or rationale;
- project-scope mismatch;
- an already terminal candidate;
- conflicting duplicate review;
- missing or duplicate series/version;
- invalid predecessor status, series, scope, or version;
- attempted supersession branching, skipped head, disconnected version, or cycle;
- invalid deactivation.

Database uniqueness is the final concurrency guard. Constraint failures are rolled back
and translated into stable service errors rather than leaving partial knowledge state.

## ContextService integration

`ContextService.build_context` adds active KnowledgeFacts to the existing
`approved_facts` collection. Existing `ReviewedFact` projection remains unchanged for
backward compatibility. Each new projection is explicitly typed:

```json
{
  "fact_kind": "knowledge_fact",
  "fact_id": "kfact_...",
  "series_id": "...",
  "version": 2,
  "scope": "company",
  "project_id": null,
  "statement": "...",
  "source_candidate_id": "kcand_...",
  "source_artifact_id": "art_...",
  "review_decision_id": "krev_..."
}
```

Project-scoped entries use `scope: "project"` and the current `project_id`. Selection is a
plain database predicate: `status = approved` and (`project_id is null` or
`project_id = current task project`). No semantic relevance scoring occurs. Results use a
stable ordering by scope, series, version, and ID before canonical serialization.

For every included KnowledgeFact, `source_references` records the KnowledgeFact,
KnowledgeCandidate, KnowledgeReviewDecision, and Artifact identities and versions or
timestamps. The fact reference includes its company/project scope and the inclusion
reason. This preserves a complete provenance path without embedding full Artifact
payloads, credentials, secrets, or environment variables.

KnowledgeFact content and provenance participate in the existing canonical JSON hash.
Identical persisted source state produces the same `context_hash`; adding, superseding,
or deactivating an included fact changes the next context hash. Existing TaskContext rows
remain immutable historical snapshots.

The external workstation exporter requires no alternate selection logic. Its structured
TaskContext JSON automatically carries the added facts and provenance, while the existing
`task_packet.json` and readable `context.md` interfaces remain backward compatible.

## Database migration

Migration `20260716_0005_knowledge_engine` follows Alpha-2 revision `0004` and creates:

- `knowledge_candidate`;
- `knowledge_review_decision`;
- `knowledge_fact`;
- indexes for source Artifact, project scope, candidate status, fact status, series, and
  predecessor lookup;
- the uniqueness and foreign-key constraints described above;
- SQLite triggers that reject mutation of immutable identity/provenance fields and reject
  invalid lifecycle or supersession transitions.
- a partial unique approved-head index over series and normalized nullable project scope.

The migration does not transform `ReviewedFact` records or promote historical Artifacts.
Upgrade begins with no KnowledgeFacts. Downgrade removes only Alpha-3 objects and leaves
all Alpha-2 data and behavior intact.

Application behavior is append-oriented: candidates, reviews, and facts have no normal
delete API. Database-level deletion is not globally prohibited, so a future controlled
administrative retention process may delete records in dependency order with its own
authorization and audit policy.

## Audit contract

Alpha-3 records these event types in the immutable AuditLog:

- `knowledge.candidate.created`;
- `knowledge.candidate.rejected`;
- `knowledge.fact.approved`;
- `knowledge.fact.superseded`;
- `knowledge.fact.deactivated`.

Audit metadata contains resource IDs, actor/reviewer, decision, scope, series/version,
predecessor/replacement IDs when applicable, and rationale where required. It does not
contain Artifact bodies, secrets, credentials, raw environment variables, or other full
sensitive payloads.

## Test strategy

Implementation follows red-green-refactor. Tests cover:

- candidate creation from an approved Artifact;
- rejection of missing, unapproved, rejected, or cross-project Artifact sources;
- exact Artifact/candidate scope equality, including null equality;
- rejection of project-to-company scope widening;
- one terminal review per candidate at both service and database levels;
- exact review retry idempotency and conflicting retry rejection;
- rejection creates no KnowledgeFact;
- approval creates one immutable fact with complete Artifact/candidate/review provenance;
- Artifact approval alone never creates knowledge;
- supersession requires an approved predecessor, identical series and scope, and a
  strictly greater version;
- the first series fact requires version 1 and no predecessor;
- every later fact explicitly replaces the current approved head;
- duplicate/branching successors, skipped heads, disconnected versions, and cycle
  attempts are rejected;
- two concurrent replacement attempts cannot create two approved heads;
- replacement creation and predecessor transition are atomic under injected failure;
- inactive, superseded, rejected, and draft knowledge never enters TaskContext;
- company-wide and same-project approved facts enter context;
- another Project's facts never enter context;
- deterministic ordering and identical context hashes for identical source state;
- fact approval, supersession, and deactivation change subsequent context hashes;
- source references contain scope and full provenance IDs;
- existing ReviewedFact projection and Alpha-2 context behavior remain compatible;
- external workstation packages include structured knowledge and readable context;
- AuditLog events are complete and contain no Artifact payload or secret fields;
- TaskContext remains readable in a new database session;
- migration verification for `0004 -> 0005 -> 0004 -> 0005`;
- the complete pytest suite and Ruff checks for `src`, `tests`, and `alembic`.

## Explicit exclusions

Alpha-3 does not implement:

- LLM-based or rule-based fact extraction;
- automatic promotion of approved Artifacts;
- embeddings, vector search, RAG, or automatic summarization;
- semantic duplicate or contradiction detection;
- dynamic trust, confidence, quality, or reputation scoring;
- knowledge decay or automatic expiration;
- project-to-company knowledge promotion or any scope-widening authorization workflow;
- REST APIs, UI, or Context Engine UI;
- dynamic token-budget optimization;
- multi-agent review, parallel candidate competition, or workflow graph changes;
- capability-routing changes, cost optimization, or LLM routing.

These require later milestones and operational evidence. Alpha-3 remains a deterministic,
human-controlled ingestion and reuse layer over the approved Alpha-2 foundation.
