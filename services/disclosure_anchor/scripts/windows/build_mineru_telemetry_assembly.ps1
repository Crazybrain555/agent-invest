param(
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# Explicit preparation, never called by a measured exporter/supervisor.
# Compile once outside the telemetry process lifetime; bind the actual source,
# recipe/compiler and DLL bytes. Runtime loading must not invoke a compiler.
$root = [IO.Path]::GetFullPath($OutputDirectory)
if (-not [IO.Directory]::Exists($root)) { throw 'existing preparation root required' }
$buildDirectory = [IO.Path]::Combine($root, ('telemetry-assembly-' + [Guid]::NewGuid().ToString('N')))
$null = [IO.Directory]::CreateDirectory($buildDirectory)
$assemblyPath = [IO.Path]::Combine($buildDirectory, 'mineru-telemetry.dll')
$manifestPath = [IO.Path]::Combine($buildDirectory, 'manifest.json')
$pins = [Collections.Generic.List[IO.FileStream]]::new()
$failures = [Collections.Generic.List[Exception]]::new()
$prepared = $null
function Get-StreamSha256([IO.Stream]$Stream) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $Stream.Position = 0
        return 'sha256:' + ([BitConverter]::ToString($algorithm.ComputeHash($Stream))).Replace('-', '').ToLowerInvariant()
    } finally { $algorithm.Dispose(); $Stream.Position = 0 }
}
try {
    $sourceNames = [string[]]@('mineru_nvml_backend.cs', 'mineru_resident_wire.cs', 'mineru_telemetry_job_supervisor.cs')
    $sourcePaths = [Collections.Generic.List[string]]::new()
    $sources = @()
    foreach ($name in $sourceNames) {
        $sourcePath = [IO.Path]::Combine($PSScriptRoot, $name)
        $pin = [IO.FileStream]::new($sourcePath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $pins.Add($pin)
        if ($pin.Length -lt 1 -or $pin.Length -gt 65536) { throw 'bounded source bytes required' }
        $sources += [ordered]@{ name = $name; sha256 = Get-StreamSha256 $pin }
        $sourcePaths.Add($sourcePath)
    }
    $compilerPath = [IO.Path]::Combine([Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory(), 'csc.exe')
    $compilerPin = [IO.FileStream]::new($compilerPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $pins.Add($compilerPin)
    $compilerSha = Get-StreamSha256 $compilerPin
    $recipePin = [IO.FileStream]::new($PSCommandPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $pins.Add($recipePin)
    $recipeSha = Get-StreamSha256 $recipePin
    # Invoke the exact pinned compiler, not CodeDom's implicit selection.
    $systemAssemblyPath = [IO.Path]::Combine([IO.Path]::GetDirectoryName($compilerPath), 'System.dll')
    $systemPin = [IO.FileStream]::new($systemAssemblyPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $pins.Add($systemPin)
    $httpAssemblyPath = [IO.Path]::Combine([IO.Path]::GetDirectoryName($compilerPath), 'System.Net.Http.dll')
    $httpPin = [IO.FileStream]::new($httpAssemblyPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $pins.Add($httpPin)
    $compilerArguments = @('/nologo', '/noconfig', '/target:library', '/warnaserror+',
        ('/out:' + $assemblyPath), ('/reference:' + $systemAssemblyPath), ('/reference:' + $httpAssemblyPath)) + $sourcePaths.ToArray()
    $quotedArguments = @($compilerArguments | ForEach-Object {
        if ($_.Contains('"') -or $_.Contains([char]0)) { throw 'invalid compiler argument' }
        '"' + $_ + '"'
    }) -join ' '
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $compilerPath; $start.Arguments = $quotedArguments
    $start.UseShellExecute = $false; $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true; $start.RedirectStandardError = $true
    $compiler = [Diagnostics.Process]::new(); $compiler.StartInfo = $start
    $compilerStarted = $false
    $compilerFailures = [Collections.Generic.List[Exception]]::new()
    try {
        if (-not $compiler.Start()) { throw 'compiler start failed' }
        $compilerStarted = $true
        $compilerOutput = $compiler.StandardOutput.ReadToEndAsync()
        $compilerError = $compiler.StandardError.ReadToEndAsync()
        if (-not $compiler.WaitForExit(15000)) {
            throw 'compiler deadline exceeded'
        }
        if (-not $compilerOutput.Wait(1000) -or -not $compilerError.Wait(1000)) { throw 'compiler output deadline exceeded' }
        if ($compiler.ExitCode -ne 0) { throw ('compiler failed: ' + $compilerOutput.Result + $compilerError.Result) }
    } catch { $compilerFailures.Add($_.Exception) }
    finally {
        if ($compilerStarted) {
            try {
                if (-not $compiler.WaitForExit(0)) {
                    try { $compiler.Kill() }
                    catch { if (-not $compiler.WaitForExit(0)) { throw } }
                    if (-not $compiler.WaitForExit(3000)) { throw 'compiler failed to exit after kill' }
                }
            } catch { $compilerFailures.Add($_.Exception) }
        }
        try { $compiler.Dispose() } catch { $compilerFailures.Add($_.Exception) }
    }
    if ($compilerFailures.Count -gt 0) { throw [AggregateException]::new('compiler lifecycle failed', $compilerFailures) }
    $assemblyPin = [IO.FileStream]::new($assemblyPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $pins.Add($assemblyPin)
    if ($assemblyPin.Length -lt 1 -or $assemblyPin.Length -gt 1048576) { throw 'bounded assembly bytes required' }
    $manifest = [ordered]@{
        assembly_name = 'mineru-telemetry.dll'
        assembly_sha256 = Get-StreamSha256 $assemblyPin
        compiler_arguments = $compilerArguments
        compiler_path = $compilerPath
        compiler_sha256 = $compilerSha
        contract_version = 'mineru.telemetry-prepared-assembly.v2'
        http_assembly_sha256 = Get-StreamSha256 $httpPin
        powershell_version = $PSVersionTable.PSVersion.ToString()
        preparation_recipe_sha256 = $recipeSha
        runtime_version = [Environment]::Version.ToString()
        sources = $sources
        system_assembly_sha256 = Get-StreamSha256 $systemPin
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes(($manifest | ConvertTo-Json -Depth 8 -Compress))
    $output = [IO.FileStream]::new($manifestPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try { $output.Write($bytes, 0, $bytes.Length); $output.Flush($true) } finally { $output.Dispose() }
    $manifestPin = [IO.FileStream]::new($manifestPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $pins.Add($manifestPin)
    $prepared = [ordered]@{ manifest_path = $manifestPath; manifest_sha256 = Get-StreamSha256 $manifestPin }
} catch { $failures.Add($_.Exception) }
finally {
    foreach ($pin in $pins) {
        try { $pin.Dispose() } catch { $failures.Add($_.Exception) }
    }
}
if ($failures.Count -gt 0) { throw [AggregateException]::new('assembly preparation failed', $failures) }
$prepared | ConvertTo-Json -Compress
