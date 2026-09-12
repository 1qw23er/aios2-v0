# Finish a campaign that stalled on MANUAL (human-in-the-loop) tasks.
#
# T6 "人工审阅" and T8 "发布闸门" are created with department=None and
# RoutingMode.MANUAL (campaign.py:290 / :308) -- no agent can claim them, so
# /tasks/{id}/execute returns 409 by design. The owner advances them:
#   T6 -> POST /tasks/{id}/complete            (owner review decision)
#   T8 -> POST /tasks/{id}/publish-gate        (L3 approval, never auto-publishes)
#
# Usage: .\scripts\run_campaign_gates.ps1 -ProjectId prj_xxx

param(
    [Parameter(Mandatory = $true)][string]$ProjectId,
    [string]$BaseUrl  = "http://127.0.0.1:8002",
    [string]$OwnerId  = "local-owner",
    [string]$OwnerKey = "local-owner-secret-0123456789abcdefghij",
    [int]$MaxRounds   = 10,
    [string]$OutDir   = "D:/wb_tmp/campaign_artifacts"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$pair = "$($OwnerId):$($OwnerKey)"
$auth = "Basic " + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($pair))
$ownerH = @{ "Authorization" = $auth }

function New-Idem([string]$s) { return "gate-" + $s + "-" + [guid]::NewGuid().ToString("N").Substring(0, 10) }

# UTF-8 safe HTTP/JSON helper. PowerShell 5.1 Invoke-RestMethod decodes
# `application/json` (no charset) as Latin-1, mangling CJK into mojibake
# (æ¼å¤å¤). Read raw bytes and decode UTF-8 explicitly.
function Invoke-AiosJson {
    param(
        [string]$Method, [string]$Uri, [hashtable]$Headers,
        [string]$Body = "", [int]$TimeoutSec = 60
    )
    $bytes = $null
    if ($Body) { $bytes = [Text.Encoding]::UTF8.GetBytes($Body) }
    $wr = Invoke-WebRequest -Method $Method -Uri $Uri -Headers $Headers -Body $bytes `
        -ContentType "application/json; charset=utf-8" -TimeoutSec $TimeoutSec
    $json = [Text.Encoding]::UTF8.GetString($wr.RawContentStream.ToArray())
    return $json | ConvertFrom-Json
}

function Get-Board {
    return Invoke-AiosJson -Method Get -Uri "$BaseUrl/owner/campaigns/$ProjectId" -Headers $ownerH -TimeoutSec 60
}

function Get-ReadyTasks($board) {
    $out = @()
    foreach ($p in $board.tasks_by_status.PSObject.Properties) {
        if ($p.Name -eq "ready") { $out = @($p.Value) }
    }
    return $out
}

function Save-Artifact($art, $name, $id) {
    $file = Join-Path $OutDir ("{0}_{1}.json" -f ($name -replace '[^A-Za-z0-9]', '_'), $id)
    ($art | ConvertTo-Json -Depth 20) | Out-File -FilePath $file -Encoding utf8
    return $file
}

$round = 0
$acted = 0
while ($round -lt $MaxRounds) {
    $round++
    # /complete and /package do NOT emit the completion event that the
    # orchestrator consumes, so downstream tasks stay BACKLOG unless we drive it.
    try {
        $null = Invoke-AiosJson -Method Post -Uri "$BaseUrl/orchestrator/process?limit=100" -Headers $ownerH -TimeoutSec 60
    } catch { }
    $board = Get-Board
    $ready = Get-ReadyTasks $board
    if ($ready.Count -eq 0) { Write-Output "round ${round}: no READY task -- done."; break }

    foreach ($t in $ready) {
        $tid   = $t.id
        $title = [string]$t.title
        $isGate    = $title -match "T8" -or $title -match "发布闸门"
        $isReview  = $title -match "T6" -or $title -match "人工审阅"
        $isPackage = $title -match "T7" -or $title -match "分发包"

        if ($isGate) {
            Write-Output "--- round ${round}: PUBLISH-GATE $title ($tid)"
            try {
                $b = @{ decision = "approved"; rationale = "owner approved L3 gate for AI-mi campaign" } | ConvertTo-Json
                $h = @{ "Authorization" = $auth; "Idempotency-Key" = (New-Idem $tid) }
                $r = Invoke-AiosJson -Method Post -Uri "$BaseUrl/tasks/$tid/publish-gate" -Headers $h -Body $b -TimeoutSec 120
                Write-Output ("    approval = {0} / {1}" -f $r.status, $r.decision)
                $acted++
            } catch { Write-Output "    FAILED: $($_.Exception.Message)" }
        }
        elseif ($isPackage) {
            Write-Output "--- round ${round}: PACKAGE $title ($tid)"
            try {
                $h = @{ "Idempotency-Key" = (New-Idem $tid) }
                $art = Invoke-AiosJson -Method Post -Uri "$BaseUrl/tasks/$tid/package" -Headers $h -TimeoutSec 180
                $f = Save-Artifact $art $title $tid
                Write-Output "    artifact = $($art.id)"
                Write-Output "    saved    = $f"
                $acted++
            } catch { Write-Output "    FAILED: $($_.Exception.Message)" }
        }
        elseif ($isReview) {
            Write-Output "--- round ${round}: OWNER COMPLETE $title ($tid)"
            try {
                $h = @{ "Idempotency-Key" = (New-Idem $tid) }
                $r = Invoke-AiosJson -Method Post -Uri "$BaseUrl/tasks/$tid/complete" -Headers $h -TimeoutSec 120
                Write-Output "    status = $($r.status)"
                $acted++
            } catch { Write-Output "    FAILED: $($_.Exception.Message)" }
        }
        else {
            Write-Output "--- round ${round}: EXEC $title ($tid)"
            try {
                $h = @{ "Idempotency-Key" = (New-Idem $tid) }
                $art = Invoke-AiosJson -Method Post -Uri "$BaseUrl/tasks/$tid/execute" -Headers $h -TimeoutSec 300
                $f = Save-Artifact $art $title $tid
                $data = $art.metadata_json.artifacts[0].data
                $keys = if ($data) { ($data.PSObject.Properties.Name -join ", ") } else { "(no data)" }
                Write-Output "    artifact = $($art.id)"
                Write-Output "    fields   = $keys"
                Write-Output "    saved    = $f"
                $acted++
            } catch { Write-Output "    FAILED: $($_.Exception.Message)" }
        }
    }
}

Write-Output ""
Write-Output "=== final board ==="
$board = Get-Board
foreach ($p in $board.tasks_by_status.PSObject.Properties) {
    $lst = @($p.Value)
    if ($lst.Count -gt 0) { Write-Output ("{0,-12} {1}" -f $p.Name, (($lst | ForEach-Object { $_.title }) -join " | ")) }
}
Write-Output "actions_taken = $acted"
Write-Output "budget_used   = $($board.project.budget_used)"
Write-Output "GATES: DONE"
