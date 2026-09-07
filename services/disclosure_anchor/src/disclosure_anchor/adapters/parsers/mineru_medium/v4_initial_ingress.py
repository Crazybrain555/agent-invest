"""Build one immutable production V4 ingress packet before PostgreSQL handoff."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import hashlib

from disclosure_anchor.adapters.parsers.mineru_medium.http_staged import (
    prepare_submission_identity_v2,
)
from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import (
    submission_form_v2,
    submission_request_exact_bytes_v2,
)
from disclosure_anchor.application.contracts.mineru_process_profile import (
    MineruProcessProfile,
    encode_mineru_process_profile,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
    build_staged_resource_credit_envelope,
)
from disclosure_anchor.application.contracts.provider_document_admission import SourcePdfObservation
from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4
from disclosure_anchor.application.services.staged_v4_capacity import staged_v4_coordinator_limits
from disclosure_anchor.application.contracts.v4_prepared_execution_spec import (
    V4_PREPARED_EXECUTION_SPEC_CONTRACT,
    V4PreparedExecutionSpec,
)
from disclosure_anchor.application.ports.file_store import FileStorePathPort
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions
from disclosure_anchor.application.ports.remote_parse_v4_ingress import (
    V4InitialIngressCommit,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    V4PreparedProposal,
)
from disclosure_anchor.application.ports.staged_new_work_v4 import (
    V4InitialIngressCapacityBlocked,
    V4OrdinaryParseCandidate,
    V4AdmissionObservationRequest,
    V4AdmissionObservationResult,
    V4SourcePdfObserverPort,
    V4RejectedSourcePdf,
)
from disclosure_anchor.application.ports.remote_parse_v4_source_rejection import V4SourceRejectionCommit
from disclosure_anchor.application.ports.staged_provider_parser import V4StageGuard
from disclosure_anchor.domain import ids


class MinerUV4InitialIngressFactory:
    """Freeze all execution inputs and produce H0's exact atomic command."""

    def __init__(
        self,
        *,
        source: V4SourcePdfObserverPort,
        paths: FileStorePathPort,
        parser_identity: ParserIdentity,
        parser_options: ParserOptions,
        process_profile: MineruProcessProfile,
        process_profile_exact_bytes: bytes,
        worker_profile: StagedWorkerProfileV4,
        result_lease_seconds: int,
        remote_runaway_seconds: int,
        archive_member_count_limit: int,
        archive_uncompressed_byte_limit: int,
        max_retries: int,
        scope_classes: tuple[str, ...] | None,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
        attempt_id_factory: Callable[[], str] = lambda: ids.new_id("rpa"),
        fence_id_factory: Callable[[], str] = lambda: ids.new_id("fence"),
        processing_run_id_factory: Callable[[], str] = lambda: str(
            ids.new_processing_run_id()
        ),
        outbox_event_id_factory: Callable[[], str] = lambda: str(
            ids.new_outbox_event_id()
        ),
    ) -> None:
        if (
            not callable(getattr(source, "observe", None))
            or not callable(getattr(paths, "parser_run_artifacts_v4_relpath", None))
            or type(parser_identity) is not ParserIdentity
            or type(parser_options) is not ParserOptions
            or type(process_profile) is not MineruProcessProfile
            or type(process_profile_exact_bytes) is not bytes
            or encode_mineru_process_profile(process_profile)
            != process_profile_exact_bytes
            or type(worker_profile) is not StagedWorkerProfileV4
            or any(
                not callable(factory)
                for factory in (
                    utc_now,
                    attempt_id_factory,
                    fence_id_factory,
                    processing_run_id_factory,
                    outbox_event_id_factory,
                )
            )
        ):
            raise ValueError("V4 initial ingress dependencies are not closed")
        if (
            parser_options.api_url is None
            or parser_options.server_url is None
            or parser_options.runtime_bundle_identity_sha256
            != process_profile.runtime_bundle_identity_sha256
        ):
            raise ValueError("V4 initial ingress parser/runtime identity is incomplete")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 1:
            raise ValueError("V4 initial ingress retry limit is invalid")
        self._source = source
        self._paths = paths
        self._parser_identity = parser_identity
        self._parser_options = parser_options
        self._process_profile = process_profile
        self._process_profile_exact_bytes = process_profile_exact_bytes
        self._worker_profile = worker_profile
        self._coordinator_capacity = staged_v4_coordinator_limits(
            process_profile, worker_profile=worker_profile,
        ).credits
        self._result_lease_seconds = result_lease_seconds
        self._remote_runaway_seconds = remote_runaway_seconds
        self._archive_member_count_limit = archive_member_count_limit
        self._archive_uncompressed_byte_limit = archive_uncompressed_byte_limit
        self._max_retries = max_retries
        self._scope_classes = scope_classes
        self._utc_now = utc_now
        self._attempt_id_factory = attempt_id_factory
        self._fence_id_factory = fence_id_factory
        self._processing_run_id_factory = processing_run_id_factory
        self._outbox_event_id_factory = outbox_event_id_factory

    def observation_request(
        self, candidate: V4OrdinaryParseCandidate, *, available_credits: ResourceCreditVector,
    ) -> V4AdmissionObservationRequest:
        if type(candidate) is not V4OrdinaryParseCandidate:
            raise ValueError("V4 initial ingress candidate must be exact")
        # Unknown archived length consumes the configured ceiling until real
        # stat/hash evidence exists; this is a budget, not invented provenance.
        credits = ResourceCreditVector(
            documents=1, snapshot_items=1,
            snapshot_bytes=(candidate.archived_raw_byte_count
                            if candidate.archived_raw_byte_count is not None
                            else self._coordinator_capacity.snapshot_bytes),
        )
        self._require_capacity(credits, available_credits=available_credits)
        return V4AdmissionObservationRequest(candidate=candidate, credits=credits)

    def observe(
        self, request: V4AdmissionObservationRequest, *, stage_guard: V4StageGuard,
    ) -> V4AdmissionObservationResult:
        return self._source.observe(request, stage_guard=stage_guard)

    def source_rejection(
        self, candidate: V4OrdinaryParseCandidate, rejection: V4RejectedSourcePdf,
    ) -> V4SourceRejectionCommit:
        target = self._parser_options.target_identity(self._parser_identity)
        run_id = self._processing_run_id_factory()
        artifact = self._paths.parser_run_artifacts_v4_relpath(
            provider=candidate.provider, security_code=candidate.security_code,
            provider_document_id=candidate.provider_document_id,
            processing_run_id=run_id, source_pdf_sha256=rejection.sha256,
            parser_backend=target.backend, parser_method=target.method,
        )
        provider = self._paths.provider_document_relpath(
            provider=candidate.provider, security_code=candidate.security_code,
            provider_document_id=candidate.provider_document_id,
            artifact_owner_processing_run_id=run_id,
        )
        return V4SourceRejectionCommit(
            candidate=candidate, rejection=rejection, parser_target=target,
            processing_run_id=run_id, parser_artifact_relpath=artifact.as_posix(),
            provider_document_relpath=provider.as_posix(), failed_at=self._utc_now(),
            created_outbox_event_id=self._outbox_event_id_factory(),
            failed_outbox_event_id=self._outbox_event_id_factory(),
            max_retries=self._max_retries, scope_classes=self._scope_classes,
        )

    def build(
        self,
        candidate: V4OrdinaryParseCandidate,
        *,
        source_observation: SourcePdfObservation,
        available_credits: ResourceCreditVector,
    ) -> V4InitialIngressCommit:
        if type(candidate) is not V4OrdinaryParseCandidate:
            raise ValueError("V4 initial ingress candidate must be exact")
        if type(available_credits) is not ResourceCreditVector:
            raise ValueError("V4 initial ingress available credits must be exact")
        if type(source_observation) is not SourcePdfObservation:
            raise ValueError("V4 initial ingress requires a completed exact source observation")
        observed = source_observation
        if (
            observed.sha256 != candidate.raw_file_hash
            or (
                candidate.archived_raw_byte_count is not None
                and observed.byte_count != candidate.archived_raw_byte_count
            )
        ):
            raise ValueError("V4 initial ingress source differs from archived authority")
        # Classify a valid but oversized source before the envelope builder's
        # closed-contract rejection. One ineligible PDF must not abort scanning
        # the rest of the ordinary backlog.
        self._require_capacity(
            ResourceCreditVector(snapshot_bytes=observed.byte_count, output_pages=observed.page_count),
            available_credits=available_credits,
        )
        credit = build_staged_resource_credit_envelope(
            profile=self._process_profile,
            source_pdf_sha256=observed.sha256,
            source_byte_count=observed.byte_count,
            source_page_count=observed.page_count,
        )
        self._require_capacity(credit.reservation, available_credits=available_credits)
        # The configured archive ceilings are process-wide maxima.  Freeze the
        # exact per-attempt ceilings inside the execution spec so a small
        # document cannot later authorize extraction beyond its own durable
        # output/temporary-disk reservation.
        archive_member_count_limit = self._archive_member_count_limit
        archive_uncompressed_byte_limit = min(
            self._archive_uncompressed_byte_limit,
            credit.reservation.temp_disk_bytes,
            credit.reservation.output_bytes,
        )

        started_at = self._utc_now()
        if (
            not isinstance(started_at, datetime)
            or started_at.tzinfo is None
            or started_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("V4 initial ingress clock must return UTC")
        attempt_id = self._attempt_id_factory()
        fence_identity = self._fence_id_factory()
        processing_run_id = self._processing_run_id_factory()
        api_origin = self._parser_options.api_url
        server_url = self._parser_options.server_url
        assert api_origin is not None
        assert server_url is not None
        prepared = prepare_submission_identity_v2(
            api_url=api_origin,
            server_url=server_url,
            options=self._parser_options,
            source_pdf_sha256=observed.sha256,
            attempt_identity=attempt_id,
            fence_identity=fence_identity,
            submission_epoch_unix=int(started_at.timestamp()),
        )
        request_exact = submission_request_exact_bytes_v2(
            api_origin=api_origin,
            form=submission_form_v2(self._parser_options, server_url=server_url),
            upload_filename=f"sha256_{observed.sha256[7:]}.pdf",
        )
        spec = V4PreparedExecutionSpec(
            contract_version=V4_PREPARED_EXECUTION_SPEC_CONTRACT,
            prepared_submission=prepared,
            parser_identity=self._parser_identity,
            parser_options=self._parser_options,
            api_origin=api_origin,
            server_url=server_url,
            request_exact_bytes=request_exact,
            request_sha256="sha256:" + hashlib.sha256(request_exact).hexdigest(),
            process_profile_exact_bytes=self._process_profile_exact_bytes,
            process_profile_sha256=self._process_profile.sha256,
            worker_profile=self._worker_profile,
            result_lease_seconds=self._result_lease_seconds,
            remote_runaway_seconds=self._remote_runaway_seconds,
            archive_member_count_limit=archive_member_count_limit,
            archive_uncompressed_byte_limit=archive_uncompressed_byte_limit,
        )
        target = self._parser_options.target_identity(self._parser_identity)
        artifact_relpath = self._paths.parser_run_artifacts_v4_relpath(
            provider=candidate.provider,
            security_code=candidate.security_code,
            provider_document_id=candidate.provider_document_id,
            processing_run_id=processing_run_id,
            source_pdf_sha256=observed.sha256,
            parser_backend=target.backend,
            parser_method=target.method,
        )
        provider_relpath = self._paths.provider_document_relpath(
            provider=candidate.provider,
            security_code=candidate.security_code,
            provider_document_id=candidate.provider_document_id,
            artifact_owner_processing_run_id=processing_run_id,
        )
        return V4InitialIngressCommit(
            proposal=V4PreparedProposal(
                document_id=candidate.document_id,
                processing_run_id=processing_run_id,
                prepared_submission=prepared,
                credit_envelope=credit,
                execution_spec=spec,
            ),
            source_observation=observed,
            expected_provider=candidate.provider,
            expected_provider_document_id=candidate.provider_document_id,
            expected_security_id=candidate.security_id,
            expected_raw_file_relpath=candidate.raw_file_relpath,
            expected_raw_file_hash=candidate.raw_file_hash,
            parser_target=target,
            parser_artifact_relpath=str(artifact_relpath),
            provider_document_relpath=str(provider_relpath),
            started_at=started_at,
            created_outbox_event_id=self._outbox_event_id_factory(),
            max_retries=self._max_retries,
            scope_classes=self._scope_classes,
        )

    def _require_capacity(
        self,
        reservation: ResourceCreditVector,
        *,
        available_credits: ResourceCreditVector,
    ) -> None:
        initial_held = ResourceCreditVector(
            documents=1,
            snapshot_items=1,
            snapshot_bytes=reservation.snapshot_bytes,
        )
        blocked = tuple(
            name
            for name in ResourceCreditVector.__dataclass_fields__
            if getattr(reservation, name) > getattr(self._coordinator_capacity, name)
            or getattr(initial_held, name) > getattr(available_credits, name)
        )
        if blocked:
            raise V4InitialIngressCapacityBlocked(
                blocked,
                ineligible_dimensions=tuple(
                    name for name in blocked
                    if getattr(reservation, name) > getattr(self._coordinator_capacity, name)
                ),
            )


__all__ = [
    "MinerUV4InitialIngressFactory",
    "V4InitialIngressCapacityBlocked",
]
