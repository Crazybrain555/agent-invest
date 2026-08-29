"""Fail-closed MinerU deployment evidence and resident admission checks."""

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

from disclosure_anchor.adapters.runtime.mineru_canary import (
    MinerUCanaryError,
    MinerUCanaryUnavailableError,
    canary_cache_is_fresh,
    canary_request_sha256,
    model_id_sha256,
    probe_mineru_served_model,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_API_INFERENCE_MAX_CONCURRENCY,
    MINERU_API_MAX_SUPPORTED_TASK_SLOTS,
    MINERU_PROCESSING_WINDOW_SIZE,
    MINERU_SMOKE_INPUT_NAME,
    MINERU_SMOKE_INPUT_SHA256,
    canonical_payload_sha256,
    client_bundle_identity,
    verify_runtime_manifest_payload,
    writer_code_digest,
)
from disclosure_anchor.adapters.runtime.mineru_orchestrator import (
    MinerUOrchestratorError,
    MinerUOrchestratorUnavailableError,
    fetch_mineru_orchestrator_health,
    mineru_orchestrator_incident_state,
    parse_mineru_orchestrator_health_payload,
)
from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
    ParserTargetIdentityError,
)
from disclosure_anchor.settings import Settings


_SMOKE_SCHEMA = "mineru_smoke_receipt.v5"
_VALIDATION_SCHEMA = "mineru_heldout_validation_receipt.v1"
_VALIDATION_POLICY = "operator-held-out-complete-pdf.v1"
_TASK_REGISTRY_SEMANTICS = "retained-terminal-gauges.v1"
_DEPLOYMENT_INPUT_PROFILE = "deployment_frozen_v1"
_HELDOUT_INPUT_PROFILE = "diagnostic_custom"
_CANARY_ATTEMPTS = 3
_MIN_HELDOUT_DOCUMENTS = 2
_MAX_HELDOUT_DOCUMENTS = 8
_MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
_MAX_VALIDATION_EVIDENCE_BYTES = 16 * 1024 * 1024
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
    task_slots: int

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
                expected_task_slots=self.task_slots,
                expected_task_retention_seconds=self.task_retention_seconds,
                expected_cleanup_interval_seconds=self.task_cleanup_interval_seconds,
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
    """Thread-safe static proof plus rate-limited live admission checks."""

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
        self._last_probe_success: float | None = None
        self._initial_idle_proved = False
        self._incident_generation = mineru_orchestrator_incident_state().generation
        self._probe_lock = threading.Lock()

    def assert_admission(self) -> None:
        evidence = self._evidence
        if evidence is None:
            return
        state = mineru_orchestrator_incident_state()
        if state.drains_in_progress:
            raise MinerUDeploymentUnavailableError(
                "MinerU API incident drain is still in progress"
            )
        generation = state.generation
        changed = generation != self._incident_generation
        observed = self._monotonic_clock()
        if (
            not changed
            and self._last_probe_success is not None
            and observed - self._last_probe_success < self._probe_interval_seconds
        ):
            return
        with self._probe_lock:
            state = mineru_orchestrator_incident_state()
            if state.drains_in_progress:
                raise MinerUDeploymentUnavailableError(
                    "MinerU API incident drain is still in progress"
                )
            generation = state.generation
            changed = generation != self._incident_generation
            observed = self._monotonic_clock()
            if (
                not changed
                and self._last_probe_success is not None
                and observed - self._last_probe_success
                < self._probe_interval_seconds
            ):
                return
            evidence.probe_orchestrator(
                require_idle=(not self._initial_idle_proved or changed)
            )
            evidence.probe_live_model()
            confirmed = mineru_orchestrator_incident_state()
            if confirmed.drains_in_progress or confirmed.generation != generation:
                raise MinerUDeploymentUnavailableError(
                    "MinerU API incident changed during live admission proof"
                )
            self._incident_generation = generation
            self._initial_idle_proved = True
            self._last_probe_success = self._monotonic_clock()


