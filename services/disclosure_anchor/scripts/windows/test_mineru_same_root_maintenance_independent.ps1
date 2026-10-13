<#
.SYNOPSIS
Independent same-root acceptance harness. Windows PowerShell 5.1 x64 only.

.DESCRIPTION
Root reviews and runs this script. It uses only a new disposable OutputRoot.
Existing owner-child cases run through the actual installation owner test,
whose fake Docker is the sole Docker executable exposed to its child PATH.
An exact finite outer test Job owns every delegated test process. A forced test
deadline is a failure, never evidence that a Docker daemon helper has exited.

The actual child functions are exercised with recorded Docker responses, without
executing the child top level. Whole-wrapper pending/approval gates remain blocked
until the evolving wrapper is ready; phase tests do not replace that acceptance. Their required acceptance cases are recorded as BLOCKED and exit 2;
the existing-owner positive cases never turn incomplete maintenance acceptance
into PASS. There is no test-local replacement owner algorithm.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$SourceRoot,
    [Parameter(Mandatory=$true)][string]$OutputRoot,
    [string]$ExpectedWrapperSha256='',
    [switch]$RunExistingOwnerChildCases
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'Windows PowerShell 5.1 is required' }
if (-not [Environment]::Is64BitProcess) { throw '64-bit PowerShell is required' }
$SourceRoot=[IO.Path]::GetFullPath($SourceRoot)
$OutputRoot=[IO.Path]::GetFullPath($OutputRoot)
$Utf8=[Text.UTF8Encoding]::new($false,$true)
$Results=[Collections.Generic.List[object]]::new()
$SourceHashes=[ordered]@{}

