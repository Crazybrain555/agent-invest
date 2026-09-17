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
    [switch]$ApiOnlyCompatibilityUpgrade,
    [ValidateSet("", "cpu", "cuda0")][string]$ApiDeviceProfile = "",
    [string]$CampaignApiCompatImageId = "",
    [ValidateSet(1)][int]$ExpectedApiTaskSlots = 1,
    [ValidateSet(1)][int]$ExpectedApiMaxPendingTasks = 1,
    [string]$CapacityConfigSource = "",
    [ValidatePattern('\A(|sha256:[a-f0-9]{64})\z')][string]$ExpectedCapacityConfigSha256 = "",
    [string]$OperationRecordDirectory = "",
    [ValidateRange(60, 86400)][int]$OperationBudgetSeconds = 1200
)

$ErrorActionPreference = "Stop"
if ($ReuseCurrentPublishedImage -and $ApiOnlyCompatibilityUpgrade) {
    throw "API compatibility upgrade and published-image reuse are mutually exclusive"
}
$ApiOnlyOperation = $ReuseCurrentPublishedImage -or $ApiOnlyCompatibilityUpgrade
$ExplicitCapacity = -not [string]::IsNullOrEmpty($CapacityConfigSource)
if ($ExplicitCapacity -ne (-not [string]::IsNullOrEmpty($ExpectedCapacityConfigSha256))) {
    throw "capacity config source and expected SHA must be supplied together"
}
if ($ExplicitCapacity -and ($PSBoundParameters.ContainsKey("ExpectedApiTaskSlots") -or
        $PSBoundParameters.ContainsKey("ExpectedApiMaxPendingTasks"))) {
    throw "explicit capacity cannot also select legacy task or pending limits"
}
if ($ApiDeviceProfile -ne "" -and (-not $ApiOnlyCompatibilityUpgrade -or -not $ExplicitCapacity)) {
    throw "API device selection requires explicit-capacity API-only compatibility upgrade"
}
$PreviousApiDeviceProfile = $null
$CapacityInputs = $null
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

# BEGIN MINERU NATIVE PROCESS V2
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

# One optional monotonic budget for every native process this script runs.
# Each call also carries its own deadline; the effective deadline is the
# smaller one, measured from the actual process start.
$script:NativeProcessBudget = $null
function Start-NativeProcessBudget {
    param([Parameter(Mandatory = $true)][long]$Seconds)
    if ($Seconds -lt 1 -or $Seconds -gt 86400) { throw "native process budget out of range" }
    $script:NativeProcessBudget = [pscustomobject]@{
        Clock = [Diagnostics.Stopwatch]::StartNew()
        TotalMilliseconds = [long]$Seconds * 1000
    }
}

function Get-NativeProcessRemainingMilliseconds {
    if ($null -eq $script:NativeProcessBudget) { return [long]::MaxValue }
    return [long]($script:NativeProcessBudget.TotalMilliseconds - $script:NativeProcessBudget.Clock.ElapsedMilliseconds)
}

function New-NativeProcessRetention {
    return [ordered]@{ Head = [IO.MemoryStream]::new(); Tail = [IO.MemoryStream]::new(); Total = [long]0; Eof = $false }
}

function Add-NativeProcessRetention {
    param($Retention, [byte[]]$Buffer, [int]$Count, [long]$Limit, [bool]$HeadTail)
    $Retention.Total += $Count
    $headRoom = $Limit - [long]$Retention.Head.Length
    if ($headRoom -gt 0) {
        $take = [int][Math]::Min($headRoom, $Count)
        $Retention.Head.Write($Buffer, 0, $take)
    }
    if (-not $HeadTail) {
        return ($Retention.Total -le $Limit)
    }
    $Retention.Tail.Write($Buffer, 0, $Count)
    if ($Retention.Tail.Length -gt (2 * $Limit)) {
        $kept = $Retention.Tail.ToArray()
        $Retention.Tail.SetLength(0)
        $Retention.Tail.Write($kept, $kept.Length - [int]$Limit, [int]$Limit)
    }
    return $true
}

function Get-NativeProcessRetentionText {
    param($Retention, [long]$Limit, [bool]$HeadTail)
    $utf8 = New-Object Text.UTF8Encoding($false)
    $head = $utf8.GetString($Retention.Head.ToArray())
    if (-not $HeadTail -or $Retention.Total -le $Limit) { return $head }
    $tailBytes = $Retention.Tail.ToArray()
    $tailStart = [Math]::Max(0, $tailBytes.Length - [int]$Limit)
    $tail = $utf8.GetString($tailBytes, $tailStart, $tailBytes.Length - $tailStart)
    $dropped = $Retention.Total - $Retention.Head.Length - ($tailBytes.Length - $tailStart)
    return ($head + "`n... [native process output: $dropped bytes dropped] ...`n" + $tail)
}

