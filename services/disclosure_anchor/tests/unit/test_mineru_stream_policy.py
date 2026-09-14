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

RUNTIME = "sha256:" + "a" * 64
OWNER = "sha256:" + "b" * 64
EVIDENCE = "sha256:" + "c" * 64
GIB = 1024**3


def sample(sequence: int, now: float, **changes: object) -> StreamPressureSample:
    return replace(
        StreamPressureSample(
            sequence=sequence,
            observed_monotonic=now,
            runtime_identity_sha256=RUNTIME,
            owner_identity_sha256=OWNER,
            evidence_sha256=EVIDENCE,
            gpu_free_bytes=2 * GIB,
            host_available_bytes=8 * GIB,
            http_active=14,
            http_pending=0,
        ),
        **changes,
    )


def config(**changes: object) -> StreamPolicyConfig:
    return replace(
        StreamPolicyConfig(
            qualified_max=4,
            runtime_identity_sha256=RUNTIME,
            owner_identity_sha256=OWNER,
        ),
        **changes,
    )


class Pressure:
    def __init__(self, value: StreamPressureSample | None) -> None:
        self.value = value

    def latest(self) -> StreamPressureSample | None:
        return self.value


class MineruStreamPolicyTests(unittest.TestCase):
    def test_unknown_start_never_creates_a_qualified_permit(self) -> None:
        policy = MineruStreamPolicy(config())
        decision = policy.evaluate(None, now=0)
        self.assertEqual(decision.target, 0)
        self.assertIsNone(decision.evidence_sha256)
        decision = policy.evaluate(sample(1, 1), now=1)
        self.assertEqual(decision.target, 4)
        self.assertEqual(decision.evidence_sha256, EVIDENCE)

    def test_missing_stale_and_error_samples_hold_then_pause_without_growth(self) -> None:
        bad_samples = (
            None,
            sample(2, 1, gpu_free_bytes=None),
            sample(2, 1, host_available_bytes=None),
            sample(2, 1, http_active=None),
            sample(2, 1, http_pending=None),
            sample(2, 1, unknown_reason="one lane timed out"),
            sample(2, 0),  # Four seconds old at first evaluation.
        )
        for bad in bad_samples:
            with self.subTest(sample=bad):
                policy = MineruStreamPolicy(config())
                self.assertEqual(policy.evaluate(sample(1, 0), now=0).target, 4)
                held = policy.evaluate(bad, now=4)
                self.assertEqual(held.target, 4)
                self.assertFalse(held.unsafe)
                self.assertEqual(policy.evaluate(bad, now=13.9).target, 4)
                self.assertEqual(policy.evaluate(bad, now=14).target, 0)

    def test_reduction_is_bounded_and_healthy_recovery_requires_clear_time(self) -> None:
        policy = MineruStreamPolicy(config(recovery_seconds=2))
        self.assertEqual(policy.evaluate(sample(1, 0), now=0).target, 4)
        self.assertEqual(policy.evaluate(sample(2, 1, gpu_free_bytes=GIB-1), now=1).target, 3)
        self.assertEqual(policy.evaluate(sample(3, 1.5, gpu_free_bytes=GIB-1), now=1.5).target, 3)
        self.assertEqual(policy.evaluate(sample(4, 3, gpu_free_bytes=GIB-1), now=3).target, 2)
        self.assertEqual(policy.evaluate(sample(5, 4, host_available_bytes=3*GIB), now=4).target, 0)
        self.assertEqual(policy.evaluate(sample(6, 5), now=5).target, 0)
        self.assertEqual(policy.evaluate(sample(7, 6, http_pending=50), now=6).target, 0)
        self.assertEqual(policy.evaluate(sample(8, 7), now=7).target, 0)
        self.assertEqual(policy.evaluate(sample(9, 8), now=8).target, 0)
        self.assertEqual(policy.evaluate(sample(10, 9), now=9).target, 1)
        previous = 1
        for tick in range(10, 25):
            target = policy.evaluate(sample(tick+1, tick), now=tick).target
            self.assertIn(target-previous, (0, 1))
            self.assertLessEqual(target, 4)
            previous = target
        self.assertEqual(previous, 4)

    def test_unknown_interrupts_recovery_and_severe_gpu_pressure_pauses(self) -> None:
        policy = MineruStreamPolicy(config(recovery_seconds=2))
        policy.evaluate(sample(1, 0), now=0)
        self.assertEqual(policy.evaluate(sample(2, 1, gpu_free_bytes=0), now=1).target, 0)
        policy.evaluate(sample(3, 2), now=2)
        policy.evaluate(None, now=3)
        self.assertEqual(policy.evaluate(sample(4, 4), now=4).target, 0)
        self.assertEqual(policy.evaluate(sample(5, 5), now=5).target, 0)
        self.assertEqual(policy.evaluate(sample(6, 6), now=6).target, 1)

    def test_identity_sequence_and_clock_drift_are_latched_without_auto_resume(self) -> None:
        baseline = sample(2, 1)
        bad_samples = (
            sample(3, 2, runtime_identity_sha256="sha256:"+"d"*64),
            sample(3, 2, owner_identity_sha256="sha256:"+"d"*64),
            sample(1, 2),
            replace(baseline, http_pending=1),
            sample(3, 0),
            sample(3, 3),  # Future relative to the observing clock.
            sample(3, 2, unsafe_reason="source owner changed"),
        )
        for bad in bad_samples:
            with self.subTest(sample=bad):
                policy = MineruStreamPolicy(config())
                policy.evaluate(baseline, now=1)
                decision = policy.evaluate(bad, now=2)
                self.assertEqual(decision.target, 0)
                self.assertTrue(decision.unsafe)
                recovered = policy.evaluate(sample(20, 100), now=100)
                self.assertEqual(recovered.target, 0)
                self.assertTrue(recovered.unsafe)

    def test_post_guard_rechecks_pause_but_does_not_revoke_positive_held_credit(self) -> None:
        pressure = Pressure(sample(1, 10))
        control = StreamAdmissionControl(MineruStreamPolicy(config()), pressure, monotonic=lambda: 10)
        self.assertEqual(control.current().target, 4)
        pressure.value = sample(2, 10, gpu_free_bytes=GIB-1)
        self.assertEqual(control.current().target, 3)
        control.assert_submission_allowed(runtime_identity_sha256=RUNTIME)
        pressure.value = sample(3, 10, gpu_free_bytes=0)
        with self.assertRaises(StreamSubmissionDeferred):
            control.assert_submission_allowed(runtime_identity_sha256=RUNTIME)
        with self.assertRaises(ValueError):
            control.assert_submission_allowed(runtime_identity_sha256="sha256:"+"d"*64)


if __name__ == "__main__":
    unittest.main()
