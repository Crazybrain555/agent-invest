"""Seven bounded V4 stage transitions over one durable PostgreSQL authority.

The scheduler owns concurrency and retry timing.  This backend owns only one
stage episode at a time: reload exact durable facts, perform the bounded side
effect through an existing port, and append/reconcile exactly one successor.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields
from math import isfinite
from pathlib import Path
import time
from typing import Protocol, TypeVar, cast

from disclosure_anchor.application.contracts.provider_secret_envelope_v4 import (
    ProviderSecretPlaintextV4,
    SealedProviderSecretV4,
    bind_provider_secret_v4,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    AcceptedSubmissionReceiptV4,
    EncodedRemoteParseEvidenceV4,
    FailureOutcomeV4,
    FailureReceiptV4,
    PreparationIntentV4,
    SnapshotReceiptV4,
    SubmissionIntentV4,
    TerminalReceiptV4,
    encode_remote_parse_evidence_v4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    CheckpointStateV4,
    CleanupResourceEntryV4,
    LocalResourceKind,
    LocalCleanupPlanV4,
    LocalCleanupReceiptV4,
    LocalMaterializationReceiptV4,
    MaterializationIntentV4,
    RemoteParseCheckpointV4,
    ResourceReservationV4,
    advance_remote_parse_checkpoint_v4,
    build_local_cleanup_plan_v4,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    CleanupOutcome,
    PerAttemptResourceAllowance,
    ResourceCreditVector,
)
from disclosure_anchor.application.ports.provider_secret_cipher_v4 import (
    ProviderSecretCipherPort,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RecoveryCandidate,
    RemoteParseV4Authority,
    V4SuccessorAppend,
)
from disclosure_anchor.application.ports.remote_provider_v4 import (
    PinnedSnapshotSourceV4,
    RemotePollCommandV4,
    RemoteProviderCompletedV4,
    RemoteProviderFailedV4,
    RemoteProviderProtocolErrorV4,
    RemoteProviderUnavailableV4,
    RemoteProviderV4Port,
    RemoteProviderWaitingV4,
    RemoteSubmissionCommandV4,
    RemoteSubmissionAmbiguousV4,
)
from disclosure_anchor.domain.errors import ParserOutputContractError
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializedProviderDocumentV4,
    PrivateProviderCapabilityV4,
    V4ClaimGuard,
    V4EvidenceReplayContext,
    V4MaterializationPort,
    V4ClaimWitness,
    seal_provider_ack_command_v4,
)
from disclosure_anchor.application.services.atomic_publication_request_factory_v4 import (
    RecoverableAtomicPublicationRequestFactoryV4,
)
from disclosure_anchor.application.services.staged_coordinator_persistence_v4 import (
    DurableStagedCoordinatorPersistenceV4,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    AdmissionOutcome,
    CoordinatorWork,
    RetryStage,
    StageLeaseGuard,
    StageWaiting,
)
from disclosure_anchor.application.use_cases.prepare_and_publish_whole_document_v4 import (
    PrepareAndPublishWholeDocumentV4,
)


class V4StageInputResolver(Protocol):
    """Hash-addressed derivations that do not own durable lifecycle state.

    Implementations must reopen exact inputs by the hashes carried by
    ``RemoteParseV4Authority``.  Current mutable settings are not a legal
    substitute after a restart.
    """

    def source_pdf(self, authority: RemoteParseV4Authority) -> Path: ...

    def submission_intent(
        self,
        authority: RemoteParseV4Authority,
        snapshot: SnapshotReceiptV4,
    ) -> SubmissionIntentV4: ...

    def submission_command(
        self,
        authority: RemoteParseV4Authority,
        *,
        snapshot: SnapshotReceiptV4,
        intent: SubmissionIntentV4,
        snapshot_source: PinnedSnapshotSourceV4,
        stage_guard: StageLeaseGuard,
    ) -> RemoteSubmissionCommandV4: ...

    def poll_command(
        self,
        authority: RemoteParseV4Authority,
        *,
        intent: SubmissionIntentV4,
        accepted: AcceptedSubmissionReceiptV4,
        capability: PrivateProviderCapabilityV4,
        stage_guard: StageLeaseGuard,
    ) -> RemotePollCommandV4: ...

    def materialization_intent(
        self,
        authority: RemoteParseV4Authority,
        *,
        accepted: AcceptedSubmissionReceiptV4,
        terminal: TerminalReceiptV4,
        capability: PrivateProviderCapabilityV4,
    ) -> MaterializationIntentV4: ...

    def materialization_allowance(
        self,
        authority: RemoteParseV4Authority,
        intent: MaterializationIntentV4,
    ) -> PerAttemptResourceAllowance: ...

    def result_lease_seconds(self, authority: RemoteParseV4Authority) -> int: ...

    def remote_runaway_seconds(self, authority: RemoteParseV4Authority) -> int: ...


class ExpectedV4AttemptFailure(RuntimeError):
    """Explicit item-local failure; authority/integrity errors must not use it."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        retryable: bool = False,
        retry_budget_class: str = "deterministic",
    ) -> None:
        for value, label in (
            (error_code, "error code"),
            (retry_budget_class, "retry budget class"),
        ):
            if type(value) is not str or not value.strip() or len(value) > 1024:
                raise ValueError(f"expected V4 failure {label} is invalid")
        if (
            type(message) is not str
            or not message.strip()
            or len(message) > 4096
        ):
            raise ValueError("expected V4 failure message is invalid")
        if type(retryable) is not bool:
            raise ValueError("expected V4 failure retryability is invalid")
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.retry_budget_class = retry_budget_class


