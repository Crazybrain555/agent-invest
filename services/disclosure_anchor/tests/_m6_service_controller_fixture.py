"""Independent synthetic service observations and finite real-thread barriers.

No receipt here claims an actual PDF, GPU task, disposal, ACK, or M6 credit.
Events express ordering; their timeout is only a failing-test cleanup bound.
"""

from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from threading import Condition, Event, Thread
from typing import Any

from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.services.m6_service_controller import (
    ServiceCompletion,
    ServiceWork,
    run_service_controller,
)


DIMENSIONS_EXCEPT_DOCUMENTS = (
    "snapshot_items", "snapshot_bytes", "remote_waits", "provider_tasks",
    "provider_result_bytes", "materialization_items", "compressed_bytes",
    "decoded_bytes", "temp_disk_bytes", "output_items", "output_bytes",
    "output_pages", "ack_items",
)
DISPOSAL_SHA = "sha256:" + "d" * 64


def work(attempt: str, *, retained: int = 0, **credits: int) -> ServiceWork:
    return ServiceWork(
        attempt, ResourceCreditVector(documents=1, **credits),
        ResourceCreditVector(temp_disk_bytes=retained),
    )


def completion(attempt: str, outcome: str = "completed") -> ServiceCompletion:
    return ServiceCompletion(attempt, outcome, DISPOSAL_SHA)  # type: ignore[arg-type]


def await_event(event: Event) -> None:
    if not event.wait(3):
        raise AssertionError("independent test barrier did not arrive within 3 seconds")


class Ledger:
    def __init__(self) -> None:
        self._condition = Condition()
        self._rows: list[tuple[object, ...]] = []

    def add(self, *row: object) -> None:
        with self._condition:
            self._rows.append(row)
            self._condition.notify_all()

    @property
    def rows(self) -> tuple[tuple[object, ...], ...]:
        with self._condition:
            return tuple(self._rows)

    def until(self, predicate: Callable[[tuple[tuple[object, ...], ...]], bool]) -> None:
        with self._condition:
            if not self._condition.wait_for(lambda: predicate(tuple(self._rows)), timeout=3):
                raise AssertionError(f"independent ledger barrier not reached: {self._rows!r}")


class TrackingPool(ThreadPoolExecutor):
    """Real executor, with observable submit/shutdown boundaries only."""

    def __init__(self, *args: Any, ledger: Ledger,
                 raise_after_submit: tuple[str, BaseException] | None = None,
                 shutdown_entered: Event | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ledger = ledger
        self.raise_after_submit = raise_after_submit
        self.shutdown_entered = shutdown_entered

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
        attempt = args[0].attempt_id
        self.ledger.add("submit", attempt)
        future = super().submit(fn, *args, **kwargs)
        if self.raise_after_submit is not None and attempt == self.raise_after_submit[0]:
            raise self.raise_after_submit[1]
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self.ledger.add("shutdown", wait, cancel_futures)
        if self.shutdown_entered is not None:
            self.shutdown_entered.set()
        super().shutdown(wait=wait, cancel_futures=cancel_futures)


class ControllerCall:
    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.done = Event()
        self.value: Any = None
        self.error: BaseException | None = None

        def target() -> None:
            try:
                self.value = run_service_controller(**kwargs)
            except BaseException as exc:
                self.error = exc
            finally:
                self.done.set()

        self.thread = Thread(target=target, name="independent-controller", daemon=True)
        self.thread.start()

    def finish(self) -> None:
        await_event(self.done)
        self.thread.join(1)
        if self.thread.is_alive():
            raise AssertionError("held controller thread did not close")


@contextmanager
def running(*, release: tuple[Event, ...] = (), **kwargs: Any) -> Iterator[ControllerCall]:
    call = ControllerCall(kwargs)
    try:
        yield call
    finally:
        for event in release:
            event.set()
        call.thread.join(4)
        if call.thread.is_alive():
            raise AssertionError("controller thread survived finite independent cleanup")


def original_errors(error: BaseException | None) -> tuple[BaseException, ...]:
    if error is None:
        return ()
    if isinstance(error, BaseExceptionGroup):
        return tuple(item for child in error.exceptions for item in original_errors(child))
    return (error,)
