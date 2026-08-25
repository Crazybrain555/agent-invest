[CmdletBinding()]
param(
    [string]$TunnelUser = "agentinvest_mineru",
    [string]$SshdConfig = "C:\ProgramData\ssh\sshd_config",
    [string]$PriorReceipt = "C:\ProgramData\agent-invest\mineru\tunnel-key-receipt.json",
    [string]$ExporterReceipt = "C:\ProgramData\agent-invest\gpu-metrics\install-receipt.json",
    [string]$ReceiptTarget = "C:\ProgramData\agent-invest\mineru\tunnel-gpu-metrics-amendment.json"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$SshdBinary = "C:\Windows\System32\OpenSSH\sshd.exe"
$AuthorizedKeysPath = "C:\Users\$TunnelUser\.ssh\authorized_keys"
$Marker = "# agent-invest MinerU tunnel: $TunnelUser"
$ExpectedOldPermitOpen = "PermitOpen 127.0.0.1:30001 127.0.0.1:30003"
$ExpectedNewPermitOpen = "$ExpectedOldPermitOpen 127.0.0.1:9835"
$OldAuthorizedPrefix = (
    'command="powershell.exe -NoProfile -NonInteractive -Command ' +
    'Start-Sleep -Seconds 2147483",restrict,port-forwarding,' +
    'permitopen="127.0.0.1:30001",permitopen="127.0.0.1:30003" '
)
$NewAuthorizedPrefix = (
    'command="powershell.exe -NoProfile -NonInteractive -Command ' +
    'Start-Sleep -Seconds 2147483",restrict,port-forwarding,' +
    'permitopen="127.0.0.1:30001",permitopen="127.0.0.1:30003",' +
    'permitopen="127.0.0.1:9835" '
)
$Timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$BackupRoot = "C:\ProgramData\agent-invest\mineru\tunnel-upgrade-$Timestamp"
$ConfigBackup = Join-Path $BackupRoot "sshd_config.bak"
$AuthorizedKeysBackup = Join-Path $BackupRoot "authorized_keys.bak"
$MutationStarted = $false

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

function Wait-ForLocalEndpoint {
    param([Parameter(Mandatory = $true)][string]$Uri)
    for ($attempt = 1; $attempt -le 30; $attempt += 1) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri $Uri
            if ($response.StatusCode -eq 200) { return }
        }
        catch {
            # sshd/exporter restart may still be converging.
        }
        Start-Sleep -Seconds 1
    }
    throw "local endpoint did not become healthy: $Uri"
}

