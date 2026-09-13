"""Finite service supply with resource backpressure and visible unresolved work.

The adapter executes the existing diagnostic lifecycle. This controller never
invents V4 claims, publication, quality or disposal. A worker exception retains
its entire reservation; only an actual adapter completion releases transient
resources. Diagnostic journals retained after disposal remain charged.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from math import isfinite
import re
from threading import Event
from typing import Literal

from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector


@dataclass(frozen=True, slots=True)
class ServiceWork:
    attempt_id: str
    reservation: ResourceCreditVector
    retained_after_disposal: ResourceCreditVector

    def __post_init__(self) -> None:
        if (type(self.attempt_id) is not str or not self.attempt_id
                or len(self.attempt_id) > 128
                or any(ord(c) <= 32 or ord(c) == 127 for c in self.attempt_id)):
            raise ValueError("service work requires an opaque attempt identity")
        if (type(self.reservation) is not ResourceCreditVector
                or type(self.retained_after_disposal) is not ResourceCreditVector
                or self.reservation.documents != 1
                or self.retained_after_disposal.documents != 0
                or not self.retained_after_disposal.fits(self.reservation)):
            raise ValueError("service reservation/retained resources are inconsistent")


@dataclass(frozen=True, slots=True)
class ServiceCompletion:
    """Adapter observation after local disposal and remote ACK reconciliation."""

    attempt_id: str
    outcome: Literal["completed", "failed"]
    disposal_receipt_sha256: str

    def __post_init__(self) -> None:
        if (type(self.attempt_id) is not str or not self.attempt_id
                or self.outcome not in {"completed", "failed"}
                or type(self.disposal_receipt_sha256) is not str
                or re.fullmatch(r"sha256:[0-9a-f]{64}", self.disposal_receipt_sha256) is None):
            raise ValueError("service completion requires an actual disposal reference")


@dataclass(frozen=True, slots=True)
class ServiceControllerResult:
    terminal: Literal["exhausted", "stopped", "failed"]
    completed: tuple[ServiceCompletion, ...]
    not_started: tuple[str, ...]
    unresolved: tuple[str, ...]
    retained_credits: ResourceCreditVector


class ServiceControllerFailure(RuntimeError):
    """Original exceptions are retained as the cause; results remain inspectable."""

    def __init__(self, result: ServiceControllerResult) -> None:
        super().__init__("service supply stopped with unresolved execution or observation")
        self.result = result


def run_service_controller(
    *, work: tuple[ServiceWork, ...], max_in_flight: int,
    credits_limit: ResourceCreditVector,
    execute: Callable[[ServiceWork, Callable[[], None]], ServiceCompletion],
    stop_requested: Callable[[], bool],
    before_submit: Callable[[], None],
    on_completion: Callable[[ServiceCompletion], None],
    on_dispatch: Callable[[ServiceWork], None] | None = None,
    poll_seconds: float = 0.1,
) -> ServiceControllerResult:
    """Refill on completion using oldest-fitting work, with no executor backlog.

    ``stop_requested``, ``on_dispatch`` and ``on_completion`` run on the
    controller thread. Dispatch persists an intent before executor submission;
    an exception preserves the full reservation as unresolved.
    ``before_submit`` runs on the executing thread and must be thread-safe;
    do not pass an owner-thread-affine M6OwnerClient method directly. It checks
    the adapter's current bounded grant at the actual POST boundary. The
    controller additionally latches stop/failure for every worker.

    Stop closes new admission and lets existing remote obligations drain. It
    does not cancel running futures. An adapter must preserve its original
    deadline and journal; this function grants no hard-kill or restart authority.
    ``on_completion`` persists/forwards the small completion reference, not a
    second copy of the diagnostic result. Exceptions there also close admission.
    """
    if (type(work) is not tuple or not 1 <= len(work) <= 10_000
            or any(type(item) is not ServiceWork for item in work)
            or len({item.attempt_id for item in work}) != len(work)
            or type(max_in_flight) is not int or not 1 <= max_in_flight <= 64
            or type(credits_limit) is not ResourceCreditVector
            or type(poll_seconds) not in (int, float) or not isfinite(poll_seconds)
            or not 0 < poll_seconds <= 1):
        raise ValueError("service controller inputs or finite bounds are invalid")
    if any(not item.reservation.fits(credits_limit) for item in work):
        raise ValueError("a service attempt cannot fit the declared resource envelope")

    stopped = Event()
    pending = list(work)
    active: dict[Future[ServiceCompletion], ServiceWork] = {}
    completed: dict[str, ServiceCompletion] = {}
    unresolved: set[str] = set()
    failures: list[BaseException] = []
    credits = ResourceCreditVector()

    def submission_guard() -> None:
        if stopped.is_set():
            raise RuntimeError("service admission is closed; preserve original prepared attempt")
        before_submit()
        if stopped.is_set():
            raise RuntimeError("service admission closed during its submission check")

    def harvest() -> None:
        nonlocal credits
        # Stable observation order is independent of the unordered wait set.
        for future, item in tuple(active.items()):
            if not future.done():
                continue
            del active[future]
            try:
                result = future.result()
                if type(result) is not ServiceCompletion or result.attempt_id != item.attempt_id:
                    raise ValueError("service completion belongs to a different attempt")
            except BaseException as exc:
                unresolved.add(item.attempt_id)
                failures.append(exc)
                stopped.set()
                continue
            completed[item.attempt_id] = result
            credits = credits - item.reservation + item.retained_after_disposal
            try:
                on_completion(result)
            except BaseException as exc:
                failures.append(exc)
                stopped.set()

    pool = ThreadPoolExecutor(max_workers=max_in_flight, thread_name_prefix="m6-service")
    try:
        while active or (pending and not stopped.is_set()):
            try:
                harvest()
                if not stopped.is_set():
                    stop = stop_requested()
                    if type(stop) is not bool:
                        raise TypeError("service stop observation must be boolean")
                    if stop:
                        stopped.set()
                while not stopped.is_set() and pending and len(active) < max_in_flight:
                    selected = next((i for i, item in enumerate(pending)
                                     if item.reservation.fits(credits_limit - credits)), None)
                    if selected is None:
                        if not active:
                            raise RuntimeError("retained service evidence exhausts the resource envelope")
                        break
                    item = pending.pop(selected)
                    # Charge before dispatch. Executor submission may enqueue
                    # before a thread-start failure, so absence of a returned
                    # Future does not prove absence of execution.
                    credits = credits + item.reservation
                    try:
                        if on_dispatch is not None:
                            on_dispatch(item)
                        future = pool.submit(execute, item, submission_guard)
                    except BaseException:
                        unresolved.add(item.attempt_id)
                        raise
                    active[future] = item
                    # Detect fast failure/stop before scheduling another item.
                    harvest()
                    if not stopped.is_set():
                        stop = stop_requested()
                        if type(stop) is not bool:
                            raise TypeError("service stop observation must be boolean")
                        if stop:
                            stopped.set()
                if active:
                    wait(tuple(active), timeout=poll_seconds, return_when=FIRST_COMPLETED)
            except BaseException as exc:
                failures.append(exc)
                stopped.set()
    finally:
        stopped.set()
        # A running Future cannot be cancelled. Join actual callbacks before
        # claiming their transient execution resources have closed.
        pool.shutdown(wait=True)
    harvest()
    result = ServiceControllerResult(
        terminal="failed" if failures else "stopped" if pending else "exhausted",
        completed=tuple(completed[item.attempt_id] for item in work if item.attempt_id in completed),
        not_started=tuple(item.attempt_id for item in pending),
        unresolved=tuple(item.attempt_id for item in work if item.attempt_id in unresolved),
        retained_credits=credits,
    )
    if failures:
        raise ServiceControllerFailure(result) from BaseExceptionGroup("original service failures", failures)
    return result
