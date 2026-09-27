"""Independent coordinator witness for one shared heavy-work execution permit.

The real coordinator and its recovery/credit scheduler are used. The backend
marks where LOCAL decode, COMMIT reopen/build and CLEANUP revalidation would
execute, but never parses, publishes, opens a database or allocates large data.
"""

from __future__ import annotations

from dataclasses import replace
import threading
import time
import unittest

from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorResult, CoordinatorTerminal, StageHeavyWorkRequired, StageWaiting,
    StagedParseCoordinator,
)
from tests.unit.test_staged_parse_coordinator import _Backend, _Clock, _LIMIT, _limits, _work


class _HeavyBackend(_Backend):
    def __init__(self, *, recoverable, block: tuple[str, str]) -> None:
        super().__init__(recoverable=recoverable)
        self.block = block
        self.release = threading.Event()
        self.entered: dict[tuple[str, str], threading.Event] = {}
        self.ack_finished = threading.Event()
        self.renewed_waiter = threading.Event()
        self._activity_lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.order: list[tuple[str, str]] = []
        self.wait_ack_for: tuple[str, str] | None = None
        self.wait_renew_for: str | None = None
        self.fail_first_commit_with_wait = False
        self._failed_once = False

    def signal(self, phase: str, attempt: str) -> threading.Event:
        return self.entered.setdefault((phase, attempt), threading.Event())

    def _heavy(self, phase, work, credit_allowance, stage_guard, call):
        stage_guard.checkpoint()
        if phase in {"local", "commit"} and getattr(stage_guard, "heavy_work_permitted", None) is False:
            raise StageHeavyWorkRequired("synthetic materializer waits for the heavy permit",
                                         retry_after_seconds=0.001)
        if phase == "cleanup":
            # Current cleanup validates strong inventory/ownership without a
            # whole-object decode, so its entry is intentionally light.
            return call(work, credit_allowance=credit_allowance, stage_guard=stage_guard)
        with self._activity_lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.order.append((phase, work.attempt_id))
        self.signal(phase, work.attempt_id).set()
        try:
            if (phase, work.attempt_id) == self.block:
                while not self.release.wait(0.001):
                    stage_guard.checkpoint()
            stage_guard.checkpoint()
            if ((phase, work.attempt_id) == self.block
                    and phase == "commit" and self.fail_first_commit_with_wait
                    and not self._failed_once):
                self._failed_once = True
                raise StageWaiting("synthetic recoverable heavy-stage interruption",
                                   retry_after_seconds=0.05)
            return call(work, credit_allowance=credit_allowance, stage_guard=stage_guard)
        finally:
            with self._activity_lock:
                self.active -= 1

    def run_local(self, work, *, credit_allowance, stage_guard):
        return self._heavy("local", work, credit_allowance, stage_guard,
                           super().run_local)

    def commit(self, work, *, credit_allowance, stage_guard):
        return self._heavy("commit", work, credit_allowance, stage_guard,
                           super().commit)

    def cleanup(self, work, *, credit_allowance, stage_guard):
        return self._heavy("cleanup", work, credit_allowance, stage_guard,
                           super().cleanup)

    def acknowledge(self, work, *, stage_guard):
        if self.wait_ack_for is not None and work.attempt_id == "ack-light":
            self.signal(*self.wait_ack_for).wait(0.5)
        answer = super().acknowledge(work, stage_guard=stage_guard)
        if work.attempt_id == "ack-light":
            self.ack_finished.set()
        return answer

    def claim_recovery(self, candidate):
        claimed = super().claim_recovery(candidate)
        if claimed.attempt_id == self.wait_renew_for:
            # The queued claim is valid on admission but approaches renewal
            # while another stage is held. The active stage still has time.
            return replace(claimed, lease_expires_monotonic=self.clock() + 3)
        return claimed

    def renew_claim(self, work, *, lease_seconds):
        renewed = super().renew_claim(work, lease_seconds=lease_seconds)
        if work.attempt_id == self.wait_renew_for:
            self.renewed_waiter.set()
        return renewed


def _start(backend: _HeavyBackend, *, limits, monotonic=time.monotonic,
           process_guard=lambda: None):
    result: list[CoordinatorResult] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            result.append(StagedParseCoordinator(
                backend=backend, limits=limits, monotonic=monotonic,
                process_guard=process_guard,
            ).run())
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=run, name="independent-heavy-coordinator", daemon=True)
    thread.start()
    return thread, result, failures


