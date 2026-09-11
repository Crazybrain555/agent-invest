param(
    [Parameter(Mandatory=$true)][string]$BinaryPath,
    [Parameter(Mandatory=$true)][string]$ExpectedBinarySha256,
    [Parameter(Mandatory=$true)][string]$ConfigurationPath,
    [Parameter(Mandatory=$true)][string]$ExpectedConfigurationSha256,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [Parameter(Mandatory=$true)][string]$ExpectedHostname,
    [long]$PlannedSeconds=30,
    [long]$CloseGraceSeconds=50,
    [long]$ResumeDeadlineTicks=0,
    [string]$OriginalAnchorSha256='none',
    [switch]$InjectCrashAfterSignal
)
# Explicit opt-in, zero-PDF native lifecycle harness. The caller drives the fixed
# protocol separately. Hold the exact process handle and record its real exit;
# neither a ready line, socket EOF nor the child's exit-intent proves termination.
$ErrorActionPreference='Stop'
$nativeHostname=[Environment]::MachineName
if($nativeHostname -cne $ExpectedHostname){throw 'Native host differs'}
if($PlannedSeconds -lt 1 -or $CloseGraceSeconds -lt 1 -or ($PlannedSeconds+$CloseGraceSeconds) -gt 90){throw 'Bounded short control fixture required'}
if($ExpectedBinarySha256 -cnotmatch '^sha256:[0-9a-f]{64}$' -or $ExpectedConfigurationSha256 -cnotmatch '^sha256:[0-9a-f]{64}$'){throw 'Explicit artifact identities required'}
if((Get-FileHash -LiteralPath $BinaryPath).Hash.ToLowerInvariant() -cne $ExpectedBinarySha256.Substring(7)){throw 'Owner executable drift'}
if((Get-FileHash -LiteralPath $ConfigurationPath).Hash.ToLowerInvariant() -cne $ExpectedConfigurationSha256.Substring(7)){throw 'Private deployment drift'}
$pin=[IO.FileStream]::new($BinaryPath,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
$process=$null
try{
    $hash=[Security.Cryptography.SHA256]::Create()
    try{$pinnedHash=([BitConverter]::ToString($hash.ComputeHash($pin))).Replace('-','').ToLowerInvariant()}finally{$hash.Dispose()}
    if($pinnedHash -cne $ExpectedBinarySha256.Substring(7)){throw 'Pinned owner executable drift'}
    $null=[Reflection.Assembly]::LoadFile($BinaryPath)
    [MineruM6PrivateStore]::CreatePrivateDirectory($OutputDirectory)
    $start=[Diagnostics.ProcessStartInfo]::new($BinaryPath)
    $start.Arguments='"'+$ConfigurationPath+'" '+$ExpectedConfigurationSha256+' '+$ExpectedBinarySha256+' '+$PlannedSeconds+' '+$CloseGraceSeconds+' 536870912 '+$ResumeDeadlineTicks+' '+$OriginalAnchorSha256
    $start.UseShellExecute=$false
    $start.RedirectStandardOutput=$true
    $start.RedirectStandardError=$true
    $process=[Diagnostics.Process]::Start($start)
    $exactHandle=$process.Handle
    $birth=$process.StartTime.ToUniversalTime().ToFileTimeUtc()
    $utf8=[Text.UTF8Encoding]::new($false,$true)
    $started=[ordered]@{pid=$process.Id;creation_filetime_100ns=$birth}|ConvertTo-Json -Compress
    $bytes=$utf8.GetBytes($started)
    $file=[IO.FileStream]::new((Join-Path $OutputDirectory 'process-start.json'),[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try{$file.Write($bytes,0,$bytes.Length);$file.Flush($true)}finally{$file.Dispose()}
    $stderr=$process.StandardError.ReadToEndAsync()
    $first=$process.StandardOutput.ReadLineAsync()
    $ready=$null
    if($first.Wait(15000)){$ready=$first.GetAwaiter().GetResult()}
    if($null -ne $ready){
        $null=$ready|ConvertFrom-Json
        $bytes=$utf8.GetBytes($ready)
        $file=[IO.FileStream]::new((Join-Path $OutputDirectory 'ready.json'),[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try{$file.Write($bytes,0,$bytes.Length);$file.Flush($true)}finally{$file.Dispose()}
    }
    $faultInjected=$false
    if($InjectCrashAfterSignal -and $null -ne $ready){
        # Only this newly started process may be fault-injected. The controller
        # must name its exact PID/birth after a durable protocol observation.
        $signal=Join-Path $OutputDirectory 'crash-request.json'
        $until=[Diagnostics.Stopwatch]::StartNew()
        while(-not $process.HasExited -and $until.Elapsed.TotalSeconds -lt 20){
            if([IO.File]::Exists($signal)){
                if((Get-Item -LiteralPath $signal).Length -gt 1024){throw 'Crash signal exceeds test bound'}
                $request=[IO.File]::ReadAllText($signal)|ConvertFrom-Json
                if(@($request.PSObject.Properties.Name).Count -ne 2 -or $request.pid -ne $process.Id -or $request.creation_filetime_100ns -ne $birth){throw 'Crash signal does not identify this child'}
                $process.Kill()
                $faultInjected=$true
                break
            }
            Start-Sleep -Milliseconds 50
        }
        if(-not $faultInjected){throw 'Explicit fixture crash signal was not received'}
    }
    # The process owns its finite Job. Do not terminate any existing service or
    # reuse a PID to infer completion. Keep the opened process handle throughout.
    if(-not $process.WaitForExit(95000)){throw 'Owned finite process has not exited; retain handle evidence and investigate'}
    if(-not $first.IsCompleted){throw 'Exited process stdout did not complete'}
    $remaining=$process.StandardOutput.ReadToEnd()
    $errors=$stderr.GetAwaiter().GetResult()
    $result=[ordered]@{
        contract_version='m6.owner-external-exit.v1';scope='zero_pdf_native_control_fixture';hostname=$nativeHostname
        pid=$process.Id;creation_filetime_100ns=$birth;exact_process_handle_opened=($exactHandle -ne [IntPtr]::Zero)
        process_handle_signaled=$process.HasExited;exit_code=$process.ExitCode;ready_received=($null -ne $ready)
        explicit_fixture_crash_injected=$faultInjected
        binary_sha256=$ExpectedBinarySha256;configuration_sha256=$ExpectedConfigurationSha256
        stdout_tail=$remaining;stderr=$errors
    }
    $raw=$result|ConvertTo-Json -Compress -Depth 5
    $bytes=$utf8.GetBytes($raw)
    $file=[IO.FileStream]::new((Join-Path $OutputDirectory 'process-exit.json'),[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try{$file.Write($bytes,0,$bytes.Length);$file.Flush($true)}finally{$file.Dispose()}
    $raw
    if($null -eq $ready -or (-not $faultInjected -and $process.ExitCode -ne 0) -or ($faultInjected -and $process.ExitCode -eq 0)){throw 'Native owner exit differs from the selected fixture; original result retained'}
}finally{
    if($null -ne $process){$process.Dispose()}
    $pin.Dispose()
}
