"""Independent supervisor acceptance: synthetic local HTTP, no external runtime.

The v4 negative terminal and prefix conservation cases remain in the existing
R23 classes in test_r22_measurement_integration_independent.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import multiprocessing
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch, sentinel

from disclosure_anchor.adapters.runtime import bounded_http
from disclosure_anchor.adapters.runtime import resident_telemetry_owner as owner
from disclosure_anchor.adapters.runtime import synchronized_telemetry_observer as observer
from disclosure_anchor.adapters.runtime import windows_resident_telemetry as resident
from disclosure_anchor.application.ports.synchronized_telemetry import (
    ResidentTelemetryCollectorSpec,
    TelemetrySnapshotDeadline,
    TelemetrySnapshotDeadlineExceeded,
    TelemetrySnapshotTransportUnavailable,
)
from tests.unit.test_r22_measurement_integration_independent import _pull_payload
from tests.unit.test_windows_resident_telemetry import HASHES, OBSERVER_CLOCK, _identity


def _sampler_config(base_url: str) -> dict[str, object]:
    return {
        "lane": "gpu_fast", "base_url": base_url, "path": "/gpu_fast",
        "maximum_response_bytes": 65536, "maximum_sample_age_ms": 1000,
        "nominal_interval_ms": 250, "collector_identity_sha256": HASHES[0],
        "observer_clock_domain_identity_sha256": OBSERVER_CLOCK,
        "expected_identity": _identity(),
        "pull_protocol": "mineru.windows-resident-pull.v1",
    }


def _forbidden_timer(*_args, **_kwargs):
    raise AssertionError("a supervised HTTP request constructed a Timer")


def _spawned_sampler_factory(config: dict[str, object]):
    """The real child entrypoint must arrange supervision; the factory does not."""
    sampler = resident.build_windows_resident_telemetry_sampler(config)
    # This runs only inside the owned spawned child, after imports/initialization.
    # Any request Timer is a visible program failure rather than a timing race.
    bounded_http.threading.Timer = _forbidden_timer
    return sampler


def _response(payload: bytes) -> Mock:
    response = Mock(status=200, will_close=False)
    response.getheader.side_effect = (
        lambda name: str(len(payload)) if name == "Content-Length" else None
    )
    response.read.return_value = payload
    return response


class _PullHandler(BaseHTTPRequestHandler):
    timeout = 2.0

    def do_GET(self) -> None:
        server = self.server
        cursor, nonce = self.path.split("/after/", 1)[1].split("/request/", 1)
        payload = _pull_payload(after=int(cursor), nonce=nonce)
        server.received.set()
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if not server.slow:
                self.wfile.write(payload)
                return
            # Each byte arrives within the socket timeout, while the whole body
            # cannot finish by D. Only the owning parent can bound this lifetime.
            server_bound = time.monotonic() + 2.0
            for value in payload:
                self.wfile.write(bytes((value,)))
                self.wfile.flush()
                if server.stop.wait(0.02) or time.monotonic() >= server_bound:
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            server.finished.set()

    def log_message(self, *_args) -> None:
        return


class ResidentSupervisionIndependentTests(unittest.TestCase):
    def test_supervised_sampler_success_has_no_timer_and_keeps_original_budget(self) -> None:
        now = [1.0]
        sampler = resident.build_windows_resident_telemetry_sampler(
            _sampler_config("http://127.0.0.1:9")
        )
        self.addCleanup(sampler.close)
        sampler._client._clock = lambda: now[0]
        connection = Mock(sock=None)
        operation_timeouts = []

        def dispatch(_method, path, **_kwargs):
            operation_timeouts.append(connection.timeout)
            cursor, nonce = path.split("/after/", 1)[1].split("/request/", 1)
            connection.getresponse.return_value = _response(
                _pull_payload(after=int(cursor), nonce=nonce)
            )

        connection.request.side_effect = dispatch
        sampler.adopt_supervisor_interruption()

        def prepare_nonce(_length):
            now[0] += 0.24
            return "1" * 32

        with (
            patch.object(resident, "time", SimpleNamespace(monotonic_ns=lambda: int(now[0] * 1e9))),
            patch.object(resident.secrets, "token_hex", side_effect=prepare_nonce),
            patch.object(sampler._client, "_new_connection", return_value=connection),
            patch.object(bounded_http.threading, "Timer", side_effect=_forbidden_timer),
        ):
            snapshot = sampler.snapshot(deadline=TelemetrySnapshotDeadline(1_250_000_000))
        self.assertEqual(snapshot.resident_exporter_provenance.wire_sequence, 1)
        self.assertEqual(len(operation_timeouts), 1)
        self.assertGreater(operation_timeouts[0], 0)
        self.assertLessEqual(operation_timeouts[0], 0.010000000001)

    def test_default_direct_sampler_and_owner_close_still_interrupt_a_blocked_read(self) -> None:
        # An operation timeout cannot unblock this synthetic reader. The default
        # Timer must close the connection, including through both compositions.
        for composition in ("direct", "sampler", "owner_close"):
            with self.subTest(composition=composition):
                sampler = resident.build_windows_resident_telemetry_sampler(
                    _sampler_config("http://127.0.0.1:9")
                )
                client = sampler._client
                closed = threading.Event()
                interruption_observed = []
                connection = Mock(sock=None)
                connection.close.side_effect = closed.set
                response = _response(b"x")

                def blocked_read(_amount):
                    interruption_observed.append(closed.wait(1))
                    raise ValueError("the watchdog closed the reader")

                response.read.side_effect = blocked_read
                connection.getresponse.return_value = response
                deadline = TelemetrySnapshotDeadline(time.monotonic_ns() + 50_000_000)
                error = (TelemetrySnapshotTransportUnavailable if composition == "sampler"
                         else bounded_http.BoundedHTTPTransportError)
                try:
                    with (
                        patch.object(client, "_new_connection", return_value=connection),
                        patch.object(owner, "ResidentSSHHTTPClient", return_value=client),
                        self.assertRaises(error),
                    ):
                        if composition == "direct":
                            client.get_bytes("/blocked", timeout_seconds=0.05)
                        elif composition == "sampler":
                            sampler.snapshot(deadline=deadline)
                        else:
                            ready = SimpleNamespace(config_bytes=b'{"port":9}', session="fixture", lane="gpu_fast")
                            owner._close_lane(sentinel.ssh, ready, deadline_ns=deadline.monotonic_ns)
                    self.assertEqual(interruption_observed, [True], "default interruption was disabled")
                    self.assertTrue(closed.is_set())
                    self.assertIsNone(client._connection)
                finally:
                    sampler.close()

    def test_supervised_programming_errors_remain_visible_before_and_after_deadline(self) -> None:
        for advance_seconds in (0.0, 0.3):
            with self.subTest(advance_seconds=advance_seconds):
                now = [1.0]
                client = bounded_http.ThreadOwnedPersistentHTTPClient(
                    "http://127.0.0.1:9", maximum_response_bytes=64,
                    monotonic_clock=lambda: now[0],
                )
                client.adopt_supervisor_interruption()
                connection = Mock(sock=None)
                response = _response(b"x")

                def broken_reader(_amount):
                    now[0] += advance_seconds
                    raise ValueError("independent reader programming defect")

                response.read.side_effect = broken_reader
                connection.getresponse.return_value = response
                try:
                    with (
                        patch.object(client, "_new_connection", return_value=connection) as new_connection,
                        patch.object(bounded_http.threading, "Timer", side_effect=_forbidden_timer),
                        self.assertRaisesRegex(ValueError, "independent reader programming defect"),
                    ):
                        client.get_bytes("/broken", timeout_seconds=0.25, transport_attempts=2)
                    new_connection.assert_called_once()
                    response.read.assert_called_once()
                    self.assertIsNone(client._connection)
                finally:
                    client.close()

    def _server(self, *, slow: bool):
        server = HTTPServer(("127.0.0.1", 0), _PullHandler)
        server.slow = slow
        server.received = threading.Event()
        server.finished = threading.Event()
        server.stop = threading.Event()
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def cleanup():
            server.stop.set()
            server.shutdown()
            server.server_close()
            thread.join(2)
            self.assertFalse(thread.is_alive(), "owned HTTP server thread was not reaped")

        self.addCleanup(cleanup)
        return server

    def _child(self, server):
        config = _sampler_config(f"http://127.0.0.1:{server.server_port}")
        spec = ResidentTelemetryCollectorSpec(
            factory_module=__name__, factory_qualname="_spawned_sampler_factory",
            canonical_config_json=json.dumps(config, sort_keys=True, separators=(",", ":")).encode(),
            expected_collector_identity_sha256=HASHES[0],
        )
        process = observer._ResidentSamplerProcess(spec, label="independent-supervision")
        self.addCleanup(process.close)
        return process

    def test_spawned_collector_adopts_supervision_before_its_first_real_http_request(self) -> None:
        server = self._server(slow=False)
        child = self._child(server)
        snapshot = child.snapshot(
            deadline=TelemetrySnapshotDeadline(time.monotonic_ns() + 1_000_000_000),
            cancel_event=threading.Event(), monotonic_ns=time.monotonic_ns,
        )
        self.assertTrue(server.received.is_set())
        self.assertEqual(snapshot.resident_exporter_provenance.wire_sequence, 1)
        child.close()
        self.assertTrue(child._process_closed)

    def test_slow_http_in_actual_child_is_killed_at_original_deadline_without_late_success(self) -> None:
        before = {process.pid for process in multiprocessing.active_children()}
        server = self._server(slow=True)
        child = self._child(server)
        started = time.monotonic()
        deadline = TelemetrySnapshotDeadline(time.monotonic_ns() + 250_000_000)
        with self.assertRaises(TelemetrySnapshotDeadlineExceeded):
            child.snapshot(deadline=deadline, cancel_event=threading.Event(), monotonic_ns=time.monotonic_ns)
        self.assertTrue(server.received.is_set(), "the synthetic HTTP stall was not reached")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertFalse(child._process.is_alive())
        self.assertIsNotNone(child._process.exitcode)
        # A body that is still arriving cannot be accepted by a renewed invocation.
        with self.assertRaises(TelemetrySnapshotTransportUnavailable):
            child.snapshot(
                deadline=TelemetrySnapshotDeadline(time.monotonic_ns() + 1_000_000_000),
                cancel_event=threading.Event(), monotonic_ns=time.monotonic_ns,
            )
        child.close()
        self.assertEqual({process.pid for process in multiprocessing.active_children()}, before)


if __name__ == "__main__":
    unittest.main()
