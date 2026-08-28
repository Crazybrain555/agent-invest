#!/usr/bin/env python3
"""Build-time, exact-source runtime compatibility patch for MinerU 3.4.4.

The patch preserves the explicit, fail-visible glibc ``malloc_trim(0)`` hook
and adds a default-off, bounded two-window Hybrid pipeline with content-free
phase evidence. Every source file must match the deployed 3.4.4 bytes before
any write occurs; legacy remains the image default until commissioning closes.
"""

from __future__ import annotations

import hashlib
from importlib import metadata
import json
from pathlib import Path
import py_compile
from typing import Final


MINERU_VERSION: Final = "3.4.4"
BASE_IMAGE_DIGEST: Final = (
    "sha256:109016f8f7666c3a86b0a6585f5b7003d1dd63c2d318f6ecd7ab1db5aa582458"
)
POLICY: Final = "glibc-malloc-trim-per-window.v1"
CAPACITY_POLICY: Final = "bounded-two-window-capacity-pipeline.v2"
SITE_PACKAGES: Final = Path("/usr/local/lib/python3.12/dist-packages")
MARKER_PATH: Final = Path(
    "/opt/agent-invest/mineru-capacity-v1/compatibility.json"
)
TARGET_PREIMAGE_SHA256: Final = {
    "mineru/backend/vlm/vlm_analyze.py": (
        "0fadf7a94ae702861b4a1fa7f42358c6687cfc63fbe322c004fb1d3248658390"
    ),
    "mineru/backend/hybrid/hybrid_analyze.py": (
        "404ce6552e9d7374b96de798d2d0f7d72927eef9485668e79c82c5002b36adb0"
    ),
    "mineru/utils/model_utils.py": (
        "7662656c5c406ab704065b8a3a6e662b662b0bb877b76b08c7d8a8a7eaf9c109"
    ),
}


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _replace_exact(
    source: str,
    old: str,
    new: str,
    *,
    count: int,
    label: str,
) -> str:
    observed = source.count(old)
    if observed != count:
        raise RuntimeError(
            f"{label} patch anchor count drifted: expected {count}, got {observed}"
        )
    return source.replace(old, new)


def _replace_exact_occurrence(
    source: str,
    old: str,
    new: str,
    *,
    count: int,
    occurrence: int,
    label: str,
) -> str:
    observed = source.count(old)
    if observed != count or not 0 <= occurrence < count:
        raise RuntimeError(
            f"{label} patch anchor count drifted: expected {count}, got {observed}"
        )
    start = -1
    for _ in range(occurrence + 1):
        start = source.index(old, start + 1)
    return source[:start] + new + source[start + len(old) :]


