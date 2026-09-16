<#
.SYNOPSIS
Own one production run of the native M6 owner host: pinned binary and private deployment, exact process
handle from spawn to verified exit, bounded continuous capture of both pipes, one absolute deadline, and an
external exit record that never trusts the child's own intent.

.DESCRIPTION
Derived from the zero-PDF live-control parent fixture with the fixture-only bounds removed (no crash
injection; planned plus grace up to the host's own 7200-second ceiling). The child enters its own finite
self-Job. This parent records PID and creation time before any protocol observation, drains stdout and
stderr continuously into bounded retention (first and last 64 KiB of each, with dropped byte counts) so a
full pipe can never block the child, captures the single READY line, and waits for the real exit against
one deadline measured from spawn. If the parent fails or the deadline passes while the child is alive, the
child is terminated within a bounded cleanup and the record says so: ownership never lapses silently.
Run it in a process that outlives the launching session. Machine execution policy is not changed.
#>
param(
    [Parameter(Mandatory=$true)][string]$BinaryPath,
    [Parameter(Mandatory=$true)][string]$ExpectedBinarySha256,
    [Parameter(Mandatory=$true)][string]$ConfigurationPath,
    [Parameter(Mandatory=$true)][string]$ExpectedConfigurationSha256,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [Parameter(Mandatory=$true)][string]$ExpectedHostname,
    [Parameter(Mandatory=$true)][long]$PlannedSeconds,
    [Parameter(Mandatory=$true)][long]$CloseGraceSeconds,
    [long]$MemoryBytes=536870912,
    [long]$ResumeDeadlineTicks=0,
    [string]$OriginalAnchorSha256='none',
    [long]$ExitWaitExtraSeconds=180,
    [long]$ReadyWaitSeconds=30
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'Windows PowerShell 5.1 is required' }
if (-not [Environment]::Is64BitProcess) { throw '64-bit PowerShell is required' }
$nativeHostname=[Environment]::MachineName
if ($nativeHostname -cne $ExpectedHostname) { throw 'Native host differs' }
if ($PlannedSeconds -lt 1 -or $CloseGraceSeconds -lt 1 -or ($PlannedSeconds + $CloseGraceSeconds) -gt 7200) { throw 'Planned plus grace seconds must be within the owner host ceiling' }
if ($MemoryBytes -lt 67108864 -or $ExitWaitExtraSeconds -lt 30 -or $ExitWaitExtraSeconds -gt 3600 -or $ReadyWaitSeconds -lt 5 -or $ReadyWaitSeconds -gt 300) { throw 'Memory bound or wait bounds out of range' }
if ($ExpectedBinarySha256 -cnotmatch '^sha256:[0-9a-f]{64}$' -or $ExpectedConfigurationSha256 -cnotmatch '^sha256:[0-9a-f]{64}$') { throw 'Explicit artifact identities required' }
if (($ResumeDeadlineTicks -gt 0) -ne ($OriginalAnchorSha256 -ne 'none')) { throw 'Resume requires both the original deadline and anchor hash' }
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $BinaryPath).Hash.ToLowerInvariant() -cne $ExpectedBinarySha256.Substring(7)) { throw 'Owner executable drift' }
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $ConfigurationPath).Hash.ToLowerInvariant() -cne $ExpectedConfigurationSha256.Substring(7)) { throw 'Private deployment drift' }
if (Test-Path -LiteralPath $OutputDirectory) { throw 'Output directory must be new' }

