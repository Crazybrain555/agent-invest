[CmdletBinding()]
param(
    [string]$ComposePath = "C:\ProgramData\compose.tailnet.yaml",
    [string]$OutputRoot = "C:\ProgramData\agent-invest\mineru-api-output",
    [switch]$CapacitySample,
    [switch]$PhaseTrace,
    [string]$TraceSinceUtc = "",
    [string]$TraceUntilUtc = "",
    [ValidateSet("legacy", "candidate")][string]$ExpectedCapacityMode = "legacy",
    [string]$ExpectedProfileSha256 = "",
    [ValidateRange(1, 200000)][int]$MaxTraceLines = 100000,
    [ValidateRange(1024, 268435456)][int]$MaxTraceBytes = 67108864
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$DockerCommand = (
    Get-Command docker.exe -CommandType Application -ErrorAction Stop |
        Select-Object -First 1 -ExpandProperty Source
)
if (
    [string]::IsNullOrWhiteSpace($DockerCommand) -or
    [IO.Path]::GetExtension($DockerCommand) -ine ".exe" -or
    -not (Test-Path -LiteralPath $DockerCommand -PathType Leaf)
) {
    throw "Docker CLI must resolve to one executable application"
}

if ($CapacitySample -and $PhaseTrace) {
    throw "capacity sampling and phase-trace capture are mutually exclusive"
}
if (-not $PhaseTrace -and (
    -not [string]::IsNullOrWhiteSpace($TraceSinceUtc) -or
    -not [string]::IsNullOrWhiteSpace($TraceUntilUtc) -or
    -not [string]::IsNullOrWhiteSpace($ExpectedProfileSha256)
)) {
    throw "phase-trace arguments require -PhaseTrace"
}

function Get-Sha256Text {
    param([Parameter(Mandatory = $true)][string]$Value)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { $hash = $algorithm.ComputeHash($bytes) }
    finally { $algorithm.Dispose() }
    $hex = -join ($hash | ForEach-Object { $_.ToString("x2") })
    return "sha256:$hex"
}

function Get-ContainerCapacitySample {
    param([Parameter(Mandatory = $true)][object]$Container)
    $name = ([string]$Container.Name).TrimStart("/")
    $probeCode = @'
import json
from pathlib import Path

def text(path):
    return Path(path).read_text(encoding="utf-8").strip()

def bounded(path):
    value = text(path)
    return None if value == "max" else int(value)

status = {}
for line in text("/proc/1/status").splitlines():
    if ":" in line:
        key, value = line.split(":", 1)
        status[key] = value.strip()

events = {}
for line in text("/sys/fs/cgroup/memory.events").splitlines():
    key, value = line.split()
    events[key] = int(value)

meminfo = {}
for line in text("/proc/meminfo").splitlines():
    if ":" in line:
        key, value = line.split(":", 1)
        meminfo[key] = int(value.strip().split()[0]) * 1024

def status_bytes(key):
    value = status.get(key, "0 kB").split()[0]
    return int(value) * 1024

print(json.dumps({
    "memory_current_bytes": int(text("/sys/fs/cgroup/memory.current")),
    "memory_max_bytes": bounded("/sys/fs/cgroup/memory.max"),
    "memory_events": events,
    "pid1_rss_bytes": status_bytes("VmRSS"),
    "pid1_rss_hwm_bytes": status_bytes("VmHWM"),
    "docker_vm_memory_total_bytes": meminfo.get("MemTotal", 0),
    "docker_vm_memory_available_bytes": meminfo.get("MemAvailable", 0),
}, separators=(",", ":")))
'@
    $probeJson = @(
        $probeCode | & $DockerCommand exec -i $name /usr/bin/python3.12 -I -
    )
    if ($LASTEXITCODE -ne 0 -or $probeJson.Count -ne 1) {
        throw "cannot sample cgroup and RSS for $name"
    }
    $probe = ([string]$probeJson[0]) | ConvertFrom-Json
    return [ordered]@{
        name = $name
        id = [string]$Container.Id
        started_at_utc = [string]$Container.State.StartedAt
        restart_count = [int]$Container.RestartCount
        oom_killed = [bool]$Container.State.OOMKilled
        exit_code = [int]$Container.State.ExitCode
        running = [bool]$Container.State.Running
        status = [string]$Container.State.Status
        health = [string]$Container.State.Health.Status
        pid = [int]$Container.State.Pid
        memory_current_bytes = [long]$probe.memory_current_bytes
        memory_max_bytes = if ($null -eq $probe.memory_max_bytes) { $null } else { [long]$probe.memory_max_bytes }
        memory_events = $probe.memory_events
        pid1_rss_bytes = [long]$probe.pid1_rss_bytes
        pid1_rss_hwm_bytes = [long]$probe.pid1_rss_hwm_bytes
        docker_vm_memory_total_bytes = [long]$probe.docker_vm_memory_total_bytes
        docker_vm_memory_available_bytes = [long]$probe.docker_vm_memory_available_bytes
    }
}

if ($CapacitySample) {
    $capacityInspect = & $DockerCommand inspect mineru-api mineru-api-proxy mineru-openai-server | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or @($capacityInspect).Count -ne 3) {
        throw "cannot inspect all MinerU containers for capacity sampling"
    }
    $machineGuid = (Get-ItemProperty -LiteralPath "HKLM:\SOFTWARE\Microsoft\Cryptography" -Name MachineGuid).MachineGuid
    $capacityResult = [ordered]@{
        schema = "mineru-host-capacity-sample.v1"
        observed_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        collector_path = $PSCommandPath
        collector_sha256 = "sha256:$((Get-FileHash -Algorithm SHA256 -LiteralPath $PSCommandPath).Hash.ToLowerInvariant())"
        windows_node_identity_sha256 = Get-Sha256Text -Value ([string]$machineGuid).Trim().ToLowerInvariant()
        containers = @(
            $capacityInspect |
                Sort-Object { ([string]$_.Name).TrimStart("/") } |
                ForEach-Object { Get-ContainerCapacitySample -Container $_ }
        )
    }
    $capacityResult | ConvertTo-Json -Depth 20 -Compress
    exit 0
}

