"""Independent F5 hold-containment witnesses, using the existing fake stages.

These tests deliberately retain accepted work. A site hold must use the
existing first-cause stop; a document-local hold must leave spare work runnable.
"""

from __future__ import annotations

from dataclasses import replace
from unittest import mock
import unittest

from disclosure_anchor.application.ports.remote_provider_v4 import RemoteProviderWaitingV4
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorTerminal,
    ResourceCreditVector,
    StageCapacityBlocked,
    StageProviderWaiting,
    StagedParseCoordinator,
)
from disclosure_anchor.application.services.worker_stop_latch import InProcessWorkerStopLatch
from tests.unit import test_staged_coordinator_backend_v4 as durable
from tests.unit.test_f5_coordinator_stop_independent import _StageBackend, _run
from tests.unit.test_staged_coordinator_grant import _GrantBackend, _grown
from tests.unit.test_staged_parse_coordinator import _LIFECYCLE_RESERVATION, _limits, _work


def _bounded_stop(after: int = 60):
    calls = [0]

    def stop() -> bool:
        calls[0] += 1
        return calls[0] > after

    return stop


class NativeStorageHoldBoundaryTests(unittest.TestCase):
    def test_blocked_poll_is_site_hold_without_failure_or_ack(self) -> None:
        authority = durable._authority("submitted")
        work = durable._work(authority)
        original = (authority.state, authority.checkpoint_sha256,
                    tuple(item.sha256 for item in authority.evidence))
        for reason in ("hard_envelope_exceeded", "codec_bound_exceeded",
                       "seal_integrity", "tree_integrity"):
            remote = mock.Mock()
            remote.poll_once.return_value = RemoteProviderWaitingV4(
                remote_task_identity="task-1", status="processing",
                response_sha256="sha256:" + "1" * 64, response_byte_count=2,
                storage_wait_reason=reason, storage_blocked=True,
            )
            backend, persistence, inputs, _ = durable._backend(authority, remote=remote)
            inputs.poll_command.return_value = mock.sentinel.poll
            with self.subTest(reason=reason):
                with mock.patch.object(backend, "_capability", return_value=mock.sentinel.capability), \
                        self.assertRaises(StageCapacityBlocked) as caught:
                    backend.run_remote(
                        work, credit_allowance=ResourceCreditVector(),
                        stage_guard=durable._guard(),
                    )
                self.assertIn(reason, str(caught.exception))
                self.assertEqual(persistence.appends, [])
                self.assertEqual((authority.state, authority.checkpoint_sha256,
                                  tuple(item.sha256 for item in authority.evidence)), original)
                remote.poll_once.assert_called_once_with(mock.sentinel.poll)

    def test_transient_native_free_floor_remains_healthy_wait(self) -> None:
        authority = durable._authority("submitted")
        remote = mock.Mock()
        remote.poll_once.return_value = RemoteProviderWaitingV4(
            remote_task_identity="task-1", status="processing",
            response_sha256="sha256:" + "1" * 64, response_byte_count=2,
            storage_wait_reason="free_floor", storage_blocked=False,
        )
        backend, persistence, inputs, _ = durable._backend(authority, remote=remote)
        inputs.poll_command.return_value = mock.sentinel.poll
        with mock.patch.object(backend, "_capability", return_value=mock.sentinel.capability), \
                self.assertRaises(StageProviderWaiting):
            backend.run_remote(
                durable._work(authority), credit_allowance=ResourceCreditVector(),
                stage_guard=durable._guard(),
            )
        self.assertEqual(persistence.appends, [])


