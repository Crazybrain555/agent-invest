[CmdletBinding()]
param(
    [string]$WatchdogSource = "C:\ProgramData\agent-invest\staging\nvidia_gpu_exporter_watchdog.ps1",
    [string]$ExpectedPriorSha256 = "e14f0f1740339e07200da95af92ecc69371bd13b08206e1304994101c20367ba",
    [string]$ExpectedNewSha256 = "0fc87534a83c29996d964d8b875cf71cacfa1a5af1036686259e7a5544976ba8",
    [string]$InstallReceipt = "C:\ProgramData\agent-invest\gpu-metrics\install-receipt.json",
    [string]$PriorAmendment = "C:\ProgramData\agent-invest\gpu-metrics\watchdog-amendment.json",
    [string]$ReceiptTarget = "C:\ProgramData\agent-invest\gpu-metrics\watchdog-amendment-v2.json",
    [string]$TaskPath = "\agent-invest\",
    [string]$TaskName = "GPU Metrics",
    [string]$WatchdogTaskName = "GPU Metrics Watchdog"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$WatchdogTarget = "C:\ProgramData\agent-invest\gpu-metrics\nvidia_gpu_exporter_watchdog.ps1"
$ExpectedExecutableSha256 = "cb5e441c3c7a1c1072f3462a69f00c3e2d7d717e66be75171494fe7c670551f7"
$BackupTarget = "$WatchdogTarget.pre-v2-$((Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')).bak"
$CandidateTarget = "$WatchdogTarget.v2-candidate"
$FailedTarget = "$WatchdogTarget.failed-v2"
$Replaced = $false

function Get-LowerSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Write-AtomicUtf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $directory = Split-Path -Parent $Path
    $temporary = Join-Path $directory ".$(Split-Path -Leaf $Path).$([Guid]::NewGuid().ToString('N')).tmp"
    $encoding = New-Object System.Text.UTF8Encoding($false)
    try {
        $stream = New-Object IO.FileStream(
            $temporary,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        try {
            $bytes = $encoding.GetBytes($Value)
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        }
        finally { $stream.Dispose() }
        [IO.File]::Move($temporary, $Path)
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Assert-ExporterTask {
    param(
        [Parameter(Mandatory = $true)]$Install,
        [Parameter(Mandatory = $true)]$Task
    )
    $principalSid = ([Security.Principal.NTAccount]$Task.Principal.UserId).Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
    $expectedArguments = @($Install.argv) -join " "
    $expectedWorkingDirectory = Split-Path -Parent $Install.executable_path
    if (
        $principalSid -ne $Install.service_identity.sid -or
        @($Task.Actions).Count -ne 1 -or
        $Task.Actions[0].Execute -ne $Install.executable_path -or
        $Task.Actions[0].Arguments -ne $expectedArguments -or
        $Task.Actions[0].WorkingDirectory -ne $expectedWorkingDirectory -or
        (Get-LowerSha256 -Path $Task.Actions[0].Execute) -ne $ExpectedExecutableSha256
    ) { throw "GPU metrics target task drifted" }
}

function Assert-ExporterListener {
    param([Parameter(Mandatory = $true)]$Install)
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort 9835 `
        -ErrorAction SilentlyContinue)
    if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne "127.0.0.1") {
        throw "GPU metrics listener is not exactly one loopback listener"
    }
    $process = Get-Process -Id $listeners[0].OwningProcess
    $processCim = Get-CimInstance -ClassName Win32_Process `
        -Filter "ProcessId = $($process.Id)"
    $owner = Invoke-CimMethod -InputObject $processCim -MethodName GetOwnerSid
    if (
        $process.Path -ne $Install.executable_path -or
        (Get-LowerSha256 -Path $process.Path) -ne $ExpectedExecutableSha256 -or
        $owner.ReturnValue -ne 0 -or
        $owner.Sid -ne $Install.service_identity.sid
    ) { throw "GPU metrics listener identity drifted" }
    return [ordered]@{
        address = $listeners[0].LocalAddress
        port = 9835
        process_id = [int]$listeners[0].OwningProcess
        process_owner_sid = $owner.Sid
    }
}

$administrator = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $administrator.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )) {
    throw "GPU metrics watchdog upgrade requires an elevated administrator"
}
foreach ($path in @($WatchdogSource, $WatchdogTarget, $InstallReceipt, $PriorAmendment)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "GPU metrics watchdog upgrade input is missing: $path"
    }
}
if (Test-Path -LiteralPath $ReceiptTarget) {
    throw "GPU metrics watchdog v2 receipt already exists"
}
if ((Get-LowerSha256 -Path $WatchdogSource) -ne $ExpectedNewSha256) {
    throw "staged watchdog SHA-256 drifted"
}
if ((Get-LowerSha256 -Path $WatchdogTarget) -ne $ExpectedPriorSha256) {
    throw "installed watchdog is not the reviewed prior generation"
}
$install = Get-Content -LiteralPath $InstallReceipt -Raw | ConvertFrom-Json
$prior = Get-Content -LiteralPath $PriorAmendment -Raw | ConvertFrom-Json
if (
    $install.schema -ne "nvidia-gpu-exporter-install-receipt.v1" -or
    $prior.schema -ne "nvidia-gpu-exporter-watchdog-amendment.v1" -or
    $prior.watchdog_sha256 -ne "sha256:$ExpectedPriorSha256" -or
    $install.scheduled_task.path -ne $TaskPath -or
    $install.scheduled_task.name -ne $TaskName -or
    $install.listener.address -ne "127.0.0.1" -or
    $install.listener.port -ne 9835
) { throw "GPU metrics receipt chain drifted" }
$task = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
Assert-ExporterTask -Install $install -Task $task
$watchdogTask = Get-ScheduledTask -TaskPath $TaskPath -TaskName $WatchdogTaskName
$watchdogSid = ([Security.Principal.NTAccount]$watchdogTask.Principal.UserId).Translate(
    [Security.Principal.SecurityIdentifier]
).Value
if (
    $watchdogSid -ne "S-1-5-18" -or
    @($watchdogTask.Actions).Count -ne 1 -or
    -not $watchdogTask.Actions[0].Arguments.Contains($WatchdogTarget)
) { throw "GPU metrics watchdog scheduled task drifted" }
if (Test-Path -LiteralPath $CandidateTarget) { throw "watchdog candidate path is occupied" }
if (Test-Path -LiteralPath $BackupTarget) { throw "watchdog backup path is occupied" }

