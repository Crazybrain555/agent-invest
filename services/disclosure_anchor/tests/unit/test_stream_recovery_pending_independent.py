"""Independent regressions for recovery while useful HTTP demand remains queued."""

from dataclasses import replace
import unittest

from disclosure_anchor.application.ports.mineru_stream_pressure import (
    StreamPressureSample,
    StreamSubmissionDeferred,
)
from disclosure_anchor.application.services.mineru_stream_policy import (
    MineruStreamPolicy,
    StreamAdmissionControl,
    StreamPolicyConfig,
)


GIB = 1024**3
RUNTIME = "sha256:" + "a" * 64
OWNER = "sha256:" + "b" * 64


def observation(sequence: int, when: float, pending: int | None = 24) -> StreamPressureSample:
    return StreamPressureSample(
        sequence=sequence,
        observed_monotonic=when,
        runtime_identity_sha256=RUNTIME,
        owner_identity_sha256=OWNER,
        evidence_sha256="sha256:" + "c" * 64,
        gpu_free_bytes=2 * GIB,
        host_available_bytes=8 * GIB,
        http_active=14,
        http_pending=pending,
    )


def policy() -> MineruStreamPolicy:
    return MineruStreamPolicy(StreamPolicyConfig(
        qualified_max=7,
        runtime_identity_sha256=RUNTIME,
        owner_identity_sha256=OWNER,
    ))


class StreamRecoveryPendingIndependentTests(unittest.TestCase):
    def test_persistent_pending_recovers_one_slot_per_full_healthy_interval(self) -> None:
        for pending in (0, 1, 24, 1000):
            with self.subTest(pending=pending):
                subject = policy()
                self.assertEqual(subject.evaluate(observation(1, 0, pending), now=0).target, 7)
                paused = replace(observation(2, 1, pending), gpu_free_bytes=0)
                self.assertEqual(subject.evaluate(paused, now=1).target, 0)
                self.assertEqual(subject.evaluate(observation(3, 2, pending), now=2).target, 0)
                self.assertEqual(subject.evaluate(observation(4, 11.999, pending), now=11.999).target, 0)
                for slot in range(1, 8):
                    when = 2 + 10 * slot
                    decision = subject.evaluate(observation(slot + 4, when, pending), now=when)
                    self.assertEqual(decision.target, slot)
                    self.assertFalse(decision.unsafe)
                self.assertEqual(subject.evaluate(observation(20, 500, pending), now=500).target, 7)

    def test_delayed_poll_cannot_skip_recovery_steps_or_raise_the_certified_ceiling(self) -> None:
        subject = policy()
        subject.evaluate(observation(1, 0), now=0)
        subject.evaluate(replace(observation(2, 1), gpu_free_bytes=0), now=1)
        subject.evaluate(observation(3, 2), now=2)
        self.assertEqual(subject.evaluate(observation(4, 100), now=100).target, 1)
        self.assertEqual(subject.evaluate(observation(4, 100), now=100).target, 1)
        self.assertEqual(subject.evaluate(observation(5, 109.999), now=109.999).target, 1)
        self.assertEqual(subject.evaluate(observation(6, 110), now=110).target, 2)

    def test_unknown_or_resource_pressure_restarts_the_entire_recovery_interval(self) -> None:
        interruptions = (
            None,
            observation(4, 10, pending=None),
            replace(observation(4, 10), unknown_reason="GPU lane unavailable"),
            replace(observation(4, 10), gpu_free_bytes=GIB),
            replace(observation(4, 10), host_available_bytes=5 * GIB),
            observation(4, 6),  # At t=10 this sample is stale.
        )
        for interruption in interruptions:
            with self.subTest(interruption=interruption):
                subject = policy()
                subject.evaluate(observation(1, 0), now=0)
                subject.evaluate(replace(observation(2, 1), gpu_free_bytes=0), now=1)
                subject.evaluate(observation(3, 2), now=2)
                self.assertEqual(subject.evaluate(interruption, now=10).target, 0)
                self.assertEqual(subject.evaluate(observation(5, 11), now=11).target, 0)
                self.assertEqual(subject.evaluate(observation(6, 20.999), now=20.999).target, 0)
                self.assertEqual(subject.evaluate(observation(7, 21), now=21).target, 1)

    def test_drift_at_recovery_boundary_remains_latched_closed(self) -> None:
        tampered = (
            replace(observation(4, 12), runtime_identity_sha256="sha256:" + "d" * 64),
            replace(observation(4, 12), owner_identity_sha256="sha256:" + "d" * 64),
            observation(3, 2, pending=25),  # Same sequence with changed contents.
            observation(4, 13),  # Sample claims to originate in the future.
            replace(observation(4, 12), unsafe_reason="oom counter changed"),
        )
        for bad in tampered:
            with self.subTest(bad=bad):
                subject = policy()
                subject.evaluate(observation(1, 0), now=0)
                subject.evaluate(replace(observation(2, 1), gpu_free_bytes=0), now=1)
                subject.evaluate(observation(3, 2), now=2)
                decision = subject.evaluate(bad, now=12)
                self.assertTrue(decision.unsafe)
                self.assertEqual(decision.target, 0)
                self.assertEqual(subject.evaluate(observation(10, 30), now=30).target, 0)

    def test_recovered_positive_target_reaches_post_guard_and_new_pressure_still_blocks(self) -> None:
        class Pressure:
            value = observation(1, 0)

            def latest(self) -> StreamPressureSample:
                return self.value

        pressure = Pressure()
        now = [0.0]
        control = StreamAdmissionControl(policy(), pressure, monotonic=lambda: now[0])
        self.assertEqual(control.current().target, 7)
        pressure.value = replace(observation(2, 1), gpu_free_bytes=0)
        now[0] = 1
        with self.assertRaises(StreamSubmissionDeferred):
            control.assert_submission_allowed(runtime_identity_sha256=RUNTIME)
        pressure.value = observation(3, 2)
        now[0] = 2
        self.assertEqual(control.current().target, 0)
        pressure.value = observation(4, 12)
        now[0] = 12
        self.assertEqual(control.current().target, 1)
        control.assert_submission_allowed(runtime_identity_sha256=RUNTIME)
        pressure.value = replace(observation(5, 12), host_available_bytes=GIB)
        with self.assertRaises(StreamSubmissionDeferred):
            control.assert_submission_allowed(runtime_identity_sha256=RUNTIME)
