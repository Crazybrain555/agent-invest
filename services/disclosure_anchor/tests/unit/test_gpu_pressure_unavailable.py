"""Independent candidate regressions: unavailable GPU samples are not identity faults.

All readers are the real local StreamPressureSession threads; only their HTTP
transport is the existing synthetic persistent-client fixture. No network/PG.
"""

from contextlib import contextmanager
import sys
import threading
import time
import traceback
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import capacity_sources
from disclosure_anchor.application.ports.mineru_stream_pressure import StreamSubmissionDeferred
from disclosure_anchor.application.services.mineru_stream_policy import (
    MineruStreamPolicy,
    StreamAdmissionControl,
    StreamPolicyConfig,
)
from tests.unit import test_mineru_stream_pressure_adapter as fixtures


def gpu_frame(kind, *, prior_timestamp=None):
    timestamp = time.time()
    if kind == "expired":
        timestamp -= 31.0
    elif kind == "rollback":
        timestamp = prior_timestamp - 31.0
    elif kind in ("future", "failed_future"):
        timestamp += 10.0
    raw = fixtures.gpu_payload(timestamp)
    if kind.startswith("failed"):
        raw = raw.replace(b"nvidia_smi_last_collect_success 1\n", b"nvidia_smi_last_collect_success 0\n")
    if kind in ("wrong_uuid", "failed_wrong_uuid"):
        raw = raw.replace(fixtures.GPU.encode(), b"bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    if kind == "failed_bad_value":
        raw = raw.replace(b"} 7818182656\n", b"} -1\n")
    if kind == "failed_bad_format":
        # An unrelated malformed line is deliberately ignored by the existing
        # parser. Corrupt the required free-memory token instead: it cannot
        # become a valid measurement or be hidden by collection success=0.
        required_value = b"} 7818182656\n"
        assert raw.count(required_value) == 1
        raw = raw.replace(required_value, b"} not-a-number\n")
    return timestamp, raw


class GpuPressureUnavailableTests(unittest.TestCase):
    def harness(self, kinds):
        fixture = fixtures.PressureSessionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        received = []
        fixture.gpu_notifications = 0
        fixture.api_notifications = 0

        def wakeup():
            if any(client.lane == "gpu" and client.owner == threading.get_ident() for client in fixture.clients):
                fixture.gpu_notifications += 1
            if any(client.lane == "api" and client.owner == threading.get_ident() for client in fixture.clients):
                fixture.api_notifications += 1
            fixture.woken.set()

        def factory(base_url, *, maximum_response_bytes):
            client = fixture.factory(base_url, maximum_response_bytes=maximum_response_bytes)
            if client.lane == "gpu":
                original = client.get_bytes

                def read(path, *, timeout_seconds, transport_attempts):
                    status, _ = original(path, timeout_seconds=timeout_seconds, transport_attempts=transport_attempts)
                    kind = kinds[min(len(received), len(kinds) - 1)]
                    stamp, raw = gpu_frame(kind, prior_timestamp=None if not received else received[0][1])
                    received.append((kind, stamp, raw))
                    return status, raw

                client.get_bytes = read
            return client

        session = fixture.session(client_factory=factory, wakeup=wakeup)
        return fixture, session, received

    def wait_for(self, fixture, session, predicate, *, timeout=3.5):
        deadline = time.monotonic() + timeout
        while True:
            fixture.woken.clear()
            sample = session.cache.latest()
            if predicate(sample):
                return sample
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail(f"reader did not reach expected public state: {sample!r}")
            fixture.woken.wait(remaining)

    @contextmanager
    def running(self, session):
        session.start()
        try:
            yield
        except BaseException:
            # Preserve the primary red assertion plus the actual reader error;
            # cleanup always reaps the test's real local threads.
            try:
                session.close()
            except BaseExceptionGroup as close_error:
                traceback.print_exception(close_error, file=sys.stderr)
            raise
        else:
            session.close()

    def recover_after_initial_unavailable(self, kind):
        fixture, session, received = self.harness([kind, "fresh"])
        with self.running(session):
            unavailable = self.wait_for(
                fixture, session,
                lambda sample: fixture.gpu_notifications >= 1 and (sample.unsafe_reason is not None or "gpu" in (sample.unknown_reason or "")),
            )
            self.assertIsNone(unavailable.unsafe_reason, "mere GPU unavailability killed its persistent reader")
            self.assertIsNone(unavailable.gpu_free_bytes, "an unavailable initial frame must not create a memory value")
            self.assertFalse(fixture.closed["gpu"].is_set())
            ready = self.wait_for(
                fixture, session,
                lambda sample: fixture.gpu_notifications >= 2 and sample.unknown_reason is None,
            )
            self.assertIsNone(ready.unsafe_reason)
            self.assertEqual(ready.gpu_free_bytes, 7818182656)
            session.wait_initial_sample(timeout_seconds=0.05)
            self.assertEqual(len([client for client in fixture.clients if client.lane == "gpu"]), 1)
            self.assertFalse(fixture.closed["gpu"].is_set())
        self.assertTrue(all(client.closed for client in fixture.clients))

    def test_expired_over_thirty_seconds_then_fresh_recovers_in_same_reader(self):
        self.recover_after_initial_unavailable("expired")

    def test_unsuccessful_collection_then_success_recovers_in_same_reader(self):
        self.recover_after_initial_unavailable("failed")

    def test_continuous_unknown_retains_old_value_without_refresh_and_policy_pauses(self):
        fixture, session, received = self.harness(["fresh", "failed", "failed"])
        with self.running(session):
            good = self.wait_for(fixture, session, lambda sample: sample.unknown_reason is None)
            now = [time.monotonic()]
            good_seen_at = now[0]
            policy = MineruStreamPolicy(StreamPolicyConfig(
                qualified_max=5,
                runtime_identity_sha256=fixture.binding.runtime_identity_sha256,
                owner_identity_sha256=fixture.binding.owner_sha256,
                host_pause_bytes=100, host_recover_bytes=500,
                missing_pause_seconds=10.0,
            ))
            self.assertEqual(policy.evaluate(good, now=now[0]).target, 5)
            bad = self.wait_for(
                fixture, session,
                lambda sample: fixture.gpu_notifications >= 2 and fixture.api_notifications >= 2 and (sample.unknown_reason is not None or sample.unsafe_reason is not None),
            )
            self.assertIsNone(bad.unsafe_reason, "collection failure must enter the existing unknown policy branch")
            self.assertEqual(bad.gpu_free_bytes, good.gpu_free_bytes)
            # The initial joined time may be the slightly older API bracket.
            # After the second API frame, the unchanged first GPU dominates;
            # it may move forward to that GPU time but never past when we
            # already observed the good frame.
            self.assertGreaterEqual(bad.observed_monotonic, good.observed_monotonic)
            self.assertLessEqual(bad.observed_monotonic, good_seen_at)
            now[0] = time.monotonic()
            first_unknown = now[0]
            self.assertEqual(policy.evaluate(bad, now=first_unknown).target, 5)
            self.assertEqual(policy.evaluate(bad, now=first_unknown + 9.999).target, 5)
            decision = policy.evaluate(bad, now=first_unknown + 10.0)
            self.assertEqual((decision.target, decision.unsafe, decision.reason), (0, False, "pressure_unknown"))
            now[0] = first_unknown + 10.0
            control = StreamAdmissionControl(policy, session.cache, monotonic=lambda: now[0])
            with self.assertRaises(StreamSubmissionDeferred) as stopped:
                control.assert_submission_allowed(runtime_identity_sha256=fixture.binding.runtime_identity_sha256)
            self.assertFalse(stopped.exception.unsafe)
            again = self.wait_for(fixture, session, lambda sample: fixture.gpu_notifications >= 3 and fixture.api_notifications >= 3 and sample.unknown_reason is not None)
            self.assertIsNone(again.unsafe_reason)
            self.assertEqual(again.observed_monotonic, bad.observed_monotonic)
            self.assertFalse(fixture.closed["gpu"].is_set())

    def test_identity_future_format_values_and_timestamp_rollback_remain_fatal(self):
        # Combined unsuccessful/invalid cases prevent a broad catch or an early
        # success=0 shortcut from hiding independent identity/protocol faults.
        variants = (
            ["wrong_uuid"], ["future"], ["failed_wrong_uuid"],
            ["failed_future"], ["failed_bad_value"], ["failed_bad_format"],
            ["fresh", "rollback"],
        )
        for kinds in variants:
            with self.subTest(kinds=kinds):
                fixture, session, received = self.harness(kinds)
                session.start()
                bad = self.wait_for(fixture, session, lambda sample: sample.unsafe_reason is not None)
                self.assertIn("gpu", bad.unsafe_reason)
                policy = MineruStreamPolicy(StreamPolicyConfig(
                    qualified_max=5,
                    runtime_identity_sha256=fixture.binding.runtime_identity_sha256,
                    owner_identity_sha256=fixture.binding.owner_sha256,
                ))
                decision = policy.evaluate(bad, now=time.monotonic())
                self.assertEqual((decision.target, decision.unsafe), (0, True))
                if kinds[-1] == "rollback":
                    # The existing cache can latch clock drift without throwing
                    # from publish_gpu; require rejection, not a new close shape.
                    try:
                        session.close()
                    except BaseExceptionGroup:
                        pass
                else:
                    self.assertTrue(fixture.closed["gpu"].wait(1))
                    with self.assertRaises(BaseExceptionGroup):
                        session.close()
                self.assertEqual([kind for kind, _, _ in received], kinds)
                self.assertTrue(all(client.closed for client in fixture.clients))

    def test_legacy_capacity_sampler_keeps_strict_unavailable_rejection(self):
        sampler = capacity_sources.GpuCapacitySampler(
            url="http://gpu.invalid/metrics", timeout_seconds=0.2,
            expected_device_uuid=fixtures.GPU,
        )
        for kind in ("expired", "failed"):
            _, raw = gpu_frame(kind)
            with self.subTest(kind=kind), patch.object(capacity_sources, "_fetch_payload", return_value=raw):
                with self.assertRaises(ValueError):
                    sampler.sample()
        _, raw = gpu_frame("fresh")
        with patch.object(capacity_sources, "_fetch_payload", return_value=raw):
            self.assertEqual(sampler.sample().framebuffer_free_bytes, 7818182656)


if __name__ == "__main__":
    unittest.main()
