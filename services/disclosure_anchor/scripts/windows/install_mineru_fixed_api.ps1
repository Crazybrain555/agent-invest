[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ComposeSource,
    [Parameter(Mandatory = $true)][string]$CollectorSource,
    [Parameter(Mandatory = $true)][string]$CompatDockerfileSource,
    [Parameter(Mandatory = $true)][string]$CompatPatcherSource,
    [string]$ComposeTarget = "C:\ProgramData\compose.tailnet.yaml",
    [string]$CollectorTarget = "C:\ProgramData\agent-invest\mineru-runtime-v6\collect_mineru_runtime.ps1",
    [string]$ReceiptTarget = "C:\ProgramData\agent-invest\mineru-runtime-v6\install-receipt.json",
    [string]$OutputRoot = "C:\ProgramData\agent-invest\mineru-api-output",
    [string]$ExpectedRepoDigest = "mineru@sha256:109016f8f7666c3a86b0a6585f5b7003d1dd63c2d318f6ecd7ab1db5aa582458",
    [string]$ExpectedImageId = "sha256:109016f8f7666c3a86b0a6585f5b7003d1dd63c2d318f6ecd7ab1db5aa582458",
    [switch]$ReuseCurrentPublishedImage,
    [string]$CampaignApiCompatImageId = "",
    [ValidateSet(1)][int]$ExpectedApiTaskSlots = 1,
    [ValidateSet(1)][int]$ExpectedApiMaxPendingTasks = 1
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
if ($ExpectedApiTaskSlots -ne 1 -or $ExpectedApiMaxPendingTasks -ne 1) {
    throw "serial MinerU requires task slots and pending depth to both equal 1"
}
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

# BEGIN MINERU NATIVE PROCESS V1
function ConvertTo-WindowsCommandLineArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') {
        return $Value
    }
    $builder = New-Object Text.StringBuilder
    [void]$builder.Append('"')
    [int]$backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes += 1
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * (($backslashes * 2) + 1)))
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append(('\' * ($backslashes * 2)))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function ConvertFrom-NativeProcessText {
    param([AllowEmptyString()][string]$Value)
    if ([string]::IsNullOrEmpty($Value)) { return @() }
    $trimmed = $Value.TrimEnd([char[]]@("`r", "`n"))
    if ([string]::IsNullOrEmpty($trimmed)) { return @() }
    return @($trimmed -split "`r?`n")
}

function Assert-NativeProcessArguments {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [AllowEmptyString()]
        [object[]]$Arguments
    )
    for ($index = 0; $index -lt $Arguments.Count; $index++) {
        $argument = $Arguments[$index]
        if ($null -eq $argument) {
            throw "native process argument $index must not be null"
        }
        if ($argument.GetType() -ne [string]) {
            throw (
                "native process argument $index must be a string; actual type: " +
                $argument.GetType().FullName
            )
        }
    }
}

function Invoke-NativeProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][AllowEmptyString()][object[]]$Arguments,
        [AllowNull()][string]$StandardInput = $null
    )
    Assert-NativeProcessArguments -Arguments $Arguments
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = (@(
        $Arguments | ForEach-Object { ConvertTo-WindowsCommandLineArgument -Value $_ }
    ) -join " ")
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.RedirectStandardInput = $null -ne $StandardInput
    $utf8 = New-Object Text.UTF8Encoding($false)
    $startInfo.StandardOutputEncoding = $utf8
    $startInfo.StandardErrorEncoding = $utf8

    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    $started = $false
    try {
        $started = $process.Start()
        if (-not $started) { throw "native process did not start: $FilePath" }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if ($null -ne $StandardInput) {
            $inputBytes = [Text.Encoding]::UTF8.GetBytes($StandardInput)
            $process.StandardInput.BaseStream.Write($inputBytes, 0, $inputBytes.Length)
            $process.StandardInput.BaseStream.Flush()
            $process.StandardInput.Close()
        }
        $process.WaitForExit()
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        return [pscustomobject]@{
            ExitCode = [int]$process.ExitCode
            StandardOutput = [string]$stdout
            StandardError = [string]$stderr
        }
    }
    catch {
        $originalError = $_
        if ($started) {
            try {
                if (-not $process.HasExited) { $process.Kill() }
                $process.WaitForExit()
            }
            catch {
                # Cleanup is best-effort; preserve the original process error.
            }
        }
        throw $originalError
    }
    finally {
        $process.Dispose()
    }
}

function Invoke-DockerProcess {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][object[]]$Arguments,
        [AllowNull()][string]$StandardInput = $null,
        [int[]]$AllowedExitCodes = @(0)
    )
    Assert-NativeProcessArguments -Arguments $Arguments
    $result = Invoke-NativeProcess -FilePath $DockerCommand -Arguments $Arguments `
        -StandardInput $StandardInput
    if ($AllowedExitCodes -notcontains $result.ExitCode) {
        $detail = ([string]$result.StandardError).Trim()
        if ([string]::IsNullOrWhiteSpace($detail)) {
            $detail = ([string]$result.StandardOutput).Trim()
        }
        if ($detail.Length -gt 4096) { $detail = $detail.Substring(0, 4096) }
        throw (
            "docker command failed with exit code $($result.ExitCode): docker " +
            "$($Arguments -join ' ')`n$detail"
        ).TrimEnd()
    }
    return $result
}

function Invoke-Docker {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][object[]]$Arguments
    )
    Assert-NativeProcessArguments -Arguments $Arguments
    $result = Invoke-DockerProcess -Arguments $Arguments
    return @(ConvertFrom-NativeProcessText -Value $result.StandardOutput)
}
# END MINERU NATIVE PROCESS V1

