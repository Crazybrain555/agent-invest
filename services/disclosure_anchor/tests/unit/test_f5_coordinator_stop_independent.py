"""Independent F5 acceptance: the coordinator latches the first public cause.

Pro §5.2/§4 and §7 group (1), exercised on the real ``StagedParseCoordinator``
with the existing fake backend: a stage fault is latched in the stage thread
before its Future is done and before the drain waits for other stages; a fault
racing an operator stop stays a public stop; a pure operator drain, retries,
waits and recovery deferrals never latch; another plane's stop halts dispatch
and revokes running stages with ``public_stop`` provenance; synchronous
controller and process-ownership failures latch before the drain. Causes carry
closed identities only, never exception or model text.
"""

from __future__ import annotations

from dataclasses import replace
import json
import threading
import time
import unittest

from disclosure_anchor.application.contracts.semantic_routes import SemanticProviderAttempt
from disclosure_anchor.application.ports.semantic_routes import SemanticRouteAdjudicatorError
from disclosure_anchor.application.ports.worker_stop_control import PublicStopCause
from disclosure_anchor.application.services.staged_execution_guard import StageLeaseGuard, StageLeaseLost
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorResult,
    CoordinatorSnapshot,
    CoordinatorTerminal,
    CoordinatorWork,
    ResourceCreditVector,
    StagedParseCoordinator,
)
from disclosure_anchor.application.services.worker_stop_latch import InProcessWorkerStopLatch
from tests.unit.test_semantic_adjudication import _identity
from tests.unit.test_staged_parse_coordinator import _Backend, _limits, _work


_SECRETS = ("sk-live-F5SECRET", "/Volumes/AgentSSD/secret/path", "业绩预告正文 prompt body")
_SECRET_MESSAGE = "provider said: " + " | ".join(_SECRETS)


class _Observed:
    """A real latch whose first-trip hook records where and when it ran."""

    def __init__(self, backend: _Backend) -> None:
        self.backend = backend
        self.trip_thread: str | None = None
        self.calls_at_trip: int | None = None
        self.remote_running_at_trip: bool | None = None
        self.remote_finished = threading.Event()
        self.latch = InProcessWorkerStopLatch(on_first_trip=self._hook)

    def _hook(self, _cause: PublicStopCause) -> None:
        self.trip_thread = threading.current_thread().name
        self.calls_at_trip = len(self.backend.calls)
        self.remote_running_at_trip = (
            self.backend.remote_entered.is_set() and not self.remote_finished.is_set()
        )