function Invoke-NativeProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][AllowEmptyString()][object[]]$Arguments,
        [AllowNull()][string]$StandardInput = $null,
        [long]$TimeoutMilliseconds = 120000,
        [long]$MaximumOutputBytes = 1048576,
        [switch]$HeadTail
    )
    Assert-NativeProcessArguments -Arguments $Arguments
    if ($TimeoutMilliseconds -lt 1000 -or $MaximumOutputBytes -lt 1024) {
        throw "native process bounds out of range"
    }
    $deadline = [Math]::Min($TimeoutMilliseconds, (Get-NativeProcessRemainingMilliseconds))
    if ($deadline -le 0) { throw "native process budget exhausted before start: $FilePath" }
    $retainLimit = if ($HeadTail) { [long][Math]::Max(1024, [Math]::Floor($MaximumOutputBytes / 2)) } else { $MaximumOutputBytes }
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

    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    $started = $false
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $stdout = New-NativeProcessRetention
    $stderr = New-NativeProcessRetention
    try {
        $started = $process.Start()
        if (-not $started) { throw "native process did not start: $FilePath" }
        # Standard input is written asynchronously under the same deadline: a
        # child that never reads its stdin cannot block this supervisor.
        $inputTask = $null
        $inputStream = $null
        if ($null -ne $StandardInput) {
            $inputBytes = [Text.Encoding]::UTF8.GetBytes($StandardInput)
            $inputStream = $process.StandardInput.BaseStream
            $inputTask = $inputStream.WriteAsync($inputBytes, 0, $inputBytes.Length)
        }
        # Both pipes are drained continuously into bounded retention so a full
        # pipe can never block the child, and the deadline is enforced during
        # the drain rather than only after it.
        $outStream = $process.StandardOutput.BaseStream
        $errStream = $process.StandardError.BaseStream
        $outBuffer = [byte[]]::new(16384)
        $errBuffer = [byte[]]::new(16384)
        $outTask = $outStream.ReadAsync($outBuffer, 0, $outBuffer.Length)
        $errTask = $errStream.ReadAsync($errBuffer, 0, $errBuffer.Length)
        while (-not ($stdout.Eof -and $stderr.Eof)) {
            $remaining = $deadline - $clock.ElapsedMilliseconds
            if ($remaining -le 0) {
                throw "native_outcome_unknown: native process deadline exceeded after $($clock.ElapsedMilliseconds) ms: $FilePath $($Arguments -join ' ')"
            }
            $pending = @()
            if ($null -ne $inputTask) { $pending += $inputTask }
            if (-not $stdout.Eof) { $pending += $outTask }
            if (-not $stderr.Eof) { $pending += $errTask }
            $index = [Threading.Tasks.Task]::WaitAny([Threading.Tasks.Task[]]$pending, [int][Math]::Min($remaining, 1000))
            if ($index -lt 0) { continue }
            $task = $pending[$index]
            if ([object]::ReferenceEquals($task, $inputTask)) {
                # A completed write yields a VoidTaskResult; discard it so only
                # the final result object reaches the caller, but keep awaiting
                # so a faulted write still raises here.
                [void]$task.GetAwaiter().GetResult()
                $inputStream.Flush()
                $process.StandardInput.Close()
                $inputTask = $null
                continue
            }
            $count = $task.GetAwaiter().GetResult()
            if ([object]::ReferenceEquals($task, $outTask)) {
                if ($count -eq 0) { $stdout.Eof = $true }
                else {
                    if (-not (Add-NativeProcessRetention $stdout $outBuffer $count $retainLimit ([bool]$HeadTail))) {
                        throw "native process stdout exceeded $MaximumOutputBytes bytes: $FilePath"
                    }
                    $outTask = $outStream.ReadAsync($outBuffer, 0, $outBuffer.Length)
                }
            }
            else {
                if ($count -eq 0) { $stderr.Eof = $true }
                else {
                    if (-not (Add-NativeProcessRetention $stderr $errBuffer $count $retainLimit ([bool]$HeadTail))) {
                        throw "native process stderr exceeded $MaximumOutputBytes bytes: $FilePath"
                    }
                    $errTask = $errStream.ReadAsync($errBuffer, 0, $errBuffer.Length)
                }
            }
        }
        $remaining = [Math]::Max(1, $deadline - $clock.ElapsedMilliseconds)
        if (-not $process.WaitForExit([int][Math]::Min($remaining, [int]::MaxValue))) {
            throw "native_outcome_unknown: native process did not exit after closing its pipes: $FilePath"
        }
        return [pscustomobject]@{
            ExitCode = [int]$process.ExitCode
            StandardOutput = [string](Get-NativeProcessRetentionText $stdout $retainLimit ([bool]$HeadTail))
            StandardError = [string](Get-NativeProcessRetentionText $stderr $retainLimit ([bool]$HeadTail))
            StandardOutputBytes = [long]$stdout.Total
            StandardErrorBytes = [long]$stderr.Total
            Truncated = [bool]($HeadTail -and ($stdout.Total -gt $retainLimit -or $stderr.Total -gt $retainLimit))
            ElapsedMilliseconds = [long]$clock.ElapsedMilliseconds
        }
    }
    catch {
        $originalError = $_
        if ($started) {
            try {
                if (-not $process.HasExited) { $process.Kill() }
                [void]$process.WaitForExit(30000)
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
        [int[]]$AllowedExitCodes = @(0),
        [long]$TimeoutMilliseconds = 120000,
        [long]$MaximumOutputBytes = 1048576,
        [switch]$HeadTail
    )
    Assert-NativeProcessArguments -Arguments $Arguments
    $result = Invoke-NativeProcess -FilePath $DockerCommand -Arguments $Arguments `
        -StandardInput $StandardInput -TimeoutMilliseconds $TimeoutMilliseconds `
        -MaximumOutputBytes $MaximumOutputBytes -HeadTail:$HeadTail
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
        [Parameter(Mandatory = $true)][AllowEmptyString()][object[]]$Arguments,
        [long]$TimeoutMilliseconds = 120000,
        [long]$MaximumOutputBytes = 1048576,
        [switch]$HeadTail
    )
    Assert-NativeProcessArguments -Arguments $Arguments
    $result = Invoke-DockerProcess -Arguments $Arguments -TimeoutMilliseconds $TimeoutMilliseconds `
        -MaximumOutputBytes $MaximumOutputBytes -HeadTail:$HeadTail
    return @(ConvertFrom-NativeProcessText -Value $result.StandardOutput)
}
# END MINERU NATIVE PROCESS V2

# One monotonic budget covers every native command of this installation,
# including rollback. A command that exceeds it fails; nothing is retried.
Start-NativeProcessBudget -Seconds $OperationBudgetSeconds
$OperationRecordCount = 0
function Write-OperationRecord {
    param([Parameter(Mandatory = $true)][string]$Name, [Parameter(Mandatory = $true)][object]$Value)
    if ([string]::IsNullOrEmpty($OperationRecordDirectory)) { return }
    $script:OperationRecordCount++
    $payload = [ordered]@{ sequence = $script:OperationRecordCount; utc = (Get-Date).ToUniversalTime().ToString("o") }
    foreach ($property in $Value.GetEnumerator()) { $payload[$property.Key] = $property.Value }
    $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes(($payload | ConvertTo-Json -Depth 8 -Compress))
    $stream = [IO.File]::Open((Join-Path $OperationRecordDirectory $Name), [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
}
if (-not [string]::IsNullOrEmpty($OperationRecordDirectory)) {
    $OperationRecordDirectory = [IO.Path]::GetFullPath($OperationRecordDirectory)
    if (Test-Path -LiteralPath $OperationRecordDirectory) { throw "operation record directory must be new" }
    New-Item -ItemType Directory -Path $OperationRecordDirectory | Out-Null
}
Write-OperationRecord "installer-started.json" ([ordered]@{
    contract_version = "mineru.installer-operation-record.v1"; phase = "started"; pid = $PID
    operation_budget_seconds = $OperationBudgetSeconds; api_only = [bool]$ApiOnlyOperation
    explicit_capacity = [bool]$ExplicitCapacity; api_device_profile = $ApiDeviceProfile
    expected_capacity_config_sha256 = $ExpectedCapacityConfigSha256
})

$ProjectName = "mineru-tailnet"
$ApiCompatImage = "agent-invest/mineru-api:3.4.4-serial-v1"
$ApiCompatBuildTag = "agent-invest/mineru-api:build-$([Guid]::NewGuid().ToString('N'))"
$HeapReturnPolicy = "glibc-malloc-trim-per-window.v1"
$CapacityPolicy = if ($ExplicitCapacity) { "single-process-explicit-capacity.v1" } else { "single-owner-serial-mineru.v1" }
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
$PreDeploymentOutputState = $null
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

function Get-CanonicalObjectJson {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value) { return "null" }
    if ($Value -is [System.Collections.IDictionary]) {
        $parts = @($Value.Keys | Sort-Object | ForEach-Object {
            ($_ | ConvertTo-Json -Compress) + ":" + (Get-CanonicalObjectJson $Value[$_])
        })
        return "{" + ($parts -join ",") + "}"
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        $parts = @($Value | ForEach-Object { Get-CanonicalObjectJson $_ })
        return "[" + ($parts -join ",") + "]"
    }
    if ($Value.GetType().FullName -ceq "System.Management.Automation.PSCustomObject") {
        $parts = @($Value.PSObject.Properties.Name | Sort-Object | ForEach-Object {
            ($_ | ConvertTo-Json -Compress) + ":" + (Get-CanonicalObjectJson $Value.$_)
        })
        return "{" + ($parts -join ",") + "}"
    }
    return ($Value | ConvertTo-Json -Compress)
}

function Get-ExplicitCapacityInputs {
    if (-not $ExplicitCapacity) { return $null }
    $context = Split-Path -Parent ([IO.Path]::GetFullPath($CompatDockerfileSource))
    $path = [IO.Path]::GetFullPath($CapacityConfigSource)
    if ($path -ine (Join-Path $context "capacity-config.json")) {
        throw "capacity config must be the explicit build context capacity-config.json"
    }
    $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        if ($stream.Length -eq 0 -or $stream.Length -gt 65536) { throw "capacity config byte bound exceeded" }
        $buffer = New-Object byte[] 65537
        $count = 0
        do {
            $read = $stream.Read($buffer, $count, $buffer.Length - $count)
            $count += $read
        } while ($read -gt 0 -and $count -lt $buffer.Length)
        if ($count -eq 0 -or $count -gt 65536 -or $count -ne $stream.Length) { throw "capacity config changed or exceeds bound" }
        $raw = New-Object byte[] $count
        [Array]::Copy($buffer, $raw, $count)
    } finally { $stream.Dispose() }
    $utf8 = New-Object Text.UTF8Encoding($false, $true)
    $text = $utf8.GetString($raw)
    if ((Get-Sha256Text $text) -cne $ExpectedCapacityConfigSha256) { throw "capacity config SHA differs" }
    $config = $text | ConvertFrom-Json
    $limits = @{
        parse_active_limit=128; total_nonterminal_limit=128; finalizer_active_limit=128;
        final_http_limit_per_loop=128; api_process_limit=1; api_event_loop_limit=1;
        processing_window_size=1024; omp_num_threads=256; mkl_num_threads=256;
        openblas_num_threads=256; pdf_render_processes_requested=256;
        result_reservation_bytes=[long]::MaxValue; max_unacked_result_bytes=[long]::MaxValue
    }
    $names = @($limits.Keys) + @("contract_version", "hybrid_batch_ratio_requested", "pipeline_inference_locks")
    if ((@($config.PSObject.Properties.Name | Sort-Object) -join ",") -cne
            (@($names | Sort-Object) -join ",") -or
        $config.contract_version -isnot [string] -or $config.contract_version -cne "mineru.capacity-config.v1" -or
        $config.pipeline_inference_locks -isnot [bool] -or -not $config.pipeline_inference_locks -or
        ($config.hybrid_batch_ratio_requested -isnot [int] -and $config.hybrid_batch_ratio_requested -isnot [long]) -or
        $config.hybrid_batch_ratio_requested -notin @(1,2,4,8)) { throw "capacity config fields or policy differ" }
    foreach ($name in $limits.Keys) {
        $value = $config.$name
        if (($value -isnot [int] -and $value -isnot [long]) -or $value -lt 1 -or $value -gt $limits[$name]) {
            throw "capacity config integer outside supported envelope: $name"
        }
    }
    if ($config.parse_active_limit -gt $config.total_nonterminal_limit -or
        $config.finalizer_active_limit -gt $config.total_nonterminal_limit -or
        $config.result_reservation_bytes -gt $config.max_unacked_result_bytes -or
        (Get-CanonicalObjectJson $config) -cne $text) { throw "capacity config is not canonical or internally consistent" }
    $mapping = @{
        MINERU_API_MAX_CONCURRENT_REQUESTS="parse_active_limit"; MINERU_API_MAX_PENDING_TASKS="total_nonterminal_limit";
        MINERU_API_FINALIZER_SLOTS="finalizer_active_limit"; MINERU_PROCESSING_WINDOW_SIZE="processing_window_size";
        OMP_NUM_THREADS="omp_num_threads"; MKL_NUM_THREADS="mkl_num_threads"; OPENBLAS_NUM_THREADS="openblas_num_threads";
        MINERU_PDF_RENDER_THREADS="pdf_render_processes_requested"; MINERU_HYBRID_BATCH_RATIO="hybrid_batch_ratio_requested";
        MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES="result_reservation_bytes";
        MINERU_TASK_PROTOCOL_V2_MAX_UNACKED_BYTES="max_unacked_result_bytes"
    }
    $environment = [ordered]@{}
    foreach ($name in ($mapping.Keys | Sort-Object)) { $environment[$name] = [string]$config.($mapping[$name]) }
    $environment["MINERU_ENABLE_PIPELINE_INFERENCE_LOCKS"] = "1"
    $sources = [ordered]@{}
    foreach ($name in @("bootstrap", "config", "file", "observation")) {
        $source = Join-Path $context "agent_capacity_$name.py"
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "capacity source missing: $source" }
        $sources["mineru/cli/agent_capacity_$name.py"] = "sha256:$((Get-FileHash -Algorithm SHA256 -LiteralPath $source).Hash.ToLowerInvariant())"
    }
    return [ordered]@{
        config=$config; config_sha256=$ExpectedCapacityConfigSha256; config_bytes=$raw;
        source_sha256=$sources; sources_sha256=(Get-Sha256Text (Get-CanonicalObjectJson $sources));
        environment=$environment; build_target="explicit-capacity"; context=$context
    }
}

function Get-ResolvedCompose {
    param([Parameter(Mandatory=$true)][string]$Path)
    $raw = Invoke-Docker -Arguments @("compose", "--project-name", $ProjectName, "--file", $Path, "config", "--format", "json")
    return (($raw -join "`n") | ConvertFrom-Json)
}

