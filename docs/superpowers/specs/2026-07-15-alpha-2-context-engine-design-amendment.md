# AIOS Alpha-2 Context Engine Design Amendment

## Status and precedence

This amendment records the required architecture-review changes to
`2026-07-15-alpha-2-deterministic-context-engine-design.md`. It is authoritative wherever
the original design conflicts with this document. All other approved design decisions
remain unchanged.

## Explicitly reviewed facts

An approved Artifact is evidence, not automatically a fact. Artifact approval only makes
the Artifact's safe projection eligible for `dependency_outputs`.

Alpha-2 adds `ReviewedFact` with:

- `id`
- `artifact_id`
- `statement`
- `status`: pending, approved, or rejected
- `reviewer`
- `reviewed_at`, optional
- `created_at`

`TaskContext.approved_facts` contains structured projections of approved ReviewedFact rows
whose source Artifact is also approved and belongs to a completed dependency Task in the
current Project. Each projection contains fact ID, statement, reviewer, reviewed timestamp,
and source Artifact ID. Artifact metadata, summaries, claims, or approval status never
create facts implicitly.

Approved Artifacts still contribute safe evidence projections to `dependency_outputs`.
Rejected and unverified Artifacts contribute neither evidence nor facts. Pending or rejected
ReviewedFact rows never enter TaskContext.

## Decision and Policy lineage

Each Decision version adds `series_id`, a stable logical identity shared by all versions of
the same Decision. The database enforces uniqueness on `(series_id, version)`. Each row has
its own record ID and positive integer version. ContextService includes the highest approved
version in each in-scope Decision series. A newer draft or rejected version does not hide
the latest approved version.

Each Policy version likewise adds `series_id`, with database uniqueness on
`(series_id, version)`. ContextService includes the highest enabled version in each in-scope
Policy series. A newer disabled version does not hide the latest enabled version.

All versions in one series must use the same company-wide or Project scope. ContextService
rejects inconsistent persisted lineage rather than silently combining scopes. Alpha-2 does
not add semantic matching or mutable in-place version increments; a new version is a new
Decision or Policy row.

Source references identify both record ID and series/version. Their `version` value is the
stable `series_id:version` pair, supplemented by normalized `updated_at` in the included
projection.

## TaskContext retention and immutability

TaskContext remains append-only during normal application behavior. The migration adds a
database trigger that rejects UPDATE. It does not add a trigger that permanently rejects
DELETE.

No REST endpoint or normal ContextService method updates or deletes TaskContext. A separate
administrative retention operation may delete a TaskContext only when given:

- context ID
- administrative actor
- non-empty rationale

The operation atomically appends a `context.deleted` AuditLog record containing context ID,
task ID, project ID, context hash, actor, and rationale, then deletes the TaskContext. It
does not copy the full context payload into AuditLog. If audit insertion or deletion fails,
the transaction rolls back. This operation is an internal service function and is not
exposed through the normal API in Alpha-2.

## Assignment authority and validation

When `assignment_id` is supplied to
`ContextService.build_context(task_id, assignment_id=None)`:

1. The ExecutionAssignment must exist.
2. Its `task_id` must exactly equal the requested `task_id`.
3. Its `selected_agent_id` must resolve to an existing Agent.
4. `assigned_agent_id` is derived exclusively from that ExecutionAssignment.
5. If the Task's non-null `assigned_agent_id` differs from the Assignment's selected Agent,
   generation fails with a conflict instead of choosing either value silently.

When `assignment_id` is omitted, ContextService may use `Task.assigned_agent_id`. A missing
referenced Agent is an explicit error. The supplied Assignment record, selected Agent, and
safe Agent profile all appear in provenance and therefore affect `context_hash`.

## Migration and test additions

Migration `20260715_0004` additionally creates ReviewedFact and the Decision/Policy lineage
constraints. It creates only the TaskContext UPDATE-rejection trigger; DELETE remains
available to the controlled retention operation.

Red-green-refactor tests additionally prove:

- approved Artifact content is evidence but does not become a fact;
- only explicitly approved ReviewedFact rows enter `approved_facts`;
- `(series_id, version)` is unique for Decision and Policy;
- ContextService selects the highest approved/enabled version per series;
- inconsistent lineage scope is rejected;
- TaskContext UPDATE is rejected;
- controlled deletion succeeds only with an audit and rationale;
- audit failure rolls back controlled deletion;
- Assignment/Task mismatch, missing selected Agent, and conflicting Task Agent are rejected;
- a supplied valid Assignment is the exclusive source of `assigned_agent_id`.
