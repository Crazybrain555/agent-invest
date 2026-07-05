"""Local admin write endpoints."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from disclosure_anchor.adapters.db.postgres.schema import CORE_SCHEMA
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.adapters.parsers.mineru.mineru_process import MinerUProcess
from disclosure_anchor.adapters.parsers.mineru.parser import MinerUDocumentParser
from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.raw_document_store import RawDocumentStore
from disclosure_anchor.api.errors import not_found
from disclosure_anchor.api.schemas.admin import (
    BuildUnitsResponse,
    ParseDocumentResponse,
    ParserOptionsRequest,
    PublishRunRequest,
    PublishRunResponse,
    RegisterLocalPdfRequest,
    RegisterLocalPdfResponse,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.use_cases.build_units import (
    BuildUnits,
    BuildUnitsCommand,
    BuildUnitsResult,
)
from disclosure_anchor.application.use_cases.parse_document import (
    ParseDocument,
    ParseDocumentCommand,
    ParseDocumentResult,
)
from disclosure_anchor.application.use_cases.publish_run import (
    PublishRun,
    PublishRunCommand,
)
from disclosure_anchor.application.use_cases.register_local_pdf import (
    RegisterLocalPdf,
    RegisterLocalPdfCommand,
    RegisterLocalPdfResult,
)
from disclosure_anchor.domain.value_objects import ReportPeriod
from disclosure_anchor.settings import Settings

try:
    from fastapi import APIRouter, Request
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    APIRouter = None  # type: ignore[assignment, misc]
    Request = None  # type: ignore[assignment, misc]


def register_local_pdf(
    request: Request,
    command: RegisterLocalPdfRequest,
) -> RegisterLocalPdfResponse:
    result = _admin_deps(request).register_local_pdf(_register_command(command))
    return _register_response(result)


def parse_document(
    document_id: str,
    request: Request,
    options: ParserOptionsRequest,
) -> ParseDocumentResponse:
    result = _admin_deps(request).parse_document(
        document_id=document_id,
        options=_parser_options(options),
    )
    return ParseDocumentResponse.model_validate(asdict(result))


def build_document_units(document_id: str, request: Request) -> BuildUnitsResponse:
    result = _admin_deps(request).build_units(document_id=document_id)
    return BuildUnitsResponse(
        processing_run_id=result.processing_run_id,
        unit_build_status=result.status,
        unit_count=result.unit_count,
    )


def publish_run(
    processing_run_id: str,
    request: Request,
    command: PublishRunRequest,
) -> PublishRunResponse:
    return _admin_deps(request).publish_run(
        processing_run_id=processing_run_id,
        allow_empty=command.allow_empty,
        reason=command.reason,
    )


class AdminDeps:
    def __init__(self, *, settings: Settings, engine: Engine) -> None:
        self._settings = settings
        self._engine = engine
        self._paths = FileStorePathBuilder(settings)
        self._artifacts = ArtifactStore(self._paths)
        self._uow_factory = unit_of_work_factory(engine)

    def register_local_pdf(
        self, command: RegisterLocalPdfCommand
    ) -> RegisterLocalPdfResult:
        return RegisterLocalPdf(
            raw_store=RawDocumentStore(self._paths),
            uow_factory=self._uow_factory,
        ).execute(command)

    def parse_document(
        self, *, document_id: str, options: ParserOptions
    ) -> ParseDocumentResult:
        executable = self._settings.disclosure_mineru_bin or Path("mineru")
        parser = MinerUDocumentParser(process=MinerUProcess(executable=executable))
        return ParseDocument(
            parser=parser,
            path_builder=self._paths,
            raw_store=RawDocumentStore(self._paths),
            artifact_store=self._artifacts,
            uow_factory=self._uow_factory,
            default_timeout_seconds=self._settings.disclosure_parse_timeout_seconds,
        ).execute(ParseDocumentCommand(document_id=document_id, options=options))

    def build_units(self, *, document_id: str) -> BuildUnitsResult:
        return BuildUnits(
            path_builder=self._paths,
            artifact_store=self._artifacts,
            uow_factory=self._uow_factory,
        ).execute(BuildUnitsCommand(document_id=document_id))

    def publish_run(
        self,
        *,
        processing_run_id: str,
        allow_empty: bool,
        reason: str | None,
    ) -> PublishRunResponse:
        result = PublishRun(uow_factory=self._uow_factory).execute(
            PublishRunCommand(
                processing_run_id=processing_run_id,
                allow_empty=allow_empty,
                reason=reason,
            )
        )
        document_id = self._document_id_for_run(result.processing_run_id)
        return PublishRunResponse(
            document_id=document_id,
            processing_run_id=result.processing_run_id,
            is_active=True,
        )

    def _document_id_for_run(self, processing_run_id: str) -> str:
        with self._engine.connect() as conn:
            document_id = conn.execute(
                text(
                    f"SELECT document_id FROM {CORE_SCHEMA}.processing_run "
                    "WHERE processing_run_id = :processing_run_id "
                    "AND is_active"
                ),
                {"processing_run_id": processing_run_id},
            ).scalar_one_or_none()
        if document_id is None:
            not_found("published processing run not found")
        return str(document_id)


def _admin_deps(request: Request) -> Any:
    existing = getattr(request.app.state, "admin_deps", None)
    if existing is not None:
        return existing
    settings = getattr(request.app.state, "settings", None)
    engine = getattr(request.app.state, "app_db_engine", None)
    if settings is None or engine is None:
        not_found("admin dependencies are not configured")
    deps = AdminDeps(settings=settings, engine=engine)
    request.app.state.admin_deps = deps
    return deps


def _register_command(command: RegisterLocalPdfRequest) -> RegisterLocalPdfCommand:
    report_period = (
        ReportPeriod.parse(command.report_period) if command.report_period else None
    )
    return RegisterLocalPdfCommand(
        file_path=command.file_path,
        company_legal_name=command.company_legal_name,
        security_code=command.security_code,
        exchange=command.exchange,
        filing_type=command.filing_type,
        title=command.title,
        announcement_date=command.announcement_date,
        provider_document_id=command.provider_document_id,
        provider=command.provider,
        report_period=report_period,
        board=command.board,
        company_credit_code=command.company_credit_code,
        expected_raw_file_hash=command.expected_raw_file_hash,
    )


def _register_response(result: RegisterLocalPdfResult) -> RegisterLocalPdfResponse:
    quarantined_path = result.quarantined_path.name if result.quarantined_path else None
    return RegisterLocalPdfResponse(
        document_id=result.document_id,
        raw_file_relpath=result.raw_file_relpath,
        raw_file_hash=result.raw_file_hash,
        source_access_id=result.source_access_id,
        outbox_event_id=result.outbox_event_id,
        reused_existing_document=result.reused_existing_document,
        quarantined_path=quarantined_path,
        quarantine_reason=result.quarantine_reason,
    )


def _parser_options(command: ParserOptionsRequest) -> ParserOptions:
    defaults = ParserOptions()
    return ParserOptions(
        method=command.method if command.method is not None else defaults.method,
        backend=command.backend if command.backend is not None else defaults.backend,
        language=command.language if command.language is not None else defaults.language,
        formula=command.formula if command.formula is not None else defaults.formula,
        table=command.table if command.table is not None else defaults.table,
        start_page=command.start_page,
        end_page=command.end_page,
        timeout_seconds=command.timeout_seconds,
    )


router: Any
if APIRouter is not None:
    router = APIRouter()
    router.add_api_route(
        "/v1/admin/documents/register-local-pdf",
        register_local_pdf,
        methods=["POST"],
        response_model=RegisterLocalPdfResponse,
    )
    router.add_api_route(
        "/v1/admin/documents/{document_id}/parse",
        parse_document,
        methods=["POST"],
        response_model=ParseDocumentResponse,
    )
    router.add_api_route(
        "/v1/admin/documents/{document_id}/build-units",
        build_document_units,
        methods=["POST"],
        response_model=BuildUnitsResponse,
    )
    router.add_api_route(
        "/v1/admin/runs/{processing_run_id}/publish",
        publish_run,
        methods=["POST"],
        response_model=PublishRunResponse,
    )
else:
    router = None
