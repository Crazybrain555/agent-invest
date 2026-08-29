"""Filesystem, replay and failure-isolation tests for Observation v1."""

from __future__ import annotations

from datetime import timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import cast
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import capacity_observer as observer_module
from disclosure_anchor.adapters.runtime.capacity_observer import (
    run_capacity_observation,
    verify_capacity_observation,
)
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.application.contracts.capacity import (
    ApiSampleValues,
    CapacityArtifactDigests,
    CapacitySource,
    GpuSampleValues,
    HostSampleValues,
    RawSampleValues,
    VllmSampleValues,
    canonical_json_bytes,
    chained_record_payload,
)
from disclosure_anchor.application.services.capacity_aggregation import (
    aggregate_capacity_interval,
)
from disclosure_anchor.settings import Settings


RUN_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
GPU_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
GPU_IDENTITY = "sha256:" + hashlib.sha256(GPU_UUID.encode("ascii")).hexdigest()


class _Sampler:
    def __init__(self, source: str, values: object, *, fail: bool = False) -> None:
        self.source = source
        self.cadence_seconds = 5.0 if source == "host" else 1.0
        self._values = values
        self._fail = fail

    def sample(self) -> object:
        if self._fail:
            raise RuntimeError("private endpoint detail must not be persisted")
        return self._values


def _settings(root: Path) -> Settings:
    data = root / "data"
    shared = root / "shared"
    return Settings(
        disclosure_data_root=data,
        disclosure_shared_root=shared,
        disclosure_runtime_root=root / "runtime",
        mineru_model_cache=shared / "mineru",
        hf_home=shared / "hf",
        modelscope_cache=shared / "modelscope",
        disclosure_mineru_runtime_bundle_identity_sha256="sha256:" + "1" * 64,
        disclosure_gpu_expected_uuid=GPU_UUID,
    )


def _samplers(*, fail_gpu: bool = False) -> tuple[_Sampler, ...]:
    return (
        _Sampler(
            "api",
            ApiSampleValues(
                queued_tasks=0,
                processing_tasks=1,
                completed_tasks_gauge=2,
                failed_tasks_gauge=0,
                task_slots=1,
                max_pending_tasks_requested=1,
                max_pending_tasks_effective=1,
                processing_window_size=16,
                task_retention_seconds=600,
                task_cleanup_interval_seconds=30,
                protocol_version=2,
            ),
        ),
        _Sampler(
            "gpu",
            GpuSampleValues(
                exporter_family="nvidia_smi",
                device_count=1,
                device_identity_sha256=GPU_IDENTITY,
                gpu_utilization_pct=88,
                framebuffer_used_bytes=10_000,
                framebuffer_free_bytes=6_000,
                framebuffer_total_bytes=16_000,
                power_usage_watts=240,
                temperature_celsius=62,
            ),
            fail=fail_gpu,
        ),
        _Sampler(
            "host",
            HostSampleValues(
                collector_sha256="sha256:" + "3" * 64,
                windows_node_identity_sha256="sha256:" + "4" * 64,
                container_epoch_sha256="sha256:" + "5" * 64,
                container_count=3,
                restart_count_total=0,
                oom_killed_count=0,
                unsafe_container_count=0,
                cgroup_oom_total=0,
                cgroup_oom_kill_total=0,
                cgroup_high_total=0,
                docker_vm_memory_total_bytes=16_000,
                docker_vm_memory_available_bytes=8_000,
                docker_memory_reserve_bytes=4_000,
                api_pid1_rss_bytes=2_000,
                api_pid1_rss_hwm_bytes=3_000,
            ),
        ),
        _Sampler(
            "vllm",
            VllmSampleValues(
                requests_running=7,
                requests_waiting=0,
                preemptions_total=0,
                kv_cache_usage_ratio=0.1,
            ),
        ),
    )


def _artifact_paths(settings: Settings, run_id: str) -> tuple[Path, Path, Path]:
    paths = FileStorePathBuilder(settings)
    return tuple(
        paths.runtime_capacity_observation_path(run_id=run_id, artifact=artifact)
        for artifact in ("raw_samples", "intervals", "run")
    )  # type: ignore[return-value]


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> bytes:
    payload = b"".join(canonical_json_bytes(row) + b"\n" for row in rows)
    path.write_bytes(payload)
    path.chmod(0o600)
    return payload


