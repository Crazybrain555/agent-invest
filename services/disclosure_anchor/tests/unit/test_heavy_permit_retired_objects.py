"""A retired heavy stage's whole object is gone before its permit is reused.

The real coordinator runs on the shared fake backend. Every COMMIT entry keeps a
weakly referenced surrogate in its frame, as the V4 backend keeps the reopened
materialized document while it publishes, so a stage exception's traceback
keeps it too. The cyclic collector is disabled: a release seen here is plain
reference counting, with no collection pass. Only consumed stages are checked;
a stage still running, or done but not yet consumed, keeps its object and its
permit. No database, provider, model, PDF or worker process is involved.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import gc
import threading
import time
import unittest
import weakref

from disclosure_anchor.application.ports.worker_stop_control import (
    PublicStopCause, exception_fingerprint,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorResult, CoordinatorTerminal, CoordinatorWork, RetryStage, StageCapacityBlocked,
    StageProviderWaiting, StageResourceGrantRequired, StageWaiting,
)
from disclosure_anchor.application.services.worker_stop_latch import InProcessWorkerStopLatch
from tests.unit.test_heavy_phase_exclusivity_independent import _start
from tests.unit.test_staged_parse_coordinator import (
    _LIFECYCLE_RESERVATION, _Backend, _limits, _work,
)


class _WholeObject:
    __slots__ = ("payload", "__weakref__")

    def __init__(self) -> None:
        self.payload = bytearray(64 * 1024)


class _RetiringBackend(_Backend):
    """COMMIT entries hold a whole object; chosen first entries retire by exception."""

    def __init__(
        self,
        *,
        recoverable: tuple[CoordinatorWork, ...],
        retire: dict[str, Callable[[], BaseException]] | None = None,
        hold: tuple[str, ...] = (),
        hold_until_revoked: tuple[str, ...] = (),
    ) -> None:
        super().__init__(recoverable=recoverable)
        # Factories, so no frame keeps the raised exception in a local.
        self.retire = dict(retire or {})
        self.hold = set(hold)
        self.hold_until_revoked = set(hold_until_revoked)
        self.release = threading.Event()
        # ("entered" | "freed", "<attempt>#<entry>") in actual order. The
        # weakref callback runs in whichever thread drops the last reference,
        # so it appends without a lock.
        self.events: list[tuple[str, str]] = []
        self.refs: dict[str, weakref.ReferenceType[_WholeObject]] = {}
        self.alive_at_entry: dict[str, dict[str, bool]] = {}
        self._lock = threading.Lock()
        self._entries: dict[str, int] = {}
        self._entered: dict[str, threading.Event] = {}
        self.active = 0
        self.peak = 0

    def entered(self, key: str) -> threading.Event:
        with self._lock:
            return self._entered.setdefault(key, threading.Event())

    def _freed(self, key: str) -> Callable[[weakref.ReferenceType[_WholeObject]], None]:
        return lambda _ref: self.events.append(("freed", key))

    def commit(self, work, *, credit_allowance, stage_guard):
        stage_guard.checkpoint()
        attempt = work.attempt_id
        with self._lock:
            entry = self._entries[attempt] = self._entries.get(attempt, 0) + 1
            key = f"{attempt}#{entry}"
            self.alive_at_entry[key] = {name: ref() is not None for name, ref in self.refs.items()}
            self.events.append(("entered", key))
            self.active += 1
            self.peak = max(self.peak, self.active)
        materialized = _WholeObject()
        self.refs[key] = weakref.ref(materialized, self._freed(key))
        self.entered(key).set()
        try:
            if attempt in self.hold:
                while not self.release.wait(0.001):
                    stage_guard.checkpoint()
            if attempt in self.hold_until_revoked:
                # Never checkpoint first: only the coordinator's guard pass
                # may find the spent deadline and revoke this stage.
                while stage_guard.revocation_provenance is None and not self.release.is_set():
                    time.sleep(0.001)
                stage_guard.checkpoint()
            factory = self.retire.pop(attempt, None) if entry == 1 else None
            if factory is not None:
                raise factory()
            return super().commit(work, credit_allowance=credit_allowance, stage_guard=stage_guard)
        finally:
            with self._lock:
                self.active -= 1


def _run(test: unittest.TestCase, backend: _RetiringBackend, *, limits, stop_control=None,
         after_start: Callable[[], None] = lambda: None) -> CoordinatorResult:
    thread, results, failures = _start(backend, limits=limits, stop_control=stop_control)
    try:
        after_start()
    finally:
        thread.join(6)
        backend.release.set()
        thread.join(3)
    test.assertFalse(thread.is_alive(), f"coordinator did not drain: {backend.events!r}")
    test.assertEqual(failures, [])
    test.assertEqual(len(results), 1)
    return results[0]


class RetiredHeavyObjectTests(unittest.TestCase):
    def setUp(self) -> None:
        if gc.isenabled():
            gc.disable()
            self.addCleanup(gc.enable)

    def assert_freed_before(self, backend: _RetiringBackend, retired: str, entry: str) -> None:
        events = backend.events
        self.assertIn(("freed", retired), events, f"{retired} is still resident: {events!r}")
        self.assertLess(events.index(("freed", retired)), events.index(("entered", entry)), events)
        self.assertFalse(backend.alive_at_entry[entry][retired], events)

    def test_every_recoverable_outcome_frees_its_object_before_the_permit_is_reused(self) -> None:
        # One COMMIT worker and the single implied permit: the next COMMIT can
        # only enter after the retired one was consumed and its worker
        # finished, so the order below is exact.
        grown = replace(_LIFECYCLE_RESERVATION, output_bytes=_LIFECYCLE_RESERVATION.output_bytes + 100)
        outcomes: dict[str, tuple[Callable[[], BaseException], bool]] = {
            "wait": (lambda: StageWaiting("synthetic wait", retry_after_seconds=0.05), True),
            "provider_wait": (lambda: StageProviderWaiting("synthetic provider wait",
                                                           retry_after_seconds=0.05), True),
            "retry": (lambda: RetryStage("synthetic retry", retry_after_seconds=0.05), True),
            "grant": (lambda: StageResourceGrantRequired("synthetic grant", required=grown), True),
            "capacity_hold": (lambda: StageCapacityBlocked(
                "synthetic hold", dimensions=("publication_envelope",)), False),
        }
        for name, (factory, retried) in outcomes.items():
            with self.subTest(outcome=name):
                backend = _RetiringBackend(
                    recoverable=(_work("commit-a", "local_materialized", 5),
                                 _work("commit-b", "local_materialized", 5)),
                    retire={"commit-a": factory},
                )
                result = _run(self, backend, limits=_limits(
                    commit_workers=1, cleanup_workers=1, poll_seconds=0.1,
                ))
                later = [key for kind, key in backend.events
                         if kind == "entered" and key != "commit-a#1"]
                self.assertIn("commit-b#1", later, backend.events)
                for key in later:
                    self.assert_freed_before(backend, "commit-a#1", key)
                self.assertEqual(backend.peak, 1, backend.events)
                if retried:
                    self.assertIn("commit-a#2", later, backend.events)
                    self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT, result)
                    self.assertEqual(dict(result.final_states),
                                     {"commit-a": "acked", "commit-b": "acked"})
                else:
                    # The held attempt stays claimed and visible; nothing
                    # re-enters it and the idle site stops for the hold.
                    self.assertEqual(later, ["commit-b#1"])
                    self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT, result)
                    self.assertEqual(dict(result.final_states), {"commit-b": "acked"})
                    self.assertTrue(any(
                        getattr(event, "attempt_id", None) == "commit-a"
                        and "publication_envelope" in getattr(event, "dimensions", ())
                        for event in result.diagnostics), result.diagnostics)

    def test_two_permits_free_the_retired_holder_and_keep_the_running_one(self) -> None:
        backend = _RetiringBackend(
            recoverable=(_work("commit-a", "local_materialized", 5),
                         _work("commit-b", "local_materialized", 5),
                         _work("commit-c", "local_materialized", 5)),
            retire={"commit-b": lambda: StageWaiting("synthetic wait", retry_after_seconds=0.05)},
            hold=("commit-a",),
        )

        def reuse_then_release() -> None:
            # A keeps its permit and its worker; C can only take B's permit
            # and B's worker, so B's worker has finished with it.
            self.assertTrue(backend.entered("commit-c#1").wait(3), backend.events)
            backend.release.set()

        result = _run(self, backend, limits=_limits(
            commit_workers=2, cleanup_workers=1, heavy_work_permits=2, poll_seconds=0.1,
        ), after_start=reuse_then_release)
        self.assert_freed_before(backend, "commit-b#1", "commit-c#1")
        # The running holder is not asked to give anything up early.
        self.assertTrue(backend.alive_at_entry["commit-c#1"]["commit-a#1"], backend.events)
        self.assertLess(backend.events.index(("entered", "commit-c#1")),
                        backend.events.index(("freed", "commit-a#1")), backend.events)
        self.assertEqual(backend.peak, 2, backend.events)
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT, result)
        self.assertEqual(dict(result.final_states),
                         {"commit-a": "acked", "commit-b": "acked", "commit-c": "acked"})

    def test_a_stage_fault_keeps_its_diagnostics_and_frees_its_object(self) -> None:
        backend = _RetiringBackend(
            recoverable=(_work("commit-a", "local_materialized", 5),
                         _work("commit-b", "local_materialized", 5)),
            retire={"commit-a": lambda: RuntimeError("synthetic commit fault")},
        )
        result = _run(self, backend, limits=_limits(commit_workers=1, cleanup_workers=1))
        self.assertIn(("freed", "commit-a#1"), backend.events)
        self.assertNotIn(("entered", "commit-b#1"), backend.events)
        self.assertEqual((result.terminal, result.termination_kind),
                         (CoordinatorTerminal.STUCK_OPEN_CIRCUIT, "public_stop"))
        self.assertIn("commit-a:commit:RuntimeError:synthetic commit fault", result.errors)
        cause = result.stop_cause
        assert cause is not None
        self.assertEqual(
            (cause.kind, cause.reason_code, cause.origin, cause.attempt_id, cause.lane,
             cause.exception_class, cause.exception_fingerprint),
            ("stage_fault", "commit_unexpected_failure", "stage_call", "commit-a", "commit",
             "builtins.RuntimeError", exception_fingerprint(RuntimeError("synthetic commit fault"))),
        )

    def test_a_public_stop_frees_both_drained_holders_and_dispatches_no_third(self) -> None:
        backend = _RetiringBackend(
            recoverable=(_work("commit-a", "local_materialized", 5),
                         _work("commit-b", "local_materialized", 5),
                         _work("commit-c", "local_materialized", 5)),
            hold=("commit-a", "commit-b"),
        )
        latch = InProcessWorkerStopLatch()
        cause = PublicStopCause(kind="maintenance_fatal", reason_code="maintenance_loop_failed",
                                origin="maintenance")

        def trip_when_both_hold() -> None:
            self.assertTrue(backend.entered("commit-a#1").wait(3), backend.events)
            self.assertTrue(backend.entered("commit-b#1").wait(3), backend.events)
            latch.trip(cause)

        result = _run(self, backend, limits=_limits(commit_workers=3, heavy_work_permits=2),
                      stop_control=latch, after_start=trip_when_both_hold)
        for key in ("commit-a#1", "commit-b#1"):
            self.assertIn(("freed", key), backend.events)
        self.assertNotIn(("entered", "commit-c#1"), backend.events)
        self.assertEqual((result.terminal, result.termination_kind, result.stop_cause),
                         (CoordinatorTerminal.STUCK_OPEN_CIRCUIT, "public_stop", cause))
        for attempt in ("commit-a", "commit-b"):
            self.assertIn(f"{attempt}:commit:public_stop:bounded stage lease expired", result.errors)
        self.assertEqual((backend.active, backend.peak), (0, 2), backend.events)

    def test_a_guard_reported_deadline_frees_its_object_once_the_stage_drains(self) -> None:
        backend = _RetiringBackend(
            recoverable=(_work("commit-a", "local_materialized", 5),),
            hold_until_revoked=("commit-a",),
        )
        result = _run(self, backend, limits=_limits(
            commit_workers=1, cleanup_workers=1, max_stage_step_seconds=0.2,
        ))
        self.assertIn(("freed", "commit-a#1"), backend.events)
        # First the guard's own report, then the drained stage's.
        self.assertEqual(
            [error for error in result.errors if error.startswith("commit-a:commit:")],
            ["commit-a:commit:stage deadline exceeded",
             "commit-a:commit:stage deadline exceeded:bounded stage lease expired"],
        )
        # The guard's revocation lets the stage latch its own copy of the same
        # cause, so either may be first.
        cause = result.stop_cause
        assert cause is not None
        self.assertEqual((cause.kind, cause.reason_code, cause.attempt_id),
                         ("deadline_exhausted", "bounded_stage_deadline_exceeded", "commit-a"))
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT, result)


if __name__ == "__main__":
    unittest.main()
