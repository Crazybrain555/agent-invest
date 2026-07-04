"""Filesystem-backed immutable raw document archive."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from disclosure_anchor.application.ports.file_store import (
    QuarantineResult,
    RawDocumentVerification,
    RawDocumentWriteResult,
)
from disclosure_anchor.application.ports.file_store import FileStorePathPort
from disclosure_anchor.domain.errors import InvalidRawDocumentError, RawDocumentError
from disclosure_anchor.domain.ids import new_ulid
from disclosure_anchor.domain.value_objects import QuarantineReason


_CHUNK_SIZE = 1024 * 1024


def _digest_from_hash(raw_file_hash: str) -> str:
    return (raw_file_hash.partition(":")[2] or raw_file_hash).lower()


def _same_hash(left: str, right: str) -> bool:
    return _digest_from_hash(left) == _digest_from_hash(right)


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
            byte_count += len(chunk)
    return f"sha256:{digest.hexdigest()}", byte_count


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class RawDocumentStore:
    """Store original PDF bytes under controlled relative paths."""

    def __init__(self, path_builder: FileStorePathPort) -> None:
        self._paths = path_builder

    def put_raw_document(
        self,
        *,
        provider: str,
        security_code: str,
        year: int | str,
        provider_document_id: str,
        input_file: Path,
        expected_raw_file_hash: str | None = None,
    ) -> RawDocumentWriteResult:
        try:
            if not input_file.is_file():
                raise InvalidRawDocumentError(f"input file is not readable: {input_file}")
            raw_file_hash, byte_count = _hash_file(input_file)
        except OSError as exc:
            raise InvalidRawDocumentError(
                f"input file is not readable: {input_file}: {exc}"
            ) from exc

        if expected_raw_file_hash and not _same_hash(raw_file_hash, expected_raw_file_hash):
            raise InvalidRawDocumentError(
                f"raw hash mismatch: expected {expected_raw_file_hash}, got {raw_file_hash}"
            )
        relpath = self._paths.raw_document_relpath(
            provider=provider,
            security_code=security_code,
            year=year,
            provider_document_id=provider_document_id,
            raw_file_hash=raw_file_hash,
        )
        final_path = self._paths.data_path(relpath)

        if final_path.exists():
            existing_hash, existing_size = _hash_file(final_path)
            if existing_hash != raw_file_hash:
                raise RawDocumentError(f"existing raw document hash mismatch: {relpath}")
            return RawDocumentWriteResult(
                relpath=relpath,
                raw_file_hash=raw_file_hash,
                byte_count=existing_size,
                created=False,
            )

        tmp_path = self._paths.runtime_tmp_path(
            f"raw_{provider_document_id}_{new_ulid()}.tmp"
        )
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            first_bytes = b""
            try:
                with input_file.open("rb") as src, tmp_path.open("xb") as dst:
                    for chunk in iter(lambda: src.read(_CHUNK_SIZE), b""):
                        if len(first_bytes) < 5:
                            first_bytes += chunk[: 5 - len(first_bytes)]
                        dst.write(chunk)
                    dst.flush()
                    os.fsync(dst.fileno())
            except OSError as exc:
                raise InvalidRawDocumentError(
                    f"input file could not be archived: {input_file}: {exc}"
                ) from exc

            if not first_bytes.startswith(b"%PDF-"):
                raise InvalidRawDocumentError(f"input file is not a PDF: {input_file}")

            tmp_hash, tmp_size = _hash_file(tmp_path)
            if tmp_hash != raw_file_hash or tmp_size != byte_count:
                raise RawDocumentError("raw document changed while being archived")

            try:
                os.link(tmp_path, final_path)
            except FileExistsError:
                existing_hash, existing_size = _hash_file(final_path)
                if existing_hash != raw_file_hash:
                    raise RawDocumentError(
                        f"existing raw document hash mismatch: {relpath}"
                    ) from None
                return RawDocumentWriteResult(
                    relpath=relpath,
                    raw_file_hash=raw_file_hash,
                    byte_count=existing_size,
                    created=False,
                )
            _fsync_dir(final_path.parent)
            return RawDocumentWriteResult(
                relpath=relpath,
                raw_file_hash=raw_file_hash,
                byte_count=byte_count,
                created=True,
            )
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def verify_raw_document(
        self, *, relpath: Path, expected_hash: str
    ) -> RawDocumentVerification:
        path = self._paths.data_path(relpath)
        if not path.is_file():
            return RawDocumentVerification(
                relpath=relpath,
                expected_hash=expected_hash,
                actual_hash=None,
                ok=False,
                message="missing raw file",
            )
        try:
            actual_hash, _ = _hash_file(path)
        except OSError as exc:
            return RawDocumentVerification(
                relpath=relpath,
                expected_hash=expected_hash,
                actual_hash=None,
                ok=False,
                message=f"raw file is not readable: {exc}",
            )
        ok = actual_hash == expected_hash
        return RawDocumentVerification(
            relpath=relpath,
            expected_hash=expected_hash,
            actual_hash=actual_hash,
            ok=ok,
            message="ok" if ok else "raw hash mismatch",
        )

    def quarantine_raw_document(
        self,
        *,
        provider: str,
        provider_document_id: str,
        input_file: Path,
        reason: QuarantineReason,
    ) -> QuarantineResult:
        copy_error = None
        if not input_file.exists():
            payload = b""
        else:
            try:
                payload = input_file.read_bytes()
            except OSError as exc:
                payload = b""
                copy_error = str(exc)

        suffix = input_file.suffix.lower() if input_file.suffix else ".bin"
        name = f"{new_ulid()}_{reason}{suffix}"
        quarantine_path = self._paths.runtime_quarantine_path(
            provider=provider,
            provider_document_id=provider_document_id,
            name=name,
        )
        quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        quarantine_path.write_bytes(payload)

        manifest_path = quarantine_path.with_suffix(quarantine_path.suffix + ".json")
        manifest = {
            "provider": provider,
            "provider_document_id": provider_document_id,
            "reason": reason,
            "original_path": str(input_file),
            "byte_count": len(payload),
            "copy_error": copy_error,
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        return QuarantineResult(
            path=quarantine_path,
            reason=reason,
            byte_count=len(payload),
        )
