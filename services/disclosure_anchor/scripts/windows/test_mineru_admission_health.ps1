<#
.SYNOPSIS
Independent pure installer-health and native queue-observer regressions, PS5.1.
.DESCRIPTION
Never runs the installer top level. AST loads two pure functions only. The
separately compiled, source-bound assembly is supplied by the execution owner.
Only MineruQueueTelemetry.Observe is invoked; no sockets, children, GPU or Docker.
Run this finite data suite under the owner's existing bounded execution wrapper.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InstallerPath,
    [Parameter(Mandatory = $true)][string]$ExpectedInstallerSha256,
    [Parameter(Mandatory = $true)][string]$WireSourcePath,
    [Parameter(Mandatory = $true)][string]$ExpectedWireSourceSha256,
    [Parameter(Mandatory = $true)][string]$WireAssemblyPath,
    [Parameter(Mandatory = $true)][string]$ExpectedWireAssemblySha256,
    [Parameter(Mandatory = $true)][string]$OutputRoot
)
Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'Windows PowerShell 5.1 required' }
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
foreach ($protected in @([Environment]::SystemDirectory, 'C:\ProgramData', 'C:\Program Files')) {
    if ($OutputRoot.StartsWith($protected, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'requires fresh disposable user output root'
    }
}
if (Test-Path -LiteralPath $OutputRoot) { throw 'output root must be new-only' }
[void](New-Item -ItemType Directory -Path $OutputRoot)
$script:Results = New-Object Collections.ArrayList
$script:SourcePins = New-Object Collections.ArrayList
$utf8 = New-Object Text.UTF8Encoding($false, $true)
function Pin-Input {
    param([string]$Path, [string]$Expected)
    if ($Expected -cnotmatch '^[0-9a-f]{64}$') { throw 'expected exact lower SHA256' }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -cne $Expected) { throw ('input source identity mismatch: ' + $Path) }
    [void]$script:SourcePins.Add([ordered]@{ path = [IO.Path]::GetFullPath($Path); sha256 = $actual })
}
function Check { param([bool]$Value, [string]$Message) if (-not $Value) { throw $Message } }
function Reject {
    param([scriptblock]$Action)
    $errorText = $null
    try { $null = & $Action } catch { $errorText = $_ | Out-String }
    if ($null -eq $errorText) { throw 'expected refusal, but operation succeeded' }
}
function Case {
    param([string]$Name, [scriptblock]$Action)
    try {
        $null = & $Action
        [void]$script:Results.Add([ordered]@{ name = $Name; status = 'pass' })
        Write-Output ('PASS ' + $Name)
    } catch {
        [void]$script:Results.Add([ordered]@{ name = $Name; status = 'fail'; error = ($_ | Out-String) })
        Write-Output ('FAIL ' + $Name + ': ' + $_)
    }
}
function New-Health {
    param([switch]$Legacy)
    $value = @'
{"status":"healthy","version":"3.4.4","protocol_version":2,"queued_tasks":0,"processing_tasks":0,"completed_tasks":0,"failed_tasks":0,"max_concurrent_requests":1,"max_pending_tasks_requested":1,"max_pending_tasks_effective":1,"processing_window_size":16,"task_retention_seconds":600,"task_cleanup_interval_seconds":30,"task_protocol_schema":"mineru-task-protocol.v2","task_protocol_runtime":{"schema":"mineru-task-runtime.v2","enabled":true,"task_registry_max_records":128,"task_result_reservation_bytes":268435456,"max_unacked_result_bytes":2147483648,"registry_schema":"mineru-task-registry.v3","admission_scope":"post_form_owned_upload"},"task_admission":{"schema":"mineru-task-admission.v1","registry_schema":"mineru-task-registry.v3","nonterminal_limit":1,"ingress_tasks":0,"accepted_pending_tasks":0,"accepted_processing_tasks":0,"accepted_finalizing_tasks":0,"durable_nonterminal_tasks":0,"routeless_accepted_tasks":0,"ingress_cleanup_tasks":0,"unowned_ingress_tasks":0,"scheduled_tasks":0,"queue_depth":0,"active_processors":0,"recovery_overcommitted":false,"admission_open":true,"blocked_reason":null}}
'@ | ConvertFrom-Json
    if ($Legacy) {
        $value.PSObject.Properties.Remove('task_admission')
        $value.task_protocol_runtime.schema = 'mineru-task-runtime.v1'
        $value.task_protocol_runtime.PSObject.Properties.Remove('registry_schema')
        $value.task_protocol_runtime.PSObject.Properties.Remove('admission_scope')
    }
    return $value
}
function New-Responsibility {
    param([string]$Phase)
    $value = New-Health
    $a = $value.task_admission
    $a.durable_nonterminal_tasks = 1; $a.admission_open = $false; $a.blocked_reason = 'capacity_full'
    switch ($Phase) {
        'ingress' { $value.queued_tasks = 1; $a.ingress_tasks = 1 }
        'pending' { $value.queued_tasks = 1; $a.accepted_pending_tasks = 1; $a.scheduled_tasks = 1; $a.queue_depth = 1 }
        'processing' { $value.processing_tasks = 1; $a.accepted_processing_tasks = 1; $a.scheduled_tasks = 1; $a.active_processors = 1 }
        'finalizing' { $value.processing_tasks = 1; $a.accepted_finalizing_tasks = 1; $a.scheduled_tasks = 1; $a.active_processors = 1 }
        'cleanup' { $value.queued_tasks = 1; $a.ingress_tasks = 1; $a.ingress_cleanup_tasks = 1; $a.blocked_reason = 'ingress_recovery_required' }
        'unowned' { $value.queued_tasks = 1; $a.ingress_tasks = 1; $a.unowned_ingress_tasks = 1; $a.blocked_reason = 'ingress_recovery_required' }
        'routeless' { $value.queued_tasks = 1; $a.accepted_pending_tasks = 1; $a.routeless_accepted_tasks = 1; $a.blocked_reason = 'accepted_recovery_required' }
        default { throw 'unknown literal phase' }
    }
    return $value
}
function Json { param([object]$Value) return ($Value | ConvertTo-Json -Depth 8 -Compress) }
function Invoke-Docker { throw 'SAFETY: Docker forbidden in independent pure tests' }
function Invoke-DockerProcess { throw 'SAFETY: Docker forbidden in independent pure tests' }
function Invoke-NativeProcess { throw 'SAFETY: native child forbidden in independent pure tests' }
function Invoke-RestMethod { throw 'SAFETY: HTTP forbidden in independent pure tests' }
$outerError = $null
try {
    Pin-Input $InstallerPath $ExpectedInstallerSha256
    Pin-Input $WireSourcePath $ExpectedWireSourceSha256
    Pin-Input $WireAssemblyPath $ExpectedWireAssemblySha256
    $tokens = $null; $parseErrors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile([IO.Path]::GetFullPath($InstallerPath), [ref]$tokens, [ref]$parseErrors)
    if (@($parseErrors).Count) { throw ($parseErrors | Out-String) }
    foreach ($name in @('Assert-RequiredProperties', 'Assert-IdleHealth')) {
        $nodes = @($ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true))
        if ($nodes.Count -ne 1) { throw ('expected one pure function ' + $name) }
        . ([ScriptBlock]::Create($nodes[0].Extent.Text))
    }

    Case 'installer explicit legacy idle allowed but candidate requires v2' {
        Assert-IdleHealth -Health (New-Health -Legacy) -Label 'old service'
        Reject { Assert-IdleHealth -Health (New-Health -Legacy) -Label 'candidate' -RequireAdmissionV2 }
        Assert-IdleHealth -Health (New-Health) -Label 'candidate' -RequireAdmissionV2
    }
    Case 'installer legacy branch does not accept a mixed v2 envelope' {
        $mixed = New-Health -Legacy
        $mixed | Add-Member -NotePropertyName task_admission -NotePropertyValue (New-Health).task_admission
        Reject { Assert-IdleHealth -Health $mixed -Label 'mixed legacy' }
        $mixed = New-Health -Legacy
        $mixed.task_protocol_runtime | Add-Member -NotePropertyName registry_schema -NotePropertyValue 'mineru-task-registry.v3'
        Reject { Assert-IdleHealth -Health $mixed -Label 'mixed legacy runtime' }
    }
    Case 'installer cannot report upload accepted finalizing or cleanup as idle' {
        foreach ($phase in @('ingress','pending','processing','finalizing','cleanup','unowned','routeless')) {
            $health = New-Responsibility $phase
            Reject { Assert-IdleHealth -Health $health -Label $phase -RequireAdmissionV2 }
            $health.queued_tasks = 0; $health.processing_tasks = 0
            Reject { Assert-IdleHealth -Health $health -Label ('false zero ' + $phase) -RequireAdmissionV2 }
        }
    }
    Case 'installer waits for exact scheduled and real processor closure' {
        foreach ($field in @('scheduled_tasks','queue_depth','active_processors')) {
            $health = New-Health; $health.task_admission.$field = 1
            Reject { Assert-IdleHealth -Health $health -Label $field -RequireAdmissionV2 }
        }
    }
    Case 'installer zero gauges and flags reject coercible scalars' {
        foreach ($field in @('queued_tasks','processing_tasks')) {
            foreach ($bad in @($false, '0', [double]0.5, [double]0)) {
                $health = New-Health; $health.$field = $bad
                Reject { Assert-IdleHealth -Health $health -Label $field -RequireAdmissionV2 }
            }
        }
        foreach ($field in @('ingress_tasks','durable_nonterminal_tasks','scheduled_tasks')) {
            foreach ($bad in @($false, '0', [double]0, -1)) {
                $health = New-Health; $health.task_admission.$field = $bad
                Reject { Assert-IdleHealth -Health $health -Label $field -RequireAdmissionV2 }
            }
        }
    }
    Case 'installer exact admission and runtime schema is mandatory' {
        $health = New-Health
        $names = @($health.task_admission.PSObject.Properties.Name)
        foreach ($field in $names) {
            $health = New-Health; $health.task_admission.PSObject.Properties.Remove($field)
            Reject { Assert-IdleHealth -Health $health -Label $field -RequireAdmissionV2 }
        }
        $health = New-Health; $health.task_admission | Add-Member -NotePropertyName extra -NotePropertyValue 0
        Reject { Assert-IdleHealth -Health $health -Label 'extra admission' -RequireAdmissionV2 }
        foreach ($field in @('registry_schema','admission_scope','schema')) {
            $health = New-Health; $health.task_protocol_runtime.$field = 'unknown'
            Reject { Assert-IdleHealth -Health $health -Label $field -RequireAdmissionV2 }
        }
    }
    Case 'installer closed admission or overcommit never qualifies idle' {
        foreach ($reason in @('shutting_down','worker_unavailable','ingress_recovery_required','accepted_recovery_required','recovery_overcommitted','capacity_full')) {
            $health = New-Health; $health.task_admission.admission_open = $false; $health.task_admission.blocked_reason = $reason
            Reject { Assert-IdleHealth -Health $health -Label $reason -RequireAdmissionV2 }
        }
        $health = New-Health; $health.task_admission.recovery_overcommitted = $true
        Reject { Assert-IdleHealth -Health $health -Label 'overcommitted' -RequireAdmissionV2 }
    }

    [void][Reflection.Assembly]::LoadFrom([IO.Path]::GetFullPath($WireAssemblyPath))
    # Installer legacy-read support is intentionally separate from current
    # capacity-bound telemetry. The raw queue bridge must not silently accept
    # either old envelope. Current-v3 native forwarding and metric cases live
    # in test_mineru_resident_wire.ps1; full admission semantics are exercised
    # through the one Python capacity validator and bridge tests.
    Case 'legacy installer health cannot masquerade as current capacity telemetry' {
        foreach ($health in @((New-Health -Legacy),(New-Health))) {
            $queue = [MineruQueueTelemetry]::new(7, 'independent-model', ('sha256:' + ('a' * 64)))
            Reject { $queue.Observe((Json $health), '{}', '') }
        }
    }
    foreach ($pin in $script:SourcePins) {
        Check ((Get-FileHash -LiteralPath $pin.path -Algorithm SHA256).Hash.ToLowerInvariant() -ceq $pin.sha256) ('input changed during suite: ' + $pin.path)
    }
} catch { $outerError = $_ | Out-String }
$failures = @($script:Results | Where-Object { $_.status -eq 'fail' }).Count
$receipt = [ordered]@{
    schema = 'independent-mineru-admission-health-tests.v1'
    pure_functions_only = $true; installer_top_level_executed = $false
    native_observer_only = $true; external_effects_requested = $false
    source_pins = @($script:SourcePins.ToArray()); cases = @($script:Results.ToArray())
    failure_count = $failures; outer_error = $outerError
}
[IO.File]::WriteAllText((Join-Path $OutputRoot 'receipt.json'), ($receipt | ConvertTo-Json -Depth 8), $utf8)
if ($null -ne $outerError) { Write-Output $outerError; exit 1 }
if ($failures -ne 0) { exit 1 }
if ($script:Results.Count -ne 8) { throw 'expected seven installer and one current/legacy boundary families' }
Write-Output 'PASS all 8 independent installer/current-capacity boundary families'
exit 0
