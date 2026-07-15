# AIOS Alpha-2 Deterministic Context Engine Design

## Goal and scope

Alpha-2 adds a deterministic context assembly service that creates a structured,
versioned, immutable `TaskContext` for any persisted Task. It is additive to the P0
workflow architecture and Alpha-1 capability routing. It does not change the workflow
graph, execute an Agent, or use an LLM.

This milestone excludes embeddings, vector search, RAG, automatic summarization,
knowledge extraction, dynamic token-budget optimization, prompt templating, LLM-based
context selection, and a Context Engine UI.

## Chosen architecture

AIOS will use persisted source models plus immutable context snapshots. This is preferred
over rebuilding context from Event or AuditLog history because the current event stream
does not contain complete decision, policy, or Artifact-review semantics. It is preferred
over an unpersisted live view because immutable snapshots, provenance, idempotency, and
historical reproduction are milestone requirements.

`ContextService` owns context selection, normalization, hashing, persistence, and audit.
The external workstation adapter only serializes an already-built TaskContext; it does not
select, summarize, or transform business facts.

## Additive source models

### Existing model extensions

- `Project.description` is added with an empty-string default so existing Projects remain
  valid.
- `Artifact.review_status` uses `unverified`, `approved`, or `rejected`. Existing Artifacts
  migrate to `unverified`; Alpha-2 never silently promotes historical output to fact.
- `Agent.limitations` is a JSON string list with an empty-list default. Context never reads
  Agent endpoint, config reference, cost policy, credentials, or environment variables.

### Decision

`Decision` stores versioned approved business guidance:

- `id`
- `project_id`, optional; null means company-wide
- `title`
- `content`
- `status`: draft, approved, or rejected
- `version`, a positive integer
- `created_at`
- `updated_at`

Only approved company-wide Decisions and approved Decisions belonging to the current
Project are relevant. Alpha-2 does not introduce a Company root model; null `project_id` is
the explicit company-wide scope until that later additive increment exists.

### Policy

`Policy` stores deterministic operating constraints:

- `id`
- `project_id`, optional; null means company-wide
- `name`
- `content`
- `enabled`
- `version`, a positive integer
- `created_at`
- `updated_at`

All enabled company-wide Policies and enabled Policies belonging to the current Project
are applicable. Alpha-2 does not add tag, semantic, or LLM-based policy matching.

## TaskContext persistence

`TaskContext` stores:

- `id`
- `task_id`
- `project_id`
- `assigned_agent_id`, optional
- `objective`
- `instructions`
- `acceptance_criteria`
- `project_context`
- `dependency_outputs`
- `approved_facts`
- `relevant_decisions`
- `applicable_policies`
- `agent_profile`
- `source_references`
- `context_hash`
- `created_at`

Structured fields use JSON columns. A unique constraint on `(task_id, context_hash)` makes
generation idempotent while allowing a new snapshot when included source state changes.
SQLite migration triggers reject UPDATE and DELETE against `task_context`. The service also
exposes no mutation operation. TaskContext is therefore append-only after creation.

The required workflow metadata lives inside `project_context`: Project name, description,
status, Task status, routing mode, dependency IDs, input context references, and selected
ExecutionAssignment metadata when supplied. This avoids adding a second top-level workflow
field outside the approved TaskContext shape.

## ContextService interface

`ContextService.build_context(task_id, assignment_id=None) -> TaskContext` performs one
deterministic assembly transaction.

1. Load Task and its Project.
2. If `assignment_id` is supplied, load it, require that it belongs to the Task, and use its
   selected Agent. Otherwise use `Task.assigned_agent_id` when present.
3. Load dependency Tasks in sorted ID order. Only dependencies in `done` status contribute
   outputs.
4. For each completed dependency, load approved Artifacts in sorted Artifact ID order.
5. Load approved company-wide and current-Project Decisions in scope/ID/version order.
6. Load enabled company-wide and current-Project Policies in scope/ID/version order.
7. Load the selected Agent and its enabled AgentCapability/Capability rows in Capability ID
   order.
8. Produce ordered source references and safe structured projections.
9. Serialize the hash payload as canonical UTF-8 JSON and calculate SHA-256.
10. Return an existing row for `(task_id, context_hash)`, or atomically insert TaskContext
    and its generation audit.

Missing Task, Project, Assignment, or selected Agent references are explicit service errors.
An Assignment belonging to another Task is a conflict. Ineligible source records are
excluded rather than treated as service failures.

## Included content

The context contains:

- Project objective, name, description, status, and relevant workflow metadata.
- Current Task description as `instructions` and its acceptance criteria.
- Approved Artifact projections from completed dependency Tasks.
- Explicit approved facts from those approved Artifacts.
- Approved company-wide and current-Project Decisions.
- Enabled company-wide and current-Project Policies.
- The selected Agent's safe profile, limitations, permissions, adapter type, status, and
  enabled capability priorities.
- Exact source provenance for every included persisted record.

