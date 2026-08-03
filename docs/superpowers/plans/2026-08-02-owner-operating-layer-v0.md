# AIOS Owner Operating Layer V0 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **PLAN-ONLY PR.** This document is the design + acceptance contract for AIOS Owner Operating Layer V0 (OOL V0). The accompanying PR contains **zero runtime code, zero migration, and zero implementation** — only this plan. Implementation follows only after the plan-merge gate (Codex architecture review → CloudCode usability/adversarial review → owner merge authorization). **No auto merge.**
>
> **Re-review revision — Round 4 (correction gate, part 2).** This is the second corrected revision after the OWNER PLAN CORRECTION GATE. The prior Round-3 revision (head `8a1f7e51fca832d8c29dd686c7dda8e6241698a6`) resolved all six findings (P1-1..P1-4, P2-1, P2-2) raised by the CloudCode/DeepSeek adversarial gate (head `14b4105`). The subsequent **independent Codex R4 architecture re-review** of that head returned `REQUEST_CHANGES_ARCHITECTURE_PLAN` (next owner `next:workbuddy`) on a single genuine defect: the P2-2 canonical `facts_binding` formula was only quoted in the historical traceability table (§15.1b) and partially in test T-S10, but the normative `§2.4.1` subsection referenced at line 269 **did not exist** in the plan body — a dangling cross-reference. Round 4 closes that gap by adding the **normative `§2.4.1 Canonical facts_binding`** subsection (full verbatim formula + all required attributes: field set `{ref: fact_revisions[ref]}`, `sorted` key ordering, `ensure_ascii=False`, compact `(",",":")` separators, UTF-8 encoding, `sha256:` prefix, empty-set constant `sha256:44136...aff8a`, integer `version` revision type, order-independence, decision-time `compare_digest`). It also clarifies §3.2 (P1-4) so `owner_confirm_suggestion` calls the adapter primitive `self.adapter.send_message(...)` directly — never the service-level `CustomerService.send_message` dispatcher (which would nest `BEGIN IMMEDIATE`). All other Round-3 resolutions (P1-1..P1-4, P2-1) are unchanged and re-verified. It is built on top of the Round-2 resolution of R2-1..R2-8 and the Round-3 resolution of P1-1..P1-4 + P2-1. **Zero-migration claim re-affirmed** (see §1.5) — the only added dependency is `cryptography` (metadata, not schema).

> **Re-review revision — Round 5 (correction gate, part 3).** This revision closes the three P1 internal contract conflicts and two P2 corrections raised by the **independent Codex R5 architecture re-review** of head `4a580cc24d47b464010a5aacc1d462d3a51ca684` (verdict `REQUEST_CHANGES_ARCHITECTURE_PLAN`, next owner `next:workbuddy`). Findings and resolutions: **(P1-1)** the single overloaded `project` claim could never satisfy both a project-A operating context and a company-wide (`resource_scope = company`) knowledge row, so the triple `token_project == claims.project == live_row_project` equality was impossible for T-K9..T-K15 — RESOLVED by splitting the sealed claims into `operating_project` (the owner's current operating context, frozen from the project-pick bootstrap) and `resource_scope` (the bound resource's authoritative scope, re-derived from the live row) and replacing the triple-check with an 8-step universal scope resolver that computes visibility (same-project OR company-wide OR company-operating-context) and mutability (same-project OR company-operating-context) independently; company-wide Knowledge rows are read-only from a project operating context (no `approve`/`reject`/`classify`/`deactivate` exposed). **(P1-2)** the `project-pick token` did not conform to the universal token schema and the `token_project == claims.project` triple-check was tautological — RESOLVED by defining one shared **`OwnerSealedToken`** AES-256-GCM envelope that wraps a `token_type` discriminator and two claim schemas: `ProjectNavToken` (navigation/bootstrap, purpose `project_select`, no live-row load) and `InboxActionToken` (the existing inbox item token); every OOL request carries exactly one sealed token (picker→inbox→detail→action, no raw project ID in transit); the fictional independent `token_project` comparison is deleted. **(P1-3)** `UNVERIFIED` and `NEEDS_REVISION` both mapped to one composite `request_edit` action that re-ran `update_content_draft` on partial-failure retry — RESOLVED by splitting into a distinct `content.resubmit` purpose (calls only `submit_content_draft`, never double-updates) for `UNVERIFIED` and `content.edit_and_resubmit` (calls `update_content_draft` + `submit_content_draft`) for `NEEDS_REVISION`, each with its own canonical `display_binding` and a 12-step acceptance test. **(P2-1)** T-T4 asserted the non-secret `kid` is absent from rendered output, contradicting the intentionally-decodable clear header — RESOLVED by asserting only that no secret key / raw resource ID / project ID / series / version / checksum / revision / decrypted claims appear separately, while explicitly permitting the public encoded header and testing that header tampering breaks AEAD authentication. **(P2-2)** the `系列 #N` label ranked by the current head's `created_at`, so a replacement head silently relabeled an unrelated series — RESOLVED by ranking series by their **immutable series-root `created_at`** (the earliest `created_at` among the series' facts, unchanged by head replacement) with a deterministic `series_id` tiebreak, and documenting the label as an **ephemeral display-only handle** accompanied by business provenance (statement summary + series id), never used as an auth/mutation identity. All prior R2-1..R2-8 and Round-4 contracts are preserved and re-verified. **Zero-migration claim re-affirmed** (see §1.5) — the only added dependency is `cryptography` (metadata, not schema).

> **Re-review revision — Round 6 (correction gate, part 4).** This revision closes the four findings raised by the **independent Codex R6 architecture re-review** of head `622e544c220e7e9477d027de418f3248be7fddfe` (verdict `REQUEST_CHANGES_ARCHITECTURE_PLAN`, next owner `next:workbuddy`): **(P1-1)** the `ProjectNavToken` could not prove live display freshness because it performed no authoritative Project-row load — RESOLVED by adding a sealed `project_ref` (used only to load the authoritative Project row) + `project_display_binding` (canonical `{project_ref, business_label, updated_at}` → sha256) distinct from `operating_project`, and a mandatory **11-step Project-nav resolver (§2.1.2a)** that loads the Project row by `project_ref`, fail-closed if missing/deleted, verifies the owner may operate it, re-derives the live `operating_project`, and recomputes + compares `project_display_binding` before minting any downstream token. **(P1-2)** the raw `GET /owner/inboxes/{kind}/{rid}` detail route exposed a database/resource ID in the URL — RESOLVED by deleting that route and adding a third sealed schema **`DetailViewToken`** (token_type `detail_view`, `rid` stays encrypted) carried via `POST /owner/inboxes/{kind}/detail` with body `detail_token=...`; the DetailViewToken is not a mutation and is rejected by decision endpoints. **(P2-1)** owner-facing Knowledge provenance referenced `series_id` — RESOLVED by removing every owner-facing `series_id` from §3.4 / §6.5 / T-K17; the internal sealed token binding (§2.4 knowledge blocks) retains `series_id` as the permitted server-side identity. **(P2-2)** T-C8 step 12 permitted a completed independent review to remain `UNVERIFIED` — RESOLVED so a completed review resolves to `REVIEW_PASSED` or `NEEDS_REVISION` (never `UNVERIFIED`), preserving all #108-A / #119 hard contracts. The accepted R5 discriminator architecture (`operating_project`/`resource_scope` split, `OwnerSealedToken` envelope, content `resubmit`/`edit_and_resubmit` split, public `kid`, immutable series-root ordering, one-plan-file scope, no auto merge) is unchanged and re-verified. **Zero-migration claim re-affirmed** — only `cryptography` (metadata) is added; no model/schema/migration change, and the new `project_ref` / `project_display_binding` / `DetailViewToken` are pure token-claim additions over the existing `Project.id` / `name` / `updated_at` columns (`models.py:231-247`).

> **Re-review revision — Round 8 (correction gate, part 6).** This revision closes the three P1 findings raised by the **independent Codex R8 architecture re-review** of head `11e503a11d4c744dae917f717843293d728283bd` (verdict `REQUEST_CHANGES_ARCHITECTURE_PLAN`, next owner `next:workbuddy`): **(P1-1)** the single overloaded `project_nav` token conflated *project selection* with *project navigation / inbox entry*, leaving the `POST /owner/project-pick` → `GET /owner/inboxes` lifecycle and replay/endpoint-targeting undefined — RESOLVED by splitting the navigation token into two explicit schemas sharing the one `OwnerSealedToken` AES-256-GCM envelope (clear authenticated `kid` header, existing key-management/rotation — **no second crypto system**): **`ProjectSelectToken`** (`token_type="project_select"`, purpose `project_select`, the *only* token accepted by `POST /owner/project-pick`, whose 13-step resolver mints a fresh **`ProjectContextToken`**) and **`ProjectContextToken`** (`token_type="project_context"`, recommended V0 purpose `project_inbox`, the *only* token accepted by `GET /owner/inboxes/{kind}`). Together with the already-accepted `DetailViewToken` and `InboxActionToken` there are now **four** one-type-one-purpose schemas; the normative chain is Picker → ProjectSelectToken → `POST /owner/project-pick` → ProjectContextToken → `GET /owner/inboxes/{kind}` → DetailViewToken → `POST /owner/inboxes/{kind}/detail` → InboxActionToken → `POST /owner/inboxes/{kind}/decide`. **(P1-2)** the universal resolver demanded an `inbox`/`kind` before token-type dispatch (impossible for the selection/context tokens) — RESOLVED by restructuring into three explicit phases: **PHASE 1** envelope/common-field validation (envelope version, alg, kid, AEAD auth, canonical decode, `token_type` present, owner present, iat/exp, authenticated owner == token owner) with **no** schema-specific field required yet; **PHASE 2** whitelist token-type dispatch (`project_select`/`project_context`/`detail_view`/`inbox_action` only; unknown → fail closed); **PHASE 3** schema-specific validation (per-type REQUIRED/FORBIDDEN field sets, allowed purpose, allowed endpoint, allowed method; reject missing-required, extra-forbidden, token_type↔purpose mismatch, endpoint mismatch, method mismatch, schema confusion; extra forbidden claims are **never** silently ignored). **(P1-3)** the plan invented non-existent per-project ACL / revocation states ("owner authorization revoked after mint") that V0 does not have — RESOLVED by freezing the **complete** V0 Project authorization model: the *only* success condition is a successfully authenticated owner acting on a **LIVE** Project row whose sealed `project_ref` resolves to that exact Project and whose `project_display_binding` is currently valid; there is **no** ProjectOwner table, no per-project ACL, no membership list, no per-project role, no revocation mapping, no hidden session ACL, and the fictional "revoked after mint" test is deleted. **COMPANY OPERATING CONTEXT IS FORBIDDEN IN V0:** `operating_project == "company"` is rejected at token mint, token resolution, navigation, detail, and owner action; no V0 endpoint may mint any of the four tokens with `operating_project=company`; the universal resolver's `if operating_project == 'company': allow broader mutation` branch is deleted (it was a normative V0 behavior — now removed). Company-wide Knowledge stays read-only from a project context (R5/R6 accepted contract, **not** reopened; it does not imply `operating_project=company`). The accepted R6 contracts (Project-row authoritative load, `project_ref`/`project_display_binding`, `DetailViewToken` no raw detail id, no owner-facing `series_id`, REVIEW_PASSED/NEEDS_REVISION completion, `operating_project`/`resource_scope` split, content `resubmit`/`edit_and_resubmit` split, public `kid`) are all preserved. **Zero-migration claim re-affirmed** — only the `cryptography` dependency-metadata addition; all four token schemas use the existing `OwnerSealedToken` envelope and existing `Project.id`/`name`/`updated_at` columns (no new persistent field, no new model, no Alembic migration).

**Owner gate:** `APPROVED TO PLAN` — non-technical owner may operate normal AIOS business flows without knowing or manually relaying any internal system identity (Artifact ID / Conversation ID / Message ID / Suggestion ID / checksum / revision / enum / canonical tag / series·version / API route / SQL / Git / Alembic).

**Goal:** A *thin* operating layer that surfaces four business-language owner inboxes — **Content Decisions, Customer-Service Decisions, Feedback Decisions, Knowledge Decisions** — and lets the owner `approve / reject / edit / escalate` each item using only human-readable labels. Every precise internal identity, CAS binding, authorization check, and AuditLog evidence stays server-side.

