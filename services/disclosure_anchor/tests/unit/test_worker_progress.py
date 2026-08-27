from __future__ import annotations

import json
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from disclosure_anchor.adapters.runtime import worker_progress as progress_module
from disclosure_anchor.adapters.runtime.worker_progress import (
    append_worker_progress,
    collect_worker_progress,
    dcgm_metrics_snapshot,
    gpu_metrics_snapshot,
    mineru_api_health_snapshot,
    nvidia_smi_metrics_snapshot,
    parse_prometheus_metrics,
    render_worker_progress,
    vllm_metrics_snapshot,
)
from disclosure_anchor.settings import Settings


def _settings(root: Path, **values: object) -> Settings:
    service = root / "service"
    shared = root / "shared"
    return Settings(
        disclosure_data_root=service,
        disclosure_shared_root=shared,
        disclosure_runtime_root=service / "runtime",
        mineru_model_cache=shared / "mineru",
        hf_home=shared / "hf",
        modelscope_cache=shared / "modelscope",
        **values,  # type: ignore[arg-type]
    )


def _event() -> dict[str, object]:
    return {
        "contract_version": "worker_progress.v2",
        "observed_at": "2026-08-24T12:00:00+00:00",
        "universe": {
            "active_companies": 1500,
            "paused_companies": 0,
            "synced_companies": 750,
        },
        "documents": {
            "known_process_documents": 100,
            "published_documents": 40,
            "denominator_is_dynamic": True,
        },
        "queues": {
            "pending_download": 8,
            "pending_parse": 7,
            "pending_build": 6,
            "pending_publish": 5,
            "download_dead_letters": 0,
            "parse_dead_letters": 1,
            "build_dead_letters": 0,
        },
        "current_work": [
            {
                "stage": "parse",
                "processing_run_id": "run_1",
                "document_id": "doc_1",
                "security_code": "600519",
                "title": "年度报告",
                "started_at": "2026-08-24T11:59:00+00:00",
            }
        ],
        "latest_interval": None,
        "orchestration": {
            "status": "available",
            "source": "mineru_api_health",
            "health_status": "healthy",
            "version": "3.4.4",
            "protocol_version": 2,
            "queued_tasks": 2,
            "processing_tasks": 2,
            "completed_tasks": 20,
            "failed_tasks": 1,
            "max_concurrent_requests": 3,
            "processing_window_size": 16,
            "task_retention_seconds": 600,
            "task_cleanup_interval_seconds": 30,
        },
        "inference": {
            "status": "available",
            "source": "vllm_metrics",
            "requests_running": 12,
            "requests_waiting": 3,
            "preemptions_total": 0,
            "kv_cache_usage_ratio_max": 0.42,
        },
        "gpu": {
            "status": "available",
            "source": "nvidia_dcgm_exporter",
            "device_count": 1,
            "gpu_utilization_pct_mean": 88.0,
            "gpu_utilization_pct_max": 88.0,
            "framebuffer_used_mib_total": 8192.0,
            "framebuffer_free_mib_total": 8192.0,
            "power_usage_watts_total": 250.0,
            "temperature_celsius_max": 68.0,
        },
    }


def _api_health_payload() -> bytes:
    return json.dumps(
        {
            "status": "healthy",
            "version": "3.4.4",
            "protocol_version": 2,
            "queued_tasks": 2,
            "processing_tasks": 2,
            "completed_tasks": 20,
            "failed_tasks": 1,
            "max_concurrent_requests": 3,
            "processing_window_size": 16,
            "task_retention_seconds": 600,
            "task_cleanup_interval_seconds": 30,
        }
    ).encode()


def _nvidia_smi_payload() -> bytes:
    uuid = b"GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    return (
        b"nvidia_smi_last_collect_success 1\n"
        b"nvidia_smi_last_collect_success_timestamp_seconds 1000\n"
        b'nvidia_smi_gpu_info{index="0",name="NVIDIA GeForce RTX 5080",uuid="'
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
        + b'"} 17094934528\n'
        + b'nvidia_smi_power_draw_watts{uuid="'
        + uuid
        + b'"} 245.5\n'
        + b'nvidia_smi_temperature_gpu{uuid="'
        + uuid
        + b'"} 67\n'
    )


