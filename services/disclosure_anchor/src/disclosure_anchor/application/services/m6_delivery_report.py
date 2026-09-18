"""Pure scoring of one frozen evaluation plan against one already-reduced M6 run.

Nothing here reads a file, a clock or a database: the caller supplies the exact
`reduce_m6_run` receipt, the owner journal records and the closure/exit facts it
could prove. Readiness is never recomputed - the reducer's
`ready_received_ticks` is the only admissible ready moment, so a late quality or
ledger observation can never backfill earlier credit. Every proof the caller
could not supply is named in `unknowns` and makes `delivery_pass` false.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
import re
from typing import Literal

from disclosure_anchor.application.contracts.m6_campaign import M6CorpusManifest
from disclosure_anchor.application.contracts.m6_campaign_intent import M6CampaignIntent, M6CampaignIntentV1
from disclosure_anchor.application.contracts.m6_delivery_report import (
    M6BusinessObligationsClosed, M6DeliveryEvidence, M6DeliveryMainWindow, M6DeliveryReport, M6Drain,
    M6EvidenceInput, M6LatencyBySize, M6LatencyClass, M6LatencyClasses, M6LatencyGateResult,
    M6LatencyMeasure, M6OwnershipClosed, M6PublicationQualified, M6QualifiedSource, M6ResourceSafety,
    M6RunValidity, M6ServiceDiagnostic, M6StageTiming, M6SubWindow, M6WholeRun, M6WindowExclusions,
)
from disclosure_anchor.application.contracts.m6_evaluation_plan import M6EvaluationPlan
from disclosure_anchor.application.contracts.m6_run import M6RunReceipt, M6RunSpec, M6SourceOutcome
from disclosure_anchor.application.contracts.m6_run_events import (
    M6AttemptAdmitted, M6AttemptFinal, M6DocumentQualified, M6PublicationCommitted, M6PublicConfirmation,
    M6RemoteAccepted, M6RunEvent, M6ServiceValidated,
)
from disclosure_anchor.application.services.m6_run_accounting import _eligible_sources
from disclosure_anchor.application.contracts.synchronized_telemetry import canonical_json_sha256
from disclosure_anchor.application.services.telemetry_resource_aggregates import ResourceAggregates

_SizeClass = Literal["short", "medium", "long"]
_SIZE_CLASSES: tuple[_SizeClass, ...] = ("short", "medium", "long")
# One exclusion bucket per non-credited accounting outcome; `credited_*` lines are
# bucketed by window position instead, so every outcome the reducer can emit is reported.
_EXCLUSION_BUCKET: dict[str, str] = {
    "carry_in": "carry_in", "replay": "replay", "not_first_publish": "not_first_publish",
    "novelty_unverified": "novelty_unverified", "quality_not_scorable": "quality_not_scorable",
    "quality_review_pending": "quality_review_pending", "confirmation_missing": "confirmation_missing",
    "page_count_conflict": "page_count_conflict", "failed": "failed",
    "qualification_missing": "qualification_missing", "admitted_after_deadline": "after_deadline",
}
# The stage notes this contract reads; any other kind belongs to another contract and is ignored.
_STAGE_NOTE_KINDS: frozenset[str] = frozenset({
    "remote_post_send", "remote_terminal_observed", "remote_terminal_failed",
})
_REMOTE_LANE = "remote"
# Every loss counter the stage observer seals. One nonzero count means the note
# stream is incomplete, so no stage-timed gate may be measured from it.
_OBSERVATION_LOSS_COUNTS: tuple[str, ...] = (
    "dropped", "guard_failures", "join_timeout", "late_notes", "note_errors", "truncated", "writer_errors",
)


@dataclass(frozen=True, slots=True)
class RunClosureFacts:
    """What the runner's own control receipts, the native sidecars and the journal close prove.

    Every field is `None` when its proof was absent: an absent proof is reported,
    never replaced by a default that would let an open obligation score as closed.
    """

    # `runner/campaign-receipt.json` -> `m6_assembly` and its `closure` block.
    runner_receipt_sha256: str | None = None
    runner_status: str | None = None
    closure_complete: bool | None = None
    ownership_closure_sha256: str | None = None
    residual_count: int | None = None
    children_exited: bool | None = None
    admitted_attempt_count: int | None = None
    final_attempt_count: int | None = None
    admission_reconciliation_sha256: str | None = None
    # `native/admission-closed.json`: the exact ACK the owner archived before its event.
    admission_closed_admitted_count: int | None = None
    admission_closed_unresolved_count: int | None = None
    admission_closed_last_producer_sequence: int | None = None
    admission_closed_reconciliation_sha256: str | None = None
    # `native/resources-closed.json`: the exact close command the owner archived.
    resources_closed_residual_count: int | None = None
    resources_closed_children_exited: bool | None = None
    resources_closed_receipt_sha256: str | None = None
    # `verifier/run-summary.json` and `verifier/drain-receipt.json`.
    verifier_summary_status: str | None = None
    drain_receipt_sha256: str | None = None
    # The owner journal's own `run_closed` reason.
    run_closed_reason: Literal["deadline_drained", "stop_requested", "failed"] | None = None


@dataclass(frozen=True, slots=True)
class ExternalExitFacts:
    """The external owner process exit and the local children the entry owned."""

    verified: bool = False
    exit_code: int | None = None
    forced_termination: bool | None = None
    cancel: str | None = None
    local_children_reaped: bool = False
    cleanup_failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TelemetryFacts:
    """Sealed observer evidence bound to this campaign, plus what its window aggregates to.

    `problems` names every binding or coverage check the evidence did not
    satisfy; each one keeps the resource gate `unknown`. `aggregates` is `None`
    when no coverage window could be established at all.
    """

    receipt_sha256: str
    seal_sha256: str | None
    contract_version: str
    status: str
    seal_status: str
    aggregates: ResourceAggregates | None = None
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StageNoteFact:
    """One line of the runner's stage-note stream, kept with its file position."""

    line: int
    attempt_id: str | None
    lane: str | None
    kind: str
    monotonic_ns: int
    scalars: Mapping[str, int | str | None]


