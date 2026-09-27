"""In-memory V4 duties bound to any profile pair, for the local-upgrade boundary tests.

Generalised from the resolver unit fixture: one ``DutyWorld`` holds several attempts in one fake
unit of work (legacy members under R0/P0/WP0, a non-member under the same parent pair, new heads
under R1/P1/WP1), each with its real closed H0, execution spec, reservation, preparation intent,
snapshot receipt and submission intent. No database, provider or model is involved.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from threading import Event
from typing import Any, cast

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
    MineruProcessProfile,
    encode_mineru_process_profile,
)
from disclosure_anchor.application.contracts.provider_document_admission import SourcePdfObservation
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    AcceptedSubmissionReceiptV4,
    EvidenceValueV4,
    SnapshotReceiptV4,
    SubmissionIntentV4,
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
from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4
from disclosure_anchor.application.contracts.v4_prepared_execution_spec import (
    V4_PREPARED_EXECUTION_SPEC_CONTRACT,
    V4PreparedExecutionSpec,
)
from disclosure_anchor.application.contracts.worker_execution_upgrade import VerifiedQualifiedExecution
from disclosure_anchor.application.ports.file_store import FileStorePathPort
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions
from disclosure_anchor.application.ports.provider_document_source import ProviderDocumentSourcePort
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RemoteParseV4Authority,
    V4PreparedProposal,
    bind_v4_prepared_proposal,
)
from disclosure_anchor.application.ports.staged_provider_parser import PreparedSubmissionIdentity
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.domain.entities import Document, ProcessingRun, Security
from tests.unit._fakes import FakeUnitOfWork


API_URL = "http://127.0.0.1:30002"
SERVER_URL = "http://mineru-openai-server:30000/v1"
PARSER = ParserIdentity(name="MinerU", version="3.4.4")
SUBMISSION_EPOCH = 1_790_200_000


def digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class StageGuard:
    """A live stage guard that never expires (checkpoint/remaining/note, like the lease guard)."""

    def __init__(self) -> None:
        self.notes: list[tuple[str, dict[str, Any]]] = []
        self.revoked = Event()

    def checkpoint(self) -> None:
        if self.revoked.is_set():
            raise RuntimeError("stage guard revoked")

    def remaining_seconds(self) -> float:
        self.checkpoint()
        return 60.0

    def note(self, kind: str, **scalars: Any) -> None:
        self.notes.append((kind, scalars))


class SnapshotSource:
    """The pinned snapshot the POST streams; it validates only its own intent and receipt."""

    def __init__(self, path: Path, intent: SubmissionIntentV4, receipt: SnapshotReceiptV4) -> None:
        self.path, self.intent, self.receipt = path, intent, receipt
        self.opens = 0

    def validates(self, *, submission_intent: SubmissionIntentV4, snapshot_receipt: SnapshotReceiptV4) -> bool:
        return submission_intent == self.intent and snapshot_receipt == self.receipt

    def open(self, *, step_guard: Any) -> Any:
        from contextlib import contextmanager

        @contextmanager
        def opened() -> Any:
            step_guard.checkpoint()
            self.opens += 1
            with self.path.open("rb") as stream:
                yield stream

        return opened()


class _Paths:
    def __init__(self, root: Path) -> None:
        self.root = root

    def data_path(self, relpath: Path) -> Path:
        return self.root / relpath

    def parser_run_artifacts_v4_relpath(
        self, *, provider: str, security_code: str, provider_document_id: str, processing_run_id: str,
        source_pdf_sha256: str, parser_backend: str, parser_method: str,
    ) -> Path:
        return Path(
            "parser_artifacts", provider, security_code, provider_document_id, processing_run_id,
            f"sha256_{source_pdf_sha256.removeprefix('sha256:')}", f"{parser_backend.split('-', 1)[0]}_{parser_method}",
        )

    def provider_document_relpath(
        self, *, provider: str, security_code: str, provider_document_id: str,
        artifact_owner_processing_run_id: str,
    ) -> Path:
        return Path(
            "derived/provider_documents", provider, security_code, provider_document_id,
            artifact_owner_processing_run_id, "provider_document.json",
        )


class _Sources:
    def __init__(self) -> None:
        self.observations: dict[Path, SourcePdfObservation] = {}
        self.relpaths: list[Path] = []

    def observe_source_pdf(self, relpath: Path) -> SourcePdfObservation:
        self.relpaths.append(relpath)
        return self.observations[relpath]


@dataclass(frozen=True)
class Duty:
    """One attempt's closed H0 packet; ``authority`` assembles any legal history of it."""

    attempt_id: str
    fence_identity: str
    source_bytes: bytes
    source_relpath: Path
    snapshot_path: Path
    spec: V4PreparedExecutionSpec
    h0: RemoteParseCheckpointV4
    reservation: Any
    preparation: Any
    snapshot: SnapshotReceiptV4

    @property
    def held_before_submit(self) -> ResourceCreditVector:
        return ResourceCreditVector(
            documents=1, snapshot_items=1, snapshot_bytes=len(self.source_bytes), remote_waits=1,
        )

    def authority(
        self,
        history: tuple[RemoteParseCheckpointV4, ...],
        evidence: tuple[EvidenceValueV4, ...],
        *,
        is_current: bool = True,
        claim_owner_identity: str | None = "boot-u01",
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
            is_current=is_current,
            lifecycle_version=checkpoint.lifecycle_version,
            checkpoint_sha256=checkpoint.sha256,
            claim_generation=1,
            claim_owner_identity=claim_owner_identity,
            claim_lease_until=datetime(2030, 1, 1, tzinfo=UTC),
            checkpoint_history=history,
            reservation=self.reservation,
            evidence=tuple(encode_remote_parse_evidence_v4(value) for value in evidence),
            publication_winner=None,
            secret_history=(),
            source_supersession_link=None,
            staged_by_link=None,
            database_lease=None,
            execution_spec=self.spec,
        )

    def prepared(self) -> RemoteParseV4Authority:
        return self.authority((self.h0,), (self.preparation,))

    def intent(self) -> SubmissionIntentV4:
        prepared = self.spec.prepared_submission
        return SubmissionIntentV4(
            attempt_id=prepared.attempt_identity,
            fence_identity=prepared.fence_identity,
            snapshot_receipt_sha256=self.snapshot.sha256,
            source_pdf_sha256=prepared.source_pdf_sha256,
            parser_target_sha256=prepared.parser_target_identity_sha256,
            request_sha256=prepared.request_sha256,
            runtime_epoch_sha256=prepared.runtime_bundle_identity_sha256,
            client_submit_key=prepared.client_submit_key,
            submission_epoch_unix=prepared.submission_epoch_unix,
            provider_protocol_version=TASK_PROTOCOL_V2,
        )

    def reconciling(self) -> tuple[RemoteParseV4Authority, SubmissionIntentV4, RemoteParseCheckpointV4]:
        intent = self.intent()
        checkpoint = advance_remote_parse_checkpoint_v4(
            self.h0, state="reconciling", held_resource_credit=self.held_before_submit,
            snapshot_receipt_sha256=self.snapshot.sha256, submission_intent_sha256=intent.sha256,
        )
        return self.authority((self.h0, checkpoint), (self.preparation, self.snapshot, intent)), intent, checkpoint

    def submitted(self) -> RemoteParseV4Authority:
        _authority, intent, reconciling = self.reconciling()
        token = b"task-capability-" + self.attempt_id.encode()
        accepted = AcceptedSubmissionReceiptV4(
            attempt_id=self.attempt_id,
            fence_identity=self.fence_identity,
            submission_intent_sha256=intent.sha256,
            remote_task_identity=f"task-{self.attempt_id}",
            status_url=f"{API_URL}/tasks/task-{self.attempt_id}",
            result_url=f"{API_URL}/tasks/task-{self.attempt_id}/result",
            secret_kind="mineru-task-token.v1",
            secret_version=1,
            token_sha256=digest(token),
            token_byte_count=len(token),
            provider_protocol_version=TASK_PROTOCOL_V2,
        )
        checkpoint = advance_remote_parse_checkpoint_v4(
            reconciling, state="submitted",
            held_resource_credit=replace(self.held_before_submit, provider_tasks=1, ack_items=1),
            accepted_submission_sha256=accepted.sha256,
        )
        return self.authority(
            (self.h0, reconciling, checkpoint), (self.preparation, self.snapshot, intent, accepted),
        )

    def command_parts(self) -> tuple[SnapshotReceiptV4, SubmissionIntentV4, SnapshotSource]:
        intent = self.intent()
        return self.snapshot, intent, SnapshotSource(self.snapshot_path, intent, self.snapshot)


