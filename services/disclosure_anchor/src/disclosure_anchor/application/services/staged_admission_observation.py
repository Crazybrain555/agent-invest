"""Single bounded admission observation on the existing preflight executor.

The coordinator charges ``credits`` and ``active_slots`` alongside durable
stage owners. Only ``observe`` runs off-controller; accepting the result and
all H0/claim/database work stay on the controller. Nothing is a second backlog.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Protocol

from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.ports.staged_new_work_v4 import (
    V4AdmissionObservationPort,
    V4AdmissionObservationRequest,
    V4AdmissionObservationResult,
)
from disclosure_anchor.application.ports.staged_provider_parser import V4StageGuard
from disclosure_anchor.application.services.staged_execution_guard import StageLeaseLost


class _RevocableGuard(V4StageGuard, Protocol):
    def revoke(self) -> None: ...


class StagedAdmissionObservation:
    def __init__(self, observer: V4AdmissionObservationPort | None) -> None:
        self._observer = observer
        self._request: V4AdmissionObservationRequest | None = None
        self._future: Future[V4AdmissionObservationResult] | None = None
        self._guard: _RevocableGuard | None = None
        self._discard = False
        self._error_observed = False

    @property
    def pending(self) -> bool:
        return self._request is not None

    @property
    def active_slots(self) -> int:
        return int(self._future is not None)

    @property
    def credits(self) -> ResourceCreditVector:
        return self._request.credits if self._request is not None else ResourceCreditVector()

    def enqueue(self, request: V4AdmissionObservationRequest) -> None:
        if self.pending or self._observer is None or type(request) is not V4AdmissionObservationRequest:
            raise RuntimeError("admission observation lacks a free bounded owner")
        self._request = request
        self._discard = False

    def dispatch(self, executor: ThreadPoolExecutor, *, stage_guard: _RevocableGuard) -> None:
        if self._request is None or self._future is not None or self._discard:
            return
        assert self._observer is not None
        self._guard = stage_guard
        self._future = executor.submit(
            self._observer.observe, self._request, stage_guard=stage_guard,
        )

    def collect(self) -> bool:
        if self._future is None or not self._future.done():
            return False
        assert self._observer is not None
        assert self._request is not None
        # Keep the reservation if result validation fails; the caller's final
        # drain owns cleanup, including errors from an already-finished future.
        try:
            result = self._future.result()
        except BaseException as exc:
            self._error_observed = True
            if not (self._discard and isinstance(exc, StageLeaseLost)):
                raise
        else:
            if type(result) is not V4AdmissionObservationResult or result.request != self._request:
                raise RuntimeError("admission observation returned another request")
            if not self._discard:
                self._observer.accept_observation(result)
        if self._discard:
            self._observer.abandon_observation(self._request)
        self._clear()
        return True

    def cancel(self) -> None:
        if self._request is None:
            return
        self._discard = True
        if self._guard is not None:
            self._guard.revoke()
        if self._future is None:
            assert self._observer is not None
            self._observer.abandon_observation(self._request)
            self._clear()

    def drained(self) -> None:
        """Called only after the shared executor actually joined its owners."""
        if self._future is not None and not self._future.done():
            raise RuntimeError("admission observation has not actually drained")
        if self._request is not None:
            assert self._observer is not None
            self._observer.abandon_observation(self._request)
        if self._future is not None and not self._error_observed:
            try:
                self._future.result()
            except StageLeaseLost:
                if not self._discard:
                    raise
        self._clear()

    def _clear(self) -> None:
        self._request = None
        self._future = None
        self._guard = None
        self._discard = False
        self._error_observed = False


__all__ = ["StagedAdmissionObservation"]
