"""Opt-in finite measured campaign: one resident telemetry owner beside one real campaign CLI.

This is a test-layer orchestration, never a production dependency. It owns two
finite children - the existing ``run_resident_telemetry_session`` owner and the
existing ``disclosure_anchor.cli.m6_campaign`` entry - and proves, from the
original evidence those two produce, that the resource window actually covers
the campaign it is asked to score. It never creates a task ledger, a spec, a
frame, a boot identity or a hash of its own, never writes the business data
plane, and never turns a killed local transport into remote absence.

The window budget is frozen before any child starts: every finite term comes
from the composition root's own timeouts and its own pure launcher-budget rule,
the frozen intent, or the explicitly frozen driver allowances in the telemetry
request. When the budget does not close, this stops before the first child
instead of sampling a window it cannot prove. Run explicitly with --execute and
the same frozen inputs the campaign entry takes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import inspect
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

from disclosure_anchor.adapters.runtime import m6_campaign_assembly as assembly
from disclosure_anchor.adapters.runtime import resident_telemetry_owner as owner_module
from disclosure_anchor.adapters.runtime.mac_observer_identity import MacObserverIdentityReader
from disclosure_anchor.adapters.runtime.m6_campaign_private_binding import load_campaign_private_binding
from disclosure_anchor.adapters.runtime.resident_ssh_http import ResidentSSHConfig
from disclosure_anchor.adapters.runtime.resident_telemetry_owner import (
    ResidentLaneLaunch, ResidentTelemetryOwnerRequest, run_resident_telemetry_session,
)
from disclosure_anchor.adapters.runtime.synchronized_telemetry_observer import (
    FRAME_V3_FILENAME, verify_synchronized_telemetry_observer,
)
from disclosure_anchor.application.contracts.m6_campaign_intent import M6CampaignIntent, decode_campaign_intent
from disclosure_anchor.application.contracts.m6_delivery_report import (
    M6_DELIVERY_REPORT_MAX_BYTES, M6DeliveryReport,
)
from disclosure_anchor.application.contracts.mineru_capacity_config import (
    decode_mineru_capacity_config,
)
from disclosure_anchor.application.contracts.mineru_process_profile import decode_mineru_process_profile
from disclosure_anchor.application.contracts.resident_combined_cpu import check_combined_resident_cpu_v4
from disclosure_anchor.application.contracts.resident_session_evidence import (
    artifact_sha256, canonical_bytes, check_external_windows_observation, check_mac_observer_identity,
    check_resident_closure, check_resident_observer_mapping_v4, check_resident_ready,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    SynchronizedSamplingPlanV1, SynchronizedTelemetryFrameV3, parse_canonical_jsonl_artifact,
)
from disclosure_anchor.application.services import m6_launch_budget as assembly_budget
from disclosure_anchor.application.services.m6_launch_budget import (
    SamplingCoverageTerms, finish_wait_seconds, launch_transport_budget,
)
from disclosure_anchor.application.services.resident_measurement_policy import (
    CoverageBudget, require_start_headroom,
)
from tests.integration.m6_fresh_workspace_independent import digest, require, save


DRIVER_CONTRACT = "m6.measured-campaign-driver.v2"
REQUEST_CONTRACT = "m6.measured-telemetry-request.v2"
BUDGET_CONTRACT = "m6.measured-campaign-budget.v2"
# What this driver records about its own window: the whole campaign, entry to the
# post-owner read-back. The product rule has one coverage definition and takes no
# policy argument, so this is a label on the driver's evidence, never a choice
# offered to it; a narrower window would not cover the campaign it scores.
COVERAGE_POLICY = "full_campaign"
NS = 1_000_000_000
# Reserved at each end of the sampling window so a source capture interval can sit wholly
# outside the campaign envelope; one period was not enough to prove an edge.
EDGE_RESERVE_SECONDS = 2.0
# The replay protocol this driver scores; the superseded ones stay readable but never
# certify a new run.
RECEIPT_VERSION = 4

# The one composition-root wait that is a literal at its call site rather than a
# named constant, pinned here with the exact call it belongs to. Every other
# finite term is read from the module itself and fails closed if it is renamed.
OWNER_IDENTITY_FETCH_SECONDS = 60.0  # m6_campaign_assembly._fetch_owner_identity

MAX_REQUEST_BYTES = 65_536
MAX_LANE_BYTES = 65_536
MAX_PROFILE_BYTES = 65_536
MAX_SUMMARY_BYTES = 8 * 1024 * 1024
MAX_OWNER_RESULT_BYTES = 1_048_576
MAX_FRAME_PROBE_BYTES = 1_048_576
MAX_CHILD_TEXT_BYTES = 16_384

_LANES = ("gpu_fast", "host_slow")
_UNKNOWN_REMOTE = ("unknown: the local transport was stopped; reconcile the original remote records "
                   "before any new attempt")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_bounded(path, *, maximum):
    """One bounded read of a regular file; the name, never the private path, is reported."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise AssertionError("expected an existing regular file: " + path.name)
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    require(len(raw) <= maximum, path.name + " exceeds its frozen byte bound")
    return raw


def load_bounded(path, *, maximum):
    value = strict_json_loads(read_bounded(path, maximum=maximum))
    require(type(value) is dict, Path(path).name + " is not a JSON object")
    return value


def absolute(value, label):
    require(type(value) is str and value.startswith("/"), label + " must be an absolute path")
    path = Path(value)
    require(path == Path(os.path.normpath(value)), label + " must be a normalized path")
    return path


# --- frozen window budget -------------------------------------------------------------


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _finish_poll_allowance_seconds():
    """How long the composition root polls its finished launcher, from the product itself.

    The named constant is preferred; while it is being promoted, the same authority is the
    declared default of the composition rule's own `finish_wait_seconds`. This driver never
    writes the number down: a second literal would stop tracking the composition rule.
    """
    named = getattr(assembly_budget, "FINISH_POLL_ALLOWANCE_SECONDS", None)
    if _finite(named):
        return float(named)
    declared = inspect.signature(finish_wait_seconds).parameters["allowance_seconds"].default
    require(_finite(declared), "the composition rule declares no finish-poll allowance")
    return float(declared)


def _exact_ns(seconds, label):
    """Seconds to exact integer nanoseconds; a value that cannot be represented is refused."""
    require(_finite(seconds) and seconds >= 0, "budget term " + label + " must be finite and non-negative")
    scaled = Decimal(str(seconds)) * NS
    require(scaled == scaled.to_integral_value(), "budget term " + label + " is not a whole nanosecond")
    return int(scaled)


def _constant(name):
    """Read one composition-root timeout; an unreadable term can never be claimed as covered."""
    value = getattr(assembly, name, None)
    if not _finite(value):
        raise AssertionError("composition-root timeout " + name + " is unreadable; the budget cannot be derived")
    return float(value)


def _int_constant(name):
    """Read one composition-root term the product's own rule requires as an exact integer."""
    value = getattr(assembly, name, None)
    if type(value) is not int:
        raise AssertionError("composition-root term " + name + " is unreadable; the budget cannot be derived")
    return value


def composition_root_transport_budget(intent):
    """The composition root's own pure launcher rule, evaluated on this frozen intent.

    Called in the local preflight, before either child exists. Rule and terms are
    the product's (``application/services/m6_launch_budget`` and the assembly's own
    constants); the driver contributes no formula and invents no future term, so a
    product change is read here instead of being mirrored.
    """
    return launch_transport_budget(
        planned_seconds=intent.run.planned_seconds,
        close_grace_seconds=intent.close_grace_seconds,
        ready_wait_seconds=intent.ready_wait_seconds,
        ssh_overhead_seconds=_constant("_SSH_OVERHEAD_SECONDS"),
        exit_wait_extra_seconds=_int_constant("_LAUNCHER_EXIT_WAIT_EXTRA_SECONDS"),
        launcher_drain_seconds=_constant("_LAUNCHER_DRAIN_SECONDS"),
    )


