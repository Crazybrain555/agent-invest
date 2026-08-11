"""Create a deterministic provider-native Unit rebuild without rerunning MinerU."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NoReturn

from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.worker.locks import exclusive_document_producer
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.entities import outbox_events
from disclosure_anchor.domain.errors import BuildUnitsError


@dataclass(frozen=True)
class RebuildUnitsCommand:
    document_id: str


@dataclass(frozen=True)
class RebuildUnitsResult:
    processing_run_id: str
    source_processing_run_id: str
    status: str


class RebuildUnits:
    """Alias one succeeded self-owned provider parse for a new Unit build."""

    def __init__(self, *, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def execute(self, command: RebuildUnitsCommand) -> RebuildUnitsResult:
        with exclusive_document_producer(self._uow_factory, command.document_id):
            return self._execute_owned(command)

    def _execute_owned(self, command: RebuildUnitsCommand) -> RebuildUnitsResult:
        now = datetime.now(timezone.utc)
        with self._uow_factory() as uow:
            document = uow.documents.get(command.document_id)
            if document is None:
                self._fail("DOCUMENT_NOT_FOUND", command.document_id)
            candidate = uow.processing_runs.latest_succeeded_provider_run_for_document(
                command.document_id
            )
            if candidate is None:
                self._fail("NO_SUCCEEDED_PARSE_RUN", command.document_id)
            owner = uow.processing_runs.get(
                candidate.artifact_owner_processing_run_id
            )
            if not self._valid_owner(
                document_id=command.document_id,
                candidate=candidate,
                owner=owner,
            ):
                self._fail("ARTIFACT_OWNER_INVALID", command.document_id)
            assert owner is not None
            run = uow.processing_runs.add(
                e.ProcessingRun(
                    processing_run_id=ids.new_processing_run_id(),
                    document_id=command.document_id,
                    artifact_owner_processing_run_id=owner.processing_run_id,
                    run_kind="rebuild_units",
                    status="succeeded",
                    parser_name=owner.parser_name,
                    parser_version=owner.parser_version,
                    parser_backend=owner.parser_backend,
                    parser_method=owner.parser_method,
                    parser_language=owner.parser_language,
                    parser_target_identity=owner.parser_target_identity,
                    input_raw_file_hash=owner.input_raw_file_hash,
                    parser_artifact_relpath=owner.parser_artifact_relpath,
                    artifact_hash=owner.artifact_hash,
                    normalized_ir_relpath=None,
                    provider_document_relpath=owner.provider_document_relpath,
                    started_at=now,
                    finished_at=now,
                )
            )
            uow.outbox.add(
                outbox_events.processing_run_created(
                    document_id=command.document_id,
                    processing_run_id=run.processing_run_id,
                    occurred_at=now,
                    status="succeeded",
                )
            )
            uow.commit()
        return RebuildUnitsResult(
            processing_run_id=run.processing_run_id,
            source_processing_run_id=owner.processing_run_id,
            status=run.status,
        )

    @staticmethod
    def _valid_owner(
        *,
        document_id: str,
        candidate: e.ProcessingRun,
        owner: e.ProcessingRun | None,
    ) -> bool:
        if owner is None:
            return False
        copied = (
            "input_raw_file_hash",
            "parser_artifact_relpath",
            "artifact_hash",
            "provider_document_relpath",
            "parser_name",
            "parser_version",
            "parser_backend",
            "parser_method",
            "parser_language",
            "parser_target_identity",
        )
        return (
            candidate.document_id == document_id
            and candidate.run_kind in {"parse", "rebuild_units"}
            and candidate.status == "succeeded"
            and candidate.normalized_ir_relpath is None
            and candidate.provider_document_relpath is not None
            and owner.document_id == document_id
            and owner.run_kind == "parse"
            and owner.status == "succeeded"
            and owner.artifact_owner_processing_run_id == owner.processing_run_id
            and owner.normalized_ir_relpath is None
            and owner.provider_document_relpath is not None
            and candidate.artifact_owner_processing_run_id
            == owner.processing_run_id
            and all(getattr(candidate, field) == getattr(owner, field) for field in copied)
        )

    @staticmethod
    def _fail(error_code: str, document_id: str) -> NoReturn:
        raise BuildUnitsError(
            {
                "stage": "rebuild_units",
                "error_code": error_code,
                "retryable": False,
                "document_id": document_id,
            }
        )


__all__ = ["RebuildUnits", "RebuildUnitsCommand", "RebuildUnitsResult"]
