"""Independent endpoint and counter oracles for the frozen M6 acceptance gates."""

from datetime import timedelta
from dataclasses import replace
import unittest

from disclosure_anchor.application.contracts.synchronized_telemetry import SynchronizedTelemetryFrameV2
from tests.unit.test_synchronized_telemetry_contract import START, _frame
from tests import m6_support as m6
from tests.m6_delivery_support import evaluation_plan


GIB = 1024 ** 3


def resource_frames():
    frames = []
    for quarter in range(9):
        lanes = ("gpu_fast", "host_slow") if quarter % 4 == 0 else ("gpu_fast",)
        for lane in lanes:
            frame = _frame(sequence=len(frames), lane=lane, started_ns=quarter * 250_000_000, first=quarter == 0)
            value = frame.model_dump()
            value["contract_version"] = "mineru.synchronized-telemetry-frame.v2"
            if lane == "gpu_fast":
                value["gpu"]["values"].update(framebuffer_total_bytes=16 * GIB,
                                               framebuffer_used_bytes=10 * GIB, framebuffer_free_bytes=6 * GIB)
            else:
                value["host_cgroup"]["values"]["memory_events"].update(
                    oom_total=4, oom_kill_total=2, oom_group_kill_total=1,
                )
                value["queue_vllm"]["values"]["vllm_preemptions_total"] = 8
            frames.append(SynchronizedTelemetryFrameV2.model_validate(value))
    return tuple(frames)


def change_frame(frame, section, changes):
    value = frame.model_dump()
    target = value
    for part in section:
        target = target[part]
    target.update(changes)
    return SynchronizedTelemetryFrameV2.model_validate(value)