def patch_source(relative_path: str, source: str) -> str:
    """Return the deterministic patched source for one exact MinerU module."""

    if relative_path == "mineru/utils/model_utils.py":
        source = _replace_exact(
            source,
            "import math\nimport os\nimport time\nimport gc\n",
            "import asyncio\n"
            "import ctypes\n"
            "from dataclasses import dataclass\n"
            "from functools import lru_cache\n"
            "import hashlib\n"
            "import json\n"
            "import math\n"
            "import os\n"
            "import stat\n"
            "import sys\n"
            "import threading\n"
            "import time\n"
            "import uuid\n"
            "import gc\n",
            count=1,
            label="model-utils imports",
        )
        helper = '''_PHASE_TRACE_PREFIX = "MINERU_PHASE_TRACE "
_PHASE_TRACE_SCHEMA = "mineru-phase-trace.v2"
_PHASE_TRACE_BACKENDS = frozenset({"hybrid", "vlm"})
_PHASE_TRACE_PIPELINE_MODES = frozenset({"legacy", "depth1"})
_PHASE_TRACE_PHASES = frozenset({
    "document",
    "document_finalize",
    "window_append",
    "window_b_queue_wait",
    "window_credit_wait",
    "window_layout",
    "window_postprocess",
    "window_release",
    "window_render",
    "window_total",
    "window_vlm",
})
_PHASE_TRACE_OUTPUT_LOCK = threading.Lock()
_PHASE_TRACE_PROCESS_EPOCH = uuid.uuid4().hex


def is_phase_trace_enabled() -> bool:
    """Return the default-off, closed-vocabulary phase-trace switch."""
    value = os.getenv("MINERU_PHASE_TRACE")
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError("MINERU_PHASE_TRACE has an invalid value")


class _DisabledPhaseTrace:
    def document_started(self) -> None:
        return None

    def document_completed(self) -> None:
        return None

    def document_failed(self) -> None:
        return None

    def window(self, **_kwargs):
        return None

    def start(self) -> int:
        return 0

    def complete(self, _phase: str, _started_ns: int, **_kwargs) -> None:
        return None


_DISABLED_PHASE_TRACE = _DisabledPhaseTrace()


_CAPACITY_PROFILE_SCHEMA = "mineru-execution-profile.v2"
_CAPACITY_PROFILE_ENV = "MINERU_CAPACITY_PROFILE_JSON"
_CAPACITY_PROFILE_FIELDS = frozenset({
    "inner_inference_concurrency",
    "max_document_pages",
    "max_resident_pages",
    "max_source_pdf_bytes",
    "min_document_pages",
    "pipeline_depth",
    "profile_id",
    "schema",
    "vllm_max_num_seqs",
    "window_size",
})
_CAPACITY_CATALOG_SCHEMA = "mineru-capacity-catalog.v1"
_CAPACITY_CATALOG_PATH_ENV = "MINERU_CAPACITY_CATALOG_PATH"
_CAPACITY_CATALOG_SHA256_ENV = "MINERU_CAPACITY_CATALOG_SHA256"
_CAPACITY_RUNTIME_COMPATIBILITY_SHA256_ENV = (
    "MINERU_CAPACITY_RUNTIME_COMPATIBILITY_SHA256"
)
_CAPACITY_CATALOG_FIELDS = frozenset({
    "commissioning_evaluator_sha256",
    "commissioning_receipt_sha256",
    "profile_id",
    "profile_sha256",
    "runtime_compatibility_sha256",
    "schema",
})
_CAPACITY_MAX_CATALOG_BYTES = 64 * 1024
_CAPACITY_MAX_PAGE_PIXEL_BYTES = 3500 * 3500 * 4


def capacity_mode() -> str:
    value = os.getenv("MINERU_CAPACITY_MODE", "legacy").strip().lower()
    if value not in {"legacy", "candidate", "auto"}:
        raise RuntimeError("MINERU_CAPACITY_MODE has an invalid value")
    return value


def _capacity_integer(value, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RuntimeError(f"capacity profile {label} is invalid")
    return value


def _capacity_profile_id(value) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 64
        or value[0] not in "abcdefghijklmnopqrstuvwxyz0123456789"
        or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
            for char in value
        )
    ):
        raise RuntimeError("capacity profile identity is invalid")
    return value


def _capacity_profile_hash(payload: dict) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _capacity_sha256(value, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in value[7:])
    ):
        raise RuntimeError(f"capacity {label} SHA-256 is invalid")
    return value


@dataclass(frozen=True)
class CapacityExecutionProfile:
    profile_id: str
    profile_sha256: str
    pipeline_mode: str
    pipeline_depth: int
    window_size: int
    max_resident_pages: int
    max_source_pdf_bytes: int
    min_document_pages: int
    max_document_pages: int
    inner_inference_concurrency: int
    vllm_max_num_seqs: int

    @property
    def max_resident_decoded_bytes(self) -> int:
        return self.max_resident_pages * _CAPACITY_MAX_PAGE_PIXEL_BYTES

def _legacy_execution_profile(configured_window_size: int) -> CapacityExecutionProfile:
    configured_window_size = _capacity_integer(
        configured_window_size,
        label="configured_window_size",
        minimum=1,
    )
    payload = {
        "inner_inference_concurrency": 7,
        "max_document_pages": 2147483647,
        "max_resident_pages": configured_window_size,
        "max_source_pdf_bytes": 9223372036854775807,
        "min_document_pages": 0,
        "pipeline_depth": 0,
        "profile_id": f"legacy-w{configured_window_size}-d0",
        "schema": _CAPACITY_PROFILE_SCHEMA,
        "vllm_max_num_seqs": 128,
        "window_size": configured_window_size,
    }
    return CapacityExecutionProfile(
        profile_id=payload["profile_id"],
        profile_sha256=_capacity_profile_hash(payload),
        pipeline_mode="legacy",
        pipeline_depth=0,
        window_size=configured_window_size,
        max_resident_pages=configured_window_size,
        max_source_pdf_bytes=payload["max_source_pdf_bytes"],
        min_document_pages=0,
        max_document_pages=payload["max_document_pages"],
        inner_inference_concurrency=7,
        vllm_max_num_seqs=128,
    )


def legacy_capacity_execution_profile(
    configured_window_size: int,
) -> CapacityExecutionProfile:
    return _legacy_execution_profile(configured_window_size)


@lru_cache(maxsize=16)
def _parse_capacity_execution_profile(
    raw_profile: str,
    configured_window_size: int,
) -> CapacityExecutionProfile:
    try:
        payload = json.loads(raw_profile)
    except json.JSONDecodeError as exc:
        raise RuntimeError("capacity profile JSON is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != _CAPACITY_PROFILE_FIELDS:
        raise RuntimeError("capacity profile fields drifted")
    if payload.get("schema") != _CAPACITY_PROFILE_SCHEMA:
        raise RuntimeError("capacity profile schema drifted")
    profile_id = _capacity_profile_id(payload.get("profile_id"))
    window_size = _capacity_integer(
        payload.get("window_size"),
        label="window_size",
        minimum=1,
    )
    pipeline_depth = _capacity_integer(
        payload.get("pipeline_depth"),
        label="pipeline_depth",
        minimum=1,
    )
    max_resident_pages = _capacity_integer(
        payload.get("max_resident_pages"),
        label="max_resident_pages",
        minimum=1,
    )
    min_document_pages = _capacity_integer(
        payload.get("min_document_pages"),
        label="min_document_pages",
        minimum=2,
    )
    max_document_pages = _capacity_integer(
        payload.get("max_document_pages"),
        label="max_document_pages",
        minimum=min_document_pages,
    )
    max_source_pdf_bytes = _capacity_integer(
        payload.get("max_source_pdf_bytes"),
        label="max_source_pdf_bytes",
        minimum=1,
    )
    inner_inference_concurrency = _capacity_integer(
        payload.get("inner_inference_concurrency"),
        label="inner_inference_concurrency",
        minimum=1,
    )
    vllm_max_num_seqs = _capacity_integer(
        payload.get("vllm_max_num_seqs"),
        label="vllm_max_num_seqs",
        minimum=inner_inference_concurrency,
    )
    configured_window_size = _capacity_integer(
        configured_window_size,
        label="configured_window_size",
        minimum=1,
    )
    if (
        pipeline_depth != 1
        or window_size * (pipeline_depth + 1) > max_resident_pages
        or max_resident_pages > configured_window_size
        or min_document_pages <= window_size
    ):
        raise RuntimeError("capacity profile exceeds the legacy owner envelope")
    return CapacityExecutionProfile(
        profile_id=profile_id,
        profile_sha256=_capacity_profile_hash(payload),
        pipeline_mode="depth1",
        pipeline_depth=pipeline_depth,
        window_size=window_size,
        max_resident_pages=max_resident_pages,
        max_source_pdf_bytes=max_source_pdf_bytes,
        min_document_pages=min_document_pages,
        max_document_pages=max_document_pages,
        inner_inference_concurrency=inner_inference_concurrency,
        vllm_max_num_seqs=vllm_max_num_seqs,
    )


def _configured_capacity_execution_profile(
    configured_window_size: int,
):
    raw_profile = os.getenv(_CAPACITY_PROFILE_ENV)
    if raw_profile is None or not raw_profile.strip():
        return None
    return _parse_capacity_execution_profile(
        raw_profile.strip(),
        configured_window_size,
    )


def _authorized_capacity_catalog(candidate: CapacityExecutionProfile) -> dict:
    raw_path = os.getenv(_CAPACITY_CATALOG_PATH_ENV)
    expected_catalog_sha256 = os.getenv(_CAPACITY_CATALOG_SHA256_ENV)
    runtime_compatibility_sha256 = os.getenv(
        _CAPACITY_RUNTIME_COMPATIBILITY_SHA256_ENV
    )
    if not raw_path or not os.path.isabs(raw_path):
        raise RuntimeError("Auto capacity catalog path is not absolute")
    expected_catalog_sha256 = _capacity_sha256(
        expected_catalog_sha256,
        label="catalog",
    )
    runtime_compatibility_sha256 = _capacity_sha256(
        runtime_compatibility_sha256,
        label="runtime compatibility",
    )
    descriptor = None
    try:
        descriptor = os.open(
            raw_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > _CAPACITY_MAX_CATALOG_BYTES
        ):
            raise RuntimeError("Auto capacity catalog file is not bounded regular data")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            encoded = handle.read(_CAPACITY_MAX_CATALOG_BYTES + 1)
    except OSError as exc:
        raise RuntimeError("Auto capacity catalog cannot be read") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(encoded) > _CAPACITY_MAX_CATALOG_BYTES:
        raise RuntimeError("Auto capacity catalog exceeds the size limit")
    if "sha256:" + hashlib.sha256(encoded).hexdigest() != expected_catalog_sha256:
        raise RuntimeError("Auto capacity catalog hash drifted")
    try:
        payload = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Auto capacity catalog JSON is invalid") from exc
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if canonical != encoded:
        raise RuntimeError("Auto capacity catalog is not canonical JSON")
    if (
        not isinstance(payload, dict)
        or set(payload) != _CAPACITY_CATALOG_FIELDS
        or payload.get("schema") != _CAPACITY_CATALOG_SCHEMA
        or payload.get("profile_id") != candidate.profile_id
        or payload.get("profile_sha256") != candidate.profile_sha256
        or payload.get("runtime_compatibility_sha256")
        != runtime_compatibility_sha256
    ):
        raise RuntimeError("Auto capacity catalog identity drifted")
    for field in (
        "commissioning_evaluator_sha256",
        "commissioning_receipt_sha256",
        "profile_sha256",
        "runtime_compatibility_sha256",
    ):
        _capacity_sha256(payload.get(field), label=field)
    return {
        "catalog_sha256": expected_catalog_sha256,
        **payload,
    }


def capacity_runtime_status(configured_window_size: int) -> dict:
    """Return a content-free, hash-bound view of process admission policy."""
    configured_window_size = _capacity_integer(
        configured_window_size,
        label="configured_window_size",
        minimum=1,
    )
    mode = capacity_mode()
    legacy = _legacy_execution_profile(configured_window_size)
    candidate = _configured_capacity_execution_profile(configured_window_size)
    if mode != "legacy" and candidate is None:
        raise RuntimeError("MINERU_CAPACITY_PROFILE_JSON must be configured")
    auto_catalog = (
        _authorized_capacity_catalog(candidate)
        if mode == "auto" and candidate is not None
        else None
    )
    nonlegacy_admission_enabled = bool(
        candidate is not None
        and (mode == "candidate" or auto_catalog is not None)
    )
    candidate_payload = None
    if candidate is not None:
        candidate_payload = {
            "auto_catalog_sha256": (
                auto_catalog["catalog_sha256"] if auto_catalog is not None else None
            ),
            "inner_inference_concurrency": candidate.inner_inference_concurrency,
            "max_document_pages": candidate.max_document_pages,
            "max_resident_decoded_bytes": candidate.max_resident_decoded_bytes,
            "max_resident_pages": candidate.max_resident_pages,
            "max_source_pdf_bytes": candidate.max_source_pdf_bytes,
            "min_document_pages": candidate.min_document_pages,
            "pipeline_depth": candidate.pipeline_depth,
            "pipeline_mode": candidate.pipeline_mode,
            "profile_id": candidate.profile_id,
            "profile_sha256": candidate.profile_sha256,
            "vllm_max_num_seqs": candidate.vllm_max_num_seqs,
            "window_size": candidate.window_size,
        }
    return {
        "candidate_profile": candidate_payload,
        "configured_window_size": configured_window_size,
        "legacy_profile_sha256": legacy.profile_sha256,
        "mode": mode,
        "nonlegacy_admission_enabled": nonlegacy_admission_enabled,
        "schema": "mineru-capacity-runtime.v1",
    }


def select_capacity_execution_profile(
    *,
    configured_window_size: int,
    page_count: int,
    source_pdf_bytes: int,
) -> CapacityExecutionProfile:
    legacy = _legacy_execution_profile(configured_window_size)
    mode = capacity_mode()
    if mode == "legacy":
        return legacy
    candidate = _configured_capacity_execution_profile(configured_window_size)
    if candidate is None:
        raise RuntimeError("MINERU_CAPACITY_PROFILE_JSON must be configured")
    page_count = _capacity_integer(page_count, label="page_count")
    source_pdf_bytes = _capacity_integer(
        source_pdf_bytes,
        label="source_pdf_bytes",
    )
    eligible = (
        candidate.min_document_pages <= page_count <= candidate.max_document_pages
        and source_pdf_bytes <= candidate.max_source_pdf_bytes
    )
    if mode == "auto":
        _authorized_capacity_catalog(candidate)
    if not eligible:
        return legacy
    return candidate


@dataclass
class CapacityCreditLease:
    page_count: int
    reserved_decoded_bytes: int
    resident_pages_after_acquire: int
    resident_decoded_bytes_after_acquire: int
    actual_decoded_bytes: int = 0
    state: str = "leased"


class CapacityCreditBank:
    """Atomic page/pixel credit bank for one immutable document profile."""

    def __init__(self, profile: CapacityExecutionProfile) -> None:
        if profile.pipeline_mode != "depth1":
            raise RuntimeError("capacity credit bank requires a depth-one profile")
        self.profile = profile
        self.available_pages = profile.max_resident_pages
        self.available_decoded_bytes = profile.max_resident_decoded_bytes
        self.condition = asyncio.Condition()

    async def acquire(self, page_count: int) -> CapacityCreditLease:
        page_count = _capacity_integer(page_count, label="lease_page_count", minimum=1)
        decoded_bytes = page_count * _CAPACITY_MAX_PAGE_PIXEL_BYTES
        if (
            page_count > self.profile.max_resident_pages
            or decoded_bytes > self.profile.max_resident_decoded_bytes
        ):
            raise RuntimeError("capacity lease exceeds its immutable profile")
        async with self.condition:
            await self.condition.wait_for(
                lambda: (
                    self.available_pages >= page_count
                    and self.available_decoded_bytes >= decoded_bytes
                )
            )
            self.available_pages -= page_count
            self.available_decoded_bytes -= decoded_bytes
            return CapacityCreditLease(
                page_count=page_count,
                reserved_decoded_bytes=decoded_bytes,
                resident_pages_after_acquire=(
                    self.profile.max_resident_pages - self.available_pages
                ),
                resident_decoded_bytes_after_acquire=(
                    self.profile.max_resident_decoded_bytes
                    - self.available_decoded_bytes
                ),
            )

    def record_actual_decoded_bytes(
        self,
        lease: CapacityCreditLease,
        actual_decoded_bytes: int,
    ) -> None:
        if lease.state != "leased":
            raise RuntimeError("capacity lease is not active")
        actual_decoded_bytes = _capacity_integer(
            actual_decoded_bytes,
            label="actual_decoded_bytes",
        )
        if actual_decoded_bytes > lease.reserved_decoded_bytes:
            raise RuntimeError("decoded pixels exceed the capacity reservation")
        lease.actual_decoded_bytes = actual_decoded_bytes

    async def release(self, lease: CapacityCreditLease) -> None:
        if lease.state != "leased":
            raise RuntimeError("capacity lease release state is invalid")
        async with self.condition:
            if (
                self.available_pages + lease.page_count
                > self.profile.max_resident_pages
                or self.available_decoded_bytes + lease.reserved_decoded_bytes
                > self.profile.max_resident_decoded_bytes
            ):
                raise RuntimeError("capacity credit bank overflowed")
            self.available_pages += lease.page_count
            self.available_decoded_bytes += lease.reserved_decoded_bytes
            lease.state = "released"
            self.condition.notify_all()

    def assert_fully_released(self) -> None:
        if (
            self.available_pages != self.profile.max_resident_pages
            or self.available_decoded_bytes
            != self.profile.max_resident_decoded_bytes
        ):
            raise RuntimeError("capacity credit bank did not close")


class CapacityCandidateFallback(RuntimeError):
    """Signal a drained, pre-append Auto candidate failure safe to replay."""


class CapacityFallbackGate:
    """Atomically separate safe replay from the first observable mutation."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.is_open = self.enabled
        self.claimed = None

    def claim(self, message: str, primary):
        if not self.is_open or not isinstance(primary, Exception):
            return None
        if self.claimed is None:
            self.claimed = CapacityCandidateFallback(message)
        return self.claimed

    def close_before_output(self) -> None:
        if self.claimed is not None:
            raise self.claimed
        self.is_open = False


class MinerUPhaseTrace:
    """Emit content-free interval events that remain valid under overlap."""

    def __init__(
        self,
        *,
        backend: str,
        page_count: int,
        window_size: int,
        total_windows: int,
        execution_profile: CapacityExecutionProfile,
        source_pdf_bytes: int,
    ) -> None:
        if backend not in _PHASE_TRACE_BACKENDS:
            raise RuntimeError("phase trace backend is unsupported")
        pipeline_mode = execution_profile.pipeline_mode
        profile_id = execution_profile.profile_id
        if pipeline_mode not in _PHASE_TRACE_PIPELINE_MODES:
            raise RuntimeError("phase trace pipeline mode is unsupported")
        if pipeline_mode == "depth1" and backend != "hybrid":
            raise RuntimeError("phase trace pipeline/backend combination is invalid")
        if (
            not isinstance(profile_id, str)
            or not 1 <= len(profile_id) <= 64
            or profile_id[0] not in "abcdefghijklmnopqrstuvwxyz0123456789"
            or any(
                char not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
                for char in profile_id
            )
        ):
            raise RuntimeError("phase trace profile identity is invalid")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (page_count, window_size, total_windows, source_pdf_bytes)
        ):
            raise RuntimeError("phase trace document dimensions are invalid")
        if window_size != execution_profile.window_size:
            raise RuntimeError("phase trace window/profile identity drifted")
        if (
            not isinstance(execution_profile.profile_sha256, str)
            or len(execution_profile.profile_sha256) != 71
            or not execution_profile.profile_sha256.startswith("sha256:")
            or any(
                char not in "0123456789abcdef"
                for char in execution_profile.profile_sha256[7:]
            )
        ):
            raise RuntimeError("phase trace profile hash is invalid")
        self.backend = backend
        self.page_count = page_count
        self.window_size = window_size
        self.total_windows = total_windows
        self.pipeline_mode = pipeline_mode
        self.profile_id = profile_id
        self.profile_sha256 = execution_profile.profile_sha256
        self.pipeline_depth = execution_profile.pipeline_depth
        self.source_pdf_bytes = source_pdf_bytes
        self.max_resident_pages = execution_profile.max_resident_pages
        self.max_resident_decoded_bytes = (
            execution_profile.max_resident_decoded_bytes
        )
        self.inner_inference_concurrency = (
            execution_profile.inner_inference_concurrency
        )
        self.vllm_max_num_seqs = execution_profile.vllm_max_num_seqs
        self.trace_id = uuid.uuid4().hex
        self.sequence = 0
        self.document_started_ns = 0
        self.ended = False

    def _emit(
        self,
        *,
        event: str,
        phase: str,
        outcome: str,
        started_ns: int,
        ended_ns: int,
        window,
        append_index,
        credit_lease,
    ) -> None:
        if phase not in _PHASE_TRACE_PHASES:
            raise RuntimeError("phase trace phase is unsupported")
        if ended_ns < started_ns:
            raise RuntimeError("phase trace interval is invalid")
        if window is None:
            window_index = page_start = page_end_exclusive = window_page_count = None
        else:
            window_index, page_start, page_end_exclusive = window
            window_page_count = page_end_exclusive - page_start
        if credit_lease is None:
            reserved_decoded_bytes = None
            actual_decoded_bytes = resident_pages_after_acquire = None
            resident_decoded_bytes_after_acquire = None
        else:
            reserved_decoded_bytes = credit_lease.reserved_decoded_bytes
            actual_decoded_bytes = credit_lease.actual_decoded_bytes
            resident_pages_after_acquire = credit_lease.resident_pages_after_acquire
            resident_decoded_bytes_after_acquire = (
                credit_lease.resident_decoded_bytes_after_acquire
            )
        with _PHASE_TRACE_OUTPUT_LOCK:
            self.sequence += 1
            payload = {
                "append_index": append_index,
                "actual_decoded_bytes": actual_decoded_bytes,
                "backend": self.backend,
                "duration_ns": ended_ns - started_ns,
                "ended_monotonic_ns": ended_ns,
                "event": event,
                "inner_inference_concurrency": self.inner_inference_concurrency,
                "max_resident_decoded_bytes": self.max_resident_decoded_bytes,
                "max_resident_pages": self.max_resident_pages,
                "outcome": outcome,
                "page_count": self.page_count,
                "page_end_exclusive": page_end_exclusive,
                "page_start": page_start,
                "phase": phase,
                "pipeline_depth": self.pipeline_depth,
                "pipeline_mode": self.pipeline_mode,
                "process_epoch": _PHASE_TRACE_PROCESS_EPOCH,
                "profile_id": self.profile_id,
                "profile_sha256": self.profile_sha256,
                "reserved_decoded_bytes": reserved_decoded_bytes,
                "resident_decoded_bytes_after_acquire": (
                    resident_decoded_bytes_after_acquire
                ),
                "resident_pages_after_acquire": resident_pages_after_acquire,
                "schema": _PHASE_TRACE_SCHEMA,
                "sequence": self.sequence,
                "started_monotonic_ns": started_ns,
                "source_pdf_bytes": self.source_pdf_bytes,
                "total_windows": self.total_windows,
                "trace_id": self.trace_id,
                "window_index": window_index,
                "window_page_count": window_page_count,
                "window_size": self.window_size,
                "vllm_max_num_seqs": self.vllm_max_num_seqs,
            }
            sys.stderr.write(
                _PHASE_TRACE_PREFIX
                + json.dumps(payload, sort_keys=True, separators=(",", ":"))
                + "\\n"
            )
            sys.stderr.flush()

    def document_started(self) -> None:
        if self.document_started_ns or self.ended:
            raise RuntimeError("phase trace document start drifted")
        self.document_started_ns = time.monotonic_ns()
        self._emit(
            event="document_start",
            phase="document",
            outcome="started",
            started_ns=self.document_started_ns,
            ended_ns=self.document_started_ns,
            window=None,
            append_index=None,
            credit_lease=None,
        )

    def _end_document(self, outcome: str) -> None:
        if self.ended:
            return
        if not self.document_started_ns:
            raise RuntimeError("phase trace document ended before start")
        self.ended = True
        self._emit(
            event="document_end",
            phase="document",
            outcome=outcome,
            started_ns=self.document_started_ns,
            ended_ns=time.monotonic_ns(),
            window=None,
            append_index=None,
            credit_lease=None,
        )

    def document_completed(self) -> None:
        self._end_document("success")

    def document_failed(self) -> None:
        self._end_document("error")

    def window(
        self,
        *,
        window_index: int,
        page_start: int,
        page_end_exclusive: int,
    ):
        if (
            isinstance(window_index, bool)
            or isinstance(page_start, bool)
            or isinstance(page_end_exclusive, bool)
            or not 0 <= window_index < self.total_windows
            or not 0 <= page_start < page_end_exclusive <= self.page_count
        ):
            raise RuntimeError("phase trace window dimensions are invalid")
        return (window_index, page_start, page_end_exclusive)

    def start(self) -> int:
        return time.monotonic_ns()

    def complete(
        self,
        phase: str,
        started_ns: int,
        *,
        window=None,
        outcome: str = "success",
        append_index=None,
        credit_lease=None,
    ) -> None:
        if isinstance(started_ns, bool) or not isinstance(started_ns, int):
            raise RuntimeError("phase trace start timestamp is invalid")
        if outcome not in {"success", "error"}:
            raise RuntimeError("phase trace interval outcome is invalid")
        finished_ns = time.monotonic_ns()
        if started_ns <= 0 or finished_ns < started_ns:
            raise RuntimeError("phase trace duration is invalid")
        if (append_index is not None) != (phase == "window_append"):
            raise RuntimeError("phase trace append identity is invalid")
        self._emit(
            event="interval_complete",
            phase=phase,
            outcome=outcome,
            started_ns=started_ns,
            ended_ns=finished_ns,
            window=window,
            append_index=append_index,
            credit_lease=credit_lease,
        )


def new_phase_trace(**kwargs):
    if not is_phase_trace_enabled():
        return _DISABLED_PHASE_TRACE
    return MinerUPhaseTrace(**kwargs)


class OwnedOperation:
    """Linearize cancellation with one started native or remote owner."""

    def __init__(self, awaitable) -> None:
        self.awaitable = awaitable
        self.task = None
        self.state = "new"
        self.cancel_requested = False

    async def run(self, *, on_cancel_result=None):
        if self.state != "new":
            raise RuntimeError("owned operation cannot be reused")
        self.state = "running"
        self.task = asyncio.ensure_future(self.awaitable)
        cancellation = None
        while True:
            try:
                result = await asyncio.shield(self.task)
                self.state = "settled_success"
                break
            except asyncio.CancelledError as exc:
                if self.task.cancelled():
                    self.state = "settled_error"
                    raise
                self.cancel_requested = True
                if cancellation is None:
                    cancellation = exc
                continue
            except BaseException as exc:
                self.state = "settled_error"
                if cancellation is not None:
                    cancellation.add_note(
                        f"owned operation drain failed: {type(exc).__name__}"
                    )
                    self.state = "drained"
                    raise cancellation from exc
                self.state = "drained"
                raise
        self.state = "drained"
        if cancellation is not None:
            if on_cancel_result is not None:
                try:
                    on_cancel_result(result)
                except BaseException as exc:
                    cancellation.add_note(
                        "owned operation cancellation cleanup failed: "
                        f"{type(exc).__name__}"
                    )
                    raise cancellation from exc
            raise cancellation
        return result


async def drain_owned_awaitable(awaitable, *, on_cancel_result=None):
    return await OwnedOperation(awaitable).run(
        on_cancel_result=on_cancel_result,
    )


async def run_native_owned(
    native_owner,
    function,
    /,
    *args,
    on_cancel_result=None,
    **kwargs,
):
    """Serialize native A/C work and never abandon a running executor thread."""
    async with native_owner:
        return await drain_owned_awaitable(
            asyncio.to_thread(function, *args, **kwargs),
            on_cancel_result=on_cancel_result,
        )


async def run_async_owned(
    native_owner,
    awaitable_factory,
    *,
    on_cancel_result=None,
):
    """Create an async wrapper only after acquiring its native owner."""
    if not callable(awaitable_factory):
        raise RuntimeError("async owned operation requires an awaitable factory")
    async with native_owner:
        return await drain_owned_awaitable(
            awaitable_factory(),
            on_cancel_result=on_cancel_result,
        )


async def _await_inference_started(started, inference_task) -> None:
    if started.is_set():
        return
    started_task = asyncio.create_task(started.wait())
    try:
        done, _ = await asyncio.wait(
            (started_task, inference_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if inference_task in done and not started.is_set():
            await inference_task
            raise RuntimeError("inference completed without acquiring its request owner")
        await started_task
    finally:
        if not started_task.done():
            started_task.cancel()
            await asyncio.gather(started_task, return_exceptions=True)


def capacity_pipeline_enabled() -> bool:
    """Report whether this process permits a non-legacy document profile."""
    mode = capacity_mode()
    if mode == "legacy":
        return False
    if mode == "candidate":
        return True
    configured_window_size = _capacity_integer(
        int(os.getenv("MINERU_PROCESSING_WINDOW_SIZE", "0")),
        label="configured_window_size",
        minimum=1,
    )
    profile = _configured_capacity_execution_profile(configured_window_size)
    if profile is None:
        raise RuntimeError("MINERU_CAPACITY_PROFILE_JSON must be configured")
    _authorized_capacity_catalog(profile)
    return True


def capacity_active_window_size(configured_window_size: int) -> int:
    """Return the configured candidate size for process-level diagnostics only."""
    configured_window_size = _capacity_integer(
        configured_window_size,
        label="configured_window_size",
        minimum=1,
    )
    mode = capacity_mode()
    if mode == "legacy":
        return configured_window_size
    profile = _configured_capacity_execution_profile(configured_window_size)
    if profile is None:
        raise RuntimeError("MINERU_CAPACITY_PROFILE_JSON must be configured")
    if mode == "auto":
        _authorized_capacity_catalog(profile)
    return profile.window_size


async def run_bounded_ordered_pipeline(
    items,
    *,
    prepare,
    infer,
    commit,
    release,
) -> None:
    """Run an ordered depth-one pipeline with at most two prepared owners."""
    ordered_items = tuple(items)
    if not ordered_items:
        return
    current = None
    prepared_next = None
    prepare_task = None
    inference_task = None
    next_inference_task = None
    primary = None
    try:
        current = await prepare(ordered_items[0])
        inference_started = asyncio.Event()
        inference_task = asyncio.create_task(infer(current, inference_started))
        await _await_inference_started(inference_started, inference_task)
        for index in range(len(ordered_items)):
            if index + 1 < len(ordered_items):
                prepare_task = asyncio.create_task(prepare(ordered_items[index + 1]))
            inference_result = await inference_task
            inference_task = None
            if prepare_task is not None:
                prepared_next = await prepare_task
                prepare_task = None
                next_inference_started = asyncio.Event()
                next_inference_task = asyncio.create_task(
                    infer(prepared_next, next_inference_started)
                )
                await _await_inference_started(
                    next_inference_started,
                    next_inference_task,
                )
            await commit(current, inference_result)
            current = None
            if prepared_next is not None:
                current = prepared_next
                prepared_next = None
                inference_task = next_inference_task
                next_inference_task = None
    except BaseException as exc:
        primary = exc
    finally:
        task_pairs = tuple(
            (name, task)
            for name, task in (
                ("prepare", prepare_task),
                ("inference", inference_task),
                ("next_inference", next_inference_task),
            )
            if task is not None
        )
        for _name, task in task_pairs:
            if not task.done():
                task.cancel()
        task_results = ()
        if task_pairs:
            try:
                task_results = await drain_owned_awaitable(
                    asyncio.gather(
                        *(task for _name, task in task_pairs),
                        return_exceptions=True,
                    )
                )
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(
                        f"pipeline task drain failed: {type(exc).__name__}"
                    )
        orphan = None
        for (name, _task), result in zip(task_pairs, task_results):
            if isinstance(result, BaseException):
                if not isinstance(result, asyncio.CancelledError):
                    if primary is None:
                        primary = result
                    else:
                        primary.add_note(
                            f"{name} task failed while draining: {type(result).__name__}"
                        )
            elif name == "prepare":
                orphan = result
        released_ids = set()
        for prepared in (orphan, prepared_next, current):
            if prepared is None or id(prepared) in released_ids:
                continue
            released_ids.add(id(prepared))
            try:
                await release(prepared)
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(
                        f"pipeline release failed: {type(exc).__name__}"
                    )
    if primary is not None:
        raise primary


def is_heap_trim_enabled() -> bool:
    """Require an explicit, closed-vocabulary heap-return policy."""
    value = os.getenv("MINERU_MALLOC_TRIM")
    if value is None:
        raise RuntimeError("MINERU_MALLOC_TRIM must be explicitly configured")
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError("MINERU_MALLOC_TRIM has an invalid value")


@lru_cache(maxsize=1)
def _malloc_trim():
    if not sys.platform.startswith("linux"):
        raise RuntimeError("heap return requires Linux/glibc")
    libc = ctypes.CDLL(None)
    function = getattr(libc, "malloc_trim", None)
    if function is None:
        raise RuntimeError("glibc malloc_trim is unavailable")
    function.argtypes = [ctypes.c_size_t]
    function.restype = ctypes.c_int
    return function


def trim_process_heap() -> bool:
    """Invoke glibc heap return when enabled; never hide an enabled failure."""
    if not is_heap_trim_enabled():
        return False
    _malloc_trim()(0)
    return True


'''
        return _replace_exact(
            source,
            "def clean_memory(device='cuda'):\n",
            helper + "def clean_memory(device='cuda'):\n",
            count=1,
            label="model-utils helper",
        )

    if relative_path == "mineru/backend/vlm/vlm_analyze.py":
        source = _replace_exact(
            source,
            "from ...utils.config_reader import get_device, get_processing_window_size\n\n"
            "from ...utils.enum_class import ImageType\n",
            "from ...utils.config_reader import get_device, get_processing_window_size\n"
            "from ...utils.model_utils import (\n"
            "    drain_owned_awaitable,\n"
            "    legacy_capacity_execution_profile,\n"
            "    new_phase_trace,\n"
            "    trim_process_heap,\n"
            ")\n\n"
            "from ...utils.enum_class import ImageType\n",
            count=1,
            label="VLM import",
        )
        source = _replace_exact(
            source,
            "@contextmanager\n"
            "def predictor_execution_guard(predictor: MinerUClient):\n"
            "    lock = getattr(predictor, \"_mineru_execution_lock\", None)\n"
            "    if lock is None:\n"
            "        yield\n"
            "        return\n"
            "    with lock:\n"
            "        yield\n\n\n"
            "@asynccontextmanager\n"
            "async def aio_predictor_execution_guard(predictor: MinerUClient):\n"
            "    lock = getattr(predictor, \"_mineru_execution_lock\", None)\n"
            "    if lock is None:\n"
            "        yield\n"
            "        return\n"
            "    await asyncio.to_thread(lock.acquire)\n"
            "    try:\n"
            "        yield\n"
            "    finally:\n"
            "        lock.release()\n",
            "@contextmanager\n"
            "def predictor_execution_guard(\n"
            "    predictor: MinerUClient,\n"
            "    *,\n"
            "    phase_trace=None,\n"
            "    trace_window=None,\n"
            "    trace_credit_lease=None,\n"
            "):\n"
            "    phase_started_ns = phase_trace.start() if phase_trace is not None else 0\n"
            "    outcome = \"success\"\n"
            "    lock = getattr(predictor, \"_mineru_execution_lock\", None)\n"
            "    try:\n"
            "        if lock is None:\n"
            "            yield\n"
            "        else:\n"
            "            with lock:\n"
            "                yield\n"
            "    except BaseException:\n"
            "        outcome = \"error\"\n"
            "        raise\n"
            "    finally:\n"
            "        if phase_trace is not None:\n"
            "            phase_trace.complete(\n"
            "                \"window_vlm\",\n"
            "                phase_started_ns,\n"
            "                window=trace_window,\n"
            "                outcome=outcome,\n"
            "                credit_lease=trace_credit_lease,\n"
            "            )\n\n\n"
            "@asynccontextmanager\n"
            "async def aio_predictor_execution_guard(\n"
            "    predictor: MinerUClient,\n"
            "    *,\n"
            "    phase_trace=None,\n"
            "    trace_window=None,\n"
            "    trace_ready_ns=None,\n"
            "    trace_credit_lease=None,\n"
            "):\n"
            "    phase_started_ns = (\n"
            "        phase_trace.start()\n"
            "        if phase_trace is not None and trace_ready_ns is None\n"
            "        else 0\n"
            "    )\n"
            "    queue_wait_completed = False\n"
            "    outcome = \"success\"\n"
            "    lock = getattr(predictor, \"_mineru_execution_lock\", None)\n"
            "    lock_acquired = False\n"
            "    try:\n"
            "        if lock is not None:\n"
            "            await drain_owned_awaitable(\n"
            "                asyncio.to_thread(lock.acquire),\n"
            "                on_cancel_result=lambda acquired: (\n"
            "                    lock.release() if acquired else None\n"
            "                ),\n"
            "            )\n"
            "            lock_acquired = True\n"
            "        if phase_trace is not None and trace_ready_ns is not None:\n"
            "            phase_trace.complete(\n"
            "                \"window_b_queue_wait\",\n"
            "                trace_ready_ns,\n"
            "                window=trace_window,\n"
            "                credit_lease=trace_credit_lease,\n"
            "            )\n"
            "            queue_wait_completed = True\n"
            "            phase_started_ns = phase_trace.start()\n"
            "        yield\n"
            "    except BaseException:\n"
            "        outcome = \"error\"\n"
            "        if (\n"
            "            phase_trace is not None\n"
            "            and trace_ready_ns is not None\n"
            "            and not queue_wait_completed\n"
            "        ):\n"
            "            phase_trace.complete(\n"
            "                \"window_b_queue_wait\",\n"
            "                trace_ready_ns,\n"
            "                window=trace_window,\n"
            "                outcome=\"error\",\n"
            "                credit_lease=trace_credit_lease,\n"
            "            )\n"
            "        raise\n"
            "    finally:\n"
            "        if lock_acquired:\n"
            "            lock.release()\n"
            "        if phase_trace is not None and phase_started_ns:\n"
            "            phase_trace.complete(\n"
            "                \"window_vlm\",\n"
            "                phase_started_ns,\n"
            "                window=trace_window,\n"
            "                outcome=outcome,\n"
            "                credit_lease=trace_credit_lease,\n"
            "            )\n",
            count=1,
            label="VLM predictor phase",
        )
        source = _replace_exact(
            source,
            "    results = []\n    doc_closed = False\n    try:\n",
            "    results = []\n    phase_trace = None\n    doc_closed = False\n    try:\n",
            count=2,
            label="VLM phase trace declaration",
        )
        source = _replace_exact(
            source,
            "with predictor_execution_guard(predictor):",
            "with predictor_execution_guard(\n"
            "                        predictor,\n"
            "                        phase_trace=phase_trace,\n"
            "                        trace_window=window_trace_context,\n"
            "                    ):",
            count=1,
            label="VLM synchronous predictor trace context",
        )
        source = _replace_exact(
            source,
            "async with aio_predictor_execution_guard(predictor):",
            "async with aio_predictor_execution_guard(\n"
            "                        predictor,\n"
            "                        phase_trace=phase_trace,\n"
            "                        trace_window=window_trace_context,\n"
            "                    ):",
            count=1,
            label="VLM asynchronous predictor trace context",
        )
        source = _replace_exact(
            source,
            "        logger.info(\n"
            "            f'VLM processing-window run. page_count={page_count}, '\n"
            "            f'window_size={configured_window_size}, total_windows={total_windows}'\n"
            "        )\n\n"
            "        infer_start = time.time()\n",
            "        logger.info(\n"
            "            f'VLM processing-window run. page_count={page_count}, '\n"
            "            f'window_size={configured_window_size}, total_windows={total_windows}'\n"
            "        )\n"
            "        execution_profile = legacy_capacity_execution_profile(\n"
            "            configured_window_size\n"
            "        )\n"
            "        phase_trace = new_phase_trace(\n"
            "            backend=\"vlm\",\n"
            "            page_count=page_count,\n"
            "            window_size=configured_window_size,\n"
            "            total_windows=total_windows,\n"
            "            execution_profile=execution_profile,\n"
            "            source_pdf_bytes=len(pdf_bytes),\n"
            "        )\n"
            "        phase_trace.document_started()\n\n"
            "        infer_start = time.time()\n",
            count=2,
            label="VLM document phase start",
        )
        source = _replace_exact(
            source,
            "            for window_index, window_start in enumerate(range(0, page_count, effective_window_size or 1)):\n"
            "                window_end = min(page_count - 1, window_start + effective_window_size - 1)\n",
            "            for window_index, window_start in enumerate(range(0, page_count, effective_window_size or 1)):\n"
            "                window_end = min(page_count - 1, window_start + effective_window_size - 1)\n"
            "                window_trace_context = phase_trace.window(\n"
            "                    window_index=window_index,\n"
            "                    page_start=window_start,\n"
            "                    page_end_exclusive=window_end + 1,\n"
            "                )\n"
            "                window_started_ns = phase_trace.start()\n"
            "                render_started_ns = phase_trace.start()\n",
            count=2,
            label="VLM window phase start",
        )
        source = _replace_exact(
            source,
            "                images_list = load_images_from_pdf_doc(\n"
            "                    pdf_doc,\n"
            "                    start_page_id=window_start,\n"
            "                    end_page_id=window_end,\n"
            "                    image_type=ImageType.PIL,\n"
            "                    pdf_bytes=pdf_bytes,\n"
            "                )\n"
            "                try:\n",
            "                images_list = load_images_from_pdf_doc(\n"
            "                    pdf_doc,\n"
            "                    start_page_id=window_start,\n"
            "                    end_page_id=window_end,\n"
            "                    image_type=ImageType.PIL,\n"
            "                    pdf_bytes=pdf_bytes,\n"
            "                )\n"
            "                phase_trace.complete(\n"
            "                    \"window_render\",\n"
            "                    render_started_ns,\n"
            "                    window=window_trace_context,\n"
            "                )\n"
            "                try:\n",
            count=1,
            label="VLM synchronous render phase",
        )
        source = _replace_exact(
            source,
            "                images_list = await aio_load_images_from_pdf_bytes_range(\n"
            "                    pdf_bytes,\n"
            "                    start_page_id=window_start,\n"
            "                    end_page_id=window_end,\n"
            "                    image_type=ImageType.PIL,\n"
            "                )\n"
            "                try:\n",
            "                images_list = await aio_load_images_from_pdf_bytes_range(\n"
            "                    pdf_bytes,\n"
            "                    start_page_id=window_start,\n"
            "                    end_page_id=window_end,\n"
            "                    image_type=ImageType.PIL,\n"
            "                )\n"
            "                phase_trace.complete(\n"
            "                    \"window_render\",\n"
            "                    render_started_ns,\n"
            "                    window=window_trace_context,\n"
            "                )\n"
            "                try:\n",
            count=1,
            label="VLM asynchronous render phase",
        )
        source = _replace_exact(
            source,
            "                    append_page_blocks_to_middle_json(\n"
            "                        middle_json,\n",
            "                    append_started_ns = phase_trace.start()\n"
            "                    append_page_blocks_to_middle_json(\n"
            "                        middle_json,\n",
            count=2,
            label="VLM append phase start",
        )
        source = _replace_exact(
            source,
            "                        progress_bar=progress_bar,\n"
            "                    )\n"
            "                    last_append_end_time = time.time()\n"
            "                finally:\n"
            "                    _close_images(images_list)\n",
            "                        progress_bar=progress_bar,\n"
            "                    )\n"
            "                    phase_trace.complete(\n"
            "                        \"window_append\",\n"
            "                        append_started_ns,\n"
            "                        window=window_trace_context,\n"
            "                        append_index=window_index,\n"
            "                    )\n"
            "                    last_append_end_time = time.time()\n"
            "                finally:\n"
            "                    _close_images(images_list)\n"
            "                    trim_process_heap()\n"
            "                    phase_trace.complete(\n"
            "                        \"window_total\",\n"
            "                        window_started_ns,\n"
            "                        window=window_trace_context,\n"
            "                    )\n",
            count=2,
            label="VLM append and window completion",
        )
        source = _replace_exact(
            source,
            "        if not client_side_output_generation:\n"
            "            finalize_middle_json(middle_json[\"pdf_info\"])\n"
            "        close_pdfium_document(pdf_doc)\n",
            "        finalize_started_ns = phase_trace.start()\n"
            "        if not client_side_output_generation:\n"
            "            finalize_middle_json(middle_json[\"pdf_info\"])\n"
            "        phase_trace.complete(\"document_finalize\", finalize_started_ns)\n"
            "        close_pdfium_document(pdf_doc)\n",
            count=1,
            label="VLM synchronous finalize phase",
        )
        source = _replace_exact(
            source,
            "        if not client_side_output_generation:\n"
            "            await asyncio.to_thread(finalize_middle_json, middle_json[\"pdf_info\"])\n"
            "        close_pdfium_document(pdf_doc)\n",
            "        finalize_started_ns = phase_trace.start()\n"
            "        if not client_side_output_generation:\n"
            "            await asyncio.to_thread(finalize_middle_json, middle_json[\"pdf_info\"])\n"
            "        phase_trace.complete(\"document_finalize\", finalize_started_ns)\n"
            "        close_pdfium_document(pdf_doc)\n",
            count=1,
            label="VLM asynchronous finalize phase",
        )
        source = _replace_exact(
            source,
            "        doc_closed = True\n        return middle_json, results\n",
            "        doc_closed = True\n"
            "        phase_trace.document_completed()\n"
            "        trim_process_heap()\n"
            "        return middle_json, results\n",
            count=2,
            label="VLM document completion",
        )
        return _replace_exact(
            source,
            "    finally:\n"
            "        if not doc_closed:\n"
            "            close_pdfium_document(pdf_doc)\n",
            "    finally:\n"
            "        if not doc_closed:\n"
            "            if phase_trace is not None:\n"
            "                phase_trace.document_failed()\n"
            "            close_pdfium_document(pdf_doc)\n",
            count=2,
            label="VLM document failure",
        )

    if relative_path == "mineru/backend/hybrid/hybrid_analyze.py":
        source = _replace_exact(
            source,
            "from mineru.utils.model_utils import clean_memory, crop_img, get_vram\n",
            "from mineru.utils.model_utils import (\n"
            "    CapacityCandidateFallback,\n"
            "    CapacityCreditBank,\n"
            "    capacity_mode,\n"
            "    clean_memory,\n"
            "    crop_img,\n"
            "    get_vram,\n"
            "    legacy_capacity_execution_profile,\n"
            "    new_phase_trace,\n"
            "    run_bounded_ordered_pipeline,\n"
            "    run_async_owned,\n"
            "    run_native_owned,\n"
            "    select_capacity_execution_profile,\n"
            "    drain_owned_awaitable,\n"
            "    trim_process_heap,\n"
            ")\n",
            count=1,
            label="Hybrid import",
        )
        source = _replace_exact(
            source,
            "    model_list = []\n"
            "    doc_closed = False\n"
            "    hybrid_pipeline_model = None\n",
            "    model_list = []\n"
            "    phase_trace = None\n"
            "    doc_closed = False\n"
            "    hybrid_pipeline_model = None\n",
            count=2,
            label="Hybrid phase trace declaration",
        )
        pipeline_helper = '''async def _aio_run_hybrid_capacity_pipeline(
    *,
    pdf_bytes,
    pdf_doc,
    image_writer,
    predictor,
    middle_json,
    page_count,
    effective_window_size,
    phase_trace,
    inline_formula_enable,
    batch_ratio,
    ocr_enable,
    effort,
    effective_image_analysis,
    native_owner,
    execution_profile,
    allow_auto_fallback,
):
    """Run a two-window, ordered A/B/C pipeline inside one whole PDF."""
    model_list = []
    hybrid_pipeline_model = None
    progress_bar = None
    last_append_end_time = None
    expected_append_index = 0
    fallback_gate = CapacityFallbackGate(allow_auto_fallback)
    credit_bank = CapacityCreditBank(execution_profile)
    windows = tuple(
        (
            window_index,
            window_start,
            min(page_count - 1, window_start + effective_window_size - 1),
        )
        for window_index, window_start in enumerate(
            range(0, page_count, effective_window_size or 1)
        )
    )

    def close_and_trim(images_list) -> None:
        _close_images(images_list)
        trim_process_heap()

    async def release(prepared) -> None:
        owner_state = prepared.get("owner_state")
        if owner_state == "released":
            return
        if owner_state not in {"owned", "resources_released"}:
            raise RuntimeError(
                f"prepared window release state is invalid: {owner_state}"
            )
        release_started_ns = phase_trace.start()
        try:
            if owner_state == "owned":
                prepared["owner_state"] = "releasing"
                close_and_trim(prepared.get("images_list"))
                prepared["owner_state"] = "resources_released"
            await drain_owned_awaitable(
                credit_bank.release(prepared["credit_lease"])
            )
            prepared["owner_state"] = "released"
        except BaseException:
            if prepared.get("owner_state") == "releasing":
                prepared["owner_state"] = "owned"
            phase_trace.complete(
                "window_release",
                release_started_ns,
                window=prepared["trace_window"],
                outcome="error",
                credit_lease=prepared["credit_lease"],
            )
            raise
        phase_trace.complete(
            "window_release",
            release_started_ns,
            window=prepared["trace_window"],
            credit_lease=prepared["credit_lease"],
        )

    async def prepare(window):
        window_index, window_start, window_end = window
        trace_window = phase_trace.window(
            window_index=window_index,
            page_start=window_start,
            page_end_exclusive=window_end + 1,
        )
        images_list = None
        credit_lease = None
        window_started_ns = phase_trace.start()
        try:
            credit_wait_started_ns = phase_trace.start()
            try:
                credit_lease = await credit_bank.acquire(
                    window_end - window_start + 1
                )
            except BaseException:
                phase_trace.complete(
                    "window_credit_wait",
                    credit_wait_started_ns,
                    window=trace_window,
                    outcome="error",
                )
                raise
            phase_trace.complete(
                "window_credit_wait",
                credit_wait_started_ns,
                window=trace_window,
                credit_lease=credit_lease,
            )
            render_started_ns = phase_trace.start()
            try:
                images_list = await run_async_owned(
                    native_owner,
                    lambda: aio_load_images_from_pdf_bytes_range(
                        pdf_bytes,
                        start_page_id=window_start,
                        end_page_id=window_end,
                        image_type=ImageType.PIL,
                    ),
                    on_cancel_result=close_and_trim,
                )
            except BaseException:
                phase_trace.complete(
                    "window_render",
                    render_started_ns,
                    window=trace_window,
                    outcome="error",
                    credit_lease=credit_lease,
                )
                raise
            phase_trace.complete(
                "window_render",
                render_started_ns,
                window=trace_window,
                credit_lease=credit_lease,
            )
            images_pil_list = [image_dict["img_pil"] for image_dict in images_list]
            actual_decoded_bytes = sum(
                int(np.asarray(image).nbytes) for image in images_pil_list
            )
            credit_bank.record_actual_decoded_bytes(
                credit_lease,
                actual_decoded_bytes,
            )
            page_sizes = [_normalize_page_size(image) for image in images_pil_list]
            logger.info(
                f'Hybrid capacity window {window_index + 1}/{len(windows)}: '
                f'pages {window_start + 1}-{window_end + 1}/{page_count} '
                f'({len(images_pil_list)} pages)'
            )
            layout_started_ns = phase_trace.start()
            try:
                images_layout_res, pipeline_model = await run_native_owned(
                    native_owner,
                    _predict_layout_for_window,
                    images_pil_list,
                    inline_formula_enable,
                    batch_ratio,
                    ocr_enable,
                )
                vlm_blocks_list = None
                if effort == "medium":
                    await run_native_owned(
                        native_owner,
                        _apply_medium_table_orientation_labels,
                        images_pil_list,
                        images_layout_res,
                        pipeline_model,
                        batch_ratio,
                    )
                    vlm_blocks_list = [
                        _build_medium_vlm_layout_blocks(
                            page_layout_res,
                            pil_img.width,
                            pil_img.height,
                        )
                        for page_layout_res, pil_img in zip(
                            images_layout_res,
                            images_pil_list,
                        )
                    ]
            except BaseException:
                phase_trace.complete(
                    "window_layout",
                    layout_started_ns,
                    window=trace_window,
                    outcome="error",
                    credit_lease=credit_lease,
                )
                raise
            phase_trace.complete(
                "window_layout",
                layout_started_ns,
                window=trace_window,
                credit_lease=credit_lease,
            )
            b_ready_ns = phase_trace.start()
            return {
                "images_layout_res": images_layout_res,
                "images_list": images_list,
                "images_pil_list": images_pil_list,
                "page_sizes": page_sizes,
                "pipeline_model": pipeline_model,
                "credit_lease": credit_lease,
                "b_ready_ns": b_ready_ns,
                "owner_state": "owned",
                "trace_window": trace_window,
                "vlm_blocks_list": vlm_blocks_list,
                "window_end": window_end,
                "window_index": window_index,
                "window_start": window_start,
                "window_started_ns": window_started_ns,
            }
        except BaseException as primary:
            resources_released = images_list is None
            if images_list is not None:
                try:
                    close_and_trim(images_list)
                    resources_released = True
                except BaseException as cleanup_error:
                    primary.add_note(
                        "prepare cleanup failed: "
                        f"{type(cleanup_error).__name__}"
                    )
            if (
                resources_released
                and credit_lease is not None
                and credit_lease.state == "leased"
            ):
                try:
                    release_started_ns = phase_trace.start()
                    await drain_owned_awaitable(
                        credit_bank.release(credit_lease)
                    )
                except BaseException as credit_error:
                    phase_trace.complete(
                        "window_release",
                        release_started_ns,
                        window=trace_window,
                        outcome="error",
                        credit_lease=credit_lease,
                    )
                    primary.add_note(
                        "prepare credit release failed: "
                        f"{type(credit_error).__name__}"
                    )
                else:
                    phase_trace.complete(
                        "window_release",
                        release_started_ns,
                        window=trace_window,
                        credit_lease=credit_lease,
                    )
            fallback_safe = bool(
                expected_append_index == 0
                and fallback_gate.is_open
                and allow_auto_fallback
                and isinstance(primary, Exception)
                and (
                    credit_lease is None
                    or (
                        resources_released
                        and credit_lease.state == "released"
                    )
                )
            )
            if fallback_safe:
                raise fallback_gate.claim(
                    "Auto candidate preparation failed before the first append",
                    primary,
                ) from primary
            raise

    async def infer(prepared, inference_started):
        trace_window = prepared["trace_window"]
        try:
            if effort == "medium":
                async with aio_predictor_execution_guard(
                    predictor,
                    phase_trace=phase_trace,
                    trace_window=trace_window,
                    trace_ready_ns=prepared["b_ready_ns"],
                    trace_credit_lease=prepared["credit_lease"],
                ):
                    inference_started.set()
                    return await drain_owned_awaitable(
                        predictor.aio_batch_extract_with_layout(
                            prepared["images_pil_list"],
                            prepared["vlm_blocks_list"],
                            not_extract_list=None if ocr_enable else not_extract_list,
                            image_analysis=effective_image_analysis,
                        )
                    )
            if effort == "high":
                async with aio_predictor_execution_guard(
                    predictor,
                    phase_trace=phase_trace,
                    trace_window=trace_window,
                    trace_ready_ns=prepared["b_ready_ns"],
                    trace_credit_lease=prepared["credit_lease"],
                ):
                    inference_started.set()
                    return await drain_owned_awaitable(
                        predictor.aio_batch_two_step_extract(
                            images=prepared["images_pil_list"],
                            not_extract_list=None if ocr_enable else not_extract_list,
                            image_analysis=effective_image_analysis,
                        )
                    )
            raise ValueError(f"Unsupported hybrid effort: {effort}")
        except Exception as primary:
            fallback = fallback_gate.claim(
                "Auto candidate inference failed before the first append",
                primary,
            )
            if fallback is not None:
                raise fallback from primary
            raise

    async def commit(prepared, window_model_list) -> None:
        nonlocal hybrid_pipeline_model, progress_bar, last_append_end_time
        nonlocal expected_append_index
        trace_window = prepared["trace_window"]
        completed = False
        try:
            images_pil_list = prepared["images_pil_list"]
            images_layout_res = prepared["images_layout_res"]
            pipeline_model = prepared["pipeline_model"]
            postprocess_started_ns = phase_trace.start()
            try:
                if effort == "medium":
                    optimize_hybrid_formula_number_blocks(window_model_list)
                if ocr_enable:
                    await run_native_owned(
                        native_owner,
                        _apply_vlm_ocr_det_sidecars_for_window,
                        images_pil_list,
                        window_model_list,
                        batch_ratio,
                        images_layout_res=images_layout_res,
                        hybrid_pipeline_model=pipeline_model,
                    )
                else:
                    window_model_list = await run_native_owned(
                        native_owner,
                        _process_ocr_and_formulas,
                        images_pil_list,
                        window_model_list,
                        inline_formula_enable,
                        batch_ratio=batch_ratio,
                        images_layout_res=images_layout_res,
                        hybrid_pipeline_model=pipeline_model,
                    )
                await run_native_owned(
                    native_owner,
                    _apply_layout_title_split,
                    window_model_list,
                    images_layout_res,
                    prepared["page_sizes"],
                )
            except BaseException as primary:
                phase_trace.complete(
                    "window_postprocess",
                    postprocess_started_ns,
                    window=trace_window,
                    outcome="error",
                    credit_lease=prepared["credit_lease"],
                )
                if (
                    fallback_gate.is_open
                    and allow_auto_fallback
                    and isinstance(primary, Exception)
                ):
                    raise fallback_gate.claim(
                        "Auto candidate postprocess failed before the first append",
                        primary,
                    ) from primary
                raise
            phase_trace.complete(
                "window_postprocess",
                postprocess_started_ns,
                window=trace_window,
                credit_lease=prepared["credit_lease"],
            )
            # No await is permitted between this close and the first mutation.
            # The event loop therefore cannot misclassify a speculative failure
            # as pre-append after candidate output becomes observable.
            fallback_gate.close_before_output()
            hybrid_pipeline_model = pipeline_model
            if prepared["window_index"] != expected_append_index:
                raise RuntimeError("capacity pipeline append order drifted")
            model_list.extend(window_model_list)
            if progress_bar is None:
                progress_bar = tqdm(total=page_count, desc="Processing pages")
            else:
                exclude_progress_bar_idle_time(
                    progress_bar,
                    last_append_end_time,
                    now=time.time(),
                )
            append_started_ns = phase_trace.start()
            try:
                append_page_model_list_to_middle_json(
                    middle_json,
                    window_model_list,
                    prepared["images_list"],
                    pdf_doc,
                    image_writer,
                    page_start_index=prepared["window_start"],
                    _ocr_enable=ocr_enable,
                    progress_bar=progress_bar,
                )
            except BaseException:
                phase_trace.complete(
                    "window_append",
                    append_started_ns,
                    window=trace_window,
                    outcome="error",
                    append_index=expected_append_index,
                    credit_lease=prepared["credit_lease"],
                )
                raise
            phase_trace.complete(
                "window_append",
                append_started_ns,
                window=trace_window,
                append_index=expected_append_index,
                credit_lease=prepared["credit_lease"],
            )
            expected_append_index += 1
            last_append_end_time = time.time()
            completed = True
        finally:
            await release(prepared)
            if completed:
                phase_trace.complete(
                    "window_total",
                    prepared["window_started_ns"],
                    window=trace_window,
                    credit_lease=prepared["credit_lease"],
                )

    infer_start = time.time()
    try:
        try:
            await run_bounded_ordered_pipeline(
                windows,
                prepare=prepare,
                infer=infer,
                commit=commit,
                release=release,
            )
        except CapacityCandidateFallback:
            credit_bank.assert_fully_released()
            raise
        credit_bank.assert_fully_released()
    finally:
        if progress_bar is not None:
            progress_bar.close()
    infer_time = round(time.time() - infer_start, 2)
    if infer_time > 0 and page_count > 0:
        logger.debug(
            f"capacity pipeline infer finished, cost: {infer_time}, "
            f"speed: {round(len(model_list) / infer_time, 3)} page/s"
        )
    return model_list, hybrid_pipeline_model


'''
        source = _replace_exact(
            source,
            "async def aio_doc_analyze(\n",
            pipeline_helper + "async def aio_doc_analyze(\n",
            count=1,
            label="Hybrid capacity pipeline helper",
        )
        source = _replace_exact(
            source,
            "with predictor_execution_guard(predictor):",
            "with predictor_execution_guard("
            "predictor, phase_trace=phase_trace, "
            "trace_window=window_trace_context):",
            count=3,
            label="Hybrid synchronous predictor trace context",
        )
        source = _replace_exact(
            source,
            "async with aio_predictor_execution_guard(predictor):",
            "async with aio_predictor_execution_guard("
            "predictor, phase_trace=phase_trace, "
            "trace_window=window_trace_context):",
            count=3,
            label="Hybrid asynchronous predictor trace context",
        )
        source = _replace_exact(
            source,
            "                        optimize_hybrid_formula_number_blocks(window_model_list)\n",
            "                        postprocess_started_ns = phase_trace.start()\n"
            "                        optimize_hybrid_formula_number_blocks(window_model_list)\n",
            count=2,
            label="Hybrid medium postprocess phase start",
        )
        source = _replace_exact_occurrence(
            source,
            "                            _apply_vlm_ocr_det_sidecars_for_window(\n",
            "                            postprocess_started_ns = phase_trace.start()\n"
            "                            _apply_vlm_ocr_det_sidecars_for_window(\n",
            count=2,
            occurrence=1,
            label="Hybrid synchronous high OCR postprocess phase start",
        )
        source = _replace_exact_occurrence(
            source,
            "                            window_model_list = _process_ocr_and_formulas(\n",
            "                            postprocess_started_ns = phase_trace.start()\n"
            "                            window_model_list = _process_ocr_and_formulas(\n",
            count=2,
            occurrence=1,
            label="Hybrid synchronous high native postprocess phase start",
        )
        source = _replace_exact_occurrence(
            source,
            "                            await asyncio.to_thread(\n"
            "                                _apply_vlm_ocr_det_sidecars_for_window,\n",
            "                            postprocess_started_ns = phase_trace.start()\n"
            "                            await asyncio.to_thread(\n"
            "                                _apply_vlm_ocr_det_sidecars_for_window,\n",
            count=2,
            occurrence=1,
            label="Hybrid asynchronous high OCR postprocess phase start",
        )
        source = _replace_exact_occurrence(
            source,
            "                            window_model_list = await asyncio.to_thread(\n"
            "                                _process_ocr_and_formulas,\n",
            "                            postprocess_started_ns = phase_trace.start()\n"
            "                            window_model_list = await asyncio.to_thread(\n"
            "                                _process_ocr_and_formulas,\n",
            count=2,
            occurrence=1,
            label="Hybrid asynchronous high native postprocess phase start",
        )
        source = _replace_exact(
            source,
            "                    _apply_layout_title_split(\n"
            "                        window_model_list,\n"
            "                        images_layout_res,\n"
            "                        page_sizes,\n"
            "                    )\n"
            "                    model_list.extend(window_model_list)\n",
            "                    _apply_layout_title_split(\n"
            "                        window_model_list,\n"
            "                        images_layout_res,\n"
            "                        page_sizes,\n"
            "                    )\n"
            "                    phase_trace.complete(\n"
            "                        \"window_postprocess\",\n"
            "                        postprocess_started_ns,\n"
            "                        window=window_trace_context,\n"
            "                    )\n"
            "                    model_list.extend(window_model_list)\n",
            count=1,
            label="Hybrid synchronous postprocess phase end",
        )
        source = _replace_exact(
            source,
            "                    await asyncio.to_thread(\n"
            "                        _apply_layout_title_split,\n"
            "                        window_model_list,\n"
            "                        images_layout_res,\n"
            "                        page_sizes,\n"
            "                    )\n"
            "                    model_list.extend(window_model_list)\n",
            "                    await asyncio.to_thread(\n"
            "                        _apply_layout_title_split,\n"
            "                        window_model_list,\n"
            "                        images_layout_res,\n"
            "                        page_sizes,\n"
            "                    )\n"
            "                    phase_trace.complete(\n"
            "                        \"window_postprocess\",\n"
            "                        postprocess_started_ns,\n"
            "                        window=window_trace_context,\n"
            "                    )\n"
            "                    model_list.extend(window_model_list)\n",
            count=1,
            label="Hybrid asynchronous postprocess phase end",
        )
        source = _replace_exact_occurrence(
            source,
            "        configured_window_size = get_processing_window_size(default=64)\n"
            "        effective_window_size = min(page_count, configured_window_size) if page_count else 0\n",
            "        configured_window_size = get_processing_window_size(default=64)\n"
            "        execution_profile = legacy_capacity_execution_profile(\n"
            "            configured_window_size\n"
            "        )\n"
            "        active_window_size = execution_profile.window_size\n"
            "        effective_window_size = min(page_count, active_window_size) if page_count else 0\n",
            count=2,
            occurrence=0,
            label="Hybrid synchronous legacy window selection",
        )
        source = _replace_exact(
            source,
            "        configured_window_size = get_processing_window_size(default=64)\n"
            "        effective_window_size = min(page_count, configured_window_size) if page_count else 0\n",
            "        configured_window_size = get_processing_window_size(default=64)\n"
            "        execution_profile = select_capacity_execution_profile(\n"
            "            configured_window_size=configured_window_size,\n"
            "            page_count=page_count,\n"
            "            source_pdf_bytes=len(pdf_bytes),\n"
            "        )\n"
            "        active_window_size = execution_profile.window_size\n"
            "        effective_window_size = min(page_count, active_window_size) if page_count else 0\n",
            count=1,
            label="Hybrid asynchronous active window selection",
        )
        source = _replace_exact_occurrence(
            source,
            "        logger.info(\n"
            "            f'Hybrid processing-window run. page_count={page_count}, '\n"
            "            f'window_size={configured_window_size}, total_windows={total_windows}'\n"
            "        )\n\n"
            "        batch_ratio = get_batch_ratio(device) if not _ocr_enable else 1\n",
            "        logger.info(\n"
            "            f'Hybrid processing-window run. page_count={page_count}, '\n"
            "            f'window_size={configured_window_size}, total_windows={total_windows}'\n"
            "        )\n"
            "        phase_trace = new_phase_trace(\n"
            "            backend=\"hybrid\",\n"
            "            page_count=page_count,\n"
            "            window_size=active_window_size,\n"
            "            total_windows=total_windows,\n"
            "            execution_profile=execution_profile,\n"
            "            source_pdf_bytes=len(pdf_bytes),\n"
            "        )\n"
            "        phase_trace.document_started()\n\n"
            "        batch_ratio = get_batch_ratio(device) if not _ocr_enable else 1\n",
            count=2,
            occurrence=0,
            label="Hybrid synchronous document phase start",
        )
        source = _replace_exact(
            source,
            "        logger.info(\n"
            "            f'Hybrid processing-window run. page_count={page_count}, '\n"
            "            f'window_size={configured_window_size}, total_windows={total_windows}'\n"
            "        )\n\n"
            "        batch_ratio = get_batch_ratio(device) if not _ocr_enable else 1\n",
            "        logger.info(\n"
            "            f'Hybrid processing-window run. page_count={page_count}, '\n"
            "            f'window_size={configured_window_size}, total_windows={total_windows}'\n"
            "        )\n"
            "        phase_trace = new_phase_trace(\n"
            "            backend=\"hybrid\",\n"
            "            page_count=page_count,\n"
            "            window_size=active_window_size,\n"
            "            total_windows=total_windows,\n"
            "            execution_profile=execution_profile,\n"
            "            source_pdf_bytes=len(pdf_bytes),\n"
            "        )\n"
            "        phase_trace.document_started()\n\n"
            "        batch_ratio = get_batch_ratio(device) if not _ocr_enable else 1\n",
            count=1,
            label="Hybrid asynchronous document phase start",
        )
        source = _replace_exact_occurrence(
            source,
            "        batch_ratio = get_batch_ratio(device) if not _ocr_enable else 1\n\n"
            "        infer_start = time.time()\n",
            "        batch_ratio = get_batch_ratio(device) if not _ocr_enable else 1\n\n"
            "        if execution_profile.pipeline_mode == \"depth1\":\n"
            "            native_owner = asyncio.Lock()\n"
            "            allow_auto_fallback = capacity_mode() == \"auto\"\n"
            "            try:\n"
            "                model_list, hybrid_pipeline_model = (\n"
            "                    await _aio_run_hybrid_capacity_pipeline(\n"
            "                        pdf_bytes=pdf_bytes,\n"
            "                        pdf_doc=pdf_doc,\n"
            "                        image_writer=image_writer,\n"
            "                        predictor=predictor,\n"
            "                        middle_json=middle_json,\n"
            "                        page_count=page_count,\n"
            "                        effective_window_size=effective_window_size,\n"
            "                        phase_trace=phase_trace,\n"
            "                        inline_formula_enable=inline_formula_enable,\n"
            "                        batch_ratio=batch_ratio,\n"
            "                        ocr_enable=_ocr_enable,\n"
            "                        effort=effort,\n"
            "                        effective_image_analysis=effective_image_analysis,\n"
            "                        native_owner=native_owner,\n"
            "                        execution_profile=execution_profile,\n"
            "                        allow_auto_fallback=allow_auto_fallback,\n"
            "                    )\n"
            "                )\n"
            "            except CapacityCandidateFallback:\n"
            "                if not allow_auto_fallback:\n"
            "                    raise\n"
            "                if middle_json.get(\"pdf_info\"):\n"
            "                    raise RuntimeError(\n"
            "                        \"Auto fallback observed candidate output\"\n"
            "                    )\n"
            "                phase_trace.document_failed()\n"
            "                execution_profile = legacy_capacity_execution_profile(\n"
            "                    configured_window_size\n"
            "                )\n"
            "                active_window_size = execution_profile.window_size\n"
            "                effective_window_size = (\n"
            "                    min(page_count, active_window_size) if page_count else 0\n"
            "                )\n"
            "                total_windows = (\n"
            "                    (page_count + effective_window_size - 1)\n"
            "                    // effective_window_size\n"
            "                    if effective_window_size\n"
            "                    else 0\n"
            "                )\n"
            "                phase_trace = new_phase_trace(\n"
            "                    backend=\"hybrid\",\n"
            "                    page_count=page_count,\n"
            "                    window_size=active_window_size,\n"
            "                    total_windows=total_windows,\n"
            "                    execution_profile=execution_profile,\n"
            "                    source_pdf_bytes=len(pdf_bytes),\n"
            "                )\n"
            "                phase_trace.document_started()\n"
            "                logger.warning(\n"
            "                    \"Auto capacity candidate failed before append; \"\n"
            "                    \"replaying this document with the legacy profile\"\n"
            "                )\n"
            "            else:\n"
            "                finalize_started_ns = phase_trace.start()\n"
            "                if client_side_output_generation:\n"
            "                    await run_native_owned(\n"
            "                        native_owner,\n"
            "                        apply_server_side_postprocess,\n"
            "                        middle_json[\"pdf_info\"],\n"
            "                        hybrid_pipeline_model,\n"
            "                        _ocr_enable,\n"
            "                    )\n"
            "                else:\n"
            "                    await run_native_owned(\n"
            "                        native_owner,\n"
            "                        finalize_middle_json,\n"
            "                        middle_json[\"pdf_info\"],\n"
            "                        hybrid_pipeline_model,\n"
            "                        _ocr_enable,\n"
            "                        effort=effort,\n"
            "                    )\n"
            "                phase_trace.complete(\"document_finalize\", finalize_started_ns)\n"
            "                close_pdfium_document(pdf_doc)\n"
            "                doc_closed = True\n"
            "                clean_memory(device)\n"
            "                phase_trace.document_completed()\n"
            "                trim_process_heap()\n"
            "                return middle_json, model_list\n\n"
            "        infer_start = time.time()\n",
            count=2,
            occurrence=1,
            label="Hybrid asynchronous capacity branch",
        )
        source = _replace_exact(
            source,
            "            for window_index, window_start in enumerate(range(0, page_count, effective_window_size or 1)):\n"
            "                window_end = min(page_count - 1, window_start + effective_window_size - 1)\n",
            "            for window_index, window_start in enumerate(range(0, page_count, effective_window_size or 1)):\n"
            "                window_end = min(page_count - 1, window_start + effective_window_size - 1)\n"
            "                window_trace_context = phase_trace.window(\n"
            "                    window_index=window_index,\n"
            "                    page_start=window_start,\n"
            "                    page_end_exclusive=window_end + 1,\n"
            "                )\n"
            "                window_started_ns = phase_trace.start()\n"
            "                render_started_ns = phase_trace.start()\n",
            count=2,
            label="Hybrid window phase start",
        )
        source = _replace_exact(
            source,
            "                images_list = load_images_from_pdf_doc(\n"
            "                    pdf_doc,\n"
            "                    start_page_id=window_start,\n"
            "                    end_page_id=window_end,\n"
            "                    image_type=ImageType.PIL,\n"
            "                    pdf_bytes=pdf_bytes,\n"
            "                )\n"
            "                try:\n",
            "                images_list = load_images_from_pdf_doc(\n"
            "                    pdf_doc,\n"
            "                    start_page_id=window_start,\n"
            "                    end_page_id=window_end,\n"
            "                    image_type=ImageType.PIL,\n"
            "                    pdf_bytes=pdf_bytes,\n"
            "                )\n"
            "                phase_trace.complete(\n"
            "                    \"window_render\",\n"
            "                    render_started_ns,\n"
            "                    window=window_trace_context,\n"
            "                )\n"
            "                try:\n",
            count=1,
            label="Hybrid synchronous render phase",
        )
        source = _replace_exact(
            source,
            "                images_list = await aio_load_images_from_pdf_bytes_range(\n"
            "                    pdf_bytes,\n"
            "                    start_page_id=window_start,\n"
            "                    end_page_id=window_end,\n"
            "                    image_type=ImageType.PIL,\n"
            "                )\n"
            "                try:\n",
            "                images_list = await aio_load_images_from_pdf_bytes_range(\n"
            "                    pdf_bytes,\n"
            "                    start_page_id=window_start,\n"
            "                    end_page_id=window_end,\n"
            "                    image_type=ImageType.PIL,\n"
            "                )\n"
            "                phase_trace.complete(\n"
            "                    \"window_render\",\n"
            "                    render_started_ns,\n"
            "                    window=window_trace_context,\n"
            "                )\n"
            "                try:\n",
            count=1,
            label="Hybrid asynchronous render phase",
        )
        source = _replace_exact(
            source,
            "                    images_layout_res, hybrid_pipeline_model = _predict_layout_for_window(\n",
            "                    layout_started_ns = phase_trace.start()\n"
            "                    images_layout_res, hybrid_pipeline_model = _predict_layout_for_window(\n",
            count=1,
            label="Hybrid synchronous layout phase start",
        )
        source = _replace_exact(
            source,
            "                        _ocr_enable,\n"
            "                    )\n"
            "                    if effort == \"medium\":\n",
            "                        _ocr_enable,\n"
            "                    )\n"
            "                    phase_trace.complete(\n"
            "                        \"window_layout\",\n"
            "                        layout_started_ns,\n"
            "                        window=window_trace_context,\n"
            "                    )\n"
            "                    if effort == \"medium\":\n",
            count=2,
            label="Hybrid layout phase end",
        )
        source = _replace_exact(
            source,
            "                    images_layout_res, hybrid_pipeline_model = await asyncio.to_thread(\n",
            "                    layout_started_ns = phase_trace.start()\n"
            "                    images_layout_res, hybrid_pipeline_model = await asyncio.to_thread(\n",
            count=1,
            label="Hybrid asynchronous layout phase start",
        )
        source = _replace_exact(
            source,
            "                    append_page_model_list_to_middle_json(\n"
            "                        middle_json,\n",
            "                    append_started_ns = phase_trace.start()\n"
            "                    append_page_model_list_to_middle_json(\n"
            "                        middle_json,\n",
            count=2,
            label="Hybrid append phase start",
        )
        source = _replace_exact(
            source,
            "                        progress_bar=progress_bar,\n"
            "                    )\n"
            "                    last_append_end_time = time.time()\n"
            "                finally:\n"
            "                    _close_images(images_list)\n",
            "                        progress_bar=progress_bar,\n"
            "                    )\n"
            "                    phase_trace.complete(\n"
            "                        \"window_append\",\n"
            "                        append_started_ns,\n"
            "                        window=window_trace_context,\n"
            "                        append_index=window_index,\n"
            "                    )\n"
            "                    last_append_end_time = time.time()\n"
            "                finally:\n"
            "                    _close_images(images_list)\n"
            "                    trim_process_heap()\n"
            "                    phase_trace.complete(\n"
            "                        \"window_total\",\n"
            "                        window_started_ns,\n"
            "                        window=window_trace_context,\n"
            "                    )\n",
            count=2,
            label="Hybrid append and window completion",
        )
        source = _replace_exact(
            source,
            "        if client_side_output_generation:\n"
            "            apply_server_side_postprocess(\n",
            "        finalize_started_ns = phase_trace.start()\n"
            "        if client_side_output_generation:\n"
            "            apply_server_side_postprocess(\n",
            count=1,
            label="Hybrid synchronous finalize phase start",
        )
        source = _replace_exact(
            source,
            "        if client_side_output_generation:\n"
            "            await asyncio.to_thread(\n",
            "        finalize_started_ns = phase_trace.start()\n"
            "        if client_side_output_generation:\n"
            "            await asyncio.to_thread(\n",
            count=1,
            label="Hybrid asynchronous finalize phase start",
        )
        source = _replace_exact(
            source,
            "        close_pdfium_document(pdf_doc)\n"
            "        doc_closed = True\n"
            "        clean_memory(device)\n"
            "        return middle_json, model_list\n",
            "        phase_trace.complete(\"document_finalize\", finalize_started_ns)\n"
            "        close_pdfium_document(pdf_doc)\n"
            "        doc_closed = True\n"
            "        clean_memory(device)\n"
            "        phase_trace.document_completed()\n"
            "        trim_process_heap()\n"
            "        return middle_json, model_list\n",
            count=2,
            label="Hybrid document completion",
        )
        return _replace_exact(
            source,
            "    finally:\n"
            "        if not doc_closed:\n"
            "            close_pdfium_document(pdf_doc)\n",
            "    finally:\n"
            "        if not doc_closed:\n"
            "            if phase_trace is not None:\n"
            "                phase_trace.document_failed()\n"
            "            close_pdfium_document(pdf_doc)\n",
            count=2,
            label="Hybrid document failure",
        )

    raise ValueError(f"unapproved MinerU compatibility target: {relative_path}")


