param(
    [Parameter(Mandatory = $true)]
    [string]$ExporterPath
)

# D7: the bounded phase ring, driven on the real Windows host with controlled stubs.
#
# The ring helpers are taken out of the real exporter source and driven directly - no
# session, no Linux container, no API, no vLLM, no GPU, no PDF, and no deadline of its own
# beyond the few hundred milliseconds each case spends. What this decides is the one thing
# the next G4 needs from the probe: the phase that consumed the shared sampling budget is
# named correctly, a phase that never started is not reported as one that failed, and the
# tail is written exactly once without ever replacing the original throw.
#
# Run on the production Windows telemetry host (Windows PowerShell 5.1), from the staged
# service tree:
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests\windows\test_mineru_sampling_phase_ring.ps1 `
#       -ExporterPath scripts\windows\mineru_resident_telemetry_exporter.ps1
# Exit 0 with a single JSON line of status=pass is the pass condition; any throw is a fail.
#
# Two helpers are stubbed rather than extracted: New-MineruJson and Quote-MineruJson are
# one-line delegates to the compiled [MineruResidentWire], which is not loaded here. The
# tail's field names, order and values still come from the exporter's own serializers; only
# the string escaping is the stub's. Escaping has its own coverage on the wire type.

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$PhaseOrder = @("linux_sample", "api_health", "api_http", "vllm_metrics", "queue_projection")
$RingCapacity = 16
$MaximumTailBytes = 64 * 1024

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

# --- the real ring, taken from the real exporter -------------------------------------------
$source = [IO.File]::ReadAllText($ExporterPath, [Text.Encoding]::UTF8)
$startMarker = '$script:phaseRingCapacity ='
$endMarker = 'function Write-MineruPhaseTail'
$startIndex = $source.IndexOf($startMarker, [StringComparison]::Ordinal)
$tailIndex = $source.IndexOf($endMarker, [StringComparison]::Ordinal)
if ($startIndex -lt 0 -or $tailIndex -le $startIndex) {
    throw "the sampling phase ring is not in $ExporterPath; this harness needs the B3 helpers"
}
# The tail writer ends at the first closing brace in column zero after its own header.
$closeIndex = $source.IndexOf("`n}", $tailIndex, [StringComparison]::Ordinal)
if ($closeIndex -lt 0) { throw "Write-MineruPhaseTail has no closing brace" }
$ring = $source.Substring($startIndex, $closeIndex + 2 - $startIndex)
foreach ($required in @("New-MineruPhaseCall", "Enter-MineruPhase", "Exit-MineruPhase",
                        "Trace-MineruPhaseFailure", "Complete-MineruPhaseCall",
                        "ConvertTo-MineruPhaseCallJson", "Write-MineruPhaseTail")) {
    Assert-True ($ring.IndexOf("function " + $required, [StringComparison]::Ordinal) -ge 0) `
        ("the extracted ring is missing " + $required)
}

# Collaborators the extracted block reads, stubbed so nothing is written outside this run.
function Quote-MineruJson([string]$Value) {
    return '"' + $Value.Replace('\', '\\').Replace('"', '\"') + '"'
}
function New-MineruJson([string[]]$Pairs) {
    $parts = [Collections.Generic.List[string]]::new()
    for ($index = 0; $index -lt $Pairs.Length; $index += 2) {
        $parts.Add((Quote-MineruJson $Pairs[$index]) + ':' + $Pairs[$index + 1])
    }
    return '{' + ($parts -join ',') + '}'
}
$script:written = [Collections.Generic.List[object]]::new()
$script:artifactThrows = $false
function Write-MineruSessionArtifact([string]$Name, [string]$Json) {
    if ($script:artifactThrows) { throw "stub artifact write failure" }
    $script:written.Add([pscustomobject]@{ Name = $Name; Json = $Json })
}
$state = [pscustomobject]@{ Session = "0123456789abcdef0123456789abcdef"; Lane = "host_slow" }
$failures = [Collections.Generic.List[Exception]]::new()
$runDirectory = [IO.Path]::GetTempPath()

Invoke-Expression $ring

$frequency = [Diagnostics.Stopwatch]::Frequency

function New-Budget([int]$Milliseconds) {
    return [Diagnostics.Stopwatch]::GetTimestamp() + [long]($frequency * $Milliseconds / 1000)
}

function Get-CallJson($Call) {
    return ConvertFrom-Json (ConvertTo-MineruPhaseCallJson $Call)
}

function Get-PhaseRecord($Document, [string]$Name) {
    foreach ($phase in @($Document.phases)) {
        if ($phase.phase -ceq $Name) { return $phase }
    }
    throw ("the call does not record the phase " + $Name)
}

# --- 1. the Linux sample spends the whole budget -------------------------------------------
# api_health must not be reported as a failure of its own: it started with nothing left.
$deadline = New-Budget 150
$call = New-MineruPhaseCall $deadline $deadline $PhaseOrder
Enter-MineruPhase $call 0
Start-Sleep -Milliseconds 400
Exit-MineruPhase $call 0
Enter-MineruPhase $call 1
try { throw "stub health failure" } catch { Trace-MineruPhaseFailure $call 1 $_ }
$exhausted = Get-CallJson $call
$linux = Get-PhaseRecord $exhausted "linux_sample"
$health = Get-PhaseRecord $exhausted "api_health"
Assert-True ($linux.outcome -ceq "ok") "the Linux phase was not recorded as the phase that ran"
Assert-True ([long]$linux.remaining_at_enter_ticks -gt 0) "the Linux phase was entered with no budget at all"
Assert-True ([long]$health.remaining_at_enter_ticks -eq 0) "the spent budget was not visible to the next phase"
Assert-True ($exhausted.deadline_exhausted_before_enter -eq $true) "a spent deadline was not reported as one"
Assert-True ($exhausted.operation_failed_with_budget_remaining -eq $false) `
    "a spent deadline was also reported as a failure with budget left"
foreach ($name in @("api_http", "vllm_metrics", "queue_projection")) {
    $later = Get-PhaseRecord $exhausted $name
    Assert-True ($later.outcome -ceq "not_entered") ("a phase that never started was not " + $name)
    Assert-True ($null -eq $later.entered_ticks) "a phase that never started reported an entry instant"
    Assert-True ($null -eq $later.remaining_at_enter_ticks) "a phase that never started reported a budget"
}

# --- 2. a phase that fails with budget left is not a deadline -------------------------------
$call = New-MineruPhaseCall (New-Budget 5000) (New-Budget 5000) $PhaseOrder
for ($index = 0; $index -lt 3; $index++) {
    Enter-MineruPhase $call $index
    Exit-MineruPhase $call $index
}
Enter-MineruPhase $call 3
try { $null = [int]::Parse("not-a-number") } catch { Trace-MineruPhaseFailure $call 3 $_ }
$failing = Get-CallJson $call
$metrics = Get-PhaseRecord $failing "vllm_metrics"
Assert-True ($metrics.outcome -ceq "failed") "the failing phase was not named as failed"
Assert-True ([long]$metrics.remaining_at_enter_ticks -gt 0) "a failure inside the budget was called exhaustion"
Assert-True ($failing.operation_failed_with_budget_remaining -eq $true) "a failure with budget left was not reported"
Assert-True ($failing.deadline_exhausted_before_enter -eq $false) "a failure with budget left was called a deadline"
# A .NET method throw arrives wrapped; the operative type is the inner one.
Assert-True ($metrics.exception_type -ceq "System.FormatException") `
    ("the failing phase kept the wrong exception identity: " + $metrics.exception_type)
$health = Get-PhaseRecord $failing "api_health"
Assert-True ($health.outcome -ceq "ok") "a healthy phase was disturbed by a later failure"

# --- 3. one deadline per call, and the budget is that deadline ------------------------------
foreach ($document in @($exhausted, $failing)) {
    Assert-True ([long]$document.qpc_frequency -eq $frequency) "the call records a frequency the host does not have"
    foreach ($phase in @($document.phases)) {
        Assert-True ($PhaseOrder -contains [string]$phase.phase) ("unknown phase name: " + $phase.phase)
        if ($null -ne $phase.entered_ticks) {
            $remaining = [long]$document.deadline_ticks - [long]$phase.entered_ticks
            if ($remaining -lt 0) { $remaining = 0 }
            Assert-True ([long]$phase.remaining_at_enter_ticks -eq $remaining) `
                "the reported budget is not the shared deadline minus the entry, so a second timeout exists"
        }
    }
}