$ProjectName = "mineru-tailnet"
$ApiCompatImage = "agent-invest/mineru-api:3.4.4-serial-v1"
$ApiCompatBuildTag = "agent-invest/mineru-api:build-$([Guid]::NewGuid().ToString('N'))"
$HeapReturnPolicy = "glibc-malloc-trim-per-window.v1"
$CapacityPolicy = "single-owner-serial-mineru.v1"
$RequiredComposeTarget = "C:\ProgramData\compose.tailnet.yaml"
$RequiredCollectorTarget = "C:\ProgramData\agent-invest\mineru-runtime-v6\collect_mineru_runtime.ps1"
$RequiredReceiptTarget = "C:\ProgramData\agent-invest\mineru-runtime-v6\install-receipt.json"
$ExpectedApiCompatImageId = $null
$OldApiCompatImageId = $null
$CompatBuildTagCreated = $false
$CompatTagSwitched = $false
if (
    -not [string]::Equals(
        [IO.Path]::GetFullPath($ComposeTarget),
        $RequiredComposeTarget,
        [StringComparison]::OrdinalIgnoreCase
    ) -or
    -not [string]::Equals(
        [IO.Path]::GetFullPath($CollectorTarget),
        $RequiredCollectorTarget,
        [StringComparison]::OrdinalIgnoreCase
    ) -or
    -not [string]::Equals(
        [IO.Path]::GetFullPath($ReceiptTarget),
        $RequiredReceiptTarget,
        [StringComparison]::OrdinalIgnoreCase
    )
) {
    throw "MinerU deployment targets must use the exact active compose and versioned v6 evidence paths"
}
$MutationStarted = $false
$DeploymentAttempted = $false
$ComposeBackupCreated = $false
$CollectorBackupCreated = $false
$ReceiptBackupCreated = $false
$OldProjectContainers = @()
$OldRunningContainers = @()
$StableServiceEpochs = $null
$ComposeExisted = Test-Path -LiteralPath $ComposeTarget -PathType Leaf
$CollectorExisted = Test-Path -LiteralPath $CollectorTarget -PathType Leaf
$ReceiptExisted = Test-Path -LiteralPath $ReceiptTarget -PathType Leaf
$Timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$ComposeBackup = "$ComposeTarget.pre-fixed-api-$Timestamp.bak"
$CollectorBackup = "$CollectorTarget.pre-fixed-api-$Timestamp.bak"
$ReceiptBackup = "$ReceiptTarget.pre-fixed-api-$Timestamp.bak"
if ($ReuseCurrentPublishedImage) {
    if ($CampaignApiCompatImageId -notmatch '^sha256:[a-f0-9]{64}$') {
        throw "reuse mode requires one canonical campaign API compatibility image ID"
    }
    if (-not $ComposeExisted -or -not $CollectorExisted -or -not $ReceiptExisted) {
        throw "reuse mode requires one complete existing deployment"
    }
    $ExpectedApiCompatImageId = $CampaignApiCompatImageId
}
elseif (-not [string]::IsNullOrEmpty($CampaignApiCompatImageId)) {
    throw "campaign API compatibility image ID is valid only in reuse mode"
}

function Get-OptionalImageId {
    param([Parameter(Mandatory = $true)][string]$Reference)
    $result = Invoke-DockerProcess -Arguments @(
        "image", "inspect", "--format", "{{.Id}}", $Reference
    ) -AllowedExitCodes @(0, 1)
    if ($result.ExitCode -eq 1) {
        $missingDetail = ([string]$result.StandardError).Trim()
        if (
            [string]::IsNullOrWhiteSpace([string]$result.StandardOutput) -and
            $missingDetail -match '^(Error response from daemon: )?No such image: .+$'
        ) {
            return $null
        }
        throw "cannot inspect optional Docker image reference $($Reference): $missingDetail"
    }
    $output = @(ConvertFrom-NativeProcessText -Value $result.StandardOutput)
    if ($output.Count -ne 1) {
        throw "cannot inspect optional Docker image reference $Reference"
    }
    $imageId = ([string]$output[0]).Trim()
    if ($imageId -notmatch '^sha256:[a-f0-9]{64}$') {
        throw "Docker image reference $Reference returned an invalid image ID"
    }
    return $imageId
}

function Get-StableServiceEpochs {
    $inspect = (Invoke-Docker -Arguments @(
        "inspect", "mineru-api-proxy", "mineru-openai-server"
    )) | ConvertFrom-Json
    if (@($inspect).Count -ne 2) {
        throw "stable MinerU services were not uniquely inspectable"
    }
    $result = [ordered]@{}
    foreach ($name in @("mineru-api-proxy", "mineru-openai-server")) {
        $container = @($inspect | Where-Object { $_.Name -eq "/$name" })
        if ($container.Count -ne 1) {
            throw "stable MinerU service $name was not unique"
        }
        $container = $container[0]
        if (
            -not [bool]$container.State.Running -or
            [string]$container.State.Health.Status -ne "healthy" -or
            [int]$container.RestartCount -ne 0 -or
            [bool]$container.State.OOMKilled -or
            [string]$container.Id -notmatch '^[a-f0-9]{64}$' -or
            [string]$container.Image -notmatch '^sha256:[a-f0-9]{64}$' -or
            [string]::IsNullOrWhiteSpace([string]$container.State.StartedAt)
        ) {
            throw "stable MinerU service $name has an invalid epoch"
        }
        $result[$name] = [ordered]@{
            container_id = [string]$container.Id
            started_at = [string]$container.State.StartedAt
            image_id = [string]$container.Image
        }
    }
    return $result
}

function Assert-StableServiceEpochs {
    param([Parameter(Mandatory = $true)][object]$Expected)
    $actual = Get-StableServiceEpochs
    foreach ($name in @("mineru-api-proxy", "mineru-openai-server")) {
        foreach ($field in @("container_id", "started_at", "image_id")) {
            if ([string]$actual[$name][$field] -ne [string]$Expected[$name][$field]) {
                throw "stable MinerU service $name $field changed during API-only deployment"
            }
        }
    }
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $Value, $encoding)
}