class SiteAndPerAttemptHoldTests(unittest.TestCase):
    def _held_result(self, dimension: str, *, spare: int = 0):
        held = _work("held", "materializing", 5)
        backend = _StageBackend(recoverable=(held,))
        backend.local_error["held"] = StageCapacityBlocked(
            "synthetic retained hold", dimensions=(dimension,),
        )
        limits = _limits(credits=replace(
            _limits().credits, materialization_items=1 + spare,
        ))
        latch = InProcessWorkerStopLatch()
        result = _run(StagedParseCoordinator(
            backend=backend, limits=limits, stop_control=latch,
        ), stop_requested=_bounded_stop())
        return result, backend, latch

    def test_transfer_integrity_hold_trips_first_cause_without_settlement(self) -> None:
        for reason in ("spool_owner_unproven", "spool_progress_unproven",
                       "spool_part_identity", "spool_part_short", "spool_prefix_mismatch"):
            with self.subTest(reason=reason):
                result, backend, latch = self._held_result(reason)
                self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
                self.assertEqual(result.termination_kind, "public_stop", result)
                cause = latch.first_cause()
                assert cause is not None
                self.assertEqual((cause.kind, cause.reason_code, cause.attempt_id,
                                  cause.lane, cause.state_at_dispatch),
                                 ("coordinator_circuit", "transfer_integrity_hold",
                                  "held", "local", "materializing"))
                self.assertFalse(any(call.startswith(("cleanup:held", "ack:held"))
                                     for call in backend.calls))
                self.assertEqual(result.final_states, ())
                self.assertGreater(result.credits_in_use.provider_result_bytes, 0)

    def test_decode_and_transfer_budget_hold_allow_other_work_with_spare_credit(self) -> None:
        for reason in ("decode_input_bytes", "decode_output_bytes",
                       "transfer_logical_deadline", "transfer_progress",
                       "transfer_range_unsupported"):
            with self.subTest(reason=reason):
                held = _work("held", "materializing", 5)
                other = _work("other", "materializing", 5)
                backend = _StageBackend(recoverable=(held, other))
                backend.local_error["held"] = StageCapacityBlocked(
                    "document-local retained hold", dimensions=(reason,),
                )
                limits = _limits(credits=replace(_limits().credits, materialization_items=2))
                latch = InProcessWorkerStopLatch()
                result = _run(StagedParseCoordinator(
                    backend=backend, limits=limits, stop_control=latch,
                ), stop_requested=_bounded_stop())
                self.assertIn(("other", "acked"), result.final_states, result)
                self.assertIsNone(latch.first_cause(), result)
                self.assertNotIn(("held", "acked"), result.final_states)
                self.assertFalse(any(call.startswith(("cleanup:held", "ack:held"))
                                     for call in backend.calls))

    def test_held_only_positive_ledger_exhaustion_stops_but_zero_unused_axes_do_not(self) -> None:
        result, backend, latch = self._held_result("decode_input_bytes", spare=0)
        cause = latch.first_cause()
        assert cause is not None, result
        self.assertEqual((cause.kind, cause.reason_code),
                         ("coordinator_circuit", "capacity_holds_exhausted"))
        self.assertGreater(result.credits_in_use.materialization_items, 0)
        self.assertEqual(result.credits_in_use.remote_waits, 0)
        self.assertEqual(result.final_states, ())
        self.assertFalse(any(call.startswith(("cleanup:held", "ack:held"))
                             for call in backend.calls))
        # With one spare materialization credit, zero active remote waits and
        # other unused dimensions must not fabricate an exhaustion stop.
        spare, _, spare_latch = self._held_result("decode_input_bytes", spare=1)
        self.assertIsNone(spare_latch.first_cause(), spare)

    def test_impossible_grant_uses_site_stop_and_never_commits_or_acks(self) -> None:
        impossible = _grown(_LIFECYCLE_RESERVATION, 20_000)
        backend = _GrantBackend(
            required={"grant": impossible},
            recoverable=(_work("grant", "submitted", 2),),
        )
        latch = InProcessWorkerStopLatch()
        result = _run(StagedParseCoordinator(
            backend=backend, limits=_limits(), stop_control=latch,
        ), stop_requested=_bounded_stop())
        cause = latch.first_cause()
        assert cause is not None, result
        self.assertEqual((cause.kind, cause.reason_code, cause.attempt_id,
                          cause.lane, cause.state_at_dispatch),
                         ("coordinator_circuit", "stage_grant_unsatisfiable",
                          "grant", "remote", "submitted"))
        self.assertEqual(backend.requirements_raised, ["grant"])
        self.assertEqual(result.final_states, ())
        self.assertFalse(any(call.startswith(("cleanup:grant", "ack:grant"))
                             for call in backend.calls))
