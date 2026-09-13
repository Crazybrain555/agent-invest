"""Bind a single explicit capacity file to MinerU's existing startup settings.

The installer supplies the two imported modules as the exact Mac codec and file
reader bytes. This bootstrap neither mutates the environment nor creates model,
pool, semaphore, or event-loop objects. Those are checked at their actual owners.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
from threading import Lock

from mineru.cli.agent_capacity_config import (
    MineruCapacityConfig,
    decode_mineru_capacity_config,
    encode_mineru_capacity_config,
)
from mineru.cli.agent_capacity_file import read_mineru_capacity_file


_PATH_VARIABLE = "MINERU_CAPACITY_CONFIG_PATH"
_HASH_VARIABLE = "MINERU_CAPACITY_CONFIG_SHA256"
_PROCESS_LOCK = Lock()
_PROCESS_INITIALIZED = False
_PROCESS_ID: int | None = None
_PROCESS_CONFIG: MineruCapacityConfig | None = None
_PROCESS_ANCHORS: tuple[str | None, str | None] | None = None
_ENVIRONMENT_FIELDS = {
    "MINERU_API_MAX_CONCURRENT_REQUESTS": "parse_active_limit",
    "MINERU_API_MAX_PENDING_TASKS": "total_nonterminal_limit",
    "MINERU_API_FINALIZER_SLOTS": "finalizer_active_limit",
    "MINERU_PROCESSING_WINDOW_SIZE": "processing_window_size",
    "OMP_NUM_THREADS": "omp_num_threads",
    "MKL_NUM_THREADS": "mkl_num_threads",
    "OPENBLAS_NUM_THREADS": "openblas_num_threads",
    "MINERU_PDF_RENDER_THREADS": "pdf_render_processes_requested",
    "MINERU_HYBRID_BATCH_RATIO": "hybrid_batch_ratio_requested",
    "MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES": "result_reservation_bytes",
    "MINERU_TASK_PROTOCOL_V2_MAX_UNACKED_BYTES": "max_unacked_result_bytes",
}


def capacity_environment(config: MineruCapacityConfig) -> dict[str, str]:
    """Project requested values to their original startup consumers."""

    encode_mineru_capacity_config(config)
    values = {
        variable: str(getattr(config, field))
        for variable, field in _ENVIRONMENT_FIELDS.items()
    }
    values["MINERU_ENABLE_PIPELINE_INFERENCE_LOCKS"] = "1"
    return values


def read_startup_capacity(
    environment: Mapping[str, str],
    *,
    expected_owner_uid: int,
) -> MineruCapacityConfig | None:
    """Select the explicit new configuration or leave the legacy path intact."""

    if _PATH_VARIABLE not in environment and _HASH_VARIABLE not in environment:
        return None
    path = environment.get(_PATH_VARIABLE)
    digest = environment.get(_HASH_VARIABLE)
    if type(path) is not str or not path or type(digest) is not str or not digest:
        raise ValueError("MinerU capacity file path and SHA must both be explicit")
    payload = read_mineru_capacity_file(
        Path(path), expected_sha256=digest, expected_owner_uid=expected_owner_uid,
    )
    config = decode_mineru_capacity_config(payload)
    for variable, expected in capacity_environment(config).items():
        actual = environment.get(variable)
        if type(actual) is not str or actual != expected:
            raise ValueError(f"MinerU capacity config conflicts with {variable}")
    return config


def verify_http_capacity(config: MineruCapacityConfig, requested_limit: int) -> None:
    """Check an actual CLI/client value; H is shared within one serving loop."""

    encode_mineru_capacity_config(config)
    if type(requested_limit) is not int or requested_limit != config.final_http_limit_per_loop:
        raise ValueError("MinerU HTTP concurrency differs from the capacity config")


def get_process_capacity() -> MineruCapacityConfig | None:
    """Load once before model startup; never silently rebind a process or fork."""
    global _PROCESS_INITIALIZED, _PROCESS_ID, _PROCESS_CONFIG, _PROCESS_ANCHORS
    if _PROCESS_INITIALIZED and _PROCESS_ID != os.getpid():
        raise RuntimeError("MinerU capacity cannot reuse an inherited process owner")
    with _PROCESS_LOCK:
        anchors = (os.environ.get(_PATH_VARIABLE), os.environ.get(_HASH_VARIABLE))
        if not _PROCESS_INITIALIZED:
            config = read_startup_capacity(os.environ, expected_owner_uid=os.geteuid())
            if anchors != (os.environ.get(_PATH_VARIABLE), os.environ.get(_HASH_VARIABLE)):
                raise RuntimeError("MinerU capacity startup anchors changed while loading")
            _PROCESS_CONFIG = config
            _PROCESS_ID = os.getpid()
            _PROCESS_ANCHORS = anchors
            _PROCESS_INITIALIZED = True
        if _PROCESS_ID != os.getpid() or anchors != _PROCESS_ANCHORS:
            raise RuntimeError("MinerU capacity process or startup anchors drifted")
        if _PROCESS_CONFIG is not None:
            for variable, expected in capacity_environment(_PROCESS_CONFIG).items():
                if os.environ.get(variable) != expected:
                    raise RuntimeError(f"MinerU capacity startup environment drifted: {variable}")
        return _PROCESS_CONFIG


__all__ = [
    "capacity_environment", "get_process_capacity", "read_startup_capacity",
    "verify_http_capacity",
]