class ResourceAggregateIndependentTests(unittest.TestCase):
    def derive(self, frames, *, start=START, end=START + timedelta(seconds=2)):
        from disclosure_anchor.application.services.telemetry_resource_aggregates import derive_resource_aggregates

        return derive_resource_aggregates(frames, window_start_utc=start, window_end_utc=end)

    def test_complete_values_use_minimum_and_counter_deltas_not_absolute_counts(self):
        frames = list(resource_frames())
        index = next(i for i, frame in enumerate(frames)
                     if frame.lane == "gpu_fast" and frame.clock.observed_at_utc == START + timedelta(seconds=1))
        frames[index] = change_frame(frames[index], ("gpu", "values"),
                                     {"framebuffer_free_bytes": 2 * GIB, "framebuffer_used_bytes": 14 * GIB})
        actual = self.derive(tuple(frames))
        self.assertEqual(actual.problems, ())
        self.assertEqual(actual.gpu_free_min_bytes, 2 * GIB)
        self.assertEqual((actual.gpu_supported_samples, actual.host_supported_samples), (9, 3))
        self.assertEqual((actual.oom_total_delta, actual.oom_kill_total_delta,
                          actual.oom_group_kill_total_delta, actual.preemption_delta), (0, 0, 0, 0))

    def test_each_oom_component_and_preemption_are_observed_separately(self):
        cases = (
            (("host_cgroup", "values", "memory_events"), "oom_total", "oom_total_delta"),
            (("host_cgroup", "values", "memory_events"), "oom_kill_total", "oom_kill_total_delta"),
            (("host_cgroup", "values", "memory_events"), "oom_group_kill_total", "oom_group_kill_total_delta"),
            (("queue_vllm", "values"), "vllm_preemptions_total", "preemption_delta"),
        )
        for section, field, output in cases:
            with self.subTest(counter=field):
                frames = list(resource_frames())
                index = max(i for i, frame in enumerate(frames) if frame.lane == "host_slow")
                value = frames[index].model_dump()
                for part in section:
                    value = value[part]
                frames[index] = change_frame(frames[index], section, {field: value[field] + 1})
                actual = self.derive(tuple(frames))
                self.assertEqual(getattr(actual, output), 1)
                self.assertEqual(actual.problems, ())

    def test_reset_then_rebound_cannot_be_hidden_by_equal_endpoints(self):
        for field, section, output in (
            ("oom_total", ("host_cgroup", "values", "memory_events"), "oom_total_delta"),
            ("oom_kill_total", ("host_cgroup", "values", "memory_events"), "oom_kill_total_delta"),
            ("oom_group_kill_total", ("host_cgroup", "values", "memory_events"), "oom_group_kill_total_delta"),
            ("vllm_preemptions_total", ("queue_vllm", "values"), "preemption_delta"),
        ):
            with self.subTest(counter=field):
                frames = list(resource_frames())
                index = next(i for i, frame in enumerate(frames)
                             if frame.lane == "host_slow" and frame.clock.observed_at_utc == START + timedelta(seconds=1))
                frames[index] = change_frame(frames[index], section, {field: 0})
                actual = self.derive(tuple(frames))
                self.assertIsNone(getattr(actual, output))
                self.assertIn("counter_reset:" + field, actual.problems)

    def test_unsupported_required_samples_remain_unknown(self):
        for lane, section, output in (
            ("gpu_fast", "gpu", "gpu_free_min_bytes"),
            ("host_slow", "host_cgroup", "oom_total_delta"),
            ("host_slow", "queue_vllm", "preemption_delta"),
        ):
            with self.subTest(lane=lane, section=section):
                frames = list(resource_frames())
                index = next(i for i, frame in enumerate(frames)
                             if frame.lane == lane and frame.clock.observed_at_utc == START + timedelta(seconds=1))
                frames[index] = change_frame(frames[index], (section,),
                                             {"status": "unsupported", "reason": "collector_unsupported", "values": None})
                actual = self.derive(tuple(frames))
                self.assertIsNone(getattr(actual, output))
                self.assertTrue(actual.problems)

    def test_empty_or_one_sample_does_not_prove_resource_safety(self):
        frames = resource_frames()
        for selected in ((), frames[:2]):
            with self.subTest(samples=len(selected)):
                actual = self.derive(selected)
                self.assertIsNone(actual.gpu_free_min_bytes)
                self.assertIsNone(actual.oom_total_delta)
                self.assertIsNone(actual.preemption_delta)
                self.assertTrue(actual.problems)

    def test_unsafe_sample_one_byte_below_gate_is_not_rounded_away(self):
        frames = list(resource_frames())
        frames[0] = change_frame(frames[0], ("gpu", "values"), {
            "framebuffer_free_bytes": 1610612735, "framebuffer_used_bytes": 16 * GIB - 1610612735,
        })
        self.assertEqual(self.derive(tuple(frames)).gpu_free_min_bytes, 1610612735)

    def test_counter_increase_at_window_edge_is_not_a_zero_error_pass(self):
        for edge in ("start", "end"):
            with self.subTest(edge=edge):
                frames = []
                for frame in resource_frames():
                    observed = frame.clock.observed_at_utc
                    threshold = START + timedelta(seconds=1 if edge == "start" else 2)
                    if frame.lane == "host_slow" and observed >= threshold:
                        frame = change_frame(frame, ("host_cgroup", "values", "memory_events"), {"oom_total": 5})
                    frames.append(frame)
                # Start edge: the increment is between the preceding sample at
                # 0 s and the first inner sample at 1 s. End edge: it is between
                # the last inner sample at 1 s and the following sample at 2 s.
                start = START + timedelta(seconds=0.25 if edge == "start" else 0)
                end = START + timedelta(seconds=2 if edge == "start" else 1.75)
                actual = self.derive(tuple(frames), start=start, end=end)
                self.assertEqual(actual.problems, ())
                self.assertEqual(actual.oom_total_delta, 1,
                                 "complete outer bracket must retain the edge increment")

    def test_missing_outer_boundaries_and_scheduled_gap_remain_unknown(self):
        original = resource_frames()
        for second in (0, 1, 2):
            with self.subTest(missing_host_second=second):
                frames = tuple(f for f in original if not (
                    f.lane == "host_slow" and f.clock.observed_at_utc == START + timedelta(seconds=second)))
                result = self.derive(frames, start=START + timedelta(seconds=.25),
                                     end=START + timedelta(seconds=1.75))
                self.assertIsNone(result.oom_total_delta)
                self.assertIsNone(result.preemption_delta)
                self.assertTrue(result.problems)
                self.assertEqual(result.gpu_free_min_bytes, 6 * GIB)

    def test_counter_outer_sample_must_be_within_one_nominal_period(self):
        frames = list(resource_frames())
        index = next(i for i, f in enumerate(frames) if f.lane == "host_slow")
        frames[index] = change_frame(frames[index], ("clock",),
                                     {"observed_at_utc": START - timedelta(seconds=2)})
        result = self.derive(tuple(frames), start=START + timedelta(seconds=.25))
        self.assertIsNone(result.oom_total_delta)
        self.assertIn("host_counter_boundary_gap", result.problems)


