"""Rebuild document units from an existing parse run's artifacts.

Rule-bundle changes only affect the build stage (S1-S8), so re-running MinerU
for every rules iteration wastes ~13 GPU-minutes per annual report. A rebuild
run copies the parser provenance and artifact references from the latest
succeeded parse run and goes straight to build+publish (05 §2 U1
run_kind='rebuild_units', implemented 2026-07-06 on user direction).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
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
    """Create a succeeded rebuild run pointing at the source run's IR."""

    def __init__(self, *, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def execute(self, command: RebuildUnitsCommand) -> RebuildUnitsResult:
        now = datetime.now(timezone.utc)
        with self._uow_factory() as uow:
            document = uow.documents.get(command.document_id)
            if document is None:
                raise BuildUnitsError(
                    {
                        "stage": "rebuild_units",
                        "error_code": "DOCUMENT_NOT_FOUND",
                        "retryable": False,
                        "document_id": command.document_id,
                    }
                )
            source = uow.processing_runs.latest_succeeded_parse_for_document(
                command.document_id
            )
            if source is None or not source.normalized_ir_relpath:
                raise BuildUnitsError(
                    {
                        "stage": "rebuild_units",
                        "error_code": "NO_SUCCEEDED_PARSE_RUN",
                        "retryable": False,
                        "document_id": command.document_id,
                    }
                )
            run = uow.processing_runs.add(
                e.ProcessingRun(
                    processing_run_id=ids.new_processing_run_id(),
                    document_id=document.document_id,
                    run_kind="rebuild_units",
                    status="succeeded",
                    parser_name=source.parser_name,
                    parser_version=source.parser_version,
                    parser_backend=source.parser_backend,
                    parser_method=source.parser_method,
                    parser_language=source.parser_language,
                    input_raw_file_hash=source.input_raw_file_hash,
                    parser_artifact_relpath=source.parser_artifact_relpath,
                    artifact_hash=source.artifact_hash,
                    normalized_ir_relpath=source.normalized_ir_relpath,
                    started_at=now,
                    finished_at=now,
                )
            )
            uow.outbox.add(
                outbox_events.processing_run_created(
                    document_id=document.document_id,
                    processing_run_id=run.processing_run_id,
                    occurred_at=now,
                    status="succeeded",
                )
            )
            uow.commit()
        return RebuildUnitsResult(
            processing_run_id=run.processing_run_id,
            source_processing_run_id=source.processing_run_id,
            status=run.status,
        )
