<#
.SYNOPSIS
Ordinary behavioral tests for the installation owner's closed idle proof (Read-IdleCapacityHealth).

.DESCRIPTION
Evaluates only the real Assert-Closed and Read-IdleCapacityHealth definitions from -WrapperPath, never the
owner's top level, under the owner's own Set-StrictMode -Version 2. Only the native readback is replaced, by
test-authored wire. The two positive bodies are actual serving health: the capacity-v1 API before the
2026-09-27 controlled install (evidence install-r1/health-before.json, sha256 4cf32171...) and the
storage-managed capacity-v2 API right after its cold start (install-failed-api-health.json, sha256
fbd634c9...). They are embedded as key-sorted compact re-serializations with identical values. Every negative
is one adjacent mutation of an actual body and must be refused for its own reason.
Windows PowerShell 5.1 only. Exit 0 when every case passes, 1 otherwise.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$WrapperPath,
    [string]$ReceiptPath=''
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'Windows PowerShell 5.1 required' }

$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile([IO.Path]::GetFullPath($WrapperPath),[ref]$tokens,[ref]$errors)
if (@($errors).Count) { throw ('wrapper parse failed: ' + ($errors | Out-String)) }
foreach ($name in @('Assert-Closed','Read-IdleCapacityHealth')) {
    $nodes=@($ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name },$true))
    if ($nodes.Count -ne 1) { throw ('expected one wrapper function ' + $name) }
    . ([ScriptBlock]::Create($nodes[0].Extent.Text))
}
$script:Wire=$null
function Invoke-BoundedReadback { param($FilePath,$Arguments) return [pscustomobject]@{ExitCode=0;StandardOutput=$script:Wire;StandardError=''} }

