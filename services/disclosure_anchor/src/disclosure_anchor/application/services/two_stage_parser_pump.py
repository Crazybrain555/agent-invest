"""Bounded remote-terminal/local-materialization coordinator.

This is deliberately mechanism-only.  It does not declare remote terminal to
be parse success: the caller receives success only after the independent local
call completes.  A durable checkpoint is mandatory before a remote receipt can
leave the remote pump.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar, cast

from disclosure_anchor.application.ports.staged_provider_parser import (
    RemoteArtifactReceipt,
)

LocalResultT = TypeVar("LocalResultT")


@dataclass(frozen=True, slots=True)
class TwoStageParseWork(Generic[LocalResultT]):
    sequence: int
    item_identity: str
    wait_remote_terminal: Callable[[], RemoteArtifactReceipt] | None
    persist_local: Callable[[RemoteArtifactReceipt], LocalResultT]
    checkpoint_remote_terminal: Callable[[RemoteArtifactReceipt], None]
    recovered_terminal: RemoteArtifactReceipt | None = None
    cancel_and_drain: Callable[[], None] = lambda: None

    def __post_init__(self) -> None:
        if self.sequence < 0 or not self.item_identity.strip():
            raise ValueError("two-stage work identity is invalid")
        if (self.wait_remote_terminal is None) == (self.recovered_terminal is None):
            raise ValueError(
                "work requires exactly one remote waiter or recovered terminal"
            )


@dataclass(frozen=True, slots=True)
class TwoStageParseOutcome(Generic[LocalResultT]):
    sequence: int
    item_identity: str
    status: Literal["succeeded", "remote_failed", "local_failed", "cancelled"]
    result: LocalResultT | None = None
    error: BaseException | None = None


class BoundedTwoStageParserPump(Generic[LocalResultT]):
    """Work-conserving two-stage pump with separate remote/local ownership."""

    def __init__(
        self,
        *,
        remote_workers: int,
        local_workers: int,
        max_terminal_receipts: int,
        max_local_items: int,
        max_local_bytes: int,
    ) -> None:
        values = (
            remote_workers,
            local_workers,
            max_terminal_receipts,
            max_local_items,
            max_local_bytes,
        )
        if any(value < 1 for value in values):
            raise ValueError("two-stage pump limits must be positive")
        self._remote_workers = remote_workers
        self._local_workers = local_workers
        self._max_terminal_receipts = max_terminal_receipts
        self._max_local_items = max_local_items
        self._max_local_bytes = max_local_bytes

    def run(
        self,
        work: Iterable[TwoStageParseWork[LocalResultT]],
        *,
        stop_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[TwoStageParseOutcome[LocalResultT], ...]:
        pending = deque(work)
        sequences = [item.sequence for item in pending]
        if len(sequences) != len(set(sequences)):
            raise ValueError("two-stage work sequences must be unique")
        outcomes: dict[int, TwoStageParseOutcome[LocalResultT]] = {}
        terminal: deque[
            tuple[TwoStageParseWork[LocalResultT], RemoteArtifactReceipt]
        ] = deque()
        remote: dict[
            Future[RemoteArtifactReceipt], TwoStageParseWork[LocalResultT]
        ] = {}
        local: dict[
            Future[LocalResultT],
            tuple[TwoStageParseWork[LocalResultT], int],
        ] = {}
        local_bytes_in_use = 0
        stopping = False

        def outcome(
            item: TwoStageParseWork[LocalResultT],
            status: Literal[
                "succeeded", "remote_failed", "local_failed", "cancelled"
            ],
            *,
            result: LocalResultT | None = None,
            error: BaseException | None = None,
        ) -> None:
            outcomes[item.sequence] = TwoStageParseOutcome(
                sequence=item.sequence,
                item_identity=item.item_identity,
                status=status,
                result=result,
                error=error,
            )

        def admit_remote(remote_pool: ThreadPoolExecutor) -> None:
            while (
                not stopping
                and pending
                and len(remote) < self._remote_workers
                and len(remote) + len(terminal) < self._max_terminal_receipts
            ):
                item = pending.popleft()
                if item.recovered_terminal is not None:
                    try:
                        item.checkpoint_remote_terminal(item.recovered_terminal)
                    except Exception as exc:  # noqa: BLE001 - stage failure is data
                        outcome(item, "remote_failed", error=exc)
                    else:
                        terminal.append((item, item.recovered_terminal))
                    continue
                waiter = item.wait_remote_terminal
                if waiter is None:  # guarded by TwoStageParseWork
                    raise AssertionError("remote waiter is absent")
                remote[remote_pool.submit(waiter)] = item

        with (
            ThreadPoolExecutor(
                max_workers=self._remote_workers,
                thread_name_prefix="parse-remote",
            ) as remote_pool,
            ThreadPoolExecutor(
                max_workers=self._local_workers,
                thread_name_prefix="parse-local",
            ) as local_pool,
        ):
            while pending or remote or terminal or local:
                if stop_requested():
                    stopping = True

                admit_remote(remote_pool)

                while (
                    terminal
                    and len(local) < min(self._local_workers, self._max_local_items)
                ):
                    item, receipt = terminal[0]
                    reserved = receipt.artifact_byte_count
                    if reserved > self._max_local_bytes:
                        terminal.popleft()
                        outcome(
                            item,
                            "local_failed",
                            error=ValueError(
                                "remote artifact exceeds local byte-credit limit"
                            ),
                        )
                        continue
                    if local_bytes_in_use + reserved > self._max_local_bytes:
                        break
                    terminal.popleft()
                    local_bytes_in_use += reserved
                    local[local_pool.submit(item.persist_local, receipt)] = (
                        item,
                        reserved,
                    )

                # Moving a terminal receipt into the independent local pump
                # returns its remote credit immediately. Refill before waiting
                # for the (potentially slow) local materialization Future.
                admit_remote(remote_pool)

                if stopping:
                    while pending:
                        outcome(pending.popleft(), "cancelled")
                    while terminal:
                        item, _receipt = terminal.popleft()
                        outcome(item, "cancelled")
                    for future, item in tuple(remote.items()):
                        if future.cancel():
                            remote.pop(future)
                            outcome(item, "cancelled")
                        else:
                            item.cancel_and_drain()

                futures = {
                    cast(Future[object], future)
                    for future in (*remote.keys(), *local.keys())
                }
                if not futures:
                    if terminal:
                        # Only byte credits can block here; active local work
                        # would have contributed a Future.
                        raise RuntimeError("two-stage local credit accounting deadlocked")
                    continue
                completed, _ = wait(
                    futures,
                    timeout=0.05,
                    return_when=FIRST_COMPLETED,
                )
                for completed_future in completed:
                    if completed_future in remote:
                        remote_future = cast(
                            Future[RemoteArtifactReceipt], completed_future
                        )
                        item = remote.pop(remote_future)  # remote credit returns here
                        try:
                            receipt = remote_future.result()
                            item.checkpoint_remote_terminal(receipt)
                        except Exception as exc:  # noqa: BLE001 - stage failure is data
                            outcome(
                                item,
                                "cancelled" if stopping else "remote_failed",
                                error=exc,
                            )
                        else:
                            if stopping:
                                outcome(item, "cancelled")
                            else:
                                terminal.append((item, receipt))
                    elif completed_future in local:
                        local_future = cast(Future[LocalResultT], completed_future)
                        item, reserved = local.pop(local_future)
                        local_bytes_in_use -= reserved
                        try:
                            result = local_future.result()
                        except Exception as exc:  # noqa: BLE001 - stage failure is data
                            outcome(item, "local_failed", error=exc)
                        else:
                            outcome(item, "succeeded", result=result)

        return tuple(outcomes[sequence] for sequence in sorted(outcomes))


__all__ = [
    "BoundedTwoStageParserPump",
    "TwoStageParseOutcome",
    "TwoStageParseWork",
]
