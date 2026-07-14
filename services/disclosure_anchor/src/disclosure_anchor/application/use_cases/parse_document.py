"""Parse a registered raw document into parser artifacts and NormalizedIR."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from disclosure_anchor.application.ports.file_store import (
    ArtifactStorePort,
    FileStorePathPort,
    RawDocumentStorePort,
)
from disclosure_anchor.application.ports.parser import DocumentParserPort, ParserOptions
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.worker.locks import maybe_lock_document
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.entities import outbox_events
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import (
    ParseDocumentError,
    ParserInvocationError,
    ParserOutputContractError,
    ParserTimeoutError,
    ParserUnknownError,
    ParserVersionProbeError,
)


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
class _ArtifactRelpaths:
    artifact_root: Path
    content_list: Path
    markdown: Path | None


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
        default_timeout_seconds: int = 1800,
    ) -> None:
        self._parser = parser
        self._paths = path_builder
        self._raw_store = raw_store
        self._artifact_store = artifact_store
        self._uow_factory = uow_factory
        self._default_timeout_seconds = default_timeout_seconds

    def execute(self, command: ParseDocumentCommand) -> ParseDocumentResult:
        options = self._effective_options(command.options)
        context = self._prepare_run(command.document_id, options)
        if context.get("prepare_failed"):
            run = context["run"]
            return ParseDocumentResult(
                processing_run_id=run.processing_run_id,
                status=run.status,
                parser_artifact_relpath=run.parser_artifact_relpath,
                normalized_ir_relpath=run.normalized_ir_relpath,
                artifact_hash=run.artifact_hash,
                error=run.error,
            )
        try:
            self._verify_raw_document(context)
            parser_result = self._parser.parse(
                input_pdf=context["input_pdf"],
                output_dir=context["artifact_root_path"],
                options=options,
                document_metadata=context["document_metadata"],
            )
            artifact_relpaths = self._artifact_relpaths(
                artifact_root_relpath=context["artifact_root_relpath"],
                artifact_root_path=context["artifact_root_path"],
                artifact_root=parser_result.artifact_root,
                content_list_path=parser_result.content_list_path,
                markdown_path=parser_result.markdown_path,
            )
            normalized_ir = dict(parser_result.normalized_ir)
            normalized_ir["parser_artifacts"] = artifact_relpath_map(
                artifact_root_relpath=artifact_relpaths.artifact_root,
                content_list_relpath=artifact_relpaths.content_list,
                markdown_relpath=artifact_relpaths.markdown,
            )
            parsed_pages = dict(normalized_ir.get("parsed_pages") or {})
            parsed_pages["full_pdf"] = (
                options.start_page is None and options.end_page is None
            )
            normalized_ir["parsed_pages"] = parsed_pages
            normalized_ir_result = self._artifact_store.write_json_atomic(
                relpath=context["normalized_ir_relpath"],
                payload=normalized_ir,
            )
            normalized_ir_hash = normalized_ir_result.artifact_hash
        except (
            _ParseRunFailure,
            ParserTimeoutError,
            ParserInvocationError,
            ParserVersionProbeError,
            ParserOutputContractError,
            ParserUnknownError,
        ) as exc:
            failure = (
                exc
                if isinstance(exc, _ParseRunFailure)
                else self._parser_failure_from_exception(exc)
            )
            run = self._finish_failed_run(context=context, failure=failure)
            return ParseDocumentResult(
                processing_run_id=run.processing_run_id,
                status=run.status,
                parser_artifact_relpath=run.parser_artifact_relpath,
                normalized_ir_relpath=run.normalized_ir_relpath,
                artifact_hash=run.artifact_hash,
                error=run.error,
            )
        except Exception as exc:
            self._finish_failed_run(
                context=context,
                failure=_ParseRunFailure(
                    stage="parse",
                    error_code=exc.__class__.__name__,
                    retryable=False,
                    message=str(exc),
                ),
            )
            raise

        run = self._finish_run(
            processing_run_id=context["processing_run_id"],
            status="succeeded",
            parser_name=parser_result.parser_name,
            parser_version=parser_result.parser_version,
            parser_backend=parser_result.parser_backend,
            parser_method=parser_result.parser_method,
            parser_language=parser_result.parser_language,
            input_raw_file_hash=context["document"].raw_file_hash,
            parser_artifact_relpath=str(artifact_relpaths.artifact_root),
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

    def _effective_options(self, options: ParserOptions) -> ParserOptions:
        if options.timeout_seconds is not None:
            return options
        return replace(options, timeout_seconds=self._default_timeout_seconds)

    def _prepare_run(self, document_id: str, options: ParserOptions) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        processing_run_id = ids.new_processing_run_id()
        with self._uow_factory() as uow:
            document = uow.documents.get(document_id)
            if document is None:
                raise ParseDocumentError(f"document not found: {document_id}")
            self._validate_document(document)
            # Lock ordering discipline (round23 review S2): every transaction
            # that touches both the document row and the outbox takes the
            # document lock FIRST (finish/publish/register all do). The
            # prepare failure branch updates document.status after its first
            # outbox insert, which inverted the order and could deadlock
            # against a concurrent finisher on the same document.
            maybe_lock_document(uow, document_id)
            security = (
                uow.securities.get(document.security_id)
                if document.security_id is not None
                else None
            )
            if security is None:
                raise ParseDocumentError(
                    f"document security not found: {document.security_id}"
                )

            provider = cast(str, document.provider)
            provider_document_id = cast(str, document.provider_document_id)
            artifact_root_relpath = self._paths.parser_run_artifacts_relpath(
                provider=provider,
                security_code=security.security_code,
                provider_document_id=provider_document_id,
                processing_run_id=processing_run_id,
            )
            normalized_ir_relpath = self._paths.normalized_ir_run_relpath(
                provider=provider,
                security_code=security.security_code,
                provider_document_id=provider_document_id,
                processing_run_id=processing_run_id,
            )
            prepare_error: dict[str, Any] | None = None
            try:
                identity = self._parser.identity()
                parser_name = identity.name
                parser_version = identity.version
            except ParserVersionProbeError as exc:
                parser_name = self._parser.__class__.__name__
                parser_version = None
                prepare_error = self._structured_error(
                    stage="parser_identity",
                    error_code="parser_version_probe_failed",
                    # Parser identity is process/configuration health, not a
                    # property of this PDF. Keep the item retryable so a
                    # repaired binary/service can resume after worker cooldown.
                    retryable=True,
                    message=str(exc),
                )
            run = uow.processing_runs.add(
                e.ProcessingRun(
                    processing_run_id=processing_run_id,
                    document_id=document.document_id,
                    run_kind="parse",
                    status="failed" if prepare_error else "running",
                    parser_name=parser_name,
                    parser_version=parser_version,
                    parser_backend=options.backend,
                    parser_method=options.method,
                    parser_language=options.language,
                    input_raw_file_hash=document.raw_file_hash,
                    parser_artifact_relpath=str(artifact_root_relpath),
                    normalized_ir_relpath=str(normalized_ir_relpath),
                    started_at=now,
                    finished_at=now if prepare_error else None,
                    error=prepare_error,
                    is_active=False,
                )
            )
            uow.outbox.add(
                outbox_events.processing_run_created(
                    document_id=document.document_id,
                    processing_run_id=run.processing_run_id,
                    occurred_at=now,
                )
            )
            if prepare_error is not None:
                self._update_document_status_for_run(
                    uow=uow, document_id=document.document_id, status="failed"
                )
                uow.outbox.add(
                    outbox_events.processing_run_failed(
                        document_id=document.document_id,
                        processing_run_id=run.processing_run_id,
                        error=prepare_error,
                        occurred_at=now,
                    )
                )
            uow.commit()

        return {
            "document": document,
            "run": run,
            "prepare_failed": prepare_error is not None,
            "processing_run_id": run.processing_run_id,
            "input_pdf": self._paths.data_path(Path(cast(str, document.raw_file_relpath))),
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
    ) -> _ArtifactRelpaths:
        def relpath(path: Path) -> Path:
            return artifact_root_relpath / path.relative_to(artifact_root_path)

        return _ArtifactRelpaths(
            artifact_root=relpath(artifact_root),
            content_list=relpath(content_list_path),
            markdown=relpath(markdown_path) if markdown_path is not None else None,
        )

    def _finish_run(
        self,
        *,
        processing_run_id: str,
        status: str,
        parser_name: str | None = None,
        parser_version: str | None = None,
        parser_backend: str | None = None,
        parser_method: str | None = None,
        parser_language: str | None = None,
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
            maybe_lock_document(uow, run.document_id)
            if run.status != "running":
                # First terminal state wins: the stale reclaimer (or a
                # duplicate finisher) already closed this run, and a retry
                # run may exist for the document. A late failure is
                # idempotent; a late success must not resurrect the run and
                # race the retry (round23).
                if status == "failed":
                    return run
                raise ParseDocumentError(
                    f"processing run {processing_run_id} is already "
                    f"{run.status}; discarding late success (stale reclaim "
                    "or duplicate finisher won)"
                )
            run.status = status
            run.parser_name = parser_name or run.parser_name
            run.parser_version = parser_version or run.parser_version
            run.parser_backend = parser_backend or run.parser_backend
            run.parser_method = parser_method or run.parser_method
            run.parser_language = parser_language or run.parser_language
            run.input_raw_file_hash = input_raw_file_hash or run.input_raw_file_hash
            run.parser_artifact_relpath = (
                parser_artifact_relpath or run.parser_artifact_relpath
            )
            run.normalized_ir_relpath = normalized_ir_relpath or run.normalized_ir_relpath
            run.artifact_hash = artifact_hash or run.artifact_hash
            run.error = error
            finished_at = datetime.now(timezone.utc)
            run.finished_at = finished_at
            updated = uow.processing_runs.update(run)
            self._update_document_status_for_run(
                uow=uow, document_id=run.document_id, status=status
            )
            if status == "failed" and error is not None:
                uow.outbox.add(
                    outbox_events.processing_run_failed(
                        document_id=run.document_id,
                        processing_run_id=run.processing_run_id,
                        error=error,
                        occurred_at=finished_at,
                    )
                )
            uow.commit()
            return updated

    def _finish_failed_run(
        self, *, context: dict[str, Any], failure: _ParseRunFailure
    ) -> e.ProcessingRun:
        return self._finish_run(
            processing_run_id=context["processing_run_id"],
            status="failed",
            error=self._structured_error(
                stage=failure.stage,
                error_code=failure.error_code,
                retryable=failure.retryable,
                message=failure.message,
            ),
        )

    def _update_document_status_for_run(
        self, *, uow: UnitOfWork, document_id: str, status: str
    ) -> None:
        document = uow.documents.get(document_id)
        if document is None or document.current_processing_run_id is not None:
            return
        if status == "succeeded":
            document.status = "parsed"
        elif status == "failed":
            document.status = "parse_failed"
        else:
            return
        uow.documents.update(document)

    def _structured_error(
        self, *, stage: str, error_code: str, retryable: bool, message: str
    ) -> dict[str, Any]:
        return {
            "stage": stage,
            "error_code": error_code,
            "retryable": retryable,
            "message": message,
        }

    def _parser_failure_from_exception(self, exc: Exception) -> _ParseRunFailure:
        if isinstance(exc, ParserTimeoutError):
            return _ParseRunFailure(
                stage="parse",
                error_code="parse_timeout",
                retryable=True,
                message=str(exc),
            )
        if isinstance(exc, ParserInvocationError):
            return _ParseRunFailure(
                stage="parse",
                error_code="parser_invocation_failed",
                retryable=True,
                message=str(exc),
            )
        if isinstance(exc, ParserVersionProbeError):
            return _ParseRunFailure(
                stage="parser_identity",
                error_code="parser_version_probe_failed",
                retryable=True,
                message=str(exc),
            )
        if isinstance(exc, ParserOutputContractError):
            return _ParseRunFailure(
                stage="parse_output",
                error_code="parser_output_contract_failed",
                retryable=False,
                message=str(exc),
            )
        if isinstance(exc, ParserUnknownError):
            return _ParseRunFailure(
                stage="parse",
                error_code="parser_unknown_failed",
                retryable=False,
                message=str(exc),
            )
        raise TypeError(f"unsupported parser failure type: {type(exc)!r}")


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