class _StageBackend(_Backend):
    """The shared fake with per-attempt stage faults and recorded guards."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.guards: dict[str, StageLeaseGuard] = {}
        self.local_error: dict[str, BaseException] = {}
        self.commit_error: dict[str, BaseException] = {}
        self.before_commit_error = lambda: None
        self.observed: _Observed | None = None
        self.fault_after_remote_entered = False
        self.calls_when_stop_observed: int | None = None

    def progress(self, snapshot: CoordinatorSnapshot) -> None:
        """Mark the first snapshot in which the controller shows a public stop."""

        if (
            self.calls_when_stop_observed is None
            and (snapshot.blocked_reason or "").startswith("public_stop:")
        ):
            self.calls_when_stop_observed = len(self.calls)
            self.on_stop_observed()

    def on_stop_observed(self) -> None:
        return None

    def admissions_after_stop_observed(self) -> list[str]:
        assert self.calls_when_stop_observed is not None, "no public_stop snapshot was emitted"
        return [call for call in self.calls[self.calls_when_stop_observed:] if call.startswith("admit:")]

    def run_remote(self, work: CoordinatorWork, *, credit_allowance: ResourceCreditVector,
                   stage_guard: StageLeaseGuard) -> CoordinatorWork:
        self.guards[work.attempt_id] = stage_guard
        try:
            return super().run_remote(work, credit_allowance=credit_allowance, stage_guard=stage_guard)
        finally:
            if self.observed is not None:
                self.observed.remote_finished.set()

    def run_local(self, work: CoordinatorWork, *, credit_allowance: ResourceCreditVector,
                  stage_guard: StageLeaseGuard) -> CoordinatorWork:
        self.guards[work.attempt_id] = stage_guard
        error = self.local_error.get(work.attempt_id)
        if error is not None:
            stage_guard.checkpoint()
            if self.fault_after_remote_entered:
                self.remote_entered.wait(timeout=5)
            self.calls.append(f"local-fault:{work.attempt_id}")
            raise error
        return super().run_local(work, credit_allowance=credit_allowance, stage_guard=stage_guard)

    def commit(self, work: CoordinatorWork, *, credit_allowance: ResourceCreditVector,
               stage_guard: StageLeaseGuard) -> CoordinatorWork:
        self.guards[work.attempt_id] = stage_guard
        error = self.commit_error.get(work.attempt_id)
        if error is not None:
            stage_guard.checkpoint()
            self.before_commit_error()
            self.calls.append(f"commit-fault:{work.attempt_id}")
            raise error
        return super().commit(work, credit_allowance=credit_allowance, stage_guard=stage_guard)


def _forbidden(message: str = _SECRET_MESSAGE) -> SemanticRouteAdjudicatorError:
    return SemanticRouteAdjudicatorError(
        message,
        reason_code="forbidden_tool_call",
        retryable=False,
        attempts=(
            SemanticProviderAttempt(
                ordinal=1,
                provider=_identity("f5-primary"),
                outcome="failed_closed",
                reason_code="forbidden_tool_call",
                cache_key="sha256:" + "c" * 64,
            ),
        ),
    )


def _payload_text(cause: PublicStopCause) -> str:
    return json.dumps(cause.to_payload(), ensure_ascii=False, sort_keys=True)


def _run(coordinator: StagedParseCoordinator, **kwargs: object) -> CoordinatorResult:
    box: list[CoordinatorResult] = []
    errors: list[BaseException] = []

    def target() -> None:
        try:
            box.append(coordinator.run(**kwargs))  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=target, name="f5-controller")
    thread.start()
    thread.join(timeout=10)
    if thread.is_alive():
        raise AssertionError("coordinator did not drain within 10s")
    if errors:
        raise errors[0]
    return box[0]


class StageFaultLatchTests(unittest.TestCase):
    def test_stage_fault_latches_in_its_stage_thread_while_another_stage_still_runs(self) -> None:
        backend = _StageBackend(recoverable=(
            _work("attempt-a", "submitted", 2),
            _work("attempt-b", "materializing", 5),
        ))
        backend.block_remote = True
        backend.fault_after_remote_entered = True
        backend.local_error["attempt-b"] = RuntimeError(_SECRET_MESSAGE)
        observed = _Observed(backend)
        backend.observed = observed

        result = _run(StagedParseCoordinator(backend=backend, limits=_limits(), stop_control=observed.latch,
                                             progress=backend.progress))

        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertEqual(result.termination_kind, "public_stop")
        cause = result.stop_cause
        assert cause is not None
        self.assertIs(cause, observed.latch.first_cause())
        self.assertEqual(
            (cause.kind, cause.reason_code, cause.origin, cause.attempt_id, cause.lane,
             cause.state_at_dispatch, cause.lifecycle_version_at_dispatch, cause.exception_class),
            ("stage_fault", "local_unexpected_failure", "stage_call", "attempt-b", "local",
             "materializing", 5, "builtins.RuntimeError"),
        )
        self.assertRegex(cause.exception_fingerprint or "", r"^sha256:[0-9a-f]{64}$")
        for secret in _SECRETS:
            self.assertNotIn(secret, _payload_text(cause))
        # Latched in the failing stage's own thread, before its Future was
        # done and while the other stage was still running.
        self.assertNotIn(observed.trip_thread, ("f5-controller", "MainThread"))
        self.assertTrue(observed.remote_running_at_trip)
        self.assertEqual(backend.guards["attempt-a"].revocation_provenance, "public_stop")
        # Nothing destroys or settles the faulted attempt, and admission closes.
        for call in ("commit:attempt-b", "cleanup:attempt-b", "ack:attempt-b"):
            self.assertNotIn(call, backend.calls)
        self.assertEqual(backend.admissions_after_stop_observed(), [])
        self.assertGreaterEqual(result.credits_in_use.documents, 2, "held credits are not released")

    def test_retry_wait_and_deferred_recovery_are_retry_neutral(self) -> None:
        backend = _StageBackend(
            recoverable=(_work("attempt-d", "submitted", 2),),
            new=(_work("attempt-n", "prepared"),),
        )
        backend.retry_remote_once = True
        backend.wait_remote_by_attempt = {"attempt-n": 2, "attempt-d": 1}
        backend.defer_claim_ids.add("attempt-d")
        original_claim = backend.claim_recovery

        def claim_once_deferred(candidate):  # type: ignore[no-untyped-def]
            try:
                return original_claim(candidate)
            finally:
                backend.defer_claim_ids.discard(candidate.attempt_id)

        backend.claim_recovery = claim_once_deferred  # type: ignore[method-assign]
        latch = InProcessWorkerStopLatch()

        result = _run(StagedParseCoordinator(backend=backend, limits=_limits(), stop_control=latch))

        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT, result.errors)
        self.assertEqual(dict(result.final_states), {"attempt-d": "acked", "attempt-n": "acked"})
        self.assertEqual((result.stop_cause, result.termination_kind), (None, "quiescent"))
        self.assertFalse(latch.is_tripped())
        self.assertGreaterEqual(backend.claim_attempts["attempt-d"], 2)

    def test_legacy_or_ownership_revocation_is_a_fault_but_drain_provenance_is_not(self) -> None:
        cases = (
            ("unspecified", True, ("stage_fault", "stage_lease_lost")),
            ("ownership_lost", True, ("ownership_lost", "in_flight_claim_lost")),
            ("deadline_exhausted", True, ("deadline_exhausted", "bounded_stage_deadline_exceeded")),
            ("operator_cancel", False, None),
            ("public_stop", False, None),
        )
        for provenance, faults, expected in cases:
            with self.subTest(provenance=provenance):
                backend = _StageBackend(recoverable=(_work("attempt-l", "materializing", 5),))
                backend.local_error["attempt-l"] = StageLeaseLost(
                    "bounded stage lease expired", provenance=provenance)  # type: ignore[arg-type]
                latch = InProcessWorkerStopLatch()
                result = _run(StagedParseCoordinator(backend=backend, limits=_limits(), stop_control=latch))
                self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
                self.assertEqual(latch.is_tripped(), faults)
                if expected is None:
                    self.assertIsNone(result.stop_cause)
                    self.assertEqual(result.termination_kind, "circuit")
                else:
                    assert result.stop_cause is not None
                    self.assertEqual((result.stop_cause.kind, result.stop_cause.reason_code), expected)
                    self.assertEqual(result.termination_kind, "public_stop")


    def test_a_fault_whose_future_is_reconciled_from_durable_state_is_still_latched(self) -> None:
        # The stage's transition is durable (its response raced a failed renewal),
        # so the controller consumes the reloaded projection and discards the
        # Future's exception. The fault must already be latched in the stage.
        initial = replace(_work("attempt-r", "submitted", 3), lease_expires_monotonic=time.monotonic() + 0.01)
        backend = _StageBackend(recoverable=(initial,))
        backend.block_remote = True
        backend.claim_lease_seconds_override = 0.01
        backend.renew_lease_seconds_override = 0.6
        backend.fail_renew_after = 2
        backend.reload_result = replace(_work("attempt-r", "remote_terminal", 4), claim_generation=2,
                                        claim_owner_identity="worker-boot-1",
                                        lease_expires_monotonic=time.monotonic() + 1)
        latch = InProcessWorkerStopLatch()

        def release_after_reload() -> None:
            deadline = time.monotonic() + 5
            while "reload:attempt-r" not in backend.calls and time.monotonic() < deadline:
                time.sleep(0.001)
            backend.fail_remote = True
            backend.remote_release.set()

        releaser = threading.Thread(target=release_after_reload)
        releaser.start()
        result = _run(StagedParseCoordinator(
            backend=backend, stop_control=latch,
            limits=_limits(claim_lease_seconds=1, claim_renew_margin_seconds=0.2, max_stage_step_seconds=0.3),
        ))
        releaser.join(timeout=5)
        self.assertIn("reload:attempt-r", backend.calls)
        cause = latch.first_cause()
        assert cause is not None, "the discarded Future's fault was never latched"
        self.assertEqual((cause.kind, cause.reason_code, cause.attempt_id, cause.lane),
                         ("stage_fault", "remote_unexpected_failure", "attempt-r", "remote"))
        self.assertEqual((result.stop_cause, result.termination_kind), (cause, "public_stop"))


class OperatorStopTests(unittest.TestCase):
    def test_pure_operator_stop_during_the_recovery_scan_is_an_operator_drain(self) -> None:
        # The production shape of the user-noted regression: TERM while the
        # startup recovery barrier waits behind another owner's live lease.
        backend = _StageBackend(recoverable=(_work("attempt-held", "submitted", 2),))
        backend.defer_claim_ids.add("attempt-held")
        latch = InProcessWorkerStopLatch()

        def stop_requested() -> bool:
            return backend.claim_attempts.get("attempt-held", 0) >= 1

        result = _run(StagedParseCoordinator(backend=backend, limits=_limits(), stop_control=latch),
                      stop_requested=stop_requested)

        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertEqual((result.stop_cause, result.termination_kind), (None, "operator_drain"))
        self.assertEqual(result.errors, ())
        self.assertFalse(latch.is_tripped())
        self.assertFalse(any(call.startswith(("remote:", "cleanup:", "ack:")) for call in backend.calls))

    def test_operator_stop_keeps_driving_accepted_remote_work_to_its_ack(self) -> None:
        backend = _StageBackend(recoverable=(_work("attempt-w", "submitted", 2),))
        backend.wait_remote_by_attempt = {"attempt-w": 3}
        latch = InProcessWorkerStopLatch()
        result = _run(StagedParseCoordinator(backend=backend, limits=_limits(), stop_control=latch),
                      stop_requested=lambda: backend.remote_calls >= 1)
        self.assertEqual((result.terminal, result.termination_kind),
                         (CoordinatorTerminal.QUIESCENT, "quiescent"))
        self.assertEqual(dict(result.final_states), {"attempt-w": "acked"})
        self.assertFalse(latch.is_tripped())

    def test_fault_racing_an_operator_stop_is_still_the_first_public_stop(self) -> None:
        backend = _StageBackend(recoverable=(_work("attempt-f", "local_materialized", 6),))
        requested = threading.Event()
        backend.before_commit_error = requested.set
        backend.commit_error["attempt-f"] = _forbidden()
        latch = InProcessWorkerStopLatch()
        snapshots: list[CoordinatorSnapshot] = []

        result = _run(
            StagedParseCoordinator(backend=backend, limits=_limits(), stop_control=latch,
                                   progress=snapshots.append),
            stop_requested=requested.is_set,
        )

        self.assertEqual(result.termination_kind, "public_stop")
        cause = result.stop_cause
        assert cause is not None
        self.assertEqual((cause.kind, cause.reason_code, cause.attempt_id, cause.lane),
                         ("semantic_failed_closed", "forbidden_tool_call", "attempt-f", "commit"))
        self.assertEqual(len(cause.provider_attempts), 1)
        attempt = cause.provider_attempts[0]
        self.assertEqual((attempt.outcome, attempt.reason_code, attempt.provider_id, attempt.cache_key),
                         ("failed_closed", "forbidden_tool_call", "f5-primary", "sha256:" + "c" * 64))
        for secret in _SECRETS:
            self.assertNotIn(secret, _payload_text(cause))
        final = snapshots[-1]
        self.assertTrue(final.circuit_open)
        self.assertEqual(final.blocked_reason, "public_stop:forbidden_tool_call",
                         "a concurrent stop request never relabels the fault as a drain")

    def test_semantic_cancellation_is_retry_neutral_and_never_latches(self) -> None:
        cancelled = SemanticRouteAdjudicatorError(
            "cancelled by shutdown", reason_code="cancelled", retryable=True)
        with self.subTest(case="during an operator stop"):
            backend = _StageBackend(recoverable=(_work("attempt-x", "local_materialized", 6),))
            requested = threading.Event()
            backend.before_commit_error = requested.set
            backend.commit_error["attempt-x"] = cancelled
            latch = InProcessWorkerStopLatch()
            result = _run(StagedParseCoordinator(backend=backend, limits=_limits(), stop_control=latch),
                          stop_requested=requested.is_set)
            self.assertFalse(latch.is_tripped())
            self.assertEqual((result.stop_cause, result.termination_kind), (None, "operator_drain"))
            self.assertFalse(any(call.startswith(("cleanup:", "ack:")) for call in backend.calls))
        with self.subTest(case="without any stop request"):
            backend = _StageBackend(recoverable=(_work("attempt-y", "local_materialized", 6),))
            backend.commit_error["attempt-y"] = cancelled
            latch = InProcessWorkerStopLatch()
            result = _run(StagedParseCoordinator(backend=backend, limits=_limits(), stop_control=latch))
            self.assertFalse(latch.is_tripped())
            self.assertEqual((result.stop_cause, result.termination_kind), (None, "circuit"),
                             "an unexplained cancellation is never reported as an operator drain")


class OtherPlaneAndControllerTests(unittest.TestCase):
    def test_another_planes_stop_halts_dispatch_and_revokes_running_stages(self) -> None:
        backend = _StageBackend(recoverable=(_work("attempt-a", "submitted", 2),))
        backend.block_remote = True
        # New work becomes available only once the controller shows the stop.
        backend.on_stop_observed = lambda: backend.new.append(_work("attempt-c", "prepared"))  # type: ignore[method-assign]
        latch = InProcessWorkerStopLatch()
        maintenance = PublicStopCause(kind="maintenance_fatal", reason_code="maintenance_loop_failed",
                                      origin="maintenance")

        def trip_when_running() -> None:
            if backend.remote_entered.wait(timeout=5):
                latch.trip(maintenance)

        tripper = threading.Thread(target=trip_when_running)
        tripper.start()
        result = _run(StagedParseCoordinator(backend=backend, limits=_limits(), stop_control=latch,
                                             progress=backend.progress))
        tripper.join(timeout=5)

        self.assertEqual((result.terminal, result.termination_kind),
                         (CoordinatorTerminal.STUCK_OPEN_CIRCUIT, "public_stop"))
        self.assertIs(result.stop_cause, maintenance)
        self.assertEqual(backend.guards["attempt-a"].revocation_provenance, "public_stop")
        self.assertFalse(backend.remote_release.is_set(), "the running stage drained by revocation")
        self.assertEqual(backend.admissions_after_stop_observed(), [])
        self.assertEqual([work.attempt_id for work in backend.new], ["attempt-c"])

    def test_controller_and_process_ownership_failures_latch_before_the_drain(self) -> None:
        for failure in ("admission", "process_guard"):
            with self.subTest(failure=failure):
                backend = _StageBackend(recoverable=(_work("attempt-a", "submitted", 2),))
                backend.block_remote = True
                observed = _Observed(backend)
                backend.observed = observed
                boom = RuntimeError(_SECRET_MESSAGE)
                kwargs: dict[str, object] = {}
                if failure == "admission":
                    original_admit = backend.admit_new

                    def admit_new(*, limit, available_credits):  # type: ignore[no-untyped-def]
                        if backend.remote_entered.is_set():
                            raise boom
                        return original_admit(limit=limit, available_credits=available_credits)

                    backend.admit_new = admit_new  # type: ignore[method-assign]
                    expected = ("coordinator_fault", "controller_unexpected_failure", "coordinator")
                else:
                    def process_guard() -> None:
                        if backend.remote_entered.is_set():
                            raise boom

                    kwargs["process_guard"] = process_guard
                    expected = ("ownership_lost", "process_guard_failed", "coordinator")
                coordinator = StagedParseCoordinator(
                    backend=backend, limits=_limits(), stop_control=observed.latch, **kwargs)  # type: ignore[arg-type]
                started = time.monotonic()
                with self.assertRaises(RuntimeError) as raised:
                    _run(coordinator)
                self.assertIs(raised.exception, boom)
                self.assertLess(time.monotonic() - started, 5)
                cause = observed.latch.first_cause()
                assert cause is not None
                self.assertEqual((cause.kind, cause.reason_code, cause.origin), expected)
                self.assertTrue(observed.remote_running_at_trip, "latched before the drain waited")
                self.assertEqual(backend.guards["attempt-a"].revocation_provenance, "public_stop")
                for secret in _SECRETS:
                    self.assertNotIn(secret, _payload_text(cause))


if __name__ == "__main__":
    unittest.main()
