"""Deterministic, DB-free tests for the fixed MinerU staged-load gate."""

from __future__ import annotations

from contextlib import redirect_stderr
from concurrent.futures import ThreadPoolExecutor
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
import time
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

import scripts.mineru_staged_load as staged
from disclosure_anchor.adapters.runtime.mineru_process_isolation import (
    active_disclosure_producers,
    mineru_processes,
)


def _host_capacity_sample(
    *,
    container_id_suffix: str = "1",
    available_bytes: int = 8192,
) -> dict[str, object]:
    containers = []
    for index, name in enumerate(
        ("mineru-api", "mineru-api-proxy", "mineru-openai-server"),
        start=1,
    ):
        containers.append(
            {
                "name": name,
                "id": (str(index) if container_id_suffix == "1" else container_id_suffix)
                * 64,
                "started_at_utc": "2026-08-25T00:00:00+00:00",
                "restart_count": 0,
                "oom_killed": False,
                "exit_code": 0,
                "running": True,
                "status": "running",
                "health": "healthy",
                "pid": 100 + index,
                "memory_current_bytes": 2048,
                "memory_max_bytes": None,
                "memory_events": {"oom": 0, "oom_kill": 0, "high": 0},
                "pid1_rss_bytes": 1024,
                "pid1_rss_hwm_bytes": 2048,
                "docker_vm_memory_total_bytes": 16384,
                "docker_vm_memory_available_bytes": available_bytes,
            }
        )
    return {
        "schema": "mineru-host-capacity-sample.v1",
        "observed_at_utc": "2026-08-25T00:00:01+00:00",
        "collector_path": staged.MINERU_WINDOWS_COLLECTOR_PATH,
        "collector_sha256": "sha256:" + "f" * 64,
        "windows_node_identity_sha256": "sha256:" + "0" * 64,
        "containers": containers,
    }


