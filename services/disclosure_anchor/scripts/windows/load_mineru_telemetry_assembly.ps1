# Dot-source this file in a fresh measured process. No compilation/subprocesses.
function Import-MineruTelemetryPreparedAssembly {
    param(
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$ExpectedManifestSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedNvmlSourceSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedSupervisorSourceSha256
    )
    foreach ($hash in @($ExpectedManifestSha256, $ExpectedNvmlSourceSha256, $ExpectedSupervisorSourceSha256)) {
        if ($hash -cnotmatch '\Asha256:[0-9a-f]{64}\z') { throw 'canonical expected SHA required' }
    }
    if ($null -ne ('MineruTelemetryJobSupervisor' -as [type]) -or $null -ne ('MineruNvmlBackend' -as [type])) {
        throw 'telemetry assembly already loaded; fresh process required'
    }
    $pins = [Collections.Generic.List[IO.FileStream]]::new()
    function Read-MineruPinnedBytes([string]$Path, [int]$MaximumBytes) {
        $pin = [IO.FileStream]::new($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $pins.Add($pin)
        if ($pin.Length -lt 1 -or $pin.Length -gt $MaximumBytes) { throw 'artifact byte bound exceeded' }
        $bytes = [byte[]]::new([int]$pin.Length)
        $offset = 0
        while ($offset -lt $bytes.Length) {
            $read = $pin.Read($bytes, $offset, $bytes.Length - $offset)
            if ($read -le 0) { throw 'unexpected artifact EOF' }
            $offset += $read
        }
        return ,$bytes
    }
    function Get-MineruBytesSha([byte[]]$Bytes) {
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try { return 'sha256:' + ([BitConverter]::ToString($algorithm.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant() }
        finally { $algorithm.Dispose() }
    }
    try {
        $fullManifestPath = [IO.Path]::GetFullPath($ManifestPath)
        $manifestBytes = Read-MineruPinnedBytes $fullManifestPath 32768
        if ((Get-MineruBytesSha $manifestBytes) -cne $ExpectedManifestSha256) { throw 'prepared manifest SHA mismatch' }
        # The owner must first verify this exact manifest with a strict decoder
        # and bind it to the preparation/source receipt. Hash matching alone is
        # not independent evidence that arbitrary compiler output is trusted.
        $manifest = [Text.UTF8Encoding]::new($false, $true).GetString($manifestBytes) | ConvertFrom-Json
        $expectedFields = @('assembly_name','assembly_sha256','compiler_arguments','compiler_path','compiler_sha256',
            'contract_version','powershell_version','preparation_recipe_sha256','runtime_version','sources','system_assembly_sha256')
        if (@(Compare-Object @($manifest.PSObject.Properties.Name | Sort-Object) $expectedFields).Count -ne 0) {
            throw 'prepared manifest shape mismatch'
        }
        if ($manifest.contract_version -cne 'mineru.telemetry-prepared-assembly.v1' -or
            $manifest.assembly_name -cne 'mineru-telemetry.dll' -or $manifest.sources.Count -ne 2) { throw 'prepared manifest identity mismatch' }
        $expected = @{'mineru_nvml_backend.cs' = $ExpectedNvmlSourceSha256; 'mineru_telemetry_job_supervisor.cs' = $ExpectedSupervisorSourceSha256}
        $seen = @{}
        foreach ($source in $manifest.sources) {
            if (@(Compare-Object @($source.PSObject.Properties.Name | Sort-Object) @('name','sha256')).Count -ne 0 -or
                -not $expected.ContainsKey($source.name) -or $seen.ContainsKey($source.name) -or
                $source.sha256 -cne $expected[$source.name]) { throw 'prepared source binding mismatch' }
            $seen[$source.name] = $true
        }
        $assemblyPath = [IO.Path]::Combine([IO.Path]::GetDirectoryName($fullManifestPath), $manifest.assembly_name)
        $assemblyBytes = Read-MineruPinnedBytes $assemblyPath 1048576
        if ((Get-MineruBytesSha $assemblyBytes) -cne $manifest.assembly_sha256) { throw 'prepared DLL SHA mismatch' }
        $assembly = [Reflection.Assembly]::Load($assemblyBytes)
        if ($null -eq $assembly.GetType('MineruTelemetryJobSupervisor', $false) -or
            $null -eq $assembly.GetType('MineruNvmlBackend', $false)) { throw 'prepared types absent' }
        # Caller retains pins through shutdown and disposes each only at exit.
        return [pscustomobject]@{ Pins = $pins; Manifest = $manifest; ManifestSha256 = $ExpectedManifestSha256; Assembly = $assembly }
    } catch {
        $failures = [Collections.Generic.List[Exception]]::new()
        $failures.Add($_.Exception)
        foreach ($pin in $pins) {
            try { $pin.Dispose() } catch { $failures.Add($_.Exception) }
        }
        throw [AggregateException]::new('prepared assembly load failed', $failures)
    }
}
