<#
.SYNOPSIS
Independent regression for the production owner launcher: Windows PowerShell 5.1 x64 only.
.DESCRIPTION
Root-authored controlled child checks: success, nonzero exit, missing/malformed/oversized READY,
record write failures, concurrent pipe flood, timeout and artifact drift. No GPU, PDF, DB or service
operation. Requires source siblings below; all generated binaries/configuration/logs stay in a new
explicit disposable output root. The test-only M6BoundedProcess owns the outer process tree.
.EXAMPLE
.\test_mineru_m6_owner_launcher.ps1 -OutputRoot C:\Users\help\workspaces\EXPLICIT-NEW-TEST
#>
param([string]$SourceRoot=$PSScriptRoot,[Parameter(Mandatory=$true)][string]$OutputRoot)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
$source=[IO.Path]::GetFullPath($SourceRoot)
$output=[IO.Path]::GetFullPath($OutputRoot)
if ($PSVersionTable.PSVersion.Major -ne 5 -or -not [Environment]::Is64BitProcess) { throw 'Windows PowerShell 5.1 x64 required' }
if (Test-Path -LiteralPath $output) { throw 'fresh output required' }
$null=New-Item -ItemType Directory -Path $output
Add-Type -Path (Join-Path $source 'test_mineru_m6_process.cs')
$csc=Join-Path (Split-Path -Parent ([Environment]::SystemDirectory)) 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$exe=Join-Path $output 'independent_launcher_fixture.exe'
$held=$null
$results=New-Object Collections.ArrayList
function Hash([string]$Path) { return 'sha256:'+(Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant() }
try {
    $held=New-Object M6BoundedProcess($csc,@('/nologo','/warnaserror+','/platform:x64','/target:exe',('/out:'+$exe),(Join-Path $source 'test_mineru_m6_owner_launcher_fixture.cs')),$output,(Join-Path $output 'compile'))
    $held.Finish(60000)
    if ($held.ExitCode -ne 0 -or $held.ForcedTermination -or $held.ActiveJobProcesses -ne 0) { throw 'fixture compile failed' }
    $held.Dispose(); $held=$null
    foreach($mode in @('normal','nonzero','no-ready','bad-ready','oversize-ready','ready-write-failure','exit-write-failure','flood','timeout','binary-drift','configuration-drift')) {
        $case=Join-Path $output $mode
        $null=New-Item -ItemType Directory -Path $case
        $config=Join-Path $case 'input.txt'
        [IO.File]::WriteAllText($config,$mode,[Text.UTF8Encoding]::new($false))
        $run=Join-Path $case 'run'
        $binaryHash=Hash $exe; $configHash=Hash $config
        if($mode -eq 'binary-drift'){$binaryHash='sha256:'+('0'*64)}
        if($mode -eq 'configuration-drift'){$configHash='sha256:'+('0'*64)}
        $argv=@('-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','RemoteSigned','-File',(Join-Path $source 'run_mineru_m6_owner_host.ps1'),'-BinaryPath',$exe,'-ExpectedBinarySha256',$binaryHash,'-ConfigurationPath',$config,'-ExpectedConfigurationSha256',$configHash,'-OutputDirectory',$run,'-ExpectedHostname',[Environment]::MachineName,'-PlannedSeconds','1','-CloseGraceSeconds','1','-ExitWaitExtraSeconds','30','-ReadyWaitSeconds','5')
        $watch=[Diagnostics.Stopwatch]::StartNew()
        $held=New-Object M6BoundedProcess((Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'),$argv,$case,(Join-Path $case 'parent'))
        $held.Finish(70000)
        $problems=New-Object Collections.ArrayList
        $expectedSuccess=$mode -in @('normal','flood')
        if (($held.ExitCode -eq 0) -ne $expectedSuccess) { $null=$problems.Add('parent success/failure differs from injected condition') }
        if ($held.ActiveJobProcesses -ne 0 -or $held.ForcedTermination -or $held.TimedOut -or $held.CleanupFailure) { $null=$problems.Add('outer parent did not close own processes naturally') }
        $record=$null; $recordPath=Join-Path $run 'process-exit.json'
        if(Test-Path -LiteralPath $recordPath -PathType Leaf) {
            $record=Get-Content -LiteralPath $recordPath -Raw|ConvertFrom-Json
            if(-not $record.process_handle_signaled -or -not $record.exact_process_handle_opened){$null=$problems.Add('original child exact handle not signaled')}
            if($mode -eq 'timeout' -and -not $record.forced_termination){$null=$problems.Add('timeout cleanup not reported')}
            if($mode -eq 'nonzero' -and $record.exit_code -ne 7){$null=$problems.Add('child nonzero lost')}
            if($mode -eq 'flood') {
                if($record.stdout_total_bytes -lt 4194304 -or $record.stderr_total_bytes -lt 4194304){$null=$problems.Add('both pipes not fully drained')}
                if($record.stdout_retained.Length -gt 140000 -or $record.stderr_retained.Length -gt 140000){$null=$problems.Add('retention not bounded')}
            }
        } elseif($mode -notin @('binary-drift','configuration-drift','exit-write-failure')) { $null=$problems.Add('durable external exit missing') }
        if($mode -in @('binary-drift','configuration-drift') -and (Test-Path -LiteralPath $run)){$null=$problems.Add('hash refusal mutated output')}
        $null=$results.Add([ordered]@{case=$mode;exit_code=$held.ExitCode;outer_exact_handle_signaled=$held.ExactProcessExited;active_job_processes=$held.ActiveJobProcesses;outer_forced=$held.ForcedTermination;elapsed_ms=$watch.ElapsedMilliseconds;problems=@($problems.ToArray());record=$record})
        Write-Host ($mode+': exit='+$held.ExitCode+' problems='+$problems.Count)
        $held.Dispose();$held=$null
    }
} finally {
    if($null -ne $held){$held.Dispose()}
    $out=[ordered]@{scope='independent production-parent regression; zero PDF';launcher_sha256=(Hash (Join-Path $source 'run_mineru_m6_owner_host.ps1'));cases=@($results.ToArray())}
    [IO.File]::WriteAllText((Join-Path $output 'evidence.json'),($out|ConvertTo-Json -Depth 10),[Text.UTF8Encoding]::new($false))
}
if(@($results|Where-Object{$_.problems.Count -ne 0}).Count -ne 0){exit 1}
exit 0
