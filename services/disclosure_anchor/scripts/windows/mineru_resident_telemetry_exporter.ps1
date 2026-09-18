param(
    [Parameter(Mandatory = $true)][string]$ConfigJsonPath,
    [Parameter(Mandatory = $true)][string]$ExpectedConfigSha256
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$pins = [Collections.Generic.List[IO.FileStream]]::new()
$failures = [Collections.Generic.List[Exception]]::new()
$failureContexts = [Collections.Generic.List[string]]::new()
function Add-MineruFailure($ErrorRecord) {
    $failures.Add($ErrorRecord.Exception)
    $failureContexts.Add([string]$ErrorRecord.ScriptStackTrace)
}
function Write-MineruFailureDetail([string]$Role,[string]$ArtifactName) {
    # PowerShell prints only the outer AggregateException message, so the full
    # nested chain (Exception.ToString includes every inner exception) is echoed
    # to stderr and persisted next to the session artifacts before the rethrow.
    # Guarded end to end: a failure here is reported and never replaces the
    # original terminal throw.
    try {
        $lines = [Collections.Generic.List[string]]::new()
        $lines.Add($Role + ' failure detail: ' + $failures.Count + ' failure(s)')
        for ($index = 0; $index -lt $failures.Count; $index++) {
            $lines.Add('[' + $index + '] ' + $failures[$index].ToString())
            if ($index -lt $failureContexts.Count -and $failureContexts[$index].Length -gt 0) { $lines.Add('    script: ' + $failureContexts[$index]) }
        }
        $text = $lines -join "`n"
        [Console]::Error.WriteLine($text)
        $directory = Get-Variable -Name runDirectory -ValueOnly -ErrorAction SilentlyContinue
        if ($null -ne $directory -and [IO.Directory]::Exists([string]$directory)) {
            $bytes = [Text.UTF8Encoding]::new($false).GetBytes($text)
            $output = [IO.FileStream]::new([IO.Path]::Combine([string]$directory,$ArtifactName),[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
            try { $output.Write($bytes,0,$bytes.Length); $output.Flush($true) } finally { $output.Dispose() }
        }
    } catch { try { [Console]::Error.WriteLine($Role + ' failure detail unavailable: ' + $_.Exception.Message) } catch { } }
}
function Get-MineruBootstrapSha([byte[]]$Bytes) {
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return 'sha256:' + ([BitConverter]::ToString($hash.ComputeHash($Bytes))).Replace('-','').ToLowerInvariant() }
    finally { $hash.Dispose() }
}
function Read-MineruBootstrap([string]$Path,[string]$ExpectedSha,[int]$Maximum) {
    if ($ExpectedSha -cnotmatch '\Asha256:[0-9a-f]{64}\z' -or -not [IO.Path]::IsPathRooted($Path)) { throw 'absolute bootstrap path and canonical SHA required' }
    $pin = [IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $pins.Add($pin)
    if ($pin.Length -lt 1 -or $pin.Length -gt $Maximum) { throw 'bootstrap file byte bound exceeded' }
    $bytes = [byte[]]::new([int]$pin.Length); $offset = 0
    while ($offset -lt $bytes.Length) {
        $count = $pin.Read($bytes,$offset,$bytes.Length-$offset)
        if ($count -le 0) { throw 'bootstrap truncated' }
        $offset += $count
    }
    if ((Get-MineruBootstrapSha $bytes) -cne $ExpectedSha) { throw 'bootstrap SHA mismatch' }
    return ,$bytes
}

$endpoint = $null; $gpu = $null; $linux = $null; $apiHttp = $null; $vllmHttp = $null
$script:linuxSequence = [long]0
$script:linuxLastFinished = [long]0
$script:linuxLastUser = [long]0
$script:linuxLastSystem = [long]0
function Assert-MineruLinuxCpu($Cpu) {
    $Cpu.Keys([string[]]@('user_ns_total','system_ns_total'))
    $user = $Cpu.Get('user_ns_total').Integer(); $system = $Cpu.Get('system_ns_total').Integer()
    if ($user -lt $script:linuxLastUser -or $system -lt $script:linuxLastSystem) { throw 'Linux sample CPU rollback' }
    $script:linuxLastUser = $user; $script:linuxLastSystem = $system
}
function Read-MineruLinuxResponse([string]$Kind,[long]$Deadline) {
    $script:linuxSequence += 1
    $command = New-MineruJson @('command',(Quote-MineruJson $Kind),'sequence',[string]$script:linuxSequence)
    $linux.Write($command,$Deadline)
    $frame = [MineruResidentWire]::Parse($linux.Read($Deadline),65536)
    $frame.Keys([string[]]@('contract_version','kind','epoch_sha256','sequence','started_monotonic_ns','finished_monotonic_ns','cpu','values'))
    if ((Get-MineruString $frame 'contract_version') -cne 'mineru.linux-resident-host.v1' -or (Get-MineruString $frame 'kind') -cne $Kind -or
        (Get-MineruString $frame 'epoch_sha256') -cne (Get-MineruString $samplerReady 'epoch_sha256') -or $frame.Get('sequence').Integer() -ne $script:linuxSequence) { throw 'Linux response identity or sequence drift' }
    $started = $frame.Get('started_monotonic_ns').Integer(); $finished = $frame.Get('finished_monotonic_ns').Integer()
    if ($started -lt $script:linuxLastFinished -or $finished -lt $started) { throw 'Linux response monotonic rollback' }
    Assert-MineruLinuxCpu $frame.Get('cpu')
    $script:linuxLastFinished = $finished
    if ($Kind -ceq 'close') {
        if ($frame.Get('values').Raw -cne 'null') { throw 'Linux close carried sample values' }
    } else { $frame.Get('values').Keys([string[]]@('api_process','host_cgroup')) }
    return $frame
}
# Bounded diagnostic phase ring. It only records where each sampling call spent
# the deadline that call already computed: no extra HTTP request, thread, timer
# or deadline, and no per-call file write. The last 16 successful records plus
# the in-progress failing record, if any, are serialized once from the outer
# finally at close or failure; a probe failure is secondary evidence and never
# replaces the original terminal throw. The marked region depends only on the
# loaded wire assembly (New-MineruJson/Quote-MineruJson) so an independent
# mechanism test can extract and drive it without a session.
# BEGIN MINERU SAMPLING PHASE RING V1
$script:phaseRingCapacity = 16
$script:phaseRing = [Collections.Generic.List[object]]::new()
$script:phaseFailing = $null
$script:phaseCallsTotal = [long]0
function New-MineruPhaseCall([long]$Deadline,[long]$Boundary,[string[]]$Names) {
    $script:phaseCallsTotal += 1
    $phases = [Collections.Generic.List[object]]::new()
    foreach ($name in $Names) {
        $phases.Add([pscustomobject]@{ Phase=$name; Entered=[long]0; Finished=[long]0; Remaining=[long]0; HasEntered=$false; HasFinished=$false; Outcome='not_entered'; ExceptionType=$null })
    }
    return [pscustomobject]@{ Ordinal=$script:phaseCallsTotal; Deadline=$Deadline; Boundary=$Boundary; Phases=$phases }
}
function Enter-MineruPhase($Call,[int]$Index) {
    # The remaining budget is computed here and never through
    # [MineruResidentWire]::Remaining, which throws once the deadline is gone
    # and would replace the operation's own exception.
    $record = $Call.Phases[$Index]
    $now = [Diagnostics.Stopwatch]::GetTimestamp()
    $record.Entered = $now
    $record.Remaining = [Math]::Max([long]0,$Call.Deadline - $now)
    $record.HasEntered = $true
}
function Exit-MineruPhase($Call,[int]$Index) {
    $record = $Call.Phases[$Index]
    $record.Finished = [Diagnostics.Stopwatch]::GetTimestamp()
    $record.HasFinished = $true
    $record.Outcome = 'ok'
}
function Trace-MineruPhaseFailure($Call,[int]$Index,$ErrorRecord) {
    $record = $Call.Phases[$Index]
    $record.Outcome = 'failed'
    if ($null -ne $ErrorRecord -and $null -ne $ErrorRecord.Exception) {
        # A .NET method throw reaches PowerShell wrapped in MethodInvocationException;
        # the operative type is the inner one. The full chain stays in exporter-failure.txt.
        $thrown = $ErrorRecord.Exception
        if ($thrown -is [Management.Automation.MethodInvocationException] -and $null -ne $thrown.InnerException) { $thrown = $thrown.InnerException }
        $record.ExceptionType = $thrown.GetType().FullName
    }
    $script:phaseFailing = $Call
}
function Complete-MineruPhaseCall($Call) {
    $script:phaseRing.Add($Call)
    if ($script:phaseRing.Count -gt $script:phaseRingCapacity) { $script:phaseRing.RemoveAt(0) }
}
function Invoke-MineruPhase($Call,[int]$Index,[scriptblock]$Action) {
    # One phase: enter, run the original operation unchanged, exit; on failure the
    # record is marked and the ORIGINAL error is rethrown untouched.
    Enter-MineruPhase $Call $Index
    try { $result = & $Action } catch { Trace-MineruPhaseFailure $Call $Index $_; throw }
    Exit-MineruPhase $Call $Index
    return $result
}
function ConvertTo-MineruPhaseJson($Record) {
    $entered = 'null'; $remaining = 'null'; $finished = 'null'; $exceptionType = 'null'
    if ($Record.HasEntered) { $entered = [string]$Record.Entered; $remaining = [string]$Record.Remaining }
    if ($Record.HasFinished) { $finished = [string]$Record.Finished }
    if ($null -ne $Record.ExceptionType) { $exceptionType = Quote-MineruJson ([string]$Record.ExceptionType) }
    return New-MineruJson @('phase',(Quote-MineruJson $Record.Phase),'entered_ticks',$entered,'finished_ticks',$finished,'remaining_at_enter_ticks',$remaining,'outcome',(Quote-MineruJson $Record.Outcome),'exception_type',$exceptionType)
}
function ConvertTo-MineruPhaseCallJson($Call) {
    $items = [Collections.Generic.List[string]]::new()
    $exhausted = 'false'; $budget = 'false'
    foreach ($record in $Call.Phases) {
        $items.Add((ConvertTo-MineruPhaseJson $record))
        if ($record.Outcome -ceq 'failed' -and $record.HasEntered) {
            if ($record.Remaining -gt 0) { $budget = 'true' } else { $exhausted = 'true' }
        }
    }
    return New-MineruJson @('ordinal',[string]$Call.Ordinal,'deadline_ticks',[string]$Call.Deadline,'qpc_frequency',[string][Diagnostics.Stopwatch]::Frequency,'boundary_ticks',[string]$Call.Boundary,'deadline_exhausted_before_enter',$exhausted,'operation_failed_with_budget_remaining',$budget,'phases',('[' + ($items -join ',') + ']'))
}
function Get-MineruPhaseTailJson([string]$Session,[string]$Lane) {
    # The v1 document text from the ring alone; no file is touched here.
    $records = [Collections.Generic.List[string]]::new()
    foreach ($call in $script:phaseRing) { $records.Add((ConvertTo-MineruPhaseCallJson $call)) }
    $failing = 'null'
    if ($null -ne $script:phaseFailing) { $failing = ConvertTo-MineruPhaseCallJson $script:phaseFailing }
    return New-MineruJson @('contract_version','"mineru.sampling-phase-tail.v1"','session',(Quote-MineruJson $Session),'lane',(Quote-MineruJson $Lane),'qpc_frequency',[string][Diagnostics.Stopwatch]::Frequency,'ring_capacity',[string]$script:phaseRingCapacity,'calls_total',[string]$script:phaseCallsTotal,'successful_records_retained',[string]$script:phaseRing.Count,'failing_record',$failing,'records',('[' + ($records -join ',') + ']'))
}
# END MINERU SAMPLING PHASE RING V1
function Write-MineruPhaseTail {
    # One write per process, once the owner-created run directory exists. A
    # failure here is recorded as a secondary failure, exactly like the
    # cleanup failures around it.
    try {
        $directory = Get-Variable -Name runDirectory -ValueOnly -ErrorAction SilentlyContinue
        if ($null -eq $directory) { return }
        Write-MineruSessionArtifact 'sampling-phase-tail.json' (Get-MineruPhaseTailJson $state.Session $state.Lane)
    } catch { $failures.Add($_.Exception) }
}
try {
    # Owner must validate canonical config/preparation and current runtime first.
    $configBytes = Read-MineruBootstrap $ConfigJsonPath $ExpectedConfigSha256 32768
    $bootstrapConfig = [Text.UTF8Encoding]::new($false,$true).GetString($configBytes) | ConvertFrom-Json
    $commonPath = [IO.Path]::Combine($PSScriptRoot,'load_mineru_resident_session.ps1')
    $null = Read-MineruBootstrap $commonPath $bootstrapConfig.sources.'load_mineru_resident_session.ps1' 65536
    . $commonPath
    $state = Initialize-MineruResidentSession
    $runDirectory = $state.RunDirectory
    $backend = $state.Config.Get('backend')
    $backendReady = 'null'
    if ($state.Lane -ceq 'gpu_fast') {
        $backend.Keys([string[]]@('nvml_dll_sha256','gpu_uuid'))
        $gpu = [MineruNvmlBackend]::new((Get-MineruString $backend 'nvml_dll_sha256'),(Get-MineruString $backend 'gpu_uuid'))
        $backendReady = New-MineruJson @('nvml_dll_sha256',(Quote-MineruJson $gpu.DllSha256),'device_identity_sha256',(Quote-MineruJson $gpu.DeviceIdentitySha256))
    } else {
        $backend.Keys([string[]]@('docker_path','docker_sha256','image_id','linux_config','api_port','vllm_port','api_namespace_pid','model_name','capacity_config_sha256'))
        $linuxConfig = $backend.Get('linux_config')
        $linuxConfig.Keys([string[]]@('boot_id','members','parent_device','parent_inode','lease_ms','lifetime_ms'))
        if ($linuxConfig.Get('lease_ms').Integer() -ne $state.Lease -or $linuxConfig.Get('lifetime_ms').Integer() -ne $state.Lifetime) { throw 'Linux and Windows finite lease/lifetime mismatch' }
        $sourceConfig = $state.Config.Get('sources')
        $supervisorSource = [MineruResidentWire]::Utf8.GetString((Read-MineruSessionBytes ([IO.Path]::Combine($state.SourceDirectory,'linux_resident_host_supervisor.py')) (Get-MineruString $sourceConfig 'linux_resident_host_supervisor.py') 65536))
        $samplerSource = [MineruResidentWire]::Utf8.GetString((Read-MineruSessionBytes ([IO.Path]::Combine($state.SourceDirectory,'linux_resident_host_sampler.py')) (Get-MineruString $sourceConfig 'linux_resident_host_sampler.py') 65536))
        $containerName = 'm6-resident-' + $state.Session
        $startupDeadline = [MineruResidentWire]::Deadline([int]$state.Lease)
        $linux = [MineruLinuxStdio]::new((Get-MineruString $backend 'docker_path'),(Get-MineruString $backend 'docker_sha256'),$containerName,(Get-MineruString $backend 'image_id'),$supervisorSource,$samplerSource,$linuxConfig.Raw)
        $linuxReady = [MineruResidentWire]::Parse($linux.Read($startupDeadline),65536)
        $linuxReady.Keys([string[]]@('contract_version','kind','identity','epoch_sha256','sampler_ready'))
        if ((Get-MineruString $linuxReady 'contract_version') -cne 'mineru.linux-resident-supervisor.v1' -or (Get-MineruString $linuxReady 'kind') -cne 'ready') { throw 'Linux supervisor READY version/kind' }
        $supervisorIdentity = $linuxReady.Get('identity')
        $supervisorIdentity.Keys([string[]]@('pid','start_ticks','source_sha256','sampler_source_sha256','boot_id','namespaces'))
        if ((Get-MineruString $supervisorIdentity 'source_sha256') -cne (Get-MineruString $sourceConfig 'linux_resident_host_supervisor.py') -or
            (Get-MineruString $supervisorIdentity 'sampler_source_sha256') -cne (Get-MineruString $sourceConfig 'linux_resident_host_sampler.py') -or
            (Get-MineruString $supervisorIdentity 'boot_id') -cne (Get-MineruString $linuxConfig 'boot_id') -or
            (Get-MineruString $linuxReady 'epoch_sha256') -cne [MineruResidentWire]::Hash([MineruResidentWire]::Utf8.GetBytes($supervisorIdentity.Raw))) { throw 'Linux supervisor READY source/epoch mismatch' }
        $null = Get-MineruInteger $supervisorIdentity 'pid' 1 ([long]::MaxValue)
        $null = Get-MineruInteger $supervisorIdentity 'start_ticks' 1 ([long]::MaxValue)
        $samplerReady = $linuxReady.Get('sampler_ready')
        $samplerReady.Keys([string[]]@('contract_version','kind','identity','epoch_sha256','cpu','monotonic_ns'))
        if ((Get-MineruString $samplerReady 'contract_version') -cne 'mineru.linux-resident-host.v1' -or (Get-MineruString $samplerReady 'kind') -cne 'ready') { throw 'Linux sampler READY version/kind' }
        $samplerIdentity = $samplerReady.Get('identity')
        $samplerIdentity.Keys([string[]]@('boot_id','members','parent_path','parent_device','parent_inode','source_sha256','pid','start_ticks','namespaces'))
        foreach ($name in @('boot_id','members','parent_device','parent_inode')) {
            if ($samplerIdentity.Get($name).Raw -cne $linuxConfig.Get($name).Raw) { throw ('Linux sampler READY config mismatch: ' + $name) }
        }
        if ((Get-MineruString $samplerIdentity 'source_sha256') -cne (Get-MineruString $sourceConfig 'linux_resident_host_sampler.py') -or
            (Get-MineruString $samplerReady 'epoch_sha256') -cne [MineruResidentWire]::Hash([MineruResidentWire]::Utf8.GetBytes($samplerIdentity.Raw)) -or
            $samplerIdentity.Get('pid').Integer() -eq $supervisorIdentity.Get('pid').Integer() -or
            $samplerIdentity.Get('namespaces').Raw -cne $supervisorIdentity.Get('namespaces').Raw) { throw 'Linux sampler READY source/epoch/process mismatch' }
        $null = Get-MineruInteger $samplerIdentity 'pid' 1 ([long]::MaxValue)
        $null = Get-MineruInteger $samplerIdentity 'start_ticks' 1 ([long]::MaxValue)
        $supervisorIdentity.Get('namespaces').Keys([string[]]@('pid','cgroup'))
        foreach ($name in @('pid','cgroup')) {
            if ((Get-MineruString $supervisorIdentity.Get('namespaces') $name) -cnotmatch ('\A'+$name+':\[[0-9]+\]\z')) { throw 'Linux namespace identity malformed' }
        }
        Assert-MineruLinuxCpu $samplerReady.Get('cpu')
        $script:linuxLastFinished = $samplerReady.Get('monotonic_ns').Integer()
        $apiHttp = [MineruBoundedHttp]::new((Get-MineruInteger $backend 'api_port' 1024 65535),$state.Loaded.Manifest.http_assembly_sha256)
        $vllmHttp = [MineruBoundedHttp]::new((Get-MineruInteger $backend 'vllm_port' 1024 65535),$state.Loaded.Manifest.http_assembly_sha256)
        $queue = [MineruQueueTelemetry]::new((Get-MineruInteger $backend 'api_namespace_pid' 1 ([long]::MaxValue)),(Get-MineruString $backend 'model_name'),(Get-MineruString $backend 'capacity_config_sha256'))
        $backendReady = New-MineruJson @('container_name',(Quote-MineruJson $containerName),'docker_pid',[string]$linux.Pid,'docker_creation_filetime_100ns',[string]$linux.CreationFiletime100ns,'docker_sha256',(Quote-MineruJson $linux.ExecutableSha256),'linux_ready',$linuxReady.Raw)
    }
    $readyJson = New-MineruJson @('contract_version','"mineru.windows-resident-ready.v1"','session',(Quote-MineruJson $state.Session),'lane',(Quote-MineruJson $state.Lane),'port',[string]$state.Port,'cadence_ms',[string]$state.Cadence,'config_sha256',(Quote-MineruJson $ExpectedConfigSha256),'identity',$state.Identity,'process',$state.Epoch,'clock',$state.Clock,'backend',$backendReady)
    $endpoint = [MineruResidentEndpoint]::new($state.Port,$state.Session,$state.Lane,$state.Cadence,$state.Lease,$state.Lifetime,$state.ResponseTimeout,$state.Identity)
    $readyAction = [Action]{ Write-MineruSessionArtifact 'ready.json' $readyJson }
    $sampleAction = [Func[long,string]]{
        param([long]$Boundary)
        $deadline = [Math]::Min($Boundary,[MineruResidentWire]::Deadline([int]$state.SamplingTimeout))
        if ($state.Lane -ceq 'gpu_fast') {
            $call = New-MineruPhaseCall $deadline $Boundary @('gpu_read')
            $value = Invoke-MineruPhase $call 0 { New-MineruJson @('gpu',$gpu.ReadJson()) }
        } else {
            $call = New-MineruPhaseCall $deadline $Boundary @('linux_sample','api_health','api_http','vllm_metrics','queue_projection')
            $frame = Invoke-MineruPhase $call 0 { Read-MineruLinuxResponse 'sample' $deadline }
            $health = Invoke-MineruPhase $call 1 { $apiHttp.Get('/health',8192,$deadline) }
            $http = Invoke-MineruPhase $call 2 { $apiHttp.Get('/agent/telemetry/http-requests/v1',1024,$deadline) }
            $metrics = Invoke-MineruPhase $call 3 { $vllmHttp.Get('/metrics',196608,$deadline) }
            $value = Invoke-MineruPhase $call 4 { New-MineruJson @('api_process',$frame.Get('values').Get('api_process').Raw,'host_cgroup',$frame.Get('values').Get('host_cgroup').Raw,'queue_vllm',$queue.Observe($health,$http,$metrics)) }
        }
        # Retained before the freshness check so an expiry there still shows the
        # complete phase timeline of the call that consumed the deadline.
        Complete-MineruPhaseCall $call
        $null = [MineruResidentWire]::Remaining($deadline)
        return $value
    }
    $closeAction = [Func[long,string]]{
        param([long]$Boundary)
        $linuxClosedSha = 'null'
        if ($null -ne $linux) {
            $closeFrame = Read-MineruLinuxResponse 'close' $Boundary
            $closed = [MineruResidentWire]::Parse($linux.Read($Boundary),65536)
            $closed.Keys([string[]]@('contract_version','kind','epoch_sha256','sequence','sampler_epoch_sha256','sampler_pid','sampler_wait_status','sampler_exit_cpu','supervisor_pre_attestation_cpu','pre_attestation_monotonic_ns'))
            if ((Get-MineruString $closed 'contract_version') -cne 'mineru.linux-resident-supervisor.v1' -or (Get-MineruString $closed 'kind') -cne 'closed' -or
                (Get-MineruString $closed 'epoch_sha256') -cne (Get-MineruString $linuxReady 'epoch_sha256') -or (Get-MineruString $closed 'sampler_epoch_sha256') -cne (Get-MineruString $samplerReady 'epoch_sha256') -or
                $closed.Get('sequence').Integer() -ne $script:linuxSequence -or $closed.Get('sampler_pid').Integer() -ne $samplerIdentity.Get('pid').Integer() -or $closed.Get('sampler_wait_status').Integer() -ne 0 -or
                $closed.Get('pre_attestation_monotonic_ns').Integer() -lt $script:linuxLastFinished) { throw 'Linux closed identity/sequence/exit mismatch' }
            Assert-MineruLinuxCpu $closed.Get('sampler_exit_cpu')
            $closed.Get('supervisor_pre_attestation_cpu').Keys([string[]]@('user_ns_total','system_ns_total'))
            $null = $closed.Get('supervisor_pre_attestation_cpu').Get('user_ns_total').Integer()
            $null = $closed.Get('supervisor_pre_attestation_cpu').Get('system_ns_total').Integer()
            $linux.Finish($Boundary)
            $linux.Dispose()
            $linuxReceipt = New-MineruJson @('contract_version','"mineru.windows-linux-closed-receipt.v1"','session',(Quote-MineruJson $state.Session),'config_sha256',(Quote-MineruJson $ExpectedConfigSha256),'backend_ready',$backendReady,'close',$closeFrame.Raw,'closed',$closed.Raw)
            Write-MineruSessionArtifact 'linux-closed.json' $linuxReceipt
            $linuxClosedSha = Quote-MineruJson ([MineruResidentWire]::Hash([MineruResidentWire]::Utf8.GetBytes($linuxReceipt)))
        }
        if ($null -ne $gpu) { $gpu.Dispose() }
        if ($null -ne $apiHttp) { $apiHttp.Dispose() }
        if ($null -ne $vllmHttp) { $vllmHttp.Dispose() }
        $closedJson = New-MineruJson @('contract_version','"mineru.windows-resident-closed.v2"','session',(Quote-MineruJson $state.Session),'config_sha256',(Quote-MineruJson $ExpectedConfigSha256),'identity',$state.Identity,'linux_closed_sha256',$linuxClosedSha,'sampling',$endpoint.CloseBoundaryJson)
        Write-MineruSessionArtifact 'closed.json' $closedJson
        return $closedJson
    }
    $endpoint.Run($readyAction,$sampleAction,$closeAction)
} catch { Add-MineruFailure $_ }
finally {
    foreach ($resource in @($endpoint,$gpu,$apiHttp,$vllmHttp,$linux)) {
        if ($null -ne $resource) {
            try { $resource.Dispose() } catch { $failures.Add($_.Exception) }
        }
    }
    Write-MineruPhaseTail
    foreach ($pin in $pins) {
        try { $pin.Dispose() } catch { $failures.Add($_.Exception) }
    }
}
if ($failures.Count -gt 0) {
    Write-MineruFailureDetail 'resident exporter' 'exporter-failure.txt'
    throw [AggregateException]::new('resident exporter failed',$failures)
}
