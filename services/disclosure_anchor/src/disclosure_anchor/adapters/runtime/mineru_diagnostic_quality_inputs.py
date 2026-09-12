"""Hold the original sealed inputs for a diagnostic quality attempt.

This lease owns file handles, not a quality verdict. It retains the complete
output tree and the same source stream; semantic work belongs to the supervised
workers. The journal's original clock and receipts remain the only authority.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
import hashlib
import os
from pathlib import PurePosixPath
import stat
from typing import Any, BinaryIO

from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import PinnedArtifactTree
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import DiagnosticJournalError
from disclosure_anchor.adapters.runtime.mineru_diagnostic_phases import DiagnosticPhases
from disclosure_anchor.adapters.runtime.mineru_diagnostic_resources import (
    DiagnosticResources, resource_identity,
)


def _close_directory(fd: int, identity: tuple[int, int]) -> None:
    current = os.fstat(fd)
    if (current.st_dev, current.st_ino) != (identity[0], identity[1]):
        raise DiagnosticJournalError("quality output descriptor was recycled; replacement left open")
    os.close(fd)


class HeldQualityInputs:
    """Adapter-created handle lease; use hold_quality_inputs to acquire it."""

    def __init__(self) -> None:
        raise TypeError("quality input leases must be acquired from their actual journal owner")

    @classmethod
    def _acquire(
        cls, *, resources: DiagnosticResources, source: BinaryIO,
        source_context: AbstractContextManager[BinaryIO],
        output_fd: int, output_acquired: tuple[int, int],
        snapshot: dict[str, Any], inventory: list[dict[str, Any]],
    ) -> HeldQualityInputs:
        held: HeldQualityInputs | None = None
        try:
            held = object.__new__(cls)
            held._initialize(resources=resources, source=source, output_fd=output_fd,
                             source_context=source_context, output_acquired=output_acquired,
                             snapshot=snapshot, inventory=inventory)
        except BaseException as primary:
            # The factory has transferred both inputs. Even object allocation
            # or the first attribute assignment can fail before _initialize
            # installs its ordinary close guard. That path has no tree yet.
            if held is None or not getattr(held, "_closed", False):
                errors = [primary]
                try:
                    _close_directory(output_fd, output_acquired)
                except BaseException as cleanup:
                    errors.append(cleanup)
                try:
                    source_context.__exit__(None, None, None)
                except BaseException as cleanup:
                    errors.append(cleanup)
                if len(errors) > 1:
                    raise BaseExceptionGroup("quality lease allocation and input closure failed", errors) from None
            raise
        return held

    def _initialize(
        self, *, resources: DiagnosticResources, source: BinaryIO,
        source_context: AbstractContextManager[BinaryIO],
        output_fd: int, output_acquired: tuple[int, int],
        snapshot: dict[str, Any], inventory: list[dict[str, Any]],
    ) -> None:
        self._resources = resources
        self._source = source
        self._source_context: AbstractContextManager[BinaryIO] | None = source_context
        self._output_fd = output_fd
        self._output_acquired = output_acquired
        self._snapshot = snapshot
        self._tree: PinnedArtifactTree | None = None
        self._closed = False
        try:
            self._inventory = {item["path"]: item for item in inventory}
            self._output_identity: list[int] = self._inventory["output"]["identity"]
            self._guard()
            self._verify_source()
            files = [item for item in inventory if stat.S_ISREG(item["identity"][2])]
            self._tree = PinnedArtifactTree.from_root_fd(
                display_root=resources.path / "output", root_fd=output_fd,
                max_files=max(1, len(files)), max_bytes=max(1, sum(item["bytes"] for item in files)),
                max_entries=len(inventory), require_private_modes=True,
                allow_empty_directories=True, checkpoint=resources.checkpoint,
            )
            self._tree.verify_unchanged()
            self._verify_inventory()
            self._verify_source()
            self._guard()
        except BaseException as primary:
            try:
                self.close()
            except BaseException as cleanup:
                raise BaseExceptionGroup("quality inputs acquisition and closure failed", [primary, cleanup]) from None
            raise

    def _guard(self) -> None:
        if self._closed or self._output_fd < 0:
            raise DiagnosticJournalError("quality input lease is closed")
        self._resources.journal.original_identity
        self._resources.checkpoint()
        if resource_identity(os.fstat(self._source.fileno())) != self._snapshot["identity"]:
            raise DiagnosticJournalError("quality held source identity changed")
        self._resources._verify_identity("source.pdf", self._snapshot["identity"])
        if resource_identity(os.fstat(self._output_fd)) != self._output_identity:
            raise DiagnosticJournalError("quality held output identity changed")
        self._resources._verify_identity("output", self._output_identity)

    @property
    def source(self) -> BinaryIO:
        self._guard()
        return self._source

    @property
    def output_root_fd(self) -> int:
        self._guard()
        return self._output_fd

    @property
    def output_tree(self) -> PinnedArtifactTree:
        self._guard()
        if self._tree is None:
            raise DiagnosticJournalError("quality input tree was not acquired")
        return self._tree

    def _verify_source(self) -> None:
        self._guard()
        before = os.fstat(self._source.fileno())
        expected = self._snapshot["bytes"]
        if before.st_size != expected:
            raise DiagnosticJournalError("quality source size differs from original snapshot")
        self._source.seek(0)
        digest = hashlib.sha256()
        observed = 0
        while True:
            self._resources.checkpoint()
            chunk = self._source.read(min(1024 * 1024, expected - observed + 1))
            self._resources.checkpoint()
            if not chunk:
                break
            observed += len(chunk)
            if observed > expected:
                raise DiagnosticJournalError("quality source grew beyond its original snapshot")
            digest.update(chunk)
        after = os.fstat(self._source.fileno())
        if (observed != expected or "sha256:" + digest.hexdigest() != self._snapshot["sha256"]
                or resource_identity(after) != self._snapshot["identity"]
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise DiagnosticJournalError("quality held source bytes changed")
        self._source.seek(0)
        self._guard()

    def _verify_inventory(self) -> None:
        self._guard()
        tree = self._tree
        if tree is None:
            raise DiagnosticJournalError("quality input tree was not acquired")
        directories = {"output" if path == PurePosixPath(".") else "output/" + path.as_posix()
                       for path in tree.directory_paths}
        expected_directories = {name for name, item in self._inventory.items()
                                if stat.S_ISDIR(item["identity"][2])}
        files = {"output/" + item.relative_path.as_posix(): item for item in tree.files}
        if directories != expected_directories or directories | files.keys() != self._inventory.keys():
            raise DiagnosticJournalError("quality full output inventory names differ from original seal")
        for name in sorted(directories):
            self._resources.checkpoint()
            self._resources._verify_identity(name, self._inventory[name]["identity"])
        for name, observed in files.items():
            self._resources.checkpoint()
            expected = self._inventory[name]
            identity = observed.identity
            if ([identity.device, identity.inode, identity.mode, identity.uid] != expected["identity"]
                    or observed.size_bytes != expected["bytes"] or observed.sha256 != expected["sha256"]):
                raise DiagnosticJournalError("quality output file differs from its original identity/bytes seal")
        self._guard()

    def verify_unchanged(self) -> None:
        """Rehash original held source and every output file under the old clock."""
        self._guard()
        self._verify_source()
        tree = self.output_tree
        tree.verify_contents_unchanged()
        self._verify_inventory()
        self._verify_source()
        self._guard()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tree, self._tree = self._tree, None
        fd, self._output_fd = self._output_fd, -1
        source_context, self._source_context = self._source_context, None
        errors: list[BaseException] = []
        if tree is not None:
            try:
                tree.close()
            except BaseException as cleanup:
                errors.append(cleanup)
        if fd >= 0:
            try:
                _close_directory(fd, self._output_acquired)
            except BaseException as cleanup:
                errors.append(cleanup)
        if source_context is not None:
            try:
                source_context.__exit__(None, None, None)
            except BaseException as cleanup:
                errors.append(cleanup)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("quality input handles did not all close", errors) from None


@contextmanager
def hold_quality_inputs(
    *, resources: DiagnosticResources, phases: DiagnosticPhases,
) -> Iterator[HeldQualityInputs]:
    """Acquire original journal-derived inputs, with no caller-seal authority."""
    journal = resources.journal
    if phases.journal is not journal:
        raise DiagnosticJournalError("quality phases and resources have different journal owners")
    journal.original_identity
    resources.checkpoint()
    original = DiagnosticPhases(journal, phases.binding)
    if original.has("cleanup_intent") or not original.has("output_sealed"):
        raise DiagnosticJournalError("quality inputs require completed original output before cleanup")
    if original.value("resources_created")["identity"] != resources.identity:
        raise DiagnosticJournalError("quality resources differ from original creation receipt")
    snapshot = original.value("snapshot_sealed")
    inventory = original.value("output_sealed")["inventory"]
    for item in inventory:
        if stat.S_ISDIR(item["identity"][2]):
            prior = resources._directories.get(item["path"])
            if prior is not None and prior != item["identity"]:
                raise DiagnosticJournalError("quality directory conflicts with original creation receipt")
            resources._directories[item["path"]] = item["identity"]
    source_context = resources.open_payload("source.pdf", identity=snapshot["identity"])
    source = source_context.__enter__()
    source_transferred = False
    held: HeldQualityInputs | None = None
    fd = -1
    acquired: tuple[int, int] | None = None
    errors: list[BaseException] = []
    try:
        with resources._parent("output") as (parent, leaf):
            resources.checkpoint()
            fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         dir_fd=parent)
            info = os.fstat(fd)
            acquired = (info.st_dev, info.st_ino)
            transfer, fd = fd, -1
            source_transferred = True
            held = HeldQualityInputs._acquire(
                resources=resources, source=source, source_context=source_context,
                output_fd=transfer, output_acquired=acquired, snapshot=snapshot, inventory=inventory,
            )
        yield held
    except BaseException as primary:
        errors.append(primary)
    finally:
        if held is not None:
            try:
                held.close()
            except BaseException as cleanup:
                errors.append(cleanup)
        if fd >= 0:
            try:
                if acquired is None:
                    os.close(fd)
                else:
                    _close_directory(fd, acquired)
            except BaseException as cleanup:
                errors.append(cleanup)
        if not source_transferred:
            try:
                source_context.__exit__(None, None, None)
            except BaseException as cleanup:
                errors.append(cleanup)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("quality input operation and closure failed", errors) from None
