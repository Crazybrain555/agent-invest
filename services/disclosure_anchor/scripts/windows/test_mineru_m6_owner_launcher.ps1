<#
.SYNOPSIS
Independent regression for the production M6 owner launcher on its R20 WP2
Prepare / Run / Cancel parameter sets. Windows PowerShell 5.1 x64 only.

.DESCRIPTION
Every case drives the real launcher as a real child process against a fresh
workspace. The official `-Prepare` set creates every protected private ancestor
before any secret exists; the staged deployment is only written into
`private\staging` afterwards, exactly as the controller would. No private
directory, ACL, `runs\<hash>\spec.json` or missing product state is ever
pre-created by this test, and no test ever repairs an ACL to make a case pass.

The controlled child is `test_mineru_m6_owner_launcher_fixture.cs` compiled
together with the twelve production native sources (`/main:LauncherFixture`), so
the launcher's pinned-assembly load resolves the real `MineruM6PrivateStore` ACL
helper and the real canonical wire. The resulting binary is a test binary: it is
built into the disposable output root, is never a production artefact, and the
official production builder (`build_mineru_m6_owner.ps1`) remains root's separate
subject. The fixture never starts the native owner host.

No GPU, NVML, network, database, Docker, service or PDF operation occurs. The
test-only `M6BoundedProcess` owns every process this file starts; a test deadline
is a test failure and never a product pass.

.EXAMPLE
.\test_mineru_m6_owner_launcher.ps1 -SourceRoot .\scripts\windows -OutputRoot C:\Users\help\workspaces\NEW-LAUNCHER-TEST
#>
[CmdletBinding()]
param(
    [string]$SourceRoot = $PSScriptRoot,
    [Parameter(Mandatory = $true)][string]$OutputRoot,
    [string]$ExpectedLauncherSha256 = '',
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

# ---- declared bounds (product minima; this test never widens a product limit) ------------------
$PlannedSeconds = 1
$CloseGraceSeconds = 1
$ExitWaitExtraSeconds = 30
$ReadyWaitSeconds = 5
$BootstrapBindSeconds = 1
$TotalDeadlineMilliseconds = ($PlannedSeconds + $CloseGraceSeconds + $ExitWaitExtraSeconds) * 1000
$LauncherDeadlineMilliseconds = 150000
$ShortLauncherDeadlineMilliseconds = 90000
$MarkerDeadlineMilliseconds = 60000
$CompilerDeadlineMilliseconds = 180000
$MaximumSourceBytes = 1048576

# ---- declared literal fixtures -----------------------------------------------------------------
$Hostname = [Environment]::MachineName
$LoopbackPort = 34317
$DeclaredCancelReason = 'independent launcher regression cancel'
# Inert placeholder: this fixture never authenticates and no credential exists in this repository.
$DeclaredPlaceholderToken = 'not-a-credential-independent-test-placeholder'
$ReadyFields = @('status', 'anchor_sha256', 'owner_epoch_sha256', 'anchor', 'spec_sha256',
    'journal_prefix_bytes', 'journal_prefix_sha256')
$CancelFields = @('contract_version', 'process_start_record_sha256', 'run_id', 'attempt_id', 'pid',
    'creation_filetime_100ns', 'binary_sha256', 'configuration_sha256', 'reason')
$MalformedReadyModes = @('ready_not_json', 'ready_extra_field', 'ready_anchor_mismatch',
    'ready_fresh_state_violation', 'ready_oversize')
# Explicit test-binary source allowlist: the twelve production native sources plus this test fixture.
$ProductionSources = @(
    'mineru_m6_owner_binding.cs', 'mineru_m6_owner_endpoint.cs', 'mineru_m6_owner_host.cs',
    'mineru_m6_owner_identity.cs', 'mineru_m6_owner_journal.cs', 'mineru_m6_owner_platform.cs',
    'mineru_m6_owner_wire.cs', 'mineru_m6_private_store.cs', 'mineru_m6_run_control.cs',
    'mineru_m6_writer_guard.cs', 'mineru_nvml_backend.cs', 'mineru_resident_wire.cs')
$FixtureSource = 'test_mineru_m6_owner_launcher_fixture.cs'
$ReferenceAssemblies = @('System.dll', 'System.Core.dll', 'System.Management.dll',
    'System.Net.Http.dll', 'System.IO.Compression.dll')

# ---- helpers -----------------------------------------------------------------------------------
function Check([bool]$Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
function Get-TestSha256Bytes([byte[]]$Bytes) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return 'sha256:' + ([BitConverter]::ToString($algorithm.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $algorithm.Dispose() }
}
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
function Read-TestText([string]$Path) { return $Utf8.GetString((Read-SharedBytes $Path)) }
function Read-TestJson([string]$Path) { return (Read-TestText $Path | ConvertFrom-Json) }
function Read-Diagnostic([string]$Path) {
    # Lenient on purpose: a child's console bytes are raw evidence, never an oracle.
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return '' }
    $bytes = Read-SharedBytes $Path
    return $Utf8NoThrow.GetString($bytes, 0, [Math]::Min($bytes.Length, 262144))
}
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
function ConvertTo-SingleQuoted([string]$Value) { return ($Value -replace "'", "''") }

# Independent ACL oracle: the test verifies the protected owner-only DACL itself rather than
# trusting the launcher's own receipt.
function Assert-ProtectedDirectory([string]$Path) {
    Check ([IO.Directory]::Exists($Path)) ('private directory missing: ' + $Path)
    $sections = [Security.AccessControl.AccessControlSections]::Access -bor [Security.AccessControl.AccessControlSections]::Owner
    $security = [IO.Directory]::GetAccessControl($Path, $sections)
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try { $user = $identity.User } finally { $identity.Dispose() }
    Check ($user.Equals($security.GetOwner([Security.Principal.SecurityIdentifier]))) ('private directory owner differs: ' + $Path)
    Check ($security.AreAccessRulesProtected) ('private directory ACL is not protected: ' + $Path)
    $system = [Security.Principal.SecurityIdentifier]::new([Security.Principal.WellKnownSidType]::LocalSystemSid, $null)
    foreach ($rule in $security.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) {
            Check ($rule.IdentityReference.Equals($user) -or $rule.IdentityReference.Equals($system)) (
                'private directory grants ' + $rule.IdentityReference.Value + ': ' + $Path)
        }
    }
}
# The only ACL mutation in this file, and it only ever WEAKENS an already prepared directory to
# prove the launcher refuses it. Nothing here ever repairs an ACL to let a case succeed.
function Disable-AclProtection([string]$Path) {
    $sections = [Security.AccessControl.AccessControlSections]::Access
    $security = [IO.Directory]::GetAccessControl($Path, $sections)
    $security.SetAccessRuleProtection($false, $true)
    [IO.Directory]::SetAccessControl($Path, $security)
    $after = [IO.Directory]::GetAccessControl($Path, $sections)
    Check (-not $after.AreAccessRulesProtected) ('the ACL weakening fixture did not take effect: ' + $Path)
}

