"""Exact local payload ownership for the explicit v2 diagnostic lifecycle.

Creation receipts precede payload writes. A completed inventory binds original
inodes and bytes; recovery never adopts whatever happens to occupy a pathname.
An interrupted, unsealed extraction is retained for explicit reconciliation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any, BinaryIO, cast
import zipfile

from disclosure_anchor.adapters.parsers.mineru_medium.http_staged import (
    _MAX_DECODED_BYTES, _MAX_UNCOMPRESSED_BYTES, _MAX_ZIP_MEMBERS,
    _validate_zip_member,
)
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import (
    DiagnosticJournal, DiagnosticJournalError,
)


def resource_identity(info: os.stat_result) -> list[int]:
    directory = stat.S_ISDIR(info.st_mode)
    if (not directory and not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or not directory and info.st_nlink != 1):
        raise DiagnosticJournalError("diagnostic resource ownership/type/mode differs")
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid]


def validate_resource_identity(value: object) -> list[int]:
    if (type(value) is not list or len(value) != 4
            or any(type(item) is not int or item < 0 for item in value)
            or not (stat.S_ISREG(value[2]) or stat.S_ISDIR(value[2]))
            or stat.S_IMODE(value[2]) != (0o700 if stat.S_ISDIR(value[2]) else 0o600)
            or value[3] != os.getuid()):
        raise DiagnosticJournalError("invalid persisted diagnostic resource identity")
    return value


def _relative(value: str) -> tuple[str, ...]:
    pure = PurePosixPath(value)
    if (not value or len(value) > 4096 or pure.is_absolute() or ".." in pure.parts
            or pure.as_posix() != value or not pure.parts):
        raise DiagnosticJournalError("invalid diagnostic relative resource path")
    return pure.parts


def _digest_open(source: BinaryIO, checkpoint: Callable[[], float]) -> dict[str, Any]:
    before = os.fstat(source.fileno())
    identity = resource_identity(before)
    if not stat.S_ISREG(before.st_mode):
        raise DiagnosticJournalError("diagnostic payload is not a regular file")
    digest = hashlib.sha256()
    size = 0
    source.seek(0)
    while chunk := source.read(1024 * 1024):
        checkpoint()
        size += len(chunk)
        if size > before.st_size:
            raise DiagnosticJournalError("diagnostic payload grew while being verified")
        digest.update(chunk)
    after = os.fstat(source.fileno())
    if (resource_identity(after) != identity or size != before.st_size
            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise DiagnosticJournalError("diagnostic payload changed while being verified")
    return {"identity": identity, "bytes": size, "sha256": "sha256:" + digest.hexdigest()}


@contextmanager
def _stream(fd: int, mode: str) -> Iterator[BinaryIO]:
    """Keep FD ownership outside the wrapper, including failed construction."""
    stream: BinaryIO | None = None
    errors: list[BaseException] = []
    try:
        stream = cast(BinaryIO, os.fdopen(fd, mode, closefd=False))
        yield stream
    except BaseException as primary:
        errors.append(primary)
    finally:
        if stream is not None:
            try:
                stream.close()
            except BaseException as cleanup:
                errors.append(cleanup)
        try:
            os.close(fd)
        except BaseException as cleanup:
            errors.append(cleanup)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("diagnostic payload and owned handle closure failed", errors) from None


@contextmanager
def _descriptor(fd: int) -> Iterator[int]:
    try:
        yield fd
    except BaseException as primary:
        try:
            os.close(fd)
        except BaseException as cleanup:
            raise BaseExceptionGroup("diagnostic operation and descriptor closure failed", [primary, cleanup]) from None
        raise
    else:
        os.close(fd)


class DiagnosticResources:
    """Pin the original private resource directory for one journal owner."""

    def __init__(self, journal: DiagnosticJournal, *, identity: list[int] | None, cleanup: bool = False) -> None:
        self.journal = journal
        self.path = journal.root / "resources"
        reclaimed = journal.root / "resources-reclaim"
        if cleanup and reclaimed.exists():
            if self.path.exists() or self.path.is_symlink():
                raise DiagnosticJournalError("both diagnostic resource root names exist")
            self.path = reclaimed
        self._fd = -1
        self._directories: dict[str, list[int]] = {}
        parent = os.open(journal.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with _descriptor(parent):
                journal.remaining_seconds()
                if identity is None:
                    os.mkdir("resources", mode=0o700, dir_fd=parent)
                    os.fsync(parent)
                self._fd = os.open(self.path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                observed = resource_identity(os.fstat(self._fd))
                if identity is not None and observed != validate_resource_identity(identity):
                    raise DiagnosticJournalError("diagnostic resource directory was replaced")
                self.identity = observed
                self.checkpoint()
                if self.path.name == "resources-reclaim" and os.listdir(self._fd):
                    raise DiagnosticJournalError("reclaimed diagnostic root must already be empty")
                if identity is None:
                    journal.append("resources_created", {"identity": observed})
        except BaseException as primary:
            try:
                self.close()
            except BaseException as cleanup:
                raise BaseExceptionGroup("diagnostic resource initialization and closure failed", [primary, cleanup]) from None
            raise

    def checkpoint(self) -> float:
        remaining = self.journal.remaining_seconds()
        if (self._fd < 0 or resource_identity(os.fstat(self._fd)) != self.identity
                or resource_identity(self.path.stat(follow_symlinks=False)) != self.identity):
            raise DiagnosticJournalError("diagnostic resource root identity drifted")
        return remaining

    @contextmanager
    def _parent(self, relative: str) -> Iterator[tuple[int, str]]:
        parts = _relative(relative)
        self.checkpoint()
        fd = os.dup(self._fd)
        try:
            for index, part in enumerate(parts[:-1], 1):
                expected = self._directories.get("/".join(parts[:index]))
                if expected is None:
                    raise DiagnosticJournalError("diagnostic parent has no original ownership receipt")
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if (resource_identity(os.fstat(child)) != expected
                            or resource_identity(os.stat(part, dir_fd=fd, follow_symlinks=False)) != expected):
                        raise DiagnosticJournalError("diagnostic parent directory was replaced before opening")
                except BaseException:
                    os.close(child)
                    raise
                prior, fd = fd, child
                os.close(prior)
            yield fd, parts[-1]
        finally:
            os.close(fd)

    @contextmanager
    def create_payload(self, name: str, *, step: str) -> Iterator[BinaryIO]:
        if len(_relative(name)) != 1:
            raise DiagnosticJournalError("top-level diagnostic payload required")
        self.checkpoint()
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._fd)
        with _stream(fd, "w+b") as sink:
            created = resource_identity(os.fstat(sink.fileno()))
            os.fsync(self._fd)
            self.journal.append(step + "_created", {"identity": created})
            self.checkpoint()
            yield sink
            sink.flush()
            os.fsync(sink.fileno())
            if resource_identity(os.stat(name, dir_fd=self._fd, follow_symlinks=False)) != created:
                raise DiagnosticJournalError("diagnostic payload was replaced during writing")
            self.checkpoint()

    @contextmanager
    def open_payload(self, name: str, *, identity: list[int]) -> Iterator[BinaryIO]:
        with self._parent(name) as (parent, leaf):
            # Reject substituted special files before any blocking stream read.
            # O_NONBLOCK has no effect on the expected regular payload file.
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with _stream(fd, "rb") as source:
                observed = resource_identity(os.fstat(source.fileno()))
                if (observed != validate_resource_identity(identity)
                        or observed != resource_identity(os.stat(leaf, dir_fd=parent, follow_symlinks=False))):
                    raise DiagnosticJournalError("diagnostic payload creation identity drifted")
                yield source
                if resource_identity(os.stat(leaf, dir_fd=parent, follow_symlinks=False)) != observed:
                    raise DiagnosticJournalError("diagnostic payload was replaced while open")
                self.checkpoint()

    def seal_payload(self, name: str, *, identity: list[int]) -> dict[str, Any]:
        with self.open_payload(name, identity=identity) as source:
            return _digest_open(source, self.checkpoint)

    def verify_payload(self, name: str, receipt: dict[str, Any]) -> None:
        if self.seal_payload(name, identity=receipt["identity"]) != receipt:
            raise DiagnosticJournalError("sealed diagnostic payload changed; never redownload or repair")

    def _mkdir(self, relative: str) -> list[int]:
        with self._parent(relative) as (parent, leaf):
            self.checkpoint()
            os.mkdir(leaf, mode=0o700, dir_fd=parent)
            child = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            with _descriptor(child):
                identity = resource_identity(os.fstat(child))
                if resource_identity(os.stat(leaf, dir_fd=parent, follow_symlinks=False)) != identity:
                    raise DiagnosticJournalError("diagnostic new directory changed before ownership registration")
                os.fsync(child)
                os.fsync(parent)
            self._directories[relative] = identity
            return identity

    def extract_archive(self, archive_receipt: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract once through directory FDs, retaining exact created identities."""
        self.verify_payload("result.zip", archive_receipt)
        created: dict[str, list[int]] = {"output": self._mkdir("output")}
        self.journal.append("output_created", {"identity": created["output"]})
        with self.open_payload("result.zip", identity=archive_receipt["identity"]) as source:
            with zipfile.ZipFile(source) as archive:
                members = archive.infolist()
                if len(members) > _MAX_ZIP_MEMBERS:
                    raise DiagnosticJournalError("diagnostic ZIP member envelope exceeded")
                seen: set[str] = set()
                decoded = uncompressed = 0
                for member in members:
                    _validate_zip_member(member, seen=seen)
                    uncompressed += member.file_size
                    if Path(member.filename).suffix.lower() in {".json", ".md", ".txt"}:
                        decoded += member.file_size * 4
                    if uncompressed > _MAX_UNCOMPRESSED_BYTES or decoded > _MAX_DECODED_BYTES:
                        raise DiagnosticJournalError("diagnostic ZIP decoded/extraction envelope exceeded")
                written = 0
                for member in members:
                    self.checkpoint()
                    parts = _relative("output/" + PurePosixPath(member.filename).as_posix())
                    for index in range(1, len(parts) if not member.is_dir() else len(parts) + 1):
                        name = "/".join(parts[:index])
                        if name not in created:
                            created[name] = self._mkdir(name)
                        self._verify_identity(name, created[name])
                    if member.is_dir():
                        continue
                    name = "/".join(parts)
                    with self._parent(name) as (parent, leaf):
                        fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=parent)
                        with _stream(fd, "wb") as sink, archive.open(member) as content:
                            created[name] = resource_identity(os.fstat(sink.fileno()))
                            for chunk in iter(lambda: content.read(1024 * 1024), b""):
                                self.checkpoint()
                                self._verify_identity("output", created["output"])
                                written += len(chunk)
                                if written > _MAX_UNCOMPRESSED_BYTES:
                                    raise DiagnosticJournalError("diagnostic ZIP actual-byte envelope exceeded")
                                sink.write(chunk)
                            sink.flush()
                            os.fsync(sink.fileno())
                        os.fsync(parent)
        inventory: list[dict[str, Any]] = []
        for name, identity in sorted(created.items()):
            self._verify_identity(name, identity)
            if stat.S_ISDIR(identity[2]):
                with self._parent(name) as (parent, leaf):
                    fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                inventory.append({"path": name, "identity": identity, "bytes": None, "sha256": None})
            else:
                inventory.append({"path": name, **self.seal_payload(name, identity=identity)})
        self.verify_inventory(inventory, prefix="output", partial=False)
        return inventory

    def _verify_identity(self, name: str, identity: list[int]) -> None:
        with self._parent(name) as (parent, leaf):
            if resource_identity(os.stat(leaf, dir_fd=parent, follow_symlinks=False)) != identity:
                raise DiagnosticJournalError("original diagnostic child was replaced")

    def _names(self) -> set[str]:
        found: set[str] = set()

        def visit(fd: int, prefix: str) -> None:
            self.checkpoint()
            for leaf in os.listdir(fd):
                name = prefix + leaf
                info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
                resource_identity(info)
                found.add(name)
                if len(found) > _MAX_ZIP_MEMBERS * 2 + 3:
                    raise DiagnosticJournalError("diagnostic inventory envelope exceeded")
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        if resource_identity(os.fstat(child)) != resource_identity(info):
                            raise DiagnosticJournalError("diagnostic inventory directory changed")
                        visit(child, name + "/")
                    finally:
                        os.close(child)
        visit(self._fd, "")
        return found

    def verify_inventory(self, inventory: list[dict[str, Any]], *, prefix: str = "", partial: bool) -> None:
        expected = {item["path"]: item for item in inventory}
        if len(expected) != len(inventory):
            raise DiagnosticJournalError("duplicate diagnostic inventory path")
        for item in inventory:
            identity = validate_resource_identity(item["identity"])
            if stat.S_ISDIR(identity[2]):
                prior = self._directories.get(item["path"])
                if prior is not None and prior != identity:
                    raise DiagnosticJournalError("diagnostic directory inventory conflicts with original creation")
                self._directories[item["path"]] = identity
        observed = self._names()
        if prefix:
            observed = {name for name in observed if name == prefix or name.startswith(prefix + "/")}
        if not observed <= expected.keys() or not partial and observed != expected.keys():
            raise DiagnosticJournalError("diagnostic tree contains missing or foreign payloads")
        for name in sorted(observed):
            receipt = expected[name]
            self._verify_identity(name, receipt["identity"])
            if not stat.S_ISDIR(receipt["identity"][2]):
                self.verify_payload(name, {key: receipt[key] for key in ("identity", "bytes", "sha256")})

    def remove(self, inventory: list[dict[str, Any]], *, quarantine_nonce: str) -> None:
        """Move raw names into the owner-reserved namespace before deleting.

        The original cleanup intent binds the nonce. A process replacing a raw
        payload name cannot cause its replacement bytes to be unlinked. Unknown
        objects moved during a race remain quarantined. The private reserved
        namespace has the same exclusive writer as the journal; this is not an
        isolation boundary against a hostile process with the same OS identity.
        """
        if len(quarantine_nonce) != 32 or any(c not in "0123456789abcdef" for c in quarantine_nonce):
            raise DiagnosticJournalError("diagnostic cleanup nonce is invalid")
        originals = {item["path"]: item for item in inventory}
        quarantines: dict[str, str] = {}
        aliases: dict[str, str] = {}
        for name in originals:
            parts = _relative(name)
            suffix = hashlib.sha256((quarantine_nonce + "\n" + name).encode()).hexdigest()
            quarantine = "/".join((*parts[:-1], ".m6-reclaim-" + suffix))
            quarantines[name] = quarantine
            aliases[quarantine] = name
        if len(originals) != len(inventory) or originals.keys() & aliases.keys():
            raise DiagnosticJournalError("diagnostic cleanup namespace collision")
        observed = self._names()
        if not observed <= originals.keys() | aliases.keys():
            raise DiagnosticJournalError("diagnostic cleanup contains unknown entries")
        physical: list[dict[str, Any]] = []
        for name in observed:
            original = aliases.get(name, name)
            if original in observed and quarantines[original] in observed:
                raise DiagnosticJournalError("both original and quarantined diagnostic payload exist")
            physical.append({**originals[original], "path": name})
        self.verify_inventory(physical, partial=False)
        for original in sorted(originals, key=lambda item: (item.count("/"), item), reverse=True):
            quarantine = quarantines[original]
            name = original if original in observed else quarantine
            if name not in observed:
                continue
            receipt = originals[original]
            with self._parent(name) as (parent, leaf):
                self.checkpoint()
                identity = resource_identity(os.stat(leaf, dir_fd=parent, follow_symlinks=False))
                if identity != receipt["identity"]:
                    raise DiagnosticJournalError("diagnostic cleanup child identity drifted")
                quarantine_leaf = _relative(quarantine)[-1]
                if name == original:
                    try:
                        os.stat(quarantine_leaf, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise DiagnosticJournalError("diagnostic reserved cleanup name unexpectedly exists")
                    os.rename(leaf, quarantine_leaf, src_dir_fd=parent, dst_dir_fd=parent)
                    os.fsync(parent)
                if resource_identity(os.stat(quarantine_leaf, dir_fd=parent, follow_symlinks=False)) != identity:
                    raise DiagnosticJournalError("replaced diagnostic object retained in cleanup quarantine")
                if stat.S_ISDIR(identity[2]):
                    os.rmdir(quarantine_leaf, dir_fd=parent)
                else:
                    self.verify_payload(quarantine, {key: receipt[key] for key in ("identity", "bytes", "sha256")})
                    os.unlink(quarantine_leaf, dir_fd=parent)
                os.fsync(parent)
        self.checkpoint()
        parent = os.open(self.journal.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.listdir(self._fd):
                raise DiagnosticJournalError("diagnostic cleanup left foreign resource entries")
            if resource_identity(os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)) != self.identity:
                raise DiagnosticJournalError("diagnostic cleanup resource root replaced")
            if self.path.name == "resources":
                if (self.journal.root / "resources-reclaim").exists():
                    raise DiagnosticJournalError("diagnostic root cleanup name unexpectedly exists")
                os.rename("resources", "resources-reclaim", src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            if resource_identity(os.stat("resources-reclaim", dir_fd=parent, follow_symlinks=False)) != self.identity:
                raise DiagnosticJournalError("replaced diagnostic root retained in cleanup quarantine")
            os.rmdir("resources-reclaim", dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)
        self.close()
        self.journal.remaining_seconds()
        if any(path.exists() or path.is_symlink() for path in (
                self.journal.root / "resources", self.journal.root / "resources-reclaim")):
            raise DiagnosticJournalError("diagnostic resource path reappeared after removal")

    def close(self) -> None:
        fd, self._fd = self._fd, -1
        if fd >= 0:
            os.close(fd)

    def __enter__(self) -> DiagnosticResources:
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, traceback: object) -> None:
        try:
            self.close()
        except BaseException as cleanup:
            if exc is not None:
                raise BaseExceptionGroup("diagnostic resource operation and handle closure failed", [exc, cleanup]) from None
            raise
