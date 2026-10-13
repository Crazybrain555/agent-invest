"""Repository ports.

Repositories persist and load domain entities. Concrete implementations live in
``adapters/db/postgres``. Use cases depend only on these protocols.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, fields

from typing import Optional, Protocol

from disclosure_anchor.application.contracts.remote_parse_checkpoint import (
    EncodedCheckpointReceipt,
    EncodedTerminalReceipt,
    RemoteParseAttempt,
    RemoteParseResumeSecret,
)
from disclosure_anchor.application.contracts.parse_requeue_decision import (
    ParseRequeueDecisionRecord,
)
from disclosure_anchor.application.contracts.staged_credit import (
    CreditVector,
    DatabaseLeaseSnapshot,
)
from disclosure_anchor.application.contracts.publish_evidence_ledger import (
    DurablePublishBaseEvidence,
    DurablePublishSupplementEvidence,
    EncodedProgressRelayCheckpoint,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    DurableCheckpointWitness,
    ProviderAckCompletionWitness,
    RecoveredV3ResumeSecret,
)

from disclosure_anchor.domain.entities import (
    Company,
    CompanyIdentifier,
    Document,
    DocumentUnit,
    OutboxEvent,
    ProcessingRun,
    Security,
    SourceAccess,
    SourceCheckpoint,
    TrackedCompany,
)


class CompanyRepository(Protocol):
    def add(self, company: Company) -> Company: ...
    def get(self, company_id: str) -> Optional[Company]: ...
    def get_by_legal_name(self, legal_name: str) -> Optional[Company]: ...
    def get_by_credit_code(self, uscc: str) -> Optional[Company]: ...
    def update(self, company: Company) -> Company: ...


class CompanyIdentifierRepository(Protocol):
    def add(self, identifier: CompanyIdentifier) -> CompanyIdentifier: ...
    def get(self, identifier_id: str) -> Optional[CompanyIdentifier]: ...
    def get_by_scheme_value(
        self, scheme: str, normalized_value: str
    ) -> Optional[CompanyIdentifier]: ...
    def list_by_scheme_value(
        self, scheme: str, normalized_value: str
    ) -> list[CompanyIdentifier]:
        """Every row for the value, any status; never a first-match answer."""
        ...
    def update(self, identifier: CompanyIdentifier) -> CompanyIdentifier: ...


class SecurityRepository(Protocol):
    def add(self, security: Security) -> Security: ...
    def get(self, security_id: str) -> Optional[Security]: ...
    def get_for_update(self, security_id: str) -> Optional[Security]:
        """Row-lock one security (FOR NO KEY UPDATE) inside the active UoW."""
        ...
    def get_by_code_exchange(self, security_code: str, exchange: str) -> Optional[Security]: ...


class TrackedCompanyRepository(Protocol):
    def add(self, tracked_company: TrackedCompany) -> TrackedCompany: ...
    def get(self, tracked_company_id: str) -> Optional[TrackedCompany]: ...
    def get_by_company_id(self, company_id: str) -> Optional[TrackedCompany]: ...
    def update(self, tracked_company: TrackedCompany) -> TrackedCompany: ...
    def list_all(self) -> list[TrackedCompany]: ...
    def delete(self, tracked_company_id: str) -> None: ...


class PendingDownloadCandidate(Mapping[str, object]):
    """One queued index candidate plus the index access that carried it.

    It reads as the candidate mapping itself, so field lookups keep working,
    while ``DownloadDocumentCommand`` takes the exact index access id from it
    instead of guessing one from the latest snapshot.
    """

    __slots__ = ("_candidate", "_index_source_access_id")

    def __init__(
        self, *, candidate: Mapping[str, object], index_source_access_id: str
    ) -> None:
        if not isinstance(index_source_access_id, str) or not index_source_access_id:
            raise ValueError("pending candidate requires its index source access id")
        self._candidate = candidate
        self._index_source_access_id = index_source_access_id

    @property
    def candidate(self) -> Mapping[str, object]:
        return self._candidate

    @property
    def index_source_access_id(self) -> str:
        return self._index_source_access_id

    def __getitem__(self, key: str) -> object:
        return self._candidate[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._candidate)

    def __len__(self) -> int:
        return len(self._candidate)

    def __repr__(self) -> str:
        return (
            "PendingDownloadCandidate("
            f"index_source_access_id={self._index_source_access_id!r}, "
            f"candidate={dict(self._candidate)!r})"
        )


@dataclass(frozen=True)
class DownloadFailureResolution:
    """One failed download attempt as ops.download_failure_resolution_v1 sees it."""

    source_access_id: str
    nonretryable: bool
    resolved_by_source_access_id: Optional[str]


class SourceAccessRepository(Protocol):
    def add(self, source_access: SourceAccess) -> SourceAccess: ...
    def get(self, source_access_id: str) -> Optional[SourceAccess]: ...
    def get_for_update(self, source_access_id: str) -> Optional[SourceAccess]:
        """Row-lock one access (FOR NO KEY UPDATE) inside the active UoW."""
        ...
    def successful_recovery_for(
        self, failed_source_access_id: str
    ) -> Optional[SourceAccess]:
        """The one access whose recovery link names this failure, if any.

        The caller still proves it is the resolution it expects (provider,
        interface, status, link, document); the link alone proves nothing.
        """
        ...
    def list_historical_security_bindings(
        self, *, security_id: str
    ) -> list[SourceAccess]:
        """Every binding access recorded for one historical security."""
        ...
    def download_failure_resolutions(
        self, *, provider_document_id: str
    ) -> list[DownloadFailureResolution]:
        """Every failed CNINFO download attempt of one provider document."""
        ...
    def list_candidate_snapshots(
        self, *, provider: str, provider_interface: str, company_id: str
    ) -> list[dict[str, object]]: ...
    def list_pending_download_candidates(
        self,
        *,
        provider: str,
        index_interfaces: Sequence[str],
        download_interface: str,
        max_retries: int,
        overlap_start: object,
    ) -> list[PendingDownloadCandidate]: ...


class SourceCheckpointRepository(Protocol):
    def add(self, checkpoint: SourceCheckpoint) -> SourceCheckpoint: ...
    def get(self, source_checkpoint_id: str) -> Optional[SourceCheckpoint]: ...
    def get_by_scope(self, provider: str, scope_key: str) -> Optional[SourceCheckpoint]: ...
    def update(self, checkpoint: SourceCheckpoint) -> SourceCheckpoint: ...


class DocumentRepository(Protocol):
    def add(self, document: Document) -> Document: ...
    def get(self, document_id: str) -> Optional[Document]: ...
    def get_for_update(self, document_id: str) -> Optional[Document]: ...
    def update(self, document: Document) -> Document: ...
    def get_by_provider_document_and_hash(
        self, *, provider: str, provider_document_id: str, raw_file_hash: str
    ) -> Optional[Document]: ...
    def latest_by_provider_document(
        self, *, provider: str, provider_document_id: str
    ) -> Optional[Document]: ...


class ProcessingRunRepository(Protocol):
    def add(self, run: ProcessingRun) -> ProcessingRun: ...
    def get(self, processing_run_id: str) -> Optional[ProcessingRun]: ...
    def latest_succeeded_provider_run_for_document(
        self, document_id: str
    ) -> Optional[ProcessingRun]: ...
    def succeeded_provider_runs_for_document(
        self, document_id: str
    ) -> tuple[ProcessingRun, ...]: ...
    def update(self, run: ProcessingRun) -> ProcessingRun: ...
    def parse_requeue_decision_for_run(
        self, processing_run_id: str
    ) -> Optional[ParseRequeueDecisionRecord]: ...
    def add_parse_requeue_decision(
        self, decision: ParseRequeueDecisionRecord
    ) -> ParseRequeueDecisionRecord: ...


@dataclass(frozen=True, slots=True)
class ClaimedAttemptSnapshot:
    attempt: RemoteParseAttempt
    database_lease: DatabaseLeaseSnapshot

    def __post_init__(self) -> None:
        if type(self.attempt) is not RemoteParseAttempt:
            raise ValueError("claimed snapshot requires an exact attempt")
        if type(self.database_lease) is not DatabaseLeaseSnapshot:
            raise ValueError("claimed snapshot requires an exact database lease")
        if (
            self.attempt.claim_owner_identity is None
            or self.attempt.claim_lease_until != self.database_lease.lease_until_utc
        ):
            raise ValueError("claimed snapshot lease drifted from attempt")


@dataclass(frozen=True, slots=True)
class CreditTransitionGrant:
    expected_current: CreditVector
    maximum_positive_delta: CreditVector

    def __post_init__(self) -> None:
        if type(self.expected_current) is not CreditVector or type(
            self.maximum_positive_delta
        ) is not CreditVector:
            raise ValueError("credit transition grant requires exact vectors")

    def permits(self, candidate: CreditVector) -> bool:
        if type(candidate) is not CreditVector:
            raise ValueError("candidate credits require an exact vector")
        positive_delta = CreditVector(
            **{
                item.name: max(
                    0,
                    getattr(candidate, item.name)
                    - getattr(self.expected_current, item.name),
                )
                for item in fields(self.expected_current)
            }
        )
        return positive_delta.fits(self.maximum_positive_delta)


class V3ResumeSecretRecoveryError(RuntimeError):
    """A private v3 token cannot be recovered without weakening identity."""


class V3ResumeSecretMissing(V3ResumeSecretRecoveryError):
    pass


class V3ResumeSecretIdentityMismatch(V3ResumeSecretRecoveryError):
    pass


class V3ResumeSecretStaleOwner(V3ResumeSecretRecoveryError):
    pass


class V3ResumeSecretKeyUnavailable(V3ResumeSecretRecoveryError):
    """A wrapped token exists but its decryption key is unavailable."""


class RemoteParseAttemptRepository(Protocol):
    def add(
        self, attempt: RemoteParseAttempt,
        submission_secret: RemoteParseResumeSecret,
    ) -> RemoteParseAttempt: ...
    def add_v3_prepared(
        self, attempt: RemoteParseAttempt,
        prepared_secret: RemoteParseResumeSecret,
    ) -> RemoteParseAttempt: ...
    def get(self, attempt_id: str) -> Optional[RemoteParseAttempt]: ...
    def durable_checkpoint_witness(
        self, attempt_id: str
    ) -> DurableCheckpointWitness: ...
    def get_current_for_document(
        self, document_id: str
    ) -> Optional[RemoteParseAttempt]: ...
    def list_recoverable(
        self, *, after_attempt_id: str | None, limit: int
    ) -> list[RemoteParseAttempt]: ...
    def list_v3_recoverable(
        self, *, after_attempt_id: str | None, limit: int,
    ) -> list[RemoteParseAttempt]: ...
    def claim_v3_recovery(
        self, *, attempt_id: str, fence_identity: str, expected_state: str,
        expected_version: int, expected_current: CreditVector,
        owner_identity: str, lease_seconds: int,
    ) -> ClaimedAttemptSnapshot: ...
    def renew_v3_claim(
        self, *, attempt_id: str, fence_identity: str, expected_state: str,
        expected_version: int, expected_current: CreditVector,
        owner_identity: str, claim_generation: int, lease_seconds: int,
    ) -> ClaimedAttemptSnapshot: ...
    def reload_v3_claim(
        self, *, attempt_id: str, fence_identity: str, expected_state: str,
        expected_version: int, expected_current: CreditVector,
        owner_identity: str, claim_generation: int,
    ) -> ClaimedAttemptSnapshot: ...
    def reconcile_v3_reconciling_after_race(
        self, *, expected_attempt: RemoteParseAttempt,
    ) -> ClaimedAttemptSnapshot: ...
    def reconcile_v3_submitted_after_race(
        self, *, expected_attempt: RemoteParseAttempt,
    ) -> ClaimedAttemptSnapshot: ...
    def reconcile_v3_terminal_after_race(
        self, *, expected_attempt: RemoteParseAttempt,
    ) -> ClaimedAttemptSnapshot: ...
    def reconcile_v3_materialization_after_race(
        self, *, expected_attempt: RemoteParseAttempt,
    ) -> ClaimedAttemptSnapshot: ...
    def reconcile_v3_local_after_race(
        self, *, expected_attempt: RemoteParseAttempt,
    ) -> ClaimedAttemptSnapshot: ...
    def reconcile_v3_finish_after_race(
        self, *, expected_attempt: RemoteParseAttempt,
    ) -> ClaimedAttemptSnapshot: ...
    def reconcile_v3_remote_failure_after_race(
        self, *, expected_attempt: RemoteParseAttempt,
        receipt: EncodedCheckpointReceipt,
    ) -> ClaimedAttemptSnapshot: ...
    def reconcile_v3_local_failure_after_race(
        self, *, expected_attempt: RemoteParseAttempt,
        receipt: EncodedCheckpointReceipt,
        local_receipt: EncodedCheckpointReceipt | None = None,
    ) -> ClaimedAttemptSnapshot: ...
    def reconcile_v3_pre_submission_failure_after_race(
        self, *, expected_attempt: RemoteParseAttempt,
        receipt: EncodedCheckpointReceipt,
    ) -> RemoteParseAttempt: ...
    def transition_v3_reconciling(
        self, *, expected_attempt: RemoteParseAttempt,
        grant: CreditTransitionGrant,
    ) -> ClaimedAttemptSnapshot: ...
    def checkpoint_v3_submitted(
        self, *, expected_attempt: RemoteParseAttempt,
        grant: CreditTransitionGrant, receipt: EncodedCheckpointReceipt,
        accepted_secret: RemoteParseResumeSecret,
    ) -> ClaimedAttemptSnapshot: ...
    def checkpoint_v3_terminal(
        self, *, expected_attempt: RemoteParseAttempt,
        grant: CreditTransitionGrant, receipt: EncodedTerminalReceipt,
        terminal_secret: RemoteParseResumeSecret,
    ) -> ClaimedAttemptSnapshot: ...
    def prepare_v3_materialization(
        self, *, expected_attempt: RemoteParseAttempt,
        grant: CreditTransitionGrant, receipt: EncodedCheckpointReceipt,
        materialization_secret: RemoteParseResumeSecret,
    ) -> ClaimedAttemptSnapshot: ...
    def checkpoint_v3_local(
        self, *, expected_attempt: RemoteParseAttempt,
        grant: CreditTransitionGrant, receipt: EncodedCheckpointReceipt,
    ) -> ClaimedAttemptSnapshot: ...
    def finish_v3_run_and_checkpoint(
        self, *, expected_attempt: RemoteParseAttempt,
        grant: CreditTransitionGrant, finished_run: ProcessingRun,
    ) -> ClaimedAttemptSnapshot: ...
    def fail_v3_pre_submission(
        self, *, expected_attempt: RemoteParseAttempt,
        receipt: EncodedCheckpointReceipt,
    ) -> RemoteParseAttempt: ...
    def fail_v3_remote(
        self, *, expected_attempt: RemoteParseAttempt,
        grant: CreditTransitionGrant, receipt: EncodedCheckpointReceipt,
    ) -> ClaimedAttemptSnapshot: ...
    def fail_v3_local(
        self, *, expected_attempt: RemoteParseAttempt,
        grant: CreditTransitionGrant, receipt: EncodedCheckpointReceipt,
        local_receipt: EncodedCheckpointReceipt | None = None,
    ) -> ClaimedAttemptSnapshot: ...
    def finalize_v3_ack(
        self, *, expected_attempt: RemoteParseAttempt,
        witness: ProviderAckCompletionWitness,
    ) -> RemoteParseAttempt: ...
    def recover_v3_resume_secret(
        self, *, attempt_id: str, fence_identity: str, secret_kind: str,
        expected_token_sha256: str, expected_token_byte_count: int,
        expected_state: str, expected_row_version: int,
        claim_owner_identity: str, claim_generation: int,
    ) -> RecoveredV3ResumeSecret: ...
    def claim_recovery(
        self, *, attempt_id: str, fence_identity: str, expected_version: int,
        owner_identity: str, lease_seconds: int,
    ) -> RemoteParseAttempt: ...
    def renew_recovery_claim(
        self, *, attempt_id: str, fence_identity: str, owner_identity: str,
        claim_generation: int, lease_seconds: int,
    ) -> RemoteParseAttempt: ...
    def checkpoint_submitted(
        self, *, attempt_id: str, fence_identity: str, expected_version: int,
        remote_task_identity: str, receipt: EncodedCheckpointReceipt,
        accepted_secret: RemoteParseResumeSecret,
        claim_owner_identity: str, claim_generation: int,
    ) -> RemoteParseAttempt: ...
    def transition(
        self, *, attempt_id: str, fence_identity: str, expected_state: str,
        expected_version: int, next_state: str, claim_owner_identity: str,
        claim_generation: int,
    ) -> RemoteParseAttempt: ...
    def checkpoint_terminal(
        self, *, attempt_id: str, fence_identity: str, expected_version: int,
        remote_task_identity: str, receipt: EncodedTerminalReceipt,
        terminal_secret: RemoteParseResumeSecret, claim_owner_identity: str,
        claim_generation: int,
    ) -> RemoteParseAttempt: ...
    def checkpoint_local(
        self, *, attempt_id: str, fence_identity: str, expected_version: int,
        claim_owner_identity: str, claim_generation: int,
        receipt: EncodedCheckpointReceipt,
    ) -> RemoteParseAttempt: ...
    def fail_run_and_checkpoint(
        self, *, document_id: str, processing_run_id: str,
        attempt_id: str, fence_identity: str, expected_state: str,
        expected_version: int, claim_owner_identity: str, claim_generation: int,
        receipt: EncodedCheckpointReceipt,
    ) -> RemoteParseAttempt: ...
    def finish_run_and_checkpoint(
        self, *, finished_run: ProcessingRun, attempt_id: str,
        fence_identity: str, expected_version: int, claim_owner_identity: str,
        claim_generation: int,
    ) -> RemoteParseAttempt: ...
    def get_secret(
        self, attempt_id: str, secret_kind: str
    ) -> Optional[RemoteParseResumeSecret]: ...


class DocumentUnitRepository(Protocol):
    def add(self, unit: DocumentUnit) -> DocumentUnit: ...
    def add_many(self, units: list[DocumentUnit]) -> list[DocumentUnit]: ...
    def get(self, asset_id: str) -> Optional[DocumentUnit]: ...
    def list_by_processing_run(self, processing_run_id: str) -> list[DocumentUnit]: ...
    def list_by_document_active(self, document_id: str) -> list[DocumentUnit]: ...


class OutboxRepository(Protocol):
    def add(self, event: OutboxEvent) -> OutboxEvent: ...
    def get(self, event_id: str) -> Optional[OutboxEvent]: ...


class PublishEvidenceRepository(Protocol):
    def add_base(self, evidence: DurablePublishBaseEvidence) -> DurablePublishBaseEvidence: ...
    def append_supplement(
        self, evidence: DurablePublishSupplementEvidence
    ) -> DurablePublishSupplementEvidence: ...
    def append_relay_head(
        self, checkpoint: EncodedProgressRelayCheckpoint
    ) -> EncodedProgressRelayCheckpoint: ...
    def latest_relay_head(self, relay_id: str) -> Optional[EncodedProgressRelayCheckpoint]: ...