def _load_evidence(
    path: Path,
    *,
    label: str,
    max_bytes: int = _MAX_EVIDENCE_BYTES,
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
        if metadata.st_size > max_bytes:
            raise MinerUDeploymentGateError(f"{label} exceeds the size limit")
        with os.fdopen(descriptor, "rb") as evidence_file:
            descriptor = None
            encoded = evidence_file.read(max_bytes + 1)
            final_metadata = os.fstat(evidence_file.fileno())
        if len(encoded) > max_bytes:
            raise MinerUDeploymentGateError(f"{label} exceeds the size limit")
        initial = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_nlink,
            metadata.st_size,
        )
        final = (
            final_metadata.st_dev,
            final_metadata.st_ino,
            final_metadata.st_mode,
            final_metadata.st_uid,
            final_metadata.st_nlink,
            final_metadata.st_size,
        )
        if final != initial:
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
    """Prove exact runtime, fixed smoke, held-out PDFs, and live boundaries."""

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
        settings.worker_parse_concurrency > MINERU_PROCESSING_WINDOW_SIZE
        or settings.worker_mineru_client_outstanding_window
        > settings.disclosure_mineru_api_task_slots
        or settings.disclosure_mineru_api_task_slots
        > MINERU_API_MAX_SUPPORTED_TASK_SLOTS
        or settings.disclosure_mineru_api_inference_concurrency
        != MINERU_API_INFERENCE_MAX_CONCURRENCY
        or settings.worker_gpu_request_budget
        != settings.mineru_effective_inference_request_upper_bound
        or settings.worker_gpu_max_sequences != 128
    ):
        raise MinerUDeploymentGateError(
            "MinerU worker/API fan-out exceeds the attested service envelope"
        )
    api_url = settings.disclosure_mineru_api_url
    observability_url = settings.disclosure_mineru_observability_url
    inference_upstream_url = settings.disclosure_mineru_inference_upstream_url
    runtime_identity = settings.disclosure_mineru_runtime_bundle_identity_sha256
    mineru_bin = settings.disclosure_mineru_bin
    smoke_path = settings.disclosure_mineru_smoke_receipt
    cache_path = settings.disclosure_mineru_canary_cache
    validation_path = settings.disclosure_mineru_validation_receipt
    if (
        not api_url
        or not observability_url
        or not inference_upstream_url
        or not runtime_identity
        or mineru_bin is None
        or smoke_path is None
        or cache_path is None
        or validation_path is None
    ):
        raise MinerUDeploymentGateError(
            "MinerU exact topology, executable, runtime identity, smoke/cache and "
            "held-out validation receipt are required"
        )
    if len(
        {
            smoke_path.resolve(strict=False),
            cache_path.resolve(strict=False),
            validation_path.resolve(strict=False),
        }
    ) != 3:
        raise MinerUDeploymentGateError("MinerU evidence paths must differ")
    smoke, smoke_file = _load_evidence(smoke_path, label="MinerU smoke receipt")
    cache, cache_file = _load_evidence(cache_path, label="MinerU canary cache")
    validation, validation_file = _load_evidence(
        validation_path,
        label="MinerU held-out validation receipt",
        max_bytes=_MAX_VALIDATION_EVIDENCE_BYTES,
    )
    if len({smoke_file, cache_file, validation_file}) != 3:
        raise MinerUDeploymentGateError(
            "MinerU evidence files must not be hard-linked aliases"
        )
    current = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        local_client = client_bundle_identity(mineru_bin)
        local_code_digest = writer_code_digest()
        manifest = verify_runtime_manifest_payload(
            {
                "identity_sha256": runtime_identity,
                "manifest": smoke.get("runtime_manifest"),
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
    if manifest.max_concurrent_requests != settings.disclosure_mineru_api_task_slots:
        raise MinerUDeploymentGateError(
            "runtime manifest task slots drifted from worker configuration"
        )
    expected_identity: dict[str, object] = {
        "local_client_identity_sha256": local_client.package_set_sha256,
        "local_content_package_versions": dict(local_client.content_package_versions),
        "local_processing_window_size": MINERU_PROCESSING_WINDOW_SIZE,
        "local_writer_code_sha256": local_code_digest,
        "runtime_manifest_identity_sha256": runtime_identity,
        "orchestrator_runtime_identity_sha256": (
            manifest.orchestrator_identity_sha256
        ),
        "provider_runtime_identity_sha256": manifest.provider_identity_sha256,
        "served_model_id": manifest.served_model_id,
        "orchestrator_task_slots": manifest.max_concurrent_requests,
    }
    expected_topology = {
        "api_endpoint_sha256": _endpoint_sha256(api_url),
        "observability_endpoint_sha256": _endpoint_sha256(observability_url),
        "inference_upstream_sha256": _endpoint_sha256(inference_upstream_url),
    }
    manifest_topology = manifest.manifest["topology"]
    if any(
        manifest_topology.get(field) != value
        for field, value in expected_topology.items()
    ):
        raise MinerUDeploymentGateError("MinerU endpoint topology drifted")
    contract: dict[str, Any] = {
        "expected_identity": expected_identity,
        "expected_topology": expected_topology,
        "expected_runtime_manifest": manifest.manifest,
        "runtime_identity": runtime_identity,
        "task_slots": settings.disclosure_mineru_api_task_slots,
        "task_retention_seconds": (
            settings.disclosure_mineru_api_task_retention_seconds
        ),
        "cleanup_interval_seconds": (
            settings.disclosure_mineru_api_cleanup_interval_seconds
        ),
        "observability_url": observability_url,
        "max_age_seconds": settings.disclosure_mineru_canary_max_age_seconds,
        "current": current,
    }
    _verify_smoke_receipt(
        smoke,
        label="MinerU deployment smoke",
        expected_profile=_DEPLOYMENT_INPUT_PROFILE,
        expected_cache=cache,
        **contract,
    )
    _verify_heldout_validation(validation, **contract)
    passed_at = _required_aware_timestamp(
        cache.get("passed_at_utc"), label="MinerU canary"
    )
    evidence = VerifiedMinerUDeployment(
        api_url=api_url,
        observability_url=observability_url,
        inference_upstream_url=inference_upstream_url,
        runtime_identity_sha256=runtime_identity,
        served_model_id=manifest.served_model_id,
        canary_passed_at_utc=passed_at,
        canary_max_age_seconds=settings.disclosure_mineru_canary_max_age_seconds,
        task_retention_seconds=settings.disclosure_mineru_api_task_retention_seconds,
        task_cleanup_interval_seconds=(
            settings.disclosure_mineru_api_cleanup_interval_seconds
        ),
        task_slots=settings.disclosure_mineru_api_task_slots,
    )
    evidence.assert_fresh(now=current)
    return evidence


def _verify_heldout_validation(
    value: object,
    **smoke_contract: Any,
) -> None:
    required = {
        "schema",
        "status",
        "created_at_utc",
        "policy",
        "database_access",
        "queue_access",
        "document_count",
        "documents",
        "epoch_before",
        "epoch_after",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise MinerUDeploymentGateError("MinerU held-out validation fields drifted")
    documents = value.get("documents")
    document_count = value.get("document_count")
    if (
        value.get("schema") != _VALIDATION_SCHEMA
        or value.get("status") != "pass"
        or value.get("policy") != _VALIDATION_POLICY
        or value.get("database_access") != "none"
        or value.get("queue_access") != "none"
        or isinstance(document_count, bool)
        or not isinstance(document_count, int)
        or not _MIN_HELDOUT_DOCUMENTS
        <= document_count
        <= _MAX_HELDOUT_DOCUMENTS
        or not isinstance(documents, list)
        or len(documents) != document_count
    ):
        raise MinerUDeploymentGateError("MinerU held-out validation is not PASS")
    current = smoke_contract["current"]
    max_age_seconds = smoke_contract["max_age_seconds"]
    created_at = _required_aware_timestamp(
        value.get("created_at_utc"), label="MinerU held-out validation creation"
    )
    if not (
        current.timestamp() - max_age_seconds
        <= created_at.timestamp()
        <= current.timestamp()
    ):
        raise MinerUDeploymentGateError("MinerU held-out validation is stale")
    source_identities: set[str] = set()
    earliest_start: datetime | None = None
    latest_finish: datetime | None = None
    for index, item in enumerate(documents):
        if not isinstance(item, dict) or set(item) != {"receipt_sha256", "receipt"}:
            raise MinerUDeploymentGateError(
                f"MinerU held-out document {index} wrapper drifted"
            )
        receipt = item.get("receipt")
        if (
            not isinstance(receipt, dict)
            or item.get("receipt_sha256") != canonical_payload_sha256(receipt)
        ):
            raise MinerUDeploymentGateError(
                f"MinerU held-out document {index} hash drifted"
            )
        source_identity, started_at, finished_at = _verify_smoke_receipt(
            receipt,
            label=f"MinerU held-out document {index}",
            expected_profile=_HELDOUT_INPUT_PROFILE,
            require_multi_page=True,
            expected_cache=None,
            **smoke_contract,
        )
        if source_identity in source_identities:
            raise MinerUDeploymentGateError(
                "MinerU held-out validation repeats a source PDF"
            )
        source_identities.add(source_identity)
        earliest_start = (
            started_at if earliest_start is None else min(earliest_start, started_at)
        )
        latest_finish = (
            finished_at if latest_finish is None else max(latest_finish, finished_at)
        )
    before_identity, before_time = _verify_epoch_wrapper(
        value.get("epoch_before"),
        runtime_identity=smoke_contract["runtime_identity"],
        label="before",
    )
    after_identity, after_time = _verify_epoch_wrapper(
        value.get("epoch_after"),
        runtime_identity=smoke_contract["runtime_identity"],
        label="after",
    )
    if (
        earliest_start is None
        or latest_finish is None
        or created_at < latest_finish
        or before_identity != after_identity
        or not before_time <= earliest_start < latest_finish <= after_time <= created_at
    ):
        raise MinerUDeploymentGateError(
            "MinerU held-out validation epoch/timeline is invalid"
        )


def _verify_epoch_wrapper(
    value: object,
    *,
    runtime_identity: str,
    label: str,
) -> tuple[str, datetime]:
    if not isinstance(value, dict) or set(value) != {"receipt_sha256", "receipt"}:
        raise MinerUDeploymentGateError(f"MinerU {label} epoch wrapper drifted")
    receipt = value.get("receipt")
    if (
        not isinstance(receipt, dict)
        or value.get("receipt_sha256") != canonical_payload_sha256(receipt)
        or set(receipt)
        != {
            "schema",
            "status",
            "created_at_utc",
            "database_access",
            "queue_access",
            "service_epoch",
            "service_epoch_sha256",
            "safety",
        }
    ):
        raise MinerUDeploymentGateError(f"MinerU {label} epoch receipt drifted")
    epoch = receipt.get("service_epoch")
    safety = receipt.get("safety")
    expected_safety = {
        "restart_count_total": 0,
        "oom_killed_count": 0,
        "unsafe_container_count": 0,
        "cgroup_oom_total": 0,
        "cgroup_oom_kill_total": 0,
    }
    if (
        receipt.get("schema") != "mineru-service-epoch-freeze.v2"
        or receipt.get("status") != "pass"
        or receipt.get("database_access") != "none"
        or receipt.get("queue_access") != "none"
        or not isinstance(epoch, dict)
        or set(epoch)
        != {
            "schema",
            "runtime_manifest_identity_sha256",
            "collector_sha256",
            "windows_node_identity_sha256",
            "container_epoch_sha256",
            "api_container_id",
        }
        or epoch.get("schema") != "mineru-service-epoch.v1"
        or epoch.get("runtime_manifest_identity_sha256") != runtime_identity
        or receipt.get("service_epoch_sha256") != canonical_payload_sha256(epoch)
        or safety != expected_safety
    ):
        raise MinerUDeploymentGateError(f"MinerU {label} epoch is not clean PASS")
    return str(receipt["service_epoch_sha256"]), _required_aware_timestamp(
        receipt.get("created_at_utc"), label=f"MinerU {label} epoch creation"
    )


def _verify_smoke_receipt(
    receipt: object,
    *,
    label: str,
    expected_profile: str,
    expected_identity: dict[str, object],
    expected_topology: dict[str, str],
    expected_runtime_manifest: dict[str, Any],
    runtime_identity: str,
    task_slots: int,
    task_retention_seconds: int,
    cleanup_interval_seconds: int,
    observability_url: str,
    max_age_seconds: int,
    current: datetime,
    expected_cache: dict[str, Any] | None,
    require_multi_page: bool = False,
) -> tuple[str, datetime, datetime]:
    required_receipt_fields = {
        "schema",
        "status",
        "started_at_utc",
        "finished_at_utc",
        "elapsed_seconds",
        "database_access",
        "queue_access",
        "input",
        "identity",
        "topology",
        "orchestrator",
        "runtime_manifest",
        "canary",
        "provider",
        "cleanup",
    }
    if not isinstance(receipt, dict) or set(receipt) != required_receipt_fields:
        raise MinerUDeploymentGateError(f"{label} fields drifted")
    if receipt.get("schema") != _SMOKE_SCHEMA or receipt.get("status") != "pass":
        raise MinerUDeploymentGateError(f"{label} is not v5 PASS")
    canary = receipt.get("canary")
    if not isinstance(canary, dict) or (
        expected_cache is not None and canary != expected_cache
    ):
        raise MinerUDeploymentGateError(f"{label} canary evidence drifted")
    if not canary_cache_is_fresh(
        canary,
        observability_url=observability_url,
        runtime_bundle_identity_sha256=runtime_identity,
        max_age_seconds=max_age_seconds,
        now=current,
    ):
        raise MinerUDeploymentGateError(f"{label} canary is stale or drifted")
    model_id = expected_identity["served_model_id"]
    if (
        canary.get("attempts") != _CANARY_ATTEMPTS
        or canary.get("model_id_sha256") != model_id_sha256(str(model_id))
        or canary.get("request_sha256") != canary_request_sha256(str(model_id))
    ):
        raise MinerUDeploymentGateError(f"{label} canary identity drifted")
    if receipt.get("identity") != expected_identity:
        raise MinerUDeploymentGateError(f"{label} runtime identity drifted")
    if receipt.get("topology") != expected_topology:
        raise MinerUDeploymentGateError(f"{label} topology drifted")
    if receipt.get("runtime_manifest") != expected_runtime_manifest:
        raise MinerUDeploymentGateError(f"{label} crossed a runtime epoch")
    input_evidence = receipt.get("input")
    if not isinstance(input_evidence, dict) or set(input_evidence) != {
        "profile",
        "logical_name",
        "sha256",
        "bytes",
        "page_count",
    }:
        raise MinerUDeploymentGateError(f"{label} input evidence is invalid")
    source_identity = input_evidence.get("sha256")
    source_pages = input_evidence.get("page_count")
    if (
        input_evidence.get("profile") != expected_profile
        or not _is_prefixed_sha256(source_identity)
        or isinstance(input_evidence.get("bytes"), bool)
        or not isinstance(input_evidence.get("bytes"), int)
        or input_evidence["bytes"] < 1
        or isinstance(source_pages, bool)
        or not isinstance(source_pages, int)
        or source_pages < (2 if require_multi_page else 1)
    ):
        raise MinerUDeploymentGateError(f"{label} input evidence drifted")
    if expected_profile == _DEPLOYMENT_INPUT_PROFILE and (
        input_evidence.get("logical_name") != MINERU_SMOKE_INPUT_NAME
        or source_identity != MINERU_SMOKE_INPUT_SHA256
    ):
        raise MinerUDeploymentGateError("MinerU frozen smoke input drifted")
    provider_pages = _verify_provider_evidence(
        receipt.get("provider"), runtime_identity=runtime_identity
    )
    if provider_pages != source_pages:
        raise MinerUDeploymentGateError(f"{label} did not preserve all source pages")
    _verify_smoke_orchestrator(
        receipt.get("orchestrator"),
        task_retention_seconds=task_retention_seconds,
        cleanup_interval_seconds=cleanup_interval_seconds,
        task_slots=task_slots,
    )
    if receipt.get("cleanup") != _EXPECTED_CLEANUP:
        raise MinerUDeploymentGateError(f"{label} cleanup was not proved")
    if (
        receipt.get("database_access") != "none"
        or receipt.get("queue_access") != "none"
    ):
        raise MinerUDeploymentGateError(f"{label} was not DB/queue free")
    started_at = _required_aware_timestamp(
        receipt.get("started_at_utc"), label=f"{label} start"
    )
    finished_at = _required_aware_timestamp(
        receipt.get("finished_at_utc"), label=f"{label} finish"
    )
    elapsed = _nonnegative_finite_value(receipt.get("elapsed_seconds"))
    if (
        elapsed is None
        or not started_at < finished_at <= current
        or (current - finished_at).total_seconds() > max_age_seconds
        or not _elapsed_matches_timeline(
            elapsed, started_at=started_at, finished_at=finished_at
        )
    ):
        raise MinerUDeploymentGateError(f"{label} timeline is invalid")
    return str(source_identity), started_at, finished_at


def _verify_smoke_orchestrator(
    value: object,
    *,
    task_retention_seconds: int,
    cleanup_interval_seconds: int,
    task_slots: int,
) -> None:
    required_fields = {
        "task_registry_semantics",
        "before",
        "after",
        "terminal_active_tasks",
        "stop_semantics",
    }
    if not isinstance(value, dict) or set(value) != required_fields:
        raise MinerUDeploymentGateError("MinerU smoke API evidence is invalid")
    before = value.get("before")
    after = value.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise MinerUDeploymentGateError("MinerU smoke API samples are invalid")
    try:
        health_samples = tuple(
            parse_mineru_orchestrator_health_payload(
                sample,
                expected_task_slots=task_slots,
                expected_task_retention_seconds=task_retention_seconds,
                expected_cleanup_interval_seconds=cleanup_interval_seconds,
            )
            for sample in (before, after)
        )
    except MinerUOrchestratorError as exc:
        raise MinerUDeploymentGateError(
            f"MinerU smoke API health drifted: {exc}"
        ) from exc
    if any(
        sample.max_pending_tasks_requested != task_slots
        or sample.max_pending_tasks_effective != task_slots
        for sample in health_samples
    ):
        raise MinerUDeploymentGateError("MinerU smoke API pending capacity drifted")
    if (
        before.get("queued_tasks") != 0
        or before.get("processing_tasks") != 0
        or after.get("queued_tasks") != 0
        or after.get("processing_tasks") != 0
        or value.get("task_registry_semantics") != _TASK_REGISTRY_SEMANTICS
        or value.get("terminal_active_tasks") != 0
        or value.get("stop_semantics") != "drain-not-cancel.v1"
    ):
        raise MinerUDeploymentGateError("MinerU smoke API evidence was not proved")


def _verify_provider_evidence(provider: object, *, runtime_identity: str) -> int:
    if not isinstance(provider, dict) or set(provider) != {
        "target_identity",
        "provider_bundle_sha256",
        "page_count",
        "block_count",
        "artifact_count",
    }:
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
    return int(provider["page_count"])


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


def _is_prefixed_sha256(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(
        character in "0123456789abcdef" for character in digest
    )


def require_mineru_deployment_gate(
    settings: Settings,
    *,
    parse_enabled: bool | None = None,
) -> None:
    """Entry point used by one-shot and resident workers."""

    verify_mineru_deployment_gate(settings, parse_enabled=parse_enabled)


__all__ = [
    "MinerUDeploymentChecker",
    "MinerUDeploymentGateError",
    "MinerUDeploymentUnavailableError",
    "VerifiedMinerUDeployment",
    "require_mineru_deployment_gate",
    "verify_mineru_deployment_gate",
]
