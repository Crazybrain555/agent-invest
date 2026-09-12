"""Original-descriptor storage for bounded owned-quality evidence.

The store creates one fixed sibling directory and sixteen exclusive files. It
never removes, replaces or truncates evidence, and grants no process or ACK
authority. Interrupted creation and unreported writes remain unresolved.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import stat
from types import TracebackType
from typing import Any, Literal

from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import MAX_WIRE_JSON_BYTES
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import (
    DiagnosticJournal, DiagnosticJournalError, DiagnosticJournalIdentity,
)
from disclosure_anchor.adapters.runtime.mineru_diagnostic_phases import DiagnosticPhases, DiagnosticUnresolved
from disclosure_anchor.adapters.runtime.mineru_diagnostic_quality_phases import quality_creation_value
from disclosure_anchor.adapters.runtime.mineru_diagnostic_store import _canonical, _digest
from disclosure_anchor.application.contracts.mineru_diagnostic_quality import (
    H2ByteBudget, QUALITY_FILE_SLOTS, RetainedFileSeal,
)

QUALITY_WRITE_CHUNK_BYTES = 64 * 1024
_LIVE_STORES: dict[DiagnosticJournal, OwnedQualityStore] = {}
_SEAL_KIND = Literal["complete", "failure_prefix"]


class DiagnosticQualityWriteError(DiagnosticJournalError):
    """One call's confirmed prefix, without guessing an unreturned write size."""

    def __init__(self, *, requested_bytes: int, confirmed_bytes: int,
                 unreported_write_outcome: bool) -> None:
        self.requested_bytes = requested_bytes
        self.confirmed_bytes = confirmed_bytes
        self.unreported_write_outcome = unreported_write_outcome
        super().__init__("owned quality write failed; preserve confirmed prefix and unresolved outcome")


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid


def _private(info: os.stat_result, *, directory: bool) -> tuple[int, int, int, int]:
    if (info.st_mode != (0o40700 if directory else 0o100600) or info.st_uid != os.getuid()
            or info.st_ino < 1 or not directory and info.st_nlink != 1):
        raise DiagnosticJournalError("owned quality file type, private mode or ownership differs")
    return _identity(info)


class _Handle:
    def __init__(self) -> None:
        self.fd = -1
        self.identity: tuple[int, int, int, int] | None = None

    @classmethod
    def open(cls, path: str | Path, flags: int, *, parent: int | None = None) -> _Handle:
        handle = cls()
        try:
            handle.fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                0o600, dir_fd=parent)
            handle.identity = _identity(os.fstat(handle.fd))
            return handle
        except BaseException as primary:
            try:
                handle.close()
            except BaseException as cleanup:
                raise BaseExceptionGroup("quality descriptor acquisition and closure failed", [primary, cleanup]) from None
            raise

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        if fd < 0:
            return
        if self.identity is not None:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != self.identity[:2]:
                raise DiagnosticJournalError("quality descriptor recycled; replacement left open")
        # Before the first successful fstat the fresh unexposed FD is ours.
        # Never repeat a failed fstat or retry a consumed close integer.
        os.close(fd)


@dataclass(slots=True)
class _Slot:
    handle: _Handle
    count: int = 0
    reserved: int = 0
    state: str = "empty"
    digest: Any = field(default_factory=hashlib.sha256)
    seal: RetainedFileSeal | None = None


class BoundedQualitySink:
    """One logical writer claim; it never owns or exposes a raw descriptor."""

    _store: OwnedQualityStore
    _slot: str
    _closed: bool

    def __init__(self) -> None:
        raise TypeError("quality sinks must be acquired from their original store")

    @classmethod
    def _new(cls, store: OwnedQualityStore, slot: str) -> BoundedQualitySink:
        sink = object.__new__(cls)
        sink._store, sink._slot, sink._closed = store, slot, False
        return sink

    def write(self, chunk: bytes) -> int:
        if self._closed:
            raise DiagnosticJournalError("quality sink is closed")
        return self._store._write(self._slot, chunk)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            slot = self._store._slots.get(self._slot)
            if slot is not None and slot.state == "writing":
                slot.state = "retired"

    def __enter__(self) -> BoundedQualitySink:
        if self._closed:
            raise DiagnosticJournalError("quality sink is closed")
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()


