"""Completion credit and progress-based stopping through real coordinator dispatch.

LOCAL_PREPARE takes decode credit only together with the LOCAL completion it
enables; the durable materializing head keeps that promise until its LOCAL
commits the actual output, and a restart rebuilds it from the head alone. A
stop needs an idle state with no wake path, not holds that happen to fill a
dimension. The 84-attempt shape uses the sizes, pages and caps of the
2026-09-29 stopped state (synthetic identities; no runtime or database).
"""

from __future__ import annotations

from dataclasses import fields, replace
import json
import threading
import time
from types import SimpleNamespace
import unittest

from disclosure_anchor.application.ports.new_work_admission import NewWorkAdmissionUnavailable
from disclosure_anchor.application.ports.staged_execution import StageNote
from disclosure_anchor.application.services.staged_execution_guard import StageLeaseLost
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CAPACITY_HOLD_BYTE_BOUNDS,
    AdmissionOutcome,
    BlockedLane,
    CapacityHoldDetail,
    CapacityHoldEvent,
    CoordinatorLane,
    CoordinatorResult,
    CoordinatorSnapshot,
    CoordinatorTerminal,
    CoordinatorWork,
    CreditPressure,
    NoProgressSummary,
    ResourceCreditVector,
    StageCapacityBlocked,
    StagedParseCoordinator,
    StageResourceGrantRequired,
    StageWaiting,
)
from disclosure_anchor.application.services.worker_stop_latch import InProcessWorkerStopLatch
from tests.unit import test_staged_new_work_admission_v4 as admission_fixture
from tests.unit.test_staged_parse_coordinator import _LIFECYCLE_RESERVATION, _LIMIT, _Backend, _limits, _work

GIB = 1 << 30
_SECRETS = ("sk-live-HOLDSECRET", "/Volumes/AgentSSD/secret/request.json", "业绩预告正文")
_SECRET_MESSAGE = "publication record refused: " + " | ".join(_SECRETS)

# The qualified caps and result-storage policy of the stopped runtime.
_CAPS = ResourceCreditVector(
    documents=128, snapshot_items=128, snapshot_bytes=134_217_728, remote_waits=14, provider_tasks=128,
    provider_result_bytes=17_179_869_184, materialization_items=2, compressed_bytes=17_179_869_184,
    decoded_bytes=4 * GIB, temp_disk_bytes=68_719_476_736, output_items=114,
    output_bytes=17_179_869_184, output_pages=4096, ack_items=128,
)
_WORK_DISK = dict(
    work_disk_bytes=68_719_476_736, work_disk_margin_bytes=409_632_768,
    work_disk_local_reserve_bytes=33_285_996_544,
)


def _grant(*, snapshot: int, temp: int, output: int, pages: int) -> ResourceCreditVector:
    """A materialization grant's effective reservation (estimate united with the grant)."""
    return ResourceCreditVector(
        documents=1, snapshot_items=1, snapshot_bytes=snapshot, remote_waits=1, provider_tasks=1,
        provider_result_bytes=536_870_912, materialization_items=1, compressed_bytes=536_870_912,
        decoded_bytes=4 * GIB, temp_disk_bytes=temp, output_items=1, output_bytes=output,
        output_pages=pages, ack_items=1,
    )


def _owned(attempt_id: str, state: str, version: int, *, reservation: ResourceCreditVector,
           credits: ResourceCreditVector) -> CoordinatorWork:
    return replace(_work(attempt_id, state, version), credit_reservation=reservation, credits=credits)


def _local_materialized(attempt_id: str, *, snapshot: int, result: int, output: int, pages: int,
                        temp: int, granted_output: int) -> CoordinatorWork:
    return _owned(
        attempt_id, "local_materialized", 6,
        reservation=_grant(snapshot=snapshot, temp=temp, output=granted_output, pages=pages),
        credits=ResourceCreditVector(
            documents=1, snapshot_items=1, snapshot_bytes=snapshot, provider_tasks=1,
            provider_result_bytes=result, compressed_bytes=result, output_items=1, output_bytes=output,
            output_pages=pages, ack_items=1,
        ),
    )


def _shape_84(terminal_count: int = 80) -> tuple[CoordinatorWork, ...]:
    """Three unpublished outputs holding 3146 pages, one materializing head holding
    all decode credit and promised 1058 pages, and remote results waiting for decode."""
    held = (
        _local_materialized("held-1035", snapshot=15_624_663, result=109_116_036, output=224_306_230,
                            pages=1035, temp=4_611_484_197, granted_output=4_502_368_161),
        _local_materialized("held-1054", snapshot=15_511_321, result=108_742_952, output=225_736_476,
                            pages=1054, temp=4_612_268_349, granted_output=4_503_525_397),
        _local_materialized("held-1057", snapshot=15_882_797, result=112_228_693, output=235_986_043,
                            pages=1057, temp=4_623_979_519, granted_output=4_511_750_826),
    )
    materializing = _owned(
        "mat-1058", "materializing", 5,
        reservation=_grant(snapshot=15_573_079, temp=4_613_338_054, output=4_504_277_562, pages=1058),
        credits=ResourceCreditVector(
            documents=1, snapshot_items=1, snapshot_bytes=15_573_079, provider_tasks=1,
            provider_result_bytes=109_060_492, materialization_items=1, compressed_bytes=109_060_492,
            decoded_bytes=4 * GIB, temp_disk_bytes=4_613_338_054, ack_items=1,
        ),
    )
    terminal = tuple(
        _owned(
            f"rt-{index:02d}", "remote_terminal", 3,
            reservation=ResourceCreditVector(
                documents=1, snapshot_items=1, snapshot_bytes=800_000, remote_waits=1, provider_tasks=1,
                provider_result_bytes=536_870_912, materialization_items=1, compressed_bytes=536_870_912,
                decoded_bytes=4 * GIB, temp_disk_bytes=2 * GIB, output_items=1, output_bytes=1_610_612_736,
                output_pages=1 + index % 5, ack_items=1,
            ),
            credits=ResourceCreditVector(
                documents=1, snapshot_items=1, snapshot_bytes=800_000, provider_tasks=1,
                provider_result_bytes=5_000_000, ack_items=1,
            ),
        )
        for index in range(terminal_count)
    )
    return (*held, materializing, *terminal)


