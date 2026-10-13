"""Bounded private evidence of failed semantic provider invocations.

The worker log carries only content-free hashes, and the CLI's own diagnostic
text otherwise lives in per-call temporary files that are removed. This store
keeps a bounded, redacted record of that text under the runtime root, readable
only by the service user. Capture errors never escape: each becomes a status
for the ordinary log and never replaces the provider failure being recorded.
"""

from __future__ import annotations

from collections.abc import Mapping
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

try:
    import fcntl
except ImportError:  # A host without POSIX locks still imports the adapter; capture fails.
    fcntl = None  # type: ignore[assignment]


FAILURE_DIAGNOSTIC_SCHEMA = "semantic_provider_failure_diagnostic.v1"
FAILURE_DIAGNOSTIC_DIRECTORY = "failure_diagnostics"
MAX_RECORDS = 256
MAX_RECORD_BYTES = 64 * 1024
MAX_TEXT_BYTES = 4 * 1024

# UTC microsecond timestamp, writer pid and a random suffix: names sort by
# creation time and never collide between threads or processes.
_RECORD_NAME = re.compile(r"[0-9]{8}T[0-9]{12}Z-[0-9]+-[0-9a-f]{16}\.json")
_LOCK_WAIT_SECONDS = 2.0
_PROCESS_LOCK = threading.Lock()

# Defence in depth for provider text: credentials can ride in URL userinfo or
# query strings, authorization headers, key/token/secret assignments, and
# bearer/API tokens. Response-header values that the 0.156.1 CLI appends to
# HTTP errors are dropped as well.
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/?#@]*@")
_URL_QUERY = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s?#,)\]>\"']*)[?#][^\s,)\]>\"']*")
_AUTHORIZATION = re.compile(
    r"(?i)\b((?:proxy-)?authorization\s*[\"']?\s*[:=]\s*[\"']?)"
    r"(?:[a-z][a-z0-9._~+/-]*\s+)?[^\s,;\"')\]}]+"
)
_SECRET_FIELD = re.compile(
    r"(?i)\b([a-z0-9_-]*(?:api[_-]?key|token|secret|password|passwd|cookie)\s*[\"']?\s*[:=]\s*[\"']?)"
    r"[^\s,;\"'&)\]}]+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;)\]]+")
_TOKEN = re.compile(
    r"\b(?:sk|sess|rk)-[A-Za-z0-9_-]{8,}"
    r"|\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"
)
_HEADER_VALUE = re.compile(r"(, (?:cf-ray|request id|auth error code|auth error): )[^,\r\n]*")


def redact_diagnostic_text(text: str) -> str:
    # Queries first: the userinfo placeholder's bracket would end a URL match.
    text = _URL_QUERY.sub(r"\1?[redacted]", text)
    text = _URL_USERINFO.sub(r"\1[redacted]@", text)
    text = _AUTHORIZATION.sub(r"\1[redacted]", text)
    text = _SECRET_FIELD.sub(r"\1[redacted]", text)
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _TOKEN.sub("[redacted]", text)
    return _HEADER_VALUE.sub(r"\1[redacted]", text)


def encoded_size(value: object) -> int:
    return len(_encode(value))


