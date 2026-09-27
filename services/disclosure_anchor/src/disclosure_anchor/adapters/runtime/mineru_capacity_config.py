"""Load the caller's expected capacity without deriving runtime observations."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from disclosure_anchor.settings import Settings

from disclosure_anchor.adapters.runtime.mineru_capacity_file import read_mineru_capacity_file
from disclosure_anchor.application.contracts.mineru_capacity_config import (
    CAPACITY_CONFIG_CONTRACT_V2,
    AnyMineruCapacityConfig,
    MineruCapacityConfig,
    MineruCapacityConfigV2,
    decode_mineru_capacity_config,
    decode_mineru_capacity_config_v2,
)

# Canonical v2 bytes carry exactly this member; anything else, including a
# non-canonical v2 document, reaches the unchanged v1 decoder and fails closed.
_V2_MEMBER = b'"contract_version":"' + CAPACITY_CONFIG_CONTRACT_V2.encode("ascii") + b'"'


@dataclass(frozen=True, slots=True)
class LoadedMineruCapacityConfig:
    config: AnyMineruCapacityConfig
    exact_bytes: bytes

    def __post_init__(self) -> None:
        if (
            type(self.config) not in (MineruCapacityConfig, MineruCapacityConfigV2)
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
    config: AnyMineruCapacityConfig = (
        decode_mineru_capacity_config_v2(payload) if _V2_MEMBER in payload
        else decode_mineru_capacity_config(payload)
    )
    return LoadedMineruCapacityConfig(config=config, exact_bytes=payload)


def load_configured_mineru_capacity(settings: Settings) -> LoadedMineruCapacityConfig | None:
    """Load the paired external authority at a composition boundary."""
    path = settings.disclosure_mineru_capacity_config
    expected = settings.disclosure_mineru_capacity_config_sha256
    if path is None and expected is None:
        return None
    if path is None or expected is None or settings.worker_parse_execution_mode != "staged-v4":
        raise ValueError("explicit MinerU capacity requires paired staged-v4 authority")
    return load_mineru_capacity_config(path, expected_sha256=expected, expected_owner_uid=os.getuid())


def configured_mineru_capacity(
    settings: Settings, expected_capacity: AnyMineruCapacityConfig | None = None,
) -> AnyMineruCapacityConfig | None:
    """Retain an already loaded authority, or load it once for this consumer."""
    if expected_capacity is None:
        loaded = load_configured_mineru_capacity(settings)
        return None if loaded is None else loaded.config
    if (
        type(expected_capacity) not in (MineruCapacityConfig, MineruCapacityConfigV2)
        or settings.disclosure_mineru_capacity_config is None
        or settings.disclosure_mineru_capacity_config_sha256 != expected_capacity.sha256
        or settings.worker_parse_execution_mode != "staged-v4"
    ):
        raise ValueError("supplied MinerU capacity disagrees with external authority")
    return expected_capacity


__all__ = ["LoadedMineruCapacityConfig", "load_mineru_capacity_config", "load_configured_mineru_capacity", "configured_mineru_capacity"]