def apply_patch(
    *,
    site_packages: Path = SITE_PACKAGES,
    marker_path: Path = MARKER_PATH,
) -> dict[str, object]:
    """Verify all preimages, patch atomically per file, and emit one marker."""

    if metadata.version("mineru") != MINERU_VERSION:
        raise RuntimeError(f"MinerU must be exactly {MINERU_VERSION}")
    original: dict[str, bytes] = {}
    for relative_path, expected in TARGET_PREIMAGE_SHA256.items():
        payload = (site_packages / relative_path).read_bytes()
        observed = hashlib.sha256(payload).hexdigest()
        if observed != expected:
            raise RuntimeError(
                f"{relative_path} preimage drifted: expected {expected}, got {observed}"
            )
        original[relative_path] = payload

    patched: dict[str, bytes] = {}
    for relative_path, payload in original.items():
        text = payload.decode("utf-8")
        updated = patch_source(relative_path, text).encode("utf-8")
        if updated == payload:
            raise RuntimeError(f"{relative_path} patch made no change")
        patched[relative_path] = updated

    for relative_path, payload in patched.items():
        path = site_packages / relative_path
        path.write_bytes(payload)
        py_compile.compile(str(path), doraise=True)

    patcher_sha256 = _sha256(Path(__file__).read_bytes())
    marker: dict[str, object] = {
        "schema": "mineru-runtime-compatibility.v2",
        "policy": POLICY,
        "capacity_policy": CAPACITY_POLICY,
        "mineru_version": MINERU_VERSION,
        "base_image_digest": BASE_IMAGE_DIGEST,
        "patcher_sha256": patcher_sha256,
        "preimage_sha256": {
            path: "sha256:" + digest
            for path, digest in sorted(TARGET_PREIMAGE_SHA256.items())
        },
        "patched_source_sha256": {
            path: _sha256(payload) for path, payload in sorted(patched.items())
        },
    }
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return marker


if __name__ == "__main__":
    print(json.dumps(apply_patch(), sort_keys=True, separators=(",", ":")))
