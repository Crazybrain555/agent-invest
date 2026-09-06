"""Application boundary for immutable, content-addressed V4 execution specs."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
import re
from typing import Protocol

from disclosure_anchor.application.contracts.v4_prepared_execution_spec import (
    MAX_V4_PREPARED_EXECUTION_SPEC_BYTES,
    V4PreparedExecutionSpec,
)


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class V4ExecutionSpecCatalogReference:
    spec_sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        if type(self.spec_sha256) is not str or _SHA256.fullmatch(self.spec_sha256) is None:
            raise ValueError("V4 execution spec reference digest is invalid")
        if (
            type(self.byte_count) is not int
            or not 1 <= self.byte_count <= MAX_V4_PREPARED_EXECUTION_SPEC_BYTES
        ):
            raise ValueError("V4 execution spec reference byte count is invalid")


class V4ExecutionSpecCatalogPathPort(Protocol):
    """Sole path authority for one content-addressed spec object."""

    def v4_execution_spec_relpath(self, *, spec_sha256: str) -> Path: ...


class V4LegacyExecutionSpecPathPort(V4ExecutionSpecCatalogPathPort, Protocol):
    def data_path(self, relpath: Path) -> Path: ...


class V4ExecutionSpecCatalogPort(Protocol):
    """Read-only legacy import boundary; never injected into worker runtime."""

    def load(
        self,
        *,
        reference: V4ExecutionSpecCatalogReference,
    ) -> V4PreparedExecutionSpec: ...


class V4LegacyExecutionSpecRetirementPort(Protocol):
    def retire_exact(
        self, *, reference: V4ExecutionSpecCatalogReference,
        authorize: Callable[[V4PreparedExecutionSpec], None],
    ) -> bool:
        """Delete only the pinned exact legacy file after callback authorization."""


__all__ = [
    "V4ExecutionSpecCatalogPathPort",
    "V4ExecutionSpecCatalogPort",
    "V4ExecutionSpecCatalogReference",
]