# ---- launcher invocation -------------------------------------------------------------------------
# Parameter names are passed bare and only values are quoted, so PowerShell binds each named
# parameter exactly; a quoted '-Name' token would bind as a positional value instead.
function Start-Launcher {
    param([string]$Label, [string]$Set, [System.Collections.Specialized.OrderedDictionary]$Parameters,
        [string]$WorkingDirectory, [string]$OutputPrefix)
    Check ($Set -cin @('Prepare', 'Run', 'Cancel')) ('unknown launcher parameter set: ' + $Set)
    $tokens = @(("& '" + (ConvertTo-SingleQuoted $script:Launcher) + "'"), ('-' + $Set))
    foreach ($entry in $Parameters.GetEnumerator()) {
        Check ($entry.Key -cmatch '^[A-Za-z][A-Za-z0-9]*$') ('launcher parameter name is invalid: ' + $entry.Key)
        $value = [string]$entry.Value
        Check ($value -notmatch '[\r\n\0]') ('launcher parameter value contains a forbidden character: ' + $entry.Key)
        $tokens += ('-' + $entry.Key)
        $tokens += ("'" + (ConvertTo-SingleQuoted $value) + "'")
    }
    # Best effort and visible if it fails: the authoritative records are the launcher's own JSON
    # files, which it writes with a strict UTF-8 encoder, so console encoding only affects diagnostics.
    $preamble = 'try { [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false) } ' +
        'catch { [Console]::Error.Write(''test adapter could not select UTF8: '' + $_.Exception.Message) }'
    $command = @($preamble, ($tokens -join ' '), 'exit $LASTEXITCODE') -join "`n"
    $arguments = [string[]]@('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'RemoteSigned',
        '-EncodedCommand', [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command)))
    $process = [M6BoundedProcess]::new($script:PowerShellExe, $arguments, $WorkingDirectory, $OutputPrefix)
    return [pscustomobject]@{ Label = $Label; Set = $Set; Process = $process; OutputPrefix = $OutputPrefix
        Arguments = @($Parameters.Keys) }
}
function Complete-Launcher([object]$Started, [int]$DeadlineMilliseconds) {
    $process = $Started.Process
    $process.Finish($DeadlineMilliseconds)
    # The M6-RESULT wire can follow two 128 KiB retained streams; diagnostic truncation is not a wire reader.
    $stdout = Read-TestText ($Started.OutputPrefix + '.stdout.txt')
    $result = $null; $resultError = $null; $resultLine = $null
    foreach ($line in ($stdout -split "`r?`n")) {
        if ($line.StartsWith('M6-RESULT ')) { $resultLine = $line.Substring(10) }
    }
    if ($null -ne $resultLine) { try { $result = $resultLine | ConvertFrom-Json } catch { $resultError = $_.ToString() } }
    return [pscustomobject]@{
        Label = $Started.Label; Set = $Started.Set; ExitCode = $process.ExitCode
        TestOwnerForced = $process.ForcedTermination; ActiveJobProcesses = $process.ActiveJobProcesses
        Result = $result; ResultError = $resultError; Stdout = $stdout
        Stderr = (Read-Diagnostic ($Started.OutputPrefix + '.stderr.txt')) }
}
$script:CleanupFailures = [Collections.Generic.List[string]]::new()
function Close-Launcher([object]$Started) {
    if ($null -eq $Started) { return }
    try { $Started.Process.Dispose() } catch { $script:CleanupFailures.Add($Started.Label + ': ' + $_.ToString()) }
}
function Invoke-Launcher {
    param([string]$Label, [string]$Set, [System.Collections.Specialized.OrderedDictionary]$Parameters,
        [string]$WorkingDirectory, [string]$OutputPrefix, [int]$DeadlineMilliseconds)
    $started = $null; $completed = $null
    try {
        $started = Start-Launcher -Label $Label -Set $Set -Parameters $Parameters -WorkingDirectory $WorkingDirectory -OutputPrefix $OutputPrefix
        $completed = Complete-Launcher $started $DeadlineMilliseconds
    } finally { Close-Launcher $started }
    return $completed
}
function Assert-TestOwnerDidNotIntervene([object]$Completed) {
    Check (-not $Completed.TestOwnerForced) ($Completed.Label + ': the test owner had to terminate the launcher; this is a test failure, never a product pass')
    Check ($Completed.ActiveJobProcesses -eq 0) ($Completed.Label + ': a descendant survived the launcher')
    Check ($null -ne $Completed.Result) ($Completed.Label + ': the launcher printed no parseable M6-RESULT line (' + [string]$Completed.ResultError + '); stdout: ' + $Completed.Stdout)
    Check ([string]$Completed.Result.contract_version -ceq 'm6.owner-launcher-result.v1') ($Completed.Label + ': launcher result contract drifted')
    Check ([string]$Completed.Result.set -ceq $Completed.Set) ($Completed.Label + ': launcher result names another parameter set')
    Check ([int]$Completed.Result.exit_code -eq $Completed.ExitCode) ($Completed.Label + ': launcher result exit code differs from the process exit code')
}

