"""Controlled filesystem reads for provider-document source admission."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import os
from pathlib import Path
import stat

from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import (
    MinerUMediumArtifactReader,
)
from disclosure_anchor.adapters.parsers.pdf_page_probe import count_pdf_pages
from disclosure_anchor.application.contracts.provider_document import ProviderDocument
from disclosure_anchor.application.contracts.provider_document_admission import (
    SourcePdfObservation,
)
from disclosure_anchor.application.ports.file_store import FileStorePathPort
from disclosure_anchor.application.ports.provider_document_source import (
    ProviderDocumentSourceError,
)
from disclosure_anchor.domain.errors import ParserOutputContractError, PathSafetyError


_CHUNK_SIZE = 1024 * 1024


class ProviderDocumentFileSource:
    """Read provider records, PDFs, and bundles beneath the controlled data root."""

    def __init__(
        self,
        path_builder: FileStorePathPort,
        *,
        reader: MinerUMediumArtifactReader | None = None,
        page_counter: Callable[[Path], int] = count_pdf_pages,
    ) -> None:
        self._paths = path_builder
        self._reader = reader or MinerUMediumArtifactReader()
        self._page_counter = page_counter

    def read_provider_document_record(self, relpath: Path) -> bytes:
        try:
            return _read_regular_file(self._checked_file(relpath))
        except (OSError, PathSafetyError) as exc:
            raise ProviderDocumentSourceError(
                "provider_document_read_failed",
                f"cannot read provider document record: {exc}",
                retryable=isinstance(exc, OSError),
            ) from exc

    def observe_source_pdf(self, relpath: Path) -> SourcePdfObservation:
        try:
            path = self._checked_file(relpath)
            first_hash = _hash_regular_file(path)
            if _read_regular_prefix(path, size=5) != b"%PDF-":
                raise ValueError("source file has no PDF signature")
            page_count = self._page_counter(path)
            second_hash = _hash_regular_file(self._checked_file(relpath))
        except (OSError, PathSafetyError, RuntimeError, ValueError) as exc:
            raise ProviderDocumentSourceError(
                "source_pdf_read_failed",
                f"cannot inspect source PDF: {exc}",
                retryable=isinstance(exc, OSError),
            ) from exc
        if first_hash != second_hash:
            raise ProviderDocumentSourceError(
                "source_pdf_changed",
                "source PDF changed while it was inspected",
                retryable=False,
            )
        try:
            return SourcePdfObservation(sha256=first_hash, page_count=page_count)
        except ValueError as exc:
            raise ProviderDocumentSourceError(
                "source_pdf_read_failed",
                f"source PDF observation is invalid: {exc}",
                retryable=False,
            ) from exc

    def rebuild_provider_document(
        self,
        bundle_relpath: Path,
        *,
        source_pdf_sha256: str,
    ) -> ProviderDocument:
        try:
            bundle_path = self._checked_directory(bundle_relpath)
            return self._reader.read(
                bundle_path,
                source_pdf_sha256=source_pdf_sha256,
            )
        except ParserOutputContractError as exc:
            raise ProviderDocumentSourceError(
                "provider_bundle_invalid",
                str(exc),
                retryable=False,
            ) from exc
        except (OSError, PathSafetyError) as exc:
            raise ProviderDocumentSourceError(
                "provider_bundle_read_failed",
                f"cannot read provider bundle: {exc}",
                retryable=isinstance(exc, OSError),
            ) from exc

    def _checked_file(self, relpath: Path) -> Path:
        path = self._checked_path(relpath)
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise PathSafetyError(f"data path is not a regular file: {relpath}")
        return path

    def _checked_directory(self, relpath: Path) -> Path:
        path = self._checked_path(relpath)
        mode = path.lstat().st_mode
        if not stat.S_ISDIR(mode):
            raise PathSafetyError(f"data path is not a directory: {relpath}")
        return path

    def _checked_path(self, relpath: Path) -> Path:
        if relpath.is_absolute() or not relpath.parts or ".." in relpath.parts:
            raise PathSafetyError(f"data path is not a safe relative path: {relpath}")
        path = self._paths.data_path(relpath)
        cursor = path
        for offset in range(len(relpath.parts)):
            mode = cursor.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise PathSafetyError(f"data path contains a symlink: {relpath}")
            if offset > 0 and not stat.S_ISDIR(mode):
                raise PathSafetyError(
                    f"data path parent is not a directory: {relpath}"
                )
            cursor = cursor.parent
        return path


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"path is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def _hash_regular_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"path is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
    finally:
        os.close(descriptor)
    return "sha256:" + digest.hexdigest()


def _read_regular_prefix(path: Path, *, size: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"path is not a regular file: {path}")
        return os.read(descriptor, size)
    finally:
        os.close(descriptor)


__all__ = ["ProviderDocumentFileSource"]
