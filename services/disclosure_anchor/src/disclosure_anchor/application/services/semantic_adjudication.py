"""Availability-only provider execution for semantic route adjudication."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Iterator
from contextlib import contextmanager
import hashlib
import json
from math import isfinite
import threading
import time
from typing import Literal

from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_FAILOVER_POLICY_VERSION,
    SEMANTIC_ROUTER_VERSION,
    SemanticDecisionCoverageError,
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
    SemanticDecisionValidator,
    SemanticExecutionGuard,
    SemanticNetworkUnavailable,
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
_INVALID_CONTRACT_REASON_CODE = "invalid_contract"
_INVALID_DECISION_REASON_CODE = "invalid_decision"
# A group is a network outage when every provider was tried and failed on
# availability and at least one of them on the recognized transport family.
# That provider is expected to answer once the network returns, so the group
# waits rather than publishing degraded routes for good, whatever availability
# reason the others gave. Without transport evidence (only capacity, login,
# executable, local IO or deadline reasons) the deliberate degraded_unavailable
# policy is unchanged.
_NETWORK_REASON_CODE = "transport_unavailable"
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True, slots=True)
class SemanticNetworkTransition:
    """One content-free state change of the shared network cooldown."""

    event: Literal["opened", "probe_failed", "closed"]
    consecutive_outages: int
    outage_seconds: float
    retry_after_seconds: float | None = None
    group_hash: str | None = None
    # (provider_id, reason_code) of the attempts that decided an outage.
    reasons: tuple[tuple[str, str], ...] = ()


class SharedSemanticNetworkCooldown:
    """One circuit for every group adjudicated by the same provider chain.

    Closed admits every provider call. A group that no provider answered, with
    at least one unreachable over the network, opens it: an uncached group then
    waits without calling any provider until the cooldown ends. After that one
    probe group runs the chain while the others keep waiting; a provider answer
    closes the circuit and a failed probe reopens it with the next delay,
    ``initial_seconds`` doubling up to ``max_seconds`` with no attempt limit, so
    a long outage keeps waiting and recovers by itself. A probe that ends any
    other way (degraded, failed closed, cancelled) is released without a
    verdict. A group admitted while closed stops before its next provider once
    the circuit has opened. State is process-local: a restarted worker starts
    closed.
    """

    def __init__(
        self,
        *,
        initial_seconds: float = 60.0,
        max_seconds: float = 600.0,
        probe_wait_seconds: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
        on_transition: Callable[[SemanticNetworkTransition], None] | None = None,
        shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        for value, label in (
            (initial_seconds, "initial"),
            (max_seconds, "maximum"),
            (probe_wait_seconds, "probe wait"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"semantic network cooldown {label} delay must be finite and positive")
        if max_seconds < initial_seconds:
            raise ValueError("semantic network cooldown maximum is below its initial delay")
        if on_transition is not None and not callable(on_transition):
            raise ValueError("semantic network cooldown observer is invalid")
        if not callable(shutdown_requested):
            raise ValueError("semantic network cooldown shutdown signal is invalid")
        self._initial = float(initial_seconds)
        self._maximum = float(max_seconds)
        self._probe_wait = float(probe_wait_seconds)
        self._monotonic = monotonic
        self._on_transition = on_transition
        self._shutdown_requested = shutdown_requested
        self._lock = threading.Lock()
        self._outages = 0  # consecutive network outages; zero means closed
        self._opened_at = 0.0
        self._open_until = 0.0
        self._probe: object | None = None

    def shutdown_requested(self) -> bool:
        """Whether provider calls were stopped for this process (an operator stop)."""

        return bool(self._shutdown_requested())

    def pending_seconds(self) -> float | None:
        """Seconds to keep waiting, or ``None`` when a call may be admitted now."""

        with self._lock:
            if self._outages == 0:
                return None
            now = self._monotonic()
            if now < self._open_until:
                return self._open_until - now
            return None if self._probe is None else self._probe_wait

    def admit(self, owner: object) -> float | None:
        """Admit a group's first provider call, or return how long it waits."""

        with self._lock:
            if self._outages == 0 or self._probe is owner:
                return None
            now = self._monotonic()
            if now < self._open_until:
                return self._open_until - now
            if self._probe is None:
                self._probe = owner
                return None
            return self._probe_wait

    def admit_next(self, owner: object) -> float | None:
        """Admit an admitted group's next provider unless the circuit opened since."""

        with self._lock:
            if self._outages == 0 or self._probe is owner:
                return None
            now = self._monotonic()
            return self._open_until - now if now < self._open_until else self._probe_wait

    def record_outage(
        self,
        owner: object,
        *,
        group_hash: str,
        attempts: tuple[SemanticProviderAttempt, ...],
    ) -> float:
        """Open or extend the cooldown after a network outage; return the wait."""

        transition: SemanticNetworkTransition | None = None
        with self._lock:
            now = self._monotonic()
            event: Literal["opened", "probe_failed"] | None
            if self._outages == 0:
                self._outages = 1
                self._opened_at = now
                self._open_until = now + self._delay(1)
                event = "opened"
            elif self._probe is owner:
                self._probe = None
                self._outages += 1
                self._open_until = now + self._delay(self._outages)
                event = "probe_failed"
            else:
                # Another group of the same outage: fresh evidence, no new step.
                self._open_until = max(self._open_until, now + self._delay(self._outages))
                event = None
            wait = self._open_until - now
            if event is not None:
                transition = SemanticNetworkTransition(
                    event=event,
                    consecutive_outages=self._outages,
                    outage_seconds=now - self._opened_at,
                    retry_after_seconds=wait,
                    group_hash=group_hash,
                    reasons=tuple(
                        (attempt.provider.provider_id, attempt.reason_code or "")
                        for attempt in attempts
                    ),
                )
        self._notify(transition)
        return wait

    def record_reachable(self) -> None:
        """Close the circuit because a provider answered."""

        with self._lock:
            if self._outages == 0:
                return
            now = self._monotonic()
            transition = SemanticNetworkTransition(
                event="closed",
                consecutive_outages=self._outages,
                outage_seconds=now - self._opened_at,
            )
            self._outages = 0
            self._opened_at = 0.0
            self._open_until = 0.0
            self._probe = None
        self._notify(transition)

    def release(self, owner: object) -> None:
        """End a group's admission without a verdict (cancelled, failed or cached)."""

        with self._lock:
            if self._probe is owner:
                self._probe = None

    def _delay(self, outages: int) -> float:
        return min(self._maximum, self._initial * 2 ** min(outages - 1, 30))

    def _notify(self, transition: SemanticNetworkTransition | None) -> None:
        if transition is None or self._on_transition is None:
            return
        try:
            self._on_transition(transition)
        except Exception:  # noqa: BLE001 - visibility only: a failing sink never changes the wait
            pass


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
        network_cooldown: SharedSemanticNetworkCooldown | None = None,
    ) -> None:
        if not providers:
            raise ValueError("semantic provider chain cannot be empty")
        if policy_version != SEMANTIC_FAILOVER_POLICY_VERSION:
            raise ValueError("semantic failover policy is unsupported")
        identities = tuple(item.adapter.provider_identity for item in providers)
        provider_ids = tuple(item.provider_id for item in identities)
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("semantic provider chain repeats a provider id")
        if network_cooldown is not None and not isinstance(
            network_cooldown, SharedSemanticNetworkCooldown
        ):
            raise ValueError("semantic network cooldown is invalid")
        self._providers = providers
        self._policy_version = policy_version
        self._identities = identities
        # Only a scheduled caller that can run the same group again composes a
        # cooldown; without one a network outage stays degraded_unavailable.
        self._network_cooldown = network_cooldown

    @property
    def provider_identities(self) -> tuple[SemanticProviderIdentity, ...]:
        return self._identities

    def adjudicate(
        self,
        batch: SemanticAdjudicationBatch,
        *,
        group_hash: str,
        stage_guard: SemanticExecutionGuard | None = None,
        validate: SemanticDecisionValidator | None = None,
    ) -> SemanticAdjudicationOutcome:
        # Measurement only: one group_started/group_ended pair around the
        # real group boundary, whatever path the adjudication takes.
        note_stage(stage_guard, "group_started", group_hash=group_hash, providers=len(self._providers))
        try:
            outcome = self._adjudicate(
                batch, group_hash=group_hash, stage_guard=stage_guard, validate=validate
            )
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
        validate: SemanticDecisionValidator | None,
    ) -> SemanticAdjudicationOutcome:
        # The token names this group's cooldown admission; a probe it holds is
        # released on every exit that did not already record a verdict.
        owner = object()
        try:
            return self._run_chain(
                batch, group_hash=group_hash, stage_guard=stage_guard, validate=validate, owner=owner,
            )
        finally:
            if self._network_cooldown is not None:
                self._network_cooldown.release(owner)

    def _run_chain(
        self,
        batch: SemanticAdjudicationBatch,
        *,
        group_hash: str,
        stage_guard: SemanticExecutionGuard | None,
        validate: SemanticDecisionValidator | None,
        owner: object,
    ) -> SemanticAdjudicationOutcome:
        cooldown = self._network_cooldown
        admitted = False
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
                    if validate is not None:
                        try:
                            validate(cached.decisions)
                        except SemanticRouteContractError as exc:
                            # Entries are written only after this validation, so
                            # a stored result that no longer routes is tampering
                            # or unversioned drift.  It is never served, retried
                            # on another provider or quarantined: the entry stays
                            # untouched as evidence and no cache hit is claimed.
                            reason_code = _validation_reason_code(exc)
                            note_stage(stage_guard, "cache_hit_invalid", group_hash=group_hash, ordinal=ordinal,
                                       provider_id=identity.provider_id, reason_code=reason_code)
                            raise _rejected_result(
                                f"semantic group cache entry failed routing validation: {exc}",
                                reason_code=reason_code,
                                attempts=attempts,
                                ordinal=ordinal,
                                identity=identity,
                                cache_key=cache_key,
                            ) from exc
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
                if cooldown is not None:
                    # Only a call that needs the network waits: cached groups
                    # above never touch the cooldown.
                    wait = cooldown.admit_next(owner) if admitted else cooldown.admit(owner)
                    if wait is not None:
                        note_stage(stage_guard, "network_cooldown_wait", group_hash=group_hash, ordinal=ordinal,
                                   provider_id=identity.provider_id)
                        raise SemanticNetworkUnavailable(
                            "semantic providers wait for the network to return after an outage",
                            retry_after_seconds=wait,
                            attempts=tuple(attempts),
                        )
                    admitted = True
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
                    if call_outcome == "failed_closed":
                        # An already recognized typed fault takes precedence
                        # over a concurrent revocation: raise it before any
                        # guard checkpoint can replace it with lease loss.
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
                    # Cancellation and availability still check the live
                    # guard first: a revoked or expired stage never falls back
                    # to the next provider or accepts a late answer.
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
                if cooldown is not None:
                    # A provider answered, so the network is back whatever the
                    # validation below decides about the answer.
                    cooldown.record_reachable()
                if validate is not None:
                    try:
                        validate(result.decisions)
                    except SemanticRouteContractError as exc:
                        # A protocol-valid answer that cannot be routed is a
                        # failed-closed attempt of this call: raised before any
                        # cache write or guard checkpoint and never retried on
                        # another provider.
                        reason_code = _validation_reason_code(exc)
                        note_stage(stage_guard, "provider_call_ended", group_hash=group_hash, ordinal=ordinal,
                                   provider_id=identity.provider_id, outcome="failed_closed",
                                   reason_code=reason_code)
                        raise _rejected_result(
                            str(exc),
                            reason_code=reason_code,
                            attempts=attempts,
                            ordinal=ordinal,
                            identity=identity,
                            cache_key=cache_key,
                        ) from exc
                    except BaseException as exc:
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
        if cooldown is not None and _is_network_outage(attempts):
            # Nothing is decided, cached or receipted for this group: the
            # scheduled caller runs the same group again after the wait.
            wait = cooldown.record_outage(owner, group_hash=group_hash, attempts=tuple(attempts))
            if stage_guard is not None:
                stage_guard.checkpoint()
            raise SemanticNetworkUnavailable(
                "no semantic provider answered and at least one was unreachable over the network",
                retry_after_seconds=wait,
                attempts=tuple(attempts),
            )
        # A degraded group proves nothing about the network: only an answer
        # closes the cooldown, and a probe that degraded is just released.
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

    if isinstance(exc, SemanticNetworkUnavailable):
        return "network_wait"
    if isinstance(exc, SemanticRouteAdjudicatorError):
        return "cancelled" if exc.reason_code == _CANCELLED_REASON_CODE else "failed_closed"
    if type(exc).__name__ == "StageLeaseLost":
        return "lease_lost"
    return "error:" + type(exc).__name__