# ---- workspace and staged deployment ---------------------------------------------------------------
function New-Workspace([string]$Name) {
    $root = Join-Path $script:WorkspacesRoot $Name
    Check (-not (Test-Path -LiteralPath $root)) ('workspace must be new: ' + $root)
    $caseDirectory = Join-Path $script:CasesRoot $Name
    [void][IO.Directory]::CreateDirectory($caseDirectory)
    # The workspace root itself is left to the launcher's own Prepare set.
    return [pscustomobject]@{
        Name = $Name; Root = $root
        PrivateRoot = (Join-Path $root 'private')
        RunsRoot = (Join-Path $root 'private\runs')
        StagingRoot = (Join-Path $root 'private\staging')
        AttemptsRoot = (Join-Path $root 'private\attempts')
        ReceiptPath = (Join-Path $root 'prepare-receipt.json')
        CaseDirectory = $caseDirectory }
}
function Invoke-Prepare([object]$Workspace, [string]$Label) {
    return (Invoke-Launcher -Label $Label -Set 'Prepare' -Parameters ([ordered]@{
        WorkspaceRoot = $Workspace.Root; ExpectedHostname = $script:Hostname
        BinaryPath = $script:FixtureExe; ExpectedBinarySha256 = $script:FixtureExeSha256
    }) -WorkingDirectory $Workspace.CaseDirectory -OutputPrefix (Join-Path $Workspace.CaseDirectory $Label) `
        -DeadlineMilliseconds $script:ShortLauncherDeadlineMilliseconds)
}
function Initialize-Workspace([string]$Name) {
    $workspace = New-Workspace $Name
    $prepared = Invoke-Prepare $workspace 'prepare'
    Check ($prepared.ExitCode -eq 0) ('Prepare failed for ' + $Name + ': exit ' + $prepared.ExitCode + '; ' + $prepared.Stdout + $prepared.Stderr)
    foreach ($path in @($workspace.PrivateRoot, $workspace.RunsRoot, $workspace.StagingRoot, $workspace.AttemptsRoot)) {
        Assert-ProtectedDirectory $path
    }
    return $workspace
}
# The closed m6.owner-deployment.v2 document. It carries no test-only field: the controlled child
# derives its behaviour from the declared test run id instead.
function New-StagedDeployment([object]$Workspace, [string]$RunId, [string]$StagedName) {
    $document = [ordered]@{
        contract_version = 'm6.owner-deployment.v2'
        run_id = $RunId
        run_root = $Workspace.RunsRoot
        mode = 'service_diagnostic'
        owner_source_sha256 = ('sha256:' + ('a' * 64))
        expected_node_sha256 = ('sha256:' + ('b' * 64))
        gpu_uuid = 'GPU-00000000-0000-0000-0000-000000000000'
        nvml_dll_sha256 = ('sha256:' + ('c' * 64))
        port = $script:LoopbackPort
        resources = [ordered]@{
            max_events = 1000; max_record_bytes = 60000; max_log_bytes = 1048576
            max_attempts = 100; max_verifier_backlog_bytes = 1048576; stop_admission_budget_ticks = 1000000 }
        max_artifacts = 256
        max_artifact_bytes = 16777216
        maximum_lease_ticks = 100000000
        propagation_reserve_ticks = 1000000
        bootstrap_bind_seconds = $script:BootstrapBindSeconds
        roles = [ordered]@{
            controller = [ordered]@{ epoch_sha256 = ('sha256:' + ('d' * 64)); token = $script:DeclaredPlaceholderToken }
            service_runner = [ordered]@{ epoch_sha256 = ('sha256:' + ('e' * 64)); token = $script:DeclaredPlaceholderToken }
            quality_verifier = [ordered]@{ epoch_sha256 = ('sha256:' + ('f' * 64)); token = $script:DeclaredPlaceholderToken } }
    }
    $path = Join-Path $Workspace.StagingRoot $StagedName
    Write-NewTestText $path ($document | ConvertTo-Json -Compress -Depth 6)
    return [pscustomobject]@{ Path = $path; Name = $StagedName; RunId = $RunId; Sha256 = (Get-TestSha256File $path)
        CommittedPath = (Join-Path $Workspace.PrivateRoot ('deployment.' + $RunId + '.json')) }
}
function New-RunParameters {
    param([object]$Workspace, [object]$Deployment, [string]$AttemptId,
        [string]$BinarySha256 = '', [string]$ConfigurationSha256 = '')
    if ($BinarySha256 -eq '') { $BinarySha256 = $script:FixtureExeSha256 }
    if ($ConfigurationSha256 -eq '') { $ConfigurationSha256 = $Deployment.Sha256 }
    return [ordered]@{
        WorkspaceRoot = $Workspace.Root; ExpectedHostname = $script:Hostname
        BinaryPath = $script:FixtureExe; ExpectedBinarySha256 = $BinarySha256
        AttemptId = $AttemptId; StagedConfigurationName = $Deployment.Name
        ExpectedConfigurationSha256 = $ConfigurationSha256; ExpectedRunId = $Deployment.RunId
        PlannedSeconds = $script:PlannedSeconds; CloseGraceSeconds = $script:CloseGraceSeconds
        ExitWaitExtraSeconds = $script:ExitWaitExtraSeconds; ReadyWaitSeconds = $script:ReadyWaitSeconds }
}
function Get-AttemptPaths([object]$Workspace, [string]$AttemptId) {
    $directory = Join-Path $Workspace.AttemptsRoot $AttemptId
    return [pscustomobject]@{ Directory = $directory
        ProcessStart = (Join-Path $directory 'process-start.json')
        Ready = (Join-Path $directory 'ready.json')
        ProcessExit = (Join-Path $directory 'process-exit.json')
        Cancel = (Join-Path $directory 'cancel.json') }
}
# The test's own READY oracle, independent of the launcher's Test-ReadyLine.
function Assert-FreshReadyRecord([string]$Path, [string]$RunId) {
    $ready = Read-TestJson $Path
    $names = @($ready.PSObject.Properties | ForEach-Object { $_.Name })
    Check ($names.Count -eq $script:ReadyFields.Count) ('READY record field count is ' + $names.Count)
    foreach ($field in $script:ReadyFields) { Check ($names -ccontains $field) ('READY record lacks ' + $field) }
    Check ([string]$ready.status -ceq 'ready_unbound') ('READY status is ' + [string]$ready.status)
    Check ($null -eq $ready.spec_sha256) 'a fresh owner must report no bound spec'
    Check ([long]$ready.journal_prefix_bytes -eq 0) 'a fresh owner must report an empty journal prefix'
    Check ([string]$ready.anchor_sha256 -cmatch '^sha256:[0-9a-f]{64}$') 'READY anchor hash shape'
    Check ([string]$ready.anchor.contract_version -ceq 'm6.owner-anchor.v1') 'READY anchor contract'
    Check ([string]$ready.anchor.run_id -ceq $RunId) 'READY anchor names another run'
    Check ([long]$ready.anchor.planned_seconds -eq $script:PlannedSeconds) 'READY anchor planned interval differs'
    Check ([long]$ready.anchor.max_close_ticks -ge [long]$ready.anchor.deadline_ticks) 'READY anchor close bound precedes its deadline'
    return $ready
}

# ---- preparation ---------------------------------------------------------------------------------
$SourceRoot = [IO.Path]::GetFullPath($SourceRoot)
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
foreach ($protected in @([Environment]::SystemDirectory, 'C:\ProgramData', 'C:\Windows', 'C:\Program Files')) {
    Check (-not $OutputRoot.StartsWith($protected, [StringComparison]::OrdinalIgnoreCase)) 'a disposable output root outside every protected location is required'
}
Check (Test-Path -LiteralPath $SourceRoot -PathType Container) ('source root missing: ' + $SourceRoot)
Check (-not (Test-Path -LiteralPath $OutputRoot)) ('output root must be new: ' + $OutputRoot)
[void][IO.Directory]::CreateDirectory($OutputRoot)
$CasesRoot = Join-Path $OutputRoot 'cases'
$WorkspacesRoot = Join-Path $OutputRoot 'workspaces'
foreach ($directory in @($CasesRoot, $WorkspacesRoot)) { [void][IO.Directory]::CreateDirectory($directory) }

$Launcher = Join-Path $SourceRoot 'run_mineru_m6_owner_host.ps1'
$ProcessHelper = Join-Path $SourceRoot 'test_mineru_m6_process.cs'
foreach ($path in @($Launcher, $ProcessHelper)) {
    Check (Test-Path -LiteralPath $path -PathType Leaf) ('required input missing: ' + $path)
}
$LauncherSha256 = Get-TestSha256File $Launcher
if ($ExpectedLauncherSha256 -ne '') {
    Check ($ExpectedLauncherSha256 -cmatch '^sha256:[0-9a-f]{64}$') 'the expected launcher hash must be canonical'
    Check ($LauncherSha256 -ceq $ExpectedLauncherSha256) ('launcher identity drifted: ' + $LauncherSha256)
}
$PowerShellExe = Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
Add-Type -Path $ProcessHelper -ErrorAction Stop

# The controlled child is compiled from an explicit allowlist: exactly the twelve production native
# sources plus this test fixture, with /main:LauncherFixture. This is a TEST binary in the disposable
# output root; it is never a production artefact and the official production builder is tested
# separately by root.
$FrameworkDirectory = Join-Path (Split-Path -Parent ([Environment]::SystemDirectory)) 'Microsoft.NET\Framework64\v4.0.30319'
$Csc = Join-Path $FrameworkDirectory 'csc.exe'
Check (Test-Path -LiteralPath $Csc -PathType Leaf) ('compiler missing: ' + $Csc)
$TestBinarySources = [ordered]@{}
$SourcePaths = @()
foreach ($name in (@($ProductionSources) + @($FixtureSource))) {
    $path = Join-Path $SourceRoot $name
    Check (Test-Path -LiteralPath $path -PathType Leaf) ('declared test-binary source missing: ' + $name)
    $length = (Get-Item -LiteralPath $path).Length
    Check ($length -ge 1 -and $length -le $MaximumSourceBytes) ($name + ' is outside the declared source byte bound: ' + $length)
    $TestBinarySources[$name] = Get-TestSha256File $path
    $SourcePaths += $path
}
$FixtureExe = Join-Path $OutputRoot 'independent_launcher_fixture.exe'
$CompilerArguments = @('/noconfig', '/nologo', '/warnaserror+', '/nowarn:1701,1702', '/target:exe',
    '/platform:x64', '/optimize-', '/debug-', ('/lib:' + $FrameworkDirectory)) +
    @($ReferenceAssemblies | ForEach-Object { '/r:' + $_ }) +
    @('/main:LauncherFixture', ('/out:' + $FixtureExe)) + $SourcePaths
$Compile = $null
try {
    $Compile = [M6BoundedProcess]::new($Csc, [string[]]$CompilerArguments, $OutputRoot, (Join-Path $OutputRoot 'compile'))
    $Compile.Finish($CompilerDeadlineMilliseconds)
    Check ($Compile.ExitCode -eq 0 -and -not $Compile.ForcedTermination -and $Compile.ActiveJobProcesses -eq 0) (
        'the declared test binary did not compile (exit ' + $Compile.ExitCode + '): ' +
        (Read-Diagnostic $Compile.StdoutPath) + (Read-Diagnostic $Compile.StderrPath))
} finally { if ($null -ne $Compile) { try { $Compile.Dispose() } catch { } } }
Check (Test-Path -LiteralPath $FixtureExe -PathType Leaf) 'the controlled child binary was not produced'
$FixtureExeSha256 = Get-TestSha256File $FixtureExe

$Results = [Collections.Generic.List[object]]::new()
function Invoke-Case([string]$Name, [scriptblock]$Body) {
    if ($script:CaseFilter -ne '' -and $script:CaseFilter -cne $Name) { return }
    $script:CleanupFailures.Clear()
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $row = [ordered]@{ case = $Name; status = 'fail'; elapsed_ms = 0; detail = $null; error = $null; cleanup_failures = @() }
    try {
        # A stray pipeline object is a defect in this test, not a product result.
        $emitted = @(& $Body)
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

# Exact identity, never a bare PID: a reused process id has a different birth and is not this child.
function Test-ExactProcessAlive([int]$ProcessId, [long]$CreationFiletime) {
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $false }
    $birth = $null
    try { if (-not $process.HasExited) { $birth = $process.StartTime.ToFileTimeUtc() } } catch { $birth = $null }
    if ($null -eq $birth) { return $false }
    return ($birth -eq $CreationFiletime)
}
function Get-ReadyEvidenceLine([string]$Stdout) {
    $line = $null
    foreach ($candidate in ($Stdout -split "`r?`n")) { if ($candidate.StartsWith('M6-READY ')) { $line = $candidate.Substring(9) } }
    return $line
}
function Assert-NoChildStarted([object]$Workspace, [object]$Deployment) {
    if (Test-Path -LiteralPath $Workspace.AttemptsRoot -PathType Container) {
        $entries = @(Get-ChildItem -LiteralPath $Workspace.AttemptsRoot -Force)
        Check ($entries.Count -eq 0) ('an attempt directory was created before the refusal: ' + (($entries | ForEach-Object { $_.Name }) -join ','))
    }
    if ($null -ne $Deployment) {
        Check (-not (Test-Path -LiteralPath $Deployment.CommittedPath)) 'the deployment was committed before the refusal'
    }
}

# ---- cases -------------------------------------------------------------------------------------

# Pro R20 section 3.3: the official Prepare creates every protected private ancestor through the
# product's own CreatePrivateDirectory before any secret exists. Nothing is pre-created here.
Invoke-Case 'prepare_creates_protected_private_ancestors' {
    $workspace = New-Workspace 'prepare'
    $first = Invoke-Prepare $workspace 'prepare'
    Assert-TestOwnerDidNotIntervene $first
    Check ($first.ExitCode -eq 0) ('Prepare exit ' + $first.ExitCode + '; ' + $first.Stdout + $first.Stderr)
    Check (Test-Path -LiteralPath $workspace.ReceiptPath -PathType Leaf) 'the prepare receipt was not persisted'
    $receipt = Read-TestJson $workspace.ReceiptPath
    Check ([string]$receipt.contract_version -ceq 'm6.owner-workspace-prepare.v1') 'prepare receipt contract drifted'
    Check ([string]$receipt.hostname -ceq $Hostname) 'prepare receipt names another host'
    Check ([string]$receipt.workspace_root -ceq $workspace.Root) 'prepare receipt workspace root differs'
    Check ([string]$receipt.private_root -ceq $workspace.PrivateRoot) 'prepare receipt private root differs'
    Check ([string]$receipt.runs_root -ceq $workspace.RunsRoot) 'prepare receipt runs root differs'
    Check ([string]$receipt.staging_root -ceq $workspace.StagingRoot) 'prepare receipt staging root differs'
    Check ([string]$receipt.attempts_root -ceq $workspace.AttemptsRoot) 'prepare receipt attempts root differs'
    Check ([string]$receipt.binary_sha256 -ceq $FixtureExeSha256) 'prepare receipt does not bind the pinned binary'
    Check ([string]$receipt.launcher_sha256 -ceq $LauncherSha256) 'prepare receipt does not bind the launcher'
    $declared = @($receipt.directories.PSObject.Properties)
    Check ($declared.Count -eq 4) ('prepare receipt declares ' + $declared.Count + ' directories')
    foreach ($entry in $declared) {
        Check ([bool]$entry.Value.protected) ('prepare receipt does not mark ' + $entry.Name + ' protected')
        Check ([string]$entry.Value.sddl -ne '') ('prepare receipt has no SDDL for ' + $entry.Name)
    }
    # Independent oracle: this test reads the real ACLs rather than trusting the receipt.
    foreach ($path in @($workspace.PrivateRoot, $workspace.RunsRoot, $workspace.StagingRoot, $workspace.AttemptsRoot)) {
        Assert-ProtectedDirectory $path
    }
    Check (@(Get-ChildItem -LiteralPath $workspace.RunsRoot -Force).Count -eq 0) 'the prepared runs root must be empty; no spec may be pre-seeded'
    Check (@(Get-ChildItem -LiteralPath $workspace.StagingRoot -Force).Count -eq 0) 'the prepared staging root must be empty before the controller uploads'
    Check (@(Get-ChildItem -LiteralPath $workspace.AttemptsRoot -Force).Count -eq 0) 'the prepared attempts root must be empty'
    # A campaign uses a fresh workspace: preparing the same one twice is refused.
    $repeat = Invoke-Prepare $workspace 'prepare-repeat'
    Assert-TestOwnerDidNotIntervene $repeat
    Check ($repeat.ExitCode -eq 70) ('repeat Prepare exit ' + $repeat.ExitCode)
    Check ([string]$repeat.Result.first_error -match 'private root already exists') ('repeat Prepare first error: ' + [string]$repeat.Result.first_error)
    [pscustomobject]@{ prepare_exit = $first.ExitCode; repeat_exit = $repeat.ExitCode
        owner_sid = [string]$receipt.owner_sid; directories = $declared.Count }
}

# The natural path: staged deployment committed under the lock, exact seven-field READY inside the
# deadline, child exits by itself, external exit record proves it.
Invoke-Case 'valid_ready_and_natural_exit' {
    $workspace = Initialize-Workspace 'normal'
    $deployment = New-StagedDeployment $workspace 'm6t.normal.a01' 'deployment.a01.json'
    $attempt = Get-AttemptPaths $workspace 'attempt-a01'
    $run = Invoke-Launcher -Label 'run' -Set 'Run' -Parameters (New-RunParameters $workspace $deployment 'attempt-a01') `
        -WorkingDirectory $workspace.CaseDirectory -OutputPrefix (Join-Path $workspace.CaseDirectory 'run') `
        -DeadlineMilliseconds $ShortLauncherDeadlineMilliseconds
    Assert-TestOwnerDidNotIntervene $run
    Check ($run.ExitCode -eq 0) ('expected exit 0, actual ' + $run.ExitCode + '; ' + [string]$run.Result.first_error)
    Assert-ProtectedDirectory $attempt.Directory
    # The staged document was committed atomically into the private root and read back.
    Check (Test-Path -LiteralPath $deployment.CommittedPath -PathType Leaf) 'the deployment was not committed into the private root'
    Check ((Get-TestSha256File $deployment.CommittedPath) -ceq $deployment.Sha256) 'the committed deployment differs from the staged bytes'
    Check (-not (Test-Path -LiteralPath $deployment.Path)) 'the staged copy survived the atomic commit'
    Check (@(Get-ChildItem -LiteralPath $workspace.RunsRoot -Force).Count -eq 0) 'a run directory or spec appeared without any bind'
    $start = Read-TestJson $attempt.ProcessStart
    Check ([string]$start.contract_version -ceq 'm6.owner-external-start.v2') 'process start contract drifted'
    Check ([string]$start.run_id -ceq $deployment.RunId) 'process start names another run'
    Check ([string]$start.attempt_id -ceq 'attempt-a01') 'process start names another attempt'
    Check ([string]$start.configuration_path -ceq $deployment.CommittedPath) 'process start does not bind the committed deployment'
    Check ([string]$start.binary_sha256 -ceq $FixtureExeSha256) 'process start does not bind the pinned binary'
    Check ([string]$start.configuration_sha256 -ceq $deployment.Sha256) 'process start does not bind the deployment identity'
    Check ([long]$start.bootstrap_bind_seconds -eq $BootstrapBindSeconds) 'process start does not carry the deployment bootstrap bound'
    Check ([long]$start.ready_wait_seconds -eq $ReadyWaitSeconds) 'process start does not carry the READY bound'
    $ready = Assert-FreshReadyRecord $attempt.Ready $deployment.RunId
    $readyLine = Get-ReadyEvidenceLine $run.Stdout
    Check ($null -ne $readyLine) 'the launcher emitted no READY evidence line'
    Check ($readyLine -ceq (Read-TestText $attempt.Ready)) 'the persisted READY record differs from the emitted READY evidence'
    $exit = Read-TestJson $attempt.ProcessExit
    Check ([string]$exit.contract_version -ceq 'm6.owner-external-exit.v2') 'external exit contract drifted'
    Check ([int]$exit.exit_code -eq 0) ('external exit code is ' + [string]$exit.exit_code)
    Check ([bool]$exit.ready_received) 'the external exit record denies READY'
    Check (-not [bool]$exit.ready_timeout) 'a satisfied READY must not be recorded as a timeout'
    Check (-not [bool]$exit.forced_termination) 'a natural exit must not be recorded as forced'
    Check ([bool]$exit.process_handle_signaled) 'the exact process handle was not signalled'
    Check ([bool]$exit.exact_process_handle_opened) 'no exact process handle was held'
    Check ($null -eq $exit.cancel) 'an uncancelled run recorded a cancel'
    Check ([long]$exit.pid -eq [long]$start.pid) 'the exit record names another process'
    Check ([long]$exit.creation_filetime_100ns -eq [long]$start.creation_filetime_100ns) 'the exit record names another process birth'
    Check (-not (Test-ExactProcessAlive ([int]$exit.pid) ([long]$exit.creation_filetime_100ns))) 'the exact child outlived the launcher'
    [pscustomobject]@{ exit_code = $run.ExitCode; host_exit_code = [int]$exit.exit_code
        ready_elapsed_ms = [long]$exit.ready_elapsed_milliseconds; anchor_sha256 = [string]$ready.anchor_sha256 }
}

# Root launcher source review 1: both pipes at EOF while the child is still alive must not bypass the
# READY deadline. EOF is not process exit.
Invoke-Case 'pipe_eof_before_ready_respects_ready_deadline' {
    $workspace = Initialize-Workspace 'eof-before-ready'
    $deployment = New-StagedDeployment $workspace 'm6t.eof_before_ready.b01' 'deployment.b01.json'
    $attempt = Get-AttemptPaths $workspace 'attempt-b01'
    $run = Invoke-Launcher -Label 'run' -Set 'Run' -Parameters (New-RunParameters $workspace $deployment 'attempt-b01') `
        -WorkingDirectory $workspace.CaseDirectory -OutputPrefix (Join-Path $workspace.CaseDirectory 'run') `
        -DeadlineMilliseconds $LauncherDeadlineMilliseconds
    Assert-TestOwnerDidNotIntervene $run
    Check ($run.ExitCode -eq 3) ('expected READY deadline exit 3, actual ' + $run.ExitCode + '; ' + [string]$run.Result.first_error)
    Check (-not (Test-Path -LiteralPath $attempt.Ready)) 'a READY record exists although the child never reported READY'
    Check (Test-Path -LiteralPath (Join-Path $attempt.Directory 'fixture-pipes-closed.json') -PathType Leaf) (
        'the controlled child never closed both pipe write ends; this case did not exercise the EOF-while-alive path')
    $exit = Read-TestJson $attempt.ProcessExit
    Check ([bool]$exit.ready_timeout) 'the READY deadline was not recorded'
    Check (-not [bool]$exit.ready_received) 'the external exit record claims READY'
    Check ([bool]$exit.forced_termination) 'a child held past the READY deadline must be terminated'
    Check ([bool]$exit.process_handle_signaled) 'the terminated child was not reaped'
    # The decisive assertion: the launcher stopped at its READY bound and did not fall through to the
    # full planned+grace+extra wait.
    Check ([long]$exit.elapsed_milliseconds -lt ($ReadyWaitSeconds * 1000 + 10000)) (
        'the launcher waited ' + [string]$exit.elapsed_milliseconds + ' ms; the READY bound is ' + ($ReadyWaitSeconds * 1000) + ' ms')
    Check ([long]$exit.elapsed_milliseconds -lt ($TotalDeadlineMilliseconds - 5000)) 'the launcher fell through to the run deadline instead of the READY deadline'
    Check (-not (Test-ExactProcessAlive ([int]$exit.pid) ([long]$exit.creation_filetime_100ns))) 'the exact child outlived the READY deadline'
    [pscustomobject]@{ exit_code = $run.ExitCode; elapsed_ms = [long]$exit.elapsed_milliseconds
        ready_timeout = [bool]$exit.ready_timeout; first_error = [string]$run.Result.first_error }
}

# Root launcher source review 1, second half: after a valid READY, EOF on both pipes must not stop
# cancel polling. The exact-instance Cancel still has to reach this child.
Invoke-Case 'pipe_eof_after_ready_does_not_disable_exact_cancel' {
    $workspace = Initialize-Workspace 'eof-after-ready'
    $deployment = New-StagedDeployment $workspace 'm6t.eof_after_ready.c01' 'deployment.c01.json'
    $attempt = Get-AttemptPaths $workspace 'attempt-c01'
    $started = $null; $run = $null; $cancel = $null
    try {
        $started = Start-Launcher -Label 'run' -Set 'Run' -Parameters (New-RunParameters $workspace $deployment 'attempt-c01') `
            -WorkingDirectory $workspace.CaseDirectory -OutputPrefix (Join-Path $workspace.CaseDirectory 'run')
        $null = Wait-ForJson $attempt.Ready $MarkerDeadlineMilliseconds 'the READY record'
        $null = Wait-ForJson (Join-Path $attempt.Directory 'fixture-pipes-closed.json') $MarkerDeadlineMilliseconds 'both fixture pipes closed'
        $cancel = Invoke-Launcher -Label 'cancel' -Set 'Cancel' -Parameters ([ordered]@{
            WorkspaceRoot = $workspace.Root; ExpectedHostname = $Hostname
            AttemptId = 'attempt-c01'; Reason = $DeclaredCancelReason
        }) -WorkingDirectory $workspace.CaseDirectory -OutputPrefix (Join-Path $workspace.CaseDirectory 'cancel') `
            -DeadlineMilliseconds $ShortLauncherDeadlineMilliseconds
        $run = Complete-Launcher $started $LauncherDeadlineMilliseconds
    } finally { Close-Launcher $started }
    Assert-TestOwnerDidNotIntervene $cancel
    Check ($cancel.ExitCode -eq 0) ('Cancel exit ' + $cancel.ExitCode + '; ' + [string]$cancel.Result.first_error)
    Check ([string]$cancel.Result.cancel -ceq 'requested') ('Cancel reported ' + [string]$cancel.Result.cancel)
    Assert-TestOwnerDidNotIntervene $run
    Check ($run.ExitCode -eq 70) ('expected cancelled exit 70, actual ' + $run.ExitCode + '; ' + [string]$run.Result.first_error)
    Check (Test-Path -LiteralPath (Join-Path $attempt.Directory 'fixture-pipes-closed.json') -PathType Leaf) (
        'the controlled child never closed both pipe write ends; this case did not exercise the EOF-while-alive path')
    $exit = Read-TestJson $attempt.ProcessExit
    Check ($null -ne $exit.cancel) 'the cancel command was never honoured after both pipes reached EOF'
    Check ([string]$exit.cancel.reason -ceq $DeclaredCancelReason) ('the honoured cancel carries reason ' + [string]$exit.cancel.reason)
    Check ([bool]$exit.forced_termination) 'a cancelled child must be recorded as forcibly terminated'
    Check ([bool]$exit.ready_received) 'this case requires a valid READY before the cancel'
    Check ([string]$run.Result.first_error -match 'cancelled:') ('launcher first error: ' + [string]$run.Result.first_error)
    Check ([long]$exit.elapsed_milliseconds -lt ($TotalDeadlineMilliseconds - 5000)) 'the launcher ran to its deadline instead of honouring the cancel'
    Check (-not (Test-ExactProcessAlive ([int]$exit.pid) ([long]$exit.creation_filetime_100ns))) 'the cancelled child survived'
    [pscustomobject]@{ run_exit = $run.ExitCode; cancel_exit = $cancel.ExitCode
        elapsed_ms = [long]$exit.elapsed_milliseconds; reason = [string]$exit.cancel.reason }
}

# Pro R20 section 3.4: Cancel is an immutable exact-instance command. An identical repeat is
# idempotent and the running launcher terminates through the handle it has held since spawn.
Invoke-Case 'exact_cancel_is_honoured_and_identical_repeat_is_idempotent' {
    $workspace = Initialize-Workspace 'cancel'
    $deployment = New-StagedDeployment $workspace 'm6t.cancel_wait.d01' 'deployment.d01.json'
    $attempt = Get-AttemptPaths $workspace 'attempt-d01'
    $started = $null; $run = $null; $first = $null; $repeat = $null; $firstBytes = $null
    try {
        $started = Start-Launcher -Label 'run' -Set 'Run' -Parameters (New-RunParameters $workspace $deployment 'attempt-d01') `
            -WorkingDirectory $workspace.CaseDirectory -OutputPrefix (Join-Path $workspace.CaseDirectory 'run')
        $null = Wait-ForJson $attempt.Ready $MarkerDeadlineMilliseconds 'the READY record'
        $cancelParameters = [ordered]@{ WorkspaceRoot = $workspace.Root; ExpectedHostname = $Hostname
            AttemptId = 'attempt-d01'; Reason = $DeclaredCancelReason }
        $first = Invoke-Launcher -Label 'cancel-first' -Set 'Cancel' -Parameters $cancelParameters `
            -WorkingDirectory $workspace.CaseDirectory -OutputPrefix (Join-Path $workspace.CaseDirectory 'cancel-first') `
            -DeadlineMilliseconds $ShortLauncherDeadlineMilliseconds
        $firstBytes = Get-TestSha256File $attempt.Cancel
        $repeat = Invoke-Launcher -Label 'cancel-repeat' -Set 'Cancel' -Parameters $cancelParameters `
            -WorkingDirectory $workspace.CaseDirectory -OutputPrefix (Join-Path $workspace.CaseDirectory 'cancel-repeat') `
            -DeadlineMilliseconds $ShortLauncherDeadlineMilliseconds
        $run = Complete-Launcher $started $LauncherDeadlineMilliseconds
    } finally { Close-Launcher $started }
    Assert-TestOwnerDidNotIntervene $first
    Assert-TestOwnerDidNotIntervene $repeat
    Check ($first.ExitCode -eq 0) ('first Cancel exit ' + $first.ExitCode + '; ' + [string]$first.Result.first_error)
    Check ([string]$first.Result.cancel -ceq 'requested') ('first Cancel reported ' + [string]$first.Result.cancel)
    Check ($repeat.ExitCode -eq 0) ('repeat Cancel exit ' + $repeat.ExitCode + '; ' + [string]$repeat.Result.first_error)
    Check ([string]$repeat.Result.cancel -ceq 'already_requested') ('repeat Cancel reported ' + [string]$repeat.Result.cancel)
    Check ((Get-TestSha256File $attempt.Cancel) -ceq $firstBytes) 'the idempotent repeat rewrote the cancel command'
    # The command names exactly this instance, taken from its own process-start record.
    $start = Read-TestJson $attempt.ProcessStart
    $command = Read-TestJson $attempt.Cancel
    $names = @($command.PSObject.Properties | ForEach-Object { $_.Name })
    Check ($names.Count -eq $CancelFields.Count) ('cancel command field count is ' + $names.Count)
    foreach ($field in $CancelFields) { Check ($names -ccontains $field) ('cancel command lacks ' + $field) }
    Check ([string]$command.contract_version -ceq 'm6.owner-cancel.v1') 'cancel contract drifted'
    Check ([string]$command.run_id -ceq $deployment.RunId) 'cancel names another run'
    Check ([string]$command.attempt_id -ceq 'attempt-d01') 'cancel names another attempt'
    Check ([long]$command.pid -eq [long]$start.pid) 'cancel names another process'
    Check ([long]$command.creation_filetime_100ns -eq [long]$start.creation_filetime_100ns) 'cancel names another process birth'
    Check ([string]$command.binary_sha256 -ceq $FixtureExeSha256) 'cancel does not bind the pinned binary'
    Check ([string]$command.configuration_sha256 -ceq $deployment.Sha256) 'cancel does not bind the deployment identity'
    Check ([string]$command.process_start_record_sha256 -ceq (Get-TestSha256File $attempt.ProcessStart)) 'cancel does not bind the process start record bytes'
    Assert-TestOwnerDidNotIntervene $run
    Check ($run.ExitCode -eq 70) ('expected cancelled exit 70, actual ' + $run.ExitCode + '; ' + [string]$run.Result.first_error)
    $exit = Read-TestJson $attempt.ProcessExit
    Check ($null -ne $exit.cancel) 'the honoured cancel is absent from the external exit record'
    Check ([string]$exit.cancel.reason -ceq $DeclaredCancelReason) 'the honoured cancel carries another reason'
    Check ([bool]$exit.forced_termination) 'a cancelled child must be recorded as forcibly terminated'
    Check ([long]$exit.elapsed_milliseconds -lt ($TotalDeadlineMilliseconds - 5000)) 'the launcher ran to its deadline instead of honouring the cancel'
    Check (-not (Test-ExactProcessAlive ([int]$exit.pid) ([long]$exit.creation_filetime_100ns))) 'the cancelled child survived'
    [pscustomobject]@{ first_cancel = [string]$first.Result.cancel; repeat_cancel = [string]$repeat.Result.cancel
        run_exit = $run.ExitCode; elapsed_ms = [long]$exit.elapsed_milliseconds }
}

