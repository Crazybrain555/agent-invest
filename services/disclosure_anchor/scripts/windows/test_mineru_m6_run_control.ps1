param(
    [Parameter(Mandatory=$true)][string]$ComponentsPath,
    [Parameter(Mandatory=$true)][string]$ComponentsSha256,
    [Parameter(Mandatory=$true)][string]$ControlPath,
    [Parameter(Mandatory=$true)][string]$ControlSha256,
    [Parameter(Mandatory=$true)][string]$VectorsPath,
    [Parameter(Mandatory=$true)][string]$VectorsSha256,
    [Parameter(Mandatory=$true)][string]$OutputDirectory
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$pins=[Collections.Generic.List[IO.FileStream]]::new()
$checks=[Collections.Generic.List[string]]::new()
$responses=[Collections.Generic.List[string]]::new()
function Assert-Check([bool]$Good,[string]$Name){if(-not $Good){throw ('M6 control check failed: '+$Name)};$checks.Add($Name)}
function Pin([string]$Path,[string]$Sha){
    if(-not [IO.Path]::IsPathRooted($Path) -or $Sha -cnotmatch '\A[0-9a-f]{64}\z'){throw 'Exact test input required'}
    $file=[IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read);$pins.Add($file)
    $hash=[Security.Cryptography.SHA256]::Create()
    try{$actual=([BitConverter]::ToString($hash.ComputeHash($file))).Replace('-','').ToLowerInvariant()}finally{$hash.Dispose()}
    if($actual -cne $Sha){throw 'Control test input drift'}
}
function Call-Control([string]$Key,[string]$Role){
    $request=$case.requests.PSObject.Properties[$Key].Value
    $raw=$control.Handle($request,$Role,$roles[$Role]);$responses.Add($raw)
    return ($raw|ConvertFrom-Json)
}
try {
    if(Test-Path -LiteralPath $OutputDirectory){throw 'New control test directory required'}
    $null=New-Item -ItemType Directory -Path $OutputDirectory
    Pin $ComponentsPath $ComponentsSha256;Pin $ControlPath $ControlSha256;Pin $VectorsPath $VectorsSha256
    $null=[Reflection.Assembly]::LoadFile($ComponentsPath);$null=[Reflection.Assembly]::LoadFile($ControlPath)
    $inputData=Get-Content -LiteralPath $VectorsPath -Raw|ConvertFrom-Json
    foreach($case in $inputData.cases){
        $dir=Join-Path $OutputDirectory $case.mode;$null=New-Item -ItemType Directory -Path $dir
        $roles=[Collections.Generic.Dictionary[string,string]]::new([StringComparer]::Ordinal)
        foreach($p in $case.roles.PSObject.Properties){$roles.Add($p.Name,$p.Value)}
        $receipts=[Collections.Generic.Dictionary[string,string]]::new([StringComparer]::Ordinal)
        foreach($p in $case.receipts.PSObject.Properties){$receipts.Add($p.Name,$p.Value)}
        $state=[pscustomobject]@{ticks=[long]100;nativeClean=$false;nativeChecks=0}
        $read=[Func[string,string]]({param($sha);return $receipts[$sha]}.GetNewClosure())
        $save=[Action[string,string]]({param($kind,$raw)
            $path=Join-Path $dir ($kind+'.json')
            if(Test-Path -LiteralPath $path){if([IO.File]::ReadAllText($path) -cne $raw){throw 'Immutable control sidecar changed'}}else{
                $bytes=[Text.UTF8Encoding]::new($false,$true).GetBytes($raw)
                $file=[IO.FileStream]::new($path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
                try{$file.Write($bytes,0,$bytes.Length);$file.Flush($true)}finally{$file.Dispose()}
            }
        }.GetNewClosure())
        $load=[Func[string,string]]({param($kind);$path=Join-Path $dir ($kind+'.json');if(Test-Path -LiteralPath $path){return [IO.File]::ReadAllText($path)};return $null}.GetNewClosure())
        $native=[Action]({$state.nativeChecks++;if(-not $state.nativeClean){throw [MineruM6ControlRefusal]::new('native_resources_pending')}}.GetNewClosure())
        $clock=[Func[long]]({return $state.ticks}.GetNewClosure())
        $stream=[IO.FileStream]::new((Join-Path $dir 'events.jsonl'),[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
        $gs=[IO.FileStream]::new((Join-Path $dir 'writer-guard.json'),[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
        $guard=[MineruM6WriterGuard]::new($gs,[Action]({$gs.Flush($true)}.GetNewClosure()),$case.run_id,$case.spec_sha256)
        $journal=[MineruM6Journal]::new($stream,[Action]({$stream.Flush($true)}.GetNewClosure()),16384,1000,1000000,$case.run_id,$case.spec_sha256,$case.boot,$guard)
        try {
            $control=[MineruM6RunControl]::new($case.anchor,$case.spec_sha256,$case.owner_epoch,$case.mode,$roles,$journal,$clock,4,6,$read,$save,$load,$native)
            Assert-Check ($journal.LastSequence -eq 1 -and $journal.LastTick -eq 100) 'bound_run_start_has_original_t0'
            $reply=Call-Control 'lease' $case.runner
            Assert-Check ($reply.status.state -ceq 'bound' -and $null -eq $reply.status.admission_valid_until_ticks) 'bound_owner_grants_no_lease'
            $before=$journal.LastSequence;$reply=Call-Control 'open' 'quality_verifier'
            Assert-Check ($reply.outcome -ceq 'rejected' -and $journal.LastSequence -eq $before) 'wrong_role_cannot_open'
            $state.ticks=101;$reply=Call-Control 'open' 'controller'
            Assert-Check ($reply.outcome -ceq 'ok' -and $reply.status.state -ceq 'open') 'controller_opens_once'
            $reply=Call-Control 'lease' $case.runner
            Assert-Check ($reply.status.admission_valid_until_ticks -eq 105) 'lease_reserves_stop_budget'
            $state.ticks=200;$reply=Call-Control 'attempt_admitted' $case.runner;$original=$reply.record
            Assert-Check ($reply.outcome -ceq 'ok') 'durable_claim_observation_accepted'
            $before=$journal.LastSequence;$reply=Call-Control 'close' 'controller'
            Assert-Check ($reply.error_code -ceq 'business_drain_pending' -and $journal.LastSequence -eq $before) 'early_close_does_not_claim_resources_closed'
            $state.ticks=220;$reply=Call-Control 'stop' $case.runner
            Assert-Check ($reply.status.state -ceq 'stopping') 'stop_request_is_not_actual_stop'
            $before=$journal.LastSequence;$reply=Call-Control 'bad_ack' $case.runner
            Assert-Check ($reply.error_code -ceq 'admission_receipt_set_differs' -and $journal.LastSequence -eq $before) 'wrong_claim_set_cannot_ack_stop'
            $state.ticks=221;$reply=Call-Control 'ack' $case.runner
            Assert-Check ($reply.outcome -ceq 'ok' -and $reply.status.state -ceq 'draining') 'validated_ack_stamps_actual_stop'
            $drainRole=if($case.mode -ceq 'service_diagnostic'){'quality_verifier'}else{'public_verifier'}
            $before=$journal.LastSequence;$reply=Call-Control 'verifier_drained' $drainRole
            Assert-Check ($reply.error_code -ceq 'verifier_drain_pending' -and $journal.LastSequence -eq $before) 'verifier_cannot_drain_before_attempt_final'
            $state.ticks=230;$reply=Call-Control 'remote_accepted' $case.runner
            Assert-Check ($reply.outcome -ceq 'ok') 'retained_remote_identity_after_stop'
            $state.ticks=240
            if($case.mode -ceq 'service_diagnostic'){$reply=Call-Control 'service_validated' $case.runner}else{
                $reply=Call-Control 'publication_committed' $case.runner
                $state.ticks=245;$reply=Call-Control 'public_confirmation' 'public_verifier'
            }
            Assert-Check ($reply.outcome -ceq 'ok') 'mode_specific_result_observation'
            $state.ticks=250;$reply=Call-Control 'document_qualified' 'quality_verifier'
            Assert-Check ($reply.outcome -ceq 'ok') 'independent_quality_observation'
            $state.ticks=260;$reply=Call-Control 'attempt_final' $case.runner
            Assert-Check ($reply.outcome -ceq 'ok') 'attempt_final_after_remote_cleanup'
            $reply=Call-Control 'ack' $case.runner
            Assert-Check ($reply.outcome -ceq 'ok') 'original_admission_ack_retry_after_producer_progress'
            $state.ticks=270;$reply=Call-Control 'verifier_drained' $drainRole
            Assert-Check ($reply.outcome -ceq 'ok') 'verifier_drains_after_all_attempts_final'
            # Same-boot process reconstruction keeps the original journal and
            # reads durable ACK sidecars; no fresh T0/admission reset is allowed.
            $journal.Dispose();$guard.Dispose()
            $stream=[IO.FileStream]::new((Join-Path $dir 'events.jsonl'),[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
            $gs=[IO.FileStream]::new((Join-Path $dir 'writer-guard.json'),[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
            $guard=[MineruM6WriterGuard]::new($gs,[Action]({$gs.Flush($true)}.GetNewClosure()),$case.run_id,$case.spec_sha256)
            $journal=[MineruM6Journal]::new($stream,[Action]({$stream.Flush($true)}.GetNewClosure()),16384,1000,1000000,$case.run_id,$case.spec_sha256,$case.boot,$guard)
            $state.ticks=280;$before=$stream.Length;$missing=$false
            try{$null=[MineruM6RunControl]::new($case.anchor,$case.spec_sha256,$case.resume_epoch,$case.mode,$roles,$journal,$clock,4,6,$read,$save,[Func[string,string]]{param($kind);return $null},$native)}catch{
                if(-not $_.Exception.ToString().Contains('archived_control_receipt_missing')){throw};$missing=$true
            }
            Assert-Check ($missing -and $stream.Length -eq $before) 'recovery_requires_archived_stop_ack_before_new_stamp'
            $control=[MineruM6RunControl]::new($case.anchor,$case.spec_sha256,$case.resume_epoch,$case.mode,$roles,$journal,$clock,4,6,$read,$save,$load,$native)
            $reply=Call-Control 'lease' $case.runner
            Assert-Check ($reply.status.state -ceq 'draining' -and $null -eq $reply.status.admission_valid_until_ticks -and
                $reply.status.owner_process_epoch_sha256 -ceq $case.resume_epoch) 'same_boot_recovery_preserves_stop_and_new_owner_epoch'
            $before=$journal.LastSequence;$reply=Call-Control 'ack' $case.runner
            Assert-Check ($reply.outcome -ceq 'ok' -and $journal.LastSequence -eq $before) 'recovered_original_ack_retry_after_finalization_is_read_only'
            $before=$journal.LastSequence;$nativeFailure=$false
            try {$reply=Call-Control 'close' 'controller'} catch {
                # A PowerShell delegate wraps its injected exception before it
                # crosses the C# boundary. The production caller must likewise
                # surface unknown native closure failures, never claim closure.
                if(-not $_.Exception.ToString().Contains('native_resources_pending')){throw}
                $nativeFailure=$true
            }
            Assert-Check ($nativeFailure -and $journal.LastSequence -eq $before) 'native_closure_failure_propagates_without_closing_journal'
            $state.nativeClean=$true;$state.ticks=300;$reply=Call-Control 'close' 'controller'
            Assert-Check ($reply.status.state -ceq 'closed' -and $state.nativeChecks -eq 2 -and $journal.IsClosed) 'actual_native_closure_before_tclose'
            $before=$stream.Length;$reply=Call-Control 'attempt_admitted' $case.runner
            Assert-Check ($reply.record.stamp.sequence -eq $original.stamp.sequence -and $reply.record.stamp.received_qpc_ticks -eq 200 -and $stream.Length -eq $before) 'closed_exact_retry_keeps_original_stamp'
            $reply=Call-Control 'open' 'controller'
            Assert-Check ($reply.outcome -ceq 'rejected' -and $stream.Length -eq $before) 'closed_owner_never_reopens'
        }finally{$journal.Dispose();$guard.Dispose()}
    }
    $responseJson=ConvertTo-Json -InputObject $responses.ToArray() -Depth 4 -Compress
    [IO.File]::WriteAllText((Join-Path $OutputDirectory 'responses.json'),$responseJson,[Text.UTF8Encoding]::new($false,$true))
    [ordered]@{status='pass';checks=$checks.ToArray();count=$checks.Count;scope='synthetic_control_and_real_filestream_only';
        components_sha256=$ComponentsSha256;control_sha256=$ControlSha256;vectors_sha256=$VectorsSha256}|ConvertTo-Json -Depth 5 -Compress
}finally{foreach($pin in $pins){$pin.Dispose()}}
