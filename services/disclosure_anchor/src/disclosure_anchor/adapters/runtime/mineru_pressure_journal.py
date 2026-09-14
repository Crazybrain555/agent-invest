"""Bounded, privately owned raw-pressure evidence for an ordinary resident.

The finite M6 owner uses its own complete evidence sink. This rolling log is
not a substitute for that finite campaign's independently verified receipts.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import tempfile
from threading import Lock
from typing import BinaryIO


class PressureJournal:
    def __init__(self, parent: Path, *, segment_bytes: int = 16 * 1024**2, segments: int = 4) -> None:
        if type(segment_bytes) is not int or segment_bytes < 262144 or type(segments) is not int or not 1 <= segments <= 16:
            raise ValueError("pressure evidence bounds are invalid")
        if not parent.is_absolute():
            raise ValueError("pressure evidence parent must be absolute")
        parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(tempfile.mkdtemp(prefix="owner-", dir=parent))
        self._segment_bytes, self._segments = segment_bytes, segments
        self._lock = Lock()
        self._number = 0
        self._size = 0
        self._file: BinaryIO | None = None
        self._closed = False
        self._open_segment()

    def _open_segment(self) -> None:
        path = self.path / f"pressure-{self._number:08d}.jsonl"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        self._file = os.fdopen(descriptor, "wb")
        self._size = 0
        if self._number >= self._segments:
            (self.path / f"pressure-{self._number-self._segments:08d}.jsonl").unlink()

    def __call__(self, event: Mapping[str, object]) -> None:
        raw = (json.dumps(dict(event), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(raw) > 262144:
            raise ValueError("pressure evidence record exceeds bound")
        with self._lock:
            if self._closed or self._file is None:
                raise RuntimeError("pressure evidence owner is closed")
            if self._size + len(raw) > self._segment_bytes:
                self._file.close()
                self._number += 1
                self._open_segment()
            self._file.write(raw)
            self._file.flush()
            self._size += len(raw)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                if self._file is not None:
                    self._file.close()
