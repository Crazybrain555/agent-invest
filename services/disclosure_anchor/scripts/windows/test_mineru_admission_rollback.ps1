<#
.SYNOPSIS
Independent functions-only deployment rollback responsibility guard regressions.
.DESCRIPTION
PS5.1 only. AST loads exactly three real installer functions. Never executes
installer top level, Docker, real services, tags, external files or other children.
IO mocks implement visible file/tag/container state and preserve operation order.
The owner runs these finite cases under its existing bounded execution wrapper.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InstallerPath,
    [Parameter(Mandatory = $true)][string]$ExpectedInstallerSha256,
    [Parameter(Mandatory = $true)][string]$OutputRoot
)
Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'Windows PowerShell 5.1 required' }
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
foreach ($protected in @([Environment]::SystemDirectory, 'C:\ProgramData', 'C:\Program Files')) {
    if ($OutputRoot.StartsWith($protected, [StringComparison]::OrdinalIgnoreCase)) { throw 'disposable user output root required' }
}
if (Test-Path -LiteralPath $OutputRoot) { throw 'output directory must be fresh' }
[void](New-Item -ItemType Directory -Path $OutputRoot)
if ($ExpectedInstallerSha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'exact lower SHA256 required' }
$beforeSha = (Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($beforeSha -cne $ExpectedInstallerSha256) { throw 'installer input identity drifted' }
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile([IO.Path]::GetFullPath($InstallerPath), [ref]$tokens, [ref]$errors)
if (@($errors).Count) { throw ($errors | Out-String) }
foreach ($name in @('Get-RollbackRegistryWitness','Assert-RollbackRegistryUnchanged','Restore-PreviousDeployment')) {
    $nodes = @($ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true))
    if ($nodes.Count -ne 1) { throw ('expected one actual function ' + $name) }
    . ([ScriptBlock]::Create($nodes[0].Extent.Text))
}
$script:Results = New-Object Collections.ArrayList
$script:Trace = New-Object Collections.ArrayList
$script:Mutations = New-Object Collections.ArrayList
function Check { param([bool]$Value, [string]$Message) if (-not $Value) { throw $Message } }
function Json { param([object]$Value) return ($Value | ConvertTo-Json -Depth 9 -Compress) }
function Copy-Value {
    param([AllowNull()][object]$Value, [int]$Depth = 0)
    # Clone this finite literal fixture graph without JSON coercing Double(-1)
    # into Int32(-1), which would erase the malformed scalar under test.
    if ($Depth -gt 8) { throw 'fixture copy depth exceeded' }
    if ($null -eq $Value) { return $null }
    if ($Value -is [pscustomobject]) {
        $copy = [ordered]@{}
        foreach ($property in $Value.PSObject.Properties) {
            $copy[$property.Name] = Copy-Value -Value $property.Value -Depth ($Depth + 1)
        }
        return [pscustomobject]$copy
    }
    if ($Value -is [array]) {
        $items = New-Object Collections.ArrayList
        foreach ($item in $Value) { [void]$items.Add((Copy-Value -Value $item -Depth ($Depth + 1))) }
        return ,$items.ToArray()
    }
    if ($Value -is [string] -or $Value -is [bool] -or $Value -is [int] -or $Value -is [long] -or $Value -is [double]) { return $Value }
    throw ('unsupported literal fixture type: ' + $Value.GetType().FullName)
}
function Read-Only { param([string]$Name) [void]$script:Trace.Add($Name) }
function Mutation { param([string]$Name) [void]$script:Trace.Add($Name); [void]$script:Mutations.Add($Name) }
function Hash-Text {
    param([string]$Value)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return ('sha256:' + (-join ($algorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value)) | ForEach-Object { $_.ToString('x2') }))) }
    finally { $algorithm.Dispose() }
}
function New-Witness {
    param([ValidateSet('v2','v3','absent')][string]$Kind = 'v2')
    # Synthetic canonical registry bytes: an actual schema discriminator changes
    # the hash. This is a mocked inspector observation, not a live output receipt.
    $registry = '{"output_root":{"device":41,"inode":9001,"mode":16832,"path":"/var/lib/mineru-api-output","uid":0},"records":[],"schema":"mineru-task-registry.' + $Kind + '","submission_watermark_bucket":-1}'
    $state = [pscustomobject]@{
        file_count = 1; total_bytes = [Text.Encoding]::UTF8.GetByteCount($registry)
        quiescence = [pscustomobject]@{
            schema = 'mineru-output-quiescence.v1'
            root_identity = [pscustomobject]@{ path = '/var/lib/mineru-api-output'; device = 41; inode = 9001; uid = 0; mode = 16832 }
            registry_sha256 = Hash-Text $registry
            record_count = 0; submission_watermark_bucket = -1
        }
    }
    if ($Kind -eq 'absent') {
        $state.file_count = 0; $state.total_bytes = 0
        $state.quiescence.registry_sha256 = $null; $state.quiescence.submission_watermark_bucket = $null
    }
    return $state
}
function Reset-Fixture {
    $script:Trace.Clear(); $script:Mutations.Clear()
    $script:DeploymentAttempted = $true
    $script:OldProjectContainers = @('mineru-api','mineru-api-proxy','mineru-openai-server')
    $script:OldRunningContainers = @($script:OldProjectContainers)
    $script:PreDeploymentOutputState = New-Witness
    $script:CurrentState = Copy-Value $script:PreDeploymentOutputState
    $script:ProbeError = $null
    $script:ComposeExisted = $true; $script:CollectorExisted = $true; $script:ReceiptExisted = $true
    $script:ComposeBackupCreated = $true; $script:CollectorBackupCreated = $true; $script:ReceiptBackupCreated = $true
    $script:ComposeTarget = 'fixture-compose'; $script:CollectorTarget = 'fixture-collector'; $script:ReceiptTarget = 'fixture-receipt'
    $script:ComposeBackup = 'backup-compose'; $script:CollectorBackup = 'backup-collector'; $script:ReceiptBackup = 'backup-receipt'
    $script:ComposeSource = 'candidate-compose'; $script:ProjectName = 'independent-fixture-project'
    $script:Files = @{'fixture-compose'='new-compose';'fixture-collector'='new-collector';'fixture-receipt'='new-receipt';
        'backup-compose'='old-compose';'backup-collector'='old-collector';'backup-receipt'='old-receipt'}
    $script:ApiCompatImage = 'fixture-api:published'; $script:OldApiCompatImageId = 'sha256:' + ('a' * 64)
    $script:CampaignApiCompatImageId = 'sha256:' + ('b' * 64)
    $script:PublishedImage = $script:CampaignApiCompatImageId; $script:RunningImage = $script:CampaignApiCompatImageId
    $script:ApiDeviceProfile = ''
    $script:ReuseCurrentPublishedImage = $false; $script:ApiOnlyCompatibilityUpgrade = $true
    $script:StableServiceEpochs = 'independent-original-epochs'
}
function Get-QuiescentOutputState {
    param([switch]$CandidateSource)
    Read-Only ('probe:candidate=' + $CandidateSource.IsPresent.ToString().ToLowerInvariant())
    if (-not $CandidateSource) { throw 'must probe through the candidate inspector, not the old reader' }
    if ($null -ne $script:ProbeError) { throw $script:ProbeError }
    return Copy-Value $script:CurrentState
}
function Test-Path { param([string]$LiteralPath) return $script:Files.ContainsKey($LiteralPath) }
function Copy-Item {
    param([string]$LiteralPath,[string]$Destination,[switch]$Force)
    if (-not $script:Files.ContainsKey($LiteralPath)) { throw 'unknown mock backup' }
    Mutation ('copy:' + $LiteralPath + '->' + $Destination)
    $script:Files[$Destination] = $script:Files[$LiteralPath]
}
function Remove-Item {
    param([string]$LiteralPath,[switch]$Force)
    Mutation ('remove:' + $LiteralPath); $script:Files.Remove($LiteralPath)
}
function Restore-ApiCompatTag { Mutation 'restore-tag'; $script:PublishedImage = $OldApiCompatImageId }
function Invoke-ApiOnlyRecreate { Mutation 'recreate-api'; $script:RunningImage = $script:PublishedImage }
function Wait-Healthy { Read-Only 'wait-healthy'; return 'fixture-healthy' }
function Get-ValidatedRuntime { Read-Only 'validate-runtime'; return 'fixture-runtime' }
function Assert-StableServiceEpochs { param([object]$Expected) Check ($Expected -ceq 'independent-original-epochs') 'epoch input changed'; Read-Only 'check-stable-epochs' }
function Remove-CompatBuildTag { Mutation 'remove-build-tag' }
function Get-OptionalImageId { param([string]$Reference) Check ($Reference -ceq $ApiCompatImage) 'unexpected tag read'; Read-Only 'inspect-tag'; return $script:PublishedImage }
function Invoke-Docker {
    param([string[]]$Arguments)
    if (($Arguments -join '|') -ceq 'network|ls|--format|{{.Name}}') {
        Read-Only 'list-networks'; return @()
    }
    if (($Arguments -join '|') -ceq 'inspect|mineru-api') {
        Read-Only 'inspect-running-api'; return ('[{"Image":"' + $script:RunningImage + '"}]')
    }
    if ($Arguments[0] -eq 'compose' -and $Arguments[1] -eq '--project-name' -and $Arguments[2] -eq $ProjectName) {
        Mutation ('docker:' + ($Arguments -join '|'))
        if ($Arguments -contains 'up') { $script:RunningImage = $script:PublishedImage }
        return ''
    }
    throw ('unexpected Docker mock operation: ' + ($Arguments -join '|'))
}
function Invoke-DockerProcess { throw 'SAFETY: real native/Docker process forbidden' }
function Invoke-NativeProcess { throw 'SAFETY: real native process forbidden' }
function Invoke-RestMethod { throw 'SAFETY: real HTTP forbidden' }
function Reject-Restore {
    param([string]$Code,[string]$OriginalMessage = '')
    $beforeFiles = Json $script:Files; $beforeTag = $script:PublishedImage; $beforeRunning = $script:RunningImage
    $errorText = $null
    try { Restore-PreviousDeployment } catch { $errorText = $_ | Out-String }
    Check ($null -ne $errorText -and $errorText.Contains($Code)) ('wrong or absent rollback refusal: ' + $errorText)
    if ($OriginalMessage) { Check ($errorText.Contains($OriginalMessage)) 'original probe error lost' }
    Check ($script:Mutations.Count -eq 0) ('rejected rollback mutated: ' + (Json $script:Mutations))
    Check ((Json $script:Files) -ceq $beforeFiles) 'rejected rollback changed files'
    Check ($script:PublishedImage -ceq $beforeTag -and $script:RunningImage -ceq $beforeRunning) 'rejected rollback changed tag or reader'
}
function Check-Restored {
    Check ($script:Files[$ComposeTarget] -ceq 'old-compose') 'compose was not restored'
    Check ($script:Files[$CollectorTarget] -ceq 'old-collector') 'collector was not restored'
    Check ($script:Files[$ReceiptTarget] -ceq 'old-receipt') 'receipt was not restored'
    Check ($script:PublishedImage -ceq $OldApiCompatImageId -and $script:RunningImage -ceq $OldApiCompatImageId) 'old image not visibly bound after recreate'
}
function Case {
    param([string]$Name,[scriptblock]$Action)
    Reset-Fixture
    try { $null = & $Action; [void]$script:Results.Add([ordered]@{name=$Name;status='pass';trace=@($script:Trace.ToArray())}); Write-Output ('PASS ' + $Name) }
    catch { [void]$script:Results.Add([ordered]@{name=$Name;status='fail';error=($_ | Out-String);trace=@($script:Trace.ToArray());mutations=@($script:Mutations.ToArray())}); Write-Output ('FAIL ' + $Name + ': ' + $_) }
}

