<#
.SYNOPSIS
Narrow installation owner: verify a staged release, hold installation exclusivity, run the existing
installer inside a finite Windows Job, and judge only from records and real post-readback.

.DESCRIPTION
Installation is the only action here. The release manifest and every packaged file are hash-verified
before anything runs. One exclusive lock file handle next to the target compose is held from that point
until exit, so two official installers cannot run concurrently. The tracked telemetry Job supervisor is
compiled from the packaged production sources (fresh csc child, bounded) and loaded; its Run() owns the
child installer's whole process tree with KILL_ON_CLOSE and a finite lifetime. The Job can only end
Windows CLI processes: a build or recreate request the Docker daemon already accepted is not cancelled
by a timeout, so a forced or missing exit leaves the outcome "unknown" with write permission closed and
never triggers an automatic rollback. Success requires the installer's own result record, the persisted
install receipt, the compose target hash, the running API image and an idle health readback to agree.
A binding v3 selects one phase of the same-root registry maintenance instead: the Job child is
run_mineru_same_root_maintenance.ps1, which owns every maintenance mutation; this owner still only reads
(closed idle health, the API's own quiescence proof, docker inspect) and judges. While that maintenance
is pending (pending.json beside the install receipt), every v1/v2 installation is refused.
Exit codes: 0 verified; 64 input; 65 identity; 70 failed or unknown.
#>
param(
    [Parameter(Mandatory=$true)][string]$ReleaseRoot,
    [Parameter(Mandatory=$true)][string]$ExpectedManifestSha256,
    [Parameter(Mandatory=$true)][string]$OperationDirectory,
    [Parameter(Mandatory=$true)][Alias('PrivateBinding')][string]$InstallationBinding,
    [string]$ComposeTarget='C:\ProgramData\compose.tailnet.yaml',
    [string]$ReceiptTarget='C:\ProgramData\agent-invest\mineru-runtime-v6\install-receipt.json',
    [string]$OutputRoot='C:\ProgramData\agent-invest\mineru-api-output'
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
$ExitCode=70
$utf8=[Text.UTF8Encoding]::new($false,$true)
$clock=[Diagnostics.Stopwatch]::StartNew()
$lockStream=$null; $pins=[Collections.Generic.List[IO.FileStream]]::new()
$firstError=$null; $status='failed'; $daemonSide='unknown'; $writePermission='closed'; $phase='start'
$manifestSha=$null; $composeSha=$null; $capacitySha=$null; $accounting=$null; $installerResult=$null; $loaded=$null; $installerExit=$null; $recordsDir=$null
$installationVerified=$false; $idleProofBefore=$null; $operationKind="api-compatibility"
$existingReceiptSha=$null; $epochsBefore=$null; $epochsAfter=$null
$sameRootPhase=$null; $sameRootOperation=$null; $sameRootResult=$null; $sameRootProofPath=$null; $sameRootProofSha=$null; $sameRootApprovalPath=$null
function Fail([int]$Code,[string]$Message) { $script:ExitCode=$Code; throw $Message }
function Get-Sha256File([string]$Path) { return 'sha256:' + (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant() }
function Get-Sha256Bytes([byte[]]$Bytes) { $a=[Security.Cryptography.SHA256]::Create(); try { return 'sha256:' + ([BitConverter]::ToString($a.ComputeHash($Bytes))).Replace('-','').ToLowerInvariant() } finally { $a.Dispose() } }
function Write-NewRecord([string]$Name,[string]$Json) {
    $bytes=$utf8.GetBytes($Json)
    $file=[IO.FileStream]::new((Join-Path $OperationDirectory $Name),[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try { $file.Write($bytes,0,$bytes.Length); $file.Flush($true) } finally { $file.Dispose() }
}
function Read-BoundedFile([string]$Path,[long]$MaximumBytes) {
    $pin=[IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $pins.Add($pin)
    if ($pin.Length -lt 1 -or $pin.Length -gt $MaximumBytes) { throw ('file byte bound exceeded: ' + $Path) }
    $bytes=[byte[]]::new([int]$pin.Length); $offset=0
    while ($offset -lt $bytes.Length) { $read=$pin.Read($bytes,$offset,$bytes.Length - $offset); if ($read -le 0) { throw ('unexpected EOF: ' + $Path) }; $offset+=$read }
    return ,$bytes
}
function ConvertFrom-StrictJson([byte[]]$Bytes) { return ($utf8.GetString($Bytes) | ConvertFrom-Json) }
function Assert-Closed($Value,[string[]]$Names,[string]$Label) {
    if ($null -eq $Value -or $Value.GetType().FullName -cne 'System.Management.Automation.PSCustomObject' -or
        (@($Value.PSObject.Properties.Name | Sort-Object) -join ',') -cne (@($Names | Sort-Object) -join ',')) { throw ($Label + ' fields are not closed') }
}
# Windows argv encoding identical to the tracked installer/collector helper.
function ConvertTo-WindowsCommandLineArgument([string]$Value) {
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $builder=New-Object Text.StringBuilder
    [void]$builder.Append('"')
    [int]$backslashes=0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $backslashes+=1; continue }
        if ($character -eq '"') { [void]$builder.Append(('\' * (($backslashes * 2) + 1))); [void]$builder.Append('"'); $backslashes=0; continue }
        if ($backslashes -gt 0) { [void]$builder.Append(('\' * $backslashes)); $backslashes=0 }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) { [void]$builder.Append(('\' * ($backslashes * 2))) }
    [void]$builder.Append('"')
    return $builder.ToString()
}
# Bounded read-only native readback (pre-mutation and post-readback only; never used for the installer itself).
function Invoke-BoundedReadback([string]$FilePath,[string[]]$Arguments,[int]$TimeoutMilliseconds=60000,[int]$MaximumBytes=1048576) {
    $start=[Diagnostics.ProcessStartInfo]::new($FilePath)
    $start.Arguments=(@($Arguments | ForEach-Object { ConvertTo-WindowsCommandLineArgument $_ }) -join ' ')
    $start.UseShellExecute=$false; $start.CreateNoWindow=$true; $start.RedirectStandardOutput=$true; $start.RedirectStandardError=$true
    $p=[Diagnostics.Process]::new(); $p.StartInfo=$start
    $out=[IO.MemoryStream]::new(); $err=[IO.MemoryStream]::new(); $buf=[byte[]]::new(16384); $ebuf=[byte[]]::new(16384)
    $sw=[Diagnostics.Stopwatch]::StartNew()
    try {
        if (-not $p.Start()) { throw ('readback did not start: ' + $FilePath) }
        $os=$p.StandardOutput.BaseStream; $es=$p.StandardError.BaseStream
        $ot=$os.ReadAsync($buf,0,$buf.Length); $et=$es.ReadAsync($ebuf,0,$ebuf.Length); $oeof=$false; $eeof=$false
        while (-not ($oeof -and $eeof)) {
            $remaining=$TimeoutMilliseconds - $sw.ElapsedMilliseconds
            if ($remaining -le 0) { throw ('readback deadline exceeded: ' + $FilePath) }
            $pending=@(); if (-not $oeof) { $pending+=$ot }; if (-not $eeof) { $pending+=$et }
            $i=[Threading.Tasks.Task]::WaitAny([Threading.Tasks.Task[]]$pending,[int][Math]::Min($remaining,1000))
            if ($i -lt 0) { continue }
            $t=$pending[$i]; $n=$t.GetAwaiter().GetResult()
            if ([object]::ReferenceEquals($t,$ot)) { if ($n -eq 0) { $oeof=$true } else { $out.Write($buf,0,$n); $ot=$os.ReadAsync($buf,0,$buf.Length) } }
            else { if ($n -eq 0) { $eeof=$true } else { $err.Write($ebuf,0,$n); $et=$es.ReadAsync($ebuf,0,$ebuf.Length) } }
            if ($out.Length + $err.Length -gt $MaximumBytes) { throw ('readback output bound exceeded: ' + $FilePath) }
        }
        if (-not $p.WaitForExit([int][Math]::Max(1,$TimeoutMilliseconds - $sw.ElapsedMilliseconds))) { throw ('readback did not exit: ' + $FilePath) }
        return [pscustomobject]@{ExitCode=$p.ExitCode; StandardOutput=[Text.Encoding]::UTF8.GetString($out.ToArray()); StandardError=[Text.Encoding]::UTF8.GetString($err.ToArray())}
    } catch {
        try { if (-not $p.HasExited) { $p.Kill(); [void]$p.WaitForExit(10000) } } catch { }
        throw
    } finally { $p.Dispose() }
}

function Read-MineruEpochs([string]$Docker) {
    $probe=Invoke-BoundedReadback $Docker @('inspect','mineru-api','mineru-api-proxy','mineru-openai-server')
    if ($probe.ExitCode -ne 0) { throw 'three-service epoch readback failed' }
    $inspect=$probe.StandardOutput | ConvertFrom-Json
    if (@($inspect).Count -ne 3) { throw 'three-service epoch readback is incomplete' }
    $epochs=[ordered]@{}
    foreach ($name in @('mineru-api','mineru-api-proxy','mineru-openai-server')) {
        $matches=@($inspect | Where-Object { $_.Name -ceq ('/' + $name) })
        if ($matches.Count -ne 1) { throw ('service epoch is not unique: ' + $name) }
        $c=$matches[0]
        if (-not [bool]$c.State.Running -or [string]$c.State.Health.Status -cne 'healthy' -or
            [int]$c.RestartCount -ne 0 -or [bool]$c.State.OOMKilled -or
            [string]$c.Id -cnotmatch '^[a-f0-9]{64}$' -or [string]$c.Image -cnotmatch '^sha256:[a-f0-9]{64}$' -or
            [string]::IsNullOrWhiteSpace([string]$c.State.StartedAt)) { throw ('service epoch is invalid: ' + $name) }
        $epochs[$name]=[ordered]@{container_id=[string]$c.Id;started_at=[string]$c.State.StartedAt;image_id=[string]$c.Image}
    }
    return $epochs
}
function Assert-InferenceEpochs($Before,$After) {
    foreach ($name in @('mineru-api','mineru-api-proxy')) {
        foreach ($field in @('container_id','started_at','image_id')) {
            if ([string]$Before[$name][$field] -cne [string]$After[$name][$field]) { throw ('untargeted service epoch changed: ' + $name) }
        }
    }
    $name='mineru-openai-server'
    if ([string]$Before[$name].image_id -cne [string]$After[$name].image_id -or
        [string]$Before[$name].container_id -ceq [string]$After[$name].container_id -or
        [string]$Before[$name].started_at -ceq [string]$After[$name].started_at) { throw 'inference service epoch did not change exactly once' }
}

# Closed idle proof from the explicit-capacity health wire: legacy queued/processing gauges alone are not proof.
# The literal sets below are the producer's (agent_task_protocol_v2.admission_status + the serving admission
# binding, agent_capacity_observation.snapshot, DurableTaskRegistry.storage_observation) exactly as
# validate_mineru_task_admission / validate_mineru_capacity_wire_health require them: 12 admission integers
# (nonterminal_limit positive, the other 11 zero when idle), 2 tags, 2 booleans, a null reason; the stage and
# 2 HTTP counters; 4 owner-control fields. The capacity version is one closed family, never a schema string
# alone: capacity v1 serves observation v1 with registry v3, 5 stage counters and no storage ledger; storage
# capacity v2 serves observation v2 with registry v4, 7 stage counters, the 9-field result-storage ledger and
# task runtime v4, which binds the ledger's policy to the expected capacity. Idle means every responsibility,
# stage, HTTP, ingress, producer, promise, completion, wait and hold count is zero; retained source/result
# bytes are not work. A cold HTTP limiter reports the same zero counters and needs no other evidence.
# An empty, partial, renamed, extra, mixed, coerced or string-typed value is a refusal, never an idle proof.
function Read-IdleCapacityHealth([string]$Docker,[string]$ExpectedCapacitySha256,[string]$Label) {
    $probe=Invoke-BoundedReadback $Docker @('exec','mineru-api','/usr/bin/python3.12','-I','-c','import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen("http://127.0.0.1:8000/health",timeout=5))))')
    if ($probe.ExitCode -ne 0) { throw ($Label + ': API health readback failed') }
    $h=$probe.StandardOutput.Trim() | ConvertFrom-Json
    if ($null -eq $h -or $h -isnot [Management.Automation.PSCustomObject]) { throw ($Label + ': API health is not a JSON object') }
    foreach ($n in @('status','queued_tasks','processing_tasks','task_admission','capacity_observation')) { if ($h.PSObject.Properties.Name -cnotcontains $n) { throw ($Label + ': API health lacks ' + $n) } }
    if ($h.status -isnot [string] -or $h.status -cne 'healthy') { throw ($Label + ': API is not healthy') }
    foreach ($n in @('queued_tasks','processing_tasks')) { $v=$h.$n; if ($v -isnot [int] -and $v -isnot [long]) { throw ($Label + ': ' + $n + ' is not an integer') }; if ([long]$v -ne 0) { throw ($Label + ': API is not idle: ' + $n) } }
    $adm=$h.task_admission; $obs=$h.capacity_observation
    if ($null -eq $adm -or $null -eq $obs) { throw ($Label + ': API health lacks the explicit-capacity admission/observation evidence') }
    if ($obs -isnot [Management.Automation.PSCustomObject] -or $obs.PSObject.Properties.Name -cnotcontains 'schema' -or $obs.schema -isnot [string] -or
        $obs.schema -cnotin @('mineru.capacity-observation.v1','mineru.capacity-observation.v2')) { throw ($Label + ': capacity observation schema is not a supported explicit-capacity version') }
    $storage=$obs.schema -ceq 'mineru.capacity-observation.v2'
    if ($obs.PSObject.Properties.Name -cnotcontains 'capacity_config_sha256' -or $obs.capacity_config_sha256 -isnot [string] -or $obs.capacity_config_sha256 -cne $ExpectedCapacitySha256) { throw ($Label + ': API capacity identity differs from the expected value') }
    foreach ($n in @('stage_counters','http_counters','owner_control')) { if ($obs.PSObject.Properties.Name -cnotcontains $n) { throw ($Label + ': capacity observation lacks ' + $n) } }
    if ($storage -ne ($obs.PSObject.Properties.Name -ccontains 'result_storage')) { throw ($Label + ': capacity observation storage ledger does not match its version') }
    $registrySchema=$(if ($storage) { 'mineru-task-registry.v4' } else { 'mineru-task-registry.v3' })
    $stageCounters=@('result_capacity_waiting','parse_waiting','parse_active','finalizer_waiting','finalizer_active')
    if ($storage) { $stageCounters+=@('source_growth_waiting','completion_waiting') }
    $zeroCounters=@('ingress_tasks','accepted_pending_tasks','accepted_processing_tasks','accepted_finalizing_tasks','durable_nonterminal_tasks','routeless_accepted_tasks','ingress_cleanup_tasks','unowned_ingress_tasks','scheduled_tasks','queue_depth','active_processors')
    Assert-Closed $adm (@('schema','registry_schema','nonterminal_limit','recovery_overcommitted','admission_open','blocked_reason') + $zeroCounters) ($Label + ': task_admission')
    Assert-Closed $obs.stage_counters $stageCounters ($Label + ': stage_counters')
    Assert-Closed $obs.http_counters @('active_requests','pending_requests') ($Label + ': http_counters')
    Assert-Closed $obs.owner_control @('foreign_loop_observed','soft_drain_requested','soft_drain_applied','trigger') ($Label + ': owner_control')
    if ($adm.schema -isnot [string] -or $adm.schema -cne 'mineru-task-admission.v1' -or $adm.registry_schema -isnot [string] -or $adm.registry_schema -cne $registrySchema) { throw ($Label + ': task_admission schema tags differ from the serving contract') }
    $limit=$adm.nonterminal_limit
    if (($limit -isnot [int] -and $limit -isnot [long]) -or [long]$limit -lt 1 -or [long]$limit -gt 128) { throw ($Label + ': task_admission.nonterminal_limit is not a positive integer') }
    foreach ($flag in @('recovery_overcommitted','admission_open')) { if ($adm.$flag -isnot [bool]) { throw ($Label + ': task_admission.' + $flag + ' is not a boolean') } }
    foreach ($flag in @('foreign_loop_observed','soft_drain_requested','soft_drain_applied')) { if ($obs.owner_control.$flag -isnot [bool]) { throw ($Label + ': owner_control.' + $flag + ' is not a boolean') } }
    $busy=@()
    foreach ($n in $zeroCounters) { $v=$adm.$n; if ($v -isnot [int] -and $v -isnot [long]) { throw ($Label + ': task_admission.' + $n + ' is not an integer') }; if ([long]$v -ne 0) { $busy+=('task_admission.' + $n) } }
    foreach ($pr in $obs.stage_counters.PSObject.Properties) { if ($pr.Value -isnot [int] -and $pr.Value -isnot [long]) { throw ($Label + ': stage_counters.' + $pr.Name + ' is not an integer') }; if ([long]$pr.Value -ne 0) { $busy+=('stage_counters.' + $pr.Name) } }
    foreach ($pr in $obs.http_counters.PSObject.Properties) { if ($pr.Value -isnot [int] -and $pr.Value -isnot [long]) { throw ($Label + ': http_counters.' + $pr.Name + ' is not an integer') }; if ([long]$pr.Value -ne 0) { $busy+=('http_counters.' + $pr.Name) } }
    if ($storage) {
        # Storage capacity: runtime v4 names the expected capacity and the ledger's policy; the ledger is idle.
        if ($h.PSObject.Properties.Name -cnotcontains 'task_protocol_runtime') { throw ($Label + ': storage-managed API health lacks task_protocol_runtime') }
        $runtime=$h.task_protocol_runtime; $ledger=$obs.result_storage
        Assert-Closed $runtime @('schema','enabled','task_registry_max_records','registry_schema','admission_scope','capacity_config_sha256','result_storage_policy_sha256') ($Label + ': task_protocol_runtime')
        Assert-Closed $ledger @('policy_sha256','source_bytes','ingress_bytes','result_bytes','growing_producers','outstanding_promise_bytes','completion_queue_depth','waiting_tasks','blocked_tasks') ($Label + ': result_storage')
        Assert-Closed $ledger.waiting_tasks @('completion_capacity','free_floor','source_growth_capacity') ($Label + ': result_storage.waiting_tasks')
        $records=$runtime.task_registry_max_records
        if ($runtime.schema -isnot [string] -or $runtime.schema -cne 'mineru-task-runtime.v4' -or $runtime.enabled -isnot [bool] -or -not $runtime.enabled -or
            $runtime.registry_schema -isnot [string] -or $runtime.registry_schema -cne $registrySchema -or
            $runtime.admission_scope -isnot [string] -or $runtime.admission_scope -cne 'post_form_owned_upload' -or
            ($records -isnot [int] -and $records -isnot [long]) -or [long]$records -ne 128 -or
            $runtime.capacity_config_sha256 -isnot [string] -or $runtime.capacity_config_sha256 -cne $ExpectedCapacitySha256) { throw ($Label + ': storage-managed task runtime differs from the serving contract') }
        if ($ledger.policy_sha256 -isnot [string] -or $ledger.policy_sha256 -cnotmatch '\Asha256:[0-9a-f]{64}\z' -or
            $runtime.result_storage_policy_sha256 -isnot [string] -or $runtime.result_storage_policy_sha256 -cne $ledger.policy_sha256) { throw ($Label + ': result storage ledger is not bound to the serving storage policy') }
        foreach ($n in @('source_bytes','result_bytes')) { $v=$ledger.$n; if (($v -isnot [int] -and $v -isnot [long]) -or [long]$v -lt 0) { throw ($Label + ': result_storage.' + $n + ' is not a non-negative integer') } }
        foreach ($n in @('ingress_bytes','growing_producers','outstanding_promise_bytes','completion_queue_depth','blocked_tasks')) {
            $v=$ledger.$n; if ($v -isnot [int] -and $v -isnot [long]) { throw ($Label + ': result_storage.' + $n + ' is not an integer') }; if ([long]$v -ne 0) { $busy+=('result_storage.' + $n) }
        }
        foreach ($pr in $ledger.waiting_tasks.PSObject.Properties) {
            if ($pr.Value -isnot [int] -and $pr.Value -isnot [long]) { throw ($Label + ': result_storage.waiting_tasks.' + $pr.Name + ' is not an integer') }; if ([long]$pr.Value -ne 0) { $busy+=('result_storage.waiting_tasks.' + $pr.Name) }
        }
    } else {
        # Explicit capacity v1 also binds its runtime to the expected capacity.
        if ($h.PSObject.Properties.Name -cnotcontains 'task_protocol_runtime') { throw ($Label + ': capacity-v1 API health lacks task_protocol_runtime') }
        $runtime=$h.task_protocol_runtime
        Assert-Closed $runtime @('schema','enabled','task_registry_max_records','task_result_reservation_bytes','max_unacked_result_bytes','registry_schema','admission_scope','capacity_config_sha256') ($Label + ': task_protocol_runtime')
        $records=$runtime.task_registry_max_records; $reservation=$runtime.task_result_reservation_bytes; $unacked=$runtime.max_unacked_result_bytes
        if ($runtime.schema -isnot [string] -or $runtime.schema -cne 'mineru-task-runtime.v3' -or $runtime.enabled -isnot [bool] -or -not $runtime.enabled -or
            $runtime.registry_schema -isnot [string] -or $runtime.registry_schema -cne $registrySchema -or
            $runtime.admission_scope -isnot [string] -or $runtime.admission_scope -cne 'post_form_owned_upload' -or
            $runtime.capacity_config_sha256 -isnot [string] -or $runtime.capacity_config_sha256 -cne $ExpectedCapacitySha256 -or
            ($records -isnot [int] -and $records -isnot [long]) -or [long]$records -ne 128 -or
            ($reservation -isnot [int] -and $reservation -isnot [long]) -or [long]$reservation -lt 1 -or
            ($unacked -isnot [int] -and $unacked -isnot [long]) -or [long]$unacked -lt [long]$reservation) { throw ($Label + ': legacy task runtime differs from the serving contract') }
    }
    if ($adm.recovery_overcommitted) { $busy+='task_admission.recovery_overcommitted' }
    if (-not $adm.admission_open) { $busy+='task_admission.admission_open=false' }
    if ($null -ne $adm.blocked_reason) { $busy+=('task_admission.blocked_reason=' + [string]$adm.blocked_reason) }
    foreach ($flag in @('foreign_loop_observed','soft_drain_requested','soft_drain_applied')) { if ($obs.owner_control.$flag) { $busy+=('owner_control.' + $flag) } }
    if ($null -ne $obs.owner_control.trigger) { $busy+=('owner_control.trigger=' + [string]$obs.owner_control.trigger) }
    if ($busy.Count -gt 0) { throw ($Label + ': API is not idle: ' + ($busy -join ',')) }
    return $h
}

# The API's own strict reader of its output root, exactly as the collector runs it: the installed module, no
# candidate source, no registry construction. One closed line; the maintenance child binds its values.
function Read-ApiQuiescenceProof([string]$Docker,[string]$Label) {
    $probe=Invoke-BoundedReadback $Docker @('exec','mineru-api','/usr/bin/python3.12','-I','-c',"import json; from pathlib import Path; from mineru.cli.agent_task_protocol_v2 import inspect_quiescent_output_root; print(json.dumps(inspect_quiescent_output_root(Path('/var/lib/mineru-api-output')), sort_keys=True))")
    if ($probe.ExitCode -ne 0) { throw ($Label + ': API quiescence proof failed: ' + $probe.StandardError.Trim()) }
    $line=$probe.StandardOutput.Trim()
    if ($line.Length -eq 0 -or $line.Contains("`n")) { throw ($Label + ': API quiescence proof is not one line') }
    $state=$line | ConvertFrom-Json
    Assert-Closed $state @('file_count','quiescence','total_bytes') ($Label + ': quiescence state')
    Assert-Closed $state.quiescence @('record_count','registry_sha256','root_identity','schema','submission_watermark_bucket') ($Label + ': quiescence proof')
    Assert-Closed $state.quiescence.root_identity @('device','inode','mode','path','uid') ($Label + ': root identity')
    if ([string]$state.quiescence.schema -cne 'mineru-output-quiescence.v1' -or [string]$state.quiescence.root_identity.path -cne '/var/lib/mineru-api-output' -or
        [long]$state.file_count -ne 1 -or [long]$state.total_bytes -lt 1 -or [string]$state.quiescence.registry_sha256 -cnotmatch '^sha256:[0-9a-f]{64}$') {
        throw ($Label + ': API quiescence proof is not one quiescent registry')
    }
    return [pscustomobject]@{ Line=$line; State=$state }
}
function Assert-SameRootProof($State,$Expected,[string]$Label) {
    foreach ($name in @('path','device','inode','uid','mode')) {
        if ([string]$State.quiescence.root_identity.$name -cne [string]$Expected.root_identity.$name) { throw ($Label + ': API root ' + $name + ' differs from the plan') }
    }
    if ([string]$State.quiescence.registry_sha256 -cne [string]$Expected.registry_sha256 -or [string]$State.total_bytes -cne [string]$Expected.registry_bytes -or
        [string]$State.quiescence.record_count -cne [string]$Expected.record_count -or
        [string]$State.quiescence.submission_watermark_bucket -cne [string]$Expected.submission_watermark_bucket) { throw ($Label + ': API registry differs from the plan') }
}

try {
    if ($PSVersionTable.PSVersion.Major -ne 5) { Fail 64 'Windows PowerShell 5.1 is required' }
    if (-not [Environment]::Is64BitProcess) { Fail 64 '64-bit PowerShell is required' }
    if ($ExpectedManifestSha256 -cnotmatch '^sha256:[0-9a-f]{64}$') { Fail 64 'Expected manifest hash must be canonical' }
    $ReleaseRoot=[IO.Path]::GetFullPath($ReleaseRoot); $OperationDirectory=[IO.Path]::GetFullPath($OperationDirectory)
    $InstallationBinding=[IO.Path]::GetFullPath($InstallationBinding)
    if (-not (Test-Path -LiteralPath $ReleaseRoot -PathType Container)) { Fail 64 ('release root missing: ' + $ReleaseRoot) }
    if (Test-Path -LiteralPath $OperationDirectory) { Fail 64 ('operation directory must be new: ' + $OperationDirectory) }
    if (-not (Test-Path -LiteralPath $InstallationBinding -PathType Leaf)) { Fail 64 'installation binding missing' }
    foreach ($forbidden in @('C:\ProgramData','C:\Windows','C:\Program Files')) {
        if ($OperationDirectory.StartsWith($forbidden,[StringComparison]::OrdinalIgnoreCase)) { Fail 64 ('operation directory must not be under ' + $forbidden) }
    }
    New-Item -ItemType Directory -Path $OperationDirectory | Out-Null

    # ---- binding ----------------------------------------------------------------------------------
    $binding=ConvertFrom-StrictJson (Read-BoundedFile $InstallationBinding 65536)
    if ($binding.contract_version -ceq 'm6.installation-binding.v1') {
        Assert-Closed $binding @('contract_version','expected_hostname','api_device_profile','expected_active_compose_sha256','expected_previous_capacity_sha256','job_lifetime_milliseconds','job_cleanup_milliseconds','mac_exclusivity_receipt_sha256') 'installation binding'
    } elseif ($binding.contract_version -ceq 'm6.installation-binding.v2') {
        Assert-Closed $binding @('contract_version','operation_kind','expected_hostname','api_device_profile','expected_active_compose_sha256','expected_previous_capacity_sha256','job_lifetime_milliseconds','job_cleanup_milliseconds','mac_exclusivity_receipt_sha256') 'installation binding'
        if ($binding.operation_kind -isnot [string] -or $binding.operation_kind -cne 'inference-recreate') { Fail 64 'installation operation kind is unsupported' }
        $operationKind='inference-recreate'
    } elseif ($binding.contract_version -ceq 'm6.installation-binding.v4') {
        Assert-Closed $binding @('contract_version','operation_kind','expected_hostname','api_device_profile','expected_active_compose_sha256','expected_previous_capacity_sha256','job_lifetime_milliseconds','job_cleanup_milliseconds','mac_exclusivity_receipt_sha256','legacy_scope_inventory_sha256') 'installation binding'
        if ($binding.operation_kind -cne 'full-install' -or [string]$binding.legacy_scope_inventory_sha256 -cnotmatch '^sha256:[0-9a-f]{64}$') { Fail 64 'full installation lacks the zero-duty inventory binding' }
        if ($null -eq $binding.expected_active_compose_sha256 -or $null -eq $binding.expected_previous_capacity_sha256) { Fail 64 'full installation requires the active compose and previous explicit capacity identities' }
        $inventoryPath=Join-Path (Split-Path -Parent $InstallationBinding) 'legacy-scope-inventory.json'
        $inventoryBytes=Read-BoundedFile $inventoryPath 16777216
        if ((Get-Sha256Bytes $inventoryBytes) -cne [string]$binding.legacy_scope_inventory_sha256) { Fail 65 'full installation zero-duty inventory bytes differ' }
        $inventory=ConvertFrom-StrictJson $inventoryBytes
        if ($inventory.contract_version -cne 'worker-legacy-scope-inventory.v1' -or @($inventory.members).Count -ne 0) { Fail 65 'full installation still has business duties' }
        $captured=[DateTimeOffset]::Parse([string]$inventory.captured_at_utc)
        $age=([DateTimeOffset]::UtcNow-$captured).TotalSeconds
        if ($age -lt -60 -or $age -gt 300) { Fail 65 'full installation zero-duty inventory is stale' }
        $operationKind='full-install'
    } elseif ($binding.contract_version -ceq 'm6.installation-binding.v3') {
        Assert-Closed $binding @('contract_version','operation_kind','phase','operation_id','expected_hostname','api_device_profile','expected_active_compose_sha256','expected_previous_capacity_sha256','job_lifetime_milliseconds','job_cleanup_milliseconds','mac_exclusivity_receipt_sha256','approval_sha256','legacy_scope_inventory_sha256') 'installation binding'
        if ($binding.operation_kind -isnot [string] -or $binding.operation_kind -cne 'same-root-reattestation') { Fail 64 'installation operation kind is unsupported' }
        if ($binding.phase -isnot [string] -or $binding.phase -cnotin @('prestop','observe','apply','restore','close','status')) { Fail 64 'same-root phase is unsupported' }
        if ($binding.operation_id -isnot [string] -or $binding.operation_id -cnotmatch '^sr-[0-9a-f]{32}$') { Fail 64 'same-root operation id is invalid' }
        # Same-root maintenance only re-attests a known explicit-capacity deployment; it never installs one.
        foreach ($name in @('expected_active_compose_sha256','expected_previous_capacity_sha256')) {
            if ($null -eq $binding.$name) { Fail 64 ('same-root maintenance requires ' + $name) }
        }
        if (($binding.phase -ceq 'apply') -ne ($null -ne $binding.approval_sha256) -or
            ($null -ne $binding.approval_sha256 -and [string]$binding.approval_sha256 -cnotmatch '^sha256:[0-9a-f]{64}$')) { Fail 64 'same-root approval identity is required for apply only' }
        if (($binding.phase -ceq 'prestop') -ne ($null -ne $binding.legacy_scope_inventory_sha256) -or
            ($null -ne $binding.legacy_scope_inventory_sha256 -and [string]$binding.legacy_scope_inventory_sha256 -cnotmatch '^sha256:[0-9a-f]{64}$')) { Fail 64 'same-root legacy-scope inventory identity is required for prestop only' }
        $operationKind='same-root-reattestation'; $sameRootPhase=[string]$binding.phase; $sameRootOperation=[string]$binding.operation_id
    } else { Fail 64 'installation binding contract is unsupported' }
    if ([Environment]::MachineName -cne [string]$binding.expected_hostname) { Fail 65 'native host differs from the installation binding' }
    if ([string]$binding.api_device_profile -cnotin @('cpu','cuda0')) { Fail 64 'installation binding device profile is invalid' }
    foreach ($name in @('expected_active_compose_sha256','expected_previous_capacity_sha256')) {
        $v=$binding.$name; if ($null -ne $v -and ([string]$v -cnotmatch '^sha256:[0-9a-f]{64}$')) { Fail 64 ('installation binding ' + $name + ' is not canonical') }
    }
    # The Mac exclusivity receipt is evidence that the deployment lock was held and
    # producers were verified stopped; a missing or malformed receipt never authorizes.
    if ($null -eq $binding.mac_exclusivity_receipt_sha256 -or ([string]$binding.mac_exclusivity_receipt_sha256 -cnotmatch '^sha256:[0-9a-f]{64}$')) { Fail 64 'installation binding lacks the Mac exclusivity receipt identity' }
    $lifetime=[long]$binding.job_lifetime_milliseconds; $cleanup=[long]$binding.job_cleanup_milliseconds
    if ($lifetime -lt 60000 -or $lifetime -gt 7200000 -or $cleanup -lt 1000 -or $cleanup -gt 10000) { Fail 64 'installation binding Job bounds are out of range' }

    # ---- manifest and package verification (every listed file, exact bytes) --------------------------
    $manifestPath=Join-Path $ReleaseRoot 'release-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { Fail 65 'release manifest missing' }
    $manifestBytes=Read-BoundedFile $manifestPath 262144
    $manifestSha=Get-Sha256Bytes $manifestBytes
    if ($manifestSha -cne $ExpectedManifestSha256) { Fail 65 ('release manifest hash differs: ' + $manifestSha) }
    $manifest=ConvertFrom-StrictJson $manifestBytes
    $manifestFields=@('contract_version','built_at_utc','source','inputs','projection','api_build','native_m6','installation','files')
    # v2 builds the owner-memory observation image; v3 builds the reclaim-mode image.
    $manifestInference=$manifest.contract_version -cin @('m6.release.v2','m6.release.v3')
    if ($manifestInference) { $manifestFields += 'inference_build' }
    elseif ($manifest.contract_version -cne 'm6.release.v1') { Fail 65 'release manifest contract is unsupported' }
    Assert-Closed $manifest $manifestFields 'release manifest'
    if ($manifestInference -and $operationKind -cnotin @('full-install','inference-recreate')) { Fail 65 'diagnostic source requires full installation' }
    if ([string]$manifest.projection.api_device_profile -cne [string]$binding.api_device_profile) { Fail 65 'release device profile differs from the installation binding' }
    $verified=[ordered]@{}
    foreach ($entry in $manifest.files) {
        $rel=[string]$entry.path
        if ($rel -match '\.\.|^/|^[A-Za-z]:|\\') { Fail 65 ('release file path is not relative: ' + $rel) }
        $path=Join-Path $ReleaseRoot ($rel -replace '/','\')
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { Fail 65 ('release file missing: ' + $rel) }
        $bytes=Read-BoundedFile $path 8388608
        if ($bytes.Length -ne [long]$entry.bytes -or (Get-Sha256Bytes $bytes) -cne [string]$entry.sha256) { Fail 65 ('release file bytes differ: ' + $rel) }
        $verified[$rel]=[string]$entry.sha256
    }
    foreach ($required in @('windows/install_mineru_fixed_api.ps1','windows/collect_mineru_runtime.ps1','windows/build_mineru_telemetry_assembly.ps1',
        'windows/load_mineru_telemetry_assembly.ps1','windows/mineru_telemetry_job_supervisor.cs','windows/mineru_nvml_backend.cs','windows/mineru_resident_wire.cs',
        'compose/mineru-windows.compose.yaml','api-context/Dockerfile','api-context/patch_mineru_344.py','api-context/capacity-config.json')) {
        if (-not $verified.Contains($required)) { Fail 65 ('release package lacks ' + $required) }
    }
    if ($operationKind -ceq 'same-root-reattestation') {
        foreach ($required in @('windows/run_mineru_same_root_maintenance.ps1','windows/mineru_same_root_identity.cs',
            'windows/mineru_same_root_registry_helper.py','api-context/agent_task_protocol_v2.py')) {
            if (-not $verified.Contains($required)) { Fail 65 ('release package lacks ' + $required) }
        }
    }
    if ($verified['windows/install_mineru_fixed_api.ps1'] -cne [string]$manifest.installation.installer_sha256 -or
        $verified['windows/collect_mineru_runtime.ps1'] -cne [string]$manifest.installation.collector_sha256 -or
        $verified['compose/mineru-windows.compose.yaml'] -cne [string]$manifest.projection.compose_sha256 -or
        $verified['api-context/capacity-config.json'] -cne [string]$manifest.api_build.capacity_config_sha256) { Fail 65 'release manifest identities disagree with its files' }
    $capacitySha=[string]$manifest.api_build.capacity_config_sha256
    $composeSha=[string]$manifest.projection.compose_sha256
    if ($operationKind -ceq 'inference-recreate') {
        if ([string]$binding.expected_active_compose_sha256 -cne $composeSha -or
            [string]$binding.expected_previous_capacity_sha256 -cne $capacitySha) {
            Fail 65 'inference recreation must bind the unchanged active compose and capacity'
        }
    }

    # ---- installation exclusivity: one lock handle next to the target compose ---------------------------
    $lockPath=$ComposeTarget + '.installation.lock'
    try { $lockStream=[IO.FileStream]::new($lockPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None) }
    catch { Fail 70 ('installation lock is held by another owner: ' + $lockPath) }
    $lockText=$utf8.GetBytes((@{pid=$PID;utc=[DateTime]::UtcNow.ToString('o');release=$manifestSha} | ConvertTo-Json -Compress))
    $lockStream.SetLength(0); $lockStream.Write($lockText,0,$lockText.Length); $lockStream.Flush($true)
    $phase='locked'
    # Fresh preconditions under the lock, immediately before any mutation: active compose identity,
    # previous explicit capacity identity and a closed idle proof from the running API.
    $docker=(Get-Command docker.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1 -ExpandProperty Source)
    # A pending same-root maintenance owns the output root and the three business containers until its own
    # verified close; there is no switch that lets an installation through.
    $sameRootState=Join-Path (Split-Path -Parent $ReceiptTarget) 'same-root-maintenance'
    $sameRootPending=Join-Path $sameRootState 'pending.json'
    if ($operationKind -cne 'same-root-reattestation' -and (Test-Path -LiteralPath $sameRootPending)) {
        Fail 65 ('a same-root registry maintenance is pending: ' + $sameRootPending)
    }
    if ($null -ne $binding.expected_active_compose_sha256) {
        if (-not (Test-Path -LiteralPath $ComposeTarget -PathType Leaf)) { Fail 65 'expected active compose is absent' }
        $active=Get-Sha256File $ComposeTarget
        if ($active -cne [string]$binding.expected_active_compose_sha256) { Fail 65 ('active compose differs from the installation binding: ' + $active) }
    }
    if ($operationKind -ceq 'same-root-reattestation') {
        # Unpinned read: the close child moves this marker, so no handle may stay open on it.
        $pendingOperation=$null; $pendingRelease=$null
        if (Test-Path -LiteralPath $sameRootPending -PathType Leaf) {
            $pendingBytes=[IO.File]::ReadAllBytes($sameRootPending)
            if ($pendingBytes.Length -lt 1 -or $pendingBytes.Length -gt 65536) { Fail 65 'same-root pending marker is outside its byte bound' }
            $pendingValue=ConvertFrom-StrictJson $pendingBytes
            $pendingOperation=[string]$pendingValue.operation_id; $pendingRelease=[string]$pendingValue.release_manifest_sha256
        }
        if ($null -ne $pendingOperation -and $pendingOperation -cne $sameRootOperation) { Fail 65 ('another same-root operation is pending: ' + $pendingOperation) }
        # The pending operation belongs to the release that started it; only status may look at it from another.
        if ($null -ne $pendingOperation -and $sameRootPhase -cne 'status' -and $pendingRelease -cne $manifestSha) {
            Fail 65 ('same-root operation ' + $pendingOperation + ' was started by release ' + $pendingRelease + '; only that release continues it')
        }
        if ($null -eq $pendingOperation -and $sameRootPhase -cnotin @('prestop','close','status')) { Fail 65 'no same-root operation is pending' }
        if (-not (Test-Path -LiteralPath $ReceiptTarget -PathType Leaf)) { Fail 65 'existing install receipt is absent' }
        $sameRootReceipt=ConvertFrom-StrictJson (Read-BoundedFile $ReceiptTarget 4194304)
        if ([string]$sameRootReceipt.schema -cne 'mineru-windows-install-receipt.v2' -or -not [bool]$sameRootReceipt.success -or
            [string]$sameRootReceipt.compose_sha256 -cne [string]$binding.expected_active_compose_sha256 -or
            [string]$sameRootReceipt.api_compatibility_image.image_id -cnotmatch '^sha256:[a-f0-9]{64}$') { Fail 65 'existing install receipt does not bind the active deployment' }
        $sameRootApiImage=[string]$sameRootReceipt.api_compatibility_image.image_id
        $previousCapacity=[string]$binding.expected_previous_capacity_sha256
        # Only a live, closed idle API with its own strict quiescence proof may start a maintenance (prestop
        # without a pending marker) or end one (close); the stopped phases in between need no live API.
        $sameRootLiveGate=($sameRootPhase -ceq 'prestop' -and $null -eq $pendingOperation) -or ($sameRootPhase -ceq 'close' -and $null -ne $pendingOperation)
        if ($sameRootLiveGate) {
            $apiImageNow=Invoke-BoundedReadback $docker @('inspect','--format','{{.Image}}','mineru-api')
            if ($apiImageNow.ExitCode -ne 0 -or $apiImageNow.StandardOutput.Trim() -cne $sameRootApiImage) { Fail 65 'running API image differs from the install receipt' }
            $null=Read-IdleCapacityHealth $docker $previousCapacity ('same-root ' + $sameRootPhase + ' before proof')
            $sameRootProof=Read-ApiQuiescenceProof $docker ('same-root ' + $sameRootPhase)
            $null=Read-IdleCapacityHealth $docker $previousCapacity ('same-root ' + $sameRootPhase + ' after proof')
            Write-NewRecord 'api-proof.json' ([ordered]@{contract_version='m6.same-root-api-proof.v1';operation_id=$sameRootOperation;phase=$sameRootPhase
                capacity_config_sha256=$previousCapacity;api_image_id=$sameRootApiImage;idle_health='explicit_capacity_closed'
                proof_line=$sameRootProof.Line;captured_utc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json -Compress -Depth 4)
            $sameRootProofPath=Join-Path $OperationDirectory 'api-proof.json'; $sameRootProofSha=Get-Sha256File $sameRootProofPath
            $idleProofBefore='explicit_capacity_closed'
        }
        if ($sameRootPhase -ceq 'apply') {
            $sameRootApprovalPath=Join-Path (Split-Path -Parent $InstallationBinding) 'same-root-approval.json'
            if (-not (Test-Path -LiteralPath $sameRootApprovalPath -PathType Leaf) -or
                (Get-Sha256File $sameRootApprovalPath) -cne [string]$binding.approval_sha256) { Fail 65 'staged same-root approval differs from the installation binding' }
        }
    } elseif ($null -ne $binding.expected_previous_capacity_sha256) {
        $null=Read-IdleCapacityHealth $docker ([string]$binding.expected_previous_capacity_sha256) 'pre-mutation'
        $idleProofBefore='explicit_capacity_closed'
    } else {
        $probe=Invoke-BoundedReadback $docker @('exec','mineru-api','/usr/bin/python3.12','-I','-c','import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen("http://127.0.0.1:8000/health",timeout=5))))')
        if ($probe.ExitCode -ne 0) { Fail 70 'pre-mutation API health readback failed' }
        $h=$probe.StandardOutput.Trim() | ConvertFrom-Json
        if ([string]$h.status -cne 'healthy' -or [int]$h.queued_tasks -ne 0 -or [int]$h.processing_tasks -ne 0) { Fail 65 'pre-mutation API is not healthy and idle' }
        if ($null -ne $h.PSObject.Properties['capacity_observation']) { Fail 65 'API already runs an explicit capacity; the previous capacity identity must be supplied' }
        $idleProofBefore='legacy_gauges_only'
    }

    if ($operationKind -ceq 'inference-recreate') {
        $collectorTarget='C:\ProgramData\agent-invest\mineru-runtime-v6\collect_mineru_runtime.ps1'
        $collectorSource=Join-Path $ReleaseRoot 'windows\collect_mineru_runtime.ps1'
        if (-not (Test-Path -LiteralPath $collectorTarget -PathType Leaf) -or
            (Get-Sha256File $collectorTarget) -cne (Get-Sha256File $collectorSource)) { Fail 65 'active collector differs from the release' }
        if ((Get-Sha256File $ComposeTarget) -cne $composeSha) { Fail 65 'active compose differs from the release' }
        # Keep the exact existing target files open for read across the Job: no owner may rewrite or replace them.
        $null=Read-BoundedFile $ComposeTarget 8388608
        $null=Read-BoundedFile $collectorTarget 8388608
        if (-not (Test-Path -LiteralPath $ReceiptTarget -PathType Leaf)) { Fail 65 'existing install receipt is absent' }
        $existingReceiptSha=Get-Sha256File $ReceiptTarget
        $existingReceipt=ConvertFrom-StrictJson (Read-BoundedFile $ReceiptTarget 4194304)
        if ([string]$existingReceipt.schema -cne 'mineru-windows-install-receipt.v2' -or
            -not [bool]$existingReceipt.success -or
            [string]$existingReceipt.compose_sha256 -cne $composeSha -or
            [string]$existingReceipt.collector_sha256 -cne (Get-Sha256File $collectorTarget) -or
            [string]$existingReceipt.api_compatibility_image.image_id -cnotmatch '^sha256:[a-f0-9]{64}$') {
            Fail 65 'existing install receipt does not bind the active release'
        }
        $apiImage=Invoke-BoundedReadback $docker @('inspect','--format','{{.Image}}','mineru-api')
        if ($apiImage.ExitCode -ne 0 -or $apiImage.StandardOutput.Trim() -cne [string]$existingReceipt.api_compatibility_image.image_id) {
            Fail 65 'running API image differs from the existing receipt'
        }
        $epochsBefore=Read-MineruEpochs $docker
    }

    if ($operationKind -ceq 'full-install') {
        $epochsBefore=Read-MineruEpochs $docker
        if (Test-Path -LiteralPath $ReceiptTarget -PathType Leaf) { $existingReceiptSha=Get-Sha256File $ReceiptTarget }
    }

    # ---- Job supervisor from packaged production sources (fresh csc child; no Add-Type) ---------------
    $windows=Join-Path $ReleaseRoot 'windows'
    $prepared=(& (Join-Path $windows 'build_mineru_telemetry_assembly.ps1') -OutputDirectory $OperationDirectory) | ConvertFrom-Json
    . (Join-Path $windows 'load_mineru_telemetry_assembly.ps1')
    $loaded=Import-MineruTelemetryPreparedAssembly -ManifestPath $prepared.manifest_path -ExpectedManifestSha256 $prepared.manifest_sha256 `
        -ExpectedNvmlSourceSha256 $verified['windows/mineru_nvml_backend.cs'] -ExpectedWireSourceSha256 $verified['windows/mineru_resident_wire.cs'] `
        -ExpectedSupervisorSourceSha256 $verified['windows/mineru_telemetry_job_supervisor.cs']
    $supervisorSourceSha=$verified['windows/mineru_telemetry_job_supervisor.cs']
    $phase='supervisor_loaded'

    # ---- the installer as an owned Job child ---------------------------------------------------------
    $recordsDir=Join-Path $OperationDirectory 'installer'
    $consolePath=Join-Path $OperationDirectory 'installer-console.txt'
    $installer=Join-Path $windows 'install_mineru_fixed_api.ps1'
    $budgetSeconds=[int][Math]::Max(60,[Math]::Floor(($lifetime - 60000) / 1000))
    # Parameter names stay bare; only values are single-quoted, so PowerShell binds each named
    # parameter and the switch exactly (a quoted '-Name' token would bind as a positional value).
    $installerParameters=[ordered]@{
        ComposeSource=(Join-Path $ReleaseRoot 'compose\mineru-windows.compose.yaml')
        CollectorSource=(Join-Path $windows 'collect_mineru_runtime.ps1')
        CompatDockerfileSource=(Join-Path $ReleaseRoot 'api-context\Dockerfile')
        CompatPatcherSource=(Join-Path $ReleaseRoot 'api-context\patch_mineru_344.py')
        CapacityConfigSource=(Join-Path $ReleaseRoot 'api-context\capacity-config.json')
        ExpectedCapacityConfigSha256=$capacitySha
        ApiOnlyCompatibilityUpgrade=$null
        OperationRecordDirectory=$recordsDir
        OperationBudgetSeconds=[string]$budgetSeconds
    }
    if ($operationKind -ceq 'inference-recreate') {
        $installerParameters.Remove('ApiOnlyCompatibilityUpgrade')
        $installerParameters['InferenceRecreate']=$null
        $installerParameters['ExpectedApiDeviceProfile']=[string]$binding.api_device_profile
        $installerParameters['CampaignApiCompatImageId']=[string]$existingReceipt.api_compatibility_image.image_id
    }
    if ($operationKind -ceq 'full-install') {
        $installerParameters.Remove('ApiOnlyCompatibilityUpgrade')
        $installerParameters['ExpectedFullApiDeviceProfile']=[string]$binding.api_device_profile
    }
    if ($manifestInference) {
            $contextNames=@('Dockerfile','mineru_inference_memory/__init__.py','mineru_inference_memory/core.py','mineru_inference_memory/worker.py')
            if ($manifest.contract_version -ceq 'm6.release.v3') {
                Assert-Closed $manifest.inference_build @('context','context_sha256','sources','mode') 'inference build'
                if ([string]$manifest.inference_build.mode -cne 'mineru.owner-memory-reclaim.v1') { Fail 65 'inference build mode is unsupported' }
                $contextNames=@('Dockerfile','mineru_inference_memory/__init__.py','mineru_inference_memory/core.py','mineru_inference_memory/reclaim.py','mineru_inference_memory/worker.py')
                $installerParameters['InferenceOwnerReclaim']=$null
            }
            Assert-Closed $manifest.inference_build.sources $contextNames 'inference build sources'
            foreach ($name in $contextNames) {
                $rel='inference-context/' + $name
                if (-not $verified.Contains($rel) -or $verified[$rel] -cne [string]$manifest.inference_build.sources.$name) { Fail 65 ('diagnostic context source differs: ' + $name) }
            }
            $installerParameters['InferenceDiagnosticContext']=(Join-Path $ReleaseRoot 'inference-context')
            $installerParameters['InferenceDiagnosticContextSha256']=[string]$manifest.inference_build.context_sha256
        }
    # A release profile declares the desired device, not always a device transition.
    # A changed explicit capacity uses the existing capacity-only comparison, which
    # retains device and all non-capacity fields. Mixed device/capacity edits still fail.
    if ($operationKind -ceq 'api-compatibility' -and ($null -eq $binding.expected_previous_capacity_sha256 -or
        [string]$binding.expected_previous_capacity_sha256 -ceq $capacitySha)) {
        $installerParameters['ApiDeviceProfile']=[string]$binding.api_device_profile
    }
    $childScript=$installer; $childParameters=$installerParameters
    if ($operationKind -ceq 'same-root-reattestation') {
        $recordsDir=Join-Path $OperationDirectory 'same-root'
        $consolePath=Join-Path $OperationDirectory 'same-root-console.txt'
        $childScript=Join-Path $windows 'run_mineru_same_root_maintenance.ps1'
        $childParameters=[ordered]@{
            Phase=$sameRootPhase
            OperationId=$sameRootOperation
            StateRoot=$sameRootState
            OutputRoot=[IO.Path]::GetFullPath($OutputRoot)
            ReleaseRoot=$ReleaseRoot
            RecordDirectory=$recordsDir
            ReleaseManifestSha256=$manifestSha
            ApiImageId=$sameRootApiImage
            HelperSourceSha256=$verified['windows/mineru_same_root_registry_helper.py']
            ProtocolSourceSha256=$verified['api-context/agent_task_protocol_v2.py']
            IdentitySourceSha256=$verified['windows/mineru_same_root_identity.cs']
            OperationBudgetSeconds=[string]$budgetSeconds
        }
        if ($null -ne $sameRootProofPath) { $childParameters['ProofPath']=$sameRootProofPath; $childParameters['ProofSha256']=$sameRootProofSha }
        if ($null -ne $sameRootApprovalPath) { $childParameters['ApprovalPath']=$sameRootApprovalPath; $childParameters['ApprovalSha256']=[string]$binding.approval_sha256 }
    }
    $installerArguments=@()
    $tokens=@()
    foreach ($entry in $childParameters.GetEnumerator()) {
        if ($entry.Key -cnotmatch '^[A-Za-z][A-Za-z0-9]*$') { Fail 64 ('installer parameter name is invalid: ' + $entry.Key) }
        if ($null -eq $entry.Value) { $tokens+=('-' + $entry.Key); $installerArguments+=('-' + $entry.Key); continue }
        $value=[string]$entry.Value
        if ($value -match '[\r\n\0]') { Fail 64 ('installer parameter value contains a forbidden character: ' + $entry.Key) }
        $tokens+=('-' + $entry.Key + " '" + ($value -replace "'","''") + "'")
        $installerArguments+=('-' + $entry.Key); $installerArguments+=$value
    }
    $childCommand="& '" + ($childScript -replace "'","''") + "' " + ($tokens -join ' ') + " *>> '" + ($consolePath -replace "'","''") + "'; exit `$LASTEXITCODE"
    $powershell=Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
    $childArguments=[string[]]@('-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-Command',$childCommand)
    $startRecord=[ordered]@{contract_version='m6.installation-operation-start.v1';hostname=[Environment]::MachineName;pid=$PID
        release_root=$ReleaseRoot;release_manifest_sha256=$manifestSha;capacity_config_sha256=$capacitySha;compose_sha256=$composeSha
        installation_binding_sha256=(Get-Sha256File $InstallationBinding);mac_exclusivity_receipt_sha256=$binding.mac_exclusivity_receipt_sha256
        supervisor_manifest_sha256=$prepared.manifest_sha256;job_lifetime_milliseconds=$lifetime;job_cleanup_milliseconds=$cleanup
        installer_arguments=$installerArguments;started_utc=[DateTime]::UtcNow.ToString('o')}
    if ($operationKind -ceq 'full-install') {
        $startRecord['operation_kind']=$operationKind
        $startRecord['legacy_scope_inventory_sha256']=[string]$binding.legacy_scope_inventory_sha256
    }
    if ($operationKind -ceq 'same-root-reattestation') {
        $startRecord['contract_version']='m6.same-root-operation-start.v1'; $startRecord['operation_kind']=$operationKind
        $startRecord['maintenance_phase']=$sameRootPhase; $startRecord['operation_id']=$sameRootOperation; $startRecord['api_proof_sha256']=$sameRootProofSha
    }
    Write-NewRecord 'operation-start.json' ($startRecord | ConvertTo-Json -Compress -Depth 6)
    $phase='job_running'
    $accountingJson=[MineruTelemetryJobSupervisor]::Run($powershell,$childArguments,[int]$lifetime,[int]$cleanup,$supervisorSourceSha)
    Write-NewRecord 'job-accounting.json' $accountingJson
    $accounting=$accountingJson | ConvertFrom-Json
    $installerExit=[int]$accounting.child_exit_code
    $phase='job_finished'
    if ([bool]$accounting.forced_termination) {
        # The Job ended the CLI tree; the daemon may still be building or recreating. Outcome unknown by construction.
        $daemonSide='unknown'; $status='unknown'
        Fail 70 ('installer Job was forced to terminate after its lifetime; daemon-side outcome unknown (child exit ' + $installerExit + ')')
    }
    if ([int]$accounting.job_active_processes -ne 0) { $status='unknown'; Fail 70 'installer Job reports live members after exit' }

    if ($operationKind -ceq 'same-root-reattestation') {
        # ---- judge the maintenance phase from the child's own record and real readback --------------------
        $resultPath=Join-Path $recordsDir 'same-root-result.json'
        if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf)) { $status='unknown'; Fail 70 ('same-root child left no result record (child exit ' + $installerExit + ')') }
        $sameRootResult=ConvertFrom-StrictJson (Read-BoundedFile $resultPath 4194304)
        if ([string]$sameRootResult.contract_version -cne 'm6.same-root-child-result.v1' -or [string]$sameRootResult.phase -cne $sameRootPhase -or
            [string]$sameRootResult.operation_id -cne $sameRootOperation) { $status='unknown'; Fail 70 'same-root child result does not bind this operation phase' }
        if ([string]$sameRootResult.status -cne 'pass') {
            $status=$(if ([string]$sameRootResult.status -ceq 'failed') { 'failed' } else { 'unknown' })
            Fail 70 ('same-root ' + $sameRootPhase + ' reported ' + [string]$sameRootResult.status + ': ' + [string]$sameRootResult.first_error)
        }
        if ($installerExit -ne 0) { $status='unknown'; Fail 70 ('same-root child result is pass but child exit is ' + $installerExit) }
        if ($sameRootPhase -ceq 'restore') {
            # The API's own strict reader must now prove the planned tuple and the expected registry bytes.
            try {
                $null=Read-IdleCapacityHealth $docker $previousCapacity 'same-root restore before proof'
                $restored=Read-ApiQuiescenceProof $docker 'same-root restore'
                Assert-SameRootProof $restored.State $sameRootResult.facts.expected 'same-root restore'
                $null=Read-IdleCapacityHealth $docker $previousCapacity 'same-root restore after proof'
            } catch { $status='failed'; Fail 70 $_.Exception.Message }
            Write-NewRecord 'api-proof-after.json' ([ordered]@{contract_version='m6.same-root-api-proof.v1';operation_id=$sameRootOperation;phase='restore'
                capacity_config_sha256=$previousCapacity;api_image_id=$sameRootApiImage;idle_health='explicit_capacity_closed'
                proof_line=$restored.Line;captured_utc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json -Compress -Depth 4)
        }
        if ($sameRootPhase -ceq 'close' -and [string]$sameRootResult.outcome -ceq 'closed') {
            try {
                $epochsAfter=Read-MineruEpochs $docker
                foreach ($item in @($sameRootResult.facts.containers)) {
                    if ([string]$epochsAfter[[string]$item.name].container_id -cne [string]$item.id) { throw ('restored container differs from its frozen identity: ' + [string]$item.name) }
                }
                $null=Read-IdleCapacityHealth $docker $previousCapacity 'same-root close'
            } catch { $status='unknown'; Fail 70 $_.Exception.Message }
        }
        # A maintenance phase never installs and never opens producers.
        $daemonSide='verified'; $status='pass'; $ExitCode=0
    } else {
        # ---- judge from records and real readback, never from the child's exit alone ----------------------
        $resultPath=Join-Path $recordsDir 'installer-result.json'
        if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf)) {
            $status=$(if ($installerExit -eq 0) { 'unknown' } else { 'failed' })
            Fail 70 ('installer left no result record (child exit ' + $installerExit + ')')
        }
        $installerResult=ConvertFrom-StrictJson (Read-BoundedFile $resultPath 4194304)
        if ($operationKind -ceq 'inference-recreate' -and [string]$installerResult.status -ceq 'pass' -and [string]$installerResult.operation_kind -cne $operationKind) {
            $status='unknown'; Fail 70 'installer result operation kind differs from the binding'
        }
        if ([string]$installerResult.status -cne 'pass') {
            $status=$(if ([string]$installerResult.status -ceq 'unknown') { 'unknown' } else { 'failed' })
            Fail 70 ('installer reported ' + [string]$installerResult.status + ': ' + [string]$installerResult.first_error)
        }
        if ($installerExit -ne 0) { $status='unknown'; Fail 70 ('installer result is pass but child exit is ' + $installerExit) }
        if (-not (Test-Path -LiteralPath $ReceiptTarget -PathType Leaf)) { $status='unknown'; Fail 70 'install receipt target is absent after a pass result' }
        if ($operationKind -ceq 'inference-recreate') {
            if ((Get-Sha256File $ReceiptTarget) -cne $existingReceiptSha -or
                [string]$installerResult.receipt_sha256 -cne $existingReceiptSha) {
                $status='unknown'; Fail 70 'inference recreation changed the existing install receipt'
            }
            $epochsAfter=Read-MineruEpochs $docker
            try { Assert-InferenceEpochs $epochsBefore $epochsAfter }
            catch { $status='unknown'; Fail 70 $_.Exception.Message }
        }
        $receipt=ConvertFrom-StrictJson (Read-BoundedFile $ReceiptTarget 4194304)
        if ([string]$receipt.schema -cne 'mineru-windows-install-receipt.v2' -or -not [bool]$receipt.success -or
            [string]$receipt.compose_sha256 -cne $composeSha -or [string]$receipt.compose_sha256 -cne [string]$installerResult.compose_sha256) {
            $status='unknown'; Fail 70 'persisted install receipt does not bind the release compose'
        }
        if ($operationKind -ceq 'inference-recreate' -and
            (Get-Sha256File 'C:\ProgramData\agent-invest\mineru-runtime-v6\collect_mineru_runtime.ps1') -cne $verified['windows/collect_mineru_runtime.ps1']) {
            $status='unknown'; Fail 70 'inference recreation changed the active collector'
        }
        $activeCompose=Get-Sha256File $ComposeTarget
        if ($activeCompose -cne $composeSha) { $status='unknown'; Fail 70 ('active compose after installation differs from the release: ' + $activeCompose) }
        $inspect=Invoke-BoundedReadback $docker @('inspect','--format','{{.Image}}','mineru-api')
        if ($inspect.ExitCode -ne 0) { $status='unknown'; Fail 70 'docker inspect of the API container failed after installation' }
        $imageId=$inspect.StandardOutput.Trim()
        if ($imageId -cne [string]$receipt.api_compatibility_image.image_id) { $status='unknown'; Fail 70 ('running API image differs from the receipt: ' + $imageId) }
        try { $null=Read-IdleCapacityHealth $docker $capacitySha 'post-installation' } catch { $status='unknown'; Fail 70 $_.Exception.Message }
        if ($operationKind -ceq 'full-install') { $epochsAfter=Read-MineruEpochs $docker }
        # Installation is verified; writer readiness stays closed until this image is qualified and bound.
        $daemonSide='verified'; $status='pass'; $installationVerified=$true; $ExitCode=0
    }
} catch {
    if ($null -eq $firstError) { $firstError=$_.Exception.Message }
    if ($ExitCode -eq 0) { $ExitCode=70 }
} finally {
    $result=[ordered]@{
        contract_version=$(if ($operationKind -ceq 'inference-recreate') { 'm6.installation-operation.v2' } else { 'm6.installation-operation.v1' }); status=$status; first_error=$firstError; phase=$phase
        daemon_side_outcome=$daemonSide; write_permission=$writePermission; installation_verified=$installationVerified
        next_required=$(if ($installationVerified) { @('qualify','bind') } else { @('read_only_recovery_judgement') }); idle_proof_before=$idleProofBefore; exit_code=$ExitCode
        hostname=[Environment]::MachineName; release_root=$ReleaseRoot; release_manifest_sha256=$manifestSha
        installer_child_exit_code=$installerExit; job_accounting=$accounting; installer_result_status=$(if ($null -ne $installerResult) { [string]$installerResult.status } else { $null })
        compose_sha256=$(if ($null -ne $manifestSha) { $composeSha } else { $null }); elapsed_milliseconds=$clock.ElapsedMilliseconds
        finished_utc=[DateTime]::UtcNow.ToString('o')
    }
    if ($operationKind -ceq 'inference-recreate') {
        $result['operation_kind']=$operationKind
        $result['existing_receipt_sha256']=$existingReceiptSha
        $result['service_epochs_before']=$epochsBefore
        $result['service_epochs_after']=$epochsAfter
    }
    if ($operationKind -ceq 'full-install') {
        $result['contract_version']='m6.installation-operation.v4'
        $result['operation_kind']=$operationKind
        $result['legacy_scope_inventory_sha256']=[string]$binding.legacy_scope_inventory_sha256
        $result['existing_receipt_sha256']=$existingReceiptSha
        $result['service_epochs_before']=$epochsBefore
        $result['service_epochs_after']=$epochsAfter
    }
    if ($operationKind -ceq 'same-root-reattestation') {
        $next=@('same_root_status','read_only_recovery_judgement')
        if ($status -ceq 'pass') {
            if ($sameRootPhase -ceq 'prestop') { $next=@('docker_storage_maintenance','observe') }
            elseif ($sameRootPhase -ceq 'observe') { $next=@($(if ([string]$sameRootResult.outcome -ceq 'reattest') { 'approve'; 'apply' } else { 'restore' })) }
            elseif ($sameRootPhase -ceq 'apply') { $next=@('restore') }
            elseif ($sameRootPhase -ceq 'restore') { $next=@('close') }
            elseif ($sameRootPhase -ceq 'close') { $next=@('api_compatibility_install') }
            else { $next=@() }
        }
        $result['contract_version']='m6.installation-operation.v3'
        $result['operation_kind']=$operationKind
        $result['maintenance_phase']=$sameRootPhase
        $result['operation_id']=$sameRootOperation
        $result['next_required']=$next
        $result['same_root_status']=$(if ($null -ne $sameRootResult) { [string]$sameRootResult.status } else { $null })
        $result['same_root_outcome']=$(if ($null -ne $sameRootResult) { $sameRootResult.outcome } else { $null })
        $result['pending_present']=$(if ($null -ne $sameRootResult) { $sameRootResult.pending_present } else { $null })
        $result['api_proof_sha256']=$sameRootProofSha
        $result['service_epochs_after']=$epochsAfter
    }
    $json=$result | ConvertTo-Json -Compress -Depth 8
    $persisted=$false
    try { if (Test-Path -LiteralPath $OperationDirectory -PathType Container) { Write-NewRecord 'operation-result.json' $json; $persisted=$true } }
    catch { if ($null -eq $firstError) { $firstError='operation result could not be persisted: ' + $_.Exception.Message; $json=(($result + @{first_error=$firstError}) | ConvertTo-Json -Compress -Depth 8) } }
    $json
    if ($null -ne $loaded) { foreach ($pin in $loaded.Pins) { try { $pin.Dispose() } catch { } } }
    foreach ($pin in $pins) { try { $pin.Dispose() } catch { } }
    if ($null -ne $lockStream) { try { $lockStream.Dispose() } catch { } }
    if (-not $persisted -and $ExitCode -eq 0) { $ExitCode=70 }
}
exit $ExitCode
