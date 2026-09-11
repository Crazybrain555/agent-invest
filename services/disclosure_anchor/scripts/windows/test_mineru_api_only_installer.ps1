<#
.SYNOPSIS
Independent functions-only API installer regression tests (Windows PowerShell 5.1).
.DESCRIPTION
Parses the installer as data and loads only an explicit set of FunctionDefinitionAst
nodes. Never dot-sources or invokes its top level. Docker/native functions are NOT
loaded. Strict mocks accept only expected commands and record every argument.
All file fixtures are under one fresh disposable directory; raw failures remain.
.EXAMPLE
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\test_mineru_api_only_installer.ps1 -InstallerPath .\install_mineru_fixed_api.ps1
#>
[CmdletBinding()]
param([string]$InstallerPath = (Join-Path $PSScriptRoot 'install_mineru_fixed_api.ps1'), [string]$OutputRoot = '')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'Windows PowerShell 5.1 required' }
if ([string]::IsNullOrEmpty($OutputRoot)) { $OutputRoot = Join-Path ([IO.Path]::GetTempPath()) ('m6-installer-tests-' + [Guid]::NewGuid().ToString('N')) }
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
foreach ($path in @([Environment]::SystemDirectory, 'C:\ProgramData', 'C:\Program Files')) {
    if ($OutputRoot.StartsWith($path, [StringComparison]::OrdinalIgnoreCase)) { throw 'requires disposable output root' }
}
if (Test-Path -LiteralPath $OutputRoot) { throw 'output root must be fresh' }
[void](New-Item -ItemType Directory -Path $OutputRoot)
$tokens = $null; $parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile([IO.Path]::GetFullPath($InstallerPath), [ref]$tokens, [ref]$parseErrors)
if (@($parseErrors).Count) { throw ($parseErrors | Out-String) }
$Allow = @('ConvertFrom-NativeProcessText', 'Get-OptionalImageId', 'Get-StableServiceEpochs', 'Assert-StableServiceEpochs',
    'Assert-ApiOnlyUpgradeInputs', 'Invoke-ApiOnlyRecreate', 'Get-ApiCompatBuildIdentity', 'Get-ValidatedApiCompatImage',
    'Get-ValidatedPublishedApiCompatImage', 'Remove-CompatBuildTag', 'Restore-ApiCompatTag', 'Restore-PreviousDeployment')
