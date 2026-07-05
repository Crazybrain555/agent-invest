"""Build document_unit snapshots from NormalizedIR v2."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from disclosure_anchor.adapters.unit_builder import rules
from disclosure_anchor.adapters.unit_builder.builder import (
    BuildStats,
    ImageBytesResolver,
    UnitDraft,
    build_unit_drafts_s1_s7,
)
from disclosure_anchor.application.ports.file_store import (
    ArtifactStorePort,
    ArtifactWriteResult,
    FileStorePathPort,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import BuildUnitsError
from disclosure_anchor.domain.services.unit_hashing import compute_unit_hashes


SNAPSHOT_KEYS = {
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
        normalized_ir = self._load_ir(Path(run.normalized_ir_relpath or ""))
        drafts, stats = build_unit_drafts_s1_s7(
            normalized_ir,
            filing_type=context["document"].filing_type,
            image_bytes_resolver=self._image_bytes_resolver(normalized_ir),
        )
        units, snapshot_rows = self._materialize_units(
            drafts=drafts,
            document=context["document"],
            run=run,
        )
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
            if exc.error.get("error_code") == "ARTIFACT_WRITE_FAILED":
                failed = self._mark_failed(run.processing_run_id, exc.error)
                return BuildUnitsResult(
                    processing_run_id=failed.processing_run_id,
                    status=failed.unit_build_status,
                    error=failed.unit_build_error,
                )
            raise
        except OSError as exc:
            error = self._structured_error(
                error_code="ARTIFACT_WRITE_FAILED",
                message=str(exc),
            )
            failed = self._mark_failed(run.processing_run_id, error)
            return BuildUnitsResult(
                processing_run_id=failed.processing_run_id,
                status=failed.unit_build_status,
                error=failed.unit_build_error,
            )

        try:
            updated = self._persist_success(
                run_id=run.processing_run_id,
                units=units,
                snapshot_relpath=snapshot_relpath,
                content_hash_aggregate=_content_hash_aggregate(units),
                structure_hash=_structure_hash_aggregate(units),
            )
        except Exception:
            self._mark_failed(
                run.processing_run_id,
                self._structured_error(
                    error_code="DB_WRITE_FAILED",
                    message="document_unit DB persistence failed after snapshot write",
                ),
            )
            raise

        return BuildUnitsResult(
            processing_run_id=updated.processing_run_id,
            status=updated.unit_build_status,
            unit_count=len(units),
            document_units_relpath=updated.document_units_relpath,
            build_stats=stats.as_dict(),
            content_hash_aggregate=updated.content_hash_aggregate,
            structure_hash=updated.structure_hash,
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
            security = uow.securities.get(document.security_id)
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

    def _load_ir(self, relpath: Path) -> dict[str, Any]:
        if not str(relpath):
            raise BuildUnitsError(
                self._structured_error(
                    error_code="IR_MISSING",
                    message="normalized_ir_relpath is missing",
                )
            )
        try:
            payload = json.loads(self._paths.data_path(relpath).read_text(encoding="utf-8"))
        except OSError as exc:
            raise BuildUnitsError(
                self._structured_error(error_code="IR_MISSING", message=str(exc))
            ) from exc
        if payload.get("contract_version") != "normalized_ir.v2":
            raise BuildUnitsError(
                self._structured_error(
                    error_code="IR_CONTRACT_TOO_OLD",
                    message="normalized IR must be regenerated as normalized_ir.v2",
                )
            )
        return payload

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
            hashes = compute_unit_hashes(
                payload_kind=draft.payload_kind,
                payload=draft.payload,
                title=draft.title,
                heading_path=draft.heading_path,
                semantic_key=draft.semantic_key,
                quality_status=draft.quality_status,
                order_index=order_index,
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
                payload=draft.payload,
                content_hash=hashes.content_hash,
                structure_hash=hashes.structure_hash,
                quality_status=draft.quality_status,
                query_projection_hash=hashes.query_projection_hash,
                artifact_locator=draft.artifact_locator,
            )
            units.append(unit)
            row = {
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
            run.unit_build_status = "failed"
            run.unit_build_error = error
            run.unit_build_attempt_count += 1
            updated = uow.processing_runs.update(run)
            uow.commit()
            return updated

    def _structured_error(self, *, error_code: str, message: str) -> dict[str, Any]:
        return {
            "stage": "build_units",
            "error_code": error_code,
            "retryable": False,
            "message": message,
        }


def _content_hash_aggregate(units: list[e.DocumentUnit]) -> str:
    return _sha256_prefixed("\n".join(sorted(unit.content_hash for unit in units)))


def _structure_hash_aggregate(units: list[e.DocumentUnit]) -> str:
    return _sha256_prefixed(
        "\n".join(unit.structure_hash or "" for unit in sorted(units, key=lambda item: item.order_index))
    )


def _sha256_prefixed(payload: str) -> str:
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
