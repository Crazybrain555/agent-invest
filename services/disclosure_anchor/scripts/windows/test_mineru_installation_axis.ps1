<#
.SYNOPSIS
Independent release-wrapper routing tests against the real installer preflight.
.DESCRIPTION
Evaluates only the wrapper's argument-construction AST statements and allowlisted
installer functions/parameter guards. Docker config reads return the pinned local
fixture; installer top-level, native processes, HTTP and installation never run.
Requires Windows PowerShell 5.1. PreviousWrapperPath optionally replays the exact
failed release wrapper to prove the ratio-only counterexample before the fix.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$WrapperPath,
    [Parameter(Mandatory=$true)][string]$InstallerPath,
    [Parameter(Mandatory=$true)][string]$OutputRoot,
    [string]$ActualComposePairPath = '',
    [string]$PreviousWrapperPath = ''
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
if($PSVersionTable.PSVersion.Major-ne 5){throw 'Windows PowerShell 5.1 required'}
if(-not [Environment]::Is64BitProcess){throw '64-bit PowerShell required'}
if([string]::IsNullOrEmpty($ActualComposePairPath)){
    if([string]::IsNullOrEmpty($PSCommandPath) -or -not [IO.Path]::IsPathRooted($PSCommandPath)){throw 'absolute test script path required'}
    $ActualComposePairPath=[IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $PSCommandPath) '..\..\tests\fixtures\mineru_device_profile\compose-pair.json'))
}
$OutputRoot=[IO.Path]::GetFullPath($OutputRoot)
foreach($protected in @('C:\ProgramData','C:\Windows','C:\Program Files',[Environment]::SystemDirectory)){
    if($OutputRoot.StartsWith($protected,[StringComparison]::OrdinalIgnoreCase)){throw 'requires disposable test output'}
}
if(Test-Path -LiteralPath $OutputRoot){throw 'output root must be new'}
$Utf8=New-Object Text.UTF8Encoding($false,$true)
$PairSha=(Get-FileHash -LiteralPath $ActualComposePairPath -Algorithm SHA256).Hash.ToLowerInvariant()
if($PairSha-cne '35bc7c11e2a4b19cc0c22ad71f7297a1f90affa604360bff18651c4e4f667994'){throw 'independent compose fixture identity differs'}
$Pair=[IO.File]::ReadAllText($ActualComposePairPath,$Utf8)|ConvertFrom-Json
function Read-Ast {param([string]$Path)
    $tokens=$null;$errors=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile([IO.Path]::GetFullPath($Path),[ref]$tokens,[ref]$errors)
    if(@($errors).Count){throw ($errors|Out-String)}
    return $ast
}
function Get-ArgumentBuilder {param([string]$Path)
    $ast=Read-Ast $Path
    $start=@($ast.FindAll({param($n)$n -is [Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text-ceq '$installerParameters'},$true))
    if($start.Count-ne 1){throw 'expected one real wrapper installerParameters assignment'}
    $statements=@($start[0].Parent.Statements)
    $selected=@();$inside=$false;$closed=$false
    foreach($statement in $statements){
        if($statement.Extent.StartOffset-eq $start[0].Extent.StartOffset){$inside=$true}
        if($inside){$selected+=,$statement}
        if($inside -and $statement -is [Management.Automation.Language.AssignmentStatementAst] -and $statement.Left.Extent.Text-ceq '$childCommand'){
            $closed=$true;break
        }
    }
    if(-not $closed){throw 'wrapper argument construction must end before child execution'}
    foreach($statement in $selected){
        foreach($command in $statement.FindAll({param($n)$n -is [Management.Automation.Language.CommandAst]},$true)){
            if($command.GetCommandName()-cnotin @('Join-Path','Fail')){throw ('unexpected command in argument-only AST: '+$command.Extent.Text)}
        }
    }
    return [ScriptBlock]::Create(($selected|ForEach-Object{$_.Extent.Text})-join "`n")
}
$Builder=Get-ArgumentBuilder $WrapperPath
$PreviousBuilder=$null
if(-not [string]::IsNullOrEmpty($PreviousWrapperPath)){$PreviousBuilder=Get-ArgumentBuilder $PreviousWrapperPath}
$InstallerAst=Read-Ast $InstallerPath
$Allow=@('Get-CanonicalObjectJson','Get-ResolvedCompose','Assert-CapacityCompose',
    'Assert-ApiStopBudgetTransition','Remove-ApiStopBudgetProjection',
    'Get-ApiDeviceProfile','Assert-ApiDeviceTransition','Assert-ApiOnlyUpgradeInputs')
foreach($name in $Allow){
    $nodes=@($InstallerAst.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name-ceq $name},$true))
    if($nodes.Count-ne 1){throw "expected one actual installer function $name"}
    . ([ScriptBlock]::Create($nodes[0].Extent.Text))
}
# The real parameter declarations and initial pure guards are retained. Stop
# before Docker executable discovery; the rest of installer top-level is not run.
$Prefix=@();$FoundBoundary=$false
foreach($statement in $InstallerAst.EndBlock.Statements){
    if($statement -is [Management.Automation.Language.AssignmentStatementAst] -and $statement.Left.Extent.Text-ceq '$DockerCommand'){$FoundBoundary=$true;break}
    $Prefix+=,$statement.Extent.Text
}
if(-not $FoundBoundary){throw 'installer pre-IO boundary was not found'}
$Preflight=[ScriptBlock]::Create('[CmdletBinding()]'+$InstallerAst.ParamBlock.Extent.Text+"`n"+($Prefix-join "`n")+@'

$CapacityInputs=$script:FixtureCapacityInputs
$script:ReceivedDeviceProfile=$ApiDeviceProfile
$script:ReceivedSource=$ComposeSource
$script:ReceivedCapacitySha=$ExpectedCapacityConfigSha256
Assert-ApiOnlyUpgradeInputs
'@)
[void](New-Item -ItemType Directory -Path $OutputRoot)
$script:Results=New-Object Collections.ArrayList
$script:Calls=New-Object Collections.ArrayList
$script:LastArguments=@()
$ProjectName='independent-axis-no-real-project'
$ComposeExisted=$true;$CollectorExisted=$true;$ReceiptExisted=$true
$OldCapacity='sha256:'+('a'*64);$NewCapacity='sha256:'+('b'*64)
function Check {param([bool]$Condition,[string]$Message)if(-not $Condition){throw $Message}}
function Reject {param([scriptblock]$Action,[string]$Pattern,[string]$Context)
    $caught='';try{& $Action}catch{$caught=$_|Out-String}
    Check ($caught-match $Pattern) "expected refusal ($Context), pattern $Pattern, got: $caught"
}
function Clone {param($Value)return (($Value|ConvertTo-Json -Depth 80 -Compress)|ConvertFrom-Json)}
function Fail {param([int]$Code,[string]$Message)throw "wrapper refusal ${Code}: $Message"}
function Invoke-NativeProcess {throw 'SAFETY native execution forbidden'}
function Invoke-DockerProcess {throw 'SAFETY Docker process execution forbidden'}
function Invoke-RestMethod {throw 'SAFETY HTTP execution forbidden'}
function Invoke-WebRequest {throw 'SAFETY HTTP execution forbidden'}
function Invoke-Docker {param([string[]]$Arguments)
    [void]$script:Calls.Add(@($Arguments))
    if($Arguments.Count-eq 8 -and ($Arguments[0..3]-join '|')-ceq "compose|--project-name|$ProjectName|--file" -and
       ($Arguments[5..7]-join '|')-ceq 'config|--format|json'){
        if($Arguments[4]-ceq $ComposeSource){return ($script:NextCompose|ConvertTo-Json -Depth 80 -Compress)}
        if($Arguments[4]-ceq $ComposeTarget){return ($script:PreviousCompose|ConvertTo-Json -Depth 80 -Compress)}
    }
    throw ('SAFETY unexpected Docker operation '+($Arguments-join '|'))
}
function Compose {param([string]$Profile,[string]$Ratio)
    $value=Clone $(if($Profile-ceq 'cuda0'){$Pair.next}else{$Pair.previous})
    $api=$value.services.'mineru-api'
    $api.command[-1]='14'
    $environment=@{
        MINERU_API_MAX_CONCURRENT_REQUESTS='7';MINERU_API_MAX_PENDING_TASKS='8';MINERU_API_FINALIZER_SLOTS='1'
        MINERU_PROCESSING_WINDOW_SIZE='16';OMP_NUM_THREADS='4';MKL_NUM_THREADS='4';OPENBLAS_NUM_THREADS='1'
        MINERU_PDF_RENDER_THREADS='3';MINERU_HYBRID_BATCH_RATIO=$Ratio;MINERU_ENABLE_PIPELINE_INFERENCE_LOCKS='1'
        MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES='268435456';MINERU_TASK_PROTOCOL_V2_MAX_UNACKED_BYTES='2147483648'
    }
    foreach($key in $environment.Keys){$api.environment|Add-Member -NotePropertyName $key -NotePropertyValue $environment[$key] -Force}
    $api|Add-Member -NotePropertyName stop_grace_period -NotePropertyValue '10s'
    return $value
}
function Reset {param([string]$PreviousProfile='cuda0',[string]$NextProfile='cuda0',[string]$PreviousRatio='2',[string]$NextRatio='1')
    $script:PreviousCompose=Compose $PreviousProfile $PreviousRatio
    $script:NextCompose=Compose $NextProfile $NextRatio
    $script:FixtureCapacityInputs=[pscustomobject]@{environment=@{};config=[pscustomobject]@{final_http_limit_per_loop=14}}
    foreach($property in $script:NextCompose.services.'mineru-api'.environment.PSObject.Properties){
        if($property.Name-cne 'MINERU_DEVICE_MODE'){$script:FixtureCapacityInputs.environment[$property.Name]=[string]$property.Value}
    }
    $script:Calls.Clear();$script:LastArguments=@();$script:ReceivedDeviceProfile=$null
}
function Route {param([scriptblock]$ArgumentBuilder=$Builder,[AllowNull()][object]$PreviousSha=$OldCapacity,[string]$CandidateSha=$NewCapacity,[string]$Profile='cuda0')
    $binding=[pscustomobject]@{api_device_profile=$Profile;expected_previous_capacity_sha256=$PreviousSha}
    $capacitySha=$CandidateSha
    # Exercise actual quoting as well as dictionary routing, without creating any
    # package files: every resolved Compose read is intercepted above.
    $ReleaseRoot=Join-Path $OutputRoot "fixture release O'Brien"
    $windows=Join-Path $ReleaseRoot 'windows';$recordsDir=Join-Path $OutputRoot 'unused installer records'
    $budgetSeconds=1200;$installer=Join-Path $windows 'never-invoked.ps1';$consolePath=Join-Path $OutputRoot 'unused console.txt'
    . $ArgumentBuilder
    $script:LastArguments=@($installerArguments)
    $script:LastParameterNames=@($installerParameters.Keys)
    # This invokes a scriptblock made from the real installer param declarations,
    # using the real wrapper's rendered named tokens; it never runs childCommand.
    & ([ScriptBlock]::Create('& $Preflight '+($tokens-join ' ')))
    Check ($script:ReceivedSource-ceq (Join-Path $ReleaseRoot 'compose\mineru-windows.compose.yaml')) 'wrapper tokens must bind the exact quoted path'
    Check ($script:ReceivedCapacitySha-ceq $CandidateSha) 'wrapper must bind the candidate capacity identity'
}
function Case {param([string]$Name,[scriptblock]$Action)
    $script:Calls.Clear();$script:LastArguments=@()
    try{& $Action;[void]$script:Results.Add([ordered]@{name=$Name;status='pass';installer_arguments=@($script:LastArguments);calls=@($script:Calls.ToArray())});Write-Host "PASS $Name"}
    catch{[void]$script:Results.Add([ordered]@{name=$Name;status='fail';error=($_|Out-String);installer_arguments=@($script:LastArguments);calls=@($script:Calls.ToArray())});Write-Host "FAIL $Name : $_"}
}
# Match the non-strict scope used by the real installer for optional Compose keys.
Set-StrictMode -Off
Case 'changed explicit capacity on unchanged CUDA uses real capacity preflight' {
    Reset
    $before=Get-CanonicalObjectJson @($script:PreviousCompose,$script:NextCompose)
    Route
    Check ($script:LastParameterNames-cnotcontains 'ApiDeviceProfile') 'capacity-only change must not select device axis'
    Check ($script:ReceivedDeviceProfile-ceq '') 'real installer must receive its empty device-axis default'
    Check ($script:Calls.Count-eq 2) 'only two resolved Compose reads are permitted'
    Check ((Get-CanonicalObjectJson @($script:PreviousCompose,$script:NextCompose))-ceq $before) 'caller Compose fixtures remain intact'
}
Case 'changed explicit capacity on unchanged CPU uses real capacity preflight' {
    Reset -PreviousProfile cpu -NextProfile cpu
    Route -Profile cpu
    Check ($script:ReceivedDeviceProfile-ceq '') 'CPU capacity change also retains the capacity axis'
}
Case 'CPU to CUDA and CUDA to CPU device-only transitions remain accepted' {
    foreach($profile in @('cuda0','cpu')){
        $old=if($profile-ceq 'cuda0'){'cpu'}else{'cuda0'}
        Reset -PreviousProfile $old -NextProfile $profile -PreviousRatio 1 -NextRatio 1
        Route -PreviousSha $NewCapacity -Profile $profile
        Check ($script:LastParameterNames-ccontains 'ApiDeviceProfile') 'same capacity retains explicit device selection'
        Check ($script:ReceivedDeviceProfile-ceq $profile) 'real installer binds selected target profile'
    }
}
Case 'simultaneous capacity and device changes reject in both directions' {
    foreach($profile in @('cuda0','cpu')){
        $old=if($profile-ceq 'cuda0'){'cpu'}else{'cuda0'}
        Reset -PreviousProfile $old -NextProfile $profile
        Reject {Route -Profile $profile} 'outside API capacity fields' $profile
        Check ($script:LastParameterNames-cnotcontains 'ApiDeviceProfile') 'mixed change cannot switch to permissive device normalization'
    }
}
Case 'unchanged CPU and CUDA releases remain accepted through device preflight' {
    foreach($profile in @('cuda0','cpu')){
        Reset -PreviousProfile $profile -NextProfile $profile -PreviousRatio 1 -NextRatio 1
        Route -PreviousSha $NewCapacity -Profile $profile
        Check ($script:ReceivedDeviceProfile-ceq $profile) 'unchanged release retains runtime device proof'
    }
}
Case 'legacy predecessor retains explicit target profile rather than selecting capacity axis' {
    foreach($profile in @('cuda0','cpu')){
        Reset -PreviousProfile $profile -NextProfile $profile -PreviousRatio 1 -NextRatio 1
        Route -PreviousSha $null -Profile $profile
        Check ($script:ReceivedDeviceProfile-ceq $profile) 'absent previous identity preserves legacy routing'
    }
}
Case 'hash change without projected Compose delta remains a valid capacity upgrade' {
    Reset -PreviousRatio 1 -NextRatio 1
    Route
    Check ($script:ReceivedDeviceProfile-ceq '') 'unprojected capacity identity changes use capacity axis'
}
Case 'capacity route rejects unrelated inference network privilege and configuration changes' {
    foreach($variant in @('inference','network','privileged','cap-add','direct-device','proxy','api-image','mount','extra-env','gpu-id','command')){
        Reset;$api=$script:NextCompose.services.'mineru-api'
        switch($variant){
            'inference'{$script:NextCompose.services.'mineru-openai-server'.image='fixture/engine:other'}
            'network'{$script:NextCompose.networks.inference.internal=$false}
            'privileged'{$api|Add-Member -NotePropertyName privileged -NotePropertyValue $true}
            'cap-add'{$api|Add-Member -NotePropertyName cap_add -NotePropertyValue @('SYS_ADMIN')}
            'direct-device'{$api|Add-Member -NotePropertyName devices -NotePropertyValue @('/dev/nvidia0:/dev/nvidia0')}
            'proxy'{$script:NextCompose.services.'mineru-api-proxy'.read_only=$false}
            'api-image'{$api.image='fixture/api:other'}
            'mount'{$api|Add-Member -NotePropertyName volumes -NotePropertyValue @([pscustomobject]@{type='bind';source='fixture';target='/host'})}
            'extra-env'{$api.environment|Add-Member -NotePropertyName EXTRA -NotePropertyValue '1'}
            'gpu-id'{$api.deploy.resources.reservations.devices[0].device_ids=@('1')}
            'command'{$api.command[0]='--other'}
        }
        Reject {Route} 'outside API capacity fields|command differs' $variant
    }
}
Case 'same-capacity device route still refuses a ratio change despite matching candidate projection' {
    Reset
    Reject {Route -PreviousSha $NewCapacity} 'outside GPU0 and device mode' 'capacity drift on device axis'
}
Case 'candidate capacity projection and original stop budget remain enforced' {
    Reset;$script:NextCompose.services.'mineru-api'.environment.MINERU_HYBRID_BATCH_RATIO='4'
    Reject {Route} 'compose capacity projection differs' 'candidate env disagrees with selected capacity'
    Reset;$script:PreviousCompose.services.'mineru-api'.stop_grace_period='30s'
    Reject {Route} 'deployed API stop_grace_period differs' 'previous stop budget drift'
    Reset;$script:NextCompose.services.'mineru-api'.PSObject.Properties.Remove('stop_grace_period')
    Reject {Route} 'stop budget projection is absent' 'missing candidate stop budget'
    Reset;$script:PreviousCompose.services.'mineru-api'.PSObject.Properties.Remove('stop_grace_period')
    Route
}
if($null-ne $PreviousBuilder){
    Case 'captured previous wrapper reproduces CUDA ratio-only refusal with actual preflight' {
        Reset
        Reject {Route -ArgumentBuilder $PreviousBuilder} 'outside GPU0 and device mode' 'original observed ratio-only install failure'
        Check ($script:LastParameterNames-ccontains 'ApiDeviceProfile') 'old wrapper selected the wrong axis'
        Reset -PreviousRatio 1 -NextRatio 1
        Route -ArgumentBuilder $PreviousBuilder -PreviousSha $NewCapacity
    }
}
$receipt=[ordered]@{schema='independent-mineru-installation-axis-tests.v1';powershell_version=$PSVersionTable.PSVersion.ToString();process_id=$PID
    wrapper_sha256=(Get-FileHash -LiteralPath $WrapperPath -Algorithm SHA256).Hash.ToLowerInvariant()
    previous_wrapper_sha256=$(if($null-ne $PreviousBuilder){(Get-FileHash -LiteralPath $PreviousWrapperPath -Algorithm SHA256).Hash.ToLowerInvariant()}else{$null})
    installer_sha256=(Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    test_sha256=(Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
    actual_compose_pair_sha256=$PairSha;wrapper_top_level_executed=$false;installer_top_level_executed=$false;native_or_network_executed=$false
    results=@($script:Results.ToArray())}
[IO.File]::WriteAllText((Join-Path $OutputRoot 'result.json'),($receipt|ConvertTo-Json -Depth 80),$Utf8)
if(@($script:Results|Where-Object{$_.status-cne 'pass'}).Count){exit 1}
exit 0
