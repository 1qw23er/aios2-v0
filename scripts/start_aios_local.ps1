# AIOS V0 — local startup (default backend: ali-server smart_router @ 8768)
# Usage (PowerShell):  .\scripts\start_aios_local.ps1
#
# AIOS does NOT read .env -- config must be exported in this shell
# (see src/aios/api/security.py).
#
# Secrets live OUTSIDE the repo: C:\Users\Administrator\.aios_local_env.ps1
# This script dot-sources it when present. The repo only ever holds placeholders,
# so nothing sensitive can be committed.
#
# NOTE (PowerShell 5.1): any .ps1 containing non-ASCII must be saved as
# UTF-8 WITH BOM, otherwise it is parsed as ANSI and fails.

$ErrorActionPreference = "Stop"

$externalEnv = "C:\Users\Administrator\.aios_local_env.ps1"
if (Test-Path $externalEnv) {
    . $externalEnv
    Write-Host "Loaded secrets from $externalEnv"
} else {
    Write-Host "WARNING: $externalEnv not found -- using placeholders." -ForegroundColor Yellow
    Write-Host "         Fill in the real values or the app will fail closed." -ForegroundColor Yellow
}

# --- Owner inbound auth (defaults; overridden by the external env file) ---
if (-not $env:AIOS_OWNER_ID)      { $env:AIOS_OWNER_ID      = "local-owner" }
if (-not $env:AIOS_OWNER_API_KEY) { $env:AIOS_OWNER_API_KEY = "REPLACE_WITH_32CHAR_RANDOM_OWNER_SECRET_0000" }

# --- Execution adapter: OpenAI-compatible chat endpoint (LLMExecutionAdapter) ---
# Default backend is the ali-server smart_router.
#   loopback on the server = keyless; external callers need the bearer key.
# To use DeepSeek directly instead, set:
#   AIOS_AGENT_BASE_URL=https://api.deepseek.com/v1
#   AIOS_AGENT_MODEL=deepseek-chat
#   AIOS_AGENT_API_KEY=sk-...
if (-not $env:AIOS_AGENT_BASE_URL) { $env:AIOS_AGENT_BASE_URL = "http://47.90.161.151:8768/v1" }
if (-not $env:AIOS_AGENT_MODEL)    { $env:AIOS_AGENT_MODEL    = "deepseek-v4-flash" }
if (-not $env:AIOS_AGENT_API_KEY)  { $env:AIOS_AGENT_API_KEY  = "REPLACE_WITH_YOUR_SMART_ROUTER_OR_DEEPSEEK_KEY" }

# Optional retry/backoff (defaults are built in; bad values are clamped).
# $env:AIOS_AGENT_MAX_RETRIES = "3"
# $env:AIOS_AGENT_BACKOFF     = "2.0"

# --- Database (SQLite). Kept separate so demos never pollute the main DB. ---
if (-not $env:AIOS_DATABASE_URL) { $env:AIOS_DATABASE_URL = "sqlite:///./data/aios_local_demo.db" }

Write-Host "AIOS env (secrets masked):"
Write-Host "  AIOS_OWNER_ID       = $env:AIOS_OWNER_ID"
Write-Host "  AIOS_AGENT_BASE_URL = $env:AIOS_AGENT_BASE_URL"
Write-Host "  AIOS_AGENT_MODEL    = $env:AIOS_AGENT_MODEL"
Write-Host "  AIOS_AGENT_API_KEY  = (len=$($env:AIOS_AGENT_API_KEY.Length))"
Write-Host "  AIOS_DATABASE_URL   = $env:AIOS_DATABASE_URL"

# Start (Alembic migrates to head automatically on startup).
# Health:  GET http://127.0.0.1:8000/health  -> {"status":"ok"}
# Swagger: http://127.0.0.1:8000/docs
& ".\.venv\Scripts\python.exe" -m uvicorn aios.api.app:app --host 127.0.0.1 --port 8000