def _is_network_outage(attempts: list[SemanticProviderAttempt]) -> bool:
    return (
        bool(attempts)
        and all(attempt.outcome == "availability_failed" for attempt in attempts)
        and any(attempt.reason_code == _NETWORK_REASON_CODE for attempt in attempts)
    )


def _validation_reason_code(exc: SemanticRouteContractError) -> str:
    # Decisions that do not name exactly the requested Units break the result
    # contract itself; any other rejection is a decision the router cannot route.
    if isinstance(exc, SemanticDecisionCoverageError):
        return _INVALID_CONTRACT_REASON_CODE
    return _INVALID_DECISION_REASON_CODE


def _rejected_result(
    message: str,
    *,
    reason_code: str,
    attempts: list[SemanticProviderAttempt],
    ordinal: int,
    identity: SemanticProviderIdentity,
    cache_key: str,
) -> SemanticRouteAdjudicatorError:
    return SemanticRouteAdjudicatorError(
        message,
        reason_code=reason_code,
        retryable=False,
        attempts=(
            *attempts,
            SemanticProviderAttempt(
                ordinal=ordinal,
                provider=identity,
                outcome="failed_closed",
                reason_code=reason_code,
                cache_key=cache_key,
            ),
        ),
    )


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
    "SemanticNetworkTransition",
    "SharedSemanticNetworkCooldown",
    "semantic_group_cache_key",
]
