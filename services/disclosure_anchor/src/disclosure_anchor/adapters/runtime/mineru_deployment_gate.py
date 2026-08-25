"""Fail-closed deployment evidence and resident MinerU admission checker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import threading
import time
from typing import Any
import uuid

from disclosure_anchor.adapters.runtime.mineru_canary import (
    CANARY_SCHEMA,
    MinerUCanaryError,
    MinerUCanaryUnavailableError,
    canary_cache_is_fresh,
    canary_request_sha256,
    model_id_sha256,
    probe_mineru_served_model,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_API_INFERENCE_MAX_CONCURRENCY,
    MINERU_API_MAX_CONCURRENT_REQUESTS,
    MINERU_PROCESSING_WINDOW_SIZE,
    MINERU_SMOKE_INPUT_NAME,
    MINERU_SMOKE_INPUT_SHA256,
    client_bundle_identity,
    verify_runtime_manifest_payload,
    writer_code_digest,
)
from disclosure_anchor.adapters.runtime.mineru_orchestrator import (
    MinerUOrchestratorError,
    MinerUOrchestratorUnavailableError,
    fetch_mineru_orchestrator_health,
    mineru_orchestrator_incident_state,
)
from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
    ParserTargetIdentityError,
)
from disclosure_anchor.settings import Settings


_RECEIPT_SCHEMA = "mineru_smoke_receipt.v2"
_STAGED_LOAD_RECEIPT_SCHEMA = "mineru_staged_load_receipt.v2"
_DEPLOYMENT_INPUT_PROFILE = "deployment_frozen_v1"
_STAGED_LOAD_INPUT_PROFILE = "operator_frozen_representative_v1"
_DEPLOYMENT_CANARY_ATTEMPTS = 3
_STAGED_DOCUMENT_CONCURRENCIES = (4, 8, 16)
_EFFECTIVE_INFERENCE_REQUEST_UPPER_BOUND = 21
_MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
_EXPECTED_CLEANUP = {
    "external_api_temp_dirs_created": 0,
    "external_mineru_processes_after": 0,
    "temporary_tree_removed": True,
    "retained_parse_artifacts": 0,
    "remote_active_tasks_after": 0,
}


class MinerUDeploymentGateError(RuntimeError):
    """The current process cannot prove its MinerU deployment gate."""


class MinerUDeploymentUnavailableError(MinerUDeploymentGateError):
    """A live admission endpoint is temporarily unavailable."""


@dataclass(frozen=True)
class VerifiedMinerUDeployment:
    api_url: str
    observability_url: str
    inference_upstream_url: str
    runtime_identity_sha256: str
    served_model_id: str
    canary_passed_at_utc: datetime
    canary_max_age_seconds: int
    task_retention_seconds: int
    task_cleanup_interval_seconds: int

    def assert_fresh(self, *, now: datetime | None = None) -> None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        age = (current - self.canary_passed_at_utc).total_seconds()
        if age < 0 or age > self.canary_max_age_seconds:
            raise MinerUDeploymentGateError("MinerU canary cache is stale")

    def probe_live_model(self) -> None:
        try:
            probe_mineru_served_model(
                self.observability_url,
                expected_model_id=self.served_model_id,
            )
        except MinerUCanaryUnavailableError as exc:
            raise MinerUDeploymentUnavailableError(
                f"MinerU live served-model probe unavailable: {exc}"
            ) from exc
        except MinerUCanaryError as exc:
            raise MinerUDeploymentGateError(
                f"MinerU live served-model probe failed: {exc}"
            ) from exc

    def probe_orchestrator(self, *, require_idle: bool) -> None:
        try:
            health = fetch_mineru_orchestrator_health(
                self.api_url,
                expected_task_retention_seconds=self.task_retention_seconds,
                expected_cleanup_interval_seconds=(
                    self.task_cleanup_interval_seconds
                ),
            )
        except MinerUOrchestratorUnavailableError as exc:
            raise MinerUDeploymentUnavailableError(
                f"MinerU API live health probe unavailable: {exc}"
            ) from exc
        except MinerUOrchestratorError as exc:
            raise MinerUDeploymentGateError(
                f"MinerU API live health probe failed: {exc}"
            ) from exc
        if require_idle and health.active_tasks != 0:
            raise MinerUDeploymentUnavailableError(
                "MinerU API has undrained work before process admission"
            )


class MinerUDeploymentChecker:
    """Thread-safe, rate-limited guard for resident parse admission.

    Static source/package/receipt evidence, including its wall-clock startup
    lease, is verified once before composition.  A healthy resident may run
    past that fixed proof's timestamp: each admission is then guarded by the
    process-local incident generation and rate-limited live API/model probes.
    Any new process composition rechecks the complete static lease.  A caller
    should treat an error as infrastructure admission stop, not a document
    failure.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        parse_enabled: bool | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._evidence = verify_mineru_deployment_gate(
            settings,
            parse_enabled=parse_enabled,
            now=self._wall_clock(),
        )
        self._probe_interval_seconds = (
            settings.disclosure_mineru_live_probe_interval_seconds
        )
        # A fresh static receipt proves the last smoke, not current liveness.
        # Force the first admission to probe before any document can start.
        self._last_probe_success: float | None = None
        self._initial_idle_proved = False
        self._incident_generation = (
            mineru_orchestrator_incident_state().generation
        )
        self._probe_lock = threading.Lock()

    def assert_admission(self) -> None:
        evidence = self._evidence
        if evidence is None:
            return
        incident_state = mineru_orchestrator_incident_state()
        if incident_state.drains_in_progress:
            raise MinerUDeploymentUnavailableError(
                "MinerU API incident drain is still in progress"
            )
        incident_generation = incident_state.generation
        incident_changed = incident_generation != self._incident_generation
        observed = self._monotonic_clock()
        if (
            not incident_changed
            and self._last_probe_success is not None
            and observed - self._last_probe_success < self._probe_interval_seconds
        ):
            return
        with self._probe_lock:
            incident_state = mineru_orchestrator_incident_state()
            if incident_state.drains_in_progress:
                raise MinerUDeploymentUnavailableError(
                    "MinerU API incident drain is still in progress"
                )
            incident_generation = incident_state.generation
            incident_changed = incident_generation != self._incident_generation
            observed = self._monotonic_clock()
            if (
                not incident_changed
                and self._last_probe_success is not None
                and observed - self._last_probe_success < self._probe_interval_seconds
            ):
                return
            # An incident invalidates only the cached live proof. Recovery is
            # safe after this checker freshly proves the fixed API is idle and
            # the exact served-model identity still matches attestation.
            evidence.probe_orchestrator(
                require_idle=(not self._initial_idle_proved or incident_changed)
            )
            evidence.probe_live_model()
            confirmed_incident_state = mineru_orchestrator_incident_state()
            if (
                confirmed_incident_state.drains_in_progress
                or confirmed_incident_state.generation != incident_generation
            ):
                raise MinerUDeploymentUnavailableError(
                    "MinerU API incident changed during live admission proof"
                )
            self._incident_generation = incident_generation
            self._initial_idle_proved = True
            self._last_probe_success = self._monotonic_clock()