function Assert-CapacityCompose {
    param([Parameter(Mandatory=$true)][object]$Compose)
    $api = $Compose.services."mineru-api"
    foreach ($name in $CapacityInputs.environment.Keys) {
        if ([string]$api.environment.$name -cne $CapacityInputs.environment[$name]) {
            throw "compose capacity projection differs: $name"
        }
    }
    foreach ($name in @("MINERU_CAPACITY_CONFIG_PATH", "MINERU_CAPACITY_CONFIG_SHA256")) {
        if ($null -ne $api.environment.PSObject.Properties[$name]) { throw "compose overrides baked capacity anchor" }
    }
    $expected = @("--host", "0.0.0.0", "--port", "8000", "--allow-public-http-client", "--max-concurrency",
        [string]$CapacityInputs.config.final_http_limit_per_loop)
    if ((Get-CanonicalObjectJson @($api.command)) -cne (Get-CanonicalObjectJson $expected)) {
        throw "compose API command differs from configured shared HTTP limit"
    }
}

# BEGIN MINERU API DEVICE PROFILE V1
function Get-ApiDeviceProfile {
    param([Parameter(Mandatory=$true)][object]$Api)
    $entry = $Api.environment.PSObject.Properties['MINERU_DEVICE_MODE']
    $mode = 'cpu'
    if ($null -ne $entry) { $mode = $entry.Value }
    if ($mode -isnot [string] -or $mode -cnotin @('cpu','cuda:0')) {
        throw 'API device mode must be cpu or cuda:0'
    }
    $devices = @($Api.deploy.resources.reservations.devices | Where-Object { $null -ne $_ })
    if ($mode -ceq 'cpu') {
        if ($devices.Count -ne 0) { throw 'CPU API must not reserve GPU devices' }
        return 'cpu'
    }
    if ($devices.Count -ne 1) { throw 'CUDA API requires one exact GPU reservation' }
    $d = $devices[0]
    $keys = @($d.PSObject.Properties.Name | Sort-Object) -join ','
    if ($keys -cnotin @('capabilities,device_ids,driver','capabilities,device_ids,driver,options') -or
        $d.driver -cne 'nvidia' -or $d.device_ids -isnot [array] -or @($d.device_ids).Count -ne 1 -or
        $d.device_ids[0] -isnot [string] -or $d.device_ids[0] -cne '0' -or
        $d.capabilities -isnot [array] -or @($d.capabilities).Count -ne 1 -or $d.capabilities[0] -cne 'gpu' -or
        ($null -ne $d.options -and @($d.options.PSObject.Properties).Count -ne 0)) {
        throw 'CUDA API reservation must select only NVIDIA GPU0 with gpu capability'
    }
    return 'cuda0'
}

function Assert-ApiDeviceRuntime {
    param([Parameter(Mandatory=$true)][object]$Container,
          [Parameter(Mandatory=$true)][ValidateSet('cpu','cuda0')][string]$Profile)
    if ($Container.HostConfig.Privileged -isnot [bool] -or $Container.HostConfig.Privileged -or
        @($Container.HostConfig.Devices | Where-Object { $null -ne $_ }).Count -ne 0 -or
        @($Container.HostConfig.CapAdd | Where-Object { $null -ne $_ }).Count -ne 0) {
        throw 'API GPU profile forbids privileged mode, direct devices and added capabilities'
    }
    $modes = @($Container.Config.Env | Where-Object { $_ -clike 'MINERU_DEVICE_MODE=*' })
    $devices = @($Container.HostConfig.DeviceRequests | Where-Object { $null -ne $_ })
    if ($Profile -ceq 'cpu') {
        if ($modes.Count -gt 1 -or ($modes.Count -eq 1 -and $modes[0] -cne 'MINERU_DEVICE_MODE=cpu') -or
            $devices.Count -ne 0) { throw 'actual CPU API device profile drifted' }
        return
    }
    if ($modes.Count -ne 1 -or $modes[0] -cne 'MINERU_DEVICE_MODE=cuda:0' -or $devices.Count -ne 1) {
        throw 'actual CUDA API device mode or request count drifted'
    }
    $d = $devices[0]
    if ((@($d.PSObject.Properties.Name | Sort-Object) -join ',') -cne 'Capabilities,Count,DeviceIDs,Driver,Options' -or
        $d.Driver -cne 'nvidia' -or ($d.Count -isnot [int] -and $d.Count -isnot [long]) -or $d.Count -ne 0 -or
        $d.DeviceIDs -isnot [array] -or @($d.DeviceIDs).Count -ne 1 -or $d.DeviceIDs[0] -isnot [string] -or $d.DeviceIDs[0] -cne '0' -or
        $d.Capabilities -isnot [array] -or @($d.Capabilities).Count -ne 1 -or
        $d.Capabilities[0] -isnot [array] -or @($d.Capabilities[0]).Count -ne 1 -or
        $d.Capabilities[0][0] -cne 'gpu' -or
        ($null -ne $d.Options -and @($d.Options.PSObject.Properties).Count -ne 0)) {
        throw 'actual CUDA API must expose only the exact NVIDIA GPU0 request'
    }
}
# END MINERU API DEVICE PROFILE V1

function Assert-ApiDeviceTransition {
    param([Parameter(Mandatory=$true)][object]$Next,
          [Parameter(Mandatory=$true)][object]$Previous,
          [Parameter(Mandatory=$true)][ValidateSet('cpu','cuda0')][string]$Profile)
    if ((Get-ApiDeviceProfile -Api $Next.services.'mineru-api') -cne $Profile) {
        throw 'candidate API device profile differs from explicit selection'
    }
    Get-ApiDeviceProfile -Api $Previous.services.'mineru-api' | Out-Null
    # Clone before normalization; do not erase caller evidence. No capacity or
    # command fields are masked in a device-only comparison.
    $normalized = @($Next, $Previous | ForEach-Object { $_ | ConvertTo-Json -Depth 100 -Compress | ConvertFrom-Json })
    foreach ($item in $normalized) {
        $api = $item.services.'mineru-api'
        $api.environment.PSObject.Properties.Remove('MINERU_DEVICE_MODE')
        if ($null -ne $api.deploy.resources.reservations) {
            $api.deploy.resources.reservations.PSObject.Properties.Remove('devices')
            if (@($api.deploy.resources.reservations.PSObject.Properties).Count -eq 0) {
                $api.deploy.resources.PSObject.Properties.Remove('reservations')
            }
        }
        if ($null -ne $api.deploy.resources -and @($api.deploy.resources.PSObject.Properties).Count -eq 0) {
            $api.deploy.PSObject.Properties.Remove('resources')
        }
        # Compose renders an empty placement object when deploy is introduced.
        # Remove only that empty object; preserve any actual placement policy.
        if ($api.deploy.placement -is [PSCustomObject] -and
            @($api.deploy.placement.PSObject.Properties).Count -eq 0) {
            $api.deploy.PSObject.Properties.Remove('placement')
        }
        if ($null -ne $api.deploy -and @($api.deploy.PSObject.Properties).Count -eq 0) {
            $api.PSObject.Properties.Remove('deploy')
        }
    }
    if ((Get-CanonicalObjectJson $normalized[0]) -cne (Get-CanonicalObjectJson $normalized[1])) {
        throw 'API device transition changed configuration outside GPU0 and device mode'
    }
}

