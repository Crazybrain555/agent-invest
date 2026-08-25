[CmdletBinding()]
param(
    [string]$ReceiptTarget = "C:\ProgramData\agent-invest\gpu-metrics\windows-time-receipt.json"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ParametersKey = "HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\Parameters"
$PeerList = "time.windows.com,0x8 time.cloudflare.com,0x8 time.nist.gov,0x8"
$BackupRoot = "C:\ProgramData\agent-invest\gpu-metrics\windows-time-backup-" + `
    (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$MutationStarted = $false
$Committed = $false
$ReceiptTempPath = $null
$AllowedSources = @(
    "time.windows.com",
    "time.cloudflare.com",
    "time.nist.gov"
)

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

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Windows Time configuration requires an elevated administrator"
}
if (Test-Path -LiteralPath $ReceiptTarget) {
    throw "Windows Time receipt already exists; refusing ambiguous replay"
}
$beforeService = Get-Service -Name w32time
if ($beforeService.Status -ne "Running") {
    throw "Windows Time must already be running before configuration"
}
$before = Get-ItemProperty -LiteralPath $ParametersKey
$beforeNtpServer = [string]$before.NtpServer
$beforeType = [string]$before.Type
New-Item -ItemType Directory -Path $BackupRoot | Out-Null
$beforeConfigurationPath = Join-Path $BackupRoot "configuration-before.txt"
$beforeStatusPath = Join-Path $BackupRoot "status-before.txt"
$beforeRegistryPath = Join-Path $BackupRoot "w32time-before.reg"
$beforeConfigurationLines = @(& w32tm.exe /query /configuration 2>&1)
$beforeConfigurationExitCode = $LASTEXITCODE
if ($beforeConfigurationExitCode -ne 0) {
    throw "cannot query the prior Windows Time configuration"
}
Write-Utf8NoBom -Path $beforeConfigurationPath -Value (
    (($beforeConfigurationLines | Out-String).Trim()) + "`n"
)
$beforeStatusLines = @(& w32tm.exe /query /status 2>&1)
$beforeStatusExitCode = $LASTEXITCODE
if ($beforeStatusExitCode -ne 0) {
    throw "cannot query the prior Windows Time status"
}
Write-Utf8NoBom -Path $beforeStatusPath -Value (
    (($beforeStatusLines | Out-String).Trim()) + "`n"
)
& reg.exe export `
    "HKLM\SYSTEM\CurrentControlSet\Services\W32Time" `
    $beforeRegistryPath /y | Out-Null
if ($LASTEXITCODE -ne 0) { throw "cannot export the Windows Time registry" }

