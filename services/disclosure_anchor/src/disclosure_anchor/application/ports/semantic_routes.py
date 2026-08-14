"""Ports for closed-vocabulary semantic route adjudication and caching."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from disclosure_anchor.application.contracts.semantic_routes import (
    SemanticAdjudicationDecision,
    SemanticDocumentContext,
    SemanticRouteTaxonomy,
    SemanticRouteReceiptRow,
    SemanticRouteUnitInput,
)
from disclosure_anchor.application.ports.file_store import ArtifactWriteResult


class SemanticRouteAdjudicatorError(RuntimeError):
    def __init__(self, message: str, *, reason_code: str, retryable: bool) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.retryable = retryable


class SemanticRouteReceiptStoreError(RuntimeError):
    """Receipt bytes are temporarily unavailable, not contract-invalid."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class SemanticAdjudicatorIdentity:
    adapter: str
    model: str
    prompt_version: str

    def __post_init__(self) -> None:
        if not self.adapter or not self.model or not self.prompt_version:
            raise ValueError("semantic adjudicator identity is incomplete")


@dataclass(frozen=True, slots=True)
class SemanticAdjudicationBatch:
    document: SemanticDocumentContext
    taxonomy: SemanticRouteTaxonomy
    units: tuple[SemanticRouteUnitInput, ...]

    def __post_init__(self) -> None:
        if not self.units:
            raise ValueError("semantic adjudication batch cannot be empty")
        indices = [unit.unit_index for unit in self.units]
        if len(indices) != len(set(indices)):
            raise ValueError("semantic adjudication batch repeats a Unit")


class SemanticRouteAdjudicatorPort(Protocol):
    @property
    def identity(self) -> SemanticAdjudicatorIdentity: ...

    def adjudicate(
        self, batch: SemanticAdjudicationBatch
    ) -> tuple[SemanticAdjudicationDecision, ...]: ...


class SemanticRouteCachePort(Protocol):
    def get(self, cache_key: str) -> SemanticAdjudicationDecision | None: ...

    def put(
        self,
        cache_key: str,
        decision: SemanticAdjudicationDecision,
    ) -> None: ...


class SemanticRouteReceiptStorePort(Protocol):
    def write(
        self,
        *,
        relpath: Path,
        rows: tuple[SemanticRouteReceiptRow, ...],
    ) -> ArtifactWriteResult: ...

    def read(
        self,
        *,
        relpath: Path,
        expected_hash: str,
    ) -> tuple[SemanticRouteReceiptRow, ...]: ...


__all__ = [
    "SemanticAdjudicationBatch",
    "SemanticAdjudicatorIdentity",
    "SemanticRouteAdjudicatorPort",
    "SemanticRouteAdjudicatorError",
    "SemanticRouteCachePort",
    "SemanticRouteReceiptStorePort",
    "SemanticRouteReceiptStoreError",
]