$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ($currentSid -ne "S-1-5-18") {
    throw "tunnel amendment must run as LocalSystem to preserve the private key ACL"
}
foreach ($path in @(
    $SshdConfig,
    $PriorReceipt,
    $ExporterReceipt,
    $AuthorizedKeysPath,
    $SshdBinary
)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "required tunnel amendment input is missing: $path"
    }
}
if (Test-Path -LiteralPath $ReceiptTarget) {
    throw "tunnel amendment receipt already exists; refusing ambiguous replay"
}
$prior = Get-Content -LiteralPath $PriorReceipt -Raw | ConvertFrom-Json
if (
    $prior.schema -ne "mineru-tunnel-key-install-receipt.v2" -or
    $prior.tunnel_user -ne $TunnelUser -or
    (@($prior.permitopen) -join ",") -ne "127.0.0.1:30001,127.0.0.1:30003"
) {
    throw "prior tunnel receipt is not the exact v2 two-forward baseline"
}
$exporter = Get-Content -LiteralPath $ExporterReceipt -Raw | ConvertFrom-Json
if (
    $exporter.schema -ne "nvidia-gpu-exporter-install-receipt.v1" -or
    $exporter.release.project -ne "utkuozdemir/nvidia_gpu_exporter" -or
    $exporter.release.version -ne "1.14.0" -or
    $exporter.release.archive_sha256 -ne `
        "sha256:1cbdb0a20da9053b41101afc3c68ffc73445aefb1ec8c4b99e68e54a7a3a3190" -or
    $exporter.release.executable_sha256 -ne `
        "sha256:cb5e441c3c7a1c1072f3462a69f00c3e2d7d717e66be75171494fe7c670551f7" -or
    $exporter.listener.address -ne "127.0.0.1" -or
    $exporter.listener.port -ne 9835 -or
    $exporter.service_identity.administrator -ne $false
) {
    throw "GPU metrics install receipt is not the reviewed deployment"
}
$tunnelSid = [string]$prior.tunnel_user_sid
$authorizedAclBefore = Get-Acl -LiteralPath $AuthorizedKeysPath
$authorizedOwnerBefore = $authorizedAclBefore.GetOwner(
    [Security.Principal.SecurityIdentifier]
).Value
if ($authorizedOwnerBefore -ne $tunnelSid -or $authorizedAclBefore.Sddl -ne $prior.authorized_keys_acl_sddl) {
    throw "authorized_keys owner or ACL drifted from the prior receipt"
}
$configBefore = [string](Get-Content -LiteralPath $SshdConfig -Raw)
$authorizedBefore = ([string](Get-Content -LiteralPath $AuthorizedKeysPath -Raw)).Trim()
if (
    [regex]::Matches(
        $configBefore,
        [regex]::Escape($ExpectedOldPermitOpen)
    ).Count -ne 1
) {
    throw "sshd_config does not contain exactly one reviewed old PermitOpen line"
}
if ($configBefore.Contains($ExpectedNewPermitOpen)) {
    throw "sshd_config already contains the GPU metrics PermitOpen"
}
if (
    -not $authorizedBefore.StartsWith($OldAuthorizedPrefix) -or
    $authorizedBefore.StartsWith($NewAuthorizedPrefix) -or
    $authorizedBefore.Substring($OldAuthorizedPrefix.Length) -notmatch `
        '^ssh-ed25519 [A-Za-z0-9+/]+={0,2}( [^\r\n]+)?$'
) {
    throw "authorized_keys is not the exact reviewed two-forward key shape"
}
$markerIndex = $configBefore.IndexOf($Marker)
if ($markerIndex -lt 0) { throw "dedicated tunnel Match marker is missing" }
$matchBlock = ($configBefore.Substring($markerIndex) -replace "`r`n", "`n")
$expectedMatchBlock = @(
    $Marker,
    "Match User $TunnelUser",
    "    AuthorizedKeysFile `"$AuthorizedKeysPath`"",
    "    AuthenticationMethods publickey",
    "    PasswordAuthentication no",
    "    AllowAgentForwarding no",
    "    AllowTcpForwarding local",
    "    AllowStreamLocalForwarding no",
    "    $ExpectedOldPermitOpen",
    "    PermitListen none",
    "    PermitTTY no",
    "    MaxSessions 0",
    ""
) -join "`n"
if ($matchBlock.TrimEnd() -ne $expectedMatchBlock.TrimEnd()) {
    throw "dedicated tunnel Match block drifted from the reviewed baseline"
}
$exporterListeners = @(Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 `
    -LocalPort 9835 -ErrorAction SilentlyContinue)
if ($exporterListeners.Count -ne 1) {
    throw "GPU metrics must have exactly one loopback listener before authorization"
}
$exporterPid = [int]$exporterListeners[0].OwningProcess
$exporterProcess = Get-Process -Id $exporterPid
if (
    $exporterProcess.Path -ne $exporter.executable_path -or
    "sha256:$(Get-LowerSha256 -Path $exporterProcess.Path)" -ne `
        $exporter.release.executable_sha256
) {
    throw "GPU metrics listener executable drifted from its receipt"
}
$exporterCim = Get-CimInstance -ClassName Win32_Process `
    -Filter "ProcessId = $exporterPid"
$exporterOwner = Invoke-CimMethod -InputObject $exporterCim -MethodName GetOwnerSid
if (
    $exporterOwner.ReturnValue -ne 0 -or
    $exporterOwner.Sid -ne $exporter.service_identity.sid
) {
    throw "GPU metrics listener owner drifted from its receipt"
}
$metricsResponse = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 `
    -Uri "http://127.0.0.1:9835/metrics"
if (
    $metricsResponse.StatusCode -ne 200 -or
    -not $metricsResponse.Content.Contains("nvidia_smi_last_collect_success 1") -or
    $metricsResponse.Content -notmatch `
        'nvidia_smi_gpu_info\{[^\r\n]*uuid="([^"]+)"[^\r\n]*\} 1'
) {
    throw "GPU metrics endpoint lacks a successful bound GPU identity"
}
$metricsUuid = $Matches[1].ToLowerInvariant().Replace("gpu-", "")
$receiptUuid = ([string]$exporter.gpu.uuid).ToLowerInvariant().Replace("gpu-", "")
if ($metricsUuid -ne $receiptUuid) {
    throw "GPU metrics UUID drifted from the exporter receipt"
}

New-Item -ItemType Directory -Path $BackupRoot | Out-Null
Copy-Item -LiteralPath $SshdConfig -Destination $ConfigBackup
Copy-Item -LiteralPath $AuthorizedKeysPath -Destination $AuthorizedKeysBackup
$configShaBefore = Get-LowerSha256 -Path $SshdConfig
$authorizedShaBefore = Get-LowerSha256 -Path $AuthorizedKeysPath

try {
    $MutationStarted = $true
    $configAfter = $configBefore.Replace($ExpectedOldPermitOpen, $ExpectedNewPermitOpen)
    $authorizedAfter = (
        $NewAuthorizedPrefix + $authorizedBefore.Substring($OldAuthorizedPrefix.Length)
    )
    Write-Utf8NoBom -Path $SshdConfig -Value $configAfter
    Write-Utf8NoBom -Path $AuthorizedKeysPath -Value ($authorizedAfter + "`n")
    & icacls.exe $AuthorizedKeysPath "/inheritance:r" "/grant:r" `
        "*$tunnelSid`:(F)" "*S-1-5-18:(F)" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "cannot restore exact authorized_keys ACL" }
    & icacls.exe $AuthorizedKeysPath "/setowner" "*$tunnelSid" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "cannot restore authorized_keys owner" }
    $authorizedAclAfter = Get-Acl -LiteralPath $AuthorizedKeysPath
    if (
        $authorizedAclAfter.Sddl -ne $prior.authorized_keys_acl_sddl -or
        $authorizedAclAfter.GetOwner(
            [Security.Principal.SecurityIdentifier]
        ).Value -ne $tunnelSid
    ) {
        throw "authorized_keys ACL or owner changed during amendment"
    }
    & $SshdBinary -t -f $SshdConfig
    if ($LASTEXITCODE -ne 0) { throw "sshd rejected the amended Match block" }
    Restart-Service -Name sshd -Force
    if ((Get-Service -Name sshd).Status -ne "Running") {
        throw "sshd did not return to Running"
    }
    Wait-ForLocalEndpoint -Uri "http://127.0.0.1:9835/metrics"

    $receipt = [ordered]@{
        schema = "mineru-tunnel-gpu-metrics-amendment.v1"
        installed_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        installer_sha256 = "sha256:$(Get-LowerSha256 -Path $PSCommandPath)"
        prior_receipt_sha256 = "sha256:$(Get-LowerSha256 -Path $PriorReceipt)"
        exporter_receipt_sha256 = "sha256:$(Get-LowerSha256 -Path $ExporterReceipt)"
        exporter_executable_sha256 = $exporter.release.executable_sha256
        exporter_service_sid = $exporter.service_identity.sid
        gpu_uuid = $exporter.gpu.uuid
        tunnel_user = $TunnelUser
        tunnel_user_sid = $tunnelSid
        sshd_config_path = $SshdConfig
        authorized_keys_path = $AuthorizedKeysPath
        sshd_config_backup = $ConfigBackup
        authorized_keys_backup = $AuthorizedKeysBackup
        sshd_config_sha256_before = "sha256:$configShaBefore"
        sshd_config_sha256_after = "sha256:$(Get-LowerSha256 -Path $SshdConfig)"
        authorized_keys_sha256_before = "sha256:$authorizedShaBefore"
        authorized_keys_sha256_after = "sha256:$(Get-LowerSha256 -Path $AuthorizedKeysPath)"
        forwarding_direction = "local-only"
        permitopen = @(
            "127.0.0.1:30001",
            "127.0.0.1:30003",
            "127.0.0.1:9835"
        )
        permitlisten = "none"
        max_sessions = 0
        authorized_keys_acl_sddl = $authorizedAclAfter.Sddl
        sshd_service_status = [string](Get-Service -Name sshd).Status
    }
    Write-Utf8NoBom -Path $ReceiptTarget -Value (
        ($receipt | ConvertTo-Json -Depth 20) + "`n"
    )
    $receipt | ConvertTo-Json -Depth 20
}
catch {
    $originalError = $_.Exception.Message
    $rollbackErrors = New-Object Collections.Generic.List[string]
    if ($MutationStarted) {
        try {
            Copy-Item -LiteralPath $ConfigBackup -Destination $SshdConfig -Force
            Copy-Item -LiteralPath $AuthorizedKeysBackup `
                -Destination $AuthorizedKeysPath -Force
            & icacls.exe $AuthorizedKeysPath "/inheritance:r" "/grant:r" `
                "*$tunnelSid`:(F)" "*S-1-5-18:(F)" | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "cannot restore backup ACL" }
            & icacls.exe $AuthorizedKeysPath "/setowner" "*$tunnelSid" | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "cannot restore backup owner" }
            & $SshdBinary -t -f $SshdConfig
            if ($LASTEXITCODE -ne 0) { throw "restored sshd_config is invalid" }
            Restart-Service -Name sshd -Force
        }
        catch { $rollbackErrors.Add($_.Exception.Message) }
    }
    if ((Get-LowerSha256 -Path $SshdConfig) -ne $configShaBefore) {
        $rollbackErrors.Add("sshd_config hash was not restored")
    }
    if ((Get-LowerSha256 -Path $AuthorizedKeysPath) -ne $authorizedShaBefore) {
        $rollbackErrors.Add("authorized_keys hash was not restored")
    }
    $failure = [ordered]@{
        schema = "mineru-tunnel-gpu-metrics-amendment-failure.v1"
        failed_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        error = $originalError
        rollback_verified = ($rollbackErrors.Count -eq 0)
        rollback_errors = @($rollbackErrors)
    }
    Write-Utf8NoBom -Path "$ReceiptTarget.failure.json" -Value (
        ($failure | ConvertTo-Json -Depth 20) + "`n"
    )
    if ($rollbackErrors.Count -eq 0) {
        throw "tunnel amendment failed and rollback was verified: $originalError"
    }
    throw (
        "tunnel amendment failed with partial rollback: $originalError; " +
        ($rollbackErrors -join "; ")
    )
}
