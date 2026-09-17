<#
Independent installer subprocess behavior tests. Never evaluates the installer
top level and never invokes Docker. Each case has an independent exact-process
Job owner; the product helper must finish itself, not rely on the test timeout.
Only this new disposable output tree is writable. No service or GPU changes.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$InstallerPath,
    [Parameter(Mandatory=$true)][string]$OutputRoot,
    [string]$Case=''
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'Windows PowerShell 5.1 required' }
$InstallerPath=[IO.Path]::GetFullPath($InstallerPath)
$OutputRoot=[IO.Path]::GetFullPath($OutputRoot)
$Utf8=[Text.UTF8Encoding]::new($false,$true)
[Console]::OutputEncoding=$Utf8
$OutputEncoding=$Utf8
$PsExe=Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
function Check([bool]$Condition,[string]$Message) { if (-not $Condition) { throw $Message } }
function Child-Args([string]$Code) {
    return [string[]]@('-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand',
        [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Code)))
}
function Require-Error([scriptblock]$Action,[string]$Pattern) {
    $caught=$null
    try { & $Action | Out-Null } catch { $caught=$_.ToString() }
    Check ($null -ne $caught -and $caught -match $Pattern) ('expected error '+$Pattern+'; actual '+$caught)
    return $caught
}

if ($Case -ne '') {
    # Explicit production functions only. No production global initializer,
    # Docker discovery, installer body, rollback or network action is loaded.
    $tokens=$null; $errors=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile($InstallerPath,[ref]$tokens,[ref]$errors)
    if (@($errors).Count -ne 0) { throw ($errors | Out-String) }
    $allowed=@('ConvertTo-WindowsCommandLineArgument','Assert-NativeProcessArguments',
        'Start-NativeProcessBudget','Get-NativeProcessRemainingMilliseconds',
        'New-NativeProcessRetention','Add-NativeProcessRetention','Get-NativeProcessRetentionText',
        'Invoke-NativeProcess','Invoke-DockerProcess')
    foreach ($name in $allowed) {
        $nodes=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$true))
        Check ($nodes.Count -eq 1) ('expected exactly one actual function '+$name)
        . ([ScriptBlock]::Create($nodes[0].Extent.Text))
    }
    $script:NativeProcessBudget=$null
    $watch=[Diagnostics.Stopwatch]::StartNew()
    $evidence=[ordered]@{case=$Case;status='fail';detail=$null;elapsed_ms=0}
    try {
        switch -Exact ($Case) {
            'normal_nonzero' {
                $r=Invoke-NativeProcess -FilePath $PsExe -Arguments (Child-Args '[Console]::Out.Write("OUT_MARK");[Console]::Error.Write("ERR_MARK");exit 23') -TimeoutMilliseconds 10000
                Check ($r.ExitCode -eq 23) 'nonzero child exit was lost'
                Check ($r.StandardOutput -ceq 'OUT_MARK' -and $r.StandardError -ceq 'ERR_MARK') 'pipe bytes were lost or merged'
                $evidence.detail=@{exit_code=$r.ExitCode;stdout=$r.StandardOutput;stderr=$r.StandardError}
            }
            'hang' {
                $evidence.detail=Require-Error { Invoke-NativeProcess -FilePath $PsExe -Arguments (Child-Args '[Threading.Thread]::Sleep(30000)') -TimeoutMilliseconds 1200 } 'deadline|did not exit'
                Check ($watch.ElapsedMilliseconds -lt 8000) 'product timeout did not bound hanging child'
            }
            'nonreading_stdin' {
                $evidence.detail=Require-Error { Invoke-NativeProcess -FilePath $PsExe -Arguments (Child-Args '[Threading.Thread]::Sleep(30000)') -StandardInput ('x'*1048576) -TimeoutMilliseconds 1200 } 'deadline|did not exit'
                Check ($watch.ElapsedMilliseconds -lt 8000) 'stdin write blocked the product deadline'
            }
            'stdin_complete' {
                $code='$s=[Console]::In.ReadToEnd();[Console]::Out.Write($s.Length.ToString());exit 0'
                $r=Invoke-NativeProcess -FilePath $PsExe -Arguments (Child-Args $code) -StandardInput ('x'*1048576) -TimeoutMilliseconds 10000
                Check ($r.ExitCode -eq 0 -and $r.StandardOutput -ceq '1048576') 'input was truncated or EOF was not delivered'
                $evidence.detail=$r.StandardOutput
            }
            'both_pipe_flood' {
                $code='[Console]::Out.Write("HEAD_OUT");[Console]::Error.Write("HEAD_ERR");$b="x"*65536;for($i=0;$i -lt 64;$i++){[Console]::Out.Write($b);[Console]::Error.Write($b)};[Console]::Out.Write("TAIL_OUT");[Console]::Error.Write("TAIL_ERR");exit 0'
                $r=Invoke-NativeProcess -FilePath $PsExe -Arguments (Child-Args $code) -TimeoutMilliseconds 10000 -MaximumOutputBytes 4096 -HeadTail
                Check ($r.ExitCode -eq 0 -and $r.Truncated) 'bounded log flood did not complete and mark truncation'
                Check ($r.StandardOutput.StartsWith('HEAD_OUT') -and $r.StandardOutput.EndsWith('TAIL_OUT')) 'stdout head/tail lost'
                Check ($r.StandardError.StartsWith('HEAD_ERR') -and $r.StandardError.EndsWith('TAIL_ERR')) 'stderr head/tail lost'
                Check ($r.StandardOutput.Length -lt 10000 -and $r.StandardError.Length -lt 10000) 'retained output is unbounded'
                Check ($r.StandardOutputBytes -eq 4194320 -and $r.StandardErrorBytes -eq 4194320) 'drained output accounting is inaccurate'
                $evidence.detail=@{stdout_bytes=$r.StandardOutputBytes;stderr_bytes=$r.StandardErrorBytes;retained_stdout=$r.StandardOutput.Length;retained_stderr=$r.StandardError.Length}
            }
            'strict_overflow' {
                $evidence.detail=Require-Error { Invoke-NativeProcess -FilePath $PsExe -Arguments (Child-Args '[Console]::Out.Write("x"*1048576);[Threading.Thread]::Sleep(30000)') -TimeoutMilliseconds 10000 -MaximumOutputBytes 4096 } 'exceeded|output.*bound'
                Check ($watch.ElapsedMilliseconds -lt 8000) 'strict overflow waited for hanging child'
            }
            'first_error_preserved' {
                $DockerCommand=$PsExe
                $evidence.detail=Require-Error { Invoke-DockerProcess -Arguments (Child-Args '[Console]::Error.Write("ORIGINAL_DIAG_37");exit 37') -TimeoutMilliseconds 10000 } 'exit code 37.*|ORIGINAL_DIAG_37'
                Check ($evidence.detail -match 'ORIGINAL_DIAG_37' -and $evidence.detail -match '37') 'original exit and diagnostic were replaced'
            }
            'budget_exhausted_before_start' {
                $marker=Join-Path $OutputRoot 'must-not-start.txt'
                Start-NativeProcessBudget -Seconds 1
                [Threading.Thread]::Sleep(1100)
                $code="[IO.File]::WriteAllText('"+($marker -replace "'","''")+"','started')"
                $evidence.detail=Require-Error { Invoke-NativeProcess -FilePath $PsExe -Arguments (Child-Args $code) -TimeoutMilliseconds 10000 } 'budget exhausted before start'
                Check (-not (Test-Path -LiteralPath $marker)) 'child started after the shared budget expired'
            }
            default { throw ('unknown independent case '+$Case) }
        }
        $evidence.status='pass'
    } catch {
        $evidence.detail=$_.ToString()
        # Retain the actual successful-call pipeline shape when a product
        # helper unexpectedly emitted extra objects before its result.
        if (Get-Variable -Name r -Scope Local -ErrorAction SilentlyContinue) {
            $evidence['returned_types']=@($r | ForEach-Object { $_.GetType().FullName })
        }
    }
    $evidence.elapsed_ms=$watch.ElapsedMilliseconds
    $json=$evidence | ConvertTo-Json -Compress -Depth 8
    [IO.File]::WriteAllText((Join-Path $OutputRoot ($Case+'.json')),$json,$Utf8)
    $json
    if ($evidence.status -cne 'pass') { exit 1 }; exit 0
}

