<#
.SYNOPSIS
Independent GPU device authorization and rollback tests for the real installer.
.DESCRIPTION
Only allowlisted function ASTs from InstallerPath are evaluated. Installer top-level,
native processes, HTTP, GPU access and Docker are never executed. All rollback file
fixtures are under one new disposable directory. Requires Windows PowerShell 5.1.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$InstallerPath,
    [Parameter(Mandatory=$true)][string]$OutputRoot,
    [string]$ActualComposePairPath = ''
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
if($PSVersionTable.PSVersion.Major -ne 5){throw 'Windows PowerShell 5.1 required'}
# Resolve defaults after Windows PowerShell has initialized script identity.
if([string]::IsNullOrEmpty($ActualComposePairPath)){
    if([string]::IsNullOrEmpty($PSCommandPath) -or -not [IO.Path]::IsPathRooted($PSCommandPath)){throw 'cannot resolve default fixture without an absolute script path'}
    $TestScriptRoot=Split-Path -Parent $PSCommandPath
    $ActualComposePairPath=[IO.Path]::GetFullPath((Join-Path $TestScriptRoot '..\..\tests\fixtures\mineru_device_profile\compose-pair.json'))
}
if(-not (Test-Path -LiteralPath $ActualComposePairPath -PathType Leaf)){throw 'compose pair fixture must be an existing file'}
$OutputRoot=[IO.Path]::GetFullPath($OutputRoot)
foreach($protected in @('C:\ProgramData','C:\Program Files',[Environment]::SystemDirectory)){
    if($OutputRoot.StartsWith($protected,[StringComparison]::OrdinalIgnoreCase)){throw 'requires disposable test output'}
}
if(Test-Path -LiteralPath $OutputRoot){throw 'output root must be new'}
[void](New-Item -ItemType Directory -Path $OutputRoot)
$Utf8=New-Object Text.UTF8Encoding($false,$true)
$ActualComposePairSha256=(Get-FileHash -LiteralPath $ActualComposePairPath -Algorithm SHA256).Hash.ToLowerInvariant()
if($ActualComposePairSha256-cne '35bc7c11e2a4b19cc0c22ad71f7297a1f90affa604360bff18651c4e4f667994'){throw 'independent deterministic compose pair identity differs'}
$ActualComposePair=[IO.File]::ReadAllText($ActualComposePairPath,$Utf8)|ConvertFrom-Json
$tokens=$null;$parseErrors=$null
$Ast=[Management.Automation.Language.Parser]::ParseFile([IO.Path]::GetFullPath($InstallerPath),[ref]$tokens,[ref]$parseErrors)
if(@($parseErrors).Count){throw ($parseErrors|Out-String)}
$Allow=@('Get-CanonicalObjectJson','Assert-RequiredProperties','Assert-ClosedProperties',
    'Get-ApiDeviceProfile','Assert-ApiDeviceTransition','Assert-ApiDeviceRuntime',
    'Assert-ApiOnlyUpgradeInputs','Get-ResolvedCompose','Assert-CapacityCompose',
    'Invoke-ApiOnlyRecreate','Get-StableServiceEpochs','Assert-StableServiceEpochs',
    'Restore-PreviousDeployment','Get-RollbackRegistryWitness','Assert-RollbackRegistryUnchanged',
    'Restore-ApiCompatTag','Remove-CompatBuildTag','Get-OptionalImageId','ConvertFrom-NativeProcessText')
