"""Bounded functional multi-PDF supply using the original v2 attempt owner.

The batch journal only owns dispatch intents and disposal references. Every
attempt keeps its original lifecycle journal, key, clock and deadline. Recovery
reconciles dispatched attempts only; it never creates fresh work. This entry
does not issue formal M6 owner events, qualified-page credit or publication.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
import hashlib
from pathlib import Path
import re
from typing import Any

from disclosure_anchor.adapters.parsers.mineru_medium.http_staged import (
    _MAX_DECODED_BYTES, _MAX_UNCOMPRESSED_BYTES, prepare_submission_identity_v2,
)
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import (
    DiagnosticJournal, DiagnosticJournalError, _MAX_RECORDS, _MAX_TOTAL,
)
from disclosure_anchor.adapters.runtime.mineru_diagnostic_lifecycle import (
    _MAX_INPUT_BYTES, run_diagnostic_attempt_v2,
)
from disclosure_anchor.adapters.runtime.mineru_diagnostic_store import _canonical, _digest
from disclosure_anchor.application.contracts.mineru_api_health import MINERU_API_RESULT_RESERVATION_BYTES
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.services.m6_service_controller import (
    ServiceCompletion, ServiceControllerFailure, ServiceControllerResult, ServiceWork, run_service_controller,
)

# One binding plus one dispatch and one disposal reference per complete PDF.
# This derives from the existing journal envelope, not an experiment profile.
MAX_SERVICE_BATCH_INPUTS = (_MAX_RECORDS - 1) // 2


@dataclass(frozen=True, slots=True)
class ServiceBatchInput:
    attempt_id: str
    fence_identity: str
    submission_epoch_unix: int
    input_pdf: Path
    source_pdf_sha256: str
    source_byte_count: int
    source_page_count: int

    def __post_init__(self) -> None:
        if (not isinstance(self.input_pdf, Path)
                or not self.input_pdf.is_absolute()
                or type(self.source_byte_count) is not int or not 0 < self.source_byte_count <= _MAX_INPUT_BYTES
                or type(self.source_page_count) is not int or not 0 < self.source_page_count <= 2**31 - 1
                or type(self.source_pdf_sha256) is not str
                or re.fullmatch(r"sha256:[0-9a-f]{64}", self.source_pdf_sha256) is None):
            raise ValueError("service batch input requires original full-PDF identity and bounds")

    def payload(self) -> dict[str, Any]:
        return {**asdict(self), "input_pdf": str(self.input_pdf)}


def service_work_reservation(item: ServiceBatchInput) -> ServiceWork:
    """Conservative actual E1 upper bounds, with retained evidence still charged.

    No smaller caller estimate overrides the real ZIP, extraction or decoder
    ceilings. These credits bound admitted resources, not all process RSS.
    """
    result = MINERU_API_RESULT_RESERVATION_BYTES
    reservation = ResourceCreditVector(
        documents=1, snapshot_items=1, snapshot_bytes=item.source_byte_count,
        remote_waits=1, provider_tasks=1, provider_result_bytes=result,
        materialization_items=1, compressed_bytes=result, decoded_bytes=_MAX_DECODED_BYTES,
        temp_disk_bytes=item.source_byte_count + result + _MAX_UNCOMPRESSED_BYTES + _MAX_TOTAL,
        output_items=1, output_bytes=_MAX_UNCOMPRESSED_BYTES,
        output_pages=item.source_page_count, ack_items=1,
    )
    return ServiceWork(item.attempt_id, reservation, ResourceCreditVector(temp_disk_bytes=_MAX_TOTAL))


@dataclass(frozen=True, slots=True)
class ServiceBatchResult:
    controller: ServiceControllerResult | None
    previously_disposed: tuple[ServiceCompletion, ...]
    not_dispatched: tuple[str, ...]
    retained_or_unresolved_credits: ResourceCreditVector
    unreconciled: tuple[str, ...] = ()
    batch_evidence_reserved_bytes: int = _MAX_TOTAL
    qualification_scope: str = "functional_lifecycle_only_quality_unverified"


class ServiceBatchFailure(RuntimeError):
    """Batch identities remain distinguishable from this invocation's futures."""

    def __init__(self, result: ServiceBatchResult) -> None:
        super().__init__("service batch has unresolved execution; preserve original batch and attempt journals")
        self.result = result