def bounded_text(text: str, limit: int) -> tuple[str, bool]:
    """Return the longest prefix whose JSON encoding fits ``limit`` bytes."""

    if encoded_size(text) <= limit:
        return text, False
    # Every character encodes to at least one byte.
    candidate = text[:limit]
    low, high = 0, len(candidate)
    while low < high:
        middle = (low + high + 1) // 2
        if encoded_size(candidate[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return candidate[:low], True


class PrivateFailureDiagnostics:
    """Keep at most ``max_records`` private failure records in one directory.

    The directory is created 0700 and opened without following symlinks; each
    record is created exclusively (0600) relative to that directory handle.
    Creation and pruning run under an exclusive lock, so the count bound holds
    across threads and worker processes sharing the runtime root. Older
    records are pruned only after a new one exists, so a failed capture never
    costs evidence within the bound; if pruning fails, at most one record
    exceeds the bound and further captures fail until it can be removed.
    """

    def __init__(
        self,
        directory: Path,
        *,
        max_records: int = MAX_RECORDS,
        max_record_bytes: int = MAX_RECORD_BYTES,
    ) -> None:
        if max_records < 1 or max_record_bytes < 1024:
            raise ValueError("failure diagnostic bounds are invalid")
        self._directory = directory
        self._max_records = max_records
        self._max_record_bytes = max_record_bytes

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def max_record_bytes(self) -> int:
        return self._max_record_bytes

    def retain(self, record: Mapping[str, object]) -> dict[str, object]:
        """Write one record; return a content-free status for the ordinary log."""

        try:
            payload = _encode(dict(record)) + b"\n"
            if len(payload) > self._max_record_bytes:
                raise ValueError("failure diagnostic record exceeds its size bound")
            name, prune_error = self._write(payload)
        except Exception as exc:  # noqa: BLE001 - capture never replaces the provider failure
            return {"status": "failed", "error": type(exc).__name__}
        status: dict[str, object] = {
            "status": "retained",
            "file": name,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        if prune_error is not None:
            status["prune_error"] = prune_error
        return status

    def _write(self, payload: bytes) -> tuple[str, str | None]:
        # One deadline bounds both waits, so a failing provider call is never
        # held up behind another writer.
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        if not _PROCESS_LOCK.acquire(timeout=_LOCK_WAIT_SECONDS):
            raise TimeoutError("failure diagnostic capture is busy")
        try:
            directory_fd = self._open_directory()
            try:
                _lock(directory_fd, deadline)
                # Only an earlier failed prune leaves more than the bound; catch
                # up first, or fail here rather than grow past one extra record.
                _prune(directory_fd, _record_names(directory_fd), keep=self._max_records)
                name = _create(directory_fd, payload)
                try:
                    _prune(
                        directory_fd,
                        # The record just written stays, whatever the wall clock did.
                        _record_names(directory_fd, excluding=name),
                        keep=self._max_records - 1,
                    )
                except OSError as exc:
                    # The new record exists; one older record stays over the bound.
                    return name, type(exc).__name__
                return name, None
            finally:
                os.close(directory_fd)  # also releases the lock
        finally:
            _PROCESS_LOCK.release()

    def _open_directory(self) -> int:
        with contextlib.suppress(FileExistsError):
            os.mkdir(self._directory, 0o700)
        directory_fd = os.open(
            self._directory,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            info = os.fstat(directory_fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise PermissionError("failure diagnostic directory is not owned by this user")
            if stat.S_IMODE(info.st_mode) != 0o700:
                os.fchmod(directory_fd, 0o700)
        except BaseException:
            os.close(directory_fd)
            raise
        return directory_fd


def _encode(value: object) -> bytes:
    # ASCII-only JSON: control and bidirectional characters stay escaped when
    # an operator prints the file.
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _lock(directory_fd: int, deadline: float) -> None:
    if fcntl is None:
        raise OSError("failure diagnostic capture needs POSIX file locking")
    while True:
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError("failure diagnostic directory is locked") from None
            time.sleep(0.01)


def _record_names(directory_fd: int, *, excluding: str | None = None) -> list[str]:
    # Only this store's regular files count; foreign entries and symlinks stay untouched.
    with os.scandir(directory_fd) as entries:
        return sorted(
            entry.name
            for entry in entries
            if entry.name != excluding
            and _RECORD_NAME.fullmatch(entry.name)
            and entry.is_file(follow_symlinks=False)
        )


def _prune(directory_fd: int, names: list[str], *, keep: int) -> None:
    for name in names[: max(0, len(names) - keep)]:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=directory_fd)


def _create(directory_fd: int, payload: bytes) -> str:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    for _attempt in range(4):
        name = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            + f"-{os.getpid()}-{secrets.token_hex(8)}.json"
        )
        try:
            file_fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            continue
        try:
            with os.fdopen(file_fd, "wb") as handle:
                # The umask may narrow the creation mode; keep it exactly 0600.
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=directory_fd)
            raise
        return name
    raise FileExistsError("could not allocate a unique failure diagnostic record name")


__all__ = [
    "FAILURE_DIAGNOSTIC_DIRECTORY",
    "FAILURE_DIAGNOSTIC_SCHEMA",
    "MAX_RECORDS",
    "MAX_RECORD_BYTES",
    "MAX_TEXT_BYTES",
    "PrivateFailureDiagnostics",
    "bounded_text",
    "encoded_size",
    "redact_diagnostic_text",
]
