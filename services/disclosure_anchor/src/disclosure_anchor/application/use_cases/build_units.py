"""Build document_unit snapshots from supported NormalizedIR generations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, cast

from disclosure_anchor.adapters.sources.cninfo.mapper import derive_primary_class
from disclosure_anchor.adapters.unit_builder import rules
from disclosure_anchor.adapters.unit_builder.builder import (
    ImageBytesResolver,
    UnitDraft,
    build_unit_drafts_s1_s7,
)
from disclosure_anchor.application.ports.file_store import (
    ArtifactStorePort,
    ArtifactWriteResult,
    FileStorePathPort,
)
from disclosure_anchor.application.contracts.normalized_ir import (
    NormalizedIRVersionError,
    validate_normalized_ir_contract,
    validate_normalized_ir_identity,
    validate_normalized_ir_path_version,
    validate_reconciliation_generation,
)
from disclosure_anchor.application.contracts.normalized_ir_table_reconciliation import (
    ReconciliationCompatibility,
    TableReconciliationContractError,
    UnsupportedTableReconciliationAlgorithm,
    assess_normalized_ir_table_reconciliation,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.worker.locks import maybe_lock_document
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
    def __init__(
        self,
        *,
        path_builder: FileStorePathPort,
        artifact_store: ArtifactStorePort,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        self._paths = path_builder
        self._artifact_store = artifact_store
        self._uow_factory = uow_factory

    def execute(self, command: BuildUnitsCommand) -> BuildUnitsResult:
        context = self._load_context(command)
        run = context["run"]
        try:
            normalized_ir = self._load_ir(
                Path(run.normalized_ir_relpath or ""),
                expected_artifact_hash=run.artifact_hash,
                expected_document_id=run.document_id,
            )
            document = context["document"]
            drafts, stats = build_unit_drafts_s1_s7(
                normalized_ir,
                filing_type=derive_primary_class(
                    (document.provider_metadata or {}).get("raw_category"),
                    document.title,
                ),
                document_title=document.title,
                security_code=context["security"].security_code,
                security_name=_optional_text(
                    (document.provider_metadata or {}).get("security_name")
                ),
                image_bytes_resolver=self._image_bytes_resolver(normalized_ir),
            )
            units, snapshot_rows = self._materialize_units(
                drafts=drafts,
                document=context["document"],
                run=run,
            )
        except BuildUnitsError as exc:
            return self._mark_and_result(run.processing_run_id, exc.error)
        except OSError as exc:
            return self._mark_and_result(
                run.processing_run_id,
                self._structured_error(
                    error_code="ARTIFACT_READ_FAILED",
                    message=str(exc),
                ),
            )
        except Exception as exc:
            # Persist the actual cause: a fixed message made every unknown
            # builder failure untraceable without re-running locally (round23).
            error = self._structured_error(
                error_code="BUILD_PREPARATION_FAILED",
                message=(
                    f"unit preparation failed: {type(exc).__name__}: {str(exc)[:400]}"
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
            stats_relpath = snapshot_relpath.parent / "build_stats.v1.json"
            self._artifact_store.write_json_atomic(
                relpath=stats_relpath,
                payload=stats.as_dict(),
            )
        except BuildUnitsError as exc:
            return self._mark_and_result(run.processing_run_id, exc.error)
        except OSError as exc:
            return self._mark_and_result(
                run.processing_run_id,
                self._structured_error(
                    error_code="ARTIFACT_WRITE_FAILED",
                    message=str(exc),
                ),
            )

        try:
            updated = self._persist_success(
                run_id=run.processing_run_id,
                units=units,
                snapshot_relpath=snapshot_relpath,
                content_hash_aggregate=_content_hash_aggregate(units),
                structure_hash=_structure_hash_aggregate(units),
            )
        except BuildUnitsError as exc:
            if exc.error.get("error_code") == "UNITS_ALREADY_BUILT":
                # Lost a concurrent build race under the document lock: the
                # winner's persisted state stands untouched (round23).
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
                message="document_unit DB persistence failed after snapshot write",
            )
            self._mark_failed(run.processing_run_id, error)
            raise BuildUnitsError(error) from exc
        except Exception as exc:
            error = self._structured_error(
                error_code="DB_WRITE_FAILED",
                message="document_unit DB persistence failed after snapshot write",
            )
            self._mark_failed(run.processing_run_id, error)
            raise BuildUnitsError(error) from exc

        return BuildUnitsResult(
            processing_run_id=updated.processing_run_id,
            status=updated.unit_build_status,
            unit_count=len(units),
            document_units_relpath=updated.document_units_relpath,
            build_stats=stats.as_dict(),
            content_hash_aggregate=updated.content_hash_aggregate,
            structure_hash=updated.structure_hash,
        )

    def _mark_and_result(
        self, processing_run_id: str, error: dict[str, Any]
    ) -> BuildUnitsResult:
        failed = self._mark_failed(processing_run_id, error)
        return BuildUnitsResult(
            processing_run_id=failed.processing_run_id,
            status=failed.unit_build_status,
            error=failed.unit_build_error,
        )

    def _load_context(self, command: BuildUnitsCommand) -> dict[str, Any]:
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
                run = uow.processing_runs.latest_succeeded_parse_for_document(
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
            if uow.document_units.list_by_processing_run(run.processing_run_id):
                raise BuildUnitsError(
                    self._structured_error(
                        error_code="UNITS_ALREADY_BUILT",
                        message=f"run already has units: {run.processing_run_id}",
                    )
                )
            return {"run": run, "document": document, "security": security}

    def _load_ir(
        self,
        relpath: Path,
        *,
        expected_artifact_hash: str | None = None,
        expected_document_id: str,
    ) -> dict[str, Any]:
        if not str(relpath):
            raise BuildUnitsError(
                self._structured_error(
                    error_code="IR_MISSING",
                    message="normalized_ir_relpath is missing",
                )
            )
        try:
            raw_bytes = self._paths.data_path(relpath).read_bytes()
        except OSError as exc:
            raise BuildUnitsError(
                self._structured_error(
                    error_code="IR_MISSING",
                    message=str(exc),
                )
            ) from exc
        # Ingress hash check, symmetric with parse's raw-PDF verification
        # (parse_document.py): the IR lives in the overwritable derived area
        # and rebuild-units consumes it months later — a corrupted/overwritten
        # IR must fail loudly here, not publish self-consistent bad units
        # (round23). Runs predating artifact_hash skip the check.
        if expected_artifact_hash:
            actual = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
            if actual != expected_artifact_hash:
                raise BuildUnitsError(
                    self._structured_error(
                        error_code="IR_HASH_MISMATCH",
                        message=(
                            f"normalized IR at {relpath} hashes to {actual}, "
                            f"run.artifact_hash is {expected_artifact_hash}; "
                            "re-parse or investigate the derived area"
                        ),
                    )
                )
        decoded: object = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise BuildUnitsError(
                self._structured_error(
                    error_code="IR_CONTRACT_UNSUPPORTED",
                    reason_code="payload_not_object",
                    message="normalized IR must be an object",
                )
            )
        payload = cast(dict[str, Any], decoded)
        try:
            version = validate_normalized_ir_contract(payload)
            validate_normalized_ir_identity(
                payload, document_id=expected_document_id
            )
            validate_normalized_ir_path_version(relpath, version=version)
        except NormalizedIRVersionError as exc:
            error_code = (
                "IR_CONTRACT_TOO_OLD"
                if exc.reason_code == "contract_version_too_old"
                else "IR_CONTRACT_UNSUPPORTED"
            )
            raise BuildUnitsError(
                self._structured_error(
                    error_code=error_code,
                    reason_code=exc.reason_code,
                    message=str(exc),
                )
            ) from exc
        self._validate_table_reconciliation(payload, version=version)
        return payload

    def _validate_table_reconciliation(
        self, payload: dict[str, Any], *, version: str
    ) -> None:
        """Validate the shared IR extension and act on evidence compatibility."""
        try:
            assessment = assess_normalized_ir_table_reconciliation(payload)
        except UnsupportedTableReconciliationAlgorithm as exc:
            raise BuildUnitsError(
                self._structured_error(
                    error_code="IR_TABLE_RECONCILIATION_UNSUPPORTED",
                    reason_code=exc.reason_code,
                    message=str(exc),
                )
            ) from exc
        except TableReconciliationContractError as exc:
            raise BuildUnitsError(
                self._structured_error(
                    error_code="IR_TABLE_RECONCILIATION_INVALID",
                    reason_code=exc.reason_code,
                    message=f"invalid normalized IR table reconciliation: {exc}",
                )
            ) from exc
        try:
            validate_reconciliation_generation(
                version=version,
                algorithm_version=assessment.algorithm_version,
            )
        except NormalizedIRVersionError as exc:
            raise BuildUnitsError(
                self._structured_error(
                    error_code="IR_TABLE_RECONCILIATION_CONTRACT_MISMATCH",
                    reason_code=exc.reason_code,
                    message=str(exc),
                )
            ) from exc
        if assessment.compatibility is ReconciliationCompatibility.REPARSE_REQUIRED:
            raise BuildUnitsError(
                self._structured_error(
                    error_code="IR_TABLE_RECONCILIATION_REPARSE_REQUIRED",
                    reason_code="legacy_physical_carriers_restored",
                    message=(
                        "normalized IR was produced by a legacy reconciliation "
                        "that rewrote physical table carriers; re-parse before "
                        "building units"
                    ),
                )
            )

    def _image_bytes_resolver(
        self, normalized_ir: dict[str, Any]
    ) -> ImageBytesResolver | None:
        parser_artifacts = normalized_ir.get("parser_artifacts") or {}
        artifact_root = parser_artifacts.get("artifact_root_relpath")
        if not artifact_root:
            return None
        artifact_root_relpath = Path(str(artifact_root))

        def resolve(image_path: str) -> bytes:
            relpath = artifact_root_relpath / Path(image_path)
            return self._paths.data_path(relpath).read_bytes()

        return resolve

    def _materialize_units(
        self,
        *,
        drafts: list[UnitDraft],
        document: e.Document,
        run: e.ProcessingRun,
    ) -> tuple[list[e.DocumentUnit], list[dict[str, Any]]]:
        units: list[e.DocumentUnit] = []
        rows: list[dict[str, Any]] = []
        for order_index, draft in enumerate(drafts, start=1):
            try:
                validate_semantic_key_state(
                    draft.semantic_key,
                    draft.semantic_keys,
                )
            except SemanticKeyInvariantError as exc:
                raise BuildUnitsError(
                    self._structured_error(
                        error_code="UNIT_SEMANTIC_KEYS_INVALID",
                        reason_code=exc.reason_code,
                        message=str(exc),
                    )
                ) from exc
            hashes = compute_unit_hashes(
                payload_kind=draft.payload_kind,
                payload=draft.payload,
                title=draft.title,
                heading_path=draft.heading_path,
                semantic_key=draft.semantic_key,
                quality_status=draft.quality_status,
                order_index=order_index,
                applicability=draft.applicability,
                semantic_keys=draft.semantic_keys,
            )
            unit = e.DocumentUnit(
                asset_id=ids.new_asset_id(),
                document_id=document.document_id,
                processing_run_id=run.processing_run_id,
                provider_document_id=document.provider_document_id,
                payload_kind=draft.payload_kind,
                heading_path=draft.heading_path,
                title=draft.title,
                order_index=order_index,
                semantic_key=draft.semantic_key,
                semantic_keys=draft.semantic_keys,
                payload=draft.payload,
                content_hash=hashes.content_hash,
                structure_hash=hashes.structure_hash,
                quality_status=draft.quality_status,
                applicability=draft.applicability,
                page_no=_locator_page_no(draft.artifact_locator),
                query_projection_hash=hashes.query_projection_hash,
                artifact_locator=draft.artifact_locator,
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

    def _snapshot_relpath(self, context: dict[str, Any]) -> Path:
        document = context["document"]
        security = context["security"]
        run = context["run"]
        return self._paths.document_units_snapshot_relpath(
            provider=document.provider,
            security_code=security.security_code,
            provider_document_id=document.provider_document_id,
            processing_run_id=run.processing_run_id,
        )

    def _verify_snapshot(
        self,
        *,
        relpath: Path,
        expected_rows: int,
        write_result: ArtifactWriteResult,
    ) -> None:
        path = self._paths.data_path(relpath)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise BuildUnitsError(
                self._structured_error(
                    error_code="ARTIFACT_WRITE_FAILED",
                    message=str(exc),
                )
            ) from exc
        actual_rows = len(content.splitlines()) if content else 0
        actual_hash = "sha256:" + hashlib.sha256(content).hexdigest()
        if actual_rows != expected_rows or actual_hash != write_result.artifact_hash:
            raise BuildUnitsError(
                self._structured_error(
                    error_code="ARTIFACT_WRITE_FAILED",
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
        content_hash_aggregate: str,
        structure_hash: str,
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
            # Serialize with any concurrent CLI/worker build of the same
            # document, then re-check under the lock: without this, the
            # loser of a concurrent build overwrote the winner's succeeded
            # state with failed (round23).
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
            run.content_hash_aggregate = content_hash_aggregate
            run.structure_hash = structure_hash
            run.builder_rules_version = rules.RULES_VERSION
            run.unit_build_status = "succeeded"
            run.unit_build_error = None
            run.unit_build_attempt_count += 1
            run.unit_built_at = datetime.now(timezone.utc)
            updated = uow.processing_runs.update(run)
            uow.commit()
            return updated

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
                # Never downgrade a persisted success: rebuilds get a NEW
                # run, so a late failure here is always a losing racer
                # (round23, defense in depth behind the document lock).
                return run
            run.unit_build_status = "failed"
            run.unit_build_error = error
            run.unit_build_attempt_count += 1
            updated = uow.processing_runs.update(run)
            uow.commit()
            return updated

    def _structured_error(
        self,
        *,
        error_code: str,
        message: str,
        reason_code: str | None = None,
    ) -> dict[str, Any]:
        error: dict[str, Any] = {
            "stage": "build_units",
            "error_code": error_code,
            "retryable": False,
            "message": message,
        }
        if reason_code is not None:
            error["reason_code"] = reason_code
        return error


def _locator_page_no(locator: dict[str, Any] | None) -> int | None:
    if not locator:
        return None
    value = locator.get("page_no")
    return value if isinstance(value, int) else None


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _content_hash_aggregate(units: list[e.DocumentUnit]) -> str:
    return content_hash_aggregate(unit.content_hash for unit in units)


def _structure_hash_aggregate(units: list[e.DocumentUnit]) -> str:
    return structure_hash_aggregate(
        unit.structure_hash or ""
        for unit in sorted(units, key=lambda item: item.order_index)
    )