# Pro R20 section 3.4: a cancel command that names another instance is refused and nothing else is
# harmed. The stale command here is a real product-produced cancel from an earlier attempt.
Invoke-Case 'cancel_naming_another_attempt_is_refused_without_harm' {
    $workspace = Initialize-Workspace 'cancel-foreign'
    $priorDeployment = New-StagedDeployment $workspace 'm6t.normal.e01' 'deployment.e01.json'
    $priorAttempt = Get-AttemptPaths $workspace 'attempt-e-prior'
    $prior = Invoke-Launcher -Label 'run-prior' -Set 'Run' -Parameters (New-RunParameters $workspace $priorDeployment 'attempt-e-prior') `
        -WorkingDirectory $workspace.CaseDirectory -OutputPrefix (Join-Path $workspace.CaseDirectory 'run-prior') `
        -DeadlineMilliseconds $ShortLauncherDeadlineMilliseconds
    Assert-TestOwnerDidNotIntervene $prior
    Check ($prior.ExitCode -eq 0) ('prior attempt exit ' + $prior.ExitCode + '; ' + [string]$prior.Result.first_error)
    $priorCancel = Invoke-Launcher -Label 'cancel-prior' -Set 'Cancel' -Parameters ([ordered]@{
        WorkspaceRoot = $workspace.Root; ExpectedHostname = $Hostname
        AttemptId = 'attempt-e-prior'; Reason = 'independent stale cancel from an earlier attempt'
    }) -WorkingDirectory $workspace.CaseDirectory -OutputPrefix (Join-Path $workspace.CaseDirectory 'cancel-prior') `
        -DeadlineMilliseconds $ShortLauncherDeadlineMilliseconds
    Assert-TestOwnerDidNotIntervene $priorCancel
    Check ($priorCancel.ExitCode -eq 0) ('prior Cancel exit ' + $priorCancel.ExitCode)
    $staleBytes = Read-SharedBytes $priorAttempt.Cancel

    $deployment = New-StagedDeployment $workspace 'm6t.cancel_ignored.e02' 'deployment.e02.json'
    $attempt = Get-AttemptPaths $workspace 'attempt-e-current'
    $started = $null; $run = $null
    try {
        $started = Start-Launcher -Label 'run-current' -Set 'Run' -Parameters (New-RunParameters $workspace $deployment 'attempt-e-current') `
            -WorkingDirectory $workspace.CaseDirectory -OutputPrefix (Join-Path $workspace.CaseDirectory 'run-current')
        $null = Wait-ForJson $attempt.Ready $MarkerDeadlineMilliseconds 'the READY record'
        # Dropped atomically: the launcher must never observe a half-written command.
        $staging = Join-Path $workspace.CaseDirectory 'stale-cancel.json'
        Write-NewTestFile $staging $staleBytes
        [IO.File]::Move($staging, $attempt.Cancel)
        $run = Complete-Launcher $started $LauncherDeadlineMilliseconds
    } finally { Close-Launcher $started }
    Assert-TestOwnerDidNotIntervene $run
    Check ($run.ExitCode -eq 0) ('the running attempt must finish naturally, actual exit ' + $run.ExitCode + '; ' + [string]$run.Result.first_error)
    $exit = Read-TestJson $attempt.ProcessExit
    Check ($null -eq $exit.cancel) 'a cancel naming another attempt was accepted'
    Check (-not [bool]$exit.forced_termination) 'a refused cancel terminated the running child'
    Check ([int]$exit.exit_code -eq 0) ('the running child exit code is ' + [string]$exit.exit_code)
    $rejected = @(Get-ChildItem -LiteralPath $attempt.Directory -Filter 'cancel-rejected.*.json' -File)
    Check ($rejected.Count -ge 1) 'the refused cancel left no rejection record'
    $staleSha = Get-TestSha256Bytes $staleBytes
    $matched = @($rejected | Where-Object { ([string](Read-TestJson $_.FullName).command_sha256) -ceq $staleSha })
    Check ($matched.Count -ge 1) ('no rejection record names the stale command ' + $staleSha)
    $rejection = Read-TestJson $matched[0].FullName
    Check ([string]$rejection.contract_version -ceq 'm6.owner-cancel-rejected.v1') 'cancel rejection contract drifted'
    # The earlier attempt's own evidence is untouched by the stale command.
    $priorExit = Read-TestJson $priorAttempt.ProcessExit
    Check (-not [bool]$priorExit.forced_termination) 'the earlier attempt record was rewritten as forced'
    Check ([int]$priorExit.exit_code -eq 0) 'the earlier attempt exit code changed'
    [pscustomobject]@{ run_exit = $run.ExitCode; rejections = $rejected.Count
        rejection_reason = [string]$rejection.reason; prior_exit = $prior.ExitCode }
}