function Assert-TargetWritable {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $stream = [IO.File]::Open(
            $Path,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Write,
            [IO.FileShare]::Read
        )
        $stream.Dispose()
        return
    }
    $parent = Split-Path -Parent $Path
    $probe = Join-Path $parent (".mineru-write-probe-" + [Guid]::NewGuid().ToString("N"))
    $stream = $null
    try {
        $stream = [IO.File]::Open(
            $probe,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
        if (Test-Path -LiteralPath $probe) { Remove-Item -LiteralPath $probe -Force }
    }
}

function Get-Sha256Text {
    param([Parameter(Mandatory = $true)][string]$Value)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { $hash = $algorithm.ComputeHash($bytes) }
    finally { $algorithm.Dispose() }
    return "sha256:" + (-join ($hash | ForEach-Object { $_.ToString("x2") }))
}

function Assert-RequiredProperties {
    param(
        [Parameter(Mandatory = $true)][object]$Value,
        [Parameter(Mandatory = $true)][string[]]$Names,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $actual = @($Value.PSObject.Properties.Name)
    foreach ($name in $Names) {
        if ($actual -notcontains $name -or $null -eq $Value.$name) {
            throw "$Label is missing required field $name"
        }
    }
}

function Assert-IdleHealth {
    param(
        [Parameter(Mandatory = $true)][object]$Health,
        [Parameter(Mandatory = $true)][string]$Label
    )
    Assert-RequiredProperties -Value $Health -Names @(
        "status", "queued_tasks", "processing_tasks"
    ) -Label $Label
    if (
        [string]$Health.status -ne "healthy" -or
        [int]$Health.queued_tasks -ne 0 -or
        [int]$Health.processing_tasks -ne 0
    ) {
        throw "$Label is not healthy and idle"
    }
}

function Assert-ExternalEgressBlocked {
    $egressResult = Invoke-DockerProcess -Arguments @(
        "exec", "mineru-api", "/usr/bin/python3.12", "-I", "-c",
        "import socket,sys;`ntry:`n socket.create_connection(('1.1.1.1',443),2); print('MINERU_EGRESS_OPEN'); sys.exit(0)`nexcept (TimeoutError,ConnectionRefusedError,OSError) as exc:`n print('MINERU_EGRESS_BLOCKED:'+type(exc).__name__); sys.exit(42)"
    ) -AllowedExitCodes @(42)
    $egressOutput = @(
        ConvertFrom-NativeProcessText -Value $egressResult.StandardOutput
    )
    if (
        $egressOutput.Count -ne 1 -or
        ([string]$egressOutput[0]).Trim() -notmatch
            '^MINERU_EGRESS_BLOCKED:(TimeoutError|ConnectionRefusedError|OSError)$'
    ) {
        throw "MinerU API egress probe did not return the exact blocked marker"
    }
}

function Assert-SinglePort {
    param(
        [Parameter(Mandatory = $true)][object]$Container,
        [Parameter(Mandatory = $true)][string]$ContainerPort,
        [Parameter(Mandatory = $true)][string]$HostPort
    )
    $bindings = @($Container.NetworkSettings.Ports.$ContainerPort)
    if (
        $bindings.Count -ne 1 -or
        [string]$bindings[0].HostIp -ne "127.0.0.1" -or
        [string]$bindings[0].HostPort -ne $HostPort
    ) {
        throw "$($Container.Name) port binding drifted"
    }
}

function Assert-NoPublishedPort {
    param(
        [Parameter(Mandatory = $true)][object]$Container,
        [Parameter(Mandatory = $true)][string]$ContainerPort
    )
    $bindings = @(
        $Container.NetworkSettings.Ports.$ContainerPort |
            Where-Object { $null -ne $_ }
    )
    if ($bindings.Count -ne 0) {
        throw "$($Container.Name) must not publish $ContainerPort"
    }
}

function Assert-ExactCommand {
    param(
        [Parameter(Mandatory = $true)][object]$Container,
        [Parameter(Mandatory = $true)][string[]]$Expected
    )
    $actual = @($Container.Config.Entrypoint) + @($Container.Config.Cmd)
    if (($actual -join "`n") -ne ($Expected -join "`n")) {
        throw "$($Container.Name) command drifted"
    }
}

function Assert-VllmIdle {
    $metrics = (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:30001/metrics" -TimeoutSec 20).Content
    $values = @{ running = @(); waiting = @() }
    foreach ($rawLine in ([string]$metrics -split "`n")) {
        $line = $rawLine.Trim()
        if ($line -match '^(vllm:num_requests_running|vllm_num_requests_running)(?:\{[^}]*\})?\s+([^\s]+)') {
            $values.running += [double]$Matches[2]
        }
        elseif ($line -match '^(vllm:num_requests_waiting|vllm_num_requests_waiting)(?:\{[^}]*\})?\s+([^\s]+)') {
            $values.waiting += [double]$Matches[2]
        }
    }
    if ($values.running.Count -eq 0 -or $values.waiting.Count -eq 0) {
        throw "old vLLM metrics do not expose running and waiting request gauges"
    }
    $running = [double](($values.running | Measure-Object -Sum).Sum)
    $waiting = [double](($values.waiting | Measure-Object -Sum).Sum)
    if (
        [double]::IsNaN($running) -or [double]::IsInfinity($running) -or
        [double]::IsNaN($waiting) -or [double]::IsInfinity($waiting) -or
        $running -ne 0 -or $waiting -ne 0
    ) {
        throw "old vLLM still has running or waiting requests"
    }
}

function Capture-OldRuntimeState {
    $allNames = @(Invoke-Docker -Arguments @("ps", "--all", "--format", "{{.Names}}"))
    $script:OldProjectContainers = @(
        $allNames | Where-Object {
            $_ -in @("mineru-api", "mineru-api-proxy", "mineru-openai-server")
        }
    )
    $script:OldRunningContainers = @()
    foreach ($name in $script:OldProjectContainers) {
        $running = @(Invoke-Docker -Arguments @(
            "inspect", "--format", "{{.State.Running}}", $name
        ))
        if ($running.Count -ne 1) { throw "cannot measure old container state for $name" }
        if (([string]$running[0]).Trim().ToLowerInvariant() -eq "true") {
            $script:OldRunningContainers += $name
        }
    }
    if ($script:OldProjectContainers -contains "mineru-api") {
        if ($script:OldRunningContainers -notcontains "mineru-api") {
            throw "old MinerU API exists but is not running; drain state is unknowable"
        }
        $oldHealth = Invoke-RestMethod -Uri "http://127.0.0.1:30003/health" -TimeoutSec 15
        Assert-IdleHealth -Health $oldHealth -Label "old MinerU API"
        $oldApi = (Invoke-Docker -Arguments @("inspect", "mineru-api")) | ConvertFrom-Json
        if ($script:OldProjectContainers -contains "mineru-api-proxy") {
            if ($script:OldRunningContainers -notcontains "mineru-api-proxy") {
                throw "old MinerU API proxy exists but is not running"
            }
            $oldProxy = (Invoke-Docker -Arguments @(
                "inspect", "mineru-api-proxy"
            )) | ConvertFrom-Json
            Assert-NoPublishedPort -Container $oldApi -ContainerPort "8000/tcp"
            Assert-SinglePort -Container $oldProxy -ContainerPort "8000/tcp" -HostPort "30003"
        }
        else {
            # One-time migration compatibility for the pre-proxy topology.
            Assert-SinglePort -Container $oldApi -ContainerPort "8000/tcp" -HostPort "30003"
        }
    }
    if ($script:OldProjectContainers -contains "mineru-openai-server") {
        if ($script:OldRunningContainers -contains "mineru-openai-server") {
            $oldInference = (Invoke-Docker -Arguments @("inspect", "mineru-openai-server")) |
                ConvertFrom-Json
            Assert-SinglePort -Container $oldInference -ContainerPort "30000/tcp" -HostPort "30001"
            Invoke-RestMethod -Uri "http://127.0.0.1:30001/health" -TimeoutSec 15 | Out-Null
            Assert-VllmIdle
        }
    }
    if ($ReuseCurrentPublishedImage) {
        $required = @("mineru-api", "mineru-api-proxy", "mineru-openai-server")
        if (
            (@($script:OldProjectContainers | Sort-Object) -join ",") -ne
                (@($required | Sort-Object) -join ",") -or
            (@($script:OldRunningContainers | Sort-Object) -join ",") -ne
                (@($required | Sort-Object) -join ",")
        ) {
            throw "reuse mode requires exactly three running MinerU containers"
        }
        $script:StableServiceEpochs = Get-StableServiceEpochs
    }
}

function Wait-Healthy {
    $deadline = (Get-Date).AddMinutes(8)
    do {
        try {
            $api = Invoke-RestMethod -Uri "http://127.0.0.1:30003/health" -TimeoutSec 10
            $models = Invoke-RestMethod -Uri "http://127.0.0.1:30001/v1/models" -TimeoutSec 20
            $containerHealth = @(
                Invoke-Docker -Arguments @(
                    "inspect", "--format", "{{.State.Health.Status}}",
                    "mineru-api", "mineru-api-proxy", "mineru-openai-server"
                )
            )
            if (
                $api.status -eq "healthy" -and @($models.data).Count -eq 1 -and
                $containerHealth.Count -eq 3 -and
                @($containerHealth | Where-Object { $_ -ne "healthy" }).Count -eq 0
            ) {
                return @($api, $models)
            }
        }
        catch {
            Start-Sleep -Seconds 5
            continue
        }
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)
    throw "MinerU fixed API did not become healthy before the deadline"
}

function Get-ValidatedRuntime {
    $healthAndModels = Wait-Healthy
    $health = $healthAndModels[0]
    $models = $healthAndModels[1]
    $inspect = (Invoke-Docker -Arguments @(
        "inspect", "mineru-api", "mineru-api-proxy", "mineru-openai-server"
    )) |
        ConvertFrom-Json
    if (@($inspect).Count -ne 3) { throw "all MinerU containers were not inspectable" }
    $api = @($inspect | Where-Object { $_.Name -eq "/mineru-api" })
    $proxy = @($inspect | Where-Object { $_.Name -eq "/mineru-api-proxy" })
    $inference = @($inspect | Where-Object { $_.Name -eq "/mineru-openai-server" })
    if ($api.Count -ne 1 -or $proxy.Count -ne 1 -or $inference.Count -ne 1) {
        throw "MinerU container names were not unique"
    }
    $api = $api[0]
    $proxy = $proxy[0]
    $inference = $inference[0]

    if (
        [string]$api.Config.Image -ne $ApiCompatImage -or
        [string]$api.Image -ne $ExpectedApiCompatImageId
    ) {
        throw "MinerU API compatibility image reference or ID drifted"
    }
    foreach ($container in @($proxy, $inference)) {
        if (
            [string]$container.Config.Image -ne $ExpectedRepoDigest -or
            [string]$container.Image -ne $ExpectedImageId
        ) {
            throw "$($container.Name) base image reference or image ID drifted"
        }
    }
    foreach ($container in @($api, $proxy, $inference)) {
        if (
            [string]$container.HostConfig.RestartPolicy.Name -ne "always" -or
            [int]$container.HostConfig.RestartPolicy.MaximumRetryCount -ne 0 -or
            [string]$container.State.Health.Status -ne "healthy"
        ) {
            throw "$($container.Name) restart or health policy drifted"
        }
    }
    if (@($api.Config.Env) -notcontains "MINERU_MALLOC_TRIM=1") {
        throw "MinerU API heap-return compatibility switch is not enabled"
    }
    $phaseTraceEnvironment = @(
        $api.Config.Env | Where-Object { $_ -like "MINERU_PHASE_TRACE=*" }
    )
    if (
        $phaseTraceEnvironment.Count -ne 1 -or
        $phaseTraceEnvironment[0] -notin @("MINERU_PHASE_TRACE=0", "MINERU_PHASE_TRACE=1")
    ) {
        throw "MinerU API phase-trace switch is not closed"
    }

    $apiPortBindings = @(
        $api.NetworkSettings.Ports."8000/tcp" |
            Where-Object { $null -ne $_ }
    )
    if ($apiPortBindings.Count -ne 0) {
        throw "MinerU API must not publish a host port directly"
    }
    Assert-SinglePort -Container $proxy -ContainerPort "8000/tcp" -HostPort "30003"
    Assert-SinglePort -Container $inference -ContainerPort "30000/tcp" -HostPort "30001"
    Assert-ExactCommand -Container $api -Expected @(
        "mineru-api", "--host", "0.0.0.0", "--port", "8000",
        "--allow-public-http-client", "--max-concurrency", "7"
    )
    Assert-ExactCommand -Container $inference -Expected @(
        "mineru-openai-server", "--host", "0.0.0.0", "--port", "30000",
        "--max-num-seqs", "128", "--mm-processor-cache-gb", "0"
    )
    $proxyCommand = @($proxy.Config.Entrypoint) + @($proxy.Config.Cmd)
    if (
        $proxyCommand.Count -ne 4 -or
        ($proxyCommand[0..2] -join ",") -ne "/usr/bin/python3.12,-I,-c" -or
        (Get-Sha256Text -Value ([string]$proxyCommand[3])) -ne
            "sha256:991ff233fb77188f402dba81a8ebb6519630122087a8b5744396e7ebd8c63922" -or
        [bool]$proxy.HostConfig.ReadonlyRootfs -ne $true -or
        @($proxy.HostConfig.CapDrop) -notcontains "ALL"
    ) {
        throw "MinerU API proxy command or confinement drifted"
    }

    $apiNetworks = @($api.NetworkSettings.Networks.PSObject.Properties.Name | Sort-Object)
    $proxyNetworks = @($proxy.NetworkSettings.Networks.PSObject.Properties.Name | Sort-Object)
    $inferenceNetworks = @($inference.NetworkSettings.Networks.PSObject.Properties.Name | Sort-Object)
    if (($apiNetworks -join ",") -ne "mineru-tailnet_inference") {
        throw "MinerU API is not isolated to the inference network"
    }
    if (($proxyNetworks -join ",") -ne "mineru-tailnet_inference,mineru-tailnet_runtime") {
        throw "MinerU API proxy network membership drifted"
    }
    if (($inferenceNetworks -join ",") -ne "mineru-tailnet_inference,mineru-tailnet_runtime") {
        throw "MinerU inference network membership drifted"
    }
    $networkInspect = (Invoke-Docker -Arguments @(
        "network", "inspect", "mineru-tailnet_inference", "mineru-tailnet_runtime"
    )) | ConvertFrom-Json
    $inferenceNetwork = @($networkInspect | Where-Object { $_.Name -eq "mineru-tailnet_inference" })
    $runtimeNetwork = @($networkInspect | Where-Object { $_.Name -eq "mineru-tailnet_runtime" })
    if (
        $inferenceNetwork.Count -ne 1 -or $runtimeNetwork.Count -ne 1 -or
        [bool]$inferenceNetwork[0].Internal -ne $true -or
        [bool]$runtimeNetwork[0].Internal -ne $false
    ) {
        throw "MinerU Docker network isolation drifted"
    }

    $apiMounts = @($api.Mounts)
    $expectedMountCount = 1
    $outputMount = @(
        $apiMounts | Where-Object {
            [string]$_.Destination -eq "/var/lib/mineru-api-output"
        }
    )
    if (
        $apiMounts.Count -ne $expectedMountCount -or
        $outputMount.Count -ne 1 -or
        [string]$outputMount[0].Type -ne "bind" -or
        [bool]$outputMount[0].RW -ne $true -or
        [IO.Path]::GetFullPath([string]$outputMount[0].Source) -ine
            [IO.Path]::GetFullPath($OutputRoot) -or
        @($proxy.Mounts | Where-Object { $_.Type -ne "tmpfs" }).Count -ne 0 -or
        @($inference.Mounts).Count -ne 0
    ) {
        throw "MinerU mount policy drifted"
    }

    Assert-RequiredProperties -Value $health -Names @(
        "status", "version", "protocol_version", "max_concurrent_requests",
        "max_pending_tasks_requested", "max_pending_tasks_effective",
        "processing_window_size", "task_retention_seconds",
        "task_cleanup_interval_seconds", "queued_tasks", "processing_tasks"
    ) -Label "new MinerU API health"
    Assert-IdleHealth -Health $health -Label "new MinerU API health"
    if (
        [string]$health.version -ne "3.4.4" -or
        [int]$health.protocol_version -ne 2 -or
        [int]$health.max_concurrent_requests -ne $ExpectedApiTaskSlots -or
        [int]$health.max_pending_tasks_requested -ne $ExpectedApiMaxPendingTasks -or
        [int]$health.max_pending_tasks_effective -ne $ExpectedApiMaxPendingTasks -or
        $ExpectedApiMaxPendingTasks -lt $ExpectedApiTaskSlots -or
        [int]$health.processing_window_size -ne 16 -or
        [int]$health.task_retention_seconds -ne 600 -or
        [int]$health.task_cleanup_interval_seconds -ne 30
    ) {
        throw "MinerU API health contract drifted or API is not idle"
    }
    if (@($models.data).Count -ne 1) { throw "served model is not singular" }
    $modelId = [string]$models.data[0].id
    $modelRevision = Split-Path -Leaf $modelId
    $expectedModelId = "/root/.cache/huggingface/hub/models--opendatalab--MinerU2.5-Pro-2605-1.2B/snapshots/bff20d4ae2bf202df9f45284b4d43681555a97ed"
    if (
        $modelId -ne $expectedModelId -or
        $modelRevision -ne "bff20d4ae2bf202df9f45284b4d43681555a97ed" -or
        [int]$models.data[0].max_model_len -ne 8192
    ) {
        throw "served model identity drifted"
    }

    $vllmVersion = @(Invoke-Docker -Arguments @(
        "exec", "mineru-openai-server", "/usr/bin/python3.12", "-I", "-c",
        "import importlib.metadata; print(importlib.metadata.version('vllm'))"
    ))
    if (
        $vllmVersion.Count -ne 1 -or
        ([string]$vllmVersion[0]).Trim() -ne "0.21.0"
    ) {
        throw "live vLLM package version drifted"
    }

    Assert-ExternalEgressBlocked

    $outputFiles = @(Get-ChildItem -LiteralPath $OutputRoot -Recurse -File -Force)
    if ($outputFiles.Count -ne 0) { throw "MinerU output root is not empty before commissioning" }

    return [ordered]@{
        api_health = $health
        model_id = $modelId
        model_revision = $modelRevision
        model_max_len = [int]$models.data[0].max_model_len
        vllm_version = ([string]$vllmVersion[0]).Trim()
        api_networks = $apiNetworks
        proxy_networks = $proxyNetworks
        inference_networks = $inferenceNetworks
        output_file_count = $outputFiles.Count
    }
}

function Get-ApiCompatBuildIdentity {
    $dockerfile = [IO.Path]::GetFullPath($CompatDockerfileSource)
    $patcher = [IO.Path]::GetFullPath($CompatPatcherSource)
    $taskProtocol = Join-Path (Split-Path -Parent $patcher) "agent_task_protocol_v2.py"
    $context = Split-Path -Parent $dockerfile
    if (
        -not [string]::Equals(
            $context,
            (Split-Path -Parent $patcher),
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        (Split-Path -Leaf $dockerfile) -ne "Dockerfile" -or
        (Split-Path -Leaf $patcher) -ne "patch_mineru_344.py"
    ) {
        throw "MinerU compatibility Dockerfile and patcher must share one exact build context"
    }
    $patcherSha256 = "sha256:$((Get-FileHash -Algorithm SHA256 -LiteralPath $patcher).Hash.ToLowerInvariant())"
    $dockerfileSha256 = "sha256:$((Get-FileHash -Algorithm SHA256 -LiteralPath $dockerfile).Hash.ToLowerInvariant())"
    $taskProtocolSha256 = "sha256:$((Get-FileHash -Algorithm SHA256 -LiteralPath $taskProtocol).Hash.ToLowerInvariant())"
    return [ordered]@{
        dockerfile = $dockerfile
        patcher = $patcher
        context = $context
        patcher_sha256 = $patcherSha256
        dockerfile_sha256 = $dockerfileSha256
        task_protocol_v2_sha256 = $taskProtocolSha256
    }
}

function Get-ValidatedApiCompatImage {
    param(
        [Parameter(Mandatory = $true)][string]$Reference,
        [Parameter(Mandatory = $true)][string]$RequiredImageId,
        [Parameter(Mandatory = $true)][object]$BuildIdentity
    )
    if ($RequiredImageId -notmatch '^sha256:[a-f0-9]{64}$') {
        throw "MinerU API compatibility image ID is invalid"
    }
    $inspect = (Invoke-Docker -Arguments @("image", "inspect", $Reference)) |
        ConvertFrom-Json
    if (
        @($inspect).Count -ne 1 -or
        [string]$inspect[0].Id -ne $RequiredImageId
    ) {
        throw "MinerU API compatibility image is not uniquely bound to the required ID"
    }
    $image = $inspect[0]
    if (
        [string]$image.Config.Labels."io.agent-invest.mineru.base-image-digest" -ne
            $ExpectedImageId -or
        [string]$image.Config.Labels."io.agent-invest.mineru.capacity-policy" -ne
            $CapacityPolicy -or
        [string]$image.Config.Labels."io.agent-invest.mineru.compatibility-policy" -ne
            $HeapReturnPolicy -or
        [string]$image.Config.Labels."io.agent-invest.mineru.compatibility-patcher-sha256" -ne
            [string]$BuildIdentity.patcher_sha256 -or
        [string]$image.Config.Labels."io.agent-invest.mineru.compatibility-dockerfile-sha256" -ne
            [string]$BuildIdentity.dockerfile_sha256 -or
        [string]$image.Config.Labels."io.agent-invest.mineru.task-protocol-v2-sha256" -ne
            [string]$BuildIdentity.task_protocol_v2_sha256 -or
        @($image.Config.Env) -notcontains "MINERU_MALLOC_TRIM=1" -or
        @($image.Config.Env) -notcontains "MINERU_PHASE_TRACE=0" -or
        @($image.Config.Env) -notcontains "MINERU_TASK_PROTOCOL_V2=0"
    ) {
        throw "MinerU API compatibility image labels or environment drifted"
    }
    return [ordered]@{
        image = $ApiCompatImage
        image_id = $RequiredImageId
        policy = $HeapReturnPolicy
        capacity_policy = $CapacityPolicy
        patcher_sha256 = [string]$BuildIdentity.patcher_sha256
        dockerfile_sha256 = [string]$BuildIdentity.dockerfile_sha256
        task_protocol_v2_sha256 = [string]$BuildIdentity.task_protocol_v2_sha256
    }
}

function Build-ValidatedApiCompatImage {
    $identity = Get-ApiCompatBuildIdentity
    Invoke-Docker -Arguments @(
        "build", "--pull=false", "--provenance=false", "--file",
        [string]$identity.dockerfile,
        "--tag", $ApiCompatBuildTag,
        "--build-arg", "COMPAT_PATCHER_SHA256=$($identity.patcher_sha256)",
        "--build-arg", "COMPAT_DOCKERFILE_SHA256=$($identity.dockerfile_sha256)",
        "--build-arg", "TASK_PROTOCOL_V2_SHA256=$($identity.task_protocol_v2_sha256)",
        [string]$identity.context
    ) | Out-Null
    $script:CompatBuildTagCreated = $true
    $script:ExpectedApiCompatImageId = Get-OptionalImageId -Reference $ApiCompatBuildTag
    if ($ExpectedApiCompatImageId -notmatch '^sha256:[a-f0-9]{64}$') {
        throw "MinerU API compatibility image ID is invalid"
    }
    return (Get-ValidatedApiCompatImage -Reference $ApiCompatBuildTag `
        -RequiredImageId $ExpectedApiCompatImageId -BuildIdentity $identity)
}

function Get-ValidatedPublishedApiCompatImage {
    $identity = Get-ApiCompatBuildIdentity
    $publishedImageId = Get-OptionalImageId -Reference $ApiCompatImage
    if ($publishedImageId -ne $CampaignApiCompatImageId) {
        throw "published MinerU API compatibility tag does not match campaign image ID"
    }
    return (Get-ValidatedApiCompatImage -Reference $ApiCompatImage `
        -RequiredImageId $CampaignApiCompatImageId -BuildIdentity $identity)
}

function Remove-CompatBuildTag {
    if ($CompatBuildTagCreated) {
        Invoke-Docker -Arguments @("image", "rm", $ApiCompatBuildTag) | Out-Null
        $script:CompatBuildTagCreated = $false
    }
}

function Restore-ApiCompatTag {
    if (-not $CompatTagSwitched) { return }
    if ($null -ne $OldApiCompatImageId) {
        Invoke-Docker -Arguments @("tag", $OldApiCompatImageId, $ApiCompatImage) |
            Out-Null
    }
    else {
        Invoke-Docker -Arguments @("image", "rm", $ApiCompatImage) | Out-Null
    }
    $script:CompatTagSwitched = $false
}

function Restore-PreviousDeployment {
    if ($ComposeExisted -and $ComposeBackupCreated) {
        Copy-Item -LiteralPath $ComposeBackup -Destination $ComposeTarget -Force
    }
    elseif (Test-Path -LiteralPath $ComposeTarget) {
        Remove-Item -LiteralPath $ComposeTarget -Force
    }
    if ($CollectorExisted -and $CollectorBackupCreated) {
        Copy-Item -LiteralPath $CollectorBackup -Destination $CollectorTarget -Force
    }
    elseif (Test-Path -LiteralPath $CollectorTarget) {
        Remove-Item -LiteralPath $CollectorTarget -Force
    }
    if ($ReceiptExisted -and $ReceiptBackupCreated) {
        Copy-Item -LiteralPath $ReceiptBackup -Destination $ReceiptTarget -Force
    }
    elseif (Test-Path -LiteralPath $ReceiptTarget) {
        Remove-Item -LiteralPath $ReceiptTarget -Force
    }

    if ($ReuseCurrentPublishedImage) {
        if ((Get-OptionalImageId -Reference $ApiCompatImage) -ne $CampaignApiCompatImageId) {
            throw "campaign image tag drifted before API-only rollback"
        }
        Invoke-Docker -Arguments @(
            "compose", "--project-name", $ProjectName, "--file", $ComposeTarget,
            "up", "--detach", "--no-build", "--no-deps", "--force-recreate",
            "mineru-api"
        ) | Out-Null
        Get-ValidatedRuntime | Out-Null
        Assert-StableServiceEpochs -Expected $StableServiceEpochs
        if ((Get-OptionalImageId -Reference $ApiCompatImage) -ne $CampaignApiCompatImageId) {
            throw "campaign image tag drifted during API-only rollback"
        }
        Remove-CompatBuildTag
        return
    }

    Restore-ApiCompatTag

    if ($ComposeExisted) {
        if ($OldProjectContainers.Count -eq 0) {
            Invoke-Docker -Arguments @(
                "compose", "--project-name", $ProjectName, "--file", $ComposeTarget,
                "down", "--remove-orphans"
            ) | Out-Null
        }
        else {
            Invoke-Docker -Arguments @(
                "compose", "--project-name", $ProjectName, "--file", $ComposeTarget,
                "up", "--detach", "--remove-orphans"
            ) | Out-Null
            foreach ($name in $OldProjectContainers) {
                if ($OldRunningContainers -notcontains $name) {
                    Invoke-Docker -Arguments @("stop", $name) | Out-Null
                }
            }
        }
    }
    elseif ($DeploymentAttempted) {
        Invoke-Docker -Arguments @(
            "compose", "--project-name", $ProjectName, "--file", $ComposeSource,
            "down", "--remove-orphans"
        ) | Out-Null
    }
    $networkNames = @(Invoke-Docker -Arguments @("network", "ls", "--format", "{{.Name}}"))
    foreach ($name in @("mineru-tailnet_inference", "mineru-tailnet_runtime")) {
        if ($networkNames -contains $name) {
            $containers = @(Invoke-Docker -Arguments @(
                "network", "inspect", $name, "--format", "{{json .Containers}}"
            ))
            if ($containers.Count -eq 1 -and ([string]$containers[0]).Trim() -eq "{}") {
                Invoke-Docker -Arguments @("network", "rm", $name) | Out-Null
            }
        }
    }
    Remove-CompatBuildTag
}

try {
    foreach ($source in @(
        $ComposeSource, $CollectorSource, $CompatDockerfileSource,
        $CompatPatcherSource
    )) {
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "required deployment source is missing: $source"
        }
    }
    Invoke-Docker -Arguments @(
        "compose", "--project-name", $ProjectName, "--file", $ComposeSource,
        "config", "--quiet"
    ) | Out-Null
    $imageInspect = (Invoke-Docker -Arguments @("image", "inspect", $ExpectedRepoDigest)) |
        ConvertFrom-Json
    if (
        @($imageInspect).Count -ne 1 -or
        [string]$imageInspect[0].Id -ne $ExpectedImageId -or
        @($imageInspect[0].RepoDigests) -notcontains $ExpectedRepoDigest
    ) {
        throw "local Docker image does not match the expected repo digest and image ID"
    }

    Capture-OldRuntimeState
    foreach ($directory in @(
        (Split-Path -Parent $ComposeTarget), (Split-Path -Parent $CollectorTarget),
        (Split-Path -Parent $ReceiptTarget), $OutputRoot
    )) {
        if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
            New-Item -ItemType Directory -Path $directory -Force | Out-Null
        }
    }
    foreach ($target in @($ComposeTarget, $CollectorTarget, $ReceiptTarget)) {
        Assert-TargetWritable -Path $target
    }
    if (@(Get-ChildItem -LiteralPath $OutputRoot -Recurse -File -Force).Count -ne 0) {
        throw "output root must be empty before installation"
    }
    if ($ReuseCurrentPublishedImage) {
        $compatImage = Get-ValidatedPublishedApiCompatImage
        Get-ValidatedRuntime | Out-Null
        Assert-StableServiceEpochs -Expected $StableServiceEpochs
    }
    else {
        $OldApiCompatImageId = Get-OptionalImageId -Reference $ApiCompatImage
        $compatImage = Build-ValidatedApiCompatImage
    }
    if ($ComposeExisted) {
        Copy-Item -LiteralPath $ComposeTarget -Destination $ComposeBackup
        $ComposeBackupCreated = $true
    }
    if ($CollectorExisted) {
        Copy-Item -LiteralPath $CollectorTarget -Destination $CollectorBackup
        $CollectorBackupCreated = $true
    }
    if ($ReceiptExisted) {
        Copy-Item -LiteralPath $ReceiptTarget -Destination $ReceiptBackup
        $ReceiptBackupCreated = $true
    }

    $MutationStarted = $true
    if (-not $ReuseCurrentPublishedImage) {
        Invoke-Docker -Arguments @(
            "tag", $ExpectedApiCompatImageId, $ApiCompatImage
        ) | Out-Null
        $CompatTagSwitched = $true
        if ((Get-OptionalImageId -Reference $ApiCompatImage) -ne $ExpectedApiCompatImageId) {
            throw "MinerU API compatibility publish tag did not bind the built image ID"
        }
    }
    Copy-Item -LiteralPath $ComposeSource -Destination $ComposeTarget -Force
    Copy-Item -LiteralPath $CollectorSource -Destination $CollectorTarget -Force
    if (
        (Get-FileHash -Algorithm SHA256 -LiteralPath $ComposeSource).Hash -ne
            (Get-FileHash -Algorithm SHA256 -LiteralPath $ComposeTarget).Hash -or
        (Get-FileHash -Algorithm SHA256 -LiteralPath $CollectorSource).Hash -ne
            (Get-FileHash -Algorithm SHA256 -LiteralPath $CollectorTarget).Hash
    ) {
        throw "deployed compose or collector bytes changed during copy"
    }

    $DeploymentAttempted = $true
    if ($ReuseCurrentPublishedImage) {
        Invoke-Docker -Arguments @(
            "compose", "--project-name", $ProjectName, "--file", $ComposeTarget,
            "up", "--detach", "--no-build", "--no-deps", "--force-recreate",
            "mineru-api"
        ) | Out-Null
    }
    else {
        Invoke-Docker -Arguments @(
            "compose", "--project-name", $ProjectName, "--file", $ComposeTarget,
            "up", "--detach", "--remove-orphans"
        ) | Out-Null
    }
    $runtime = Get-ValidatedRuntime
    if ($ReuseCurrentPublishedImage) {
        Assert-StableServiceEpochs -Expected $StableServiceEpochs
        if ((Get-OptionalImageId -Reference $ApiCompatImage) -ne $CampaignApiCompatImageId) {
            throw "campaign image tag drifted during API-only deployment"
        }
    }
    $collectorOutput = @(
        & $CollectorTarget -ComposePath $ComposeTarget -OutputRoot $OutputRoot
    )
    if ($collectorOutput.Count -ne 1) {
        throw "formal runtime collector did not return one observation"
    }
    $collectorObservation = ([string]$collectorOutput[0]) | ConvertFrom-Json
    if ([string]$collectorObservation.schema -ne "mineru-windows-runtime-observation.v3") {
        throw "formal runtime collector contract drifted"
    }
    Remove-CompatBuildTag
    $receipt = [ordered]@{
        schema = "mineru-windows-install-receipt.v2"
        installed_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        success = $true
        compose_path = $ComposeTarget
        compose_sha256 = "sha256:$((Get-FileHash -Algorithm SHA256 -LiteralPath $ComposeTarget).Hash.ToLowerInvariant())"
        collector_path = $CollectorTarget
        collector_sha256 = "sha256:$((Get-FileHash -Algorithm SHA256 -LiteralPath $CollectorTarget).Hash.ToLowerInvariant())"
        base_repo_digest = $ExpectedRepoDigest
        base_image_id = $ExpectedImageId
        api_compatibility_image = $compatImage
        compose_backup = if ($ComposeExisted) { $ComposeBackup } else { $null }
        collector_backup = if ($CollectorExisted) { $CollectorBackup } else { $null }
        collector_observation_schema = [string]$collectorObservation.schema
        runtime = $runtime
    }
    $receiptJson = $receipt | ConvertTo-Json -Depth 100
    Write-Utf8NoBom -Path $ReceiptTarget -Value ($receiptJson + "`n")
    $receiptJson
}
catch {
    $originalError = $_.Exception.Message
    if (-not $MutationStarted) {
        $cleanupError = $null
        try { Remove-CompatBuildTag }
        catch { $cleanupError = $_.Exception.Message }
        if ($null -ne $cleanupError) {
            throw "installation preflight failed: $originalError; temporary image cleanup also failed: $cleanupError"
        }
        throw "installation preflight failed before runtime mutation: $originalError"
    }
    $rollbackError = $null
    try { Restore-PreviousDeployment }
    catch { $rollbackError = $_.Exception.Message }
    if ($null -ne $rollbackError) {
        throw "installation failed: $originalError; rollback also failed: $rollbackError"
    }
    throw "installation failed and previous deployment was restored: $originalError"
}
