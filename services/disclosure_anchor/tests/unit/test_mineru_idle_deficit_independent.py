"""Independent bounded-deficit acceptance: real policy/coordinator, disposable IO."""

from __future__ import annotations

import unittest

from disclosure_anchor.adapters.runtime.worker_stop_control import (
    RuntimeWorkerStopControl,
    WorkerControlStore,
)
from disclosure_anchor.application.services.mineru_stream_policy import (
    MineruStreamPolicy,
    StreamAdmissionControl,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorTerminal,
    StagedParseCoordinator,
)
from tests.unit._f5_stop_fixture import settings_for, trusted_root
from tests.unit.test_mineru_stream_policy import GIB, MIB, config, sample, warm
from tests.unit.test_staged_parse_coordinator import _Backend, _limits, _work


def idle(sequence, now, **changes):
    return sample(sequence, now, http_active=0, **changes)


class IndependentIdleDeficitPolicyTests(unittest.TestCase):
    def test_all_admission_bands_have_honest_lanes_and_unchanged_floors(self):
        cases = (
            (200 * MIB, 8 * GIB, "memory_pause", "gpu"),
            (800 * MIB, 8 * GIB, "gpu_memory_pressure", "gpu"),
            (1200 * MIB, 8 * GIB, "idle_memory_deficit", "gpu"),
            (2 * GIB, 3 * GIB, "memory_pause", "host"),
            (2 * GIB, 5 * GIB, "idle_memory_deficit", "host"),
            (200 * MIB, 3 * GIB, "memory_pause", "gpu_host"),
            (800 * MIB, 5 * GIB, "gpu_memory_pressure", "gpu_host"),
            (1200 * MIB, 5 * GIB, "idle_memory_deficit", "gpu_host"),
        )
        for gpu, host, reason, lanes in cases:
            with self.subTest(gpu=gpu, host=host):
                settings = config()
                before = settings.sha256
                policy = MineruStreamPolicy(settings)
                first = policy.evaluate(idle(1, 0, gpu_free_bytes=gpu,
                                             host_available_bytes=host), now=0)
                next_decision = policy.evaluate(idle(2, 2, gpu_free_bytes=gpu,
                                             host_available_bytes=host), now=2)
                self.assertEqual((first.reason, first.idle_deficit), (reason, lanes))
                self.assertEqual((next_decision.target, next_decision.new_post_allowed), (0, False))
                self.assertEqual(next_decision.idle_deficit_seconds, 2)
                self.assertEqual(settings.sha256, before)
                self.assertEqual((settings.gpu_pause_bytes, settings.gpu_reduce_bytes,
                                  settings.gpu_recover_bytes, settings.host_pause_bytes,
                                  settings.host_recover_bytes),
                                 (512 * MIB, GIB, 1536 * MIB, 4 * GIB, 6 * GIB))

    def test_repeated_reads_and_new_sequence_with_same_observation_create_no_time(self):
        for new_sequence in (False, True):
            with self.subTest(new_sequence=new_sequence):
                policy = MineruStreamPolicy(config())
                initial = idle(1, 0, gpu_free_bytes=0)
                policy.evaluate(initial, now=0)
                for tick in (1, 2, 3):
                    current = idle(tick + 1, 0, gpu_free_bytes=0) if new_sequence else initial
                    decision = policy.evaluate(current, now=tick)
                    self.assertEqual(decision.idle_deficit_seconds, 0,
                                     "a cached observation cannot attest elapsed deficit")
                stale = policy.evaluate(current, now=4)
                self.assertIsNone(stale.idle_deficit)
                self.assertEqual(stale.idle_deficit_seconds, 0)

    def test_evaluation_lag_cannot_stretch_the_interval_between_observations(self):
        policy = MineruStreamPolicy(config())
        policy.evaluate(idle(1, 0, gpu_free_bytes=0), now=2)
        observed_one_second_later = policy.evaluate(idle(2, 1, gpu_free_bytes=0), now=4)
        self.assertEqual(observed_one_second_later.idle_deficit_seconds, 1)

    def test_positive_held_target_in_reduce_band_still_has_bounded_idle_deficit(self):
        policy = MineruStreamPolicy(config())
        end, sequence = warm(policy, 4)
        first = policy.evaluate(idle(sequence + 1, end + 1, gpu_free_bytes=800 * MIB),
                                now=end + 1)
        second = policy.evaluate(idle(sequence + 2, end + 2, gpu_free_bytes=800 * MIB),
                                 now=end + 2)
        self.assertGreater(first.target, 0)
        self.assertGreater(second.target, 0)
        self.assertFalse(second.new_post_allowed)
        self.assertEqual((second.reason, second.idle_deficit, second.idle_deficit_seconds),
                         ("gpu_memory_pressure", "gpu", 1))

    def test_unknown_stale_missing_and_unsafe_suspend_credit_without_reset_or_growth(self):
        for kind in ("missing", "unknown", "host", "provider", "stale", "unsafe"):
            with self.subTest(kind=kind):
                policy = MineruStreamPolicy(config())
                policy.evaluate(idle(1, 0, gpu_free_bytes=0), now=0)
                self.assertEqual(policy.evaluate(idle(2, 2, gpu_free_bytes=0), now=2)
                                 .idle_deficit_seconds, 2)
                changes = {"gpu_free_bytes": 0}
                if kind == "unknown":
                    changes["unknown_reason"] = "joined lane unavailable"
                elif kind == "host":
                    changes["host_available_bytes"] = None
                elif kind == "provider":
                    changes["provider_nonterminal_tasks"] = None
                elif kind == "unsafe":
                    changes["unsafe_reason"] = "owner_drift"
                bad = None if kind == "missing" else idle(3, 2 if kind == "stale" else 6,
                                                          **changes)
                decision = policy.evaluate(bad, now=6)
                self.assertIsNone(decision.idle_deficit)
                self.assertEqual(decision.idle_deficit_seconds, 2)
                resumed = policy.evaluate(idle(4, 8, gpu_free_bytes=0), now=8)
                self.assertEqual(resumed.idle_deficit_seconds, 2)
                if kind != "unsafe":
                    self.assertEqual(resumed.idle_deficit, "gpu")
                    self.assertEqual(policy.evaluate(idle(5, 10, gpu_free_bytes=0), now=10)
                                     .idle_deficit_seconds, 4)
                else:
                    self.assertTrue(resumed.unsafe)
                    self.assertIsNone(resumed.idle_deficit)

    def test_unknown_gap_and_band_alternation_cannot_restart_a_known_episode(self):
        policy = MineruStreamPolicy(config())
        expected = (0, 2, 4, 4, 4, 6)
        values = ((0, 200 * MIB, None), (2, 800 * MIB, None),
                  (4, 1200 * MIB, None), (20, 0, "unavailable"),
                  (22, 200 * MIB, None), (24, 1200 * MIB, None))
        for sequence, ((now, gpu, unknown), credit) in enumerate(zip(values, expected), 1):
            decision = policy.evaluate(idle(sequence, now, gpu_free_bytes=gpu,
                                           unknown_reason=unknown), now=now)
            self.assertEqual(decision.idle_deficit_seconds, credit)
            self.assertFalse(decision.new_post_allowed)

    def test_brief_floor_pulse_suspends_credit_and_full_hysteresis_recovery_resets_it(self):
        # Root's explicit adjudication: physical headroom for one observation
        # does not restore POST admission. Resetting then would let brief
        # pulses postpone the bounded stop forever; full recovery ends it.
        policy = MineruStreamPolicy(config())
        policy.evaluate(idle(1, 0, gpu_free_bytes=0), now=0)
        self.assertEqual(policy.evaluate(idle(2, 2, gpu_free_bytes=0), now=2)
                         .idle_deficit_seconds, 2)
        pulse = policy.evaluate(idle(3, 3), now=3)
        self.assertEqual((pulse.target, pulse.new_post_allowed), (0, False))
        self.assertIsNone(pulse.idle_deficit)
        self.assertEqual(pulse.idle_deficit_seconds, 2)
        relapsed = policy.evaluate(idle(4, 4, gpu_free_bytes=0), now=4)
        self.assertEqual(relapsed.idle_deficit_seconds, 2)
        self.assertEqual(policy.evaluate(idle(5, 5, gpu_free_bytes=0), now=5)
                         .idle_deficit_seconds, 3)
        for sequence, now in enumerate((6, 8, 10, 12, 14, 15.999), 6):
            recovering = policy.evaluate(idle(sequence, now), now=now)
            self.assertFalse(recovering.new_post_allowed)
            self.assertIsNone(recovering.idle_deficit)
            self.assertEqual(recovering.idle_deficit_seconds, 3)
        recovered = policy.evaluate(idle(12, 16), now=16)
        self.assertEqual((recovered.target, recovered.new_post_allowed), (1, True))
        self.assertIsNone(recovered.idle_deficit)
        self.assertEqual(recovered.idle_deficit_seconds, 0)
        self.assertEqual(policy.config.recovery_seconds, 10)
        self.assertEqual(policy.evaluate(idle(13, 17, gpu_free_bytes=0), now=17)
                         .idle_deficit_seconds, 0)

    def test_accepted_work_in_each_provider_fact_resets_credit(self):
        for lane in ("provider_nonterminal_tasks", "http_active", "http_pending"):
            with self.subTest(lane=lane):
                policy = MineruStreamPolicy(config())
                policy.evaluate(idle(1, 0, gpu_free_bytes=0), now=0)
                policy.evaluate(idle(2, 2, gpu_free_bytes=0), now=2)
                changes = {"gpu_free_bytes": 0, lane: 1}
                active = sample(3, 3, **{"http_active": 0, **changes})
                decision = policy.evaluate(active, now=3)
                self.assertIsNone(decision.idle_deficit)
                self.assertEqual(decision.idle_deficit_seconds, 0)
                self.assertEqual(policy.evaluate(idle(4, 4, gpu_free_bytes=0), now=4)
                                 .idle_deficit_seconds, 0)


