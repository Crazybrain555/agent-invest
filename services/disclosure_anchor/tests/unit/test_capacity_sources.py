"""Read-only source projection tests for capacity observation."""

from __future__ import annotations

from email.message import Message
import json
import unittest
from unittest.mock import MagicMock, patch

import disclosure_anchor.adapters.runtime.capacity_sources as sources
import disclosure_anchor.adapters.runtime.worker_progress as progress


def _gpu_payload() -> bytes:
    uuid = b"GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    return (
        b"nvidia_smi_last_collect_success 1\n"
        b"nvidia_smi_last_collect_success_timestamp_seconds 1000\n"
        b'nvidia_smi_gpu_info{index="0",name="RTX 5080",uuid="'
        + uuid
        + b'"} 1\n'
        + b'nvidia_smi_utilization_gpu_ratio{uuid="'
        + uuid
        + b'"} 0.875\n'
        + b'nvidia_smi_memory_used_bytes{uuid="'
        + uuid
        + b'"} 9283043328\n'
        + b'nvidia_smi_memory_free_bytes{uuid="'
        + uuid
        + b'"} 7818182656\n'
        + b'nvidia_smi_memory_total_bytes{uuid="'
        + uuid
        + b'"} 17101225984\n'
        + b'nvidia_smi_power_draw_watts{uuid="'
        + uuid
        + b'"} 245.5\n'
        + b'nvidia_smi_temperature_gpu{uuid="'
        + uuid
        + b'"} 67\n'
    )


