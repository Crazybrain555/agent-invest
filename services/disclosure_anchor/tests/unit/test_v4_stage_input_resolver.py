from __future__ import annotations

from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from typing import cast

from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import (
    TASK_PROTOCOL_V2,
    canonical_client_submit_key_v2,
    submission_form_v2,
    submission_request_exact_bytes_v2,
)
from disclosure_anchor.adapters.parsers.mineru_medium.v4_stage_input_resolver import (
    ProductionV4StageInputResolver,
)
from disclosure_anchor.application.contracts.mineru_process_profile import (
    encode_mineru_process_profile,
)
from disclosure_anchor.application.contracts.provider_document_admission import (
    SourcePdfObservation,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    AcceptedSubmissionReceiptV4,
    EvidenceValueV4,
    SnapshotReceiptV4,
    SubmissionIntentV4,
    TerminalReceiptV4,
    encode_remote_parse_evidence_v4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    RemoteParseCheckpointV4,
    advance_remote_parse_checkpoint_v4,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
    build_staged_resource_credit_envelope,
)
from disclosure_anchor.application.contracts.v4_prepared_execution_spec import (
    V4_PREPARED_EXECUTION_SPEC_CONTRACT,
    V4PreparedExecutionSpec,
)
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions
from disclosure_anchor.application.ports.file_store import FileStorePathPort
from disclosure_anchor.application.ports.provider_document_source import (
    ProviderDocumentSourcePort,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RemoteParseV4Authority,
    V4PreparedProposal,
    bind_v4_prepared_proposal,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    PreparedSubmissionIdentity,
    PrivateProviderCapabilityV4,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.staged_parse_coordinator import (
    StageLeaseGuard,
    StageLeaseLost,
)
from disclosure_anchor.domain.entities import Document, ProcessingRun, Security
from tests.unit._fakes import FakeUnitOfWork
from tests.unit.test_mineru_process_profile import _profile


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


class _Paths:
    def __init__(self, root: Path) -> None:
        self.root = root

    def data_path(self, relpath: Path) -> Path:
        return self.root / relpath

    def parser_run_artifacts_v4_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
        source_pdf_sha256: str,
        parser_backend: str,
        parser_method: str,
    ) -> Path:
        digest = source_pdf_sha256.removeprefix("sha256:")
        return Path(
            "parser_artifacts",
            provider,
            security_code,
            provider_document_id,
            processing_run_id,
            f"sha256_{digest}",
            f"{parser_backend.split('-', 1)[0]}_{parser_method}",
        )

    def provider_document_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        artifact_owner_processing_run_id: str,
    ) -> Path:
        return Path(
            "derived/provider_documents",
            provider,
            security_code,
            provider_document_id,
            artifact_owner_processing_run_id,
            "provider_document.json",
        )


class _Source:
    def __init__(self, observation: SourcePdfObservation) -> None:
        self.observation = observation
        self.relpaths: list[Path] = []

    def observe_source_pdf(self, relpath: Path) -> SourcePdfObservation:
        self.relpaths.append(relpath)
        return self.observation


class _SnapshotSource:
    def __init__(
        self,
        *,
        snapshot: SnapshotReceiptV4,
        intent: object,
    ) -> None:
        self.snapshot = snapshot
        self.intent = intent

    def validates(
        self,
        *,
        submission_intent: object,
        snapshot_receipt: SnapshotReceiptV4,
    ) -> bool:
        return submission_intent == self.intent and snapshot_receipt == self.snapshot

    def open(self, *, step_guard: object) -> object:
        raise AssertionError("resolver tests never open the snapshot")


