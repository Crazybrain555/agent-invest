from dataclasses import replace
import threading
import unittest

from disclosure_anchor.application.services.mineru_stream_policy import (
    MineruStreamPolicy,
    StreamAdmissionControl,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorResult,
    CoordinatorSnapshot,
    CoordinatorTerminal,
    CoordinatorWork,
    ResourceCreditVector,
    StageLeaseGuard,
    StagedParseCoordinator,
)
from tests.unit.test_mineru_stream_policy import GIB, Pressure, config, sample, warm
from tests.unit.test_staged_parse_coordinator import _Backend, _limits, _work


def control(pressure: Pressure, maximum: int = 2) -> StreamAdmissionControl:
    """A cold control: its first sample starts a recovery window at target 0."""

    return StreamAdmissionControl(
        MineruStreamPolicy(config(qualified_max=maximum)),
        pressure,
        monotonic=lambda: 10,
    )


def warmed(maximum: int = 2) -> tuple[StreamAdmissionControl, Pressure, list[int]]:
    """A control already holding ``maximum`` after continuous healthy windows.

    The returned pressure holds the last healthy sample at the frozen clock;
    ``following`` yields the next sequence for a replacement sample.
    """

    policy = MineruStreamPolicy(config(qualified_max=maximum))
    end, sequence = warm(policy, maximum)
    pressure = Pressure(sample(sequence, end))
    following = [sequence, int(end)]
    return StreamAdmissionControl(policy, pressure, monotonic=lambda: end), pressure, following


def successor(following: list[int], **changes: object):
    following[0] += 1
    return sample(following[0], following[1], **changes)


