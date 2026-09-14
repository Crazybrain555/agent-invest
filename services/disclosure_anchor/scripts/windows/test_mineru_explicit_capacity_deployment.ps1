<#
.SYNOPSIS
Independent functions-only explicit capacity installer and collector projection tests.
.DESCRIPTION
Loads allowlisted real AST definitions/statements, never installer/collector top level.
All Docker/native/network IO is mocked; only a fresh disposable directory is writable.
Requires Windows PowerShell 5.1. No native compilation, service, policy or runtime changes.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$InstallerPath,
    [Parameter(Mandatory=$true)][string]$CollectorPath,
    [string]$InputPath = '',
    [string]$SourceContext = '',
    [Parameter(Mandatory=$true)][string]$TestOutput
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
# Original cases select no device transition; device-specific tests are separate.
$ApiDeviceProfile=''
if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'Windows PowerShell 5.1 required' }
# Resolve defaults after Windows PowerShell has initialized script identity.
if([string]::IsNullOrEmpty($InputPath)){
    if([string]::IsNullOrEmpty($PSCommandPath) -or -not [IO.Path]::IsPathRooted($PSCommandPath)){throw 'cannot resolve default fixture without an absolute script path'}
    $TestScriptRoot=Split-Path -Parent $PSCommandPath
    $InputPath=[IO.Path]::GetFullPath((Join-Path $TestScriptRoot '..\..\tests\fixtures\mineru_explicit_capacity_deployment\inputs.json'))
}
if(-not (Test-Path -LiteralPath $InputPath -PathType Leaf)){throw 'capacity input fixture must be an existing file'}
$TestOutput=[IO.Path]::GetFullPath($TestOutput)
foreach($protected in @('C:\ProgramData','C:\Program Files',[Environment]::SystemDirectory)) {
    if($TestOutput.StartsWith($protected,[StringComparison]::OrdinalIgnoreCase)){throw 'requires disposable test output'}
}
if(Test-Path -LiteralPath $TestOutput){throw 'test output must be new'}
[void](New-Item -ItemType Directory -Path $TestOutput)
$Utf8=New-Object Text.UTF8Encoding($false,$true)
$Inputs=([IO.File]::ReadAllText($InputPath,$Utf8)|ConvertFrom-Json)
if($Inputs.schema -cne 'independent-capacity-deployment-inputs.v1'){throw 'independent fixture schema drift'}
function Read-Ast { param([string]$Path)
    $t=$null;$e=$null;$value=[Management.Automation.Language.Parser]::ParseFile([IO.Path]::GetFullPath($Path),[ref]$t,[ref]$e)
    if(@($e).Count){throw ($e|Out-String)};return $value
}
$InstallerAst=Read-Ast $InstallerPath;$CollectorAst=Read-Ast $CollectorPath
$Allow=@('Get-CanonicalObjectJson','Get-ExplicitCapacityInputs','Get-ResolvedCompose','Assert-CapacityCompose',
 'Assert-ApiOnlyUpgradeInputs','Get-Sha256Text','Assert-RequiredProperties','Assert-ClosedProperties',
 'Assert-CapacityIdleHealth','Assert-IdleHealth','Assert-CapacityFiles','Get-ApiCompatBuildIdentity',
 'Build-ValidatedApiCompatImage','Get-ValidatedApiCompatImage','Get-ValidatedPublishedApiCompatImage',
 'Get-OptionalImageId','ConvertFrom-NativeProcessText','Get-ValidatedRuntime','Assert-ExactCommand',
 'Assert-SinglePort','Assert-ExternalEgressBlocked','Get-RollbackRegistryWitness',
 'Assert-RollbackRegistryUnchanged','Restore-PreviousDeployment','Restore-ApiCompatTag',
 'Invoke-ApiOnlyRecreate','Remove-CompatBuildTag','Get-StableServiceEpochs','Assert-StableServiceEpochs')
