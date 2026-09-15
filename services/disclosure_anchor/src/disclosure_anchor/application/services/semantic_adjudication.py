"""Availability-only provider execution for semantic route adjudication."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import json
import threading
from typing import Literal

from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_FAILOVER_POLICY_VERSION,
    SEMANTIC_ROUTER_VERSION,
    SemanticProviderAttempt,
    SemanticProviderIdentity,
    SemanticRouteContractError,
)
from disclosure_anchor.application.ports.semantic_routes import (
    SemanticAdjudicationBatch,
    SemanticAdjudicationCacheEntry,
    SemanticAdjudicationGroupCachePort,
    SemanticAdjudicationOutcome,
    SemanticAdjudicatorAdapterPort,
    SemanticExecutionGuard,
    SemanticRouteAdjudicatorError,
    SemanticRouteCacheError,
)
from disclosure_anchor.application.ports.staged_execution import note_stage, semantic_group_scope


_AVAILABILITY_REASON_CODES = frozenset(
    {
        "capacity_unavailable",
        "executable_unavailable",
        "not_authenticated",
        "runtime_io_failed",
        "timeout",
        "transport_unavailable",
    }
)
_CANCELLED_REASON_CODE = "cancelled"
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True, slots=True)
class ConfiguredSemanticProvider:
    adapter: SemanticAdjudicatorAdapterPort
    cache: SemanticAdjudicationGroupCachePort


class OrderedSemanticAdjudicationExecutor:
    """Run a fixed provider chain without weakening routing validation."""

    def __init__(
        self,
        providers: tuple[ConfiguredSemanticProvider, ...],
        *,
        policy_version: str = SEMANTIC_FAILOVER_POLICY_VERSION,
    ) -> None:
        if not providers:
            raise ValueError("semantic provider chain cannot be empty")
        if policy_version != SEMANTIC_FAILOVER_POLICY_VERSION:
            raise ValueError("semantic failover policy is unsupported")
        identities = tuple(item.adapter.provider_identity for item in providers)
        provider_ids = tuple(item.provider_id for item in identities)
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("semantic provider chain repeats a provider id")
        self._providers = providers
        self._policy_version = policy_version
        self._identities = identities

    @property
    def provider_identities(self) -> tuple[SemanticProviderIdentity, ...]:
        return self._identities

    def adjudicate(
        self,
        batch: SemanticAdjudicationBatch,
        *,
        group_hash: str,
        stage_guard: SemanticExecutionGuard | None = None,
    ) -> SemanticAdjudicationOutcome:
        # Measurement only: one group_started/group_ended pair around the
        # real group boundary, whatever path the adjudication takes.
        note_stage(stage_guard, "group_started", group_hash=group_hash, providers=len(self._providers))
        try:
            outcome = self._adjudicate(batch, group_hash=group_hash, stage_guard=stage_guard)
        except BaseException as exc:
            note_stage(stage_guard, "group_ended", group_hash=group_hash, outcome=_failure_outcome(exc))
            raise
        note_stage(stage_guard, "group_ended", group_hash=group_hash, outcome=(
            "degraded_unavailable" if outcome.degraded_unavailable
            else outcome.attempts[-1].outcome if outcome.attempts else "succeeded"
        ))
        return outcome

    def _adjudicate(
        self,
        batch: SemanticAdjudicationBatch,
        *,
        group_hash: str,
        stage_guard: SemanticExecutionGuard | None,
    ) -> SemanticAdjudicationOutcome:
        attempts: list[SemanticProviderAttempt] = []
        for ordinal, configured in enumerate(self._providers, start=1):
            if stage_guard is not None:
                stage_guard.checkpoint()
            identity = configured.adapter.provider_identity
            cache_key = semantic_group_cache_key(
                identity=identity,
                taxonomy_version=batch.taxonomy.version,
                group_hash=group_hash,
            )
            lock = _single_flight_lock(cache_key)
            with _guarded_single_flight(lock, stage_guard):
                cached = configured.cache.get(cache_key)
                if stage_guard is not None:
                    stage_guard.checkpoint()
                if cached is not None:
                    _validate_cache_entry(
                        cached,
                        cache_key=cache_key,
                        group_hash=group_hash,
                        identity=identity,
                    )
                    attempt = SemanticProviderAttempt(
                        ordinal=ordinal,
                        provider=identity,
                        outcome="cache_hit",
                        cache_key=cache_key,
                        response_sha256=cached.response_sha256,
                    )
                    attempts.append(attempt)
                    note_stage(stage_guard, "cache_hit", group_hash=group_hash, ordinal=ordinal,
                               provider_id=identity.provider_id)
                    return _successful_outcome(
                        group_hash=group_hash,
                        attempts=attempts,
                        decisions=cached.decisions,
                        identity=identity,
                        response_sha256=cached.response_sha256,
                    )
                note_stage(stage_guard, "provider_call_started", group_hash=group_hash, ordinal=ordinal,
                           provider_id=identity.provider_id)
                call_outcome = "succeeded"
                try:
                    with semantic_group_scope(group_hash):
                        result = (
                            configured.adapter.adjudicate_with_result(batch)
                            if stage_guard is None
                            else configured.adapter.adjudicate_with_result(batch, stage_guard=stage_guard)
                        )
                except SemanticRouteAdjudicatorError as exc:
                    call_outcome = (
                        "cancelled" if exc.reason_code == _CANCELLED_REASON_CODE
                        else "availability_failed" if exc.reason_code in _AVAILABILITY_REASON_CODES
                        else "failed_closed"
                    )
                    note_stage(stage_guard, "provider_call_ended", group_hash=group_hash, ordinal=ordinal,
                               provider_id=identity.provider_id, outcome=call_outcome, reason_code=exc.reason_code)
                    if stage_guard is not None:
                        stage_guard.checkpoint()
                    if exc.reason_code == _CANCELLED_REASON_CODE:
                        cancelled = SemanticProviderAttempt(
                            ordinal=ordinal,
                            provider=identity,
                            outcome="cancelled",
                            reason_code=exc.reason_code,
                            cache_key=cache_key,
                        )
                        raise SemanticRouteAdjudicatorError(
                            str(exc),
                            reason_code=exc.reason_code,
                            retryable=True,
                            attempts=(*attempts, cancelled),
                        ) from exc
                    if exc.reason_code not in _AVAILABILITY_REASON_CODES:
                        failed = SemanticProviderAttempt(
                            ordinal=ordinal,
                            provider=identity,
                            outcome="failed_closed",
                            reason_code=exc.reason_code,
                            cache_key=cache_key,
                        )
                        raise SemanticRouteAdjudicatorError(
                            str(exc),
                            reason_code=exc.reason_code,
                            retryable=False,
                            attempts=(*attempts, failed),
                        ) from exc
                    attempts.append(
                        SemanticProviderAttempt(
                            ordinal=ordinal,
                            provider=identity,
                            outcome="availability_failed",
                            reason_code=exc.reason_code,
                            availability_abstain_eligible=True,
                            cache_key=cache_key,
                        )
                    )
                    continue
                except BaseException as exc:
                    # Lease loss, cancellation or any unexpected error: the call
                    # interval is closed for measurement, the exception is untouched.
                    note_stage(stage_guard, "provider_call_ended", group_hash=group_hash, ordinal=ordinal,
                               provider_id=identity.provider_id, outcome=_failure_outcome(exc))
                    raise
                note_stage(stage_guard, "provider_call_ended", group_hash=group_hash, ordinal=ordinal,
                           provider_id=identity.provider_id, outcome=call_outcome)
                entry = SemanticAdjudicationCacheEntry(
                    cache_key=cache_key,
                    group_hash=group_hash,
                    provider=identity,
                    decisions=result.decisions,
                    response_sha256=result.response_sha256,
                )
                outcome: Literal[
                    "succeeded", "succeeded_cache_write_failed"
                ] = "succeeded"
                try:
                    if stage_guard is not None:
                        stage_guard.checkpoint()
                    configured.cache.put(entry)
                except SemanticRouteCacheError:
                    # The exact validated result and this failure are frozen in
                    # receipt v2 before DB success.  Receipt failure still
                    # fails the build closed.
                    outcome = "succeeded_cache_write_failed"
                if stage_guard is not None:
                    stage_guard.checkpoint()
                attempt = SemanticProviderAttempt(
                    ordinal=ordinal,
                    provider=identity,
                    outcome=outcome,
                    cache_key=cache_key,
                    response_sha256=result.response_sha256,
                )
                attempts.append(attempt)
                return _successful_outcome(
                    group_hash=group_hash,
                    attempts=attempts,
                    decisions=result.decisions,
                    identity=identity,
                    response_sha256=result.response_sha256,
                )
        if stage_guard is not None:
            stage_guard.checkpoint()
        return SemanticAdjudicationOutcome(
            policy_version=self._policy_version,
            group_hash=group_hash,
            attempts=tuple(attempts),
            decisions=(),
            actual_result_attempt=None,
            actual_result_identity=None,
            group_response_sha256=None,
            degraded_unavailable=True,
        )


def _failure_outcome(exc: BaseException) -> str:
    """Name a failed measurement interval without touching the exception."""

    if isinstance(exc, SemanticRouteAdjudicatorError):
        return "cancelled" if exc.reason_code == _CANCELLED_REASON_CODE else "failed_closed"
    if type(exc).__name__ == "StageLeaseLost":
        return "lease_lost"
    return "error:" + type(exc).__name__


@contextmanager
def _guarded_single_flight(
    lock: threading.Lock, stage_guard: SemanticExecutionGuard | None,
) -> Iterator[None]:
    if stage_guard is None:
        with lock:
            yield
        return
    while True:
        remaining = stage_guard.remaining_seconds()
        if lock.acquire(timeout=min(0.1, remaining)):
            break
    try:
        stage_guard.checkpoint()
        yield
    finally:
        lock.release()


def semantic_group_cache_key(
    *,
    identity: SemanticProviderIdentity,
    taxonomy_version: str,
    group_hash: str,
) -> str:
    payload = {
        "adapter_kind": identity.adapter_kind,
        "adapter_version": identity.adapter_version,
        "canonical_model": identity.canonical_model,
        "contract_version": "semantic_route_cache.v2",
        "group_hash": group_hash,
        "inference_profile": identity.inference_profile,
        "output_schema_sha256": identity.output_schema_sha256,
        "output_schema_version": identity.output_schema_version,
        "prompt_sha256": identity.prompt_sha256,
        "prompt_version": identity.prompt_version,
        "provider": identity.provider,
        "provider_id": identity.provider_id,
        "router_version": SEMANTIC_ROUTER_VERSION,
        "taxonomy_version": taxonomy_version,
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _successful_outcome(
    *,
    group_hash: str,
    attempts: list[SemanticProviderAttempt],
    decisions: tuple,
    identity: SemanticProviderIdentity,
    response_sha256: str,
) -> SemanticAdjudicationOutcome:
    return SemanticAdjudicationOutcome(
        policy_version=SEMANTIC_FAILOVER_POLICY_VERSION,
        group_hash=group_hash,
        attempts=tuple(attempts),
        decisions=decisions,
        actual_result_attempt=len(attempts),
        actual_result_identity=identity,
        group_response_sha256=response_sha256,
        degraded_unavailable=False,
    )


def _validate_cache_entry(
    entry: SemanticAdjudicationCacheEntry,
    *,
    cache_key: str,
    group_hash: str,
    identity: SemanticProviderIdentity,
) -> None:
    if (
        entry.cache_key != cache_key
        or entry.group_hash != group_hash
        or entry.provider != identity
    ):
        raise SemanticRouteContractError("semantic group cache identity drifted")


def _single_flight_lock(cache_key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(cache_key, threading.Lock())


__all__ = [
    "ConfiguredSemanticProvider",
    "OrderedSemanticAdjudicationExecutor",
    "semantic_group_cache_key",
]
