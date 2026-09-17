<#
.SYNOPSIS
Independent composition tests for the narrow Windows installation owner
(scripts/windows/run_mineru_installation.ps1, Pro R20 execution plan section 2.6).

.DESCRIPTION
Each case runs the real owner script as a real child process against a disposable
release package, a declared installation binding, a compiled fake Docker CLI and a
declared fake installer. No real Docker daemon, engine, service, GPU, PDF, database
or C:\ProgramData path is touched: the owner's own -ComposeTarget/-ReceiptTarget
parameters point into the case workspace, and the child PATH is composed so the only
docker.exe it can resolve is the test fixture.

What is deliberately NOT re-tested here: the installer's own deployment judgements,
its bounded subprocess helper (scripts/windows/test_mineru_install_process.ps1 owns
the hang/flood/oversize/first-error cases) and the rollback registry witness guard
itself (scripts/windows/test_mineru_admission_rollback.ps1 owns those refusals).
This file tests only what the owner layer adds: exclusivity over one target, exact
Job ownership of the installer subtree, and judging installation from agreeing
records and real readback rather than from a child exit code.

Every expectation is a literal declared here. No expected value is produced by the
code under test. Each owner invocation is held by the existing test-only exact
process/Job helper (test_mineru_m6_process.cs): a test deadline is a test failure and
never a product pass, and cleanup only ever terminates this test's own Job.

Inputs required under -SourceRoot: run_mineru_installation.ps1,
build_mineru_telemetry_assembly.ps1, load_mineru_telemetry_assembly.ps1,
collect_mineru_runtime.ps1, mineru_nvml_backend.cs, mineru_resident_wire.cs,
mineru_telemetry_job_supervisor.cs, test_mineru_m6_process.cs,
test_mineru_fake_docker.cs, test_mineru_fake_installer.ps1.

Windows PowerShell 5.1 x64 only. Exit 0 when every selected case passes, 1 otherwise.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SourceRoot,
    [Parameter(Mandatory = $true)][string]$OutputRoot,
    [string]$ExpectedWrapperSha256 = '',
    [string]$CaseFilter = ''
)
Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'Windows PowerShell 5.1 is required' }
if (-not [Environment]::Is64BitProcess) { throw '64-bit PowerShell is required' }

$Utf8 = [Text.UTF8Encoding]::new($false, $true)
$Utf8NoThrow = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $Utf8
$OutputEncoding = $Utf8

# ---- declared bounds -------------------------------------------------------------------------
# 1200000 ms is the product's default candidate Job lifetime and 60000 ms its documented
# minimum; both are read from the binding by the owner and are not changed here.
$JobLifetimeMilliseconds = 1200000
$ForcedJobLifetimeMilliseconds = 60000
$JobCleanupMilliseconds = 2000
$ExpectedOperationBudgetSeconds = 1140
$ExpectedForcedOperationBudgetSeconds = 60
$LockHoldMilliseconds = 20000
$HangMilliseconds = 300000
$ShortDeadlineMilliseconds = 120000
$OwnerDeadlineMilliseconds = 240000
$ForcedDeadlineMilliseconds = 300000
$MarkerDeadlineMilliseconds = 180000
$QuiesceDeadlineMilliseconds = 30000

# ---- declared literal fixtures ---------------------------------------------------------------
$MacExclusivityReceiptSha256 = 'sha256:' + ('6d' * 32)
$DeclaredApiDeviceProfile = 'cuda0'
$DeclaredImageId = 'sha256:' + ('ab' * 32)
$DeclaredOtherImageId = 'sha256:' + ('cd' * 32)
$DeclaredReceiptSha256 = 'sha256:' + ('ef' * 32)
$ComposeFixtureText = @'
# Independent installation-owner test fixture. Never deployed and never parsed by a
# Docker daemon: only its exact bytes and hash matter to the owner under test.
services:
  mineru-api:
    image: mineru-api-compatibility:independent-owner-test-fixture
'@
$DockerfileFixtureText = @'
# Independent installation-owner test fixture. Never built.
FROM scratch
'@
$PatcherFixtureText = @'
"""Independent installation-owner test fixture. Never executed."""
'@
$CapacityFixtureText = '{"contract_version":"mineru.capacity-config.independent-owner-test-fixture.v1","note":"declared bytes only"}'

$LegacyIdleHealth = '{"status":"healthy","queued_tasks":0,"processing_tasks":0}'
$LegacyBusyHealth = '{"status":"healthy","queued_tasks":1,"processing_tasks":0}'
# Literal subset required for the installation owner's closed idle proof, with the
# actual serving contract's names and strict JSON types (not invented counters).
function New-ExplicitCapacityHealth([string]$CapacitySha256) {
    return ('{"status":"healthy","queued_tasks":0,"processing_tasks":0,' +
        '"task_admission":{"schema":"mineru-task-admission.v1","registry_schema":"mineru-task-registry.v3",' +
        '"nonterminal_limit":8,"ingress_tasks":0,"accepted_pending_tasks":0,"accepted_processing_tasks":0,' +
        '"accepted_finalizing_tasks":0,"durable_nonterminal_tasks":0,"routeless_accepted_tasks":0,' +
        '"ingress_cleanup_tasks":0,"unowned_ingress_tasks":0,"scheduled_tasks":0,"queue_depth":0,' +
        '"active_processors":0,"recovery_overcommitted":false,"admission_open":true,"blocked_reason":null},' +
        '"capacity_observation":{"schema":"mineru.capacity-observation.v1","capacity_config_sha256":"' + $CapacitySha256 + '",' +
        '"stage_counters":{"result_capacity_waiting":0,"parse_waiting":0,"parse_active":0,"finalizer_waiting":0,"finalizer_active":0},' +
        '"http_counters":{"active_requests":0,"pending_requests":0},' +
        '"owner_control":{"foreign_loop_observed":false,"soft_drain_requested":false,"soft_drain_applied":false,"trigger":null}}}')
}
# The exact argument vectors the owner is contracted to hand to the Docker CLI.
$ExpectedHealthArguments = @('exec', 'mineru-api', '/usr/bin/python3.12', '-I', '-c',
    'import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen("http://127.0.0.1:8000/health",timeout=5))))')
$ExpectedInspectArguments = @('inspect', '--format', '{{.Image}}', 'mineru-api')
# The complete parameter set the owner is contracted to bind, and nothing else: the legacy
# ExpectedApiTaskSlots / ExpectedApiMaxPendingTasks must never appear.
$ExpectedInstallerParameterNames = @('ApiDeviceProfile', 'ApiOnlyCompatibilityUpgrade', 'CapacityConfigSource',
    'CollectorSource', 'CompatDockerfileSource', 'CompatPatcherSource', 'ComposeSource',
    'ExpectedCapacityConfigSha256', 'OperationBudgetSeconds', 'OperationRecordDirectory')
$ForbiddenOwnerCommands = @('Copy-Item', 'Move-Item', 'Remove-Item', 'Rename-Item', 'Set-Content',
    'Add-Content', 'Out-File', 'Start-Process', 'Invoke-Expression', 'Invoke-WebRequest',
    'Invoke-RestMethod', 'Stop-Process', 'Stop-Service', 'Start-Service', 'Restart-Service',
    'Set-Service', 'Add-Type', 'Register-ScheduledTask')

# ---- helpers ---------------------------------------------------------------------------------
function Check([bool]$Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
function Get-TestSha256Bytes([byte[]]$Bytes) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return 'sha256:' + ([BitConverter]::ToString($algorithm.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $algorithm.Dispose() }
}
# Every read shares with a writer that may still hold the file: a product record that is being
# appended must never turn into a sharing violation the test would report as a product failure.
function Read-SharedBytes([string]$Path) {
    $stream = [IO.FileStream]::new($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        if ($stream.Length -gt 8388608) { throw ('file exceeds the finite test read bound: ' + $Path) }
        $bytes = [byte[]]::new([int]$stream.Length)
        $offset = 0
        while ($offset -lt $bytes.Length) {
            $read = $stream.Read($bytes, $offset, $bytes.Length - $offset)
            if ($read -le 0) { break }
            $offset += $read
        }
        if ($offset -ne $bytes.Length) { throw ('short read: ' + $Path) }
        return ,$bytes
    } finally { $stream.Dispose() }
}
function Get-TestSha256File([string]$Path) { return Get-TestSha256Bytes (Read-SharedBytes $Path) }
function Write-NewTestFile([string]$Path, [byte[]]$Bytes) {
    $stream = [IO.FileStream]::new($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try { $stream.Write($Bytes, 0, $Bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
}
function Write-NewTestText([string]$Path, [string]$Text) { Write-NewTestFile $Path ($Utf8NoThrow.GetBytes($Text)) }
function Read-TestJson([string]$Path) { return ($Utf8.GetString((Read-SharedBytes $Path)) | ConvertFrom-Json) }
function Read-Diagnostic([string]$Path) {
    # Lenient on purpose: a child's console bytes are raw evidence, never an oracle.
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $bytes = Read-SharedBytes $Path
    $count = [Math]::Min($bytes.Length, 65536)
    return $Utf8NoThrow.GetString($bytes, 0, $count)
}
# Exact identity, never a bare PID: a reused process id has a different birth and is not this child.
function Test-ExactProcessAlive([int]$ProcessId, [long]$CreationFiletime) {
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $false }
    $birth = $null
    try { if (-not $process.HasExited) { $birth = $process.StartTime.ToFileTimeUtc() } } catch { $birth = $null }
    if ($null -eq $birth) { return $false }
    return ($birth -eq $CreationFiletime)
}
# A record that exists is not yet a record that is complete; poll until it parses or the finite
# test deadline expires. The last original failure is reported, never swallowed.
function Wait-ForJson([string]$Path, [int]$DeadlineMilliseconds, [string]$Label) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $last = 'the record never appeared'
    while ($true) {
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            try { return Read-TestJson $Path } catch { $last = $_.ToString() }
        }
        if ($watch.ElapsedMilliseconds -ge $DeadlineMilliseconds) {
            throw ($Label + ' was not readable within the test deadline (' + $Path + '): ' + $last)
        }
        Start-Sleep -Milliseconds 100
    }
}
function Wait-ForDockerCalls([string]$LogPath, [int]$Count, [int]$DeadlineMilliseconds) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while (@(Read-DockerCalls $LogPath).Count -lt $Count) {
        if ($watch.ElapsedMilliseconds -ge $DeadlineMilliseconds) { throw ('fewer than ' + $Count + ' declared Docker calls within the test deadline') }
        Start-Sleep -Milliseconds 100
    }
}
function Read-DockerCalls([string]$LogPath) {
    if (-not (Test-Path -LiteralPath $LogPath -PathType Leaf)) { return @() }
    $calls = [Collections.Generic.List[object]]::new()
    foreach ($line in ($Utf8.GetString((Read-SharedBytes $LogPath)) -split "`r`n")) {
        if ($line.Trim().Length -eq 0) { continue }
        $fields = $line.Split([char]9)
        Check ($fields.Count -eq 4) ('fake Docker call record is malformed: ' + $line)
        $argv = $Utf8.GetString([Convert]::FromBase64String($fields[3]))
        $calls.Add([pscustomobject]@{ Utc = $fields[0]; ResponseIndex = [int]$fields[1]; ExitCode = [int]$fields[2]
            Arguments = @($argv.Split([char]31)) })
    }
    return @($calls.ToArray())
}
function Assert-ArgumentsEqual([object[]]$Actual, [string[]]$Expected, [string]$Label) {
    Check ($Actual.Count -eq $Expected.Count) ($Label + ' argument count is ' + $Actual.Count + ', expected ' + $Expected.Count)
    for ($index = 0; $index -lt $Expected.Count; $index++) {
        Check ($Actual[$index] -ceq $Expected[$index]) ($Label + ' argument ' + $index + ' is [' + $Actual[$index] + '], expected [' + $Expected[$index] + ']')
    }
}
function Assert-LockReleased([string]$ComposeTarget, [int]$DeadlineMilliseconds) {
    $lockPath = $ComposeTarget + '.installation.lock'
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) { return $false }
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $last = $null
    while ($true) {
        try {
            $stream = [IO.FileStream]::new($lockPath, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
            $stream.Dispose()
            return $true
        } catch { $last = $_.ToString() }
        if ($watch.ElapsedMilliseconds -ge $DeadlineMilliseconds) {
            throw ('the installation lock is still held after the owner exited: ' + $last)
        }
        Start-Sleep -Milliseconds 100
    }
}

