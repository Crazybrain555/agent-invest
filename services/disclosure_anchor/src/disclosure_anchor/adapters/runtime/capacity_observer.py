"""External, read-only capacity observer with replayable private evidence."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import time
from typing import Any, cast
import uuid

import disclosure_anchor.adapters.runtime as runtime_package_module
import disclosure_anchor.adapters.runtime.capacity_runtime_identity as capacity_runtime_identity_module
import disclosure_anchor.adapters.runtime.capacity_sources as capacity_sources_module
import disclosure_anchor.adapters.runtime.gpu_telemetry_freshness as gpu_freshness_module
import disclosure_anchor.adapters.runtime.mineru_identity as mineru_identity_module
import disclosure_anchor.adapters.runtime.mineru_host_capacity_observer as host_module
import disclosure_anchor.adapters.storage.path_builder as path_builder_module
import disclosure_anchor.application.contracts.capacity as capacity_contract_module
import disclosure_anchor.application.services.capacity_aggregation as aggregation_module
import disclosure_anchor.settings as settings_module
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.application.contracts.capacity import (
    CapacityArtifactDigests,
    CapacityObservationInterval,
    CapacityObservationRun,
    CapacityRawSample,
    CapacitySource,
    CapacitySourceCounts,
    GpuSampleValues,
    IntervalStatus,
    RawSampleValues,
    SafetyViolation,
    canonical_json_bytes,
    chained_record_payload,
    verify_chained_record,
)
from disclosure_anchor.application.ports.capacity_observation import (
    CapacitySamplerPort,
)
from disclosure_anchor.application.services.capacity_aggregation import (
    aggregate_capacity_interval,
)
from disclosure_anchor.settings import Settings


MAX_RAW_RECORDS = 500_000
MAX_INTERVAL_RECORDS = 20_000
MAX_RAW_FILE_BYTES = 256 * 1024 * 1024
MAX_INTERVAL_FILE_BYTES = 64 * 1024 * 1024
MAX_RAW_RECORD_BYTES = 64 * 1024
MAX_INTERVAL_RECORD_BYTES = 256 * 1024
MAX_RUN_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class _SampleResult:
    source: CapacitySource
    duration_seconds: float
    finished_monotonic_seconds: float
    values: RawSampleValues | None
    reason_code: str | None
    underlying_reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class CapacityVerificationResult:
    run: CapacityObservationRun
    raw_samples: tuple[CapacityRawSample, ...]
    intervals: tuple[CapacityObservationInterval, ...]


class _CapacityArtifactWriter:
    def __init__(self, *, paths: FileStorePathBuilder, run_id: str) -> None:
        self.raw_path = paths.runtime_capacity_observation_path(
            run_id=run_id,
            artifact="raw_samples",
        )
        self.interval_path = paths.runtime_capacity_observation_path(
            run_id=run_id,
            artifact="intervals",
        )
        self.run_path = paths.runtime_capacity_observation_path(
            run_id=run_id,
            artifact="run",
        )
        if not (
            self.raw_path.parent == self.interval_path.parent == self.run_path.parent
        ):
            raise ValueError("capacity artifact paths disagree")
        capacity_root = self.raw_path.parent.parent
        capacity_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if capacity_root.is_symlink() or not capacity_root.is_dir():
            raise ValueError("capacity artifact root is unsafe")
        self.raw_path.parent.mkdir(mode=0o700, exist_ok=False)
        _fsync_dir(capacity_root)
        directory_stat = self.raw_path.parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise ValueError("capacity run path is not a directory")
        os.chmod(self.raw_path.parent, 0o700, follow_symlinks=False)
        self._raw_fd = _open_new_private(self.raw_path)
        try:
            self._interval_fd = _open_new_private(self.interval_path)
        except Exception:
            os.close(self._raw_fd)
            raise
        self._raw_hash = hashlib.sha256()
        self._interval_hash = hashlib.sha256()
        self._raw_count = 0
        self._interval_count = 0
        self._raw_bytes = 0
        self._interval_bytes = 0
        self._closed = False

    def append_raw(self, sample: CapacityRawSample) -> None:
        payload = canonical_json_bytes(sample.model_dump(mode="json")) + b"\n"
        self._raw_count += 1
        self._raw_bytes += len(payload)
        if (
            len(payload) > MAX_RAW_RECORD_BYTES
            or self._raw_count > MAX_RAW_RECORDS
            or self._raw_bytes > MAX_RAW_FILE_BYTES
        ):
            raise ValueError("capacity raw sample stream exceeds its bound")
        _write_all(self._raw_fd, payload)
        self._raw_hash.update(payload)

    def append_interval(self, interval: CapacityObservationInterval) -> None:
        payload = canonical_json_bytes(interval.model_dump(mode="json")) + b"\n"
        self._interval_count += 1
        self._interval_bytes += len(payload)
        if (
            len(payload) > MAX_INTERVAL_RECORD_BYTES
            or self._interval_count > MAX_INTERVAL_RECORDS
            or self._interval_bytes > MAX_INTERVAL_FILE_BYTES
        ):
            raise ValueError("capacity interval stream exceeds its bound")
        _write_all(self._interval_fd, payload)
        self._interval_hash.update(payload)
        os.fsync(self._raw_fd)
        os.fsync(self._interval_fd)

    def close_streams(
        self,
        *,
        raw_chain_head: str | None,
        interval_chain_head: str | None,
    ) -> CapacityArtifactDigests:
        if self._closed:
            raise ValueError("capacity artifact streams are already closed")
        os.fsync(self._raw_fd)
        os.fsync(self._interval_fd)
        os.close(self._raw_fd)
        os.close(self._interval_fd)
        self._closed = True
        _fsync_dir(self.raw_path.parent)
        return CapacityArtifactDigests(
            raw_samples_sha256="sha256:" + self._raw_hash.hexdigest(),
            intervals_sha256="sha256:" + self._interval_hash.hexdigest(),
            raw_chain_head_sha256=raw_chain_head,
            interval_chain_head_sha256=interval_chain_head,
        )

    def write_run(self, run: CapacityObservationRun) -> None:
        if not self._closed:
            raise ValueError("capacity artifact streams must close before run receipt")
        payload = canonical_json_bytes(run.model_dump(mode="json")) + b"\n"
        if len(payload) > MAX_RUN_BYTES:
            raise ValueError("capacity run receipt exceeds its bound")
        descriptor = _open_new_private(self.run_path)
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_dir(self.run_path.parent)

    def abort(self) -> None:
        if self._closed:
            return
        for descriptor in (self._raw_fd, self._interval_fd):
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self._closed = True


def observer_source_sha256() -> str:
    """Bind observations to the exact source files that define their meaning."""

    modules = (
        aggregation_module,
        capacity_contract_module,
        capacity_runtime_identity_module,
        capacity_sources_module,
        gpu_freshness_module,
        host_module,
        mineru_identity_module,
        path_builder_module,
        runtime_package_module,
        settings_module,
    )
    digest = hashlib.sha256()
    for module in sorted(modules, key=lambda item: item.__name__):
        module_file = getattr(module, "__file__", None)
        if not isinstance(module_file, str):
            raise ValueError("capacity observer source file is unavailable")
        payload = Path(module_file).read_bytes()
        name = module.__name__.encode("utf-8")
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    own_payload = Path(__file__).read_bytes()
    own_name = __name__.encode("utf-8")
    digest.update(len(own_name).to_bytes(4, "big"))
    digest.update(own_name)
    digest.update(len(own_payload).to_bytes(8, "big"))
    digest.update(own_payload)
    cli_path = Path(__file__).resolve().parents[2] / "cli" / "capacity.py"
    cli_payload = cli_path.read_bytes()
    cli_name = b"disclosure_anchor.cli.capacity"
    digest.update(len(cli_name).to_bytes(4, "big"))
    digest.update(cli_name)
    digest.update(len(cli_payload).to_bytes(8, "big"))
    digest.update(cli_payload)
    return "sha256:" + digest.hexdigest()


def _canonical_run_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError("run_id is not a canonical UUID") from exc
    if str(parsed) != value:
        raise ValueError("run_id is not a canonical UUID")
    return value


def run_capacity_observation(
    *,
    settings: Settings,
    samplers: Sequence[CapacitySamplerPort],
    duration_seconds: float,
    interval_seconds: float = 60.0,
    run_id: str | None = None,
) -> CapacityObservationRun:
    """Run a passive observation; sampler failures degrade evidence only."""

    if not math.isfinite(duration_seconds) or not math.isfinite(interval_seconds):
        raise ValueError("capacity observation durations must be finite")
    canonical_duration = round(duration_seconds, 6)
    canonical_interval = round(interval_seconds, 6)
    if canonical_duration <= 0 or canonical_interval <= 0:
        raise ValueError("capacity observation durations must be positive")
    if (
        _expected_interval_count(
            duration=canonical_duration,
            interval=canonical_interval,
        )
        > MAX_INTERVAL_RECORDS
    ):
        raise ValueError("capacity observation interval count exceeds its bound")
    runtime_identity = settings.disclosure_mineru_runtime_bundle_identity_sha256
    if runtime_identity is None:
        raise ValueError("MinerU runtime bundle identity is required")
    sampler_by_source = {sampler.source: sampler for sampler in samplers}
    if set(sampler_by_source) != {"api", "gpu", "host", "vllm"}:
        raise ValueError("capacity observation requires exactly four samplers")
    if len(sampler_by_source) != len(samplers):
        raise ValueError("capacity observation sampler sources are duplicated")
    for source, sampler in sampler_by_source.items():
        expected_cadence = 5.0 if source == "host" else 1.0
        if sampler.cadence_seconds != expected_cadence:
            raise ValueError("capacity sampler cadence drifted")

    resolved_run_id = _canonical_run_id(run_id or str(uuid.uuid4()))
    source_sha256 = observer_source_sha256()
    writer = _CapacityArtifactWriter(
        paths=FileStorePathBuilder(settings),
        run_id=resolved_run_id,
    )
    raw_sequence = 0
    raw_chain_head: str | None = None
    interval_chain_head: str | None = None
    rolling_samples: list[CapacityRawSample] = []
    intervals: list[CapacityObservationInterval] = []
    source_counts: Counter[str] = Counter()
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            initial_host = _sample_one(sampler_by_source["host"])
            initial_results = sorted(
                [
                    initial_host,
                    *_sample_batch(
                        executor,
                        [
                            sampler_by_source[source]
                            for source in ("api", "gpu", "vllm")
                        ],
                    ),
                ],
                key=lambda result: result.source,
            )
            started_at = datetime.now(UTC)
            started_monotonic = time.monotonic()
            for result in initial_results:
                raw = _raw_sample(
                    result,
                    run_id=resolved_run_id,
                    sequence=raw_sequence,
                    previous_record_sha256=raw_chain_head,
                    runtime_identity=runtime_identity,
                    observer_source=source_sha256,
                    started_at=started_at,
                    offset_seconds=0.0,
                )
                writer.append_raw(raw)
                rolling_samples.append(raw)
                source_counts[raw.source] += 1
                raw_chain_head = raw.record_sha256
                raw_sequence += 1

            next_due = {
                source: sampler.cadence_seconds
                for source, sampler in sampler_by_source.items()
            }
            dispatch_cutoff = {
                source: max(0.0, canonical_duration - sampler.cadence_seconds)
                for source, sampler in sampler_by_source.items()
            }
            next_interval_end = min(canonical_interval, canonical_duration)
            while True:
                elapsed = time.monotonic() - started_monotonic
                if elapsed >= canonical_duration:
                    break
                eligible_due = [
                    due
                    for source, due in next_due.items()
                    if due <= dispatch_cutoff[source] + 1e-9
                    and elapsed <= dispatch_cutoff[source] + 1e-9
                ]
                due_at = min(eligible_due) if eligible_due else canonical_duration
                if due_at > elapsed:
                    time.sleep(due_at - elapsed)
                elapsed_before = time.monotonic() - started_monotonic
                due_sources = [
                    source
                    for source, due in sorted(next_due.items())
                    if due <= elapsed_before + 1e-6
                    and due <= dispatch_cutoff[source] + 1e-9
                    and elapsed_before <= dispatch_cutoff[source] + 1e-9
                ]
                if not due_sources:
                    continue
                results = _sample_batch(
                    executor,
                    [sampler_by_source[source] for source in due_sources],
                )
                batch_elapsed = min(
                    canonical_duration,
                    time.monotonic() - started_monotonic,
                )
                for result in results:
                    actual_finished_offset = max(
                        0.0,
                        result.finished_monotonic_seconds - started_monotonic,
                    )
                    if actual_finished_offset > canonical_duration:
                        result = _SampleResult(
                            source=result.source,
                            duration_seconds=result.duration_seconds,
                            finished_monotonic_seconds=(
                                result.finished_monotonic_seconds
                            ),
                            values=None,
                            reason_code="sample_completed_after_deadline",
                            underlying_reason_code=result.reason_code,
                        )
                        observed_offset = canonical_duration
                    else:
                        observed_offset = actual_finished_offset
                    raw = _raw_sample(
                        result,
                        run_id=resolved_run_id,
                        sequence=raw_sequence,
                        previous_record_sha256=raw_chain_head,
                        runtime_identity=runtime_identity,
                        observer_source=source_sha256,
                        started_at=started_at,
                        offset_seconds=observed_offset,
                    )
                    writer.append_raw(raw)
                    rolling_samples.append(raw)
                    source_counts[raw.source] += 1
                    raw_chain_head = raw.record_sha256
                    raw_sequence += 1
                    next_due[raw.source] = (
                        batch_elapsed + sampler_by_source[raw.source].cadence_seconds
                    )
                while next_interval_end <= batch_elapsed + 1e-9:
                    interval = aggregate_capacity_interval(
                        rolling_samples,
                        run_id=resolved_run_id,
                        interval_index=len(intervals),
                        start_seconds=(
                            0.0
                            if not intervals
                            else intervals[-1].monotonic_end_seconds
                        ),
                        end_seconds=next_interval_end,
                        observed_at_utc=(
                            started_at + timedelta(seconds=next_interval_end)
                        ),
                        runtime_bundle_identity_sha256=runtime_identity,
                        observer_source_sha256=source_sha256,
                        previous_record_sha256=interval_chain_head,
                    )
                    writer.append_interval(interval)
                    intervals.append(interval)
                    interval_chain_head = interval.record_sha256
                    rolling_samples = _prune_samples(
                        rolling_samples,
                        boundary_seconds=next_interval_end,
                    )
                    if next_interval_end >= canonical_duration:
                        break
                    next_interval_end = min(
                        canonical_duration,
                        next_interval_end + canonical_interval,
                    )

        actual_duration = canonical_duration
        previous_end = intervals[-1].monotonic_end_seconds if intervals else 0.0
        while previous_end < actual_duration - 1e-9:
            final_interval_end = min(
                actual_duration,
                round(previous_end + canonical_interval, 6),
            )
            interval = aggregate_capacity_interval(
                rolling_samples,
                run_id=resolved_run_id,
                interval_index=len(intervals),
                start_seconds=previous_end,
                end_seconds=final_interval_end,
                observed_at_utc=(
                    started_at + timedelta(seconds=final_interval_end)
                ),
                runtime_bundle_identity_sha256=runtime_identity,
                observer_source_sha256=source_sha256,
                previous_record_sha256=interval_chain_head,
            )
            writer.append_interval(interval)
            intervals.append(interval)
            interval_chain_head = interval.record_sha256
            previous_end = final_interval_end
            rolling_samples = _prune_samples(
                rolling_samples,
                boundary_seconds=previous_end,
            )
        if not intervals:
            raise ValueError("capacity observation produced no interval")
        artifacts = writer.close_streams(
            raw_chain_head=raw_chain_head,
            interval_chain_head=interval_chain_head,
        )
        run = _build_run(
            run_id=resolved_run_id,
            runtime_identity=runtime_identity,
            observer_source=source_sha256,
            started_at=started_at,
            duration_seconds=actual_duration,
            interval_seconds=canonical_interval,
            raw_count=raw_sequence,
            source_counts=source_counts,
            intervals=intervals,
            artifacts=artifacts,
        )
        writer.write_run(run)
        return run
    except Exception:
        writer.abort()
        raise


def verify_capacity_observation(
    *,
    settings: Settings,
    run_id: str,
) -> CapacityVerificationResult:
    """Mechanically replay raw samples into exact interval and run evidence."""

    run_id = _canonical_run_id(run_id)
    paths = FileStorePathBuilder(settings)
    raw_path = paths.runtime_capacity_observation_path(
        run_id=run_id,
        artifact="raw_samples",
    )
    interval_path = paths.runtime_capacity_observation_path(
        run_id=run_id,
        artifact="intervals",
    )
    run_path = paths.runtime_capacity_observation_path(run_id=run_id, artifact="run")
    raw_payloads, raw_file_sha = _read_jsonl(
        raw_path,
        maximum_file_bytes=MAX_RAW_FILE_BYTES,
        maximum_record_bytes=MAX_RAW_RECORD_BYTES,
        maximum_records=MAX_RAW_RECORDS,
    )
    interval_payloads, interval_file_sha = _read_jsonl(
        interval_path,
        maximum_file_bytes=MAX_INTERVAL_FILE_BYTES,
        maximum_record_bytes=MAX_INTERVAL_RECORD_BYTES,
        maximum_records=MAX_INTERVAL_RECORDS,
    )
    run_payload = json.loads(_read_private_file(run_path, maximum_bytes=MAX_RUN_BYTES))
    run = CapacityObservationRun.model_validate(run_payload)
    _verify_applicability(settings, run)
    raw_samples = tuple(CapacityRawSample.model_validate(item) for item in raw_payloads)
    intervals = tuple(
        CapacityObservationInterval.model_validate(item) for item in interval_payloads
    )
    if not raw_samples or not intervals:
        raise ValueError("capacity observation evidence is empty")
    _verify_chain(raw_payloads, sequence_field="sequence")
    _verify_chain(interval_payloads, sequence_field="interval_index")
    _verify_identity(run, raw_samples=raw_samples, intervals=intervals)
    _verify_gpu_identity(settings, raw_samples=raw_samples)
    _verify_geometry(run, raw_samples=raw_samples, intervals=intervals)

    previous_interval_hash: str | None = None
    previous_end = 0.0
    recomputed: list[CapacityObservationInterval] = []
    for recorded in intervals:
        if abs(recorded.monotonic_start_seconds - previous_end) > 1e-6:
            raise ValueError("capacity intervals are not contiguous")
        rebuilt = aggregate_capacity_interval(
            raw_samples,
            run_id=run.run_id,
            interval_index=recorded.interval_index,
            start_seconds=recorded.monotonic_start_seconds,
            end_seconds=recorded.monotonic_end_seconds,
            observed_at_utc=recorded.observed_at_utc,
            runtime_bundle_identity_sha256=run.runtime_bundle_identity_sha256,
            observer_source_sha256=run.observer_source_sha256,
            previous_record_sha256=previous_interval_hash,
        )
        if canonical_json_bytes(rebuilt.model_dump(mode="json")) != canonical_json_bytes(
            recorded.model_dump(mode="json")
        ):
            raise ValueError("capacity interval differs from raw replay")
        recomputed.append(rebuilt)
        previous_interval_hash = rebuilt.record_sha256
        previous_end = rebuilt.monotonic_end_seconds
    if abs(previous_end - run.duration_seconds) > 1e-6:
        raise ValueError("capacity run duration differs from interval coverage")

    source_counts: Counter[str] = Counter(
        str(sample.source) for sample in raw_samples
    )
    expected_artifacts = CapacityArtifactDigests(
        raw_samples_sha256=raw_file_sha,
        intervals_sha256=interval_file_sha,
        raw_chain_head_sha256=raw_samples[-1].record_sha256,
        interval_chain_head_sha256=intervals[-1].record_sha256,
    )
    expected_run = _build_run(
        run_id=run.run_id,
        runtime_identity=run.runtime_bundle_identity_sha256,
        observer_source=run.observer_source_sha256,
        started_at=run.started_at_utc,
        duration_seconds=run.duration_seconds,
        interval_seconds=run.interval_seconds,
        raw_count=len(raw_samples),
        source_counts=source_counts,
        intervals=recomputed,
        artifacts=expected_artifacts,
    )
    if canonical_json_bytes(expected_run.model_dump(mode="json")) != canonical_json_bytes(
        run.model_dump(mode="json")
    ):
        raise ValueError("capacity run receipt differs from replay")
    return CapacityVerificationResult(
        run=run,
        raw_samples=raw_samples,
        intervals=intervals,
    )


def _sample_batch(
    executor: ThreadPoolExecutor,
    samplers: Sequence[CapacitySamplerPort],
) -> list[_SampleResult]:
    futures = {executor.submit(_sample_one, sampler): sampler for sampler in samplers}
    return sorted(
        (future.result() for future in futures),
        key=lambda result: result.source,
    )


def _sample_one(sampler: CapacitySamplerPort) -> _SampleResult:
    started = time.monotonic()
    try:
        values = sampler.sample()
    except RuntimeError:
        reason = "endpoint_unreachable" if sampler.source != "host" else "host_sample_failed"
        finished = time.monotonic()
        return _SampleResult(
            source=cast(CapacitySource, sampler.source),
            duration_seconds=finished - started,
            finished_monotonic_seconds=finished,
            values=None,
            reason_code=reason,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        reason = "contract_unsatisfied" if sampler.source != "host" else "host_sample_failed"
        finished = time.monotonic()
        return _SampleResult(
            source=cast(CapacitySource, sampler.source),
            duration_seconds=finished - started,
            finished_monotonic_seconds=finished,
            values=None,
            reason_code=reason,
        )
    except Exception:
        finished = time.monotonic()
        return _SampleResult(
            source=cast(CapacitySource, sampler.source),
            duration_seconds=finished - started,
            finished_monotonic_seconds=finished,
            values=None,
            reason_code="unexpected_sampler_failure",
        )
    finished = time.monotonic()
    return _SampleResult(
        source=cast(CapacitySource, sampler.source),
        duration_seconds=finished - started,
        finished_monotonic_seconds=finished,
        values=values,
        reason_code=None,
    )


def _raw_sample(
    result: _SampleResult,
    *,
    run_id: str,
    sequence: int,
    previous_record_sha256: str | None,
    runtime_identity: str,
    observer_source: str,
    started_at: datetime,
    offset_seconds: float,
) -> CapacityRawSample:
    rounded_offset = round(offset_seconds, 6)
    payload: dict[str, Any] = {
        "contract_version": "capacity_observation.raw_sample.v1",
        "run_id": run_id,
        "sequence": sequence,
        "previous_record_sha256": previous_record_sha256,
        "record_sha256": "sha256:" + "0" * 64,
        "runtime_bundle_identity_sha256": runtime_identity,
        "observer_source_sha256": observer_source,
        "observed_at_utc": (
            started_at + timedelta(seconds=rounded_offset)
        ).isoformat(),
        "monotonic_offset_seconds": rounded_offset,
        "sample_duration_seconds": round(result.duration_seconds, 6),
        "source": result.source,
        "status": "available" if result.values is not None else "unavailable",
        "values": (
            result.values.model_dump(mode="json")
            if result.values is not None
            else None
        ),
        "reason_code": result.reason_code,
        "underlying_reason_code": result.underlying_reason_code,
    }
    draft = CapacityRawSample.model_validate(payload)
    normalized = draft.model_dump(mode="json")
    normalized["record_sha256"] = None
    return CapacityRawSample.model_validate(chained_record_payload(normalized))


def _prune_samples(
    samples: Sequence[CapacityRawSample],
    *,
    boundary_seconds: float,
) -> list[CapacityRawSample]:
    retained = [
        sample for sample in samples if sample.monotonic_offset_seconds >= boundary_seconds
    ]
    for source in ("api", "gpu", "host", "vllm"):
        preceding = [
            sample
            for sample in samples
            if sample.source == source
            and sample.monotonic_offset_seconds < boundary_seconds
        ]
        if preceding:
            retained.append(preceding[-1])
    return sorted(retained, key=lambda item: item.sequence)


def _build_run(
    *,
    run_id: str,
    runtime_identity: str,
    observer_source: str,
    started_at: datetime,
    duration_seconds: float,
    interval_seconds: float,
    raw_count: int,
    source_counts: Counter[str],
    intervals: Sequence[CapacityObservationInterval],
    artifacts: CapacityArtifactDigests,
) -> CapacityObservationRun:
    rounded_duration = round(duration_seconds, 6)
    status_counts = Counter(interval.status for interval in intervals)
    safety = tuple(
        sorted(
            {
                violation
                for interval in intervals
                for violation in interval.safety_violations
            }
        )
    )
    status: IntervalStatus = (
        "unsafe"
        if status_counts["unsafe"]
        else "incomplete" if status_counts["incomplete"] else "complete"
    )
    return CapacityObservationRun(
        run_id=run_id,
        runtime_bundle_identity_sha256=runtime_identity,
        observer_source_sha256=observer_source,
        started_at_utc=started_at,
        finished_at_utc=started_at + timedelta(seconds=rounded_duration),
        duration_seconds=rounded_duration,
        interval_seconds=round(interval_seconds, 6),
        status=status,
        raw_sample_count=raw_count,
        interval_count=len(intervals),
        complete_interval_count=status_counts["complete"],
        incomplete_interval_count=status_counts["incomplete"],
        unsafe_interval_count=status_counts["unsafe"],
        source_sample_counts=CapacitySourceCounts(
            api=source_counts["api"],
            gpu=source_counts["gpu"],
            host=source_counts["host"],
            vllm=source_counts["vllm"],
        ),
        safety_violations=cast(tuple[SafetyViolation, ...], safety),
        artifacts=artifacts,
        activation_authorized=False,
    )


def _verify_identity(
    run: CapacityObservationRun,
    *,
    raw_samples: Sequence[CapacityRawSample],
    intervals: Sequence[CapacityObservationInterval],
) -> None:
    if any(
        sample.run_id != run.run_id
        or sample.runtime_bundle_identity_sha256
        != run.runtime_bundle_identity_sha256
        or sample.observer_source_sha256 != run.observer_source_sha256
        for sample in raw_samples
    ) or any(
        interval.run_id != run.run_id
        or interval.runtime_bundle_identity_sha256
        != run.runtime_bundle_identity_sha256
        or interval.observer_source_sha256 != run.observer_source_sha256
        for interval in intervals
    ):
        raise ValueError("capacity observation identity drifted")


def _verify_applicability(settings: Settings, run: CapacityObservationRun) -> None:
    configured_identity = settings.disclosure_mineru_runtime_bundle_identity_sha256
    if configured_identity is None or run.runtime_bundle_identity_sha256 != configured_identity:
        raise ValueError("capacity receipt is not for the configured runtime identity")
    if run.observer_source_sha256 != observer_source_sha256():
        raise ValueError("capacity receipt is not from the exact-current observer source")


def _verify_gpu_identity(
    settings: Settings,
    *,
    raw_samples: Sequence[CapacityRawSample],
) -> None:
    expected_uuid = settings.disclosure_gpu_expected_uuid
    if expected_uuid is None:
        raise ValueError("configured GPU UUID is required for capacity replay")
    expected_digest = capacity_sources_module.gpu_device_identity_sha256(expected_uuid)
    for sample in raw_samples:
        if sample.source != "gpu" or sample.status != "available":
            continue
        if (
            not isinstance(sample.values, GpuSampleValues)
            or sample.values.device_identity_sha256 != expected_digest
        ):
            raise ValueError("capacity GPU identity drifted from configured UUID")


def _expected_interval_count(*, duration: float, interval: float) -> int:
    ratio = duration / interval
    nearest = round(ratio)
    if abs(ratio - nearest) <= 1e-9:
        return max(1, int(nearest))
    return max(1, math.ceil(ratio))


def _verify_geometry(
    run: CapacityObservationRun,
    *,
    raw_samples: Sequence[CapacityRawSample],
    intervals: Sequence[CapacityObservationInterval],
) -> None:
    expected_count = _expected_interval_count(
        duration=run.duration_seconds,
        interval=run.interval_seconds,
    )
    if len(intervals) != expected_count:
        raise ValueError("capacity interval count differs from run geometry")
    for index, interval in enumerate(intervals):
        expected_start = round(index * run.interval_seconds, 6)
        expected_end = round(
            min(run.duration_seconds, (index + 1) * run.interval_seconds),
            6,
        )
        expected_observed_at = run.started_at_utc + timedelta(seconds=expected_end)
        if (
            interval.interval_index != index
            or abs(interval.monotonic_start_seconds - expected_start) > 1e-6
            or abs(interval.monotonic_end_seconds - expected_end) > 1e-6
            or interval.observed_at_utc != expected_observed_at
        ):
            raise ValueError("capacity interval geometry is not mechanically derived")
    for sample in raw_samples:
        if sample.monotonic_offset_seconds > run.duration_seconds:
            raise ValueError("capacity raw sample is outside the run boundary")
        expected_observed_at = run.started_at_utc + timedelta(
            seconds=sample.monotonic_offset_seconds
        )
        if sample.observed_at_utc != expected_observed_at:
            raise ValueError("capacity raw sample UTC differs from monotonic offset")


def _verify_chain(payloads: Sequence[dict[str, Any]], *, sequence_field: str) -> None:
    previous: str | None = None
    for expected_sequence, payload in enumerate(payloads):
        if payload.get(sequence_field) != expected_sequence:
            raise ValueError("capacity observation sequence is not contiguous")
        if payload.get("previous_record_sha256") != previous:
            raise ValueError("capacity observation hash chain is broken")
        verify_chained_record(payload)
        previous = cast(str, payload["record_sha256"])


def _read_jsonl(
    path: Path,
    *,
    maximum_file_bytes: int,
    maximum_record_bytes: int,
    maximum_records: int,
) -> tuple[list[dict[str, Any]], str]:
    payload = _read_private_file(path, maximum_bytes=maximum_file_bytes)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    if payload and not payload.endswith(b"\n"):
        raise ValueError("capacity JSONL is not newline terminated")
    rows: list[dict[str, Any]] = []
    for line in payload.splitlines():
        if len(line) > maximum_record_bytes:
            raise ValueError("capacity JSONL record exceeds its bound")
        decoded = json.loads(line)
        if not isinstance(decoded, dict):
            raise ValueError("capacity JSONL record is not an object")
        rows.append(decoded)
        if len(rows) > maximum_records:
            raise ValueError("capacity JSONL record count exceeds its bound")
    return rows, digest


def _read_private_file(path: Path, *, maximum_bytes: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_size > maximum_bytes
        ):
            raise ValueError("capacity artifact is not a bounded private file")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError("capacity artifact changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("capacity artifact changed while being read")
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(payload) > maximum_bytes:
        raise ValueError("capacity artifact exceeds its bound")
    return payload


def _open_new_private(path: Path) -> int:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise ValueError("capacity artifact file creation is unsafe")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("capacity artifact write made no progress")
        view = view[written:]


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "CapacityVerificationResult",
    "observer_source_sha256",
    "run_capacity_observation",
    "verify_capacity_observation",
]
