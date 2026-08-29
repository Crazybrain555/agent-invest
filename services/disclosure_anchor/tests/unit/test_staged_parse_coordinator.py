from __future__ import annotations

from dataclasses import replace
import threading
import time
import unittest

from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorLimits,
    CoordinatorTerminal,
    CoordinatorWork,
    CreditVector,
    RecoveryDeferred,
    RetryStage,
    StagedParseCoordinator,
)


_LIMIT = CreditVector(
    documents=8,
    remote_waits=4,
    retained_results=4,
    retained_bytes=10_000,
    local_items=4,
    compressed_bytes=10_000,
    decoded_bytes=20_000,
    temp_disk_bytes=30_000,
    db_stage_items=4,
    ack_items=4,
    unpublished_pages=1_000,
)


def _work(attempt_id: str, state: str, version: int = 0) -> CoordinatorWork:
    state_credits = {
        "prepared": CreditVector(documents=1),
        "reconciling": CreditVector(documents=1, remote_waits=1),
        "submitted": CreditVector(documents=1, remote_waits=1),
        "remote_terminal": CreditVector(
            documents=1, retained_results=1, retained_bytes=100
        ),
        "materializing": CreditVector(
            documents=1,
            retained_results=1,
            retained_bytes=100,
            local_items=1,
            compressed_bytes=100,
            decoded_bytes=400,
            temp_disk_bytes=500,
        ),
        "local_materialized": CreditVector(
            documents=1,
            retained_results=1,
            retained_bytes=100,
            db_stage_items=1,
            unpublished_pages=10,
        ),
        "finish_committed": CreditVector(
            documents=1, retained_results=1, retained_bytes=100, ack_items=1
        ),
        "remote_failure_committed": CreditVector(
            documents=1, retained_results=1, ack_items=1
        ),
        "local_failure_committed": CreditVector(
            documents=1, retained_results=1, retained_bytes=100, ack_items=1
        ),
        "acked": CreditVector(),
        "remote_failed": CreditVector(),
        "local_failed": CreditVector(),
        "pre_submission_failed": CreditVector(),
    }
    return CoordinatorWork(
        attempt_id=attempt_id,
        state=state,
        row_version=version,
        claim_generation=1,
        credits=state_credits[state],
    )


