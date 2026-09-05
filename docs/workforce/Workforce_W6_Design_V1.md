# Workforce W6 Design V1 — D-1 (Budget / Cost Evidence Source Event) & D-4 (Employee Lifecycle)

**Status**: DESIGN ONLY — no implementation, no migration, no PR, no push.
**Base (exact)**: `fedd38819d5a57798fd31fd8a3691c30a60b5c21` (main tip after PR #14 / W5 Squash merge)
**Branch**: `docs/w6-design`
**Scope**: resolve (or explicitly defer) the two unresolved decisions carried out of W5 — **D-1** (Budget / Cost Evidence 真实 source event) and **D-4** (Employee 删除 / 生命周期语义).
**Method**: repo-evidence driven. Every factual claim carries a `file:line` pointer. Anything not derivable from the repo is tagged `[ASSUMPTION]`. Anything requiring a business decision is tagged `[DECISION REQUIRED]` with a stable ID. No inference is presented as fact. No W7/W8 implementation is designed here.

---

## 1. Executive Summary

### 1.1 What this document does

W5 shipped a **bookkeeping-only** `cost_evidence` table plus a **dormant writer** with no caller, and explicitly deferred two decisions: D-1 (where a real cost source event comes from) and D-4 (what happens to an `Employee` over its lifetime). This document closes the *design* half of both questions by reading the repository as it exists at `fedd388`, and deliberately leaves the *business* half open for owner decision.

### 1.2 Headline conclusions

| # | Conclusion | Basis |
|---|---|---|
| C-1 | **The repo contains no Workforce-attributable cost source event.** `record_cost_evidence` is therefore correctly caller-less in V1, and W6 must not invent an event to "close the loop". | `src/aios/workforce_cost_evidence.py:1-30` module docstring; `src/aios/models.py:276` (`cost_policy` schema-less JSON); `src/aios/models.py:1728+` (`BenchmarkResult` has no cost column); `src/aios/workforce.py:1251+` (`_DefaultBenchmarkAdapter` returns `trusted=False`, no execution) — see §3.1 |
| C-2 | **The only realized cost in the repo belongs to the Delegation/Project domain** (`DelegatedRun.cost`, bound `Task → Project`, referencing no Workforce row). Reusing `delegated_run.id` as a Workforce source event is explicitly forbidden. | `src/aios/models.py:466-495` (`DelegatedRun`: `project_id`, `task_id`, `agent_id`, `cost`, `usage`; no employee/job/candidate FK); `src/aios/workforce_cost_evidence.py:14-18` |
| C-3 | **Budget authority stays in Delegation.** `check_budget` gates *before* remote execution; `_accrue_budget` is the only writer of `Project.budget_used`. Workforce must never read or write it. | `src/aios/delegation.py:157`, `:282`, `:476-491`; `src/aios/models.py:244-257` |
| C-4 | **`cost_evidence` must NOT get a `Project` FK in W6.** The entire Workforce chain has zero `project_id`; adding one would invert the dependency direction and couple Workforce lifecycle to Project deletion. | `grep project_id src/aios/models.py` restricted to lines 1370–2200 → **no matches**; `src/aios/models.py:533-548` (`Event.project_id` is NOT NULL — project-scoping is a Delegation-side invariant) |
| C-5 | **D-4: `Employee` is permanent by construction, not by accident** — one status member (`ACTIVE`) with zero outbound edges, no terminate/purge writer, all lineage FKs `RESTRICT`, `uq_employee_trial` uniqueness. W6 keeps it permanent. | `src/aios/models.py:1996` (docstring: "V1 has exactly ONE member … zero outbound edges"); `src/aios/models.py:2083-2179` (RESTRICT lineage); `src/aios/workforce_employee.py:329` (only writer, no delete path) |
| C-6 | **Logical termination is a `[DECISION REQUIRED]`, not a design conclusion.** The repo has no consumer of Employee lifecycle state beyond `CostEvidence.employee_id`; nothing in the repo *requires* termination. If approved, it is a **zero-migration** change (`employee.status` is a plain `sa.String()`). | `src/aios/models.py:1996-2010`; §4.3 |
| C-7 | **Physical purge of `Employee` is forbidden in W6.** A future controlled archive/purge is *possible* but gated on four preconditions, modelled on the single existing purge precedent (`purge_recommendation`). | `src/aios/workforce_recommendation.py:667-735`; §4.6 |
| C-8 | **"API 层统一映射 409" cannot be evidenced today**: Workforce has *no* HTTP surface at all, and `_translate()` passes `ServiceError.status_code` through verbatim. 409 is currently a **service-layer** decision. | `grep` of `src/aios/api/*.py` for `/workforce`, `/jobs`, `/employees`, `/trials` → zero; `src/aios/api/app.py:301` (`_translate`) |
| C-9 | **W6 requires no FK / ON DELETE change and no migration for D-4 status.** Every W6 schema change, if approved, is additive-only and must preserve the single Alembic head `20260904_0001_workforce_cost_evidence`. | `alembic/versions/20260904_0001_workforce_cost_evidence.py` (additive + reversible `downgrade()`); §12 |

### 1.3 The one-sentence answer to each deferred decision

- **D-1**: there is no legal source event in the repo today, so W6's job is to *publish an admission contract* for a future producer (§8) and to **freeze** `cost_evidence` — not to fabricate a producer.
- **D-4**: `Employee` stays permanent; termination is an optional, zero-migration, reversible-in-design addition that only proceeds on owner approval; purge stays forbidden.

### 1.4 Deliberately NOT in this document

W6 does not design: a Budget reservation engine, a scheduler, an execution backend, a Workforce API surface, a benchmark runner that produces measured cost, or any W7/W8 artefact. See §15.

---

## 2. Current Repo Evidence (as of `fedd388`)

Every row below was read directly from the working copy at base `fedd388`.

### 2.1 Schema evidence

| Evidence | Location | Fact |
|---|---|---|
| Workforce table set (13) | `src/aios/models.py:1403-2200` | `business_goal`, `required_work`, `job`, `job_version`, `capability_requirement`, `candidate`, `benchmark`, `benchmark_version`, `benchmark_result`, `match`, `recommendation`, `trial`, `employee`, `cost_evidence` (14 with `cost_evidence`) |
| Zero `project_id` in Workforce chain | `grep -n project_id src/aios/models.py` filtered to lines 1370–2200 | **no matches** — the chain is rooted at `business_goal`, not `project` |
| `Project.budget_limit` / `budget_used` | `src/aios/models.py:244-257` | `budget_limit: float = 0.0`, `budget_used: float = 0.0`; comment: "running spend accrued by delegated runs … HARD-block remote execution (0.0 limit = no enforcement)" |
| `Task.estimated_cost` / `actual_cost` | `src/aios/models.py:361-390` | estimate lives on `Task`; `actual_cost: float = 0.0` |
| `DelegatedRun` | `src/aios/models.py:454-495` | `project_id` FK, `task_id` FK, `agent_id` FK, `cost: float = 0.0`, `usage` JSON, unique `idempotency_key`. **No** employee/job/candidate/trial/job_version reference |
| `Agent.cost_policy` | `src/aios/models.py:276` | `cost_policy: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))` — free-form JSON, no schema |
| `BenchmarkResult` | `src/aios/models.py:1728-1783` | `passed_cases`, `total_cases`, `quality_score` (all nullable), `status` (`RECORDED`/`UNKNOWN`). **No cost column** |
| `EmployeeStatus` | `src/aios/models.py:1996-2010` | docstring "V1 has exactly ONE member -- ACTIVE -- and therefore zero outbound edges … no TERMINATED / ON_LEAVE / SUSPENDED member, because V1 has no writer for any of them (D-4 …)". Member: `ACTIVE = "active"`; plain `sa.String()` → zero-migration |
| `Trial` parents | `src/aios/models.py:2014-2034` | all three parent FKs `RESTRICT` |
| `Employee` parents + uniqueness | `src/aios/models.py:2083-2179` | `candidate_id` / `trial_id` / `job_id` / `job_version_id` all `RESTRICT`; `agent_id` `NO ACTION`; `uq_employee_trial` unique on `trial_id`; `hired_at`; `status` default `ACTIVE` |
| `CostEvidence` | `src/aios/models.py:2181-2210` | `job_version_id` RESTRICT + index; `employee_id` nullable RESTRICT + index; `amount` nullable float; `source_event_type` / `source_event_id` NOT NULL (no default); `idempotency_key` NOT NULL unique + index; `recorded_at`; `note` nullable. id prefix `ce` |
| Generic outbox `Event` | `src/aios/models.py:532-548` | `project_id` FK **NOT NULL**, `task_id` nullable, `type`, `payload`, unique `idempotency_key`, `status` (`PENDING`/`PROCESSED`) |
| `AuditLog` | `src/aios/audit.py:59-72` | `actor`, `action` (indexed), `resource_type`, `resource_id`, `project_id`/`task_id` nullable, `before_snapshot`, `after_snapshot`, unique `idempotency_key`, `created_at` |

### 2.2 Service / writer evidence

| Evidence | Location | Fact |
|---|---|---|
| `record_cost_evidence` signature | `src/aios/workforce_cost_evidence.py:~60-95` | `(session, *, job_version_id, amount, source_event_type, source_event_id, actor, employee_id=None, note=None)` |
| Owner-only | `workforce_cost_evidence.py:96` | `_assert_owner_actor(actor)` — no default actor |
| Source-event identity guard | `workforce_cost_evidence.py:99-106` | empty `source_event_type` or `source_event_id` → `ServiceError(422, "…cost evidence must bind a real source event, never a fabricated one")` |
| Measured-amount guard | `workforce_cost_evidence.py:108-109` | `amount is None` → `ServiceError(422, "amount is required: only measured costs are recorded")` |
| Fail-closed parents | `workforce_cost_evidence.py:111-113` | `_load_job_version` → 404; `_load_employee` when given → 404 |
| Idempotency key | `workforce_cost_evidence.py:115` | `f"{source_event_type}:{source_event_id}"` |
| Single savepoint | `workforce_cost_evidence.py:127-145` | `session.begin_nested()` wraps evidence insert + `append_audit`; replay's `IntegrityError` propagates, nothing absorbed |
| Audit params | `workforce_cost_evidence.py:136-143` | `action="cost_evidence.create"`, `resource_type="cost_evidence"`, `project_id=None`, `task_id=None`, same `idempotency_key` |
| **No source-event existence check** | `workforce_cost_evidence.py:99-113` | the writer validates only that the identity strings are **non-empty**; it never verifies that a row with that id exists in the producer's table |
| `promote_to_employee` | `src/aios/workforce_employee.py:329-432` | only `Employee` writer; 409 on non-COMPLETED trial; 409 on non-TRIALING candidate; snapshots `agent_id/job_id/job_version_id` from Candidate (F-E19); idempotent replay by `trial_id`; `IntegrityError` → adopt winner row |
| `release_candidate` | `src/aios/workforce_employee.py:433+` | 409 "trial is not releasable" (F-E23) |
| Single purge precedent | `src/aios/workforce_recommendation.py:667-735` | `purge_recommendation`: owner-only, 404 missing, **409 unless WITHDRAWN/REJECTED**, full-column audit snapshot (`action="recommendation.deleted"`, `project_id=None`, `task_id=None`, `idempotency_key=f"rec:{rec.id}:purge:{actor.owner_id}"`), then `session.delete` inside `begin_nested()` |
| All `session.delete` sites in `src/` | grep (excluding `api/app.py`) | exactly 3: `agent_registry.py:296`, `context_retention.py:37`, `workforce_recommendation.py:729`. **None in the Employee/Job/Trial/Candidate path** |
| Cost advisory | `src/aios/workforce_recommendation.py:128+` | reads `agent.cost_policy`, emits advisory **text naming policy keys only, never a value** (F-R5) |
| Evaluation context | `src/aios/workforce.py:905-945` | emits `"cost_evidence": {"status": "unknown", "reason": "Agent.cost_policy has no defined schema (W5 Budget domain)"}` |
| Benchmark execution | `src/aios/workforce.py:1251-1325` | `_DefaultBenchmarkAdapter.run` → `trusted=False`, all scores `None` ("V1 placeholder: no execution backend exists yet") |
| `run_benchmark` | `src/aios/workforce.py:1327+` | idempotent on `(candidate_id, benchmark_version_id, run_id)`; fail-closed writes `status="unknown"` |

### 2.3 Budget authority evidence

| Evidence | Location | Fact |
|---|---|---|
| `BudgetExceededError` | `src/aios/delegation.py:131` | subclass of `DelegatedExecutionError` |
| `check_budget` | `src/aios/delegation.py:157-172` | returns immediately when `project.budget_limit <= 0.0`; else `budget_used + estimated_cost > budget_limit` → raise. **Reads** `budget_used`, never writes |
| Call site | `src/aios/delegation.py:282` | invoked inside `DelegatedExecutionAdapter.run` (def at `:243`), gated on `ctx_project_id is not None`, `est = Task.estimated_cost`, **before** any remote call / secret resolution (#104 hardening gate) |
| `_accrue_budget` | `src/aios/delegation.py:476-495` | reads persisted `DelegatedRun.cost`; `if cost <= 0.0: return`; else `project.budget_used = float(project.budget_used) + cost`. "We do NOT decrement on failure (no charge for an unsuccessful delegation)" |
| **Only writer of `budget_used`** | `grep -rn budget_used src/` | writes: `delegation.py:491` only. Reads/comparisons: `delegation.py:161,165,169`. Model declaration: `models.py:257`. Workforce mentions: `workforce_cost_evidence.py:9,92` (both docstring-level prohibitions) |
| No reservation writer | grep for reservation/commit semantics in `src/` | **no reservation or hold mechanism exists**; `budget_used` is post-hoc accrual only |

*Repo observation (not a defect claim)*: `src/aios/delegation.py:263-282` contains a duplicated "2. Budget" comment block. Noted for hygiene only; out of W6 scope.

### 2.4 API / error-translation evidence

| Evidence | Location | Fact |
|---|---|---|
| `ServiceError` | `src/aios/services.py:~30-49` | `status_code: int`, `detail: str`, `untrusted: bool = False` (owner-facing surfaces must render byte-identical bodies — no enumeration oracle) |
| `_translate` | `src/aios/api/app.py:301` | `return HTTPException(status_code=error.status_code, detail=error.detail)` — **verbatim passthrough; no 409 coalescing** |
| Workforce routes | grep `/workforce`, `/jobs`, `/employees`, `/trials` in `src/aios/api/*.py`, `src/aios/console.py` | **zero matches** → Workforce has no HTTP surface at base `fedd388` |
| Delete endpoints | grep `@app.delete` / `@router.delete` | **zero** in the repo |
| Only 409 coercion found | `src/aios/api/app.py:2343` | `status_code = 400 if error.status_code == 400 else 409`, in the owner-home `launch_campaign` HTML handler — unrelated to Workforce |

### 2.5 Test / migration evidence

| Evidence | Location | Fact |
|---|---|---|
| W5 test suite | `tests/test_workforce_cost_evidence_w5.py` (20 tests) | includes `test_writer_structurally_excluded_from_budget_machinery` (:406), `test_writer_has_no_caller_in_v1` (:431), `test_record_is_replay_safe_at_most_once` (:459), `test_record_trails_audit_in_same_transaction` (:535), `test_deleting_job_version_with_evidence_is_blocked` (:573), `test_deleting_employee_with_evidence_is_blocked` (:598), `test_employee_lifecycle_unchanged_single_active_state` (:623), `test_v1_population_is_zero_without_a_caller` (:643) |
| W4 test suite | `tests/test_workforce_employee_w4.py` (~36 tests) | owner-only 403s, 404s, 409 illegal transitions, idempotent promote, `test_promote_does_not_write_job_filled` (:690), `test_alembic_single_head_is_w4_employee` (:784) |
| Alembic head | `alembic/versions/20260904_0001_workforce_cost_evidence.py` | single head; `down_revision = 20260903_0002_workforce_employee`; additive-only (new table + 3 indexes); reversible `downgrade()` |
| W5 design doc | not on `main` | `docs/workforce/` at `fedd388` contains W3A/W3B/W3C/W3D/W4 artefacts only; no `Workforce_W5_Design_V1.md` (it lives on the unmerged branch `docs/w5-design` @ `7b77df1`). W6 must therefore restate any W5 rule it depends on |

---

## 3. D-1 — Budget / Cost Evidence 真实 Source Event

> **Honesty constraint (carried from W5, `workforce_cost_evidence.py:1-30`)**: do not fabricate a source event in order to produce a closed loop. If the repo has no such event, the design says so.

### 3.1 Q1 — Workforce 第一个真实成本源事件最合理是什么？

**Repo answer: 当前 repo 中不存在任何 Workforce 可归因的成本源事件。** Candidate classes and why each fails *today*:

| Candidate | Would it be Workforce-attributable? | Repo evidence | Verdict today |
|---|---|---|---|
| **A. Benchmark / evaluation execution** | Yes — attributable to `candidate_id` → `job_version_id` | `BenchmarkResult` has **no cost column** (`models.py:1728-1783`); `_DefaultBenchmarkAdapter` returns `trusted=False` with all-`None` scores (`workforce.py:1251-1325`); `run_benchmark` writes `status="unknown"` (`workforce.py:1327+`) | **Not realizable** — no execution backend, no measurement. Would require an additive cost column + a real runner ⇒ new capability, not a W6 decision |
| **B. Trial execution** | Yes — attributable to `trial_id` → `job_version_id`, and post-hire to `employee_id` | `Trial` has only lifecycle columns (`models.py:2014-2034`); `activate_trial`/`complete_trial`/`cancel_trial` record no usage or spend (`workforce_employee.py:181-328`) | **Not realizable** — no usage metering exists |
| **C. Delegated run executed on behalf of a Workforce job** | Ambiguous — the spend is realized, but its anchor is `task_id`/`project_id` | `DelegatedRun` (`models.py:466-495`) has `project_id`, `task_id`, `agent_id`, `cost`, `usage`; **no** Workforce FK anywhere. Adopting it would require a Workforce→Delegation reference that does not exist and would re-anchor a delegation-domain fact | **Forbidden as a stand-in** (`workforce_cost_evidence.py:14-18`: "`delegated_run.id` is never accepted as a stand-in for a Workforce-attributable event") |
| **D. External procurement / manually attested spend** (e.g. owner-attested subscription or invoice line) | Yes — if attested by an owner with a real external document id | No writer, no table, no API exists. Would be a *new* source event type introduced by a future stage | **Not present** — must be introduced deliberately, not assumed |
| **E. `Agent.cost_policy` derived estimate** | No | `cost_policy` is schema-less JSON (`models.py:276`); `workforce.py:905-945` explicitly emits `cost_evidence.status = "unknown"` because "cost_policy has no defined schema"; `_build_cost_advisory` names policy **keys only, never a value** (`workforce_recommendation.py:128+`, F-R5) | **Explicitly inadmissible** — an estimate is not a measurement (W5 I6) |

**Conclusion**: Q1 cannot be answered with an existing event. The *design* answer is a **contract**, not a choice: any future event must satisfy the admission rules in §8. The *business* answer — which event is first — is `[DECISION REQUIRED] DR-D1-1`.

**Design recommendation (non-binding)**: the structurally cheapest first real event is **A (benchmark/evaluation execution)**, because it already carries a Workforce anchor (`candidate_id → job_version_id`) and an execution slot (`run_benchmark`, `workforce.py:1327`). But it requires a real execution backend and a cost column — i.e. a *capability* that does not exist. Labelled `[ASSUMPTION] A-1` and gated on DR-D1-1.

### 3.2 Q2 — 该事件属于 Workforce 还是 Delegation/Project？

**Attribution test (design rule, derived from the FK topology):**

| Anchor present on the event | Owning domain | Rationale |
|---|---|---|
| `job_version_id` (and optionally `employee_id`) | **Workforce** | `CostEvidence.job_version_id` is RESTRICT-anchored to `job_version` (`models.py:2185-2187`); the Workforce chain is rooted at `business_goal` and has zero `project_id` |
| `project_id` / `task_id` / `delegated_run_id` | **Delegation / Project** | `DelegatedRun` is bound `Task → Project` (`models.py:466-495`); `check_budget`/`_accrue_budget` operate on `Project` (`delegation.py:157,476`) |
| Both | **Conflict — must be resolved before recording** | Recording the same spend in both domains double-counts. `Project.budget_used` is the authoritative ledger for delegation spend; `cost_evidence` is a non-authoritative fact log for Workforce spend |

**Design rule B-1 (no double count)**: a single unit of spend is recorded **exactly once**, in the domain that owns its anchor. `cost_evidence` must never duplicate a spend already accrued into `Project.budget_used`.

**Design rule B-2 (no cross-domain adoption)**: Workforce must not adopt a delegation-domain id as its own source event, even if the same money is involved. If a Workforce job is executed *through* delegation, the two records are related by *correlation*, not by FK.

**Open**: whether a future Workforce execution should be routed through delegation (and thus inherit its budget gate) or own its own execution path — `[DECISION REQUIRED] DR-D1-2`.

### 3.3 Q3 — source event 应由哪个阶段产生？

**Design rule B-3 (producer owns the measurement)**: the stage that *owns the execution* is the only stage that may produce a cost source event, because it is the only stage that observes a measured amount. `cost_evidence` is a **consumer**, never a producer, and never an estimator.

Repo evidence supporting B-3:
- `_DefaultBenchmarkAdapter` deliberately returns `trusted=False` rather than inventing a score (`workforce.py:1251-1325`) — the repo's established pattern is *refuse to fabricate a measurement*.
- `run_benchmark` is already the execution-shaped entry point with a natural idempotency key (`candidate_id`, `benchmark_version_id`, `run_id`) (`workforce.py:1327+`).
- `_accrue_budget` in delegation reads a **persisted** `DelegatedRun.cost` rather than recomputing (`delegation.py:476-495`) — same producer-owns-measurement pattern.

**Design rule B-4 (producer ≠ consumer, no self-attestation)**: the producer must pass a **measured** `amount`; it must never pass an estimate, a policy-derived number, or a default. `Agent.cost_policy` output is advisory text only (F-R5) and is inadmissible as `amount`.

**Conclusion**: no current stage can legally produce the event (all candidates in §3.1 fail). The producing stage is a future capability → `[DECISION REQUIRED] DR-D1-3`.

### 3.4 Q4 — `cost_evidence` 如何合法消费该事件？

The consumption contract is **already fully specified by W5** (`workforce_cost_evidence.py:96-147`). W6 adds no new writer semantics; it only pins the *pre-conditions the caller must satisfy*:

| # | Pre-condition | Enforced today? | Source |
|---|---|---|---|
| P-1 | `actor` is an owner (`_assert_owner_actor`) | Yes (403 / TypeError) | `:96` |
| P-2 | `source_event_type` and `source_event_id` are non-empty | Yes (422) | `:99-106` |
| P-3 | `amount is not None` (measured, never estimated) | Yes (422) | `:108-109` |
| P-4 | `JobVersion` exists | Yes (404) | `:111` |
| P-5 | `Employee` exists when `employee_id` given | Yes (404) | `:112-113` |
| P-6 | at-most-once via UNIQUE `idempotency_key = f"{type}:{id}"` | Yes (IntegrityError propagates) | `:115`, `:127-145`; `models.py:2205` |
| P-7 | evidence + audit in one savepoint | Yes | `:127-145` |
| P-8 | `project_id=None`, `task_id=None` on the audit row | Yes | `:139-140` |
| **P-9** | **the referenced source event row actually exists** | **NO** — the writer only checks non-empty strings (`:99-106`) | gap found in this review |

**P-9 is a real gap, not a defect to fix now.** Today, because the writer has no caller (`test_writer_has_no_caller_in_v1`, `:431`), P-9 cannot be violated. Once a producer exists, a caller could pass a well-formed but non-existent `source_event_id` and create an unverifiable fact. Two candidate designs:
- **(a) Generic**: register a per-`source_event_type` dispatch table mapping type → (model, table) and existence-check before insert. Cost: a new registry module; every new event type must register.
- **(b) Producer-side only**: trust the producer (it just wrote the row in the same transaction) and document P-9 as a caller obligation, enforced by review/tests.

→ `[DECISION REQUIRED] DR-D1-4`. Default if unanswered: **(b)**, because (a) introduces a cross-module registry that no current code needs, and W5's own tests already assert "no caller".

**Design rule B-5 (no back-fill)**: `cost_evidence` rows are append-only facts about a past measurement. There is no update path in W5 and W6 must not add one; a correction is a *new* row with a new source event id, or nothing.

### 3.5 Q5 — 是否需要 Project FK？

**No.** Evidence and reasoning:

1. The Workforce chain has zero `project_id` (`models.py` lines 1370–2200 grep → no matches). Adding one to `cost_evidence` would make it the *only* project-coupled Workforce row.
2. The repo's generic outbox already encodes "events are project-scoped" (`Event.project_id` **NOT NULL**, `models.py:535`). Workforce deliberately opted out of that world (audit rows pass `project_id=None`, `workforce_cost_evidence.py:139`).
3. A `Project` FK would make `cost_evidence` deletion-sensitive to `Project` deletion and would let a Project-owning caller query/alter Workforce cost facts — inverting the dependency direction (see §13 P7).
4. `Project.budget_used` accrual is delegation-owned (`delegation.py:491`); a FK would create the *expectation* that `cost_evidence` feeds it. It must not (§3.6).

**Design rule B-6**: `cost_evidence` stays project-free in W6. If a future stage needs project roll-up, it is a **read-side projection in the owning domain**, not an FK on the Workforce table.

`[ASSUMPTION] A-2`: eventual multi-tenant/project reporting may need a project dimension; the design position is that this is satisfied by projection, not by FK. Unverified — no reporting requirement exists in the repo.

### 3.6 Q6 — 是否应该进入 `Project.budget_used`？

**No.** Three independent reasons, all repo-evidenced:

1. **No target**: `budget_used` lives on `Project` (`models.py:257`); `cost_evidence` has no `project_id` (§3.5) and therefore cannot even identify which `Project` to charge.
2. **Single writer**: `_accrue_budget` (`delegation.py:476-495`) is the *only* writer, and its input is `DelegatedRun.cost`. Adding a second writer from a different domain would break the invariant that `budget_used` means "spend accrued by delegated runs" (`models.py:255-257` comment).
3. **Explicitly prohibited**: `workforce_cost_evidence.py:9` and `:92` — "read or write ``Project.budget_used``" is listed in the module docstring as something this function never does, and `tests/test_workforce_cost_evidence_w5.py:406` (`test_writer_structurally_excluded_from_budget_machinery`) locks it.

**Design rule B-7**: `cost_evidence` is a **non-authoritative fact log**. It never mutates any ledger. `Project.budget_used` remains the only authoritative spend ledger, and its only writer remains delegation.

### 3.7 Q7 — `check_budget` 应该在哪一层发生？

**Repo fact**: `check_budget` is called at exactly one place — `delegation.py:282`, inside `DelegatedExecutionAdapter.run` (`delegation.py:243`), **before** any remote call or secret resolution. It uses `Task.estimated_cost` as the estimate and `Project.budget_limit` as the ceiling.

**Design rule B-8 (gate precedes spend, in the domain that owns the budget)**:
- The budget gate belongs to the **executing domain that owns the ledger**. Today that is delegation, at the pre-execution boundary.
- Workforce has **no execution boundary today** (no runner, no remote call, no budget). Therefore Workforce has **no gate**.
- `cost_evidence` can **never** be a gate: it is written *after* the spend is measured. A post-hoc record cannot prevent anything.

**Design rule B-9 (if Workforce later gains execution)**: the gate must be introduced at the **new execution entry point** in whichever domain performs the spend, using that domain's ledger — not bolted onto `record_cost_evidence`. If Workforce executes through delegation, delegation's existing gate already applies and Workforce must not add a second one (double gating, double counting).

→ Which of these paths Workforce will take is `[DECISION REQUIRED] DR-D1-2`.

### 3.8 Q8 — 成本记录 / 预算预留 / 实际扣除的 domain boundary

| Concept | Meaning | Exists in repo? | Owner | Storage |
|---|---|---|---|---|
| **Estimate (估算)** | Predicted spend used to decide whether to proceed | Yes | Delegation (input) / Task | `Task.estimated_cost` (`models.py:~370`); consumed at `delegation.py:282` |
| **Reservation / hold (预留)** | Tentative deduction that reduces available budget before spend | **No** — grep finds no reservation writer; `budget_used` is post-hoc only | — | — |
| **Actual accrual (实际扣除)** | Authoritative post-spend increment of the ledger | Yes | **Delegation** | `Project.budget_used`, written only at `delegation.py:491` from `DelegatedRun.cost` |
| **Evidence record (成本记录)** | Non-authoritative append-only fact that a measured cost occurred, anchored to a Workforce JobVersion | Yes (table + dormant writer) | **Workforce** | `cost_evidence` (`models.py:2181-2210`) |
| **Advisory (建议)** | Human-readable cost signal used in ranking, never a number | Yes | Workforce (recommendation) | `agent.cost_policy` → advisory text (`workforce_recommendation.py:128+`) |

**Boundary rules**:
- **R-1** Estimate → Reservation → Accrual is a *Delegation/Project* lifecycle. Workforce does not participate.
- **R-2** Evidence is *Workforce* and is orthogonal to the ledger: a row may exist with no ledger impact, and ledger movement may occur with no evidence row.
- **R-3** Advisory is never promoted to estimate or accrual (F-R5, `workforce.py:905-945` keeps `cost_evidence.status = "unknown"`).
- **R-4** No reservation mechanism exists today. Introducing one is out of W6 scope (§15).

### 3.9 Q9 — idempotency / audit / replay / failure 如何与 W5 一致？

**All four are inherited unchanged from W5; W6 adds nothing.** Restated because W5's design doc is not on `main` (§2.5):

| Concern | W5 behaviour (inherit) | Evidence |
|---|---|---|
| Idempotency key | `f"{source_event_type}:{source_event_id}"`, UNIQUE index `ix_cost_evidence_idempotency_key` | `workforce_cost_evidence.py:115`; `models.py:2205` |
| Replay | Re-flush violates the UNIQUE constraint; `IntegrityError` **propagates** (not absorbed) and rolls back evidence + audit together | `workforce_cost_evidence.py:124-145`; `tests/…w5.py:459` |
| Audit pairing | one `begin_nested()` savepoint; `action="cost_evidence.create"`, `project_id=None`, `task_id=None`, same key | `:127-145` |
| Failure | 422 (empty identity / `amount is None`), 404 (missing JobVersion/Employee), 403 (non-owner), TypeError (no actor) — all fail-closed, no defaults | `:96-113`; `tests/…w5.py:301-404` |
| Population | zero rows in V1 (no caller) — asserted | `tests/…w5.py:643` |

**One W6 addition (design only)**: if DR-D1-4 selects option (a) (source-event existence check), the check must happen **inside the same savepoint** as the insert and audit, so that a failed check rolls back nothing but also commits nothing. This preserves W5's "all three or none" shape.

---

## 4. D-4 — Employee 删除 / 生命周期语义

### 4.1 Q1 — Employee 为什么当前永久保留？

Five independent repo facts, each sufficient on its own:

1. **Single status member, zero outbound edges.** `EmployeeStatus` (`models.py:1996-2010`) has exactly one member, `ACTIVE`. Its docstring states there is no `TERMINATED`/`ON_LEAVE`/`SUSPENDED` member "because V1 has no writer for any of them (D-4: W4 ships no `purge_employee` and no delete semantics at all — that is a W5 obligation)". A state machine with no outbound edge cannot leave `ACTIVE`.
2. **No writer for any other state.** The only `Employee` writer is `promote_to_employee` (`workforce_employee.py:329`), which hard-codes `status=EmployeeStatus.ACTIVE` ("V1 has one status and one writer: this constructor call").
3. **No delete path.** The only three `session.delete` sites in `src/` are `agent_registry.py:296`, `context_retention.py:37`, `workforce_recommendation.py:729`. None touches `Employee`.
4. **Fail-closed lineage.** `Employee.candidate_id` / `trial_id` / `job_id` / `job_version_id` are all `ON DELETE RESTRICT` (`models.py:2083-2179`) with the stated intent: "An Employee is live hiring evidence; deleting an upstream row must FAIL EXPLICITLY rather than silently cascade the company's headcount away."
5. **One employee per trial.** `uq_employee_trial` (unique on `trial_id`) means minting is already at-most-once; there is no "second chance" path that would need cleanup.

**Repo-verified**: `tests/test_workforce_cost_evidence_w5.py:623` (`test_employee_lifecycle_unchanged_single_active_state`) locks facts 1–2 as a regression guard.

**Conclusion**: permanence is a deliberate W4/W5 design decision documented in the model docstring, deferred to W6 as an explicit obligation — not an oversight.

### 4.2 Q2 — 是否需要 logical termination？

**Repo answer: cannot be determined from the repo.** Evidence of the *absence of demand*:

| Signal | Finding |
|---|---|
| Reverse references to `Employee` | exactly one: `CostEvidence.employee_id` (`models.py:2190-2194`). `Job`, `JobVersion`, `Trial`, `Candidate` have **no** `employee_id` |
| Forward consumers of `Employee.status` | none found — no read site branches on `employee.status` other than the constructor default |
| API surface | none (§2.4) — nothing external can request termination |
| Reporting/headcount requirement | none in `docs/` at `fedd388` |

So: **no repo artefact requires termination.** Whether the *business* requires it (an employee stops working but the hiring record must survive) is a product decision.

→ `[DECISION REQUIRED] DR-D4-1`: Options —
- **(A) Keep permanent (W6 default if unanswered).** No code change. `Employee` means "was hired", never "is employed".
- **(B) Add logical termination.** Add a `TERMINATED` status member + an owner-only writer + transition guard. Zero-migration (§4.3). Cost: new writer, new audit action, new tests, and a definition of "what a terminated employee may still be referenced by".
- **(C) Defer again to W7.** Acceptable, but then W6 must say so explicitly rather than re-deferring silently.

### 4.3 Q3 — 若未来 terminate，应新增什么状态 / 生命周期边界？

**Designonly** (no implementation). If DR-D4-1 = (B), the minimal coherent design is:

| Element | Design | Migration class | Evidence / rationale |
|---|---|---|---|
| Status member | `EmployeeStatus.TERMINATED = "terminated"` | **Zero-migration** — `employee.status` is a plain `sa.String()` (`models.py:1996-2010`) | same mechanism that let W3 add enum members without a revision |
| Transition guard | `ACTIVE → TERMINATED` only; `TERMINATED` is terminal (no re-hire by transition) | n/a (code) | mirrors `CandidateStatus.EMPLOYED` being terminal and `TrialLifecycle` rejecting illegal transitions (`workforce_employee.py:81-128`; `tests/…w4.py:711,736`) |
| Writer | owner-only (`_assert_owner_actor`), 404 if missing, 409 if not `ACTIVE` | n/a (code) | identical shape to `promote_to_employee` / `release_candidate` (`workforce_employee.py:329,433`) |
| Idempotency | replay of a terminate on an already-`TERMINATED` employee returns the existing row (adopt), not 409 — *or* 409; this is a sub-decision | n/a | W4 precedent is mixed: `promote` adopts (`workforce_employee.py:~420-430`, `test_promote_idempotent_returns_same_employee`), `activate` 409s on replay (`tests/…w4.py:309`) |
| Audit | `action="employee.terminated"`, `resource_type="employee"`, `before`/`after` full-column snapshots, `project_id=None`, `task_id=None`, `idempotency_key=f"employee:{employee_id}:terminate"` | n/a | matches `promote_to_employee`'s `employee.hired` pattern and the audit signature (`audit.py:~75-100`) |
| Immutability | `agent_id`, `job_id`, `job_version_id`, `candidate_id`, `trial_id`, `hired_at` remain frozen; termination adds **no** new column in the minimal design | n/a | F-E19 snapshots are never re-resolved (`workforce_employee.py:329-432`) |
| Effect on `Candidate` | none — `Candidate.EMPLOYED` is terminal and is **not** reverted | n/a | `tests/…w4.py:736` |
| Effect on `Trial` / `Job` | none — no reverse FK exists | n/a | §4.1 fact 1 |
| Effect on `CostEvidence` | none — historical evidence keeps pointing at the employee; `employee_id` stays RESTRICT | n/a | `models.py:2190-2194` |

**Design rule L-1**: termination is a **status change, never a delete**. The row, its audit trail, and all RESTRICT edges survive.

**Design rule L-2**: if a `terminated_at` timestamp or a `termination_reason` is wanted later, both are **additive nullable columns** — a normal additive migration, never a rewrite.

**Sub-decision**: whether a terminated employee's `job_version` may still receive new `cost_evidence` → folded into `[DECISION REQUIRED] DR-D4-4`.

### 4.4 Q4 — Employee 被 Job / Trial / Candidate / CostEvidence 引用后的删除语义？

Current, **DB-enforced** semantics (all verified by existing tests):

| Deletion attempt | Blocker | ON DELETE | Result today | Test |
|---|---|---|---|---|
| delete `JobVersion` referenced by an `Employee` | `employee.job_version_id` | **RESTRICT** | refused (IntegrityError) | `models.py:2083-2179`; W5 test `:573` blocks `job_version` delete when evidence exists (same chain) |
| delete `Job` referenced by an `Employee` | `employee.job_id` | **RESTRICT** | refused | `models.py:2083-2179` |
| delete `Trial` referenced by an `Employee` | `employee.trial_id` | **RESTRICT** | refused | `models.py:2083-2179` |
| delete `Candidate` referenced by an `Employee` | `employee.candidate_id` | **RESTRICT** | refused | `models.py:2083-2179` |
| delete `Employee` referenced by `CostEvidence` | `cost_evidence.employee_id` | **RESTRICT** | refused | `models.py:2190-2194`; `tests/…w5.py:598` |
| delete `Employee` **with no** cost evidence | — | — | **permitted at DB level**; no service-layer delete exists to forbid it | absence of any delete writer (§4.1 fact 3) |
| delete `Agent` referenced by an `Employee` | `employee.agent_id` | **NO ACTION** | DB-dependent; not RESTRICT | `models.py:2083-2179` |

**Two findings worth recording**:

- **F-1 (asymmetry)**: `Candidate`/`Trial`/`Job`/`JobVersion` are protected *by* the Employee, but the `Employee` row itself is only protected by `cost_evidence`. In V1 the table is empty (`tests/…w5.py:643`), so **today nothing at all prevents deleting an `Employee` row at the DB level**. The protection is "there is no code that deletes it", which is a convention, not an invariant.
- **F-2 (`agent_id` = NO ACTION)**: the one non-RESTRICT edge in the Employee lineage. Whether this is intentional (agent registry owns its own lifecycle) or an oversight is not documented in the model.

**Design rule L-3**: W6 keeps every existing `ON DELETE` exactly as-is (§6). Protection of the `Employee` row itself is raised from convention to invariant **only if** DR-D4-3 approves an explicit policy — otherwise F-1 stands as a documented accepted risk.

→ `[DECISION REQUIRED] DR-D4-5`: accept F-1 as-is, or add an explicit service-layer guard that no delete writer for `Employee` may exist (a test-level invariant, zero schema change).

### 4.5 Q5 — API 层是否应该统一映射为 409？

**Repo answer: there is no Workforce API layer to map.** Evidence:
- grep of `src/aios/api/*.py` and `src/aios/console.py` for `/workforce`, `/jobs`, `/employees`, `/trials` → **zero matches**.
- no `@app.delete` / `@router.delete` endpoint exists anywhere.
- `_translate` (`src/aios/api/app.py:301`) is a verbatim passthrough: `HTTPException(status_code=error.status_code, detail=error.detail)`. There is no 409 coalescing layer to extend.

Therefore the honest statement is:

- **Today**, 409 is a **service-layer** decision raised directly by Workforce writers: `promote_to_employee` (non-completed trial, non-trialing candidate), `release_candidate` (not releasable), `purge_recommendation` (not withdrawn/rejected). RESTRICT violations surface as DB `IntegrityError`, **not** as 409 — there is no translation from `IntegrityError` to 409 anywhere in the Workforce path.
- **If and when** a Workforce HTTP surface is added, the design position is:

| Condition | Mapping | Rationale |
|---|---|---|
| Illegal lifecycle transition (state machine rejects) | **409** | already the service-layer convention (`workforce_employee.py:329,433`) |
| Delete blocked by RESTRICT (`IntegrityError`) | **409** — requires an explicit `except IntegrityError` at the service boundary | today it escapes as a 500-class DB error; leaving it untranslated leaks internals |
| Missing parent row | **404** | W5 fail-closed (`workforce_cost_evidence.py:111-113`) |
| Empty required identity / `amount is None` | **422** | W5 (`:99-109`) |
| Non-owner actor | **403** | `_assert_owner_actor` |
| Any `untrusted` failure | byte-identical body, no detail | `ServiceError.untrusted` (`services.py:~40-49`) |

**Design rule L-4**: the mapping table above belongs to the **service layer**; the API layer stays a verbatim translator. Adding 409 coalescing *in the API layer* would re-implement domain rules outside the domain.

→ `[DECISION REQUIRED] DR-D4-2`: approve the mapping table as the contract for the future Workforce API (it is unenforceable today and therefore not a repo fact).

### 4.6 Q6 — physical purge 是否永久禁止？

**W6 position: forbidden in W6.** Two reasons, both repo-grounded:
1. `Employee` is defined as **live hiring evidence**; its docstring states that deleting an upstream row must fail explicitly rather than "silently cascade the company's headcount away" (`models.py:2083-2179`). That intent is incompatible with a purge path.
2. Evidence rows are RESTRICT-bound to it (`models.py:2190-2194`), so a purge would require first deleting or re-anchoring cost facts — i.e. destroying the audit trail W5 was built to preserve.

**Not permanently forbidden by design.** A future controlled archive/purge is *conceivable*; the single existing precedent (`purge_recommendation`, `workforce_recommendation.py:667-735`) shows exactly what the repo considers an acceptable purge:

| Precondition (from the precedent) | Applied to `Employee` |
|---|---|
| Owner-only (`_assert_owner_actor`) | mandatory |
| 404 if the row does not exist | mandatory |
| **409 unless the row is in a terminal, purgeable state** (WITHDRAWN/REJECTED for recommendations) | requires a defined terminal state — which is exactly what `TERMINATED` would provide. **Purge is therefore strictly downstream of DR-D4-1 = (B)** |
| Full-column `before` snapshot into audit, `action="<resource>.deleted"`, `project_id=None`, `task_id=None`, explicit `idempotency_key` | mandatory |
| Delete inside `begin_nested()` with the audit write | mandatory |
| RESTRICT children must be resolved first | `cost_evidence.employee_id` is RESTRICT → purge is impossible while any evidence exists |

**Design rule L-5 (ordering)**: `terminate` (status) → `archive` (if ever) → `purge` (if ever). Each step is a separate decision; none may be collapsed into another.

**Design rule L-6 (prefer archive over purge)**: if erasure pressure exists, the preferred shape is an additive archive (copy + mark), not a physical delete, because RESTRICT children and audit history must survive.

→ `[DECISION REQUIRED] DR-D4-3`: permanently forbid purge; or allow a future controlled purge subject to all six preconditions above.

---

## 5. Domain Boundary (W6 vs W1–W5)

### 5.1 Ownership map

| Domain | Owns | Tables / modules | W6 may touch? |
|---|---|---|---|
| **Delegation / Project** | budget limit, spend ledger, pre-execution gate, remote execution | `project`, `task`, `delegated_run`, `delegation.py` (`check_budget`, `_accrue_budget`) | **No — frozen** |
| **W1 Discovery** | `business_goal`, `required_work`, `job`, `job_version` | `workforce.py` | **No — frozen** |
| **W2 Capability / Evaluation** | `capability_requirement`, `candidate`, evaluation context | `workforce.py` | **No — frozen** |
| **W3-A/B/C/D** | benchmark, match, recommendation, trial lifecycle | `workforce.py`, `workforce_recommendation.py`, `workforce_employee.py` (trial part) | **No — frozen** |
| **W4 Employee** | `employee` minting from a completed trial | `models.py:2083+`, `workforce_employee.py` | **Only additively** (new status value = zero-migration; new writer module) |
| **W5 Cost Evidence** | `cost_evidence` table + dormant writer | `models.py:2181+`, `workforce_cost_evidence.py` | **Frozen for W6** — W6 changes no W5 behaviour; if P-9 is approved later it is an additive guard inside the existing savepoint |
| **W6 (this document)** | (a) the source-event **admission contract**; (b) the Employee **lifecycle policy** | `docs/` only at this stage | — |
| **Audit (cross-cutting)** | `audit_log`, `append_audit`, `redact_secrets` | `audit.py` | **No — consume only** |

### 5.2 What W6 may change (when implementation is authorized)

- Add a **new module** for a future cost producer (not in this document).
- Add **new status values** to `EmployeeStatus` (zero-migration).
- Add **additive nullable columns** to `employee` (e.g. `terminated_at`) if approved.
- Add **new tests**; never modify existing W1–W5 assertions.

### 5.3 What W6 must never change

- Any of the 13 frozen Workforce tables' existing columns or `ON DELETE` behaviour.
- `delegation.py` budget functions and `Project.budget_*`.
- `cost_evidence` column set, FK targets, idempotency key derivation, savepoint shape, audit action name.
- Existing Alembic revisions.
- The zero-population property of `cost_evidence` in the absence of a producer (`tests/…w5.py:643`).

---

## 6. Schema / FK Design

### 6.1 FK / ON DELETE: no change required

| Question | Answer | Evidence |
|---|---|---|
| Does W6 require a new FK? | **No.** Termination needs no FK; purge is forbidden | §4.3, §4.6 |
| Does any existing `ON DELETE` change? | **No.** All RESTRICT edges stay RESTRICT; `agent_id` stays `NO ACTION` | §4.4 |
| Is a `Project` FK needed? | **No**, and it is actively prohibited | §3.5 |
| Is a `cost_evidence → source event` FK possible? | **No** — a polymorphic FK to "whichever table produced it" is not expressible; this is why P-9 (§3.4) is a registry or caller-obligation question, not an FK question | `models.py:2196-2200` (`source_event_type` / `source_event_id` are plain strings, no FK) |

### 6.2 Change → migration class table

| Proposed change | Migration class | Notes |
|---|---|---|
| `EmployeeStatus.TERMINATED` member | **zero-migration** | `employee.status` is plain `sa.String()` (`models.py:1996-2010`); enum members add no DDL |
| New lifecycle writer (`terminate_employee`) | **none** | pure Python module |
| New audit action string | **none** | `audit_log.action` is a plain indexed string (`audit.py:64`) |
| `employee.terminated_at` (nullable) | **additive** | `ALTER TABLE … ADD COLUMN nullable` |
| `employee.termination_reason` (nullable) | **additive** | same |
| New `source event` producer table, if ever approved | **additive** | new table + indexes + reversible `downgrade()` |
| Any change to existing `ON DELETE` | **prohibited in W6** | §5.3 |
| `cost_evidence.project_id` | **prohibited** | §3.5 |

### 6.3 Freeze assertions to carry into implementation

- Single Alembic head remains `20260904_0001_workforce_cost_evidence` until a W6 revision is authorized; any new revision sets `down_revision` to the then-current head.
- Every new revision must have a reversible `downgrade()` (W5 precedent).
- Base/head schema comparison must use `sqlalchemy.inspect()` semantic comparison, **not** `sqlite_master.sql` text (known non-determinism — see long-term project notes).

---

## 7. Budget Authority Design

### 7.1 Authority assignment

| Capability | Owner | Mechanism | Workforce's role |
|---|---|---|---|
| Define the ceiling | Project owner | `Project.budget_limit` (`models.py:250`) | none |
| Estimate before spend | Task/Delegation | `Task.estimated_cost` → `check_budget` (`delegation.py:282`) | none |
| Gate before execution | **Delegation** | `check_budget` raises `BudgetExceededError` **before** remote call/secret resolution (`delegation.py:157,282`) | **must not re-gate** |
| Accrue after spend | **Delegation** | `_accrue_budget` adds `DelegatedRun.cost` to `Project.budget_used` (`delegation.py:476-495`) | **must not write** |
| Record a Workforce-attributable fact | **Workforce** | `record_cost_evidence` (dormant) | owns — but non-authoritative |
| Advise on cost in ranking | Workforce | `agent.cost_policy` → advisory text only (`workforce_recommendation.py:128+`) | owns — never a number, never a gate |

### 7.2 Invariants

- **BA-1** `Project.budget_used` has exactly one writer (`delegation.py:491`).
- **BA-2** `cost_evidence` never reads or writes `Project.budget_*` (`workforce_cost_evidence.py:9,92`; test `:406`).
- **BA-3** `cost_evidence` never calls `check_budget` and never blocks anything (it is written after the spend).
- **BA-4** A spend is recorded in exactly one domain (§3.2, R-2/B-1).
- **BA-5** No reservation/hold concept exists; W6 does not introduce one (§15).

---

## 8. Source Event Contract (the W6 deliverable for D-1)

Because no producer exists, W6's concrete output is an **admission contract** that any future producer must satisfy before `record_cost_evidence` may be called with its id.

### 8.1 Admission rules

| ID | Rule | Rationale / evidence |
|---|---|---|
| **A-1 Measured** | The event carries a **measured** amount observed by the producer. Estimates, policy-derived numbers and defaults are inadmissible. | W5 I6 (`workforce_cost_evidence.py:108-109`); F-R5 (advisory text only) |
| **A-2 Anchored** | The event is attributable to a `job_version` (and optionally an `employee`). An event anchored only to `project`/`task`/`delegated_run` is a delegation-domain fact and is **not** admissible here. | §3.2; `models.py:2185-2194` |
| **A-3 Identifiable** | The event has a stable, unique id in its own table, and a stable type string. `{type}:{id}` must be globally unique as a cost fact. | `workforce_cost_evidence.py:115`; `models.py:2205` |
| **A-4 Single producer** | Exactly one code path may produce a given `{type}:{id}`. Two producers for the same id break at-most-once. | W5 I5 |
| **A-5 Non-repudiable** | The producer persists the measurement before (or in the same transaction as) the evidence write, so the amount can be re-derived from the producer's own row. | mirrors `_accrue_budget` reading a **persisted** `DelegatedRun.cost` (`delegation.py:476-495`) |
| **A-6 No double count** | The spend is not already recorded in the authoritative ledger of another domain. | BA-4 |
| **A-7 Fail-closed** | If any of A-1…A-6 cannot be verified, **no row is written**. | W5 I3 |

### 8.2 Explicit rejection list

| Rejected input | Why |
|---|---|
| `delegated_run.id` as `source_event_id` | delegation-domain, not Workforce-attributable (`workforce_cost_evidence.py:14-18`) |
| `agent.cost_policy` value | schema-less estimate, never a measurement (`models.py:276`; `workforce.py:905-945`) |
| A benchmark result with `status="unknown"` | nothing was measured (`workforce.py:1251-1325`) |
| Synthetic / empty / defaulted identity | hard 422 (`workforce_cost_evidence.py:99-106`) |
| `amount = 0` inferred from "no measurement" | "no measurement" must stay "no row" (W5 I6) |

### 8.3 Contract status

The contract is **empty of instances** at `fedd388`. Registering the first producer requires DR-D1-1 + DR-D1-3.

---

## 9. Idempotency / Replay

### 9.1 Inherited (W5)

| Property | Behaviour |
|---|---|
| Key derivation | `f"{source_event_type}:{source_event_id}"` |
| Uniqueness | UNIQUE index on `cost_evidence.idempotency_key` (`models.py:2205`) |
| Replay outcome | second insert violates UNIQUE; `IntegrityError` **propagates**; the savepoint rolls back evidence + audit together (`workforce_cost_evidence.py:124-145`) |
| Concurrency | first writer wins; the loser raises (W4 precedent adopts the winner in `promote_to_employee`, but W5 deliberately does **not** absorb) |
| Distinct events | two different source events with the same amount produce two rows (`tests/…w5.py:507`) |

### 9.2 W6 additions (design only)

- **IR-1** If a `terminate_employee` writer is ever added, its idempotency key shape is `employee:{employee_id}:terminate` and its replay behaviour must be chosen explicitly (adopt vs 409) — see §4.3 sub-decision and `[DECISION REQUIRED] DR-D4-4`.
- **IR-2** If DR-D1-4 = (a) (existence registry), the existence check lives **inside** the same savepoint as insert + audit, preserving "all or nothing".
- **IR-3** Replay must never mutate: there is no update path, and W6 must not add one (B-5).

### 9.3 Replay vs lifecycle

Evidence rows are **not** part of any state machine (W5 I9: "`cost_evidence` sits OUTSIDE both lifecycle state machines … never gates `Candidate` / `Trial` / `Employee` transitions"). Replaying a lifecycle transition therefore never re-writes evidence, and replaying evidence never transitions anything.

---

## 10. Audit Contract

### 10.1 Existing contract

`append_audit(session, *, actor, action, resource_type, resource_id, project_id, task_id, before, after, idempotency_key)` (`src/aios/audit.py`), with `before`/`after` passed through `redact_secrets` (key-name based; also pattern-matches token-looking values).

### 10.2 Workforce conventions (must be preserved)

| Convention | Value | Evidence |
|---|---|---|
| `project_id` | `None` | `workforce_cost_evidence.py:139`; W5 I10 |
| `task_id` | `None` | same |
| `actor` | `actor.owner_id` | `:137` |
| `idempotency_key` | same key as the business row (reuse, not a second key) | `:143` |
| `before` / `after` | full-column snapshots | `_cost_evidence_snapshot`, `_employee_snapshot` (`workforce_employee.py:154`) |
| `resource_type` | singular resource name | `"cost_evidence"`, `"employee"` |
| pairing | inside the same savepoint as the business write | `workforce_cost_evidence.py:127-145` |

### 10.3 Proposed (not implemented) action strings

| Action | When | Status |
|---|---|---|
| `cost_evidence.create` | exists | shipped (`workforce_cost_evidence.py:138`) |
| `employee.hired` | exists | shipped (`workforce_employee.py:329-432`) |
| `employee.terminated` | if DR-D4-1 = (B) | **proposed** — new string, no migration (`audit_log.action` is a plain indexed string, `audit.py:64`) |
| `employee.purged` | only if DR-D4-3 allows purge | **proposed**, modelled on `recommendation.deleted` (`workforce_recommendation.py:667-735`) |

### 10.4 Redaction caveat

`redact_secrets` redacts by **key name** and by token-shaped values; it does not understand domain semantics. Any future cost payload placed in `before`/`after` must avoid embedding secrets under unrecognized key names. (Project note: redaction is key-name driven; see long-term notes.)

---

## 11. Failure / Fail-Closed Semantics

### 11.1 Existing (W5 + W4)

| Failure | Status | Where |
|---|---|---|
| Non-owner actor | 403 | `_assert_owner_actor` |
| Missing `actor` | TypeError | `_assert_owner_actor` |
| Empty `source_event_type`/`source_event_id` | 422 | `workforce_cost_evidence.py:99-106` |
| `amount is None` | 422 | `:108-109` |
| Missing `JobVersion` / `Employee` | 404 | `:111-113` |
| Replay (UNIQUE violation) | `IntegrityError` propagates | `:124-145` |
| Illegal trial/candidate transition | 409 | `workforce_employee.py:329,433` |
| Purge of a non-terminal recommendation | 409 | `workforce_recommendation.py:667+` |
| Unknown dimension / fabricated value | 422 | `_copy_unknown_dimensions` (`workforce_recommendation.py`) |

### 11.2 W6 additions (design only)

| New failure (only if the corresponding decision is approved) | Proposed status | Rationale |
|---|---|---|
| Terminate a non-`ACTIVE` employee | **409** | matches `promote_to_employee` / `release_candidate` |
| Terminate a missing employee | **404** | W5 fail-closed pattern |
| Purge an employee with RESTRICT children (`cost_evidence`) | **409** (service layer) | today it would escape as a DB error; L-4 requires translation |
| Call `record_cost_evidence` with a source event that fails A-1…A-6 | **no row, propagate** | A-7 fail-closed |
| Call `record_cost_evidence` with a non-existent source event id, if DR-D1-4 = (a) | **404/422 — to be fixed by that decision** | P-9 |

### 11.3 Global rule

**FC-1** Every W6 path fails **closed**: on any unverifiable input, write nothing. No defaults, no "best effort" rows, no silent degradation. (Matches `_DefaultBenchmarkAdapter` returning `trusted=False` rather than inventing a score.)

---

## 12. Migration Strategy

### 12.1 Principles

1. **Additive-only.** New table or new nullable column. Never rewrite, never drop, never alter an existing `ON DELETE`.
2. **Single head.** Preserve one Alembic head; every new revision chains to the current head.
3. **Reversible.** Every revision ships a `downgrade()` (W5 precedent: drop indexes, then table).
4. **Zero-migration preferred.** Status/enum additions and new action strings need no DDL — use them.
5. **Semantic verification.** Compare base/head schemas with `sqlalchemy.inspect()`, not `sqlite_master.sql` text.
6. **`engine.dispose()` between DDL and verification** — known SQLite + QueuePool stale-schema trap (long-term project note).

### 12.2 If the decisions are approved, the ordered sequence is

1. **DR-D4-1 = (B)** → add `EmployeeStatus.TERMINATED` (zero-migration) + writer + audit action + tests. **No revision.**
2. *(optional)* add nullable `terminated_at` / `termination_reason` → **one additive revision** on `employee`.
3. **DR-D1-1 / DR-D1-3** → add the producer's own table/columns → **one additive revision** in the producer's domain.
4. *(optional, DR-D1-4 = (a))* add the source-event registry → **no revision** (code only).
5. **DR-D4-3 = allow purge** → not before steps 1–2; purge is strictly downstream of a terminal state.

Each step is an independent authorization gate; none is implied by this document.

---

## 13. W1–W5 Boundary Proof

| # | Required proof | Proof |
|---|---|---|
| **P1** | **W6 与 W1–W5 的 domain boundary** | §5.1 ownership map. W6 touches only (a) a *contract* for a future cost producer and (b) the Employee *lifecycle policy*. It changes no W1–W5 behaviour. `cost_evidence` remains dormant and zero-populated (`tests/…w5.py:643`). |
| **P2** | **哪些表/模块可改、哪些必须冻结** | §5.2 / §5.3. Mutable: `EmployeeStatus` values, additive nullable `employee` columns, new modules, new tests. Frozen: 13 Workforce tables' columns and ON DELETE, `delegation.py` budget functions, `Project.budget_*`, `cost_evidence` shape, existing revisions, `audit.py`. |
| **P3** | **FK / ON DELETE 是否需变化** | §6.1 — **no change**. Termination is a status value (zero-migration); purge is forbidden; no polymorphic FK is expressible for the source event (which is why P-9 is a registry/caller-obligation question). `agent_id` stays `NO ACTION` (F-2 noted, not changed). |
| **P4** | **migration 是否 additive-only** | §6.2 / §12. Every change class is zero-migration or additive-nullable. No drop, no rewrite, no ON DELETE alteration. Head stays `20260904_0001_workforce_cost_evidence` until authorized. |
| **P5** | **audit / idempotency / replay 边界** | §9 / §10. Keys derived from the source event natural key; evidence+audit in one savepoint with the same key; replay raises rather than absorbs; `project_id`/`task_id` permanently `None` for Workforce rows; no update path. |
| **P6** | **budget authority 归谁** | §7. Delegation owns ceiling/estimate/gate/accrual (`delegation.py:157,282,476-495`). Workforce owns only a non-authoritative fact log and advisory text. BA-1…BA-5. |
| **P7** | **依赖方向，禁止循环依赖** | Workforce chain has **zero** `project_id` (`models.py` lines 1370–2200 grep → no matches) and references no delegation table; `DelegatedRun` references no Workforce row (`models.py:466-495`). Direction is therefore **Workforce ⇸ Delegation/Project** (no edge at all today). Adding `cost_evidence.project_id` would create the first edge and would let Project lifecycle constrain Workforce facts — B-6 forbids it. Cross-domain correlation, if ever needed, is a read-side projection in the owning domain. |

---

## 14. Invariants

| ID | Invariant | Status |
|---|---|---|
| **I-W6-1** | `cost_evidence` has no caller and zero rows until a producer satisfies §8. | inherited (W5), test `:643` |
| **I-W6-2** | `cost_evidence` never reads or writes `Project.budget_*` and never calls `check_budget`. | inherited, test `:406` |
| **I-W6-3** | `cost_evidence` has no `project_id` and no FK to any Delegation/Project table. | new (B-6) |
| **I-W6-4** | A `source_event_id` is admissible only if §8 A-1…A-6 hold; otherwise no row is written. | new (A-7) |
| **I-W6-5** | `delegated_run.id` is never used as a Workforce source event. | inherited (module docstring `:14-18`) |
| **I-W6-6** | `Agent.cost_policy` output is advisory text only and is never an `amount`. | inherited (F-R5, `workforce.py:905-945`) |
| **I-W6-7** | Evidence rows are append-only; there is no update path. | new (B-5) |
| **I-W6-8** | `Employee` has exactly one writer and one status unless DR-D4-1 = (B) is authorized; then exactly one additional writer and one additional terminal status. | inherited + new |
| **I-W6-9** | No `Employee` purge path exists in W6. | new (L-5) |
| **I-W6-10** | No existing `ON DELETE` is altered; all Employee lineage edges stay RESTRICT. | new (L-3) |
| **I-W6-11** | All Workforce audit rows carry `project_id=None, task_id=None`. | inherited (I10) |
| **I-W6-12** | Business write + audit write share one savepoint; replay rolls back both. | inherited |
| **I-W6-13** | Termination (if approved) is a status change, never a delete; all RESTRICT edges and audit history survive. | new (L-1) |
| **I-W6-14** | Every W6 path fails closed: unverifiable input ⇒ no write. | new (FC-1) |

---

## 15. Explicit Non-Goals

W6 does **not**:

1. Implement or name a concrete first cost source event (DR-D1-1).
2. Build a benchmark/trial execution backend or any cost measurement capability.
3. Add a `Project` FK anywhere in the Workforce chain.
4. Move, mirror, or feed `Project.budget_used` from Workforce.
5. Introduce a reservation / hold / commit budget mechanism (none exists today).
6. Implement `terminate_employee`, `archive_employee`, or `purge_employee`.
7. Add any Workforce HTTP route or change `_translate` behaviour.
8. Write any migration, or modify `src/`, `tests/`, or `alembic/`.
9. Design W7/W8 scope, a scheduler, or an execution fabric.
10. Decide retention/erasure policy (a compliance input, not a repo fact).

---

## 16. Test Matrix

### 16.1 Frozen — must remain green, must not be modified

| File | Tests | Guards |
|---|---|---|
| `tests/test_workforce_cost_evidence_w5.py` | 20 | table shape, RESTRICT FKs, single Alembic head, 403/TypeError/422/404, budget-machinery exclusion, **no caller**, replay at-most-once, audit pairing, delete blocking, single ACTIVE state, zero population |
| `tests/test_workforce_employee_w4.py` | ~36 | owner-only, 404/409 transitions, idempotent promote, snapshot immutability, `promote` does not fill the job, single head |
| `tests/test_workforce_trial_w3d.py`, `…w3c.py`, `…w3b.py`, `…w3a.py`, `test_workforce_models.py` | existing | W3 regression surface |

### 16.2 Proposed — only if the corresponding decision is approved (not written now)

| Proposed test | Guards | Gated on |
|---|---|---|
| `test_no_project_fk_on_cost_evidence` | reflect `cost_evidence` FKs; assert no `project_id` and no FK to `project`/`task`/`delegated_run` | I-W6-3 (always desirable) |
| `test_employee_has_no_delete_writer` | static/source-level assertion that no Workforce module deletes `Employee` | DR-D4-5 |
| `test_employee_status_accepts_terminated_without_migration` | build to head, insert `status="terminated"`, assert no schema drift vs. `employee` columns frozen test | DR-D4-1 = (B) |
| `test_terminate_rejects_non_owner_403` / `…_missing_404` / `…_non_active_409` | new writer fail-closed | DR-D4-1 = (B) |
| `test_terminate_trails_audit_in_same_transaction` | savepoint pairing | DR-D4-1 = (B) |
| `test_terminate_preserves_restrict_children` | `cost_evidence` still references the employee; delete still blocked | DR-D4-1 = (B) |
| `test_purge_employee_blocked_while_evidence_exists` | RESTRICT, 409 at service layer | DR-D4-3 |
| `test_source_event_admission_rejects_delegated_run_id` | A-2 / I-W6-5 static + behavioural | DR-D1-1 |
| `test_source_event_existence_check` | P-9 | DR-D1-4 = (a) |
| `test_cost_evidence_population_still_zero` | I-W6-1 after any W6 change | always |

### 16.3 Test-writing constraints for the implementation stage

- Use `--basetemp=` inside the repo (known `E:\Temp\pytest-of-Administrator` PermissionError).
- Run pytest **serially, single process** (known concurrent sqlite disk I/O false failures).
- `engine.dispose()` between DDL and verification.
- `PYTHONPATH=src`.

---

## 17. `[ASSUMPTION]` Register

| ID | Assumption | Why unverifiable from repo |
|---|---|---|
| **A-1** | The structurally cheapest first real cost source event is a benchmark/evaluation execution event, because it already has a Workforce anchor and an execution-shaped entry point (`run_benchmark`). | No execution backend, no cost column, no requirement document exists |
| **A-2** | Any future project-level cost reporting can be satisfied by a read-side projection rather than a `Project` FK. | No reporting requirement exists in `docs/` at `fedd388` |
| **A-3** | "Employee" in the business sense may need to distinguish "was hired" from "is currently employed". | No consumer of `employee.status` exists; no product requirement in repo |
| **A-4** | `Employee.agent_id` being `NO ACTION` (rather than RESTRICT) is intentional, because agent lifecycle is owned by the agent registry. | Not documented in the model; `agent_registry.py:296` does contain a delete, but no rationale is recorded |
| **A-5** | The absence of any reservation/hold mechanism is intentional (post-hoc accrual only), not an unfinished feature. | No design document mentions reservation |
| **A-6** | `cost_evidence` will eventually need a reporting/aggregation surface, which will read the table but never write it. | No reporting code exists |

---

## 18. `[DECISION REQUIRED]` Register

| ID | Question | Options | Impact if deferred | Recommended default |
|---|---|---|---|---|
| **DR-D1-1** | Which event is the first real Workforce-attributable cost source event? | (a) benchmark/eval execution — needs a real runner + additive cost column; (b) trial execution — needs usage metering; (c) externally attested spend (owner-attested, new event type); (d) none yet — keep `cost_evidence` frozen | `cost_evidence` stays dormant and empty forever; the W5 table remains unused | **(d)** keep frozen; do not fabricate |
| **DR-D1-2** | If Workforce work is executed, does it go through delegation (inheriting `check_budget` + `budget_used`) or through a Workforce-owned execution path with its own ledger? | (a) through delegation; (b) Workforce-owned execution + ledger; (c) never execute from Workforce | Determines whether a second budget authority is ever created | **(a)** — reuse, do not duplicate (BA-1) |
| **DR-D1-3** | Which stage/team owns producing the event (i.e. who builds the measurement capability)? | n/a — resource/product decision | No producer ⇒ no evidence ⇒ I-W6-1 holds | — |
| **DR-D1-4** | Should `record_cost_evidence` verify that the referenced source event row exists? | (a) per-type existence registry inside the savepoint; (b) caller obligation, enforced by review/tests | (b) leaves P-9 as a trust gap once a producer exists | **(b)** — no registry needed while the writer has no caller |
| **DR-D4-1** | Does `Employee` need logical termination? | (A) keep permanent; (B) add `TERMINATED` + writer (zero-migration); (C) defer to W7 | `Employee` permanently means "was hired" | **(A)** — nothing in the repo requires termination |
| **DR-D4-2** | Approve the §4.5 status-mapping contract for a future Workforce API (409 for illegal transition and for RESTRICT-blocked delete)? | approve / amend | Untranslated `IntegrityError` would leak as a 500 when an API lands | **approve as written** (currently unenforceable — no routes) |
| **DR-D4-3** | Is physical purge of `Employee` permanently forbidden, or may a controlled archive/purge be designed later? | (a) permanently forbidden; (b) allowed subject to all six preconditions in §4.6 | Blocks any future erasure/retention compliance path | **(a)** — consistent with "live hiring evidence" |
| **DR-D4-4** | If termination is approved: (i) is `TERMINATED` strictly terminal (no re-hire by transition)? (ii) does replay adopt the existing row or 409? (iii) may a terminated employee's `job_version` still receive cost evidence? | per sub-question | Blocks the termination design | (i) terminal; (ii) adopt (W4 promote precedent); (iii) **yes** — evidence is historical and sits outside the state machine (W5 I9) |
| **DR-D4-5** | Accept F-1 (§4.4) — today nothing at DB level prevents deleting an Employee that has no cost evidence — or add an explicit "no delete writer" test-level invariant? | accept / add invariant | An accidental future delete writer would silently destroy hiring evidence | **add invariant** (zero schema change) |

---

## 19. Open Questions

1. **Is `cost_evidence` ever meant to become authoritative?** W5 defines it as a non-authoritative fact log with no ledger effect. If the product expects "Workforce spend" to be *the* number, a separate ledger decision is required — currently `[ASSUMPTION] A-6`.
2. **Who is the "customer" of Workforce cost data?** No reporting, dashboard, or API consumer exists at `fedd388`. Without a consumer, the value of admitting a source event is unproven.
3. **Does `Trial` execution ever become measurable?** `Trial` carries no usage column today; if it becomes the execution unit (rather than `BenchmarkResult`), the anchor for evidence would shift from `candidate`/`benchmark` to `trial`.
4. **`agent_id` = NO ACTION (F-2)**: is this intentional? If an Agent is purged (`agent_registry.py:296`), what should happen to the `Employee` rows that reference it?
5. **Retention / erasure**: is there any external obligation (contractual or regulatory) that would one day force Employee erasure? This is the only force that could legitimately reopen DR-D4-3.
6. **Should `cost_evidence` support corrections/credits (negative amounts)?** `amount` is nullable float with no sign constraint; nothing in W5 forbids negatives, but no correction policy exists. Out of W6 scope; noted for the stage that admits the first producer.
7. **Will a Workforce HTTP surface ever exist?** If yes, §4.5 becomes binding and the L-4 mapping must be implemented at the service layer before the first route ships.

---

## Appendix A — How to re-verify this document

All evidence pointers are reproducible from base `fedd388`:

- Workforce chain has no `project_id`: filter `grep -n project_id src/aios/models.py` to lines 1370–2200.
- Budget writer uniqueness: `grep -rn budget_used src/ --include=*.py` → single write at `delegation.py:491`.
- Delete-site enumeration: `grep -rn "session.delete" src/ --include=*.py` (excluding `api/app.py`) → 3 sites, none in the Employee path.
- Absence of Workforce routes: `grep -rn "/workforce\|/employees\|/jobs\|/trials" src/aios/api/ src/aios/console.py`.
- Alembic head: `ls alembic/versions/` → `20260904_0001_workforce_cost_evidence.py` is the newest.
- Frozen tests: `pytest -q tests/test_workforce_cost_evidence_w5.py tests/test_workforce_employee_w4.py --basetemp=<repo-local-dir>` with `PYTHONPATH=src`.

---

## Appendix B — Document provenance

- Base: `fedd38819d5a57798fd31fd8a3691c30a60b5c21`
- Branch: `docs/w6-design`
- Change type: **docs-only**. No `src/`, `tests/`, or `alembic/` file was created, modified, or deleted.
- No push, no PR, no merge performed. Awaiting owner authorization for the next step.
