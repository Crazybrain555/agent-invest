"""Independent fatal-pressure effect boundary and real coordinator drain tests."""

import threading
import unittest
from unittest import mock

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.http_remote_v4 import (
    MinerUHttpRemoteV4,
)
from disclosure_anchor.application.ports.mineru_stream_pressure import (
    StreamSubmissionDeferred,
)
from disclosure_anchor.application.ports.remote_provider_v4 import RemoteProviderV4Port
from disclosure_anchor.application.services.mineru_stream_policy import (
    MineruStreamPolicy,
    StreamAdmissionControl,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorTerminal,
    ResourceCreditVector,
    StageAdmissionDeferred,
    StageWaiting,
    StagedParseCoordinator,
)
from tests.unit import test_mineru_http_remote_v4 as wire_fixtures
from tests.unit import test_staged_coordinator_backend_v4 as durable_fixtures
from tests.unit.test_mineru_stream_policy import Pressure, config, sample
from tests.unit.test_staged_parse_coordinator import _Backend, _Clock, _limits, _work


REASON = "independent_pressure_owner_changed"


def control(pressure, *, runtime=None):
    settings = config(
        qualified_max=2,
        **({} if runtime is None else {"runtime_identity_sha256": runtime}),
    )
    return StreamAdmissionControl(
        MineruStreamPolicy(settings), pressure, monotonic=lambda: 10
    )


class FatalPressureHTTPTests(unittest.TestCase):
    def setUp(self):
        self.wire = wire_fixtures.MinerUHttpRemoteV4Tests()
        self.wire.setUp()
        self.addCleanup(self.wire.tearDown)
        self.command = self.wire._submission_command()
        self.runtime = self.command.parser_options.runtime_bundle_identity_sha256
        self.pressure = Pressure(
            sample(1, 10, runtime_identity_sha256=self.runtime, unsafe_reason=REASON)
        )
        self.control = control(self.pressure, runtime=self.runtime)
        self.requests = []

    def provider(self, handler):
        return MinerUHttpRemoteV4(
            transport=httpx.MockTransport(handler),
            token_factory=lambda n: b"s" * n,
            wall_clock=lambda: 10000.0,
            request_timeout_seconds=30,
            submission_guard=self.control,
        )

    def test_fatal_guard_preserves_404_proof_and_throws_before_any_POST(self):
        def handler(request):
            self.requests.append(request.method + " " + request.url.path)
            self.assertEqual(request.method, "GET")
            return httpx.Response(404, json={"detail": "Task not found"})

        before = self.wire.snapshot.read_bytes()
        with (
            self.provider(handler) as provider,
            self.assertRaises(StreamSubmissionDeferred) as caught,
        ):
            provider.reconcile_or_submit(self.command)
        self.assertTrue(caught.exception.unsafe)
        self.assertIn(REASON, str(caught.exception))
        self.assertEqual(self.requests, ["GET /tasks/by-idempotency/" + self.wire.key])
        self.assertEqual(self.command.snapshot_source.opens, 1)
        self.assertEqual(self.wire.snapshot.read_bytes(), before)

    def test_fatal_pressure_still_reconciles_accepted_polls_leases_and_ACKs(self):
        def handler(request):
            path = request.url.path
            self.requests.append(request.method + " " + path)
            if "/by-idempotency/" in path:
                return httpx.Response(200, json=self.wire._task_payload("pending"))
            if path == "/tasks/task-1":
                return httpx.Response(
                    200, json=self.wire._task_payload("completed", artifact_bytes=8)
                )
            if path.endswith("/lease"):
                return httpx.Response(200, json=self.wire._lease_payload(10120.0))
            self.assertEqual(path, "/tasks/task-1/ack")
            return httpx.Response(
                200,
                content=b'{"schema":"mineru-task-protocol.v2","task_id":"task-1","status":"consumed"}',
            )

        with mock.patch.object(
            self.control,
            "assert_submission_allowed",
            wraps=self.control.assert_submission_allowed,
        ) as guard:
            with self.provider(handler) as provider:
                accepted = provider.reconcile_or_submit(self.command)
                provider.poll_once(self.wire._poll_command(accepted))
                ack, capability = self.wire._ack_evidence()
                provider.acknowledge(
                    command=ack,
                    provider_capability=capability,
                    step_guard=self.wire.guard,
                    before_ack_post=lambda: self.requests.append("current-claim"),
                )
            guard.assert_not_called()
        self.assertEqual(self.command.snapshot_source.opens, 0)
        self.assertEqual(
            self.requests,
            [
                "GET /tasks/by-idempotency/" + self.wire.key,
                "GET /tasks/task-1",
                "POST /tasks/task-1/lease",
                "current-claim",
                "POST /tasks/task-1/ack",
            ],
        )

    def test_pressure_fatal_after_POST_does_not_turn_ambiguous_acceptance_into_absence(
        self,
    ):
        self.pressure.value = sample(1, 10, runtime_identity_sha256=self.runtime)

        def handler(request):
            self.requests.append(request.method)
            if request.method == "POST":
                self.assertIn(self.wire.source, request.read())
                self.pressure.value = sample(
                    2, 10, runtime_identity_sha256=self.runtime, unsafe_reason=REASON
                )
                raise httpx.ReadTimeout("accepted response was lost", request=request)
            if len(self.requests) == 1:
                return httpx.Response(404, json={"detail": "Task not found"})
            return httpx.Response(200, json=self.wire._task_payload("processing"))

        with self.provider(handler) as provider:
            accepted = provider.reconcile_or_submit(self.command)
        self.assertEqual(accepted.receipt.remote_task_identity, "task-1")
        self.assertEqual(self.requests, ["GET", "POST", "GET"])