class OwnedQualityStore:
    """Adapter-created owner of all original evidence handles and byte charges."""

    _closed: bool
    _poisoned: bool
    _busy: bool
    _journal: DiagnosticJournal
    _phases: DiagnosticPhases
    _handles: list[_Handle]
    _slots: dict[str, _Slot]
    _parent: _Handle | None
    _root: _Handle | None
    _ready: bool
    _total: int
    _reserved: int
    _original: DiagnosticJournalIdentity
    _budget: H2ByteBudget
    _path: Path

    def __init__(self) -> None:
        raise TypeError("quality stores require the actual original journal factory")

    @classmethod
    def _new(cls, journal: DiagnosticJournal, phases: DiagnosticPhases) -> OwnedQualityStore:
        # Complete all allocation-only setup before opening or claiming an FD.
        store = object.__new__(cls)
        store._closed, store._poisoned, store._busy = False, False, False
        store._journal, store._phases = journal, phases
        store._handles = []
        store._slots = {}
        store._parent = None
        store._root = None
        store._ready = False
        store._total = store._reserved = 0
        store._original = journal.original_identity
        config = phases.owned_quality
        if config is None:
            raise DiagnosticJournalError("quality store requires the original owned v3 binding")
        store._budget = config.budget
        store._path = journal.root.parent / config.retained_name
        return store

    def _open(self, path: str | Path, flags: int, *, parent: int | None = None) -> _Handle:
        self._assert_live()
        handle = _Handle.open(path, flags, parent=parent)
        try:
            self._assert_live()
            self._handles.append(handle)
        except BaseException as primary:
            try:
                handle.close()
            except BaseException as cleanup:
                raise BaseExceptionGroup("quality handle registration and closure failed", [primary, cleanup]) from None
            raise
        return handle

    def _assert_live(self) -> None:
        if self._closed or _LIVE_STORES.get(self._journal) is not self:
            raise DiagnosticJournalError("quality store is closed or no longer the live owner")
        if self._poisoned:
            raise DiagnosticUnresolved("quality store IO is uncertain; retain original evidence")

    def _guard(self) -> None:
        self._assert_live()
        self._journal.remaining_seconds()
        self._assert_live()
        if self._journal.original_identity != self._original:
            raise DiagnosticJournalError("quality original journal header differs")
        # Raw appends or an externally changed cached phase object grant no IO.
        self._phases = DiagnosticPhases(self._journal, self._phases.binding)
        parent = self._parent
        if parent is None or parent.fd < 0:
            raise DiagnosticJournalError("quality parent handle missing")
        if (_identity(os.fstat(parent.fd)) != parent.identity
                or _identity(self._path.parent.stat(follow_symlinks=False)) != parent.identity
                or _identity(os.stat(self._journal.root.name, dir_fd=parent.fd, follow_symlinks=False))
                != self._original.root_identity):
            raise DiagnosticJournalError("quality retained parent or original journal namespace changed")
        root = self._root
        if root is not None:
            if (_private(os.fstat(root.fd), directory=True) != root.identity
                    or _identity(os.stat(self._path.name, dir_fd=parent.fd, follow_symlinks=False)) != root.identity):
                raise DiagnosticJournalError("quality retained root changed")
            if set(os.listdir(root.fd)) != set(self._slots):
                raise DiagnosticJournalError("quality retained namespace has missing or foreign slots")
            for name, slot in self._slots.items():
                info = os.fstat(slot.handle.fd)
                if (_private(info, directory=False) != slot.handle.identity
                        or _identity(os.stat(name, dir_fd=root.fd, follow_symlinks=False)) != slot.handle.identity
                        or info.st_size != slot.count
                        or os.lseek(slot.handle.fd, 0, os.SEEK_CUR) != slot.count):
                    raise DiagnosticJournalError("quality original slot identity, size or write offset changed")
        self._assert_live()

    @contextmanager
    def _operation(self) -> Iterator[None]:
        if self._busy:
            raise DiagnosticJournalError("quality store operation cannot be reentered")
        self._busy = True
        try:
            try:
                self._guard()
            except BaseException:
                self._poisoned = True
                raise
            if not self._ready:
                raise DiagnosticJournalError("quality slot identities are not durably established")
            yield
        finally:
            self._busy = False

    @property
    def path(self) -> Path:
        with self._operation():
            return self._path

    @property
    def budget(self) -> H2ByteBudget:
        with self._operation():
            return H2ByteBudget.from_payload(self._budget.to_payload())

    @property
    def retained_bytes(self) -> int:
        with self._operation():
            return self._total

    def _slot(self, name: str) -> _Slot:
        if type(name) is not str or name not in QUALITY_FILE_SLOTS or name not in self._slots:
            raise DiagnosticJournalError("quality fixed slot required")
        return self._slots[name]

    def _remaining(self, name: str) -> int:
        slot = self._slot(name)
        return min(self._budget.slot_limit(name) - slot.count - slot.reserved,
                   self._budget.retained_total_bytes - self._total - self._reserved)

    def remaining(self, slot: str) -> int:
        with self._operation():
            return self._remaining(slot)

    def open_new_sink(self, slot: str) -> BoundedQualitySink:
        with self._operation():
            state = self._slot(slot)
            if state.state != "empty" or state.count or state.seal is not None:
                raise DiagnosticJournalError("quality slot already claimed, retired or sealed")
            sink = BoundedQualitySink._new(self, slot)
            state.state = "writing"
            return sink

    def _write(self, name: str, chunk: bytes) -> int:
        with self._operation():
            slot = self._slot(name)
            if slot.state != "writing" or type(chunk) is not bytes or len(chunk) > QUALITY_WRITE_CHUNK_BYTES:
                raise DiagnosticJournalError("quality write requires its live writer and one bounded byte chunk")
            if len(chunk) > self._remaining(name):
                raise DiagnosticJournalError("quality chunk exceeds original per-slot or aggregate allowance")
            slot.reserved += len(chunk)
            self._reserved += len(chunk)
            confirmed = 0
            in_syscall = False
            try:
                while confirmed < len(chunk):
                    self._guard()
                    if slot.state != "writing":
                        raise DiagnosticJournalError("quality writer retired before the next raw write")
                    in_syscall = True
                    count = os.write(slot.handle.fd, memoryview(chunk)[confirmed:])
                    if type(count) is int and count == 0:
                        in_syscall = False
                        raise OSError("quality write returned no valid bounded progress")
                    if type(count) is not int or not 0 < count <= len(chunk) - confirmed:
                        raise OSError("quality write returned an impossible progress report")
                    in_syscall = False
                    # Commit exactly returned bytes before the next callback.
                    start = confirmed
                    confirmed += count
                    slot.count += count
                    self._total += count
                    slot.reserved -= count
                    self._reserved -= count
                    slot.digest.update(memoryview(chunk)[start:confirmed])
                    self._guard()
                self._guard()
                return confirmed
            except BaseException as primary:
                self._poisoned = True
                # Unreported bytes remain reserved, never relabelled as zero.
                raise DiagnosticQualityWriteError(requested_bytes=len(chunk), confirmed_bytes=confirmed,
                                                  unreported_write_outcome=in_syscall) from primary

    def _scan(self, slot: _Slot, *, collect: bool) -> tuple[str, bytes]:
        self._guard()
        before = os.fstat(slot.handle.fd)
        digest = hashlib.sha256()
        count = 0
        chunks: list[bytes] = []
        while True:
            self._guard()
            raw = os.pread(slot.handle.fd, min(QUALITY_WRITE_CHUNK_BYTES, slot.count - count + 1), count)
            self._guard()
            if not raw:
                break
            count += len(raw)
            if count > slot.count:
                raise DiagnosticJournalError("quality slot grew during bounded reread")
            digest.update(raw)
            if collect:
                chunks.append(raw)
        after = os.fstat(slot.handle.fd)
        fields = ("st_size", "st_mtime_ns", "st_ctime_ns")
        sha = "sha256:" + digest.hexdigest()
        if (count != slot.count or any(getattr(before, key) != getattr(after, key) for key in fields)
                or sha != "sha256:" + slot.digest.hexdigest()):
            raise DiagnosticJournalError("quality slot exact bytes changed while rereading")
        self._guard()
        return sha, b"".join(chunks) if collect else b""

    def seal(self, slot: str, *, evidence_kind: _SEAL_KIND) -> RetainedFileSeal:
        with self._operation():
            state = self._slot(slot)
            if type(evidence_kind) is not str or evidence_kind not in {"complete", "failure_prefix"}:
                raise DiagnosticJournalError("quality retained evidence kind differs")
            if state.seal is not None and state.seal.evidence_kind != evidence_kind:
                raise DiagnosticJournalError("quality sealed disposition cannot be changed")
            state.state = "retired"
            try:
                self._guard()
                os.fsync(state.handle.fd)
                self._guard()
                sha, _ = self._scan(state, collect=False)
                assert state.handle.identity is not None
                current = RetainedFileSeal(slot, state.handle.identity, state.count, sha, evidence_kind)
                if state.seal is not None and state.seal != current:
                    raise DiagnosticJournalError("quality original retained seal changed")
                state.seal = current
                state.state = "sealed"
                # The registry owns its own frozen value. A caller deliberately
                # altering a detached DTO cannot upgrade our original seal kind.
                return RetainedFileSeal.from_payload(current.to_payload())
            except BaseException:
                self._poisoned = True
                raise

    def read_sealed(self, slot: str, *, maximum_bytes: int) -> bytes:
        with self._operation():
            state = self._slot(slot)
            seal = state.seal
            if (seal is None or type(maximum_bytes) is not int or maximum_bytes <= 0
                    or seal.byte_count > min(maximum_bytes, self._budget.slot_limit(slot))):
                raise DiagnosticJournalError("quality sealed read lacks its original seal or bounded allowance")
            try:
                sha, raw = self._scan(state, collect=True)
                if sha != seal.sha256 or len(raw) != seal.byte_count:
                    raise DiagnosticJournalError("quality sealed bytes no longer match")
                return raw
            except BaseException:
                self._poisoned = True
                raise

    def verify_unchanged(self) -> tuple[RetainedFileSeal, ...]:
        with self._operation():
            if any(self._slots[name].seal is None for name in QUALITY_FILE_SLOTS):
                raise DiagnosticJournalError("quality complete inventory requires every original slot seal")
            seals: list[RetainedFileSeal] = []
            try:
                for name in QUALITY_FILE_SLOTS:
                    state = self._slots[name]
                    seal = state.seal
                    assert seal is not None
                    sha, _ = self._scan(state, collect=False)
                    if sha != seal.sha256 or state.count != seal.byte_count or state.handle.identity != seal.identity:
                        raise DiagnosticJournalError("quality frozen inventory changed")
                    seals.append(RetainedFileSeal.from_payload(seal.to_payload()))
                if sum(s.byte_count for s in seals) != self._total or self._reserved:
                    raise DiagnosticJournalError("quality total retained byte conservation differs")
                return tuple(seals)
            except BaseException:
                self._poisoned = True
                raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        for handle in reversed(self._handles):
            try:
                handle.close()
            except BaseException as error:
                errors.append(error)
        if _LIVE_STORES.get(self._journal) is self:
            del _LIVE_STORES[self._journal]
        if errors:
            raise BaseExceptionGroup("quality original handles did not all close", errors)

    def __enter__(self) -> OwnedQualityStore:
        with self._operation():
            return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        try:
            self.close()
        except BaseException as cleanup:
            if exc is not None:
                raise BaseExceptionGroup("quality operation and descriptor closure failed", [exc, cleanup]) from None
            raise