# ---- release package fixture -------------------------------------------------------------------
# Real tracked bytes for everything the owner compiles or hashes as a production source, so the
# source-only supervisor path is genuine; declared literals for everything that would otherwise
# describe a deployable artefact; the declared fake installer in the installer's package slot.
$RealPackageSources = [ordered]@{
    'windows/install_mineru_fixed_api.ps1'         = 'test_mineru_fake_installer.ps1'
    'windows/collect_mineru_runtime.ps1'           = 'collect_mineru_runtime.ps1'
    'windows/build_mineru_telemetry_assembly.ps1'  = 'build_mineru_telemetry_assembly.ps1'
    'windows/load_mineru_telemetry_assembly.ps1'   = 'load_mineru_telemetry_assembly.ps1'
    'windows/mineru_nvml_backend.cs'               = 'mineru_nvml_backend.cs'
    'windows/mineru_resident_wire.cs'              = 'mineru_resident_wire.cs'
    'windows/mineru_telemetry_job_supervisor.cs'   = 'mineru_telemetry_job_supervisor.cs'
}

function New-ReleaseFixture([string]$Name) {
    $caseDirectory = Join-Path $script:CasesRoot $Name
    [void][IO.Directory]::CreateDirectory($caseDirectory)
    # The release root, the operation tree and the Docker fixture all carry a space so every case
    # exercises the quoting that the owner's command construction and argv encoder must survive.
    $releaseRoot = Join-Path $caseDirectory 'release pkg'
    [void][IO.Directory]::CreateDirectory($releaseRoot)
    foreach ($relative in @('windows', 'compose', 'api-context')) {
        [void][IO.Directory]::CreateDirectory((Join-Path $releaseRoot $relative))
    }
    $targetDirectory = Join-Path $caseDirectory 'deployment target'
    $evidenceDirectory = Join-Path $caseDirectory 'child evidence'
    $dockerDirectory = Join-Path $caseDirectory 'docker fixture'
    foreach ($directory in @($targetDirectory, $evidenceDirectory, $dockerDirectory)) {
        [void][IO.Directory]::CreateDirectory($directory)
    }

    $literals = [ordered]@{
        'compose/mineru-windows.compose.yaml' = $script:ComposeFixtureText
        'api-context/Dockerfile'              = $script:DockerfileFixtureText
        'api-context/patch_mineru_344.py'     = $script:PatcherFixtureText
        'api-context/capacity-config.json'    = $script:CapacityFixtureText
    }
    $hashes = [ordered]@{}
    $files = [Collections.Generic.List[object]]::new()
    foreach ($entry in $RealPackageSources.GetEnumerator()) {
        $target = Join-Path $releaseRoot ($entry.Key -replace '/', '\')
        [IO.File]::Copy((Join-Path $script:SourceRoot $entry.Value), $target, $false)
    }
    foreach ($entry in $literals.GetEnumerator()) {
        Write-NewTestText (Join-Path $releaseRoot ($entry.Key -replace '/', '\')) $entry.Value
    }
    foreach ($packagePath in (@($RealPackageSources.Keys) + @($literals.Keys))) {
        $target = Join-Path $releaseRoot ($packagePath -replace '/', '\')
        $bytes = [IO.File]::ReadAllBytes($target)
        $sha = Get-TestSha256Bytes $bytes
        $hashes[$packagePath] = $sha
        $files.Add([ordered]@{ path = $packagePath; sha256 = $sha; bytes = $bytes.Length
            provenance = [ordered]@{ kind = 'independent_test_fixture'; note = 'installation owner composition test package' } })
    }

    $manifest = [ordered]@{
        contract_version = 'm6.release.v1'
        built_at_utc     = '2026-09-17T00:00:00Z'
        source           = [ordered]@{ head = ('0' * 40); note = 'independent installation-owner test fixture; not a release build' }
        inputs           = [ordered]@{ capacity_config_sha256 = $hashes['api-context/capacity-config.json'] }
        projection       = [ordered]@{ api_device_profile = $script:DeclaredApiDeviceProfile
            compose_sha256 = $hashes['compose/mineru-windows.compose.yaml'] }
        api_build        = [ordered]@{ context = 'api-context'; capacity_config_sha256 = $hashes['api-context/capacity-config.json'] }
        native_m6        = [ordered]@{ note = 'not part of this independent fixture package' }
        installation     = [ordered]@{ installer_sha256 = $hashes['windows/install_mineru_fixed_api.ps1']
            collector_sha256 = $hashes['windows/collect_mineru_runtime.ps1'] }
        files            = @($files.ToArray())
    }
    $manifestPath = Join-Path $releaseRoot 'release-manifest.json'
    Write-NewTestText $manifestPath ($manifest | ConvertTo-Json -Compress -Depth 8)

    return [pscustomobject]@{
        Name = $Name; CaseDirectory = $caseDirectory; ReleaseRoot = $releaseRoot; ManifestPath = $manifestPath
        ManifestSha256 = (Get-TestSha256File $manifestPath); PackageSha256 = $hashes
        ComposeSha256 = $hashes['compose/mineru-windows.compose.yaml']
        CapacitySha256 = $hashes['api-context/capacity-config.json']
        SupervisorSourceSha256 = $hashes['windows/mineru_telemetry_job_supervisor.cs']
        TargetDirectory = $targetDirectory
        ComposeTarget = (Join-Path $targetDirectory 'compose.tailnet.yaml')
        ReceiptTarget = (Join-Path $targetDirectory 'install-receipt.json')
        EvidenceDirectory = $evidenceDirectory; DockerDirectory = $dockerDirectory
        PackageComposePath = (Join-Path $releaseRoot 'compose\mineru-windows.compose.yaml')
    }
}

function Write-InstallationBinding {
    # The two expected-identity fields are [object] on purpose: a [string] parameter would coerce a
    # declared null into an empty string, which the owner then refuses as a non-canonical hash.
    param([string]$Path, [long]$LifetimeMilliseconds, [long]$CleanupMilliseconds,
        [object]$ActiveComposeSha256 = $null, [object]$PreviousCapacitySha256 = $null)
    $document = [ordered]@{
        contract_version                 = 'm6.installation-binding.v1'
        expected_hostname                = [Environment]::MachineName
        api_device_profile               = $script:DeclaredApiDeviceProfile
        expected_active_compose_sha256   = $ActiveComposeSha256
        expected_previous_capacity_sha256 = $PreviousCapacitySha256
        job_lifetime_milliseconds        = $LifetimeMilliseconds
        job_cleanup_milliseconds         = $CleanupMilliseconds
        mac_exclusivity_receipt_sha256   = $script:MacExclusivityReceiptSha256
    }
    Write-NewTestText $Path ($document | ConvertTo-Json -Compress -Depth 4)
}

function Write-DockerPlan([string]$Path, [object[]]$Responses) {
    $lines = [Collections.Generic.List[string]]::new()
    $lines.Add('contract_version=mineru.test-fake-docker-plan.v1')
    foreach ($response in $Responses) {
        $lines.Add('---')
        $lines.Add('match=' + [string]$response['Match'])
        if ($response.ContainsKey('MaximumUses')) { $lines.Add('maximum_uses=' + [string]$response['MaximumUses']) }
        if ($response.ContainsKey('DelayMilliseconds')) { $lines.Add('delay_ms=' + [string]$response['DelayMilliseconds']) }
        if ($response.ContainsKey('ExitCode')) { $lines.Add('exit_code=' + [string]$response['ExitCode']) }
        if ($response.ContainsKey('StandardOutput')) {
            $lines.Add('stdout_base64=' + [Convert]::ToBase64String($Utf8NoThrow.GetBytes([string]$response['StandardOutput'])))
        }
        if ($response.ContainsKey('StandardError')) {
            $lines.Add('stderr_base64=' + [Convert]::ToBase64String($Utf8NoThrow.GetBytes([string]$response['StandardError'])))
        }
    }
    Write-NewTestText $Path (($lines.ToArray() -join "`r`n") + "`r`n")
}

function Write-InstallerPlan {
    param([string]$Path, [string]$EvidenceDirectory, [bool]$WriteAliveMarker = $false,
        [object[]]$AbsoluteWrites = @(), [object[]]$RecordWrites = @(),
        [int]$HangMilliseconds = 0, [string]$ThrowMessage = '', [int]$ExitCode = 0)
    # A [string] parameter coerces $null to an empty string, so the absence of a declared throw is
    # carried as an explicit JSON null rather than an empty message the fixture would then raise.
    $document = [ordered]@{
        contract_version   = 'mineru.test-fake-installer-plan.v1'
        evidence_directory = $EvidenceDirectory
        write_alive_marker = $WriteAliveMarker
        absolute_writes    = @($AbsoluteWrites)
        record_writes      = @($RecordWrites)
        hang_milliseconds  = $HangMilliseconds
        throw_message      = $(if ([string]::IsNullOrEmpty($ThrowMessage)) { $null } else { $ThrowMessage })
        exit_code          = $ExitCode
    }
    Write-NewTestText $Path ($document | ConvertTo-Json -Compress -Depth 6)
}

# ---- owner invocation ----------------------------------------------------------------------------
function ConvertTo-SingleQuoted([string]$Value) { return ($Value -replace "'", "''") }
function Start-Owner {
    param([string]$Name, [object]$Fixture, [string]$OperationDirectory, [string]$BindingPath,
        [string]$ExpectedManifestSha256, [string]$DockerPlanPath, [string]$DockerLogPath, [string]$InstallerPlanPath)
    # The owner runs through the same named-parameter entry the Mac driver uses. Only the test
    # adapter's console encoding is pinned, so a localized child diagnostic stays readable; the
    # owner's own parameter binding, records and exit code are untouched.
    $invocation = "& '" + (ConvertTo-SingleQuoted $script:Wrapper) + "'" +
        " -ReleaseRoot '" + (ConvertTo-SingleQuoted $Fixture.ReleaseRoot) + "'" +
        " -ExpectedManifestSha256 '" + (ConvertTo-SingleQuoted $ExpectedManifestSha256) + "'" +
        " -OperationDirectory '" + (ConvertTo-SingleQuoted $OperationDirectory) + "'" +
        " -InstallationBinding '" + (ConvertTo-SingleQuoted $BindingPath) + "'" +
        " -ComposeTarget '" + (ConvertTo-SingleQuoted $Fixture.ComposeTarget) + "'" +
        " -ReceiptTarget '" + (ConvertTo-SingleQuoted $Fixture.ReceiptTarget) + "'"
    # Best effort and visible if it fails: the authoritative record is operation-result.json, which
    # the owner writes with its own strict UTF-8 encoder, so console encoding only affects diagnostics.
    $preamble = 'try { [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false) } ' +
        'catch { [Console]::Error.Write(''test adapter could not select UTF8: '' + $_.Exception.Message) }'
    $command = @($preamble, $invocation, 'exit $LASTEXITCODE') -join "`n"
    $arguments = [string[]]@('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'RemoteSigned',
        '-EncodedCommand', [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command)))
    $env:PATH = $script:ChildPath
    $env:MINERU_TEST_FAKE_DOCKER_PLAN = $DockerPlanPath
    $env:MINERU_TEST_FAKE_DOCKER_LOG = $DockerLogPath
    $env:MINERU_TEST_FAKE_INSTALLER_PLAN = $InstallerPlanPath
    $process = [M6BoundedProcess]::new($script:PowerShellExe, $arguments, $Fixture.CaseDirectory,
        (Join-Path $Fixture.CaseDirectory ('owner-' + $Name)))
    return [pscustomobject]@{ Name = $Name; Process = $process; OperationDirectory = $OperationDirectory
        DockerLogPath = $DockerLogPath; Fixture = $Fixture }
}
function Complete-Owner([object]$Started, [int]$DeadlineMilliseconds) {
    $process = $Started.Process
    $process.Finish($DeadlineMilliseconds)
    $resultPath = Join-Path $Started.OperationDirectory 'operation-result.json'
    $result = $null
    if (Test-Path -LiteralPath $resultPath -PathType Leaf) { $result = Read-TestJson $resultPath }
    return [pscustomobject]@{
        Name = $Started.Name; ExitCode = $process.ExitCode; TestOwnerForced = $process.ForcedTermination
        ActiveJobProcesses = $process.ActiveJobProcesses; Result = $result
        DockerCalls = @(Read-DockerCalls $Started.DockerLogPath)
        Stdout = (Read-Diagnostic $process.StdoutPath); Stderr = (Read-Diagnostic $process.StderrPath)
    }
}
$script:CleanupFailures = [Collections.Generic.List[string]]::new()
function Close-Owner([object]$Started) {
    if ($null -eq $Started) { return }
    try { $Started.Process.Dispose() } catch { $script:CleanupFailures.Add($Started.Name + ': ' + $_.ToString()) }
}
function Assert-TestOwnerDidNotIntervene([object]$Completed) {
    Check (-not $Completed.TestOwnerForced) ($Completed.Name + ': the test owner had to terminate the installation owner; this is a test failure, never a product pass')
    Check ($Completed.ActiveJobProcesses -eq 0) ($Completed.Name + ': a descendant survived the installation owner')
}
function Assert-ClosedFailure([object]$Completed, [string]$ExpectedStatus) {
    $result = $Completed.Result
    Check ($null -ne $result) 'the installation owner must persist an operation result'
    Check ([string]$result.contract_version -ceq 'm6.installation-operation.v1') 'operation result contract drifted'
    Check ([string]$result.status -ceq $ExpectedStatus) ('operation status is ' + [string]$result.status + ', expected ' + $ExpectedStatus)
    Check (-not [bool]$result.installation_verified) 'a failed or unknown installation must never be reported as verified'
    Check ([string]$result.write_permission -ceq 'closed') 'write permission must stay closed'
    Check ((@($result.next_required) -join ',') -ceq 'read_only_recovery_judgement') 'a failed or unknown installation must require a read-only recovery judgement'
}
function Assert-RejectedBeforeMutation([object]$Completed, [object]$Started, [object]$Fixture, [string]$ExpectedError) {
    Check ($Completed.ExitCode -eq 65) ('expected identity exit 65, actual ' + $Completed.ExitCode)
    Assert-ClosedFailure $Completed 'failed'
    Check ([string]$Completed.Result.phase -ceq 'start') ('expected phase start, actual ' + [string]$Completed.Result.phase)
    Check ([string]$Completed.Result.first_error -match $ExpectedError) ('first error is [' + [string]$Completed.Result.first_error + '], expected ' + $ExpectedError)
    Check (-not (Test-Path -LiteralPath ($Fixture.ComposeTarget + '.installation.lock'))) 'the owner took the installation lock before the package identity was verified'
    Check (-not (Test-Path -LiteralPath (Join-Path $Started.OperationDirectory 'operation-start.json'))) 'the owner started an operation over an unverified package'
    Check (-not (Test-Path -LiteralPath (Join-Path $Started.OperationDirectory 'installer'))) 'the owner created installer records over an unverified package'
    Check ($Completed.DockerCalls.Count -eq 0) 'the owner reached Docker before the package identity was verified'
    Check (-not (Test-Path -LiteralPath $Fixture.ComposeTarget)) 'the deployment target was touched before the package identity was verified'
}

# ---- preparation -----------------------------------------------------------------------------
$SourceRoot = [IO.Path]::GetFullPath($SourceRoot)
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
foreach ($protected in @([Environment]::SystemDirectory, 'C:\ProgramData', 'C:\Windows', 'C:\Program Files')) {
    Check (-not $OutputRoot.StartsWith($protected, [StringComparison]::OrdinalIgnoreCase)) 'a disposable output root outside every protected location is required'
}
Check (Test-Path -LiteralPath $SourceRoot -PathType Container) ('source root missing: ' + $SourceRoot)
Check (-not (Test-Path -LiteralPath $OutputRoot)) ('output root must be new: ' + $OutputRoot)
[void][IO.Directory]::CreateDirectory($OutputRoot)
$CasesRoot = Join-Path $OutputRoot 'cases'
[void][IO.Directory]::CreateDirectory($CasesRoot)
foreach ($name in @('run_mineru_installation.ps1', 'build_mineru_telemetry_assembly.ps1', 'load_mineru_telemetry_assembly.ps1',
    'collect_mineru_runtime.ps1', 'mineru_nvml_backend.cs', 'mineru_resident_wire.cs', 'mineru_telemetry_job_supervisor.cs',
    'test_mineru_m6_process.cs', 'test_mineru_fake_docker.cs', 'test_mineru_fake_installer.ps1')) {
    Check (Test-Path -LiteralPath (Join-Path $SourceRoot $name) -PathType Leaf) ('required input missing under the source root: ' + $name)
}
$Wrapper = Join-Path $SourceRoot 'run_mineru_installation.ps1'
$WrapperSha256 = Get-TestSha256File $Wrapper
if ($ExpectedWrapperSha256 -ne '') {
    Check ($ExpectedWrapperSha256 -cmatch '^sha256:[0-9a-f]{64}$') 'the expected installation owner hash must be canonical'
    Check ($WrapperSha256 -ceq $ExpectedWrapperSha256) ('installation owner identity drifted: ' + $WrapperSha256)
}
# The tracked preparation recipe refuses a source outside 1..65536 bytes; say so before csc does.
foreach ($name in @('mineru_nvml_backend.cs', 'mineru_resident_wire.cs', 'mineru_telemetry_job_supervisor.cs')) {
    $length = (Get-Item -LiteralPath (Join-Path $SourceRoot $name)).Length
    Check ($length -ge 1 -and $length -le 65536) ($name + ' is outside the telemetry preparation byte bound: ' + $length)
}
$PowerShellExe = Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
Add-Type -Path (Join-Path $SourceRoot 'test_mineru_m6_process.cs') -ErrorAction Stop
$FakeDockerDirectory = Join-Path $OutputRoot 'fake docker'
[void][IO.Directory]::CreateDirectory($FakeDockerDirectory)
$FakeDockerPath = Join-Path $FakeDockerDirectory 'docker.exe'
Add-Type -Path (Join-Path $SourceRoot 'test_mineru_fake_docker.cs') -OutputAssembly $FakeDockerPath -OutputType ConsoleApplication -ErrorAction Stop
Check (Test-Path -LiteralPath $FakeDockerPath -PathType Leaf) 'the fake Docker CLI fixture was not produced'
# A closed child PATH: the only docker.exe the owner can resolve is this fixture, and a missing
# fixture fails visibly instead of falling through to a real Docker installation.
$ChildPath = @($FakeDockerDirectory, [Environment]::SystemDirectory, (Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0')) -join ';'
$VisibleDocker = @()
foreach ($entry in $ChildPath.Split(';')) {
    $candidate = Join-Path $entry 'docker.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $VisibleDocker += $candidate }
}
Check ($VisibleDocker.Count -eq 1 -and $VisibleDocker[0] -ceq $FakeDockerPath) ('the child PATH must expose exactly the fixture docker.exe; actual: ' + ($VisibleDocker -join ','))
$OriginalPath = $env:PATH

$Results = [Collections.Generic.List[object]]::new()
function Invoke-Case([string]$Name, [scriptblock]$Body) {
    if ($script:CaseFilter -ne '' -and $script:CaseFilter -cne $Name) { return }
    $script:CleanupFailures.Clear()
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $row = [ordered]@{ case = $Name; status = 'fail'; elapsed_ms = 0; detail = $null; error = $null; cleanup_failures = @() }
    try {
        $emitted = @(& $Body)
        # A stray pipeline object is a defect in this test, not a product result: name the types
        # instead of letting an unexpected extra object be read as the case evidence.
        Check ($emitted.Count -eq 1) ('case body emitted ' + $emitted.Count + ' objects: ' +
            ((@($emitted | ForEach-Object { if ($null -eq $_) { 'null' } else { $_.GetType().FullName } })) -join ','))
        $row.detail = $emitted[0]
        $row.status = 'pass'
    } catch { $row.error = $_.ToString() }
    if ($script:CleanupFailures.Count -ne 0) {
        $row.cleanup_failures = @($script:CleanupFailures.ToArray())
        $row.status = 'fail'
    }
    $row.elapsed_ms = $watch.ElapsedMilliseconds
    $script:Results.Add([pscustomobject]$row)
    Write-Host ($row.status.ToUpperInvariant() + ' ' + $Name + ' (' + $row.elapsed_ms + ' ms)')
    if ($null -ne $row.error) { Write-Host ('    ' + $row.error) }
}

# ---- cases -----------------------------------------------------------------------------------

# Pro R20 section 2.6: "父进程从manifest pin到真实Job关闭持有目标Compose对应的Windows独占文件handle,
# 防止两个官方installer并行."
Invoke-Case 'two_official_owners_compete_for_one_target_lock' {
    $fixture = New-ReleaseFixture 'competition'
    $bindingFirst = Join-Path $fixture.CaseDirectory 'binding-first.json'
    $bindingSecond = Join-Path $fixture.CaseDirectory 'binding-second.json'
    Write-InstallationBinding $bindingFirst $JobLifetimeMilliseconds $JobCleanupMilliseconds $null $null
    Write-InstallationBinding $bindingSecond $JobLifetimeMilliseconds $JobCleanupMilliseconds $null $null
    $planFirst = Join-Path $fixture.DockerDirectory 'plan-first.txt'
    $logFirst = Join-Path $fixture.DockerDirectory 'calls-first.log'
    $planSecond = Join-Path $fixture.DockerDirectory 'plan-second.txt'
    $logSecond = Join-Path $fixture.DockerDirectory 'calls-second.log'
    # The first owner holds the target lock across a declared 20 s health readback and is then
    # refused by a declared non-idle API: it never builds, never starts an installer, never mutates.
    Write-DockerPlan $planFirst @(@{ Match = 'urllib.request'; DelayMilliseconds = $LockHoldMilliseconds; StandardOutput = $LegacyBusyHealth })
    Write-DockerPlan $planSecond @(@{ Match = 'urllib.request'; StandardOutput = $LegacyIdleHealth })
    $installerPlan = Join-Path $fixture.CaseDirectory 'installer-plan.json'
    Write-InstallerPlan -Path $installerPlan -EvidenceDirectory $fixture.EvidenceDirectory -ExitCode 0
    $operationFirst = Join-Path $fixture.CaseDirectory 'operation first'
    $operationSecond = Join-Path $fixture.CaseDirectory 'operation second'
    $first = $null; $second = $null; $completedFirst = $null; $completedSecond = $null
    try {
        $first = Start-Owner 'first' $fixture $operationFirst $bindingFirst $fixture.ManifestSha256 $planFirst $logFirst $installerPlan
        # Reaching Docker at all is only possible after the exclusive lock was taken.
        Wait-ForDockerCalls $logFirst 1 $ShortDeadlineMilliseconds
        $second = Start-Owner 'second' $fixture $operationSecond $bindingSecond $fixture.ManifestSha256 $planSecond $logSecond $installerPlan
        $completedSecond = Complete-Owner $second $ShortDeadlineMilliseconds
        $completedFirst = Complete-Owner $first $ShortDeadlineMilliseconds
    } finally { Close-Owner $second; Close-Owner $first }
    Assert-TestOwnerDidNotIntervene $completedSecond
    Assert-TestOwnerDidNotIntervene $completedFirst
    Check ($completedSecond.ExitCode -eq 70) ('the second official owner must fail, actual exit ' + $completedSecond.ExitCode)
    Assert-ClosedFailure $completedSecond 'failed'
    Check ([string]$completedSecond.Result.phase -ceq 'start') ('the refused owner reported phase ' + [string]$completedSecond.Result.phase)
    Check ([string]$completedSecond.Result.first_error -match 'installation lock is held by another owner') ('first error: ' + [string]$completedSecond.Result.first_error)
    Check (-not (Test-Path -LiteralPath (Join-Path $operationSecond 'operation-start.json'))) 'the refused owner started an operation'
    Check (-not (Test-Path -LiteralPath (Join-Path $operationSecond 'installer'))) 'the refused owner created installer records'
    Check ($completedSecond.DockerCalls.Count -eq 0) 'the refused owner reached Docker'
    Check ($completedFirst.ExitCode -eq 65) ('the first owner must be refused by the declared non-idle API, actual exit ' + $completedFirst.ExitCode)
    Assert-ClosedFailure $completedFirst 'failed'
    Check ([string]$completedFirst.Result.phase -ceq 'locked') ('the first owner did not hold the lock; phase ' + [string]$completedFirst.Result.phase)
    Check ([string]$completedFirst.Result.first_error -match 'not healthy and idle') ('first error: ' + [string]$completedFirst.Result.first_error)
    Check ($completedFirst.DockerCalls.Count -eq 1) ('the first owner made ' + $completedFirst.DockerCalls.Count + ' Docker calls')
    Check (-not (Test-Path -LiteralPath (Join-Path $operationFirst 'operation-start.json'))) 'a refused precondition must not start the installer Job'
    Check (-not (Test-Path -LiteralPath $fixture.ComposeTarget)) 'the deployment target was written under a refused precondition'
    Check (Assert-LockReleased $fixture.ComposeTarget $QuiesceDeadlineMilliseconds) 'the installation lock must exist and be releasable once both owners exit'
    [pscustomobject]@{ first_exit = $completedFirst.ExitCode; first_phase = [string]$completedFirst.Result.phase
        second_exit = $completedSecond.ExitCode; second_first_error = [string]$completedSecond.Result.first_error
        second_docker_calls = $completedSecond.DockerCalls.Count }
}

# Controlled early exit: no result record, so the owner has no evidence of an installation.
# Also pins the owner's installer command construction (bare parameter names, quoted values) and
# the source-only telemetry supervisor build.
Invoke-Case 'controlled_installer_early_exit_leaves_no_verified_result' {
    $fixture = New-ReleaseFixture 'installer-early-exit'
    $binding = Join-Path $fixture.CaseDirectory 'binding.json'
    Write-InstallationBinding $binding $JobLifetimeMilliseconds $JobCleanupMilliseconds $null $null
    $plan = Join-Path $fixture.DockerDirectory 'plan.txt'
    $log = Join-Path $fixture.DockerDirectory 'calls.log'
    Write-DockerPlan $plan @(@{ Match = 'urllib.request'; StandardOutput = $LegacyIdleHealth })
    $installerPlan = Join-Path $fixture.CaseDirectory 'installer-plan.json'
    Write-InstallerPlan -Path $installerPlan -EvidenceDirectory $fixture.EvidenceDirectory -WriteAliveMarker $true -ExitCode 3
    $operation = Join-Path $fixture.CaseDirectory 'operation one'
    $started = $null; $completed = $null
    try {
        $started = Start-Owner 'early-exit' $fixture $operation $binding $fixture.ManifestSha256 $plan $log $installerPlan
        $completed = Complete-Owner $started $OwnerDeadlineMilliseconds
    } finally { Close-Owner $started }
    Assert-TestOwnerDidNotIntervene $completed
    Check ($completed.ExitCode -eq 70) ('expected exit 70, actual ' + $completed.ExitCode)
    Assert-ClosedFailure $completed 'failed'
    $result = $completed.Result
    Check ([string]$result.phase -ceq 'job_finished') ('expected phase job_finished, actual ' + [string]$result.phase)
    Check ([string]$result.first_error -match 'installer left no result record \(child exit 3\)') ('first error: ' + [string]$result.first_error)
    Check ([int]$result.installer_child_exit_code -eq 3) ('child exit is ' + [string]$result.installer_child_exit_code)
    Check ($null -eq $result.installer_result_status) 'no installer result status can exist without a result record'
    Check (-not [bool]$result.job_accounting.forced_termination) 'a child that exited on its own must not be recorded as forced'
    Check ([int]$result.job_accounting.job_active_processes -eq 0) 'the installer Job still reports live members'
    Check ([string]$result.job_accounting.supervisor_source_sha256 -ceq $fixture.SupervisorSourceSha256) 'the Job supervisor was not the packaged source'
    Check (-not (Test-Path -LiteralPath (Join-Path $operation 'installer\installer-result.json'))) 'the declared early exit must not leave a result record'
    Check (-not (Test-Path -LiteralPath $fixture.ComposeTarget)) 'the deployment target was written by a child that exited early'
    Check (-not (Test-Path -LiteralPath $fixture.ReceiptTarget)) 'an install receipt was written by a child that exited early'
    Check ($completed.DockerCalls.Count -eq 1) ('expected exactly the pre-mutation readback, actual ' + $completed.DockerCalls.Count + ' Docker calls')
    Assert-ArgumentsEqual $completed.DockerCalls[0].Arguments $ExpectedHealthArguments 'pre-mutation health'

    # The exact Job child the owner accounted for is the child that actually ran.
    $alive = Read-TestJson (Join-Path $fixture.EvidenceDirectory 'installer-alive.json')
    Check ([int]$alive.pid -eq [int]$result.job_accounting.child_pid) 'Job accounting names a different child process'
    Check ([long]$alive.creation_filetime_100ns -eq [long]$result.job_accounting.child_creation_filetime_100ns) 'Job accounting names a different child birth'

    # Command construction: bare parameter names, quoted values, no legacy capacity parameters.
    Check ($fixture.ReleaseRoot.Contains(' ')) 'this case must exercise a package path containing a space'
    $expectedArguments = @(
        '-ComposeSource', (Join-Path $fixture.ReleaseRoot 'compose\mineru-windows.compose.yaml'),
        '-CollectorSource', (Join-Path $fixture.ReleaseRoot 'windows\collect_mineru_runtime.ps1'),
        '-CompatDockerfileSource', (Join-Path $fixture.ReleaseRoot 'api-context\Dockerfile'),
        '-CompatPatcherSource', (Join-Path $fixture.ReleaseRoot 'api-context\patch_mineru_344.py'),
        '-CapacityConfigSource', (Join-Path $fixture.ReleaseRoot 'api-context\capacity-config.json'),
        '-ExpectedCapacityConfigSha256', $fixture.CapacitySha256,
        '-ApiOnlyCompatibilityUpgrade',
        '-ApiDeviceProfile', $DeclaredApiDeviceProfile,
        '-OperationRecordDirectory', (Join-Path $operation 'installer'),
        '-OperationBudgetSeconds', ([string]$ExpectedOperationBudgetSeconds))
    $start = Read-TestJson (Join-Path $operation 'operation-start.json')
    Assert-ArgumentsEqual @($start.installer_arguments) $expectedArguments 'declared installer'
    Check ([string]$start.release_manifest_sha256 -ceq $fixture.ManifestSha256) 'the operation start record does not bind the release manifest'
    Check ([string]$start.mac_exclusivity_receipt_sha256 -ceq $MacExclusivityReceiptSha256) 'the operation start record does not bind the Mac exclusivity receipt'
    Check ([long]$start.job_lifetime_milliseconds -eq $JobLifetimeMilliseconds) 'the declared Job lifetime was not used'

    $bound = Read-TestJson (Join-Path $fixture.EvidenceDirectory 'installer-bound-parameters.json')
    Check (((@($bound.bound_parameter_names) | Sort-Object) -join ',') -ceq (($ExpectedInstallerParameterNames | Sort-Object) -join ',')) (
        'the installer bound [' + ((@($bound.bound_parameter_names)) -join ',') + ']')
    Check (@($bound.remaining_arguments).Count -eq 0) ('the owner passed unbound arguments: ' + ((@($bound.remaining_arguments)) -join ','))
    Check ([string]$bound.bound_parameters.ComposeSource -ceq $expectedArguments[1]) 'ComposeSource did not bind to the packaged compose'
    Check ([string]$bound.bound_parameters.CollectorSource -ceq $expectedArguments[3]) 'CollectorSource did not bind to the packaged collector'
    Check ([string]$bound.bound_parameters.CompatDockerfileSource -ceq $expectedArguments[5]) 'CompatDockerfileSource did not bind'
    Check ([string]$bound.bound_parameters.CompatPatcherSource -ceq $expectedArguments[7]) 'CompatPatcherSource did not bind'
    Check ([string]$bound.bound_parameters.CapacityConfigSource -ceq $expectedArguments[9]) 'CapacityConfigSource did not bind'
    Check ([string]$bound.bound_parameters.ExpectedCapacityConfigSha256 -ceq $fixture.CapacitySha256) 'the expected capacity identity did not bind'
    Check ([bool]$bound.bound_parameters.ApiOnlyCompatibilityUpgrade) 'the API-only compatibility upgrade did not bind as a switch'
    Check ([string]$bound.bound_parameters.ApiDeviceProfile -ceq $DeclaredApiDeviceProfile) 'the device profile did not bind'
    Check ([string]$bound.bound_parameters.OperationRecordDirectory -ceq (Join-Path $operation 'installer')) 'the operation record directory did not bind'
    Check ([int]$bound.bound_parameters.OperationBudgetSeconds -eq $ExpectedOperationBudgetSeconds) 'the operation budget did not bind'

    # Source-only supervisor: compiled from the packaged production sources, no external assembly.
    $prepared = @(Get-ChildItem -LiteralPath $operation -Directory -Filter 'telemetry-assembly-*')
    Check ($prepared.Count -eq 1) ('expected exactly one prepared telemetry assembly directory, actual ' + $prepared.Count)
    $preparedManifestPath = Join-Path $prepared[0].FullName 'manifest.json'
    $preparedManifest = Read-TestJson $preparedManifestPath
    Check ([string]$start.supervisor_manifest_sha256 -ceq (Get-TestSha256File $preparedManifestPath)) 'the operation start record does not bind the prepared assembly manifest'
    $expectedSources = @{ 'mineru_nvml_backend.cs' = $fixture.PackageSha256['windows/mineru_nvml_backend.cs']
        'mineru_resident_wire.cs' = $fixture.PackageSha256['windows/mineru_resident_wire.cs']
        'mineru_telemetry_job_supervisor.cs' = $fixture.PackageSha256['windows/mineru_telemetry_job_supervisor.cs'] }
    Check (@($preparedManifest.sources).Count -eq 3) 'the prepared assembly did not record exactly three sources'
    foreach ($source in @($preparedManifest.sources)) {
        Check ($expectedSources.ContainsKey([string]$source.name)) ('unexpected compiled source: ' + [string]$source.name)
        Check ([string]$source.sha256 -ceq $expectedSources[[string]$source.name]) ('compiled source ' + [string]$source.name + ' is not the packaged source')
    }
    $assemblies = @(Get-ChildItem -LiteralPath $fixture.CaseDirectory -Recurse -File -Filter '*.dll')
    Check ($assemblies.Count -eq 1 -and $assemblies[0].Name -ceq 'mineru-telemetry.dll') (
        'the source-only package must produce exactly the freshly built assembly, actual: ' + ((@($assemblies | ForEach-Object { $_.Name })) -join ','))
    [pscustomobject]@{ exit_code = $completed.ExitCode; child_exit_code = [int]$result.installer_child_exit_code
        first_error = [string]$result.first_error; installer_arguments = @($start.installer_arguments)
        prepared_assembly_sha256 = [string]$preparedManifest.assembly_sha256 }
}

# The owner's Job is created with KILL_ON_CLOSE and holds the installer subtree: when the owner
# process disappears, the exact child Job closes and no stale lock survives.
Invoke-Case 'parent_disappears_and_the_exact_child_job_closes' {
    $fixture = New-ReleaseFixture 'parent-loss'
    $binding = Join-Path $fixture.CaseDirectory 'binding.json'
    Write-InstallationBinding $binding $JobLifetimeMilliseconds $JobCleanupMilliseconds $null $null
    $plan = Join-Path $fixture.DockerDirectory 'plan.txt'
    $log = Join-Path $fixture.DockerDirectory 'calls.log'
    Write-DockerPlan $plan @(@{ Match = 'urllib.request'; StandardOutput = $LegacyIdleHealth })
    $installerPlan = Join-Path $fixture.CaseDirectory 'installer-plan.json'
    Write-InstallerPlan -Path $installerPlan -EvidenceDirectory $fixture.EvidenceDirectory -WriteAliveMarker $true -HangMilliseconds $HangMilliseconds
    $operation = Join-Path $fixture.CaseDirectory 'operation one'
    $started = $null; $childPid = 0; $childCreation = 0; $ownerExit = $null; $ownerForced = $false
    try {
        $started = Start-Owner 'parent-loss' $fixture $operation $binding $fixture.ManifestSha256 $plan $log $installerPlan
        $alive = Wait-ForJson (Join-Path $fixture.EvidenceDirectory 'installer-alive.json') $MarkerDeadlineMilliseconds 'the installer Job child marker'
        $childPid = [int]$alive.pid
        $childCreation = [long]$alive.creation_filetime_100ns
        Check (Test-ExactProcessAlive $childPid $childCreation) 'the declared Job child was not running before the injected parent loss'
        Check (-not $started.Process.ExactProcessExited) 'the installation owner exited before the injected parent loss'
        # Exact handle, not a PID lookup: this test owns the process it terminates.
        $started.Process.CrashExactOwner()
        $watch = [Diagnostics.Stopwatch]::StartNew()
        while ($started.Process.ActiveJobProcesses -ne 0) {
            Check ($watch.ElapsedMilliseconds -lt $QuiesceDeadlineMilliseconds) 'the exact child Job did not close when its parent disappeared'
            Start-Sleep -Milliseconds 100
        }
        $ownerExit = $started.Process.ExitCode
        $ownerForced = $started.Process.ForcedTermination
    } finally { Close-Owner $started }
    Check ($ownerExit -eq 137) ('the owner did not exit through the injected parent loss; exit ' + $ownerExit)
    Check ($ownerForced) 'the injected parent loss was not recorded by the test owner'
    Check (-not (Test-ExactProcessAlive $childPid $childCreation)) 'the exact installer child outlived its parent'
    Check (Test-Path -LiteralPath (Join-Path $operation 'operation-start.json')) 'the owner must have started the Job before the injected parent loss'
    Check (-not (Test-Path -LiteralPath (Join-Path $operation 'job-accounting.json'))) 'a terminated owner cannot produce Job accounting'
    Check (-not (Test-Path -LiteralPath (Join-Path $operation 'operation-result.json'))) 'a terminated owner cannot produce an operation result'
    Check (-not (Test-Path -LiteralPath (Join-Path $operation 'installer\installer-result.json'))) 'the closed child must not have produced a result record'
    Check (-not (Test-Path -LiteralPath $fixture.ComposeTarget)) 'the deployment target was written before the injected parent loss'
    Check (-not (Test-Path -LiteralPath $fixture.ReceiptTarget)) 'an install receipt was written before the injected parent loss'
    Check (Assert-LockReleased $fixture.ComposeTarget $QuiesceDeadlineMilliseconds) 'the installation lock outlived the owner process'
    [pscustomobject]@{ owner_exit_code = $ownerExit; child_pid = $childPid; child_creation_filetime_100ns = $childCreation
        child_alive_after_parent_loss = (Test-ExactProcessAlive $childPid $childCreation) }
}

# Pro R20 section 2.6: a transport deadline over a request the daemon already accepted leaves the
# outcome unknown; writes stay closed and the owner never rolls anything back.
Invoke-Case 'daemon_mutation_then_child_timeout_stays_unknown_without_rollback' {
    $fixture = New-ReleaseFixture 'child-unknown'
    $binding = Join-Path $fixture.CaseDirectory 'binding.json'
    Write-InstallationBinding $binding $JobLifetimeMilliseconds $JobCleanupMilliseconds $null $null
    $plan = Join-Path $fixture.DockerDirectory 'plan.txt'
    $log = Join-Path $fixture.DockerDirectory 'calls.log'
    Write-DockerPlan $plan @(@{ Match = 'urllib.request'; StandardOutput = $LegacyIdleHealth })
    $unknownError = 'native_outcome_unknown: docker compose --project-name mineru up --detach exceeded the remaining operation budget'
    $installerPlan = Join-Path $fixture.CaseDirectory 'installer-plan.json'
    Write-InstallerPlan -Path $installerPlan -EvidenceDirectory $fixture.EvidenceDirectory -WriteAliveMarker $true `
        -AbsoluteWrites @([ordered]@{ path = $fixture.ComposeTarget; copy_from = $fixture.PackageComposePath }) `
        -RecordWrites @(
            [ordered]@{ name = 'installer-first-error.json'; text = ('{"phase":"failed","first_error":"' + $unknownError + '","mutation_started":true,"deployment_attempted":true,"daemon_side_outcome":"unknown"}') },
            [ordered]@{ name = 'installer-result.json'; text = ('{"phase":"unknown","status":"unknown","first_error":"' + $unknownError + '","rollback":"not_attempted"}') }) `
        -ThrowMessage ('installation outcome unknown; rollback not attempted because the daemon-side result is undetermined: ' + $unknownError)
    $operation = Join-Path $fixture.CaseDirectory 'operation one'
    $started = $null; $completed = $null
    try {
        $started = Start-Owner 'child-unknown' $fixture $operation $binding $fixture.ManifestSha256 $plan $log $installerPlan
        $completed = Complete-Owner $started $OwnerDeadlineMilliseconds
    } finally { Close-Owner $started }
    Assert-TestOwnerDidNotIntervene $completed
    Check ($completed.ExitCode -eq 70) ('expected exit 70, actual ' + $completed.ExitCode)
    Assert-ClosedFailure $completed 'unknown'
    $result = $completed.Result
    Check ([string]$result.daemon_side_outcome -ceq 'unknown') ('daemon side outcome is ' + [string]$result.daemon_side_outcome)
    Check ([string]$result.installer_result_status -ceq 'unknown') ('installer result status is ' + [string]$result.installer_result_status)
    Check ([string]$result.first_error -match 'installer reported unknown') ('first error: ' + [string]$result.first_error)
    Check ([string]$result.first_error -match 'native_outcome_unknown:') 'the original transport failure was not preserved in the owner first error'
    Check ([int]$result.installer_child_exit_code -ne 0) 'a child that threw must not report a zero exit'
    Check (-not [bool]$result.job_accounting.forced_termination) 'a child that exited on its own must not be recorded as forced'
    Check ([int]$result.job_accounting.job_active_processes -eq 0) 'the installer Job still reports live members'
    # No automatic rollback: the change the daemon already accepted is still in place, and the
    # owner issued no Docker command of its own after the Job finished.
    Check (Test-Path -LiteralPath $fixture.ComposeTarget -PathType Leaf) 'the daemon-side change was silently undone'
    Check ((Get-TestSha256File $fixture.ComposeTarget) -ceq $fixture.ComposeSha256) 'the deployment target no longer holds the change the daemon accepted'
    Check ($completed.DockerCalls.Count -eq 1) ('the owner issued ' + $completed.DockerCalls.Count + ' Docker calls; only the pre-mutation readback is allowed')
    Check (-not (Test-Path -LiteralPath $fixture.ReceiptTarget)) 'an install receipt exists for an undetermined installation'
    $childRecord = Read-TestJson (Join-Path $operation 'installer\installer-result.json')
    Check ([string]$childRecord.rollback -ceq 'not_attempted') 'the child record no longer states that rollback was not attempted'
    Check ([string]$childRecord.first_error -ceq $unknownError) 'the owner rewrote the child original error record'
    [pscustomobject]@{ exit_code = $completed.ExitCode; status = [string]$result.status
        first_error = [string]$result.first_error; docker_calls = $completed.DockerCalls.Count
        compose_target_sha256 = (Get-TestSha256File $fixture.ComposeTarget) }
}

# The owner's own Job lifetime expires while the daemon-side request is already in flight. The Job
# can only end the Windows CLI subtree, so the outcome is unknown by construction.
Invoke-Case 'daemon_mutation_then_job_lifetime_forced_close_stays_unknown' {
    $fixture = New-ReleaseFixture 'job-forced'
    $binding = Join-Path $fixture.CaseDirectory 'binding.json'
    Write-InstallationBinding $binding $ForcedJobLifetimeMilliseconds $JobCleanupMilliseconds $null $null
    $plan = Join-Path $fixture.DockerDirectory 'plan.txt'
    $log = Join-Path $fixture.DockerDirectory 'calls.log'
    Write-DockerPlan $plan @(@{ Match = 'urllib.request'; StandardOutput = $LegacyIdleHealth })
    $installerPlan = Join-Path $fixture.CaseDirectory 'installer-plan.json'
    Write-InstallerPlan -Path $installerPlan -EvidenceDirectory $fixture.EvidenceDirectory -WriteAliveMarker $true `
        -AbsoluteWrites @([ordered]@{ path = $fixture.ComposeTarget; copy_from = $fixture.PackageComposePath }) `
        -HangMilliseconds $HangMilliseconds
    $operation = Join-Path $fixture.CaseDirectory 'operation one'
    $started = $null; $completed = $null
    try {
        $started = Start-Owner 'job-forced' $fixture $operation $binding $fixture.ManifestSha256 $plan $log $installerPlan
        $completed = Complete-Owner $started $ForcedDeadlineMilliseconds
    } finally { Close-Owner $started }
    Assert-TestOwnerDidNotIntervene $completed
    Check ($completed.ExitCode -eq 70) ('expected exit 70, actual ' + $completed.ExitCode)
    Assert-ClosedFailure $completed 'unknown'
    $result = $completed.Result
    Check ([string]$result.daemon_side_outcome -ceq 'unknown') ('daemon side outcome is ' + [string]$result.daemon_side_outcome)
    Check ([string]$result.first_error -match 'installer Job was forced to terminate after its lifetime') ('first error: ' + [string]$result.first_error)
    Check ([bool]$result.job_accounting.forced_termination) 'the forced Job close was not recorded'
    Check ([int]$result.job_accounting.job_active_processes -eq 0) 'the forced Job close left live members'
    Check ($null -eq $result.installer_result_status) 'a forced Job close cannot carry an installer result status'
    Check (-not (Test-Path -LiteralPath (Join-Path $operation 'installer\installer-result.json'))) 'the hung child must not have produced a result record'
    $alive = Read-TestJson (Join-Path $fixture.EvidenceDirectory 'installer-alive.json')
    Check ([int]$alive.pid -eq [int]$result.job_accounting.child_pid) 'Job accounting names a different child process'
    Check ([long]$alive.creation_filetime_100ns -eq [long]$result.job_accounting.child_creation_filetime_100ns) 'Job accounting names a different child birth'
    Check (-not (Test-ExactProcessAlive ([int]$alive.pid) ([long]$alive.creation_filetime_100ns))) 'the exact Windows CLI child survived the forced Job close'
    # The child budget is derived from the declared Job lifetime, here the product minimum.
    $bound = Read-TestJson (Join-Path $fixture.EvidenceDirectory 'installer-bound-parameters.json')
    Check ([int]$bound.bound_parameters.OperationBudgetSeconds -eq $ExpectedForcedOperationBudgetSeconds) (
        'the operation budget is ' + [string]$bound.bound_parameters.OperationBudgetSeconds + ', expected ' + $ExpectedForcedOperationBudgetSeconds)
    Check (Test-Path -LiteralPath $fixture.ComposeTarget -PathType Leaf) 'the daemon-side change was silently undone after the forced close'
    Check ((Get-TestSha256File $fixture.ComposeTarget) -ceq $fixture.ComposeSha256) 'the deployment target no longer holds the change the daemon accepted'
    Check ($completed.DockerCalls.Count -eq 1) ('the owner issued ' + $completed.DockerCalls.Count + ' Docker calls; a forced close must not start recovery')
    Check (Assert-LockReleased $fixture.ComposeTarget $QuiesceDeadlineMilliseconds) 'the installation lock outlived the owner'
    [pscustomobject]@{ exit_code = $completed.ExitCode; status = [string]$result.status
        forced_termination = [bool]$result.job_accounting.forced_termination
        child_exit_code = [int]$result.installer_child_exit_code; docker_calls = $completed.DockerCalls.Count }
}

# A rollback the installer refused because the registry witness changed stays a closed failure.
# The refusal guard itself is covered by scripts/windows/test_mineru_admission_rollback.ps1; what is
# proved here is that the owner surfaces it without adding recovery of its own.
Invoke-Case 'registry_witness_change_refusal_stays_failed_and_closed' {
    $fixture = New-ReleaseFixture 'rollback-refused'
    $binding = Join-Path $fixture.CaseDirectory 'binding.json'
    Write-InstallationBinding $binding $JobLifetimeMilliseconds $JobCleanupMilliseconds $null $null
    $plan = Join-Path $fixture.DockerDirectory 'plan.txt'
    $log = Join-Path $fixture.DockerDirectory 'calls.log'
    Write-DockerPlan $plan @(@{ Match = 'urllib.request'; StandardOutput = $LegacyIdleHealth })
    $registryRefusal = 'rollback_blocked_registry_changed: retained responsibilities or root identity changed'
    $originalFailure = 'docker compose --project-name mineru up --detach failed with exit code 1'
    $installerPlan = Join-Path $fixture.CaseDirectory 'installer-plan.json'
    Write-InstallerPlan -Path $installerPlan -EvidenceDirectory $fixture.EvidenceDirectory -WriteAliveMarker $true `
        -AbsoluteWrites @([ordered]@{ path = $fixture.ComposeTarget; copy_from = $fixture.PackageComposePath }) `
        -RecordWrites @(
            [ordered]@{ name = 'installer-phase-rollback-started.json'; text = '{"phase":"rollback_started"}' },
            [ordered]@{ name = 'installer-result.json'; text = ('{"phase":"rolled_back","status":"failed","first_error":"' + $originalFailure +
                '","rollback":"failed","rollback_error":"' + $registryRefusal + '"}') }) `
        -ThrowMessage ('installation failed: ' + $originalFailure + '; rollback also failed: ' + $registryRefusal)
    $operation = Join-Path $fixture.CaseDirectory 'operation one'
    $started = $null; $completed = $null
    try {
        $started = Start-Owner 'rollback-refused' $fixture $operation $binding $fixture.ManifestSha256 $plan $log $installerPlan
        $completed = Complete-Owner $started $OwnerDeadlineMilliseconds
    } finally { Close-Owner $started }
    Assert-TestOwnerDidNotIntervene $completed
    Check ($completed.ExitCode -eq 70) ('expected exit 70, actual ' + $completed.ExitCode)
    Assert-ClosedFailure $completed 'failed'
    $result = $completed.Result
    Check ([string]$result.installer_result_status -ceq 'failed') ('installer result status is ' + [string]$result.installer_result_status)
    Check ([string]$result.first_error -match 'installer reported failed') ('first error: ' + [string]$result.first_error)
    Check ([string]$result.first_error -match ([regex]::Escape($originalFailure))) 'the original deployment failure was not preserved'
    Check ($completed.DockerCalls.Count -eq 1) ('the owner issued ' + $completed.DockerCalls.Count + ' Docker calls; a refused rollback must not start recovery')
    Check ((Get-TestSha256File $fixture.ComposeTarget) -ceq $fixture.ComposeSha256) 'the owner restored or removed the deployment target after a refused rollback'
    Check (-not (Test-Path -LiteralPath $fixture.ReceiptTarget)) 'an install receipt exists after a refused rollback'
    $childRecord = Read-TestJson (Join-Path $operation 'installer\installer-result.json')
    Check ([string]$childRecord.rollback_error -ceq $registryRefusal) 'the registry refusal evidence did not survive in the child record'
    Check ([string]$childRecord.phase -ceq 'rolled_back') 'the child rollback phase record was rewritten'
    [pscustomobject]@{ exit_code = $completed.ExitCode; status = [string]$result.status
        first_error = [string]$result.first_error; child_rollback_error = [string]$childRecord.rollback_error
        docker_calls = $completed.DockerCalls.Count }
}

# Manifest and packaged-source drift are refused on identity, before the lock, before Docker and
# before anything can be mutated.
Invoke-Case 'manifest_identity_drift_is_refused_before_any_mutation' {
    $fixture = New-ReleaseFixture 'manifest-drift'
    $binding = Join-Path $fixture.CaseDirectory 'binding.json'
    Write-InstallationBinding $binding $JobLifetimeMilliseconds $JobCleanupMilliseconds $null $null
    $plan = Join-Path $fixture.DockerDirectory 'plan.txt'
    $log = Join-Path $fixture.DockerDirectory 'calls.log'
    Write-DockerPlan $plan @(@{ Match = 'urllib.request'; StandardOutput = $LegacyIdleHealth })
    $installerPlan = Join-Path $fixture.CaseDirectory 'installer-plan.json'
    Write-InstallerPlan -Path $installerPlan -EvidenceDirectory $fixture.EvidenceDirectory -ExitCode 0
    # Still valid JSON: the refusal must come from the pinned hash, not from a parse error.
    $drifted = ($Utf8.GetString([IO.File]::ReadAllBytes($fixture.ManifestPath))).Replace('2026-09-17T00:00:00Z', '2026-09-17T00:00:01Z')
    [IO.File]::WriteAllBytes($fixture.ManifestPath, $Utf8NoThrow.GetBytes($drifted))
    Check ((Get-TestSha256File $fixture.ManifestPath) -cne $fixture.ManifestSha256) 'the drift fixture did not change the manifest bytes'
    $operation = Join-Path $fixture.CaseDirectory 'operation one'
    $started = $null; $completed = $null
    try {
        $started = Start-Owner 'manifest-drift' $fixture $operation $binding $fixture.ManifestSha256 $plan $log $installerPlan
        $completed = Complete-Owner $started $ShortDeadlineMilliseconds
    } finally { Close-Owner $started }
    Assert-TestOwnerDidNotIntervene $completed
    Assert-RejectedBeforeMutation $completed $started $fixture 'release manifest hash differs'
    [pscustomobject]@{ exit_code = $completed.ExitCode; first_error = [string]$completed.Result.first_error }
}

Invoke-Case 'packaged_supervisor_source_drift_is_refused_before_any_mutation' {
    $fixture = New-ReleaseFixture 'source-drift'
    $binding = Join-Path $fixture.CaseDirectory 'binding.json'
    Write-InstallationBinding $binding $JobLifetimeMilliseconds $JobCleanupMilliseconds $null $null
    $plan = Join-Path $fixture.DockerDirectory 'plan.txt'
    $log = Join-Path $fixture.DockerDirectory 'calls.log'
    Write-DockerPlan $plan @(@{ Match = 'urllib.request'; StandardOutput = $LegacyIdleHealth })
    $installerPlan = Join-Path $fixture.CaseDirectory 'installer-plan.json'
    Write-InstallerPlan -Path $installerPlan -EvidenceDirectory $fixture.EvidenceDirectory -ExitCode 0
    $supervisor = Join-Path $fixture.ReleaseRoot 'windows\mineru_telemetry_job_supervisor.cs'
    $bytes = [Collections.Generic.List[byte]]::new([IO.File]::ReadAllBytes($supervisor))
    $bytes.AddRange($Utf8NoThrow.GetBytes("`r`n// independent installation-owner drift marker`r`n"))
    [IO.File]::WriteAllBytes($supervisor, $bytes.ToArray())
    Check ((Get-TestSha256File $supervisor) -cne $fixture.PackageSha256['windows/mineru_telemetry_job_supervisor.cs']) 'the drift fixture did not change the packaged supervisor source'
    $operation = Join-Path $fixture.CaseDirectory 'operation one'
    $started = $null; $completed = $null
    try {
        $started = Start-Owner 'source-drift' $fixture $operation $binding $fixture.ManifestSha256 $plan $log $installerPlan
        $completed = Complete-Owner $started $ShortDeadlineMilliseconds
    } finally { Close-Owner $started }
    Assert-TestOwnerDidNotIntervene $completed
    Assert-RejectedBeforeMutation $completed $started $fixture 'release file bytes differ: windows/mineru_telemetry_job_supervisor\.cs'
    [pscustomobject]@{ exit_code = $completed.ExitCode; first_error = [string]$completed.Result.first_error }
}

# Static: the installation owner layer itself owns no recovery, restore or deployment mutation, and
# issues exactly the two declared read-only Docker readbacks. No child process is started here.
Invoke-Case 'installation_owner_declares_no_recovery_or_mutation_path' {
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($Wrapper, [ref]$tokens, [ref]$errors)
    Check (@($errors).Count -eq 0) ('the installation owner does not parse: ' + ($errors | Out-String))
    $commands = @($ast.FindAll({ param($node) $node -is [Management.Automation.Language.CommandAst] }, $true))
    $names = @()
    foreach ($command in $commands) {
        $name = $command.GetCommandName()
        if ($null -ne $name) { $names += $name }
    }
    foreach ($forbidden in $ForbiddenOwnerCommands) {
        Check (@($names | Where-Object { $_ -ieq $forbidden }).Count -eq 0) ('the installation owner must not call ' + $forbidden)
    }
    Check (@($names | Where-Object { $_ -ieq 'Import-MineruTelemetryPreparedAssembly' }).Count -eq 1) 'the installation owner no longer loads the prepared telemetry assembly'
    $readbacks = @($commands | Where-Object { $_.GetCommandName() -ieq 'Invoke-BoundedReadback' })
    Check ($readbacks.Count -ge 1) 'the installation owner no longer performs a bounded Docker readback'
    $subcommands = @()
    foreach ($readback in $readbacks) {
        $arrays = @($readback.FindAll({ param($node) $node -is [Management.Automation.Language.ArrayLiteralAst] }, $true))
        Check ($arrays.Count -ge 1) 'a bounded readback no longer passes a literal argument vector'
        $first = $arrays[0].Elements[0]
        Check ($first -is [Management.Automation.Language.StringConstantExpressionAst]) 'a bounded readback argument vector does not start with a literal subcommand'
        $subcommands += $first.Value
    }
    foreach ($subcommand in $subcommands) {
        Check ($subcommand -ceq 'exec' -or $subcommand -ceq 'inspect') ('the installation owner issues an undeclared Docker subcommand: ' + $subcommand)
    }
    $wrapperText = $Utf8.GetString([IO.File]::ReadAllBytes($Wrapper))
    Check (-not $wrapperText.Contains('installation-supervisor.dll')) 'the installation owner must not depend on a prebuilt supervisor assembly'
    [pscustomobject]@{ command_count = $commands.Count; readback_subcommands = @($subcommands)
        installation_owner_sha256 = $WrapperSha256 }
}

# Narrow regression for the independently reproduced empty-counter acceptance.
# Evaluate only the real function definitions in an isolated scope, never the
# owner top level; replace the native readback with exact test-authored wire.
Invoke-Case 'closed_idle_health_rejects_missing_and_coerced_evidence' {
    $valid = New-ExplicitCapacityHealth ('sha256:' + ('12' * 32))
    $variants = [ordered]@{
        empty_stage = $valid.Replace('"result_capacity_waiting":0,"parse_waiting":0,"parse_active":0,"finalizer_waiting":0,"finalizer_active":0', '')
        empty_http = $valid.Replace('"active_requests":0,"pending_requests":0', '')
        missing_stage = $valid.Replace('"parse_waiting":0,', '')
        extra_stage = $valid.Replace('"parse_active":0', '"extra":0,"parse_active":0')
        string_stage = $valid.Replace('"parse_active":0', '"parse_active":"0"')
        boolean_http = $valid.Replace('"active_requests":0', '"active_requests":false')
        fractional_http = $valid.Replace('"pending_requests":0', '"pending_requests":0.1')
        negative_http = $valid.Replace('"pending_requests":0', '"pending_requests":-1')
        missing_admission = $valid.Replace('"ingress_tasks":0,', '')
        string_admission = $valid.Replace('"durable_nonterminal_tasks":0', '"durable_nonterminal_tasks":"0"')
        string_open = $valid.Replace('"admission_open":true', '"admission_open":"false"')
        string_drain = $valid.Replace('"soft_drain_requested":false', '"soft_drain_requested":"false"')
        busy = $valid.Replace('"parse_active":0', '"parse_active":1')
        drifted_identity = $valid.Replace(('12' * 32), ('13' * 32))
    }
    $outcomes = & {
        param($Path, $ValidRaw, $Invalids)
        $tokens = $null; $errors = $null
        $ast = [Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
        if (@($errors).Count) { throw 'product owner function parse failed' }
        foreach ($node in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
            . ([ScriptBlock]::Create($node.Extent.Text))
        }
        function Invoke-BoundedReadback {
            param($FilePath, $Arguments)
            return [pscustomobject]@{ ExitCode = 0; StandardOutput = $script:IndependentIdleRaw; StandardError = '' }
        }
        $script:IndependentIdleRaw = $ValidRaw
        $positive = Read-IdleCapacityHealth 'declared-fake' ('sha256:' + ('12' * 32)) 'independent'
        if ($null -eq $positive) { throw 'valid idle proof was not returned' }
        foreach ($entry in $Invalids.GetEnumerator()) {
            $script:IndependentIdleRaw = $entry.Value
            $errorText = $null
            try { $null = Read-IdleCapacityHealth 'declared-fake' ('sha256:' + ('12' * 32)) 'independent' }
            catch { $errorText = $_.ToString() }
            if ($null -eq $errorText) { throw ('malformed idle proof accepted: ' + $entry.Key) }
            [pscustomobject]@{ variant = $entry.Key; refused = $true; original_error = $errorText }
        }
    } $Wrapper $valid $variants
    Check (@($outcomes).Count -eq 14) 'not all independently declared malformed cases were checked'
    [pscustomobject]@{ valid_idle_accepted = $true; invalid_variants = @($outcomes) }
}

# Positive control. Only when the child result, the persisted receipt, the compose target hash, the
# running image and a closed idle readback all agree is the installation verified -- and writer
# readiness still stays closed until qualification and binding.
Invoke-Case 'verified_installation_requires_every_record_to_agree' {
    $fixture = New-ReleaseFixture 'verified-pass'
    $binding = Join-Path $fixture.CaseDirectory 'binding.json'
    Write-InstallationBinding $binding $JobLifetimeMilliseconds $JobCleanupMilliseconds $null $null
    $plan = Join-Path $fixture.DockerDirectory 'plan.txt'
    $log = Join-Path $fixture.DockerDirectory 'calls.log'
    Write-DockerPlan $plan @(
        @{ Match = 'urllib.request'; MaximumUses = 1; StandardOutput = $LegacyIdleHealth },
        @{ Match = '{{.Image}}'; StandardOutput = ($DeclaredImageId + "`n") },
        @{ Match = 'urllib.request'; StandardOutput = (New-ExplicitCapacityHealth $fixture.CapacitySha256) })
    $receipt = [ordered]@{
        schema = 'mineru-windows-install-receipt.v2'; installed_at_utc = '2026-09-17T00:00:00.0000000Z'; success = $true
        compose_path = $fixture.ComposeTarget; compose_sha256 = $fixture.ComposeSha256
        collector_sha256 = $fixture.PackageSha256['windows/collect_mineru_runtime.ps1']
        api_compatibility_image = [ordered]@{ image_id = $DeclaredImageId }
    }
    $installerResult = '{"phase":"complete","status":"pass","first_error":null,"compose_sha256":"' + $fixture.ComposeSha256 +
        '","collector_sha256":"' + $fixture.PackageSha256['windows/collect_mineru_runtime.ps1'] +
        '","api_image_id":"' + $DeclaredImageId + '","receipt_sha256":"' + $DeclaredReceiptSha256 + '"}'
    $installerPlan = Join-Path $fixture.CaseDirectory 'installer-plan.json'
    Write-InstallerPlan -Path $installerPlan -EvidenceDirectory $fixture.EvidenceDirectory -WriteAliveMarker $true `
        -AbsoluteWrites @(
            [ordered]@{ path = $fixture.ComposeTarget; copy_from = $fixture.PackageComposePath },
            [ordered]@{ path = $fixture.ReceiptTarget; text = ($receipt | ConvertTo-Json -Compress -Depth 4) }) `
        -RecordWrites @([ordered]@{ name = 'installer-result.json'; text = $installerResult }) -ExitCode 0
    $operation = Join-Path $fixture.CaseDirectory 'operation one'
    $started = $null; $completed = $null
    try {
        $started = Start-Owner 'verified-pass' $fixture $operation $binding $fixture.ManifestSha256 $plan $log $installerPlan
        $completed = Complete-Owner $started $OwnerDeadlineMilliseconds
    } finally { Close-Owner $started }
    Assert-TestOwnerDidNotIntervene $completed
    Check ($completed.ExitCode -eq 0) ('expected exit 0, actual ' + $completed.ExitCode + '; first error: ' +
        $(if ($null -ne $completed.Result) { [string]$completed.Result.first_error } else { $completed.Stdout }))
    $result = $completed.Result
    Check ([string]$result.status -ceq 'pass') ('operation status is ' + [string]$result.status)
    Check ([string]$result.daemon_side_outcome -ceq 'verified') ('daemon side outcome is ' + [string]$result.daemon_side_outcome)
    Check ([bool]$result.installation_verified) 'an agreeing installation was not reported as verified'
    Check ((@($result.next_required) -join ',') -ceq 'qualify,bind') ('next required is ' + ((@($result.next_required)) -join ','))
    # Writer readiness is not granted by a successful installation.
    Check ([string]$result.write_permission -ceq 'closed') 'a verified installation must still leave write permission closed'
    Check ([string]$result.idle_proof_before -ceq 'legacy_gauges_only') ('idle proof before is ' + [string]$result.idle_proof_before)
    Check ([string]$result.compose_sha256 -ceq $fixture.ComposeSha256) 'the verified result does not bind the release compose'
    Check ([int]$result.installer_child_exit_code -eq 0) 'a verified installation requires a zero child exit'
    Check ([string]$result.installer_result_status -ceq 'pass') 'the child result status was not recorded'
    Check (-not [bool]$result.job_accounting.forced_termination) 'a verified installation cannot follow a forced Job close'
    Check ([int]$result.job_accounting.job_active_processes -eq 0) 'the installer Job still reports live members'
    Check ($completed.DockerCalls.Count -eq 3) ('expected three declared Docker readbacks, actual ' + $completed.DockerCalls.Count)
    Assert-ArgumentsEqual $completed.DockerCalls[0].Arguments $ExpectedHealthArguments 'pre-mutation health'
    Assert-ArgumentsEqual $completed.DockerCalls[1].Arguments $ExpectedInspectArguments 'image inspect'
    Assert-ArgumentsEqual $completed.DockerCalls[2].Arguments $ExpectedHealthArguments 'post-installation health'
    [pscustomobject]@{ exit_code = $completed.ExitCode; status = [string]$result.status
        installation_verified = [bool]$result.installation_verified; write_permission = [string]$result.write_permission
        next_required = @($result.next_required); docker_calls = $completed.DockerCalls.Count }
}

# The same agreeing records, except that the image actually running differs from the receipt: a
# child that exited zero and reported pass is still not a verified installation.
Invoke-Case 'pass_result_with_a_different_running_image_is_unknown' {
    $fixture = New-ReleaseFixture 'image-mismatch'
    $binding = Join-Path $fixture.CaseDirectory 'binding.json'
    Write-InstallationBinding $binding $JobLifetimeMilliseconds $JobCleanupMilliseconds $null $null
    $plan = Join-Path $fixture.DockerDirectory 'plan.txt'
    $log = Join-Path $fixture.DockerDirectory 'calls.log'
    Write-DockerPlan $plan @(
        @{ Match = 'urllib.request'; MaximumUses = 1; StandardOutput = $LegacyIdleHealth },
        @{ Match = '{{.Image}}'; StandardOutput = ($DeclaredOtherImageId + "`n") },
        @{ Match = 'urllib.request'; StandardOutput = (New-ExplicitCapacityHealth $fixture.CapacitySha256) })
    $receipt = [ordered]@{
        schema = 'mineru-windows-install-receipt.v2'; installed_at_utc = '2026-09-17T00:00:00.0000000Z'; success = $true
        compose_path = $fixture.ComposeTarget; compose_sha256 = $fixture.ComposeSha256
        collector_sha256 = $fixture.PackageSha256['windows/collect_mineru_runtime.ps1']
        api_compatibility_image = [ordered]@{ image_id = $DeclaredImageId }
    }
    $installerResult = '{"phase":"complete","status":"pass","first_error":null,"compose_sha256":"' + $fixture.ComposeSha256 +
        '","collector_sha256":"' + $fixture.PackageSha256['windows/collect_mineru_runtime.ps1'] +
        '","api_image_id":"' + $DeclaredImageId + '","receipt_sha256":"' + $DeclaredReceiptSha256 + '"}'
    $installerPlan = Join-Path $fixture.CaseDirectory 'installer-plan.json'
    Write-InstallerPlan -Path $installerPlan -EvidenceDirectory $fixture.EvidenceDirectory -WriteAliveMarker $true `
        -AbsoluteWrites @(
            [ordered]@{ path = $fixture.ComposeTarget; copy_from = $fixture.PackageComposePath },
            [ordered]@{ path = $fixture.ReceiptTarget; text = ($receipt | ConvertTo-Json -Compress -Depth 4) }) `
        -RecordWrites @([ordered]@{ name = 'installer-result.json'; text = $installerResult }) -ExitCode 0
    $operation = Join-Path $fixture.CaseDirectory 'operation one'
    $started = $null; $completed = $null
    try {
        $started = Start-Owner 'image-mismatch' $fixture $operation $binding $fixture.ManifestSha256 $plan $log $installerPlan
        $completed = Complete-Owner $started $OwnerDeadlineMilliseconds
    } finally { Close-Owner $started }
    Assert-TestOwnerDidNotIntervene $completed
    Check ($completed.ExitCode -eq 70) ('expected exit 70, actual ' + $completed.ExitCode)
    Assert-ClosedFailure $completed 'unknown'
    $result = $completed.Result
    Check ([string]$result.first_error -match 'running API image differs from the receipt') ('first error: ' + [string]$result.first_error)
    Check ([int]$result.installer_child_exit_code -eq 0) 'this case must refuse a child that exited zero'
    Check ([string]$result.installer_result_status -ceq 'pass') 'this case must refuse a child that reported pass'
    Check ($completed.DockerCalls.Count -eq 2) ('expected the health and inspect readbacks only, actual ' + $completed.DockerCalls.Count)
    Check ((Get-TestSha256File $fixture.ComposeTarget) -ceq $fixture.ComposeSha256) 'the owner undid the deployment target after refusing the readback'
    [pscustomobject]@{ exit_code = $completed.ExitCode; status = [string]$result.status
        first_error = [string]$result.first_error; child_exit_code = [int]$result.installer_child_exit_code
        installer_result_status = [string]$result.installer_result_status }
}

# ---- receipt ---------------------------------------------------------------------------------
$env:PATH = $OriginalPath
if ($CaseFilter -ne '' -and $Results.Count -eq 0) { throw ('the case filter matched no case: ' + $CaseFilter) }
Check ($Results.Count -gt 0) 'no case ran'
$Failed = @($Results | Where-Object { $_.status -cne 'pass' }).Count
$Final = [ordered]@{
    schema                     = 'independent-installation-owner-tests.v1'
    status                     = $(if ($Failed -eq 0) { 'pass' } else { 'fail' })
    hostname                   = [Environment]::MachineName
    finished_utc               = [DateTime]::UtcNow.ToString('o')
    installation_owner_sha256  = $WrapperSha256
    fake_docker_source_sha256  = (Get-TestSha256File (Join-Path $SourceRoot 'test_mineru_fake_docker.cs'))
    fake_installer_sha256      = (Get-TestSha256File (Join-Path $SourceRoot 'test_mineru_fake_installer.ps1'))
    job_supervisor_source_sha256 = (Get-TestSha256File (Join-Path $SourceRoot 'mineru_telemetry_job_supervisor.cs'))
    case_filter                = $CaseFilter
    failure_count              = $Failed
    cases                      = @($Results.ToArray())
}
$Json = $Final | ConvertTo-Json -Depth 12
Write-NewTestText (Join-Path $OutputRoot 'runner-evidence.json') $Json
$Json
if ($Failed -ne 0) { exit 1 }
exit 0
