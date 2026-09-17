"""Read-only loader for one finished campaign output directory.

It opens nothing but existing immutable files, records the exact sha256 of every
byte it read, and reduces the owner journal with the unchanged
`reduce_m6_run`. Nothing is fetched, written, mutated, or queried: no network,
no database, no remote store. An absent or unreadable proof becomes a named
`unknowns` entry and a `None` fact - never a default that could let an
unproven run score as delivered. Only a conflict between two inputs that must
agree (spec, intent, evaluation plan, manifest, quality plan) raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Literal, cast

from disclosure_anchor.adapters.runtime.m6_campaign_assembly import (
    CampaignIdentityError, CampaignInputError, external_exit_problems, launcher_lines,
)
from disclosure_anchor.adapters.runtime.m6_public_consumer_verifier import _audit
from disclosure_anchor.adapters.runtime.resident_owner_evidence import (
    OWNER_RESULT_CONTRACT_VERSION, replay_resident_owner_evidence,
)
from disclosure_anchor.adapters.runtime.synchronized_telemetry_observer import (
    verify_synchronized_telemetry_observer,
)
from disclosure_anchor.application.contracts.closed_document import sha256_of
from disclosure_anchor.application.contracts.resident_combined_cpu import check_combined_resident_cpu_v4
from disclosure_anchor.application.contracts.resident_session_evidence import check_resident_observer_mapping_v4
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    SynchronizedTelemetryFrameV2, SynchronizedTelemetryFrameV3, SynchronizedTelemetryReceiptV4, SynchronizedTelemetrySealV4,
)
from disclosure_anchor.application.contracts.m6_campaign import M6CorpusManifest
from disclosure_anchor.application.contracts.m6_campaign_intent import (
    M6_CAMPAIGN_INTENT_MAX_BYTES, M6CampaignIntent, M6CampaignIntentV1, decode_campaign_intent,
)
from disclosure_anchor.application.contracts.m6_document_qualification import (
    M6QualificationEvidence, M6QualityPlan,
)
from disclosure_anchor.application.contracts.m6_evaluation_plan import M6EvaluationPlan
from disclosure_anchor.application.contracts.m6_owner import M6OwnerAnchor, bind_physical_owner_boot
from disclosure_anchor.application.contracts.m6_run import M6RunReceipt, M6RunSpec, M6SourceHistoryFact
from disclosure_anchor.application.contracts.m6_run_events import (
    M6AttemptAdmitted, M6PublicConfirmation, M6RunClosed, M6RunEvent,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.services.m6_delivery_report import (
    ClockBinding, ExternalExitFacts, RunClosureFacts, StageNoteFact, StageTimingFacts, TelemetryFacts,
    VerifierAttemptFact,
)
from disclosure_anchor.application.services.m6_run_accounting import reduce_m6_run
from disclosure_anchor.application.services.m6_run_spec_factory import build_run_spec
from disclosure_anchor.application.services.telemetry_resource_aggregates import (
    derive_resource_aggregates, derive_resource_aggregates_v4,
)

_MAX_RUN_FILE_BYTES = 65536
_MAX_INPUT_BYTES = 8 * 1024 * 1024
_MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
_MAX_RECORD_BYTES = 1024 * 1024
_HISTORY_AUDIT_CONTRACT = "m6.private-source-history-audit.v1"
# `coverage_gaps`/`base_identity_conflicts` are audit-only projections that the
# bound fact does not carry; `m6_public_consumer_verifier._bind_inputs` drops exactly these.
_AUDIT_ONLY_PROJECTION_FIELDS = frozenset({"base_identity_conflicts", "coverage_gaps"})
_MAX_STAGE_EVENTS_BYTES = 64 * 1024 * 1024
_MAX_STAGE_EVENT_LINES = 1_000_000
_MAX_TELEMETRY_FRAMES_BYTES = 256 * 1024 * 1024
_STAGE_NOTE_FIELDS = frozenset({"attempt_id", "lane", "kind", "monotonic_ns", "scalars"})
# The note kinds this measurement contract owns; any other kind is another contract's and is ignored.
_STAGE_NOTE_KINDS = frozenset({
    "remote_post_send", "remote_terminal_observed", "remote_terminal_failed", "observation_closed",
})
_OBSERVATION_COUNT_NAMES: tuple[str, ...] = (
    "dropped", "guard_failures", "join_timeout", "late_notes", "note_errors", "truncated", "writer_errors",
)
# The exact private files one sealed v3 observer run holds.
_TELEMETRY_ARTIFACTS: tuple[tuple[str, int], ...] = (
    ("frames.v2.jsonl", _MAX_TELEMETRY_FRAMES_BYTES), ("receipt.v3.json", _MAX_INPUT_BYTES),
    ("seal.v3.json", _MAX_INPUT_BYTES),
)
_TELEMETRY_ARTIFACTS_V4: tuple[tuple[str, int], ...] = (
    ("frames.v3.jsonl", _MAX_TELEMETRY_FRAMES_BYTES), ("receipt.v4.json", _MAX_INPUT_BYTES),
    ("seal.v4.json", _MAX_INPUT_BYTES), ("sampling-plan.v1.json", _MAX_INPUT_BYTES),
)
LOCAL_MEASUREMENT_WINDOW_CONTRACT = "m6.local-measurement-window.v1"


@dataclass(frozen=True, slots=True)
class CampaignEvidence:
    """Everything one campaign output directory can prove about its own run."""

    intent: M6CampaignIntent | M6CampaignIntentV1 | None
    spec: M6RunSpec
    manifest: M6CorpusManifest
    quality_plan: M6QualityPlan
    receipt: M6RunReceipt | None
    events: tuple[M6RunEvent, ...]
    closure: RunClosureFacts
    external: ExternalExitFacts
    telemetry: TelemetryFacts | None
    stage_timing: StageTimingFacts
    inputs: tuple[tuple[str, str], ...]
    unknowns: tuple[str, ...]


_DIGEST_TEXT = re.compile(r"sha256:[0-9a-f]{64}")


class _Reader:
    """Reads existing files only, hashing each one and naming every proof it could not read."""

    def __init__(self, run_dir: Path) -> None:
        self._run_dir = run_dir
        self._inputs: dict[str, str] = {}
        self._unknowns: set[str] = set()

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    def note(self, name: str) -> None:
        self._unknowns.add(name)

    def label(self, path: Path) -> str:
        try:
            return path.relative_to(self._run_dir).as_posix()
        except ValueError:
            return path.as_posix()

    def record(self, path: Path, raw: bytes) -> str:
        digest = sha256_of(raw)
        self._inputs[self.label(path)] = digest
        return digest

    def record_digest(self, path: Path, digest: str) -> str:
        """Index a digest another bounded reader already computed over the original bytes.

        The bytes are not reread (a second read could see a changed file) and the
        digest text is never hashed again; only its canonical form is checked.
        """
        if _DIGEST_TEXT.fullmatch(digest) is None:
            raise CampaignIdentityError(f"evidence digest for {self.label(path)} is not a canonical SHA-256")
        self._inputs[self.label(path)] = digest
        return digest

    def digest(self, path: Path) -> str | None:
        return self._inputs.get(self.label(path))

    def read(self, path: Path, *, absent: str, maximum: int) -> bytes | None:
        """The exact bytes, hashed into the evidence index; absent/unreadable/oversized is an unknown."""
        if path.is_symlink() or not path.is_file():
            self._unknowns.add(absent)
            return None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise OSError("evidence is not an ordinary file")
                raw = source.read(maximum + 1)
                after = os.fstat(source.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise OSError("evidence changed while being read")
        except OSError as exc:
            self._unknowns.add(f"{absent[:-7] if absent.endswith('_absent') else absent}_unreadable:{type(exc).__name__}")
            return None
        if len(raw) > maximum:
            self._unknowns.add(f"{absent[:-7] if absent.endswith('_absent') else absent}_exceeds_byte_bound")
            return None
        self.record(path, raw)
        return raw

    def document(self, path: Path, *, absent: str, maximum: int) -> dict[str, Any] | None:
        raw = self.read(path, absent=absent, maximum=maximum)
        if raw is None:
            return None
        try:
            value = strict_json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            self._unknowns.add(f"{self.label(path)}_unreadable:{type(exc).__name__}")
            return None
        if type(value) is not dict:
            self._unknowns.add(f"{self.label(path)}_is_not_an_object")
            return None
        return value

    @property
    def inputs(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._inputs.items()))

    @property
    def unknowns(self) -> tuple[str, ...]:
        return tuple(sorted(self._unknowns))


def _integer(value: object) -> int | None:
    return value if type(value) is int else None


def _boolean(value: object) -> bool | None:
    return value if type(value) is bool else None


def _hash(value: object) -> str | None:
    return value if type(value) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", value) else None


def _mapping(value: object) -> dict[str, Any]:
    return value if type(value) is dict else {}


def _resolve_pinned(
    reader: _Reader, *, expected_sha256: str, label: str, maximum: int,
    explicit: Path | None, recorded: object, hashes: dict[str, str],
) -> bytes:
    """Read one input pinned by hash: the caller's path, the recorded path, then the driver's hash index."""
    candidates: list[Path] = []
    for item in (explicit, recorded if type(recorded) is str else None):
        if item is not None:
            path = Path(item)
            if path.is_absolute():
                candidates.append(path)
    candidates.extend(Path(path) for path, digest in sorted(hashes.items())
                      if digest == expected_sha256 and Path(path).is_absolute())
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            continue
        raw = reader.read(path, absent="pinned_input_unreadable", maximum=maximum)
        if raw is None:
            raise CampaignInputError(f"{label} is unreadable or exceeds its byte bound")
        if sha256_of(raw) != expected_sha256:
            raise CampaignIdentityError(f"{label} bytes differ from the identity pinned by the run spec")
        reader.record(path, raw)
        return raw
    raise CampaignInputError(
        f"{label} could not be resolved; pass its path explicitly (expected {expected_sha256})",
    )


def _journal_lines(path: Path, spec: M6RunSpec) -> list[bytes]:
    """Bounded `readline` over the physical append order, exactly as the reducer's source adapter must."""
    resources = spec.resources
    lines: list[bytes] = []
    consumed = 0
    with path.open("rb") as handle:
        while len(lines) <= resources.max_events and consumed <= resources.max_log_bytes:
            raw = handle.readline(resources.max_record_bytes + 2)
            if not raw:
                break
            lines.append(raw)
            consumed += len(raw)
    return lines