class WorkerProgressTests(unittest.TestCase):
    def test_telemetry_fetch_bypasses_proxies_and_rejects_redirect_or_html(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "http://127.0.0.1:30002/health"
        response.headers.get_content_type.return_value = "application/json"
        response.read.return_value = b"{}"
        opener = MagicMock()
        opener.open.return_value = response
        with patch(
            "disclosure_anchor.adapters.runtime.worker_progress."
            "urllib.request.build_opener",
            return_value=opener,
        ) as build_opener:
            payload = progress_module._fetch_payload(
                "http://127.0.0.1:30002/health",
                timeout_seconds=3,
                accept="application/json",
                maximum_bytes=64,
            )

        self.assertEqual(payload, b"{}")
        handlers = build_opener.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], progress_module._NoRedirectHandler)

        for final_url, content_type, error in (
            (
                "http://elsewhere.invalid/health",
                "application/json",
                "redirected",
            ),
            (
                "http://127.0.0.1:30002/health",
                "text/html",
                "content type",
            ),
        ):
            response.geturl.return_value = final_url
            response.headers.get_content_type.return_value = content_type
            with self.subTest(error=error), patch(
                "disclosure_anchor.adapters.runtime.worker_progress."
                "urllib.request.build_opener",
                return_value=opener,
            ), self.assertRaisesRegex(RuntimeError, error):
                progress_module._fetch_payload(
                    "http://127.0.0.1:30002/health",
                    timeout_seconds=3,
                    accept="application/json",
                    maximum_bytes=64,
                )

    @staticmethod
    def _with_progress_urls(
        settings: Settings,
        *,
        api_url: str | None,
        observability_url: str | None,
    ) -> SimpleNamespace:
        values = {
            name: getattr(settings, name)
            for name in settings.__class__.model_fields
        }
        values.update(
            disclosure_mineru_api_url=api_url,
            disclosure_mineru_observability_url=observability_url,
        )
        return SimpleNamespace(**values)

    def test_prometheus_parser_ignores_comments_invalid_and_nonfinite(self) -> None:
        samples = parse_prometheus_metrics(
            b"# HELP x test\nx{gpu=\"0\"} 1\nx{gpu=\"1\"} NaN\nbad\ny 2 3\n"
        )

        self.assertEqual(samples, {"x": (1.0,), "y": (2.0,)})

    def test_vllm_and_dcgm_telemetry_remain_separately_labelled(self) -> None:
        vllm = vllm_metrics_snapshot(
            b"vllm:num_requests_running 4\n"
            b"vllm_num_requests_running 99\n"
            b"vllm:num_requests_waiting 2\n"
            b"vllm_num_requests_waiting 98\n"
            b"vllm:num_preemptions_total 1\n"
            b"vllm:gpu_cache_usage_perc 0.75\n"
        )
        dcgm = dcgm_metrics_snapshot(
            b"DCGM_FI_DEV_GPU_UTIL{gpu=\"0\"} 80\n"
            b"DCGM_FI_DEV_GPU_UTIL{gpu=\"1\"} 60\n"
            b"DCGM_FI_DEV_FB_USED{gpu=\"0\"} 1024\n"
            b"DCGM_FI_DEV_FB_USED{gpu=\"1\"} 2048\n"
        )

        self.assertEqual(vllm["source"], "vllm_metrics")
        self.assertEqual(vllm["requests_running"], 4)
        self.assertEqual(vllm["requests_waiting"], 2)
        self.assertNotIn("gpu_utilization_pct_mean", vllm)
        self.assertEqual(vllm["kv_cache_usage_ratio_max"], 0.75)
        self.assertEqual(dcgm["source"], "nvidia_dcgm_exporter")
        self.assertEqual(dcgm["gpu_utilization_pct_mean"], 70.0)
        self.assertEqual(dcgm["framebuffer_used_mib_total"], 3072.0)

    def test_windows_nvidia_smi_metrics_are_fresh_real_gpu_telemetry(self) -> None:
        payload = _nvidia_smi_payload()

        observed = nvidia_smi_metrics_snapshot(
            payload,
            now_timestamp=1005,
            expected_device_uuid="GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )

        self.assertEqual(observed["source"], "nvidia_smi_exporter")
        self.assertEqual(
            observed["device_uuid"],
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )
        self.assertEqual(observed["device_name"], "NVIDIA GeForce RTX 5080")
        self.assertEqual(observed["sample_age_seconds"], 5.0)
        self.assertEqual(observed["gpu_utilization_pct_mean"], 87.5)
        self.assertEqual(observed["framebuffer_used_mib_total"], 8853.0)
        self.assertEqual(observed["framebuffer_free_mib_total"], 7456.0)
        self.assertEqual(observed["power_usage_watts_total"], 245.5)
        self.assertEqual(observed["temperature_celsius_max"], 67.0)
        near_future = nvidia_smi_metrics_snapshot(
            payload,
            now_timestamp=999.75,
            expected_device_uuid="GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )
        self.assertEqual(near_future["sample_age_seconds"], 0.0)
        with patch(
            "disclosure_anchor.adapters.runtime.worker_progress.time.time",
            return_value=1005,
        ):
            self.assertEqual(gpu_metrics_snapshot(payload), observed)

        for invalid in (
            payload.replace(
                b"nvidia_smi_last_collect_success 1",
                b"nvidia_smi_last_collect_success 0",
            ),
            payload.replace(b"timestamp_seconds 1000", b"timestamp_seconds 900"),
            payload.replace(b'"} 0.875', b'"} 87.5'),
            payload + (
                b'nvidia_smi_utilization_gpu_ratio{uuid="'
                b'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"} 0.1\n'
            ),
            payload.replace(
                b'nvidia_smi_memory_used_bytes{uuid="GPU-aaaaaaaa',
                b'nvidia_smi_memory_used_bytes{uuid="GPU-bbbbbbbb',
            ),
            payload.replace(b'index="0"', b'index="1"'),
        ):
            with self.subTest(invalid=invalid[-80:]), self.assertRaises(ValueError):
                nvidia_smi_metrics_snapshot(invalid, now_timestamp=1005)
        with self.assertRaises(ValueError):
            nvidia_smi_metrics_snapshot(
                payload,
                now_timestamp=1005,
                expected_device_uuid="GPU-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            )
        with patch(
            "disclosure_anchor.adapters.runtime.worker_progress.time.time",
            return_value=1005,
        ), self.assertRaises(ValueError):
            gpu_metrics_snapshot(
                payload + b'DCGM_FI_DEV_GPU_UTIL{gpu="0"} 90\n'
            )

    def test_mineru_api_health_contract_is_exact(self) -> None:
        payload = json.loads(_api_health_payload())
        assert isinstance(payload, dict)
        observed = mineru_api_health_snapshot(_api_health_payload())

        self.assertEqual(observed["source"], "mineru_api_health")
        self.assertEqual(observed["health_status"], "healthy")
        self.assertEqual(observed["version"], "3.4.4")
        self.assertEqual(observed["protocol_version"], 2)
        self.assertEqual(observed["queued_tasks"], 2)
        self.assertEqual(observed["processing_tasks"], 2)
        self.assertEqual(observed["max_concurrent_requests"], 3)
        for task_slots in (1, 2, 3):
            accepted = {
                **payload,
                "max_concurrent_requests": task_slots,
                "processing_tasks": min(2, task_slots),
            }
            self.assertEqual(
                mineru_api_health_snapshot(
                    json.dumps(accepted).encode(),
                    expected_task_slots=task_slots,
                )["max_concurrent_requests"],
                task_slots,
            )
        with self.assertRaises(ValueError):
            mineru_api_health_snapshot(
                _api_health_payload(),
                expected_task_slots=1,
            )
        for mutation in (
            {"version": "3.4.3"},
            {"protocol_version": 1},
            {"queued_tasks": True},
            {"processing_tasks": -1},
            {"processing_tasks": 4},
            {"queued_tasks": 14, "processing_tasks": 3},
            {"max_concurrent_requests": 0},
            {"max_concurrent_requests": 16},
            {"task_retention_seconds": 3600},
            {"unexpected": 1},
        ):
            with self.subTest(mutation=mutation):
                invalid = {**payload, **mutation}
                with self.assertRaises(ValueError):
                    mineru_api_health_snapshot(json.dumps(invalid).encode())
        missing = dict(payload)
        missing.pop("task_cleanup_interval_seconds")
        with self.assertRaises(ValueError):
            mineru_api_health_snapshot(json.dumps(missing).encode())

    def test_collect_marks_unconfigured_telemetry_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "disclosure_anchor.adapters.runtime.worker_progress."
            "worker_progress_database_snapshot",
            return_value={
                "universe": {},
                "documents": {},
                "queues": {},
                "current_work": [],
            },
        ):
            engine = MagicMock()
            event = collect_worker_progress(
                settings=_settings(Path(tmp)),
                engine=engine,
                scope_classes=("annual_report",),
                now=lambda: datetime(2026, 8, 24, tzinfo=timezone.utc),
            )

            raw_connection = engine.connect.return_value.__enter__.return_value
            raw_connection.execution_options.assert_called_once_with(
                isolation_level="REPEATABLE READ"
            )
            connection = raw_connection.execution_options.return_value
            connection.exec_driver_sql.assert_called_once_with(
                "SET TRANSACTION READ ONLY"
            )

        self.assertEqual(event["inference"]["reason"], "not_configured")
        self.assertEqual(event["orchestration"]["reason"], "not_configured")
        self.assertEqual(event["gpu"]["reason"], "not_configured")
        self.assertEqual(event["contract_version"], "worker_progress.v2")
        self.assertRegex(event["event_id"], r"^[a-f0-9]{32}:[0-9]+$")
        self.assertGreater(event["sequence"], 0)

    def test_probe_distinguishes_endpoints_and_contracts_and_normalizes_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "disclosure_anchor.adapters.runtime.worker_progress."
            "worker_progress_database_snapshot",
            return_value={
                "universe": {},
                "documents": {},
                "queues": {},
                "current_work": [],
            },
        ), patch(
            "disclosure_anchor.adapters.runtime.worker_progress._fetch_api_health",
            return_value=_api_health_payload(),
        ) as fetch_api, patch(
            "disclosure_anchor.adapters.runtime.worker_progress._fetch_metrics",
            side_effect=[RuntimeError("offline")],
        ) as fetch_metrics, patch(
            "disclosure_anchor.adapters.runtime.worker_progress."
            "_fetch_gpu_metrics",
            return_value=b"unrelated_metric 1\n",
        ) as fetch_gpu_metrics:
            base_settings = _settings(
                Path(tmp),
                disclosure_gpu_metrics_url="http://127.0.0.1:30004/metrics",
                disclosure_mineru_api_task_slots=3,
                worker_gpu_request_budget=21,
            )
            settings = self._with_progress_urls(
                base_settings,
                api_url="http://orchestrator.invalid/",
                observability_url="http://gpu.invalid/v1/",
            )
            engine = MagicMock()
            event = collect_worker_progress(
                settings=settings,  # type: ignore[arg-type]
                engine=engine,
                scope_classes=("annual_report",),
            )

        self.assertEqual(event["orchestration"]["status"], "available")
        self.assertEqual(event["orchestration"]["queued_tasks"], 2)
        self.assertEqual(event["inference"]["reason"], "endpoint_unreachable")
        self.assertEqual(event["gpu"]["reason"], "metric_contract_unsatisfied")
        fetch_api.assert_called_once_with(
            "http://orchestrator.invalid/health",
            timeout_seconds=5.0,
        )
        self.assertEqual(
            [call.args[0] for call in fetch_metrics.call_args_list],
            ["http://gpu.invalid/metrics"],
        )
        fetch_gpu_metrics.assert_called_once_with(
            "http://127.0.0.1:30004/metrics",
            timeout_seconds=5.0,
        )

    def test_collect_marks_invalid_api_health_as_contract_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "disclosure_anchor.adapters.runtime.worker_progress."
            "worker_progress_database_snapshot",
            return_value={
                "universe": {},
                "documents": {},
                "queues": {},
                "current_work": [],
            },
        ), patch(
            "disclosure_anchor.adapters.runtime.worker_progress._fetch_api_health",
            return_value=b'{"wrong":"shape"}',
        ):
            settings = self._with_progress_urls(
                _settings(Path(tmp)),
                api_url="http://orchestrator.invalid",
                observability_url=None,
            )
            event = collect_worker_progress(
                settings=settings,  # type: ignore[arg-type]
                engine=MagicMock(),
                scope_classes=None,
            )

        self.assertEqual(
            event["orchestration"]["reason"],
            "api_contract_unsatisfied",
        )

    def test_append_is_jsonl_private_and_terminal_has_two_progress_bars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            event = _event()
            path = append_worker_progress(settings, event)
            first_persisted = json.loads(path.read_text(encoding="utf-8"))
            first_mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod(0o644)
            append_worker_progress(settings, event)
            persisted_lines = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            repaired_mode = stat.S_IMODE(path.stat().st_mode)

        self.assertEqual(first_persisted["contract_version"], "worker_progress.v2")
        self.assertEqual(first_mode, 0o600)
        self.assertEqual(len(persisted_lines), 2)
        self.assertEqual(repaired_mode, 0o600)
        rendered = render_worker_progress(event)
        self.assertIn("750/1500 synced", rendered)
        self.assertIn("40/100 published (dynamic total)", rendered)
        self.assertIn(
            "MinerU API queued=2 processing=2 completed=20 failed=1 cap=3 window=16",
            rendered,
        )
        self.assertIn("vLLM running=12 waiting=3 KV=42.0%", rendered)
        self.assertIn(
            "GPU/DCGM util=88.0% mean/88.0% max VRAM=8192/16384MiB "
            "power=250.0W temp=68C",
            rendered,
        )
        self.assertIn("current parse:600519", rendered)

        event["latest_interval"] = {
            "synced_companies": 0,
            "downloaded": 0,
            "parsed": 0,
            "built": 0,
            "published": 0,
            "failed": 0,
            "admission": {
                "status": "unavailable",
                "reason": "MinerU API unreachable",
                "first_failure_at": "2026-08-24T12:00:00+00:00",
                "consecutive_failures": 2,
                "next_probe_at": "2026-08-24T12:04:00+00:00",
            },
        }
        unavailable = render_worker_progress(event)
        self.assertIn(
            "admission=unavailable consecutive_failures=2 "
            "next_probe=2026-08-24T12:04:00+00:00 "
            "reason=MinerU API unreachable",
            unavailable,
        )


if __name__ == "__main__":
    unittest.main()
