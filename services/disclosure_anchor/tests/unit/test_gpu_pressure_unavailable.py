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
from disclosure_anchor.adapters.runtime.bounded_http import BoundedHTTPResponse
from disclosure_anchor.adapters.runtime.gpu_telemetry_freshness import GpuTelemetryUnavailable
from disclosure_anchor.application.ports.mineru_stream_pressure import StreamSubmissionDeferred
from disclosure_anchor.application.services.mineru_stream_policy import (
    MineruStreamPolicy,
    StreamAdmissionControl,
    StreamPolicyConfig,
)
from tests.unit import test_mineru_stream_pressure_adapter as fixtures
from tests.unit.test_capacity_sources import _exporter_date, _gpu_health_only_payload

# The exporter host clock is deliberately far from this host's clock: only its
# own Date and last-success values may be compared with each other.
EXPORTER_CLOCK_OFFSET_SECONDS = -86_400 * 3 + 1.5


def gpu_frame(kind, *, prior_timestamp=None):
    """Return (last-success token, metrics bytes, Date values) of one response."""
    now = int(time.time() + EXPORTER_CLOCK_OFFSET_SECONDS)
    timestamp = float(now)
    dates = (_exporter_date(now),)
    if kind == "expired":
        timestamp -= 31.0
    elif kind == "rollback":
        timestamp = prior_timestamp - 31.0
    elif kind == "clock_unordered":
        # The exporter host clock stepped back between collection and response.
        timestamp += 2.0
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
    if kind == "upstream_failed":
        raw = _gpu_health_only_payload(timestamp - 5.0)
    if kind == "upstream_warmup":
        raw = _gpu_health_only_payload(None, failures=0)
    if kind == "health_only_success":
        raw = _gpu_health_only_payload(timestamp).replace(b"success 0", b"success 1")
    if kind in ("missing_date", "failed_missing_date"):
        dates = ()
    if kind == "duplicate_date":
        dates = dates * 2
    if kind == "obsolete_date":
        dates = (time.strftime("%A, %d-%b-%y %H:%M:%S GMT", time.gmtime(now)),)
    return timestamp, raw, dates


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
                original = client.get_response

                def read(path, *, response_headers, timeout_seconds, transport_attempts):
                    status = original(
                        path, response_headers=response_headers,
                        timeout_seconds=timeout_seconds, transport_attempts=transport_attempts,
                    ).status
                    kind = kinds[min(len(received), len(kinds) - 1)]
                    stamp, raw, dates = gpu_frame(kind, prior_timestamp=None if not received else received[0][1])
                    received.append((kind, stamp, raw))
                    return BoundedHTTPResponse(status=status, body=raw, headers={"Date": dates}, elapsed_seconds=0.001)

                client.get_response = read
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

    def test_expired_or_clock_unordered_sample_then_fresh_recovers_in_same_reader(self):
        # Both verdicts come from the exporter's own Date and last-success
        # values: over 30 s old, or dated before its own collection because
        # that host's clock stepped back. Neither is an identity fault.
        for kind in ("expired", "clock_unordered"):
            with self.subTest(kind=kind):
                self.recover_after_initial_unavailable(kind)

    def test_unsuccessful_collection_then_success_recovers_in_same_reader(self):
        # "failed" keeps device rows; the upstream 1.14.0 shapes after a failed
        # collection and before a first success carry only health families.
        for kind in ("failed", "upstream_failed", "upstream_warmup"):
            with self.subTest(kind=kind):
                self.recover_after_initial_unavailable(kind)

    def test_continuous_unknown_retains_old_value_without_refresh_and_policy_pauses(self):
        fixture, session, received = self.harness(["fresh", "upstream_failed", "upstream_failed"])
        with self.running(session):
            good = self.wait_for(fixture, session, lambda sample: sample.unknown_reason is None)
            now = [time.monotonic()]
            good_seen_at = now[0]
            policy = MineruStreamPolicy(StreamPolicyConfig(
                qualified_max=1,
                runtime_identity_sha256=fixture.binding.runtime_identity_sha256,
                owner_identity_sha256=fixture.binding.owner_sha256,
                host_pause_bytes=100, host_recover_bytes=500,
                missing_pause_seconds=10.0,
                recovery_seconds=0.1,
            ))
            self.assertEqual(policy.evaluate(good, now=now[0]).target, 0)
            # The cached joined observation remains inside the existing 3s
            # freshness window for this short configured recovery interval.
            warm_at = now[0] + 0.2
            self.assertEqual(policy.evaluate(good, now=warm_at).target, 1)
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
            first_unknown = max(now[0], warm_at)
            self.assertEqual(policy.evaluate(bad, now=first_unknown).target, 1)
            self.assertEqual(policy.evaluate(bad, now=first_unknown + 9.999).target, 1)
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

    def test_identity_clock_evidence_format_values_and_timestamp_rollback_remain_fatal(self):
        # Combined unsuccessful/invalid cases prevent a broad catch or an early
        # success=0 shortcut from hiding independent identity/protocol faults.
        # Clock evidence is protocol: a missing, repeated or obsolete-format
        # Date is never replaced by the reading host's wall clock.
        variants = (
            ["wrong_uuid"], ["failed_wrong_uuid"], ["failed_bad_value"],
            ["failed_bad_format"], ["health_only_success"], ["missing_date"],
            ["failed_missing_date"], ["duplicate_date"], ["obsolete_date"],
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

        def served(kind):
            _, raw, dates = gpu_frame(kind)
            return patch.object(
                capacity_sources, "_fetch_response",
                return_value=BoundedHTTPResponse(status=200, body=raw, headers={"Date": dates}, elapsed_seconds=0.001),
            )

        for kind in ("expired", "clock_unordered", "failed", "upstream_failed", "upstream_warmup"):
            with self.subTest(kind=kind), served(kind), self.assertRaises(GpuTelemetryUnavailable):
                sampler.sample()
        for kind in ("missing_date", "health_only_success", "wrong_uuid"):
            with self.subTest(kind=kind), served(kind), self.assertRaises(ValueError) as caught:
                sampler.sample()
            self.assertNotIsInstance(caught.exception, GpuTelemetryUnavailable)
        with served("fresh"):
            self.assertEqual(sampler.sample().framebuffer_free_bytes, 7818182656)


if __name__ == "__main__":
    unittest.main()