function Convert-EnvironmentToMap {
    param([Parameter(Mandatory = $true)][object[]]$Values)
    $result = [ordered]@{}
    foreach ($item in $Values) {
        $name, $value = ([string]$item).Split("=", 2)
        if ([string]::IsNullOrWhiteSpace($name) -or $result.Contains($name)) {
            throw "container environment is ambiguous"
        }
        $result[$name] = $value
    }
    return $result
}

function Select-ExactEnvironment {
    param(
        [Parameter(Mandatory = $true)][object[]]$ActualValues,
        [Parameter(Mandatory = $true)][object[]]$ImageValues,
        [Parameter(Mandatory = $true)][object]$ResolvedValues,
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [string[]]$AllowedNames
    )
    $resolvedNames = @($ResolvedValues.PSObject.Properties.Name | Sort-Object)
    if (($resolvedNames -join ",") -ne (($AllowedNames | Sort-Object) -join ",")) {
        throw "compose service environment contains missing or additional fields"
    }
    $actual = Convert-EnvironmentToMap -Values $ActualValues
    $expectedActual = Convert-EnvironmentToMap -Values $ImageValues
    foreach ($property in $ResolvedValues.PSObject.Properties) {
        $expectedActual[$property.Name] = [string]$property.Value
    }
    if (
        (@($actual.Keys | Sort-Object) -join ",") -ne
            (@($expectedActual.Keys | Sort-Object) -join ",")
    ) {
        throw "actual container environment has undeclared additions or omissions"
    }
    foreach ($name in $expectedActual.Keys) {
        if ([string]$actual[$name] -ne [string]$expectedActual[$name]) {
            throw "actual container environment drifted from image plus compose for $name"
        }
    }
    $selected = [ordered]@{}
    foreach ($name in ($AllowedNames | Sort-Object)) {
        if (-not $actual.Contains($name)) { throw "actual container environment is missing $name" }
        $resolvedValue = [string]$ResolvedValues.$name
        if ([string]$actual[$name] -ne $resolvedValue) {
            throw "actual container environment drifted for $name"
        }
        $selected[$name] = $resolvedValue
    }
    $sensitiveNames = @(
        $actual.Keys | Where-Object {
            $_ -match "(?i)(proxy|password|secret|token|api.?key|access.?key)"
        }
    )
    if ($sensitiveNames.Count -ne 0) {
        throw "runtime container environment contains credential/proxy-like fields"
    }
    return $selected
}