class StageTimingIndependentTests(unittest.TestCase):
    def setUp(self):
        from disclosure_anchor.application.services.m6_delivery_report import (
            ClockBinding, StageNoteFact, StageTimingFacts, VerifierAttemptFact,
        )

        members = {**{f"s{i:02d}": (20, "fresh") for i in range(20)},
                   "long": (150, "fresh"), "medium": (60, "fresh")}
        self.fixture = m6.make_fixture("e2e_publication", members, planned_seconds=4800)
        self.journal = m6.Journal(self.fixture)
        self.journal.start()
        self.journal.opened(self.journal.at(1))
        notes, verified = [], []
        for index, (name, source) in enumerate(self.fixture.entries.items()):
            attempt = "att-" + name
            admitted = self.journal.admit(source, attempt, self.journal.at(2 + index * 10))
            accepted = self.journal.accept(attempt, self.journal.at(3 + index * 10))
            self.journal.commit(admitted, self.journal.at(4 + index * 10), ledger_seq=index + 1)
            public = self.journal.confirm(admitted, self.journal.at(5 + index * 10), ledger_seq=index + 1)
            start = 1_000_000_000_000 + index * 100_000_000_000
            seconds = 700 if name == "long" else 40 if name == "medium" else index + 1
            terminal = start + seconds * 1_000_000_000
            notes.extend((
                StageNoteFact(line=len(notes) + 1, attempt_id=attempt, lane="remote", kind="remote_post_send",
                              monotonic_ns=start, scalars={"fence_identity": admitted.fence_identity,
                              "source_pdf_sha256": admitted.source_pdf_sha256,
                              "submission_intent_sha256": m6.digest("submission:" + attempt)}),
                StageNoteFact(line=len(notes) + 2, attempt_id=attempt, lane="remote", kind="remote_terminal_observed",
                              monotonic_ns=terminal, scalars={"fence_identity": admitted.fence_identity,
                              "remote_task_identity_sha256": accepted.remote_task_identity_sha256,
                              "accepted_submission_receipt_sha256": accepted.acceptance_receipt_sha256,
                              "terminal_receipt_sha256": m6.digest("terminal:" + attempt),
                              "lease_observed_at_unix": "1.0"}),
            ))
            verified.append(VerifierAttemptFact(attempt_id=attempt, confirmed=True,
                            public_ns=terminal + 2_000_000_000,
                            public_confirmation_sha256=public.canonical_sha256(),
                            started_ns=start, finished_ns=terminal + 50_000_000_000))
        clock = ClockBinding(source="python.time.monotonic_ns", implementation="mach_absolute_time()",
                             boot_session_uuid="11111111-1111-4111-8111-111111111111")
        self.facts = StageTimingFacts(
            observation_status="complete", observation_counts={
                "dropped": 0, "truncated": 0, "note_errors": 0, "guard_failures": 0,
                "late_notes": 0, "writer_errors": 0, "join_timeout": 0,
                "events_written": len(notes), "bytes_written": 10000,
            }, runner_clock=clock, verifier_clock=clock, notes=tuple(notes),
            verifier_attempts=tuple(verified), problems=(),
        )

    def measure(self, facts=None):
        from disclosure_anchor.application.services.m6_delivery_report import latency_by_size

        return latency_by_size(evaluation_plan(required_safety=True), self.fixture.spec,
                               tuple(self.journal.records), stage_timing=self.facts if facts is None else facts)

    def test_hand_computed_endpoint_vectors_and_nearest_rank(self):
        actual = self.measure()
        self.assertEqual(actual.gates.status, "pass", actual.stage_timing.reasons)
        self.assertEqual(actual.classes.short.remote_samples, 20)
        self.assertEqual(actual.classes.short.remote_post_to_terminal.p95_s, 19.0)
        self.assertEqual(actual.classes.short.remote_post_to_terminal.max_s, 20.0)
        self.assertEqual(actual.classes.long.remote_post_to_terminal.max_s, 700.0)
        self.assertEqual(actual.classes.medium.remote_post_to_terminal.max_s, 40.0)
        self.assertEqual(actual.classes.short.terminal_to_public_confirmation.max_s, 2.0)
        self.assertEqual(actual.classes.short.admission_to_public_confirmation.max_s, 3.0)

    def test_retry_keeps_first_actual_post_as_start(self):
        # Add a retry 10 s into the 20 s short sample: replacing the first
        # start would incorrectly change the short maximum to 19 s.
        original = next(note for note in self.facts.notes if note.attempt_id == "att-s19"
                        and note.kind == "remote_post_send")
        resent = replace(original, line=len(self.facts.notes) + 1,
                         monotonic_ns=original.monotonic_ns + 10_000_000_000)
        actual = self.measure(replace(self.facts, notes=(*self.facts.notes, resent)))
        self.assertEqual(actual.gates.status, "pass", actual.stage_timing.reasons)
        self.assertEqual(actual.classes.short.remote_post_to_terminal.max_s, 20.0)
        self.assertEqual(actual.classes.short.remote_resends, 1)

    def test_missing_or_failed_attempt_cannot_pass_by_reducing_the_sample(self):
        for kind in ("remote_post_send", "remote_terminal_observed"):
            with self.subTest(missing=kind):
                notes = tuple(note for note in self.facts.notes
                              if not (note.attempt_id == "att-s19" and note.kind == kind))
                actual = self.measure(replace(self.facts, notes=notes))
                self.assertNotEqual(actual.gates.status, "pass")
                self.assertTrue(any("att-s19" in reason for reason in actual.stage_timing.reasons))
        terminal = next(note for note in self.facts.notes if note.attempt_id == "att-s19"
                        and note.kind == "remote_terminal_observed")
        failed = replace(terminal, kind="remote_terminal_failed",
                         scalars={**terminal.scalars, "error_code": "provider_terminal_failure"})
        notes = tuple(failed if note is terminal else note for note in self.facts.notes)
        self.assertNotEqual(self.measure(replace(self.facts, notes=notes)).gates.status, "pass")

    def test_identity_conflicts_and_reversed_times_never_become_samples(self):
        post, terminal = self.facts.notes[:2]
        corruptions = (
            (post, replace(post, scalars={**post.scalars, "source_pdf_sha256": m6.digest("foreign-source")})),
            (post, replace(post, scalars={**post.scalars, "fence_identity": "foreign-fence"})),
            (post, replace(post, lane="commit")),
            (terminal, replace(terminal, scalars={**terminal.scalars, "remote_task_identity_sha256": m6.digest("foreign-task")})),
            (terminal, replace(terminal, scalars={**terminal.scalars, "accepted_submission_receipt_sha256": m6.digest("foreign-acceptance")})),
            (terminal, replace(terminal, scalars={key: value for key, value in terminal.scalars.items()
                                                 if key != "terminal_receipt_sha256"})),
            (terminal, replace(terminal, monotonic_ns=post.monotonic_ns - 1)),
        )
        for index, (before, after) in enumerate(corruptions):
            with self.subTest(case=index):
                facts = replace(self.facts, notes=tuple(after if note is before else note for note in self.facts.notes))
                self.assertNotEqual(self.measure(facts).gates.status, "pass")
        conflict = replace(terminal, line=len(self.facts.notes) + 1,
                           scalars={**terminal.scalars, "terminal_receipt_sha256": m6.digest("second-terminal")})
        self.assertNotEqual(self.measure(replace(self.facts, notes=(*self.facts.notes, conflict))).gates.status, "pass")

    def test_public_exact_payload_clock_and_time_are_required(self):
        first = self.facts.verifier_attempts[0]
        for change in (
            {"public_ns": None}, {"public_confirmation_sha256": m6.digest("foreign-public")},
            {"confirmed": False}, {"public_ns": self.facts.notes[0].monotonic_ns - 1},
        ):
            with self.subTest(change=change):
                facts = replace(self.facts, verifier_attempts=(replace(first, **change), *self.facts.verifier_attempts[1:]))
                self.assertNotEqual(self.measure(facts).gates.status, "pass")
        facts = replace(self.facts, verifier_clock=replace(self.facts.verifier_clock,
                        boot_session_uuid="22222222-2222-4222-8222-222222222222"))
        measured = self.measure(facts)
        self.assertNotEqual(measured.gates.status, "pass")
        self.assertIsNone(measured.classes.short.terminal_to_public_confirmation,
                          "different boot clocks cannot be subtracted even for a diagnostic measure")

    def test_duplicate_verifier_attempt_cannot_select_the_faster_observation(self):
        duplicate = replace(self.facts.verifier_attempts[0], public_ns=self.facts.verifier_attempts[0].public_ns - 1)
        actual = self.measure(replace(self.facts, verifier_attempts=(*self.facts.verifier_attempts, duplicate)))
        self.assertNotEqual(actual.gates.status, "pass")
        self.assertEqual(actual.stage_timing.status, "unknown")

    def test_malformed_receipts_are_not_valid_endpoint_identity(self):
        post, terminal = self.facts.notes[:2]
        for before, key in ((post, "submission_intent_sha256"), (terminal, "terminal_receipt_sha256")):
            for value in (None, "not-a-sha", True):
                with self.subTest(field=key, value=value):
                    changed = replace(before, scalars={**before.scalars, key: value})
                    actual = self.measure(replace(self.facts, notes=tuple(
                        changed if note is before else note for note in self.facts.notes)))
                    self.assertNotEqual(actual.gates.status, "pass")

    def test_loss_or_partial_capture_cannot_pass_but_normal_written_counts_can(self):
        self.assertEqual(self.measure().gates.status, "pass")
        for name in ("dropped", "truncated", "note_errors", "guard_failures", "late_notes", "writer_errors", "join_timeout"):
            with self.subTest(loss=name):
                facts = replace(self.facts, observation_counts={**self.facts.observation_counts, name: 1})
                self.assertNotEqual(self.measure(facts).gates.status, "pass")
        for status in ("partial", "invalid", None):
            with self.subTest(status=status):
                self.assertNotEqual(self.measure(replace(self.facts, observation_status=status)).gates.status, "pass")
        facts = replace(self.facts, problems=("stage_events_malformed_line:4",))
        self.assertNotEqual(self.measure(facts).gates.status, "pass")


if __name__ == "__main__":
    unittest.main()
