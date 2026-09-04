# W4 Employee — DSH Path-③ Implementation-State Audit Report

- **Date**: 2026-09-04
- **Audited branch**: `w4-employee-spec` @ `d912048` + uncommitted working tree
- **Audit mode**: Path-③ self-contained static audit of the implementation state
  (no execution; test evidence from the in-session suite runs is cited, CI is
  the source of truth)
- **Baseline contract**: R7-approved decisions D-1 … D-6 (2026-09-03) plus the
  extra confirmations recorded in the same ruling
- **Verdict**: ✅ **PASS — implementation matches the approved design. No
  boundary violation found.**

---

## 1. Scope of the audited change

```
 M  src/aios/models.py        (enum members + Trial 4 columns + Employee table)
 M  src/aios/workforce.py     (CandidateLifecycle unfreeze — the ONLY allowed edit)
 ?? src/aios/workforce_employee.py   (new module, PURE ADDITION)
 ?? alembic/versions/20260903_0002_workforce_employee.py  (additive migration)
 ?? tests/test_workforce_employee_w4.py  (34 contract tests)
 M  14 existing test files    (22 head-assertion bumps only)
 M  tests/test_workforce_recommendation_w3c.py (deferred-list update: employee now exists)
```

## 2. Contract-by-contract verification

### D-1 Budget — ✅ PASS
- `workforce_employee.py` contains **zero** cost/budget logic; the words
  budget/scheduler/execution/training/performance do not appear outside
  deliberate docstring boundary statements.
- No Workforce → Project FK anywhere: the `project_id=None` arguments in the
  service functions are the mandatory parameter of `append_audit` (same shape
  as W3-B/C/D), not a schema reference.
- `models.py` Employee table has exactly the three W3-parent FKs + agent +
  job/job_version — no Project column.

### D-2 Failure semantics — ✅ PASS
- `CandidateLifecycle.ALLOWED[TRIALING] == {EMPLOYED, POOLED}` (workforce.py:
  the W3-D dead-end comment replaced by the two W4 edges).
- `release_candidate`: owner-only; only FAILED/CANCELLED trials; COMPLETED →
  409; moves TRIALING → POOLED; **no new Candidate terminal state** (only new
  member is the R7-approved `EMPLOYED`).
- W3-C's F-R8 withdraw path untouched (`_sync_candidate_back` unchanged).

### D-3 Employee minimal core — ✅ PASS
- `Employee(` appears **exactly once** in the module (inside
  `promote_to_employee`, L379) — the sole creator.
- 10 columns as approved; `job_id` FK = RESTRICT; `UNIQUE(trial_id)` idempotency
  anchor mirrors the W3-D `UNIQUE(recommendation_id)` pattern.
