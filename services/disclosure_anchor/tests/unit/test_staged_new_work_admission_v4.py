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
from disclosure_anchor.application.ports.remote_parse_v4_ingress import (
    V4InitialIngressNotEligible,
)
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
from disclosure_anchor.application.services.staged_v4_capacity import (
    staged_v4_coordinator_limits,
)
from tests.unit.test_mineru_process_profile import _profile
from tests.unit.test_staged_coordinator_persistence_v4 import _prepared_authority


_SOURCE_SHA = "sha256:" + "a" * 64


class _CandidateSource:
    """``pending_parse`` stand-in over one immutable raw archive.

    A committed H0 leaves the view, as the real view excludes running parses;
    ``withdrawn`` rows lose eligibility after they were listed. ``reads``
    records every raw source read.
    """

    def __init__(self, candidates: tuple[V4OrdinaryParseCandidate, ...]) -> None:
        self.candidates = candidates
        self.calls: list[tuple[str | None, str | None, int]] = []
        self.ceilings: list[str | None] = []
        self.admitted: list[str] = []
        self.withdrawn: set[str] = set()
        self.reads: list[str] = []
        self.page_counts: dict[str, int] = {}

    def _eligible(self) -> list[V4OrdinaryParseCandidate]:
        return sorted(
            (item for item in self.candidates
             if item.document_id not in self.admitted and item.document_id not in self.withdrawn),
            key=lambda item: item.document_id,
        )

    def latest_document_id(self) -> str | None:
        eligible = self._eligible()
        self.ceilings.append(eligible[-1].document_id if eligible else None)
        return self.ceilings[-1]

    def list_candidates(
        self, *, after_document_id: str | None, limit: int,
        through_document_id: str | None = None,
    ) -> V4OrdinaryParseCandidatePage:
        self.calls.append((after_document_id, through_document_id, limit))
        candidates = tuple(
            item for item in self._eligible()
            if (after_document_id is None or item.document_id > after_document_id)
            and (through_document_id is None or item.document_id <= through_document_id)
        )
        return V4OrdinaryParseCandidatePage(
            candidates=candidates[:limit],
            has_more=len(candidates) > limit,
        )


class _Source:
    def __init__(self, archive: _CandidateSource) -> None:
        self.archive = archive

    def observe(self, request, *, stage_guard):
        stage_guard.checkpoint()
        candidate = request.candidate
        self.archive.reads.append(candidate.document_id)
        return V4AdmissionObservationResult(request=request, source=SourcePdfObservation(
            sha256=_SOURCE_SHA,
            byte_count=candidate.archived_raw_byte_count or 1024,
            page_count=self.archive.page_counts.get(candidate.document_id, 2),
        ))


class _Paths:
    def parser_run_artifacts_v4_relpath(self, **values: object) -> Path:
        return Path("parser_artifacts") / str(values["processing_run_id"])

    def provider_document_relpath(self, **values: object) -> Path:
        return Path("provider_documents") / f"{values['artifact_owner_processing_run_id']}.json"


class _IngressCommitter:
    def __init__(self, authority: object, view: _CandidateSource) -> None:
        self.authority = authority
        self.view = view

    def commit(self, command: object) -> object:
        document_id = command.proposal.document_id  # type: ignore[attr-defined]
        if document_id in self.view.withdrawn:
            raise V4InitialIngressNotEligible(document_id)
        self.view.admitted.append(document_id)
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


def _ordinary(document_id: str, byte_count: int | None = 1024) -> V4OrdinaryParseCandidate:
    return replace(_candidate(), document_id=document_id, archived_raw_byte_count=byte_count)


def _guard() -> StageLeaseGuard:
    return StageLeaseGuard(
        deadline_monotonic=time.monotonic() + 10, _revoked=Event(), _monotonic=time.monotonic,
    )


def _capacity() -> ResourceCreditVector:
    profile = _profile()
    return staged_v4_coordinator_limits(
        profile, worker_profile=StagedWorkerProfileV4(profile.sha256, 1, 1),
    ).credits


# A known-size source that fits the profile but not the credit left now.
_HOLD = AdmissionOutcome(
    work=(), backlog_exists=True, blocked_dimensions=("snapshot_bytes",), scan_incomplete=True,
)


