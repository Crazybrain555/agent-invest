"""Bounded, exact file observation shared with the standalone MinerU bootstrap.

This POSIX module is stdlib-only so the same bytes can be installed in the API
image. It reads a caller-selected file; it does not select configuration, read
environment variables, or decode the capacity contract.
"""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import stat


_MAX_BYTES = 64 * 1024


def read_mineru_capacity_file(
    path: Path,
    *,
    expected_sha256: str,
    expected_owner_uid: int,
) -> bytes:
    """Read through held directory/file descriptors, then verify exact bytes."""

    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("MinerU capacity file path must be absolute")
    if (
        type(expected_sha256) is not str
        or len(expected_sha256) != 71
        or not expected_sha256.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in expected_sha256[7:])
    ):
        raise ValueError("MinerU capacity file expected hash is invalid")
    if type(expected_owner_uid) is not int or expected_owner_uid < 0:
        raise ValueError("MinerU capacity file expected owner is invalid")
    parts = path.parts
    if (
        len(parts) < 2
        or parts[0] != os.sep
        or any(part in {"", ".", ".."} for part in parts[1:])
    ):
        raise ValueError("MinerU capacity file path components are invalid")
    required_flags = ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK", "O_CLOEXEC")
    if any(getattr(os, name, 0) == 0 for name in required_flags):
        raise ValueError("MinerU capacity file requires POSIX no-follow file support")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as held:
        directory = os.open(os.sep, directory_flags)
        held.callback(os.close, directory)
        for part in parts[1:-1]:
            directory = os.open(part, directory_flags, dir_fd=directory)
            held.callback(os.close, directory)
        descriptor = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
        held.callback(os.close, descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("MinerU capacity file must be regular")
        if before.st_uid != expected_owner_uid:
            raise ValueError("MinerU capacity file owner differs from expected")
        if before.st_nlink != 1:
            raise ValueError("MinerU capacity file must have exactly one hard link")
        if stat.S_IMODE(before.st_mode) & 0o022:
            raise ValueError("MinerU capacity file is group/world writable")
        if not 1 <= before.st_size <= _MAX_BYTES:
            raise ValueError("MinerU capacity file is outside the closed envelope")
        chunks: list[bytes] = []
        remaining = _MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(after) or len(payload) != before.st_size:
            raise ValueError("MinerU capacity file changed while reading")
    if "sha256:" + hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError("MinerU capacity file hash differs from expected identity")
    return payload


def _file_identity(observation: os.stat_result) -> tuple[int, ...]:
    return (
        observation.st_dev,
        observation.st_ino,
        observation.st_mode,
        observation.st_nlink,
        observation.st_uid,
        observation.st_gid,
        observation.st_size,
        observation.st_mtime_ns,
        observation.st_ctime_ns,
    )


__all__ = ["read_mineru_capacity_file"]