class HeavyPhaseExclusivityIndependentTests(unittest.TestCase):
    def test_heavy_commit_excludes_local_decode_while_light_cleanup_ack_and_renewal_continue(self) -> None:
        backend = _HeavyBackend(
            recoverable=(
                _work("ack-light", "ack_pending", 5),
                _work("cleanup-light", "cleanup_pending", 5),
                _work("commit-first", "local_materialized", 5),
                _work("local-next", "materializing", 5),
            ),
            block=("commit", "commit-first"),
        )
        clock = _Clock()
        backend.clock = clock
        backend.wait_renew_for = "local-next"
        backend.wait_ack_for = backend.block
        limits = _limits(
            credits=replace(_LIMIT, materialization_items=1,
                            decoded_bytes=400, temp_disk_bytes=500),
            local_workers=1, commit_workers=1, cleanup_workers=1, ack_workers=1,
            max_stage_step_seconds=5, claim_lease_seconds=20,
            claim_renew_margin_seconds=2,
        )
        thread, result, failures = _start(backend, limits=limits, monotonic=clock)
        try:
            self.assertTrue(backend.signal(*backend.block).wait(1),
                            f"cleanup never entered; calls={backend.calls!r}; "
                            f"results={result!r}; failures={failures!r}")
            self.assertTrue(backend.ack_finished.wait(1), "light ACK stalled behind heavy work")
            self.assertIn("cleanup:cleanup-light:cleanup_pending", backend.calls,
                          "light cleanup stalled behind the heavy permit")
            clock.advance(2)
            self.assertTrue(backend.renewed_waiter.wait(1), "queued claim did not renew")
            self.assertFalse(backend.signal("local", "local-next").wait(0.05),
                             f"LOCAL decode entered while heavy COMMIT was active: {backend.order!r}")
        finally:
            backend.release.set()
            thread.join(3)
            self.assertFalse(thread.is_alive(), "coordinator did not drain after release")
        self.assertFalse(thread.is_alive(), "coordinator did not drain")
        self.assertEqual(failures, [])
        self.assertEqual(result[0].terminal, CoordinatorTerminal.QUIESCENT, repr(result[0]))
        self.assertEqual(backend.peak, 1, backend.order)
        self.assertEqual(backend.order[0], ("commit", "commit-first"))
        self.assertTrue(backend.signal("local", "local-next").is_set())

    def test_recoverable_commit_interruption_releases_heavy_work_for_successor(self) -> None:
        backend = _HeavyBackend(
            recoverable=(
                _work("commit-a", "local_materialized", 5),
                _work("commit-b", "local_materialized", 5),
            ),
            block=("commit", "commit-a"),
        )
        backend.fail_first_commit_with_wait = True
        thread, result, failures = _start(
            backend, limits=_limits(commit_workers=2, cleanup_workers=1),
        )
        try:
            self.assertTrue(backend.signal("commit", "commit-a").wait(1))
            self.assertFalse(backend.signal("commit", "commit-b").wait(0.05),
                             f"second COMMIT entered before the first released its permit: {backend.order!r}")
        finally:
            backend.release.set()
            thread.join(3)
            self.assertFalse(thread.is_alive(), "coordinator did not drain after release")
        self.assertFalse(thread.is_alive(), "coordinator did not drain")
        self.assertEqual(failures, [])
        self.assertEqual(result[0].terminal, CoordinatorTerminal.QUIESCENT)
        self.assertTrue(backend.signal("commit", "commit-b").is_set())
        self.assertEqual(backend.peak, 1, backend.order)
        attempts_a = [index for index, item in enumerate(backend.order)
                      if item == ("commit", "commit-a")]
        self.assertEqual(len(attempts_a), 2, backend.order)
        self.assertLess(backend.order.index(("commit", "commit-b")),
                        attempts_a[1], backend.order)
        self.assertEqual(dict(result[0].final_states),
                         {"commit-a": "acked", "commit-b": "acked"})

    def test_revoked_heavy_stage_does_not_trap_a_later_coordinator(self) -> None:
        backend = _HeavyBackend(
            recoverable=(_work("commit-after-restart", "local_materialized", 5),),
            block=("commit", "commit-after-restart"),
        )
        lose_owner = threading.Event()

        def process_guard() -> None:
            if lose_owner.is_set():
                raise RuntimeError("synthetic process ownership loss")

        first_thread, first_result, first_failures = _start(
            backend, limits=_limits(), process_guard=process_guard,
        )
        try:
            self.assertTrue(backend.signal(*backend.block).wait(1))
            lose_owner.set()
            first_thread.join(3)
            self.assertFalse(first_thread.is_alive(), "revoked stage did not drain")
        finally:
            # Bound teardown even if the coordinator failed to revoke its guard.
            backend.release.set()
            first_thread.join(3)
        self.assertEqual(backend.active, 0)
        self.assertTrue(
            first_failures or (first_result and first_result[0].terminal
                               is CoordinatorTerminal.STUCK_OPEN_CIRCUIT),
            "synthetic ownership loss was not observed",
        )

        # The fake recovery row is deliberately unchanged: a new coordinator
        # must be able to take the same original obligation after this stop.
        backend.block = ("none", "none")
        second_thread, second_result, second_failures = _start(backend, limits=_limits())
        second_thread.join(3)
        self.assertFalse(second_thread.is_alive(), "later coordinator stalled on stale heavy work")
        self.assertEqual(second_failures, [])
        self.assertEqual(second_result[0].terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(dict(second_result[0].final_states), {"commit-after-restart": "acked"})


if __name__ == "__main__":
    unittest.main()