def _decode_events(spec: M6RunSpec, lines: list[bytes]) -> tuple[M6RunEvent, ...]:
    """Decode exactly the prefix the reducer consumes; a malformed record is skipped, not repaired."""
    resources = spec.resources
    events: list[M6RunEvent] = []
    count = consumed = 0
    for raw in lines:
        if (count >= resources.max_events or consumed + len(raw) > resources.max_log_bytes
                or len(raw) > resources.max_record_bytes + 1):
            break
        count += 1
        consumed += len(raw)
        if not raw.endswith(b"\n"):
            break
        try:
            events.append(M6RunEvent.from_canonical_bytes(raw[:-1], maximum_bytes=resources.max_record_bytes))
        except ValueError:
            continue
    return tuple(events)


def _bind_public_confirmation(
    reader: _Reader, path: Path, confirmation: M6PublicConfirmation, attempt: str,
) -> None:
    """The persisted confirmation must be the exact record the owner stamped for that attempt."""
    raw = reader.read(path, absent=f"public_confirmation_absent:{attempt}", maximum=_MAX_EVIDENCE_BYTES)
    if raw is None:
        return
    try:
        persisted = M6PublicConfirmation.from_canonical_bytes(raw, maximum_bytes=_MAX_EVIDENCE_BYTES)
    except ValueError as exc:
        reader.note(f"public_confirmation_unusable:{attempt}:{type(exc).__name__}")
        return
    if persisted != confirmation:
        raise CampaignIdentityError(
            f"the persisted public confirmation for {attempt} differs from the owner-stamped journal record",
        )


def _bind_native_evidence(reader: _Reader, run_dir: Path, spec: M6RunSpec) -> None:
    """Hash-record the owner's own copies of the run identity; a different spec is a conflict, not a warning."""
    native = run_dir / "native"
    native_spec = reader.read(native / "spec.json", absent="native_spec_absent", maximum=_MAX_RUN_FILE_BYTES)
    if native_spec is not None and sha256_of(native_spec) != spec.canonical_sha256():
        raise CampaignIdentityError("the owner's private spec.json differs from run/run-spec.json")
    native_anchor = reader.read(native / "anchor.json", absent="native_anchor_absent", maximum=_MAX_RUN_FILE_BYTES)
    if native_anchor is not None:
        try:
            M6OwnerAnchor.from_canonical_bytes(native_anchor, maximum_bytes=_MAX_RUN_FILE_BYTES).assert_spec(spec)
        except ValueError as exc:
            raise CampaignIdentityError(f"the owner's private anchor differs from the frozen spec: {exc}") from exc
    # The owner's exit observation is its own account of shutdown and explicitly does not
    # self-attest external process closure; it is recorded as evidence, never as exit proof.
    reader.read(native / "exit-observation.json", absent="native_exit_observation_absent",
                maximum=_MAX_RUN_FILE_BYTES)
    reader.read(run_dir / "verifier" / "public-inputs.json", absent="verifier_public_inputs_absent",
                maximum=_MAX_INPUT_BYTES)


def _history_facts(
    reader: _Reader, run_dir: Path, events: tuple[M6RunEvent, ...],
) -> tuple[M6SourceHistoryFact, ...]:
    """Rebuild each source's history fact from its private audit rows, bound by `audit_receipt_sha256`.

    The binding is the one `m6_public_consumer_verifier._bind_inputs` performs:
    the audit bytes must hash to the receipt the public confirmation names, the
    audit must be its exact canonical read-only contract, and exactly one
    projection row for the admitted source rebuilds the fact.
    """
    sources = {record.event.payload.attempt_id: record.event.payload.source_pdf_sha256
               for record in events if isinstance(record.event.payload, M6AttemptAdmitted)}
    public_dir = run_dir / "verifier" / "public"
    facts: dict[str, M6SourceHistoryFact] = {}
    bound: set[str] = set()
    for record in events:
        confirmation = record.event.payload
        if not isinstance(confirmation, M6PublicConfirmation):
            continue
        attempt = confirmation.attempt_id
        bound.add(attempt)
        _bind_public_confirmation(reader, public_dir / attempt / "public-confirmation.json", confirmation, attempt)
        source = sources.get(attempt)
        if source is None or source in facts:
            continue
        path = public_dir / attempt / "private-history-audit.json"
        raw = reader.read(path, absent=f"private_history_audit_absent:{attempt}", maximum=_MAX_EVIDENCE_BYTES)
        if raw is None:
            continue
        digest = sha256_of(raw)
        if digest != confirmation.history_audit_receipt_sha256:
            reader.note(f"private_history_audit_receipt_mismatch:{attempt}")
            continue
        try:
            audit = _audit(raw, digest, _HISTORY_AUDIT_CONTRACT)
            projections = audit["projections"]
            if type(projections) is not list:
                raise ValueError("history audit carries no projection rows")
            rows = [row for row in projections
                    if type(row) is dict and row.get("source_pdf_sha256") == source]
            if len(rows) != 1:
                raise ValueError("history audit does not project the admitted source exactly once")
            facts[source] = M6SourceHistoryFact.model_validate({
                **{key: value for key, value in rows[0].items()
                   if key not in _AUDIT_ONLY_PROJECTION_FIELDS},
                "audit_receipt_sha256": digest,
            })
        except (ValueError, KeyError) as exc:
            reader.note(f"private_history_audit_unusable:{attempt}:{type(exc).__name__}")
    if public_dir.is_dir():
        for directory in sorted(public_dir.iterdir()):
            if directory.is_dir() and directory.name not in bound:
                reader.note(f"private_history_audit_unbound:{directory.name}")
    return tuple(facts[key] for key in sorted(facts))


