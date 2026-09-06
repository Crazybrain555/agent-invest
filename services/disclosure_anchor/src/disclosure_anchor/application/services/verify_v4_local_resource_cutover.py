"""Bounded-memory all-history barrier, once per resident start, never per tick."""

from collections.abc import Callable
from typing import Protocol

from disclosure_anchor.application.ports.remote_parse_v4_repository import V4HistoricalLocalResources
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork


class V4LocalResourceHistoryInspector(Protocol):
    def verify_historical_local_resources(self, history: V4HistoricalLocalResources) -> None: ...


def verify_v4_local_resource_cutover(
    *, uow_factory: Callable[[], UnitOfWork], inspector: V4LocalResourceHistoryInspector,
    ownership_guard: Callable[[], None], page_size: int = 100,
) -> None:
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise ValueError("historical local resource page must be 1..100")
    cursor = None
    while True:
        ownership_guard()
        with uow_factory() as uow:
            page = uow.remote_parse_v4.list_historical_local_resources(
                after_attempt_id=cursor, limit=page_size,
            )
        for item in page:
            ownership_guard()
            if cursor is not None and item.intent.attempt_id <= cursor:
                raise ValueError("historical local resource cursor did not advance")
            inspector.verify_historical_local_resources(item)
            cursor = item.intent.attempt_id
        if len(page) < page_size:
            ownership_guard()
            return