foreach ($name in $Allow) {
    $nodes = @($ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true))
    if ($nodes.Count -ne 1) { throw "expected one function $name" }
    . ([ScriptBlock]::Create($nodes[0].Extent.Text))
}
$script:Calls = New-Object Collections.ArrayList
$script:Results = New-Object Collections.ArrayList
$script:Image = 'sha256:' + ('a' * 64)
$script:OldImage = 'sha256:' + ('b' * 64)
$script:ImageInspect = $null
$script:StableInspect = $null
$script:OptionalIds = @()
$script:RuntimeCount = 0
$script:NativeReply = $null
$script:WaitCount = 0
function Invoke-NativeProcess { throw 'SAFETY: installer native process execution forbidden in independent test' }
function Invoke-DockerProcess {
    param([string[]]$Arguments, [int[]]$AllowedExitCodes)
    [void]$script:Calls.Add(@($Arguments))
    if (($Arguments -join '|') -notmatch '^image\|inspect\|--format\|\{\{\.Id\}\}\|') { throw 'unexpected DockerProcess mock command' }
    if ($null -ne $script:NativeReply) { return $script:NativeReply }
    if ($script:OptionalIds.Count -eq 0) { throw 'no mock image reply queued' }
    $id = $script:OptionalIds[0]
    $script:OptionalIds = @($script:OptionalIds | Select-Object -Skip 1)
    return [pscustomobject]@{ ExitCode = 0; StandardOutput = $id; StandardError = '' }
}
function Invoke-Docker {
    param([string[]]$Arguments)
    [void]$script:Calls.Add(@($Arguments))
    if (($Arguments -join '|') -eq 'inspect|mineru-api-proxy|mineru-openai-server') { return ($script:StableInspect | ConvertTo-Json -Depth 10 -Compress) }
    if (($Arguments -join '|') -eq 'inspect|mineru-api') { return ('[{"Image":"' + $script:RunningApiImage + '"}]') }
    if ($Arguments.Count -eq 3 -and $Arguments[0] -eq 'image' -and $Arguments[1] -eq 'inspect') { return ($script:ImageInspect | ConvertTo-Json -Depth 10 -Compress) }
    $expected = @('compose', '--project-name', $ProjectName, '--file', $ComposeTarget, 'up', '--detach', '--no-build', '--no-deps', '--force-recreate', 'mineru-api')
    if (($Arguments -join '|') -eq ($expected -join '|')) { $script:RunningApiImage = $(if ($script:ForceWrongRunning) { $script:Image } else { $script:PublishedApiImage }); return '' }
    if (($Arguments -join '|') -eq ('tag|' + $OldApiCompatImageId + '|' + $ApiCompatImage)) { $script:PublishedApiImage = $OldApiCompatImageId; return '' }
    if (($Arguments -join '|') -eq ('image|rm|' + $ApiCompatBuildTag)) { return '' }
    throw ('SAFETY: unexpected Docker command rejected by mock: ' + ($Arguments -join '|'))
}
function Wait-Healthy { $script:WaitCount++; return 'mock-healthy' }
function Get-ValidatedRuntime { $script:RuntimeCount++; return 'mock-runtime' }
function Check { param([bool]$Yes, [string]$Message) if (-not $Yes) { throw $Message } }
function Reject { param([scriptblock]$Action, [string]$Pattern) $errorText = ''; try { & $Action } catch { $errorText = $_ | Out-String }; Check ($errorText -match $Pattern) ('expected refusal ' + $Pattern + ', got: ' + $errorText) }
function Case {
    param([string]$Name, [scriptblock]$Action)
    $script:Calls.Clear()
    try { & $Action; [void]$script:Results.Add([ordered]@{ name = $Name; status = 'pass'; calls = @($script:Calls.ToArray()) }); Write-Host "PASS $Name" }
    catch { [void]$script:Results.Add([ordered]@{ name = $Name; status = 'fail'; error = ($_ | Out-String); calls = @($script:Calls.ToArray()) }); Write-Host "FAIL $Name : $_" }
}
function Stable-Containers {
    return @(
        [pscustomobject]@{ Name = '/mineru-api-proxy'; Id = ('c' * 64); Image = ('sha256:' + ('d' * 64)); RestartCount = 0; State = [pscustomobject]@{ Running = $true; OOMKilled = $false; StartedAt = '2026-01-01T00:00:00Z'; Health = [pscustomobject]@{ Status = 'healthy' } } },
        [pscustomobject]@{ Name = '/mineru-openai-server'; Id = ('e' * 64); Image = ('sha256:' + ('f' * 64)); RestartCount = 0; State = [pscustomobject]@{ Running = $true; OOMKilled = $false; StartedAt = '2026-01-02T00:00:00Z'; Health = [pscustomobject]@{ Status = 'healthy' } } }
    )
}
$ProjectName = 'independent-no-real-project'
$ComposeSource = Join-Path $OutputRoot 'compose-source.yml'; $ComposeTarget = Join-Path $OutputRoot 'compose-target.yml'
$CollectorTarget = Join-Path $OutputRoot 'collector.ps1'; $ReceiptTarget = Join-Path $OutputRoot 'receipt.json'
$ComposeBackup = Join-Path $OutputRoot 'compose.bak'; $CollectorBackup = Join-Path $OutputRoot 'collector.bak'; $ReceiptBackup = Join-Path $OutputRoot 'receipt.bak'
$ComposeExisted = $true; $CollectorExisted = $true; $ReceiptExisted = $true
$ComposeBackupCreated = $true; $CollectorBackupCreated = $true; $ReceiptBackupCreated = $true
$ApiCompatImage = 'test-api:published'; $ApiCompatBuildTag = 'test-api:unpublished'
$OldApiCompatImageId = $script:OldImage; $CampaignApiCompatImageId = $script:Image
$ReuseCurrentPublishedImage = $false; $ApiOnlyCompatibilityUpgrade = $true; $CompatTagSwitched = $true; $CompatBuildTagCreated = $true
$script:PublishedApiImage = $script:Image; $script:RunningApiImage = $script:Image; $script:ForceWrongRunning = $false
$utf8 = New-Object Text.UTF8Encoding($false)
foreach ($path in @($ComposeSource, $ComposeTarget, $CollectorTarget, $ReceiptTarget)) { [IO.File]::WriteAllText($path, 'original-fixture', $utf8) }
foreach ($path in @($ComposeBackup, $CollectorBackup, $ReceiptBackup)) { [IO.File]::WriteAllText($path, 'restored-fixture', $utf8) }

