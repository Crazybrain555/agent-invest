"""Admission shares real coordinator executors, credits and cancellation."""

from threading import Event, get_ident
import time
import unittest

from disclosure_anchor.application.contracts.provider_document_admission import SourcePdfObservation
from disclosure_anchor.application.ports.staged_new_work_v4 import (
    V4AdmissionObservationRequest, V4AdmissionObservationResult, V4OrdinaryParseCandidate,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    AdmissionOutcome, CoordinatorTerminal, ResourceCreditVector, StageLeaseLost, StagedParseCoordinator,
)
from tests.unit.test_staged_parse_coordinator import _Backend, _Clock, _limits, _work


class _ObservedBackend(_Backend):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.request = V4AdmissionObservationRequest(
            candidate=V4OrdinaryParseCandidate(
                document_id="observed", provider="cninfo", provider_document_id="observed",
                security_id="security", security_code="000001", raw_file_relpath="raw/source.pdf",
                raw_file_hash="sha256:" + "a" * 64, archived_raw_byte_count=100,
            ), credits=ResourceCreditVector(documents=1, snapshot_items=1, snapshot_bytes=100),
        )
        self.observation_requested = False
        self.accepted = False
        self.abandoned = False
        self.started = Event()
        self.release = Event()
        self.exited = Event()
        self.controller_thread = get_ident()
        self.observer_thread = None
        self.admission_times = []
        self.defer_until = 0.0
        self.cancel_seen = False
        self.preflight_exited = Event()
        self.check_preflight = False

    def admit_new(self, *, limit, available_credits):
        assert get_ident() == self.controller_thread
        self.admission_times.append(self.clock())
        if self.clock() < self.defer_until:
            return AdmissionOutcome(work=(), backlog_exists=True, deferred_reason="provider_unavailable")
        if not self.observation_requested:
            assert self.request.credits.fits(available_credits)
            self.observation_requested = True
            return AdmissionOutcome(
                work=(), backlog_exists=True, scan_incomplete=True, observation_request=self.request,
            )
        assert self.accepted
        return super().admit_new(limit=limit, available_credits=available_credits)

    def observe(self, request, *, stage_guard):
        self.observer_thread = get_ident()
        assert self.observer_thread != self.controller_thread
        if self.check_preflight:
            assert self.preflight_exited.is_set()
        self.started.set()
        deadline = time.monotonic() + 3
        try:
            while not self.release.wait(0.001):
                if time.monotonic() > deadline:
                    raise AssertionError("test observation release was not reached")
                try:
                    stage_guard.checkpoint()
                except StageLeaseLost:
                    self.cancel_seen = True
                    # Simulate actual resource teardown after revocation. The
                    # next controller snapshot must still charge our credits.
                    if not self.release.wait(2):
                        raise AssertionError("controller released credit or failed to drain") from None
                    raise
            stage_guard.checkpoint()
            return V4AdmissionObservationResult(request=request, source=SourcePdfObservation(
                sha256=request.candidate.raw_file_hash, byte_count=100, page_count=2,
            ))
        finally:
            self.exited.set()

    def accept_observation(self, result):
        assert get_ident() == self.controller_thread
        assert self.exited.is_set()
        assert result.request == self.request
        self.accepted = True

    def abandon_observation(self, request):
        assert get_ident() == self.controller_thread
        assert request == self.request
        assert not self.started.is_set() or self.exited.is_set()
        self.abandoned = True

    def prepare_remote_io(self, work, **kwargs):
        result = super().prepare_remote_io(work, **kwargs)
        self.preflight_exited.set()
        return result


