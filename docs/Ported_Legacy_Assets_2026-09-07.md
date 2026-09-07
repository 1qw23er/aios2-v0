# Ported legacy assets (2026-09-07)

This document records the provenance of code and docs recovered from archived
branches and re-landed on `main`, so future audits do not treat them as
unexplained new work.

## Source

| Item | Source branch | Source commits |
|---|---|---|
| Gap #2 Aimi attribution | `feat/real-agent-identity` | `6ccbd50b` |
| Gap #3 real-agent-identity | `feat/real-agent-identity` | `729d1cf0` |
| W3-D Trial spec + DSH audit | `w3d-trial-spec` | `5e5be067`, `c63f5d53` |

Both source branches are preserved on the remote; nothing was rewritten or
force-pushed. The port re-lands the content on a fresh branch cut from `main`
(`82d209d`) rather than merging the old branches, because `main` has since moved
through W4–W8 + the P1 cost closure and the old branch bases no longer apply.

## What was ported

### Gap #2 — Aimi lead-gen attribution (zero-migration)

`src/aios/attribution.py` + a 14-line integration in `src/aios/content_draft.py`
+ `tests/test_aimi_attribution.py` (14 cases).

- Every article gets a server-minted `attribution_key`, frozen into the draft
  `metadata_json` at creation time. Because metadata is already covered by the
  draft checksum, adding the field does **not** change any existing draft's
  checksum — hence zero migration.
- Signup observations are written to the existing append-only `AuditLog` with
  action `content.aimi_attribution`. No new table, no migration drift.
- Identity rule preserved: the module never resolves an actor itself; every
  audit write takes the already-resolved `actor` from the auth boundary.

### Gap #3 — real-agent-identity for CLI actors

`src/aios/known_agents.py` + `scripts/seed_known_agents.py` +
`tests/test_real_agent_identity.py` (4 cases).

- Gives the local CLI tooling's `workbuddy` / `gpt` actors real registry-backed
  `Agent` rows, so identity becomes registry-validated and fails closed (404)
  when absent.
- Seeding is idempotent and never overwrites operator-tuned rows.
- Zero migration: it only inserts rows at runtime.

### W3-D Trial spec + DSH audit record (docs only)

`docs/workforce/Workforce_W3D_Trial_Spec_V1.md`,
`docs/workforce/DSH_Path3_Audit_Prompt_W3D_V1.md`,
`docs/workforce/Workforce_W3D_DSH_Audit_Report_V1.md`.

`main` already archives the W3-A/B/C, W4 and W6 spec + audit records; the W3-D
pair was the only gap in that chain (only the merge verification report had
landed). Porting the docs restores completeness. Docs only — no code impact.

## Verification performed

- Shared-file patch applied cleanly to `main` (`git apply --check`, no conflict).
- Ported suites: 18/18 passed (`test_aimi_attribution.py` 14,
  `test_real_agent_identity.py` 4).
- Neighbour + boundary suites: `test_workforce_w6_invariants.py`,
  `test_workforce_w7_invariants.py`, `test_content_draft.py`,
  `test_wb_draft_to_aios.py`, `test_gateway_agent_registry.py` — all green
  except the known `test_w7_i14_slice_touches_only_tests`, which fails only
  while `src/` changes are uncommitted and heals after the commit.
- `ruff check src tests alembic` (CI scope): clean.
