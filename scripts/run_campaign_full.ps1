# Run a FULL AIOS campaign (T1..T9) against a live HTTP service.
#
# Usage: .\scripts\run_campaign_full.ps1 -OwnerKey "<your owner api key>"
#
# NOTE (PowerShell 5.1): files with non-ASCII must be UTF-8 WITH BOM.

param(
    [string]$BaseUrl   = "http://127.0.0.1:8002",
    [string]$OwnerId   = "local-owner",
    [string]$OwnerKey  = "local-owner-secret-0123456789abcdefghij",
    [string]$Goal      = "AI觅 电商卖家内容获客（真实业务目标）",
    [string]$Objective = "为 AI觅（aimi.quantv.com，米核AI官方贴牌代理）策划并执行一轮内容获客。" +
                         "产品定位：面向电商卖家的 AI 内容创作工具，用 AI 批量产出商品主图、详情页文案与短视频脚本。" +
                         "目标受众：拼多多/淘宝/抖音的中小电商卖家（无专职设计、文案产出慢、不懂 AI 工具、外包设计贵）。" +
                         "核心痛点：主图与详情页外包成本高、上新节奏跟不上、不会写转化文案、短视频不会拍不会剪。" +
                         "渠道：微信公众号（黎叔AI创业实验室）+ 小红书 + 短视频。" +
                         "转化目标：引导读者注册成为 AI觅 合伙人（零门槛推广分佣）。" +
                         "要求：所有内容必须有真人感、去 AI 味、给出具体可操作细节，不要空泛吹捧；涉及价格与收益须保守表述、不承诺具体收入。",
    [int]$MaxRounds    = 12,
    [string]$OutDir    = "D:/wb_tmp/campaign_artifacts"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function New-BasicHeader([string]$id, [string]$key) {
    $pair  = "$($id):$($key)"
    $b64   = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($pair))
    return @{ "Authorization" = "Basic $b64" }
}

function New-Idem([string]$suffix) {
    return "run-" + $suffix + "-" + [guid]::NewGuid().ToString("N").Substring(0, 10)
}

# UTF-8 safe HTTP/JSON helper.
# PowerShell 5.1 Invoke-RestMethod decodes `application/json` (no charset) as
# Latin-1, mangling every CJK char into mojibake (æ¼å¤å¤). We read the raw
# response bytes from Invoke-WebRequest and decode as UTF-8 explicitly, so the
# .NET string is correct before ConvertFrom-Json / Write-Output.
function Invoke-AiosJson {
    param(
        [string]$Method,
        [string]$Uri,
        [hashtable]$Headers,
        [string]$Body = "",
        [int]$TimeoutSec = 60
    )
    $bytes = $null
    if ($Body) { $bytes = [Text.Encoding]::UTF8.GetBytes($Body) }
    $wr = Invoke-WebRequest -Method $Method -Uri $Uri -Headers $Headers -Body $bytes `
        -ContentType "application/json; charset=utf-8" -TimeoutSec $TimeoutSec
    $json = [Text.Encoding]::UTF8.GetString($wr.RawContentStream.ToArray())
    return $json | ConvertFrom-Json
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$ownerH = New-BasicHeader $OwnerId $OwnerKey

# ---------- 1. launch campaign ----------
Write-Output "=== 1/3 POST /owner/campaigns ==="
$body = @{ name = $Goal; objective = $Objective } | ConvertTo-Json -Depth 6
# Invoke-AiosJson sends/reads UTF-8 explicitly, so CJK chars survive both ways.
$campH = @{ "Authorization" = $ownerH["Authorization"]; "Idempotency-Key" = (New-Idem "campaign") }
$camp  = Invoke-AiosJson -Method Post -Uri "$BaseUrl/owner/campaigns" `
             -Headers $campH -Body $body -TimeoutSec 120

# project id lives at different depths depending on the payload shape
$projectId = $null
foreach ($path in @(
    { $camp.project.id }, { $camp.project_id }, { $camp.id }
)) {
    try { $v = & $path; if ($v) { $projectId = $v; break } } catch { }
}
if (-not $projectId) { throw "cannot resolve project id from campaign response" }
Write-Output "project = $projectId"

# ---------- 2. drain the DAG ----------
Write-Output ""
Write-Output "=== 2/3 executing tasks until the DAG drains ==="
$round = 0
$done  = 0
while ($round -lt $MaxRounds) {
    $round++
    $board = Invoke-AiosJson -Method Get -Uri "$BaseUrl/owner/campaigns/$projectId" -Headers $ownerH -TimeoutSec 60
    $ready = @()
    foreach ($p in $board.tasks_by_status.PSObject.Properties) {
        if ($p.Name -eq "ready") { $ready = @($p.Value) }
    }
    if ($ready.Count -eq 0) {
        Write-Output "round ${round}: no READY task left -- DAG drained."
        break
    }
    foreach ($t in $ready) {
        $tid   = $t.id
        $title = $t.title
        Write-Output "--- round ${round}: EXEC $title ($tid)"
        try {
            $art = Invoke-AiosJson -Method Post -Uri "$BaseUrl/tasks/$tid/execute" `
                       -Headers @{ "Idempotency-Key" = (New-Idem $tid) } `
                       -TimeoutSec 300
            $file = Join-Path $OutDir ("{0}_{1}.json" -f ($title -replace '[^A-Za-z0-9]', '_'), $tid)
            ($art | ConvertTo-Json -Depth 20) | Out-File -FilePath $file -Encoding utf8
            $data = $art.metadata_json.artifacts[0].data
            $keys = if ($data) { ($data.PSObject.Properties.Name -join ", ") } else { "(none)" }
            Write-Output "    artifact = $($art.id)  saved -> $file"
            Write-Output "    fields   = $keys"
            $done++
        } catch {
            Write-Output "    FAILED: $($_.Exception.Message)"
        }
    }
}

# ---------- 3. final board ----------
Write-Output ""
Write-Output "=== 3/3 final board ==="
$board = Invoke-AiosJson -Method Get -Uri "$BaseUrl/owner/campaigns/$projectId" -Headers $ownerH -TimeoutSec 60
foreach ($p in $board.tasks_by_status.PSObject.Properties) {
    $lst = @($p.Value)
    if ($lst.Count -gt 0) {
        $names = ($lst | ForEach-Object { $_.title }) -join " | "
        Write-Output ("{0,-12} {1}" -f $p.Name, $names)
    }
}
Write-Output "executed_in_this_run = $done"
Write-Output "budget_used          = $($board.project.budget_used)"
Write-Output ""
Write-Output "ARTIFACTS_DIR = $OutDir"
Write-Output "CAMPAIGN: DONE"
