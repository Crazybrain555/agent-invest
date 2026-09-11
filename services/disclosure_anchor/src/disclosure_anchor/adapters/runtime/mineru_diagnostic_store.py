"""Private diagnostic file primitives shared by explicit lifecycle owners.

These operations grant no parser, database, publication or remote ACK authority.
Callers own a new private root and must prove phase/identity before cleanup.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import stat

from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import PinnedArtifactTree


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _record(root: Path, name: str, value: object) -> None:
    fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as sink:
        sink.write(_canonical(value))
        sink.flush()
        os.fsync(sink.fileno())
    _fsync_directory(root)


def _identity(path: Path) -> tuple[int, int, int, int]:
    value = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid():
        raise ValueError("diagnostic directory ownership drifted")
    return value.st_dev, value.st_ino, value.st_mode, value.st_uid


def _remove_diagnostic_resources(
    resources: Path, *, expected_identity: tuple[int, int, int, int],
    checkpoint: Callable[[], float],
) -> None:
    """Use the same pinned exact-content deletion primitive as V4 cleanup."""
    parent_fd = os.open(resources.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    root_fd = -1
    try:
        root_fd = os.open(resources.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=parent_fd)
        pinned = os.fstat(root_fd)
        if (pinned.st_dev, pinned.st_ino, pinned.st_mode, pinned.st_uid) != expected_identity:
            raise ValueError("diagnostic cleanup root identity drifted")

        def before_effect() -> None:
            checkpoint()
            current = os.stat(resources.name, dir_fd=parent_fd, follow_symlinks=False)
            if ((current.st_dev, current.st_ino, current.st_mode, current.st_uid) != expected_identity
                    or _identity(resources) != expected_identity):
                raise ValueError("diagnostic resource root replaced before cleanup effect")

        with PinnedArtifactTree.from_root_fd(
            display_root=resources, root_fd=root_fd, allow_empty_directories=True,
        ) as tree:
            before_effect()
            tree.remove_exact_admitted_contents(before_effect=before_effect, last_files=())
        before_effect()
        if os.listdir(root_fd):
            raise ValueError("diagnostic cleanup left unexpected owned entries")
        os.rmdir(resources.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        # macOS may report st_nlink=1 for an already removed open directory;
        # admission/empty-FD/rmdir/parent-fsync prove removal, not that counter.
        try:
            os.stat(resources.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError("diagnostic root path reappeared after removal")
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(parent_fd)

