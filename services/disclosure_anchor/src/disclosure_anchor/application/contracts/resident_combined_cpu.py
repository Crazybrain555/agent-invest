"""Disjoint resident CPU roles, not external activation or a full-run claim."""

from __future__ import annotations

from dataclasses import dataclass
import json

from disclosure_anchor.application.contracts.resident_session_evidence import (
    CheckedResidentClosure, CheckedResidentReady, artifact_sha256, canonical_bytes,
)
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    SynchronizedTelemetryReceiptV3, SynchronizedTelemetrySealV3,
    parse_canonical_json_artifact,
)


def check_resident_supervisor_started(
    *, ready: CheckedResidentReady, started_bytes: bytes, job_bytes: bytes,
) -> None:
    """Bind the new-only pre-Job marker to the eventual parent receipt."""
    started = parse_canonical_json_artifact(started_bytes, label="supervisor start", maximum_bytes=8192)
    job = parse_canonical_json_artifact(job_bytes, label="supervisor Job", maximum_bytes=65536)
    if not isinstance(job, dict) or set(job) != {"config_sha256", "contract_version", "job", "session", "supervisor_process"}:
        raise ValueError("supervisor Job shape differs")
    if job["contract_version"] != "mineru.windows-resident-job-receipt.v1" or job["session"] != ready.session or job["config_sha256"] != artifact_sha256(ready.config_bytes):
        raise ValueError("supervisor Job session binding differs")
    expected = {
        "contract_version": "mineru.windows-resident-supervisor-started.v1",
        "session": ready.session, "config_sha256": artifact_sha256(ready.config_bytes),
        "supervisor_process": job["supervisor_process"],
    }
    if canonical_bytes(started) != canonical_bytes(expected):
        raise ValueError("supervisor pre-Job marker binding differs")


@dataclass(frozen=True, slots=True)
class CheckedCombinedResidentCpu:
    """Mechanical cost boundary; actual processes/host/absence are separate."""

    observer_preseal_ns: int
    gpu_job_ns: int
    host_job_ns: int
    gpu_supervisor_pre_attestation_ns: int
    host_supervisor_pre_attestation_ns: int
    linux_sampler_exit_ns: int
    linux_supervisor_pre_attestation_ns: int
    sampling_elapsed_ns: int

    @property
    def total_cpu_ns(self) -> int:
        return (
            self.observer_preseal_ns + self.gpu_job_ns + self.host_job_ns
            + self.gpu_supervisor_pre_attestation_ns + self.host_supervisor_pre_attestation_ns
            + self.linux_sampler_exit_ns + self.linux_supervisor_pre_attestation_ns
        )

    @property
    def within_two_percent(self) -> bool:
        return 100 * self.total_cpu_ns <= 2 * self.sampling_elapsed_ns


def check_combined_resident_cpu(
    *, gpu_ready: CheckedResidentReady, host_ready: CheckedResidentReady,
    gpu_closure: CheckedResidentClosure, host_closure: CheckedResidentClosure,
    receipt: SynchronizedTelemetryReceiptV3, seal: SynchronizedTelemetrySealV3,
) -> CheckedCombinedResidentCpu:
    """Use one denominator; never add per-frame/collector CPU a second time.

    Observer parent bootstrap, resource tracker, outer control owner, SSH server,
    control CLI and seal/attestation serialization/exit are named exclusions.
    Collector birth-to-reap CPU already enters the observer seal delta.
    Inputs must come from the exact canonical replay, not constructed claims.
    """
    if not isinstance(receipt, SynchronizedTelemetryReceiptV3) or not isinstance(seal, SynchronizedTelemetrySealV3):
        raise ValueError("combined resident CPU requires explicit v3 evidence")
    if (gpu_ready.lane, host_ready.lane) != ("gpu_fast", "host_slow"):
        raise ValueError("combined resident CPU requires distinct GPU/host lanes")
    if gpu_ready.session == host_ready.session or gpu_closure.job_instance == host_closure.job_instance:
        raise ValueError("combined resident CPU sessions/Jobs collide")
    for ready, closed in ((gpu_ready, gpu_closure), (host_ready, host_closure)):
        if ready.session != closed.session:
            raise ValueError("combined resident CPU closure session differs")
        if closed.config_sha256 != artifact_sha256(ready.config_bytes) or closed.ready_sha256 != artifact_sha256(ready.ready_bytes):
            raise ValueError("combined resident CPU closure config/READY binding differs")
        if ready.identity.runtime_bundle_identity_sha256 != receipt.runtime_bundle_identity_sha256 or ready.identity.process_profile_sha256 != receipt.process_profile.process_profile_sha256:
            raise ValueError("combined resident CPU runtime/profile differs")
    for name in ("host_assignment_identity_sha256", "boot_identity_sha256", "runtime_bundle_identity_sha256", "process_profile_sha256"):
        if getattr(gpu_ready.identity, name) != getattr(host_ready.identity, name):
            raise ValueError("combined resident CPU host identity differs")
    # These processes coexist during sampling; PID reuse cannot explain a
    # numeric collision even when the purported birth timestamps differ.
    pids = {gpu_ready.pid, host_ready.pid, gpu_closure.windows_supervisor_epoch[0], host_closure.windows_supervisor_epoch[0]}
    docker_pid = json.loads(host_ready.ready_bytes)["backend"]["docker_pid"]
    if len(pids) != 4 or docker_pid in pids:
        raise ValueError("combined resident CPU Windows process roles collide")
    if gpu_closure.linux_sampler_exit_cpu_ns is not None or gpu_closure.linux_supervisor_pre_attestation_cpu_ns is not None:
        raise ValueError("combined resident GPU lane double-counts Linux CPU")
    if host_closure.linux_sampler_exit_cpu_ns is None or host_closure.linux_supervisor_pre_attestation_cpu_ns is None:
        raise ValueError("combined resident CPU lacks Linux exit cost")
    elapsed = receipt.finished_monotonic_ns - receipt.started_monotonic_ns
    if seal.run_id != receipt.run_id or seal.receipt_sha256 != artifact_sha256(canonical_bytes(receipt.model_dump(mode="json"))) or seal.sampling_elapsed_ns_denominator != elapsed or seal.frames_jsonl_sha256 != receipt.artifacts.frames_jsonl_sha256:
        raise ValueError("combined resident CPU observer seal binding differs")
    result = CheckedCombinedResidentCpu(
        seal.preseal_observer_cpu_ns, gpu_closure.windows_job_cpu_ns, host_closure.windows_job_cpu_ns,
        gpu_closure.windows_supervisor_pre_attestation_cpu_ns, host_closure.windows_supervisor_pre_attestation_cpu_ns,
        host_closure.linux_sampler_exit_cpu_ns, host_closure.linux_supervisor_pre_attestation_cpu_ns, elapsed,
    )
    if any(type(value) is not int or value < 0 for value in (
        result.observer_preseal_ns, result.gpu_job_ns, result.host_job_ns,
        result.gpu_supervisor_pre_attestation_ns, result.host_supervisor_pre_attestation_ns,
        result.linux_sampler_exit_ns, result.linux_supervisor_pre_attestation_ns,
    )) or elapsed <= 0:
        raise ValueError("combined resident CPU cost is invalid")
    return result
