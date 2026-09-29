from dataclasses import replace
import unittest

from disclosure_anchor.application.ports.mineru_stream_pressure import (
    StreamPressureSample,
    StreamSubmissionDeferred,
)
from disclosure_anchor.application.services.mineru_stream_policy import (
    STREAM_POLICY_ALGORITHM_V2,
    MineruStreamPolicy,
    StreamAdmissionControl,
    StreamPolicyConfig,
)

RUNTIME = "sha256:" + "a" * 64
OWNER = "sha256:" + "b" * 64
EVIDENCE = "sha256:" + "c" * 64
GIB = 1024**3
MIB = 1024**2


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
            provider_nonterminal_tasks=0,
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


def warm(policy: MineruStreamPolicy, target: int, *, step: float = 1.0) -> tuple[float, int]:
    """Feed continuous fresh healthy samples until ``target`` is held.

    Returns the last observation time and sequence. Samples are ``step`` apart,
    inside the default three-second maximum age, so the window is attested.
    """

    now, sequence = 0.0, 1
    decision = policy.evaluate(sample(sequence, now), now=now)
    while decision.target < target:
        now += step
        sequence += 1
        decision = policy.evaluate(sample(sequence, now), now=now)
    return now, sequence


class Pressure:
    def __init__(self, value: StreamPressureSample | None) -> None:
        self.value = value

    def latest(self) -> StreamPressureSample | None:
        return self.value