def freeze_budget(
    *, intent, host_cadence_ms, duration_seconds, launcher_transport_timeout_seconds,
    sampling_start_allowance_seconds, campaign_launch_allowance_seconds, cleanup_allowance_seconds,
):
    """Derive the finite window this run needs on one absolute launcher clock.

    Everything the entry spends between spawning the launcher and the launcher's
    own exit lives inside that single command deadline: the READY wait, the
    business window and the finish wait are phases of it, never terms to add up.
    So the window must hold the driver's own allowances, Prepare, stage, the whole
    launcher transport lifetime, the bounded read-back and fetches, and two host
    periods for the edge sample after the close.

    ``launcher_transport_timeout_seconds`` is declared in the frozen execution
    input and adjudicated here, in the local pre-flight, against the composition
    root's own pure rule on this same intent: a mismatch blocks before either
    child is created. The entry's ``launcher-command.json`` is polled afterwards
    as an ongoing consistency check, and that poll is never the authorization,
    because nothing orders it before the entry's first admission.

    Coverage is the whole campaign: ``SamplingCoverageTerms`` defines one required
    span, and the driver adds only its own frozen allowances on top of it.
    """
    prepare = _constant("_PREPARE_TIMEOUT_SECONDS")
    stage = _constant("_STAGE_TIMEOUT_SECONDS")
    ssh_overhead = _constant("_SSH_OVERHEAD_SECONDS")
    fetch = _constant("_FETCH_TIMEOUT_SECONDS")
    require(type(host_cadence_ms) is int and host_cadence_ms > 0,
            "budget input host_cadence_ms must be a positive integer")
    for label, value in (("duration_seconds", duration_seconds),
                         ("launcher_transport_timeout_seconds", launcher_transport_timeout_seconds)):
        require(_finite(value) and value > 0, "budget input " + label + " must be a positive finite number")
    for label, value in (("sampling_start_allowance_seconds", sampling_start_allowance_seconds),
                         ("campaign_launch_allowance_seconds", campaign_launch_allowance_seconds),
                         ("cleanup_allowance_seconds", cleanup_allowance_seconds)):
        require(_finite(value) and value >= 0,
                "budget allowance " + label + " must be a finite number of at least zero")
    try:
        product = composition_root_transport_budget(intent)
    except (AttributeError, TypeError, ValueError) as exc:
        raise AssertionError("the composition root's own rule refuses this intent: " + str(exc)) from exc
    planned, grace, ready_wait = intent.run.planned_seconds, intent.close_grace_seconds, intent.ready_wait_seconds
    business = float(planned + grace)
    ready = float(ready_wait) + ssh_overhead
    transport = float(launcher_transport_timeout_seconds)
    terms = SamplingCoverageTerms(
        entry_to_spawn_seconds=prepare + stage,
        owner_transport_seconds=transport,
        post_transport_seconds=OWNER_IDENTITY_FETCH_SECONDS + fetch + fetch,
        host_period_seconds=host_cadence_ms / 1000.0,
    )
    # The finite vector is the product's own: the existing phase bounds go in, the required
    # window comes out. The driver adds no arithmetic of its own, so a term Fable moves moves
    # here too. The edge reserve is a fixed two seconds at each end, not one lane period: a
    # source capture interval has to sit wholly outside the campaign envelope to prove an edge.
    vector = CoverageBudget(
        entry_to_spawn_ns=_exact_ns(terms.entry_to_spawn_seconds, "entry_to_spawn"),
        launcher_transport_ns=_exact_ns(transport, "launcher_transport"),
        finish_poll_allowance_ns=_exact_ns(_finish_poll_allowance_seconds(), "finish_poll"),
        retrieval_ns=_exact_ns(terms.post_transport_seconds, "retrieval"),
        driver_start_ns=_exact_ns(sampling_start_allowance_seconds, "sampling_start_allowance"),
        driver_launch_ns=_exact_ns(campaign_launch_allowance_seconds, "campaign_launch_allowance"),
        driver_cleanup_ns=_exact_ns(cleanup_allowance_seconds, "cleanup_allowance"),
        edge_reserve_each_ns=_exact_ns(EDGE_RESERVE_SECONDS, "edge_reserve"),
    )
    allowance = (float(sampling_start_allowance_seconds) + float(campaign_launch_allowance_seconds)
                 + float(cleanup_allowance_seconds))
    duration_ns = _exact_ns(duration_seconds, "duration")
    headroom = transport - ready - business
    retrieval = terms.post_transport_seconds + float(cleanup_allowance_seconds)
    span = (float(campaign_launch_allowance_seconds) + terms.entry_to_spawn_seconds + transport
            + _finish_poll_allowance_seconds() + retrieval)
    host_edge = 2.0 * EDGE_RESERVE_SECONDS
    required_total = vector.required_ns / NS
    # The product rule decides whether the window holds; the arithmetic margin is recorded
    # either way, because a negative one is the number root and Pro need to see.
    margin_ns = duration_ns - vector.required_ns
    problems = []
    try:
        vector.require_fits(duration_ns)
    except ValueError:
        problems.append("sampling_window_shorter_than_the_required_span")
    if transport != float(product.timeout_seconds):
        problems.append("declared_launcher_transport_differs_from_the_composition_root_rule:"
                        f"{transport}!={float(product.timeout_seconds)}")
    if headroom < 0:
        problems.append("launcher_transport_cannot_cover_a_delayed_t0_and_the_business_close")
    return {
        "contract_version": BUDGET_CONTRACT,
        "coverage_policy": COVERAGE_POLICY,
        "code_terms": {
            "prepare_timeout_seconds": prepare, "stage_timeout_seconds": stage,
            "ssh_overhead_seconds": ssh_overhead, "fetch_timeout_seconds": fetch,
            "owner_identity_fetch_seconds": OWNER_IDENTITY_FETCH_SECONDS,
            "finish_poll_allowance_seconds": _finish_poll_allowance_seconds(),
        },
        "intent_terms": {
            "planned_seconds": planned, "close_grace_seconds": grace,
            "ready_wait_seconds": ready_wait, "business_max_close_seconds": business,
            "ready_deadline_seconds": ready, "host_cadence_ms": host_cadence_ms,
        },
        "composition_root_terms": product.as_dict(),
        "declared_terms": {"launcher_transport_timeout_seconds": transport},
        "driver_allowances": {
            "sampling_start_allowance_seconds": float(sampling_start_allowance_seconds),
            "campaign_launch_allowance_seconds": float(campaign_launch_allowance_seconds),
            "cleanup_allowance_seconds": float(cleanup_allowance_seconds),
            "total_seconds": allowance,
            "allowed_seconds": float(duration_seconds) - required_total,
        },
        "finite_vector_ns": {name: getattr(vector, name) for name in vector.__dataclass_fields__},
        "business_seconds": business, "launcher_transport_headroom_seconds": headroom,
        "retrieval_seconds": retrieval, "host_edge_seconds": host_edge,
        "campaign_span_requirement_seconds": span,
        "required_after_first_frame_seconds": span + host_edge,
        "required_total_seconds": required_total, "duration_seconds": float(duration_seconds),
        "margin_seconds": margin_ns / NS,
        "satisfied": not problems, "problems": sorted(problems),
    }


def identity_problems(*, intent, binding, request, profile, release_binding):
    """Named disagreements between the three frozen inputs that must describe one runtime.

    The campaign intent, the release binding, the telemetry request and the private
    Windows target are frozen separately and handed to different children. Each is already validated
    by its own owner - this compares them, and only that. Nothing here re-derives
    a hash, re-checks what `_validate_request` or the campaign entry already
    prove, or invents a second definition of an identity: every term is a value
    one input already carries, compared with the same value in another.

    The campaign's own `summary` would catch a mismatch, but only after the whole
    window has been spent on a run it can never score, so this runs before either
    child exists.
    """
    runtime = intent.runtime
    problems = []
    if release_binding.get("capacity_config_sha256") != decode_mineru_capacity_config(
            request.capacity_config_bytes).sha256:
        problems.append("release_binding_capacity_differs_from_the_telemetry_capacity")
    if runtime.runtime_bundle_identity_sha256 != profile.runtime_bundle_identity_sha256:
        problems.append("intent_runtime_bundle_differs_from_the_telemetry_process_profile")
    if runtime.process_profile_sha256 != profile.sha256:
        problems.append("intent_process_profile_differs_from_the_telemetry_process_profile")
    if binding.windows.node_sha256 != request.windows_node_identity_sha256:
        problems.append("campaign_windows_node_differs_from_the_telemetry_node")
    backend = strict_json_loads(request.gpu.config_bytes).get("backend")
    if type(backend) is not dict:
        problems.append("frozen_gpu_lane_has_no_backend")
    else:
        if backend.get("gpu_uuid") != binding.windows.gpu_uuid:
            problems.append("campaign_gpu_target_differs_from_the_telemetry_gpu_lane")
        if backend.get("nvml_dll_sha256") != binding.windows.nvml_dll_sha256:
            problems.append("campaign_nvml_library_differs_from_the_telemetry_gpu_lane")
    return tuple(problems)