class _Backend:
    def __init__(
        self,
        *,
        recoverable: tuple[CoordinatorWork, ...] = (),
        new: tuple[CoordinatorWork, ...] = (),
    ) -> None:
        self.recoverable = list(recoverable)
        self.new = list(new)
        self.calls: list[str] = []
        self.remote_entered = threading.Event()
        self.remote_release = threading.Event()
        self.block_remote = False
        self.retry_remote_once = False
        self.remote_calls = 0
        self.fail_remote = False
        self.defer_claim = False
        self.violate_admission_credit = False

    def list_recoverable(
        self, *, after_attempt_id: str | None, limit: int
    ) -> tuple[CoordinatorWork, ...]:
        self.calls.append(f"list:{after_attempt_id}")
        rows = [
            work
            for work in self.recoverable
            if after_attempt_id is None or work.attempt_id > after_attempt_id
        ]
        return tuple(rows[:limit])

    def claim_recovery(self, work: CoordinatorWork) -> CoordinatorWork:
        self.calls.append(f"claim:{work.attempt_id}")
        if self.defer_claim:
            raise RecoveryDeferred("held", retry_after_seconds=0.001)
        return replace(work, claim_generation=work.claim_generation + 1)

    def admit_new(
        self, *, limit: int, available_credits: CreditVector
    ) -> tuple[CoordinatorWork, ...]:
        self.calls.append(f"admit:{limit}")
        if self.violate_admission_credit and self.new:
            selected = (self.new.pop(0),)
            return selected
        selected_list: list[CoordinatorWork] = []
        aggregate = CreditVector()
        for work in self.new[:limit]:
            candidate = aggregate + work.credits
            if not candidate.fits(available_credits):
                break
            aggregate = candidate
            selected_list.append(work)
        selected = tuple(selected_list)
        del self.new[: len(selected)]
        return selected

    def prepare_remote_io(self, work: CoordinatorWork) -> CoordinatorWork:
        self.calls.append(f"preflight:{work.attempt_id}")
        return replace(
            _work(work.attempt_id, "reconciling", work.row_version + 1),
            claim_generation=work.claim_generation,
        )

    def run_remote(self, work: CoordinatorWork) -> CoordinatorWork:
        self.calls.append(f"remote:{work.attempt_id}:{work.state}")
        self.remote_calls += 1
        self.remote_entered.set()
        if self.block_remote:
            self.remote_release.wait(timeout=2)
        if self.retry_remote_once and self.remote_calls == 1:
            raise RetryStage("ambiguous", retry_after_seconds=0.001)
        if self.fail_remote:
            raise RuntimeError("boom")
        if work.state == "reconciling":
            target = _work(work.attempt_id, "submitted", work.row_version + 1)
        else:
            target = _work(work.attempt_id, "remote_terminal", work.row_version + 1)
        return replace(target, claim_generation=work.claim_generation)

    def prepare_local_io(self, work: CoordinatorWork) -> CoordinatorWork:
        self.calls.append(f"local_prepare:{work.attempt_id}")
        target = _work(work.attempt_id, "materializing", work.row_version + 1)
        return replace(
            target,
            claim_generation=work.claim_generation,
            credits=replace(
                target.credits,
                retained_bytes=max(
                    target.credits.retained_bytes, work.credits.retained_bytes
                ),
            ),
        )

    def run_local(self, work: CoordinatorWork) -> CoordinatorWork:
        self.calls.append(f"local:{work.attempt_id}")
        target = _work(work.attempt_id, "local_materialized", work.row_version + 1)
        return replace(
            target,
            claim_generation=work.claim_generation,
            credits=replace(
                target.credits,
                retained_bytes=max(
                    target.credits.retained_bytes, work.credits.retained_bytes
                ),
            ),
        )

    def commit(self, work: CoordinatorWork) -> CoordinatorWork:
        self.calls.append(f"commit:{work.attempt_id}")
        target = _work(work.attempt_id, "finish_committed", work.row_version + 1)
        return replace(
            target,
            claim_generation=work.claim_generation,
            credits=replace(
                target.credits,
                retained_bytes=max(
                    target.credits.retained_bytes, work.credits.retained_bytes
                ),
            ),
        )

    def acknowledge(self, work: CoordinatorWork) -> CoordinatorWork:
        self.calls.append(f"ack:{work.attempt_id}:{work.state}")
        state = {
            "finish_committed": "acked",
            "remote_failure_committed": "remote_failed",
            "local_failure_committed": "local_failed",
        }[work.state]
        return replace(
            _work(work.attempt_id, state, work.row_version + 1),
            claim_generation=work.claim_generation,
        )


def _limits(**changes: object) -> CoordinatorLimits:
    values: dict[str, object] = {
        "credits": _LIMIT,
        "poll_seconds": 0.001,
        "idle_open_circuit_seconds": 0.02,
    }
    values.update(changes)
    return CoordinatorLimits(**values)  # type: ignore[arg-type]


class CreditVectorTests(unittest.TestCase):
    def test_dimensions_are_non_fungible_and_never_negative(self) -> None:
        used = CreditVector(documents=1, decoded_bytes=10)
        self.assertFalse(used.fits(CreditVector(documents=2, decoded_bytes=9)))
        self.assertTrue(used.fits(CreditVector(documents=1, decoded_bytes=10)))
        with self.assertRaisesRegex(ValueError, "negative"):
            _ = used - CreditVector(documents=2)
        with self.assertRaisesRegex(ValueError, "non-negative"):
            CreditVector(local_items=-1)


