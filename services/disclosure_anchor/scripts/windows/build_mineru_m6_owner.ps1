<#
.SYNOPSIS
Production-only build of the native M6 owner host from exactly twelve tracked sources.

.DESCRIPTION
Compiles the same production source list and compiler arguments the native suite uses, without any
test source, without Add-Type and without loading the result. Every source, the pinned framework csc
and its referenced assemblies are held open with FileShare.Read from hashing through compilation, so
the receipt describes the actual compiler inputs. The compiler runs as a bounded child: both pipes
are drained continuously with a byte cap and one deadline from spawn; a child alive after failure or
deadline is terminated within a bounded cleanup. The receipt binds every source hash, the production
source manifest digest (same formula as the native suite), the compiler identity and arguments, and
the executable hash. Nothing is installed.
#>
param(
    [Parameter(Mandatory=$true)][string]$SourceRoot,
    [Parameter(Mandatory=$true)][string]$OutputRoot,
    [string]$ExpectedProductionSourceManifestSha256='',
    [ValidateRange(30,900)][int]$CompilerTimeoutSeconds=300,
    [ValidateRange(65536,8388608)][int]$MaximumCompilerOutputBytes=1048576
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'Windows PowerShell 5.1 is required' }
if (-not [Environment]::Is64BitProcess) { throw '64-bit PowerShell is required' }
if ($ExpectedProductionSourceManifestSha256 -ne '' -and $ExpectedProductionSourceManifestSha256 -cnotmatch '^sha256:[0-9a-f]{64}$') { throw 'Expected source manifest hash must be canonical' }

# Literal production list: identical to the native suite's list; never derived from a directory listing.
$ProductionSources=@(
    'mineru_m6_owner_binding.cs','mineru_m6_owner_endpoint.cs','mineru_m6_owner_host.cs',
    'mineru_m6_owner_identity.cs','mineru_m6_owner_journal.cs','mineru_m6_owner_platform.cs',
    'mineru_m6_owner_wire.cs','mineru_m6_private_store.cs','mineru_m6_run_control.cs',
    'mineru_m6_writer_guard.cs','mineru_nvml_backend.cs','mineru_resident_wire.cs'
)
$SourceRoot=[IO.Path]::GetFullPath($SourceRoot)
$OutputRoot=[IO.Path]::GetFullPath($OutputRoot)
foreach ($forbidden in @('C:\ProgramData','C:\Windows','C:\Program Files')) {
    if ($OutputRoot.StartsWith($forbidden,[StringComparison]::OrdinalIgnoreCase)) { throw ('output root must not be under ' + $forbidden) }
}
if (Test-Path -LiteralPath $OutputRoot) { throw ('output root must be new: ' + $OutputRoot) }
if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) { throw ('source root missing: ' + $SourceRoot) }
$FrameworkDir=Join-Path (Split-Path -Parent ([Environment]::SystemDirectory)) 'Microsoft.NET\Framework64\v4.0.30319'
$Csc=Join-Path $FrameworkDir 'csc.exe'
if (-not (Test-Path -LiteralPath $Csc -PathType Leaf)) { throw ('compiler missing: ' + $Csc) }

