<#
.SYNOPSIS
Own one native M6 owner attempt on a fresh workspace: Prepare the protected private ancestors before any
secret exists, Run the pinned binary against a committed private deployment with an exact process handle,
strict READY within a fixed deadline, bounded continuous capture, exact-instance Cancel, and an external exit
record that never trusts the child's own intent.

.DESCRIPTION
Three parameter sets, one file:
  -Prepare  creates <WorkspaceRoot>\private{,\runs,\staging,\attempts} through the qualified binary's own
            CreatePrivateDirectory (owner SID + SYSTEM, protected DACL, must be new) and prints the receipt.
            The controller uploads the private deployment into private\staging only after validating it.
  -Run      re-validates every private ancestor, commits the staged deployment (hash, closed structure,
            run/root/port references, owner set to this account, atomic move, read-back), creates one new
            attempt directory, spawns the host, requires the exact seven-field READY line within
            ReadyWaitSeconds (else terminates and reaps the child, exit 3), then supervises: both pipes are
            drained continuously into bounded head/tail retention, cancel.json is polled every 250 ms and
            honoured only when every field names this exact instance, and one absolute deadline from spawn is
            never renewed. Supervision ends only when the held process handle signals; pipe EOF is not exit.
            READY is decoded solely by the pinned assembly's strict parser and canonical anchor validator.
            process-exit.json records the actual exit code and any forced termination.
  -Cancel   writes cancel.json for one attempt from its own process-start record; an identical repeat is
            idempotent, different content is refused. There is no PID-only kill anywhere.
