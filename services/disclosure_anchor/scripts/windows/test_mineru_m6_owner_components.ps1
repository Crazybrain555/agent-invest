param(
    [Parameter(Mandatory=$true)][string]$AssemblyPath,
    [Parameter(Mandatory=$true)][string]$ExpectedAssemblySha256,
    [Parameter(Mandatory=$true)][string]$VectorsPath,
    [Parameter(Mandatory=$true)][string]$ExpectedVectorsSha256,
    [Parameter(Mandatory=$true)][string]$OutputDirectory
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$pins=[Collections.Generic.List[IO.FileStream]]::new()
$guards=[Collections.Generic.List[IDisposable]]::new()
$checks=[Collections.Generic.List[string]]::new()
function Assert-Check([bool]$Condition,[string]$Name) {
    if(-not $Condition){throw ('M6 component check failed: '+$Name)}
    $checks.Add($Name)
}
function Find-DamageCode($ErrorRecord) {
    $errorObject=$ErrorRecord.Exception
    while($null -ne $errorObject){
        if($errorObject -is [MineruM6JournalDamage]){return $errorObject.Code}
        $errorObject=$errorObject.InnerException
    }
    return ''
}
function Pin-Input([string]$Path,[string]$Hash,[long]$Maximum) {
    if(-not [IO.Path]::IsPathRooted($Path) -or $Hash -cnotmatch '\A[0-9a-f]{64}\z'){throw 'Exact input path/hash required'}
    $file=[IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $pins.Add($file)
    if($file.Length -lt 1 -or $file.Length -gt $Maximum){throw 'Input bound exceeded'}
    $digest=[Security.Cryptography.SHA256]::Create()
    try {$actual=([BitConverter]::ToString($digest.ComputeHash($file))).Replace('-','').ToLowerInvariant()}
    finally {$digest.Dispose()}
    if($actual -cne $Hash){throw 'Component input identity drift'}
    $file.Position=0
    return $file
}
function New-TestGuard([string]$Path='') {
    $guardStream=if($Path){[IO.FileStream]::new($Path,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::Read)}else{[IO.MemoryStream]::new()}
    $guardFlush=if($Path){[Action]({$guardStream.Flush($true)}.GetNewClosure())}else{[Action]{}}
    $guard=[MineruM6WriterGuard]::new($guardStream,$guardFlush,$inputData.run_id,$inputData.spec_sha256)
    $guards.Add($guard)
    return $guard
}
try {
    if(Test-Path -LiteralPath $OutputDirectory){throw 'Test output directory must be new'}
    $null=New-Item -ItemType Directory -Path $OutputDirectory
    $assembly=Pin-Input $AssemblyPath $ExpectedAssemblySha256 1048576
    $vectors=Pin-Input $VectorsPath $ExpectedVectorsSha256 262144
    $null=[Reflection.Assembly]::LoadFile($AssemblyPath)
    $reader=[IO.StreamReader]::new($vectors,[Text.UTF8Encoding]::new($false,$true),$false,4096,$true)
    try {$inputData=$reader.ReadToEnd() | ConvertFrom-Json} finally {$reader.Dispose()}
    foreach($raw in $inputData.requests){Assert-Check ([MineruM6OwnerWire]::Request($raw) -ceq $raw) 'python_request_exact_canonical_parity'}
    foreach($raw in $inputData.anchors){Assert-Check ([MineruM6OwnerWire]::Anchor([MineruResidentWire]::Parse($raw,65536)) -ceq $raw) 'python_anchor_canonical_parity'}
    foreach($raw in $inputData.resources){Assert-Check ([MineruM6OwnerWire]::Resources([MineruResidentWire]::Parse($raw,65536)) -ceq $raw) 'python_resource_canonical_parity'}
    foreach($raw in $inputData.edge_producers){Assert-Check ([MineruM6OwnerWire]::Producer([MineruResidentWire]::Parse($raw,65536)) -ceq $raw) 'unicode_int64_and_escaped_string_parity'}
    foreach($raw in @($inputData.records)+@($inputData.extra_records)){
        Assert-Check ([MineruM6OwnerWire]::Record($raw) -ceq $raw) 'python_event_exact_canonical_parity'
    }
    $badRequests=@((' '+$inputData.requests[0]),($inputData.requests[0]+' '),($inputData.requests[0].Substring(0,$inputData.requests[0].Length-1)+',"run_id":"duplicate"}'))
    Assert-Check ($badRequests.Count -eq 3) 'three_distinct_malformed_request_inputs'
    foreach($raw in $badRequests){
        $rejected=$false
        try {$null=[MineruM6OwnerWire]::Request($raw)} catch {$rejected=$true}
        Assert-Check $rejected 'noncanonical_or_duplicate_request_rejected'
    }
    $path=Join-Path $OutputDirectory 'actual-file-journal.jsonl'
    $guardPath=Join-Path $OutputDirectory 'writer-guard.json'
    $stream=[IO.FileStream]::new($path,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::Read)
    $tracker=[pscustomobject]@{count=0}
    $flush=[Action]({$stream.Flush($true);$tracker.count++}.GetNewClosure())
    $guard=New-TestGuard $guardPath
    $journal=[MineruM6Journal]::new($stream,$flush,16384,1000,1000000,$inputData.run_id,$inputData.spec_sha256,$inputData.boot_sha256,$guard)
    try {
        foreach($raw in $inputData.records){
            $parsed=[MineruResidentWire]::Parse($raw,65536)
            $result=$journal.Append($parsed.Get('event').Raw,$parsed.Get('stamp').Get('received_qpc_ticks').Integer(),$inputData.owner_epoch_sha256)
            Assert-Check ($result.Record -ceq $raw -and -not $result.Duplicate -and -not $result.Conflict) 'actual_file_append_matches_python_stamp'
        }
        Assert-Check ($tracker.count -eq $inputData.records.Count) 'flush_before_each_ack'
        $prior=[MineruResidentWire]::Parse($inputData.records[2],65536)
        $bytesBefore=$stream.Length
        $retry=$journal.Append($prior.Get('event').Raw,39999,$inputData.owner_epoch_sha256)
        Assert-Check ($retry.Duplicate -and $retry.Record -ceq $inputData.records[2] -and $stream.Length -eq $bytesBefore) 'closed_retry_returns_original_stamp_without_write'
        $rejected=$false
        try {$null=$journal.Append($inputData.conflict_event,39999,$inputData.owner_epoch_sha256)}catch{$rejected=$true}
        Assert-Check ($rejected -and $stream.Length -eq $bytesBefore) 'new_stamp_after_close_rejected'
    } finally {$journal.Dispose();$guard.Dispose()}
    $original=[IO.File]::ReadAllBytes($path)
    $stream=[IO.FileStream]::new($path,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::Read)
    $guard=New-TestGuard $guardPath
    $journal=[MineruM6Journal]::new($stream,[Action]({$stream.Flush($true)}.GetNewClosure()),16384,1000,1000000,$inputData.run_id,$inputData.spec_sha256,$inputData.boot_sha256,$guard)
    try {
        $retry=$journal.Append($prior.Get('event').Raw,39999,$inputData.owner_epoch_sha256)
        Assert-Check ($journal.IsClosed -and $retry.Record -ceq $inputData.records[2]) 'recovery_rebuilds_closed_retry_index'
    } finally {$journal.Dispose();$guard.Dispose()}
    Assert-Check ([Convert]::ToBase64String([IO.File]::ReadAllBytes($path)) -ceq [Convert]::ToBase64String($original)) 'clean_recovery_preserves_original_file_bytes'
    $damaged=[byte[]]::new($original.Length+1)
    [Array]::Copy($original,$damaged,$original.Length);$damaged[$original.Length]=123
    $stream=[IO.MemoryStream]::new($damaged,$true)
    $guard=New-TestGuard
    $rejected=$false;$damageCode=''
    try {$null=[MineruM6Journal]::new($stream,[Action]{},16384,1000,1000000,$inputData.run_id,$inputData.spec_sha256,$inputData.boot_sha256,$guard)}
    catch {$rejected=$true;$damageCode=Find-DamageCode $_}
    Assert-Check ($rejected -and $damageCode -ceq 'event_log_truncated' -and [Convert]::ToBase64String($stream.ToArray()) -ceq [Convert]::ToBase64String($damaged)) 'partial_tail_retained_without_repair'
    $stream.Dispose()
    $stream=[IO.MemoryStream]::new()
    $guardStream=[IO.MemoryStream]::new()
    $guard=[MineruM6WriterGuard]::new($guardStream,[Action]{},$inputData.run_id,$inputData.spec_sha256)
    $guards.Add($guard)
    $journal=[MineruM6Journal]::new($stream,[Action]{throw [IO.IOException]::new('injected durable flush failure')},16384,1000,1000000,$inputData.run_id,$inputData.spec_sha256,$inputData.boot_sha256,$guard)
    try {
        $first=[MineruResidentWire]::Parse($inputData.records[0],65536)
        $rejected=$false
        try {$null=$journal.Append($first.Get('event').Raw,100,$inputData.owner_epoch_sha256)}catch{$rejected=$true}
        $retained=[Convert]::ToBase64String($stream.ToArray())
        Assert-Check ($rejected -and $stream.Length -gt 0 -and $journal.LastSequence -eq 0) 'failed_flush_no_ack_or_index_credit'
        $rejected=$false
        try {$null=$journal.Append($first.Get('event').Raw,100,$inputData.owner_epoch_sha256)}catch{$rejected=$true}
        Assert-Check ($rejected -and [Convert]::ToBase64String($stream.ToArray()) -ceq $retained) 'uncertain_writer_cannot_retry_or_repair_in_place'
        $uncertainJournal=$stream.ToArray();$uncertainGuard=$guardStream.ToArray()
    } finally {$journal.Dispose()}
    $recoveryStream=[IO.MemoryStream]::new($uncertainJournal,$true)
    $guardStream=[IO.MemoryStream]::new($uncertainGuard,$true)
    $guard=[MineruM6WriterGuard]::new($guardStream,[Action]{},$inputData.run_id,$inputData.spec_sha256)
    $guards.Add($guard)
    $rejected=$false
    try {$null=[MineruM6Journal]::new($recoveryStream,[Action]{},16384,1000,1000000,$inputData.run_id,$inputData.spec_sha256,$inputData.boot_sha256,$guard)}catch{$rejected=$true}
    Assert-Check ($rejected -and [Convert]::ToBase64String($recoveryStream.ToArray()) -ceq [Convert]::ToBase64String($uncertainJournal)) 'complete_lf_with_dirty_guard_cannot_resume_as_clean'
    $recoveryStream.Dispose()
    $stream=[IO.MemoryStream]::new()
    $guard=New-TestGuard
    $journal=[MineruM6Journal]::new($stream,[Action]{},16384,1000,1000000,$inputData.run_id,$inputData.spec_sha256,$inputData.boot_sha256,$guard)
    try {
        foreach($raw in $inputData.records[0..2]){
            $parsed=[MineruResidentWire]::Parse($raw,65536)
            $null=$journal.Append($parsed.Get('event').Raw,$parsed.Get('stamp').Get('received_qpc_ticks').Integer(),$inputData.owner_epoch_sha256)
        }
        $conflict=$journal.Append($inputData.conflict_event,400,$inputData.owner_epoch_sha256)
        $bytesBefore=$stream.Length
        $retry=$journal.Append($inputData.conflict_event,401,$inputData.owner_epoch_sha256)
        Assert-Check ($conflict.Conflict -and -not $conflict.Duplicate -and $journal.HasConflicts -and $retry.Duplicate -and $retry.Conflict -and $conflict.Record -ceq $retry.Record -and $stream.Length -eq $bytesBefore) 'conflicting_variant_retained_and_exact_retry_not_restamped'
    } finally {$journal.Dispose()}
    # Pure append validation must leave both durable guard and journal unchanged.
    $stream=[IO.MemoryStream]::new();$guardStream=[IO.MemoryStream]::new()
    $guard=[MineruM6WriterGuard]::new($guardStream,[Action]{},$inputData.run_id,$inputData.spec_sha256)
    $guards.Add($guard)
    $journal=[MineruM6Journal]::new($stream,[Action]{},16384,1000,1000000,$inputData.run_id,$inputData.spec_sha256,$inputData.boot_sha256,$guard)
    try {
        foreach($raw in $inputData.records[0..2]){
            $parsed=[MineruResidentWire]::Parse($raw,65536)
            $null=$journal.Append($parsed.Get('event').Raw,$parsed.Get('stamp').Get('received_qpc_ticks').Integer(),$inputData.owner_epoch_sha256)
        }
        $next=[MineruResidentWire]::Parse($inputData.records[3],65536).Get('event').Raw
        $cases=@(
            @{raw=$next;tick=150;epoch=$inputData.owner_epoch_sha256;code='physical_owner_order_invalid'},
            @{raw=$next.Replace($inputData.run_id,'another-run');tick=210;epoch=$inputData.owner_epoch_sha256;code='run_spec_or_boot_changed'},
            @{raw=$next;tick=210;epoch='';code='FormatException'}
        )
        foreach($case in $cases){
            $before=[Convert]::ToBase64String($stream.ToArray());$guardBefore=[Convert]::ToBase64String($guardStream.ToArray())
            $actual=''
            try {$null=$journal.Append($case.raw,$case.tick,$case.epoch)} catch {
                $actual=Find-DamageCode $_
                if(-not $actual){$e=$_.Exception;while($null -ne $e){if($e -is [FormatException]){$actual='FormatException';break};$e=$e.InnerException}}
            }
            Assert-Check ($actual -ceq $case.code -and $journal.LastSequence -eq 3 -and
                [Convert]::ToBase64String($stream.ToArray()) -ceq $before -and
                [Convert]::ToBase64String($guardStream.ToArray()) -ceq $guardBefore) ('invalid_append_rejected_before_durability_'+$case.code)
        }
        $null=$journal.Append($next,210,$inputData.owner_epoch_sha256)
        Assert-Check ($journal.LastSequence -eq 4) 'invalid_input_does_not_poison_valid_journal'
    } finally {$journal.Dispose()}
    foreach($stage in @('prepare','committed')){
        $stream=[IO.MemoryStream]::new();$guardStream=[IO.MemoryStream]::new();$calls=[pscustomobject]@{n=0}
        $failAt=if($stage -ceq 'prepare'){2}else{3}
        $guardFlush=[Action]({$calls.n++;if($calls.n -eq $failAt){throw [IO.IOException]::new('injected guard flush failure')}}.GetNewClosure())
        $guard=[MineruM6WriterGuard]::new($guardStream,$guardFlush,$inputData.run_id,$inputData.spec_sha256);$guards.Add($guard)
        $journal=[MineruM6Journal]::new($stream,[Action]{},16384,1000,1000000,$inputData.run_id,$inputData.spec_sha256,$inputData.boot_sha256,$guard)
        try {
            $first=[MineruResidentWire]::Parse($inputData.records[0],65536).Get('event').Raw
            $failed=$false;try{$null=$journal.Append($first,100,$inputData.owner_epoch_sha256)}catch{$failed=$true}
            Assert-Check ($failed -and $calls.n -eq $failAt -and $journal.LastSequence -eq 0 -and
                (($stage -ceq 'prepare' -and $stream.Length -eq 0) -or ($stage -ceq 'committed' -and $stream.Length -gt 0))) ('guard_'+$stage+'_flush_failure_no_ack')
            $before=[Convert]::ToBase64String($stream.ToArray())
            $failed=$false;try{$null=$journal.Append($first,100,$inputData.owner_epoch_sha256)}catch{$failed=$true}
            Assert-Check ($failed -and [Convert]::ToBase64String($stream.ToArray()) -ceq $before) ('guard_'+$stage+'_failure_current_writer_poisoned')
        }finally{$journal.Dispose()}
    }
    $stream=[IO.MemoryStream]::new();$guard=New-TestGuard
    $journal=[MineruM6Journal]::new($stream,[Action]{},16384,1000,1000000,$inputData.run_id,$inputData.spec_sha256,$inputData.boot_sha256,$guard)
    try {
        foreach($raw in $inputData.resume_records){
            $parsed=[MineruResidentWire]::Parse($raw,65536);$stamp=$parsed.Get('stamp')
            $actual=$journal.Append($parsed.Get('event').Raw,$stamp.Get('received_qpc_ticks').Integer(),$stamp.Get('owner_process_epoch_sha256').String())
            Assert-Check ($actual.Record -ceq $raw) 'same_boot_resume_exact_original_interval_and_epoch_chain'
        }
    }finally{$journal.Dispose()}
    foreach($limitCase in @('record','events')){
        $stream=[IO.MemoryStream]::new();$guard=New-TestGuard
        $recordLimit=if($limitCase -ceq 'record'){512}else{16384}
        $eventLimit=if($limitCase -ceq 'events'){1}else{1000}
        $journal=[MineruM6Journal]::new($stream,[Action]{},$recordLimit,$eventLimit,1000000,$inputData.run_id,$inputData.spec_sha256,$inputData.boot_sha256,$guard)
        try {
            $first=[MineruResidentWire]::Parse($inputData.records[0],65536).Get('event').Raw
            if($limitCase -ceq 'events'){$null=$journal.Append($first,100,$inputData.owner_epoch_sha256);$first=[MineruResidentWire]::Parse($inputData.records[1],65536).Get('event').Raw}
            $before=[Convert]::ToBase64String($stream.ToArray());$code=''
            try{$null=$journal.Append($first,101,$inputData.owner_epoch_sha256)}catch{
                $e=$_.Exception;while($null -ne $e){if($e -is [MineruM6JournalBound]){$code=$e.Code;break};$e=$e.InnerException}
            }
            $expected=if($limitCase -ceq 'record'){'producer_record_over_bound'}else{'event_log_bound_exhausted'}
            Assert-Check ($code -ceq $expected -and [Convert]::ToBase64String($stream.ToArray()) -ceq $before) ('distinct_bound_before_write_'+$limitCase)
        }finally{$journal.Dispose()}
    }
    $result=[pscustomobject]@{contract_version='m6.owner-component-checks.v1'; checks=$checks.ToArray(); status='pass'; scope='wire_and_journal_mechanisms_only'; physical_clock_and_process_gate=$false; business_runtime_changed=$false}
    $result | ConvertTo-Json -Depth 5 -Compress
} finally {foreach($guard in $guards){$guard.Dispose()};foreach($file in $pins){$file.Dispose()}}