@dataclass(frozen=True, slots=True)
class ClockBinding:
    """The monotonic clock a process stamped its notes with, bound to one boot session."""

    source: str
    implementation: str
    boot_session_uuid: str

    def canonical_sha256(self) -> str:
        # Same canonical JSON bytes as `closed_document.canonical_bytes`; this helper is
        # already inside the greenfield seam so the report service adds no new dependency.
        return canonical_json_sha256({
            "boot_session_uuid": self.boot_session_uuid,
            "implementation": self.implementation, "source": self.source,
        })


@dataclass(frozen=True, slots=True)
class VerifierAttemptFact:
    """The verifier's record for one attempt; only `public_ns` bound to the exact payload is a sample."""

    attempt_id: str
    confirmed: bool
    public_ns: int | None
    public_confirmation_sha256: str | None
    started_ns: int
    finished_ns: int


@dataclass(frozen=True, slots=True)
class StageTimingFacts:
    """Everything the Mac stage observation and the verifier can contribute to timing.

    Absence is expressed by the defaults, never by an invented instant: a run
    with no observation yields `observation_status=None` and no notes, which
    leaves every stage-timed gate unknown.
    """

    observation_status: Literal["complete", "partial", "invalid"] | None = None
    observation_counts: Mapping[str, int] = field(default_factory=dict)
    runner_clock: ClockBinding | None = None
    verifier_clock: ClockBinding | None = None
    notes: tuple[StageNoteFact, ...] = ()
    verifier_attempts: tuple[VerifierAttemptFact, ...] = ()
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _StageAttempt:
    """One admitted attempt scored against the stage notes, or the reason it could not be."""

    size: _SizeClass
    required_remote: bool
    remote_seconds: float | None
    public_seconds: float | None
    resends: int
    terminal_failures: int
    problems: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MainWindowResult:
    """The plan's main window scored in owner ticks, with the credited set it used."""

    window: M6DeliveryMainWindow
    credited: tuple[M6SourceOutcome, ...]
    drain: M6Drain


@dataclass(frozen=True, slots=True)
class _Attempt:
    attempt_id: str
    page_count: int
    admitted_ticks: int
    ticks: dict[str, int] = field(default_factory=dict)


def _size_class(page_count: int, plan: M6EvaluationPlan) -> _SizeClass:
    if page_count <= plan.size_classes.short_max_pages:
        return "short"
    return "medium" if page_count <= plan.size_classes.medium_max_pages else "long"


