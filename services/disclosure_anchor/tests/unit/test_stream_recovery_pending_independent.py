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
    StreamAdmissionDecision,
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
        provider_nonterminal_tasks=1,
    )


def policy() -> MineruStreamPolicy:
    return MineruStreamPolicy(StreamPolicyConfig(
        qualified_max=7,
        runtime_identity_sha256=RUNTIME,
        owner_identity_sha256=OWNER,
    ))


def healthy_through(
    subject: MineruStreamPolicy, *, sequence: int, start: float,
    stop: float, pending: int | None = 24,
) -> tuple[StreamAdmissionDecision, int]:
    """Supply fresh observations no more than two seconds apart, through stop."""
    when = start
    while True:
        decision = subject.evaluate(observation(sequence, when, pending), now=when)
        sequence += 1
        if when == stop:
            return decision, sequence
        when = min(when + 2, stop)


class StreamRecoveryPendingIndependentTests(unittest.TestCase):
    def test_persistent_pending_recovers_one_slot_per_full_healthy_interval(self) -> None:
        for pending in (0, 1, 24, 1000):
            with self.subTest(pending=pending):
                subject = policy()
                self.assertEqual(subject.evaluate(observation(1, 0, pending), now=0).target, 0)
                paused = replace(observation(2, 1, pending), gpu_free_bytes=0)
                self.assertEqual(subject.evaluate(paused, now=1).target, 0)
                before, sequence = healthy_through(
                    subject, sequence=3, start=2, stop=11.999, pending=pending,
                )
                self.assertEqual(before.target, 0)
                first = subject.evaluate(observation(sequence, 12, pending), now=12)
                self.assertEqual(first.target, 1)
                sequence += 1
                for slot in range(2, 8):
                    when = 2 + 10 * slot
                    decision, sequence = healthy_through(
                        subject, sequence=sequence, start=when - 8,
                        stop=when, pending=pending,
                    )
                    self.assertEqual(decision.target, slot)
                    self.assertFalse(decision.unsafe)
                self.assertEqual(subject.evaluate(observation(sequence, 500, pending), now=500).target, 7)

    def test_delayed_poll_cannot_skip_recovery_steps_or_raise_the_certified_ceiling(self) -> None:
        subject = policy()
        self.assertEqual(subject.evaluate(observation(1, 0), now=0).target, 0)
        subject.evaluate(replace(observation(2, 1), gpu_free_bytes=0), now=1)
        recovered, sequence = healthy_through(subject, sequence=3, start=2, stop=12)
        self.assertEqual(recovered.target, 1)
        delayed = observation(sequence, 100)
        self.assertEqual(subject.evaluate(delayed, now=100).target, 1)
        self.assertEqual(subject.evaluate(delayed, now=100).target, 1)
        before, sequence = healthy_through(subject, sequence=sequence + 1, start=102, stop=109.999)
        self.assertEqual(before.target, 1)
        self.assertEqual(subject.evaluate(observation(sequence, 110), now=110).target, 2)

    def test_unknown_or_resource_pressure_restarts_the_entire_recovery_interval(self) -> None:
        interruptions = (
            None,
            observation(6, 10, pending=None),
            replace(observation(6, 10), unknown_reason="GPU lane unavailable"),
            replace(observation(6, 10), gpu_free_bytes=GIB),
            replace(observation(6, 10), host_available_bytes=5 * GIB),
            observation(5, 6),  # At t=10 this unchanged sample is stale.
        )
        for interruption in interruptions:
            with self.subTest(interruption=interruption):
                subject = policy()
                self.assertEqual(subject.evaluate(observation(1, 0), now=0).target, 0)
                subject.evaluate(replace(observation(2, 1), gpu_free_bytes=0), now=1)
                before, _ = healthy_through(subject, sequence=3, start=2, stop=6)
                self.assertEqual(before.target, 0)
                self.assertEqual(subject.evaluate(interruption, now=10).target, 0)
                before, sequence = healthy_through(subject, sequence=7, start=11, stop=20.999)
                self.assertEqual(before.target, 0)
                self.assertEqual(subject.evaluate(observation(sequence, 21), now=21).target, 1)

    def test_drift_at_recovery_boundary_remains_latched_closed(self) -> None:
        for kind in ("runtime", "owner", "same_sequence", "future", "oom"):
            with self.subTest(kind=kind):
                subject = policy()
                subject.evaluate(observation(1, 0), now=0)
                subject.evaluate(replace(observation(2, 1), gpu_free_bytes=0), now=1)
                recovered, sequence = healthy_through(subject, sequence=3, start=2, stop=12)
                self.assertEqual(recovered.target, 1)
                tampered = {
                    "runtime": replace(observation(sequence, 13), runtime_identity_sha256="sha256:" + "d" * 64),
                    "owner": replace(observation(sequence, 13), owner_identity_sha256="sha256:" + "d" * 64),
                    "same_sequence": observation(sequence - 1, 12, pending=25),
                    "future": observation(sequence, 14),
                    "oom": replace(observation(sequence, 13), unsafe_reason="oom counter changed"),
                }
                decision = subject.evaluate(tampered[kind], now=13)
                self.assertTrue(decision.unsafe)
                self.assertEqual(decision.target, 0)
                self.assertEqual(subject.evaluate(observation(sequence + 1, 30), now=30).target, 0)

    def test_recovered_positive_target_reaches_post_guard_and_new_pressure_still_blocks(self) -> None:
        class Pressure:
            value = observation(1, 0)

            def latest(self) -> StreamPressureSample:
                return self.value

        pressure = Pressure()
        now = [0.0]
        control = StreamAdmissionControl(policy(), pressure, monotonic=lambda: now[0])
        self.assertEqual(control.current().target, 0)
        pressure.value = replace(observation(2, 1), gpu_free_bytes=0)
        now[0] = 1
        with self.assertRaises(StreamSubmissionDeferred):
            control.assert_submission_allowed(runtime_identity_sha256=RUNTIME)
        for sequence, when in enumerate((2, 4, 6, 8, 10, 12), start=3):
            pressure.value = observation(sequence, when)
            now[0] = float(when)
            decision = control.current()
        self.assertEqual(decision.target, 1)
        control.assert_submission_allowed(runtime_identity_sha256=RUNTIME)
        pressure.value = replace(observation(9, 12), host_available_bytes=GIB)
        with self.assertRaises(StreamSubmissionDeferred):
            control.assert_submission_allowed(runtime_identity_sha256=RUNTIME)
