[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ArchiveSource,
    [string]$Version = "1.14.0",
    [string]$ExpectedArchiveSha256 = "1cbdb0a20da9053b41101afc3c68ffc73445aefb1ec8c4b99e68e54a7a3a3190",
    [string]$ExpectedExecutableSha256 = "cb5e441c3c7a1c1072f3462a69f00c3e2d7d717e66be75171494fe7c670551f7",
    [string]$WatchdogSource = "C:\ProgramData\agent-invest\staging\nvidia_gpu_exporter_watchdog.ps1",
    [string]$ExpectedWatchdogSha256 = "0fc87534a83c29996d964d8b875cf71cacfa1a5af1036686259e7a5544976ba8",
    [string]$ServiceUser = "agentinvest_gpu",
    [string]$ReceiptTarget = "C:\ProgramData\agent-invest\gpu-metrics\install-receipt.json"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$InstallRoot = "C:\ProgramData\agent-invest\gpu-metrics"
$ExecutablePath = Join-Path $InstallRoot "nvidia_gpu_exporter.exe"
$LicensePath = Join-Path $InstallRoot "LICENSE"
$WatchdogPath = Join-Path $InstallRoot "nvidia_gpu_exporter_watchdog.ps1"
$NvidiaSmiPath = "C:\Windows\System32\nvidia-smi.exe"
$TaskName = "GPU Metrics"
$WatchdogTaskName = "GPU Metrics Watchdog"
$TaskPath = "\agent-invest\"
$ListenAddress = "127.0.0.1:9835"
$UserCreated = $false
$BatchRightGranted = $false
$InstallCreated = $false
$TaskCreated = $false
$WatchdogTaskCreated = $false
$PlainPassword = $null
$StagingRoot = $null

Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;

namespace AgentInvestLsa {
    public static class AccountRights {
        [StructLayout(LayoutKind.Sequential)]
        private struct LSA_OBJECT_ATTRIBUTES {
            public int Length;
            public IntPtr RootDirectory;
            public IntPtr ObjectName;
            public uint Attributes;
            public IntPtr SecurityDescriptor;
            public IntPtr SecurityQualityOfService;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct LSA_UNICODE_STRING {
            public ushort Length;
            public ushort MaximumLength;
            public IntPtr Buffer;
        }

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern uint LsaOpenPolicy(
            IntPtr systemName,
            ref LSA_OBJECT_ATTRIBUTES objectAttributes,
            uint desiredAccess,
            out IntPtr policyHandle
        );

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern uint LsaAddAccountRights(
            IntPtr policyHandle,
            IntPtr accountSid,
            LSA_UNICODE_STRING[] userRights,
            uint countOfRights
        );

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern uint LsaRemoveAccountRights(
            IntPtr policyHandle,
            IntPtr accountSid,
            bool allRights,
            LSA_UNICODE_STRING[] userRights,
            uint countOfRights
        );

        [DllImport("advapi32.dll")]
        private static extern uint LsaNtStatusToWinError(uint status);

        [DllImport("advapi32.dll")]
        private static extern uint LsaClose(IntPtr policyHandle);

        private static void ChangeRight(string sidValue, string right, bool add) {
            SecurityIdentifier sid = new SecurityIdentifier(sidValue);
            byte[] sidBytes = new byte[sid.BinaryLength];
            sid.GetBinaryForm(sidBytes, 0);
            GCHandle sidHandle = GCHandle.Alloc(sidBytes, GCHandleType.Pinned);
            IntPtr rightBuffer = Marshal.StringToHGlobalUni(right);
            IntPtr policyHandle = IntPtr.Zero;
            try {
                LSA_OBJECT_ATTRIBUTES attributes = new LSA_OBJECT_ATTRIBUTES();
                attributes.Length = Marshal.SizeOf(typeof(LSA_OBJECT_ATTRIBUTES));
                uint status = LsaOpenPolicy(
                    IntPtr.Zero,
                    ref attributes,
                    0x00000810,
                    out policyHandle
                );
                if (status != 0) {
                    throw new Win32Exception((int)LsaNtStatusToWinError(status));
                }
                LSA_UNICODE_STRING[] rights = new LSA_UNICODE_STRING[1];
                rights[0].Buffer = rightBuffer;
                rights[0].Length = (ushort)(right.Length * 2);
                rights[0].MaximumLength = (ushort)((right.Length + 1) * 2);
                status = add
                    ? LsaAddAccountRights(
                        policyHandle, sidHandle.AddrOfPinnedObject(), rights, 1
                    )
                    : LsaRemoveAccountRights(
                        policyHandle, sidHandle.AddrOfPinnedObject(), false, rights, 1
                    );
                if (status != 0) {
                    throw new Win32Exception((int)LsaNtStatusToWinError(status));
                }
            }
            finally {
                if (policyHandle != IntPtr.Zero) { LsaClose(policyHandle); }
                Marshal.FreeHGlobal(rightBuffer);
                sidHandle.Free();
            }
        }

        public static void Add(string sidValue, string right) {
            ChangeRight(sidValue, right, true);
        }

        public static void Remove(string sidValue, string right) {
            ChangeRight(sidValue, right, false);
        }
    }
}
'@

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

function Wait-ForExporter {
    param([Parameter(Mandatory = $true)][int]$Attempts)
    for ($attempt = 1; $attempt -le $Attempts; $attempt += 1) {
        try {
            $health = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 `
                -Uri "http://127.0.0.1:9835/-/healthy"
            $metrics = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 `
                -Uri "http://127.0.0.1:9835/metrics"
            if (
                $health.StatusCode -eq 200 -and
                $metrics.StatusCode -eq 200 -and
                $metrics.Content.Contains("nvidia_smi_last_collect_success 1") -and
                $metrics.Content.Contains("nvidia_smi_gpu_info{")
            ) {
                return [string]$metrics.Content
            }
        }
        catch {
            # Startup and the first bounded nvidia-smi collection may race.
        }
        Start-Sleep -Seconds 1
    }
    $task = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName `
        -ErrorAction SilentlyContinue
    $taskInfo = Get-ScheduledTaskInfo -TaskPath $TaskPath -TaskName $TaskName `
        -ErrorAction SilentlyContinue
    $listener = Get-NetTCPConnection -State Listen -LocalPort 9835 `
        -ErrorAction SilentlyContinue
    $state = if ($null -ne $task) { [string]$task.State } else { "missing" }
    $lastResult = if ($null -ne $taskInfo) { $taskInfo.LastTaskResult } else { "missing" }
    $listenerCount = @($listener).Count
    throw (
        "GPU metrics exporter did not become healthy with a successful " +
        "collection; task_state=$state last_result=$lastResult " +
        "listener_count=$listenerCount"
    )
}

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "only 64-bit Windows is supported"
}
$InstallerSha256 = Get-LowerSha256 -Path $PSCommandPath
if ($Version -ne "1.14.0") {
    throw "exporter version is not the reviewed pinned version"
}
if ($ExpectedArchiveSha256 -notmatch '^[0-9a-f]{64}$') {
    throw "expected archive SHA-256 is not canonical lowercase hex"
}
if ($ExpectedExecutableSha256 -notmatch '^[0-9a-f]{64}$') {
    throw "expected executable SHA-256 is not canonical lowercase hex"
}
if ($ExpectedWatchdogSha256 -notmatch '^[0-9a-f]{64}$') {
    throw "expected watchdog SHA-256 is not canonical lowercase hex"
}
if ($ServiceUser -notmatch '^[a-z][a-z0-9_]{2,19}$') {
    throw "service user name is invalid"
}
foreach ($path in @($ArchiveSource, $WatchdogSource, $NvidiaSmiPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "required installer input is missing: $path"
    }
}
if ((Get-LowerSha256 -Path $WatchdogSource) -ne $ExpectedWatchdogSha256) {
    throw "GPU metrics watchdog SHA-256 drifted"
}
if ((Get-LowerSha256 -Path $ArchiveSource) -ne $ExpectedArchiveSha256) {
    throw "exporter archive SHA-256 does not match the reviewed release asset"
}
if (Test-Path -LiteralPath $InstallRoot) {
    throw "GPU metrics install root already exists; refusing ambiguous reinstall"
}
if ($null -ne (Get-LocalUser -Name $ServiceUser -ErrorAction SilentlyContinue)) {
    throw "dedicated GPU metrics user already exists; refusing ambiguous reinstall"
}
if ($null -ne (Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    throw "GPU metrics scheduled task already exists; refusing ambiguous reinstall"
}
if ($null -ne (Get-ScheduledTask -TaskPath $TaskPath -TaskName $WatchdogTaskName -ErrorAction SilentlyContinue)) {
    throw "GPU metrics watchdog task already exists; refusing ambiguous reinstall"
}
if (Get-NetTCPConnection -State Listen -LocalPort 9835 -ErrorAction SilentlyContinue) {
    throw "port 9835 already has a listener"
}

$StagingRoot = Join-Path ([IO.Path]::GetTempPath()) (
    "agentinvest-gpu-metrics-" + [Guid]::NewGuid().ToString("N")
)
try {
    New-Item -ItemType Directory -Path $StagingRoot | Out-Null
    Expand-Archive -LiteralPath $ArchiveSource -DestinationPath $StagingRoot
    $stagedFiles = @(Get-ChildItem -LiteralPath $StagingRoot -File | Sort-Object Name)
    if (
        $stagedFiles.Count -ne 2 -or
        ($stagedFiles.Name -join ",") -ne "LICENSE,nvidia_gpu_exporter.exe"
    ) {
        throw "exporter archive does not contain the exact reviewed file set"
    }
    $stagedExecutable = Join-Path $StagingRoot "nvidia_gpu_exporter.exe"
    if ((Get-LowerSha256 -Path $stagedExecutable) -ne $ExpectedExecutableSha256) {
        throw "extracted exporter executable SHA-256 drifted"
    }
    $versionStdout = Join-Path $StagingRoot "version.stdout.txt"
    $versionStderr = Join-Path $StagingRoot "version.stderr.txt"
    $versionProcess = Start-Process -FilePath $stagedExecutable `
        -ArgumentList "--version" -Wait -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $versionStdout -RedirectStandardError $versionStderr
    $versionOutput = (
        @(
            Get-Content -LiteralPath $versionStdout -Raw -ErrorAction SilentlyContinue
            Get-Content -LiteralPath $versionStderr -Raw -ErrorAction SilentlyContinue
        ) -join ""
    ).Trim()
    if (
        $versionProcess.ExitCode -ne 0 -or
        -not $versionOutput.Contains("version 1.14.0")
    ) {
        throw "exporter executable did not report the pinned version"
    }

    $passwordBytes = New-Object byte[] 48
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($passwordBytes) }
    finally { $random.Dispose() }
    $PlainPassword = [Convert]::ToBase64String($passwordBytes)
    $securePassword = ConvertTo-SecureString $PlainPassword -AsPlainText -Force
    New-LocalUser -Name $ServiceUser -Password $securePassword -AccountNeverExpires `
        -PasswordNeverExpires -UserMayNotChangePassword -Description `
        "agent-invest loopback NVIDIA metrics" | Out-Null
    $UserCreated = $true
    $serviceIdentity = Get-LocalUser -Name $ServiceUser
    $serviceSid = [string]$serviceIdentity.SID.Value
    $usersGroup = Get-LocalGroup -SID "S-1-5-32-545"
    Add-LocalGroupMember -Group $usersGroup -Member $serviceIdentity
    [AgentInvestLsa.AccountRights]::Add($serviceSid, "SeBatchLogonRight")
    $BatchRightGranted = $true
    $administrators = Get-LocalGroup -SID "S-1-5-32-544" | Get-LocalGroupMember
    if ($administrators | Where-Object { $_.SID.Value -eq $serviceSid }) {
        throw "GPU metrics service account must not be an administrator"
    }

    New-Item -ItemType Directory -Path $InstallRoot | Out-Null
    $InstallCreated = $true
    Copy-Item -LiteralPath $stagedExecutable -Destination $ExecutablePath
    Copy-Item -LiteralPath (Join-Path $StagingRoot "LICENSE") -Destination $LicensePath
    Copy-Item -LiteralPath $WatchdogSource -Destination $WatchdogPath
    & icacls.exe $InstallRoot "/inheritance:r" "/grant:r" `
        "*$serviceSid`:(OI)(CI)(RX)" "*S-1-5-18:(OI)(CI)(F)" `
        "*S-1-5-32-544:(OI)(CI)(F)" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "cannot set GPU metrics install ACL" }
    & icacls.exe $InstallRoot "/setowner" "*S-1-5-32-544" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "cannot set GPU metrics install owner" }
    $installAcl = Get-Acl -LiteralPath $InstallRoot
    $aclSddl = $installAcl.Sddl
    $actualPrincipals = @($installAcl.Access | ForEach-Object {
        $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    } | Sort-Object -Unique)
    $expectedPrincipals = @($serviceSid, "S-1-5-18", "S-1-5-32-544") | Sort-Object -Unique
    if (($actualPrincipals -join ",") -ne ($expectedPrincipals -join ",")) {
        throw "GPU metrics install ACL contains unexpected principals"
    }

    $argumentList = @(
        "--web.listen-address=$ListenAddress",
        "--web.network=tcp4",
        "--web.max-requests=2",
        "--web.read-timeout=5s",
        "--web.read-header-timeout=5s",
        "--web.write-timeout=5s",
        "--web.idle-timeout=15s",
        "--nvidia-smi-command=$NvidiaSmiPath",
        "--query-field-names=timestamp,name,uuid,index,utilization.gpu,memory.total,memory.used,memory.free,temperature.gpu,power.draw",
        "--collect.interval=5s",
        "--collect.timeout=3s",
        "--no-collect.compute-apps",
        "--no-shutdown-on-error",
        "--no-web.enable-pprof",
        "--log.level=warn"
    )
    $taskArguments = $argumentList -join " "
    $action = New-ScheduledTaskAction -Execute $ExecutablePath `
        -Argument $taskArguments -WorkingDirectory $InstallRoot
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName `
        -Action $action -Trigger $trigger -Settings $settings `
        -User "$env:COMPUTERNAME\$ServiceUser" -Password $PlainPassword `
        -RunLevel Limited | Out-Null
    $TaskCreated = $true
    $watchdogCommand = (
        "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass " +
        "-File $WatchdogPath"
    )
    & schtasks.exe /Create /TN "$TaskPath$WatchdogTaskName" /SC MINUTE /MO 1 `
        /RU SYSTEM /RL HIGHEST /TR $watchdogCommand /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "cannot register GPU metrics watchdog task" }
    $WatchdogTaskCreated = $true
    Start-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
    $metrics = Wait-ForExporter -Attempts 30

    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort 9835)
    if (
        $listeners.Count -ne 1 -or
        $listeners[0].LocalAddress -ne "127.0.0.1"
    ) {
        throw "GPU metrics exporter is not bound to exactly one IPv4 loopback listener"
    }
    $listenerPid = [int]$listeners[0].OwningProcess
    $listenerProcess = Get-Process -Id $listenerPid
    if ($listenerProcess.Path -ne $ExecutablePath) {
        throw "port 9835 is not owned by the pinned exporter executable"
    }
    $listenerCim = Get-CimInstance -ClassName Win32_Process `
        -Filter "ProcessId = $listenerPid"
    $ownerResult = Invoke-CimMethod -InputObject $listenerCim -MethodName GetOwnerSid
    if ($ownerResult.ReturnValue -ne 0 -or $ownerResult.Sid -ne $serviceSid) {
        throw "GPU metrics exporter process is not owned by the service identity"
    }
    $gpuIdentity = (& $NvidiaSmiPath `
        --query-gpu=uuid,name,driver_version `
        --format=csv,noheader,nounits 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or ($gpuIdentity -split "`r?`n").Count -ne 1) {
        throw "expected exactly one observable NVIDIA GPU"
    }
    $gpuFields = @($gpuIdentity -split "," | ForEach-Object { $_.Trim() })
    if ($gpuFields.Count -ne 3) { throw "NVIDIA GPU identity is malformed" }
    $task = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
    $taskInfo = Get-ScheduledTaskInfo -TaskPath $TaskPath -TaskName $TaskName
    if ($task.Principal.RunLevel -ne "Limited" -or $task.Settings.ExecutionTimeLimit -ne "PT0S") {
        throw "GPU metrics scheduled task privilege or lifetime drifted"
    }
    $taskPrincipal = ([Security.Principal.NTAccount]$task.Principal.UserId).Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
    if ($taskPrincipal -ne $serviceSid) {
        throw "GPU metrics scheduled task principal drifted"
    }
    $observedAction = @($task.Actions)
    if (
        $observedAction.Count -ne 1 -or
        $observedAction[0].Execute -ne $ExecutablePath -or
        $observedAction[0].Arguments -ne $taskArguments -or
        $observedAction[0].WorkingDirectory -ne $InstallRoot
    ) {
        throw "GPU metrics scheduled task action drifted"
    }

    $receipt = [ordered]@{
        schema = "nvidia-gpu-exporter-install-receipt.v1"
        installed_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        release = [ordered]@{
            project = "utkuozdemir/nvidia_gpu_exporter"
            version = $Version
            archive_sha256 = "sha256:$ExpectedArchiveSha256"
            executable_sha256 = "sha256:$ExpectedExecutableSha256"
            observed_executable_sha256 = "sha256:$(Get-LowerSha256 -Path $ExecutablePath)"
            installer_sha256 = "sha256:$InstallerSha256"
            version_output = $versionOutput
        }
        executable_path = $ExecutablePath
        watchdog = [ordered]@{
            path = $WatchdogPath
            sha256 = "sha256:$ExpectedWatchdogSha256"
            task_path = $TaskPath
            task_name = $WatchdogTaskName
            interval = "PT1M"
            principal_sid = "S-1-5-18"
        }
        argv = @($argumentList)
        listener = [ordered]@{
            address = "127.0.0.1"
            port = 9835
            protocol = "tcp4"
            pid = $listenerPid
            process_owner_sid = $ownerResult.Sid
        }
        service_identity = [ordered]@{
            user = $ServiceUser
            sid = $serviceSid
            administrator = $false
            local_groups = @("S-1-5-32-545")
            account_rights = @("SeBatchLogonRight")
        }
        scheduled_task = [ordered]@{
            path = $TaskPath
            name = $TaskName
            state = [string]$task.State
            last_task_result = $taskInfo.LastTaskResult
            execution_time_limit = [string]$task.Settings.ExecutionTimeLimit
            multiple_instances = [string]$task.Settings.MultipleInstances
            restart_count = $task.Settings.RestartCount
            restart_interval = [string]$task.Settings.RestartInterval
            startup_trigger = $true
        }
        gpu = [ordered]@{
            uuid = $gpuFields[0]
            name = $gpuFields[1]
            driver_version = $gpuFields[2]
        }
        install_acl_sddl = $aclSddl
        metrics_contract = [ordered]@{
            path = "/metrics"
            background_interval_seconds = 5
            collection_timeout_seconds = 3
            last_collect_success = $metrics.Contains("nvidia_smi_last_collect_success 1")
        }
    }
    Write-Utf8NoBom -Path $ReceiptTarget -Value (
        ($receipt | ConvertTo-Json -Depth 20) + "`n"
    )
    $receipt | ConvertTo-Json -Depth 20
}
catch {
    $originalError = $_.Exception.Message
    $rollbackErrors = New-Object Collections.Generic.List[string]
    if ($WatchdogTaskCreated) {
        & schtasks.exe /Delete /TN "$TaskPath$WatchdogTaskName" /F | Out-Null
        if ($LASTEXITCODE -ne 0) {
            $rollbackErrors.Add("watchdog task removal failed")
        }
    }
    if ($TaskCreated) {
        try {
            Stop-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName `
                -ErrorAction SilentlyContinue
            for ($attempt = 1; $attempt -le 30; $attempt += 1) {
                $listener = Get-NetTCPConnection -State Listen -LocalPort 9835 `
                    -ErrorAction SilentlyContinue
                if ($null -eq $listener) { break }
                Start-Sleep -Seconds 1
            }
            $listener = Get-NetTCPConnection -State Listen -LocalPort 9835 `
                -ErrorAction SilentlyContinue
            if ($null -ne $listener) {
                $candidate = Get-Process -Id $listener.OwningProcess `
                    -ErrorAction SilentlyContinue
                if ($null -ne $candidate -and $candidate.Path -eq $ExecutablePath) {
                    Stop-Process -Id $candidate.Id -Force
                }
            }
            Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName `
                -Confirm:$false -ErrorAction Stop
        }
        catch { $rollbackErrors.Add("task: $($_.Exception.Message)") }
    }
    if ($InstallCreated -and (Test-Path -LiteralPath $InstallRoot)) {
        try { Remove-Item -LiteralPath $InstallRoot -Recurse -Force }
        catch { $rollbackErrors.Add("install root: $($_.Exception.Message)") }
    }
    if ($UserCreated) {
        if ($BatchRightGranted) {
            try {
                [AgentInvestLsa.AccountRights]::Remove(
                    $serviceSid,
                    "SeBatchLogonRight"
                )
                $BatchRightGranted = $false
            }
            catch { $rollbackErrors.Add("account right: $($_.Exception.Message)") }
        }
        try { Remove-LocalUser -Name $ServiceUser -ErrorAction Stop }
        catch { $rollbackErrors.Add("service user: $($_.Exception.Message)") }
    }
    if (Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue) {
        $rollbackErrors.Add("scheduled task remains")
    }
    if (Get-ScheduledTask -TaskPath $TaskPath -TaskName $WatchdogTaskName -ErrorAction SilentlyContinue) {
        $rollbackErrors.Add("watchdog scheduled task remains")
    }
    if (Get-NetTCPConnection -State Listen -LocalPort 9835 -ErrorAction SilentlyContinue) {
        $rollbackErrors.Add("port 9835 listener remains")
    }
    if (Test-Path -LiteralPath $InstallRoot) {
        $rollbackErrors.Add("install root remains")
    }
    if ($null -ne (Get-LocalUser -Name $ServiceUser -ErrorAction SilentlyContinue)) {
        $rollbackErrors.Add("service user remains")
    }
    if ($rollbackErrors.Count -eq 0) {
        throw "GPU metrics installation failed and rollback was verified: $originalError"
    }
    throw (
        "GPU metrics installation failed with partial rollback: $originalError; " +
        ($rollbackErrors -join "; ")
    )
}
finally {
    $PlainPassword = $null
    if ($null -ne $StagingRoot -and (Test-Path -LiteralPath $StagingRoot)) {
        Remove-Item -LiteralPath $StagingRoot -Recurse -Force
    }
}
