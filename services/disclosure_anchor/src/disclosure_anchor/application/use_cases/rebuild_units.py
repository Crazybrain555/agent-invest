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
            owner = uow.processing_runs.get(source.artifact_owner_processing_run_id)
            if (
                owner is None
                or owner.document_id != document.document_id
                or owner.run_kind != "parse"
                or owner.artifact_owner_processing_run_id != owner.processing_run_id
                or owner.status != "succeeded"
                or owner.normalized_ir_relpath != source.normalized_ir_relpath
                or owner.parser_artifact_relpath != source.parser_artifact_relpath
                or owner.artifact_hash != source.artifact_hash
            ):
                raise BuildUnitsError(
                    {
                        "stage": "rebuild_units",
                        "error_code": "ARTIFACT_OWNER_INVALID",
                        "retryable": False,
                        "document_id": command.document_id,
                    }
                )
            run = uow.processing_runs.add(
                e.ProcessingRun(
                    processing_run_id=ids.new_processing_run_id(),
                    document_id=document.document_id,
                    artifact_owner_processing_run_id=(owner.processing_run_id),
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
                    normalized_ir_relpath=owner.normalized_ir_relpath,
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
