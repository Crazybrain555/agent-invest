"""Production reconstruction of immutable inputs for the staged V4 backend.

The resolver deliberately has no ``Settings`` dependency.  Once H0 exists,
every execution-affecting value is reopened from its content-addressed spec or
from authoritative document/run rows and checked against the current V4
authority before any provider command is returned.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeVar, cast

from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import (
    TASK_PROTOCOL_V2,
    api_origin_from_task_routes_v2,
    canonical_client_submit_key_v2,
    submission_form_v2,
    submission_request_exact_bytes_v2,
)
from disclosure_anchor.application.contracts.local_materialization_manifest_v4 import (
    LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    PROVIDER_DOCUMENT_FILENAME,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    AcceptedSubmissionReceiptV4,
    PreparationIntentV4,
    SnapshotReceiptV4,
    SubmissionIntentV4,
    TerminalReceiptV4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    MaterializationIntentV4,
    ProviderEnvelopeContextV4,
    ResourceReservationV4,
    build_materialization_intent_v4,
    validate_resource_reservation_checkpoint_binding_v4,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    PerAttemptResourceAllowance,
    ResourceReservationInput,
    encode_resource_reservation_input,
)
from disclosure_anchor.application.contracts.v4_prepared_execution_spec import (
    V4PreparedExecutionSpec,
)
from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4
from disclosure_anchor.application.ports.file_store import FileStorePathPort
from disclosure_anchor.application.ports.provider_document_source import (
    ProviderDocumentSourcePort,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RemoteParseV4Authority,
)
from disclosure_anchor.application.ports.remote_provider_v4 import (
    PinnedSnapshotSourceV4,
    RemotePollCommandV4,
    RemoteSubmissionCommandV4,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    PrivateProviderCapabilityV4,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.staged_parse_coordinator import (
    StageLeaseGuard,
)
from disclosure_anchor.domain.entities import Document, ProcessingRun, Security


_EvidenceT = TypeVar("_EvidenceT")


@dataclass(frozen=True, slots=True)
class _AuthoritativeDocumentFacts:
    document: Document
    processing_run: ProcessingRun
    security: Security
    source_pdf_relpath: Path
    parser_artifact_root_relpath: Path
    provider_document_relpath: Path


@dataclass(frozen=True, slots=True)
class _BoundV4Inputs:
    spec: V4PreparedExecutionSpec
    reservation: ResourceReservationV4
    preparation: PreparationIntentV4
    facts: _AuthoritativeDocumentFacts


class ProductionV4StageInputResolver:
    """Reopen one exact H0-bound execution packet without mutable settings."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        paths: FileStorePathPort,
        provider_source: ProviderDocumentSourcePort,
        worker_profile: StagedWorkerProfileV4,
    ) -> None:
        if not callable(uow_factory):
            raise ValueError("V4 input resolver UoW factory is invalid")
        if type(worker_profile) is not StagedWorkerProfileV4:
            raise ValueError("V4 input resolver requires an exact worker profile")
        self._worker_profile = worker_profile
        self._uow_factory = uow_factory
        self._paths = paths
        self._provider_source = provider_source

    def bind_stage(
        self, authority: RemoteParseV4Authority, *, stage_guard: StageLeaseGuard,
    ) -> ProductionV4StageInputResolver:
        """Hydrate immutable H0/spec once, with no controller/global cache.

        Document/run facts and filesystem observations still reopen at their
        existing call sites. Only the immutable identity packet is shared by
        calls within this single stage. Every later stage reopens exact bytes.
        """
        stage_guard.checkpoint()
        identity = self._identity(authority)
        stage_guard.checkpoint()
        return _HydratedStageInputResolver(
            resolver=self, authority=authority, identity=identity, stage_guard=stage_guard,
        )

    def source_pdf(self, authority: RemoteParseV4Authority) -> Path:
        bound = self._bound(authority)
        return self._verified_source_path(bound)

    def submission_intent(
        self,
        authority: RemoteParseV4Authority,
        snapshot: SnapshotReceiptV4,
    ) -> SubmissionIntentV4:
        bound = self._bound(authority)
        self._require_snapshot(bound, snapshot)
        prepared = bound.spec.prepared_submission
        return SubmissionIntentV4(
            attempt_id=prepared.attempt_identity,
            fence_identity=prepared.fence_identity,
            snapshot_receipt_sha256=snapshot.sha256,
            source_pdf_sha256=prepared.source_pdf_sha256,
            parser_target_sha256=prepared.parser_target_identity_sha256,
            request_sha256=prepared.request_sha256,
            runtime_epoch_sha256=prepared.runtime_bundle_identity_sha256,
            client_submit_key=prepared.client_submit_key,
            submission_epoch_unix=prepared.submission_epoch_unix,
            provider_protocol_version=TASK_PROTOCOL_V2,
        )

    def submission_command(
        self,
        authority: RemoteParseV4Authority,
        *,
        snapshot: SnapshotReceiptV4,
        intent: SubmissionIntentV4,
        snapshot_source: PinnedSnapshotSourceV4,
        stage_guard: StageLeaseGuard,
    ) -> RemoteSubmissionCommandV4:
        bound = self._bound(authority)
        self._require_snapshot(bound, snapshot)
        expected_intent = self._expected_submission_intent(bound, authority)
        if intent != expected_intent:
            raise ValueError("V4 submission intent drifted from frozen execution spec")
        self._require_evidence(authority, "snapshot_receipt", snapshot)
        self._require_evidence(authority, "submission_intent", intent)

        # This is the final source observation performed by the resolver before
        # returning the POST command.  A changed byte/page/hash fact fails closed.
        self._verified_source_path(bound)
        upload_filename = (
            bound.reservation.source_pdf_sha256.removeprefix("sha256:") + ".pdf"
        )
        request = submission_request_exact_bytes_v2(
            api_origin=bound.spec.api_origin,
            form=submission_form_v2(
                bound.spec.parser_options,
                server_url=bound.spec.server_url,
            ),
            upload_filename=upload_filename,
        )
        if request != bound.spec.request_exact_bytes:
            raise ValueError("V4 submission request drifted from exact execution spec")
        return RemoteSubmissionCommandV4(
            submission_intent=intent,
            snapshot_receipt=snapshot,
            snapshot_source=snapshot_source,
            source_byte_count=bound.reservation.source_byte_count,
            parser_identity=bound.spec.parser_identity,
            parser_options=bound.spec.parser_options,
            upload_filename=upload_filename,
            request_exact_bytes=request,
            step_guard=stage_guard,
        )

    def poll_command(
        self,
        authority: RemoteParseV4Authority,
        *,
        intent: SubmissionIntentV4,
        accepted: AcceptedSubmissionReceiptV4,
        capability: PrivateProviderCapabilityV4,
        stage_guard: StageLeaseGuard,
    ) -> RemotePollCommandV4:
        bound = self._bound(authority)
        self._require_evidence(authority, "submission_intent", intent)
        self._require_evidence(authority, "accepted_submission", accepted)
        expected_origin = api_origin_from_task_routes_v2(
            status_url=accepted.status_url,
            result_url=accepted.result_url,
            task_id=accepted.remote_task_identity,
        )
        if (
            expected_origin != bound.spec.api_origin
            or intent != self._expected_submission_intent(bound, authority)
            or not capability.validates_accepted_submission(accepted)
            or capability.capability_purpose != "submitted_task_resume"
        ):
            raise ValueError("V4 poll inputs drifted from frozen authority")
        return RemotePollCommandV4(
            submission_intent=intent,
            accepted_submission=accepted,
            provider_capability=capability,
            artifact_byte_limit=bound.reservation.reserved_credit.provider_result_bytes,
            result_lease_seconds=bound.spec.result_lease_seconds,
            step_guard=stage_guard,
        )

    def materialization_intent(
        self,
        authority: RemoteParseV4Authority,
        *,
        accepted: AcceptedSubmissionReceiptV4,
        terminal: TerminalReceiptV4,
        capability: PrivateProviderCapabilityV4,
    ) -> MaterializationIntentV4:
        bound = self._bound(authority)
        self._require_evidence(authority, "accepted_submission", accepted)
        self._require_evidence(authority, "terminal_receipt", terminal)
        if (
            authority.state != "remote_terminal"
            or terminal.accepted_submission_receipt_sha256 != accepted.sha256
            or (
                terminal.attempt_id,
                terminal.fence_identity,
                terminal.remote_task_identity,
            )
            != (
                accepted.attempt_id,
                accepted.fence_identity,
                accepted.remote_task_identity,
            )
            or accepted.provider_protocol_version != TASK_PROTOCOL_V2
            or terminal.provider_protocol_version != TASK_PROTOCOL_V2
            or api_origin_from_task_routes_v2(
                status_url=accepted.status_url,
                result_url=accepted.result_url,
                task_id=accepted.remote_task_identity,
            )
            != bound.spec.api_origin
            or terminal.artifact_byte_count
            > bound.reservation.reserved_credit.provider_result_bytes
            or not capability.validates_accepted_submission(accepted)
            or capability.capability_purpose != "result_download"
        ):
            raise ValueError("V4 materialization inputs drifted from frozen authority")
        target = bound.spec.parser_options.target_identity(bound.spec.parser_identity)
        context = ProviderEnvelopeContextV4(
            document_id=authority.document_id,
            processing_run_id=authority.processing_run_id,
            provider=cast(str, bound.facts.document.provider),
            provider_document_id=cast(
                str, bound.facts.document.provider_document_id
            ),
            source_pdf_relpath=bound.facts.source_pdf_relpath.as_posix(),
            source_pdf_sha256=bound.reservation.source_pdf_sha256,
            source_page_count=bound.reservation.source_page_count,
            parser_artifact_root_relpath=(
                bound.facts.parser_artifact_root_relpath.as_posix()
            ),
            parser_target_identity=target,
        )
        allowance = self._allowance(bound.reservation)
        return build_materialization_intent_v4(
            reservation=bound.reservation,
            source_checkpoint=authority.checkpoint,
            terminal_receipt_sha256=terminal.sha256,
            remote_task_identity=terminal.remote_task_identity,
            artifact_owner_identity=terminal.result_owner_identity,
            artifact_sha256=terminal.artifact_sha256,
            artifact_byte_count=terminal.artifact_byte_count,
            provider_envelope_context=context,
            allowance_sha256=allowance.sha256,
            provider_capability_kind=capability.secret_kind,
            provider_capability_sha256=capability.token_sha256,
            provider_capability_byte_count=capability.token_byte_count,
            output_dir_name=authority.processing_run_id,
            provider_envelope_relpath=PROVIDER_DOCUMENT_FILENAME,
            output_manifest_relpath=LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
            member_count_limit=bound.spec.archive_member_count_limit,
            uncompressed_byte_limit=bound.spec.archive_uncompressed_byte_limit,
        )

    def materialization_allowance(
        self,
        authority: RemoteParseV4Authority,
        intent: MaterializationIntentV4,
    ) -> PerAttemptResourceAllowance:
        bound = self._bound(authority)
        self._require_evidence(authority, "materialization_intent", intent)
        allowance = self._allowance(bound.reservation)
        context = intent.provider_envelope_context
        target = bound.spec.parser_options.target_identity(bound.spec.parser_identity)
        if (
            authority.state != "materializing"
            or intent.reservation_sha256 != bound.reservation.sha256
            or intent.allowance_sha256 != allowance.sha256
            or intent.member_count_limit != bound.spec.archive_member_count_limit
            or intent.uncompressed_byte_limit
            != bound.spec.archive_uncompressed_byte_limit
            or intent.output_dir_name != authority.processing_run_id
            or context.document_id != authority.document_id
            or context.processing_run_id != authority.processing_run_id
            or context.provider != bound.facts.document.provider
            or context.provider_document_id
            != bound.facts.document.provider_document_id
            or context.source_pdf_relpath
            != bound.facts.source_pdf_relpath.as_posix()
            or context.source_pdf_sha256 != bound.reservation.source_pdf_sha256
            or context.source_page_count != bound.reservation.source_page_count
            or context.parser_artifact_root_relpath
            != bound.facts.parser_artifact_root_relpath.as_posix()
            or context.parser_target_identity != target
        ):
            raise ValueError("V4 materialization allowance drifted from authority")
        return allowance

    def result_lease_seconds(self, authority: RemoteParseV4Authority) -> int:
        return self._bound(authority).spec.result_lease_seconds

    def remote_runaway_seconds(self, authority: RemoteParseV4Authority) -> int:
        return self._bound(authority).spec.remote_runaway_seconds

    def assert_execution_profile(self, authority: RemoteParseV4Authority) -> None:
        """Fence every lane, including tails after source/staging cleanup.

        This only reopens H0 identity; it must not require source bytes or
        document/run activity, which legitimately change after publication.
        """
        self._identity(authority)

    def _identity(
        self, authority: RemoteParseV4Authority,
    ) -> tuple[ResourceReservationV4, PreparationIntentV4, V4PreparedExecutionSpec]:
        if (
            type(authority) is not RemoteParseV4Authority
            or not authority.is_current
            or authority.claim_owner_identity is None
            or authority.claim_generation < 1
        ):
            raise ValueError("V4 input resolution requires current claimed authority")
        reservation = authority.reservation
        if type(reservation) is not ResourceReservationV4:
            raise ValueError("V4 input resolution lacks exact resource reservation")
        validate_resource_reservation_checkpoint_binding_v4(
            reservation=reservation,
            checkpoint=authority.checkpoint,
        )
        h0 = authority.checkpoint_history[0]
        if h0.state != "prepared" or h0.lifecycle_version != 0:
            raise ValueError("V4 input resolution lacks prepared H0 authority")
        validate_resource_reservation_checkpoint_binding_v4(
            reservation=reservation,
            checkpoint=h0,
        )
        preparation = self._single_evidence(
            authority,
            "preparation_intent",
            PreparationIntentV4,
        )
        if h0.preparation_intent_sha256 != preparation.sha256:
            raise ValueError("V4 H0 preparation intent binding drifted")
        spec = authority.execution_spec
        if (type(spec) is not V4PreparedExecutionSpec
            or spec.sha256 != preparation.execution_spec_sha256
            or spec.byte_count != preparation.execution_spec_byte_count):
            raise ValueError("V4 execution authority returned the wrong exact object")
        self._validate_closed_packet(authority, reservation, preparation, spec)
        return reservation, preparation, spec

    def _bound(self, authority: RemoteParseV4Authority) -> _BoundV4Inputs:
        reservation, preparation, spec = self._identity(authority)
        h0 = authority.checkpoint_history[0]
        facts = self._document_facts(authority, reservation, spec)
        bound = _BoundV4Inputs(
            spec=spec,
            reservation=reservation,
            preparation=preparation,
            facts=facts,
        )
        if h0.snapshot_receipt_sha256 is not None:
            snapshot = self._single_evidence(
                authority,
                "snapshot_receipt",
                SnapshotReceiptV4,
            )
            self._require_snapshot(bound, snapshot)
            if snapshot.sha256 != h0.snapshot_receipt_sha256:
                raise ValueError("V4 H0 snapshot receipt binding drifted")
        return bound

    def _validate_closed_packet(
        self,
        authority: RemoteParseV4Authority,
        reservation: ResourceReservationV4,
        preparation: PreparationIntentV4,
        spec: V4PreparedExecutionSpec,
    ) -> None:
        prepared = spec.prepared_submission
        if spec.worker_profile != self._worker_profile:
            raise ValueError("V4 worker composition changed; restore the bound profile before recovery")
        expected_key = canonical_client_submit_key_v2(
            source_pdf_sha256=prepared.source_pdf_sha256,
            attempt_identity=prepared.attempt_identity,
            fence_identity=prepared.fence_identity,
            submission_epoch_unix=prepared.submission_epoch_unix,
        )
        upload_filename = prepared.source_pdf_sha256.removeprefix("sha256:") + ".pdf"
        expected_request = submission_request_exact_bytes_v2(
            api_origin=spec.api_origin,
            form=submission_form_v2(spec.parser_options, server_url=spec.server_url),
            upload_filename=upload_filename,
        )
        authority_facts = (
            authority.attempt_id,
            authority.processing_run_id,
            authority.document_id,
            authority.attempt_generation,
            authority.fence_identity,
            authority.source_pdf_sha256,
            authority.parser_target_sha256,
            authority.request_sha256,
            authority.runtime_epoch_sha256,
            authority.client_submit_key,
        )
        reservation_facts = (
            reservation.attempt_id,
            reservation.processing_run_id,
            reservation.document_id,
            reservation.attempt_generation,
            reservation.fence_identity,
            reservation.source_pdf_sha256,
            prepared.parser_target_identity_sha256,
            reservation.request_sha256,
            reservation.runtime_epoch_sha256,
            prepared.client_submit_key,
        )
        prepared_facts = (
            prepared.attempt_identity,
            reservation.processing_run_id,
            reservation.document_id,
            reservation.attempt_generation,
            prepared.fence_identity,
            prepared.source_pdf_sha256,
            prepared.parser_target_identity_sha256,
            prepared.request_sha256,
            prepared.runtime_bundle_identity_sha256,
            prepared.client_submit_key,
        )
        preparation_facts = (
            preparation.attempt_id,
            preparation.processing_run_id,
            preparation.document_id,
            reservation.attempt_generation,
            preparation.fence_identity,
            preparation.source_pdf_sha256,
            preparation.parser_target_sha256,
            preparation.request_sha256,
            preparation.runtime_epoch_sha256,
            prepared.client_submit_key,
        )
        if not (
            authority_facts == reservation_facts == prepared_facts == preparation_facts
        ):
            raise ValueError("V4 authority/reservation/H0 execution identities drifted")
        if (
            expected_key != prepared.client_submit_key
            or expected_request != spec.request_exact_bytes
            or reservation.prepared_submission_identity_sha256 != prepared.sha256
            or reservation.process_profile_sha256 != spec.process_profile_sha256
            or preparation.reservation_sha256 != reservation.sha256
            or preparation.source_byte_count != reservation.source_byte_count
            or preparation.source_page_count != reservation.source_page_count
            or preparation.process_profile_sha256 != reservation.process_profile_sha256
            or preparation.snapshot_relpath != reservation.snapshot_relpath
            or preparation.snapshot_part_relpath != reservation.snapshot_part_relpath
            or preparation.snapshot_part_owner_relpath
            != reservation.snapshot_part_owner_relpath
            or preparation.snapshot_lock_relpath != reservation.snapshot_lock_relpath
        ):
            raise ValueError("V4 frozen execution packet is not closed")

    def _document_facts(
        self,
        authority: RemoteParseV4Authority,
        reservation: ResourceReservationV4,
        spec: V4PreparedExecutionSpec,
    ) -> _AuthoritativeDocumentFacts:
        with self._uow_factory() as uow:
            document = uow.documents.get(authority.document_id)
            run = uow.processing_runs.get(authority.processing_run_id)
            security = (
                uow.securities.get(document.security_id)
                if document is not None and document.security_id is not None
                else None
            )
        if document is None or run is None or security is None:
            raise ValueError("V4 authoritative document/run/security facts are absent")
        required_document = (
            document.provider,
            document.provider_document_id,
            document.raw_file_relpath,
            document.raw_file_hash,
            document.security_id,
        )
        if any(type(value) is not str or not value for value in required_document):
            raise ValueError("V4 authoritative document source facts are incomplete")
        source_relpath = self._safe_relpath(cast(str, document.raw_file_relpath))
        source_parts = PurePosixPath(source_relpath.as_posix()).parts
        source_digest_name = (
            "sha256_" + reservation.source_pdf_sha256.removeprefix("sha256:") + ".pdf"
        )
        if (
            len(source_parts) != 6
            or source_parts[0] != "raw_documents"
            or source_parts[1] != document.provider
            or source_parts[2] != security.security_code
            or source_parts[4] != document.provider_document_id
            or source_parts[5] != source_digest_name
        ):
            raise ValueError("V4 authoritative source path identity drifted from H0")
        target = spec.parser_options.target_identity(spec.parser_identity)
        parser_artifact = self._paths.parser_run_artifacts_v4_relpath(
            provider=cast(str, document.provider),
            security_code=security.security_code,
            provider_document_id=cast(str, document.provider_document_id),
            processing_run_id=authority.processing_run_id,
            source_pdf_sha256=reservation.source_pdf_sha256,
            parser_backend=target.backend,
            parser_method=target.method,
        )
        provider_document = self._paths.provider_document_relpath(
            provider=cast(str, document.provider),
            security_code=security.security_code,
            provider_document_id=cast(str, document.provider_document_id),
            artifact_owner_processing_run_id=authority.processing_run_id,
        )
        expected_run = (
            authority.processing_run_id,
            authority.document_id,
            authority.processing_run_id,
            "parse",
            "running",
            target.name,
            target.package_version,
            target.backend,
            target.method,
            target.language,
            target.to_payload(),
            reservation.source_pdf_sha256,
            parser_artifact.as_posix(),
            provider_document.as_posix(),
            False,
        )
        observed_run = (
            run.processing_run_id,
            run.document_id,
            run.artifact_owner_processing_run_id,
            run.run_kind,
            run.status,
            run.parser_name,
            run.parser_version,
            run.parser_backend,
            run.parser_method,
            run.parser_language,
            run.parser_target_identity,
            run.input_raw_file_hash,
            run.parser_artifact_relpath,
            run.provider_document_relpath,
            run.is_active,
        )
        if (
            document.document_id != authority.document_id
            or document.raw_file_hash != reservation.source_pdf_sha256
            or document.security_id != security.security_id
            or observed_run != expected_run
        ):
            raise ValueError("V4 authoritative document/run facts drifted from H0")
        return _AuthoritativeDocumentFacts(
            document=document,
            processing_run=run,
            security=security,
            source_pdf_relpath=source_relpath,
            parser_artifact_root_relpath=parser_artifact,
            provider_document_relpath=provider_document,
        )

    def _verified_source_path(self, bound: _BoundV4Inputs) -> Path:
        observed = self._provider_source.observe_source_pdf(
            bound.facts.source_pdf_relpath
        )
        expected = (
            bound.reservation.source_pdf_sha256,
            bound.reservation.source_byte_count,
            bound.reservation.source_page_count,
        )
        if (observed.sha256, observed.byte_count, observed.page_count) != expected:
            raise ValueError("V4 source PDF hash/byte/page identity drifted before POST")
        path = self._paths.data_path(bound.facts.source_pdf_relpath)
        if not path.is_absolute():
            raise ValueError("V4 source PDF path authority returned a relative path")
        return path

    def _expected_submission_intent(
        self,
        bound: _BoundV4Inputs,
        authority: RemoteParseV4Authority,
    ) -> SubmissionIntentV4:
        snapshot = self._single_evidence(
            authority,
            "snapshot_receipt",
            SnapshotReceiptV4,
        )
        self._require_snapshot(bound, snapshot)
        prepared = bound.spec.prepared_submission
        return SubmissionIntentV4(
            attempt_id=prepared.attempt_identity,
            fence_identity=prepared.fence_identity,
            snapshot_receipt_sha256=snapshot.sha256,
            source_pdf_sha256=prepared.source_pdf_sha256,
            parser_target_sha256=prepared.parser_target_identity_sha256,
            request_sha256=prepared.request_sha256,
            runtime_epoch_sha256=prepared.runtime_bundle_identity_sha256,
            client_submit_key=prepared.client_submit_key,
            submission_epoch_unix=prepared.submission_epoch_unix,
            provider_protocol_version=TASK_PROTOCOL_V2,
        )

    @staticmethod
    def _allowance(reservation: ResourceReservationV4) -> PerAttemptResourceAllowance:
        encoded = encode_resource_reservation_input(
            ResourceReservationInput(
                source_pdf_sha256=reservation.source_pdf_sha256,
                source_byte_count=reservation.source_byte_count,
                source_page_count=reservation.source_page_count,
                process_profile_sha256=reservation.process_profile_sha256,
                credit_policy_sha256=reservation.credit_policy_sha256,
                bucket=reservation.reservation_bucket,
                reservation=reservation.reserved_credit,
            )
        )
        if encoded.sha256 != reservation.reservation_input_sha256:
            raise ValueError("V4 reservation input cannot be reconstructed exactly")
        return PerAttemptResourceAllowance(
            reservation_input_sha256=encoded.sha256,
            reservation_input=encoded,
            limits=reservation.reserved_credit,
        )

    @staticmethod
    def _require_snapshot(
        bound: _BoundV4Inputs,
        snapshot: SnapshotReceiptV4,
    ) -> None:
        if (
            type(snapshot) is not SnapshotReceiptV4
            or snapshot.attempt_id != bound.reservation.attempt_id
            or snapshot.fence_identity != bound.reservation.fence_identity
            or snapshot.preparation_intent_sha256 != bound.preparation.sha256
            or snapshot.snapshot_relpath != bound.reservation.snapshot_relpath
            or snapshot.snapshot_sha256 != bound.reservation.source_pdf_sha256
            or snapshot.snapshot_byte_count != bound.reservation.source_byte_count
        ):
            raise ValueError("V4 snapshot receipt drifted from H0")

    @staticmethod
    def _single_evidence(
        authority: RemoteParseV4Authority,
        kind: str,
        expected_type: type[_EvidenceT],
    ) -> _EvidenceT:
        matches = tuple(item.value for item in authority.evidence if item.kind == kind)
        if len(matches) != 1 or type(matches[0]) is not expected_type:
            raise ValueError(f"V4 {kind} evidence is absent, duplicated, or mistyped")
        return cast(_EvidenceT, matches[0])

    @staticmethod
    def _require_evidence(
        authority: RemoteParseV4Authority,
        kind: str,
        expected: object,
    ) -> None:
        if ProductionV4StageInputResolver._single_evidence(
            authority,
            kind,
            type(expected),
        ) != expected:
            raise ValueError(f"V4 {kind} value drifted from durable evidence")

    @staticmethod
    def _safe_relpath(value: str) -> Path:
        pure = PurePosixPath(value)
        if pure.is_absolute() or not pure.parts or ".." in pure.parts:
            raise ValueError("V4 authoritative source path is unsafe")
        return Path(*pure.parts)


