"""Bounded commit/reconciliation loops for frozen V4 ingress/disposition."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from disclosure_anchor.application.ports.remote_parse_v4_ingress import (
    V4InitialIngressCommit, V4InitialIngressNotCommitted,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import RemoteParseV4Authority
from disclosure_anchor.application.ports.remote_parse_v4_source_rejection import V4SourceRejectionCommit
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork


_T = TypeVar("_T")


class StagedIngressResponseLost(RuntimeError):
    """The bounded ingress write could not be closed by exact reconciliation."""


class DurableStagedIngressV4:
    def __init__(self, *, uow_factory: Callable[[], UnitOfWork]) -> None:
        if not callable(uow_factory):
            raise ValueError("staged ingress requires a unit-of-work factory")
        self._uow_factory = uow_factory

    def execute(
        self, command: V4InitialIngressCommit, *, write_guard: Callable[[], None],
    ) -> RemoteParseV4Authority:
        if type(command) is not V4InitialIngressCommit:
            raise ValueError("staged ingress command must be exact")
        return self._commit_or_resolve(
            label=command.proposal.attempt_id, write_guard=write_guard,
            mutate=lambda uow: uow.remote_parse_v4_ingress.commit(command),
            reconcile=lambda uow: uow.remote_parse_v4_ingress.reconcile(command).authority,
        )

    def reject_source(
        self, command: V4SourceRejectionCommit, *, write_guard: Callable[[], None],
    ) -> None:
        if type(command) is not V4SourceRejectionCommit:
            raise ValueError("staged source rejection command must be exact")
        self._commit_or_resolve(
            label=command.processing_run_id, write_guard=write_guard,
            mutate=lambda uow: uow.remote_parse_v4_ingress.reject_source(command),
            reconcile=lambda uow: uow.remote_parse_v4_ingress.reconcile_source_rejection(command),
        )

    def _commit_or_resolve(
        self, *, label: str, mutate: Callable[[UnitOfWork], _T],
        reconcile: Callable[[UnitOfWork], _T], write_guard: Callable[[], None],
    ) -> _T:
        if not callable(write_guard):
            raise ValueError("staged ingress requires an explicit write guard")
        last_unknown: Exception | None = None
        for write_number in range(2):
            write_guard()
            mutation_returned = False
            try:
                with self._uow_factory() as uow:
                    result = mutate(uow)
                    mutation_returned = True
                    write_guard()
                    uow.commit()
                return result
            except Exception as exc:
                if not mutation_returned:
                    raise
                last_unknown = exc
                try:
                    with self._uow_factory() as uow:
                        # Already-committed outcome reconciliation is read-only
                        # and remains valid after readiness/ownership loss.
                        reconciled = reconcile(uow)
                except V4InitialIngressNotCommitted:
                    if write_number == 0:
                        continue
                    break
                except Exception as reconcile_exc:
                    raise StagedIngressResponseLost(
                        f"{label}: ingress outcome drifted"
                    ) from reconcile_exc
                return reconciled
        assert last_unknown is not None
        raise StagedIngressResponseLost(
            f"{label}: ingress remained absent after replay"
        ) from last_unknown


__all__ = ["DurableStagedIngressV4", "StagedIngressResponseLost"]