def _nearest_rank_p95(values: list[float]) -> float:
    """Nearest-rank percentile: the smallest sample at or above 95% of the ordered set."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))]


def _measure(values: list[float]) -> M6LatencyMeasure | None:
    if not values:
        return None
    return M6LatencyMeasure(p95_s=_nearest_rank_p95(values), max_s=max(values))


def classify_main_window(
    plan: M6EvaluationPlan, spec: M6RunSpec, receipt: M6RunReceipt | None,
) -> MainWindowResult:
    """Score the plan's frozen `[T0 + start*f, T0 + end*f)` interval from the reducer's ready ticks.

    Credit is deduplicated per immutable source exactly as the accounting
    contract does (earliest ready wins), and only a `credited_window` source
    whose ready tick falls inside the interval counts: whole-run and drain
    credit never enters the window even when the plan interval extends past the
    admission deadline.
    """
    frequency = spec.clock.qpc_frequency_hz
    start_ticks = spec.t0_ticks + plan.main_window.start_offset_seconds * frequency
    end_ticks = spec.t0_ticks + plan.main_window.end_offset_seconds * frequency
    span_seconds = plan.main_window.end_offset_seconds - plan.main_window.start_offset_seconds
    sub_ticks = plan.sub_window_seconds * frequency
    lines: tuple[M6SourceOutcome, ...] = () if receipt is None else receipt.sources
    eligible, _carry_in = _eligible_sources(lines)
    credited = tuple(sorted(eligible.values(), key=lambda line: line.source_pdf_sha256))

    def inside(line: M6SourceOutcome, low: int, high: int) -> bool:
        ready = line.ready_received_ticks
        return (line.outcome == "credited_window" and ready is not None and low <= ready < high)

    in_window = [line for line in credited if inside(line, start_ticks, end_ticks)]
    documents, pages = len(in_window), sum(line.page_count for line in in_window)
    sub_windows = []
    for index in range(plan.sub_window_count):
        low = start_ticks + index * sub_ticks
        members = [line for line in credited if inside(line, low, low + sub_ticks)]
        sub_windows.append(M6SubWindow(
            index=index, start_ticks=low, end_ticks=low + sub_ticks,
            documents=len(members), pages=sum(line.page_count for line in members),
        ))
    counts = dict.fromkeys(_EXCLUSION_BUCKET.values(), 0)
    counts["late_or_after_window"] = 0
    for line in lines:
        bucket = _EXCLUSION_BUCKET.get(line.outcome)
        if bucket is not None:
            counts[bucket] += 1
    for line in credited:
        if line.outcome == "credited_whole_run_only":
            counts["after_deadline"] += 1
        elif not inside(line, start_ticks, end_ticks):
            counts["late_or_after_window"] += 1
    drained = [line for line in credited if line.outcome == "credited_whole_run_only"]
    return MainWindowResult(
        window=M6DeliveryMainWindow(
            start_ticks=start_ticks, end_ticks=end_ticks, span_seconds=span_seconds,
            documents=documents, pages=pages,
            documents_per_hour=documents * 3600 / span_seconds,
            pages_per_minute=pages * 60 / span_seconds,
            sub_windows=tuple(sub_windows), excluded=M6WindowExclusions(**counts),
        ),
        credited=credited,
        drain=M6Drain(documents=len(drained), pages=sum(line.page_count for line in drained)),
    )


def _attempts(events: tuple[M6RunEvent, ...]) -> dict[str, _Attempt]:
    """The first admission and the first owner-received tick per attempt fact, in journal order."""
    attempts: dict[str, _Attempt] = {}
    for record in events:
        payload = record.event.payload
        tick = record.stamp.received_qpc_ticks
        if isinstance(payload, M6AttemptAdmitted):
            attempts.setdefault(payload.attempt_id, _Attempt(
                attempt_id=payload.attempt_id, page_count=payload.source_page_count, admitted_ticks=tick,
            ))
        elif isinstance(payload, (
            M6AttemptFinal, M6DocumentQualified, M6PublicationCommitted, M6PublicConfirmation,
            M6RemoteAccepted, M6ServiceValidated,
        )):
            attempt = attempts.get(payload.attempt_id)
            if attempt is not None:
                attempt.ticks.setdefault(payload.kind, tick)
    return attempts


def latency_by_size(
    plan: M6EvaluationPlan, spec: M6RunSpec, events: tuple[M6RunEvent, ...],
    stage_timing: StageTimingFacts | None = None,
) -> M6LatencyBySize:
    """Latency per plan size class on two clocks, with the plan's latency gates applied.

    Owner-tick measures come from the physical owner's stamps. The remote and
    public measures come from the Mac stage notes and the verifier's exact
    public sample; the owner journal is the identity authority for which
    attempts those samples are allowed to describe. The two clocks are never
    subtracted from each other, and no owner tick ever substitutes for a stage
    instant: a missing stage sample leaves its gate unknown.
    """
    return _latency_sections(plan, spec, events, stage_timing)[0]


def _journal_identities(events: tuple[M6RunEvent, ...]) -> tuple[
    dict[str, M6AttemptAdmitted], dict[str, M6RemoteAccepted], dict[str, M6PublicConfirmation],
]:
    """The journal facts that decide which stage notes may describe which attempt."""
    admitted: dict[str, M6AttemptAdmitted] = {}
    accepted: dict[str, M6RemoteAccepted] = {}
    confirmed: dict[str, M6PublicConfirmation] = {}
    for record in events:
        payload = record.event.payload
        if isinstance(payload, M6AttemptAdmitted):
            admitted.setdefault(payload.attempt_id, payload)
        elif isinstance(payload, M6RemoteAccepted):
            accepted.setdefault(payload.attempt_id, payload)
        elif isinstance(payload, M6PublicConfirmation):
            confirmed.setdefault(payload.attempt_id, payload)
    return admitted, accepted, confirmed


def _valid_sha(value: object) -> bool:
    return type(value) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def _stage_preconditions(facts: StageTimingFacts | None) -> tuple[str, ...]:
    """Whole-run conditions without which a formal stage-timing gate cannot pass."""
    if facts is None or facts.observation_status is None:
        return ("stage_observation_absent",)
    reasons: list[str] = []
    if facts.observation_status != "complete":
        reasons.append("stage_observation_" + facts.observation_status)
    for name in _OBSERVATION_LOSS_COUNTS:
        value = facts.observation_counts.get(name)
        if type(value) is not int or value < 0:
            reasons.append("stage_observation_count_unreadable:" + name)
        elif value:
            reasons.append("stage_observation_loss:" + name)
    if (facts.runner_clock is None or facts.verifier_clock is None
            or facts.runner_clock != facts.verifier_clock
            or facts.runner_clock.source != "python.time.monotonic_ns"):
        reasons.append("stage_clock_domain_unbound")
    reasons.extend(facts.problems)
    return tuple(sorted(set(reasons)))


def _stage_attempt(
    admission: M6AttemptAdmitted, *, size: _SizeClass, accepted: M6RemoteAccepted | None,
    confirmation: M6PublicConfirmation | None, notes: list[StageNoteFact],
    verifier: VerifierAttemptFact | None, public_clock_bound: bool,
) -> _StageAttempt:
    """Score one admitted attempt's remote episode from its own notes.

    A note may only describe this attempt when every identity the journal
    already fixed agrees: fence, source and the accepted remote task. Anything
    else is a contradiction, and a contradicted attempt yields no sample at all
    rather than a shorter one.
    """
    attempt_id = admission.attempt_id
    problems: list[str] = []
    contradiction = "latency_note_contradiction:" + attempt_id
    required_remote = accepted is not None
    if accepted is not None and accepted.attempt_id != admission.attempt_id:
        problems.append(contradiction)
    if any(note.attempt_id != attempt_id or note.monotonic_ns < 0 for note in notes):
        problems.append(contradiction)
    if any(note.lane != _REMOTE_LANE for note in notes):
        problems.append(contradiction)  # these kinds exist only on the remote lane
    remote_notes = [note for note in notes if note.lane == _REMOTE_LANE]

    posts: list[StageNoteFact] = []
    for note in (item for item in remote_notes if item.kind == "remote_post_send"):
        if (note.scalars.get("fence_identity") != admission.fence_identity
                or note.scalars.get("source_pdf_sha256") != admission.source_pdf_sha256
                or not _valid_sha(note.scalars.get("submission_intent_sha256"))):
            problems.append(contradiction)
            continue
        posts.append(note)
    if len({note.scalars.get("submission_intent_sha256") for note in posts}) > 1:
        problems.append(contradiction)
    # The first real POST of this attempt; a resend after a transport fault is counted, never averaged in.
    start_ns = min((note.monotonic_ns for note in posts), default=None)

    identity = None if accepted is None else accepted.remote_task_identity_sha256
    terminals: list[StageNoteFact] = []
    for note in (item for item in remote_notes if item.kind == "remote_terminal_observed"):
        if (accepted is None or note.scalars.get("remote_task_identity_sha256") != identity
                or note.scalars.get("fence_identity") != admission.fence_identity
                or note.scalars.get("accepted_submission_receipt_sha256") != accepted.acceptance_receipt_sha256
                or not _valid_sha(note.scalars.get("terminal_receipt_sha256"))):
            problems.append(contradiction)  # a competing terminal identity is never a sample
            continue
        terminals.append(note)
    if len({note.scalars.get("terminal_receipt_sha256") for note in terminals}) > 1:
        problems.append(contradiction)
        terminals = []
    terminal_ns = min((note.monotonic_ns for note in terminals), default=None)

    failures = [note for note in remote_notes if note.kind == "remote_terminal_failed"]
    if failures and required_remote:
        problems.append("latency_remote_failed:" + attempt_id)

    remote_seconds: float | None = None
    if required_remote:
        if start_ns is None or terminal_ns is None:
            problems.append("latency_sample_missing:" + attempt_id + ":remote")
        elif terminal_ns <= start_ns:
            problems.append("latency_time_reversed:" + attempt_id)
        elif not problems:
            remote_seconds = (terminal_ns - start_ns) / 1_000_000_000

    public_seconds: float | None = None
    if remote_seconds is not None and confirmation is not None:
        assert terminal_ns is not None
        if (verifier is None or verifier.attempt_id != attempt_id or verifier.confirmed is not True
                or verifier.public_ns is None or not _valid_sha(verifier.public_confirmation_sha256)
                or verifier.public_confirmation_sha256 != confirmation.canonical_sha256()
                or confirmation.source_pdf_sha256 != admission.source_pdf_sha256
                or confirmation.source_page_count != admission.source_page_count
                or confirmation.document_id != admission.document_id
                or (admission.processing_run_id is not None
                    and confirmation.processing_run_id != admission.processing_run_id)):
            problems.append("latency_sample_missing:" + attempt_id + ":public")
        elif (verifier.started_ns < 0 or verifier.finished_ns < verifier.started_ns
              or not verifier.started_ns <= verifier.public_ns <= verifier.finished_ns):
            problems.append("latency_time_reversed:" + attempt_id)
        elif not public_clock_bound:
            # Do not even compare cross-boot values as an ordered interval.
            # Runner-only duration is still interpretable on its own clock.
            problems.append("stage_clock_domain_unbound")
        elif verifier.public_ns < terminal_ns:
            problems.append("latency_time_reversed:" + attempt_id)
        else:
            public_seconds = (verifier.public_ns - terminal_ns) / 1_000_000_000

    return _StageAttempt(
        size=size, required_remote=required_remote,
        remote_seconds=None if any(p != "stage_clock_domain_unbound" for p in problems) else remote_seconds,
        public_seconds=None if problems else public_seconds,
        resends=max(0, len(posts) - 1), terminal_failures=len(failures),
        problems=tuple(sorted(set(problems))),
    )


def _latency_sections(
    plan: M6EvaluationPlan, spec: M6RunSpec, events: tuple[M6RunEvent, ...],
    stage_timing: StageTimingFacts | None = None,
) -> tuple[M6LatencyBySize, tuple[str, ...]]:
    """The reported latency section and the names of the plan's gates the run could not measure."""
    frequency = spec.clock.qpc_frequency_hz
    attempts = _attempts(events)
    admitted, accepted, confirmations = _journal_identities(events)
    samples: dict[tuple[str, str], list[float]] = {}
    population = dict.fromkeys(_SIZE_CLASSES, 0)
    admission_to_public: list[float] = []
    pairs = (
        ("admission_to_remote_accepted", None, "remote_accepted"),
        ("remote_accepted_to_publication_committed", "remote_accepted", "publication_committed"),
        ("publication_committed_to_public_confirmation", "publication_committed", "public_confirmation"),
        ("admission_to_public_confirmation", None, "public_confirmation"),
    )
    for attempt in attempts.values():
        size = _size_class(attempt.page_count, plan)
        population[size] += 1
        for name, start_kind, end_kind in pairs:
            start = attempt.admitted_ticks if start_kind is None else attempt.ticks.get(start_kind)
            end = attempt.ticks.get(end_kind)
            if start is None or end is None or end < start:
                continue
            samples.setdefault((size, name), []).append((end - start) / frequency)
        confirmed = attempt.ticks.get("public_confirmation")
        if confirmed is not None and confirmed >= attempt.admitted_ticks:
            admission_to_public.append((confirmed - attempt.admitted_ticks) / frequency)

    stage_reasons = list(_stage_preconditions(stage_timing))
    notes_by_attempt: dict[str, list[StageNoteFact]] = {}
    if stage_timing is not None:
        for note in stage_timing.notes:
            if note.kind in _STAGE_NOTE_KINDS and note.attempt_id is not None:
                notes_by_attempt.setdefault(note.attempt_id, []).append(note)
    verifiers: dict[str, VerifierAttemptFact] = {}
    duplicate_verifiers: set[str] = set()
    if stage_timing is not None:
        for item in stage_timing.verifier_attempts:
            if item.attempt_id in verifiers:
                duplicate_verifiers.add(item.attempt_id)
            else:
                verifiers[item.attempt_id] = item
    for attempt_id in sorted(duplicate_verifiers):
        stage_reasons.append("latency_verifier_duplicate:" + attempt_id)
        del verifiers[attempt_id]  # neither order nor last-write-wins can hide a conflict
    stage_reasons.extend("latency_verifier_unadmitted:" + name
                         for name in sorted(set(verifiers) - set(admitted)))
    public_clock_bound = bool(stage_timing is not None and stage_timing.runner_clock is not None
                              and stage_timing.runner_clock == stage_timing.verifier_clock
                              and stage_timing.runner_clock.source == "python.time.monotonic_ns")
    # The journal admits attempts; a note for an attempt it never admitted contradicts it.
    stage_reasons.extend("latency_note_contradiction:" + name
                         for name in sorted(set(notes_by_attempt) - set(admitted)))
    scored = {
        attempt_id: _stage_attempt(
            admission, size=_size_class(admission.source_page_count, plan),
            accepted=accepted.get(attempt_id), confirmation=confirmations.get(attempt_id),
            notes=notes_by_attempt.get(attempt_id, []), verifier=verifiers.get(attempt_id),
            public_clock_bound=public_clock_bound,
        )
        for attempt_id, admission in admitted.items()
    }
    attempt_problems = sorted({name for item in scored.values() for name in item.problems})
    remote_by_class: dict[str, list[float]] = {size: [] for size in _SIZE_CLASSES}
    public_by_class: dict[str, list[float]] = {size: [] for size in _SIZE_CLASSES}
    resends = dict.fromkeys(_SIZE_CLASSES, 0)
    terminal_failures = dict.fromkeys(_SIZE_CLASSES, 0)
    for entry in scored.values():
        if entry.remote_seconds is not None:
            remote_by_class[entry.size].append(entry.remote_seconds)
        if entry.public_seconds is not None:
            public_by_class[entry.size].append(entry.public_seconds)
        resends[entry.size] += entry.resends
        terminal_failures[entry.size] += entry.terminal_failures
    all_public = [value for values in public_by_class.values() for value in values]

    classes = {size: M6LatencyClass(
        n=population[size],
        **{name: _measure(samples.get((size, name), [])) for name, _start, _end in pairs},
        remote_post_to_terminal=_measure(remote_by_class[size]),
        terminal_to_public_confirmation=_measure(public_by_class[size]),
        remote_samples=len(remote_by_class[size]), public_samples=len(public_by_class[size]),
        remote_resends=resends[size], remote_terminal_failures=terminal_failures[size],
    ) for size in _SIZE_CLASSES}
    blocked = bool(stage_reasons) or bool(attempt_problems)
    result, unmeasured = _latency_gates(
        plan, classes, all_public=all_public, admission_to_public=admission_to_public, blocked=blocked,
    )
    required = sum(1 for item in scored.values() if item.required_remote)
    measured = sum(1 for item in scored.values() if item.remote_seconds is not None)
    reasons = tuple(sorted(set(stage_reasons) | set(attempt_problems)))
    if not reasons and measured == 0:
        reasons = ("stage_no_timed_attempt",)
    binding = None
    if (stage_timing is not None and stage_timing.runner_clock is not None
            and stage_timing.runner_clock == stage_timing.verifier_clock):
        binding = stage_timing.runner_clock.canonical_sha256()
    return M6LatencyBySize(
        classes=M6LatencyClasses(**classes),
        stage_timing=M6StageTiming(
            status="unknown" if reasons else "measured", reasons=reasons,
            observation_status=None if stage_timing is None else stage_timing.observation_status,
            clock_binding_sha256=binding, attempts_required=required, attempts_measured=measured,
            notes_total=0 if stage_timing is None else len(stage_timing.notes),
        ),
        gates=result,
    ), unmeasured


