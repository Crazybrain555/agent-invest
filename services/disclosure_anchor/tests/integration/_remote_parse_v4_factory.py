"""Canonical V4 rows for scratch-database authority tests.

The helpers in this module deliberately build lifecycle bytes through the
production contracts.  SQL is used only to project those closed objects into
the append-only 0057 tables; it is not a second implementation of canonical
JSON or lifecycle hashing.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json

from sqlalchemy import text
from sqlalchemy.engine import Connection

from disclosure_anchor.adapters.security.provider_secret_cipher import (
    AesGcmProviderSecretCipher,
)
from disclosure_anchor.adapters.security.provider_secret_keyring import (
    StaticProviderSecretKeyring,
)
from disclosure_anchor.application.contracts.provider_secret_envelope_v4 import (
    ProviderSecretPlaintextV4,
    SealedProviderSecretV4,
    bind_provider_secret_v4,
)
from disclosure_anchor.application.contracts.local_materialization_manifest_v4 import (
    LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
    LocalMaterializationObservationsV4,
    LocalMaterializationPayloadFileV4,
    seal_local_materialization_manifest_v4,
)
from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity
from disclosure_anchor.application.contracts.provider_document_envelope import (
    PROVIDER_DOCUMENT_FILENAME,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    AcceptedSubmissionReceiptV4,
    EncodedRemoteParseEvidenceV4,
    EvidenceValueV4,
    FailureReceiptV4,
    PreparationIntentV4,
    SnapshotReceiptV4,
    SubmissionIntentV4,
    TerminalReceiptV4,
    build_preparation_intent_v4,
    encode_remote_parse_evidence_v4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    CleanupResourceEntryV4,
    LocalCleanupReceiptV4,
    LocalCleanupResourceResultV4,
    LocalCleanupPlanV4,
    LocalMaterializationReceiptV4,
    LocalOutputFileV4,
    MaterializationIntentV4,
    ProviderAckReceiptV4,
    ProviderEnvelopeContextV4,
    RemoteParseCheckpointV4,
    ResourceReservationV4,
    advance_remote_parse_checkpoint_v4,
    build_initial_remote_parse_checkpoint_v4,
    build_local_cleanup_plan_v4,
    build_local_cleanup_receipt_v4,
    build_local_materialization_receipt_v4,
    build_materialization_intent_v4,
    build_resource_free_remote_parse_checkpoint_v4,
    build_resource_reservation_v4,
    provider_ack_request_v4_bytes,
    provider_ack_request_v4_identity,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    EncodedResourceReservationInput,
    PerAttemptResourceAllowance,
    ResourceCreditVector,
    ResourceReservationInput,
    encode_resource_reservation_input,
)
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
    AtomicPublicationWinnerV4,
    DurablePublishBaseCommitReference,
    UnitAssetWinnerV4,
    final_unit_rows_sha256_v4,
    lineage_rows_sha256_v4,
    seal_published_outbox_commit_reference_v4,
    seal_published_outbox_event_v4,
)
from disclosure_anchor.domain import ids


HELD_CREDIT_NAMES = tuple(item.name for item in fields(ResourceCreditVector))
EVIDENCE_FIELD_NAMES = (
    "preparation_intent_sha256",
    "snapshot_receipt_sha256",
    "submission_intent_sha256",
    "accepted_submission_sha256",
    "terminal_receipt_sha256",
    "materialization_intent_sha256",
    "local_materialization_receipt_sha256",
    "failure_receipt_sha256",
    "supersession_receipt_sha256",
    "cleanup_plan_sha256",
    "cleanup_receipt_sha256",
    "ack_receipt_sha256",
)


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class V4AuthorityFixture:
    document_id: str
    processing_run_id: str
    attempt_id: str
    fence_identity: str
    client_submit_key: str
    source_pdf_sha256: str
    parser_target_identity_json: str
    parser_target_sha256: str
    request_sha256: str
    runtime_epoch_sha256: str
    process_profile_sha256: str
    credit_policy_sha256: str
    reservation_input: EncodedResourceReservationInput
    reservation: ResourceReservationV4
    preparation: PreparationIntentV4
    snapshot: SnapshotReceiptV4
    submission: SubmissionIntentV4
    accepted: AcceptedSubmissionReceiptV4
    preparation_failure: FailureReceiptV4
    remote_failure: FailureReceiptV4
    cleanup_plan: LocalCleanupPlanV4
    cleanup_receipt: LocalCleanupReceiptV4
    ack_receipt: ProviderAckReceiptV4
    prepared: RemoteParseCheckpointV4
    reconciling: RemoteParseCheckpointV4
    submitted: RemoteParseCheckpointV4
    cleanup_pending: RemoteParseCheckpointV4
    ack_pending: RemoteParseCheckpointV4
    remote_failed: RemoteParseCheckpointV4
    preparation_failed: RemoteParseCheckpointV4
    sealed_secret: SealedProviderSecretV4
    terminal: TerminalReceiptV4
    materialization_intent: MaterializationIntentV4
    local_materialization_receipt: LocalMaterializationReceiptV4
    remote_terminal: RemoteParseCheckpointV4
    materializing: RemoteParseCheckpointV4
    local_materialized: RemoteParseCheckpointV4
    publication_winner: AtomicPublicationWinnerV4
    publish_committed: RemoteParseCheckpointV4
    success_cleanup_plan: LocalCleanupPlanV4
    success_cleanup_receipt: LocalCleanupReceiptV4
    success_ack_receipt: ProviderAckReceiptV4
    success_cleanup_pending: RemoteParseCheckpointV4
    success_ack_pending: RemoteParseCheckpointV4
    acked: RemoteParseCheckpointV4

    @property
    def prepared_evidence(self) -> tuple[EvidenceValueV4, ...]:
        return (self.preparation, self.snapshot)

    @property
    def submitted_evidence(self) -> tuple[EvidenceValueV4, ...]:
        return (
            self.preparation,
            self.snapshot,
            self.submission,
            self.accepted,
        )

    @property
    def final_evidence(self) -> tuple[EvidenceValueV4, ...]:
        return (
            *self.submitted_evidence,
            self.remote_failure,
            self.cleanup_plan,
            self.cleanup_receipt,
            self.ack_receipt,
        )

    @property
    def submitted_checkpoints(self) -> tuple[RemoteParseCheckpointV4, ...]:
        return (self.prepared, self.reconciling, self.submitted)

    @property
    def final_checkpoints(self) -> tuple[RemoteParseCheckpointV4, ...]:
        return (
            *self.submitted_checkpoints,
            self.cleanup_pending,
            self.ack_pending,
            self.remote_failed,
        )

    @property
    def local_materialized_evidence(self) -> tuple[EvidenceValueV4, ...]:
        return (
            *self.submitted_evidence,
            self.terminal,
            self.materialization_intent,
            self.local_materialization_receipt,
        )

    @property
    def local_materialized_checkpoints(
        self,
    ) -> tuple[RemoteParseCheckpointV4, ...]:
        return (
            *self.submitted_checkpoints,
            self.remote_terminal,
            self.materializing,
            self.local_materialized,
        )

    @property
    def success_evidence(self) -> tuple[EvidenceValueV4, ...]:
        return (
            *self.local_materialized_evidence,
            self.success_cleanup_plan,
            self.success_cleanup_receipt,
            self.success_ack_receipt,
        )

    @property
    def success_checkpoints(self) -> tuple[RemoteParseCheckpointV4, ...]:
        return (
            *self.local_materialized_checkpoints,
            self.publish_committed,
            self.success_cleanup_pending,
            self.success_ack_pending,
            self.acked,
        )


def build_v4_authority_fixture() -> V4AuthorityFixture:
    document_id = ids.new_document_id()
    processing_run_id = ids.new_processing_run_id()
    attempt_id = "rpa_" + ids.new_ulid()
    fence_identity = "fence-" + ids.new_ulid()
    client_submit_key = "submit-" + ids.new_ulid()
    source_pdf_sha256 = sha256_bytes((attempt_id + ":source").encode())
    parser_target_identity = ParserTargetIdentity(
        name="MinerU",
        package_version="3.4.4",
        backend="hybrid-http-client",
        method="auto",
        language="ch",
        formula=True,
        table=True,
        effort="medium",
        runtime_bundle_identity_sha256=sha256_bytes(
            (attempt_id + ":runtime-bundle").encode()
        ),
    )
    parser_target_identity_json = json.dumps(
        parser_target_identity.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
    )
    parser_target_sha256 = sha256_bytes(parser_target_identity_json.encode())
    request_sha256 = sha256_bytes((attempt_id + ":request").encode())
    runtime_epoch_sha256 = sha256_bytes((attempt_id + ":epoch").encode())
    process_profile_sha256 = sha256_bytes((attempt_id + ":profile").encode())
    credit_policy_sha256 = sha256_bytes((attempt_id + ":credit").encode())

    reservation_credit = ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=100,
        remote_waits=1,
        provider_tasks=1,
        provider_result_bytes=20,
        materialization_items=1,
        compressed_bytes=20,
        decoded_bytes=30,
        temp_disk_bytes=8192,
        output_items=1,
        output_bytes=4096,
        output_pages=2,
        ack_items=1,
    )
    reservation_input = encode_resource_reservation_input(
        ResourceReservationInput(
            source_pdf_sha256=source_pdf_sha256,
            source_byte_count=100,
            source_page_count=2,
            process_profile_sha256=process_profile_sha256,
            credit_policy_sha256=credit_policy_sha256,
            bucket="regular",
            reservation=reservation_credit,
        )
    )
    reservation = build_resource_reservation_v4(
        attempt_id=attempt_id,
        attempt_generation=1,
        fence_identity=fence_identity,
        document_id=document_id,
        processing_run_id=processing_run_id,
        source_pdf_sha256=source_pdf_sha256,
        source_byte_count=100,
        source_page_count=2,
        prepared_submission_identity_sha256=sha256_bytes(
            (attempt_id + ":prepared-submission").encode()
        ),
        request_sha256=request_sha256,
        runtime_epoch_sha256=runtime_epoch_sha256,
        process_profile_sha256=process_profile_sha256,
        credit_policy_sha256=credit_policy_sha256,
        reservation_bucket="regular",
        reservation_input_sha256=reservation_input.sha256,
        reserved_credit=reservation_credit,
    )
    preparation = build_preparation_intent_v4(
        reservation=reservation,
        parser_target_sha256=parser_target_sha256,
    )
    snapshot = SnapshotReceiptV4(
        attempt_id=attempt_id,
        fence_identity=fence_identity,
        preparation_intent_sha256=preparation.sha256,
        snapshot_relpath=reservation.snapshot_relpath,
        snapshot_sha256=source_pdf_sha256,
        snapshot_byte_count=100,
        part_path_absent=True,
        part_owner_path_absent=True,
        file_fsync_completed=True,
        parent_fsync_completed=True,
    )
    prepared_credit = ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=100,
    )
    prepared = build_initial_remote_parse_checkpoint_v4(
        reservation=reservation,
        preparation_intent_sha256=preparation.sha256,
        snapshot_receipt_sha256=snapshot.sha256,
        held_resource_credit=prepared_credit,
    )
    submission = SubmissionIntentV4(
        attempt_id=attempt_id,
        fence_identity=fence_identity,
        snapshot_receipt_sha256=snapshot.sha256,
        source_pdf_sha256=source_pdf_sha256,
        parser_target_sha256=parser_target_sha256,
        request_sha256=request_sha256,
        runtime_epoch_sha256=runtime_epoch_sha256,
        client_submit_key=client_submit_key,
        submission_epoch_unix=1,
        provider_protocol_version="mineru-task-protocol.v2",
    )
    reconciling = advance_remote_parse_checkpoint_v4(
        prepared,
        state="reconciling",
        held_resource_credit=replace(prepared_credit, remote_waits=1),
        submission_intent_sha256=submission.sha256,
    )
    token = ("provider-token:" + attempt_id).encode()
    accepted = AcceptedSubmissionReceiptV4(
        attempt_id=attempt_id,
        fence_identity=fence_identity,
        submission_intent_sha256=submission.sha256,
        remote_task_identity="task-" + ids.new_ulid(),
        status_url="https://provider.invalid/tasks/1",
        result_url="https://provider.invalid/tasks/1/result",
        secret_kind="mineru-task-token.v1",
        secret_version=1,
        token_sha256=sha256_bytes(token),
        token_byte_count=len(token),
        provider_protocol_version=submission.provider_protocol_version,
    )
    submitted_credit = ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=100,
        remote_waits=1,
        provider_tasks=1,
        ack_items=1,
    )
    submitted = advance_remote_parse_checkpoint_v4(
        reconciling,
        state="submitted",
        held_resource_credit=submitted_credit,
        accepted_submission_sha256=accepted.sha256,
    )
    artifact_sha256 = sha256_bytes((attempt_id + ":artifact").encode())
    terminal = TerminalReceiptV4(
        attempt_id=attempt_id,
        fence_identity=fence_identity,
        accepted_submission_receipt_sha256=accepted.sha256,
        remote_task_identity=accepted.remote_task_identity,
        result_owner_identity="result-owner-" + ids.new_ulid(),
        artifact_sha256=artifact_sha256,
        artifact_byte_count=20,
        provider_protocol_version=submission.provider_protocol_version,
    )
    remote_terminal_credit = ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=100,
        provider_tasks=1,
        provider_result_bytes=20,
        ack_items=1,
    )
    remote_terminal = advance_remote_parse_checkpoint_v4(
        submitted,
        state="remote_terminal",
        held_resource_credit=remote_terminal_credit,
        terminal_receipt_sha256=terminal.sha256,
    )
    provider_document_id = "1225087169"
    source_digest = source_pdf_sha256.removeprefix("sha256:")
    provider_context = ProviderEnvelopeContextV4(
        document_id=document_id,
        processing_run_id=processing_run_id,
        provider="cninfo",
        provider_document_id=provider_document_id,
        source_pdf_relpath=(
            "raw_documents/cninfo/000001/2026/"
            f"{provider_document_id}/sha256_{source_digest}.pdf"
        ),
        source_pdf_sha256=source_pdf_sha256,
        source_page_count=2,
        parser_artifact_root_relpath=(
            "parser_artifacts/cninfo/000001/"
            f"{provider_document_id}/{processing_run_id}/"
            f"sha256_{source_digest}/hybrid_auto"
        ),
        parser_target_identity=parser_target_identity,
    )
    allowance = PerAttemptResourceAllowance(
        reservation_input_sha256=reservation_input.sha256,
        reservation_input=reservation_input,
        limits=reservation_credit,
    )
    materialization_intent = build_materialization_intent_v4(
        reservation=reservation,
        source_checkpoint=remote_terminal,
        terminal_receipt_sha256=terminal.sha256,
        remote_task_identity=accepted.remote_task_identity,
        artifact_owner_identity=terminal.result_owner_identity,
        artifact_sha256=terminal.artifact_sha256,
        artifact_byte_count=terminal.artifact_byte_count,
        provider_envelope_context=provider_context,
        allowance_sha256=allowance.sha256,
        provider_capability_kind=accepted.secret_kind,
        provider_capability_sha256=accepted.token_sha256,
        provider_capability_byte_count=accepted.token_byte_count,
        output_dir_name="output-" + ids.new_ulid(),
        provider_envelope_relpath=PROVIDER_DOCUMENT_FILENAME,
        output_manifest_relpath=LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
        member_count_limit=10,
        uncompressed_byte_limit=100,
    )
    materializing = advance_remote_parse_checkpoint_v4(
        remote_terminal,
        state="materializing",
        held_resource_credit=materialization_intent.held_resource_credit,
        materialization_intent_sha256=materialization_intent.sha256,
    )
    provider_envelope_bytes = b"provider"
    parser_artifact_bytes = b"parser-output"
    payload_files = tuple(
        sorted(
            (
                LocalMaterializationPayloadFileV4(
                    role="provider_envelope",
                    relpath=PROVIDER_DOCUMENT_FILENAME,
                    sha256=sha256_bytes(provider_envelope_bytes),
                    byte_count=len(provider_envelope_bytes),
                ),
                LocalMaterializationPayloadFileV4(
                    role="parser_artifact",
                    relpath="result/content.md",
                    sha256=sha256_bytes(parser_artifact_bytes),
                    byte_count=len(parser_artifact_bytes),
                ),
            ),
            key=lambda item: item.relpath,
        )
    )
    payload_byte_count = sum(item.byte_count for item in payload_files)
    materialization_manifest = seal_local_materialization_manifest_v4(
        attempt_id=attempt_id,
        fence_identity=fence_identity,
        document_id=document_id,
        processing_run_id=processing_run_id,
        materialization_intent_sha256=materialization_intent.sha256,
        terminal_receipt_sha256=terminal.sha256,
        remote_task_identity=accepted.remote_task_identity,
        artifact_owner_identity=terminal.result_owner_identity,
        artifact_sha256=artifact_sha256,
        artifact_byte_count=20,
        source_pdf_sha256=source_pdf_sha256,
        source_page_count=2,
        parser_target_sha256=parser_target_sha256,
        spool_relpath=materialization_intent.spool_relpath,
        output_relpath=materialization_intent.output_relpath,
        provider_envelope_relpath=PROVIDER_DOCUMENT_FILENAME,
        provider_envelope_sha256=sha256_bytes(provider_envelope_bytes),
        provider_envelope_byte_count=len(provider_envelope_bytes),
        observations=LocalMaterializationObservationsV4(
            member_count=2,
            uncompressed_byte_count=30,
            decoded_byte_count=20,
            temporary_disk_peak_byte_count=50,
            output_file_count=len(payload_files),
            output_byte_count=payload_byte_count,
        ),
        payload_files=payload_files,
    )
    output_files = tuple(
        sorted(
            (
                LocalOutputFileV4(
                    relpath=LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
                    sha256=materialization_manifest.sha256,
                    byte_count=len(materialization_manifest.canonical_bytes),
                ),
                *(
                    LocalOutputFileV4(
                        relpath=item.relpath,
                        sha256=item.sha256,
                        byte_count=item.byte_count,
                    )
                    for item in payload_files
                ),
            ),
            key=lambda item: item.relpath,
        )
    )
    local_materialization_receipt = build_local_materialization_receipt_v4(
        intent=materialization_intent,
        manifest=materialization_manifest,
        source_page_count=2,
        output_files=output_files,
        provider_envelope_relpath=PROVIDER_DOCUMENT_FILENAME,
        output_manifest_relpath=LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
        member_count=2,
        uncompressed_byte_count=30,
        decoded_byte_count=20,
        temporary_disk_peak_byte_count=50,
        file_fsync_completed=True,
        output_parent_fsync_completed=True,
        marker_removed=True,
        spool_part_absent=True,
        spool_part_owner_absent=True,
        staging_absent=True,
    )
    local_credit = ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=100,
        provider_tasks=1,
        provider_result_bytes=20,
        compressed_bytes=20,
        output_items=1,
        output_bytes=local_materialization_receipt.output_byte_count,
        output_pages=2,
        ack_items=1,
    )
    local_materialized = advance_remote_parse_checkpoint_v4(
        materializing,
        state="local_materialized",
        held_resource_credit=local_credit,
        local_materialization_receipt_sha256=(
            local_materialization_receipt.sha256
        ),
    )
    publication_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    unit_asset = UnitAssetWinnerV4(
        unit_index=1,
        asset_id=str(ids.new_asset_id()),
        routed_draft_sha256=sha256_bytes((attempt_id + ":draft").encode()),
        final_unit_row_sha256=sha256_bytes(
            (attempt_id + ":final-unit").encode()
        ),
        lineage_row_sha256=sha256_bytes((attempt_id + ":lineage").encode()),
    )
    outbox_event = seal_published_outbox_event_v4(
        event_id=str(ids.new_outbox_event_id()),
        event_sequence=1,
        event_kind="processing_run_published",
        change_kind="materialized",
        subject_kind="processing_run",
        subject_ref=processing_run_id,
        document_id=document_id,
        processing_run_id=processing_run_id,
        asset_id=None,
        canonical_payload_json="{}",
        occurred_at=publication_at,
    )
    outbox_commit = seal_published_outbox_commit_reference_v4(
        events=(outbox_event,)
    )
    publication_winner = AtomicPublicationWinnerV4(
        attempt_id=attempt_id,
        fence_identity=fence_identity,
        document_id=document_id,
        processing_run_id=processing_run_id,
        publish_attempt_generation=1,
        local_checkpoint_sha256=local_materialized.sha256,
        lifecycle_version_before=local_materialized.lifecycle_version,
        lifecycle_version_after=local_materialized.lifecycle_version + 1,
        request_sha256=request_sha256,
        upstream_evidence_sha256=sha256_bytes(
            (attempt_id + ":upstream-evidence").encode()
        ),
        final_units_sha256=final_unit_rows_sha256_v4((unit_asset,)),
        lineage_sha256=lineage_rows_sha256_v4((unit_asset,)),
        processing_run_row_sha256=sha256_bytes(
            (processing_run_id + ":published-row").encode()
        ),
        previous_active_run_id=None,
        inserted_count=1,
        updated_count=0,
        deleted_count=0,
        outbox_commit=outbox_commit,
        durable_base_commit=DurablePublishBaseCommitReference(
            document_id=document_id,
            processing_run_id=processing_run_id,
            publish_attempt_generation=1,
            source_identity_sha256=source_pdf_sha256,
            source_page_count=2,
            publish_precommit_at=publication_at,
            durable_base_sha256=sha256_bytes(
                (attempt_id + ":durable-base").encode()
            ),
        ),
        unit_assets=(unit_asset,),
        publish_precommit_at=publication_at,
    )
    publish_committed = advance_remote_parse_checkpoint_v4(
        local_materialized,
        state="publish_committed",
        held_resource_credit=local_credit,
        publication_winner_sha256=publication_winner.sha256,
    )
    success_cleanup_resources = (
        CleanupResourceEntryV4(
            kind="snapshot",
            relpath=reservation.snapshot_relpath,
            ownership_basis_sha256=reservation.sha256,
            expected_sha256=source_pdf_sha256,
            expected_byte_count=100,
            action="delete",
        ),
        CleanupResourceEntryV4(
            kind="spool",
            relpath=materialization_intent.spool_relpath,
            ownership_basis_sha256=materialization_intent.sha256,
            expected_sha256=terminal.artifact_sha256,
            expected_byte_count=terminal.artifact_byte_count,
            action="delete",
        ),
        CleanupResourceEntryV4(
            kind="output",
            relpath=materialization_intent.output_relpath,
            ownership_basis_sha256=local_materialization_receipt.sha256,
            expected_sha256=local_materialization_receipt.output_files_sha256,
            expected_byte_count=local_materialization_receipt.output_byte_count,
            action="transfer",
            target_owner_identity=processing_run_id,
            target_relpath=provider_context.parser_artifact_root_relpath,
        ),
    )
    success_cleanup_plan = build_local_cleanup_plan_v4(
        reservation=reservation,
        source_checkpoint=publish_committed,
        outcome="success",
        remote_task_identity=accepted.remote_task_identity,
        materialization_intent=materialization_intent,
        local_materialization_receipt=local_materialization_receipt,
        resources=success_cleanup_resources,
    )
    success_cleanup_pending = advance_remote_parse_checkpoint_v4(
        publish_committed,
        state="cleanup_pending",
        held_resource_credit=publish_committed.held_resource_credit,
        cleanup_plan_sha256=success_cleanup_plan.sha256,
    )
    success_cleanup_receipt = build_local_cleanup_receipt_v4(
        plan=success_cleanup_plan,
        cleanup_pending_checkpoint=success_cleanup_pending,
        results=(
            LocalCleanupResourceResultV4(
                kind="snapshot",
                relpath=reservation.snapshot_relpath,
                disposition="absent",
            ),
            LocalCleanupResourceResultV4(
                kind="spool",
                relpath=materialization_intent.spool_relpath,
                disposition="absent",
            ),
            LocalCleanupResourceResultV4(
                kind="output",
                relpath=materialization_intent.output_relpath,
                disposition="transferred",
                target_owner_identity=processing_run_id,
                target_relpath=provider_context.parser_artifact_root_relpath,
            ),
        ),
    )
    success_ack_pending = advance_remote_parse_checkpoint_v4(
        success_cleanup_pending,
        state="ack_pending",
        held_resource_credit=ResourceCreditVector(
            documents=1,
            provider_tasks=1,
            provider_result_bytes=terminal.artifact_byte_count,
            ack_items=1,
        ),
        cleanup_receipt_sha256=success_cleanup_receipt.sha256,
    )
    success_ack_request_bytes = provider_ack_request_v4_bytes(
        accepted_submission_sha256=accepted.sha256,
        ack_pending_checkpoint_sha256=success_ack_pending.sha256,
        attempt_id=attempt_id,
        cleanup_plan_sha256=success_cleanup_plan.sha256,
        cleanup_receipt_sha256=success_cleanup_receipt.sha256,
        document_id=document_id,
        fence_identity=fence_identity,
        outcome="success",
        processing_run_id=processing_run_id,
        provider_protocol_version=submission.provider_protocol_version,
        remote_task_identity=accepted.remote_task_identity,
        result_owner_identity=terminal.result_owner_identity,
        terminal_receipt_sha256=terminal.sha256,
    )
    success_ack_request_sha256 = sha256_bytes(success_ack_request_bytes)
    success_ack_receipt = ProviderAckReceiptV4(
        attempt_id=attempt_id,
        fence_identity=fence_identity,
        document_id=document_id,
        processing_run_id=processing_run_id,
        outcome="success",
        ack_pending_checkpoint_sha256=success_ack_pending.sha256,
        ack_pending_lifecycle_version=success_ack_pending.lifecycle_version,
        accepted_submission_sha256=accepted.sha256,
        remote_task_identity=accepted.remote_task_identity,
        result_owner_identity=terminal.result_owner_identity,
        terminal_receipt_sha256=terminal.sha256,
        failure_receipt_sha256=None,
        supersession_receipt_sha256=None,
        local_materialization_receipt_sha256=(
            local_materialization_receipt.sha256
        ),
        publication_winner_sha256=publication_winner.sha256,
        cleanup_plan_sha256=success_cleanup_plan.sha256,
        cleanup_receipt_sha256=success_cleanup_receipt.sha256,
        provider_protocol_version=submission.provider_protocol_version,
        request_identity=provider_ack_request_v4_identity(
            success_ack_request_sha256
        ),
        ack_request_sha256=success_ack_request_sha256,
        ack_kind="consumed",
        http_status=204,
        provider_response_sha256=sha256_bytes(b""),
        provider_response_byte_count=0,
        provider_receipt_identity=None,
    )
    acked = advance_remote_parse_checkpoint_v4(
        success_ack_pending,
        state="acked",
        held_resource_credit=ResourceCreditVector(),
        ack_receipt_sha256=success_ack_receipt.sha256,
    )
    remote_failure = FailureReceiptV4(
        attempt_id=attempt_id,
        fence_identity=fence_identity,
        outcome="remote_failure",
        source_state=submitted.state,
        source_lifecycle_version=submitted.lifecycle_version,
        source_checkpoint_sha256=submitted.sha256,
        submission_was_attempted=True,
        submission_absence_proof=None,
        accepted_submission_receipt_sha256=accepted.sha256,
        terminal_receipt_sha256=None,
        materialization_intent_sha256=None,
        local_materialization_receipt_sha256=None,
        error_code="provider_failed",
        error_stage="poll",
        error_class="ProviderError",
        retryable=True,
        retry_budget_class="network",
        message="provider reported a terminal failure",
    )
    cleanup_resource = CleanupResourceEntryV4(
        kind="snapshot",
        relpath=reservation.snapshot_relpath,
        ownership_basis_sha256=reservation.sha256,
        expected_sha256=source_pdf_sha256,
        expected_byte_count=100,
        action="delete",
    )
    cleanup_plan = build_local_cleanup_plan_v4(
        reservation=reservation,
        source_checkpoint=submitted,
        outcome="remote_failure",
        remote_task_identity=accepted.remote_task_identity,
        failure_receipt_sha256=remote_failure.sha256,
        resources=(cleanup_resource,),
    )
    cleanup_pending = advance_remote_parse_checkpoint_v4(
        submitted,
        state="cleanup_pending",
        held_resource_credit=submitted_credit,
        failure_receipt_sha256=remote_failure.sha256,
        cleanup_plan_sha256=cleanup_plan.sha256,
    )
    cleanup_receipt = build_local_cleanup_receipt_v4(
        plan=cleanup_plan,
        cleanup_pending_checkpoint=cleanup_pending,
        results=(
            LocalCleanupResourceResultV4(
                kind="snapshot",
                relpath=reservation.snapshot_relpath,
                disposition="absent",
            ),
        ),
    )
    ack_pending = advance_remote_parse_checkpoint_v4(
        cleanup_pending,
        state="ack_pending",
        held_resource_credit=ResourceCreditVector(
            documents=1,
            provider_tasks=1,
            ack_items=1,
        ),
        cleanup_receipt_sha256=cleanup_receipt.sha256,
    )
    ack_request_bytes = provider_ack_request_v4_bytes(
        accepted_submission_sha256=accepted.sha256,
        ack_pending_checkpoint_sha256=ack_pending.sha256,
        attempt_id=attempt_id,
        cleanup_plan_sha256=cleanup_plan.sha256,
        cleanup_receipt_sha256=cleanup_receipt.sha256,
        document_id=document_id,
        fence_identity=fence_identity,
        outcome="remote_failure",
        processing_run_id=processing_run_id,
        provider_protocol_version=submission.provider_protocol_version,
        remote_task_identity=accepted.remote_task_identity,
        result_owner_identity=None,
        terminal_receipt_sha256=None,
    )
    ack_request_sha256 = sha256_bytes(ack_request_bytes)
    ack_receipt = ProviderAckReceiptV4(
        attempt_id=attempt_id,
        fence_identity=fence_identity,
        document_id=document_id,
        processing_run_id=processing_run_id,
        outcome="remote_failure",
        ack_pending_checkpoint_sha256=ack_pending.sha256,
        ack_pending_lifecycle_version=ack_pending.lifecycle_version,
        accepted_submission_sha256=accepted.sha256,
        remote_task_identity=accepted.remote_task_identity,
        result_owner_identity=None,
        terminal_receipt_sha256=None,
        failure_receipt_sha256=remote_failure.sha256,
        supersession_receipt_sha256=None,
        local_materialization_receipt_sha256=None,
        publication_winner_sha256=None,
        cleanup_plan_sha256=cleanup_plan.sha256,
        cleanup_receipt_sha256=cleanup_receipt.sha256,
        provider_protocol_version=submission.provider_protocol_version,
        request_identity=provider_ack_request_v4_identity(ack_request_sha256),
        ack_request_sha256=ack_request_sha256,
        ack_kind="consumed",
        http_status=204,
        provider_response_sha256=sha256_bytes(b""),
        provider_response_byte_count=0,
        provider_receipt_identity=None,
    )
    remote_failed = advance_remote_parse_checkpoint_v4(
        ack_pending,
        state="remote_failed",
        held_resource_credit=ResourceCreditVector(),
        ack_receipt_sha256=ack_receipt.sha256,
    )
    preparation_failure = FailureReceiptV4(
        attempt_id=attempt_id,
        fence_identity=fence_identity,
        outcome="preparation_failure",
        source_state="not_prepared",
        source_lifecycle_version=0,
        source_checkpoint_sha256=None,
        submission_was_attempted=False,
        submission_absence_proof=None,
        accepted_submission_receipt_sha256=None,
        terminal_receipt_sha256=None,
        materialization_intent_sha256=None,
        local_materialization_receipt_sha256=None,
        error_code="snapshot_failed",
        error_stage="prepare",
        error_class="OSError",
        retryable=True,
        retry_budget_class="local_io",
        message="snapshot preparation failed",
    )
    preparation_failed = build_resource_free_remote_parse_checkpoint_v4(
        state="preparation_failed",
        attempt_id=attempt_id,
        attempt_generation=1,
        fence_identity=fence_identity,
        document_id=document_id,
        processing_run_id=processing_run_id,
        source_pdf_sha256=source_pdf_sha256,
        source_byte_count=100,
        source_page_count=2,
        request_sha256=request_sha256,
        runtime_epoch_sha256=runtime_epoch_sha256,
        process_profile_sha256=process_profile_sha256,
        credit_policy_sha256=credit_policy_sha256,
        reservation_input_sha256=reservation_input.sha256,
        failure_receipt_sha256=preparation_failure.sha256,
    )
    binding = bind_provider_secret_v4(accepted)
    entropy_chunks = [b"d" * 32, b"n" * 12, b"w" * 12]

    def deterministic_entropy(count: int) -> bytes:
        value = entropy_chunks.pop(0)
        if len(value) != count:
            raise AssertionError("test entropy request changed")
        return value

    sealed_secret = AesGcmProviderSecretCipher(
        keyring=StaticProviderSecretKeyring(
            primary_kek_id="kek-test",
            keks={"kek-test": b"k" * 32},
        ),
        rng=deterministic_entropy,
    ).seal(ProviderSecretPlaintextV4(binding=binding, token=token))
    return V4AuthorityFixture(
        document_id=document_id,
        processing_run_id=processing_run_id,
        attempt_id=attempt_id,
        fence_identity=fence_identity,
        client_submit_key=client_submit_key,
        source_pdf_sha256=source_pdf_sha256,
        parser_target_identity_json=parser_target_identity_json,
        parser_target_sha256=parser_target_sha256,
        request_sha256=request_sha256,
        runtime_epoch_sha256=runtime_epoch_sha256,
        process_profile_sha256=process_profile_sha256,
        credit_policy_sha256=credit_policy_sha256,
        reservation_input=reservation_input,
        reservation=reservation,
        preparation=preparation,
        snapshot=snapshot,
        submission=submission,
        accepted=accepted,
        preparation_failure=preparation_failure,
        remote_failure=remote_failure,
        cleanup_plan=cleanup_plan,
        cleanup_receipt=cleanup_receipt,
        ack_receipt=ack_receipt,
        prepared=prepared,
        reconciling=reconciling,
        submitted=submitted,
        cleanup_pending=cleanup_pending,
        ack_pending=ack_pending,
        remote_failed=remote_failed,
        preparation_failed=preparation_failed,
        sealed_secret=sealed_secret,
        terminal=terminal,
        materialization_intent=materialization_intent,
        local_materialization_receipt=local_materialization_receipt,
        remote_terminal=remote_terminal,
        materializing=materializing,
        local_materialized=local_materialized,
        publication_winner=publication_winner,
        publish_committed=publish_committed,
        success_cleanup_plan=success_cleanup_plan,
        success_cleanup_receipt=success_cleanup_receipt,
        success_ack_receipt=success_ack_receipt,
        success_cleanup_pending=success_cleanup_pending,
        success_ack_pending=success_ack_pending,
        acked=acked,
    )


def insert_core_rows(conn: Connection, fixture: V4AuthorityFixture) -> None:
    conn.execute(
        text(
            "INSERT INTO disclosure_core.document (document_id,status) "
            "VALUES (:document_id,'registered')"
        ),
        {"document_id": fixture.document_id},
    )
    conn.execute(
        text(
            "INSERT INTO disclosure_core.processing_run "
            "(processing_run_id,document_id,artifact_owner_processing_run_id,"
            "run_kind,status,input_raw_file_hash,provider_document_relpath,"
            "parser_target_identity) VALUES "
            "(:run_id,:document_id,:run_id,'parse','running',:source_sha,"
            "'scratch/provider.json',CAST(:target AS jsonb))"
        ),
        {
            "run_id": fixture.processing_run_id,
            "document_id": fixture.document_id,
            "source_sha": fixture.source_pdf_sha256,
            "target": fixture.parser_target_identity_json,
        },
    )


def insert_v4_head(
    conn: Connection,
    fixture: V4AuthorityFixture,
    checkpoint: RemoteParseCheckpointV4,
) -> None:
    current = checkpoint.state not in {
        "acked",
        "remote_failed",
        "local_failed",
        "pre_submission_failed",
        "preparation_failed",
        "superseded",
    }
    claimed = checkpoint.state != "prepared" and checkpoint.lifecycle_version > 0
    conn.execute(
        text(
            "INSERT INTO disclosure_ops.remote_parse_attempt "
            "(attempt_id,processing_run_id,document_id,attempt_generation,"
            "fence_identity,source_pdf_sha256,parser_target_sha256,request_sha256,"
            "runtime_epoch_sha256,client_submit_key,checkpoint_contract_version,"
            "state,is_current,row_version,current_checkpoint_sha256,claim_generation,"
            "claim_owner_identity,claim_lease_until) VALUES "
            "(:attempt_id,:run_id,:document_id,1,:fence,:source_sha,:target_sha,"
            ":request_sha,:epoch_sha,:submit_key,4,:state,:is_current,:version,"
            ":checkpoint_sha,:claim_generation,:claim_owner,:claim_lease)"
        ),
        {
            "attempt_id": fixture.attempt_id,
            "run_id": fixture.processing_run_id,
            "document_id": fixture.document_id,
            "fence": fixture.fence_identity,
            "source_sha": fixture.source_pdf_sha256,
            "target_sha": fixture.parser_target_sha256,
            "request_sha": fixture.request_sha256,
            "epoch_sha": fixture.runtime_epoch_sha256,
            "submit_key": fixture.client_submit_key,
            "state": checkpoint.state,
            "is_current": current,
            "version": checkpoint.lifecycle_version,
            "checkpoint_sha": checkpoint.sha256,
            "claim_generation": 1 if claimed else 0,
            "claim_owner": "worker-test" if claimed and current else None,
            "claim_lease": (
                datetime.now(timezone.utc) + timedelta(hours=1)
                if claimed and current
                else None
            ),
        },
    )


def insert_legacy_head(conn: Connection, fixture: V4AuthorityFixture) -> None:
    conn.execute(
        text(
            "INSERT INTO disclosure_ops.remote_parse_attempt "
            "(attempt_id,processing_run_id,document_id,attempt_generation,"
            "fence_identity,source_pdf_sha256,parser_target_sha256,request_sha256,"
            "runtime_epoch_sha256,client_submit_key,checkpoint_contract_version,"
            "state,is_current,row_version) VALUES "
            "(:attempt_id,:run_id,:document_id,1,:fence,:source_sha,:target_sha,"
            ":request_sha,:epoch_sha,:submit_key,1,'prepared',true,0)"
        ),
        {
            "attempt_id": fixture.attempt_id,
            "run_id": fixture.processing_run_id,
            "document_id": fixture.document_id,
            "fence": fixture.fence_identity,
            "source_sha": fixture.source_pdf_sha256,
            "target_sha": fixture.parser_target_sha256,
            "request_sha": fixture.request_sha256,
            "epoch_sha": fixture.runtime_epoch_sha256,
            "submit_key": fixture.client_submit_key,
        },
    )


def insert_evidence(
    conn: Connection,
    fixture: V4AuthorityFixture,
    value: EvidenceValueV4,
) -> EncodedRemoteParseEvidenceV4:
    encoded = encode_remote_parse_evidence_v4(value)
    conn.execute(
        text(
            "INSERT INTO disclosure_ops.remote_parse_v4_evidence "
            "(attempt_id,fence_identity,evidence_kind,evidence_sha256,"
            "evidence_bytes,evidence_byte_count) VALUES "
            "(:attempt_id,:fence,:kind,:sha,:payload,:byte_count)"
        ),
        {
            "attempt_id": fixture.attempt_id,
            "fence": fixture.fence_identity,
            "kind": encoded.kind,
            "sha": encoded.sha256,
            "payload": encoded.exact_bytes,
            "byte_count": encoded.byte_count,
        },
    )
    return encoded


def insert_checkpoint(
    conn: Connection,
    fixture: V4AuthorityFixture,
    checkpoint: RemoteParseCheckpointV4,
    *,
    preparation_intent_override: str | None | object = ...,
    column_overrides: dict[str, object] | None = None,
    held_credit_overrides: dict[str, int] | None = None,
    source_byte_count_override: int | None = None,
    source_page_count_override: int | None = None,
) -> None:
    reservation = (
        fixture.reservation
        if checkpoint.lifecycle_version == 0 and checkpoint.state == "prepared"
        else None
    )
    column_names = [
        "attempt_id",
        "fence_identity",
        "state",
        "lifecycle_version",
        "previous_checkpoint_sha256",
        "checkpoint_sha256",
        "checkpoint_bytes",
        "checkpoint_byte_count",
        "resource_reservation_sha256",
        "resource_reservation_bytes",
        "resource_reservation_byte_count",
        "source_byte_count",
        "source_page_count",
        *(f"held_{name}" for name in HELD_CREDIT_NAMES),
        *EVIDENCE_FIELD_NAMES,
        "publication_winner_sha256",
    ]
    values: dict[str, object] = {
        "attempt_id": fixture.attempt_id,
        "fence_identity": fixture.fence_identity,
        "state": checkpoint.state,
        "lifecycle_version": checkpoint.lifecycle_version,
        "previous_checkpoint_sha256": checkpoint.previous_checkpoint_sha256,
        "checkpoint_sha256": checkpoint.sha256,
        "checkpoint_bytes": checkpoint.canonical_bytes,
        "checkpoint_byte_count": len(checkpoint.canonical_bytes),
        "resource_reservation_sha256": None
        if reservation is None
        else reservation.sha256,
        "resource_reservation_bytes": None
        if reservation is None
        else reservation.canonical_bytes,
        "resource_reservation_byte_count": (
            None if reservation is None else len(reservation.canonical_bytes)
        ),
        "source_byte_count": checkpoint.source_byte_count
        if source_byte_count_override is None
        else source_byte_count_override,
        "source_page_count": checkpoint.source_page_count
        if source_page_count_override is None
        else source_page_count_override,
        **{
            f"held_{name}": getattr(checkpoint.held_resource_credit, name)
            for name in HELD_CREDIT_NAMES
        },
        **{name: getattr(checkpoint, name) for name in EVIDENCE_FIELD_NAMES},
        "publication_winner_sha256": checkpoint.publication_winner_sha256,
    }
    if preparation_intent_override is not ...:
        values["preparation_intent_sha256"] = preparation_intent_override
    if column_overrides:
        allowed_overrides = {
            *EVIDENCE_FIELD_NAMES,
            "publication_winner_sha256",
            "state",
        }
        unexpected = set(column_overrides) - allowed_overrides
        if unexpected:
            raise ValueError("checkpoint column override is unsupported")
        values.update(column_overrides)
    if held_credit_overrides:
        unexpected_credit = set(held_credit_overrides) - set(HELD_CREDIT_NAMES)
        if unexpected_credit:
            raise ValueError("held credit override is unsupported")
        values.update(
            {
                f"held_{name}": value
                for name, value in held_credit_overrides.items()
            }
        )
    conn.execute(
        text(
            "INSERT INTO disclosure_ops.remote_parse_v4_checkpoint ("
            + ",".join(column_names)
            + ") VALUES ("
            + ",".join(f":{name}" for name in column_names)
            + ")"
        ),
        values,
    )


def insert_secret(
    conn: Connection,
    fixture: V4AuthorityFixture,
    sealed: SealedProviderSecretV4 | None = None,
) -> None:
    value = fixture.sealed_secret if sealed is None else sealed
    binding = value.binding
    conn.execute(
        text(
            "INSERT INTO disclosure_ops.remote_parse_v4_secret "
            "(attempt_id,fence_identity,accepted_submission_sha256,secret_kind,"
            "provider_secret_version,token_sha256,token_byte_count,"
            "encryption_revision,kek_id,wrap_nonce,wrapped_dek,data_nonce,"
            "token_ciphertext) VALUES "
            "(:attempt_id,:fence,:accepted,:kind,:provider_version,:token_sha,"
            ":token_count,:revision,:kek_id,:wrap_nonce,:wrapped_dek,:data_nonce,"
            ":ciphertext)"
        ),
        {
            "attempt_id": fixture.attempt_id,
            "fence": fixture.fence_identity,
            "accepted": fixture.accepted.sha256,
            "kind": binding.secret_kind,
            "provider_version": binding.provider_secret_version,
            "token_sha": binding.token_sha256,
            "token_count": binding.token_byte_count,
            "revision": value.encryption_revision,
            "kek_id": value.kek_id,
            "wrap_nonce": value.wrap_nonce,
            "wrapped_dek": value.wrapped_dek,
            "data_nonce": value.data_nonce,
            "ciphertext": value.token_ciphertext,
        },
    )


def update_v4_head(
    conn: Connection,
    fixture: V4AuthorityFixture,
    checkpoint: RemoteParseCheckpointV4,
) -> None:
    current = checkpoint.state not in {
        "acked",
        "remote_failed",
        "local_failed",
        "pre_submission_failed",
        "preparation_failed",
        "superseded",
    }
    conn.execute(
        text(
            "UPDATE disclosure_ops.remote_parse_attempt SET "
            "state=:state,is_current=:is_current,row_version=:version,"
            "current_checkpoint_sha256=:checkpoint_sha,claim_generation=1,"
            "claim_owner_identity=:claim_owner,claim_lease_until=:claim_lease "
            "WHERE attempt_id=:attempt_id"
        ),
        {
            "attempt_id": fixture.attempt_id,
            "state": checkpoint.state,
            "is_current": current,
            "version": checkpoint.lifecycle_version,
            "checkpoint_sha": checkpoint.sha256,
            "claim_owner": "worker-test" if current else None,
            "claim_lease": (
                datetime.now(timezone.utc) + timedelta(hours=1) if current else None
            ),
        },
    )


def install_prepared_cycle(conn: Connection, fixture: V4AuthorityFixture) -> None:
    insert_core_rows(conn, fixture)
    insert_v4_head(conn, fixture, fixture.prepared)
    for evidence in fixture.prepared_evidence:
        insert_evidence(conn, fixture, evidence)
    insert_checkpoint(conn, fixture, fixture.prepared)


def install_submitted_cycle(
    conn: Connection,
    fixture: V4AuthorityFixture,
    *,
    include_secret: bool,
) -> None:
    insert_core_rows(conn, fixture)
    insert_v4_head(conn, fixture, fixture.prepared)
    for evidence in fixture.prepared_evidence:
        insert_evidence(conn, fixture, evidence)
    insert_checkpoint(conn, fixture, fixture.prepared)
    insert_evidence(conn, fixture, fixture.submission)
    insert_checkpoint(conn, fixture, fixture.reconciling)
    update_v4_head(conn, fixture, fixture.reconciling)
    insert_evidence(conn, fixture, fixture.accepted)
    insert_checkpoint(conn, fixture, fixture.submitted)
    update_v4_head(conn, fixture, fixture.submitted)
    if include_secret:
        insert_secret(conn, fixture)


def append_remote_failed_tail(
    conn: Connection,
    fixture: V4AuthorityFixture,
) -> None:
    for evidence in (
        fixture.remote_failure,
        fixture.cleanup_plan,
        fixture.cleanup_receipt,
        fixture.ack_receipt,
    ):
        insert_evidence(conn, fixture, evidence)
    for checkpoint in (
        fixture.cleanup_pending,
        fixture.ack_pending,
        fixture.remote_failed,
    ):
        insert_checkpoint(conn, fixture, checkpoint)
        update_v4_head(conn, fixture, checkpoint)


def install_remote_failed_without_secret(
    conn: Connection,
    fixture: V4AuthorityFixture,
) -> None:
    install_submitted_cycle(conn, fixture, include_secret=False)
    for evidence in (
        fixture.remote_failure,
        fixture.cleanup_plan,
        fixture.cleanup_receipt,
        fixture.ack_receipt,
    ):
        insert_evidence(conn, fixture, evidence)
    for checkpoint in (
        fixture.cleanup_pending,
        fixture.ack_pending,
        fixture.remote_failed,
    ):
        insert_checkpoint(conn, fixture, checkpoint)
        update_v4_head(conn, fixture, checkpoint)


def insert_winner(
    conn: Connection,
    fixture: V4AuthorityFixture,
) -> None:
    winner = fixture.publication_winner
    conn.execute(
        text(
            "INSERT INTO disclosure_ops.atomic_publication_winner_v4 "
            "(attempt_id,fence_identity,document_id,processing_run_id,"
            "publish_attempt_generation,local_checkpoint_sha256,"
            "lifecycle_version_before,lifecycle_version_after,request_sha256,"
            "upstream_evidence_sha256,final_units_sha256,lineage_sha256,"
            "processing_run_row_sha256,previous_active_run_id,inserted_count,"
            "updated_count,deleted_count,publish_precommit_at,winner_sha256,"
            "winner_bytes,winner_byte_count) VALUES "
            "(:attempt_id,:fence,:document_id,:run_id,:generation,"
            ":local_checkpoint,:version_before,:version_after,:request_sha,"
            ":upstream_sha,:units_sha,:lineage_sha,:run_row_sha,"
            ":previous_run,:inserted,:updated,:deleted,:published_at,"
            ":winner_sha,:winner_bytes,:winner_byte_count)"
        ),
        {
            "attempt_id": winner.attempt_id,
            "fence": winner.fence_identity,
            "document_id": winner.document_id,
            "run_id": winner.processing_run_id,
            "generation": winner.publish_attempt_generation,
            "local_checkpoint": winner.local_checkpoint_sha256,
            "version_before": winner.lifecycle_version_before,
            "version_after": winner.lifecycle_version_after,
            "request_sha": winner.request_sha256,
            "upstream_sha": winner.upstream_evidence_sha256,
            "units_sha": winner.final_units_sha256,
            "lineage_sha": winner.lineage_sha256,
            "run_row_sha": winner.processing_run_row_sha256,
            "previous_run": winner.previous_active_run_id,
            "inserted": winner.inserted_count,
            "updated": winner.updated_count,
            "deleted": winner.deleted_count,
            "published_at": winner.publish_precommit_at,
            "winner_sha": winner.sha256,
            "winner_bytes": winner.canonical_bytes,
            "winner_byte_count": len(winner.canonical_bytes),
        },
    )


def install_local_materialized_cycle(
    conn: Connection,
    fixture: V4AuthorityFixture,
) -> None:
    install_submitted_cycle(conn, fixture, include_secret=True)
    for evidence, checkpoint in (
        (fixture.terminal, fixture.remote_terminal),
        (fixture.materialization_intent, fixture.materializing),
        (
            fixture.local_materialization_receipt,
            fixture.local_materialized,
        ),
    ):
        insert_evidence(conn, fixture, evidence)
        insert_checkpoint(conn, fixture, checkpoint)
        update_v4_head(conn, fixture, checkpoint)


def install_success_ack_pending_cycle(
    conn: Connection,
    fixture: V4AuthorityFixture,
) -> None:
    install_local_materialized_cycle(conn, fixture)
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    conn.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    insert_winner(conn, fixture)
    insert_checkpoint(conn, fixture, fixture.publish_committed)
    update_v4_head(conn, fixture, fixture.publish_committed)
    for evidence, checkpoint in (
        (fixture.success_cleanup_plan, fixture.success_cleanup_pending),
        (fixture.success_cleanup_receipt, fixture.success_ack_pending),
    ):
        insert_evidence(conn, fixture, evidence)
        insert_checkpoint(conn, fixture, checkpoint)
        update_v4_head(conn, fixture, checkpoint)


def install_acked_cycle(
    conn: Connection,
    fixture: V4AuthorityFixture,
) -> None:
    install_success_ack_pending_cycle(conn, fixture)
    insert_evidence(conn, fixture, fixture.success_ack_receipt)
    insert_checkpoint(conn, fixture, fixture.acked)
    update_v4_head(conn, fixture, fixture.acked)
    deleted = conn.execute(
        text(
            "SELECT disclosure_ops.purge_remote_parse_v4_secrets_final("
            ":attempt_id,:fence,:version,:checkpoint_sha,:revision)"
        ),
        {
            "attempt_id": fixture.attempt_id,
            "fence": fixture.fence_identity,
            "version": fixture.acked.lifecycle_version,
            "checkpoint_sha": fixture.acked.sha256,
            "revision": fixture.sealed_secret.encryption_revision,
        },
    ).scalar_one()
    if deleted != 1:
        raise AssertionError("acked-cycle fixture did not purge one secret")


def install_resource_free_failure(
    conn: Connection,
    fixture: V4AuthorityFixture,
) -> None:
    insert_core_rows(conn, fixture)
    insert_v4_head(conn, fixture, fixture.preparation_failed)
    insert_evidence(conn, fixture, fixture.preparation_failure)
    insert_checkpoint(conn, fixture, fixture.preparation_failed)


__all__ = [
    "EVIDENCE_FIELD_NAMES",
    "HELD_CREDIT_NAMES",
    "V4AuthorityFixture",
    "append_remote_failed_tail",
    "build_v4_authority_fixture",
    "insert_checkpoint",
    "insert_core_rows",
    "insert_evidence",
    "insert_legacy_head",
    "insert_secret",
    "insert_v4_head",
    "insert_winner",
    "install_acked_cycle",
    "install_local_materialized_cycle",
    "install_prepared_cycle",
    "install_remote_failed_without_secret",
    "install_resource_free_failure",
    "install_success_ack_pending_cycle",
    "install_submitted_cycle",
    "sha256_bytes",
    "update_v4_head",
]
