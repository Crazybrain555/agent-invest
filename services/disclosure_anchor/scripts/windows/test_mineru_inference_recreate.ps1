<#
.SYNOPSIS
Offline Windows PowerShell 5.1 checks of the real inference recreation helpers.
.DESCRIPTION
Loads only installer function AST nodes. All Docker calls are strict in-memory fakes.
The installer top level, Docker, API, GPU, and file targets are never run.
#>
[CmdletBinding()]
param([string]$InstallerPath = (Join-Path $PSScriptRoot 'install_mineru_fixed_api.ps1'))
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'Windows PowerShell 5.1 required' }
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile([IO.Path]::GetFullPath($InstallerPath),[ref]$tokens,[ref]$errors)
if (@($errors).Count -ne 0) { throw ($errors | Out-String) }
foreach ($name in @('Get-MineruServiceEpochs','Assert-InferenceRecreateEpochs','Invoke-InferenceRecreate')) {
    $nodes=@($ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name},$true))
    if ($nodes.Count -ne 1) { throw ('installer helper missing or duplicated: ' + $name) }
    . ([ScriptBlock]::Create($nodes[0].Extent.Text))
}
$ProjectName='fixture-project'; $ComposeTarget='C:\fixture\compose.yaml'
$script:Calls=New-Object Collections.ArrayList
$script:Epoch=1
$script:Broken=$false
function Invoke-Docker {
    param([string[]]$Arguments,[int]$TimeoutMilliseconds)
    [void]$script:Calls.Add(@($Arguments))
    if (($Arguments -join '|') -eq 'inspect|mineru-api|mineru-api-proxy|mineru-openai-server') {
        $names=@('mineru-api','mineru-api-proxy','mineru-openai-server')
        $items=@()
        foreach ($name in $names) {
            $id=if ($name -eq 'mineru-openai-server' -and $script:Epoch -eq 2) { 'd' * 64 } else { 'a' * 64 }
            $started=if ($name -eq 'mineru-openai-server' -and $script:Epoch -eq 2) { '2026-01-02T00:00:00Z' } else { '2026-01-01T00:00:00Z' }
            $items+=@{Name=('/' + $name);Id=$id;Image=('sha256:' + ('b' * 64));RestartCount=0;
                State=@{Running=$true;OOMKilled=$false;StartedAt=$started;Health=@{Status=$(if ($script:Broken) {'unhealthy'} else {'healthy'})}}}
        }
        return (ConvertTo-Json -InputObject $items -Depth 8 -Compress)
    }
    $expected=@('compose','--project-name',$ProjectName,'--file',$ComposeTarget,'up','--detach','--no-build','--no-deps','--force-recreate','mineru-openai-server')
    if (($Arguments -join '|') -eq ($expected -join '|') -and $TimeoutMilliseconds -eq 900000) {
        $script:Epoch=2
        return ''
    }
    throw ('unexpected Docker command: ' + ($Arguments -join '|'))
}
function Reject([scriptblock]$Action,[string]$Pattern) {
    $caught=''
    try { & $Action } catch { $caught=$_.Exception.Message }
    if ($caught -notmatch $Pattern) { throw ('expected ' + $Pattern + ', got ' + $caught) }
}
$before=Get-MineruServiceEpochs
Reject { Assert-InferenceRecreateEpochs -Before $before -After $before } 'did not produce'
Invoke-InferenceRecreate
$after=Get-MineruServiceEpochs
Assert-InferenceRecreateEpochs -Before $before -After $after
$compose=@($script:Calls | Where-Object { $_[0] -eq 'compose' })
if ($compose.Count -ne 1) { throw 'recreation must issue exactly one named-service Compose request' }
$after['mineru-api'].started_at='2026-01-03T00:00:00Z'
Reject { Assert-InferenceRecreateEpochs -Before $before -After $after } 'inference recreation changed mineru-api started_at'
$script:Broken=$true
Reject { Get-MineruServiceEpochs } 'invalid epoch'
Write-Host 'PASS inference recreation exact named service and three-service epoch checks'