def _latency_gates(
    plan: M6EvaluationPlan, classes: dict[_SizeClass, M6LatencyClass], *,
    all_public: list[float], admission_to_public: list[float], blocked: bool,
) -> tuple[M6LatencyGateResult, tuple[str, ...]]:
    """Apply the plan's latency gates; a gate the evidence cannot measure is named and never passes.

    A stage-timed gate is unmeasured whenever the stage observation is blocked
    for any named reason, so a contradicted or partly lost run can never pass on
    the attempts that happened to survive.
    """
    gates = plan.latency_gates
    if gates is None:
        return M6LatencyGateResult(status="unknown", failed=()), ()

    def stage(measure: M6LatencyMeasure | None, attribute: str) -> float | None:
        if blocked or measure is None:
            return None
        value = getattr(measure, attribute)
        assert type(value) is float
        return value

    short = classes["short"].remote_post_to_terminal
    long_class = classes["long"].remote_post_to_terminal
    remote_public = _measure(all_public)
    admission = _measure(admission_to_public)
    checks: tuple[tuple[str, float | None, int], ...] = (
        ("short_p95_s", stage(short, "p95_s"), gates.short_p95_s),
        ("short_max_s", stage(short, "max_s"), gates.short_max_s),
        ("long_p95_s", stage(long_class, "p95_s"), gates.long_p95_s),
        ("long_max_s", stage(long_class, "max_s"), gates.long_max_s),
        ("remote_to_public_p95_s", stage(remote_public, "p95_s"), gates.remote_to_public_p95_s),
        ("remote_to_public_max_s", stage(remote_public, "max_s"), gates.remote_to_public_max_s),
        ("admission_to_public_max_s", None if admission is None else admission.max_s,
         gates.admission_to_public_max_s),
    )
    failed = tuple(sorted(name for name, observed, bound in checks if observed is not None and observed > bound))
    unmeasured = tuple(sorted(name for name, observed, _bound in checks if observed is None))
    if failed:
        return M6LatencyGateResult(status="fail", failed=failed), unmeasured
    return M6LatencyGateResult(status="unknown" if unmeasured else "pass", failed=()), unmeasured


