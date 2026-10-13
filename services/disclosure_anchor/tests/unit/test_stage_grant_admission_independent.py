"""Independent coordinator scheduling witnesses for stage-grant admission.

Only the public coordinator runs. An existing in-memory backend models durable
stage transitions; this does not claim Postgres evidence or provider coverage.
"""

from __future__ import annotations

from dataclasses import replace
import threading
import time
import unittest

from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    decode_remote_parse_evidence_v4,
    effective_resource_reservation_v4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    decode_materialization_intent_v4,
    decode_resource_reservation_v4,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorTerminal,
    ResourceCreditVector,
    StageResourceGrantRequired,
    StagedParseCoordinator,
)
from tests.unit.test_stage_resource_grant_independent import _v5_bundle
from tests.unit.test_staged_parse_coordinator import _Backend, _Clock, _LIMIT, _limits, _work


class _ActualGrantBackend(_Backend):
    def __init__(self, *, recoverable, new=()):
        super().__init__(recoverable=recoverable, new=new)
        self._active_large_prepares = 0
        self.max_parallel_large_prepares = 0
        self._prepare_lock = threading.Lock()
        self.grant_requests: list[tuple[str, int]] = []
        self.granted_prepares: list[tuple[str, int]] = []

    def prepare_local_io(self, work, *, credit_allowance, stage_guard):
        if work.attempt_id.startswith("large-"):
            required = replace(work.credit_reservation, temp_disk_bytes=900)
            if not required.fits(work.credit_reservation):
                self.grant_requests.append((work.attempt_id, required.temp_disk_bytes))
                raise StageResourceGrantRequired("synthetic verified result growth", required=required)
            self.granted_prepares.append((work.attempt_id, work.credit_reservation.temp_disk_bytes))
            with self._prepare_lock:
                self._active_large_prepares += 1
                self.max_parallel_large_prepares = max(
                    self.max_parallel_large_prepares, self._active_large_prepares,
                )
            try:
                stage_guard.checkpoint()
                time.sleep(0.02)  # bounded overlap window for an erroneous second grant
                return super().prepare_local_io(
                    work, credit_allowance=credit_allowance, stage_guard=stage_guard,
                )
            finally:
                with self._prepare_lock:
                    self._active_large_prepares -= 1
        return super().prepare_local_io(
            work, credit_allowance=credit_allowance, stage_guard=stage_guard,
        )


class _RenewedGrantBackend(_ActualGrantBackend):
    def __init__(self, work):
        super().__init__(recoverable=(work,))
        self.test_clock = _Clock()
        self.clock = self.test_clock
        self.advance_clock = self.test_clock.advance
        self.claim_lease_seconds_override = 0.6
        self.original_reservation = work.credit_reservation
        self.advanced_once = False
        self.renew_inputs: list[int] = []
        self.persisted_transition = None

    def prepare_local_io(self, work, *, credit_allowance, stage_guard):
        if not self.advanced_once and work.credit_reservation.temp_disk_bytes < 900:
            # An actual claim can approach renewal while its first grant
            # request is in flight; the next dispatch must preserve the grant.
            self.test_clock.advance(0.4)
            self.advanced_once = True
        updated = super().prepare_local_io(
            work, credit_allowance=credit_allowance, stage_guard=stage_guard,
        )
        self.persisted_transition = updated
        return updated

    def renew_claim(self, work, *, lease_seconds):
        self.renew_inputs.append(work.credit_reservation.temp_disk_bytes)
        durable_before_grant = replace(work, credit_reservation=self.original_reservation)
        return super().renew_claim(durable_before_grant, lease_seconds=lease_seconds)