# Pro R20 section 3.3: a missing or non-conforming private ancestor fails before any sensitive
# upload or child process. This test only ever weakens an ACL to prove refusal; it never repairs one.
Invoke-Case 'private_ancestor_failure_refuses_before_any_child' {
    $missing = New-Workspace 'ancestor-missing'
    [void][IO.Directory]::CreateDirectory($missing.Root)
    $missingRun = Invoke-Launcher -Label 'run-missing' -Set 'Run' -Parameters ([ordered]@{
        WorkspaceRoot = $missing.Root; ExpectedHostname = $Hostname
        BinaryPath = $FixtureExe; ExpectedBinarySha256 = $FixtureExeSha256
        AttemptId = 'attempt-missing'; StagedConfigurationName = 'deployment.missing.json'
        ExpectedConfigurationSha256 = ('sha256:' + ('0' * 64)); ExpectedRunId = 'm6t.normal.missing'
        PlannedSeconds = $PlannedSeconds; CloseGraceSeconds = $CloseGraceSeconds
        ExitWaitExtraSeconds = $ExitWaitExtraSeconds; ReadyWaitSeconds = $ReadyWaitSeconds
    }) -WorkingDirectory $missing.CaseDirectory -OutputPrefix (Join-Path $missing.CaseDirectory 'run-missing') `
        -DeadlineMilliseconds $ShortLauncherDeadlineMilliseconds
    Assert-TestOwnerDidNotIntervene $missingRun
    Check ($missingRun.ExitCode -eq 65) ('expected identity exit 65, actual ' + $missingRun.ExitCode + '; ' + [string]$missingRun.Result.first_error)
    Check ([string]$missingRun.Result.first_error -match 'private directory missing') ('first error: ' + [string]$missingRun.Result.first_error)
    Assert-NoChildStarted $missing $null

    $weakened = Initialize-Workspace 'ancestor-acl'
    $deployment = New-StagedDeployment $weakened 'm6t.normal.f01' 'deployment.f01.json'
    Disable-AclProtection $weakened.StagingRoot
    $weakenedRun = Invoke-Launcher -Label 'run-acl' -Set 'Run' -Parameters (New-RunParameters $weakened $deployment 'attempt-f01') `
        -WorkingDirectory $weakened.CaseDirectory -OutputPrefix (Join-Path $weakened.CaseDirectory 'run-acl') `
        -DeadlineMilliseconds $ShortLauncherDeadlineMilliseconds
    Assert-TestOwnerDidNotIntervene $weakenedRun
    Check ($weakenedRun.ExitCode -eq 65) ('expected identity exit 65, actual ' + $weakenedRun.ExitCode + '; ' + [string]$weakenedRun.Result.first_error)
    Check ([string]$weakenedRun.Result.first_error -match 'ACL is not protected') ('first error: ' + [string]$weakenedRun.Result.first_error)
    Assert-NoChildStarted $weakened $deployment
    Check (Test-Path -LiteralPath $deployment.Path -PathType Leaf) 'the staged deployment was consumed despite the ACL refusal'
    [pscustomobject]@{ missing_exit = $missingRun.ExitCode; acl_exit = $weakenedRun.ExitCode
        missing_error = [string]$missingRun.Result.first_error; acl_error = [string]$weakenedRun.Result.first_error }
}

# Pinned binary and staged deployment identities are verified before any mutation or child.
Invoke-Case 'identity_drift_refuses_before_any_child' {
    $details = [Collections.Generic.List[object]]::new()
    foreach ($variant in @('binary', 'configuration')) {
        $workspace = Initialize-Workspace ('drift-' + $variant)
        $deployment = New-StagedDeployment $workspace ('m6t.normal.g' + $variant.Substring(0, 3)) ('deployment.g' + $variant.Substring(0, 3) + '.json')
        $binarySha = $FixtureExeSha256; $configurationSha = $deployment.Sha256
        $pattern = 'owner executable drift'
        if ($variant -ceq 'binary') { $binarySha = 'sha256:' + ('0' * 64) }
        else { $configurationSha = 'sha256:' + ('0' * 64); $pattern = 'staged deployment hash differs' }
        $run = Invoke-Launcher -Label ('run-' + $variant) -Set 'Run' -Parameters (
            New-RunParameters -Workspace $workspace -Deployment $deployment -AttemptId ('attempt-' + $variant) `
                -BinarySha256 $binarySha -ConfigurationSha256 $configurationSha) `
            -WorkingDirectory $workspace.CaseDirectory -OutputPrefix (Join-Path $workspace.CaseDirectory ('run-' + $variant)) `
            -DeadlineMilliseconds $ShortLauncherDeadlineMilliseconds
        Assert-TestOwnerDidNotIntervene $run
        Check ($run.ExitCode -eq 65) ($variant + ': expected identity exit 65, actual ' + $run.ExitCode + '; ' + [string]$run.Result.first_error)
        Check ([string]$run.Result.first_error -match $pattern) ($variant + ' first error: ' + [string]$run.Result.first_error)
        Assert-NoChildStarted $workspace $deployment
        Check (Test-Path -LiteralPath $deployment.Path -PathType Leaf) ($variant + ': the staged deployment was consumed despite the refusal')
        $details.Add([pscustomobject]@{ variant = $variant; exit_code = $run.ExitCode; first_error = [string]$run.Result.first_error })
    }
    [pscustomobject]@{ variants = @($details.ToArray()) }
}