def _resource_safety(
    plan: M6EvaluationPlan, telemetry: TelemetryFacts | None,
) -> M6ResourceSafety:
    """Score the plan's resource gates against one sealed observation window.

    A positive component delta or GPU headroom below the gate is a failure even
    when other components are unproven. Otherwise any binding, coverage or
    aggregate problem - and any component the window could not prove - leaves
    the gate `unknown`: an unproven counter is never clamped to zero.
    """
    gates = plan.resource_gates
    if gates is None:
        return M6ResourceSafety(
            status="unknown", gates=None, evidence_sha256=None,
            reason="evaluation_plan_declares_no_resource_gates",
        )
    if telemetry is None:
        return M6ResourceSafety(
            status="unknown", gates=gates, evidence_sha256=None, reason="resource_telemetry_receipt_absent",
        )
    evidence = telemetry.receipt_sha256
    aggregates = telemetry.aggregates
    failed: list[str] = []
    unproven: list[str] = []
    if aggregates is None:
        unproven.append("resource_aggregates_absent")
    else:
        for name, delta in aggregates.component_deltas:
            bound = gates.preemption_max if name == "vllm_preemptions_total" else gates.oom_max
            if delta is None:
                unproven.append("resource_measure_unproven:" + name)
            elif delta > bound:
                failed.append(name)
        if aggregates.gpu_free_min_bytes is None:
            unproven.append("resource_measure_unproven:gpu_free_min_bytes")
        elif aggregates.gpu_free_min_bytes < gates.gpu_free_min_bytes:
            failed.append("gpu_free_min_bytes")
    if failed:
        return M6ResourceSafety(
            status="fail", gates=gates, evidence_sha256=evidence,
            reason="resource_gate_exceeded:" + ",".join(sorted(set(failed))),
        )
    problems = list(telemetry.problems) + ([] if aggregates is None else list(aggregates.problems)) + unproven
    if problems:
        # The named reason prefers what the evidence itself says (a failed terminal, a lane
        # collection failure, a binding mismatch) over the generic "no aggregate" marker that
        # every such run also carries; the report stays unknown either way.
        generic = set(unproven)
        return M6ResourceSafety(
            status="unknown", gates=gates, evidence_sha256=evidence,
            reason=sorted(set(problems), key=lambda name: (name in generic, name))[0],
        )
    return M6ResourceSafety(
        status="pass", gates=gates, evidence_sha256=evidence,
        reason="observed_resource_evidence_meets_every_plan_gate",
    )