def pin_sampling_plan(*, observer_run, request, evidence_directory, local_clock_domain_sha256,
                      maximum=MAX_LANE_BYTES):
    """Read back the observer's frozen plan through the product's own closed decoder.

    The plan is the owner's retained `duration_seconds` projected once, before the first
    collector call. Three things are checked here and nowhere else in this driver: the
    plan document is the product's closed model, its intent hash is the hash of the owner's
    own retained intent bytes whose *contents* name this run and duration, and its clock
    domain is the independently read local observer identity - which is what makes
    `plan.end - monotonic_ns()` a subtraction inside one domain rather than across two.
    """
    model = SynchronizedSamplingPlanV1
    raw = read_bounded(Path(observer_run) / "sampling-plan.v1.json", maximum=maximum)
    document = strict_json_loads(raw)
    require(type(document) is dict, "sampling plan is not a JSON object")
    plan = model.model_validate(document)
    intent_raw = read_bounded(Path(evidence_directory) / "owner-intent.json",
                              maximum=MAX_OWNER_RESULT_BYTES)
    intent = strict_json_loads(intent_raw)
    require(type(intent) is dict, "retained owner intent is not a JSON object")
    problems = []
    if plan.run_id != request.run_id:
        problems.append("sampling_plan_names_another_run")
    if intent.get("run_id") != request.run_id:
        problems.append("owner_intent_names_another_run")
    if intent.get("duration_seconds") != request.duration_seconds:
        problems.append("owner_intent_duration_differs_from_the_approved_request")
    if plan.owner_intent_sha256 != digest(intent_raw):
        problems.append("sampling_plan_is_not_the_retained_owner_intent")
    if plan.duration_ns != _exact_ns(request.duration_seconds, "request duration"):
        problems.append("sampling_plan_duration_differs_from_the_approved_request")
    if plan.host_nominal_interval_ms != host_cadence_ms(request):
        problems.append("sampling_plan_host_cadence_differs_from_the_frozen_lane")
    if plan.observer_clock_domain_identity_sha256 != local_clock_domain_sha256:
        problems.append("sampling_plan_clock_domain_is_not_this_observer")
    return plan, digest(raw), tuple(problems)


def launcher_transport_state(campaign_dir, *, declared):
    """`(recorded, problem)` from the entry's own launch record: an ongoing consistency check.

    The entry writes the record just before it spawns the launcher, but this
    reader is polled on its own cadence and nothing orders it before READY, bind,
    open or the first admission. So a divergence between the frozen declaration
    and the entry's actual command deadline is detected here, possibly late and
    with business consequences already begun - it is never the authorization to
    admit. That authorization is the pre-flight adjudication in `freeze_budget`,
    which runs before either child exists. Absence stays absence: a record that is
    not there yet is reported as not recorded, never as agreement.
    """
    path = Path(campaign_dir) / "launcher-command.json"
    if path.is_symlink() or not path.is_file():
        return False, None
    document = load_bounded(path, maximum=MAX_SUMMARY_BYTES)
    actual = document.get("timeout_seconds")
    if type(actual) not in (int, float) or isinstance(actual, bool):
        return True, "launcher_transport_timeout_unreadable"
    if float(actual) != float(declared):
        return True, ("launcher_transport_timeout_differs_from_the_frozen_budget:"
                      f"{actual}!={float(declared)}")
    return True, None


# --- original frames ------------------------------------------------------------------


def frame_problems(frame, *, run_id, runtime_bundle_sha256, process_profile_sha256, lane_identity):
    """Why one replayed frame is not usable evidence for this frozen session."""
    problems = []
    if frame.run_id != run_id:
        problems.append("frame_run_id")
    if frame.runtime_bundle_identity_sha256 != runtime_bundle_sha256:
        problems.append("frame_runtime_bundle")
    if frame.process_profile_sha256 != process_profile_sha256:
        problems.append("frame_process_profile")
    if frame.lane not in lane_identity:
        problems.append("frame_lane")
    else:
        identity = lane_identity[frame.lane]
        # A v3 frame always carries its request witness; the model makes it mandatory, so the
        # only question left here is whether it names this host's boot and assignment.
        provenance = frame.resident_exporter_provenance
        if provenance.boot_identity_sha256 != identity["boot_identity_sha256"]:
            problems.append("frame_boot_identity")
        if provenance.host_assignment_identity_sha256 != identity["host_assignment_identity_sha256"]:
            problems.append("frame_host_assignment")
    if frame.quality.status not in ("first", "on_time") or frame.quality.missed_deadlines:
        problems.append("frame_sample_quality")
    if frame.lane == "gpu_fast":
        if frame.gpu.status != "supported" or frame.gpu.values is None:
            problems.append("frame_gpu_unsupported")
    elif frame.lane == "host_slow":
        # The same both-observations rule the resource aggregates use for a usable host sample.
        if frame.host_cgroup.status != "supported" or frame.host_cgroup.values is None:
            problems.append("frame_host_cgroup_unsupported")
        if frame.queue_vllm.status != "supported" or frame.queue_vllm.values is None:
            problems.append("frame_queue_unsupported")
    return tuple(sorted(set(problems)))


def trusted_frames(frames, *, run_id, runtime_bundle_sha256, process_profile_sha256, lane_identity):
    return tuple(frame for frame in frames if not frame_problems(
        frame, run_id=run_id, runtime_bundle_sha256=runtime_bundle_sha256,
        process_profile_sha256=process_profile_sha256, lane_identity=lane_identity,
    ))


def probe_frames(path, *, maximum_bytes=MAX_FRAME_PROBE_BYTES):
    """Bounded head read of the live frame stream: only whole canonical records.

    The observer appends one canonical record per write, so a torn tail is
    dropped rather than repaired, and nothing outside the head bound is read.
    """
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return ()
    with path.open("rb") as stream:
        raw = stream.read(maximum_bytes)
    end = raw.rfind(b"\n")
    if end < 0:
        return ()
    records = parse_canonical_jsonl_artifact(
        raw[: end + 1], label="observer frame probe", maximum_bytes=maximum_bytes,
    )
    # The stream this driver probes is the R22 observer's own v3 frame file; a superseded
    # record on this path is refused here rather than read as evidence for a v4 run.
    return tuple(SynchronizedTelemetryFrameV3.model_validate(record) for record in records)


def frame_identity(frame):
    """A bounded, non-secret description of one original frame."""
    provenance = frame.resident_exporter_provenance
    return {
        "lane": frame.lane, "sequence": frame.sequence,
        "observed_at_utc": frame.clock.observed_at_utc.isoformat(),
        "nominal_interval_ms": frame.quality.nominal_interval_ms, "quality_status": frame.quality.status,
        "boot_identity_sha256": None if provenance is None else provenance.boot_identity_sha256,
        "wire_sequence": None if provenance is None else provenance.wire_sequence,
    }


def coverage_problems(*, frames, receipt, started_utc, finished_utc, host_cadence_ms):
    """Does this sealed window actually bracket the campaign it is asked to score?

    Both lanes must open no later than one of their own periods after the
    campaign started and close no earlier than one period before it finished,
    and the host lane must still hold a sample taken after the campaign closed.
    """
    problems = []
    if receipt.started_at_utc > started_utc:
        problems.append("sampling_started_after_campaign")
    if receipt.finished_at_utc < finished_utc:
        problems.append("sampling_finished_before_campaign")
    for lane in _LANES:
        lane_frames = [frame for frame in frames if frame.lane == lane]
        if not lane_frames:
            problems.append("lane_without_trusted_frames:" + lane)
            continue
        # The lane's own slowest declared cadence, the same tolerance the resource gate applies.
        period = timedelta(milliseconds=max(frame.quality.nominal_interval_ms for frame in lane_frames))
        observed = sorted(frame.clock.observed_at_utc for frame in lane_frames)
        if observed[0] > started_utc + period:
            problems.append("lane_opens_after_campaign_start:" + lane)
        if observed[-1] < finished_utc - period:
            problems.append("lane_closes_before_campaign_finish:" + lane)
    host_frames = [frame for frame in frames if frame.lane == "host_slow"]
    if any(frame.quality.nominal_interval_ms != host_cadence_ms for frame in host_frames):
        problems.append("host_cadence_differs_from_the_frozen_configuration")
    if not [frame for frame in host_frames if frame.clock.observed_at_utc > finished_utc]:
        problems.append("host_edge_sample_missing_after_campaign_close")
    return tuple(sorted(set(problems)))


# --- delivery gate --------------------------------------------------------------------


def gate_problems(report, *, mode):
    """The measurement gate: is the resource window actually proven for this run?"""
    problems = []
    if report.resource_safety.status != "pass":
        problems.append("resource_safety_" + report.resource_safety.status)
    for unknown in report.unknowns:
        if unknown.startswith("resource_") or unknown.startswith("telemetry_"):
            problems.append("unknown:" + unknown)
    if mode == "run" and report.run_validity.status != "complete":
        problems.append("run_validity_" + report.run_validity.status)
    return tuple(sorted(set(problems)))