Artifact metadata is not copied wholesale. Artifact approval is the explicit reviewer
attestation that the allowlisted summary and approved facts are safe for downstream
context. `dependency_outputs` includes only Artifact ID,
type, checksum, an explicitly stored summary, and explicitly stored approved facts.
`approved_facts` is populated only from the `approved_facts` key of an approved Artifact's
metadata. Alpha-2 does not infer facts from claims, free text, files, or URIs.

## Excluded and sensitive content

The service excludes rejected and unverified Artifacts, pending/draft/rejected Decisions,
disabled Policies, incomplete dependency outputs, and all unrelated Project records. It
does not load process environment variables or Agent endpoint/configuration references.

All projections are allowlists. A recursive safety pass redacts structured keys named
secret, token, password, credential, api_key, api-key, authorization, or cookie before
hashing or persistence. Decision and Policy approval is the explicit reviewer attestation that their governed
business text is safe for downstream context and contains no credentials. Alpha-2 does not
attempt probabilistic secret detection in prose.

## Determinism and hashing

Hash input includes every persisted TaskContext business field from `task_id` through
`source_references`, excluding only generated `id`, `context_hash`, and `created_at`.
Canonical JSON uses sorted object keys, compact separators, UTF-8, preserved Unicode, and
normalized UTC timestamps. Lists are sorted by explicit stable keys before serialization.

The same included source state therefore produces byte-identical normalized JSON and the
same context hash. A change to any included Project, Task, Assignment, approved Artifact,
Decision, Policy, Agent profile, capability profile, or source version changes the hash.
Changes to excluded or unrelated records do not change it.

## Provenance and versions

Every source reference contains:

- `resource_type`
- `resource_id`
- `version`
- `inclusion_reason`

Project, Task, Decision, and Policy references use normalized `updated_at`; Decision and
Policy versions also include their explicit integer version. Artifact version is its
immutable checksum. ExecutionAssignment version combines ID and `created_at`. Agent,
Capability, and AgentCapability lack update timestamps, so their version is a SHA-256 hash
of the exact safe field projection used by ContextService.

Source references use fixed inclusion reasons such as `task_definition`,
`project_scope`, `completed_dependency_output`, `approved_fact_source`,
`approved_decision`, `applicable_policy`, `selected_assignment`, and
`selected_agent_profile`.

## Audit and transaction behavior

New Context creation appends one `context.generated` AuditLog entry in the same transaction.
The audit contains task ID, optional assignment ID, context ID, context hash, and source
references. It never contains full TaskContext payloads, Artifact contents, Decision text,
Policy text, Agent configuration, secrets, or credentials.

The audit idempotency key derives from the Context ID. Rebuilding identical source state
returns the existing TaskContext and creates no additional audit. Audit insertion or commit
failure rolls back the new TaskContext. The service leaves the caller's Session usable by
rolling back before propagating database errors.

## External workstation integration

The existing `task_packet.json` schema and the existing `context.md` path remain valid.
`ExternalWorkstationAdapter.export_task` gains an optional `task_context` argument:

- Calls using the existing `export_task(packet, context_string)` signature behave as before.
- Calls supplying TaskContext also write `task_context.json` next to the existing files.
- `task_context.json` is the structured source of truth for the exported context.
- `context.md` is rendered deterministically from the same TaskContext using fixed headings
  and ordered sections.
- `task_packet.json` receives no new required field, so existing closed-source workstations
  remain compatible.

`ExportedTask` may expose an optional `context_json_path`; old consumers of packet and
Markdown paths remain valid. File-write failures do not mutate or delete the persisted
TaskContext, and callers can safely retry export with the same snapshot.

## Migration

Migration `20260715_0004` adds Project description, Artifact review status, Agent
limitations, Decision, Policy, and TaskContext. Existing rows receive backward-compatible
defaults. It adds TaskContext uniqueness and immutability triggers. Downgrade removes the
triggers and Alpha-2 tables/columns without changing P0 or Alpha-1 tables beyond reversing
the new columns.

Verification must prove `0003 -> 0004 -> 0003 -> 0004` on SQLite and a fresh upgrade to
head.

## Test strategy

Red-green-refactor tests cover:

- identical inputs return the same Context ID and context hash;
- a changed included dependency Artifact changes the hash;
- rejected and unverified Artifacts are excluded;
- unrelated Project Artifacts, Decisions, and Policies are excluded;
- company-wide and current-Project approved Decisions and enabled Policies are included;
- selected Agent capabilities, priorities, permissions, and limitations are included;
- mismatched Assignment references are rejected;
- source references record exact stable versions and reasons;
- generation audit contains identifiers, hash, and references without full payloads;
- an audit failure rolls back Context creation;
- TaskContext cannot be updated or deleted;
- duplicate generation creates no duplicate Context or audit;
- TaskContext remains readable in a new database Session;
- legacy external export remains unchanged;
- context-aware export writes task packet, structured context JSON, and readable Markdown;
- the existing external result import and P0/Alpha-1 suites remain green.

Final delivery requires complete pytest and Ruff success, Alembic upgrade/downgrade
evidence, a clean worktree, and a final Git commit hash.
