"""Scratch-PostgreSQL, real-filesystem, fake-HTTP staged V4 closure."""

from __future__ import annotations

from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import io
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch
import zipfile

import httpx
import pypdfium2 as pdfium
import sqlalchemy as sa

from disclosure_anchor.adapters.db.postgres.atomic_document_publisher_v4 import (
    PostgresAtomicWholeDocumentPublisherV4,
)
from disclosure_anchor.adapters.db.postgres.staged_new_work_v4 import PostgresV4OrdinaryParseCandidateSource
from disclosure_anchor.adapters.db.postgres.unit_of_work import (
    SqlAlchemyUnitOfWork,
    unit_of_work_factory,
)
from disclosure_anchor.adapters.parsers.mineru_medium.http_remote_v4 import (
    MinerUHttpRemoteV4,
)
from disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4 import (
    MinerUHttpStagedV4,
)
from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import (
    canonical_result_owner_v2,
    canonical_client_submit_key_v2,
)
from disclosure_anchor.adapters.parsers.mineru_medium.v4_initial_ingress import (
    MinerUV4InitialIngressFactory,
)
from disclosure_anchor.adapters.parsers.mineru_medium.v4_stage_input_resolver import (
    ProductionV4StageInputResolver,
)
from disclosure_anchor.adapters.security.provider_secret_cipher import (
    AesGcmProviderSecretCipher,
)
from disclosure_anchor.adapters.security.provider_secret_keyring import (
    StaticProviderSecretKeyring,
)
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
from disclosure_anchor.adapters.storage.published_parser_output_verifier_v4 import (
    PublishedParserOutputVerifierV4,
)
from disclosure_anchor.adapters.storage.v4_source_observation import BoundedV4SourcePdfObserver
from disclosure_anchor.application.contracts.mineru_process_profile import (
    encode_mineru_process_profile,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_ROUTE_RECEIPT_VERSION,
)
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
    AtomicPublicationCommitResponseLost,
)
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    V4SecretRewrap,
)
from disclosure_anchor.application.ports.staged_new_work_v4 import (
    V4OrdinaryParseCandidate,
)
from disclosure_anchor.application.services.atomic_publication_request_builder_v4 import (
    ProductionAtomicPublicationRequestBuilderV4,
)
from disclosure_anchor.application.services.atomic_publication_request_factory_v4 import (
    RecoverableAtomicPublicationRequestFactoryV4,
)
from disclosure_anchor.application.services.provider_document_admission import (
    ProviderDocumentAdmission,
)
from disclosure_anchor.application.services.semantic_router import (
    SemanticRouteBatchResult,
)
from disclosure_anchor.application.services.staged_coordinator_backend_v4 import (
    DurableStagedCoordinatorBackendV4,
)
from disclosure_anchor.application.services.staged_coordinator_persistence_v4 import (
    DurableStagedCoordinatorPersistenceV4,
    DurableV4ClaimGuard,
)
from disclosure_anchor.application.services.staged_ingress_v4 import DurableStagedIngressV4
from disclosure_anchor.application.services.verify_v4_local_resource_cutover import verify_v4_local_resource_cutover
from disclosure_anchor.application.ports.staged_provider_parser import V4ResourceOwnershipError
from disclosure_anchor.application.services.staged_new_work_admission_v4 import StagedV4NewWorkAdmitter
from disclosure_anchor.application.services.staged_parse_coordinator import (
    StagedParseCoordinator, CoordinatorTerminal,
)
from disclosure_anchor.application.services.staged_v4_capacity import (
    staged_v4_coordinator_limits,
)
from disclosure_anchor.application.use_cases.prepare_and_publish_whole_document_v4 import (
    PrepareAndPublishWholeDocumentV4,
)
from disclosure_anchor.domain import ids
from disclosure_anchor.settings import load_settings
from tests.integration._support import engine_or_skip
from tests.unit._semantic_routes import _fallback_receipt
from tests.unit.test_mineru_medium_artifacts import _write_bundle
from tests.unit.test_mineru_process_profile import _profile
from tests.unit.test_settings import _env, _mineru_topology


class _StageGuard:
    def checkpoint(self) -> None:
        return None

    def remaining_seconds(self) -> float:
        return 60.0


class _V4SemanticRouter:
    def route(self, *, drafts: tuple[Any, ...], **_kwargs: Any) -> Any:
        receipts = tuple(
            replace(
                _fallback_receipt(index),
                contract_version=SEMANTIC_ROUTE_RECEIPT_VERSION,
                semantic_keys=(),
                evidence=(),
            )
            for index in range(len(drafts))
        )
        return SemanticRouteBatchResult(units=drafts, receipts=receipts)