class MineruStreamPolicyTests(unittest.TestCase):
    def test_unknown_start_never_creates_a_qualified_permit(self) -> None:
        policy = MineruStreamPolicy(config())
        self.assertEqual(policy.algorithm, STREAM_POLICY_ALGORITHM_V2)
        decision = policy.evaluate(None, now=0)
        self.assertEqual((decision.target, decision.new_post_allowed), (0, False))
        self.assertIsNone(decision.evidence_sha256)
        # A healthy cold start is a recovery from zero, never a qualified jump.
        decision = policy.evaluate(sample(1, 1), now=1)
        self.assertEqual((decision.target, decision.new_post_allowed), (0, False))
        self.assertEqual(decision.evidence_sha256, EVIDENCE)
        for sequence, now in enumerate((3, 5, 7, 9, 10.999), start=2):
            self.assertEqual(policy.evaluate(sample(sequence, now), now=now).target, 0)
        decision = policy.evaluate(sample(7, 11), now=11)
        self.assertEqual((decision.target, decision.reason, decision.new_post_allowed), (1, "recovery", True))
        # Below the recover floor with provably no provider work: an explicit
        # deficit that keeps zero; unknown provider work is not idleness.
        for provider, http_active, reason in ((0, 0, "idle_memory_deficit"), (None, 0, "holding_pressure"),
                                              (1, 0, "holding_pressure"), (0, 1, "holding_pressure")):
            with self.subTest(provider=provider, http_active=http_active):
                cold = MineruStreamPolicy(config())
                decision = cold.evaluate(
                    sample(1, 0, gpu_free_bytes=1065 * MIB, provider_nonterminal_tasks=provider,
                           http_active=http_active), now=0,
                )
                self.assertEqual((decision.target, decision.reason, decision.new_post_allowed), (0, reason, False))

    def test_missing_stale_and_error_samples_hold_then_pause_without_growth(self) -> None:
        for name, changes, observed_offset in (
            ("missing", None, 0.0),
            ("gpu", {"gpu_free_bytes": None}, 0.0),
            ("host", {"host_available_bytes": None}, 0.0),
            ("active", {"http_active": None}, 0.0),
            ("pending", {"http_pending": None}, 0.0),
            ("reason", {"unknown_reason": "one lane timed out"}, 0.0),
            ("stale", {}, 0.0),  # Four seconds old at its first evaluation.
        ):
            with self.subTest(sample=name):
                policy = MineruStreamPolicy(config())
                end, sequence = warm(policy, 4)
                bad = None if changes is None else sample(sequence + 1, end + observed_offset, **changes)
                held = policy.evaluate(bad, now=end + 4)
                self.assertEqual((held.target, held.unsafe, held.new_post_allowed), (4, False, False))
                self.assertEqual(policy.evaluate(bad, now=end + 13.9).target, 4)
                self.assertEqual(policy.evaluate(bad, now=end + 14).target, 0)

    def test_reduction_is_bounded_and_healthy_recovery_requires_clear_time(self) -> None:
        policy = MineruStreamPolicy(config(recovery_seconds=2))
        base, sequence = warm(policy, 4)
        steps = (
            (1, {"gpu_free_bytes": GIB - 1}, 3, False),
            (1.5, {"gpu_free_bytes": GIB - 1}, 3, False),
            (3, {"gpu_free_bytes": GIB - 1}, 2, False),
            (4, {"host_available_bytes": 3 * GIB}, 0, False),
            (5, {}, 0, False),
            (6, {"http_pending": 50}, 0, False),
            (7, {}, 1, True),
            (8, {}, 1, True),
            (9, {}, 2, True),
        )
        for offset, changes, expected, allowed in steps:
            sequence += 1
            decision = policy.evaluate(sample(sequence, base + offset, **changes), now=base + offset)
            self.assertEqual((decision.target, decision.new_post_allowed), (expected, allowed), offset)
        previous = 2
        for tick in range(10, 25):
            sequence += 1
            target = policy.evaluate(sample(sequence, base + tick), now=base + tick).target
            self.assertIn(target - previous, (0, 1))
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
        # A gap between fresh samples wider than their maximum age is unobserved
        # time and restarts the window even though both endpoints are healthy.
        self.assertEqual(policy.evaluate(sample(7, 7), now=7).target, 1)
        self.assertEqual(policy.evaluate(sample(8, 11), now=11).target, 1)
        self.assertEqual(policy.evaluate(sample(9, 12.999), now=12.999).target, 1)
        self.assertEqual(policy.evaluate(sample(10, 13), now=13).target, 2)

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
        # A partial startup sample has no joined time; it never anchors the
        # clock ordering, and every fault after the first join still latches.
        partial = sample(1, 0, observed_monotonic=None, gpu_free_bytes=None,
                         unknown_reason="gpu_unavailable_or_stale")
        for bad in bad_samples:
            for partial_start in (False, True):
                with self.subTest(sample=bad, partial_start=partial_start):
                    policy = MineruStreamPolicy(config())
                    if partial_start:
                        self.assertEqual(policy.evaluate(partial, now=0.5).reason, "pressure_unknown")
                    policy.evaluate(baseline, now=1)
                    decision = policy.evaluate(bad, now=2)
                    self.assertEqual(decision.target, 0)
                    self.assertTrue(decision.unsafe)
                    recovered = policy.evaluate(sample(20, 100), now=100)
                    self.assertEqual(recovered.target, 0)
                    self.assertTrue(recovered.unsafe)
        policy = MineruStreamPolicy(config())
        policy.evaluate(partial, now=1)
        self.assertFalse(policy.evaluate(sample(2, -2.0), now=1).unsafe)
        self.assertEqual(policy.evaluate(sample(3, -2.5), now=1).reason, "unsafe:sample_clock_drift")

    def test_post_guard_rechecks_pause_but_does_not_revoke_positive_held_credit(self) -> None:
        policy = MineruStreamPolicy(config())
        end, sequence = warm(policy, 4)
        clock = [end]
        pressure = Pressure(sample(sequence, end))
        control = StreamAdmissionControl(policy, pressure, monotonic=lambda: clock[0])
        self.assertEqual(control.current().target, 4)
        control.assert_submission_allowed(runtime_identity_sha256=RUNTIME)
        # A lowered target never revokes held permits, but reduction closes new POSTs.
        pressure.value = sample(sequence + 1, end, gpu_free_bytes=GIB - 1)
        self.assertEqual(control.current().target, 3)
        with self.assertRaises(StreamSubmissionDeferred):
            control.assert_submission_allowed(runtime_identity_sha256=RUNTIME)
        # A fresh holding-band sample keeps the held target and may admit within it.
        pressure.value = sample(sequence + 2, end, gpu_free_bytes=1200 * MIB)
        decision = control.current()
        self.assertEqual((decision.target, decision.reason, decision.new_post_allowed), (3, "holding_pressure", True))
        control.assert_submission_allowed(runtime_identity_sha256=RUNTIME)
        # An unknown grace keeps the held target but never authorizes a new POST.
        pressure.value = None
        clock[0] = end + 1
        self.assertEqual(control.current().target, 3)
        with self.assertRaises(StreamSubmissionDeferred):
            control.assert_submission_allowed(runtime_identity_sha256=RUNTIME)
        pressure.value = sample(sequence + 3, end + 1, gpu_free_bytes=0)
        with self.assertRaises(StreamSubmissionDeferred):
            control.assert_submission_allowed(runtime_identity_sha256=RUNTIME)
        with self.assertRaises(ValueError):
            control.assert_submission_allowed(runtime_identity_sha256="sha256:"+"d"*64)


if __name__ == "__main__":
    unittest.main()
