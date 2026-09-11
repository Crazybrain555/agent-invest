param(
    [Parameter(Mandatory=$true)][string]$AssemblyPath,
    [Parameter(Mandatory=$true)][string]$ExpectedAssemblySha256,
    [Parameter(Mandatory=$true)][string]$ProbePath,
    [Parameter(Mandatory=$true)][string]$ExpectedProbeSha256,
    [Parameter(Mandatory=$true)][string]$OutputDirectory
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$pins=[Collections.Generic.List[IO.FileStream]]::new()
$checks=[Collections.Generic.List[object]]::new()
function Pin-Exact([string]$Path,[string]$Expected) {
    if(-not [IO.Path]::IsPathRooted($Path) -or $Expected -cnotmatch '\A[0-9a-f]{64}\z'){throw 'Exact absolute input required'}
    $file=[IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read);$pins.Add($file)
    $hash=[Security.Cryptography.SHA256]::Create()
    try{$actual=([BitConverter]::ToString($hash.ComputeHash($file))).Replace('-','').ToLowerInvariant()}finally{$hash.Dispose()}
    if($actual -cne $Expected){throw 'Self-Job parent input drift'}
}
try {
    if(Test-Path -LiteralPath $OutputDirectory){throw 'New qualification directory required'}
    $null=New-Item -ItemType Directory -Path $OutputDirectory
    Pin-Exact $AssemblyPath $ExpectedAssemblySha256
    Pin-Exact $ProbePath $ExpectedProbeSha256
    $powershell=Join-Path $PSHOME 'powershell.exe'
    foreach($mode in @('normal','expire')){
        $receiptPath=Join-Path $OutputDirectory ($mode+'.json')
        foreach($arg in @($ProbePath,$AssemblyPath,$receiptPath)){if($arg.Contains('"')){throw 'Unexpected quote in fixed local test path'}}
        $start=[Diagnostics.ProcessStartInfo]::new($powershell)
        $start.Arguments='-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'+$ProbePath+'" -Mode '+$mode+' -AssemblyPath "'+$AssemblyPath+'" -ExpectedAssemblySha256 '+$ExpectedAssemblySha256+' -ReceiptPath "'+$receiptPath+'"'
        $start.UseShellExecute=$false;$start.RedirectStandardOutput=$true;$start.RedirectStandardError=$true
        $parentStart=[Diagnostics.Stopwatch]::GetTimestamp()
        $process=[Diagnostics.Process]::Start($start)
        try {
            $pidOwned=$process.Id;$birthOwned=$process.StartTime.ToUniversalTime().ToFileTimeUtc()
            $stdout=$process.StandardOutput.ReadToEndAsync();$stderr=$process.StandardError.ReadToEndAsync()
            $signaled=$process.WaitForExit(10000)
            if(-not $signaled){
                # This exact handle belongs to the child started above. It has
                # its own self-Job; never select a process by name or reused PID.
                $process.Kill();$null=$process.WaitForExit(3000)
                throw 'Self-Job failed its finite process-exit qualification'
            }
            if(-not $stdout.Wait(1000) -or -not $stderr.Wait(1000)){throw 'Self-Job output pipes did not close'}
            $receipt=Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
            $expectedExit=if($mode -ceq 'normal'){0}else{124}
            if($process.ExitCode -ne $expectedExit -or $receipt.pid -ne $pidOwned -or
               $receipt.creation_filetime_100ns -ne $birthOwned -or -not $receipt.no_children_assertion_passed){throw 'Self-Job physical identity/exit differs'}
            $checks.Add([ordered]@{mode=$mode;exit_code=$process.ExitCode;process_handle_signaled=$signaled;
                parent_pid=$pidOwned;parent_creation_filetime_100ns=$birthOwned;parent_start_ticks=$parentStart;
                parent_end_ticks=[Diagnostics.Stopwatch]::GetTimestamp();child_receipt=$receipt;
                stdout=$stdout.Result;stderr=$stderr.Result})
        }finally{$process.Dispose()}
    }
    $result=[ordered]@{contract_version='m6.self-job-qualification.v1';status='pass';checks=$checks.ToArray();
        assembly_sha256=$ExpectedAssemblySha256;probe_sha256=$ExpectedProbeSha256;
        powershell_sha256=(Get-FileHash -LiteralPath $powershell -Algorithm SHA256).Hash.ToLowerInvariant();
        frequency_hz=[Diagnostics.Stopwatch]::Frequency;active_business_services_changed=$false}
    $json=$result | ConvertTo-Json -Depth 7 -Compress
    $bytes=[Text.UTF8Encoding]::new($false,$true).GetBytes($json)
    $file=[IO.FileStream]::new((Join-Path $OutputDirectory 'parent-receipt.json'),[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try{$file.Write($bytes,0,$bytes.Length);$file.Flush($true)}finally{$file.Dispose()}
    $json
} finally {foreach($pin in $pins){$pin.Dispose()}}
