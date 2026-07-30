# Secret Store Operations (V4 self-update credentials)

This document covers how to operate the V4 agent credential secret store that
was implemented in issue #103 and hardened with startup fail-closed validation
in the follow-up. It is the operator-facing companion to
`docs/issue-103-secret-store-plan.md`.

## What it is

V4 agent self-update bearer credentials are **never** persisted as plaintext or
reversible ciphertext. The secret store maps a bearer credential to an
`agent_id` and supports rotation. Two backends implement the same contract:

| Backend | Persistence | Key material | Use case |
| --- | --- | --- | --- |
| `memory` (default) | in-process dict | none | tests, single-process dev |
| `encrypted_db` (opt-in) | `agent_secret` table | `AIOS_SECRET_MASTER_KEY` | production, multi-process |

For `encrypted_db`, only KEK-derived HMAC tags are stored:

- `token_tag = HMAC-SHA256(KEK, token)` — one-way; the token itself is never
  written.
- `row_mac = HMAC-SHA256(KEK, agent_id || token_tag)` — cryptographically binds
  each tag to its agent so a tag cannot be transplanted to another row.

## Choosing a backend

**Default (`memory`)** is correct for development, CI, and single-process
deployments. Credentials live only in the process; a restart invalidates all
issued credentials (agents must re-bootstrap). No environment variables needed.

**`encrypted_db`** is for deployments that need credentials to survive restarts
or be shared across worker processes. It is **opt-in** — you must set both the
backend and a valid key.

## Provisioning the KEK (encrypted_db only)

The master key (`AIOS_SECRET_MASTER_KEY`, the KEK) MUST be exactly **32 bytes**,
supplied as either hex (64 chars) or base64 (urlsafe or standard). Any other
length or format is rejected (fail-closed).

Generate a key (pick one):

```bash
# 32 random bytes -> 64-char hex
python - <<'PY'
import secrets
print(secrets.token_hex(32))
PY

# 32 random bytes -> base64 (urlsafe)
python - <<'PY'
import secrets, base64
print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
PY
```

Set it in the environment / secret manager (never commit it):

```bash
export AIOS_SECRET_STORE_BACKEND=encrypted_db
export AIOS_SECRET_MASTER_KEY="<64-hex-or-base64-value>"
```

Store the KEK in your secret manager (Vault, AWS Secrets Manager, Doppler,
etc.). **Losing the KEK means existing credentials can no longer be resolved**
(they will 503 until re-issued via bootstrap/rotate). Rotating the KEK requires
re-issuing all agent credentials.

## Fail-closed behavior

Two independent fail-closed layers protect against misconfiguration and
outages:

1. **Startup validation** (`validate_secret_store_config()` in `lifespan`).
   If `encrypted_db` is selected but the KEK is missing/invalid, or an unknown
   backend is set, the process **refuses to start** with a readable error:

   ```
   secret store misconfigured: backend 'encrypted_db' requires a valid
   AIOS_SECRET_MASTER_KEY (exactly 32 bytes, hex or base64)
   ```

   The default `memory` backend always passes. Unknown backends raise.

2. **Request-time 503** (G3). Even with a valid config, if the backend is
   momentarily unavailable (DB down, row integrity failure, missing table), the
   affected credential check returns **HTTP 503**, not a token-dependent 401.
   The readiness probe runs *before* any token-format branch — including a
   request with no bearer — so no token-format information leaks during an
   outage.

Transient DB outages are intentionally **not** checked at startup: a brief
outage must not prevent the process from booting; it degrades to 503 at request
time instead.

## Migration

The `encrypted_db` backend adds exactly one Alembic migration that creates the
`agent_secret` table and its unique `token_tag` index (`20260730_0001`).
Downgrading it is **fail-closed**: if any row exists in `agent_secret`, the
downgrade raises rather than silently dropping credential data. An empty table
downgrades cleanly.

## Quick checklist before promoting to production

- [ ] `AIOS_SECRET_STORE_BACKEND=encrypted_db` set in the deployment env.
- [ ] `AIOS_SECRET_MASTER_KEY` is 32 bytes (hex/base64) and sourced from a
      secret manager, not the code or image.
- [ ] App boot log shows no `secret store misconfigured` error (startup
      validation passed).
- [ ] `agent_secret` table exists after migrations (`alembic history` shows a
      single head `20260730_0001`).
- [ ] A deliberate KEK removal at boot reproduces the startup failure above
      (confirm fail-closed).
