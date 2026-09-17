"""Independent R22 acceptance: D1-D8 at the boundaries the plan actually changes.

These are the independent cases for the R22 convergence: a sampling plan frozen
from the owner's own retained duration, a native endpoint that samples once per
valid request instead of running a second scheduler, resource edges proved by
capture intervals rather than request timestamps, and one finite budget vector.

Two rules shape every case here. Behaviour is exercised at a physical boundary -
the pure rule, the decoder, the control socket, the real report - never by
matching a source string. And an interface R22 has not landed yet is a **named
failure**, not a skip: `_interface` fails with the exact module and symbol the
case is waiting for, so a red run says what is missing rather than hiding it.

Raw fixtures are synthetic and identity-consistent; no live host identity, run
id or captured payload is copied into the repository. The native
backend-call-count proof is not here - it can only be made against the compiled
endpoint, and lives in `scripts/windows/test_mineru_resident_endpoint.ps1`.
"""

from __future__ import annotations

from datetime import timedelta
from fractions import Fraction
import importlib
import json
from pathlib import Path
import socket
import threading
import unittest

from disclosure_anchor.application.contracts.resident_session_evidence import (
    artifact_sha256, canonical_bytes,
)
from tests._mineru_capacity_config_fixture import CAPACITY_BYTES
from tests.unit.test_m6_measured_driver_independent import frame as _v2_frame
from tests.unit.test_synchronized_telemetry_contract import HASH, HASH_B, HASH_C, RUN_ID, START


NS = 1_000_000_000
POLICY = "disclosure_anchor.application.services.resident_measurement_policy"
WIRE = "disclosure_anchor.application.contracts.windows_resident_telemetry"
TELEMETRY = "disclosure_anchor.application.contracts.synchronized_telemetry"
AGGREGATES = "disclosure_anchor.application.services.telemetry_resource_aggregates"
# Synthetic, internally consistent identities. Not a host, run or capacity of any machine.
RUN = "3f2b6c31-0000-4000-8000-00000000d1d1"
INTENT_SHA = "sha256:" + "11" * 32
CLOCK_SHA = "sha256:" + "22" * 32
EPOCH_SHA = "sha256:" + "33" * 32
NONCE = "0" * 31 + "1"
# A scripted owner session has no real IO, but the drain-timeout case legitimately waits out
# the planned end plus the product's own grace. Beyond this the session is hung, not slow.
SCRIPTED_SESSION_TIMEOUT_SECONDS = 25.0


def _interface(module_name: str, attribute: str):
    """Resolve one R22 product interface; absence is a named failure, never a skip."""
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised while R22 lands
        raise AssertionError(
            f"R22 interface not implemented yet: module {module_name} ({exc})") from exc
    if not hasattr(module, attribute):
        raise AssertionError(
            f"R22 interface not implemented yet: {module_name}.{attribute}")
    return getattr(module, attribute)


def _wire_plan(*, duration_ns: int = 120 * NS, start_ns: int = 5 * NS, gpu_interval_ms: int = 250,
               **changes):
    """The product's own closed plan document, as the observer writes it."""
    model = _interface(TELEMETRY, "SynchronizedSamplingPlanV1")
    fields = {"run_id": RUN_ID, "owner_intent_sha256": INTENT_SHA,
              "observer_clock_domain_identity_sha256": _shared_receipt().clock_domain_identity_sha256,
              "started_monotonic_ns": start_ns, "duration_ns": duration_ns,
              "planned_end_monotonic_ns": start_ns + duration_ns,
              "gpu_nominal_interval_ms": gpu_interval_ms, "host_nominal_interval_ms": 1000}
    fields.update(changes)
    return model.model_validate(fields)


def _pull_frame(plan, lane, index, *, stream_sequence=0, native_ppm=0, native_origin_ns=10 ** 6,
                utc_shift_seconds=0.0, finish_extra_ns=1_000_000, nonce=NONCE, **changes):
    """One real frame v3 at a frozen slot, with a complete fresh-per-request witness.

    `native_ppm` runs the source oscillator fast or slow and `utc_shift_seconds` steps the
    source's wall clock; neither may move the Mac interval, which is what D3 decides.
    """
    frame_model = _interface(TELEMETRY, "SynchronizedTelemetryFrameV3")
    period = plan.gpu_nominal_interval_ms if lane == "gpu_fast" else plan.host_nominal_interval_ms
    scheduled = plan.started_monotonic_ns + index * period * 1_000_000
    # The lane's own pull cursor is its slot index; the stream sequence is this frame's
    # position in the merged JSONL, which the product checks separately.
    cursor = index
    # The Mac brackets the request; the native capture sits inside it, in its own domain.
    request_ns, response_ns = scheduled, scheduled + finish_extra_ns
    scale = Fraction(1_000_000 + native_ppm, 1_000_000)
    # The source's own QPC advances with every slot of this lane, so a later frame always
    # carries a later native instant; only the rate differs from the Mac clock.
    lane_origin = native_origin_ns + int(index * period * 1_000_000 * scale)
    native = lambda offset: lane_origin + int(offset * scale)  # noqa: E731 - local shorthand
    value = _v2_frame(lane=lane, sequence=stream_sequence, started_ns=scheduled,
                      first=index == 0).model_dump(mode="json")
    # One identity across plan, frames and receipt, taken from the product's shared session
    # fixture; the replay binds all three and a drift in any of them is a separate case.
    source = _shared_receipt()
    value.update(contract_version="mineru.synchronized-telemetry-frame.v3", run_id=plan.run_id,
                 runtime_bundle_identity_sha256=source.runtime_bundle_identity_sha256,
                 process_profile_sha256=source.process_profile.process_profile_sha256,
                 observer_source_sha256=_observer_source_sha256())
    # Each lane owns only its own observations; the other lane's are explicitly unsupported.
    unsupported = {"status": "unsupported", "reason": "not_due_at_this_tick", "values": None}
    for name in (("api_process", "host_cgroup", "queue_vllm") if lane == "gpu_fast" else ("gpu",)):
        value[name] = dict(unsupported)
    if lane == "host_slow":
        # The host lane's own observations answer to the same observed profile the receipt
        # carries; the replay binds the API epoch and the admission limit to it.
        if value["api_process"]["values"] is not None:
            value["api_process"]["values"]["process_epoch_sha256"] = (
                source.process_profile.process_epoch_sha256)
        if value["queue_vllm"]["values"] is not None:
            value["queue_vllm"]["values"]["api_max_pending_tasks"] = (
                source.process_profile.parameters.api_max_pending_tasks)
            value["queue_vllm"]["values"]["api_nonterminal_tasks"] = min(
                value["queue_vllm"]["values"]["api_nonterminal_tasks"],
                source.process_profile.parameters.api_max_pending_tasks)
            value["queue_vllm"]["values"]["api_queued_tasks"] = 0
            value["queue_vllm"]["values"]["api_processing_tasks"] = (
                value["queue_vllm"]["values"]["api_nonterminal_tasks"])
    value["clock"].update(
        clock_domain_identity_sha256=plan.observer_clock_domain_identity_sha256,  # one domain
        scheduled_monotonic_ns=scheduled, started_monotonic_ns=request_ns,
        finished_monotonic_ns=response_ns,
        observed_at_utc=(START + timedelta(seconds=utc_shift_seconds)
                         + timedelta(microseconds=scheduled / 1000)).isoformat())
    value["quality"].update(nominal_interval_ms=period,
                            collection_duration_ms=(response_ns - request_ns) / 1_000_000)
    value["resident_exporter_provenance"] = {
        "exporter_source_sha256": HASH, "host_assignment_identity_sha256": HASH_B,
        "boot_identity_sha256": HASH_C, "exporter_process_epoch_sha256": HASH,
        "wire_sequence": cursor + 1,
        "wire_observed_at_utc": value["clock"]["observed_at_utc"],
        "wire_sampled_monotonic_ns": native(20), "request_nonce": nonce,
        "after_sequence": cursor, "local_request_monotonic_ns": request_ns,
        "local_response_monotonic_ns": response_ns,
        "native_request_received_monotonic_ns": native(0),
        "native_capture_finished_monotonic_ns": native(40),
        "native_reply_started_monotonic_ns": native(60),
    }
    for path, replacement in changes.pop("overrides", ()):
        target = value
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = replacement
    value.update(changes)
    return frame_model.model_validate(value)


