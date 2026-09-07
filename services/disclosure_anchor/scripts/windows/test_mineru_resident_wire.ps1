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
    $health = '{"status":"healthy","version":"3.4.4","protocol_version":2,"queued_tasks":0,"processing_tasks":1,"completed_tasks":9,"failed_tasks":0,"max_concurrent_requests":1,"max_pending_tasks_requested":1,"max_pending_tasks_effective":1,"processing_window_size":16,"task_retention_seconds":600,"task_cleanup_interval_seconds":30,"task_protocol_schema":"mineru-task-protocol.v2","task_protocol_runtime":{"schema":"mineru-task-runtime.v1","enabled":true,"task_registry_max_records":128,"task_result_reservation_bytes":268435456,"max_unacked_result_bytes":2147483648}}'
    $http = '{"contract_version":"mineru.api-http-request-snapshot.v1","process_id":7,"active_requests":2,"pending_requests":3}'
    $metricLines = @(
        'vllm:num_requests_running{engine="0",model_name="test-model"} 2.0',
        'vllm:num_requests_waiting{model_name="test-model",engine="0"} 3e0',
        'vllm:kv_cache_usage_perc{engine="0",model_name="test-model"} 0.000244140625',
        'vllm:num_preemptions_total{engine="0",model_name="test-model"} 1.0'
    )
    $metrics = $metricLines -join "`n"
    $queue = [MineruQueueTelemetry]::new(7,'test-model')
    $observation = [MineruResidentWire]::Parse($queue.Observe($health,$http,$metrics),65536)
    Assert-True ($observation.Get('values').Get('vllm_requests_waiting').Integer() -eq 3) 'exact_decimal_integer_count'
    Assert-True ($observation.Get('values').Get('vllm_kv_cache_usage_ratio').Raw -ceq '0.000244140625') 'metric_ratio_spelling_preserved'
    $null = $queue.Observe($health.Replace('"completed_tasks":9','"completed_tasks":0'),$http,$metrics)
    Assert-True $true 'health_terminal_gauge_can_decrease'
    $badCases = @(
        @{h=$health.Replace('"queued_tasks":0','"queued_tasks":true'); p=$http; m=$metrics},
        @{h=$health.Replace('"queued_tasks":0','"queued_tasks":1'); p=$http; m=$metrics},
        @{h=$health.Replace('"enabled":true','"enabled":false'); p=$http; m=$metrics},
        @{h=$health.Replace('"task_retention_seconds":600','"task_retention_seconds":599'); p=$http; m=$metrics},
        @{h=$health; p=$http.Replace('"process_id":7','"process_id":8'); m=$metrics},
        @{h=$health; p=$http.Replace('"active_requests":2','"active_requests":2.0'); m=$metrics},
        @{h=$health; p=$http; m=$metrics.Replace('engine="0"','engine="1"')},
        @{h=$health; p=$http; m=$metrics.Replace('test-model','wrong-model')},
        @{h=$health; p=$http; m=$metrics+"`n"+$metricLines[0]},
        @{h=$health; p=$http; m=$metrics.Replace('vllm:num_requests_running{','vllm:num_requests_running_alias{')},
        @{h=$health; p=$http; m=$metrics.Replace(' 2.0',' 1.00000000000000000000000000001')},
        @{h=$health; p=$http; m=$metrics.Replace(' 2.0',' 1e-999')},
        @{h=$health; p=$http; m=$metrics.Replace(' 2.0',' 9223372036854775808')},
        @{h=$health; p=$http; m=$metrics.Replace(' 2.0',' 1.5')},
        @{h=$health; p=$http; m=$metrics.Replace(' 0.000244140625',' NaN')},
        @{h=$health; p=$http; m=$metrics.Replace(' 0.000244140625',' 1.01')},
        @{h=$health; p=$http; m=$metrics.Replace('engine="0",','engine="0",engine="0",')},
        @{h=$health; p=$http; m=$metrics.Replace('test-model"}','test-model",}')},
        @{h=$health; p=$http; m=$metrics.Replace(' 1.0',' 0.0')}
    )
    foreach ($case in $badCases) {
        $rejected=$false
        try { $null=$queue.Observe($case.h,$case.p,$case.m) } catch { $rejected=$true }
        Assert-True $rejected 'queue_identity_shape_count_or_rollback_rejected'
    }
    $values = [MineruResidentWire]::Parse($queue.Observe($health,$http,$metrics.Replace(' 2.0',' 9223372036854775807')),65536).Get('values')
    Assert-True ($values.Get('vllm_requests_running').Integer() -eq [long]::MaxValue) 'int64_max_not_rounded'
    [ordered]@{ contract_version='mineru.resident-wire-mechanism-test.v1'; checks=$checks.ToArray();
        prepared_manifest_sha256=$ExpectedManifestSha256; source_sha256=$ExpectedWireSourceSha256 } | ConvertTo-Json -Depth 4 -Compress
} finally {
    foreach ($pin in $prepared.Pins) { $pin.Dispose() }
}