$RetainBytes=65536
$utf8=[Text.UTF8Encoding]::new($false,$true)
function Write-NewRecord([string]$Name,[string]$Json) {
    $bytes=$utf8.GetBytes($Json)
    $file=[IO.FileStream]::new((Join-Path $OutputDirectory $Name),[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try { $file.Write($bytes,0,$bytes.Length); $file.Flush($true) } finally { $file.Dispose() }
}
# Bounded retention of one pipe: first and last RetainBytes, plus the count of everything in between.
function New-Retention { return [ordered]@{head=[IO.MemoryStream]::new();tail=[IO.MemoryStream]::new();total=[long]0;eof=$false} }
function Add-Retention($r,[byte[]]$buffer,[int]$count) {
    $r.total += $count
    $headRoom=$RetainBytes - [int]$r.head.Length
    if ($headRoom -gt 0) { $take=[Math]::Min($headRoom,$count); $r.head.Write($buffer,0,$take) }
    $r.tail.Write($buffer,0,$count)
    if ($r.tail.Length -gt (2 * $RetainBytes)) {
        $keep=$r.tail.ToArray(); $start=$keep.Length - $RetainBytes
        $r.tail.SetLength(0); $r.tail.Write($keep,$start,$RetainBytes)
    }
}
function Get-RetentionText($r) {
    $head=$r.head.ToArray(); $tail=$r.tail.ToArray()
    $tailStart=[Math]::Max(0,$tail.Length - $RetainBytes)
    $text=[Text.Encoding]::UTF8.GetString($head)
    if ($r.total -gt $head.Length) { $text += ('...[' + ($r.total - $head.Length) + ' more bytes; tail follows]...' + [Text.Encoding]::UTF8.GetString($tail,$tailStart,$tail.Length - $tailStart)) }
    return $text
}

$pin=[IO.FileStream]::new($BinaryPath,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
$process=$null
$stdout=New-Retention; $stderr=New-Retention
$ready=$null; $exited=$false; $forced=$false; $failure=$null; $exitCode=$null; $birth=$null; $exactHandle=[IntPtr]::Zero
$recordPersisted=$true; $recordError=$null
$clock=[Diagnostics.Stopwatch]::StartNew()
$totalMilliseconds=[long](($PlannedSeconds + $CloseGraceSeconds + $ExitWaitExtraSeconds) * 1000)
try {
    $hash=[Security.Cryptography.SHA256]::Create()
    try { $pinnedHash=([BitConverter]::ToString($hash.ComputeHash($pin))).Replace('-','').ToLowerInvariant() } finally { $hash.Dispose() }
    if ($pinnedHash -cne $ExpectedBinarySha256.Substring(7)) { throw 'Pinned owner executable drift' }
    $null=[Reflection.Assembly]::LoadFile($BinaryPath)
    [MineruM6PrivateStore]::CreatePrivateDirectory($OutputDirectory)
    $start=[Diagnostics.ProcessStartInfo]::new($BinaryPath)
    $start.Arguments='"'+$ConfigurationPath+'" '+$ExpectedConfigurationSha256+' '+$ExpectedBinarySha256+' '+$PlannedSeconds+' '+$CloseGraceSeconds+' '+$MemoryBytes+' '+$ResumeDeadlineTicks+' '+$OriginalAnchorSha256
    $start.UseShellExecute=$false
    $start.RedirectStandardOutput=$true
    $start.RedirectStandardError=$true
    $start.WorkingDirectory=$OutputDirectory
    $clock.Restart()   # one deadline from spawn
    $process=[Diagnostics.Process]::Start($start)
    $exactHandle=$process.Handle
    $birth=$process.StartTime.ToUniversalTime().ToFileTimeUtc()
    Write-NewRecord 'process-start.json' ([ordered]@{contract_version='m6.owner-external-start.v1';hostname=$nativeHostname;pid=$process.Id;creation_filetime_100ns=$birth;planned_seconds=$PlannedSeconds;close_grace_seconds=$CloseGraceSeconds;memory_bytes=$MemoryBytes;total_wait_milliseconds=$totalMilliseconds;binary_sha256=$ExpectedBinarySha256;configuration_sha256=$ExpectedConfigurationSha256;started_utc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json -Compress)
    $outStream=$process.StandardOutput.BaseStream; $errStream=$process.StandardError.BaseStream
    $outBuf=[byte[]]::new(16384); $errBuf=[byte[]]::new(16384)
    $outTask=$outStream.ReadAsync($outBuf,0,$outBuf.Length); $errTask=$errStream.ReadAsync($errBuf,0,$errBuf.Length)
    $readyLine=[IO.MemoryStream]::new(); $readyDone=$false
    while (-not ($stdout.eof -and $stderr.eof)) {
        $remaining=$totalMilliseconds - $clock.ElapsedMilliseconds
        if ($remaining -le 0) { break }
        $pending=@(); if (-not $stdout.eof) { $pending += $outTask }; if (-not $stderr.eof) { $pending += $errTask }
        $index=[Threading.Tasks.Task]::WaitAny([Threading.Tasks.Task[]]$pending,[int][Math]::Min($remaining,1000))
        if ($index -lt 0) {
            if (-not $readyDone -and $clock.ElapsedMilliseconds -gt ($ReadyWaitSeconds * 1000)) { $readyDone=$true }
            continue
        }
        $task=$pending[$index]
        $count=$task.GetAwaiter().GetResult()
        if ([object]::ReferenceEquals($task,$outTask)) {
            if ($count -eq 0) { $stdout.eof=$true } else {
                Add-Retention $stdout $outBuf $count
                if (-not $readyDone) {
                    $nl=[Array]::IndexOf($outBuf,[byte]10,0,$count)
                    if ($nl -ge 0) { $readyLine.Write($outBuf,0,$nl); $readyDone=$true } else { $readyLine.Write($outBuf,0,$count) }
                    if ($readyLine.Length -gt $RetainBytes) { $readyDone=$true; $readyLine.SetLength(0) }
                    if ($readyDone -and $readyLine.Length -gt 0) {
                        $candidate=[Text.Encoding]::UTF8.GetString($readyLine.ToArray()).TrimEnd([char]13)
                        try { $null=$candidate | ConvertFrom-Json; $ready=$candidate; Write-NewRecord 'ready.json' $ready } catch { $ready=$null }
                    }
                }
                $outTask=$outStream.ReadAsync($outBuf,0,$outBuf.Length)
            }
        } else {
            if ($count -eq 0) { $stderr.eof=$true } else { Add-Retention $stderr $errBuf $count; $errTask=$errStream.ReadAsync($errBuf,0,$errBuf.Length) }
        }
    }
    $remaining=[Math]::Max(0,$totalMilliseconds - $clock.ElapsedMilliseconds)
    $exited=$process.WaitForExit([int][Math]::Min($remaining,[int]::MaxValue))
    if ($exited) { $exitCode=$process.ExitCode }
} catch {
    $failure=$_.Exception.Message
} finally {
    if ($null -ne $process) {
        if (-not $exited) {
            # Ownership never lapses: a child alive after failure or deadline is terminated within a bounded cleanup.
            try { if (-not $process.HasExited) { $process.Kill() }; $forced=$true } catch { if ($null -eq $failure) { $failure='cleanup kill failed: ' + $_.Exception.Message } }
            $exited=$process.WaitForExit(30000)
            if ($exited) { try { $exitCode=$process.ExitCode } catch { } }
        }
        $record=[ordered]@{
            contract_version='m6.owner-external-exit.v1';scope='production_owner_run';hostname=$nativeHostname
            pid=$process.Id;creation_filetime_100ns=$birth;exact_process_handle_opened=($exactHandle -ne [IntPtr]::Zero)
            process_handle_signaled=$exited;exit_code=$exitCode;ready_received=($null -ne $ready)
            forced_termination=$forced;parent_failure=$failure;elapsed_milliseconds=$clock.ElapsedMilliseconds;total_wait_milliseconds=$totalMilliseconds
            stdout_total_bytes=$stdout.total;stderr_total_bytes=$stderr.total
            binary_sha256=$ExpectedBinarySha256;configuration_sha256=$ExpectedConfigurationSha256
            finished_utc=[DateTime]::UtcNow.ToString('o');stdout_retained=(Get-RetentionText $stdout);stderr_retained=(Get-RetentionText $stderr)
        }
        $raw=$record | ConvertTo-Json -Compress -Depth 5
        # The exit record is the external proof of the run; if it cannot be persisted the launch fails visibly
        # even when the child itself succeeded, and the record still goes to stdout for the caller.
        try { Write-NewRecord 'process-exit.json' $raw; $recordPersisted=$true } catch { $recordPersisted=$false; $recordError=$_.Exception.Message }
        $raw
        $process.Dispose()
    }
    $pin.Dispose()
}
if ($null -ne $process -and -not $recordPersisted) { throw ('Owner exit record could not be persisted (' + $recordError + '); the run has no durable external exit proof; record printed above only') }
if ($null -ne $failure) { throw ('Owner launch failed: ' + $failure + '; child ' + $(if ($forced) { 'terminated by bounded cleanup' } else { 'exited' }) + '; original record retained') }
if (-not $exited) { throw 'Owner process did not exit even after bounded cleanup; investigate before any retry' }
if ($forced) { throw 'Owner process exceeded its deadline and was terminated; original record retained' }
if ($null -eq $ready) { throw 'Owner host never reported READY; original result retained' }
if ($exitCode -ne 0) { throw ('Owner host exited ' + $exitCode + '; original result retained') }
