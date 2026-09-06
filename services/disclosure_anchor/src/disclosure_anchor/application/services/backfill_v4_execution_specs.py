"""Explicit finite offline legacy-spec transfer; never a worker fallback."""

from collections.abc import Callable
from dataclasses import dataclass

from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.ports.v4_execution_spec_catalog import (
    V4ExecutionSpecCatalogPort, V4ExecutionSpecCatalogReference,
)


@dataclass(frozen=True, slots=True)
class V4ExecutionSpecBackfillResult:
    verified: int
    inserted: int
    after_attempt_id: str | None
    exhausted: bool


def backfill_v4_execution_specs_once(
    *, uow_factory: Callable[[], UnitOfWork], legacy: V4ExecutionSpecCatalogPort,
    after_attempt_id: str | None, limit: int, write_guard: Callable[[], None],
) -> V4ExecutionSpecBackfillResult:
    """Caller proves actual old-writer drain; lock ownership alone is not drain.

    A commit-response loss is resolved by replaying the same cursor. No files
    are deleted here, and every existing PG row is verified without FS fallback.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("execution spec backfill limit must be 1..100")
    write_guard()
    with uow_factory() as uow:
        page = uow.remote_parse_v4.list_execution_spec_backfill(
            after_attempt_id=after_attempt_id, limit=limit,
        )
        inserted = 0
        cursor = after_attempt_id
        for candidate in page:
            write_guard()
            preparation = candidate.preparation
            spec = candidate.execution_spec
            if spec is None:
                spec = legacy.load(reference=V4ExecutionSpecCatalogReference(
                    spec_sha256=preparation.execution_spec_sha256,
                    byte_count=preparation.execution_spec_byte_count,
                ))
                write_guard()
                uow.remote_parse_v4.backfill_execution_spec(
                    attempt_id=preparation.attempt_id, spec=spec,
                )
                inserted += 1
            cursor = preparation.attempt_id
        write_guard()
        uow.commit()
    return V4ExecutionSpecBackfillResult(len(page), inserted, cursor, len(page) < limit)