$V1Capacity='sha256:e56d1c8bd286906c83f81bc25339c7ef0eeb47a78d3f117e5f583529c6abd0ee'
$V2Capacity='sha256:560cb6efbefe986a4487600fc684391a19182998bebc57188ec61cc2e588e825'
$V2Policy='sha256:8f551f5c37068619a6680269f41f390601d50a2429f08dc57390b881dce05003'
$V1Health=@'
{"capacity_observation":{"capacity_config_sha256":"sha256:e56d1c8bd286906c83f81bc25339c7ef0eeb47a78d3f117e5f583529c6abd0ee","framework_limits":{"mkl_threads":{"reason":"no_serving_getter","state":"unavailable","value":null},"openblas_threads":{"reason":"no_serving_getter","state":"unavailable","value":null},"pdf_render_pool_max_workers":{"reason":null,"state":"available","value":3},"torch_intraop_threads":{"reason":null,"state":"available","value":4}},"http_counters":{"active_requests":0,"pending_requests":0},"http_limiter_state":"initialized","observed_at":{"clock":"python.monotonic_ns","completed_ns":1277504444502310,"implementation":"clock_gettime(CLOCK_MONOTONIC)","started_ns":1277504444450035},"owner":{"boot_id":"2d355740-a048-4498-8af2-e749e2e340d7","loop_epoch":"dd26e9cb-fd03-4fab-bd36-d54af91c286d","process_id":1,"process_start_ticks":90154987},"owner_control":{"foreign_loop_observed":false,"soft_drain_applied":false,"soft_drain_requested":false,"trigger":null},"resolved_limits":{"final_http_limit_per_loop":14,"finalizer_active_limit":1,"max_unacked_result_bytes":4294967296,"parse_active_limit":12,"result_reservation_bytes":268435456,"total_nonterminal_limit":14},"schema":"mineru.capacity-observation.v1","stage_counters":{"finalizer_active":0,"finalizer_waiting":0,"parse_active":0,"parse_waiting":0,"result_capacity_waiting":0}},"completed_tasks":0,"failed_tasks":0,"max_concurrent_requests":12,"max_pending_tasks_effective":14,"max_pending_tasks_requested":14,"processing_tasks":0,"processing_window_size":16,"protocol_version":2,"queued_tasks":0,"status":"healthy","task_admission":{"accepted_finalizing_tasks":0,"accepted_pending_tasks":0,"accepted_processing_tasks":0,"active_processors":0,"admission_open":true,"blocked_reason":null,"durable_nonterminal_tasks":0,"ingress_cleanup_tasks":0,"ingress_tasks":0,"nonterminal_limit":14,"queue_depth":0,"recovery_overcommitted":false,"registry_schema":"mineru-task-registry.v3","routeless_accepted_tasks":0,"scheduled_tasks":0,"schema":"mineru-task-admission.v1","unowned_ingress_tasks":0},"task_cleanup_interval_seconds":30,"task_protocol_runtime":{"admission_scope":"post_form_owned_upload","capacity_config_sha256":"sha256:e56d1c8bd286906c83f81bc25339c7ef0eeb47a78d3f117e5f583529c6abd0ee","enabled":true,"max_unacked_result_bytes":4294967296,"registry_schema":"mineru-task-registry.v3","schema":"mineru-task-runtime.v3","task_registry_max_records":128,"task_result_reservation_bytes":268435456},"task_protocol_schema":"mineru-task-protocol.v2","task_retention_seconds":600,"version":"3.4.4"}
'@
$V2Health=@'
{"capacity_observation":{"capacity_config_sha256":"sha256:560cb6efbefe986a4487600fc684391a19182998bebc57188ec61cc2e588e825","framework_limits":{"mkl_threads":{"reason":"no_serving_getter","state":"unavailable","value":null},"openblas_threads":{"reason":"no_serving_getter","state":"unavailable","value":null},"pdf_render_pool_max_workers":{"reason":"serving_pool_not_initialized","state":"unavailable","value":null},"torch_intraop_threads":{"reason":null,"state":"available","value":4}},"http_counters":{"active_requests":0,"pending_requests":0},"http_limiter_state":"not_initialized","observed_at":{"clock":"python.monotonic_ns","completed_ns":1277647296237586,"implementation":"clock_gettime(CLOCK_MONOTONIC)","started_ns":1277647296123178},"owner":{"boot_id":"2d355740-a048-4498-8af2-e749e2e340d7","loop_epoch":"a17f9849-374e-46f8-a403-a2f6ee7c1536","process_id":1,"process_start_ticks":127752948},"owner_control":{"foreign_loop_observed":false,"soft_drain_applied":false,"soft_drain_requested":false,"trigger":null},"resolved_limits":{"final_http_limit_per_loop":null,"finalizer_active_limit":1,"parse_active_limit":12,"total_nonterminal_limit":14},"result_storage":{"blocked_tasks":0,"completion_queue_depth":0,"growing_producers":0,"ingress_bytes":0,"outstanding_promise_bytes":0,"policy_sha256":"sha256:8f551f5c37068619a6680269f41f390601d50a2429f08dc57390b881dce05003","result_bytes":0,"source_bytes":0,"waiting_tasks":{"completion_capacity":0,"free_floor":0,"source_growth_capacity":0}},"schema":"mineru.capacity-observation.v2","stage_counters":{"completion_waiting":0,"finalizer_active":0,"finalizer_waiting":0,"parse_active":0,"parse_waiting":0,"result_capacity_waiting":0,"source_growth_waiting":0}},"completed_tasks":0,"failed_tasks":0,"max_concurrent_requests":12,"max_pending_tasks_effective":14,"max_pending_tasks_requested":14,"processing_tasks":0,"processing_window_size":16,"protocol_version":2,"queued_tasks":0,"status":"healthy","task_admission":{"accepted_finalizing_tasks":0,"accepted_pending_tasks":0,"accepted_processing_tasks":0,"active_processors":0,"admission_open":true,"blocked_reason":null,"durable_nonterminal_tasks":0,"ingress_cleanup_tasks":0,"ingress_tasks":0,"nonterminal_limit":14,"queue_depth":0,"recovery_overcommitted":false,"registry_schema":"mineru-task-registry.v4","routeless_accepted_tasks":0,"scheduled_tasks":0,"schema":"mineru-task-admission.v1","unowned_ingress_tasks":0},"task_cleanup_interval_seconds":30,"task_protocol_runtime":{"admission_scope":"post_form_owned_upload","capacity_config_sha256":"sha256:560cb6efbefe986a4487600fc684391a19182998bebc57188ec61cc2e588e825","enabled":true,"registry_schema":"mineru-task-registry.v4","result_storage_policy_sha256":"sha256:8f551f5c37068619a6680269f41f390601d50a2429f08dc57390b881dce05003","schema":"mineru-task-runtime.v4","task_registry_max_records":128},"task_protocol_schema":"mineru-task-protocol.v2","task_retention_seconds":600,"version":"3.4.4"}
'@