foreach ($protected in @('C:\ProgramData','C:\Windows','C:\Program Files')) {
    Check (-not $OutputRoot.StartsWith($protected,[StringComparison]::OrdinalIgnoreCase)) 'output must be disposable'
}
Check (-not (Test-Path -LiteralPath $OutputRoot)) 'output must be new'
[void](New-Item -ItemType Directory -Path $OutputRoot)
Add-Type -Path (Join-Path $PSScriptRoot 'test_mineru_m6_process.cs') -ErrorAction Stop
$cases=@('normal_nonzero','hang','nonreading_stdin','stdin_complete','both_pipe_flood','strict_overflow','first_error_preserved','budget_exhausted_before_start')
$results=[Collections.Generic.List[object]]::new()
foreach ($name in $cases) {
    $arguments=[string[]]@('-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',$PSCommandPath,
        '-InstallerPath',$InstallerPath,'-OutputRoot',$OutputRoot,'-Case',$name)
    $child=[M6BoundedProcess]::new($PsExe,$arguments,$OutputRoot,(Join-Path $OutputRoot $name))
    $row=[ordered]@{case=$name;status='fail';pid=$child.Pid;creation_filetime=$child.CreationFiletime;exit_code=$null;forced=$null;active_processes=$null;error=$null}
    try {
        $child.Finish(15000)
        $row.exit_code=$child.ExitCode; $row.forced=$child.ForcedTermination; $row.active_processes=$child.ActiveJobProcesses
        Check ($child.ExactProcessExited -and -not $child.ForcedTermination -and $child.ActiveJobProcesses -eq 0) 'independent outer owner had to clean up product execution'
        Check ($child.ExitCode -eq 0) ('case failed: '+$child.ReadStdout()+' '+$child.ReadStderr())
        $receipt=[IO.File]::ReadAllText((Join-Path $OutputRoot ($name+'.json')),$Utf8) | ConvertFrom-Json
        Check ($receipt.status -ceq 'pass' -and $receipt.case -ceq $name) 'missing/mismatched case evidence'
        $row.status='pass'
    } catch { $row.error=$_.ToString() }
    finally { try { $child.Dispose() } catch { $row.error=[string]$row.error+'; cleanup: '+$_.ToString();$row.status='fail' } }
    $results.Add($row)
    Write-Host ($row.status.ToUpperInvariant()+' '+$name)
}
$failed=@($results | Where-Object {$_.status -cne 'pass'}).Count
$final=[ordered]@{schema='independent-installer-process-tests.v1';status=$(if($failed -eq 0){'pass'}else{'fail'});installer_sha256=('sha256:'+(Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash.ToLowerInvariant());cases=@($results.ToArray())}
$json=$final | ConvertTo-Json -Depth 12
[IO.File]::WriteAllText((Join-Path $OutputRoot 'runner-evidence.json'),$json,$Utf8)
$json
if ($failed -ne 0) { exit 1 }
