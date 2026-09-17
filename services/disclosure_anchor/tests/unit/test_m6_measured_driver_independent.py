"""Deterministic self-checks for the measured-campaign driver's own orchestration.

No process, socket, SSH, observer or campaign runs here. These close the
driver's own failure families: a finite budget that must come from the
composition root, from its own pure launcher rule and from one absolute launcher
clock; the pre-flight that refuses a misdeclared transport or a short window
before any child is constructed; the original-frame gate; the bounded frame
probe; window coverage; the rule that retained evidence is only evidence when
its original bytes still pass the product's own resident checks; the two-child
supervision; and the rule that neither a summary exit code nor a valid
measurement is a business acceptance.

The resident positive below is composed from the product's own shared session
fixture, so it is a structural positive, not a real-machine acceptance; genuine
short-session bytes replace it when an actual session exists.
"""

import contextlib
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import signal
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime import m6_campaign_assembly as assembly
from disclosure_anchor.application.contracts.m6_delivery_report import (
    M6_DELIVERY_REPORT_MAX_BYTES, M6DeliveryReport,
)
from disclosure_anchor.application.contracts.resident_combined_cpu import check_combined_resident_cpu_v4
from disclosure_anchor.application.contracts.resident_session_evidence import (
    artifact_sha256, canonical_bytes, check_resident_closure, check_resident_ready,
)
from disclosure_anchor.application.contracts.synchronized_telemetry import SynchronizedTelemetryFrameV2
from disclosure_anchor.adapters.runtime.m6_campaign_private_binding import (
    CAMPAIGN_PRIVATE_BINDING_CONTRACT,
)
from disclosure_anchor.application.services.m6_launch_budget import launch_transport_budget
from disclosure_anchor.adapters.runtime import resident_telemetry_owner as owner_module
from tests import m6_support as m6
from tests.integration import m6_measured_campaign_independent as driver
from tests._mineru_capacity_config_fixture import CAPACITY_BYTES
from tests.m6_delivery_support import campaign_intent_for_spec, empty_report_wire, evaluation_plan
from tests.unit.test_resident_session_evidence import (
    _mapping_fixture, _resident_capacity, _resident_profile,
)
from tests.unit.test_resident_telemetry_owner import _fixture
from tests.unit.test_synchronized_telemetry_contract import HASH, HASH_B, HASH_C, RUN_ID, START, _frame


BOOT = "sha256:" + "1" * 64
ASSIGNMENT = "sha256:" + "2" * 64
IDENTITY = {lane: {"boot_identity_sha256": BOOT, "host_assignment_identity_sha256": ASSIGNMENT}
            for lane in ("gpu_fast", "host_slow")}
FROZEN = {"run_id": RUN_ID, "runtime_bundle_sha256": HASH, "process_profile_sha256": HASH_B,
          "lane_identity": IDENTITY}


def campaign_intent(*, planned_seconds, close_grace_seconds):
    """One frozen intent from the product's own shared campaign fixtures."""
    fixture = m6.make_fixture("e2e_publication", planned_seconds=planned_seconds,
                              close_grace_seconds=close_grace_seconds)
    return campaign_intent_for_spec(fixture.spec, evaluation_plan(), close_grace_seconds=close_grace_seconds)


# G3's frozen business window and a legitimately short one. The launcher transport each needs is
# never written down here: it is read from the composition root's own pure rule, so a product
# change moves these fixtures instead of silently disagreeing with them.
G3_INTENT = campaign_intent(planned_seconds=4800, close_grace_seconds=2400)
SHORT_INTENT = campaign_intent(planned_seconds=600, close_grace_seconds=120)
# R22 froze the driver's own three phase reserves at 30 s each and the window at 8500 s.
G3 = {"host_cadence_ms": 1000,
      "launcher_transport_timeout_seconds": driver.composition_root_transport_budget(G3_INTENT).timeout_seconds,
      "sampling_start_allowance_seconds": 30, "campaign_launch_allowance_seconds": 30,
      "cleanup_allowance_seconds": 30}
G3_DURATION = 8500
SHORT = {"host_cadence_ms": 1000,
         "launcher_transport_timeout_seconds": driver.composition_root_transport_budget(SHORT_INTENT).timeout_seconds,
         "sampling_start_allowance_seconds": 30, "campaign_launch_allowance_seconds": 30,
         "cleanup_allowance_seconds": 0}
SHORT_DURATION = 2100
# The intent's runtime binding fields, so a test can move exactly one of them apart.
_RUNTIME_FIELDS = ("source_commit", "source_manifest_sha256", "runtime_bundle_identity_sha256",
                   "process_profile_sha256", "worker_profile_sha256", "deployment_qualification_sha256")


def frame(*, lane="host_slow", sequence=0, started_ns=0, first=True, provenance=True, changes=()):
    """One v2 frame from the shared contract fixture plus resident provenance."""
    value = _frame(sequence=sequence, lane=lane, started_ns=started_ns, first=first).model_dump()
    value["contract_version"] = "mineru.synchronized-telemetry-frame.v2"
    if provenance:
        value["resident_exporter_provenance"] = {
            "exporter_source_sha256": HASH, "host_assignment_identity_sha256": ASSIGNMENT,
            "boot_identity_sha256": BOOT, "exporter_process_epoch_sha256": HASH_C,
            "wire_sequence": sequence + 1, "wire_observed_at_utc": value["clock"]["observed_at_utc"],
            "wire_sampled_monotonic_ns": started_ns,
        }
    for path, replacement in changes:
        target = value
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = replacement
    return SynchronizedTelemetryFrameV2.model_validate(value)


class FakeChild:
    """A child stand-in that records signals; it never owns a real process."""

    def __init__(self, *, label="fake", exit_code=None, exit_on_interrupt=None):
        self.label = label
        self._exit = exit_code
        self._on_interrupt = exit_on_interrupt
        self.interrupted = False
        self.killed = False

    def poll(self):
        return self._exit

    def wait(self, timeout):
        self.last_wait = timeout
        return self._exit

    def interrupt(self):
        self.interrupted = True
        if self._on_interrupt is not None:
            self._exit = self._on_interrupt

    def kill_group(self):
        self.killed = True
        self._exit = -9
        return self._exit


def owner_journal(request, external, result, *, drop=(), rewrite=(), reindex=True):
    """Write the evidence directory the owner retains, then its own hash index over it.

    `drop` removes files after indexing (named but absent), `rewrite` replaces
    bytes; `reindex` decides whether the index is recomputed over the rewritten
    bytes, which separates a corrupted file from a forged index.
    """
    directory = request.evidence_directory
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "owner-intent.json": canonical_bytes({
            "run_id": request.run_id, "duration_seconds": request.duration_seconds,
            "source_hashes": dict(request.source_hashes),
            "ssh_executable_sha256": request.ssh_executable_sha256,
            "qualification": "diagnostic-only; external runtime qualification and full-hour acceptance remain separate",
        }),
        "process-profile.json": request.process_profile_bytes,
        "local-sources.json": canonical_bytes({
            name: {"file": f"local-source-{index}.py", "sha256": driver.digest(payload)}
            for index, (name, payload) in enumerate(owner_module._local_sources().items())}),
    }
    readies, closures = {}, {}
    for lane, plan in zip(("gpu_fast", "host_slow"), (request.gpu, request.host), strict=True):
        first, last = external[lane]
        files[lane + "-config.json"] = plan.config_bytes
        files[lane + "-manifest.json"] = plan.manifest_bytes
        files[lane + "-ready.stdout"] = canonical_bytes(first)
        files[lane + "-closed.stdout"] = canonical_bytes(last)
        files[lane + "-start.stdout"] = last["job_raw"].encode() + b"\r\n"
        readies[lane] = check_resident_ready(
            config_bytes=plan.config_bytes, ready_bytes=first["ready_raw"].encode(),
            manifest_bytes=plan.manifest_bytes, expected_source_hashes=request.source_hashes)
        closures[lane] = check_resident_closure(
            ready=readies[lane], closed_bytes=last["closed_raw"].encode(),
            job_bytes=last["job_raw"].encode(),
            linux_closed_bytes=last["linux_closed_raw"].encode() if lane == "host_slow" else None)
    cpu = check_combined_resident_cpu_v4(gpu_ready=readies["gpu_fast"], host_ready=readies["host_slow"],
                                      gpu_closure=closures["gpu_fast"], host_closure=closures["host_slow"],
                                      receipt=result.receipt, seal=result.seal)
    for name, payload in rewrite:
        files[name] = payload
    index = {name: driver.digest(payload) for name, payload in files.items()}
    if not reindex:
        for name, _ in rewrite:
            index[name] = driver.digest(dict(files)[name] + b" ")
    for name, payload in files.items():
        if name not in drop:
            (directory / name).write_bytes(payload)
    document = {
        "contract_version": "mineru.resident-owner-diagnostic.v1", "run_id": request.run_id,
        "observer_status": result.evidence_status,
        "observer_receipt_sha256": driver.digest(canonical_bytes(result.receipt.model_dump(mode="json"))),
        "observer_seal_sha256": driver.digest(canonical_bytes(result.seal.model_dump(mode="json"))),
        "total_cpu_ns": cpu.total_cpu_ns, "within_two_percent": cpu.within_two_percent,
        "evidence_sha256": index, "activation_authorized": False,
    }
    (directory / "owner-result.json").write_bytes(canonical_bytes(document))
    return document


