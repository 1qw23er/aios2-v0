# Runtime Layer P1 — Design Review (READ-ONLY)

> Repository: `1qw23er/aios2-v0` · Baseline: `main @ 75b2fb46` (post PR #25 Skill System V1 + PR #26 restore)
> Mode: **Read-only design review.** No source / migration / test / API / DB change, no commit / push / PR.
> Companion to: `skill_system_v1_design_review.md`, `skill_system_v1_implementation_contract.md`.

---

## 1. Executive Summary

The question posed is: *does AIOS need a first-class Runtime registration/adaptation layer so that Claude Code, Codex, DeepSeek Harness and future harnesses can be discovered, health-checked, capability-described and invoked **without intruding** on the existing Task / Execution / DelegatedRun / Governance main chain?*

**Finding from real code: the Runtime layer already exists in AIOS — it is just named `Agent`, not `Runtime`.**

- `Agent` (`models.py:263`) is already a first-class, owner-operable **and** self-registerable execution identity that carries `adapter_type`, `delegation_mode`, `endpoint`, `config_ref`, `secret_ref` (opaque), `capabilities`, `status` (AVAILABLE/UNAVAILABLE/MAINTENANCE), `trust_level`, `timeout_s`, `max_retries`, `platform`, `external_ref`, `bootstrap_token_ref`. This *is* a RuntimeRegistration object — by a different name.
- The adapter seam already exists as two Protocols: `DelegatedAdapter` (`delegation.py:100`) and `WorkerClient` (`worker_contract.py:179`), with `DelegatedExecutionAdapter` (`delegation.py:174`) owning all lifecycle work.
- The run/attempt state machine already exists: `DelegatedRun` (`models.py:464`) + `DelegatedRunStatus` (`models.py:78`) — submit → running → succeeded/failed/cancelled/expired.
- Capability SSoT already exists: `Capability` (`models.py:344`) + `AgentCapability` (`models.py:352`, with `priority` + `enabled`) drives deterministic routing in `route_task`/`_rank` (`scheduler.py:114`/`65`).
- The adapter *selection* seam already exists: `build_execution_adapter` (`adapters/factory.py:22`).

The only genuine gaps vs Multica's "Runtime thin layer" are **two minimal increments**, neither of which requires a new entity:

1. **Liveness metadata** — `Agent.last_heartbeat_at` (computed STALE, not a new state machine).
2. **Adapter selection as a data-driven registry** keyed by `adapter_type`, replacing the hardcoded `if/else` in `build_execution_adapter` — so adding Claude Code / Codex / a new harness is a registry entry, not a factory edit.

**Verdict: GO WITH CONDITIONS.** Do **not** introduce a parallel `Runtime`/`RuntimeRegistration` table or a `runtime_id` FK — that would duplicate `Agent`, violate *Agent identity > Runtime identity*, and risk governance boundary leakage. The P1 work is *Runtime-readiness hardening of the existing Agent seam*.

---

## 2. Current Runtime / Adapter Archaeology

### 2.1 Real call chain (verified, not assumed)

```
Task (required_capabilities, routing_mode, preferred_agent_id)
  └─ route_task(session, task_id, key)                         [scheduler.py:114]
       ├─ _candidate()  : enabled? status==AVAILABLE? AgentCapability match+priority
       ├─ _rank()       : sort (-min_priority, -total_priority, agent_id)   [scheduler.py:65]
       └─ ExecutionAssignment(task_id, selected_agent_id=agent.id)           [models.py:558]
                         ⚠ NO runtime_id column
  └─ execute_task(session, task_id, key, adapter=...)          [execution.py:187]
       ├─ build_context()  → immutable TaskContext (capability projection)
       ├─ adapter = build_execution_adapter(session, task_id)  [adapters/factory.py:22]
       │     reads task.assigned_agent_id → Agent
       │     if AIOS_DEEPSEEK_HARNESS_ENABLED and config_ref startswith "deepseek-harness+file://":
       │         DeepSeekHarnessWorkerClient + WorkerDelegatedAdapter → as_execution_adapter()
       │     else: LLMExecutionAdapter()
       └─ adapter.run(task_context, output_schema, idempotency_key)
             └─ DelegatedExecutionAdapter.run()                [delegation.py:243]
                  ├─ assert_trust_delegable(agent)             [delegation.py:144]  (governance gate)
                  ├─ check_budget(project, est)                [delegation.py:157]  (governance gate)
                  ├─ _create_run() → DelegatedRun(agent_id=...)               [models.py:464]
                  │                  ⚠ NO runtime_id column
                  ├─ submit / status-poll / ingest via WorkerClient (transport seam)
                  ├─ DelegatedRunStatus: SUBMITTED→RUNNING→SUCCEEDED/FAILED/CANCELLED/EXPIRED
                  └─ build_delegated_provenance(agent_id, mode, remote_run_id)   [delegation.py:554]
```

### 2.2 What is already a Runtime seam

| Concept (Multica) | AIOS reality (file:line) | Status |
|---|---|---|
| Runtime registration | `Agent` model (`models.py:263`) — full identity + transport + trust + tuning | **Present (by name `Agent`)** |
| Runtime type / transport | `AdapterType` (`models.py:53`: api/cli/external/model) + `DelegationMode` (`models.py:60`: remote_api/a2a/mcp/workstation) | **Present** |
| Endpoint / transport metadata | `Agent.endpoint`, `config_ref`, `callback_url`, `secret_ref` (opaque) | **Present** |
| Capability declaration | `AgentCapability` → `Capability` (`models.py:352`/`344`); discover-time `WorkerCapabilities` (`worker_contract.py:25`) | **Present** |
| Adapter interface | `DelegatedAdapter` Protocol (`delegation.py:100`); `WorkerClient` Protocol (`worker_contract.py:179`) | **Present** |
| Adapter registry / selection | `build_execution_adapter` (`adapters/factory.py:22`) — **hardcoded if/else** | **Present but not data-driven** |
| Run / attempt state machine | `DelegatedRun` + `DelegatedRunStatus` (`models.py:464`/`78`) | **Present** |
| Execution-level timeout / failure | per-agent `timeout_s` → `EXPIRED` (`delegation.py:466`); `max_retries` backoff | **Present** |
| Availability | `Agent.enabled` (execution availability, `employee_bridge.py:105`) + `Agent.status` AVAILABLE/UNAVAILABLE/MAINTENANCE | **Present (owner-set only)** |
| Self-registration | `create_agent_via_bootstrap` (`agent_registry.py:309`) + `upsert_agent` (`agent_registry.py:442`) + scoped token | **Present** |

### 2.3 What is MISSING (the real P1 delta)

1. **No liveness signal.** `Agent.status` is *owner-set* (enable/disable), not *liveness-detected*. A dead harness is only discovered when a task routed to it times out (`EXPIRED`). There is no `last_heartbeat_at` and no stale computation.
2. **No adapter registry.** `build_execution_adapter` (`adapters/factory.py:22-46`) is a hardcoded `if env-flag and config_ref-prefix` branch. Adding Claude Code / Codex means editing this function — not a registry entry.
3. **Runtime identity is implicit, not first-class.** The "runtime" is the tuple `(adapter_type, config_ref, endpoint)` on `Agent`; multiple agents sharing one substrate cannot be expressed as one named Runtime, and nothing records *which* substrate served a run beyond `agent_id` + `remote_run_id` (which is sufficient for attribution but not for fleet health).

### 2.4 Names that look similar but are NOT the runtime seam

- `pilot2/registration_diff.py` `last_seen_seq` — customer-data registration monotonicity, unrelated to runtime liveness.
- `Event` model (`models.py:542`) — task/domain event bus, not runtime health.
- `AgentSecret` (`models.py:316`) — credential HMAC store, not runtime identity.

---

## 3. Multica Runtime Concepts

Multica proposes: `RuntimeRegistration` (id, type, endpoint, capabilities, status, registered_at, last_heartbeat_at, version) + `Heartbeat` (liveness, stale threshold, status transition) + `Adapter` (interface, registry, capability declaration, versioning). We decompose each into the smallest useful unit and judge against AIOS code.

---

## 4. KEEP / ADAPT / REJECT Matrix

### A. RuntimeRegistration

| Sub-field (Multica) | Judgment | Evidence / reason |
|---|---|---|
| `runtime_id` | **REJECT as separate entity** | `Agent.id` already is the runtime identity (`models.py:266`). A parallel `runtime` table duplicates it. |
| `runtime_type` / `adapter_type` | **KEEP** | `Agent.adapter_type` (`models.py:269`) + `delegation_mode` (`models.py:273`) already encode this. |
| `endpoint` / `transport` | **KEEP** | `Agent.endpoint`/`config_ref`/`callback_url` (`models.py:277-284`). |
| `capabilities` | **KEEP (SSoT)** | `AgentCapability`→`Capability` (`models.py:352`/`344`); `WorkerCapabilities` at discover-time (`worker_contract.py:25`). |
| `metadata` | **KEEP** | `Agent.limitations`, `cost_policy`, `permissions` (`models.py:275-286`). |
| `status` | **KEEP** | `Agent.status` AVAILABLE/UNAVAILABLE/MAINTENANCE (`models.py:211`) + `enabled` (`models.py:285`). |
| `registered_at` | **ADAPT (optional)** | `Agent` has **no `created_at`** (verified lines 266-313). Add a nullable `registered_at` for audit completeness — not required for liveness. |
| `last_heartbeat_at` | **ADAPT (recommended)** | Does **not** exist. Add nullable column to `Agent`. |
| `version` | **KEEP** | `WorkerCapabilities.protocol_version`/`runtime_version` already carried at discover-time (`worker_contract.py:29-30`); no persisted runtime version column needed. |

**Decision for A: ADAPT `Agent`** (add `last_heartbeat_at` + optional `registered_at`). **REJECT** a new `Runtime`/`RuntimeRegistration` table.

### B. Heartbeat

- **ADAPT as availability *metadata*, REJECT as a new execution state machine.**
- AIOS already has execution-level failure/timeout: `DelegatedRunStatus.EXPIRED` (`delegation.py:466`), per-agent `timeout_s`, `max_retries` backoff (`delegation.py:189-209`). A heartbeat must **not** duplicate this state machine.
- Proposed: `last_heartbeat_at: datetime | None` on `Agent`. Staleness is **computed**: `is_stale = (now_utc() - last_heartbeat_at) > STALE_THRESHOLD`. No persisted `HEALTHY/STALE` column; `DISABLED` = existing `enabled=False`.
- Heartbeat participates in selection **only as a pre-filter** (drop stale before `_rank`), never as a new status column.
- Trust boundary: a heartbeat write is scoped to the agent's own credential (`actor.agent_id === target`), identical to the `upsert_agent` scope lock (`agent_registry.py:472-493`).

### C. Adapter

- **KEEP** `DelegatedAdapter` (`delegation.py:100`) and `WorkerClient` (`worker_contract.py:179`) Protocols — they already define `discover/submit/status/cancel/ingest` (+ `events/result/resume`). This is 80%+ of Multica's Adapter surface.
- **ADAPT** `build_execution_adapter` (`adapters/factory.py:22`) → a **registry keyed by `AdapterType`** mapping to a builder. Adding Claude Code / Codex = one registry entry. This is a *minimal* registry (a `dict`), **not** a new framework or Protocol hierarchy.
- **REJECT** further protocol-ization / runtime-side versioning beyond the existing `protocol_version` in `WorkerCapabilities`. The transport seam (`WorkerClient`) is sufficient.

---

## 5. Runtime Boundary

**Runtime layer MAY own (execution substrate metadata + adapter seam):**
- Runtime/agent identity (`Agent.id`), registration (`agent_registry.py`), transport metadata (`endpoint`/`config_ref`/`callback_url`), capability *reference* (`AgentCapability` → `capability_id`), adapter *selection* (`build_execution_adapter` registry), liveness metadata (`last_heartbeat_at`).

**Runtime layer MUST NOT own (governance control plane):**
- Task DAG / `Task` lifecycle — `execute_task` (`execution.py:187`).
- `TaskContext` — `ContextService.build_context` (immutable snapshot; Skill projection added in PR #25).
- Budget — `check_budget` (`delegation.py:157`); `employee_cost.py`.
- Trust — `assert_trust_delegable` (`delegation.py:144`); `AgentTrustLevel` (`models.py:217`).
- Approval / Review — `review.py`, `Approval` (`models.py:505`).
- Artifact governance — `Artifact`, schema validation (`delegation.py:517`).
- Cross-task orchestration — `orchestrator.py`, `scheduler.py`.
- Workforce employment — `Employee`/`EmployeeAgentBinding` (`models.py:2107`/`2231`).
- Billing / cost authority — `employee_cost.py`, `workforce_cost_evidence.py`.

**Definition:** `Runtime = execution-substrate metadata + adapter seam`, a *projection surface beneath `Agent`*, never a governance authority.

---

## 6. Runtime ↔ Agent

- **Agent identity is the SSoT; Runtime is not a separable identity.** `Agent` is already the registered runtime.
- One Agent ⇒ one logical runtime binding today (its `adapter_type`+`config_ref`). Multiple Agents may implicitly share one substrate (same `config_ref`/`endpoint`) but cannot be *grouped* as one named Runtime — acceptable; grouping is a future optional `runtime_ref` tag (metadata only, NOT a routing FK).
- **Dynamic re-binding** `Agent → Runtime` is already possible: owner or self-update changes `config_ref`/`endpoint`/`adapter_type` (`agent_registry.py:500-507`; `upsert_agent`). No new binding model needed.
- REJECT a heavyweight `Agent → Runtime` binding entity; it would duplicate the `Agent` row's own transport fields.

---

## 7. Runtime ↔ Capability

- **Capability is the only SSoT** (frozen by Skill System V1 boundary and `agent_registry.py:251` `_resolve_capabilities` fail-closed on unknown slugs).
- Runtime capability declaration = `WorkerCapabilities` at discover-time (`worker_contract.py:25`) — it *references* capability semantics, it does **not** define vocabulary.
- REJECT any Runtime-owned capability taxonomy. `AgentCapability.capability_id` remains the routing key (`scheduler.py:33-45`).
- Consistency: exactly mirrors Skill System V1's "Capability is the unique Capability boundary" (PR #25, `skill_service.py`).

---

## 8. Runtime ↔ Workforce

- Employment chain is `Employee → EmployeeAgentBinding → Agent` (`models.py:2107`/`2231`). Runtime metadata lives **on `Agent`**.
- Routing already reaches an Agent via capability priority (`_candidate`/`_rank`); the runtime is resolved *after* assignment at `build_execution_adapter` time.
- REJECT `Employee → Runtime` (would bypass `Agent` identity and the `employee_bridge.py:98` `_check_active` gate).
- Future Squad form: `Team → Employees → Agents → (Agent's runtime metadata)` and `Team → required capabilities → routing → Agent → adapter`. No Runtime FK in the employment model.

---

## 9. Runtime ↔ DelegatedRun / ExecutionAssignment / Task

**Recommendation: add `runtime_id` to NONE of them.**

- `DelegatedRun.agent_id` (`models.py:481`) and `ExecutionAssignment.selected_agent_id` (`models.py:563`) already pin the Agent — and the Agent carries the runtime tuple. Adding `runtime_id` would:
  - break **immutable assignment** semantics (an Agent's runtime binding could change underneath a historical run);
  - complicate **retry / idempotency** — `idempotency_key = H(task_id, agent_id, attempt)` (`delegation.py:53`); a `runtime_id` in the key would shift on re-route;
  - create **governance boundary leakage** (runtime becomes a first-class provenance/routing dimension);
  - add a **new routing state** outside `_rank`.
- Runtime resolution stays at `build_execution_adapter` time (`adapters/factory.py:22`), **not persisted to the run**. Provenance is already complete via `build_delegated_provenance` (`delegation.py:554`: `agent_id`, `mode`, `remote_run_id`).

---

## 10. Lifecycle

- REJECT the Multica `REGISTERED → HEALTHY → STALE → DISABLED` persisted state machine.
- ADAPT minimal: **`registered + last_heartbeat_at + disabled`**.
  - `registered` = `Agent` row exists (optionally `registered_at`).
  - `last_heartbeat_at` = nullable; `STALE` computed (`now - last_heartbeat_at > STALE_THRESHOLD`).
  - `disabled` = existing `enabled=False` (`agent_registry.py:202` `set_agent_enabled`).
- Identity immutable: `Agent.id` is minted once (`new_id("agt")`, `models.py:266`); never reassigned.
- Deletion: owner may disable (soft) per existing pattern; hard-delete only via owner, out of scope for P1.

---

## 11. Deterministic Selection

- Current determinism is solid (`scheduler.py`):
  - base order `select(Agent).order_by(Agent.id)` (`scheduler.py:183`);
  - `_candidate` filters `enabled` + `status==AVAILABLE` + `AgentCapability` enabled/priority (`scheduler.py:29-45`);
  - `_rank` sorts by `(-minimum_priority, -total_priority, agent_id)` (`scheduler.py:65-73`) — fully deterministic, `agent_id` is the stable final tie-break.
- Heartbeat, if adopted, is a **pre-filter** applied *before* `_rank`: drop agents whose `last_heartbeat_at` is stale. Among the surviving healthy set, ordering is unchanged (`-min_priority, -total_priority, agent_id`).
- Wall-clock is used **only** for the staleness cutoff (a uniform `now_utc()` comparison), never for ordering. No random selection, no unordered DB result, no implicit priority.
- REJECT health-aware *scoring* (would make selection non-deterministic / wall-clock-dependent). Filter-only is the rule.

---

## 12. Persistence Proposal

**No new table.** Extend `Agent` (`models.py:263`):

| Column | Type | Nullable | Immutable? | Notes |
|---|---|---|---|---|
| `last_heartbeat_at` | `datetime` | yes (NULL = unknown) | mutable | liveness metadata; STALE computed |
| `registered_at` | `datetime` | yes (backfill NULL) | immutable | optional audit completeness (Agent has no `created_at` today) |

- FK: none added. No `runtime` table, no `runtime_id` anywhere.
- Indexes: `last_heartbeat_at` index optional (only if staleness queries are hot); not required for P1.
- Migration: one additive migration (two nullable columns). Aligns with the repo convention of single-head migrations (PR #25 added `20260907_0001_skill_system`).
- Deletion semantics: unchanged (owner disable / delete).

---

## 13. API Proposal (minimal)

Reuse the existing agent-registry routes (`agent_registry.py`); add **one** endpoint:

- `POST /agents/{id}/heartbeat` — updates `last_heartbeat_at = now_utc()`. **Scoped**: caller must be the agent's own credential (`actor.kind=="agent"` and `actor.agent_id == id`), same scope lock as `upsert_agent` (`agent_registry.py:472-493`). Owner may also call (for manual liveness).
- Explicitly **NOT** added: Runtime CRUD, Runtime list/get/disable as separate entities — those are the Agent endpoints already present.
- Rationale per endpoint: "why does the orchestration control plane need it?" — heartbeat exists so a dead harness is detected *before* a task is routed to it (proactive vs. the current reactive `EXPIRED` timeout). No other Runtime endpoint clears that bar.

---

## 14. Security / Trust Boundary

- **No new auth system.** Reuse the existing model:
  - `secret_ref` stays an opaque handle (`models.py:283`); value never persisted (`agent_registry.py:9-13`).
  - `trust_level` single axis gates delegation (`delegation.py:141`, `agent_registry.py:55`).
  - Self-registration uses scoped bootstrap tokens (`agent_registry.py:309`); heartbeat reuses the agent's own bearer (`AgentSecret`, `models.py:316`).
- Agent may only heartbeat **its own** row (scope lock). Arbitrary-agent Runtime self-registration is **rejected** — there is no "register a Runtime" path; the agent *is* the runtime, and its registration is already gated by bootstrap tokens.
- Governance gates (trust/budget/least-privilege projection) stay in `DelegatedExecutionAdapter.run()` (`delegation.py:243-282`); the Runtime layer adds **zero** authority.

---

## 15. Skill System Boundary

- Runtime does **not** own Skill (`skill_service.py`), Knowledge (`knowledge_service.py`), or `TaskContext`.
- Runtime does **not** execute Skill directly. Skill selection is `ContextService._select_skills` (capability intersection → `TaskContext.applicable_skills`), executed at context-build time; runtime is selected later at `build_execution_adapter` time.
- Ideal chain preserved: `Task → Capability → (Skill projection for context) + (Agent selection for execution) → DelegatedRun`. Runtime sits *beneath* Agent as transport metadata; it never crosses into the Skill/Capability SSoT.
- `WorkerCapabilities` (discover-time) references capability semantics only — consistent with the Skill contract's "Capability is the unique SSoT".

---

## 16. Workforce Team / Squad Dependency

- Analyzed, not implemented. `Runtime P1` is a **soft** prerequisite for a future Multica-style Squad (Squad would route `required capabilities → agents → runtime`), but **not a hard blocker** — Squad can be built on the existing `Agent` + `AgentCapability` model without a Runtime FK.
- P1 must be designed so Squad later reuses `Agent` + `AgentCapability` directly (Condition C8). No Squad entity is introduced here.

---

## 17. Multica §11 Updated Decision Matrix

| Item | Multica classification | AIOS current | Decision | Priority |
|---|---|---|---|---|
| Skill | P0 | DONE (PR #25/#26) | BORROWED | — |
| Runtime | P1 | `Agent` absorbs ~80%; gaps = liveness + adapter registry | **ADAPT (extend `Agent` + registry; REJECT new entity)** | **P1** |
| Workforce Team | P1 | W1–W8 present, not Multica Squad dynamic form | DEFER / ADAPT-later (not blocked by Runtime) | P1 |
| Autopilot | P2 | NOT DONE (only `coze.py` webhook comment) | DEFER | P2 |
| Daemon | IGNORE | NOT DONE | IGNORE | — |
| Human board | IGNORE | NOT DONE | IGNORE | — |

---

## 18. Non-Goals

- No new `runtime` / `RuntimeRegistration` table.
- No `runtime_id` FK on `DelegatedRun` / `ExecutionAssignment` / `Task` / `Artifact`.
- No persisted heartbeat state machine.
- No Runtime-owned capability vocabulary.
- No Runtime intrusion into Workforce employment (`Employee → Agent` unchanged).
- No Runtime executing Skill / mutating `TaskContext`.
- No arbitrary-agent self-registration of a separate Runtime entity.
- No scheduler / execution-engine / `route_task` / `DelegatedRun` redesign.

---

## 19. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| "Adapter registry" quietly becomes a framework | Med | Condition C5: registry is a `dict[AdapterType, builder]`; no new Protocol hierarchy; `WorkerClient` stays the seam. |
| Heartbeat degenerates into a routing score | Med | Condition C6: heartbeat is a pre-filter only; `_rank` unchanged. |
| Adding `last_heartbeat_at` tempts future `runtime_id` on runs | Low | Condition C2 freezes no FK on runs. |
| Stale threshold mis-tuned → healthy agents dropped | Low | `STALE_THRESHOLD` configurable; filter is conservative (only drops when explicitly stale). |
| New columns widen `Agent` migration surface | Low | Two nullable columns, additive, single-head migration. |

---

## 20. Recommendation

**GO WITH CONDITIONS.** AIOS already possesses a Runtime layer under the name `Agent` + `DelegatedAdapter`/`WorkerClient` + `build_execution_adapter` + `DelegatedRun`. The faithful, minimal P1 is **Runtime-readiness hardening of the existing seam**, not the creation of a Multica-shaped Runtime entity:

1. Add `Agent.last_heartbeat_at` (+ optional `registered_at`) — liveness metadata, STALE computed.
2. Convert `build_execution_adapter` into an `AdapterType`-keyed registry — extensibility without factory edits.
3. Add `POST /agents/{id}/heartbeat` (own-credential scoped).

Net change to the governance main chain: **zero**. The change is contained to the Agent model (2 nullable columns), the adapter factory (registry refactor, behavior-preserving), and one scoped endpoint.

---

## 21. Implementation Contract Readiness

**Ready to freeze an Implementation Contract** under the following mandatory conditions:

- **C1** — No new `runtime` table; Runtime-readiness added to `Agent` only.
- **C2** — No `runtime_id` FK on `DelegatedRun` / `ExecutionAssignment` / `Task` / `Artifact`; runtime resolution stays at `build_execution_adapter` time.
- **C3** — `last_heartbeat_at` is metadata; STALE computed (`now - last_heartbeat_at > STALE_THRESHOLD`); no persisted status; `disabled` = existing `enabled=False`.
- **C4** — Capability SSoT unchanged; `WorkerCapabilities` references `capability_id` only; Runtime never owns capability vocabulary (consistent with Skill System V1).
- **C5** — Adapter registry keyed by `AdapterType` enum → builder; no new Protocol hierarchy; `WorkerClient` remains the transport seam.
- **C6** — Deterministic selection preserved: heartbeat is a pre-filter before `_rank`; tie-break remains `(-min_priority, -total_priority, agent_id)`; wall-clock only for the staleness cutoff.
- **C7** — Governance unchanged: trust/budget/least-privilege/opaque-secret gates stay in `DelegatedExecutionAdapter.run()` / `ContextService`; Runtime adds zero authority.
- **C8** — Workforce untouched: `Employee → EmployeeAgentBinding → Agent` unchanged; Runtime metadata lives on `Agent`.
- **C9** — API minimal: extend agent-registry routes; add only `POST /agents/{id}/heartbeat` (own-credential scoped); no Runtime CRUD.
- **C10** — Heartbeat trust: agent may only write its own `last_heartbeat_at` (scope lock identical to `upsert_agent`).

**Pre-conditions for contract execution:** head-pin audit (every new migration must advance the alembic head-pin in the suite, per PR #25 convention); ruff clean; targeted Skill + routing + agent-registry regression green; full pytest once at the end (current baseline `20 failures / 1433 passes`, all non-Skill, per PR #25/#26 closure).

---

*End of review. No code, migration, test, API, or DB change was made; no commit/push/PR performed.*