$script:Results=New-Object Collections.ArrayList
function Check([bool]$Condition,[string]$Message) { if (-not $Condition) { throw $Message } }
function Read-Wire([string]$Raw,[string]$Capacity) { $script:Wire=$Raw; return (Read-IdleCapacityHealth 'fixture-docker' $Capacity 'test') }
function Reject([string]$Raw,[string]$Capacity,[string]$Pattern,[string]$Context) {
    $caught=$null
    try { $null=Read-Wire $Raw $Capacity } catch { $caught=$_.Exception.Message }
    Check ($null -ne $caught) ('idle proof accepted: ' + $Context)
    Check ($caught -match $Pattern) ('refused for another reason (' + $Context + '): ' + $caught)
}
function Mutate([string]$Raw,[scriptblock]$Change) { $value=$Raw | ConvertFrom-Json; $null=& $Change $value; return ($value | ConvertTo-Json -Depth 20 -Compress) }
function Case([string]$Name,[scriptblock]$Body) {
    try { & $Body; [void]$script:Results.Add([ordered]@{name=$Name;status='pass'}); Write-Host ('PASS ' + $Name) }
    catch { [void]$script:Results.Add([ordered]@{name=$Name;status='fail';error=$_.Exception.Message}); Write-Host ('FAIL ' + $Name + ': ' + $_.Exception.Message) }
}

Case 'actual v1 health and the cold storage-v2 health are closed idle proofs' {
    $v1=Read-Wire $V1Health $V1Capacity
    Check ($v1.capacity_observation.schema -ceq 'mineru.capacity-observation.v1') 'v1 fixture drifted'
    $v2=Read-Wire $V2Health $V2Capacity
    Check ($v2.capacity_observation.http_limiter_state -ceq 'not_initialized') 'v2 fixture is not the cold limiter'
    Check ($null -eq $v2.capacity_observation.resolved_limits.final_http_limit_per_loop) 'cold limiter resolved no final HTTP limit'
    Check ($v2.capacity_observation.result_storage.policy_sha256 -ceq $V2Policy) 'v2 fixture policy drifted'
    # The limiter state is not idle evidence: an initialized limiter with zero counters is idle too.
    $null=Read-Wire (Mutate $V2Health { param($h) $h.capacity_observation.http_limiter_state='initialized'; $h.capacity_observation.resolved_limits.final_http_limit_per_loop=14 }) $V2Capacity
    # Retained source/result bytes are responsibility awaiting ACK, not work in progress.
    $null=Read-Wire (Mutate $V2Health { param($h) $h.capacity_observation.result_storage.source_bytes=1048576; $h.capacity_observation.result_storage.result_bytes=4096 }) $V2Capacity
}

Case 'the expected capacity identity stays exact in observation and runtime' {
    Reject $V2Health $V1Capacity 'capacity identity differs' 'v2 health against the v1 identity'
    Reject $V1Health $V2Capacity 'capacity identity differs' 'v1 health against the v2 identity'
    Reject (Mutate $V2Health { param($h) $h.task_protocol_runtime.capacity_config_sha256=$V1Capacity }) $V2Capacity 'task runtime differs' 'runtime names another capacity'
    Reject (Mutate $V1Health { param($h) $h.task_protocol_runtime.capacity_config_sha256=$V2Capacity }) $V1Capacity 'legacy task runtime differs' 'v1 runtime names another capacity'
}