# The child's own nonzero exit is preserved, never normalised.
Invoke-Case 'host_nonzero_exit_is_reported' {
    $workspace = Initialize-Workspace 'nonzero'
    $deployment = New-StagedDeployment $workspace 'm6t.nonzero.h01' 'deployment.h01.json'
    $attempt = Get-AttemptPaths $workspace 'attempt-h01'
    $run = Invoke-Launcher -Label 'run' -Set 'Run' -Parameters (New-RunParameters $workspace $deployment 'attempt-h01') `
        -WorkingDirectory $workspace.CaseDirectory -OutputPrefix (Join-Path $workspace.CaseDirectory 'run') `
        -DeadlineMilliseconds $ShortLauncherDeadlineMilliseconds
    Assert-TestOwnerDidNotIntervene $run
    Check ($run.ExitCode -eq 4) ('expected host-failure exit 4, actual ' + $run.ExitCode + '; ' + [string]$run.Result.first_error)
    Check ([string]$run.Result.first_error -match 'owner host exited 7') ('first error: ' + [string]$run.Result.first_error)
    $exit = Read-TestJson $attempt.ProcessExit
    Check ([int]$exit.exit_code -eq 7) ('the child nonzero exit was lost: ' + [string]$exit.exit_code)
    Check ([bool]$exit.ready_received) 'this case requires a valid READY before the nonzero exit'
    Check (-not [bool]$exit.forced_termination) 'a child that exited by itself must not be recorded as forced'
    [pscustomobject]@{ exit_code = $run.ExitCode; host_exit_code = [int]$exit.exit_code }
}