foreach($name in $Allow){
    $nodes=@($InstallerAst.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true))
    if($nodes.Count-ne 1){throw "expected exactly one actual installer function $name"}
    . ([ScriptBlock]::Create($nodes[0].Extent.Text))
}
foreach($name in @('Convert-EnvironmentToMap','Select-ExactEnvironment','Get-ApiDeviceProfile','Assert-ApiDeviceRuntime')){
    $nodes=@($CollectorAst.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true))
    if($nodes.Count-ne 1){throw "expected exactly one collector function $name"}
    . ([ScriptBlock]::Create($nodes[0].Extent.Text))
}
function Statement-Index {param($Ast,[string]$Variable)
    $matches=@();for($i=0;$i-lt $Ast.EndBlock.Statements.Count;$i++){
        $n=$Ast.EndBlock.Statements[$i]
        if($n -is [Management.Automation.Language.AssignmentStatementAst] -and
           $n.Left -is [Management.Automation.Language.VariableExpressionAst] -and $n.Left.VariablePath.UserPath -ceq $Variable){$matches+=,$i}
    };if($matches.Count-ne 1){throw "expected one top-level assignment $Variable"};return [int]$matches[0]
}
function Actual-Slice {param($Ast,[string]$First,[string]$Until)
    $a=Statement-Index $Ast $First;$b=Statement-Index $Ast $Until
    if($a-ge $b){throw 'actual AST slice order changed'}
    return [ScriptBlock]::Create(($Ast.EndBlock.Statements[$a..($b-1)]|ForEach-Object{$_.Extent.Text}) -join "`n")
}
$selectionBody=Actual-Slice $InstallerAst 'ExplicitCapacity' 'ProgressPreference'
$selectionParameters=@($InstallerAst.ParamBlock.Parameters|Where-Object{$_.Name.VariablePath.UserPath-in @('CapacityConfigSource','ExpectedCapacityConfigSha256','ExpectedApiTaskSlots','ExpectedApiMaxPendingTasks')}|ForEach-Object{$_.Extent.Text})
$Selection=[ScriptBlock]::Create('[CmdletBinding()]param('+($selectionParameters-join ',')+')'+"`n"+$selectionBody.ToString())
$CollectorEnvironment=Actual-Slice $CollectorAst 'apiAllowedEnvironment' 'compatProbeCode'
$CollectorHealth=Actual-Slice $CollectorAst 'health' 'models'
$CollectorEpoch=Actual-Slice $CollectorAst 'apiEpochAfter' 'result'
$resultIndex=Statement-Index $CollectorAst 'result'
$CollectorResult=[ScriptBlock]::Create('param([string]$PSCommandPath)'+"`n"+(($CollectorAst.EndBlock.Statements[$resultIndex..($CollectorAst.EndBlock.Statements.Count-2)]|ForEach-Object{$_.Extent.Text}) -join "`n"))
$tailNodes=@($InstallerAst.FindAll({param($n) $n -is [Management.Automation.Language.AssignmentStatementAst] -and $n.Left -is [Management.Automation.Language.VariableExpressionAst] -and $n.Left.VariablePath.UserPath -ceq 'collectorObservation'},$true))
if($tailNodes.Count-ne 1){throw 'expected one formal collector observation assignment'}
$tailStatements=$tailNodes[0].Parent.Statements;$tailStart=-1;$tailEnd=-1
for($i=0;$i-lt $tailStatements.Count;$i++){
    if([object]::ReferenceEquals($tailStatements[$i],$tailNodes[0])){$tailStart=$i-1}
    if($tailStart-ge 0 -and $tailStatements[$i].Extent.Text-ceq 'Remove-CompatBuildTag'){$tailEnd=$i;break}
}
if($tailStart-lt 0 -or $tailEnd-le $tailStart){throw 'formal collector guard anchors changed'}
$CollectorTail=[ScriptBlock]::Create(($tailStatements[$tailStart..($tailEnd-1)]|ForEach-Object{$_.Extent.Text}) -join "`n")
$script:Calls=New-Object Collections.ArrayList;$script:Results=New-Object Collections.ArrayList
function Check {param([bool]$Condition,[string]$Message) if(-not $Condition){throw $Message}}
function Reject {param([scriptblock]$Action,[string]$Pattern,[string]$Context='')
    $caught='';try{& $Action}catch{$caught=$_|Out-String}
    Check ($caught -match $Pattern) "expected refusal $Pattern ($Context); actual: $caught"
}
function Clone {param($Value) return (($Value|ConvertTo-Json -Depth 100 -Compress)|ConvertFrom-Json)}
function Case {param([string]$Name,[scriptblock]$Action)
    $script:Calls.Clear()
    try {& $Action;[void]$script:Results.Add([ordered]@{name=$Name;status='pass';calls=@($script:Calls.ToArray())});Write-Host "PASS $Name"}
    catch{[void]$script:Results.Add([ordered]@{name=$Name;status='fail';error=($_|Out-String);calls=@($script:Calls.ToArray())});Write-Host "FAIL $Name : $_"}
}
function Invoke-NativeProcess {throw 'SAFETY native execution forbidden'}
function Invoke-RestMethod {param([string]$Uri,[int]$TimeoutSec)
    [void]$script:Calls.Add(@('http',$Uri,[string]$TimeoutSec))
    if($Uri -ceq 'http://127.0.0.1:30003/health' -and $TimeoutSec-eq 15){return $script:ProxyHealth}
    throw 'SAFETY unexpected network command'
}
function Invoke-DockerProcess {param([string[]]$Arguments,[string]$StandardInput='', [int[]]$AllowedExitCodes=@(0))
    [void]$script:Calls.Add(@($Arguments))
    if(($Arguments-join '|') -match '^image\|inspect\|--format\|\{\{\.Id\}\}\|'){
        return [pscustomobject]@{ExitCode=0;StandardOutput=$script:OptionalImage;StandardError=''}
    }
    if($Arguments.Count-eq 7 -and ($Arguments[0..1]-join '|') -ceq 'exec|-i' -and $Arguments[2]-ceq ('1'*64) -and
       ($Arguments[3..5]-join '|') -ceq '/usr/bin/python3.12|-I|-' -and $Arguments[6]-ceq $ExpectedCapacityConfigSha256){
        Check (-not [string]::IsNullOrEmpty($StandardInput)) 'real capacity file probe must be sent'
        $wire=if($null-ne $script:InstalledFilesRaw){$script:InstalledFilesRaw}else{($script:InstalledFiles|ConvertTo-Json -Depth 20 -Compress)}
        return [pscustomobject]@{ExitCode=0;StandardOutput=$wire;StandardError=''}
    }
    if($Arguments.Count-eq 6 -and ($Arguments[0..4]-join '|') -ceq 'exec|mineru-api|/usr/bin/python3.12|-I|-c' -and
        $Arguments[5].Contains('socket.create_connection')){
        return [pscustomobject]@{ExitCode=42;StandardOutput='MINERU_EGRESS_BLOCKED:TimeoutError';StandardError=''}
    }
    throw ('SAFETY unexpected DockerProcess: '+($Arguments-join '|'))
}
function Invoke-Docker {param([string[]]$Arguments)
    [void]$script:Calls.Add(@($Arguments))
    if($Arguments[0]-ceq 'build'){return ''}
    if($Arguments.Count-eq 3 -and ($Arguments[0..1]-join '|')-ceq 'image|inspect'){return ($script:ImageInspect|ConvertTo-Json -Depth 40 -Compress)}
    if(($Arguments-join '|')-ceq 'inspect|mineru-api|mineru-api-proxy|mineru-openai-server'){return ($script:Inspect|ConvertTo-Json -Depth 40 -Compress)}
    if(($Arguments-join '|')-ceq 'inspect|mineru-api-proxy|mineru-openai-server'){return (@($script:Inspect[1],$script:Inspect[2])|ConvertTo-Json -Depth 40 -Compress)}
    if(($Arguments-join '|')-ceq 'inspect|--format|{{.Id}} {{.State.StartedAt}}|mineru-api'){return $script:EpochAfter}
    if(($Arguments-join '|')-ceq 'network|inspect|mineru-tailnet_inference|mineru-tailnet_runtime'){
        return '[{"Name":"mineru-tailnet_inference","Internal":true},{"Name":"mineru-tailnet_runtime","Internal":false}]'
    }
    if($Arguments.Count-eq 8 -and $Arguments[0]-ceq 'compose' -and ($Arguments[5..7]-join '|')-ceq 'config|--format|json'){
        if($Arguments[4]-ceq $ComposeSource){return ($script:NextCompose|ConvertTo-Json -Depth 100 -Compress)}
        if($Arguments[4]-ceq $ComposeTarget){return ($script:PreviousCompose|ConvertTo-Json -Depth 100 -Compress)}
    }
    if(($Arguments-join '|')-ceq "exec|mineru-openai-server|/usr/bin/python3.12|-I|-c|import importlib.metadata; print(importlib.metadata.version('vllm'))"){return '0.21.0'}
    throw ('SAFETY unexpected Docker: '+($Arguments-join '|'))
}
function Wait-Healthy {return @($script:Health,$script:Models)}
function Get-QuiescentOutputState {param([switch]$CandidateSource)
    [void]$script:Calls.Add(@('quiescent',[string]$CandidateSource.IsPresent))
    if($null-ne $script:QuiescentError){throw $script:QuiescentError}
    return $script:QuiescentState
}
$Context=Join-Path $TestOutput 'context';[void](New-Item -ItemType Directory -Path $Context)
# A clean checkout uses its current actual source bytes. The four capacity
# source expectations remain independent literals in inputs.json. An explicit
# SourceContext still supports an already-pinned isolated test package.
$ServiceRoot=Split-Path -Parent (Split-Path -Parent (Split-Path -Parent ([IO.Path]::GetFullPath($InstallerPath))))
$ContextSources=@{
    'Dockerfile'='scripts/windows/mineru_heap_trim_compat/Dockerfile';
    'patch_mineru_344.py'='scripts/windows/mineru_heap_trim_compat/patch_mineru_344.py';
    'agent_task_protocol_v2.py'='scripts/windows/mineru_heap_trim_compat/agent_task_protocol_v2.py';
    'agent_capacity_config.py'='src/disclosure_anchor/application/contracts/mineru_capacity_config.py';
    'agent_capacity_file.py'='src/disclosure_anchor/adapters/runtime/mineru_capacity_file.py';
    'agent_capacity_bootstrap.py'='scripts/windows/mineru_heap_trim_compat/agent_capacity_bootstrap.py';
    'agent_capacity_observation.py'='scripts/windows/mineru_heap_trim_compat/agent_capacity_observation.py'
}
foreach($name in @('Dockerfile','patch_mineru_344.py','agent_task_protocol_v2.py','agent_capacity_config.py','agent_capacity_file.py','agent_capacity_bootstrap.py','agent_capacity_observation.py')){
    $source=if([string]::IsNullOrEmpty($SourceContext)){Join-Path $ServiceRoot $ContextSources[$name]}else{Join-Path $SourceContext $name}
    Copy-Item -LiteralPath $source -Destination (Join-Path $Context $name)
}
$CompatDockerfileSource=Join-Path $Context 'Dockerfile';$CompatPatcherSource=Join-Path $Context 'patch_mineru_344.py'
$CapacityConfigSource=Join-Path $Context 'capacity-config.json';$ProjectName='independent-no-real-project'
$ComposeSource=Join-Path $TestOutput 'next.yml';$ComposeTarget=Join-Path $TestOutput 'old.yml'
$CollectorTarget=Join-Path $TestOutput 'collector.ps1';$ReceiptTarget=Join-Path $TestOutput 'receipt.json'
$OutputRoot=Join-Path $TestOutput 'api-output';[void](New-Item -ItemType Directory -Path $OutputRoot)
$ExpectedImageId='sha256:109016f8f7666c3a86b0a6585f5b7003d1dd63c2d318f6ecd7ab1db5aa582458'
$ExpectedRepoDigest='mineru@'+$ExpectedImageId;$ApiCompatImage='agent-invest/mineru-api:3.4.4-serial-v1'
$ApiCompatBuildTag='independent-api:unpublished';$HeapReturnPolicy='glibc-malloc-trim-per-window.v1'
$ExpectedApiTaskSlots=1;$ExpectedApiMaxPendingTasks=1
foreach($path in @($ComposeSource,$ComposeTarget,$CollectorTarget,$ReceiptTarget)){[IO.File]::WriteAllText($path,'untouched-fixture',$Utf8)}
function Reset-Profile {param([int]$Index=0)
    $script:Profile=Clone $Inputs.profiles[$Index]
    $script:ApiDeviceProfile=''
    $script:ExplicitCapacity=$true;$script:CapacityInputs=$null
    $script:ExpectedCapacityConfigSha256=$script:Profile.config_sha256
    [IO.File]::WriteAllText($CapacityConfigSource,$script:Profile.config_text,$Utf8)
    $script:CapacityInputs=Get-ExplicitCapacityInputs
    $script:CapacityPolicy='single-process-explicit-capacity.v1'
    $script:Health=Clone $script:Profile.raw.api_health;$script:ProxyHealth=Clone $script:Health
    $script:Models=Clone $script:Profile.models;$script:Inspect=Clone $script:Profile.inspect
    # Literal baseline inspect fields omitted by the original CPU-only fixture.
    $script:Inspect[0].HostConfig|Add-Member -NotePropertyName Privileged -NotePropertyValue $false -Force
    $script:Inspect[0].HostConfig|Add-Member -NotePropertyName Devices -NotePropertyValue @() -Force
    $script:Inspect[0].HostConfig|Add-Member -NotePropertyName CapAdd -NotePropertyValue $null -Force
    $script:Inspect[0].HostConfig|Add-Member -NotePropertyName DeviceRequests -NotePropertyValue @() -Force
    $script:Inspect[0].Mounts[0].Source=$OutputRoot
    $script:ExpectedApiCompatImageId=$script:Profile.raw.api.image_id
    $script:CampaignApiCompatImageId=$script:ExpectedApiCompatImageId;$script:OptionalImage=$script:ExpectedApiCompatImageId
    $script:NextCompose=Clone $script:Profile.compose;$script:PreviousCompose=Clone $Inputs.legacy_compose
    $script:ComposeExisted=$true;$script:CollectorExisted=$true;$script:ReceiptExisted=$true
    $script:CompatBuildTagCreated=$false;$script:CompatTagSwitched=$false;$script:ReuseCurrentPublishedImage=$false
    $script:ApiOnlyCompatibilityUpgrade=$true;$script:DeploymentAttempted=$true
    $script:OldProjectContainers=@('mineru-api','mineru-api-proxy','mineru-openai-server')
    $script:QuiescentError=$null
    $script:QuiescentState=[pscustomobject]@{file_count=0;total_bytes=0;quiescence=[pscustomobject]@{
        schema='mineru-output-quiescence.v1';root_identity=[pscustomobject]@{path='/var/lib/mineru-api-output';device=7;inode=8;uid=0;mode=16832};
        registry_sha256=$null;record_count=0;submission_watermark_bucket=$null}}
    $script:PreDeploymentOutputState=Clone $script:QuiescentState
    $script:InstalledFilesRaw=$null
    $script:InstalledFiles=[pscustomobject]@{config_sha256=$script:Profile.config_sha256;byte_count=$Utf8.GetByteCount($script:Profile.config_text);source_sha256=(Clone $Inputs.source_sha256)}
    $script:EpochAfter=('1'*64)+' 2026-01-01T00:00:00Z'
    $pins=@{};foreach($n in @('patch_mineru_344.py','Dockerfile','agent_task_protocol_v2.py')){$pins[$n]='sha256:'+((Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $Context $n)).Hash.ToLowerInvariant())}
    $labels=[ordered]@{
        'io.agent-invest.mineru.base-image-digest'=$ExpectedImageId;'io.agent-invest.mineru.capacity-policy'=$script:CapacityPolicy;
        'io.agent-invest.mineru.compatibility-policy'=$HeapReturnPolicy;
        'io.agent-invest.mineru.compatibility-patcher-sha256'=$pins['patch_mineru_344.py'];
        'io.agent-invest.mineru.compatibility-dockerfile-sha256'=$pins['Dockerfile'];
        'io.agent-invest.mineru.task-protocol-v2-sha256'=$pins['agent_task_protocol_v2.py'];
        'io.agent-invest.mineru.capacity-config-sha256'=$script:Profile.config_sha256;
        'io.agent-invest.mineru.capacity-sources-sha256'=$Inputs.sources_sha256}
    $script:ImageInspect=@([pscustomobject]@{Id=$script:ExpectedApiCompatImageId;Config=[pscustomobject]@{Labels=[pscustomobject]$labels;Env=@('MINERU_MALLOC_TRIM=1','MINERU_PHASE_TRACE=0','MINERU_CAPACITY_CONFIG_PATH=/usr/local/etc/mineru/capacity.json',('MINERU_CAPACITY_CONFIG_SHA256='+$script:Profile.config_sha256))}})
}
Case 'paired explicit selection rejects incomplete and legacy-mixed authority before IO' {
    foreach($variant in @('source-only','sha-only','legacy-task','legacy-pending')){
        $source=$CapacityConfigSource;$pin=$Inputs.profiles[0].config_sha256
        $bound=@{}
        if($variant-eq 'source-only'){$pin=''}elseif($variant-eq 'sha-only'){$source=''}
        elseif($variant-eq 'legacy-task'){$bound.ExpectedApiTaskSlots=1}else{$bound.ExpectedApiMaxPendingTasks=1}
        $bound.CapacityConfigSource=$source;$bound.ExpectedCapacityConfigSha256=$pin
        Reject { & $Selection @bound } 'supplied together|legacy task or pending' $variant
    }
    Check ($script:Calls.Count-eq 0) 'selection must precede Docker/native/network calls'
}
Case 'two canonical configurations project exact bytes source map and twelve independent values' {
    foreach($index in @(0,1)){
        Reset-Profile $index
        Check ($CapacityInputs.config_sha256-ceq $Profile.config_sha256) 'external config SHA retained'
        Check ([Convert]::ToBase64String($CapacityInputs.config_bytes)-ceq [Convert]::ToBase64String($Utf8.GetBytes($Profile.config_text))) 'original canonical bytes retained'
        Check ($CapacityInputs.environment.Count-eq 12) 'twelve exact capacity ENV projections'
        foreach($property in $Profile.environment.PSObject.Properties){Check ($CapacityInputs.environment[$property.Name]-ceq $property.Value) ('ENV '+$property.Name)}
        Check ($CapacityInputs.source_sha256.Count-eq 4) 'four distinct source pins'
        foreach($property in $Inputs.source_sha256.PSObject.Properties){Check ($CapacityInputs.source_sha256[$property.Name]-ceq $property.Value) ('source '+$property.Name)}
        Check ($CapacityInputs.sources_sha256-ceq $Inputs.sources_sha256) 'canonical four-source domain'
        Check ($CapacityInputs.build_target-ceq 'explicit-capacity') 'explicit target'
        Check ([IO.Path]::GetFullPath($CapacityInputs.context)-ceq [IO.Path]::GetFullPath($Context)) 'same build context'
    }
    Check ($script:Calls.Count-eq 0) 'input validation is read-only local IO'
}
Case 'config missing malformed noncanonical oversized and source drift cannot start a build' {
    Reset-Profile
    $original=$Profile.config_text
    foreach($value in @(($original+"`n"),($original.Replace('"parse_active_limit":2','"parse_active_limit":2.0')),
            ($original.Replace('"api_process_limit":1','"api_process_limit":1,"api_process_limit":1')),'{','')){
        [IO.File]::WriteAllText($CapacityConfigSource,$value,$Utf8);$script:ExpectedCapacityConfigSha256='sha256:'+((Get-FileHash -LiteralPath $CapacityConfigSource -Algorithm SHA256).Hash.ToLowerInvariant())
        Reject {Get-ExplicitCapacityInputs|Out-Null} '.'
    }
    [IO.File]::WriteAllBytes($CapacityConfigSource,(New-Object byte[] 65537));Reject {Get-ExplicitCapacityInputs|Out-Null} 'byte bound'
    Reset-Profile;$script:ExpectedCapacityConfigSha256='sha256:'+('0'*64);Reject {Get-ExplicitCapacityInputs|Out-Null} 'SHA differs'
    Reset-Profile;$saved=$CapacityConfigSource;$CapacityConfigSource=Join-Path $TestOutput 'outside.json'
    try{Reject {Get-ExplicitCapacityInputs|Out-Null} 'build context'}finally{$CapacityConfigSource=$saved}
    Reset-Profile;$path=Join-Path $Context 'agent_capacity_observation.py';$bytes=[IO.File]::ReadAllBytes($path)
    try{
        [IO.File]::WriteAllText($path,'different-source',$Utf8)
        Reject {Get-ApiCompatBuildIdentity|Out-Null} 'source bytes changed'
        Remove-Item -LiteralPath $path;Reject {Get-ExplicitCapacityInputs|Out-Null} 'source missing'
    }finally{[IO.File]::WriteAllBytes($path,$bytes)}
    Check ($script:Calls.Count-eq 0) 'all invalid local inputs rejected before build'
}
Case 'actual build path selects explicit target and all independently expected build arguments' {
    Reset-Profile 1
    $result=Build-ValidatedApiCompatImage
    Check ($result.image_id-ceq $Profile.raw.api.image_id) 'actual image validator returned required ID'
    $builds=@($script:Calls|Where-Object{$_[0]-ceq 'build'});Check ($builds.Count-eq 1) 'exactly one mocked build'
    $expected=@('build','--pull=false','--provenance=false','--target','explicit-capacity','--file',$CompatDockerfileSource,
        '--tag',$ApiCompatBuildTag)
    foreach($pair in @(@('COMPAT_PATCHER_SHA256','patch_mineru_344.py'),@('COMPAT_DOCKERFILE_SHA256','Dockerfile'),@('TASK_PROTOCOL_V2_SHA256','agent_task_protocol_v2.py'))){
        $hash='sha256:'+((Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $Context $pair[1])).Hash.ToLowerInvariant())
        $expected+=@('--build-arg',($pair[0]+'='+$hash))
    }
    foreach($name in @('config','file','bootstrap','observation')){$expected+=@('--build-arg',('CAPACITY_'+$name.ToUpperInvariant()+'_SOURCE_SHA256='+$Inputs.source_sha256.('mineru/cli/agent_capacity_'+$name+'.py')))}
    $expected+=@('--build-arg',('CAPACITY_SOURCES_SHA256='+$Inputs.sources_sha256),'--build-arg',('CAPACITY_CONFIG_SHA256='+$Profile.config_sha256),$Context)
    Check (($builds[0]-join '|')-ceq ($expected-join '|')) 'complete real build argv matches independent expected argv'
    Check (@($script:Calls|Where-Object{($_-join '|')-ceq ('image|inspect|'+$ApiCompatBuildTag)}).Count-eq 1) 'built image independently inspected'
}
Case 'image and published reuse reject stale config source labels duplicate anchors or wrong content ID' {
    foreach($variant in @('positive','config','sources','anchor','duplicate','wrong-id','missing')){
        Reset-Profile;$identity=Get-ApiCompatBuildIdentity
        if($variant-eq 'config'){$script:ImageInspect[0].Config.Labels.'io.agent-invest.mineru.capacity-config-sha256'='sha256:'+('0'*64)}
        elseif($variant-eq 'sources'){$script:ImageInspect[0].Config.Labels.'io.agent-invest.mineru.capacity-sources-sha256'='sha256:'+('0'*64)}
        elseif($variant-eq 'anchor'){$script:ImageInspect[0].Config.Env[3]='MINERU_CAPACITY_CONFIG_SHA256=sha256:'+('0'*64)}
        elseif($variant-eq 'duplicate'){$script:ImageInspect[0].Config.Env+=,$script:ImageInspect[0].Config.Env[3]}
        elseif($variant-eq 'wrong-id'){$script:OptionalImage='sha256:'+('0'*64)}
        elseif($variant-eq 'missing'){$script:ImageInspect[0].Config.Labels.PSObject.Properties.Remove('io.agent-invest.mineru.capacity-config-sha256')}
        if($variant-eq 'positive'){Get-ValidatedApiCompatImage $ApiCompatImage $ExpectedApiCompatImageId $identity|Out-Null;Get-ValidatedPublishedApiCompatImage|Out-Null}
        else{Reject {Get-ValidatedPublishedApiCompatImage|Out-Null} '.'}
    }
    Check (@($script:Calls|Where-Object{$_[0]-in @('tag','build','stop')}).Count-eq 0) 'image qualification never mutates image or service'
}
Case 'API-only compose allows capacity change and rejects unrelated service network mount or command change' {
    Reset-Profile 1;Assert-ApiOnlyUpgradeInputs
    foreach($variant in @('proxy','inference','network','mount','environment','command','anchor')){
        Reset-Profile 1
        if($variant-eq 'proxy'){$script:NextCompose.services.'mineru-api-proxy'.restart='no'}
        elseif($variant-eq 'inference'){$script:NextCompose.services.'mineru-openai-server'.command[5]='127'}
        elseif($variant-eq 'network'){$script:NextCompose.services.'mineru-api'.networks+=,'runtime'}
        elseif($variant-eq 'mount'){$script:NextCompose.services.'mineru-api'.volumes[0].source='C:/different'}
        elseif($variant-eq 'environment'){$script:NextCompose.services.'mineru-api'.environment|Add-Member -NotePropertyName 'UNDECLARED' -NotePropertyValue '1'}
        elseif($variant-eq 'command'){$script:NextCompose.services.'mineru-api'.command+=,'--different'}
        else{$script:NextCompose.services.'mineru-api'.environment|Add-Member -NotePropertyName 'MINERU_CAPACITY_CONFIG_SHA256' -NotePropertyValue $Profile.config_sha256}
        if($variant-eq 'inference'){
            Check ($script:NextCompose.services.'mineru-openai-server'.command[5]-ceq '127') 'independent changed command fixture present'
            Check ($script:PreviousCompose.services.'mineru-openai-server'.command[5]-ceq '128') 'independent original command fixture preserved'
            Check ((Get-CanonicalObjectJson $script:NextCompose.services.'mineru-openai-server'.command)-cne (Get-CanonicalObjectJson $script:PreviousCompose.services.'mineru-openai-server'.command)) 'actual canonical comparator must distinguish literal 127 versus 128 command arrays'
        }
        Reject {Assert-ApiOnlyUpgradeInputs} '.' $variant
    }
    Check (@($script:Calls|Where-Object{$_[0]-ceq 'compose' -and $_[-1]-cne 'json'}).Count-eq 0) 'compose comparisons only read normalized configuration'
}
Case 'full health accepts target and prior idle v3 with lazy unknown but rejects debt drain and scalar drift' {
    Reset-Profile
    Assert-IdleHealth $Health 'next' -ExpectedCapacity $Profile.config -RequireAdmissionV2
    Assert-IdleHealth $Inputs.profiles[1].raw.api_health 'previous'
    Reject {Assert-IdleHealth $Inputs.profiles[1].raw.api_health 'wrong-next' -ExpectedCapacity $Profile.config} 'differs'
    $lazy=Clone $Health;$lazy.capacity_observation.http_limiter_state='not_initialized';$lazy.capacity_observation.resolved_limits.final_http_limit_per_loop=$null
    $lazy.capacity_observation.framework_limits.torch_intraop_threads=[pscustomobject]@{state='unavailable';value=$null;reason='serving_getter_not_loaded'}
    Assert-IdleHealth $lazy 'lazy' -ExpectedCapacity $Profile.config
    foreach($variant in @('missing','ingress','stage','http','requested','applied','status-array','runtime-array','sha-lf','float-counter')){
        $value=Clone $Health
        if($variant-eq 'missing'){$value.PSObject.Properties.Remove('capacity_observation')}
        elseif($variant-eq 'ingress'){$value.queued_tasks=1;$value.task_admission.ingress_tasks=1;$value.task_admission.durable_nonterminal_tasks=1}
        elseif($variant-eq 'stage'){$value.capacity_observation.stage_counters.parse_active=1}
        elseif($variant-eq 'http'){$value.capacity_observation.http_counters.pending_requests=1}
        elseif($variant-in @('requested','applied')){$value.capacity_observation.owner_control.foreign_loop_observed=$true;$value.capacity_observation.owner_control.soft_drain_requested=$true;$value.capacity_observation.owner_control.trigger='foreign_event_loop';if($variant-eq 'applied'){$value.capacity_observation.owner_control.soft_drain_applied=$true;$value.task_admission.admission_open=$false;$value.task_admission.blocked_reason='shutting_down'}}
        elseif($variant-eq 'status-array'){$value.status=@('healthy')}
        elseif($variant-eq 'runtime-array'){$value.task_protocol_runtime.schema=@('mineru-task-runtime.v3')}
        elseif($variant-eq 'sha-lf'){$value.task_protocol_runtime.capacity_config_sha256+="`n";$value.capacity_observation.capacity_config_sha256=$value.task_protocol_runtime.capacity_config_sha256}
        else{$value.completed_tasks=[double]0}
        # No JSON roundtrip after assigning invalid types. The old-v3 idle branch
        # must itself enforce scalar/schema closure, without a next-config oracle.
        Reject {Assert-IdleHealth $value ('malformed-'+$variant)} '.' $variant
    }
}
Case 'actual runtime validation binds installed files ENV command and complete serving health' {
    Reset-Profile 1
    $actual=Get-ValidatedRuntime
    Check ((Get-CanonicalObjectJson $actual.api_health)-ceq (Get-CanonicalObjectJson $Health)) 'complete actual 17-field health retained'
    foreach($variant in @('env','command','source','config','count')){
        Reset-Profile 1
        if($variant-eq 'env'){$script:Inspect[0].Config.Env=@($script:Inspect[0].Config.Env|ForEach-Object{if($_-clike 'MINERU_API_FINALIZER_SLOTS=*'){'MINERU_API_FINALIZER_SLOTS=1'}else{$_}})}
        elseif($variant-eq 'command'){$script:Inspect[0].Config.Cmd[-1]='7'}
        elseif($variant-eq 'source'){$script:InstalledFiles.source_sha256.'mineru/cli/agent_capacity_file.py'='sha256:'+('0'*64)}
        elseif($variant-eq 'config'){$script:InstalledFiles.config_sha256='sha256:'+('0'*64)}
        else{
            # Preserve the invalid scalar on the actual JSON wire. ConvertTo-Json
            # would normalize an integral Double back to integer spelling.
            $script:InstalledFilesRaw=($script:InstalledFiles|ConvertTo-Json -Depth 20 -Compress) -replace '("byte_count":)([0-9]+)', '${1}${2}.0'
            Check ($script:InstalledFilesRaw-match '"byte_count":[0-9]+\.0') 'literal floating byte count must reach parser'
        }
        Reject {Get-ValidatedRuntime|Out-Null} '.' $variant
    }
}
Case 'legacy build target and byte-identical compose remain explicit with no capacity adoption' {
    Reset-Profile;$ExplicitCapacity=$false;$CapacityInputs=$null;$CapacityPolicy='single-owner-serial-mineru.v1'
    Check ($null-eq (Get-ExplicitCapacityInputs)) 'legacy does not load config'
    $identity=Get-ApiCompatBuildIdentity;Check ($identity.build_target-ceq 'legacy-runtime') 'legacy stops before optional Docker stages'
    Check (-not $identity.Contains('capacity')) 'legacy build identity has no fabricated capacity'
    Assert-ApiOnlyUpgradeInputs
    [IO.File]::WriteAllText($ComposeSource,'different',$Utf8)
    try{Reject {Assert-ApiOnlyUpgradeInputs} 'unchanged compose'}finally{[IO.File]::WriteAllText($ComposeSource,'untouched-fixture',$Utf8)}
    $script:ImageInspect[0].Config.Labels.'io.agent-invest.mineru.capacity-policy'=$CapacityPolicy
    $script:ImageInspect[0].Config.Env=@('MINERU_MALLOC_TRIM=1','MINERU_PHASE_TRACE=0')
    Build-ValidatedApiCompatImage|Out-Null
    $buildArguments=@($script:Calls|Where-Object{$_[0]-ceq 'build'})[-1]
    Check ($buildArguments[4]-ceq 'legacy-runtime') 'legacy actual build argv target'
    Check (@($buildArguments|Where-Object{$_-clike 'CAPACITY_*'}).Count-eq 0) 'legacy build sends no capacity args'
}
Case 'rollback fresh witness remains first and refuses all mutations when changed or unreadable' {
    foreach($variant in @('changed','unreadable')){
        Reset-Profile;$script:Calls.Clear()
        if($variant-eq 'changed'){$script:QuiescentState.quiescence.root_identity.inode=9}else{$script:QuiescentError='independent unreadable registry'}
        $before=@{};foreach($path in @($ComposeTarget,$CollectorTarget,$ReceiptTarget)){$before[$path]=[IO.File]::ReadAllText($path)}
        Reject {Restore-PreviousDeployment} 'rollback_blocked_registry_changed|rollback_blocked_registry_unverified'
        Check ($script:Calls.Count-eq 1 -and ($script:Calls[0]-join '|')-ceq 'quiescent|True') 'only fresh candidate-source read before refusal'
        foreach($path in $before.Keys){Check ([IO.File]::ReadAllText($path)-ceq $before[$path]) 'rollback refusal preserves target bytes'}
    }
}
Case 'collector real ENV projection excludes anchors and refuses duplicate override or undeclared values' {
    foreach($variant in @('positive','duplicate','actual-sha','image-sha','compose-anchor','extra')){
        Reset-Profile 1
        & {
            # The collector has no StrictMode. Match its actual statement context.
            Set-StrictMode -Off
            $api=Clone $script:Inspect[0];$proxy=Clone $script:Inspect[1];$vllm=Clone $script:Inspect[2]
            $configObject=Clone $script:NextCompose
            $apiImageEnvironment=@($script:ImageInspect[0].Config.Env);$baseImageEnvironment=@('PATH=/usr/bin');$vllm.Config.Env+=,'PATH=/usr/bin';$proxy.Config.Env+=,'PATH=/usr/bin'
            if($variant-eq 'duplicate'){$api.Config.Env+=,$api.Config.Env[-1]}
            elseif($variant-eq 'actual-sha'){$api.Config.Env=@($api.Config.Env|ForEach-Object{if($_-clike 'MINERU_CAPACITY_CONFIG_SHA256=*'){'MINERU_CAPACITY_CONFIG_SHA256=sha256:'+('0'*64)}else{$_}})}
            elseif($variant-eq 'image-sha'){$apiImageEnvironment[3]='MINERU_CAPACITY_CONFIG_SHA256=sha256:'+('0'*64)}
            elseif($variant-eq 'compose-anchor'){$configObject.services.'mineru-api'.environment|Add-Member -NotePropertyName MINERU_CAPACITY_CONFIG_PATH -NotePropertyValue '/usr/local/etc/mineru/capacity.json'}
            elseif($variant-eq 'extra'){$api.Config.Env+=,'UNDECLARED=1'}
            if($variant-eq 'positive'){
                . $CollectorEnvironment
                Check ((Get-CanonicalObjectJson $apiEnvironment)-ceq (Get-CanonicalObjectJson $Profile.raw.api.environment)) 'twenty selected API ENV values retained exactly'
                Check (-not $apiEnvironment.Contains('MINERU_CAPACITY_CONFIG_PATH')) 'image anchors are not invented compose ENV'
                Check ((Get-CanonicalObjectJson $vllmEnvironment)-ceq (Get-CanonicalObjectJson $Profile.raw.inference.environment)) 'inference ENV unchanged'
            }else{Reject {. $CollectorEnvironment} '.'}
        }
    }
    Check ($script:Calls.Count-eq 0) 'ENV projection has no external operations'
}
Case 'collector uses one original complete serving sample and rejects proxy static identity drift' {
    Reset-Profile
    & {
        Set-StrictMode -Off
        $compatProbe=[pscustomobject]@{serving_health=(Clone $Profile.raw.api_health)}
        $script:ProxyHealth=Clone $Health
        $script:ProxyHealth.completed_tasks=27
        $script:ProxyHealth.capacity_observation.observed_at.completed_ns=190
        . $CollectorHealth
        Check ([object]::ReferenceEquals($health,$compatProbe.serving_health)) 'all volatile health remains the original sample object'
        Check ((Get-CanonicalObjectJson $health)-ceq (Get-CanonicalObjectJson $Profile.raw.api_health)) 'complete seventeen fields retained'
        Check ($health.completed_tasks-ne 27) 'proxy second sample not mixed into original observation'
        $script:ProxyHealth.task_protocol_runtime.capacity_config_sha256='sha256:'+('0'*64)
        Reject {. $CollectorHealth} 'proxy health static capacity identity drifted'
    }
    Check ($script:Calls.Count-eq 2) 'one bounded proxy read per real health statement invocation'
    foreach($call in $script:Calls){Check (($call-join '|')-ceq 'http|http://127.0.0.1:30003/health|15') 'only actual fixed proxy health GET'}
}
Case 'collector final v6 preserves four source pins full health and original epoch boundary' {
    Reset-Profile 1
    & {
        Set-StrictMode -Off
        $api=$script:Inspect[0];$proxy=$script:Inspect[1];$vllm=$script:Inspect[2]
        $configObject=$script:NextCompose;$ComposePath=$ComposeSource
        $nodeIdentity='sha256:'+('9'*64)
        $inferenceNetwork=@([pscustomobject]@{Name='mineru-tailnet_inference';Driver='bridge';Internal=$true})
        $runtimeNetwork=@([pscustomobject]@{Name='mineru-tailnet_runtime';Driver='bridge';Internal=$false})
        $apiEnvironment=$Profile.raw.api.environment;$proxyEnvironment=$Profile.raw.proxy.environment;$vllmEnvironment=$Profile.raw.inference.environment
        $apiMounts=$Profile.raw.api.mounts;$proxyMounts=$Profile.raw.proxy.mounts;$vllmMounts=$Profile.raw.inference.mounts
        $apiNetworks=$Profile.raw.api.networks;$proxyNetworks=$Profile.raw.proxy.networks;$vllmNetworks=$Profile.raw.inference.networks
        $apiPort=@();$proxyPort=@($Profile.raw.proxy.port);$vllmPort=@($Profile.raw.inference.port)
        $externalTcpEgressBlocked=$true;$compatProbe=Clone $Profile.raw.api_compatibility
        $compatLabels=$compatProbe.image_labels;$health=Clone $Profile.raw.api_health
        $models=$Profile.models;$modelId=$Profile.raw.served_model.id;$revision=$Profile.raw.served_model.revision;$vllmVersion=$Profile.raw.served_model.vllm_version
        $outputState=$script:QuiescentState
        . $CollectorEpoch
        . $CollectorResult -PSCommandPath $CollectorPath
        Check ($result.schema-ceq 'mineru-windows-runtime-observation.v6') 'one v6 schema across both independent configs'
        Check ($result.api_health.PSObject.Properties.Name.Count-eq 17) 'no normalization drops full health fields'
        Check ([object]::ReferenceEquals($result.api_health,$health)) 'final projection preserves original health object'
        Check (-not $result.api_compatibility.Contains('capacity_runtime')) 'explicit branch has no fabricated legacy capacity runtime'
        Check ((Get-CanonicalObjectJson $result.api_compatibility.capacity_sources_actual_sha256)-ceq (Get-CanonicalObjectJson $Inputs.source_sha256)) 'four installed source hashes retained'
        Check ((Get-CanonicalObjectJson $result.api_compatibility.capacity_config_file)-ceq (Get-CanonicalObjectJson $Profile.raw.api_compatibility.capacity_config_file)) 'raw config file bytes count and SHA retained'
        $script:EpochAfter=('1'*64)+' 2026-01-01T00:00:01Z'
        Reject {. $CollectorEpoch} 'epoch changed'
    }
    Check ($script:Calls.Count-eq 2) 'only actual final epoch reads'
}
Case 'formal collector tail accepts exact explicit v6 and legacy v5 and rejects cross-version or scalar drift' {
    foreach($explicit in @($true,$false)){
        $ExplicitCapacity=$explicit
        $expected=if($explicit){'mineru-windows-runtime-observation.v6'}else{'mineru-windows-runtime-observation.v5'}
        $collectorOutput=@('{"schema":"'+$expected+'"}')
        . $CollectorTail
        Check ($collectorObservation.schema-ceq $expected) 'actual tail keeps version for selected branch'
        $other=if($explicit){'mineru-windows-runtime-observation.v5'}else{'mineru-windows-runtime-observation.v6'}
        foreach($raw in @(('{"schema":"'+$other+'"}'),('{"schema":["'+$expected+'"]}'),'{"schema":null}')){
            $collectorOutput=@($raw);Reject {. $CollectorTail} 'contract drifted' ('explicit='+$explicit)
        }
        $collectorOutput=@();Reject {. $CollectorTail} 'one observation'
        $collectorOutput=@(('{"schema":"'+$expected+'"}'),('{"schema":"'+$expected+'"}'));Check ($collectorOutput.Count-eq 2) 'two literal collector outputs retained';Reject {. $CollectorTail} 'one observation'
    }
    Check ($script:Calls.Count-eq 0) 'formal collector schema guard does not invoke collector or deployment mutations'
}
$receipt=[ordered]@{
    schema='independent-explicit-capacity-deployment-tests.v1'
    powershell_version=$PSVersionTable.PSVersion.ToString()
    process_id=$PID
    installer_sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $InstallerPath).Hash.ToLowerInvariant()
    collector_sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $CollectorPath).Hash.ToLowerInvariant()
    script_sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $PSCommandPath).Hash.ToLowerInvariant()
    inputs_sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $InputPath).Hash.ToLowerInvariant()
    source_sha256=$Inputs.source_sha256
    installer_top_level_executed=$false;collector_top_level_executed=$false
    real_docker_network_service_operations=$false
    cases=@($script:Results.ToArray())
    passed=@($script:Results|Where-Object{$_.status-ceq 'pass'}).Count
    failed=@($script:Results|Where-Object{$_.status-ceq 'fail'}).Count
}
[IO.File]::WriteAllText((Join-Path $TestOutput 'receipt.json'),($receipt|ConvertTo-Json -Depth 100),$Utf8)
Write-Host ('RESULT '+$receipt.passed+' pass '+$receipt.failed+' fail')
if($receipt.failed){exit 1}