class FatalPressureBackendTests(unittest.TestCase):
    def test_only_fatal_proved_absence_selects_park_without_durable_successor(self):
        for unsafe in (False, True):
            authority = durable_fixtures._authority("reconciling")
            work = durable_fixtures._work(authority)
            deferred = StreamSubmissionDeferred(REASON, unsafe=unsafe)
            remote = mock.Mock(spec=RemoteProviderV4Port)
            remote.reconcile_or_submit.side_effect = deferred
            backend, persistence, inputs, _ = durable_fixtures._backend(
                authority, remote=remote
            )
            inputs.submission_command.return_value = mock.sentinel.command
            with self.subTest(unsafe=unsafe), self.assertRaises(StageWaiting) as caught:
                backend.run_remote(
                    work,
                    credit_allowance=ResourceCreditVector(
                        provider_tasks=1, ack_items=1
                    ),
                    stage_guard=durable_fixtures._guard(),
                )
            self.assertIs(
                type(caught.exception),
                StageAdmissionDeferred if unsafe else StageWaiting,
            )
            self.assertIs(caught.exception.__cause__, deferred)
            if unsafe:
                self.assertIn(REASON, str(caught.exception))
            self.assertEqual(persistence.appends, [])
            self.assertIs(persistence.authority, authority)
            self.assertEqual(persistence.reload_claim(work), work)
            self.assertEqual(
                authority.checkpoint.held_resource_credit.provider_tasks, 0
            )
            self.assertEqual(authority.checkpoint.held_resource_credit.ack_items, 0)
            self.assertEqual(
                tuple(e.kind for e in authority.evidence),
                ("preparation_intent", "snapshot_receipt", "submission_intent"),
            )
            backend._secret_cipher.seal.assert_not_called()
            remote.reconcile_or_submit.assert_called_once_with(mock.sentinel.command)


class _ProvedAbsentBackend(_Backend):
    def __init__(self, *, absent="b-absent", fatal=True, **kwargs):
        super().__init__(**kwargs)
        self.absent, self.fatal = absent, fatal
        self.absence_checks = 0
        self.parked_work = None

    def run_remote(self, work, *, credit_allowance, stage_guard):
        if work.attempt_id == self.absent and work.state == "reconciling":
            self.absence_checks += 1
            if self.fatal or self.absence_checks == 1:
                stage_guard.checkpoint()
                self.parked_work = work
                outcome = StageAdmissionDeferred if self.fatal else StageWaiting
                raise outcome(REASON, retry_after_seconds=0.001)
        return super().run_remote(
            work, credit_allowance=credit_allowance, stage_guard=stage_guard
        )