def _late_frame(frame, over_deadline_ns):
    """The same frame whose collection actually finished past its own deadline."""
    model = _interface(TELEMETRY, "SynchronizedTelemetryFrameV3")
    period_ns = frame.quality.nominal_interval_ms * 1_000_000
    deadline = frame.clock.scheduled_monotonic_ns + period_ns
    value = frame.model_dump(mode="json")
    finished = deadline + over_deadline_ns
    value["clock"]["finished_monotonic_ns"] = finished
    value["quality"]["collection_duration_ms"] = (
        finished - frame.clock.started_monotonic_ns) / 1_000_000
    value["resident_exporter_provenance"]["local_response_monotonic_ns"] = finished
    return model.model_validate(value)


def _pull_frames(plan, *, drop=(), **changes):
    """Every frame the frozen plan expects, in emitted order, minus `drop` (lane, index) pairs.

    Both lanes share one JSONL, so the stream sequence is the merged position while each
    lane keeps its own pull cursor - the distinction the edge counters depend on.
    """
    dropped, slots = set(drop), []
    for lane in ("gpu_fast", "host_slow"):
        period_ns = (plan.gpu_nominal_interval_ms if lane == "gpu_fast"
                     else plan.host_nominal_interval_ms) * 1_000_000
        expected = (plan.duration_ns + period_ns - 1) // period_ns
        slots.extend((plan.started_monotonic_ns + index * period_ns, lane, index)
                     for index in range(expected) if (lane, index) not in dropped)
    slots.sort(key=lambda slot: (slot[0], slot[1]))
    return tuple(_pull_frame(plan, lane, index, stream_sequence=position, **changes)
                 for position, (_, lane, index) in enumerate(slots))


def _observer_source_sha256():
    """The running observer build's own identity, as the replay recomputes it."""
    return _interface("disclosure_anchor.adapters.runtime.synchronized_telemetry_observer",
                      "synchronized_observer_source_sha256")()


def _shared_receipt():
    """The product's own shared session receipt, used only for its identity blocks."""
    import tempfile
    from tests.unit.test_resident_telemetry_owner import _fixture

    if not hasattr(_shared_receipt, "value"):
        with tempfile.TemporaryDirectory() as root:
            _shared_receipt.value = _fixture(Path(root).resolve())[2].receipt
    return _shared_receipt.value