def run_service_batch(
    *, batch_id: str, inputs: tuple[ServiceBatchInput, ...], api_url: str,
    server_url: str, options: ParserOptions, journal_root: Path,
    clock_identity_sha256: str, deadline_ns: int, continuous_ns: Callable[[], int],
    max_in_flight: int, credits_limit: ResourceCreditVector,
    stop_requested: Callable[[], bool], before_submit: Callable[[], None],
    resume: bool = False,
) -> ServiceBatchResult:
    """Supply a frozen finite batch; POST authority is checked again per attempt.

    Caller owns a runtime parent directory and a current thread-safe admission
    guard. The journal and attempt directories are private siblings beneath it.
    Resume binds the same complete input/configuration/clock/deadline; unknown
    or partially created attempt state stays unresolved in the original journal.
    """
    if (type(inputs) is not tuple or not 1 <= len(inputs) <= MAX_SERVICE_BATCH_INPUTS
            or any(type(item) is not ServiceBatchInput for item in inputs)
            or len({item.attempt_id for item in inputs}) != len(inputs)
            or len({item.source_pdf_sha256 for item in inputs}) != len(inputs)
            or type(resume) is not bool or not journal_root.is_absolute()
            or type(max_in_flight) is not int or not 1 <= max_in_flight <= 64
            or type(credits_limit) is not ResourceCreditVector
            or credits_limit.temp_disk_bytes < _MAX_TOTAL):
        raise ValueError("service batch identity, input set or resource envelope is invalid")
    effective_limit = replace(credits_limit, temp_disk_bytes=credits_limit.temp_disk_bytes - _MAX_TOTAL)
    by_id = {item.attempt_id: item for item in inputs}
    work = tuple(service_work_reservation(item) for item in inputs)
    if any(not item.reservation.fits(effective_limit) for item in work):
        raise ValueError("service attempt cannot fit alongside the batch journal reservation")
    # Validate every original submission before creating the first batch object.
    for item in inputs:
        prepare_submission_identity_v2(
            api_url=api_url, server_url=server_url, options=options,
            source_pdf_sha256=item.source_pdf_sha256, attempt_identity=item.attempt_id,
            fence_identity=item.fence_identity, submission_epoch_unix=item.submission_epoch_unix,
        )
    paths = {
        item.attempt_id: journal_root.with_name(
            journal_root.name + "-attempt-" + hashlib.sha256(item.attempt_id.encode()).hexdigest()
        ) for item in inputs
    }
    if any(item.input_pdf == journal_root or any(
        item.input_pdf == path or path in item.input_pdf.parents for path in paths.values()
    ) for item in inputs):
        raise ValueError("service batch evidence roots overlap an original PDF")
    binding = {
        "contract_version": "m6.service-batch-binding.v1", "batch_id": batch_id,
        "inputs": [item.payload() for item in inputs], "api_url": api_url, "server_url": server_url,
        "options": asdict(options), "max_in_flight": max_in_flight,
        "credits_limit": asdict(credits_limit),
        "attempt_journals": {identity: str(path) for identity, path in paths.items()},
        "qualification_scope": "functional_lifecycle_only_quality_unverified",
    }
    binding_sha = _digest(_canonical(binding))
    with DiagnosticJournal(
        journal_root, create=not resume, attempt_id=batch_id,
        configuration_sha256=binding_sha, clock_identity_sha256=clock_identity_sha256,
        deadline_ns=deadline_ns, continuous_ns=continuous_ns,
    ) as journal:
        if not journal.records:
            if resume:
                raise DiagnosticJournalError("service batch binding was never durably established")
            journal.append("batch_binding", binding)
        records = journal.records
        if records[0].step != "batch_binding" or records[0].value != binding:
            raise DiagnosticJournalError("service batch original binding differs")
        dispatched: set[str] = set()
        disposed: dict[str, ServiceCompletion] = {}
        for record in records[1:]:
            value = record.value
            identity = value.get("attempt_id")
            if type(identity) is not str or identity not in by_id:
                raise DiagnosticJournalError("service batch record references an unknown attempt")
            if record.step == "dispatch_intent":
                if (value != {"attempt_id": identity, "binding_sha256": binding_sha}
                        or identity in dispatched):
                    raise DiagnosticJournalError("service batch repeated or mismatched dispatch")
                dispatched.add(identity)
            elif record.step == "attempt_disposed":
                if (set(value) != {"attempt_id", "outcome", "disposal_receipt_sha256"}
                        or identity not in dispatched or identity in disposed):
                    raise DiagnosticJournalError("service batch disposal lacks its unique dispatch")
                disposed[identity] = ServiceCompletion(**value)
            else:
                raise DiagnosticJournalError("service batch contains a foreign lifecycle record")

        def dispatch(item: ServiceWork) -> None:
            journal.remaining_seconds()
            if resume:
                if item.attempt_id not in dispatched:
                    raise DiagnosticJournalError("batch recovery cannot admit a new attempt")
            else:
                # An append can become durable before its caller sees an IO
                # failure. Record possible responsibility before entering it;
                # no failure result may label this item definitely undispatched.
                dispatched.add(item.attempt_id)
                journal.append("dispatch_intent", {"attempt_id": item.attempt_id, "binding_sha256": binding_sha})

        def complete(result: ServiceCompletion) -> None:
            previous = disposed.get(result.attempt_id)
            if previous is not None:
                if previous != result:
                    raise DiagnosticJournalError("recovered disposal differs from the original reference")
            else:
                journal.append("attempt_disposed", asdict(result))
                disposed[result.attempt_id] = result

        def remaining_credits() -> ResourceCreditVector:
            # Unlike controller invocation counters, this includes resources
            # already owned before a recovery call and not yet reconciled.
            total = ResourceCreditVector(temp_disk_bytes=_MAX_TOTAL)
            for item in work:
                if item.attempt_id in disposed:
                    total = total + item.retained_after_disposal
                elif item.attempt_id in dispatched:
                    total = total + item.reservation
            return total

        def execute(item: ServiceWork, guard: Callable[[], None], *, require_disposed: bool = False) -> ServiceCompletion:
            source = by_id[item.attempt_id]
            proof = run_diagnostic_attempt_v2(
                input_pdf=source.input_pdf, source_pdf_sha256=source.source_pdf_sha256,
                source_byte_count=source.source_byte_count, source_page_count=source.source_page_count,
                api_url=api_url, server_url=server_url, options=options,
                journal_root=paths[source.attempt_id], attempt_identity=source.attempt_id,
                fence_identity=source.fence_identity, submission_epoch_unix=source.submission_epoch_unix,
                clock_identity_sha256=clock_identity_sha256, deadline_ns=deadline_ns,
                continuous_ns=continuous_ns, resume=resume, before_submit=guard,
                require_disposed=require_disposed,
            )
            return ServiceCompletion(source.attempt_id, proof["outcome"], _digest(_canonical(proof)))

        # Verify final proofs with a read-only E1 guard before accounting only
        # retained journals. A missing/damaged/unfinished attempt cannot gain
        # disposal authority from the batch reference or cause recovery effects.
        retained = ResourceCreditVector()
        for retained_work in work:
            if retained_work.attempt_id in disposed:
                actual = execute(retained_work, before_submit, require_disposed=True)
                if actual != disposed[retained_work.attempt_id]:
                    raise DiagnosticJournalError("original attempt disposal proof differs from batch reference")
                retained = retained + retained_work.retained_after_disposal
        selected = tuple(item for item in work if not resume or (
            item.attempt_id in dispatched and item.attempt_id not in disposed
        ))
        if not retained.fits(effective_limit):
            raise DiagnosticJournalError("retained batch evidence exceeds original resource envelope")
        controller_limit = effective_limit - retained
        if resume:
            # Every undisposed dispatch may already own its full resources,
            # including jobs not yet scheduled by this invocation's executor.
            existing = ResourceCreditVector()
            for existing_work in selected:
                existing = existing + existing_work.reservation
            if not existing.fits(controller_limit):
                raise DiagnosticJournalError("unresolved batch reservations exceed original recovery envelope")
        previous = tuple(disposed[item.attempt_id] for item in inputs if item.attempt_id in disposed)
        result = None
        if selected:
            journal.require_capacity(additional_records=(
                len(selected) if resume else 2 * len(selected)
            ), additional_bytes=4096 * len(selected))
            try:
                result = run_service_controller(
                    work=selected, max_in_flight=max_in_flight, credits_limit=controller_limit,
                    execute=execute, stop_requested=stop_requested, before_submit=before_submit,
                    on_dispatch=dispatch, on_completion=complete,
                )
            except ServiceControllerFailure as exc:
                raise ServiceBatchFailure(ServiceBatchResult(
                    controller=exc.result, previously_disposed=previous,
                    not_dispatched=tuple(item.attempt_id for item in inputs if item.attempt_id not in dispatched),
                    retained_or_unresolved_credits=remaining_credits(),
                    unreconciled=tuple(item.attempt_id for item in inputs
                                       if item.attempt_id in dispatched and item.attempt_id not in disposed),
                )) from exc
        return ServiceBatchResult(
            controller=result, previously_disposed=previous,
            not_dispatched=tuple(item.attempt_id for item in inputs if item.attempt_id not in dispatched),
            retained_or_unresolved_credits=remaining_credits(),
            unreconciled=tuple(item.attempt_id for item in inputs
                               if item.attempt_id in dispatched and item.attempt_id not in disposed),
        )