try {
    $MutationStarted = $true
    & w32tm.exe /config /manualpeerlist:"$PeerList" `
        /syncfromflags:manual /update | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "cannot configure Windows Time peers" }
    # Both /update and a service start may initiate an NTP exchange. Keep each
    # transition above the strictest configured provider's four-second floor.
    Start-Sleep -Seconds 5
    Restart-Service -Name w32time -Force
    Start-Sleep -Seconds 5
    $synchronized = $false
    $statusText = ""
    $sourceText = ""
    $successfulAttempt = $null
    for ($attempt = 1; $attempt -le 6; $attempt += 1) {
        & w32tm.exe /resync /rediscover | Out-Null
        $resyncExitCode = $LASTEXITCODE
        # NIST asks clients not to query the same server more often than every
        # four seconds. Keep retries above that floor even during recovery.
        Start-Sleep -Seconds 5
        $statusLines = @(& w32tm.exe /query /status 2>&1)
        $statusExitCode = $LASTEXITCODE
        $statusText = ($statusLines | Out-String).Trim()
        $sourceLines = @(& w32tm.exe /query /source 2>&1)
        $sourceExitCode = $LASTEXITCODE
        $sourceText = ($sourceLines | Out-String).Trim()
        $normalizedSource = ($sourceText.Split(",")[0]).Trim().ToLowerInvariant()
        $nonemptyStatusLines = @(
            $statusLines | Where-Object {
                -not [string]::IsNullOrWhiteSpace([string]$_)
            }
        )
        $leapStatusLine = if ($nonemptyStatusLines.Count -gt 0) {
            [string]$nonemptyStatusLines[0]
        }
        else { "" }
        if (
            $resyncExitCode -eq 0 -and
            $statusExitCode -eq 0 -and
            $sourceExitCode -eq 0 -and
            $leapStatusLine -match '^[^:]+:\s*0\s*(?:\([^\r\n]*\))?\s*$' -and
            $AllowedSources -contains $normalizedSource
        ) {
            $synchronized = $true
            $successfulAttempt = $attempt
            break
        }
    }
    if (-not $synchronized) {
        throw "Windows Time did not reach synchronized leap state"
    }
    $after = Get-ItemProperty -LiteralPath $ParametersKey
    if ([string]$after.NtpServer -ne $PeerList -or [string]$after.Type -ne "NTP") {
        throw "Windows Time registry does not match the reviewed peer policy"
    }
    $stripchart = [ordered]@{}
    foreach ($peer in $AllowedSources) {
        Start-Sleep -Seconds 5
        $output = (& w32tm.exe /stripchart /computer:$peer /samples:1 /dataonly 2>&1 |
            Out-String).Trim()
        $probeExitCode = $LASTEXITCODE
        $stripchart[$peer] = [ordered]@{
            success = ($probeExitCode -eq 0)
            exit_code = $probeExitCode
            output = $output
        }
    }
    $afterConfigurationLines = @(& w32tm.exe /query /configuration 2>&1)
    $afterConfigurationExitCode = $LASTEXITCODE
    if ($afterConfigurationExitCode -ne 0) {
        throw "cannot query the resulting Windows Time configuration"
    }
    $afterStatusLines = @(& w32tm.exe /query /status 2>&1)
    $afterStatusExitCode = $LASTEXITCODE
    if ($afterStatusExitCode -ne 0) {
        throw "cannot query the resulting Windows Time status"
    }
    $afterStatusText = ($afterStatusLines | Out-String).Trim()
    $afterNonemptyStatusLines = @(
        $afterStatusLines | Where-Object {
            -not [string]::IsNullOrWhiteSpace([string]$_)
        }
    )
    if (
        $afterNonemptyStatusLines.Count -eq 0 -or
        [string]$afterNonemptyStatusLines[0] -notmatch `
            '^[^:]+:\s*0\s*(?:\([^\r\n]*\))?\s*$'
    ) {
        throw "resulting Windows Time status lost synchronized leap state"
    }
    $afterConfigurationPath = Join-Path $BackupRoot "configuration-after.txt"
    $afterStatusPath = Join-Path $BackupRoot "status-after.txt"
    Write-Utf8NoBom -Path $afterConfigurationPath -Value (
        (($afterConfigurationLines | Out-String).Trim()) + "`n"
    )
    Write-Utf8NoBom -Path $afterStatusPath -Value ($afterStatusText + "`n")
    $receipt = [ordered]@{
        schema = "agent-invest-windows-time-receipt.v1"
        configured_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        installer_sha256 = "sha256:$(Get-LowerSha256 -Path $PSCommandPath)"
        prior = [ordered]@{
            ntp_server = $beforeNtpServer
            type = $beforeType
            registry_backup = $beforeRegistryPath
            configuration_exit_code = $beforeConfigurationExitCode
            configuration_path = $beforeConfigurationPath
            status_exit_code = $beforeStatusExitCode
            status_path = $beforeStatusPath
        }
        current = [ordered]@{
            ntp_server = [string]$after.NtpServer
            type = [string]$after.Type
            synchronized = $true
            source = $sourceText
            leap_status_line = $leapStatusLine
            successful_attempt = $successfulAttempt
            configuration_exit_code = $afterConfigurationExitCode
            configuration_path = $afterConfigurationPath
            status_exit_code = $afterStatusExitCode
            status_path = $afterStatusPath
            peer_probes = $stripchart
        }
    }
    $receiptJson = ($receipt | ConvertTo-Json -Depth 20) + "`n"
    $receiptDirectory = Split-Path -Parent $ReceiptTarget
    $ReceiptTempPath = Join-Path $receiptDirectory (
        ".windows-time-receipt.$([Guid]::NewGuid().ToString('N')).tmp"
    )
    $receiptBytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes(
        $receiptJson
    )
    $receiptStream = New-Object IO.FileStream(
        $ReceiptTempPath,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $receiptStream.Write($receiptBytes, 0, $receiptBytes.Length)
        $receiptStream.Flush($true)
    }
    finally { $receiptStream.Dispose() }
    [IO.File]::Move($ReceiptTempPath, $ReceiptTarget)
    $ReceiptTempPath = $null
    $Committed = $true
}
catch {
    $originalError = $_.Exception.Message
    $rollbackErrors = New-Object Collections.Generic.List[string]
    if ($null -ne $ReceiptTempPath -and (Test-Path -LiteralPath $ReceiptTempPath)) {
        try { Remove-Item -LiteralPath $ReceiptTempPath -Force }
        catch { $rollbackErrors.Add("temporary receipt remains") }
    }
    if ($MutationStarted) {
        try {
            Set-ItemProperty -LiteralPath $ParametersKey -Name NtpServer `
                -Value $beforeNtpServer
            Set-ItemProperty -LiteralPath $ParametersKey -Name Type `
                -Value $beforeType
            Restart-Service -Name w32time -Force
        }
        catch { $rollbackErrors.Add($_.Exception.Message) }
    }
    $rolledBack = Get-ItemProperty -LiteralPath $ParametersKey
    if (
        [string]$rolledBack.NtpServer -ne $beforeNtpServer -or
        [string]$rolledBack.Type -ne $beforeType
    ) {
        $rollbackErrors.Add("Windows Time peer policy was not restored")
    }
    if ($rollbackErrors.Count -eq 0) {
        throw "Windows Time configuration failed and rollback was verified: $originalError"
    }
    throw (
        "Windows Time configuration failed with partial rollback: " +
        "$originalError; $($rollbackErrors -join '; ')"
    )
}
if ($Committed) { Write-Output $receiptJson }