class StageGrantAdmissionIndependentTests(unittest.TestCase):
    def test_existing_occupancy_two_large_grants_and_small_tail(self):
        holder = _work("holder", "materializing", 5)
        large = tuple(_work(f"large-{index}", "remote_terminal", 5) for index in range(2))
        small = tuple(_work(f"small-{index}", "prepared") for index in range(5))
        backend = _ActualGrantBackend(recoverable=(holder, *large), new=small)
        snapshots = []
        deadline = time.monotonic() + 4
        limit = replace(_LIMIT, temp_disk_bytes=1000)
        result = StagedParseCoordinator(
            backend=backend,
            limits=_limits(
                credits=limit, local_prepare_workers=2,
                admission_batch_size=1, poll_seconds=0.001,
            ),
            progress=snapshots.append,
        ).run(stop_requested=lambda: time.monotonic() >= deadline)

        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.credits_in_use, ResourceCreditVector())
        self.assertEqual(result.completed, 8)
        self.assertEqual(len(backend.grant_requests), 2)
        self.assertEqual(sorted(backend.grant_requests), [("large-0", 900), ("large-1", 900)])
        self.assertEqual(sorted(backend.granted_prepares), [("large-0", 900), ("large-1", 900)])
        self.assertEqual(backend.max_parallel_large_prepares, 1)
        self.assertTrue(snapshots)
        self.assertLessEqual(max(snapshot.credits_in_use.temp_disk_bytes for snapshot in snapshots), 1000)
        self.assertLess(
            backend.calls.index("local:holder"),
            backend.calls.index("local_prepare:large-0"),
        )
        self.assertLess(
            backend.calls.index("local_prepare:large-0"),
            backend.calls.index("preflight:small-4"),
        )

    def test_claim_renewal_and_recovered_projection_keep_effective_grant(self):
        backend = _RenewedGrantBackend(_work("large-renew", "remote_terminal", 5))
        credits = replace(_LIMIT, temp_disk_bytes=1000)
        limits = _limits(
            credits=credits, claim_lease_seconds=1,
            claim_renew_margin_seconds=0.2, max_stage_step_seconds=0.1,
            poll_seconds=0.001,
        )
        deadline = time.monotonic() + 3
        result = StagedParseCoordinator(
            backend=backend, limits=limits, monotonic=backend.test_clock,
        ).run(stop_requested=lambda: time.monotonic() >= deadline)
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(backend.renew_inputs, [900])
        self.assertEqual(backend.granted_prepares, [("large-renew", 900)])
        persisted = backend.persisted_transition
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.state, "materializing")
        self.assertEqual(persisted.credit_reservation.temp_disk_bytes, 900)

        class ReopenedBackend(_Backend):
            def __init__(self):
                super().__init__(recoverable=(persisted,))
                self.reopened_reservations = []

            def run_local(self, work, *, credit_allowance, stage_guard):
                self.reopened_reservations.append(work.credit_reservation.temp_disk_bytes)
                return super().run_local(
                    work, credit_allowance=credit_allowance, stage_guard=stage_guard,
                )

        reopened = ReopenedBackend()
        replay = StagedParseCoordinator(backend=reopened, limits=limits).run(
            stop_requested=lambda: time.monotonic() >= deadline + 2,
        )
        self.assertEqual(replay.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(reopened.reopened_reservations, [900])

    def test_encoded_intent_readback_uses_grant_above_immutable_estimate(self):
        reservation, values, _history = _v5_bundle()
        terminal, intent = values[4:6]
        original_hash = reservation.sha256
        old_estimate = reservation.reserved_credit
        reopened_reservation = decode_resource_reservation_v4(reservation.canonical_bytes)
        reopened_terminal = decode_remote_parse_evidence_v4(
            "terminal_receipt", terminal.canonical_bytes,
        ).value
        reopened_intent = decode_materialization_intent_v4(intent.canonical_bytes)
        effective = effective_resource_reservation_v4(
            reopened_reservation, terminal=reopened_terminal, intent=reopened_intent,
        )
        self.assertEqual(reopened_reservation.sha256, original_hash)
        self.assertEqual(reopened_reservation.reserved_credit, old_estimate)
        self.assertEqual(old_estimate.decoded_bytes, 30)
        self.assertEqual(effective.decoded_bytes, 40)
        self.assertEqual(effective, intent.resource_grant.limits)


if __name__ == "__main__":
    unittest.main()
