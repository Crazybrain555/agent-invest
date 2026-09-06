from __future__ import annotations

from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
import unittest

from disclosure_anchor.adapters.parsers.mineru_medium.v4_initial_ingress import (
    MinerUV4InitialIngressFactory,
)
from disclosure_anchor.application.contracts.mineru_process_profile import (
    encode_mineru_process_profile,
)
from disclosure_anchor.application.contracts.provider_document_admission import (
    SourcePdfObservation,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
    build_staged_resource_credit_envelope,
)
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions
from disclosure_anchor.application.ports.remote_parse_v4_failure_committer import (
    V4FinalFailureCommit,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import V4SuccessorAppend
from disclosure_anchor.application.ports.staged_new_work_v4 import (
    V4InitialIngressCapacityBlocked,
    V4OrdinaryParseCandidate,
    V4AdmissionObservationResult,
)
from tests.unit.test_mineru_process_profile import _profile
from tests.unit.test_mineru_http_staged_v4 import _claim
from tests.unit.test_remote_parse_evidence_v4 import _typed_pre_submission_failure_bundle


_SOURCE_SHA = "sha256:" + "a" * 64


class _Source:
    def __init__(self) -> None:
        self.observation = SourcePdfObservation(
            sha256=_SOURCE_SHA,
            byte_count=1024,
            page_count=2,
        )
        self.calls: list[Path] = []

    def observe(self, request, *, stage_guard):
        stage_guard.checkpoint()
        self.calls.append(Path(request.candidate.raw_file_relpath))
        return V4AdmissionObservationResult(request=request, source=self.observation)

    def observe_source_pdf(self, relpath: Path) -> SourcePdfObservation:
        self.calls.append(relpath)
        return self.observation


class _Paths:
    def parser_run_artifacts_v4_relpath(self, **values: object) -> Path:
        return Path("parser_artifacts") / str(values["processing_run_id"])

    def provider_document_relpath(self, **values: object) -> Path:
        return Path("provider_documents") / f"{values['artifact_owner_processing_run_id']}.json"


def _candidate(*, archived_bytes: int | None = 1024) -> V4OrdinaryParseCandidate:
    return V4OrdinaryParseCandidate(
        document_id="doc-1",
        provider="cninfo",
        provider_document_id="notice-1",
        security_id="sec-1",
        security_code="000001",
        raw_file_relpath="raw_documents/cninfo/000001/notice-1.pdf",
        raw_file_hash=_SOURCE_SHA,
        archived_raw_byte_count=archived_bytes,
    )


class MinerUV4InitialIngressFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = _profile()
        self.source = _Source()
        self.options = ParserOptions(
            method="auto",
            backend="hybrid-http-client",
            language="ch",
            formula=True,
            table=True,
            effort="medium",
            image_analysis=False,
            start_page=None,
            end_page=None,
            timeout_seconds=21_600,
            api_url="http://127.0.0.1:30003",
            api_drain_timeout_seconds=86_400,
            server_url="http://mineru-openai-server:30000/v1",
            http_request_concurrency=7,
            runtime_bundle_identity_sha256=(
                self.profile.runtime_bundle_identity_sha256
            ),
        )
        self.credit = build_staged_resource_credit_envelope(
            profile=self.profile,
            source_pdf_sha256=_SOURCE_SHA,
            source_byte_count=1024,
            source_page_count=2,
        )

    def _factory(self, *, now: datetime | None = None) -> MinerUV4InitialIngressFactory:
        return MinerUV4InitialIngressFactory(
            source=self.source,
            paths=_Paths(),
            parser_identity=ParserIdentity(name="MinerU", version="3.4.4"),
            parser_options=self.options,
            process_profile=self.profile,
            process_profile_exact_bytes=encode_mineru_process_profile(self.profile),
            worker_profile=StagedWorkerProfileV4(self.profile.sha256, 1, 1),
            result_lease_seconds=300,
            remote_runaway_seconds=86_400,
            archive_member_count_limit=100_000,
            archive_uncompressed_byte_limit=16 * 1024 * 1024 * 1024,
            max_retries=3,
            scope_classes=("annual_report",),
            utc_now=lambda: now or datetime(2026, 8, 29, tzinfo=UTC),
            attempt_id_factory=lambda: "attempt-1",
            fence_id_factory=lambda: "fence-1",
            processing_run_id_factory=lambda: "run-1",
            outbox_event_id_factory=lambda: "event-1",
        )

    def test_build_closes_source_spec_credit_paths_and_h0_reference(self) -> None:
        initial_available = ResourceCreditVector(
            documents=1,
            snapshot_items=1,
            snapshot_bytes=1024,
        )
        command = self._factory().build(
            _candidate(),
            source_observation=self.source.observation, available_credits=initial_available,
        )

        self.assertEqual(self.source.calls, [])
        spec = command.proposal.execution_spec
        self.assertGreater(len(spec.exact_bytes), 0)
        self.assertEqual(command.proposal.prepared_submission, spec.prepared_submission)
        self.assertEqual(
            command.proposal.credit_envelope.reservation_input.value.source_byte_count,
            1024,
        )
        self.assertEqual(command.parser_target.name, "MinerU")
        self.assertEqual(command.parser_artifact_relpath, "parser_artifacts/run-1")
        self.assertEqual(command.provider_document_relpath, "provider_documents/run-1.json")
        self.assertEqual(command.started_at, datetime(2026, 8, 29, tzinfo=UTC))
        self.assertEqual(
            spec.archive_member_count_limit,
            100_000,
        )
        self.assertEqual(
            spec.archive_uncompressed_byte_limit,
            min(
                16 * 1024 * 1024 * 1024,
                self.credit.reservation.temp_disk_bytes,
                self.credit.reservation.output_bytes,
            ),
        )

    def test_capacity_block_happens_before_proposal_creation(self) -> None:
        with self.assertRaises(V4InitialIngressCapacityBlocked) as raised:
            self._factory().build(
                _candidate(),
                source_observation=self.source.observation, available_credits=ResourceCreditVector(),
            )
        self.assertIn("documents", raised.exception.blocked_dimensions)
        self.assertEqual(raised.exception.ineligible_dimensions, ())

    def test_profile_ineligible_reservation_is_distinct_from_available_credit(self) -> None:
        self.profile = replace(self.profile, unpublished_pages_limit=2)
        self.source.observation = replace(self.source.observation, page_count=3)
        with self.assertRaises(V4InitialIngressCapacityBlocked) as raised:
            self._factory().build(_candidate(), source_observation=self.source.observation, available_credits=self.credit.reservation)
        self.assertIn("output_pages", raised.exception.ineligible_dimensions)

    def test_all_utc_contracts_reject_nonzero_offsets_of_the_same_instant(self) -> None:
        command = self._factory().build(_candidate(), source_observation=self.source.observation, available_credits=self.credit.reservation)
        final, _, _, _, predecessor, _ = _typed_pre_submission_failure_bundle()
        append = V4SuccessorAppend(claim=_claim(predecessor), successor=final)
        for hours in (5, -4):
            now = command.started_at.astimezone(timezone(timedelta(hours=hours)))
            self.assertEqual(now, command.started_at)  # equality is not a UTC test
            with self.subTest(offset=hours, boundary="factory"):
                with self.assertRaisesRegex(ValueError, "UTC"):
                    self._factory(now=now).build(_candidate(), source_observation=self.source.observation, available_credits=self.credit.reservation)
            with self.subTest(offset=hours, boundary="ingress"):
                with self.assertRaisesRegex(ValueError, "UTC"):
                    replace(command, started_at=now)
            with self.subTest(offset=hours, boundary="failure"):
                with self.assertRaisesRegex(ValueError, "UTC"):
                    V4FinalFailureCommit(append=append, failed_at=now, outbox_event_id="event-1")
        V4FinalFailureCommit(append=append, failed_at=command.started_at, outbox_event_id="event-1")

    def test_archived_source_byte_drift_fails_before_spec_write(self) -> None:
        with self.assertRaisesRegex(ValueError, "archived authority"):
            self._factory().build(
                _candidate(archived_bytes=2048),
                source_observation=self.source.observation, available_credits=self.credit.reservation,
            )


if __name__ == "__main__":
    unittest.main()