Case 'the capacity version is one closed family, never a schema string' {
    Reject (Mutate $V1Health { param($h) $h.capacity_observation.schema='mineru.capacity-observation.v2' }) $V1Capacity 'storage ledger does not match' 'v1 body relabelled v2'
    Reject (Mutate $V2Health { param($h) $h.capacity_observation.schema='mineru.capacity-observation.v1' }) $V2Capacity 'storage ledger does not match' 'v2 body relabelled v1'
    Reject (Mutate $V2Health { param($h) $h.capacity_observation.PSObject.Properties.Remove('result_storage') }) $V2Capacity 'storage ledger does not match' 'v2 without its ledger'
    $ledger=($V2Health | ConvertFrom-Json).capacity_observation.result_storage
    Reject (Mutate $V1Health { param($h) $h.capacity_observation | Add-Member -NotePropertyName result_storage -NotePropertyValue $ledger }) $V1Capacity 'storage ledger does not match' 'v1 carrying a ledger'
    Reject (Mutate $V2Health { param($h) $h.task_admission.registry_schema='mineru-task-registry.v3' }) $V2Capacity 'task_admission schema tags' 'v2 with registry v3'
    Reject (Mutate $V1Health { param($h) $h.task_admission.registry_schema='mineru-task-registry.v4' }) $V1Capacity 'task_admission schema tags' 'v1 with registry v4'
    Reject (Mutate $V2Health { param($h) $h.capacity_observation.stage_counters.PSObject.Properties.Remove('source_growth_waiting') }) $V2Capacity 'stage_counters fields are not closed' 'v2 with the v1 stage set'
    $v2Stages={ param($h) foreach ($stage in @('source_growth_waiting','completion_waiting')) { $h.capacity_observation.stage_counters | Add-Member -NotePropertyName $stage -NotePropertyValue 0 } }
    Reject (Mutate $V1Health $v2Stages) $V1Capacity 'stage_counters fields are not closed' 'v1 with the v2 stage set'
    Reject (Mutate $V2Health { param($h) $h.capacity_observation.schema='mineru.capacity-observation.v3' }) $V2Capacity 'not a supported explicit-capacity version' 'unsupported observation version'
    Reject (Mutate $V1Health { param($h) $h.PSObject.Properties.Remove('task_protocol_runtime') }) $V1Capacity 'lacks task_protocol_runtime' 'v1 without its runtime'
    Reject (Mutate $V1Health { param($h) $h.task_protocol_runtime | Add-Member -NotePropertyName result_storage_policy_sha256 -NotePropertyValue $V2Policy }) $V1Capacity 'task_protocol_runtime fields are not closed' 'v1 runtime carrying a storage policy'
    Reject (Mutate $V1Health { param($h) $h.task_protocol_runtime.schema='mineru-task-runtime.v4' }) $V1Capacity 'legacy task runtime differs' 'v1 with runtime v4'
    Reject (Mutate $V1Health { param($h) $h.task_protocol_runtime.max_unacked_result_bytes=1 }) $V1Capacity 'legacy task runtime differs' 'v1 unacked budget below its reservation'
    Reject (Mutate $V2Health { param($h) $h.PSObject.Properties.Remove('task_protocol_runtime') }) $V2Capacity 'lacks task_protocol_runtime' 'v2 without its runtime'
    Reject (Mutate $V2Health { param($h) $h.task_protocol_runtime.schema='mineru-task-runtime.v3' }) $V2Capacity 'task runtime differs' 'v2 with runtime v3'
    Reject (Mutate $V2Health { param($h) $h.task_protocol_runtime.registry_schema='mineru-task-registry.v3' }) $V2Capacity 'task runtime differs' 'runtime registry v3'
    Reject (Mutate $V2Health { param($h) $h.task_protocol_runtime.enabled=$false }) $V2Capacity 'task runtime differs' 'runtime disabled'
    Reject (Mutate $V2Health { param($h) $h.task_protocol_runtime.task_registry_max_records=127 }) $V2Capacity 'task runtime differs' 'runtime record bound'
    Reject (Mutate $V2Health { param($h) $h.task_protocol_runtime | Add-Member -NotePropertyName task_result_reservation_bytes -NotePropertyValue 268435456 }) $V2Capacity 'task_protocol_runtime fields are not closed' 'v2 runtime carrying a v1 budget'
}

