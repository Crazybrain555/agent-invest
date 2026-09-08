# Private, default-off session initialization. The entrypoint has pinned and
# SHA-verified this file and config before dot-sourcing it. No compiler here.
function Read-MineruSessionBytes([string]$Path, [string]$ExpectedSha, [int]$Maximum) {
    if ($ExpectedSha -cnotmatch '\Asha256:[0-9a-f]{64}\z' -or -not [IO.Path]::IsPathRooted($Path)) { throw 'absolute source and canonical SHA required' }
    $pin = [IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $pins.Add($pin)
    if ($pin.Length -lt 1 -or $pin.Length -gt $Maximum) { throw 'session file byte bound exceeded' }
    $bytes = [byte[]]::new([int]$pin.Length); $offset = 0
    while ($offset -lt $bytes.Length) {
        $count = $pin.Read($bytes,$offset,$bytes.Length-$offset)
        if ($count -le 0) { throw 'session file truncated' }
        $offset += $count
    }
    if ((Get-MineruBootstrapSha $bytes) -cne $ExpectedSha) { throw ('session file SHA mismatch: ' + [IO.Path]::GetFileName($Path)) }
    return ,$bytes
}
function Get-MineruString($Value, [string]$Name) { return $Value.Get($Name).String() }
function Get-MineruInteger($Value, [string]$Name, [long]$Minimum, [long]$Maximum) {
    $number = $Value.Get($Name).Integer()
    if ($number -lt $Minimum -or $number -gt $Maximum) { throw ('configuration range: ' + $Name) }
    return $number
}
function New-MineruJson([string[]]$Pairs) { return [MineruResidentWire]::Object($Pairs) }
function Quote-MineruJson([string]$Value) { return [MineruResidentWire]::Quote($Value) }
function Write-MineruSessionArtifact([string]$Name,[string]$Json) {
    if ($Name -cnotmatch '\A[a-z-]+\.json\z') { throw 'fixed artifact filename required' }
    $null = [MineruResidentWire]::Parse($Json,65536)
    $bytes = [MineruResidentWire]::Utf8.GetBytes($Json)
    $path = [IO.Path]::Combine($runDirectory,$Name)
    # Never expose the final name while bytes are still being written. The
    # same-directory Move has no overwrite overload on Framework 4.8; a
    # collision fails and preserves the previous artifact and pending evidence.
    $pending = [IO.Path]::Combine($runDirectory,($Name+'.pending-'+[Guid]::NewGuid().ToString('N')))
    $output = [IO.FileStream]::new($pending,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try { $output.Write($bytes,0,$bytes.Length); $output.Flush($true) } finally { $output.Dispose() }
    [IO.File]::Move($pending,$path)
    # The independent owner later reopens, verifies exact bytes and binds the
    # complete artifact set. This pin is not a POSIX directory-seal claim.
    $null = Read-MineruSessionBytes $path ([MineruResidentWire]::Hash($bytes)) 65536
}
function Initialize-MineruResidentSession {
    $sourceDirectory = [IO.Path]::GetFullPath($bootstrapConfig.source_directory)
    if ($sourceDirectory -cne [IO.Path]::GetFullPath($PSScriptRoot)) { throw 'source directory differs from executed entrypoint' }
    $loaderPath = [IO.Path]::Combine($sourceDirectory,'load_mineru_telemetry_assembly.ps1')
    $null = Read-MineruSessionBytes $loaderPath $bootstrapConfig.sources.'load_mineru_telemetry_assembly.ps1' 65536
    . $loaderPath
    $preparedConfig = $bootstrapConfig.prepared
    $loaded = Import-MineruTelemetryPreparedAssembly -ManifestPath $preparedConfig.manifest_path -ExpectedManifestSha256 $preparedConfig.manifest_sha256 -ExpectedNvmlSourceSha256 $preparedConfig.nvml_source_sha256 -ExpectedWireSourceSha256 $preparedConfig.wire_source_sha256 -ExpectedSupervisorSourceSha256 $preparedConfig.supervisor_source_sha256
    foreach ($pin in $loaded.Pins) { $pins.Add($pin) }
    $config = [MineruResidentWire]::Parse([MineruResidentWire]::Utf8.GetString($configBytes),32768)
    $config.Keys([string[]]@('contract_version','session','lane','cadence_ms','port','lease_ms','lifetime_ms','sampling_timeout_ms','response_timeout_ms','run_directory','source_directory','prepared','sources','powershell_executable_sha256','owner_identity','backend'))
    if ((Get-MineruString $config 'contract_version') -cne 'mineru.windows-resident-session.v1') { throw 'session config version' }
    $expectedSources = [string[]]@('load_mineru_resident_session.ps1','load_mineru_telemetry_assembly.ps1','mineru_resident_telemetry_exporter.ps1','start_mineru_resident_telemetry.ps1','linux_resident_host_sampler.py','linux_resident_host_supervisor.py')
    $sourceConfig = $config.Get('sources'); $sourceConfig.Keys($expectedSources)
    foreach ($name in $expectedSources) {
        $null = Read-MineruSessionBytes ([IO.Path]::Combine($sourceDirectory,$name)) (Get-MineruString $sourceConfig $name) 65536
    }
    $config.Get('prepared').Keys([string[]]@('manifest_path','manifest_sha256','nvml_source_sha256','wire_source_sha256','supervisor_source_sha256'))
    $ownerIdentity = $config.Get('owner_identity')
    $ownerIdentity.Keys([string[]]@('host_assignment_identity_sha256','boot_identity_sha256','runtime_bundle_identity_sha256','process_profile_sha256'))
    foreach ($name in @('host_assignment_identity_sha256','boot_identity_sha256','runtime_bundle_identity_sha256','process_profile_sha256')) {
        if ((Get-MineruString $ownerIdentity $name) -cnotmatch '\Asha256:[0-9a-f]{64}\z') { throw 'owner identity SHA malformed' }
    }
    $session = Get-MineruString $config 'session'
    if ($session -cnotmatch '\A[0-9a-f]{32}\z') { throw 'unique session GUID required' }
    $lane = Get-MineruString $config 'lane'
    $cadence = Get-MineruInteger $config 'cadence_ms' 250 1000
    if (-not (($lane -ceq 'gpu_fast' -and $cadence -in @(250,500)) -or ($lane -ceq 'host_slow' -and $cadence -eq 1000))) { throw 'lane cadence mismatch' }
    $lease = Get-MineruInteger $config 'lease_ms' 2000 30000
    $lifetime = Get-MineruInteger $config 'lifetime_ms' $lease 7190000
    $port = Get-MineruInteger $config 'port' 1024 65535
    $samplingTimeout = Get-MineruInteger $config 'sampling_timeout_ms' 1 $cadence
    $responseTimeout = Get-MineruInteger $config 'response_timeout_ms' 1 1000
    $runDirectory = [IO.Path]::GetFullPath((Get-MineruString $config 'run_directory'))
    if (-not [IO.Directory]::Exists($runDirectory)) { throw 'owner-created private run directory required' }
    $powerShellExe = [IO.Path]::Combine($PSHOME,'powershell.exe')
    $null = Read-MineruSessionBytes $powerShellExe (Get-MineruString $config 'powershell_executable_sha256') 16777216
    # Owner attestation hashes are claims until independently checked against
    # actual host boot/runtime/profile before and after this exact process.
    $process = [Diagnostics.Process]::GetCurrentProcess()
    try { $creation = $process.StartTime.ToUniversalTime().ToFileTimeUtc(); $processId = $process.Id }
    finally { $process.Dispose() }
    $clock = New-MineruJson @('boot_identity_sha256',$ownerIdentity.Get('boot_identity_sha256').Raw,'clock_source','"QueryPerformanceCounter"','frequency_hz',[string][Diagnostics.Stopwatch]::Frequency)
    $epoch = New-MineruJson @('session',(Quote-MineruJson $session),'config_sha256',(Quote-MineruJson $ExpectedConfigSha256),'pid',[string]$processId,'creation_filetime_100ns',[string]$creation,'manifest_sha256',(Quote-MineruJson $loaded.ManifestSha256),'assembly_sha256',(Quote-MineruJson $loaded.Manifest.assembly_sha256))
    $identity = New-MineruJson @('exporter_source_sha256',$sourceConfig.Get('mineru_resident_telemetry_exporter.ps1').Raw,'host_assignment_identity_sha256',$ownerIdentity.Get('host_assignment_identity_sha256').Raw,'boot_identity_sha256',$ownerIdentity.Get('boot_identity_sha256').Raw,'runtime_bundle_identity_sha256',$ownerIdentity.Get('runtime_bundle_identity_sha256').Raw,'process_profile_sha256',$ownerIdentity.Get('process_profile_sha256').Raw,'clock_domain_identity_sha256',(Quote-MineruJson ([MineruResidentWire]::Hash([MineruResidentWire]::Utf8.GetBytes($clock)))),'exporter_process_epoch_sha256',(Quote-MineruJson ([MineruResidentWire]::Hash([MineruResidentWire]::Utf8.GetBytes($epoch)))))
    return [pscustomobject]@{ Config=$config; Loaded=$loaded; SourceDirectory=$sourceDirectory; RunDirectory=$runDirectory; Session=$session; Lane=$lane; Cadence=$cadence; Lease=$lease; Lifetime=$lifetime; Port=$port; SamplingTimeout=$samplingTimeout; ResponseTimeout=$responseTimeout; PowerShellExe=$powerShellExe; Identity=$identity; Epoch=$epoch; Clock=$clock }
}