# A child that exits without ever reporting READY is not a success.
Invoke-Case 'absent_ready_is_not_a_successful_run' {
    $workspace = Initialize-Workspace 'no-ready'
    $deployment = New-StagedDeployment $workspace 'm6t.no_ready.i01' 'deployment.i01.json'
    $attempt = Get-AttemptPaths $workspace 'attempt-i01'
    $run = Invoke-Launcher -Label 'run' -Set 'Run' -Parameters (New-RunParameters $workspace $deployment 'attempt-i01') `
        -WorkingDirectory $workspace.CaseDirectory -OutputPrefix (Join-Path $workspace.CaseDirectory 'run') `
        -DeadlineMilliseconds $ShortLauncherDeadlineMilliseconds
    Assert-TestOwnerDidNotIntervene $run
    Check ($run.ExitCode -eq 70) ('expected launcher failure exit 70, actual ' + $run.ExitCode + '; ' + [string]$run.Result.first_error)
    Check ([string]$run.Result.first_error -match 'never reported READY') ('first error: ' + [string]$run.Result.first_error)
    Check (-not (Test-Path -LiteralPath $attempt.Ready)) 'a READY record exists although none was reported'
    $exit = Read-TestJson $attempt.ProcessExit
    Check (-not [bool]$exit.ready_received) 'the external exit record claims READY'
    Check ([int]$exit.exit_code -eq 0) 'the child exit code was lost'
    Check ([bool]$exit.process_handle_signaled) 'the child was not reaped'
    [pscustomobject]@{ exit_code = $run.ExitCode; host_exit_code = [int]$exit.exit_code }
}

# Adjacent READY-shape failures, combined: not JSON, an extra field, an anchor naming another run
# with a wrong hash, a fresh owner claiming a bound spec and journal prefix, and an oversized line.
Invoke-Case 'malformed_ready_is_refused_without_a_ready_record' {
    $details = [Collections.Generic.List[object]]::new()
    foreach ($mode in $MalformedReadyModes) {
        $short = $mode.Replace('ready_', '').Replace('_', '')
        if ($short.Length -gt 12) { $short = $short.Substring(0, 12) }
        $workspace = Initialize-Workspace ('ready-' + $short)
        $deployment = New-StagedDeployment $workspace ('m6t.' + $mode + '.' + $short) ('deployment.' + $short + '.json')
        $attempt = Get-AttemptPaths $workspace ('attempt-' + $short)
        $run = Invoke-Launcher -Label ('run-' + $short) -Set 'Run' -Parameters (New-RunParameters $workspace $deployment ('attempt-' + $short)) `
            -WorkingDirectory $workspace.CaseDirectory -OutputPrefix (Join-Path $workspace.CaseDirectory ('run-' + $short)) `
            -DeadlineMilliseconds $ShortLauncherDeadlineMilliseconds
        Assert-TestOwnerDidNotIntervene $run
        Check ($run.ExitCode -eq 70) ($mode + ': expected exit 70, actual ' + $run.ExitCode + '; ' + [string]$run.Result.first_error)
        Check ([string]$run.Result.first_error -match 'ready_invalid') ($mode + ' first error: ' + [string]$run.Result.first_error)
        Check (-not (Test-Path -LiteralPath $attempt.Ready)) ($mode + ': a READY record was persisted for a refused line')
        $exit = Read-TestJson $attempt.ProcessExit
        Check (-not [bool]$exit.ready_received) ($mode + ': the external exit record claims READY')
        Check ([bool]$exit.process_handle_signaled) ($mode + ': the child was not reaped')
        Check (-not (Test-ExactProcessAlive ([int]$exit.pid) ([long]$exit.creation_filetime_100ns))) ($mode + ': the child outlived the refusal')
        $details.Add([pscustomobject]@{ mode = $mode; exit_code = $run.ExitCode; first_error = [string]$run.Result.first_error })
    }
    [pscustomobject]@{ modes = @($details.ToArray()) }
}