foreach($name in $Allow){
    $nodes=@($Ast.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$true))
    if($nodes.Count-ne 1){throw "expected one actual installer function $name"}
    . ([ScriptBlock]::Create($nodes[0].Extent.Text))
}
$deviceParameter=@($Ast.ParamBlock.Parameters|Where-Object{$_.Name.VariablePath.UserPath-ceq 'ApiDeviceProfile'})
$deviceGuard=@($Ast.EndBlock.Statements|Where-Object{
    $_ -is [Management.Automation.Language.IfStatementAst] -and
    $_.Clauses[0].Item1.Extent.Text.Contains('$ApiDeviceProfile')
})
if($deviceParameter.Count-ne 1 -or $deviceGuard.Count-ne 1){throw 'expected one real device parameter and top-level authorization guard'}
$Selection=[ScriptBlock]::Create('[CmdletBinding()]param('+ $deviceParameter[0].Extent.Text + ',[bool]$ApiOnlyCompatibilityUpgrade,[bool]$ExplicitCapacity)' + "`n" + $deviceGuard[0].Extent.Text)
$script:Results=New-Object Collections.ArrayList
$script:Calls=New-Object Collections.ArrayList
function Check {param([bool]$Condition,[string]$Message)if(-not $Condition){throw $Message}}
function Reject {param([scriptblock]$Action,[string]$Pattern='.',[string]$Context='')
    $caught='';try{& $Action}catch{$caught=$_|Out-String}
    Check ($caught-match $Pattern) "expected refusal ($Context), pattern $Pattern, got: $caught"
}
function Clone {param($Value)return (($Value|ConvertTo-Json -Depth 80 -Compress)|ConvertFrom-Json)}
function Case {param([string]$Name,[scriptblock]$Action)
    $script:Calls.Clear()
    try{& $Action;[void]$script:Results.Add([ordered]@{name=$Name;status='pass';calls=@($script:Calls.ToArray())});Write-Host "PASS $Name"}
    catch{[void]$script:Results.Add([ordered]@{name=$Name;status='fail';error=($_|Out-String);calls=@($script:Calls.ToArray())});Write-Host "FAIL $Name : $_"}
}
function Cpu-Compose {
    return (@'
{"name":"fixture","services":{"mineru-api":{"image":"fixture/api:exact","command":["--host","0.0.0.0","--port","8000","--allow-public-http-client","--max-concurrency","7"],"environment":{"MINERU_API_MAX_CONCURRENT_REQUESTS":"5","MINERU_API_MAX_PENDING_TASKS":"6","MINERU_API_FINALIZER_SLOTS":"1","MINERU_PROCESSING_WINDOW_SIZE":"16","OMP_NUM_THREADS":"4","MKL_NUM_THREADS":"4","OPENBLAS_NUM_THREADS":"1","MINERU_PHASE_TRACE":"1","MINERU_HYBRID_BATCH_RATIO":"1","MINERU_PIPELINE_INFERENCE_LOCKS":"1"},"volumes":[{"type":"bind","source":"fixture/models","target":"/models","read_only":true}],"networks":{"inference":{}},"mem_limit":"15g","restart":"always"},"mineru-openai-server":{"image":"fixture/engine:exact","command":["mineru-openai-server","--max-num-seqs","128"],"environment":{"VLLM_GPU_MEMORY_UTILIZATION":"0.5"},"deploy":{"resources":{"reservations":{"devices":[{"driver":"nvidia","device_ids":["0"],"capabilities":["gpu"]}]}}}},"mineru-api-proxy":{"image":"fixture/proxy:exact","read_only":true,"cap_drop":["ALL"],"ports":[{"target":8000,"published":"30003"}] }},"networks":{"inference":{"internal":true}}}
'@ | ConvertFrom-Json)
}
function Cuda-Compose {
    $value=Cpu-Compose
    $api=$value.services.'mineru-api'
    $api.environment|Add-Member -NotePropertyName MINERU_DEVICE_MODE -NotePropertyValue 'cuda:0'
    $api|Add-Member -NotePropertyName deploy -NotePropertyValue ([pscustomobject]@{resources=[pscustomobject]@{reservations=[pscustomobject]@{
        devices=@([pscustomobject]@{driver='nvidia';device_ids=@('0');capabilities=@('gpu')})}}})
    return $value
}
function Cpu-Container {
    return (@'
{"Name":"/mineru-api","Id":"1111111111111111111111111111111111111111111111111111111111111111","Image":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","Config":{"Env":["PATH=/usr/bin","MINERU_PHASE_TRACE=1"]},"HostConfig":{"DeviceRequests":[],"Devices":[],"Privileged":false,"CapAdd":null},"State":{"Running":true,"Health":{"Status":"healthy"}}}
'@ | ConvertFrom-Json)
}
function Cuda-Container {
    $value=Cpu-Container
    $value.Config.Env+=,'MINERU_DEVICE_MODE=cuda:0'
    $value.HostConfig.DeviceRequests=@(('{"Driver":"nvidia","Count":0,"DeviceIDs":["0"],"Capabilities":[["gpu"]],"Options":{}}'|ConvertFrom-Json))
    return $value
}
function Invoke-NativeProcess {throw 'SAFETY native execution forbidden'}
function Invoke-RestMethod {throw 'SAFETY HTTP execution forbidden'}
function Invoke-WebRequest {throw 'SAFETY HTTP execution forbidden'}
function Invoke-DockerProcess {throw 'SAFETY native DockerProcess execution forbidden'}
function Invoke-Docker {param([string[]]$Arguments)
    [void]$script:Calls.Add(@($Arguments))
    if($Arguments.Count-eq 8 -and ($Arguments[0..3]-join '|')-ceq "compose|--project-name|$ProjectName|--file" -and
       ($Arguments[5..7]-join '|')-ceq 'config|--format|json'){
        if($Arguments[4]-ceq $ComposeSource){return ($script:NextCompose|ConvertTo-Json -Depth 80 -Compress)}
        if($Arguments[4]-ceq $ComposeTarget){return ($script:PreviousCompose|ConvertTo-Json -Depth 80 -Compress)}
    }
    $recreate=@('compose','--project-name',$ProjectName,'--file',$ComposeTarget,'up','--detach','--no-build','--no-deps','--force-recreate','mineru-api')
    if(($Arguments-join '|')-ceq ($recreate-join '|')){
        Check ([IO.File]::ReadAllText($ComposeTarget)-ceq $script:OriginalComposeBytes) 'original compose must be restored before recreation'
        $script:RunningApi=if($script:WrongRollbackDevice){Cuda-Container}else{Cpu-Container}
        $script:RunningApi.Image=$OldApiCompatImageId
        return ''
    }
    if(($Arguments-join '|')-ceq "tag|$OldApiCompatImageId|$ApiCompatImage"){return ''}
    if(($Arguments-join '|')-ceq 'inspect|mineru-api'){return (@($script:RunningApi)|ConvertTo-Json -Depth 30 -Compress)}
    if(($Arguments-join '|')-ceq 'inspect|mineru-api-proxy|mineru-openai-server'){return ($script:StableInspect|ConvertTo-Json -Depth 20 -Compress)}
    throw ('SAFETY unexpected Docker operation '+($Arguments-join '|'))
}
function Wait-Healthy { [void]$script:Calls.Add(@('wait-healthy'));return 'healthy' }
function Get-QuiescentOutputState {param([switch]$CandidateSource)
    Check ([bool]$CandidateSource) 'rollback must use original candidate-source witness'
    [void]$script:Calls.Add(@('registry-witness'))
    if($script:ChangedRegistry){$value=Clone $PreDeploymentOutputState;$value.quiescence.root_identity.inode=99;return $value}
    return $PreDeploymentOutputState
}
$ProjectName='independent-no-real-project'
$ComposeSource=Join-Path $OutputRoot 'next.yml';$ComposeTarget=Join-Path $OutputRoot 'active.yml'
$CollectorTarget=Join-Path $OutputRoot 'collector.ps1';$ReceiptTarget=Join-Path $OutputRoot 'receipt.json'
$ComposeBackup=Join-Path $OutputRoot 'old-compose.bak';$CollectorBackup=Join-Path $OutputRoot 'old-collector.bak';$ReceiptBackup=Join-Path $OutputRoot 'old-receipt.bak'
function Reset {
    $script:NextCompose=Cuda-Compose;$script:PreviousCompose=Cpu-Compose
    $script:ComposeExisted=$true;$script:CollectorExisted=$true;$script:ReceiptExisted=$true
    $script:ExplicitCapacity=$true;$script:ApiDeviceProfile='cuda0';$script:PreviousApiDeviceProfile='cpu'
    $script:ApiOnlyCompatibilityUpgrade=$true;$script:ReuseCurrentPublishedImage=$false
    $script:CapacityInputs=[pscustomobject]@{environment=@{};config=[pscustomobject]@{final_http_limit_per_loop=7}}
    foreach($property in $script:NextCompose.services.'mineru-api'.environment.PSObject.Properties){
        if($property.Name-notin @('MINERU_DEVICE_MODE','MINERU_PHASE_TRACE')){$script:CapacityInputs.environment[$property.Name]=$property.Value}
    }
    $script:DeploymentAttempted=$true;$script:OldProjectContainers=@('mineru-api','mineru-api-proxy','mineru-openai-server')
    $script:ComposeBackupCreated=$true;$script:CollectorBackupCreated=$true;$script:ReceiptBackupCreated=$true
    $script:OldApiCompatImageId='sha256:'+('a'*64);$script:ExpectedApiCompatImageId='sha256:'+('b'*64)
    $script:ApiCompatImage='fixture/api:exact';$script:ApiCompatBuildTag='fixture/api:unused'
    $script:CompatTagSwitched=$true;$script:CompatBuildTagCreated=$false
    $script:ChangedRegistry=$false;$script:WrongRollbackDevice=$false
    $script:OriginalComposeBytes="original-cpu-profile`r`nexact-snapshot`n"
    [IO.File]::WriteAllText($ComposeBackup,$script:OriginalComposeBytes,$Utf8)
    [IO.File]::WriteAllText($CollectorBackup,'original-collector',$Utf8)
    [IO.File]::WriteAllText($ReceiptBackup,'original-receipt',$Utf8)
    foreach($p in @($ComposeSource,$ComposeTarget,$CollectorTarget,$ReceiptTarget)){[IO.File]::WriteAllText($p,'candidate-cuda-profile',$Utf8)}
    $script:PreDeploymentOutputState=[pscustomobject]@{file_count=0;total_bytes=0;quiescence=[pscustomobject]@{
        schema='mineru-output-quiescence.v1';root_identity=[pscustomobject]@{path='/var/lib/mineru-api-output';device=7;inode=8;uid=0;mode=16832};
        registry_sha256=$null;record_count=0;submission_watermark_bucket=$null}}
    $script:StableInspect=@(
        [pscustomobject]@{Name='/mineru-api-proxy';Id=('c'*64);Image=('sha256:'+('d'*64));RestartCount=0;State=[pscustomobject]@{Running=$true;OOMKilled=$false;StartedAt='2026-01-01T00:00:00Z';Health=[pscustomobject]@{Status='healthy'}}},
        [pscustomobject]@{Name='/mineru-openai-server';Id=('e'*64);Image=('sha256:'+('f'*64));RestartCount=0;State=[pscustomobject]@{Running=$true;OOMKilled=$false;StartedAt='2026-01-02T00:00:00Z';Health=[pscustomobject]@{Status='healthy'}}})
    $script:StableServiceEpochs=Get-StableServiceEpochs
    $script:Calls.Clear()
}
# Match the installer's actual scope. Its optional compose properties are read in
# non-strict mode; enabling StrictMode only in this harness would invent failures.
Set-StrictMode -Off
Case 'actual parameter guard permits only explicit-capacity API-only device selection before any IO' {
    foreach($profile in @('cpu','cuda0')){
        & $Selection -ApiDeviceProfile $profile -ApiOnlyCompatibilityUpgrade $true -ExplicitCapacity $true
        foreach($pair in @(@($false,$true),@($true,$false),@($false,$false))){
            Reject {& $Selection -ApiDeviceProfile $profile -ApiOnlyCompatibilityUpgrade $pair[0] -ExplicitCapacity $pair[1]} 'API device selection requires'
        }
    }
    foreach($profile in @('cuda:0','cuda1','CUDA0','all')){
        if($profile-ceq 'CUDA0'){continue} # ValidateSet is case-insensitive; the actual profile classifier remains closed.
        Reject {& $Selection -ApiDeviceProfile $profile -ApiOnlyCompatibilityUpgrade $true -ExplicitCapacity $true} '.' $profile
    }
    & $Selection -ApiOnlyCompatibilityUpgrade $false -ExplicitCapacity $false
    Check ($script:Calls.Count-eq 0) 'authorization guard cannot reach Docker or file mutation'
}
Case 'only CPU and explicit GPU0 profiles are valid and classification does not mutate input' {
    $cpu=Cpu-Compose;$cuda=Cuda-Compose;$before=Get-CanonicalObjectJson @($cpu,$cuda)
    Check ((Get-ApiDeviceProfile -Api $cpu.services.'mineru-api')-ceq 'cpu') 'absent device override is CPU'
    Check ((Get-ApiDeviceProfile -Api $cuda.services.'mineru-api')-ceq 'cuda0') 'explicit GPU0 CUDA profile'
    Check ((Get-CanonicalObjectJson @($cpu,$cuda))-ceq $before) 'profile classification retains all input fields'
    $cpu.services.'mineru-api'.environment|Add-Member -NotePropertyName MINERU_DEVICE_MODE -NotePropertyValue 'cpu'
    Check ((Get-ApiDeviceProfile -Api $cpu.services.'mineru-api')-ceq 'cpu') 'explicit CPU is also CPU'
    Check ($script:Calls.Count-eq 0) 'pure classification never calls Docker'
}
Case 'CPU to CUDA0 and reverse preserve full configuration without changing input objects' {
    foreach($direction in @('cuda0','cpu')){
        $previous=if($direction-eq 'cuda0'){Cpu-Compose}else{Cuda-Compose}
        $next=if($direction-eq 'cuda0'){Cuda-Compose}else{Cpu-Compose}
        $before=Get-CanonicalObjectJson @($previous,$next)
        Assert-ApiDeviceTransition -Next $next -Previous $previous -Profile $direction
        Check ((Get-CanonicalObjectJson @($previous,$next))-ceq $before) 'transition validation cannot erase capacity or device proof'
    }
    Check ($script:Calls.Count-eq 0) 'transition is pure'
}
Case 'device profile rejects wrong hardware ambiguous authority and broadened device capability' {
    foreach($variant in @('gpu1','all','two-gpus','wrong-driver','compute-extra','cap-scalar','id-scalar','extra-request','options','count','missing-env','env-cpu','env-auto','env-array','env-case')){
        $api=(Cuda-Compose).services.'mineru-api';$request=$api.deploy.resources.reservations.devices[0]
        switch($variant){
            'gpu1'{$request.device_ids=@('1')}
            'all'{$request.device_ids=@('all')}
            'two-gpus'{$request.device_ids=@('0','1')}
            'wrong-driver'{$request.driver='other'}
            'compute-extra'{$request.capabilities=@('gpu','compute')}
            'cap-scalar'{$request.capabilities='gpu'}
            'id-scalar'{$request.device_ids='0'}
            'extra-request'{$api.deploy.resources.reservations.devices+=,(Clone $request)}
            'options'{$request|Add-Member -NotePropertyName options -NotePropertyValue ([pscustomobject]@{unknown='yes'})}
            'count'{$request|Add-Member -NotePropertyName count -NotePropertyValue 1}
            'missing-env'{$api.environment.PSObject.Properties.Remove('MINERU_DEVICE_MODE')}
            'env-cpu'{$api.environment.MINERU_DEVICE_MODE='cpu'}
            'env-auto'{$api.environment.MINERU_DEVICE_MODE='cuda'}
            'env-array'{$api.environment.MINERU_DEVICE_MODE=@('cuda:0')}
            'env-case'{$api.environment.MINERU_DEVICE_MODE='CUDA:0'}
        }
        Reject {Get-ApiDeviceProfile -Api $api|Out-Null} '.' $variant
    }
}
Case 'device transition rejects every adjacent non-device config drift including capacity allowances' {
    foreach($variant in @('N','P','H','W','phase','threads','batch','lock','mount','new-env','api-image','engine','proxy','network','cap-add','privileged','direct-device','shm','deploy-limit','reservation-memory')){
        $previous=Cpu-Compose;$next=Cuda-Compose;$api=$next.services.'mineru-api'
        switch($variant){
            'N'{$api.environment.MINERU_API_MAX_CONCURRENT_REQUESTS='6'}
            'P'{$api.environment.MINERU_API_MAX_PENDING_TASKS='7'}
            'H'{$api.command[-1]='14'}
            'W'{$api.environment.MINERU_PROCESSING_WINDOW_SIZE='8'}
            'phase'{$api.environment.MINERU_PHASE_TRACE='0'}
            'threads'{$api.environment.OMP_NUM_THREADS='8'}
            'batch'{$api.environment.MINERU_HYBRID_BATCH_RATIO='2'}
            'lock'{$api.environment.MINERU_PIPELINE_INFERENCE_LOCKS='0'}
            'mount'{$api.volumes[0].read_only=$false}
            'new-env'{$api.environment|Add-Member -NotePropertyName EXTRA -NotePropertyValue '1'}
            'api-image'{$api.image='fixture/api:other'}
            'engine'{$next.services.'mineru-openai-server'.environment.VLLM_GPU_MEMORY_UTILIZATION='0.4'}
            'proxy'{$next.services.'mineru-api-proxy'.read_only=$false}
            'network'{$next.networks.inference.internal=$false}
            'cap-add'{$api|Add-Member -NotePropertyName cap_add -NotePropertyValue @('SYS_ADMIN')}
            'privileged'{$api|Add-Member -NotePropertyName privileged -NotePropertyValue $true}
            'direct-device'{$api|Add-Member -NotePropertyName devices -NotePropertyValue @('/dev/nvidia0:/dev/nvidia0')}
            'shm'{$api|Add-Member -NotePropertyName shm_size -NotePropertyValue '8g'}
            'deploy-limit'{$api.deploy.resources|Add-Member -NotePropertyName limits -NotePropertyValue ([pscustomobject]@{memory='20g'})}
            'reservation-memory'{$api.deploy.resources.reservations|Add-Member -NotePropertyName memory -NotePropertyValue '1g'}
        }
        Reject {Assert-ApiDeviceTransition -Next $next -Previous $previous -Profile 'cuda0'} '.' $variant
    }
}
Case 'device transition leaves unrelated deploy resources unchanged and refuses pruning their drift' {
    $old=Cpu-Compose;$next=Cuda-Compose
    $old.services.'mineru-api'|Add-Member -NotePropertyName deploy -NotePropertyValue ([pscustomobject]@{resources=[pscustomobject]@{limits=[pscustomobject]@{memory='15g'}}})
    $next.services.'mineru-api'.deploy.resources|Add-Member -NotePropertyName limits -NotePropertyValue ([pscustomobject]@{memory='15g'})
    Assert-ApiDeviceTransition -Next $next -Previous $old -Profile 'cuda0'
    $next.services.'mineru-api'.deploy.resources.limits.memory='16g'
    Reject {Assert-ApiDeviceTransition -Next $next -Previous $old -Profile 'cuda0'} '.' 'preserve nonempty deploy parents'
    Reject {Assert-ApiDeviceTransition -Next (Cuda-Compose) -Previous (Cpu-Compose) -Profile 'cpu'} '.' 'requested profile must bind actual candidate'
}
Case 'deterministic CPU CUDA pair accepts Docker empty placement and keeps both inputs intact' {
    foreach($direction in @('cuda0','cpu')){
        $previous=Clone $(if($direction-eq 'cuda0'){$ActualComposePair.previous}else{$ActualComposePair.next})
        $next=Clone $(if($direction-eq 'cuda0'){$ActualComposePair.next}else{$ActualComposePair.previous})
        $before=Get-CanonicalObjectJson @($previous,$next)
        Assert-ApiDeviceTransition -Next $next -Previous $previous -Profile $direction
        Check ((Get-CanonicalObjectJson @($previous,$next))-ceq $before) 'deterministic pair must remain unmodified'
    }
    Check ((Get-FileHash -LiteralPath $ActualComposePairPath -Algorithm SHA256).Hash.ToLowerInvariant()-ceq $ActualComposePairSha256) 'pinned deterministic compose pair remains exact'
}
Case 'placement normalization never erases a nonempty constraint or nonobject value' {
    foreach($variant in @('constraint','preference','empty-constraint-array','array','string','null','boolean','integer')){
        $previous=Clone $ActualComposePair.previous;$next=Clone $ActualComposePair.next
        $placement=$next.services.'mineru-api'.deploy
        switch($variant){
            'constraint'{$placement.placement=[pscustomobject]@{constraints=@('node.labels.gpu == yes')}}
            'preference'{$placement.placement=[pscustomobject]@{preferences=@([pscustomobject]@{spread='node.labels.zone'})}}
            'empty-constraint-array'{$placement.placement=[pscustomobject]@{constraints=@()}}
            'array'{$placement.placement=@()}
            'string'{$placement.placement=''}
            'null'{$placement.placement=$null}
            'boolean'{$placement.placement=$false}
            'integer'{$placement.placement=0}
        }
        $before=Get-CanonicalObjectJson @($previous,$next)
        Reject {Assert-ApiDeviceTransition -Next $next -Previous $previous -Profile 'cuda0'} '.' $variant
        Check ((Get-CanonicalObjectJson @($previous,$next))-ceq $before) 'refusal must also preserve caller evidence'
    }
}
Case 'unchanged nonempty placement is preserved while a changed constraint rejects' {
    $previous=Clone $ActualComposePair.previous;$next=Clone $ActualComposePair.next
    $policy=[pscustomobject]@{constraints=@('node.labels.gpu == yes');max_replicas_per_node=1}
    $previous.services.'mineru-api'|Add-Member -NotePropertyName deploy -NotePropertyValue ([pscustomobject]@{placement=(Clone $policy)})
    $next.services.'mineru-api'.deploy.placement=Clone $policy
    $before=Get-CanonicalObjectJson @($previous,$next)
    Assert-ApiDeviceTransition -Next $next -Previous $previous -Profile 'cuda0'
    Check ((Get-CanonicalObjectJson @($previous,$next))-ceq $before) 'nonempty original constraints must not be normalized away'
    $next.services.'mineru-api'.deploy.placement.constraints=@('node.labels.gpu == other')
    Reject {Assert-ApiDeviceTransition -Next $next -Previous $previous -Profile 'cuda0'} '.' 'changed placement constraint'
    $next=Clone $ActualComposePair.next
    Reject {Assert-ApiDeviceTransition -Next $next -Previous $previous -Profile 'cuda0'} '.' 'constraint removal disguised as generated empty placement'
}
Case 'actual runtime requires exactly the selected device and rejects malformed inspect scalar fields' {
    Assert-ApiDeviceRuntime -Container (Cpu-Container) -Profile 'cpu'
    Assert-ApiDeviceRuntime -Container (Cuda-Container) -Profile 'cuda0'
    foreach($variant in @('none','gpu1','all','count-minus1','count-one','count-string','count-bool','count-float','count-array','ids-scalar','caps-flat','caps-extra','caps-or','options','driver','extra-key','extra-request','device-cpu','duplicate-env','wrong-case-env','privileged','direct-device','cap-add')){
        $api=Cuda-Container;$request=$api.HostConfig.DeviceRequests[0]
        switch($variant){
            'none'{$api.HostConfig.DeviceRequests=@()}
            'gpu1'{$request.DeviceIDs=@('1')}
            'all'{$request.DeviceIDs=@('all')}
            'count-minus1'{$request.Count=-1}
            'count-one'{$request.Count=1}
            'count-string'{$request.Count='0'}
            'count-bool'{$request.Count=$false}
            'count-float'{$request.Count=[double]0}
            'count-array'{$request.Count=@(0)}
            'ids-scalar'{$request.DeviceIDs='0'}
            'caps-flat'{$request.Capabilities=@('gpu')}
            'caps-extra'{$request.Capabilities=@(@('gpu','compute'))}
            'caps-or'{$request.Capabilities=@(@('gpu'),@('utility'))}
            'options'{$request.Options=[pscustomobject]@{unknown='yes'}}
            'driver'{$request.Driver='other'}
            'extra-key'{$request|Add-Member -NotePropertyName Future -NotePropertyValue 'unreviewed'}
            'extra-request'{$api.HostConfig.DeviceRequests+=,(Clone $request)}
            'device-cpu'{$api.Config.Env[-1]='MINERU_DEVICE_MODE=cpu'}
            'duplicate-env'{$api.Config.Env+=,'MINERU_DEVICE_MODE=cuda:0'}
            'wrong-case-env'{$api.Config.Env[-1]='MINERU_DEVICE_MODE=CUDA:0'}
            'privileged'{$api.HostConfig.Privileged=$true}
            'direct-device'{$api.HostConfig.Devices=@([pscustomobject]@{PathOnHost='/dev/nvidia0';PathInContainer='/dev/nvidia0';CgroupPermissions='rwm'})}
            'cap-add'{$api.HostConfig.CapAdd=@('SYS_ADMIN')}
        }
        Reject {Assert-ApiDeviceRuntime -Container $api -Profile 'cuda0'} '.' $variant
    }
    Reject {Assert-ApiDeviceRuntime -Container (Cuda-Container) -Profile 'cpu'} '.' 'CUDA runtime cannot pass CPU expected profile'
    Reject {Assert-ApiDeviceRuntime -Container (Cpu-Container) -Profile 'cuda0'} '.' 'CPU runtime cannot pass CUDA expected profile'
}
Case 'real API-only preflight uses strict device transition instead of capacity normalization' {
    Reset
    Assert-ApiOnlyUpgradeInputs
    $script:NextCompose.services.'mineru-api'.environment.MINERU_API_MAX_CONCURRENT_REQUESTS='6'
    $script:CapacityInputs.environment.MINERU_API_MAX_CONCURRENT_REQUESTS='6'
    Reject {Assert-ApiOnlyUpgradeInputs} '.' 'even matching candidate capacity cannot authorize an extra N change'
    Check (@($script:Calls|Where-Object{$_[0]-ceq 'compose' -and $_[-1]-cne 'json'}).Count-eq 0) 'preflight never recreates services'
}
Case 'rollback restores exact original CPU files and profile while recreating only API' {
    Reset
    Restore-PreviousDeployment
    Check ([IO.File]::ReadAllText($ComposeTarget)-ceq $script:OriginalComposeBytes) 'original snapshot bytes restored'
    Check ([IO.File]::ReadAllText($CollectorTarget)-ceq 'original-collector') 'collector snapshot restored'
    Check ([IO.File]::ReadAllText($ReceiptTarget)-ceq 'original-receipt') 'receipt snapshot restored'
    $recreates=@($script:Calls|Where-Object{$_[0]-ceq 'compose'})
    Check ($recreates.Count-eq 1) 'exactly one API recreation'
    Check (($recreates[0]-join '|')-ceq (@('compose','--project-name',$ProjectName,'--file',$ComposeTarget,'up','--detach','--no-build','--no-deps','--force-recreate','mineru-api')-join '|')) 'engine and proxy never recreated'
    Check (($script:Calls[0]-join '|')-ceq 'registry-witness') 'registry witness is first rollback operation'
    Check ($script:RunningApi.Image-ceq $OldApiCompatImageId) 'restored API old image is observed'
}
Case 'rollback cannot report success when restored runtime retains unauthorized CUDA device' {
    Reset;$script:WrongRollbackDevice=$true
    Reject {Restore-PreviousDeployment} 'device|Device|GPU|profile|CPU|CUDA' 'old CPU profile must be checked after restoration'
}
Case 'retained responsibility change blocks rollback before any file or service mutation' {
    Reset;$script:ChangedRegistry=$true
    Reject {Restore-PreviousDeployment} 'rollback_blocked_registry_changed'
    foreach($p in @($ComposeTarget,$CollectorTarget,$ReceiptTarget)){Check ([IO.File]::ReadAllText($p)-ceq 'candidate-cuda-profile') 'blocked rollback leaves candidate bytes untouched'}
    Check ($script:Calls.Count-eq 1 -and ($script:Calls[0]-join '|')-ceq 'registry-witness') 'no recreate or device detach when debt changed'
}
$receipt=[ordered]@{schema='independent-mineru-api-device-profile-tests.v1';powershell_version=$PSVersionTable.PSVersion.ToString();process_id=$PID;
    installer_sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $InstallerPath).Hash.ToLowerInvariant();
    test_sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $PSCommandPath).Hash.ToLowerInvariant();
    actual_compose_pair_sha256=$ActualComposePairSha256;installer_top_level_executed=$false;native_or_network_executed=$false;results=@($script:Results.ToArray())}
[IO.File]::WriteAllText((Join-Path $OutputRoot 'result.json'),($receipt|ConvertTo-Json -Depth 80),$Utf8)
if(@($script:Results|Where-Object{$_.status-cne 'pass'}).Count){exit 1}
exit 0
