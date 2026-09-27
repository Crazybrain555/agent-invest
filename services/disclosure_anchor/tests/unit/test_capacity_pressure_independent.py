"""Independent no-runtime pressure regressions for the capacity recovery boundary."""

from __future__ import annotations

from dataclasses import fields
from typing import Any, cast
import unittest

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.http_remote_v4 import MinerUHttpRemoteV4
from disclosure_anchor.adapters.runtime.mineru_stream_pressure import StreamPressureCache
from disclosure_anchor.application.ports.mineru_stream_pressure import (
    StreamPressureSample,
    StreamSubmissionDeferred,
)
from disclosure_anchor.application.services.mineru_stream_policy import (
    MineruStreamPolicy,
    StreamAdmissionControl,
    StreamPolicyConfig,
)
from tests._mineru_capacity_config_fixture import canonical_payload
from tests.unit.test_mineru_stream_pressure_adapter import examples, gpu_payload
from tests.unit import test_mineru_http_remote_v4 as wire_fixtures


MIB = 1024**2
GIB = 1024**3
RUNTIME = "sha256:" + "a" * 64
OWNER = "sha256:" + "b" * 64
EVIDENCE = "sha256:" + "c" * 64
_SAMPLE_FIELDS = {field.name for field in fields(StreamPressureSample)}


def _sample(
    sequence: int,
    when: float,
    *,
    gpu_mib: int = 1536,
    provider_tasks: int | None = 0,
    **changes: object,
) -> StreamPressureSample:
    values: dict[str, object] = {
        "sequence": sequence,
        "observed_monotonic": when,
        "runtime_identity_sha256": RUNTIME,
        "owner_identity_sha256": OWNER,
        "evidence_sha256": EVIDENCE,
        "gpu_free_bytes": gpu_mib * MIB,
        "host_available_bytes": 8 * GIB,
        "http_active": 0,
        "http_pending": 0,
    }
    if "provider_nonterminal_tasks" in _SAMPLE_FIELDS:
        values["provider_nonterminal_tasks"] = provider_tasks
    values.update(changes)
    return StreamPressureSample(**cast(Any, values))


def _policy() -> MineruStreamPolicy:
    return MineruStreamPolicy(StreamPolicyConfig(
        qualified_max=14,
        runtime_identity_sha256=RUNTIME,
        owner_identity_sha256=OWNER,
    ))


class _Pressure:
    def __init__(self, sample: StreamPressureSample | None) -> None:
        self.sample = sample

    def latest(self) -> StreamPressureSample | None:
        return self.sample


