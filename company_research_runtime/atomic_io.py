# -*- coding: utf-8 -*-
"""Atomic file writers for runtime artifacts."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterable

import yaml


def ensure_parent_dir(path: Path) -> None:
    """Ensure parent directory exists."""
    path.parent.mkdir(parents=True, exist_ok=True)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    ensure_parent_dir(path)
    tmp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    with open(tmp_path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def atomic_write_bytes(path: str | Path, data: bytes) -> Path:
    target = Path(path)
    _atomic_write_bytes(target, data)
    return target


def atomic_write_text(path: str | Path, text: str) -> Path:
    return atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_yaml(
    path: str | Path,
    payload: Any,
    *,
    sort_keys: bool = False,
) -> Path:
    text = yaml.safe_dump(payload, sort_keys=sort_keys, allow_unicode=True)
    return atomic_write_text(path, text)


def _json_dumps(
    payload: Any,
    *,
    ensure_ascii: bool,
    sort_keys: bool,
    default: Any | None,
) -> str:
    return json.dumps(
        payload,
        ensure_ascii=ensure_ascii,
        sort_keys=sort_keys,
        default=default,
    )


def atomic_write_json(
    path: str | Path,
    payload: Any,
    *,
    ensure_ascii: bool = False,
    sort_keys: bool = False,
    default: Any | None = None,
) -> Path:
    text = _json_dumps(
        payload,
        ensure_ascii=ensure_ascii,
        sort_keys=sort_keys,
        default=default,
    )
    return atomic_write_text(path, text)


def atomic_write_jsonl(
    path: str | Path,
    records: Iterable[Any],
    *,
    ensure_ascii: bool = False,
    default: Any | None = None,
) -> Path:
    lines = [
        _json_dumps(record, ensure_ascii=ensure_ascii, sort_keys=False, default=default)
        for record in records
    ]
    text = "\n".join(lines)
    if text:
        text += "\n"
    return atomic_write_text(path, text)


def atomic_write_parquet(
    path: str | Path,
    frame: Any,
    *,
    index: bool = False,
    **kwargs: Any,
) -> Path:
    try:
        import pandas as pd  # noqa: F401
    except ImportError as exc:  # pragma: no cover - import guard
        raise ImportError("pandas is required for parquet output") from exc

    target = Path(path)
    ensure_parent_dir(target)
    tmp_path = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    frame.to_parquet(tmp_path, index=index, **kwargs)
    os.replace(tmp_path, target)
    return target