**Issue:** GitHub #121 (created alongside this PR)
**Baseline HEAD this plan is written against:** `d282c4daba63e7aa926dbbe177d5fceaf4c7deb9` (post #119 squash-merge)
**Alembic single head at baseline:** `20260731_0001` (unchanged by OOL V0; zero migration)
**Plan revision HEAD this PR was last reviewed against (R2):** `c2ee55945aab2d92566df92ccd0f93b142587568`
**Plan revision HEAD reviewed by the CloudCode/DeepSeek independent adversarial correction gate:** `14b41052e13cae8eb40f98e828e64c548ce346b9` (verdict `REQUEST_CHANGES_ARCHITECTURE_PLAN`, next owner `next:workbuddy`; resolved P1-1..P1-4 + P2-1 + P2-2).
**Plan revision HEAD reviewed by the independent Codex R4 architecture re-review:** `8a1f7e51fca832d8c29dd686c7dda8e6241698a6` (verdict `REQUEST_CHANGES_ARCHITECTURE_PLAN`, next owner `next:workbuddy`; single defect: dangling `§2.4.1` reference for P2-2 `facts_binding` — formula was quoted only in the historical traceability table §15.1b and partially in test T-S10, not in the plan body; closed by this Round-4 revision adding the normative `§2.4.1`).
**Plan revision HEAD reviewed by the independent Codex R5 architecture re-review:** `4a580cc24d47b464010a5aacc1d462d3a51ca684` (verdict `REQUEST_CHANGES_ARCHITECTURE_PLAN`, next owner `next:workbuddy`; findings: P1-1 operating_project vs resource_scope split, P1-2 OwnerSealedToken envelope + ProjectNavToken/InboxActionToken, P1-3 content resubmit/edit_and_resubmit split, P2-1 public `kid` test, P2-2 series label stability — all resolved by this Round-5 revision).
**Plan revision HEAD for the next Codex architecture re-review (new exact head, R6):** recorded in the PR #122 re-review request comment and in the Issue #121 traceability comment (it is the head of `plan/owner-operating-layer-v0` after this Round-5 revision is pushed — it is external to this file, so it is not self-embedded here).

---

## 1. Scope & non-goals

### 1.1 In scope (V0, deliberately thin)
- Four owner inboxes aggregating items that await a **human decision**.
- Business-language rendering of each pending item (no internal IDs in the UI).
- Owner `approve / reject / edit / escalate` actions that bind the exact server-side identity **without the owner copying anything**.
- Human-readable errors when an action cannot be taken (stale / already-decided / not-authorized / token invalid).
- Audit + traceability visibility (read-only) so the owner can see *what was decided and by whom* after the fact.
- A relay metric `OWNER_RELAY_COUNT` (defined §9.1) measuring how often the owner is still forced to relay an internal identity or technical state.

### 1.2 Explicit exclusions (owner gate)
- **No** real WeCom channel wiring (CS stays on the Mock adapter, as today).
- **No** D1 (闲鱼/电商代运营) channel automation.
- **No** new publication platforms (no real WeChat/小红书 auto-post in V0).
- **No** payments / auto-pricing / production external actions.
- **No** architecture redesign — OOL V0 is a thin presentation + binding layer over the **existing** service modules (`content_draft.py`, `customer_service.py`, `feedback.py`, `knowledge_service.py`, `review.py`), reusing their transaction/CAS/audit contracts verbatim.
- **No** weakening of any approval or authorization contract. Every hard gate from #108-A / #109 / #110 / #119 / #74 / #88 is preserved.
- **No** general admin dashboard — V0 is not a system console for agents, secrets, review policies, or measurement. Those keep their existing owner surfaces.
- **No** KnowledgeFact created by content approval; **no** real external publication; **no** paid-LLM call on the happy path (#108-A / #109 / #110 / #119).

### 1.3 Hard invariant (absolute rule)
> **The owner MUST NEVER manually copy/paste an internal system identity** (Artifact ID, Conversation ID, Message ID, Suggestion ID, checksum, revision, enum value, canonical tag ID, series·version, API route, SQL, Git SHA, Alembic id) to advance a normal business flow.

If a flow still requires the owner to relay such an identity, that flow is **incomplete** and must be fixed before V0 is accepted. This invariant also extends to **technical state relay**: the owner must never copy, remember, translate, or transfer any internal ID / checksum / revision / enum between pages, agents, or tools (see §9.1 / R2-8).

### 1.4 Architecture decisions carried forward (APPROVED, unchanged)
The following conclusions are **retained** from Round-1 and Round-2 and are not reopened:
1. **Four narrow inboxes** (Content / CS / Feedback / Knowledge), each a distinct decision surface — not one combined admin queue.
2. **Existing services remain authoritative.** OOL V0 never re-implements CAS / transaction / audit; it only translates a business-label click into the correct existing call with server-bound identities.
3. **No admin dashboard** for agents / secrets / review policy / measurement.
4. **No real WeCom / D1 / publication / payment / paid-LLM / production action** in V0.
5. **No bypass of `KnowledgeFact` creation rules** — content approval never creates a KnowledgeFact.
6. **Human-readable error mapping** is a first-class deliverable, not an afterthought.
7. **Plan-only + owner gate**: no implementation ships until the plan-merge gate (Codex → CloudCode → owner) passes. **No auto merge.**
8. **Stateless AEAD token, chosen over HMAC-signed and over a persistent opaque handle** (R2-1). The token is a *reference + display-consent binding*, never an auth / authz / bearer capability.

### 1.5 Zero-migration claim — re-evaluation (BLOCKER §ZERO-MIGRATION)
After selecting the token model (§2.1, **AES-256-GCM stateless sealed token**) the zero-migration claim is **re-affirmed**, with explicit evidence and one honest correction vs. the Round-1 plan:

- **MODEL requires no persistence.** The token is a self-contained AEAD ciphertext; the server holds only the encryption key(s) (env / secret-store), not a token table. No new table, no Alembic migration.
- **CS one-shot reuses the existing `CsSuggestion.consumed: bool` column** (`models.py:914`, already present). No new column.
- **Project scope is derived from the existing `project_id` / `source_project_id` FK** on every resource row (`Artifact.project_id` models.py:382; `Conversation.project_id` 873; `CsSuggestion.project_id` 907; `Feedback`/`Artifact` carry `project_id` + `checksum`/`revision_count`; `KnowledgeCandidate.source_project_id` 698). No new FK.
- **The only required dependency addition is `cryptography`** (declared in project metadata — `pyproject.toml` + lockfile — in the implementation PR, §11). This is a **dependency-metadata change, not a schema/migration change**. The Round-1 plan incorrectly implied the implementation diff contained only OOL Python files; this revision corrects that (R2-1).
- **The only required configuration** is the token key env vars (§2.1.3) — credentials, not schema.
- **Single Alembic head `20260731_0001` is preserved.** `git diff --stat` for the implementation PR will show the 4 OOL files + the `customer_service.py` one-shot extension + `tests` + `pyproject.toml`/lock (§11).

**Replay safety is NOT provided by the token itself.** The token is stateless and short-lived; it does not prevent replay on its own. Replay safety is provided by the composition of (a) **action-specific token binding** (a token is valid for exactly one `(inbox, kind, purpose, rid)`), (b) **decision-time live-state comparison** (the bound `display_binding` must equal the re-derived live state, §2.4), and (c) **existing domain terminal / idempotency / concurrency semantics** (`CsSuggestion.consumed`, `#109` `idempotency_key`, `BEGIN IMMEDIATE` CAS, `#110`/`#108-A` revision/checksum checks). If a future revision ever discovers that durable replay state is actually required, that is an explicit owner architecture decision requiring a new migration — it is **not** silently added here.

---

## 2. Architecture (thin, binding-only)

OOL V0 adds **one new module + one console section + one API group**, all delegating to existing services.

```
src/aios/
  owner_inbox.py        # NEW (thin): token seal/resolve + 4 inbox queries + action adapters
src/aios/api/
  owner_inbox_routes.py # NEW (thin): GET inboxes, POST decisions (all owner-authed)
src/aios/console.py     # EXTEND: /owner/inboxes hub + 4 inbox pages (server-rendered)
```

**No new models. No new Alembic migration.** `owner_inbox.py` only *reads* existing rows (Artifact / Conversation / CsSuggestion / Feedback / KnowledgeCandidate / KnowledgeFact / Approval / AuditLog) and *calls* existing service methods. All CAS/transaction/audit semantics live in the underlying service; OOL V0 never re-implements them.

### 2.1 `inbox_item_token` — stateless AES-256-GCM sealed reference  [R2-1 / F1]

**Round-1 root cause (recap):** the previous design used an HMAC-*signed* token that embedded the raw primary key in its claims. HMAC provides *integrity/authenticity only* — not confidentiality or opacity — so the "internal ID" was not actually hidden, and a signed token cannot carry an encrypted `kid` for key selection. **HMAC is rejected.**

**Round-2 root cause (R2-1):** the Round-1 AEAD draft put `kid` *inside* the encrypted plaintext but defined the token as `nonce ‖ ciphertext`, so the server could not select the decryption key before decrypting; it also used `XChaCha20Poly1305` from an **undeclared `cryptography` dependency** that the project does not currently have. **Both are corrected below.**

**Selected envelope — AES-256-GCM, cleartext `kid` header as AAD (Owner Architecture Decision A).**

**Token wire structure (three base64url segments, joined by `.`):**
```
<base64url(canonical_header)> . <base64url(nonce)> . <base64url(ciphertext_and_tag)>
```
- `canonical_header` = cleartext JSON, **supplied byte-for-byte as the AEAD AAD**:
  ```json
  {"v": 1, "kid": "<key-id>", "alg": "A256GCM"}
  ```
  `kid` is **cleartext on purpose**: the server must select the decryption key *before* decrypting. `kid` is **not secret**. `alg` is fixed to `"A256GCM"`.
- `nonce` = 12 random bytes (`AESGCM` standard nonce size).
- `ciphertext_and_tag` = `AESGCM(key).encrypt(nonce, plaintext_bytes, associated_data=canonical_header_bytes)` — 16-byte tag appended by the library.
- All three segments use **base64url without padding**, one unambiguous encoding (no "base64 or raw").

**Encrypted plaintext — shared `OwnerSealedToken` envelope (canonical JSON, sorted keys):** one AES-256-GCM wire format (§2.1) wraps a `token_type` discriminator plus a claims object. **Four** claim schemas share the envelope — there is **no second token format** and no second crypto system (the same AEAD key-management contract, §2.1.1, covers all four):

- **`ProjectSelectToken`** (`token_type = "project_select"`): the *project-selection* token (purpose `project_select`). It is the **only** token accepted by `POST /owner/project-pick`. It carries **no business resource binding** — the Project-select resolver (§2.1.2a) validates it by loading the authoritative Project row and, on success, **mints a fresh `ProjectContextToken`** (it does **not** itself grant inbox access). It binds **no `(kind, rid)` business row**. It is **stateless and NOT cryptographically one-time** — replaying it at most mints an equivalent fresh `ProjectContextToken` and MUST NOT change any business/domain state (V0 has no consumed-once table; "consumed once" wording is deleted). Claims:
```json
{
  "v": 1,
  "token_type": "project_select",
  "owner": "<owner_id>",
  "project_ref": "<sealed internal Project.id — used ONLY to load the authoritative Project row; never exposed to the owner>",
  "operating_project": "<the picked project id, frozen at bootstrap — the operating-context binding>",
  "project_display_binding": "<sha256-hex of the canonical project-label binding (§2.4 project-label binding)>",
  "purpose": "project_select",
  "iat": 1764567890,
  "exp": 1764568790
}
```
- **`ProjectContextToken`** (`token_type = "project_context"`): the *project-navigation / inbox-entry* token. It is **only** accepted by `GET /owner/inboxes/{kind}?ctx=<ProjectContextToken>` (the allowlisted inbox-list endpoints + kinds — it is **never** accepted by `POST /owner/project-pick`, the detail endpoint, or any decision/mutation endpoint). Its recommended V0 purpose is `project_inbox` (the operating-context binding for listing that project's inbox). It carries **no business resource (`kind`/`rid`) binding** — the Project-context resolver (§2.1.2b) re-validates the authoritative Project row but grants only inbox *listing*, not any item action. Claims:
```json
{
  "v": 1,
  "token_type": "project_context",
  "owner": "<owner_id>",
  "project_ref": "<sealed internal Project.id — used ONLY to load the authoritative Project row; never exposed to the owner>",
  "operating_project": "<the picked project id, frozen at bootstrap — the operating-context binding>",
  "project_display_binding": "<sha256-hex of the canonical project-label binding (§2.4 project-label binding)>",
  "purpose": "project_inbox",
  "iat": 1764567890,
  "exp": 1764568790
}
```
- **`DetailViewToken`** (`token_type = "detail_view"`): the exact-resource detail-view token (purpose `view_detail`). It is **NOT** authentication, **NOT** authorization, and **NOT** a mutation — it only permits resolution of the exact displayed detail candidate after live authorization + stale-state checks (§2.1.2b). It is **rejected by every decision/mutation endpoint** (purpose mismatch). The `rid` stays encrypted inside the token; it is never placed in a URL, route, HTML, JSON, log, referrer, or relay. Claims:
```json
{
  "v": 1,
  "token_type": "detail_view",
  "owner": "<owner_id>",
  "operating_project": "<frozen operating context (from the project_select → project_context bootstrap)>",
  "resource_scope": "<authoritative scope of the bound resource: its project_id, or 'company' sentinel if project_id IS NULL (Knowledge only)>",
  "inbox": "content",
  "kind": "artifact",
  "rid": "<internal resource id, encrypted>",
  "purpose": "view_detail",
  "display_binding": "<sha256-hex of the canonical display-binding JSON (§2.4)>",
  "iat": 1764567890,
  "exp": 1764568790
}
```
- **`InboxActionToken`** (`token_type = "inbox_action"`): the decision token (navigation into an item + the owner decision). Claims:
```json
{
  "v": 1,
  "token_type": "inbox_action",
  "owner": "<owner_id>",
  "operating_project": "<project context the owner is operating in, frozen from the project_select → project_context bootstrap (company operating context is forbidden in V0 — see §2.1.2 / §6.4)>",
  "resource_scope": "<authoritative scope of the bound resource: its project_id, or 'company' sentinel if project_id IS NULL (Knowledge only)>",
  "inbox": "content",
  "kind": "artifact",
  "rid": "<internal resource id, encrypted>",
  "purpose": "approve",
  "display_binding": "<sha256-hex of the canonical display-binding JSON (§2.4)>",
  "iat": 1764567890,
  "exp": 1764568790
}
```
- `rid`, `operating_project`, `resource_scope`, `project_ref`, `project_display_binding`, `display_binding` are **confidential** — encrypted, never shown.
- `display_binding` is the **mandatory cryptographic consent binding** (§2.4, R2-2) for business resources; `project_display_binding` is its Project-nav analogue (§2.4 project-label binding). Both are SHA-256 digests of canonical JSON, stored *inside* the AEAD ciphertext so they are authenticated and tamper-evident.
- `project_ref` and `operating_project` are **distinct proofs** (P1-1): `project_ref` is the sealed internal Project identity used *only* to locate the authoritative Project row; `operating_project` is the frozen operating-context binding the token claims. The resolver loads the row by `project_ref` and **independently re-derives** the live `operating_project` from the loaded row, then compares — the two are never collapsed into one (a `project_ref`→Project A with a token `operating_project`→Project B is rejected, §2.1.2a step 7).
- `operating_project` is the owner's **navigation/operating context**, frozen into the token at mint time from the project-pick bootstrap (§4.1). It is **never** the bound resource's own scope and is **never** used as the resource's auth scope (P1-1).
- `resource_scope` is the bound resource's **authoritative scope**, re-derived at decision time from the live row (§2.1.3 / §4.1) — for Knowledge this is `project_id` (NULL ⇒ `company` sentinel), **never `source_project_id`** (R2-4 / P1-3). For Content/CS/Feedback rows it equals the row's concrete `project_id` (those kinds always belong to exactly one project).
- `operating_project` and `resource_scope` are **distinct fields**; the resolver keeps them separate at every step (P1-1). There is no single `project` claim overloaded for both meanings.

**Sealing (normative):**
- Cipher: `AESGCM` from `cryptography.hazmat.primitives.ciphers.aead` (implementation dependency MUST be declared — §11 / R2-1). **NOT** `XChaCha20Poly1305`. **NOT** an undeclared dependency.
- Plaintext: `json.dumps(claims, separators=(",",":"), sort_keys=True, ensure_ascii=False)` → UTF-8. The `claims` object MUST include `token_type` (`"project_select"`, `"project_context"`, `"detail_view"`, or `"inbox_action"`) so the resolver (§2.1.2) dispatches by PHASE 2 whitelist to the correct PHASE 3 validation path (`project_select` → §2.1.2a Project-select resolver, `project_context` → §2.1.2b Project-context resolver, `detail_view` → §2.1.2c Detail-view resolver, `inbox_action` → §2.1.2d Inbox-action resolver).
- Header: `json.dumps({"v":1,"kid":kid,"alg":"A256GCM"}, separators=(",",":"), sort_keys=True)` → UTF-8; this exact byte string is the AAD.
- `nonce = secrets.token_bytes(12)`; `ct = AESGCM(key).encrypt(nonce, plaintext, aad=header_bytes)`.
- Token = `.`.join([b64url(header), b64url(nonce), b64url(ct)]), all base64url no-padding.
- Minted server-side when rendering an inbox page / list item; never stored. TTL short (default **900 s = 15 min**, configurable via `AIOS_OOL_TOKEN_TTL_SECONDS`).

**Binding contract (authoritative — every sub-point is a hard requirement):**
1. **Token is NOT authentication.** Owner authentication (`authenticate_owner`, §5) runs **first** and is a hard prerequisite. Token resolution runs *after* auth. The token conveys *which resource + which purpose + which display-consent*, never *who is calling*.
2. **Token grants no authority.** Authority comes solely from owner auth + the existing per-project / per-artifact service-level guards + the server-issued project context (§4.1). The token only selects the target; the service decides whether the action is permitted.
3. **Server re-evaluates authorization & project scope at decision time** (§4.1). The token never carries a usable mutable `project_id`; `resource_scope` is re-derived from the live row and compared against `operating_project` (frozen from the **sealed `project_select` → `project_context` bootstrap**, stateless — §4.1 / P1-2). The two fields are never collapsed into one (P1-1).
4. **Decision-time re-reads authoritative state** (§2.3 / §2.4). The token binds a resource id *and a display-consent digest*; the server re-loads the live row, re-derives the display-binding, and compares before acting.
5. **Precise endpoint × inbox × action × purpose binding.** Each decision endpoint declares its allowed `(inbox, kind, purpose)`. The resolved token's `inbox`/`kind`/`purpose` MUST match exactly; a mismatch is rejected (no implicit reuse of a token for a different action).
6. **Canonical serialization** (above) — no ad-hoc field ordering; deterministic so verification is unambiguous.
7. **Constant-time verification.** AEAD tag check is constant-time internally; any owner/string/ digest comparison uses `secrets.compare_digest`.
8. **Malformed / expired / wrong-* handling** (§2.1.4 step 3) — all collapse to a single uniform human-readable message ("该条目已失效，请刷新收件箱") with **no distinguisher**.
9. **Maximum clock skew** — `±30 s` tolerance on `iat`/`exp` to absorb minor skew; beyond that the token is rejected.
10. **Key loading / rotation / prev-key window / fail-closed** — see §2.1.3.
11. **Secret never exposed** — the key is never in the token, never logged, never echoed in any response or AuditLog. `display_binding` digest and `rid`/`project` are never surfaced either.
12. **The token does NOT itself prevent replay** (§1.5). Replay safety = action-specific binding + live-state comparison + existing domain terminal/idempotency/concurrency semantics.

#### 2.1.1 Key configuration contract (fail-closed)  [R2-1]
**One encoding only:** every AES key is configured as **base64url (no padding)**. After decoding there must be **exactly 32 bytes**. There is no "base64 or raw" alternative.

**Configuration names (normative):**
| Env var | Required | Meaning |
|---|---|---|
| `AIOS_OOL_TOKEN_CURRENT_KID` | yes | key-id label of the current key (cleartext, appears in header `kid`) |
| `AIOS_OOL_TOKEN_CURRENT_KEY_B64` | yes | base64url of exactly 32 bytes — the current AES-256 key |
| `AIOS_OOL_TOKEN_PREVIOUS_KID` | optional | key-id label of the previous (rotating) key |
| `AIOS_OOL_TOKEN_PREVIOUS_KEY_B64` | optional* | base64url of exactly 32 bytes — the previous key (*required iff PREVIOUS_KID set) |
| `AIOS_OOL_TOKEN_PREVIOUS_ACCEPT_UNTIL` | optional* | absolute UTC ISO-8601 deadline (`2026-09-01T00:00:00Z`) until which previous-key tokens are accepted (*required iff PREVIOUS_KID set) |
| `AIOS_OOL_TOKEN_TTL_SECONDS` | optional | token lifetime, default 900 |

**Rotation / acceptance rules (all hard):**
- current `kid` and previous `kid` MUST differ.
- current key MUST decode to exactly 32 bytes; previous key, when configured, MUST decode to exactly 32 bytes.
- previous key REQUIRES an explicit absolute UTC acceptance deadline; tokens sealed under the previous key are accepted **only until that deadline**; once the deadline passes, the previous key is **deterministically rejected** (even if still configured).
- **unknown `kid` fails closed.**
- **invalid / missing current key configuration fails closed** → `503 owner_token_key_unavailable` (mirrors owner-auth misconfig in `security.py`). Under no circumstance does the server fall back to a hardcoded / empty / default key.
- **malformed deadline fails closed.**
- **no automatic unlimited previous-key fallback**; **no try-every-random-key behavior** (the key ring is exactly {current} ∪ {previous-within-window}).
- key bytes **never** appear in token claims / logs / errors.

**Startup + runtime failure semantics:**
- At process startup, validate current key decodes to 32 bytes; if previous configured, validate its 32 bytes + distinct `kid` + parseable absolute UTC deadline. Any failure → process refuses to start token mint/verify (fail-closed `503`).
- At runtime, if the current key becomes unloadable (e.g., secret-store eviction), mint/verify returns `503` — never a silent downgrade.
- Rotation procedure: set `PREVIOUS_* = old`, `CURRENT_* = new`; re-mint new tokens under the new `kid`; old tokens stay verifiable until `PREVIOUS_ACCEPT_UNTIL`; after the deadline remove `PREVIOUS_*`.

#### 2.1.2 Token resolution protocol — three-phase (P1-1 / P1-2)

There is **one** AES-256-GCM wire format (`OwnerSealedToken` envelope, §2.1) and **four** `token_type` schemas — `project_select`, `project_context`, `detail_view`, `inbox_action` — each with **one** purpose class and **no implicit token promotion** between them. The resolver is restructured into three explicit phases so that token-type dispatch never requires a schema-specific field first (P1-2):

- **PHASE 1 — Envelope / common-field validation** (no schema-specific field yet): authenticate owner; split `.`-segments; decode + AEAD-verify the header; select key by `kid` (current, or previous-within-window); `AESGCM` decrypt; canonical-decode the claims JSON. Validate only the fields **every** `OwnerSealedToken` shares: `v==1`, `token_type` present, `owner` present, `iat`/`exp` (±30s skew), and `authenticated owner_id == claims.owner`. No `inbox`/`kind`/`purpose` matching happens here.
- **PHASE 2 — Whitelist token-type dispatch:** `token_type` MUST be one of `project_select` / `project_context` / `detail_view` / `inbox_action`; **any other value → fail closed** (uniform message). Dispatch to the matching PHASE 3 schema resolver (§2.1.2a / §2.1.2b / §2.1.2c / §2.1.2d).
- **PHASE 3 — Schema-specific validation:** each schema defines its own REQUIRED / FORBIDDEN field set, allowed `purpose`, allowed endpoint, and allowed method (normative table below). Reject if a REQUIRED field is missing, a FORBIDDEN field is present (extra forbidden claims are **never** silently ignored), `token_type`↔`purpose` mismatch, endpoint mismatch, or method mismatch. Schema confusion (a token of one schema presented to another schema's endpoint) is caught here.

> **V0 envelope invariant:** `operating_project == "company"` is **forbidden** in V0 and is rejected in PHASE 1/PHASE 3 at every mint and every resolution. No V0 endpoint mints any of the four tokens with `operating_project=company`; the Project-select / Project-context / Detail-view / Inbox-action resolvers all reject a `company` operating context. Company operating context is described **only** in the explicitly-marked **FUTURE / NON-NORMATIVE** note (§6.4) and does not participate in any V0 resolver, route, test, or acceptance.

**Normative schema field table (V0) — one definition per schema:**

| Schema (`token_type`) | REQUIRED claim fields | FORBIDDEN / must NOT carry | Allowed `purpose` | Allowed endpoint(s) | Allowed method |
|---|---|---|---|---|---|
| `project_select` | `owner`, `project_ref`, `operating_project`, `project_display_binding`, `purpose` | `inbox`, `kind`, `rid`, any resource `resource_scope`, any mutation-action field | `project_select` | `POST /owner/project-pick` | POST |
| `project_context` | `owner`, `project_ref`, `operating_project`, `project_display_binding`, `purpose` | `inbox`, `kind`, `rid`, any resource `resource_scope`, any inbox-action / mutation `display_binding` | `project_inbox` (V0) | `GET /owner/inboxes/{kind}?ctx=<token>` | GET |
| `detail_view` | `owner`, `operating_project`, `resource_scope`, `inbox`, `kind`, `rid`, `purpose`, `display_binding` | any inbox-action / mutation purpose (must be `view_detail` only) | `view_detail` | `POST /owner/inboxes/{kind}/detail` | POST |
| `inbox_action` | `owner`, `operating_project`, `resource_scope`, `inbox`, `kind`, `rid`, `purpose`, `display_binding` | — (purpose must be the exact action) | an exact action (approve / reject / resubmit / edit_and_resubmit / classify / deactivate / adopt_and_send / escalate / set_lead_stage / assign_human / create_followup_task / apply_transition / mark_duplicate / reject_feedback / ...) | `POST /owner/inboxes/{kind}/decide` | POST |

The four schemas are mutually exclusive at the endpoint level: a `project_select` token is rejected by the inbox-list, detail, and decide endpoints; a `project_context` token is rejected by `POST /owner/project-pick`, the detail endpoint, and the decide endpoint; a `detail_view` token is rejected by `POST /owner/project-pick`, the inbox-list GET, and the decide endpoint; an `inbox_action` token is rejected by `POST /owner/project-pick`, the inbox-list GET, and the detail POST.

**§2.1.3 Universal scope resolver — `operating_project` vs `resource_scope` (P1-1 / R5, company branch deleted):** for the two business-resource schemas (`detail_view` / `inbox_action`), after the row is loaded, visibility and mutability are computed independently (no single overloaded `project` claim, no `company` operating context in V0):
- `operating_project` = `claims.operating_project` (frozen at mint from the `project_select` → `project_context` bootstrap).
- `resource_scope` = the authoritative scope of the live row: Content/CS/Feedback → the row's concrete `project_id` (never NULL); Knowledge → `project_id` if not NULL, else the `'company'` sentinel (`project_id IS NULL` ⇒ company-wide). **`source_project_id` is NEVER used as scope.**
- **Visibility:** `resource_scope == operating_project` (same project) OR `resource_scope == 'company'` (company-wide visible from any project context). Otherwise → fail closed.
- **Mutability:** `resource_scope == operating_project` (same-project mutation) **only**. A `resource_scope == 'company'` row is **READ-ONLY** from a project context (`operating_project != 'company'`): its mutation purposes (`classify`/`approve`/`reject`/`deactivate`) are withheld at the inbox-query stage (§6.4) and any such token is rejected (uniform message). *(The R5 `if operating_project == 'company': allow broader mutation` normative branch is deleted — V0 has no company operating context; see §6.4 FUTURE note.)*
- **No single `project` field** — `operating_project` and `resource_scope` stay distinct at every step (P1-1 / R5).

**Common resolution steps (PHASE 1 + PHASE 2 + dispatch to PHASE 3):**
```
1. authenticate_owner()  → ActorContext(kind="owner", owner_id)                 # fail-closed 401/503
2. split token on '.' → [header_b64, nonce_b64, ct_b64]; base64url-decode each  # fail-closed uniform on any decode error
3. json.loads(header) → {v, kid, alg}; require v==1, alg=="A256GCM";
   select key by header.kid (current, or previous-within-PREVIOUS_ACCEPT_UNTIL window)
   - any failure (malformed b64, unknown kid, expired previous window) → uniform "该条目已失效，请刷新收件箱" (NO distinguisher)
4. AESGCM(key).decrypt(nonce, ct, associated_data=canonical_header_bytes) → plaintext  # AEAD auth failure → uniform message (NO distinguisher)
5. json.loads(plaintext) → claims
   - unknown schema version v                          → reject (uniform message)
   - claims.token_type missing                         → fail closed (PHASE 2 dispatch impossible)
   - claims.owner missing / != authenticated owner_id  → 403-equivalent (uniform message)
   - exp exceeded (beyond ±30s skew)                   → uniform message
   # --- PHASE 1 complete: only common fields validated; NO inbox/kind/purpose check here ---
6. PHASE 2 — if claims.token_type NOT IN {project_select, project_context, detail_view, inbox_action}
     → fail closed (uniform message).  (unknown token_type never reaches a schema resolver)
7. PHASE 3 — dispatch to the schema resolver (§2.1.2a/§2.1.2b/§2.1.2c/§2.1.2d); the resolver enforces
     its REQUIRED/FORBIDDEN fields, allowed purpose, allowed endpoint, allowed method, and
     (for business-resource schemas) the §2.1.3 universal scope resolver. Extra forbidden claims
     are rejected, never silently ignored.
```

**§2.1.2a Project-select resolver (`project_select` — mandatory Project-row load, P1-1/P1-3):** After PHASE 1, the selection token is resolved by loading the authoritative Project row — it is **never** trusted without this load. The resolver performs exactly these 13 steps (any failure → fail-closed uniform message):
```
1. require `token_type == "project_select"`.
2. exact endpoint / `purpose` match (purpose == "project_select", endpoint == `POST /owner/project-pick`); mismatch → reject.
3. REQUIRED-field check: `owner`, `project_ref`, `operating_project`, `project_display_binding` present; FORBIDDEN check: `inbox`/`kind`/`rid`/`resource_scope`/mutation-action fields absent (schema confusion → reject).
4. load the authoritative **LIVE** Project row using the sealed `project_ref` (Project.id).
5. missing / deleted / not-found / non-live Project → fail closed (uniform "该条目已失效，请刷新收件箱").
6. V0 Project authorization is **simple and explicit**: the *only* success condition is a successfully authenticated owner (PHASE 1) acting on a **LIVE** Project row whose sealed `project_ref` resolves to that exact Project. There is **NO** ProjectOwner table, NO per-project ACL, NO membership list, NO per-project role, NO revocation mapping, NO hidden session ACL. (The fictional "owner authorization revoked after mint" test/state is deleted — V0 has no such authority state.)
7. derive the authoritative live `operating_project` identity from the loaded row (the row's own stable `Project.id`).
8. verify it matches the token's `operating_project` (secrets.compare_digest); a `project_ref`→A with token `operating_project`→B is rejected here — `project_ref` and `operating_project` are never collapsed.
9. recompute the live `project_display_binding` from the loaded row (§2.4 project-label binding).
10. compare to the token's `project_display_binding` (secrets.compare_digest).
11. mismatch (project renamed / relabeled / `updated_at` changed after mint) → stale / fail closed (uniform "该条目已变更，请刷新收件箱").
12. enforce V0 `operating_project != "company"` (rejected here with a uniform message; company context is FUTURE-only, §6.4).
13. on success, **mint a fresh `ProjectContextToken`** (purpose `project_inbox`, embedding the verified `operating_project` + `project_ref` + `project_display_binding`) and render the inbox hub. The `ProjectSelectToken` itself is stateless and NOT cryptographically one-time: replaying it only mints an equivalent fresh `ProjectContextToken` and MUST NOT change any business/domain state.
```
No `(kind, rid)` business-row load occurs in this resolver (the select token binds no business resource); the Project-row load is mandatory and authoritative for `operating_project` and `project_display_binding`.

**§2.1.2b Project-context resolver (`project_context` — inbox entry, P1-1):** After PHASE 1, the context token is resolved to **list** that project's inbox — it grants **no** item action. The resolver performs exactly these 13 steps (any failure → fail-closed uniform message):
```
1. require `token_type == "project_context"`.
2. exact endpoint / `purpose` match (purpose == "project_inbox", endpoint == `GET /owner/inboxes/{kind}`); mismatch → reject (a `project_context` token is NOT accepted by `POST /owner/project-pick`, the detail endpoint, or any decide endpoint).
3. REQUIRED-field check: `owner`, `project_ref`, `operating_project`, `project_display_binding` present; FORBIDDEN check: `inbox`/`kind`/`rid`/`resource_scope`/mutation-action fields / inbox-action `display_binding` absent (schema confusion → reject).
4. load the authoritative **LIVE** Project row using `project_ref`.
5. missing / deleted / non-live Project → fail closed.
6. V0 Project authorization (simple/explicit, same as §2.1.2a step 6 — no ACL/revocation).
7. derive live `operating_project` from the loaded row; compare to token's `operating_project` (compare_digest).
8. recompute `project_display_binding`; compare to token's (compare_digest).
9. mismatch → stale / fail closed.
10. enforce `operating_project != "company"`.
11. resolve the requested `{kind}` against the **allowlisted** inbox kinds (content / cs / feedback / knowledge) — `kind` is NOT taken from the token (it is a URL path param validated against the allowlist); unknown kind → fail closed.
12. list decisionable items **within that project only** (no global cross-project aggregation); company-wide Knowledge rows are visible-but-read-only per §6.4.
13. mint each list item a `DetailViewToken` (for detail navigation) — NOT an `InboxActionToken` (item actions are minted only on the detail page, §2.1.2c).
```

**§2.1.2c Detail-view resolver (`detail_view` — exact resource view, P1-2):** After PHASE 1, the detail token is resolved (it is **not** a mutation and is rejected by decision endpoints):
```
1. require `token_type == "detail_view"` and `purpose == "view_detail"`.
2. require the endpoint is the detail endpoint (POST /owner/inboxes/{kind}/detail) and claims.inbox / claims.kind match the endpoint; mismatch → reject (uniform message).
3. REQUIRED-field check: `owner`, `operating_project`, `resource_scope`, `inbox`, `kind`, `rid`, `purpose`, `display_binding` present; FORBIDDEN check: any inbox-action / mutation purpose (it is `view_detail` only) → reject.
4. verify the authenticated owner is authorized for claims.operating_project (P1-3 simple project auth).
5. enforce `operating_project != "company"`.
6. load the live row by (claims.kind, claims.rid); if missing → 404-equivalent "该条目已不存在".
7. §2.1.3 universal scope resolver — visibility must pass (a hidden / cross-project row is not viewable).
8. recompute display_binding from the live row (§2.4); secrets.compare_digest against claims.display_binding → mismatch → fail-closed uniform "该条目已变更，请刷新收件箱".
9. only after steps 1–8 pass, render the detail page and mint fresh action-specific InboxActionToken(s) for the owner's valid decisions (each with its own purpose).
```
A `DetailViewToken` submitted to any decision endpoint (`POST /decide`) is rejected at step 1–2 (purpose `view_detail` != endpoint's allowed action); an `InboxActionToken` submitted to the detail endpoint is rejected at §2.1.2c step 2 (purpose != `view_detail`). A `DetailViewToken` submitted to `POST /owner/project-pick` or the inbox-list GET is also rejected (endpoint mismatch). The tokens are never interchangeable.

**§2.1.2d Inbox-action resolver (`inbox_action` — decision, P1-1/P1-2):** After PHASE 1, the action token is resolved for the owner's decision:
```
1. require `token_type == "inbox_action"` and an exact action `purpose`.
2. require the endpoint is `POST /owner/inboxes/{kind}/decide` and claims.inbox / claims.kind match; mismatch → reject.
3. REQUIRED-field check: `owner`, `operating_project`, `resource_scope`, `inbox`, `kind`, `rid`, `purpose`, `display_binding` present.
4. V0 Project authorization (simple/explicit): the operating-context Project must be a LIVE row the authenticated owner may operate (resolved via the frozen `operating_project`, itself derived from a verified Project row upstream).
5. enforce `operating_project != "company"`.
6. load the live row by (claims.kind, claims.rid); if missing → 404-equivalent.
7. §2.1.3 universal scope resolver — visibility + mutability must pass (a company-wide row is read-only from a project context → mutation purpose rejected).
8. recompute display_binding (§2.4); compare_digest → mismatch → fail closed.
9. invoke the underlying service with server-bound identities (token supplies rid; server supplies project_id / checksum / revision / consumed / canonical pick).
10. allow the service's CAS (BEGIN IMMEDIATE / checksum / revision / consumed) to be the FINAL authority; translate stale / conflict / already-decided into human language (§8).
```
An `InboxActionToken` submitted to `POST /owner/project-pick`, the inbox-list GET, or the detail POST is rejected (endpoint mismatch).

The owner never sees `rid` / `kind` / `purpose` / `kid` / `operating_project` / `resource_scope` / `project_ref` / `project_display_binding` / `display_binding` as editable text. The `rid` travels only inside the sealed token (hidden form field / request body) — it is **never** placed in a URL, route, HTML, JSON, log, referrer, or relay (P1-2).

### 2.2 Server-side binding semantics (what OOL hides)
| Inbox | Underlying service call today | Identity OOL binds server-side |
|---|---|---|
| Content | `ContentDraftService.approve_content_draft(artifact_id, actor, review_checksum, review_revision)` | Looks up `artifact_id` from token, then reads the **current** `independent_review.reviewed_checksum` + `reviewed_revision` from the row and passes them — owner only clicks "批准". |
| Content (reject) | `reject_content_draft(artifact_id, actor, reason)` | `artifact_id` from token + owner-typed reason. |
| Content (resubmit) | `submit_content_draft(artifact_id, actor)` (UNVERIFIED only) | `artifact_id` from token; calls **only** `submit_content_draft` — no `update_content_draft`, no `revision_count` bump (P1-3). |
| Content (edit_and_resubmit) | `update_content_draft(artifact_id, actor, new_body)` + `submit_content_draft(artifact_id, actor)` (NEEDS_REVISION only) | `artifact_id` from token + owner-edited body; re-runs independent review; item returns when next review passes. |
| CS (send) | `owner_confirm_suggestion(conversation_id, suggestion_id, actor, edited_text=None)` (authoritative one-shot, own single `BEGIN IMMEDIATE` transaction — **NOT** wrapping `_human_send`, §3.2 / P1-4) | `conversation_id` + `suggestion_id` from token; consumed + idempotency + display-binding enforced server-side; outbound audit id `audit:cs:outbound:owner:send:{suggestion_id}`. |
| CS (escalate) | `CustomerService.escalate(conversation_id, ...)` | `conversation_id` from token. |
| CS (lead stage) | `set_lead_stage(conversation_id, stage, ...)` | `conversation_id` from token + owner-picked **business label** mapped to `LeadStage` server-side. |
| Feedback (approve) | `FeedbackService.apply_transition(..., APPROVE_SOLUTION)` | `artifact_id` from token; transition + current stage + checksum/revision resolved server-side. |
| Feedback (reject/defer/etc.) | `apply_transition(..., REJECT_SOLUTION\|DEFER\|REJECT_FEEDBACK\|MARK_DUPLICATE)` | `artifact_id` from token; for `MARK_DUPLICATE` the owner picks the canonical item from a **server-rendered list** → server resolves `canonical_feedback_id` (same-project only). |
| Knowledge (approve) | `KnowledgeService.review_candidate(candidate_id, APPROVE, rationale, series_id, version, supersedes_fact_id)` | `candidate_id` from token; server calls `next_version(series_id, project_id)` to derive `version` and resolves `supersedes_fact_id` from the current approved head — owner only picks a rationale + (optionally) an existing series from a dropdown. |
| Knowledge (reject) | `review_candidate(candidate_id, REJECT, rationale)` | `candidate_id` from token + owner-typed rationale. |
| Knowledge (classify) | `classify_candidate_tags(candidate_id, tags)` | `candidate_id` from token; owner picks canonical tags from the **7 canonical** set, server calls `normalize_tags`. |
| Knowledge (deactivate) | `deactivate_fact(fact_id, rationale)` | `fact_id` from token + owner-typed rationale. |

Nothing the owner types becomes a raw PK, checksum, revision, enum string, series/version, or canonical-tag ID. All such values are **server-derived from the token + the live row**.

### 2.3 Stale-state binding: DISPLAY TIME vs DECISION TIME  [R2-2 / F2]
OOL V0 **never treats the displayed snapshot as authoritative.** Each inbox item is rendered from a point-in-time projection; the authoritative state is always **re-read at decision time** (§2.1.2 PHASE 1–3 + §2.1.3). The protection is two-layered:

1. **Live-state re-validation** — re-read the authoritative row and require it is still in the expected decisionable state (status / checksum / revision / consumed).
2. **Cryptographic display-consent binding** — the exact business state the owner *saw* at render time is frozen as a SHA-256 digest inside the sealed token (`display_binding`, §2.4). At decision time the server re-derives the same digest from the live row and compares; **any mismatch fails closed** and the adapter **never substitutes a newer live binding into the old owner action**.

**Universal decision protocol (applies to all four inboxes):**
1. Authenticate owner (§5).
2. Resolve + verify token (§2.1.2).
3. Verify token `owner` / `inbox` / `kind` / `purpose` match the endpoint.
4. Re-read the **authoritative** live row by `rid`.
5. Resolve project scope via the universal resolver (§2.1.3): compute `operating_project` vs `resource_scope` visibility + mutability; do **not** assert a single `project` equality (P1-1).
6. Recompute `display_binding` from the live row; `secrets.compare_digest` against the token's bound digest; **mismatch → fail closed**.
7. Call the existing service method (which holds the CAS / transaction / audit).
8. Allow the service's CAS to make the final accept/reject decision.
9. Translate any `ServiceError` (stale / already-decided / not-authorized / conflict) into the human-readable message (§8) — never a raw ID, SQL state, or stack trace.

### 2.4 Canonical `display_binding` (mandatory, per inbox/action)  [R2-2 / Decision B]
**Normative serialization:** every binding is a JSON object with an **explicit, fixed field set per inbox/action** (listed below — no "hash current state" hand-waving). Serialize with `json.dumps(binding, sort_keys=True, ensure_ascii=False, separators=(",",":"))` (the same canonical helper used in `feedback.py` / `customer_service.py`), then `display_binding = sha256(canonical_bytes).hexdigest()`. There is **no dict-order-dependent hashing** — `sort_keys` makes the bytes deterministic. The token stores this hex digest (inside the AEAD ciphertext). At decision time the server recomputes the digest from the live row and constant-time-compares.

> `kind` here is the resource kind string used in the token (`artifact` / `suggestion` / `conversation` / `feedback` / `candidate` / `fact`); `rid` is the bound primary key.

### 2.4.1 Canonical `facts_binding` (CS suggestion consent binding)  [P2-2]

`facts_binding` is a **second, independent canonical digest** that freezes the exact set of KnowledgeFact revisions a CS suggestion was built against. It is **NOT** a `display_binding` (which freezes *visible* business state, §2.4); it freezes the *provenance* of the facts embedded in the suggestion text, so a silently-changed fact revision is detected at decision time.

**Normative formula (authoritative — implement verbatim):**

```python
import hashlib, json

def canonical_facts_binding(knowledge_fact_refs, fact_revisions):
    # knowledge_fact_refs: list[str] of KnowledgeFact.id (or stable fact refs)
    # fact_revisions: dict[ref -> int version]  (version is the int `version`
    #   column of KnowledgeFact; see customer_service.py:367)
    canonical_facts = json.dumps(
        {ref: fact_revisions[ref] for ref in sorted(knowledge_fact_refs)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical_facts).hexdigest()
```

**Required attributes (all mandatory):**
- **Field set:** exactly `{ref: fact_revisions[ref]}` — each referenced fact mapped to its bound revision value. No extra keys, no tags, no `series_id`.
- **Key ordering:** `sorted(knowledge_fact_refs)` — ascending by ref. Makes the digest **order-independent**: `["f2","f1"]` and `["f1","f2"]` produce the identical `facts_binding`.
- **Serialization:** `json.dumps(..., sort_keys=True, separators=(",",":"), ensure_ascii=False)` — compact (no spaces), Unicode-preserving (Chinese fact text kept as-is, not `\uXXXX`).
- **Encoding:** `.encode("utf-8")` before hashing.
- **Hash + prefix:** `sha256` over the UTF-8 bytes; the literal prefix **`sha256:`** is prepended to the hex digest.
- **Revision value type:** `fact_revisions[ref]` is the **integer `version`** of the KnowledgeFact (customer_service.py:367). Never a string, never a timestamp.
- **Empty set:** `knowledge_fact_refs == []` → `canonical_facts = b"{}"` → fixed constant `facts_binding = "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"`.
- **Decision-time:** recompute `canonical_facts_binding` from the live `fact_revisions`; `secrets.compare_digest` against the token-bound `facts_binding`; mismatch → **fail closed** (the suggestion's fact provenance changed).

Referenced by `cs.adopt_and_send` (§2.3 token claim `facts_binding`, below) and covered by test **T-S10** (§12).

**CONTENT — `content.approve` / `content.reject`:**
```json
{
  "kind": "artifact",
  "rid": "<artifact.id>",
  "review_status": "<metadata_json.review_status>",          // e.g. "REVIEW_PASSED"
  "reviewed_revision": "<metadata_json.independent_review.reviewed_revision>",
  "reviewed_checksum": "<metadata_json.independent_review.reviewed_checksum>"
}
```
- The card the owner clicked froze the exact `reviewed_revision` + `reviewed_checksum` + `review_status` they viewed.
- **Decision-time:** re-read artifact; recompute digest; mismatch → fail closed. Required test (R2-2): owner opens a **rev2 REVIEW_PASSED** card → producer edits → **rev3** → rev3 independently reaches **REVIEW_PASSED** → owner clicks the **old rev2 card** → **REJECT as stale**; the adapter MUST NOT read rev3 and substitute rev3's checksum/revision into the old rev2 action.

**CUSTOMER SERVICE — `cs.adopt_and_send` (owner_confirm_suggestion):**
```json
{
  "kind": "suggestion",
  "rid": "<suggestion.id>",
  "decision": "<suggestion.decision>",                       // "human_confirm"
  "text_sha256": "sha256:<suggestion.text>",
  "facts_binding": "<sha256-hex of the canonical facts JSON — see §2.4.1>",
  "consumed": false,
  "conv_suggestion_count": "<count of cs_suggestion rows for this conversation at render>"
}
```
- Uses **only existing persisted authoritative data** (`CsSuggestion.id/text/decision/knowledge_fact_refs/fact_revisions/consumed`, `models.py:902-917`); no new OOL state invented.
- `conv_suggestion_count` catches the "replaced suggestion returns to the same decision state" case: if a newer suggestion was created for the conversation, the live count exceeds the bound count → fail closed.
- **Decision-time:** re-read suggestion; require `consumed == false` (else already-handled, §3.2); compare digest; compare live `conv_suggestion_count`; mismatch → fail closed. Old card + a replaced/new `HUMAN_CONFIRM` suggestion (still unconsumed) → fail closed.

**CUSTOMER SERVICE — `cs.escalate` / `cs.set_lead_stage` / `cs.assign_human` / `cs.create_followup`:**
```json
{
  "kind": "conversation",
  "rid": "<conversation.id>",
  "lead_stage": "<conversation.lead_stage>",
  "suggestion_count": "<count of cs_suggestion rows for this conversation at render>"
}
```
- **Decision-time:** re-read conversation; compare digest + `suggestion_count`; mismatch → fail closed (stale conversation state).

**FEEDBACK — `feedback.approve_solution` / `feedback.reject_solution` / `feedback.defer` / `feedback.mark_duplicate` / `feedback.reject_feedback`:**
```json
{
  "kind": "feedback",
  "rid": "<artifact.id>",
  "stage": "<metadata_json.stage>",                          // "AWAIT_OWNER_APPROVE"
  "revision": "<artifact.revision_count>",
  "checksum": "<artifact.checksum>"
}
```
- Binds the exact pending solution the owner saw (`artifact.checksum` covers the A-zone including `solution_text`, `feedback.py:287-297`; `artifact.revision_count` is the solution revision round, `feedback.py:23-24`).
- **Decision-time:** re-read artifact; compare digest; mismatch → fail closed. Old card must fail after a **new solution reaches AWAIT_OWNER_APPROVE again** (new submit bumps `revision_count` + recomputes `checksum` → digest mismatch).

**KNOWLEDGE — `knowledge.approve_candidate`:**
```json
{
  "kind": "candidate",
  "rid": "<candidate.id>",
  "status": "<candidate.status>",                            // "DRAFT"
  "statement_sha256": "sha256:<candidate.statement>",
  "series_id": "<candidate.series_id or ''>",
  "head_version": "<current approved head version for series_id at render (re-derived)>"
}
```
- Binds the exact candidate decision state the owner viewed, including the head version the approval would supersede.
- **Decision-time:** re-read candidate; compare digest; re-derive `head_version` from the live approved head for the series; mismatch → fail closed (a changed/replaced/already-terminal candidate, or a moved head, cannot be approved via the stale card).

**KNOWLEDGE — `knowledge.deactivate_fact`:**
```json
{
  "kind": "fact",
  "rid": "<fact.id>",
  "status": "<fact.status>",                                 // "APPROVED"
  "version": "<fact.version>",
  "statement_sha256": "sha256:<fact.statement>",
  "series_id": "<fact.series_id>"
}
```
- **Decision-time:** re-read fact; compare digest; mismatch → fail closed (a changed/replaced/already-terminal fact cannot be deactivated via the stale card).

**PROJECT — `project_select` / `project_context` (project-label binding)  [P1-1]:**
```json
{
  "project_ref": "<Project.id — stable internal identity>",
  "business_label": "<Project.name — authoritative current owner-facing business label>",
  "updated_at": "<Project.updated_at ISO-8601 — freshness signal>"
}
```
- Uses **only existing authoritative Project columns** (`models.py:231-247`): `id` (stable identity), `name` (owner-facing business label), `updated_at` (freshness). **No new persistent Project version field is invented** (zero-migration, §1.5).
- **Canonicalization (identical rules to every other binding, §2.4):** `project_display_binding = sha256(json.dumps({...}, sort_keys=True, ensure_ascii=False, separators=(",",":")).encode("utf-8")).hexdigest()`.
- **Invariant:** a project rename / relabel / `updated_at` change recomputes a different `project_display_binding`; an old `ProjectSelectToken`/`ProjectContextToken`'s bound digest no longer matches the live row → fail closed (§2.1.2a step 9–11 / §2.1.2b step 8–9). `project_ref` is included so the binding is bound to the exact Project identity, not just a label (a label collision across projects cannot masquerade as another project).
- **Decision-time:** recomputed from the live Project row loaded by `project_ref` (§2.1.2a); `secrets.compare_digest` against the token-bound `project_display_binding`; mismatch → fail closed.

---

## 3. Owner user journeys (the four inboxes)

### 3.1 Content Decisions  [R5 / F5 — honest edit contract]
1. Owner opens **Content Decisions** inbox → sees CONTENT_DRAFT items in the three pending states `REVIEW_PASSED` (awaiting approval), `UNVERIFIED` (edited, awaiting resubmit), and `NEEDS_REVISION` (review returned low-confidence, needs re-edit), each shown as a business label (e.g. *"《<topic>》— 第 N 轮独立复审已通过，待你批准"* for `REVIEW_PASSED`; *"《<topic>》— 编辑已保存，待重新送审"* for `UNVERIFIED`; *"《<topic>》— 复审未通过，待你重新编辑"* for `NEEDS_REVISION`).
2. Owner clicks an item → reads the draft body (read-only) + the independent review summary (reviewer, result, reason) + the audit trail (read-only).
3. Owner chooses **批准** / **驳回（填理由）** / **重新送审** (only when status `UNVERIFIED`) / **编辑并重新送审** (only when status `NEEDS_REVISION`). The two edit actions are **distinct server-bound purposes** (P1-3 — never one composite `request_edit`):
   - 批准 → server binds reviewed_checksum+revision (frozen in the token `display_binding`), calls `approve_content_draft`.
   - 驳回 → server calls `reject_content_draft(reason)`.
   - **重新送审** (`content.resubmit`, status `UNVERIFIED` only) → server calls **only `submit_content_draft`**; it does **NOT** call `update_content_draft`, does **NOT** bump `revision_count`, does **NOT** re-archive/clear state. The item is already edited-and-saved (`UNVERIFIED`); the owner merely re-triggers the independent review.
   - **编辑并重新送审** (`content.edit_and_resubmit`, status `NEEDS_REVISION` only) → server calls `update_content_draft` (owner edits body in a text box) + `submit_content_draft` to re-run independent review; item returns to inbox when next review passes.
4. **Edit-honesty contract (R2-5, refined by P1-3):** the edit action is **renamed to the truthful business operation** (`content.edit_and_resubmit`, "编辑并重新送审") because the owner actually **rewrites the draft body and immediately triggers another independent review** — it is **NOT** a request handed to the producer. A distinct `content.resubmit` ("重新送审", UNVERIFIED only) was added in Round 5 so a mere resubmit never re-runs `update_content_draft`. The contract is explicit:
   - It modifies the **draft body only** via the existing `update_content_draft` + `submit_content_draft` path. It does **not** silently re-approve, does **not** bypass the independent review, and does **not** change review status to APPROVED.
   - **Partial-failure recovery (authoritative service behavior — Option A, no change to `content_draft.py`):**
     - **`resubmit` (UNVERIFIED):** (a) the owner clicks *重新送审*; the server calls **only `submit_content_draft`**. (b) If `submit_content_draft` fails (exception before the review commits), the artifact **stays in `UNVERIFIED`** and the UI shows *"编辑已保存，但重新送审未成功，请重试送审。"* — **never** "复审中", and the owner resubmits without re-discovering the Artifact ID. `update_content_draft` is **NOT** called again, so `revision_count` is **NOT** incremented and state is **NOT** re-archived/re-cleared. (c) Only if `submit_content_draft` *commits the review* and the review returns low-confidence does the artifact become `NEEDS_REVISION`. The owner then re-opens and re-edits (`edit_and_resubmit`).
     - **`edit_and_resubmit` (NEEDS_REVISION):** (a) `update_content_draft` succeeds → `content_draft.py:434` sets `artifact.review_status = UNVERIFIED` (the owner edit is recorded; **not** `NEEDS_REVISION`); the Content inbox query, §6.1, **includes** `UNVERIFIED`, so the item does **not** vanish. (b) `submit_content_draft` then fails (exception before the review commits) → the artifact **stays in `UNVERIFIED`** with the edited body; UI shows *"编辑已保存，但重新送审未成功，请重试送审。"*; the owner re-opens the `UNVERIFIED` item and **resubmits** (`resubmit` purpose) — **not** a second `edit_and_resubmit` — so `update_content_draft` is not called a second time. (c) Only if `submit_content_draft` *commits the review* and the review returns low-confidence does the artifact become `NEEDS_REVISION` again — a distinct, still-inbox-visible state.
     - In all three pending states (`REVIEW_PASSED` / `UNVERIFIED` / `NEEDS_REVISION`) the item remains in the Owner Inbox; the owner advances without copying any internal ID. The two edit actions are never collapsed into one composite that re-runs `update_content_draft` on a mere resubmit (P1-3).
   - OOL V0 introduces **no second edit transaction model**. The resulting re-review is the same `submit_content_draft` flow any agent uses; OOL only hides the IDs.
   - All #108-A / #119 gates remain: on re-approval the server binds the **new** `reviewed_checksum`+`reviewed_revision`; stale review → 409; review `idempotency_key` unchanged schema; no KnowledgeFact created.
5. No internal ID is ever shown or copied.

### 3.2 Customer-Service Decisions  [R2-3 / F3 — ONE replay contract]
1. Owner opens **Customer-Service Decisions** inbox → sees conversations with a pending `HUMAN_CONFIRM` suggestion or an `ESCALATE` flag, each shown as: *"客户对话 #<customer_ref> — AI 建议：<suggestion_text>（置信度 X%，待你确认/转人工）"*.
2. Owner clicks an item → reads the recent messages (PII-redacted on the surface) + the suggested reply + why it was flagged.
3. Owner chooses **采用并发送** / **转人工** / **标记阶段（visitor→lead→qualified→proposal→won）** / **分配负责人** / **建跟进任务**.
4. **CS one-shot contract (R2-3) — ONE authoritative definition, used identically by §3.2 / §7 / §10.2 / §12 T-S* / §14 Task 4 / §13:**
   The owner send is a single method:
   `owner_confirm_suggestion(conversation_id, suggestion_id, actor, edited_text=None)` that:
   - (a) binds the **current** suggestion + decision to the authoritative live row (resolved from the token's `rid`, never owner-typed);
   - (b) **re-validates freshness of knowledge facts** against the live state, reusing the *logic* of the existing `_auto_send` stale-fact-recheck (`customer_service.py:501`) — applied inside its **own** transaction (see the single-transaction rule below), **not** by calling `_auto_send`/`_human_send` as a nested call;
   - (c) enforces **one-shot consumption** by setting the existing `CsSuggestion.consumed = True` (`models.py:914`) inside the **same single transaction**, so a repeated click / concurrent session cannot double-send;
   - (d) **terminal-replay contract (CANONICAL):** after one outbound owner action has successfully reached its terminal/consumed state, the **same action submitted again MUST NOT send again, MUST NOT call the adapter again, MUST NOT create a second outbound `Message`, MUST NOT create a second successful-send audit**, and returns a **stable domain conflict (409 "already handled")** translated to human language *"这条回复已经处理，无需再次发送。"* (It does **NOT** return the previous successful result — the Round-1 "returns the prior result" wording is deleted as contradictory to #109.);
   - (e) preserves adapter ambiguity / failure semantics **unchanged** (#109): on adapter failure the send is rolled back (consumed stays False) and the owner sees "发送失败，未重复发送" (§8); escalation writes audit but triggers **NO** production action;
   - (f) honors per-project 403 and PII redaction in the outbound audit `after_snapshot` unchanged;
   - **(g) #109 guardrails preserved (authoritative):** authenticated owner → project authorization → exact current `HUMAN_CONFIRM` suggestion → `consumed == False` → one-shot → exact outbound text binding → no duplicate successful send → full auditability → fail closed. Every one is a hard requirement of `owner_confirm_suggestion`.
   - **Single coherent transaction (P1-4 — critical):** `owner_confirm_suggestion` is **one** authoritative domain operation that opens **its own single `BEGIN IMMEDIATE` transaction** and performs, within that single boundary, the guard checks (a)/(b)/(g), the send (calling the adapter primitive `self.adapter.send_message(...)` directly — **NOT** the service-level `CustomerService.send_message` dispatcher, and **NOT** by calling the existing `_human_send`, `customer_service.py:453`, which owns its **own** `BEGIN IMMEDIATE` / rollback boundary at `:464`), the `consumed = True` mark (c), and the outbound audit write. It MUST **NOT** wrap or call `_human_send()` (or `_auto_send`) from inside another transaction — doing so would nest `BEGIN IMMEDIATE` and break the CAS/rollback contract. This is a minimal `CustomerService` domain extension (not OOL business logic); `owner_inbox.py` merely calls it. It reuses the existing `consumed` column + `idempotency_key` + `send_message` primitives — **no new model/column**, zero migration.
   - **Audit identity (P1-4):** the owner send writes **exactly one** outbound audit with `idempotency_key = "audit:cs:outbound:owner:send:{suggestion_id}"`. This is **distinct** from `CsSuggestion.idempotency_key` (which identifies *suggestion creation*, models.py:915) — the two MUST NOT be conflated; `AuditLog.idempotency_key` remains **globally UNIQUE**. On replay (consumed already True) no second audit row is written (contract d).
   - **Owner edit of HUMAN_CONFIRM text** (if the owner edits the suggested reply before sending) is bounded to the suggestion text passed as `edited_text` to `owner_confirm_suggestion`; the edit is validated (non-empty, length-bounded, no cross-field injection) and tagged for audit. There is **no pre-existing "owner-only suggestion-edit path"** — the edit is simply the `edited_text` argument; the plan does **not** claim a non-existent path.
   - **External-delivery honesty (P1-4):** OOL V0 CS runs on the **MockWeCom** adapter (`customer_service.py`), so the plan guarantees **DB-level one-shot / idempotency** (single `consumed` flip + single audit) but **explicitly does NOT claim exactly-once real external delivery** — there is no real WeCom channel in V0 (#109 / §1.2). The 409 "already handled" contract is enforced at the database/domain layer, not by the mock adapter.
5. Every owner outbound send is audited with `redact_pii` under the owner-send audit id `audit:cs:outbound:owner:send:{suggestion_id}` (§3.2) — unchanged PII redaction; the human-send audit id `audit:cs:outbound:human:{msg.id}` and auto-send id remain as today.

### 3.3 Feedback Decisions
1. Owner opens **Feedback Decisions** inbox → sees FEEDBACK items in `AWAIT_OWNER_APPROVE`, shown as: *"用户反馈：<original_text 摘要> — 方案已就绪，待你批准"*.
2. Owner clicks an item → reads scenario / expected_outcome / solution_text / risk_tags (PII-redacted, business-labeled per §6.5) + the cluster summary (if any).
3. Owner chooses **批准方案** / **驳回方案** / **暂缓** / **标记重复（从列表选 canonical）** / **拒绝该反馈**.
   - Each maps to `FeedbackService.apply_transition` with the correct named verb (`FeedbackTransition`); `artifact_id` + stage resolved server-side. Only named verbs accepted — never a bare stage string (#110).
   - `MARK_DUPLICATE` canonical picker is **server-rendered from same-project feedback only** (#110 per-artifact same-project 403); the owner picks a business label, server resolves `canonical_feedback_id`. Cross-project pick → 409.
   - **Display-binding freshness:** the token's `display_binding` freezes `artifact.checksum` + `artifact.revision_count` + `stage` (§2.4). A new solution submit bumps both → old card fails closed (R2-2 same-status replacement covered by T-F* / T-X*).
4. Cluster runs remain a separate, owner-triggered action (no auto-cluster in V0).

### 3.4 Knowledge Decisions  [R2-7]
1. Owner opens **Knowledge Decisions** inbox → sees `KnowledgeCandidate` in `DRAFT` (awaiting classify/review) and approved `KnowledgeFact` eligible for deactivation, shown as: *"知识候选 #<project-scoped-seq>：<statement 摘要> — 待你分类/审定"* or *"知识事实 #<project-scoped-seq> — 待你停用"*.
   - **No `version` in the owner surface.** Approved facts are shown by a **project-scoped sequence handle** (`知识事实 #<seq>`), never `知识事实 vN` (the Round-1 `vN` display conflict is removed; `version` stays internal only).
2. Owner clicks a candidate → reads the statement + legacy-sentinel warning (if unclassified) + suggested canonical tags (business-labeled per §6.5).
3. Owner chooses **审定通过（选系列）** / **驳回** / **先分类（勾选 canonical tags）** / **停用事实（填理由）**.
   - Approve: server derives the target **series** from the owner's business-label selection (dropdown of existing series shown by **business label**, not id — see §6.5) + `version` via `next_version` + `supersedes_fact_id` from current head.
   - Classify: server calls `normalize_tags` on the owner's checkbox selection (7 canonical tags, business-labeled).
4. **`detail_ref` must NOT be derived from `series_id`·`version`** (F7). The display handle is a **project-scoped sequence / created-order label** (e.g. "知识候选 #3"), independent of the internal series/version identity. The owner never sees `series_id` or `version` as a value.
5. **Series business label — SINGLE deterministic algorithm (R2-7 / P2-1 / P2-2 stability):** the `KnowledgeFact` model stores only `series_id` (`models.py:755`) — there is **no separate persistent business-name field**. V0 therefore renders a series by **one** deterministic algorithm (no ambiguity, no "dominant tag" guesswork):
   - **Step 1 — domain label (if available):** if the domain already supplies a human-readable series label for `series_id` (e.g. a stored slug/name), render that label verbatim.
   - **Step 2 — deterministic fallback:** otherwise render `"系列 #" + str(N)`, where `N` is the 1-based position of this series within the **authoritative scope** (the operating project context, including company-wide NULL), computed by ordering all series in that scope by their **immutable series-root `created_at`** ascending, with `series_id` ascending as the deterministic tiebreak. The **series-root `created_at`** is the **earliest `created_at` among the facts that belong to that series** — it is **immutable under head replacement** (a replacement head has a newer `created_at`, but the earliest fact in the series does not move), so a new head for series X **cannot** silently relabel an unrelated series Y (P2-2). `N` is server-derived and stable for a given scope + facts.
   - The label is an **ephemeral display-only handle**; it is **never** used as an auth or mutation identity. It is accompanied by **business provenance** (the candidate/fact statement summary + business series label), so the owner can make an informed selection even though the `#N` handle may shift across versioning. The internal series identity stays internal; every mutation resolves from the **sealed internal binding** in the token, never from the displayed label. In V0 there is no domain-supplied series label, so the fallback `"系列 #N"` is always used.
   - **No new column / no migration** is introduced for series names. If a persistent editable series name is later required, that is an explicit owner architecture decision requiring a new column + migration — it is **not** taken in V0 (flagged, not silently added).
   - **Stability test (T-K17):** adding a replacement head to series X (newer `created_at`) must **not** change the `#N` label of an unrelated series Y; the label is derived from series-root `created_at`, not replacement-head `created_at`.

---

## 4. Read / write boundaries

| Surface | Read | Write (owner action) |
|---|---|---|
| Content inbox | CONTENT_DRAFT rows pending-owner (`REVIEW_PASSED`/`UNVERIFIED`/`NEEDS_REVISION`) + review + audit (APPROVED/REJECTED are terminal, not shown) | approve / reject / resubmit (UNVERIFIED only) / edit_and_resubmit (NEEDS_REVISION only) |
| CS inbox | Conversations + messages + suggestions + lead stage | send (human, one-shot) / escalate / set_lead_stage / assign_human / create_followup_task |
| Feedback inbox | FEEDBACK rows + cluster summaries | apply_transition (owner verbs) |
| Knowledge inbox | Candidates + Facts + review decisions | review_candidate / classify_* / deactivate_fact |
| Audit/traceability (all) | read-only `AuditLog` filtered by resource | **none** — OOL V0 never writes audit directly; only the underlying services do |

**Authorization boundary (unchanged):** every OOL route/console page is guarded by the existing owner auth (`authenticate_owner` / `_assert_owner_actor`). A non-owner caller is rejected 401/403 exactly as today. OOL V0 introduces **no new agent seat** and **no relaxation** of per-project / per-artifact read guards already enforced inside the services.

### 4.1 Project-scoped reads & leakage prevention  [R2-4 / F4 / P1-2 — stateless]

**Verified facts (code):** `authenticate_owner` returns `ActorContext(kind="owner", owner_id=...)` with **no `project_id`** (`security.py:135,157`) — the owner identity carries no project scope. Every resource row carries a project FK: `Artifact.project_id` (382); `Conversation.project_id` (873); `CsSuggestion.project_id` (907); `KnowledgeCandidate.project_id|None` + `source_project_id` (698, never NULL, provenance only); `KnowledgeFact.project_id|None` + `source_project_id` (761, provenance only). Existing services read the row's own project id (e.g. `content_draft.py:443`); Knowledge scope is the row's **`project_id`** (`knowledge_service.py:70` — "project_id is the single source of truth for scope: NULL => company-wide"), **never `source_project_id`** (R2-4 / P1-3).

**Stateless project context — the P1-2 model (one shared envelope, no mutable session):** OOL V0 is **project-scoped**, never company-global, and it introduces **no mutable global `selected_project` session**. Project scope travels entirely inside **sealed `OwnerSealedToken` envelopes** (§2.1). There is exactly **one** token wire format with a `token_type` discriminator and **four** schemas: `project_select` (project *selection* — `POST /owner/project-pick` only), `project_context` (project *navigation / inbox entry* — `GET /owner/inboxes/{kind}` only), `detail_view` (exact-resource detail view), and `inbox_action` (decision). The `project-pick token` is a **`project_select`** token — it conforms to the universal schema (it is not a second token format); it binds no business `(kind, rid)` row and is resolved by the mandatory Project-row-load resolver (§2.1.2a), which on success mints a **`project_context`** token that carries the verified operating context into the inbox list:

1. **`GET /owner/project-picker`** (owner-authed, read-only) renders a **server-generated list of business labels** for the projects the owner is authorized to operate. **No project-list response may expose raw project IDs or internal identifiers.** Each option carries a sealed **`project_select` token** (`purpose = "project_select"`, `project_ref` = the Project.id, `operating_project` = the picked project id, `project_display_binding` = sha256 of the canonical project-label JSON — §2.4 project-label binding).
2. **`POST /owner/project-pick`** (owner-authed) receives the chosen `project_select` token; the server runs the **Project-select resolver (§2.1.2a)** — it loads the authoritative Project row by `project_ref`, verifies the owner may operate it, re-derives the live `operating_project`, and compares `project_display_binding` — then **mints a fresh `project_context` token** (purpose `project_inbox`) embedding the verified operating context and **re-renders the inbox hub**. **Nothing is stored in a global session**; the operating context lives inside each downstream token. There is **no separate floating "bootstrap object"** — the `project_select` token is the selection bootstrap and the `project_context` token is the navigation/inbox-entry bootstrap; both are stateless (the `project_select` token is NOT cryptographically one-time — replaying it only mints an equivalent fresh `project_context` token and MUST NOT change any business state).
3. **Single token per request — picker → select → context → detail → action (no raw project ID / no raw resource ID in transit, P1-2):**
   - **Picker:** `GET /owner/project-picker` → renders business labels, each a `project_select` token (no raw project id in the HTML, only the opaque token).
   - **Select:** `POST /owner/project-pick` with body `select_token=<ProjectSelectToken>` → server runs the Project-select resolver (§2.1.2a) and mints a `project_context` token (the navigation bootstrap).
   - **Inbox list:** `GET /owner/inboxes/{kind}?ctx=<project_context>` → server reads `ctx`, derives `operating_project`, lists decisionable items **within that project only**, mints each item a `detail_view` token (for detail navigation) and, when the detail page is rendered, `inbox_action` token(s) (for the owner's valid decisions).
   - **Detail (no raw `rid` in the URL — P1-2):** `POST /owner/inboxes/{kind}/detail` with body `detail_token=<DetailViewToken>` (preferred V0 — the resource reference stays encrypted inside the token and out of the URL / route / referrer / log). The server runs the **Detail-view resolver (§2.1.2c)** — auth + `token_type == detail_view` + `purpose == view_detail` + `inbox`/`kind` match + operating-project authorization + live `resource_scope` + live `display_binding` verification — then renders the detail page and mints fresh action-specific `inbox_action` token(s). The `DetailViewToken` is **NOT** accepted by any decision/mutation endpoint (purpose mismatch).
   - **Action:** `POST /owner/inboxes/{kind}/decide` with body carrying **one** `inbox_action` token (the action token). The server resolves it (§2.1.2d inbox-action resolver): `operating_project` is already inside the token; `resource_scope` is re-derived from the live row; no raw project id is ever supplied by the client.
4. **Deterministic multi-tab:** tab A's tokens bind `operating_project = A`, tab B's bind `operating_project = B`. Because no global state is written, there is **no "last selection wins"** — each tab is independently and deterministically scoped by its own tokens.

**Mandatory rules (replacing the Round-1 "session context" language):**
1. **LIST / aux / count / audit / mutation are all scoped by the universal resolver (§2.1.3)** using `operating_project` (from the token) vs `resource_scope` (re-derived from the live row) — never a client-supplied `project_id`. The inbox LIST iterates decisionable items **within the operating project only** (or company-wide where visible) — there is **no global cross-project aggregation** and therefore no cross-project enumeration.
2. **No single `project` triple-check (P1-1).** The R5 `token_project == claims.project == live_row_project` equality is deleted; the universal resolver (§2.1.3) computes visibility (`resource_scope == operating_project` OR `resource_scope == 'company'` — company-wide rows are visible from any project context) and mutability (`resource_scope == operating_project` only) independently. `operating_project == 'company'` is **forbidden in V0** (rejected at mint/resolution, §2.1.2 / §2.1.3); there is no company operating context in V0 (FUTURE-only, §6.4). A token minted for project A's resource **cannot** be acted on under a project-B operating context, and a company-wide row is read-only from a project context.
3. **Owner cannot choose `project_id`.** No OOL request accepts a `project_id` parameter. The only project selection is the business-label pick that yields a sealed `project_select` token (the project-pick bootstrap); the owner never types or sees the internal id.
4. **Token cannot expand project scope.** The token binds a specific resource `rid`; resolution is by that `rid` → its row → its project. A token is valid only for the project its resource belongs to.
5. **No cross-project enumeration.** No raw project id, no per-project count, no project identifier in any owner-facing payload beyond the selected business label. Pagination is **bounded and deterministic over items** (cursor by item, fixed page size), not over projects.
6. **No leakage via counts / pagination / timing.** LIST responses do not expose total counts or project structure. Error timing is normalized (§8): 404 / 403 / 409 all collapse to uniform human-readable messages with no distinguishable latency or body.
7. **Normal responses hide internal IDs.** `rid`, `operating_project`, `resource_scope`, `project_id`, `series_id`, `version`, enum strings, canonical-tag ids, checksum, revision are **never** present in HTML / JSON sent to the owner. `detail_ref` is a display-only handle (§6).
8. **Per-project guards preserved.** Because project scope is re-derived from the live row + token binding, the existing per-project 403 (#109) and per-artifact same-project 403 (#110) continue to apply unchanged.

**CSRF protection contract (all owner POST actions + the navigation GET) [P1-2]:** OOL V0 has **no session cookie** (owner auth is HTTP Basic, re-verified per request; project scope is in sealed tokens, not a cookie). CSRF defense is therefore provided by the **sealed single-purpose token**, which is unguessable, server-issued, purpose-bound, short-TTL, and only ever embedded in the server-rendered form — an attacker cannot forge or obtain it cross-site (equivalent to a double-submit token). Every owner request **must** carry a valid sealed token whose `owner`/`inbox`/`kind`/`purpose` exactly matches the endpoint (§2.1.2) — a request without a valid, matching token is rejected before any mutation/navigation: the `project-pick` POST requires a `project_select` token (`purpose = project_select`), the inbox-list GET requires a `project_context` token (`purpose = project_inbox`, via `?ctx=`), the detail POST requires a `detail_view` token (`purpose = view_detail`), and the decision POST requires an `inbox_action` token (`purpose` ∈ the endpoint's allowed set). No separate anti-CSRF cookie is introduced (that would require a mutable session, which is excluded by P1-2).

---

## 5. Auth boundary

- Reuses the existing owner authentication from `src/aios/api/security.py` (Basic Auth for owner, as landed in #74) and `ActorContext(kind="owner")`.
- `owner_inbox_routes.py` and the console pages import the same dependency; they add **no new credential scheme**.
- **Token resolution runs AFTER owner auth** (§2.1.2 step 1 before step 2). The token is a convenience reference + display-consent binding, **not** an auth mechanism, and conveys no authority (§2.1 rule 1–2).
- Key handling mirrors owner-auth fail-closed: missing/invalid `AIOS_OOL_TOKEN_CURRENT_KEY_B64` → `503` (§2.1.1), never a silent fallback.

---

## 6. Four inbox schemas (read model — no new tables)

Each inbox page is rendered from a **read projection** built by `owner_inbox.py`:

```python
@dataclass
class InboxItem:
    token: str                 # opaque AES-256-GCM sealed DetailViewToken (list item → detail navigation, §2.1)
    business_label: str        # human-readable one-line summary
    status_label: str          # e.g. "待批准" / "已转人工" / "待审定"
    detail_ref: str            # display-only stable handle (NOT series/version; §6.5)
    preview: str               # truncated, PII-redacted body/text
    decisions: list[str]       # which actions are currently valid for THIS item
    updated_at: str
```

`detail_ref` is a **display-only** counter/handle derived from the item **without** exposing any internal identity:
- Content: `"内容#<project-scoped-seq>"` (seq = created order within the token-bound project).
- CS: `"对话#<customer_ref>"` — `customer_ref` is an existing **business** reference, not an internal PK.
- Feedback: `"反馈#<project-scoped-seq>"`.
- Knowledge: `"知识候选#<project-scoped-seq>"` / `"知识事实#<project-scoped-seq>"` — **never** `series_id`·`version` (F7/R2-7).

### 6.1 Content inbox query
- Source: `Artifact.type == CONTENT_DRAFT` and `review_status in (REVIEW_PASSED, UNVERIFIED, NEEDS_REVISION)` **within the token-bound project** (project scope per §4.1, resolved server-side from the sealed token, never client-selected). All three pending states are inbox-visible:
  - `REVIEW_PASSED` — independent review passed, awaiting owner approval.
  - `UNVERIFIED` — owner (or producer) saved an edit via `update_content_draft` (`content_draft.py:434`) but has **not yet resubmitted**; the item stays visible so the owner can resubmit without re-discovering the Artifact ID.
  - `NEEDS_REVISION` — `submit_content_draft` ran the review and it returned low-confidence (`content_draft.py:565`); needs re-edit.
- `decisions` derived from status:
  - `REVIEW_PASSED → [approve, reject]`
  - `UNVERIFIED → [resubmit]` (`content.resubmit`: re-opening re-invokes **only** `submit_content_draft` — never vanishes, never re-calls `update_content_draft`)
  - `NEEDS_REVISION → [edit_and_resubmit]` (`content.edit_and_resubmit`: `update_content_draft` + `submit_content_draft`)

### 6.2 CS inbox query
- Source: `Conversation` (within the token-bound project) whose latest suggestion is `HUMAN_CONFIRM` or carries `escalation_flag`, OR lead stage not yet `WON`/`proposal` and owner wants to manage.
- `decisions` derived per conversation: `[adopt_and_send, escalate, set_lead_stage, assign_human, create_followup]` (subset valid by current state).

### 6.3 Feedback inbox query
- Source: `Artifact.type == FEEDBACK` and `stage == AWAIT_OWNER_APPROVE` **within the token-bound project**.
- `decisions`: `[approve_solution, reject_solution, defer, mark_duplicate, reject_feedback]` (owner verbs valid from `AWAIT_OWNER_APPROVE`; `MARK_DUPLICATE`/`REJECT_FEEDBACK` allowed from a wider set per `ALLOWED_TRANSITIONS`).

### 6.4 Knowledge inbox query  [P1-3 — `project_id` is scope, `source_project_id` is provenance]
- **`scope != provenance`.** The authoritative, reusable **scope** of a Knowledge row is its `project_id` (NULL = company-wide, visible in every project context). `source_project_id` is **provenance only** (which project originally produced it) and is **NEVER** used for auth/inbox-scope filtering.
- Source (Candidate): `KnowledgeCandidate.status == DRAFT` (optionally still legacy-sentinel → show "需先分类"), **scoped by `(project_id == <operating_project> OR project_id IS NULL)`** (visibility per the universal resolver, §2.1.3).
- Source (Fact eligible for deactivation): `KnowledgeFact.status == APPROVED`, **scoped by `(project_id == <operating_project> OR project_id IS NULL)`**.
- **Other projects' project-scoped rows are NEVER visible.** A `KnowledgeCandidate`/`KnowledgeFact` whose `project_id` is another project (and not NULL) does not appear, and cannot be read or mutated, from the current operating context (visibility check fails).
- **Company-wide (`project_id IS NULL`) rows are READ-ONLY from a project operating context (P1-1).** Both candidates and facts with `resource_scope == 'company'` shown from a project context (`operating_project != 'company'`) expose **no mutation action** — their `classify`/`approve`/`reject`/`deactivate` purposes are **withheld** at the inbox-query stage and any such token is rejected (uniform message). Company-wide mutation requires the future **company operating context** (see Future).
- `decisions` derived (mutability per §2.1.3):
  - candidate DRAFT + legacy + **project-scoped** (`project_id == <operating_project>`) → `[classify, reject]`
  - candidate DRAFT + classified + **project-scoped** → `[approve, reject]`
  - candidate (any status) + **company-wide** (`project_id IS NULL`) from a project context → `[]` (read-only; classify/approve/reject withheld)
  - fact APPROVED + **project-scoped** (`project_id == <operating_project>`) → `[deactivate]`
  - fact APPROVED + **company-wide** (`project_id IS NULL`) from a project context → `[]` (read-only; deactivation withheld in V0)
- **Future (NON-NORMATIVE — not part of V0):** company-wide mutation requires a distinct **company operating context** (`operating_project == 'company'`) + an explicit owner architecture decision (new scope model + possibly migration). It is **not** taken in V0 (flagged, not silently added); no V0 resolver, route, test, or acceptance references a company operating context — see the V0 envelope invariant in §2.1.2 and the P1-3 authorization tests (§12 T-AU8).

### 6.5 Business-language view model  [F7 / R2-7]
OOL V0 renders **business language**, never internal identifiers or enum/string raw values. The mapping is centralized and fixed (defined in implementation; no owner input):

- **risk_tags (Feedback):** mapped to a fixed Chinese business-label dictionary (e.g. `"privacy"` → "涉及隐私", `"needs_human_review"` → "需人工复核"). Owner sees only the business label; the raw tag string never appears.
- **Feedback actions:** `APPROVE_SOLUTION` → "批准方案", `REJECT_SOLUTION` → "驳回方案", `DEFER` → "暂缓", `MARK_DUPLICATE` → "标记重复", `REJECT_FEEDBACK` → "拒绝该反馈" — the named `FeedbackTransition` verb is the only accepted input (#110); the owner picks the label.
- **Knowledge scope / canonical tags:** the 7 `CANONICAL` tags (`knowledge_tags.CANONICAL`) are shown by business label (e.g. "客户常见问题" / "产品功能" / "退款政策"); owner checks boxes; server calls `normalize_tags`. Series are shown by **business label** (§3.4 / R2-7), never `series_id`; **never by `version`**.
- **Provenance:** conversation → "客户对话 #<customer_ref>"; artifact → "内容《topic》第 N 轮"; candidate/fact → "知识候选 #<seq>" / "知识事实 #<seq>". All derived from existing business columns (`customer_ref`, topic, project-scoped seq); **no internal PK, no `series_id`·`version`, no checksum/revision** in the owner surface.
- **CS lead stage:** owner picks business label ("访客→线索→合格→方案→成交"); server maps to `LeadStage` enum server-side.

---

## 7. Actions (approve / reject / edit / escalate)

All four verbs map to existing service methods (see §2.2). OOL V0 adds **no new business logic in the binding layer** — it translates a business-label click into the correct existing call with server-bound identities. (The one necessary *domain* extension is `owner_confirm_suggestion` in `customer_service.py`, declared in §11 / §14 and proven zero-migration; it is not "OOL business logic".)

| Verb | Content | CS | Feedback | Knowledge |
|---|---|---|---|---|
| **approve** | `approve_content_draft` | `owner_confirm_suggestion` (one-shot, **409 already-handled on replay**, §3.2) | `apply_transition(APPROVE_SOLUTION)` | `review_candidate(APPROVE)` |
| **reject** | `reject_content_draft` | `escalate` (manual) | `apply_transition(REJECT_SOLUTION)` | `review_candidate(REJECT)` |
| **resubmit / edit_and_resubmit** | `resubmit` (UNVERIFIED) → `submit_content_draft` **only** (no `update_content_draft`, no `revision_count` bump, P1-3); `edit_and_resubmit` (NEEDS_REVISION) → `update_content_draft` + `submit_content_draft` (honest, §3.1 / P1-3) | edit suggestion text via `edited_text` arg to `owner_confirm_suggestion` (CS has no separate resubmit) | `apply_transition(RETURN_TO_CLARIFY)` / `INVALIDATE_PENDING` | `classify_candidate_tags` (project-scoped candidate only; company-wide candidate is read-only, §6.4) |
| **escalate** | (n/a — content uses reject/request-edit) | `escalate` | `apply_transition(DEFER)` | (n/a) |

- **Human-readable confirmation:** after each action the owner sees a plain-language result ("已批准《topic》第 N 轮" / "已转人工" / "已暂缓该反馈"), never a raw ID or SQL state.
- **Stale handling:** if the underlying row changed state between render and submit (decision-time re-read + `display_binding` compare, §2.3/§2.4), the token resolves to a stale item → return **409-equivalent human message** ("该内容已被修改，请重新审阅最新一轮" / "该建议已处理，未重复发送" / "该方案已变更，请重新查看"). The underlying service's CAS is the final guard — OOL V0 never bypasses it.

---

## 8. Human-readable error behavior

OOL V0 wraps service `ServiceError` and token-resolution failures into business language. Mapping examples:

| Failure | Owner-facing message (zh) |
|---|---|
| Token malformed / decode fail / unknown key-id / unknown schema version | "该条目已失效，请刷新收件箱。" (uniform — no distinguisher) |
| Token expired (beyond skew) | "该条目已失效，请刷新收件箱。" |
| Token owner / inbox / kind / purpose mismatch | "该操作已不可用，请刷新收件箱。" |
| **Display-binding mismatch (stale card)** | "该条目已变更，请刷新收件箱。" |
| 404 not found / item deleted | "该条目已不存在或已处理，请刷新收件箱。" |
| 409 stale review (content) | "内容已被修改，请重新审阅最新一轮。" |
| **Content partial-failure (update saved, resubmit failed)** | "编辑已保存，但重新送审未成功，请重试送审。" |
| **409 CS already-handled (replay)** | "这条回复已经处理，无需再次发送。" |
| 409 already decided / consumed (other inboxes) | "该条目已处理，无需重复操作。" |
| 409 CS fact re-validation failed | "知识已更新，请重新确认后再发送。" |
| **403 wrong-project token / cross-project** | "你无权操作该项目的此条目。" |
| 422 missing rationale | "请填写处理理由。" |
| 502 outbound delivery failed (CS) | "消息发送失败，请稍后重试（未重复发送）。" |
| 409 auto-send confidence below threshold | "该建议置信度不足，需你手动确认后发送。" |

No stack trace, no SQL, no internal ID, no key material leaks to the owner surface. AuditLog `redact_secrets` / `redact_pii` continue to run server-side. Error **timing and bodies are normalized** so a 404 / 403 / 409 are indistinguishable to an attacker (no enumeration oracle, §4.1).

---

## 9. Audit & traceability visibility

- Each inbox detail page shows a **read-only audit trail** for that item: the relevant `AuditLog` rows (action + actor + before/after summary + timestamp), filtered by `resource_id`/`resource_type` **within the token-bound project** (§4.1 — audit views are project-scoped, no cross-project leak).
- OOL V0 **never writes** AuditLog; it only reads. All writes remain inside the underlying services (preserving #108-A inert-metrics, #109 outbound/escalation, #110 stage transitions, #119 review idempotency).
- A dedicated **"我的操作记录"** view lists the owner's recent decisions across all four inboxes (read-only, project-scoped), giving the owner post-hoc traceability without exposing internal IDs.

### 9.1 `OWNER_RELAY_COUNT` metric  [F8 / R2-8]
- **Definition:** the count of times, across a Synthetic Human UAT run, the owner was forced to **relay technical state** to complete a normal business flow. "Relay" includes **copying, remembering, translating, or transferring** any of: Artifact ID / Conversation ID / Message ID / Suggestion ID / checksum / revision / enum / canonical-tag id / series·version / API route / SQL / Git / Alembic id / any internal PK / any token segment.
- **Internal technical relay = ZERO (hard).** By design every internal identity is server-bound (token + live row + sealed project-binding context). There is **no code path** where the owner relays an internal ID or token segment. The metric exists to *catch regressions*; it must read 0 in a correct implementation.
- **Measurement method — itemized relay ledger (R2-8):** the UAT harness records, for **every one of the four journeys**, an **itemized ledger** of each manual handoff the owner performs:
  - any internal-ID input field rendered or required → +1 and FAIL;
  - any internal ID / token segment the owner had to **copy** (clipboard), **remember** (type from memory), **translate** (map a system value to a UI value), or **transfer** (carry between pages/agents/tools) → +1 and FAIL;
  - any business-label selection the owner makes (e.g. pick a canonical feedback, pick a series by name) is recorded as a *business* choice, **not** a relay, and is bounded per §9.1 ceilings.
  The ledger is emitted as part of the UAT report; acceptance FAILS if any technical value appears in it.
- **Owner-typed business-choice ceilings (synthetic-UAT tolerance only):** where a journey unavoidably requires the owner to make a *business* (non-internal) selection, the count of such owner-typed business selections is bounded: **Content ≤ 1, CS ≤ 2, Feedback ≤ 2, Knowledge ≤ 2** per journey. These ceilings apply **only** to owner-typed *business* choices that are NOT internal IDs; if any internal-ID-typed field is ever surfaced, the metric fails regardless of ceiling.
- This metric is **observational only** in V0 — it does not change behavior; it gates acceptance.

---

## 10. No hidden weakening of existing gates

Explicitly preserved (must remain GREEN after OOL V0):

1. **Content (#108-A / #119):** owner approval still binds EXACT `reviewed_checksum`+`reviewed_revision` under `BEGIN IMMEDIATE`; review audit `idempotency_key` still `artifact_id:source_revision:source_checksum`; APPROVED never creates a `KnowledgeFact`; stale review → 409; AuditLog global uniqueness intact. The `display_binding` (§2.4) adds a *second*, cryptographic consent check on top — it does not relax any of these.
2. **CS (#109):** `owner_confirm_suggestion` stays owner-only and one-shot (reuses `CsSuggestion.consumed`, `models.py:914`); **replay returns 409 already-handled, never a second send** (§3.2); auto-send still requires a bound `suggestion_id` + re-checked knowledge fact + active threshold; escalation writes audit but triggers NO production action; PII redaction in audit after_snapshot unchanged; per-project 403 unchanged.
3. **Feedback (#110):** only named `FeedbackTransition` verbs accepted (never bare stage); owner approval binds exact pending solution revision under `BEGIN IMMEDIATE` (checksum + `revision_count`, §2.4); no `KnowledgeFact`/`Task`/`Event`/`Payment` side effect; per-artifact same-project 403 unchanged; deterministic clustering untouched.
4. **Knowledge (#87 / review protocol):** `review_candidate` stays owner-only; contiguous versioning + supersede-head enforcement; legacy-sentinel must be classified before review; `classify_*` one-time transition only; deactivate only APPROVED facts; `detail_ref` never derived from `series_id`·`version` (F7/R2-7); `display_binding` freezes `head_version` so a moved head cannot be silently superseded.
5. **Review gate (#64 / #74):** `owner_approve_review` remains the ONLY path to APPROVED for reviewed artifacts; AI reviewers never substitute; `authenticate_owner` + `_assert_owner_actor` unchanged.
6. **Auth (#74):** owner Basic Auth + `ActorContext(kind="owner")` unchanged; no new agent seat; no relaxation of read guards; token is not an auth mechanism (§2.1 / §5).
7. **Project isolation (#109 / #110 / R2-4 / P1-1):** all reads/aux/counts/audit/mutations are scoped to the server-issued project context via the universal resolver (§2.1.3): `operating_project` vs `resource_scope` visibility + mutability; company-wide rows read-only from a project context; wrong-project / wrong-scope token → fail closed; `operating_project == 'company'` is forbidden in V0 (rejected at mint/resolution).

Any OOL V0 change that would relax one of these is **out of scope and must be rejected** in review.

---

## 11. File map (for the implementation PR — NOT in this plan-only PR)

- **NEW** `src/aios/owner_inbox.py` — token seal/resolve (`OwnerSealedToken` AES-256-GCM envelope with `project_select` / `project_context` / `detail_view` / `inbox_action` schemas, §2.1), four inbox read projections, action adapters delegating to existing services. **Zero new models / zero migration.**
- **NEW** `src/aios/api/owner_inbox_routes.py` — `GET /owner/project-picker`, `POST /owner/project-pick`, `GET /owner/inboxes/{kind}` (`?ctx=<project_context>`), `POST /owner/inboxes/{kind}/detail` (body `detail_token`), `POST /owner/inboxes/{kind}/decide`. All owner-authed; no `project_id` request parameter (§4.1 / P1-2); project context comes from the sealed `project_select` → `project_context` bootstrap (stateless — no global session). Every request requires a valid sealed, purpose-bound token (CSRF contract, §4.1 — `project_select` for pick, `project_context` for inbox list, `detail_view` for detail, `inbox_action` for decide).
- **EXTEND** `src/aios/console.py` — add the `/owner/project-picker` (server-rendered business labels + sealed `project_select` (`project_select`) tokens) + `POST /owner/project-pick` (Project-select resolver §2.1.2a validates `project_select`, mints a `project_context` token) + `/owner/inboxes` hub + 4 inbox pages (entered with `?ctx=<project_context>`) + `POST /owner/inboxes/{kind}/detail` (resolves `detail_view`, renders detail + mints `inbox_action` tokens) + detail/audit views (server-rendered, business-labeled per §6.5; **no mutable `selected_project` session**).
- **EXTEND (minimal domain change)** `src/aios/customer_service.py` — implement `owner_confirm_suggestion` + extend `_human_send` to bind `suggestion_id` + mark `consumed` + honor `idempotency_key` (repairs pre-existing gap; adds **no column**; zero migration). This is the only service-layer change; it is explicitly declared, not hidden.
- **NEW** `tests/test_owner_inbox.py` — TDD acceptance matrix (§12).
- **Dependency metadata (honest correction of Round-1, R2-1):** add `cryptography` to `pyproject.toml` `dependencies` **and** update the lockfile. This is the **only** dependency change and it is **metadata, not schema** — it does not affect Alembic / migrations. The implementation diff therefore may include `pyproject.toml` + lockfile **in addition to** the OOL Python files + `customer_service.py` extension + tests; the plan-PR scope (this PR #122) remains **one documentation file**.
- **Config (no schema change):** `AIOS_OOL_TOKEN_CURRENT_KID` + `AIOS_OOL_TOKEN_CURRENT_KEY_B64` (§2.1.3) + optional previous-key vars + optional `AIOS_OOL_TOKEN_TTL_SECONDS`.
- No change to `models.py`, `alembic/`, `content_draft.py`, `feedback.py`, `knowledge_service.py`, `review.py`, `api/security.py` beyond the `customer_service.py` extension above.

---

## 12. TDD acceptance matrix (implementation PR)

Each criterion is a test that must be RED before implementation and GREEN after. Grouped by concern. (F6/R2-6: matrix expanded to cover token/reference security, content, CS, feedback, knowledge, project/auth, concurrency, usability, and the R2-2 same-status-replacement + R2-4 wrong-project + R2-5 partial-failure + R2-7 knowledge-naming cases.)

### Token / reference security  [R2-1 / F1]
- [ ] **T-T1** Sealed token decrypts only with correct key; tampered ciphertext → uniform "该条目已失效" (no distinguisher); unknown `kid` → rejected fail-closed.
- [ ] **T-T2** Token with `owner` != authenticated owner → 403-equivalent uniform message; token `purpose` != endpoint allowed action → rejected.
- [ ] **T-T3** Expired token (beyond ±30s skew) → uniform message; valid-within-TTL token accepted.
- [ ] **T-T4** No secret key material, raw `rid`, `operating_project`, `resource_scope`, `project_id`, `series_id`, `version`, checksum, revision, or **decrypted claims** are rendered separately in HTML / JSON (grep the response). The opaque token's **public base64url `kid` header is explicitly permitted** (it is non-secret by design, §2.1); the test asserts tampering with the `kid`/header segment breaks AEAD authentication (uniform "该条目已失效"), proving the header is authenticated, not confidential.
- [ ] **T-T5** Missing / invalid `AIOS_OOL_TOKEN_CURRENT_KEY_B64` (wrong length / undecodable) → `503` fail-closed; key never in logs / AuditLog / error body.
- [ ] **T-T6** Token is not an auth bypass: a request with a valid token but **no** owner auth → 401; token resolution never runs before auth.
- [ ] **T-T7** **Malformed envelope** — wrong segment count, invalid base64url, non-JSON header, `alg != "A256GCM"`, `v != 1` → uniform "该条目已失效" (no distinguisher).
- [ ] **T-T8** **Key rotation window** — token sealed under `PREVIOUS_KID` accepted until `PREVIOUS_ACCEPT_UNTIL`; after the deadline it is deterministically rejected; `CURRENT_KID == PREVIOUS_KID` config → startup fail-closed.
- [ ] **T-T9** Encrypted `display_binding` digest and `rid`/`project` are never leaked in any response / log / AuditLog.

### Content Decisions  [F5 / R2-5]
- [ ] **T-C1** Owner opens Content inbox (token-bound project) → only `REVIEW_PASSED`/`UNVERIFIED`/`NEEDS_REVISION` CONTENT_DRAFT shown; no raw Artifact ID in rendered HTML.
- [ ] **T-C2** Owner approves via token → `approve_content_draft` called with the **current** `reviewed_checksum`+`reviewed_revision` (server-bound, not owner-typed); status → APPROVED.
- [ ] **T-C3** **rev2 → rev3 REVIEW_PASSED stale test (R2-2, mandatory):** owner opens a **rev2 REVIEW_PASSED** card → producer edits → **rev3** → rev3 independently reaches **REVIEW_PASSED** → owner clicks the **old rev2 card** → **REJECT as stale** ("该条目已变更"); the adapter does **NOT** read rev3 and substitute rev3 checksum/revision into the old rev2 action.
- [ ] **T-C4** Owner rejects with reason → `reject_content_draft` called; status → REJECTED; audit written.
- [ ] **T-C5** **Honest edit + partial-failure (P1-1/R5, Option A):** for the `NEEDS_REVISION` item the `edit_and_resubmit` action (`update_content_draft` + `submit_content_draft`) is labeled "编辑并重新送审"; `update_content_draft` succeeds → `review_status = UNVERIFIED` (`content_draft.py:434`, **not** `NEEDS_REVISION`); item stays visible (§6.1 includes `UNVERIFIED`); if `submit_content_draft` then fails, status stays `UNVERIFIED` and UI shows *"编辑已保存，但重新送审未成功，请重试送审。"* — **never** "复审中"; the owner then uses the distinct `resubmit` action (no second `update_content_draft`); only if the review commits and returns low-confidence does the item become `NEEDS_REVISION` again. No silent re-approve (#108-A/#119 intact).
- [ ] **T-C6** Token TTL exceeded / item deleted → decision rejected with human-readable message; no action taken.
- [ ] **T-C7** Non-owner caller → 401/403; no inbox data leaked.
- [ ] **T-C8** **`resubmit` vs `edit_and_resubmit` distinct contract — 12-step acceptance (P1-3):**
  1. Seed a CONTENT_DRAFT in `UNVERIFIED` (owner/producer saved an edit via `update_content_draft`, `content_draft.py:434`); it is visible in the Content inbox with decision `[resubmit]` and **NOT** `[edit_and_resubmit]`.
  2. Seed a CONTENT_DRAFT in `NEEDS_REVISION`; it is visible with decision `[edit_and_resubmit]` and **NOT** `[resubmit]`.
  3. Open the `UNVERIFIED` item → click **重新送审** (`content.resubmit`) → server calls **only `submit_content_draft`**; `update_content_draft` call count == **0**; `revision_count` is **NOT** incremented.
  4. `submit_content_draft` raises before the review commits → item stays `UNVERIFIED`; UI shows *"编辑已保存，但重新送审未成功，请重试送审。"*; re-clicking resubmit again calls **only `submit_content_draft`** a second time (still no `update_content_draft`).
  5. `submit_content_draft` commits a low-confidence review → item becomes `NEEDS_REVISION`; the `resubmit` action is no longer offered; `edit_and_resubmit` is offered.
  6. Open the `NEEDS_REVISION` item → click **编辑并重新送审** (`content.edit_and_resubmit`) → server calls `update_content_draft(new_body)` **then** `submit_content_draft`; `revision_count` increments once (the edit), not on a bare resubmit (steps 3–4 prove no double-increment).
  7. The `resubmit` token's `display_binding` freezes `review_status == "UNVERIFIED"`; the `edit_and_resubmit` token's `display_binding` freezes `review_status == "NEEDS_REVISION"` — the two bindings are **different**; a `resubmit` token cannot be replayed as an `edit_and_resubmit` and vice-versa (purpose mismatch → rejected).
  8. Stale `resubmit` card: the `UNVERIFIED` item is meanwhile re-approved by another path → its `review_status` leaves `UNVERIFIED`; the old `resubmit` token's `display_binding` no longer matches → **fail closed** ("该条目已变更，请刷新收件箱"), no `submit_content_draft` call.
  9. Partial-failure on `edit_and_resubmit`: `update_content_draft` succeeds (item → `UNVERIFIED`) but `submit_content_draft` raises; item stays `UNVERIFIED` with edited body; owner uses `resubmit` (not a second `edit_and_resubmit`) → `update_content_draft` call count stays at 1.
  10. No internal ID (`artifact_id` / checksum / revision / `independent_review` ids) is ever shown or copied by the owner across steps 1–9.
  11. A `resubmit` POST lacking a valid sealed `InboxActionToken` (or with `purpose != resubmit`) → rejected before any service call (CSRF / purpose contract, §4.1).
  12. After a successful `resubmit` and a successful `edit_and_resubmit`, a **completed** independent review resolves the item to **`REVIEW_PASSED` or `NEEDS_REVISION`** — it MUST NOT remain `UNVERIFIED` (a completed review is a terminal review outcome, never the retryable pre-review state); #108-A/#119 gates (exact `reviewed_checksum`+`reviewed_revision` binding, stale-review 409, no KnowledgeFact created) remain intact. `UNVERIFIED` is only the pre-review / retryable state entered by `update_content_draft`.

### Customer-Service Decisions  [R2-3 / F3]
- [ ] **T-S1** Owner opens CS inbox (token-bound project) → conversations with `HUMAN_CONFIRM`/`ESCALATE` shown; `conversation_id` / `suggestion_id` never in HTML.
- [ ] **T-S2** Owner adopts & sends → `owner_confirm_suggestion` sets `consumed=True` under `BEGIN IMMEDIATE`; `cs.outbound_send` audit written with PII redacted.
- [ ] **T-S3** **Terminal replay (canonical):** repeated click / concurrent session on same suggestion → **no double send**; `consumed` + `idempotency_key` enforce one-shot; second attempt → **409 "这条回复已经处理，无需再次发送"** (NOT a prior-result return). No second `Message`, no second adapter call, no second successful-send audit.
- [ ] **T-S4** Owner escalates → `escalate` called; `cs.escalation` audit written; no production side effect.
- [ ] **T-S5** Owner sets lead stage via business label → mapped to `LeadStage` server-side; `cs.lead_stage` audit written.
- [ ] **T-S6** Outbound failure → 502 mapped to human-readable "发送失败，未重复发送"; `consumed` rolled back (no partial send).
- [ ] **T-S7** Knowledge fact goes stale before send → re-validation fails → human-readable "知识已更新，请重新确认".
- [ ] **T-S8** **CS display-binding same-status replacement (R2-2):** owner opens a `HUMAN_CONFIRM` card → a **new** suggestion is created for the same conversation (still `HUMAN_CONFIRM`, unconsumed) → owner clicks the **old card** → **fail closed** (stale via `conv_suggestion_count`); no send of the old suggestion.
- [ ] **T-S9** **CS HUMAN_CONFIRM edited reply:** owner edits the suggested text before sending → `edited_text` passed to `owner_confirm_suggestion`; edited text validated (non-empty, length-bounded) and audited (PII-redacted); no claim of a non-existent separate edit path.
- [ ] **T-S10** **CS facts_binding canonical (P2-2):** `canonical_facts_binding` is order-independent (`["f2","f1"]` == `["f1","f2"]`); the empty set yields the fixed `sha256:{}` constant; different revision values yield different digests; decision-time `compare_digest` rejects a changed binding.

### Feedback Decisions  [F7 / R2-2]
- [ ] **T-F1** Owner opens Feedback inbox (token-bound project) → only `AWAIT_OWNER_APPROVE` shown; no raw Artifact ID in HTML.
- [ ] **T-F2** Owner approves solution via token → `apply_transition(APPROVE_SOLUTION)`; binds exact pending revision (checksum + `revision_count`); status → DEVELOP.
- [ ] **T-F3** Owner rejects / defers → correct named verb; CAS 409 on stale edit surfaced human-readably.
- [ ] **T-F4** Owner marks duplicate → picks canonical from server-rendered **same-project** list; `canonical_feedback_id` resolved server-side; cross-project pick rejected 409.
- [ ] **T-F5** Only valid transitions offered for current stage (no bare stage string accepted).
- [ ] **T-F6** **Feedback same-status replacement (R2-2):** owner opens a solution card → a **new solution submit** bumps `revision_count` + recomputes `checksum` → item returns to `AWAIT_OWNER_APPROVE` → owner clicks the **old card** → **fail closed** (digest mismatch).
- [ ] **T-F7** **Request clarification:** `apply_transition(RETURN_TO_CLARIFY)` / `INVALIDATE_PENDING` path works and re-binds checksum/revision.

### Knowledge Decisions  [F7 / R2-7 / R2-2]
- [ ] **T-K1** Owner opens Knowledge inbox (token-bound project) → DRAFT candidates + APPROVED facts shown, **scoped by `project_id` (NULL = company-wide) — NOT `source_project_id`** (scope != provenance, §6.4 / P1-3); no raw candidate/fact ID in HTML; **no `version` shown** (uses `#<seq>`).
- [ ] **T-K2** Owner approves candidate → `review_candidate(APPROVE)` with `version` derived via `next_version` + `supersedes_fact_id` from head; no series/version typed.
- [ ] **T-K3** Owner classifies legacy-sentinel candidate → `classify_candidate_tags` with `normalize_tags` on checkbox selection; one-time transition enforced.
- [ ] **T-K4** Owner rejects / deactivates → respective owner-only methods; deactivate only on APPROVED fact.
- [ ] **T-K5** Contiguous versioning + supersede-head unchanged (existing contract preserved).
- [ ] **T-K6** `detail_ref` is a project-scoped sequence handle; **never** contains `series_id` or `version`.
- [ ] **T-K7** **Knowledge business provenance (R2-7):** series shown by **business label** (server-derived stable label, §3.4), never `series_id`; approved fact shown as "知识事实 #<seq>", never "vN"; no candidate/fact bypass (approve still goes through `review_candidate`).
- [ ] **T-K8** **Knowledge display-binding same-status replacement (R2-2):** owner opens a DRAFT candidate card → a **moved head version** (new approved fact in same series) OR a changed `statement` → owner clicks the **old card** → **fail closed** (`head_version`/`statement_sha256` mismatch); a changed/replaced/already-terminal candidate cannot be approved via the stale card.
- [ ] **T-K9** **Knowledge scope = project_id, not source_project_id (P1-3):** a company-wide `KnowledgeFact` (`project_id IS NULL`) is **visible** from project A's context.
- [ ] **T-K10** **Knowledge scope (company-wide from B):** the same company-wide `KnowledgeFact` (`project_id IS NULL`) is **visible** from project B's context.
- [ ] **T-K11** **Knowledge cross-project isolation:** a project-A-scoped `KnowledgeFact` (`project_id == A`) is **NOT visible** from project B's context (scope != provenance).
- [ ] **T-K12** **Knowledge cross-project isolation (mirror):** a project-B-scoped `KnowledgeFact` (`project_id == B`) is **NOT visible** from project A's context.
- [ ] **T-K13** **Company-wide read-only from project context:** a company-wide `KnowledgeFact` (`project_id IS NULL`) shown from a project context has **no `deactivate` action**; an attempted deactivation is forbidden (403/uniform). Deactivation of company-wide requires the future company operating context.
- [ ] **T-K14** **Company-wide candidate visible but READ-ONLY:** a `KnowledgeCandidate` with `project_id IS NULL` is **shown** in the Knowledge inbox from a project operating context (company-wide visible), but its `decisions == []` — `classify`/`approve`/`reject` are **withheld** (P1-1: a company candidate mutation is a company-scope action, not permitted from a project context).
- [ ] **T-K15** **source_project_id grants no mutation right:** a `KnowledgeFact` whose `source_project_id == A` but `project_id == B` is **NOT visible** and **NOT mutable** from project A's context (provenance ≠ scope).
- [ ] **T-K16** **Company-wide candidate mutation forbidden from project context (P1-1):** an attempt to `classify`/`approve`/`reject` a company-wide `KnowledgeCandidate` (`project_id IS NULL`) while `operating_project != 'company'` is rejected (403 / uniform message); no `classify_candidate_tags` / `review_candidate` call is made. The resolver's mutability check (§2.1.3) forbids it.
- [ ] **T-K17** **Series label stability (P2-2):** adding a replacement head to series X (newer `created_at`) does **not** change the `"系列 #N"` label of an unrelated series Y; the label is derived from the **immutable series-root `created_at`** (earliest fact in the series), not the replacement-head `created_at`. The label is documented as an ephemeral display-only handle accompanied by **business provenance only** (the candidate/fact statement summary + business series label — never an internal `series_id`, UUID, PK, version, or canonical-tag id, per P2-1).

### Project-select / Project-context lifecycle, schema-confusion & V0 authorization  [P1-1 / P1-2 / P1-3]

**Lifecycle / endpoint-targeting (one token type, one purpose class, no implicit promotion):**
- [ ] **T-PS1** A valid `ProjectSelectToken` (`token_type="project_select"`, purpose `project_select`) is accepted **only** by `POST /owner/project-pick` and (on success) mints a fresh `ProjectContextToken`; the same token presented to `GET /owner/inboxes/{kind}`, `POST /owner/inboxes/{kind}/detail`, or `POST /owner/inboxes/{kind}/decide` is rejected (endpoint mismatch, before any navigation/mutation).
- [ ] **T-PS2** A valid `ProjectContextToken` (`token_type="project_context"`, purpose `project_inbox`) is accepted **only** by `GET /owner/inboxes/{kind}?ctx=<token>` (allowlisted kinds only); the same token presented to `POST /owner/project-pick`, `POST /owner/inboxes/{kind}/detail`, or `POST /owner/inboxes/{kind}/decide` is rejected (endpoint mismatch).
- [ ] **T-PS3** A `DetailViewToken` presented to `POST /owner/project-pick` or the inbox-list GET is rejected (endpoint mismatch); an `InboxActionToken` presented to `POST /owner/project-pick`, the inbox-list GET, or the detail POST is rejected (endpoint mismatch). The four schemas are mutually exclusive at the endpoint level — no schema promotion.
- [ ] **T-PS4** **Replay safety (stateless, not one-time):** replaying a `ProjectSelectToken` at `POST /owner/project-pick` only mints an equivalent fresh `ProjectContextToken` and MUST NOT change any business/domain state (no Project row written, no inbox mutation, no audit written) — V0 has no consumed-once table; "consumed once" is deleted.
- [ ] **T-PS5** Expired `ProjectSelectToken` / `ProjectContextToken` (`exp` beyond ±30s skew) → uniform "该条目已失效" (PHASE 1), no `ProjectContextToken` minted / no inbox listed.

**Project display freshness (stale context invalidation):**
- [ ] **T-PS6** Business label changed after mint: the Project `name` (or `updated_at`) changes after the `ProjectSelectToken` is minted → recomputed `project_display_binding` mismatches → fail closed (uniform "该条目已变更，请刷新收件箱"), no `ProjectContextToken` minted. (Same for a stale `ProjectContextToken` at the inbox-list GET.)
- [ ] **T-PS7** Project deleted / not-found after mint: the Project row is missing / non-live → `project_ref` load fails → fail closed (uniform "该条目已失效").

**Schema-confusion (PHASE 3 rejects missing-required / extra-forbidden / token_type↔purpose mismatch / endpoint mismatch; extra forbidden claims never silently ignored):**
- [ ] **T-SC1** `project_select` token carrying a forged `inbox` / `kind` / `rid` / `resource_scope` (FORBIDDEN fields) → rejected (extra forbidden claims), never silently ignored.
- [ ] **T-SC2** `project_context` token carrying a forged `inbox` / `kind` / `rid` / `resource_scope` / inbox-action `display_binding` → rejected (extra forbidden claims).
- [ ] **T-SC3** `detail_view` token missing `inbox` → rejected (REQUIRED field missing, PHASE 3).
- [ ] **T-SC4** `detail_view` token missing `kind` → rejected (REQUIRED field missing, PHASE 3).
- [ ] **T-SC5** `inbox_action` token missing `inbox` → rejected (REQUIRED field missing, PHASE 3).
- [ ] **T-SC6** `inbox_action` token missing `kind` → rejected (REQUIRED field missing, PHASE 3).
- [ ] **T-SC7** `token_type="project_context"` with `purpose="content.approve"` → rejected (`token_type`↔`purpose` mismatch; a context token may only carry `project_inbox`).
- [ ] **T-SC8** `token_type="inbox_action"` with `purpose="project_select"` → rejected (`token_type`↔`purpose` mismatch).
- [ ] **T-SC9** Each schema submitted to another schema's endpoint → rejected (e.g. `project_select`→inbox-list, `project_context`→decide, `detail_view`→decide, `inbox_action`→detail/pick/list) — endpoint mismatch.
- [ ] **T-SC10** Unknown `token_type` (anything outside the whitelist `project_select`/`project_context`/`detail_view`/`inbox_action`) → fail closed at PHASE 2.

**V0 Project authorization — simple & explicit (P1-3, no ACL / no revocation state):**
- [ ] **T-AU1** A successfully authenticated owner + a live Project A and a live Project B are each allowed (the *only* success condition is auth + LIVE Project whose `project_ref` resolves to that exact Project + valid `project_display_binding`; there is NO ProjectOwner table / per-project ACL / membership list / per-project role / revocation mapping / hidden session ACL).
- [ ] **T-AU2** Non-existent / deleted Project reference (`project_ref` → no row) → fail closed (uniform "该条目已失效").
- [ ] **T-AU3** Token's `project_ref` resolves to Project A but the token's `operating_project` claims Project B (or the live row's id is B) → fail closed (§2.1.2a step 8 / §2.1.2b step 7); `project_ref` and `operating_project` are never collapsed.
- [ ] **T-AU4** Wrong Basic-auth owner (a different authenticated owner than `claims.owner`) → fail closed (PHASE 1, §2.1.2 step 5).
- [ ] **T-AU5** Forged `operating_project` (token asserts a project the sealed `project_ref` does not resolve to) → fail closed (§2.1.2a step 8).
- [ ] **T-AU6** Tampered `project_ref` (does not match an authorized live Project) → fail closed.
- [ ] **T-AU7** Expired project token (`exp` beyond ±30s skew) → fail closed (PHASE 1).
- [ ] **T-AU8** **Company operating context forbidden in V0 (P1-3):** no V0 endpoint may mint a `ProjectSelectToken` or `ProjectContextToken` with `operating_project="company"`; the Project-select resolver (§2.1.2a step 12), the Project-context resolver (§2.1.2b step 10), the Detail-view resolver (§2.1.2c step 5), and the Inbox-action resolver (§2.1.2d step 5) all reject `operating_project == "company"` with a uniform message; there is **no** V0 route that enters a company operating context (company context is FUTURE-only, §6.4).

### Detail-view token  [P1-2]
- [ ] **T-D1** No raw resource ID in any owner-facing surface: the inbox LIST HTML, the detail URL (`POST /owner/inboxes/{kind}/detail` with body `detail_token`, never a `{rid}` route segment), the detail JSON/HTML, server logs, and referrers contain **no** raw `rid` / database PK / `kind` / `operating_project` / `resource_scope` / `display_binding` (grep the responses + logs). The `rid` is only ever inside the sealed `detail_view` token.
- [ ] **T-D2** Valid `DetailViewToken` resolves the correct `(kind, rid)` row and renders the detail page with fresh `inbox_action` token(s); `token_type == detail_view` + `purpose == view_detail` + `inbox`/`kind` match + operating-project auth + live `resource_scope` + live `display_binding` all pass.
- [ ] **T-D3** Wrong owner / wrong `operating_project` / wrong `resource_scope` → resolver fails (step 3 / scope resolver) → fail closed.
- [ ] **T-D4** Stale `display_binding`: the underlying row changed after the detail token was minted → recomputed digest mismatches → fail closed (uniform "该条目已变更，请刷新收件箱").
- [ ] **T-D5** Expired `DetailViewToken` → uniform message (step 5).
- [ ] **T-D6** `DetailViewToken` submitted to a decision endpoint (`POST /decide`) → rejected (purpose `view_detail` != endpoint's allowed action) before any mutation.
- [ ] **T-D7** `InboxActionToken` submitted to the detail endpoint (`POST /detail`) → rejected (`token_type`/`purpose` != `view_detail`) before any view render.
- [ ] **T-D8** Changed underlying resource after list render: the list minted a `detail_view` token for `(kind, rid)`; the row is then edited/deleted; the old detail token is submitted → live `display_binding` / row-load fails → fail closed (no stale detail).

### Project / auth isolation  [R2-4 / F4]
- [ ] **T-P1** No OOL request accepts a `project_id` parameter; inbox LIST renders no project identifier / no per-project count; LIST is scoped to the project bound in the sealed token (no global cross-project aggregation); the project-pick flow uses a sealed `project_select` token (§4.1 / P1-2).
- [ ] **T-P2** Wrong-project token: a token minted in project A, presented under project B's operating context (a `detail_view`/`inbox_action` whose `operating_project` disagrees with the live row's `resource_scope`) → **fail closed** (403 uniform); the universal scope resolver (§2.1.3) rejects the mismatch before any mutation.
- [ ] **T-P3** Cross-project auxiliary selection / count / audit query → all scoped to the token-bound project; no leakage of other projects' items.
- [ ] **T-P4** 404 / 403 / 409 responses are normalized (uniform bodies, no timing/enumeration oracle).
- [ ] **T-P5** LIST pagination bounded + deterministic over items; no cross-project enumeration endpoint; no project-list endpoint that exposes raw project IDs (§4.1 / P1-2).

### Concurrency (multi-session)  [R2-6]
- [ ] **T-CC1** Two concurrent approve attempts on the same content item → exactly one APPROVED; the other → 409 (CAS under `BEGIN IMMEDIATE`).
- [ ] **T-CC2** Two concurrent `owner_confirm_suggestion` on the same suggestion (two sessions) → exactly one outbound `Message`; the other → 409 already-handled (consumed guard).
- [ ] **T-CC3** Two concurrent Feedback approve on the same artifact → exactly one DEVELOP; other → 409 (checksum/revision CAS).
- [ ] **T-CC4** Two concurrent Knowledge approve on the same candidate → exactly one APPROVED fact; other → 409 (status CAS + `display_binding` head_version check).

### Usability  [F6]
- [ ] **T-U1** A non-technical tester completes all four journeys using only business labels; zero internal-ID fields encountered.

### Cross-cutting / relay  [F8 / R2-8]
- [ ] **T-X1** `OWNER_RELAY_COUNT` harness asserts zero internal-ID input fields are presented/required of the owner across all four journeys (internal technical relay = 0).
- [ ] **T-X2** **Itemized relay ledger (R2-8):** the UAT records, per journey, every manual handoff; any copy/remember/translate/transfer of a technical value → FAIL. Business-label selections are logged separately and bounded per §9.1 ceilings.
- [ ] **T-X3** Full `ruff check src tests alembic` + `pytest` green; `git diff --stat` shows the 4 OOL files + `customer_service.py` extension + `tests` + `pyproject.toml`/lock (no model/migration change beyond the `_human_send` extension).
- [ ] **T-X4** No new Alembic migration; `alembic heads` single head `20260731_0001` preserved.
- [ ] **T-X5** **Leakage via previews/aux/audit/pagination:** previews are PII-redacted + business-labeled; auxiliary lists (MARK_DUPLICATE picker, series dropdown) are project-scoped; audit views are project-scoped; pagination exposes no internal ID / count.

---

## 13. Synthetic Human UAT acceptance criteria

A non-technical "owner" actor (Codex as UAT Actor per Protocol V1) drives the four journeys against a fresh disposable SQLite DB, using **only** the OOL console, with no access to IDs/checksums/revisions/enums. The owner first selects an **operating project by business label** (§4.1); all journeys run within that project.

| Journey | Pass criterion | `OWNER_RELAY_COUNT` target |
|---|---|---|
| Content | create → independent review passes → owner approves (no ID copy) → APPROVED; reject + edit-resubmit paths also work (honest label, partial-failure recovery) | **≤ 1** |
| Customer-Service | ingest → suggestion HUMAN_CONFIRM → owner adopts & sends (no ID copy, one-shot, 409 on replay) ; escalate path works | **≤ 2** |
| Feedback | submit → SOLUTION → owner approves (no ID copy) → DEVELOP; defer/duplicate/request-clarify paths work | **≤ 2** |
| Knowledge | candidate DRAFT → owner classifies + approves (no series/version typed, business-label series) → APPROVED fact; deactivate works | **≤ 2** |

- All four journeys complete **without the owner ever copying an internal identity or relaying technical state**; internal technical relay measured at **0**; the **itemized relay ledger** (§9.1 / T-X2) shows only business-label selections.
- No automatic `KnowledgeFact` created by content approval; no external publication; no paid LLM call in the happy path (consistent with #108-A / #109 / #110 / #119).
- Human-readable errors verified on injected stale/already-decided/forbidden/wrong-project/tampered-token states (including the rev2→rev3 and same-status-replacement cases).
- Audit trail readable post-hoc for each decision (project-scoped).

---

## 14. Implementation tasks (future — post plan-merge gate)

> These run only after the plan PR is Codex-approved, CloudCode-approved, and owner-merge-authorized. They are listed here so reviewers can see the full shape; **none are executed in this plan-only PR.**

- [ ] **Task 1:** Implement `owner_inbox.py` — `OwnerSealedToken` **AES-256-GCM seal/resolve** (`cryptography.hazmat.primitives.ciphers.aead.AESGCM`, cleartext `kid` header as AAD, §2.1) with `kid`/rotation/fail-closed key load (§2.1.1/§2.1.3); **four schemas** (`project_select` selection-only token with mandatory Project-row-load resolver §2.1.2a that mints a `project_context` token, `project_context` inbox-entry token §2.1.2b, `detail_view` exact-resource view §2.1.2c, `inbox_action` decision §2.1.2d); three-phase resolver (PHASE 1 common / PHASE 2 whitelist dispatch / PHASE 3 schema-specific, §2.1.2); four inbox read projections; action adapters; per-inbox `display_binding` + project-label `project_display_binding` computation (§2.4). Zero models/migration.
- [ ] **Task 2:** Implement `api/owner_inbox_routes.py` — owner-authed GET/POST; delegate to §2.2 bindings; no `project_id` request param (§4.1); project context from the sealed `project_select` → `project_context` bootstrap (stateless — no global session); `GET /owner/inboxes/{kind}?ctx=<project_context>` lists items and mints `detail_view` tokens; `POST /owner/inboxes/{kind}/detail` resolves `detail_view` and mints `inbox_action` token(s); `POST /owner/inboxes/{kind}/decide` resolves `inbox_action`.
- [ ] **Task 3:** Extend `console.py` — `/owner/project-picker` (GET, server-rendered business labels + sealed `project_select` (`project_select`) tokens) + `POST /owner/project-pick` (Project-select resolver §2.1.2a validates `project_select`, mints a `project_context` token) + `/owner/inboxes` hub + 4 pages (entered with `?ctx=<project_context>`) + `POST /owner/inboxes/{kind}/detail` (renders detail + mints `inbox_action` tokens) + detail/audit views (all server-rendered, business-labeled §6.5; **no mutable `selected_project` session** — project binding travels in sealed tokens, P1-2).
- [ ] **Task 4:** **Minimal CustomerService domain extension** — implement `owner_confirm_suggestion` composing `consumed` + `idempotency_key` + `send_message`; extend `customer_service.py:_human_send` to bind `suggestion_id` + mark `consumed` + honor `idempotency_key` (repairs gap; zero migration). **One replay contract: 409 already-handled, no second send (§3.2).** No claim of a non-existent separate edit path — owner edit is the `edited_text` arg.
- [ ] **Task 5:** Add `cryptography` to `pyproject.toml` + lockfile (dependency metadata only, R2-1).
- [ ] **Task 6:** Write `tests/test_owner_inbox.py` covering §12 matrix (RED→GREEN), including the rev2→rev3 and same-status-replacement cases.
- [ ] **Task 7:** Run `ruff` + full `pytest`; verify zero migration / single Alembic head; verify `AIOS_OOL_TOKEN_CURRENT_KEY_B64` fail-closed; verify `cryptography` importable.
- [ ] **Task 8:** Rerun Synthetic Human UAT (§13); record **itemized relay ledger** per journey (internal technical relay = 0).
- [ ] **Task 9:** Open implementation PR → Codex review → CloudCode adversarial/usability review → owner merge gate. **No auto merge.**

---

## 15. Handoff (Protocol V1 — correction gate → next Codex re-review)

### 15.1 R2 finding → revised plan section → contract added/changed (historical — resolved in Round 2)
| R2 finding | Severity | Revised section(s) | Exact contract added/changed |
|---|---|---|---|
| **R2-1** token envelope contradiction + undeclared crypto | P1 | §2.1, §2.1.1, §2.1.3, §11, §12 T-T1..T-T9, §14 T1/T5 | AES-256-GCM via declared `cryptography`; wire `header.nonce.ct` with cleartext `kid` header as AAD; new env names `AIOS_OOL_TOKEN_CURRENT_KID`/`_KEY_B64` + optional previous-key vars with absolute-UTC deadline; base64url-only, 32-byte validation; `cryptography` added to `pyproject.toml`+lock (metadata, not schema). |
| **R2-2** display_binding not in token | P1 | §2.3, §2.4, §3.2, §3.3, §3.4, §12 T-C3/T-S8/T-F6/T-K8 | Mandatory per-inbox `display_binding` (sha256 of canonical JSON) frozen in sealed token; decision-time recompute + `compare_digest`; mismatch fail closed; explicit rev2→rev3 + CS/Feedback/Knowledge same-status-replacement tests. |
| **R2-3** CS replay contradiction | P1 | §3.2, §7, §10.2, §12 T-S3/T-S9, §13, §14 T4 | **ONE** contract: after terminal/consumed send, replay → 409 already-handled, no second Message/adapter/audit; removed "returns prior result"; `owner_confirm_suggestion` declared as minimal CustomerService domain extension; no non-existent edit-path claim. |
| **R2-4** project scope contradiction | P1 | §4.1, §6.1/§6.3/§6.4, §12 T-P1..T-P5, §13 | Owner selects project **by business label**; project context derived server-side and (post correction gate, §15.1b P1-2) carried in **sealed tokens — no mutable session**; LIST/aux/count/audit/mutation all scoped; token `project` triple-checked (token == claims == live row); wrong-project token → fail closed. |
| **R2-5** Content edit dishonesty + partial failure | P1 | §3.1, §6.1, §12 T-C5, §13 | Renamed to "由负责人编辑并重新送审" (truthful); `NEEDS_REVISION` included in inbox so item never vanishes; partial-failure recovery defined (update success + submit fail → stays `NEEDS_REVISION`, never "复审中"). |
| **R2-6** TDD matrix incomplete | P1 | §12 (T-T7..T-T9, T-C3, T-S8/T-S9, T-F6/T-F7, T-K7/T-K8, T-P1..T-P5, T-CC2..T-CC4, T-X2/T-X5) | Expanded matrix with explicit RED→GREEN for malformed envelope, key rotation, wrong-project, terminal replay per family, CS edited reply, Feedback same-stage replacement + clarify, Knowledge provenance + no bypass, concurrency for Feedback/Knowledge, same-status replacement for all four, leakage via previews/aux/audit/pagination, and real relay ledger. |
| **R2-7** Knowledge series name / version conflict | P1 | §3.4, §6.4, §6.5, §12 T-K7 | Series shown by **server-derived stable business label** (no new column; persistent name flagged as future owner decision); removed "vN" from owner display (uses `#<seq>`); `version` internal-only. |
| **R2-8** relay measurement thin | P2 | §9.1, §12 T-X2, §13 | Measures **actual** relay (copy/remember/translate/transfer) via **itemized ledger per journey**; fails on any technical value; business selections logged separately + bounded. |

### 15.1b Correction-gate findings → revised plan section → contract added/changed (CloudCode/DeepSeek independent adversarial review, head `14b4105`)
| Finding | Severity | Revised section(s) | Exact contract added/changed |
|---|---|---|---|
| **P1-1** Content partial-failure used wrong status (`NEEDS_REVISION`) | P1 | §3.1 (step 1 & 4), §4 RW table, §6.1, §8, §12 T-C5 | `update_content_draft` commits `UNVERIFIED` (`content_draft.py:434`), **not** `NEEDS_REVISION`; Content Owner Inbox includes `REVIEW_PASSED` / `UNVERIFIED` / `NEEDS_REVISION`; partial-failure UI = *"编辑已保存，但重新送审未成功，请重试送审。"*; owner resubmits without re-discovering Artifact ID; `content_draft.py` untouched. |
| **P1-2** Project context implied a mutable session | P1 | §4.1 (full rewrite), §2.1.2 step 7, §6.1/§6.2/§6.3/§6.4, §11, §14 T3 | **Stateless** model: `GET /owner/project-picker` (server-rendered business labels only) + sealed `project_select` token + `POST /owner/project-pick` bootstrap; project binding carried in **every** OOL token; **no mutable `selected_project` session**; deterministic multi-tab; removed contradictory "No project list endpoint" sentence; **CSRF contract** added (sealed single-purpose action token = CSRF defense, no anti-CSRF cookie). |
| **P1-3** Knowledge scope used `source_project_id` | P1 | §4.1, §6.4, §12 T-K1/T-K9..T-K15 | **`scope != provenance`**: authoritative scope = `project_id` (NULL = company-wide, visible in all contexts); `source_project_id` is provenance-only, never used for auth/scope filtering; candidate/fact query `(project_id == <operating_project> OR project_id IS NULL)`; company-wide fact is **read-only** from project context (no `deactivate`); 7 new tests. |
| **P1-4** `owner_confirm_suggestion` wrapping `_human_send` / audit id | P1 | §2.2, §3.2 (contract a–g + single-txn), §8, §12 T-S3, §14 T4 | Defined as **one** authoritative domain op with its **own single `BEGIN IMMEDIATE` transaction** that does **NOT** wrap/call `_human_send` (`customer_service.py:453`, own `BEGIN IMMEDIATE` at `:464`); reuses low-level `send_message` primitive; new audit id `audit:cs:outbound:owner:send:{suggestion_id}` (distinct from `CsSuggestion.idempotency_key`, `AuditLog.idempotency_key` globally unique); **MockWeCom honesty** — DB-level one-shot/idempotency guaranteed, **not** claiming exactly-once external delivery. |
| **P2-1** Series business label ambiguous | P2 | §3.4 (point 5), §6.5 | **SINGLE deterministic algorithm**: domain label if available, else `"系列 #N"` by creation order within authoritative scope (deterministic tiebreak); display-only, never an auth/mutation identity; mutation resolves from sealed internal binding. |
| **P2-2** `facts_binding` undefined | P2 | §2.4.1 (new), §12 T-S10 | **Canonical** `facts_binding = "sha256:" + sha256(json.dumps({ref: fact_revisions[ref] for ref in sorted(refs)}, sort_keys, separators, ensure_ascii=False).encode("utf-8"))` — precise fields / sorted refs / UTF-8 / compact separators / `sha256:` prefix / empty-set constant; order-independent test T-S10. |

### 15.1c Codex R5 architecture re-review findings → revised plan section → contract added/changed (independent Codex R5, head `4a580cc24d47b464010a5aacc1d462d3a51ca684`)
| Finding | Severity | Revised section(s) | Exact contract added/changed |
|---|---|---|---|
| **P1-1** `project` claim overloaded for navigation context + knowledge scope | P1 | §2.1 (claims split), §2.1.2 step 7 (8-step resolver), §2.3 step 5, §4.1 (resolver rules), §6.4 (read-only company rows), §12 T-K9..T-K17 | Split sealed claims into `operating_project` (frozen from `project_nav` bootstrap) + `resource_scope` (re-derived from live row, `project_id`, never `source_project_id`); replaced the impossible `token_project == claims.project == live_row_project` triple with an **8-step universal resolver** computing visibility (`resource_scope == operating_project` OR `== 'company'` OR `operating_project == 'company'`) and mutability (`resource_scope == operating_project` OR `operating_project == 'company'`) independently; **company-wide Knowledge candidates AND facts are read-only from a project context** (`classify`/`approve`/`reject`/`deactivate` withheld); T-K9..T-K15 reworded to `operating_project`, T-K14→read-only candidate, T-K16 company-candidate mutation forbidden, T-K17 series stability. |
| **P1-2** `project-pick token` not conformant + tautological triple-check | P1 | §2.1 (OwnerSealedToken envelope), §2.1.2 (token_type dispatch), §2.1.2b (detail-view resolver), §4.1 (single-token transport), §6.4 (T-K16), §12 T-D1..T-D8 | Defined **one** shared `OwnerSealedToken` AES-256-GCM envelope wrapping a `token_type` discriminator with **three** schemas: `project_nav` (purpose `project_select`, mandatory Project-row-load resolver §2.1.2a, no business-row load), `detail_view` (purpose `view_detail`, exact-resource detail, `rid` stays encrypted — **no raw `{rid}` in the URL**), and `inbox_action` (the decision token). The `project-pick token` IS a `project_nav` token — it conforms to the universal schema (no second format). Every OOL request carries **exactly one** sealed token; transport picker→inbox→detail→action carries no raw project ID and no raw resource ID. Deleted the fictional independent `token_project` comparison. |
| **P1-3** `UNVERIFIED`/`NEEDS_REVISION` collapsed into one composite `request_edit` | P1 | §2.2 (two rows), §2.4 (two bindings), §3.1 (step 3 + partial-failure), §4 RW table, §6.1 (decisions), §7 (actions), §12 T-C5/T-C8 | Split into distinct **`content.resubmit`** (UNVERIFIED, calls **only** `submit_content_draft`, never `update_content_draft`, never bumps `revision_count`) and **`content.edit_and_resubmit`** (NEEDS_REVISION, `update_content_draft` + `submit_content_draft`), each with its own canonical `display_binding`; 12-step acceptance test T-C8 proving no double `update_content_draft` on partial-failure retry. |
| **P2-1** Public `kid` test contradiction | P2 | §12 T-T4 | T-T4 now asserts only that no secret key / raw `rid` / `operating_project` / `resource_scope` / `project_id` / `series` / `version` / checksum / revision / decrypted claims appear separately; the **public base64url `kid` header is explicitly permitted**; tampering with it breaks AEAD auth. |
| **P2-2** Series label deterministic but not stable | P2 | §3.4 (point 5), §12 T-K17 | Series `#N` now ranked by **immutable series-root `created_at`** (earliest fact in series, unchanged by head replacement) with `series_id` tiebreak; label documented as **ephemeral display-only** + business provenance; replacement head for series X cannot relabel unrelated series Y (T-K17). |

### 15.1d Codex R6 architecture re-review findings → revised plan section → contract added/changed (independent Codex R6, head `622e544c220e7e9477d027de418f3248be7fddfe`)
| Finding | Severity | Revised section(s) | Exact contract added/changed |
|---|---|---|---|
| **P1-1** Project-nav token did not resolve a real Project (no authoritative Project-row load; `operating_project` was self-asserted) | P1 | §2.1 (claims: `project_ref` + `project_display_binding` added to the nav token, wire renamed `token_type = "project_nav"`), §2.4 (PROJECT — `project_select`/`project_nav` binding block), §2.1.2a (NEW 11-step Project-nav resolver), §4.1 (picker→nav carries `project_ref` + `operating_project` + `project_display_binding`), §12 T-PN1..T-PN8, §15.1c P1-1 row | Added sealed `project_ref` (internal stable Project identity — used ONLY to load the authoritative Project row, never exposed to owner) + `project_display_binding` to the `project_nav` token; canonical binding = `{"project_ref": <Project.id>, "business_label": <Project.name>, "updated_at": <Project.updated_at>}` → sha256 (uses only existing `models.py:231-247` columns, **zero migration**); **11-step resolver** loads the Project row by `project_ref`, fails closed on missing/deleted/wrong-owner/expired/tampered/`operating_project` mismatch/`project_display_binding` mismatch; 8 new tests (T-PN1 valid; T-PN2 label changed; T-PN3 deleted; T-PN4 wrong owner; T-PN5 expired; T-PN6 tampered; T-PN7 `project_ref`→A but `operating_project`→B; T-PN8 owner loses auth post-mint) — all fail closed except valid. |
| **P1-2** Raw `{rid}` detail route leaked the resource id + only two token schemas existed | P1 | §2.1 (third `detail_view` schema `DetailViewToken`), §2.1.2 (intro → three-token dispatch), §2.1.2b (NEW detail-view resolver), §4.1 (deleted `GET /owner/inboxes/{kind}/{rid}?...`; added `POST /owner/inboxes/{kind}/detail` body `detail_token=<DetailViewToken>`), §6 (InboxItem `token` field → `DetailViewToken`), §11 (file map), §12 T-D1..T-D8, §15.1c P1-2 row | Removed the raw `{rid}` detail route; defined a **third** `detail_view` sealed schema (`DetailViewToken`, `token_type = "detail_view"`) that reuses the same `OwnerSealedToken` AES-256-GCM envelope + key mgmt (no second crypto); `rid` stays encrypted; transport `POST /owner/inboxes/{kind}/detail` with body `detail_token` (reference out of URL); `DetailViewToken` accepted only by the detail endpoint and rejected by mutation endpoints, `InboxActionToken` rejected by the detail endpoint; 8 new tests (T-D1 no raw id anywhere + grep logs; T-D2 valid resolves; T-D3 wrong owner/operating_project/resource_scope; T-D4 stale display_binding; T-D5 expired; T-D6 detail→decide rejected; T-D7 action→detail rejected; T-D8 changed underlying resource fails). |
| **P2-1** `series_id` leaked into owner-facing provenance | P2 | §3.4 (points 3 & 5), §6.5, §12 T-K17 | Removed every owner-facing reference to `series_id`; owner-facing Knowledge provenance now carries **business-readable context only** (business series label — never an internal `series_id`/UUID/PK/version/canonical-tag id); internal/server-side series identity MAY remain in sealed-token binding specs and model references but is FORBIDDEN in owner UI/HTML/JSON/provenance/relay. |
| **P2-2** T-C8 step 12 left a completed review as `UNVERIFIED` | P2 | §12 T-C8 step 12 | Corrected T-C8 step 12: a **completed** independent review resolves the item to **`REVIEW_PASSED` or `NEEDS_REVISION`** — it MUST NOT remain `UNVERIFIED`; `UNVERIFIED` is only the pre-review / retryable state entered by `update_content_draft`; #108-A/#119 hard contracts preserved (no revision bump on review; approve path unchanged). |

### 15.1e Codex R8 architecture re-review findings → revised plan section → contract added/changed (independent Codex R8, head `11e503a11d4c744dae917f717843293d728283bd`)
| Finding | Severity | Revised section(s) | Exact contract added/changed |
|---|---|---|---|
| **P1-1** Overloaded `project_nav` token conflated project *selection* with *navigation / inbox entry*; V0 lifecycle between `POST /owner/project-pick` and `GET /owner/inboxes` undefined; replay/endpoint-targeting ambiguous | P1 | §2.1 (split nav into **two** schemas: `ProjectSelectToken` `project_select` + `ProjectContextToken` `project_context`, same `OwnerSealedToken` envelope — no second crypto), §2.1.2 (§2.1.2a 13-step Project-select resolver mints `ProjectContextToken`; §2.1.2b 13-step Project-context resolver for inbox list), §4.1 (five-step chain picker→select→context→detail→action; `GET /owner/inboxes/{kind}?ctx=<ProjectContextToken>`), §2.4 (PROJECT — `project_select`/`project_context` binding; old `ProjectNavToken` wording removed), §11/§14 (four schemas, ctx route, project_select bootstrap), §12 (T-PS1..T-PS7 lifecycle + T-SC1..T-SC10 schema-confusion) | Split the single `project_nav` token into two explicit one-type-one-purpose schemas sharing the one AES-256-GCM envelope + key mgmt: `ProjectSelectToken` (only `POST /owner/project-pick`, 13-step resolver mints `ProjectContextToken`) and `ProjectContextToken` (only `GET /owner/inboxes/{kind}`, grants inbox listing only). Together with `DetailViewToken`/`InboxActionToken` there are now **four** schemas; normative chain picker → select → context → detail → action; `ProjectSelectToken` is stateless, NOT cryptographically one-time (replay only mints equivalent fresh context, never changes domain state — "consumed once" deleted). |
| **P1-2** Universal resolver demanded `inbox`/`kind` before token-type dispatch (impossible for selection/context tokens) | P1 | §2.1.2 (three-phase resolver: PHASE 1 envelope/common-field validation with NO schema-specific field; PHASE 2 whitelist token-type dispatch; PHASE 3 schema-specific REQUIRED/FORBIDDEN/purpose/endpoint/method validation; extra forbidden claims rejected, never silently ignored), §2.1.3 (universal scope resolver extracted, company branch deleted), §2.1.2c/§2.1.2d (detail/action resolvers), §4.1 (CSRF enumerates four token types; inbox-list GET requires `project_context`), §12 (T-SC1..T-SC10) | Restructured the resolver into three explicit phases so dispatch never requires a schema-specific field first; added the normative schema field table (REQUIRED/FORBIDDEN per schema); unknown `token_type` fails closed at PHASE 2; schema confusion (token of one schema at another's endpoint, missing required, extra forbidden, token_type↔purpose mismatch) rejected at PHASE 3. |
| **P1-3** Plan invented non-existent per-project ACL / revocation states ("owner authorization revoked after mint") | P1 | §2.1.2a step 6, §2.1.2b step 6, §2.1.2d step 4 (simple/explicit V0 Project auth — no ACL/revocation), §6.4 (Future note marked NON-NORMATIVE), §12 (T-AU1..T-AU8 replace fictional revocation; T-AU8 forbids `operating_project=company` in V0), §2.1.2 (V0 envelope invariant: `company` forbidden at mint/resolution) | Froze the **complete** V0 Project authorization model: the *only* success condition is a successfully authenticated owner acting on a **LIVE** Project row whose sealed `project_ref` resolves to that exact Project and whose `project_display_binding` is valid. No ProjectOwner table / per-project ACL / membership list / per-project role / revocation mapping / hidden session ACL; the fictional "revoked after mint" test is deleted. `operating_project == "company"` is rejected at every mint/resolution/navigation/detail/action; no V0 route enters a company operating context (company context is FUTURE/NON-NORMATIVE only). Company-wide Knowledge read-only-from-project-context (R5/R6) preserved, not reopened. |

### 15.2 Process metadata (Round 8 correction revision)
- **Objective:** close the three P1 findings from the independent **Codex R8** architecture re-review (head `11e503a11d4c744dae917f717843293d728283bd`) — P1-1 (split overloaded `project_nav` into two explicit schemas `ProjectSelectToken` + `ProjectContextToken`; define the V0 selection→navigation→detail→action lifecycle), P1-2 (restructure the universal resolver into three phases so token-type dispatch never requires an `inbox`/`kind` first; add the normative schema field table + schema-confusion rejection), P1-3 (freeze the simple/explicit V0 Project authorization model — no per-project ACL / revocation states; forbid `operating_project=company` in V0) — preserving every R2-1..R2-8, Round-4, R5, R6, CloudCode/DeepSeek correction-gate, and all accepted R6 contracts (`project_ref`/`project_display_binding`, `DetailViewToken` no raw detail id, no owner-facing `series_id`, REVIEW_PASSED/NEEDS_REVISION completion, `operating_project`/`resource_scope` split, content `resubmit`/`edit_and_resubmit` split, public `kid`); keep the PR **plan-only** (zero code / zero migration).
- **Completed:** revised plan doc only (this file) — `docs/superpowers/plans/2026-08-02-owner-operating-layer-v0.md`.
- **Evidence:** n/a (plan only).
- **Changed files:** `docs/superpowers/plans/2026-08-02-owner-operating-layer-v0.md` only.
- **New exact head SHA (Round-8 plan content):** recorded in the PR #122 re-review request comment after push (Next Gate step 3) — the exact 40-char **branch tip** is the authoritative review head and is intentionally NOT self-embedded here (it drifts once committed/pushed; per the Round-6 convention). The substantive R8 content and this §15.2 process metadata are committed together in one R8 plan-only commit.
- **Hard contracts checked:** §10 (all seven preserved), §1.2 exclusions, §1.3 absolute rule, §1.5 zero-migration re-affirmed (only `cryptography` metadata; all four token schemas reuse the existing `OwnerSealedToken` envelope + existing `Project.id`/`name`/`updated_at`, no new persistent field), §2.1 `OwnerSealedToken` envelope (AES-256-GCM, **four** `token_type` schemas `project_select`/`project_context`/`detail_view`/`inbox_action`), §2.1.2 three-phase resolver (PHASE 1 envelope/common, PHASE 2 whitelist dispatch, PHASE 3 schema-specific + normative field table), §2.1.2a (13-step Project-select resolver), §2.1.2b (13-step Project-context resolver), §2.1.2c (detail-view resolver), §2.1.2d (inbox-action resolver), §2.1.3 universal scope resolver (company branch deleted), §2.4 `project_select`/`project_context` binding + display_binding canonicalization, §4.1 stateless four-token transport (no raw `{rid}` route) + CSRF (four token types), §3.4/§6.5/T-K17 series owner-facing removal (P2-1), §12 T-C8 completed-review resolution (P2-2), T-PS1..T-PS7 + T-SC1..T-SC10 + T-AU1..T-AU8 + T-D1..T-D8.
- **Next Codex architecture-rereview verdict (R9):** *PENDING — Codex will post a PR #122 top-level comment after re-review of the branch tip (exact SHA in the PR #122 re-review request comment).*
- **CI result:** **PENDING** — exact-head run on the branch tip to be triggered after push; plan-only change (no code / no test / no migration touched), so the gate is satisfied trivially and honestly once green.
- **GitHub comment reference:** PR #122 re-review request comment (branch tip SHA + `next:codex` label set) + Issue #121 traceability comment.
- **Failures/Risks:** none in plan; implementation risks = key rotation procedure + stale-resolution UX + token TTL tuning + `cryptography` dependency availability in CI + stateless multi-tab token binding + three-phase resolver ordering + Project-select/Project-context 13-step ordering + detail-view `rid` confidentiality + schema-confusion coverage (covered by T-T1..T-T9 / T-PS1..T-PS7 / T-SC1..T-SC10 / T-AU1..T-AU8 / T-D1..T-D8 / T-C8 / T-K17).
- **Next action:** Codex architecture **re-review (R9) of the revised plan** at the branch tip (exact SHA in the PR #122 re-review request comment).
- **Next owner:** `codex`.
- **Owner gate:** plan merge requires owner authorization (No auto merge). If the next Codex verdict = `REQUEST_CHANGES_ARCHITECTURE_PLAN` → `next:workbuddy`; if `APPROVE_ARCHITECTURE_PLAN` → `next:cloudcode`; CloudCode must NOT review before Codex architecture approval.