def _rehash_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    rebuilt: list[dict[str, object]] = []
    previous: str | None = None
    for row in rows:
        unhashed = dict(row)
        unhashed["previous_record_sha256"] = previous
        unhashed["record_sha256"] = None
        chained = chained_record_payload(unhashed)
        rebuilt.append(chained)
        previous = str(chained["record_sha256"])
    return rebuilt


def _rewrite_run_artifacts(
    *,
    run: object,
    raw_path: Path,
    interval_path: Path,
    run_path: Path,
) -> None:
    assert hasattr(run, "model_copy")
    raw_payload = raw_path.read_bytes()
    interval_payload = interval_path.read_bytes()
    raw_rows = [json.loads(line) for line in raw_payload.splitlines()]
    interval_rows = [json.loads(line) for line in interval_payload.splitlines()]
    artifacts = CapacityArtifactDigests(
        raw_samples_sha256="sha256:" + hashlib.sha256(raw_payload).hexdigest(),
        intervals_sha256="sha256:" + hashlib.sha256(interval_payload).hexdigest(),
        raw_chain_head_sha256=raw_rows[-1]["record_sha256"],
        interval_chain_head_sha256=interval_rows[-1]["record_sha256"],
    )
    updated = run.model_copy(update={"artifacts": artifacts})
    run_path.write_bytes(canonical_json_bytes(updated.model_dump(mode="json")))
    run_path.chmod(0o600)


