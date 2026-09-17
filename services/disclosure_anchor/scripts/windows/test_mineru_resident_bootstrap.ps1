<# Independent entry-point tests. Invalid config hash prevents all Jobs,
   sockets, Docker and GPU use. Pure function cases never invoke the loader. #>
param(
    [Parameter(Mandatory=$true)][string]$SourceDirectory,
    [Parameter(Mandatory=$true)][string]$ExpectedStarterSha256,
    [Parameter(Mandatory=$true)][string]$ExpectedExporterSha256,
    [Parameter(Mandatory=$true)][string]$OutputDirectory
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'PowerShell 5 required' }
if (Test-Path -LiteralPath $OutputDirectory) { throw 'new output directory required' }
$null = New-Item -ItemType Directory -Path $OutputDirectory
$configPath = [IO.Path]::Combine($OutputDirectory,'invalid-hash-config.json')
[IO.File]::WriteAllText($configPath,'{}',[Text.UTF8Encoding]::new($false))
$results = [Collections.Generic.List[object]]::new()
function Check([bool]$Value,[string]$Name) {
    if (-not $Value) { throw ('assertion failed: '+$Name) }
}
function Case([string]$Name,[scriptblock]$Action) {
    try { $null=& $Action; $results.Add(@{name=$Name;pass=$true}) }
    catch { $results.Add(@{name=$Name;pass=$false;error=$_.Exception.ToString()}) }
}
function Reject([scriptblock]$Action) {
    $refused=$false
    try { $null=& $Action } catch { $refused=$true }
    Check $refused 'invalid input was accepted'
}
function Check-PureBootstrap($Ast,[string]$Role) {
    # These declarations exist only in this function scope, never in a real
    # entry's parent. A missing production helper cannot be supplied by a test.
    foreach ($functionName in @('Get-MineruBootstrapSha','Read-MineruBootstrap')) {
        $nodes=@($Ast.FindAll({param($node)
            $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName
        },$true))
        Check ($nodes.Count -eq 1) ('one production helper '+$functionName)
        . ([scriptblock]::Create($nodes[0].Extent.Text))
    }
    $pins=[Collections.Generic.List[IO.FileStream]]::new()
    try {
        $digest='sha256:'+(Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $bytes=Read-MineruBootstrap $configPath $digest 2
        Check ([Text.Encoding]::UTF8.GetString($bytes) -ceq '{}') 'exact valid bytes'
        Reject { Read-MineruBootstrap $configPath ('sha256:'+('a'*64)) 2 }
        Reject { Read-MineruBootstrap $configPath $digest 1 }
        Reject { Read-MineruBootstrap 'relative.json' $digest 2 }
        Reject { Read-MineruBootstrap $configPath 'bad-digest' 2 }
        Reject { Read-MineruBootstrap ([IO.Path]::Combine($OutputDirectory,'absent.json')) $digest 2 }
    } finally { foreach($pin in $pins){$pin.Dispose()} }
}
foreach ($entry in @(
    @{name='start_mineru_resident_telemetry.ps1';hash=$ExpectedStarterSha256;role='starter'},
    @{name='mineru_resident_telemetry_exporter.ps1';hash=$ExpectedExporterSha256;role='exporter'}
)) {
    $entryPath=[IO.Path]::Combine($SourceDirectory,$entry.name)
    Check (('sha256:'+(Get-FileHash -LiteralPath $entryPath -Algorithm SHA256).Hash.ToLowerInvariant()) -ceq $entry.hash) 'entry source identity'
    Case ($entry.role+' real fresh entry preserves bootstrap failure') {
        # A fresh native process, so the harness cannot provide missing helpers.
        $info=[Diagnostics.ProcessStartInfo]::new()
        $info.FileName=(Get-Process -Id $PID).Path
        $info.UseShellExecute=$false
        $info.CreateNoWindow=$true
        $info.RedirectStandardOutput=$true
        $info.RedirectStandardError=$true
        foreach($value in @($entryPath,$configPath)){Check (-not $value.Contains('"')) 'safe argument path'}
        $info.Arguments='-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'+$entryPath+'" -ConfigJsonPath "'+$configPath+'" -ExpectedConfigSha256 "sha256:'+('a'*64)+'"'
        $process=[Diagnostics.Process]::new();$process.StartInfo=$info
        try {
            Check ($process.Start()) 'child started'
            $stdout=$process.StandardOutput.ReadToEndAsync()
            $stderr=$process.StandardError.ReadToEndAsync()
            if (-not $process.WaitForExit(10000)) {
                $process.Kill();$process.WaitForExit()
                throw 'invalid-config child exceeded bounded bootstrap test'
            }
            Check ($stdout.Wait(2000) -and $stderr.Wait(2000)) 'pipes drained'
            $out=$stdout.GetAwaiter().GetResult();$err=$stderr.GetAwaiter().GetResult()
            [IO.File]::WriteAllText([IO.Path]::Combine($OutputDirectory,$entry.role+'.stdout'),$out)
            [IO.File]::WriteAllText([IO.Path]::Combine($OutputDirectory,$entry.role+'.stderr'),$err)
            Check ($process.ExitCode -ne 0) 'invalid checksum must fail'
            Check ($err.Contains('bootstrap SHA mismatch')) 'original checksum error retained'
            Check (-not $err.Contains('CommandNotFoundException')) 'bootstrap dependency exists'
        } finally { $process.Dispose() }
    }
    Case ($entry.role+' bounded pure bootstrap byte and path cases') {
        $tokens=$null;$errors=$null
        $ast=[Management.Automation.Language.Parser]::ParseFile($entryPath,[ref]$tokens,[ref]$errors)
        Check (@($errors).Count -eq 0) 'entry parses'
        Check-PureBootstrap $ast $entry.role
    }
}
$receipt=@{contract='mineru.resident-bootstrap-independent-test.v1';cases=$results.ToArray();
    starter_sha256=$ExpectedStarterSha256;exporter_sha256=$ExpectedExporterSha256;invalid_config_only=$true}
$json=$receipt|ConvertTo-Json -Depth 8
[IO.File]::WriteAllText([IO.Path]::Combine($OutputDirectory,'receipt.json'),$json)
Write-Output $json
if (@($results|Where-Object{-not $_.pass}).Count -ne 0) { exit 1 }