class MinerUStagedLoadTests(unittest.TestCase):
    @staticmethod
    def _corpus(count: int = 16) -> tuple[staged.FrozenCorpusInput, ...]:
        return tuple(
            staged.FrozenCorpusInput(
                logical_name=f"real-{index:02d}.pdf",
                payload=f"pdf-{index}".encode(),
                digest=hashlib.sha256(f"pdf-{index}".encode()).hexdigest(),
                page_count=600
                if index == count
                else (100 if index == count - 1 else 7),
                workload_class=(
                    "huge"
                    if index == count
                    else ("heavy" if index == count - 1 else "regular")
                ),
            )
            for index in range(1, count + 1)
        )

    @classmethod
    def _corpus_fixture(
        cls, identity: str
    ) -> tuple[tuple[staged.FrozenCorpusInput, ...], dict[str, object]]:
        corpus = cls._corpus()
        return corpus, {
            "profile": "operator_frozen_heterogeneous_v2",
            "logical_name": "corpus.json",
            "sha256": identity,
            "bytes": sum(len(item.payload) for item in corpus),
            "minimum_required_pages": 7,
            "documents": [item.evidence() for item in corpus],
        }

    @staticmethod
    def _host_observer_files(root: Path) -> tuple[Path, Path]:
        identity = root / "observer-key"
        known_hosts = root / "observer-known-hosts"
        identity.write_text("private-key", encoding="utf-8")
        known_hosts.write_text(
            "100.64.0.1 ssh-ed25519 a2V5\n",
            encoding="utf-8",
        )
        identity.chmod(0o600)
        known_hosts.chmod(0o600)
        return identity, known_hosts

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
            max_concurrent_requests=1,
            processing_window_size=16,
            task_retention_seconds=600,
            task_cleanup_interval_seconds=30,
        )

    def test_fixed_envelope_has_no_operator_stage_override(self) -> None:
        self.assertEqual(staged.STAGE_DOCUMENT_COUNTS, (4, 8, 16))
        self.assertEqual(staged.ORCHESTRATOR_INFERENCE_CONCURRENCY, 7)
        self.assertEqual(staged.RECEIPT_SCHEMA, "mineru_staged_load_receipt.v6")
        self.assertEqual(staged.RECEIPT_SCHEMA_VERSION, 6)

        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            staged._parse_args(
                [
                    "--runtime-manifest",
                    "manifest.json",
                    "--receipt-out",
                    "receipt.json",
                    "--corpus-manifest",
                    "corpus.json",
                    "--expected-corpus-sha256",
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

        staged.execute_fixed_stage_sequence(run)

        self.assertEqual(calls, [4, 8])

    def test_each_stage_selects_an_exact_heterogeneous_set(self) -> None:
        # Keep both non-regular strata strictly beyond the largest stage
        # prefix.  This prevents a future stage-16-only regression back to
        # ``corpus[:document_count]`` from passing the selector contract.
        corpus = self._corpus(count=18)

        for document_count in staged.STAGE_DOCUMENT_COUNTS:
            with self.subTest(document_count=document_count):
                selected = staged._select_stage_inputs(
                    corpus,
                    document_count=document_count,
                )
                self.assertEqual(len(selected), document_count)
                self.assertEqual(len({item.digest for item in selected}), document_count)
                self.assertEqual(
                    {item.workload_class for item in selected[:3]},
                    {"regular", "heavy", "huge"},
                )
                expected_names = ["real-01.pdf", "real-17.pdf", "real-18.pdf"] + [
                    f"real-{index:02d}.pdf"
                    for index in range(2, document_count - 1)
                ]
                self.assertEqual(
                    [item.logical_name for item in selected],
                    expected_names,
                )
                expected_by_name = {item.logical_name: item.digest for item in corpus}
                self.assertEqual(
                    [item.digest for item in selected],
                    [expected_by_name[name] for name in expected_names],
                )

        with self.assertRaisesRegex(ValueError, "no huge PDF"):
            staged._select_stage_inputs(
                corpus[:-1],
                document_count=4,
            )

    def test_stage_admission_caps_clients_and_keeps_huge_exclusive(self) -> None:
        admission = staged._StageAdmission(
            outstanding_window=2,
            copy_indices=(1, 2, 3, 4, 5, 6),
        )
        state_lock = threading.Lock()
        active = 0
        huge_active = False
        violations: list[str] = []

        def operation(copy_index: int, workload_class: str) -> dict[str, object]:
            def inner() -> dict[str, object]:
                nonlocal active, huge_active
                with state_lock:
                    if workload_class == "huge" and active != 0:
                        violations.append("huge-overlapped")
                    if workload_class != "huge" and huge_active:
                        violations.append("regular-overlapped-huge")
                    active += 1
                    huge_active = workload_class == "huge"
                time.sleep(0.01)
                with state_lock:
                    active -= 1
                    if workload_class == "huge":
                        huge_active = False
                return {"status": "pass"}

            return admission.run(
                copy_index=copy_index,
                workload_class=workload_class,
                operation=inner,
            )

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [
                executor.submit(operation, copy_index, workload_class)
                for copy_index, workload_class in enumerate(
                    (
                        "regular",
                        "heavy",
                        "huge",
                        "regular",
                        "heavy",
                        "regular",
                    ),
                    start=1,
                )
            ]
            for future in futures:
                self.assertEqual(future.result(), {"status": "pass"})

        self.assertEqual(violations, [])
        self.assertLessEqual(admission.peak, 2)
        self.assertEqual(
            admission.evidence()["admission_order_copy_indices"],
            [1, 2, 3, 4, 5, 6],
        )

    def test_stage_admission_reaches_window_before_huge_can_enter(self) -> None:
        admission = staged._StageAdmission(
            outstanding_window=2,
            copy_indices=(1, 2, 3),
        )
        both_non_huge_entered = threading.Barrier(3, timeout=1)
        release_non_huge = threading.Event()
        huge_entered = threading.Event()
        release_huge = threading.Event()

        def non_huge(copy_index: int, workload_class: str) -> dict[str, object]:
            return admission.run(
                copy_index=copy_index,
                workload_class=workload_class,
                operation=lambda: _hold_non_huge(),
            )

        def _hold_non_huge() -> dict[str, object]:
            both_non_huge_entered.wait()
            self.assertTrue(release_non_huge.wait(timeout=1))
            return {"status": "pass"}

        def hold_huge() -> dict[str, object]:
            huge_entered.set()
            self.assertTrue(release_huge.wait(timeout=1))
            return {"status": "pass"}

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=3) as executor:
            regular = executor.submit(non_huge, 1, "regular")
            heavy = executor.submit(non_huge, 2, "heavy")
            both_non_huge_entered.wait()
            huge = executor.submit(
                admission.run,
                copy_index=3,
                workload_class="huge",
                operation=hold_huge,
            )
            self.assertFalse(huge_entered.wait(timeout=0.05))
            self.assertEqual(admission.peak, 2)
            release_non_huge.set()
            self.assertTrue(huge_entered.wait(timeout=1))
            release_huge.set()
            self.assertEqual(regular.result(), {"status": "pass"})
            self.assertEqual(heavy.result(), {"status": "pass"})
            self.assertEqual(huge.result(), {"status": "pass"})

    def test_stage_admission_is_fifo_under_reverse_thread_start(self) -> None:
        admission = staged._StageAdmission(
            outstanding_window=2,
            copy_indices=(1, 2, 3, 4, 5, 6),
        )

        def run(copy_index: int) -> dict[str, object]:
            return admission.run(
                copy_index=copy_index,
                workload_class="regular",
                operation=lambda: ({"status": "pass"}),
            )

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(run, index) for index in range(6, 0, -1)]
            for future in futures:
                self.assertEqual(future.result(), {"status": "pass"})
        admission.close()

        evidence = admission.evidence()
        self.assertEqual(evidence["admission_order_copy_indices"], [1, 2, 3, 4, 5, 6])
        self.assertEqual(
            [record["admission_ordinal"] for record in evidence["records"]],
            list(range(6)),
        )

    def test_stage_admission_huge_fifo_head_blocks_later_regular(self) -> None:
        admission = staged._StageAdmission(
            outstanding_window=2,
            copy_indices=(1, 2, 3),
        )
        first_entered = threading.Event()
        release_first = threading.Event()
        huge_entered = threading.Event()
        release_huge = threading.Event()
        later_regular_entered = threading.Event()

        def first() -> dict[str, object]:
            first_entered.set()
            self.assertTrue(release_first.wait(timeout=1))
            return {"status": "pass"}

        def huge() -> dict[str, object]:
            huge_entered.set()
            self.assertTrue(release_huge.wait(timeout=1))
            return {"status": "pass"}

        with ThreadPoolExecutor(max_workers=3) as executor:
            first_future = executor.submit(
                admission.run,
                copy_index=1,
                workload_class="regular",
                operation=first,
            )
            self.assertTrue(first_entered.wait(timeout=1))
            later_future = executor.submit(
                admission.run,
                copy_index=3,
                workload_class="regular",
                operation=lambda: (
                    later_regular_entered.set() or {"status": "pass"}
                ),
            )
            huge_future = executor.submit(
                admission.run,
                copy_index=2,
                workload_class="huge",
                operation=huge,
            )
            release_first.set()
            self.assertTrue(huge_entered.wait(timeout=1))
            self.assertFalse(later_regular_entered.wait(timeout=0.05))
            release_huge.set()
            self.assertEqual(first_future.result(), {"status": "pass"})
            self.assertEqual(huge_future.result(), {"status": "pass"})
            self.assertEqual(later_future.result(), {"status": "pass"})

        admission.close()
        self.assertEqual(
            admission.evidence()["admission_order_copy_indices"],
            [1, 2, 3],
        )

    def test_stage_admission_releases_after_exception_and_close_wakes_waiter(
        self,
    ) -> None:
        admission = staged._StageAdmission(
            outstanding_window=1,
            copy_indices=(1, 2),
        )
        with self.assertRaisesRegex(RuntimeError, "boom"):
            admission.run(
                copy_index=1,
                workload_class="regular",
                operation=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            )
        with self.assertRaisesRegex(
            staged._StageAdmissionClosedError,
            "earlier failure",
        ):
            admission.run(
                copy_index=2,
                workload_class="regular",
                operation=lambda: {"status": "pass"},
            )

        admission = staged._StageAdmission(
            outstanding_window=1,
            copy_indices=(1, 2),
        )
        occupied = threading.Event()
        release = threading.Event()
        waiter_done = threading.Event()
        waiter_errors: list[type[BaseException]] = []

        def hold() -> dict[str, object]:
            occupied.set()
            self.assertTrue(release.wait(timeout=1))
            return {"status": "pass"}

        holder = threading.Thread(
            target=lambda: admission.run(
                copy_index=1,
                workload_class="regular",
                operation=hold,
            )
        )
        holder.start()
        self.assertTrue(occupied.wait(timeout=1))

        def wait_for_slot() -> None:
            try:
                admission.run(
                    copy_index=2,
                    workload_class="regular",
                    operation=lambda: {"status": "unexpected"},
                )
            except BaseException as exc:  # test captures the exact wake-up path
                waiter_errors.append(type(exc))
            finally:
                waiter_done.set()

        waiter = threading.Thread(target=wait_for_slot)
        waiter.start()
        admission.close()
        self.assertTrue(waiter_done.wait(timeout=1))
        release.set()
        holder.join(timeout=1)
        waiter.join(timeout=1)
        self.assertEqual(waiter_errors, [staged._StageAdmissionClosedError])

    def test_stage_admission_rechecks_latched_failure_before_next_submit(
        self,
    ) -> None:
        abort_latch = staged._StageAbortLatch()
        admission = staged._StageAdmission(
            outstanding_window=1,
            copy_indices=(1, 2),
            abort_latch=abort_latch,
        )
        occupied = threading.Event()
        release = threading.Event()
        violating_sample_committed = threading.Event()
        allow_failure_publish = threading.Event()
        second_operation_called = threading.Event()
        samples = [
            _host_capacity_sample(available_bytes=8192),
            _host_capacity_sample(available_bytes=1024),
            _host_capacity_sample(available_bytes=6144),
        ]
        monitor = staged._HostCapacityMonitor(
            sampler=lambda: staged._validate_host_capacity_sample(
                samples.pop(0),
                expected_collector_sha256="sha256:" + "f" * 64,
                expected_windows_node_identity_sha256="sha256:" + "0" * 64,
                docker_memory_reserve_bytes=4096,
            ),
            collector_sha256="sha256:" + "f" * 64,
            windows_node_identity_sha256="sha256:" + "0" * 64,
            docker_memory_reserve_bytes=4096,
            abort_latch=abort_latch,
            sample_interval_seconds=3600,
        )
        monitor.start()
        append_trusted_sample_locked = monitor._append_trusted_sample_locked

        def append_then_pause(
            sample: dict[str, object],
            *,
            observed_seconds: float,
        ) -> int:
            sample_index = append_trusted_sample_locked(
                sample,
                observed_seconds=observed_seconds,
            )
            if sample["containers"][0]["docker_vm_memory_available_bytes"] == 1024:  # type: ignore[index]
                violating_sample_committed.set()
                self.assertTrue(allow_failure_publish.wait(timeout=1))
            return sample_index

        def hold() -> dict[str, object]:
            occupied.set()
            self.assertTrue(release.wait(timeout=1))
            return {"status": "pass"}

        try:
            with (
                patch.object(
                    monitor,
                    "_append_trusted_sample_locked",
                    side_effect=append_then_pause,
                ),
                ThreadPoolExecutor(max_workers=3) as executor,
            ):
                first = executor.submit(
                    admission.run,
                    copy_index=1,
                    workload_class="regular",
                    operation=hold,
                )
                self.assertTrue(occupied.wait(timeout=1))
                second = executor.submit(
                    admission.run,
                    copy_index=2,
                    workload_class="regular",
                    operation=lambda: (
                        second_operation_called.set() or {"status": "unexpected"}
                    ),
                )
                sampler = executor.submit(monitor._sample_once)
                self.assertTrue(violating_sample_committed.wait(timeout=1))
                release.set()
                self.assertFalse(second_operation_called.wait(timeout=0.05))
                allow_failure_publish.set()

                sampler.result()
                self.assertEqual(first.result(), {"status": "pass"})
                with self.assertRaisesRegex(
                    staged._StageAdmissionClosedError,
                    "host_capacity_violation",
                ):
                    second.result()
        finally:
            allow_failure_publish.set()
            release.set()
            monitor.stop()

        self.assertFalse(second_operation_called.is_set())
        self.assertEqual(admission.peak, 1)
        self.assertEqual(len(monitor.evidence()["violations"]), 1)

    def test_host_capacity_sample_preserves_trusted_safety_violations(
        self,
    ) -> None:
        valid = _host_capacity_sample()
        observed = staged._validate_host_capacity_sample(
            valid,
            expected_collector_sha256="sha256:" + "f" * 64,
            expected_windows_node_identity_sha256="sha256:" + "0" * 64,
            docker_memory_reserve_bytes=4096,
        )
        self.assertEqual(len(observed["containers"]), 3)

        for field, value in (
            ("restart_count", 1),
            ("oom_killed", True),
            ("docker_vm_memory_available_bytes", 1024),
        ):
            sample = _host_capacity_sample()
            sample["containers"][0][field] = value  # type: ignore[index]
            with self.subTest(field=field), self.assertRaises(
                staged._TrustedHostCapacityViolation
            ) as raised:
                staged._validate_host_capacity_sample(
                    sample,
                    expected_collector_sha256="sha256:" + "f" * 64,
                    expected_windows_node_identity_sha256="sha256:" + "0" * 64,
                    docker_memory_reserve_bytes=4096,
                )
            self.assertEqual(len(raised.exception.sample["containers"]), 3)

    def test_host_capacity_monitor_keeps_violating_and_drain_samples(self) -> None:
        samples = [
            _host_capacity_sample(),
            _host_capacity_sample(available_bytes=1024),
            _host_capacity_sample(available_bytes=6144),
        ]
        monitor = staged._HostCapacityMonitor(
            sampler=lambda: staged._validate_host_capacity_sample(
                samples.pop(0),
                expected_collector_sha256="sha256:" + "f" * 64,
                expected_windows_node_identity_sha256="sha256:" + "0" * 64,
                docker_memory_reserve_bytes=4096,
            ),
            collector_sha256="sha256:" + "f" * 64,
            windows_node_identity_sha256="sha256:" + "0" * 64,
            docker_memory_reserve_bytes=4096,
            sample_interval_seconds=3600,
        )

        monitor.start()
        monitor._sample_once()
        self.assertIn("docker_vm_memory_reserve_crossed", str(monitor.failure))
        monitor.stop()
        evidence = monitor.evidence()

        self.assertEqual(evidence["schema"], "mineru-host-capacity-evidence.v2")
        self.assertEqual(len(evidence["samples"]), 3)
        self.assertEqual(len(evidence["violations"]), 1)
        self.assertEqual(evidence["sampling_failures"], [])
        self.assertEqual(
            evidence["summary"]["min_docker_vm_memory_available_bytes"],
            1024,
        )

    def test_host_capacity_monitor_keeps_sampling_after_malformed_payload(self) -> None:
        malformed = _host_capacity_sample()
        del malformed["containers"]
        samples = [
            _host_capacity_sample(available_bytes=8192),
            malformed,
            _host_capacity_sample(available_bytes=6144),
        ]

        def sample() -> dict[str, object]:
            return staged._validate_host_capacity_sample(
                samples.pop(0),
                expected_collector_sha256="sha256:" + "f" * 64,
                expected_windows_node_identity_sha256="sha256:" + "0" * 64,
                docker_memory_reserve_bytes=4096,
            )

        monitor = staged._HostCapacityMonitor(
            sampler=sample,
            collector_sha256="sha256:" + "f" * 64,
            windows_node_identity_sha256="sha256:" + "0" * 64,
            docker_memory_reserve_bytes=4096,
            sample_interval_seconds=3600,
        )

        monitor.start()
        monitor._sample_once()
        monitor.stop()
        evidence = monitor.evidence()

        self.assertEqual(evidence["status"], "fail")
        self.assertIn("host_capacity_sample_failed", str(evidence["failure"]))
        self.assertEqual(len(evidence["samples"]), 2)
        self.assertEqual(len(evidence["sampling_failures"]), 1)
        self.assertEqual(evidence["violations"], [])
        self.assertEqual(
            evidence["summary"]["min_docker_vm_memory_available_bytes"],
            6144,
        )

    def test_host_capacity_monitor_binds_epoch_and_has_pre_post_samples(self) -> None:
        samples = [_host_capacity_sample(), _host_capacity_sample()]
        monitor = staged._HostCapacityMonitor(
            sampler=lambda: samples.pop(0),
            collector_sha256="sha256:" + "f" * 64,
            windows_node_identity_sha256="sha256:" + "0" * 64,
            docker_memory_reserve_bytes=4096,
            sample_interval_seconds=3600,
        )

        monitor.start()
        monitor.stop()
        evidence = monitor.evidence()

        self.assertEqual(evidence["status"], "pass")
        self.assertEqual(evidence["summary"]["sample_count"], 2)

        changed = [_host_capacity_sample(), _host_capacity_sample(container_id_suffix="a")]
        monitor = staged._HostCapacityMonitor(
            sampler=lambda: changed.pop(0),
            collector_sha256="sha256:" + "f" * 64,
            windows_node_identity_sha256="sha256:" + "0" * 64,
            docker_memory_reserve_bytes=4096,
            sample_interval_seconds=3600,
        )
        monitor.start()
        monitor.stop()
        self.assertEqual(monitor.failure, "host_capacity_epoch_changed")

        collector_source = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "windows"
            / "collect_mineru_runtime.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "$probeCode | & docker exec -i $name /usr/bin/python3.12 -I -",
            collector_source,
        )
        self.assertNotIn("-I -c $probeCode", collector_source)

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

    def test_metrics_monitor_transport_failures_degrade_without_hard_abort(
        self,
    ) -> None:
        monitor = staged._MetricsMonitor(
            sampler=MagicMock(
                side_effect=staged.MetricsTransportUnavailableError("budget exhausted")
            ),
            expected_preemptions=0,
            monotonic_clock=MagicMock(side_effect=(0.0, 1.0, 2.0, 3.0, 4.0)),
        )

        self.assertTrue(monitor._observe_once())
        self.assertIsNone(monitor.failure)
        self.assertEqual(len(monitor.sampling_failures), 1)
        self.assertTrue(monitor._observe_once())
        self.assertIsNone(monitor.failure)
        self.assertEqual(len(monitor.sampling_failures), 2)
        self.assertEqual(monitor.evidence()["state"], "DEGRADED_TRANSPORT")

    def test_metrics_monitor_invalid_payload_degrades_evidence(self) -> None:
        monitor = staged._MetricsMonitor(
            sampler=MagicMock(side_effect=ValueError("missing required signals")),
            expected_preemptions=0,
            monotonic_clock=MagicMock(side_effect=(0.0, 1.0, 2.0)),
        )

        self.assertTrue(monitor._observe_once())
        self.assertIsNone(monitor.failure)
        self.assertEqual(len(monitor.sampling_failures), 1)
        self.assertEqual(monitor.evidence()["state"], "DEGRADED_TRANSPORT")

    def test_metrics_monitor_gap_does_not_invent_waiting_safety_evidence(self) -> None:
        monitor = staged._MetricsMonitor(
            sampler=MagicMock(
                side_effect=(
                    staged.MetricsSample(0, 1, 64, 0, 0.1),
                    staged.MetricsTransportUnavailableError("budget exhausted"),
                )
            ),
            expected_preemptions=0,
            monotonic_clock=MagicMock(side_effect=(0.0, 1.0, 2.0, 3.0, 4.0)),
        )

        self.assertTrue(monitor._observe_once())
        self.assertTrue(monitor._observe_once())
        self.assertIsNone(monitor.failure)
        self.assertEqual(monitor.evidence()["state"], "DEGRADED_TRANSPORT")

    def test_metrics_monitor_long_gap_degrades_without_data_plane_abort(self) -> None:
        monitor = staged._MetricsMonitor(
            sampler=MagicMock(
                side_effect=staged.MetricsTransportUnavailableError(
                    "late transport return"
                )
            ),
            expected_preemptions=0,
            monotonic_clock=MagicMock(side_effect=(0.0, 1.0, 12.0)),
        )

        self.assertTrue(monitor._observe_once())
        self.assertIsNone(monitor.failure)
        self.assertEqual(monitor.sampling_failures[0].duration_seconds, 11.0)
        self.assertEqual(monitor.evidence()["state"], "DEGRADED_TRANSPORT")

    def test_metrics_terminal_sample_transport_gap_is_never_tolerated(self) -> None:
        monitor = staged._MetricsMonitor(
            sampler=MagicMock(
                side_effect=staged.MetricsTransportUnavailableError(
                    "terminal transport gap"
                )
            ),
            expected_preemptions=0,
            monotonic_clock=MagicMock(side_effect=(0.0, 1.0, 2.0, 3.0)),
        )
        monitor._thread_started = True

        with (
            patch.object(monitor._thread, "join"),
            patch.object(monitor._thread, "is_alive", return_value=False),
        ):
            monitor.stop()

        self.assertIsNone(monitor.failure)
        self.assertEqual(monitor.evidence_failure, "metrics_observation_incomplete")
        self.assertEqual(monitor.evidence()["state"], "CLOSED")
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
                side_effect=(0.0, 1.0, 2.0, 3.0, 4.0, 5.0)
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
        self.assertEqual(monitor.evidence_failure, "metrics_observation_incomplete")
        self.assertEqual(
            [item["to"] for item in monitor.evidence()["transitions"]],
            ["DEGRADED_TRANSPORT", "HEALTHY", "CLOSED"],
        )

    def test_metrics_midstage_and_terminal_gaps_fail_stage(self) -> None:
        monitor = staged._MetricsMonitor(
            sampler=MagicMock(
                side_effect=staged.MetricsTransportUnavailableError("transport gap")
            ),
            expected_preemptions=0,
            monotonic_clock=MagicMock(
                side_effect=(0.0, 1.0, 2.0, 3.0, 4.0, 5.0)
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
        self.assertEqual(monitor.evidence_failure, "metrics_observation_incomplete")
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

    def test_orchestrator_evidence_accepts_retained_gauge_cleanup(self) -> None:
        baseline = self._health(completed=2, failed=2)
        processing_only = (
            staged.OrchestratorSample(0.1, 0, 1, 2, 2),
            staged.OrchestratorSample(0.2, 0, 1, 1, 1),
            staged.OrchestratorSample(0.3, 0, 1, 0, 0),
        )
        terminal = self._health(completed=1, failed=0)

        self.assertIsNone(
            staged._orchestrator_evidence_failure(
                baseline=baseline,
                samples=processing_only,
                terminal=terminal,
                task_slots=1,
                client_outstanding_window=1,
            )
        )
        with_queue = (
            *processing_only,
            staged.OrchestratorSample(0.2, 1, 1, 100, 2),
        )
        self.assertEqual(
            staged._orchestrator_evidence_failure(
                baseline=baseline,
                samples=with_queue,
                terminal=terminal,
                task_slots=1,
                client_outstanding_window=1,
            ),
            "orchestrator_active_exceeded_client_window",
        )

        summary = staged._orchestrator_summary(
            baseline=baseline,
            samples=processing_only,
            terminal=terminal,
            preflight_drain_seconds=0.0,
            terminal_drain_seconds=0.5,
        )
        self.assertEqual(
            summary["task_registry_semantics"],
            "retained-terminal-gauges.v1",
        )
        self.assertNotIn("completed_delta", summary)
        self.assertNotIn("failed_delta", summary)
        self.assertEqual(summary["range"]["completed_tasks"], {"min": 0, "max": 2})
        self.assertEqual(summary["range"]["failed_tasks"], {"min": 0, "max": 2})

    def test_orchestrator_evidence_still_requires_idle_boundaries(self) -> None:
        processing = (staged.OrchestratorSample(0.1, 0, 1, 0, 0),)
        self.assertEqual(
            staged._orchestrator_evidence_failure(
                baseline=self._health(processing=1),
                samples=processing,
                terminal=self._health(),
                task_slots=1,
                client_outstanding_window=1,
            ),
            "orchestrator_baseline_not_idle",
        )
        self.assertEqual(
            staged._orchestrator_evidence_failure(
                baseline=self._health(),
                samples=processing,
                terminal=self._health(processing=1),
                task_slots=1,
                client_outstanding_window=1,
            ),
            "orchestrator_terminal_not_idle",
        )

    def test_orchestrator_monitor_fails_closed_above_attested_processing(self) -> None:
        monitor = staged._OrchestratorMonitor(
            sampler=lambda: self._health(processing=2),
            task_slots=1,
            client_outstanding_window=2,
        )

        monitor._run()

        self.assertEqual(
            monitor.failure,
            "orchestrator_processing_exceeded_attested_slots",
        )
        self.assertEqual(monitor.samples[0].processing_tasks, 2)

    def test_orchestrator_monitor_rejects_active_above_client_window(self) -> None:
        monitor = staged._OrchestratorMonitor(
            sampler=lambda: self._health(queued=2, processing=1),
            task_slots=1,
            client_outstanding_window=2,
        )

        monitor._run()

        self.assertEqual(
            monitor.failure,
            "orchestrator_active_exceeded_client_window",
        )

    def test_unapproved_stage_is_rejected_before_metrics_or_parse(self) -> None:
        sampled = False

        def sample() -> staged.MetricsSample:
            nonlocal sampled
            sampled = True
            return staged.MetricsSample(0, 0, 0, 0, 0)

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "not an approved stage"):
                staged._run_stage(
                    document_count=32,
                    run_root=Path(tmp),
                    corpus=self._corpus(),
                    mineru_bin=Path("/unused/mineru"),
                    api_url="http://unused-api",
                    inference_upstream_url="http://unused-upstream/v1",
                    runtime_identity="sha256:" + "a" * 64,
                    timeout_seconds=1,
                    expected_preemptions=0,
                    metrics_sampler=sample,
                    orchestrator_sampler=lambda: self._health(),
                    orchestrator_idle_waiter=lambda: (self._health(), 0.0),
                    task_slots=1,
                )
        self.assertFalse(sampled)

    def test_between_stage_preemption_change_fails_before_parse(self) -> None:
        health = self._health(completed=10)
        with tempfile.TemporaryDirectory() as tmp:
            result = staged._run_stage(
                document_count=4,
                run_root=Path(tmp),
                corpus=self._corpus(),
                mineru_bin=Path("/unused/mineru"),
                api_url="http://unused-api",
                inference_upstream_url="http://unused-upstream/v1",
                runtime_identity="sha256:" + "a" * 64,
                timeout_seconds=1,
                expected_preemptions=3,
                metrics_sampler=lambda: staged.MetricsSample(0, 0, 0, 4, 0),
                orchestrator_sampler=lambda: health,
                orchestrator_idle_waiter=lambda: (health, 0.0),
                task_slots=1,
            )

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["failure"], "preemption_counter_changed_between_stages")
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
        self.assertEqual(observed["server_url"], "http://mineru-vllm:30000/v1")
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
                document_count=4,
                run_root=Path(tmp),
                corpus=self._corpus(),
                mineru_bin=Path("/unused/mineru"),
                api_url="http://unused-api",
                inference_upstream_url="http://unused-upstream/v1",
                runtime_identity="sha256:" + "a" * 64,
                timeout_seconds=1,
                expected_preemptions=0,
                metrics_sampler=lambda: metrics,
                orchestrator_sampler=lambda: health,
                orchestrator_idle_waiter=idle_waiter,
                task_slots=1,
            )

        statuses = [item["status"] for item in result["documents"]]
        self.assertEqual(statuses[0], "fail")
        self.assertNotIn("not_started", statuses)
        self.assertEqual(
            statuses[1:],
            [staged.NOT_ADMITTED_ATOMIC_ABORT] * 3,
        )
        self.assertEqual(
            [item.get("failure_class") for item in result["documents"][1:]],
            ["stage_abort"] * 3,
        )
        self.assertEqual(result["admission_order_copy_indices"], [1])
        self.assertEqual(
            [item["state"] for item in result["admission"]["records"]],
            ["failed"] + [staged.NOT_ADMITTED_ATOMIC_ABORT] * 3,
        )

    def test_degraded_metrics_evidence_does_not_terminate_completed_stage(
        self,
    ) -> None:
        baseline = staged.MetricsSample(0, 0, 0, 0, 0)
        active = staged.MetricsSample(1, 1, 0, 0, 0.1)
        health = self._health()

        class DegradedMetricsMonitor:
            failure = None
            evidence_failure = "metrics_observation_incomplete"
            samples = (active,)
            sampling_failures = (
                staged.MetricsSamplingFailure(1, 9.0, "timeout-1"),
                staged.MetricsSamplingFailure(10, 9.0, "timeout-2"),
            )
            terminal_sample_observed_seconds = None

            def start(self) -> None:
                return None

            def stop(self) -> None:
                return None

            def evidence(self) -> dict[str, object]:
                return {
                    "profile": staged.METRICS_OBSERVER_PROFILE,
                    "state": "CLOSED",
                    "observation_complete": False,
                    "hard_failure": None,
                    "transitions": [
                        {
                            "from": "STARTING",
                            "to": "DEGRADED_TRANSPORT",
                            "reason": "metrics_transport_unavailable",
                            "observed_seconds": 1.0,
                        },
                        {
                            "from": "DEGRADED_TRANSPORT",
                            "to": "CLOSED",
                            "reason": "monitor_stopped",
                            "observed_seconds": 20.0,
                        },
                    ],
                }

        def parse(*, copy_index: int, **_kwargs: object) -> dict[str, object]:
            return {
                "copy_index": copy_index,
                "logical_name": f"copy-{copy_index}.pdf",
                "input_sha256": "sha256:" + f"{copy_index:064x}",
                "status": "pass",
                "elapsed_seconds": 1.0,
                "page_count": 7,
                "block_count": 1,
                "provider_bundle_sha256": "sha256:" + "a" * 64,
            }

        terminate = MagicMock(return_value=0)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(staged, "_MetricsMonitor", return_value=DegradedMetricsMonitor()),
            patch.object(staged, "_parse_frozen_copy", side_effect=parse),
            patch.object(staged, "_orchestrator_evidence_failure", return_value=None),
            patch.object(staged, "mineru_api_temp_dirs", return_value=set()),
            patch.object(staged, "_wait_for_process_cleanup", return_value={}),
            patch.object(
                staged,
                "terminate_active_mineru_processes",
                terminate,
            ),
        ):
            result = staged._run_stage(
                document_count=4,
                run_root=Path(tmp),
                corpus=self._corpus(),
                mineru_bin=Path("/unused/mineru"),
                api_url="http://unused-api",
                inference_upstream_url="http://unused-upstream/v1",
                runtime_identity="sha256:" + "a" * 64,
                timeout_seconds=1,
                expected_preemptions=0,
                metrics_sampler=lambda: baseline,
                orchestrator_sampler=lambda: health,
                orchestrator_idle_waiter=MagicMock(
                    side_effect=((health, 0.0), (health, 0.0))
                ),
                task_slots=1,
            )

        self.assertEqual(result["failure"], "metrics_observation_incomplete")
        self.assertEqual(
            [item["status"] for item in result["documents"]],
            ["pass"] * 4,
        )
        terminate.assert_not_called()

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

    def test_operational_failure_writes_fail_receipt_without_remote_access(
        self,
    ) -> None:
        input_bytes = b"%PDF-1.4 frozen fixture"
        input_sha = "sha256:" + hashlib.sha256(input_bytes).hexdigest()
        runtime_identity = "sha256:" + "a" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / "fixture.pdf"
            fixture.write_bytes(input_bytes)
            corpus_manifest = root / "corpus.json"
            corpus_manifest.write_text("{}", encoding="utf-8")
            mineru = root / "mineru"
            mineru.write_text("executable", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            receipt = root / "receipt.json"
            observer_identity, observer_known_hosts = self._host_observer_files(root)
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
                patch.object(
                    staged,
                    "_load_frozen_corpus",
                    return_value=self._corpus_fixture(input_sha),
                ),
                patch.object(
                    staged, "writer_code_digest", return_value="sha256:" + "c" * 64
                ),
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
                        "--corpus-manifest",
                        str(corpus_manifest),
                        "--expected-corpus-sha256",
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
                        "--host-observer-ssh-host",
                        "100.64.0.1",
                        "--host-observer-ssh-user",
                        "operator",
                        "--host-observer-identity-file",
                        str(observer_identity),
                        "--host-observer-known-hosts-file",
                        str(observer_known_hosts),
                        "--docker-memory-reserve-bytes",
                        "1024",
                    ]
                )

            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(result, 2)
            self.assertEqual(payload["status"], "fail")
            self.assertEqual(payload["schema"], "mineru_staged_load_receipt.v6")
            self.assertEqual(payload["receipt_schema_version"], 6)
            self.assertEqual(
                payload["topology"]["api_endpoint_sha256"],
                "sha256:" + hashlib.sha256(b"http://unused-api").hexdigest(),
            )
            self.assertEqual(payload["database_access"], "none")
            self.assertEqual(payload["queue_access"], "none")
            self.assertIn("manifest drift", payload["failure"])
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)

    def test_preflight_failures_write_new_private_receipts(self) -> None:
        for case in ("missing_bin", "bad_window", "invalid_corpus"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                mineru = root / "mineru"
                if case != "missing_bin":
                    mineru.write_text("executable", encoding="utf-8")
                corpus = root / "corpus.json"
                corpus.write_text("{}", encoding="utf-8")
                manifest = root / "manifest.json"
                manifest.write_text("{}", encoding="utf-8")
                receipt = root / "receipt.json"
                identity, known_hosts = self._host_observer_files(root)
                arguments = [
                    "--runtime-manifest",
                    str(manifest),
                    "--receipt-out",
                    str(receipt),
                    "--corpus-manifest",
                    str(corpus),
                    "--expected-corpus-sha256",
                    "sha256:" + "a" * 64,
                    "--mineru-bin",
                    str(mineru),
                    "--api-url",
                    "http://unused-api",
                    "--observability-url",
                    "http://unused-observability/v1",
                    "--inference-upstream-url",
                    "http://unused-upstream/v1",
                    "--runtime-bundle-identity",
                    "sha256:" + "b" * 64,
                    "--host-observer-ssh-host",
                    "100.64.0.1",
                    "--host-observer-ssh-user",
                    "operator",
                    "--host-observer-identity-file",
                    str(identity),
                    "--host-observer-known-hosts-file",
                    str(known_hosts),
                    "--docker-memory-reserve-bytes",
                    "1024",
                ]
                window = "15" if case == "bad_window" else "16"
                with patch.dict(
                    os.environ,
                    {"MINERU_PROCESSING_WINDOW_SIZE": window},
                ):
                    result = staged.main(arguments)

                payload = json.loads(receipt.read_text(encoding="utf-8"))
                self.assertEqual(result, 2)
                self.assertEqual(payload["schema"], "mineru_staged_load_receipt.v6")
                self.assertEqual(payload["receipt_schema_version"], 6)
                self.assertEqual(payload["status"], "fail")
                self.assertEqual(payload["failure_phase"], "preflight_configuration")
                self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)

    def test_main_routes_three_urls_and_emits_staged_v6_receipt(self) -> None:
        input_bytes = b"%PDF-1.4 frozen fixture"
        input_sha = "sha256:" + hashlib.sha256(input_bytes).hexdigest()
        runtime_identity = "sha256:" + "a" * 64
        api_url = "http://127.0.0.1:30000"
        observability_url = "http://127.0.0.1:30002/v1"
        inference_upstream_url = "http://mineru-vllm:30000/v1"
        topology = {
            name: "sha256:" + hashlib.sha256(url.rstrip("/").encode()).hexdigest()
            for name, url in {
                "api_endpoint_sha256": api_url,
                "observability_endpoint_sha256": observability_url,
                "inference_upstream_sha256": inference_upstream_url,
            }.items()
        }
        topology.update(
            {
                "windows_collector_path": staged.MINERU_WINDOWS_COLLECTOR_PATH,
                "windows_collector_sha256": "sha256:" + "f" * 64,
                "windows_node_identity_sha256": "sha256:" + "0" * 64,
            }
        )
        verified = SimpleNamespace(
            manifest={
                "topology": topology,
                "orchestrator": {
                    "task_retention_seconds": 600,
                    "task_cleanup_interval_seconds": 30,
                },
            },
            identity_sha256=runtime_identity,
            orchestrator_identity_sha256="sha256:" + "d" * 64,
            provider_identity_sha256="sha256:" + "e" * 64,
            served_model_id="mineru-model",
            max_concurrent_requests=1,
        )
        metrics = staged.MetricsSample(0, 0, 0, 0, 0)
        health = self._health()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / "fixture.pdf"
            fixture.write_bytes(input_bytes)
            corpus_manifest = root / "corpus.json"
            corpus_manifest.write_text("{}", encoding="utf-8")
            mineru = root / "mineru"
            mineru.write_text("executable", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            receipt = root / "receipt.json"
            observer_identity, observer_known_hosts = self._host_observer_files(root)
            client = SimpleNamespace(
                package_set_sha256="sha256:" + "b" * 64,
                content_package_versions={},
            )
            run_stage = MagicMock(
                side_effect=lambda **kwargs: {
                    "status": "pass",
                    "stage_document_count": kwargs["document_count"],
                }
            )
            metrics_fetch = MagicMock(return_value=metrics)
            health_fetch = MagicMock(return_value=health)
            idle_waiter = MagicMock(return_value=(health, 0.0))
            host_monitor = MagicMock()
            host_monitor.failure = None
            host_monitor.evidence.return_value = {
                "schema": "mineru-host-capacity-evidence.v2",
                "status": "pass",
                "failure": None,
            }
            with (
                patch.dict(os.environ, {"MINERU_PROCESSING_WINDOW_SIZE": "16"}),
                patch.object(staged, "mineru_api_temp_dirs", return_value=set()),
                patch.object(staged, "process_snapshot", return_value={}),
                patch.object(staged, "_wait_for_process_cleanup", return_value={}),
                patch.object(staged, "client_bundle_identity", return_value=client),
                patch.object(
                    staged,
                    "_load_frozen_corpus",
                    return_value=self._corpus_fixture(input_sha),
                ),
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
                patch.object(
                    staged,
                    "_HostCapacityMonitor",
                    return_value=host_monitor,
                ),
            ):
                result = staged.main(
                    [
                        "--runtime-manifest",
                        str(manifest),
                        "--receipt-out",
                        str(receipt),
                        "--corpus-manifest",
                        str(corpus_manifest),
                        "--expected-corpus-sha256",
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
                        "--host-observer-ssh-host",
                        "100.64.0.1",
                        "--host-observer-ssh-user",
                        "operator",
                        "--host-observer-identity-file",
                        str(observer_identity),
                        "--host-observer-known-hosts-file",
                        str(observer_known_hosts),
                        "--docker-memory-reserve-bytes",
                        "1024",
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
            self.assertEqual(
                metrics_fetch.call_args_list[-1].args, (observability_url,)
            )
            self.assertEqual(health_fetch.call_args.args, (api_url,))
            self.assertEqual(idle_waiter.call_args.args, (api_url,))
            self.assertEqual(
                health_fetch.call_args.kwargs,
                {
                    "expected_task_slots": 1,
                    "expected_task_retention_seconds": 600,
                    "expected_cleanup_interval_seconds": 30,
                },
            )
            self.assertEqual(
                {
                    key: idle_waiter.call_args.kwargs[key]
                    for key in (
                        "expected_task_slots",
                        "expected_task_retention_seconds",
                        "expected_cleanup_interval_seconds",
                    )
                },
                {
                    "expected_task_slots": 1,
                    "expected_task_retention_seconds": 600,
                    "expected_cleanup_interval_seconds": 30,
                },
            )
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["schema"], "mineru_staged_load_receipt.v6")
            self.assertEqual(payload["receipt_schema_version"], 6)
            self.assertEqual(
                payload["topology"],
                {
                    key: topology[key]
                    for key in (
                        "api_endpoint_sha256",
                        "observability_endpoint_sha256",
                        "inference_upstream_sha256",
                    )
                },
            )
            self.assertEqual(
                payload["fixed_stage_document_counts"],
                [4, 8, 16],
            )
            self.assertEqual(payload["orchestrator_task_concurrency"], 1)
            self.assertEqual(payload["orchestrator_inference_concurrency"], 7)
            self.assertEqual(
                payload["effective_inference_request_upper_bound"],
                7,
            )


if __name__ == "__main__":
    unittest.main()
