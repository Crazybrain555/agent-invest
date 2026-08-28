"""Regressions for fail-closed MinerU A-B-B-A commissioning."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.mineru_capacity_commissioning import (
    evaluate_capacity_commissioning,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_WINDOWS_COLLECTOR_PATH,
)
from disclosure_anchor.adapters.runtime.mineru_phase_trace_capture import (
    MineruPhaseTraceCapture,
)


_COLLECTOR_SHA = "sha256:" + "a" * 64
_NODE_SHA = "sha256:" + "b" * 64
_LEGACY_SHA = "sha256:" + "c" * 64
_CANDIDATE_SHA = "sha256:" + "d" * 64
_IMAGE_SHA = "sha256:" + "e" * 64
_MEMORY_RESERVE_BYTES = 7 * 1024**3


def _cleanup() -> dict[str, object]:
    return {
        "temporary_tree_removed": True,
        "external_api_temp_dirs_after": 0,
        "api_temp_cleanup_errors": [],
        "external_mineru_processes_after": 0,
        "observation_error": None,
    }


def _api_health(*, completed: int, processing: int = 0) -> dict[str, object]:
    return {
        "status": "healthy",
        "version": "3.4.4",
        "protocol_version": 2,
        "queued_tasks": 0,
        "processing_tasks": processing,
        "completed_tasks": completed,
        "failed_tasks": 0,
        "max_concurrent_requests": 1,
        "processing_window_size": 16,
        "task_retention_seconds": 600,
        "task_cleanup_interval_seconds": 30,
    }


def _metrics(*, stage_elapsed: float) -> dict[str, object]:
    return {
        "sample_count": 1,
        "sampling_failures": [],
        "terminal_sample_observed_seconds": min(1.0, stage_elapsed),
        "observer": {
            "profile": "metrics-observer.v1",
            "state": "CLOSED",
            "observation_complete": True,
            "hard_failure": None,
            "transitions": [
                {
                    "from": "STARTING",
                    "to": "HEALTHY",
                    "reason": "valid_metrics_sample",
                    "observed_seconds": 0.0,
                },
                {
                    "from": "HEALTHY",
                    "to": "CLOSED",
                    "reason": "monitor_stopped",
                    "observed_seconds": min(1.0, stage_elapsed),
                },
            ],
        },
        "baseline": {
            "observed_seconds": 0.0,
            "running": 0,
            "waiting": 0,
            "preemptions": 0,
            "kv_cache": 0,
        },
        "range": {
            "running": {"min": 0, "max": 1},
            "waiting": {"min": 0, "max": 0},
            "preemptions": {"min": 0, "max": 0},
            "kv_cache": {"min": 0, "max": 0},
        },
        "percentiles": {
            "running_p95": 1,
            "waiting_p95": 0,
            "kv_cache_p95": 0,
        },
    }


def _orchestrator(*, count: int, completed_before: int) -> dict[str, object]:
    sample = {
        "observed_seconds": 0.25,
        "queued_tasks": 0,
        "processing_tasks": 1,
        "completed_tasks": completed_before,
        "failed_tasks": 0,
    }
    return {
        "task_registry_semantics": "retained-terminal-gauges.v1",
        "baseline": _api_health(completed=completed_before),
        "samples": [sample],
        "sample_count": 1,
        "sampling_failures": [],
        "observer": {
            "profile": "orchestrator-observer.v1",
            "state": "CLOSED",
            "observation_complete": True,
            "hard_failure": None,
            "admission_stop_reason": None,
            "transitions": [
                {
                    "from": "STARTING",
                    "to": "HEALTHY",
                    "reason": "valid_orchestrator_sample",
                    "observed_seconds": 0.25,
                },
                {
                    "from": "HEALTHY",
                    "to": "CLOSED",
                    "reason": "monitor_stopped",
                    "observed_seconds": 0.5,
                },
            ],
        },
        "terminal": _api_health(completed=completed_before + count),
        "terminal_active_tasks": 0,
        "preflight_drain_seconds": 0.0,
        "terminal_drain_seconds": 0.5,
        "stop_semantics": "drain-not-cancel.v1",
        "range": {
            "queued_tasks": {"min": 0, "max": 0},
            "processing_tasks": {"min": 0, "max": 1},
            "completed_tasks": {
                "min": completed_before,
                "max": completed_before + count,
            },
            "failed_tasks": {"min": 0, "max": 0},
        },
    }


def _container(name: str, container_id: str, started_at: str) -> dict[str, object]:
    return {
        "name": name,
        "id": container_id,
        "started_at_utc": started_at,
        "restart_count": 0,
        "oom_killed": False,
        "exit_code": 0,
        "running": True,
        "status": "running",
        "health": "healthy",
        "pid": 100,
        "memory_current_bytes": 1,
        "memory_max_bytes": 30_000_000_000,
        "memory_events": {"oom": 0, "oom_kill": 0},
        "pid1_rss_bytes": 1,
        "pid1_rss_hwm_bytes": 1,
        "docker_vm_memory_total_bytes": 30_000_000_000,
        "docker_vm_memory_available_bytes": 10_000_000_000,
    }


def _host(api_id: str, *, elapsed_seconds: float) -> dict[str, object]:
    containers = [
        _container("mineru-api", api_id, "2026-08-27T00:10:00+00:00"),
        _container("mineru-api-proxy", "1" * 64, "2026-08-27T00:00:00+00:00"),
        _container(
            "mineru-openai-server", "2" * 64, "2026-08-27T00:00:00+00:00"
        ),
    ]
    observed_seconds = [
        float(value) for value in range(0, int(elapsed_seconds) + 1, 5)
    ]
    if observed_seconds[-1] != elapsed_seconds:
        observed_seconds.append(elapsed_seconds)
    observed_base = datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc)
    samples = [
        {
            "schema": "mineru-host-capacity-sample.v1",
            "observed_at_utc": (
                observed_base + timedelta(seconds=observed)
            ).isoformat(),
            "observed_seconds": observed,
            "collector_path": MINERU_WINDOWS_COLLECTOR_PATH,
            "collector_sha256": _COLLECTOR_SHA,
            "windows_node_identity_sha256": _NODE_SHA,
            "containers": deepcopy(containers),
        }
        for observed in observed_seconds
    ]
    return {
        "schema": "mineru-host-capacity-evidence.v2",
        "status": "pass",
        "failure": None,
        "sample_interval_seconds": 5.0,
        "max_sample_gap_seconds": 15.0,
        "collector_path": MINERU_WINDOWS_COLLECTOR_PATH,
        "collector_sha256": _COLLECTOR_SHA,
        "windows_node_identity_sha256": _NODE_SHA,
        "docker_memory_reserve_bytes": _MEMORY_RESERVE_BYTES,
        "samples": samples,
        "violations": [],
        "sampling_failures": [],
        "summary": {
            "sample_count": len(samples),
            "max_api_pid1_rss_hwm_bytes": 1,
            "min_docker_vm_memory_available_bytes": 10_000_000_000,
        },
    }


def _documents(count: int) -> list[dict[str, object]]:
    return [
        {
            "status": "pass",
            "copy_index": index,
            "logical_name": f"doc-{index:02d}.pdf",
            "workload_class": ("regular", "heavy", "huge")[(index - 1) % 3],
            "input_sha256": "sha256:" + f"{index:064x}",
            "page_count": 10,
            "block_count": 20,
            "elapsed_seconds": 1.0,
            "provider_bundle_sha256": "sha256:" + f"{index + 100:064x}",
        }
        for index in range(1, count + 1)
    ]


def _receipt(
    *,
    execution_id: str,
    elapsed_seconds: float,
    runtime_identity: str,
    api_id: str,
    started_at: datetime,
) -> dict[str, object]:
    finished_at = started_at + timedelta(seconds=elapsed_seconds)
    stage_elapsed = elapsed_seconds / 4
    return {
        "schema": "mineru_staged_load_receipt.v6",
        "receipt_schema_version": 6,
        "execution_id": execution_id,
        "status": "pass",
        "failure": None,
        "database_access": "none",
        "queue_access": "none",
        "fixed_stage_document_counts": [4, 8, 16],
        "orchestrator_task_concurrency": 1,
        "orchestrator_inference_concurrency": 7,
        "effective_inference_request_upper_bound": 7,
        "safety_limits": {
            "profile": "whole-document-runaway-and-drain.v1",
            "document_runaway_timeout_seconds": 86400,
            "api_drain_timeout_seconds": 86400,
        },
        "started_at_utc": started_at.isoformat(),
        "finished_at_utc": finished_at.isoformat(),
        "elapsed_seconds": elapsed_seconds,
        "cleanup": _cleanup(),
        "host_capacity": _host(api_id, elapsed_seconds=elapsed_seconds),
        "input": {"profile": "frozen", "sha256": "sha256:" + "f" * 64},
        "topology": {"api_endpoint_sha256": "sha256:" + "9" * 64},
        "identity": {
            "local_client_identity_sha256": "sha256:" + "3" * 64,
            "local_content_package_versions": {"mineru": "3.4.4"},
            "local_processing_window_size": 16,
            "local_writer_code_sha256": "sha256:" + "4" * 64,
            "served_model_id": "pinned-model",
            "orchestrator_task_slots": 1,
            "runtime_manifest_identity_sha256": runtime_identity,
        },
        "stages": [
            {
                "status": "pass",
                "failure": None,
                "stage_document_count": count,
                "client_outstanding_window": 1,
                "peak_client_outstanding": 1,
                "admission_order_profile": "copy-index-fifo.v1",
                "admission_order_copy_indices": list(range(1, count + 1)),
                "admission": {
                    "profile": "copy-index-fifo.v1",
                    "expected_copy_indices": list(range(1, count + 1)),
                    "admission_order_copy_indices": list(range(1, count + 1)),
                    "records": [
                        {
                            "copy_index": index,
                            "admission_ordinal": index - 1,
                            "state": "completed",
                        }
                        for index in range(1, count + 1)
                    ],
                    "closed": True,
                    "abort_reason": None,
                },
                "selection_profile": "per_stage_regular_heavy_huge.v1",
                "orchestrator_task_concurrency": 1,
                "orchestrator_inference_concurrency": 7,
                "effective_inference_request_upper_bound": 7,
                "elapsed_seconds": stage_elapsed,
                "cleanup": _cleanup(),
                "metrics": _metrics(stage_elapsed=stage_elapsed),
                "orchestrator": _orchestrator(
                    count=count,
                    completed_before=sum((4, 8, 16)[:stage_index]),
                ),
                "documents": _documents(count),
            }
            for stage_index, count in enumerate((4, 8, 16))
        ],
    }


def _capture(
    *,
    mode: str,
    profile_sha: str,
    api_id: str,
    started_at: datetime,
    elapsed_seconds: float,
) -> MineruPhaseTraceCapture:
    finished_at = started_at + timedelta(seconds=elapsed_seconds)
    return MineruPhaseTraceCapture(
        active_profile_sha256=profile_sha,
        capacity_mode=mode,
        collected_at_utc=(finished_at + timedelta(seconds=10)).isoformat(),
        collector_path=MINERU_WINDOWS_COLLECTOR_PATH,
        collector_sha256=_COLLECTOR_SHA,
        container_id=api_id,
        container_image="agent-invest/mineru-api:test",
        container_image_id=_IMAGE_SHA,
        container_started_at_utc="2026-08-27T00:10:00+00:00",
        line_count=1,
        lines_sha256="sha256:" + "5" * 64,
        since_utc=(started_at - timedelta(seconds=5)).isoformat(),
        trace_bytes=1,
        until_utc=(finished_at + timedelta(seconds=5)).isoformat(),
        windows_node_identity_sha256=_NODE_SHA,
        events=(),
    )


class MineruCapacityCommissioningTests(unittest.TestCase):
    def _arms(self) -> list[tuple[dict[str, object], MineruPhaseTraceCapture]]:
        modes = ("legacy", "candidate", "candidate", "legacy")
        profiles = (_LEGACY_SHA, _CANDIDATE_SHA, _CANDIDATE_SHA, _LEGACY_SHA)
        elapsed = (100.0, 80.0, 82.0, 101.0)
        runtime = ("A", "B", "B", "A")
        arms = []
        cursor = datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc)
        for index, (mode, profile, duration, identity) in enumerate(
            zip(modes, profiles, elapsed, runtime, strict=True), start=1
        ):
            api_id = f"{index + 5:x}" * 64
            arms.append(
                (
                    _receipt(
                        execution_id=f"00000000-0000-0000-0000-{index:012d}",
                        elapsed_seconds=duration,
                        runtime_identity=identity,
                        api_id=api_id,
                        started_at=cursor,
                    ),
                    _capture(
                        mode=mode,
                        profile_sha=profile,
                        api_id=api_id,
                        started_at=cursor,
                        elapsed_seconds=duration,
                    ),
                )
            )
            cursor += timedelta(seconds=duration + 10)
        return arms

    @staticmethod
    def _retime(
        arms: list[tuple[dict[str, object], MineruPhaseTraceCapture]],
    ) -> None:
        cursor = datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc)
        for index, (receipt, capture) in enumerate(arms):
            duration = float(receipt["elapsed_seconds"])
            finished = cursor + timedelta(seconds=duration)
            receipt["started_at_utc"] = cursor.isoformat()
            receipt["finished_at_utc"] = finished.isoformat()
            receipt["host_capacity"] = _host(
                capture.container_id,
                elapsed_seconds=duration,
            )
            arms[index] = (
                receipt,
                replace(
                    capture,
                    since_utc=(cursor - timedelta(seconds=5)).isoformat(),
                    until_utc=(finished + timedelta(seconds=5)).isoformat(),
                ),
            )
            cursor = finished + timedelta(seconds=10)

    @staticmethod
    def _summary(capture, **_kwargs):
        return {
            "document_count": 28,
            "page_count": 280,
            "capacity_mode": capture.capacity_mode,
        }

    def test_both_candidate_arms_must_beat_both_bracketing_baselines(self) -> None:
        with patch(
            "disclosure_anchor.adapters.runtime.mineru_capacity_commissioning."
            "summarize_phase_trace_capture",
            side_effect=self._summary,
        ):
            result = evaluate_capacity_commissioning(
                self._arms(),
                expected_legacy_profile_sha256=_LEGACY_SHA,
                expected_candidate_profile_sha256=_CANDIDATE_SHA,
                expected_collector_sha256=_COLLECTOR_SHA,
                expected_collector_path=MINERU_WINDOWS_COLLECTOR_PATH,
                expected_docker_memory_reserve_bytes=_MEMORY_RESERVE_BYTES,
                expected_windows_node_identity_sha256=_NODE_SHA,
                minimum_improvement_basis_points=500,
                maximum_repeat_spread_basis_points=300,
            )
        self.assertEqual(result["decision"], "COMMISSION")
        self.assertTrue(result["profile_commissioning_authorized"])
        self.assertGreater(
            result["candidate_floor_pages_per_host_hour_milli"],
            result["baseline_ceiling_pages_per_host_hour_milli"],
        )

    def test_one_slow_candidate_arm_stops_commissioning(self) -> None:
        arms = self._arms()
        arms[2][0]["elapsed_seconds"] = 120.0
        self._retime(arms)
        with patch(
            "disclosure_anchor.adapters.runtime.mineru_capacity_commissioning."
            "summarize_phase_trace_capture",
            side_effect=self._summary,
        ):
            result = evaluate_capacity_commissioning(
                arms,
                expected_legacy_profile_sha256=_LEGACY_SHA,
                expected_candidate_profile_sha256=_CANDIDATE_SHA,
                expected_collector_sha256=_COLLECTOR_SHA,
                expected_collector_path=MINERU_WINDOWS_COLLECTOR_PATH,
                expected_docker_memory_reserve_bytes=_MEMORY_RESERVE_BYTES,
                expected_windows_node_identity_sha256=_NODE_SHA,
                minimum_improvement_basis_points=500,
                maximum_repeat_spread_basis_points=300,
            )
        self.assertEqual(result["decision"], "STOP")
        self.assertFalse(result["profile_commissioning_authorized"])

    def test_per_arm_provider_bundle_hash_may_vary(self) -> None:
        arms = self._arms()
        arms[1][0]["stages"][0]["documents"][0][
            "provider_bundle_sha256"
        ] = "sha256:" + "8" * 64
        with patch(
            "disclosure_anchor.adapters.runtime.mineru_capacity_commissioning."
            "summarize_phase_trace_capture",
            side_effect=self._summary,
        ):
            result = evaluate_capacity_commissioning(
                arms,
                expected_legacy_profile_sha256=_LEGACY_SHA,
                expected_candidate_profile_sha256=_CANDIDATE_SHA,
                expected_collector_sha256=_COLLECTOR_SHA,
                expected_collector_path=MINERU_WINDOWS_COLLECTOR_PATH,
                expected_docker_memory_reserve_bytes=_MEMORY_RESERVE_BYTES,
                expected_windows_node_identity_sha256=_NODE_SHA,
                minimum_improvement_basis_points=500,
                maximum_repeat_spread_basis_points=300,
            )
        self.assertEqual(result["decision"], "COMMISSION")

    def test_output_semantic_drift_fails_closed(self) -> None:
        arms = self._arms()
        arms[1][0]["stages"][0]["documents"][0]["block_count"] = 21
        with patch(
            "disclosure_anchor.adapters.runtime.mineru_capacity_commissioning."
            "summarize_phase_trace_capture",
            side_effect=self._summary,
        ):
            with self.assertRaisesRegex(ValueError, "output semantics"):
                evaluate_capacity_commissioning(
                    arms,
                    expected_legacy_profile_sha256=_LEGACY_SHA,
                    expected_candidate_profile_sha256=_CANDIDATE_SHA,
                    expected_collector_sha256=_COLLECTOR_SHA,
                    expected_collector_path=MINERU_WINDOWS_COLLECTOR_PATH,
                    expected_docker_memory_reserve_bytes=_MEMORY_RESERVE_BYTES,
                    expected_windows_node_identity_sha256=_NODE_SHA,
                    minimum_improvement_basis_points=500,
                    maximum_repeat_spread_basis_points=300,
                )

    def test_timeline_order_is_proved_not_inferred_from_argument_position(self) -> None:
        arms = self._arms()
        overlap_start = datetime.fromisoformat(
            str(arms[0][0]["finished_at_utc"])
        ) - timedelta(seconds=2)
        overlap_finish = overlap_start + timedelta(
            seconds=float(arms[1][0]["elapsed_seconds"])
        )
        arms[1][0]["started_at_utc"] = overlap_start.isoformat()
        arms[1][0]["finished_at_utc"] = overlap_finish.isoformat()
        arms[1] = (
            arms[1][0],
            replace(
                arms[1][1],
                since_utc=(overlap_start - timedelta(seconds=5)).isoformat(),
                until_utc=(overlap_finish + timedelta(seconds=5)).isoformat(),
            ),
        )
        with patch(
            "disclosure_anchor.adapters.runtime.mineru_capacity_commissioning."
            "summarize_phase_trace_capture",
            side_effect=self._summary,
        ):
            with self.assertRaisesRegex(ValueError, "chronological non-overlapping"):
                evaluate_capacity_commissioning(
                    arms,
                    expected_legacy_profile_sha256=_LEGACY_SHA,
                    expected_candidate_profile_sha256=_CANDIDATE_SHA,
                    expected_collector_sha256=_COLLECTOR_SHA,
                    expected_collector_path=MINERU_WINDOWS_COLLECTOR_PATH,
                    expected_docker_memory_reserve_bytes=_MEMORY_RESERVE_BYTES,
                    expected_windows_node_identity_sha256=_NODE_SHA,
                    minimum_improvement_basis_points=500,
                    maximum_repeat_spread_basis_points=300,
                )

    def test_reported_elapsed_cannot_create_false_candidate_gain(self) -> None:
        arms = self._arms()
        arms[0][0]["elapsed_seconds"] = 104.9
        with patch(
            "disclosure_anchor.adapters.runtime.mineru_capacity_commissioning."
            "summarize_phase_trace_capture",
            side_effect=self._summary,
        ):
            with self.assertRaisesRegex(ValueError, "timeline is inconsistent"):
                evaluate_capacity_commissioning(
                    arms,
                    expected_legacy_profile_sha256=_LEGACY_SHA,
                    expected_candidate_profile_sha256=_CANDIDATE_SHA,
                    expected_collector_sha256=_COLLECTOR_SHA,
                    expected_collector_path=MINERU_WINDOWS_COLLECTOR_PATH,
                    expected_docker_memory_reserve_bytes=_MEMORY_RESERVE_BYTES,
                    expected_windows_node_identity_sha256=_NODE_SHA,
                    minimum_improvement_basis_points=500,
                    maximum_repeat_spread_basis_points=300,
                )

    def test_timeline_tolerance_uses_utc_span_and_rejects_51ms(self) -> None:
        baseline_arms = self._arms()
        within_tolerance_arms = self._arms()
        within_tolerance_arms[0][0]["elapsed_seconds"] = 100.049
        outside_tolerance_arms = self._arms()
        outside_tolerance_arms[0][0]["elapsed_seconds"] = 100.051
        with patch(
            "disclosure_anchor.adapters.runtime.mineru_capacity_commissioning."
            "summarize_phase_trace_capture",
            side_effect=self._summary,
        ):
            baseline = evaluate_capacity_commissioning(
                baseline_arms,
                expected_legacy_profile_sha256=_LEGACY_SHA,
                expected_candidate_profile_sha256=_CANDIDATE_SHA,
                expected_collector_sha256=_COLLECTOR_SHA,
                expected_collector_path=MINERU_WINDOWS_COLLECTOR_PATH,
                expected_docker_memory_reserve_bytes=_MEMORY_RESERVE_BYTES,
                expected_windows_node_identity_sha256=_NODE_SHA,
                minimum_improvement_basis_points=500,
                maximum_repeat_spread_basis_points=300,
            )
            accepted = evaluate_capacity_commissioning(
                within_tolerance_arms,
                expected_legacy_profile_sha256=_LEGACY_SHA,
                expected_candidate_profile_sha256=_CANDIDATE_SHA,
                expected_collector_sha256=_COLLECTOR_SHA,
                expected_collector_path=MINERU_WINDOWS_COLLECTOR_PATH,
                expected_docker_memory_reserve_bytes=_MEMORY_RESERVE_BYTES,
                expected_windows_node_identity_sha256=_NODE_SHA,
                minimum_improvement_basis_points=500,
                maximum_repeat_spread_basis_points=300,
            )
            self.assertEqual(
                accepted["arm_pages_per_host_hour_milli"],
                baseline["arm_pages_per_host_hour_milli"],
            )
            with self.assertRaisesRegex(ValueError, "timeline is inconsistent"):
                evaluate_capacity_commissioning(
                    outside_tolerance_arms,
                    expected_legacy_profile_sha256=_LEGACY_SHA,
                    expected_candidate_profile_sha256=_CANDIDATE_SHA,
                    expected_collector_sha256=_COLLECTOR_SHA,
                    expected_collector_path=MINERU_WINDOWS_COLLECTOR_PATH,
                    expected_docker_memory_reserve_bytes=_MEMORY_RESERVE_BYTES,
                    expected_windows_node_identity_sha256=_NODE_SHA,
                    minimum_improvement_basis_points=500,
                    maximum_repeat_spread_basis_points=300,
                )

    def test_capture_collector_path_is_bound_again_by_evaluator(self) -> None:
        arms = self._arms()
        arms[0] = (
            arms[0][0],
            replace(
                arms[0][1],
                collector_path=r"C:\ProgramData\alternate-collector.ps1",
            ),
        )
        with patch(
            "disclosure_anchor.adapters.runtime.mineru_capacity_commissioning."
            "summarize_phase_trace_capture",
            side_effect=self._summary,
        ):
            with self.assertRaisesRegex(ValueError, "collector path drifted"):
                evaluate_capacity_commissioning(
                    arms,
                    expected_legacy_profile_sha256=_LEGACY_SHA,
                    expected_candidate_profile_sha256=_CANDIDATE_SHA,
                    expected_collector_sha256=_COLLECTOR_SHA,
                    expected_collector_path=MINERU_WINDOWS_COLLECTOR_PATH,
                    expected_docker_memory_reserve_bytes=_MEMORY_RESERVE_BYTES,
                    expected_windows_node_identity_sha256=_NODE_SHA,
                    minimum_improvement_basis_points=500,
                    maximum_repeat_spread_basis_points=300,
                )

    def test_host_hard_gate_recomputes_reserve_gap_summary_and_vm(self) -> None:
        def low_reserve(arms) -> None:
            arms[0][0]["host_capacity"]["docker_memory_reserve_bytes"] = 1

        def sample_gap(arms) -> None:
            host = arms[0][0]["host_capacity"]
            host["samples"] = [host["samples"][0], host["samples"][-1]]
            host["summary"]["sample_count"] = 2

        def summary_drift(arms) -> None:
            arms[0][0]["host_capacity"]["summary"]["sample_count"] += 1

        def vm_total_drift(arms) -> None:
            arms[0][0]["host_capacity"]["samples"][0]["containers"][0][
                "docker_vm_memory_total_bytes"
            ] -= 1

        with patch(
            "disclosure_anchor.adapters.runtime.mineru_capacity_commissioning."
            "summarize_phase_trace_capture",
            side_effect=self._summary,
        ):
            for label, mutate in (
                ("low_reserve", low_reserve),
                ("sample_gap", sample_gap),
                ("summary_drift", summary_drift),
                ("vm_total_drift", vm_total_drift),
            ):
                with self.subTest(label=label):
                    arms = self._arms()
                    mutate(arms)
                    with self.assertRaisesRegex(ValueError, "host evidence"):
                        evaluate_capacity_commissioning(
                            arms,
                            expected_legacy_profile_sha256=_LEGACY_SHA,
                            expected_candidate_profile_sha256=_CANDIDATE_SHA,
                            expected_collector_sha256=_COLLECTOR_SHA,
                            expected_collector_path=(
                                MINERU_WINDOWS_COLLECTOR_PATH
                            ),
                            expected_docker_memory_reserve_bytes=(
                                _MEMORY_RESERVE_BYTES
                            ),
                            expected_windows_node_identity_sha256=_NODE_SHA,
                            minimum_improvement_basis_points=500,
                            maximum_repeat_spread_basis_points=300,
                        )

    def test_orchestrator_sampling_gap_cannot_enter_commissioning(self) -> None:
        arms = self._arms()
        arms[0][0]["stages"][0]["orchestrator"]["sampling_failures"] = [
            {
                "observed_seconds": 0.3,
                "duration_seconds": 0.1,
                "failure": "MinerUOrchestratorUnavailableError:route loss",
            }
        ]
        with patch(
            "disclosure_anchor.adapters.runtime.mineru_capacity_commissioning."
            "summarize_phase_trace_capture",
            side_effect=self._summary,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "orchestrator evidence is not proved",
            ):
                evaluate_capacity_commissioning(
                    arms,
                    expected_legacy_profile_sha256=_LEGACY_SHA,
                    expected_candidate_profile_sha256=_CANDIDATE_SHA,
                    expected_collector_sha256=_COLLECTOR_SHA,
                    expected_collector_path=MINERU_WINDOWS_COLLECTOR_PATH,
                    expected_docker_memory_reserve_bytes=_MEMORY_RESERVE_BYTES,
                    expected_windows_node_identity_sha256=_NODE_SHA,
                    minimum_improvement_basis_points=500,
                    maximum_repeat_spread_basis_points=300,
                )

    def test_safety_limits_must_be_safe_and_identical_across_arms(self) -> None:
        with patch(
            "disclosure_anchor.adapters.runtime.mineru_capacity_commissioning."
            "summarize_phase_trace_capture",
            side_effect=self._summary,
        ):
            for tamper, expected in (
                ("short_document", "staged-load arm is not PASS"),
                ("short_drain", "staged-load arm is not PASS"),
                ("drift", "safety limits drifted"),
            ):
                with self.subTest(tamper=tamper):
                    arms = self._arms()
                    if tamper == "short_document":
                        arms[0][0]["safety_limits"][
                            "document_runaway_timeout_seconds"
                        ] = 1800
                    elif tamper == "short_drain":
                        arms[0][0]["safety_limits"][
                            "api_drain_timeout_seconds"
                        ] = 1800
                    else:
                        arms[1][0]["safety_limits"][
                            "api_drain_timeout_seconds"
                        ] = 172800
                    with self.assertRaisesRegex(ValueError, expected):
                        evaluate_capacity_commissioning(
                            arms,
                            expected_legacy_profile_sha256=_LEGACY_SHA,
                            expected_candidate_profile_sha256=_CANDIDATE_SHA,
                            expected_collector_sha256=_COLLECTOR_SHA,
                            expected_collector_path=(
                                MINERU_WINDOWS_COLLECTOR_PATH
                            ),
                            expected_docker_memory_reserve_bytes=(
                                _MEMORY_RESERVE_BYTES
                            ),
                            expected_windows_node_identity_sha256=_NODE_SHA,
                            minimum_improvement_basis_points=500,
                            maximum_repeat_spread_basis_points=300,
                        )

    def test_retained_terminal_gauges_may_expire_during_an_arm(self) -> None:
        arms = self._arms()
        orchestrator = arms[0][0]["stages"][0]["orchestrator"]
        orchestrator["baseline"]["completed_tasks"] = 100
        orchestrator["baseline"]["failed_tasks"] = 5
        orchestrator["samples"][0]["completed_tasks"] = 90
        orchestrator["samples"][0]["failed_tasks"] = 4
        orchestrator["terminal"]["completed_tasks"] = 80
        orchestrator["terminal"]["failed_tasks"] = 3
        orchestrator["range"]["completed_tasks"] = {"min": 80, "max": 100}
        orchestrator["range"]["failed_tasks"] = {"min": 3, "max": 5}
        with patch(
            "disclosure_anchor.adapters.runtime.mineru_capacity_commissioning."
            "summarize_phase_trace_capture",
            side_effect=self._summary,
        ):
            result = evaluate_capacity_commissioning(
                arms,
                expected_legacy_profile_sha256=_LEGACY_SHA,
                expected_candidate_profile_sha256=_CANDIDATE_SHA,
                expected_collector_sha256=_COLLECTOR_SHA,
                expected_collector_path=MINERU_WINDOWS_COLLECTOR_PATH,
                expected_docker_memory_reserve_bytes=_MEMORY_RESERVE_BYTES,
                expected_windows_node_identity_sha256=_NODE_SHA,
                minimum_improvement_basis_points=500,
                maximum_repeat_spread_basis_points=300,
            )
        self.assertEqual(result["decision"], "COMMISSION")

    def test_material_gain_and_repeatability_are_both_required(self) -> None:
        arms = self._arms()
        arms[1][0]["elapsed_seconds"] = 99.0
        arms[2][0]["elapsed_seconds"] = 99.0
        self._retime(arms)
        with patch(
            "disclosure_anchor.adapters.runtime.mineru_capacity_commissioning."
            "summarize_phase_trace_capture",
            side_effect=self._summary,
        ):
            result = evaluate_capacity_commissioning(
                arms,
                expected_legacy_profile_sha256=_LEGACY_SHA,
                expected_candidate_profile_sha256=_CANDIDATE_SHA,
                expected_collector_sha256=_COLLECTOR_SHA,
                expected_collector_path=MINERU_WINDOWS_COLLECTOR_PATH,
                expected_docker_memory_reserve_bytes=_MEMORY_RESERVE_BYTES,
                expected_windows_node_identity_sha256=_NODE_SHA,
                minimum_improvement_basis_points=500,
                maximum_repeat_spread_basis_points=300,
            )
        self.assertEqual(result["decision"], "STOP")
        self.assertIn(
            "candidate_gain_below_predeclared_minimum",
            result["findings"],
        )


if __name__ == "__main__":
    unittest.main()