class FatalPressureCoordinatorTests(unittest.TestCase):
    def run_bounded(self, coordinator):
        stop = threading.Event()
        result, errors = [], []

        def run():
            try:
                result.append(coordinator.run(stop_requested=stop.is_set))
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=run, name="independent-fatal-drain-test")
        thread.start()
        try:
            thread.join(2)
            finished_without_test_stop = not thread.is_alive()
        finally:
            stop.set()
            thread.join(3)
        self.assertFalse(
            thread.is_alive(), "local test coordinator did not close its owners"
        )
        self.assertTrue(
            finished_without_test_stop, "fatal pressure never reached drained terminal"
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(result), 1)
        return result[0]

    def test_fatal_retains_absent_and_prepared_credits_while_all_accepted_tails_drain(
        self,
    ):
        prepared = _work("a-prepared", "prepared")
        absent = _work("b-absent", "reconciling", 1)
        backend = _ProvedAbsentBackend(
            recoverable=(
                prepared,
                absent,
                _work("c-poll", "submitted", 2),
                _work("d-result", "remote_terminal", 3),
                _work("e-ack", "ack_pending", 7),
            ),
            new=(_work("new-unadmitted", "prepared"),),
        )
        backend.wait_remote_by_attempt["c-poll"] = 2
        snapshots = []
        result = self.run_bounded(
            StagedParseCoordinator(
                backend=backend,
                limits=_limits(),
                progress=snapshots.append,
                stream_control=control(Pressure(sample(1, 10, unsafe_reason=REASON))),
            )
        )
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertEqual(result.completed, 3)
        self.assertEqual(result.admitted, 0)
        self.assertEqual(result.credits_in_use, prepared.credits + absent.credits)
        self.assertEqual(
            backend.absence_checks, 1, "a proved-absent fatal intent must not retry"
        )
        self.assertEqual(backend.parked_work.state, "reconciling")
        self.assertEqual(
            backend.parked_work.lifecycle_version, absent.lifecycle_version
        )
        self.assertEqual(len(backend.new), 1)
        self.assertNotIn("preflight:a-prepared", backend.calls)
        self.assertFalse(any(call.startswith("admit:") for call in backend.calls))
        for name in ("c-poll", "d-result", "e-ack"):
            self.assertEqual(backend.calls.count(f"ack:{name}:ack_pending"), 1)
        self.assertGreaterEqual(backend.remote_calls_by_attempt["c-poll"], 3)
        self.assertIn("local_prepare:d-result", backend.calls)
        self.assertIn("commit:d-result", backend.calls)
        self.assertEqual(
            dict(result.final_states),
            {name: "acked" for name in ("c-poll", "d-result", "e-ack")},
        )
        self.assertTrue(any(REASON in message for message in result.errors))
        self.assertFalse(any("retry budget" in message for message in result.errors))
        self.assertTrue(
            all(not snap.circuit_open for snap in snapshots if snap.completed < 3)
        )
        self.assertEqual(snapshots[-1].blocked_reason, "stream_pressure_closed")

    def test_normal_deferral_retries_original_intent_and_can_finish_without_open_circuit(
        self,
    ):
        backend = _ProvedAbsentBackend(
            fatal=False, recoverable=(_work("b-absent", "reconciling", 1),)
        )
        result = self.run_bounded(
            StagedParseCoordinator(
                backend=backend,
                limits=_limits(),
                stream_control=control(Pressure(sample(1, 10))),
            )
        )
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(result.completed, 1)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.credits_in_use, ResourceCreditVector())
        self.assertEqual(backend.absence_checks, 2)

    def test_reconciling_may_already_be_accepted_and_must_recover_under_fatal_pressure(
        self,
    ):
        backend = _Backend(
            recoverable=(_work("a-ambiguous-accepted", "reconciling", 1),)
        )
        result = self.run_bounded(
            StagedParseCoordinator(
                backend=backend,
                limits=_limits(),
                stream_control=control(Pressure(sample(1, 10, unsafe_reason=REASON))),
            )
        )
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertEqual(result.completed, 1)
        self.assertEqual(result.credits_in_use, ResourceCreditVector())
        self.assertIn("remote:a-ambiguous-accepted:reconciling", backend.calls)
        self.assertIn("remote:a-ambiguous-accepted:submitted", backend.calls)
        self.assertEqual(backend.calls.count("ack:a-ambiguous-accepted:ack_pending"), 1)

    def test_parked_intent_claim_is_renewed_until_long_accepted_drain_finishes(self):
        clock = _Clock(1000.0)
        absent = _work("b-absent", "reconciling", 1)
        backend = _ProvedAbsentBackend(
            recoverable=(absent, _work("c-long-poll", "submitted", 2))
        )
        backend.clock = clock
        backend.advance_clock = clock.advance
        backend.claim_lease_seconds_override = 10
        backend.enforce_lease_expiry = True
        backend.advance_per_remote = 1
        backend.wait_remote_by_attempt["c-long-poll"] = 12
        result = self.run_bounded(
            StagedParseCoordinator(
                backend=backend,
                limits=_limits(
                    claim_lease_seconds=10,
                    claim_renew_margin_seconds=1,
                    max_stage_step_seconds=3,
                ),
                monotonic=clock,
                progress=lambda _: clock.advance(0.01),
                stream_control=control(Pressure(sample(1, 10, unsafe_reason=REASON))),
            )
        )
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertEqual(result.completed, 1)
        self.assertEqual(result.credits_in_use, absent.credits)
        self.assertGreater(clock(), backend.parked_work.lease_expires_monotonic)
        self.assertTrue(any(name == "b-absent" for name, _ in backend.renew_times))
        self.assertEqual(backend.absence_checks, 1)
        self.assertEqual(backend.calls.count("ack:c-long-poll:ack_pending"), 1)
        self.assertFalse(any("claim lost" in error for error in result.errors))


