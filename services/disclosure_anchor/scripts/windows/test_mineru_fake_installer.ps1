<#
.SYNOPSIS
Test-only declared stand-in for install_mineru_fixed_api.ps1, used by
test_mineru_installation_owner_independent.ps1 as the installation owner's Job child.

.DESCRIPTION
It performs no deployment: no Docker, no network, no service, no registry witness,
no C:\ProgramData access. It declares exactly the parameters the owner is
contracted to pass, records how PowerShell actually bound them, and then executes
the literal steps of a test-authored plan (declared file writes that stand in for a
daemon-side mutation the owner cannot undo, declared operation records, a bounded
hang, an explicit throw or exit code).

The bound-parameter record is written to the plan's own evidence directory before
anything else, so a regression in the owner's command construction (a quoted
'-Name' token binding as a positional value) is still visible even when
-OperationRecordDirectory itself failed to bind.

Every path this fixture writes is supplied by the test plan; nothing is derived
from the machine. Exit 90..93 are fixture faults and are never a product outcome.
#>
[CmdletBinding()]
param(
    [string]$ComposeSource = '',
    [string]$CollectorSource = '',
    [string]$CompatDockerfileSource = '',
    [string]$CompatPatcherSource = '',
    [string]$CapacityConfigSource = '',
    [string]$ExpectedCapacityConfigSha256 = '',
    [switch]$ApiOnlyCompatibilityUpgrade,
    [string]$ApiDeviceProfile = '',
    [string]$OperationRecordDirectory = '',
    [int]$OperationBudgetSeconds = 0,
    [Parameter(ValueFromRemainingArguments = $true)][AllowEmptyCollection()][string[]]$Remaining = @()
)
Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
$PlanContract = 'mineru.test-fake-installer-plan.v1'
$Utf8 = [Text.UTF8Encoding]::new($false, $true)
$MaximumHangMilliseconds = 600000

function Write-NewFixtureFile([string]$Path, [byte[]]$Bytes) {
    $stream = [IO.FileStream]::new($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try { $stream.Write($Bytes, 0, $Bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
}
function Write-NewFixtureText([string]$Path, [string]$Text) {
    Write-NewFixtureFile $Path ([Text.UTF8Encoding]::new($false).GetBytes($Text))
}
function Get-DeclaredText([object]$Entry) {
    $names = @($Entry.PSObject.Properties.Name | Sort-Object) -join ','
    if ($names -ceq 'path,text') { return [Text.UTF8Encoding]::new($false).GetBytes([string]$Entry.text) }
    if ($names -ceq 'copy_from,path') { return [IO.File]::ReadAllBytes([string]$Entry.copy_from) }
    throw ('declared write must be {path,text} or {path,copy_from}; actual: ' + $names)
}
# An empty declared collection can reach here as null, an empty string or an empty array depending
# on how it was serialized; anything else must be a real object or the plan is malformed.
function Get-DeclaredEntries([object]$Value) {
    if ($null -eq $Value) { return @() }
    if ($Value -is [string]) {
        if ($Value.Length -eq 0) { return @() }
        throw 'declared plan collection is a string'
    }
    $entries = @($Value)
    foreach ($entry in $entries) {
        if ($entry -isnot [Management.Automation.PSCustomObject]) {
            throw ('declared plan entry is not an object: ' + [string]$entry)
        }
    }
    return $entries
}

$planPath = [Environment]::GetEnvironmentVariable('MINERU_TEST_FAKE_INSTALLER_PLAN')
if ([string]::IsNullOrEmpty($planPath)) {
    [Console]::Error.Write('fake-installer-fixture-error: MINERU_TEST_FAKE_INSTALLER_PLAN is required')
    exit 90
}
$plan = $Utf8.GetString([IO.File]::ReadAllBytes($planPath)) | ConvertFrom-Json
if ([string]$plan.contract_version -cne $PlanContract) {
    [Console]::Error.Write('fake-installer-fixture-error: unsupported plan contract')
    exit 91
}
$evidence = [string]$plan.evidence_directory
if (-not (Test-Path -LiteralPath $evidence -PathType Container)) {
    [Console]::Error.Write('fake-installer-fixture-error: declared evidence directory is absent')
    exit 92
}

# Exactly how PowerShell bound this invocation, recorded before any action.
$bound = [ordered]@{}
foreach ($entry in $PSBoundParameters.GetEnumerator()) {
    $value = $entry.Value
    if ($value -is [Management.Automation.SwitchParameter]) { $value = [bool]$value.IsPresent }
    elseif ($value -is [array]) { $value = @($value | ForEach-Object { [string]$_ }) }
    $bound[$entry.Key] = $value
}
$self = [Diagnostics.Process]::GetCurrentProcess()
try {
    Write-NewFixtureText (Join-Path $evidence 'installer-bound-parameters.json') (([ordered]@{
        contract_version = 'mineru.test-fake-installer-binding.v1'
        bound_parameters = $bound
        bound_parameter_names = @($PSBoundParameters.Keys | Sort-Object)
        remaining_arguments = @($Remaining)
        raw_command_line = @([Environment]::GetCommandLineArgs())
        pid = $self.Id
        creation_filetime_100ns = $self.StartTime.ToFileTimeUtc()
        recorded_utc = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json -Compress -Depth 8))
} finally { $self.Dispose() }

# Mirrors the real installer: the operation record directory must be new.
if (-not [string]::IsNullOrEmpty($OperationRecordDirectory)) {
    $OperationRecordDirectory = [IO.Path]::GetFullPath($OperationRecordDirectory)
    if (Test-Path -LiteralPath $OperationRecordDirectory) { throw 'operation record directory must be new' }
    New-Item -ItemType Directory -Path $OperationRecordDirectory | Out-Null
}

if ([bool]$plan.write_alive_marker) {
    $alive = [Diagnostics.Process]::GetCurrentProcess()
    try {
        Write-NewFixtureText (Join-Path $evidence 'installer-alive.json') (([ordered]@{
            contract_version = 'mineru.test-fake-installer-alive.v1'
            pid = $alive.Id
            creation_filetime_100ns = $alive.StartTime.ToFileTimeUtc()
            started_utc = [DateTime]::UtcNow.ToString('o')
        } | ConvertTo-Json -Compress -Depth 4))
    } finally { $alive.Dispose() }
}

# Declared absolute writes stand in for a change the Docker daemon already
# accepted; the owner must never silently undo one of these.
foreach ($write in (Get-DeclaredEntries $plan.absolute_writes)) {
    Write-NewFixtureFile ([string]$write.path) (Get-DeclaredText $write)
}
foreach ($record in (Get-DeclaredEntries $plan.record_writes)) {
    if ([string]::IsNullOrEmpty($OperationRecordDirectory)) { throw 'declared operation record requires a bound -OperationRecordDirectory' }
    Write-NewFixtureText (Join-Path $OperationRecordDirectory ([string]$record.name)) ([string]$record.text)
}

$hang = [int]$plan.hang_milliseconds
if ($hang -lt 0 -or $hang -gt $MaximumHangMilliseconds) { throw 'declared hang is outside the finite fixture bound' }
# The alive marker, not console output, is the evidence that this child ran:
# the Job child inherits the owner's stdout handle and must not pollute it.
if ($hang -gt 0) { [Threading.Thread]::Sleep($hang) }

if (-not [string]::IsNullOrEmpty([string]$plan.throw_message)) { throw ([string]$plan.throw_message) }
exit ([int]$plan.exit_code)
