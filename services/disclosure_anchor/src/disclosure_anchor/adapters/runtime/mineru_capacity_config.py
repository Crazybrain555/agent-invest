"""Load the caller's expected capacity without deriving runtime observations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from disclosure_anchor.adapters.runtime.mineru_capacity_file import read_mineru_capacity_file
from disclosure_anchor.application.contracts.mineru_capacity_config import (
    MineruCapacityConfig,
    decode_mineru_capacity_config,
)


@dataclass(frozen=True, slots=True)
class LoadedMineruCapacityConfig:
    config: MineruCapacityConfig
    exact_bytes: bytes

    def __post_init__(self) -> None:
        if (
            type(self.config) is not MineruCapacityConfig
            or type(self.exact_bytes) is not bytes
            or self.config.exact_bytes != self.exact_bytes
        ):
            raise ValueError("MinerU loaded capacity config bytes disagree")

    @property
    def sha256(self) -> str:
        return self.config.sha256


def load_mineru_capacity_config(
    path: Path,
    *,
    expected_sha256: str,
    expected_owner_uid: int,
) -> LoadedMineruCapacityConfig:
    payload = read_mineru_capacity_file(
        path,
        expected_sha256=expected_sha256,
        expected_owner_uid=expected_owner_uid,
    )
    return LoadedMineruCapacityConfig(
        config=decode_mineru_capacity_config(payload), exact_bytes=payload,
    )


__all__ = ["LoadedMineruCapacityConfig", "load_mineru_capacity_config"]
