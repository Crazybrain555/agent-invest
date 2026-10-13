"""Private agent API application (separate loopback process, port 8712).

Mounts the existing Filing API read routers verbatim behind the read token,
adds explicit processing requests (request token), quick original text, the
read-only disclosure inventory and the recorded official download link.
Provider discovery, download and parse are owned by the resident worker; this
process only reads, accepts intents and extracts rough text on demand. The
public Filing API app (``main.create_app``) is unchanged and never mounts it.
"""

from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import threading
from typing import Any

from disclosure_anchor.adapters.db.postgres.connection import (
    app_database_url,
    create_db_engine,
    reader_database_url,
    require_runtime_reader_engine,
)
from disclosure_anchor.adapters.db.postgres.disclosure_inventory import (
    PostgresDisclosureInventoryReader,
)
from disclosure_anchor.adapters.db.postgres.original_download_link import (
    PostgresOriginalDownloadLinkReader,
)
from disclosure_anchor.adapters.db.postgres.processing_requests import (
    PostgresOriginalLocatorReader,
    PostgresProcessingStatusReader,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import (
    processing_request_uow_factory,
    unit_of_work_factory,
)
from disclosure_anchor.adapters.runtime.doctor import render_report, run_startup_preflight
from disclosure_anchor.adapters.sources.cninfo.mapper import load_processing_policy
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.api.agent_auth import AgentApiAccess, require_agent_read
from disclosure_anchor.api.agent_errors import AGENT_ERROR_CODES, install_agent_error_handlers
from disclosure_anchor.api.agent_middleware import AgentApiGuard
from disclosure_anchor.api.errors import install_error_handlers
from disclosure_anchor.api.routers.changes import router as changes_router
from disclosure_anchor.api.routers.disclosure_inventory import (
    router as disclosure_inventory_router,
)
from disclosure_anchor.api.routers.document_text import QuickTextReader
from disclosure_anchor.api.routers.document_text import router as document_text_router
from disclosure_anchor.api.routers.documents import router as documents_router
from disclosure_anchor.api.routers.filings import router as filings_router
from disclosure_anchor.api.routers.health import router as health_router
from disclosure_anchor.api.routers.original_download_link import (
    router as original_download_link_router,
)
from disclosure_anchor.api.routers.processing_requests import (
    router as processing_requests_router,
)
from disclosure_anchor.api.routers.tracked import router as tracked_router
from disclosure_anchor.api.routers.units import router as units_router
from disclosure_anchor.application.use_cases.processing_requests import (
    ProcessingRequestPolicy,
    ProcessingRequestService,
)
from disclosure_anchor.application.use_cases.read_disclosure_inventory import (
    ReadDisclosureInventory,
)
from disclosure_anchor.application.use_cases.read_original_download_link import (
    ReadOriginalDownloadLink,
)
from disclosure_anchor.application.use_cases.read_quick_document_text import (
    ReadQuickDocumentText,
)
from disclosure_anchor.domain.errors import ConfigurationError, MissingDependencyError
from disclosure_anchor.settings import Settings, load_settings

try:
    from fastapi import Depends, FastAPI
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local environment
    raise MissingDependencyError(
        "fastapi is not installed; install project dependencies before starting the API"
    ) from exc


READ_ROUTERS = (
    health_router,
    documents_router,
    filings_router,
    units_router,
    changes_router,
    tracked_router,
)


def _extend_openapi_error_codes(app: FastAPI) -> None:
    """Publish the agent-only error codes in this app's own OpenAPI document."""

    public_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        schema = public_openapi()
        envelope = schema.get("components", {}).get("schemas", {}).get("ErrorEnvelope")
        if isinstance(envelope, dict):
            codes = envelope["properties"]["error_code"]["enum"]
            for code in AGENT_ERROR_CODES:
                if code not in codes:
                    codes.append(code)
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


def require_loopback_host(host: str) -> None:
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ConfigurationError("DISCLOSURE_AGENT_API_HOST must be a loopback address")


def create_agent_api_app(
    settings: Settings | None = None,
    *,
    validate_runtime: bool = True,
    processing_requests: ProcessingRequestService | None = None,
    quick_text: QuickTextReader | None = None,
    disclosure_inventory: ReadDisclosureInventory | None = None,
    original_download_link: ReadOriginalDownloadLink | None = None,
) -> FastAPI:
    resolved = settings or load_settings()
    access = AgentApiAccess.from_settings(resolved)
    require_loopback_host(resolved.disclosure_agent_api_host)
    app_engine = None
    reader_engine = None
    if validate_runtime:
        if resolved.database_url is not None:
            app_engine = create_db_engine(app_database_url(resolved))
        report = run_startup_preflight(resolved, engine=app_engine)
        if not report.ok:
            if app_engine is not None:
                app_engine.dispose()
            raise ConfigurationError("runtime preflight failed:\n" + render_report(report.results))
    elif resolved.database_url is not None:
        app_engine = create_db_engine(app_database_url(resolved))
    if app_engine is not None:
        try:
            reader_engine = create_db_engine(reader_database_url(resolved))
            if validate_runtime:
                require_runtime_reader_engine(reader_engine)
        except BaseException:
            if reader_engine is not None:
                reader_engine.dispose()
            app_engine.dispose()
            raise

    app = FastAPI(title="disclosure_anchor agent API", version="0.1.0")
    install_error_handlers(app)
    install_agent_error_handlers(app)
    _extend_openapi_error_codes(app)
    app.state.settings = resolved
    app.state.agent_api_access = access
    if app_engine is not None:
        app.state.app_db_engine = app_engine
        app.state.db_engine = app_engine
        app.state.uow_factory = unit_of_work_factory(app_engine)
    if reader_engine is not None:
        app.state.reader_db_engine = reader_engine
    # The resident worker's scope, so every "selected"/"queued" fact is the
    # answer its own scanner would give.
    resident_scope = (
        load_processing_policy(resolved.disclosure_processing_policy_path)
        if app_engine is not None
        else None
    )
    if processing_requests is None and app_engine is not None:
        processing_requests = ProcessingRequestService(
            uow_factory=processing_request_uow_factory(app_engine),
            status_reader=PostgresProcessingStatusReader(
                app_engine,
                max_download_retries=resolved.cninfo_max_retries,
                max_parse_retries=resolved.disclosure_max_parse_retries,
                max_build_retries=resolved.disclosure_max_build_retries,
                scope_classes=resident_scope,
            ),
            policy=ProcessingRequestPolicy(
                daily_request_limit=resolved.disclosure_agent_api_daily_request_limit,
            ),
            clock=lambda: datetime.now(timezone.utc),
        )
    if quick_text is None and app_engine is not None and reader_engine is not None:
        from disclosure_anchor.adapters.parsers.quick_pdf_text import QuickPdfTextExtractor

        quick_text = QuickTextReader(
            use_case=ReadQuickDocumentText(
                locator=PostgresOriginalLocatorReader(app_engine, reader_engine),
                extractor=QuickPdfTextExtractor(
                    timeout_seconds=resolved.disclosure_agent_api_text_timeout_seconds
                ),
                original_root=FileStorePathBuilder(resolved).data_root(),
            ),
            slots=threading.BoundedSemaphore(
                resolved.disclosure_agent_api_text_concurrency
            ),
        )
    if disclosure_inventory is None and app_engine is not None:
        disclosure_inventory = ReadDisclosureInventory(
            PostgresDisclosureInventoryReader(
                app_engine,
                max_download_retries=resolved.cninfo_max_retries,
                max_parse_retries=resolved.disclosure_max_parse_retries,
                max_build_retries=resolved.disclosure_max_build_retries,
                scope_classes=resident_scope,
            )
        )
    if original_download_link is None and app_engine is not None:
        original_download_link = ReadOriginalDownloadLink(
            PostgresOriginalDownloadLinkReader(app_engine)
        )
    app.state.processing_requests = processing_requests
    app.state.quick_text = quick_text
    app.state.disclosure_inventory = disclosure_inventory
    app.state.original_download_link = original_download_link
    for router in READ_ROUTERS:
        if router is not None:
            app.include_router(router, dependencies=[Depends(require_agent_read)])
    app.include_router(processing_requests_router)
    app.include_router(document_text_router)
    app.include_router(disclosure_inventory_router)
    app.include_router(original_download_link_router)
    app.add_middleware(AgentApiGuard, access=access)
    return app