try {
    Copy-Item -LiteralPath $WatchdogSource -Destination $CandidateTarget
    if ((Get-LowerSha256 -Path $CandidateTarget) -ne $ExpectedNewSha256) {
        throw "watchdog candidate changed during copy"
    }
    [IO.File]::Replace($CandidateTarget, $WatchdogTarget, $BackupTarget, $true)
    $Replaced = $true
    if (
        (Get-LowerSha256 -Path $WatchdogTarget) -ne $ExpectedNewSha256 -or
        (Get-LowerSha256 -Path $BackupTarget) -ne $ExpectedPriorSha256
    ) { throw "watchdog atomic replacement could not be proved" }

    $started = (Get-Date).ToUniversalTime()
    & schtasks.exe /Run /TN "$TaskPath$WatchdogTaskName" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "cannot start upgraded watchdog" }
    $watchdogInfo = $null
    for ($attempt = 1; $attempt -le 30; $attempt += 1) {
        $watchdogInfo = Get-ScheduledTaskInfo -TaskPath $TaskPath `
            -TaskName $WatchdogTaskName
        if (
            $watchdogInfo.LastRunTime.ToUniversalTime() -ge $started -and
            $watchdogInfo.LastTaskResult -eq 0
        ) { break }
        Start-Sleep -Seconds 1
    }
    if (
        $null -eq $watchdogInfo -or
        $watchdogInfo.LastRunTime.ToUniversalTime() -lt $started -or
        $watchdogInfo.LastTaskResult -ne 0
    ) { throw "upgraded watchdog did not complete successfully" }
    $verifiedTask = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
    Assert-ExporterTask -Install $install -Task $verifiedTask
    $listener = Assert-ExporterListener -Install $install
    $metrics = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 `
        -MaximumRedirection 0 -Uri "http://127.0.0.1:9835/metrics"
    if (
        $metrics.StatusCode -ne 200 -or
        -not $metrics.Content.Contains("nvidia_smi_last_collect_success 1")
    ) { throw "upgraded watchdog metrics verification failed" }

    $receipt = [ordered]@{
        schema = "nvidia-gpu-exporter-watchdog-amendment.v2"
        upgraded_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        upgrader_sha256 = "sha256:$(Get-LowerSha256 -Path $PSCommandPath)"
        install_receipt_sha256 = "sha256:$(Get-LowerSha256 -Path $InstallReceipt)"
        prior_amendment_sha256 = "sha256:$(Get-LowerSha256 -Path $PriorAmendment)"
        prior_watchdog_sha256 = "sha256:$ExpectedPriorSha256"
        watchdog_path = $WatchdogTarget
        watchdog_sha256 = "sha256:$ExpectedNewSha256"
        backup_path = $BackupTarget
        target_task_path = $TaskPath
        target_task_name = $TaskName
        watchdog_task_name = $WatchdogTaskName
        watchdog_principal_sid = $watchdogSid
        listener = $listener
        recovered = $true
    }
    Write-AtomicUtf8NoBom -Path $ReceiptTarget -Value (
        ($receipt | ConvertTo-Json -Depth 20) + "`n"
    )
    $receipt | ConvertTo-Json -Depth 20
}
catch {
    $originalError = $_.Exception.Message
    if ($Replaced) {
        if (Test-Path -LiteralPath $FailedTarget) {
            Remove-Item -LiteralPath $FailedTarget -Force
        }
        [IO.File]::Replace($BackupTarget, $WatchdogTarget, $FailedTarget, $true)
        if ((Get-LowerSha256 -Path $WatchdogTarget) -ne $ExpectedPriorSha256) {
            throw "watchdog upgrade failed and prior generation could not be restored: $originalError"
        }
    }
    throw "watchdog upgrade failed and rollback was verified: $originalError"
}
finally {
    if (Test-Path -LiteralPath $CandidateTarget) {
        Remove-Item -LiteralPath $CandidateTarget -Force
    }
}
