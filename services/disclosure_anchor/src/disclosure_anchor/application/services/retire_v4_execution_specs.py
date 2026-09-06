"""Finite explicitly selected legacy files; no resident sweep or time-based GC."""

from collections.abc import Callable

from disclosure_anchor.application.contracts.v4_prepared_execution_spec import V4PreparedExecutionSpec
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.ports.v4_execution_spec_catalog import (
    V4ExecutionSpecCatalogReference, V4LegacyExecutionSpecRetirementPort,
)


def retire_v4_execution_specs_once(
    *, uow_factory: Callable[[], UnitOfWork], files: V4LegacyExecutionSpecRetirementPort,
    references: tuple[V4ExecutionSpecCatalogReference, ...], ownership_guard: Callable[[], None],
) -> int:
    if type(references) is not tuple or not 1 <= len(references) <= 100 or len(set(references)) != len(references):
        raise ValueError("retirement requires 1..100 distinct exact spec references")
    deleted = 0
    for reference in references:
        ownership_guard()
        with uow_factory() as uow:
            uow.remote_parse_v4.require_execution_spec_cutover()

            def authorize(spec: V4PreparedExecutionSpec) -> None:
                ownership_guard()
                uow.remote_parse_v4.require_legacy_execution_spec_retirable(spec)
                ownership_guard()

            # No SQL mutation/commit. Physical old-writer exit is a caller precondition.
            deleted += int(files.retire_exact(reference=reference, authorize=authorize))
    return deleted
