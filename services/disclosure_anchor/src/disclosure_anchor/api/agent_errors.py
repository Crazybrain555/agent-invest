"""Error mapping of the private agent API onto the Filing API envelope.

These codes are agent-API only; they are not added to the public read-side
ErrorEnvelope enum. A database connectivity loss or exclusive corpus
maintenance is reported as 503 SERVICE_UNAVAILABLE: for the write that means
"outcome unknown — retry the same idempotency key", never success or absence.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import InterfaceError, OperationalError

from disclosure_anchor.api.errors import (
    FORBIDDEN,
    NOT_FOUND,
    SERVICE_UNAVAILABLE,
    UNAUTHORIZED,
    VALIDATION_ERROR,
    FilingApiError,
)
from disclosure_anchor.application.contracts.processing_request import (
    ProcessingRequestInvalid,
)
from disclosure_anchor.application.contracts.quick_pdf_text import QuickPdfTextError
from disclosure_anchor.application.use_cases.processing_requests import (
    IdempotencyConflict,
    ProcessingRequestLimitExceeded,
    ProcessingRequestNotFound,
    ProcessingTargetMismatch,
    ProcessingTargetNotFound,
)
from disclosure_anchor.application.use_cases.read_quick_document_text import (
    DocumentTextNotFound,
    OriginalTextIntegrityError,
    OriginalTextUnavailable,
)
from disclosure_anchor.application.worker.locks import CorpusWriteBusyError

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    FastAPI = None  # type: ignore[assignment, misc]
    Request = None  # type: ignore[assignment, misc]
    JSONResponse = None  # type: ignore[assignment, misc]


IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
PROCESSING_REQUEST_LIMIT = "PROCESSING_REQUEST_LIMIT"
TARGET_MISMATCH = "TARGET_MISMATCH"
ORIGINAL_UNAVAILABLE = "ORIGINAL_UNAVAILABLE"
ORIGINAL_INTEGRITY_ERROR = "ORIGINAL_INTEGRITY_ERROR"
TEXT_SOURCE_UNSUPPORTED = "TEXT_SOURCE_UNSUPPORTED"
TEXT_CURSOR_SOURCE_MISMATCH = "TEXT_CURSOR_SOURCE_MISMATCH"
TEXT_INVALID_CURSOR = "TEXT_INVALID_CURSOR"
TEXT_EXTRACTION_FAILED = "TEXT_EXTRACTION_FAILED"
TEXT_EXTRACTION_BUSY = "TEXT_EXTRACTION_BUSY"
TEXT_EXTRACTION_TIMEOUT = "TEXT_EXTRACTION_TIMEOUT"
PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
LENGTH_REQUIRED = "LENGTH_REQUIRED"
AGENT_ERROR_CODES = (
    UNAUTHORIZED,
    FORBIDDEN,
    SERVICE_UNAVAILABLE,
    IDEMPOTENCY_CONFLICT,
    PROCESSING_REQUEST_LIMIT,
    TARGET_MISMATCH,
    ORIGINAL_UNAVAILABLE,
    ORIGINAL_INTEGRITY_ERROR,
    TEXT_SOURCE_UNSUPPORTED,
    TEXT_CURSOR_SOURCE_MISMATCH,
    TEXT_INVALID_CURSOR,
    TEXT_EXTRACTION_FAILED,
    TEXT_EXTRACTION_BUSY,
    TEXT_EXTRACTION_TIMEOUT,
    PAYLOAD_TOO_LARGE,
    LENGTH_REQUIRED,
)

# Quick-text extractor codes (quick-text contract) → HTTP status and API code.
_QUICK_TEXT_ERRORS: dict[str, tuple[int, str]] = {
    "invalid_request": (422, VALIDATION_ERROR),
    "invalid_cursor": (422, TEXT_INVALID_CURSOR),
    "cursor_source_mismatch": (409, TEXT_CURSOR_SOURCE_MISMATCH),
    "source_not_found": (500, ORIGINAL_INTEGRITY_ERROR),
    "source_identity_changed": (500, ORIGINAL_INTEGRITY_ERROR),
    "source_hash_mismatch": (500, ORIGINAL_INTEGRITY_ERROR),
    "source_unavailable": (503, ORIGINAL_UNAVAILABLE),
    "invalid_pdf": (409, TEXT_SOURCE_UNSUPPORTED),
    "password_required": (409, TEXT_SOURCE_UNSUPPORTED),
    "security_unsupported": (409, TEXT_SOURCE_UNSUPPORTED),
    "extraction_failed": (502, TEXT_EXTRACTION_FAILED),
    "extraction_process_failed": (502, TEXT_EXTRACTION_FAILED),
    "extraction_timeout": (504, TEXT_EXTRACTION_TIMEOUT),
}


def quick_text_error(exc: QuickPdfTextError) -> FilingApiError:
    status_code, error_code = _QUICK_TEXT_ERRORS.get(
        exc.code, (502, TEXT_EXTRACTION_FAILED)
    )
    return FilingApiError(
        status_code=status_code,
        error_code=error_code,
        message=exc.message,
        detail={"reason": exc.code},
    )


def agent_api_error(exc: Exception) -> FilingApiError | None:
    if isinstance(exc, ProcessingRequestInvalid):
        return FilingApiError(
            status_code=422,
            error_code=VALIDATION_ERROR,
            message="request validation failed",
            detail={"errors": [{"field": exc.field_name, "message": exc.message}]},
        )
    if isinstance(exc, IdempotencyConflict):
        return FilingApiError(
            status_code=409,
            error_code=IDEMPOTENCY_CONFLICT,
            message="idempotency key is bound to a different intent",
            detail={"request_id": exc.request_id, "intent_sha256": exc.intent_sha256},
        )
    if isinstance(exc, ProcessingRequestLimitExceeded):
        return FilingApiError(
            status_code=429,
            error_code=PROCESSING_REQUEST_LIMIT,
            message="too many new processing requests",
            detail={"limit": exc.limit, "window_hours": exc.window_hours},
        )
    if isinstance(exc, ProcessingTargetMismatch):
        return FilingApiError(
            status_code=422,
            error_code=TARGET_MISMATCH,
            message="local filing contradicts the requested security or window",
            detail={"found": exc.found},
        )
    if isinstance(exc, (ProcessingTargetNotFound, ProcessingRequestNotFound, DocumentTextNotFound)):
        return FilingApiError(status_code=404, error_code=NOT_FOUND, message="resource not found")
    if isinstance(exc, OriginalTextUnavailable):
        return FilingApiError(
            status_code=409,
            error_code=ORIGINAL_UNAVAILABLE,
            message="document has no archived original",
            detail={"reason": exc.reason},
        )
    if isinstance(exc, OriginalTextIntegrityError):
        return FilingApiError(
            status_code=500,
            error_code=ORIGINAL_INTEGRITY_ERROR,
            message="archived original failed verification",
            detail={"reason": exc.reason},
        )
    if isinstance(exc, QuickPdfTextError):
        return quick_text_error(exc)
    if isinstance(exc, CorpusWriteBusyError):
        return _unavailable("corpus_maintenance_active")
    if isinstance(exc, (OperationalError, InterfaceError)):
        return _unavailable("database_unavailable")
    return None


def _unavailable(reason: str) -> FilingApiError:
    return FilingApiError(
        status_code=503,
        error_code=SERVICE_UNAVAILABLE,
        message="backend unavailable; a write outcome is unknown, retry the same key",
        detail={"reason": reason},
    )


_HANDLED: tuple[type[Exception], ...] = (
    ProcessingRequestInvalid,
    IdempotencyConflict,
    ProcessingRequestLimitExceeded,
    ProcessingTargetMismatch,
    ProcessingTargetNotFound,
    ProcessingRequestNotFound,
    DocumentTextNotFound,
    OriginalTextUnavailable,
    OriginalTextIntegrityError,
    QuickPdfTextError,
    CorpusWriteBusyError,
    OperationalError,
    InterfaceError,
)


def install_agent_error_handlers(app: FastAPI) -> None:
    async def _handler(request: Request, exc: Exception) -> Any:
        api_error = agent_api_error(exc)
        if api_error is None:  # pragma: no cover - only registered types reach here
            raise exc
        return JSONResponse(status_code=api_error.status_code, content=api_error.body())

    for exc_type in _HANDLED:
        app.add_exception_handler(exc_type, _handler)
