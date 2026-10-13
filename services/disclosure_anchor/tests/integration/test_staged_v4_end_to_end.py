"""Scratch-PostgreSQL, real-filesystem, fake-HTTP staged V4 closure."""

from __future__ import annotations

from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4

from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import io
import json
import os
import re
import time
from types import SimpleNamespace
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
from disclosure_anchor.adapters.db.postgres.staged_recovery_scope_v4 import (
    inspect_accepted_recovery_scope, require_accepted_recovery_scope,
)
from disclosure_anchor.adapters.runtime.staged_worker_v4 import build_staged_worker_v4_runtime
from disclosure_anchor.adapters.runtime.worker_stop_control import (
    WorkerControlStore,
    release_worker_circuit,
)
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
from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    decode_atomic_publication_request_v4,
)
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    ATOMIC_PUBLICATION_PREPARATION_FILENAME,
    ATOMIC_PUBLICATION_READINESS_FILENAME,
    decode_atomic_publication_preparation_v1,
)
from disclosure_anchor.application.contracts.mineru_process_profile import (
    encode_mineru_process_profile,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    provider_document_envelope_from_bytes,
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
from disclosure_anchor.application.ports.worker_stop_control import WorkerOperationalStopError
from disclosure_anchor.application.ports.staged_new_work_v4 import (
    V4OrdinaryParseCandidate,
)
from disclosure_anchor.application.services.atomic_publication_request_builder_v4 import (
    AtomicPublicationRequestBuilderV4Error,
    ProductionAtomicPublicationRequestBuilderV4,
    _unit_page_numbers,
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
from disclosure_anchor.adapters.db.postgres import atomic_document_publisher_v4 as publisher_module
from disclosure_anchor.application.services.staged_coordinator_persistence_v4 import (
    DurableStagedCoordinatorPersistenceV4 as _Persistence,
)
from disclosure_anchor.cli.worker import WORKER_NS
from disclosure_anchor.domain import ids
from disclosure_anchor.settings import load_settings
from disclosure_anchor.cli.staged_commission import _documents
from scripts.gc_orphan_artifacts import (
    _collect_orphans,
    _merge_expected_owners,
    _scan_old_candidates,
    _snapshot_expected_owners,
    _snapshot_preparation_owners,
)
from tests.integration._f5_upgrade_legacy_fixture import FleetProvider, create_document
from tests.integration._support import engine_or_skip
from tests.unit._semantic_routes import _fallback_receipt
from tests.unit.test_mineru_medium_artifacts import _write_bundle
from tests.unit.test_mineru_process_profile import _profile
from tests.unit.test_settings import _env, _mineru_topology
from tests.unit._publication_text_fixture import as_v23


class _StageGuard:
    def checkpoint(self) -> None:
        return None

    def remaining_seconds(self) -> float:
        return 60.0

    def note(self, kind: str, **scalars: int | str | None) -> None:
        return None


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


def _official_result_zip(
    source_pdf_sha256: str, *, cross_page_heading: bool = False, first_text: tuple[str, bool] | None = None,
) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_bundle(root)
        (root / "images" / "owner.jpg").write_bytes(b"\xff\xd8\xffowner-crop")
        (root / "images" / "continuation.jpg").write_bytes(
            b"\xff\xd8\xffcontinuation-crop"
        )
        content_list = next(root.glob("*_content_list.json"))
        if first_text is not None:
            # The provider's own first block, as MinerU returns it: its text is
            # exactly ``text`` (escaped by json.dumps), as a heading or as body.
            text, heading = first_text
            content = json.loads(content_list.read_text())
            content[0].update(text=text, text_level=1 if heading else 0)
            content_list.write_text(json.dumps(content, ensure_ascii=False))
            typed_file = next(root.glob("*_content_list_v2.json"))
            typed = json.loads(typed_file.read_text())
            typed[0][0].update(type="title" if heading else "paragraph", level=1)
            typed_file.write_text(json.dumps(typed, ensure_ascii=False))
        if cross_page_heading:
            content = json.loads(content_list.read_text())
            content[0].update(text="一、考核安排", text_level=1)
            content.extend([
                {"type": "text", "page_idx": 1, "bbox": [100, 500, 900, 550],
                 "text": "（一）考核次数", "text_level": 2},
                {"type": "text", "page_idx": 1, "bbox": [100, 600, 900, 650],
                 "text": "每个会计年度考核一次。"},
            ])
            content_list.write_text(json.dumps(content, ensure_ascii=False))
            typed_file = next(root.glob("*_content_list_v2.json"))
            typed = json.loads(typed_file.read_text())
            typed[0][0].update(type="title", level=1)
            typed[1].extend([
                {"type": "title", "bbox": [100, 500, 900, 550], "level": 2},
                {"type": "paragraph", "bbox": [100, 600, 900, 650]},
            ])
            typed_file.write_text(json.dumps(typed, ensure_ascii=False))
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
        # The worker start gate trusts only an existing 0700 runtime root owned
        # by this user, as on the production volume; it never creates one.
        runtime_root = self.root / "services" / "disclosure_anchor" / "runtime"
        runtime_root.mkdir(parents=True, mode=0o700)
        runtime_root.chmod(0o700)
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
                    # Fresh content-preserving NUL cases now publish Units; remove their dependent
                    # scratch rows before processing_run, exactly as for the primary fixture document.
                    for table in ("disclosure_ops.durable_publish_base", "disclosure_ops.outbox_event",
                                  "disclosure_core.document_unit"):
                        conn.execute(sa.text(f"DELETE FROM {table} WHERE document_id=:document"),
                                     {"document": extra_document})
                    conn.execute(sa.text(
                        "UPDATE disclosure_core.document SET current_processing_run_id=NULL WHERE document_id=:document"
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

    def test_scoped_production_runtime_builder_publishes_only_selected_document(self) -> None:
        self._run_scoped_builder(recover_encoding_failure=False)

    def test_accepted_result_recovery_uses_original_h0_and_no_new_post_or_admission(self) -> None:
        self._run_scoped_builder(recover_encoding_failure=True)

    def test_cross_page_publication_tail_recovery_preserves_h0_and_materialization(self) -> None:
        self._run_scoped_builder(recover_encoding_failure=False, recover_lineage_failure=True)

    def _run_scoped_builder(self, *, recover_encoding_failure: bool,
                            recover_lineage_failure: bool = False) -> None:
        recovering = recover_encoding_failure or recover_lineage_failure
        profile = replace(_profile(), api_task_slots=1, api_max_pending_tasks=1,
            cpu_worker_threads=3, omp_thread_count=1,
            registry_nonterminal_cap=1, registry_terminal_cap=127, processing_window_size=16,
            raster_stage_slots=1, layout_stage_slots=1, postprocess_stage_slots=1,
            native_owner_slots=1, requested_hybrid_batch_ratio=1, effective_hybrid_batch_ratio=1,
            finalizer_slots=1, gpu_request_slots=7)
        profile_path, keyring_path = self.root / "profile.json", self.root / "keyring.json"
        profile_path.write_bytes(profile.exact_bytes)
        keyring_path.write_text(json.dumps({"format": "disclosure-v4-secret-keyring.v1",
            "primary_kek_id": "fixture", "keks": {"fixture": "11"*32}}))
        profile_path.chmod(0o600)
        keyring_path.chmod(0o600)
        outside = "doc_000_" + ids.new_ulid()
        self.extra_document_ids.append(outside)
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO disclosure_core.document (document_id,security_id,provider,provider_document_id,"
                "raw_file_relpath,raw_file_hash,status) VALUES (:doc,:sec,'cninfo','outside-scope',"
                ":path,:sha,'registered')"
            ), {"doc": outside, "sec": self.security_id, "path": self.source_relpath.as_posix(), "sha": self.source_sha256})
        fake = _FakeMinerU(attempt_id="pending", fence_identity="pending", client_submit_key="pending",
                           source_pdf=self.source_bytes, artifact=_official_result_zip(
                               self.source_sha256, cross_page_heading=recover_lineage_failure))
        fail_encoding = recover_encoding_failure

        def http(request: httpx.Request) -> httpx.Response:
            nonlocal fail_encoding
            if request.method == "POST" and request.url.path == "/tasks":
                body = request.read()
                for field, attribute in (("agent_attempt_identity", "attempt_id"),
                                         ("agent_fence_identity", "fence_identity"),
                                         ("agent_idempotency_key", "client_submit_key")):
                    match = re.search(b'name="'+field.encode()+b'"\r\n\r\n([^\r\n]+)', body)
                    self.assertIsNotNone(match)
                    setattr(fake, attribute, match.group(1).decode())
            response = fake(request)
            if request.url.path.endswith("/lease"):
                response = httpx.Response(200, json={**response.json(), "lease_until_unix": time.time()+600})
            if request.url.path.endswith("/result") and fail_encoding:
                # Adjacent bad-server case: the client must reject the encoding
                # before reading, retain the owner, and recover the same result.
                fail_encoding = False
                response.headers["Content-Encoding"] = "gzip"
            return response

        def make_remote(**kwargs: Any) -> MinerUHttpRemoteV4:
            self.remote = MinerUHttpRemoteV4(transport=httpx.MockTransport(http), **kwargs)
            return self.remote
        normal_uow = unit_of_work_factory(self.engine)
        scratch = self.settings.disclosure_runtime_root / "staged_v4" / "scratch"

        def cleanup_probe() -> bool:
            with normal_uow() as uow:
                authority = uow.remote_parse_v4.load(fake.attempt_id)
            intent = next(e.value for e in authority.evidence if e.kind == "materialization_intent")
            return authority.state == "ack_pending" and all(not (scratch/relpath).exists() for relpath in (
                authority.reservation.snapshot_relpath, intent.spool_relpath, intent.output_relpath))

        fake.cleanup_probe = cleanup_probe

        def old_lineage_check(**kwargs: Any) -> tuple[int, ...]:
            pages = _unit_page_numbers(**kwargs)
            if pages[0] != kwargs["draft"].page_no:
                raise AtomicPublicationRequestBuilderV4Error(
                    "publication Unit primary page differs from full locator lineage")
            return pages

        with (
            patch.dict(os.environ, {**_env(self.root), **_mineru_topology(),
                "WORKER_PARSE_EXECUTION_MODE": "staged-v4",
                "WORKER_PARSE_CONCURRENCY": "1", "WORKER_FINALIZE_CONCURRENCY": "1",
                "DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256": profile.runtime_bundle_identity_sha256,
                "DISCLOSURE_V4_PROCESS_PROFILE_FILE": str(profile_path),
                "DISCLOSURE_V4_PROCESS_PROFILE_SHA256": profile.sha256,
                "DISCLOSURE_V4_SECRET_KEYRING_FILE": str(keyring_path),
                "DISCLOSURE_V4_ARCHIVE_MEMBER_COUNT_LIMIT": "8192"}, clear=True),
            patch("disclosure_anchor.adapters.runtime.staged_worker_v4.MinerUHttpRemoteV4", side_effect=make_remote),
            patch("disclosure_anchor.adapters.runtime.staged_worker_v4.build_semantic_runtime",
                  return_value=SimpleNamespace(router=_V4SemanticRouter())),
        ):
            runtime = build_staged_worker_v4_runtime(settings=load_settings(), engine=self.engine,
                ownership_guard=lambda: None, admission_guard=lambda: None, process_scope_classes=None,
                progress=lambda _: None, admission_document_ids=(self.document_id,))
            try:
                runtime.verify_startup()
                deadline = time.monotonic()+30
                with (patch(
                    "disclosure_anchor.application.services.atomic_publication_request_builder_v4._unit_page_numbers",
                    side_effect=old_lineage_check,
                ) if recover_lineage_failure else nullcontext()):
                    result = runtime.coordinator.run(stop_requested=lambda: time.monotonic() >= deadline)
            finally:
                runtime.close()
            if recovering:
                self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT, result.errors)
                self.assertEqual((result.admitted, result.completed, fake.task_posts, fake.ack_posts), (1, 0, 1, 0))
                expected_error = ("primary page differs" if recover_lineage_failure else "result headers drifted")
                self.assertTrue(any(expected_error in error for error in result.errors), result.errors)
                rows = inspect_accepted_recovery_scope(self.engine, document_ids=(self.document_id,))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["state"], "local_materialized" if recover_lineage_failure else "materializing")
                pinned = tuple({key: value for key, value in row.items()
                                if key not in {"state", "runtime_epoch_sha256", "is_current"}} for row in rows)
                original = pinned[0].copy()
                # The failed run is a persisted public stop; recovery needs an
                # explicit, hash-bound release, never a silent restart.
                self._release_recorded_stop(result, fake.attempt_id)
                with normal_uow() as uow:
                    before_authority = uow.remote_parse_v4.load(fake.attempt_id)
                before_materialization = next((e.sha256 for e in before_authority.evidence
                                               if e.kind == "local_materialization_receipt"), None)
                with self.engine.begin() as conn:
                    conn.execute(sa.text("UPDATE disclosure_ops.remote_parse_attempt SET claim_lease_until=:expired WHERE attempt_id=:attempt"),
                                 {"attempt": fake.attempt_id, "expired": datetime.now(UTC)-timedelta(seconds=1)})

                def guard() -> None:
                    require_accepted_recovery_scope(self.engine, attempts=pinned,
                                                   runtime_sha256=profile.runtime_bundle_identity_sha256)

                def no_admission() -> None:
                    raise AssertionError("recovery must never call ordinary or prepared admission")

                recovered = build_staged_worker_v4_runtime(settings=load_settings(), engine=self.engine,
                    ownership_guard=guard, admission_guard=no_admission, process_scope_classes=None,
                    progress=lambda _: None, admission_document_ids=(self.document_id,), recovery_only=True)
                try:
                    self.assertFalse(recovered.remote._allow_task_submission)
                    recovered.verify_startup()
                    deadline = time.monotonic()+30
                    result = recovered.coordinator.run(stop_requested=lambda: time.monotonic() >= deadline)
                finally:
                    recovered.close()
                guard()
                after = inspect_accepted_recovery_scope(self.engine, document_ids=(self.document_id,),
                                                       accepted_attempt_ids=(fake.attempt_id,))[0]
                self.assertEqual({key: after[key] for key in original}, original)
                self.assertFalse(after["is_current"])
                if recover_lineage_failure:
                    self.assertIsNotNone(before_materialization)
                    with normal_uow() as uow:
                        after_authority = uow.remote_parse_v4.load(fake.attempt_id)
                    self.assertEqual(next(e.sha256 for e in after_authority.evidence
                                          if e.kind == "local_materialization_receipt"), before_materialization)
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT, result.errors)
        self.assertEqual((result.admitted, result.completed), (0 if recovering else 1, 1))
        self.assertEqual(result.credits_in_use.nonzero(), {})
        self.assertEqual((fake.task_posts, fake.ack_posts), (1, 1))
        self.assertEqual(_documents(self.engine, (self.document_id,))[self.document_id]["attempt_state"], "acked")
        with self.engine.connect() as conn:
            row = conn.execute(sa.text("SELECT status,current_processing_run_id FROM disclosure_core.document WHERE document_id=:doc"),
                               {"doc": self.document_id}).mappings().one()
            self.assertEqual(row["status"], "published")
            self.processing_run_id = row["current_processing_run_id"]
            if recover_lineage_failure:
                child = conn.execute(sa.text(
                    "SELECT page_no,artifact_locator FROM disclosure_public.document_units_v1 "
                    "WHERE document_id=:doc AND is_active_run AND title='（一）考核次数'"
                ), {"doc": self.document_id}).mappings().one()
                self.assertEqual(child["page_no"], 2)
                self.assertEqual([h["source_index"] for h in child["artifact_locator"]["heading_chain"]], [0, 3])
            self.assertEqual(conn.execute(sa.text("SELECT status FROM disclosure_core.document WHERE document_id=:doc"), {"doc": outside}).scalar_one(), "registered")
            self.assertEqual(conn.execute(sa.text("SELECT count(*) FROM disclosure_ops.remote_parse_attempt WHERE document_id=:doc"), {"doc": outside}).scalar_one(), 0)
        self._assert_generic_published_admission()

    def _release_recorded_stop(self, result: Any, attempt_id: str, *, document_id: str | None = None) -> None:
        cause = result.stop_cause
        self.assertEqual(result.termination_kind, "public_stop")
        self.assertIsNotNone(cause)
        self.assertEqual((cause.kind, cause.origin, cause.attempt_id), ("stage_fault", "stage_call", attempt_id))
        settings = load_settings()
        active = WorkerControlStore.for_settings(settings).read_active()
        self.assertEqual(active.status, "valid")
        assert active.record is not None and active.sha256 is not None
        self.assertEqual((active.record.record_origin, active.record.cause), ("automatic_fault", cause))
        with self.assertRaises(WorkerOperationalStopError) as refused:
            build_staged_worker_v4_runtime(
                settings=settings, engine=self.engine, ownership_guard=lambda: None,
                admission_guard=lambda: None, process_scope_classes=None, progress=lambda _: None,
                admission_document_ids=(document_id or self.document_id,), recovery_only=True,
            )
        self.assertEqual((refused.exception.state, refused.exception.active_sha256),
                         ("PUBLIC_STOP", active.sha256))
        # The operator procedure: worker singleton first, then the hash-bound
        # release. The scratch run has no old worker process to wait for.
        lock_engine = sa.create_engine(self.engine.url, poolclass=sa.pool.NullPool,
                                       isolation_level="AUTOCOMMIT")
        try:
            with lock_engine.connect() as lock_conn:
                self.assertTrue(lock_conn.execute(sa.text("SELECT pg_try_advisory_lock(:ns, 0)"),
                                                  {"ns": WORKER_NS}).scalar_one())
                try:
                    receipt = release_worker_circuit(
                        settings, expect_sha256=active.sha256, decided_by="integration-test",
                        reason="scratch fault fixture corrected", fixed_by="integration-fixture",
                        process_lister=lambda: (),
                    )
                finally:
                    lock_conn.execute(sa.text("SELECT pg_advisory_unlock(:ns, 0)"), {"ns": WORKER_NS})
        finally:
            lock_engine.dispose()
        self.assertTrue(receipt["released"])
        self.assertEqual(WorkerControlStore.for_settings(settings).read_active().status, "absent")

    def _assert_generic_published_admission(self) -> None:
        with unit_of_work_factory(self.engine)() as uow:
            document = uow.documents.get(self.document_id)
            assert document is not None and document.current_processing_run_id is not None
            run = uow.processing_runs.get(document.current_processing_run_id)
        assert run is not None and run.provider_document_relpath is not None
        envelope = provider_document_envelope_from_bytes(
            self.source.read_provider_document_record(Path(run.provider_document_relpath))
        )
        admitted = ProviderDocumentAdmission(path_builder=self.paths, source=self.source).admit(
            document=document, run=run, artifact_owner=run, security_code="000001",
        )
        self.assertEqual(admitted.envelope, envelope)
        self.assertEqual(self.source.rebuild_provider_document(
            Path(envelope.parser_artifact_root_relpath), source_pdf_sha256=self.source_sha256,
        ), envelope.provider_document)

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
        self._assert_generic_published_admission()
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

    # -- publication text representability (U+0000) through the production runtime --------------

    _PUBLICATION_EVENTS = frozenset({
        "processing_run_published", "document_unit_created", "document_unit_removed",
        "document_unit_projection_changed",
    })
    _NUL_HEADING = "第\x00节 重要事项"

    def _staged_env(self) -> dict[str, str]:
        profile = replace(_profile(), api_task_slots=1, api_max_pending_tasks=1,
            cpu_worker_threads=3, omp_thread_count=1,
            registry_nonterminal_cap=1, registry_terminal_cap=127, processing_window_size=16,
            raster_stage_slots=1, layout_stage_slots=1, postprocess_stage_slots=1,
            native_owner_slots=1, requested_hybrid_batch_ratio=1, effective_hybrid_batch_ratio=1,
            finalizer_slots=1, gpu_request_slots=7)
        profile_path, keyring_path = self.root / "profile.json", self.root / "keyring.json"
        if not profile_path.exists():
            profile_path.write_bytes(profile.exact_bytes)
            keyring_path.write_text(json.dumps({"format": "disclosure-v4-secret-keyring.v1",
                "primary_kek_id": "fixture", "keks": {"fixture": "11"*32}}))
            profile_path.chmod(0o600)
            keyring_path.chmod(0o600)
        return {**_env(self.root), **_mineru_topology(),
                "WORKER_PARSE_EXECUTION_MODE": "staged-v4",
                "WORKER_PARSE_CONCURRENCY": "1", "WORKER_FINALIZE_CONCURRENCY": "1",
                "DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256": profile.runtime_bundle_identity_sha256,
                "DISCLOSURE_V4_PROCESS_PROFILE_FILE": str(profile_path),
                "DISCLOSURE_V4_PROCESS_PROFILE_SHA256": profile.sha256,
                "DISCLOSURE_V4_SECRET_KEYRING_FILE": str(keyring_path),
                "DISCLOSURE_V4_ARCHIVE_MEMBER_COUNT_LIMIT": "8192"}

    @contextmanager
    def _production_environment(self, fleet: FleetProvider, router: Any) -> Iterator[None]:
        """The scoped production runtime's environment: scratch DB, fake HTTP, fallback routing."""

        def make_remote(**kwargs: Any) -> MinerUHttpRemoteV4:
            self.remote = MinerUHttpRemoteV4(transport=httpx.MockTransport(fleet), **kwargs)
            return self.remote

        with (
            patch.dict(os.environ, self._staged_env(), clear=True),
            patch("disclosure_anchor.adapters.runtime.staged_worker_v4.MinerUHttpRemoteV4", side_effect=make_remote),
            patch("disclosure_anchor.adapters.runtime.staged_worker_v4.build_semantic_runtime",
                  return_value=SimpleNamespace(router=router)),
        ):
            yield

    def _boot(self, document_ids: tuple[str, ...], *, seconds: float = 60.0) -> Any:
        """One production-composed worker boot until quiescence, stop, or the bound."""

        runtime = build_staged_worker_v4_runtime(settings=load_settings(), engine=self.engine,
            ownership_guard=lambda: None, admission_guard=lambda: None, process_scope_classes=None,
            progress=lambda _: None, admission_document_ids=document_ids)
        try:
            runtime.verify_startup()
            deadline = time.monotonic() + seconds
            return runtime.coordinator.run(stop_requested=lambda: time.monotonic() >= deadline)
        finally:
            runtime.close()
            if self.remote is not None:
                self.remote.close()
                self.remote = None

    def _bad_document(self, label: str, width: int, height: int) -> Any:
        document = create_document(self.engine, self.paths, security_id=self.security_id, label=label,
                                   target="bad", width=width, height=height)
        self.extra_document_ids.append(document.document_id)
        return document

    def _only_attempt(self, document_id: str) -> Any:
        with unit_of_work_factory(self.engine)() as uow:
            with self.engine.connect() as conn:
                attempts = conn.execute(sa.text(
                    "SELECT attempt_id FROM disclosure_ops.remote_parse_attempt WHERE document_id=:doc"
                ), {"doc": document_id}).scalars().all()
            self.assertEqual(len(attempts), 1, f"{document_id} must have exactly one attempt")
            return uow.remote_parse_v4.load(attempts[0])

    def _publication_facts(self, document_id: str, processing_run_id: str) -> dict[str, Any]:
        with self.engine.connect() as conn:
            return {
                "units": conn.execute(sa.text(
                    "SELECT count(*) FROM disclosure_core.document_unit WHERE processing_run_id=:run"
                ), {"run": processing_run_id}).scalar_one(),
                "winners": conn.execute(sa.text(
                    "SELECT count(*) FROM disclosure_ops.atomic_publication_winner_v4 WHERE processing_run_id=:run"
                ), {"run": processing_run_id}).scalar_one(),
                "events": Counter(conn.execute(sa.text(
                    "SELECT event_kind FROM disclosure_ops.outbox_event WHERE document_id=:doc"
                ), {"doc": document_id}).scalars().all()),
                "failed_payloads": conn.execute(sa.text(
                    "SELECT payload FROM disclosure_ops.outbox_event "
                    "WHERE document_id=:doc AND event_kind='processing_run_failed'"
                ), {"doc": document_id}).scalars().all(),
                "document": dict(conn.execute(sa.text(
                    "SELECT status,current_processing_run_id FROM disclosure_core.document WHERE document_id=:doc"
                ), {"doc": document_id}).mappings().one()),
                "run": dict(conn.execute(sa.text(
                    "SELECT status,is_active,error FROM disclosure_core.processing_run WHERE processing_run_id=:run"
                ), {"run": processing_run_id}).mappings().one()),
            }

    def _assert_closed_as_unstorable_text(self, document_id: str, raw_relpath: Path, raw_sha256: str) -> Any:
        """The one typed local failure: receipt, cleanup, ACK, local_failed; nothing published."""

        authority = self._only_attempt(document_id)
        self.assertEqual(authority.state, "local_failed")
        receipts = [item.value for item in authority.evidence if item.kind == "failure_receipt"]
        self.assertEqual(len(receipts), 1)
        failure = receipts[0]
        self.assertEqual(
            (failure.outcome, failure.error_stage, failure.error_code, failure.error_class, failure.retryable,
             failure.retry_budget_class),
            ("local_failure", "commit", "publication_text_unrepresentable", "PublicationTextUnrepresentableError",
             False, "provider_artifact_contract"),
        )
        message = failure.message
        self.assertTrue(message.isascii() and message.isprintable() and 1 <= len(message) <= 4096, message)
        self.assertIn("U+0000", message)
        self.assertIn("publication_text_representability.v1", message)
        for fragment in ("重要事项", "节", "正文"):
            self.assertNotIn(fragment, message)
        self.assertLessEqual({"cleanup_plan", "cleanup_receipt", "ack_receipt"},
                             {item.kind for item in authority.evidence})
        facts = self._publication_facts(document_id, authority.processing_run_id)
        self.assertEqual((facts["units"], facts["winners"]), (0, 0))
        self.assertFalse(set(facts["events"]) & self._PUBLICATION_EVENTS, facts["events"])
        self.assertEqual(facts["events"]["processing_run_failed"], 1)
        # The same safe message is what the two JSONB failure sinks persisted and read back.
        self.assertEqual(facts["failed_payloads"][0]["error"]["message"], message)
        self.assertEqual(facts["run"]["status"], "failed")
        self.assertFalse(facts["run"]["is_active"])
        self.assertEqual(facts["run"]["error"], {
            "stage": "commit", "error_code": "publication_text_unrepresentable",
            "error_class": "PublicationTextUnrepresentableError", "retryable": False,
            "retry_budget_class": "provider_artifact_contract", "message": message,
        })
        self.assertEqual(facts["document"], {"status": "parse_failed", "current_processing_run_id": None})
        raw = self.paths.data_path(raw_relpath).read_bytes()
        self.assertEqual("sha256:" + hashlib.sha256(raw).hexdigest(), raw_sha256)
        return authority

    def test_fresh_nul_text_publishes_recorded_replacements_while_good_documents_keep_publishing(self) -> None:
        documents_with_nul = (
            (self._bad_document("nul-title", 120, 160), self._NUL_HEADING, True),
            (self._bad_document("nul-body", 130, 170), "正文\x00片段", False),
        )
        fleet = FleetProvider()
        for document, original, heading in documents_with_nul:
            fleet.register(document.source_sha256, document.source, _official_result_zip(
                document.source_sha256, first_text=(original, heading)), complete_on_accept=True)
        fleet.register(self.source_sha256, self.source_bytes, _official_result_zip(self.source_sha256),
                       complete_on_accept=True)
        documents = (*[document.document_id for document, _, _ in documents_with_nul], self.document_id)
        with self._production_environment(fleet, _V4SemanticRouter()):
            result = self._boot(documents)
            self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT, result.errors)
            self.assertIsNone(result.stop_cause, result.errors)
            self.assertEqual((result.admitted, result.completed), (3, 3))
            self.assertEqual([state for _attempt, state in result.final_states], ["acked"] * 3)
            for document, original, heading in documents_with_nul:
                authority = self._only_attempt(document.document_id)
                self.assertEqual(authority.state, "acked")
                with self.engine.connect() as conn:
                    rows = conn.execute(sa.text(
                        "SELECT title,heading_path,payload,quality_status,artifact_locator "
                        "FROM disclosure_core.document_unit WHERE processing_run_id=:run ORDER BY order_index"
                    ), {"run": authority.processing_run_id}).mappings().all()
                    version = conn.execute(sa.text(
                        "SELECT builder_rules_version FROM disclosure_core.processing_run WHERE processing_run_id=:run"
                    ), {"run": authority.processing_run_id}).scalar_one()
                self.assertEqual(version, "provider_unit.v24")
                self.assertTrue(rows)
                affected = [row for row in rows if row["artifact_locator"].get("text_substitutions")]
                self.assertTrue(affected)
                replacement = original.replace("\x00", "\ufffd")
                for row in affected:
                    self.assertEqual(row["quality_status"], "needs_review")
                    self.assertEqual(row["artifact_locator"]["contract_version"], "provider_unit_locator.v10")
                    pending = list(row.values())
                    while pending:
                        value = pending.pop()
                        if isinstance(value, str):
                            self.assertNotIn("\x00", value)
                        elif isinstance(value, dict):
                            pending.extend(value.keys())
                            pending.extend(value.values())
                        elif isinstance(value, (list, tuple)):
                            pending.extend(value)
                self.assertTrue(any(replacement in (row["title"] or "" if heading else
                                    json.dumps(row["payload"], ensure_ascii=False)) for row in affected))
                self.assertEqual("sha256:" + hashlib.sha256(self.paths.data_path(document.relpath).read_bytes()).hexdigest(),
                                 document.source_sha256)
                task = fleet.task_for_attempt(authority.attempt_id)
                self.assertEqual((fleet.posts[task.client_submit_key], fleet.acks[task.task_id]), (1, 1))
            self.assertEqual(fleet.duplicate_posts, [])
            posts = sum(fleet.posts.values())
            again = self._boot(documents)
            self.assertEqual((again.admitted, again.completed), (0, 0))
            self.assertEqual(sum(fleet.posts.values()), posts)

    def test_sealed_unstorable_request_from_old_code_closes_typed_after_release_with_evidence_kept(self) -> None:
        fleet = FleetProvider()
        fleet.register(self.source_sha256, self.source_bytes, _official_result_zip(
            self.source_sha256, first_text=(self._NUL_HEADING, True)), complete_on_accept=True)
        router = _CountingSemanticRouter()
        with self._production_environment(fleet, router):
            # Boot 1 is the old code (no text check): it seals and promotes the request and the
            # real driver refuses the TEXT title inside transaction P, a public stop.
            with as_v23() as simulation:
                stopped = self._boot((self.document_id,))
            self.assertGreater(simulation.builds, 0)
            self.assertEqual(stopped.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT, stopped.errors)
            cause = stopped.stop_cause
            assert cause is not None
            self.assertEqual((cause.kind, cause.reason_code, cause.lane, cause.exception_class),
                             ("stage_fault", "commit_unexpected_failure", "commit", "sqlalchemy.exc.DataError"))
            sealed = self._only_attempt(self.document_id)
            self.assertEqual(sealed.state, "local_materialized")
            self.assertNotIn("failure_receipt", {item.kind for item in sealed.evidence})
            facts = self._publication_facts(self.document_id, sealed.processing_run_id)
            self.assertEqual((facts["units"], facts["winners"]), (0, 0))
            self.assertFalse(set(facts["events"]) & self._PUBLICATION_EVENTS)
            data_root = self.paths.data_path(Path())
            (preparation_path,) = [path for path in data_root.rglob(ATOMIC_PUBLICATION_PREPARATION_FILENAME)
                                   if sealed.processing_run_id in path.parts]
            preparation = decode_atomic_publication_preparation_v1(preparation_path.read_bytes())
            request = decode_atomic_publication_request_v4(preparation.canonical_request_json.encode())
            self.assertTrue(any(unit.title is not None and "\x00" in unit.title for unit in request.units))
            self.assertEqual(json.loads(request.processing_run_projection_json)["builder_rules_version"], "provider_unit.v23")
            retained = [
                preparation_path, preparation_path.with_name(ATOMIC_PUBLICATION_READINESS_FILENAME),
                data_root / preparation.document_unit_snapshot_plan.relpath,
                data_root / preparation.semantic_route_receipts_plan.relpath,
                data_root / preparation.provider_document_plan.relpath,
                *sorted(path for path in (data_root / preparation.parser_output_plan.published_relpath).rglob("*")
                        if path.is_file()),
                self.paths.data_path(self.source_relpath),
            ]
            hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in retained}
            self.assertGreater(len(hashes), 6)
            self._release_recorded_stop(stopped, sealed.attempt_id)
            with self.engine.begin() as conn:
                conn.execute(sa.text(
                    "UPDATE disclosure_ops.remote_parse_attempt SET claim_lease_until=:expired WHERE attempt_id=:attempt"
                ), {"attempt": sealed.attempt_id, "expired": datetime.now(UTC) - timedelta(seconds=1)})
            model_calls_before = router.calls
            calls: Counter[str] = Counter()
            real_build = ProductionAtomicPublicationRequestBuilderV4.build
            real_commit = PostgresAtomicWholeDocumentPublisherV4.commit_whole_document

            def counted_build(builder: Any, **kwargs: Any) -> Any:
                calls["build"] += 1
                return real_build(builder, **kwargs)

            def counted_commit(publisher: Any, *args: Any, **kwargs: Any) -> Any:
                calls["transaction_p"] += 1
                return real_commit(publisher, *args, **kwargs)

            with (
                patch.object(ProductionAtomicPublicationRequestBuilderV4, "build", counted_build),
                patch.object(PostgresAtomicWholeDocumentPublisherV4, "commit_whole_document", counted_commit),
            ):
                recovered = self._boot((self.document_id,))
            self.assertEqual(recovered.terminal, CoordinatorTerminal.QUIESCENT, recovered.errors)
            self.assertIsNone(recovered.stop_cause, recovered.errors)
            self.assertEqual((recovered.admitted, recovered.completed), (0, 1))
            # Reopened, refused before readiness: no rebuild, no model call, no P, no new POST.
            self.assertEqual((calls["build"], calls["transaction_p"], router.calls - model_calls_before), (0, 0, 0))
            task = fleet.task_for_attempt(sealed.attempt_id)
            self.assertEqual((fleet.posts[task.client_submit_key], fleet.acks[task.task_id]), (1, 1))
            self._assert_closed_as_unstorable_text(self.document_id, self.source_relpath, self.source_sha256)
            self.assertEqual({path: hashlib.sha256(path.read_bytes()).hexdigest() for path in retained}, hashes)
            # GC: the DB owners plus the valid preparation keep every retained file; none is an orphan.
            with self.engine.connect() as conn:
                owners = _merge_expected_owners(_snapshot_expected_owners(conn),
                                                _snapshot_preparation_owners(data_root))
            later = time.time() + 30 * 86400
            candidates, _young = _scan_old_candidates(data_root, now_ts=later)
            orphans, _recheck = _collect_orphans(candidates, data_root=data_root, expected=owners, now_ts=later)
            self.assertFalse({orphan.path for orphan in orphans} & set(retained))
            preparation_only = _snapshot_preparation_owners(data_root)
            self.assertIn(preparation.document_unit_snapshot_plan.relpath,
                          preparation_only["document_unit_snapshots"])
            self.assertIn(preparation.parser_output_plan.published_relpath.rstrip("/"),
                          preparation_only["parser_artifacts"])
            # A damaged preparation stops GC instead of freeing its tree.
            original = preparation_path.read_bytes()
            try:
                preparation_path.write_bytes(original[: len(original) // 2])
                with self.assertRaisesRegex(RuntimeError, "blocks GC"):
                    _snapshot_preparation_owners(data_root)
            finally:
                preparation_path.write_bytes(original)
            self.assertEqual(hashlib.sha256(preparation_path.read_bytes()).hexdigest(), hashes[preparation_path])

    def test_failure_persistence_errors_and_unknown_data_errors_still_stop_publicly(self) -> None:
        fleet = FleetProvider()
        fleet.register(self.source_sha256, self.source_bytes, _official_result_zip(
            self.source_sha256, first_text=(self._NUL_HEADING, True)), complete_on_accept=True)
        with self._production_environment(fleet, _V4SemanticRouter()):
            # Preserve an actual old sealed request before exercising the receipt-persistence stop.
            with as_v23() as simulation:
                original_stop = self._boot((self.document_id,))
            self.assertGreater(simulation.builds, 0)
            self.assertEqual(original_stop.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
            old = self._only_attempt(self.document_id)
            self._release_recorded_stop(original_stop, old.attempt_id)
            with self.engine.begin() as conn:
                conn.execute(sa.text(
                    "UPDATE disclosure_ops.remote_parse_attempt SET claim_lease_until=:expired WHERE attempt_id=:attempt"
                ), {"attempt": old.attempt_id, "expired": datetime.now(UTC) - timedelta(seconds=1)})
            real_append = _Persistence.append_successor
            injected: list[str] = []

            def failing_receipt_append(persistence: Any, work: Any, append: Any, **kwargs: Any) -> Any:
                kinds = {item.kind for item in append.new_evidence}
                if "failure_receipt" in kinds and not injected:
                    injected.append(append.successor.state)
                    raise sa.exc.OperationalError("INSERT remote_parse_v4_evidence", {}, Exception("connection lost"))
                return real_append(persistence, work, append, **kwargs)

            with patch.object(_Persistence, "append_successor", failing_receipt_append):
                stopped = self._boot((self.document_id,))
            self.assertEqual(injected, ["cleanup_pending"])
            self.assertEqual(stopped.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT, stopped.errors)
            cause = stopped.stop_cause
            assert cause is not None
            self.assertEqual((cause.kind, cause.reason_code, cause.lane), ("stage_fault", "commit_unexpected_failure",
                                                                           "commit"))
            attempt = self._only_attempt(self.document_id)
            self.assertEqual(attempt.state, "local_materialized")
            self.assertNotIn("failure_receipt", {item.kind for item in attempt.evidence})
            self._release_recorded_stop(stopped, attempt.attempt_id)
            with self.engine.begin() as conn:
                conn.execute(sa.text(
                    "UPDATE disclosure_ops.remote_parse_attempt SET claim_lease_until=:expired WHERE attempt_id=:attempt"
                ), {"attempt": attempt.attempt_id, "expired": datetime.now(UTC) - timedelta(seconds=1)})
            closed = self._boot((self.document_id,))
            self.assertEqual(closed.terminal, CoordinatorTerminal.QUIESCENT, closed.errors)
            self._assert_closed_as_unstorable_text(self.document_id, self.source_relpath, self.source_sha256)

        # An unknown server-side DataError from a valid document stays a public stop, and P rolls back.
        good = self._bad_document("valid-but-server-refused", 140, 180)
        fleet_two = FleetProvider()
        fleet_two.register(good.source_sha256, good.source, _official_result_zip(good.source_sha256),
                           complete_on_accept=True)
        real_unit = publisher_module._document_unit

        def out_of_range_unit(unit: Any, *, asset_id: str) -> Any:
            return replace(real_unit(unit, asset_id=asset_id), page_no=2**40)

        with (
            self._production_environment(fleet_two, _V4SemanticRouter()),
            patch.object(publisher_module, "_document_unit", out_of_range_unit),
        ):
            refused = self._boot((good.document_id,))
            self.assertEqual(refused.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT, refused.errors)
            cause = refused.stop_cause
            assert cause is not None
            self.assertEqual((cause.kind, cause.reason_code, cause.exception_class),
                             ("stage_fault", "commit_unexpected_failure", "sqlalchemy.exc.DataError"))
            attempt = self._only_attempt(good.document_id)
            self.assertEqual(attempt.state, "local_materialized")
            self.assertNotIn("failure_receipt", {item.kind for item in attempt.evidence})
            facts = self._publication_facts(good.document_id, attempt.processing_run_id)
            self.assertEqual((facts["units"], facts["winners"]), (0, 0))
            self.assertFalse(set(facts["events"]) & self._PUBLICATION_EVENTS)
            self._release_recorded_stop(refused, attempt.attempt_id, document_id=good.document_id)


class _CountingSemanticRouter(_V4SemanticRouter):
    """Fallback routing that counts every call as model work."""

    def __init__(self) -> None:
        self.calls = 0

    def route(self, *, drafts: tuple[Any, ...], **kwargs: Any) -> Any:
        self.calls += 1
        return super().route(drafts=drafts, **kwargs)


if __name__ == "__main__":
    unittest.main()
