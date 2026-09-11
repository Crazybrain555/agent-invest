<#
.SYNOPSIS
Runs the independent actual Windows owner parent/controller against a qualified build.
.DESCRIPTION
Five finite zero-PDF cases: normal, same-boot crash/resume and predecessor retry,
bad-tail preservation, pre-bind refusal followed by clean close, unbound watchdog.
Requires a prior standalone production12 build receipt, GPU UUID and pinned NVML
hash from the coordinator's authorized read-only preflight. No inference/Docker,
firewall, SSH, service, machine policy, ProgramData or PDF operation is performed.
Every process is held by an exact kernel handle and a test-only unnamed Job. Raw
output and failed artifacts remain in the fresh root. See AUTHORSHIP.md for bounds.
.EXAMPLE
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\test_mineru_m6_owner_host_live.ps1 -QualifiedBuildRoot C:\Users\help\workspaces\EXPLICIT\component-run -GpuUuid GPU-EXPLICIT -NvmlDllSha256 sha256:EXPLICIT -SourceCommit EXPLICIT -OutputRoot C:\Users\help\workspaces\EXPLICIT\live-run
#>
[CmdletBinding()]
param(
    [string]$SourceRoot = $PSScriptRoot,
    [Parameter(Mandatory = $true)][string]$QualifiedBuildRoot,
    [Parameter(Mandatory = $true)][ValidatePattern('^GPU-[a-fA-F0-9-]+$')][string]$GpuUuid,
    [Parameter(Mandatory = $true)][ValidatePattern('^sha256:[0-9a-f]{64}$')][string]$NvmlDllSha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceCommit,
    [string]$FixturePath = '',
    [string]$OutputRoot = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
if ($PSVersionTable.PSVersion.Major -ne 5 -or -not [Environment]::Is64BitProcess) { throw '64-bit Windows PowerShell 5.1 required' }
$SourceRoot = [IO.Path]::GetFullPath($SourceRoot)
$QualifiedBuildRoot = [IO.Path]::GetFullPath($QualifiedBuildRoot)
if ([string]::IsNullOrEmpty($OutputRoot)) { $OutputRoot = Join-Path ([IO.Path]::GetTempPath()) ('m6-native-live-' + [Guid]::NewGuid().ToString('N')) }
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
foreach ($forbidden in @('C:\ProgramData', 'C:\Windows', 'C:\Program Files')) { if ($OutputRoot.StartsWith($forbidden, [StringComparison]::OrdinalIgnoreCase)) { throw 'requires fresh disposable output root' } }
if (Test-Path -LiteralPath $OutputRoot) { throw 'output root must be fresh' }
if ([string]::IsNullOrEmpty($FixturePath)) { $FixturePath = Join-Path $SourceRoot '..\..\tests\fixtures\m6_owner\wire-vectors.v2.json' }
$FixturePath = [IO.Path]::GetFullPath($FixturePath)
function Sha { param([string]$Path) return 'sha256:' + (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
$receipt = Get-Content -LiteralPath (Join-Path $QualifiedBuildRoot 'runner-evidence.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$HostExe = Join-Path $QualifiedBuildRoot 'build\mineru_m6_owner_host.exe'
if ($receipt.production_build.exit_code -ne 0 -or (Sha $HostExe) -ne $receipt.production_exe_sha256) { throw 'qualified standalone production executable missing or drifted' }
$expectedNames = @('mineru_m6_owner_binding.cs','mineru_m6_owner_endpoint.cs','mineru_m6_owner_host.cs','mineru_m6_owner_identity.cs','mineru_m6_owner_journal.cs','mineru_m6_owner_platform.cs','mineru_m6_owner_wire.cs','mineru_m6_private_store.cs','mineru_m6_run_control.cs','mineru_m6_writer_guard.cs','mineru_nvml_backend.cs','mineru_resident_wire.cs')
if (@($receipt.production_sources.PSObject.Properties).Count -ne 12) { throw 'qualified manifest must contain exact production12' }
foreach ($name in $expectedNames) { if ((Sha (Join-Path $SourceRoot $name)) -ne $receipt.production_sources.$name) { throw "production source drift: $name" } }
if ((Sha $FixturePath) -ne $receipt.fixture_sha256) { throw 'fixture drifted from component qualification' }
$FrameworkDir = Join-Path (Split-Path -Parent ([Environment]::SystemDirectory)) 'Microsoft.NET\Framework64\v4.0.30319'
$Csc = Join-Path $FrameworkDir 'csc.exe'
if ((Sha $Csc) -ne $receipt.compiler_sha256) { throw 'compiler identity drifted' }
[void](New-Item -ItemType Directory -Path $OutputRoot)
$BuildRoot = Join-Path $OutputRoot 'build'; $WorkRoot = Join-Path $OutputRoot 'work'
[void](New-Item -ItemType Directory -Path $BuildRoot, $WorkRoot)
Add-Type -Path (Join-Path $SourceRoot 'test_mineru_m6_process.cs') -ErrorAction Stop
$ParentExe = Join-Path $BuildRoot 'independent_m6_live_parent.exe'
$compileArgs = @('/noconfig','/nologo','/utf8output','/warnaserror+','/target:exe','/platform:x64','/optimize-','/debug-',"/out:$ParentExe",'/main:M6LiveParent',"/lib:$FrameworkDir",'/r:System.dll','/r:System.Core.dll','/r:System.Web.Extensions.dll', (Join-Path $SourceRoot 'test_mineru_m6_process.cs'), (Join-Path $SourceRoot 'test_mineru_m6_owner_host_live.cs'))
$evidence = [ordered]@{ contract_version = 'm6.independent-live-runner.v1'; production_build_receipt_sha256 = (Sha (Join-Path $QualifiedBuildRoot 'runner-evidence.json')); production_exe_sha256 = (Sha $HostExe); compiler_sha256 = (Sha $Csc); live_cs_sha256 = (Sha (Join-Path $SourceRoot 'test_mineru_m6_owner_host_live.cs')); process_cs_sha256 = (Sha (Join-Path $SourceRoot 'test_mineru_m6_process.cs')); runner_sha256 = (Sha $PSCommandPath); fixture_sha256 = (Sha $FixturePath); source_commit_supplied_by_coordinator = $SourceCommit; output_root = $OutputRoot; pdf_count = 0 }
function Finish-Invocation {
    param([M6BoundedProcess]$Process, [int]$Milliseconds)
    $Process.Finish($Milliseconds)
    return [ordered]@{ pid = $Process.Pid; creation_filetime_100ns = $Process.CreationFiletime; exit_code = $Process.ExitCode; exact_handle_signaled = $Process.ExactProcessExited; active_job_processes = $Process.ActiveJobProcesses; total_job_processes = $Process.TotalJobProcesses; forced_termination = $Process.ForcedTermination; timed_out = $Process.TimedOut; cleanup_failure = $Process.CleanupFailure; stdout_path = $Process.StdoutPath; stderr_path = $Process.StderrPath }
}
$failures = New-Object Collections.ArrayList
$build = $null; $parent = $null
try {
    $build = New-Object M6BoundedProcess($Csc, $compileArgs, $BuildRoot, (Join-Path $OutputRoot 'compiler'))
    $evidence['compile'] = Finish-Invocation $build 300000
    if ($build.ExitCode -ne 0 -or $build.TimedOut -or $build.ForcedTermination) { throw ('independent live parent compile failed with exit ' + $build.ExitCode + '; raw diagnostics retained at ' + $build.StdoutPath + ' and ' + $build.StderrPath) }
    $build.Dispose(); $build = $null
    $arguments = @($HostExe, $FixturePath, $WorkRoot, $GpuUuid, [string]$receipt.production_source_manifest_sha256, $SourceCommit, $NvmlDllSha256)
    $parent = New-Object M6BoundedProcess($ParentExe, $arguments, $WorkRoot, (Join-Path $OutputRoot 'parent'))
    $evidence['parent'] = Finish-Invocation $parent 600000
    Write-Host $parent.ReadStdout()
    if ($parent.ExitCode -ne 0 -or $parent.TimedOut -or $parent.ForcedTermination) { throw ('actual host test parent failed; raw output retained: ' + $parent.ReadStderr()) }
    if (-not (Test-Path -LiteralPath (Join-Path $WorkRoot 'live-evidence.json'))) { throw 'actual live evidence missing' }
    $live = Get-Content -LiteralPath (Join-Path $WorkRoot 'live-evidence.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($live.failed -ne 0 -or @($live.scenarios).Count -ne 5) { throw 'not all five actual host scenarios passed' }
}
catch { [void]$failures.Add(($_ | Out-String)) }
finally {
    foreach ($held in @($build, $parent)) { if ($null -ne $held) { try { $held.Dispose() } catch { [void]$failures.Add(($_ | Out-String)) } } }
    $evidence['failures'] = @($failures.ToArray()); $evidence['failure_count'] = $failures.Count
    [IO.File]::WriteAllText((Join-Path $OutputRoot 'live-runner-evidence.json'), ($evidence | ConvertTo-Json -Depth 16), (New-Object Text.UTF8Encoding($false)))
}
if ($failures.Count) { $failures | ForEach-Object { Write-Host $_ }; exit 1 }
Write-Host 'PASS actual owner/control zero-PDF lifecycle boundaries. No M6/G2/hour/business qualification.'
exit 0