Windows PowerShell 5.1, 64-bit. Machine execution policy is not changed. Exit codes: 0 ok, 2 parameter,
3 READY deadline missed, 4 host exited non-zero, 65 identity/ACL mismatch, 70 launcher failure.
#>
[CmdletBinding(DefaultParameterSetName='Run')]
param(
    [Parameter(ParameterSetName='Prepare',Mandatory=$true)][switch]$Prepare,
    [Parameter(ParameterSetName='Run',Mandatory=$true)][switch]$Run,
    [Parameter(ParameterSetName='Cancel',Mandatory=$true)][switch]$Cancel,
    [Parameter(Mandatory=$true)][string]$WorkspaceRoot,
    [Parameter(Mandatory=$true)][string]$ExpectedHostname,
    [Parameter(ParameterSetName='Prepare',Mandatory=$true)][Parameter(ParameterSetName='Run',Mandatory=$true)][string]$BinaryPath,
    [Parameter(ParameterSetName='Prepare',Mandatory=$true)][Parameter(ParameterSetName='Run',Mandatory=$true)][string]$ExpectedBinarySha256,
    [Parameter(ParameterSetName='Run',Mandatory=$true)][Parameter(ParameterSetName='Cancel',Mandatory=$true)][string]$AttemptId,
    [Parameter(ParameterSetName='Run',Mandatory=$true)][string]$StagedConfigurationName,
    [Parameter(ParameterSetName='Run',Mandatory=$true)][string]$ExpectedConfigurationSha256,
    [Parameter(ParameterSetName='Run',Mandatory=$true)][string]$ExpectedRunId,
    [Parameter(ParameterSetName='Run',Mandatory=$true)][long]$PlannedSeconds,
    [Parameter(ParameterSetName='Run',Mandatory=$true)][long]$CloseGraceSeconds,
    [Parameter(ParameterSetName='Run')][long]$MemoryBytes=536870912,
    [Parameter(ParameterSetName='Run')][long]$ResumeDeadlineTicks=0,
    [Parameter(ParameterSetName='Run')][string]$OriginalAnchorSha256='none',
    [Parameter(ParameterSetName='Run')][long]$ExitWaitExtraSeconds=180,
    [Parameter(ParameterSetName='Run')][long]$ReadyWaitSeconds=30,
    [Parameter(ParameterSetName='Cancel',Mandatory=$true)][string]$Reason
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
$Set=$PSCmdlet.ParameterSetName
$RetainBytes=65536
$utf8=[Text.UTF8Encoding]::new($false,$true)
$HashPattern='^sha256:[0-9a-f]{64}$'
$IdPattern='^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
$ReadyFields=@('status','anchor_sha256','owner_epoch_sha256','anchor','spec_sha256','journal_prefix_bytes','journal_prefix_sha256')
$CancelFields=@('contract_version','process_start_record_sha256','run_id','attempt_id','pid','creation_filetime_100ns','binary_sha256','configuration_sha256','reason')
$EmptySha='sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'

function Sha256Bytes([byte[]]$Bytes) {
    $hash=[Security.Cryptography.SHA256]::Create()
    try { return 'sha256:' + ([BitConverter]::ToString($hash.ComputeHash($Bytes))).Replace('-','').ToLowerInvariant() } finally { $hash.Dispose() }
}
function Sha256File([string]$Path) {
    $file=[IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    try { $hash=[Security.Cryptography.SHA256]::Create(); try { return 'sha256:' + ([BitConverter]::ToString($hash.ComputeHash($file))).Replace('-','').ToLowerInvariant() } finally { $hash.Dispose() } } finally { $file.Dispose() }
}
function Emit([string]$Prefix,[string]$Json) { [Console]::Out.WriteLine($Prefix + ' ' + $Json); [Console]::Out.Flush() }
function Write-NewRecord([string]$Path,[string]$Json) {
    $bytes=$utf8.GetBytes($Json)
    $file=[IO.FileStream]::new($Path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try { $file.Write($bytes,0,$bytes.Length); $file.Flush($true) } finally { $file.Dispose() }
    return (Sha256Bytes $bytes)
}
function Read-BoundedBytes([string]$Path,[int]$Maximum) {
    $file=[IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite)
    try {
        if ($file.Length -lt 1 -or $file.Length -gt $Maximum) { throw ('record byte bound: ' + $Path) }
        $bytes=[byte[]]::new([int]$file.Length); $used=0
        while ($used -lt $bytes.Length) { $n=$file.Read($bytes,$used,$bytes.Length - $used); if ($n -eq 0) { throw 'truncated record' }; $used += $n }
        return $bytes
    } finally { $file.Dispose() }
}
function Get-Property($Object,[string]$Name) {
    $property=$Object.PSObject.Properties[$Name]
    if ($null -eq $property) { throw ('missing field ' + $Name) }
    return $property.Value
}
function Assert-ExactFields($Object,[string[]]$Fields,[string]$Label) {
    if ($null -eq $Object -or $Object -isnot [Management.Automation.PSCustomObject]) { throw ($Label + ' is not a JSON object') }
    $names=@($Object.PSObject.Properties | ForEach-Object { $_.Name })
    if ($names.Count -ne $Fields.Count) { throw ($Label + ' field count differs') }
    foreach ($field in $Fields) { if ($names -cnotcontains $field) { throw ($Label + ' lacks ' + $field) } }
}
function Assert-CanonicalPath([string]$Path) {
    if (-not [IO.Path]::IsPathRooted($Path) -or [IO.Path]::GetFullPath($Path) -cne $Path -or $Path.StartsWith('\\') -or $Path.Length -lt 4) { throw ('absolute canonical local path required: ' + $Path) }
}
function Assert-NoReparse([string]$Path) {
    $current=$Path
    while (-not [string]::IsNullOrEmpty($current)) {
        if (([IO.File]::GetAttributes($current) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw ('reparse point in private path: ' + $current) }
        $current=[IO.Path]::GetDirectoryName($current)
    }
}
function Get-UserSid { $identity=[Security.Principal.WindowsIdentity]::GetCurrent(); try { if ($null -eq $identity.User) { throw 'current user SID unavailable' }; return $identity.User } finally { $identity.Dispose() } }
$SystemSid=[Security.Principal.SecurityIdentifier]::new([Security.Principal.WellKnownSidType]::LocalSystemSid,$null)
$Sections=[Security.AccessControl.AccessControlSections]::Access -bor [Security.AccessControl.AccessControlSections]::Owner
function Assert-PrivateRules($Security,[string]$Label) {
    $user=Get-UserSid
    if (-not $user.Equals($Security.GetOwner([Security.Principal.SecurityIdentifier]))) { throw ($Label + ': owner differs') }
    if (-not $Security.AreAccessRulesProtected) { throw ($Label + ': ACL is not protected') }
    foreach ($rule in $Security.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and -not ($rule.IdentityReference.Equals($user) -or $rule.IdentityReference.Equals($SystemSid))) { throw ($Label + ': grants another principal ' + $rule.IdentityReference.Value) }
    }
    return $Security.GetSecurityDescriptorSddlForm($Sections)
}
function Assert-PrivateDirectory([string]$Path) {
    Assert-CanonicalPath $Path
    if (-not [IO.Directory]::Exists($Path)) { throw ('private directory missing: ' + $Path) }
    Assert-NoReparse $Path
    return (Assert-PrivateRules ([IO.Directory]::GetAccessControl($Path,$Sections)) $Path)
}
function Assert-PrivateFile([string]$Path) {
    Assert-CanonicalPath $Path
    if (-not [IO.File]::Exists($Path)) { throw ('private file missing: ' + $Path) }
    Assert-NoReparse $Path
    return (Assert-PrivateRules ([IO.File]::GetAccessControl($Path,$Sections)) $Path)
}
function Set-PrivateFileOwner([string]$Path) {
    # sftp-created files may be owned by the Administrators group; the host requires this account as owner.
    $user=Get-UserSid
    $security=[IO.File]::GetAccessControl($Path,$Sections)
    $security.SetOwner($user)
    $security.SetAccessRuleProtection($true,$false)
    foreach ($rule in @($security.GetAccessRules($true,$false,[Security.Principal.SecurityIdentifier]))) { $null=$security.RemoveAccessRule($rule) }
    foreach ($sid in @($user,$SystemSid)) { $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,[Security.AccessControl.FileSystemRights]::FullControl,[Security.AccessControl.AccessControlType]::Allow)) }
    [IO.File]::SetAccessControl($Path,$security)
}
function Load-PinnedBinary {
    if ($ExpectedBinarySha256 -cnotmatch $HashPattern) { throw 'explicit owner executable identity required' }
    Assert-CanonicalPath $BinaryPath
    $pin=[IO.FileStream]::new($BinaryPath,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $hash=[Security.Cryptography.SHA256]::Create()
    try { $pinned='sha256:' + ([BitConverter]::ToString($hash.ComputeHash($pin))).Replace('-','').ToLowerInvariant() } finally { $hash.Dispose() }
    if ($pinned -cne $ExpectedBinarySha256) { $pin.Dispose(); throw 'owner executable drift' }
    $null=[Reflection.Assembly]::LoadFile($BinaryPath)
    return $pin
}
$nativeHostname=[Environment]::MachineName
$launcherSha=Sha256File $PSCommandPath
$firstError=$null
$records=[ordered]@{}
function Finish([int]$Code,[hashtable]$Extra) {
    $result=[ordered]@{contract_version='m6.owner-launcher-result.v1';set=$Set;hostname=$nativeHostname;launcher_sha256=$launcherSha;exit_code=$Code;records=$records;first_error=$firstError;finished_utc=[DateTime]::UtcNow.ToString('o')}
    if ($null -ne $Extra) { foreach ($key in $Extra.Keys) { $result[$key]=$Extra[$key] } }
    Emit 'M6-RESULT' ($result | ConvertTo-Json -Compress -Depth 6)
    exit $Code
}
try {
    if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'Windows PowerShell 5.1 is required' }
    if (-not [Environment]::Is64BitProcess) { throw '64-bit PowerShell is required' }
    if ($nativeHostname -cne $ExpectedHostname) { throw 'native host differs' }
    Assert-CanonicalPath $WorkspaceRoot
} catch { $firstError='parameter: ' + $_.Exception.Message; Finish 2 $null }
$privateRoot=Join-Path $WorkspaceRoot 'private'
$runsRoot=Join-Path $privateRoot 'runs'
$stagingRoot=Join-Path $privateRoot 'staging'
$attemptsRoot=Join-Path $privateRoot 'attempts'

# ---------------------------------------------------------------- Prepare
if ($Set -eq 'Prepare') {
    $pin=$null
    try {
        $pin=Load-PinnedBinary
        if ([IO.Directory]::Exists($privateRoot) -or [IO.File]::Exists($privateRoot)) { throw 'private root already exists; a campaign uses a fresh workspace' }
        if (-not [IO.Directory]::Exists($WorkspaceRoot)) { $null=[IO.Directory]::CreateDirectory($WorkspaceRoot) }
        Assert-NoReparse $WorkspaceRoot
        $directories=[ordered]@{}
        foreach ($path in @($privateRoot,$runsRoot,$stagingRoot,$attemptsRoot)) {
            [MineruM6PrivateStore]::CreatePrivateDirectory($path)
            $directories[$path]=[ordered]@{sddl=(Assert-PrivateDirectory $path);protected=$true}
        }
        $receipt=[ordered]@{contract_version='m6.owner-workspace-prepare.v1';hostname=$nativeHostname;workspace_root=$WorkspaceRoot;private_root=$privateRoot;runs_root=$runsRoot;staging_root=$stagingRoot;attempts_root=$attemptsRoot;owner_sid=(Get-UserSid).Value;directories=$directories;binary_sha256=$ExpectedBinarySha256;launcher_sha256=$launcherSha;created_utc=[DateTime]::UtcNow.ToString('o')}
        $json=$receipt | ConvertTo-Json -Compress -Depth 6
        $records['prepare_receipt']=Join-Path $WorkspaceRoot 'prepare-receipt.json'
        $null=Write-NewRecord $records['prepare_receipt'] $json
        Emit 'M6-PREPARE' $json
    } catch { $firstError='prepare: ' + $_.Exception.Message } finally { if ($null -ne $pin) { $pin.Dispose() } }
    if ($null -ne $firstError) { Finish 70 $null }
    Finish 0 $null
}

# ---------------------------------------------------------------- Cancel
if ($Set -eq 'Cancel') {
    try {
        if ($AttemptId -cnotmatch $IdPattern) { throw 'attempt id shape' }
        if ($Reason.Length -lt 1 -or $Reason.Length -gt 256 -or @($Reason.ToCharArray() | Where-Object { [int]$_ -lt 32 -or [int]$_ -eq 127 }).Count -gt 0) { throw 'reason must be 1..256 printable characters' }
        $attemptDir=Join-Path $attemptsRoot $AttemptId
        $null=Assert-PrivateDirectory $attemptDir
        $startPath=Join-Path $attemptDir 'process-start.json'
        $startBytes=Read-BoundedBytes $startPath 65536
        $start=$utf8.GetString($startBytes) | ConvertFrom-Json
        $command=[ordered]@{contract_version='m6.owner-cancel.v1';process_start_record_sha256=(Sha256Bytes $startBytes);run_id=(Get-Property $start 'run_id');attempt_id=$AttemptId;pid=[long](Get-Property $start 'pid');creation_filetime_100ns=[long](Get-Property $start 'creation_filetime_100ns');binary_sha256=(Get-Property $start 'binary_sha256');configuration_sha256=(Get-Property $start 'configuration_sha256');reason=$Reason}
        if ((Get-Property $start 'attempt_id') -cne $AttemptId) { throw 'process-start record names another attempt' }
        $cancelPath=Join-Path $attemptDir 'cancel.json'
        if ([IO.File]::Exists($cancelPath)) {
            $existing=$utf8.GetString((Read-BoundedBytes $cancelPath 4096)) | ConvertFrom-Json
            Assert-ExactFields $existing $CancelFields 'existing cancel command'
            foreach ($field in $CancelFields) { if ([string](Get-Property $existing $field) -cne [string]$command[$field]) { $firstError='cancel: an earlier cancel command with different content exists (' + $field + ')'; Finish 65 $null } }
            $records['cancel']=$cancelPath
            Finish 0 @{cancel='already_requested'}
        }
        $records['cancel']=$cancelPath
        $null=Write-NewRecord $cancelPath ($command | ConvertTo-Json -Compress)
        Emit 'M6-CANCEL' ($command | ConvertTo-Json -Compress)
    } catch { $firstError='cancel: ' + $_.Exception.Message; Finish 70 $null }
    Finish 0 @{cancel='requested'}
}

# ---------------------------------------------------------------- Run
try {
    if ($AttemptId -cnotmatch $IdPattern -or $ExpectedRunId -cnotmatch $IdPattern) { throw 'attempt/run id shape' }
    if ($PlannedSeconds -lt 1 -or $CloseGraceSeconds -lt 1 -or ($PlannedSeconds + $CloseGraceSeconds) -gt 7200) { throw 'planned plus grace seconds must be within the owner host ceiling' }
    if ($MemoryBytes -lt 67108864 -or $ExitWaitExtraSeconds -lt 30 -or $ExitWaitExtraSeconds -gt 3600 -or $ReadyWaitSeconds -lt 5 -or $ReadyWaitSeconds -gt 300) { throw 'memory bound or wait bounds out of range' }
    if ($ExpectedConfigurationSha256 -cnotmatch $HashPattern) { throw 'explicit deployment identity required' }
    if (($ResumeDeadlineTicks -gt 0) -ne ($OriginalAnchorSha256 -ne 'none')) { throw 'resume requires both the original deadline and anchor hash' }
    if ($OriginalAnchorSha256 -ne 'none' -and $OriginalAnchorSha256 -cnotmatch $HashPattern) { throw 'original anchor hash shape' }
    if ($StagedConfigurationName -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json$') { throw 'staged configuration name shape' }
} catch { $firstError='parameter: ' + $_.Exception.Message; Finish 2 $null }

$resume=$OriginalAnchorSha256 -ne 'none'
$pin=$null; $process=$null
$stdout=$null; $stderr=$null
$ready=$null; $readyUtc=$null; $readyElapsed=$null; $exited=$false; $forced=$false; $failure=$null; $exitCode=$null; $birth=$null; $exactHandle=[IntPtr]::Zero
$cancelAccepted=$null; $cancelRejectedSha=$null; $recordPersisted=$true; $recordError=$null; $identityFailure=$false; $readyTimeout=$false
$attemptDir=Join-Path $attemptsRoot $AttemptId
$configurationPath=Join-Path $privateRoot ('deployment.' + $ExpectedRunId + '.json')
$clock=[Diagnostics.Stopwatch]::StartNew()
$totalMilliseconds=[long](($PlannedSeconds + $CloseGraceSeconds + $ExitWaitExtraSeconds) * 1000)
# Bounded retention of one pipe: the exact first RetainBytes (head), then only the bytes after the head in a
# sliding tail of at most RetainBytes; every byte is counted once (total = head + tail + dropped) as it arrives.
function New-Retention { return [ordered]@{head=[IO.MemoryStream]::new();tail=[IO.MemoryStream]::new();total=[long]0;dropped=[long]0;eof=$false} }
function Add-Retention($r,[byte[]]$buffer,[int]$count) {
    $r.total += $count
    $offset=0
    $headRoom=$RetainBytes - [int]$r.head.Length
    if ($headRoom -gt 0) { $take=[Math]::Min($headRoom,$count); $r.head.Write($buffer,0,$take); $offset=$take }
    if ($count -gt $offset) {
        $r.tail.Write($buffer,$offset,$count - $offset)
        if ($r.tail.Length -gt $RetainBytes) {
            $keep=$r.tail.ToArray(); $excess=$keep.Length - $RetainBytes
            $r.dropped += $excess
            $r.tail.SetLength(0); $r.tail.Write($keep,$excess,$RetainBytes)
        }
    }
}
function Get-RetentionText($r) {
    $head=$r.head.ToArray(); $tail=$r.tail.ToArray()
    $text=[Text.Encoding]::UTF8.GetString($head)
    if ($r.dropped -gt 0) { $text += ('...[' + $r.dropped + ' bytes dropped; tail follows]...') }
    if ($tail.Length -gt 0) { $text += [Text.Encoding]::UTF8.GetString($tail) }
    return $text
}
function Test-ReadyLine([string]$Candidate) {
    # The pinned owner assembly's strict parser and anchor validator are the only READY decoders: exact seven-field
    # canonical object, canonical anchor whose hash equals anchor_sha256, integer types, original interval and fresh/
    # recovered rules. Nothing else is interpreted as READY.
    $v=[MineruResidentWire]::Parse($Candidate,65536)
    $v.Keys([string[]]$ReadyFields)
    if ($v.Canonical() -cne $Candidate) { throw 'READY line is not canonical JSON' }
    $status=$v.Get('status').String()
    if ($resume) { if ($status -cne 'ready_recovered') { throw 'READY status is not ready_recovered' } } elseif ($status -cne 'ready_unbound') { throw 'READY status is not ready_unbound' }
    $anchorValue=$v.Get('anchor')
    $anchorRaw=$anchorValue.Raw
    if ([MineruM6OwnerWire]::Anchor($anchorValue) -cne $anchorRaw) { throw 'READY anchor is not the canonical owner anchor' }
    $anchorSha=$v.Get('anchor_sha256').String()
    if ($anchorSha -cnotmatch $HashPattern -or [MineruResidentWire]::Hash([MineruResidentWire]::Utf8.GetBytes($anchorRaw)) -cne $anchorSha) { throw 'READY anchor_sha256 differs from the canonical anchor bytes' }
    if ($anchorValue.Get('run_id').String() -cne $ExpectedRunId) { throw 'READY anchor names another run' }
    if ($anchorValue.Get('planned_seconds').Integer() -ne $PlannedSeconds) { throw 'READY anchor planned interval differs' }
    $anchorEpoch=$anchorValue.Get('owner_process_epoch_sha256').String()
    $epoch=$v.Get('owner_epoch_sha256').String()
    if ($epoch -cnotmatch $HashPattern) { throw 'READY owner epoch shape' }
    $prefixBytes=$v.Get('journal_prefix_bytes').Integer()
    $prefixSha=$v.Get('journal_prefix_sha256').String()
    if ($prefixSha -cnotmatch $HashPattern) { throw 'READY journal prefix hash shape' }
    $specValue=$v.Get('spec_sha256')
    if ($resume) {
        if ($anchorSha -cne $OriginalAnchorSha256) { throw 'READY anchor differs from the original anchor' }
        if ($anchorValue.Get('max_close_ticks').Integer() -ne $ResumeDeadlineTicks) { throw 'READY original close bound differs from the resume deadline' }
        if ($specValue.Raw -ceq 'null' -or $specValue.String() -cnotmatch $HashPattern) { throw 'recovered owner must report its original bound spec' }
        if ($epoch -ceq $anchorEpoch) { throw 'recovered owner must be a new incarnation of the original anchor' }
        if ($prefixBytes -lt 0 -or (($prefixBytes -eq 0) -ne ($prefixSha -ceq $EmptySha))) { throw 'recovered journal prefix length and hash disagree' }
    } else {
        if ($epoch -cne $anchorEpoch) { throw 'fresh owner epoch differs from its anchor' }
        if ($specValue.Raw -cne 'null') { throw 'fresh owner reports a bound spec' }
        if ($prefixBytes -ne 0 -or $prefixSha -cne $EmptySha) { throw 'fresh owner reports a journal prefix' }
    }
}
function Test-CancelCommand([byte[]]$Bytes,[string]$StartSha) {
    $value=$utf8.GetString($Bytes) | ConvertFrom-Json
    Assert-ExactFields $value $CancelFields 'cancel command'
    $expected=[ordered]@{contract_version='m6.owner-cancel.v1';process_start_record_sha256=$StartSha;run_id=$ExpectedRunId;attempt_id=$AttemptId;pid=[string]$process.Id;creation_filetime_100ns=[string]$birth;binary_sha256=$ExpectedBinarySha256;configuration_sha256=$ExpectedConfigurationSha256}
    foreach ($field in $expected.Keys) { if ([string](Get-Property $value $field) -cne [string]$expected[$field]) { throw ('cancel ' + $field + ' names another instance') } }
    $reason=[string](Get-Property $value 'reason')
    if ($reason.Length -lt 1 -or $reason.Length -gt 256 -or @($reason.ToCharArray() | Where-Object { [int]$_ -lt 32 -or [int]$_ -eq 127 }).Count -gt 0) { throw 'cancel reason shape' }
    return $reason
}
# One bounded stdout/stderr pump for live supervision and the post-exit drain: every completed read is retained,
# and the first stdout line is decoded as READY exactly once, wherever it is consumed. Returns 'idle' (nothing
# completed within the wait, or both pipes at EOF), 'progress' or 'failed' (a decisive READY failure was recorded).
function Receive-Stdout([int]$count) {
    Add-Retention $stdout $outBuf $count
    if ($script:readyDone) { return $false }
    $nl=[Array]::IndexOf($outBuf,[byte]10,0,$count)
    if ($nl -ge 0) { $script:readyLine.Write($outBuf,0,$nl); $script:readyDone=$true } else { $script:readyLine.Write($outBuf,0,$count) }
    if ($script:readyLine.Length -gt $RetainBytes) { $script:readyDone=$true; $script:readyLine.SetLength(0) }
    if (-not $script:readyDone) { return $false }
    try {
        $candidate=$utf8.GetString($script:readyLine.ToArray()).TrimEnd([char]13)   # strict UTF-8: invalid bytes are a failure, never rewritten
        Test-ReadyLine $candidate
        $script:ready=$candidate
    } catch { $script:failure='ready_invalid: ' + $_.Exception.Message; return $true }
    $script:readyUtc=[DateTime]::UtcNow.ToString('o'); $script:readyElapsed=$clock.ElapsedMilliseconds
    $records['ready']=Join-Path $attemptDir 'ready.json'
    $null=Write-NewRecord $records['ready'] $script:ready
    Emit 'M6-READY' $script:ready
    return $false
}
function Invoke-PipePump([int]$waitMilliseconds) {
    $pending=@(); if (-not $stdout.eof) { $pending += $script:outTask }; if (-not $stderr.eof) { $pending += $script:errTask }
    if ($pending.Count -eq 0) { return 'idle' }
    $index=[Threading.Tasks.Task]::WaitAny([Threading.Tasks.Task[]]$pending,$waitMilliseconds)
    if ($index -lt 0) { return 'idle' }
    $task=$pending[$index]
    $count=$task.GetAwaiter().GetResult()
    if ([object]::ReferenceEquals($task,$script:outTask)) {
        if ($count -eq 0) { $stdout.eof=$true } else {
            if (Receive-Stdout $count) { return 'failed' }
            $script:outTask=$outStream.ReadAsync($outBuf,0,$outBuf.Length)
        }
    } else {
        if ($count -eq 0) { $stderr.eof=$true } else { Add-Retention $stderr $errBuf $count; $script:errTask=$errStream.ReadAsync($errBuf,0,$errBuf.Length) }
    }
    return 'progress'
}
try {
    $pin=Load-PinnedBinary
    foreach ($path in @($privateRoot,$runsRoot,$stagingRoot,$attemptsRoot)) { $null=Assert-PrivateDirectory $path }
    if ([IO.Directory]::Exists($attemptDir) -or [IO.File]::Exists($attemptDir)) { throw 'attempt directory must be new' }
    # --- commit the staged private deployment (validated inside private staging, then moved atomically)
    if ([IO.File]::Exists($configurationPath)) {
        if (-not $resume) { throw 'a committed deployment already exists for this run; a fresh run needs a fresh workspace' }
        $null=Assert-PrivateFile $configurationPath
        if ((Sha256File $configurationPath) -cne $ExpectedConfigurationSha256) { throw 'committed deployment differs from the expected identity' }
    } else {
        $stagedPath=Join-Path $stagingRoot $StagedConfigurationName
        $null=Assert-CanonicalPath $stagedPath
        if (-not [IO.File]::Exists($stagedPath)) { throw 'staged deployment missing' }
        Assert-NoReparse $stagedPath
        $stagedBytes=Read-BoundedBytes $stagedPath 65536
        if ((Sha256Bytes $stagedBytes) -cne $ExpectedConfigurationSha256) { throw 'staged deployment hash differs' }
        $cfg=$utf8.GetString($stagedBytes) | ConvertFrom-Json
        Assert-ExactFields $cfg @('contract_version','run_id','run_root','mode','owner_source_sha256','expected_node_sha256','gpu_uuid','nvml_dll_sha256','port','resources','max_artifacts','max_artifact_bytes','maximum_lease_ticks','propagation_reserve_ticks','bootstrap_bind_seconds','roles') 'deployment'
        if ((Get-Property $cfg 'contract_version') -cne 'm6.owner-deployment.v2') { throw 'deployment contract version' }
        if ((Get-Property $cfg 'run_id') -cne $ExpectedRunId) { throw 'deployment names another run' }
        if ((Get-Property $cfg 'run_root') -cne $runsRoot) { throw 'deployment run_root is not this workspace private runs root' }
        $port=Get-Property $cfg 'port'
        if (($port -isnot [int] -and $port -isnot [long]) -or [long]$port -lt 1024 -or [long]$port -gt 65535) { throw 'deployment loopback port range' }
        $bootstrapSeconds=Get-Property $cfg 'bootstrap_bind_seconds'
        if (($bootstrapSeconds -isnot [int] -and $bootstrapSeconds -isnot [long]) -or [long]$bootstrapSeconds -lt 1 -or [long]$bootstrapSeconds -gt ($PlannedSeconds + $CloseGraceSeconds)) { throw 'deployment bootstrap bound range' }
        Set-PrivateFileOwner $stagedPath
        [IO.File]::Move($stagedPath,$configurationPath)
        $committedSddl=Assert-PrivateFile $configurationPath
        if ((Sha256File $configurationPath) -cne $ExpectedConfigurationSha256) { throw 'committed deployment read-back differs' }
        $records['deployment_commit']=Join-Path $attemptsRoot ('deployment-commit.' + $ExpectedRunId + '.json')
        $null=Write-NewRecord $records['deployment_commit'] ([ordered]@{contract_version='m6.owner-deployment-commit.v1';run_id=$ExpectedRunId;staged_name=$StagedConfigurationName;configuration_path=$configurationPath;configuration_sha256=$ExpectedConfigurationSha256;sddl=$committedSddl;bootstrap_bind_seconds=[long]$bootstrapSeconds;committed_utc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json -Compress)
    }
    $cfgBootstrap=[long](Get-Property ($utf8.GetString((Read-BoundedBytes $configurationPath 65536)) | ConvertFrom-Json) 'bootstrap_bind_seconds')
    [MineruM6PrivateStore]::CreatePrivateDirectory($attemptDir)
    $records['attempt_directory']=$attemptDir
    $stdout=New-Retention; $stderr=New-Retention
    $start=[Diagnostics.ProcessStartInfo]::new($BinaryPath)
    $start.Arguments='"'+$configurationPath+'" '+$ExpectedConfigurationSha256+' '+$ExpectedBinarySha256+' '+$PlannedSeconds+' '+$CloseGraceSeconds+' '+$MemoryBytes+' '+$ResumeDeadlineTicks+' '+$OriginalAnchorSha256
    $start.UseShellExecute=$false
    $start.RedirectStandardOutput=$true
    $start.RedirectStandardError=$true
    $start.WorkingDirectory=$attemptDir
    $clock.Restart()   # one deadline from spawn, never renewed
    $process=[Diagnostics.Process]::Start($start)
    $exactHandle=$process.Handle
    $birth=$process.StartTime.ToUniversalTime().ToFileTimeUtc()
    $records['process_start']=Join-Path $attemptDir 'process-start.json'
    $startSha=Write-NewRecord $records['process_start'] ([ordered]@{contract_version='m6.owner-external-start.v2';hostname=$nativeHostname;run_id=$ExpectedRunId;attempt_id=$AttemptId;workspace_root=$WorkspaceRoot;pid=$process.Id;creation_filetime_100ns=$birth;planned_seconds=$PlannedSeconds;close_grace_seconds=$CloseGraceSeconds;memory_bytes=$MemoryBytes;bootstrap_bind_seconds=$cfgBootstrap;ready_wait_seconds=$ReadyWaitSeconds;total_wait_milliseconds=$totalMilliseconds;resume=$resume;original_anchor_sha256=$OriginalAnchorSha256;binary_sha256=$ExpectedBinarySha256;configuration_sha256=$ExpectedConfigurationSha256;configuration_path=$configurationPath;launcher_sha256=$launcherSha;started_utc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json -Compress)
    $outStream=$process.StandardOutput.BaseStream; $errStream=$process.StandardError.BaseStream
    $outBuf=[byte[]]::new(16384); $errBuf=[byte[]]::new(16384)
    $outTask=$outStream.ReadAsync($outBuf,0,$outBuf.Length); $errTask=$errStream.ReadAsync($errBuf,0,$errBuf.Length)
    $readyLine=[IO.MemoryStream]::new(); $readyDone=$false
    $cancelPath=Join-Path $attemptDir 'cancel.json'
    # Supervision ends only when the held process handle signals exit (or the deadline/READY bound/cancel ends it):
    # both pipes reaching EOF is not process exit and never stops READY deadline or cancel polling. Output already
    # produced by a child that exits at once is consumed by the same pump in the post-exit drain below.
    while (-not $process.WaitForExit(0)) {
        $remaining=$totalMilliseconds - $clock.ElapsedMilliseconds
        if ($remaining -le 0) { break }
        $state=Invoke-PipePump ([int][Math]::Min($remaining,250))
        if ($state -eq 'failed') { break }
        if (-not $readyDone -and $clock.ElapsedMilliseconds -gt ($ReadyWaitSeconds * 1000)) { $readyTimeout=$true; $failure='ready_timeout: no valid READY line within ' + $ReadyWaitSeconds + ' seconds'; break }
        if ($null -eq $cancelAccepted -and [IO.File]::Exists($cancelPath)) {
            $cancelBytes=$null
            try { $cancelBytes=Read-BoundedBytes $cancelPath 4096 } catch { $cancelBytes=$null }
            if ($null -ne $cancelBytes) {
                $cancelSha=Sha256Bytes $cancelBytes
                if ($cancelSha -cne $cancelRejectedSha) {
                    try {
                        $reason=Test-CancelCommand $cancelBytes $startSha
                        $cancelAccepted=[ordered]@{command_sha256=$cancelSha;reason=$reason;accepted_utc=[DateTime]::UtcNow.ToString('o');elapsed_milliseconds=$clock.ElapsedMilliseconds}
                        # Terminate through the handle held since spawn: the exact instance, never a PID lookup.
                        # forced_termination records only a Kill actually issued; a child already gone is not forced.
                        if (-not $process.HasExited) { $process.Kill(); $forced=$true }
                    } catch {
                        $cancelRejectedSha=$cancelSha
                        $rejectedPath=Join-Path $attemptDir ('cancel-rejected.' + $cancelSha.Substring(7,16) + '.json')
                        if (-not [IO.File]::Exists($rejectedPath)) { $null=Write-NewRecord $rejectedPath ([ordered]@{contract_version='m6.owner-cancel-rejected.v1';command_sha256=$cancelSha;reason=$_.Exception.Message;observed_utc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json -Compress) }
                    }
                }
            }
        }
        if ($state -eq 'idle' -and $stdout.eof -and $stderr.eof) { $null=$process.WaitForExit([int][Math]::Min([Math]::Max(1,$totalMilliseconds - $clock.ElapsedMilliseconds),250)) }
    }
    if ($null -eq $failure) {
        $remaining=[Math]::Max(0,$totalMilliseconds - $clock.ElapsedMilliseconds)
        $exited=$process.WaitForExit([int][Math]::Min($remaining,[int]::MaxValue))
        if ($exited) {
            $exitCode=$process.ExitCode
            # Bounded drain of whatever the exited child left in its pipes, through the same pump (READY included).
            $drainUntil=$clock.ElapsedMilliseconds + 5000
            while (-not ($stdout.eof -and $stderr.eof) -and $clock.ElapsedMilliseconds -lt $drainUntil) {
                $state=Invoke-PipePump 250
                if ($state -eq 'failed') { break }
            }
        }
    }
} catch {
    $failure=$_.Exception.Message
    if ($null -eq $process) { $identityFailure=$true }
} finally {
    if ($null -ne $process) {
        if (-not $exited) {
            # Ownership never lapses: a child alive after failure, READY timeout, cancel or deadline is terminated
            # through the held handle within a bounded cleanup, then reaped.
            try { if (-not $process.HasExited) { $process.Kill(); $forced=$true } } catch { if ($null -eq $failure) { $failure='cleanup kill failed: ' + $_.Exception.Message } }
            $exited=$process.WaitForExit(30000)
            if ($exited) { try { $exitCode=$process.ExitCode } catch { } }
        }
        $record=[ordered]@{
            contract_version='m6.owner-external-exit.v2';scope='production_owner_run';hostname=$nativeHostname
            run_id=$ExpectedRunId;attempt_id=$AttemptId
            pid=$process.Id;creation_filetime_100ns=$birth;exact_process_handle_opened=($exactHandle -ne [IntPtr]::Zero)
            process_handle_signaled=$exited;exit_code=$exitCode;ready_received=($null -ne $ready);ready_observed_utc=$readyUtc;ready_elapsed_milliseconds=$readyElapsed
            ready_timeout=$readyTimeout;forced_termination=$forced;cancel=$cancelAccepted;parent_failure=$failure
            elapsed_milliseconds=$clock.ElapsedMilliseconds;total_wait_milliseconds=$totalMilliseconds
            stdout_total_bytes=$stdout.total;stderr_total_bytes=$stderr.total;stdout_eof=$stdout.eof;stderr_eof=$stderr.eof
            stdout_retained=(Get-RetentionText $stdout);stderr_retained=(Get-RetentionText $stderr)
            stdout_dropped_bytes=$stdout.dropped;stderr_dropped_bytes=$stderr.dropped
            binary_sha256=$ExpectedBinarySha256;configuration_sha256=$ExpectedConfigurationSha256;launcher_sha256=$launcherSha
            finished_utc=[DateTime]::UtcNow.ToString('o')
        }
        $raw=$record | ConvertTo-Json -Compress -Depth 5
        # The exit record is the external proof of the attempt; if it cannot be persisted the launch fails visibly
        # even when the child itself succeeded, and the record still goes to stdout for the controller.
        $records['process_exit']=Join-Path $attemptDir 'process-exit.json'
        try { $null=Write-NewRecord $records['process_exit'] $raw; $recordPersisted=$true } catch { $recordPersisted=$false; $recordError=$_.Exception.Message }
        Emit 'M6-EXIT' $raw
        $process.Dispose()
    }
    if ($null -ne $pin) { $pin.Dispose() }
}
$code=0
if ($null -ne $process -and -not $recordPersisted) { $firstError='exit record could not be persisted (' + $recordError + '); no durable external exit proof; record printed above only'; $code=70 }
elseif ($identityFailure) { $firstError='identity: ' + $failure; $code=65 }
elseif ($readyTimeout) { $firstError=$failure; $code=3 }
elseif ($null -ne $failure) { $firstError='run: ' + $failure; $code=70 }
elseif (-not $exited) { $firstError='owner process did not exit even after bounded cleanup; investigate before any retry'; $code=70 }
elseif ($forced) { $firstError=$(if ($null -ne $cancelAccepted) { 'cancelled: ' + $cancelAccepted.reason } else { 'owner process exceeded its deadline and was terminated' }); $code=70 }
elseif ($null -eq $ready) { $firstError='owner host never reported READY'; $code=70 }
elseif ($exitCode -ne 0) { $firstError='owner host exited ' + $exitCode; $code=4 }
Finish $code @{exit_code_host=$exitCode;forced_termination=$forced;cancel=$cancelAccepted;ready_received=($null -ne $ready)}