# One absolute deadline from spawn, never renewed: a child that outlives it is terminated through
# the held handle and reaped, and the outcome is a visible failure.
Invoke-Case 'run_deadline_forces_termination_through_the_held_handle' {
    $workspace = Initialize-Workspace 'timeout'
    $deployment = New-StagedDeployment $workspace 'm6t.timeout.j01' 'deployment.j01.json'
    $attempt = Get-AttemptPaths $workspace 'attempt-j01'
    $run = Invoke-Launcher -Label 'run' -Set 'Run' -Parameters (New-RunParameters $workspace $deployment 'attempt-j01') `
        -WorkingDirectory $workspace.CaseDirectory -OutputPrefix (Join-Path $workspace.CaseDirectory 'run') `
        -DeadlineMilliseconds $LauncherDeadlineMilliseconds
    Assert-TestOwnerDidNotIntervene $run
    Check ($run.ExitCode -eq 70) ('expected launcher failure exit 70, actual ' + $run.ExitCode + '; ' + [string]$run.Result.first_error)
    Check ([string]$run.Result.first_error -match 'exceeded its deadline') ('first error: ' + [string]$run.Result.first_error)
    $exit = Read-TestJson $attempt.ProcessExit
    Check ([bool]$exit.forced_termination) 'the deadline termination was not recorded'
    Check ($null -eq $exit.cancel) 'a deadline termination must not be recorded as a cancel'
    Check ([bool]$exit.ready_received) 'this case requires a valid READY before the deadline'
    Check ([bool]$exit.process_handle_signaled) 'the terminated child was not reaped'
    Check ([long]$exit.elapsed_milliseconds -ge ($TotalDeadlineMilliseconds - 3000)) (
        'the launcher gave up after ' + [string]$exit.elapsed_milliseconds + ' ms, before its own deadline')
    Check ([long]$exit.total_wait_milliseconds -eq $TotalDeadlineMilliseconds) (
        'the recorded deadline is ' + [string]$exit.total_wait_milliseconds + ' ms')
    Check (-not (Test-ExactProcessAlive ([int]$exit.pid) ([long]$exit.creation_filetime_100ns))) 'the exact child outlived the deadline'
    [pscustomobject]@{ exit_code = $run.ExitCode; elapsed_ms = [long]$exit.elapsed_milliseconds
        total_wait_ms = [long]$exit.total_wait_milliseconds }
}

# Both pipes are drained continuously and retention stays bounded with an accurate total.
Invoke-Case 'concurrent_pipe_flood_is_drained_with_bounded_retention' {
    $workspace = Initialize-Workspace 'flood'
    $deployment = New-StagedDeployment $workspace 'm6t.flood.k01' 'deployment.k01.json'
    $attempt = Get-AttemptPaths $workspace 'attempt-k01'
    $run = Invoke-Launcher -Label 'run' -Set 'Run' -Parameters (New-RunParameters $workspace $deployment 'attempt-k01') `
        -WorkingDirectory $workspace.CaseDirectory -OutputPrefix (Join-Path $workspace.CaseDirectory 'run') `
        -DeadlineMilliseconds $ShortLauncherDeadlineMilliseconds
    Assert-TestOwnerDidNotIntervene $run
    Check ($run.ExitCode -eq 0) ('expected exit 0, actual ' + $run.ExitCode + '; ' + [string]$run.Result.first_error)
    $exit = Read-TestJson $attempt.ProcessExit
    # The fixture writes exactly 512 x 8192 ASCII bytes to each pipe; stdout also carries the READY line.
    Check ([long]$exit.stderr_total_bytes -eq 4194304) ('stderr drain accounting is ' + [string]$exit.stderr_total_bytes + ', expected 4194304')
    Check ([long]$exit.stdout_total_bytes -gt 4194304) ('stdout drain accounting is ' + [string]$exit.stdout_total_bytes)
    Check ($exit.stdout_retained.Length -lt 140000) ('stdout retention is ' + $exit.stdout_retained.Length + ' characters')
    Check ($exit.stderr_retained.Length -lt 140000) ('stderr retention is ' + $exit.stderr_retained.Length + ' characters')
    Check ([long]$exit.stderr_dropped_bytes -gt 0) 'a flood larger than the retention budget reported no dropped bytes'
    Check ([bool]$exit.ready_received) 'the READY line was lost in the flood'
    Check (-not [bool]$exit.forced_termination) 'a child that exited by itself must not be recorded as forced'
    [pscustomobject]@{ exit_code = $run.ExitCode; stdout_total_bytes = [long]$exit.stdout_total_bytes
        stderr_total_bytes = [long]$exit.stderr_total_bytes; stderr_dropped_bytes = [long]$exit.stderr_dropped_bytes
        stdout_retained_length = $exit.stdout_retained.Length }
}

# Evidence that cannot be persisted is a visible failure, never a quiet success. The artifact that
# blocks each record is created by the controlled child inside its own attempt directory.
Invoke-Case 'record_write_failure_fails_visibly' {
    $details = [Collections.Generic.List[object]]::new()
    foreach ($variant in @('ready', 'exit')) {
        $mode = $variant + '_write_failure'
        $workspace = Initialize-Workspace ('write-' + $variant)
        $deployment = New-StagedDeployment $workspace ('m6t.' + $mode + '.l' + $variant) ('deployment.l' + $variant + '.json')
        $attempt = Get-AttemptPaths $workspace ('attempt-l' + $variant)
        $run = Invoke-Launcher -Label ('run-' + $variant) -Set 'Run' -Parameters (New-RunParameters $workspace $deployment ('attempt-l' + $variant)) `
            -WorkingDirectory $workspace.CaseDirectory -OutputPrefix (Join-Path $workspace.CaseDirectory ('run-' + $variant)) `
            -DeadlineMilliseconds $ShortLauncherDeadlineMilliseconds
        Assert-TestOwnerDidNotIntervene $run
        Check ($run.ExitCode -eq 70) ($variant + ': expected exit 70, actual ' + $run.ExitCode + '; ' + [string]$run.Result.first_error)
        if ($variant -ceq 'ready') {
            Check (Test-Path -LiteralPath $attempt.Ready -PathType Container) 'the blocking artifact for the READY record is absent'
            Check ([string]$run.Result.first_error -match '^run:') ('first error: ' + [string]$run.Result.first_error)
            $exit = Read-TestJson $attempt.ProcessExit
            Check ([bool]$exit.ready_received) 'the accepted READY line was discarded when its record failed'
        } else {
            Check (Test-Path -LiteralPath $attempt.ProcessExit -PathType Container) 'the blocking artifact for the exit record is absent'
            Check ([string]$run.Result.first_error -match 'exit record could not be persisted') ('first error: ' + [string]$run.Result.first_error)
            # The external exit proof still reaches the controller on stdout even when it cannot be stored.
            Check ($run.Stdout -match 'M6-EXIT ') 'the unpersistable exit record was not emitted to the controller'
        }
        $details.Add([pscustomobject]@{ variant = $variant; exit_code = $run.ExitCode; first_error = [string]$run.Result.first_error })
    }
    [pscustomobject]@{ variants = @($details.ToArray()) }
}

# ---- receipt -------------------------------------------------------------------------------------
if ($CaseFilter -ne '' -and $Results.Count -eq 0) { throw ('the case filter matched no case: ' + $CaseFilter) }
Check ($Results.Count -gt 0) 'no case ran'
$Failed = @($Results | Where-Object { $_.status -cne 'pass' }).Count
$Final = [ordered]@{
    schema = 'independent-m6-owner-launcher-tests.v2'
    scope = 'independent production launcher Prepare/Run/Cancel regression; zero GPU, Docker, database, service or PDF operation'
    status = $(if ($Failed -eq 0) { 'pass' } else { 'fail' })
    hostname = $Hostname
    finished_utc = [DateTime]::UtcNow.ToString('o')
    launcher_sha256 = $LauncherSha256
    test_binary_sha256 = $FixtureExeSha256
    test_binary_sources = $TestBinarySources
    compiler_path = $Csc
    compiler_arguments = @($CompilerArguments)
    process_helper_sha256 = (Get-TestSha256File $ProcessHelper)
    declared_bounds = [ordered]@{ planned_seconds = $PlannedSeconds; close_grace_seconds = $CloseGraceSeconds
        exit_wait_extra_seconds = $ExitWaitExtraSeconds; ready_wait_seconds = $ReadyWaitSeconds
        bootstrap_bind_seconds = $BootstrapBindSeconds; total_deadline_milliseconds = $TotalDeadlineMilliseconds }
    case_filter = $CaseFilter
    failure_count = $Failed
    cases = @($Results.ToArray())
}
$Json = $Final | ConvertTo-Json -Depth 12
Write-NewTestText (Join-Path $OutputRoot 'evidence.json') $Json
$Json
if ($Failed -ne 0) { exit 1 }
exit 0