function Check([bool]$Condition,[string]$Message) { if (-not $Condition) { throw $Message } }
function Get-ExactSha256([string]$Path) { return 'sha256:' + (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant() }
function Write-NewJson([string]$Path,$Value) {
    $bytes=$script:Utf8.GetBytes(($Value | ConvertTo-Json -Depth 30 -Compress))
    $stream=[IO.FileStream]::new($Path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try { $stream.Write($bytes,0,$bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
}
function Get-ParsedSource([string]$Path) {
    $tokens=$null; $errors=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile($Path,[ref]$tokens,[ref]$errors)
    Check (@($errors).Count -eq 0) ('source parse failed: ' + $Path + ': ' + ($errors | Out-String))
    return $ast
}
function Import-ActualFunctions([string]$Path,[string[]]$Names) {
    $ast=Get-ParsedSource $Path
    foreach ($name in $Names) {
        $nodes=@($ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name }.GetNewClosure(),$true))
        Check ($nodes.Count -eq 1) ('required actual function absent or ambiguous: ' + $name + ' in ' + $Path)
        # Only reviewed function declarations execute; the owner's top level is
        # never dot-sourced. Return their blocks for evaluation in caller scope.
        [ScriptBlock]::Create($nodes[0].Extent.Text)
    }
}
function Get-LiteralFixture([string]$Path,[string]$Name) {
    $ast=Get-ParsedSource $Path
    $nodes=@($ast.FindAll({ param($n)
        $n -is [Management.Automation.Language.AssignmentStatementAst] -and
        $n.Left -is [Management.Automation.Language.VariableExpressionAst] -and
        $n.Left.VariablePath.UserPath -ceq $Name
    }.GetNewClosure(),$true))
    Check ($nodes.Count -eq 1) ('required literal fixture absent or ambiguous: ' + $Name)
    $values=@($nodes[0].Right.FindAll({ param($n) $n -is [Management.Automation.Language.StringConstantExpressionAst] },$true))
    Check ($values.Count -eq 1) ('fixture must contain one literal string: ' + $Name)
    return $values[0].Value
}
function Case([string]$Name,[scriptblock]$Body) {
    try {
        & $Body
        $script:Results.Add([ordered]@{name=$Name;status='pass'})
        Write-Host ('PASS ' + $Name)
    } catch {
        $script:Results.Add([ordered]@{name=$Name;status='fail';error=$_.ToString()})
        Write-Host ('FAIL ' + $Name + ': ' + $_.Exception.Message)
    }
}
function Blocked([string]$Name,[string]$RequiredInterface) {
    $script:Results.Add([ordered]@{name=$Name;status='blocked';required_interface=$RequiredInterface})
    Write-Host ('BLOCKED ' + $Name + ': ' + $RequiredInterface)
}

foreach ($protected in @([Environment]::SystemDirectory,'C:\ProgramData','C:\Windows','C:\Program Files')) {
    Check (-not $OutputRoot.StartsWith($protected,[StringComparison]::OrdinalIgnoreCase)) 'new disposable OutputRoot must be outside protected locations'
}
Check (Test-Path -LiteralPath $SourceRoot -PathType Container) 'SourceRoot must exist'
Check (-not (Test-Path -LiteralPath $OutputRoot)) 'OutputRoot must be new'
Check (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($OutputRoot)) -PathType Container) 'OutputRoot parent must exist'
[void][IO.Directory]::CreateDirectory($OutputRoot)
$Wrapper=Join-Path $SourceRoot 'run_mineru_installation.ps1'
$ExistingOwnerTests=Join-Path $SourceRoot 'test_mineru_installation_owner_independent.ps1'
$ExistingHealthTests=Join-Path $SourceRoot 'test_mineru_installation_idle_health.ps1'
$TestProcessSource=Join-Path $SourceRoot 'test_mineru_m6_process.cs'
foreach ($path in @($Wrapper,$ExistingOwnerTests,$ExistingHealthTests,$TestProcessSource)) {
    Check (Test-Path -LiteralPath $path -PathType Leaf) ('required existing harness/source absent: ' + $path)
    $SourceHashes[[IO.Path]::GetFileName($path)]=Get-ExactSha256 $path
}
if ($ExpectedWrapperSha256 -ne '') {
    Check ($ExpectedWrapperSha256 -cmatch '^sha256:[0-9a-f]{64}$') 'ExpectedWrapperSha256 must be canonical'
    Check ($SourceHashes['run_mineru_installation.ps1'] -ceq $ExpectedWrapperSha256) 'wrapper hash differs from reviewed source'
}

# Literal serving-wire fixtures from the existing tracked test. These are
# wire examples, never a qualification, business task, or current live proof.
$V2Health=Get-LiteralFixture $ExistingHealthTests 'V2Health'
$V2Capacity=Get-LiteralFixture $ExistingHealthTests 'V2Capacity'
foreach ($definition in @(Import-ActualFunctions $Wrapper @('Assert-Closed','Read-IdleCapacityHealth'))) { . $definition }
$script:FixtureWire=$V2Health
$script:FixtureReadFails=$false
function Invoke-BoundedReadback { param($FilePath,$Arguments)
    if ($script:FixtureReadFails) { throw 'synthetic stopped API readback; no native process started' }
    return [pscustomobject]@{ExitCode=0;StandardOutput=$script:FixtureWire;StandardError=''}
}
Case 'actual_ordinary_idle_validator_accepts_closed_storage_wire' {
    $actual=Read-IdleCapacityHealth 'synthetic-unused-docker' $V2Capacity 'independent-test'
    Check ($actual.task_admission.durable_nonterminal_tasks -eq 0) 'positive literal fixture changed'
    Check ($actual.capacity_observation.schema -ceq 'mineru.capacity-observation.v2') 'wrong serving family'
}
Case 'ordinary_idle_validator_still_refuses_stopped_api' {
    $script:FixtureReadFails=$true
    $caught=$null
    try { $null=Read-IdleCapacityHealth 'synthetic-unused-docker' $V2Capacity 'independent-test' } catch { $caught=$_.Exception.Message }
    finally { $script:FixtureReadFails=$false }
    Check ($null -ne $caught -and $caught -match 'synthetic stopped API') 'stopped API was treated as a live idle proof'
}

function Invoke-ExistingExactJobCase([string]$Name,[int]$DeadlineMilliseconds) {
    $caseRoot=Join-Path $script:OutputRoot $Name
    $prefix=Join-Path $script:OutputRoot ($Name + '-outer-job')
    $exe=Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
    $arguments=[string[]]@('-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','RemoteSigned',
        '-File',$script:ExistingOwnerTests,'-SourceRoot',$script:SourceRoot,'-OutputRoot',$caseRoot,
        '-ExpectedWrapperSha256',$script:SourceHashes['run_mineru_installation.ps1'],'-CaseFilter',$Name)
    $process=$null
    try {
        $process=[M6BoundedProcess]::new($exe,$arguments,$script:OutputRoot,$prefix)
        $process.Finish($DeadlineMilliseconds)
        Check (-not $process.ForcedTermination) 'outer test Job intervened: no product pass may be inferred'
        Check ($process.ActiveJobProcesses -eq 0) 'a test Job descendant survived'
        Check ($process.ExitCode -eq 0) ('existing real-owner case failed with exit ' + $process.ExitCode)
        $evidence=([IO.File]::ReadAllText((Join-Path $caseRoot 'runner-evidence.json'),$script:Utf8) | ConvertFrom-Json)
        Check (@($evidence.cases).Count -eq 1) 'exactly one delegated owner case must have run'
        Check ($evidence.cases[0].case -ceq $Name -and $evidence.cases[0].status -ceq 'pass') 'delegated owner evidence names another case or failed'
        Write-NewJson (Join-Path $script:OutputRoot ($Name + '-outer-job.json')) ([ordered]@{
            exact_process_id=$process.Pid;creation_filetime=$process.CreationFiletime;exit_code=$process.ExitCode
            forced_termination=$process.ForcedTermination;active_job_processes=$process.ActiveJobProcesses
            stdout_path=$process.StdoutPath;stderr_path=$process.StderrPath
        })
    } finally { if ($null -ne $process) { $process.Dispose() } }
}
if ($RunExistingOwnerChildCases) {
    # Reuse the actual prior real-child/fake-Docker/disposable ComposeTarget and
    # ReceiptTarget composition checks, including their literal expectations.
    Add-Type -Path $TestProcessSource -ErrorAction Stop
    Case 'existing_daemon_unknown_record_keeps_owner_closed' {
        Invoke-ExistingExactJobCase 'daemon_mutation_then_child_timeout_stays_unknown_without_rollback' 300000
    }
    Case 'existing_forced_job_does_not_prove_daemon_work_ended' {
        Invoke-ExistingExactJobCase 'daemon_mutation_then_job_lifetime_forced_close_stays_unknown' 360000
    }
} else {
    Blocked 'existing_real_owner_child_baselines' 'Explicit -RunExistingOwnerChildCases after root reviews finite child/fake-Docker target isolation.'
}

# Actual child-function acceptance. The only replacement is the Docker transport:
# fixed recorded responses with exact expected command prefixes. Files/records,
# intent sequencing, ownership decisions, result classification and argv builder
# are the shipped functions. No test-local owner implementation executes.
$Child=Join-Path $SourceRoot 'run_mineru_same_root_maintenance.ps1'
if(Test-Path -LiteralPath $Child -PathType Leaf){
 $SourceHashes['run_mineru_same_root_maintenance.ps1']=Get-ExactSha256 $Child
 # Import every shipped function except the transport replaced below. New
 # internal ownership/checkpoint helpers remain actual product code as the
 # child evolves; no top-level statements or child mutations are evaluated.
 $childAst=Get-ParsedSource $Child
 $childNames=@($childAst.FindAll({param($node)$node -is [Management.Automation.Language.FunctionDefinitionAst]},$true) | ForEach-Object{$_.Name} | Where-Object{$_ -cne 'Invoke-DockerBounded'})
 foreach($definition in @(Import-ActualFunctions $Child $childNames)){. $definition}
 $script:MaximumRegistryBytes=16777216
 $script:HelperDeadlineSeconds=120
 $script:ApiOutputTarget='/var/lib/mineru-api-output'
 $script:HelperMount='/opt/mineru-same-root'
 $script:IdentityFields=@('final_path','volume_path','file_system','volume_serial','file_id','is_directory','sddl')
 $script:OperationId='sr-0123456789abcdef0123456789abcdef'
 $script:ApiImageId='sha256:'+('a'*64)
 $script:HelperSourceSha256=Get-ExactSha256 (Join-Path $SourceRoot 'mineru_same_root_registry_helper.py')
 $script:ProtocolSourceSha256=Get-ExactSha256 (Join-Path $SourceRoot 'mineru_heap_trim_compat\agent_task_protocol_v2.py')
 $script:ReleaseRoot=Join-Path $OutputRoot 'literal-helper-release'
 [void][IO.Directory]::CreateDirectory((Join-Path $ReleaseRoot 'windows'))
 [void][IO.Directory]::CreateDirectory((Join-Path $ReleaseRoot 'api-context'))
 Write-NewBytes (Join-Path $ReleaseRoot 'windows\mineru_same_root_registry_helper.py') ([IO.File]::ReadAllBytes((Join-Path $SourceRoot 'mineru_same_root_registry_helper.py')))
 Write-NewBytes (Join-Path $ReleaseRoot 'api-context\agent_task_protocol_v2.py') ([IO.File]::ReadAllBytes((Join-Path $SourceRoot 'mineru_heap_trim_compat\agent_task_protocol_v2.py')))
 $SourceHashes['mineru_same_root_registry_helper.py']=$HelperSourceSha256
 $SourceHashes['mineru_heap_trim_compat/agent_task_protocol_v2.py']=$ProtocolSourceSha256
 $script:RecordedSteps=[Collections.Generic.Queue[object]]::new()
 $script:RecordedCalls=[Collections.Generic.List[object]]::new()
 function Response([string[]]$Prefix,[string]$Output='',[int]$Code=0,[bool]$Timeout=$false,[string]$ErrorText=''){
  $script:RecordedSteps.Enqueue([pscustomobject]@{prefix=$Prefix;value=[pscustomobject]@{ExitCode=$Code;TimedOut=$Timeout;StandardOutput=$Output;StandardError=$ErrorText}})
 }
 function Invoke-DockerBounded([string[]]$Arguments,[long]$TimeoutMilliseconds,[int]$MaximumBytes=1048576){
  $script:RecordedCalls.Add([ordered]@{arguments=$Arguments;timeout_ms=$TimeoutMilliseconds})
  if($script:InjectStopAfterResumeGate -and $script:RecordedSteps.Count -eq 0){$script:ResumeReachedNextBoundary=$true;throw 'independent stop after close resume ownership gate'}
  Check ($script:RecordedSteps.Count -gt 0) ('unexpected Docker call: '+($Arguments -join ' '))
  $step=$script:RecordedSteps.Dequeue()
  Check ($Arguments.Count -ge $step.prefix.Count) 'short Docker argv'
  for($i=0;$i -lt $step.prefix.Count;$i++){Check ($Arguments[$i] -ceq  $step.prefix[$i]) ('Docker transcript differs: '+($Arguments -join ' '))}
  if($Arguments[0] -ceq  'rm'){
   Check (Test-Path -LiteralPath (Join-Path $script:OpDir 'helper-apply-1-result.json')) 'helper removal preceded persisted result'
  }
  return $step.value
 }
 function New-ChildFixture([string]$CaseName){
  $script:ReleaseManifestSha256='sha256:'+('b'*64)
  $script:OpDir=Join-Path $script:OutputRoot ($CaseName+'-operation')
  [void][IO.Directory]::CreateDirectory($script:OpDir)
  $script:PendingPath=Join-Path $script:OpDir 'pending.json'
  $script:Uncertain=$false
  $script:CheckSequence=0
  $script:InjectStopAfterResumeGate=$false;$script:ResumeReachedNextBoundary=$false
  $script:RecordedSteps.Clear();$script:RecordedCalls.Clear()
  $script:ExactHelper='mineru-same-root-'+$script:OperationId+'-apply-1'
  $script:Frozen=[pscustomobject]@{api_image_id=$script:ApiImageId;api_output_bind=[pscustomobject]@{source=(Join-Path $script:OutputRoot 'unmounted-synthetic-output');destination=$script:ApiOutputTarget;rw=$true};containers=@([pscustomobject]@{name='mineru-api';user='';restart_policy='unless-stopped'})}
  $null=Write-StateRecord 'containers.json' $script:Frozen
  Write-NewBytes $script:PendingPath (ConvertTo-RecordBytes ([ordered]@{contract_version='mineru-same-root-pending.v1';operation_id=$script:OperationId;state_directory=$script:OpDir;release_manifest_sha256=$script:ReleaseManifestSha256;created_utc='2026-10-01T00:00:00Z'}))
  $script:PendingBefore=[IO.File]::ReadAllBytes($script:PendingPath)
  $script:ContainersBefore=[IO.File]::ReadAllBytes((Join-Path $script:OpDir 'containers.json'))
  $script:Request=New-HelperRequest 'apply' ([pscustomobject]@{path=$script:ApiOutputTarget;device=13;inode=27;uid=0;mode=16832}) ('sha256:'+('c'*64)) 400 ('sha256:'+('d'*64)) ([pscustomobject]@{path=$script:ApiOutputTarget;device=14;inode=28;uid=0;mode=16832}) ('sha256:'+('e'*64)) 401
 }
 function HelperWire([string]$State,[string]$Operation=''){
  if($Operation -ceq  ''){$Operation=$script:OperationId}
  return (([ordered]@{Id=('f'*64);Name=('/'+$script:ExactHelper);Image=$script:ApiImageId;
   Mounts=@([ordered]@{Type='bind';Source=$script:Frozen.api_output_bind.source;Destination=$script:ApiOutputTarget;RW=$true},[ordered]@{Type='bind';Source=(Join-Path $script:OpDir 'helper-apply-1');Destination=$script:HelperMount;RW=$false});
   Config=[ordered]@{User='';Labels=[ordered]@{'agent-invest.same-root.operation'=$Operation;'agent-invest.same-root.action'='apply';'agent-invest.same-root.sequence'='1'}};
   HostConfig=[ordered]@{NetworkMode='none';ReadonlyRootfs=$true;RestartPolicy=[ordered]@{Name='no'}};
   State=[ordered]@{Status=$State;Running=($State -ceq  'running');ExitCode=0;OOMKilled=$false;FinishedAt='2026-10-01T00:00:01Z'}})|ConvertTo-Json -Depth 12 -Compress)
 }
 function ListEmpty{Response @('container','ls','--all') ''}
 function Absent{Response @('container','inspect',$script:ExactHelper) '' 1 $false 'No such container'}
 function InspectHelper([string]$State,[string]$Operation=''){Response @('container','inspect',$script:ExactHelper) ('['+(HelperWire $State $Operation)+']')}
 function FinishedResult{
  $result=[ordered]@{schema='mineru-same-root-apply.v1';operation_id=$script:OperationId;outcome='applied';registry_sha256=('sha256:'+('e'*64));registry_bytes=401
   registry_file=[ordered]@{device=14;inode=32;uid=0;mode=33152;nlink=1;size=401}
   previous_registry_file=[ordered]@{device=14;inode=31;uid=0;mode=33152;nlink=1;size=400}
   current_root=$script:Request.current_root;stale_temp_removed=$false}
  Response @('logs',$script:ExactHelper) ($result|ConvertTo-Json -Depth 12 -Compress)
  Response @('rm',$script:ExactHelper) $script:ExactHelper
 }
 function CheckPreserved{
  Check ($script:RecordedSteps.Count -eq 0) 'Docker response transcript was not fully consumed'
  Check ((Get-Sha256Bytes ([IO.File]::ReadAllBytes($script:PendingPath))) -ceq  (Get-Sha256Bytes $script:PendingBefore)) 'pending marker changed outside verified close'
  Check ((Get-Sha256Bytes ([IO.File]::ReadAllBytes((Join-Path $script:OpDir 'containers.json')))) -ceq  (Get-Sha256Bytes $script:ContainersBefore)) 'frozen restart snapshot changed'
  Check (@($script:RecordedCalls|Where-Object{$_.arguments[0] -ceq  'update'}).Count -eq 0) 'helper outcome automatically restored a restart policy'
  $creates=@($script:RecordedCalls|Where-Object{$_.arguments[0] -ceq  'create'})
  foreach($call in $creates){$i=[Array]::IndexOf([string[]]$call.arguments,'--name');Check ($i -ge  0 -and $call.arguments[$i+1] -ceq  $script:ExactHelper) 'helper identity drifted across acknowledgments'}
  $script:CheckSequence+=1
  Write-NewJson (Join-Path $script:OpDir ('independent-transcript-'+$script:CheckSequence+'.json')) ([ordered]@{
   boundary='actual child functions, recorded Docker responses; no native Docker or child top level'
   calls=@($script:RecordedCalls.ToArray());uncertain=$script:Uncertain
   pending_sha256=(Get-Sha256Bytes ([IO.File]::ReadAllBytes($script:PendingPath)))
   frozen_restart_snapshot_sha256=(Get-Sha256Bytes ([IO.File]::ReadAllBytes((Join-Path $script:OpDir 'containers.json'))))
  })
 }
 Case 'actual_child_lost_create_ack_resolves_same_predeclared_helper'{
  New-ChildFixture 'lost-create'
  ListEmpty;Absent;Response @('create') '' 0 $true;InspectHelper 'created'
  Response @('start',$script:ExactHelper) $script:ExactHelper;InspectHelper 'exited';FinishedResult
  $actual=Invoke-SameRootHelper 'apply' $script:Request
  Check ($actual.Kind -ceq  'pass' -and $actual.Name -ceq  $script:ExactHelper -and $actual.Sequence -eq  1) 'lost create acknowledgment did not finish exact helper'
  Check (@($script:RecordedCalls|Where-Object{$_.arguments[0] -ceq  'create'}).Count -eq 1) 'lost acknowledgment started another create'
  CheckPreserved
 }
 Case 'actual_child_lost_start_ack_resolves_same_helper'{
  New-ChildFixture 'lost-start'
  ListEmpty;Absent;Response @('create') ('f'*64);InspectHelper 'created'
  Response @('start',$script:ExactHelper) '' 0 $true;InspectHelper 'running'
  Response @('wait',$script:ExactHelper) '0';InspectHelper 'exited';FinishedResult
  $actual=Invoke-SameRootHelper 'apply' $script:Request
  Check ($actual.Kind -ceq  'pass' -and $actual.Name -ceq  $script:ExactHelper) 'lost start acknowledgment did not finish exact helper'
  Check (@($script:RecordedCalls|Where-Object{$_.arguments[0] -ceq  'start'}).Count -eq 1) 'lost acknowledgment repeated start'
  CheckPreserved
 }
 Case 'actual_child_live_helper_deadline_remains_unknown_and_resumes_same_intent'{
  New-ChildFixture 'live-deadline'
  ListEmpty;Absent;Response @('create') ('f'*64);InspectHelper 'created'
  Response @('start',$script:ExactHelper) $script:ExactHelper;InspectHelper 'running'
  Response @('wait',$script:ExactHelper) '' 0 $true
  $caught=$null;try{$null=Invoke-SameRootHelper 'apply' $script:Request}catch{$caught=$_.Exception.Message}
  Check ($null -ne  $caught -and $script:Uncertain) 'live helper deadline did not stay unknown'
  Check (-not (Test-Path -LiteralPath (Join-Path $script:OpDir 'helper-apply-1-result.json'))) 'deadline fabricated terminal result'
  Check (@($script:RecordedCalls|Where-Object{$_.arguments[0] -ceq  'rm'}).Count -eq 0) 'live helper was removed'
  CheckPreserved
  $intentHash=Get-ExactSha256 (Join-Path $script:OpDir 'helper-apply-1-intent.json')
  $script:RecordedCalls.Clear();InspectHelper 'exited';FinishedResult
  $actual=Invoke-SameRootHelper 'apply' $script:Request
  Check ($actual.Kind -ceq  'pass' -and $actual.Sequence -eq  1) 'resumed helper sequence changed'
  Check ((Get-ExactSha256 (Join-Path $script:OpDir 'helper-apply-1-intent.json')) -ceq  $intentHash) 'resumed intent was rewritten'
  Check (@($script:RecordedCalls|Where-Object{$_.arguments[0] -ceq  'create' -or $_.arguments[0] -ceq  'start'}).Count -eq 0) 'resume recreated or restarted completed helper'
  CheckPreserved
 }
 Case 'actual_child_wrong_helper_operation_label_refuses_without_start_or_remove'{
  New-ChildFixture 'foreign-owner'
  ListEmpty;InspectHelper 'created' 'sr-ffffffffffffffffffffffffffffffff'
  $caught=$null;try{$null=Invoke-SameRootHelper 'apply' $script:Request}catch{$caught=$_.Exception.Message}
  Check ($null -ne  $caught) 'foreign helper operation was adopted'
  Check (@($script:RecordedCalls|Where-Object{$_.arguments[0] -ceq  'start' -or $_.arguments[0] -ceq  'rm'}).Count -eq 0) 'foreign helper was mutated'
  CheckPreserved
 }
 Case 'actual_child_unreadable_helper_identity_stays_unknown'{
  New-ChildFixture 'unreadable-owner'
  ListEmpty;Response @('container','inspect',$script:ExactHelper) '' 0 $true
  $caught=$null;try{$null=Invoke-SameRootHelper 'apply' $script:Request}catch{$caught=$_.Exception.Message}
  Check ($null -ne  $caught -and $script:Uncertain) 'unreadable helper identity was treated as absent'
  Check (@($script:RecordedCalls|Where-Object{$_.arguments[0] -ceq  'create'}).Count -eq 0) 'unknown helper identity allowed create'
  CheckPreserved
 }
 Case 'actual_child_pending_helper_rejects_a_changed_approved_request_before_Docker'{
  New-ChildFixture 'changed-request'
  ListEmpty;Response @('container','inspect',$script:ExactHelper) '' 0 $true
  $caught=$null;try{$null=Invoke-SameRootHelper 'apply' $script:Request}catch{$caught=$_.Exception.Message}
  Check ($null -ne  $caught -and $script:Uncertain) 'initial unknown intent fixture did not remain pending'
  $intentHash=Get-ExactSha256 (Join-Path $script:OpDir 'helper-apply-1-intent.json')
  $callCount=$script:RecordedCalls.Count
  $changed=(ConvertTo-RecordBytes $script:Request)|ForEach-Object{$_}
  $changed=$script:Utf8.GetString([byte[]]$changed)|ConvertFrom-Json
  $changed.expected_r1_sha256='sha256:'+('0'*64)
  $caught=$null;try{$null=Invoke-SameRootHelper 'apply' $changed}catch{$caught=$_.Exception.Message}
  Check ($null -ne  $caught) 'pending intent accepted a different approved pair'
  Check ($script:RecordedCalls.Count -eq $callCount) 'changed request touched Docker before resolving owned intent'
  Check ((Get-ExactSha256 (Join-Path $script:OpDir 'helper-apply-1-intent.json')) -ceq  $intentHash) 'changed request replaced old intent'
  CheckPreserved
 }
 Case 'actual_child_incomplete_writer_success_record_is_unknown'{
  New-ChildFixture 'incomplete-result'
  ListEmpty;InspectHelper 'exited'
  Response @('logs',$script:ExactHelper) '{"schema":"mineru-same-root-apply.v1"}'
  Response @('rm',$script:ExactHelper) $script:ExactHelper
  $actual=Invoke-SameRootHelper 'apply' $script:Request
  Check ($actual.Kind -ceq  'unknown' -and $script:Uncertain) 'schema-only writer output became a success receipt'
  CheckPreserved
 }
 Case 'actual_child_pending_marker_rejects_a_different_operation'{
  New-ChildFixture 'wrong-pending'
  $saved=$script:OperationId
  try{$script:OperationId='sr-ffffffffffffffffffffffffffffffff';$caught=$null;try{Assert-PendingOperation}catch{$caught=$_.Exception.Message};Check ($null -ne  $caught) 'another pending operation was accepted'}finally{$script:OperationId=$saved}
  CheckPreserved
 }
 function New-CloseFixture([string]$Name,[bool]$OriginalPoliciesNo=$false){
  New-ChildFixture $Name
  $script:ContainerNames=@('mineru-openai-server','mineru-api','mineru-api-proxy')
  $script:Facts=[ordered]@{};$script:Outcome=$null
  $policies=@('always','unless-stopped','on-failure:3')
  if($OriginalPoliciesNo){$policies=@('no','no','no')}
  $items=@()
  for($i=0;$i -lt  3;$i++){$items+=[ordered]@{name=$script:ContainerNames[$i];id=([string]($i+1)*64);image_id=$script:ApiImageId;restart_policy=$policies[$i];user=''}}
  $script:Frozen=[pscustomobject]@{containers=$items;api_image_id=$script:ApiImageId;api_output_bind=$script:Frozen.api_output_bind}
  [IO.File]::WriteAllBytes((Join-Path $script:OpDir 'containers.json'),(ConvertTo-RecordBytes $script:Frozen))
  $script:ContainersBefore=[IO.File]::ReadAllBytes((Join-Path $script:OpDir 'containers.json'))
  $expected=[ordered]@{source='apply-1.json';plan_sha256=('sha256:'+('9'*64));root_identity=$script:Request.current_root;registry_sha256=$script:Request.expected_r1_sha256;registry_bytes=401;registry_file_id='synthetic-r1-id';record_count=3;submission_watermark_bucket=1900000123}
  $null=Write-StateRecord 'prestop.json' ([ordered]@{contract_version='mineru-same-root-prestop.v1';operation_id=$script:OperationId;release_manifest_sha256=$script:ReleaseManifestSha256})
  $null=Write-StateRecord 'restore-1.json' ([ordered]@{contract_version='mineru-same-root-restore.v1';operation_id=$script:OperationId;expected=$expected;started=@('mineru-openai-server','mineru-api')})
  $state=[ordered]@{file_count=1;total_bytes=401;quiescence=[ordered]@{root_identity=$expected.root_identity;registry_sha256=$expected.registry_sha256;record_count=3;submission_watermark_bucket=1900000123}}
  $proof=[ordered]@{contract_version='m6.same-root-api-proof.v1';operation_id=$script:OperationId;phase='close';proof_line=$script:Utf8.GetString((ConvertTo-RecordBytes $state))}
  $script:ProofPath=Join-Path $script:OutputRoot ($Name+'-verified-api-proof.json')
  $script:ProofBytes=ConvertTo-RecordBytes $proof
  Write-NewBytes $script:ProofPath $script:ProofBytes
  $script:ProofSha256=Get-Sha256Bytes $script:ProofBytes
 }
 function BusinessOne([int]$Index,[string]$Policy='no',[bool]$Running=$true){
  $count=0;$name=$Policy
  if($Policy.StartsWith('on-failure:')){$name='on-failure';$count=[int]$Policy.Substring(11)}
  $item=$script:Frozen.containers[$Index]
  return [ordered]@{Id=$item.id;Name=('/'+$item.name);Image=$item.image_id;RestartCount=0;Config=[ordered]@{User=''}
   HostConfig=[ordered]@{RestartPolicy=[ordered]@{Name=$name;MaximumRetryCount=$count}}
   State=[ordered]@{Running=$Running;Status=$(if($Running){'running'}else{'exited'});ExitCode=0;OOMKilled=$false;Health=[ordered]@{Status='healthy'}}}
 }
 function BusinessInspect([string[]]$Policies,[bool]$ProxyRunning=$true){
  $all=@();for($i=0;$i -lt  3;$i++){$all+=BusinessOne $i $Policies[$i] ($i -ne  2 -or $ProxyRunning)}
  Response @('container','inspect','mineru-openai-server','mineru-api','mineru-api-proxy') ($all|ConvertTo-Json -Depth 12 -Compress)
 }
 function BusinessIdInspect([int]$Index,[string]$Policy='no',[bool]$Running=$true){
  Response @('container','inspect',[string]$script:Frozen.containers[$Index].id) ('['+((BusinessOne $Index $Policy $Running)|ConvertTo-Json -Depth 12 -Compress)+']')
 }
 function CloseTranscript([string]$Checkpoint){
  Write-NewJson (Join-Path $script:OpDir ('independent-close-'+$Checkpoint+'.json')) ([ordered]@{
   boundary='actual Invoke-Close with recorded Docker responses; no native Docker/top level'
   calls=@($script:RecordedCalls.ToArray());uncertain=$script:Uncertain;outcome=$script:Outcome
   pending_present=(Test-Path -LiteralPath $script:PendingPath);close_present=(Test-Path -LiteralPath (Join-Path $script:OpDir 'close.json'))
   frozen_snapshot_sha256=(Get-Sha256Bytes ([IO.File]::ReadAllBytes((Join-Path $script:OpDir 'containers.json'))))
  })
 }
 Case 'actual_business_container_decoder_selects_three_distinct_members_from_standard_wire_array'{
  New-CloseFixture 'business-array-decoder'
  # This is the ordinary docker inspect wire shape: one JSON array with three
  # member objects. Direct ConvertFrom-Json assignment and foreach verify the
  # fixture before the actual product reader applies its own array handling.
  $wire='[{"Name":"/mineru-openai-server","Id":"1111111111111111111111111111111111111111111111111111111111111111"},{"Name":"/mineru-api","Id":"2222222222222222222222222222222222222222222222222222222222222222"},{"Name":"/mineru-api-proxy","Id":"3333333333333333333333333333333333333333333333333333333333333333"}]'
  $decoded=ConvertFrom-Json -InputObject $wire
  $members=@();foreach($member in $decoded){$members+=[string]$member.Name}
  Check ($members.Count -eq  3) 'literal business inspect fixture did not decode three members'
  Check (($members -join  '|') -ceq  '/mineru-openai-server|/mineru-api|/mineru-api-proxy') 'fixture has another container order/name'
  Write-NewBytes (Join-Path $script:OpDir 'standard-business-inspect-wire.json') ($script:Utf8.GetBytes($wire))
  Response @('container','inspect','mineru-openai-server','mineru-api','mineru-api-proxy') $wire
  try{
   $actual=Read-BusinessContainers
   Check ($actual.Count -eq  3) 'actual reader returned another cardinality'
   for($i=0;$i -lt  3;$i++){Check ([string]$actual[$script:ContainerNames[$i]].Id -ceq  ([string]($i+1)*64)) 'actual reader lost distinct container identity'}
  }finally{CloseTranscript 'array-decoder'}
 }
 Case 'actual_close_resumes_after_its_first_restart_policy_ack_and_second_timeout'{
  New-CloseFixture 'partial-close'
  BusinessInspect @('no','no','no') $false
  BusinessIdInspect 2 'no' $false;Response @('start',[string]$script:Frozen.containers[2].id) ''
  BusinessIdInspect 2 'no' $true
  Response @('update','--restart','always',[string]$script:Frozen.containers[0].id) ''
  BusinessIdInspect 0 'always' $true
  Response @('update','--restart','unless-stopped',[string]$script:Frozen.containers[1].id) '' 0 $true
  BusinessIdInspect 1 'no' $true
  $caught=$null;try{Invoke-Close}catch{$caught=$_.Exception.Message}
  Check ($null -ne  $caught -and $script:Uncertain) 'second policy timeout was not an unknown checkpoint'
  Check ($script:RecordedSteps.Count -eq  0) 'partial close did not reach the recorded acknowledged policy then timeout'
  Check (Test-Path -LiteralPath $script:PendingPath) 'partial close removed pending marker'
  Check (-not (Test-Path -LiteralPath (Join-Path $script:OpDir 'close.json'))) 'partial close fabricated completion'
  CloseTranscript 'partial'
  $script:RecordedCalls.Clear()
  BusinessInspect @('always','no','no') $true
  # A finite readback fault at the next transport boundary detects whether the
  # actual owner can continue past its own acknowledged restart-policy change.
  # This case claims that decision only; full close completion has a separate case.
  $script:InjectStopAfterResumeGate=$true
  $caught=$null;try{Invoke-Close}catch{$caught=$_.Exception.Message}finally{$script:InjectStopAfterResumeGate=$false;CloseTranscript 'resume'}
  Check ($script:ResumeReachedNextBoundary -or -not (Test-Path -LiteralPath $script:PendingPath)) ('same-operation close rejected its own restored policy: '+$caught)
  Check ((Get-Sha256Bytes ([IO.File]::ReadAllBytes((Join-Path $script:OpDir 'containers.json')))) -ceq  (Get-Sha256Bytes $script:ContainersBefore)) 'frozen original policies changed'
 }
 Case 'actual_close_refuses_foreign_policy_drift_before_further_Docker_mutation'{
  New-CloseFixture 'foreign-close-policy'
  BusinessInspect @('on-failure:7','no','no') $true
  $caught=$null;try{Invoke-Close}catch{$caught=$_.Exception.Message}finally{CloseTranscript 'foreign-policy'}
  Check ($null -ne  $caught) 'unowned restart-policy drift was accepted'
  Check ($script:RecordedCalls.Count -eq  1) 'foreign policy drift reached another Docker boundary'
  Check (Test-Path -LiteralPath $script:PendingPath) 'foreign policy drift cleared pending marker'
 }
 Case 'actual_close_refuses_frozen_original_policy_without_owned_restore_action'{
  New-CloseFixture 'foreign-original-close-policy'
  # The value belongs to the frozen vocabulary, but this operation has not
  # attempted or acknowledged any policy restoration. The value is foreign
  # action evidence, so accepting it would erase ownership of the close.
  BusinessInspect @('always','no','no') $true
  $caught=$null;try{Invoke-Close}catch{$caught=$_.Exception.Message}finally{CloseTranscript 'foreign-original-policy'}
  Check ($null -ne $caught) 'frozen original policy without this operation action was accepted'
  Check ($caught -notmatch 'unexpected Docker call|Docker transcript differs|short Docker argv') 'fixture transport failure cannot prove original-policy refusal'
  Check ($script:RecordedSteps.Count -eq 0) 'foreign-original decision did not consume its business readback'
  Check ($script:RecordedCalls.Count -eq 1) 'unattributed original policy reached another Docker boundary'
  Check (Test-Path -LiteralPath $script:PendingPath) 'unattributed policy cleared pending marker'
  Check (-not (Test-Path -LiteralPath (Join-Path $script:OpDir 'close.json'))) 'unattributed policy fabricated close'
 }
 Case 'actual_close_completes_after_exact_proof_file_exists_before_close_record'{
  New-CloseFixture 'paired-close-record' $true
  $proofCopy='api-proof-close-'+$script:ProofSha256.Substring(7,16)+'.json'
  Write-NewBytes (Join-Path $script:OpDir $proofCopy) $script:ProofBytes
  $proofBefore=Get-ExactSha256 (Join-Path $script:OpDir $proofCopy)
  BusinessInspect @('no','no','no') $true
  BusinessIdInspect 2 'no' $true;BusinessIdInspect 2 'no' $true
  for($i=0;$i -lt  3;$i++){Response @('update','--restart','no',[string]$script:Frozen.containers[$i].id) '';BusinessIdInspect $i 'no' $true}
  BusinessInspect @('no','no','no') $true
  try{Invoke-Close}finally{CloseTranscript 'paired-proof'}
  Check ($script:RecordedSteps.Count -eq  0) 'close did not consume finite recorded readbacks'
  Check ((Get-ExactSha256 (Join-Path $script:OpDir $proofCopy)) -ceq  $proofBefore) 'resume replaced original exact proof file'
  Check (Test-Path -LiteralPath (Join-Path $script:OpDir 'close.json')) 'close did not complete after byte-identical proof checkpoint'
  Check (-not (Test-Path -LiteralPath $script:PendingPath)) 'completed close left pending marker'
  Check (Test-Path -LiteralPath (Join-Path $script:OpDir 'pending.closed.json')) 'close did not archive exact pending marker'
 }
 Case 'actual_child_physical_continuity_refuses_host_and_root_control_spool_drift'{
  New-ChildFixture 'physical-continuity'
  $object=[ordered]@{final_path='C:\synthetic-root';volume_path='\\?\Volume{00000000-0000-0000-0000-000000000001}\synthetic-root';file_system='NTFS';volume_serial='0000000000000001';file_id='00000000000000010000000000000002';is_directory=$true;sddl='O:SYG:SYD:(A;;FA;;;SY)'}
  $raw=([ordered]@{host='synthetic-host';ancestors=@([ordered]@{path='C:\';file_id='00000000000000000000000000000001'});root=$object;control=$object;spool=$object}|ConvertTo-Json -Depth 12 -Compress)
  $before=$raw|ConvertFrom-Json
  Assert-SameRoot $before ($raw|ConvertFrom-Json)
  $now=$raw|ConvertFrom-Json;$now.host='another-host';$caught=$null;try{Assert-SameRoot $before $now}catch{$caught=$_.Exception.Message};Check ($null -ne  $caught) 'wrong host was accepted'
  foreach($part in @('root','control','spool')){
   foreach($field in @('file_id','volume_serial','volume_path','final_path','sddl','file_system')){
    $now=$raw|ConvertFrom-Json;$now.$part.$field='foreign-value';$caught=$null
    try{Assert-SameRoot $before $now}catch{$caught=$_.Exception.Message}
    Check ($null -ne  $caught) ($part+' '+$field+' drift was accepted')
   }
  }
  $now=$raw|ConvertFrom-Json;$now.ancestors[0].file_id='foreign-ancestor';$caught=$null;try{Assert-SameRoot $before $now}catch{$caught=$_.Exception.Message};Check ($null -ne  $caught) 'foreign ancestor FileId was accepted'
  CheckPreserved
 }
 Case 'actual_child_observe_mount_is_read_only_and_apply_uses_frozen_bind'{
  New-ChildFixture 'mount-forms'
  $observe=[string[]]@(New-HelperArguments 'synthetic-unused' 'observe' 1 $script:OpDir $script:Frozen)
  $apply=[string[]]@(New-HelperArguments 'synthetic-unused' 'apply' 1 $script:OpDir $script:Frozen)
  $prefix='type=bind,source='+$script:Frozen.api_output_bind.source+',target='+$script:ApiOutputTarget
  Check ($observe -contains  ($prefix+',readonly')) 'actual observe helper has a writable output bind'
  Check ($apply -contains  $prefix) 'actual apply helper differs from frozen API output bind'
  CheckPreserved
 }
}else{
 Blocked 'actual_child_lifecycle_recorded_transport_cases' 'Actual shipped run_mineru_same_root_maintenance.ps1 must be present beside reviewed sources.'
}
# Whole official-entry and approval boundaries still require the actual wrapper.
Blocked 'all_official_entries_refuse_another_pending_operation' 'Actual pending guard in wrapper/direct installer, executed with new disposable ReceiptTarget whose parent owns same-root-maintenance/pending.json; required to reject v1/v2 and another operation without Docker mutations.'
Blocked 'approval_binds_one_exact_R0_R1_pair_and_current_physical_root' 'Published real plan/approval validator and its complete closed Windows input shape; literal R0/R1 fixtures are in D/independent-tests/windows-literal-fixtures.json.'
Blocked 'whole_owner_lost_ack_helper_survives_job_boundary' 'Actual v3 wrapper/child composition with real finite child Job and disposable fake-Docker acceptance; recorded actual child-function cases above cover decisions only.'
Blocked 'whole_owner_restore_close_requires_strict_api_proof' 'Actual v3 restore/close wrapper-child composition, frozen restart policy snapshot and strict API reader evidence; recorded child-function deadline cases do not prove whole-owner lifecycle.'

$failed=@($Results | Where-Object { $_.status -ceq 'fail' }).Count
$blocked=@($Results | Where-Object { $_.status -ceq 'blocked' }).Count
Write-NewJson (Join-Path $OutputRoot 'acceptance-result.json') ([ordered]@{
    schema='mineru-same-root-independent-windows-acceptance.v1'
    status=$(if ($failed) {'fail'} elseif ($blocked) {'blocked'} else {'pass'})
    powershell_version=$PSVersionTable.PSVersion.ToString();source_sha256=$SourceHashes
    test_sha256=(Get-ExactSha256 $PSCommandPath);cases=@($Results.ToArray());failed=$failed;blocked=$blocked
    real_docker_executed=$false;production_targets_used=$false;business_load_submitted=$false
    new_maintenance_acceptance_complete=($failed -eq 0 -and $blocked -eq 0)
})
Write-Host ('RESULT ' + ($Results.Count - $failed - $blocked) + ' pass ' + $failed + ' fail ' + $blocked + ' blocked')
if ($failed) { exit 1 }
if ($blocked) { exit 2 }
exit 0
