"""Ports for closed-vocabulary semantic route adjudication and caching."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Protocol

from disclosure_anchor.application.contracts.semantic_routes import (
    SemanticAdjudicationDecision,
    SemanticDocumentContext,
    SemanticProviderAttempt,
    SemanticProviderIdentity,
    SemanticRouteTaxonomy,
    SemanticRouteReceiptRow,
    SemanticRouteUnitInput,
)
from disclosure_anchor.application.ports.file_store import ArtifactWriteResult


class SemanticRouteAdjudicatorError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        retryable: bool,
        attempts: tuple[SemanticProviderAttempt, ...] = (),
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.retryable = retryable
        self.attempts = attempts


class SemanticNetworkUnavailable(SemanticRouteAdjudicatorError):
    """No provider answered a group and the network is why; nothing was decided.

    Raised only by an executor composed with a network cooldown, for a scheduled
    caller that runs the same group again after ``retry_after_seconds``. No
    outcome, receipt or cache entry exists for the group. ``attempts`` are the
    group's provider attempts in this episode, empty when the shared cooldown
    refused the group before any provider was called. Escaping a scheduled
    caller, it is an adjudication error like any other and fails closed.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float,
        attempts: tuple[SemanticProviderAttempt, ...] = (),
    ) -> None:
        if (
            isinstance(retry_after_seconds, bool)
            or not isinstance(retry_after_seconds, (int, float))
            or not isfinite(retry_after_seconds)
            or retry_after_seconds <= 0
        ):
            raise ValueError("semantic network wait delay must be positive")
        super().__init__(
            message,
            reason_code="transport_unavailable",
            retryable=True,
            attempts=attempts,
        )
        self.retry_after_seconds = float(retry_after_seconds)


class SemanticNetworkWaitPort(Protocol):
    """Read-only view of the shared network cooldown for a scheduled caller."""

    def pending_seconds(self) -> float | None:
        """Seconds to keep waiting, or ``None`` when a provider call may be admitted now."""
        ...

    def shutdown_requested(self) -> bool:
        """Whether provider calls were stopped for this process, so a wait ends now."""
        ...


class SemanticRouteReceiptStoreError(RuntimeError):
    """Receipt bytes are temporarily unavailable, not contract-invalid."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class SemanticRouteCacheError(RuntimeError):
    """A cache mechanism failed without changing the adjudication result."""

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


@dataclass(frozen=True, slots=True)
class SemanticProviderResult:
    decisions: tuple[SemanticAdjudicationDecision, ...]
    response_sha256: str


@dataclass(frozen=True, slots=True)
class SemanticAdjudicationCacheEntry:
    cache_key: str
    group_hash: str
    provider: SemanticProviderIdentity
    decisions: tuple[SemanticAdjudicationDecision, ...]
    response_sha256: str


@dataclass(frozen=True, slots=True)
class SemanticAdjudicationOutcome:
    policy_version: str
    group_hash: str
    attempts: tuple[SemanticProviderAttempt, ...]
    decisions: tuple[SemanticAdjudicationDecision, ...]
    actual_result_attempt: int | None
    actual_result_identity: SemanticProviderIdentity | None
    group_response_sha256: str | None
    degraded_unavailable: bool


class SemanticRouteAdjudicatorPort(Protocol):
    @property
    def identity(self) -> SemanticAdjudicatorIdentity: ...

    def adjudicate(
        self, batch: SemanticAdjudicationBatch
    ) -> tuple[SemanticAdjudicationDecision, ...]: ...


class SemanticExecutionGuard(Protocol):
    """Live execution permission checked while waiting and around external IO."""

    def checkpoint(self) -> None: ...

    def remaining_seconds(self) -> float: ...


class SemanticAdjudicatorAdapterPort(SemanticRouteAdjudicatorPort, Protocol):
    @property
    def provider_identity(self) -> SemanticProviderIdentity: ...

    def adjudicate_with_result(
        self, batch: SemanticAdjudicationBatch, *,
        stage_guard: SemanticExecutionGuard | None = None,
    ) -> SemanticProviderResult: ...


class SemanticAdjudicationGroupCachePort(Protocol):
    def get(self, cache_key: str) -> SemanticAdjudicationCacheEntry | None: ...

    def put(self, entry: SemanticAdjudicationCacheEntry) -> None: ...


# Routing validation of one group's decoded decisions.  It raises
# SemanticRouteContractError when the decisions cannot become receipts for the
# requested group (SemanticDecisionCoverageError when they do not name exactly
# the requested Units); the executor runs it before any cache write and on every
# cache hit, so only routable results are cached or reused.
SemanticDecisionValidator = Callable[[tuple[SemanticAdjudicationDecision, ...]], None]


class SemanticAdjudicationExecutorPort(Protocol):
    @property
    def provider_identities(self) -> tuple[SemanticProviderIdentity, ...]: ...

    def adjudicate(
        self,
        batch: SemanticAdjudicationBatch,
        *,
        group_hash: str,
        stage_guard: SemanticExecutionGuard | None = None,
        validate: SemanticDecisionValidator | None = None,
    ) -> SemanticAdjudicationOutcome: ...


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
    "SemanticExecutionGuard",
    "SemanticAdjudicationBatch",
    "SemanticAdjudicationCacheEntry",
    "SemanticAdjudicationExecutorPort",
    "SemanticAdjudicationGroupCachePort",
    "SemanticAdjudicationOutcome",
    "SemanticAdjudicatorIdentity",
    "SemanticAdjudicatorAdapterPort",
    "SemanticDecisionValidator",
    "SemanticNetworkUnavailable",
    "SemanticNetworkWaitPort",
    "SemanticProviderResult",
    "SemanticRouteAdjudicatorPort",
    "SemanticRouteAdjudicatorError",
    "SemanticRouteCachePort",
    "SemanticRouteCacheError",
    "SemanticRouteReceiptStorePort",
    "SemanticRouteReceiptStoreError",
]