class MeasuredDriverBudgetTests(unittest.TestCase):
    def test_budget_terms_come_from_the_composition_root(self):
        base = driver.freeze_budget(intent=SHORT_INTENT, duration_seconds=SHORT_DURATION, **SHORT)
        original = assembly._PREPARE_TIMEOUT_SECONDS
        try:
            assembly._PREPARE_TIMEOUT_SECONDS = original + 100.0
            moved = driver.freeze_budget(intent=SHORT_INTENT, duration_seconds=SHORT_DURATION, **SHORT)
        finally:
            assembly._PREPARE_TIMEOUT_SECONDS = original
        self.assertEqual(moved["required_total_seconds"], base["required_total_seconds"] + 100.0)
        self.assertEqual(base["code_terms"]["prepare_timeout_seconds"], original)
        # The whole campaign, never the narrower owner-interval coverage the product also offers.
        self.assertEqual(base["coverage_policy"], "full_campaign")
        try:
            assembly._PREPARE_TIMEOUT_SECONDS = True
            with self.assertRaises(AssertionError):
                driver.freeze_budget(intent=SHORT_INTENT, duration_seconds=SHORT_DURATION, **SHORT)
        finally:
            assembly._PREPARE_TIMEOUT_SECONDS = original
        with self.assertRaises(AssertionError):
            driver._constant("_NO_SUCH_COMPOSITION_ROOT_TIMEOUT")
        with self.assertRaises(AssertionError):
            driver._int_constant("_SSH_OVERHEAD_SECONDS")  # a float term is not the integer the rule takes
        # Non-finite and mistyped declarations never reach the arithmetic as a satisfied budget.
        for field, value in (("host_cadence_ms", 1000.0), ("cleanup_allowance_seconds", True),
                             ("cleanup_allowance_seconds", float("inf")),
                             ("cleanup_allowance_seconds", float("nan")),
                             ("launcher_transport_timeout_seconds", 0),
                             ("launcher_transport_timeout_seconds", float("inf")),
                             ("launcher_transport_timeout_seconds", float("nan"))):
            with self.assertRaises(AssertionError):
                driver.freeze_budget(intent=SHORT_INTENT, duration_seconds=SHORT_DURATION,
                                     **{**SHORT, field: value})
        for duration in (0, float("inf"), float("nan"), "2000"):
            with self.assertRaises(AssertionError):
                driver.freeze_budget(intent=SHORT_INTENT, duration_seconds=duration, **SHORT)

    def test_the_declared_transport_must_equal_the_composition_root_rule(self):
        # The product's own pure rule, called here with the same intent and the same composition-root
        # constants the driver reads; nothing about its shape is restated in the driver.
        product = launch_transport_budget(
            planned_seconds=SHORT_INTENT.run.planned_seconds,
            close_grace_seconds=SHORT_INTENT.close_grace_seconds,
            ready_wait_seconds=SHORT_INTENT.ready_wait_seconds,
            ssh_overhead_seconds=assembly._SSH_OVERHEAD_SECONDS,
            exit_wait_extra_seconds=assembly._LAUNCHER_EXIT_WAIT_EXTRA_SECONDS,
            launcher_drain_seconds=assembly._LAUNCHER_DRAIN_SECONDS,
        )
        self.assertEqual(driver.composition_root_transport_budget(SHORT_INTENT).as_dict(), product.as_dict())
        accepted = driver.freeze_budget(intent=SHORT_INTENT, duration_seconds=SHORT_DURATION, **SHORT)
        self.assertEqual(accepted["problems"], [])
        self.assertEqual(accepted["composition_root_terms"], product.as_dict())
        # What the driver itself requires of any accepted transport: it reaches a delayed T0's close.
        self.assertGreaterEqual(accepted["declared_terms"]["launcher_transport_timeout_seconds"],
                                accepted["intent_terms"]["ready_deadline_seconds"] + accepted["business_seconds"])
        for declared in (product.timeout_seconds - 1, product.timeout_seconds + 1):
            off = driver.freeze_budget(intent=SHORT_INTENT, duration_seconds=SHORT_DURATION,
                                       **{**SHORT, "launcher_transport_timeout_seconds": declared})
            self.assertFalse(off["satisfied"])
            self.assertTrue(any(problem.startswith("declared_launcher_transport_differs_from_the"
                                                   "_composition_root_rule") for problem in off["problems"]))
        # Move one composition-root term and the only declaration the pre-flight accepts moves with it.
        original = assembly._LAUNCHER_EXIT_WAIT_EXTRA_SECONDS
        try:
            assembly._LAUNCHER_EXIT_WAIT_EXTRA_SECONDS = original + 30
            self.assertFalse(driver.freeze_budget(intent=SHORT_INTENT, duration_seconds=SHORT_DURATION,
                                                  **SHORT)["satisfied"])
            follows = driver.freeze_budget(
                intent=SHORT_INTENT, duration_seconds=SHORT_DURATION,
                **{**SHORT, "launcher_transport_timeout_seconds": product.timeout_seconds + 30})
            self.assertEqual(follows["problems"], [])
        finally:
            assembly._LAUNCHER_EXIT_WAIT_EXTRA_SECONDS = original
        # An intent the composition root's own rule refuses is refused here too, never approximated.
        refused = SimpleNamespace(run=SimpleNamespace(planned_seconds=4800),
                                  close_grace_seconds=2400, ready_wait_seconds=0)
        with self.assertRaises(AssertionError):
            driver.freeze_budget(intent=refused, duration_seconds=8300, **G3)

    def test_budget_blocks_a_transport_that_cannot_reach_the_business_close(self):
        # Root's counterexample: a 7200 s transport cannot hold a 30 s delayed READY plus 7200 s of
        # business. It is now refused twice - as a misdeclaration, and on the driver's own headroom.
        blocked = driver.freeze_budget(intent=G3_INTENT, duration_seconds=G3_DURATION,
                                       **{**G3, "launcher_transport_timeout_seconds": 7200})
        self.assertFalse(blocked["satisfied"])
        self.assertEqual(blocked["launcher_transport_headroom_seconds"], -120.0)
        self.assertIn("launcher_transport_cannot_cover_a_delayed_t0_and_the_business_close",
                      blocked["problems"])
        self.assertEqual(len(blocked["problems"]), 2)
        # Understating the transport makes the window look like it fits - 8300 s covers a 7200 s
        # transport - which is exactly why coverage alone could never have caught this.
        self.assertLess(blocked["required_total_seconds"], 8300)
        short = driver.freeze_budget(intent=SHORT_INTENT, duration_seconds=SHORT_DURATION, **SHORT)
        self.assertTrue(short["satisfied"])
        self.assertEqual(short["required_total_seconds"], 1974.0)
        self.assertEqual(short["margin_seconds"], SHORT_DURATION - 1974.0)
        self.assertGreater(short["launcher_transport_headroom_seconds"], 0)
        # One second less window than the frozen plan needs is one second too few.
        self.assertFalse(driver.freeze_budget(intent=SHORT_INTENT, duration_seconds=1973,
                                              **SHORT)["satisfied"])
        self.assertTrue(driver.freeze_budget(intent=SHORT_INTENT, duration_seconds=1974,
                                             **SHORT)["satisfied"])

    def test_the_g3_vector_fits_the_r22_window_with_sixteen_seconds_to_spare(self):
        # The R21 finding was that G3 needed more window than the old 8300 s ceiling allowed.
        # R22 resolves it by widening the finite window to 8500 s, not by covering less: the
        # required span still holds the whole campaign plus both edges and all three reserves.
        g3 = driver.freeze_budget(intent=G3_INTENT, duration_seconds=G3_DURATION, **G3)
        self.assertEqual(g3["problems"], [])
        self.assertTrue(g3["satisfied"])
        self.assertEqual(g3["required_total_seconds"], 8484.0)
        self.assertEqual(g3["margin_seconds"], 16.0)
        self.assertEqual(g3["driver_allowances"]["total_seconds"], 90.0)
        self.assertEqual(g3["coverage_policy"], "full_campaign")
        # The same vector against the superseded window is still short, by the same arithmetic.
        old = driver.freeze_budget(intent=G3_INTENT, duration_seconds=8300, **G3)
        self.assertEqual(old["problems"], ["sampling_window_shorter_than_the_required_span"])
        self.assertEqual(old["margin_seconds"], 8300 - 8484.0)
        # One second short of the frozen requirement is one second too few.
        self.assertFalse(driver.freeze_budget(intent=G3_INTENT, duration_seconds=8483,
                                              **G3)["satisfied"])
        self.assertTrue(driver.freeze_budget(intent=G3_INTENT, duration_seconds=8484,
                                             **G3)["satisfied"])

    def test_launcher_record_detects_a_divergence_it_cannot_authorize(self):
        # The entry writes this record before it spawns the launcher, but the driver polls it on its
        # own cadence: it is a consistency detector beside the run, never the pre-flight's authority.
        with tempfile.TemporaryDirectory() as root:
            campaign = Path(root)
            declared = SHORT["launcher_transport_timeout_seconds"]
            self.assertEqual(driver.launcher_transport_state(campaign, declared=declared), (False, None))
            record = campaign / "launcher-command.json"
            record.write_bytes(canonical_bytes({"timeout_seconds": declared, "argv": []}))
            self.assertEqual(driver.launcher_transport_state(campaign, declared=declared), (True, None))
            recorded, problem = driver.launcher_transport_state(campaign, declared=declared + 1)
            self.assertTrue(recorded)
            self.assertTrue(problem.startswith("launcher_transport_timeout_differs_from_the_frozen_budget"))
            record.unlink()
            record.write_bytes(canonical_bytes({"timeout_seconds": True, "argv": []}))
            self.assertEqual(driver.launcher_transport_state(campaign, declared=declared),
                             (True, "launcher_transport_timeout_unreadable"))