class _HydratedStageInputResolver(ProductionV4StageInputResolver):
    """A stack-owned stage view, never retained by the shared root resolver."""

    def __init__(
        self, *, resolver: ProductionV4StageInputResolver, authority: RemoteParseV4Authority,
        identity: tuple[ResourceReservationV4, PreparationIntentV4, V4PreparedExecutionSpec],
        stage_guard: StageLeaseGuard,
    ) -> None:
        super().__init__(
            uow_factory=resolver._uow_factory, paths=resolver._paths,
            provider_source=resolver._provider_source, worker_profile=resolver._worker_profile,
        )
        self._stage_authority = authority
        self._stage_identity = identity
        self._stage_guard = stage_guard

    def bind_stage(
        self, authority: RemoteParseV4Authority, *, stage_guard: StageLeaseGuard,
    ) -> ProductionV4StageInputResolver:
        raise ValueError("a hydrated V4 stage cannot authorize another stage")

    def _identity(
        self, authority: RemoteParseV4Authority,
    ) -> tuple[ResourceReservationV4, PreparationIntentV4, V4PreparedExecutionSpec]:
        self._stage_guard.checkpoint()
        if authority is not self._stage_authority:
            raise ValueError("hydrated V4 inputs belong to another exact stage authority")
        return self._stage_identity


__all__ = ["ProductionV4StageInputResolver"]
