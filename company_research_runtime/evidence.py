# -*- coding: utf-8 -*-
"""Append-only ledgers for evidence and questions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .atomic_io import ensure_parent_dir


def ensure_jsonl(path: str | Path) -> Path:
    target = Path(path)
    ensure_parent_dir(target)
    if not target.exists():
        target.write_text("", encoding="utf-8")
    return target


def append_jsonl(
    path: str | Path,
    record: Mapping[str, Any],
    *,
    ensure_ascii: bool = False,
    default: Any | None = str,
) -> Path:
    target = ensure_jsonl(path)
    line = json.dumps(record, ensure_ascii=ensure_ascii, default=default)
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")
    return target


def append_records(
    path: str | Path,
    records: Iterable[Mapping[str, Any]],
    *,
    ensure_ascii: bool = False,
    default: Any | None = str,
) -> Path:
    target = ensure_jsonl(path)
    with open(target, "a", encoding="utf-8") as handle:
        for record in records:
            line = json.dumps(record, ensure_ascii=ensure_ascii, default=default)
            handle.write(line)
            handle.write("\n")
    return target


def append_evidence(path: str | Path, record: Mapping[str, Any]) -> Path:
    return append_jsonl(path, record)


def append_question(path: str | Path, record: Mapping[str, Any]) -> Path:
    return append_jsonl(path, record)