class MeasuredDriverFrameTests(unittest.TestCase):
    def test_frame_gate_accepts_an_original_lane_frame_and_names_every_defect(self):
        for lane in ("gpu_fast", "host_slow"):
            self.assertEqual(driver.frame_problems(frame(lane=lane), **FROZEN), ())
        cases = {
            "frame_run_id": frame(changes=((("run_id",), "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),)),
            "frame_process_profile": frame(changes=((("process_profile_sha256",), HASH_C),)),
            "frame_runtime_bundle": frame(changes=((("runtime_bundle_identity_sha256",), HASH_C),)),
            "frame_resident_provenance_absent": frame(provenance=False),
            "frame_boot_identity": frame(changes=((("resident_exporter_provenance", "boot_identity_sha256"),
                                                   "sha256:" + "9" * 64),)),
            "frame_host_assignment": frame(changes=(
                (("resident_exporter_provenance", "host_assignment_identity_sha256"), "sha256:" + "8" * 64),)),
            "frame_host_cgroup_unsupported": frame(changes=((("host_cgroup", "values"), None),
                                                            (("host_cgroup", "status"), "unsupported"),
                                                            (("host_cgroup", "reason"), "collector_unsupported"))),
            "frame_sample_quality": frame(first=False, sequence=1, started_ns=1_000_000_000, changes=(
                (("quality", "status"), "late"), (("quality", "missed_deadlines"), 1))),
        }
        for expected, case in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(driver.frame_problems(case, **FROZEN), (expected,))
        clean = frame(lane="gpu_fast")
        self.assertEqual(driver.trusted_frames((clean, cases["frame_run_id"]), **FROZEN), (clean,))
        self.assertEqual(driver.frame_identity(clean)["boot_identity_sha256"], BOOT)

    def test_frame_probe_reads_whole_records_inside_its_head_bound(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "frames.v2.jsonl"
            self.assertEqual(driver.probe_frames(path), ())
            records = [canonical_bytes(frame(lane="gpu_fast", sequence=index,
                                             started_ns=index * 250_000_000,
                                             first=index == 0).model_dump(mode="json"))
                       for index in range(2)]
            path.write_bytes(b"\n".join(records) + b"\n" + records[0][:20])
            probed = driver.probe_frames(path)
            self.assertEqual([record.sequence for record in probed], [0, 1])
            # A head bound that lands inside the first record yields nothing rather than a guess.
            self.assertEqual(driver.probe_frames(path, maximum_bytes=10), ())
            self.assertEqual([record.sequence for record in
                              driver.probe_frames(path, maximum_bytes=len(records[0]) + 1)], [0])

    def test_the_frame_gate_fails_closed_on_a_dead_owner_and_a_filled_head(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "frames.v2.jsonl"
            ticks = iter(range(0, 1000))
            wait = {"frames_path": path, "wait_seconds": 60, "sleep": lambda _: None,
                    "now": lambda: float(next(ticks)), **FROZEN}
            # Root's r1 shape: the owner exits before any lane samples; that is never sampling proof.
            with self.assertRaises(AssertionError) as dead:
                driver.await_trusted_frames(telemetry=FakeChild(exit_code=1), **wait)
            self.assertIn("exited before both lanes", str(dead.exception))
            with self.assertRaises(AssertionError) as late:
                driver.await_trusted_frames(telemetry=FakeChild(), **wait)
            self.assertIn("first-frame wait", str(late.exception))
            # Only one lane trusted and the bounded head already full: a named stop, not a longer wait.
            host = canonical_bytes(frame(lane="host_slow").model_dump(mode="json"))
            path.write_bytes((host + b"\n") * 4)
            with self.assertRaises(AssertionError) as filled:
                driver.await_trusted_frames(telemetry=FakeChild(), probe_bytes=len(host) + 1, **wait)
            self.assertIn("bounded frame head filled", str(filled.exception))
            gpu = canonical_bytes(frame(lane="gpu_fast", sequence=1).model_dump(mode="json"))
            foreign = canonical_bytes(frame(lane="gpu_fast", sequence=2, provenance=False).model_dump(mode="json"))
            path.write_bytes(foreign + b"\n" + host + b"\n" + gpu + b"\n")
            first, rejected, session_first = driver.await_trusted_frames(telemetry=FakeChild(), **wait)
            self.assertEqual(sorted(first), ["gpu_fast", "host_slow"])
            self.assertEqual(rejected, {"gpu_fast:frame_resident_provenance_absent": 1})
            # The session anchor is the first record written, trusted or not, never the first trusted one.
            self.assertEqual(session_first, min(f.clock.observed_at_utc for f in driver.probe_frames(path)))

    def test_coverage_requires_both_lanes_and_a_host_edge_after_the_close(self):
        class Receipt:
            started_at_utc = START - timedelta(seconds=30)
            finished_at_utc = START + timedelta(seconds=30)

        def frames_until(host_end_seconds):
            built = []
            for index in range(host_end_seconds + 1):
                built.append(frame(lane="host_slow", sequence=index, started_ns=index * 1_000_000_000,
                                   first=index == 0))
                built.append(frame(lane="gpu_fast", sequence=index, started_ns=index * 1_000_000_000,
                                   first=index == 0))
            return tuple(built)

        window = {"started_utc": START, "finished_utc": START + timedelta(seconds=4),
                  "host_cadence_ms": 1000}
        self.assertEqual(driver.coverage_problems(frames=frames_until(6), receipt=Receipt(), **window), ())
        self.assertEqual(
            driver.coverage_problems(frames=frames_until(4), receipt=Receipt(), **window),
            ("host_edge_sample_missing_after_campaign_close",),
        )
        host_only = tuple(f for f in frames_until(6) if f.lane == "host_slow")
        self.assertEqual(
            driver.coverage_problems(frames=host_only, receipt=Receipt(), **window),
            ("lane_without_trusted_frames:gpu_fast",),
        )
        self.assertEqual(
            driver.coverage_problems(frames=frames_until(6), receipt=Receipt(),
                                     **{**window, "host_cadence_ms": 500}),
            ("host_cadence_differs_from_the_frozen_configuration",),
        )

        class LateReceipt(Receipt):
            started_at_utc = START + timedelta(seconds=1)
            finished_at_utc = START + timedelta(seconds=3)

        self.assertEqual(
            driver.coverage_problems(frames=frames_until(6), receipt=LateReceipt(), **window),
            ("sampling_finished_before_campaign", "sampling_started_after_campaign"),
        )


class MeasuredDriverResidentEvidenceTests(unittest.TestCase):
    def _session(self, root):
        request, external, result = _fixture(Path(root))
        return request, external, result

    def _problems(self, request, result, document):
        return driver.resident_evidence_problems(
            request, frames=result.frames, receipt=result.receipt, seal=result.seal,
            sampling_plan=result.plan, result_document=document,
        )

    def test_retained_resident_evidence_passes_the_owner_s_own_checks(self):
        with tempfile.TemporaryDirectory() as root:
            request, external, result = self._session(root)
            document = owner_journal(request, external, result)
            problems, facts = self._problems(request, result, document)
            self.assertEqual(problems, ())
            self.assertEqual(sorted(facts), ["gpu_fast", "host_slow", "total_cpu_ns", "within_two_percent"])
            self.assertTrue(facts["within_two_percent"])
            self.assertEqual(facts["total_cpu_ns"], document["total_cpu_ns"])
            self.assertNotEqual(facts["gpu_fast"]["session"], facts["host_slow"]["session"])
            whole, _ = driver.owner_evidence_problems(
                request, frames=result.frames, receipt=result.receipt, seal=result.seal,
                sampling_plan=result.plan, receipt_sha256=document["observer_receipt_sha256"],
                seal_sha256=document["observer_seal_sha256"])
            self.assertEqual(whole, ())

    def test_named_evidence_without_original_bytes_never_passes(self):
        with tempfile.TemporaryDirectory() as root:
            request, external, result = self._session(root)
            # Root's counterexample: a complete index whose six raw lane files do not exist.
            named = [name for name in owner_journal(request, external, result)["evidence_sha256"]
                     if name.endswith(".stdout")]
            self.assertEqual(len(named), 6)
            for name in named:
                (request.evidence_directory / name).unlink()
            problems, facts = self._problems(request, result, owner_journal(
                request, external, result, drop=tuple(named)))
            self.assertTrue(any(problem.startswith("resident_evidence_rejected:gpu_fast") for problem in problems))
            self.assertTrue(any(problem.startswith("resident_evidence_rejected:host_slow") for problem in problems))
            self.assertEqual(facts, {})

    def test_truncated_forged_and_foreign_evidence_are_each_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            request, external, result = self._session(root)
            good = canonical_bytes(external["gpu_fast"][1])
            cases = {
                # Bytes changed under an unchanged index: the index read-back catches it first.
                "truncated": {"rewrite": (("gpu_fast-closed.stdout", good[: len(good) // 2]),),
                              "reindex": False},
                # Index forged to match modified bytes: the product's own checks reject the content.
                "forged": {"rewrite": (("gpu_fast-closed.stdout", canonical_bytes(
                    {**external["gpu_fast"][1], "observed_at_utc": "2000-01-01T00:00:00+00:00"}),),),
                    "reindex": True},
                # Another lane's original bytes in this lane's file.
                "foreign": {"rewrite": (("gpu_fast-closed.stdout",
                                         canonical_bytes(external["host_slow"][1])),), "reindex": True},
            }
            for label, mutation in cases.items():
                with self.subTest(case=label):
                    for name in list(request.evidence_directory.glob("*")):
                        name.unlink()
                    document = owner_journal(request, external, result, **mutation)
                    problems, facts = self._problems(request, result, document)
                    self.assertTrue(any(problem.startswith("resident_evidence_rejected:gpu_fast")
                                        for problem in problems), problems)
                    self.assertNotIn("gpu_fast", facts)
                    self.assertIn("host_slow", facts)

    def test_owner_result_claims_are_bound_to_the_independent_replay(self):
        with tempfile.TemporaryDirectory() as root:
            request, external, result = self._session(root)
            document = owner_journal(request, external, result)
            mismatched = {**document, "total_cpu_ns": document["total_cpu_ns"] + 1}
            problems, _ = self._problems(request, result, mismatched)
            self.assertEqual(problems, ("owner_result_cpu_differs_from_the_independent_replay",))
            self.assertEqual(self._problems(request, result, {**document, "evidence_sha256": None})[0],
                             ("owner_result_evidence_index_absent",))
            # The owner's own composition snapshot must still be this checkout's product source.
            drifted = canonical_bytes({"adapters.runtime.resident_telemetry_owner":
                                       {"file": "local-source-0.py", "sha256": HASH}})
            document = owner_journal(request, external, result,
                                     rewrite=(("local-sources.json", drifted),))
            self.assertEqual(self._problems(request, result, document)[0],
                             ("owner_composition_source_differs_from_this_checkout",))
            (request.evidence_directory / "owner-result.json").write_bytes(canonical_bytes(
                {**document, "observer_receipt_sha256": HASH, "within_two_percent": False}))
            whole, _ = driver.owner_evidence_problems(
                request, frames=result.frames, receipt=result.receipt, seal=result.seal,
                sampling_plan=result.plan, receipt_sha256=document["observer_receipt_sha256"],
                seal_sha256=document["observer_seal_sha256"])
            self.assertEqual([problem for problem in whole if problem.startswith("owner_result_differs")],
                             ["owner_result_differs:observer_receipt_sha256",
                              "owner_result_differs:within_two_percent"])
            (request.evidence_directory / "owner-result.json").unlink()
            with self.assertRaises(AssertionError):
                driver.owner_evidence_problems(
                    request, frames=result.frames, receipt=result.receipt, seal=result.seal,
                    sampling_plan=result.plan, receipt_sha256=HASH, seal_sha256=HASH_B)


class MeasuredDriverSupervisionTests(unittest.TestCase):
    def _supervise(self, campaign, telemetry, *, deadline_seconds=10.0, ticks=(0.0, 1.0, 2.0, 3.0),
                   transport_check=None):
        evidence, clock, slept = {}, iter(ticks), []
        outcome = driver.supervise(
            campaign=campaign, telemetry=telemetry, deadline_seconds=deadline_seconds, evidence=evidence,
            transport_check=transport_check, now=lambda: next(clock), sleep=slept.append, poll_seconds=0.25,
        )
        return outcome, evidence, slept

    def test_supervision_returns_the_first_finite_outcome(self):
        outcome, evidence, _ = self._supervise(FakeChild(exit_code=0), FakeChild())
        self.assertEqual((outcome, evidence["campaign_exit_code"]), ("campaign_exited", 0))
        outcome, evidence, _ = self._supervise(FakeChild(), FakeChild(exit_code=1))
        self.assertEqual(outcome, "telemetry_exited")
        self.assertEqual(evidence["stop_reason"], "telemetry_owner_exited_before_the_campaign")
        self.assertNotIn("campaign_exit_code", evidence)
        outcome, evidence, _ = self._supervise(FakeChild(), FakeChild(),
                                               transport_check=lambda: "launcher_transport_timeout_unreadable")
        self.assertEqual((outcome, evidence["stop_reason"]),
                         ("transport_rejected", "launcher_transport_timeout_unreadable"))
        outcome, evidence, slept = self._supervise(FakeChild(), FakeChild(), deadline_seconds=1.0,
                                                   ticks=(0.0, 0.5, 2.0))
        self.assertEqual((outcome, evidence["stop_reason"]), ("deadline", "campaign_span_budget_exceeded"))
        self.assertEqual(slept, [0.25])

    def test_forced_stops_record_an_unknown_remote_outcome(self):
        cooperative, evidence = FakeChild(exit_on_interrupt=0), {}
        driver.stop_campaign(cooperative, grace_seconds=5.0, evidence=evidence)
        self.assertTrue(cooperative.interrupted)
        self.assertFalse(cooperative.killed)
        self.assertEqual(evidence["campaign_exit_code"], 0)
        # Even a clean local stop never asserts that the remote owner is gone.
        self.assertTrue(evidence["owner_outcome"].startswith("unknown:"))
        self.assertNotIn("campaign_forced", evidence)

        stubborn, evidence = FakeChild(), {}
        driver.stop_campaign(stubborn, grace_seconds=5.0, evidence=evidence)
        self.assertTrue(stubborn.killed and evidence["campaign_forced"])
        self.assertEqual(evidence["campaign_exit_code"], -9)

        owner, evidence = FakeChild(exit_on_interrupt=1), {}
        driver.stop_telemetry(owner, grace_seconds=5.0, evidence=evidence)
        self.assertEqual(evidence["telemetry_exit_code"], 1)
        self.assertNotIn("telemetry_remote_outcome", evidence)

        killed, evidence = FakeChild(), {}
        driver.stop_telemetry(killed, grace_seconds=5.0, evidence=evidence)
        self.assertTrue(killed.interrupted and killed.killed)
        self.assertTrue(evidence["telemetry_remote_outcome"].startswith("unknown:"))

    def test_a_terminating_signal_reaches_the_cleanup_instead_of_orphaning_children(self):
        previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGHUP)}
        try:
            driver.install_stop_handlers()
            with self.assertRaises(driver.DriverStopped):
                signal.raise_signal(signal.SIGTERM)
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)


class MeasuredDriverGateTests(unittest.TestCase):
    @staticmethod
    def report(wire):
        # The same canonical decode the driver performs on the summary's own output file.
        return M6DeliveryReport.from_canonical_bytes(canonical_bytes(wire),
                                                     maximum_bytes=M6_DELIVERY_REPORT_MAX_BYTES)

    def test_measurement_gate_fails_on_unknown_resource_evidence(self):
        wire = empty_report_wire()
        report = self.report(wire)
        self.assertFalse(report.delivery_pass)
        self.assertEqual(driver.gate_problems(report, mode="run"),
                         ("resource_safety_unknown", "run_validity_unknown"))
        self.assertEqual(driver.gate_problems(report, mode="bootstrap-check"), ("resource_safety_unknown",))
        wire["resource_safety"] = {"status": "pass", "gates": None, "evidence_sha256": None,
                                   "reason": "resource_window_measured"}
        wire["run_validity"] = {**wire["run_validity"], "status": "complete"}
        wire["unknowns"] = ["telemetry_coverage_incomplete:host_slow"]
        # The summary can exit 0 and still leave the resource window unproven.
        self.assertEqual(driver.gate_problems(self.report(wire), mode="run"),
                         ("unknown:telemetry_coverage_incomplete:host_slow",))
        wire["unknowns"] = []
        self.assertEqual(driver.gate_problems(self.report(wire), mode="run"), ())

    def test_delivery_gate_never_passes_a_failed_business_run(self):
        wire = empty_report_wire()
        wire["resource_safety"] = {"status": "pass", "gates": None, "evidence_sha256": None,
                                   "reason": "resource_window_measured"}
        wire["run_validity"] = {**wire["run_validity"], "status": "complete"}
        wire["unknowns"] = []
        measured = self.report(wire)
        # Root's counterexample: a valid measurement of a run whose obligations are open is not a pass.
        self.assertEqual(driver.gate_problems(measured, mode="run"), ())
        self.assertEqual(driver.delivery_problems(measured, mode="run"),
                         ("delivery_pass_false", "business_obligations_open"))
        # A zero-admission rehearsal is never scored against the business obligations.
        self.assertEqual(driver.delivery_problems(measured, mode="bootstrap-check"), ())
        wire["business_obligations_closed"] = {**wire["business_obligations_closed"], "all_closed": True,
                                               "ack_or_absence_proven": True, "admission_reconciled": True,
                                               "external_owner_exit_verified": True,
                                               "local_children_reaped": True, "missing": []}
        self.assertEqual(driver.delivery_problems(self.report(wire), mode="run"), ("delivery_pass_false",))
        wire["delivery_pass"] = True
        self.assertEqual(driver.delivery_problems(self.report(wire), mode="run"), ())
        # The driver's own verdict keeps the two results apart and never reports a pass for either failure.
        self.assertEqual(driver.driver_status(measurement_problems=[], delivery_problems=[]), "pass")
        self.assertEqual(driver.driver_status(measurement_problems=[],
                                              delivery_problems=["delivery_pass_false"]), "delivery_failed")
        self.assertEqual(driver.driver_status(measurement_problems=["resource_safety_unknown"],
                                              delivery_problems=[]), "measurement_failed")
        self.assertEqual(driver.driver_status(measurement_problems=["resource_safety_unknown"],
                                              delivery_problems=["delivery_pass_false"]), "measurement_failed")


class SpawnBlocked(Exception):
    """Raised in place of a real spawn so a physical child never exists in a unit test."""


class MeasuredDriverPlanTests(unittest.TestCase):
    """The frozen plan is read back through the product's closed decoder and bound to the inputs."""

    PLAN_RUN = "3f2b6c31-0000-4000-8000-00000000d1d1"
    CLOCK_SHA = "sha256:" + "2" * 64

    def _plan_document(self, **changes):
        document = {
            "contract_version": "mineru.synchronized-sampling-plan.v1",
            "run_id": self.PLAN_RUN, "owner_intent_sha256": None,
            "observer_clock_domain_identity_sha256": self.CLOCK_SHA,
            "started_monotonic_ns": 5_000_000_000, "duration_ns": 120_000_000_000,
            "planned_end_monotonic_ns": 125_000_000_000,
            "gpu_nominal_interval_ms": 250, "host_nominal_interval_ms": 1000,
        }
        document.update(changes)
        return document

    def _request(self, **changes):
        fields = {"run_id": self.PLAN_RUN, "duration_seconds": 120,
                  "host": SimpleNamespace(config_bytes=canonical_bytes(
                      {"lane": "host_slow", "cadence_ms": 1000}))}
        fields.update(changes)
        return SimpleNamespace(**fields)

    def _pin(self, root, document=None, *, request=None, intent=None, clock=None):
        """Write the observer plan and the owner's retained intent, then pin them together."""
        base = Path(root)
        observer, evidence = base / "observer-run", base / "owner-evidence"
        observer.mkdir(parents=True, exist_ok=True)
        evidence.mkdir(parents=True, exist_ok=True)
        request = request or self._request()
        intent_document = intent if intent is not None else {
            "run_id": request.run_id, "duration_seconds": request.duration_seconds}
        intent_bytes = canonical_bytes(intent_document)
        (evidence / "owner-intent.json").write_bytes(intent_bytes)
        document = self._plan_document() if document is None else document
        if document.get("owner_intent_sha256") is None:
            document = {**document, "owner_intent_sha256": driver.digest(intent_bytes)}
        (observer / "sampling-plan.v1.json").write_bytes(canonical_bytes(document))
        return driver.pin_sampling_plan(
            observer_run=observer, request=request, evidence_directory=evidence,
            local_clock_domain_sha256=self.CLOCK_SHA if clock is None else clock)

    def test_the_frozen_plan_binds_to_the_retained_intent_and_this_observer(self):
        with tempfile.TemporaryDirectory() as root:
            plan, sha256, problems = self._pin(root)
            self.assertEqual(problems, ())
            self.assertEqual(plan.run_id, self.PLAN_RUN)
            self.assertEqual(plan.duration_ns, 120_000_000_000)
            self.assertEqual(plan.planned_end_monotonic_ns,
                             plan.started_monotonic_ns + plan.duration_ns)
            self.assertEqual(sha256, driver.digest(
                (Path(root) / "observer-run" / "sampling-plan.v1.json").read_bytes()))

    def test_every_frozen_input_the_plan_must_agree_with_is_checked(self):
        cases = (
            ({"run_id": "00000000-0000-4000-8000-000000000999"}, {},
             "sampling_plan_names_another_run"),
            ({}, {"intent": {"run_id": "00000000-0000-4000-8000-000000000999",
                             "duration_seconds": 120}},
             "owner_intent_names_another_run"),
            ({}, {"intent": {"run_id": "3f2b6c31-0000-4000-8000-00000000d1d1",
                             "duration_seconds": 119}},
             "owner_intent_duration_differs_from_the_approved_request"),
            ({"duration_ns": 119_000_000_000, "planned_end_monotonic_ns": 124_000_000_000}, {},
             "sampling_plan_duration_differs_from_the_approved_request"),
            ({}, {"clock": "sha256:" + "9" * 64},
             "sampling_plan_clock_domain_is_not_this_observer"),
        )
        for changes, keywords, problem in cases:
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as root:
                document = self._plan_document(**changes)
                _, _, problems = self._pin(root, document, **keywords)
                self.assertIn(problem, problems)

    def test_a_plan_the_product_decoder_refuses_never_becomes_a_window(self):
        with tempfile.TemporaryDirectory() as root:
            observer = Path(root) / "observer-run"
            observer.mkdir(parents=True)
            (Path(root) / "owner-evidence").mkdir()
            with self.assertRaises(AssertionError):
                driver.pin_sampling_plan(
                    observer_run=observer, request=self._request(),
                    evidence_directory=Path(root) / "owner-evidence",
                    local_clock_domain_sha256=self.CLOCK_SHA)
        for name, changes in (
            ("superseded_contract", {"contract_version": "mineru.synchronized-sampling-plan.v0"}),
            ("end_is_not_start_plus_duration", {"planned_end_monotonic_ns": 125_000_000_001}),
            ("unsupported_cadence", {"gpu_nominal_interval_ms": 300}),
            ("over_the_finite_ceiling", {"duration_ns": 8501 * 1_000_000_000,
                                         "planned_end_monotonic_ns": 5_000_000_000
                                         + 8501 * 1_000_000_000}),
        ):
            with self.subTest(case=name), tempfile.TemporaryDirectory() as root:
                with self.assertRaises(ValueError):
                    self._pin(root, self._plan_document(**changes))


class MeasuredDriverPreflightTests(unittest.TestCase):
    """The main path itself: what it refuses before it constructs its first child.

    Both cases run the real `main` over a complete set of frozen inputs composed
    from the product's own fixtures, with the one spawn primitive replaced. The
    negative proves no child is constructed; the positive proves that primitive
    does fire when the declaration matches the composition root's own rule, so
    the negative is not vacuous.
    """

    def _telemetry_lanes(self):
        """The two frozen lane configurations this driver's pre-flight has to read.

        The shared GPU fixture supplies the parts that are expensive and lane-independent
        (prepared manifest, pinned sources, directories); the host lane's serving backend
        is written here, because the current contract binds its capacity to the exact
        process profile and the driver's own input must satisfy that on its own.
        """
        shared, _, _ = _mapping_fixture(False)
        # The profile must be the projection of the frozen capacity; both come from the
        # shared session fixtures so this driver never invents an authority of its own.
        profile = _resident_profile()
        gpu = dict(shared["config"])
        gpu_uuid = gpu["backend"]["gpu_uuid"]
        node = profile.host_runtime_identity_sha256
        owner = {
            "boot_identity_sha256": gpu["owner_identity"]["boot_identity_sha256"],
            "host_assignment_identity_sha256": artifact_sha256(canonical_bytes(
                {"windows_node_identity_sha256": node, "gpu_uuid": gpu_uuid})),
            "process_profile_sha256": profile.sha256,
            "runtime_bundle_identity_sha256": profile.runtime_bundle_identity_sha256,
        }
        session = "234567891234423482341234567890ab"
        members = {name: {"cgroup": "/docker/" + str(index) * 64, "pid": index, "start_ticks": 100 + index}
                   for index, name in enumerate(("api", "inference", "proxy"), 1)}
        host = dict(gpu)
        host.update(
            lane="host_slow", cadence_ms=1000, port=30317, sampling_timeout_ms=900, session=session,
            run_directory="C:\\fixture\\run-" + session,
            backend={
                "api_namespace_pid": 1, "api_port": 30003,
                "capacity_config_sha256": _resident_capacity().sha256,
                "docker_path": "C:\\docker\\docker.exe", "docker_sha256": HASH,
                "image_id": HASH_B, "model_name": "fixture-model", "vllm_port": 30001,
                "linux_config": {"boot_id": "12345678-1234-4234-8234-1234567890ab", "members": members,
                                 "parent_device": 25, "parent_inode": 3067},
            })
        for config in (gpu, host):
            config["owner_identity"] = owner
            config["lease_ms"], config["lifetime_ms"] = 30000, 8_380_000
        host["backend"]["linux_config"].update(lease_ms=host["lease_ms"], lifetime_ms=host["lifetime_ms"])
        sources = {**shared["hashes"], "observe_mineru_resident_session.ps1": artifact_sha256(b"control")}
        return {"gpu": gpu, "host": host, "manifest": canonical_bytes(shared["manifest"]),
                "sources": sources, "profile": profile, "node": node,
                "capacity": _resident_capacity()}

    def _frozen(self, root, *, declared_transport=None, duration_seconds=SHORT_DURATION,
                windows_drift=None, runtime_drift=None, capacity_drift=None):
        """Every file `main` reads, written from the shared owner and campaign fixtures.

        The campaign intent, the private Windows target and the telemetry request are
        built to name one runtime; `windows_drift`/`runtime_drift` move exactly one of
        those values apart so the pre-flight has a single named disagreement to refuse.
        """
        frozen = self._telemetry_lanes()
        profile, gpu_backend = frozen["profile"], frozen["gpu"]["backend"]
        base = root / "frozen"
        (base / "package").mkdir(parents=True)
        lanes = {}
        for label in ("gpu", "host"):
            (base / f"{label}-config.json").write_bytes(canonical_bytes(frozen[label]))
            (base / f"{label}-manifest.json").write_bytes(frozen["manifest"])
            lanes[label] = {"config_path": str(base / f"{label}-config.json"),
                            "manifest_path": str(base / f"{label}-manifest.json"),
                            "remote_config_path": "C:\\frozen\\" + label + ".json"}
        (base / "sources.json").write_bytes(canonical_bytes(frozen["sources"]))
        (base / "profile.json").write_bytes(profile.exact_bytes)
        (base / "capacity.json").write_bytes(frozen["capacity"].exact_bytes)
        if declared_transport is None:
            declared_transport = SHORT["launcher_transport_timeout_seconds"]
        (base / "telemetry-request.json").write_bytes(canonical_bytes({
            "contract_version": driver.REQUEST_CONTRACT,
            "owner": {
                "evidence_directory": str(root / "owner"), "observer_artifact_root": str(root / "observer"),
                "run_id": RUN_ID, "gpu": lanes["gpu"], "host": lanes["host"],
                "source_hashes_path": str(base / "sources.json"),
                "process_profile_path": str(base / "profile.json"),
                "capacity_config_path": str(base / "capacity.json"),
                "windows_node_identity_sha256": frozen["node"],
                "ssh": {"address": "192.0.2.1", "port": 22, "username": "frozen",
                        "private_key_path": "/frozen/key", "known_hosts_path": "/frozen/known-hosts"},
                "ssh_executable": "/usr/bin/ssh", "ssh_executable_sha256": HASH_B,
                "duration_seconds": duration_seconds,
            },
            "budget": {"launcher_transport_timeout_seconds": declared_transport,
                       "sampling_start_allowance_seconds": SHORT["sampling_start_allowance_seconds"],
                       "campaign_launch_allowance_seconds": SHORT["campaign_launch_allowance_seconds"],
                       "cleanup_allowance_seconds": SHORT["cleanup_allowance_seconds"],
                       "first_frame_wait_seconds": 60, "telemetry_stop_grace_seconds": 30,
                       "telemetry_close_allowance_seconds": 30},
        }))
        fixture = m6.make_fixture("e2e_publication", planned_seconds=SHORT_INTENT.run.planned_seconds,
                                  close_grace_seconds=SHORT_INTENT.close_grace_seconds)
        runtime = {name: getattr(fixture.spec.runtime, name) for name in _RUNTIME_FIELDS}
        runtime.update({"runtime_bundle_identity_sha256": profile.runtime_bundle_identity_sha256,
                        "process_profile_sha256": profile.sha256, **(runtime_drift or {})})
        intent = campaign_intent_for_spec(fixture.spec, evaluation_plan(), runtime=runtime,
                                          close_grace_seconds=SHORT_INTENT.close_grace_seconds)
        intent_bytes = intent.canonical_bytes()
        (base / "intent.json").write_bytes(intent_bytes)
        binding = self._private_binding(root, windows={
            "node_sha256": frozen["node"],
            "gpu_uuid": gpu_backend["gpu_uuid"], "nvml_dll_sha256": gpu_backend["nvml_dll_sha256"],
            **(windows_drift or {})})
        (base / "package" / "release-manifest.json").write_bytes(canonical_bytes({"frozen": "release"}))
        (base / "binding.json").write_bytes(canonical_bytes({
            "capacity_config_sha256": (capacity_drift or frozen["capacity"].sha256)}))
        for name in ("manifest", "scope", "quality-plan", "evaluation-plan"):
            (base / (name + ".json")).write_bytes(canonical_bytes({"frozen": name}))
        output = root / "measured"
        argv = ["--execute", "--intent", str(base / "intent.json"),
                "--intent-sha256", driver.digest(intent_bytes),
                "--package", str(base / "package"), "--output", str(output),
                "--private-binding", str(binding)]
        for key in ("binding", "manifest", "scope", "quality-plan", "evaluation-plan"):
            argv += ["--" + key, str(base / (key + ".json"))]
        argv += ["--telemetry-request", str(base / "telemetry-request.json")]
        return argv, output

    def _private_binding(self, root, *, windows):
        """A campaign private binding the product's own loader accepts, on real local files."""
        private = root / "private"
        (private / "env").mkdir(parents=True)
        (private / "runtime").mkdir()
        (root / "service" / "src" / "disclosure_anchor").mkdir(parents=True)
        for name in ("worker.env", "cninfo.env"):
            secret = private / "env" / name
            secret.write_bytes(b"FROZEN=1\n")
            secret.chmod(0o600)
        key = private / "ssh-key"
        key.write_bytes(b"frozen-private-key\n")
        key.chmod(0o600)
        (private / "known_hosts").write_text("192.0.2.1 ssh-ed25519 AAAAFROZEN\n", encoding="utf-8")
        transports = {}
        for name in ("ssh", "sftp"):
            payload = ("frozen-" + name).encode()
            (private / name).write_bytes(payload)
            transports[name] = (str(private / name), "sha256:" + hashlib.sha256(payload).hexdigest())
        document = {
            "contract_version": CAMPAIGN_PRIVATE_BINDING_CONTRACT,
            "env_dir": str(private / "env"), "service_root": str(root / "service"),
            "python_executable": sys.executable, "runtime_root": str(private / "runtime"),
            "ssh": {"address": "192.0.2.1", "port": 22, "username": "frozen",
                    "private_key_path": str(key), "known_hosts_path": str(private / "known_hosts"),
                    "executable_path": transports["ssh"][0], "executable_sha256": transports["ssh"][1],
                    "sftp_executable_path": transports["sftp"][0],
                    "sftp_executable_sha256": transports["sftp"][1]},
            "windows": {"hostname": "frozen-node", "workspace_root": "C:\\m6",
                        "owner_executable_path": "C:\\m6\\owner.exe", "owner_executable_sha256": HASH,
                        "owner_source_sha256": HASH_B, "launcher_path": "C:\\m6\\launch.ps1",
                        "launcher_sha256": HASH_C, "port": 40001, "maximum_lease_ticks": 300_000_000,
                        "propagation_reserve_ticks": 30_000_000, "max_artifacts": 256,
                        "max_artifact_bytes": 16_777_216, **windows},
            "mac_exclusive_lock_path": str(private / "m6.lock"),
        }
        path = private / "private-binding.json"
        path.write_bytes(canonical_bytes(document))
        path.chmod(0o600)
        return path

    def _main(self, argv):
        """Run the real entry with the one spawn primitive replaced; nothing else is faked."""
        spawns = []

        def popen(command, **keywords):
            spawns.append(list(command))
            raise SpawnBlocked("the spawn primitive was reached")

        handlers = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP))
        try:
            with mock.patch.object(driver.subprocess, "Popen", popen), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = driver.main(argv)
        finally:
            signal.signal(signal.SIGTERM, handlers[0])
            signal.signal(signal.SIGHUP, handlers[1])
        return code, spawns

    def test_a_misdeclared_transport_is_refused_before_any_child_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Understated by 60 s: the window still looks affordable and the transport still
            # outlasts the business close, so only the composition root's rule catches it.
            declared = SHORT["launcher_transport_timeout_seconds"] - 60
            argv, output = self._frozen(root, declared_transport=declared)
            code, spawns = self._main(argv)
            self.assertEqual(code, 1)
            self.assertEqual(spawns, [])
            # No child, no command for one, and nothing of the campaign's own directory.
            for name in ("telemetry.stdout", "telemetry.stderr", "telemetry-command.json",
                         "telemetry-start.json", "campaign"):
                self.assertFalse((output / name).exists(), name)
            budget = json.loads((output / "budget.json").read_bytes())
            self.assertFalse(budget["satisfied"])
            self.assertEqual([problem.split(":")[0] for problem in budget["problems"]],
                             ["declared_launcher_transport_differs_from_the_composition_root_rule"])
            evidence = json.loads((output / "independent-evidence.json").read_bytes())
            self.assertEqual(evidence["status"], "incomplete")
            self.assertIn("refused before any child", evidence["failure"])
            self.assertNotIn("telemetry_pid", evidence)

    def test_the_composition_root_s_own_budget_admits_the_first_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv, output = self._frozen(
                root, declared_transport=SHORT["launcher_transport_timeout_seconds"])
            code, spawns = self._main(argv)
            self.assertEqual(code, 1)  # the spawn primitive refused; the pre-flight did not
            self.assertEqual(len(spawns), 1)
            self.assertEqual(spawns[0][1:4],
                             ["-m", "tests.integration.m6_measured_campaign_independent", "telemetry-child"])
            identity = json.loads((output / "identity.json").read_bytes())
            self.assertEqual(identity["problems"], [])
            self.assertTrue(identity["gpu_target_agrees"])
            # The release's frozen capacity is the one authority the host lane will be judged by.
            self.assertEqual(identity["capacity_config_sha256"], _resident_capacity().sha256)
            budget = json.loads((output / "budget.json").read_bytes())
            self.assertTrue(budget["satisfied"])
            self.assertEqual(budget["problems"], [])
            self.assertEqual(budget["coverage_policy"], "full_campaign")
            self.assertEqual(budget["declared_terms"]["launcher_transport_timeout_seconds"],
                             budget["composition_root_terms"]["timeout_seconds"])
            evidence = json.loads((output / "independent-evidence.json").read_bytes())
            self.assertTrue(evidence["failure"].startswith("SpawnBlocked:"))
            self.assertEqual(evidence["status"], "incomplete")
            # Whatever ends the run from here on, the children get the frozen graces, not a
            # smaller literal: the entry's own cancel plus its fetches, and the frozen stop grace.
            self.assertEqual(evidence["stop_grace"], {
                "campaign": assembly._CANCEL_TIMEOUT_SECONDS + budget["retrieval_seconds"],
                "telemetry": 30})
            # The frozen inputs were pinned by path and by role before the first child.
            pins = json.loads((output / "input-hashes.json").read_bytes())
            self.assertEqual(sorted(name for name in pins if name.startswith("telemetry:")),
                             ["telemetry:capacity-config", "telemetry:gpu-config",
                              "telemetry:gpu-manifest", "telemetry:host-config",
                              "telemetry:host-manifest", "telemetry:process-profile",
                              "telemetry:source-hashes"])

    def test_inputs_naming_different_runtimes_are_refused_before_any_child(self):
        # One frozen value moved apart at a time: the campaign intent's runtime binding, or the
        # private Windows target. Each is a single named disagreement, and none of them reaches
        # a child - the campaign's own summary would have found it only after the window was gone.
        cases = (
            ({"runtime_drift": {"runtime_bundle_identity_sha256": HASH_C}},
             "intent_runtime_bundle_differs_from_the_telemetry_process_profile"),
            ({"runtime_drift": {"process_profile_sha256": HASH_C}},
             "intent_process_profile_differs_from_the_telemetry_process_profile"),
            ({"windows_drift": {"node_sha256": HASH_C}},
             "campaign_windows_node_differs_from_the_telemetry_node"),
            ({"windows_drift": {"gpu_uuid": "GPU-00000000-0000-4000-8000-000000000000"}},
             "campaign_gpu_target_differs_from_the_telemetry_gpu_lane"),
            ({"windows_drift": {"nvml_dll_sha256": HASH_C}},
             "campaign_nvml_library_differs_from_the_telemetry_gpu_lane"),
            ({"capacity_drift": "sha256:" + "c" * 64},
             "release_binding_capacity_differs_from_the_telemetry_capacity"),
        )
        for drift, problem in cases:
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                argv, output = self._frozen(root, **drift)
                code, spawns = self._main(argv)
                self.assertEqual(code, 1)
                self.assertEqual(spawns, [])
                self.assertFalse((output / "telemetry-command.json").exists())
                self.assertFalse((output / "budget.json").exists())
                identity = json.loads((output / "identity.json").read_bytes())
                self.assertEqual(identity["problems"], [problem])
                evidence = json.loads((output / "independent-evidence.json").read_bytes())
                self.assertIn("name different runtimes", evidence["failure"])
                self.assertNotIn("telemetry_pid", evidence)
                # Refused before the budget existed, so the campaign grace is still the
                # composition root's own cancel bound rather than an ad-hoc literal.
                self.assertEqual(evidence["stop_grace"],
                                 {"campaign": assembly._CANCEL_TIMEOUT_SECONDS, "telemetry": 30})

    def test_a_window_shorter_than_the_frozen_plan_is_refused_before_any_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # One second under the frozen requirement for this shape.
            argv, output = self._frozen(root, duration_seconds=1973,
                                        declared_transport=SHORT["launcher_transport_timeout_seconds"])
            code, spawns = self._main(argv)
            self.assertEqual(code, 1)
            self.assertEqual(spawns, [])
            budget = json.loads((output / "budget.json").read_bytes())
            self.assertEqual(budget["problems"], ["sampling_window_shorter_than_the_required_span"])
            self.assertFalse((output / "telemetry-command.json").exists())


class MeasuredDriverRequestTests(unittest.TestCase):
    def test_owner_request_mapping_is_direct_and_closed(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            for name, payload in (("gpu.json", b'{"lane":"gpu_fast"}'), ("host.json", b'{"lane":"host_slow"}'),
                                  ("manifest.json", b"{}"), ("profile.json", b"{}"),
                                  ("capacity.json", CAPACITY_BYTES),
                                  ("sources.json", b'{"start_mineru_resident_telemetry.ps1":"sha256:ab"}')):
                (base / name).write_bytes(payload)
            document = {
                "contract_version": driver.REQUEST_CONTRACT,
                "owner": {
                    "evidence_directory": str(base / "evidence"),
                    "observer_artifact_root": str(base / "observer"), "run_id": RUN_ID,
                    "gpu": {"config_path": str(base / "gpu.json"), "manifest_path": str(base / "manifest.json"),
                            "remote_config_path": "C:\\frozen\\gpu.json"},
                    "host": {"config_path": str(base / "host.json"), "manifest_path": str(base / "manifest.json"),
                             "remote_config_path": "C:\\frozen\\host.json"},
                    "source_hashes_path": str(base / "sources.json"),
                    "process_profile_path": str(base / "profile.json"),
                    "capacity_config_path": str(base / "capacity.json"),
                    "windows_node_identity_sha256": HASH,
                    "ssh": {"address": "192.0.2.7", "port": 22, "username": "frozen",
                            "private_key_path": "/frozen/key", "known_hosts_path": "/frozen/known-hosts"},
                    "ssh_executable": "/usr/bin/ssh", "ssh_executable_sha256": HASH_B,
                    "duration_seconds": 8250,
                },
                "budget": {"launcher_transport_timeout_seconds": 7470,
                           "sampling_start_allowance_seconds": 30, "campaign_launch_allowance_seconds": 30,
                           "cleanup_allowance_seconds": 0, "first_frame_wait_seconds": 900,
                           "telemetry_stop_grace_seconds": 180, "telemetry_close_allowance_seconds": 300},
            }
            request = driver.build_owner_request(document)
            self.assertEqual(request.run_id, RUN_ID)
            self.assertEqual(request.duration_seconds, 8250)
            self.assertEqual(request.gpu.config_bytes, b'{"lane":"gpu_fast"}')
            self.assertEqual(request.gpu.remote_config_path, "C:\\frozen\\gpu.json")
            self.assertEqual(request.host.manifest_bytes, b"{}")
            self.assertEqual(request.ssh.address, "192.0.2.7")
            self.assertEqual(request.observer_artifact_root, base / "observer")
            self.assertEqual(driver.request_budget(document)["first_frame_wait_seconds"], 900)
            pins = driver.pin_inputs([base / "sources.json"], request)
            self.assertEqual(sorted(pins), [str(base / "sources.json"), "telemetry:capacity-config",
                                            "telemetry:gpu-config", "telemetry:gpu-manifest",
                                            "telemetry:host-config", "telemetry:host-manifest",
                                            "telemetry:process-profile", "telemetry:source-hashes"])
            (base / "host.json").write_bytes(b'{"lane":"host_slow","cadence_ms":1000}')
            self.assertEqual(driver.host_cadence_ms(driver.build_owner_request(document)), 1000)
            # An indirect input that changed is visible through the same pins.
            self.assertNotEqual(driver.pin_inputs([base / "sources.json"],
                                                  driver.build_owner_request(document)), pins)
            with self.assertRaises(AssertionError):
                driver.host_cadence_ms(request)

            def copy_of(document):
                return {"contract_version": document["contract_version"],
                        "owner": {**document["owner"], "ssh": dict(document["owner"]["ssh"]),
                                  "gpu": dict(document["owner"]["gpu"])},
                        "budget": dict(document["budget"])}

            for mutate in (lambda d: d["owner"].pop("run_id"),
                           lambda d: d["owner"].update(extra=1),
                           lambda d: d["owner"]["ssh"].pop("port"),
                           lambda d: d["owner"]["gpu"].update(extra=1),
                           lambda d: d.update(contract_version="other"),
                           lambda d: d["owner"].update(evidence_directory="relative/path"),
                           lambda d: d["owner"].update(process_profile_path=str(base / "absent.json")),
                           lambda d: d["owner"].pop("capacity_config_path")):
                broken = copy_of(document)
                mutate(broken)
                with self.assertRaises((AssertionError, ValueError, KeyError)):
                    driver.build_owner_request(broken)
            for mutate in (lambda d: d["budget"].update(cleanup_allowance_seconds=-1),
                           lambda d: d["budget"].pop("first_frame_wait_seconds"),
                           lambda d: d["budget"].update(telemetry_stop_grace_seconds=True),
                           lambda d: d["budget"].update(launcher_transport_timeout_seconds=0),
                           lambda d: d["budget"].update(launcher_transport_timeout_seconds=float("inf")),
                           lambda d: d["budget"].update(sampling_start_allowance_seconds=float("nan")),
                           lambda d: d["budget"].update(cleanup_allowance_seconds=float("inf"))):
                broken = copy_of(document)
                mutate(broken)
                with self.assertRaises(AssertionError):
                    driver.request_budget(broken)


if __name__ == "__main__":
    unittest.main()