# --- 4. the ring is bounded, and the failing call is kept apart from the successful ones -----
$script:phaseRing.Clear()
$script:phaseFailing = $null
$script:phaseCallsTotal = [long]0
for ($index = 0; $index -lt ($RingCapacity + 6); $index++) {
    $call = New-MineruPhaseCall (New-Budget 5000) (New-Budget 5000) $PhaseOrder
    Enter-MineruPhase $call 0
    Exit-MineruPhase $call 0
    Complete-MineruPhaseCall $call
}
$last = New-MineruPhaseCall (New-Budget 5000) (New-Budget 5000) $PhaseOrder
Enter-MineruPhase $last 0
try { throw "stub final failure" } catch { Trace-MineruPhaseFailure $last 0 $_ }
Write-MineruPhaseTail
Assert-True ($script:written.Count -eq 1) "the tail was not written exactly once"
$tail = $script:written[0].Json
$document = ConvertFrom-Json $tail
Assert-True ($script:written[0].Name -ceq "sampling-phase-tail.json") "the tail artifact is misnamed"
Assert-True ($document.contract_version -ceq "mineru.sampling-phase-tail.v1") "tail contract version"
Assert-True ([int]$document.ring_capacity -eq $RingCapacity) "the ring does not declare the capacity it kept"
Assert-True ([int]$document.calls_total -eq ($RingCapacity + 7)) "the tail does not declare how many calls really happened"
$records = @($document.records)
Assert-True ($records.Count -eq $RingCapacity) "the ring kept more or fewer calls than it declares"
Assert-True ([int]$document.successful_records_retained -eq $records.Count) "the retained count disagrees with the records"
Assert-True ([int]$records[0].ordinal -eq 7) "the ring dropped the wrong end"
Assert-True ($null -ne $document.failing_record) "the failing call was not retained"
Assert-True ([int]$document.failing_record.ordinal -eq ($RingCapacity + 7)) "the retained failing call is not the last one"
Assert-True ((Get-PhaseRecord $document.failing_record "linux_sample").outcome -ceq "failed") `
    "the failing call was retained without its failure"
Assert-True (([Text.Encoding]::UTF8.GetByteCount($tail)) -le $MaximumTailBytes) "the tail document is over its byte bound"
Assert-True ($failures.Count -eq 0) "a successful tail write recorded a failure"

# --- 5. a tail that cannot be written is secondary evidence, never the terminal throw --------
$script:written.Clear()
$script:artifactThrows = $true
Write-MineruPhaseTail
$script:artifactThrows = $false
Assert-True ($script:written.Count -eq 0) "the failing writer still recorded an artifact"
Assert-True ($failures.Count -eq 1) "a failed tail write was not recorded as a secondary failure"

# --- 6. the exporter's own operation order, read from its source ----------------------------
# Static, and labelled as such: it executes nothing. It exists so a ring whose phase names
# drift from the operations they measure cannot pass the cases above by itself.
$sampleStart = $source.IndexOf('$sampleAction = [Func[long,string]]', [StringComparison]::Ordinal)
Assert-True ($sampleStart -ge 0) "the exporter no longer has the sample action this ring measures"
$sampleBody = $source.Substring($sampleStart)
$position = 0
foreach ($phase in $PhaseOrder) {
    $found = $sampleBody.IndexOf($phase, $position, [StringComparison]::Ordinal)
    Assert-True ($found -ge 0) ("the sample action does not name the phase " + $phase)
    $position = $found
}
Assert-True ($sampleBody.IndexOf('[MineruResidentWire]::Deadline', [StringComparison]::Ordinal) -ge 0) `
    "the sample action no longer derives the one deadline the ring reports"

[ordered]@{
    schema = "mineru-sampling-phase-ring-smoke.v1"
    status = "pass"
    exporter = $ExporterPath
    powershell = $PSVersionTable.PSVersion.ToString()
    qpc_frequency = $frequency
    deadline_exhausted_before_health = $true
    not_entered_phases_kept_null = $true
    failure_with_budget_remaining_named = $true
    inner_exception_type_kept = $true
    ring_capacity = $RingCapacity
    records_kept = $records.Count
    calls_total = [int]$document.calls_total
    failing_record_retained = $true
    tail_written_once = $true
    tail_write_failure_is_secondary = $true
    tail_bytes = [Text.Encoding]::UTF8.GetByteCount($tail)
    phase_order_matches_source = $true
    note = "no session, container, API, vLLM, GPU or PDF was used; New-MineruJson/Quote-MineruJson are stubs for the compiled wire type"
} | ConvertTo-Json -Compress
