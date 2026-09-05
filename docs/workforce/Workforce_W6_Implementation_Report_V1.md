# Workforce W6 — Implementation Report V1 (Decision Freeze → Implementation)

**Branch**: `w6-invariants`
**Base**: `fedd38819d5a57798fd31fd8a3691c30a60b5c21` (main tip, W5 merged)
**Scope type**: additive tests only — **zero source change, zero migration, zero API**
**Design source**: `docs/workforce/Workforce_W6_Design_V1.md`

---

## 1. W6 Implementation Scope (decision → artefact)

Each row maps one frozen decision to the concrete artefact that makes it
executable. "No code artefact" means the decision is satisfied by *not writing
code* — it is never silently implemented.

| Decision | Ruling | Artefact | Status |
|---|---|---|---|
| **DR-D1-1** | (d) no new Workforce-native cost source event | **No code artefact.** No producer, no benchmark/trial/delegated_run reuse. The dormant-writer invariant already exists in `tests/test_workforce_cost_evidence_w5.py::test_writer_has_no_caller_in_v1` and `::test_v1_population_is_zero_without_a_caller` and is left untouched | DONE (by omission) |
| **DR-D1-2** | (a) reuse Delegation execution / budget gate / authoritative ledger | `test_workforce_chain_has_no_project_or_delegation_reference`, `test_workforce_modules_never_reference_delegation_domain`, `test_budget_used_has_exactly_one_writer` | DONE |
| **DR-D1-3** | deferred (producer owner = business decision) | **No code artefact** | DONE (deferred) |
| **DR-D1-4** | (b) no source-event registry; caller owns identity truth | **No code artefact.** P-9 (writer validates non-emptiness, not row existence) is left **OPEN** — see §2 | DONE (gap preserved) |
| **DR-D4-1** | (A) no `TERMINATED` status; permanent Employee | `test_employee_status_has_exactly_one_member`, `test_no_employee_terminate_writer_exists` | DONE |
| **DR-D4-2** | 409 mapping approved; no HTTP surface exists | `test_no_workforce_http_route_is_registered` — the mapping itself is **not** implemented (nothing to map); the test fails on purpose the day a Workforce route appears | DONE |
| **DR-D4-3** | (a) physical purge permanently forbidden | `test_no_employee_delete_or_purge_writer_exists`, `test_no_delete_call_targets_employee` | DONE |
| **DR-D4-4** | terminate terminal / replay adopts / terminated employees still carry historical cost evidence | **NOT IMPLEMENTED — conditional.** DR-D4-4 is only actionable when DR-D4-1 = (B); the owner chose (A). Recorded in the design document only | STOPPED (see §2) |
| **DR-D4-5** | test-level invariant instead of schema | the three static delete/terminate-writer tests above; **no column, no migration** | DONE |

### 1.1 Artefact summary

| Item | Value |
|---|---|
| Files added | 1 — `tests/test_workforce_w6_invariants.py` |
| Files modified | 0 |
| Files deleted | 0 |
| `src/` changes | **0** |
| `alembic/` changes | **0** (Alembic head unchanged: `20260904_0001_workforce_cost_evidence`) |
| Tests added | 10 |
| Tests modified / deleted | 0 |

---

## 2. STOP AND REPORT items (explicitly not done)

1. **P-9 (source-event row existence) — NOT fixed.** DR-D1-4 = (b) explicitly
   assigns the obligation to the future caller/producer. Fixing it would mean
   either a per-type dispatch registry or a new validation path inside the W5
   writer, both of which change W5 behaviour. Per authorization rule 7 the fix
   was judged **out of scope**; the gap is documented in Design V1 §3.4 and
   re-recorded in the test module docstring.
2. **DR-D4-4 — NOT implemented.** It presupposes a `TERMINATED` state that
   DR-D4-1 = (A) declined. Implementing "terminate is terminal / replay adopts"
   would require the very writer the owner refused. No partial implementation
   was attempted.
3. **F-1 / F-2 — preserved, not changed** (authorization rule 8). F-1 (an
   `Employee` row is DB-protected only by `cost_evidence`; with an empty
   evidence table nothing forbids deleting an Employee) is now covered at the
   *code* level by the delete-writer invariants — no FK was touched. F-2
   (`employee.agent_id` is `NO ACTION`) is pinned verbatim by
   `test_employee_fk_lifecycle_is_frozen`, so any future change is a conscious
   one.
4. **No Project FK, no `check_budget` wiring, no second ledger, no
   `Project.budget_used` write** (authorization rules 3–5). All four are now
   machine-checked.

---

## 3. Test → decision matrix (`tests/test_workforce_w6_invariants.py`)

