"""Build document_unit snapshots from supported NormalizedIR generations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from disclosure_anchor.application.contracts.document_structure import (
    DocumentStructureContractError,
    require_current_document_structure,
)
from disclosure_anchor.application.contracts.normalized_ir import (
    CURRENT_NORMALIZED_IR_VERSION,
    NormalizedIRVersionError,
    validate_normalized_ir_contract,
    validate_normalized_ir_identity,
    validate_normalized_ir_path_version,
    validate_reconciliation_generation,
)
from disclosure_anchor.application.contracts.normalized_ir_table_reconciliation import (
    TableReconciliationContractError,
    UnsupportedTableReconciliationAlgorithm,
    assess_normalized_ir_table_reconciliation,
)
from disclosure_anchor.application.contracts.publication_identity import (
    PublicationIdentityViolation,
    require_publishable_run_identity,
)
from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
    ParserTargetIdentityError,
)
from disclosure_anchor.application.contracts.publication_safety import (
    evaluate_publication_gate_v1,
)
from disclosure_anchor.application.contracts.unit_source_projection import (
    payload_page_no,
)
from disclosure_anchor.application.ports.file_store import (
    ArtifactStorePort,
    ArtifactWriteResult,
    FileStorePathPort,
)
from disclosure_anchor.application.ports.source_evidence import (
    SourceEvidenceValidationError,
    SourceEvidenceValidatorPort,
    VerifiedParserArtifact,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.data_file_reader import (
    DataFileMissingError,
    DataStoreReadError,
    read_data_file_bytes,
)
from disclosure_anchor.application.services.document_unit_audit import (
    AuditDocumentMetadata,
)
from disclosure_anchor.application.services.unit_builder import rules
from disclosure_anchor.application.services.unit_builder.builder import (
    ImageArtifactResolver,
    ResolvedImageArtifact,
    SourceEvidenceClosureError,
    UnitDraft,
)
from disclosure_anchor.application.services.unit_preparation import (
    prepare_and_audit_units,
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
    def __init__(
        self,
        *,
        path_builder: FileStorePathPort,
        artifact_store: ArtifactStorePort,
        uow_factory: Callable[[], UnitOfWork],
        source_evidence_validator: SourceEvidenceValidatorPort,
    ) -> None:
        self._paths = path_builder
        self._artifact_store = artifact_store
        self._uow_factory = uow_factory
        self._source_evidence_validator = source_evidence_validator

    def execute(self, command: BuildUnitsCommand) -> BuildUnitsResult:
        initial = self._load_context(command)
        run = initial["run"]
        # The producer UoW owns both corpus admission and the document lease
        # across IR/image reads, snapshot writes, and every DB transaction.
        with exclusive_document_producer(
            self._uow_factory,
            run.document_id,
        ):
            return self._execute_owned(command)

    def _execute_owned(
        self,
        command: BuildUnitsCommand,
    ) -> BuildUnitsResult:
        # Re-read after document admission. A concurrent winner may have
        # completed between initial resolution and the document lease.
        context = self._load_context(command)
        run = context["run"]
        try:
            normalized_ir = self._load_ir(
                Path(run.normalized_ir_relpath or ""),
                expected_artifact_hash=run.artifact_hash,
                expected_document_id=run.document_id,
                expected_parser_target=run.parser_target_identity,
            )
            parsed_pages = normalized_ir.get("parsed_pages")
            if (
                not isinstance(parsed_pages, dict)
                or parsed_pages.get("full_pdf") is not True
            ):
                raise BuildUnitsError(
                    self._structured_error(
                        error_code="PARTIAL_PDF_NOT_PUBLISHABLE",
                        message=(
                            "page-range diagnostic parses cannot enter the "
                            "document-unit publication pipeline"
                        ),
                    )
                )
            self._require_publishable_identity(
                run=run,
                document=context["document"],
                normalized_ir=normalized_ir,
            )
            evidence = self._source_evidence_validator.validate(
                normalized_ir,
                load_artifact=lambda role: self._load_hashed_parser_artifact(
                    normalized_ir,
                    role=role,
                ),
            )
            document = context["document"]
            security = context["security"]
            # Retrieval taxonomy consumes the classification materialized on
            # the document.  Parser/build structure must never be reclassified
            # from provider metadata or title text at this downstream stage.
            filing_type = document.class_filing_type
            image_hashes_by_role: dict[str, str] = {}
            drafts, stats, report = prepare_and_audit_units(
                normalized_ir=normalized_ir,
                filing_type=filing_type,
                metadata=AuditDocumentMetadata(
                    document_id=document.document_id,
                    title=document.title,
                    filing_type=filing_type,
                    security_code=security.security_code,
                ),
                image_artifact_resolver=self._image_artifact_resolver(
                    normalized_ir,
                    image_hashes_by_role=image_hashes_by_role,
                ),
                image_hash_provider=lambda: _image_hashes_by_source(
                    normalized_ir,
                    image_hashes_by_role=image_hashes_by_role,
                ),
                source_proof=evidence.proof,
            )
            publication_gate = evaluate_publication_gate_v1(report)
            if publication_gate.decision != "publish":
                sample = "; ".join(
                    f"{finding.code}:{finding.source_ref or '-'}"
                    for finding in report.findings[:8]
                )
                failed_checks = ",".join(
                    name
                    for name, passed in publication_gate.checks.items()
                    if not passed
                )
                raise BuildUnitsError(
                    self._structured_error(
                        error_code="UNIT_SOURCE_AUDIT_FAILED",
                        message=(
                            "publication gate blocked source replay "
                            f"({failed_checks}); "
                            f"{report.metrics.get('error_count', -1)} "
                            f"unit finding(s): {sample}"
                        ),
                    )
                )
            source_files = normalized_ir["parser_artifacts"]["files"]
            gate_receipt = {
                **publication_gate.as_dict(),
                "document_id": document.document_id,
                "processing_run_id": run.processing_run_id,
                "source_pdf_sha256": normalized_ir["source_pdf_sha256"],
                "source_evidence_sha256": source_files["source_evidence"][
                    "sha256"
                ],
                "normalized_ir_sha256": run.artifact_hash,
                "parser_target_identity": dict(run.parser_target_identity or {}),
            }
            units, snapshot_rows = self._materialize_units(
                drafts=drafts,
                document=context["document"],
                run=run,
            )
        except SourceEvidenceClosureError as exc:
            return self._mark_and_result(
                run.processing_run_id,
                self._structured_error(
                    error_code="SOURCE_EVIDENCE_UNADDRESSABLE",
                    message=str(exc),
                ),
            )
        except SourceEvidenceValidationError as exc:
            return self._mark_and_result(
                run.processing_run_id,
                self._structured_error(
                    error_code="SOURCE_EVIDENCE_INVALID",
                    reason_code=exc.reason_code,
                    message=str(exc),
                ),
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
            gate_relpath = snapshot_relpath.parent / "publication_gate.v1.json"
            self._artifact_store.write_json_atomic(
                relpath=gate_relpath,
                payload=gate_receipt,
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

    def _require_publishable_identity(
        self,
        *,
        run: e.ProcessingRun,
        document: e.Document,
        normalized_ir: dict[str, Any],
    ) -> None:
        """Close the shared publication identity gate before placement."""

        try:
            require_publishable_run_identity(
                document_raw_file_hash=document.raw_file_hash,
                run_input_raw_file_hash=run.input_raw_file_hash,
                normalized_ir=normalized_ir,
                read_artifact_bytes=lambda relpath: read_data_file_bytes(
                    self._paths, Path(relpath)
                ),
            )
        except PublicationIdentityViolation as exc:
            raise BuildUnitsError(
                self._structured_error(
                    error_code=exc.error_code,
                    reason_code=exc.reason_code,
                    message=exc.message,
                )
            ) from exc

    def _load_ir(
        self,
        relpath: Path,
        *,
        expected_artifact_hash: str | None = None,
        expected_document_id: str,
        expected_parser_target: object,
    ) -> dict[str, Any]:
        if not str(relpath):
            raise BuildUnitsError(
                self._structured_error(
                    error_code="IR_MISSING",
                    message="normalized_ir_relpath is missing",
                )
            )
        try:
            raw_bytes = read_data_file_bytes(self._paths, relpath)
        except DataFileMissingError as exc:
            raise BuildUnitsError(
                self._structured_error(
                    error_code="IR_MISSING",
                    message=str(exc),
                )
            ) from exc
        except DataStoreReadError as exc:
            raise BuildUnitsError(
                self._structured_error(
                    error_code="IR_READ_FAILED",
                    retryable=True,
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
            validate_normalized_ir_identity(payload, document_id=expected_document_id)
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
        if version != CURRENT_NORMALIZED_IR_VERSION:
            raise BuildUnitsError(
                self._structured_error(
                    error_code="IR_CONTRACT_TOO_OLD",
                    reason_code="structure_proof_reparse_required",
                    message=(
                        f"{version} has no source-bound document structure "
                        "and must be re-parsed before building units"
                    ),
                )
            )
        # A current envelope may still carry an earlier structure algorithm.
        # Convert the central currency gate into the same typed terminal here
        # instead of letting the builder's contract error surface as a
        # generic preparation failure.
        try:
            require_current_document_structure(
                cast(dict[str, Any], payload["structure_proof"])
            )
        except DocumentStructureContractError as exc:
            raise BuildUnitsError(
                self._structured_error(
                    error_code="IR_CONTRACT_TOO_OLD",
                    reason_code="structure_proof_reparse_required",
                    message=str(exc),
                )
            ) from exc
        try:
            run_target = ParserTargetIdentity.from_payload(expected_parser_target)
            ir_target = ParserTargetIdentity.from_payload(payload.get("parser"))
        except ParserTargetIdentityError as exc:
            raise BuildUnitsError(
                self._structured_error(
                    error_code="PARSER_TARGET_IDENTITY_INVALID",
                    message=str(exc),
                )
            ) from exc
        if run_target != ir_target:
            raise BuildUnitsError(
                self._structured_error(
                    error_code="PARSER_TARGET_IDENTITY_MISMATCH",
                    message=(
                        "processing run parser target differs from "
                        "NormalizedIR"
                    ),
                )
            )
        self._validate_table_reconciliation(payload, version=version)
        return payload

    def _load_hashed_parser_artifact(
        self,
        normalized_ir: Mapping[str, Any],
        *,
        role: str,
    ) -> VerifiedParserArtifact:
        parser_artifacts = cast(
            Mapping[str, Any],
            normalized_ir["parser_artifacts"],
        )
        files = cast(Mapping[str, Any], parser_artifacts["files"])
        descriptor = cast(Mapping[str, Any], files[role])
        relpath = Path(cast(str, descriptor["relpath"]))
        expected_sha256 = cast(str, descriptor["sha256"])
        expected_size = cast(int, descriptor["size_bytes"])
        try:
            payload = read_data_file_bytes(self._paths, relpath)
        except DataFileMissingError as exc:
            raise BuildUnitsError(
                self._structured_error(
                    error_code="PARSER_ARTIFACT_MISSING",
                    message=f"{role}: {exc}",
                )
            ) from exc
        except DataStoreReadError as exc:
            raise BuildUnitsError(
                self._structured_error(
                    error_code="PARSER_ARTIFACT_READ_FAILED",
                    retryable=True,
                    message=f"{role}: {exc}",
                )
            ) from exc
        actual_sha256 = "sha256:" + hashlib.sha256(payload).hexdigest()
        if len(payload) != expected_size or actual_sha256 != expected_sha256:
            raise BuildUnitsError(
                self._structured_error(
                    error_code="PARSER_ARTIFACT_IDENTITY_MISMATCH",
                    message=(
                        f"{role} at {relpath} has size/hash "
                        f"{len(payload)}/{actual_sha256}; expected "
                        f"{expected_size}/{expected_sha256}"
                    ),
                )
            )
        return VerifiedParserArtifact(payload=payload, sha256=actual_sha256)

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
    def _image_artifact_resolver(
        self,
        normalized_ir: dict[str, Any],
        *,
        image_hashes_by_role: dict[str, str] | None = None,
    ) -> ImageArtifactResolver | None:
        parser_artifacts = normalized_ir.get("parser_artifacts")
        if not isinstance(parser_artifacts, Mapping):
            return None
        artifact_root = parser_artifacts.get("artifact_root_relpath")
        files = parser_artifacts.get("files")
        if not isinstance(artifact_root, str) or not isinstance(files, Mapping):
            return None
        artifact_root_relpath = Path(artifact_root)

        def resolve(artifact_role: str, image_path: str) -> ResolvedImageArtifact:
            role = artifact_role
            descriptor = files.get(role)
            if not isinstance(descriptor, Mapping):
                raise SourceEvidenceClosureError(
                    f"image artifact role is missing: {role}"
                )
            expected_relpath = artifact_root_relpath / Path(image_path)
            if (
                descriptor.get("availability") != "present"
                or descriptor.get("relpath") != str(expected_relpath)
            ):
                raise SourceEvidenceClosureError(
                    f"image artifact role does not bind image_path: {role}"
                )
            artifact = self._load_hashed_parser_artifact(
                normalized_ir,
                role=role,
            )
            if image_hashes_by_role is not None:
                image_hashes_by_role[role] = artifact.sha256.removeprefix(
                    "sha256:"
                )
            return ResolvedImageArtifact(
                content=artifact.payload,
                artifact_role=role,
                sha256=artifact.sha256,
                size_bytes=len(artifact.payload),
                media_type=_image_media_type(artifact.payload),
            )

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
                page_no=payload_page_no(
                    payload_kind=draft.payload_kind,
                    payload=draft.payload,
                    artifact_locator=draft.artifact_locator,
                ),
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


def _image_hashes_by_source(
    normalized_ir: Mapping[str, Any],
    *,
    image_hashes_by_role: Mapping[str, str],
) -> dict[str, str]:
    output = dict(image_hashes_by_role)
    elements = normalized_ir.get("elements")
    if not isinstance(elements, list):
        return output
    for element in elements:
        if not isinstance(element, Mapping):
            continue
        ir_id = element.get("ir_id")
        source_item_index = element.get("source_item_index")
        if (
            isinstance(ir_id, str)
            and isinstance(source_item_index, int)
            and not isinstance(source_item_index, bool)
            and (
                role := f"evidence_image_{source_item_index:06d}"
            ) in image_hashes_by_role
        ):
            output[ir_id] = image_hashes_by_role[role]
    return output


def _image_media_type(content: bytes) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    raise SourceEvidenceClosureError("image artifact has an unsupported media type")


def _content_hash_aggregate(units: list[e.DocumentUnit]) -> str:
    return content_hash_aggregate(unit.content_hash for unit in units)


def _structure_hash_aggregate(units: list[e.DocumentUnit]) -> str:
    return structure_hash_aggregate(
        unit.structure_hash or ""
        for unit in sorted(units, key=lambda item: item.order_index)
    )
