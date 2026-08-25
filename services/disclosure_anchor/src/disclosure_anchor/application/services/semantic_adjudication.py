"""Availability-only provider execution for semantic route adjudication."""

from __future__ import annotations

from dataclasses import dataclass
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
    SemanticRouteAdjudicatorError,
    SemanticRouteCacheError,
)


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
    ) -> SemanticAdjudicationOutcome:
        attempts: list[SemanticProviderAttempt] = []
        for ordinal, configured in enumerate(self._providers, start=1):
            identity = configured.adapter.provider_identity
            cache_key = semantic_group_cache_key(
                identity=identity,
                taxonomy_version=batch.taxonomy.version,
                group_hash=group_hash,
            )
            lock = _single_flight_lock(cache_key)
            with lock:
                cached = configured.cache.get(cache_key)
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
                    return _successful_outcome(
                        group_hash=group_hash,
                        attempts=attempts,
                        decisions=cached.decisions,
                        identity=identity,
                        response_sha256=cached.response_sha256,
                    )
                try:
                    result = configured.adapter.adjudicate_with_result(batch)
                except SemanticRouteAdjudicatorError as exc:
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
                    configured.cache.put(entry)
                except SemanticRouteCacheError:
                    # The exact validated result and this failure are frozen in
                    # receipt v2 before DB success.  Receipt failure still
                    # fails the build closed.
                    outcome = "succeeded_cache_write_failed"
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
