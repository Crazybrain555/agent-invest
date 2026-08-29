param(
    [Parameter(Mandatory = $true)][ValidateSet('gpu_fast', 'host_slow')][string]$Lane,
    [Parameter(Mandatory = $true)][ValidateRange(250, 1000)][int]$CadenceMilliseconds,
    [Parameter(Mandatory = $true)][ValidateRange(1024, 65536)][int]$Port,
    [Parameter(Mandatory = $true)][string]$IdentityJsonPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Runtime-unverified/default-off source.  The backend is loaded exactly once.  The
# sampling loop itself never starts powershell/ssh/docker/wsl/nvidia-smi or any
# other helper.  A missing backend is explicit unsupported evidence, never zero.
$identityBytes = [System.IO.File]::ReadAllBytes($IdentityJsonPath)
$identity = [System.Text.Encoding]::UTF8.GetString($identityBytes) | ConvertFrom-Json
function Get-MineruResidentTelemetrySample {
    param([string]$RequestedLane)
    $unsupported = [ordered]@{
        reason = 'collector_unsupported'
        status = 'unsupported'
        values = $null
    }
    if ($RequestedLane -eq 'gpu_fast') {
        return [ordered]@{ gpu = $unsupported }
    }
    return [ordered]@{
        api_process = $unsupported
        host_cgroup = $unsupported
        queue_vllm = $unsupported
    }
}

$listener = [System.Net.HttpListener]::new()
$listener.Prefixes.Add("http://127.0.0.1:$Port/")
$listener.Start()
$sequence = [int64]0
$latest = $null
$timer = [System.Diagnostics.Stopwatch]::StartNew()
$nextSample = [int64]0
$contextTask = $listener.GetContextAsync()
$waitingContext = $null
$waitingAfter = [int64]-1
function Complete-MineruTelemetryResponse {
    param($Context, [int]$StatusCode, $Payload)
    try {
        $Context.Response.StatusCode = $StatusCode
        if ($null -ne $Payload) {
            $json = $Payload | ConvertTo-Json -Depth 16 -Compress
            $body = [System.Text.Encoding]::UTF8.GetBytes($json)
            $Context.Response.ContentType = 'application/json'
            $Context.Response.ContentLength64 = $body.Length
            $Context.Response.OutputStream.Write($body, 0, $body.Length)
        }
    } catch {
        # A disconnected observer is transport evidence, not a reason to kill
        # the resident exporter.  The next after-sequence request must proceed.
    } finally {
        try { $Context.Response.OutputStream.Close() } catch { }
    }
}
try {
    while ($listener.IsListening) {
        if ($timer.ElapsedMilliseconds -ge $nextSample) {
            $sequence += 1
            try {
                $observation = Get-MineruResidentTelemetrySample -RequestedLane $Lane
                $observedAt = [DateTime]::UtcNow.ToString('o')
                $sampledNs = [int64]($timer.ElapsedTicks * (1000000000.0 / [System.Diagnostics.Stopwatch]::Frequency))
                if ($Lane -eq 'gpu_fast') {
                    $latest = [ordered]@{
                        contract_version = 'mineru.windows-resident-telemetry.v1'
                        gpu = $observation.gpu
                        identity = $identity
                        lane = $Lane
                        observed_at_utc = $observedAt
                        sampled_monotonic_ns = $sampledNs
                        sequence = $sequence
                    }
                } else {
                    $latest = [ordered]@{
                        api_process = $observation.api_process
                        contract_version = 'mineru.windows-resident-telemetry.v1'
                        host_cgroup = $observation.host_cgroup
                        identity = $identity
                        lane = $Lane
                        observed_at_utc = $observedAt
                        queue_vllm = $observation.queue_vllm
                        sampled_monotonic_ns = $sampledNs
                        sequence = $sequence
                    }
                }
            } catch {
                $unsupported = [ordered]@{
                    reason = 'collector_backend_error'
                    status = 'unsupported'
                    values = $null
                }
                $observedAt = [DateTime]::UtcNow.ToString('o')
                $sampledNs = [int64]($timer.ElapsedTicks * (1000000000.0 / [System.Diagnostics.Stopwatch]::Frequency))
                if ($Lane -eq 'gpu_fast') {
                    $latest = [ordered]@{
                        contract_version = 'mineru.windows-resident-telemetry.v1'; gpu = $unsupported
                        identity = $identity; lane = $Lane; observed_at_utc = $observedAt
                        sampled_monotonic_ns = $sampledNs; sequence = $sequence
                    }
                } else {
                    $latest = [ordered]@{
                        api_process = $unsupported; contract_version = 'mineru.windows-resident-telemetry.v1'
                        host_cgroup = $unsupported; identity = $identity; lane = $Lane
                        observed_at_utc = $observedAt; queue_vllm = $unsupported
                        sampled_monotonic_ns = $sampledNs; sequence = $sequence
                    }
                }
            }
            $nextSample += $CadenceMilliseconds
            if ($null -ne $waitingContext -and $sequence -ge $waitingAfter + 1) {
                if ($null -eq $latest) {
                    Complete-MineruTelemetryResponse -Context $waitingContext -StatusCode 503 -Payload $null
                } else {
                    Complete-MineruTelemetryResponse -Context $waitingContext -StatusCode 200 -Payload $latest
                }
                $waitingContext = $null
                $waitingAfter = [int64]-1
            }
        }
        if ($null -ne $waitingContext -or -not $contextTask.Wait([Math]::Max(1, $CadenceMilliseconds / 4))) { continue }
        $context = $contextTask.Result
        $contextTask = $listener.GetContextAsync()
        $match = [regex]::Match($context.Request.Url.AbsolutePath, "^/$Lane/after/([0-9]+)$")
        if ($context.Request.HttpMethod -ne 'GET' -or -not $match.Success) {
            Complete-MineruTelemetryResponse -Context $context -StatusCode 404 -Payload $null
            continue
        }
        $after = [int64]::Parse($match.Groups[1].Value)
        if ($after -eq 0) { $after = $sequence }
        if ($after -gt $sequence -or $after -lt $sequence - 1) {
            Complete-MineruTelemetryResponse -Context $context -StatusCode 409 -Payload $null
        } elseif ($after -eq $sequence - 1 -and $null -ne $latest) {
            Complete-MineruTelemetryResponse -Context $context -StatusCode 200 -Payload $latest
        } elseif ($after -eq $sequence) {
            $waitingContext = $context
            $waitingAfter = $after
        } else {
            Complete-MineruTelemetryResponse -Context $context -StatusCode 409 -Payload $null
        }
    }
} finally {
    if ($null -ne $waitingContext) {
        Complete-MineruTelemetryResponse -Context $waitingContext -StatusCode 503 -Payload $null
    }
    $listener.Close()
}