def driver_status(*, measurement_problems, delivery_problems):
    """One explicit verdict: a valid measurement is not by itself a business acceptance."""
    if measurement_problems:
        return "measurement_failed"
    if delivery_problems:
        return "delivery_failed"
    return "pass"


def delivery_problems(report, *, mode):
    """The business obligations the frozen evaluation plan itself scored.

    A measured run whose delivery fails is a valid measurement and a failed
    acceptance; the driver reports both and passes only on both. No threshold is
    invented here - these are the report's own verdicts against its frozen plan.
    """
    if mode != "run":
        return ()
    problems = []
    if report.delivery_pass is not True:
        problems.append("delivery_pass_false")
    if report.business_obligations_closed.all_closed is not True:
        problems.append("business_obligations_open")
    return tuple(problems)


# --- owned finite children ------------------------------------------------------------


class Child:
    """One owned finite subprocess in its own session; only its own exit proves it ended."""

    def __init__(self, argv, *, output, label, environment=None, cwd=None):
        self.label = label
        self.stdout_path = output / (label + ".stdout")
        self.stderr_path = output / (label + ".stderr")
        self._out = self.stdout_path.open("xb")
        self._err = self.stderr_path.open("xb")
        try:
            self._process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=self._out, stderr=self._err,
                env=environment, cwd=cwd, start_new_session=True,
            )
        except BaseException:
            self.close_files()
            raise
        self.pid = self._process.pid
        self.interrupted = False
        self.killed = False

    def poll(self):
        return self._process.poll()

    def wait(self, timeout):
        try:
            return self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def interrupt(self):
        """Cooperative stop through the child's own documented interrupt path."""
        self.interrupted = True
        try:
            os.kill(self.pid, signal.SIGINT)
        except ProcessLookupError:
            pass

    def kill_group(self):
        self.killed = True
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return self.wait(30)

    def close_files(self):
        for stream in (getattr(self, "_out", None), getattr(self, "_err", None)):
            if stream is not None and not stream.closed:
                stream.close()

    def output_head(self):
        """A bounded head of each pipe file, for the evidence document only."""
        texts = {}
        for name, path in (("stdout", self.stdout_path), ("stderr", self.stderr_path)):
            if path.is_file():
                with path.open("rb") as stream:
                    texts[name] = stream.read(MAX_CHILD_TEXT_BYTES).decode("utf-8", "replace")
            else:
                texts[name] = ""
        return texts


def await_trusted_frames(*, telemetry, frames_path, wait_seconds, run_id, runtime_bundle_sha256,
                         process_profile_sha256, lane_identity, probe_bytes=MAX_FRAME_PROBE_BYTES,
                         now=time.monotonic, sleep=time.sleep, poll_seconds=1.0):
    """Wait for one trusted original frame per lane, or fail closed with the reason.

    A telemetry owner that exited, a wait that ran out and a bounded head that
    filled are each a named stop: none of them may be read as sampling proof,
    and a progress line never stands in for a frame.
    """
    deadline = now() + wait_seconds
    first, rejected, session_first = {}, {}, None
    while len(first) < len(_LANES):
        require(telemetry.poll() is None, "the telemetry owner exited before both lanes produced a frame")
        require(now() < deadline, "no trusted frame pair within the frozen first-frame wait")
        probed = probe_frames(frames_path, maximum_bytes=probe_bytes)
        for frame in probed:
            problems = frame_problems(
                frame, run_id=run_id, runtime_bundle_sha256=runtime_bundle_sha256,
                process_profile_sha256=process_profile_sha256, lane_identity=lane_identity,
            )
            if problems:
                label = frame.lane + ":" + ",".join(problems)
                rejected[label] = rejected.get(label, 0) + 1
            else:
                first.setdefault(frame.lane, frame)
        if probed:
            observed = min(frame.clock.observed_at_utc for frame in probed)
            session_first = observed if session_first is None else min(session_first, observed)
        if len(first) < len(_LANES):
            require(not Path(frames_path).is_file()
                    or Path(frames_path).stat().st_size <= probe_bytes,
                    "the bounded frame head filled before both lanes produced a trusted frame")
            sleep(poll_seconds)
    return first, rejected, session_first


def supervise(*, campaign, telemetry, deadline_seconds, evidence, transport_check=None,
              now=time.monotonic, sleep=time.sleep, poll_seconds=0.5):
    """Watch both finite children until the campaign ends, one fails, or the frozen span is spent.

    ``transport_check`` returns a problem name once the entry's own launch record
    is seen to contradict the frozen budget. It is a consistency detector running
    beside the campaign, not a gate ahead of it: this poll may first observe the
    record after READY or after admission, so what it buys is an early stop and a
    named cause, never the authorization the pre-flight already gave.
    """
    deadline = now() + deadline_seconds
    while True:
        code = campaign.poll()
        if code is not None:
            evidence["campaign_exit_code"] = code
            return "campaign_exited"
        code = telemetry.poll()
        if code is not None:
            evidence["telemetry_exit_code"] = code
            evidence["stop_reason"] = "telemetry_owner_exited_before_the_campaign"
            return "telemetry_exited"
        if transport_check is not None:
            problem = transport_check()
            if problem is not None:
                evidence["stop_reason"] = problem
                return "transport_rejected"
        if now() >= deadline:
            evidence["stop_reason"] = "campaign_span_budget_exceeded"
            return "deadline"
        sleep(poll_seconds)


def stop_campaign(campaign, *, grace_seconds, evidence):
    """Stop new admission through the entry's own interrupt path; never claim the remote owner exited."""
    campaign.interrupt()
    code = campaign.wait(grace_seconds)
    if code is None:
        code = campaign.kill_group()
        evidence["campaign_forced"] = True
    evidence["campaign_exit_code"] = code
    evidence["owner_outcome"] = _UNKNOWN_REMOTE
    return code


def stop_telemetry(telemetry, *, grace_seconds, evidence):
    """Let the owner run its own closure first; a forced stop leaves the remote session unknown."""
    telemetry.interrupt()
    code = telemetry.wait(grace_seconds)
    if code is None:
        code = telemetry.kill_group()
        evidence["telemetry_forced"] = True
        evidence["telemetry_remote_outcome"] = _UNKNOWN_REMOTE
    evidence["telemetry_exit_code"] = code
    return code


# --- frozen telemetry request ---------------------------------------------------------


def lane_launch(document, *, label):
    plan = document[label]
    require(type(plan) is dict and set(plan) == {"config_path", "manifest_path", "remote_config_path"},
            "telemetry request lane " + label + " fields are not closed")
    config = read_bounded(absolute(plan["config_path"], label + " config"), maximum=MAX_LANE_BYTES)
    manifest = read_bounded(absolute(plan["manifest_path"], label + " manifest"), maximum=MAX_LANE_BYTES)
    remote = plan["remote_config_path"]
    require(type(remote) is str and remote, label + " remote config path must be a string")
    return ResidentLaneLaunch(config, manifest, remote)


def build_owner_request(document):
    """Direct field mapping from the private frozen request to the existing owner request.

    Nothing is defaulted, renamed or derived here: every field is the approved
    input's own value, and the lane bytes are the approved configuration files.
    """
    require(document.get("contract_version") == REQUEST_CONTRACT,
            "telemetry request contract version differs")
    owner = document.get("owner")
    require(type(owner) is dict, "telemetry request has no owner mapping")
    expected = {"evidence_directory", "observer_artifact_root", "run_id", "gpu", "host", "source_hashes_path",
                "process_profile_path", "capacity_config_path", "windows_node_identity_sha256", "ssh",
                "ssh_executable", "ssh_executable_sha256", "duration_seconds"}
    require(set(owner) == expected, "telemetry request owner fields are not closed")
    ssh = owner["ssh"]
    require(type(ssh) is dict and set(ssh) == {"address", "port", "username", "private_key_path", "known_hosts_path"},
            "telemetry request ssh fields are not closed")
    source_hashes = load_bounded(absolute(owner["source_hashes_path"], "source hashes"), maximum=MAX_LANE_BYTES)
    profile_raw = read_bounded(absolute(owner["process_profile_path"], "process profile"), maximum=MAX_PROFILE_BYTES)
    return ResidentTelemetryOwnerRequest(
        absolute(owner["evidence_directory"], "evidence directory"),
        absolute(owner["observer_artifact_root"], "observer artifact root"),
        owner["run_id"],
        lane_launch(owner, label="gpu"),
        lane_launch(owner, label="host"),
        source_hashes,
        profile_raw,
        owner["windows_node_identity_sha256"],
        ResidentSSHConfig(ssh["address"], ssh["port"], ssh["username"], ssh["private_key_path"],
                          ssh["known_hosts_path"]),
        absolute(owner["ssh_executable"], "ssh executable"),
        owner["ssh_executable_sha256"],
        owner["duration_seconds"],
        read_bounded(absolute(owner["capacity_config_path"], "capacity config"), maximum=MAX_LANE_BYTES),
    )


