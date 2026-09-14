"""Independent bounded cache and two-reader lifecycle tests; no network or CLI."""

from dataclasses import replace
import json
import threading
import time
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import mineru_stream_pressure as adapter
from disclosure_anchor.adapters.runtime.bounded_http import (
    BoundedHTTPProtocolError,
    BoundedHTTPTransportError,
)
from disclosure_anchor.application.contracts.mineru_capacity_config import (
    decode_mineru_capacity_config,
)
from tests._mineru_capacity_config_fixture import canonical_payload
from tests._mineru_capacity_v11_fixture import idle_health
from tests._mineru_package_a_fixture import explicit_payload
from tests.unit.test_capacity_sources import _gpu_payload
from tests.unit.test_mineru_process_pressure_consumer import (
    CGROUP,
    OWNER,
    pressure_payload,
)


GPU = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def examples():
    payload = explicit_payload(14)
    capacity = decode_mineru_capacity_config(canonical_payload(payload))
    health = idle_health(payload)
    health["capacity_observation"]["owner"] = dict(OWNER)
    pressure = pressure_payload()
    pressure["capacity_config_sha256"] = capacity.sha256
    binding = adapter.PressureBinding(
        runtime_identity_sha256="sha256:" + "3" * 64,
        capacity=capacity,
        owner_json=canonical_payload(OWNER),
        cgroup_identity_sha256=CGROUP,
        cgroup_max_bytes=1000,
        gpu_uuid=GPU,
    )
    return binding, health, pressure


def gpu_payload(timestamp):
    return _gpu_payload().replace(
        b"timestamp_seconds 1000\n", f"timestamp_seconds {timestamp}\n".encode()
    )


