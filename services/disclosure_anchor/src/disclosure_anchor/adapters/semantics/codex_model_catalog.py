"""Verified, immutable Codex model metadata for tool-free classification."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat


CATALOG_DIRNAME = Path("semantic/codex_model_catalogs")
NEUTRAL_TOOL_FIELDS: dict[str, object] = {
    "tool_mode": "direct",
    "shell_type": "disabled",
    "apply_patch_tool_type": None,
    "experimental_supported_tools": [],
    "supports_search_tool": False,
}
_MAX_CATALOG_BYTES = 8 * 1024 * 1024


class _ClosedJsonError(ValueError):
    pass


def _closed_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise _ClosedJsonError(f"duplicate key: {key}")
        payload[key] = value
    return payload


def _reject_json_constant(value: str) -> object:
    raise _ClosedJsonError(f"invalid constant: {value}")


def _strict_json_loads(raw: str) -> object:
    return json.loads(raw, object_pairs_hook=_closed_json_object, parse_constant=_reject_json_constant)


def _entries(raw: bytes) -> list[dict[str, object]]:
    if not raw or len(raw) > _MAX_CATALOG_BYTES:
        raise ValueError("Codex model catalog size is invalid")
    try:
        value = _strict_json_loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Codex model catalog is not closed JSON") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"models"}
        or not isinstance(value["models"], list)
        or not all(isinstance(item, dict) for item in value["models"])
    ):
        raise ValueError("Codex model catalog shape is invalid")
    return value["models"]


def neutralize_bundled_catalog(bundled: bytes, *, model: str) -> bytes:
    """Preserve the exact bundled entry apart from its five tool-surface fields."""

    matches = [item for item in _entries(bundled) if item.get("slug") == model]
    if len(matches) != 1:
        raise ValueError("Codex bundled catalog must contain one exact model")
    entry = matches[0]
    if not NEUTRAL_TOOL_FIELDS.keys() <= entry.keys():
        raise ValueError("Codex bundled tool fields changed; re-qualification required")
    entry.update(NEUTRAL_TOOL_FIELDS)
    return json.dumps(
        {"models": [entry]}, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class CodexModelCatalog:
    model: str
    sha256: str
    raw: bytes

    def __post_init__(self) -> None:
        if self.sha256 != "sha256:" + hashlib.sha256(self.raw).hexdigest():
            raise ValueError("Codex model catalog hash mismatch")
        entries = _entries(self.raw)
        if len(entries) != 1 or entries[0].get("slug") != self.model:
            raise ValueError("Codex model catalog identity mismatch")
        for key, expected in NEUTRAL_TOOL_FIELDS.items():
            value = entries[0].get(key)
            if key not in entries[0] or type(value) is not type(expected) or value != expected:
                raise ValueError("Codex model catalog exposes a tool surface")


def codex_model_catalog_path(runtime_root: Path, sha256: str) -> Path:
    if re.fullmatch(r"sha256:[0-9a-f]{64}", sha256) is None:
        raise ValueError("Codex model catalog hash is invalid")
    return runtime_root / CATALOG_DIRNAME / f"{sha256[7:]}.json"


def load_codex_model_catalog(runtime_root: Path, *, sha256: str, model: str) -> CodexModelCatalog:
    path = codex_model_catalog_path(runtime_root, sha256)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_CATALOG_BYTES:
                raise ValueError("Codex model catalog must be a bounded regular file")
            raw = handle.read(_MAX_CATALOG_BYTES + 1)
    except OSError as exc:
        raise ValueError("Codex model catalog is missing or unreadable; prepare it before startup") from exc
    return CodexModelCatalog(model=model, sha256=sha256, raw=raw)