Case 'the storage ledger is closed, bound to the serving policy and idle' {
    $zero='sha256:' + ('0' * 64)
    Reject (Mutate $V2Health { param($h) $h.task_protocol_runtime.result_storage_policy_sha256=$zero }) $V2Capacity 'not bound to the serving storage policy' 'ledger and runtime name different policies'
    Reject (Mutate $V2Health { param($h) $h.capacity_observation.result_storage.policy_sha256=$V2Policy.ToUpperInvariant(); $h.task_protocol_runtime.result_storage_policy_sha256=$V2Policy.ToUpperInvariant() }) $V2Capacity 'not bound to the serving storage policy' 'non-canonical policy digest'
    Reject (Mutate $V2Health { param($h) $h.capacity_observation.result_storage.policy_sha256=($V2Policy + "`n"); $h.task_protocol_runtime.result_storage_policy_sha256=($V2Policy + "`n") }) $V2Capacity 'not bound to the serving storage policy' 'policy digest with a trailing newline'
    Reject (Mutate $V2Health { param($h) $h.capacity_observation.result_storage | Add-Member -NotePropertyName extra -NotePropertyValue 0 }) $V2Capacity 'result_storage fields are not closed' 'extra ledger field'
    Reject (Mutate $V2Health { param($h) $h.capacity_observation.result_storage.PSObject.Properties.Remove('blocked_tasks') }) $V2Capacity 'result_storage fields are not closed' 'missing ledger field'
    Reject (Mutate $V2Health { param($h) $h.capacity_observation.result_storage.waiting_tasks.PSObject.Properties.Remove('free_floor') }) $V2Capacity 'waiting_tasks fields are not closed' 'missing wait reason'
    foreach ($name in @('ingress_bytes','growing_producers','outstanding_promise_bytes','completion_queue_depth','blocked_tasks')) {
        Reject (Mutate $V2Health { param($h) $h.capacity_observation.result_storage.$name=1 }) $V2Capacity ('API is not idle: result_storage\.' + $name) ('busy ledger ' + $name)
    }
    foreach ($name in @('completion_capacity','free_floor','source_growth_capacity')) {
        Reject (Mutate $V2Health { param($h) $h.capacity_observation.result_storage.waiting_tasks.$name=1 }) $V2Capacity ('API is not idle: result_storage\.waiting_tasks\.' + $name) ('waiting ' + $name)
    }
    foreach ($name in @('source_growth_waiting','completion_waiting')) {
        Reject (Mutate $V2Health { param($h) $h.capacity_observation.stage_counters.$name=1 }) $V2Capacity ('API is not idle: stage_counters\.' + $name) ('storage stage ' + $name)
    }
    Reject (Mutate $V2Health { param($h) $h.capacity_observation.result_storage.source_bytes='0' }) $V2Capacity 'source_bytes is not a non-negative integer' 'string source bytes'
    Reject (Mutate $V2Health { param($h) $h.capacity_observation.result_storage.result_bytes=-1 }) $V2Capacity 'result_bytes is not a non-negative integer' 'negative result bytes'
    Reject (Mutate $V2Health { param($h) $h.capacity_observation.result_storage.ingress_bytes=0.5 }) $V2Capacity 'ingress_bytes is not an integer' 'fractional ingress'
    Reject (Mutate $V2Health { param($h) $h.capacity_observation.result_storage.blocked_tasks=$false }) $V2Capacity 'blocked_tasks is not an integer' 'boolean hold count'
    Reject (Mutate $V2Health { param($h) $h.capacity_observation.result_storage.waiting_tasks.free_floor='0' }) $V2Capacity 'waiting_tasks\.free_floor is not an integer' 'string wait count'
}

Case 'shared admission and owner refusals hold for storage health' {
    Reject (Mutate $V2Health { param($h) $h.task_admission.durable_nonterminal_tasks=1 }) $V2Capacity 'API is not idle: task_admission\.durable_nonterminal_tasks' 'accepted responsibility'
    Reject (Mutate $V2Health { param($h) $h.capacity_observation.owner_control.soft_drain_requested=$true }) $V2Capacity 'API is not idle: owner_control\.soft_drain_requested' 'draining owner'
    Reject (Mutate $V2Health { param($h) $h.queued_tasks=1 }) $V2Capacity 'API is not idle: queued_tasks' 'queued gauge'
}

$failed=@($script:Results | Where-Object { $_.status -cne 'pass' }).Count
if (-not [string]::IsNullOrEmpty($ReceiptPath)) {
    $receipt=[ordered]@{ schema='mineru-installation-idle-health-tests.v1'; powershell_version=$PSVersionTable.PSVersion.ToString()
        wrapper_sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $WrapperPath).Hash.ToLowerInvariant()
        test_sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $PSCommandPath).Hash.ToLowerInvariant()
        wrapper_top_level_executed=$false; native_or_network_executed=$false
        cases=@($script:Results.ToArray()); failed=$failed }
    [IO.File]::WriteAllText($ReceiptPath,($receipt | ConvertTo-Json -Depth 10),[Text.UTF8Encoding]::new($false))
}
Write-Host ('RESULT ' + ($script:Results.Count - $failed) + ' pass ' + $failed + ' fail')
if ($failed) { exit 1 }
exit 0