def _load_evidence(
    path: Path,
    *,
    label: str,
) -> tuple[dict[str, Any], tuple[int, int]]:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise MinerUDeploymentGateError(
                f"{label} must be an owner-only 0600 regular file with one link"
            )
        if metadata.st_size > _MAX_EVIDENCE_BYTES:
            raise MinerUDeploymentGateError(f"{label} exceeds the size limit")
        with os.fdopen(descriptor, "rb") as evidence_file:
            descriptor = None
            encoded = evidence_file.read(_MAX_EVIDENCE_BYTES + 1)
            final_metadata = os.fstat(evidence_file.fileno())
        if len(encoded) > _MAX_EVIDENCE_BYTES:
            raise MinerUDeploymentGateError(f"{label} exceeds the size limit")
        if (
            final_metadata.st_dev,
            final_metadata.st_ino,
            final_metadata.st_mode,
            final_metadata.st_uid,
            final_metadata.st_nlink,
            final_metadata.st_size,
        ) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_nlink,
            metadata.st_size,
        ):
            raise MinerUDeploymentGateError(f"{label} changed while being read")
        payload = json.loads(encoded)
    except MinerUDeploymentGateError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise MinerUDeploymentGateError(f"{label} cannot be read: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(payload, dict):
        raise MinerUDeploymentGateError(f"{label} root must be an object")
    return payload, (metadata.st_dev, metadata.st_ino)


def verify_mineru_deployment_gate(
    settings: Settings,
    *,
    parse_enabled: bool | None = None,
    now: datetime | None = None,
) -> VerifiedMinerUDeployment | None:
    """Verify the exact DB-free receipt before a parse-capable composition."""

    enabled = (
        settings.worker_batch_parse != 0 if parse_enabled is None else parse_enabled
    )
    if not enabled:
        return None
    if settings.mineru_processing_window_size != MINERU_PROCESSING_WINDOW_SIZE:
        raise MinerUDeploymentGateError(
            "MINERU_PROCESSING_WINDOW_SIZE drifted from the deployment contract"
        )
    if (
        settings.worker_parse_concurrency > _STAGED_DOCUMENT_CONCURRENCIES[-1]
        or settings.disclosure_mineru_api_task_slots
        != MINERU_API_MAX_CONCURRENT_REQUESTS
        or settings.disclosure_mineru_api_inference_concurrency
        != MINERU_API_INFERENCE_MAX_CONCURRENCY
        or settings.worker_gpu_request_budget
        != _EFFECTIVE_INFERENCE_REQUEST_UPPER_BOUND
        or settings.worker_gpu_max_sequences != 128
    ):
        raise MinerUDeploymentGateError(
            "MinerU worker/API fan-out exceeds the staged 16-submit/3x7-active "
            "envelope or max sequences drifted from 128"
        )
    api_url = settings.disclosure_mineru_api_url
    observability_url = settings.disclosure_mineru_observability_url
    inference_upstream_url = settings.disclosure_mineru_inference_upstream_url
    runtime_identity = settings.disclosure_mineru_runtime_bundle_identity_sha256
    mineru_bin = settings.disclosure_mineru_bin
    receipt_path = settings.disclosure_mineru_smoke_receipt
    cache_path = settings.disclosure_mineru_canary_cache
    staged_load_path = settings.disclosure_mineru_staged_load_receipt
    staged_confirmation_path = (
        settings.disclosure_mineru_staged_load_confirmation_receipt
    )
    staged_input_sha256 = settings.disclosure_mineru_staged_input_sha256
    if (
        not api_url
        or not observability_url
        or not inference_upstream_url
        or not runtime_identity
        or mineru_bin is None
        or not staged_input_sha256
    ):
        raise MinerUDeploymentGateError(
            "MinerU fixed-API topology, executable, runtime identity, and "
            "pinned staged input are required"
        )
    if (
        receipt_path is None
        or cache_path is None
        or staged_load_path is None
        or staged_confirmation_path is None
    ):
        raise MinerUDeploymentGateError(
            "DISCLOSURE_MINERU_SMOKE_RECEIPT and "
            "DISCLOSURE_MINERU_CANARY_CACHE and "
            "both DISCLOSURE_MINERU_STAGED_LOAD receipts are required"
        )
    evidence_paths = {
        receipt_path.resolve(strict=False),
        cache_path.resolve(strict=False),
        staged_load_path.resolve(strict=False),
        staged_confirmation_path.resolve(strict=False),
    }
    if len(evidence_paths) != 4:
        raise MinerUDeploymentGateError("MinerU evidence paths must differ")
    receipt, receipt_file_identity = _load_evidence(
        receipt_path, label="MinerU smoke receipt"
    )
    cache, cache_file_identity = _load_evidence(
        cache_path, label="MinerU canary cache"
    )
    staged_load, staged_load_file_identity = _load_evidence(
        staged_load_path,
        label="MinerU staged-load receipt",
    )
    staged_confirmation, staged_confirmation_file_identity = _load_evidence(
        staged_confirmation_path,
        label="MinerU staged-load confirmation receipt",
    )
    if len(
        {
            receipt_file_identity,
            cache_file_identity,
            staged_load_file_identity,
            staged_confirmation_file_identity,
        }
    ) != 4:
        raise MinerUDeploymentGateError(
            "MinerU evidence files must not be hard-linked aliases"
        )
    if receipt.get("schema") != _RECEIPT_SCHEMA or receipt.get("status") != "pass":
        raise MinerUDeploymentGateError("MinerU smoke receipt is not PASS")
    if cache.get("schema") != CANARY_SCHEMA or receipt.get("canary") != cache:
        raise MinerUDeploymentGateError("MinerU receipt/cache pair does not match")
    current = now or datetime.now(UTC)
    if not canary_cache_is_fresh(
        cache,
        observability_url=observability_url,
        runtime_bundle_identity_sha256=runtime_identity,
        max_age_seconds=settings.disclosure_mineru_canary_max_age_seconds,
        now=current,
    ):
        raise MinerUDeploymentGateError("MinerU canary cache is stale or drifted")
    if cache.get("attempts") != _DEPLOYMENT_CANARY_ATTEMPTS:
        raise MinerUDeploymentGateError(
            "MinerU deployment canary must have exactly three attempts"
        )

    try:
        local_client = client_bundle_identity(mineru_bin)
        local_code_digest = writer_code_digest()
        verified_manifest = verify_runtime_manifest_payload(
            {
                "identity_sha256": runtime_identity,
                "manifest": receipt.get("runtime_manifest"),
            },
            configured_identity=runtime_identity,
            local_client_identity=local_client,
            local_processing_window_size=MINERU_PROCESSING_WINDOW_SIZE,
            local_writer_code_digest=local_code_digest,
        )
    except (OSError, ValueError) as exc:
        raise MinerUDeploymentGateError(
            f"MinerU exact runtime identity cannot be verified: {exc}"
        ) from exc

    identity = receipt.get("identity")
    if not isinstance(identity, dict):
        raise MinerUDeploymentGateError("MinerU receipt identity is invalid")
    expected_identity = {
        "local_client_identity_sha256": local_client.package_set_sha256,
        "local_content_package_versions": dict(local_client.content_package_versions),
        "local_processing_window_size": MINERU_PROCESSING_WINDOW_SIZE,
        "local_writer_code_sha256": local_code_digest,
        "runtime_manifest_identity_sha256": runtime_identity,
        "orchestrator_runtime_identity_sha256": (
            verified_manifest.orchestrator_identity_sha256
        ),
        "provider_runtime_identity_sha256": (
            verified_manifest.provider_identity_sha256
        ),
        "served_model_id": verified_manifest.served_model_id,
    }
    if identity != expected_identity:
        raise MinerUDeploymentGateError("MinerU receipt identity drifted")

    expected_topology = {
        "api_endpoint_sha256": _endpoint_sha256(api_url),
        "observability_endpoint_sha256": _endpoint_sha256(observability_url),
        "inference_upstream_sha256": _endpoint_sha256(inference_upstream_url),
    }
    manifest_topology = verified_manifest.manifest["topology"]
    if (
        receipt.get("topology") != expected_topology
        or any(
            manifest_topology.get(field) != value
            for field, value in expected_topology.items()
        )
    ):
        raise MinerUDeploymentGateError("MinerU endpoint topology drifted")

    input_evidence = receipt.get("input")
    if (
        not isinstance(input_evidence, dict)
        or input_evidence.get("profile") != _DEPLOYMENT_INPUT_PROFILE
        or input_evidence.get("logical_name") != MINERU_SMOKE_INPUT_NAME
        or input_evidence.get("sha256") != MINERU_SMOKE_INPUT_SHA256
        or isinstance(input_evidence.get("bytes"), bool)
        or not isinstance(input_evidence.get("bytes"), int)
        or input_evidence["bytes"] < 1
    ):
        raise MinerUDeploymentGateError("MinerU frozen smoke input is invalid")

    served_model_id = verified_manifest.served_model_id
    if cache.get("model_id_sha256") != model_id_sha256(served_model_id):
        raise MinerUDeploymentGateError("MinerU canary model identity drifted")
    if cache.get("request_sha256") != canary_request_sha256(served_model_id):
        raise MinerUDeploymentGateError("MinerU canary request identity drifted")

    _verify_smoke_orchestrator(
        receipt.get("orchestrator"),
        task_retention_seconds=(
            settings.disclosure_mineru_api_task_retention_seconds
        ),
        cleanup_interval_seconds=(
            settings.disclosure_mineru_api_cleanup_interval_seconds
        ),
    )

    cleanup = receipt.get("cleanup")
    if cleanup != _EXPECTED_CLEANUP:
        raise MinerUDeploymentGateError("MinerU smoke cleanup was not proved")
    provider = receipt.get("provider")
    _verify_provider_evidence(provider, runtime_identity=runtime_identity)
    if (
        receipt.get("database_access") != "none"
        or receipt.get("queue_access") != "none"
    ):
        raise MinerUDeploymentGateError("MinerU smoke was not DB/queue free")

    smoke_started_at = _required_aware_timestamp(
        receipt.get("started_at_utc"),
        label="MinerU smoke start",
    )
    passed_at = _required_aware_timestamp(
        cache.get("passed_at_utc"),
        label="MinerU canary",
    )
    smoke_finished_at = _required_aware_timestamp(
        receipt.get("finished_at_utc"),
        label="MinerU smoke finish",
    )
    current_utc = current.astimezone(UTC)
    smoke_elapsed = _nonnegative_finite_value(receipt.get("elapsed_seconds"))
    if (
        smoke_elapsed is None
        or not smoke_started_at < smoke_finished_at <= current_utc
        or not smoke_started_at <= passed_at <= smoke_finished_at
        or not _elapsed_matches_timeline(
            smoke_elapsed,
            started_at=smoke_started_at,
            finished_at=smoke_finished_at,
        )
    ):
        raise MinerUDeploymentGateError("MinerU smoke/canary timeline is invalid")
    (
        first_staged_input,
        first_execution_id,
        first_orchestrator_terminal,
    ) = _verify_staged_load_receipt(
        staged_load,
        expected_identity=expected_identity,
        expected_topology=expected_topology,
        smoke_finished_at=smoke_finished_at,
        current=current,
        max_age_seconds=settings.disclosure_mineru_canary_max_age_seconds,
        expected_input_sha256=staged_input_sha256,
    )
    first_staged_finished = _required_aware_timestamp(
        staged_load.get("finished_at_utc"),
        label="MinerU first staged-load finish",
    )
    (
        _,
        confirmation_execution_id,
        _,
    ) = _verify_staged_load_receipt(
        staged_confirmation,
        expected_identity=expected_identity,
        expected_topology=expected_topology,
        smoke_finished_at=first_staged_finished,
        current=current,
        max_age_seconds=settings.disclosure_mineru_canary_max_age_seconds,
        expected_input=first_staged_input,
        expected_input_sha256=staged_input_sha256,
        expected_orchestrator_baseline=first_orchestrator_terminal,
    )
    if confirmation_execution_id == first_execution_id:
        raise MinerUDeploymentGateError(
            "MinerU staged-load confirmation is not an independent execution"
        )
    evidence = VerifiedMinerUDeployment(
        api_url=api_url,
        observability_url=observability_url,
        inference_upstream_url=inference_upstream_url,
        runtime_identity_sha256=runtime_identity,
        served_model_id=served_model_id,
        canary_passed_at_utc=passed_at,
        canary_max_age_seconds=settings.disclosure_mineru_canary_max_age_seconds,
        task_retention_seconds=(
            settings.disclosure_mineru_api_task_retention_seconds
        ),
        task_cleanup_interval_seconds=(
            settings.disclosure_mineru_api_cleanup_interval_seconds
        ),
    )
    evidence.assert_fresh(now=current)
    return evidence


def _verify_staged_load_receipt(
    receipt: dict[str, Any],
    *,
    expected_identity: dict[str, Any],
    expected_topology: dict[str, str],
    smoke_finished_at: datetime,
    current: datetime,
    max_age_seconds: int,
    expected_input_sha256: str,
    expected_input: dict[str, object] | None = None,
    expected_orchestrator_baseline: dict[str, int | str] | None = None,
) -> tuple[dict[str, object], str, dict[str, int | str]]:
    if (
        receipt.get("schema") != _STAGED_LOAD_RECEIPT_SCHEMA
        or receipt.get("status") != "pass"
        or receipt.get("failure") is not None
    ):
        raise MinerUDeploymentGateError("MinerU staged-load receipt is not PASS")
    execution_id = receipt.get("execution_id")
    try:
        parsed_execution_id = uuid.UUID(str(execution_id))
    except (ValueError, AttributeError) as exc:
        raise MinerUDeploymentGateError(
            "MinerU staged-load execution identity is invalid"
        ) from exc
    if str(parsed_execution_id) != execution_id:
        raise MinerUDeploymentGateError(
            "MinerU staged-load execution identity is not canonical"
        )
    if (
        receipt.get("database_access") != "none"
        or receipt.get("queue_access") != "none"
        or receipt.get("identity") != expected_identity
        or receipt.get("topology") != expected_topology
        or receipt.get("fixed_stage_client_document_concurrency")
        != list(_STAGED_DOCUMENT_CONCURRENCIES)
        or receipt.get("orchestrator_task_concurrency")
        != MINERU_API_MAX_CONCURRENT_REQUESTS
        or receipt.get("orchestrator_inference_concurrency")
        != MINERU_API_INFERENCE_MAX_CONCURRENCY
        or receipt.get("effective_inference_request_upper_bound")
        != _EFFECTIVE_INFERENCE_REQUEST_UPPER_BOUND
    ):
        raise MinerUDeploymentGateError(
            "MinerU staged-load identity or endpoint drifted"
        )

    input_evidence = receipt.get("input")
    if (
        not isinstance(input_evidence, dict)
        or input_evidence.get("profile") != _STAGED_LOAD_INPUT_PROFILE
        or not isinstance(input_evidence.get("logical_name"), str)
        or not input_evidence["logical_name"]
        or not _is_prefixed_sha256(input_evidence.get("sha256"))
        or input_evidence.get("sha256") != expected_input_sha256
        or isinstance(input_evidence.get("bytes"), bool)
        or not isinstance(input_evidence.get("bytes"), int)
        or input_evidence["bytes"] < 1
        or input_evidence.get("minimum_required_pages") != 7
    ):
        raise MinerUDeploymentGateError("MinerU staged-load input is invalid")
    input_sha256 = input_evidence["sha256"]
    canonical_input: dict[str, object] = {
        "profile": input_evidence["profile"],
        "logical_name": input_evidence["logical_name"],
        "sha256": input_evidence["sha256"],
        "bytes": input_evidence["bytes"],
        "minimum_required_pages": input_evidence["minimum_required_pages"],
    }
    if expected_input is not None and canonical_input != expected_input:
        raise MinerUDeploymentGateError(
            "MinerU staged-load confirmation input drifted"
        )

    stages = receipt.get("stages")
    if not isinstance(stages, list) or len(stages) != len(
        _STAGED_DOCUMENT_CONCURRENCIES
    ):
        raise MinerUDeploymentGateError("MinerU staged-load stages are incomplete")
    staged_preemptions: float | None = None
    previous_orchestrator_terminal: dict[str, int | str] | None = None
    total_stage_elapsed = 0.0
    for stage, concurrency in zip(
        stages,
        _STAGED_DOCUMENT_CONCURRENCIES,
        strict=True,
    ):
        stage_elapsed = (
            _nonnegative_finite_value(stage.get("elapsed_seconds"))
            if isinstance(stage, dict)
            else None
        )
        if (
            not isinstance(stage, dict)
            or stage.get("status") != "pass"
            or stage.get("failure") is not None
            or stage.get("client_document_concurrency") != concurrency
            or stage.get("orchestrator_task_concurrency")
            != MINERU_API_MAX_CONCURRENT_REQUESTS
            or stage.get("orchestrator_inference_concurrency")
            != MINERU_API_INFERENCE_MAX_CONCURRENCY
            or stage.get("effective_inference_request_upper_bound")
            != _EFFECTIVE_INFERENCE_REQUEST_UPPER_BOUND
            or not _cleanup_is_proved(stage.get("cleanup"))
            or stage_elapsed is None
            or stage_elapsed <= 0
        ):
            raise MinerUDeploymentGateError("MinerU staged-load stage drifted")
        total_stage_elapsed += stage_elapsed
        documents = stage.get("documents")
        if not isinstance(documents, list) or len(documents) != concurrency:
            raise MinerUDeploymentGateError(
                "MinerU staged-load documents are incomplete"
            )
        for copy_index, document in enumerate(documents, start=1):
            if (
                not isinstance(document, dict)
                or document.get("status") != "pass"
                or document.get("copy_index") != copy_index
                or document.get("input_sha256") != input_sha256
                or isinstance(document.get("page_count"), bool)
                or not isinstance(document.get("page_count"), int)
                or document["page_count"] < 7
                or not _is_prefixed_sha256(
                    document.get("provider_bundle_sha256")
                )
            ):
                raise MinerUDeploymentGateError(
                    "MinerU staged-load document evidence is invalid"
                )
        metrics = stage.get("metrics")
        if (
            not isinstance(metrics, dict)
            or isinstance(metrics.get("sample_count"), bool)
            or not isinstance(metrics.get("sample_count"), int)
            or metrics["sample_count"] < 1
            or not isinstance(metrics.get("baseline"), dict)
            or not isinstance(metrics.get("range"), dict)
            or not isinstance(metrics.get("percentiles"), dict)
            or not _metrics_payload_is_proved(
                metrics,
                stage_elapsed_seconds=stage_elapsed,
            )
        ):
            raise MinerUDeploymentGateError(
                "MinerU staged-load metrics were not proved"
            )
        current_preemptions = _nonnegative_finite_value(
            metrics["baseline"].get("preemptions")
        )
        assert current_preemptions is not None
        if staged_preemptions is None:
            staged_preemptions = current_preemptions
        elif current_preemptions != staged_preemptions:
            raise MinerUDeploymentGateError(
                "MinerU staged-load preemption baseline changed between stages"
            )
        orchestrator_terminal = _verify_staged_orchestrator(
            stage.get("orchestrator"),
            concurrency=concurrency,
            stage_elapsed_seconds=stage_elapsed,
        )
        if (
            previous_orchestrator_terminal is not None
            and stage["orchestrator"]["baseline"]
            != previous_orchestrator_terminal
        ):
            raise MinerUDeploymentGateError(
                "MinerU staged-load API counters are discontinuous between stages"
            )
        if (
            previous_orchestrator_terminal is None
            and expected_orchestrator_baseline is not None
            and stage["orchestrator"]["baseline"]
            != expected_orchestrator_baseline
        ):
            raise MinerUDeploymentGateError(
                "MinerU staged-load API counters are discontinuous between runs"
            )
        previous_orchestrator_terminal = orchestrator_terminal

    if not _cleanup_is_proved(receipt.get("cleanup")):
        raise MinerUDeploymentGateError("MinerU staged-load cleanup was not proved")
    started_at = _required_aware_timestamp(
        receipt.get("started_at_utc"),
        label="MinerU staged-load start",
    )
    finished_at = _required_aware_timestamp(
        receipt.get("finished_at_utc"),
        label="MinerU staged-load finish",
    )
    elapsed_seconds = _nonnegative_finite_value(receipt.get("elapsed_seconds"))
    if elapsed_seconds is None or elapsed_seconds <= 0:
        raise MinerUDeploymentGateError(
            "MinerU staged-load elapsed time is invalid"
        )
    current_utc = current.astimezone(UTC)
    age = (current_utc - finished_at).total_seconds()
    if (
        not smoke_finished_at < started_at < finished_at <= current_utc
        or not _elapsed_matches_timeline(
            elapsed_seconds,
            started_at=started_at,
            finished_at=finished_at,
        )
        or elapsed_seconds < total_stage_elapsed
    ):
        raise MinerUDeploymentGateError(
            "MinerU smoke/staged-load timeline is invalid"
        )
    if age < 0 or age > max_age_seconds:
        raise MinerUDeploymentGateError(
            "MinerU staged-load receipt is stale"
        )
    assert previous_orchestrator_terminal is not None
    return canonical_input, str(execution_id), previous_orchestrator_terminal


def _verify_smoke_orchestrator(
    value: object,
    *,
    task_retention_seconds: int,
    cleanup_interval_seconds: int,
) -> None:
    if not isinstance(value, dict):
        raise MinerUDeploymentGateError("MinerU smoke API evidence is invalid")
    before = value.get("before")
    after = value.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise MinerUDeploymentGateError("MinerU smoke API samples are invalid")
    required = {
        "status",
        "version",
        "protocol_version",
        "queued_tasks",
        "processing_tasks",
        "completed_tasks",
        "failed_tasks",
        "max_concurrent_requests",
        "processing_window_size",
        "task_retention_seconds",
        "task_cleanup_interval_seconds",
    }
    if set(before) != required or set(after) != required:
        raise MinerUDeploymentGateError("MinerU smoke API health fields drifted")
    for sample in (before, after):
        for field in required - {"status", "version"}:
            item = sample.get(field)
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise MinerUDeploymentGateError(
                    f"MinerU smoke API health {field} is invalid"
                )
        if (
            sample.get("status") != "healthy"
            or sample.get("version") != "3.4.4"
            or sample.get("protocol_version") != 2
            or sample.get("max_concurrent_requests") != 3
            or sample.get("processing_window_size") != 16
            or sample.get("task_retention_seconds") != task_retention_seconds
            or sample.get("task_cleanup_interval_seconds")
            != cleanup_interval_seconds
            or sample["processing_tasks"] > sample["max_concurrent_requests"]
            or sample["queued_tasks"] + sample["processing_tasks"]
            > sample["processing_window_size"]
        ):
            raise MinerUDeploymentGateError("MinerU smoke API identity drifted")
    if (
        before.get("queued_tasks") != 0
        or before.get("processing_tasks") != 0
        or after.get("queued_tasks") != 0
        or after.get("processing_tasks") != 0
        or value.get("completed_delta") != 1
        or value.get("failed_delta") != 0
        or value.get("terminal_active_tasks") != 0
        or value.get("stop_semantics") != "drain-not-cancel.v1"
        or after.get("completed_tasks") != before.get("completed_tasks", 0) + 1
        or after.get("failed_tasks") != before.get("failed_tasks")
    ):
        raise MinerUDeploymentGateError("MinerU smoke API task accounting drifted")


def _verify_staged_orchestrator(
    value: object,
    *,
    concurrency: int,
    stage_elapsed_seconds: float,
) -> dict[str, int | str]:
    if not isinstance(value, dict):
        raise MinerUDeploymentGateError(
            "MinerU staged-load API evidence is invalid"
        )
    baseline = _strict_health_payload(value.get("baseline"))
    terminal = _strict_health_payload(value.get("terminal"))
    samples = value.get("samples")
    if not isinstance(samples, list) or not samples:
        raise MinerUDeploymentGateError(
            "MinerU staged-load API samples are incomplete"
        )
    normalized_samples: list[dict[str, int | float]] = []
    previous_observed_seconds = -1.0
    for sample in samples:
        if not isinstance(sample, dict) or set(sample) != {
            "observed_seconds",
            "queued_tasks",
            "processing_tasks",
            "completed_tasks",
            "failed_tasks",
        }:
            raise MinerUDeploymentGateError(
                "MinerU staged-load API sample fields drifted"
            )
        observed_seconds = _nonnegative_finite_value(
            sample.get("observed_seconds")
        )
        if (
            observed_seconds is None
            or observed_seconds < previous_observed_seconds
            or observed_seconds > stage_elapsed_seconds
        ):
            raise MinerUDeploymentGateError(
                "MinerU staged-load API sample timing is invalid"
            )
        previous_observed_seconds = observed_seconds
        normalized: dict[str, int | float] = {
            "observed_seconds": observed_seconds
        }
        for field in (
            "queued_tasks",
            "processing_tasks",
            "completed_tasks",
            "failed_tasks",
        ):
            item = sample.get(field)
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise MinerUDeploymentGateError(
                    f"MinerU staged-load API sample {field} is invalid"
                )
            normalized[field] = item
        normalized_samples.append(normalized)

    completed_values = [
        int(baseline["completed_tasks"]),
        *(int(sample["completed_tasks"]) for sample in normalized_samples),
        int(terminal["completed_tasks"]),
    ]
    failed_values = [
        int(baseline["failed_tasks"]),
        *(int(sample["failed_tasks"]) for sample in normalized_samples),
        int(terminal["failed_tasks"]),
    ]
    processing_values = [
        int(baseline["processing_tasks"]),
        *(int(sample["processing_tasks"]) for sample in normalized_samples),
        int(terminal["processing_tasks"]),
    ]
    queued_values = [
        int(baseline["queued_tasks"]),
        *(int(sample["queued_tasks"]) for sample in normalized_samples),
        int(terminal["queued_tasks"]),
    ]
    expected_range = {
        "queued_tasks": {"min": min(queued_values), "max": max(queued_values)},
        "processing_tasks": {
            "min": min(processing_values),
            "max": max(processing_values),
        },
        "completed_tasks": {
            "min": min(completed_values),
            "max": max(completed_values),
        },
        "failed_tasks": {"min": min(failed_values), "max": max(failed_values)},
    }
    preflight_drain = _nonnegative_finite_value(
        value.get("preflight_drain_seconds")
    )
    terminal_drain = _nonnegative_finite_value(
        value.get("terminal_drain_seconds")
    )
    if (
        baseline["queued_tasks"] != 0
        or baseline["processing_tasks"] != 0
        or terminal["queued_tasks"] != 0
        or terminal["processing_tasks"] != 0
        or value.get("sample_count") != len(normalized_samples)
        or value.get("completed_delta") != concurrency
        or value.get("failed_delta") != 0
        or value.get("terminal_active_tasks") != 0
        or value.get("stop_semantics") != "drain-not-cancel.v1"
        or int(terminal["completed_tasks"])
        - int(baseline["completed_tasks"])
        != concurrency
        or terminal["failed_tasks"] != baseline["failed_tasks"]
        or completed_values != sorted(completed_values)
        or failed_values != sorted(failed_values)
        or max(processing_values) > MINERU_API_MAX_CONCURRENT_REQUESTS
        or max(
            queued + processing
            for queued, processing in zip(
                queued_values,
                processing_values,
                strict=True,
            )
        )
        > MINERU_PROCESSING_WINDOW_SIZE
        or max(processing_values) == 0
        or (concurrency in (8, 16) and max(queued_values) == 0)
        or value.get("range") != expected_range
        or preflight_drain is None
        or terminal_drain is None
    ):
        raise MinerUDeploymentGateError(
            "MinerU staged-load API accounting was not proved"
        )
    return terminal


def _strict_health_payload(value: object) -> dict[str, int | str]:
    required = {
        "status",
        "version",
        "protocol_version",
        "queued_tasks",
        "processing_tasks",
        "completed_tasks",
        "failed_tasks",
        "max_concurrent_requests",
        "processing_window_size",
        "task_retention_seconds",
        "task_cleanup_interval_seconds",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise MinerUDeploymentGateError(
            "MinerU staged-load API health fields drifted"
        )
    for field in required - {"status", "version"}:
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise MinerUDeploymentGateError(
                f"MinerU staged-load API health {field} is invalid"
            )
    if (
        value.get("status") != "healthy"
        or value.get("version") != "3.4.4"
        or value.get("protocol_version") != 2
        or value.get("max_concurrent_requests")
        != MINERU_API_MAX_CONCURRENT_REQUESTS
        or value.get("processing_window_size") != MINERU_PROCESSING_WINDOW_SIZE
        or value.get("task_retention_seconds") != 600
        or value.get("task_cleanup_interval_seconds") != 30
        or int(value["processing_tasks"])
        > MINERU_API_MAX_CONCURRENT_REQUESTS
        or int(value["queued_tasks"]) + int(value["processing_tasks"])
        > MINERU_PROCESSING_WINDOW_SIZE
    ):
        raise MinerUDeploymentGateError(
            "MinerU staged-load API identity drifted"
        )
    return value


def _endpoint_sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.rstrip("/").encode("utf-8")).hexdigest()


def _elapsed_matches_timeline(
    elapsed_seconds: float,
    *,
    started_at: datetime,
    finished_at: datetime,
) -> bool:
    observed = (finished_at - started_at).total_seconds()
    tolerance = max(1.0, observed * 0.05)
    return (
        elapsed_seconds > 0
        and observed > 0
        and abs(observed - elapsed_seconds) <= tolerance
    )


def _cleanup_is_proved(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("external_api_temp_dirs_after") == 0
        and value.get("api_temp_cleanup_errors") == []
        and value.get("external_mineru_processes_after") == 0
        and value.get("temporary_tree_removed") is True
        and value.get("observation_error") is None
    )


def _metrics_payload_is_proved(
    metrics: dict[str, Any],
    *,
    stage_elapsed_seconds: float,
) -> bool:
    baseline = metrics.get("baseline")
    ranges = metrics.get("range")
    percentiles = metrics.get("percentiles")
    sampling_failures = metrics.get("sampling_failures")
    assert isinstance(baseline, dict)
    assert isinstance(ranges, dict)
    assert isinstance(percentiles, dict)
    terminal_sample = _nonnegative_finite_value(
        metrics.get("terminal_sample_observed_seconds")
    )
    if (
        terminal_sample is None
        or terminal_sample > stage_elapsed_seconds
        or not isinstance(sampling_failures, list)
        or len(sampling_failures) > 1
        or any(
            not isinstance(item, dict)
            or (
                observed_seconds := _nonnegative_finite_value(
                    item.get("observed_seconds")
                )
            )
            is None
            or observed_seconds >= terminal_sample
            or (
                duration_seconds := _nonnegative_finite_value(
                    item.get("duration_seconds")
                )
            )
            is None
            or duration_seconds > 10.0
            or not isinstance(item.get("failure"), str)
            or not item["failure"].startswith(
                "MetricsTransportUnavailableError:"
            )
            for item in sampling_failures
        )
    ):
        return False
    normalized: dict[str, tuple[float, float, float]] = {}
    for name in ("running", "waiting", "preemptions", "kv_cache"):
        baseline_value = _nonnegative_finite_value(baseline.get(name))
        metric_range = ranges.get(name)
        minimum = (
            _nonnegative_finite_value(metric_range.get("min"))
            if isinstance(metric_range, dict)
            else None
        )
        maximum = (
            _nonnegative_finite_value(metric_range.get("max"))
            if isinstance(metric_range, dict)
            else None
        )
        if (
            baseline_value is None
            or minimum is None
            or maximum is None
            or minimum > maximum
            or not minimum <= baseline_value <= maximum
        ):
            return False
        if name == "preemptions" and minimum != maximum:
            return False
        normalized[name] = (baseline_value, minimum, maximum)
    for name in ("running", "waiting", "kv_cache"):
        p95 = _nonnegative_finite_value(percentiles.get(f"{name}_p95"))
        _, minimum, maximum = normalized[name]
        if p95 is None or not minimum <= p95 <= maximum:
            return False
    running_baseline, _, running_maximum = normalized["running"]
    waiting_baseline, _, _ = normalized["waiting"]
    kv_baseline, _, kv_maximum = normalized["kv_cache"]
    return bool(
        running_baseline == 0
        and waiting_baseline == 0
        and (
            running_maximum > running_baseline
            or kv_maximum > kv_baseline
        )
    )


def _nonnegative_finite_value(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) and normalized >= 0 else None


def _required_aware_timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise MinerUDeploymentGateError(f"{label} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MinerUDeploymentGateError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise MinerUDeploymentGateError(f"{label} timestamp is naive")
    return parsed.astimezone(UTC)


def require_mineru_deployment_gate(
    settings: Settings,
    *,
    parse_enabled: bool | None = None,
) -> None:
    """Compatibility entry point used by one-shot and resident workers."""

    verify_mineru_deployment_gate(settings, parse_enabled=parse_enabled)


def _verify_provider_evidence(
    provider: object,
    *,
    runtime_identity: str,
) -> None:
    if not isinstance(provider, dict):
        raise MinerUDeploymentGateError("MinerU provider evidence is invalid")
    for field, minimum in (
        ("page_count", 1),
        ("block_count", 0),
        ("artifact_count", 0),
    ):
        value = provider.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise MinerUDeploymentGateError(f"MinerU provider {field} is invalid")
    if not _is_prefixed_sha256(provider.get("provider_bundle_sha256")):
        raise MinerUDeploymentGateError("MinerU provider bundle identity is invalid")
    try:
        target = ParserTargetIdentity.from_payload(provider.get("target_identity"))
    except ParserTargetIdentityError as exc:
        raise MinerUDeploymentGateError(
            f"MinerU provider target identity is invalid: {exc}"
        ) from exc
    if (
        target.name != "MinerU"
        or target.package_version != "3.4.4"
        or target.backend != "hybrid-http-client"
        or target.method != "auto"
        or target.language != "ch"
        or not target.formula
        or not target.table
        or target.effort != "medium"
        or target.image_analysis
        or not target.full_pdf
        or target.start_page is not None
        or target.end_page is not None
        or target.runtime_bundle_identity_sha256 != runtime_identity
    ):
        raise MinerUDeploymentGateError("MinerU provider target drifted")


def _is_prefixed_sha256(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(
        character in "0123456789abcdef" for character in digest
    )


__all__ = [
    "MinerUDeploymentChecker",
    "MinerUDeploymentGateError",
    "MinerUDeploymentUnavailableError",
    "VerifiedMinerUDeployment",
    "require_mineru_deployment_gate",
    "verify_mineru_deployment_gate",
]
