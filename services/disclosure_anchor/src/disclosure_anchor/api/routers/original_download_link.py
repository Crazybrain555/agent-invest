"""Authenticated read of a recorded official original-PDF link."""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from disclosure_anchor.api.agent_auth import require_agent_read
from disclosure_anchor.api.errors import FilingApiError, not_found, strict_query_params
from disclosure_anchor.api.schemas.original_download_link import OriginalDownloadLinkOut
from disclosure_anchor.application.contracts.processing_request import DOCUMENT_ID_RE
from disclosure_anchor.application.use_cases.read_original_download_link import (
    DownloadLinkDocumentNotFound,
    ReadOriginalDownloadLink,
)

try:
    from fastapi import APIRouter, Depends, Request
except ModuleNotFoundError:  # pragma: no cover - app startup validates availability
    APIRouter = None  # type: ignore[assignment, misc]
    Depends = None  # type: ignore[assignment]
    Request = None  # type: ignore[assignment, misc]


def get_original_download_link(request: Request, document_id: str) -> dict[str, Any]:
    if not DOCUMENT_ID_RE.fullmatch(document_id):
        not_found("document not found")
    reader = getattr(request.app.state, "original_download_link", None)
    if not isinstance(reader, ReadOriginalDownloadLink):
        raise FilingApiError(status_code=503, error_code="SERVICE_UNAVAILABLE", message="Original download link reader is unavailable")
    try:
        return reader.execute(document_id)
    except DownloadLinkDocumentNotFound:
        not_found("document not found")
    except SQLAlchemyError as exc:
        raise FilingApiError(status_code=503, error_code="SERVICE_UNAVAILABLE", message="Original download link is unavailable") from exc


router: Any
if APIRouter is not None:
    router = APIRouter(dependencies=[Depends(strict_query_params)])
    router.add_api_route(
        "/v1/documents/{document_id}/download-link", get_original_download_link,
        methods=["GET"], response_model=OriginalDownloadLinkOut,
        dependencies=[Depends(require_agent_read)],
    )
else:  # pragma: no cover
    router = None
