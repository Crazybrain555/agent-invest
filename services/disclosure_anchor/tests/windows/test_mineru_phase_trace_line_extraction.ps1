param(
    [Parameter(Mandatory = $true)]
    [string]$CollectorPath
)

$ErrorActionPreference = "Stop"
$source = [IO.File]::ReadAllText($CollectorPath, [Text.Encoding]::UTF8)
$beginMarker = "# BEGIN MINERU PHASE TRACE LINE EXTRACTION V1"
$endMarker = "# END MINERU PHASE TRACE LINE EXTRACTION V1"
$beginIndex = $source.IndexOf($beginMarker, [StringComparison]::Ordinal)
$endIndex = $source.IndexOf($endMarker, [StringComparison]::Ordinal)
if ($beginIndex -lt 0 -or $endIndex -le $beginIndex) {
    throw "phase-trace extraction helper markers are missing"
}
$helperStart = $beginIndex + $beginMarker.Length
$helper = $source.Substring($helperStart, $endIndex - $helperStart)
Invoke-Expression $helper

$event = 'MINERU_PHASE_TRACE {"schema":"mineru-phase-trace.v2"}'
$columnZero = @(Get-MineruPhaseTraceLines -RawLines @($event) -Stream "stderr")
if ($columnZero.Count -ne 1 -or $columnZero[0] -ne $event) {
    throw "column-zero phase trace did not round trip"
}

$embeddedRaw = "`e[2K`rprogress 50%`r$event"
$embedded = @(
    Get-MineruPhaseTraceLines -RawLines @($embeddedRaw) -Stream "stderr"
)
if ($embedded.Count -ne 1 -or $embedded[0] -ne $event) {
    throw "embedded phase trace did not canonicalize"
}

$nonTrace = @(
    Get-MineruPhaseTraceLines -RawLines @("ordinary stderr") -Stream "stderr"
)
if ($nonTrace.Count -ne 0) {
    throw "ordinary stderr was treated as a phase trace"
}

$doubleRejected = $false
try {
    Get-MineruPhaseTraceLines -RawLines @("$event $event") -Stream "stderr" |
        Out-Null
}
catch {
    $doubleRejected = $true
}
if (-not $doubleRejected) { throw "multiple phase-trace prefixes were accepted" }

$stdoutRejected = 0
foreach ($stdoutLine in @($event, "prefix $event", "$event suffix")) {
    try {
        Get-MineruPhaseTraceLines -RawLines @($stdoutLine) -Stream "stdout" |
            Out-Null
    }
    catch {
        $stdoutRejected += 1
    }
}
if ($stdoutRejected -ne 3) { throw "stdout phase-trace placement was accepted" }

$nonStringRejected = $false
try {
    Get-MineruPhaseTraceLines -RawLines @(42) -Stream "stderr" | Out-Null
}
catch {
    $nonStringRejected = $true
}
if (-not $nonStringRejected) { throw "non-string log line was accepted" }

[ordered]@{
    schema = "mineru-phase-trace-line-extraction-smoke.v1"
    status = "pass"
    column_zero = $true
    embedded_progress = $true
    non_trace_ignored = $true
    multiple_prefix_rejected = $true
    stdout_placements_rejected = 3
    non_string_rejected = $true
} | ConvertTo-Json -Compress