def _qualifications(reader: _Reader, run_dir: Path) -> tuple[M6QualificationEvidence, ...]:
    """Every immutable whole-document qualification evidence the quality verifier persisted."""
    quality_dir = run_dir / "verifier" / "quality"
    if not quality_dir.is_dir():
        reader.note("verifier_quality_evidence_absent")
        return ()
    found: dict[str, M6QualificationEvidence] = {}
    for directory in sorted(quality_dir.iterdir()):
        if not directory.is_dir():
            continue
        attempt = directory.name
        raw = reader.read(directory / "qualification-evidence.json",
                          absent=f"qualification_evidence_absent:{attempt}", maximum=_MAX_EVIDENCE_BYTES)
        if raw is None:
            continue
        try:
            evidence = M6QualificationEvidence.from_canonical_bytes(raw, maximum_bytes=_MAX_EVIDENCE_BYTES)
        except ValueError as exc:
            reader.note(f"qualification_evidence_unusable:{attempt}:{type(exc).__name__}")
            continue
        found[evidence.canonical_sha256()] = evidence
    return tuple(found[key] for key in sorted(found))


def _closure_facts(
    reader: _Reader, run_dir: Path, events: tuple[M6RunEvent, ...], verifier: dict[str, Any] | None,
) -> RunClosureFacts:
    """The runner's control receipts, the owner's archived sidecars and the journal's own close."""
    receipt_path = run_dir / "runner" / "campaign-receipt.json"
    receipt = reader.document(receipt_path, absent="runner_campaign_receipt_absent", maximum=_MAX_INPUT_BYTES)
    assembly = _mapping(receipt.get("m6_assembly")) if receipt is not None else {}
    closure = _mapping(assembly.get("closure"))
    admission = reader.document(run_dir / "native" / "admission-closed.json",
                                absent="admission_closed_sidecar_absent", maximum=_MAX_RUN_FILE_BYTES)
    resources = reader.document(run_dir / "native" / "resources-closed.json",
                                absent="resources_closed_sidecar_absent", maximum=_MAX_RUN_FILE_BYTES)
    drain = reader.read(run_dir / "verifier" / "drain-receipt.json",
                        absent="verifier_drain_receipt_absent", maximum=_MAX_INPUT_BYTES)
    reason: Literal["deadline_drained", "stop_requested", "failed"] | None = None
    for record in events:
        payload = record.event.payload
        if isinstance(payload, M6RunClosed):
            reason = payload.reason
    status = verifier.get("status") if verifier is not None else None
    return RunClosureFacts(
        runner_receipt_sha256=None if receipt is None else reader.digest(receipt_path),
        runner_status=assembly.get("status") if type(assembly.get("status")) is str else None,
        closure_complete=_boolean(closure.get("complete")),
        ownership_closure_sha256=_hash(closure.get("ownership_closure_sha256")),
        residual_count=_integer(closure.get("residual_count")),
        children_exited=_boolean(closure.get("children_exited")),
        admitted_attempt_count=_integer(closure.get("admitted_attempt_count")),
        final_attempt_count=_integer(closure.get("final_attempt_count")),
        admission_reconciliation_sha256=_hash(closure.get("admission_reconciliation_sha256")),
        admission_closed_admitted_count=None if admission is None else _integer(admission.get("admitted_attempt_count")),
        admission_closed_unresolved_count=None if admission is None else _integer(admission.get("unresolved_claim_count")),
        admission_closed_last_producer_sequence=None if admission is None else _integer(admission.get("last_producer_sequence")),
        admission_closed_reconciliation_sha256=None if admission is None else _hash(admission.get("reconciliation_receipt_sha256")),
        resources_closed_residual_count=None if resources is None else _integer(resources.get("residual_count")),
        resources_closed_children_exited=None if resources is None else _boolean(resources.get("children_exited")),
        resources_closed_receipt_sha256=None if resources is None else _hash(resources.get("ownership_receipt_sha256")),
        verifier_summary_status=status if type(status) is str else None,
        drain_receipt_sha256=None if drain is None else sha256_of(drain),
        run_closed_reason=reason,
    )


def _external_facts(
    reader: _Reader, run_dir: Path, summary: dict[str, Any] | None, *,
    intent: M6CampaignIntent | M6CampaignIntentV1 | None = None,
    anchor: M6OwnerAnchor | None = None, inputs: dict[str, Any] | None = None,
) -> ExternalExitFacts:
    """Recompute exit verification from original launch/start/READY/exit bytes.

    The existing launcher-command artifact carries a non-secret, pre-spawn
    projection of the same expected inputs used online. A summary's success
    boolean is never positive evidence; an explicit recorded failure stays visible.
    """
    records = run_dir / "launcher-records"
    record = reader.document(records / "process-exit.json", absent="owner_external_exit_record_absent", maximum=_MAX_RECORD_BYTES)
    start = reader.document(records / "process-start.json", absent="owner_external_start_record_absent", maximum=_MAX_RECORD_BYTES)
    ready = reader.read(records / "ready.json", absent="owner_external_ready_record_absent", maximum=_MAX_RUN_FILE_BYTES)
    observed_ready = reader.read(run_dir / "ready.json", absent="owner_transport_ready_record_absent", maximum=_MAX_RUN_FILE_BYTES)
    launch = reader.document(run_dir / "launcher-command.json", absent="owner_launch_command_absent", maximum=_MAX_RECORD_BYTES)
    expected = _mapping(None if launch is None else launch.get("expected_start"))
    problems: list[str] = []
    if intent is None or anchor is None:
        problems.append("owner_launch_frozen_identity_absent")
    else:
        if launch is None or launch.get("intent_sha256") != intent.canonical_sha256():
            problems.append("owner_launch_intent_mismatch")
        pinned = {
            "run_id": intent.run.run_id, "planned_seconds": intent.run.planned_seconds,
            "close_grace_seconds": intent.close_grace_seconds, "memory_bytes": intent.memory_bytes,
            "bootstrap_bind_seconds": intent.bootstrap_bind_seconds, "ready_wait_seconds": intent.ready_wait_seconds,
        }
        for key, value in pinned.items():
            if type(expected.get(key)) is not type(value) or expected.get(key) != value:
                problems.append("owner_launch_intent_mismatch:" + key)
    attempt = None if inputs is None else inputs.get("attempt_id")
    if type(attempt) is not str or not attempt or expected.get("attempt_id") != attempt:
        problems.append("owner_launch_attempt_unbound")
    printed = None
    stdout_path = run_dir / "launcher-stdout.raw"
    if stdout_path.exists():
        stdout = reader.read(stdout_path, absent="launcher_stdout_absent", maximum=_MAX_RECORD_BYTES)
        if stdout is not None:
            try:
                lines = launcher_lines(stdout, "M6-EXIT")
                if len(lines) > 1:
                    problems.append("owner_external_exit_duplicate_printed_record")
                elif lines:
                    value = strict_json_loads(lines[0])
                    if type(value) is not dict:
                        problems.append("owner_external_exit_printed_record_malformed")
                    else:
                        printed = value
            except ValueError:
                problems.append("owner_external_exit_printed_record_malformed")
    problems.extend(external_exit_problems(
        record=record or {}, start=start or {}, expected_start=expected,
        ready_raw=ready, observed_ready_raw=observed_ready, expected_anchor=anchor, printed_exit=printed,
    ))
    summary = summary or {}
    declared = _mapping(_mapping(_mapping(summary.get("children")).get("launcher")).get("external_exit"))
    if record is not None and any(key in declared and declared[key] != record.get(key)
                                 for key in ("exit_code", "forced_termination", "cancel", "pid",
                                             "creation_filetime_100ns", "process_handle_signaled",
                                             "ready_received", "parent_failure")):
        problems.append("owner_external_exit_record_disagrees_with_summary")
    for key in ("run_id", "attempt_id"):
        if key in summary and summary[key] != expected.get(key):
            problems.append("owner_external_exit_summary_mismatch:" + key)
    cleanup = summary.get("cleanup_failures")
    failures = list(sorted(cleanup)) if type(cleanup) is dict else ["campaign_summary_cleanup_unproven"]
    if summary.get("first_error") is not None:
        failures.append("campaign_first_error")
    if summary.get("owner_external_exit_verified") is False:
        problems.append("owner_external_exit_online_failure")
    for problem in problems:
        reader.note("owner_external_exit_unverified:" + problem)
    cancel = None if record is None else record.get("cancel")
    return ExternalExitFacts(
        verified=not problems,
        exit_code=None if record is None else _integer(record.get("exit_code")),
        forced_termination=None if record is None else _boolean(record.get("forced_termination")),
        cancel=cancel if type(cancel) is str else None,
        local_children_reaped=summary.get("local_children_reaped") is True,
        cleanup_failures=tuple(sorted(set(failures))),
    )


