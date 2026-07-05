"""Atomic writer for derived disclosure artifacts."""

from __future__ import annotations

from collections.abc import Sequence

import hashlib
import json
import os
from pathlib import Path

from disclosure_anchor.application.ports.file_store import (
    ArtifactWriteResult,
    FileStorePathPort,
)
from disclosure_anchor.domain.ids import new_ulid


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ArtifactStore:
    """Write derived artifacts under the data root with atomic replacement."""

    def __init__(self, path_builder: FileStorePathPort) -> None:
        self._paths = path_builder

    def write_json_atomic(self, *, relpath: Path, payload: object) -> ArtifactWriteResult:
        raw = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        return self._write_bytes_atomic(relpath=relpath, payload=raw)

    def write_jsonl_atomic(
        self, *, relpath: Path, rows: Sequence[object]
    ) -> ArtifactWriteResult:
        raw = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        ).encode("utf-8")
        return self._write_bytes_atomic(relpath=relpath, payload=raw)

    def write_text_atomic(self, *, relpath: Path, text: str) -> ArtifactWriteResult:
        return self._write_bytes_atomic(relpath=relpath, payload=text.encode("utf-8"))

    def _write_bytes_atomic(self, *, relpath: Path, payload: bytes) -> ArtifactWriteResult:
        final_path = self._paths.data_path(relpath)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = final_path.with_name(f".{final_path.name}.{new_ulid()}.tmp")
        try:
            with tmp_path.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, final_path)
            _fsync_dir(final_path.parent)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

        return ArtifactWriteResult(
            relpath=relpath,
            artifact_hash="sha256:" + hashlib.sha256(payload).hexdigest(),
            byte_count=len(payload),
        )
