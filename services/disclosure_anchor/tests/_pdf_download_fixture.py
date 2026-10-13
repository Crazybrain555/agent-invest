"""Synthetic PDF-download bodies and sinks for the streaming acquisition tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx

from disclosure_anchor.adapters.storage.raw_document_store import VolumeSpace
from disclosure_anchor.application.ports.file_store import AcquisitionCapacityError
from disclosure_anchor.settings import Settings


def acquisition_settings(root: Path) -> Settings:
    data_root = root / "services" / "disclosure_anchor"
    shared_root = root / "shared"
    return Settings(
        disclosure_data_root=data_root,
        disclosure_shared_root=shared_root,
        disclosure_runtime_root=data_root / "runtime",
        mineru_model_cache=shared_root / "model_cache" / "mineru",
        hf_home=shared_root / "model_cache" / "huggingface",
        modelscope_cache=shared_root / "model_cache" / "modelscope",
    )


class DirectoryVolume:
    """Synthetic volume: free space shrinks by what the watched tree holds."""

    def __init__(self, watched: Path, *, free: int, total: int = 1 << 40) -> None:
        self.watched = watched
        self.free = free
        self.total = total

    def probe(self, _directory: Path) -> VolumeSpace:
        used = sum(
            path.stat().st_size for path in self.watched.rglob("*") if path.is_file()
        )
        return VolumeSpace(available_bytes=self.free - used, total_bytes=self.total)


class RecordingSink:
    """Keeps every attempt apart so restarts and appends stay visible."""

    def __init__(self, *, refuse_writes: bool = False) -> None:
        self.attempts: list[bytearray] = []
        self.declared: list[int | None] = []
        self.refuse_writes = refuse_writes

    def begin_attempt(self, *, declared_byte_count: int | None) -> None:
        self.declared.append(declared_byte_count)
        self.attempts.append(bytearray())

    def write(self, chunk: bytes) -> None:
        if self.refuse_writes:
            raise AcquisitionCapacityError(
                phase="download", required_bytes=len(chunk), available_bytes=0, floor_bytes=0
            )
        self.attempts[-1].extend(chunk)


class ChunkStream(httpx.SyncByteStream):
    """A response body delivered in chunks; ``fail_at`` resets it mid-body."""

    def __init__(self, *chunks: bytes, fail_at: int | None = None) -> None:
        self.chunks = chunks
        self.fail_at = fail_at
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for index, chunk in enumerate(self.chunks):
            if index == self.fail_at:
                raise httpx.ReadError("connection reset mid-body")
            yield chunk

    def close(self) -> None:
        self.closed = True