def pin_inputs(files, request):
    """Every frozen byte this run depends on: the entry's inputs by path, the private ones by role.

    The lane configurations, manifests, profile and source hashes are read again
    through the request the child also reads, so an indirect input cannot change
    between the parent's pre-flight and the end of the run without being seen.
    """
    pins = {str(path): digest(read_bounded(path, maximum=MAX_SUMMARY_BYTES)) for path in files}
    pins.update({
        "telemetry:gpu-config": digest(request.gpu.config_bytes),
        "telemetry:gpu-manifest": digest(request.gpu.manifest_bytes),
        "telemetry:host-config": digest(request.host.config_bytes),
        "telemetry:host-manifest": digest(request.host.manifest_bytes),
        "telemetry:process-profile": digest(request.process_profile_bytes),
        "telemetry:capacity-config": digest(request.capacity_config_bytes),
        "telemetry:source-hashes": digest(canonical_bytes(dict(request.source_hashes))),
    })
    return pins


def request_budget(document):
    budget = document.get("budget")
    require(type(budget) is dict and set(budget) == {
        "launcher_transport_timeout_seconds", "sampling_start_allowance_seconds",
        "campaign_launch_allowance_seconds", "cleanup_allowance_seconds",
        "first_frame_wait_seconds", "telemetry_stop_grace_seconds", "telemetry_close_allowance_seconds",
    }, "telemetry request budget fields are not closed")
    for key, value in budget.items():
        require(type(value) in (int, float) and not isinstance(value, bool), "telemetry request budget "
                + key + " must be a number")
    for key in ("sampling_start_allowance_seconds", "campaign_launch_allowance_seconds",
                "cleanup_allowance_seconds"):
        require(0 <= budget[key] <= 3600, "telemetry request budget " + key + " must be 0..3600 seconds")
    for key in ("first_frame_wait_seconds", "telemetry_stop_grace_seconds",
                "telemetry_close_allowance_seconds"):
        require(0 < budget[key] <= 3600, "telemetry request budget " + key + " must be 0..3600 seconds")
    # Declared here, then adjudicated in the pre-flight against the composition root's own
    # pure rule and re-checked against the entry's launch record; never a guessed constant.
    require(0 < budget["launcher_transport_timeout_seconds"] <= 86400,
            "telemetry request declared launcher transport timeout is out of range")
    return budget


def lane_identities(request):
    """The approved lane identity each frame must carry; read from the frozen configuration bytes."""
    identities = {}
    for lane, plan in zip(_LANES, (request.gpu, request.host)):
        config = strict_json_loads(plan.config_bytes)
        require(type(config) is dict and config.get("lane") == lane, "frozen lane configuration differs")
        identity = config.get("owner_identity")
        require(type(identity) is dict, "frozen lane configuration has no owner identity")
        identities[lane] = {
            "boot_identity_sha256": identity["boot_identity_sha256"],
            "host_assignment_identity_sha256": identity["host_assignment_identity_sha256"],
        }
    return identities


def host_cadence_ms(request):
    config = strict_json_loads(request.host.config_bytes)
    cadence = config.get("cadence_ms")
    require(type(cadence) is int and cadence > 0, "frozen host lane cadence is unreadable")
    return cadence


# --- bounded telemetry child entry ----------------------------------------------------


def telemetry_child(argv):
    """Bounded in-package child entry: one finite owner session, no new production CLI."""
    parser = argparse.ArgumentParser(prog="telemetry-child", description=telemetry_child.__doc__)
    parser.add_argument("--request", type=Path, required=True)
    arguments = parser.parse_args(argv)
    document = load_bounded(arguments.request, maximum=MAX_REQUEST_BYTES)
    request = build_owner_request(document)
    try:
        result = run_resident_telemetry_session(
            request, progress=lambda message: print(json.dumps({"progress": message}), flush=True),
        )
    except BaseException as exc:  # noqa: BLE001 - the owner already retained its own original evidence
        traceback.print_exc(limit=20, file=sys.stderr)
        print(json.dumps({"telemetry_child_error": type(exc).__name__, "message": str(exc)[:2000]}), flush=True)
        return 1
    print(json.dumps({
        "observer_status": result.observer.evidence_status,
        "total_cpu_ns": result.cpu.total_cpu_ns, "within_two_percent": result.cpu.within_two_percent,
        "run_directory": str(result.observer.run_directory),
    }), flush=True)
    return 0


# --- verification ---------------------------------------------------------------------


def _reason(exc):
    """A bounded, non-secret description of why a pure contract check rejected the bytes."""
    return (type(exc).__name__ + ": " + str(exc))[:200]


def _pinned(directory, index, name, *, maximum):
    """One retained evidence file, read back and bound to the owner's own hash index."""
    raw = read_bounded(directory / name, maximum=maximum)
    require(index.get(name) == digest(raw),
            "retained owner evidence differs from the owner's own index: " + name)
    return raw


def resident_evidence_problems(request, *, frames, receipt, seal, sampling_plan, result_document):
    """Re-run the owner's own pure checks over the bytes it retained.

    The owner made these checks while the session was alive; repeating them here
    over the retained originals is what proves the evidence directory holds that
    session rather than a summary of it. Nothing is re-derived: the same product
    validators, bound to the same replayed frames, receipt and seal, and every
    file is read back against the owner's own hash index first.
    """
    directory = request.evidence_directory
    index = result_document.get("evidence_sha256")
    problems, facts = [], {}
    if type(index) is not dict:
        return ("owner_result_evidence_index_absent",), facts
    try:
        intent = strict_json_loads(_pinned(directory, index, "owner-intent.json", maximum=MAX_OWNER_RESULT_BYTES))
        profile_raw = _pinned(directory, index, "process-profile.json", maximum=MAX_PROFILE_BYTES)
    except (AssertionError, ValueError, OSError) as exc:
        return ("owner_evidence_unreadable:" + _reason(exc),), facts
    try:
        sources = strict_json_loads(_pinned(directory, index, "local-sources.json",
                                            maximum=MAX_OWNER_RESULT_BYTES))
        # The owner already snapshots the composition it ran; compare that snapshot with this checkout.
        recorded = {name: entry.get("sha256") if type(entry) is dict else None
                    for name, entry in sources.items()}
        if recorded != {name: digest(payload) for name, payload in owner_module._local_sources().items()}:
            problems.append("owner_composition_source_differs_from_this_checkout")
    except (AssertionError, ValueError, OSError, AttributeError) as exc:
        problems.append("owner_composition_source_unreadable:" + _reason(exc))
    # The child read the frozen inputs itself; its own journal must agree with the parent's bytes.
    if profile_raw != request.process_profile_bytes:
        problems.append("owner_process_profile_differs_from_the_frozen_input")
    for key, expected in (("run_id", request.run_id), ("duration_seconds", request.duration_seconds),
                          ("source_hashes", dict(request.source_hashes)),
                          ("ssh_executable_sha256", request.ssh_executable_sha256)):
        if intent.get(key) != expected:
            problems.append("owner_intent_differs_from_the_frozen_input:" + key)
    readies, closures = {}, {}
    for lane, plan in zip(_LANES, (request.gpu, request.host), strict=True):
        try:
            config = _pinned(directory, index, lane + "-config.json", maximum=MAX_LANE_BYTES)
            manifest = _pinned(directory, index, lane + "-manifest.json", maximum=MAX_LANE_BYTES)
            ready_raw = _pinned(directory, index, lane + "-ready.stdout", maximum=MAX_OWNER_RESULT_BYTES)
            start_raw = _pinned(directory, index, lane + "-start.stdout", maximum=MAX_OWNER_RESULT_BYTES)
            closed_raw = _pinned(directory, index, lane + "-closed.stdout", maximum=MAX_OWNER_RESULT_BYTES)
            if config != plan.config_bytes or manifest != plan.manifest_bytes:
                problems.append("lane_input_differs_from_the_frozen_request:" + lane)
            outer = strict_json_loads(ready_raw)
            ready = check_resident_ready(
                config_bytes=config, ready_bytes=outer["ready_raw"].encode(), manifest_bytes=manifest,
                expected_source_hashes=request.source_hashes,
            )
            observed = check_external_windows_observation(
                payload=ready_raw, ready=ready,
                windows_node_identity_sha256=request.windows_node_identity_sha256,
            )
            check_external_windows_observation(
                payload=closed_raw, ready=ready,
                windows_node_identity_sha256=request.windows_node_identity_sha256, previous_ready=observed,
            )
            external = strict_json_loads(closed_raw)
            job_bytes, closed_bytes = external["job_raw"].encode(), external["closed_raw"].encode()
            if start_raw.rstrip(b"\r\n") != job_bytes:
                problems.append("starter_stdout_differs_from_the_reread_job:" + lane)
            closure = check_resident_closure(
                ready=ready, closed_bytes=closed_bytes, job_bytes=job_bytes,
                linux_closed_bytes=external["linux_closed_raw"].encode() if lane == "host_slow" else None,
            )
            # A v4 run is mapped against its own frozen plan; the superseded mapping stays for
            # historical evidence and never scores a new run.
            check_resident_observer_mapping_v4(
                ready=ready, closed_bytes=closed_bytes, frames=frames, receipt=receipt,
                plan=sampling_plan,
            )
        except (AssertionError, ValueError, TypeError, AttributeError, KeyError, OSError) as exc:
            problems.append("resident_evidence_rejected:" + lane + ":" + _reason(exc))
            continue
        readies[lane], closures[lane] = ready, closure
        facts[lane] = {"session": ready.session, "cadence_ms": ready.cadence_ms,
                       "closed_sha256": digest(closed_bytes), "job_sha256": digest(job_bytes)}
    if len(readies) == len(_LANES):
        try:
            cpu = check_combined_resident_cpu_v4(
                gpu_ready=readies["gpu_fast"], host_ready=readies["host_slow"],
                gpu_closure=closures["gpu_fast"], host_closure=closures["host_slow"],
                receipt=receipt, seal=seal,
            )
        except (AssertionError, ValueError, TypeError) as exc:
            problems.append("combined_cpu_rejected:" + _reason(exc))
        else:
            facts["total_cpu_ns"] = cpu.total_cpu_ns
            facts["within_two_percent"] = cpu.within_two_percent
            if not cpu.within_two_percent:
                problems.append("observer_overhead_above_two_percent")
            if result_document.get("total_cpu_ns") != cpu.total_cpu_ns:
                problems.append("owner_result_cpu_differs_from_the_independent_replay")
    return tuple(sorted(set(problems))), facts


