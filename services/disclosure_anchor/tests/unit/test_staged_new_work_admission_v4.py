from __future__ import annotations

from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import unittest
from unittest import mock
from threading import Event
import time

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
from disclosure_anchor.application.ports.new_work_admission import NewWorkAdmissionUnavailable
from disclosure_anchor.application.ports.staged_new_work_v4 import (
    V4InitialIngressCapacityBlocked,
    V4OrdinaryParseCandidate,
    V4OrdinaryParseCandidatePage,
    V4AdmissionObservationResult,
)
from disclosure_anchor.application.services.staged_ingress_v4 import (
    DurableStagedIngressV4,
)
from disclosure_anchor.application.services.staged_new_work_admission_v4 import (
    StagedV4NewWorkAdmitter,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    AdmissionInterrupted,
    AdmissionOutcome,
    CoordinatorWork,
    CoordinatorTerminal,
    StagedParseCoordinator,
    StageLeaseGuard,
)
from tests.unit.test_mineru_process_profile import _profile
from tests.unit.test_staged_coordinator_persistence_v4 import _prepared_authority


_SOURCE_SHA = "sha256:" + "a" * 64


class _Source:
    def observe(self, request, *, stage_guard):
        stage_guard.checkpoint()
        return V4AdmissionObservationResult(
            request=request, source=self.observe_source_pdf(Path(request.candidate.raw_file_relpath)),
        )

    def observe_source_pdf(self, _relpath: Path) -> SourcePdfObservation:
        return SourcePdfObservation(sha256=_SOURCE_SHA, byte_count=1024, page_count=2)


class _Paths:
    def parser_run_artifacts_v4_relpath(self, **values: object) -> Path:
        return Path("parser_artifacts") / str(values["processing_run_id"])

    def provider_document_relpath(self, **values: object) -> Path:
        return Path("provider_documents") / f"{values['artifact_owner_processing_run_id']}.json"


class _CandidateSource:
    def __init__(self, candidates: tuple[V4OrdinaryParseCandidate, ...]) -> None:
        self.candidates = candidates
        self.calls: list[tuple[str | None, int]] = []

    def list_candidates(
        self, *, after_document_id: str | None, limit: int
    ) -> V4OrdinaryParseCandidatePage:
        self.calls.append((after_document_id, limit))
        candidates = tuple(
            item for item in self.candidates
            if after_document_id is None or item.document_id > after_document_id
        )
        return V4OrdinaryParseCandidatePage(
            candidates=candidates[:limit],
            has_more=len(candidates) > limit,
        )


class _IngressCommitter:
    def __init__(self, authority: object) -> None:
        self.authority = authority

    def commit(self, _command: object) -> object:
        return self.authority

    def reconcile(self, _command: object) -> object:
        raise AssertionError("reconciliation is not expected")


class _IngressUow:
    def __init__(self, committer: _IngressCommitter) -> None:
        self.remote_parse_v4_ingress = committer

    def __enter__(self) -> _IngressUow:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        return None


class _PreparedClaims:
    def __init__(self, claimed: CoordinatorWork) -> None:
        self.claimed = claimed
        self.claims: list[str] = []

    def admit_new(
        self, *, limit: int, available_credits: ResourceCreditVector
    ) -> AdmissionOutcome:
        return AdmissionOutcome(work=(), backlog_exists=False)

    def claim_recovery(self, candidate: object) -> CoordinatorWork:
        self.claims.append(candidate.attempt_id)  # type: ignore[attr-defined]
        return self.claimed


def _candidate() -> V4OrdinaryParseCandidate:
    return V4OrdinaryParseCandidate(
        document_id="doc-1",
        provider="cninfo",
        provider_document_id="notice-1",
        security_id="sec-1",
        security_code="000001",
        raw_file_relpath="raw_documents/cninfo/000001/notice-1.pdf",
        raw_file_hash=_SOURCE_SHA,
        archived_raw_byte_count=1024,
    )


