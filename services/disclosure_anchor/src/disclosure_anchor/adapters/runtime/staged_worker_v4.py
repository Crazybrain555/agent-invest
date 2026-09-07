"""Explicit, default-off production composition for the staged V4 worker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import time

from sqlalchemy.engine import Engine

from disclosure_anchor.adapters.db.postgres.atomic_document_publisher_v4 import (
    PostgresAtomicWholeDocumentPublisherV4,
)
from disclosure_anchor.adapters.db.postgres.staged_new_work_v4 import (
    PostgresV4OrdinaryParseCandidateSource,
    require_commissioning_recovery_scope,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.adapters.parsers.mineru_medium.http_remote_v4 import (
    MinerUHttpRemoteV4,
)
from disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4 import (
    MinerUHttpStagedV4,
)
from disclosure_anchor.adapters.parsers.mineru_medium.v4_initial_ingress import (
    MinerUV4InitialIngressFactory,
)
from disclosure_anchor.adapters.parsers.mineru_medium.v4_stage_input_resolver import (
    ProductionV4StageInputResolver,
)
from disclosure_anchor.adapters.parsers.pdf_text_observation import (
    observe_pdf_text_rectangles,
)
from disclosure_anchor.adapters.runtime.mineru_process_profile import (
    load_mineru_process_profile,
)
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import (
    verify_staged_process_profile_configuration,
)
from disclosure_anchor.adapters.security.provider_secret_cipher import (
    AesGcmProviderSecretCipher,
)
from disclosure_anchor.adapters.security.provider_secret_keyring import (
    load_provider_secret_keyring_from_settings,
)
from disclosure_anchor.adapters.semantics.runtime import build_semantic_runtime
from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.storage.atomic_publication_artifact_readiness_v4 import (
    FilesystemAtomicPublicationArtifactReadinessV4,
)
from disclosure_anchor.adapters.storage.immutable_artifact_store import (
    ImmutableArtifactStore,
)
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.provider_document_source import (
    ProviderDocumentFileSource,
)
from disclosure_anchor.adapters.storage.v4_source_observation import BoundedV4SourcePdfObserver
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions
from disclosure_anchor.application.ports.staged_new_work_v4 import validate_admission_document_ids
from disclosure_anchor.application.services.atomic_publication_request_builder_v4 import (
    ProductionAtomicPublicationRequestBuilderV4,
)
from disclosure_anchor.application.services.atomic_publication_request_factory_v4 import (
    RecoverableAtomicPublicationRequestFactoryV4,
)
from disclosure_anchor.application.services.provider_document_admission import (
    ProviderDocumentAdmission,
)
from disclosure_anchor.application.services.staged_coordinator_backend_v4 import (
    DurableStagedCoordinatorBackendV4,
)
from disclosure_anchor.application.services.staged_coordinator_persistence_v4 import (
    DurableStagedCoordinatorPersistenceV4,
    DurableV4ClaimGuard,
)
from disclosure_anchor.application.services.staged_ingress_v4 import (
    DurableStagedIngressV4,
)
from disclosure_anchor.application.services.staged_new_work_admission_v4 import (
    StagedV4NewWorkAdmitter,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    AdmissionOutcome,
    CoordinatorSnapshot,
    StagedParseCoordinator,
)
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.services.staged_v4_capacity import (
    staged_v4_coordinator_limits,
)
from disclosure_anchor.application.services.verify_v4_local_resource_cutover import verify_v4_local_resource_cutover
from disclosure_anchor.application.use_cases.prepare_and_publish_whole_document_v4 import (
    PrepareAndPublishWholeDocumentV4,
)
from disclosure_anchor.settings import Settings, load_staged_v4_settings


class _RecoveryOnlyAdmission:
    def admit_new(self, *, limit: int, available_credits: ResourceCreditVector) -> AdmissionOutcome:
        return AdmissionOutcome(work=(), backlog_exists=False)


@dataclass(slots=True)
class StagedWorkerV4Runtime:
    """Owned staged resources whose lifetime is the worker process."""

    coordinator: StagedParseCoordinator
    remote: MinerUHttpRemoteV4
    owner_identity: str
    worker_profile_sha256: str
    startup_guard: Callable[[], None]
    _startup_verified: bool = False

    def verify_startup(self) -> None:
        if not self._startup_verified:
            self.startup_guard()
            self._startup_verified = True

    def close(self) -> None:
        self.remote.close()


def build_staged_worker_v4_runtime(
    *,
    settings: Settings,
    engine: Engine,
    ownership_guard: Callable[[], None],
    admission_guard: Callable[[], None],
    process_scope_classes: tuple[str, ...] | None,
    progress: Callable[[CoordinatorSnapshot], None],
    publication_committed: Callable[[bool], None] = lambda _replaced: None,
    owner_identity: str | None = None,
    admission_document_ids: tuple[str, ...] | None = None,
    recovery_only: bool = False,
) -> StagedWorkerV4Runtime:
    """Compose exactly one seven-lane runtime after explicit mode selection."""

    if settings.worker_parse_execution_mode != "staged-v4":
        raise ValueError("staged V4 composition requires explicit staged-v4 mode")
    validate_admission_document_ids(admission_document_ids)
    if type(recovery_only) is not bool or (recovery_only and admission_document_ids is None):
        raise ValueError("recovery-only composition requires explicit bounded document scope")
    if (
        not callable(ownership_guard)
        or not callable(admission_guard)
        or not callable(progress)
        or not callable(publication_committed)
    ):
        raise ValueError("staged V4 process callbacks are invalid")
    if admission_document_ids is not None:
        original_ownership_guard = ownership_guard
        selected_document_ids = admission_document_ids

        def commissioning_ownership_guard() -> None:
            original_ownership_guard()
            require_commissioning_recovery_scope(engine, selected_document_ids)

        ownership_guard = commissioning_ownership_guard
        # Before profile/keyring/scratch construction and before any recovery
        # write. The same guard also checks each controller effect boundary.
        ownership_guard()
    staged = load_staged_v4_settings()
    loaded = load_mineru_process_profile(
        staged.process_profile_file,
        expected_sha256=staged.process_profile_sha256,
        expected_owner_uid=os.getuid(),
    )
    verify_staged_process_profile_configuration(settings, loaded.profile)
    runtime_identity = settings.disclosure_mineru_runtime_bundle_identity_sha256
    if (
        runtime_identity is None
        or loaded.profile.runtime_bundle_identity_sha256 != runtime_identity
    ):
        raise ValueError("staged V4 profile differs from configured runtime identity")
    if (
        settings.disclosure_mineru_api_url is None
        or settings.disclosure_mineru_inference_upstream_url is None
    ):
        raise ValueError("staged V4 requires the complete MinerU endpoint topology")

    worker_profile = staged.worker_profile(
        process_profile_sha256=loaded.profile.sha256,
        mac_preflight_workers=settings.worker_parse_concurrency,
        mac_finalize_workers=settings.worker_finalize_concurrency,
    )
    limits = staged_v4_coordinator_limits(loaded.profile, worker_profile=worker_profile)
    exact_owner = owner_identity or _new_owner_identity()
    paths = FileStorePathBuilder(settings)
    uow_factory = unit_of_work_factory(engine)
    provider_source = ProviderDocumentFileSource(
        paths,
        text_reader=observe_pdf_text_rectangles,
    )
    immutable_store = ImmutableArtifactStore(paths)
    persistence = DurableStagedCoordinatorPersistenceV4(
        uow_factory=uow_factory,
        limits=limits,
        owner_identity=exact_owner,
        process_guard=ownership_guard,
    )
    claim_guard = DurableV4ClaimGuard(uow_factory=uow_factory)
    remote = MinerUHttpRemoteV4(
        request_timeout_seconds=limits.max_stage_step_seconds,
        allow_task_submission=not recovery_only,
    )
    try:
        materialization = MinerUHttpStagedV4(
            scratch_root=(settings.disclosure_runtime_root / "staged_v4" / "scratch"),
            published_root=paths.data_path(Path()),
            transport=remote,
            clock=time.time,
        )
        readiness = FilesystemAtomicPublicationArtifactReadinessV4(
            paths=paths,
            immutable_store=immutable_store,
            output_promotion=materialization,
        )
        semantic = build_semantic_runtime(
            settings=settings,
            paths=paths,
            artifacts=ArtifactStore(paths),
        )
        new_publication_request = ProductionAtomicPublicationRequestBuilderV4(
            path_builder=paths,
            uow_factory=uow_factory,
            admission=ProviderDocumentAdmission(
                path_builder=paths,
                source=provider_source,
            ),
            semantic_router=semantic.router,
        )
        publication_requests = RecoverableAtomicPublicationRequestFactoryV4(
            readiness=readiness,
            new_request_builder=new_publication_request,
        )
        publisher = PrepareAndPublishWholeDocumentV4(
            uow_factory=uow_factory,
            publication_requests=publication_requests,
            readiness=readiness,
            publisher=PostgresAtomicWholeDocumentPublisherV4(engine=engine),
        )
        inputs = ProductionV4StageInputResolver(
            uow_factory=uow_factory,
            paths=paths,
            provider_source=provider_source,
            worker_profile=worker_profile,
        )
        parser_options = ParserOptions(
            backend="hybrid-http-client",
            effort="medium",
            image_analysis=False,
            timeout_seconds=settings.disclosure_parse_runaway_timeout_seconds,
            api_url=settings.disclosure_mineru_api_url,
            api_drain_timeout_seconds=(
                settings.disclosure_mineru_api_drain_timeout_seconds
            ),
            server_url=settings.disclosure_mineru_inference_upstream_url,
            http_request_concurrency=None,
            runtime_bundle_identity_sha256=runtime_identity,
        )
        ingress_factory = MinerUV4InitialIngressFactory(
            source=BoundedV4SourcePdfObserver(paths=paths),
            paths=paths,
            parser_identity=ParserIdentity(name="MinerU", version="3.4.4"),
            parser_options=parser_options,
            process_profile=loaded.profile,
            process_profile_exact_bytes=loaded.exact_bytes,
            worker_profile=worker_profile,
            result_lease_seconds=min(loaded.profile.task_retention_seconds, 3600),
            remote_runaway_seconds=(settings.disclosure_parse_runaway_timeout_seconds),
            archive_member_count_limit=staged.archive_member_count_limit,
            archive_uncompressed_byte_limit=(loaded.profile.temporary_disk_bytes_limit),
            max_retries=settings.disclosure_max_parse_retries,
            scope_classes=process_scope_classes,
        )
        ingress = DurableStagedIngressV4(uow_factory=uow_factory)
        new_work = StagedV4NewWorkAdmitter(
            prepared_claims=persistence,
            ordinary_candidates=PostgresV4OrdinaryParseCandidateSource(
                engine=engine,
                max_retries=settings.disclosure_max_parse_retries,
                scope_classes=process_scope_classes,
                admission_document_ids=admission_document_ids,
            ),
            ingress_factory=ingress_factory,
            ingress=ingress,
            candidate_page_size=limits.recovery_page_size,
            admission_guard=admission_guard,
            process_guard=ownership_guard,
            admission_document_ids=admission_document_ids,
        )
        backend = DurableStagedCoordinatorBackendV4(
            persistence=persistence,
            inputs=inputs,
            remote=remote,
            materialization=materialization,
            secret_cipher=AesGcmProviderSecretCipher(
                keyring=load_provider_secret_keyring_from_settings(settings)
            ),
            claim_guard=claim_guard,
            publisher=publisher,
            poll_seconds=worker_profile.provider_poll_milliseconds / 1000,
            new_work_admitter=_RecoveryOnlyAdmission() if recovery_only else new_work,
            publication_committed=publication_committed,
        )
        return StagedWorkerV4Runtime(
            coordinator=StagedParseCoordinator(
                backend=backend,
                limits=limits,
                progress=progress,
                process_guard=ownership_guard,
                admission_observer=None if recovery_only else new_work,
            ),
            remote=remote,
            owner_identity=exact_owner,
            worker_profile_sha256=worker_profile.sha256,
            startup_guard=lambda: verify_v4_local_resource_cutover(
                uow_factory=uow_factory, inspector=materialization,
                ownership_guard=ownership_guard,
            ),
        )
    except BaseException:
        remote.close()
        raise


def _new_owner_identity() -> str:
    return f"staged-v4-{os.getpid()}-{secrets.token_hex(16)}"


__all__ = ["StagedWorkerV4Runtime", "build_staged_worker_v4_runtime"]