class _CommitResponseLostUnitOfWork:
    """Raise after one selected successful scratch-DB commit."""

    def __init__(
        self,
        delegate: SqlAlchemyUnitOfWork,
        lose_next: list[bool],
    ) -> None:
        self._delegate = delegate
        self._lose_next = lose_next

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def __enter__(self) -> _CommitResponseLostUnitOfWork:
        self._delegate.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        self._delegate.__exit__(exc_type, exc, traceback)

    def commit(self) -> None:
        self._delegate.commit()
        if self._lose_next[0]:
            self._lose_next[0] = False
            raise RuntimeError("simulated database commit response loss")


class _LoseFirstPublicationResponse:
    def __init__(self, delegate: PostgresAtomicWholeDocumentPublisherV4) -> None:
        self._delegate = delegate
        self.lost = False

    def commit_whole_document(self, *args: Any, **kwargs: Any) -> Any:
        winner = self._delegate.commit_whole_document(*args, **kwargs)
        if not self.lost:
            self.lost = True
            raise AtomicPublicationCommitResponseLost(
                "simulated transaction-P response loss"
            )
        return winner

    def reload_commit_winner(self, *args: Any, **kwargs: Any) -> Any:
        return self._delegate.reload_commit_winner(*args, **kwargs)


class _FakeMinerU:
    def __init__(
        self,
        *,
        attempt_id: str,
        fence_identity: str,
        client_submit_key: str,
        source_pdf: bytes,
        artifact: bytes,
    ) -> None:
        self.attempt_id = attempt_id
        self.fence_identity = fence_identity
        self.client_submit_key = client_submit_key
        self.source_pdf = source_pdf
        self.artifact = artifact
        self.task_id = "task-m5d-e2e"
        self.artifact_digest = hashlib.sha256(artifact).hexdigest()
        self.artifact_owner = canonical_result_owner_v2(
            task_id=self.task_id,
            artifact_sha256=self.artifact_digest,
            artifact_byte_count=len(artifact),
        )
        self.accepted = False
        self.task_posts = 0
        self.result_gets = 0
        self.lease_posts = 0
        self.ack_posts = 0
        self.calls: list[str] = []
        self.cleanup_probe: Callable[[], bool] = lambda: False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        route = f"{request.method} {request.url.path}"
        self.calls.append(route)
        if request.method == "GET" and request.url.path.startswith(
            "/tasks/by-idempotency/"
        ):
            if not self.accepted:
                return httpx.Response(404, json={"detail": "Task not found"})
            return httpx.Response(200, json=self._task_payload("pending"))
        if request.method == "POST" and request.url.path == "/tasks":
            body = request.read()
            if self.client_submit_key.encode() not in body or self.source_pdf not in body:
                raise AssertionError("fake provider received a drifted submission")
            self.task_posts += 1
            self.accepted = True
            # The provider accepted the exact request, but the HTTP response was lost.
            raise httpx.ReadTimeout("simulated POST response loss", request=request)
        if request.method == "GET" and request.url.path == f"/tasks/{self.task_id}":
            return httpx.Response(200, json=self._task_payload("completed"))
        if request.method == "POST" and request.url.path == (
            f"/tasks/{self.task_id}/lease"
        ):
            self.lease_posts += 1
            return httpx.Response(
                200,
                json={
                    "schema": "mineru-task-protocol.v2",
                    "task_id": self.task_id,
                    "lease_until_unix": 2_000.0,
                },
            )
        if request.method == "GET" and request.url.path == (
            f"/tasks/{self.task_id}/result"
        ):
            self.result_gets += 1
            return httpx.Response(
                200,
                content=self.artifact,
                headers={
                    "Content-Type": "application/zip",
                    "Content-Length": str(len(self.artifact)),
                    "X-MinerU-Result-SHA256": self.artifact_digest,
                    "X-MinerU-Result-Owner": self.artifact_owner,
                },
            )
        if request.method == "POST" and request.url.path == (
            f"/tasks/{self.task_id}/ack"
        ):
            if not self.cleanup_probe():
                raise AssertionError("provider ACK happened before local cleanup")
            self.ack_posts += 1
            return httpx.Response(
                200,
                content=(
                    b'{"schema":"mineru-task-protocol.v2","task_id":'
                    b'"task-m5d-e2e","status":"consumed"}'
                ),
            )
        raise AssertionError(f"unexpected fake MinerU request: {route}")

    def _task_payload(self, status: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "task_id": self.task_id,
            "status": status,
            "status_url": f"/tasks/{self.task_id}",
            "result_url": f"/tasks/{self.task_id}/result",
            "task_protocol_schema": "mineru-task-protocol.v2",
            "idempotency_key": self.client_submit_key,
            "attempt_identity": self.attempt_id,
            "fence_identity": self.fence_identity,
            "protocol_state": status,
            "error": None,
        }
        if status == "completed":
            payload.update(
                {
                    "result_artifact_schema": "mineru-retained-result.v1",
                    "result_artifact_sha256": self.artifact_digest,
                    "result_artifact_bytes": len(self.artifact),
                    "result_artifact_owner": self.artifact_owner,
                }
            )
        return payload


