from __future__ import annotations

from dataclasses import fields, replace
import threading
import time
import unittest

from disclosure_anchor.application.contracts.staged_resource_credit import (
    STAGED_RESOURCE_STATE_TRANSITIONS,
)
from disclosure_anchor.application.services import staged_parse_coordinator
from disclosure_anchor.application.services.staged_parse_coordinator import (
    AdmissionOutcome,
    CoordinatorLimits,
    CoordinatorResult,
    CoordinatorSnapshot,
    CoordinatorTerminal,
    CoordinatorWork,
    ResourceCreditVector,
    RecoveryDeferred,
    RetryStage,
    StageLeaseGuard,
    StageLeaseLost,
    StageWaiting,
    StagedParseCoordinator,
)


_LIMIT = ResourceCreditVector(
    documents=8,
    snapshot_items=8,
    snapshot_bytes=800,
    remote_waits=4,
    provider_tasks=8,
    provider_result_bytes=10_000,
    materialization_items=8,
    compressed_bytes=10_000,
    decoded_bytes=20_000,
    temp_disk_bytes=30_000,
    output_items=4,
    output_bytes=20_000,
    ack_items=4,
    output_pages=1_000,
)
_LIFECYCLE_RESERVATION = ResourceCreditVector(
    documents=1,
    snapshot_items=1,
    snapshot_bytes=100,
    remote_waits=1,
    provider_tasks=1,
    provider_result_bytes=100,
    materialization_items=1,
    compressed_bytes=100,
    decoded_bytes=400,
    temp_disk_bytes=500,
    output_items=1,
    output_bytes=400,
    ack_items=1,
    output_pages=10,
)


def _remote_terminal_credits(
    provider_result_bytes: int = 100,
) -> ResourceCreditVector:
    return ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=100,
        provider_tasks=1,
        provider_result_bytes=provider_result_bytes,
        ack_items=1,
    )