class CapacityObserverTests(unittest.TestCase):
    def test_import_does_not_load_database_runtime(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "import disclosure_anchor.adapters.runtime.capacity_observer; "
                    "assert not any(name == 'sqlalchemy' or "
                    "name.startswith('sqlalchemy.') for name in sys.modules); "
                    "assert not any(name.startswith("
                    "'disclosure_anchor.adapters.db') for name in sys.modules)"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_observe_writes_private_new_only_evidence_and_replays_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            run = run_capacity_observation(
                settings=settings,
                samplers=_samplers(),  # type: ignore[arg-type]
                duration_seconds=0.02,
                interval_seconds=0.02,
                run_id=RUN_ID,
            )
            verified = verify_capacity_observation(settings=settings, run_id=RUN_ID)

            self.assertEqual(run, verified.run)
            self.assertEqual(run.status, "complete")
            self.assertFalse(run.activation_authorized)
            paths = FileStorePathBuilder(settings)
            for artifact in ("raw_samples", "intervals", "run"):
                path = paths.runtime_capacity_observation_path(
                    run_id=RUN_ID,
                    artifact=artifact,
                )
                metadata = path.stat(follow_symlinks=False)
                self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
                self.assertEqual(metadata.st_nlink, 1)
            encoded = paths.runtime_capacity_observation_path(
                run_id=RUN_ID,
                artifact="raw_samples",
            ).read_text(encoding="utf-8")
            for forbidden in (
                "document_id",
                "security_code",
                "company_name",
                "source_url",
                "task_id",
                "private endpoint detail",
            ):
                self.assertNotIn(forbidden, encoded)

            with self.assertRaises(FileExistsError):
                run_capacity_observation(
                    settings=settings,
                    samplers=_samplers(),  # type: ignore[arg-type]
                    duration_seconds=0.01,
                    interval_seconds=0.01,
                    run_id=RUN_ID,
                )

    def test_scheduler_records_completion_deadline_and_exact_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            sampler_values = {
                sampler.source: cast(RawSampleValues, sampler._values)  # noqa: SLF001
                for sampler in _samplers()
            }
            clock = [100.0]

            def monotonic() -> float:
                return clock[0]

            def sleep(seconds: float) -> None:
                clock[0] += seconds

            def result(
                source: CapacitySource,
                finished: float,
                *,
                reason_code: str | None = None,
            ) -> observer_module._SampleResult:  # noqa: SLF001
                return observer_module._SampleResult(  # noqa: SLF001
                    source=source,
                    duration_seconds=max(0.0, finished - 101.0),
                    finished_monotonic_seconds=finished,
                    values=None if reason_code is not None else sampler_values[source],
                    reason_code=reason_code,
                )

            initial_host = result("host", 100.0)
            initial_fast = [
                result("api", 100.0),
                result("gpu", 100.0),
                result("vllm", 100.0),
            ]
            periodic = [
                result("api", 102.4, reason_code="endpoint_unreachable"),
                result("gpu", 102.5),
                result("vllm", 101.4),
            ]

            batch_calls = [0]

            def sample_batch(
                *args: object,
                **kwargs: object,
            ) -> list[observer_module._SampleResult]:  # noqa: SLF001
                del args, kwargs
                if batch_calls[0] == 0:
                    batch_calls[0] += 1
                    return initial_fast
                clock[0] = 102.5
                return periodic
            with patch.object(
                observer_module,
                "_sample_one",
                return_value=initial_host,
            ), patch.object(
                observer_module,
                "_sample_batch",
                side_effect=sample_batch,
            ), patch.object(
                observer_module.time,
                "monotonic",
                side_effect=monotonic,
            ), patch.object(observer_module.time, "sleep", side_effect=sleep):
                run = run_capacity_observation(
                    settings=settings,
                    samplers=_samplers(),  # type: ignore[arg-type]
                    duration_seconds=2.0,
                    interval_seconds=1.0,
                )

            verified = verify_capacity_observation(
                settings=settings,
                run_id=run.run_id,
            )
            self.assertEqual(run.interval_count, 2)
            periodic_offsets = {
                sample.source: sample.monotonic_offset_seconds
                for sample in verified.raw_samples
                if sample.monotonic_offset_seconds > 0
            }
            self.assertEqual(
                periodic_offsets,
                {"api": 2.0, "gpu": 2.0, "vllm": 1.4},
            )
            deadline_gpu = next(
                sample
                for sample in verified.raw_samples
                if sample.source == "gpu" and sample.monotonic_offset_seconds > 0
            )
            self.assertEqual(deadline_gpu.status, "unavailable")
            self.assertEqual(
                deadline_gpu.reason_code,
                "sample_completed_after_deadline",
            )
            self.assertIsNone(deadline_gpu.underlying_reason_code)
            deadline_api = next(
                sample
                for sample in verified.raw_samples
                if sample.source == "api" and sample.monotonic_offset_seconds > 0
            )
            self.assertEqual(deadline_api.status, "unavailable")
            self.assertEqual(
                deadline_api.reason_code,
                "sample_completed_after_deadline",
            )
            self.assertEqual(
                deadline_api.underlying_reason_code,
                "endpoint_unreachable",
            )

    def test_scheduler_quiesces_each_source_one_cadence_before_deadline(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            sampler_values = {
                sampler.source: cast(RawSampleValues, sampler._values)  # noqa: SLF001
                for sampler in _samplers()
            }
            clock = [100.0]

            def monotonic() -> float:
                return clock[0]

            def sleep(seconds: float) -> None:
                clock[0] += seconds

            def result(
                source: CapacitySource,
                finished: float,
            ) -> observer_module._SampleResult:  # noqa: SLF001
                return observer_module._SampleResult(  # noqa: SLF001
                    source=source,
                    duration_seconds=0.1,
                    finished_monotonic_seconds=finished,
                    values=sampler_values[source],
                    reason_code=None,
                )

            batch_calls = [0]
            periodic_sources: list[tuple[str, ...]] = []

            def sample_batch(
                *args: object,
                **kwargs: object,
            ) -> list[observer_module._SampleResult]:  # noqa: SLF001
                del kwargs
                batch_calls[0] += 1
                samplers = cast(list[_Sampler], args[1])
                sources = tuple(sampler.source for sampler in samplers)
                if batch_calls[0] == 1:
                    return [
                        result("api", 100.0),
                        result("gpu", 100.0),
                        result("vllm", 100.0),
                    ]
                periodic_sources.append(sources)
                if "host" in sources:
                    raise AssertionError("expired host due must not be dispatched")
                if batch_calls[0] == 2:
                    clock[0] = 106.0
                else:
                    clock[0] += 0.1
                return [
                    result(cast(CapacitySource, source), clock[0])
                    for source in sources
                ]

            with patch.object(
                observer_module,
                "_sample_one",
                return_value=result("host", 100.0),
            ), patch.object(
                observer_module,
                "_sample_batch",
                side_effect=sample_batch,
            ), patch.object(
                observer_module.time,
                "monotonic",
                side_effect=monotonic,
            ), patch.object(observer_module.time, "sleep", side_effect=sleep):
                run = run_capacity_observation(
                    settings=settings,
                    samplers=_samplers(),  # type: ignore[arg-type]
                    duration_seconds=10.0,
                    interval_seconds=5.0,
                )

            verified = verify_capacity_observation(
                settings=settings,
                run_id=run.run_id,
            )
            self.assertGreaterEqual(batch_calls[0], 3)
            self.assertEqual(run.interval_count, 2)
            self.assertTrue(periodic_sources)
            self.assertTrue(
                all("host" not in sources for sources in periodic_sources)
            )
            self.assertEqual(
                [
                    sample.monotonic_offset_seconds
                    for sample in verified.raw_samples
                    if sample.source == "host"
                ],
                [0.0],
            )

    def test_sampler_failure_invalidates_evidence_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            run = run_capacity_observation(
                settings=settings,
                samplers=_samplers(fail_gpu=True),  # type: ignore[arg-type]
                duration_seconds=0.01,
                interval_seconds=0.01,
            )

            self.assertEqual(run.status, "incomplete")
            self.assertEqual(run.incomplete_interval_count, 1)

    def test_invalid_run_id_does_not_create_partial_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            with self.assertRaisesRegex(ValueError, "canonical UUID"):
                run_capacity_observation(
                    settings=settings,
                    samplers=_samplers(),  # type: ignore[arg-type]
                    duration_seconds=0.01,
                    interval_seconds=0.01,
                    run_id="not-a-uuid",
                )
            self.assertFalse(
                (settings.disclosure_runtime_root / "reports" / "capacity").exists()
            )

    def test_verify_rejects_old_runtime_and_old_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            run = run_capacity_observation(
                settings=settings,
                samplers=_samplers(),  # type: ignore[arg-type]
                duration_seconds=0.01,
                interval_seconds=0.01,
            )
            stale_settings = settings.model_copy(
                update={
                    "disclosure_mineru_runtime_bundle_identity_sha256": (
                        "sha256:" + "9" * 64
                    )
                }
            )
            with self.assertRaisesRegex(ValueError, "configured runtime identity"):
                verify_capacity_observation(
                    settings=stale_settings,
                    run_id=run.run_id,
                )
            with patch.object(
                observer_module,
                "observer_source_sha256",
                return_value="sha256:" + "8" * 64,
            ), self.assertRaisesRegex(ValueError, "exact-current observer source"):
                verify_capacity_observation(settings=settings, run_id=run.run_id)
            stale_gpu_settings = settings.model_copy(
                update={
                    "disclosure_gpu_expected_uuid": (
                        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
                    )
                }
            )
            with self.assertRaisesRegex(ValueError, "GPU identity drifted"):
                verify_capacity_observation(
                    settings=stale_gpu_settings,
                    run_id=run.run_id,
                )

    def test_verify_rejects_interval_geometry_not_derived_from_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            run = run_capacity_observation(
                settings=settings,
                samplers=_samplers(),  # type: ignore[arg-type]
                duration_seconds=0.02,
                interval_seconds=0.02,
            )
            verified = verify_capacity_observation(settings=settings, run_id=run.run_id)
            first = aggregate_capacity_interval(
                verified.raw_samples,
                run_id=run.run_id,
                interval_index=0,
                start_seconds=0.0,
                end_seconds=0.01,
                observed_at_utc=run.started_at_utc + timedelta(seconds=0.01),
                runtime_bundle_identity_sha256=run.runtime_bundle_identity_sha256,
                observer_source_sha256=run.observer_source_sha256,
                previous_record_sha256=None,
            )
            second = aggregate_capacity_interval(
                verified.raw_samples,
                run_id=run.run_id,
                interval_index=1,
                start_seconds=0.01,
                end_seconds=0.02,
                observed_at_utc=run.started_at_utc + timedelta(seconds=0.02),
                runtime_bundle_identity_sha256=run.runtime_bundle_identity_sha256,
                observer_source_sha256=run.observer_source_sha256,
                previous_record_sha256=first.record_sha256,
            )
            raw_path, interval_path, run_path = _artifact_paths(settings, run.run_id)
            interval_payload = _write_jsonl(
                interval_path,
                [
                    first.model_dump(mode="json"),
                    second.model_dump(mode="json"),
                ],
            )
            artifacts = CapacityArtifactDigests(
                raw_samples_sha256=(
                    "sha256:" + hashlib.sha256(raw_path.read_bytes()).hexdigest()
                ),
                intervals_sha256=(
                    "sha256:" + hashlib.sha256(interval_payload).hexdigest()
                ),
                raw_chain_head_sha256=verified.raw_samples[-1].record_sha256,
                interval_chain_head_sha256=second.record_sha256,
            )
            statuses = [first.status, second.status]
            malicious_run = run.model_dump(mode="json")
            malicious_run.update(
                {
                    "interval_count": 2,
                    "complete_interval_count": statuses.count("complete"),
                    "incomplete_interval_count": statuses.count("incomplete"),
                    "unsafe_interval_count": statuses.count("unsafe"),
                    "status": (
                        "unsafe"
                        if "unsafe" in statuses
                        else "incomplete" if "incomplete" in statuses else "complete"
                    ),
                    "safety_violations": sorted(
                        {
                            *first.safety_violations,
                            *second.safety_violations,
                        }
                    ),
                    "artifacts": artifacts.model_dump(mode="json"),
                }
            )
            run_path.write_bytes(
                canonical_json_bytes(malicious_run)
            )
            run_path.chmod(0o600)

            with self.assertRaisesRegex(ValueError, "interval count"):
                verify_capacity_observation(settings=settings, run_id=run.run_id)

    def test_verify_rejects_raw_utc_and_run_boundary_drift(self) -> None:
        for drift in ("utc", "boundary", "timezone"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                settings = _settings(Path(tmp))
                run = run_capacity_observation(
                    settings=settings,
                    samplers=_samplers(),  # type: ignore[arg-type]
                    duration_seconds=0.01,
                    interval_seconds=0.01,
                )
                raw_path, interval_path, run_path = _artifact_paths(
                    settings,
                    run.run_id,
                )
                rows = [json.loads(line) for line in raw_path.read_bytes().splitlines()]
                if drift == "utc":
                    rows[0]["observed_at_utc"] = (
                        run.started_at_utc + timedelta(microseconds=1)
                    ).isoformat()
                    expected = "UTC differs"
                elif drift == "boundary":
                    offset = run.duration_seconds + 0.001
                    rows[0]["monotonic_offset_seconds"] = offset
                    rows[0]["observed_at_utc"] = (
                        run.started_at_utc + timedelta(seconds=offset)
                    ).isoformat()
                    expected = "outside the run boundary"
                else:
                    rows[0]["observed_at_utc"] = run.started_at_utc.astimezone(
                        timezone(timedelta(hours=8))
                    ).isoformat()
                    expected = "must use UTC offset"
                _write_jsonl(raw_path, _rehash_rows(rows))
                _rewrite_run_artifacts(
                    run=run,
                    raw_path=raw_path,
                    interval_path=interval_path,
                    run_path=run_path,
                )

                with self.assertRaisesRegex(ValueError, expected):
                    verify_capacity_observation(settings=settings, run_id=run.run_id)

    def test_verify_rejects_interval_utc_drift_with_valid_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            run = run_capacity_observation(
                settings=settings,
                samplers=_samplers(),  # type: ignore[arg-type]
                duration_seconds=0.01,
                interval_seconds=0.01,
            )
            raw_path, interval_path, run_path = _artifact_paths(settings, run.run_id)
            rows = [json.loads(line) for line in interval_path.read_bytes().splitlines()]
            rows[0]["observed_at_utc"] = (
                run.finished_at_utc + timedelta(microseconds=1)
            ).isoformat()
            _write_jsonl(interval_path, _rehash_rows(rows))
            _rewrite_run_artifacts(
                run=run,
                raw_path=raw_path,
                interval_path=interval_path,
                run_path=run_path,
            )

            with self.assertRaisesRegex(ValueError, "interval geometry"):
                verify_capacity_observation(settings=settings, run_id=run.run_id)

    def test_replay_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            run_capacity_observation(
                settings=settings,
                samplers=_samplers(),  # type: ignore[arg-type]
                duration_seconds=0.01,
                interval_seconds=0.01,
                run_id=RUN_ID,
            )
            path = FileStorePathBuilder(settings).runtime_capacity_observation_path(
                run_id=RUN_ID,
                artifact="raw_samples",
            )
            payload = path.read_bytes()
            changed = payload.replace(b'"queued_tasks":0', b'"queued_tasks":9', 1)
            self.assertNotEqual(changed, payload)
            descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
            try:
                os.write(descriptor, changed)
            finally:
                os.close(descriptor)

            with self.assertRaises(ValueError):
                verify_capacity_observation(settings=settings, run_id=RUN_ID)


if __name__ == "__main__":
    unittest.main()