class CapacityPressureIndependentTests(unittest.TestCase):
    def test_low_margin_cold_and_warm_stay_closed_even_when_http_and_provider_idle(self) -> None:
        for gpu_mib in (842, 1065):
            with self.subTest(gpu_mib=gpu_mib, phase="cold"):
                subject = _policy()
                self.assertEqual(subject.evaluate(_sample(1, 0, gpu_mib=gpu_mib), now=0).target, 0)
            with self.subTest(gpu_mib=gpu_mib, phase="warm"):
                subject = _policy()
                self.assertEqual(subject.evaluate(_sample(1, 0, gpu_mib=400), now=0).target, 0)
                self.assertEqual(subject.evaluate(_sample(2, 1, gpu_mib=gpu_mib), now=1).target, 0)
                self.assertEqual(subject.evaluate(_sample(3, 20, gpu_mib=gpu_mib), now=20).target, 0)

    def test_only_fresh_original_recovery_floor_for_ten_seconds_opens_one_slot(self) -> None:
        subject = _policy()
        # Two-second observations are merely a witness inside the configured
        # three-second maximum age; this does not prescribe a polling cadence.
        observations = (
            (0.0, 0), (2.0, 0), (4.0, 0), (6.0, 0), (8.0, 0),
            (9.999, 0), (10.0, 1), (10.001, 1),
            (12.0, 1), (14.0, 1), (16.0, 1), (18.0, 1), (20.0, 2),
        )
        for sequence, (when, expected) in enumerate(observations, start=1):
            with self.subTest(when=when):
                decision = subject.evaluate(_sample(sequence, when), now=when)
                self.assertEqual(decision.target, expected)
                self.assertLess(decision.target, 14)

    def test_observed_missing_or_stale_interval_restarts_the_recovery_window(self) -> None:
        for name, interrupted_at, interruption, resumed_at in (
            ("missing", 5.0, None, 6.0),
            ("stale", 7.1, _sample(3, 4.0), 8.0),
        ):
            with self.subTest(name=name):
                subject = _policy()
                for sequence, when in enumerate((0.0, 2.0, 4.0), start=1):
                    self.assertEqual(subject.evaluate(_sample(sequence, when), now=when).target, 0)
                interrupted = subject.evaluate(interruption, now=interrupted_at)
                self.assertEqual(interrupted.target, 0)
                self.assertFalse(interrupted.unsafe)
                for sequence, when in enumerate(
                    (resumed_at, resumed_at + 2, resumed_at + 4,
                     resumed_at + 6, resumed_at + 8, resumed_at + 9.999),
                    start=4,
                ):
                    self.assertEqual(subject.evaluate(_sample(sequence, when), now=when).target, 0)
                self.assertEqual(
                    subject.evaluate(_sample(10, resumed_at + 10), now=resumed_at + 10).target,
                    1,
                )

    def test_unobserved_gap_beyond_max_sample_age_earns_no_recovery_credit(self) -> None:
        subject = _policy()
        self.assertLess(subject.config.sample_max_age_seconds, 20.0)
        self.assertEqual(subject.evaluate(_sample(1, 0.0), now=0.0).target, 0)
        # Both endpoint samples are fresh. Nothing attests to the interval
        # between them, so a new POST cannot inherit ten healthy seconds.
        after_gap = subject.evaluate(_sample(2, 20.0), now=20.0)
        self.assertEqual(after_gap.target, 0)
        self.assertFalse(after_gap.new_post_allowed)
        for sequence, when in enumerate((22.0, 24.0, 26.0, 28.0, 29.999), start=3):
            self.assertEqual(subject.evaluate(_sample(sequence, when), now=when).target, 0)
        self.assertEqual(subject.evaluate(_sample(8, 30.0), now=30.0).target, 1)

    def test_provider_idle_is_a_real_fresh_health_fact_not_inferred_from_http_zero(self) -> None:
        self.assertIn("provider_nonterminal_tasks", _SAMPLE_FIELDS)
        for provider_tasks in (None, 1):
            with self.subTest(provider_tasks=provider_tasks):
                subject = _policy()
                first = subject.evaluate(_sample(1, 0, gpu_mib=1065, provider_tasks=provider_tasks), now=0)
                later = subject.evaluate(_sample(2, 20, gpu_mib=1065, provider_tasks=provider_tasks), now=20)
                self.assertEqual((first.target, later.target), (0, 0))
        missing = _policy().evaluate(_sample(1, 0, provider_tasks=None), now=0)
        self.assertEqual(missing.target, 0)

    def test_health_projection_preserves_provider_activity_and_changes_evidence(self) -> None:
        binding, health, pressure = cast(Any, examples)()
        now = [100.0]
        cache = StreamPressureCache(binding, monotonic=lambda: now[0])
        cache.publish_api(cast(Any, canonical_payload)(health), cast(Any, canonical_payload)(pressure), started=100, finished=100.1)
        cache.publish_gpu(cast(Any, gpu_payload)(1000.0), started=100, finished=100.1, received_wall=1000.0)
        idle = cache.latest()
        self.assertEqual(getattr(idle, "provider_nonterminal_tasks", None), 0)
        health["processing_tasks"] = 1
        health["task_admission"].update(
            accepted_processing_tasks=1, durable_nonterminal_tasks=1,
            scheduled_tasks=1, active_processors=1,
        )
        health["capacity_observation"]["stage_counters"]["parse_active"] = 1
        pressure["observed_at"].update(started_ns=2000, completed_ns=2100)
        now[0] = 101.0
        cache.publish_api(cast(Any, canonical_payload)(health), cast(Any, canonical_payload)(pressure), started=101, finished=101.1)
        active = cache.latest()
        self.assertEqual(active.http_active, 0)
        self.assertEqual(active.http_pending, 0)
        self.assertEqual(getattr(active, "provider_nonterminal_tasks", None), 1)
        self.assertNotEqual(active.evidence_sha256, idle.evidence_sha256)

    def test_unknown_and_unsafe_never_turn_a_held_target_into_new_post_permission(self) -> None:
        for name, replacement, check_at in (
            ("missing", None, 11.0),
            ("unknown", _sample(7, 11, unknown_reason="API unavailable"), 11.0),
            ("stale", _sample(6, 10), 14.1),
        ):
            with self.subTest(name=name):
                local_clock = [0.0]
                local_pressure = _Pressure(replacement)
                local = StreamAdmissionControl(_policy(), local_pressure, monotonic=lambda: local_clock[0])
                local_pressure.sample = _sample(1, 0)
                self.assertEqual(local.current().target, 0)
                for sequence, when in enumerate((2, 4, 6, 8, 10), start=2):
                    local_clock[0] = float(when)
                    local_pressure.sample = _sample(sequence, float(when))
                    decision = local.current()
                self.assertEqual(decision.target, 1)
                local_clock[0] = check_at
                local_pressure.sample = replacement
                with self.assertRaises(StreamSubmissionDeferred):
                    local.assert_submission_allowed(runtime_identity_sha256=RUNTIME)
        for name, bad in (
            ("sequence_reset", _sample(1, 11)),
            ("changed_identity", _sample(7, 11, owner_identity_sha256="sha256:" + "d" * 64)),
            ("oom", _sample(7, 11, unsafe_reason="new_cgroup_oom")),
        ):
            with self.subTest(name=name):
                local_clock = [0.0]
                local_pressure = _Pressure(_sample(1, 0))
                local = StreamAdmissionControl(_policy(), local_pressure, monotonic=lambda: local_clock[0])
                local.current()
                for sequence, when in enumerate((2, 4, 6, 8, 10), start=2):
                    local_clock[0] = float(when)
                    local_pressure.sample = _sample(sequence, float(when))
                    decision = local.current()
                self.assertEqual(decision.target, 1)
                local_clock[0] = 11
                local_pressure.sample = bad
                with self.assertRaises(StreamSubmissionDeferred):
                    local.assert_submission_allowed(runtime_identity_sha256=RUNTIME)
                local_clock[0] = 30
                local_pressure.sample = _sample(8, 30)
                with self.assertRaises(StreamSubmissionDeferred):
                    local.assert_submission_allowed(runtime_identity_sha256=RUNTIME)

    def test_accepted_tail_can_be_reconciled_and_polled_while_new_post_is_closed(self) -> None:
        wire = wire_fixtures.MinerUHttpRemoteV4Tests()
        wire.setUp()
        self.addCleanup(wire.tearDown)
        command = wire._submission_command()
        clock = [0.0]
        pressure = _Pressure(_sample(1, 0, gpu_mib=842))
        control = StreamAdmissionControl(_policy(), pressure, monotonic=lambda: clock[0])
        self.assertEqual(control.current().target, 0)
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method + " " + request.url.path)
            if "/by-idempotency/" in request.url.path:
                return httpx.Response(200, json=wire._task_payload("processing"))
            self.assertEqual(request.url.path, "/tasks/task-1")
            return httpx.Response(200, json=wire._task_payload("processing"))

        with MinerUHttpRemoteV4(
            transport=httpx.MockTransport(handler), token_factory=lambda count: b"s" * count,
            wall_clock=lambda: 10_000.0, request_timeout_seconds=30.0,
            submission_guard=control,
        ) as provider:
            accepted = provider.reconcile_or_submit(command)
            provider.poll_once(wire._poll_command(accepted))
        self.assertIsNone(accepted.absence_proof)
        self.assertEqual(methods, ["GET /tasks/by-idempotency/" + wire.key, "GET /tasks/task-1"])


if __name__ == "__main__":
    unittest.main()