class StagedParseCoordinatorTests(unittest.TestCase):
    def test_recovery_barrier_precedes_new_admission_and_full_lifecycle_acks(self) -> None:
        backend = _Backend(
            recoverable=(_work("attempt-0", "finish_committed", 7),),
            new=(_work("attempt-1", "prepared"),),
        )
        result = StagedParseCoordinator(
            backend=backend, limits=_limits()
        ).run()

        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(result.credits_in_use, CreditVector())
        self.assertEqual(
            result.final_states,
            (("attempt-0", "acked"), ("attempt-1", "acked")),
        )
        self.assertLess(
            backend.calls.index("claim:attempt-0"),
            next(i for i, call in enumerate(backend.calls) if call.startswith("admit:")),
        )
        self.assertNotIn("cancelled", " ".join(backend.calls))

    def test_remote_and_local_lanes_overlap_instead_of_serializing(self) -> None:
        backend = _Backend(
            recoverable=(
                _work("attempt-local", "remote_terminal", 3),
                _work("attempt-remote", "submitted", 2),
            )
        )
        backend.block_remote = True
        result_box: list[object] = []

        def run() -> None:
            result_box.append(
                StagedParseCoordinator(backend=backend, limits=_limits()).run(
                    stop_requested=lambda: False
                )
            )

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(backend.remote_entered.wait(timeout=1))
        deadline = time.monotonic() + 1
        while "local:attempt-local" not in backend.calls and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertIn("local:attempt-local", backend.calls)
        backend.remote_release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        result = result_box[0]
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)  # type: ignore[attr-defined]

    def test_transient_remote_retry_preserves_work_and_then_completes(self) -> None:
        backend = _Backend(new=(_work("attempt-1", "prepared"),))
        backend.retry_remote_once = True
        result = StagedParseCoordinator(backend=backend, limits=_limits()).run(
            stop_requested=lambda: False
        )
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(result.final_states, (("attempt-1", "acked"),))
        self.assertGreaterEqual(backend.remote_calls, 3)

    def test_unexpected_stage_failure_opens_circuit_and_retains_credit(self) -> None:
        backend = _Backend(new=(_work("attempt-1", "prepared"),))
        backend.fail_remote = True
        result = StagedParseCoordinator(backend=backend, limits=_limits()).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertTrue(result.errors)
        self.assertEqual(result.credits_in_use.documents, 1)
        self.assertEqual(result.completed, 0)

    def test_stop_closes_only_admission_and_durable_work_still_acks(self) -> None:
        backend = _Backend(new=(_work("attempt-1", "prepared"),))

        def stop() -> bool:
            return any(call.startswith("remote:attempt-1") for call in backend.calls)

        result = StagedParseCoordinator(backend=backend, limits=_limits()).run(
            stop_requested=stop
        )
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(result.final_states, (("attempt-1", "acked"),))
        self.assertEqual(result.credits_in_use, CreditVector())
        self.assertNotIn("cancelled", " ".join(backend.calls))

    def test_backend_admission_credit_violation_is_preserved_and_opens_circuit(self) -> None:
        too_large = replace(
            _work("attempt-1", "prepared"),
            credits=CreditVector(documents=9),
        )
        backend = _Backend(new=(too_large,))
        backend.violate_admission_credit = True
        result = StagedParseCoordinator(backend=backend, limits=_limits()).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertEqual(result.final_states, (("attempt-1", "acked"),))
        self.assertTrue(
            any("admission exceeded credit grant" in error for error in result.errors)
        )
        self.assertEqual(result.credits_in_use, CreditVector())

    def test_recovered_oversubscription_drains_before_new_admission(self) -> None:
        huge = replace(
            _work("attempt-old", "remote_terminal", 5),
            credits=CreditVector(
                documents=1, retained_results=1, retained_bytes=20_000
            ),
        )
        backend = _Backend(
            recoverable=(huge,),
            new=(_work("attempt-new", "prepared"),),
        )
        result = StagedParseCoordinator(backend=backend, limits=_limits()).run(
            stop_requested=lambda: False
        )
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        local_index = backend.calls.index("local:attempt-old")
        admit_indices = [
            i for i, call in enumerate(backend.calls) if call.startswith("admit:")
        ]
        self.assertTrue(admit_indices)
        self.assertLess(local_index, admit_indices[0])

    def test_stop_during_unclaimable_recovery_never_reports_quiescent(self) -> None:
        backend = _Backend(recoverable=(_work("attempt-0", "submitted"),))
        backend.defer_claim = True
        calls = 0

        def stop() -> bool:
            nonlocal calls
            calls += 1
            return calls >= 2

        result = StagedParseCoordinator(backend=backend, limits=_limits()).run(
            stop_requested=stop
        )
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertFalse(result.recovery_complete)


def result_ready(backend: _Backend) -> bool:
    return any(call.startswith("ack:") for call in backend.calls)


if __name__ == "__main__":
    unittest.main()
