"""Exact source identity for the MinerU capacity commissioning evaluator."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
from typing import Final


EVALUATOR_BUNDLE_SCHEMA: Final = "mineru-capacity-commissioning-evaluator.v1"
EVALUATOR_COMPONENT_PATHS: Final = (
    "scripts/collect_mineru_phase_trace.py",
    "scripts/evaluate_mineru_capacity_commissioning.py",
    "scripts/mineru_staged_load.py",
    "src/disclosure_anchor/adapters/runtime/bounded_http.py",
    "src/disclosure_anchor/adapters/runtime/mineru_capacity_commissioning.py",
    "src/disclosure_anchor/adapters/runtime/mineru_capacity_evaluator_identity.py",
    "src/disclosure_anchor/adapters/runtime/mineru_deployment_gate.py",
    "src/disclosure_anchor/adapters/runtime/mineru_identity.py",
    "src/disclosure_anchor/adapters/runtime/mineru_phase_trace.py",
    "src/disclosure_anchor/adapters/runtime/mineru_phase_trace_capture.py",
)
_MAX_COMPONENT_BYTES = 32 * 1024 * 1024


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _component_bytes(path: Path, *, label: str) -> bytes:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or metadata.st_size > _MAX_COMPONENT_BYTES
    ):
        raise ValueError(f"{label} must be one bounded regular file")
    payload = path.read_bytes()
    if len(payload) != metadata.st_size:
        raise ValueError(f"{label} changed while reading")
    return payload


def commissioning_evaluator_identity(
    *, service_root: Path | None = None
) -> dict[str, object]:
    """Return the exact current source bundle that can authorize Auto."""

    root = service_root or Path(__file__).resolve().parents[4]
    components = [
        {
            "path": relative,
            "sha256": _sha256(
                _component_bytes(
                    root / relative,
                    label=f"evaluator component {relative}",
                )
            ),
        }
        for relative in EVALUATOR_COMPONENT_PATHS
    ]
    bundle = {
        "components": components,
        "schema": EVALUATOR_BUNDLE_SCHEMA,
    }
    return {
        **bundle,
        "bundle_sha256": _sha256(
            json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ),
    }


__all__ = [
    "EVALUATOR_BUNDLE_SCHEMA",
    "EVALUATOR_COMPONENT_PATHS",
    "commissioning_evaluator_identity",
]
