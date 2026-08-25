[CmdletBinding()]
param(
    [string]$TaskPath = "\agent-invest\",
    [string]$TaskName = "GPU Metrics",
    [string]$InstallReceipt = "C:\ProgramData\agent-invest\gpu-metrics\install-receipt.json"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ExpectedExecutableSha256 = "cb5e441c3c7a1c1072f3462a69f00c3e2d7d717e66be75171494fe7c670551f7"

function Get-LowerSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Assert-ExporterTaskIdentity {
    param(
        [Parameter(Mandatory = $true)]$Task,
        [Parameter(Mandatory = $true)]$Install
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
    ) {
        throw "GPU metrics task identity, exact action or executable drifted"
    }
}

function Assert-ExporterListenerIdentity {
    param([Parameter(Mandatory = $true)]$Install)
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort 9835 `
        -ErrorAction SilentlyContinue)
    if (
        $listeners.Count -ne 1 -or
        $listeners[0].LocalAddress -ne "127.0.0.1"
    ) {
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
    ) {
        throw "GPU metrics listener process drifted"
    }
}

$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ($currentSid -ne "S-1-5-18") {
    throw "GPU metrics watchdog must run as LocalSystem"
}
if (-not (Test-Path -LiteralPath $InstallReceipt -PathType Leaf)) {
    throw "GPU metrics install receipt is missing"
}
$install = Get-Content -LiteralPath $InstallReceipt -Raw | ConvertFrom-Json
if (
    $install.schema -ne "nvidia-gpu-exporter-install-receipt.v1" -or
    $install.release.executable_sha256 -ne "sha256:$ExpectedExecutableSha256" -or
    $install.scheduled_task.path -ne $TaskPath -or
    $install.scheduled_task.name -ne $TaskName -or
    $install.listener.address -ne "127.0.0.1" -or
    $install.listener.port -ne 9835 -or
    @($install.argv).Count -lt 1
) {
    throw "GPU metrics install receipt drifted"
}
$task = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
Assert-ExporterTaskIdentity -Task $task -Install $install

$listeners = @(Get-NetTCPConnection -State Listen -LocalPort 9835 `
    -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 1) {
    throw "GPU metrics port has multiple listeners"
}
if ($listeners.Count -eq 1) {
    Assert-ExporterListenerIdentity -Install $install
    exit 0
}

# A force-killed long-running action can remain logically Running for a while.
# Reset only this exact task, then let its password-bound principal start it.
try {
    Stop-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName `
        -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
    $recovered = $false
    for ($attempt = 1; $attempt -le 30; $attempt += 1) {
        try {
            $metrics = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 `
                -MaximumRedirection 0 -Uri "http://127.0.0.1:9835/metrics"
            if (
                $metrics.StatusCode -eq 200 -and
                $metrics.Content.Contains("nvidia_smi_last_collect_success 1")
            ) {
                $recovered = $true
                break
            }
        }
        catch {
            # The password-bound task may still be starting its first collection.
        }
        Start-Sleep -Seconds 1
    }
    if (-not $recovered) {
        throw "GPU metrics task did not recover within the watchdog deadline"
    }
    $recoveredTask = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
    Assert-ExporterTaskIdentity -Task $recoveredTask -Install $install
    Assert-ExporterListenerIdentity -Install $install
    exit 0
}
catch {
    Stop-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName `
        -ErrorAction SilentlyContinue
    throw
}
