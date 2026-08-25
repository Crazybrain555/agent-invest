"""Deterministic, DB-free tests for the fixed MinerU staged-load gate."""

from __future__ import annotations

from contextlib import redirect_stderr
from email.message import Message
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
from types import SimpleNamespace
import stat
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

import scripts.mineru_staged_load as staged
from disclosure_anchor.adapters.runtime.mineru_process_isolation import (
    active_disclosure_producers,
    mineru_processes,
)


class MinerUStagedLoadTests(unittest.TestCase):
    @staticmethod
    def _metrics_response(payload: bytes) -> MagicMock:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = payload
        return response

    @staticmethod
    def _valid_metrics() -> bytes:
        return b"""
vllm:num_requests_running 1
vllm:num_requests_waiting 0
vllm:num_preemptions_total 0
vllm:gpu_cache_usage_perc 0.1
"""

    @staticmethod
    def _health(
        *,
        queued: int = 0,
        processing: int = 0,
        completed: int = 0,
        failed: int = 0,
    ) -> staged.MinerUOrchestratorHealth:
        return staged.MinerUOrchestratorHealth(
            status="healthy",
            version="3.4.4",
            protocol_version=2,
            queued_tasks=queued,
            processing_tasks=processing,
            completed_tasks=completed,
            failed_tasks=failed,
            max_concurrent_requests=3,
            processing_window_size=16,
            task_retention_seconds=86400,
            task_cleanup_interval_seconds=300,
        )

    def test_fixed_envelope_has_no_operator_stage_override(self) -> None:
        self.assertEqual(staged.STAGE_DOCUMENT_CONCURRENCIES, (4, 8, 16))
        self.assertEqual(staged.ORCHESTRATOR_TASK_CONCURRENCY, 3)
        self.assertEqual(staged.ORCHESTRATOR_INFERENCE_CONCURRENCY, 7)
        self.assertEqual(staged.EFFECTIVE_INFERENCE_REQUEST_UPPER_BOUND, 21)
        self.assertEqual(
            staged.STAGE_EFFECTIVE_INFERENCE_REQUEST_UPPER_BOUNDS,
            (21, 21, 21),
        )

        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            staged._parse_args(
                [
                    "--runtime-manifest",
                    "manifest.json",
                    "--receipt-out",
                    "receipt.json",
                    "--input",
                    "frozen.pdf",
                    "--expected-input-sha256",
                    "a" * 64,
                    "--stages",
                    "32",
                ]
            )

    def test_stage_sequence_stops_before_unsafe_next_stage(self) -> None:
        calls: list[int] = []

        def run(concurrency: int) -> dict[str, str]:
            calls.append(concurrency)
            return {"status": "fail" if concurrency == 8 else "pass"}

        result = staged.execute_fixed_stage_sequence(run)

        self.assertEqual(calls, [4, 8])
        self.assertEqual([item["status"] for item in result], ["pass", "fail"])

    def test_metrics_parser_aggregates_labels_and_accepts_kv_alias(self) -> None:
        sample = staged.parse_vllm_metrics(
            b"""
# HELP ignored comment
vllm:num_requests_running{engine=\"0\"} 2
vllm:num_requests_running{engine=\"1\"} 3
vllm:num_requests_waiting 64
vllm:num_preemptions_total 7
vllm:kv_cache_usage_perc{engine=\"0\"} 0.25
vllm:kv_cache_usage_perc{engine=\"1\"} 0.75
"""
        )

        self.assertEqual(sample.running, 5)
        self.assertEqual(sample.waiting, 64)
        self.assertEqual(sample.preemptions, 7)
        self.assertEqual(sample.kv_cache, 0.75)

    def test_metrics_parser_fails_closed_on_missing_or_invalid_signal(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing required signals"):
            staged.parse_vllm_metrics(b"vllm:num_requests_running 1\n")
        with self.assertRaisesRegex(ValueError, "invalid"):
            staged.parse_vllm_metrics(
                b"""
vllm:num_requests_running 1
vllm:num_requests_waiting -1
vllm:num_preemptions_total 0
vllm:gpu_cache_usage_perc 0.5
"""
            )

    def test_metrics_transport_retries_once_within_same_sample_budget(self) -> None:
        opener = MagicMock()
        opener.open.side_effect = [
            urllib.error.URLError(TimeoutError("first attempt timed out")),
            self._metrics_response(self._valid_metrics()),
        ]
        clock = MagicMock(side_effect=(0.0, 0.0, 5.0, 5.0))

        with patch.object(staged.urllib.request, "build_opener", return_value=opener):
            sample = staged.fetch_vllm_metrics(
                "http://gpu.invalid/v1",
                monotonic_clock=clock,
            )

        self.assertEqual(sample.running, 1)
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(
            [call.kwargs["timeout"] for call in opener.open.call_args_list],
            [4.5, 4.5],
        )

    def test_metrics_retry_uses_only_remaining_logical_budget(self) -> None:
        opener = MagicMock()
        opener.open.side_effect = [
            urllib.error.URLError(TimeoutError("first attempt timed out")),
            self._metrics_response(self._valid_metrics()),
        ]
        clock = MagicMock(side_effect=(0.0, 0.0, 6.0, 6.0))

        with patch.object(staged.urllib.request, "build_opener", return_value=opener):
            staged.fetch_vllm_metrics(
                "http://gpu.invalid/v1",
                monotonic_clock=clock,
            )

        self.assertEqual(
            [call.kwargs["timeout"] for call in opener.open.call_args_list],
            [4.5, 4.0],
        )

    def test_metrics_exhausted_budget_does_not_start_second_attempt(self) -> None:
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError(
            TimeoutError("first attempt consumed the budget")
        )
        clock = MagicMock(side_effect=(0.0, 0.0, 10.0))

        with (
            patch.object(staged.urllib.request, "build_opener", return_value=opener),
            self.assertRaisesRegex(RuntimeError, "budget exhausted"),
        ):
            staged.fetch_vllm_metrics(
                "http://gpu.invalid/v1",
                monotonic_clock=clock,
            )

        self.assertEqual(opener.open.call_count, 1)

    def test_metrics_late_success_does_not_count_as_a_valid_sample(self) -> None:
        opener = MagicMock()
        opener.open.return_value = self._metrics_response(self._valid_metrics())
        clock = MagicMock(side_effect=(0.0, 0.0, 10.001))

        with (
            patch.object(staged.urllib.request, "build_opener", return_value=opener),
            self.assertRaisesRegex(RuntimeError, "exceeded the logical"),
        ):
            staged.fetch_vllm_metrics(
                "http://gpu.invalid/v1",
                monotonic_clock=clock,
            )

        self.assertEqual(opener.open.call_count, 1)

    def test_metrics_transport_two_failures_remain_fail_closed(self) -> None:
        opener = MagicMock()
        opener.open.side_effect = [
            urllib.error.URLError(TimeoutError("first attempt timed out")),
            urllib.error.URLError(TimeoutError("second attempt timed out")),
        ]

        with (
            patch.object(staged.urllib.request, "build_opener", return_value=opener),
            self.assertRaisesRegex(RuntimeError, "after 2 transport attempts"),
        ):
            staged.fetch_vllm_metrics("http://gpu.invalid/v1")

        self.assertEqual(opener.open.call_count, 2)

    def test_metrics_monitor_tolerates_only_one_transport_sample_failure(self) -> None:
        monitor = staged._MetricsMonitor(
            sampler=MagicMock(
                side_effect=staged.MetricsTransportUnavailableError(
                    "budget exhausted"
                )
            ),
            expected_preemptions=0,
            monotonic_clock=MagicMock(
                side_effect=(0.0, 1.0, 2.0, 3.0, 4.0)
            ),
        )

        self.assertTrue(monitor._observe_once())
        self.assertIsNone(monitor.failure)
        self.assertEqual(len(monitor.sampling_failures), 1)
        self.assertFalse(monitor._observe_once())
        self.assertEqual(
            monitor.failure,
            "metrics_unavailable:MetricsTransportUnavailableError:budget exhausted",
        )

    def test_metrics_monitor_does_not_tolerate_invalid_metrics(self) -> None:
        monitor = staged._MetricsMonitor(
            sampler=MagicMock(side_effect=ValueError("missing required signals")),
            expected_preemptions=0,
            monotonic_clock=MagicMock(side_effect=(0.0, 1.0)),
        )

        self.assertFalse(monitor._observe_once())
        self.assertEqual(
            monitor.failure,
            "metrics_unavailable:ValueError:missing required signals",
        )
        self.assertEqual(monitor.sampling_failures, ())

    def test_metrics_monitor_does_not_tolerate_outage_after_high_waiting(self) -> None:
        monitor = staged._MetricsMonitor(
            sampler=MagicMock(
                side_effect=(
                    staged.MetricsSample(0, 1, 64, 0, 0.1),
                    staged.MetricsTransportUnavailableError("budget exhausted"),
                )
            ),
            expected_preemptions=0,
            monotonic_clock=MagicMock(
                side_effect=(0.0, 1.0, 2.0, 3.0, 4.0)
            ),
        )

        self.assertTrue(monitor._observe_once())
        self.assertFalse(monitor._observe_once())
        self.assertIsNotNone(monitor.failure)

    def test_metrics_monitor_rejects_a_gap_longer_than_logical_budget(self) -> None:
        monitor = staged._MetricsMonitor(
            sampler=MagicMock(
                side_effect=staged.MetricsTransportUnavailableError(
                    "late transport return"
                )
            ),
            expected_preemptions=0,
            monotonic_clock=MagicMock(side_effect=(0.0, 1.0, 12.0)),
        )

        self.assertFalse(monitor._observe_once())
        self.assertIsNotNone(monitor.failure)
        self.assertEqual(monitor.sampling_failures[0].duration_seconds, 11.0)

    def test_metrics_terminal_sample_transport_gap_is_never_tolerated(self) -> None:
        monitor = staged._MetricsMonitor(
            sampler=MagicMock(
                side_effect=staged.MetricsTransportUnavailableError(
                    "terminal transport gap"
                )
            ),
            expected_preemptions=0,
            monotonic_clock=MagicMock(side_effect=(0.0, 1.0, 2.0)),
        )
        monitor._thread_started = True

        with (
            patch.object(monitor._thread, "join"),
            patch.object(monitor._thread, "is_alive", return_value=False),
        ):
            monitor.stop()

        self.assertIsNotNone(monitor.failure)
        self.assertIsNone(monitor.terminal_sample_observed_seconds)

    def test_metrics_midstage_gap_requires_successful_terminal_sample(self) -> None:
        monitor = staged._MetricsMonitor(
            sampler=MagicMock(
                side_effect=(
                    staged.MetricsTransportUnavailableError("midstage gap"),
                    staged.MetricsSample(0, 0, 0, 0, 0),
                )
            ),
            expected_preemptions=0,
            monotonic_clock=MagicMock(
                side_effect=(0.0, 1.0, 2.0, 3.0, 4.0)
            ),
        )
        self.assertTrue(monitor._observe_once())
        monitor._thread_started = True

        with (
            patch.object(monitor._thread, "join"),
            patch.object(monitor._thread, "is_alive", return_value=False),
        ):
            monitor.stop()

        self.assertIsNone(monitor.failure)
        self.assertEqual(monitor.terminal_sample_observed_seconds, 4.0)

    def test_metrics_midstage_and_terminal_gaps_fail_stage(self) -> None:
        monitor = staged._MetricsMonitor(
            sampler=MagicMock(
                side_effect=staged.MetricsTransportUnavailableError("transport gap")
            ),
            expected_preemptions=0,
            monotonic_clock=MagicMock(
                side_effect=(0.0, 1.0, 2.0, 3.0, 4.0)
            ),
        )
        self.assertTrue(monitor._observe_once())
        monitor._thread_started = True

        with (
            patch.object(monitor._thread, "join"),
            patch.object(monitor._thread, "is_alive", return_value=False),
        ):
            monitor.stop()

        self.assertIsNotNone(monitor.failure)
        self.assertIsNone(monitor.terminal_sample_observed_seconds)

    def test_metrics_http_errors_do_not_retry(self) -> None:
        for status in (429, 500):
            with self.subTest(status=status):
                opener = MagicMock()
                opener.open.side_effect = urllib.error.HTTPError(
                    "http://gpu.invalid/metrics",
                    status,
                    "failure",
                    hdrs=Message(),
                    fp=None,
                )
                with (
                    patch.object(
                        staged.urllib.request,
                        "build_opener",
                        return_value=opener,
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    staged.fetch_vllm_metrics("http://gpu.invalid/v1")
                self.assertEqual(opener.open.call_count, 1)

    def test_metrics_invalid_responses_do_not_retry(self) -> None:
        cases = (
            (b"vllm:num_requests_running 1\n", ValueError),
            (b"x" * (staged._MAX_METRICS_BYTES + 1), RuntimeError),
        )
        for payload, error_type in cases:
            with self.subTest(error_type=error_type.__name__):
                opener = MagicMock()
                opener.open.return_value = self._metrics_response(payload)
                with (
                    patch.object(
                        staged.urllib.request,
                        "build_opener",
                        return_value=opener,
                    ),
                    self.assertRaises(error_type),
                ):
                    staged.fetch_vllm_metrics("http://gpu.invalid/v1")
                self.assertEqual(opener.open.call_count, 1)

    def test_preemption_change_and_sustained_waiting_abort(self) -> None:
        healthy = staged.MetricsSample(0, 1, 64, 5, 0.5)
        failure, waiting_since = staged.metric_abort_reason(
            healthy,
            baseline_preemptions=5,
            waiting_since=None,
            observed_at=10,
        )
        self.assertIsNone(failure)
        self.assertEqual(waiting_since, 10)

        failure, waiting_since = staged.metric_abort_reason(
            healthy,
            baseline_preemptions=5,
            waiting_since=waiting_since,
            observed_at=39.999,
        )
        self.assertIsNone(failure)
        failure, _ = staged.metric_abort_reason(
            healthy,
            baseline_preemptions=5,
            waiting_since=waiting_since,
            observed_at=40,
        )
        self.assertEqual(failure, "waiting_gte_64_for_30_seconds")

        preempted = staged.MetricsSample(0, 1, 0, 6, 0.5)
        failure, _ = staged.metric_abort_reason(
            preempted,
            baseline_preemptions=5,
            waiting_since=None,
            observed_at=0,
        )
        self.assertEqual(failure, "preemption_counter_changed")

    def test_waiting_timer_resets_below_threshold(self) -> None:
        sample = staged.MetricsSample(0, 1, 63, 0, 0.5)
        failure, waiting_since = staged.metric_abort_reason(
            sample,
            baseline_preemptions=0,
            waiting_since=10,
            observed_at=20,
        )
        self.assertIsNone(failure)
        self.assertIsNone(waiting_since)

    def test_stage_metrics_require_idle_baseline_and_observed_activity(self) -> None:
        idle = staged.MetricsSample(0, 0, 0, 0, 0)
        active = staged.MetricsSample(1, 1, 0, 0, 0.1)

        self.assertFalse(staged._metrics_prove_staged_activity(idle, ()))
        self.assertFalse(
            staged._metrics_prove_staged_activity(
                idle,
                (staged.MetricsSample(1, 0, 0, 0, 0),),
            )
        )
        self.assertTrue(staged._metrics_prove_staged_activity(idle, (active,)))

    def test_metrics_receipt_preserves_waiting_p95(self) -> None:
        baseline = staged.MetricsSample(0, 0, 0, 0, 0)
        samples = tuple(
            staged.MetricsSample(index, index, waiting, 0, index / 10)
            for index, waiting in enumerate((0, 1, 2, 3, 40), start=1)
        )

        summary = staged._metrics_summary(
            baseline,
            samples,
            (
                staged.MetricsSamplingFailure(
                    observed_seconds=3.5,
                    duration_seconds=8.0,
                    failure="MetricsTransportUnavailableError:timed out",
                ),
            ),
            terminal_sample_observed_seconds=6.0,
        )

        self.assertEqual(summary["sample_count"], 5)
        self.assertEqual(summary["percentiles"]["waiting_p95"], 40)
        self.assertEqual(summary["percentiles"]["running_p95"], 5)
        self.assertEqual(len(summary["sampling_failures"]), 1)
        self.assertEqual(summary["terminal_sample_observed_seconds"], 6.0)

    def test_orchestrator_evidence_requires_queue_for_8_and_exact_deltas(self) -> None:
        baseline = self._health(completed=100, failed=2)
        processing_only = (
            staged.OrchestratorSample(0.1, 0, 3, 100, 2),
        )
        terminal = self._health(completed=108, failed=2)

        self.assertEqual(
            staged._orchestrator_evidence_failure(
                concurrency=8,
                baseline=baseline,
                samples=processing_only,
                terminal=terminal,
            ),
            "orchestrator_queue_not_observed",
        )
        with_queue = (
            *processing_only,
            staged.OrchestratorSample(0.2, 5, 3, 100, 2),
        )
        self.assertIsNone(
            staged._orchestrator_evidence_failure(
                concurrency=8,
                baseline=baseline,
                samples=with_queue,
                terminal=terminal,
            )
        )
        self.assertEqual(
            staged._orchestrator_evidence_failure(
                concurrency=8,
                baseline=baseline,
                samples=with_queue,
                terminal=self._health(completed=107, failed=2),
            ),
            "orchestrator_completed_delta_mismatch",
        )
        self.assertEqual(
            staged._orchestrator_evidence_failure(
                concurrency=8,
                baseline=baseline,
                samples=with_queue,
                terminal=self._health(completed=108, failed=3),
            ),
            "orchestrator_failed_delta_changed",
        )

    def test_orchestrator_monitor_fails_closed_above_three_processing(self) -> None:
        monitor = staged._OrchestratorMonitor(
            sampler=lambda: self._health(processing=4),
        )

        monitor._run()

        self.assertEqual(
            monitor.failure,
            "orchestrator_processing_exceeded_3",
        )
        self.assertEqual(monitor.samples[0].processing_tasks, 4)

    def test_unapproved_stage_is_rejected_before_metrics_or_parse(self) -> None:
        sampled = False

        def sample() -> staged.MetricsSample:
            nonlocal sampled
            sampled = True
            return staged.MetricsSample(0, 0, 0, 0, 0)

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "not an approved stage"):
                staged._run_stage(
                    concurrency=32,
                    run_root=Path(tmp),
                    input_bytes=b"pdf",
                    input_digest=hashlib.sha256(b"pdf").hexdigest(),
                    input_logical_name="frozen.pdf",
                    mineru_bin=Path("/unused/mineru"),
                    api_url="http://unused-api",
                    inference_upstream_url="http://unused-upstream/v1",
                    runtime_identity="sha256:" + "a" * 64,
                    timeout_seconds=1,
                    expected_preemptions=0,
                    metrics_sampler=sample,
                    orchestrator_sampler=lambda: self._health(),
                    orchestrator_idle_waiter=lambda: (self._health(), 0.0),
                )
        self.assertFalse(sampled)

    def test_between_stage_preemption_change_fails_before_parse(self) -> None:
        health = self._health(completed=10)
        with tempfile.TemporaryDirectory() as tmp:
            result = staged._run_stage(
                concurrency=4,
                run_root=Path(tmp),
                input_bytes=b"pdf",
                input_digest=hashlib.sha256(b"pdf").hexdigest(),
                input_logical_name="frozen.pdf",
                mineru_bin=Path("/unused/mineru"),
                api_url="http://unused-api",
                inference_upstream_url="http://unused-upstream/v1",
                runtime_identity="sha256:" + "a" * 64,
                timeout_seconds=1,
                expected_preemptions=3,
                metrics_sampler=lambda: staged.MetricsSample(0, 0, 0, 4, 0),
                orchestrator_sampler=lambda: health,
                orchestrator_idle_waiter=lambda: (health, 0.0),
            )

        self.assertEqual(result["status"], "fail")
        self.assertEqual(
            result["failure"], "preemption_counter_changed_between_stages"
        )
        self.assertEqual(result["documents"], [])

    def test_real_parser_path_receives_fixed_api_and_internal_upstream(self) -> None:
        input_bytes = b"%PDF-1.4 frozen fixture"
        digest = hashlib.sha256(input_bytes).hexdigest()
        provider = SimpleNamespace(
            pages=[object()] * 7,
            blocks=[object(), object()],
            parser_version="3.4.4",
            bundle_sha256="sha256:" + "b" * 64,
        )
        observed: dict[str, object] = {}

        def parse(**kwargs: object) -> SimpleNamespace:
            input_pdf = kwargs["input_pdf"]
            options = kwargs["options"]
            assert isinstance(input_pdf, Path)
            assert isinstance(options, staged.ParserOptions)
            observed["bytes"] = input_pdf.read_bytes()
            observed["api_url"] = options.api_url
            observed["server_url"] = options.server_url
            observed["concurrency"] = options.http_request_concurrency
            observed["runtime_identity"] = options.runtime_bundle_identity_sha256
            observed["source_sha256"] = kwargs["source_pdf_sha256"]
            return SimpleNamespace(provider_document=provider)

        with tempfile.TemporaryDirectory() as tmp:
            parser = SimpleNamespace(parse=parse)
            with (
                patch.object(staged, "MinerUProcess"),
                patch.object(
                    staged,
                    "MinerUMediumDocumentParser",
                    return_value=parser,
                ),
            ):
                result = staged._parse_frozen_copy(
                    copy_index=1,
                    stage_root=Path(tmp),
                    input_bytes=input_bytes,
                    input_digest=digest,
                    input_logical_name="representative.pdf",
                    mineru_bin=Path("/fake/mineru"),
                    api_url="http://127.0.0.1:30000",
                    inference_upstream_url="http://mineru-vllm:30000/v1",
                    runtime_identity="sha256:" + "a" * 64,
                    timeout_seconds=30,
                )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(observed["bytes"], input_bytes)
        self.assertEqual(observed["api_url"], "http://127.0.0.1:30000")
        self.assertEqual(
            observed["server_url"], "http://mineru-vllm:30000/v1"
        )
        self.assertIsNone(observed["concurrency"])
        self.assertEqual(observed["runtime_identity"], "sha256:" + "a" * 64)
        self.assertEqual(observed["source_sha256"], f"sha256:{digest}")

    def test_single_page_smoke_fixture_cannot_pass_as_staged_load(self) -> None:
        input_bytes = b"%PDF-1.4 one page"
        digest = hashlib.sha256(input_bytes).hexdigest()
        provider = SimpleNamespace(
            pages=[object()],
            blocks=[],
            parser_version="3.4.4",
            bundle_sha256="sha256:" + "b" * 64,
        )
        parser = SimpleNamespace(
            parse=lambda **_kwargs: SimpleNamespace(provider_document=provider)
        )

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(staged, "MinerUProcess"),
            patch.object(
                staged,
                "MinerUMediumDocumentParser",
                return_value=parser,
            ),
        ):
            result = staged._parse_frozen_copy(
                copy_index=1,
                stage_root=Path(tmp),
                input_bytes=input_bytes,
                input_digest=digest,
                input_logical_name="smoke.pdf",
                mineru_bin=Path("/fake/mineru"),
                api_url="http://127.0.0.1:30000",
                inference_upstream_url="http://mineru-vllm:30000/v1",
                runtime_identity="sha256:" + "a" * 64,
                timeout_seconds=30,
            )

        self.assertEqual(result["status"], "fail")
        self.assertIn("at least 7 pages", result["failure_detail"])

    def test_failure_detail_keeps_both_diagnostic_edges(self) -> None:
        detail = "startup evidence " + ("x" * 800) + " terminal root cause"

        safe = staged._safe_detail(detail)

        self.assertEqual(len(safe), staged._MAX_SAFE_DETAIL_CHARS)
        self.assertTrue(safe.startswith("startup evidence"))
        self.assertIn(staged._SAFE_DETAIL_TRUNCATION_MARKER, safe)
        self.assertTrue(safe.endswith("terminal root cause"))

    def test_failure_class_uses_complete_diagnostic_before_excerpt(self) -> None:
        exc = RuntimeError(
            "startup "
            + ("x" * 300)
            + " HTTP 500 remote failure "
            + ("y" * 300)
            + " terminal"
        )

        outcome = staged._failed_document_outcome(
            1,
            "a" * 64,
            exc,
        )

        self.assertEqual(outcome["failure_class"], "remote_5xx")
        self.assertNotIn("HTTP 500", outcome["failure_detail"])
        self.assertGreater(outcome["failure_detail_chars"], 500)
        self.assertRegex(
            outcome["failure_detail_sha256"],
            r"^sha256:[a-f0-9]{64}$",
        )

    def test_stage_receipt_finalizes_every_future_after_abort(self) -> None:
        release = threading.Event()

        def parse(*, copy_index: int, **_kwargs: object) -> dict[str, object]:
            if copy_index == 1:
                return {
                    "copy_index": 1,
                    "status": "fail",
                    "failure_class": "parse_failure",
                    "failure_detail": "ParserTaskError:primary failure",
                }
            self.assertTrue(release.wait(timeout=2))
            return {
                "copy_index": copy_index,
                "status": "fail",
                "failure_class": "parse_failure",
                "failure_detail": "ParserCancelledError:cancelled by stage abort",
            }

        def terminate(*_args: object, **_kwargs: object) -> int:
            release.set()
            return 3

        metrics = staged.MetricsSample(0, 0, 0, 0, 0)
        health = self._health()
        idle_waiter = MagicMock(side_effect=((health, 0.0), (health, 0.0)))
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(staged, "_parse_frozen_copy", side_effect=parse),
            patch.object(staged, "mineru_api_temp_dirs", return_value=set()),
            patch.object(staged, "_wait_for_process_cleanup", return_value={}),
            patch.object(
                staged,
                "terminate_active_mineru_processes",
                side_effect=terminate,
            ),
        ):
            result = staged._run_stage(
                concurrency=4,
                run_root=Path(tmp),
                input_bytes=b"pdf",
                input_digest=hashlib.sha256(b"pdf").hexdigest(),
                input_logical_name="frozen.pdf",
                mineru_bin=Path("/unused/mineru"),
                api_url="http://unused-api",
                inference_upstream_url="http://unused-upstream/v1",
                runtime_identity="sha256:" + "a" * 64,
                timeout_seconds=1,
                expected_preemptions=0,
                metrics_sampler=lambda: metrics,
                orchestrator_sampler=lambda: health,
                orchestrator_idle_waiter=idle_waiter,
            )

        statuses = [item["status"] for item in result["documents"]]
        self.assertEqual(statuses[0], "fail")
        self.assertNotIn("not_started", statuses)
        self.assertEqual(statuses[1:], ["cancelled_after_stage_abort"] * 3)
        self.assertEqual(
            [item.get("failure_class") for item in result["documents"][1:]],
            ["stage_abort"] * 3,
        )

    def test_process_snapshot_classifiers_reject_producers_and_mineru(self) -> None:
        processes = {
            10: "python -m disclosure_anchor.cli.worker loop",
            11: "/venv/bin/mineru -p input.pdf",
            12: "python -m mineru.cli --help",
            14: "/venv/bin/python /venv/bin/mineru -p input.pdf",
            15: "uvicorn mineru.cli.fast_api:app",
            13: "python unrelated.py",
        }

        self.assertEqual(set(active_disclosure_producers(processes)), {10})
        self.assertEqual(set(mineru_processes(processes)), {11, 12, 14, 15})

    def test_receipt_writer_is_new_only_and_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / "receipt.json"
            staged._write_new_json(receipt, {"status": "pass"})
            first_bytes = receipt.read_bytes()

            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                staged._write_new_json(receipt, {"status": "fail"})
            self.assertEqual(receipt.read_bytes(), first_bytes)

    def test_operational_failure_writes_fail_receipt_without_remote_access(self) -> None:
        input_bytes = b"%PDF-1.4 frozen fixture"
        input_sha = "sha256:" + hashlib.sha256(input_bytes).hexdigest()
        runtime_identity = "sha256:" + "a" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / "fixture.pdf"
            fixture.write_bytes(input_bytes)
            mineru = root / "mineru"
            mineru.write_text("executable", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            receipt = root / "receipt.json"
            client = SimpleNamespace(
                package_set_sha256="sha256:" + "b" * 64,
                content_package_versions={},
            )
            with (
                patch.dict(os.environ, {"MINERU_PROCESSING_WINDOW_SIZE": "16"}),
                patch.object(staged, "mineru_api_temp_dirs", return_value=set()),
                patch.object(staged, "process_snapshot", return_value={}),
                patch.object(staged, "_wait_for_process_cleanup", return_value={}),
                patch.object(staged, "client_bundle_identity", return_value=client),
                patch.object(staged, "writer_code_digest", return_value="sha256:" + "c" * 64),
                patch.object(
                    staged,
                    "verify_runtime_manifest_payload",
                    side_effect=ValueError("manifest drift"),
                ),
            ):
                result = staged.main(
                    [
                        "--runtime-manifest",
                        str(manifest),
                        "--receipt-out",
                        str(receipt),
                        "--input",
                        str(fixture),
                        "--expected-input-sha256",
                        input_sha,
                        "--mineru-bin",
                        str(mineru),
                        "--api-url",
                        "http://unused-api",
                        "--observability-url",
                        "http://unused-observability/v1",
                        "--inference-upstream-url",
                        "http://unused-upstream/v1",
                        "--runtime-bundle-identity",
                        runtime_identity,
                    ]
                )

            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(result, 2)
            self.assertEqual(payload["status"], "fail")
            self.assertEqual(payload["schema"], "mineru_staged_load_receipt.v2")
            self.assertEqual(
                payload["topology"]["api_endpoint_sha256"],
                "sha256:" + hashlib.sha256(b"http://unused-api").hexdigest(),
            )
            self.assertEqual(payload["database_access"], "none")
            self.assertEqual(payload["queue_access"], "none")
            self.assertIn("manifest drift", payload["failure"])
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)

    def test_main_routes_three_urls_and_emits_fixed_api_v2_receipt(self) -> None:
        input_bytes = b"%PDF-1.4 frozen fixture"
        input_sha = "sha256:" + hashlib.sha256(input_bytes).hexdigest()
        runtime_identity = "sha256:" + "a" * 64
        api_url = "http://127.0.0.1:30000"
        observability_url = "http://127.0.0.1:30002/v1"
        inference_upstream_url = "http://mineru-vllm:30000/v1"
        topology = {
            name: "sha256:"
            + hashlib.sha256(url.rstrip("/").encode()).hexdigest()
            for name, url in {
                "api_endpoint_sha256": api_url,
                "observability_endpoint_sha256": observability_url,
                "inference_upstream_sha256": inference_upstream_url,
            }.items()
        }
        verified = SimpleNamespace(
            manifest={
                "topology": topology,
                "orchestrator": {
                    "task_retention_seconds": 86400,
                    "task_cleanup_interval_seconds": 300,
                },
            },
            identity_sha256=runtime_identity,
            orchestrator_identity_sha256="sha256:" + "d" * 64,
            provider_identity_sha256="sha256:" + "e" * 64,
            served_model_id="mineru-model",
        )
        metrics = staged.MetricsSample(0, 0, 0, 0, 0)
        health = self._health()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / "fixture.pdf"
            fixture.write_bytes(input_bytes)
            mineru = root / "mineru"
            mineru.write_text("executable", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            receipt = root / "receipt.json"
            client = SimpleNamespace(
                package_set_sha256="sha256:" + "b" * 64,
                content_package_versions={},
            )
            run_stage = MagicMock(
                side_effect=lambda **kwargs: {
                    "status": "pass",
                    "client_document_concurrency": kwargs["concurrency"],
                }
            )
            metrics_fetch = MagicMock(return_value=metrics)
            health_fetch = MagicMock(return_value=health)
            idle_waiter = MagicMock(return_value=(health, 0.0))
            with (
                patch.dict(os.environ, {"MINERU_PROCESSING_WINDOW_SIZE": "16"}),
                patch.object(staged, "mineru_api_temp_dirs", return_value=set()),
                patch.object(staged, "process_snapshot", return_value={}),
                patch.object(staged, "_wait_for_process_cleanup", return_value={}),
                patch.object(staged, "client_bundle_identity", return_value=client),
                patch.object(
                    staged,
                    "writer_code_digest",
                    return_value="sha256:" + "c" * 64,
                ),
                patch.object(
                    staged,
                    "verify_runtime_manifest_payload",
                    return_value=verified,
                ),
                patch.object(staged, "probe_mineru_served_model") as probe,
                patch.object(staged, "fetch_vllm_metrics", metrics_fetch),
                patch.object(
                    staged,
                    "fetch_mineru_orchestrator_health",
                    health_fetch,
                ),
                patch.object(
                    staged,
                    "wait_for_mineru_orchestrator_idle",
                    idle_waiter,
                ),
                patch.object(staged, "_run_stage", run_stage),
            ):
                result = staged.main(
                    [
                        "--runtime-manifest",
                        str(manifest),
                        "--receipt-out",
                        str(receipt),
                        "--input",
                        str(fixture),
                        "--expected-input-sha256",
                        input_sha,
                        "--mineru-bin",
                        str(mineru),
                        "--api-url",
                        api_url,
                        "--observability-url",
                        observability_url,
                        "--inference-upstream-url",
                        inference_upstream_url,
                        "--runtime-bundle-identity",
                        runtime_identity,
                    ]
                )
                first_call = run_stage.call_args_list[0].kwargs
                first_call["metrics_sampler"]()
                first_call["orchestrator_sampler"]()
                first_call["orchestrator_idle_waiter"]()

            self.assertEqual(result, 0)
            probe.assert_called_once_with(
                observability_url,
                expected_model_id="mineru-model",
            )
            self.assertEqual(metrics_fetch.call_args_list[0].args, (observability_url,))
            self.assertEqual(run_stage.call_count, 3)
            self.assertEqual(first_call["api_url"], api_url)
            self.assertEqual(
                first_call["inference_upstream_url"],
                inference_upstream_url,
            )
            self.assertEqual(metrics_fetch.call_args_list[-1].args, (observability_url,))
            self.assertEqual(health_fetch.call_args.args, (api_url,))
            self.assertEqual(idle_waiter.call_args.args, (api_url,))
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["schema"], "mineru_staged_load_receipt.v2")
            self.assertEqual(payload["topology"], topology)
            self.assertEqual(
                payload["fixed_stage_client_document_concurrency"],
                [4, 8, 16],
            )
            self.assertEqual(payload["orchestrator_task_concurrency"], 3)
            self.assertEqual(payload["orchestrator_inference_concurrency"], 7)
            self.assertEqual(
                payload["effective_inference_request_upper_bound"],
                21,
            )


if __name__ == "__main__":
    unittest.main()
