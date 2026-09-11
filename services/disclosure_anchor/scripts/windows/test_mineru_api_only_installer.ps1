param([Parameter(Mandatory=$true)][string]$InstallerPath)
$ErrorActionPreference='Stop'
$tokens=$null;$parseErrors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($InstallerPath,[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count -ne 0){throw ($parseErrors|Out-String)}
foreach($name in @('Assert-ApiOnlyUpgradeInputs','Invoke-ApiOnlyRecreate','Restore-ApiCompatTag','Restore-PreviousDeployment')){
    $definition=$ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $name},$false)
    if($definition.Count -ne 1){throw "required installer function not unique: $name"}
    . ([ScriptBlock]::Create($definition[0].Extent.Text))
}
$scratch=Join-Path ([IO.Path]::GetTempPath()) ('m6-api-only-test-'+[Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($scratch)
$checks=[Collections.Generic.List[string]]::new()
$calls=[Collections.Generic.List[string]]::new()
$ProjectName='mineru-tailnet';$ApiCompatImage='test-api-tag';$ApiCompatBuildTag='test-build-tag'
$ComposeSource=Join-Path $scratch 'source.yaml';$ComposeTarget=Join-Path $scratch 'target.yaml';$ComposeBackup=Join-Path $scratch 'compose.bak'
$CollectorTarget=Join-Path $scratch 'collector.ps1';$CollectorBackup=Join-Path $scratch 'collector.bak'
$ReceiptTarget=Join-Path $scratch 'receipt.json';$ReceiptBackup=Join-Path $scratch 'receipt.bak'
$ComposeExisted=$true;$CollectorExisted=$true;$ReceiptExisted=$true
$ComposeBackupCreated=$true;$CollectorBackupCreated=$true;$ReceiptBackupCreated=$true
$ReuseCurrentPublishedImage=$false;$ApiOnlyCompatibilityUpgrade=$true
$OldApiCompatImageId='sha256:'+('a'*64);$script:FakeImage='sha256:'+('b'*64)
$StableServiceEpochs=@{fixture='unchanged'}
function Reset-RollbackFixture([bool]$TagSwitched=$true) {
    $calls.Clear()
    $script:CompatTagSwitched=$TagSwitched
    $script:FakeImage=if($TagSwitched){'sha256:'+('b'*64)}else{$OldApiCompatImageId}
    $script:FailHealth=$false;$script:FakeWrongImage=$false
    $script:FailStable=$false;$script:FailCompose=$false
    foreach($path in @($ComposeTarget,$CollectorTarget,$ReceiptTarget)){
        [IO.File]::WriteAllText($path,'new-bytes-to-be-restored')
    }
}
function Invoke-Docker {
    param([object[]]$Arguments)
    $calls.Add(($Arguments -join ' '))
    if($Arguments[0] -ceq 'tag'){$script:FakeImage=$Arguments[1];return}
    if($Arguments[0] -ceq 'compose'){
        if(($Arguments -join ' ') -cne "compose --project-name mineru-tailnet --file $ComposeTarget up --detach --no-build --no-deps --force-recreate mineru-api"){throw 'unexpected non-API compose mutation'}
        if($script:FakeImage -cne $OldApiCompatImageId){throw 'API recreation happened before restoring old image tag'}
        foreach($path in @($ComposeTarget,$CollectorTarget,$ReceiptTarget)){
            if([IO.File]::ReadAllText($path) -cne 'old-exact-bytes'){throw 'API recreation happened before restoring old files'}
        }
        if($script:FailCompose){throw 'injected rollback compose failure'}
        return
    }
    if(($Arguments -join ' ') -ceq 'inspect mineru-api'){
        return '[{"Image":"'+$(if($script:FakeWrongImage){'sha256:'+('c'*64)}else{$script:FakeImage})+'"}]'
    }
    throw 'unexpected Docker call in API-only rollback'
}
function Wait-Healthy {if($script:FailHealth){throw 'injected rollback health failure'};$calls.Add('healthy')}
function Assert-StableServiceEpochs {param($Expected);if($script:FailStable){throw 'injected stable epoch drift'};if($Expected.fixture -cne 'unchanged'){throw 'wrong stable identity'};$calls.Add('stable')}
function Remove-CompatBuildTag {$calls.Add('cleanup-build-tag')}
function Assert-Throws([ScriptBlock]$Action,[string]$Message){
    try {& $Action}catch{if($_.Exception.Message -notmatch [Regex]::Escape($Message)){throw};return}
    throw 'expected failure did not occur'
}
try{
    [IO.File]::WriteAllText($ComposeSource,'same-memory-and-swap-caps')
    [IO.File]::WriteAllText($ComposeTarget,'same-memory-and-swap-caps')
    Assert-ApiOnlyUpgradeInputs
    [IO.File]::WriteAllText($ComposeSource,'changed-memory')
    Assert-Throws {Assert-ApiOnlyUpgradeInputs} 'unchanged compose bytes'
    [IO.File]::WriteAllText($ComposeSource,'same-memory-and-swap-caps')
    $ReceiptExisted=$false
    Assert-Throws {Assert-ApiOnlyUpgradeInputs} 'complete existing deployment'
    $ReceiptExisted=$true;$checks.Add('unchanged-compose-and-complete-backup-preconditions')
    foreach($path in @($ComposeBackup,$CollectorBackup,$ReceiptBackup)){[IO.File]::WriteAllText($path,'old-exact-bytes')}
    Reset-RollbackFixture
    Restore-PreviousDeployment
    if($calls[0] -cne "tag $OldApiCompatImageId $ApiCompatImage" -or $calls[-1] -cne 'cleanup-build-tag'){throw 'rollback order or cleanup wrong'}
    foreach($path in @($ComposeTarget,$CollectorTarget,$ReceiptTarget)){if([IO.File]::ReadAllText($path) -cne 'old-exact-bytes'){throw 'backup bytes not restored'}}
    $checks.Add('rollback-restores-old-tag-and-bytes-before-API-only-recreate')
    Reset-RollbackFixture -TagSwitched $false
    Restore-PreviousDeployment
    if(@($calls|Where-Object {$_ -like 'tag *'}).Count -ne 0 -or $calls[-1] -cne 'cleanup-build-tag'){throw 'unswitched tag rollback failed'}
    $checks.Add('unswitched-tag-rollback-recreates-only-API-and-verifies-old-image')
    Reset-RollbackFixture
    $script:FailHealth=$true
    Assert-Throws {Restore-PreviousDeployment} 'injected rollback health failure'
    if($calls.Contains('cleanup-build-tag') -or $calls.Contains('inspect mineru-api')){throw 'health failure discarded evidence or continued'}
    $checks.Add('rollback-health-failure-propagates-and-retains-build-evidence')
    Reset-RollbackFixture
    $script:FakeWrongImage=$true
    Assert-Throws {Restore-PreviousDeployment} 'previous API image'
    if($calls.Contains('cleanup-build-tag')){throw 'image mismatch discarded build evidence'}
    $checks.Add('rollback-image-mismatch-fails-closed-and-retains-build-evidence')
    Reset-RollbackFixture
    $script:FailStable=$true
    Assert-Throws {Restore-PreviousDeployment} 'injected stable epoch drift'
    if($calls.Contains('cleanup-build-tag') -or $calls.Contains('inspect mineru-api')){throw 'epoch drift discarded evidence or continued'}
    $checks.Add('rollback-epoch-drift-propagates-without-further-inspection-or-cleanup')
    Reset-RollbackFixture
    $script:FailCompose=$true
    Assert-Throws {Restore-PreviousDeployment} 'injected rollback compose failure'
    if(@($calls|Where-Object {$_ -like 'compose *'}).Count -ne 1 -or $calls.Contains('healthy') -or $calls.Contains('cleanup-build-tag')){throw 'compose failure retried or discarded evidence'}
    $checks.Add('rollback-compose-failure-propagates-without-retry-or-cleanup')
    [ordered]@{checks=$checks;docker_calls=$calls;actual_docker_invoked=$false;scratch=$scratch}|ConvertTo-Json -Depth 6
}finally{
    # Only this test's newly created fixture tree, never deployment/runtime paths.
    [IO.Directory]::Delete($scratch,$true)
}