def owner_evidence_problems(request, *, frames, receipt, seal, sampling_plan, receipt_sha256, seal_sha256):
    """The owner's result document and the original bytes behind every claim in it."""
    document = load_bounded(request.evidence_directory / "owner-result.json", maximum=MAX_OWNER_RESULT_BYTES)
    index = document.get("evidence_sha256")
    problems = []
    # The R22 owner writes v2, which binds its own result to three further facts: the receipt
    # protocol it replayed, the exact frozen plan bytes, and the original intent it retained.
    # Each expectation below is this driver's own value - the digest of the plan this driver
    # replayed, and the owner's index entry for the intent, whose agreement with the bytes on
    # disk `_pinned` proves separately. The version stays a literal here on purpose: importing
    # the product's constant would make this validator agree with the product by construction.
    for key, expected in (("contract_version", "mineru.resident-owner-diagnostic.v2"),
                          ("run_id", request.run_id), ("receipt_version", RECEIPT_VERSION),
                          ("sampling_plan_sha256",
                           digest(canonical_bytes(sampling_plan.model_dump(mode="json")))),
                          ("owner_intent_sha256",
                           index.get("owner-intent.json") if type(index) is dict else None),
                          ("observer_receipt_sha256", receipt_sha256),
                          ("observer_seal_sha256", seal_sha256), ("observer_status", "complete"),
                          ("within_two_percent", True), ("activation_authorized", False)):
        if document.get(key) != expected or type(document.get(key)) is not type(expected):
            problems.append("owner_result_differs:" + key)
    evidence, facts = resident_evidence_problems(
        request, frames=frames, receipt=receipt, seal=seal, sampling_plan=sampling_plan,
        result_document=document,
    )
    return tuple(sorted(set(problems + list(evidence)))), facts


def run_summary(*, python_executable, service_root, output, campaign_dir, evaluation_plan, request,
                environment):
    """Call the existing summary entry; its exit code is only 'the report was written'.

    The owner's retained evidence directory is passed explicitly so the product's own
    reader re-checks the original READY/start/closed/Job records through the same
    validators this driver replays, instead of scoring the observer seal alone.
    """
    command = [str(python_executable), "-m", "disclosure_anchor.cli.m6_campaign", "summary",
               "--run-dir", str(campaign_dir), "--evaluation-plan", str(evaluation_plan),
               "--telemetry-artifact-root", str(request.observer_artifact_root),
               "--telemetry-run-id", request.run_id,
               "--telemetry-receipt-version", str(RECEIPT_VERSION),
               "--resident-owner-evidence-dir", str(request.evidence_directory),
               "--output", str(output / "report")]
    save(output / "summary-command.json", command)
    with (output / "summary.stdout").open("xb") as out, (output / "summary.stderr").open("xb") as err:
        completed = subprocess.run(command, cwd=str(service_root), env=environment, stdout=out,
                                   stderr=err, timeout=900, check=False)
    return completed.returncode


def verify(*, request, campaign_dir, campaign_summary, mode, transport_timeout_seconds, evidence):
    """Independent replay and gate; the campaign's own summary supplies the window to cover."""
    result = verify_synchronized_telemetry_observer(
        artifact_root=request.observer_artifact_root, run_id=request.run_id,
        receipt_version=RECEIPT_VERSION,
    )
    receipt, seal = result.receipt, result.seal
    # The owner's own serialisation, so its result document and this replay compare byte for byte.
    receipt_sha256 = artifact_sha256(canonical_bytes(receipt.model_dump(mode="json")))
    seal_sha256 = artifact_sha256(canonical_bytes(seal.model_dump(mode="json")))
    # Proves this driver's canonical bytes agree with the sealed hash before they are compared
    # with the owner's own result document.
    require(seal.receipt_sha256 == receipt_sha256, "sealed receipt hash differs from the replayed receipt")
    profile = decode_mineru_process_profile(request.process_profile_bytes)
    trusted = trusted_frames(
        result.frames, run_id=request.run_id, runtime_bundle_sha256=profile.runtime_bundle_identity_sha256,
        process_profile_sha256=profile.sha256, lane_identity=lane_identities(request),
    )
    started = datetime.fromisoformat(campaign_summary["started_utc"])
    finished = datetime.fromisoformat(campaign_summary["finished_utc"])
    require(started.tzinfo is not None and finished.tzinfo is not None,
            "the campaign summary window is not an absolute UTC bracket")
    coverage = coverage_problems(frames=trusted, receipt=receipt, started_utc=started, finished_utc=finished,
                                 host_cadence_ms=host_cadence_ms(request))
    owner_problems, owner_facts = owner_evidence_problems(
        request, frames=result.frames, receipt=receipt, seal=seal, sampling_plan=result.plan,
        receipt_sha256=receipt_sha256, seal_sha256=seal_sha256,
    )
    edges = [frame for frame in trusted
             if frame.lane == "host_slow" and frame.clock.observed_at_utc > finished]
    evidence["telemetry"] = {
        "receipt_sha256": receipt_sha256, "seal_sha256": seal_sha256,
        "observer_status": result.evidence_status, "receipt_status": receipt.status,
        "sampling_started_utc": receipt.started_at_utc.isoformat(),
        "sampling_finished_utc": receipt.finished_at_utc.isoformat(),
        "frames_total": len(result.frames), "frames_trusted": len(trusted),
        "untrusted_frames": len(result.frames) - len(trusted),
        "host_edge_samples_after_campaign": len(edges),
        "first_host_edge_after_campaign": frame_identity(edges[0]) if edges else None,
        "coverage_problems": list(coverage),
        "resident_evidence_problems": list(owner_problems), "resident_evidence": owner_facts,
    }
    problems = list(coverage) + list(owner_problems)
    if result.evidence_status != "complete":
        problems.append("observer_status_" + str(result.evidence_status))
    if len(trusted) != len(result.frames):
        problems.append("untrusted_frames_in_sealed_window")
    # Final agreement: the entry's own launch record must still match the transport the
    # pre-flight adjudicated against the composition root's rule before any child existed.
    recorded, transport_problem = launcher_transport_state(campaign_dir, declared=transport_timeout_seconds)
    evidence["launcher_transport_recorded"] = recorded
    if not recorded:
        problems.append("launcher_command_record_absent")
    elif transport_problem is not None:
        problems.append(transport_problem)
    if campaign_summary.get("status") != "complete":
        problems.append("campaign_status_" + str(campaign_summary.get("status")))
    for key in ("owner_external_exit_verified", "local_children_reaped"):
        if campaign_summary.get(key) is not True:
            problems.append("campaign_" + key + "_false")
    if campaign_summary.get("cleanup_failures"):
        problems.append("campaign_cleanup_failures")
    if mode == "bootstrap-check" and campaign_summary.get("admitted_count") != 0:
        problems.append("bootstrap_check_admitted_business_work")
    return tuple(sorted(set(problems)))


