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
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
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


__all__ = ["write_new_exact"]
