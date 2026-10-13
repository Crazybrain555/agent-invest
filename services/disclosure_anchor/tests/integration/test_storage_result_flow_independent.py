"""One storage-era result through the real scratch-PG staged publication path.

Run with the managed integration runner only. HTTP and result bytes are synthetic;
the durable ingress, coordinator backend, publisher, cleanup and ACK are real.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import io
import json
import mmap
import os
from pathlib import Path
import random
import stat
import traceback
import unittest
from unittest.mock import patch
import zipfile

import httpx
import sqlalchemy as sa

from disclosure_anchor.adapters.db.postgres.atomic_document_publisher_v4 import (
    PostgresAtomicWholeDocumentPublisherV4,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.adapters.runtime.worker_stop_control import (
    RuntimeWorkerStopControl,
    WorkerControlStore,
)
from disclosure_anchor.adapters.parsers.mineru_medium.http_remote_v4 import MinerUHttpRemoteV4
from disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4 import MinerUHttpStagedV4
from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import canonical_client_submit_key_v2
from disclosure_anchor.adapters.parsers.mineru_medium.v4_initial_ingress import MinerUV4InitialIngressFactory
from disclosure_anchor.adapters.parsers.mineru_medium.v4_stage_input_resolver import ProductionV4StageInputResolver
from disclosure_anchor.adapters.security.provider_secret_cipher import AesGcmProviderSecretCipher
from disclosure_anchor.adapters.security.provider_secret_keyring import StaticProviderSecretKeyring
from disclosure_anchor.adapters.storage.atomic_publication_artifact_readiness_v4 import (
    FilesystemAtomicPublicationArtifactReadinessV4,
)
from disclosure_anchor.adapters.storage.published_parser_output_verifier_v4 import (
    PublishedParserOutputVerifierV4,
)
from disclosure_anchor.adapters.storage.v4_source_observation import BoundedV4SourcePdfObserver
from disclosure_anchor.application.contracts.mineru_process_profile import (
    RESULT_STORAGE_PROCESS_PROFILE_CONTRACT,
    encode_mineru_process_profile,
)
from disclosure_anchor.application.contracts.mineru_capacity_config import MineruResultStoragePolicy
from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializationTransferContinuesV4,
    MaterializationUnpackContinuesV4,
)
from disclosure_anchor.application.ports.staged_new_work_v4 import V4OrdinaryParseCandidate
from disclosure_anchor.application.services.atomic_publication_request_builder_v4 import (
    ProductionAtomicPublicationRequestBuilderV4,
)
from disclosure_anchor.application.services.atomic_publication_request_factory_v4 import (
    RecoverableAtomicPublicationRequestFactoryV4,
)
from disclosure_anchor.application.services.provider_document_admission import ProviderDocumentAdmission
from disclosure_anchor.application.services.staged_coordinator_backend_v4 import DurableStagedCoordinatorBackendV4
from disclosure_anchor.application.services.staged_coordinator_persistence_v4 import (
    DurableStagedCoordinatorPersistenceV4,
    DurableV4ClaimGuard,
)
from disclosure_anchor.application.services.staged_ingress_v4 import DurableStagedIngressV4
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorTerminal,
    StageResourceGrantRequired,
    StageWaiting,
    StagedParseCoordinator,
)
from disclosure_anchor.application.services.staged_v4_capacity import staged_v4_coordinator_limits
from disclosure_anchor.application.use_cases.prepare_and_publish_whole_document_v4 import (
    PrepareAndPublishWholeDocumentV4,
)
from disclosure_anchor.domain import ids
from tests.integration import test_staged_v4_end_to_end as existing
from tests.unit.test_mineru_materialize_grant_v5 import synthetic_storage_policy
from tests.unit.test_mineru_process_profile import _profile


_MIB = 1024 * 1024
_DURABLE_PREFIX_BYTES = 2 * _MIB


class _InterruptedBody(httpx.SyncByteStream):
    def __init__(self, prefix: bytes) -> None:
        self.prefix = prefix

    def __iter__(self):
        yield self.prefix
        raise httpx.ReadError("synthetic interruption after durable prefix")

    def close(self) -> None:
        pass


class _ArtifactSuffix(httpx.SyncByteStream):
    """Serve a large mmap-backed suffix without copying it into one HTTP body."""

    def __init__(self, artifact: bytes | mmap.mmap, offset: int) -> None:
        self.artifact = artifact
        self.offset = offset

    def __iter__(self):
        for position in range(self.offset, len(self.artifact), _MIB):
            yield self.artifact[position:position + _MIB]

    def close(self) -> None:
        pass


class _StorageResultProvider(existing._FakeMinerU):
    def __init__(self, *, policy_sha256: str, inventory_sha256: str,
                 selected_bytes: int, member_count: int, blocked: bool = False,
                 **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.policy_sha256 = policy_sha256
        self.inventory_sha256 = inventory_sha256
        self.selected_bytes = selected_bytes
        self.member_count = member_count
        self.blocked = blocked
        self.status_polls = 0
        self.offsets: list[int] = []

    def _task_payload(self, status: str) -> dict[str, object]:
        payload = super()._task_payload(status)
        sealed = status == "completed"
        held = self.blocked and status == "processing"
        payload["storage"] = {
            "schema": "mineru.task-storage-status.v1",
            "policy_sha256": self.policy_sha256,
            "phase": "zip_sealed" if sealed else "source_growing" if held else "admitted",
            "wait_reason": "tree_integrity" if held else None,
            "wait_since_unix": 1000.0 if held else None,
            "blocked": held,
            "selected_bytes": self.selected_bytes if sealed else 0,
            "member_count": self.member_count if sealed else 0,
            "inventory_sha256": self.inventory_sha256 if sealed else None,
            "zip_bytes": len(self.artifact) if sealed else 0,
        }
        return payload

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if (self.blocked and request.method == "GET"
                and request.url.path == f"/tasks/{self.task_id}"):
            self.calls.append(f"GET {request.url.path}")
            self.status_polls += 1
            if self.status_polls > 2:
                raise AssertionError("blocked native poll was redispatched without public stop")
            return httpx.Response(200, json=self._task_payload("processing"))
        if request.method == "GET" and request.url.path == f"/tasks/{self.task_id}/result":
            self.calls.append(f"GET {request.url.path}")
            self.result_gets += 1
            headers = {
                "Content-Type": "application/zip",
                "X-MinerU-Result-SHA256": self.artifact_digest,
                "X-MinerU-Result-Owner": self.artifact_owner,
                "ETag": f'"{self.artifact_digest}"',
            }
            if self.result_gets == 1:
                self.offsets.append(0)
                return httpx.Response(200, stream=_InterruptedBody(self.artifact[:_DURABLE_PREFIX_BYTES]),
                                      headers={**headers, "Content-Length": str(len(self.artifact))})
            range_header = request.headers.get("Range")
            assert range_header is not None and range_header.startswith("bytes=")
            offset = int(range_header.removeprefix("bytes=").removesuffix("-"))
            assert request.headers.get("If-Range") == headers["ETag"]
            assert 0 < offset < len(self.artifact)
            self.offsets.append(offset)
            return httpx.Response(206, stream=_ArtifactSuffix(self.artifact, offset), headers={
                **headers,
                "Content-Length": str(len(self.artifact) - offset),
                "Content-Range": f"bytes {offset}-{len(self.artifact)-1}/{len(self.artifact)}",
            })
        return super().__call__(request)


class _NearUnpackDeadline:
    """Report a short LOCAL budget only after the first durable member exists.

    The coordinator's real guard still checks every checkpoint and claim. This
    wrapper scripts the safe continuation decision without granting extra time.
    """

    def __init__(self, delegate: object, first_member: Path, enabled: list[bool]) -> None:
        self.delegate = delegate
        self.first_member = first_member
        self.enabled = enabled

    def checkpoint(self) -> None:
        self.delegate.checkpoint()

    def remaining_seconds(self) -> float:
        actual = self.delegate.remaining_seconds()
        return min(actual, 9.0) if self.enabled[0] and self.first_member.exists() else actual

    def note(self, kind: str, **scalars: int | str | None) -> None:
        self.delegate.note(kind, **scalars)


def _expanded_official_zip(source_sha256: str) -> tuple[bytes, int, int]:
    """Keep the source-bound official bundle and add one opaque, bounded member."""
    original = existing._official_result_zip(source_sha256)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(
        output, "w", zipfile.ZIP_DEFLATED
    ) as target:
        for info in source.infolist():
            target.writestr(info, source.read(info.filename))
        padding = zipfile.ZipInfo("capacity-padding.bin")
        padding.compress_type = zipfile.ZIP_DEFLATED
        padding.create_system = 3
        padding.external_attr = (stat.S_IFREG | 0o600) << 16
        target.writestr(padding, random.Random(20260927).randbytes(3 * _MIB))
    archive = output.getvalue()
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        infos = [info for info in zipped.infolist() if not info.is_dir()]
    return archive, sum(info.file_size for info in infos), len(infos)


def _large_official_zip(source_sha256: str, destination: Path) -> tuple[mmap.mmap, int, int, str]:
    """Make a >256 MiB official bundle with a streamed, deterministic sidecar."""
    original = existing._official_result_zip(source_sha256)
    random_bytes = random.Random(20260927)
    padding_digest = hashlib.sha256()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(
        destination, "w", zipfile.ZIP_DEFLATED
    ) as target:
        for info in source.infolist():
            target.writestr(info, source.read(info.filename))
        padding = zipfile.ZipInfo("capacity-padding.bin")
        padding.compress_type = zipfile.ZIP_DEFLATED
        padding.create_system = 3
        padding.external_attr = (stat.S_IFREG | 0o600) << 16
        with target.open(padding, "w", force_zip64=True) as member:
            for _ in range(257):
                chunk = random_bytes.randbytes(_MIB)
                padding_digest.update(chunk)
                member.write(chunk)
    with zipfile.ZipFile(destination) as zipped:
        infos = [info for info in zipped.infolist() if not info.is_dir()]
    with destination.open("rb") as source:
        artifact = mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ)
    return artifact, sum(info.file_size for info in infos), len(infos), padding_digest.hexdigest()


def _draft_policy_on_fixture_volume(path: Path, fixture_root: Path) -> MineruResultStoragePolicy:
    """Keep the general draft budgets while binding Mac identity to scratch."""
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise ValueError("storage policy draft must be a JSON object")
    usage = os.statvfs(fixture_root)
    values["mac_volume_identity"] = f"scratch-volume:dev-{fixture_root.stat().st_dev}"
    values["mac_volume_total_bytes"] = usage.f_blocks * usage.f_frsize
    return MineruResultStoragePolicy(**values)


class StorageResultFlowIndependentTests(unittest.TestCase):
    def test_larger_than_initial_credit_continues_transfer_and_unpack_then_publishes_once(self) -> None:
        self._run_case(blocked=False)

    def test_native_blocked_poll_records_public_stop_without_settling_accepted_task(self) -> None:
        self._run_case(blocked=True)

    def _run_case(self, *, blocked: bool, large_policy_path: Path | None = None) -> None:
        # Reuse the established scratch-only fixture; do not inherit its tests.
        fixture = existing.StagedV4EndToEndIntegrationTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        policy = (_draft_policy_on_fixture_volume(large_policy_path, fixture.root)
                  if large_policy_path is not None else synthetic_storage_policy(
                      initial_result_estimate_bytes=4_096,
                  ))
        profile = replace(
            _profile(), contract_version=RESULT_STORAGE_PROCESS_PROFILE_CONTRACT,
            result_reservation_bytes=None, max_unacked_result_bytes=None,
            result_storage_policy_sha256=policy.sha256,
            # A release-bound storage profile must cash one maximal selected
            # source plus W. The legacy test profile's 4 GiB output ceiling
            # is not bindable to the general 12 GiB/4 GiB draft policy.
            **({
                "source_pdf_bytes_limit": policy.source_pdf_bytes_limit,
                "terminal_output_bytes_limit": (
                    policy.native_source_single_limit_bytes
                    + policy.mac_decode_working_set_budget_bytes
                ),
            } if large_policy_path is not None else {}),
        )
        worker = StagedWorkerProfileV4(profile.sha256, 1, 1)
        limits = staged_v4_coordinator_limits(profile, worker_profile=worker, storage_policy=policy)
        options = ParserOptions(
            method="auto", backend="hybrid-http-client", language="ch", formula=True,
            table=True, effort="medium", image_analysis=False, timeout_seconds=3600,
            api_url="https://mineru.invalid", api_drain_timeout_seconds=3600,
            server_url="http://mineru-openai-server:30000/v1", http_request_concurrency=7,
            runtime_bundle_identity_sha256=profile.runtime_bundle_identity_sha256,
        )
        attempt_id, fence = ids.new_id("rpa"), ids.new_id("fence")
        fixture.processing_run_id = str(ids.new_processing_run_id())
        ingress = MinerUV4InitialIngressFactory(
            source=BoundedV4SourcePdfObserver(paths=fixture.paths), paths=fixture.paths,
            parser_identity=ParserIdentity(name="MinerU", version="3.4.4"),
            parser_options=options, process_profile=profile,
            process_profile_exact_bytes=encode_mineru_process_profile(profile),
            worker_profile=worker, result_lease_seconds=300, remote_runaway_seconds=3600,
            archive_member_count_limit=8192,
            archive_uncompressed_byte_limit=profile.decoded_payload_bytes_limit,
            max_retries=3, scope_classes=None, storage_policy=policy,
            utc_now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
            attempt_id_factory=lambda: attempt_id, fence_id_factory=lambda: fence,
            processing_run_id_factory=lambda: fixture.processing_run_id,
            outbox_event_id_factory=lambda: ids.new_id("evt"),
        )
        command = ingress.build(
            V4OrdinaryParseCandidate(
                document_id=fixture.document_id, provider="cninfo",
                provider_document_id=fixture.provider_document_id,
                security_id=fixture.security_id, security_code="000001",
                raw_file_relpath=fixture.source_relpath.as_posix(),
                raw_file_hash=fixture.source_sha256,
                archived_raw_byte_count=len(fixture.source_bytes),
            ),
            source_observation=fixture.source.observe_source_pdf(fixture.source_relpath),
            available_credits=limits.credits,
        )
        admitted = DurableStagedIngressV4(uow_factory=unit_of_work_factory(fixture.engine)).execute(
            command, write_guard=lambda: None,
        )
        padding_digest: str | None = None
        if large_policy_path is None:
            artifact, selected_bytes, member_count = _expanded_official_zip(fixture.source_sha256)
        else:
            self.assertFalse(blocked)
            self.assertEqual(policy.initial_result_estimate_bytes, 256 * _MIB)
            artifact, selected_bytes, member_count, padding_digest = _large_official_zip(
                fixture.source_sha256, fixture.root / "large-provider-result.zip",
            )
            self.addCleanup(artifact.close)
            self.assertGreater(len(artifact), 256 * _MIB)
            self.assertLess(len(artifact), 260 * _MIB)
        reservation = admitted.reservation
        self.assertLess(policy.initial_result_estimate_bytes, len(artifact))
        self.assertLess(reservation.reserved_credit.provider_result_bytes, len(artifact))
        fake = _StorageResultProvider(
            attempt_id=attempt_id, fence_identity=fence,
            client_submit_key=canonical_client_submit_key_v2(
                source_pdf_sha256=fixture.source_sha256, attempt_identity=attempt_id,
                fence_identity=fence,
                submission_epoch_unix=int(datetime(2026, 9, 4, 12, tzinfo=UTC).timestamp()),
            ),
            source_pdf=fixture.source_bytes, artifact=artifact,
            policy_sha256=policy.sha256,
            inventory_sha256="sha256:" + hashlib.sha256(b"synthetic inventory").hexdigest(),
            selected_bytes=selected_bytes, member_count=member_count, blocked=blocked,
        )
        remote = MinerUHttpRemoteV4(
            transport=httpx.MockTransport(fake), token_factory=lambda count: b"t" * count,
            wall_clock=lambda: 1000.0, request_timeout_seconds=30.0,
            result_storage_policy_sha256=policy.sha256,
        )
        fixture.remote = remote
        scratch = fixture.settings.disclosure_runtime_root / "staged_v4" / "scratch"
        materializer = MinerUHttpStagedV4(
            scratch_root=scratch, published_root=fixture.paths.data_path(Path()),
            transport=remote, clock=lambda: 1000.0, storage_policy=policy,
        )
        uow = unit_of_work_factory(fixture.engine)
        resolver = ProductionV4StageInputResolver(
            uow_factory=uow, paths=fixture.paths, provider_source=fixture.source,
            worker_profile=worker, storage_policy=policy,
        )
        readiness = FilesystemAtomicPublicationArtifactReadinessV4(
            paths=fixture.paths, immutable_store=fixture.store,
            output_promotion=materializer,
        )
        builder = ProductionAtomicPublicationRequestBuilderV4(
            path_builder=fixture.paths, uow_factory=uow,
            admission=ProviderDocumentAdmission(path_builder=fixture.paths, source=fixture.source),
            semantic_router=existing._V4SemanticRouter(),
        )
        publisher = PrepareAndPublishWholeDocumentV4(
            uow_factory=uow,
            publication_requests=RecoverableAtomicPublicationRequestFactoryV4(
                readiness=readiness, new_request_builder=builder,
            ),
            readiness=readiness,
            publisher=PostgresAtomicWholeDocumentPublisherV4(engine=fixture.engine),
        )
        persistence = DurableStagedCoordinatorPersistenceV4(
            uow_factory=uow, limits=limits, owner_identity="storage-flow-winner",
        )
        backend = DurableStagedCoordinatorBackendV4(
            persistence=persistence, inputs=resolver, remote=remote,
            materialization=materializer,
            secret_cipher=AesGcmProviderSecretCipher(keyring=StaticProviderSecretKeyring(
                primary_kek_id="kek-storage", keks={"kek-storage": b"s" * 32},
            )),
            claim_guard=DurableV4ClaimGuard(uow_factory=uow), publisher=publisher,
            poll_seconds=0.001 if blocked else 1.0, wall_clock=lambda: 1000.0,
        )
        stop_store = WorkerControlStore.for_settings(fixture.settings) if blocked else None
        stop_control = (RuntimeWorkerStopControl(
            store=stop_store, supervisor=None, emit=lambda _line: None,
        ) if stop_store is not None else None)
        grant_requirements = []
        continuation_causes = []
        unpack_pause = [True]
        intents = []
        materializer_exceptions: list[str] = []
        original_remote = backend.run_remote
        original_prepare_local = backend.prepare_local_io
        original_local = backend.run_local
        original_materialize = materializer.materialize_v4

        def observe_materialize(**kwargs):
            try:
                return original_materialize(**kwargs)
            except Exception as exc:
                materializer_exceptions.append("".join(traceback.format_exception(exc)))
                raise

        def observe_remote(work, *, credit_allowance, stage_guard):
            try:
                return original_remote(work, credit_allowance=credit_allowance, stage_guard=stage_guard)
            except StageResourceGrantRequired as exc:
                grant_requirements.append(exc.required)
                raise

        def observe_local(work, *, credit_allowance, stage_guard):
            with uow() as tx:
                authority = tx.remote_parse_v4.load(attempt_id)
            intent = next(item.value for item in authority.evidence if item.kind == "materialization_intent")
            intents.append(intent)
            first_member = scratch / intent.staging_relpath / ".unpack" / "images" / "continuation.jpg"
            bounded_guard = _NearUnpackDeadline(stage_guard, first_member, unpack_pause)
            try:
                return original_local(work, credit_allowance=credit_allowance, stage_guard=bounded_guard)
            except StageWaiting as exc:
                continuation_causes.append(type(exc.__cause__))
                if isinstance(exc.__cause__, MaterializationUnpackContinuesV4):
                    unpack_pause[0] = False
                raise

        def observe_prepare_local(work, *, credit_allowance, stage_guard):
            try:
                return original_prepare_local(
                    work, credit_allowance=credit_allowance, stage_guard=stage_guard,
                )
            except StageResourceGrantRequired as exc:
                grant_requirements.append(exc.required)
                raise

        def cleanup_before_ack() -> bool:
            if not intents:
                return False
            intent = intents[-1]
            return all(not (scratch / path).exists() for path in (
                reservation.snapshot_relpath, intent.spool_relpath, intent.output_relpath,
            ))

        fake.cleanup_probe = cleanup_before_ack
        with (patch.object(backend, "run_remote", side_effect=observe_remote),
              patch.object(backend, "prepare_local_io", side_effect=observe_prepare_local),
              patch.object(backend, "run_local", side_effect=observe_local),
              patch.object(materializer, "materialize_v4", side_effect=observe_materialize)):
            result = StagedParseCoordinator(
                backend=backend, limits=limits, stop_control=stop_control,
            ).run()
        with uow() as tx:
            observed = tx.remote_parse_v4.load(attempt_id)
        failures = tuple(item.value for item in observed.evidence if item.kind == "failure_receipt")
        with fixture.engine.connect() as conn:
            run_failure = conn.execute(sa.text(
                "SELECT status,error FROM disclosure_core.processing_run "
                "WHERE processing_run_id=:run"
            ), {"run": fixture.processing_run_id}).mappings().one()
        diagnostic = {
            "coordinator": repr(result),
            "durable_state": observed.state,
            "durable_history": tuple(item.state for item in observed.checkpoint_history),
            "evidence_kinds": tuple(item.kind for item in observed.evidence),
            "failure_receipts": tuple({
                "outcome": item.outcome, "stage": item.error_stage,
                "code": item.error_code, "class": item.error_class,
                "message": item.message,
            } for item in failures),
            "processing_run": dict(run_failure),
            "grant_requirements": tuple(repr(item) for item in grant_requirements),
            "continuation_causes": tuple(item.__name__ for item in continuation_causes),
            "materializer_exceptions": tuple(materializer_exceptions),
            "provider_calls": tuple(fake.calls),
            "result_offsets": tuple(fake.offsets),
            "unpack_pause_pending": unpack_pause[0],
        }
        if blocked:
            self.assertEqual(result.termination_kind, "public_stop", msg=repr(diagnostic))
            cause = result.stop_cause
            assert cause is not None
            self.assertEqual((cause.kind, cause.reason_code, cause.attempt_id,
                              cause.lane, cause.state_at_dispatch),
                             ("coordinator_circuit", "native_storage_hold",
                              attempt_id, "remote", "submitted"), msg=repr(diagnostic))
            assert stop_store is not None
            active = stop_store.read_active()
            self.assertEqual(active.status, "valid", msg=repr(active))
            assert active.record is not None
            self.assertEqual(active.record.cause, cause)
            self.assertEqual(observed.state, "submitted")
            self.assertEqual(tuple(item.state for item in observed.checkpoint_history),
                             ("prepared", "reconciling", "submitted"))
            self.assertFalse({"failure_receipt", "cleanup_plan", "cleanup_receipt", "ack_receipt"}
                             & {item.kind for item in observed.evidence})
            self.assertEqual((fake.task_posts, fake.result_gets, fake.ack_posts), (1, 0, 0))
            self.assertEqual(fake.status_polls, 1)
            self.assertGreater(result.credits_in_use.provider_tasks, 0)
            return
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT, msg=repr(diagnostic))
        self.assertEqual((result.admitted, result.completed), (0, 1))
        self.assertEqual(result.final_states, ((attempt_id, "acked"),), msg=repr(diagnostic))
        self.assertEqual(result.errors, ())
        self.assertEqual(len(grant_requirements), 2)
        self.assertGreater(grant_requirements[0].provider_result_bytes,
                           reservation.reserved_credit.provider_result_bytes)
        self.assertIn(MaterializationTransferContinuesV4, continuation_causes)
        self.assertIn(MaterializationUnpackContinuesV4, continuation_causes)
        self.assertEqual(fake.offsets, [0, _DURABLE_PREFIX_BYTES])
        self.assertFalse(unpack_pause[0])
        with uow() as tx:
            final = tx.remote_parse_v4.load(attempt_id)
        intent = next(item.value for item in final.evidence if item.kind == "materialization_intent")
        self.assertIsNotNone(intent.resource_grant)
        self.assertGreater(intent.resource_grant.artifact_byte_count,
                           reservation.reserved_credit.provider_result_bytes)
        receipt = next(item.value for item in final.evidence if item.kind == "local_materialization_receipt")
        PublishedParserOutputVerifierV4(fixture.paths).verify_published(
            published_relpath=intent.provider_envelope_context.parser_artifact_root_relpath,
            expected_inventory_sha256=receipt.output_files_sha256,
            expected_file_count=receipt.output_file_count,
            expected_byte_count=receipt.output_byte_count,
        )
        if padding_digest is not None:
            published_padding = fixture.paths.data_path(Path(
                intent.provider_envelope_context.parser_artifact_root_relpath,
            )) / "capacity-padding.bin"
            exact_padding = hashlib.sha256()
            byte_count = 0
            with published_padding.open("rb") as source:
                for chunk in iter(lambda: source.read(_MIB), b""):
                    byte_count += len(chunk)
                    exact_padding.update(chunk)
            self.assertEqual(byte_count, 257 * _MIB)
            self.assertEqual(exact_padding.hexdigest(), padding_digest)
        with fixture.engine.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT state,is_current FROM disclosure_ops.remote_parse_attempt "
                "WHERE attempt_id=:attempt"
            ), {"attempt": attempt_id}).one()
            winner_count = conn.execute(sa.text(
                "SELECT count(*) FROM disclosure_ops.atomic_publication_winner_v4 "
                "WHERE attempt_id=:attempt"
            ), {"attempt": attempt_id}).scalar_one()
            task_count = conn.execute(sa.text(
                "SELECT count(*) FROM disclosure_ops.remote_parse_attempt WHERE document_id=:document"
            ), {"document": fixture.document_id}).scalar_one()
            document = conn.execute(sa.text(
                "SELECT status,current_processing_run_id FROM disclosure_core.document "
                "WHERE document_id=:document"
            ), {"document": fixture.document_id}).one()
            unit_count = conn.execute(sa.text(
                "SELECT count(*) FROM disclosure_core.document_unit WHERE processing_run_id=:run"
            ), {"run": fixture.processing_run_id}).scalar_one()
        self.assertEqual((rows.state, rows.is_current), ("acked", False))
        self.assertEqual(winner_count, 1)
        self.assertEqual(task_count, 1)
        self.assertEqual((document.status, document.current_processing_run_id),
                         ("published", fixture.processing_run_id))
        self.assertGreater(unit_count, 0)
        self.assertEqual((fake.task_posts, fake.ack_posts), (1, 1))
        self.assertLess(fake.calls.index(f"GET /tasks/{fake.task_id}/result"),
                        fake.calls.index(f"POST /tasks/{fake.task_id}/ack"))


def run_large_storage_result_flow(policy_path: Path) -> None:
    """Explicit scratch-runner entry point; never collected by default unittest."""
    case = StorageResultFlowIndependentTests(methodName="runTest")
    try:
        case._run_case(blocked=False, large_policy_path=policy_path)
    finally:
        case.doCleanups()


def large_storage_result_flow_opt_in() -> unittest.FunctionTestCase:
    """A named unittest target for the managed scratch runner, absent from discovery."""
    policy_path = Path(os.environ["DISCLOSURE_LARGE_STORAGE_POLICY_PATH"])
    return unittest.FunctionTestCase(
        lambda: run_large_storage_result_flow(policy_path),
        description="one >256 MiB storage-era result through publication and ACK",
    )