def _creation_capacity(journal: DiagnosticJournal, values: list[tuple[str, dict[str, Any]]]) -> None:
    # Worst possible integer/hash envelope widths bound these exact remaining
    # creation payloads. Preserve minimum seven-record ACK/absence tail space;
    # the later full owner additionally reserves its remaining role/final data.
    demand = 0
    for step, value in values:
        raw = _canonical({"contract_version": "mineru-diagnostic-record.v2", "sequence": 96, "step": step,
                          "previous_sha256": "sha256:" + "0" * 64, "value_sha256": _digest(_canonical(value)),
                          "value": value, "observed_ns": 2**63 - 1})
        if len(raw) > 2 * 1024 * 1024 + 8192:
            raise DiagnosticJournalError("quality creation envelope exceeds original record limit")
        demand += len(raw)
    journal.require_capacity(additional_records=len(values) + 7,
                             additional_bytes=demand + 4 * MAX_WIRE_JSON_BYTES + 7 * 8192)


def create_quality_store(*, journal: DiagnosticJournal, phases: DiagnosticPhases) -> OwnedQualityStore:
    if phases.journal is not journal:
        raise DiagnosticJournalError("quality store and phases have different journal owners")
    original = DiagnosticPhases(journal, phases.binding)
    journal.remaining_seconds()
    if (original.owned_quality is None or not original.has("output_sealed")
            or original.has("quality_intent") or original.has("validated") or original.has("cleanup_intent")):
        raise DiagnosticUnresolved("quality store creation requires fresh completed original output")
    if journal in _LIVE_STORES:
        raise DiagnosticJournalError("quality store already owns this exact journal")
    store = OwnedQualityStore._new(journal, original)
    if _LIVE_STORES.setdefault(journal, store) is not store:
        raise DiagnosticJournalError("quality store already owns this exact journal")
    try:
        store._parent = store._open(journal.root.parent, os.O_RDONLY | os.O_DIRECTORY)
        parent = store._parent
        assert parent.identity is not None
        if not stat.S_ISDIR(parent.identity[2]):
            raise DiagnosticJournalError("quality retained parent is not a directory")
        store._guard()
        intent = quality_creation_value(original, "quality_intent", retained_parent_identity=list(parent.identity),
                                        retained_name=store._path.name,
                                        snapshot_record_sha256=original.latest["snapshot_sealed"].sha256,
                                        output_record_sha256=original.latest["output_sealed"].sha256)
        common = {"contract_version": "mineru-owned-quality.phase.v1",
                  "configuration_sha256": store._original.configuration_sha256,
                  "basis_record_sha256": "sha256:" + "0" * 64}
        maximum_identity = [2**63 - 1, 2**63 - 1, 0o100600, 2**63 - 1]
        future_root = {**common, "root_identity": [2**63 - 1, 2**63 - 1, 0o40700, 2**63 - 1]}
        future_files = {**common, "files": [{"slot": name, "identity": maximum_identity} for name in QUALITY_FILE_SLOTS]}
        _creation_capacity(journal, [("quality_intent", intent), ("quality_root_created", future_root),
                                     ("quality_files_created", future_files)])
        original.append("quality_intent", intent)
        store._guard()
        _creation_capacity(journal, [("quality_root_created", future_root), ("quality_files_created", future_files)])
        store._guard()
        os.mkdir(store._path.name, mode=0o700, dir_fd=parent.fd)
        store._root = store._open(store._path.name, os.O_RDONLY | os.O_DIRECTORY, parent=parent.fd)
        root = store._root
        if _private(os.fstat(root.fd), directory=True) != root.identity:
            raise DiagnosticJournalError("quality created root changed before admission")
        store._guard()
        os.fsync(root.fd)
        os.fsync(parent.fd)
        store._guard()
        store._phases.append("quality_root_created", quality_creation_value(
            store._phases, "quality_root_created", root_identity=list(root.identity)))
        for name in QUALITY_FILE_SLOTS:
            _creation_capacity(journal, [("quality_files_created", future_files)])
            store._guard()
            handle = store._open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL, parent=root.fd)
            _private(os.fstat(handle.fd), directory=False)
            store._slots[name] = _Slot(handle)
            store._guard()
            os.fsync(handle.fd)
            store._guard()
        os.fsync(root.fd)
        store._guard()
        files = [{"slot": name, "identity": list(store._slots[name].handle.identity or ())} for name in QUALITY_FILE_SLOTS]
        store._phases.append("quality_files_created", quality_creation_value(store._phases, "quality_files_created", files=files))
        store._guard()
        store._ready = True
        return store
    except BaseException as primary:
        try:
            store.close()
        except BaseException as cleanup:
            raise BaseExceptionGroup("quality store creation and closure failed", [primary, cleanup]) from None
        raise


def reopen_quality_store(*, journal: DiagnosticJournal, phases: DiagnosticPhases) -> OwnedQualityStore:
    if phases.journal is not journal:
        raise DiagnosticJournalError("quality store and phases have different journal owners")
    DiagnosticPhases(journal, phases.binding)
    journal.remaining_seconds()
    # No accepted phase currently proves both complete roles or a sealed final.
    # This seam must not return an early read-only owner or reset byte authority.
    raise DiagnosticUnresolved("quality reopen requires durable complete roles or final evidence")