class PressureCacheTests(unittest.TestCase):
    def setUp(self):
        self.binding, self.health, self.pressure = examples()
        self.now = 100.0
        self.remote_ns = 1000
        self.cache = adapter.StreamPressureCache(
            self.binding, monotonic=lambda: self.now
        )

    def publish_api(self, *, duration=0.1):
        self.remote_ns += 1000
        self.pressure["observed_at"].update(
            started_ns=self.remote_ns, completed_ns=self.remote_ns + 100
        )
        self.cache.publish_api(
            canonical_payload(self.health),
            canonical_payload(self.pressure),
            started=self.now,
            finished=self.now + duration,
        )

    def publish_gpu(self, *, timestamp=1000.0, wall=1000.0):
        with patch.object(adapter.time, "time", return_value=wall):
            self.cache.publish_gpu(
                gpu_payload(timestamp),
                started=self.now,
                finished=self.now + 0.1,
                received_wall=wall,
            )

    def test_no_data_or_failed_lane_remains_unknown_and_never_synthetic_zero(self):
        sample = self.cache.latest()
        self.assertIsNone(sample.gpu_free_bytes)
        self.assertIsNone(sample.host_available_bytes)
        self.assertIsNone(sample.http_active)
        self.assertIn("api_unavailable", sample.unknown_reason)
        self.publish_api()
        self.assertIsNone(self.cache.latest().gpu_free_bytes)
        self.publish_gpu()
        good = self.cache.latest()
        self.assertIsNone(good.unknown_reason)
        self.assertEqual(good.host_available_bytes, 700)
        self.assertEqual(good.gpu_free_bytes, 7818182656)
        self.cache.record_failure("gpu", BoundedHTTPTransportError("bounded timeout"))
        failed = self.cache.latest()
        self.assertIn("gpu", failed.unknown_reason)
        self.assertEqual(failed.gpu_free_bytes, good.gpu_free_bytes)
        self.assertIsNone(failed.unsafe_reason)
        self.publish_gpu()
        self.assertIsNone(self.cache.latest().unknown_reason)
        self.cache.record_failure("api", ValueError("owner mismatch"), fatal=True)
        self.publish_api()
        self.assertEqual(self.cache.latest().unsafe_reason, "api_reader_failed")

    def test_API_three_seconds_and_GPU_eight_seconds_expire_independently(self):
        self.publish_api()
        self.publish_gpu()
        self.now = 103.01
        self.assertIn("api_unavailable_or_stale", self.cache.latest().unknown_reason)
        self.publish_api()
        self.assertIsNone(self.cache.latest().unknown_reason)
        self.now = 107.99
        self.publish_api()
        self.assertIsNone(self.cache.latest().unknown_reason)
        self.now = 108.01
        self.assertEqual(self.cache.latest().unknown_reason, "gpu_unavailable_or_stale")

    def test_repeated_exporter_timestamp_cannot_refresh_age_despite_new_HTTP_reads(
        self,
    ):
        self.publish_api()
        self.publish_gpu()
        self.now = 108.01
        self.publish_api()
        # Even implausible wall-clock jitter cannot make this same exporter sample fresh.
        self.publish_gpu(timestamp=1000.0, wall=1000.0)
        self.assertIn("gpu_unavailable_or_stale", self.cache.latest().unknown_reason)
        self.assertEqual(self.cache.latest().observed_monotonic, 100.0)
        self.publish_gpu(timestamp=1008.0, wall=1008.01)
        self.assertIsNone(self.cache.latest().unknown_reason)
        self.assertAlmostEqual(self.cache.latest().observed_monotonic, 108.0)

    def test_local_start_bracket_includes_collection_delay_and_remote_clocks_do_not_mix(
        self,
    ):
        self.publish_api(duration=4.0)
        self.publish_gpu()
        self.now = 104.0
        self.assertIn("api_unavailable_or_stale", self.cache.latest().unknown_reason)
        self.assertEqual(self.cache.latest().observed_monotonic, 100.0)
        self.assertNotEqual(self.cache.latest().observed_monotonic, self.remote_ns)
        for started, finished in ((2.0, 1.0), (-1.0, 1.0), (float("nan"), 1.0)):
            with self.subTest(started=started), self.assertRaises(ValueError):
                self.cache.publish_api(
                    canonical_payload(self.health),
                    canonical_payload(self.pressure),
                    started=started,
                    finished=finished,
                )

    def test_event_deltas_require_same_identity_and_regressions_or_new_OOM_stay_unsafe(
        self,
    ):
        self.pressure["memory"]["memory_events"]["oom"] = 7
        self.pressure["memory"]["memory_events"]["oom_kill"] = 2
        self.publish_api()
        self.assertIsNone(
            self.cache.latest().unsafe_reason, "historical OOM count is not a new event"
        )
        self.publish_api()
        self.assertIsNone(self.cache.latest().unsafe_reason)
        self.pressure["memory"]["memory_events"]["oom"] += 1
        self.publish_api()
        self.assertEqual(self.cache.latest().unsafe_reason, "new_cgroup_oom")
        self.publish_api()
        self.assertEqual(self.cache.latest().unsafe_reason, "new_cgroup_oom")
        self.cache = adapter.StreamPressureCache(
            self.binding, monotonic=lambda: self.now
        )
        self.publish_api()
        self.pressure["memory"]["memory_events"]["high"] -= 1
        self.publish_api()
        self.assertEqual(self.cache.latest().unsafe_reason, "cgroup_events_regressed")
        self.pressure["memory"]["cgroup_identity_sha256"] = "sha256:" + "9" * 64
        with self.assertRaises(ValueError):
            self.publish_api()

    def test_GPU_timestamp_rollback_and_wrong_device_are_visible(self):
        self.publish_gpu()
        self.now += 1
        self.publish_gpu(timestamp=999.0, wall=1001.0)
        self.assertEqual(
            self.cache.latest().unsafe_reason, "gpu_sample_clock_regressed"
        )
        with (
            patch.object(adapter.time, "time", return_value=1001.0),
            self.assertRaises(ValueError),
        ):
            self.cache.publish_gpu(
                gpu_payload(1001.0).replace(
                    GPU.encode(), b"bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
                ),
                started=self.now,
                finished=self.now + 0.1,
                received_wall=1001.0,
            )

    def test_binding_is_canonical_and_validated_before_readers_exist(self):
        for changed in (
            {"owner_json": json.dumps(OWNER).encode()},
            {"owner_json": canonical_payload({**OWNER, "process_id": True})},
            {"cgroup_max_bytes": True},
            {"api_max_age_seconds": 0},
            {"gpu_max_age_seconds": float("nan")},
            {"runtime_identity_sha256": "not-a-hash"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                replace(self.binding, **changed)


class PressureSessionTests(unittest.TestCase):
    def setUp(self):
        self.binding, self.health, self.pressure = examples()
        self.clients = []
        self.actions = {}
        self.closed = {lane: threading.Event() for lane in ("api", "gpu")}
        self.read_twice = {lane: threading.Event() for lane in ("api", "gpu")}
        self.records = []
        self.woken = threading.Event()

    def factory(self, base_url, *, maximum_response_bytes):
        case = self
        lane = "api" if "api.invalid" in base_url else "gpu"
        self.assertEqual(maximum_response_bytes, 65536)

        class Client:
            def __init__(self):
                self.owner = threading.get_ident()
                self.calls = []
                self.closed = False
                self.lane = lane

            def get_bytes(self, path, *, timeout_seconds, transport_attempts):
                case.assertEqual(threading.get_ident(), self.owner)
                case.assertEqual(timeout_seconds, 0.2)
                case.assertEqual(transport_attempts, 1)
                self.calls.append(path)
                if len(self.calls) >= (4 if lane == "api" else 2):
                    case.read_twice[lane].set()
                action = case.actions.get(lane)
                if action is not None:
                    action(path)
                if path == "/health":
                    return 200, canonical_payload(case.health)
                if path == "/agent/telemetry/pressure/v1":
                    value = {
                        **case.pressure,
                        "observed_at": {
                            "clock": "python.monotonic_ns",
                            "started_ns": time.monotonic_ns(),
                            "completed_ns": time.monotonic_ns(),
                        },
                    }
                    return 200, canonical_payload(value)
                return 200, gpu_payload(time.time())

            def close(self):
                case.assertEqual(threading.get_ident(), self.owner)
                self.closed = True
                case.closed[lane].set()
                action = case.actions.get(lane + "_close")
                if action is not None:
                    action()

        client = Client()
        self.clients.append(client)
        return client

    def session(self, **overrides):
        args = dict(
            api_url="http://api.invalid",
            gpu_url="http://gpu.invalid/metrics",
            evidence_sink=self.records.append,
            wakeup=self.woken.set,
            request_timeout_seconds=0.2,
            client_factory=self.factory,
        )
        args.update(overrides)
        result = adapter.StreamPressureSession(self.binding, **args)

        def cleanup():
            # Always reap only these test-created local threads, including red startup cases.
            result._stop.set()
            for thread in result._threads:
                if thread.ident is not None:
                    thread.join(3)
            self.assertFalse(any(thread.is_alive() for thread in result._threads))

        self.addCleanup(cleanup)
        return result

    def test_two_persistent_connections_publish_independently_and_close_in_owner_threads(
        self,
    ):
        result = self.session()
        result.start()
        self.assertTrue(self.read_twice["api"].wait(2))
        self.assertTrue(self.read_twice["gpu"].wait(2))
        result.close()
        self.assertEqual(len(self.clients), 2)
        self.assertEqual(len({client.owner for client in self.clients}), 2)
        self.assertTrue(all(client.closed for client in self.clients))
        self.assertEqual({record["lane"] for record in self.records}, {"api", "gpu"})
        self.assertTrue(all(not thread.daemon for thread in result._threads))
        self.assertTrue(self.woken.is_set())
        self.assertIsNone(result.cache.latest().unknown_reason)
        with self.assertRaises(RuntimeError):
            result.start()

    def test_blocked_API_read_does_not_block_GPU_or_cached_latest(self):
        entered, release = threading.Event(), threading.Event()

        def block(_):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test API release missing")

        self.actions["api"] = block
        result = self.session()
        result.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.read_twice["gpu"].wait(2))
            before = time.monotonic()
            sample = result.cache.latest()
            self.assertLess(time.monotonic() - before, 0.1)
            self.assertIsNone(sample.host_available_bytes)
            self.assertIsNotNone(sample.gpu_free_bytes)
            self.assertIsNotNone(sample.unknown_reason)
        finally:
            release.set()
            result.close()

    def test_transport_failure_is_unknown_protocol_or_sink_failure_is_fatal_and_visible_at_close(
        self,
    ):
        for kind in ("transport", "protocol", "sink", "client_close"):
            self.clients.clear()
            self.actions.clear()
            self.woken.clear()
            self.closed = {lane: threading.Event() for lane in ("api", "gpu")}
            marker = (
                BoundedHTTPTransportError("temporary timeout")
                if kind == "transport"
                else BoundedHTTPProtocolError("bad frame")
            )

            def fail(_):
                raise marker

            kwargs = {}
            if kind in ("transport", "protocol"):
                self.actions["api"] = fail
            elif kind == "sink":
                kwargs["evidence_sink"] = fail
            else:
                self.actions["api_close"] = lambda: (_ for _ in ()).throw(
                    RuntimeError("close failed")
                )
            result = self.session(**kwargs)
            result.start()
            self.assertTrue(self.woken.wait(1))
            with self.subTest(kind=kind):
                if kind == "transport":
                    result.close()
                    self.assertIsNone(result.cache.latest().unsafe_reason)
                    self.assertIsNotNone(result.cache.latest().unknown_reason)
                else:
                    with self.assertRaises(BaseExceptionGroup):
                        result.close()
                    self.assertIsNotNone(result.cache.latest().unsafe_reason)
                self.assertTrue(all(client.closed for client in self.clients))

    def test_partial_thread_start_failure_closes_already_started_lane_and_preserves_error(
        self,
    ):
        result = self.session()
        original_start = threading.Thread.start
        marker = RuntimeError("injected second lane start failure")

        def start(thread):
            if thread.name == "mineru-pressure-gpu":
                raise marker
            original_start(thread)

        with patch.object(threading.Thread, "start", autospec=True, side_effect=start):
            with self.assertRaises(RuntimeError) as caught:
                result.start()
        self.assertIs(caught.exception, marker)
        self.assertTrue(
            self.closed["api"].wait(1),
            "partial start leaked its first persistent reader",
        )
        self.assertFalse(any(thread.is_alive() for thread in result._threads))
        result.close()
