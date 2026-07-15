# AIOS Alpha-1 Capability Routing Design

## Goal and scope

Alpha-1 adds deterministic capability-based worker selection without rewriting P0. Existing fixed-agent tasks, workflow dependencies, transactional outbox, AuditLog, approvals, and external workstation packages remain valid. This increment excludes Context Engine, Knowledge Engine, dynamic or learned scoring, reputation, parallel execution, quality scoring, cost optimization, and LLM routing.

## Persistence model

`Capability` stores an ID, unique name, and description. `AgentCapability` uses `(agent_id, capability_id)` as its compound primary key and stores static priority from 1 through 100 plus an enabled flag.

`Agent.status` adds `available`, `unavailable`, and `maintenance`, defaulting to available. Existing `Agent.capabilities` JSON remains for compatibility but is not used by the new scheduler.

`Task` adds `required_capabilities`, optional `preferred_agent_id`, and `routing_mode`. Routing modes are fixed, preferred_with_fallback, best_available, and manual. The default is fixed, so existing tasks continue to use `assigned_agent_id` unchanged.

`ExecutionAssignment` stores its ID, task ID, selected Agent ID, routing reason, fallback flag, idempotency key, and creation time. The idempotency key is unique so event retries cannot duplicate assignments.

## Scheduler boundary

The Orchestrator remains responsible only for deciding when a task becomes ready. `DeterministicScheduler.route_task(task_id, idempotency_key)` runs after readiness and decides who should execute. It neither modifies the workflow graph nor invokes an Agent.

A successful decision atomically writes one ExecutionAssignment, updates `Task.assigned_agent_id`, appends `task.assigned`, and appends an AuditLog entry. Replaying the same idempotency key returns the original assignment without new state, event, or audit rows.

## Routing modes

Fixed mode uses the existing `assigned_agent_id`. If that Agent is unavailable, in maintenance, disabled, or missing, the task remains ready and no ExecutionAssignment is created. A blocking audit entry records the reason. Fixed mode never falls back.

Preferred-with-fallback first evaluates `preferred_agent_id`. If the preferred Agent is enabled, available, and matches all requirements, it is selected. Otherwise the scheduler selects the highest-ranked eligible alternative and records `fallback_used=true` plus the preferred rejection reason.

Best-available ignores preference and selects the highest-ranked eligible Agent. It requires at least one required capability; without requirements, the task remains ready and the audit states that routing is under-specified.

Manual mode creates no assignment and records an audit stating that human selection is required.

## Eligibility and deterministic ordering

An eligible Agent must be enabled, available, and have one enabled AgentCapability row for every required Capability. External, API, and CLI Agents use the same eligibility rules.

Candidates sort by minimum capability priority descending, total capability priority descending, then Agent ID ascending. This favors Agents without a weak required capability and provides a stable tie-break. The scheduler stores considered candidates, scores, filter reasons, selected Agent, routing mode, and final reason in AuditLog.

## External workstation compatibility

An external Agent selected by capability creates the same ExecutionAssignment as any other worker. Execution continues through the existing task package export/import path. Routing does not modify the external adapter, result validation, Artifact handling, or completion events.

## Error and retry behavior

Missing tasks and malformed references are explicit errors. A task that is not ready is not routed. No eligible candidate leaves the task ready and produces a single idempotent decision audit. Database failures roll back Assignment, task update, Event, and audit together.

The Alpha-1 fallback requirement covers selection-time unavailability. Runtime failure and reassignment history will be a later additive increment because the approved ExecutionAssignment shape has no lifecycle or supersession fields.

## Tests

Tests cover fixed-agent backward compatibility, fixed-agent unavailable blocking, preferred fallback, best-available static priority, deterministic tie-breaking, manual mode, missing requirements, duplicate event retries, decision audit contents, external worker selection, and workflow completion when a preferred Agent is unavailable. All P0 tests must remain green.
