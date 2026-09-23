"""Independent absolute-deadline regressions; no sockets, processes or live inputs."""

from __future__ import annotations

import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch, sentinel

from disclosure_anchor.adapters.runtime import bounded_http
from disclosure_anchor.adapters.runtime import synchronized_telemetry_observer as observer
from disclosure_anchor.adapters.runtime import windows_resident_telemetry as resident
from disclosure_anchor.application.ports.synchronized_telemetry import (
    TelemetrySnapshotDeadline,
    TelemetrySnapshotDeadlineExceeded,
    TelemetrySnapshotTransportUnavailable,
)
from tests.unit.test_r22_measurement_integration_independent import _pull_payload
from tests.unit.test_windows_resident_telemetry import HASHES, OBSERVER_CLOCK, _identity


class _Clock:
    def __init__(self) -> None:
        self.now_ns = 1_000_000_000

    def monotonic_ns(self) -> int:
        return self.now_ns

    def monotonic(self) -> float:
        return self.now_ns / 1_000_000_000


class ResidentTransportDeadlineIndependentTests(unittest.TestCase):
    def _sampler(self, clock: _Clock):
        sampler = resident.build_windows_resident_telemetry_sampler({
            "lane": "gpu_fast", "base_url": "http://127.0.0.1:9",
            "path": "/gpu_fast", "maximum_response_bytes": 65536,
            "maximum_sample_age_ms": 1000, "nominal_interval_ms": 250,
            "collector_identity_sha256": HASHES[0],
            "observer_clock_domain_identity_sha256": OBSERVER_CLOCK,
            "expected_identity": _identity(),
            "pull_protocol": "mineru.windows-resident-pull.v1",
        })
        self.addCleanup(sampler.close)
        # Keep the actual HTTP budget/dispatch implementation, changing only its clock.
        sampler._client._clock = clock.monotonic
        connection = Mock()
        connection.sock = None
        dispatches = []

        def dispatch(method, path, **_kwargs):
            self.assertEqual(method, "GET")
            cursor, nonce = path.split("/after/", 1)[1].split("/request/", 1)
            dispatches.append((clock.now_ns, connection.timeout, nonce))
            payload = _pull_payload(after=int(cursor), nonce=nonce)
            response = Mock(status=200, will_close=False)
            response.getheader.side_effect = (
                lambda name: str(len(payload)) if name == "Content-Length" else None
            )

            def read(amount):
                self.assertEqual(amount, len(payload))
                clock.now_ns += 100_000
                return payload

            response.read.side_effect = read
            connection.getresponse.return_value = response

        connection.request.side_effect = dispatch
        return sampler, connection, dispatches

    def _sample_after_nonce_delay(self, *, delay_ns: int):
        clock = _Clock()
        deadline = TelemetrySnapshotDeadline(clock.now_ns + 250_000_000)
        sampler, connection, dispatches = self._sampler(clock)
        nonce = "1" * 32

        def make_nonce(length):
            self.assertEqual(length, 16)
            clock.now_ns += delay_ns
            return nonce

        returned = error = None
        with (
            patch.object(resident, "time", clock),
            patch.object(resident, "secrets", SimpleNamespace(token_hex=make_nonce)),
            patch.object(sampler._client, "_new_connection", return_value=connection),
            # Timer scheduling is a separate test family; this case has only fake-clock I/O.
            patch.object(bounded_http.threading, "Timer"),
        ):
            try:
                returned = sampler.snapshot(deadline=deadline)
            except (TelemetrySnapshotDeadlineExceeded, TelemetrySnapshotTransportUnavailable) as exc:
                error = exc
        return deadline, dispatches, returned, error

    def test_timely_dispatch_keeps_only_the_original_remaining_budget(self) -> None:
        for delay_ns in (0, 240_000_000):
            with self.subTest(nonce_delay_ns=delay_ns):
                deadline, dispatches, snapshot, error = self._sample_after_nonce_delay(
                    delay_ns=delay_ns,
                )
                self.assertIsNone(error)
                self.assertIsNotNone(snapshot)
                self.assertEqual(len(dispatches), 1)
                dispatched_ns, operation_timeout, nonce = dispatches[0]
                remaining = (deadline.monotonic_ns - dispatched_ns) / 1_000_000_000
                self.assertGreater(operation_timeout, 0)
                self.assertLessEqual(
                    operation_timeout, remaining + 1e-12,
                    "nonce preparation must consume, not renew, the original deadline",
                )
                self.assertEqual(snapshot.resident_exporter_provenance.request_nonce, nonce)
                self.assertEqual(snapshot.resident_exporter_provenance.wire_sequence, 1)

    def test_expiry_during_nonce_preparation_never_dispatches_or_returns_a_sample(self) -> None:
        for delay_ns in (250_000_000, 260_000_000):
            with self.subTest(nonce_delay_ns=delay_ns):
                _, dispatches, snapshot, error = self._sample_after_nonce_delay(delay_ns=delay_ns)
                self.assertEqual(dispatches, [], "an expired sample must not consume a remote cursor")
                self.assertIsNone(snapshot)
                self.assertIsInstance(
                    error, (TelemetrySnapshotDeadlineExceeded, TelemetrySnapshotTransportUnavailable),
                )

    def _parent(self):
        # Exercise the original parent method without spawning a process. The fake
        # endpoint is alive; only the already-expired/cancelled dispatch boundary varies.
        invocation = object.__new__(observer._ResidentSamplerProcess)
        invocation._connection = connection = Mock()
        invocation._process = Mock()
        invocation._process.is_alive.return_value = True
        invocation._started = True
        connection.poll.return_value = True
        connection.recv.return_value = ("ok", sentinel.snapshot)
        return invocation, connection

    def test_parent_expired_deadline_is_checked_before_sending(self) -> None:
        clock = _Clock()
        for offset in (0, -1):
            with self.subTest(deadline_offset_ns=offset):
                invocation, connection = self._parent()
                with (
                    patch.object(invocation, "_terminate"),
                    self.assertRaises(TelemetrySnapshotDeadlineExceeded),
                ):
                    invocation.snapshot(
                        deadline=TelemetrySnapshotDeadline(clock.now_ns + offset),
                        cancel_event=threading.Event(), monotonic_ns=clock.monotonic_ns,
                    )
                connection.send.assert_not_called()

    def test_parent_cancellation_is_checked_before_sending(self) -> None:
        clock = _Clock()
        invocation, connection = self._parent()
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(invocation, "_terminate"), self.assertRaises(observer._InvocationCancelled):
            invocation.snapshot(
                deadline=TelemetrySnapshotDeadline(clock.now_ns + 250_000_000),
                cancel_event=cancelled, monotonic_ns=clock.monotonic_ns,
            )
        connection.send.assert_not_called()

    def test_parent_timely_completion_still_returns_the_owned_reply(self) -> None:
        clock = _Clock()
        invocation, connection = self._parent()
        deadline = TelemetrySnapshotDeadline(clock.now_ns + 250_000_000)
        with patch.object(invocation, "_terminate") as terminate:
            result = invocation.snapshot(
                deadline=deadline, cancel_event=threading.Event(), monotonic_ns=clock.monotonic_ns,
            )
        self.assertIs(result, sentinel.snapshot)
        connection.send.assert_called_once_with(("snapshot", deadline))
        terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