if ($PhaseTrace) {
    if (
        [string]::IsNullOrWhiteSpace($TraceSinceUtc) -or
        [string]::IsNullOrWhiteSpace($TraceUntilUtc) -or
        $ExpectedProfileSha256 -notmatch '^sha256:[a-f0-9]{64}$'
    ) {
        throw "phase-trace capture requires UTC bounds and one profile hash"
    }
    $style = [Globalization.DateTimeStyles]::RoundtripKind
    $culture = [Globalization.CultureInfo]::InvariantCulture
    $since = [DateTimeOffset]::Parse($TraceSinceUtc, $culture, $style)
    $until = [DateTimeOffset]::Parse($TraceUntilUtc, $culture, $style)
    if (
        $since.Offset -ne [TimeSpan]::Zero -or
        $until.Offset -ne [TimeSpan]::Zero -or
        $until -le $since -or
        ($until - $since).TotalHours -gt 6
    ) {
        throw "phase-trace capture bounds must be increasing UTC within six hours"
    }

    $traceInspect = @(& $DockerCommand inspect mineru-api | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0 -or $traceInspect.Count -ne 1) {
        throw "cannot inspect MinerU API for phase-trace capture"
    }
    $traceContainer = $traceInspect[0]
    if (
        -not [bool]$traceContainer.State.Running -or
        [string]$traceContainer.State.Health.Status -ne "healthy" -or
        [int]$traceContainer.RestartCount -ne 0 -or
        [bool]$traceContainer.State.OOMKilled
    ) {
        throw "MinerU API is not a clean phase-trace source"
    }
    $traceEnvironment = Convert-EnvironmentToMap -Values @($traceContainer.Config.Env)
    $traceSwitch = ([string]$traceEnvironment["MINERU_PHASE_TRACE"]).Trim().ToLowerInvariant()
    if ($traceSwitch -notin @("1", "true", "yes", "on")) {
        throw "MinerU phase trace is not enabled"
    }
    if ([string]$traceEnvironment["MINERU_CAPACITY_MODE"] -ne $ExpectedCapacityMode) {
        throw "MinerU capacity mode drifted during phase-trace capture"
    }

    $profileProbeCode = @'
import json
import os
from mineru.utils.model_utils import (
    capacity_runtime_status,
    is_phase_trace_enabled,
    legacy_capacity_execution_profile,
)

window_size = int(os.environ["MINERU_PROCESSING_WINDOW_SIZE"])
runtime = capacity_runtime_status(window_size)
if runtime["mode"] == "legacy":
    active_profile_sha256 = legacy_capacity_execution_profile(
        window_size
    ).profile_sha256
else:
    candidate = runtime["candidate_profile"]
    if candidate is None or not runtime["nonlegacy_admission_enabled"]:
        raise RuntimeError("candidate admission is not enabled")
    active_profile_sha256 = candidate["profile_sha256"]
print(json.dumps({
    "active_profile_sha256": active_profile_sha256,
    "capacity_mode": runtime["mode"],
    "phase_trace_enabled": is_phase_trace_enabled(),
}, sort_keys=True, separators=(",", ":")))
'@
    $profileProbeOutput = @(
        $profileProbeCode | & $DockerCommand exec -i mineru-api /usr/bin/python3.12 -I -
    )
    if ($LASTEXITCODE -ne 0 -or $profileProbeOutput.Count -ne 1) {
        throw "cannot bind the active MinerU phase-trace profile"
    }
    $profileProbe = ([string]$profileProbeOutput[0]) | ConvertFrom-Json
    if (
        -not [bool]$profileProbe.phase_trace_enabled -or
        [string]$profileProbe.capacity_mode -ne $ExpectedCapacityMode -or
        [string]$profileProbe.active_profile_sha256 -ne $ExpectedProfileSha256
    ) {
        throw "active MinerU phase-trace profile drifted"
    }

    $rawLogLines = @(
        & $DockerCommand logs --since ($since.ToString("o")) --until ($until.ToString("o")) `
            mineru-api 2>&1 | ForEach-Object { [string]$_ }
    )
    if ($LASTEXITCODE -ne 0) {
        throw "cannot read bounded MinerU API logs"
    }
    $traceLines = @(
        $rawLogLines | Where-Object { $_.StartsWith("MINERU_PHASE_TRACE ") }
    )
    if ($traceLines.Count -eq 0 -or $traceLines.Count -gt $MaxTraceLines) {
        throw "phase-trace line count is empty or exceeds its bound"
    }
    [long]$traceByteCount = 0
    foreach ($line in $traceLines) {
        $traceByteCount += [Text.Encoding]::UTF8.GetByteCount($line) + 1
        if ($traceByteCount -gt $MaxTraceBytes) {
            throw "phase-trace bytes exceed their bound"
        }
        $event = $line.Substring("MINERU_PHASE_TRACE ".Length) | ConvertFrom-Json
        if (
            [string]$event.schema -ne "mineru-phase-trace.v2" -or
            [string]$event.profile_sha256 -ne $ExpectedProfileSha256
        ) {
            throw "phase-trace event identity drifted"
        }
    }
    $traceText = ($traceLines -join "`n") + "`n"
    $machineGuid = (Get-ItemProperty -LiteralPath "HKLM:\SOFTWARE\Microsoft\Cryptography" -Name MachineGuid).MachineGuid
    $traceResult = [ordered]@{
        schema = "mineru-phase-trace-capture.v1"
        collected_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        collector_path = $PSCommandPath
        collector_sha256 = "sha256:$((Get-FileHash -Algorithm SHA256 -LiteralPath $PSCommandPath).Hash.ToLowerInvariant())"
        windows_node_identity_sha256 = Get-Sha256Text -Value ([string]$machineGuid).Trim().ToLowerInvariant()
        since_utc = $since.ToUniversalTime().ToString("o")
        until_utc = $until.ToUniversalTime().ToString("o")
        capacity_mode = $ExpectedCapacityMode
        active_profile_sha256 = $ExpectedProfileSha256
        container = [ordered]@{
            name = ([string]$traceContainer.Name).TrimStart("/")
            id = [string]$traceContainer.Id
            image = [string]$traceContainer.Config.Image
            image_id = [string]$traceContainer.Image
            started_at_utc = [string]$traceContainer.State.StartedAt
            restart_count = [int]$traceContainer.RestartCount
            oom_killed = [bool]$traceContainer.State.OOMKilled
            running = [bool]$traceContainer.State.Running
            status = [string]$traceContainer.State.Status
            health = [string]$traceContainer.State.Health.Status
        }
        line_count = $traceLines.Count
        trace_bytes = $traceByteCount
        trace_lines_sha256 = Get-Sha256Text -Value $traceText
        lines = $traceLines
    }
    $traceResult | ConvertTo-Json -Depth 20 -Compress
    exit 0
}

if (-not (Test-Path -LiteralPath $ComposePath -PathType Leaf)) { throw "compose file is missing" }
$resolvedConfig = & $DockerCommand compose --project-name mineru-tailnet --file $ComposePath config --format json
if ($LASTEXITCODE -ne 0) { throw "cannot resolve MinerU compose configuration" }
$configObject = $resolvedConfig | ConvertFrom-Json
$inspect = & $DockerCommand inspect mineru-api mineru-api-proxy mineru-openai-server | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $inspect.Count -ne 3) { throw "cannot inspect all MinerU containers" }
$api = $inspect | Where-Object { $_.Name -eq "/mineru-api" }
$proxy = $inspect | Where-Object { $_.Name -eq "/mineru-api-proxy" }
$vllm = $inspect | Where-Object { $_.Name -eq "/mineru-openai-server" }
if ($null -eq $api -or $null -eq $proxy -or $null -eq $vllm) {
    throw "MinerU container identities drifted"
}
$apiImageInspect = & $DockerCommand image inspect ([string]$api.Config.Image) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or @($apiImageInspect).Count -ne 1) {
    throw "cannot inspect the pinned MinerU API compatibility image"
}
$baseImageInspect = & $DockerCommand image inspect ([string]$vllm.Config.Image) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or @($baseImageInspect).Count -ne 1) {
    throw "cannot inspect the pinned MinerU base image"
}
$apiImageEnvironment = @($apiImageInspect[0].Config.Env)
$baseImageEnvironment = @($baseImageInspect[0].Config.Env)

$apiAllowedEnvironment = @(
    "MINERU_API_TASK_CLEANUP_INTERVAL_SECONDS", "MINERU_API_DISABLE_ACCESS_LOG",
    "MINERU_API_ENABLE_FASTAPI_DOCS", "MINERU_API_MAX_CONCURRENT_REQUESTS",
    "MINERU_API_OUTPUT_ROOT", "MINERU_API_TASK_RETENTION_SECONDS",
    "MINERU_CAPACITY_CATALOG_PATH", "MINERU_CAPACITY_CATALOG_SHA256",
    "MINERU_CAPACITY_MODE", "MINERU_CAPACITY_PROFILE_JSON",
    "MINERU_CAPACITY_RUNTIME_COMPATIBILITY_SHA256", "MINERU_MALLOC_TRIM",
    "MINERU_MODEL_SOURCE", "MINERU_PHASE_TRACE", "MINERU_PROCESSING_WINDOW_SIZE"
)
$vllmAllowedEnvironment = @("MINERU_MODEL_SOURCE")
$apiEnvironment = Select-ExactEnvironment -ActualValues @($api.Config.Env) `
    -ImageValues $apiImageEnvironment `
    -ResolvedValues $configObject.services."mineru-api".environment -AllowedNames $apiAllowedEnvironment
$vllmEnvironment = Select-ExactEnvironment -ActualValues @($vllm.Config.Env) `
    -ImageValues $baseImageEnvironment `
    -ResolvedValues $configObject.services."mineru-openai-server".environment -AllowedNames $vllmAllowedEnvironment
$emptyEnvironment = [pscustomobject]@{}
$proxyEnvironment = Select-ExactEnvironment -ActualValues @($proxy.Config.Env) `
    -ImageValues $baseImageEnvironment -ResolvedValues $emptyEnvironment -AllowedNames @()

$compatProbeCode = @'
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path

from mineru.utils.model_utils import (
    capacity_runtime_status,
    is_heap_trim_enabled,
    is_phase_trace_enabled,
)

paths = (
    "mineru/backend/vlm/vlm_analyze.py",
    "mineru/backend/hybrid/hybrid_analyze.py",
    "mineru/utils/model_utils.py",
)
root = Path("/usr/local/lib/python3.12/dist-packages")
marker = json.loads(
    Path("/opt/agent-invest/mineru-capacity-v1/compatibility.json")
    .read_text(encoding="utf-8")
)
print(json.dumps({
    "marker": marker,
    "actual_source_sha256": {
        path: "sha256:" + hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in paths
    },
    "heap_trim_enabled": is_heap_trim_enabled(),
    "capacity_runtime": capacity_runtime_status(
        int(os.environ["MINERU_PROCESSING_WINDOW_SIZE"])
    ),
    "phase_trace_enabled": is_phase_trace_enabled(),
    "mineru_version": importlib.metadata.version("mineru"),
}, sort_keys=True, separators=(",", ":")))
'@
$compatProbeOutput = @(
    $compatProbeCode | & $DockerCommand exec -i mineru-api /usr/bin/python3.12 -I -
)
if ($LASTEXITCODE -ne 0 -or $compatProbeOutput.Count -ne 1) {
    throw "cannot measure the live MinerU heap-return compatibility layer"
}
$compatProbe = ([string]$compatProbeOutput[0]) | ConvertFrom-Json
if ([string]$compatProbe.mineru_version -ne "3.4.4") {
    throw "live MinerU API version drifted"
}
$compatLabelNames = @(
    "io.agent-invest.mineru.base-image-digest",
    "io.agent-invest.mineru.capacity-policy",
    "io.agent-invest.mineru.compatibility-policy",
    "io.agent-invest.mineru.compatibility-patcher-sha256",
    "io.agent-invest.mineru.compatibility-dockerfile-sha256"
)
$compatLabels = [ordered]@{}
foreach ($name in $compatLabelNames) {
    $value = $apiImageInspect[0].Config.Labels.$name
    if ([string]::IsNullOrWhiteSpace([string]$value)) {
        throw "MinerU API compatibility image label is missing: $name"
    }
    $compatLabels[$name] = [string]$value
}

$health = Invoke-RestMethod -Uri "http://127.0.0.1:30003/health" -TimeoutSec 15
$models = Invoke-RestMethod -Uri "http://127.0.0.1:30001/v1/models" -TimeoutSec 30
if ($models.data.Count -ne 1) { throw "vLLM model list is not singular" }
$modelId = [string]$models.data[0].id
$modelPattern = '^/root/\.cache/huggingface/hub/models--opendatalab--MinerU2\.5-Pro-2605-1\.2B/snapshots/([a-f0-9]{40})$'
if ($modelId -notmatch $modelPattern) { throw "served model identity is not the approved repository snapshot" }
$revision = $Matches[1]
if ($revision -ne "bff20d4ae2bf202df9f45284b4d43681555a97ed") {
    throw "served model revision drifted"
}

$vllmVersionOutput = @(
    & $DockerCommand exec mineru-openai-server /usr/bin/python3.12 -I -c `
        "import importlib.metadata; print(importlib.metadata.version('vllm'))"
)
if ($LASTEXITCODE -ne 0 -or $vllmVersionOutput.Count -ne 1) {
    throw "cannot measure the live vLLM package version"
}
$vllmVersion = ([string]$vllmVersionOutput[0]).Trim()
if ($vllmVersion -ne "0.21.0") { throw "live vLLM package version drifted" }

$egressOutput = @(
    & $DockerCommand exec mineru-api /usr/bin/python3.12 -I -c `
        "import socket,sys;`ntry:`n socket.create_connection(('1.1.1.1',443),2); print('MINERU_EGRESS_OPEN'); sys.exit(0)`nexcept (TimeoutError,ConnectionRefusedError,OSError) as exc:`n print('MINERU_EGRESS_BLOCKED:'+type(exc).__name__); sys.exit(42)" 2>$null
)
$externalTcpEgressBlocked = (
    $LASTEXITCODE -eq 42 -and $egressOutput.Count -eq 1 -and
    ([string]$egressOutput[0]).Trim() -match '^MINERU_EGRESS_BLOCKED:(TimeoutError|ConnectionRefusedError|OSError)$'
)
if (-not $externalTcpEgressBlocked) {
    throw "MinerU API external egress probe did not return the exact blocked marker"
}

$machineGuid = (Get-ItemProperty -LiteralPath "HKLM:\SOFTWARE\Microsoft\Cryptography" -Name MachineGuid).MachineGuid
$nodeIdentity = Get-Sha256Text -Value ([string]$machineGuid).Trim().ToLowerInvariant()
$outputFiles = @()
if (Test-Path -LiteralPath $OutputRoot -PathType Container) {
    $outputFiles = @(Get-ChildItem -LiteralPath $OutputRoot -Recurse -File -Force)
}
$apiMounts = @($api.Mounts | Select-Object Type,Source,Destination,RW,Propagation)
$proxyMounts = @($proxy.Mounts | Where-Object { $_.Type -ne "tmpfs" } | Select-Object Type,Source,Destination,RW,Propagation)
$vllmMounts = @($vllm.Mounts | Select-Object Type,Source,Destination,RW,Propagation)
$apiNetworks = @($api.NetworkSettings.Networks.PSObject.Properties.Name | Sort-Object)
$proxyNetworks = @($proxy.NetworkSettings.Networks.PSObject.Properties.Name | Sort-Object)
$vllmNetworks = @($vllm.NetworkSettings.Networks.PSObject.Properties.Name | Sort-Object)
$apiPort = @(
    $api.NetworkSettings.Ports."8000/tcp" |
        Where-Object { $null -ne $_ }
)
$proxyPort = @($proxy.NetworkSettings.Ports."8000/tcp")
$vllmPort = @($vllm.NetworkSettings.Ports."30000/tcp")
$networkInspect = & $DockerCommand network inspect mineru-tailnet_inference mineru-tailnet_runtime | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or @($networkInspect).Count -ne 2) {
    throw "cannot inspect the live MinerU Docker networks"
}
$inferenceNetwork = @($networkInspect | Where-Object { $_.Name -eq "mineru-tailnet_inference" })
$runtimeNetwork = @($networkInspect | Where-Object { $_.Name -eq "mineru-tailnet_runtime" })
if ($inferenceNetwork.Count -ne 1 -or $runtimeNetwork.Count -ne 1) {
    throw "live MinerU Docker network identities drifted"
}

$result = [ordered]@{
    schema = "mineru-windows-runtime-observation.v3"
    observed_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    collector_path = $PSCommandPath
    collector_sha256 = "sha256:$((Get-FileHash -Algorithm SHA256 -LiteralPath $PSCommandPath).Hash.ToLowerInvariant())"
    windows_node_identity_rule = "sha256(utf8(lower(trim(HKLM MachineGuid))))"
    windows_node_identity_sha256 = $nodeIdentity
    compose_path = $ComposePath
    compose_sha256 = "sha256:$((Get-FileHash -Algorithm SHA256 -LiteralPath $ComposePath).Hash.ToLowerInvariant())"
    compose_config_sha256 = Get-Sha256Text -Value (($configObject | ConvertTo-Json -Depth 100 -Compress))
    networks = [ordered]@{
        inference = [ordered]@{ name = [string]$inferenceNetwork[0].Name; driver = [string]$inferenceNetwork[0].Driver; internal = [bool]$inferenceNetwork[0].Internal }
        runtime = [ordered]@{ name = [string]$runtimeNetwork[0].Name; driver = [string]$runtimeNetwork[0].Driver; internal = [bool]$runtimeNetwork[0].Internal }
    }
    api = [ordered]@{
        image = [string]$api.Config.Image; image_id = [string]$api.Image
        entrypoint = @($api.Config.Entrypoint); command = @($api.Config.Cmd)
        environment = $apiEnvironment; mounts = $apiMounts; networks = $apiNetworks
        port = if ($apiPort.Count -eq 1) { $apiPort[0] } else { $null }
        restart_policy = $api.HostConfig.RestartPolicy; health_state = [string]$api.State.Health.Status
        external_tcp_egress_blocked = $externalTcpEgressBlocked
    }
    api_compatibility = [ordered]@{
        marker = $compatProbe.marker
        actual_source_sha256 = $compatProbe.actual_source_sha256
        capacity_runtime = $compatProbe.capacity_runtime
        heap_trim_enabled = [bool]$compatProbe.heap_trim_enabled
        phase_trace_enabled = [bool]$compatProbe.phase_trace_enabled
        image_labels = $compatLabels
    }
    proxy = [ordered]@{
        image = [string]$proxy.Config.Image; image_id = [string]$proxy.Image
        entrypoint = @($proxy.Config.Entrypoint); command = @($proxy.Config.Cmd)
        environment = $proxyEnvironment; mounts = $proxyMounts; networks = $proxyNetworks
        port = if ($proxyPort.Count -eq 1) { $proxyPort[0] } else { $null }
        restart_policy = $proxy.HostConfig.RestartPolicy; health_state = [string]$proxy.State.Health.Status
        read_only_rootfs = [bool]$proxy.HostConfig.ReadonlyRootfs
        cap_drop = @($proxy.HostConfig.CapDrop); security_opt = @($proxy.HostConfig.SecurityOpt)
    }
    inference = [ordered]@{
        image = [string]$vllm.Config.Image; image_id = [string]$vllm.Image
        entrypoint = @($vllm.Config.Entrypoint); command = @($vllm.Config.Cmd)
        environment = $vllmEnvironment; mounts = $vllmMounts; networks = $vllmNetworks
        port = if ($vllmPort.Count -eq 1) { $vllmPort[0] } else { $null }
        restart_policy = $vllm.HostConfig.RestartPolicy; health_state = [string]$vllm.State.Health.Status
    }
    api_health = $health
    served_model = [ordered]@{
        id = $modelId; repository = "opendatalab/MinerU2.5-Pro-2605-1.2B"
        revision = $revision; max_model_len = [int]$models.data[0].max_model_len
        vllm_version = $vllmVersion
    }
    output_root = [ordered]@{
        path = $OutputRoot; file_count = $outputFiles.Count
        total_bytes = [long](($outputFiles | Measure-Object -Property Length -Sum).Sum)
    }
}

$result | ConvertTo-Json -Depth 100 -Compress
