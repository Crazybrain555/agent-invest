"""Run the fixed DB-free 4/8/16 MinerU deployment load envelope.

This is an explicit operator gate, never a resident worker. It uses the exact
official Hybrid-medium writer path against frozen PDF copies, samples vLLM
metrics throughout each stage, stops on the first unsafe signal, removes all
temporary state, and writes one new-only private receipt.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Any
import urllib.error
import urllib.request
import uuid

from disclosure_anchor.adapters.parsers.mineru_medium import (
    MinerUMediumDocumentParser,
    MinerUProcess,
)
from disclosure_anchor.adapters.parsers.mineru_medium.process import (
    terminate_active_mineru_processes,
)
from disclosure_anchor.adapters.runtime.mineru_canary import (
    probe_mineru_served_model,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_API_INFERENCE_MAX_CONCURRENCY,
    MINERU_API_MAX_CONCURRENT_REQUESTS,
    MINERU_PROCESSING_WINDOW_SIZE,
    client_bundle_identity,
    verify_runtime_manifest_payload,
    writer_code_digest,
)
from disclosure_anchor.adapters.runtime.mineru_orchestrator import (
    MinerUOrchestratorHealth,
    fetch_mineru_orchestrator_health,
    wait_for_mineru_orchestrator_idle,
)
from disclosure_anchor.adapters.runtime.mineru_process_isolation import (
    active_disclosure_producers,
    mineru_api_temp_dirs,
    mineru_processes,
    process_snapshot,
)
from disclosure_anchor.application.ports.parser import ParserOptions


RECEIPT_SCHEMA = "mineru_staged_load_receipt.v2"
STAGE_DOCUMENT_CONCURRENCIES = (4, 8, 16)
ORCHESTRATOR_TASK_CONCURRENCY = MINERU_API_MAX_CONCURRENT_REQUESTS
ORCHESTRATOR_INFERENCE_CONCURRENCY = MINERU_API_INFERENCE_MAX_CONCURRENCY
EFFECTIVE_INFERENCE_REQUEST_UPPER_BOUND = (
    ORCHESTRATOR_TASK_CONCURRENCY * ORCHESTRATOR_INFERENCE_CONCURRENCY
)
STAGE_EFFECTIVE_INFERENCE_REQUEST_UPPER_BOUNDS = tuple(
    min(concurrency, ORCHESTRATOR_TASK_CONCURRENCY)
    * ORCHESTRATOR_INFERENCE_CONCURRENCY
    for concurrency in STAGE_DOCUMENT_CONCURRENCIES
)
MINIMUM_INPUT_PAGES = 7
WAITING_ABORT_THRESHOLD = 64.0
WAITING_ABORT_SECONDS = 30.0
METRICS_SAMPLE_INTERVAL_SECONDS = 1.0
ORCHESTRATOR_SAMPLE_INTERVAL_SECONDS = 0.25
METRICS_LOGICAL_SAMPLE_TIMEOUT_SECONDS = 10.0
METRICS_TRANSPORT_ATTEMPT_TIMEOUT_SECONDS = 4.5
METRICS_TRANSPORT_ATTEMPTS = 2
MAX_TOLERATED_METRICS_SAMPLE_FAILURES_PER_STAGE = 1
_MAX_METRICS_BYTES = 2 * 1024 * 1024
_MAX_SAFE_DETAIL_CHARS = 500
_SAFE_DETAIL_TRUNCATION_MARKER = " ...[middle truncated]... "
_METRIC_ALIASES = {
    "running": {
        "vllm:num_requests_running",
        "vllm_num_requests_running",
    },
    "waiting": {
        "vllm:num_requests_waiting",
        "vllm_num_requests_waiting",
    },
    "preemptions": {
        "vllm:num_preemptions_total",
        "vllm_num_preemptions_total",
    },
    "kv_cache": {
        "vllm:gpu_cache_usage_perc",
        "vllm_gpu_cache_usage_perc",
        "vllm:kv_cache_usage_perc",
        "vllm_kv_cache_usage_perc",
    },
}


@dataclass(frozen=True)
class MetricsSample:
    observed_seconds: float
    running: float
    waiting: float
    preemptions: float
    kv_cache: float

    def to_payload(self) -> dict[str, float]:
        return {
            "observed_seconds": round(self.observed_seconds, 3),
            "running": self.running,
            "waiting": self.waiting,
            "preemptions": self.preemptions,
            "kv_cache": self.kv_cache,
        }


@dataclass(frozen=True)
class MetricsSamplingFailure:
    observed_seconds: float
    duration_seconds: float
    failure: str

    def to_payload(self) -> dict[str, float | str]:
        return {
            "observed_seconds": round(self.observed_seconds, 6),
            "duration_seconds": round(self.duration_seconds, 6),
            "failure": self.failure,
        }


class MetricsTransportUnavailableError(RuntimeError):
    """One logical metrics sample exhausted only its transport budget."""


@dataclass(frozen=True)
class OrchestratorSample:
    observed_seconds: float
    queued_tasks: int
    processing_tasks: int
    completed_tasks: int
    failed_tasks: int

    @classmethod
    def from_health(
        cls,
        health: MinerUOrchestratorHealth,
        *,
        observed_seconds: float,
    ) -> "OrchestratorSample":
        return cls(
            observed_seconds=observed_seconds,
            queued_tasks=health.queued_tasks,
            processing_tasks=health.processing_tasks,
            completed_tasks=health.completed_tasks,
            failed_tasks=health.failed_tasks,
        )

    def to_payload(self) -> dict[str, float | int]:
        return {
            "observed_seconds": round(self.observed_seconds, 6),
            "queued_tasks": self.queued_tasks,
            "processing_tasks": self.processing_tasks,
            "completed_tasks": self.completed_tasks,
            "failed_tasks": self.failed_tasks,
        }


class _OrchestratorMonitor:
    """Continuously sample the fixed API queue while one stage is active."""

    def __init__(
        self,
        *,
        sampler: Callable[[], MinerUOrchestratorHealth],
        monotonic_clock: Callable[[], float] = time.monotonic,
        sample_interval_seconds: float = ORCHESTRATOR_SAMPLE_INTERVAL_SECONDS,
    ) -> None:
        self._sampler = sampler
        self._monotonic_clock = monotonic_clock
        self._sample_interval_seconds = sample_interval_seconds
        self._started = monotonic_clock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="mineru-staged-load-orchestrator",
            daemon=True,
        )
        self._lock = threading.Lock()
        self._samples: list[OrchestratorSample] = []
        self._failure: str | None = None
        self._thread_started = False

    def start(self) -> None:
        self._thread.start()
        self._thread_started = True

    def stop(self) -> None:
        if not self._thread_started:
            return
        self._stop.set()
        self._thread.join(timeout=max(16.0, self._sample_interval_seconds * 2))
        if self._thread.is_alive():
            with self._lock:
                self._failure = self._failure or "orchestrator_monitor_did_not_stop"

    @property
    def failure(self) -> str | None:
        with self._lock:
            return self._failure

    @property
    def samples(self) -> tuple[OrchestratorSample, ...]:
        with self._lock:
            return tuple(self._samples)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                health = self._sampler()
                sample = OrchestratorSample.from_health(
                    health,
                    observed_seconds=max(
                        0.0,
                        self._monotonic_clock() - self._started,
                    ),
                )
                failure = (
                    "orchestrator_processing_exceeded_3"
                    if sample.processing_tasks > ORCHESTRATOR_TASK_CONCURRENCY
                    else None
                )
                with self._lock:
                    self._samples.append(sample)
                    if failure is not None:
                        self._failure = failure
                if failure is not None:
                    return
            except Exception as exc:
                with self._lock:
                    self._failure = (
                        "orchestrator_health_unavailable:"
                        f"{type(exc).__name__}:{_safe_detail(str(exc))}"
                    )
                return
            self._stop.wait(self._sample_interval_seconds)


class _MetricsMonitor:
    def __init__(
        self,
        *,
        sampler: Callable[[], MetricsSample],
        expected_preemptions: float,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sample_interval_seconds: float = METRICS_SAMPLE_INTERVAL_SECONDS,
    ) -> None:
        self._sampler = sampler
        self._expected_preemptions = expected_preemptions
        self._monotonic_clock = monotonic_clock
        self._sample_interval_seconds = sample_interval_seconds
        self._started = monotonic_clock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="mineru-staged-load-metrics",
            daemon=True,
        )
        self._lock = threading.Lock()
        self._samples: list[MetricsSample] = []
        self._sampling_failures: list[MetricsSamplingFailure] = []
        self._failure: str | None = None
        self._waiting_since: float | None = None
        self._terminal_sample_observed_seconds: float | None = None
        self._thread_started = False

    def start(self) -> None:
        self._thread.start()
        self._thread_started = True

    def stop(self) -> None:
        if not self._thread_started:
            return
        self._stop.set()
        self._thread.join(timeout=max(12.0, self._sample_interval_seconds * 2))
        if self._thread.is_alive():
            with self._lock:
                self._failure = self._failure or "metrics_monitor_did_not_stop"
            return
        if self.failure is None:
            self._observe_once(allow_transport_gap=False, terminal=True)

    @property
    def failure(self) -> str | None:
        with self._lock:
            return self._failure

    @property
    def samples(self) -> tuple[MetricsSample, ...]:
        with self._lock:
            return tuple(self._samples)

    @property
    def sampling_failures(self) -> tuple[MetricsSamplingFailure, ...]:
        with self._lock:
            return tuple(self._sampling_failures)

    @property
    def terminal_sample_observed_seconds(self) -> float | None:
        with self._lock:
            return self._terminal_sample_observed_seconds

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._observe_once():
                return
            self._stop.wait(self._sample_interval_seconds)

    def _observe_once(
        self,
        *,
        allow_transport_gap: bool = True,
        terminal: bool = False,
    ) -> bool:
        sample_started = self._monotonic_clock()
        try:
            raw = self._sampler()
            now = self._monotonic_clock()
            sample = MetricsSample(
                observed_seconds=max(0.0, now - self._started),
                running=raw.running,
                waiting=raw.waiting,
                preemptions=raw.preemptions,
                kv_cache=raw.kv_cache,
            )
            failure, waiting_since = metric_abort_reason(
                sample,
                baseline_preemptions=self._expected_preemptions,
                waiting_since=self._waiting_since,
                observed_at=now,
            )
            with self._lock:
                self._waiting_since = waiting_since
                self._samples.append(sample)
                if terminal:
                    self._terminal_sample_observed_seconds = sample.observed_seconds
                if failure is not None:
                    self._failure = failure
            return failure is None
        except MetricsTransportUnavailableError as exc:
            now = self._monotonic_clock()
            observed = MetricsSamplingFailure(
                observed_seconds=max(0.0, now - self._started),
                duration_seconds=max(0.0, now - sample_started),
                failure=f"{type(exc).__name__}:{_safe_detail(str(exc))}",
            )
            with self._lock:
                self._sampling_failures.append(observed)
                unsafe_to_tolerate = (
                    not allow_transport_gap
                    or observed.duration_seconds
                    > METRICS_LOGICAL_SAMPLE_TIMEOUT_SECONDS
                    or self._waiting_since is not None
                    or len(self._sampling_failures)
                    > MAX_TOLERATED_METRICS_SAMPLE_FAILURES_PER_STAGE
                )
                if unsafe_to_tolerate:
                    self._failure = f"metrics_unavailable:{observed.failure}"
            return not unsafe_to_tolerate
        except Exception as exc:
            with self._lock:
                self._failure = (
                    "metrics_unavailable:"
                    f"{type(exc).__name__}:{_safe_detail(str(exc))}"
                )
            return False


def parse_vllm_metrics(payload: bytes) -> MetricsSample:
    """Parse the exact four safety signals from Prometheus text."""

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("vLLM metrics are not UTF-8") from exc
    values: dict[str, list[float]] = {name: [] for name in _METRIC_ALIASES}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        metric_name = fields[0].partition("{")[0]
        matched = next(
            (
                name
                for name, aliases in _METRIC_ALIASES.items()
                if metric_name in aliases
            ),
            None,
        )
        if matched is None:
            continue
        try:
            value = float(fields[1])
        except ValueError as exc:
            raise ValueError(f"vLLM metric {metric_name} is not numeric") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"vLLM metric {metric_name} is invalid")
        values[matched].append(value)
    missing = sorted(name for name, samples in values.items() if not samples)
    if missing:
        raise ValueError(f"vLLM metrics missing required signals: {','.join(missing)}")
    return MetricsSample(
        observed_seconds=0.0,
        running=sum(values["running"]),
        waiting=sum(values["waiting"]),
        preemptions=sum(values["preemptions"]),
        kv_cache=max(values["kv_cache"]),
    )


def metric_abort_reason(
    sample: MetricsSample,
    *,
    baseline_preemptions: float,
    waiting_since: float | None,
    observed_at: float,
) -> tuple[str | None, float | None]:
    if sample.preemptions != baseline_preemptions:
        return "preemption_counter_changed", waiting_since
    if sample.waiting < WAITING_ABORT_THRESHOLD:
        return None, None
    if waiting_since is None:
        return None, observed_at
    if observed_at - waiting_since >= WAITING_ABORT_SECONDS:
        return "waiting_gte_64_for_30_seconds", waiting_since
    return None, waiting_since


def fetch_vllm_metrics(
    server_url: str,
    *,
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> MetricsSample:
    root = server_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3].rstrip("/")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    maximum_attempt_timeout = METRICS_TRANSPORT_ATTEMPT_TIMEOUT_SECONDS
    deadline = monotonic_clock() + METRICS_LOGICAL_SAMPLE_TIMEOUT_SECONDS
    last_transport_error: BaseException | None = None
    for attempt in range(1, METRICS_TRANSPORT_ATTEMPTS + 1):
        remaining_seconds = deadline - monotonic_clock()
        if remaining_seconds <= 0:
            raise MetricsTransportUnavailableError(
                "cannot read vLLM metrics: logical transport budget exhausted "
                f"after {attempt - 1} attempts"
            ) from last_transport_error
        attempt_timeout = min(maximum_attempt_timeout, remaining_seconds)
        try:
            with opener.open(root + "/metrics", timeout=attempt_timeout) as response:
                payload = response.read(_MAX_METRICS_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"cannot read vLLM metrics: {exc}") from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            last_transport_error = exc
            if attempt == METRICS_TRANSPORT_ATTEMPTS:
                raise MetricsTransportUnavailableError(
                    "cannot read vLLM metrics after "
                    f"{METRICS_TRANSPORT_ATTEMPTS} transport attempts: {exc}"
                ) from exc
            continue
        if not isinstance(payload, bytes) or len(payload) > _MAX_METRICS_BYTES:
            raise RuntimeError("vLLM metrics response exceeds the safety limit")
        sample = parse_vllm_metrics(payload)
        if monotonic_clock() > deadline:
            raise MetricsTransportUnavailableError(
                "vLLM metrics response exceeded the logical transport budget"
            )
        return sample
    raise AssertionError("unreachable vLLM metrics transport loop")


def execute_fixed_stage_sequence(
    stage_runner: Callable[[int], dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for concurrency in STAGE_DOCUMENT_CONCURRENCIES:
        result = stage_runner(concurrency)
        results.append(result)
        if result.get("status") != "pass":
            break
    return results


def _run_stage(
    *,
    concurrency: int,
    run_root: Path,
    input_bytes: bytes,
    input_digest: str,
    input_logical_name: str,
    mineru_bin: Path,
    api_url: str,
    inference_upstream_url: str,
    runtime_identity: str,
    timeout_seconds: int,
    expected_preemptions: float,
    metrics_sampler: Callable[[], MetricsSample],
    orchestrator_sampler: Callable[[], MinerUOrchestratorHealth],
    orchestrator_idle_waiter: Callable[
        [], tuple[MinerUOrchestratorHealth, float]
    ],
) -> dict[str, Any]:
    if concurrency not in STAGE_DOCUMENT_CONCURRENCIES:
        raise ValueError("MinerU staged load concurrency is not an approved stage")
    stage_started = time.monotonic()
    orchestrator_baseline, preflight_drain_seconds = orchestrator_idle_waiter()
    if orchestrator_baseline.active_tasks != 0:
        raise RuntimeError("MinerU orchestrator idle waiter returned active tasks")
    metrics_baseline = metrics_sampler()
    if metrics_baseline.preemptions != expected_preemptions:
        return _stage_preflight_failure(
            concurrency=concurrency,
            started=stage_started,
            metrics_baseline=metrics_baseline,
            orchestrator_baseline=orchestrator_baseline,
            preflight_drain_seconds=preflight_drain_seconds,
            failure="preemption_counter_changed_between_stages",
        )
    if metrics_baseline.running != 0 or metrics_baseline.waiting != 0:
        return _stage_preflight_failure(
            concurrency=concurrency,
            started=stage_started,
            metrics_baseline=metrics_baseline,
            orchestrator_baseline=orchestrator_baseline,
            preflight_drain_seconds=preflight_drain_seconds,
            failure="stage_remote_baseline_not_idle",
        )
    metrics_monitor = _MetricsMonitor(
        sampler=metrics_sampler,
        expected_preemptions=expected_preemptions,
    )
    orchestrator_monitor = _OrchestratorMonitor(sampler=orchestrator_sampler)
    outcomes: dict[int, dict[str, Any]] = {
        index: {
            "copy_index": index,
            "logical_name": f"{input_logical_name}.copy-{index:02d}",
            "input_sha256": f"sha256:{input_digest}",
            "status": "not_started",
        }
        for index in range(1, concurrency + 1)
    }
    failure: str | None = None
    orchestrator_terminal: MinerUOrchestratorHealth | None = None
    terminal_drain_seconds: float | None = None
    stage_tree: Path | None = None
    api_temp_before = mineru_api_temp_dirs()
    try:
        with tempfile.TemporaryDirectory(
            prefix=f"stage-{concurrency:02d}-",
            dir=run_root,
        ) as tmp:
            stage_tree = Path(tmp)
            metrics_monitor.start()
            orchestrator_monitor.start()
            with ThreadPoolExecutor(
                max_workers=concurrency,
                thread_name_prefix=f"mineru-stage-{concurrency}",
            ) as executor:
                futures: dict[Future[dict[str, Any]], int] = {
                    executor.submit(
                        _parse_frozen_copy,
                        copy_index=index,
                        stage_root=stage_tree,
                        input_bytes=input_bytes,
                        input_digest=input_digest,
                        input_logical_name=input_logical_name,
                        mineru_bin=mineru_bin,
                        api_url=api_url,
                        inference_upstream_url=inference_upstream_url,
                        runtime_identity=runtime_identity,
                        timeout_seconds=timeout_seconds,
                    ): index
                    for index in range(1, concurrency + 1)
                }
                pending = set(futures)
                while pending:
                    monitor_failure = (
                        metrics_monitor.failure or orchestrator_monitor.failure
                    )
                    if monitor_failure is not None:
                        failure = monitor_failure
                        break
                    done, pending = wait(
                        pending,
                        timeout=0.25,
                        return_when=FIRST_COMPLETED,
                    )
                    for future in done:
                        index = futures[future]
                        try:
                            outcome = future.result()
                        except Exception as exc:
                            outcome = _failed_document_outcome(
                                index,
                                input_digest,
                                exc,
                                input_logical_name=input_logical_name,
                            )
                        outcomes[index] = outcome
                        if outcome["status"] != "pass" and failure is None:
                            failure = str(outcome["failure_class"])
                    if failure is not None:
                        break
                if failure is not None:
                    for future in pending:
                        future.cancel()
                    terminate_active_mineru_processes()
            # ThreadPoolExecutor.__exit__ waits for every running future.  Read
            # terminal states only after that boundary; sampling them inside
            # the context can leave a completed/cancelled document recorded as
            # ``not_started`` in the immutable receipt.
            for future, index in futures.items():
                if future.cancelled():
                    outcomes[index]["status"] = "cancelled_after_stage_abort"
                    continue
                if not future.done():
                    outcomes[index] = _failed_document_outcome(
                        index,
                        input_digest,
                        RuntimeError("stage future unresolved after executor shutdown"),
                        input_logical_name=input_logical_name,
                    )
                    failure = failure or "stage_future_unresolved"
                    continue
                if outcomes[index]["status"] != "not_started":
                    continue
                try:
                    outcome = future.result()
                except Exception as exc:
                    outcome = _failed_document_outcome(
                        index,
                        input_digest,
                        exc,
                        input_logical_name=input_logical_name,
                    )
                if (
                    failure is not None
                    and outcome.get("failure_detail", "").startswith(
                        "ParserCancelledError:"
                    )
                ):
                    outcome["status"] = "cancelled_after_stage_abort"
                    outcome["failure_class"] = "stage_abort"
                outcomes[index] = outcome
    finally:
        try:
            orchestrator_terminal, terminal_drain_seconds = (
                orchestrator_idle_waiter()
            )
        except Exception as exc:
            failure = failure or (
                "orchestrator_terminal_drain_failed:"
                f"{type(exc).__name__}:{_safe_detail(str(exc))}"
            )
        orchestrator_monitor.stop()
        metrics_monitor.stop()
    cleanup_ok = stage_tree is not None and not stage_tree.exists()
    cleanup_observation_error: str | None = None
    api_temp_dirs_created = 0
    api_temp_cleanup_errors: list[str] = []
    try:
        remaining_processes = _wait_for_process_cleanup()
        if remaining_processes:
            terminate_active_mineru_processes()
            remaining_processes = _wait_for_process_cleanup()
        created_api_temp_dirs = mineru_api_temp_dirs() - api_temp_before
        api_temp_dirs_created = len(created_api_temp_dirs)
        if not remaining_processes:
            api_temp_cleanup_errors = _remove_api_temp_dirs(created_api_temp_dirs)
        new_api_temp_dirs = mineru_api_temp_dirs() - api_temp_before
    except Exception as exc:
        remaining_processes = {}
        new_api_temp_dirs = set()
        cleanup_observation_error = (
            "cleanup_observation_failed:"
            f"{type(exc).__name__}:{_safe_detail(str(exc))}"
        )
    metrics_samples = metrics_monitor.samples
    if metrics_monitor.failure is not None and failure is None:
        failure = metrics_monitor.failure
    if orchestrator_monitor.failure is not None and failure is None:
        failure = orchestrator_monitor.failure
    if not cleanup_ok and failure is None:
        failure = "stage_temporary_tree_not_removed"
    if cleanup_observation_error is not None and failure is None:
        failure = cleanup_observation_error
    if api_temp_cleanup_errors and failure is None:
        failure = "stage_api_temp_cleanup_failed"
    if (remaining_processes or new_api_temp_dirs) and failure is None:
        failure = (
            f"stage_cleanup_failed:pids={sorted(remaining_processes)}:"
            f"temp_dirs={len(new_api_temp_dirs)}"
        )
    if failure is not None:
        terminate_active_mineru_processes()
    if failure is None and any(item["status"] != "pass" for item in outcomes.values()):
        failure = "stage_document_incomplete"
    if failure is None and not _metrics_prove_staged_activity(
        metrics_baseline,
        metrics_samples,
    ):
        failure = "stage_metrics_observed_no_load_activity"
    orchestrator_failure = _orchestrator_evidence_failure(
        concurrency=concurrency,
        baseline=orchestrator_baseline,
        samples=orchestrator_monitor.samples,
        terminal=orchestrator_terminal,
    )
    if failure is None and orchestrator_failure is not None:
        failure = orchestrator_failure
    return {
        "client_document_concurrency": concurrency,
        "orchestrator_task_concurrency": ORCHESTRATOR_TASK_CONCURRENCY,
        "orchestrator_inference_concurrency": ORCHESTRATOR_INFERENCE_CONCURRENCY,
        "effective_inference_request_upper_bound": (
            EFFECTIVE_INFERENCE_REQUEST_UPPER_BOUND
        ),
        "status": "pass" if failure is None else "fail",
        "failure": failure,
        "elapsed_seconds": round(time.monotonic() - stage_started, 3),
        "documents": [outcomes[index] for index in sorted(outcomes)],
        "metrics": _metrics_summary(
            metrics_baseline,
            metrics_samples,
            metrics_monitor.sampling_failures,
            terminal_sample_observed_seconds=(
                metrics_monitor.terminal_sample_observed_seconds
            ),
        ),
        "orchestrator": _orchestrator_summary(
            baseline=orchestrator_baseline,
            samples=orchestrator_monitor.samples,
            terminal=orchestrator_terminal,
            preflight_drain_seconds=preflight_drain_seconds,
            terminal_drain_seconds=terminal_drain_seconds,
        ),
        "cleanup": {
            "temporary_tree_removed": cleanup_ok,
            "external_api_temp_dirs_created": api_temp_dirs_created,
            "external_api_temp_dirs_after": len(new_api_temp_dirs),
            "api_temp_cleanup_errors": api_temp_cleanup_errors,
            "external_mineru_processes_after": len(remaining_processes),
            "observation_error": cleanup_observation_error,
        },
    }


def _stage_preflight_failure(
    *,
    concurrency: int,
    started: float,
    metrics_baseline: MetricsSample,
    orchestrator_baseline: MinerUOrchestratorHealth,
    preflight_drain_seconds: float,
    failure: str,
) -> dict[str, Any]:
    return {
        "client_document_concurrency": concurrency,
        "orchestrator_task_concurrency": ORCHESTRATOR_TASK_CONCURRENCY,
        "orchestrator_inference_concurrency": ORCHESTRATOR_INFERENCE_CONCURRENCY,
        "effective_inference_request_upper_bound": (
            EFFECTIVE_INFERENCE_REQUEST_UPPER_BOUND
        ),
        "status": "fail",
        "failure": failure,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "documents": [],
        "metrics": _metrics_summary(metrics_baseline, ()),
        "orchestrator": _orchestrator_summary(
            baseline=orchestrator_baseline,
            samples=(),
            terminal=orchestrator_baseline,
            preflight_drain_seconds=preflight_drain_seconds,
            terminal_drain_seconds=0.0,
        ),
        "cleanup": {
            "temporary_tree_removed": True,
            "external_api_temp_dirs_created": 0,
            "external_api_temp_dirs_after": 0,
            "api_temp_cleanup_errors": [],
            "external_mineru_processes_after": 0,
            "observation_error": None,
        },
    }


def _parse_frozen_copy(
    *,
    copy_index: int,
    stage_root: Path,
    input_bytes: bytes,
    input_digest: str,
    input_logical_name: str,
    mineru_bin: Path,
    api_url: str,
    inference_upstream_url: str,
    runtime_identity: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    started = time.monotonic()
    document_root = stage_root / f"copy-{copy_index:02d}"
    private_tmp = document_root / "tmp"
    private_tmp.mkdir(parents=True)
    source = document_root / f"sha256_{input_digest}.pdf"
    source.write_bytes(input_bytes)
    if hashlib.sha256(source.read_bytes()).hexdigest() != input_digest:
        raise RuntimeError("frozen staged-load copy hash drifted")
    process = MinerUProcess(
        executable=mineru_bin,
        extra_env={
            "TEMP": str(private_tmp),
            "TMP": str(private_tmp),
            "TMPDIR": str(private_tmp),
        },
    )
    parser = MinerUMediumDocumentParser(
        process=process,
        api_url=api_url,
        server_url=inference_upstream_url,
    )
    try:
        result = parser.parse(
            input_pdf=source,
            output_dir=document_root / "output",
            options=ParserOptions(
                timeout_seconds=timeout_seconds,
                api_url=api_url,
                api_drain_timeout_seconds=timeout_seconds,
                server_url=inference_upstream_url,
                http_request_concurrency=None,
                runtime_bundle_identity_sha256=runtime_identity,
            ),
            source_pdf_sha256=f"sha256:{input_digest}",
        )
        provider = result.provider_document
        if len(provider.pages) < MINIMUM_INPUT_PAGES:
            raise ValueError(
                "staged-load input must produce at least "
                f"{MINIMUM_INPUT_PAGES} pages"
            )
        if provider.parser_version != "3.4.4":
            raise ValueError("staged-load parser version drifted")
        return {
            "copy_index": copy_index,
            "logical_name": f"{input_logical_name}.copy-{copy_index:02d}",
            "input_sha256": f"sha256:{input_digest}",
            "status": "pass",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "page_count": len(provider.pages),
            "block_count": len(provider.blocks),
            "provider_bundle_sha256": provider.bundle_sha256,
        }
    except Exception as exc:
        return _failed_document_outcome(
            copy_index,
            input_digest,
            exc,
            input_logical_name=input_logical_name,
            started=started,
        )


def _failed_document_outcome(
    copy_index: int,
    input_digest: str,
    exc: Exception,
    *,
    input_logical_name: str = "frozen-input.pdf",
    started: float | None = None,
) -> dict[str, Any]:
    raw_detail = f"{type(exc).__name__}:{' '.join(str(exc).split())}"
    detail = _safe_detail(raw_detail)
    return {
        "copy_index": copy_index,
        "logical_name": f"{input_logical_name}.copy-{copy_index:02d}",
        "input_sha256": f"sha256:{input_digest}",
        "status": "fail",
        # Classify the complete normalized diagnostic before rendering the
        # bounded receipt excerpt.  A marker in the omitted middle must not be
        # silently downgraded to a generic parse failure.
        "failure_class": _classify_failure(raw_detail),
        "failure_detail": detail,
        "failure_detail_chars": len(raw_detail),
        "failure_detail_sha256": (
            "sha256:" + hashlib.sha256(raw_detail.encode("utf-8")).hexdigest()
        ),
        "elapsed_seconds": (
            round(time.monotonic() - started, 3) if started is not None else None
        ),
    }


def _classify_failure(detail: str) -> str:
    lowered = detail.lower()
    markers = (
        ("429", "overload_429"),
        ("resource_exhausted", "overload_resource_exhausted"),
        ("overload", "overload"),
        ("out of memory", "gpu_oom"),
        ("cuda oom", "gpu_oom"),
        ("enginecore", "engine_core_failure"),
        ("engine core", "engine_core_failure"),
        ("http 5", "remote_5xx"),
        ("http error 5", "remote_5xx"),
        ("status code: [5", "remote_5xx"),
    )
    return next((label for marker, label in markers if marker in lowered), "parse_failure")


def _metrics_summary(
    baseline: MetricsSample,
    samples: tuple[MetricsSample, ...],
    sampling_failures: tuple[MetricsSamplingFailure, ...] = (),
    *,
    terminal_sample_observed_seconds: float | None = None,
) -> dict[str, Any]:
    observed = (baseline, *samples)
    distribution = samples or (baseline,)

    def p95(name: str) -> float:
        values = sorted(float(getattr(item, name)) for item in distribution)
        index = max(0, math.ceil(0.95 * len(values)) - 1)
        return values[index]

    return {
        "baseline": baseline.to_payload(),
        "sample_count": len(samples),
        "sampling_failures": [item.to_payload() for item in sampling_failures],
        "terminal_sample_observed_seconds": (
            round(terminal_sample_observed_seconds, 6)
            if terminal_sample_observed_seconds is not None
            else None
        ),
        "range": {
            name: {
                "min": min(getattr(item, name) for item in observed),
                "max": max(getattr(item, name) for item in observed),
            }
            for name in ("running", "waiting", "preemptions", "kv_cache")
        },
        "percentiles": {
            f"{name}_p95": p95(name)
            for name in ("running", "waiting", "kv_cache")
        },
    }


def _orchestrator_evidence_failure(
    *,
    concurrency: int,
    baseline: MinerUOrchestratorHealth,
    samples: tuple[OrchestratorSample, ...],
    terminal: MinerUOrchestratorHealth | None,
) -> str | None:
    if terminal is None:
        return "orchestrator_terminal_health_missing"
    if terminal.active_tasks != 0:
        return "orchestrator_terminal_not_idle"
    if terminal.completed_tasks - baseline.completed_tasks != concurrency:
        return "orchestrator_completed_delta_mismatch"
    if terminal.failed_tasks - baseline.failed_tasks != 0:
        return "orchestrator_failed_delta_changed"
    observed = (
        OrchestratorSample.from_health(baseline, observed_seconds=0.0),
        *samples,
        OrchestratorSample.from_health(terminal, observed_seconds=0.0),
    )
    if max(sample.processing_tasks for sample in observed) > (
        ORCHESTRATOR_TASK_CONCURRENCY
    ):
        return "orchestrator_processing_exceeded_3"
    if not samples or max(sample.processing_tasks for sample in samples) == 0:
        return "orchestrator_observed_no_processing_activity"
    if concurrency in (8, 16) and max(
        sample.queued_tasks for sample in samples
    ) == 0:
        return "orchestrator_queue_not_observed"
    return None


def _orchestrator_summary(
    *,
    baseline: MinerUOrchestratorHealth,
    samples: tuple[OrchestratorSample, ...],
    terminal: MinerUOrchestratorHealth | None,
    preflight_drain_seconds: float,
    terminal_drain_seconds: float | None,
) -> dict[str, Any]:
    observed = (
        OrchestratorSample.from_health(baseline, observed_seconds=0.0),
        *samples,
    )
    if terminal is not None:
        observed = (
            *observed,
            OrchestratorSample.from_health(
                terminal,
                observed_seconds=(
                    samples[-1].observed_seconds if samples else 0.0
                ),
            ),
        )
    return {
        "baseline": baseline.as_dict(),
        "samples": [sample.to_payload() for sample in samples],
        "sample_count": len(samples),
        "terminal": terminal.as_dict() if terminal is not None else None,
        "completed_delta": (
            terminal.completed_tasks - baseline.completed_tasks
            if terminal is not None
            else None
        ),
        "failed_delta": (
            terminal.failed_tasks - baseline.failed_tasks
            if terminal is not None
            else None
        ),
        "terminal_active_tasks": (
            terminal.active_tasks if terminal is not None else None
        ),
        "preflight_drain_seconds": round(preflight_drain_seconds, 6),
        "terminal_drain_seconds": (
            round(terminal_drain_seconds, 6)
            if terminal_drain_seconds is not None
            else None
        ),
        "stop_semantics": "drain-not-cancel.v1",
        "range": {
            name: {
                "min": min(getattr(sample, name) for sample in observed),
                "max": max(getattr(sample, name) for sample in observed),
            }
            for name in (
                "queued_tasks",
                "processing_tasks",
                "completed_tasks",
                "failed_tasks",
            )
        },
    }


def _metrics_prove_staged_activity(
    baseline: MetricsSample,
    samples: tuple[MetricsSample, ...],
) -> bool:
    return bool(
        samples
        and (
            max(sample.running for sample in samples) > baseline.running
            or max(sample.kv_cache for sample in samples) > baseline.kv_cache
        )
    )


def _wait_for_process_cleanup() -> dict[int, str]:
    deadline = time.monotonic() + 5
    while True:
        remaining = mineru_processes(process_snapshot())
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(0.25)


def _remove_api_temp_dirs(paths: set[Path]) -> list[str]:
    errors: list[str] = []
    for path in sorted(paths):
        if not path.name.startswith("mineru-api-client-"):
            errors.append(f"refused_unexpected_path:{path.name}")
            continue
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{path.name}:{type(exc).__name__}:{_safe_detail(str(exc))}")
    return errors


def _write_new_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _safe_detail(value: str) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= _MAX_SAFE_DETAIL_CHARS:
        return normalized
    remaining = _MAX_SAFE_DETAIL_CHARS - len(_SAFE_DETAIL_TRUNCATION_MARKER)
    head_chars = remaining // 2
    tail_chars = remaining - head_chars
    return (
        normalized[:head_chars]
        + _SAFE_DETAIL_TRUNCATION_MARKER
        + normalized[-tail_chars:]
    )


def _endpoint_sha256(url: str) -> str:
    return hashlib.sha256(url.rstrip("/").encode("utf-8")).hexdigest()


def _verify_endpoint_identity(
    topology: object,
    *,
    field: str,
    url: str,
) -> None:
    if not isinstance(topology, dict):
        raise ValueError("runtime manifest topology must be an object")
    if topology.get(field) != "sha256:" + _endpoint_sha256(url):
        raise ValueError(f"runtime manifest topology {field} drifted")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mineru_staged_load", description=__doc__)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--receipt-out", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--expected-input-sha256", required=True)
    parser.add_argument("--mineru-bin", type=Path)
    parser.add_argument("--api-url")
    parser.add_argument("--observability-url")
    parser.add_argument("--inference-upstream-url")
    parser.add_argument("--runtime-bundle-identity")
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.receipt_out.exists() or args.receipt_out.is_symlink():
        raise SystemExit(f"[abort] output already exists; stale evidence: {args.receipt_out}")
    mineru_bin = args.mineru_bin or (
        Path(value) if (value := os.environ.get("DISCLOSURE_MINERU_BIN")) else None
    )
    api_url = args.api_url or os.environ.get("DISCLOSURE_MINERU_API_URL")
    observability_url = args.observability_url or os.environ.get(
        "DISCLOSURE_MINERU_OBSERVABILITY_URL"
    )
    inference_upstream_url = args.inference_upstream_url or os.environ.get(
        "DISCLOSURE_MINERU_INFERENCE_UPSTREAM_URL"
    )
    runtime_identity = args.runtime_bundle_identity or os.environ.get(
        "DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256"
    )
    if mineru_bin is None or not mineru_bin.is_file():
        raise SystemExit("[abort] DISCLOSURE_MINERU_BIN is missing or not a file")
    if not api_url or not observability_url or not inference_upstream_url:
        raise SystemExit("[abort] complete MinerU fixed-API topology is required")
    if not _is_prefixed_sha256(runtime_identity):
        raise SystemExit("[abort] runtime bundle identity is missing or invalid")
    if args.work_root is not None and not args.work_root.is_dir():
        raise SystemExit(f"[abort] work-root is not a directory: {args.work_root}")
    if args.timeout_seconds < 1:
        raise SystemExit("[abort] timeout-seconds must be positive")
    if os.environ.get("MINERU_PROCESSING_WINDOW_SIZE") != str(
        MINERU_PROCESSING_WINDOW_SIZE
    ):
        raise SystemExit("[abort] MINERU_PROCESSING_WINDOW_SIZE must be 16")
    if args.input.is_symlink() or not args.input.is_file():
        raise SystemExit(f"[abort] frozen input is missing or unsafe: {args.input}")
    input_bytes = args.input.read_bytes()
    input_digest = hashlib.sha256(input_bytes).hexdigest()
    if _normalized_sha256(args.expected_input_sha256) != input_digest:
        raise SystemExit("[abort] frozen input hash drifted")
    input_sha256 = f"sha256:{input_digest}"

    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat()
    stage_results: list[dict[str, Any]] = []
    failure: str | None = None
    identity_payload: dict[str, Any] = {}
    run_tree: Path | None = None
    api_temp_before: set[Path] = set()
    remaining_processes: dict[int, str] = {}
    new_api_temp_dirs: set[Path] = set()
    temporary_tree_removed = True
    cleanup_observation_error: str | None = None
    api_temp_dirs_created = 0
    api_temp_cleanup_errors: list[str] = []
    try:
        api_temp_before = mineru_api_temp_dirs()
        if api_temp_before:
            raise RuntimeError(
                "pre-existing MinerU API temporary directories require cleanup"
            )
        before = process_snapshot()
        if producers := active_disclosure_producers(before):
            raise RuntimeError(
                f"disclosure producer processes are active: {sorted(producers)}"
            )
        if existing_mineru := mineru_processes(before):
            raise RuntimeError(
                f"pre-existing MinerU processes require cleanup: {sorted(existing_mineru)}"
            )
        local_client = client_bundle_identity(mineru_bin)
        code_digest = writer_code_digest()
        manifest_payload = json.loads(args.runtime_manifest.read_bytes())
        verified = verify_runtime_manifest_payload(
            manifest_payload,
            configured_identity=str(runtime_identity),
            local_client_identity=local_client,
            local_processing_window_size=MINERU_PROCESSING_WINDOW_SIZE,
            local_writer_code_digest=code_digest,
        )
        topology = verified.manifest["topology"]
        _verify_endpoint_identity(
            topology,
            field="api_endpoint_sha256",
            url=api_url,
        )
        _verify_endpoint_identity(
            topology,
            field="observability_endpoint_sha256",
            url=observability_url,
        )
        _verify_endpoint_identity(
            topology,
            field="inference_upstream_sha256",
            url=inference_upstream_url,
        )
        orchestrator_manifest = verified.manifest["orchestrator"]
        expected_task_retention_seconds = int(
            orchestrator_manifest["task_retention_seconds"]
        )
        expected_cleanup_interval_seconds = int(
            orchestrator_manifest["task_cleanup_interval_seconds"]
        )
        probe_mineru_served_model(
            observability_url,
            expected_model_id=verified.served_model_id,
        )
        global_metrics_baseline = fetch_vllm_metrics(observability_url)
        identity_payload = {
            "local_client_identity_sha256": local_client.package_set_sha256,
            "local_content_package_versions": dict(
                local_client.content_package_versions
            ),
            "local_processing_window_size": MINERU_PROCESSING_WINDOW_SIZE,
            "local_writer_code_sha256": code_digest,
            "runtime_manifest_identity_sha256": verified.identity_sha256,
            "orchestrator_runtime_identity_sha256": (
                verified.orchestrator_identity_sha256
            ),
            "provider_runtime_identity_sha256": verified.provider_identity_sha256,
            "served_model_id": verified.served_model_id,
        }
        with tempfile.TemporaryDirectory(
            prefix="disclosure-mineru-staged-load-",
            dir=args.work_root,
        ) as tmp:
            run_tree = Path(tmp)
            stage_results = execute_fixed_stage_sequence(
                lambda concurrency: _run_stage(
                    concurrency=concurrency,
                    run_root=run_tree,
                    input_bytes=input_bytes,
                    input_digest=input_digest,
                    input_logical_name=args.input.name,
                    mineru_bin=mineru_bin,
                    api_url=api_url,
                    inference_upstream_url=inference_upstream_url,
                    runtime_identity=str(runtime_identity),
                    timeout_seconds=args.timeout_seconds,
                    expected_preemptions=global_metrics_baseline.preemptions,
                    metrics_sampler=lambda: fetch_vllm_metrics(observability_url),
                    orchestrator_sampler=lambda: (
                        fetch_mineru_orchestrator_health(
                            api_url,
                            expected_task_retention_seconds=(
                                expected_task_retention_seconds
                            ),
                            expected_cleanup_interval_seconds=(
                                expected_cleanup_interval_seconds
                            ),
                        )
                    ),
                    orchestrator_idle_waiter=lambda: (
                        wait_for_mineru_orchestrator_idle(
                            api_url,
                            timeout_seconds=args.timeout_seconds,
                            expected_task_retention_seconds=(
                                expected_task_retention_seconds
                            ),
                            expected_cleanup_interval_seconds=(
                                expected_cleanup_interval_seconds
                            ),
                        )
                    ),
                )
            )
        if len(stage_results) != len(STAGE_DOCUMENT_CONCURRENCIES) or any(
            result.get("status") != "pass" for result in stage_results
        ):
            failure = "staged_load_stopped_before_all_fixed_stages_passed"
    except Exception as exc:
        failure = f"{type(exc).__name__}:{_safe_detail(str(exc))}"
    finally:
        try:
            remaining_processes = _wait_for_process_cleanup()
            if remaining_processes:
                terminate_active_mineru_processes()
                remaining_processes = _wait_for_process_cleanup()
            created_api_temp_dirs = mineru_api_temp_dirs() - api_temp_before
            api_temp_dirs_created = len(created_api_temp_dirs)
            if not remaining_processes:
                api_temp_cleanup_errors = _remove_api_temp_dirs(
                    created_api_temp_dirs
                )
            new_api_temp_dirs = mineru_api_temp_dirs() - api_temp_before
            temporary_tree_removed = run_tree is None or not run_tree.exists()
        except Exception as exc:
            cleanup_observation_error = (
                "cleanup_observation_failed:"
                f"{type(exc).__name__}:{_safe_detail(str(exc))}"
            )
            failure = failure or cleanup_observation_error
        if (
            remaining_processes
            or new_api_temp_dirs
            or api_temp_cleanup_errors
            or not temporary_tree_removed
        ):
            cleanup_failure = (
                f"cleanup_failed:pids={sorted(remaining_processes)}:"
                f"temp_dirs={len(new_api_temp_dirs)}:"
                f"temp_cleanup_errors={len(api_temp_cleanup_errors)}:"
                f"tree={temporary_tree_removed}"
            )
            failure = failure or cleanup_failure

    receipt_status = "pass" if failure is None else "fail"
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "execution_id": str(uuid.uuid4()),
        "status": receipt_status,
        "started_at_utc": started_at,
        "finished_at_utc": datetime.now(UTC).isoformat(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "topology": {
            "api_endpoint_sha256": "sha256:" + _endpoint_sha256(api_url),
            "observability_endpoint_sha256": (
                "sha256:" + _endpoint_sha256(observability_url)
            ),
            "inference_upstream_sha256": (
                "sha256:" + _endpoint_sha256(inference_upstream_url)
            ),
        },
        "database_access": "none",
        "queue_access": "none",
        "fixed_stage_client_document_concurrency": list(
            STAGE_DOCUMENT_CONCURRENCIES
        ),
        "orchestrator_task_concurrency": ORCHESTRATOR_TASK_CONCURRENCY,
        "orchestrator_inference_concurrency": ORCHESTRATOR_INFERENCE_CONCURRENCY,
        "effective_inference_request_upper_bound": (
            EFFECTIVE_INFERENCE_REQUEST_UPPER_BOUND
        ),
        "input": {
            "profile": "operator_frozen_representative_v1",
            "logical_name": args.input.name,
            "sha256": input_sha256,
            "bytes": len(input_bytes),
            "minimum_required_pages": MINIMUM_INPUT_PAGES,
        },
        "identity": identity_payload,
        "stages": stage_results,
        "failure": failure,
        "cleanup": {
            "external_api_temp_dirs_created": api_temp_dirs_created,
            "external_api_temp_dirs_after": len(new_api_temp_dirs),
            "api_temp_cleanup_errors": api_temp_cleanup_errors,
            "external_mineru_processes_after": len(remaining_processes),
            "temporary_tree_removed": temporary_tree_removed,
            "observation_error": cleanup_observation_error,
        },
    }
    _write_new_json(args.receipt_out, receipt)
    print(
        f"mineru-staged-load: {receipt_status.upper()} "
        f"stages={len(stage_results)}/{len(STAGE_DOCUMENT_CONCURRENCIES)} "
        f"receipt={args.receipt_out}"
    )
    return 0 if failure is None else 2


def _is_prefixed_sha256(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _normalized_sha256(value: object) -> str:
    if not isinstance(value, str):
        raise SystemExit("[abort] expected input SHA-256 is invalid")
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise SystemExit("[abort] expected input SHA-256 is invalid")
    return digest


if __name__ == "__main__":
    raise SystemExit(main())
