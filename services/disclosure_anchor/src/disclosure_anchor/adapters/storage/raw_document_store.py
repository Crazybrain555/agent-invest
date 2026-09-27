"""Filesystem-backed immutable raw document archive."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import errno
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
from typing import TYPE_CHECKING

from disclosure_anchor.application.ports.disclosure_source import CompletedPdfTransfer
from disclosure_anchor.application.ports.file_store import (
    AcquisitionCapacityError,
    IncompletePdfDownloadError,
    QuarantineResult,
    RawDocumentVerification,
    RawDocumentWriteResult,
    RetainedRawDocument,
    SealedPdfDownload,
)
from disclosure_anchor.application.ports.file_store import FileStorePathPort
from disclosure_anchor.domain.errors import (
    InvalidRawDocumentError,
    PathSafetyError,
    RawDocumentError,
)
from disclosure_anchor.domain.ids import new_ulid
from disclosure_anchor.domain.value_objects import QuarantineReason

if TYPE_CHECKING:
    from disclosure_anchor.settings import Settings


LOGGER = logging.getLogger(__name__)
_CHUNK_SIZE = 1024 * 1024
_RETAINED_NAME_RE = re.compile(r"^sha256_([0-9a-f]{64})\.pdf$")
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_EMPTY_SHA256 = "sha256:" + hashlib.sha256(b"").hexdigest()
# An unset floor keeps 10% of the volume free: the doctor's disk-headroom
# watermark for this volume, which PGDATA shares and the archive only grows.
DEFAULT_FREE_FLOOR_PERCENT = 10


@dataclass(frozen=True)
class VolumeSpace:
    available_bytes: int
    total_bytes: int


def statvfs_space(path: Path) -> VolumeSpace:
    usage = os.statvfs(path)
    return VolumeSpace(
        available_bytes=usage.f_bavail * usage.f_frsize,
        total_bytes=usage.f_blocks * usage.f_frsize,
    )


class FreeSpaceFloor:
    """Live free-space floor of the volume holding ``directory``.

    Every growth step asks the filesystem, so concurrent writers share the
    floor without a ledger and may overshoot it by at most one chunk each.
    It bounds physical growth only; it never limits a document's size.
    """

    def __init__(
        self,
        directory: Path,
        *,
        floor_bytes: int | None,
        probe: Callable[[Path], VolumeSpace] = statvfs_space,
    ) -> None:
        if floor_bytes is not None and floor_bytes < 0:
            raise ValueError("free floor must be non-negative")
        self._directory = directory
        self._floor_bytes = floor_bytes
        self._probe = probe

    def require(self, byte_count: int, *, phase: str) -> None:
        space = self._probe(self._directory)
        floor = self._floor_bytes
        if floor is None:
            floor = (space.total_bytes * DEFAULT_FREE_FLOOR_PERCENT + 99) // 100
        if space.available_bytes - byte_count < floor:
            raise AcquisitionCapacityError(
                phase=phase,
                required_bytes=byte_count,
                available_bytes=space.available_bytes,
                floor_bytes=floor,
            )


class OwnedPdfDownload:
    """One exclusively created download staging file (``PdfDownloadStaging``).

    Each attempt truncates the file and restarts the hash, so a retry never
    appends to an earlier partial; every write first checks the free floor.
    """

    def __init__(self, path: Path, *, floor: FreeSpaceFloor) -> None:
        # Refuse before creating anything: below the floor nothing can land.
        floor.require(0, phase="admission")
        self._path = path
        self._floor = floor
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _O_CLOEXEC,
            0o600,
        )
        try:
            created = os.fstat(fd)
        except BaseException:
            os.close(fd)
            raise
        self._fd: int | None = fd
        self._identity = (created.st_dev, created.st_ino)
        self._digest = hashlib.sha256()
        self._byte_count = 0
        self._attempts = 0
        self._declared_byte_count: int | None = None
        self._sealed: SealedPdfDownload | None = None
        self._removed = False
        self._closed = False

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def byte_count(self) -> int:
        return self._byte_count

    @property
    def declared_byte_count(self) -> int | None:
        return self._declared_byte_count

    def begin_attempt(self, *, declared_byte_count: int | None) -> None:
        fd = self._writable_fd()
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        self._digest = hashlib.sha256()
        self._byte_count = 0
        self._attempts += 1
        self._declared_byte_count = declared_byte_count
        if declared_byte_count is not None:
            # Early refusal only; the bytes actually written are checked too.
            self._floor.require(declared_byte_count, phase="declared_length")

    def write(self, chunk: bytes) -> None:
        fd = self._writable_fd()
        if self._attempts == 0:
            raise RuntimeError("begin_attempt must precede the first body byte")
        if not chunk:
            return
        self._floor.require(len(chunk), phase="download")
        view = memoryview(chunk)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                # A write without progress would otherwise spin forever.
                raise OSError(errno.EIO, "download staging write made no progress")
            view = view[written:]
        self._digest.update(chunk)
        self._byte_count += len(chunk)

    def seal(self, transfer: CompletedPdfTransfer) -> SealedPdfDownload:
        fd = self._writable_fd()
        if transfer.byte_count != self._byte_count or (
            transfer.declared_byte_count is not None
            and transfer.declared_byte_count != self._byte_count
        ):
            raise IncompletePdfDownloadError(
                f"transfer reported {transfer.byte_count} bytes (declared "
                f"{transfer.declared_byte_count}) but its attempt staged "
                f"{self._byte_count}"
            )
        os.fsync(fd)
        written = os.fstat(fd)
        current = os.lstat(self._path)
        if (
            written.st_size != self._byte_count
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != self._identity
        ):
            raise IncompletePdfDownloadError(
                f"staged download changed on disk before sealing: {self._path.name}"
            )
        # A sealed file may be retained as the only copy, so its directory
        # entry must be durable before the seal is handed out.
        _fsync_dir(self._path.parent)
        self._close_fd()
        self._sealed = SealedPdfDownload(
            path=self._path,
            raw_file_hash=f"sha256:{self._digest.hexdigest()}",
            byte_count=self._byte_count,
        )
        return self._sealed

    def discard(self) -> None:
        self._close_fd()
        if self._removed:
            return
        self._removed = True
        try:
            current = os.lstat(self._path)
        except FileNotFoundError:
            return
        # Only ever remove the inode this object created.
        if (current.st_dev, current.st_ino) == self._identity:
            os.unlink(self._path)

    def close(self) -> Path | None:
        """Release the file; remove a partial, retain a sealed undiscarded one."""

        if self._closed:
            return None if self._removed else self._path
        self._closed = True
        if self._sealed is None:
            self.discard()
        if self._removed:
            return None
        LOGGER.warning(
            "retaining sealed download %s: no archive or complete quarantine "
            "copy holds its bytes",
            self._path.name,
        )
        return self._path

    def _writable_fd(self) -> int:
        if self._fd is None:
            raise RuntimeError("download staging file is sealed or closed")
        return self._fd

    def _close_fd(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)


def _digest_from_hash(raw_file_hash: str) -> str:
    return (raw_file_hash.partition(":")[2] or raw_file_hash).lower()


def _same_hash(left: str, right: str) -> bool:
    return _digest_from_hash(left) == _digest_from_hash(right)


def _hash_file(path: Path) -> tuple[str, int]:
    raw_file_hash, byte_count, _ = _hash_file_head(path)
    return raw_file_hash, byte_count


def _hash_file_head(path: Path) -> tuple[str, int, bytes]:
    """Stream-hash ``path`` and keep its first five bytes (the PDF magic)."""

    digest = hashlib.sha256()
    byte_count = 0
    head = b""
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK_SIZE), b""):
            if len(head) < 5:
                head += chunk[: 5 - len(head)]
            digest.update(chunk)
            byte_count += len(chunk)
    return f"sha256:{digest.hexdigest()}", byte_count, head


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_bytes_durable(path: Path, payload: bytes) -> None:
    """tmp + fsync + atomic rename: quarantine evidence must survive a crash
    mid-write just like the immutable archive does (round23)."""

    tmp = path.with_suffix(path.suffix + f".tmp-{new_ulid()}")
    with tmp.open("xb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


class RawDocumentStore:
    """Store original PDF bytes under controlled relative paths."""

    def __init__(
        self,
        path_builder: FileStorePathPort,
        *,
        free_floor_bytes: int | None = None,
        space_probe: Callable[[Path], VolumeSpace] = statvfs_space,
    ) -> None:
        self._paths = path_builder
        self._free_floor_bytes = free_floor_bytes
        self._space_probe = space_probe

    @classmethod
    def from_settings(
        cls, path_builder: FileStorePathPort, settings: Settings
    ) -> RawDocumentStore:
        return cls(
            path_builder,
            free_floor_bytes=settings.disclosure_acquisition_free_floor_bytes,
        )

    def open_pdf_download(self, path: Path) -> OwnedPdfDownload:
        return OwnedPdfDownload(path, floor=self._free_floor(path.parent))

    def _free_floor(self, directory: Path) -> FreeSpaceFloor:
        return FreeSpaceFloor(
            directory, floor_bytes=self._free_floor_bytes, probe=self._space_probe
        )

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
        # Only facts about the input make it invalid: it is absent or not a
        # regular file, has the wrong hash, or is not a PDF, all settled before
        # any archive write. A failed stat/open/read of an existing input, like
        # the archive's own storage failures (free-floor refusal, ENOSPC/EIO on
        # write or fsync), says nothing about its bytes and propagates
        # unchanged, so callers keep it retryable.
        try:
            input_stat = input_file.stat()
        except (FileNotFoundError, NotADirectoryError):
            raise InvalidRawDocumentError(f"input file is not readable: {input_file}") from None
        if not stat.S_ISREG(input_stat.st_mode):
            raise InvalidRawDocumentError(f"input file is not readable: {input_file}")
        raw_file_hash, byte_count, head = _hash_file_head(input_file)

        if expected_raw_file_hash and not _same_hash(raw_file_hash, expected_raw_file_hash):
            raise InvalidRawDocumentError(
                f"raw hash mismatch: expected {expected_raw_file_hash}, got {raw_file_hash}"
            )
        if not head.startswith(b"%PDF-"):
            raise InvalidRawDocumentError(f"input file is not a PDF: {input_file}")
        relpath = self._paths.raw_document_relpath(
            provider=provider,
            security_code=security_code,
            year=year,
            provider_document_id=provider_document_id,
            raw_file_hash=raw_file_hash,
        )
        final_path = self._paths.data_path(relpath)

        if final_path.exists():
            # Already archived under this content address: no copy, no space.
            existing_hash, existing_size = _hash_file(final_path)
            if existing_hash != raw_file_hash:
                raise RawDocumentError(f"existing raw document hash mismatch: {relpath}")
            return RawDocumentWriteResult(
                relpath=relpath,
                raw_file_hash=raw_file_hash,
                byte_count=existing_size,
                created=False,
            )

        tmp_path = self._paths.runtime_tmp_path(f"raw_{new_ulid()}.tmp")
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        floor = self._free_floor(tmp_path.parent)
        # The copy adds byte_count to the volume: refuse it whole when it
        # cannot fit, then recheck before every chunk since other writers
        # share the volume.
        floor.require(byte_count, phase="archive")

        try:
            with input_file.open("rb") as src, tmp_path.open("xb") as dst:
                for chunk in iter(lambda: src.read(_CHUNK_SIZE), b""):
                    floor.require(len(chunk), phase="archive")
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())

            tmp_hash, tmp_size = _hash_file(tmp_path)
            if tmp_hash != raw_file_hash or tmp_size != byte_count:
                raise RawDocumentError("raw document changed while being archived")

            # Nothing enters the archive tree before the verified copy exists.
            final_path.parent.mkdir(parents=True, exist_ok=True)
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

    def locate_retained_raw_document(
        self,
        *,
        provider: str,
        security_code: str,
        year: int | str,
        provider_document_id: str,
    ) -> RetainedRawDocument:
        try:
            directory = self._paths.raw_document_dir_relpath(
                provider=provider,
                security_code=security_code,
                year=year,
                provider_document_id=provider_document_id,
            )
        except PathSafetyError as exc:
            raise RawDocumentError(f"retained archive path is unsafe: {exc}") from exc
        absolute = self._retained_data_path(directory)
        self._require_no_symlink_component(directory)
        try:
            entries = sorted(os.scandir(absolute), key=lambda entry: entry.name)
        except FileNotFoundError:
            raise RawDocumentError(
                f"retained archive directory is missing: {directory}"
            ) from None
        except NotADirectoryError:
            raise RawDocumentError(
                f"retained archive path is not a directory: {directory}"
            ) from None
        names: list[str] = []
        for entry in entries:
            # Only content-addressed archive files may live here; anything
            # else means the directory is not the archive this code wrote.
            if (
                entry.is_symlink()
                or not entry.is_file(follow_symlinks=False)
                or _RETAINED_NAME_RE.fullmatch(entry.name) is None
            ):
                raise RawDocumentError(
                    f"retained archive directory holds an unexpected entry: "
                    f"{directory / entry.name}"
                )
            names.append(entry.name)
        if len(names) != 1:
            raise RawDocumentError(
                f"retained archive directory holds {len(names)} versions, "
                f"expected exactly one: {directory}"
            )
        relpath = directory / names[0]
        raw_file_hash, byte_count = self._hash_retained(relpath)
        return RetainedRawDocument(
            relpath=relpath, raw_file_hash=raw_file_hash, byte_count=byte_count
        )

    def verify_retained_raw_document(
        self, *, relpath: Path, expected_hash: str, expected_byte_count: int
    ) -> RawDocumentWriteResult:
        raw_file_hash, byte_count = self._hash_retained(relpath)
        if raw_file_hash != expected_hash or byte_count != expected_byte_count:
            raise RawDocumentError(
                f"retained archive changed: {relpath} is {raw_file_hash}/"
                f"{byte_count} bytes, expected {expected_hash}/{expected_byte_count}"
            )
        return RawDocumentWriteResult(
            relpath=relpath,
            raw_file_hash=raw_file_hash,
            byte_count=byte_count,
            created=False,
        )

    def _hash_retained(self, relpath: Path) -> tuple[str, int]:
        """Stream-hash one archive file, refusing links and concurrent change."""

        match = _RETAINED_NAME_RE.fullmatch(relpath.name)
        if match is None or relpath.parts[:1] != ("raw_documents",):
            raise RawDocumentError(f"not a content-addressed archive path: {relpath}")
        path = self._retained_data_path(relpath)
        self._require_no_symlink_component(relpath)
        try:
            inspected = os.lstat(path)
        except OSError as exc:
            raise RawDocumentError(f"retained archive is not readable: {relpath}: {exc}") from exc
        if not stat.S_ISREG(inspected.st_mode):
            raise RawDocumentError(f"retained archive is not a regular file: {relpath}")
        try:
            # O_NONBLOCK: a FIFO or device swapped in after the lstat above
            # must fail the descriptor check below, not block the open.
            fd = os.open(
                path,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as exc:
            raise RawDocumentError(f"retained archive is not readable: {relpath}: {exc}") from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise RawDocumentError(f"retained archive is not a regular file: {relpath}")
            if _file_identity(before) != _file_identity(inspected):
                raise RawDocumentError(f"retained archive changed while being opened: {relpath}")
            os.set_blocking(fd, True)
            digest = hashlib.sha256()
            byte_count = 0
            first_bytes = b""
            while True:
                chunk = os.read(fd, _CHUNK_SIZE)
                if not chunk:
                    break
                if len(first_bytes) < 5:
                    first_bytes += chunk[: 5 - len(first_bytes)]
                digest.update(chunk)
                byte_count += len(chunk)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        try:
            current = os.lstat(path)
        except OSError as exc:
            raise RawDocumentError(f"retained archive vanished: {relpath}: {exc}") from exc
        if (
            _file_identity(before) != _file_identity(after)
            or _file_identity(before) != _file_identity(current)
            or byte_count != before.st_size
        ):
            raise RawDocumentError(f"retained archive changed while being read: {relpath}")
        if not first_bytes.startswith(b"%PDF-"):
            raise RawDocumentError(f"retained archive is not a PDF: {relpath}")
        raw_file_hash = f"sha256:{digest.hexdigest()}"
        if raw_file_hash != f"sha256:{match.group(1)}":
            raise RawDocumentError(
                f"retained archive bytes do not match their content address: {relpath}"
            )
        return raw_file_hash, byte_count

    def _retained_data_path(self, relpath: Path) -> Path:
        try:
            return self._paths.data_path(relpath)
        except PathSafetyError as exc:
            raise RawDocumentError(f"archive path is unsafe: {relpath}: {exc}") from exc

    def _require_no_symlink_component(self, relpath: Path) -> None:
        """No component below the data root may be a link."""

        current = Path()
        for part in relpath.parts:
            current = current / part
            try:
                info = os.lstat(self._retained_data_path(current))
            except FileNotFoundError:
                return
            except OSError as exc:
                raise RawDocumentError(f"cannot inspect archive path {current}: {exc}") from exc
            if stat.S_ISLNK(info.st_mode):
                raise RawDocumentError(f"archive path component is a link: {current}")

    def quarantine_raw_document(
        self,
        *,
        provider: str,
        provider_document_id: str,
        input_file: Path,
        reason: QuarantineReason,
    ) -> QuarantineResult:
        # The quarantine copy may replace a caller's only input (a download
        # tmp file), so only a streamed, fsynced copy verified against an
        # unchanged input reports transfer_complete. Anything less leaves an
        # explicit empty marker and a manifest saying why, never a silent
        # b"" standing in for the original (round23).
        suffix = input_file.suffix.lower() if input_file.suffix else ".bin"
        name = f"{new_ulid()}_{reason}{suffix}"
        quarantine_path = self._paths.runtime_quarantine_path(
            provider=provider,
            provider_document_id=provider_document_id,
            name=name,
        )
        quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        copied = _copy_verified(
            input_file, quarantine_path, self._free_floor(quarantine_path.parent)
        )
        if not copied.complete:
            _write_bytes_durable(quarantine_path, b"")
        byte_count = copied.byte_count if copied.complete else 0
        payload_sha256 = copied.sha256 if copied.sha256 is not None else _EMPTY_SHA256

        manifest_path = quarantine_path.with_suffix(quarantine_path.suffix + ".json")
        manifest = {
            "provider": provider,
            "provider_document_id": provider_document_id,
            "reason": reason,
            "original_path": str(input_file),
            "byte_count": byte_count,
            "payload_sha256": payload_sha256,
            "payload_empty": byte_count == 0,
            "payload_complete": copied.complete,
            "input_missing": copied.input_missing,
            "input_byte_count": copied.input_byte_count,
            "copy_error": copied.error,
        }
        _write_bytes_durable(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        return QuarantineResult(
            path=quarantine_path,
            reason=reason,
            byte_count=byte_count,
            transfer_complete=copied.complete,
            input_missing=copied.input_missing,
            payload_sha256=copied.sha256,
        )


@dataclass(frozen=True)
class _CopyOutcome:
    complete: bool
    byte_count: int = 0
    sha256: str | None = None
    input_missing: bool = False
    input_byte_count: int | None = None
    error: str | None = None


def _copy_verified(source: Path, destination: Path, floor: FreeSpaceFloor) -> _CopyOutcome:
    """Stream ``source`` to ``destination`` via tmp + fsync + atomic rename.

    Input-side failures (missing, unreadable, not a regular file, changed
    while copied, no room above the free floor) return an incomplete outcome
    and leave nothing at ``destination``; destination-side OS errors propagate.
    """

    try:
        # O_NONBLOCK: a FIFO or device in place of the input must fail the
        # regular-file check below, not block this open.
        fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | _O_CLOEXEC)
    except FileNotFoundError:
        return _CopyOutcome(
            complete=False,
            input_missing=True,
            error="input file missing at quarantine time",
        )
    except OSError as exc:
        return _CopyOutcome(complete=False, error=f"input file is not readable: {exc}")
    tmp = destination.with_name(f"{destination.name}.tmp-{new_ulid()}")
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return _CopyOutcome(complete=False, error="input is not a regular file")
        os.set_blocking(fd, True)
        size = before.st_size
        try:
            floor.require(size, phase="quarantine")
        except AcquisitionCapacityError as exc:
            return _CopyOutcome(complete=False, input_byte_count=size, error=str(exc))
        digest = hashlib.sha256()
        copied = 0
        with tmp.open("xb") as out:
            while True:
                try:
                    chunk = os.read(fd, _CHUNK_SIZE)
                except OSError as exc:
                    return _CopyOutcome(
                        complete=False,
                        input_byte_count=size,
                        error=f"input read failed after {copied} bytes: {exc}",
                    )
                if not chunk:
                    break
                try:
                    floor.require(len(chunk), phase="quarantine")
                except AcquisitionCapacityError as exc:
                    return _CopyOutcome(
                        complete=False, input_byte_count=size, error=str(exc)
                    )
                out.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
            out.flush()
            os.fsync(out.fileno())
        after = os.fstat(fd)
        if _file_identity(after) != _file_identity(before) or copied != size:
            return _CopyOutcome(
                complete=False,
                input_byte_count=after.st_size,
                error=f"input changed while it was copied ({copied} of {size} bytes)",
            )
        os.replace(tmp, destination)
        _fsync_dir(destination.parent)
        return _CopyOutcome(
            complete=True,
            byte_count=copied,
            sha256=f"sha256:{digest.hexdigest()}",
            input_byte_count=size,
        )
    finally:
        os.close(fd)
        if os.path.lexists(tmp):
            tmp.unlink()


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
