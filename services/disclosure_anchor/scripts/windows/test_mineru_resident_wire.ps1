param(
    [Parameter(Mandatory = $true)][string]$ManifestPath,
    [Parameter(Mandatory = $true)][string]$ExpectedManifestSha256,
    [Parameter(Mandatory = $true)][string]$ExpectedNvmlSourceSha256,
    [Parameter(Mandatory = $true)][string]$ExpectedWireSourceSha256,
    [Parameter(Mandatory = $true)][string]$ExpectedSupervisorSourceSha256
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# Explicit standalone mechanism test, NOT a measured exporter or CPU receipt.
# The owner must independently verify the supplied prepared manifest first.
. ([IO.Path]::Combine($PSScriptRoot, 'load_mineru_telemetry_assembly.ps1'))
$prepared = Import-MineruTelemetryPreparedAssembly @PSBoundParameters
$checks = [Collections.Generic.List[string]]::new()
function Assert-True([bool]$Value, [string]$Name) {
    if (-not $Value) { throw ('assertion failed: ' + $Name) }
    $checks.Add($Name)
}
try {
    $raw = '{"a":{"ratio":0.000244140625,"zero":0.0},"z":1}'
    $parsed = [MineruResidentWire]::Parse($raw, 65536)
    $parsed.Keys([string[]]@('a','z'))
    Assert-True ($parsed.Get('a').Raw -ceq '{"ratio":0.000244140625,"zero":0.0}') 'raw_float_subtree_preserved'
    Assert-True ([MineruResidentWire]::Object([string[]]@('z','1','a',$parsed.Get('a').Raw)) -ceq $raw) 'ordinal_outer_object'
    Assert-True ($parsed.Get('z').Integer() -eq 1) 'strict_integer'
    $bad = [string[]]@(
        '{"a":1,"a":2}', '{"a":1,"\u0061":2}', '{"a":{"b":0,"b":1}}',
        '{"a":NaN}', '{"a":Infinity}', '{"a":1e999}', '{"a":01}', '{"a":+1}',
        '{"a":1.}', '{"a":.1}', '{"a":1e}', '{"a":true,}', '[0,]', '{}{}',
        '"unterminated', '"\x00"', '"\ud800"', '"\udc00"', ('[' * 40 + '0' + ']' * 40)
    )
    foreach ($invalidJson in $bad) {
        $rejected = $false
        try { $null = [MineruResidentWire]::Parse($invalidJson, 65536) } catch { $rejected = $true }
        Assert-True $rejected 'malformed_json_rejected'
    }
    $rejected = $false
    try { $null = [MineruResidentWire]::Parse('"' + ('x' * 65536) + '"',65536) } catch { $rejected = $true }
    Assert-True $rejected 'json_byte_bound'
    $rejected = $false
    try { $parsed.Keys([string[]]@('a','missing')) } catch { $rejected = $true }
    Assert-True $rejected 'exact_object_shape'
    $rejected = $false
    try { $null = [MineruResidentWire]::Parse('1.0',32).Integer() } catch { $rejected = $true }
    Assert-True $rejected 'float_not_integer'
    # Test-only stream subclass makes OS-read fragmentation deterministic.
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
public sealed class MineruOneByteProbeStream : MemoryStream {
    public MineruOneByteProbeStream(byte[] value) : base(value,false) {}
    public override Task<int> ReadAsync(byte[] value,int offset,int count,CancellationToken token) {
        token.ThrowIfCancellationRequested(); return Task.FromResult(Read(value,offset,Math.Min(count,1)));
    }
}
'@
    foreach ($fragmented in @($false,$true)) {
        $bytes = [Text.Encoding]::UTF8.GetBytes("{`"kind`":`"close`"}`n{`"kind`":`"closed`"}`n")
        $stream = if ($fragmented) { [MineruOneByteProbeStream]::new($bytes) } else { [IO.MemoryStream]::new($bytes,$false) }
        try {
            $reader = [MineruBoundedLineReader]::new($stream)
            $deadline = [MineruResidentWire]::Deadline(1000)
            Assert-True ($reader.Read($deadline) -ceq '{"kind":"close"}') 'first_close_frame'
            Assert-True ($reader.Read($deadline) -ceq '{"kind":"closed"}') 'second_closed_frame'
            Assert-True ($null -eq $reader.Read($deadline)) 'exact_stream_eof'
        } finally { $stream.Dispose() }
    }
    foreach ($badWire in @("{}`r`n",'{',('x' * 65537))) {
        $stream = [IO.MemoryStream]::new([Text.Encoding]::UTF8.GetBytes($badWire),$false)
        try {
            $reader = [MineruBoundedLineReader]::new($stream)
            $rejected = $false
            try { $null = $reader.Read([MineruResidentWire]::Deadline(1000)) } catch { $rejected = $true }
            Assert-True $rejected 'invalid_line_wire_rejected'
        } finally { $stream.Dispose() }
    }
    $task = [Threading.Tasks.Task]::Delay(100)
    $rejected = $false
    try { [MineruResidentWire]::Wait($task,[MineruResidentWire]::Deadline(10)) } catch { $rejected = $true }
    Assert-True $rejected 'absolute_task_deadline'
    $null = $task.GetAwaiter().GetResult()
    # --- host queue forwarding boundary --------------------------------------------------
    # The wire no longer mirrors the serving API's capacity rules. It binds the frozen
    # capacity identity at the source, proves the HTTP snapshot belongs to the sampled
    # serving process, digests vLLM exactly, and forwards the producer's health and HTTP
    # bytes unchanged. Every rule inside the health body - limits, admission counters,
    # stage owners, retention, the capacity observation's own owner PID/boot/start ticks
    # and the drain flags - is evaluated once by the Mac owner's canonical validator and is
    # covered by tests/unit/test_resident_capacity_bridge_independent.py. Demanding it here
    # too would only pin a second copy of those rules to this assembly.
    # Every identity below is synthetic: no live host, boot, epoch or capacity value is
    # tracked in this file.
    $capacityA = 'sha256:' + ('a1' * 32)
    $capacityB = 'sha256:' + ('b2' * 32)
    $healthTemplate = '{"status":"healthy","version":"3.4.4","protocol_version":2,"queued_tasks":0,"processing_tasks":1,"completed_tasks":9,"failed_tasks":0,"max_concurrent_requests":@PARSE@,"max_pending_tasks_requested":@TOTAL@,"max_pending_tasks_effective":@TOTAL@,"processing_window_size":16,"task_retention_seconds":600,"task_cleanup_interval_seconds":30,"task_protocol_schema":"mineru-task-protocol.v2","task_protocol_runtime":{"schema":"mineru-task-runtime.v3","enabled":true,"task_registry_max_records":128,"task_result_reservation_bytes":268435456,"max_unacked_result_bytes":2147483648,"registry_schema":"mineru-task-registry.v3","admission_scope":"post_form_owned_upload","capacity_config_sha256":"@RUNTIMESHA@"},"task_admission":{"schema":"mineru-task-admission.v1","registry_schema":"mineru-task-registry.v3","ingress_tasks":0,"accepted_pending_tasks":0,"accepted_processing_tasks":1,"accepted_finalizing_tasks":0,"durable_nonterminal_tasks":1,"routeless_accepted_tasks":0,"ingress_cleanup_tasks":0,"unowned_ingress_tasks":0,"nonterminal_limit":@TOTAL@,"scheduled_tasks":1,"queue_depth":0,"active_processors":1,"recovery_overcommitted":false,"admission_open":true,"blocked_reason":null},"capacity_observation":{"schema":"mineru.capacity-observation.v1","capacity_config_sha256":"@OBSERVEDSHA@","owner":{"process_id":7,"process_start_ticks":4242,"boot_id":"00000000-0000-4000-8000-000000000001","loop_epoch":"00000000-0000-4000-8000-000000000002"},"resolved_limits":{"parse_active_limit":@PARSE@,"total_nonterminal_limit":@TOTAL@,"finalizer_active_limit":@FINALIZER@,"result_reservation_bytes":268435456,"max_unacked_result_bytes":2147483648,"final_http_limit_per_loop":@HTTPLIMIT@},"http_limiter_state":"initialized","stage_counters":{"result_capacity_waiting":0,"parse_waiting":0,"parse_active":1,"finalizer_waiting":0,"finalizer_active":0},"http_counters":{"active_requests":2,"pending_requests":3},"owner_control":{"foreign_loop_observed":false,"soft_drain_requested":false,"soft_drain_applied":false,"trigger":null},"framework_limits":{"torch_intraop_threads":{"state":"available","value":4,"reason":null},"pdf_render_pool_max_workers":{"state":"available","value":3,"reason":null},"mkl_threads":{"state":"unavailable","value":null,"reason":"no_serving_getter"},"openblas_threads":{"state":"unavailable","value":null,"reason":"no_serving_getter"}},"observed_at":{"clock":"python.monotonic_ns","implementation":"clock_gettime(CLOCK_MONOTONIC)","started_ns":1000,"completed_ns":2000}}}'
    $healthA = $healthTemplate.Replace('@RUNTIMESHA@',$capacityA).Replace('@OBSERVEDSHA@',$capacityA).Replace('@PARSE@','7').Replace('@TOTAL@','8').Replace('@FINALIZER@','1').Replace('@HTTPLIMIT@','14')
    $healthB = $healthTemplate.Replace('@RUNTIMESHA@',$capacityB).Replace('@OBSERVEDSHA@',$capacityB).Replace('@PARSE@','2').Replace('@TOTAL@','4').Replace('@FINALIZER@','2').Replace('@HTTPLIMIT@','6')
    $http = '{"contract_version":"mineru.api-http-request-snapshot.v1","process_id":7,"active_requests":2,"pending_requests":3}'
    $metricLines = @(
        'vllm:num_requests_running{engine="0",model_name="test-model"} 2.0',
        'vllm:num_requests_waiting{engine="0",model_name="test-model"} 3',
        'vllm:kv_cache_usage_perc{engine="0",model_name="test-model"} 0.000244140625',
        'vllm:num_preemptions_total{engine="0",model_name="test-model"} 1.0'
    )
    $metrics = $metricLines -join "`n"
    foreach ($legal in @(@{s=$capacityA; h=$healthA}, @{s=$capacityB; h=$healthB})) {
        # Two legal capacity configurations, neither of them a literal this assembly knows.
        $forwarder = [MineruQueueTelemetry]::new(7,'test-model',$legal.s)
        $sample = [MineruResidentWire]::Parse($forwarder.Observe($legal.h,$http,$metrics),65536)
        $sample.Keys([string[]]@('reason','status','values'))
        Assert-True ($sample.Get('status').String() -ceq 'supported') 'queue_status_supported'
        $values = $sample.Get('values')
        $values.Keys([string[]]@('api_health','api_http','vllm'))
        # The producer's own bytes survive the string transport exactly, escaping included.
        Assert-True ($values.Get('api_health').String() -ceq $legal.h) 'raw_health_bytes_forwarded_exactly'
        Assert-True ($values.Get('api_http').String() -ceq $http) 'raw_http_bytes_forwarded_exactly'
        $vllm = $values.Get('vllm')
        $vllm.Keys([string[]]@('vllm_kv_cache_usage_ratio','vllm_preemptions_total','vllm_requests_running','vllm_requests_waiting'))
        Assert-True ($vllm.Get('vllm_requests_waiting').Integer() -eq 3) 'exact_decimal_integer_count'
        Assert-True ($vllm.Get('vllm_kv_cache_usage_ratio').Raw -ceq '0.000244140625') 'metric_ratio_spelling_preserved'
        Assert-True ($vllm.Get('vllm_requests_running').Integer() -eq 2) 'fractional_zero_metric_is_exact'
    }
    $queue = [MineruQueueTelemetry]::new(7,'test-model',$capacityA)
    $null = $queue.Observe($healthA,$http,$metrics)
    # The preemption counter may repeat inside one pinned epoch; only a decrease is drift.
    $null = $queue.Observe($healthA,$http,$metrics)
    Assert-True $true 'equal_preemption_counter_accepted'
    foreach ($badIdentity in @(
        @{pid=0; model='test-model'; sha=$capacityA},
        @{pid=7; model=''; sha=$capacityA},
        @{pid=7; model='test-model'; sha='sha256:' + ('A1' * 32)},
        @{pid=7; model='test-model'; sha=('a1' * 32)},
        @{pid=7; model='test-model'; sha='sha256:' + ('a1' * 31)})) {
        $rejected=$false
        try { $null=[MineruQueueTelemetry]::new($badIdentity.pid,$badIdentity.model,$badIdentity.sha) } catch { $rejected=$true }
        Assert-True $rejected 'frozen_capacity_identity_required'
    }
    $oversized = $healthA.Replace('clock_gettime(CLOCK_MONOTONIC)','clock_gettime(CLOCK_MONOTONIC)' + ('x' * 8192))
    $badCases = @(
        # Source binding: the serving process must claim the frozen capacity in both places.
        @{h=$healthB; p=$http; m=$metrics},
        @{h=$healthA.Replace('"capacity_config_sha256":"' + $capacityA + '"},"task_admission"','"capacity_config_sha256":"' + $capacityB + '"},"task_admission"'); p=$http; m=$metrics},
        @{h=$healthA.Replace('"mineru.capacity-observation.v1","capacity_config_sha256":"' + $capacityA,'"mineru.capacity-observation.v1","capacity_config_sha256":"' + $capacityB); p=$http; m=$metrics},
        @{h=$healthA.Replace('"capacity_config_sha256":"' + $capacityA + '"},"task_admission"','"capacity_config_sha256":null},"task_admission"'); p=$http; m=$metrics},
        # Wire mechanism on the forwarded document itself.
        @{h=$healthA.Replace('"status":"healthy"','"status":"healthy","status":"healthy"'); p=$http; m=$metrics},
        @{h=$healthA.Replace('"processing_window_size":16','"processing_window_size":16.0e'); p=$http; m=$metrics},
        @{h=$oversized; p=$http; m=$metrics},
        @{h='{"status":"healthy"}'; p=$http; m=$metrics},
        # HTTP snapshot identity and shape.
        @{h=$healthA; p=$http.Replace('"process_id":7','"process_id":8'); m=$metrics},
        @{h=$healthA; p=$http.Replace('mineru.api-http-request-snapshot.v1','mineru.api-http-request-snapshot.v2'); m=$metrics},
        @{h=$healthA; p=$http.Replace('"pending_requests":3','"pending_requests":3,"extra":0'); m=$metrics},
        @{h=$healthA; p=$http.Replace(',"pending_requests":3',''); m=$metrics},
        # vLLM series identity, exact counts and the pinned-epoch rollback boundary.
        @{h=$healthA; p=$http; m=$metrics.Replace('engine="0"','engine="1"')},
        @{h=$healthA; p=$http; m=$metrics.Replace('test-model','wrong-model')},
        @{h=$healthA; p=$http; m=$metrics + "`n" + $metricLines[0]},
        @{h=$healthA; p=$http; m=$metrics.Replace('vllm:num_requests_running{','vllm:num_requests_running_alias{')},
        @{h=$healthA; p=$http; m=($metricLines[0..2] -join "`n")},
        @{h=$healthA; p=$http; m=$metrics.Replace(' 2.0',' 1.00000000000000000000000000001')},
        @{h=$healthA; p=$http; m=$metrics.Replace(' 2.0',' 1e-999')},
        @{h=$healthA; p=$http; m=$metrics.Replace(' 2.0',' 9223372036854775808')},
        @{h=$healthA; p=$http; m=$metrics.Replace(' 2.0',' 1.5')},
        @{h=$healthA; p=$http; m=$metrics.Replace(' 0.000244140625',' NaN')},
        @{h=$healthA; p=$http; m=$metrics.Replace(' 0.000244140625',' 1.01')},
        @{h=$healthA; p=$http; m=$metrics.Replace('engine="0",','engine="0",engine="0",')},
        @{h=$healthA; p=$http; m=$metrics.Replace('test-model"}','test-model",}')},
        @{h=$healthA; p=$http; m=$metrics.Replace(' 1.0',' 0.0')}
    )
    foreach ($case in $badCases) {
        $rejected=$false
        try { $null=$queue.Observe($case.h,$case.p,$case.m) } catch { $rejected=$true }
        Assert-True $rejected 'queue_source_identity_shape_count_or_rollback_rejected'
    }
    $exact = [MineruResidentWire]::Parse($queue.Observe($healthA,$http,$metrics.Replace(' 2.0',' 9223372036854775807')),65536).Get('values')
    Assert-True ($exact.Get('vllm').Get('vllm_requests_running').Integer() -eq [long]::MaxValue) 'int64_max_not_rounded'
    [ordered]@{ contract_version='mineru.resident-wire-mechanism-test.v1'; checks=$checks.ToArray();
        prepared_manifest_sha256=$ExpectedManifestSha256; source_sha256=$ExpectedWireSourceSha256 } | ConvertTo-Json -Depth 4 -Compress
} finally {
    foreach ($pin in $prepared.Pins) { $pin.Dispose() }
}
