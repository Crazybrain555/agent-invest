<#
.SYNOPSIS
Job child of the same-root registry maintenance owner: one phase per run.

.DESCRIPTION
Started only by run_mineru_installation.ps1 with an m6.installation-binding.v3 binding, inside its finite
Windows Job and under its installation lock. This child owns every mutation of the maintenance: the
persistent operation state beside the install receipt, the three business containers' restart policies and
run state, and the one-shot Linux helper that observes or re-attests the native task registry. The wrapper
only reads and judges.

The operation is resumable and never silent: every record is create-new and flushed, nothing is deleted to
retry, a Docker timeout is resolved only by reading the same object back, and an outcome that cannot be read
back stays "unknown" with the pending marker in place. A record pair interrupted after its first file is
completed only from byte- or object-identical evidence, or left as evidence under a number never reused.
Restart policies and containers are restored only by the explicit restore/close phases; close records its
transition before its first mutation and from then on is the only phase that continues the operation.
Exit codes: 0 pass; 70 failed or unknown (see same-root-result.json).
#>
param(
    [Parameter(Mandatory=$true)][ValidateSet('prestop','observe','apply','restore','close','status')][string]$Phase,
    [Parameter(Mandatory=$true)][string]$OperationId,
    [Parameter(Mandatory=$true)][string]$StateRoot,
    [Parameter(Mandatory=$true)][string]$OutputRoot,
    [Parameter(Mandatory=$true)][string]$ReleaseRoot,
    [Parameter(Mandatory=$true)][string]$RecordDirectory,
    [Parameter(Mandatory=$true)][string]$ReleaseManifestSha256,
    [Parameter(Mandatory=$true)][string]$ApiImageId,
    [Parameter(Mandatory=$true)][string]$HelperSourceSha256,
    [Parameter(Mandatory=$true)][string]$ProtocolSourceSha256,
    [Parameter(Mandatory=$true)][string]$IdentitySourceSha256,
    [string]$ProofPath='',
    [string]$ProofSha256='',
    [string]$ApprovalPath='',
    [string]$ApprovalSha256='',
    [ValidateRange(60,86400)][int]$OperationBudgetSeconds=1140
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
$ProgressPreference='SilentlyContinue'
$utf8=[Text.UTF8Encoding]::new($false,$true)
$script:Clock=[Diagnostics.Stopwatch]::StartNew()
$script:BudgetMilliseconds=[long]$OperationBudgetSeconds * 1000
$script:Uncertain=$false
$script:Facts=[ordered]@{}
$script:Outcome=$null
$script:OpDir=$null
$script:PendingPath=$null
$status='failed'; $firstError=$null; $exitCode=70
# Start order; stopping runs in reverse (proxy, API, inference).
$ContainerNames=@('mineru-openai-server','mineru-api','mineru-api-proxy')
$ApiOutputTarget='/var/lib/mineru-api-output'
$HelperMount='/opt/mineru-same-root'
$HelperDeadlineSeconds=120
$MaximumRegistryBytes=16777216
$IdentityFields=@('final_path','volume_path','file_system','volume_serial','file_id','is_directory','sddl')

function Get-Sha256Bytes([byte[]]$Bytes) { $a=[Security.Cryptography.SHA256]::Create(); try { return 'sha256:' + ([BitConverter]::ToString($a.ComputeHash($Bytes))).Replace('-','').ToLowerInvariant() } finally { $a.Dispose() } }
function Read-Bounded([string]$Path,[long]$MaximumBytes) {
    $file=[IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    try {
        if ($file.Length -lt 1 -or $file.Length -gt $MaximumBytes) { throw ('file byte bound exceeded: ' + $Path) }
        $bytes=[byte[]]::new([int]$file.Length); $offset=0
        while ($offset -lt $bytes.Length) { $read=$file.Read($bytes,$offset,$bytes.Length - $offset); if ($read -le 0) { throw ('unexpected EOF: ' + $Path) }; $offset+=$read }
        return ,$bytes
    } finally { $file.Dispose() }
}
function Write-NewBytes([string]$Path,[byte[]]$Bytes) {
    $file=[IO.FileStream]::new($Path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try { $file.Write($Bytes,0,$Bytes.Length); $file.Flush($true) } finally { $file.Dispose() }
}
function ConvertFrom-StrictJson([byte[]]$Bytes) { return ($utf8.GetString($Bytes) | ConvertFrom-Json) }
function ConvertTo-RecordBytes($Value) { return $utf8.GetBytes(($Value | ConvertTo-Json -Compress -Depth 12)) }
function Get-Optional($Object,[string]$Name) {
    if ($null -eq $Object) { return $null }
    $property=$Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}
function Assert-Closed($Value,[string[]]$Names,[string]$Label) {
    if ($null -eq $Value -or $Value.GetType().FullName -cne 'System.Management.Automation.PSCustomObject' -or
        (@($Value.PSObject.Properties.Name | Sort-Object) -join ',') -cne (@($Names | Sort-Object) -join ',')) { throw ($Label + ' fields are not closed') }
}
# Persistent operation records: create-new, flushed, never rewritten.
function Write-StateRecord([string]$Name,$Value) {
    $bytes=ConvertTo-RecordBytes $Value
    Write-NewBytes (Join-Path $script:OpDir $Name) $bytes
    return (Get-Sha256Bytes $bytes)
}
function Read-StateBytes([string]$Name) {
    $path=Join-Path $script:OpDir $Name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    return ,(Read-Bounded $path $MaximumRegistryBytes)
}
function Read-StateRecord([string]$Name) {
    $bytes=Read-StateBytes $Name
    if ($null -eq $bytes) { return $null }
    return (ConvertFrom-StrictJson $bytes)
}
function Get-NumberedRecords([string]$Prefix) {
    $items=@()
    foreach ($file in @(Get-ChildItem -LiteralPath $script:OpDir -File)) {
        if ($file.Name -cmatch ('\A' + [regex]::Escape($Prefix) + '-([0-9]{1,6})\.json\z')) {
            $number=[int]$Matches[1]
            $bytes=Read-Bounded $file.FullName $MaximumRegistryBytes
            $items+=[pscustomobject]@{ Number=$number; Name=$file.Name; Sha256=(Get-Sha256Bytes $bytes); Value=(ConvertFrom-StrictJson $bytes) }
        }
    }
    return @($items | Sort-Object Number)
}
function Get-Latest([string]$Prefix) { $items=@(Get-NumberedRecords $Prefix); if ($items.Count -eq 0) { return $null }; return $items[$items.Count - 1] }

# Windows argv encoding identical to the tracked owner helpers.
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
# One bounded Docker CLI call. A deadline ends only this CLI process and is returned as TimedOut, because a
# request the daemon already accepted is not cancelled by it; the caller resolves it by reading the same object.
function Invoke-DockerBounded([string[]]$Arguments,[long]$TimeoutMilliseconds,[int]$MaximumBytes=1048576) {
    $left=$script:BudgetMilliseconds - $script:Clock.ElapsedMilliseconds - 5000
    if ($left -lt 1000) { throw ('same-root operation budget exhausted before docker ' + $Arguments[0]) }
    $deadline=[long][Math]::Min($TimeoutMilliseconds,$left)
    $start=[Diagnostics.ProcessStartInfo]::new($script:Docker)
    $start.Arguments=(@($Arguments | ForEach-Object { ConvertTo-WindowsCommandLineArgument $_ }) -join ' ')
    $start.UseShellExecute=$false; $start.CreateNoWindow=$true
    $start.RedirectStandardOutput=$true; $start.RedirectStandardError=$true; $start.RedirectStandardInput=$true
    $p=[Diagnostics.Process]::new(); $p.StartInfo=$start
    $out=[IO.MemoryStream]::new(); $err=[IO.MemoryStream]::new(); $buf=[byte[]]::new(16384); $ebuf=[byte[]]::new(16384)
    $sw=[Diagnostics.Stopwatch]::StartNew(); $timedOut=$false
    try {
        if (-not $p.Start()) { throw ('docker did not start for ' + $Arguments[0]) }
        $p.StandardInput.Close()
        $os=$p.StandardOutput.BaseStream; $es=$p.StandardError.BaseStream
        $ot=$os.ReadAsync($buf,0,$buf.Length); $et=$es.ReadAsync($ebuf,0,$ebuf.Length); $oeof=$false; $eeof=$false
        while (-not ($oeof -and $eeof)) {
            $remaining=$deadline - $sw.ElapsedMilliseconds
            if ($remaining -le 0) { $timedOut=$true; break }
            $pending=@(); if (-not $oeof) { $pending+=$ot }; if (-not $eeof) { $pending+=$et }
            $i=[Threading.Tasks.Task]::WaitAny([Threading.Tasks.Task[]]$pending,[int][Math]::Min($remaining,1000))
            if ($i -lt 0) { continue }
            $t=$pending[$i]; $n=$t.GetAwaiter().GetResult()
            if ([object]::ReferenceEquals($t,$ot)) { if ($n -eq 0) { $oeof=$true } else { $out.Write($buf,0,$n); $ot=$os.ReadAsync($buf,0,$buf.Length) } }
            else { if ($n -eq 0) { $eeof=$true } else { $err.Write($ebuf,0,$n); $et=$es.ReadAsync($ebuf,0,$ebuf.Length) } }
            if ($out.Length + $err.Length -gt $MaximumBytes) { throw ('docker output bound exceeded for ' + $Arguments[0]) }
        }
        if (-not $timedOut -and -not $p.WaitForExit([int][Math]::Max(1,$deadline - $sw.ElapsedMilliseconds))) { $timedOut=$true }
        if ($timedOut) {
            try { if (-not $p.HasExited) { $p.Kill() }; [void]$p.WaitForExit(10000) } catch { }
            return [pscustomobject]@{ ExitCode=$null; TimedOut=$true
                StandardOutput=[Text.Encoding]::UTF8.GetString($out.ToArray()); StandardError=[Text.Encoding]::UTF8.GetString($err.ToArray()) }
        }
        return [pscustomobject]@{ ExitCode=$p.ExitCode; TimedOut=$false
            StandardOutput=[Text.Encoding]::UTF8.GetString($out.ToArray()); StandardError=[Text.Encoding]::UTF8.GetString($err.ToArray()) }
    } catch {
        try { if (-not $p.HasExited) { $p.Kill(); [void]$p.WaitForExit(10000) } } catch { }
        throw
    } finally { $p.Dispose() }
}
function Get-DockerError($Result) {
    if ($Result.TimedOut) { return 'timed out' }
    return ('exit ' + $Result.ExitCode + ': ' + ([string]$Result.StandardError).Trim())
}

# ---- Windows physical identity (pinned-csc assembly, no Add-Type) -----------------------------------------
function Import-SameRootIdentityAssembly {
    $sourceBytes=Read-Bounded (Join-Path $script:Windows 'mineru_same_root_identity.cs') 65536
    if ((Get-Sha256Bytes $sourceBytes) -cne $IdentitySourceSha256) { throw 'identity source differs from the verified release' }
    $build=Join-Path $RecordDirectory 'identity-assembly'
    [void][IO.Directory]::CreateDirectory($build)
    # Compile exactly the verified bytes, not a path another writer could replace.
    $source=Join-Path $build 'mineru_same_root_identity.cs'
    Write-NewBytes $source $sourceBytes
    $assemblyPath=Join-Path $build 'mineru-same-root-identity.dll'
    $compiler=Join-Path ([Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()) 'csc.exe'
    $system=Join-Path ([IO.Path]::GetDirectoryName($compiler)) 'System.dll'
    $compilerSha=Get-Sha256Bytes (Read-Bounded $compiler 67108864)
    $arguments=@('/nologo','/noconfig','/target:library','/warnaserror+',('/out:' + $assemblyPath),('/reference:' + $system),$source)
    $start=[Diagnostics.ProcessStartInfo]::new($compiler)
    $start.Arguments=(@($arguments | ForEach-Object { if ($_.Contains('"')) { throw 'invalid compiler argument' }; '"' + $_ + '"' }) -join ' ')
    $start.UseShellExecute=$false; $start.CreateNoWindow=$true; $start.RedirectStandardOutput=$true; $start.RedirectStandardError=$true
    $compilerProcess=[Diagnostics.Process]::new(); $compilerProcess.StartInfo=$start
    try {
        if (-not $compilerProcess.Start()) { throw 'identity compiler did not start' }
        $output=$compilerProcess.StandardOutput.ReadToEndAsync(); $errors=$compilerProcess.StandardError.ReadToEndAsync()
        if (-not $compilerProcess.WaitForExit(60000)) { try { $compilerProcess.Kill() } catch { }; throw 'identity compiler deadline exceeded' }
        if (-not $output.Wait(5000) -or -not $errors.Wait(5000)) { throw 'identity compiler output deadline exceeded' }
        if ($compilerProcess.ExitCode -ne 0) { throw ('identity compiler failed: ' + $output.Result + $errors.Result) }
    } finally { $compilerProcess.Dispose() }
    $assemblyBytes=Read-Bounded $assemblyPath 1048576
    $assembly=[Reflection.Assembly]::Load($assemblyBytes)
    if ($null -eq $assembly.GetType('MineruSameRootIdentity',$false)) { throw 'identity type absent from the compiled assembly' }
    $script:Facts['identity_assembly']=[ordered]@{ compiler_path=$compiler; compiler_sha256=$compilerSha
        source_sha256=$IdentitySourceSha256; assembly_sha256=(Get-Sha256Bytes $assemblyBytes); compiler_arguments=$arguments }
}
function Get-WindowsIdentity {
    $control=Join-Path $script:OutputRootFull '.agent-task-protocol-v2'
    $spoolPath=Join-Path $script:OutputRootFull '.agent-ingress-spool'
    # Assign before wrapping: Windows PowerShell 5.1 ConvertFrom-Json emits a JSON array as one object.
    $parsed=[MineruSameRootIdentity]::Ancestors($script:OutputRootFull) | ConvertFrom-Json
    $ancestors=@($parsed)
    $root=[MineruSameRootIdentity]::Describe($script:OutputRootFull,$true) | ConvertFrom-Json
    $controlFacts=[MineruSameRootIdentity]::Describe($control,$true) | ConvertFrom-Json
    $spool=$null
    if (Test-Path -LiteralPath $spoolPath) { $spool=[MineruSameRootIdentity]::Describe($spoolPath,$true) | ConvertFrom-Json }
    $read=[MineruSameRootIdentity]::ReadRegular((Join-Path $control 'registry.json'),$MaximumRegistryBytes)
    $registry=$read.Facts | ConvertFrom-Json
    if ([string]$root.file_system -cne 'NTFS') { throw ('output root is not on NTFS: ' + [string]$root.file_system) }
    if ([long]$registry.link_count -ne 1) { throw 'registry file has more than one link' }
    $record=[ordered]@{ contract_version='mineru-same-root-windows-identity.v1'; host=[Environment]::MachineName
        captured_utc=[DateTime]::UtcNow.ToString('o'); ancestors=$ancestors; root=$root; control=$controlFacts; spool=$spool
        registry=$registry; registry_sha256=(Get-Sha256Bytes $read.Bytes); registry_bytes=$read.Bytes.Length }
    return [pscustomobject]@{ Record=$record; Bytes=$read.Bytes }
}
function Assert-SameObject($Expected,$Actual,[string]$Label) {
    if ($null -eq $Expected -or $null -eq $Actual) {
        if ($null -ne $Expected -or $null -ne $Actual) { throw ($Label + ' appeared or disappeared') }
        return
    }
    foreach ($name in $IdentityFields) {
        if ([string](Get-Optional $Expected $name) -cne [string](Get-Optional $Actual $name)) { throw ($Label + ' ' + $name + ' changed; the same physical root is not proved') }
    }
}
# Physical continuity: same host, plain ancestors, and the same root/control/spool objects (FileId, volume, ACL).
function Assert-SameRoot($Before,$Now) {
    if ([string]$Before.host -cne [string]$Now.host) { throw 'host changed during the maintenance' }
    $a=@($Before.ancestors); $b=@($Now.ancestors)
    if ($a.Count -ne $b.Count) { throw 'output root ancestor chain changed' }
    for ($i=0; $i -lt $a.Count; $i++) {
        if ([string]$a[$i].path -cne [string]$b[$i].path -or [string]$a[$i].file_id -cne [string]$b[$i].file_id) { throw ('output root ancestor changed: ' + [string]$b[$i].path) }
    }
    Assert-SameObject $Before.root $Now.root 'output root'
    Assert-SameObject $Before.control $Now.control 'task control directory'
    Assert-SameObject $Before.spool $Now.spool 'ingress spool directory'
}

# ---- business containers ---------------------------------------------------------------------------------
function Read-BusinessContainers {
    $r=Invoke-DockerBounded @('container','inspect','mineru-openai-server','mineru-api','mineru-api-proxy') 60000
    if ($r.TimedOut -or $r.ExitCode -ne 0) { throw ('business container inspect failed: ' + (Get-DockerError $r)) }
    $parsed=$r.StandardOutput | ConvertFrom-Json
    $all=@($parsed)
    $map=[ordered]@{}
    foreach ($name in $ContainerNames) {
        $found=@($all | Where-Object { [string]$_.Name -ceq ('/' + $name) })
        if ($found.Count -ne 1) { throw ('business container is not unique: ' + $name) }
        $map[$name]=$found[0]
    }
    return $map
}
function Read-ContainerById([string]$Id) {
    $r=Invoke-DockerBounded @('container','inspect',$Id) 60000
    if ($r.TimedOut -or $r.ExitCode -ne 0) { throw ('container inspect failed: ' + (Get-DockerError $r)) }
    $parsed=$r.StandardOutput | ConvertFrom-Json
    $items=@($parsed)
    if ($items.Count -ne 1 -or [string]$items[0].Id -cne $Id) { throw ('container identity readback differs: ' + $Id) }
    return $items[0]
}
function Get-RestartPolicyText($Container) {
    $policy=$Container.HostConfig.RestartPolicy
    $name=[string]$policy.Name; $count=[long]$policy.MaximumRetryCount
    if ($name -ceq '' -or $name -ceq 'no') { return 'no' }
    if ($name -ceq 'on-failure') { if ($count -gt 0) { return ('on-failure:' + $count) }; return 'on-failure' }
    if ($name -ceq 'always' -or $name -ceq 'unless-stopped') { return $name }
    throw ('unsupported restart policy: ' + $name)
}
function Get-HealthText($Container) {
    $health=Get-Optional $Container.State 'Health'
    if ($null -eq $health) { return 'none' }
    return [string]$health.Status
}
function Get-ApiOutputBind($Api) {
    $binds=@($Api.Mounts | Where-Object { [string]$_.Destination -ceq $ApiOutputTarget })
    if ($binds.Count -ne 1) { throw 'API output mount is not unique' }
    $bind=$binds[0]
    $source=[string]$bind.Source
    if ([string]$bind.Type -cne 'bind' -or -not [bool]$bind.RW) { throw 'API output mount is not a writable bind' }
    if ($source -cnotmatch '\A[A-Za-z]:[\\/][^,"]*\z') { throw ('API output bind source is not a plain Windows path: ' + $source) }
    if (-not [string]::Equals([IO.Path]::GetFullPath($source.Replace('/','\')).TrimEnd('\'),$script:OutputRootFull.TrimEnd('\'),[StringComparison]::OrdinalIgnoreCase)) {
        throw ('API output bind source differs from the maintained output root: ' + $source)
    }
    return [ordered]@{ source=$source; destination=$ApiOutputTarget; rw=$true }
}
function New-FrozenContainers($Live) {
    $items=@()
    foreach ($name in $ContainerNames) {
        $c=$Live[$name]
        if (-not [bool]$c.State.Running -or (Get-HealthText $c) -cne 'healthy' -or [long]$c.RestartCount -ne 0 -or [bool]$c.State.OOMKilled) {
            throw ('business container is not running healthy before maintenance: ' + $name)
        }
        if ([string]$c.Id -cnotmatch '\A[a-f0-9]{64}\z' -or [string]$c.Image -cnotmatch '\Asha256:[a-f0-9]{64}\z') { throw ('business container identity is invalid: ' + $name) }
        $items+=[ordered]@{ name=$name; id=[string]$c.Id; image_id=[string]$c.Image; restart_policy=(Get-RestartPolicyText $c); user=[string]$c.Config.User }
    }
    if ([string]$Live['mineru-api'].Image -cne $ApiImageId) { throw 'running API image differs from the install receipt' }
    return [ordered]@{ contract_version='mineru-same-root-containers.v1'; operation_id=$OperationId; captured_utc=[DateTime]::UtcNow.ToString('o')
        api_image_id=$ApiImageId; api_output_bind=(Get-ApiOutputBind $Live['mineru-api']); containers=$items }
}
# Same container objects throughout. Restart policies stay held at no until close. Only after close has
# recorded its transition (close-intent.json, passed as $Closing) may a policy already be back, and only on a
# container that record names by name and id, at exactly the policy it records; anything else is drift.
function Assert-FrozenContainers($Frozen,$Live,[bool]$Stopped,$Closing=$null) {
    foreach ($item in @($Frozen.containers)) {
        $c=$Live[[string]$item.name]
        if ([string]$c.Id -cne [string]$item.id -or [string]$c.Image -cne [string]$item.image_id) { throw ('business container was replaced during maintenance: ' + $item.name) }
        $policy=Get-RestartPolicyText $c
        if ($policy -cne 'no') {
            $restored=@()
            if ($null -ne $Closing) { $restored=@(@($Closing.containers) | Where-Object { [string]$_.name -ceq [string]$item.name -and [string]$_.id -ceq [string]$item.id }) }
            if ($restored.Count -ne 1 -or $policy -cne [string]$restored[0].restart_policy) {
                throw ('business container restart policy ' + $policy + ' is neither held nor restored by this operation''s recorded close: ' + $item.name)
            }
        }
        if ($Stopped -and [bool]$c.State.Running) { throw ('business container is running during maintenance: ' + $item.name) }
    }
}
function Set-BusinessRestartPolicy($Item,[string]$Policy) {
    $r=Invoke-DockerBounded @('update','--restart',$Policy,[string]$Item.id) 60000
    $actual=Get-RestartPolicyText (Read-ContainerById ([string]$Item.id))
    if ($actual -cne $Policy) {
        if ($r.TimedOut) { $script:Uncertain=$true }
        throw ('restart policy of ' + $Item.name + ' is ' + $actual + ', not ' + $Policy + ' (' + (Get-DockerError $r) + ')')
    }
}
function Stop-BusinessContainer($Item) {
    $r=Invoke-DockerBounded @('stop',[string]$Item.id) 180000
    $c=Read-ContainerById ([string]$Item.id)
    if ([bool]$c.State.Running) {
        if ($r.TimedOut) { $script:Uncertain=$true }
        throw ('business container did not stop: ' + $Item.name + ' (' + (Get-DockerError $r) + ')')
    }
}
function Start-BusinessContainer($Item,[long]$HealthyMilliseconds) {
    $started=$null
    if (-not [bool](Read-ContainerById ([string]$Item.id)).State.Running) {
        $started=Invoke-DockerBounded @('start',[string]$Item.id) 120000
    }
    $watch=[Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        $c=Read-ContainerById ([string]$Item.id)
        $health=Get-HealthText $c
        if ([bool]$c.State.Running -and $health -ceq 'healthy') { return }
        # Only a start whose acknowledgement was lost may still be in flight; anything else that is not
        # running has ended (restart policy no), e.g. a reader that refused the registry.
        if (-not [bool]$c.State.Running -and ($null -eq $started -or -not $started.TimedOut)) {
            throw ('business container is not running after start: ' + $Item.name + ' (' + [string]$c.State.Status + ', exit ' +
                [string]$c.State.ExitCode + $(if ($null -ne $started) { '; ' + (Get-DockerError $started) } else { '' }) + ')')
        }
        if ($health -ceq 'unhealthy') { throw ('business container is unhealthy after start: ' + $Item.name) }
        if ($watch.ElapsedMilliseconds -ge $HealthyMilliseconds) {
            if ($null -ne $started -and $started.TimedOut) { $script:Uncertain=$true }
            throw ('business container did not become healthy: ' + $Item.name + ' (' + $health + ')')
        }
        Start-Sleep -Milliseconds 2000
    }
}

# ---- one-shot helper ---------------------------------------------------------------------------------------
function Find-Helper([string]$Name) {
    $r=Invoke-DockerBounded @('container','inspect',$Name) 60000
    if ($r.TimedOut) { $script:Uncertain=$true; throw ('helper inspect timed out: ' + $Name) }
    if ($r.ExitCode -ne 0) {
        if ([string]$r.StandardError -match 'No such (container|object)') { return $null }
        throw ('helper inspect failed: ' + (Get-DockerError $r))
    }
    $parsed=$r.StandardOutput | ConvertFrom-Json
    $items=@($parsed)
    if ($items.Count -ne 1 -or [string]$items[0].Name -cne ('/' + $Name)) { throw ('helper inspect identity differs: ' + $Name) }
    return $items[0]
}
function Assert-HelperIdentity($Container,$Intent) {
    $labels=$Container.Config.Labels
    if ([string](Get-Optional $labels 'agent-invest.same-root.operation') -cne $OperationId -or
        [string](Get-Optional $labels 'agent-invest.same-root.action') -cne [string]$Intent.action -or
        [string](Get-Optional $labels 'agent-invest.same-root.sequence') -cne [string]$Intent.sequence -or
        [string]$Container.Image -cne [string]$Intent.image_id -or [string]$Container.HostConfig.NetworkMode -cne 'none' -or
        -not [bool]$Container.HostConfig.ReadonlyRootfs) { throw ('container named ' + $Intent.name + ' is not this operation''s helper') }
}
function Assert-NoOtherHelper {
    $r=Invoke-DockerBounded @('container','ls','--all','--filter','label=agent-invest.same-root.operation','--format','{{.Names}}|{{.State}}') 60000
    if ($r.TimedOut -or $r.ExitCode -ne 0) { throw ('same-root helper listing failed: ' + (Get-DockerError $r)) }
    foreach ($line in @(([string]$r.StandardOutput).Split([char[]]"`r`n",[StringSplitOptions]::RemoveEmptyEntries))) {
        $state=$line.Substring($line.LastIndexOf('|') + 1)
        if ($state -cne 'exited' -and $state -cne 'dead') { throw ('another same-root helper exists: ' + $line) }
    }
}
function Copy-VerifiedSource([string]$Relative,[string]$ExpectedSha256,[string]$Target) {
    $bytes=Read-Bounded (Join-Path $ReleaseRoot $Relative) 8388608
    if ((Get-Sha256Bytes $bytes) -cne $ExpectedSha256) { throw ('helper source differs from the verified release: ' + $Relative) }
    Write-NewBytes $Target $bytes
}
function New-HelperArguments([string]$Name,[string]$Action,[int]$Sequence,[string]$Directory,$Frozen) {
    if ($Directory -match '[,"]') { throw 'helper directory cannot be expressed as a bind mount' }
    $arguments=@('create','--name',$Name,
        '--label',('agent-invest.same-root.operation=' + $OperationId),
        '--label',('agent-invest.same-root.action=' + $Action),
        '--label',('agent-invest.same-root.sequence=' + $Sequence),
        '--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
        '--memory','536870912','--memory-swap','536870912','--cpus','1','--pids-limit','32','--restart','no',
        '--mount',('type=bind,source=' + [string]$Frozen.api_output_bind.source + ',target=' + $ApiOutputTarget + $(if ($Action -ceq 'observe') { ',readonly' } else { '' })),
        '--mount',('type=bind,source=' + $Directory + ',target=' + $HelperMount + ',readonly'),
        '--entrypoint','/usr/bin/python3.12')
    $api=@($Frozen.containers | Where-Object { [string]$_.name -ceq 'mineru-api' })[0]
    if ([string]$api.user -cne '') { $arguments+=@('--user',[string]$api.user) }
    $arguments+=@([string]$Frozen.api_image_id,'-I','-B',($HelperMount + '/mineru_same_root_registry_helper.py'),($HelperMount + '/request.json'))
    return $arguments
}
# Deterministic helper ownership: one intent record per sequence before docker create; a lost create/start
# acknowledgement is resolved only through the same container name; the result is persisted before rm.
# A helper directory without its intent record was left before any container could carry its name: that
# number is skipped, never reused, and the directory stays as evidence.
function Invoke-SameRootHelper([string]$Action,$Request) {
    $requestBytes=ConvertTo-RecordBytes $Request
    $requestSha=Get-Sha256Bytes $requestBytes
    $sequences=@(); $directories=@()
    foreach ($entry in @(Get-ChildItem -LiteralPath $script:OpDir)) {
        if ($entry.PSIsContainer) { if ($entry.Name -cmatch ('\Ahelper-' + $Action + '-([0-9]{1,4})\z')) { $directories+=[int]$Matches[1] } }
        elseif ($entry.Name -cmatch ('\Ahelper-' + $Action + '-([0-9]{1,4})-intent\.json\z')) { $sequences+=[int]$Matches[1] }
    }
    $last=0; foreach ($value in $sequences) { if ($value -gt $last) { $last=$value } }
    $resume=$last -gt 0 -and -not (Test-Path -LiteralPath (Join-Path $script:OpDir ('helper-' + $Action + '-' + $last + '-result.json')))
    $next=$last; foreach ($value in $directories) { if ($value -gt $next) { $next=$value } }
    $sequence=$(if ($resume) { $last } else { $next + 1 })
    $stem='helper-' + $Action + '-' + $sequence
    $name='mineru-same-root-' + $OperationId + '-' + $Action + '-' + $sequence
    if ($resume) {
        $intent=Read-StateRecord ($stem + '-intent.json')
        if ([string]$intent.request_sha256 -cne $requestSha) { throw ('pending helper ' + $name + ' was created for a different request; resolve it before a new decision') }
    } else {
        Assert-NoOtherHelper
        $directory=Join-Path $script:OpDir $stem
        if (Test-Path -LiteralPath $directory) { throw ('helper directory already exists: ' + $directory) }
        [void][IO.Directory]::CreateDirectory($directory)
        Copy-VerifiedSource 'windows\mineru_same_root_registry_helper.py' $HelperSourceSha256 (Join-Path $directory 'mineru_same_root_registry_helper.py')
        Copy-VerifiedSource 'api-context\agent_task_protocol_v2.py' $ProtocolSourceSha256 (Join-Path $directory 'agent_task_protocol_v2.py')
        Write-NewBytes (Join-Path $directory 'request.json') $requestBytes
        $frozen=Read-StateRecord 'containers.json'
        $intent=[ordered]@{ contract_version='mineru-same-root-helper-intent.v1'; operation_id=$OperationId; action=$Action; sequence=$sequence
            name=$name; image_id=[string]$frozen.api_image_id; request_sha256=$requestSha; helper_sha256=$HelperSourceSha256
            protocol_sha256=$ProtocolSourceSha256; arguments=(New-HelperArguments $name $Action $sequence $directory $frozen)
            created_utc=[DateTime]::UtcNow.ToString('o') }
        $null=Write-StateRecord ($stem + '-intent.json') $intent
        $intent=Read-StateRecord ($stem + '-intent.json')
    }
    $arguments=[string[]]@($intent.arguments)
    $container=Find-Helper $name
    if ($null -eq $container) {
        $created=Invoke-DockerBounded $arguments 120000
        $container=Find-Helper $name
        if ($null -eq $container -and $created.TimedOut) {
            # Name uniqueness: this create either makes the only helper or conflicts with the late one.
            $created=Invoke-DockerBounded $arguments 120000
            $container=Find-Helper $name
        }
        if ($null -eq $container) {
            if ($created.TimedOut) { $script:Uncertain=$true }
            throw ('helper was not created: ' + (Get-DockerError $created))
        }
    }
    Assert-HelperIdentity $container $intent
    if ([string]$container.State.Status -ceq 'created') {
        $startResult=Invoke-DockerBounded @('start',$name) 120000
        $container=Find-Helper $name
        if ($null -eq $container) { $script:Uncertain=$true; throw ('helper disappeared after start: ' + $name) }
        if ([string]$container.State.Status -ceq 'created') {
            if ($startResult.TimedOut) { $script:Uncertain=$true }
            throw ('helper did not start: ' + (Get-DockerError $startResult))
        }
    }
    if ([string]$container.State.Status -ceq 'running') {
        $waited=Invoke-DockerBounded @('wait',$name) (([long]$HelperDeadlineSeconds + 60) * 1000)
        if ($waited.TimedOut) { $script:Uncertain=$true; throw ('helper ' + $name + ' did not exit within its deadline; it is left for the next run') }
        $container=Find-Helper $name
        if ($null -eq $container) { $script:Uncertain=$true; throw ('helper disappeared before its result was read: ' + $name) }
    }
    if ([string]$container.State.Status -cne 'exited') { $script:Uncertain=$true; throw ('helper ' + $name + ' is ' + [string]$container.State.Status) }
    $logs=Invoke-DockerBounded @('logs',$name) 60000
    if ($logs.TimedOut -or $logs.ExitCode -ne 0) { $script:Uncertain=$true; throw ('helper output could not be read: ' + (Get-DockerError $logs)) }
    $lines=@(([string]$logs.StandardOutput).Split([char[]]"`r`n",[StringSplitOptions]::RemoveEmptyEntries))
    $exit=[int]$container.State.ExitCode; $oom=[bool]$container.State.OOMKilled
    $value=$null
    if ($lines.Count -eq 1) { try { $value=$lines[0] | ConvertFrom-Json } catch { $value=$null } }
    # Only a complete closed record of the requested action is a result; a partial line is not.
    $kind='unknown'
    if (-not $oom -and $null -ne $value -and $value.GetType().FullName -ceq 'System.Management.Automation.PSCustomObject') {
        $fields=@($value.PSObject.Properties.Name | Sort-Object) -join ','
        $schema=[string](Get-Optional $value 'schema'); $reported=[string](Get-Optional $value 'kind')
        $applyFields=@('schema','operation_id','outcome','registry_sha256','registry_bytes','registry_file','previous_registry_file','current_root','stale_temp_removed')
        $observeFields=@('schema','root','registry_schema','approved_root','current_root','registry_state','outcome','r0_sha256','r0_bytes','body_sha256',
            'r1_sha256','r1_bytes','record_count','submission_watermark_bucket','registry_file','control_directory','spool_directory')
        $failureFields=@('schema','action','operation_id','kind','phase','visible','error')
        if ($exit -eq 0 -and $Action -ceq 'apply' -and $schema -ceq 'mineru-same-root-apply.v1' -and $fields -ceq ((@($applyFields | Sort-Object)) -join ',') -and
            [string]$value.operation_id -ceq $OperationId -and [string]$value.outcome -cin @('applied','already_applied')) { $kind='pass' }
        elseif ($exit -eq 0 -and $Action -ceq 'observe' -and $schema -ceq 'mineru-same-root-observation.v1' -and $fields -ceq ((@($observeFields | Sort-Object)) -join ',') -and
            [string]$value.registry_state -cin @('r0','r1') -and [string]$value.outcome -cin @('noop','reattest')) { $kind='pass' }
        elseif ($schema -ceq 'mineru-same-root-helper-failure.v1' -and $fields -ceq ((@($failureFields | Sort-Object)) -join ',') -and
            (($exit -eq 64 -and $reported -ceq 'invalid') -or ($exit -eq 65 -and $reported -ceq 'refused' -and [string]$value.operation_id -ceq $OperationId))) { $kind=$reported }
    }
    # An observer has no write path; any other end of the writer is an unknown write.
    if ($kind -ceq 'unknown' -and $Action -ceq 'observe') { $kind='failed' }
    $null=Write-StateRecord ($stem + '-result.json') ([ordered]@{ contract_version='mineru-same-root-helper-result.v1'; operation_id=$OperationId
        action=$Action; sequence=$sequence; name=$name; container_id=[string]$container.Id; exit_code=$exit; oom_killed=$oom
        kind=$kind; stdout=[string]$logs.StandardOutput; stderr=([string]$logs.StandardError).Substring(0,[Math]::Min(65536,([string]$logs.StandardError).Length))
        finished_at=[string]$container.State.FinishedAt; recorded_utc=[DateTime]::UtcNow.ToString('o') })
    $removed=Invoke-DockerBounded @('rm',$name) 60000
    $null=Write-StateRecord ($stem + '-removed.json') ([ordered]@{ name=$name; removed=(-not $removed.TimedOut -and $removed.ExitCode -eq 0); detail=(Get-DockerError $removed) })
    if ($kind -ceq 'unknown') { $script:Uncertain=$true }
    $line=$(if ($lines.Count -eq 1) { $lines[0] } else { $null })
    return [pscustomobject]@{ Kind=$kind; Value=$value; Line=$line; Name=$name; Sequence=$sequence }
}
function Get-HelperError($Helper) {
    if ($null -ne $Helper.Value -and $null -ne (Get-Optional $Helper.Value 'error')) { return [string]$Helper.Value.error }
    return ('helper ' + $Helper.Name + ' ended as ' + $Helper.Kind)
}
function ConvertTo-TupleText($Tuple) {
    return ([ordered]@{ path=$Tuple.path; device=$Tuple.device; inode=$Tuple.inode; uid=$Tuple.uid; mode=$Tuple.mode } | ConvertTo-Json -Compress)
}

# ---- operation state -----------------------------------------------------------------------------------------
function Read-Pending {
    if (-not (Test-Path -LiteralPath $script:PendingPath -PathType Leaf)) { return $null }
    $pending=ConvertFrom-StrictJson (Read-Bounded $script:PendingPath 65536)
    Assert-Closed $pending @('contract_version','operation_id','state_directory','release_manifest_sha256','created_utc') 'same-root pending marker'
    if ([string]$pending.contract_version -cne 'mineru-same-root-pending.v1') { throw 'same-root pending marker contract is unsupported' }
    return $pending
}
function Assert-PendingOperation {
    $pending=Read-Pending
    if ($null -eq $pending -or [string]$pending.operation_id -cne $OperationId) { throw 'no pending same-root maintenance names this operation' }
    if (-not (Test-Path -LiteralPath $script:OpDir -PathType Container)) { throw 'pending same-root operation directory is absent' }
}
function Read-VerifiedProof([string]$Phase) {
    if ([string]::IsNullOrEmpty($ProofPath) -or $ProofSha256 -cnotmatch '\Asha256:[0-9a-f]{64}\z') { throw ('the ' + $Phase + ' phase requires the wrapper''s API proof') }
    $bytes=Read-Bounded $ProofPath 1048576
    if ((Get-Sha256Bytes $bytes) -cne $ProofSha256) { throw 'API proof bytes differ from the wrapper record' }
    $proof=ConvertFrom-StrictJson $bytes
    if ([string]$proof.contract_version -cne 'm6.same-root-api-proof.v1' -or [string]$proof.operation_id -cne $OperationId -or [string]$proof.phase -cne $Phase) { throw 'API proof does not bind this operation phase' }
    return [pscustomobject]@{ Bytes=$bytes; Proof=$proof; State=([string]$proof.proof_line | ConvertFrom-Json) }
}
function Get-PrestopState {
    $proof=Read-StateRecord 'api-proof-prestop.json'
    if ($null -eq $proof) { throw 'prestop API proof is absent' }
    $state=[string]$proof.proof_line | ConvertFrom-Json
    if ([long]$state.file_count -ne 1 -or [long]$state.total_bytes -lt 1 -or [string]$state.quiescence.registry_sha256 -cnotmatch '\Asha256:[0-9a-f]{64}\z' -or
        [string]$state.quiescence.root_identity.path -cne $ApiOutputTarget) { throw 'prestop API proof is not a one-registry quiescent root' }
    return $state
}
function Assert-PrestopComplete {
    Assert-PendingOperation
    if ($null -eq (Read-StateRecord 'prestop.json')) { throw 'prestop has not completed for this operation' }
}
# Once close has recorded its transition, only close continues the operation.
function Assert-NotClosing {
    if (Test-Path -LiteralPath (Join-Path $script:OpDir 'close-intent.json')) { throw 'close has begun for this operation; only close continues it' }
}
# The pending operation belongs to the release that started it (its marker records the verified manifest):
# every later phase runs that same package, helper and protocol. Only status may look at it from another.
function Assert-PendingRelease {
    $pending=Read-Pending
    if ($null -ne $pending -and [string]$pending.operation_id -ceq $OperationId -and [string]$pending.release_manifest_sha256 -cne $ReleaseManifestSha256) {
        throw ('same-root operation ' + $OperationId + ' was started by release ' + [string]$pending.release_manifest_sha256 + '; only that release continues it')
    }
}
# The pair to re-attest from: the prestop original, or this operation's latest re-attestation when the
# Docker storage was mounted again. It is always the latest actual registry; the old R0 is never written back.
function Get-Baseline {
    $state=Get-PrestopState
    $identity=Read-StateRecord 'identity-prestop.json'
    $apply=Get-Latest 'apply'
    if ($null -eq $apply) {
        return [pscustomobject]@{ Generation=1; ApprovedRoot=$state.quiescence.root_identity; Sha256=[string]$state.quiescence.registry_sha256
            Bytes=[long]$state.total_bytes; FileId=[string]$identity.registry.file_id; Source='prestop' }
    }
    return [pscustomobject]@{ Generation=([int]$apply.Value.generation + 1); ApprovedRoot=$apply.Value.current_root
        Sha256=[string]$apply.Value.registry_sha256; Bytes=[long]$apply.Value.registry_bytes; FileId=[string]$apply.Value.registry_file_id; Source=$apply.Name }
}
function New-HelperRequest([string]$Action,$Approved,[string]$R0Sha,[long]$R0Bytes,$Body,$Current,$R1Sha,$R1Bytes) {
    return [ordered]@{ schema='mineru-same-root-helper-request.v1'; action=$Action; operation_id=$OperationId; root=$ApiOutputTarget
        approved_root=$Approved; expected_r0_sha256=$R0Sha; expected_r0_bytes=$R0Bytes; expected_body_sha256=$Body
        current_root=$Current; expected_r1_sha256=$R1Sha; expected_r1_bytes=$R1Bytes; deadline_seconds=$HelperDeadlineSeconds }
}
function Assert-ObservationConserves($Observation,$State) {
    if ([long]$Observation.record_count -ne [long]$State.quiescence.record_count -or
        [string]$Observation.submission_watermark_bucket -cne [string]$State.quiescence.submission_watermark_bucket) {
        throw 'helper observation does not conserve the prestop record count and watermark'
    }
}

# ---- phases ------------------------------------------------------------------------------------------------
function Invoke-Prestop {
    $pending=Read-Pending
    if ($null -eq $pending) {
        if (Test-Path -LiteralPath $script:OpDir) {
            if (Test-Path -LiteralPath (Join-Path $script:OpDir 'pending.closed.json')) { throw ('same-root operation is already closed: ' + $OperationId) }
            # The marker follows the proof copy before anything is frozen or changed: a directory without it
            # holds no mutation, and a new operation starts cleanly beside it.
            throw ('operation directory exists without a pending marker; nothing was changed under it, start a new operation: ' + $script:OpDir)
        }
        $proof=Read-VerifiedProof 'prestop'
        [void][IO.Directory]::CreateDirectory($StateRoot)
        [void][IO.Directory]::CreateDirectory($script:OpDir)
        Write-NewBytes (Join-Path $script:OpDir 'api-proof-prestop.json') $proof.Bytes
        # The marker exists before the first container mutation and is only moved by a verified close.
        Write-NewBytes $script:PendingPath (ConvertTo-RecordBytes ([ordered]@{ contract_version='mineru-same-root-pending.v1'; operation_id=$OperationId
            state_directory=$script:OpDir; release_manifest_sha256=$ReleaseManifestSha256; created_utc=[DateTime]::UtcNow.ToString('o') }))
        $script:Facts['resumed']=$false
    } else {
        Assert-PendingOperation
        Assert-NotClosing
        $script:Facts['resumed']=$true
    }
    $state=Get-PrestopState
    $frozen=Read-StateRecord 'containers.json'
    if ($null -eq $frozen) {
        $null=Write-StateRecord 'containers.json' (New-FrozenContainers (Read-BusinessContainers))
        $frozen=Read-StateRecord 'containers.json'
    } else {
        $live=Read-BusinessContainers
        foreach ($item in @($frozen.containers)) {
            if ([string]$live[[string]$item.name].Id -cne [string]$item.id) { throw ('business container was replaced before the stop: ' + $item.name) }
        }
    }
    if ($null -eq (Read-StateRecord 'identity-prestop.json')) {
        $identity=Get-WindowsIdentity
        if ($identity.Record.registry_sha256 -cne [string]$state.quiescence.registry_sha256 -or [long]$identity.Record.registry_bytes -ne [long]$state.total_bytes) {
            throw 'Windows registry bytes differ from the API''s own quiescence proof'
        }
        # A run interrupted after copying the original is completed only when that copy is the proof's exact
        # bytes; it is never rewritten.
        $copied=Read-StateBytes 'r0.registry.json'
        if ($null -eq $copied) { Write-NewBytes (Join-Path $script:OpDir 'r0.registry.json') $identity.Bytes }
        elseif ((Get-Sha256Bytes $copied) -cne $identity.Record.registry_sha256) { throw 'persisted original registry differs from the prestop proof' }
        $null=Write-StateRecord 'identity-prestop.json' $identity.Record
    }
    $original=Read-StateBytes 'r0.registry.json'
    if ((Get-Sha256Bytes $original) -cne [string]$state.quiescence.registry_sha256) { throw 'persisted original registry differs from the prestop proof' }
    if ($null -eq (Read-StateRecord 'restart-no.json')) {
        foreach ($item in @($frozen.containers)) { Set-BusinessRestartPolicy $item 'no' }
        $null=Write-StateRecord 'restart-no.json' ([ordered]@{ operation_id=$OperationId; policy='no'; verified_utc=[DateTime]::UtcNow.ToString('o') })
    }
    if ($null -eq (Read-StateRecord 'stopped.json')) {
        $items=@($frozen.containers); [array]::Reverse($items)
        foreach ($item in $items) { Stop-BusinessContainer $item }
        $null=Write-StateRecord 'stopped.json' ([ordered]@{ operation_id=$OperationId; order=@($items | ForEach-Object { [string]$_.name }); verified_utc=[DateTime]::UtcNow.ToString('o') })
    }
    Assert-FrozenContainers $frozen (Read-BusinessContainers) $true
    $before=Read-StateRecord 'identity-prestop.json'
    if ($null -eq (Read-StateRecord 'identity-stopped.json')) {
        $now=Get-WindowsIdentity
        Assert-SameRoot $before $now.Record
        if ([string]$now.Record.registry.file_id -cne [string]$before.registry.file_id -or $now.Record.registry_sha256 -cne [string]$before.registry_sha256) {
            throw 'the stop rewrote or replaced the registry'
        }
        $null=Write-StateRecord 'identity-stopped.json' $now.Record
    }
    if ($null -eq (Read-StateRecord 'baseline.json')) {
        # Same VM boot, same mount form: the helper must see exactly the API's tuple and the original bytes.
        $helper=Invoke-SameRootHelper 'observe' (New-HelperRequest 'observe' $state.quiescence.root_identity ([string]$state.quiescence.registry_sha256) ([long]$state.total_bytes) $null $null $null $null)
        if ($helper.Kind -cne 'pass') { throw ('baseline helper observation ' + $helper.Kind + ': ' + (Get-HelperError $helper)) }
        $observation=$helper.Value
        if ([string]$observation.registry_state -cne 'r0' -or [string]$observation.outcome -cne 'noop' -or
            (ConvertTo-TupleText $observation.current_root) -cne (ConvertTo-TupleText $state.quiescence.root_identity)) {
            throw 'the helper does not see the API''s own root tuple before the storage move'
        }
        Assert-ObservationConserves $observation $state
        $null=Write-StateRecord 'baseline.json' ([ordered]@{ operation_id=$OperationId; helper=$helper.Name; observation=$helper.Line
            body_sha256=[string]$observation.body_sha256 })
    }
    if ($null -eq (Read-StateRecord 'prestop.json')) {
        $null=Write-StateRecord 'prestop.json' ([ordered]@{ contract_version='mineru-same-root-prestop.v1'; operation_id=$OperationId
            release_manifest_sha256=$ReleaseManifestSha256; api_image_id=$ApiImageId; registry_sha256=[string]$state.quiescence.registry_sha256
            registry_bytes=[long]$state.total_bytes; body_sha256=[string](Read-StateRecord 'baseline.json').body_sha256
            completed_utc=[DateTime]::UtcNow.ToString('o') })
    }
    $script:Outcome='stopped'
}

function Invoke-Observe {
    Assert-PrestopComplete
    Assert-NotClosing
    $state=Get-PrestopState
    $frozen=Read-StateRecord 'containers.json'
    Assert-FrozenContainers $frozen (Read-BusinessContainers) $true
    $before=Read-StateRecord 'identity-prestop.json'
    $baseline=Get-Baseline
    $body=[string](Read-StateRecord 'baseline.json').body_sha256
    $now=Get-WindowsIdentity
    Assert-SameRoot $before $now.Record
    if ([string]$now.Record.registry.sddl -cne [string]$before.registry.sddl) { throw 'registry ACL differs from the original' }
    $unchangedFile=[string]$now.Record.registry.file_id -ceq $baseline.FileId
    if ($unchangedFile -and $now.Record.registry_sha256 -cne $baseline.Sha256) { throw 'registry bytes changed in place' }
    $helper=Invoke-SameRootHelper 'observe' (New-HelperRequest 'observe' $baseline.ApprovedRoot $baseline.Sha256 $baseline.Bytes $body $null $null $null)
    if ($helper.Kind -cne 'pass') { throw ('helper observation ' + $helper.Kind + ': ' + (Get-HelperError $helper)) }
    $observation=$helper.Value
    Assert-ObservationConserves $observation $state
    if ($unchangedFile -and [string]$observation.registry_state -cne 'r0') { throw 'helper state differs from the unchanged registry file' }
    if (-not $unchangedFile -and ([string]$observation.registry_state -cne 'r1' -or $now.Record.registry_sha256 -cne [string]$observation.r1_sha256)) {
        throw 'registry file changed but is not this operation''s re-attestation'
    }
    # A plan number is never reused: an observation interrupted before its plan record keeps its registry copy
    # and identity record as evidence, and the next observation takes the following number.
    $number=1
    foreach ($file in @(Get-ChildItem -LiteralPath $script:OpDir -File)) {
        if ($file.Name -cmatch '\A(?:plan|identity-observe)-([0-9]{1,6})(?:\.registry)?\.json\z' -and [int]$Matches[1] -ge $number) { $number=[int]$Matches[1] + 1 }
    }
    Write-NewBytes (Join-Path $script:OpDir ('plan-' + $number + '.registry.json')) $now.Bytes
    $identitySha=Write-StateRecord ('identity-observe-' + $number + '.json') $now.Record
    $planSha=Write-StateRecord ('plan-' + $number + '.json') ([ordered]@{ contract_version='mineru-same-root-plan.v1'; operation_id=$OperationId
        sequence=$number; generation=$baseline.Generation; baseline=$baseline.Source; captured_utc=[DateTime]::UtcNow.ToString('o')
        release_manifest_sha256=$ReleaseManifestSha256; api_image_id=$ApiImageId; windows_identity_sha256=$identitySha
        registry_file_id=[string]$now.Record.registry.file_id; approved_root=$observation.approved_root; current_root=$observation.current_root
        outcome=[string]$observation.outcome; registry_state=[string]$observation.registry_state
        expected_r0_sha256=[string]$observation.r0_sha256; expected_r0_bytes=$observation.r0_bytes; expected_body_sha256=[string]$observation.body_sha256
        expected_r1_sha256=[string]$observation.r1_sha256; expected_r1_bytes=$observation.r1_bytes
        record_count=$observation.record_count; submission_watermark_bucket=$observation.submission_watermark_bucket
        helper_observation=$helper.Line })
    $script:Facts['plan']=[ordered]@{ name=('plan-' + $number + '.json'); sha256=$planSha; generation=$baseline.Generation
        outcome=[string]$observation.outcome; registry_state=[string]$observation.registry_state }
    $script:Outcome=[string]$observation.outcome
}

function Invoke-Apply {
    Assert-PrestopComplete
    Assert-NotClosing
    $state=Get-PrestopState
    if ([string]::IsNullOrEmpty($ApprovalPath) -or $ApprovalSha256 -cnotmatch '\Asha256:[0-9a-f]{64}\z') { throw 'apply requires the approval bound by the installation binding' }
    $approvalBytes=Read-Bounded $ApprovalPath 65536
    if ((Get-Sha256Bytes $approvalBytes) -cne $ApprovalSha256) { throw 'approval bytes differ from the installation binding' }
    $approval=ConvertFrom-StrictJson $approvalBytes
    Assert-Closed $approval @('contract_version','operation_id','plan_sha256','approved_by','decision_reference','approved_at_utc') 'same-root approval'
    if ([string]$approval.contract_version -cne 'm6.same-root-approval.v1' -or [string]$approval.operation_id -cne $OperationId) { throw 'approval does not bind this operation' }
    $plan=Get-Latest 'plan'
    if ($null -eq $plan -or $plan.Sha256 -cne [string]$approval.plan_sha256) { throw 'approval does not name the latest observed plan' }
    $value=$plan.Value
    $frozen=Read-StateRecord 'containers.json'
    $before=Read-StateRecord 'identity-prestop.json'
    $committed=Get-Latest 'apply'
    if ($null -ne $committed -and [string]$committed.Value.plan_sha256 -ceq $plan.Sha256) {
        # This plan's apply is already committed and only its acknowledgement was lost: confirm that the registry
        # is still the very object the commit recorded, and write nothing.
        if ([string]$committed.Value.approval_sha256 -cne $ApprovalSha256) { throw 'the approved plan was applied under another approval' }
        Assert-FrozenContainers $frozen (Read-BusinessContainers) $true
        $now=Get-WindowsIdentity
        Assert-SameRoot $before $now.Record
        if ([string]$now.Record.registry.file_id -cne [string]$committed.Value.registry_file_id -or $now.Record.registry_sha256 -cne [string]$committed.Value.registry_sha256 -or
            [string]$now.Record.registry.sddl -cne [string]$before.registry.sddl) { throw 'registry is no longer the object this operation''s apply recorded' }
        $script:Facts['apply']=[ordered]@{ generation=[int]$committed.Value.generation; outcome='already_recorded'; registry_sha256=$now.Record.registry_sha256 }
        $script:Outcome='already_recorded'
        return
    }
    $baseline=Get-Baseline
    if ([string]$value.outcome -cne 'reattest') { throw 'the approved plan needs no registry write' }
    if ([int]$value.generation -ne $baseline.Generation) { throw 'the approved plan was observed against an older registry and is stale' }
    Assert-FrozenContainers $frozen (Read-BusinessContainers) $true
    $now=Get-WindowsIdentity
    Assert-SameRoot $before $now.Record
    $atOriginal=[string]$now.Record.registry.file_id -ceq $baseline.FileId -and $now.Record.registry_sha256 -ceq [string]$value.expected_r0_sha256
    $atTarget=$now.Record.registry_sha256 -ceq [string]$value.expected_r1_sha256
    if (-not $atOriginal -and -not $atTarget) { throw 'registry is neither the approved original nor its approved re-attestation' }
    $helper=Invoke-SameRootHelper 'apply' (New-HelperRequest 'apply' $value.approved_root ([string]$value.expected_r0_sha256) ([long]$value.expected_r0_bytes) ([string]$value.expected_body_sha256) $value.current_root ([string]$value.expected_r1_sha256) $value.expected_r1_bytes)
    if ($helper.Kind -cne 'pass') { throw ('helper apply ' + $helper.Kind + ': ' + (Get-HelperError $helper)) }
    $result=$helper.Value
    if ([string]$result.registry_sha256 -cne [string]$value.expected_r1_sha256) { $script:Uncertain=$true; throw 'helper reported bytes other than the approved re-attestation' }
    $after=Get-WindowsIdentity
    Assert-SameRoot $before $after.Record
    if ($after.Record.registry_sha256 -cne [string]$value.expected_r1_sha256 -or [long]$after.Record.registry_bytes -ne [long]$value.expected_r1_bytes) {
        $script:Uncertain=$true; throw 'Windows readback differs from the approved re-attestation'
    }
    if ([string]$after.Record.registry.file_id -ceq $baseline.FileId) { $script:Uncertain=$true; throw 'Windows readback still names the original registry file' }
    if ([string]$after.Record.registry.sddl -cne [string]$before.registry.sddl) { $script:Uncertain=$true; throw 'the re-attested registry ACL differs from the original' }
    $generation=$baseline.Generation
    # The post-apply readback is recorded before its commit record. A run interrupted between the two completes
    # only when it reads back the very same registry object; the earlier record is kept, never rewritten.
    $identityName='identity-apply-' + $generation + '.json'
    $recorded=Read-StateBytes $identityName
    if ($null -eq $recorded) { $identitySha=Write-StateRecord $identityName $after.Record }
    else {
        $previous=ConvertFrom-StrictJson $recorded
        Assert-SameRoot $previous $after.Record
        if ([string]$previous.registry.file_id -cne [string]$after.Record.registry.file_id -or [string]$previous.registry_sha256 -cne $after.Record.registry_sha256 -or
            [long]$previous.registry_bytes -ne [long]$after.Record.registry_bytes -or [string]$previous.registry.sddl -cne [string]$after.Record.registry.sddl) {
            throw 'registry differs from the object this operation recorded right after its apply'
        }
        $identitySha=Get-Sha256Bytes $recorded
    }
    $null=Write-StateRecord ('apply-' + $generation + '.json') ([ordered]@{ contract_version='mineru-same-root-apply-record.v1'; operation_id=$OperationId
        generation=$generation; plan_name=$plan.Name; plan_sha256=$plan.Sha256; approval_sha256=$ApprovalSha256; windows_identity_sha256=$identitySha
        approved_root=$value.approved_root; current_root=$value.current_root; registry_sha256=$after.Record.registry_sha256
        registry_bytes=$after.Record.registry_bytes; registry_file_id=[string]$after.Record.registry.file_id
        previous_registry_file_id=$baseline.FileId; helper=$helper.Name; helper_result=$helper.Line; recorded_utc=[DateTime]::UtcNow.ToString('o') })
    $script:Facts['apply']=[ordered]@{ generation=$generation; outcome=[string]$result.outcome; registry_sha256=$after.Record.registry_sha256 }
    $script:Outcome=[string]$result.outcome
}

function Get-RestoreExpectation {
    $state=Get-PrestopState
    $plan=Get-Latest 'plan'
    if ($null -eq $plan) { throw 'restore requires an observed plan' }
    $apply=Get-Latest 'apply'
    if ($null -ne $apply -and [string]$apply.Value.plan_sha256 -ceq $plan.Sha256) {
        $sha=[string]$apply.Value.registry_sha256; $bytes=[long]$apply.Value.registry_bytes; $fileId=[string]$apply.Value.registry_file_id; $source=$apply.Name
    } elseif ([string]$plan.Value.outcome -ceq 'noop' -and [string]$plan.Value.registry_state -ceq 'r0') {
        $baseline=Get-Baseline
        $sha=$baseline.Sha256; $bytes=$baseline.Bytes; $fileId=$baseline.FileId; $source=$plan.Name
    } else { throw 'the latest plan is neither applied nor a no-op; restore is not proven safe' }
    return [ordered]@{ source=$source; plan_sha256=$plan.Sha256; root_identity=$plan.Value.current_root; registry_sha256=$sha
        registry_bytes=$bytes; registry_file_id=$fileId; record_count=$state.quiescence.record_count
        submission_watermark_bucket=$state.quiescence.submission_watermark_bucket }
}

function Invoke-Restore {
    Assert-PrestopComplete
    Assert-NotClosing
    $expected=Get-RestoreExpectation
    $frozen=Read-StateRecord 'containers.json'
    $live=Read-BusinessContainers
    Assert-FrozenContainers $frozen $live $false
    if ([bool]$live['mineru-api-proxy'].State.Running) { throw 'the proxy is running before the API''s own reader has proved the root' }
    $before=Read-StateRecord 'identity-prestop.json'
    $now=Get-WindowsIdentity
    Assert-SameRoot $before $now.Record
    if ([string]$now.Record.registry.file_id -cne [string]$expected.registry_file_id -or $now.Record.registry_sha256 -cne [string]$expected.registry_sha256) {
        throw 'registry differs from the verified restore expectation'
    }
    $items=@{}; foreach ($item in @($frozen.containers)) { $items[[string]$item.name]=$item }
    Start-BusinessContainer $items['mineru-openai-server'] 900000
    Start-BusinessContainer $items['mineru-api'] 600000
    $number=@(Get-NumberedRecords 'restore').Count + 1
    $null=Write-StateRecord ('restore-' + $number + '.json') ([ordered]@{ contract_version='mineru-same-root-restore.v1'; operation_id=$OperationId
        expected=$expected; started=@('mineru-openai-server','mineru-api'); recorded_utc=[DateTime]::UtcNow.ToString('o') })
    $script:Facts['expected']=$expected
    $script:Outcome='api_started'
}

function Invoke-Close {
    $pending=Read-Pending
    if ($null -eq $pending -and $null -ne (Read-StateRecord 'close.json')) { $script:Outcome='already_closed'; return }
    Assert-PrestopComplete
    $restore=Get-Latest 'restore'
    if ($null -eq $restore) { throw 'close requires a restore record' }
    $expected=$restore.Value.expected
    $frozen=Read-StateRecord 'containers.json'
    if ($null -eq (Read-StateRecord 'close.json')) {
        $proof=Read-VerifiedProof 'close'
        $quiescence=$proof.State.quiescence
        if ((ConvertTo-TupleText $quiescence.root_identity) -cne (ConvertTo-TupleText $expected.root_identity) -or
            [string]$quiescence.registry_sha256 -cne [string]$expected.registry_sha256 -or [long]$proof.State.total_bytes -ne [long]$expected.registry_bytes -or
            [long]$quiescence.record_count -ne [long]$expected.record_count -or
            [string]$quiescence.submission_watermark_bucket -cne [string]$expected.submission_watermark_bucket) {
            throw 'the API''s own reader does not prove the restored registry'
        }
        # The close transition is recorded before its first mutation: this operation, its restore record and every
        # frozen container's name and id with its original policy, as deterministic bytes. A resumed close must
        # rebuild exactly these bytes; only then do the policies it names count as restored, not as foreign drift.
        $owned=@(); foreach ($item in @($frozen.containers)) { $owned+=[ordered]@{ name=[string]$item.name; id=[string]$item.id; restart_policy=[string]$item.restart_policy } }
        $intentBytes=ConvertTo-RecordBytes ([ordered]@{ contract_version='mineru-same-root-close-intent.v1'; operation_id=$OperationId
            restore=$restore.Name; restore_sha256=$restore.Sha256; containers=$owned })
        $intentSha=Get-Sha256Bytes $intentBytes
        $recorded=Read-StateBytes 'close-intent.json'
        $closing=$null
        if ($null -ne $recorded) {
            if ((Get-Sha256Bytes $recorded) -cne $intentSha) { throw 'recorded close intent differs from this operation''s restore record and frozen containers' }
            $closing=ConvertFrom-StrictJson $recorded
        }
        $live=Read-BusinessContainers
        Assert-FrozenContainers $frozen $live $false $closing
        foreach ($name in @('mineru-openai-server','mineru-api')) {
            if (-not [bool]$live[$name].State.Running -or (Get-HealthText $live[$name]) -cne 'healthy') { throw ('restored container is not running healthy: ' + $name) }
        }
        # Keyed by digest: every close run keeps the API proof that admitted it, before it changes anything.
        $proofCopy='api-proof-close-' + $ProofSha256.Substring(7,16) + '.json'
        $existing=Read-StateBytes $proofCopy
        if ($null -eq $existing) { Write-NewBytes (Join-Path $script:OpDir $proofCopy) $proof.Bytes }
        elseif ((Get-Sha256Bytes $existing) -cne $ProofSha256) { throw ('close proof copy differs from its digest: ' + $proofCopy) }
        if ($null -eq $recorded) { Write-NewBytes (Join-Path $script:OpDir 'close-intent.json') $intentBytes }
        $items=@{}; foreach ($item in @($frozen.containers)) { $items[[string]$item.name]=$item }
        Start-BusinessContainer $items['mineru-api-proxy'] 300000
        foreach ($item in @($frozen.containers)) { Set-BusinessRestartPolicy $item ([string]$item.restart_policy) }
        $live=Read-BusinessContainers
        foreach ($item in @($frozen.containers)) {
            $c=$live[[string]$item.name]
            if ([string]$c.Id -cne [string]$item.id -or -not [bool]$c.State.Running -or (Get-RestartPolicyText $c) -cne [string]$item.restart_policy) { throw ('restored container differs from its frozen identity: ' + $item.name) }
        }
        $null=Write-StateRecord 'close.json' ([ordered]@{ contract_version='mineru-same-root-close.v1'; operation_id=$OperationId
            restore=$restore.Name; close_intent_sha256=$intentSha; api_proof=$proofCopy; api_proof_sha256=$ProofSha256; restart_policies='restored'
            closed_utc=[DateTime]::UtcNow.ToString('o') })
    }
    [IO.File]::Move($script:PendingPath,(Join-Path $script:OpDir 'pending.closed.json'))
    if (Test-Path -LiteralPath $script:PendingPath) { throw 'pending marker is still present after close' }
    $script:Facts['containers']=@($frozen.containers)
    $script:Outcome='closed'
}

function Invoke-Status {
    $pending=Read-Pending
    $script:Facts['pending']=$(if ($null -eq $pending) { $null } else { [string]$pending.operation_id })
    $script:Facts['pending_release_manifest_sha256']=$(if ($null -eq $pending) { $null } else { [string]$pending.release_manifest_sha256 })
    $script:Facts['operation_directory_exists']=(Test-Path -LiteralPath $script:OpDir -PathType Container)
    $script:Facts['close_intent_present']=(Test-Path -LiteralPath (Join-Path $script:OpDir 'close-intent.json') -PathType Leaf)
    try {
        $live=Read-BusinessContainers
        $script:Facts['containers']=@($ContainerNames | ForEach-Object { $c=$live[$_]
            [ordered]@{ name=$_; id=[string]$c.Id; running=[bool]$c.State.Running; health=(Get-HealthText $c); restart_policy=(Get-RestartPolicyText $c) } })
    } catch { $script:Facts['containers_error']=$_.Exception.Message }
    $helpers=Invoke-DockerBounded @('container','ls','--all','--filter',('label=agent-invest.same-root.operation=' + $OperationId),'--format','{{.Names}}|{{.State}}') 60000
    $script:Facts['helpers']=$(if ($helpers.TimedOut -or $helpers.ExitCode -ne 0) { 'unreadable: ' + (Get-DockerError $helpers) } else { [string]$helpers.StandardOutput })
    $script:Outcome=$(if ($null -eq $pending) { 'no_pending' } elseif ([string]$pending.operation_id -ceq $OperationId) { 'pending' } else { 'other_operation_pending' })
}

# ---- main ----------------------------------------------------------------------------------------------------
try {
    if ($PSVersionTable.PSVersion.Major -ne 5 -or -not [Environment]::Is64BitProcess) { throw 'Windows PowerShell 5.1 x64 is required' }
    if ($OperationId -cnotmatch '\Asr-[0-9a-f]{32}\z') { throw 'operation id is invalid' }
    foreach ($value in @($ReleaseManifestSha256,$HelperSourceSha256,$ProtocolSourceSha256,$IdentitySourceSha256)) {
        if ($value -cnotmatch '\Asha256:[0-9a-f]{64}\z') { throw 'release identities must be canonical' }
    }
    if ($ApiImageId -cnotmatch '\Asha256:[a-f0-9]{64}\z') { throw 'API image identity must be canonical' }
    $RecordDirectory=[IO.Path]::GetFullPath($RecordDirectory)
    if (Test-Path -LiteralPath $RecordDirectory) { throw 'child record directory must be new' }
    [void][IO.Directory]::CreateDirectory($RecordDirectory)
    $StateRoot=[IO.Path]::GetFullPath($StateRoot)
    $script:OutputRootFull=[IO.Path]::GetFullPath($OutputRoot)
    $script:OpDir=Join-Path $StateRoot $OperationId
    $script:PendingPath=Join-Path $StateRoot 'pending.json'
    $script:Windows=Join-Path $ReleaseRoot 'windows'
    $script:Docker=(Get-Command docker.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1 -ExpandProperty Source)
    if ($Phase -cne 'status') { Assert-PendingRelease; Import-SameRootIdentityAssembly }
    switch ($Phase) {
        'prestop' { Invoke-Prestop }
        'observe' { Invoke-Observe }
        'apply' { Invoke-Apply }
        'restore' { Invoke-Restore }
        'close' { Invoke-Close }
        'status' { Invoke-Status }
    }
    $status='pass'; $exitCode=0
} catch {
    $firstError=$_.Exception.Message
    $status=$(if ($script:Uncertain) { 'unknown' } else { 'failed' })
    $exitCode=70
} finally {
    # Evidence for the Mac: a copy of every persistent record of this operation, whatever the outcome.
    $exported=@()
    try {
        if ($null -ne $script:OpDir -and (Test-Path -LiteralPath $script:OpDir -PathType Container) -and (Test-Path -LiteralPath $RecordDirectory -PathType Container)) {
            $stateCopy=Join-Path $RecordDirectory 'state'
            [void][IO.Directory]::CreateDirectory($stateCopy)
            foreach ($file in @(Get-ChildItem -LiteralPath $script:OpDir -File)) {
                Write-NewBytes (Join-Path $stateCopy $file.Name) (Read-Bounded $file.FullName $MaximumRegistryBytes)
                $exported+=$file.Name
            }
        }
    } catch { if ($null -eq $firstError) { $firstError='state export failed: ' + $_.Exception.Message; $status='failed'; $exitCode=70 } }
    $result=[ordered]@{ contract_version='m6.same-root-child-result.v1'; phase=$Phase; operation_id=$OperationId; status=$status
        first_error=$firstError; uncertain=$script:Uncertain; outcome=$script:Outcome; state_directory=$script:OpDir
        pending_present=$(if ($null -ne $script:PendingPath) { Test-Path -LiteralPath $script:PendingPath } else { $null })
        facts=$script:Facts; exported_records=$exported; elapsed_milliseconds=$script:Clock.ElapsedMilliseconds; finished_utc=[DateTime]::UtcNow.ToString('o') }
    try { if (Test-Path -LiteralPath $RecordDirectory -PathType Container) { Write-NewBytes (Join-Path $RecordDirectory 'same-root-result.json') (ConvertTo-RecordBytes $result) } }
    catch { $exitCode=70 }
    $result | ConvertTo-Json -Compress -Depth 12
}
exit $exitCode
