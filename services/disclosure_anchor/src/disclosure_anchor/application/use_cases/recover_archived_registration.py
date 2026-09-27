"""Register PDFs archived before their download registration failed.

A retained registration replays exactly one failed download attempt from the
point after its PDF was archived: no provider request, no parser, no model.
Its input is an operator request naming exact failed accesses and the index
accesses that carried their candidates; its authority is a recorded
historical-security binding; its raw input is the single content-addressed
archive file of each provider document, verified read-only.

Preview writes nothing and refuses the whole request if any item cannot be
proven. Execute runs the frozen plan item by item, each in its own
transaction under the failed access's row lock:

    receipt already recorded  -> verify it is this obligation; no new rows
    otherwise                 -> re-verify failure, index, binding, raw and
                                 document state, then register through the
                                 shared core with the recovery link

A committed prefix stays committed; the first refusal stops the run; a
re-run skips verified completed items. A commit whose outcome is unknown is
read back under the same row lock before anything else is decided.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
import json
from pathlib import Path
from typing import Any, Literal

from disclosure_anchor.application.contracts.historical_security_registration import (
    DOWNLOAD_FAILURE_INTERFACE,
    FAILURE_RECORD_ARCHIVE_BINDING,
    HISTORICAL_SECURITY_BINDING_INTERFACE,
    HISTORICAL_SECURITY_STATUS,
    POST_FAILURE_ARCHIVE_INVENTORY,
    PROVIDER,
    RECOVERABLE_FAILURE_ERROR_CODES,
    RETAINED_REGISTRATION_DATASET,
    RETAINED_REGISTRATION_INTERFACE,
    RETAINED_REGISTRATION_PLAN_SCHEMA,
    ContractViolation,
    RetainedRegistrationPlanItemV1,
    RetainedRegistrationPlanV1,
    RetainedRegistrationRequestItemV1,
    RetainedRegistrationRequestV1,
    failed_access_projection_sha256,
    retained_archive_relpath,
)
from disclosure_anchor.application.ports.file_store import RetainedRawArchivePort
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.register_document import register_document
from disclosure_anchor.application.services.source_security_resolution import (
    HistoricalAcquisitionProvenance,
    RetainedRecoveryProvenance,
    VerifiedAcquisitionSubject,
    index_candidate,
    resolve_acquisition_subject,
)
from disclosure_anchor.application.services.subject_resolver import SubjectResolver
from disclosure_anchor.application.use_cases.download_document import (
    candidate_registration,
)
from disclosure_anchor.application.worker.locks import shared_corpus_writer
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.errors import (
    DocumentIdentityConflictError,
    RawDocumentError,
    RegistrationMetadataError,
    SourceRecoveryError,
)


@dataclass(frozen=True)
class PreviewRefusal:
    failed_source_access_id: str
    error_code: str
    message: str


@dataclass(frozen=True)
class RetainedRegistrationPreviewResult:
    """Either a complete plan or every refusal; never a partial plan."""

    plan: RetainedRegistrationPlanV1 | None
    refusals: tuple[PreviewRefusal, ...]


ItemState = Literal["committed", "already_completed", "stopped", "not_executed"]
ReconciledState = Literal["resolved", "unresolved", "conflict"]


@dataclass(frozen=True)
class ItemOutcome:
    sequence: int
    failed_source_access_id: str
    state: ItemState
    receipt_source_access_id: str | None = None
    document_id: str | None = None
    reused_existing_document: bool | None = None
    readback_after_unknown_commit: bool = False
    error_code: str | None = None
    message: str | None = None

    def to_document(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "failed_source_access_id": self.failed_source_access_id,
            "state": self.state,
            "receipt_source_access_id": self.receipt_source_access_id,
            "document_id": self.document_id,
            "reused_existing_document": self.reused_existing_document,
            "readback_after_unknown_commit": self.readback_after_unknown_commit,
            "error_code": self.error_code,
            "message": self.message,
        }


@dataclass(frozen=True)
class RetainedRegistrationExecutionResult:
    plan_sha256: str
    items: tuple[ItemOutcome, ...]

    @property
    def stopped(self) -> bool:
        return any(item.state == "stopped" for item in self.items)

    def to_document(self) -> dict[str, object]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.state] = counts.get(item.state, 0) + 1
        return {
            "schema": "retained-registration-execution-result.v1",
            "plan_sha256": self.plan_sha256,
            "item_count": len(self.items),
            "counts": dict(sorted(counts.items())),
            "stopped": self.stopped,
            "items": [item.to_document() for item in self.items],
            "authority": "database receipts; run reconcile before relying on this report",
        }


@dataclass(frozen=True)
class ReconciledItem:
    sequence: int
    failed_source_access_id: str
    state: ReconciledState
    failure_history_unchanged: bool
    receipt_source_access_id: str | None
    document_id: str | None
    detail: str | None = None

    def to_document(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "failed_source_access_id": self.failed_source_access_id,
            "state": self.state,
            "failure_history_unchanged": self.failure_history_unchanged,
            "receipt_source_access_id": self.receipt_source_access_id,
            "document_id": self.document_id,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RetainedRegistrationReconciliation:
    plan_sha256: str
    items: tuple[ReconciledItem, ...] = field(default_factory=tuple)

    def to_document(self) -> dict[str, object]:
        states = {"resolved": 0, "unresolved": 0, "conflict": 0}
        for item in self.items:
            states[item.state] += 1
        return {
            "schema": "retained-registration-reconciliation.v1",
            "plan_sha256": self.plan_sha256,
            "item_count": len(self.items),
            "resolved": states["resolved"],
            "unresolved": states["unresolved"],
            "conflict": states["conflict"],
            "failure_history_unchanged": sum(
                1 for item in self.items if item.failure_history_unchanged
            ),
            "items": [item.to_document() for item in self.items],
        }


@dataclass(frozen=True)
class _FailureFacts:
    provider_document_id: str
    error_code: str
    reason: str | None
    recorded_index_source_access_id: str | None
    # None: legacy record without archive facts.
    recorded_archive: Mapping[str, object] | None


class RetainedRegistrationPreview:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        retained_archive: RetainedRawArchivePort,
        code_identity: str,
        max_download_retries: int,
        subject_resolver: SubjectResolver | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._retained = retained_archive
        self._code_identity = code_identity
        self._max_download_retries = max_download_retries
        self._subject_resolver = subject_resolver or SubjectResolver()

    def preview(
        self,
        request: RetainedRegistrationRequestV1,
        *,
        request_sha256: str,
        binding_source_access_id: str,
        max_items: int,
    ) -> RetainedRegistrationPreviewResult:
        if len(request.items) > max_items:
            raise SourceRecoveryError(
                "MAX_ITEMS_EXCEEDED",
                f"request names {len(request.items)} failed accesses, bound is {max_items}",
            )
        with self._uow_factory() as uow:
            binding_sha256 = _binding_sha256(uow, binding_source_access_id)
        requested = frozenset(item.failed_source_access_id for item in request.items)
        items: list[RetainedRegistrationPlanItemV1] = []
        refusals: list[PreviewRefusal] = []
        for sequence, requested_item in enumerate(request.items, start=1):
            try:
                items.append(
                    self._preview_item(
                        sequence,
                        requested_item,
                        binding_source_access_id=binding_source_access_id,
                        requested_failures=requested,
                    )
                )
            except SourceRecoveryError as exc:
                refusals.append(
                    PreviewRefusal(
                        failed_source_access_id=requested_item.failed_source_access_id,
                        error_code=exc.error_code,
                        message=exc.message,
                    )
                )
        if refusals:
            return RetainedRegistrationPreviewResult(plan=None, refusals=tuple(refusals))
        plan = RetainedRegistrationPlanV1.model_validate(
            {
                "schema": RETAINED_REGISTRATION_PLAN_SCHEMA,
                "provider": PROVIDER,
                "binding_source_access_id": binding_source_access_id,
                "binding_sha256": binding_sha256,
                "request_sha256": request_sha256,
                "code_identity": self._code_identity,
                "max_items": max_items,
                "item_count": len(items),
                "total_byte_count": sum(item.byte_count for item in items),
                "items": [item.model_dump(mode="json") for item in items],
            },
            strict=True,
        )
        return RetainedRegistrationPreviewResult(plan=plan, refusals=())

    def _preview_item(
        self,
        sequence: int,
        requested: RetainedRegistrationRequestItemV1,
        *,
        binding_source_access_id: str,
        requested_failures: frozenset[str],
    ) -> RetainedRegistrationPlanItemV1:
        failed_id = requested.failed_source_access_id
        with self._uow_factory() as uow:
            failed, facts = _recoverable_failure(
                uow.source_accesses.get(failed_id), failed_id
            )
            index = _index_for_failure(
                uow,
                failed=failed,
                facts=facts,
                index_source_access_id=requested.index_source_access_id,
            )
            candidate = _candidate_of(index, facts.provider_document_id)
            verified = _verified_subject(
                uow,
                candidate=candidate,
                index=index,
                binding_source_access_id=binding_source_access_id,
                subject_resolver=self._subject_resolver,
            )
            acquisition = _acquisition(verified)
            _require_no_other_blocker(
                uow,
                provider_document_id=facts.provider_document_id,
                failed_source_access_id=failed_id,
                requested_failures=requested_failures,
                max_download_retries=self._max_download_retries,
            )
            receipt = uow.source_accesses.successful_recovery_for(failed_id)
            projection = failed_access_projection_sha256(failed)
        located = self._locate(candidate, facts, requested)
        with self._uow_factory() as uow:
            document_state, existing_document_id = _document_state(
                uow,
                provider_document_id=facts.provider_document_id,
                raw_file_hash=located.raw_file_hash,
                raw_file_relpath=str(located.relpath),
                verified=verified,
            )
            if receipt is not None:
                _require_same_obligation(
                    uow,
                    receipt=receipt,
                    failed=failed,
                    provider_document_id=facts.provider_document_id,
                    raw_file_hash=located.raw_file_hash,
                    verified=verified,
                )
        announcement_date = _candidate_announcement_date(candidate)
        return RetainedRegistrationPlanItemV1.model_validate(
            {
                "sequence": sequence,
                "failed_source_access_id": failed_id,
                "failed_access_projection_sha256": projection,
                "failure_error_code": facts.error_code,
                "failure_reason": facts.reason,
                "provider_document_id": facts.provider_document_id,
                "index_source_access_id": index.source_access_id,
                "index_result_hash": acquisition.index_result_hash,
                "index_provider_interface": str(index.provider_interface),
                "candidate_sha256": acquisition.candidate_sha256,
                "announcement_date": announcement_date.isoformat(),
                "original_candidate_code": acquisition.original_candidate_code,
                "exchange": acquisition.exchange,
                "acquisition_scope_company_id": acquisition.acquisition_scope_company_id,
                "acquisition_scope_security_id": acquisition.acquisition_scope_security_id,
                "target_company_id": verified.subject.company.company_id,
                "target_security_id": verified.subject.security.security_id,
                "raw_file_relpath": str(located.relpath),
                "raw_file_hash": located.raw_file_hash,
                "byte_count": located.byte_count,
                "association_basis": (
                    FAILURE_RECORD_ARCHIVE_BINDING
                    if facts.recorded_archive is not None
                    else POST_FAILURE_ARCHIVE_INVENTORY
                ),
                "document_state": document_state,
                "existing_document_id": existing_document_id,
                "preview_state": "already_resolved" if receipt is not None else "ready",
                "existing_receipt_source_access_id": (
                    receipt.source_access_id if receipt is not None else None
                ),
            },
            strict=True,
        )

    def _locate(
        self,
        candidate: Mapping[str, object],
        facts: _FailureFacts,
        requested: RetainedRegistrationRequestItemV1,
    ) -> Any:
        security_code = _candidate_text(candidate, "security_code")
        announcement_date = _candidate_announcement_date(candidate)
        try:
            located = self._retained.locate_retained_raw_document(
                provider=PROVIDER,
                security_code=security_code,
                year=announcement_date.year,
                provider_document_id=facts.provider_document_id,
            )
        except RawDocumentError as exc:
            raise SourceRecoveryError("RAW_ARCHIVE_NOT_VERIFIED", str(exc)) from exc
        if (
            located.raw_file_hash != requested.expected_raw_file_hash
            or located.byte_count != requested.expected_byte_count
        ):
            raise SourceRecoveryError(
                "RAW_ARCHIVE_MISMATCH",
                f"{located.relpath} is {located.raw_file_hash}/{located.byte_count} bytes, "
                f"the request expects {requested.expected_raw_file_hash}/"
                f"{requested.expected_byte_count}",
            )
        _require_recorded_archive(
            facts,
            raw_file_relpath=str(located.relpath),
            raw_file_hash=located.raw_file_hash,
            byte_count=located.byte_count,
        )
        return located


class RetainedRegistrationExecutor:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        retained_archive: RetainedRawArchivePort,
        code_identity: str,
        max_download_retries: int,
        subject_resolver: SubjectResolver | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._retained = retained_archive
        self._code_identity = code_identity
        self._max_download_retries = max_download_retries
        self._subject_resolver = subject_resolver or SubjectResolver()

    def execute(
        self,
        plan: RetainedRegistrationPlanV1,
        *,
        plan_sha256: str,
        max_items: int,
        on_item: Callable[[ItemOutcome], None] | None = None,
    ) -> RetainedRegistrationExecutionResult:
        if plan.sha256() != plan_sha256:
            raise SourceRecoveryError("PLAN_HASH_MISMATCH", "plan bytes do not match plan_sha256")
        if plan.code_identity != self._code_identity:
            raise SourceRecoveryError(
                "CODE_IDENTITY_CHANGED",
                "the plan was previewed by different code; preview again",
            )
        if plan.item_count > max_items:
            raise SourceRecoveryError(
                "MAX_ITEMS_EXCEEDED",
                f"plan has {plan.item_count} items, bound is {max_items}",
            )
        with self._uow_factory() as uow:
            if _binding_sha256(uow, plan.binding_source_access_id) != plan.binding_sha256:
                raise SourceRecoveryError(
                    "BINDING_CHANGED", "the plan's binding no longer has the planned hash"
                )
        requested = frozenset(item.failed_source_access_id for item in plan.items)
        outcomes: list[ItemOutcome] = []
        stopped = False
        for item in plan.items:
            if stopped:
                outcome = ItemOutcome(
                    sequence=item.sequence,
                    failed_source_access_id=item.failed_source_access_id,
                    state="not_executed",
                )
            else:
                outcome = self._execute_item(
                    plan, item, plan_sha256=plan_sha256, requested_failures=requested
                )
                stopped = outcome.state == "stopped"
            outcomes.append(outcome)
            if on_item is not None:
                on_item(outcome)
        return RetainedRegistrationExecutionResult(
            plan_sha256=plan_sha256, items=tuple(outcomes)
        )

    def _execute_item(
        self,
        plan: RetainedRegistrationPlanV1,
        item: RetainedRegistrationPlanItemV1,
        *,
        plan_sha256: str,
        requested_failures: frozenset[str],
    ) -> ItemOutcome:
        # One controlled re-read after a Document unique race with a
        # concurrent ordinary registration; never a blind retry loop.
        for attempt in (1, 2):
            try:
                return self._attempt_item(
                    plan, item, plan_sha256=plan_sha256, requested_failures=requested_failures
                )
            except DocumentIdentityConflictError as exc:
                if attempt == 2:
                    return _stopped(item, "DOCUMENT_IDENTITY_RACE", str(exc))
            except SourceRecoveryError as exc:
                return _stopped(item, exc.error_code, exc.message)
        raise AssertionError("unreachable")

    def _attempt_item(
        self,
        plan: RetainedRegistrationPlanV1,
        item: RetainedRegistrationPlanItemV1,
        *,
        plan_sha256: str,
        requested_failures: frozenset[str],
    ) -> ItemOutcome:
        with shared_corpus_writer(self._uow_factory):
            try:
                raw = self._retained.verify_retained_raw_document(
                    relpath=Path(item.raw_file_relpath),
                    expected_hash=item.raw_file_hash,
                    expected_byte_count=item.byte_count,
                )
            except RawDocumentError as exc:
                raise SourceRecoveryError("RAW_ARCHIVE_NOT_VERIFIED", str(exc)) from exc
            commit_started = False
            try:
                with self._uow_factory() as uow:
                    failed = uow.source_accesses.get_for_update(item.failed_source_access_id)
                    receipt = uow.source_accesses.successful_recovery_for(
                        item.failed_source_access_id
                    )
                    if receipt is not None:
                        document = _require_same_obligation_for_item(
                            uow, receipt, item, failed=failed
                        )
                        return ItemOutcome(
                            sequence=item.sequence,
                            failed_source_access_id=item.failed_source_access_id,
                            state="already_completed",
                            receipt_source_access_id=receipt.source_access_id,
                            document_id=document.document_id,
                        )
                    verified, candidate = self._reverify(
                        uow, plan, item, failed=failed, requested_failures=requested_failures
                    )
                    acquisition = _acquisition(verified)
                    outcome = register_document(
                        uow,
                        subject=verified.subject,
                        doc_meta=candidate_registration(
                            candidate,
                            provider_interface=RETAINED_REGISTRATION_INTERFACE,
                            dataset_key=RETAINED_REGISTRATION_DATASET,
                            provenance=RetainedRecoveryProvenance(
                                acquisition=acquisition,
                                recovery_of_source_access_id=item.failed_source_access_id,
                                failed_access_projection_sha256=(
                                    item.failed_access_projection_sha256
                                ),
                                replay_plan_sha256=plan_sha256,
                                raw_file_relpath=item.raw_file_relpath,
                                raw_file_hash=item.raw_file_hash,
                                byte_count=item.byte_count,
                                association_basis=item.association_basis,
                            ),
                        ),
                        raw=raw,
                    )
                    commit_started = True
                    uow.commit()
            except (SourceRecoveryError, DocumentIdentityConflictError):
                if commit_started:
                    return self._read_back(item)
                raise
            except Exception:
                if not commit_started:
                    raise
                # The commit's outcome is unknown (e.g. the connection died
                # while committing). The database decides, not this process.
                return self._read_back(item)
        return ItemOutcome(
            sequence=item.sequence,
            failed_source_access_id=item.failed_source_access_id,
            state="committed",
            receipt_source_access_id=outcome.source_access.source_access_id,
            document_id=outcome.document.document_id,
            reused_existing_document=outcome.reused_existing_document,
        )

    def _reverify(
        self,
        uow: UnitOfWork,
        plan: RetainedRegistrationPlanV1,
        item: RetainedRegistrationPlanItemV1,
        *,
        failed: e.SourceAccess | None,
        requested_failures: frozenset[str],
    ) -> tuple[VerifiedAcquisitionSubject, Mapping[str, object]]:
        failed, facts = _recoverable_failure(failed, item.failed_source_access_id)
        if failed_access_projection_sha256(failed) != item.failed_access_projection_sha256:
            raise SourceRecoveryError(
                "FAILURE_RECORD_CHANGED",
                f"failed access {item.failed_source_access_id} no longer matches its plan projection",
            )
        if (
            facts.provider_document_id != item.provider_document_id
            or facts.error_code != item.failure_error_code
            or facts.reason != item.failure_reason
        ):
            raise SourceRecoveryError(
                "FAILURE_RECORD_CHANGED",
                f"failed access {item.failed_source_access_id} no longer matches the plan's failure",
            )
        index = _index_for_failure(
            uow, failed=failed, facts=facts, index_source_access_id=item.index_source_access_id
        )
        if (
            index.result_hash != item.index_result_hash
            or index.provider_interface != item.index_provider_interface
        ):
            raise SourceRecoveryError("INDEX_CHANGED", f"index access {index.source_access_id} changed")
        candidate = _candidate_of(index, item.provider_document_id)
        verified = _verified_subject(
            uow,
            candidate=candidate,
            index=index,
            binding_source_access_id=plan.binding_source_access_id,
            subject_resolver=self._subject_resolver,
        )
        acquisition = _acquisition(verified)
        announcement_date = _candidate_announcement_date(candidate)
        if (
            acquisition.candidate_sha256 != item.candidate_sha256
            or acquisition.binding_sha256 != plan.binding_sha256
            or acquisition.acquisition_scope_company_id != item.acquisition_scope_company_id
            or acquisition.acquisition_scope_security_id != item.acquisition_scope_security_id
            or acquisition.original_candidate_code != item.original_candidate_code
            or acquisition.exchange != item.exchange
            or announcement_date.isoformat() != item.announcement_date
            or verified.subject.company.company_id != item.target_company_id
            or verified.subject.security.security_id != item.target_security_id
        ):
            raise SourceRecoveryError(
                "SUBJECT_CHANGED",
                f"verified lineage of {item.provider_document_id} differs from the plan",
            )
        # The plan's raw is this document's own archive file, never a path
        # the plan merely names.
        archive_relpath = retained_archive_relpath(
            security_code=_candidate_text(candidate, "security_code"),
            year=announcement_date.year,
            provider_document_id=item.provider_document_id,
            raw_file_hash=item.raw_file_hash,
        )
        if archive_relpath != item.raw_file_relpath:
            raise SourceRecoveryError(
                "RAW_ARCHIVE_MISMATCH",
                f"{item.raw_file_relpath} is not the archive path of "
                f"{item.provider_document_id} ({archive_relpath})",
            )
        _require_recorded_archive(
            facts,
            raw_file_relpath=item.raw_file_relpath,
            raw_file_hash=item.raw_file_hash,
            byte_count=item.byte_count,
        )
        expected_basis = (
            FAILURE_RECORD_ARCHIVE_BINDING
            if facts.recorded_archive is not None
            else POST_FAILURE_ARCHIVE_INVENTORY
        )
        if item.association_basis != expected_basis:
            raise SourceRecoveryError(
                "FAILURE_RECORD_CHANGED",
                f"failed access {item.failed_source_access_id} supports "
                f"{expected_basis}, not {item.association_basis}",
            )
        _require_no_other_blocker(
            uow,
            provider_document_id=item.provider_document_id,
            failed_source_access_id=item.failed_source_access_id,
            requested_failures=requested_failures,
            max_download_retries=self._max_download_retries,
        )
        _document_state(
            uow,
            provider_document_id=item.provider_document_id,
            raw_file_hash=item.raw_file_hash,
            raw_file_relpath=item.raw_file_relpath,
            verified=verified,
        )
        return verified, candidate

    def _read_back(self, item: RetainedRegistrationPlanItemV1) -> ItemOutcome:
        with self._uow_factory() as uow:
            # Waits for any still-open writer of this failure to finish.
            failed = uow.source_accesses.get_for_update(item.failed_source_access_id)
            receipt = uow.source_accesses.successful_recovery_for(item.failed_source_access_id)
            if receipt is None:
                return _stopped(
                    item,
                    "COMMIT_NOT_APPLIED",
                    "the registration transaction did not commit; nothing was recorded",
                )
            try:
                document = _require_same_obligation_for_item(uow, receipt, item, failed=failed)
            except SourceRecoveryError as exc:
                return _stopped(item, exc.error_code, exc.message)
        return ItemOutcome(
            sequence=item.sequence,
            failed_source_access_id=item.failed_source_access_id,
            state="committed",
            receipt_source_access_id=receipt.source_access_id,
            document_id=document.document_id,
            readback_after_unknown_commit=True,
        )


class RetainedRegistrationReconciler:
    """Read-only: the database receipts are the authority for every item."""

    def __init__(self, *, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def reconcile(
        self, plan: RetainedRegistrationPlanV1, *, plan_sha256: str
    ) -> RetainedRegistrationReconciliation:
        if plan.sha256() != plan_sha256:
            raise SourceRecoveryError("PLAN_HASH_MISMATCH", "plan bytes do not match plan_sha256")
        items: list[ReconciledItem] = []
        with self._uow_factory() as uow:
            for item in plan.items:
                items.append(_reconcile_item(uow, item))
        return RetainedRegistrationReconciliation(plan_sha256=plan_sha256, items=tuple(items))


def _reconcile_item(uow: UnitOfWork, item: RetainedRegistrationPlanItemV1) -> ReconciledItem:
    failed = uow.source_accesses.get(item.failed_source_access_id)
    unchanged = (
        failed is not None
        and failed_access_projection_sha256(failed) == item.failed_access_projection_sha256
    )
    receipt = uow.source_accesses.successful_recovery_for(item.failed_source_access_id)
    facts = {
        fact.source_access_id: fact
        for fact in uow.source_accesses.download_failure_resolutions(
            provider_document_id=item.provider_document_id
        )
    }
    fact = facts.get(item.failed_source_access_id)
    if receipt is None:
        return ReconciledItem(
            sequence=item.sequence,
            failed_source_access_id=item.failed_source_access_id,
            state="unresolved",
            failure_history_unchanged=unchanged,
            receipt_source_access_id=None,
            document_id=None,
        )
    try:
        document = _require_same_obligation_for_item(uow, receipt, item, failed=failed)
    except SourceRecoveryError as exc:
        return ReconciledItem(
            sequence=item.sequence,
            failed_source_access_id=item.failed_source_access_id,
            state="conflict",
            failure_history_unchanged=unchanged,
            receipt_source_access_id=receipt.source_access_id,
            document_id=None,
            detail=f"{exc.error_code}: {exc.message}",
        )
    if fact is None or fact.resolved_by_source_access_id != receipt.source_access_id:
        return ReconciledItem(
            sequence=item.sequence,
            failed_source_access_id=item.failed_source_access_id,
            state="conflict",
            failure_history_unchanged=unchanged,
            receipt_source_access_id=receipt.source_access_id,
            document_id=document.document_id,
            detail="the queue facts view does not count this receipt as the resolution",
        )
    return ReconciledItem(
        sequence=item.sequence,
        failed_source_access_id=item.failed_source_access_id,
        state="resolved",
        failure_history_unchanged=unchanged,
        receipt_source_access_id=receipt.source_access_id,
        document_id=document.document_id,
    )


def _binding_sha256(uow: UnitOfWork, binding_source_access_id: str) -> str:
    access = uow.source_accesses.get(binding_source_access_id)
    if (
        access is None
        or access.provider != PROVIDER
        or access.provider_interface != HISTORICAL_SECURITY_BINDING_INTERFACE
        or access.status != "ok"
        or not access.result_hash
        or access.security_id is None
    ):
        raise SourceRecoveryError(
            "BINDING_NOT_FOUND",
            f"{binding_source_access_id} is not a recorded historical security binding",
        )
    security = uow.securities.get(access.security_id)
    if security is None or security.status != HISTORICAL_SECURITY_STATUS:
        raise SourceRecoveryError(
            "BINDING_NOT_FOUND",
            f"binding {binding_source_access_id} does not name a historical security",
        )
    return str(access.result_hash)


def _recoverable_failure(
    failed: e.SourceAccess | None, failed_id: str
) -> tuple[e.SourceAccess, _FailureFacts]:
    if (
        failed is None
        or failed.provider != PROVIDER
        or failed.provider_interface != DOWNLOAD_FAILURE_INTERFACE
        or failed.status != "failed"
    ):
        raise SourceRecoveryError(
            "NOT_A_FAILED_DOWNLOAD", f"{failed_id} is not a failed CNINFO download attempt"
        )
    try:
        error = json.loads(failed.error or "")
    except json.JSONDecodeError:
        error = None
    if not isinstance(error, dict):
        raise SourceRecoveryError("FAILURE_NOT_RECOVERABLE", f"{failed_id} has no structured error")
    error_code = error.get("error_code")
    if (
        error_code not in RECOVERABLE_FAILURE_ERROR_CODES
        or error.get("retryable") is not False
        or error.get("stage") != "download"
        or error.get("failure_phase", "registration") != "registration"
    ):
        raise SourceRecoveryError(
            "FAILURE_NOT_RECOVERABLE",
            f"{failed_id} is {error_code!r} (retryable={error.get('retryable')!r}, "
            f"phase={error.get('failure_phase', 'legacy')!r}); only non-retryable "
            f"registration failures {sorted(RECOVERABLE_FAILURE_ERROR_CODES)} are recoverable",
        )
    query = failed.query_params if isinstance(failed.query_params, Mapping) else {}
    provider_document_id = query.get("provider_document_id")
    error_document_id = error.get("provider_document_id")
    if (
        not isinstance(provider_document_id, str)
        or not provider_document_id
        or provider_document_id == "unknown"
        or (error_document_id is not None and error_document_id != provider_document_id)
    ):
        raise SourceRecoveryError(
            "FAILURE_NOT_RECOVERABLE", f"{failed_id} does not name one provider document"
        )
    snapshot = failed.result_snapshot if isinstance(failed.result_snapshot, Mapping) else {}
    archive = snapshot.get("archive")
    recorded_archive: Mapping[str, object] | None = None
    if archive is not None:
        if not isinstance(archive, Mapping) or archive.get("archive_completed") is not True:
            raise SourceRecoveryError(
                "FAILURE_HAS_NO_ARCHIVE",
                f"{failed_id} failed before its PDF was archived; nothing is retained",
            )
        recorded_archive = archive
    recorded_index = query.get("index_source_access_id")
    reason = snapshot.get("reason")
    return failed, _FailureFacts(
        provider_document_id=provider_document_id,
        error_code=str(error_code),
        reason=reason if isinstance(reason, str) else None,
        recorded_index_source_access_id=(
            recorded_index if isinstance(recorded_index, str) else None
        ),
        recorded_archive=recorded_archive,
    )


def _index_for_failure(
    uow: UnitOfWork,
    *,
    failed: e.SourceAccess,
    facts: _FailureFacts,
    index_source_access_id: str,
) -> e.SourceAccess:
    if (
        facts.recorded_index_source_access_id is not None
        and facts.recorded_index_source_access_id != index_source_access_id
    ):
        raise SourceRecoveryError(
            "INDEX_MISMATCH",
            f"failed access {failed.source_access_id} recorded index "
            f"{facts.recorded_index_source_access_id}, not {index_source_access_id}",
        )
    index = _required_access(uow, index_source_access_id)
    if index.accessed_at > failed.accessed_at:
        raise SourceRecoveryError(
            "INDEX_AFTER_FAILURE",
            f"index access {index_source_access_id} was taken after the failure it "
            "would explain",
        )
    return index


def _require_recorded_archive(
    facts: _FailureFacts, *, raw_file_relpath: str, raw_file_hash: str, byte_count: int
) -> None:
    """A failure that recorded its archive admits only that exact raw."""

    recorded = facts.recorded_archive
    if recorded is not None and (
        recorded.get("raw_file_hash") != raw_file_hash
        or recorded.get("byte_count") != byte_count
        or recorded.get("raw_file_relpath") != raw_file_relpath
    ):
        raise SourceRecoveryError(
            "RAW_ARCHIVE_MISMATCH",
            f"the failure record names archived raw {recorded.get('raw_file_relpath')!r}, "
            f"not {raw_file_relpath} ({raw_file_hash}/{byte_count} bytes)",
        )


def _required_access(uow: UnitOfWork, source_access_id: str) -> e.SourceAccess:
    access = uow.source_accesses.get(source_access_id)
    if access is None:
        raise SourceRecoveryError("INDEX_NOT_FOUND", f"{source_access_id} does not exist")
    return access


def _candidate_of(index: e.SourceAccess, provider_document_id: str) -> Mapping[str, object]:
    try:
        return index_candidate(index, provider_document_id)
    except RegistrationMetadataError as exc:
        raise SourceRecoveryError("INDEX_CANDIDATE_NOT_VERIFIED", str(exc)) from exc


def _verified_subject(
    uow: UnitOfWork,
    *,
    candidate: Mapping[str, object],
    index: e.SourceAccess,
    binding_source_access_id: str,
    subject_resolver: SubjectResolver,
) -> VerifiedAcquisitionSubject:
    try:
        return resolve_acquisition_subject(
            uow,
            candidate=candidate,
            index_source_access_id=index.source_access_id,
            subject_resolver=subject_resolver,
            expected_binding_source_access_id=binding_source_access_id,
        )
    except (RegistrationMetadataError, ContractViolation) as exc:
        raise SourceRecoveryError("SUBJECT_NOT_VERIFIED", str(exc)) from exc


def _acquisition(verified: VerifiedAcquisitionSubject) -> HistoricalAcquisitionProvenance:
    if verified.provenance is None:
        raise SourceRecoveryError(
            "SUBJECT_NOT_VERIFIED", "retained registration requires a historical binding"
        )
    return verified.provenance


def _require_no_other_blocker(
    uow: UnitOfWork,
    *,
    provider_document_id: str,
    failed_source_access_id: str,
    requested_failures: frozenset[str],
    max_download_retries: int,
) -> None:
    """After resolution the document must actually leave the dead letters."""

    failures = uow.source_accesses.download_failure_resolutions(
        provider_document_id=provider_document_id
    )
    if not any(item.source_access_id == failed_source_access_id for item in failures):
        raise SourceRecoveryError(
            "FAILURE_NOT_RECOVERABLE",
            f"{failed_source_access_id} is not a failed attempt of {provider_document_id}",
        )
    others = sorted(
        item.source_access_id
        for item in failures
        if item.nonretryable
        and item.resolved_by_source_access_id is None
        and item.source_access_id not in requested_failures
    )
    if others:
        raise SourceRecoveryError(
            "OTHER_UNRESOLVED_FAILURE",
            f"{provider_document_id} has other unresolved non-retryable failures "
            f"outside this request: {others}",
        )
    if len(failures) >= max_download_retries:
        raise SourceRecoveryError(
            "RETRY_BUDGET_EXHAUSTED",
            f"{provider_document_id} has {len(failures)} failed attempts "
            f"(budget {max_download_retries}); a retained registration does not "
            "reset the retry budget",
        )


def _document_state(
    uow: UnitOfWork,
    *,
    provider_document_id: str,
    raw_file_hash: str,
    raw_file_relpath: str,
    verified: VerifiedAcquisitionSubject,
) -> tuple[str, str | None]:
    latest = uow.documents.latest_by_provider_document(
        provider=PROVIDER, provider_document_id=provider_document_id
    )
    if latest is not None and latest.raw_file_hash != raw_file_hash:
        raise SourceRecoveryError(
            "NEWER_VERSION_EXISTS",
            f"document {latest.document_id} registers another raw version of "
            f"{provider_document_id}; a retained archive never supersedes it",
        )
    existing = uow.documents.get_by_provider_document_and_hash(
        provider=PROVIDER,
        provider_document_id=provider_document_id,
        raw_file_hash=raw_file_hash,
    )
    if existing is None:
        return "absent", None
    if (
        existing.company_id != verified.subject.company.company_id
        or existing.security_id != verified.subject.security.security_id
        or existing.raw_file_relpath != raw_file_relpath
    ):
        raise SourceRecoveryError(
            "DOCUMENT_SUBJECT_CONFLICT",
            f"document {existing.document_id} registers these bytes under another "
            "subject or archive path",
        )
    return "existing_same_subject", existing.document_id


def _require_same_obligation(
    uow: UnitOfWork,
    *,
    receipt: e.SourceAccess,
    failed: e.SourceAccess,
    provider_document_id: str,
    raw_file_hash: str,
    verified: VerifiedAcquisitionSubject,
) -> e.Document:
    return _receipt_document(
        uow,
        receipt=receipt,
        failed=failed,
        provider_document_id=provider_document_id,
        raw_file_hash=raw_file_hash,
        company_id=verified.subject.company.company_id,
        security_id=verified.subject.security.security_id,
    )


def _require_same_obligation_for_item(
    uow: UnitOfWork,
    receipt: e.SourceAccess,
    item: RetainedRegistrationPlanItemV1,
    *,
    failed: e.SourceAccess | None,
) -> e.Document:
    return _receipt_document(
        uow,
        receipt=receipt,
        failed=failed,
        provider_document_id=item.provider_document_id,
        raw_file_hash=item.raw_file_hash,
        company_id=item.target_company_id,
        security_id=item.target_security_id,
    )


def _receipt_document(
    uow: UnitOfWork,
    *,
    receipt: e.SourceAccess,
    failed: e.SourceAccess | None,
    provider_document_id: str,
    raw_file_hash: str,
    company_id: str,
    security_id: str,
) -> e.Document:
    """The receipt resolves ``failed`` exactly as the queue facts view counts
    a resolution, and it is this item's obligation (raw and subject)."""

    failed_query = (
        failed.query_params
        if failed is not None and isinstance(failed.query_params, Mapping)
        else {}
    )
    if (
        failed is None
        or failed.provider != PROVIDER
        or failed.provider_interface != DOWNLOAD_FAILURE_INTERFACE
        or failed.status != "failed"
        or failed_query.get("provider_document_id") != provider_document_id
    ):
        raise SourceRecoveryError(
            "FAILURE_RECORD_CHANGED",
            f"receipt {receipt.source_access_id} does not name the failed download "
            f"of {provider_document_id}",
        )
    receipt_query = receipt.query_params if isinstance(receipt.query_params, Mapping) else {}
    if (
        receipt.recovery_of_source_access_id != failed.source_access_id
        or receipt.provider != failed.provider
        or receipt.provider_interface != RETAINED_REGISTRATION_INTERFACE
        or receipt.status != "ok"
        or receipt_query.get("provider_document_id") != provider_document_id
    ):
        raise SourceRecoveryError(
            "RECEIPT_CONFLICT",
            f"receipt {receipt.source_access_id} is not a successful retained "
            f"registration of failed access {failed.source_access_id}",
        )
    if (
        receipt.result_hash != raw_file_hash
        or receipt.company_id != company_id
        or receipt.security_id != security_id
    ):
        raise SourceRecoveryError(
            "RECEIPT_CONFLICT",
            f"receipt {receipt.source_access_id} resolves this failure with a "
            "different raw or subject",
        )
    document = uow.documents.get_by_provider_document_and_hash(
        provider=receipt.provider,
        provider_document_id=provider_document_id,
        raw_file_hash=raw_file_hash,
    )
    if (
        document is None
        or document.company_id != receipt.company_id
        or document.security_id != receipt.security_id
    ):
        raise SourceRecoveryError(
            "RECEIPT_CONFLICT",
            f"receipt {receipt.source_access_id} has no matching document",
        )
    return document


def _stopped(item: RetainedRegistrationPlanItemV1, error_code: str, message: str) -> ItemOutcome:
    return ItemOutcome(
        sequence=item.sequence,
        failed_source_access_id=item.failed_source_access_id,
        state="stopped",
        error_code=error_code,
        message=message,
    )


def _candidate_text(candidate: Mapping[str, object], key: str) -> str:
    value = candidate.get(key)
    if value is None or value == "":
        raise SourceRecoveryError("INDEX_CANDIDATE_NOT_VERIFIED", f"candidate missing {key}")
    return str(value)


def _candidate_announcement_date(candidate: Mapping[str, object]) -> date:
    value = _candidate_text(candidate, "announcement_date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SourceRecoveryError(
            "INDEX_CANDIDATE_NOT_VERIFIED", f"announcement_date is not ISO: {value!r}"
        ) from exc


__all__ = [
    "ItemOutcome",
    "PreviewRefusal",
    "ReconciledItem",
    "RetainedRegistrationExecutionResult",
    "RetainedRegistrationExecutor",
    "RetainedRegistrationPreview",
    "RetainedRegistrationPreviewResult",
    "RetainedRegistrationReconciler",
    "RetainedRegistrationReconciliation",
]