$utf8=[Text.UTF8Encoding]::new($false,$true)
$pins=[Collections.Generic.List[IO.FileStream]]::new()
function Open-Pin([string]$Path,[long]$MaximumBytes) {
    $pin=[IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $pins.Add($pin)
    if ($pin.Length -lt 1 -or $pin.Length -gt $MaximumBytes) { throw ('file byte bound exceeded: ' + $Path) }
    return $pin
}
function Get-StreamSha256([IO.Stream]$Stream) {
    $algorithm=[Security.Cryptography.SHA256]::Create()
    try { $Stream.Position=0; return 'sha256:' + ([BitConverter]::ToString($algorithm.ComputeHash($Stream))).Replace('-','').ToLowerInvariant() }
    finally { $algorithm.Dispose(); $Stream.Position=0 }
}
function ConvertTo-CommandLineArgument([string]$Value) {
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $builder=New-Object Text.StringBuilder
    [void]$builder.Append('"')
    $backslashes=0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $backslashes++; continue }
        if ($character -eq '"') { [void]$builder.Append('\' * ($backslashes * 2 + 1)); [void]$builder.Append('"'); $backslashes=0; continue }
        if ($backslashes -gt 0) { [void]$builder.Append('\' * $backslashes); $backslashes=0 }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) { [void]$builder.Append('\' * ($backslashes * 2)) }
    [void]$builder.Append('"')
    return $builder.ToString()
}
function Write-NewRecord([string]$Path,[string]$Json) {
    $bytes=$utf8.GetBytes($Json)
    $file=[IO.FileStream]::new($Path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try { $file.Write($bytes,0,$bytes.Length); $file.Flush($true) } finally { $file.Dispose() }
}

$failure=$null; $exitCode=$null; $forced=$false; $exeSha=$null; $stdoutText=''; $stderrText=''
$stdoutTotal=[long]0; $stderrTotal=[long]0; $elapsed=[long]0
$sourceHashes=[ordered]@{}; $sourcePaths=@(); $manifestSha=$null; $compilerSha=$null
$referenceNames=@('System.dll','System.Core.dll','System.Management.dll','System.Net.Http.dll','System.IO.Compression.dll')
$referenceHashes=[ordered]@{}
$compilerArguments=@()
$outRetained=[IO.MemoryStream]::new(); $errRetained=[IO.MemoryStream]::new()
$clock=[Diagnostics.Stopwatch]::StartNew()
$compiler=$null; $started=$false
try {
    foreach ($name in $ProductionSources) {
        if ($name -like 'test_*') { throw ('test source is not a production input: ' + $name) }
        $path=Join-Path $SourceRoot $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw ('missing production source: ' + $name) }
        # Pins stay open through compilation so the receipt describes the bytes csc read.
        $sourceHashes[$name]=Get-StreamSha256 (Open-Pin $path 1048576)
        $sourcePaths+=$path
    }
    $manifestText=($ProductionSources | ForEach-Object { $_ + ' ' + $sourceHashes[$_] }) -join "`n"
    $manifestSha='sha256:' + ((-join ([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($manifestText + "`n")) | ForEach-Object { $_.ToString('x2') })))
    if ($ExpectedProductionSourceManifestSha256 -ne '' -and $manifestSha -cne $ExpectedProductionSourceManifestSha256) { throw ('production source manifest differs from the expected identity: ' + $manifestSha) }
    $compilerSha=Get-StreamSha256 (Open-Pin $Csc 16777216)
    foreach ($reference in $referenceNames) { $referenceHashes[$reference]=Get-StreamSha256 (Open-Pin (Join-Path $FrameworkDir $reference) 33554432) }

    New-Item -ItemType Directory -Path $OutputRoot | Out-Null
    $HostExe=Join-Path $OutputRoot 'mineru_m6_owner_host.exe'
    $compilerArguments=@(
        '/noconfig','/nologo','/warnaserror+','/nowarn:1701,1702','/target:exe','/platform:x64',
        '/optimize-','/debug-',('/lib:' + $FrameworkDir),
        '/r:System.dll','/r:System.Core.dll','/r:System.Management.dll','/r:System.Net.Http.dll','/r:System.IO.Compression.dll',
        '/main:MineruM6OwnerHost',('/out:' + $HostExe)
    ) + $sourcePaths
    $start=[Diagnostics.ProcessStartInfo]::new($Csc)
    $start.Arguments=(@($compilerArguments | ForEach-Object { ConvertTo-CommandLineArgument $_ }) -join ' ')
    $start.UseShellExecute=$false; $start.CreateNoWindow=$true
    $start.RedirectStandardOutput=$true; $start.RedirectStandardError=$true
    $start.WorkingDirectory=$OutputRoot
    $compiler=[Diagnostics.Process]::new(); $compiler.StartInfo=$start
    $deadline=[long]$CompilerTimeoutSeconds * 1000
    $spawn=[Diagnostics.Stopwatch]::StartNew()
    if (-not $compiler.Start()) { throw 'compiler start failed' }
    $started=$true
    $outStream=$compiler.StandardOutput.BaseStream; $errStream=$compiler.StandardError.BaseStream
    $outBuffer=[byte[]]::new(16384); $errBuffer=[byte[]]::new(16384)
    $outTask=$outStream.ReadAsync($outBuffer,0,$outBuffer.Length); $errTask=$errStream.ReadAsync($errBuffer,0,$errBuffer.Length)
    $outEof=$false; $errEof=$false
    while (-not ($outEof -and $errEof)) {
        $remaining=$deadline - $spawn.ElapsedMilliseconds
        if ($remaining -le 0) { throw ('compiler deadline exceeded after ' + $spawn.ElapsedMilliseconds + ' ms') }
        $pending=@(); if (-not $outEof) { $pending+=$outTask }; if (-not $errEof) { $pending+=$errTask }
        $index=[Threading.Tasks.Task]::WaitAny([Threading.Tasks.Task[]]$pending,[int][Math]::Min($remaining,1000))
        if ($index -lt 0) { continue }
        $task=$pending[$index]; $count=$task.GetAwaiter().GetResult()
        if ([object]::ReferenceEquals($task,$outTask)) {
            if ($count -eq 0) { $outEof=$true } else { $stdoutTotal+=$count; $outRetained.Write($outBuffer,0,$count); $outTask=$outStream.ReadAsync($outBuffer,0,$outBuffer.Length) }
        } else {
            if ($count -eq 0) { $errEof=$true } else { $stderrTotal+=$count; $errRetained.Write($errBuffer,0,$count); $errTask=$errStream.ReadAsync($errBuffer,0,$errBuffer.Length) }
        }
        if ($stdoutTotal + $stderrTotal -gt $MaximumCompilerOutputBytes) { throw ('compiler output exceeded ' + $MaximumCompilerOutputBytes + ' bytes') }
    }
    $remaining=[Math]::Max(1,$deadline - $spawn.ElapsedMilliseconds)
    if (-not $compiler.WaitForExit([int][Math]::Min($remaining,[int]::MaxValue))) { throw 'compiler did not exit after closing its pipes' }
    $exitCode=$compiler.ExitCode
    $elapsed=$spawn.ElapsedMilliseconds
    if ($exitCode -eq 0 -and (Test-Path -LiteralPath $HostExe -PathType Leaf)) { $exeSha=Get-StreamSha256 (Open-Pin $HostExe 16777216) }
} catch {
    $failure=$_.Exception.Message
} finally {
    if ($started) {
        try {
            if (-not $compiler.HasExited) { $compiler.Kill(); $forced=$true }
            if (-not $compiler.WaitForExit(10000)) { if ($null -eq $failure) { $failure='compiler did not exit after kill' } }
            elseif ($null -eq $exitCode) { try { $exitCode=$compiler.ExitCode } catch { } }
        } catch { if ($null -eq $failure) { $failure='compiler cleanup failed: ' + $_.Exception.Message } }
    }
    if ($null -ne $compiler) { $compiler.Dispose() }
    # Retained (bounded) compiler diagnostics are reported on every path, including failures.
    $stdoutText=[Text.Encoding]::UTF8.GetString($outRetained.ToArray()); $stderrText=[Text.Encoding]::UTF8.GetString($errRetained.ToArray())
    if ($started -and $elapsed -eq 0) { $elapsed=$clock.ElapsedMilliseconds }
}
if (-not (Test-Path -LiteralPath $OutputRoot -PathType Container)) { New-Item -ItemType Directory -Path $OutputRoot | Out-Null }
$receipt=[ordered]@{
    contract_version='m6.owner-production-build.v1'
    built_utc=[DateTime]::UtcNow.ToString('o')
    hostname=[Environment]::MachineName
    powershell_version=$PSVersionTable.PSVersion.ToString()
    clr_version=[Environment]::Version.ToString()
    source_root=$SourceRoot
    sources=$sourceHashes
    production_source_manifest_sha256=$manifestSha
    compiler_path=$Csc
    compiler_sha256=$compilerSha
    reference_assembly_sha256=$referenceHashes
    compiler_arguments=$compilerArguments
    compiler_exit_code=$exitCode
    compiler_forced_termination=$forced
    compiler_elapsed_milliseconds=$elapsed
    compiler_stdout_bytes=$stdoutTotal
    compiler_stderr_bytes=$stderrTotal
    compiler_stdout=$stdoutText
    compiler_stderr=$stderrText
    executable_path=$(if ($null -ne $exeSha) { Join-Path $OutputRoot 'mineru_m6_owner_host.exe' } else { $null })
    executable_sha256=$exeSha
    failure=$failure
    status=$(if ($null -ne $exeSha -and $null -eq $failure) { 'pass' } else { 'fail' })
}
$json=$receipt | ConvertTo-Json -Depth 6
$persistError=$null
try { Write-NewRecord (Join-Path $OutputRoot 'build-receipt.json') $json } catch { $persistError=$_.Exception.Message }
foreach ($pin in $pins) { try { $pin.Dispose() } catch { } }
$json
if ($null -ne $persistError) { throw ('build receipt could not be persisted: ' + $persistError) }
if ($receipt.status -ne 'pass') { throw ('production build failed: ' + $(if ($null -ne $failure) { $failure } else { 'compiler exit ' + $exitCode })) }
