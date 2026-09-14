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
from tests.unit.test_mineru_stream_policy import GIB, Pressure, config, sample
from tests.unit.test_staged_parse_coordinator import _Backend, _limits, _work


def control(pressure: Pressure, maximum: int = 2) -> StreamAdmissionControl:
    return StreamAdmissionControl(
        MineruStreamPolicy(config(qualified_max=maximum)),
        pressure,
        monotonic=lambda: 10,
    )


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

        thread, results, errors = self.start(StagedParseCoordinator(
            backend=backend,
            limits=_limits(preflight_workers=4),
            stream_control=control(Pressure(sample(1, 10))),
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
        pressure = Pressure(sample(1, 10))
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
            stream_control=control(pressure), progress=progress,
        ))
        try:
            self.assertTrue(two_held.wait(2), errors)
            pressure.value = sample(2, 10, gpu_free_bytes=GIB-1)
            self.assertTrue(reduced_and_tail_done.wait(2), errors)
            self.assertNotIn("preflight:attempt-c-prepared", backend.calls)
            self.assertIn("ack:attempt-d-tail:ack_pending", backend.calls)
            self.assertTrue(any(s.stream_target == 1 and s.stream_actual == 2 for s in snapshots))
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
