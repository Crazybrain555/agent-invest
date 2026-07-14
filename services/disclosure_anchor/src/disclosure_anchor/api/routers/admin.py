"""Local admin write endpoints."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
import hmac
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
from disclosure_anchor.api.errors import (
    FORBIDDEN,
    UNAUTHORIZED,
    FilingApiError,
    not_found,
    validation_error,
)
from disclosure_anchor.api.schemas.admin import (
    BuildUnitsResponse,
    ParseDocumentResponse,
    ParserOptionsRequest,
    PublishRunRequest,
    PublishRunResponse,
    RegisterLocalPdfRequest,
    RegisterLocalPdfResponse,
    SyncCompanyRequest,
    SyncCompanyResponse,
    TrackCompaniesRequest,
    TrackCompaniesResponse,
    TrackDriftResponse,
    TrackEntryResultResponse,
    UntrackCompanyResponse,
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
from disclosure_anchor.application.use_cases.sync_disclosure_index import (
    SyncDisclosureIndex,
    SyncDisclosureIndexCommand,
    SyncDisclosureIndexError,
    compute_sync_window,
)
from disclosure_anchor.application.use_cases.track_companies import (
    ResolveTrackedProfiles,
    TrackCompanies,
    TrackCompaniesCommand,
    TrackCompaniesResult,
    TrackEntry,
    UntrackCompanies,
    UntrackCompaniesResult,
)
from disclosure_anchor.domain.value_objects import ReportPeriod
from disclosure_anchor.settings import Settings

try:
    from fastapi import APIRouter, Depends, Request
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    APIRouter = None  # type: ignore[assignment, misc]
    Depends = None  # type: ignore[assignment]
    Request = None  # type: ignore[assignment, misc]


def register_local_pdf(
    request: Request,
    command: RegisterLocalPdfRequest,
) -> RegisterLocalPdfResponse:
    try:
        register_command = _register_command(command)
    except ValueError as exc:
        raise validation_error("register", str(exc)) from exc
    result = _admin_deps(request).register_local_pdf(register_command)
    return _register_response(result)


def parse_document(
    document_id: str,
    request: Request,
    options: ParserOptionsRequest,
) -> ParseDocumentResponse:
    settings = getattr(request.app.state, "settings", None)
    defaults = (
        ParserOptions(
            backend=settings.disclosure_mineru_backend,
            server_url=settings.disclosure_mineru_server_url,
        )
        if settings is not None
        else ParserOptions()
    )
    result = _admin_deps(request).parse_document(
        document_id=document_id,
        options=_parser_options(options, defaults),
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


def track_companies(
    request: Request,
    command: TrackCompaniesRequest,
) -> TrackCompaniesResponse:
    """Upsert the tracked-company pool — FULL-ROW UPSERT.

    FULL-ROW UPSERT semantics (identical to the CSV import path): every
    optional field is authoritative. Omitting an optional override field
    (lookback_days, sync_frequency, process_classes) CLEARS the stored
    override back to inherit-global — a blank field does NOT preserve the
    previous value, it drops it. Each result echoes `cleared_overrides` so the
    caller sees exactly which overrides this request cleared, plus `action`
    (created | updated | unchanged) and any `status_change`.
    """
    # Full-row upsert semantics (same as the CSV import path): an absent
    # optional field clears the stored override back to inherit-global.
    deps = _admin_deps(request)
    try:
        result = deps.track_companies(
            TrackCompaniesCommand(
                entries=tuple(
                    TrackEntry(
                        security_code=entry.security_code,
                        exchange=entry.exchange,
                        status=entry.status,
                        lookback_days=entry.lookback_days,
                        sync_frequency=entry.sync_frequency,
                        process_classes=(
                            tuple(entry.process_classes)
                            if entry.process_classes
                            else None
                        ),
                    )
                    for entry in command.entries
                ),
                reconcile=command.reconcile,
                prune_drift=command.prune_drift,
                dry_run=command.dry_run,
            )
        )
    except ValueError as exc:
        # TrackCompanies rejects unknown sync_frequency/process_classes and
        # negative lookback with ValueError — surface as the envelope's 422.
        raise validation_error("entries", str(exc)) from exc
    if not command.dry_run:
        # On-add metadata fetch (Miniflux pattern), best-effort: pending
        # legal names resolve now when credentials allow; the worker's
        # first sync heals whatever this pass could not.
        deps.resolve_profiles(
            tuple((item.security_code, item.exchange) for item in result.results)
        )
    return _track_response(result)


def sync_company(
    security_code: str,
    request: Request,
    exchange: str,
    command: SyncCompanyRequest,
) -> SyncCompanyResponse:
    """On-demand acquisition trigger (Miniflux refresh-feed analog).

    This is the machine-callable entry the L6 pull loop uses: an
    evidence_request that needs a company's disclosures NOW calls this
    instead of waiting for the scheduler's next round. Synchronous like the
    admin parse endpoint; downloads/parses still flow through worker rounds.
    """

    deps = _admin_deps(request)
    if not deps.can_sync():
        raise validation_error(
            "cninfo", "CNINFO credentials are required for on-demand sync"
        )
    if command.window_days is not None and command.window_days < 0:
        raise validation_error("window_days", "must be non-negative")
    return deps.sync_company(
        security_code=security_code,
        exchange=exchange,
        window_days=command.window_days,
        window_start=command.window_start,
        window_end=command.window_end,
    )


def untrack_company(
    security_code: str,
    request: Request,
    exchange: str,
) -> UntrackCompanyResponse:
    deps = _admin_deps(request)
    result: UntrackCompaniesResult = deps.untrack_companies(
        ((security_code, exchange),)
    )
    if not result.removed:
        not_found(f"company is not tracked: {security_code}.{exchange}")
    removed = result.removed[0]
    return UntrackCompanyResponse(
        security_code=removed.security_code,
        exchange=removed.exchange,
        tracked_company_id=removed.tracked_company_id,
        company_id=removed.company_id,
        documents_retained=deps.document_count(removed.company_id),
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

    def track_companies(self, command: TrackCompaniesCommand) -> TrackCompaniesResult:
        return TrackCompanies(uow_factory=self._uow_factory).execute(command)

    def untrack_companies(
        self, codes: tuple[tuple[str, str], ...]
    ) -> UntrackCompaniesResult:
        return UntrackCompanies(uow_factory=self._uow_factory).execute(codes)

    def resolve_profiles(self, codes: tuple[tuple[str, str], ...]) -> None:
        settings = self._settings
        if not self.can_sync():
            return
        from disclosure_anchor.adapters.sources.cninfo import (
            CninfoClient,
            CninfoSource,
        )

        source = CninfoSource(CninfoClient.from_settings(settings))
        ResolveTrackedProfiles(
            uow_factory=self._uow_factory,
            profile_loader=source.profile_for_security,
        ).execute(codes)

    def can_sync(self) -> bool:
        return bool(
            self._settings.cninfo_access_key and self._settings.cninfo_access_secret
        )

    def sync_company(
        self,
        *,
        security_code: str,
        exchange: str,
        window_days: int | None,
        window_start: date | None = None,
        window_end: date | None = None,
    ) -> SyncCompanyResponse:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from disclosure_anchor.adapters.sources.cninfo import (
            CninfoClient,
            CninfoSource,
        )

        settings = self._settings
        source = CninfoSource(CninfoClient.from_settings(settings))
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        try:
            window_start, window_end = compute_sync_window(
                uow_factory=self._uow_factory,
                company=security_code,
                exchange=exchange,
                explicit_window_days=window_days,
                today=today,
                overlap_days=settings.cninfo_overlap_days,
                initial_lookback_days=settings.disclosure_initial_lookback_days,
                # Absolute backfill range (round23): the shared window helper
                # owns the mutual-exclusion/order/future validation.
                explicit_window_start=window_start,
                explicit_window_end=window_end,
            )
        except ValueError as exc:
            raise validation_error("window", str(exc)) from exc
        try:
            result = SyncDisclosureIndex(
                source=source,
                profile_loader=source.profile_for_security,
                uow_factory=self._uow_factory,
            ).execute(
                SyncDisclosureIndexCommand(
                    security_code=security_code,
                    exchange=exchange,
                    window_start=window_start,
                    window_end=window_end,
                )
            )
        except SyncDisclosureIndexError as exc:
            # Durable failure trace already persisted (source_access row).
            return SyncCompanyResponse(
                sync_status="failed",
                security_code=security_code,
                exchange=exchange,
                window_start=window_start,
                window_end=window_end,
                error=str(exc),
            )
        return SyncCompanyResponse(
            sync_status="ok",
            security_code=security_code,
            exchange=exchange,
            window_start=window_start,
            window_end=window_end,
            company_id=result.company_id,
            candidate_count=result.candidate_count,
            empty=result.empty,
            checkpoint_id=result.checkpoint_id,
        )

    def document_count(self, company_id: str) -> int:
        with self._engine.connect() as conn:
            count = conn.execute(
                text(
                    f"SELECT count(*) FROM {CORE_SCHEMA}.document "
                    "WHERE company_id = :company_id"
                ),
                {"company_id": company_id},
            ).scalar()
        return int(count or 0)

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


def _track_response(result: TrackCompaniesResult) -> TrackCompaniesResponse:
    return TrackCompaniesResponse(
        results=[
            TrackEntryResultResponse(**asdict(item)) for item in result.results
        ],
        drift=[TrackDriftResponse(**asdict(item)) for item in result.drift],
        dry_run=result.dry_run,
        created_count=result.created_count,
    )


def _parser_options(
    command: ParserOptionsRequest, defaults: ParserOptions | None = None
) -> ParserOptions:
    defaults = defaults or ParserOptions()
    return ParserOptions(
        method=command.method if command.method is not None else defaults.method,
        backend=command.backend if command.backend is not None else defaults.backend,
        language=command.language if command.language is not None else defaults.language,
        formula=command.formula if command.formula is not None else defaults.formula,
        table=command.table if command.table is not None else defaults.table,
        start_page=command.start_page,
        end_page=command.end_page,
        timeout_seconds=command.timeout_seconds,
        server_url=(
            command.server_url
            if command.server_url is not None
            else defaults.server_url
        ),
    )


def require_admin_auth(request: Request) -> None:
    """Static bearer token + loopback double barrier (user decision
    2026-07-14, supersedes round8's unauthenticated local-ops stance).

    The token comes from DISCLOSURE_ADMIN_TOKEN via app.state.settings;
    main.py refuses to mount this router when it is missing, so a None
    here means a misconfigured embedding — fail closed. The client-host
    allowlist keeps the write surface loopback-only even if API_HOST is
    overridden; "testclient"/None cover the in-process ASGI test client,
    which never represents a remote peer.
    """

    settings = getattr(request.app.state, "settings", None)
    token = getattr(settings, "disclosure_admin_token", None) if settings else None
    if token is None:
        raise FilingApiError(
            status_code=401,
            error_code=UNAUTHORIZED,
            message="admin API token is not configured",
        )
    client_host = request.client.host if request.client else None
    if client_host not in (None, "127.0.0.1", "::1", "testclient"):
        raise FilingApiError(
            status_code=403,
            error_code=FORBIDDEN,
            message="admin API is loopback-only",
        )
    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        presented.strip(), token.get_secret_value()
    ):
        raise FilingApiError(
            status_code=401,
            error_code=UNAUTHORIZED,
            message="missing or invalid admin bearer token",
        )


router: Any
if APIRouter is not None:
    router = APIRouter(dependencies=[Depends(require_admin_auth)])
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
    router.add_api_route(
        "/v1/admin/tracked-companies",
        track_companies,
        methods=["PUT"],
        response_model=TrackCompaniesResponse,
    )
    router.add_api_route(
        "/v1/admin/tracked-companies/{security_code}",
        untrack_company,
        methods=["DELETE"],
        response_model=UntrackCompanyResponse,
    )
    router.add_api_route(
        "/v1/admin/tracked-companies/{security_code}/sync",
        sync_company,
        methods=["POST"],
        response_model=SyncCompanyResponse,
    )
else:
    router = None