def _resident_boot_alias(reader: _Reader, anchor: M6OwnerAnchor) -> str | None:
    """One copied original native metadata/body pair, not a caller-supplied alias."""
    records = _physical_owner_records(reader)
    if records is None:
        return None
    metadata_raw, body_raw, start = records
    try:
        return bind_physical_owner_boot(
            metadata_raw=metadata_raw, body_raw=body_raw, anchor=anchor,
            pid=start.get("pid"), creation_filetime_100ns=start.get("creation_filetime_100ns"),
        )
    except (ValueError, TypeError) as exc:
        reader.note("physical_owner_identity_binding_invalid:" + type(exc).__name__)
        return None


def _physical_owner_node_identity(reader: _Reader) -> str | None:
    """The Windows node identity the original physical owner identity body recorded, if readable."""
    records = _physical_owner_records(reader)
    if records is None:
        return None
    try:
        body = strict_json_loads(records[1])
    except ValueError:
        return None
    node = body.get("windows_node_identity_sha256") if type(body) is dict else None
    return node if type(node) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", node) else None


def _physical_owner_records(reader: _Reader) -> tuple[bytes, bytes, dict[str, Any]] | None:
    """The singleton owner-identity metadata, its body and the external start record."""
    native = reader.run_dir / "native"
    if not native.is_dir() or native.is_symlink():
        reader.note("physical_owner_identity_absent")
        return None
    from itertools import islice

    paths = sorted(islice(native.glob("diagnostic-*.json"), 4097))
    if len(paths) > 4096:
        reader.note("physical_owner_identity_metadata_bound")
        return None
    matches: list[tuple[Path, bytes]] = []
    for path in paths:
        if re.fullmatch(r"diagnostic-[0-9a-f]{32}\.json", path.name) is None:
            reader.note("physical_owner_identity_metadata_name")
            return None
        raw = reader.read(path, absent="physical_owner_metadata_absent", maximum=1024)
        if raw is None:
            reader.note("physical_owner_identity_metadata_unreadable")
            return None
        try:
            value = strict_json_loads(raw)
        except ValueError:
            reader.note("physical_owner_identity_metadata_malformed")
            return None
        if type(value) is not dict:
            reader.note("physical_owner_identity_metadata_malformed")
            return None
        if value.get("code") == "owner_identity":
            matches.append((path, raw))
    if len(matches) != 1:
        reader.note("physical_owner_identity_not_singleton")
        return None
    path, metadata_raw = matches[0]
    body_raw = reader.read(path.with_suffix(".bin"), absent="physical_owner_identity_body_absent", maximum=65536)
    start = reader.document(reader.run_dir / "launcher-records/process-start.json",
                            absent="physical_owner_external_start_absent", maximum=_MAX_RECORD_BYTES)
    if body_raw is None or start is None:
        return None
    return metadata_raw, body_raw, start


