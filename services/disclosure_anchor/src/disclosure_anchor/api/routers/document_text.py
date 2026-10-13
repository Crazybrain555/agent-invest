"""Quick native text of a document's archived original (read-only)."""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any

from disclosure_anchor.api.agent_auth import require_agent_read
from disclosure_anchor.api.agent_errors import TEXT_EXTRACTION_BUSY
from disclosure_anchor.api.errors import FilingApiError, strict_query_params, validation_error
from disclosure_anchor.api.schemas.agent import DocumentTextOut
from disclosure_anchor.application.contracts.processing_request import DOCUMENT_ID_RE
from disclosure_anchor.application.use_cases.read_quick_document_text import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_PAGES,
    MAX_CHARS,
    MAX_CURSOR_CHARS,
    MAX_PAGES,
    DocumentTextNotFound,
    ReadQuickDocumentText,
)

try:
    from fastapi import APIRouter, Depends, Request
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    APIRouter = None  # type: ignore[assignment, misc]
    Depends = None  # type: ignore[assignment]
    Request = None  # type: ignore[assignment, misc]


@dataclass(frozen=True)
class QuickTextReader:
    """The use case plus the bound on concurrent extraction children."""

    use_case: ReadQuickDocumentText
    slots: threading.BoundedSemaphore


def get_document_text(
    request: Request,
    document_id: str,
    cursor: str | None = None,
    start_page: int | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict[str, Any]:
    if not DOCUMENT_ID_RE.fullmatch(document_id):
        raise DocumentTextNotFound(f"document {document_id} not found")
    if cursor is not None and start_page is not None:
        raise validation_error("start_page", "must be absent when cursor is given")
    if cursor is not None and not 1 <= len(cursor) <= MAX_CURSOR_CHARS:
        raise validation_error("cursor", f"must be 1-{MAX_CURSOR_CHARS} characters")
    if start_page is not None and start_page < 1:
        raise validation_error("start_page", "must be at least 1")
    if not 1 <= max_chars <= MAX_CHARS:
        raise validation_error("max_chars", f"must be 1-{MAX_CHARS}")
    if not 1 <= max_pages <= MAX_PAGES:
        raise validation_error("max_pages", f"must be 1-{MAX_PAGES}")
    reader = getattr(request.app.state, "quick_text", None)
    if not isinstance(reader, QuickTextReader):
        raise RuntimeError("quick text reader is not configured")
    if not reader.slots.acquire(blocking=False):
        raise FilingApiError(
            status_code=503,
            error_code=TEXT_EXTRACTION_BUSY,
            message="text extraction capacity is in use; retry shortly",
            detail={"retry_after_seconds": 1},
        )
    try:
        return reader.use_case.execute(
            document_id=document_id,
            cursor=cursor,
            start_page=start_page,
            max_chars=max_chars,
            max_pages=max_pages,
        )
    finally:
        reader.slots.release()


router: Any
if APIRouter is not None:
    router = APIRouter(dependencies=[Depends(strict_query_params)])
    router.add_api_route(
        "/v1/documents/{document_id}/text",
        get_document_text,
        methods=["GET"],
        response_model=DocumentTextOut,
        dependencies=[Depends(require_agent_read)],
    )
else:  # pragma: no cover
    router = None
