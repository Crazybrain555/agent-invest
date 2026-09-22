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
Exit codes: 0 verified; 64 input; 65 identity; 70 failed or unknown.
#>
param(
    [Parameter(Mandatory=$true)][string]$ReleaseRoot,
    [Parameter(Mandatory=$true)][string]$ExpectedManifestSha256,
    [Parameter(Mandatory=$true)][string]$OperationDirectory,
    [Parameter(Mandatory=$true)][Alias('PrivateBinding')][string]$InstallationBinding,
    [string]$ComposeTarget='C:\ProgramData\compose.tailnet.yaml',
    [string]$ReceiptTarget='C:\ProgramData\agent-invest\mineru-runtime-v6\install-receipt.json'
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
$ExitCode=70
$utf8=[Text.UTF8Encoding]::new($false,$true)
$clock=[Diagnostics.Stopwatch]::StartNew()
$lockStream=$null; $pins=[Collections.Generic.List[IO.FileStream]]::new()
$firstError=$null; $status='failed'; $daemonSide='unknown'; $writePermission='closed'; $phase='start'
$manifestSha=$null; $composeSha=$null; $capacitySha=$null; $accounting=$null; $installerResult=$null; $loaded=$null; $installerExit=$null; $recordsDir=$null
$installationVerified=$false; $idleProofBefore=$null
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

# Closed idle proof from the explicit-capacity health wire: legacy queued/processing gauges alone are not proof.
# The literal sets below are the producer's (agent_task_protocol_v2.admission_status + the serving admission
# binding, agent_capacity_observation.snapshot) exactly as validate_mineru_task_admission /
# validate_mineru_capacity_wire_health require them: 12 admission integers (nonterminal_limit positive, the
# other 11 zero when idle), 2 tags, 2 booleans, a null reason; 5 stage and 2 HTTP counters; 4 owner-control
# fields. An empty, partial, renamed, extra, coerced or string-typed value is a refusal, never an idle proof.
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
    if ($obs -isnot [Management.Automation.PSCustomObject] -or $obs.PSObject.Properties.Name -cnotcontains 'schema' -or $obs.schema -isnot [string] -or $obs.schema -cne 'mineru.capacity-observation.v1') { throw ($Label + ': capacity observation schema is not mineru.capacity-observation.v1') }
    if ($obs.PSObject.Properties.Name -cnotcontains 'capacity_config_sha256' -or $obs.capacity_config_sha256 -isnot [string] -or $obs.capacity_config_sha256 -cne $ExpectedCapacitySha256) { throw ($Label + ': API capacity identity differs from the expected value') }
    foreach ($n in @('stage_counters','http_counters','owner_control')) { if ($obs.PSObject.Properties.Name -cnotcontains $n) { throw ($Label + ': capacity observation lacks ' + $n) } }
    $zeroCounters=@('ingress_tasks','accepted_pending_tasks','accepted_processing_tasks','accepted_finalizing_tasks','durable_nonterminal_tasks','routeless_accepted_tasks','ingress_cleanup_tasks','unowned_ingress_tasks','scheduled_tasks','queue_depth','active_processors')
    Assert-Closed $adm (@('schema','registry_schema','nonterminal_limit','recovery_overcommitted','admission_open','blocked_reason') + $zeroCounters) ($Label + ': task_admission')
    Assert-Closed $obs.stage_counters @('result_capacity_waiting','parse_waiting','parse_active','finalizer_waiting','finalizer_active') ($Label + ': stage_counters')
    Assert-Closed $obs.http_counters @('active_requests','pending_requests') ($Label + ': http_counters')
    Assert-Closed $obs.owner_control @('foreign_loop_observed','soft_drain_requested','soft_drain_applied','trigger') ($Label + ': owner_control')
    if ($adm.schema -isnot [string] -or $adm.schema -cne 'mineru-task-admission.v1' -or $adm.registry_schema -isnot [string] -or $adm.registry_schema -cne 'mineru-task-registry.v3') { throw ($Label + ': task_admission schema tags differ from the serving contract') }
    $limit=$adm.nonterminal_limit
    if (($limit -isnot [int] -and $limit -isnot [long]) -or [long]$limit -lt 1 -or [long]$limit -gt 128) { throw ($Label + ': task_admission.nonterminal_limit is not a positive integer') }
    foreach ($flag in @('recovery_overcommitted','admission_open')) { if ($adm.$flag -isnot [bool]) { throw ($Label + ': task_admission.' + $flag + ' is not a boolean') } }
    foreach ($flag in @('foreign_loop_observed','soft_drain_requested','soft_drain_applied')) { if ($obs.owner_control.$flag -isnot [bool]) { throw ($Label + ': owner_control.' + $flag + ' is not a boolean') } }
    $busy=@()
    foreach ($n in $zeroCounters) { $v=$adm.$n; if ($v -isnot [int] -and $v -isnot [long]) { throw ($Label + ': task_admission.' + $n + ' is not an integer') }; if ([long]$v -ne 0) { $busy+=('task_admission.' + $n) } }
    foreach ($pr in $obs.stage_counters.PSObject.Properties) { if ($pr.Value -isnot [int] -and $pr.Value -isnot [long]) { throw ($Label + ': stage_counters.' + $pr.Name + ' is not an integer') }; if ([long]$pr.Value -ne 0) { $busy+=('stage_counters.' + $pr.Name) } }
    foreach ($pr in $obs.http_counters.PSObject.Properties) { if ($pr.Value -isnot [int] -and $pr.Value -isnot [long]) { throw ($Label + ': http_counters.' + $pr.Name + ' is not an integer') }; if ([long]$pr.Value -ne 0) { $busy+=('http_counters.' + $pr.Name) } }
    if ($adm.recovery_overcommitted) { $busy+='task_admission.recovery_overcommitted' }
    if (-not $adm.admission_open) { $busy+='task_admission.admission_open=false' }
    if ($null -ne $adm.blocked_reason) { $busy+=('task_admission.blocked_reason=' + [string]$adm.blocked_reason) }
    foreach ($flag in @('foreign_loop_observed','soft_drain_requested','soft_drain_applied')) { if ($obs.owner_control.$flag) { $busy+=('owner_control.' + $flag) } }
    if ($null -ne $obs.owner_control.trigger) { $busy+=('owner_control.trigger=' + [string]$obs.owner_control.trigger) }
    if ($busy.Count -gt 0) { throw ($Label + ': API is not idle: ' + ($busy -join ',')) }
    return $h
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
    Assert-Closed $binding @('contract_version','expected_hostname','api_device_profile','expected_active_compose_sha256','expected_previous_capacity_sha256','job_lifetime_milliseconds','job_cleanup_milliseconds','mac_exclusivity_receipt_sha256') 'installation binding'
    if ($binding.contract_version -cne 'm6.installation-binding.v1') { Fail 64 'installation binding contract is unsupported' }
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
    Assert-Closed $manifest @('contract_version','built_at_utc','source','inputs','projection','api_build','native_m6','installation','files') 'release manifest'
    if ($manifest.contract_version -cne 'm6.release.v1') { Fail 65 'release manifest contract is unsupported' }
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
    if ($verified['windows/install_mineru_fixed_api.ps1'] -cne [string]$manifest.installation.installer_sha256 -or
        $verified['windows/collect_mineru_runtime.ps1'] -cne [string]$manifest.installation.collector_sha256 -or
        $verified['compose/mineru-windows.compose.yaml'] -cne [string]$manifest.projection.compose_sha256 -or
        $verified['api-context/capacity-config.json'] -cne [string]$manifest.api_build.capacity_config_sha256) { Fail 65 'release manifest identities disagree with its files' }
    $capacitySha=[string]$manifest.api_build.capacity_config_sha256
    $composeSha=[string]$manifest.projection.compose_sha256

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
    if ($null -ne $binding.expected_active_compose_sha256) {
        if (-not (Test-Path -LiteralPath $ComposeTarget -PathType Leaf)) { Fail 65 'expected active compose is absent' }
        $active=Get-Sha256File $ComposeTarget
        if ($active -cne [string]$binding.expected_active_compose_sha256) { Fail 65 ('active compose differs from the installation binding: ' + $active) }
    }
    if ($null -ne $binding.expected_previous_capacity_sha256) {
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
    # A release profile declares the desired device, not always a device transition.
    # A changed explicit capacity uses the existing capacity-only comparison, which
    # retains device and all non-capacity fields. Mixed device/capacity edits still fail.
    if ($null -eq $binding.expected_previous_capacity_sha256 -or
        [string]$binding.expected_previous_capacity_sha256 -ceq $capacitySha) {
        $installerParameters['ApiDeviceProfile']=[string]$binding.api_device_profile
    }
    $installerArguments=@()
    $tokens=@()
    foreach ($entry in $installerParameters.GetEnumerator()) {
        if ($entry.Key -cnotmatch '^[A-Za-z][A-Za-z0-9]*$') { Fail 64 ('installer parameter name is invalid: ' + $entry.Key) }
        if ($null -eq $entry.Value) { $tokens+=('-' + $entry.Key); $installerArguments+=('-' + $entry.Key); continue }
        $value=[string]$entry.Value
        if ($value -match '[\r\n\0]') { Fail 64 ('installer parameter value contains a forbidden character: ' + $entry.Key) }
        $tokens+=('-' + $entry.Key + " '" + ($value -replace "'","''") + "'")
        $installerArguments+=('-' + $entry.Key); $installerArguments+=$value
    }
    $childCommand="& '" + ($installer -replace "'","''") + "' " + ($tokens -join ' ') + " *>> '" + ($consolePath -replace "'","''") + "'; exit `$LASTEXITCODE"
    $powershell=Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
    $childArguments=[string[]]@('-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-Command',$childCommand)
    Write-NewRecord 'operation-start.json' ([ordered]@{contract_version='m6.installation-operation-start.v1';hostname=[Environment]::MachineName;pid=$PID
        release_root=$ReleaseRoot;release_manifest_sha256=$manifestSha;capacity_config_sha256=$capacitySha;compose_sha256=$composeSha
        installation_binding_sha256=(Get-Sha256File $InstallationBinding);mac_exclusivity_receipt_sha256=$binding.mac_exclusivity_receipt_sha256
        supervisor_manifest_sha256=$prepared.manifest_sha256;job_lifetime_milliseconds=$lifetime;job_cleanup_milliseconds=$cleanup
        installer_arguments=$installerArguments;started_utc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json -Compress -Depth 6)
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

    # ---- judge from records and real readback, never from the child's exit alone ----------------------
    $resultPath=Join-Path $recordsDir 'installer-result.json'
    if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf)) {
        $status=$(if ($installerExit -eq 0) { 'unknown' } else { 'failed' })
        Fail 70 ('installer left no result record (child exit ' + $installerExit + ')')
    }
    $installerResult=ConvertFrom-StrictJson (Read-BoundedFile $resultPath 4194304)
    if ([string]$installerResult.status -cne 'pass') {
        $status=$(if ([string]$installerResult.status -ceq 'unknown') { 'unknown' } else { 'failed' })
        Fail 70 ('installer reported ' + [string]$installerResult.status + ': ' + [string]$installerResult.first_error)
    }
    if ($installerExit -ne 0) { $status='unknown'; Fail 70 ('installer result is pass but child exit is ' + $installerExit) }
    if (-not (Test-Path -LiteralPath $ReceiptTarget -PathType Leaf)) { $status='unknown'; Fail 70 'install receipt target is absent after a pass result' }
    $receipt=ConvertFrom-StrictJson (Read-BoundedFile $ReceiptTarget 4194304)
    if ([string]$receipt.schema -cne 'mineru-windows-install-receipt.v2' -or -not [bool]$receipt.success -or
        [string]$receipt.compose_sha256 -cne $composeSha -or [string]$receipt.compose_sha256 -cne [string]$installerResult.compose_sha256) {
        $status='unknown'; Fail 70 'persisted install receipt does not bind the release compose'
    }
    $activeCompose=Get-Sha256File $ComposeTarget
    if ($activeCompose -cne $composeSha) { $status='unknown'; Fail 70 ('active compose after installation differs from the release: ' + $activeCompose) }
    $inspect=Invoke-BoundedReadback $docker @('inspect','--format','{{.Image}}','mineru-api')
    if ($inspect.ExitCode -ne 0) { $status='unknown'; Fail 70 'docker inspect of the API container failed after installation' }
    $imageId=$inspect.StandardOutput.Trim()
    if ($imageId -cne [string]$receipt.api_compatibility_image.image_id) { $status='unknown'; Fail 70 ('running API image differs from the receipt: ' + $imageId) }
    try { $null=Read-IdleCapacityHealth $docker $capacitySha 'post-installation' } catch { $status='unknown'; Fail 70 $_.Exception.Message }
    # Installation is verified; writer readiness stays closed until this image is qualified and bound.
    $daemonSide='verified'; $status='pass'; $installationVerified=$true; $ExitCode=0
} catch {
    if ($null -eq $firstError) { $firstError=$_.Exception.Message }
    if ($ExitCode -eq 0) { $ExitCode=70 }
} finally {
    $result=[ordered]@{
        contract_version='m6.installation-operation.v1'; status=$status; first_error=$firstError; phase=$phase
        daemon_side_outcome=$daemonSide; write_permission=$writePermission; installation_verified=$installationVerified
        next_required=$(if ($installationVerified) { @('qualify','bind') } else { @('read_only_recovery_judgement') }); idle_proof_before=$idleProofBefore; exit_code=$ExitCode
        hostname=[Environment]::MachineName; release_root=$ReleaseRoot; release_manifest_sha256=$manifestSha
        installer_child_exit_code=$installerExit; job_accounting=$accounting; installer_result_status=$(if ($null -ne $installerResult) { [string]$installerResult.status } else { $null })
        compose_sha256=$(if ($null -ne $manifestSha) { $composeSha } else { $null }); elapsed_milliseconds=$clock.ElapsedMilliseconds
        finished_utc=[DateTime]::UtcNow.ToString('o')
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