class StagedV4NewWorkAdmitterTests(unittest.TestCase):
    @staticmethod
    def _admit(admitter, *, limit, available_credits):
        # Manual mechanism witness: real coordinator overlap is tested separately.
        first = admitter.admit_new(limit=limit, available_credits=available_credits)
        if first.observation_request is None:
            return first
        result = admitter.observe(
            first.observation_request,
            stage_guard=StageLeaseGuard(
                deadline_monotonic=time.monotonic() + 10,
                _revoked=Event(), _monotonic=time.monotonic,
            ),
        )
        admitter.accept_observation(result)
        held = ResourceCreditVector()
        for work in first.work:
            held = held + work.credits
        second = admitter.admit_new(
            limit=limit-len(first.work), available_credits=available_credits-held,
        )
        return replace(second, work=(*first.work, *second.work))

    def _fixture(self, *, oversized_prefix: int = 0, page_size: int = 8):
        profile = _profile()
        credit = build_staged_resource_credit_envelope(
            profile=profile,
            source_pdf_sha256=_SOURCE_SHA,
            source_byte_count=1024,
            source_page_count=2,
        )
        options = ParserOptions(
            timeout_seconds=21_600,
            api_url="http://127.0.0.1:30003",
            api_drain_timeout_seconds=86_400,
            server_url="http://mineru-openai-server:30000/v1",
            http_request_concurrency=7,
            runtime_bundle_identity_sha256=profile.runtime_bundle_identity_sha256,
        )
        factory = MinerUV4InitialIngressFactory(
            source=_Source(),
            paths=_Paths(),
            parser_identity=ParserIdentity(name="MinerU", version="3.4.4"),
            parser_options=options,
            process_profile=profile,
            process_profile_exact_bytes=encode_mineru_process_profile(profile),
            worker_profile=StagedWorkerProfileV4(profile.sha256, 1, 1),
            result_lease_seconds=300,
            remote_runaway_seconds=86_400,
            archive_member_count_limit=100_000,
            archive_uncompressed_byte_limit=16 * 1024 * 1024 * 1024,
            max_retries=3,
            scope_classes=None,
            utc_now=lambda: datetime(2026, 8, 29, tzinfo=UTC),
            attempt_id_factory=lambda: "attempt-1",
            fence_id_factory=lambda: "fence-1",
            processing_run_id_factory=lambda: "run-1",
            outbox_event_id_factory=lambda: "event-1",
        )
        authority = _prepared_authority(
            "attempt-1",
            snapshot_bytes=1024,
            database_now=datetime(2026, 8, 29, tzinfo=UTC),
        )
        claimed = CoordinatorWork(
            attempt_id="attempt-1",
            state="prepared",
            lifecycle_version=0,
            claim_generation=1,
            claim_owner_identity="boot-1",
            lease_expires_monotonic=100.0,
            credit_reservation=credit.reservation,
            credits=ResourceCreditVector(
                documents=1,
                snapshot_items=1,
                snapshot_bytes=1024,
            ),
        )
        claims = _PreparedClaims(claimed)
        committer = _IngressCommitter(authority)
        ingress = DurableStagedIngressV4(
            uow_factory=lambda: _IngressUow(committer)  # type: ignore[arg-type]
        )
        class _PrefixFactory:
            def observation_request(self, candidate, *, available_credits):
                if candidate.document_id.startswith("blocked-"):
                    raise V4InitialIngressCapacityBlocked(("output_pages",))
                return factory.observation_request(candidate, available_credits=available_credits)

            def observe(self, request, *, stage_guard):
                return factory.observe(request, stage_guard=stage_guard)

            def build(self, candidate, *, source_observation, available_credits):
                return factory.build(candidate, source_observation=source_observation, available_credits=available_credits)

            def source_rejection(self, candidate, rejection):
                return factory.source_rejection(candidate, rejection)

        candidates = _CandidateSource(tuple(
            replace(_candidate(), document_id=f"blocked-{index:04}")
            for index in range(oversized_prefix)
        ) + (_candidate(),))
        admitter = StagedV4NewWorkAdmitter(
            prepared_claims=claims,
            ordinary_candidates=candidates,
            ingress_factory=_PrefixFactory(),
            ingress=ingress,
            candidate_page_size=page_size,
        )
        return admitter, candidates, claims, claimed, credit

    def test_creates_h0_then_claims_it_with_remaining_credit(self) -> None:
        admitter, _, claims, claimed, credit = self._fixture()

        outcome = self._admit(admitter,
            limit=1,
            available_credits=credit.reservation,
        )

        self.assertEqual(outcome.work, (claimed,))
        self.assertFalse(outcome.backlog_exists)
        self.assertEqual(claims.claims, ["attempt-1"])

    def test_readiness_deferral_keeps_prepared_claims_and_does_not_scan_new_work(self) -> None:
        admitter, candidates, claims, claimed, credit = self._fixture()
        claims.admit_new = lambda **_kwargs: AdmissionOutcome(
            work=(claimed,), backlog_exists=False,
        )
        admitter._admission_guard = mock.Mock(
            side_effect=NewWorkAdmissionUnavailable("provider still draining"),
        )
        outcome = self._admit(admitter,
            limit=2, available_credits=credit.reservation + credit.reservation,
        )
        self.assertEqual(outcome.work, (claimed,))
        self.assertEqual(outcome.deferred_reason, "provider still draining")
        self.assertFalse(outcome.scan_incomplete)
        self.assertEqual(candidates.calls, [])

    def test_deferral_after_observation_preserves_cursor_for_retry(self) -> None:
        admitter, candidates, claims, claimed, credit = self._fixture(page_size=2)
        admitter._admission_guard = mock.Mock(side_effect=(
            None, None, NewWorkAdmissionUnavailable("provider unavailable"), None, None, None,
        ))
        first = self._admit(admitter, limit=1, available_credits=credit.reservation)
        self.assertEqual(first.work, ())
        self.assertIsNotNone(first.deferred_reason)
        self.assertEqual(claims.claims, [])
        second = self._admit(admitter, limit=1, available_credits=credit.reservation)
        self.assertEqual(second.work, (claimed,))
        self.assertIsNone(second.deferred_reason)
        self.assertEqual(candidates.calls, [(None, 2)])

    def test_real_worker_availability_callback_does_not_abort_owned_recovery(self) -> None:
        from disclosure_anchor.cli import worker as worker_cli
        from tests.unit.test_staged_parse_coordinator import _Backend, _limits, _work

        for state in ("submitted", "local_materialized", "ack_pending"):
            with self.subTest(recovery_state=state):
                admitter, candidates, _, _, _ = self._fixture()
                checker = mock.Mock(spec=worker_cli.MinerUDeploymentChecker)
                checker.assert_admission.side_effect = worker_cli.MinerUDeploymentUnavailableError(
                    "owned provider task prevents initial idle proof",
                )
                lock_conn = mock.Mock()
                lock_conn.execute.return_value.scalar_one.return_value = True
                admitter._admission_guard = lambda: worker_cli._assert_worker_admission(
                    lock_conn, mineru_checker=checker,
                    singleton_guard=lambda: worker_cli._assert_staged_singleton(lock_conn),
                )
                backend = _Backend(recoverable=(_work("recovery", state, 3),))
                backend.admit_new = admitter.admit_new  # type: ignore[method-assign]
                result = StagedParseCoordinator(backend=backend, limits=_limits()).run()
                self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
                self.assertEqual(result.errors, ())
                self.assertEqual(result.final_states, (("recovery", "acked"),))
                self.assertEqual(result.credits_in_use, ResourceCreditVector())
                self.assertEqual(candidates.calls, [])
                self.assertTrue(checker.assert_admission.called)

    def test_bounded_keyset_progress_past_nonfitting_prefix(self) -> None:
        admitter, candidates, _, claimed, credit = self._fixture(
            oversized_prefix=3, page_size=1,
        )
        outcomes = [self._admit(admitter, limit=1, available_credits=credit.reservation)
                    for _ in range(4)]
        self.assertEqual(outcomes[-1].work, (claimed,))
        self.assertEqual(candidates.calls, [
            (None, 1), ("blocked-0000", 1), ("blocked-0001", 1), ("blocked-0002", 1),
        ])
        self.assertTrue(all(item.scan_incomplete for item in outcomes[:-1]))
        self.assertFalse(outcomes[-1].scan_incomplete)

    def test_durable_overgrant_is_preserved_in_interrupted_admission(self) -> None:
        admitter, _, claims, claimed, credit = self._fixture()
        overgrant = replace(
            claimed, credits=replace(claimed.credits, snapshot_bytes=2048),
            credit_reservation=replace(claimed.credit_reservation, snapshot_bytes=2048),
        )
        claims.claimed = overgrant
        with self.assertRaises(AdmissionInterrupted) as raised:
            self._admit(admitter,
                limit=1,
                available_credits=replace(credit.reservation, snapshot_bytes=1024),
            )
        self.assertEqual(claims.claims, ["attempt-1"])
        self.assertEqual(raised.exception.claimed_work, (overgrant,))

    def test_prior_durable_claim_is_preserved_if_later_candidate_read_fails(self) -> None:
        admitter, candidates, claims, claimed, credit = self._fixture()
        claims.admit_new = lambda **_kwargs: AdmissionOutcome(
            work=(claimed,), backlog_exists=False,
        )
        def broken_page(**_kwargs):
            raise OSError("candidate read interrupted")
        candidates.list_candidates = broken_page
        with self.assertRaises(AdmissionInterrupted) as raised:
            self._admit(admitter, limit=2, available_credits=credit.reservation)
        self.assertEqual(raised.exception.claimed_work, (claimed,))

    def test_prior_overgrant_remains_owned_before_ordinary_scan(self) -> None:
        admitter, _, claims, claimed, credit = self._fixture()
        overgrant = replace(
            claimed, credits=replace(claimed.credits, snapshot_bytes=2048),
            credit_reservation=replace(claimed.credit_reservation, snapshot_bytes=2048),
        )
        claims.admit_new = lambda **_kwargs: AdmissionOutcome(
            work=(overgrant,), backlog_exists=False,
        )
        with self.assertRaises(AdmissionInterrupted) as raised:
            self._admit(admitter,
                limit=2, available_credits=replace(credit.reservation, snapshot_bytes=1024),
            )
        self.assertEqual(raised.exception.claimed_work, (overgrant,))

    def test_prepared_and_ordinary_scans_finish_one_shared_pass(self) -> None:
        admitter, candidates, claims, _, credit = self._fixture(oversized_prefix=3, page_size=1)
        candidates.candidates = candidates.candidates[:3]
        prepared_calls = []

        def prepared_pages(**_kwargs):
            prepared_calls.append(len(prepared_calls) + 1)
            return AdmissionOutcome(
                work=(), backlog_exists=True,
                ineligible_dimensions=("snapshot_bytes",),
                scan_incomplete=len(prepared_calls) % 2 == 1,
            )

        claims.admit_new = prepared_pages
        outcomes = [self._admit(admitter, limit=1, available_credits=credit.reservation)
                    for _ in range(4)]
        self.assertEqual(len(prepared_calls), 2)
        self.assertEqual(len(candidates.calls), 3)
        self.assertTrue(all(item.scan_incomplete for item in outcomes[:-1]))
        self.assertFalse(outcomes[-1].scan_incomplete)
        self.assertEqual(outcomes[-1].blocked_dimensions, ("output_pages",))
        self.assertEqual(outcomes[-1].ineligible_dimensions, ("snapshot_bytes",))
        # A later pass reopens both sources, including arrivals behind a cursor.
        self._admit(admitter, limit=1, available_credits=credit.reservation)
        self.assertEqual(len(prepared_calls), 3)

    def test_prepared_blocking_baseline_is_after_claims_before_ordinary_scan(self) -> None:
        admitter, candidates, claims, claimed, credit = self._fixture(
            oversized_prefix=2, page_size=1,
        )
        candidates.candidates = candidates.candidates[:2]
        claims.admit_new = lambda **_kwargs: AdmissionOutcome(
            work=(claimed,), backlog_exists=True, blocked_dimensions=("snapshot_bytes",),
        )
        available = replace(credit.reservation, snapshot_bytes=2048)
        first = self._admit(admitter, limit=2, available_credits=available)
        self.assertEqual(first.work, (claimed,))
        self.assertTrue(first.scan_incomplete)
        # That claim finishes while ordinary pages are advancing. Prepared
        # was blocked at 1024 remaining, not at the pre-claim grant of 2048.
        final = self._admit(admitter, limit=2, available_credits=available)
        self.assertTrue(final.scan_incomplete)
        self.assertFalse(admitter._prepared_complete)

    def test_repeated_page_fails_closed_instead_of_looping(self) -> None:
        admitter, candidates, _, _, credit = self._fixture(oversized_prefix=2, page_size=1)
        self._admit(admitter, limit=1, available_credits=credit.reservation)
        candidates.list_candidates = lambda **_kwargs: V4OrdinaryParseCandidatePage(
            candidates=(candidates.candidates[0],), has_more=True,
        )
        with self.assertRaisesRegex(ValueError, "cursor did not advance"):
            self._admit(admitter, limit=1, available_credits=credit.reservation)

    def test_credit_released_during_scan_requests_fresh_pass_before_idle(self) -> None:
        admitter, candidates, _, _, credit = self._fixture(oversized_prefix=2, page_size=1)
        low = replace(credit.reservation, output_pages=0)
        self._admit(admitter, limit=1, available_credits=low)
        # The fitting tail disappears before observation, while another lane
        # releases the credit which blocked the already-scanned prefix.
        candidates.candidates = candidates.candidates[:2]
        final = self._admit(admitter, limit=1, available_credits=credit.reservation)
        self.assertTrue(final.scan_incomplete)
        self._admit(admitter, limit=1, available_credits=credit.reservation)
        self.assertEqual(candidates.calls[-1], (None, 1))


if __name__ == "__main__":
    unittest.main()
