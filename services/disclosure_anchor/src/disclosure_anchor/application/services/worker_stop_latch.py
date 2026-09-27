"""Process-local first-cause latch for the worker stop control.

The latch is the synchronous half of a public stop: it records the first cause
under a short lock, makes the halt visible to every plane and runs the wake
callbacks before anything slow happens. Durable persistence is an optional hook
run once by the first caller after the halt; later callers never wait for it.

Used alone, it is the compatibility control for coordinators composed without a
durable adapter (tests and embedded callers): it still halts and records the
cause, it just persists nothing.
"""

from __future__ import annotations

from collections.abc import Callable
import sys
import threading

from disclosure_anchor.application.ports.worker_stop_control import PublicStopCause


class InProcessWorkerStopLatch:
    """Thread-safe once-per-process latch; the first cause is immutable."""

    def __init__(
        self,
        *,
        on_first_trip: Callable[[PublicStopCause], None] | None = None,
    ) -> None:
        if on_first_trip is not None and not callable(on_first_trip):
            raise ValueError("stop latch persistence hook is not callable")
        self._lock = threading.Lock()
        self._halted = threading.Event()
        self._cause: PublicStopCause | None = None
        self._wake: list[Callable[[], None]] = []
        self._on_first_trip = on_first_trip

    def trip(self, cause: PublicStopCause) -> bool:
        if type(cause) is not PublicStopCause:
            raise TypeError("worker stop cause must be a PublicStopCause")
        with self._lock:
            first = self._cause is None
            if first:
                self._cause = cause
                callbacks = tuple(self._wake)
        # The halt is visible before any callback or durable IO, and a later
        # caller returns without waiting behind the first caller's persistence.
        self._halted.set()
        if not first:
            return False
        try:
            for callback in callbacks:
                _call_quietly(callback, "wake callback")
        finally:
            # Persistence runs once even if a wake callback escapes with a
            # BaseException; that exception then propagates unchanged.
            hook = self._on_first_trip
            if hook is not None:
                _call_quietly(lambda: hook(cause), "persistence hook")
        return True

    def is_tripped(self) -> bool:
        return self._halted.is_set()

    def first_cause(self) -> PublicStopCause | None:
        with self._lock:
            return self._cause

    def add_wake_callback(self, callback: Callable[[], None]) -> None:
        if not callable(callback):
            raise ValueError("stop latch wake callback is not callable")
        with self._lock:
            tripped = self._cause is not None
            if not tripped:
                self._wake.append(callback)
        if tripped:
            _call_quietly(callback, "wake callback")

    def wait_halted(self, timeout: float | None = None) -> bool:
        return self._halted.wait(timeout)


def _call_quietly(action: Callable[[], object], label: str) -> None:
    # The first cause is already latched and halting; a failing callback must
    # neither replace the original fault nor stay invisible.
    try:
        action()
    except Exception as exc:  # noqa: BLE001 - reported, never masks the latched cause
        try:
            print(
                f"[worker-control] stop {label} failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        except Exception:  # noqa: BLE001 - reporting is visibility only
            # A closed/broken stderr or a failing __str__ never skips the
            # remaining wake callbacks or the persistence hook.
            pass


__all__ = ["InProcessWorkerStopLatch"]
