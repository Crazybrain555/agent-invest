"""Read-only source projection tests for capacity observation."""

from __future__ import annotations

from email.message import Message
from email.utils import formatdate
import json
import unittest
from unittest.mock import MagicMock, patch

from disclosure_anchor.adapters.runtime.bounded_http import BoundedHTTPResponse
import disclosure_anchor.adapters.runtime.capacity_sources as sources
from disclosure_anchor.adapters.runtime.gpu_telemetry_freshness import (
    GpuClockEvidenceError,
    GpuCollectionUnavailableError,
    GpuSampleClockUnorderedError,
    GpuSampleStaleError,
    GpuTelemetryUnavailable,
)
import disclosure_anchor.adapters.runtime.worker_progress as progress


def _exporter_date(seconds: float) -> str:
    """The pinned Go exporter's HTTP Date (IMF-fixdate, whole seconds)."""
    return formatdate(seconds, usegmt=True)


def _gpu_health_only_payload(timestamp: float | None, *, failures: int = 61) -> bytes:
    """Upstream 1.14.0 output while its latest collection is unsuccessful.

    Only the always-present health families remain (exporter.go renders no
    device rows when the snapshot table is nil); the last-success timestamp is
    absent until a first success.
    """
    return (
        b'nvidia_gpu_exporter_build_info{branch="HEAD",goarch="amd64",goos="windows",'
        b'goversion="go1.26.5",revision="8f0b43c7c59b71286238455da247650f330df560",'
        b'tags="unknown",version="1.14.0"} 1\n'
        + f"nvidia_smi_failed_scrapes_total {failures}\n".encode()
        + b"nvidia_smi_last_collect_success 0\n"
        + (b"" if timestamp is None else f"nvidia_smi_last_collect_success_timestamp_seconds {timestamp}\n".encode())
    )


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
        from tests._mineru_health_fixture import protocol_health_fields

        health = json.dumps(
            {
                **protocol_health_fields(),
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
        gpu_response = BoundedHTTPResponse(
            status=200, body=_gpu_payload(), headers={"Date": (_exporter_date(1001),)},
            elapsed_seconds=0.25,
        )
        with patch.object(
            sources,
            "_fetch_payload",
            side_effect=(health, metrics),
        ), patch.object(
            sources,
            "_fetch_response",
            return_value=gpu_response,
        ) as fetch_gpu:
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

        self.assertEqual(fetch_gpu.call_args.kwargs["capture_headers"], ("Date",))
        self.assertEqual(api.completed_tasks_gauge, 9)
        self.assertEqual(api.max_pending_tasks_requested, 1)
        self.assertEqual(api.max_pending_tasks_effective, 1)
        self.assertEqual(vllm.requests_running, 7)
        progress_api = progress.mineru_api_health_snapshot(
            health,
            expected_task_slots=1,
        )
        progress_vllm = progress.vllm_metrics_snapshot(metrics)
        progress_gpu = progress.gpu_metrics_snapshot(
            _gpu_payload(),
            expected_device_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            response_date=(_exporter_date(1001),),
            transport_elapsed_seconds=0.25,
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
                response_date=(_exporter_date(1000),),
                transport_elapsed_seconds=0.0,
            )

    def test_capacity_api_rejects_pending_depth_drift(self) -> None:
        from tests._mineru_health_fixture import protocol_health_fields

        base = {
            **protocol_health_fields(),
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
            json.dumps(base).encode(), expected_task_slots=None
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
                    expected_task_slots=None,
                )

    def test_both_api_health_consumers_reject_impossible_slot_state(self) -> None:
        from tests._mineru_health_fixture import protocol_health_fields

        payload = {
            **protocol_health_fields(),
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
        with self.assertRaisesRegex(ValueError, "task-slot/pending"):
            sources._api_values(encoded, expected_task_slots=1)
        with self.assertRaisesRegex(ValueError, "task-slot/pending"):
            progress.mineru_api_health_snapshot(encoded, expected_task_slots=1)

    def test_gpu_freshness_uses_only_the_exporter_host_clock(self) -> None:
        uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

        def strict(payload: bytes, dates: tuple[str, ...], elapsed: object = 0.0) -> object:
            return sources._gpu_values(
                payload, expected_device_uuid=uuid, response_date=dates,
                transport_elapsed_seconds=elapsed,
            )

        def shown(payload: bytes, dates: tuple[str, ...] | None, elapsed: object = 0.0) -> object:
            return progress.nvidia_smi_metrics_snapshot(
                payload, response_date=dates, transport_elapsed_seconds=elapsed,
                expected_device_uuid=uuid,
            )

        # The same-host Date - success bound plus the local request-to-receipt
        # time decides; the reading host's wall clock is never consulted, so
        # any cross-host offset or local step is irrelevant. Both whole-second
        # floors add one conservative second.
        with patch("time.time", side_effect=AssertionError("local wall clock read")):
            # Real 2026-09-29 pinned-exporter pair that the retired cross-host
            # rule rejected as 1.06 s "in the future" of the reading Mac.
            natural = _gpu_payload().replace(b"timestamp_seconds 1000", b"timestamp_seconds 1.790697811e+09")
            natural_date = ("Tue, 29 Sep 2026 16:03:31 GMT",)
            self.assertEqual(strict(natural, natural_date, 0.2).gpu_utilization_pct, 87.5)
            self.assertEqual(shown(natural, natural_date, 0.2)["sample_age_seconds"], 1.2)
            for date, elapsed, bound in ((1000, 0.0, 1.0), (1005, 0.25, 6.25), (1029, 0.0, 30.0)):
                with self.subTest(date=date, elapsed=elapsed):
                    self.assertEqual(strict(_gpu_payload(), (_exporter_date(date),), elapsed).gpu_utilization_pct, 87.5)
                    self.assertEqual(shown(_gpu_payload(), (_exporter_date(date),), elapsed)["sample_age_seconds"], bound)
            for date, elapsed, error in (
                (1030, 0.0, GpuSampleStaleError), (1029, 0.5, GpuSampleStaleError),
                (999, 0.0, GpuSampleClockUnorderedError),
            ):
                for parse in (strict, shown):
                    with self.subTest(date=date, elapsed=elapsed, parse=parse), self.assertRaises(error):
                        parse(_gpu_payload(), (_exporter_date(date),), elapsed)
            # The local elapsed time is a measured monotonic duration: absent,
            # negative, non-finite or boolean values fail closed as invalid.
            for elapsed in (None, -0.001, float("nan"), float("inf"), True):
                for parse in (strict, shown):
                    with self.subTest(elapsed=elapsed, parse=parse), self.assertRaises(ValueError) as caught:
                        parse(_gpu_payload(), (_exporter_date(1000),), elapsed)
                    self.assertNotIsInstance(caught.exception, GpuTelemetryUnavailable)
            clock_evidence_faults = (
                (), (_exporter_date(1000), _exporter_date(1000)), ("1000",),
                ("Thursday, 01-Jan-70 00:16:40 GMT",), ("Fri, 01 Jan 1970 00:16:40 GMT",),
                ("Thu, 31 Feb 1970 00:16:40 GMT",), ("Thu, 01 Jan 1970 00:16:40 +0000",),
            )
            for dates in clock_evidence_faults:
                for parse in (strict, shown):
                    with self.subTest(dates=dates, parse=parse), self.assertRaises(GpuClockEvidenceError) as caught:
                        parse(_gpu_payload(), dates)
                    self.assertNotIsInstance(caught.exception, GpuTelemetryUnavailable)
            with self.assertRaises(GpuClockEvidenceError):
                shown(_gpu_payload(), None)
            with self.assertRaises(GpuClockEvidenceError):
                progress.gpu_metrics_snapshot(_gpu_payload(), expected_device_uuid=uuid, transport_elapsed_seconds=0.0)

            # The real failed-collection and warm-up shapes are known transient
            # unavailability; a malformed or identity-less success stays hard.
            dates = (_exporter_date(1010),)
            for payload in (_gpu_health_only_payload(1000), _gpu_health_only_payload(None, failures=0)):
                with self.subTest(payload=payload[-60:]), self.assertRaises(GpuCollectionUnavailableError):
                    strict(payload, dates)
            for payload in (
                _gpu_health_only_payload(1000).replace(b"nvidia_smi_failed_scrapes_total 61\n", b""),
                _gpu_health_only_payload(1000).replace(b"success 0", b"success 1"),
                _gpu_health_only_payload(0),
            ):
                with self.subTest(payload=payload[-60:]), self.assertRaises(ValueError) as caught:
                    strict(payload, dates)
                self.assertNotIsInstance(caught.exception, GpuTelemetryUnavailable)

    def test_gpu_sampler_age_includes_the_local_request_to_receipt_time(self) -> None:
        # The response Date is stamped before its body finishes arriving; the
        # real fetch path adds its own monotonic request-to-receipt duration.
        def sample(date: int, monotonic_marks: tuple[float, float]) -> object:
            headers = Message()
            headers["Content-Type"] = "text/plain; version=0.0.4"
            headers["Date"] = _exporter_date(date)
            response = MagicMock()
            response.__enter__.return_value = response
            response.geturl.return_value = "http://127.0.0.1:30004/metrics"
            response.headers = headers
            response.read.return_value = _gpu_payload()
            response.status = 200
            opener = MagicMock()
            opener.open.return_value = response
            with patch.object(sources.urllib.request, "build_opener", return_value=opener), patch.object(
                sources.time, "monotonic", side_effect=monotonic_marks,
            ), patch("time.time", side_effect=AssertionError("local wall clock read")):
                return sources.GpuCapacitySampler(
                    url="http://127.0.0.1:30004/metrics", timeout_seconds=1,
                    expected_device_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                ).sample()

        # Adjacent pair: 29 s by the exporter's own clock plus 0.4 s of local
        # transfer is current; the same sample after a 1.5 s body read is not.
        self.assertEqual(sample(1028, (100.0, 100.4)).gpu_utilization_pct, 87.5)
        with self.assertRaises(GpuSampleStaleError):
            sample(1028, (100.0, 101.5))
        # Root's counterexample: Date 1029 / token 1000 bounds 30 s at response
        # time; after a 1.5 s body read the sample is up to 31.5 s old.
        with self.assertRaises(GpuSampleStaleError):
            sample(1029, (100.0, 101.5))


if __name__ == "__main__":
    unittest.main()