class V4StageInputResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.source_bytes = b"%PDF-1.7\nimmutable fixture\n"
        self.source_sha256 = _digest(self.source_bytes)
        self.source_relpath = Path(
            "raw_documents",
            "cninfo",
            "600001",
            "2024",
            "provider-doc-1",
            f"sha256_{self.source_sha256.removeprefix('sha256:')}.pdf",
        )
        source_path = self.root / self.source_relpath
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(self.source_bytes)

        self.profile = _profile()
        self.identity = ParserIdentity(name="MinerU", version="3.4.4")
        self.options = ParserOptions(
            method="auto",
            backend="hybrid-http-client",
            language="ch",
            formula=True,
            table=True,
            effort="medium",
            image_analysis=False,
            timeout_seconds=21_600,
            api_url="http://127.0.0.1:30003",
            api_drain_timeout_seconds=86_400,
            server_url="http://mineru-openai-server:30000/v1",
            http_request_concurrency=7,
            runtime_bundle_identity_sha256=(
                self.profile.runtime_bundle_identity_sha256
            ),
        )
        self.submission_epoch = 1_725_000_000
        client_key = canonical_client_submit_key_v2(
            source_pdf_sha256=self.source_sha256,
            attempt_identity="attempt-1",
            fence_identity="fence-1",
            submission_epoch_unix=self.submission_epoch,
        )
        upload_filename = "sha256_" + self.source_sha256.removeprefix("sha256:") + ".pdf"
        request = submission_request_exact_bytes_v2(
            api_origin=self.options.api_url or "",
            form=submission_form_v2(
                self.options,
                server_url=self.options.server_url or "",
            ),
            upload_filename=upload_filename,
        )
        target_payload = self.options.target_identity(self.identity).to_payload()
        target_sha256 = _digest(
            json.dumps(
                target_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        prepared_payload = {
            "schema": "mineru-prepared-submission.v1",
            "attempt_identity": "attempt-1",
            "fence_identity": "fence-1",
            "source_pdf_sha256": self.source_sha256,
            "parser_target_identity_sha256": target_sha256,
            "runtime_bundle_identity_sha256": (
                self.profile.runtime_bundle_identity_sha256
            ),
            "request_sha256": _digest(request),
            "client_submit_key": client_key,
            "submission_epoch_unix": self.submission_epoch,
        }
        prepared_bytes = json.dumps(
            prepared_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        prepared = PreparedSubmissionIdentity(
            schema="mineru-prepared-submission.v1",
            attempt_identity="attempt-1",
            fence_identity="fence-1",
            source_pdf_sha256=self.source_sha256,
            parser_target_identity_sha256=target_sha256,
            runtime_bundle_identity_sha256=(
                self.profile.runtime_bundle_identity_sha256
            ),
            request_sha256=_digest(request),
            client_submit_key=client_key,
            submission_epoch_unix=self.submission_epoch,
            exact_bytes=prepared_bytes,
            sha256=_digest(prepared_bytes),
        )
        profile_bytes = encode_mineru_process_profile(self.profile)
        self.spec = V4PreparedExecutionSpec(
            contract_version=V4_PREPARED_EXECUTION_SPEC_CONTRACT,
            prepared_submission=prepared,
            parser_identity=self.identity,
            parser_options=self.options,
            api_origin=self.options.api_url or "",
            server_url=self.options.server_url or "",
            request_exact_bytes=request,
            request_sha256=_digest(request),
            process_profile_exact_bytes=profile_bytes,
            process_profile_sha256=self.profile.sha256,
            worker_profile=StagedWorkerProfileV4(self.profile.sha256, 1, 1),
            result_lease_seconds=300,
            remote_runaway_seconds=86_400,
            archive_member_count_limit=10_000,
            archive_uncompressed_byte_limit=128 * 1024 * 1024,
        )
        envelope = build_staged_resource_credit_envelope(
            profile=self.profile,
            source_pdf_sha256=self.source_sha256,
            source_byte_count=len(self.source_bytes),
            source_page_count=2,
        )
        proposal = V4PreparedProposal(
            document_id="document-1",
            processing_run_id="run-1",
            prepared_submission=prepared,
            credit_envelope=envelope,
            execution_spec=self.spec,
        )
        creation = bind_v4_prepared_proposal(proposal, attempt_generation=1)
        self.reservation = creation.reservation
        self.preparation = creation.preparation_intent
        self.h0 = creation.checkpoint
        self.snapshot = SnapshotReceiptV4(
            attempt_id="attempt-1",
            fence_identity="fence-1",
            preparation_intent_sha256=self.preparation.sha256,
            snapshot_relpath=self.reservation.snapshot_relpath,
            snapshot_sha256=self.source_sha256,
            snapshot_byte_count=len(self.source_bytes),
            part_path_absent=True,
            part_owner_path_absent=True,
            file_fsync_completed=True,
            parent_fsync_completed=True,
        )
        self.paths = _Paths(self.root)
        parser_artifact = self.paths.parser_run_artifacts_v4_relpath(
            provider="cninfo",
            security_code="600001",
            provider_document_id="provider-doc-1",
            processing_run_id="run-1",
            source_pdf_sha256=self.source_sha256,
            parser_backend=self.options.backend,
            parser_method=self.options.method,
        )
        provider_document = self.paths.provider_document_relpath(
            provider="cninfo",
            security_code="600001",
            provider_document_id="provider-doc-1",
            artifact_owner_processing_run_id="run-1",
        )
        self.uow = FakeUnitOfWork()
        self.uow.securities.add(
            Security(
                security_id="security-1",
                company_id="company-1",
                security_code="600001",
                exchange="SSE",
            )
        )
        self.document = self.uow.documents.add(
            Document(
                document_id="document-1",
                status="archived",
                security_id="security-1",
                provider="cninfo",
                provider_document_id="provider-doc-1",
                raw_file_relpath=self.source_relpath.as_posix(),
                raw_file_hash=self.source_sha256,
            )
        )
        self.processing_run = self.uow.processing_runs.add(
            ProcessingRun(
                processing_run_id="run-1",
                document_id="document-1",
                artifact_owner_processing_run_id="run-1",
                run_kind="parse",
                status="running",
                parser_name=self.identity.name,
                parser_version=self.identity.version,
                parser_backend=self.options.backend,
                parser_method=self.options.method,
                parser_language=self.options.language,
                parser_target_identity=target_payload,
                input_raw_file_hash=self.source_sha256,
                parser_artifact_relpath=parser_artifact.as_posix(),
                provider_document_relpath=provider_document.as_posix(),
                is_active=False,
            )
        )
        self.source = _Source(
            SourcePdfObservation(
                sha256=self.source_sha256,
                byte_count=len(self.source_bytes),
                page_count=2,
            )
        )
        self.resolver = ProductionV4StageInputResolver(
            uow_factory=lambda: cast(UnitOfWork, self.uow),
            paths=cast(FileStorePathPort, self.paths),
            provider_source=cast(ProviderDocumentSourcePort, self.source),
            worker_profile=self.spec.worker_profile,
        )
        self.guard = StageLeaseGuard(
            deadline_monotonic=100,
            _revoked=Event(),
            _monotonic=lambda: 1,
        )
        hydration = patch.object(self.resolver, "_identity", wraps=self.resolver._identity)
        self.hydrations = hydration.start()
        self.addCleanup(hydration.stop)

    def test_stage_hydrates_spec_once_but_keeps_document_and_source_checks_fresh(self) -> None:
        authority = self._authority((self.h0,), (self.preparation,))
        stage = self.resolver.bind_stage(authority, stage_guard=self.guard)
        stage.source_pdf(authority)
        stage.submission_intent(authority, self.snapshot)
        stage.result_lease_seconds(authority)
        stage.remote_runaway_seconds(authority)
        self.assertEqual(self.hydrations.call_count, 1)
        self.processing_run.parser_language = "en"
        with self.assertRaisesRegex(ValueError, "document/run facts drifted"):
            stage.source_pdf(authority)
        self.processing_run.parser_language = self.options.language
        self.source.observation = replace(self.source.observation, page_count=3)
        with self.assertRaisesRegex(ValueError, "hash/byte/page"):
            stage.source_pdf(authority)
        self.assertEqual(self.hydrations.call_count, 1)

    def test_each_new_stage_reopens_exact_spec_and_rejects_changed_durable_bytes(self) -> None:
        authority = self._authority((self.h0,), (self.preparation,))
        first = self.resolver.bind_stage(authority, stage_guard=self.guard)
        second = self.resolver.bind_stage(authority, stage_guard=self.guard)
        self.assertIsNot(first, second)
        self.assertEqual(self.hydrations.call_count, 2)
        authority = replace(authority, execution_spec=replace(self.spec, remote_runaway_seconds=86_401))
        with self.assertRaisesRegex(ValueError, "wrong exact object"):
            self.resolver.bind_stage(authority, stage_guard=self.guard)
        self.assertEqual(self.hydrations.call_count, 3)

    def test_stage_context_cannot_cross_authority_rebind_or_survive_revocation(self) -> None:
        authority = self._authority((self.h0,), (self.preparation,))
        stage = self.resolver.bind_stage(authority, stage_guard=self.guard)
        with self.assertRaisesRegex(ValueError, "another exact stage"):
            stage.source_pdf(replace(authority))
        with self.assertRaisesRegex(ValueError, "cannot authorize another stage"):
            stage.bind_stage(authority, stage_guard=self.guard)
        self.guard.revoke()
        with self.assertRaises(StageLeaseLost):
            stage.result_lease_seconds(authority)
        self.assertEqual(self.source.relpaths, [])

    def test_resolves_source_and_rechecks_hash_byte_page_before_post(self) -> None:
        prepared = self._authority((self.h0,), (self.preparation,))
        self.assertEqual(
            self.resolver.source_pdf(prepared),
            self.root / self.source_relpath,
        )
        intent = self.resolver.submission_intent(prepared, self.snapshot)
        reconciling = advance_remote_parse_checkpoint_v4(
            self.h0,
            state="reconciling",
            held_resource_credit=ResourceCreditVector(
                documents=1,
                snapshot_items=1,
                snapshot_bytes=len(self.source_bytes),
                remote_waits=1,
            ),
            snapshot_receipt_sha256=self.snapshot.sha256,
            submission_intent_sha256=intent.sha256,
        )
        authority = self._authority(
            (self.h0, reconciling),
            (self.preparation, self.snapshot, intent),
        )
        source = _SnapshotSource(snapshot=self.snapshot, intent=intent)
        command = self.resolver.submission_command(
            authority,
            snapshot=self.snapshot,
            intent=intent,
            snapshot_source=source,  # type: ignore[arg-type]
            stage_guard=self.guard,
        )
        self.assertEqual(command.request_exact_bytes, self.spec.request_exact_bytes)
        self.assertEqual(command.parser_options, self.options)

        for changed in (
            replace(self.source.observation, sha256="sha256:" + "f" * 64),
            replace(self.source.observation, byte_count=len(self.source_bytes) + 1),
            replace(self.source.observation, page_count=3),
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(
                ValueError, "hash/byte/page"
            ):
                self.source.observation = changed
                self.resolver.submission_command(
                    authority,
                    snapshot=self.snapshot,
                    intent=intent,
                    snapshot_source=source,  # type: ignore[arg-type]
                    stage_guard=self.guard,
                )
        self.assertGreaterEqual(len(self.source.relpaths), 4)

    def test_recovery_rejects_local_composition_drift_before_source_io(self) -> None:
        prepared = self._authority((self.h0,), (self.preparation,))
        for change in (
            {"mac_preflight_workers": 2}, {"mac_finalize_workers": 2},
            {"provider_poll_milliseconds": 2000}, {"admission_probe_milliseconds": 2000},
        ):
            with self.subTest(change=change):
                resolver = ProductionV4StageInputResolver(
                    uow_factory=lambda: cast(UnitOfWork, self.uow),
                    paths=cast(FileStorePathPort, self.paths),
                    provider_source=cast(ProviderDocumentSourcePort, self.source),
                    worker_profile=replace(self.spec.worker_profile, **change),
                )
                with self.assertRaisesRegex(ValueError, "worker composition changed"):
                    resolver.source_pdf(prepared)
        self.assertEqual(self.source.relpaths, [])

    def test_profile_only_recovery_fence_does_not_reopen_cleaned_source(self) -> None:
        prepared = self._authority((self.h0,), (self.preparation,))
        (self.root / self.source_relpath).unlink()
        self.resolver.assert_execution_profile(prepared)
        self.assertEqual(self.source.relpaths, [])

    def test_poll_reopens_frozen_endpoint_limits_and_lease(self) -> None:
        _, intent, reconciling = self._reconciling()
        accepted, capability = self._accepted(
            intent,
            purpose="submitted_task_resume",
        )
        submitted = advance_remote_parse_checkpoint_v4(
            reconciling,
            state="submitted",
            held_resource_credit=ResourceCreditVector(
                documents=1,
                snapshot_items=1,
                snapshot_bytes=len(self.source_bytes),
                remote_waits=1,
                provider_tasks=1,
                ack_items=1,
            ),
            accepted_submission_sha256=accepted.sha256,
        )
        authority = self._authority(
            (self.h0, reconciling, submitted),
            (self.preparation, self.snapshot, intent, accepted),
        )
        command = self.resolver.poll_command(
            authority,
            intent=intent,
            accepted=accepted,
            capability=capability,
            stage_guard=self.guard,
        )
        self.assertEqual(
            command.artifact_byte_limit,
            self.reservation.reserved_credit.provider_result_bytes,
        )
        self.assertEqual(command.result_lease_seconds, 300)
        self.assertEqual(self.resolver.remote_runaway_seconds(authority), 86_400)

    def test_materialization_rebuilds_authoritative_paths_and_allowance(self) -> None:
        authority, accepted, terminal, capability = self._remote_terminal()
        intent = self.resolver.materialization_intent(
            authority,
            accepted=accepted,
            terminal=terminal,
            capability=capability,
        )
        self.assertEqual(
            intent.provider_envelope_context.source_pdf_relpath,
            self.source_relpath.as_posix(),
        )
        self.assertEqual(
            intent.provider_envelope_context.parser_artifact_root_relpath,
            self.processing_run.parser_artifact_relpath,
        )
        self.assertEqual(intent.member_count_limit, 10_000)
        self.assertEqual(intent.uncompressed_byte_limit, 128 * 1024 * 1024)

        materializing = advance_remote_parse_checkpoint_v4(
            authority.checkpoint,
            state="materializing",
            held_resource_credit=intent.held_resource_credit,
            materialization_intent_sha256=intent.sha256,
        )
        materializing_authority = self._authority(
            (*authority.checkpoint_history, materializing),
            (
                self.preparation,
                self.snapshot,
                self._evidence(authority, "submission_intent"),
                accepted,
                terminal,
                intent,
            ),
        )
        allowance = self.resolver.materialization_allowance(
            materializing_authority,
            intent,
        )
        self.assertEqual(allowance.limits, self.reservation.reserved_credit)
        self.assertEqual(
            allowance.reservation_input_sha256,
            self.reservation.reservation_input_sha256,
        )
        self.assertEqual(
            self.resolver.result_lease_seconds(materializing_authority),
            self.spec.result_lease_seconds,
        )

    def test_wrong_authority_spec_and_authoritative_run_drift_fail_closed(self) -> None:
        authority = self._authority((self.h0,), (self.preparation,))
        with self.assertRaisesRegex(ValueError, "wrong exact object"):
            self.resolver.source_pdf(replace(authority, execution_spec=replace(self.spec, remote_runaway_seconds=86_401)))

        self.processing_run.parser_language = "en"
        with self.assertRaisesRegex(ValueError, "document/run facts drifted"):
            self.resolver.source_pdf(authority)

    def _reconciling(
        self,
    ) -> tuple[
        RemoteParseV4Authority,
        SubmissionIntentV4,
        RemoteParseCheckpointV4,
    ]:
        prepared = self._authority((self.h0,), (self.preparation,))
        intent = self.resolver.submission_intent(prepared, self.snapshot)
        reconciling = advance_remote_parse_checkpoint_v4(
            self.h0,
            state="reconciling",
            held_resource_credit=ResourceCreditVector(
                documents=1,
                snapshot_items=1,
                snapshot_bytes=len(self.source_bytes),
                remote_waits=1,
            ),
            snapshot_receipt_sha256=self.snapshot.sha256,
            submission_intent_sha256=intent.sha256,
        )
        return prepared, intent, reconciling

    def _accepted(
        self,
        intent: SubmissionIntentV4,
        *,
        purpose: str = "result_download",
    ) -> tuple[AcceptedSubmissionReceiptV4, PrivateProviderCapabilityV4]:
        token = b"result-capability"
        accepted = AcceptedSubmissionReceiptV4(
            attempt_id="attempt-1",
            fence_identity="fence-1",
            submission_intent_sha256=intent.sha256,
            remote_task_identity="task-1",
            status_url="http://127.0.0.1:30003/tasks/task-1",
            result_url="http://127.0.0.1:30003/tasks/task-1/result",
            secret_kind="mineru-task-token.v1",
            secret_version=1,
            token_sha256=_digest(token),
            token_byte_count=len(token),
            provider_protocol_version=TASK_PROTOCOL_V2,
        )
        capability = PrivateProviderCapabilityV4(
            attempt_id="attempt-1",
            remote_task_identity="task-1",
            provider_protocol_version=TASK_PROTOCOL_V2,
            secret_kind=accepted.secret_kind,
            secret_version=accepted.secret_version,
            capability_purpose=purpose,
            token_bytes=token,
            token_sha256=accepted.token_sha256,
            token_byte_count=accepted.token_byte_count,
        )
        return accepted, capability

    def _remote_terminal(
        self,
    ) -> tuple[
        RemoteParseV4Authority,
        AcceptedSubmissionReceiptV4,
        TerminalReceiptV4,
        PrivateProviderCapabilityV4,
    ]:
        _, intent, reconciling = self._reconciling()
        accepted, capability = self._accepted(intent)
        submitted = advance_remote_parse_checkpoint_v4(
            reconciling,
            state="submitted",
            held_resource_credit=ResourceCreditVector(
                documents=1,
                snapshot_items=1,
                snapshot_bytes=len(self.source_bytes),
                remote_waits=1,
                provider_tasks=1,
                ack_items=1,
            ),
            accepted_submission_sha256=accepted.sha256,
        )
        terminal = TerminalReceiptV4(
            attempt_id="attempt-1",
            fence_identity="fence-1",
            accepted_submission_receipt_sha256=accepted.sha256,
            remote_task_identity="task-1",
            result_owner_identity="result-owner-1",
            artifact_sha256="sha256:" + "d" * 64,
            artifact_byte_count=1024,
            provider_protocol_version=TASK_PROTOCOL_V2,
        )
        remote_terminal = advance_remote_parse_checkpoint_v4(
            submitted,
            state="remote_terminal",
            held_resource_credit=ResourceCreditVector(
                documents=1,
                snapshot_items=1,
                snapshot_bytes=len(self.source_bytes),
                provider_tasks=1,
                provider_result_bytes=terminal.artifact_byte_count,
                ack_items=1,
            ),
            terminal_receipt_sha256=terminal.sha256,
        )
        authority = self._authority(
            (self.h0, reconciling, submitted, remote_terminal),
            (self.preparation, self.snapshot, intent, accepted, terminal),
        )
        return authority, accepted, terminal, capability

    def _authority(
        self,
        history: tuple[RemoteParseCheckpointV4, ...],
        evidence_values: tuple[EvidenceValueV4, ...],
    ) -> RemoteParseV4Authority:
        checkpoint = history[-1]
        prepared = self.spec.prepared_submission
        return RemoteParseV4Authority(
            attempt_id=checkpoint.attempt_id,
            processing_run_id=checkpoint.processing_run_id,
            document_id=checkpoint.document_id,
            attempt_generation=checkpoint.attempt_generation,
            fence_identity=checkpoint.fence_identity,
            source_pdf_sha256=checkpoint.source_pdf_sha256,
            parser_target_sha256=prepared.parser_target_identity_sha256,
            request_sha256=checkpoint.request_sha256,
            runtime_epoch_sha256=checkpoint.runtime_epoch_sha256,
            client_submit_key=prepared.client_submit_key,
            state=checkpoint.state,
            is_current=True,
            lifecycle_version=checkpoint.lifecycle_version,
            checkpoint_sha256=checkpoint.sha256,
            claim_generation=1,
            claim_owner_identity="boot-1",
            claim_lease_until=datetime(2030, 1, 1, tzinfo=UTC),
            checkpoint_history=history,
            reservation=self.reservation,
            evidence=tuple(
                encode_remote_parse_evidence_v4(value)
                for value in evidence_values
            ),
            publication_winner=None,
            secret_history=(),
            source_supersession_link=None,
            staged_by_link=None,
            database_lease=None,
            execution_spec=self.spec,
        )

    @staticmethod
    def _evidence(
        authority: RemoteParseV4Authority,
        kind: str,
    ) -> EvidenceValueV4:
        return next(item.value for item in authority.evidence if item.kind == kind)


if __name__ == "__main__":
    unittest.main()
