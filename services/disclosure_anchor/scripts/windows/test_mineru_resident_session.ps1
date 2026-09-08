param(
    [Parameter(Mandatory = $true)][string]$ConfigJsonPath,
    [Parameter(Mandatory = $true)][string]$ExpectedConfigSha256,
    [ValidateRange(0,240)][int]$SampleCount = 12
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# Explicit real-backend diagnostic owner, outside the measured Job tree. It
# requires an externally verified private config, sources, prepared manifest,
# host/runtime/profile, and a new private directory. Never called by installers.
$configPin = [IO.FileStream]::new($ConfigJsonPath,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
$process = $null; $started = $false; $prepared = $null
$failures = [Collections.Generic.List[Exception]]::new()
try {
    if ($configPin.Length -lt 1 -or $configPin.Length -gt 32768) { throw 'config byte bound' }
    $bytes = [byte[]]::new([int]$configPin.Length); $offset = 0
    while ($offset -lt $bytes.Length) { $n=$configPin.Read($bytes,$offset,$bytes.Length-$offset); if($n -le 0){throw 'config EOF'}; $offset += $n }
    $hash=[Security.Cryptography.SHA256]::Create()
    try { $actual='sha256:'+([BitConverter]::ToString($hash.ComputeHash($bytes))).Replace('-','').ToLowerInvariant() } finally { $hash.Dispose() }
    if ($actual -cne $ExpectedConfigSha256) { throw 'config hash mismatch' }
    $config = [Text.UTF8Encoding]::new($false,$true).GetString($bytes) | ConvertFrom-Json
    $loaderPath=[IO.Path]::Combine($config.source_directory,'load_mineru_telemetry_assembly.ps1')
    if(('sha256:'+(Get-FileHash -Algorithm SHA256 -LiteralPath $loaderPath).Hash.ToLowerInvariant()) -cne $config.sources.'load_mineru_telemetry_assembly.ps1'){throw 'diagnostic prepared loader drift'}
    . $loaderPath
    $prepared=Import-MineruTelemetryPreparedAssembly -ManifestPath $config.prepared.manifest_path -ExpectedManifestSha256 $config.prepared.manifest_sha256 -ExpectedNvmlSourceSha256 $config.prepared.nvml_source_sha256 -ExpectedWireSourceSha256 $config.prepared.wire_source_sha256 -ExpectedSupervisorSourceSha256 $config.prepared.supervisor_source_sha256
    $run = $config.run_directory
    foreach($name in @('ready.json','closed.json','job-accounting.json')) { if(Test-Path -LiteralPath ([IO.Path]::Combine($run,$name))){throw 'new-only diagnostic run required'} }
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = [IO.Path]::Combine($PSHOME,'powershell.exe')
    # All three args originate in the externally pinned config/known source.
    $scriptPath = [IO.Path]::Combine($config.source_directory,'start_mineru_resident_telemetry.ps1')
    foreach($value in @($scriptPath,$ConfigJsonPath,$ExpectedConfigSha256)) { if($value.Contains('"') -or $value.Contains([char]0) -or $value.EndsWith('\')){throw 'invalid diagnostic argv'} }
    $start.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'+$scriptPath+'" -ConfigJsonPath "'+$ConfigJsonPath+'" -ExpectedConfigSha256 "'+$ExpectedConfigSha256+'"'
    $start.UseShellExecute=$false; $start.CreateNoWindow=$true; $start.RedirectStandardOutput=$true; $start.RedirectStandardError=$true
    $process = [Diagnostics.Process]::new(); $process.StartInfo=$start
    if(-not $process.Start()){throw 'diagnostic supervisor start failed'}; $started=$true
    $supervisorPid=$process.Id; $supervisorCreation=$process.StartTime.ToUniversalTime().ToFileTimeUtc()
    $stdout=$process.StandardOutput.ReadToEndAsync(); $stderr=$process.StandardError.ReadToEndAsync()
    $timer=[Diagnostics.Stopwatch]::StartNew(); $readyPath=[IO.Path]::Combine($run,'ready.json')
    while(-not (Test-Path -LiteralPath $readyPath)) {
        if($process.WaitForExit(0)){throw ('supervisor exited before READY: '+$stderr.GetAwaiter().GetResult())}
        if($timer.ElapsedMilliseconds -gt 10000){throw 'diagnostic READY deadline'}
        [Threading.Thread]::Sleep(20)
    }
    $readyRaw=[IO.File]::ReadAllText($readyPath); $ready=$readyRaw|ConvertFrom-Json
    $child=[Diagnostics.Process]::GetProcessById($ready.process.pid)
    try { if($child.StartTime.ToUniversalTime().ToFileTimeUtc() -ne $ready.process.creation_filetime_100ns){throw 'READY does not match actual live process'} } finally { $child.Dispose() }
    $containerEvidence=$null; $containerAbsence=$null
    if($config.lane -ceq 'host_slow') {
        $docker=$config.backend.docker_path
        if(('sha256:'+(Get-FileHash -Algorithm SHA256 -LiteralPath $docker).Hash.ToLowerInvariant()) -cne $config.backend.docker_sha256){throw 'diagnostic Docker executable drift'}
        $containerName='m6-resident-'+$config.session
        if($ready.backend.container_name -cne $containerName){throw 'container name does not bind session'}
        $inspection=[MineruDiagnosticProcess]::Run($docker,$config.backend.docker_sha256,[string[]]@('--host','npipe:////./pipe/dockerDesktopLinuxEngine','inspect',$containerName),5000,65536)
        if($inspection.ExitCode -ne 0 -or $inspection.StandardError.Length -ne 0){throw 'exact running helper inspect failed'}
        $containers=$inspection.StandardOutput|ConvertFrom-Json
        if($containers.Count -ne 1){throw 'helper inspect count'}
        $container=$containers[0]
        $shape=[ordered]@{id=$container.Id;name=$container.Name;image=$container.Image;running=$container.State.Running;pid=$container.State.Pid;ready_pid=$ready.backend.linux_ready.identity.pid;pid_mode=$container.HostConfig.PidMode;cgroup_mode=$container.HostConfig.CgroupnsMode;network=$container.HostConfig.NetworkMode;read_only=$container.HostConfig.ReadonlyRootfs;auto_remove=$container.HostConfig.AutoRemove;privileged=$container.HostConfig.Privileged;cap_add=$container.HostConfig.CapAdd;cap_drop=$container.HostConfig.CapDrop;security_opt=$container.HostConfig.SecurityOpt;mount_count=@($container.Mounts).Count;entrypoint=$container.Config.Entrypoint}
        if($container.Id -cnotmatch '\A[0-9a-f]{64}\z' -or $container.Name -cne ('/'+$containerName) -or
           $container.Image -cne $config.backend.image_id -or -not $container.State.Running -or
           $container.State.Pid -ne $ready.backend.linux_ready.identity.pid -or
           $container.HostConfig.PidMode -cne 'host' -or $container.HostConfig.CgroupnsMode -cne 'host' -or
           $container.HostConfig.NetworkMode -cne 'none' -or -not $container.HostConfig.ReadonlyRootfs -or
           -not $container.HostConfig.AutoRemove -or $container.HostConfig.Privileged -or
           ($null -ne $container.HostConfig.CapAdd -and $container.HostConfig.CapAdd.Count -ne 0) -or @($container.Mounts).Count -ne 0 -or
           @($container.HostConfig.CapDrop).Count -ne 1 -or $container.HostConfig.CapDrop[0] -cne 'ALL' -or
           # Docker 29.6.1 generateSecurityOpt appends label=disable for host
           # PID mode. Require this observed exact set, not arbitrary extras.
           @($container.HostConfig.SecurityOpt).Count -ne 2 -or
           (@($container.HostConfig.SecurityOpt | Sort-Object) -join ',') -cne 'label=disable,no-new-privileges' -or
           @($container.Config.Entrypoint).Count -ne 1 -or $container.Config.Entrypoint[0] -cne '/usr/bin/python3.12') {throw ('actual helper runtime configuration mismatch: '+($shape|ConvertTo-Json -Depth 5 -Compress))}
        $containerEvidence=[ordered]@{id=$container.Id;name=$container.Name;image=$container.Image;pid=$container.State.Pid;started_at=$container.State.StartedAt;pid_mode=$container.HostConfig.PidMode;cgroup_mode=$container.HostConfig.CgroupnsMode;network=$container.HostConfig.NetworkMode;read_only=$container.HostConfig.ReadonlyRootfs;auto_remove=$container.HostConfig.AutoRemove;privileged=$container.HostConfig.Privileged;cap_drop=$container.HostConfig.CapDrop;security_opt=$container.HostConfig.SecurityOpt;mount_count=@($container.Mounts).Count;entrypoint=$container.Config.Entrypoint}
    }
    $path='/v1/'+$config.session+'/'+$config.lane
    function Get-DiagnosticHttp([string]$Suffix) {
        $request=[Net.HttpWebRequest]::Create('http://127.0.0.1:'+$config.port+$path+$Suffix)
        $request.Proxy=$null; $request.Timeout=2000; $request.ReadWriteTimeout=2000; $request.KeepAlive=$true
        $response=$request.GetResponse()
        try {
            if([int]$response.StatusCode -ne 200){throw 'diagnostic HTTP status'}
            $stream=$response.GetResponseStream(); $memory=[IO.MemoryStream]::new(); $buffer=[byte[]]::new(4096)
            try {
                while($true){$n=$stream.Read($buffer,0,[Math]::Min($buffer.Length,65537-[int]$memory.Length)); if($n -eq 0){break}; $memory.Write($buffer,0,$n); if($memory.Length -gt 65536){throw 'diagnostic HTTP byte bound'}}
                return [Text.UTF8Encoding]::new($false,$true).GetString($memory.ToArray())
            } finally { $memory.Dispose(); $stream.Dispose() }
        } finally { $response.Dispose() }
    }
    $sequence=[long]0; $frames=[Collections.Generic.List[string]]::new()
    for($index=0;$index -lt $SampleCount;$index++) {
        $raw=Get-DiagnosticHttp ('/after/'+[string]$sequence)
        $sample=$raw|ConvertFrom-Json
        if($sample.sequence -ne $sequence+1){throw 'diagnostic resident sequence gap'}
        $sequence=$sample.sequence; $frames.Add($raw)
    }
    $closeRaw=Get-DiagnosticHttp '/close'
    if(-not $process.WaitForExit(8000)){throw 'diagnostic normal exit deadline'}
    if(-not $stdout.Wait(1000) -or -not $stderr.Wait(1000)){throw 'diagnostic output EOF deadline'}
    if($process.ExitCode -ne 0 -or $stderr.Result.Length -ne 0){throw ('diagnostic child nonzero/error: '+$stderr.Result)}
    $closedRaw=[IO.File]::ReadAllText([IO.Path]::Combine($run,'closed.json'))
    if($closedRaw -cne $closeRaw){throw 'close response and durable artifact differ'}
    $jobRaw=[IO.File]::ReadAllText([IO.Path]::Combine($run,'job-accounting.json')); $receipt=$jobRaw|ConvertFrom-Json
    $job=$receipt.job
    if($job.child_pid -ne $ready.process.pid -or $job.child_creation_filetime_100ns -ne $ready.process.creation_filetime_100ns -or
       $job.supervisor_pid -ne $supervisorPid -or $job.supervisor_creation_filetime_100ns -ne $supervisorCreation -or
       $job.child_exit_code -ne 0 -or $job.forced_termination -or $job.job_active_processes -ne 0){throw 'Job/live READY identity or quiescence mismatch'}
    $linuxClosedRaw=$null
    if($null -ne $containerEvidence) {
        $absence=[MineruDiagnosticProcess]::Run($docker,$config.backend.docker_sha256,[string[]]@('--host','npipe:////./pipe/dockerDesktopLinuxEngine','inspect',$containerEvidence.id),5000,65536)
        $absenceCode=$absence.ExitCode
        $absenceText=($absence.StandardOutput+$absence.StandardError).Trim()
        if($absenceCode -ne 1 -or $absence.StandardOutput.Trim() -cne '[]' -or $absence.StandardError.Trim() -cne ('error: no such object: '+$containerEvidence.id)){throw ('exact helper absence not verified: '+$absenceText)}
        $containerAbsence=[ordered]@{id=$containerEvidence.id;exit_code=$absenceCode;raw_output=$absenceText}
        $linuxClosedRaw=[IO.File]::ReadAllText([IO.Path]::Combine($run,'linux-closed.json'))
    }
    $diagnosticJson = [ordered]@{contract_version='mineru.resident-session-diagnostic.v1';config_sha256=$ExpectedConfigSha256;ready_raw=$readyRaw;frames_raw=$frames.ToArray();closed_raw=$closedRaw;job_raw=$jobRaw;linux_closed_raw=$linuxClosedRaw;container=$containerEvidence;container_absence=$containerAbsence;supervisor_exit_code=$process.ExitCode;sample_count=$frames.Count;qualification='mechanism-only; external identity, canonical replay, combined CPU and full hour are separate gates'} | ConvertTo-Json -Depth 8 -Compress
    # Preserve exact raw frames independently of terminal output truncation or
    # stdout/stderr interleaving. Diagnostic-only, outside the measured Job.
    $diagnosticBytes=[Text.UTF8Encoding]::new($false,$true).GetBytes($diagnosticJson)
    if($diagnosticBytes.Length -gt 16777216){throw 'diagnostic report byte bound'}
    $pending=[IO.Path]::Combine($run,('diagnostic.json.pending-'+[Guid]::NewGuid().ToString('N')))
    $report=[IO.FileStream]::new($pending,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try{$report.Write($diagnosticBytes,0,$diagnosticBytes.Length);$report.Flush($true)}finally{$report.Dispose()}
    [IO.File]::Move($pending,[IO.Path]::Combine($run,'diagnostic.json'))
    [Console]::Out.WriteLine($diagnosticJson)
} catch {
    $failures.Add($_.Exception)
    [Console]::Error.WriteLine($_.Exception.ToString())
    if ($started -and $stderr.IsCompleted) { [Console]::Error.WriteLine($stderr.GetAwaiter().GetResult()) }
}
finally {
    if($started) {
        try { if(-not $process.WaitForExit(0)){$process.Kill();if(-not $process.WaitForExit(3000)){throw 'diagnostic supervisor failed to exit after kill'}} } catch { $failures.Add($_.Exception) }
    }
    if($null -ne $process){try{$process.Dispose()}catch{$failures.Add($_.Exception)}}
    try{$configPin.Dispose()}catch{$failures.Add($_.Exception)}
    if($null -ne $prepared){foreach($pin in $prepared.Pins){try{$pin.Dispose()}catch{$failures.Add($_.Exception)}}}
}
if($failures.Count -gt 0){throw [AggregateException]::new('resident session diagnostic failed',$failures)}
