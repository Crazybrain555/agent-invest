"""Parse a registered raw document into parser artifacts and NormalizedIR."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from disclosure_anchor.application.ports.file_store import (
    ArtifactStorePort,
    FileStorePathPort,
    RawDocumentStorePort,
)
from disclosure_anchor.application.ports.parser import DocumentParserPort, ParserOptions
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import ParseDocumentError


@dataclass(frozen=True)
class ParseDocumentCommand:
    document_id: str
    options: ParserOptions = ParserOptions()


@dataclass(frozen=True)
class ParseDocumentResult:
    processing_run_id: str
    status: str
    parser_artifact_relpath: str | None = None
    normalized_ir_relpath: str | None = None
    artifact_hash: str | None = None
    error: dict[str, Any] | None = None


@dataclass(frozen=True)
class _ParseRunFailure(Exception):
    stage: str
    error_code: str
    retryable: bool
    message: str


class ParseDocument:
    """Use case for the Phase 04 parse step."""

    def __init__(
        self,
        *,
        parser: DocumentParserPort,
        path_builder: FileStorePathPort,
        raw_store: RawDocumentStorePort,
        artifact_store: ArtifactStorePort,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        self._parser = parser
        self._paths = path_builder
        self._raw_store = raw_store
        self._artifact_store = artifact_store
        self._uow_factory = uow_factory

    def execute(self, command: ParseDocumentCommand) -> ParseDocumentResult:
        context = self._prepare_run(command.document_id)
        try:
            self._verify_raw_document(context)
            parser_result = self._parser.parse(
                input_pdf=context["input_pdf"],
                output_dir=context["artifact_root_path"],
                options=command.options,
                document_metadata=context["document_metadata"],
            )
            artifact_relpaths = self._artifact_relpaths(
                artifact_root_relpath=context["artifact_root_relpath"],
                artifact_root_path=context["artifact_root_path"],
                artifact_root=parser_result.artifact_root,
                content_list_path=parser_result.content_list_path,
                markdown_path=parser_result.markdown_path,
            )
            parser_result.normalized_ir["parser_artifacts"] = artifact_relpath_map(
                artifact_root_relpath=artifact_relpaths["artifact_root"],
                content_list_relpath=artifact_relpaths["content_list"],
                markdown_relpath=artifact_relpaths["markdown"],
            )
            normalized_ir_result = self._artifact_store.write_json_atomic(
                relpath=context["normalized_ir_relpath"],
                payload=parser_result.normalized_ir,
            )
            normalized_ir_hash = normalized_ir_result.artifact_hash
        except _ParseRunFailure as exc:
            run = self._finish_run(
                processing_run_id=context["processing_run_id"],
                status="failed",
                error=self._structured_error(
                    stage=exc.stage,
                    error_code=exc.error_code,
                    retryable=exc.retryable,
                    message=exc.message,
                ),
            )
            return ParseDocumentResult(
                processing_run_id=run.processing_run_id,
                status=run.status,
                parser_artifact_relpath=run.parser_artifact_relpath,
                normalized_ir_relpath=run.normalized_ir_relpath,
                artifact_hash=run.artifact_hash,
                error=run.error,
            )
        except Exception as exc:
            run = self._finish_run(
                processing_run_id=context["processing_run_id"],
                status="failed",
                error=self._structured_error(
                    stage="parse",
                    error_code=exc.__class__.__name__,
                    retryable=True,
                    message=str(exc),
                ),
            )
            return ParseDocumentResult(
                processing_run_id=run.processing_run_id,
                status=run.status,
                parser_artifact_relpath=run.parser_artifact_relpath,
                normalized_ir_relpath=run.normalized_ir_relpath,
                artifact_hash=run.artifact_hash,
                error=run.error,
            )

        run = self._finish_run(
            processing_run_id=context["processing_run_id"],
            status="succeeded",
            parser_name=parser_result.parser_name,
            parser_version=parser_result.parser_version,
            parser_backend=parser_result.parser_backend,
            input_raw_file_hash=context["document"].raw_file_hash,
            parser_artifact_relpath=str(artifact_relpaths["artifact_root"]),
            normalized_ir_relpath=str(context["normalized_ir_relpath"]),
            artifact_hash=normalized_ir_hash,
        )
        return ParseDocumentResult(
            processing_run_id=run.processing_run_id,
            status=run.status,
            parser_artifact_relpath=run.parser_artifact_relpath,
            normalized_ir_relpath=run.normalized_ir_relpath,
            artifact_hash=run.artifact_hash,
        )

    def _prepare_run(self, document_id: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        processing_run_id = ids.new_processing_run_id()
        with self._uow_factory() as uow:
            document = uow.documents.get(document_id)
            if document is None:
                raise ParseDocumentError(f"document not found: {document_id}")
            self._validate_document(document)
            security = uow.securities.get(document.security_id)
            if security is None:
                raise ParseDocumentError(
                    f"document security not found: {document.security_id}"
                )

            artifact_root_relpath = self._paths.parser_run_artifacts_relpath(
                provider=document.provider,
                security_code=security.security_code,
                provider_document_id=document.provider_document_id,
                processing_run_id=processing_run_id,
            )
            normalized_ir_relpath = self._paths.normalized_ir_run_relpath(
                provider=document.provider,
                security_code=security.security_code,
                provider_document_id=document.provider_document_id,
                processing_run_id=processing_run_id,
            )
            run = uow.processing_runs.add(
                e.ProcessingRun(
                    processing_run_id=processing_run_id,
                    document_id=document.document_id,
                    run_kind="parse",
                    status="running",
                    input_raw_file_hash=document.raw_file_hash,
                    parser_artifact_relpath=str(artifact_root_relpath),
                    normalized_ir_relpath=str(normalized_ir_relpath),
                    started_at=now,
                    is_active=False,
                )
            )
            uow.commit()

        return {
            "document": document,
            "processing_run_id": run.processing_run_id,
            "input_pdf": self._paths.data_path(Path(document.raw_file_relpath)),
            "artifact_root_relpath": artifact_root_relpath,
            "artifact_root_path": self._paths.data_path(artifact_root_relpath),
            "normalized_ir_relpath": normalized_ir_relpath,
            "document_metadata": {
                "document_id": document.document_id,
                "title": document.title,
                "source_pdf": document.raw_file_relpath,
                "provider": document.provider,
                "provider_document_id": document.provider_document_id,
                "raw_file_hash": document.raw_file_hash,
            },
        }

    def _validate_document(self, document: e.Document) -> None:
        missing = [
            name
            for name in (
                "provider",
                "provider_document_id",
                "security_id",
                "raw_file_relpath",
                "raw_file_hash",
            )
            if not getattr(document, name)
        ]
        if missing:
            raise ParseDocumentError(
                f"document {document.document_id} missing parse metadata: {missing}"
            )

    def _verify_raw_document(self, context: dict[str, Any]) -> None:
        document = context["document"]
        verification = self._raw_store.verify_raw_document(
            relpath=Path(document.raw_file_relpath),
            expected_hash=document.raw_file_hash,
        )
        if verification.ok:
            return
        error_code = (
            "raw_missing"
            if verification.actual_hash is None
            else "raw_hash_mismatch"
        )
        raise _ParseRunFailure(
            stage="raw_verification",
            error_code=error_code,
            retryable=False,
            message=verification.message,
        )

    def _artifact_relpaths(
        self,
        *,
        artifact_root_relpath: Path,
        artifact_root_path: Path,
        artifact_root: Path,
        content_list_path: Path,
        markdown_path: Path | None,
    ) -> dict[str, Path | None]:
        def relpath(path: Path) -> Path:
            return artifact_root_relpath / path.relative_to(artifact_root_path)

        return {
            "artifact_root": relpath(artifact_root),
            "content_list": relpath(content_list_path),
            "markdown": relpath(markdown_path) if markdown_path is not None else None,
        }

    def _finish_run(
        self,
        *,
        processing_run_id: str,
        status: str,
        parser_name: str | None = None,
        parser_version: str | None = None,
        parser_backend: str | None = None,
        input_raw_file_hash: str | None = None,
        parser_artifact_relpath: str | None = None,
        normalized_ir_relpath: str | None = None,
        artifact_hash: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> e.ProcessingRun:
        with self._uow_factory() as uow:
            run = uow.processing_runs.get(processing_run_id)
            if run is None:
                raise ParseDocumentError(f"processing run not found: {processing_run_id}")
            run.status = status
            run.parser_name = parser_name or run.parser_name
            run.parser_version = parser_version or run.parser_version
            run.parser_backend = parser_backend or run.parser_backend
            run.input_raw_file_hash = input_raw_file_hash or run.input_raw_file_hash
            run.parser_artifact_relpath = (
                parser_artifact_relpath or run.parser_artifact_relpath
            )
            run.normalized_ir_relpath = normalized_ir_relpath or run.normalized_ir_relpath
            run.artifact_hash = artifact_hash or run.artifact_hash
            run.error = error
            run.finished_at = datetime.now(timezone.utc)
            updated = uow.processing_runs.update(run)
            uow.commit()
            return updated

    def _structured_error(
        self, *, stage: str, error_code: str, retryable: bool, message: str
    ) -> dict[str, Any]:
        return {
            "stage": stage,
            "error_code": error_code,
            "retryable": retryable,
            "message": message,
        }


def artifact_relpath_map(
    *,
    artifact_root_relpath: Path,
    content_list_relpath: Path,
    markdown_relpath: Path | None,
) -> dict[str, str]:
    artifacts = {
        "artifact_root_relpath": str(artifact_root_relpath),
        "content_list_relpath": str(content_list_relpath),
    }
    if markdown_relpath is not None:
        artifacts["markdown_relpath"] = str(markdown_relpath)
    return artifacts