def _ownership_closed(
    events: tuple[M6RunEvent, ...], closure: RunClosureFacts,
) -> tuple[M6OwnershipClosed | None, tuple[str, ...]]:
    """The owner's `resources_closed` record, which the runner receipt and the native sidecar must both repeat."""
    missing: list[str] = []
    observed: list[tuple[int, bool, str]] = []
    for record in events:
        payload = record.event.payload
        if payload.kind == "resources_closed":
            observed.append((payload.residual_count, payload.children_exited, payload.ownership_receipt_sha256))
    if not observed:
        missing.append("owner_resources_closed_event")
    for name, count, exited, receipt_sha in (
        ("runner_ownership_closure", closure.residual_count, closure.children_exited,
         closure.ownership_closure_sha256),
        ("resources_closed_sidecar", closure.resources_closed_residual_count,
         closure.resources_closed_children_exited, closure.resources_closed_receipt_sha256),
    ):
        if count is None or exited is None or receipt_sha is None:
            missing.append(name)
        else:
            observed.append((count, exited, receipt_sha))
    if not observed:
        return None, tuple(missing)
    first = observed[0]
    if any(item != first for item in observed):
        missing.append("ownership_closure_agreement")
    ownership = M6OwnershipClosed(residual_count=first[0], children_exited=first[1], receipt_sha256=first[2])
    if ownership.residual_count != 0 or not ownership.children_exited:
        missing.append("ownership_closure_residual")
    return ownership, tuple(missing)


