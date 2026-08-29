"""Anchored observer-v2 artifact projection for full-host-hour replay."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from disclosure_anchor.adapters.runtime.synchronized_telemetry_observer import (
    verify_synchronized_telemetry_observer,
)
from disclosure_anchor.application.contracts.full_host_hour_kpi import (
    VerifiedTelemetryCoverage,
)


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def verified_coverage_from_observer_artifacts(
    *, artifact_root: Path, run_id: str
) -> VerifiedTelemetryCoverage:
    """Reopen anchored files and derive host identity only from sealed frames."""

    result = verify_synchronized_telemetry_observer(
        artifact_root=artifact_root,
        run_id=run_id,
    )
    quality = {item.lane: item for item in result.receipt.lane_quality}
    provenance = {
        frame.lane: frame.resident_exporter_provenance
        for frame in result.frames
        if frame.resident_exporter_provenance is not None
    }
    if set(provenance) != {"gpu_fast", "host_slow"}:
        raise ValueError("sealed observer evidence lacks resident exporter provenance")
    gpu = provenance["gpu_fast"]
    host = provenance["host_slow"]
    assert gpu is not None and host is not None
    if (
        gpu.host_assignment_identity_sha256
        != host.host_assignment_identity_sha256
        or gpu.boot_identity_sha256 != host.boot_identity_sha256
    ):
        raise ValueError("sealed exporter lanes disagree on host or boot identity")
    gpu_devices = {
        frame.gpu.values.device_identity_sha256
        for frame in result.frames
        if frame.lane == "gpu_fast"
        and frame.gpu.status == "supported"
        and frame.gpu.values is not None
    }
    cgroup_epochs = {
        frame.host_cgroup.values.parent_cgroup_epoch_sha256
        for frame in result.frames
        if frame.lane == "host_slow"
        and frame.host_cgroup.status == "supported"
        and frame.host_cgroup.values is not None
    }
    if len(gpu_devices) != 1 or len(cgroup_epochs) != 1:
        raise ValueError("sealed observer evidence lacks one stable GPU/cgroup identity")
    return VerifiedTelemetryCoverage(
        started_at_utc=result.receipt.started_at_utc,
        finished_at_utc=result.receipt.finished_at_utc,
        host_assignment_identity_sha256=gpu.host_assignment_identity_sha256,
        boot_identity_sha256=gpu.boot_identity_sha256,
        gpu_exporter_process_epoch_sha256=gpu.exporter_process_epoch_sha256,
        host_exporter_process_epoch_sha256=host.exporter_process_epoch_sha256,
        gpu_exporter_source_sha256=gpu.exporter_source_sha256,
        host_exporter_source_sha256=host.exporter_source_sha256,
        gpu_device_identity_sha256=next(iter(gpu_devices)),
        parent_cgroup_epoch_sha256=next(iter(cgroup_epochs)),
        runtime_bundle_identity_sha256=result.receipt.runtime_bundle_identity_sha256,
        process_profile_sha256=result.receipt.process_profile.process_profile_sha256,
        observer_process_epoch_sha256=result.receipt.process_profile.process_epoch_sha256,
        receipt_sha256=_canonical_hash(result.receipt.model_dump(mode="json")),
        seal_sha256=_canonical_hash(result.seal.model_dump(mode="json")),
        status=result.evidence_status,
        gpu_lane_complete=(
            quality["gpu_fast"].supported_frame_count
            == quality["gpu_fast"].sample_count
            and quality["gpu_fast"].late_sample_count == 0
            and quality["gpu_fast"].missed_deadline_count == 0
        ),
        host_lane_complete=(
            quality["host_slow"].supported_frame_count
            == quality["host_slow"].sample_count
            and quality["host_slow"].late_sample_count == 0
            and quality["host_slow"].missed_deadline_count == 0
        ),
        observer_overhead_safe=result.seal.status != "unsafe",
        exporter_overhead_safe=False,
        exporter_overhead_attestation_sha256=None,
    )


__all__ = ["verified_coverage_from_observer_artifacts"]