function Assert-ApiOnlyUpgradeInputs {
    if (-not $ComposeExisted -or -not $CollectorExisted -or -not $ReceiptExisted) {
        throw "API-only compatibility upgrade requires a complete existing deployment"
    }
    if ($ExplicitCapacity) {
        $next = Get-ResolvedCompose $ComposeSource
        $previous = Get-ResolvedCompose $ComposeTarget
        Assert-CapacityCompose $next
        if ($ApiDeviceProfile -ne "") {
            Assert-ApiDeviceTransition -Next $next -Previous $previous -Profile $ApiDeviceProfile
            $script:PreviousApiDeviceProfile = Get-ApiDeviceProfile -Api $previous.services."mineru-api"
            return
        }
        # Only the selected API capacity fields may differ. Inference/proxy,
        # networks, mounts, image name and every other setting remain exact.
        foreach ($item in @($next, $previous)) {
            foreach ($name in $CapacityInputs.environment.Keys) {
                $item.services."mineru-api".environment.PSObject.Properties.Remove($name)
            }
            $command = @($item.services."mineru-api".command)
            if ($command.Count -ne 7 -or ($command[0..5] -join ",") -cne
                    "--host,0.0.0.0,--port,8000,--allow-public-http-client,--max-concurrency") {
                throw "API-only capacity upgrade requires the original command shape"
            }
            $item.services."mineru-api".command = @($command[0..5]) + @("capacity-value")
        }
        if ((Get-CanonicalObjectJson $next) -cne (Get-CanonicalObjectJson $previous)) {
            throw "API-only capacity upgrade changed configuration outside API capacity fields"
        }
    }
    elseif ((Get-FileHash -Algorithm SHA256 -LiteralPath $ComposeSource).Hash -ne
            (Get-FileHash -Algorithm SHA256 -LiteralPath $ComposeTarget).Hash) {
        throw "API-only compatibility upgrade requires unchanged compose bytes"
    }
}

