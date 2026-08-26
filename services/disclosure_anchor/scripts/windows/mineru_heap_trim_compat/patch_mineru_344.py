#!/usr/bin/env python3
"""Build-time, exact-source compatibility patch for MinerU 3.4.4 RSS retention.

The patch is intentionally narrower than the open upstream proposal: it adds a
single explicit, fail-visible glibc ``malloc_trim(0)`` hook and calls it after
each VLM/Hybrid processing window plus the document-final cleanup boundary.
Every source file must match the deployed 3.4.4 bytes before any write occurs.
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
SITE_PACKAGES: Final = Path("/usr/local/lib/python3.12/dist-packages")
MARKER_PATH: Final = Path(
    "/opt/agent-invest/mineru-heap-return-v1/compatibility.json"
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


def patch_source(relative_path: str, source: str) -> str:
    """Return the deterministic patched source for one exact MinerU module."""

    if relative_path == "mineru/utils/model_utils.py":
        source = _replace_exact(
            source,
            "import math\nimport os\nimport time\nimport gc\n",
            "import ctypes\n"
            "from functools import lru_cache\n"
            "import math\n"
            "import os\n"
            "import sys\n"
            "import time\n"
            "import gc\n",
            count=1,
            label="model-utils imports",
        )
        helper = '''def is_heap_trim_enabled() -> bool:
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
            "from ...utils.model_utils import trim_process_heap\n\n"
            "from ...utils.enum_class import ImageType\n",
            count=1,
            label="VLM import",
        )
        source = _replace_exact(
            source,
            "                finally:\n                    _close_images(images_list)\n",
            "                finally:\n"
            "                    _close_images(images_list)\n"
            "                    trim_process_heap()\n",
            count=2,
            label="VLM window cleanup",
        )
        return _replace_exact(
            source,
            "        doc_closed = True\n        return middle_json, results\n",
            "        doc_closed = True\n"
            "        trim_process_heap()\n"
            "        return middle_json, results\n",
            count=2,
            label="VLM document cleanup",
        )

    if relative_path == "mineru/backend/hybrid/hybrid_analyze.py":
        source = _replace_exact(
            source,
            "from mineru.utils.model_utils import clean_memory, crop_img, get_vram\n",
            "from mineru.utils.model_utils import (\n"
            "    clean_memory,\n"
            "    crop_img,\n"
            "    get_vram,\n"
            "    trim_process_heap,\n"
            ")\n",
            count=1,
            label="Hybrid import",
        )
        source = _replace_exact(
            source,
            "                finally:\n                    _close_images(images_list)\n",
            "                finally:\n"
            "                    _close_images(images_list)\n"
            "                    trim_process_heap()\n",
            count=2,
            label="Hybrid window cleanup",
        )
        return _replace_exact(
            source,
            "        clean_memory(device)\n        return middle_json, model_list\n",
            "        clean_memory(device)\n"
            "        trim_process_heap()\n"
            "        return middle_json, model_list\n",
            count=2,
            label="Hybrid document cleanup",
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
        "schema": "mineru-heap-return-compatibility.v1",
        "policy": POLICY,
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
