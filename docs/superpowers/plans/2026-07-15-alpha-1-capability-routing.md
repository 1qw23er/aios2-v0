# AIOS Alpha-1 Capability Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic capability-based execution assignment while preserving every P0 fixed-agent and external-workstation workflow.

**Architecture:** Alembic adds normalized capability profiles, Agent status, Task routing requirements, and immutable ExecutionAssignment rows. A standalone scheduler evaluates ready tasks, persists the selected executor, Event, and full routing AuditLog atomically, without changing the Orchestrator graph.

**Tech Stack:** Python 3.12+, SQLModel, SQLAlchemy, Alembic, SQLite, pytest, Ruff.

---

### Task 1: Alpha-1 persistence model and migration

**Files:**
- Modify: `src/aios/models.py`
- Create: `alembic/versions/20260715_0003_capability_routing.py`
- Create: `tests/test_capability_models.py`

- [ ] Write failing tests for Capability uniqueness, AgentCapability priority bounds, Agent status default, Task fixed-mode defaults, and ExecutionAssignment persistence.
- [ ] Run `python -m pytest tests/test_capability_models.py -v`; expect missing model imports.
- [ ] Add `Capability`, compound-key `AgentCapability`, `AgentStatus`, `RoutingMode`, and `ExecutionAssignment`.
- [ ] Extend Agent with status and Task with required_capabilities, preferred_agent_id, and routing_mode while retaining legacy fields.
- [ ] Add migration 0003 with defaults that preserve existing rows (`available`, `fixed`, empty capabilities).
- [ ] Run model tests, full pytest, and Ruff; expect all green.
- [ ] Commit `feat: add capability routing persistence`.

### Task 2: Deterministic scheduler modes

**Files:**
- Create: `src/aios/scheduler.py`
- Create: `tests/test_scheduler.py`

- [ ] Write failing tests for fixed backward compatibility, fixed unavailable blocking, preferred fallback, best-available ordering, manual blocking, and missing requirements.
- [ ] Run scheduler tests; expect missing module.
- [ ] Implement eligibility: enabled Agent, available status, and every enabled required AgentCapability.
- [ ] Rank by minimum priority descending, sum descending, then Agent ID ascending.
- [ ] Implement fixed, preferred_with_fallback, best_available, and manual exactly as approved.
- [ ] Run scheduler tests, full pytest, and Ruff; expect all green.
- [ ] Commit `feat: add deterministic capability scheduler`.

### Task 3: Atomic assignment, audit, event, and retry idempotency

**Files:**
- Modify: `src/aios/scheduler.py`
- Create: `tests/test_scheduler_audit.py`

- [ ] Write failing tests proving one Assignment/Event/Audit row under duplicate idempotency keys and rollback when audit insertion fails.
- [ ] Test AuditLog snapshots include every considered candidate, capability scores, rejection reasons, selected Agent, routing mode, fallback flag, and final reason.
- [ ] Persist ExecutionAssignment, Task.assigned_agent_id, `task.assigned`, and AuditLog in one transaction.
- [ ] Persist one idempotent blocked/manual decision audit without creating Assignment or assignment Event.
- [ ] Run targeted tests, full pytest, and Ruff; expect all green.
- [ ] Commit `feat: audit deterministic routing decisions`.

### Task 4: External-worker compatibility and fallback workflow

**Files:**
- Create: `tests/test_scheduler_external_workflow.py`
- Modify: `README.md`

- [ ] Write a failing test selecting an external Agent by Capability and exporting/importing its existing task package without adapter changes.
- [ ] Write a workflow test where the preferred Agent is unavailable, fallback is assigned, and the P0 task chain still completes.
- [ ] Confirm no Context Engine, Knowledge Engine, dynamic score, reputation, parallel candidate execution, cost optimization, or LLM route code is introduced.
- [ ] Update README with Alpha-1 models, modes, deterministic ordering, AuditLog fields, and exclusions.
- [ ] Run editable install, complete pytest, and Ruff.
- [ ] Commit `test: verify Alpha-1 routing compatibility`.

### Task 5: Review, merge, and delivery

- [ ] Review `git diff` against this plan and the approved design.
- [ ] Verify working tree is clean and all P0 tests remain green.
- [ ] Fast-forward merge the feature branch into main.
- [ ] Re-run editable install, full pytest, and Ruff on main.
- [ ] Push main using the configured GitHub SSH-over-443 origin.
- [ ] Verify local and remote commit hashes match.
