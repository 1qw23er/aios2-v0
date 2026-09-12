#Requires -Version 5.1
<#
.SYNOPSIS
    AIOS HTTP 端到端冒烟：health -> 启动 campaign -> 执行首个 READY 任务 -> 看板。

.DESCRIPTION
    已实测通过的链路（2026-09-11，DeepSeek deepseek-chat）：
      1. GET  /health                        -> 200 {"status":"ok"}
      2. POST /owner/campaigns               -> 201，建 T1-T9 图并自动把无依赖根任务置 READY
      3. POST /tasks/{id}/execute            -> 200，产出 Artifact（真实调用 LLM）
      4. GET  /owner/campaigns/{id}          -> T1 done、T2 自动 ready（依赖激活）
    关键事实：
      - 根任务不会自动 READY，**但** campaign 启动会替你置 READY；下游任务在上游 done 后自动激活。
      - `budget_used` 恒为 0：LOCAL 执行"可见不控"，未配 AIOS_MODEL_PRICING 价目表时不入预算。
      - owner 接口走 HTTP Basic：用户名 = $AIOS_OWNER_ID，密码 = $AIOS_OWNER_API_KEY。

.PARAMETER OwnerId
    AIOS_OWNER_ID，默认 local-owner。
.PARAMETER OwnerKey
    AIOS_OWNER_API_KEY（>=32 字符）。生产请换成随机串，不要复用这里的值。
.PARAMETER BaseUrl
    服务基址，默认 http://127.0.0.1:8000。
.PARAMETER Goal
    campaign 标题（业务目标）。
.PARAMETER Objective
    campaign 目标描述。

.EXAMPLE
    # 另开一个终端，用 start_aios_local.ps1 起服务后：
    .\scripts\http_smoke.ps1 -OwnerKey "你的32位以上随机串"
#>
param(
    [string]$OwnerId   = "local-owner",
    [string]$OwnerKey  = "REPLACE_WITH_32CHAR_RANDOM_OWNER_SECRET_0000",
    [string]$BaseUrl   = "http://127.0.0.1:8000",
    [string]$Goal      = "AIOS HTTP 冒烟：验证真实执行链路",
    [string]$Objective = "验证 AIOS 能通过 HTTP 接口真实调度部门 agent 并产出 Artifact（冒烟，非真实投放）。"
)

# UTF-8 safe HTTP/JSON helper. PowerShell 5.1 Invoke-RestMethod decodes
# `application/json` (no charset) as Latin-1, mangling CJK into mojibake
# (æ¼å¤å¤). Read raw bytes and decode UTF-8 explicitly.
function Invoke-AiosJson {
    param(
        [string]$Method, [string]$Uri,
        [hashtable]$Headers = @{},
        [string]$Body = "", [int]$TimeoutSec = 60
    )
    $bytes = $null
    if ($Body) { $bytes = [Text.Encoding]::UTF8.GetBytes($Body) }
    $wr = Invoke-WebRequest -Method $Method -Uri $Uri -Headers $Headers -Body $bytes `
        -ContentType "application/json; charset=utf-8" -TimeoutSec $TimeoutSec
    $json = [Text.Encoding]::UTF8.GetString($wr.RawContentStream.ToArray())
    return $json | ConvertFrom-Json
}

$ErrorActionPreference = "Stop"

# ---- owner 认证：HTTP Basic（AIOS_OWNER_ID : AIOS_OWNER_API_KEY）----
$pair   = "$($OwnerId):$($OwnerKey)"
$bytes  = [System.Text.Encoding]::UTF8.GetBytes($pair)
$auth   = "Basic " + [Convert]::ToBase64String($bytes)
$ownerH = @{ Authorization = $auth }

function Step([string]$msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# ---------- 1) health ----------
Step "1/4 GET /health"
try {
    $h = Invoke-AiosJson -Method Get -Uri "$BaseUrl/health" -TimeoutSec 15
    Write-Host "health = $($h.status)"
    if ($h.status -ne "ok") { throw "health 不是 ok" }
} catch {
    Write-Host "连不上 $BaseUrl —— 请先在另一个终端运行 scripts\start_aios_local.ps1 起服务。" -ForegroundColor Red
    throw
}

# ---------- 2) 启动 campaign ----------
Step "2/4 POST /owner/campaigns"
$idem  = "http-smoke-" + [guid]::NewGuid().ToString("N").Substring(0, 10)
$body  = @{ name = $Goal; objective = $Objective } | ConvertTo-Json -Depth 4
$campH = @{ Authorization = $auth; "Idempotency-Key" = $idem }
$camp  = Invoke-AiosJson -Method Post -Uri "$BaseUrl/owner/campaigns" `
            -Headers $campH -Body $body -TimeoutSec 60
Write-Host "project = $($camp.project_id)  task_count = $($camp.task_count)"

$ready = $camp.tasks | Where-Object { $_.status -eq "ready" } | Select-Object -First 1
if (-not $ready) { throw "campaign 没有产出 READY 任务，请检查服务日志。" }
Write-Host "首个 READY 任务：$($ready.key) $($ready.title) -> $($ready.task_id)"

# ---------- 3) 真实执行 ----------
Step "3/4 POST /tasks/$($ready.task_id)/execute （真实调用 LLM，可能十几秒）"
$execIdem = "http-exec-" + [guid]::NewGuid().ToString("N").Substring(0, 10)
$art = Invoke-AiosJson -Method Post -Uri "$BaseUrl/tasks/$($ready.task_id)/execute" `
         -Headers @{ "Idempotency-Key" = $execIdem } `
         -Body "{}" -TimeoutSec 240
Write-Host "Artifact = $($art.id)  source = $($art.source)"
$payload = $art.metadata_json.artifacts[0].data
if ($payload) {
    Write-Host "产出字段 = " + (($payload.PSObject.Properties.Name) -join ", ")
}

# ---------- 4) 看板 ----------
Step "4/4 GET /owner/campaigns/$($camp.project_id)"
$board = Invoke-AiosJson -Method Get -Uri "$BaseUrl/owner/campaigns/$($camp.project_id)" -Headers $ownerH -TimeoutSec 30
foreach ($st in $board.tasks_by_status.PSObject.Properties.Name) {
    $list = $board.tasks_by_status.$st
    if ($list -and $list.Count -gt 0) {
        Write-Host ("{0,-16} {1}" -f $st, (($list | ForEach-Object { $_.title }) -join " | "))
    }
}
Write-Host "budget_used = $($board.project.budget_used)  （LOCAL 未配价目表时恒为 0，符合 GAP-3 边界）"
Write-Host "`nSMOKE: PASS" -ForegroundColor Green