def _obligations(
    events: tuple[M6RunEvent, ...], closure: RunClosureFacts, external: ExternalExitFacts,
) -> M6BusinessObligationsClosed:
    """Close every claimed obligation from the journal, then demand each declared control proof.

    A missing or disagreeing proof is named in `missing`; a high count never
    stands in for it, and `all_closed` is true only when nothing is missing.
    """
    admitted: dict[str, M6AttemptAdmitted] = {}
    finals: dict[str, M6AttemptFinal] = {}
    accepted: set[str] = set()
    drained: list[str] = []
    runner_sequences: list[int] = []
    for record in events:
        payload = record.event.payload
        if record.event.producer_kind in {"e2e_runner", "service_runner"}:
            runner_sequences.append(record.event.producer_sequence)
        if isinstance(payload, M6AttemptAdmitted):
            admitted.setdefault(payload.attempt_id, payload)
        elif isinstance(payload, M6AttemptFinal):
            finals.setdefault(payload.attempt_id, payload)
        elif payload.kind == "remote_accepted":
            accepted.add(payload.attempt_id)
        elif payload.kind == "verifier_drained":
            drained.append(payload.drain_receipt_sha256)
    without_final = tuple(sorted(name for name in admitted if name not in finals))
    remote_open = tuple(sorted(
        name for name, final in finals.items()
        if name in accepted and final.remote_disposition not in {"consumed", "absent"}
    ))
    missing: list[str] = []
    if without_final:
        missing.append("attempt_final")
    if remote_open:
        missing.append("remote_disposition")
    ownership, ownership_missing = _ownership_closed(events, closure)
    missing.extend(ownership_missing)
    journal_admitted, journal_finals = len(admitted), len(finals)
    # Runner control receipt.
    if closure.runner_receipt_sha256 is None:
        missing.append("runner_campaign_receipt")
    if closure.runner_status != "complete":
        missing.append("runner_status_complete")
    if closure.closure_complete is not True:
        missing.append("runner_closure_complete")
    runner_agrees = (
        closure.admission_reconciliation_sha256 is not None
        and closure.admitted_attempt_count == journal_admitted
        and closure.final_attempt_count == journal_finals
    )
    if not runner_agrees:
        missing.append("runner_admission_reconciliation")
    # The owner's archived ACK sidecar, which must repeat the runner's reconciliation exactly.
    last_runner_sequence = max(runner_sequences) if runner_sequences else None
    sidecar_agrees = (
        closure.admission_closed_admitted_count == journal_admitted
        and closure.admission_closed_unresolved_count == 0
        and closure.admission_closed_last_producer_sequence == last_runner_sequence
        and closure.admission_closed_reconciliation_sha256 is not None
        and closure.admission_closed_reconciliation_sha256 == closure.admission_reconciliation_sha256
    )
    if closure.admission_closed_admitted_count is None:
        missing.append("admission_closed_sidecar")
    elif not sidecar_agrees:
        missing.append("admission_closed_agreement")
    reconciled = runner_agrees and sidecar_agrees
    # Verifier closure: its summary and the drain receipt the owner journal named.
    if closure.verifier_summary_status != "complete":
        missing.append("verifier_run_summary_complete")
    if closure.drain_receipt_sha256 is None:
        missing.append("verifier_drain_receipt")
    elif not drained or any(item != closure.drain_receipt_sha256 for item in drained):
        missing.append("verifier_drain_receipt_agreement")
    if closure.run_closed_reason is None:
        missing.append("run_closed")
    # External owner exit and locally owned children.
    if not external.verified:
        missing.append("external_owner_exit")
    if external.exit_code is None:
        missing.append("external_owner_exit_code")
    elif external.exit_code != 0:
        missing.append("external_owner_exit_code_nonzero")
    if external.forced_termination is not False:
        missing.append("external_owner_not_forced")
    if external.cancel is not None:
        missing.append("external_owner_not_cancelled")
    if not external.local_children_reaped:
        missing.append("local_children_reaped")
    missing.extend("cleanup_failure:" + name for name in external.cleanup_failures)
    return M6BusinessObligationsClosed(
        all_closed=not missing, admitted_attempts=journal_admitted, final_attempts=journal_finals,
        attempts_without_final=without_final, remote_open=remote_open,
        ack_or_absence_proven=not without_final and not remote_open,
        admission_reconciled=reconciled, ownership_closed=ownership,
        run_closed_reason=closure.run_closed_reason,
        external_owner_exit_verified=external.verified, local_children_reaped=external.local_children_reaped,
        missing=tuple(sorted(set(missing))),
    )


# Loader unknowns that describe how the evidence was found rather than what the
# run proved; they never enter the report or gate a pass.
DISCOVERY_ONLY_UNKNOWNS: frozenset[str] = frozenset({"driver_input_hashes_absent"})
# Evidence whose absence, unreadability or damage blocks `delivery_pass` on top
# of every open obligation: nothing to score, or the intent/plan not provably
# frozen before admission. A stem matches its `_absent`, `_unreadable:<error>`,
# `_exceeds_byte_bound` and `_is_not_an_object` forms alike.
_BLOCKING_STEMS: tuple[str, ...] = (
    "run_receipt", "owner_journal", "campaign_intent", "campaign-intent.json", "campaign_inputs",
    "campaign-inputs.json", "evaluation_plan_not_frozen_in_intent", "evaluation_plan_not_in_run_output",
    "evaluation-plan.json",
)
_BLOCKING_PREFIXES: tuple[str, ...] = ("resource_safety_unproven:", "latency_gate_unmeasured:")


def _blocking(unknowns: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        name for name in unknowns
        if name.startswith(_BLOCKING_PREFIXES)
        or any(name == stem or name.startswith(stem + "_") for stem in _BLOCKING_STEMS)
    )


def _unknowns(
    *, intent: M6CampaignIntent | M6CampaignIntentV1 | None, receipt: M6RunReceipt | None,
    events: tuple[M6RunEvent, ...], closure: RunClosureFacts, obligations: M6BusinessObligationsClosed,
    resource: M6ResourceSafety, unmeasured_gates: tuple[str, ...], loader_unknowns: tuple[str, ...],
    plan: M6EvaluationPlan,
) -> tuple[str, ...]:
    names = list(obligations.missing)
    covered = set(obligations.missing)
    for name in loader_unknowns:
        if name in DISCOVERY_ONLY_UNKNOWNS:
            continue
        if name == "resource_telemetry_receipt_absent" and plan.resource_gates is None:
            continue
        stem = name.removesuffix("_absent")
        if stem in covered or stem + "_complete" in covered:
            continue  # the same absent proof is already named as an open obligation
        names.append(name)
    if receipt is None:
        names.append("run_receipt_absent")
    if not events:
        names.append("owner_journal_absent")
    if intent is None:
        names.append("campaign_intent_absent")
    elif isinstance(intent, M6CampaignIntentV1):
        names.append("evaluation_plan_not_frozen_in_intent")
    if resource.gates is not None and resource.status == "unknown":
        names.append("resource_safety_unproven:" + resource.reason)
    names.extend("latency_gate_unmeasured:" + name for name in unmeasured_gates)
    return tuple(sorted(set(names)))