class IndependentIdleDeficitCoordinatorTests(unittest.TestCase):
    def test_finite_shortage_persists_stop_after_accepted_tails_and_preserves_intents(self):
        for gpu, host, lane in ((0, 8 * GIB, "gpu"), (2 * GIB, 0, "host"),
                                (0, 0, "gpu_host")):
            with self.subTest(lane=lane):
                root = trusted_root(self)
                settings = settings_for(root)
                store = WorkerControlStore.for_settings(settings)
                control = RuntimeWorkerStopControl(store=store, supervisor=None, emit=lambda _: None)
                parked = _work("a-unsubmitted", "prepared")
                tail = _work("b-retained-result", "remote_terminal", 3)
                backend = _Backend(recoverable=(parked, tail),
                                   new=(_work("new-unadmitted", "prepared"),))
                class Clock:
                    now = 0.0
                    def tick(self):
                        self.now += 0.25
                        return self.now
                clock = Clock()
                class FreshPressure:
                    sequence = 0
                    def latest(self):
                        self.sequence += 1
                        return idle(self.sequence, clock.now, gpu_free_bytes=gpu,
                                    host_available_bytes=host)
                snapshots = []
                coordinator = StagedParseCoordinator(backend=backend,
                    limits=_limits(stream_idle_deficit_stop_seconds=0.5),
                    stream_control=StreamAdmissionControl(MineruStreamPolicy(config()),
                        FreshPressure(), monotonic=clock.tick),
                    stop_control=control, progress=snapshots.append)
                result = coordinator.run(wait_for_stream_admission=True,
                                         stop_requested=lambda: clock.now > 20)
                self.assertLess(clock.now, 20, "fallback required an external test stop")
                self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
                self.assertEqual(result.termination_kind, "public_stop")
                self.assertEqual((result.admitted, result.completed), (0, 1))
                self.assertEqual(dict(result.final_states), {tail.attempt_id: "acked"})
                self.assertEqual(result.credits_in_use, parked.credits)
                self.assertEqual(backend.calls.count("ack:b-retained-result:ack_pending"), 1)
                self.assertNotIn("preflight:a-unsubmitted", backend.calls)
                self.assertFalse(any(call.startswith("admit:") for call in backend.calls))
                self.assertEqual(len(backend.new), 1)
                reason = "stream_idle_" + lane + "_memory_deficit"
                self.assertEqual(result.stop_cause.reason_code, reason)
                persisted = WorkerControlStore.for_settings(settings).read_active()
                self.assertIsNotNone(persisted.record)
                self.assertEqual(persisted.record.cause, result.stop_cause)
                # The fake runtime has no native supervisor; the marker is real
                # disposable IO, and no claim of a real launchd stop is made.
                self.assertTrue(control.is_tripped())
                self.assertTrue(all(not s.circuit_open for s in snapshots if s.completed < 1))


if __name__ == "__main__":
    unittest.main()
