"""Bounded private evidence for one explicitly owned v2 diagnostic attempt.

This container proves file ownership, ordering and original budget binding. The
lifecycle must validate every step payload before issuing a provider action. A
pending or damaged append is evidence to reconcile, never permission to repeat a
POST, replace a file, or fabricate an ACK. Legacy v1 journals are not adopted.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import stat
from types import TracebackType
from typing import Any
from collections.abc import Callable

from disclosure_anchor.adapters.runtime.mineru_diagnostic_store import _canonical, _digest

_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_STEP = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
_RECORD = re.compile(r"([0-9]{4})-([a-z][a-z0-9_]{0,47})\.json\Z")
_MAX_RECORD = 2 * 1024 * 1024 + 8192
_MAX_TOTAL = 16 * 1024 * 1024
_MAX_RECORDS = 96
_MAX_LIFETIME_NS = 7200 * 1_000_000_000
_HEADER = "00-journal.json"
_PENDING = "append-pending.json"


class DiagnosticJournalError(ValueError):
    """Evidence cannot authorize another diagnostic side effect."""


@dataclass(frozen=True, slots=True)
class DiagnosticJournalRecord:
    sequence: int
    step: str
    value: dict[str, Any]
    sha256: str
    observed_ns: int


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DiagnosticJournalError("diagnostic record contains duplicate JSON keys")
        result[key] = value
    return result


def _decode(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw, object_pairs_hook=_object)
    if type(value) is not dict or _canonical(value) != raw:
        raise DiagnosticJournalError("diagnostic record is not a canonical object")
    return value


def _directory_identity(info: os.stat_result) -> list[int]:
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise DiagnosticJournalError("diagnostic journal requires its private owned directory")
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid]


def _require_json_value(value: object) -> None:
    """Reject Python values whose JSON encoding would silently change meaning."""
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                raise DiagnosticJournalError("diagnostic object keys must be strings")
            _require_json_value(child)
    elif type(value) is list:
        for child in value:
            _require_json_value(child)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise DiagnosticJournalError("diagnostic payload must contain only JSON values")


class DiagnosticJournal:
    """One local writer, explicit same-clock/deadline recovery, immutable records."""

    def __init__(
        self, root: Path, *, create: bool, attempt_id: str,
        configuration_sha256: str, clock_identity_sha256: str,
        deadline_ns: int, continuous_ns: Callable[[], int],
    ) -> None:
        if (not root.is_absolute() or root.is_symlink() or not attempt_id
                or len(attempt_id) > 128 or any(ord(c) < 33 for c in attempt_id)
                or not _HASH.fullmatch(configuration_sha256) or not _HASH.fullmatch(clock_identity_sha256)
                or type(deadline_ns) is not int or not 0 < deadline_ns <= 2**63 - 1):
            raise DiagnosticJournalError("explicit bounded diagnostic identity and original deadline required")
        self.root, self._clock, self._deadline = root, continuous_ns, deadline_ns
        self._root_fd = self._lock_fd = -1
        self._closed = self._poisoned = False
        self._last_clock: int | None = None
        self._lock_identity: tuple[int, int] | None = None
        self._records: list[DiagnosticJournalRecord] = []
        self._bytes = 0
        if create:
            now = continuous_ns()
            if type(now) is not int or now < 0:
                raise DiagnosticJournalError("diagnostic continuous clock is invalid")
            if now >= deadline_ns:
                raise TimeoutError("original diagnostic deadline expired; preserve exact attempt")
            if deadline_ns - now > _MAX_LIFETIME_NS:
                raise DiagnosticJournalError("diagnostic lifetime exceeds the finite owner envelope")
            self._last_clock = now
            parent = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.mkdir(root.name, mode=0o700, dir_fd=parent)
                os.fsync(parent)
            except BaseException as primary:
                try:
                    os.close(parent)
                except BaseException as cleanup:
                    raise BaseExceptionGroup("diagnostic directory creation and parent closure failed",
                                             [primary, cleanup]) from None
                raise
            else:
                os.close(parent)
        try:
            self._root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self._identity = _directory_identity(os.fstat(self._root_fd))
            self._assert_root()
            flags = os.O_RDWR | os.O_NOFOLLOW
            if create:
                flags |= os.O_CREAT | os.O_EXCL
            self._lock_fd = os.open("owner.lock", flags, 0o600, dir_fd=self._root_fd)
            lock_info = self._owned_file(self._lock_fd)
            self._lock_identity = (lock_info.st_dev, lock_info.st_ino)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            header = dict(contract_version="mineru-diagnostic-journal.v2", attempt_id=attempt_id,
                          configuration_sha256=configuration_sha256, clock_identity_sha256=clock_identity_sha256,
                          deadline_ns=deadline_ns, root_identity=self._identity)
            if create:
                self.remaining_seconds()
                assert self._last_clock is not None
                if deadline_ns - self._last_clock > _MAX_LIFETIME_NS:
                    raise DiagnosticJournalError("diagnostic lifetime exceeds the finite owner envelope")
                self._write_new(_HEADER, _canonical({**header, "started_ns": self._last_clock}))
            raw = self._read(_HEADER)
            saved = _decode(raw)
            started = saved.get("started_ns")
            if (type(started) is not int or not 0 <= started < deadline_ns
                    or deadline_ns - started > _MAX_LIFETIME_NS
                    or raw != _canonical({**header, "started_ns": started})):
                raise DiagnosticJournalError("diagnostic original identity, root, clock or deadline changed")
            self._last_clock = started
            self._tail_sha = _digest(raw)
            self._bytes = len(raw)
            names = sorted(os.listdir(self._root_fd))
            if _PENDING in names:
                raise DiagnosticJournalError("diagnostic append outcome is uncertain; retain pending evidence")
            if "resources" in names:
                # Presence grants no cleanup authority. The lifecycle checks
                # the original creation receipt and phase before touching it.
                _directory_identity(os.stat("resources", dir_fd=self._root_fd, follow_symlinks=False))
            names = [name for name in names if name not in {_HEADER, "owner.lock", "resources"}]
            if len(names) > _MAX_RECORDS:
                raise DiagnosticJournalError("diagnostic journal record count exceeded")
            for sequence, name in enumerate(names, 1):
                match = _RECORD.fullmatch(name)
                if match is None or int(match[1]) != sequence:
                    raise DiagnosticJournalError("foreign or missing diagnostic record")
                raw = self._read(name)
                data = _decode(raw)
                if (set(data) != {"contract_version", "sequence", "step", "previous_sha256", "value_sha256", "value", "observed_ns"}
                        or data["contract_version"] != "mineru-diagnostic-record.v2"
                        or type(data["sequence"]) is not int or data["sequence"] != sequence
                        or data["step"] != match[2] or data["previous_sha256"] != self._tail_sha
                        or type(data["observed_ns"]) is not int
                        or not self._last_clock <= data["observed_ns"] < deadline_ns
                        or type(data["value"]) is not dict or data["value_sha256"] != _digest(_canonical(data["value"]))):
                    raise DiagnosticJournalError("diagnostic record identity/hash chain differs")
                self._bytes += len(raw)
                if self._bytes > _MAX_TOTAL:
                    raise DiagnosticJournalError("diagnostic journal byte budget exceeded")
                self._tail_sha = _digest(raw)
                self._last_clock = data["observed_ns"]
                self._records.append(DiagnosticJournalRecord(sequence, data["step"], data["value"], self._tail_sha, data["observed_ns"]))
            self._assert_root()
        except BaseException as primary:
            try:
                self.close()
            except BaseException as cleanup:
                raise BaseExceptionGroup("diagnostic initialization and handle closure failed", [primary, cleanup]) from None
            raise

    @property
    def records(self) -> tuple[DiagnosticJournalRecord, ...]:
        # Callers cannot mutate the container's already verified payload cache.
        return tuple(DiagnosticJournalRecord(r.sequence, r.step, _decode(_canonical(r.value)), r.sha256, r.observed_ns)
                     for r in self._records)

    def _assert_root(self) -> None:
        if self._closed or self._root_fd < 0:
            raise DiagnosticJournalError("diagnostic journal is closed")
        if (_directory_identity(os.fstat(self._root_fd)) != self._identity
                or _directory_identity(self.root.stat(follow_symlinks=False)) != self._identity):
            raise DiagnosticJournalError("diagnostic journal root was replaced")
        if self._lock_identity is not None:
            info = self._owned_file(self._lock_fd)
            named = os.stat("owner.lock", dir_fd=self._root_fd, follow_symlinks=False)
            if ((info.st_dev, info.st_ino) != self._lock_identity
                    or (named.st_dev, named.st_ino) != self._lock_identity):
                raise DiagnosticJournalError("diagnostic writer lock was replaced")

    @staticmethod
    def _owned_file(fd: int) -> os.stat_result:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise DiagnosticJournalError("diagnostic file ownership or type differs")
        return info

    def _read(self, name: str) -> bytes:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._root_fd)
        with os.fdopen(fd, "rb") as source:
            info = self._owned_file(source.fileno())
            if not 0 < info.st_size <= _MAX_RECORD:
                raise DiagnosticJournalError("diagnostic record byte bound exceeded")
            raw = source.read(_MAX_RECORD + 1)
            after = os.fstat(source.fileno())
            if (len(raw) != info.st_size or (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise DiagnosticJournalError("diagnostic record changed while reading")
        return raw

    def _write_new(self, name: str, raw: bytes) -> None:
        self._assert_root()
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._root_fd)
        with os.fdopen(fd, "wb") as out:
            self._owned_file(out.fileno())
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.fsync(self._root_fd)
        self._assert_root()
        if self._read(name) != raw:
            raise DiagnosticJournalError("diagnostic persisted record differs")

    def remaining_seconds(self) -> float:
        self._assert_root()
        now = self._clock()
        if (type(now) is not int or now < 0
                or (self._last_clock is not None and now < self._last_clock)):
            self._poisoned = True
            raise DiagnosticJournalError("diagnostic continuous clock is invalid or regressed")
        self._last_clock = now
        if self._poisoned:
            raise DiagnosticJournalError("diagnostic journal is poisoned after uncertain IO")
        if now >= self._deadline:
            raise TimeoutError("original diagnostic deadline expired; preserve exact attempt")
        return min(30.0, (self._deadline - now) / 1_000_000_000)

    def append(self, step: str, value: dict[str, Any]) -> DiagnosticJournalRecord:
        self.remaining_seconds()
        assert self._last_clock is not None
        if not _STEP.fullmatch(step) or type(value) is not dict:
            raise DiagnosticJournalError("closed diagnostic step and object required")
        _require_json_value(value)
        # Seal one detached payload before IO. A caller may change its object
        # while persistence runs; every returned/cached field must still bind
        # the same bytes that were durably written.
        value_raw = _canonical(value)
        payload = _decode(value_raw)
        observed_ns = self._last_clock
        sequence = len(self._records) + 1
        data = dict(contract_version="mineru-diagnostic-record.v2", sequence=sequence, step=step,
                    previous_sha256=self._tail_sha, value_sha256=_digest(value_raw), value=payload,
                    observed_ns=observed_ns)
        raw = _canonical(data)
        if sequence > _MAX_RECORDS or len(raw) > _MAX_RECORD or self._bytes + len(raw) > _MAX_TOTAL:
            raise DiagnosticJournalError("diagnostic append exceeds declared record/byte envelope")
        sha = _digest(raw)
        name = f"{sequence:04d}-{step}.json"
        try:
            self._write_new(_PENDING, _canonical(dict(record=name, sha256=sha)))
            self._write_new(name, raw)
            os.unlink(_PENDING, dir_fd=self._root_fd)
            os.fsync(self._root_fd)
            self._assert_root()
        except BaseException:
            self._poisoned = True
            raise
        record = DiagnosticJournalRecord(sequence, step, payload, sha, observed_ns)
        self._records.append(record)
        self._tail_sha = sha
        self._bytes += len(raw)
        return DiagnosticJournalRecord(sequence, step, _decode(value_raw), sha, observed_ns)

    def close(self) -> None:
        self._closed = True
        errors: list[BaseException] = []
        for attribute in ("_lock_fd", "_root_fd"):
            fd = getattr(self, attribute)
            if fd >= 0:
                # An error does not prove the descriptor is still ours. Retrying
                # its integer could close an unrelated, subsequently reused FD.
                setattr(self, attribute, -1)
                try:
                    os.close(fd)
                except BaseException as error:
                    errors.append(error)
        if errors:
            raise BaseExceptionGroup("diagnostic journal handle closure failed", errors)

    def __enter__(self) -> DiagnosticJournal:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        try:
            self.close()
        except BaseException as cleanup:
            if exc is not None:
                raise BaseExceptionGroup("diagnostic operation and handle closure failed", [exc, cleanup]) from None
            raise