def _official_result_zip(source_pdf_sha256: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_bundle(root)
        (root / "images" / "owner.jpg").write_bytes(b"\xff\xd8\xffowner-crop")
        (root / "images" / "continuation.jpg").write_bytes(
            b"\xff\xd8\xffcontinuation-crop"
        )
        content_list = next(root.glob("*_content_list.json"))
        fixture_stem = content_list.name.removesuffix("_content_list.json")
        source_stem = source_pdf_sha256.replace("sha256:", "sha256_", 1)
        for path in tuple(root.iterdir()):
            if path.is_file() and path.name.startswith(fixture_stem):
                path.rename(
                    path.with_name(source_stem + path.name[len(fixture_stem) :])
                )
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
        return output.getvalue()


class StagedV4EndToEndIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.tempdir = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.root = Path(self.tempdir.name)
        with patch.dict(
            os.environ,
            {**_env(self.root), **_mineru_topology()},
            clear=True,
        ):
            self.settings = load_settings()
        self.paths = FileStorePathBuilder(self.settings)
        self.store = ImmutableArtifactStore(self.paths)
        self.source = ProviderDocumentFileSource(
            self.paths,
            text_reader=lambda _path, *, document: (),
            page_counter=lambda _path: 2,
        )
        self.company_id = "co_" + ids.new_ulid()
        self.security_id = "sec_" + ids.new_ulid()
        self.document_id = str(ids.new_document_id())
        self.extra_document_ids: list[str] = []
        self.provider_document_id = "m5d-" + ids.new_ulid()
        # A real readable two-page PDF lets the composed coordinator exercise
        # the actual bounded observer child. Fake HTTP/artifact content remains
        # a mechanics witness, not a source/table-quality qualification.
        with pdfium.PdfDocument.new() as pdf:
            for _ in range(2):
                page = pdf.new_page(100, 100)
                page.close()
            output = io.BytesIO()
            pdf.save(output)
            self.source_bytes = output.getvalue()
        self.source_sha256 = "sha256:" + hashlib.sha256(
            self.source_bytes
        ).hexdigest()
        self.source_relpath = self.paths.raw_document_relpath(
            provider="cninfo",
            security_code="000001",
            year=2026,
            provider_document_id=self.provider_document_id,
            raw_file_hash=self.source_sha256,
        )
        source_path = self.paths.data_path(self.source_relpath)
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(self.source_bytes)
        source_path.chmod(0o600)
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO disclosure_core.company (company_id,legal_name) "
                    "VALUES (:company,'M5d Integration Fixture')"
                ),
                {"company": self.company_id},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO disclosure_core.security "
                    "(security_id,company_id,security_code,exchange) VALUES "
                    "(:security,:company,'000001','SZSE')"
                ),
                {"security": self.security_id, "company": self.company_id},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO disclosure_core.document "
                    "(document_id,security_id,provider,provider_document_id,"
                    "raw_file_relpath,raw_file_hash,status) VALUES "
                    "(:document,:security,'cninfo',:provider_document,"
                    ":source_relpath,:source_sha,'registered')"
                ),
                {
                    "document": self.document_id,
                    "security": self.security_id,
                    "provider_document": self.provider_document_id,
                    "source_relpath": self.source_relpath.as_posix(),
                    "source_sha": self.source_sha256,
                },
            )
        self.remote: MinerUHttpRemoteV4 | None = None
        self.processing_run_id: str | None = None

    def tearDown(self) -> None:
        if self.remote is not None:
            self.remote.close()
        try:
            with self.engine.begin() as conn:
                conn.exec_driver_sql(
                    "TRUNCATE TABLE disclosure_ops.remote_parse_attempt CASCADE"
                )
                for extra_document in self.extra_document_ids:
                    conn.execute(sa.text(
                        "DELETE FROM disclosure_ops.outbox_event WHERE document_id=:document"
                    ), {"document": extra_document})
                    conn.execute(sa.text(
                        "DELETE FROM disclosure_core.processing_run WHERE document_id=:document"
                    ), {"document": extra_document})
                    conn.execute(sa.text(
                        "DELETE FROM disclosure_core.document WHERE document_id=:document"
                    ), {"document": extra_document})
                conn.execute(
                    sa.text(
                        "DELETE FROM disclosure_ops.durable_publish_base "
                        "WHERE document_id=:document"
                    ),
                    {"document": self.document_id},
                )
                conn.execute(
                    sa.text(
                        "DELETE FROM disclosure_ops.outbox_event "
                        "WHERE document_id=:document"
                    ),
                    {"document": self.document_id},
                )
                conn.execute(
                    sa.text(
                        "DELETE FROM disclosure_core.document_unit "
                        "WHERE document_id=:document"
                    ),
                    {"document": self.document_id},
                )
                conn.execute(
                    sa.text(
                        "DELETE FROM disclosure_core.processing_run "
                        "WHERE document_id=:document"
                    ),
                    {"document": self.document_id},
                )
                conn.execute(
                    sa.text(
                        "DELETE FROM disclosure_core.document "
                        "WHERE document_id=:document"
                    ),
                    {"document": self.document_id},
                )
                conn.execute(
                    sa.text(
                        "DELETE FROM disclosure_core.security "
                        "WHERE security_id=:security"
                    ),
                    {"security": self.security_id},
                )
                conn.execute(
                    sa.text(
                        "DELETE FROM disclosure_core.company "
                        "WHERE company_id=:company"
                    ),
                    {"company": self.company_id},
                )
        finally:
            self.engine.dispose()
            self.tempdir.cleanup()

    def test_restart_rewrap_response_loss_cleanup_and_atomic_publish_close(self) -> None:
        self._run_closure(coordinator_owned=False)

    def test_ordinary_admission_and_actual_coordinator_publish_with_response_loss(self) -> None:
        self._run_closure(coordinator_owned=True)

    def test_malformed_first_source_is_durably_rejected_good_next_publishes_and_restart_is_empty(self) -> None:
        self._run_closure(coordinator_owned=True, malformed_first=True)

    def test_markerless_invalid_output_keeps_pg_credits_and_blocks_ack_across_three_boots(self) -> None:
        self._run_closure(coordinator_owned=True, ownership_failure=True)

    def _run_closure(self, *, coordinator_owned: bool, malformed_first: bool = False, ownership_failure: bool = False) -> None:
        profile = _profile()
        limits = staged_v4_coordinator_limits(
            profile,
            worker_profile=StagedWorkerProfileV4(profile.sha256, 1, 1),
        )
        options = ParserOptions(
            method="auto",
            backend="hybrid-http-client",
            language="ch",
            formula=True,
            table=True,
            effort="medium",
            image_analysis=False,
            timeout_seconds=3_600,
            api_url="https://mineru.invalid",
            api_drain_timeout_seconds=3_600,
            server_url="http://mineru-openai-server:30000/v1",
            http_request_concurrency=7,
            runtime_bundle_identity_sha256=profile.runtime_bundle_identity_sha256,
        )
        attempt_id = ids.new_id("rpa")
        fence_identity = ids.new_id("fence")
        self.processing_run_id = str(ids.new_processing_run_id())
        run_ids = [self.processing_run_id]
        rejected_run_id = str(ids.new_processing_run_id())
        if malformed_first:
            # Sort before the good candidate, independently of wall-clock IDs.
            bad_document = "doc_000_" + ids.new_ulid()
            self.assertLess(bad_document, self.document_id)
            self.extra_document_ids.append(bad_document)
            bad_bytes = b"%PDF-1.7\ninvalid archived document\n%%EOF\n"
            bad_sha = "sha256:" + hashlib.sha256(bad_bytes).hexdigest()
            bad_relpath = self.paths.raw_document_relpath(
                provider="cninfo", security_code="000001", year=2026,
                provider_document_id="bad-" + self.provider_document_id, raw_file_hash=bad_sha,
            )
            bad_path = self.paths.data_path(bad_relpath)
            bad_path.parent.mkdir(parents=True, exist_ok=True)
            bad_path.write_bytes(bad_bytes)
            bad_path.chmod(0o600)
            with self.engine.begin() as conn:
                conn.execute(sa.text(
                    "INSERT INTO disclosure_core.document "
                    "(document_id,security_id,provider,provider_document_id,raw_file_relpath,raw_file_hash,status) "
                    "VALUES (:document,:security,'cninfo',:provider_document,:relpath,:sha,'registered')"
                ), {"document": bad_document, "security": self.security_id,
                    "provider_document": "bad-" + self.provider_document_id,
                    "relpath": bad_relpath.as_posix(), "sha": bad_sha})
            run_ids.insert(0, rejected_run_id)
        run_id_sequence = iter(run_ids)
        ingress = MinerUV4InitialIngressFactory(
            source=BoundedV4SourcePdfObserver(paths=self.paths),
            paths=self.paths,
            parser_identity=ParserIdentity(name="MinerU", version="3.4.4"),
            parser_options=options,
            process_profile=profile,
            process_profile_exact_bytes=encode_mineru_process_profile(profile),
            worker_profile=StagedWorkerProfileV4(profile.sha256, 1, 1),
            result_lease_seconds=300,
            remote_runaway_seconds=3_600,
            archive_member_count_limit=8_192,
            archive_uncompressed_byte_limit=profile.decoded_payload_bytes_limit,
            max_retries=3,
            scope_classes=None,
            utc_now=lambda: datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
            attempt_id_factory=lambda: attempt_id,
            fence_id_factory=lambda: fence_identity,
            processing_run_id_factory=lambda: next(run_id_sequence),
            outbox_event_id_factory=lambda: ids.new_id("evt"),
        )
        ingress_loss = [True]

        def ingress_uow() -> Any:
            return _CommitResponseLostUnitOfWork(
                SqlAlchemyUnitOfWork(engine=self.engine), ingress_loss
            )

        if not coordinator_owned:
            command = ingress.build(
                V4OrdinaryParseCandidate(
                    document_id=self.document_id,
                    provider="cninfo",
                    provider_document_id=self.provider_document_id,
                    security_id=self.security_id,
                    security_code="000001",
                    raw_file_relpath=self.source_relpath.as_posix(),
                    raw_file_hash=self.source_sha256,
                    archived_raw_byte_count=len(self.source_bytes),
                ),
                source_observation=self.source.observe_source_pdf(self.source_relpath),
                available_credits=limits.credits,
            )
            authority = DurableStagedIngressV4(uow_factory=ingress_uow).execute(
                command, write_guard=lambda: None,
            )
            self.assertFalse(ingress_loss[0])
            self.assertEqual(authority.state, "prepared")


        artifact = _official_result_zip(self.source_sha256)
        fake = _FakeMinerU(
            attempt_id=attempt_id,
            fence_identity=fence_identity,
            client_submit_key=canonical_client_submit_key_v2(
                source_pdf_sha256=self.source_sha256, attempt_identity=attempt_id,
                fence_identity=fence_identity,
                submission_epoch_unix=int(datetime(2026, 9, 4, 12, tzinfo=UTC).timestamp()),
            ),
            source_pdf=self.source_bytes,
            artifact=artifact,
        )
        self.remote = MinerUHttpRemoteV4(
            transport=httpx.MockTransport(fake),
            token_factory=lambda count: b"t" * count,
            wall_clock=lambda: 1_000.0,
            request_timeout_seconds=30.0,
        )
        scratch_root = self.settings.disclosure_runtime_root / "staged_v4" / "scratch"
        materialization = MinerUHttpStagedV4(
            scratch_root=scratch_root,
            published_root=self.paths.data_path(Path()),
            transport=self.remote,
            clock=lambda: 1_000.0,
        )
        normal_uow = unit_of_work_factory(self.engine)
        if ownership_failure:
            injected = False

            def lose_promoted_receipt(phase: str) -> None:
                nonlocal injected
                if phase != "after_promotion" or injected:
                    return
                injected = True
                with normal_uow() as uow:
                    current = uow.remote_parse_v4.load(attempt_id)
                intent = next(item.value for item in current.evidence if item.kind == "materialization_intent")
                output = scratch_root / intent.output_relpath
                # Reproduce process exit after marker removal, before local receipt,
                # with invalid output bytes. No authority is invented on restart.
                (output / Path(intent.staging_marker_relpath).name).unlink()
                victim = output / intent.output_manifest_relpath
                victim.write_bytes(b"{torn")
                raise RuntimeError("exit before local receipt response")

            materialization._fault_hook = lose_promoted_receipt
        persistence_loss = [False]

        def persistence_uow() -> Any:
            return _CommitResponseLostUnitOfWork(
                SqlAlchemyUnitOfWork(engine=self.engine), persistence_loss
            )

        inputs = ProductionV4StageInputResolver(
            uow_factory=normal_uow,
            paths=self.paths,
            provider_source=self.source,
            worker_profile=StagedWorkerProfileV4(profile.sha256, 1, 1),
        )
        readiness = FilesystemAtomicPublicationArtifactReadinessV4(
            paths=self.paths,
            immutable_store=self.store,
            output_promotion=materialization,
        )
        request_builder = ProductionAtomicPublicationRequestBuilderV4(
            path_builder=self.paths,
            uow_factory=normal_uow,
            admission=ProviderDocumentAdmission(
                path_builder=self.paths,
                source=self.source,
            ),
            semantic_router=_V4SemanticRouter(),  # type: ignore[arg-type]
        )
        publication_transport = _LoseFirstPublicationResponse(
            PostgresAtomicWholeDocumentPublisherV4(engine=self.engine)
        )
        publisher = PrepareAndPublishWholeDocumentV4(
            uow_factory=normal_uow,
            publication_requests=RecoverableAtomicPublicationRequestFactoryV4(
                readiness=readiness,
                new_request_builder=request_builder,
            ),
            readiness=readiness,
            publisher=publication_transport,  # type: ignore[arg-type]
        )
        old_cipher = AesGcmProviderSecretCipher(
            keyring=StaticProviderSecretKeyring(
                primary_kek_id="kek-old",
                keks={"kek-old": b"o" * 32},
            )
        )

        def backend(owner: str, cipher: AesGcmProviderSecretCipher) -> Any:
            persistence = DurableStagedCoordinatorPersistenceV4(
                uow_factory=persistence_uow,
                limits=limits,
                owner_identity=owner,
            )
            return DurableStagedCoordinatorBackendV4(
                persistence=persistence,
                inputs=inputs,
                remote=self.remote,
                materialization=materialization,
                secret_cipher=cipher,
                claim_guard=DurableV4ClaimGuard(uow_factory=normal_uow),
                publisher=publisher,
                poll_seconds=1.0,
                wall_clock=lambda: 1_000.0,
                new_work_admitter=StagedV4NewWorkAdmitter(
                    prepared_claims=persistence,
                    ordinary_candidates=PostgresV4OrdinaryParseCandidateSource(
                        engine=self.engine, max_retries=3, scope_classes=None,
                    ),
                    ingress_factory=ingress,
                    ingress=DurableStagedIngressV4(uow_factory=ingress_uow),
                    candidate_page_size=limits.recovery_page_size,
                ) if coordinator_owned else None,
            )

        if ownership_failure:
            held = None
            for boot in range(3):
                composed = backend(f"ownership-restart-{boot}", old_cipher)
                observed = []
                result = StagedParseCoordinator(
                    backend=composed, limits=limits, progress=observed.append,
                    admission_observer=composed._new_work_admitter,
                ).run()
                self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
                self.assertEqual(result.completed, 0)
                with normal_uow() as uow:
                    current = uow.remote_parse_v4.load(attempt_id)
                self.assertEqual(current.state, "materializing")
                self.assertNotIn("failure_receipt", {item.kind for item in current.evidence})
                self.assertNotIn("local_materialization_receipt", {item.kind for item in current.evidence})
                self.assertIsNone(current.publication_winner)
                self.assertGreater(current.checkpoint.held_resource_credit.temp_disk_bytes, 0)
                self.assertEqual(result.credits_in_use, current.checkpoint.held_resource_credit)
                if held is None:
                    held = result.credits_in_use
                self.assertEqual(result.credits_in_use, held)
                self.assertEqual(fake.ack_posts, 0)
                if boot > 0:
                    self.assertTrue(any("V4ResourceOwnershipError" in error for error in result.errors))
                    intent = next(item.value for item in current.evidence if item.kind == "materialization_intent")
                    self.assertFalse((scratch_root/intent.output_relpath).exists())
                    self.assertEqual((scratch_root/intent.staging_relpath/intent.output_manifest_relpath).read_bytes(), b"{torn")
                    self.assertFalse(materialization._quarantine_path(intent).exists())
                with self.engine.begin() as conn:
                    conn.execute(sa.text(
                        "UPDATE disclosure_ops.remote_parse_attempt SET claim_lease_until=:expired WHERE attempt_id=:attempt"
                    ), {"attempt": attempt_id, "expired": datetime.now(UTC)-timedelta(seconds=1)})
            self.assertEqual(fake.task_posts, 1)
            self.assertFalse(publication_transport.lost)
            return

        if coordinator_owned:
            def cleanup_probe() -> bool:
                with normal_uow() as uow:
                    current = uow.remote_parse_v4.load(attempt_id)
                current_intent = next(item.value for item in current.evidence
                                      if item.kind == "materialization_intent")
                return current.state == "ack_pending" and all(
                    not (scratch_root / relpath).exists() for relpath in (
                        current.reservation.snapshot_relpath,
                        current_intent.spool_relpath, current_intent.output_relpath,
                    )
                )

            fake.cleanup_probe = cleanup_probe
            snapshots = []
            composed_backend = backend("m5d-coordinator-owner", old_cipher)
            with (
                patch.object(composed_backend, "_authority", wraps=composed_backend._authority) as stages,
                patch.object(inputs, "_identity", wraps=inputs._identity) as spec_reads,
            ):
                result = StagedParseCoordinator(
                    backend=composed_backend,
                    limits=limits, progress=snapshots.append,
                    admission_observer=composed_backend._new_work_admitter,
                ).run()
                self.assertGreaterEqual(stages.call_count, 9)
                self.assertEqual(spec_reads.call_count, stages.call_count)
            self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
            self.assertEqual(result.admitted, 1)
            self.assertEqual(result.completed, 1)
            self.assertEqual(result.errors, ())
            self.assertTrue(any(sum(dict(s.in_flight).values()) > 0 for s in snapshots))
            self.assertFalse(ingress_loss[0])
            self.assertTrue(publication_transport.lost)
            with normal_uow() as uow:
                ack_authority = uow.remote_parse_v4.load(attempt_id)
            final_state = ack_authority.state
            intent = next(item.value for item in ack_authority.evidence
                          if item.kind == "materialization_intent")
            if malformed_first:
                with self.engine.connect() as conn:
                    rejected = conn.execute(sa.text(
                        "SELECT status,error FROM disclosure_core.processing_run WHERE processing_run_id=:run"
                    ), {"run": rejected_run_id}).mappings().one()
                    self.assertEqual(rejected["status"], "failed")
                    self.assertEqual(rejected["error"]["error_code"], "source_pdf_invalid_format")
                    self.assertFalse(rejected["error"]["retryable"])
                    self.assertEqual(conn.execute(sa.text(
                        "SELECT count(*) FROM disclosure_ops.remote_parse_attempt WHERE processing_run_id=:run"
                    ), {"run": rejected_run_id}).scalar_one(), 0)
                    self.assertEqual(conn.execute(sa.text(
                        "SELECT count(*) FROM disclosure_ops.outbox_event WHERE processing_run_id=:run"
                    ), {"run": rejected_run_id}).scalar_one(), 2)
                restarted_backend = backend("m5d-after-rejection-restart", old_cipher)
                restarted = StagedParseCoordinator(
                    backend=restarted_backend, limits=limits,
                    admission_observer=restarted_backend._new_work_admitter,
                ).run()
                self.assertEqual(restarted.terminal, CoordinatorTerminal.QUIESCENT)
                self.assertEqual((restarted.admitted, restarted.completed), (0, 0))
        else:
            first = backend("m5d-owner-1", old_cipher)
            candidate = first.list_recoverable(after_attempt_id=None, limit=10)[0]
            work = first.claim_recovery(candidate)
            guard = _StageGuard()
            work = first.prepare_remote_io(
                work,
                credit_allowance=limits.credits,
                stage_guard=guard,  # type: ignore[arg-type]
            )
            work = first.run_remote(
                work,
                credit_allowance=limits.credits,
                stage_guard=guard,  # type: ignore[arg-type]
            )
            self.assertEqual(work.state, "submitted")
            self.assertEqual(fake.task_posts, 1)

            rotated_cipher = AesGcmProviderSecretCipher(
                keyring=StaticProviderSecretKeyring(
                    primary_kek_id="kek-new",
                    keks={"kek-old": b"o" * 32, "kek-new": b"n" * 32},
                )
            )
            with normal_uow() as uow:
                submitted = uow.remote_parse_v4.load(attempt_id)
                rewrapped = rotated_cipher.rewrap(submitted.secret_history[-1])
                history = uow.remote_parse_v4.rewrap_secret(
                    V4SecretRewrap(
                        attempt_id=attempt_id,
                        fence_identity=fence_identity,
                        rewrapped=rewrapped,
                    )
                )
                uow.commit()
            self.assertEqual(tuple(item.encryption_revision for item in history), (1, 2))

            # Simulate a process dying with a live submission.  The old claim is
            # expired, then a boot-unique owner reconstructs every input from DB/files.
            with self.engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "UPDATE disclosure_ops.remote_parse_attempt SET "
                        "claim_lease_until=:expired WHERE attempt_id=:attempt"
                    ),
                    {
                        "attempt": attempt_id,
                        "expired": datetime.now(UTC) - timedelta(seconds=1),
                    },
                )
            restarted = backend("m5d-owner-2", rotated_cipher)
            candidate = restarted.list_recoverable(after_attempt_id=None, limit=10)[0]
            work = restarted.claim_recovery(candidate)

            # The remote-terminal successor commits, but its response is lost;
            # exact reconciliation must still return the one durable successor.
            persistence_loss[0] = True
            work = restarted.run_remote(
                work,
                credit_allowance=limits.credits,
                stage_guard=guard,  # type: ignore[arg-type]
            )
            self.assertFalse(persistence_loss[0])
            self.assertEqual(work.state, "remote_terminal")
            work = restarted.prepare_local_io(
                work,
                credit_allowance=limits.credits,
                stage_guard=guard,  # type: ignore[arg-type]
            )
            work = restarted.run_local(
                work,
                credit_allowance=limits.credits,
                stage_guard=guard,  # type: ignore[arg-type]
            )
            if work.state != "local_materialized":
                with normal_uow() as uow:
                    failed = uow.remote_parse_v4.load(attempt_id)
                failure = next(
                    item.value
                    for item in reversed(failed.evidence)
                    if item.kind == "failure_receipt"
                )
                self.fail(
                    "materialization unexpectedly failed: "
                    f"{failure.error_class}: {failure.error_code}: {failure.message}"
                )
            work = restarted.commit(
                work,
                credit_allowance=limits.credits,
                stage_guard=guard,  # type: ignore[arg-type]
            )
            self.assertTrue(publication_transport.lost)
            self.assertEqual(work.state, "publish_committed")
            work = restarted.cleanup(
                work,
                credit_allowance=limits.credits,
                stage_guard=guard,  # type: ignore[arg-type]
            )
            work = restarted.cleanup(
                work,
                credit_allowance=limits.credits,
                stage_guard=guard,  # type: ignore[arg-type]
            )
            self.assertEqual(work.state, "ack_pending")

            with normal_uow() as uow:
                ack_authority = uow.remote_parse_v4.load(attempt_id)
            intent = next(
                item.value
                for item in ack_authority.evidence
                if item.kind == "materialization_intent"
            )
            cleanup_paths = (
                scratch_root / ack_authority.reservation.snapshot_relpath,
                scratch_root / intent.spool_relpath,
                scratch_root / intent.output_relpath,
            )
            fake.cleanup_probe = lambda: all(not path.exists() for path in cleanup_paths)
            work = restarted.acknowledge(work, stage_guard=guard)  # type: ignore[arg-type]

            final_state = work.state

        self.assertEqual(final_state, "acked")
        receipt = next(item.value for item in ack_authority.evidence
                       if item.kind == "local_materialization_receipt")
        published_relpath = intent.provider_envelope_context.parser_artifact_root_relpath
        self.assertFalse((scratch_root / "parser_artifacts").exists())
        self.assertTrue(self.paths.data_path(Path(published_relpath)).is_dir())
        PublishedParserOutputVerifierV4(self.paths).verify_published(
            published_relpath=published_relpath,
            expected_inventory_sha256=receipt.output_files_sha256,
            expected_file_count=receipt.output_file_count,
            expected_byte_count=receipt.output_byte_count,
        )
        rebuilt = self.source.rebuild_provider_document(
            Path(published_relpath), source_pdf_sha256=self.source_sha256,
        )
        self.assertEqual(rebuilt.source_pdf_sha256, self.source_sha256)
        self.assertEqual(fake.task_posts, 1)
        self.assertEqual(fake.result_gets, 1)
        self.assertEqual(fake.lease_posts, 2)
        self.assertEqual(fake.ack_posts, 1)
        verify_v4_local_resource_cutover(
            uow_factory=normal_uow, inspector=materialization, ownership_guard=lambda: None,
            page_size=1,
        )
        # A completed attempt cannot hide a legacy detached residual at next boot.
        legacy_residual = materialization._quarantine_path(intent)
        materialization._ensure_parent(legacy_residual)
        legacy_residual.mkdir(mode=0o700)
        with self.assertRaisesRegex(V4ResourceOwnershipError, "legacy detached"):
            verify_v4_local_resource_cutover(
                uow_factory=normal_uow, inspector=materialization, ownership_guard=lambda: None,
                page_size=1,
            )
        self.assertLess(
            fake.calls.index(f"GET /tasks/{fake.task_id}/result"),
            fake.calls.index(f"POST /tasks/{fake.task_id}/ack"),
        )
        with self.engine.begin() as conn:
            final = conn.execute(
                sa.text(
                    "SELECT state,is_current FROM disclosure_ops.remote_parse_attempt "
                    "WHERE attempt_id=:attempt"
                ),
                {"attempt": attempt_id},
            ).mappings().one()
            document = conn.execute(
                sa.text(
                    "SELECT status,current_processing_run_id FROM "
                    "disclosure_core.document WHERE document_id=:document"
                ),
                {"document": self.document_id},
            ).mappings().one()
            run = conn.execute(
                sa.text(
                    "SELECT status,is_active FROM disclosure_core.processing_run "
                    "WHERE processing_run_id=:run"
                ),
                {"run": self.processing_run_id},
            ).mappings().one()
            unit_count = conn.execute(
                sa.text(
                    "SELECT count(*) FROM disclosure_core.document_unit "
                    "WHERE processing_run_id=:run"
                ),
                {"run": self.processing_run_id},
            ).scalar_one()
            secret_count = conn.execute(
                sa.text(
                    "SELECT count(*) FROM disclosure_ops.remote_parse_v4_secret "
                    "WHERE attempt_id=:attempt"
                ),
                {"attempt": attempt_id},
            ).scalar_one()
        self.assertEqual(dict(final), {"state": "acked", "is_current": False})
        self.assertEqual(document["status"], "published")
        self.assertEqual(document["current_processing_run_id"], self.processing_run_id)
        self.assertEqual(dict(run), {"status": "succeeded", "is_active": True})
        self.assertGreater(unit_count, 0)
        self.assertEqual(secret_count, 0)


if __name__ == "__main__":
    unittest.main()