class _SizedBackend(_Backend):
    """Durable successors own exactly what their state owns under the reservation.

    Every transition updates the owned vectors, so the peak of output pages
    actually owned at once is observable.
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.lock = threading.Lock()
        self.owned = {work.attempt_id: work.credits for work in self.recoverable}
        self.peak_pages = sum(credits.output_pages for credits in self.owned.values())
        self.commit_holds: dict[str, StageCapacityBlocked] = {}
        self.local_allowances: dict[str, ResourceCreditVector] = {}

    def _next(self, work: CoordinatorWork, state: str, allowance: ResourceCreditVector) -> CoordinatorWork:
        held, reserved = work.credits, work.credit_reservation
        base = dict(documents=1, snapshot_items=1, snapshot_bytes=held.snapshot_bytes, provider_tasks=1,
                    provider_result_bytes=held.provider_result_bytes, ack_items=1)
        if state == "materializing":
            credits = ResourceCreditVector(
                **base, materialization_items=1, compressed_bytes=held.provider_result_bytes,
                decoded_bytes=reserved.decoded_bytes, temp_disk_bytes=reserved.temp_disk_bytes,
            )
        elif state == "local_materialized":
            credits = ResourceCreditVector(
                **base, compressed_bytes=held.provider_result_bytes, output_items=1,
                output_bytes=reserved.output_bytes // 20, output_pages=reserved.output_pages,
            )
        elif state in {"publish_committed", "cleanup_pending"}:
            credits = held
        else:
            credits = ResourceCreditVector(documents=1, provider_tasks=1,
                                           provider_result_bytes=held.provider_result_bytes, ack_items=1)
        updated = replace(
            _work(work.attempt_id, state, work.lifecycle_version + 1),
            claim_generation=work.claim_generation, claim_owner_identity=work.claim_owner_identity,
            lease_expires_monotonic=work.lease_expires_monotonic, credit_reservation=reserved, credits=credits,
        )
        self._assert_credit_grant(work, updated, allowance)
        with self.lock:
            self.owned[work.attempt_id] = credits
            self.peak_pages = max(self.peak_pages, sum(item.output_pages for item in self.owned.values()))
        return updated

    def prepare_local_io(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        stage_guard.checkpoint()
        self.calls.append(f"local_prepare:{work.attempt_id}")
        return self._next(work, "materializing", credit_allowance)

    def run_local(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        stage_guard.checkpoint()
        self.calls.append(f"local:{work.attempt_id}")
        self.local_allowances[work.attempt_id] = credit_allowance
        return self._next(work, "local_materialized", credit_allowance)

    def commit(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        stage_guard.checkpoint()
        hold = self.commit_holds.get(work.attempt_id)
        if hold is not None:
            self.calls.append(f"commit-hold:{work.attempt_id}")
            raise hold
        self.calls.append(f"commit:{work.attempt_id}")
        return self._next(work, "publish_committed", credit_allowance)

    def cleanup(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        stage_guard.checkpoint()
        self.calls.append(f"cleanup:{work.attempt_id}:{work.state}")
        target = "cleanup_pending" if work.state == "publish_committed" else "ack_pending"
        return self._next(work, target, credit_allowance)

    def acknowledge(self, work, *, stage_guard):  # type: ignore[no-untyped-def]
        acked = super().acknowledge(work, stage_guard=stage_guard)
        with self.lock:
            self.owned.pop(work.attempt_id, None)
        return acked


def _run(coordinator: StagedParseCoordinator, *, seconds: float = 10.0,
         drain: threading.Event | None = None) -> CoordinatorResult:
    """Run to its own end; needing the safety bound is itself a failure (a silent wait).

    ``drain`` is an operator stop request, which is a legitimate end of the run.
    """
    deadline = time.monotonic() + seconds
    bound_used: list[bool] = []

    def stop_requested() -> bool:
        if drain is not None and drain.is_set():
            return True
        if time.monotonic() < deadline:
            return False
        bound_used.append(True)
        return True

    result = coordinator.run(stop_requested=stop_requested)
    if bound_used:
        raise AssertionError(f"the run waited until its safety bound: {result!r}")
    return result


def _latch(backend: _Backend) -> tuple[InProcessWorkerStopLatch, list[int]]:
    calls_at_trip: list[int] = []
    return InProcessWorkerStopLatch(
        on_first_trip=lambda _cause: calls_at_trip.append(len(backend.calls)),
    ), calls_at_trip


def _summary(result: CoordinatorResult) -> NoProgressSummary:
    summaries = [item for item in result.diagnostics if isinstance(item, NoProgressSummary)]
    assert len(summaries) == 1, result.diagnostics
    return summaries[0]


class _RecordingObserver:
    def __init__(self) -> None:
        self.notes: list[StageNote] = []
        self.failures: list[BaseException] = []

    def note(self, record: StageNote) -> None:
        self.notes.append(record)

    def record_failure(self, error: BaseException) -> None:
        self.failures.append(error)


class RecoveredShapeTests(unittest.TestCase):
    def test_held_publications_stop_the_84_shape_once_and_name_the_chain(self) -> None:
        backend = _SizedBackend(recoverable=_shape_84())
        detail = CapacityHoldDetail(record_kind="publication_request", byte_count=10_496_982,
                                    limit=8_388_608, policy_sha256="sha256:" + "a" * 64)
        for attempt_id in ("held-1035", "held-1054", "held-1057"):
            backend.commit_holds[attempt_id] = StageCapacityBlocked(
                _SECRET_MESSAGE, dimensions=("publication_envelope",), detail=detail,
            )
        latch, _calls = _latch(backend)
        result = _run(StagedParseCoordinator(
            backend=backend, limits=_limits(credits=_CAPS, **_WORK_DISK), stop_control=latch,
        ))
        cause = latch.first_cause()
        assert cause is not None, result
        holds = [item for item in result.diagnostics if isinstance(item, CapacityHoldEvent)]
        self.assertEqual([item.attempt_id for item in holds], ["held-1035", "held-1054", "held-1057"])
        self.assertEqual((cause.kind, cause.reason_code, cause.attempt_id, cause.lane, cause.state_at_dispatch),
                         ("coordinator_circuit", "capacity_holds_exhausted", "held-1035", "commit",
                          "local_materialized"))
        self.assertEqual(cause.exception_fingerprint, holds[0].exception_fingerprint)
        self.assertEqual(result.termination_kind, "public_stop")
        # Each held COMMIT ran once; no decode or output credit was taken, and
        # nothing was cleaned, failed or ACKed.
        self.assertEqual(sorted(call for call in backend.calls if call.startswith("commit-hold:")),
                         ["commit-hold:held-1035", "commit-hold:held-1054", "commit-hold:held-1057"])
        self.assertFalse([call for call in backend.calls
                          if call.startswith(("local:", "local_prepare:", "cleanup:", "ack:"))])
        self.assertEqual(result.final_states, ())
        self.assertEqual(result.credits_in_use.output_pages, 3146)
        self.assertEqual(result.credits_in_use.decoded_bytes, 4 * GIB)
        self.assertIn("capacity_holds_exhausted:decoded_bytes,output_pages", result.errors)
        summary = _summary(result)
        self.assertEqual(summary.reason_code, "capacity_holds_exhausted")
        self.assertEqual(summary.hold_count, 3)
        self.assertEqual(
            [(lane.lane, lane.queued, lane.head_attempt_id, lane.shortages) for lane in summary.blocked_lanes],
            [("local", 1, "mat-1058", ("output_pages",)),
             ("local_prepare", 80, "rt-00", ("decoded_bytes", "output_pages"))],
        )
        pressure = {item.dimension: item for item in summary.pressure}
        self.assertEqual(sorted(pressure), ["decoded_bytes", "output_pages"])
        pages = pressure["output_pages"]
        self.assertEqual((pages.owned, pages.promised, pages.requested, pages.limit), (3146, 1058, 1058, 4096))
        self.assertEqual(pages.holders, ("mat-1058", "held-1057", "held-1054"))
        decoded = pressure["decoded_bytes"]
        self.assertEqual((decoded.owned, decoded.promised, decoded.requested, decoded.limit, decoded.holders),
                         (4 * GIB, 0, 4 * GIB, 4 * GIB, ("mat-1058",)))
        self.assertEqual({item.detail for item in holds}, {detail})
        encoded = json.dumps([item.to_payload() for item in result.diagnostics], ensure_ascii=False)
        encoded += json.dumps(cause.to_payload(), ensure_ascii=False)
        for secret in _SECRETS:
            self.assertNotIn(secret, encoded)

    def test_recovered_promise_deficit_drains_through_the_tail_before_any_new_growth(self) -> None:
        backend = _SizedBackend(recoverable=_shape_84())
        # Decode credit for two documents: only the promised pages hold new
        # LOCAL_PREPARE work back, so a missing promise would start it at once.
        limits = _limits(credits=replace(_CAPS, decoded_bytes=8 * GIB), **_WORK_DISK)
        latch, _calls = _latch(backend)
        result = _run(StagedParseCoordinator(backend=backend, limits=limits, stop_control=latch))
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT, result.errors)
        self.assertIsNone(latch.first_cause())
        self.assertEqual(result.errors, ())
        self.assertEqual(dict(result.final_states), {work.attempt_id: "acked" for work in _shape_84()})
        self.assertEqual(result.credits_in_use, ResourceCreditVector())
        # 3146 owned + 1058 promised > 4096: nothing grows until a held output
        # is released, and owned pages never pass the limit.
        released = min(index for index, call in enumerate(backend.calls)
                       if call.startswith("cleanup:held-") and call.endswith(":cleanup_pending"))
        first_prepare = min(index for index, call in enumerate(backend.calls)
                            if call.startswith("local_prepare:"))
        self.assertLess(released, backend.calls.index("local:mat-1058"))
        self.assertLess(released, first_prepare)
        self.assertLessEqual(backend.peak_pages, 4096)
        # The kept promise is exactly the durable grant's completion, once.
        self.assertEqual(
            backend.local_allowances["mat-1058"],
            ResourceCreditVector(output_items=1, output_bytes=4_504_277_562, output_pages=1058),
        )


class CompletionPromiseTests(unittest.TestCase):
    def test_a_restart_rebuilds_the_promise_from_the_durable_materializing_head(self) -> None:
        paged = replace(_LIFECYCLE_RESERVATION, output_pages=60)

        class _FirstBoot(_Backend):
            durable: CoordinatorWork | None = None

            def prepare_local_io(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
                self.durable = super().prepare_local_io(
                    work, credit_allowance=credit_allowance, stage_guard=stage_guard,
                )
                return self.durable

            def run_local(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
                # The boot is drained before LOCAL: only the durable head survives.
                raise StageLeaseLost("drained before LOCAL", provenance="operator_cancel")

        first = _FirstBoot(recoverable=(replace(_work("x", "remote_terminal", 3), credit_reservation=paged),))
        drained = _run(StagedParseCoordinator(backend=first, limits=_limits()))
        self.assertEqual(drained.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        durable = first.durable
        assert durable is not None
        self.assertEqual((durable.state, durable.credits.output_pages), ("materializing", 0))

        # A fresh coordinator sees only durable heads. X's rebuilt promise and
        # Y's completion (60 + 60) exceed 100 pages: Y waits for X's release.
        second = _SizedBackend(recoverable=(
            durable, replace(_work("y", "remote_terminal", 3), credit_reservation=paged),
        ))
        snapshots: list[CoordinatorSnapshot] = []
        result = _run(StagedParseCoordinator(
            backend=second, limits=_limits(credits=replace(_LIMIT, output_pages=100)),
            progress=snapshots.append,
        ))
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT, result.errors)
        self.assertEqual(dict(result.final_states), {"x": "acked", "y": "acked"})
        self.assertLess(second.calls.index("cleanup:x:cleanup_pending"), second.calls.index("local_prepare:y"))
        self.assertEqual(snapshots[0].credits_promised.output_pages, 60)

    def test_a_kept_promise_counts_once_even_when_it_fills_the_limit(self) -> None:
        backend = _Backend(recoverable=(
            _work("x", "materializing", 5), _work("y", "remote_terminal", 3),
        ))
        result = _run(StagedParseCoordinator(
            backend=backend,
            limits=_limits(credits=replace(_LIMIT, output_items=1, output_pages=10)),
        ))
        # X's promise is the whole page and item limit: its LOCAL keeps it and
        # runs; Y's new promise waits for X's release instead of both starving.
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT, result.errors)
        self.assertEqual(dict(result.final_states), {"x": "acked", "y": "acked"})
        self.assertLess(backend.calls.index("local:x"), backend.calls.index("local_prepare:y"))
        self.assertLess(backend.calls.index("cleanup:x:cleanup_pending"), backend.calls.index("local_prepare:y"))

    def test_a_grown_grant_is_rechecked_beside_open_promises_before_its_stage(self) -> None:
        granted = replace(_LIFECYCLE_RESERVATION, output_bytes=900)

        class _GrowingBackend(_Backend):
            def renew_claim(self, work, *, lease_seconds):  # type: ignore[no-untyped-def]
                renewed = super().renew_claim(work, lease_seconds=lease_seconds)
                return replace(renewed, credit_reservation=work.durable_reservation,
                               durable_credit_reservation=None)

            def prepare_local_io(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
                if work.attempt_id == "y" and not granted.fits(work.credit_reservation):
                    self.calls.append("grant_required:y")
                    raise StageResourceGrantRequired("verified result needs its grant", required=granted)
                return super().prepare_local_io(work, credit_allowance=credit_allowance, stage_guard=stage_guard)

        backend = _GrowingBackend(recoverable=(
            _work("x", "materializing", 5), _work("y", "remote_terminal", 3),
        ))
        result = _run(StagedParseCoordinator(
            backend=backend, limits=_limits(credits=replace(_LIMIT, output_bytes=1_000)),
        ))
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT, result.errors)
        self.assertEqual(dict(result.final_states), {"x": "acked", "y": "acked"})
        # The estimate fitted beside X's 400-byte promise; the grown 900 does
        # not, so the granted stage waits for X's output to be released.
        requested = backend.calls.index("grant_required:y")
        self.assertLess(requested, backend.calls.index("local_prepare:y"))
        self.assertLess(backend.calls.index("cleanup:x:cleanup_pending"), backend.calls.index("local_prepare:y"))

    def test_the_promise_and_the_local_grant_share_one_work_volume_extent(self) -> None:
        backend = _Backend(recoverable=(_work("x", "remote_terminal", 3), _work("y", "remote_terminal", 3)))
        snapshots: list[CoordinatorSnapshot] = []
        # Each source owns 100; a LOCAL grant covers spool, tree and outputs in
        # 500 (spool 100 + output 400 inside it). D = 700 fits one grant beside
        # the other source, and the LOCAL that keeps the promise adds nothing.
        result = _run(StagedParseCoordinator(
            backend=backend, limits=_limits(work_disk_bytes=700), progress=snapshots.append,
        ))
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT, result.errors)
        self.assertEqual(dict(result.final_states), {"x": "acked", "y": "acked"})
        self.assertLess(backend.calls.index("local:x"), backend.calls.index("local_prepare:y"))
        self.assertLess(backend.calls.index("cleanup:x:cleanup_pending"), backend.calls.index("local_prepare:y"))
        self.assertIn(400, {snapshot.credits_promised.output_bytes for snapshot in snapshots})


class ProgressStopTests(unittest.TestCase):
    def test_a_hold_below_every_limit_stops_when_it_blocks_the_only_candidate(self) -> None:
        held = replace(_work("held", "local_materialized", 5),
                       credit_reservation=replace(_LIFECYCLE_RESERVATION, output_pages=600),
                       credits=replace(_work("held", "local_materialized", 5).credits, output_pages=600))
        waiting = replace(_work("wait", "remote_terminal", 3),
                          credit_reservation=replace(_LIFECYCLE_RESERVATION, output_pages=500))
        backend = _SizedBackend(recoverable=(held, waiting))
        backend.commit_holds["held"] = StageCapacityBlocked(
            _SECRET_MESSAGE, dimensions=("publication_envelope",),
            detail=CapacityHoldDetail(record_kind="publication_readiness", byte_count=9_000_000,
                                      bound="lower_bound", limit=8_388_608),
        )
        observer = _RecordingObserver()
        latch, _calls = _latch(backend)
        result = _run(StagedParseCoordinator(
            backend=backend, limits=_limits(credits=replace(_LIMIT, output_pages=1_000)),
            stop_control=latch, stage_observer=observer,
        ))
        cause = latch.first_cause()
        assert cause is not None, result
        # 600 < 1000 pages, yet the held output leaves no room for 500 more.
        self.assertEqual((cause.reason_code, cause.attempt_id, cause.lane),
                         ("capacity_holds_exhausted", "held", "commit"))
        self.assertNotIn("local_prepare:wait", backend.calls)
        pages = {item.dimension: item for item in _summary(result).pressure}["output_pages"]
        self.assertEqual((pages.owned, pages.promised, pages.requested, pages.limit, pages.holders),
                         (600, 0, 500, 1_000, ("held",)))
        hold_note = next(note for note in observer.notes if note.kind == "capacity_hold")
        self.assertEqual((hold_note.attempt_id, hold_note.lane), ("held", "commit"))
        self.assertEqual(
            {key: value for key, value in hold_note.scalars
             if key in {"record_kind", "byte_count", "bound", "limit", "dimensions"}},
            {"record_kind": "publication_readiness", "byte_count": 9_000_000, "bound": "lower_bound",
             "limit": 8_388_608, "dimensions": "publication_envelope"},
        )
        stop_note = next(note for note in observer.notes if note.kind == "no_progress")
        self.assertEqual(
            {key: value for key, value in stop_note.scalars
             if key in {"reason_code", "first_hold", "output_pages_owned", "output_pages_requested",
                        "output_pages_limit"}},
            {"reason_code": "capacity_holds_exhausted", "first_hold": "held", "output_pages_owned": 600,
             "output_pages_requested": 500, "output_pages_limit": 1_000},
        )
        self.assertEqual(observer.failures, [])
        for note in observer.notes:
            for secret in _SECRETS:
                self.assertNotIn(secret, repr(note.scalars))

    def test_holds_wait_while_any_other_path_can_still_progress(self) -> None:
        def held_commit_backend(**kwargs: object) -> _SizedBackend:
            backend = _SizedBackend(**kwargs)
            backend.commit_holds["held"] = StageCapacityBlocked(
                "held", dimensions=("publication_envelope",),
            )
            return backend

        held = _work("held", "local_materialized", 5)
        cases: dict[str, tuple[_SizedBackend, str]] = {}
        waiting = held_commit_backend(recoverable=(held, _work("w", "submitted", 2)))
        waiting.wait_remote_by_attempt["w"] = 3  # a healthy provider wait
        cases["retry timer"] = (waiting, "w")
        deferred = held_commit_backend(recoverable=(_work("d", "submitted", 2), held))
        deferred.defer_claim_ids.add("d")  # a live foreign lease that expires
        original_claim = deferred.claim_recovery

        def claim_after_one_deferral(candidate):  # type: ignore[no-untyped-def]
            try:
                return original_claim(candidate)
            finally:
                deferred.defer_claim_ids.discard(candidate.attempt_id)

        deferred.claim_recovery = claim_after_one_deferral  # type: ignore[method-assign]
        cases["deferred claim"] = (deferred, "d")

        class _PagedBackend(_SizedBackend):
            pages = 0

            def admit_new(self, *, limit, available_credits):  # type: ignore[no-untyped-def]
                self.pages += 1
                if self.pages < 4:  # unread pages of a still-open admission scan
                    return AdmissionOutcome(work=(), backlog_exists=True, scan_incomplete=True)
                return super().admit_new(limit=limit, available_credits=available_credits)

        paged = _PagedBackend(recoverable=(held,), new=(_work("n", "prepared"),))
        paged.commit_holds["held"] = StageCapacityBlocked("held", dimensions=("publication_envelope",))
        cases["admission scan"] = (paged, "n")
        for label, (backend, progressing) in cases.items():
            with self.subTest(label):
                latch, calls_at_trip = _latch(backend)
                result = _run(StagedParseCoordinator(backend=backend, limits=_limits(), stop_control=latch))
                cause = latch.first_cause()
                assert cause is not None, result
                self.assertEqual((cause.reason_code, cause.attempt_id), ("capacity_holds_exhausted", "held"))
                self.assertEqual(dict(result.final_states), {progressing: "acked"})
                self.assertLess(backend.calls.index(f"ack:{progressing}:ack_pending"), calls_at_trip[0])
                self.assertEqual(backend.calls.count("commit-hold:held"), 1)

    def test_a_healthy_wait_beside_blocked_growth_is_not_a_stop(self) -> None:
        class _WaitTwice(_Backend):
            waits = 2

            def run_local(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
                if self.waits:
                    self.waits -= 1
                    raise StageWaiting("live free space is short", retry_after_seconds=0.001)
                return super().run_local(work, credit_allowance=credit_allowance, stage_guard=stage_guard)

        backend = _WaitTwice(recoverable=(_work("x", "materializing", 5), _work("y", "remote_terminal", 3)))
        latch, _calls = _latch(backend)
        # Y's promise waits for X's pages while X only waits for space: no stop.
        result = _run(StagedParseCoordinator(
            backend=backend, limits=_limits(credits=replace(_LIMIT, output_pages=10)), stop_control=latch,
        ))
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT, result.errors)
        self.assertIsNone(latch.first_cause())
        self.assertEqual(dict(result.final_states), {"x": "acked", "y": "acked"})

    def test_a_credit_deadlock_names_its_blocked_head_instead_of_nothing(self) -> None:
        backend = _Backend(recoverable=tuple(_work(name, "remote_terminal", 3) for name in ("rt-a", "rt-b", "rt-c")))
        latch, _calls = _latch(backend)
        # Three owned sources (300) leave no room in D = 700 for a 500-byte grant.
        result = _run(StagedParseCoordinator(backend=backend, limits=_limits(work_disk_bytes=700),
                                             stop_control=latch))
        cause = latch.first_cause()
        assert cause is not None, result
        self.assertEqual((cause.reason_code, cause.attempt_id, cause.lane, cause.state_at_dispatch),
                         ("resource_credit_grant_unavailable", "rt-a", "local_prepare", "remote_terminal"))
        self.assertIn("durable queued work cannot obtain its next credit grant", result.errors)
        summary = _summary(result)
        self.assertEqual((summary.hold_count, summary.holds), (0, ()))
        disk = {item.dimension: item for item in summary.pressure}["work_disk_bytes"]
        self.assertEqual((disk.owned, disk.promised, disk.requested, disk.limit, disk.holders),
                         (300, 0, 500, 700, ("rt-a", "rt-b", "rt-c")))
        self.assertFalse([call for call in backend.calls if call.startswith("local_prepare:")])


def _held_commit(**kwargs: object) -> _SizedBackend:
    backend = _SizedBackend(**kwargs)
    backend.commit_holds["held"] = StageCapacityBlocked("held", dimensions=("publication_envelope",))
    return backend


def _beside_real_admission(*, page_size: int = 1, prefix: int = 3, snapshot_bytes: int | None = None):  # type: ignore[no-untyped-def]
    """One held COMMIT beside the real ordinary admitter (real ingress factory, fake stores).

    The backlog is ``prefix`` rows of unknown size that are passed over for
    credit (the cursor moves on) and then ``doc-1``, which fits.
    """
    fixture = admission_fixture.StagedV4NewWorkAdmitterTests()._fixture  # a fixture method, not a test run
    admitter, source, claims, claimed, _credit = fixture(oversized_prefix=prefix, page_size=page_size)
    claims.claimed = replace(claimed, lease_expires_monotonic=time.monotonic() + 60)
    backend = _held_commit(recoverable=(_work("held", "local_materialized", 5),))

    def admit_new(*, limit, available_credits):  # type: ignore[no-untyped-def]
        backend.calls.append("admit")
        return admitter.admit_new(limit=limit, available_credits=available_credits)

    backend.admit_new = admit_new  # type: ignore[method-assign]
    capacity = admission_fixture._capacity()
    credits = capacity if snapshot_bytes is None else replace(capacity, snapshot_bytes=snapshot_bytes)
    return admitter, source, backend, _limits(credits=credits, admission_probe_seconds=0.05)


class WakePathTests(unittest.TestCase):
    """A hold waits while anything can still bring work; then the site stops, once."""

    def _stops_once_after(self, backend: _Backend, result: CoordinatorResult, latch: InProcessWorkerStopLatch,
                          calls_at_trip: list[int], acked: str) -> None:
        cause = latch.first_cause()
        assert cause is not None, result
        self.assertEqual((cause.reason_code, cause.attempt_id, cause.lane),
                         ("capacity_holds_exhausted", "held", "commit"))
        self.assertEqual(dict(result.final_states), {acked: "acked"})
        self.assertLess(backend.calls.index(f"ack:{acked}:ack_pending"), calls_at_trip[0])
        self.assertEqual(backend.calls.count("commit-hold:held"), 1)
        self.assertEqual(len(calls_at_trip), 1)

    def test_real_scans_read_past_rows_passed_over_for_credit_before_stopping(self) -> None:
        # The scanner reports the credit it lacked for passed-over rows while
        # its cursor moves on: an unread page, however pages fall, not a hold.
        for page_size in (1, 2, 4):
            with self.subTest(page_size=page_size):
                admitter, source, backend, limits = _beside_real_admission(page_size=page_size)
                latch, calls_at_trip = _latch(backend)
                result = _run(StagedParseCoordinator(
                    backend=backend, limits=limits, stop_control=latch, admission_observer=admitter,
                ))
                self.assertEqual((source.reads, source.admitted), (["doc-1"], ["doc-1"]))
                self.assertEqual(source.calls[0][:2], (None, "doc-1"))
                self._stops_once_after(backend, result, latch, calls_at_trip, acked="attempt-1")

    def test_a_scan_held_in_place_for_credit_stops_once_without_waiting(self) -> None:
        # A known size waits in place for snapshot credit the hold keeps: the
        # next call would read the same row, so it is no reason to wait.
        admitter, source, backend, limits = _beside_real_admission(prefix=0, snapshot_bytes=2_048)
        source.candidates = (admission_fixture._ordinary("doc-a", 4_096),)
        latch, calls_at_trip = _latch(backend)
        result = _run(StagedParseCoordinator(
            backend=backend, limits=limits, stop_control=latch, admission_observer=admitter,
        ))
        cause = latch.first_cause()
        assert cause is not None, result
        self.assertEqual((cause.reason_code, cause.attempt_id), ("capacity_holds_exhausted", "held"))
        self.assertGreaterEqual(backend.calls.count("admit"), 1)
        self.assertEqual((source.reads, source.admitted), ([], []))
        self.assertEqual(result.final_states, ())
        self.assertEqual((backend.calls.count("commit-hold:held"), len(calls_at_trip)), (1, 1))

    def test_readiness_deferral_waits_but_open_legacy_obligations_stop_once(self) -> None:
        for gate in ("readiness", "legacy_obligations"):
            with self.subTest(gate=gate):
                admitter, source, backend, limits = _beside_real_admission(prefix=0)
                if gate == "readiness":
                    refusals = [NewWorkAdmissionUnavailable("provider readiness unknown")]

                    def readiness() -> None:
                        if refusals:
                            raise refusals.pop()

                    admitter._admission_guard = readiness
                else:
                    # The held attempt is itself one of the obligations: it can
                    # only close by finishing, which it never does here.
                    admitter._legacy_obligations = SimpleNamespace(require_closed=lambda: (_ for _ in ()).throw(
                        NewWorkAdmissionUnavailable("legacy obligations open (1/1)")))
                latch, calls_at_trip = _latch(backend)
                result = _run(StagedParseCoordinator(
                    backend=backend, limits=limits, stop_control=latch, admission_observer=admitter,
                ))
                if gate == "readiness":
                    self.assertEqual(source.admitted, ["doc-1"])
                    self._stops_once_after(backend, result, latch, calls_at_trip, acked="attempt-1")
                else:
                    cause = latch.first_cause()
                    assert cause is not None, result
                    self.assertEqual((cause.reason_code, cause.attempt_id), ("capacity_holds_exhausted", "held"))
                    self.assertEqual((source.calls, source.reads, result.final_states), ([], [], ()))
                    self.assertEqual((backend.calls.count("commit-hold:held"), len(calls_at_trip)), (1, 1))

    def test_a_safe_cold_stream_pause_admits_its_backlog_before_the_single_stop(self) -> None:
        opens_at = time.monotonic() + 0.3

        class _ColdStream:
            policy = SimpleNamespace(config=SimpleNamespace(qualified_max=1))

            def current(self):  # type: ignore[no-untyped-def]
                warm = time.monotonic() >= opens_at
                return SimpleNamespace(unsafe=False, new_post_allowed=warm, target=1 if warm else 0,
                                       reason="qualified_target" if warm else "recovery", evidence_sha256=None)

        backend = _held_commit(recoverable=(_work("held", "local_materialized", 5),), new=(_work("n", "prepared"),))
        latch, calls_at_trip = _latch(backend)
        result = _run(StagedParseCoordinator(
            backend=backend, limits=_limits(), stop_control=latch, stream_control=_ColdStream(),  # type: ignore[arg-type]
        ))
        self._stops_once_after(backend, result, latch, calls_at_trip, acked="n")

    def test_an_operator_drain_leaves_holds_without_a_new_public_stop(self) -> None:
        with self.subTest("only holds are left"):
            drained = threading.Event()

            class _DrainAfterSmall(_SizedBackend):
                def acknowledge(self, work, *, stage_guard):  # type: ignore[no-untyped-def]
                    acked = super().acknowledge(work, stage_guard=stage_guard)
                    if work.attempt_id == "small":
                        drained.set()
                    return acked

            backend = _DrainAfterSmall(recoverable=(_work("big", "local_materialized", 5),
                                                    _work("small", "local_materialized", 5)))
            backend.commit_holds["big"] = StageCapacityBlocked("held", dimensions=("publication_envelope",))
            latch, _calls = _latch(backend)
            result = _run(StagedParseCoordinator(backend=backend, limits=_limits(), stop_control=latch),
                          drain=drained)
            self.assertIsNone(latch.first_cause())
            self.assertEqual((result.terminal, result.termination_kind),
                             (CoordinatorTerminal.STUCK_OPEN_CIRCUIT, "operator_drain"))
            self.assertEqual(dict(result.final_states), {"small": "acked"})
            self.assertFalse([call for call in backend.calls if call.startswith(("cleanup:big", "ack:big"))])
        with self.subTest("accepted work a hold blocks still stops, as before"):
            held = replace(_work("held", "local_materialized", 5),
                           credit_reservation=replace(_LIFECYCLE_RESERVATION, output_pages=600),
                           credits=replace(_work("held", "local_materialized", 5).credits, output_pages=600))
            waiting = replace(_work("wait", "remote_terminal", 3),
                              credit_reservation=replace(_LIFECYCLE_RESERVATION, output_pages=500))
            backend = _held_commit(recoverable=(held, waiting))
            drain = threading.Event()
            drain.set()
            latch, _calls = _latch(backend)
            result = _run(StagedParseCoordinator(
                backend=backend, limits=_limits(credits=replace(_LIMIT, output_pages=1_000)), stop_control=latch,
            ), drain=drain)
            cause = latch.first_cause()
            assert cause is not None, result
            self.assertEqual((cause.reason_code, cause.attempt_id), ("capacity_holds_exhausted", "held"))
            self.assertNotIn("local_prepare:wait", backend.calls)

    def test_a_persisting_hold_stops_each_run_once_with_the_same_cause(self) -> None:
        # A release lets recovery dispatch the held COMMIT once more; the hold
        # persists, so the next run stops again, once, with the same identity.
        causes = []
        for _run_number in range(2):
            backend = _held_commit(recoverable=(_work("held", "local_materialized", 5),))
            latch, calls_at_trip = _latch(backend)
            result = _run(StagedParseCoordinator(backend=backend, limits=_limits(), stop_control=latch))
            cause = latch.first_cause()
            assert cause is not None, result
            self.assertEqual((result.termination_kind, backend.calls.count("commit-hold:held"), len(calls_at_trip)),
                             ("public_stop", 1, 1))
            causes.append((cause.kind, cause.reason_code, cause.attempt_id, cause.lane, cause.state_at_dispatch,
                           cause.lifecycle_version_at_dispatch, cause.exception_fingerprint))
        self.assertEqual(causes[0], causes[1])
        self.assertEqual(causes[0][:5], ("coordinator_circuit", "capacity_holds_exhausted", "held", "commit",
                                         "local_materialized"))


class DiagnosticContractTests(unittest.TestCase):
    def test_hold_facts_are_closed_typed_and_content_free(self) -> None:
        valid = CapacityHoldDetail(record_kind="publication_request", byte_count=1, limit=2)
        self.assertEqual(valid.to_payload(), {"bound": "exact", "byte_count": 1, "limit": 2,
                                              "policy_sha256": None, "record_kind": "publication_request"})
        self.assertEqual(CAPACITY_HOLD_BYTE_BOUNDS, ("exact", "lower_bound", "upper_bound"))
        for bound in CAPACITY_HOLD_BYTE_BOUNDS:
            self.assertEqual(replace(valid, bound=bound).to_payload()["bound"], bound)
        for changes in (
            {"record_kind": "Publication Request"}, {"record_kind": _SECRET_MESSAGE}, {"byte_count": -1},
            {"limit": True}, {"bound": "roughly"}, {"bound": True}, {"bound": "Exact"},
            {"policy_sha256": "md5:abc"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(valid, **changes)
        with self.assertRaises(ValueError):
            StageCapacityBlocked("held", dimensions=("publication_envelope",),
                                 detail={"record_kind": "x"})  # type: ignore[arg-type]
        # A summary lists at most one head per lane and one pressure per dimension.
        lane = BlockedLane(lane="local", queued=1, head_attempt_id="a", head_state="materializing",
                           shortages=("output_pages",))
        pressure = CreditPressure(dimension="output_pages", owned=1, promised=0, requested=1, limit=1,
                                  holders=("a",))
        lanes, dimensions = len(CoordinatorLane), len(fields(ResourceCreditVector)) + 1
        NoProgressSummary(reason_code="capacity_holds_exhausted", blocked_lanes=(lane,) * lanes, holds=(),
                          hold_count=0, pressure=(pressure,) * dimensions)
        for too_many in ({"blocked_lanes": (lane,) * (lanes + 1)}, {"pressure": (pressure,) * (dimensions + 1)}):
            with self.subTest(too_many=sorted(too_many)), self.assertRaises(ValueError):
                NoProgressSummary(**{"reason_code": "capacity_holds_exhausted", "blocked_lanes": (),
                                     "holds": (), "hold_count": 0, "pressure": (), **too_many})

        # A reported dimension that is not a closed token never reaches a payload.
        class _OddHold(_Backend):
            def run_local(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
                raise StageCapacityBlocked(_SECRET_MESSAGE, dimensions=(_SECRET_MESSAGE, "decode_input_bytes"))

        backend = _OddHold(recoverable=(_work("odd", "materializing", 5),))
        result = _run(StagedParseCoordinator(backend=backend, limits=_limits()))
        event = next(item for item in result.diagnostics if isinstance(item, CapacityHoldEvent))
        self.assertEqual(event.dimensions, ("unclassified", "decode_input_bytes"))
        self.assertIn("odd:local:stage_capacity_hold:unclassified,decode_input_bytes", result.errors)
        encoded = json.dumps([item.to_payload() for item in result.diagnostics], ensure_ascii=False)
        for secret in _SECRETS:
            self.assertNotIn(secret, encoded)


if __name__ == "__main__":
    unittest.main()