class DutyWorld:
    """Several attempts in one fake unit of work, under one data root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.uow = FakeUnitOfWork()
        self.paths = _Paths(root)
        self.sources = _Sources()
        self.uow.securities.add(Security(
            security_id="security-f5", company_id="company-f5", security_code="600001", exchange="SSE",
        ))

    def resolver(
        self, worker_profile: StagedWorkerProfileV4, *, legacy_execution: VerifiedQualifiedExecution | None = None,
    ) -> ProductionV4StageInputResolver:
        return ProductionV4StageInputResolver(
            uow_factory=lambda: cast(UnitOfWork, self.uow),
            paths=cast(FileStorePathPort, self.paths),
            provider_source=cast(ProviderDocumentSourcePort, self.sources),
            worker_profile=worker_profile,
            legacy_execution=legacy_execution,
        )

    def duty(
        self, *, tag: str, process_profile: MineruProcessProfile, worker_profile: StagedWorkerProfileV4,
        submission_epoch_unix: int = SUBMISSION_EPOCH, source_variant: bytes = b"", timeout_seconds: int = 21_600,
    ) -> Duty:
        attempt_id, fence = f"rpa_f5_{tag}", f"fence_f5_{tag}"
        document_id, run_id, provider_document_id = f"doc_f5_{tag}", f"run_f5_{tag}", f"provider-doc-{tag}"
        source_bytes = b"%PDF-1.7\n% independent F5 duty " + tag.encode() + source_variant + b"\n%%EOF\n"
        source_sha = digest(source_bytes)
        source_relpath = Path("raw_documents", "cninfo", "600001", "2026", provider_document_id,
                              f"sha256_{source_sha.removeprefix('sha256:')}.pdf")
        (self.root / source_relpath).parent.mkdir(parents=True, exist_ok=True)
        (self.root / source_relpath).write_bytes(source_bytes)
        runtime = process_profile.runtime_bundle_identity_sha256
        options = ParserOptions(
            method="auto", backend="hybrid-http-client", language="ch", formula=True, table=True,
            effort="medium", image_analysis=False, timeout_seconds=timeout_seconds, api_url=API_URL,
            api_drain_timeout_seconds=86_400, server_url=SERVER_URL,
            http_request_concurrency=process_profile.inference_concurrency,
            runtime_bundle_identity_sha256=runtime,
        )
        key = canonical_client_submit_key_v2(
            source_pdf_sha256=source_sha, attempt_identity=attempt_id, fence_identity=fence,
            submission_epoch_unix=submission_epoch_unix,
        )
        upload = "sha256_" + source_sha.removeprefix("sha256:") + ".pdf"
        request = submission_request_exact_bytes_v2(
            api_origin=API_URL, form=submission_form_v2(options, server_url=SERVER_URL), upload_filename=upload,
        )
        target_payload = options.target_identity(PARSER).to_payload()
        target_sha = digest(_canonical(target_payload))
        prepared_payload = {
            "schema": "mineru-prepared-submission.v1", "attempt_identity": attempt_id, "fence_identity": fence,
            "source_pdf_sha256": source_sha, "parser_target_identity_sha256": target_sha,
            "runtime_bundle_identity_sha256": runtime, "request_sha256": digest(request),
            "client_submit_key": key, "submission_epoch_unix": submission_epoch_unix,
        }
        prepared_bytes = _canonical(prepared_payload)
        prepared = PreparedSubmissionIdentity(
            schema="mineru-prepared-submission.v1", attempt_identity=attempt_id, fence_identity=fence,
            source_pdf_sha256=source_sha, parser_target_identity_sha256=target_sha,
            runtime_bundle_identity_sha256=runtime, request_sha256=digest(request), client_submit_key=key,
            submission_epoch_unix=submission_epoch_unix, exact_bytes=prepared_bytes, sha256=digest(prepared_bytes),
        )
        spec = V4PreparedExecutionSpec(
            contract_version=V4_PREPARED_EXECUTION_SPEC_CONTRACT, prepared_submission=prepared,
            parser_identity=PARSER, parser_options=options, api_origin=API_URL, server_url=SERVER_URL,
            request_exact_bytes=request, request_sha256=digest(request),
            process_profile_exact_bytes=encode_mineru_process_profile(process_profile),
            process_profile_sha256=process_profile.sha256, worker_profile=worker_profile,
            result_lease_seconds=600, remote_runaway_seconds=86_400, archive_member_count_limit=100_000,
            archive_uncompressed_byte_limit=process_profile.temporary_disk_bytes_limit,
        )
        envelope = build_staged_resource_credit_envelope(
            profile=process_profile, source_pdf_sha256=source_sha, source_byte_count=len(source_bytes),
            source_page_count=2,
        )
        creation = bind_v4_prepared_proposal(V4PreparedProposal(
            document_id=document_id, processing_run_id=run_id, prepared_submission=prepared,
            credit_envelope=envelope, execution_spec=spec,
        ), attempt_generation=1)
        snapshot = SnapshotReceiptV4(
            attempt_id=attempt_id, fence_identity=fence, preparation_intent_sha256=creation.preparation_intent.sha256,
            snapshot_relpath=creation.reservation.snapshot_relpath, snapshot_sha256=source_sha,
            snapshot_byte_count=len(source_bytes), part_path_absent=True, part_owner_path_absent=True,
            file_fsync_completed=True, parent_fsync_completed=True,
        )
        snapshot_path = self.root / f"snapshot-{tag}.pdf"
        snapshot_path.write_bytes(source_bytes)
        parser_artifact = self.paths.parser_run_artifacts_v4_relpath(
            provider="cninfo", security_code="600001", provider_document_id=provider_document_id,
            processing_run_id=run_id, source_pdf_sha256=source_sha, parser_backend=options.backend,
            parser_method=options.method,
        )
        provider_document = self.paths.provider_document_relpath(
            provider="cninfo", security_code="600001", provider_document_id=provider_document_id,
            artifact_owner_processing_run_id=run_id,
        )
        self.uow.documents.add(Document(
            document_id=document_id, status="archived", security_id="security-f5", provider="cninfo",
            provider_document_id=provider_document_id, raw_file_relpath=source_relpath.as_posix(),
            raw_file_hash=source_sha,
        ))
        self.uow.processing_runs.add(ProcessingRun(
            processing_run_id=run_id, document_id=document_id, artifact_owner_processing_run_id=run_id,
            run_kind="parse", status="running", parser_name=PARSER.name, parser_version=PARSER.version,
            parser_backend=options.backend, parser_method=options.method, parser_language=options.language,
            parser_target_identity=target_payload, input_raw_file_hash=source_sha,
            parser_artifact_relpath=parser_artifact.as_posix(), provider_document_relpath=provider_document.as_posix(),
            is_active=False,
        ))
        self.sources.observations[source_relpath] = SourcePdfObservation(
            sha256=source_sha, byte_count=len(source_bytes), page_count=2,
        )
        return Duty(
            attempt_id=attempt_id, fence_identity=fence, source_bytes=source_bytes, source_relpath=source_relpath,
            snapshot_path=snapshot_path, spec=spec, h0=creation.checkpoint, reservation=creation.reservation,
            preparation=creation.preparation_intent, snapshot=snapshot,
        )


def member_payload(authority: RemoteParseV4Authority) -> dict[str, Any]:
    """The inventory member of one head, by this author's reading of the contract."""

    spec = authority.execution_spec
    assert spec is not None
    return {
        "attempt_id": authority.attempt_id,
        "document_id": authority.document_id,
        "processing_run_id": authority.processing_run_id,
        "attempt_generation": authority.attempt_generation,
        "fence_identity": authority.fence_identity,
        "h0_checkpoint_sha256": authority.checkpoint_history[0].sha256,
        "execution_spec_sha256": spec.sha256,
        "source_pdf_sha256": authority.source_pdf_sha256,
        "parser_target_sha256": authority.parser_target_sha256,
        "request_sha256": authority.request_sha256,
        "runtime_epoch_sha256": authority.runtime_epoch_sha256,
        "client_submit_key": authority.client_submit_key,
        "submission_epoch_unix": spec.prepared_submission.submission_epoch_unix,
        "process_profile_sha256": spec.process_profile_sha256,
        "worker_profile_sha256": spec.worker_profile.sha256,
        "observed_state": authority.state,
        "observed_lifecycle_version": authority.lifecycle_version,
        "observed_checkpoint_sha256": authority.checkpoint_sha256,
        "accepted_submission_sha256": authority.checkpoint.accepted_submission_sha256,
    }


__all__ = ["API_URL", "Duty", "DutyWorld", "SERVER_URL", "SnapshotSource", "StageGuard", "digest", "member_payload"]