class KnownLowPressurePolicyTests(unittest.TestCase):
    def test_known_low_memory_pauses_even_if_other_lane_is_unknown_and_only_fresh_clear_recovers(
        self,
    ):
        cases = (
            dict(
                gpu_free_bytes=0,
                host_available_bytes=None,
                unknown_reason="api_unavailable",
            ),
            dict(
                gpu_free_bytes=None,
                host_available_bytes=0,
                unknown_reason="gpu_unavailable",
            ),
            dict(gpu_free_bytes=0, unknown_reason="gpu_unavailable_or_stale"),
        )
        for index, values in enumerate(cases):
            policy = MineruStreamPolicy(config(qualified_max=5, recovery_seconds=2))
            self.assertEqual(policy.evaluate(sample(1, 0), now=0).target, 5)
            with self.subTest(values=values):
                paused = policy.evaluate(
                    sample(2, 11 if index < 2 else 0, **values), now=11
                )
                self.assertEqual(paused.target, 0)
                self.assertFalse(paused.unsafe)
                self.assertEqual(paused.reason, "memory_pause")
                self.assertEqual(
                    policy.evaluate(
                        sample(3, 12, unknown_reason="still_missing"), now=12
                    ).target,
                    0,
                )
                self.assertEqual(policy.evaluate(sample(4, 13), now=13).target, 0)
                self.assertEqual(policy.evaluate(sample(5, 15), now=15).target, 1)
                self.assertEqual(policy.evaluate(sample(6, 17), now=17).target, 2)
                for sequence, now, expected in ((7, 19, 3), (8, 21, 4), (9, 23, 5)):
                    self.assertEqual(
                        policy.evaluate(sample(sequence, now), now=now).target,
                        expected,
                    )