def _clock_binding(value: object) -> ClockBinding | None:
    """The exact three-field binding both processes must record, or nothing."""
    binding = _mapping(value)
    source, implementation = binding.get("source"), binding.get("implementation")
    boot = binding.get("boot_session_uuid")
    if type(source) is not str or type(implementation) is not str or type(boot) is not str:
        return None
    if (set(binding) != {"source", "implementation", "boot_session_uuid"}
            or source != "python.time.monotonic_ns" or not implementation or len(implementation) > 128
            or re.fullmatch(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", boot) is None):
        return None
    return ClockBinding(source=source, implementation=implementation, boot_session_uuid=boot)


def _stage_note(line: bytes, number: int) -> StageNoteFact | None:
    """One exact stage-note object, or `None` when the line is not one."""
    try:
        value = strict_json_loads(line)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if type(value) is not dict or set(value) != _STAGE_NOTE_FIELDS:
        return None
    attempt, lane, kind = value["attempt_id"], value["lane"], value["kind"]
    instant, scalars = value["monotonic_ns"], value["scalars"]
    if (attempt is not None and type(attempt) is not str) or (lane is not None and type(lane) is not str):
        return None
    if type(kind) is not str or type(instant) is not int or instant < 0 or type(scalars) is not dict:
        return None
    if any(value is not None and (not 1 <= len(value) <= 128
               or re.search(r"[\x00-\x20\x7f]", value)) for value in (attempt, lane, kind)):
        return None
    if any(item is not None and type(item) not in (int, str) for item in scalars.values()):
        return None
    return StageNoteFact(line=number, attempt_id=attempt, lane=lane, kind=kind, monotonic_ns=instant,
                         scalars=MappingProxyType(dict(scalars)))


def _stage_notes(
    reader: _Reader, path: Path,
) -> tuple[tuple[StageNoteFact, ...], tuple[str, ...], bool, int, int]:
    """Read the runner's note stream in file order; a malformed line is a problem, never a skip."""
    before = set(reader.unknowns)
    raw = reader.read(path, absent="stage_events_absent", maximum=_MAX_STAGE_EVENTS_BYTES)
    added = sorted(set(reader.unknowns) - before)
    if raw is None:
        # Plain absence is reported through the observation status; anything else is a measurement problem.
        return (), tuple(name for name in added if name != "stage_events_absent"), False, 0, 0
    problems: list[str] = []
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    elif lines:
        problems.append("stage_events_unterminated_tail")
    record_count = len(lines)
    if len(lines) > _MAX_STAGE_EVENT_LINES:
        problems.append("stage_events_exceeds_line_bound")
        lines = lines[:_MAX_STAGE_EVENT_LINES]
    notes: list[StageNoteFact] = []
    for number, line in enumerate(lines, 1):
        note = _stage_note(line, number)
        if note is None:
            problems.append(f"stage_events_malformed_line:{number}")
        elif note.kind in _STAGE_NOTE_KINDS:
            notes.append(note)
    return tuple(notes), tuple(problems), True, len(raw), record_count


def _verifier_attempts(
    document: dict[str, Any] | None,
) -> tuple[tuple[VerifierAttemptFact, ...], tuple[str, ...]]:
    """The verifier's per-attempt records, wherever its summary carries them."""
    if document is None:
        return (), ()
    entries: object = document.get("attempts")
    for fallback in ("supervisor", "closure"):
        if type(entries) is not list:
            entries = _mapping(document.get(fallback)).get("attempts")
    if type(entries) is not list:
        return (), ("verifier_attempts_absent",)
    facts: list[VerifierAttemptFact] = []
    problems: list[str] = []
    for position, entry in enumerate(entries):
        record = _mapping(entry)
        attempt = record.get("attempt_id")
        started, finished = _integer(record.get("started_ns")), _integer(record.get("finished_ns"))
        if (type(attempt) is not str or not 1 <= len(attempt) <= 128
                or re.search(r"[\x00-\x20\x7f]", attempt) or started is None or finished is None):
            problems.append(f"verifier_attempt_malformed:{position}")
            continue
        facts.append(VerifierAttemptFact(
            attempt_id=attempt, confirmed=_mapping(record.get("public")).get("confirmed") is True,
            public_ns=_integer(record.get("public_ns")),
            public_confirmation_sha256=_hash(record.get("public_confirmation_sha256")),
            started_ns=started, finished_ns=finished,
        ))
    return tuple(facts), tuple(problems)


def _stage_timing_facts(
    reader: _Reader, run_dir: Path, verifier_summary: dict[str, Any] | None, *, spec: M6RunSpec,
) -> StageTimingFacts:
    """The Mac stage observation and the verifier's exact public samples, with every gap named."""
    observation = run_dir / "runner" / "observation"
    notes, problems_tuple, present, byte_count, record_count = _stage_notes(reader, observation / "stage-events.jsonl")
    summary = reader.document(observation / "observation-summary.json",
                              absent="stage_observation_summary_absent", maximum=_MAX_INPUT_BYTES)
    problems = list(problems_tuple)
    status: Literal["complete", "partial", "invalid"] | None = None
    counts: dict[str, int] = {}
    runner_clock: ClockBinding | None = None
    if summary is not None:
        declared = summary.get("measurement_status")
        if declared == "complete":
            status = "complete"
        elif declared == "partial":
            status = "partial"
        elif declared == "invalid":
            status = "invalid"
        else:
            problems.append("stage_observation_status_unreadable")
        for name in _OBSERVATION_COUNT_NAMES:
            value = _integer(summary.get(name))
            if value is None or value < 0:
                problems.append("stage_observation_count_unreadable:" + name)
            else:
                counts[name] = value
        for name, actual in (("events_written", record_count), ("bytes_written", byte_count)):
            declared_count = _integer(summary.get(name))
            if declared_count is None or declared_count < 0:
                problems.append("stage_observation_count_unreadable:" + name)
            else:
                counts[name] = declared_count
                if declared_count != actual:
                    problems.append("stage_observation_count_mismatch:" + name)
        # These are the existing writer's close facts, not a second observation log.
        closed = [note for note in notes if note.kind == "observation_closed"]
        started_ns = _integer(summary.get("started_monotonic_ns"))
        closed_ns = _integer(summary.get("closed_monotonic_ns"))
        if (summary.get("contract_version") != "staged-observation-summary.v1"
                or summary.get("writer_thread_alive") is not False
                or summary.get("events_file") != "stage-events.jsonl"
                or _integer(summary.get("summary_write_error")) != 0):
            problems.append("stage_writer_close_unproven")
        if (len(closed) != 1 or closed[0].line != record_count
                or closed[0].attempt_id is not None or closed[0].lane is not None
                or closed[0].scalars or closed[0].monotonic_ns != closed_ns):
            problems.append("stage_close_record_unproven")
        if (started_ns is None or closed_ns is None or started_ns < 0 or closed_ns < started_ns
                or any(not started_ns <= note.monotonic_ns <= closed_ns for note in notes)):
            problems.append("stage_observation_interval_invalid")
        runner_clock = _clock_binding(summary.get("clock"))
        if runner_clock is None:
            problems.append("stage_runner_clock_unreadable")
        if not present:
            problems.append("stage_events_absent")
    verifier_clock = None if verifier_summary is None else _clock_binding(verifier_summary.get("clock"))
    if verifier_summary is not None and verifier_clock is None:
        problems.append("stage_verifier_clock_unreadable")
    verifier_run = _mapping(None if verifier_summary is None else verifier_summary.get("m6_run"))
    if (verifier_run.get("run_id") != spec.run_id
            or verifier_run.get("spec_sha256") != spec.canonical_sha256()):
        problems.append("stage_verifier_run_spec_unbound")
    attempts, attempt_problems = _verifier_attempts(verifier_summary)
    problems.extend(attempt_problems)
    return StageTimingFacts(
        observation_status=status, observation_counts=MappingProxyType(counts),
        runner_clock=runner_clock, verifier_clock=verifier_clock, notes=notes,
        verifier_attempts=attempts, problems=tuple(sorted(set(problems))),
    )


def _coverage_window(summary: dict[str, Any] | None) -> tuple[datetime, datetime] | None:
    """The assembly's own UTC brackets: coverage only, never a duration subtracted across machines."""
    if summary is None:
        return None
    started_raw, finished_raw = summary.get("started_utc"), summary.get("finished_utc")
    if type(started_raw) is not str or type(finished_raw) is not str:
        return None
    try:
        started, finished = datetime.fromisoformat(started_raw), datetime.fromisoformat(finished_raw)
    except ValueError:
        return None
    if started.tzinfo is None or finished.tzinfo is None or finished < started:
        return None
    return started, finished


def _measurement_window(summary: dict[str, Any] | None) -> tuple[str, int, int] | None:
    """The assembly's own local monotonic window (domain, start, finish); never a cross-host difference."""
    if summary is None:
        return None
    window = summary.get("telemetry_window")
    if type(window) is not dict or window.get("contract_version") != LOCAL_MEASUREMENT_WINDOW_CONTRACT:
        return None
    domain, started, finished = window.get("clock_domain_identity_sha256"), window.get("started_monotonic_ns"), window.get("finished_monotonic_ns")
    if (type(domain) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", domain) is None
            or type(started) is not int or type(finished) is not int or finished <= started or "problem" in window):
        return None
    return domain, started, finished


def _telemetry_facts_v4(
    reader: _Reader, *, artifact_root: Path, run_id: str, spec: M6RunSpec,
    summary: dict[str, Any] | None, anchor: M6OwnerAnchor, resident_owner_evidence_dir: Path | None,
) -> TelemetryFacts:
    """Replay a v4 (R22) observer run: frozen plan, fresh-pull frames, local monotonic window, owner evidence."""
    try:
        result = verify_synchronized_telemetry_observer(artifact_root=artifact_root, run_id=run_id, receipt_version=4)
    except ValueError as exc:
        raise CampaignIdentityError(f"synchronized telemetry evidence failed its own replay: {exc}") from exc
    run_directory = artifact_root / run_id
    for name, bound in _TELEMETRY_ARTIFACTS_V4:
        reader.read(run_directory / name, absent="telemetry_artifact_absent:" + name, maximum=bound)
    receipt, seal, plan = result.receipt, result.seal, result.plan
    if not isinstance(receipt, SynchronizedTelemetryReceiptV4) or not isinstance(seal, SynchronizedTelemetrySealV4) or plan is None:
        raise CampaignIdentityError("telemetry evidence is not a v4 observer run")
    frames = tuple(frame for frame in result.frames if isinstance(frame, SynchronizedTelemetryFrameV3))
    if len(frames) != len(result.frames):
        raise CampaignIdentityError("v4 telemetry evidence carries non-v3 frames")
    problems: list[str] = []
    if receipt.runtime_bundle_identity_sha256 != spec.runtime.runtime_bundle_identity_sha256:
        problems.append("telemetry_runtime_bundle_mismatch")
    if receipt.process_profile.process_profile_sha256 != spec.runtime.process_profile_sha256:
        problems.append("telemetry_process_profile_mismatch")
    if receipt.status != "complete":
        problems.append("telemetry_receipt_not_complete")
    if seal.status != "complete":
        problems.append("telemetry_seal_not_complete")
    boots = {frame.resident_exporter_provenance.boot_identity_sha256 for frame in frames}
    boot_alias = None
    if boots - {spec.clock.boot_identity_sha256}:
        boot_alias = _resident_boot_alias(reader, anchor)
        if boot_alias is None:
            problems.append("telemetry_native_resident_boot_binding_unproven")
    for frame in frames:
        values = frame.gpu.values
        if (frame.gpu.status == "supported" and values is not None
                and values.device_identity_sha256 != spec.runtime.gpu_device_identity_sha256):
            problems.append("telemetry_gpu_device_mismatch")
        provenance = frame.resident_exporter_provenance
        if (provenance.host_assignment_identity_sha256 != spec.clock.host_assignment_identity_sha256
                or provenance.boot_identity_sha256 not in {spec.clock.boot_identity_sha256, boot_alias}):
            problems.append("telemetry_host_identity_mismatch")
    window = _measurement_window(summary)
    aggregates = None
    if window is None:
        problems.append("telemetry_coverage_window_absent")
    else:
        domain, started, finished = window
        if domain != receipt.clock_domain_identity_sha256 or domain != plan.observer_clock_domain_identity_sha256:
            problems.append("telemetry_window_domain_mismatch")
        else:
            if receipt.started_monotonic_ns > started:
                problems.append("telemetry_coverage_incomplete:receipt_start")
            if receipt.planned_end_monotonic_ns < finished:
                problems.append("telemetry_coverage_incomplete:planned_end")
            aggregates = derive_resource_aggregates_v4(
                frames, plan=plan, window_started_monotonic_ns=started, window_finished_monotonic_ns=finished,
            )
    if resident_owner_evidence_dir is None:
        problems.append("resident_owner_evidence_not_supplied")
    else:
        owner = replay_resident_owner_evidence(
            resident_owner_evidence_dir, run_id=run_id, windows_node_identity_sha256=_physical_owner_node_identity(reader),
        )
        # The owner reader hashed the original bytes it read once, bounded and without
        # following symlinks; those digests enter the input index as they are.
        for name, digest in sorted(owner.files.items()):
            reader.record_digest(resident_owner_evidence_dir / name, digest)
        problems.extend("resident_owner:" + problem for problem in owner.problems)
        # A v4 summary needs the R22 owner: its v2 result, its plan bytes equal to the
        # receipt's plan, and its original intent pinned by the plan the child froze.
        if owner.result_contract_version != OWNER_RESULT_CONTRACT_VERSION or owner.receipt_version != 4:
            problems.append("resident_owner_result_not_v4")
        if owner.plan_bytes is None or sha256_of(owner.plan_bytes) != receipt.sampling_plan_sha256:
            problems.append("resident_owner_plan_mismatch")
        if owner.owner_intent_sha256 != plan.owner_intent_sha256:
            problems.append("resident_owner_intent_mismatch")
        if owner.intent_duration_ns != plan.duration_ns:
            problems.append("resident_owner_intent_duration_mismatch")
        for lane, ready in sorted(owner.readies.items()):
            closed = owner.closed_payloads.get(lane)
            if closed is None:
                problems.append("resident_owner_closed_absent:" + lane)
                continue
            try:
                check_resident_observer_mapping_v4(ready=ready, closed_bytes=closed, frames=frames, receipt=receipt, plan=plan)
            except ValueError as exc:
                problems.append(f"resident_owner_mapping_failed:{lane}:{exc}"[:300])
        if {"gpu_fast", "host_slow"} <= set(owner.readies) and {"gpu_fast", "host_slow"} <= set(owner.closures):
            try:
                cpu = check_combined_resident_cpu_v4(
                    gpu_ready=owner.readies["gpu_fast"], host_ready=owner.readies["host_slow"],
                    gpu_closure=owner.closures["gpu_fast"], host_closure=owner.closures["host_slow"], receipt=receipt, seal=seal,
                )
            except ValueError as exc:
                problems.append(f"resident_owner_cpu_failed:{exc}"[:300])
            else:
                if not cpu.within_two_percent:
                    problems.append("resident_owner_cpu_over_two_percent")
        else:
            problems.append("resident_owner_lanes_incomplete")
    return TelemetryFacts(
        receipt_sha256=seal.receipt_sha256, seal_sha256=reader.digest(run_directory / "seal.v4.json"),
        contract_version=receipt.contract_version, status=receipt.status, seal_status=seal.status,
        aggregates=aggregates, problems=tuple(sorted(set(problems))),
    )


def _telemetry_facts(
    reader: _Reader, *, artifact_root: Path | None, run_id: str | None, spec: M6RunSpec,
    summary: dict[str, Any] | None, anchor: M6OwnerAnchor, receipt_version: Literal[3, 4] = 3,
    resident_owner_evidence_dir: Path | None = None,
) -> TelemetryFacts | None:
    """Replay one sealed observer run and bind it to this campaign.

    The replay itself proves the artifacts; this binds them to the run that is
    being scored. Every identity or coverage check the evidence does not satisfy
    is a named problem, which keeps the resource gate unknown rather than
    letting another run's telemetry stand in for this one.
    """
    if artifact_root is None and run_id is None:
        reader.note("resource_telemetry_receipt_absent")
        return None
    if artifact_root is None or run_id is None:
        raise CampaignInputError(
            "a telemetry artifact root and its observer run id must be supplied together",
        )
    if receipt_version == 4:
        return _telemetry_facts_v4(
            reader, artifact_root=artifact_root, run_id=run_id, spec=spec, summary=summary, anchor=anchor,
            resident_owner_evidence_dir=resident_owner_evidence_dir,
        )
    if resident_owner_evidence_dir is not None:
        raise CampaignInputError("resident owner evidence replay requires the v4 telemetry protocol")
    try:
        result = verify_synchronized_telemetry_observer(
            artifact_root=artifact_root, run_id=run_id, receipt_version=3,
        )
    except ValueError as exc:
        raise CampaignIdentityError(f"synchronized telemetry evidence failed its own replay: {exc}") from exc
    run_directory = artifact_root / run_id
    for name, bound in _TELEMETRY_ARTIFACTS:
        reader.read(run_directory / name, absent="telemetry_artifact_absent:" + name, maximum=bound)
    receipt, seal, frames = result.receipt, result.seal, result.frames
    problems: list[str] = []
    if receipt.runtime_bundle_identity_sha256 != spec.runtime.runtime_bundle_identity_sha256:
        problems.append("telemetry_runtime_bundle_mismatch")
    if receipt.process_profile.process_profile_sha256 != spec.runtime.process_profile_sha256:
        problems.append("telemetry_process_profile_mismatch")
    if receipt.status != "complete":
        problems.append("telemetry_receipt_not_complete")
    if seal.status != "complete":
        problems.append("telemetry_seal_not_complete")
    boots = {frame.resident_exporter_provenance.boot_identity_sha256
             for frame in frames if frame.resident_exporter_provenance is not None}
    boot_alias = None
    if boots - {spec.clock.boot_identity_sha256}:
        boot_alias = _resident_boot_alias(reader, anchor)
        if boot_alias is None:
            problems.append("telemetry_native_resident_boot_binding_unproven")
    for frame in frames:
        values = frame.gpu.values
        if (frame.gpu.status == "supported" and values is not None
                and values.device_identity_sha256 != spec.runtime.gpu_device_identity_sha256):
            problems.append("telemetry_gpu_device_mismatch")
        provenance = frame.resident_exporter_provenance
        if provenance is None:
            problems.append("telemetry_host_identity_unproven")
        elif (provenance.host_assignment_identity_sha256 != spec.clock.host_assignment_identity_sha256
                or provenance.boot_identity_sha256 not in {spec.clock.boot_identity_sha256, boot_alias}):
            problems.append("telemetry_host_identity_mismatch")
    window = _coverage_window(summary)
    aggregates = None
    if window is None:
        problems.append("telemetry_coverage_window_absent")
    else:
        started, finished = window
        if receipt.started_at_utc > started:
            problems.append("telemetry_coverage_incomplete:receipt_start")
        if receipt.finished_at_utc < finished:
            problems.append("telemetry_coverage_incomplete:receipt_finish")
        aggregates = derive_resource_aggregates(
            cast(tuple[SynchronizedTelemetryFrameV2, ...], frames), window_start_utc=started, window_end_utc=finished,
        )
        for lane, first, last, interval in (
            ("gpu_fast", aggregates.gpu_first_utc, aggregates.gpu_last_utc,
             aggregates.gpu_nominal_interval_ms),
            ("host_slow", aggregates.host_first_utc, aggregates.host_last_utc,
             aggregates.host_nominal_interval_ms),
        ):
            if first is None or last is None or interval is None:
                problems.append("telemetry_coverage_incomplete:" + lane)
            elif (first > started + timedelta(milliseconds=interval)
                    or last < finished - timedelta(milliseconds=interval)):
                problems.append("telemetry_coverage_incomplete:" + lane)
    return TelemetryFacts(
        receipt_sha256=seal.receipt_sha256, seal_sha256=reader.digest(run_directory / "seal.v3.json"),
        contract_version=receipt.contract_version, status=receipt.status, seal_status=seal.status,
        aggregates=aggregates, problems=tuple(sorted(set(problems))),
    )


def _bind_plan(
    reader: _Reader, run_dir: Path, *, plan: M6EvaluationPlan, spec: M6RunSpec,
    inputs: dict[str, Any] | None, summary: dict[str, Any] | None,
    intent: M6CampaignIntent | M6CampaignIntentV1 | None,
) -> None:
    """The supplied plan must be the plan this run froze, by bytes and by every recorded hash."""
    digest = plan.canonical_sha256()
    if plan.mode != spec.mode:
        raise CampaignIdentityError("evaluation plan mode differs from the frozen run spec")
    frozen = reader.read(run_dir / "evaluation-plan.json",
                         absent="evaluation_plan_not_in_run_output", maximum=_MAX_INPUT_BYTES)
    if frozen is not None and frozen != plan.canonical_bytes():
        raise CampaignIdentityError("the run's frozen evaluation-plan.json differs from the supplied plan")
    for label, document in (("campaign-inputs.json", inputs), ("campaign-summary.json", summary)):
        recorded = None if document is None else document.get("evaluation_plan_sha256")
        if recorded is not None and recorded != digest:
            raise CampaignIdentityError(f"{label} froze another evaluation plan than the supplied one")
    if isinstance(intent, M6CampaignIntent):
        if intent.evaluation_plan_sha256 != digest:
            raise CampaignIdentityError("the campaign intent froze another evaluation plan")
    elif intent is not None:
        reader.note("evaluation_plan_not_frozen_in_intent")


def _bind_spec(
    reader: _Reader, run_dir: Path, spec: M6RunSpec, summary: dict[str, Any] | None,
) -> M6OwnerAnchor:
    anchor_raw = reader.read(run_dir / "run" / "anchor.json",
                             absent="owner_anchor_absent", maximum=_MAX_RUN_FILE_BYTES)
    if anchor_raw is None:
        # Without the owner's anchor nothing attributes this spec to the owner process that
        # ran it, so the run cannot be scored at all rather than scored with a gap.
        raise CampaignInputError("run/anchor.json is required to attribute the run spec to its owner")
    try:
        anchor = M6OwnerAnchor.from_canonical_bytes(anchor_raw, maximum_bytes=_MAX_RUN_FILE_BYTES)
    except ValueError as exc:
        raise CampaignInputError(f"owner anchor invalid: {exc}") from exc
    try:
        anchor.assert_spec(spec)
    except ValueError as exc:
        raise CampaignIdentityError(str(exc)) from exc
    recorded = None if summary is None else summary.get("spec_sha256")
    if recorded is not None and recorded != spec.canonical_sha256():
        raise CampaignIdentityError("campaign-summary.json names another run spec than run/run-spec.json")
    return anchor


def load_campaign_evidence(
    run_dir: Path, *, evaluation_plan: M6EvaluationPlan, native_journal: Path | None = None,
    telemetry_artifact_root: Path | None = None, telemetry_run_id: str | None = None,
    manifest: Path | None = None, quality_plan: Path | None = None,
    telemetry_receipt_version: Literal[3, 4] = 3, resident_owner_evidence_dir: Path | None = None,
) -> CampaignEvidence:
    """Load one campaign output directory read-only and reduce its owner journal.

    `manifest`/`quality_plan` override the paths the run recorded; otherwise they
    are resolved from `campaign-inputs.json` or from the driver's
    `input-hashes.json` index next to the run directory, and their bytes must
    hash to the identities the run spec pinned. `native_journal` replaces
    `<run-dir>/native/events.jsonl` for an archived run whose journal was
    fetched elsewhere. `telemetry_artifact_root`/`telemetry_run_id` name one
    sealed synchronized-observer run, which is replayed in full before any of
    its aggregates is bound to this campaign. The run spec and the owner anchor
    are required; every other absent input is recorded as an unknown, and a
    conflict between inputs raises.
    """
    if type(evaluation_plan) is not M6EvaluationPlan:
        raise CampaignInputError("the delivery summary requires an exact m6.evaluation-plan.v1")
    run_dir = Path(run_dir)
    if not run_dir.is_absolute() or not run_dir.is_dir():
        raise CampaignInputError("campaign run directory must be an existing absolute directory")
    reader = _Reader(run_dir)
    inputs = reader.document(run_dir / "campaign-inputs.json",
                             absent="campaign_inputs_absent", maximum=_MAX_INPUT_BYTES)
    summary = reader.document(run_dir / "campaign-summary.json",
                              absent="campaign_summary_absent", maximum=_MAX_INPUT_BYTES)
    spec_raw = reader.read(run_dir / "run" / "run-spec.json",
                           absent="run_spec_absent", maximum=_MAX_RUN_FILE_BYTES)
    if spec_raw is None:
        raise CampaignInputError("run/run-spec.json is required to score a run and could not be read")
    try:
        spec = M6RunSpec.from_canonical_bytes(spec_raw, maximum_bytes=_MAX_RUN_FILE_BYTES)
    except ValueError as exc:
        raise CampaignInputError(f"run spec invalid: {exc}") from exc
    anchor = _bind_spec(reader, run_dir, spec, summary)
    _bind_native_evidence(reader, run_dir, spec)
    index = reader.document(run_dir.parent / "input-hashes.json",
                            absent="driver_input_hashes_absent", maximum=_MAX_INPUT_BYTES)
    hashes = {key: value for key, value in (index or {}).items() if type(value) is str}
    intent = _load_intent(reader, run_dir, inputs=inputs, hashes=hashes)
    _bind_plan(reader, run_dir, plan=evaluation_plan, spec=spec, inputs=inputs, summary=summary, intent=intent)
    corpus = _load_manifest(reader, spec=spec, explicit=manifest, inputs=inputs, hashes=hashes)
    plan = _load_quality_plan(reader, spec=spec, explicit=quality_plan, inputs=inputs, hashes=hashes)
    if spec.campaign_id != corpus.campaign_id or spec.mode != corpus.mode:
        raise CampaignIdentityError("corpus manifest names another campaign or mode than the run spec")
    if spec.mode != plan.mode:
        raise CampaignIdentityError("quality plan declares another mode than the run spec")
    if intent is not None:
        # The intent must identify the whole run: rebuilding the spec from the intent and the owner
        # anchor with the product factory has to reproduce the frozen spec byte for byte.
        try:
            rebuilt = build_run_spec(anchor=anchor, intent=intent.run, runtime=intent.runtime)
        except ValueError as exc:
            raise CampaignIdentityError(f"the campaign intent does not fit the owner anchor: {exc}") from exc
        if rebuilt.canonical_sha256() != spec.canonical_sha256():
            raise CampaignIdentityError("the campaign intent declares another run than the frozen spec")
    journal = native_journal if native_journal is not None else run_dir / "native" / "events.jsonl"
    receipt, events = _reduce(reader, journal, spec=spec, manifest=corpus, quality_plan=plan)
    verifier = reader.document(run_dir / "verifier" / "run-summary.json",
                               absent="verifier_run_summary_absent", maximum=_MAX_INPUT_BYTES)
    return CampaignEvidence(
        intent=intent, spec=spec, manifest=corpus, quality_plan=plan, receipt=receipt, events=events,
        closure=_closure_facts(reader, run_dir, events, verifier),
        external=_external_facts(reader, run_dir, summary, intent=intent, anchor=anchor, inputs=inputs),
        telemetry=_telemetry_facts(reader, artifact_root=telemetry_artifact_root, run_id=telemetry_run_id,
                                   spec=spec, summary=summary, anchor=anchor, receipt_version=telemetry_receipt_version,
                                   resident_owner_evidence_dir=resident_owner_evidence_dir),
        stage_timing=_stage_timing_facts(reader, run_dir, verifier, spec=spec),
        inputs=reader.inputs, unknowns=reader.unknowns,
    )


def _load_intent(
    reader: _Reader, run_dir: Path, *, inputs: dict[str, Any] | None, hashes: dict[str, str],
) -> M6CampaignIntent | M6CampaignIntentV1 | None:
    """The run's own frozen `campaign-intent.json` first, checked against the hash the run recorded.

    The official entry persists the exact intent bytes before Prepare. An archived
    run that predates that copy may still be resolved through a driver's
    `input-hashes.json` index; a copy whose bytes do not hash to the recorded
    identity is refused rather than replaced by another copy.
    """
    expected = None if inputs is None else inputs.get("intent_sha256")
    if type(expected) is not str:
        reader.note("campaign_intent_absent")
        return None
    own = run_dir / "campaign-intent.json"
    paths = [own] if (own.is_symlink() or own.is_file()) else [
        Path(path) for path, digest in sorted(hashes.items())
        if digest == expected and Path(path).is_absolute()
    ]
    for path in paths:
        raw = reader.read(path, absent="campaign_intent_absent", maximum=M6_CAMPAIGN_INTENT_MAX_BYTES)
        if raw is None:
            continue
        if sha256_of(raw) != expected:
            raise CampaignIdentityError("campaign intent bytes differ from the identity the run recorded")
        try:
            return decode_campaign_intent(raw)
        except ValueError as exc:
            raise CampaignInputError(f"campaign intent invalid: {exc}") from exc
    reader.note("campaign_intent_absent")
    return None


def _load_manifest(
    reader: _Reader, *, spec: M6RunSpec, explicit: Path | None, inputs: dict[str, Any] | None,
    hashes: dict[str, str],
) -> M6CorpusManifest:
    raw = _resolve_pinned(
        reader, expected_sha256=spec.manifest_sha256, label="corpus manifest", maximum=_MAX_INPUT_BYTES,
        explicit=explicit, recorded=None if inputs is None else inputs.get("manifest_path"), hashes=hashes,
    )
    try:
        return M6CorpusManifest.from_canonical_bytes(raw, maximum_bytes=_MAX_INPUT_BYTES)
    except ValueError as exc:
        raise CampaignInputError(f"corpus manifest invalid: {exc}") from exc


def _load_quality_plan(
    reader: _Reader, *, spec: M6RunSpec, explicit: Path | None, inputs: dict[str, Any] | None,
    hashes: dict[str, str],
) -> M6QualityPlan:
    raw = _resolve_pinned(
        reader, expected_sha256=spec.quality_plan_sha256, label="quality plan", maximum=_MAX_INPUT_BYTES,
        explicit=explicit, recorded=None if inputs is None else inputs.get("quality_plan_path"), hashes=hashes,
    )
    try:
        return M6QualityPlan.from_canonical_bytes(raw, maximum_bytes=_MAX_INPUT_BYTES)
    except ValueError as exc:
        raise CampaignInputError(f"quality plan invalid: {exc}") from exc


def _reduce(
    reader: _Reader, journal: Path, *, spec: M6RunSpec, manifest: M6CorpusManifest,
    quality_plan: M6QualityPlan,
) -> tuple[M6RunReceipt | None, tuple[M6RunEvent, ...]]:
    """Reduce the owner journal with the unchanged accounting contract; no journal means no receipt."""
    if journal.is_symlink() or not journal.is_file():
        reader.note("owner_journal_absent")
        return None, ()
    try:
        lines = _journal_lines(journal, spec)
    except OSError as exc:
        reader.note(f"owner_journal_unreadable:{type(exc).__name__}")
        return None, ()
    reader.record(journal, b"".join(lines))
    events = _decode_events(spec, lines)
    history = _history_facts(reader, reader.run_dir, events)
    qualifications = _qualifications(reader, reader.run_dir)
    try:
        receipt = reduce_m6_run(
            spec=spec, manifest=manifest, quality_plan=quality_plan, journal_lines=lines,
            history=history, qualifications=qualifications,
        )
    except ValueError as exc:
        raise CampaignInputError(f"the run journal could not be reduced from its own evidence: {exc}") from exc
    if receipt.spec_sha256 != spec.canonical_sha256():
        raise CampaignIdentityError("the reduced receipt names another spec than run/run-spec.json")
    return receipt, events


__all__ = ["CampaignEvidence", "load_campaign_evidence"]