class CapacitySourcesTests(unittest.TestCase):
    def test_fetch_bypasses_proxy_rejects_redirect_and_bounds_payload(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "http://127.0.0.1:30002/health"
        headers = Message()
        headers["Content-Type"] = "application/json"
        response.headers = headers
        response.read.return_value = b"{}"
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(
            sources.urllib.request,
            "build_opener",
            return_value=opener,
        ) as build_opener:
            payload = sources._fetch_payload(
                "http://127.0.0.1:30002/health",
                timeout_seconds=1,
                accepted_content_types=frozenset({"application/json"}),
                maximum_bytes=64,
            )
        response.geturl.return_value = "http://127.0.0.1:30002/health"
        response.read.return_value = b"x" * 65
        with patch.object(
            sources.urllib.request,
            "build_opener",
            return_value=opener,
        ), self.assertRaisesRegex(ValueError, "safety limit"):
            sources._fetch_payload(
                "http://127.0.0.1:30002/health",
                timeout_seconds=1,
                accepted_content_types=frozenset({"application/json"}),
                maximum_bytes=64,
            )

        self.assertEqual(payload, b"{}")
        self.assertEqual(build_opener.call_args.args[0].proxies, {})
        response.geturl.return_value = "http://redirect.invalid/health"
        with patch.object(
            sources.urllib.request,
            "build_opener",
            return_value=opener,
        ), self.assertRaisesRegex(ValueError, "redirected"):
            sources._fetch_payload(
                "http://127.0.0.1:30002/health",
                timeout_seconds=1,
                accepted_content_types=frozenset({"application/json"}),
                maximum_bytes=64,
            )

    def test_samplers_project_only_closed_content_free_fields(self) -> None:
        health = json.dumps(
            {
                "status": "healthy",
                "version": "3.4.4",
                "protocol_version": 2,
                "queued_tasks": 0,
                "processing_tasks": 1,
                "completed_tasks": 9,
                "failed_tasks": 0,
                "max_concurrent_requests": 1,
                "max_pending_tasks_requested": 1,
                "max_pending_tasks_effective": 1,
                "processing_window_size": 16,
                "task_retention_seconds": 600,
                "task_cleanup_interval_seconds": 30,
            }
        ).encode()
        metrics = (
            b"vllm:num_requests_running 7\n"
            b"vllm:num_requests_waiting 0\n"
            b"vllm:num_preemptions_total 0\n"
            b"vllm:gpu_cache_usage_perc 0.1\n"
        )
        with patch.object(
            sources,
            "_fetch_payload",
            side_effect=(health, metrics, _gpu_payload()),
        ), patch.object(
            sources.time,
            "time",
            return_value=1001,
        ):
            api = sources.MineruApiCapacitySampler(
                url="http://127.0.0.1:30002",
                timeout_seconds=1,
                task_slots=1,
            ).sample()
            vllm = sources.VllmCapacitySampler(
                url="http://127.0.0.1:30003/v1",
                timeout_seconds=1,
            ).sample()
            gpu = sources.GpuCapacitySampler(
                url="http://127.0.0.1:30004/metrics",
                timeout_seconds=1,
                expected_device_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            ).sample()

        self.assertEqual(api.completed_tasks_gauge, 9)
        self.assertEqual(api.max_pending_tasks_requested, 1)
        self.assertEqual(api.max_pending_tasks_effective, 1)
        self.assertEqual(vllm.requests_running, 7)
        progress_api = progress.mineru_api_health_snapshot(
            health,
            expected_task_slots=1,
        )
        progress_vllm = progress.vllm_metrics_snapshot(metrics)
        with patch.object(progress.time, "time", return_value=1001):
            progress_gpu = progress.gpu_metrics_snapshot(
                _gpu_payload(),
                expected_device_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            )
        self.assertEqual(api.queued_tasks, progress_api["queued_tasks"])
        self.assertEqual(
            api.max_pending_tasks_effective,
            progress_api["max_pending_tasks_effective"],
        )
        self.assertEqual(vllm.requests_waiting, progress_vllm["requests_waiting"])
        self.assertEqual(
            gpu.gpu_utilization_pct,
            progress_gpu["gpu_utilization_pct_mean"],
        )
        encoded = json.dumps(gpu.model_dump(mode="json"), sort_keys=True)
        self.assertNotIn("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", encoded)
        self.assertIn("device_identity_sha256", encoded)

    def test_capacity_gpu_rejects_uncommissioned_dcgm_family(self) -> None:
        with self.assertRaisesRegex(ValueError, "pinned nvidia-smi"):
            sources._gpu_values(
                b'DCGM_FI_DEV_GPU_UTIL{gpu="0"} 90\n',
                expected_device_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            )

    def test_capacity_api_rejects_pending_depth_drift(self) -> None:
        base = {
            "status": "healthy",
            "version": "3.4.4",
            "protocol_version": 2,
            "queued_tasks": 0,
            "processing_tasks": 0,
            "completed_tasks": 0,
            "failed_tasks": 0,
            "max_concurrent_requests": 3,
            "max_pending_tasks_requested": 4,
            "max_pending_tasks_effective": 4,
            "processing_window_size": 16,
            "task_retention_seconds": 600,
            "task_cleanup_interval_seconds": 30,
        }
        accepted = sources._api_values(
            json.dumps(base).encode(), expected_task_slots=3
        )
        self.assertEqual(accepted.max_pending_tasks_effective, 4)
        for mutation in (
            {"max_pending_tasks_effective": 2},
            {"max_pending_tasks_requested": 5},
            {"max_pending_tasks_effective": True},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                sources._api_values(
                    json.dumps({**base, **mutation}).encode(),
                    expected_task_slots=3,
                )

    def test_both_api_health_consumers_reject_impossible_slot_state(self) -> None:
        payload = {
            "status": "healthy",
            "version": "3.4.4",
            "protocol_version": 2,
            "queued_tasks": 0,
            "processing_tasks": 2,
            "completed_tasks": 0,
            "failed_tasks": 0,
            "max_concurrent_requests": 1,
            "max_pending_tasks_requested": 2,
            "max_pending_tasks_effective": 2,
            "processing_window_size": 16,
            "task_retention_seconds": 600,
            "task_cleanup_interval_seconds": 30,
        }
        encoded = json.dumps(payload).encode()
        with self.assertRaisesRegex(ValueError, "exceed"):
            sources._api_values(encoded, expected_task_slots=1)
        with self.assertRaisesRegex(ValueError, "exceed"):
            progress.mineru_api_health_snapshot(encoded, expected_task_slots=1)

    def test_cross_host_gpu_timestamp_has_bounded_future_skew(self) -> None:
        with patch.object(sources.time, "time", return_value=999.75):
            observed = sources._gpu_values(
                _gpu_payload(),
                expected_device_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            )
        progress_observed = progress.nvidia_smi_metrics_snapshot(
            _gpu_payload(),
            now_timestamp=999.75,
            expected_device_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )

        self.assertEqual(observed.gpu_utilization_pct, 87.5)
        self.assertEqual(progress_observed["sample_age_seconds"], 0.0)
        for now_timestamp in (998.99, 1030.01):
            with self.subTest(now_timestamp=now_timestamp), patch.object(
                sources.time,
                "time",
                return_value=now_timestamp,
            ), self.assertRaises(ValueError):
                sources._gpu_values(
                    _gpu_payload(),
                    expected_device_uuid=(
                        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
                    ),
                )
            with self.assertRaises(ValueError):
                progress.nvidia_smi_metrics_snapshot(
                    _gpu_payload(),
                    now_timestamp=now_timestamp,
                    expected_device_uuid=(
                        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
                    ),
                )


if __name__ == "__main__":
    unittest.main()