# --- entry ----------------------------------------------------------------------------


class DriverStopped(Exception):
    """This driver was asked to stop; its two children are still its own to close."""


def install_stop_handlers():
    """A terminating signal must run the cleanup below, never orphan an owned child."""
    def stop(number, _frame):
        raise DriverStopped("measured driver received signal " + str(number))

    for number in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, stop)


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True, help="explicit real-machine opt-in")
    for key in ("intent", "private-binding", "binding", "package", "manifest", "scope", "quality-plan",
                "evaluation-plan", "telemetry-request", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--intent-sha256", required=True)
    parser.add_argument("--mode", choices=("run", "bootstrap-check"), default="run")
    parser.add_argument("--attempt-id", default=None)
    return parser.parse_args(argv)


def main(argv=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "telemetry-child":
        return telemetry_child(raw[1:])
    args = parse_arguments(raw)
    output = args.output.absolute()
    require(not output.exists(), "measured driver output must be new")
    output.mkdir(mode=0o700)
    evidence = {"contract_version": DRIVER_CONTRACT, "status": "incomplete", "mode": args.mode,
                "started_utc": utc_now(), "failure": None,
                # This driver's own monotonic instants are orchestration evidence only. The
                # measured window is the product's: the observer's plan and the campaign
                # summary's own entry/finish in the same domain.
                "orchestration_monotonic_ns": {"started": time.monotonic_ns()}}
    campaign = telemetry = None
    # Whatever ends this run - a named stop, a failed check or a signal - the children get the
    # same frozen grace. These are set before either child can exist, so the cleanup below
    # always finds them; they only widen as the frozen terms become known.
    stop_grace = {}
    evidence["stop_grace"] = stop_grace
    install_stop_handlers()
    try:
        stop_grace["campaign"] = stop_grace["telemetry"] = _constant("_CANCEL_TIMEOUT_SECONDS")
        intent_raw = read_bounded(args.intent, maximum=MAX_SUMMARY_BYTES)
        require(digest(intent_raw) == args.intent_sha256, "intent changed")
        intent = decode_campaign_intent(intent_raw)
        require(type(intent) is M6CampaignIntent, "a measured campaign requires a prebound v2 intent")
        # The campaign entry's own loader and type: one definition of the private target.
        binding = load_campaign_private_binding(args.private_binding)
        document = load_bounded(args.telemetry_request, maximum=MAX_REQUEST_BYTES)
        request = build_owner_request(document)
        allowances = request_budget(document)
        stop_grace["telemetry"] = allowances["telemetry_stop_grace_seconds"]
        release_manifest = args.package / "release-manifest.json"
        files = [args.intent, args.private_binding, args.binding, release_manifest, args.manifest, args.scope,
                 args.quality_plan, args.evaluation_plan, args.telemetry_request]
        pins = pin_inputs(files, request)
        save(output / "input-hashes.json", pins)

        # Local pre-flight of the whole frozen telemetry input: no remote contact, no child yet.
        owner_module._validate_request(request)
        require(not request.evidence_directory.exists(), "owner evidence directory must be new")
        observer_run = request.observer_artifact_root / request.run_id
        require(not observer_run.exists(), "observer run directory already exists; a session is never reused")

        # The three frozen inputs must name one runtime; a disagreement is refused here
        # rather than discovered by the campaign's summary after the window is spent.
        profile = decode_mineru_process_profile(request.process_profile_bytes)
        release_binding = load_bounded(args.binding, maximum=MAX_SUMMARY_BYTES)
        identity = identity_problems(intent=intent, binding=binding, request=request, profile=profile,
                                     release_binding=release_binding)
        evidence["identity"] = {
            "problems": list(identity),
            "runtime_bundle_identity_sha256": profile.runtime_bundle_identity_sha256,
            "process_profile_sha256": profile.sha256,
            "capacity_config_sha256": decode_mineru_capacity_config(request.capacity_config_bytes).sha256,
            "windows_node_identity_sha256": request.windows_node_identity_sha256,
            "nvml_dll_sha256": binding.windows.nvml_dll_sha256,
            "gpu_target_agrees": "campaign_gpu_target_differs_from_the_telemetry_gpu_lane" not in identity,
        }
        save(output / "identity.json", evidence["identity"])
        require(not identity, "the frozen campaign and telemetry inputs name different runtimes: "
                + "; ".join(identity))
        # Adjudicates the declared transport against the composition root's own rule and
        # proves the window covers the whole campaign - both before either child exists.
        budget = freeze_budget(
            intent=intent, host_cadence_ms=host_cadence_ms(request),
            duration_seconds=request.duration_seconds,
            launcher_transport_timeout_seconds=allowances["launcher_transport_timeout_seconds"],
            sampling_start_allowance_seconds=allowances["sampling_start_allowance_seconds"],
            campaign_launch_allowance_seconds=allowances["campaign_launch_allowance_seconds"],
            cleanup_allowance_seconds=allowances["cleanup_allowance_seconds"],
        )
        # Once the budget is frozen the entry's own failure path is bounded: its cancel plus
        # the fetches it still has to make.
        stop_grace["campaign"] = _constant("_CANCEL_TIMEOUT_SECONDS") + budget["retrieval_seconds"]
        save(output / "budget.json", budget)
        evidence["budget"] = budget
        require(budget["satisfied"], "frozen finite budget is refused before any child: "
                + "; ".join(budget["problems"])
                + f" (required {budget['required_total_seconds']}s, duration {budget['duration_seconds']}s,"
                f" transport headroom {budget['launcher_transport_headroom_seconds']}s)")
        require(allowances["first_frame_wait_seconds"] >= allowances["sampling_start_allowance_seconds"],
                "the first-frame wait must not be shorter than the frozen sampling-start allowance")

        service_root, python_executable = binding.service_root, binding.python_executable
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"DATABASE_URL", "DISCLOSURE_MIGRATION_DATABASE_URL"}}
        environment["PYTHONPATH"] = str(service_root / "src")
        telemetry_command = [str(python_executable), "-m",
                             "tests.integration.m6_measured_campaign_independent", "telemetry-child",
                             "--request", str(args.telemetry_request.absolute())]
        save(output / "telemetry-command.json", telemetry_command)
        telemetry_spawn_ns = time.monotonic_ns()
        evidence["orchestration_monotonic_ns"]["telemetry_spawn"] = telemetry_spawn_ns
        telemetry = Child(telemetry_command, output=output, label="telemetry",
                          environment=environment, cwd=str(service_root))
        evidence["telemetry_pid"] = telemetry.pid
        save(output / "telemetry-start.json", {"pid": telemetry.pid, "run_id": request.run_id,
                                               "duration_seconds": request.duration_seconds})

        # Sampling must be proven by original frames from both lanes, never by a progress line.
        first, rejected, session_first = await_trusted_frames(
            # `FRAME_FILENAME` is the superseded v2 stream; a v4 run writes the v3 one, and
            # probing the wrong name looks exactly like two lanes that never sampled.
            telemetry=telemetry, frames_path=observer_run / FRAME_V3_FILENAME,
            wait_seconds=allowances["first_frame_wait_seconds"], run_id=request.run_id,
            runtime_bundle_sha256=profile.runtime_bundle_identity_sha256,
            process_profile_sha256=profile.sha256, lane_identity=lane_identities(request),
        )
        # The observer's own frozen plan, read back and bound to the approved input. Until it
        # is pinned this driver has no window it can prove, so nothing is launched.
        # The observer's clock domain is read independently here, not taken from the plan.
        local_clock_domain = MacObserverIdentityReader().observe()
        plan, plan_sha256, plan_problems = pin_sampling_plan(
            observer_run=observer_run, request=request,
            evidence_directory=request.evidence_directory,
            local_clock_domain_sha256=check_mac_observer_identity(
                local_clock_domain).clock_domain_identity_sha256)
        evidence["sampling_plan"] = {
            "sha256": plan_sha256, "run_id": plan.run_id, "start_ns": plan.start_ns,
            "planned_end_ns": plan.end_ns, "duration_ns": plan.duration_ns,
            "problems": list(plan_problems),
        }
        save(output / "sampling-plan-pin.json", evidence["sampling_plan"])
        require(not plan_problems, "the observer plan does not match the frozen inputs: "
                + "; ".join(plan_problems))
        # The pre-GO reserve is local and causal: this driver spawned the earliest starter it
        # owns, so both instants are its own monotonic clock.
        require_start_headroom(earliest_starter_spawn_ns=telemetry_spawn_ns,
                               sampling_start_ns=plan.start_ns)
        save(output / "first-frames.json", {lane: frame_identity(frame) for lane, frame in sorted(first.items())})
        evidence["first_frames"] = {lane: frame_identity(frame) for lane, frame in sorted(first.items())}
        evidence["rejected_frames_before_launch"] = dict(sorted(rejected.items())[:16])
        evidence["session_first_frame_utc"] = session_first.isoformat()

        # Each frozen reserve is enforced against its own absolute deadline in this same clock,
        # so a phase that overruns stops the run instead of quietly eating the next one.
        started_phase_ns = time.monotonic_ns()
        evidence["phase_seconds"] = {"sampling_start": (started_phase_ns - telemetry_spawn_ns) / NS}
        require(evidence["phase_seconds"]["sampling_start"]
                <= allowances["sampling_start_allowance_seconds"],
                "the sampling-start phase overran its frozen reserve: "
                f"{evidence['phase_seconds']['sampling_start']}s > "
                f"{allowances['sampling_start_allowance_seconds']}s")
        launch_allowance_ns = _exact_ns(allowances["campaign_launch_allowance_seconds"],
                                        "campaign launch allowance")

        # What is left of the frozen window, in the same clock the plan was written in. No UTC
        # difference and no frame timestamp takes part in this.
        remaining = (plan.end_ns - time.monotonic_ns()) / NS
        evidence["remaining_sampling_seconds_at_launch"] = remaining
        require(remaining >= budget["required_after_first_frame_seconds"],
                f"only {remaining}s of sampling remain; the campaign needs "
                f"{budget['required_after_first_frame_seconds']}s")

        campaign_dir = output / "campaign"
        campaign_command = [str(python_executable), "-m", "disclosure_anchor.cli.m6_campaign", args.mode]
        for key in ("intent", "intent-sha256", "private-binding", "binding", "manifest", "scope", "quality-plan",
                    "evaluation-plan"):
            campaign_command.extend(["--" + key, str(getattr(args, key.replace("-", "_")))])
        campaign_command += ["--release-manifest", str(release_manifest), "--output", str(campaign_dir)]
        if args.attempt_id is not None:
            campaign_command += ["--attempt-id", args.attempt_id]
        save(output / "campaign-command.json", campaign_command)
        evidence["orchestration_monotonic_ns"]["campaign_spawn"] = time.monotonic_ns()
        campaign = Child(campaign_command, output=output, label="campaign", environment=environment,
                         cwd=str(service_root))
        evidence["campaign_pid"] = campaign.pid
        save(output / "campaign-start.json", {"pid": campaign.pid, "mode": args.mode,
                                              "maximum_span_seconds": budget["campaign_span_requirement_seconds"]})
        evidence["phase_seconds"]["campaign_launch"] = (time.monotonic_ns() - started_phase_ns) / NS
        require(time.monotonic_ns() - started_phase_ns <= launch_allowance_ns,
                "the campaign-launch phase overran its frozen reserve: "
                f"{evidence['phase_seconds']['campaign_launch']}s > "
                f"{allowances['campaign_launch_allowance_seconds']}s")
        declared_transport = allowances["launcher_transport_timeout_seconds"]

        def transport_check():
            # Polled beside the run; it may first see the record after admission has begun,
            # so it only stops early and names the cause - the pre-flight did the admitting.
            return launcher_transport_state(campaign_dir, declared=declared_transport)[1]

        outcome = supervise(campaign=campaign, telemetry=telemetry, transport_check=transport_check,
                            deadline_seconds=budget["campaign_span_requirement_seconds"], evidence=evidence)
        evidence["supervision_outcome"] = outcome
        if outcome != "campaign_exited":
            # A launcher still inside its own absolute deadline makes this a forced stop, which is
            # recorded as an unknown remote outcome rather than waited out for the rest of the
            # transport lifetime.
            stop_campaign(campaign, evidence=evidence, grace_seconds=stop_grace["campaign"])
        require(outcome == "campaign_exited", "the measured campaign did not finish on its own: " + outcome)
        require(evidence["campaign_exit_code"] == 0,
                f"campaign entry exited {evidence['campaign_exit_code']}; original evidence retained")

        # The sampling window closes on its own finite deadline; it is never cut short here.
        remaining_close = max(60.0, (plan.end_ns - time.monotonic_ns()) / NS
                              + allowances["telemetry_close_allowance_seconds"])
        evidence["telemetry_close_wait_seconds"] = remaining_close
        code = telemetry.wait(remaining_close)
        if code is None:
            code = stop_telemetry(telemetry, grace_seconds=stop_grace["telemetry"], evidence=evidence)
        evidence["telemetry_exit_code"] = code
        require(code == 0, f"telemetry owner exited {code}; its original failure evidence is retained")

        campaign_summary = load_bounded(campaign_dir / "campaign-summary.json", maximum=MAX_SUMMARY_BYTES)
        evidence["campaign_summary"] = {
            key: campaign_summary.get(key) for key in
            ("status", "admitted_count", "owner_external_exit_verified", "local_children_reaped",
             "cleanup_failures", "first_error", "spec_sha256", "started_utc", "finished_utc")
        }
        measurement = list(verify(request=request, campaign_dir=campaign_dir,
                                  campaign_summary=campaign_summary, mode=args.mode,
                                  transport_timeout_seconds=declared_transport, evidence=evidence))

        summary_exit = run_summary(python_executable=python_executable, service_root=service_root,
                                   output=output, campaign_dir=campaign_dir,
                                   evaluation_plan=args.evaluation_plan, request=request,
                                   environment=environment)
        evidence["summary_exit_code"] = summary_exit
        require(summary_exit == 0, f"delivery summary exited {summary_exit}")
        report = M6DeliveryReport.from_canonical_bytes(
            read_bounded(output / "report" / "delivery-report.json",
                         maximum=M6_DELIVERY_REPORT_MAX_BYTES).rstrip(b"\n"),
            maximum_bytes=M6_DELIVERY_REPORT_MAX_BYTES,
        )
        # Exit 0 only means a report exists; the verdict is read from the report itself.
        evidence["delivery"] = {
            "delivery_pass": report.delivery_pass, "run_validity": report.run_validity.status,
            "resource_safety": report.resource_safety.status, "resource_reason": report.resource_safety.reason,
            "obligations_closed": report.business_obligations_closed.all_closed,
            "unknowns": list(report.unknowns)[:64], "unknown_count": len(report.unknowns),
            "report_sha256": report.canonical_sha256(),
        }
        measurement.extend(gate_problems(report, mode=args.mode))
        # Re-read every direct and indirect frozen input, including the ones only the child opened.
        final_request = build_owner_request(load_bounded(args.telemetry_request, maximum=MAX_REQUEST_BYTES))
        if pin_inputs(files, final_request) != pins:
            measurement.append("frozen_input_changed_during_execution")
        evidence["measurement_problems"] = sorted(set(measurement))
        evidence["measurement_valid"] = not evidence["measurement_problems"]
        evidence["delivery_problems"] = sorted(delivery_problems(report, mode=args.mode))
        evidence["problems"] = sorted(set(evidence["measurement_problems"] + evidence["delivery_problems"]))
        # A valid measurement of a failed delivery is a named failure, not a pass and not a crash.
        evidence["status"] = driver_status(measurement_problems=evidence["measurement_problems"],
                                           delivery_problems=evidence["delivery_problems"])
    except (Exception, KeyboardInterrupt) as exc:
        evidence["failure"] = f"{type(exc).__name__}: {str(exc)[:2000]}"
    finally:
        for child in (campaign, telemetry):
            if child is None:
                continue
            if child.poll() is None:
                if child is campaign:
                    stop_campaign(child, grace_seconds=stop_grace["campaign"], evidence=evidence)
                else:
                    stop_telemetry(child, grace_seconds=stop_grace["telemetry"], evidence=evidence)
            evidence[child.label + "_output"] = child.output_head()
            child.close_files()
        evidence["finished_utc"] = utc_now()
        evidence["orchestration_monotonic_ns"]["finished"] = time.monotonic_ns()
        if "sampling_plan" in evidence and not evidence["sampling_plan"]["problems"]:
            evidence.setdefault("phase_seconds", {})["cleanup"] = (
                evidence["orchestration_monotonic_ns"]["finished"]
                - evidence["sampling_plan"]["planned_end_ns"]) / NS
        save(output / "independent-evidence.json", evidence)
    print(json.dumps({key: evidence[key] for key in
                      ("status", "measurement_valid", "failure", "measurement_problems", "delivery_problems",
                       "delivery", "supervision_outcome")
                      if key in evidence}, ensure_ascii=False))
    return 0 if evidence["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
