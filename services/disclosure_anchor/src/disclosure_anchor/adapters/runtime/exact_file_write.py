"""Create a new file with exactly the given bytes or fail visibly.

``os.write`` may write fewer bytes than asked without raising; a receipt or
spec written that way must never be reported durable. The write loops until
every byte is on the descriptor, refuses to spin without progress, fsyncs, and
checks the resulting size before returning.
"""

from __future__ import annotations

import os
from pathlib import Path


def write_new_exact(path: Path, payload: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(payload)
        stalls = 0
        while view:
            written = os.write(fd, view)
            if written <= 0:
                stalls += 1
                if stalls >= 8:
                    raise OSError(f"write to {path} made no progress")
                continue
            stalls = 0
            view = view[written:]
        os.fsync(fd)
        if os.fstat(fd).st_size != len(payload):
            raise OSError(f"{path} size differs from its payload after write")
    finally:
        os.close(fd)


def publish_new_exact(path: Path, payload: bytes) -> None:
    """Make ``path`` appear only once its complete bytes are durable; never overwrite.

    A file created under its final name and then filled can be observed by a
    concurrent reader with partial content. The bytes are written whole and
    fsynced under a hidden sibling name first (``write_new_exact`` semantics:
    loop until complete, refuse to spin without progress, fsync, size check),
    then linked to the final name, which fails if that name already exists,
    and the directory entry is fsynced. A reader therefore sees either no file
    or the exact bytes. The sibling is removed afterwards; a leftover sibling
    after a crash is visible and never mistaken for the published file.
    """
    partial = path.with_name(f".{path.name}.partial.{os.getpid()}")
    write_new_exact(partial, payload)
    try:
        os.link(partial, path)
    finally:
        os.unlink(partial)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


__all__ = ["publish_new_exact", "write_new_exact"]
