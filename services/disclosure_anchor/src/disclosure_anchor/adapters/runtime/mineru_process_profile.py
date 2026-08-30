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
    expected_sha256: str | None = None,
) -> LoadedMineruProcessProfile:
    """Read one regular file without following a replaceable symlink.

    Consumers retain the exact bytes.  They must compare this hash with live
    health, container, collector and epoch evidence; reconstructing an
    equivalent dictionary is deliberately not supported.
    """

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow == 0:
        raise RuntimeError("MinerU profile loader requires no-follow file support")
    try:
        descriptor = os.open(path, flags | nofollow)
    except OSError as exc:
        raise ValueError("MinerU process profile cannot be opened safely") from exc
    try:
        observation = os.fstat(descriptor)
        if not stat.S_ISREG(observation.st_mode):
            raise ValueError("MinerU process profile is not a regular file")
        if not 1 <= observation.st_size <= _MAX_PROFILE_BYTES:
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
        if len(payload) != observation.st_size:
            raise ValueError("MinerU process profile changed while it was read")
    finally:
        os.close(descriptor)

    profile = decode_mineru_process_profile(payload)
    if expected_sha256 is not None and profile.sha256 != expected_sha256:
        raise ValueError("MinerU process profile hash differs from expected identity")
    return LoadedMineruProcessProfile(profile=profile, exact_bytes=payload)


__all__ = ["LoadedMineruProcessProfile", "load_mineru_process_profile"]
