"""Offline real-coordinator regression for completed heavy Future retention.

Run with PYTHONPATH=<service>/src:<service> python3 -B -m unittest -v
test_scheduler_retention_review from the containing directory. No DB, model,
PDF, worker, or production resource is used.
"""

from __future__ import annotations

import gc
import threading
import unittest
import weakref

from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorTerminal,
    StageWaiting,
)
from tests.unit.test_heavy_phase_exclusivity_independent import _start
from tests.unit.test_staged_parse_coordinator import _Backend, _limits, _work


class _WholeObject:
    __slots__ = ("bytes", "__weakref__")

    def __init__(self) -> None:
        self.bytes = bytearray(64 * 1024)


class _WaitingCommitBackend(_Backend):
    """Two first COMMITs fail while two queued COMMITs replace them."""

    def __init__(self) -> None:
        super().__init__(recoverable=(
            _work("commit-a", "local_materialized", 5),
            _work("commit-b", "local_materialized", 5),
            _work("commit-c", "local_materialized", 5),
            _work("commit-d", "local_materialized", 5),
        ))
        self._state_lock = threading.Lock()
        self._first_pair = threading.Barrier(2)
        self._started: set[str] = set()
        self.first_payload_refs: dict[str, weakref.ReferenceType[_WholeObject]] = {}
        self.replacement_observations: list[tuple[str, dict[str, bool]]] = []

    def commit(self, work, *, credit_allowance, stage_guard):
        stage_guard.checkpoint()
        with self._state_lock:
            first = work.attempt_id not in self._started
            self._started.add(work.attempt_id)
        if first and work.attempt_id in {"commit-a", "commit-b"}:
            # This local is held by the propagated exception traceback in the
            # real Future until the coordinator drops that Future reference.
            materialized = _WholeObject()
            with self._state_lock:
                self.first_payload_refs[work.attempt_id] = weakref.ref(materialized)
            self._first_pair.wait(timeout=3)
            raise StageWaiting("synthetic recoverable commit wait", retry_after_seconds=0.5)

        # C/D are queued behind the two heavy permits. At least one first
        # Future is completed when they enter; a completed Future's traceback
        # must not keep its old whole object while its permit is reused.
        if work.attempt_id in {"commit-c", "commit-d"}:
            gc.collect()
            with self._state_lock:
                alive = {attempt: ref() is not None
                         for attempt, ref in self.first_payload_refs.items()}
                self.replacement_observations.append((work.attempt_id, alive))
        return super().commit(
            work, credit_allowance=credit_allowance, stage_guard=stage_guard,
        )


class SchedulerRetentionReview(unittest.TestCase):
    def test_two_waiting_commits_release_whole_objects_before_replacement(self) -> None:
        backend = _WaitingCommitBackend()
        thread, results, failures = _start(
            backend, limits=_limits(commit_workers=2, heavy_work_permits=2),
        )
        thread.join(6)
        self.assertFalse(thread.is_alive(), "real coordinator did not drain")
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(dict(results[0].final_states),
                         {key: "acked" for key in ("commit-a", "commit-b", "commit-c", "commit-d")})
        self.assertEqual(set(backend.first_payload_refs), {"commit-a", "commit-b"})
        self.assertEqual(len(backend.replacement_observations), 2)
        for attempt, alive in backend.replacement_observations:
            self.assertEqual(set(alive), {"commit-a", "commit-b"}, attempt)
            self.assertFalse(all(alive.values()),
                             f"{attempt} entered while both first COMMIT objects remained: {alive}")


if __name__ == "__main__":
    unittest.main()