def _work(attempt_id: str, state: str, version: int = 0) -> CoordinatorWork:
    final = state in {
        "acked",
        "remote_failed",
        "local_failed",
        "pre_submission_failed",
        "preparation_failed",
        "superseded",
    }
    actual = {
        "prepared": ResourceCreditVector(
            documents=1, snapshot_items=1, snapshot_bytes=100
        ),
        "reconciling": ResourceCreditVector(
            documents=1,
            snapshot_items=1,
            snapshot_bytes=100,
            remote_waits=1,
        ),
        "submitted": ResourceCreditVector(
            documents=1,
            snapshot_items=1,
            snapshot_bytes=100,
            remote_waits=1,
            provider_tasks=1,
            ack_items=1,
        ),
        "remote_terminal": _remote_terminal_credits(),
        "materializing": ResourceCreditVector(
            documents=1,
            snapshot_items=1,
            snapshot_bytes=100,
            provider_tasks=1,
            provider_result_bytes=100,
            materialization_items=1,
            compressed_bytes=100,
            decoded_bytes=400,
            temp_disk_bytes=500,
            ack_items=1,
        ),
        "local_materialized": ResourceCreditVector(
            documents=1,
            snapshot_items=1,
            snapshot_bytes=100,
            provider_tasks=1,
            provider_result_bytes=100,
            compressed_bytes=100,
            output_items=1,
            output_bytes=400,
            output_pages=10,
            ack_items=1,
        ),
        "publish_committed": ResourceCreditVector(
            documents=1,
            snapshot_items=1,
            snapshot_bytes=100,
            provider_tasks=1,
            provider_result_bytes=100,
            compressed_bytes=100,
            output_items=1,
            output_bytes=400,
            output_pages=10,
            ack_items=1,
        ),
        "cleanup_pending": ResourceCreditVector(
            documents=1,
            snapshot_items=1,
            snapshot_bytes=100,
            provider_tasks=1,
            provider_result_bytes=100,
            compressed_bytes=100,
            output_items=1,
            output_bytes=400,
            output_pages=10,
            ack_items=1,
        ),
        "ack_pending": ResourceCreditVector(
            documents=1,
            provider_tasks=1,
            provider_result_bytes=100,
            ack_items=1,
        ),
    }.get(state, ResourceCreditVector())
    return CoordinatorWork(
        attempt_id=attempt_id,
        state=state,
        lifecycle_version=version,
        claim_generation=1,
        claim_owner_identity=None if final else "worker-boot-1",
        lease_expires_monotonic=None if final else time.monotonic() + 60,
        credit_reservation=ResourceCreditVector() if final else _LIFECYCLE_RESERVATION,
        credits=actual,
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
        self.remote_calls_by_attempt: dict[str, int] = {}
        self.fail_remote = False
        self.defer_claim = False
        self.defer_claim_ids: set[str] = set()
        self.foreign_deferred_projection = False
        self.deferred_projection_overrides: dict[str, CoordinatorWork] = {}
        self.deferred_projection_sequences: dict[str, list[CoordinatorWork]] = {}
        self.deferred_retry_after_seconds = 0.001
        self.duplicate_recovery_page = False
        self.claim_attempts: dict[str, int] = {}
        self.violate_admission_credit = False
        self.retry_remote_remaining = 0
        self.wait_remote_remaining = 0
        self.wait_remote_by_attempt: dict[str, int] = {}
        self.transition_violation: str | None = None
        self.renew_calls = 0
        self.fail_renew = False
        self.fail_renew_after: int | None = None
        self.renew_lease_seconds_override: float | None = None
        self.reload_result: CoordinatorWork | None = None
        self.fail_local_prepare = False
        self.claim_lease_seconds_override: float | None = None
        self.block_local_prepare = False
        self.local_prepare_entered = 0
        self.local_prepare_release = threading.Event()
        self.block_local = False
        self.local_entered = 0
        self.local_release = threading.Event()
        self.retry_local_once = False
        self.local_calls = 0
        self.fail_local_attempts: set[str] = set()
        self.outcome_by_attempt: dict[str, str] = {}

    @staticmethod
    def _assert_credit_grant(
        before: CoordinatorWork,
        after: CoordinatorWork,
        allowance: ResourceCreditVector,
    ) -> None:
        positive = ResourceCreditVector(
            **{
                item.name: max(
                    0,
                    getattr(after.credits, item.name)
                    - getattr(before.credits, item.name),
                )
                for item in fields(ResourceCreditVector)
            }
        )
        if not positive.fits(allowance):
            raise RetryStage("stage credit grant exhausted", retry_after_seconds=0.001)

    def list_recoverable(
        self, *, after_attempt_id: str | None, limit: int
    ) -> tuple[CoordinatorWork, ...]:
        self.calls.append(f"list:{after_attempt_id}")
        rows = [
            work
            for work in self.recoverable
            if after_attempt_id is None or work.attempt_id > after_attempt_id
        ]
        page = tuple(rows[:limit])
        if self.duplicate_recovery_page and page:
            return (page[0], page[0])
        return page

    def claim_recovery(self, work: CoordinatorWork) -> CoordinatorWork:
        self.calls.append(f"claim:{work.attempt_id}")
        self.claim_attempts[work.attempt_id] = (
            self.claim_attempts.get(work.attempt_id, 0) + 1
        )
        if self.defer_claim or work.attempt_id in self.defer_claim_ids:
            projections = self.deferred_projection_sequences.get(work.attempt_id)
            durable = (
                replace(work, attempt_id="foreign-attempt")
                if self.foreign_deferred_projection
                else (
                    projections.pop(0)
                    if projections
                    else self.deferred_projection_overrides.get(work.attempt_id, work)
                )
            )
            raise RecoveryDeferred(
                "held",
                retry_after_seconds=self.deferred_retry_after_seconds,
                durable_work=durable,
            )
        return replace(
            work,
            claim_generation=work.claim_generation + 1,
            claim_owner_identity="worker-boot-1",
            lease_expires_monotonic=time.monotonic()
            + (
                self.claim_lease_seconds_override
                if self.claim_lease_seconds_override is not None
                else 60
            ),
        )

    def renew_claim(
        self, work: CoordinatorWork, *, lease_seconds: int
    ) -> CoordinatorWork:
        self.calls.append(f"renew:{work.attempt_id}")
        self.renew_calls += 1
        if self.fail_renew or (
            self.fail_renew_after is not None
            and self.renew_calls >= self.fail_renew_after
        ):
            raise RuntimeError("claim lost")
        return replace(
            work,
            lease_expires_monotonic=time.monotonic()
            + (
                self.renew_lease_seconds_override
                if self.renew_lease_seconds_override is not None
                else lease_seconds
            ),
        )

    def reload_claim(self, work: CoordinatorWork) -> CoordinatorWork:
        self.calls.append(f"reload:{work.attempt_id}")
        return self.reload_result or work

    def admit_new(
        self, *, limit: int, available_credits: ResourceCreditVector
    ) -> AdmissionOutcome:
        self.calls.append(f"admit:{limit}")
        if self.violate_admission_credit and self.new:
            work = self.new.pop(0)
            return AdmissionOutcome(
                work=(work,),
                backlog_exists=bool(self.new),
            )
        selected_list: list[CoordinatorWork] = []
        aggregate = ResourceCreditVector()
        for work in self.new[:limit]:
            candidate = aggregate + work.credits
            if not candidate.fits(available_credits):
                break
            aggregate = candidate
            selected_list.append(work)
        selected = tuple(selected_list)
        del self.new[: len(selected)]
        remaining = available_credits - aggregate
        blocked_dimensions: tuple[str, ...] = ()
        if self.new and len(selected) < limit:
            blocked_dimensions = tuple(
                item.name
                for item in fields(ResourceCreditVector)
                if getattr(self.new[0].credits, item.name)
                > getattr(remaining, item.name)
            )
        return AdmissionOutcome(
            work=selected,
            backlog_exists=bool(self.new),
            blocked_dimensions=blocked_dimensions,
        )

    def prepare_remote_io(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        stage_guard.checkpoint()
        self.calls.append(f"preflight:{work.attempt_id}")
        updated = replace(
            _work(work.attempt_id, "reconciling", work.lifecycle_version + 1),
            claim_generation=work.claim_generation,
        )
        self._assert_credit_grant(work, updated, credit_allowance)
        if self.transition_violation == "equal_version":
            return replace(updated, lifecycle_version=work.lifecycle_version)
        if self.transition_violation == "jump_version":
            return replace(updated, lifecycle_version=work.lifecycle_version + 2)
        if self.transition_violation == "claim_generation":
            return replace(updated, claim_generation=work.claim_generation + 1)
        if self.transition_violation == "state_jump":
            return replace(updated, state="remote_terminal")
        return updated

    def run_remote(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        stage_guard.checkpoint()
        self.calls.append(f"remote:{work.attempt_id}:{work.state}")
        self.remote_calls += 1
        self.remote_calls_by_attempt[work.attempt_id] = (
            self.remote_calls_by_attempt.get(work.attempt_id, 0) + 1
        )
        self.remote_entered.set()
        if self.block_remote:
            while not self.remote_release.wait(timeout=0.001):
                stage_guard.checkpoint()
        stage_guard.checkpoint()
        if self.retry_remote_once and self.remote_calls == 1:
            raise RetryStage("ambiguous", retry_after_seconds=0.001)
        if self.retry_remote_remaining > 0:
            self.retry_remote_remaining -= 1
            raise RetryStage("persistent", retry_after_seconds=0.001)
        if work.state == "submitted" and self.wait_remote_remaining > 0:
            self.wait_remote_remaining -= 1
            raise StageWaiting("provider running", retry_after_seconds=0.001)
        waiting = self.wait_remote_by_attempt.get(work.attempt_id, 0)
        if work.state == "submitted" and waiting > 0:
            self.wait_remote_by_attempt[work.attempt_id] = waiting - 1
            raise StageWaiting("provider running", retry_after_seconds=0.001)
        if self.fail_remote:
            raise RuntimeError("boom")
        if work.state == "reconciling":
            target = _work(work.attempt_id, "submitted", work.lifecycle_version + 1)
        else:
            target = _work(
                work.attempt_id, "remote_terminal", work.lifecycle_version + 1
            )
        updated = replace(target, claim_generation=work.claim_generation)
        self._assert_credit_grant(work, updated, credit_allowance)
        return updated

    def prepare_local_io(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        self.calls.append(f"local_prepare:{work.attempt_id}")
        stage_guard.checkpoint()
        self.local_prepare_entered += 1
        if self.block_local_prepare:
            self.local_prepare_release.wait(timeout=2)
        if self.fail_local_prepare:
            self.outcome_by_attempt[work.attempt_id] = "local_failure"
            target = _work(
                work.attempt_id,
                "cleanup_pending",
                work.lifecycle_version + 1,
            )
            return replace(
                target,
                claim_generation=work.claim_generation,
                claim_owner_identity=work.claim_owner_identity,
                lease_expires_monotonic=work.lease_expires_monotonic,
                credit_reservation=work.credit_reservation,
                credits=_remote_terminal_credits(),
            )
        target = _work(work.attempt_id, "materializing", work.lifecycle_version + 1)
        updated = replace(
            target,
            claim_generation=work.claim_generation,
            claim_owner_identity=work.claim_owner_identity,
            lease_expires_monotonic=work.lease_expires_monotonic,
            credit_reservation=work.credit_reservation,
        )
        self._assert_credit_grant(work, updated, credit_allowance)
        return updated

    def run_local(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        stage_guard.checkpoint()
        self.calls.append(f"local:{work.attempt_id}")
        self.local_calls += 1
        self.local_entered += 1
        if self.block_local:
            while not self.local_release.wait(timeout=0.001):
                stage_guard.checkpoint()
        if self.retry_local_once and self.local_calls == 1:
            raise RetryStage("local transient", retry_after_seconds=0.005)
        if work.attempt_id in self.fail_local_attempts:
            self.outcome_by_attempt[work.attempt_id] = "local_failure"
            updated = replace(
                _work(
                    work.attempt_id,
                    "cleanup_pending",
                    work.lifecycle_version + 1,
                ),
                claim_generation=work.claim_generation,
                claim_owner_identity=work.claim_owner_identity,
                lease_expires_monotonic=work.lease_expires_monotonic,
                credit_reservation=work.credit_reservation,
                credits=ResourceCreditVector(
                    documents=1,
                    snapshot_items=1,
                    snapshot_bytes=100,
                    provider_tasks=1,
                    provider_result_bytes=100,
                    materialization_items=1,
                    compressed_bytes=100,
                    decoded_bytes=400,
                    temp_disk_bytes=500,
                    ack_items=1,
                ),
            )
            self._assert_credit_grant(work, updated, credit_allowance)
            return updated
        target = _work(
            work.attempt_id, "local_materialized", work.lifecycle_version + 1
        )
        updated = replace(
            target,
            claim_generation=work.claim_generation,
            claim_owner_identity=work.claim_owner_identity,
            lease_expires_monotonic=work.lease_expires_monotonic,
            credit_reservation=work.credit_reservation,
        )
        self._assert_credit_grant(work, updated, credit_allowance)
        return updated

    def commit(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        stage_guard.checkpoint()
        self.calls.append(f"commit:{work.attempt_id}")
        target = _work(
            work.attempt_id,
            "publish_committed",
            work.lifecycle_version + 1,
        )
        updated = replace(
            target,
            claim_generation=work.claim_generation,
            claim_owner_identity=work.claim_owner_identity,
            lease_expires_monotonic=work.lease_expires_monotonic,
            credit_reservation=work.credit_reservation,
        )
        self._assert_credit_grant(work, updated, credit_allowance)
        return updated

    def cleanup(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        stage_guard.checkpoint()
        self.calls.append(f"cleanup:{work.attempt_id}:{work.state}")
        if work.state == "publish_committed":
            self.outcome_by_attempt[work.attempt_id] = "success"
            target_state = "cleanup_pending"
        else:
            target_state = "ack_pending"
        target = _work(
            work.attempt_id,
            target_state,
            work.lifecycle_version + 1,
        )
        updated = replace(
            target,
            claim_generation=work.claim_generation,
            claim_owner_identity=work.claim_owner_identity,
            lease_expires_monotonic=work.lease_expires_monotonic,
            credit_reservation=work.credit_reservation,
        )
        self._assert_credit_grant(work, updated, credit_allowance)
        return updated

    def acknowledge(
        self, work: CoordinatorWork, *, stage_guard: StageLeaseGuard
    ) -> CoordinatorWork:
        stage_guard.checkpoint()
        self.calls.append(f"ack:{work.attempt_id}:{work.state}")
        state = {
            "success": "acked",
            "remote_failure": "remote_failed",
            "local_failure": "local_failed",
            "superseded": "superseded",
        }[self.outcome_by_attempt.get(work.attempt_id, "success")]
        return replace(
            _work(work.attempt_id, state, work.lifecycle_version + 1),
            claim_generation=work.claim_generation,
        )


def _limits(**changes: object) -> CoordinatorLimits:
    values: dict[str, object] = {
        "credits": _LIMIT,
        "remote_workers": _LIMIT.remote_waits,
        "poll_seconds": 0.001,
        "idle_open_circuit_seconds": 0.02,
    }
    values.update(changes)
    return CoordinatorLimits(**values)  # type: ignore[arg-type]


class CreditVectorTests(unittest.TestCase):
    def test_dimensions_are_non_fungible_and_never_negative(self) -> None:
        used = ResourceCreditVector(documents=1, decoded_bytes=10)
        self.assertFalse(used.fits(ResourceCreditVector(documents=2, decoded_bytes=9)))
        self.assertTrue(used.fits(ResourceCreditVector(documents=1, decoded_bytes=10)))
        with self.assertRaisesRegex(ValueError, "negative"):
            _ = used - ResourceCreditVector(documents=2)
        with self.assertRaisesRegex(ValueError, "non-negative"):
            ResourceCreditVector(materialization_items=-1)

    def test_work_shape_closes_nonfinal_and_final_credit_ownership(self) -> None:
        nonfinal = _work("attempt-1", "submitted")
        with self.assertRaisesRegex(ValueError, "nonfinal coordinator work"):
            replace(
                nonfinal,
                claim_owner_identity=None,
                lease_expires_monotonic=None,
            )
        with self.assertRaisesRegex(ValueError, "nonfinal coordinator work"):
            replace(
                nonfinal,
                credit_reservation=ResourceCreditVector(),
                credits=ResourceCreditVector(),
            )

        final = _work("attempt-1", "acked")
        with self.assertRaisesRegex(ValueError, "final coordinator work"):
            replace(
                final,
                claim_owner_identity="worker-boot-1",
                lease_expires_monotonic=time.monotonic() + 60,
            )
        with self.assertRaisesRegex(ValueError, "final coordinator work"):
            replace(
                final,
                credit_reservation=_LIFECYCLE_RESERVATION,
                credits=ResourceCreditVector(documents=1),
            )

    def test_unknown_state_remains_constructible_for_fail_closed_reporting(
        self,
    ) -> None:
        unknown = CoordinatorWork(
            attempt_id="attempt-unknown",
            state="future_state",
            lifecycle_version=1,
            claim_generation=0,
            claim_owner_identity=None,
            lease_expires_monotonic=None,
            credit_reservation=ResourceCreditVector(),
            credits=ResourceCreditVector(),
        )
        self.assertEqual(unknown.state, "future_state")

    def test_nonfinite_and_non_numeric_work_leases_fail_closed(self) -> None:
        work = _work("attempt-1", "submitted")
        for invalid in (float("nan"), float("inf"), float("-inf"), True, "1"):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ValueError, "claim projection"),
            ):
                replace(work, lease_expires_monotonic=invalid)  # type: ignore[arg-type]

    def test_nonfinite_and_non_numeric_retry_delays_fail_closed(self) -> None:
        work = _work("attempt-1", "submitted")
        for invalid in (float("nan"), float("inf"), float("-inf"), True, "1"):
            with (
                self.subTest(kind="recovery", invalid=invalid),
                self.assertRaisesRegex(ValueError, "retry delay"),
            ):
                RecoveryDeferred(
                    "held",
                    retry_after_seconds=invalid,  # type: ignore[arg-type]
                    durable_work=work,
                )
            with (
                self.subTest(kind="stage", invalid=invalid),
                self.assertRaisesRegex(ValueError, "retry delay"),
            ):
                RetryStage("retry", retry_after_seconds=invalid)  # type: ignore[arg-type]
            with (
                self.subTest(kind="wait", invalid=invalid),
                self.assertRaisesRegex(ValueError, "wait delay"),
            ):
                StageWaiting("waiting", retry_after_seconds=invalid)  # type: ignore[arg-type]

    def test_nonfinite_and_non_numeric_timing_limits_fail_closed(self) -> None:
        fields_to_check = (
            "poll_seconds",
            "idle_open_circuit_seconds",
            "claim_renew_margin_seconds",
            "max_stage_step_seconds",
            "retry_initial_backoff_seconds",
            "retry_max_backoff_seconds",
            "retry_stuck_seconds",
        )
        for field_name in fields_to_check:
            for invalid in (float("nan"), float("inf"), float("-inf"), True, "1"):
                with (
                    self.subTest(field=field_name, invalid=invalid),
                    self.assertRaisesRegex(ValueError, "finite and positive"),
                ):
                    _limits(**{field_name: invalid})

    def test_remote_poll_workers_are_independent_of_resident_wait_credit(self) -> None:
        limits = _limits(remote_workers=1)
        self.assertEqual(limits.remote_workers, 1)
        self.assertGreater(limits.credits.remote_waits, limits.remote_workers)


class StagedParseCoordinatorTests(unittest.TestCase):
    def test_every_v4_nonfinal_state_has_exactly_one_lane(self) -> None:
        self.assertEqual(
            set(staged_parse_coordinator._LANE_BY_STATE),
            set(STAGED_RESOURCE_STATE_TRANSITIONS),
        )

    def test_stage_guard_rejects_nonfinite_deadline_and_clock(self) -> None:
        for invalid in (float("nan"), float("inf"), float("-inf"), True, "1"):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ValueError, "deadline"),
            ):
                StageLeaseGuard(
                    deadline_monotonic=invalid,  # type: ignore[arg-type]
                    _revoked=threading.Event(),
                    _monotonic=time.monotonic,
                )
        for invalid in (float("nan"), float("inf"), float("-inf"), True, "1"):
            guard = StageLeaseGuard(
                deadline_monotonic=time.monotonic() + 60,
                _revoked=threading.Event(),
                _monotonic=lambda invalid=invalid: invalid,  # type: ignore[misc]
            )
            with self.subTest(clock=invalid), self.assertRaises(StageLeaseLost):
                guard.checkpoint()

    def test_stage_guard_uses_the_coordinator_clock_domain(self) -> None:
        clock = [9.0]
        guard = StageLeaseGuard(
            deadline_monotonic=10.0,
            _revoked=threading.Event(),
            _monotonic=lambda: clock[0],
        )
        guard.checkpoint()
        clock[0] = 11.0
        with self.assertRaises(StageLeaseLost):
            guard.checkpoint()

    def test_local_prepare_deterministic_failure_drains_directly_through_ack(
        self,
    ) -> None:
        backend = _Backend(recoverable=(_work("attempt-1", "remote_terminal", 4),))
        backend.fail_local_prepare = True
        result = StagedParseCoordinator(backend=backend, limits=_limits()).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(result.final_states, (("attempt-1", "local_failed"),))
        self.assertIn("cleanup:attempt-1:cleanup_pending", backend.calls)
        self.assertIn("ack:attempt-1:ack_pending", backend.calls)

    def test_post_materialization_failure_cleans_before_next_local_prepare(
        self,
    ) -> None:
        backend = _Backend(
            recoverable=(
                _work("attempt-1", "remote_terminal", 4),
                _work("attempt-2", "remote_terminal", 4),
            )
        )
        backend.fail_local_attempts.add("attempt-1")
        result = StagedParseCoordinator(
            backend=backend,
            limits=_limits(
                credits=replace(
                    _LIMIT,
                    materialization_items=1,
                    compressed_bytes=100,
                    decoded_bytes=400,
                    temp_disk_bytes=500,
                )
            ),
        ).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        cleanup_index = backend.calls.index("cleanup:attempt-1:cleanup_pending")
        second_prepare_index = backend.calls.index("local_prepare:attempt-2")
        self.assertLess(cleanup_index, second_prepare_index)

    def test_recovery_barrier_precedes_new_admission_and_full_lifecycle_acks(
        self,
    ) -> None:
        backend = _Backend(
            recoverable=(_work("attempt-0", "ack_pending", 8),),
            new=(_work("attempt-1", "prepared"),),
        )
        result = StagedParseCoordinator(backend=backend, limits=_limits()).run()

        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(result.credits_in_use, ResourceCreditVector())
        self.assertEqual(
            result.final_states,
            (("attempt-0", "acked"), ("attempt-1", "acked")),
        )
        self.assertLess(
            backend.calls.index("claim:attempt-0"),
            next(
                i for i, call in enumerate(backend.calls) if call.startswith("admit:")
            ),
        )
        self.assertNotIn("cancelled", " ".join(backend.calls))

    def test_cleanup_is_durable_and_precedes_ack(self) -> None:
        backend = _Backend(new=(_work("attempt-1", "prepared"),))
        result = StagedParseCoordinator(backend=backend, limits=_limits()).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        planned = backend.calls.index("cleanup:attempt-1:publish_committed")
        cleaned = backend.calls.index("cleanup:attempt-1:cleanup_pending")
        acknowledged = backend.calls.index("ack:attempt-1:ack_pending")
        self.assertLess(planned, cleaned)
        self.assertLess(cleaned, acknowledged)

    def test_cleanup_lane_prioritizes_existing_cleanup_over_new_plan(self) -> None:
        backend = _Backend(
            recoverable=(
                _work("attempt-a-plan", "publish_committed", 7),
                _work("attempt-b-clean", "cleanup_pending", 8),
            )
        )
        result = StagedParseCoordinator(
            backend=backend,
            limits=_limits(cleanup_workers=1),
        ).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        cleanup_calls = [call for call in backend.calls if call.startswith("cleanup:")]
        self.assertEqual(
            cleanup_calls[0],
            "cleanup:attempt-b-clean:cleanup_pending",
        )

    def test_healthy_long_remote_wait_does_not_consume_retry_budget(self) -> None:
        backend = _Backend(
            recoverable=(_work("attempt-wait", "submitted", 2),),
            new=(_work("attempt-new", "prepared"),),
        )
        backend.wait_remote_by_attempt["attempt-wait"] = 100
        snapshots: list[CoordinatorSnapshot] = []
        result = StagedParseCoordinator(
            backend=backend,
            limits=_limits(
                retry_max_attempts=8,
                retry_stuck_seconds=0.05,
            ),
            progress=snapshots.append,
        ).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(result.errors, ())
        self.assertEqual(
            result.final_states,
            (("attempt-new", "acked"), ("attempt-wait", "acked")),
        )
        third_wait_poll = [
            index
            for index, call in enumerate(backend.calls)
            if call == "remote:attempt-wait:submitted"
        ][2]
        self.assertLess(
            backend.calls.index("remote:attempt-new:reconciling"), third_wait_poll
        )
        self.assertFalse(
            any(snapshot.blocked_reason == "retry_degraded" for snapshot in snapshots)
        )

    def test_blocked_remote_poll_does_not_starve_independent_submit_growth(
        self,
    ) -> None:
        backend = _Backend(
            recoverable=(
                _work("attempt-a-blocked", "submitted", 2),
                _work("attempt-b-feed", "reconciling", 1),
                _work("attempt-z-holder", "remote_terminal", 3),
            )
        )
        backend.block_local_prepare = True
        snapshots: list[CoordinatorSnapshot] = []
        result_box: list[CoordinatorResult] = []
        thread = threading.Thread(
            target=lambda: result_box.append(
                StagedParseCoordinator(
                    backend=backend,
                    limits=_limits(
                        credits=replace(_LIMIT, provider_result_bytes=100),
                    ),
                    progress=snapshots.append,
                ).run()
            )
        )
        thread.start()
        deadline = time.monotonic() + 1
        while (
            "remote:attempt-b-feed:reconciling" not in backend.calls
            or not any(
                "provider_result_bytes"
                in dict(snapshot.credit_blocked_by_lane)["remote"]
                for snapshot in snapshots
            )
        ) and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertIn("remote:attempt-b-feed:reconciling", backend.calls)
        self.assertTrue(
            any(
                "provider_result_bytes"
                in dict(snapshot.credit_blocked_by_lane)["remote"]
                for snapshot in snapshots
            )
        )
        backend.local_prepare_release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_box[0].terminal, CoordinatorTerminal.QUIESCENT)

    def test_document_credit_backpressure_is_visible_while_admission_is_open(
        self,
    ) -> None:
        backend = _Backend(
            new=(
                _work("attempt-1", "prepared"),
                _work("attempt-2", "prepared"),
            )
        )
        backend.block_remote = True
        snapshots: list[CoordinatorSnapshot] = []
        result_box: list[CoordinatorResult] = []
        thread = threading.Thread(
            target=lambda: result_box.append(
                StagedParseCoordinator(
                    backend=backend,
                    limits=_limits(
                        credits=replace(
                            _LIMIT,
                            documents=1,
                            snapshot_items=1,
                            snapshot_bytes=100,
                        )
                    ),
                    progress=snapshots.append,
                ).run()
            )
        )
        thread.start()
        self.assertTrue(backend.remote_entered.wait(timeout=1))
        deadline = time.monotonic() + 1
        while (
            not any(
                snapshot.admission_open
                and snapshot.blocked_reason is not None
                and snapshot.blocked_reason.startswith("credit_backpressure:")
                for snapshot in snapshots
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        self.assertTrue(
            any(
                snapshot.admission_open
                and snapshot.blocked_reason is not None
                and "documents" in snapshot.blocked_reason
                for snapshot in snapshots
            )
        )
        backend.remote_release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_box[0].terminal, CoordinatorTerminal.QUIESCENT)

    def test_non_document_admission_backpressure_is_exact_and_not_busy_polled(
        self,
    ) -> None:
        backend = _Backend(
            recoverable=(_work("attempt-running", "submitted", 2),),
            new=(_work("attempt-new", "prepared"),),
        )
        backend.block_remote = True
        snapshots: list[CoordinatorSnapshot] = []
        result_box: list[CoordinatorResult] = []
        thread = threading.Thread(
            target=lambda: result_box.append(
                StagedParseCoordinator(
                    backend=backend,
                    limits=_limits(
                        credits=replace(
                            _LIMIT,
                            documents=2,
                            snapshot_items=1,
                            snapshot_bytes=100,
                        )
                    ),
                    progress=snapshots.append,
                ).run()
            )
        )
        thread.start()
        self.assertTrue(backend.remote_entered.wait(timeout=1))
        deadline = time.monotonic() + 1
        expected = "credit_backpressure:snapshot_items,snapshot_bytes"
        while (
            not any(snapshot.blocked_reason == expected for snapshot in snapshots)
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        self.assertTrue(
            any(snapshot.blocked_reason == expected for snapshot in snapshots)
        )
        admission_calls = sum(call.startswith("admit:") for call in backend.calls)
        time.sleep(0.02)
        self.assertEqual(
            sum(call.startswith("admit:") for call in backend.calls),
            admission_calls,
        )
        backend.remote_release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_box[0].terminal, CoordinatorTerminal.QUIESCENT)

    def test_admission_count_is_capped_by_available_document_credit(self) -> None:
        backend = _Backend(
            new=tuple(_work(f"attempt-{index}", "prepared") for index in range(4))
        )
        result = StagedParseCoordinator(
            backend=backend,
            limits=_limits(
                credits=replace(
                    _LIMIT,
                    documents=2,
                    snapshot_items=2,
                    snapshot_bytes=200,
                )
            ),
        ).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        grants = [
            int(call.split(":", 1)[1])
            for call in backend.calls
            if call.startswith("admit:")
        ]
        self.assertTrue(grants)
        self.assertLessEqual(max(grants), 2)

    def test_remote_and_local_lanes_overlap_instead_of_serializing(self) -> None:
        backend = _Backend(
            recoverable=(
                _work("attempt-local", "remote_terminal", 3),
                _work("attempt-remote", "submitted", 2),
            )
        )
        backend.block_remote = True
        result_box: list[CoordinatorResult] = []

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
        while (
            "local:attempt-local" not in backend.calls and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        self.assertIn("local:attempt-local", backend.calls)
        backend.remote_release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        result = result_box[0]
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)

    def test_remote_capacity_is_not_throttled_by_future_local_reservations(
        self,
    ) -> None:
        backend = _Backend(
            new=tuple(_work(f"attempt-{index}", "prepared") for index in range(4))
        )
        backend.block_remote = True
        result_box: list[CoordinatorResult] = []
        limits = _limits(
            credits=replace(_LIMIT, remote_waits=8, materialization_items=1),
            preflight_workers=4,
            remote_workers=8,
            local_prepare_workers=1,
            local_workers=1,
        )
        thread = threading.Thread(
            target=lambda: result_box.append(
                StagedParseCoordinator(backend=backend, limits=limits).run()
            )
        )
        thread.start()
        deadline = time.monotonic() + 1
        while (
            sum(call.startswith("remote:") for call in backend.calls) < 4
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        self.assertGreaterEqual(
            sum(call.startswith("remote:") for call in backend.calls), 4
        )
        backend.remote_release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_box[0].terminal, CoordinatorTerminal.QUIESCENT)

    def test_parallel_local_prepare_holds_cannot_oversubscribe_global_credit(
        self,
    ) -> None:
        backend = _Backend(
            recoverable=(
                _work("attempt-1", "remote_terminal", 4),
                _work("attempt-2", "remote_terminal", 4),
            )
        )
        backend.block_local_prepare = True
        result_box: list[CoordinatorResult] = []
        limits = _limits(
            credits=replace(_LIMIT, materialization_items=1),
            local_prepare_workers=2,
        )
        thread = threading.Thread(
            target=lambda: result_box.append(
                StagedParseCoordinator(backend=backend, limits=limits).run()
            )
        )
        thread.start()
        deadline = time.monotonic() + 1
        while backend.local_prepare_entered < 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(backend.local_prepare_entered, 1)
        time.sleep(0.02)
        self.assertEqual(backend.local_prepare_entered, 1)
        backend.local_prepare_release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(backend.local_prepare_entered, 2)
        self.assertEqual(result_box[0].terminal, CoordinatorTerminal.QUIESCENT)

    def test_parallel_local_completion_holds_db_and_page_credit_before_cas(
        self,
    ) -> None:
        backend = _Backend(
            recoverable=(
                _work("attempt-1", "materializing", 5),
                _work("attempt-2", "materializing", 5),
            )
        )
        backend.block_local = True
        result_box: list[CoordinatorResult] = []
        limits = _limits(
            credits=replace(_LIMIT, output_items=1, output_pages=10),
            local_workers=2,
        )
        thread = threading.Thread(
            target=lambda: result_box.append(
                StagedParseCoordinator(backend=backend, limits=limits).run()
            )
        )
        thread.start()
        deadline = time.monotonic() + 1
        while backend.local_entered < 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(backend.local_entered, 1)
        time.sleep(0.02)
        self.assertEqual(backend.local_entered, 1)
        backend.local_release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(backend.local_entered, 2)
        self.assertEqual(result_box[0].terminal, CoordinatorTerminal.QUIESCENT)

    def test_queued_credit_waits_for_retrying_downstream_owner_to_release(self) -> None:
        backend = _Backend(
            recoverable=(
                _work("attempt-downstream", "materializing", 5),
                _work("attempt-upstream", "remote_terminal", 4),
            )
        )
        backend.retry_local_once = True
        result = StagedParseCoordinator(
            backend=backend,
            limits=_limits(
                credits=replace(_LIMIT, materialization_items=1),
                retry_initial_backoff_seconds=0.001,
                retry_max_backoff_seconds=0.005,
            ),
        ).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(result.errors, ())
        self.assertGreaterEqual(backend.local_calls, 3)
        self.assertIn("local_prepare:attempt-upstream", backend.calls)

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
        self.assertEqual(result.credits_in_use, ResourceCreditVector())
        self.assertNotIn("cancelled", " ".join(backend.calls))

    def test_backend_admission_credit_violation_is_preserved_and_opens_circuit(
        self,
    ) -> None:
        too_large = replace(
            _work("attempt-1", "prepared"),
            credit_reservation=replace(_LIFECYCLE_RESERVATION, documents=9),
            credits=ResourceCreditVector(documents=9),
        )
        backend = _Backend(new=(too_large,))
        backend.violate_admission_credit = True
        result = StagedParseCoordinator(backend=backend, limits=_limits()).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertEqual(result.final_states, ())
        self.assertTrue(
            any("admission exceeded credit grant" in error for error in result.errors)
        )
        self.assertEqual(result.credits_in_use.documents, 9)

    def test_backend_admission_must_return_prepared_lifecycle_zero(self) -> None:
        backend = _Backend(new=(_work("attempt-1", "submitted", 2),))
        result = StagedParseCoordinator(backend=backend, limits=_limits()).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertTrue(
            any("invalid initial state" in error for error in result.errors)
        )
        self.assertEqual(result.credits_in_use.documents, 1)

    def test_backend_admission_rejects_prepared_nonzero_lifecycle(self) -> None:
        backend = _Backend(new=(_work("attempt-1", "prepared", 1),))
        result = StagedParseCoordinator(backend=backend, limits=_limits()).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertTrue(
            any("invalid initial state" in error for error in result.errors)
        )
        self.assertEqual(result.credits_in_use.documents, 1)

    def test_recovered_oversubscription_drains_before_new_admission(self) -> None:
        huge = replace(
            _work("attempt-old", "remote_terminal", 5),
            credit_reservation=replace(
                _LIFECYCLE_RESERVATION, provider_result_bytes=20_000
            ),
            credits=_remote_terminal_credits(20_000),
        )
        backend = _Backend(
            recoverable=(huge,),
            new=(_work("attempt-new", "prepared"),),
        )
        result = StagedParseCoordinator(backend=backend, limits=_limits()).run(
            stop_requested=lambda: False
        )
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        local_prepare_index = backend.calls.index("local_prepare:attempt-old")
        admit_indices = [
            i for i, call in enumerate(backend.calls) if call.startswith("admit:")
        ]
        self.assertTrue(admit_indices)
        self.assertLess(local_prepare_index, admit_indices[0])

    def test_recovery_with_unreachable_lifecycle_envelope_uses_emergency_drain(
        self,
    ) -> None:
        current_fits = replace(
            _work("attempt-old", "remote_terminal", 5),
            credit_reservation=replace(_LIFECYCLE_RESERVATION, materialization_items=5),
        )
        backend = _Backend(
            recoverable=(current_fits,),
            new=(_work("attempt-new", "prepared"),),
        )
        result = StagedParseCoordinator(
            backend=backend,
            limits=_limits(credits=replace(_LIMIT, materialization_items=1)),
        ).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertIn("local_prepare:attempt-old", backend.calls)
        self.assertLess(
            backend.calls.index("local_prepare:attempt-old"),
            next(
                index
                for index, call in enumerate(backend.calls)
                if call.startswith("admit:")
            ),
        )

    def test_multiple_oversubscribed_recoveries_receive_one_growth_grant(self) -> None:
        recoverable = tuple(
            replace(
                _work(f"attempt-{index}", "remote_terminal", 5),
                credit_reservation=replace(
                    _LIFECYCLE_RESERVATION, provider_result_bytes=20_000
                ),
                credits=_remote_terminal_credits(20_000),
            )
            for index in range(3)
        )
        backend = _Backend(recoverable=recoverable)
        backend.block_local_prepare = True
        result_box: list[CoordinatorResult] = []
        thread = threading.Thread(
            target=lambda: result_box.append(
                StagedParseCoordinator(
                    backend=backend,
                    limits=_limits(local_prepare_workers=3),
                ).run()
            )
        )
        thread.start()
        deadline = time.monotonic() + 1
        while backend.local_prepare_entered < 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(backend.local_prepare_entered, 1)
        time.sleep(0.02)
        self.assertEqual(backend.local_prepare_entered, 1)
        backend.local_prepare_release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(backend.local_prepare_entered, 3)
        self.assertEqual(result_box[0].terminal, CoordinatorTerminal.QUIESCENT)

    def test_aggregate_only_recovery_overage_marks_every_releasing_owner(self) -> None:
        recoverable = tuple(
            replace(
                _work(f"attempt-{index}", "remote_terminal", 5),
                credit_reservation=replace(
                    _LIFECYCLE_RESERVATION, provider_result_bytes=6_000
                ),
                credits=_remote_terminal_credits(6_000),
            )
            for index in range(2)
        )
        backend = _Backend(recoverable=recoverable)
        result = StagedParseCoordinator(
            backend=backend,
            limits=_limits(credits=replace(_LIMIT, provider_result_bytes=10_000)),
        ).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(result.completed, 2)
        self.assertEqual(
            sum(call.startswith("local_prepare:") for call in backend.calls), 2
        )

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

    def test_transition_contract_rejects_noop_jump_and_claim_change(self) -> None:
        for violation in (
            "equal_version",
            "jump_version",
            "claim_generation",
            "state_jump",
        ):
            with self.subTest(violation=violation):
                backend = _Backend(new=(_work("attempt-1", "prepared"),))
                backend.transition_violation = violation
                result = StagedParseCoordinator(backend=backend, limits=_limits()).run()
                self.assertEqual(
                    result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT
                )
                self.assertEqual(result.completed, 0)
                self.assertEqual(result.credits_in_use.documents, 1)

    def test_deferred_recovery_does_not_block_claimed_work_drain(self) -> None:
        backend = _Backend(
            recoverable=(
                _work("attempt-active", "ack_pending", 8),
                _work("attempt-deferred", "submitted", 2),
            )
        )
        backend.defer_claim_ids.add("attempt-deferred")
        result_box: list[CoordinatorResult] = []
        thread = threading.Thread(
            target=lambda: result_box.append(
                StagedParseCoordinator(backend=backend, limits=_limits()).run()
            )
        )
        thread.start()
        deadline = time.monotonic() + 1
        while "ack:attempt-active:ack_pending" not in backend.calls:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.001)
        self.assertFalse(any(call.startswith("admit:") for call in backend.calls))
        backend.defer_claim_ids.clear()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_box[0].terminal, CoordinatorTerminal.QUIESCENT)

    def test_deferred_recovery_credits_block_conflicting_claimed_growth(self) -> None:
        backend = _Backend(
            recoverable=(
                _work("attempt-claimed", "remote_terminal", 4),
                _work("attempt-deferred", "materializing", 5),
            )
        )
        backend.defer_claim_ids.add("attempt-deferred")
        result_box: list[CoordinatorResult] = []
        thread = threading.Thread(
            target=lambda: result_box.append(
                StagedParseCoordinator(
                    backend=backend,
                    limits=_limits(credits=replace(_LIMIT, materialization_items=1)),
                ).run()
            )
        )
        thread.start()
        deadline = time.monotonic() + 1
        while (
            backend.claim_attempts.get("attempt-deferred", 0) < 2
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        self.assertEqual(backend.local_prepare_entered, 0)
        backend.defer_claim_ids.clear()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_box[0].terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(backend.local_prepare_entered, 1)

    def test_deferred_recovery_rejects_a_foreign_durable_projection(self) -> None:
        backend = _Backend(recoverable=(_work("attempt-1", "submitted", 2),))
        backend.defer_claim = True
        backend.foreign_deferred_projection = True
        with self.assertRaisesRegex(RuntimeError, "foreign or final"):
            StagedParseCoordinator(backend=backend, limits=_limits()).run()

    def test_deferred_row_causing_aggregate_overage_does_not_strand_releaser(
        self,
    ) -> None:
        rows = tuple(
            replace(
                _work(f"attempt-{suffix}", "remote_terminal", 4),
                credit_reservation=replace(
                    _LIFECYCLE_RESERVATION, provider_result_bytes=6_000
                ),
                credits=_remote_terminal_credits(6_000),
            )
            for suffix in ("a", "b")
        )
        backend = _Backend(recoverable=rows)
        backend.defer_claim_ids.add("attempt-b")
        result_box: list[CoordinatorResult] = []
        thread = threading.Thread(
            target=lambda: result_box.append(
                StagedParseCoordinator(
                    backend=backend,
                    limits=_limits(
                        credits=replace(_LIMIT, provider_result_bytes=10_000)
                    ),
                ).run()
            )
        )
        thread.start()
        deadline = time.monotonic() + 1
        while (
            "local_prepare:attempt-a" not in backend.calls
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        self.assertIn("local_prepare:attempt-a", backend.calls)
        self.assertNotIn("local_prepare:attempt-b", backend.calls)
        backend.defer_claim_ids.clear()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_box[0].terminal, CoordinatorTerminal.QUIESCENT)

    def test_deferred_shrink_clears_emergency_before_other_dimension_growth(
        self,
    ) -> None:
        backend = _Backend(
            recoverable=(
                _work("attempt-a", "materializing", 5),
                _work("attempt-b", "materializing", 5),
            )
        )
        backend.defer_claim_ids.add("attempt-b")
        backend.deferred_retry_after_seconds = 0.001
        backend.deferred_projection_sequences["attempt-b"] = [
            _work("attempt-b", "materializing", 5),
            _work("attempt-b", "local_materialized", 6),
        ]
        clock_value = [time.monotonic()]

        def advancing_monotonic() -> float:
            clock_value[0] += 0.01
            return clock_value[0]

        result = StagedParseCoordinator(
            backend=backend,
            limits=_limits(
                credits=replace(
                    _LIMIT,
                    materialization_items=1,
                    output_items=1,
                    output_pages=10,
                ),
                idle_open_circuit_seconds=0.02,
            ),
            monotonic=advancing_monotonic,
        ).run(stop_requested=lambda: backend.claim_attempts.get("attempt-b", 0) >= 2)
        # B's refreshed durable db-stage ownership makes the aggregate fit,
        # so A must lose emergency status and may not create a second db item.
        self.assertNotIn("local:attempt-a", backend.calls)
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)

    def test_duplicate_attempt_id_in_recovery_page_fails_closed(self) -> None:
        backend = _Backend(recoverable=(_work("attempt-a", "submitted", 2),))
        backend.duplicate_recovery_page = True
        with self.assertRaisesRegex(RuntimeError, "strictly ordered"):
            StagedParseCoordinator(backend=backend, limits=_limits()).run()
        self.assertNotIn("remote:attempt-a", backend.calls)

    def test_stage_renews_near_expiry_claim_before_side_effect(self) -> None:
        near_expiry = replace(
            _work("attempt-1", "prepared"),
            lease_expires_monotonic=time.monotonic() + 0.01,
        )
        backend = _Backend(new=(near_expiry,))
        result = StagedParseCoordinator(
            backend=backend,
            limits=_limits(
                claim_lease_seconds=2,
                claim_renew_margin_seconds=0.5,
                max_stage_step_seconds=1,
            ),
        ).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertGreaterEqual(backend.renew_calls, 1)
        self.assertLess(
            backend.calls.index("renew:attempt-1"),
            backend.calls.index("preflight:attempt-1"),
        )

    def test_inflight_renew_commit_race_uses_durable_reload(self) -> None:
        initial = replace(
            _work("attempt-1", "submitted", 3),
            lease_expires_monotonic=time.monotonic() + 0.01,
        )
        backend = _Backend(recoverable=(initial,))
        backend.block_remote = True
        backend.claim_lease_seconds_override = 0.01
        backend.renew_lease_seconds_override = 0.6
        backend.fail_renew_after = 2
        backend.reload_result = replace(
            _work("attempt-1", "remote_terminal", 4),
            claim_generation=2,
            claim_owner_identity="worker-boot-1",
            lease_expires_monotonic=time.monotonic() + 1,
        )
        result_box: list[CoordinatorResult] = []
        thread = threading.Thread(
            target=lambda: result_box.append(
                StagedParseCoordinator(
                    backend=backend,
                    limits=_limits(
                        claim_lease_seconds=1,
                        claim_renew_margin_seconds=0.2,
                        max_stage_step_seconds=0.3,
                    ),
                ).run()
            )
        )
        thread.start()
        deadline = time.monotonic() + 1
        while "reload:attempt-1" not in backend.calls and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertIn("reload:attempt-1", backend.calls)
        backend.remote_release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        result = result_box[0]
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(result.errors, ())

    def test_inflight_claim_loss_revokes_the_running_stage_guard(self) -> None:
        initial = replace(
            _work("attempt-1", "submitted", 3),
            lease_expires_monotonic=time.monotonic() + 0.01,
        )
        backend = _Backend(recoverable=(initial,))
        backend.block_remote = True
        backend.claim_lease_seconds_override = 0.01
        backend.renew_lease_seconds_override = 0.6
        backend.fail_renew_after = 2
        backend.reload_result = replace(
            initial,
            claim_generation=99,
            claim_owner_identity="other-worker",
            lease_expires_monotonic=time.monotonic() + 1,
        )
        result_box: list[CoordinatorResult] = []
        thread = threading.Thread(
            target=lambda: result_box.append(
                StagedParseCoordinator(
                    backend=backend,
                    limits=_limits(
                        claim_lease_seconds=1,
                        claim_renew_margin_seconds=0.2,
                        max_stage_step_seconds=0.3,
                    ),
                ).run()
            )
        )
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_box[0].terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertTrue(any("claim" in error for error in result_box[0].errors))

    def test_short_renewal_fails_before_the_stage_backend_is_called(self) -> None:
        initial = replace(
            _work("attempt-1", "prepared"),
            lease_expires_monotonic=time.monotonic() + 0.01,
        )
        backend = _Backend(new=(initial,))
        backend.renew_lease_seconds_override = 0.02
        result = StagedParseCoordinator(
            backend=backend,
            limits=_limits(
                claim_lease_seconds=1,
                claim_renew_margin_seconds=0.2,
                max_stage_step_seconds=0.3,
            ),
        ).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertNotIn("preflight:attempt-1", backend.calls)
        self.assertTrue(any("renewal cannot cover" in error for error in result.errors))

    def test_retry_backoff_is_clamped_inside_the_live_claim_lease(self) -> None:
        backend = _Backend(new=(_work("attempt-1", "prepared"),))
        backend.retry_remote_once = True
        started = time.monotonic()
        result = StagedParseCoordinator(
            backend=backend,
            limits=_limits(
                claim_lease_seconds=1,
                claim_renew_margin_seconds=0.2,
                max_stage_step_seconds=0.3,
                retry_initial_backoff_seconds=30,
                retry_max_backoff_seconds=30,
            ),
        ).run()
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)

    def test_long_remote_is_guarded_while_other_lane_completions_arrive(self) -> None:
        backend = _Backend(
            recoverable=(
                _work("attempt-ack", "ack_pending", 8),
                _work("attempt-remote", "submitted", 3),
            )
        )
        backend.block_remote = True
        backend.claim_lease_seconds_override = 0.02
        backend.renew_lease_seconds_override = 0.6
        result_box: list[CoordinatorResult] = []
        thread = threading.Thread(
            target=lambda: result_box.append(
                StagedParseCoordinator(
                    backend=backend,
                    limits=_limits(
                        claim_lease_seconds=1,
                        claim_renew_margin_seconds=0.2,
                        max_stage_step_seconds=0.3,
                    ),
                ).run()
            )
        )
        thread.start()
        deadline = time.monotonic() + 1
        while backend.renew_calls < 2 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertGreaterEqual(backend.renew_calls, 2)
        self.assertIn("ack:attempt-ack:ack_pending", backend.calls)
        backend.remote_release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_box[0].terminal, CoordinatorTerminal.QUIESCENT)

    def test_stage_deadline_opens_circuit_and_stops_lease_extension(self) -> None:
        initial = replace(
            _work("attempt-1", "submitted", 3),
            lease_expires_monotonic=time.monotonic() + 0.01,
        )
        backend = _Backend(recoverable=(initial,))
        backend.block_remote = True
        snapshots: list[CoordinatorSnapshot] = []
        result_box: list[CoordinatorResult] = []
        thread = threading.Thread(
            target=lambda: result_box.append(
                StagedParseCoordinator(
                    backend=backend,
                    limits=_limits(
                        claim_lease_seconds=1,
                        claim_renew_margin_seconds=0.2,
                        max_stage_step_seconds=0.03,
                    ),
                    progress=snapshots.append,
                ).run()
            )
        )
        thread.start()
        deadline = time.monotonic() + 1
        while (
            not any(
                snapshot.blocked_reason == "bounded_stage_deadline_exceeded"
                for snapshot in snapshots
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        self.assertTrue(
            any(
                snapshot.blocked_reason == "bounded_stage_deadline_exceeded"
                for snapshot in snapshots
            )
        )
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        renewals = backend.renew_calls
        result = result_box[0]
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertTrue(
            any("stage deadline exceeded" in error for error in result.errors)
        )
        self.assertEqual(backend.renew_calls, renewals)

    def test_retry_degrades_admission_then_success_reopens(self) -> None:
        backend = _Backend(new=(_work("attempt-1", "prepared"),))
        backend.retry_remote_remaining = 2
        snapshots: list[CoordinatorSnapshot] = []
        result = StagedParseCoordinator(
            backend=backend,
            limits=_limits(
                retry_initial_backoff_seconds=0.001,
                retry_max_backoff_seconds=0.002,
                retry_consecutive_threshold=2,
                retry_max_attempts=4,
                retry_stuck_seconds=1,
            ),
            progress=snapshots.append,
        ).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertTrue(any(s.blocked_reason == "retry_degraded" for s in snapshots))
        self.assertEqual(result.final_states, (("attempt-1", "acked"),))

    def test_persistent_retry_becomes_stuck_and_retains_reservation(self) -> None:
        backend = _Backend(new=(_work("attempt-1", "prepared"),))
        backend.retry_remote_remaining = 20
        result = StagedParseCoordinator(
            backend=backend,
            limits=_limits(
                retry_initial_backoff_seconds=0.001,
                retry_max_backoff_seconds=0.002,
                retry_consecutive_threshold=2,
                retry_max_attempts=3,
                retry_stuck_seconds=1,
            ),
        ).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertEqual(result.completed, 0)
        self.assertEqual(result.credits_in_use.documents, 1)


def result_ready(backend: _Backend) -> bool:
    return any(call.startswith("ack:") for call in backend.calls)


if __name__ == "__main__":
    unittest.main()
