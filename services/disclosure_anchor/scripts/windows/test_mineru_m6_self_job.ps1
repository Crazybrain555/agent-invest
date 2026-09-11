param(
    [Parameter(Mandatory=$true)][ValidateSet('normal','expire')][string]$Mode,
    [Parameter(Mandatory=$true)][string]$AssemblyPath,
    [Parameter(Mandatory=$true)][string]$ExpectedAssemblySha256,
    [Parameter(Mandatory=$true)][string]$ReceiptPath
)
$t0=[Diagnostics.Stopwatch]::GetTimestamp()
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$pin=[IO.FileStream]::new($AssemblyPath,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
try {
    if($pin.Length -gt 1048576){throw 'Assembly bound exceeded'}
    $hash=[Security.Cryptography.SHA256]::Create()
    try {$actual=([BitConverter]::ToString($hash.ComputeHash($pin))).Replace('-','').ToLowerInvariant()}
    finally {$hash.Dispose()}
    if($actual -cne $ExpectedAssemblySha256){throw 'Self-Job test assembly identity drift'}
    $null=[Reflection.Assembly]::LoadFile($AssemblyPath)
    $deadline=$t0+[Diagnostics.Stopwatch]::Frequency*3
    $job=[MineruM6SelfJob]::Enter($deadline,536870912)
    $job.AssertNoChildren()
    $process=[Diagnostics.Process]::GetCurrentProcess()
    try {
        $result=[ordered]@{contract_version='m6.self-job-probe.v1'; mode=$Mode; pid=$process.Id;
            creation_filetime_100ns=$process.StartTime.ToUniversalTime().ToFileTimeUtc();
            t0_ticks=$t0; deadline_ticks=$deadline; observed_ticks=[Diagnostics.Stopwatch]::GetTimestamp();
            frequency_hz=[Diagnostics.Stopwatch]::Frequency; no_children_assertion_passed=$true}
    } finally {$process.Dispose()}
    $bytes=[Text.UTF8Encoding]::new($false,$true).GetBytes(($result | ConvertTo-Json -Compress))
    $output=[IO.FileStream]::new($ReceiptPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try {$output.Write($bytes,0,$bytes.Length);$output.Flush($true)} finally {$output.Dispose()}
    if($Mode -eq 'expire'){
        while($true){[Threading.Thread]::Sleep(100)}
    }
    # Do not explicitly close the KILL_ON_CLOSE self-Job before process exit.
    # The independent parent must wait for this exact process handle to signal.
} finally {$pin.Dispose()}
exit 0
