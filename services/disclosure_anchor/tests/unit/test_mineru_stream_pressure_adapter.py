"""Independent bounded cache and two-reader lifecycle tests; no network or CLI."""

from dataclasses import replace
import json
import math
import threading
import time
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import mineru_stream_pressure as adapter
from disclosure_anchor.adapters.runtime.bounded_http import (
    BoundedHTTPProtocolError,
    BoundedHTTPResponse,
    BoundedHTTPTransportError,
)
from disclosure_anchor.application.contracts.mineru_capacity_config import (
    decode_mineru_capacity_config,
)
from disclosure_anchor.application.services.mineru_stream_policy import (
    MineruStreamPolicy,
    StreamPolicyConfig,
)
from tests._mineru_capacity_config_fixture import canonical_payload
from tests._mineru_capacity_v11_fixture import idle_health
from tests._mineru_package_a_fixture import explicit_payload
from tests.unit.test_capacity_sources import _exporter_date, _gpu_payload
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

    def publish_gpu(self, *, timestamp=1000.0, date=1000):
        # ``date`` is the exporter host's own HTTP Date; no local wall clock.
        self.cache.publish_gpu(
            gpu_payload(timestamp),
            started=self.now,
            finished=self.now + 0.1,
            response_date=(_exporter_date(date),),
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
        # Date equal to the collection second bounds the sample at under one
        # second old, so it is conservatively anchored at start - 1 = 99.
        self.publish_gpu()
        self.now = 103.01
        self.assertIn("api_unavailable_or_stale", self.cache.latest().unknown_reason)
        self.publish_api()
        self.assertIsNone(self.cache.latest().unknown_reason)
        self.now = 106.99
        self.publish_api()
        self.assertIsNone(self.cache.latest().unknown_reason)
        self.now = 107.01
        self.assertEqual(self.cache.latest().unknown_reason, "gpu_unavailable_or_stale")

    def test_repeated_exporter_timestamp_cannot_refresh_age_despite_new_HTTP_reads(
        self,
    ):
        self.publish_api()
        self.publish_gpu()
        self.now = 108.01
        self.publish_api()
        # A successful HTTP read of the same exporter sample, even one whose
        # Date implausibly repeats the collection second, cannot refresh it.
        self.publish_gpu(timestamp=1000.0, date=1000)
        self.assertIn("gpu_unavailable_or_stale", self.cache.latest().unknown_reason)
        self.assertEqual(self.cache.latest().observed_monotonic, 99.0)
        self.publish_gpu(timestamp=1008.0, date=1008)
        self.assertIsNone(self.cache.latest().unknown_reason)
        # It completed after the previous response, which did not show it, started.
        self.assertAlmostEqual(self.cache.latest().observed_monotonic, 108.01)

    def test_local_start_bracket_includes_collection_delay_and_remote_clocks_do_not_mix(
        self,
    ):
        self.publish_api(duration=4.0)
        self.publish_gpu()
        self.now = 104.0
        self.assertIn("api_unavailable_or_stale", self.cache.latest().unknown_reason)
        # The joined time is the older local bound: GPU start - 1 exporter second.
        self.assertEqual(self.cache.latest().observed_monotonic, 99.0)
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
        self.publish_gpu(timestamp=999.0, date=1001)
        self.assertEqual(
            self.cache.latest().unsafe_reason, "gpu_sample_clock_regressed"
        )
        with self.assertRaises(ValueError):
            self.cache.publish_gpu(
                gpu_payload(1001.0).replace(
                    GPU.encode(), b"bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
                ),
                started=self.now,
                finished=self.now + 0.1,
                response_date=(_exporter_date(1001),),
            )

    def test_GPU_age_uses_exporter_date_and_local_monotonic_never_local_wall(self):
        # The exporter host runs about 190 years ahead of this cache's clocks
        # and the local wall clock would trap if read: only the same-host
        # Date - success bound and local monotonic brackets decide freshness.
        remote = 7_000_000_000
        self.publish_api()
        with patch("time.time", side_effect=AssertionError("local wall clock read")):
            self.publish_gpu(timestamp=float(remote), date=remote + 2)
            first = self.cache.latest()
            self.assertIsNone(first.unknown_reason)
            self.assertEqual(first.observed_monotonic, 97.0)
            # A new token has two lower bounds: 106 - 1 from its own Date and
            # 100, the start of the previous response that did not show it.
            self.now = 106.0
            self.publish_api()
            self.publish_gpu(timestamp=float(remote + 5), date=remote + 5)
            self.assertEqual(self.cache.latest().observed_monotonic, 105.0)
            self.assertIsNone(self.cache.latest().unknown_reason)
            # Once this Date precedes that earlier response's Date the exporter
            # clock stepped back, so its Date - success cannot vouch for the
            # sample; only the transition bound (107) remains, and it is stale.
            self.now = 107.0
            self.publish_gpu(timestamp=float(remote + 5), date=remote + 20)
            self.now = 116.0
            self.publish_api()
            self.publish_gpu(timestamp=float(remote + 10), date=remote + 11)
            self.assertEqual(self.cache.latest().observed_monotonic, 107.0)
            self.assertIn("gpu_unavailable_or_stale", self.cache.latest().unknown_reason)
            self.assertIsNone(self.cache.latest().unsafe_reason)
            self.now = 106.0
            # A first-seen sample the exporter itself dates as old is never
            # accepted as fresh: 12 s by its own clock, or stale over 30 s.
            fresh_cache = adapter.StreamPressureCache(self.binding, monotonic=lambda: self.now)
            self.cache = fresh_cache
            self.publish_api()
            self.publish_gpu(timestamp=float(remote), date=remote + 11)
            self.assertIn("gpu_unavailable_or_stale", self.cache.latest().unknown_reason)
            self.assertEqual(self.cache.latest().observed_monotonic, 94.0)
            self.cache = adapter.StreamPressureCache(self.binding, monotonic=lambda: self.now)
            self.publish_api()
            self.publish_gpu(timestamp=float(remote), date=remote + 40)
            stale = self.cache.latest()
            self.assertIsNone(stale.gpu_free_bytes)
            self.assertIn("gpu", stale.unknown_reason)
            self.assertIsNone(stale.unsafe_reason)
            # A response dated before its own collection (an exporter-host
            # clock step inside it) is transient: no value, no unsafe latch.
            self.publish_gpu(timestamp=float(remote + 3), date=remote + 1)
            self.assertIsNone(self.cache.latest().gpu_free_bytes)
            self.assertIsNone(self.cache.latest().unsafe_reason)
            self.publish_gpu(timestamp=float(remote + 3), date=remote + 3)
            self.assertIsNone(self.cache.latest().unknown_reason)
            self.assertEqual(self.cache.latest().gpu_free_bytes, 7818182656)

    def test_GPU_bound_before_the_monotonic_origin_is_kept_not_floored(self):
        def fresh_cache(now):
            self.now = now
            self.cache = adapter.StreamPressureCache(self.binding, monotonic=lambda: self.now)

        # Root's counterexample: the monotonic clock is only 1 s past its
        # origin, and Date 1019 for token 1000 bounds the sample at up to
        # 20.1 s old on receipt at 1.1. It must be stale, not floored to 0.
        fresh_cache(1.0)
        self.publish_api()
        self.publish_gpu(timestamp=1000.0, date=1019)
        self.now = 1.1
        stale = self.cache.latest()
        self.assertIn("gpu_unavailable_or_stale", stale.unknown_reason)
        self.assertAlmostEqual(stale.observed_monotonic, -19.0)
        self.assertIsNone(stale.unsafe_reason)
        policy = MineruStreamPolicy(StreamPolicyConfig(
            qualified_max=1,
            runtime_identity_sha256=self.binding.runtime_identity_sha256,
            owner_identity_sha256=self.binding.owner_sha256,
            host_pause_bytes=100, host_recover_bytes=500,
            sample_max_age_seconds=8.0,
        ))
        decision = policy.evaluate(stale, now=self.now)
        self.assertEqual((decision.reason, decision.unsafe, decision.new_post_allowed), ("pressure_unknown", False, False))
        self.assertAlmostEqual(decision.sample_observed_monotonic, -19.0)
        # The public sample carries any finite local bound, and nothing else.
        for invalid in (float("nan"), float("inf"), True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                replace(stale, observed_monotonic=invalid)
        # A valid sample first read near the origin is fresh, carries its
        # exact pre-origin time publicly, and expires exactly 8 s after it.
        fresh_cache(1.0)
        self.publish_api()
        self.publish_gpu(timestamp=1000.0, date=1002)
        for now, stale_expected in ((5.99, False), (6.01, True)):
            with self.subTest(now=now):
                self.now = now
                self.publish_api()
                sample = self.cache.latest()
                self.assertAlmostEqual(sample.observed_monotonic, -2.0)
                self.assertEqual("gpu_unavailable_or_stale" in (sample.unknown_reason or ""), stale_expected)

    def test_partial_startup_then_first_pre_origin_join_is_not_clock_drift(self):
        # Root's F3 with the real cache and real policy: the policy already
        # evaluated an API-only startup sample before any GPU value existed.
        self.now = 1.0
        self.cache = adapter.StreamPressureCache(self.binding, monotonic=lambda: self.now)
        policy = MineruStreamPolicy(StreamPolicyConfig(
            qualified_max=1,
            runtime_identity_sha256=self.binding.runtime_identity_sha256,
            owner_identity_sha256=self.binding.owner_sha256,
            host_pause_bytes=100, host_recover_bytes=500,
            sample_max_age_seconds=8.0,
        ))
        self.publish_api()
        partial = self.cache.latest()
        self.assertIsNone(partial.observed_monotonic, "no joined time is reported as a placeholder")
        self.assertIn("gpu_unavailable_or_stale", partial.unknown_reason)
        startup = policy.evaluate(partial, now=self.now)
        self.assertEqual((startup.reason, startup.unsafe, startup.sample_observed_monotonic),
                         ("pressure_unknown", False, None))
        with self.assertRaises(ValueError):
            replace(partial, unknown_reason=None)
        # The first joined bound precedes the monotonic origin; it is ordered
        # only against later joined times, never against the partial sample.
        self.publish_gpu(timestamp=1000.0, date=1002)
        self.now = 1.1
        joined = self.cache.latest()
        self.assertAlmostEqual(joined.observed_monotonic, -2.0)
        self.assertIsNone(joined.unknown_reason)
        first_join = policy.evaluate(joined, now=self.now)
        self.assertEqual((first_join.reason, first_join.unsafe), ("recovery", False))
        # A real regression between joined samples still latches unsafe.
        regressed = replace(joined, sequence=joined.sequence + 1, observed_monotonic=-3.0)
        latched = policy.evaluate(regressed, now=self.now)
        self.assertEqual((latched.reason, latched.unsafe), ("unsafe:sample_clock_drift", True))

    def test_exporter_step_back_before_first_acceptance_stays_conservative(self):
        # Reviewer F4 schedules: the exporter clock E(t) = BASE + t steps back
        # 4 s at t=100.5, after the 100.2 collection but before any read showed
        # it. Reads start on whole seconds, Date is stamped at +0.01 and each
        # read completes at +0.02; once the collector also stalls, that cached
        # success keeps being served.
        base = 1_000_000

        def exporter(t):
            return base + t + (-4.0 if t >= 100.5 else 0.0)

        for stalled in (False, True):
            with self.subTest(stalled=stalled):
                self.cache = adapter.StreamPressureCache(self.binding, monotonic=lambda: self.now)
                completions = (80.2, 85.2, 90.2, 95.2, 100.2) + (() if stalled else (105.2, 110.2))
                collections = {math.floor(exporter(c)): c for c in completions}
                worst_true_fresh = 0.0
                for tenth in range(920, 1200):
                    t = tenth / 10
                    if tenth % 10 == 0:
                        gather = t + 0.01
                        token = max(tok for tok, c in collections.items() if c <= gather)
                        self.now = t
                        self.publish_api()
                        self.cache.publish_gpu(
                            gpu_payload(float(token)), started=t, finished=t + 0.02,
                            response_date=(_exporter_date(math.floor(exporter(gather))),),
                        )
                    self.now = t + 0.05
                    sample = self.cache.latest()
                    cached = self.cache._gpu_timestamp
                    if sample.unknown_reason is None and cached is not None:
                        completed = collections[int(cached)]
                        # The reported observation never postdates the true collection.
                        self.assertLessEqual(sample.observed_monotonic, completed, (t, cached))
                        worst_true_fresh = max(worst_true_fresh, self.now - completed)
                    if tenth == 1040:
                        # First accepted at 104 after unordered reads 101-103:
                        # only the start of read 100, which did not show it yet.
                        self.assertEqual((cached, sample.observed_monotonic), (float(base + 100), 100.0))
                self.assertLessEqual(worst_true_fresh, 8.0)
                self.assertIsNone(self.cache.latest().unsafe_reason)

    def test_token_first_seen_unbounded_without_predecessor_waits_for_a_newer_token(self):
        # The reader's very first response shows a token it cannot bound
        # (dated before its collection, or already over 30 s old). Waiting for
        # a later Date must not make it fresh, and absence never becomes 0.
        base = 1_000_000
        for first_date, later_dates in ((base + 8, (base + 9, base + 10, base + 11)),
                                        (base + 50, (base + 12, base + 13, base + 14))):
            with self.subTest(first_date=first_date):
                self.cache = adapter.StreamPressureCache(self.binding, monotonic=lambda: self.now)
                for second, date in zip((10, 11, 12, 13), (first_date, *later_dates)):
                    self.now = float(second)
                    self.publish_api()
                    self.publish_gpu(timestamp=float(base + 10), date=date)
                    sample = self.cache.latest()
                    self.assertIsNone(sample.gpu_free_bytes)
                    self.assertIsNone(sample.observed_monotonic)
                    self.assertIn("gpu", sample.unknown_reason)
                    self.assertIsNone(sample.unsafe_reason)
                self.assertIn("no age bound since its first response", self.cache._failures["gpu"])
                # A newer collection has a predecessor response: bounded normally,
                # the later of its Date bound (16.1 - 2.1) and read 13's start.
                self.now = 16.0
                self.publish_api()
                self.publish_gpu(timestamp=float(base + 15), date=base + 16)
                recovered = self.cache.latest()
                self.assertIsNone(recovered.unknown_reason)
                self.assertEqual(recovered.gpu_free_bytes, 7818182656)
                self.assertAlmostEqual(recovered.observed_monotonic, 14.0)

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
                case.assertEqual(lane, "api", "the GPU lane must read its response Date")
                self.calls.append(path)
                if len(self.calls) >= 4:
                    case.read_twice[lane].set()
                action = case.actions.get(lane)
                if action is not None:
                    action(path)
                if path == "/health":
                    return 200, canonical_payload(case.health)
                value = {
                    **case.pressure,
                    "observed_at": {
                        "clock": "python.monotonic_ns",
                        "started_ns": time.monotonic_ns(),
                        "completed_ns": time.monotonic_ns(),
                    },
                }
                return 200, canonical_payload(value)

            def get_response(self, path, *, response_headers, timeout_seconds, transport_attempts):
                case.assertEqual(threading.get_ident(), self.owner)
                case.assertEqual((lane, path, response_headers), ("gpu", "/metrics", ("Date",)))
                case.assertEqual((timeout_seconds, transport_attempts), (0.2, 1))
                self.calls.append(path)
                if len(self.calls) >= 2:
                    case.read_twice[lane].set()
                action = case.actions.get(lane)
                if action is not None:
                    action(path)
                # The exporter host clock runs an hour ahead of this host.
                exporter_now = int(time.time()) + 3600
                return BoundedHTTPResponse(
                    status=200, body=gpu_payload(float(exporter_now)),
                    headers={"Date": (_exporter_date(exporter_now),)}, elapsed_seconds=0.001,
                )

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
