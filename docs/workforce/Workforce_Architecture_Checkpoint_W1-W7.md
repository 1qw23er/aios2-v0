# Workforce Architecture Checkpoint — W1–W7

**Status**: `PASS WITH CONDITIONS` (analysis-only checkpoint; **Implementation authorized: NO**)
**Baseline commit**: `94df645eac880041e6adb3ad19b76115c5c46ea6` (origin/main tip, PR#16 squash merge)
**Baseline tree**: `aa7f042fd877a8b9bbac1981fd1eee2a193fdbd0` (byte-identical to W7 exact-head tree — zero drift)
**Date**: 2026-09-06
**Scope**: repo archaeology + analysis only. Zero changes to `src/`, `alembic/`, `tests/`. This document is the sole artifact.

---

## 1. Executive Summary

W1–W7 delivered a complete, machine-verified **planning-and-hiring lifecycle** (Discovery → Candidate → Evaluation → Match/Benchmark → Recommendation → Trial → Employee) plus a **bookkeeping-only cost evidence ledger**, with every architectural boundary frozen by tests (W6: decision-freeze invariants; W7: 14 execution-boundary invariants).

The load-bearing finding of this checkpoint:

> **The Workforce domain and the execution/budget domain remain two disjoint trees.**
> `BusinessGoal` has no `project_id`; `DelegatedRun` has no Workforce lineage; `ExecutionResult` carries no cost; the only budget authority lives in delegation (`check_budget` + `Project.budget_used`); and `CostEvidence` is an empty-by-design projection awaiting a real Workforce-native source event.

**DR-W7-5 recommendation (b) — keep the bridge frozen, cost attribution stays caller-responsibility — is re-validated against the merged codebase.** All five blocking sub-questions (G-1..G-5) remain unanswered. The bridge must stay frozen until the DR-W7-5 business decision is made.

**Conditions on this PASS:**

1. DR-W7-5 stays `DECISION REQUIRED`; no bridge work may start from an unpriced premise.
2. The P1 debt items (JobVersion↔DelegatedRun attribution gap; failed-run cost non-accrual) are consequences of the frozen boundary, not defects to "fix" unilaterally — they are inputs to the DR-W7-5 decision.
3. No implementation, migration, or API surface for the bridge is authorized by this checkpoint.

---

## 2. Baseline

| Item | Value | Verification |
|---|---|---|
| origin/main tip | `94df645` (PR#16, W7 tests-only hardening) | `git rev-parse HEAD` |
| Parent | `d888a93` (PR#15, W6 invariants) | `git cat-file -p 94df645` |
| Tree | `aa7f042…`, identical to W7 exact-head `9885597`'s tree | zero-drift squash |
| Alembic single head | `20260904_0001_workforce_cost_evidence` (down: `20260903_0002_workforce_employee`), 31 migrations under `alembic/versions/` | all additive; no destructive migration in the W1–W7 series |
| CI gate | ruff + `pytest -q` (W7 invariants: 14 passed) | `tests/test_workforce_w7_invariants.py` |

**W1–W7 squash-merge chain on main** (recorded from merge history; SHAs ≥ W5 verified locally, earlier SHAs verified at merge time):

| Wave | PR | Squash SHA |
|---|---|---|
| W1+W2 Discovery/Candidate | #2 | `bf2f5c1` |
| W3-A Evaluation | #3 | (squash) |
| W3-B Match/Benchmark | #4 | (squash) |
| W3-C Recommendation/Approval/Trial | #5 | `225de0a` |
| W3-D Trial completion | #10 | `3f313ad` |
| W4 Employee | #12 | `6a4fd9f` |
| W5 Cost Evidence | #14 | `fedd388` |
| W6 Invariants | #15 | `d888a93` |
| W7 Hardening | #16 | `94df645` |

---

## 3. Capability Matrix

Status vocabulary: `IMPLEMENTED` / `PARTIAL` / `DESIGN ONLY` / `DEFERRED` / `NOT IMPLEMENTED`.

| Capability | Status | Evidence |
|---|---|---|
| W1 Discovery chain (BusinessGoal→RequiredWork→Job→JobVersion→CapabilityRequirement) | **IMPLEMENTED** | `workforce.py`: `create_business_goal` (138), `create_required_work` (166), `create_job` (224), `create_job_version` (271), `add_capability_requirement` (345). `CapabilityRequirement` UNIQUE(job_version_id, capability_id), FK RESTRICT to Alpha-1 `Capability` SSoT |
| W2 Candidate registry | **IMPLEMENTED** | `discover_candidates` (574), Candidate lifecycle enums (plain `sa.String` — additive-only evolution) |
| W3-A Candidate evaluation | **IMPLEMENTED** | `evaluate_candidate` (960); reliability/historical-performance stay `unknown` (no fabrication) |
| W3-B Match & Benchmark | **IMPLEMENTED** | `create_benchmark` (1136), `run_benchmark` (1327), `compute_match` (1489); `AgentCapability.priority` is the sole capability-fit signal |
| W3-C Recommendation & approval | **IMPLEMENTED** | `rank_candidates` (1716); `_build_cost_advisory` (`workforce_recommendation.py:128-139`) returns advisory text, never scores |
| W3-D Trial | **IMPLEMENTED** | Trial state machine; `TRIALING→EMPLOYED` transition deliberately routed to W4 |
| W4 Employee | **IMPLEMENTED** | `promote_to_employee` (`workforce_employee.py:329`) is the sole creator; 4-parent FK RESTRICT except `agent_id` NO ACTION; no soft-delete columns; `EmployeeStatus` has single member `ACTIVE` |
| W5 Cost evidence (bookkeeping) | **IMPLEMENTED** (schema + writer contract; **0 callers, 0 rows by design**) | `workforce_cost_evidence.record_cost_evidence` (65); `CostEvidence` (`models.py:2143`) |
| W6 Decision-freeze invariants | **IMPLEMENTED** (tests-only) | `test_workforce_w6_invariants.py`; DR-D1-1/2/4, DR-D4-1/2/3/5 frozen |
| W7 Execution-boundary hardening | **IMPLEMENTED** (tests-only) | `test_workforce_w7_invariants.py` — 14 invariants W7-I1..I14 |
| Workforce→Execution bridge | **NOT IMPLEMENTED** (frozen; DR-W7-5 open) | I1–I6 machine-checked: zero imports of `delegation`/`execution`, no schema reference, no producer |
| Workforce budget authority | **NOT IMPLEMENTED** (deferred) | budget authority is delegation's (see §5.3); I7/I8 |
| Workforce API routes | **NOT IMPLEMENTED** | no workforce route, no global IntegrityError handler in `api/app.py` (I13) |
| Reliability / historical-performance scoring | **NOT IMPLEMENTED** (FUTURE; fabrication forbidden) | W3 spec constraint |
| Agent termination / Employee purge | **NOT IMPLEMENTED** (deliberate) | I10/I12: no soft-delete infrastructure, no `TERMINATED` status; agent lifecycle authority = `agent_registry.set_agent_enabled` (I11) |

---

## 4. Current Architecture (module map, verified at `94df645`)

```
BusinessGoal ── RequiredWork ── Job ── JobVersion ── CapabilityRequirement
      (owner="human_ceo", NO project_id)         │ (FK→ Capability, RESTRICT)
                                                 ▼
                    Candidate ── Evaluation ── Match/Benchmark ── Recommendation
                                                                 │ approval
                                                                 ▼
                                     Trial ──(promote_to_employee)── Employee
                                                                  │
                                                    CostEvidence (job_version_id NOT NULL,
                                                     employee_id nullable; 0 rows in V1)

Execution side (disjoint tree):
Project ── DelegatedRun ── (budget_used accrual) ── build_delegated_provenance receipt
Task ── execute_task (ExecutionAdapter) ── ExecutionResult{summary,claims,artifacts,metadata}
```

- `workforce.py` imports exactly: `aios.agent_registry`, `aios.audit`, `aios.models`, `aios.services` (lines 76–99). **Zero** `delegation` / `execution` imports. `project_id=None` / `task_id=None` appear only as `append_audit` kwargs (lines 677, 759, 790, 1015, 1074, 1097, 1436, 1654, 1687).
- `ExecutionResult` (`execution.py:45`): `summary` / `claims` / `artifacts` / `metadata` — **no cost, no usage**. `LLMExecutionAdapter` (519) emits attempts metadata only ⇒ local execution has **zero cost metering**.
- `DelegatedExecutionAdapter` (`delegation.py:174`) carries cost; `WorkerResult.usage["cost"]` is the only live cost chain.
- `build_delegated_provenance` (`delegation.py:554-579`) produces the existing **execution receipt** (`Artifact.provenance_json`, includes `delegated_run_id` / cost / usage).

---

## 5. Authority Boundaries (5/5 verified)

### 5.1 Workforce Authority
Owns the planning→hiring lifecycle end-to-end (§4 left tree). Owns **no** execution, **no** budget, **no** project reference. Machine-checked by W7-I1, I2, I3, I5.

### 5.2 Execution Authority
`execution.execute_task` (187) + `ExecutionAdapter` protocol (60) for local execution; `delegation.DelegatedExecutionAdapter` (174) for delegated execution. Anchored on Task→Project; `DelegatedRun.project_id` NOT NULL. **Execution authority produces no cost for local runs.**

### 5.3 Budget Authority
**Sole authority: delegation.** `check_budget` (`delegation.py:157`) is the only gate (HARD-block only when `budget_limit > 0`, and only when `ctx_project_id is not None`); `_accrue_budget` (476–493) is the **only** writer of `Project.budget_used` (line 491), success-path only. Workforce owns no budget code (W7-I7); `CostEvidence` is not a budget authority (W7-I8).

### 5.4 Cost Evidence Authority
`CostEvidence` is an **append-only evidence ledger / projection**, not an authority. Per its model contract (`models.py:2143` docstring + migration `20260904_0001`): rows exist only for real, repo-defined, Workforce-attributable source events; **V1 has none ⇒ table expected 0 rows**; `delegated_run.id` is never reused as Workforce-attributable; no `currency` column; nullable `amount` float mirrors repo convention.

### 5.5 Workforce ↔ Execution Bridge
**Does not exist.** No import path (I1, I4), no schema reference (I3), no Workforce-owned execution producer (I5), and any future seam must fail loudly (I6). Two domain trees share zero join keys: `BusinessGoal` has no `project_id`; `DelegatedRun` has no Workforce lineage column.

---

## 6. DR-W7-5 Revalidation

| Sub-question | Re-check at `94df645` | Verdict |
|---|---|---|
| G-1: who is the budget owner for a Workforce-driven execution? | Unchanged — budget authority is delegation-only; Workforce has no budget hook | **Unanswered** |
| G-2: which Project carries the cost? | Unchanged — `BusinessGoal` has no `project_id`; no join key exists | **Unanswered** |
| G-3: what is the attribution载体 JobVersion↔DelegatedRun? | Unchanged — no shared column, no receipt binding | **Unanswered** |
| G-4: are failed runs charged? | Unchanged — `_accrue_budget` is success-path only (by design) | **Unanswered** |
| G-5: what is the Workforce-native cost source event? | Unchanged — `CostEvidence` has no producer; 0 rows | **Unanswered** |

**Conclusion**: DR-W7-5 recommendation **(b) — bridge stays frozen; cost attribution remains caller responsibility — still holds.** P-9 target (**C: execution receipt** as the future carrier; reject B registry-style and D cross-domain FK) unchanged. Any bridge work before a DR-W7-5 decision is unauthorized.

---

## 7. Closed / Frozen / Deferred

**Closed** (decided, code-backed): capability SSoT (Alpha-1); priority as sole fit signal; no fabrication of reliability/performance; DR-1 RESTRICT lineage for all Workforce parents; additive-only migration policy; Employee permanence (no purge, no soft-delete); audit redaction by key name.

**Frozen** (tests, not opinions): W6 invariants (budget/scheduler/execution deferral; zero-caller cost evidence); W7 invariants I1–I14 (import/schema/producer/receipt/budget/lifecycle/API hygiene).

**Deferred**: Budget/Scheduler for Workforce (W3 spec §8); DR-W7-4 (D4-4 employee-purge semantics); currency model (single-currency assumption recorded, no column).

---

## 8. Known Gaps

| # | Gap | Class |
|---|---|---|
| G-A | No attribution carrier between `JobVersion` (Workforce) and `DelegatedRun` (execution) | Boundary consequence of frozen bridge |
| G-B | Failed delegated runs never accrue cost (`_accrue_budget` success-path only) | Boundary consequence; interacts with G-A |
| G-C | `check_budget` is conditional: project-less `Task` skips the gate entirely | Pre-existing delegation-domain behavior |
| G-D | `CostEvidence` has no real source event (0 rows) — writer contract only | By design (D-1.4) |
| G-E | Candidate filtering does not consult agent `trust_level` | Workforce-internal quality gap |
| G-F | No API surface for Workforce; no global `IntegrityError`→409 translation (500 today) | API hygiene |

---

## 9. Decision Backlog (reclassified)

| Decision | Class | One-line |
|---|---|---|
| DR-W7-1 | ARCHITECTURE | Adopt execution-receipt (C) as the future cost carrier? |
| DR-W7-1b | ARCHITECTURE | Which domain owns the receipt? |
| DR-W7-2 | ARCHITECTURE | Pre-arranged 409 on `purge_employee` (integrity by contract)? |
| DR-W7-3 | ARCHITECTURE | Unify 11 `agent` FKs to RESTRICT (needs SQLite table rebuilds)? |
| DR-W7-6 | ARCHITECTURE | Global `IntegrityError`→409 translator in `api/app.py`? |
| DR-W7-5a–d | BUSINESS | Bridge master switch: budget owner / carrier Project / attribution载体 / failed-run charging |
| DR-W7-4 | DEFERRED | D4-4 employee deletion semantics |
| DR-W7-5e–h | IMPLEMENTATION DETAIL | Follow-ons once 5a–d are answered |

---

## 10. Candidate Directions (max 3)

1. **Debt & hygiene slice** (low risk, no boundary change): DR-W7-6 IntegrityError translator; FILLED-status writer-orphan resolution (write or deprecate); document `Task.actual_cost` dead column; trust_level in candidate filtering. Tests/docs + small additive code only.
2. **DR-W7-5 decision closure**: a decision workshop producing answers to 5a–d with a written ADR; only then re-open the bridge question. No code before the ADR.
3. **Cost Evidence activation** (only after a real Workforce-native source event exists — e.g., Workforce-routed delegation per 5a–d): wire the first legitimate caller of `record_cost_evidence`. Blocked by direction 2.

---

## 11. Recommended Next Objective

**Close the DR-W7-5 business decision (direction 2) and ship the debt & hygiene slice (direction 1) before any execution-bridge design work.** Rationale: every bridge option is unpriced until 5a–d are answered; direction 1 removes standing 500s and dead-code ambiguity without touching the frozen boundary. *(Checkpoint recommendation only — not an authorization.)*

---

## 12. Architecture Debt Register

| Priority | Item | Impact |
|---|---|---|
| **P0** | — | none (no boundary violation found; invariants hold) |
| **P1** | G-A attribution carrier missing; G-B failed-run cost non-accrual | Cost truth is incomplete for any future Workforce-driven execution |
| **P2** | G-C conditional budget gate; G-E trust_level not consulted | Silent gate bypass for project-less tasks; fit quality gap |
| **P3** | `JobStatus.FILLED` orphan enum (no writer); `Task.actual_cost` dead column; `origin/HEAD` default-branch pollution (`feat/real-agent-identity`); no Workforce API; single-currency assumption undocumented in code | Cosmetic / housekeeping; P3 origin/HEAD item is a recurring PR-tooling hazard |

---

## 13. Non-Goals

This checkpoint does **not** authorize: any bridge implementation; any migration; any W8 scope; any change to W6/W7 invariants; any budget-authority relocation. It makes no claim beyond commit `94df645`.

---

## 14. Evidence Index

| Claim | Primary evidence |
|---|---|
| Workforce imports (no delegation/execution) | `src/aios/workforce.py:76-99` |
| `ExecutionResult` has no cost/usage | `src/aios/execution.py:45-59` |
| Sole `budget_used` writer; success-path only | `src/aios/delegation.py:476-493` (write at 491) |
| `check_budget` conditional hard gate | `src/aios/delegation.py:157-170` |
| Execution receipt builder | `src/aios/delegation.py:554-579` |
| `CostEvidence` schema-only contract | `src/aios/models.py:2143` + `alembic/versions/20260904_0001_workforce_cost_evidence.py` |
| `BusinessGoal` owner/`no project_id` | `src/aios/models.py:1396-1414` |
| Employee FK policy, sole creator | `src/aios/models.py:2083+`, `src/aios/workforce_employee.py:329` |
| Advisory-only cost in recommendation | `src/aios/workforce_recommendation.py:128-139` |
| 14 boundary invariants | `tests/test_workforce_w7_invariants.py:137-446` (I1–I14) |
| Alembic single head | `alembic/versions/20260904_0001_workforce_cost_evidence.py` (revision/down_revision) |
| Baseline commit/tree | `git cat-file`/`rev-parse` at `94df645` / `aa7f042` |