Case 'upgrade requires complete deployment and exact compose bytes' {
    Assert-ApiOnlyUpgradeInputs
    $ReceiptExisted = $false; Reject { Assert-ApiOnlyUpgradeInputs } 'complete existing'; $ReceiptExisted = $true
    [IO.File]::WriteAllText($ComposeTarget, 'changed-fixture', $utf8)
    Reject { Assert-ApiOnlyUpgradeInputs } 'unchanged compose bytes'
    [IO.File]::WriteAllText($ComposeTarget, 'original-fixture', $utf8)
    Check ($script:Calls.Count -eq 0) 'input check must not run Docker'
}
Case 'recreate invokes only API with no-build no-deps' {
    Invoke-ApiOnlyRecreate
    Check ($script:Calls.Count -eq 1) 'exactly one compose command'
    Check (($script:Calls[0] -join '|') -eq (@('compose','--project-name',$ProjectName,'--file',$ComposeTarget,'up','--detach','--no-build','--no-deps','--force-recreate','mineru-api') -join '|')) 'API recreate exact argv'
}
Case 'stable service identity requires healthy unique epochs and detects drift' {
    $script:StableInspect = Stable-Containers
    $expected = Get-StableServiceEpochs
    Assert-StableServiceEpochs $expected
    $script:StableInspect[0].State.StartedAt = '2026-01-03T00:00:00Z'
    Reject { Assert-StableServiceEpochs $expected } 'changed during API-only'
    $script:StableInspect = Stable-Containers; $script:StableInspect[1].State.OOMKilled = $true
    Reject { Get-StableServiceEpochs } 'invalid epoch'
    $script:StableInspect = @(($script:StableInspect)[0], ($script:StableInspect)[0])
    Reject { Get-StableServiceEpochs } 'not unique'
}
Case 'API upgrade rollback restores old image and never recreates stable services' {
    $script:PublishedApiImage = $script:Image; $script:RunningApiImage = $script:Image; $CompatTagSwitched = $true
    $script:StableInspect = Stable-Containers; $StableServiceEpochs = Get-StableServiceEpochs
    Restore-PreviousDeployment
    foreach ($p in @($ComposeTarget,$CollectorTarget,$ReceiptTarget)) { Check ([IO.File]::ReadAllText($p) -eq 'restored-fixture') 'backup restored byte-for-byte' }
    Check ($script:WaitCount -eq 1) 'waits for API health'
    $compose = @($script:Calls | Where-Object { $_[0] -eq 'compose' })
    Check ($compose.Count -eq 1) 'one API-only rollback recreate'
    Check ($script:PublishedApiImage -eq $script:OldImage -and $script:RunningApiImage -eq $script:OldImage) 'old image restored through tag then recreate, independently observed by inspect'
    $trace = @($script:Calls | ForEach-Object { $_ -join '|' })
    $tagIndex = [Array]::IndexOf($trace, ('tag|' + $script:OldImage + '|' + $ApiCompatImage))
    $upIndex = [Array]::IndexOf($trace, ($compose[0] -join '|'))
    Check ($tagIndex -ge 0 -and $upIndex -gt $tagIndex) 'old image tag restored before API recreate'
}
Case 'API upgrade rollback rejects wrong restored image' {
    $script:StableInspect = Stable-Containers; $StableServiceEpochs = Get-StableServiceEpochs
    $script:PublishedApiImage = $script:Image; $script:RunningApiImage = $script:Image; $CompatTagSwitched = $true
    $script:ForceWrongRunning = $true
    try { Reject { Restore-PreviousDeployment } 'did not restore the previous API image' } finally { $script:ForceWrongRunning = $false }
}
Case 'campaign rollback pins same published image before and after API recreate' {
    $ReuseCurrentPublishedImage = $true; $script:OptionalIds = @($script:Image, $script:Image)
    $script:StableInspect = Stable-Containers; $StableServiceEpochs = Get-StableServiceEpochs
    Restore-PreviousDeployment
    Check ($script:RuntimeCount -eq 1) 'campaign rollback validates runtime'
    Check ($script:OptionalIds.Count -eq 0) 'both image reads consumed'
    Check (@($script:Calls | Where-Object { $_[0] -eq 'tag' }).Count -eq 0) 'campaign rollback never retags image'
    $script:OptionalIds = @($script:OldImage)
    Reject { Restore-PreviousDeployment } 'drifted before API-only rollback'
    $script:OptionalIds = @($script:Image, $script:OldImage)
    Reject { Restore-PreviousDeployment } 'drifted during API-only rollback'
}
$ExpectedImageId = 'sha256:' + ('0' * 64); $CapacityPolicy = 'test-capacity'; $HeapReturnPolicy = 'test-compat'
$identity = [ordered]@{ patcher_sha256 = ('sha256:' + ('1' * 64)); dockerfile_sha256 = ('sha256:' + ('2' * 64)); task_protocol_v2_sha256 = ('sha256:' + ('3' * 64)) }
function Valid-Image {
    return @([pscustomobject]@{ Id = $script:Image; Config = [pscustomobject]@{ Labels = [pscustomobject]@{
        'io.agent-invest.mineru.base-image-digest' = $ExpectedImageId; 'io.agent-invest.mineru.capacity-policy' = $CapacityPolicy
        'io.agent-invest.mineru.compatibility-policy' = $HeapReturnPolicy; 'io.agent-invest.mineru.compatibility-patcher-sha256' = $identity.patcher_sha256
        'io.agent-invest.mineru.compatibility-dockerfile-sha256' = $identity.dockerfile_sha256; 'io.agent-invest.mineru.task-protocol-v2-sha256' = $identity.task_protocol_v2_sha256
    }; Env = @('MINERU_MALLOC_TRIM=1','MINERU_PHASE_TRACE=0') } })
}
Case 'image binding rejects identity labels environment and duplicate images' {
    $script:ImageInspect = @(Valid-Image)
    $bound = Get-ValidatedApiCompatImage -Reference $ApiCompatImage -RequiredImageId $script:Image -BuildIdentity $identity
    Check ($bound.image_id -eq $script:Image) 'returned binding equals required content ID'
    foreach ($key in @($script:ImageInspect[0].Config.Labels.PSObject.Properties.Name)) {
        $script:ImageInspect = @(Valid-Image); $script:ImageInspect[0].Config.Labels.$key = 'drift'
        Reject { Get-ValidatedApiCompatImage $ApiCompatImage $script:Image $identity } 'labels or environment drifted'
    }
    $script:ImageInspect = @(Valid-Image); $script:ImageInspect[0].Config.Env += 'MINERU_TASK_PROTOCOL_V2=1'
    Reject { Get-ValidatedApiCompatImage $ApiCompatImage $script:Image $identity } 'labels or environment drifted'
    $script:ImageInspect = @(Valid-Image); $script:ImageInspect[0].Id = $script:OldImage
    Reject { Get-ValidatedApiCompatImage $ApiCompatImage $script:Image $identity } 'not uniquely bound'
    $script:ImageInspect = @((Valid-Image)[0], (Valid-Image)[0])
    Reject { Get-ValidatedApiCompatImage $ApiCompatImage $script:Image $identity } 'not uniquely bound'
}
Case 'optional image absence is distinguished from daemon or malformed errors' {
    $script:NativeReply = [pscustomobject]@{ ExitCode = 1; StandardOutput = ''; StandardError = 'Error response from daemon: No such image: test:missing' }
    Check ($null -eq (Get-OptionalImageId 'test:missing')) 'only canonical absence maps to null'
    $script:NativeReply.StandardError = 'permission denied'
    Reject { Get-OptionalImageId 'test:missing' } 'cannot inspect optional Docker'
    $script:NativeReply = [pscustomobject]@{ ExitCode = 0; StandardOutput = 'not-an-image-id'; StandardError = '' }
    Reject { Get-OptionalImageId 'test:missing' } 'invalid image ID'
    $script:NativeReply = $null
}
$failed = @($script:Results | Where-Object { $_.status -ne 'pass' }).Count
$evidence = [ordered]@{ contract_version = 'm6.independent-installer-tests.v1'; installer_sha256 = ('sha256:' + (Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()); script_sha256 = ('sha256:' + (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()); loaded_functions = $Allow; top_level_executed = $false; native_or_docker_executed = $false; output_root = $OutputRoot; failed = $failed; results = @($script:Results.ToArray()) }
[IO.File]::WriteAllText((Join-Path $OutputRoot 'installer-evidence.json'), ($evidence | ConvertTo-Json -Depth 20), $utf8)
if ($failed) { exit 1 }
Write-Host 'PASS independent installer functions-only mocked boundaries; no deployment executed.'
exit 0