class StagedV4NewWorkAdmitterTests(unittest.TestCase):
    @staticmethod
    def _admit(admitter, *, limit, available_credits):
        # Manual mechanism witness: real coordinator overlap is tested separately.
        first = admitter.admit_new(limit=limit, available_credits=available_credits)
        if first.observation_request is None:
            return first
        result = admitter.observe(first.observation_request, stage_guard=_guard())
        admitter.accept_observation(result)
        held = ResourceCreditVector()
        for work in first.work:
            held = held + work.credits
        second = admitter.admit_new(
            limit=limit-len(first.work), available_credits=available_credits-held,
        )
        return replace(second, work=(*first.work, *second.work))

    def _fixture(self, *, oversized_prefix: int = 0, ineligible_prefix: bool = False,
                 page_size: int = 8, admission_document_ids: tuple[str, ...] | None = None):
        # "blocked-" rows never fit: either profile-ineligible, or an unknown
        # size under a temporary shortage. Both are skipped, never held.
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
        candidates = _CandidateSource(tuple(
            _ordinary(f"blocked-{index:04}", 1024 if ineligible_prefix else None)
            for index in range(oversized_prefix)
        ) + (_candidate(),))
        factory = MinerUV4InitialIngressFactory(
            source=_Source(candidates),
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
        committer = _IngressCommitter(authority, candidates)
        ingress = DurableStagedIngressV4(
            uow_factory=lambda: _IngressUow(committer)  # type: ignore[arg-type]
        )
        prefix_ineligible = ("output_pages",) if ineligible_prefix else ()

        class _PrefixFactory:
            def observation_request(self, candidate, *, available_credits):
                if candidate.document_id.startswith("blocked-"):
                    raise V4InitialIngressCapacityBlocked(
                        ("output_pages",), ineligible_dimensions=prefix_ineligible,
                    )
                return factory.observation_request(candidate, available_credits=available_credits)

            def observe(self, request, *, stage_guard):
                return factory.observe(request, stage_guard=stage_guard)

            def build(self, candidate, *, source_observation, available_credits):
                return factory.build(candidate, source_observation=source_observation, available_credits=available_credits)

            def source_rejection(self, candidate, rejection):
                return factory.source_rejection(candidate, rejection)

        admitter = StagedV4NewWorkAdmitter(
            prepared_claims=claims,
            ordinary_candidates=candidates,
            ingress_factory=_PrefixFactory(),
            ingress=ingress,
            candidate_page_size=page_size,
            admission_document_ids=admission_document_ids,
        )
        return admitter, candidates, claims, claimed, credit

    def test_commissioning_scope_rejects_injected_candidate_before_observation(self) -> None:
        admitter, _, claims, _, credit = self._fixture(admission_document_ids=("selected",))
        with mock.patch.object(admitter._ingress_factory, "observation_request") as observe:
            with self.assertRaisesRegex(ValueError, "outside commissioning scope"):
                admitter.admit_new(limit=1, available_credits=credit.reservation)
            observe.assert_not_called()
        self.assertEqual(claims.claims, [])

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
        self.assertEqual(candidates.calls, [(None, "doc-1", 2)])

    def test_frozen_ceiling_survives_first_candidate_abandonment_and_deferral(self) -> None:
        # Both leave the cursor unset; neither may re-freeze the pass ceiling.
        admitter, candidates, _, _, credit = self._fixture()
        candidates.candidates = (_ordinary("doc-a"), _ordinary("doc-b"))
        deferring = [False]

        def admission_guard() -> None:
            if deferring[0]:
                raise NewWorkAdmissionUnavailable("provider draining")

        admitter._admission_guard = admission_guard
        first = admitter.admit_new(limit=1, available_credits=credit.reservation)
        admitter.abandon_observation(first.observation_request)
        candidates.candidates += (_ordinary("doc-c"),)
        deferring[0] = True
        deferred = admitter.admit_new(limit=1, available_credits=credit.reservation)
        deferring[0] = False
        candidates.candidates += (_ordinary("doc-d"),)
        for _ in range(4):
            self._admit(admitter, limit=1, available_credits=credit.reservation)
        self.assertEqual(first.observation_request.candidate.document_id, "doc-a")
        self.assertEqual(deferred, AdmissionOutcome(
            work=(), backlog_exists=True, deferred_reason="provider draining",
        ))
        self.assertEqual(candidates.admitted, ["doc-a", "doc-b", "doc-c", "doc-d"])
        self.assertEqual(candidates.ceilings, ["doc-b", "doc-d"])
        self.assertEqual(candidates.calls, [
            (None, "doc-b", 8), (None, "doc-b", 8), ("doc-a", "doc-b", 8),
            (None, "doc-d", 8), ("doc-c", "doc-d", 8),
        ])

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

    def test_capacity_hold_keeps_owned_lanes_draining_and_yields_to_stop(self) -> None:
        from tests.unit.test_staged_parse_coordinator import _LIMIT, _Backend, _limits, _work

        for stop_after_hold in (False, True):
            with self.subTest(stop_after_hold=stop_after_hold):
                admitter, candidates, claims, _, _ = self._fixture()
                candidates.candidates = (_ordinary("doc-held", 100),)
                claims.claimed = _work("attempt-held", "prepared")
                # The recovered owner holds 100 of 150 snapshot bytes until it drains.
                backend = _Backend(recoverable=(_work("inflight", "submitted", 2),))
                backend.block_remote = True
                seen: list[tuple[int, AdmissionOutcome]] = []
                stopping = [False]

                def admit_new(*, limit, available_credits):
                    outcome = admitter.admit_new(limit=limit, available_credits=available_credits)
                    seen.append((available_credits.snapshot_bytes, outcome))
                    if len(seen) == 2:
                        stopping[0] = stop_after_hold
                        backend.remote_release.set()
                    return outcome

                # A never-quiescing admission must fail the test, not hang it.
                deadline = time.monotonic() + 20
                valve = [False]

                def stop_requested() -> bool:
                    if not valve[0] and time.monotonic() > deadline:
                        valve[0] = True
                        backend.remote_release.set()
                    return stopping[0] or valve[0]

                backend.admit_new = admit_new  # type: ignore[method-assign]
                result = StagedParseCoordinator(
                    backend=backend, admission_observer=admitter,
                    limits=_limits(credits=replace(_LIMIT, snapshot_bytes=150)),
                ).run(stop_requested=stop_requested)
                self.assertFalse(valve[0], "the run ended only through the safety stop")
                self.assertEqual(seen[:2], [(50, _HOLD), (50, _HOLD)])
                self.assertEqual((result.terminal, result.errors), (CoordinatorTerminal.QUIESCENT, ()))
                self.assertEqual(result.credits_in_use, ResourceCreditVector())
                if stop_after_hold:
                    self.assertEqual(len(seen), 2)
                    self.assertEqual((candidates.reads, candidates.admitted), ([], []))
                    self.assertEqual(result.final_states, (("inflight", "acked"),))
                else:
                    self.assertEqual(
                        [available for available, outcome in seen if outcome.observation_request],
                        [150],
                    )
                    self.assertEqual((candidates.reads, candidates.admitted), (["doc-held"], ["doc-held"]))
                    self.assertEqual(
                        result.final_states, (("attempt-held", "acked"), ("inflight", "acked")),
                    )

    def test_bounded_keyset_progress_past_profile_ineligible_prefix(self) -> None:
        admitter, candidates, _, claimed, credit = self._fixture(
            oversized_prefix=3, ineligible_prefix=True, page_size=1,
        )
        outcomes = [self._admit(admitter, limit=1, available_credits=credit.reservation)
                    for _ in range(4)]
        self.assertEqual(outcomes[-1].work, (claimed,))
        self.assertEqual(candidates.calls, [
            (None, "doc-1", 1), ("blocked-0000", "doc-1", 1),
            ("blocked-0001", "doc-1", 1), ("blocked-0002", "doc-1", 1),
        ])
        self.assertTrue(all(item.scan_incomplete for item in outcomes[:-1]))
        self.assertFalse(outcomes[-1].scan_incomplete)
        self.assertEqual(outcomes[-1].ineligible_dimensions, ("output_pages",))

    def test_each_pass_is_finite_while_ids_arrive_above_and_below_its_cursor(self) -> None:
        admitter, candidates, _, _, credit = self._fixture(page_size=1)
        candidates.candidates = (_ordinary("doc-c"), _ordinary("doc-e"))
        # One batch arrives after every call; doc-a is a lower ID that became
        # eligible only after the cursor had passed it.
        arrivals = (("doc-a", "doc-x1"), ("doc-x2",), ("doc-x3",), ("doc-x4",), ("doc-x5",))
        outcomes = []
        for batch in arrivals:
            outcomes.append(self._admit(admitter, limit=1, available_credits=credit.reservation))
            candidates.candidates += tuple(_ordinary(item) for item in batch)
        self.assertEqual([len(item.work) for item in outcomes], [1, 1, 1, 1, 1])
        self.assertEqual(candidates.admitted, ["doc-c", "doc-e", "doc-a", "doc-x1", "doc-x2"])
        self.assertEqual(candidates.ceilings, ["doc-e", "doc-x2"])
        self.assertEqual(candidates.calls, [
            (None, "doc-e", 1), ("doc-c", "doc-e", 1),
            (None, "doc-x2", 1), ("doc-a", "doc-x2", 1), ("doc-x1", "doc-x2", 1),
        ])
        self.assertEqual([item.scan_incomplete for item in outcomes], [True, False, True, True, False])

    def test_empty_eligible_snapshot_completes_without_a_page_query(self) -> None:
        admitter, candidates, _, _, credit = self._fixture()
        candidates.candidates = ()
        empty = self._admit(admitter, limit=1, available_credits=credit.reservation)
        self.assertEqual(empty, AdmissionOutcome(work=(), backlog_exists=False))
        self.assertEqual((candidates.ceilings, candidates.calls), ([None], []))
        candidates.candidates = (_candidate(),)
        arrived = self._admit(admitter, limit=1, available_credits=credit.reservation)
        self.assertEqual(len(arrived.work), 1)
        self.assertEqual(candidates.ceilings, [None, "doc-1"])
        self.assertEqual(candidates.calls, [(None, "doc-1", 8)])

    def test_temporary_shortage_holds_a_known_size_candidate_before_the_cursor(self) -> None:
        admitter, candidates, _, _, credit = self._fixture()
        low, high = (replace(credit.reservation, snapshot_bytes=value) for value in (2048, 8192))
        candidates.candidates = (
            _ordinary("doc-a", 4096), _ordinary("doc-b"), _ordinary("doc-c", 4096), _ordinary("doc-d"),
        )
        # doc-a holds with the cursor still unset while higher IDs arrive;
        # doc-b would fit but must not overtake it.
        for arrival in ("doc-e", "doc-f"):
            self.assertEqual(self._admit(admitter, limit=1, available_credits=low), _HOLD)
            candidates.candidates += (_ordinary(arrival),)
        self.assertEqual((candidates.reads, candidates.admitted, candidates.ceilings), ([], [], ["doc-d"]))
        for _ in range(2):
            self.assertEqual(len(self._admit(admitter, limit=1, available_credits=high).work), 1)
        self.assertEqual(self._admit(admitter, limit=1, available_credits=low), _HOLD)
        for _ in range(3):
            self._admit(admitter, limit=1, available_credits=high)
        self.assertEqual(candidates.admitted, ["doc-a", "doc-b", "doc-c", "doc-d", "doc-e"])
        self.assertEqual(candidates.reads, candidates.admitted)
        self.assertEqual(candidates.ceilings, ["doc-d", "doc-f"])
        # Each call, held or not, rechecks one bounded page whose head is the
        # held candidate; the cursor never passes an unadmitted row.
        self.assertEqual(candidates.calls, [
            (None, "doc-d", 8), (None, "doc-d", 8), (None, "doc-d", 8), ("doc-a", "doc-d", 8),
            ("doc-b", "doc-d", 8), ("doc-b", "doc-d", 8), ("doc-c", "doc-d", 8), (None, "doc-f", 8),
        ])

    def test_held_candidate_losing_eligibility_releases_the_scan_in_place(self) -> None:
        admitter, candidates, _, _, credit = self._fixture()
        low = replace(credit.reservation, snapshot_bytes=2048)
        candidates.candidates = (_ordinary("doc-a", 4096), _ordinary("doc-b"))
        self.assertEqual(self._admit(admitter, limit=1, available_credits=low), _HOLD)
        # A new failure, scope change or another owner removes doc-a from the view.
        candidates.withdrawn.add("doc-a")
        self.assertEqual(len(self._admit(admitter, limit=1, available_credits=low).work), 1)
        self.assertEqual((candidates.admitted, candidates.reads), (["doc-b"], ["doc-b"]))
        self.assertEqual(candidates.calls, [(None, "doc-b", 8), (None, "doc-b", 8)])

    def test_completed_observation_is_retained_across_a_build_shortage(self) -> None:
        # Once observed, even a source whose archive size was unknown has a
        # proven size: build waits for credit and never reads the source again.
        for archived, observe_bytes, short_bytes in (
            (4096, 8192, 2048), (None, _capacity().snapshot_bytes, 512),
        ):
            with self.subTest(archived_raw_byte_count=archived):
                admitter, candidates, _, _, credit = self._fixture()
                high = replace(credit.reservation, snapshot_bytes=8192)
                short = replace(credit.reservation, snapshot_bytes=short_bytes)
                candidates.candidates = (_ordinary("doc-a"), _ordinary("doc-b", archived), _ordinary("doc-c"))
                self._admit(admitter, limit=1, available_credits=high)
                requested = admitter.admit_new(
                    limit=1, available_credits=replace(credit.reservation, snapshot_bytes=observe_bytes),
                )
                self.assertEqual(requested.observation_request.candidate.document_id, "doc-b")
                admitter.accept_observation(admitter.observe(requested.observation_request, stage_guard=_guard()))
                # Another owner took the bytes before the controller could build doc-b.
                for arrival in ("doc-d", "doc-e"):
                    self.assertEqual(admitter.admit_new(limit=1, available_credits=short), _HOLD)
                    candidates.candidates += (_ordinary(arrival),)
                resumed = admitter.admit_new(limit=1, available_credits=high)
                self.assertEqual((len(resumed.work), resumed.observation_request), (1, None))
                for _ in range(2):
                    self._admit(admitter, limit=1, available_credits=high)
                self.assertEqual(candidates.admitted, ["doc-a", "doc-b", "doc-c", "doc-d"])
                self.assertEqual(candidates.reads, candidates.admitted)
                self.assertEqual(candidates.ceilings, ["doc-c", "doc-e"])

    def test_permanent_or_unknown_size_shortage_is_skipped_not_held(self) -> None:
        capacity = _capacity()
        cases = (
            # first candidate, observed pages, raw reads, blocked, ineligible
            (_ordinary("doc-a", capacity.snapshot_bytes + 1), 2,
             ["doc-b"], (), ("snapshot_bytes",)),
            (_ordinary("doc-a", None), 2, ["doc-b"], ("snapshot_bytes",), ()),
            (_ordinary("doc-a"), capacity.output_pages + 1,
             ["doc-a", "doc-b"], (), ("output_pages",)),
        )
        for first, pages, reads, blocked, ineligible in cases:
            with self.subTest(size=first.archived_raw_byte_count, pages=pages):
                admitter, candidates, _, _, credit = self._fixture()
                high = replace(credit.reservation, snapshot_bytes=8192)
                candidates.candidates = (first, _ordinary("doc-b"))
                candidates.page_counts["doc-a"] = pages
                outcomes = [self._admit(admitter, limit=1, available_credits=high)]
                while outcomes[-1].scan_incomplete and len(outcomes) < 4:
                    outcomes.append(self._admit(admitter, limit=1, available_credits=high))
                self.assertEqual((candidates.admitted, candidates.reads), (["doc-b"], reads))
                self.assertEqual(
                    (outcomes[-1].scan_incomplete, outcomes[-1].blocked_dimensions,
                     outcomes[-1].ineligible_dimensions),
                    (False, blocked, ineligible),
                )
                # Skipped, not lost: the next finite pass evaluates doc-a again.
                self.assertEqual(self._admit(admitter, limit=1, available_credits=high).work, ())
                self.assertEqual(candidates.ceilings, ["doc-b", "doc-a"])
                self.assertEqual(candidates.calls[-1], (None, "doc-a", 8))

    def test_page_row_above_the_frozen_ceiling_fails_closed(self) -> None:
        for prepared_first in (False, True):
            with self.subTest(prepared_first=prepared_first):
                admitter, candidates, claims, claimed, credit = self._fixture()
                candidates.candidates = (_ordinary("doc-a"), _ordinary("doc-b"))
                if prepared_first:
                    claims.admit_new = lambda **_kwargs: AdmissionOutcome(
                        work=(claimed,), backlog_exists=False,
                    )
                bounded = candidates.list_candidates

                def escaping_page(**kwargs):
                    bounded(**kwargs)
                    return V4OrdinaryParseCandidatePage(
                        candidates=(_ordinary("doc-z"),), has_more=False,
                    )

                candidates.list_candidates = escaping_page
                expected = AdmissionInterrupted if prepared_first else ValueError
                with self.assertRaises(expected) as raised:
                    admitter.admit_new(
                        limit=2, available_credits=credit.reservation + credit.reservation,
                    )
                if prepared_first:
                    self.assertEqual(raised.exception.claimed_work, (claimed,))
                self.assertEqual(candidates.calls, [(None, "doc-b", 8)])
                self.assertEqual((candidates.reads, candidates.admitted), ([], []))

    def test_ingress_eligibility_refusal_is_skipped_without_claim_or_hold(self) -> None:
        admitter, candidates, claims, _, credit = self._fixture()
        candidates.candidates = (_ordinary("doc-a"), _ordinary("doc-b"))
        first = admitter.admit_new(limit=1, available_credits=credit.reservation)
        admitter.accept_observation(admitter.observe(first.observation_request, stage_guard=_guard()))
        # Another owner's H0 or a new failure removed doc-a before the guarded write.
        candidates.withdrawn.add("doc-a")
        refused = admitter.admit_new(limit=1, available_credits=credit.reservation)
        self.assertEqual(
            (refused.work, refused.scan_incomplete, refused.blocked_dimensions), ((), True, ()),
        )
        self.assertEqual(claims.claims, [])
        self.assertEqual(len(self._admit(admitter, limit=1, available_credits=credit.reservation).work), 1)
        self.assertEqual((candidates.admitted, candidates.reads), (["doc-b"], ["doc-a", "doc-b"]))
        self.assertEqual(candidates.calls, [(None, "doc-b", 8), ("doc-a", "doc-b", 8)])

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
        for failing in ("latest_document_id", "list_candidates"):
            with self.subTest(failing=failing):
                admitter, candidates, claims, claimed, credit = self._fixture()
                claims.admit_new = lambda **_kwargs: AdmissionOutcome(
                    work=(claimed,), backlog_exists=False,
                )

                def broken_read(**_kwargs):
                    raise OSError("candidate read interrupted")

                setattr(candidates, failing, broken_read)
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

    def test_prepared_claims_lead_each_pass_before_its_ordinary_pages(self) -> None:
        admitter, candidates, claims, claimed, credit = self._fixture()
        candidates.candidates = (_ordinary("doc-a"),)
        order: list[str] = []
        prepared_pages = iter((
            AdmissionOutcome(work=(claimed,), backlog_exists=True, scan_incomplete=True),
            AdmissionOutcome(work=(claimed,), backlog_exists=False),
            AdmissionOutcome(work=(), backlog_exists=False),
        ))

        def prepared_scan(**_kwargs):
            order.append("prepared")
            return next(prepared_pages)

        listed = candidates.list_candidates

        def ordinary_page(**kwargs):
            order.append("ordinary")
            return listed(**kwargs)

        claims.admit_new = prepared_scan
        candidates.list_candidates = ordinary_page
        outcomes = [self._admit(admitter, limit=1, available_credits=credit.reservation)
                    for _ in range(3)]
        candidates.candidates += (_ordinary("doc-b"),)
        outcomes.append(self._admit(admitter, limit=1, available_credits=credit.reservation))
        # An incomplete prepared scan, then prepared work filling the grant,
        # both precede any ordinary page; every new pass starts prepared-first.
        self.assertEqual([item.work for item in outcomes[:2]], [(claimed,), (claimed,)])
        self.assertTrue(all(item.scan_incomplete for item in outcomes[:2]))
        self.assertEqual(order, ["prepared", "prepared", "ordinary", "prepared", "ordinary"])
        self.assertEqual(candidates.admitted, ["doc-a", "doc-b"])
        self.assertEqual(candidates.ceilings, ["doc-a", "doc-b"])

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
        self.assertEqual(candidates.ceilings, ["doc-1", "blocked-0001"])
        self.assertEqual(candidates.calls[-1], (None, "blocked-0001", 1))


if __name__ == "__main__":
    unittest.main()