| Test | Pins | Method |
|---|---|---|
| `test_workforce_chain_has_no_project_or_delegation_reference` | DR-D1-2(a), I-W6-3, P7 | real migrated schema: no `project_id` column, no FK into `project`/`task`/`delegated_run`, outward refs limited to `agent`/`capability` SSoT |
| `test_cost_evidence_fks_are_unchanged_and_restrict` | additive-only, no FK/ON DELETE change | `cost_evidence` FKs == exactly `{job_version: RESTRICT, employee: RESTRICT}` |
| `test_workforce_modules_never_reference_delegation_domain` | DR-D1-2(a), BA-1/2/3 | AST scan of every `workforce*.py`: no `check_budget` / `budget_used` / `DelegatedRun` / `delegated_run`, no `aios.delegation` import |
| `test_budget_used_has_exactly_one_writer` | BA-1, DR-D1-2(a) | AST scan: exactly one `.budget_used` assignment in all of `src/`, located in `delegation.py` |
| `test_employee_status_has_exactly_one_member` | DR-D4-1(A) | `EmployeeStatus` == `["active"]` |
| `test_no_employee_terminate_writer_exists` | DR-D4-1(A) | no `src/` function named `*employee*terminate|deactivate|offboard*` |
| `test_no_employee_delete_or_purge_writer_exists` | DR-D4-3(a) | no `src/` function named `*employee*delete|purge|remove|archive|destroy*` |
| `test_no_delete_call_targets_employee` | DR-D4-3(a), DR-D4-5 | no `*.delete(...)` call site in `src/` passes an Employee-typed argument |
| `test_employee_fk_lifecycle_is_frozen` | rule 8, F-2 | `employee` FK map pinned verbatim, including `agent_id: NO ACTION` |
| `test_no_workforce_http_route_is_registered` | DR-D4-2 | no route literal starting with `/workforce`, `/employee`, `/job`, `/trial`, `/candidate` (knowledge-candidate routes are a different domain and are excluded) |

Deliberate non-duplication: the W5 dormant-writer / zero-population and the W4
lifecycle tests already cover DR-D1-1 (d) and DR-D4-1 (A) behaviourally, so
they were neither copied nor modified.

---

## 4. Self-test

Environment: managed Python 3.13.12, `PYTHONPATH=src`, `--basetemp=<repo-local>`
(the default `E:\Temp\pytest-of-Administrator` raises PermissionError on this
machine), **single process, serial** (concurrent pytest on this host produces
false `sqlite disk I/O error` failures).

| Gate | Command | Result |
|---|---|---|
| Lint | `ruff check src tests alembic` | **All checks passed** |
| New invariants (standalone) | `pytest -q tests/test_workforce_w6_invariants.py` | **10 passed in 2.64s** |
| Targeted regression | `pytest -q tests/test_workforce_models.py tests/test_workforce_employee_w4.py tests/test_workforce_cost_evidence_w5.py tests/test_workforce_w6_invariants.py -p no:randomly` | **90 passed, 71 warnings in 1561.39s (26:01)** |
| Alembic head | `alembic/versions/20260904_0001_workforce_cost_evidence.py` | unchanged — `20260904_0001_workforce_cost_evidence` (down `20260903_0002_workforce_employee`) |

**Not re-run locally**: `test_workforce_evaluation_w3a.py`,
`test_workforce_benchmark_match_w3b.py`, `test_workforce_recommendation_w3c.py`,
`test_workforce_trial_w3d.py`. The change adds one test module and touches no
source, migration, or fixture, so the W3 suites cannot be affected by it; on
this host they cost ~2.7 tests/minute (disk pressure), which is not a useful
trade. **CI remains the final authority** — if CI disagrees, CI wins.

## 5. Pre-PR Audit (self-audit)

| # | Gate | Verdict |
|---|---|---|
| 1 | Only authorized scope implemented (§1) — nothing beyond the frozen decisions | PASS |
| 2 | No Workforce cost producer created (DR-D1-1 = d) | PASS — dormant writer untouched |
| 3 | No `Project` FK on any Workforce table (DR-D1-2 = a) | PASS — machine-checked |
| 4 | No `check_budget` wiring, no `Project.budget_used` write | PASS — machine-checked |
| 5 | No second budget ledger; `budget_used` still has exactly one writer | PASS — machine-checked |
| 6 | W1–W5 domain boundary unchanged | PASS — W4 + W5 suites green, unmodified |
| 7 | P-9 (source-event existence) not silently fixed; judged out of scope | PASS — reported in §2 |
| 8 | F-1 / F-2 preserved, no FK lifecycle change | PASS — pinned, not "fixed" |
| 9 | Additive-only: no migration, no source change, Alembic head unchanged | PASS |
| 10 | ruff clean on `src tests alembic` | PASS |

**Self-audit verdict: GO** (pending R7 confirmation and CI green; AI does not merge).
exact-head / tree / diff stat are recorded in the PR body and in the handoff message.

## 6. What the next stage inherits

* `cost_evidence` remains a dormant contract with zero rows and zero callers;
  the first producer still requires DR-D1-1 + DR-D1-3 to be reopened.
* The budget authority question is now machine-guarded: a second
  `budget_used` writer, a Workforce→Delegation import, or a `project_id`
  column on any Workforce table will fail CI immediately.
* Employee permanence is now a *test-level invariant*; reintroducing
  termination requires deleting `test_no_employee_terminate_writer_exists`
  **and** reopening DR-D4-1 in a new design document — not a silent edit.
* F-1 and F-2 remain open findings, pinned but unresolved.