def build_delivery_report(
    *, plan: M6EvaluationPlan, intent: M6CampaignIntent | M6CampaignIntentV1 | None, spec: M6RunSpec,
    receipt: M6RunReceipt | None, events: tuple[M6RunEvent, ...], manifest: M6CorpusManifest,
    closure: RunClosureFacts, external: ExternalExitFacts, telemetry: TelemetryFacts | None,
    inputs: tuple[tuple[str, str], ...], loader_unknowns: tuple[str, ...] = (),
    stage_timing: StageTimingFacts | None = None,
) -> M6DeliveryReport:
    """Score `plan` against one run's evidence; `receipt=None` leaves validity unknown and every count zero.

    `loader_unknowns` are the evidence gaps the reader named while loading the
    run directory; every one of them is reported, and the ones that mean the
    plan was not provably frozen or nothing could be scored block a pass.
    `stage_timing=None` means no Mac stage observation was supplied at all, so
    every stage-timed latency gate is unknown with `stage_observation_absent`.

    `delivery_pass` is true only when the plan was frozen in a v2 intent, the
    run reduced to `complete`, every claimed obligation is closed with proven
    external/child exits, and each declared gate family was measured and met.
    """
    if plan.mode != spec.mode:
        raise ValueError("evaluation plan scores another run mode than the frozen spec")
    if spec.manifest_sha256 != manifest.canonical_sha256():
        raise ValueError("corpus manifest differs from the frozen spec")
    if receipt is not None and receipt.spec_sha256 != spec.canonical_sha256():
        raise ValueError("run receipt was reduced from another spec")
    if isinstance(intent, M6CampaignIntent) and intent.evaluation_plan_sha256 != plan.canonical_sha256():
        raise ValueError("the campaign intent froze another evaluation plan than the one scored")
    frequency = spec.clock.qpc_frequency_hz
    main = classify_main_window(plan, spec, receipt)
    latency, unmeasured = _latency_sections(plan, spec, events, stage_timing)
    resource = _resource_safety(plan, telemetry)
    obligations = _obligations(events, closure, external)
    origins = {entry.source_pdf_sha256: entry.origin for entry in manifest.entries}
    qualified = tuple(M6QualifiedSource(
        source_pdf_sha256=line.source_pdf_sha256, attempt_id=line.attempt_id, page_count=line.page_count,
        ready_received_ticks=line.ready_received_ticks,
        outcome="credited_window" if line.outcome == "credited_window" else "credited_whole_run_only",
    ) for line in main.credited)
    whole_pages = sum(line.page_count for line in main.credited)
    service = None if spec.mode != "service_diagnostic" else M6ServiceDiagnostic(
        validated_documents=len(main.credited), validated_pages=whole_pages,
        replay_pages=sum(line.page_count for line in main.credited
                         if origins.get(line.source_pdf_sha256) == "replay"),
    )
    gates_met = plan.delivery_gates is None or (
        main.window.documents_per_hour >= plan.delivery_gates.main_docs_per_hour_min
        and main.window.pages_per_minute >= plan.delivery_gates.main_pages_per_min_min
    )
    unknowns = _unknowns(
        intent=intent, receipt=receipt, events=events, closure=closure, obligations=obligations,
        resource=resource, unmeasured_gates=unmeasured, loader_unknowns=loader_unknowns, plan=plan,
    )
    delivery_pass = bool(
        isinstance(intent, M6CampaignIntent)
        and receipt is not None and receipt.status == "complete" and obligations.all_closed and gates_met
        and (plan.latency_gates is None or latency.gates.status == "pass")
        and (plan.resource_gates is None or resource.status == "pass")
        and not _blocking(unknowns)
    )
    return M6DeliveryReport(
        run_validity=M6RunValidity(
            status="unknown" if receipt is None else receipt.status,
            incomplete_reasons=() if receipt is None else receipt.incomplete_reasons,
            invalid_reasons=() if receipt is None else receipt.invalid_reasons,
            spec_sha256=spec.canonical_sha256(),
            intent_sha256=None if intent is None else intent.canonical_sha256(),
            evaluation_plan_sha256=plan.canonical_sha256(),
            journal_prefix_sha256=None if receipt is None else receipt.journal_prefix_sha256,
            events_total=0 if receipt is None else receipt.events_total,
            duplicate_events=0 if receipt is None else receipt.duplicate_events,
            owner_epoch_sha256=events[0].stamp.owner_process_epoch_sha256 if events else None,
            t0_ticks=spec.t0_ticks, deadline_ticks=spec.deadline_ticks,
            tclose_ticks=None if receipt is None else receipt.tclose_ticks,
            qpc_frequency_hz=frequency,
        ),
        business_obligations_closed=obligations,
        publication_qualified=M6PublicationQualified(
            documents=len(qualified), pages=whole_pages, sources=qualified,
        ),
        main_window=main.window,
        whole_run=M6WholeRun(
            documents=len(main.credited), pages=whole_pages,
            metrics=None if receipt is None else receipt.metrics,
            elapsed_seconds=None if receipt is None or receipt.elapsed_ticks is None
            else receipt.elapsed_ticks / frequency,
        ),
        drain=main.drain,
        service_diagnostic=service,
        resource_safety=resource,
        latency_by_size=latency,
        unknowns=unknowns,
        delivery_pass=delivery_pass,
        evidence=M6DeliveryEvidence(inputs=tuple(
            M6EvidenceInput(path=path, sha256=digest) for path, digest in inputs
        )),
    )


__all__ = [
    "DISCOVERY_ONLY_UNKNOWNS", "ClockBinding", "ExternalExitFacts", "MainWindowResult", "RunClosureFacts",
    "StageNoteFact", "StageTimingFacts", "TelemetryFacts", "VerifierAttemptFact",
    "build_delivery_report", "classify_main_window", "latency_by_size",
]