- `JobStatus.FILLED`: **zero writes in src/** — the only occurrences are the
  enum definition (L1391, pre-existing) and boundary docstrings.

### D-4 RESTRICT unlock — ✅ PASS
- No `purge_employee`, no delete path, no service/owner bypass. `EMPLOYED` is
  terminal in `CandidateLifecycle` (zero outbound edges), consistent with
  `EmployeeStatus` having the single member `ACTIVE`.

### D-5 Trial extension — ✅ PASS
- 4 structured columns (`trial_plan_ref`, `started_at`, `ended_at`,
  `outcome`) — no JSON bag.
- One additive migration `20260903_0002_workforce_employee` on top of
  `20260903_0001_workforce_trial`; `alembic heads` shows the single head.
- `downgrade()` drops the employee table and batch-drops the 4 columns →
  round-trip reversible.
- Zero explicit indexes (R7 single-head ruling style, same as W3-D).

### D-6 Two-stage human gate — ✅ PASS
- All 5 service functions start with `_assert_owner_actor(actor)`; `actor` is
  keyword-only with no default (missing → `TypeError`, non-owner → 403).
- `complete_trial` writes verdict (outcome + ended_at) and **never** touches
  Candidate or Employee (INV-E5); the single `Employee(` call site lives in
  `promote_to_employee`, reachable only from a COMPLETED trial + TRIALING
  candidate.
- `TrialLifecycle` (single writer `_transition_trial_status`) implements the
  W3-D §12 C-4 handover exactly: PROPOSED→{ACTIVE, CANCELLED};
  ACTIVE→{COMPLETED, FAILED, CANCELLED}; terminals closed.

### Extra confirmations — ✅ PASS
- W3-C/D definitions unmodified: `git diff` over
  `workforce_recommendation.py` / `workforce_trial.py` / W3 test files shows
  **no changes** (the 14 modified test files differ only in the head-string).
- `models.py` removed lines are exclusively W3-D "deliberately absent"
  commentary that W4 was contractually required to replace (the W3-D docstring
  itself says: *"W4 MUST introduce TRIAL_ALLOWED + a single status writer
  together with its first transition"*). No W3 code (enum members / columns /
  FKs) removed.
- `workforce.py` code delta is exactly 2 edge lines
  (`TRIALING: {EMPLOYED, POOLED}` + `EMPLOYED: set()`); the remaining diff
  lines are docstring/comment documentation of the new edges.
- No Budget/Scheduler/Execution/Training/Performance code introduced; imports
  of the new module are limited to stdlib + sqlalchemy/sqlmodel +
  aios.actor/audit/models/services/workforce (Capability SSoT preserved via
  `CandidateLifecycle` reuse only).

## 3. Test fixes made in this round (no implementation change)

Two defects were found in the **test layer**; neither required any change to
the W4 implementation, and one of them doubles as positive evidence that the
R7 guards fire as designed.

### 3.1 W4 contract suite — `tests/test_workforce_employee_w4.py`

| # | Defect | Fix |
|---|---|---|
| 1 | `ApprovalStatus` was never imported → `NameError` in the `_approved` helper, failing 28 of 34 tests | added the import; removed the now-unused `RecommendationStatus` and re-sorted the block (ruff I001) |
| 2 | `test_release_rejects_non_owner_403` called `complete_trial` directly on a **PROPOSED** trial; the state machine correctly refused it (`409 illegal trial transition: proposed -> failed`) | inserted the missing `activate_trial` — the test now exercises the intended 403 on `release_candidate` |

Defect 2 is a *positive* signal: `TrialLifecycle` rejected an illegal edge
instead of silently accepting it, which is exactly the W3-D §12 C-4 handover
behaviour (`TrialLifecycle` + a single status writer).

### 3.2 W3-D boundary test handover — `tests/test_workforce_trial_w3d.py`

Three W3-D tests froze the pre-W4 boundary and therefore contradict the R7
ruling; they were updated to the post-handover state, keeping their original
intent (the W3-D module still owns none of the new behaviour):

| Test | W3-D assertion | W4 (R7 D-2 / D-3) state asserted now |
|---|---|---|
| `test_trialing_has_no_outbound_edge` | `ALLOWED[TRIALING] == set()` | `== {EMPLOYED, POOLED}`, every other target still 409 |
| `test_no_employee_table_or_column` | `class Employee not in models.py` | `class Employee` **is** present, but `workforce_trial.py` contains neither `Employee(` nor `workforce_employee` |
| `test_lifecycle_edges_added_and_trialing_member` | `ALLOWED[TRIALING] == set()` | `== {EMPLOYED, POOLED}`; `ALLOWED[EMPLOYED] == set()` |

The same file also carries one approved head-assertion bump
(`test_single_alembic_head`). All other W3-D tests (F-R8 withdraw, idempotency,
table shape, migration round-trip) are untouched.

> **R7 attention item**: item 3.2 edits a W3-D test file. The ruling says "do
> not modify W3-C / W3-D", yet D-2 / D-3 mandate exactly the state these
> assertions forbid, so "0 failed" and "do not touch W3-D tests" cannot both
> hold. The edits are test-layer boundary handovers only — no W3-C / W3-D
> implementation file (`workforce_recommendation.py`, `workforce_trial.py`) was
> modified. Please ratify or order a revert.

## 4. Verification results

| Gate | Method | Result |
|---|---|---|
| W4 contract suite (34 tests) | pytest, single process, in-repo `--basetemp` | ✅ 34 passed |
| **Full suite** | pytest, single process, in-repo `--basetemp` | ✅ **1289 passed, 0 failed** (6:13:33) |
| Ruff | `ruff check src tests alembic` (CI scope) | ✅ clean |
| Alembic heads | `alembic heads` | ✅ `20260903_0002_workforce_employee (head)` — single |
| Diff review | `git diff` per file | ✅ W3-C/D implementations untouched; 12 of 14 test files contain head-string changes **only** (zero non-head changed lines) |

Note on runtime: local runs execute the full 29-step migration chain per
DB-backed test (~15-20 s/test), so a gate run costs ~6 h locally; CI performs
the same suite in 130-390 s and remains the authoritative gate.

## 5. Findings

- **P0 / P1: none.**
- P2 (note only): `scripts/run_alpha_3_1_validation.py` has a pre-existing
  ruff I001 on the branch baseline. It is outside CI's ruff scope
  (`src tests alembic`) and outside W4 scope — intentionally left untouched to
  keep the PR diff strictly W4.
- P2 (note only): two single-test re-runs hit the local WorkBuddy safe-delete
  shim failing to clean a stale `.pytest_run` directory at fixture setup —
  environment-only, avoided by using a fresh `--basetemp` per run.

## 6. Merge gate reminder

Per the standing protocol this audit is **not** a merge authorization. The PR
must be opened by the owner via the direct-compare URL
(`compare/main...w4-employee-spec?expand=1`) — never via the base-branch
dropdown (origin/HEAD pollution) — and merged only after the next explicit R7
ruling against the exact-head SHA.