def _receipt_v4(plan, frames, *, overshoot_ns=0, wall_extra_ns=0, termination="duration_elapsed"):
    """A real receipt v4 over these frames, with the actual finish and wall clocks as given.

    Identity blocks come from the product's own shared session fixture, so nothing here
    invents a profile or an observer. `overshoot_ns` is the finish past the planned end -
    the value the old reader let define the window.
    """
    model = _interface(TELEMETRY, "SynchronizedTelemetryReceiptV4")
    derive = _interface(TELEMETRY, "derive_frame_evidence_v4")
    quality, coverage, unsupported = derive(frames, plan=plan)
    source = _shared_receipt()
    finished_ns = plan.planned_end_monotonic_ns + overshoot_ns
    elapsed = finished_ns - plan.started_monotonic_ns
    started_utc = source.started_at_utc
    # UTC carries microseconds, so the recorded divergence is the exact difference between
    # the wall interval this receipt can express and the monotonic one it measured.
    wall_ns = ((elapsed + wall_extra_ns) // 1000) * 1000
    return model.model_validate({
        "run_id": plan.run_id,
        "runtime_bundle_identity_sha256": source.runtime_bundle_identity_sha256,
        "process_profile": source.process_profile.model_dump(mode="json"),
        "observer_identity": source.observer_identity.model_dump(mode="json"),
        "observer_source_sha256": _observer_source_sha256(),
        "clock_domain_identity_sha256": source.clock_domain_identity_sha256,
        "sampling_plan_sha256": INTENT_SHA,
        "duration_ns": plan.duration_ns,
        "planned_end_monotonic_ns": plan.planned_end_monotonic_ns,
        "started_at_utc": started_utc.isoformat(),
        "finished_at_utc": (started_utc + timedelta(microseconds=wall_ns // 1000)).isoformat(),
        "started_monotonic_ns": plan.started_monotonic_ns,
        "finished_monotonic_ns": finished_ns,
        # The receipt states what the evidence says: any missing slot, late response or
        # unsupported observation makes the run incomplete.
        "status": ("complete" if (all(item.missing_slots == 0 for item in coverage)
                                  and all(item.late_sample_count == 0 and item.supported_frame_count
                                          for item in quality) and unsupported == 0)
                   else "incomplete"),
        "lane_quality": [item.model_dump(mode="json") for item in quality],
        "slot_coverage": [item.model_dump(mode="json") for item in coverage],
        "termination_reason": termination,
        "observed_clock_divergence_ns": abs(wall_ns - elapsed),
        "epoch_changed": False, "safety_drift_reasons": [],
        "unsupported_observation_count": unsupported,
        "artifacts": source.artifacts.model_dump(mode="json"),
    })


def _plan(*, duration_ns: int = 120 * NS, start_ns: int = 5 * NS, gpu_period_ns: int = 250_000_000):
    plan_type = _interface(POLICY, "SamplingPlan")
    return plan_type(run_id=RUN, owner_intent_sha256=INTENT_SHA,
                     observer_clock_domain_sha256=CLOCK_SHA, start_ns=start_ns,
                     duration_ns=duration_ns, gpu_period_ns=gpu_period_ns)


def _slots(plan, lane, *, drop=()):
    """Every scheduled instant the frozen plan expects for one lane, minus `drop` indices."""
    period = plan.period_ns(lane)
    expected = (plan.duration_ns + period - 1) // period
    return [plan.start_ns + index * period for index in range(expected) if index not in set(drop)]


class R22PlanAndSlotTests(unittest.TestCase):
    """D1 - the plan is the owner's frozen duration, never the last frame."""

    def test_a_frozen_plan_counts_half_open_slots_for_both_lanes(self):
        coverage = _interface(POLICY, "slot_coverage")
        plan = _plan()
        for lane, expected in (("gpu_fast", 480), ("host_slow", 120)):
            with self.subTest(lane=lane):
                full = coverage(plan, lane, _slots(plan, lane))
                self.assertEqual((full.expected, full.observed, full.missing), (expected, expected, 0))
                self.assertTrue(full.complete)
        self.assertEqual(plan.end_ns, plan.start_ns + plan.duration_ns)

    def test_the_actual_finish_is_fed_to_the_real_reader_and_moves_nothing(self):
        # The r7 receipt finished 79,025,666 ns after its planned end. Here that overshoot is
        # actually given to the product's own derivation and to a real receipt v4 - the place
        # the previous `finished_at_utc` rule defined the window - and the slot accounting is
        # identical every time. The last frame's own finish clock moves with it.
        derive = _interface(TELEMETRY, "derive_frame_evidence_v4")
        plan = _wire_plan(duration_ns=3 * NS)
        # Every collection is on time; only the session's own finish moves past the planned
        # end, which is what post-window cleanup actually looks like.
        frames = _pull_frames(plan)
        baseline = None
        for overshoot in (1, 79_025_666, 500_000_000):
            with self.subTest(overshoot_ns=overshoot):
                _, coverage, _ = derive(frames, plan=plan)
                observed = {item.lane: (item.expected_slots, item.observed_slots, item.missing_slots)
                            for item in coverage}
                self.assertEqual(observed, {"gpu_fast": (12, 12, 0), "host_slow": (3, 3, 0)})
                if baseline is None:
                    baseline = observed
                self.assertEqual(observed, baseline)
                receipt = _receipt_v4(plan, frames, overshoot_ns=overshoot)
                self.assertEqual(receipt.status, "complete")
                self.assertEqual(receipt.planned_end_monotonic_ns,
                                 plan.started_monotonic_ns + plan.duration_ns)
                self.assertEqual(receipt.finished_monotonic_ns,
                                 receipt.planned_end_monotonic_ns + overshoot)
                self.assertEqual({item.lane: item.missing_slots for item in receipt.slot_coverage},
                                 {"gpu_fast": 0, "host_slow": 0})
        # A frame scheduled at or past the frozen end is refused by the same reader.
        with self.assertRaises(ValueError):
            derive(_pull_frames(plan) + (_pull_frame(plan, "host_slow", 3, stream_sequence=15),),
                   plan=plan)

    def test_a_collection_that_overruns_its_own_deadline_is_late_and_incomplete(self):
        # Root's counterexample: the last GPU capture finishes after its frozen deadline, so
        # every slot is still present, yet the run is not complete. Pro C1 separates a late
        # response from a missing slot; a derivation that only reads `quality.status` cannot
        # see this, because the status is what the collector claimed rather than what it did.
        derive = _interface(TELEMETRY, "derive_frame_evidence_v4")
        plan = _wire_plan(duration_ns=3 * NS)
        period_ns = plan.gpu_nominal_interval_ms * 1_000_000
        for over_deadline_ns in (1, 100_000_000):
            with self.subTest(over_deadline_ns=over_deadline_ns):
                frames = []
                for frame in _pull_frames(plan):
                    last_gpu = (frame.lane == "gpu_fast"
                                and frame.clock.scheduled_monotonic_ns
                                == plan.planned_end_monotonic_ns - period_ns)
                    frames.append(_late_frame(frame, over_deadline_ns) if last_gpu else frame)
                quality, coverage, _ = derive(tuple(frames), plan=plan)
                by_lane = {item.lane: item for item in coverage}
                self.assertEqual(by_lane["gpu_fast"].missing_slots, 0,
                                 "the slot itself was answered; only its deadline was missed")
                late = {item.lane: item.late_sample_count for item in quality}
                self.assertGreaterEqual(
                    late["gpu_fast"], 1,
                    "a capture that finished after min(scheduled + period, planned end) is late; "
                    f"the derivation reported {late} for an overrun of {over_deadline_ns} ns")
                self.assertEqual(_receipt_v4(plan, tuple(frames)).status, "incomplete")

    def test_both_lanes_dropping_the_same_final_second_is_still_missing(self):
        # Pro's counterexample, through the real derivation over a real 120 s frame set: an end
        # taken from the last scheduled frame would let both lanes agree on 119 s and call
        # themselves complete. The frozen plan reports the gap, and the receipt is incomplete.
        derive = _interface(TELEMETRY, "derive_frame_evidence_v4")
        plan = _wire_plan()
        full = _pull_frames(plan)
        self.assertEqual(len(full), 600)
        _, coverage, _ = derive(full, plan=plan)
        self.assertEqual({item.lane: item.missing_slots for item in coverage},
                         {"gpu_fast": 0, "host_slow": 0})
        dropped = [("gpu_fast", index) for index in range(476, 480)] + [("host_slow", 119)]
        short = _pull_frames(plan, drop=dropped)
        _, coverage, _ = derive(short, plan=plan)
        by_lane = {item.lane: item for item in coverage}
        self.assertEqual((by_lane["gpu_fast"].missing_slots, by_lane["gpu_fast"].trailing_missing), (4, 4))
        self.assertEqual((by_lane["host_slow"].missing_slots, by_lane["host_slow"].trailing_missing), (1, 1))
        self.assertEqual(_receipt_v4(plan, short).status, "incomplete")

    def test_head_interior_and_partial_final_slots_are_separated(self):
        coverage = _interface(POLICY, "slot_coverage")
        plan = _plan()
        interior = coverage(plan, "host_slow", _slots(plan, "host_slow", drop=(60,)))
        self.assertEqual((interior.interior_missing, interior.leading_missing,
                          interior.trailing_missing), (1, 0, 0))
        head = coverage(plan, "host_slow", _slots(plan, "host_slow", drop=(0, 1)))
        self.assertEqual((head.leading_missing, head.interior_missing), (2, 0))
        empty = coverage(plan, "host_slow", [])
        self.assertEqual((empty.observed, empty.missing, empty.leading_missing), (0, 120, 120))
        # A duration that is not a whole number of periods still owns its partial final slot.
        partial = _plan(duration_ns=120 * NS + 400_000_000)
        self.assertEqual(coverage(partial, "gpu_fast", _slots(partial, "gpu_fast")).expected, 482)

    def test_a_cancelled_run_never_shortens_its_own_plan(self):
        coverage = _interface(POLICY, "slot_coverage")
        plan = _plan()
        cancelled = coverage(plan, "host_slow", _slots(plan, "host_slow")[:30])
        self.assertEqual((cancelled.expected, cancelled.observed, cancelled.trailing_missing),
                         (120, 30, 90))
        self.assertFalse(cancelled.complete)

    def test_duplicate_reordered_and_off_grid_slots_are_refused(self):
        coverage = _interface(POLICY, "slot_coverage")
        plan = _plan()
        legal = _slots(plan, "host_slow")
        for name, stamps in (
            ("duplicate", legal[:5] + [legal[4]]),
            ("reordered", [legal[3], legal[1]]),
            ("off_grid", [legal[0] + 1]),
            ("before_start", [plan.start_ns - plan.period_ns("host_slow")]),
            ("after_end", [plan.end_ns]),
        ):
            with self.subTest(case=name), self.assertRaises(ValueError):
                coverage(plan, "host_slow", stamps)

    def test_the_plan_refuses_a_duration_or_cadence_it_cannot_own(self):
        for name, changes in (("zero_duration", {"duration_ns": 0}),
                              ("over_ceiling", {"duration_ns": 8501 * NS}),
                              ("unsupported_gpu_cadence", {"gpu_period_ns": 300_000_000})):
            with self.subTest(case=name), self.assertRaises(ValueError):
                _plan(**changes)


def _pull_payload(*, after=7, nonce=NONCE, native_ppm=0, native_origin_ns=10 ** 6,
                  utc_shift_seconds=0.0, lane="gpu_fast", request_received_ns=None, **changes):
    """Exact canonical pull bytes as the native endpoint composes them."""
    from tests.unit.test_windows_resident_telemetry import _payload

    inner = json.loads(_payload(after + 1, lane=lane))
    scale = Fraction(1_000_000 + native_ppm, 1_000_000)
    native = lambda offset: native_origin_ns + int(offset * scale)  # noqa: E731 - local shorthand
    inner["sampled_monotonic_ns"] = native(20)
    inner["observed_at_utc"] = (START + timedelta(seconds=utc_shift_seconds)).isoformat()
    document = {"contract_version": "mineru.windows-resident-pull.v1", "request_nonce": nonce,
                "after_sequence": after,
                "request_received_monotonic_ns": (native(0) if request_received_ns is None
                                                  else request_received_ns),
                "sample_capture_finished_monotonic_ns": native(40),
                "reply_started_monotonic_ns": native(60), "sample": inner}
    document.update(changes)
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class R22FreshPullTests(unittest.TestCase):
    """D2/D3 - one Mac tick, one real capture per request, causal bounds only."""

    def _timing(self, **changes):
        timing_type = _interface(POLICY, "PullTiming")
        fields = {"request_nonce": NONCE, "after_sequence": 7, "sample_sequence": 8,
                  "local_request_ns": 1_000_000_000, "local_response_ns": 1_060_000_000,
                  "q_request_ns": 500, "q_capture_start_ns": 900,
                  "q_capture_end_ns": 40_000_900, "q_reply_ns": 41_000_000}
        fields.update(changes)
        return timing_type(**fields)

    def test_the_causal_interval_is_the_request_not_a_midpoint_or_offset(self):
        bounds = _interface(POLICY, "pull_capture_bounds")
        timing = self._timing()
        result = bounds(timing, expected_nonce=NONCE, expected_after_sequence=7)
        self.assertEqual((result.earliest_start_ns, result.latest_end_ns),
                         (timing.local_request_ns, timing.local_response_ns))
        self.assertEqual(result.receive_age_upper_ns, 60_000_000)
        self.assertEqual(result.width_ns, 60_000_000)

    def test_a_shifted_source_wall_clock_and_any_source_rate_are_accepted_end_to_end(self):
        # Pro B3: on the new path the source's UTC is diagnostic and its oscillator rate is not
        # a gate. This drives the real chain - canonical pull bytes, the product decoder, the
        # frame v3 provenance validator, the plan derivation and a real receipt - with the
        # source wall stepped by an hour and its clock running 120 ppm fast and slow. All of it
        # must be accepted while the causal bracket, session ordering and freshness hold.
        decode = _interface(WIRE, "decode_windows_resident_pull")
        derive = _interface(TELEMETRY, "derive_frame_evidence_v4")
        plan = _wire_plan(duration_ns=3 * NS)
        for name, ppm, shift in (("baseline", 0, 0.0), ("fast_120ppm", 120, 0.0),
                                 ("slow_120ppm", -120, 0.0), ("wall_step_back", 0, -3600.0),
                                 ("wall_step_forward", 0, 3600.0),
                                 ("wall_step_and_fast_rate", 120, 0.585)):
            with self.subTest(case=name):
                pull = decode(_pull_payload(native_ppm=ppm, utc_shift_seconds=shift), lane="gpu_fast")
                self.assertEqual(pull.after_sequence, 7)
                self.assertEqual(pull.sample.sequence, 8)
                frames = _pull_frames(plan, native_ppm=ppm, utc_shift_seconds=shift)
                _, coverage, _ = derive(frames, plan=plan)
                self.assertEqual({item.lane: item.missing_slots for item in coverage},
                                 {"gpu_fast": 0, "host_slow": 0})
                receipt = _receipt_v4(plan, frames, wall_extra_ns=int(shift * NS))
                # The divergence is recorded, never a gate.
                self.assertEqual(receipt.status, "complete")
                self.assertGreaterEqual(receipt.observed_clock_divergence_ns, 0)

    def test_the_mac_interval_ignores_the_native_rate_it_never_shares(self):
        bounds = _interface(POLICY, "pull_capture_bounds")
        reference = bounds(self._timing(), expected_nonce=NONCE, expected_after_sequence=7)
        for name, ppm in (("fast_50ppm", 50), ("slow_50ppm", -50),
                          ("fast_120ppm", 120), ("slow_120ppm", -120)):
            scale = Fraction(1_000_000 + ppm, 1_000_000)
            base = self._timing()
            scaled = self._timing(
                q_request_ns=int(base.q_request_ns * scale),
                q_capture_start_ns=int(base.q_capture_start_ns * scale),
                q_capture_end_ns=int(base.q_capture_end_ns * scale),
                q_reply_ns=int(base.q_reply_ns * scale))
            with self.subTest(case=name):
                moved = bounds(scaled, expected_nonce=NONCE, expected_after_sequence=7)
                self.assertEqual((moved.earliest_start_ns, moved.latest_end_ns),
                                 (reference.earliest_start_ns, reference.latest_end_ns))

    def _serve(self, replies):
        """A loopback exporter that answers the collector's own nonce and cursor.

        `replies` maps a cursor to a function of (cursor, nonce) returning the exact bytes for
        that request, so a case can echo a foreign nonce, repeat a stale capture or overlap the
        previous reply without the collector's random nonce guard being weakened.
        """
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import re as _re
        import threading as _threading

        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                match = _re.fullmatch(r"/[a-z_]+/after/(\d+)/request/([0-9a-f]{32})", self.path)
                if match is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                cursor, nonce = int(match.group(1)), match.group(2)
                seen.append((cursor, nonce))
                payload = replies[cursor](cursor, nonce)
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = _threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, seen

    def _collector(self, server):
        from tests.unit import test_windows_resident_telemetry as live

        build = _interface("disclosure_anchor.adapters.runtime.windows_resident_telemetry",
                           "build_windows_resident_telemetry_sampler")
        sampler = build({
            "lane": "gpu_fast", "base_url": f"http://127.0.0.1:{server.server_port}",
            "path": "/gpu_fast", "maximum_response_bytes": 65536,
            "maximum_sample_age_ms": 1000, "nominal_interval_ms": 250,
            "collector_identity_sha256": live.HASHES[0],
            "observer_clock_domain_identity_sha256": live.OBSERVER_CLOCK,
            "expected_identity": live._identity(),
            "pull_protocol": "mineru.windows-resident-pull.v1",
        })
        self.addCleanup(sampler.close)
        return sampler

    def _deadline(self):
        from disclosure_anchor.application.ports.synchronized_telemetry import (
            TelemetrySnapshotDeadline,
        )
        import time as _time

        return TelemetrySnapshotDeadline(_time.monotonic_ns() + 2_000_000_000)

    def test_the_live_collector_path_keeps_no_wall_or_rate_gate(self):
        # Two sequential requests really traverse the collector's continuity checks: the source
        # clock must advance and this request must arrive after the previous reply. Between them
        # the source's wall clock steps by an hour and its oscillator runs 120 ppm fast or slow,
        # and Pro B3 step 5 says neither may refuse the sample.
        for name, ppm, shift in (("fast_120ppm_and_wall_step", 120, 3600.0),
                                 ("slow_120ppm_and_wall_step_back", -120, -3600.0)):
            with self.subTest(case=name):
                origin = 10 ** 9

                def reply(cursor, nonce, ppm=ppm, shift=shift, origin=origin):
                    # Each reply advances the source's own clock; the wall step applies from the
                    # second sample, so the pair spans the discontinuity.
                    return _pull_payload(after=cursor, nonce=nonce, native_ppm=ppm,
                                         native_origin_ns=origin + cursor * 250_000_000,
                                         utc_shift_seconds=shift if cursor else 0.0)

                server, seen = self._serve({0: reply, 1: reply})
                sampler = self._collector(server)
                first = sampler.snapshot(deadline=self._deadline())
                second = sampler.snapshot(deadline=self._deadline())
                self.assertEqual([cursor for cursor, _ in seen], [0, 1])
                self.assertEqual(len({nonce for _, nonce in seen}), 2,
                                 "each request must carry its own nonce")
                for index, snapshot in enumerate((first, second)):
                    provenance = snapshot.resident_exporter_provenance
                    self.assertIsNotNone(provenance)
                    self.assertEqual(provenance.after_sequence, index)
                    self.assertEqual(provenance.request_nonce, seen[index][1])
                    self.assertLessEqual(provenance.local_request_monotonic_ns,
                                         provenance.local_response_monotonic_ns)

    def test_the_collector_still_refuses_a_foreign_cached_or_overlapping_reply(self):
        origin = 10 ** 9

        def honest(cursor, nonce, origin=origin):
            return _pull_payload(after=cursor, nonce=nonce,
                                 native_origin_ns=origin + cursor * 250_000_000)

        cases = {
            "foreign_nonce": lambda cursor, nonce: _pull_payload(
                after=cursor, nonce="f" * 32, native_origin_ns=origin + 250_000_000),
            "answers_another_cursor": lambda cursor, nonce: _pull_payload(
                after=cursor + 1, nonce=nonce, native_origin_ns=origin + 250_000_000),
            "cached_source_clock": lambda cursor, nonce: _pull_payload(
                after=cursor, nonce=nonce, native_origin_ns=origin),
            # The capture itself advances, but the exporter claims it received this request
            # before it had replied to the previous one.
            "reply_overlaps_the_previous_one": lambda cursor, nonce: _pull_payload(
                after=cursor, nonce=nonce, native_origin_ns=origin + 250_000_000,
                request_received_ns=origin + 1),
        }
        for name, second in cases.items():
            with self.subTest(case=name):
                server, _ = self._serve({0: honest, 1: second})
                sampler = self._collector(server)
                sampler.snapshot(deadline=self._deadline())
                with self.assertRaises(ValueError):
                    sampler.snapshot(deadline=self._deadline())


class R22ResourceEdgeTests(unittest.TestCase):
    """D4 - counters and gauges consume the same proved capture interval."""

    def _point(self, sequence, start_ns, end_ns, value, *, supported=True, epoch=EPOCH_SHA):
        point_type = _interface(POLICY, "CounterPoint")
        return point_type(sequence=sequence, epoch_sha256=epoch, earliest_start_ns=start_ns,
                          latest_end_ns=end_ns, value=value, supported=supported)

    def test_a_stale_reply_after_the_window_can_never_certify_zero(self):
        # Pro's edge: window [10,20) s, the increment happens at 19.95 s, and a GET issued at
        # 20.1 s returns a value captured at 19.9 s. Judged by request time that is a zero
        # delta; judged by the capture interval it is not an after-edge at all.
        envelope = _interface(POLICY, "counter_envelope")
        window = {"window_start_ns": 10 * NS, "window_end_ns": 20 * NS}
        stale = [self._point(1, 9 * NS, int(9.5 * NS), 0),
                 self._point(2, int(19.8 * NS), int(19.9 * NS), 0)]
        with self.assertRaises(ValueError):
            envelope(stale, **window)
        # Only a capture that provably started after the window closes is an after edge,
        # and it reports the increment the window could contain.
        honest = stale + [self._point(3, int(20.2 * NS), int(20.3 * NS), 1)]
        result = envelope(honest, **window)
        self.assertEqual((result.before_sequence, result.after_sequence, result.outer_delta),
                         (1, 3, 1))
        self.assertNotEqual(result.outer_delta, 0)

    def test_a_capture_that_straddles_an_edge_is_not_an_edge(self):
        envelope = _interface(POLICY, "counter_envelope")
        window = {"window_start_ns": 10 * NS, "window_end_ns": 20 * NS}
        straddling = [self._point(1, int(9.9 * NS), int(10.1 * NS), 0),
                      self._point(2, int(20.2 * NS), int(20.3 * NS), 0)]
        with self.assertRaises(ValueError):
            envelope(straddling, **window)

    def test_unsupported_reset_gap_or_foreign_epoch_inside_the_envelope_fails_closed(self):
        envelope = _interface(POLICY, "counter_envelope")
        window = {"window_start_ns": 10 * NS, "window_end_ns": 20 * NS}
        good = [self._point(1, 8 * NS, 9 * NS, 5),
                self._point(2, 12 * NS, 13 * NS, 5),
                self._point(3, 21 * NS, 22 * NS, 5)]
        self.assertEqual(envelope(good, **window).outer_delta, 0)
        for name, points in (
            ("unsupported", [good[0], self._point(2, 12 * NS, 13 * NS, None, supported=False), good[2]]),
            ("reset", [good[0], self._point(2, 12 * NS, 13 * NS, 4), good[2]]),
            ("gap", [good[0], self._point(4, 21 * NS, 22 * NS, 5)]),
            ("foreign_epoch", [good[0], self._point(2, 12 * NS, 13 * NS, 5, epoch="sha256:" + "44" * 32),
                               good[2]]),
        ):
            with self.subTest(case=name), self.assertRaises(ValueError):
                envelope(points, **window)

    def test_a_gauge_keeps_every_sample_that_could_overlap_the_window(self):
        overlaps = _interface(POLICY, "possibly_overlaps")
        bounds_type = _interface(POLICY, "CaptureBounds")

        def bounds(start_ns, end_ns):
            return bounds_type(earliest_start_ns=start_ns, latest_end_ns=end_ns,
                               receive_age_upper_ns=end_ns - start_ns,
                               round_trip_ns=end_ns - start_ns, native_service_ns=1,
                               source_collection_ns=1)

        window = {"start_ns": 10 * NS, "end_ns": 20 * NS}
        # A capture whose midpoint falls outside still could have observed the window.
        self.assertTrue(overlaps(bounds(int(9.4 * NS), int(10.1 * NS)), **window))
        self.assertTrue(overlaps(bounds(int(19.9 * NS), int(20.6 * NS)), **window))
        self.assertTrue(overlaps(bounds(12 * NS, 13 * NS), **window))
        self.assertFalse(overlaps(bounds(8 * NS, 9 * NS), **window))
        self.assertFalse(overlaps(bounds(21 * NS, 22 * NS), **window))


class R22FiniteBudgetTests(unittest.TestCase):
    """D7 - one derived vector, and the ceilings each layer actually enforces."""

    def _budget(self, **changes):
        budget_type = _interface(POLICY, "CoverageBudget")
        fields = {"entry_to_spawn_ns": 360 * NS, "launcher_transport_ns": 7605 * NS,
                  "finish_poll_allowance_ns": 5 * NS, "retrieval_ns": 420 * NS,
                  "driver_start_ns": 30 * NS, "driver_launch_ns": 30 * NS,
                  "driver_cleanup_ns": 30 * NS, "edge_reserve_each_ns": 2 * NS}
        fields.update(changes)
        return budget_type(**fields)

    def test_the_g3_vector_requires_8484_and_fits_8500_with_sixteen_to_spare(self):
        budget = self._budget()
        self.assertEqual(budget.required_ns, 8484 * NS)
        self.assertEqual(budget.require_fits(8500 * NS), 16 * NS)

    def test_the_old_window_and_an_over_ceiling_window_are_both_refused(self):
        budget = self._budget()
        for duration in (8300 * NS, 8483 * NS, 8501 * NS):
            with self.subTest(duration_s=duration // NS), self.assertRaises(ValueError):
                budget.require_fits(duration)
        self.assertEqual(budget.require_fits(8484 * NS), 0)

    def test_the_four_ceilings_are_derived_from_one_another(self):
        sample = _interface(POLICY, "SAMPLE_MAX_SECONDS")
        lane = _interface(POLICY, "LANE_MAX_SECONDS")
        wire = _interface(POLICY, "WIRE_MAX_SECONDS")
        command = _interface(POLICY, "FINITE_COMMAND_MAX_SECONDS")
        pre_go = _interface(POLICY, "PRE_GO_MAX_SECONDS")
        post = _interface(POLICY, "POST_SAMPLE_MAX_SECONDS")
        self.assertEqual((sample, pre_go, post), (8500, 20, 60))
        self.assertEqual(lane, sample + pre_go + post)
        self.assertEqual(wire, lane + 10)
        self.assertEqual(command, wire + 10)
        self.assertEqual((lane, wire, command), (8580, 8590, 8600))

    def test_the_pre_go_reserve_is_a_hard_local_bound(self):
        headroom = _interface(POLICY, "require_start_headroom")
        headroom(earliest_starter_spawn_ns=NS, sampling_start_ns=NS + 20 * NS)
        with self.assertRaises(ValueError):
            headroom(earliest_starter_spawn_ns=NS, sampling_start_ns=NS + 20 * NS + 1)
        with self.assertRaises(ValueError):
            headroom(earliest_starter_spawn_ns=2 * NS, sampling_start_ns=NS)

    def test_the_linux_lane_refuses_a_lifetime_above_the_finite_command_ceiling(self):
        # Exercised, not read: `run` validates lease/lifetime before it arms any timer, so a
        # lifetime past the finite command ceiling is refused with no side effect. The lower
        # half of this mirror - that an R22-legal lane lifetime is accepted - cannot be proved
        # here without a side-effect-free bound on the Linux side; that gap is reported.
        supervisor = importlib.import_module("scripts.windows.linux_resident_host_supervisor")
        sampler = importlib.import_module("scripts.windows.linux_resident_host_sampler")
        ceiling = _interface(POLICY, "FINITE_COMMAND_MAX_SECONDS")
        config = {"lease_ms": 30_000, "lifetime_ms": (ceiling + 1) * 1000}
        with self.assertRaises(ValueError) as refusal:
            supervisor.run(config, sampler, "sha256:" + "0" * 64, "sha256:" + "0" * 64)
        self.assertIn("lease/lifetime", str(refusal.exception))


class R22LifecycleTests(unittest.TestCase):
    """D6 - the native source closes before the expensive replay, and failures stay bounded.

    The owner is driven through its own session entry with the product's collaborators
    replaced by scripted ones, exactly as the established owner fixture does. Ordering is
    read from the recorded event sequence; the expensive replay is a gate this test opens,
    never a real sleep.
    """

    def _session(self, *, drained=True, replay_gate=True, starter_exits_early=False,
                 child_fails=False):
        import tempfile
        from pathlib import Path as _Path
        from unittest.mock import patch
        from types import SimpleNamespace as _NS

        owner = importlib.import_module("disclosure_anchor.adapters.runtime.resident_telemetry_owner")
        from disclosure_anchor.adapters.runtime.resident_owner_control import OwnerCommandResult
        from tests.unit.test_dedicated_mac_observer import _identity_bytes
        from tests.unit.test_resident_telemetry_owner import _fixture, retain_observer_artifacts

        with tempfile.TemporaryDirectory() as root:
            request, external, result = _fixture(_Path(root).resolve())
            # The sealed artifacts the child would have left; the owner reads its plan from them.
            retain_observer_artifacts(request, result)
            events = []
            # A starter ends when its own remote session closes, which is what lets the owner
            # reap it before the replay; nothing here ends it early on its own.
            closed_lanes = set()
            replay_released = threading.Event()
            if not replay_gate:
                replay_released.set()

            class Command:
                def __init__(self, phase, lane):
                    self.phase, self.lane, self.aborted = phase, lane, False

                def poll(self, *, timeout=0):
                    if (self.phase == "start" and self.lane not in closed_lanes
                            and not starter_exits_early):
                        return None
                    value = external[self.lane][0 if self.phase == "ready" else 1]
                    stdout = (value["job_raw"].encode() + b"\r\n" if self.phase == "start"
                              else canonical_bytes(value))
                    return OwnerCommandResult(0, stdout, b"synthetic warning\r\n")

                def abort(self):
                    self.aborted = True
                    events.append(("abort", self.lane))

                @property
                def captured_output(self):
                    return b"partial", b"warning"

                def retention_report(self):
                    return {"stdout_bytes": 7, "stderr_bytes": 7, "truncated": False}

            def launch(request, plan, *, phase, journal, container_id=None):
                lane = json.loads(plan.config_bytes)["lane"]
                events.append((phase, lane))
                return Command(phase, lane)

            class Observer:
                """The settled v4 parent API: events first, replay only after the close."""

                identity_bytes = _identity_bytes()

                def __init__(self, request):
                    self.request = request
                    # The child announces the very plan it retained on disk, which is what the
                    # owner re-reads and hashes before deriving any deadline from it.
                    plan = result.plan
                    plan_sha256 = artifact_sha256(canonical_bytes(plan.model_dump(mode="json")))
                    self._pending = [
                        {"kind": "plan_recorded", "run_id": request.run_id,
                         "sampling_plan_sha256": plan_sha256,
                         "started_monotonic_ns": plan.started_monotonic_ns,
                         "planned_end_monotonic_ns": plan.planned_end_monotonic_ns},
                    ]
                    if drained:
                        self._pending.append(
                            {"kind": "sampling_drained", "run_id": request.run_id,
                             "sampling_plan_sha256": plan_sha256,
                             "drained_monotonic_ns": plan.planned_end_monotonic_ns,
                             "frames_jsonl_sha256": artifact_sha256(b"frames"),
                             "frames_bytes": 1024, "frames_records": len(result.frames)})

                def start(self):
                    events.append(("GO", "once"))

                def poll_event(self, *, timeout):
                    if not self._pending:
                        return None
                    message = self._pending.pop(0)
                    events.append((message["kind"], "once"))
                    return message

                def wait_exit(self, *, timeout):
                    if child_fails:
                        raise RuntimeError("synthetic child failure")
                    events.append(("child-exit", "once"))
                    return True

                def replay_result(self, *, deadline_ns=None):
                    events.append(("replay-entered", "once"))
                    if not replay_released.wait(2.0):
                        raise TimeoutError("replay gate never opened")
                    events.append(("replay-returned", "once"))
                    return result

                def poll(self, *, timeout):
                    events.append(("legacy-poll", "once"))
                    return result

                def close(self):
                    events.append(("observer-close", "once"))

            def close_lane(ssh, ready, *, deadline_ns=None):
                events.append(("close", ready.lane))
                closed_lanes.add(ready.lane)
                replay_released.set()
                return external[ready.lane][1]["closed_raw"].encode()

            outcome = {}

            def session():
                try:
                    owner.run_resident_telemetry_session(request)
                except BaseException as exc:  # noqa: BLE001 - the failure itself is the evidence
                    outcome["failure"] = exc

            with patch.object(owner, "_launch_command", side_effect=launch), \
                    patch.object(owner, "DedicatedMacObserver", Observer), \
                    patch.object(owner, "MacObserverIdentityReader",
                                 return_value=_NS(observe=_identity_bytes)), \
                    patch.object(owner, "_close_lane", side_effect=close_lane):
                # A scripted session must finish promptly; a hang is itself a failure to report,
                # never something this suite waits out.
                worker = threading.Thread(target=session, daemon=True)
                worker.start()
                worker.join(SCRIPTED_SESSION_TIMEOUT_SECONDS)
                if worker.is_alive():
                    replay_released.set()
                    worker.join(SCRIPTED_SESSION_TIMEOUT_SECONDS)
                    self.fail("the scripted owner session did not return; last events: "
                              + repr(events[-8:]))
            return events, outcome.get("failure")

    def _reasons(self, failure):
        """Every original message in a failure, including an exception group's members."""
        if failure is None:
            return []
        members = getattr(failure, "exceptions", ())
        return [str(failure)] + [text for item in members for text in self._reasons(item)]

    def _order(self, events, kind):
        return next((index for index, item in enumerate(events) if item[0] == kind), None)

    def test_the_native_lanes_close_before_the_expensive_replay_is_entered(self):
        # The replay only returns once both lanes have closed, so a long JSONL cannot eat the
        # 30 s lease. No sleep: the close itself opens the gate.
        # A scripted observer cannot produce a sealed v4 result, so this decides the order
        # the owner actually performs, not the content of the replay it would consume.
        events, _ = self._session()
        drained = self._order(events, "sampling_drained")
        replay = self._order(events, "replay-entered")
        closes = [index for index, item in enumerate(events) if item[0] == "close"]
        self.assertIsNotNone(drained, f"no sampling_drained event was consumed: {events[:12]}")
        self.assertIsNotNone(replay, f"replay_result was never used: {events[:12]}")
        self.assertEqual(len(closes), 2, f"both lanes must close: {events}")
        self.assertLess(drained, min(closes), "the close must follow the drained message")
        self.assertLess(max(closes), replay, "the expensive replay must follow the native close")
        self.assertNotIn(("legacy-poll", "once"), events,
                         "a v4 session must not fall back to the superseded replay entry")

    def test_a_missing_drain_is_a_named_timeout_that_still_closes_and_never_completes(self):
        events, failure = self._session(drained=False, replay_gate=False)
        self.assertIsNotNone(failure, "a session with no drain message cannot succeed")
        reasons = self._reasons(failure)
        self.assertTrue(any("telemetry_drain_timeout" in text for text in reasons),
                        f"the drain timeout must be named; got {reasons}")
        self.assertEqual([item for item in events if item[0] == "close"].__len__(), 2,
                         f"the bounded close still runs after a drain timeout: {events}")

    def test_an_early_starter_exit_and_a_child_failure_both_stay_bounded(self):
        for name, changes in (("starter_exits_early", {"starter_exits_early": True}),
                              ("child_fails", {"child_fails": True})):
            with self.subTest(case=name):
                events, failure = self._session(replay_gate=False, **changes)
                self.assertIsNotNone(failure, "the original failure must reach the caller")
                self.assertNotIn(("replay-returned", "once"), events,
                                 "a failed session never claims a completed replay")


class R22OriginalEvidenceTests(unittest.TestCase):
    """D5 - a complete original v4 artifact set, replayed by the product, then tampered.

    The positive is written to disk exactly as the observer retains it and replayed through
    `verify_synchronized_telemetry_observer(receipt_version=4)`. The negatives change one
    original byte or line at a time, so each failure names a single conserved fact.
    """

    def _artifacts(self, directory, *, duration_ns=3 * NS):
        from disclosure_anchor.application.contracts.resident_session_evidence import artifact_sha256

        # One clock domain across the plan, its frames and its receipt: the replay binds them.
        plan = _wire_plan(duration_ns=duration_ns)
        frames = _pull_frames(plan)
        plan_bytes = canonical_bytes(plan.model_dump(mode="json"))
        run = Path(directory) / plan.run_id
        run.mkdir(parents=True, mode=0o700)

        def retain(name, payload):
            """Write one artifact exactly as the observer retains it: owner-only, complete."""
            path = run / name
            path.write_bytes(payload)
            path.chmod(0o600)
            return path

        retain("sampling-plan.v1.json", plan_bytes)
        lines = b"".join(canonical_bytes(frame.model_dump(mode="json")) + b"\n" for frame in frames)
        retain("frames.v3.jsonl", lines)
        receipt = _receipt_v4(plan, frames)
        receipt_document = receipt.model_dump(mode="json")
        receipt_document["sampling_plan_sha256"] = artifact_sha256(plan_bytes)
        receipt_document["artifacts"] = {"frames_jsonl_sha256": artifact_sha256(lines)}
        receipt = _interface(TELEMETRY, "SynchronizedTelemetryReceiptV4").model_validate(receipt_document)
        receipt_bytes = canonical_bytes(receipt.model_dump(mode="json"))
        retain("receipt.v4.json", receipt_bytes)
        seal = _interface(TELEMETRY, "SynchronizedTelemetrySealV4").model_validate({
            "run_id": plan.run_id, "receipt_sha256": artifact_sha256(receipt_bytes),
            "frames_jsonl_sha256": artifact_sha256(lines),
            "preseal_observer_process_cpu_started_ns": 0,
            "preseal_observer_process_cpu_finished_ns": 100,
            "preseal_observer_cpu_ns": 100,
            "sampling_elapsed_ns_denominator": plan.duration_ns,
            "receipt_status": receipt.status, "status": receipt.status,
            "sampling_plan_sha256": artifact_sha256(plan_bytes),
            # The lifecycle is the receipt's own interval; the denominator stays the frozen
            # duration, which is what keeps a long cleanup out of the CPU ratio.
            "lifecycle_elapsed_ns": receipt.finished_monotonic_ns - receipt.started_monotonic_ns,
            "frames_records": len(frames), "frames_bytes": len(lines),
        })
        retain("seal.v4.json", canonical_bytes(seal.model_dump(mode="json")))
        self.retain = retain
        return plan, run, lines

    def test_an_original_v4_artifact_set_replays_and_one_changed_byte_does_not(self):
        import tempfile
        replay = _interface("disclosure_anchor.adapters.runtime.synchronized_telemetry_observer",
                            "verify_synchronized_telemetry_observer")
        with tempfile.TemporaryDirectory() as root:
            plan, run, lines = self._artifacts(root)
            result = replay(artifact_root=Path(root), run_id=plan.run_id, receipt_version=4)
            self.assertEqual(len(result.frames), 15)
            self.assertEqual(result.receipt.status, "complete")
            conserved = {"records": len(result.frames), "bytes": len(lines)}
            for name, mutated in (
                ("one_byte_changed", lines.replace(b'"sequence":3', b'"sequence":4', 1)),
                ("one_line_removed", b"".join(lines.splitlines(keepends=True)[:-1])),
                ("one_line_added", lines + lines.splitlines(keepends=True)[-1]),
                ("trailing_byte", lines + b" "),
            ):
                with self.subTest(case=name):
                    self.retain("frames.v3.jsonl", mutated)
                    with self.assertRaises(ValueError):
                        replay(artifact_root=Path(root), run_id=plan.run_id, receipt_version=4)
            self.retain("frames.v3.jsonl", lines)
            self.assertEqual(
                len(replay(artifact_root=Path(root), run_id=plan.run_id, receipt_version=4).frames),
                conserved["records"])

    def test_a_foreign_plan_or_receipt_never_certifies_this_run(self):
        import tempfile
        replay = _interface("disclosure_anchor.adapters.runtime.synchronized_telemetry_observer",
                            "verify_synchronized_telemetry_observer")
        with tempfile.TemporaryDirectory() as root:
            plan, run, _ = self._artifacts(root)
            original = (run / "sampling-plan.v1.json").read_bytes()
            foreign = json.loads(original)
            foreign["duration_ns"] = plan.duration_ns + NS
            foreign["planned_end_monotonic_ns"] = plan.started_monotonic_ns + foreign["duration_ns"]
            self.retain("sampling-plan.v1.json", canonical_bytes(foreign))
            with self.assertRaises(ValueError):
                replay(artifact_root=Path(root), run_id=plan.run_id, receipt_version=4)
            self.retain("sampling-plan.v1.json", original)
            receipt = json.loads((run / "receipt.v4.json").read_bytes())
            receipt["sampling_plan_sha256"] = "sha256:" + "0" * 64
            self.retain("receipt.v4.json", canonical_bytes(receipt))
            with self.assertRaises(ValueError):
                replay(artifact_root=Path(root), run_id=plan.run_id, receipt_version=4)


class R22ControlRecordTests(unittest.TestCase):
    """D6 - the control channel's settled reading rules, at the real socket.

    Root's partial-record counterexample is the shape these protect: a control record that
    arrives in pieces must complete exactly once, under the single absolute deadline its first
    byte started, and a record that never completes must fail once and stay failed. Nothing
    here re-implements the reader; every case drives the product's own reader over a real
    socket pair.
    """

    def _reader(self):
        module = importlib.import_module("disclosure_anchor.adapters.runtime.dedicated_mac_observer")
        reader_type = _interface("disclosure_anchor.adapters.runtime.dedicated_mac_observer",
                                 "_ControlRecordReader")
        parent, child = socket.socketpair()
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        return module, reader_type(), parent, child

    @staticmethod
    def _record(**changes):
        document = {"kind": "plan_recorded", "run_id": RUN_ID, "sampling_plan_sha256": INTENT_SHA,
                    "started_monotonic_ns": NS, "planned_end_monotonic_ns": 2 * NS}
        document.update(changes)
        payload = json.dumps(document, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode()
        return len(payload).to_bytes(4, "big") + payload, payload

    def test_a_record_split_across_idle_polls_completes_exactly_once(self):
        _, reader, parent, child = self._reader()
        framed, payload = self._record()
        # Three fragments: a partial length prefix, the rest of it, then the body. Each poll in
        # between sees an incomplete record and must keep it rather than restart or reparse.
        delivered = None
        for fragment in (framed[:2], framed[2:6], framed[6:]):
            child.sendall(fragment)
            delivered = reader.poll(parent, timeout=0.2)
            if delivered is None:
                self.assertTrue(reader.partial, "an incomplete record keeps its buffer")
        self.assertEqual(delivered, payload, "the fragmented record must complete exactly once")
        self.assertFalse(reader.partial, "a completed record leaves no partial buffer")
        # The next idle poll sees no new record rather than re-parsing the last one.
        self.assertIsNone(reader.poll(parent, timeout=0.05))

    def test_two_records_in_one_segment_are_delivered_one_at_a_time(self):
        _, reader, parent, child = self._reader()
        first, first_payload = self._record()
        second, second_payload = self._record(started_monotonic_ns=2 * NS,
                                              planned_end_monotonic_ns=3 * NS)
        child.sendall(first + second)
        self.assertEqual(reader.poll(parent, timeout=0.2), first_payload)
        self.assertEqual(reader.poll(parent, timeout=0.2), second_payload)

    def test_a_started_record_that_never_completes_fails_once_on_its_own_deadline(self):
        from unittest.mock import patch

        module, reader, parent, child = self._reader()
        framed, _ = self._record()
        # The record deadline is the product's; it is shortened here so the case stays bounded.
        with patch.object(module, "_CONTROL_RECORD_SECONDS", 0.2):
            child.sendall(framed[:6])
            self.assertIsNone(reader.poll(parent, timeout=0.05))
            self.assertTrue(reader.partial, "the started record keeps its buffer between polls")
            with self.assertRaises(TimeoutError):
                for _ in range(40):
                    reader.poll(parent, timeout=0.05)
            # The deadline belongs to the record, so the reader stays failed rather than
            # accepting the rest of it later.
            child.sendall(framed[6:])
            with self.assertRaises(RuntimeError):
                reader.poll(parent, timeout=0.05)

    def test_an_impossible_length_prefix_and_a_closed_channel_each_fail_once(self):
        _, reader, parent, child = self._reader()
        child.sendall((10 ** 7).to_bytes(4, "big"))
        with self.assertRaises(ValueError):
            reader.poll(parent, timeout=0.2)
        with self.assertRaises(RuntimeError):
            reader.poll(parent, timeout=0.05)

        _, other, parent_two, child_two = self._reader()
        framed, _ = self._record()
        child_two.sendall(framed[:8])
        self.assertIsNone(other.poll(parent_two, timeout=0.05))
        child_two.close()
        with self.assertRaises(EOFError):
            other.poll(parent_two, timeout=0.2)


class R22EntryAndPackageTests(unittest.TestCase):
    """D8 - the same entries, the same historical schemas, no test payload shipped."""

    def test_the_existing_operational_schemas_keep_their_exported_bytes(self):
        documents = _interface(TELEMETRY, "operational_schema_documents")()
        from pathlib import Path
        exported = Path("contracts/operational")
        self.assertTrue(exported.is_dir(), "tracked operational schema directory is missing")
        checked = 0
        for path in sorted(exported.glob("*.schema.json")):
            if path.name not in documents:
                continue
            tracked = json.loads(path.read_bytes())
            self.assertEqual(documents[path.name], tracked,
                             f"exported schema changed for {path.name}")
            checked += 1
        self.assertGreater(checked, 0)

    def test_the_new_v4_schemas_join_the_same_registry_without_a_second_one(self):
        documents = _interface(TELEMETRY, "operational_schema_documents")()
        expected = ("synchronized-sampling-plan.v1.schema.json",
                    "synchronized-telemetry-frame.v3.schema.json",
                    "synchronized-telemetry-receipt.v4.schema.json",
                    "synchronized-telemetry-seal.v4.schema.json")
        missing = [name for name in expected if name not in documents]
        self.assertEqual(missing, [], "R22 interface not implemented yet: "
                                      + ", ".join(missing) + " in the operational registry")
        self.assertEqual(len(documents), len(set(documents)))

    def test_the_pull_envelope_is_decoded_as_a_closed_document(self):
        decode = _interface(WIRE, "decode_windows_resident_pull")
        payload = _pull_payload()
        accepted = decode(payload, lane="gpu_fast")
        self.assertEqual(accepted.contract_version, "mineru.windows-resident-pull.v1")
        self.assertEqual(accepted.request_nonce, NONCE)
        for name, broken in (
            ("duplicate_field", payload.replace(b'"after_sequence":7',
                                                b'"after_sequence":7,"after_sequence":7', 1)),
            ("noncanonical_spacing", payload.replace(b'"request_nonce":', b'"request_nonce" :', 1)),
            ("superseded_version", payload.replace(b"resident-pull.v1", b"resident-pull.v0", 1)),
            ("wrong_lane_model", payload),
        ):
            lane = "host_slow" if name == "wrong_lane_model" else "gpu_fast"
            with self.subTest(case=name), self.assertRaises(ValueError):
                decode(broken, lane=lane)

    def test_the_release_package_never_ships_the_acceptance_layer(self):
        # The release ships an explicit allow-list. The acceptance layer - this module, the
        # measured driver, the Windows mechanism tests - must never appear in it.
        files = _interface("disclosure_anchor.adapters.runtime.mineru_release_package",
                           "RELEASE_SOURCE_FILES")
        shipped = tuple(files.values()) + tuple(files)
        offending = [entry for entry in shipped
                     if "tests/" in entry or entry.startswith("tests") or "test_" in entry]
        self.assertEqual(offending, [], "the release source list ships the acceptance layer")

    def test_the_capacity_authority_used_by_these_cases_is_the_shared_one(self):
        # The acceptance layer never invents a capacity: it decodes the shared fixture bytes
        # with the product's own decoder, so a capacity change breaks here first.
        decode = _interface("disclosure_anchor.application.contracts.mineru_capacity_config",
                            "decode_mineru_capacity_config")
        config = decode(CAPACITY_BYTES)
        self.assertEqual(config.exact_bytes, CAPACITY_BYTES)


if __name__ == "__main__":
    unittest.main()
