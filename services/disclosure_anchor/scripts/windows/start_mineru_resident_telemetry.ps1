param(
    [Parameter(Mandatory = $true)][string]$ConfigJsonPath,
    [Parameter(Mandatory = $true)][string]$ExpectedConfigSha256
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$pins = [Collections.Generic.List[IO.FileStream]]::new()
$failures = [Collections.Generic.List[Exception]]::new()
$failureContexts = [Collections.Generic.List[string]]::new()
function Add-MineruFailure($ErrorRecord) {
    $failures.Add($ErrorRecord.Exception)
    $failureContexts.Add([string]$ErrorRecord.ScriptStackTrace)
}
function Write-MineruFailureDetail([string]$Role,[string]$ArtifactName) {
    # PowerShell prints only the outer AggregateException message, so the full
    # nested chain (Exception.ToString includes every inner exception) is echoed
    # to stderr and persisted next to the session artifacts before the rethrow.
    # Guarded end to end: a failure here is reported and never replaces the
    # original terminal throw.
    try {
        $lines = [Collections.Generic.List[string]]::new()
        $lines.Add($Role + ' failure detail: ' + $failures.Count + ' failure(s)')
        for ($index = 0; $index -lt $failures.Count; $index++) {
            $lines.Add('[' + $index + '] ' + $failures[$index].ToString())
            if ($index -lt $failureContexts.Count -and $failureContexts[$index].Length -gt 0) { $lines.Add('    script: ' + $failureContexts[$index]) }
        }
        $text = $lines -join "`n"
        [Console]::Error.WriteLine($text)
        $directory = Get-Variable -Name runDirectory -ValueOnly -ErrorAction SilentlyContinue
        if ($null -ne $directory -and [IO.Directory]::Exists([string]$directory)) {
            $bytes = [Text.UTF8Encoding]::new($false).GetBytes($text)
            $output = [IO.FileStream]::new([IO.Path]::Combine([string]$directory,$ArtifactName),[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
            try { $output.Write($bytes,0,$bytes.Length); $output.Flush($true) } finally { $output.Dispose() }
        }
    } catch { try { [Console]::Error.WriteLine($Role + ' failure detail unavailable: ' + $_.Exception.Message) } catch { } }
}
function Get-MineruBootstrapSha([byte[]]$Bytes) {
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return 'sha256:' + ([BitConverter]::ToString($hash.ComputeHash($Bytes))).Replace('-','').ToLowerInvariant() }
    finally { $hash.Dispose() }
}
function Read-MineruBootstrap([string]$Path,[string]$ExpectedSha,[int]$Maximum) {
    if ($ExpectedSha -cnotmatch '\Asha256:[0-9a-f]{64}\z' -or -not [IO.Path]::IsPathRooted($Path)) { throw 'absolute bootstrap path and canonical SHA required' }
    $pin = [IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $pins.Add($pin)
    if ($pin.Length -lt 1 -or $pin.Length -gt $Maximum) { throw 'bootstrap file byte bound exceeded' }
    $bytes = [byte[]]::new([int]$pin.Length); $offset = 0
    while ($offset -lt $bytes.Length) {
        $count = $pin.Read($bytes,$offset,$bytes.Length-$offset)
        if ($count -le 0) { throw 'bootstrap truncated' }
        $offset += $count
    }
    if ((Get-MineruBootstrapSha $bytes) -cne $ExpectedSha) { throw 'bootstrap SHA mismatch' }
    return ,$bytes
}

try {
    # Owner must validate canonical config/preparation and current runtime first.
    $configBytes = Read-MineruBootstrap $ConfigJsonPath $ExpectedConfigSha256 32768
    $bootstrapConfig = [Text.UTF8Encoding]::new($false,$true).GetString($configBytes) | ConvertFrom-Json
    $commonPath = [IO.Path]::Combine($PSScriptRoot,'load_mineru_resident_session.ps1')
    $null = Read-MineruBootstrap $commonPath $bootstrapConfig.sources.'load_mineru_resident_session.ps1' 65536
    . $commonPath
    $state = Initialize-MineruResidentSession
    $runDirectory = $state.RunDirectory
    $exporter = [IO.Path]::Combine($state.SourceDirectory,'mineru_resident_telemetry_exporter.ps1')
    $arguments = [string[]]@('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',$exporter,'-ConfigJsonPath',$ConfigJsonPath,'-ExpectedConfigSha256',$ExpectedConfigSha256)
    # One fresh owner per lane. Includes startup and close; no infinite wait or
    # compilation. Forced/nonzero Job receipts are retained but never qualified.
    # New-only pre-Job marker makes a lost launch response discoverable. Never
    # start a second Job under the same session/config or overwrite its parent.
    $startedReceipt = New-MineruJson @('contract_version','"mineru.windows-resident-supervisor-started.v1"','session',(Quote-MineruJson $state.Session),'config_sha256',(Quote-MineruJson $ExpectedConfigSha256),'supervisor_process',$state.Epoch)
    Write-MineruSessionArtifact 'supervisor-started.json' $startedReceipt
    $jobJson = [MineruTelemetryJobSupervisor]::Run($state.PowerShellExe,$arguments,([int]$state.Lifetime+5000),3000,$state.Loaded.Manifest.sources.Where({$_.name -ceq 'mineru_telemetry_job_supervisor.cs'})[0].sha256)
    $receipt = New-MineruJson @('contract_version','"mineru.windows-resident-job-receipt.v1"','session',(Quote-MineruJson $state.Session),'config_sha256',(Quote-MineruJson $ExpectedConfigSha256),'supervisor_process',$state.Epoch,'job',$jobJson)
    Write-MineruSessionArtifact 'job-accounting.json' $receipt
    $job = [MineruResidentWire]::Parse($jobJson,8192)
    if ($job.Get('forced_termination').Raw -cne 'false' -or $job.Get('child_exit_code').Integer() -ne 0 -or $job.Get('job_active_processes').Integer() -ne 0) { throw 'resident Job did not close normally; retained receipt is unverified' }
    [Console]::Out.WriteLine($receipt)
} catch { Add-MineruFailure $_ }
finally {
    foreach ($pin in $pins) {
        try { $pin.Dispose() } catch { $failures.Add($_.Exception) }
    }
}
if ($failures.Count -gt 0) {
    Write-MineruFailureDetail 'resident supervisor' 'supervisor-failure.txt'
    throw [AggregateException]::new('resident supervisor failed',$failures)
}
