"""Exact-byte loader for an attested MinerU process profile."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat

from disclosure_anchor.application.contracts.mineru_process_profile import (
    MineruProcessProfile,
    decode_mineru_process_profile,
)


_MAX_PROFILE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class LoadedMineruProcessProfile:
    """One immutable file observation and its decoded closed contract."""

    profile: MineruProcessProfile
    exact_bytes: bytes

    @property
    def sha256(self) -> str:
        return self.profile.sha256


def load_mineru_process_profile(
    path: Path,
    *,
    expected_sha256: str,
    expected_owner_uid: int,
) -> LoadedMineruProcessProfile:
    """Read one regular file without following a replaceable symlink.

    Consumers retain the exact bytes.  They must compare this hash with live
    health, container, collector and epoch evidence; reconstructing an
    equivalent dictionary is deliberately not supported.
    """

    if not path.is_absolute():
        raise ValueError("MinerU process profile path must be absolute")
    if not expected_sha256.startswith("sha256:") or len(expected_sha256) != 71:
        raise ValueError("MinerU process profile expected hash is invalid")
    if (
        isinstance(expected_owner_uid, bool)
        or not isinstance(expected_owner_uid, int)
        or expected_owner_uid < 0
    ):
        raise ValueError("MinerU process profile expected owner is invalid")
    descriptor = _open_absolute_without_symlinks(path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow == 0:
        raise ValueError("MinerU profile loader requires no-follow file support")
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("MinerU process profile is not a regular file")
        if before.st_uid != expected_owner_uid:
            raise ValueError("MinerU process profile owner differs from expected")
        if before.st_nlink != 1:
            raise ValueError("MinerU process profile must have exactly one hard link")
        if stat.S_IMODE(before.st_mode) & 0o022:
            raise ValueError("MinerU process profile is group/world writable")
        if not 1 <= before.st_size <= _MAX_PROFILE_BYTES:
            raise ValueError("MinerU process profile file is outside the closed envelope")
        chunks: list[bytes] = []
        remaining = _MAX_PROFILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(after) or len(payload) != before.st_size:
            raise ValueError("MinerU process profile changed while it was read")
    finally:
        os.close(descriptor)

    profile = decode_mineru_process_profile(payload)
    if profile.sha256 != expected_sha256:
        raise ValueError("MinerU process profile hash differs from expected identity")
    return LoadedMineruProcessProfile(profile=profile, exact_bytes=payload)


def _open_absolute_without_symlinks(path: Path) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    if nofollow == 0 or directory_flag == 0:
        raise ValueError("MinerU profile loader requires safe directory-open support")
    components = path.parts
    if not components or components[0] != os.sep or len(components) < 2:
        raise ValueError("MinerU process profile path is invalid")
    directory = os.open(os.sep, os.O_RDONLY | directory_flag | close_on_exec)
    try:
        for component in components[1:-1]:
            if component in {"", ".", ".."}:
                raise ValueError("MinerU process profile path component is invalid")
            next_directory = os.open(
                component,
                os.O_RDONLY | directory_flag | nofollow | close_on_exec,
                dir_fd=directory,
            )
            os.close(directory)
            directory = next_directory
        try:
            return os.open(
                components[-1],
                os.O_RDONLY | nofollow | close_on_exec,
                dir_fd=directory,
            )
        except OSError as exc:
            raise ValueError("MinerU process profile cannot be opened safely") from exc
    except OSError as exc:
        raise ValueError("MinerU process profile path cannot be traversed safely") from exc
    finally:
        os.close(directory)


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


__all__ = ["LoadedMineruProcessProfile", "load_mineru_process_profile"]
