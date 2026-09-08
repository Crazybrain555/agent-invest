param(
    [Parameter(Mandatory = $true)][string]$ConfigJsonPath,
    [Parameter(Mandatory = $true)][string]$ExpectedConfigSha256,
    [Parameter(Mandatory = $true)][ValidateSet('ready','closed')][string]$Phase,
    [string]$ExpectedContainerId = ''
)
# Read-only outer-owner evidence. Never a collector, launcher, sampler or lease
# renewer. The caller independently pins this script and bounds its SSH command.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$pins = [Collections.Generic.List[IO.FileStream]]::new()
function Get-MineruBootstrapSha([byte[]]$Bytes) {
    $hash=[Security.Cryptography.SHA256]::Create()
    try{return 'sha256:'+([BitConverter]::ToString($hash.ComputeHash($Bytes))).Replace('-','').ToLowerInvariant()}finally{$hash.Dispose()}
}
function Read-ControlBytes([string]$Path,[int]$Maximum) {
    if(-not [IO.Path]::IsPathRooted($Path)){throw 'absolute control path required'}
    $pin=[IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $pins.Add($pin)
    if($pin.Length -lt 1 -or $pin.Length -gt $Maximum){throw 'control artifact byte bound'}
    $bytes=[byte[]]::new([int]$pin.Length);$offset=0
    while($offset -lt $bytes.Length){$n=$pin.Read($bytes,$offset,$bytes.Length-$offset);if($n -le 0){throw 'control artifact truncated'};$offset+=$n}
    return ,$bytes
}
function Read-ControlJson([string]$Name) {
    if($Name -cnotmatch '\A[a-z-]+\.json\z'){throw 'fixed control artifact name required'}
    $raw=[Text.UTF8Encoding]::new($false,$true).GetString((Read-ControlBytes ([IO.Path]::Combine($state.RunDirectory,$Name)) 65536))
    $null=[MineruResidentWire]::Parse($raw,65536)
    return $raw
}
function Observe-ControlProcess([string]$Role,[int]$ProcessId,[long]$Creation) {
    $process=$null
    try{$process=[Diagnostics.Process]::GetProcessById($ProcessId)}catch [ArgumentException]{
        return [ordered]@{role=$Role;pid=$ProcessId;expected_creation_filetime_100ns=$Creation;actual_creation_filetime_100ns=$null;state='absent'}
    }
    try{
        $actual=$process.StartTime.ToUniversalTime().ToFileTimeUtc()
        return [ordered]@{role=$Role;pid=$ProcessId;expected_creation_filetime_100ns=$Creation;actual_creation_filetime_100ns=$actual;state=$(if($actual -eq $Creation){'same-process'}else{'different-birth'})}
    }finally{$process.Dispose()}
}
try {
    $configBytes=Read-ControlBytes $ConfigJsonPath 32768
    if((Get-MineruBootstrapSha $configBytes) -cne $ExpectedConfigSha256){throw 'control config hash mismatch'}
    $bootstrapConfig=[Text.UTF8Encoding]::new($false,$true).GetString($configBytes)|ConvertFrom-Json
    $common=[IO.Path]::Combine($PSScriptRoot,'load_mineru_resident_session.ps1')
    if((Get-MineruBootstrapSha (Read-ControlBytes $common 65536)) -cne $bootstrapConfig.sources.'load_mineru_resident_session.ps1'){throw 'control common source hash mismatch'}
    . $common
    $state=Initialize-MineruResidentSession
    if($Phase -ceq 'ready') {
        $readyPath=[IO.Path]::Combine($state.RunDirectory,'ready.json')
        $wait=[Diagnostics.Stopwatch]::StartNew()
        while(-not [IO.File]::Exists($readyPath)) {
            if($wait.ElapsedMilliseconds -ge 10000){throw 'control READY artifact deadline'}
            Start-Sleep -Milliseconds 50
        }
    }
    $readyRaw=Read-ControlJson 'ready.json';$ready=$readyRaw|ConvertFrom-Json
    $startedRaw=Read-ControlJson 'supervisor-started.json';$started=$startedRaw|ConvertFrom-Json
    $processes=@(
        (Observe-ControlProcess 'supervisor' $started.supervisor_process.pid $started.supervisor_process.creation_filetime_100ns),
        (Observe-ControlProcess 'exporter' $ready.process.pid $ready.process.creation_filetime_100ns)
    )
    if($state.Lane -ceq 'host_slow'){$processes+=Observe-ControlProcess 'docker' $ready.backend.docker_pid $ready.backend.docker_creation_filetime_100ns}
    $container=$null;$absence=$null;$closedRaw=$null;$jobRaw=$null;$linuxRaw=$null
    if($Phase -ceq 'ready') {
        if($ExpectedContainerId.Length -ne 0){throw 'ready must resolve its actual container ID'}
        if(@($processes|Where-Object {$_.state -cne 'same-process'}).Count -ne 0){throw 'READY processes are not actually alive with the expected births'}
    }else{
        $closedRaw=Read-ControlJson 'closed.json';$jobRaw=Read-ControlJson 'job-accounting.json'
        if(@($processes|Where-Object {$_.state -ceq 'same-process'}).Count -ne 0){throw 'closed session still has a live owned process'}
        if($state.Lane -ceq 'host_slow'){$linuxRaw=Read-ControlJson 'linux-closed.json'}
    }
    if($state.Lane -ceq 'host_slow') {
        $docker=$bootstrapConfig.backend.docker_path;$dockerSha=$bootstrapConfig.backend.docker_sha256
        if($Phase -ceq 'ready') {
            $name='m6-resident-'+$state.Session
            $r=[MineruDiagnosticProcess]::Run($docker,$dockerSha,[string[]]@('--host','npipe:////./pipe/dockerDesktopLinuxEngine','inspect',$name),5000,65536)
            if($r.ExitCode -ne 0 -or $r.StandardError.Length -ne 0){throw 'live helper inspect failed'}
            $rows=$r.StandardOutput|ConvertFrom-Json
            if($rows.Count -ne 1){throw 'live helper inspect row count'}
            $c=$rows[0]
            # Deliberately omit Env, labels, mounts' source paths and raw Docker
            # internals. The owner checks this closed, content-free projection.
            $container=[ordered]@{id=$c.Id;name=$c.Name;image=$c.Image;running=$c.State.Running;pid=$c.State.Pid;started_at=$c.State.StartedAt;pid_mode=$c.HostConfig.PidMode;cgroup_mode=$c.HostConfig.CgroupnsMode;network=$c.HostConfig.NetworkMode;read_only=$c.HostConfig.ReadonlyRootfs;auto_remove=$c.HostConfig.AutoRemove;privileged=$c.HostConfig.Privileged;cap_add=$c.HostConfig.CapAdd;cap_drop=$c.HostConfig.CapDrop;security_opt=$c.HostConfig.SecurityOpt;mount_count=@($c.Mounts).Count;entrypoint=$c.Config.Entrypoint}
        }else{
            if($ExpectedContainerId -cnotmatch '\A[0-9a-f]{64}\z'){throw 'exact previously observed container ID required'}
            $r=[MineruDiagnosticProcess]::Run($docker,$dockerSha,[string[]]@('--host','npipe:////./pipe/dockerDesktopLinuxEngine','inspect',$ExpectedContainerId),5000,65536)
            $absence=[ordered]@{id=$ExpectedContainerId;exit_code=$r.ExitCode;stdout=$r.StandardOutput;stderr=$r.StandardError}
            if($r.ExitCode -ne 1 -or $r.StandardOutput.Trim() -cne '[]' -or $r.StandardError.Trim() -cne ('error: no such object: '+$ExpectedContainerId)){throw 'exact helper absence not verified'}
        }
    }elseif($ExpectedContainerId.Length -ne 0){throw 'GPU lane has no Linux helper container'}
    $result=[ordered]@{contract_version='mineru.windows-resident-external-observation.v1';phase=$Phase;observed_at_utc=[DateTime]::UtcNow.ToString('o');windows_boot_utc=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o');config_sha256=$ExpectedConfigSha256;ready_raw=$readyRaw;started_raw=$startedRaw;processes=$processes;container=$container;container_absence=$absence;closed_raw=$closedRaw;job_raw=$jobRaw;linux_closed_raw=$linuxRaw}
    [Console]::Out.WriteLine(($result|ConvertTo-Json -Depth 8 -Compress))
} finally {
    foreach($pin in $pins){$pin.Dispose()}
}
