# Alpha-3.1 Operational Validation Report

**Date:** 2026-07-16
**Issue:** [#5 Alpha-3.1: Operational validation of end-to-end AIOS workflow](https://github.com/QLM1234/aios-v0/issues/5)
**Runner:** `scripts/run_alpha_3_1_validation.py`
**Test:** `tests/test_alpha_3_1_validation.py`
**Method:** Deterministic, no LLM, no architecture change. Uses only the existing
capability scheduler, ContextService, external workstation adapter, orchestrator,
KnowledgeService, Approval, AuditLog, and transactional outbox.

---

## 1. Scenario

A single persistent AI e-commerce market-analysis Project with four Tasks:

| Task | Routing mode | Depends on |
|------|-------------|------------|
| `tsk_research`  | `best_available` (capability) | — |
| `tsk_planning`  | `best_available` (capability) | `tsk_research` |
| `tsk_writing`   | `best_available` (capability) | `tsk_planning` |
| `tsk_approval`  | `manual` (L4 human approval)   | `tsk_writing` |

Three agents (`agt_research`, `agt_planning`, `agt_writing`) carry the
capability-routed tasks; the L4 approval Task is reserved for a human operator.

---

## 2. Manual interventions

All human touch-points were exercised explicitly (no auto-decision):

1. **Seed world** — 3 agents, 3 capabilities, 1 project, 4 tasks created.
2. **External import** — research result `res_research` imported from the external
   workstation, producing the research Artifact.
3. **Explicit manual approval of the research Artifact** — `review_status`
   flipped to `approved` by a human operator (not by any automated path).
4. **Knowledge review** — submitted 2 KnowledgeCandidates; **approved 1**
   (`market_analysis_series` v1 → KnowledgeFact), **rejected 1**.
   _No LLM was used; the human reviewer (`human_ceo`) made both calls._
5. **L4 Approval requested** at risk `L4`, left **PENDING**. No automatic
   publication and no external L4 action occurred.

---

## 3. Exported / imported packages

- Exported (research Task):
  - `<work>/outbox/tsk_research/task_packet.json`
  - `<work>/outbox/tsk_research/context.md`
- Imported:
  - `<work>/inbox/res_research.json` → Artifact `art_…` (system-generated uuid)

> Artifact IDs are assigned by the system (`new_id("art")`), so they vary per
> run. Task, agent, capability, and KnowledgeFact series IDs are deterministic.

---

## 4. Context hashes (TaskContext per Task)

| Task | Context hash (sha256) |
|------|------------------------|
| `tsk_research`  | `116007eeffc11df9fff879574732ad6a3ec48f800119c9b45dbe4f81963b3084` |
| `tsk_planning`  | `1559cec84d94db98fa517821d546fe7370a3a4475dbfba3c467e77d841e29718` |
| `tsk_writing`   | `c92103fccfd6ad1ecc30d972dddadde5268ac705c8f778bc36f750ab79c9a6fb` |

Every routed Task received a generated `TaskContext`. The planning and writing
contexts both embed the **approved** KnowledgeFact (see §5), proving knowledge
reuse flows downstream.

---

## 5. Routing decisions & fallback

| Task | Mode | Selected agent | Reason | Fallback |
|------|------|----------------|--------|----------|
| `tsk_research`  | `best_available` | `agt_research`  | `best_available_static_priority` | no |
| `tsk_planning`  | `best_available` | `agt_planning`  | `best_available_static_priority` | no |
| `tsk_writing`   | `best_available` | `agt_writing`   | `best_available_static_priority` | no |
| `tsk_approval`  | `manual`        | — (none)        | `manual_assignment_required`     | n/a |

No fallback was triggered: each capability-routed Task found an eligible agent on
the first static-priority pass. The manual L4 Task was correctly **not**
auto-assigned — `route_task` returned `None` and recorded a
`routing.blocked` audit entry with reason `manual_assignment_required`.

---

## 6. Knowledge approval provenance

| Candidate | Decision | KnowledgeFact | Series / Version |
|-----------|----------|---------------|------------------|
| `kcand_…` | approve  | `kfact_…` | `market_analysis_series` v1 |
| `kcand_…` | reject   | — (none)      | — |

Only the **approved** candidate produced a `KnowledgeFact`
(`status = approved`). The rejected candidate is terminal
(`status = rejected`) and created no fact. Consequently, the planning and
writing `TaskContexts` contain exactly **one** approved fact and **zero** entries
from the rejected claim — AIOS enforced knowledge provenance end-to-end.

---

## 7. Counts (persisted, re-verified in a new session)

| Entity | Count |
|--------|-------|
| Projects | 1 |
| Tasks | 4 |
| ExecutionAssignments | 3 (research / planning / writing; L4 = 0) |
| TaskContexts | 3 |
| Artifacts | 3 (research / planning / writing) |
| KnowledgeCandidates | 2 (1 approved, 1 rejected) |
| KnowledgeFacts | 1 (approved) |
| KnowledgeReviewDecisions | 2 |
| Events (outbox) | 10 |
| Pending Approvals | 1 (L4) |
| AuditLogs | 17 |

Persistence was re-verified by reopening a **brand-new database session** after
the workflow completed: every entity above was readable, and the L4 Task still
had no `ExecutionAssignment`.

---

## 8. Workflow friction

- **Manual L4 gate is intentional friction.** The orchestrator activated the L4
  Task to `READY` (correct — its dependency completed), but AIOS refused to
  auto-route or auto-decide it because its `routing_mode = manual`. A human must
  assign the agent and approve. This is the designed safety boundary, not a bug.
- **Artifact approval is a manual step** outside any API; the operator flips
  `review_status`. In a fully automated deployment this would be a documented
  human touch-point.
- No capability fallback, no schema drift, no migration re-run issues were
  observed. The pytest template-DB optimization (PR #3) kept the suite at
  **76 passed in ~29s**.

---

## 9. Coordination impact (evidence-based, neutral)

The runner records the coordination conclusion in
`report.manual_gates_preserved`, **derived from observed state** rather than
asserted up front:

- `l4_assignment == []` — the MANUAL L4 Task received **no** auto-assigned agent
  (`route_task` returned `None`, `manual_assignment_required`).
- `approvals_pending == 1` (L4, `PENDING`) — the high-risk decision is surfaced
  for a human and left untouched by any automated path.

**Conclusion:** the manual gates were *preserved*. AIOS automated the middle of
the pipeline (capability routing, context generation, orchestration, external
export/import, knowledge ingestion) but did **not** remove or bypass the human
checkpoints. Whether this *reduces* total human coordination work is a
deployment-dependent judgment; the validation records the evidence and does not
pre-judge the net effect.

---

## 10. Reproduce

```bash
# Default DB: data/alpha-3-1-validation.db (refuses to overwrite)
python scripts/run_alpha_3_1_validation.py

# Recreate if it already exists
python scripts/run_alpha_3_1_validation.py --reset

# Custom location
python scripts/run_alpha_3_1_validation.py --db /path/to/custom.db
```

The generated SQLite database is **never committed to git** (`data/` is
git-ignored). Integration test:

```bash
python -m pytest tests/test_alpha_3_1_validation.py -q
```
