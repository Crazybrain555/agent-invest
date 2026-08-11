"""Build document Units from one source-admitted provider document."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any, cast

from disclosure_anchor.application.contracts.provider_document_admission import (
    ProviderDocumentAdmissionError,
)
from disclosure_anchor.application.contracts.provider_unit import (
    PROVIDER_UNIT_BUILDER_VERSION,
    ProviderUnitBuildResult,
    ProviderUnitDraft,
    provider_unit_locator_to_payload,
)
from disclosure_anchor.application.ports.file_store import (
    ArtifactStorePort,
    ArtifactWriteResult,
    FileStorePathPort,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.provider_document_admission import (
    ProviderDocumentAdmission,
)
from disclosure_anchor.application.services.provider_unit_builder import (
    build_provider_units,
)
from disclosure_anchor.application.worker.locks import (
    exclusive_document_producer,
    maybe_lock_document,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import BuildUnitsError
from disclosure_anchor.domain.services.unit_hashing import (
    compute_unit_hashes,
    content_hash_aggregate,
    structure_hash_aggregate,
)
from disclosure_anchor.domain.value_objects.semantic_key import (
    SemanticKeyInvariantError,
    validate_semantic_key_state,
)


SNAPSHOT_KEYS = {
    "applicability",
    "page_no",
    "artifact_locator",
    "asset_id",
    "content_hash",
    "document_id",
    "heading_path",
    "order_index",
    "payload",
    "payload_kind",
    "quality_status",
    "semantic_key",
    "semantic_keys",
    "title",
}


@dataclass(frozen=True)
class BuildUnitsCommand:
    document_id: str | None = None
    processing_run_id: str | None = None


@dataclass(frozen=True)
class BuildUnitsResult:
    processing_run_id: str
    status: str
    unit_count: int = 0
    document_units_relpath: str | None = None
    build_stats: dict[str, Any] | None = None
    content_hash_aggregate: str | None = None
    structure_hash: str | None = None
    error: dict[str, Any] | None = None


class BuildUnits:
    """Materialize the deterministic provider projection under one document lock."""

    def __init__(
        self,
        *,
        path_builder: FileStorePathPort,
        artifact_store: ArtifactStorePort,
        uow_factory: Callable[[], UnitOfWork],
        admission: ProviderDocumentAdmission,
    ) -> None:
        self._paths = path_builder
        self._artifact_store = artifact_store
        self._uow_factory = uow_factory
        self._admission = admission

    def execute(self, command: BuildUnitsCommand) -> BuildUnitsResult:
        initial = self._load_context(command)
        run = cast(e.ProcessingRun, initial["run"])
        with exclusive_document_producer(self._uow_factory, run.document_id):
            return self._execute_owned(command)

    def _execute_owned(self, command: BuildUnitsCommand) -> BuildUnitsResult:
        context = self._load_context(command)
        run = cast(e.ProcessingRun, context["run"])
        try:
            admitted = self._admission.admit(
                document=cast(e.Document, context["document"]),
                run=run,
                artifact_owner=cast(e.ProcessingRun, context["artifact_owner"]),
                security_code=cast(str, context["security_code"]),
            )
            build = build_provider_units(admitted)
            if build.unassigned_table_parts:
                raise BuildUnitsError(
                    self._structured_error(
                        error_code="UNASSIGNED_TABLE_EVIDENCE",
                        message=(
                            "provider table evidence has no source-bound Unit owner; "
                            "the builder will not guess one"
                        ),
                    )
                )
            units, snapshot_rows = self._materialize_units(
                build=build,
                document=cast(e.Document, context["document"]),
                run=run,
            )
            stats = self._build_stats(build)
        except ProviderDocumentAdmissionError as exc:
            return self._mark_and_result(
                run.processing_run_id,
                self._structured_error(
                    error_code="PROVIDER_DOCUMENT_ADMISSION_FAILED",
                    reason_code=exc.reason_code,
                    retryable=exc.retryable,
                    message=str(exc),
                ),
            )
        except BuildUnitsError as exc:
            return self._mark_and_result(run.processing_run_id, exc.error)
        except (TypeError, ValueError) as exc:
            return self._mark_and_result(
                run.processing_run_id,
                self._structured_error(
                    error_code="PROVIDER_UNIT_PROJECTION_INVALID",
                    message=str(exc),
                ),
            )
        except OSError as exc:
            return self._mark_and_result(
                run.processing_run_id,
                self._structured_error(
                    error_code="ARTIFACT_READ_FAILED",
                    retryable=True,
                    message=str(exc),
                ),
            )
        except Exception as exc:
            error = self._structured_error(
                error_code="BUILD_PREPARATION_FAILED",
                message=(
                    "provider Unit preparation failed: "
                    f"{type(exc).__name__}: {str(exc)[:400]}"
                ),
            )
            self._mark_failed(run.processing_run_id, error)
            raise BuildUnitsError(error) from exc

        try:
            snapshot_relpath = self._snapshot_relpath(context)
            snapshot_result = self._artifact_store.write_jsonl_atomic(
                relpath=snapshot_relpath,
                rows=snapshot_rows,
            )
            self._verify_snapshot(
                relpath=snapshot_relpath,
                expected_rows=len(snapshot_rows),
                write_result=snapshot_result,
            )
            self._artifact_store.write_json_atomic(
                relpath=snapshot_relpath.parent / "build_stats.v1.json",
                payload=stats,
            )
        except BuildUnitsError as exc:
            return self._mark_and_result(run.processing_run_id, exc.error)
        except OSError as exc:
            return self._mark_and_result(
                run.processing_run_id,
                self._structured_error(
                    error_code="ARTIFACT_WRITE_FAILED",
                    retryable=True,
                    message=str(exc),
                ),
            )

        try:
            updated = self._persist_success(
                run_id=run.processing_run_id,
                units=units,
                snapshot_relpath=snapshot_relpath,
                content_aggregate=content_hash_aggregate(
                    unit.content_hash for unit in units
                ),
                structure_aggregate=structure_hash_aggregate(
                    cast(str, unit.structure_hash) for unit in units
                ),
            )
        except BuildUnitsError as exc:
            if exc.error.get("error_code") == "UNITS_ALREADY_BUILT":
                with self._uow_factory() as uow:
                    existing = uow.processing_runs.get(run.processing_run_id)
                return BuildUnitsResult(
                    processing_run_id=run.processing_run_id,
                    status=(existing.unit_build_status if existing else "failed"),
                    document_units_relpath=(
                        existing.document_units_relpath if existing else None
                    ),
                    error=exc.error,
                )
            error = self._structured_error(
                error_code="DB_WRITE_FAILED",
                retryable=True,
                message="document_unit DB persistence failed after snapshot write",
            )
            self._mark_failed(run.processing_run_id, error)
            raise BuildUnitsError(error) from exc
        except Exception as exc:
            error = self._structured_error(
                error_code="DB_WRITE_FAILED",
                retryable=True,
                message="document_unit DB persistence failed after snapshot write",
            )
            self._mark_failed(run.processing_run_id, error)
            raise BuildUnitsError(error) from exc

        return BuildUnitsResult(
            processing_run_id=updated.processing_run_id,
            status=updated.unit_build_status,
            unit_count=len(units),
            document_units_relpath=updated.document_units_relpath,
            build_stats=stats,
            content_hash_aggregate=updated.content_hash_aggregate,
            structure_hash=updated.structure_hash,
        )

    def _load_context(self, command: BuildUnitsCommand) -> dict[str, object]:
        if bool(command.document_id) == bool(command.processing_run_id):
            raise BuildUnitsError(
                self._structured_error(
                    error_code="RUN_NOT_FOUND",
                    message="provide exactly one of document_id or processing_run_id",
                )
            )
        with self._uow_factory() as uow:
            if command.processing_run_id:
                run = uow.processing_runs.get(command.processing_run_id)
            else:
                assert command.document_id is not None
                run = uow.processing_runs.latest_succeeded_provider_run_for_document(
                    command.document_id
                )
            if run is None:
                raise BuildUnitsError(
                    self._structured_error(
                        error_code="RUN_NOT_FOUND",
                        message="processing run not found",
                    )
                )
            if run.status != "succeeded":
                raise BuildUnitsError(
                    self._structured_error(
                        error_code="RUN_NOT_SUCCEEDED",
                        message=f"run status is {run.status}",
                    )
                )
            if (
                run.provider_document_relpath is None
                or run.normalized_ir_relpath is not None
            ):
                raise BuildUnitsError(
                    self._structured_error(
                        error_code="RUN_OUTPUT_CONTRACT_UNSUPPORTED",
                        message="only provider_document.v1 runs can build Units",
                    )
                )
            document = uow.documents.get(run.document_id)
            if document is None:
                raise BuildUnitsError(
                    self._structured_error(
                        error_code="RUN_NOT_FOUND",
                        message=f"run document not found: {run.document_id}",
                    )
                )
            security = (
                uow.securities.get(document.security_id)
                if document.security_id is not None
                else None
            )
            if security is None:
                raise BuildUnitsError(
                    self._structured_error(
                        error_code="RUN_NOT_FOUND",
                        message=f"document security not found: {document.security_id}",
                    )
                )
            artifact_owner = uow.processing_runs.get(
                run.artifact_owner_processing_run_id
            )
            if artifact_owner is None:
                raise BuildUnitsError(
                    self._structured_error(
                        error_code="ARTIFACT_OWNER_INVALID",
                        message="provider parse owner is missing",
                    )
                )
            if uow.document_units.list_by_processing_run(run.processing_run_id):
                raise BuildUnitsError(
                    self._structured_error(
                        error_code="UNITS_ALREADY_BUILT",
                        message=f"run already has units: {run.processing_run_id}",
                    )
                )
            return {
                "run": run,
                "artifact_owner": artifact_owner,
                "document": document,
                "security_code": security.security_code,
            }

    def _materialize_units(
        self,
        *,
        build: ProviderUnitBuildResult,
        document: e.Document,
        run: e.ProcessingRun,
    ) -> tuple[list[e.DocumentUnit], list[dict[str, Any]]]:
        units: list[e.DocumentUnit] = []
        rows: list[dict[str, Any]] = []
        for draft in build.units:
            self._validate_draft_hashes(draft)
            try:
                validate_semantic_key_state(
                    draft.semantic_key,
                    list(draft.semantic_keys),
                )
            except SemanticKeyInvariantError as exc:
                raise BuildUnitsError(
                    self._structured_error(
                        error_code="UNIT_SEMANTIC_KEYS_INVALID",
                        reason_code=exc.reason_code,
                        message=str(exc),
                    )
                ) from exc
            unit = e.DocumentUnit(
                asset_id=ids.new_asset_id(),
                document_id=document.document_id,
                processing_run_id=run.processing_run_id,
                provider_document_id=document.provider_document_id,
                payload_kind=draft.payload_kind,
                heading_path=list(draft.heading_path),
                title=draft.title,
                order_index=draft.unit_index + 1,
                semantic_key=draft.semantic_key,
                semantic_keys=list(draft.semantic_keys),
                payload=cast(dict[str, Any], dict(draft.payload)),
                content_hash=draft.content_hash,
                structure_hash=draft.structure_hash,
                quality_status=draft.quality_status,
                applicability=None,
                page_no=draft.page_no,
                query_projection_hash=draft.query_projection_hash,
                artifact_locator=provider_unit_locator_to_payload(draft.locator),
            )
            units.append(unit)
            row = {
                "applicability": unit.applicability,
                "page_no": unit.page_no,
                "artifact_locator": unit.artifact_locator,
                "asset_id": unit.asset_id,
                "content_hash": unit.content_hash,
                "document_id": unit.document_id,
                "heading_path": unit.heading_path,
                "order_index": unit.order_index,
                "payload": unit.payload,
                "payload_kind": unit.payload_kind,
                "quality_status": unit.quality_status,
                "semantic_key": unit.semantic_key,
                "semantic_keys": unit.semantic_keys,
                "title": unit.title,
            }
            if set(row) != SNAPSHOT_KEYS:
                raise AssertionError("snapshot row key drift")
            rows.append(row)
        return units, rows

    def _validate_draft_hashes(self, draft: ProviderUnitDraft) -> None:
        hashes = compute_unit_hashes(
            payload_kind=draft.payload_kind,
            payload=cast(dict[str, Any], draft.payload),
            title=draft.title,
            heading_path=list(draft.heading_path),
            semantic_key=draft.semantic_key,
            semantic_keys=list(draft.semantic_keys),
            quality_status=draft.quality_status,
            order_index=draft.unit_index + 1,
        )
        if (
            hashes.content_hash != draft.content_hash
            or hashes.query_projection_hash != draft.query_projection_hash
            or hashes.structure_hash != draft.structure_hash
        ):
            raise BuildUnitsError(
                self._structured_error(
                    error_code="PROVIDER_UNIT_HASH_MISMATCH",
                    message="provider Unit draft hashes do not replay canonically",
                )
            )

    def _snapshot_relpath(self, context: dict[str, object]) -> Path:
        document = cast(e.Document, context["document"])
        run = cast(e.ProcessingRun, context["run"])
        return self._paths.document_units_snapshot_relpath(
            provider=cast(str, document.provider),
            security_code=cast(str, context["security_code"]),
            provider_document_id=cast(str, document.provider_document_id),
            processing_run_id=run.processing_run_id,
        )

    def _verify_snapshot(
        self,
        *,
        relpath: Path,
        expected_rows: int,
        write_result: ArtifactWriteResult,
    ) -> None:
        try:
            content = self._paths.data_path(relpath).read_bytes()
        except OSError as exc:
            raise BuildUnitsError(
                self._structured_error(
                    error_code="ARTIFACT_WRITE_FAILED",
                    retryable=True,
                    message=str(exc),
                )
            ) from exc
        actual_rows = len(content.splitlines()) if content else 0
        actual_hash = "sha256:" + hashlib.sha256(content).hexdigest()
        if actual_rows != expected_rows or actual_hash != write_result.artifact_hash:
            raise BuildUnitsError(
                self._structured_error(
                    error_code="ARTIFACT_WRITE_FAILED",
                    retryable=True,
                    message=(
                        "snapshot verification failed: "
                        f"rows={actual_rows}/{expected_rows} "
                        f"hash={actual_hash}/{write_result.artifact_hash}"
                    ),
                )
            )

    def _persist_success(
        self,
        *,
        run_id: str,
        units: list[e.DocumentUnit],
        snapshot_relpath: Path,
        content_aggregate: str,
        structure_aggregate: str,
    ) -> e.ProcessingRun:
        with self._uow_factory() as uow:
            run = uow.processing_runs.get(run_id)
            if run is None:
                raise BuildUnitsError(
                    self._structured_error(
                        error_code="RUN_NOT_FOUND",
                        message=f"processing run not found: {run_id}",
                    )
                )
            maybe_lock_document(uow, run.document_id)
            if uow.document_units.list_by_processing_run(run_id):
                raise BuildUnitsError(
                    self._structured_error(
                        error_code="UNITS_ALREADY_BUILT",
                        message=f"run already has units: {run_id}",
                    )
                )
            uow.document_units.add_many(units)
            run.document_units_relpath = str(snapshot_relpath)
            run.content_hash_aggregate = content_aggregate
            run.structure_hash = structure_aggregate
            run.builder_rules_version = PROVIDER_UNIT_BUILDER_VERSION
            run.unit_build_status = "succeeded"
            run.unit_build_error = None
            run.unit_build_attempt_count += 1
            run.unit_built_at = datetime.now(timezone.utc)
            updated = uow.processing_runs.update(run)
            uow.commit()
            return updated

    def _mark_and_result(
        self, processing_run_id: str, error: dict[str, Any]
    ) -> BuildUnitsResult:
        failed = self._mark_failed(processing_run_id, error)
        return BuildUnitsResult(
            processing_run_id=failed.processing_run_id,
            status=failed.unit_build_status,
            error=failed.unit_build_error,
        )

    def _mark_failed(self, run_id: str, error: dict[str, Any]) -> e.ProcessingRun:
        with self._uow_factory() as uow:
            run = uow.processing_runs.get(run_id)
            if run is None:
                raise BuildUnitsError(
                    self._structured_error(
                        error_code="RUN_NOT_FOUND",
                        message=f"processing run not found: {run_id}",
                    )
                )
            if run.unit_build_status == "succeeded":
                return run
            run.unit_build_status = "failed"
            run.unit_build_error = error
            run.unit_build_attempt_count += 1
            updated = uow.processing_runs.update(run)
            uow.commit()
            return updated

    @staticmethod
    def _build_stats(build: ProviderUnitBuildResult) -> dict[str, Any]:
        return {
            "contract_version": "provider_unit_build_stats.v1",
            "builder_rules_version": PROVIDER_UNIT_BUILDER_VERSION,
            "provider_document_sha256": build.provider_document_sha256,
            "unit_count": len(build.units),
            "unassigned_table_part_count": len(build.unassigned_table_parts),
        }

    @staticmethod
    def _structured_error(
        *,
        error_code: str,
        message: str,
        reason_code: str | None = None,
        retryable: bool = False,
    ) -> dict[str, Any]:
        error: dict[str, Any] = {
            "stage": "build_units",
            "error_code": error_code,
            "retryable": retryable,
            "message": message,
        }
        if reason_code is not None:
            error["reason_code"] = reason_code
        return error


__all__ = ["BuildUnits", "BuildUnitsCommand", "BuildUnitsResult", "SNAPSHOT_KEYS"]