function Invoke-ApiOnlyRecreate {
    Invoke-Docker -Arguments @(
        "compose", "--project-name", $ProjectName, "--file", $ComposeTarget,
        "up", "--detach", "--no-build", "--no-deps", "--force-recreate",
        "mineru-api"
    ) -TimeoutMilliseconds 900000 | Out-Null
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

function Assert-ClosedProperties {
    param([object]$Value, [string[]]$Names, [string]$Label)
    if ($null -eq $Value -or $Value.GetType().FullName -cne "System.Management.Automation.PSCustomObject" -or (@($Value.PSObject.Properties.Name | Sort-Object) -join ",") -cne
            (@($Names | Sort-Object) -join ",")) { throw "$Label fields are not closed" }
}

function Assert-CapacityIdleHealth {
    param([object]$Health, [AllowNull()][object]$ExpectedCapacity, [string]$Label)
    Assert-ClosedProperties $Health @("status", "version", "protocol_version", "max_concurrent_requests",
        "max_pending_tasks_requested", "max_pending_tasks_effective", "processing_window_size",
        "task_retention_seconds", "task_cleanup_interval_seconds", "task_protocol_schema", "queued_tasks",
        "processing_tasks", "completed_tasks", "failed_tasks", "task_protocol_runtime", "task_admission",
        "capacity_observation") $Label
    foreach ($name in @("protocol_version", "max_concurrent_requests", "max_pending_tasks_requested",
        "max_pending_tasks_effective", "processing_window_size", "task_retention_seconds",
        "task_cleanup_interval_seconds", "queued_tasks", "processing_tasks", "completed_tasks", "failed_tasks")) {
        if (($Health.$name -isnot [int] -and $Health.$name -isnot [long]) -or $Health.$name -lt 0) {
            throw "$Label noninteger health counter: $name"
        }
    }
    if ($Health.status -isnot [string] -or $Health.version -isnot [string] -or
        $Health.task_protocol_schema -isnot [string] -or
        $Health.status -cne "healthy" -or $Health.version -cne "3.4.4" -or $Health.protocol_version -ne 2 -or
        $Health.task_protocol_schema -cne "mineru-task-protocol.v2" -or $Health.queued_tasks -ne 0 -or
        $Health.processing_tasks -ne 0 -or $Health.task_retention_seconds -ne 600 -or
        $Health.task_cleanup_interval_seconds -ne 30) { throw "$Label capacity API is not healthy and idle" }
    $runtime = $Health.task_protocol_runtime
    Assert-ClosedProperties $runtime @("schema", "enabled", "task_registry_max_records", "task_result_reservation_bytes",
        "max_unacked_result_bytes", "registry_schema", "admission_scope", "capacity_config_sha256") "$Label runtime"
    if ($runtime.schema -isnot [string] -or $runtime.registry_schema -isnot [string] -or
        $runtime.admission_scope -isnot [string] -or $runtime.capacity_config_sha256 -isnot [string] -or
        $runtime.schema -cne "mineru-task-runtime.v3" -or $runtime.enabled -isnot [bool] -or -not $runtime.enabled -or
        $runtime.registry_schema -cne "mineru-task-registry.v3" -or $runtime.admission_scope -cne "post_form_owned_upload" -or
        $runtime.capacity_config_sha256 -cnotmatch '\Asha256:[a-f0-9]{64}\z') { throw "$Label runtime identity differs" }
    foreach ($name in @("task_registry_max_records", "task_result_reservation_bytes", "max_unacked_result_bytes")) {
        if (($runtime.$name -isnot [int] -and $runtime.$name -isnot [long]) -or $runtime.$name -lt 1) {
            throw "$Label runtime limit is invalid: $name"
        }
    }
    if ($runtime.task_registry_max_records -ne 128 -or $runtime.task_result_reservation_bytes -gt $runtime.max_unacked_result_bytes) {
        throw "$Label runtime capacity is inconsistent"
    }
    $observation = $Health.capacity_observation
    Assert-ClosedProperties $observation @("schema", "capacity_config_sha256", "owner", "resolved_limits",
        "http_limiter_state", "stage_counters", "http_counters", "owner_control", "framework_limits", "observed_at") "$Label observation"
    if ($observation.schema -isnot [string] -or $observation.capacity_config_sha256 -isnot [string] -or
        $observation.http_limiter_state -isnot [string] -or
        $observation.schema -cne "mineru.capacity-observation.v1" -or
        $observation.capacity_config_sha256 -cne $runtime.capacity_config_sha256) { throw "$Label config identity differs" }
    $limits = $observation.resolved_limits
    Assert-ClosedProperties $limits @("parse_active_limit", "total_nonterminal_limit", "finalizer_active_limit",
        "result_reservation_bytes", "max_unacked_result_bytes", "final_http_limit_per_loop") "$Label resolved limits"
    foreach ($name in @("parse_active_limit", "total_nonterminal_limit", "finalizer_active_limit", "result_reservation_bytes", "max_unacked_result_bytes")) {
        if (($limits.$name -isnot [int] -and $limits.$name -isnot [long]) -or $limits.$name -lt 1) {
            throw "$Label resolved limit is invalid: $name"
        }
        if ($null -ne $ExpectedCapacity -and $limits.$name -ne $ExpectedCapacity.$name) {
            throw "$Label resolved limit differs from external config: $name"
        }
    }
    if ($limits.parse_active_limit -ne $Health.max_concurrent_requests -or
        $limits.total_nonterminal_limit -ne $Health.max_pending_tasks_requested -or
        $limits.total_nonterminal_limit -ne $Health.max_pending_tasks_effective -or
        $limits.total_nonterminal_limit -gt 128 -or $limits.parse_active_limit -gt $limits.total_nonterminal_limit -or
        $limits.finalizer_active_limit -gt $limits.total_nonterminal_limit -or
        $limits.result_reservation_bytes -ne $runtime.task_result_reservation_bytes -or
        $limits.max_unacked_result_bytes -ne $runtime.max_unacked_result_bytes) { throw "$Label resolved owners disagree" }
    if ($null -ne $ExpectedCapacity -and
        ((Get-Sha256Text (Get-CanonicalObjectJson $ExpectedCapacity)) -cne $runtime.capacity_config_sha256 -or
        $Health.processing_window_size -ne $ExpectedCapacity.processing_window_size)) { throw "$Label expected config differs" }
    if ($observation.http_limiter_state -ceq "not_initialized") {
        if ($null -ne $limits.final_http_limit_per_loop) { throw "$Label lazy HTTP limiter fabricated capacity" }
    } elseif ($observation.http_limiter_state -ceq "initialized") {
        if (($limits.final_http_limit_per_loop -isnot [int] -and $limits.final_http_limit_per_loop -isnot [long]) -or
            $limits.final_http_limit_per_loop -lt 1 -or $limits.final_http_limit_per_loop -gt 128 -or
            ($null -ne $ExpectedCapacity -and $limits.final_http_limit_per_loop -ne $ExpectedCapacity.final_http_limit_per_loop)) {
            throw "$Label shared HTTP limit differs"
        }
    } else { throw "$Label unknown HTTP limiter state" }
    Assert-ClosedProperties $observation.owner_control @("foreign_loop_observed", "soft_drain_requested", "soft_drain_applied", "trigger") "$Label owner control"
    foreach ($name in @("foreign_loop_observed", "soft_drain_requested", "soft_drain_applied")) {
        if ($observation.owner_control.$name -isnot [bool] -or $observation.owner_control.$name) { throw "$Label owner is draining" }
    }
    if ($null -ne $observation.owner_control.trigger) { throw "$Label owner trigger is active" }
    $stages = @("result_capacity_waiting", "parse_waiting", "parse_active", "finalizer_waiting", "finalizer_active")
    $http = @("active_requests", "pending_requests")
    Assert-ClosedProperties $observation.stage_counters $stages "$Label stages"
    Assert-ClosedProperties $observation.http_counters $http "$Label HTTP"
    foreach ($pair in @(@($observation.stage_counters, $stages), @($observation.http_counters, $http))) {
        foreach ($name in $pair[1]) {
            if (($pair[0].$name -isnot [int] -and $pair[0].$name -isnot [long]) -or $pair[0].$name -ne 0) {
                throw "$Label has active stage or HTTP responsibility: $name"
            }
        }
    }
    $admission = $Health.task_admission
    $counters = @("ingress_tasks", "accepted_pending_tasks", "accepted_processing_tasks", "accepted_finalizing_tasks",
        "durable_nonterminal_tasks", "routeless_accepted_tasks", "ingress_cleanup_tasks", "unowned_ingress_tasks",
        "scheduled_tasks", "queue_depth", "active_processors")
    Assert-ClosedProperties $admission (@("schema", "registry_schema", "nonterminal_limit", "recovery_overcommitted", "admission_open", "blocked_reason") + $counters) "$Label admission"
    if ($admission.schema -isnot [string] -or $admission.registry_schema -isnot [string] -or
        $admission.schema -cne "mineru-task-admission.v1" -or $admission.registry_schema -cne "mineru-task-registry.v3" -or
        ($admission.nonterminal_limit -isnot [int] -and $admission.nonterminal_limit -isnot [long]) -or
        $admission.nonterminal_limit -ne $limits.total_nonterminal_limit -or $admission.recovery_overcommitted -isnot [bool] -or
        $admission.recovery_overcommitted -or $admission.admission_open -isnot [bool] -or -not $admission.admission_open -or
        $null -ne $admission.blocked_reason) { throw "$Label admission is not open and idle" }
    foreach ($name in $counters) {
        if (($admission.$name -isnot [int] -and $admission.$name -isnot [long]) -or $admission.$name -ne 0) {
            throw "$Label retains task responsibility: $name"
        }
    }
    Assert-ClosedProperties $observation.owner @("process_id", "process_start_ticks", "boot_id", "loop_epoch") "$Label owner"
    foreach ($name in @("process_id", "process_start_ticks")) {
        if (($observation.owner.$name -isnot [int] -and $observation.owner.$name -isnot [long]) -or $observation.owner.$name -lt 1) {
            throw "$Label invalid serving process identity"
        }
    }
    foreach ($name in @("boot_id", "loop_epoch")) {
        $guid = [Guid]::Empty
        if ($observation.owner.$name -isnot [string] -or -not [Guid]::TryParseExact($observation.owner.$name, "D", [ref]$guid) -or
            $guid.ToString("D") -cne $observation.owner.$name) { throw "$Label invalid serving epoch" }
    }
    Assert-ClosedProperties $observation.observed_at @("clock", "implementation", "started_ns", "completed_ns") "$Label clock"
    $clock = $observation.observed_at
    if ($clock.clock -isnot [string] -or $clock.clock -cne "python.monotonic_ns" -or $clock.implementation -isnot [string] -or
        $clock.implementation.Length -lt 1 -or $clock.implementation.Length -gt 128) { throw "$Label invalid observation clock" }
    foreach ($name in @("started_ns", "completed_ns")) {
        if (($clock.$name -isnot [int] -and $clock.$name -isnot [long]) -or $clock.$name -lt 0) { throw "$Label invalid clock sample" }
    }
    if ($clock.completed_ns -lt $clock.started_ns) { throw "$Label reversed clock sample" }
    $framework = $observation.framework_limits
    $reasons = @{
        torch_intraop_threads=@("serving_getter_not_loaded");
        pdf_render_pool_max_workers=@("serving_pool_not_initialized", "serving_pool_lock_busy");
        mkl_threads=@("no_serving_getter"); openblas_threads=@("no_serving_getter")
    }
    Assert-ClosedProperties $framework @($reasons.Keys) "$Label framework"
    foreach ($name in $reasons.Keys) {
        $item = $framework.$name
        Assert-ClosedProperties $item @("state", "value", "reason") "$Label framework $name"
        if ($item.state -isnot [string]) { throw "$Label framework state is not a scalar string" }
        if ($item.state -ceq "available") {
            if ($name -in @("mkl_threads", "openblas_threads") -or $null -ne $item.reason -or
                ($item.value -isnot [int] -and $item.value -isnot [long]) -or $item.value -lt 1) { throw "$Label unsupported framework getter" }
        } elseif ($item.state -ceq "unavailable") {
            if ($null -ne $item.value -or $item.reason -isnot [string] -or $item.reason -cnotin $reasons[$name]) { throw "$Label framework unknown is not explicit" }
        } else { throw "$Label unknown framework state" }
    }
}

function Assert-CapacityFiles {
    param([Parameter(Mandatory=$true)][string]$ContainerId)
    $code = @'
import hashlib,json,sys
from pathlib import Path
from mineru.cli.agent_capacity_file import read_mineru_capacity_file
from mineru.cli.agent_capacity_config import decode_mineru_capacity_config
raw=read_mineru_capacity_file(Path('/usr/local/etc/mineru/capacity.json'),expected_sha256=sys.argv[1],expected_owner_uid=0)
config=decode_mineru_capacity_config(raw)
sources={}
for name in ('bootstrap','config','file','observation'):
    relative='mineru/cli/agent_capacity_'+name+'.py'
    with open('/usr/local/lib/python3.12/dist-packages/'+relative,'rb') as stream:
        source=stream.read(1024*1024+1)
    if len(source)>1024*1024: raise RuntimeError('capacity source exceeds byte bound')
    sources[relative]='sha256:'+hashlib.sha256(source).hexdigest()
print(json.dumps({'config_sha256':config.sha256,'byte_count':len(raw),'source_sha256':sources},sort_keys=True,separators=(',',':')))
'@
    $result = Invoke-DockerProcess -Arguments @("exec", "-i", $ContainerId, "/usr/bin/python3.12", "-I", "-", $ExpectedCapacityConfigSha256) -StandardInput $code
    $actual = $result.StandardOutput | ConvertFrom-Json
    Assert-ClosedProperties $actual @("config_sha256", "byte_count", "source_sha256") "installed capacity files"
    if ($actual.config_sha256 -isnot [string] -or $actual.config_sha256 -cne $CapacityInputs.config_sha256 -or
        ($actual.byte_count -isnot [int] -and $actual.byte_count -isnot [long]) -or
        $actual.byte_count -ne $CapacityInputs.config_bytes.Length -or
        (Get-CanonicalObjectJson $actual.source_sha256) -cne (Get-CanonicalObjectJson $CapacityInputs.source_sha256)) {
        throw "installed capacity source/config bytes differ from reviewed inputs"
    }
}

function Assert-IdleHealth {
    param(
        [Parameter(Mandatory = $true)][object]$Health,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$RequireAdmissionV2,
        [AllowNull()][object]$ExpectedCapacity = $null
    )
    Assert-RequiredProperties -Value $Health -Names @(
        "status", "queued_tasks", "processing_tasks"
    ) -Label $Label
    if (
        [string]$Health.status -ne "healthy" -or
        ($Health.queued_tasks -isnot [int] -and $Health.queued_tasks -isnot [long]) -or
        ($Health.processing_tasks -isnot [int] -and $Health.processing_tasks -isnot [long]) -or
        [int]$Health.queued_tasks -ne 0 -or
        [int]$Health.processing_tasks -ne 0
    ) {
        throw "$Label is not healthy and idle"
    }
    $runtime = $Health.task_protocol_runtime
    $isV3 = $null -ne $runtime -and $runtime.schema -eq "mineru-task-runtime.v3"
    if ($isV3) {
        Assert-CapacityIdleHealth -Health $Health -ExpectedCapacity $ExpectedCapacity -Label $Label
        return
    }
    if ($null -ne $ExpectedCapacity) { throw "$Label has no explicit capacity evidence" }
    $isV2 = $null -ne $runtime -and $runtime.schema -eq "mineru-task-runtime.v2"
    if ($RequireAdmissionV2 -and -not $isV2) {
        throw "$Label has no durable admission evidence"
    }
    if ($isV2) {
        $runtimeNames = @("schema", "enabled", "task_registry_max_records",
            "task_result_reservation_bytes", "max_unacked_result_bytes", "registry_schema", "admission_scope")
        if (@($runtime.PSObject.Properties.Name).Count -ne $runtimeNames.Count) {
            throw "$Label runtime evidence is not closed"
        }
        Assert-RequiredProperties -Value $runtime -Names $runtimeNames -Label $Label
        if ($runtime.enabled -isnot [bool] -or -not $runtime.enabled -or
            $runtime.registry_schema -ne "mineru-task-registry.v3" -or
            $runtime.admission_scope -ne "post_form_owned_upload") {
            throw "$Label runtime admission identity drifted"
        }
        $limits = @{ task_registry_max_records = 128; task_result_reservation_bytes = 268435456;
            max_unacked_result_bytes = 2147483648 }
        foreach ($key in $limits.Keys) {
            if (($runtime.$key -isnot [int] -and $runtime.$key -isnot [long]) -or
                $runtime.$key -ne $limits[$key]) { throw "$Label runtime limit drifted: $key" }
        }
        $admission = $Health.task_admission
        $zeroCounters = @("ingress_tasks", "accepted_pending_tasks", "accepted_processing_tasks",
            "accepted_finalizing_tasks", "durable_nonterminal_tasks", "routeless_accepted_tasks",
            "ingress_cleanup_tasks", "unowned_ingress_tasks", "scheduled_tasks", "queue_depth", "active_processors")
        $fields = @("schema", "registry_schema", "nonterminal_limit", "recovery_overcommitted",
            "admission_open", "blocked_reason") + $zeroCounters
        if ($null -eq $admission -or @($admission.PSObject.Properties.Name).Count -ne $fields.Count) {
            throw "$Label admission evidence is not closed"
        }
        foreach ($key in $fields) {
            if ($admission.PSObject.Properties.Name -notcontains $key) {
                throw "$Label admission evidence is missing $key"
            }
        }
        if ($admission.schema -ne "mineru-task-admission.v1" -or
            $admission.registry_schema -ne "mineru-task-registry.v3" -or
            ($admission.nonterminal_limit -isnot [int] -and $admission.nonterminal_limit -isnot [long]) -or
            $admission.nonterminal_limit -ne $Health.max_pending_tasks_effective -or
            $admission.nonterminal_limit -lt 1 -or $admission.nonterminal_limit -gt 128 -or
            $admission.recovery_overcommitted -isnot [bool] -or $admission.recovery_overcommitted -or
            $admission.admission_open -isnot [bool] -or -not $admission.admission_open -or
            $null -ne $admission.blocked_reason) {
            throw "$Label admission is not open and idle"
        }
        foreach ($key in $zeroCounters) {
            if (($admission.$key -isnot [int] -and $admission.$key -isnot [long]) -or $admission.$key -ne 0) {
                throw "$Label retains task responsibility: $key"
            }
        }
    } elseif ($null -ne $runtime -and $runtime.schema -ne "mineru-task-runtime.v1") {
        throw "$Label runtime version is unsupported"
    } elseif ($null -ne $runtime) {
        $legacyNames = @("schema", "enabled", "task_registry_max_records",
            "task_result_reservation_bytes", "max_unacked_result_bytes")
        if ($Health.PSObject.Properties.Name -contains "task_admission" -or
            @($runtime.PSObject.Properties.Name).Count -ne $legacyNames.Count) {
            throw "$Label mixes legacy runtime and admission evidence"
        }
        Assert-RequiredProperties -Value $runtime -Names $legacyNames -Label $Label
        if ($runtime.enabled -isnot [bool] -or -not $runtime.enabled) {
            throw "$Label legacy runtime is disabled"
        }
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
        if (
            $script:OldProjectContainers -notcontains "mineru-api-proxy" -or
            $script:OldRunningContainers -notcontains "mineru-api-proxy"
        ) {
            throw "existing MinerU topology is not the exact proxy-isolated topology"
        }
        $oldProxy = (Invoke-Docker -Arguments @(
            "inspect", "mineru-api-proxy"
        )) | ConvertFrom-Json
        Assert-NoPublishedPort -Container $oldApi -ContainerPort "8000/tcp"
        Assert-SinglePort -Container $oldProxy -ContainerPort "8000/tcp" -HostPort "30003"
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
    if ($ApiOnlyOperation) {
        $required = @("mineru-api", "mineru-api-proxy", "mineru-openai-server")
        if (
            (@($script:OldProjectContainers | Sort-Object) -join ",") -ne
                (@($required | Sort-Object) -join ",") -or
            (@($script:OldRunningContainers | Sort-Object) -join ",") -ne
                (@($required | Sort-Object) -join ",")
        ) {
            throw "API-only operation requires exactly three running MinerU containers"
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

function Get-QuiescentOutputState {
    param([switch]$CandidateSource)
    $before = Invoke-RestMethod -Uri "http://127.0.0.1:30003/health" -TimeoutSec 15
    Assert-IdleHealth -Health $before -Label "output inspection before"
    if ($CandidateSource) {
        # The old image may predate protocol-v2. Stream the exact reviewed build
        # source through stdin, without installing it or constructing a registry.
        $sourcePath = Join-Path (Split-Path -Parent $CompatPatcherSource) "agent_task_protocol_v2.py"
        $source = [IO.File]::ReadAllText($sourcePath)
        $source += "`nprint(json.dumps(inspect_quiescent_output_root(Path('/var/lib/mineru-api-output'), allow_empty=True), sort_keys=True))`n"
        $probeResult = Invoke-DockerProcess -Arguments @(
            "exec", "-i", "mineru-api", "/usr/bin/python3.12", "-I", "-"
        ) -StandardInput $source
        $output = @(ConvertFrom-NativeProcessText -Value $probeResult.StandardOutput)
    }
    else {
        $output = @(Invoke-Docker -Arguments @(
            "exec", "mineru-api", "/usr/bin/python3.12", "-I", "-c",
            "import json; from pathlib import Path; from mineru.cli.agent_task_protocol_v2 import inspect_quiescent_output_root; print(json.dumps(inspect_quiescent_output_root(Path('/var/lib/mineru-api-output')), sort_keys=True))"
        ))
    }
    if ($output.Count -ne 1) { throw "output inspection must return one witness" }
    $state = ([string]$output[0]) | ConvertFrom-Json
    if ([string]$state.quiescence.schema -ne "mineru-output-quiescence.v1") {
        throw "output quiescence schema drifted"
    }
    $after = Invoke-RestMethod -Uri "http://127.0.0.1:30003/health" -TimeoutSec 15
    Assert-IdleHealth -Health $after -Label "output inspection after"
    return $state
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
    if ($ApiDeviceProfile -ne "") { Assert-ApiDeviceRuntime -Container $api -Profile $ApiDeviceProfile }
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
    $httpLimit = if ($ExplicitCapacity) { [string]$CapacityInputs.config.final_http_limit_per_loop } else { "7" }
    Assert-ExactCommand -Container $api -Expected @(
        "mineru-api", "--host", "0.0.0.0", "--port", "8000",
        "--allow-public-http-client", "--max-concurrency", $httpLimit
    )
    if ($ExplicitCapacity) {
        foreach ($name in $CapacityInputs.environment.Keys) {
            $actual = @($api.Config.Env | Where-Object { $_ -clike "$name=*" })
            if ($actual.Count -ne 1 -or $actual[0] -cne "$name=$($CapacityInputs.environment[$name])") {
                throw "actual API capacity environment differs: $name"
            }
        }
        Assert-CapacityFiles -ContainerId ([string]$api.Id)
    }
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
        "task_cleanup_interval_seconds", "task_protocol_schema",
        "queued_tasks", "processing_tasks"
    ) -Label "new MinerU API health"
    $expectedCapacity = if ($ExplicitCapacity) { $CapacityInputs.config } else { $null }
    Assert-IdleHealth -Health $health -Label "new MinerU API health" -RequireAdmissionV2 -ExpectedCapacity $expectedCapacity
    if (-not $ExplicitCapacity -and (
        [string]$health.version -ne "3.4.4" -or
        [int]$health.protocol_version -ne 2 -or
        [string]$health.task_protocol_schema -ne "mineru-task-protocol.v2" -or
        [int]$health.max_concurrent_requests -ne $ExpectedApiTaskSlots -or
        [int]$health.max_pending_tasks_requested -ne $ExpectedApiMaxPendingTasks -or
        [int]$health.max_pending_tasks_effective -ne $ExpectedApiMaxPendingTasks -or
        $ExpectedApiMaxPendingTasks -lt $ExpectedApiTaskSlots -or
        [int]$health.processing_window_size -ne 16 -or
        [int]$health.task_retention_seconds -ne 600 -or
        [int]$health.task_cleanup_interval_seconds -ne 30
    )) {
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

    $outputState = Get-QuiescentOutputState

    return [ordered]@{
        api_health = $health
        model_id = $modelId
        model_revision = $modelRevision
        model_max_len = [int]$models.data[0].max_model_len
        vllm_version = ([string]$vllmVersion[0]).Trim()
        api_networks = $apiNetworks
        proxy_networks = $proxyNetworks
        inference_networks = $inferenceNetworks
        output_file_count = [int]$outputState.file_count
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
    $result = [ordered]@{
        dockerfile = $dockerfile
        patcher = $patcher
        context = $context
        patcher_sha256 = $patcherSha256
        dockerfile_sha256 = $dockerfileSha256
        task_protocol_v2_sha256 = $taskProtocolSha256
        build_target = "legacy-runtime"
    }
    if ($ExplicitCapacity) {
        $result["capacity"] = Get-ExplicitCapacityInputs
        $result["build_target"] = "explicit-capacity"
        if ($null -ne $CapacityInputs -and
            (Get-CanonicalObjectJson $result.capacity.source_sha256) -cne (Get-CanonicalObjectJson $CapacityInputs.source_sha256)) {
            throw "capacity source bytes changed after preflight"
        }
    }
    return $result
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
        @($image.Config.Env | Where-Object { $_ -like "MINERU_TASK_PROTOCOL_V2=*" }).Count -ne 0
    ) {
        throw "MinerU API compatibility image labels or environment drifted"
    }
    if ($ExplicitCapacity) {
        foreach ($item in @(
            @("capacity-config-sha256", $BuildIdentity.capacity.config_sha256),
            @("capacity-sources-sha256", $BuildIdentity.capacity.sources_sha256)
        )) {
            $name = "io.agent-invest.mineru.$($item[0])"
            if ([string]$image.Config.Labels.$name -cne [string]$item[1]) { throw "capacity image label differs: $name" }
        }
        foreach ($item in @(
            @("MINERU_CAPACITY_CONFIG_PATH", "/usr/local/etc/mineru/capacity.json"),
            @("MINERU_CAPACITY_CONFIG_SHA256", $BuildIdentity.capacity.config_sha256)
        )) {
            $values = @($image.Config.Env | Where-Object { $_ -clike "$($item[0])=*" })
            if ($values.Count -ne 1 -or $values[0] -cne "$($item[0])=$($item[1])") { throw "capacity image anchor differs" }
        }
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
    $arguments = @(
        "build", "--pull=false", "--provenance=false", "--target", [string]$identity.build_target, "--file",
        [string]$identity.dockerfile,
        "--tag", $ApiCompatBuildTag,
        "--build-arg", "COMPAT_PATCHER_SHA256=$($identity.patcher_sha256)",
        "--build-arg", "COMPAT_DOCKERFILE_SHA256=$($identity.dockerfile_sha256)",
        "--build-arg", "TASK_PROTOCOL_V2_SHA256=$($identity.task_protocol_v2_sha256)"
    )
    if ($ExplicitCapacity) {
        foreach ($name in @("config", "file", "bootstrap", "observation")) {
            $pin = $identity.capacity.source_sha256["mineru/cli/agent_capacity_$name.py"]
            $arguments += @("--build-arg", "CAPACITY_$($name.ToUpperInvariant())_SOURCE_SHA256=$pin")
        }
        $arguments += @("--build-arg", "CAPACITY_SOURCES_SHA256=$($identity.capacity.sources_sha256)",
            "--build-arg", "CAPACITY_CONFIG_SHA256=$($identity.capacity.config_sha256)")
    }
    $arguments += [string]$identity.context
    # Image builds are the only long, chatty native command: head/tail retention
    # with dropped counts, and the remaining operation budget as the deadline.
    Invoke-Docker -Arguments $arguments -TimeoutMilliseconds 3600000 -MaximumOutputBytes 8388608 -HeadTail | Out-Null
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

function Get-RollbackRegistryWitness {
    param([AllowNull()][object]$State)
    # Both observations come from the same pinned, read-only inspector. Keep a
    # closed, typed canonical witness so missing values cannot compare as zero.
    $proof = $State.quiescence
    $root = $proof.root_identity
    foreach ($shape in @(
        @($State, "file_count,quiescence,total_bytes"),
        @($proof, "record_count,registry_sha256,root_identity,schema,submission_watermark_bucket"),
        @($root, "device,inode,mode,path,uid")
    )) {
        if ($null -eq $shape[0] -or $shape[0] -isnot [pscustomobject]) {
            throw "output witness must contain typed objects"
        }
        $keys = @($shape[0].PSObject.Properties.Name | Sort-Object) -join ","
        if ($keys -cne $shape[1]) { throw "output witness fields drifted" }
    }
    if ($proof.schema -isnot [string] -or $root.path -isnot [string] -or
        $proof.schema -cne "mineru-output-quiescence.v1" -or
        $root.path -cne "/var/lib/mineru-api-output") {
        throw "output witness schema or root path drifted"
    }
    foreach ($value in @(
        $State.file_count, $State.total_bytes, $proof.record_count,
        $root.device, $root.inode, $root.uid, $root.mode
    )) {
        if (($value -isnot [int] -and $value -isnot [long]) -or $value -lt 0) {
            throw "output witness counters and identity must be nonnegative integers"
        }
    }
    if ($null -eq $proof.registry_sha256) {
        if ($State.file_count -ne 0 -or $State.total_bytes -ne 0 -or
            $proof.record_count -ne 0 -or $null -ne $proof.submission_watermark_bucket) {
            throw "absent registry requires an empty physical witness"
        }
    }
    else {
        $watermark = $proof.submission_watermark_bucket
        if ($proof.registry_sha256 -isnot [string] -or
            $proof.registry_sha256 -cnotmatch '\Asha256:[a-f0-9]{64}\z' -or
            $State.file_count -ne 1 -or $State.total_bytes -le 0 -or
            ($watermark -isnot [int] -and $watermark -isnot [long]) -or $watermark -lt -1) {
            throw "present registry witness is invalid"
        }
    }
    return ([ordered]@{
        file_count = $State.file_count; total_bytes = $State.total_bytes
        schema = $proof.schema
        path = $root.path; device = $root.device; inode = $root.inode
        uid = $root.uid; mode = $root.mode
        registry_sha256 = $proof.registry_sha256
        record_count = $proof.record_count
        submission_watermark_bucket = $proof.submission_watermark_bucket
    } | ConvertTo-Json -Depth 4 -Compress)
}

function Assert-RollbackRegistryUnchanged {
    # Operator writer exclusion must span preflight through this decision,
    # including requests already in Form parsing. Health is not a writer lock.
    try {
        $before = Get-RollbackRegistryWitness -State $PreDeploymentOutputState
        $current = Get-QuiescentOutputState -CandidateSource
        $after = Get-RollbackRegistryWitness -State $current
    }
    catch {
        throw "rollback_blocked_registry_unverified: $($_.Exception.Message)"
    }
    if (-not [string]::Equals($before, $after, [StringComparison]::Ordinal)) {
        throw "rollback_blocked_registry_changed: retained responsibilities or root identity changed"
    }
}

function Restore-PreviousDeployment {
    if ($DeploymentAttempted -and ($OldProjectContainers -contains "mineru-api")) {
        Assert-RollbackRegistryUnchanged
    }
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
        ) -TimeoutMilliseconds 900000 | Out-Null
        Get-ValidatedRuntime | Out-Null
        Assert-StableServiceEpochs -Expected $StableServiceEpochs
        if ((Get-OptionalImageId -Reference $ApiCompatImage) -ne $CampaignApiCompatImageId) {
            throw "campaign image tag drifted during API-only rollback"
        }
        Remove-CompatBuildTag
        return
    }

    Restore-ApiCompatTag
    if ($ApiOnlyCompatibilityUpgrade) {
        Invoke-ApiOnlyRecreate
        Wait-Healthy | Out-Null
        Assert-StableServiceEpochs -Expected $StableServiceEpochs
        $restored = (Invoke-Docker -Arguments @("inspect", "mineru-api")) | ConvertFrom-Json
        if (@($restored).Count -ne 1 -or [string]$restored[0].Image -ne $OldApiCompatImageId) {
            throw "API-only rollback did not restore the previous API image"
        }
        if ($ApiDeviceProfile -ne "") {
            Assert-ApiDeviceRuntime -Container $restored[0] -Profile $PreviousApiDeviceProfile
        }
        Remove-CompatBuildTag
        return
    }

    if ($ComposeExisted) {
        if ($OldProjectContainers.Count -eq 0) {
            Invoke-Docker -Arguments @(
                "compose", "--project-name", $ProjectName, "--file", $ComposeTarget,
                "down", "--remove-orphans"
            ) -TimeoutMilliseconds 900000 | Out-Null
        }
        else {
            Invoke-Docker -Arguments @(
                "compose", "--project-name", $ProjectName, "--file", $ComposeTarget,
                "up", "--detach", "--remove-orphans"
            ) -TimeoutMilliseconds 900000 | Out-Null
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
        ) -TimeoutMilliseconds 900000 | Out-Null
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
    $CapacityInputs = Get-ExplicitCapacityInputs
    if ($ExplicitCapacity) { Assert-CapacityCompose (Get-ResolvedCompose $ComposeSource) }
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
    ) -TimeoutMilliseconds 900000 | Out-Null
    $imageInspect = (Invoke-Docker -Arguments @("image", "inspect", $ExpectedRepoDigest)) |
        ConvertFrom-Json
    if (
        @($imageInspect).Count -ne 1 -or
        [string]$imageInspect[0].Id -ne $ExpectedImageId -or
        @($imageInspect[0].RepoDigests) -notcontains $ExpectedRepoDigest
    ) {
        throw "local Docker image does not match the expected repo digest and image ID"
    }

    if ($ApiOnlyCompatibilityUpgrade) { Assert-ApiOnlyUpgradeInputs }
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
    if ($OldProjectContainers -contains "mineru-api") {
        $PreDeploymentOutputState = Get-QuiescentOutputState -CandidateSource
        Get-RollbackRegistryWitness -State $PreDeploymentOutputState | Out-Null
    }
    elseif (@(Get-ChildItem -LiteralPath $OutputRoot -Force).Count -ne 0) {
        throw "first installation requires a genuinely empty output root"
    }
    if ($ReuseCurrentPublishedImage) {
        $compatImage = Get-ValidatedPublishedApiCompatImage
        Get-ValidatedRuntime | Out-Null
        Assert-StableServiceEpochs -Expected $StableServiceEpochs
    }
    else {
        $OldApiCompatImageId = Get-OptionalImageId -Reference $ApiCompatImage
        if ($ApiOnlyCompatibilityUpgrade) {
            $oldApi = (Invoke-Docker -Arguments @("inspect", "mineru-api")) | ConvertFrom-Json
            if (@($oldApi).Count -ne 1 -or [string]$oldApi[0].Image -ne $OldApiCompatImageId) {
                throw "API-only upgrade requires published tag to match the current API image"
            }
        }
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

    if ($ApiOnlyCompatibilityUpgrade) {
        Assert-ApiOnlyUpgradeInputs
        Assert-StableServiceEpochs -Expected $StableServiceEpochs
        if ((Get-OptionalImageId -Reference $ApiCompatImage) -ne $OldApiCompatImageId) {
            throw "API image tag drifted before compatibility upgrade"
        }
    }
    Write-OperationRecord "installer-phase-preflight-complete.json" ([ordered]@{ phase = "preflight_complete" })
    $MutationStarted = $true
    Write-OperationRecord "installer-phase-mutation-started.json" ([ordered]@{ phase = "mutation_started" })
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
    Write-OperationRecord "installer-phase-deployment-attempted.json" ([ordered]@{ phase = "deployment_attempted" })
    if ($ApiOnlyOperation) {
        Invoke-ApiOnlyRecreate
    }
    else {
        Invoke-Docker -Arguments @(
            "compose", "--project-name", $ProjectName, "--file", $ComposeTarget,
            "up", "--detach", "--remove-orphans"
        ) -TimeoutMilliseconds 900000 | Out-Null
    }
    $runtime = Get-ValidatedRuntime
    if ($ApiOnlyOperation) { Assert-StableServiceEpochs -Expected $StableServiceEpochs }
    if ($ReuseCurrentPublishedImage) {
        Assert-StableServiceEpochs -Expected $StableServiceEpochs
        if ((Get-OptionalImageId -Reference $ApiCompatImage) -ne $CampaignApiCompatImageId) {
            throw "campaign image tag drifted during API-only deployment"
        }
    }
    $collectorArguments = @{ ComposePath=$ComposeTarget; OutputRoot=$OutputRoot }
    if ($ExplicitCapacity) { $collectorArguments["ExpectedCapacityConfigSha256"] = $ExpectedCapacityConfigSha256 }
    $collectorOutput = @(& $CollectorTarget @collectorArguments)
    if ($collectorOutput.Count -ne 1) {
        throw "formal runtime collector did not return one observation"
    }
    $collectorObservation = ([string]$collectorOutput[0]) | ConvertFrom-Json
    $expectedCollectorSchema = if ($ExplicitCapacity) { "mineru-windows-runtime-observation.v6" } else { "mineru-windows-runtime-observation.v5" }
    if ($collectorObservation.schema -isnot [string] -or $collectorObservation.schema -cne $expectedCollectorSchema) {
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
    Write-OperationRecord "installer-result.json" ([ordered]@{
        phase = "complete"; status = "pass"; first_error = $null
        compose_sha256 = $receipt.compose_sha256; collector_sha256 = $receipt.collector_sha256
        api_image_id = $compatImage.image_id; receipt_sha256 = (Get-Sha256Text $receiptJson)
    })
    $receiptJson
}
catch {
    $originalError = $_.Exception.Message
    # A deadline during a daemon-side request leaves the daemon outcome unknown:
    # record it, keep writes closed and never roll back over an unknown state.
    $outcomeUnknown = $originalError -like "native_outcome_unknown:*"
    Write-OperationRecord "installer-first-error.json" ([ordered]@{
        phase = "failed"; first_error = $originalError; mutation_started = [bool]$MutationStarted
        deployment_attempted = [bool]$DeploymentAttempted; daemon_side_outcome = $(if ($outcomeUnknown) { "unknown" } else { "cli_observed" })
    })
    if ($MutationStarted -and $outcomeUnknown) {
        Write-OperationRecord "installer-result.json" ([ordered]@{
            phase = "unknown"; status = "unknown"; first_error = $originalError; rollback = "not_attempted"
        })
        throw "installation outcome unknown; rollback not attempted because the daemon-side result is undetermined: $originalError"
    }
    if (-not $MutationStarted) {
        $cleanupError = $null
        try { Remove-CompatBuildTag }
        catch { $cleanupError = $_.Exception.Message }
        Write-OperationRecord "installer-result.json" ([ordered]@{
            phase = "preflight_failed"; status = "failed"; first_error = $originalError; rollback = "not_needed"
            cleanup_error = $cleanupError
        })
        if ($null -ne $cleanupError) {
            throw "installation preflight failed: $originalError; temporary image cleanup also failed: $cleanupError"
        }
        throw "installation preflight failed before runtime mutation: $originalError"
    }
    Write-OperationRecord "installer-phase-rollback-started.json" ([ordered]@{ phase = "rollback_started" })
    $rollbackError = $null
    try { Restore-PreviousDeployment }
    catch { $rollbackError = $_.Exception.Message }
    Write-OperationRecord "installer-result.json" ([ordered]@{
        phase = "rolled_back"; status = "failed"; first_error = $originalError
        rollback = $(if ($null -eq $rollbackError) { "restored" } else { "failed" }); rollback_error = $rollbackError
    })
    if ($null -ne $rollbackError) {
        throw "installation failed: $originalError; rollback also failed: $rollbackError"
    }
    throw "installation failed and previous deployment was restored: $originalError"
}