class StagedAdmissionObservationTests(unittest.TestCase):
    def test_slow_observation_does_not_block_remote_publication_cleanup_or_ack(self):
        backend = _ObservedBackend(recoverable=tuple(
            _work(f"recovered-{index}", state, 4)
            for index, state in enumerate(("submitted", "local_materialized", "ack_pending"))
        ), new=(_work("new", "prepared"),))
        held = []

        def progress(snapshot):
            if backend.started.is_set() and not backend.exited.is_set():
                held.append(snapshot)
                self.assertTrue(backend.request.credits.fits(snapshot.credits_in_use))
                if snapshot.completed == 3:
                    backend.release.set()

        result = StagedParseCoordinator(
            backend=backend, limits=_limits(), progress=progress, admission_observer=backend,
        ).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(result.completed, 4)
        self.assertTrue(held)
        self.assertTrue(backend.accepted)
        self.assertFalse(backend.abandoned)
        self.assertEqual(result.credits_in_use, ResourceCreditVector())

    def test_observation_and_durable_preflight_share_one_slot(self):
        backend = _ObservedBackend(recoverable=(_work("owned", "prepared"),))
        backend.check_preflight = True
        backend.release.set()
        snapshots = []
        result = StagedParseCoordinator(
            backend=backend, limits=_limits(preflight_workers=1), progress=snapshots.append,
            admission_observer=backend,
        ).run()
        self.assertEqual(result.completed, 1)
        self.assertTrue(backend.accepted)
        self.assertTrue(all(dict(item.in_flight)["preflight"] <= 1 for item in snapshots))

    def test_stop_revokes_observer_but_retains_credit_until_actual_exit(self):
        backend = _ObservedBackend()
        stop = Event()
        charged_during_drain = []

        def progress(snapshot):
            if backend.started.is_set():
                stop.set()
            if backend.cancel_seen and not backend.exited.is_set():
                charged_during_drain.append(snapshot.credits_in_use)
                self.assertEqual(snapshot.credits_in_use, backend.request.credits)
                backend.release.set()

        result = StagedParseCoordinator(
            backend=backend, limits=_limits(), progress=progress, admission_observer=backend,
        ).run(stop_requested=stop.is_set)
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertTrue(charged_during_drain)
        self.assertTrue(backend.abandoned)
        self.assertFalse(backend.accepted)
        self.assertTrue(backend.exited.is_set())
        self.assertEqual(result.credits_in_use, ResourceCreditVector())

    def test_deferred_admission_stays_closed_until_probe_and_clears_after_recovery(self):
        backend = _ObservedBackend(recoverable=(_work("owned", "submitted"),))
        clock = _Clock()
        backend.clock = clock
        backend.defer_until = clock() + 0.04
        backend.wait_remote_remaining = 80
        backend.release.set()
        snapshots = []

        def progress(snapshot):
            snapshots.append(snapshot)
            clock.advance(0.001)

        result = StagedParseCoordinator(
            backend=backend, limits=_limits(admission_probe_seconds=0.02), progress=progress,
            admission_observer=backend, monotonic=clock,
        ).run()
        self.assertEqual(result.completed, 1)
        deferred = [item for item in snapshots if item.blocked_reason == "admission_deferred:provider_unavailable"]
        self.assertGreater(len(deferred), 1)
        self.assertTrue(all(not item.admission_open for item in deferred))
        self.assertGreaterEqual(backend.admission_times[1] - backend.admission_times[0], 0.02)
        self.assertIsNone(snapshots[-1].blocked_reason)

    def test_slow_observation_preserves_waiting_claim_renewal(self):
        clock = _Clock()
        backend = _ObservedBackend(recoverable=tuple(
            _work(f"owned-{index}", "ack_pending", 8) for index in range(5)
        ))
        backend.clock = clock
        backend.advance_clock = clock.advance
        backend.advance_per_ack = 8
        backend.claim_lease_seconds_override = 30
        backend.enforce_lease_expiry = True

        def progress(snapshot):
            if snapshot.completed == 5:
                backend.release.set()

        # The source budget is long enough for the deliberately advanced
        # clock, while each durable claim still needs its normal renewal.
        result = StagedParseCoordinator(
            backend=backend, admission_observer=backend, progress=progress, monotonic=clock,
            limits=_limits(ack_workers=1, claim_lease_seconds=90,
                           claim_renew_margin_seconds=5, max_stage_step_seconds=45),
        ).run()
        self.assertEqual(result.completed, 5)
        self.assertTrue(backend.accepted)
        self.assertIn("renew:owned-4", backend.calls)
        self.assertLess(backend.calls.index("renew:owned-4"), backend.calls.index("ack:owned-4:ack_pending"))