class _HeldPreflightBackend(_Backend):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.preflight_entered: list[str] = []
        self.preflight_release = threading.Event()

    def prepare_remote_io(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        self.preflight_entered.append(work.attempt_id)
        while not self.preflight_release.wait(.001):
            stage_guard.checkpoint()
        return super().prepare_remote_io(
            work, credit_allowance=credit_allowance, stage_guard=stage_guard
        )


class StagedParseStreamAdmissionTests(unittest.TestCase):
    def start(self, coordinator: StagedParseCoordinator):
        results: list[CoordinatorResult] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                results.append(coordinator.run())
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        return thread, results, errors

    def test_one_shot_waits_for_fresh_cold_recovery_before_source_scan(self) -> None:
        class Clock:
            now = 0.0

            def tick(self) -> float:
                self.now += 1.0
                return self.now

        class FreshPressure:
            sequence = 0

            def latest(self):
                self.sequence += 1
                return sample(self.sequence, clock.now)

        clock = Clock()
        backend = _Backend(new=(_work("attempt-cold", "prepared"),))
        snapshots: list[CoordinatorSnapshot] = []

        def progress(snapshot: CoordinatorSnapshot) -> None:
            snapshots.append(snapshot)
            if snapshot.stream_target == 0:
                self.assertFalse(any(call.startswith("admit:") for call in backend.calls))

        result = StagedParseCoordinator(
            backend=backend, limits=_limits(), progress=progress,
            stream_control=StreamAdmissionControl(
                MineruStreamPolicy(config(qualified_max=1)), FreshPressure(),
                monotonic=clock.tick,
            ),
        ).run(wait_for_stream_admission=True)
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual((result.admitted, result.completed), (1, 1))
        self.assertTrue(any(s.stream_target == 0 and s.blocked_reason == "stream_pause:recovery" for s in snapshots))
        self.assertTrue(any(s.stream_target == 1 for s in snapshots))
        self.assertTrue(any(call.startswith("admit:") for call in backend.calls))

    def test_one_shot_pause_obeys_stop_before_recovery(self) -> None:
        class Clock:
            now = 0.0

            def tick(self) -> float:
                self.now += 1.0
                return self.now

        class FreshPressure:
            sequence = 0

            def latest(self):
                self.sequence += 1
                return sample(self.sequence, clock.now)

        clock = Clock()
        backend = _Backend(new=(_work("attempt-pending", "prepared"),))
        snapshots: list[CoordinatorSnapshot] = []
        deadline = 4.0

        result = StagedParseCoordinator(
            backend=backend, limits=_limits(), progress=snapshots.append,
            stream_control=StreamAdmissionControl(
                MineruStreamPolicy(config(qualified_max=1)), FreshPressure(),
                monotonic=clock.tick,
            ),
        ).run(stop_requested=lambda: clock.now >= deadline, wait_for_stream_admission=True)
        self.assertGreaterEqual(clock.now, deadline)
        self.assertLess(clock.now, 11.0)
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(result.admitted, 0)
        self.assertEqual(len(backend.new), 1)
        self.assertFalse(any(call.startswith("admit:") for call in backend.calls))

    def test_one_shot_unsafe_pressure_still_stops_without_admission(self) -> None:
        backend = _Backend(new=(_work("attempt-pending", "prepared"),))
        result = StagedParseCoordinator(
            backend=backend, limits=_limits(),
            stream_control=control(Pressure(sample(1, 10, unsafe_reason="identity_drift"))),
        ).run(wait_for_stream_admission=True)
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertEqual(result.admitted, 0)
        self.assertFalse(any(call.startswith("admit:") for call in backend.calls))

    def test_default_run_keeps_idle_quiescence_during_cold_pause(self) -> None:
        backend = _Backend(new=(_work("attempt-pending", "prepared"),))
        result = StagedParseCoordinator(
            backend=backend, limits=_limits(),
            stream_control=control(Pressure(sample(1, 10))),
        ).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(result.admitted, 0)
        self.assertFalse(any(call.startswith("admit:") for call in backend.calls))

    def test_one_shot_exhausted_scope_finishes_after_pressure_drops(self) -> None:
        held, pressure, following = warmed(maximum=1)

        class DropAfterAck(_Backend):
            def acknowledge(self, work: CoordinatorWork, *, stage_guard: StageLeaseGuard) -> CoordinatorWork:
                result = super().acknowledge(work, stage_guard=stage_guard)
                pressure.value = successor(following, host_available_bytes=0)
                return result

        backend = DropAfterAck(new=(_work("attempt-only", "prepared"),))
        snapshots: list[CoordinatorSnapshot] = []
        forced_stop = threading.Event()

        def progress(snapshot: CoordinatorSnapshot) -> None:
            snapshots.append(snapshot)
            if sum(s.stream_reason == "memory_pause" for s in snapshots) >= 2:
                forced_stop.set()

        result = StagedParseCoordinator(
            backend=backend, limits=_limits(), stream_control=held,
            progress=progress,
        ).run(stop_requested=forced_stop.is_set, wait_for_stream_admission=True)
        self.assertFalse(forced_stop.is_set())
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual((result.admitted, result.completed), (1, 1))
        self.assertTrue(any(s.stream_target == 0 and s.stream_reason == "memory_pause" for s in snapshots))

    def test_one_durable_plus_one_provisional_remote_wait_fills_target(self) -> None:
        backend = _HeldPreflightBackend(recoverable=(
            _work("attempt-a-durable", "submitted", 2),
            _work("attempt-b-prepared", "prepared"),
            _work("attempt-c-prepared", "prepared"),
            _work("attempt-d-prepared", "prepared"),
        ))
        backend.block_remote = True
        snapshots: list[CoordinatorSnapshot] = []
        filled = threading.Event()

        def progress(snapshot: CoordinatorSnapshot) -> None:
            snapshots.append(snapshot)
            if snapshot.stream_actual == 2 and backend.preflight_entered:
                filled.set()

        held, _pressure, _following = warmed()
        thread, results, errors = self.start(StagedParseCoordinator(
            backend=backend,
            limits=_limits(preflight_workers=4),
            stream_control=held,
            progress=progress,
        ))
        try:
            self.assertTrue(filled.wait(2), errors)
            self.assertEqual(len(backend.preflight_entered), 1)
            self.assertTrue(all(s.stream_actual is None or s.stream_actual <= 2 for s in snapshots))
        finally:
            backend.preflight_release.set()
            backend.remote_release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0].terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(results[0].completed, 4)

    def test_reduced_target_keeps_existing_remote_and_tail_work_until_release(self) -> None:
        backend = _Backend(recoverable=(
            _work("attempt-a-accepted", "submitted", 2),
            _work("attempt-b-accepted", "submitted", 2),
            _work("attempt-c-prepared", "prepared"),
            _work("attempt-d-tail", "ack_pending", 7),
        ))
        backend.block_remote = True
        held, pressure, following = warmed()
        snapshots: list[CoordinatorSnapshot] = []
        two_held = threading.Event()
        reduced_and_tail_done = threading.Event()

        def progress(snapshot: CoordinatorSnapshot) -> None:
            snapshots.append(snapshot)
            if snapshot.stream_actual == 2:
                two_held.set()
            if snapshot.stream_target == 1 and snapshot.completed >= 1:
                reduced_and_tail_done.set()

        thread, results, errors = self.start(StagedParseCoordinator(
            backend=backend, limits=_limits(),
            stream_control=held, progress=progress,
        ))
        try:
            self.assertTrue(two_held.wait(2), errors)
            pressure.value = successor(following, gpu_free_bytes=GIB-1)
            self.assertTrue(reduced_and_tail_done.wait(2), errors)
            self.assertNotIn("preflight:attempt-c-prepared", backend.calls)
            self.assertIn("ack:attempt-d-tail:ack_pending", backend.calls)
            self.assertTrue(any(s.stream_target == 1 and s.stream_actual == 2 for s in snapshots))
            # Reduction closes new POSTs; a fresh clear sample re-opens them
            # within the held reduced target once the accepted waits drain.
            pressure.value = successor(following)
        finally:
            backend.remote_release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0].errors, ())
        self.assertEqual(results[0].completed, 4)
        self.assertEqual(results[0].credits_in_use, ResourceCreditVector())
        self.assertIn("preflight:attempt-c-prepared", backend.calls)
        self.assertTrue(all(s.stream_actual is None or s.stream_actual <= 2 for s in snapshots))

    def test_paused_admission_still_polls_materializes_and_acks_accepted_work(self) -> None:
        backend = _Backend(
            recoverable=(
                _work("attempt-a-poll", "submitted", 2),
                _work("attempt-b-result", "remote_terminal", 3),
                _work("attempt-c-ack", "ack_pending", 7),
            ),
            new=(_work("attempt-new", "prepared"),),
        )
        result = StagedParseCoordinator(
            backend=backend, limits=_limits(),
            stream_control=control(Pressure(sample(1, 10, host_available_bytes=0))),
        ).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(result.admitted, 0)
        self.assertEqual(result.completed, 3)
        self.assertEqual(len(backend.new), 1)
        self.assertIn("remote:attempt-a-poll:submitted", backend.calls)
        self.assertIn("local_prepare:attempt-b-result", backend.calls)
        self.assertIn("ack:attempt-c-ack:ack_pending", backend.calls)
        self.assertEqual(result.credits_in_use, ResourceCreditVector())

    def test_pause_does_not_hide_a_durable_transition_exceeding_hard_credit(self) -> None:
        class ExceedingBackend(_Backend):
            def run_remote(self, work, *, credit_allowance, stage_guard):
                result = super().run_remote(
                    work, credit_allowance=credit_allowance, stage_guard=stage_guard
                )
                # The durable reply claims more result credit than was granted.
                return replace(
                    result,
                    credits=replace(result.credits, provider_result_bytes=101),
                    credit_reservation=replace(result.credit_reservation, provider_result_bytes=101),
                )

        backend = ExceedingBackend(recoverable=(
            _work("attempt-a-owned", "submitted", 2),
            _work("attempt-b-waiting", "prepared"),
        ))
        result = StagedParseCoordinator(
            backend=backend, limits=_limits(),
            stream_control=control(Pressure(sample(1, 10, gpu_free_bytes=0))),
        ).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertTrue(any("credit grant" in error for error in result.errors))
        self.assertGreater(result.credits_in_use.documents, 0)
        self.assertEqual(result.completed, 0)
        self.assertNotIn("preflight:attempt-b-waiting", backend.calls)


if __name__ == "__main__":
    unittest.main()