class V4SubmissionSnapshotPort(V4MaterializationPort, Protocol):
    def submission_snapshot_source_v4(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        reservation: ResourceReservationV4,
        snapshot_receipt: SnapshotReceiptV4,
        submission_intent: SubmissionIntentV4,
        evidence: tuple[EncodedRemoteParseEvidenceV4, ...],
        resourceful_checkpoint_history: tuple[RemoteParseCheckpointV4, ...],
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
    ) -> PinnedSnapshotSourceV4: ...


_EvidenceT = TypeVar("_EvidenceT")


class DurableStagedCoordinatorBackendV4:
    """Mechanically composable, default-off implementation of all seven lanes."""

    def __init__(
        self,
        *,
        persistence: DurableStagedCoordinatorPersistenceV4,
        inputs: V4StageInputResolver,
        remote: RemoteProviderV4Port,
        materialization: V4SubmissionSnapshotPort,
        secret_cipher: ProviderSecretCipherPort,
        claim_guard: V4ClaimGuard,
        publication_requests: RecoverableAtomicPublicationRequestFactoryV4,
        publisher: PrepareAndPublishWholeDocumentV4,
        poll_seconds: float,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not 0 < poll_seconds <= 300:
            raise ValueError("v4 backend poll interval is invalid")
        if not callable(wall_clock):
            raise ValueError("v4 backend wall clock is invalid")
        self._persistence = persistence
        self._inputs = inputs
        self._remote = remote
        self._materialization = materialization
        self._secret_cipher = secret_cipher
        self._claim_guard = claim_guard
        self._publication_requests = publication_requests
        self._publisher = publisher
        self._poll_seconds = float(poll_seconds)
        self._wall_clock = wall_clock

    def list_recoverable(
        self,
        *,
        after_attempt_id: str | None,
        limit: int,
    ) -> tuple[RecoveryCandidate, ...]:
        return tuple(
            self._persistence.list_recoverable(
                after_attempt_id=after_attempt_id,
                limit=limit,
            )
        )

    def claim_recovery(self, candidate: RecoveryCandidate) -> CoordinatorWork:
        return self._persistence.claim_recovery(candidate)

    def admit_new(
        self,
        *,
        limit: int,
        available_credits: ResourceCreditVector,
    ) -> AdmissionOutcome:
        return self._persistence.admit_new(
            limit=limit,
            available_credits=available_credits,
        )

    def renew_claim(
        self,
        work: CoordinatorWork,
        *,
        lease_seconds: int,
    ) -> CoordinatorWork:
        return self._persistence.renew_claim(work, lease_seconds=lease_seconds)

    def reload_claim(self, work: CoordinatorWork) -> CoordinatorWork:
        return self._persistence.reload_claim(work)

    def prepare_remote_io(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        authority = self._authority(work, "prepared", stage_guard)
        reservation = self._reservation(authority)
        preparation = self._evidence(
            authority,
            "preparation_intent",
            PreparationIntentV4,
        )
        try:
            snapshot = self._optional_evidence(
                authority,
                "snapshot_receipt",
                SnapshotReceiptV4,
            )
            if snapshot is None:
                snapshot = self._materialization.create_or_reconcile_snapshot_v4(
                    checkpoint=authority.checkpoint,
                    reservation=reservation,
                    preparation_intent=preparation,
                    source_pdf=self._inputs.source_pdf(authority),
                    evidence=authority.evidence,
                    resourceful_checkpoint_history=authority.checkpoint_history,
                    claim=authority.claim_witness,
                    claim_guard=self._claim_guard,
                    stage_guard=stage_guard,
                )
            intent = self._inputs.submission_intent(authority, snapshot)
        except (ExpectedV4AttemptFailure, ParserOutputContractError) as exc:
            return self._fail_attempt(
                work,
                authority,
                outcome="pre_submission_failure",
                error=exc,
                error_stage="preflight",
                credit_allowance=credit_allowance,
                stage_guard=stage_guard,
            )
        successor = advance_remote_parse_checkpoint_v4(
            authority.checkpoint,
            state="reconciling",
            held_resource_credit=ResourceCreditVector(
                documents=1,
                snapshot_items=1,
                snapshot_bytes=authority.checkpoint.source_byte_count,
                remote_waits=1,
            ),
            snapshot_receipt_sha256=snapshot.sha256,
            submission_intent_sha256=intent.sha256,
        )
        self._require_credit_transition(
            authority.checkpoint,
            successor,
            credit_allowance,
        )
        new_evidence = [encode_remote_parse_evidence_v4(intent)]
        if authority.checkpoint.snapshot_receipt_sha256 is None:
            new_evidence.insert(0, encode_remote_parse_evidence_v4(snapshot))
        return self._append(
            work,
            authority,
            successor,
            new_evidence=tuple(new_evidence),
            stage_guard=stage_guard,
        )

    def run_remote(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        if work.state == "reconciling":
            return self._submit(
                work,
                credit_allowance=credit_allowance,
                stage_guard=stage_guard,
            )
        if work.state == "submitted":
            return self._poll(
                work,
                credit_allowance=credit_allowance,
                stage_guard=stage_guard,
            )
        raise ValueError("remote lane received an unsupported V4 state")

    def prepare_local_io(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        authority = self._authority(work, "remote_terminal", stage_guard)
        accepted = self._evidence(
            authority,
            "accepted_submission",
            AcceptedSubmissionReceiptV4,
        )
        terminal = self._evidence(
            authority,
            "terminal_receipt",
            TerminalReceiptV4,
        )
        capability = self._capability(authority, accepted, "result_download")
        try:
            intent = self._inputs.materialization_intent(
                authority,
                accepted=accepted,
                terminal=terminal,
                capability=capability,
            )
        except ExpectedV4AttemptFailure as exc:
            return self._fail_attempt(
                work,
                authority,
                outcome="local_failure",
                error=exc,
                error_stage="local_preflight",
                credit_allowance=credit_allowance,
                stage_guard=stage_guard,
            )
        successor = advance_remote_parse_checkpoint_v4(
            authority.checkpoint,
            state="materializing",
            held_resource_credit=intent.held_resource_credit,
            materialization_intent_sha256=intent.sha256,
        )
        self._require_credit_transition(
            authority.checkpoint,
            successor,
            credit_allowance,
        )
        return self._append(
            work,
            authority,
            successor,
            new_evidence=(encode_remote_parse_evidence_v4(intent),),
            stage_guard=stage_guard,
        )

    def run_local(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        authority = self._authority(work, "materializing", stage_guard)
        reservation = self._reservation(authority)
        preparation = self._evidence(
            authority,
            "preparation_intent",
            PreparationIntentV4,
        )
        accepted = self._evidence(
            authority,
            "accepted_submission",
            AcceptedSubmissionReceiptV4,
        )
        terminal = self._evidence(
            authority,
            "terminal_receipt",
            TerminalReceiptV4,
        )
        intent = self._evidence(
            authority,
            "materialization_intent",
            MaterializationIntentV4,
        )
        capability = self._capability(authority, accepted, "result_download")
        self._require_credit_delta(
            authority.checkpoint.held_resource_credit,
            ResourceCreditVector(
                output_items=reservation.reserved_credit.output_items,
                output_bytes=reservation.reserved_credit.output_bytes,
                output_pages=reservation.reserved_credit.output_pages,
            ),
            credit_allowance,
        )
        try:
            materialized = self._materialization.materialize_v4(
                checkpoint=authority.checkpoint,
                reservation=reservation,
                preparation_intent=preparation,
                intent=intent,
                accepted_submission=accepted,
                terminal_receipt=terminal,
                provider_capability=capability,
                claim=authority.claim_witness,
                claim_guard=self._claim_guard,
                stage_guard=stage_guard,
                result_lease_seconds=self._inputs.result_lease_seconds(authority),
                allowance=self._inputs.materialization_allowance(authority, intent),
                replay_context=self._replay_context(authority),
            )
        except RemoteProviderUnavailableV4 as exc:
            raise RetryStage(
                "provider result materialization was unavailable",
                retry_after_seconds=self._poll_seconds,
            ) from exc
        except (ExpectedV4AttemptFailure, ParserOutputContractError) as exc:
            return self._fail_attempt(
                work,
                authority,
                outcome="local_failure",
                error=exc,
                error_stage="materialize",
                credit_allowance=credit_allowance,
                stage_guard=stage_guard,
            )
        receipt = materialized.receipt
        successor = advance_remote_parse_checkpoint_v4(
            authority.checkpoint,
            state="local_materialized",
            held_resource_credit=ResourceCreditVector(
                documents=1,
                snapshot_items=1,
                snapshot_bytes=authority.checkpoint.source_byte_count,
                provider_tasks=1,
                provider_result_bytes=intent.artifact_byte_count,
                compressed_bytes=intent.artifact_byte_count,
                output_items=1,
                output_bytes=receipt.output_byte_count,
                output_pages=receipt.source_page_count,
                ack_items=1,
            ),
            local_materialization_receipt_sha256=receipt.sha256,
        )
        self._require_credit_transition(
            authority.checkpoint,
            successor,
            credit_allowance,
        )
        return self._append(
            work,
            authority,
            successor,
            new_evidence=(encode_remote_parse_evidence_v4(receipt),),
            stage_guard=stage_guard,
        )

    def commit(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        authority = self._authority(work, "local_materialized", stage_guard)
        self._require_credit_transition(
            authority.checkpoint,
            authority.checkpoint,
            credit_allowance,
        )
        materialized = self._reopen_materialized(authority, stage_guard)
        request = self._publication_requests.build_or_reopen(
            checkpoint=authority.checkpoint,
            materialized=materialized,
            stage_guard=stage_guard,
        )
        stage_guard.checkpoint()
        self._publisher.execute(
            request=request,
            checkpoint=authority.checkpoint,
            materialized=materialized,
            claim=authority.claim_witness,
            claim_guard=self._claim_guard,
        )
        stage_guard.checkpoint()
        return self._persistence.reload_claim(work)

    def cleanup(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        if work.state == "publish_committed":
            authority = self._authority(work, "publish_committed", stage_guard)
            plan = self._build_cleanup_plan(authority, outcome="success")
            successor = advance_remote_parse_checkpoint_v4(
                authority.checkpoint,
                state="cleanup_pending",
                held_resource_credit=authority.checkpoint.held_resource_credit,
                cleanup_plan_sha256=plan.sha256,
            )
            self._require_credit_transition(
                authority.checkpoint,
                successor,
                credit_allowance,
            )
            return self._append(
                work,
                authority,
                successor,
                new_evidence=(encode_remote_parse_evidence_v4(plan),),
                stage_guard=stage_guard,
            )
        if work.state != "cleanup_pending":
            raise ValueError("cleanup lane received an unsupported V4 state")
        authority = self._authority(work, "cleanup_pending", stage_guard)
        source = authority.checkpoint_history[-2]
        plan = self._evidence(authority, "cleanup_plan", LocalCleanupPlanV4)
        intent = self._optional_evidence(
            authority,
            "materialization_intent",
            MaterializationIntentV4,
        )
        local_receipt = self._optional_evidence(
            authority,
            "local_materialization_receipt",
            LocalMaterializationReceiptV4,
        )
        receipt = self._materialization.cleanup_v4(
            checkpoint=authority.checkpoint,
            source_checkpoint=source,
            reservation=self._reservation(authority),
            intent=intent,
            local_receipt=local_receipt,
            plan=plan,
            claim=authority.claim_witness,
            claim_guard=self._claim_guard,
            stage_guard=stage_guard,
            replay_context=self._replay_context(authority),
        )
        target = (
            "ack_pending"
            if plan.provider_ack_required
            else (
                "superseded"
                if plan.outcome == "superseded"
                else "pre_submission_failed"
            )
        )
        held = (
            self._ack_credit(authority)
            if target == "ack_pending"
            else ResourceCreditVector()
        )
        successor = advance_remote_parse_checkpoint_v4(
            authority.checkpoint,
            state=cast(CheckpointStateV4, target),
            held_resource_credit=held,
            cleanup_receipt_sha256=receipt.sha256,
        )
        self._require_credit_transition(
            authority.checkpoint,
            successor,
            credit_allowance,
        )
        return self._append(
            work,
            authority,
            successor,
            new_evidence=(encode_remote_parse_evidence_v4(receipt),),
            stage_guard=stage_guard,
        )

    def acknowledge(
        self,
        work: CoordinatorWork,
        *,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        authority = self._authority(work, "ack_pending", stage_guard)
        accepted = self._evidence(
            authority,
            "accepted_submission",
            AcceptedSubmissionReceiptV4,
        )
        terminal = self._optional_evidence(
            authority,
            "terminal_receipt",
            TerminalReceiptV4,
        )
        plan = self._evidence(authority, "cleanup_plan", LocalCleanupPlanV4)
        cleanup_receipt = self._evidence(
            authority,
            "cleanup_receipt",
            LocalCleanupReceiptV4,
        )
        command = seal_provider_ack_command_v4(
            ack_pending_checkpoint=authority.checkpoint,
            accepted_submission=accepted,
            terminal_receipt=terminal,
            cleanup_plan=plan,
            cleanup_receipt=cleanup_receipt,
            replay_context=self._replay_context(authority),
        )
        capability = self._capability(
            authority,
            accepted,
            "result_acknowledgement",
        )
        try:
            receipt = self._materialization.acknowledge_v4(
                command=command,
                provider_capability=capability,
                claim=authority.claim_witness,
                claim_guard=self._claim_guard,
                stage_guard=stage_guard,
            )
        except RemoteProviderUnavailableV4 as exc:
            raise RetryStage(
                "provider acknowledgement was unavailable",
                retry_after_seconds=self._poll_seconds,
            ) from exc
        target = {
            "success": "acked",
            "remote_failure": "remote_failed",
            "local_failure": "local_failed",
            "superseded": "superseded",
        }[plan.outcome]
        successor = advance_remote_parse_checkpoint_v4(
            authority.checkpoint,
            state=cast(CheckpointStateV4, target),
            held_resource_credit=ResourceCreditVector(),
            ack_receipt_sha256=receipt.sha256,
        )
        return self._append(
            work,
            authority,
            successor,
            new_evidence=(encode_remote_parse_evidence_v4(receipt),),
            stage_guard=stage_guard,
        )

    def _submit(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        authority = self._authority(work, "reconciling", stage_guard)
        snapshot = self._evidence(
            authority,
            "snapshot_receipt",
            SnapshotReceiptV4,
        )
        intent = self._evidence(
            authority,
            "submission_intent",
            SubmissionIntentV4,
        )
        snapshot_source = self._materialization.submission_snapshot_source_v4(
            checkpoint=authority.checkpoint,
            reservation=self._reservation(authority),
            snapshot_receipt=snapshot,
            submission_intent=intent,
            evidence=authority.evidence,
            resourceful_checkpoint_history=authority.checkpoint_history,
            claim=authority.claim_witness,
            claim_guard=self._claim_guard,
        )
        command = self._inputs.submission_command(
            authority,
            snapshot=snapshot,
            intent=intent,
            snapshot_source=snapshot_source,
            stage_guard=stage_guard,
        )
        self._require_credit_delta(
            authority.checkpoint.held_resource_credit,
            ResourceCreditVector(provider_tasks=1, ack_items=1),
            credit_allowance,
        )
        try:
            accepted = self._remote.reconcile_or_submit(command)
        except (RemoteProviderUnavailableV4, RemoteSubmissionAmbiguousV4) as exc:
            raise RetryStage(
                "provider submission episode was unavailable",
                retry_after_seconds=self._poll_seconds,
            ) from exc
        sealed = self._secret_cipher.seal(
            ProviderSecretPlaintextV4(
                binding=bind_provider_secret_v4(accepted.receipt),
                token=accepted.provider_capability.token_bytes,
            )
        )
        successor = advance_remote_parse_checkpoint_v4(
            authority.checkpoint,
            state="submitted",
            held_resource_credit=ResourceCreditVector(
                documents=1,
                snapshot_items=1,
                snapshot_bytes=authority.checkpoint.source_byte_count,
                remote_waits=1,
                provider_tasks=1,
                ack_items=1,
            ),
            accepted_submission_sha256=accepted.receipt.sha256,
        )
        self._require_credit_transition(
            authority.checkpoint,
            successor,
            credit_allowance,
        )
        return self._append(
            work,
            authority,
            successor,
            new_evidence=(encode_remote_parse_evidence_v4(accepted.receipt),),
            sealed_secret=sealed,
            stage_guard=stage_guard,
        )

    def _poll(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        authority = self._authority(work, "submitted", stage_guard)
        intent = self._evidence(
            authority,
            "submission_intent",
            SubmissionIntentV4,
        )
        accepted = self._evidence(
            authority,
            "accepted_submission",
            AcceptedSubmissionReceiptV4,
        )
        capability = self._capability(
            authority,
            accepted,
            "submitted_task_resume",
        )
        command = self._inputs.poll_command(
            authority,
            intent=intent,
            accepted=accepted,
            capability=capability,
            stage_guard=stage_guard,
        )
        try:
            outcome = self._remote.poll_once(command)
        except RemoteProviderUnavailableV4 as exc:
            raise RetryStage(
                "provider poll episode was unavailable",
                retry_after_seconds=self._poll_seconds,
            ) from exc
        except RemoteProviderProtocolErrorV4 as exc:
            return self._fail_attempt(
                work,
                authority,
                outcome="remote_failure",
                error=exc,
                error_stage="poll",
                credit_allowance=credit_allowance,
                stage_guard=stage_guard,
            )
        if type(outcome) is RemoteProviderWaitingV4:
            runaway = self._remote_runaway_failure(authority, intent)
            if runaway is not None:
                return self._fail_attempt(
                    work,
                    authority,
                    outcome="remote_failure",
                    error=runaway,
                    error_stage="poll_runaway",
                    credit_allowance=credit_allowance,
                    stage_guard=stage_guard,
                )
            raise StageWaiting(
                f"provider task is {outcome.status}",
                retry_after_seconds=self._poll_seconds,
            )
        if type(outcome) is RemoteProviderCompletedV4:
            terminal = outcome.receipt
            successor = advance_remote_parse_checkpoint_v4(
                authority.checkpoint,
                state="remote_terminal",
                held_resource_credit=ResourceCreditVector(
                    documents=1,
                    snapshot_items=1,
                    snapshot_bytes=authority.checkpoint.source_byte_count,
                    provider_tasks=1,
                    provider_result_bytes=terminal.artifact_byte_count,
                    ack_items=1,
                ),
                terminal_receipt_sha256=terminal.sha256,
            )
            self._require_credit_transition(
                authority.checkpoint,
                successor,
                credit_allowance,
            )
            return self._append(
                work,
                authority,
                successor,
                new_evidence=(encode_remote_parse_evidence_v4(terminal),),
                stage_guard=stage_guard,
            )
        if type(outcome) is not RemoteProviderFailedV4:
            raise ValueError("provider poll returned a forged V4 outcome")
        return self._fail_attempt(
            work,
            authority,
            outcome="remote_failure",
            error=ExpectedV4AttemptFailure(
                error_code="provider_terminal_failure",
                message=outcome.provider_error,
                retry_budget_class="provider_terminal",
            ),
            error_stage="poll",
            credit_allowance=credit_allowance,
            stage_guard=stage_guard,
        )

    def _remote_runaway_failure(
        self,
        authority: RemoteParseV4Authority,
        intent: SubmissionIntentV4,
    ) -> ExpectedV4AttemptFailure | None:
        limit = self._inputs.remote_runaway_seconds(authority)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("V4 remote runaway limit is invalid")
        observed = self._wall_clock()
        if (
            isinstance(observed, bool)
            or not isinstance(observed, (int, float))
            or not isfinite(float(observed))
            or float(observed) < intent.submission_epoch_unix
        ):
            raise ValueError("V4 remote runaway clock is invalid")
        if float(observed) < intent.submission_epoch_unix + limit:
            return None
        return ExpectedV4AttemptFailure(
            error_code="remote_parse_runaway",
            message=f"remote parse exceeded the {limit}s runaway guard",
            retry_budget_class="provider_runaway",
        )

    def _fail_attempt(
        self,
        work: CoordinatorWork,
        authority: RemoteParseV4Authority,
        *,
        outcome: CleanupOutcome,
        error: Exception,
        error_stage: str,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        if outcome not in {
            "pre_submission_failure",
            "remote_failure",
            "local_failure",
        }:
            raise ValueError("V4 attempt failure outcome is invalid")
        accepted = self._optional_evidence(
            authority,
            "accepted_submission",
            AcceptedSubmissionReceiptV4,
        )
        terminal = self._optional_evidence(
            authority,
            "terminal_receipt",
            TerminalReceiptV4,
        )
        intent = self._optional_evidence(
            authority,
            "materialization_intent",
            MaterializationIntentV4,
        )
        local = self._optional_evidence(
            authority,
            "local_materialization_receipt",
            LocalMaterializationReceiptV4,
        )
        if isinstance(error, ExpectedV4AttemptFailure):
            error_code = error.error_code
            retryable = error.retryable
            retry_budget_class = error.retry_budget_class
            message = str(error)
        elif isinstance(error, ParserOutputContractError):
            error_code = "parser_output_contract_rejected"
            retryable = False
            retry_budget_class = "provider_artifact_contract"
            message = "provider artifact failed the local output contract"
        elif isinstance(error, RemoteProviderProtocolErrorV4):
            error_code = "provider_protocol_rejected"
            retryable = False
            retry_budget_class = "provider_protocol"
            message = "provider response failed the remote protocol contract"
        else:
            raise ValueError("untyped V4 attempt failure cannot be persisted")
        failure = FailureReceiptV4(
            attempt_id=authority.attempt_id,
            fence_identity=authority.fence_identity,
            outcome=cast(FailureOutcomeV4, outcome),
            source_state=authority.state,
            source_lifecycle_version=authority.lifecycle_version,
            source_checkpoint_sha256=authority.checkpoint_sha256,
            submission_was_attempted=(outcome != "pre_submission_failure"),
            submission_absence_proof=None,
            accepted_submission_receipt_sha256=(
                None if accepted is None else accepted.sha256
            ),
            terminal_receipt_sha256=(None if terminal is None else terminal.sha256),
            materialization_intent_sha256=(
                None if intent is None else intent.sha256
            ),
            local_materialization_receipt_sha256=(
                None if local is None else local.sha256
            ),
            error_code=error_code,
            error_stage=error_stage,
            error_class=type(error).__name__,
            retryable=retryable,
            retry_budget_class=retry_budget_class,
            message=message,
        )
        plan = self._build_cleanup_plan(
            authority,
            outcome=outcome,
            failure=failure,
        )
        successor = advance_remote_parse_checkpoint_v4(
            authority.checkpoint,
            state="cleanup_pending",
            held_resource_credit=authority.checkpoint.held_resource_credit,
            failure_receipt_sha256=failure.sha256,
            cleanup_plan_sha256=plan.sha256,
        )
        self._require_credit_transition(
            authority.checkpoint,
            successor,
            credit_allowance,
        )
        return self._append(
            work,
            authority,
            successor,
            new_evidence=(
                encode_remote_parse_evidence_v4(failure),
                encode_remote_parse_evidence_v4(plan),
            ),
            stage_guard=stage_guard,
        )

    def _reopen_materialized(
        self,
        authority: RemoteParseV4Authority,
        stage_guard: StageLeaseGuard,
    ) -> MaterializedProviderDocumentV4:
        return self._materialization.reopen_materialized_v4(
            checkpoint=authority.checkpoint,
            reservation=self._reservation(authority),
            intent=self._evidence(
                authority,
                "materialization_intent",
                MaterializationIntentV4,
            ),
            local_receipt=self._evidence(
                authority,
                "local_materialization_receipt",
                LocalMaterializationReceiptV4,
            ),
            claim=authority.claim_witness,
            claim_guard=self._claim_guard,
            stage_guard=stage_guard,
            replay_context=self._replay_context(authority),
        )

    def _build_cleanup_plan(
        self,
        authority: RemoteParseV4Authority,
        *,
        outcome: CleanupOutcome,
        failure: FailureReceiptV4 | None = None,
    ) -> LocalCleanupPlanV4:
        reservation = self._reservation(authority)
        intent = self._optional_evidence(
            authority,
            "materialization_intent",
            MaterializationIntentV4,
        )
        local = self._optional_evidence(
            authority,
            "local_materialization_receipt",
            LocalMaterializationReceiptV4,
        )
        accepted = self._optional_evidence(
            authority,
            "accepted_submission",
            AcceptedSubmissionReceiptV4,
        )
        resources = [
            CleanupResourceEntryV4(
                kind="snapshot",
                relpath=reservation.snapshot_relpath,
                ownership_basis_sha256=reservation.sha256,
                expected_sha256=reservation.source_pdf_sha256,
                expected_byte_count=reservation.source_byte_count,
                action="delete",
            )
        ]
        if authority.checkpoint.snapshot_receipt_sha256 is None:
            resources.extend(
                (
                    CleanupResourceEntryV4(
                        kind="snapshot_part",
                        relpath=reservation.snapshot_part_relpath,
                        ownership_basis_sha256=reservation.sha256,
                        expected_sha256=None,
                        expected_byte_count=None,
                        action="delete",
                    ),
                    CleanupResourceEntryV4(
                        kind="snapshot_part_owner",
                        relpath=reservation.snapshot_part_owner_relpath,
                        ownership_basis_sha256=reservation.sha256,
                        expected_sha256=None,
                        expected_byte_count=None,
                        action="delete",
                    ),
                )
            )
        if intent is not None:
            resources.append(
                CleanupResourceEntryV4(
                    kind="spool",
                    relpath=intent.spool_relpath,
                    ownership_basis_sha256=intent.sha256,
                    expected_sha256=intent.artifact_sha256,
                    expected_byte_count=intent.artifact_byte_count,
                    action="delete",
                )
            )
            if local is None:
                for kind, relpath in (
                    ("spool_part", intent.spool_part_relpath),
                    ("spool_part_owner", intent.spool_part_owner_relpath),
                    ("staging", intent.staging_relpath),
                    ("staging_marker", intent.staging_marker_relpath),
                ):
                    resources.append(
                        CleanupResourceEntryV4(
                            kind=cast(LocalResourceKind, kind),
                            relpath=relpath,
                            ownership_basis_sha256=intent.sha256,
                            expected_sha256=None,
                            expected_byte_count=None,
                            action="delete",
                        )
                    )
            else:
                resources.append(
                    CleanupResourceEntryV4(
                        kind="output",
                        relpath=intent.output_relpath,
                        ownership_basis_sha256=local.sha256,
                        expected_sha256=local.output_files_sha256,
                        expected_byte_count=local.output_byte_count,
                        action="transfer" if outcome == "success" else "delete",
                        target_owner_identity=(
                            authority.processing_run_id
                            if outcome == "success"
                            else None
                        ),
                        target_relpath=(
                            intent.provider_envelope_context.parser_artifact_root_relpath
                            if outcome == "success"
                            else None
                        ),
                    )
                )
        return build_local_cleanup_plan_v4(
            reservation=reservation,
            source_checkpoint=authority.checkpoint,
            outcome=outcome,
            resources=tuple(resources),
            materialization_intent=intent,
            local_materialization_receipt=local,
            remote_task_identity=(
                None if accepted is None else accepted.remote_task_identity
            ),
            failure_receipt_sha256=(None if failure is None else failure.sha256),
        )

    def _capability(
        self,
        authority: RemoteParseV4Authority,
        accepted: AcceptedSubmissionReceiptV4,
        purpose: str,
    ) -> PrivateProviderCapabilityV4:
        if not authority.secret_history:
            raise ValueError("durable V4 accepted task lacks its private capability")
        plaintext = self._secret_cipher.open(authority.secret_history[-1])
        if plaintext.binding != bind_provider_secret_v4(accepted):
            raise ValueError("opened V4 provider capability binding drifted")
        return PrivateProviderCapabilityV4(
            attempt_id=accepted.attempt_id,
            remote_task_identity=accepted.remote_task_identity,
            provider_protocol_version=accepted.provider_protocol_version,
            secret_kind=accepted.secret_kind,
            secret_version=accepted.secret_version,
            capability_purpose=purpose,
            token_bytes=plaintext.token,
            token_sha256=accepted.token_sha256,
            token_byte_count=accepted.token_byte_count,
        )

    def _authority(
        self,
        work: CoordinatorWork,
        expected_state: str,
        stage_guard: StageLeaseGuard,
    ) -> RemoteParseV4Authority:
        stage_guard.checkpoint()
        authority = self._persistence.load_owned_authority(work)
        stage_guard.checkpoint()
        if authority.state != expected_state:
            raise ValueError("durable V4 stage state changed before execution")
        return authority

    def _append(
        self,
        work: CoordinatorWork,
        authority: RemoteParseV4Authority,
        successor: RemoteParseCheckpointV4,
        *,
        new_evidence: tuple[EncodedRemoteParseEvidenceV4, ...],
        stage_guard: StageLeaseGuard,
        sealed_secret: SealedProviderSecretV4 | None = None,
    ) -> CoordinatorWork:
        stage_guard.checkpoint()
        append = V4SuccessorAppend(
            claim=authority.claim_witness,
            successor=successor,
            new_evidence=new_evidence,
            sealed_secret=sealed_secret,
        )
        result = self._persistence.append_successor(work, append)
        stage_guard.checkpoint()
        return result

    @staticmethod
    def _reservation(
        authority: RemoteParseV4Authority,
    ) -> ResourceReservationV4:
        if authority.reservation is None:
            raise ValueError("resourceful V4 stage lacks its reservation")
        return authority.reservation

    @staticmethod
    def _evidence(
        authority: RemoteParseV4Authority,
        kind: str,
        expected_type: type[_EvidenceT],
    ) -> _EvidenceT:
        value = DurableStagedCoordinatorBackendV4._optional_evidence(
            authority,
            kind,
            expected_type,
        )
        if value is None:
            raise ValueError(f"durable V4 stage lacks {kind} evidence")
        return value

    @staticmethod
    def _optional_evidence(
        authority: RemoteParseV4Authority,
        kind: str,
        expected_type: type[_EvidenceT],
    ) -> _EvidenceT | None:
        matches = tuple(item.value for item in authority.evidence if item.kind == kind)
        if not matches:
            return None
        if len(matches) != 1 or type(matches[0]) is not expected_type:
            raise ValueError(f"durable V4 {kind} evidence drifted")
        return cast(_EvidenceT, matches[0])

    @staticmethod
    def _replay_context(
        authority: RemoteParseV4Authority,
        *,
        materialized: MaterializedProviderDocumentV4 | None = None,
    ) -> V4EvidenceReplayContext:
        reservation = authority.reservation
        if reservation is None:
            raise ValueError("resourceful V4 replay lacks its reservation")
        history = authority.checkpoint_history
        manifest = None if materialized is None else materialized.manifest
        provider_envelope = (
            None if materialized is None else materialized.provider_envelope
        )
        if authority.state == "cleanup_pending":
            return V4EvidenceReplayContext(
                evidence=authority.evidence,
                reservation=reservation,
                resourceful_checkpoint_history=history[:-1],
                cleanup_source_checkpoint=history[-2],
                local_materialization_manifest=manifest,
                provider_envelope=provider_envelope,
            )
        if authority.state == "ack_pending":
            return V4EvidenceReplayContext(
                evidence=authority.evidence,
                reservation=reservation,
                resourceful_checkpoint_history=history[:-2],
                cleanup_source_checkpoint=history[-3],
                cleanup_pending_checkpoint=history[-2],
                ack_pending_checkpoint=history[-1],
                local_materialization_manifest=manifest,
                provider_envelope=provider_envelope,
            )
        return V4EvidenceReplayContext(
            evidence=authority.evidence,
            reservation=reservation,
            resourceful_checkpoint_history=history,
            local_materialization_manifest=manifest,
            provider_envelope=provider_envelope,
        )

    @staticmethod
    def _ack_credit(authority: RemoteParseV4Authority) -> ResourceCreditVector:
        terminal = DurableStagedCoordinatorBackendV4._optional_evidence(
            authority,
            "terminal_receipt",
            TerminalReceiptV4,
        )
        return ResourceCreditVector(
            documents=1,
            provider_tasks=1,
            provider_result_bytes=(
                0 if terminal is None else terminal.artifact_byte_count
            ),
            ack_items=1,
        )

    @staticmethod
    def _require_credit_transition(
        before: RemoteParseCheckpointV4,
        after: RemoteParseCheckpointV4,
        allowance: ResourceCreditVector,
    ) -> None:
        DurableStagedCoordinatorBackendV4._require_credit_delta(
            before.held_resource_credit,
            after.held_resource_credit,
            allowance,
        )

    @staticmethod
    def _require_credit_delta(
        before: ResourceCreditVector,
        added: ResourceCreditVector,
        allowance: ResourceCreditVector,
    ) -> None:
        required = ResourceCreditVector(
            **{
                item.name: max(
                    0,
                    getattr(added, item.name) - getattr(before, item.name),
                )
                for item in fields(ResourceCreditVector)
            }
        )
        if not required.fits(allowance):
            raise RetryStage(
                "stage credit grant exhausted",
                retry_after_seconds=0.001,
            )


__all__ = [
    "DurableStagedCoordinatorBackendV4",
    "V4StageInputResolver",
]
