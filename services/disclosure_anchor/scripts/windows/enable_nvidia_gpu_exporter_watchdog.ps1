[CmdletBinding()]
param(
    [string]$WatchdogSource = "C:\ProgramData\agent-invest\staging\nvidia_gpu_exporter_watchdog.ps1",
    [string]$ExpectedWatchdogSha256 = "0fc87534a83c29996d964d8b875cf71cacfa1a5af1036686259e7a5544976ba8",
    [string]$TaskPath = "\agent-invest\",
    [string]$TaskName = "GPU Metrics",
    [string]$WatchdogTaskName = "GPU Metrics Watchdog",
    [string]$InstallReceipt = "C:\ProgramData\agent-invest\gpu-metrics\install-receipt.json",
    [string]$ReceiptTarget = "C:\ProgramData\agent-invest\gpu-metrics\watchdog-amendment.json"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$WatchdogTarget = "C:\ProgramData\agent-invest\gpu-metrics\nvidia_gpu_exporter_watchdog.ps1"
$TaskCreated = $false
$FileCreated = $false

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $Value, $encoding)
}

function Get-LowerSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

foreach ($path in @($WatchdogSource, $InstallReceipt)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "GPU metrics watchdog input is missing: $path"
    }
}
if ((Get-LowerSha256 -Path $WatchdogSource) -ne $ExpectedWatchdogSha256) {
    throw "GPU metrics watchdog source SHA-256 drifted"
}
if (Test-Path -LiteralPath $WatchdogTarget) {
    throw "GPU metrics watchdog target already exists"
}
if (Test-Path -LiteralPath $ReceiptTarget) {
    throw "GPU metrics watchdog receipt already exists"
}
if (Get-ScheduledTask -TaskPath $TaskPath -TaskName $WatchdogTaskName `
        -ErrorAction SilentlyContinue) {
    throw "GPU metrics watchdog task already exists"
}
$install = Get-Content -LiteralPath $InstallReceipt -Raw | ConvertFrom-Json
$task = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
$principalSid = ([Security.Principal.NTAccount]$task.Principal.UserId).Translate(
    [Security.Principal.SecurityIdentifier]
).Value
if (
    $install.schema -ne "nvidia-gpu-exporter-install-receipt.v1" -or
    $principalSid -ne $install.service_identity.sid -or
    @($task.Actions).Count -ne 1 -or
    $task.Actions[0].Execute -ne $install.executable_path
) {
    throw "GPU metrics install receipt or task drifted before watchdog install"
}

try {
    Copy-Item -LiteralPath $WatchdogSource -Destination $WatchdogTarget
    $FileCreated = $true
    if ((Get-LowerSha256 -Path $WatchdogTarget) -ne $ExpectedWatchdogSha256) {
        throw "installed GPU metrics watchdog SHA-256 drifted"
    }
    $watchdogCommand = (
        "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass " +
        "-File $WatchdogTarget"
    )
    & schtasks.exe /Create /TN "$TaskPath$WatchdogTaskName" /SC MINUTE /MO 1 `
        /RU SYSTEM /RL HIGHEST /TR $watchdogCommand /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "cannot create GPU metrics watchdog task" }
    $TaskCreated = $true
    & schtasks.exe /Run /TN "$TaskPath$WatchdogTaskName" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "cannot start GPU metrics watchdog task" }
    for ($attempt = 1; $attempt -le 30; $attempt += 1) {
        try {
            $metrics = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 `
                -Uri "http://127.0.0.1:9835/metrics"
            if (
                $metrics.StatusCode -eq 200 -and
                $metrics.Content.Contains("nvidia_smi_last_collect_success 1")
            ) { break }
        }
        catch {
            # The watchdog may still be resetting the password-bound task.
        }
        Start-Sleep -Seconds 1
    }
    if (
        $null -eq $metrics -or
        $metrics.StatusCode -ne 200 -or
        -not $metrics.Content.Contains("nvidia_smi_last_collect_success 1")
    ) {
        throw "GPU metrics did not recover through the watchdog"
    }
    $watchdogTask = Get-ScheduledTask -TaskPath $TaskPath `
        -TaskName $WatchdogTaskName
    $watchdogPrincipalSid = (
        [Security.Principal.NTAccount]$watchdogTask.Principal.UserId
    ).Translate([Security.Principal.SecurityIdentifier]).Value
    if (
        $watchdogPrincipalSid -ne "S-1-5-18" -or
        @($watchdogTask.Actions).Count -ne 1 -or
        -not $watchdogTask.Actions[0].Arguments.Contains($WatchdogTarget)
    ) {
        throw "GPU metrics watchdog task principal or action drifted"
    }
    $receipt = [ordered]@{
        schema = "nvidia-gpu-exporter-watchdog-amendment.v1"
        installed_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        installer_sha256 = "sha256:$(Get-LowerSha256 -Path $PSCommandPath)"
        install_receipt_sha256 = "sha256:$(Get-LowerSha256 -Path $InstallReceipt)"
        watchdog_path = $WatchdogTarget
        watchdog_sha256 = "sha256:$ExpectedWatchdogSha256"
        task_path = $TaskPath
        task_name = $WatchdogTaskName
        interval = "PT1M"
        principal_sid = $watchdogPrincipalSid
        target_task_path = $TaskPath
        target_task_name = $TaskName
        target_service_sid = $install.service_identity.sid
        recovered = $true
    }
    Write-Utf8NoBom -Path $ReceiptTarget -Value (
        ($receipt | ConvertTo-Json -Depth 20) + "`n"
    )
    $receipt | ConvertTo-Json -Depth 20
}
catch {
    $originalError = $_.Exception.Message
    $rollbackErrors = New-Object Collections.Generic.List[string]
    if ($TaskCreated) {
        & schtasks.exe /Delete /TN "$TaskPath$WatchdogTaskName" /F | Out-Null
        if ($LASTEXITCODE -ne 0) {
            $rollbackErrors.Add("watchdog task remains")
        }
    }
    if ($FileCreated -and (Test-Path -LiteralPath $WatchdogTarget)) {
        try { Remove-Item -LiteralPath $WatchdogTarget -Force }
        catch { $rollbackErrors.Add("watchdog file remains") }
    }
    if ($rollbackErrors.Count -eq 0) {
        throw "GPU metrics watchdog install failed and rollback was verified: $originalError"
    }
    throw (
        "GPU metrics watchdog install failed with partial rollback: " +
        "$originalError; $($rollbackErrors -join '; ')"
    )
}