Case 'unchanged v2 including never-submitted minus-one watermark probes before every mutation' {
    Restore-PreviousDeployment
    Check-Restored
    Check ($script:Trace[0] -ceq 'probe:candidate=true') 'guard was not first'
    Check (@($script:Trace | Where-Object { $_ -like 'probe:*' }).Count -eq 1) 'expected one fresh candidate probe'
    Check (($script:Trace -join '|') -ceq 'probe:candidate=true|copy:backup-compose->fixture-compose|copy:backup-collector->fixture-collector|copy:backup-receipt->fixture-receipt|restore-tag|recreate-api|wait-healthy|check-stable-epochs|inspect-running-api|remove-build-tag') 'actual restore order changed'
}
Case 'unchanged genuinely absent registry and numeric long witness allow rollback' {
    foreach ($kind in @('absent','v2')) {
        Reset-Fixture; $script:PreDeploymentOutputState = New-Witness $kind
        if ($kind -eq 'v2') { $script:PreDeploymentOutputState.quiescence.submission_watermark_bucket = [long]2147483648 }
        $script:CurrentState = Copy-Value $script:PreDeploymentOutputState
        Restore-PreviousDeployment; Check-Restored
    }
}
Case 'canonical comparison ignores property insertion order but preserves input objects' {
    $before = Json $script:PreDeploymentOutputState
    $a = $script:CurrentState.quiescence; $r = $a.root_identity
    $script:CurrentState = [pscustomobject]@{ total_bytes=$script:CurrentState.total_bytes; quiescence=[pscustomobject]@{
        submission_watermark_bucket=$a.submission_watermark_bucket; root_identity=[pscustomobject]@{uid=$r.uid;path=$r.path;mode=$r.mode;inode=$r.inode;device=$r.device};
        schema=$a.schema; registry_sha256=$a.registry_sha256; record_count=$a.record_count}; file_count=1 }
    $currentBefore = Json $script:CurrentState
    Restore-PreviousDeployment; Check-Restored
    Check ((Json $script:PreDeploymentOutputState) -ceq $before -and (Json $script:CurrentState) -ceq $currentBefore) 'comparison rewrote witness'
}
Case 'candidate v3 rewrite blocks old reader even when empty record counts match' {
    $script:CurrentState = New-Witness 'v3'
    Reject-Restore 'rollback_blocked_registry_changed'
    Check ($script:Trace.Count -eq 1 -and $script:Trace[0] -ceq 'probe:candidate=true') 'unexpected rejected restore activity'
}
Case 'valid root identities and retained hash bytes count or watermark drift are changed' {
    foreach ($field in @('device','inode','uid','mode')) {
        Reset-Fixture; $script:CurrentState.quiescence.root_identity.$field += 1
        Reject-Restore 'rollback_blocked_registry_changed'
    }
    foreach ($field in @('registry_sha256','record_count','submission_watermark_bucket','total_bytes')) {
        Reset-Fixture
        switch ($field) {
            'registry_sha256' { $script:CurrentState.quiescence.registry_sha256 = 'sha256:' + ('c' * 64) }
            'record_count' { $script:CurrentState.quiescence.record_count = 1 }
            'submission_watermark_bucket' { $script:CurrentState.quiescence.submission_watermark_bucket = 0 }
            'total_bytes' { $script:CurrentState.total_bytes += 1 }
        }
        Reject-Restore 'rollback_blocked_registry_changed'
    }
}
Case 'registry creation and disappearance block rollback as valid differences' {
    $script:CurrentState = New-Witness 'absent'
    Reject-Restore 'rollback_blocked_registry_changed'
    Reset-Fixture; $script:PreDeploymentOutputState = New-Witness 'absent'
    Reject-Restore 'rollback_blocked_registry_changed'
}
Case 'unreadable fresh inspection preserves original error and blocks all mutations' {
    $script:ProbeError = [IO.IOException]::new('independent unreadable registry marker')
    Reject-Restore 'rollback_blocked_registry_unverified' 'independent unreadable registry marker'
    Check ($script:Trace.Count -eq 1) 'fresh probe did not occur exactly once'
}
Case 'missing or extra witness members cannot become defaults on either side' {
    foreach ($side in @('PreDeploymentOutputState','CurrentState')) {
        foreach ($section in @('top','proof','root')) {
            Reset-Fixture
            $state = Get-Variable -Name $side -Scope Script -ValueOnly
            $object = if ($section -eq 'top') {$state} elseif ($section -eq 'proof') {$state.quiescence} else {$state.quiescence.root_identity}
            $names = @($object.PSObject.Properties.Name)
            foreach ($field in $names) {
                Reset-Fixture; $state = Get-Variable -Name $side -Scope Script -ValueOnly
                $object = if ($section -eq 'top') {$state} elseif ($section -eq 'proof') {$state.quiescence} else {$state.quiescence.root_identity}
                $object.PSObject.Properties.Remove($field)
                Reject-Restore 'rollback_blocked_registry_unverified'
            }
            Reset-Fixture; $state = Get-Variable -Name $side -Scope Script -ValueOnly
            $object = if ($section -eq 'top') {$state} elseif ($section -eq 'proof') {$state.quiescence} else {$state.quiescence.root_identity}
            $object | Add-Member -NotePropertyName extra -NotePropertyValue 0
            Reject-Restore 'rollback_blocked_registry_unverified'
        }
    }
}
Case 'schema path hash scalar and presence contradictions are unverified not changed' {
    foreach ($bad in @($false,'0',[double]0,-1)) {
        Reset-Fixture; $script:CurrentState.file_count = $bad
        Reject-Restore 'rollback_blocked_registry_unverified'
    }
    foreach ($bad in @($false,'-1',[double]-1,-2,$null)) {
        Reset-Fixture; $script:CurrentState.quiescence.submission_watermark_bucket = $bad
        Reject-Restore 'rollback_blocked_registry_unverified'
    }
    foreach ($field in @('schema','path','registry_sha256')) {
        foreach ($bad in @('unknown',0,$false,(,[object[]]@('mineru-output-quiescence.v1')),('sha256:' + ('c' * 64) + "`n"))) {
            Reset-Fixture
            if ($field -eq 'path') {$script:CurrentState.quiescence.root_identity.path=$bad}
            else {$script:CurrentState.quiescence.$field=$bad}
            Reject-Restore 'rollback_blocked_registry_unverified'
        }
    }
    Reset-Fixture; $script:CurrentState.quiescence.registry_sha256 = $null
    Reject-Restore 'rollback_blocked_registry_unverified'
    Reset-Fixture; $script:CurrentState = New-Witness 'absent'; $script:CurrentState.quiescence.submission_watermark_bucket = -1
    Reject-Restore 'rollback_blocked_registry_unverified'
}
Case 'all existing deployment restoration modes are blocked before files tags and restart' {
    foreach ($mode in @('api-upgrade','published-image-reuse','full-compose')) {
        Reset-Fixture
        $script:ApiOnlyCompatibilityUpgrade = $mode -eq 'api-upgrade'
        $script:ReuseCurrentPublishedImage = $mode -eq 'published-image-reuse'
        $script:CurrentState = New-Witness 'v3'
        Reject-Restore 'rollback_blocked_registry_changed'
    }
}
Case 'before deployment attempt skips the new guard and preserves prior rollback flow' {
    $script:DeploymentAttempted = $false; $script:PreDeploymentOutputState = $null
    $script:ProbeError = [IO.IOException]::new('must not probe before deployment attempt')
    Restore-PreviousDeployment; Check-Restored
    Check (@($script:Trace | Where-Object { $_ -like 'probe:*' }).Count -eq 0) 'before-attempt path called guard'
}
Case 'first installation has no old reader and retains existing teardown behavior' {
    $script:OldProjectContainers = @(); $script:OldRunningContainers = @()
    $script:ComposeExisted = $false; $script:CollectorExisted = $false; $script:ReceiptExisted = $false
    $script:ApiOnlyCompatibilityUpgrade = $false; $script:PreDeploymentOutputState = $null
    $script:ProbeError = [IO.IOException]::new('must not probe nonexistent old reader')
    Restore-PreviousDeployment
    Check (@($script:Trace | Where-Object { $_ -like 'probe:*' }).Count -eq 0) 'first-install path called guard'
    foreach ($path in @($ComposeTarget,$CollectorTarget,$ReceiptTarget)) { Check (-not $script:Files.ContainsKey($path)) 'first-install artifact was not removed' }
    Check (@($script:Mutations | Where-Object { $_ -like 'docker:*down*' }).Count -eq 1) 'first-install prior teardown was not preserved'
}
$afterSha = (Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()
$failures = @($script:Results | Where-Object { $_.status -eq 'fail' }).Count
$receipt = [ordered]@{ schema='independent-mineru-admission-rollback-tests.v1'; installer_sha256_before=$beforeSha;
    installer_sha256_after=$afterSha; installer_top_level_executed=$false; io_simulated=$true;
    cases=@($script:Results.ToArray()); failure_count=$failures }
[IO.File]::WriteAllText((Join-Path $OutputRoot 'receipt.json'),($receipt | ConvertTo-Json -Depth 9),(New-Object Text.UTF8Encoding($false)))
if ($afterSha -cne $beforeSha) { throw 'installer changed during suite' }
if ($failures -ne 0) { exit 1 }
if ($script:Results.Count -ne 12) { throw 'all twelve rollback families must execute' }
Write-Output 'PASS all 12 independent rollback responsibility guard families'
exit 0
