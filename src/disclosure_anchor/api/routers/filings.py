"""Filing collection endpoints."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.engine import Engine

from disclosure_anchor.api.db import reader_engine_from_request
from disclosure_anchor.api.pagination import (
    DEFAULT_LIMIT,
    DocumentCursor,
    decode_document_cursor,
    validate_limit,
)
from disclosure_anchor.api.routers.documents import (
    DOCUMENT_COLUMNS,
    DocumentFilters,
    _append_document_cursor,
    _document_list_response,
    _document_where,
    _fetch_all,
    _where_sql,
)
from disclosure_anchor.api.schemas.public import DocumentListResponse
from disclosure_anchor.adapters.db.postgres.schema import PUBLIC_SCHEMA

try:
    from fastapi import APIRouter, Request
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    APIRouter = None  # type: ignore[assignment, misc]
    Request = None  # type: ignore[assignment, misc]


def latest_filings(
    request: Request,
    company_ref: str | None = None,
    security_code: str | None = None,
    filing_type: str | None = None,
    report_period: str | None = None,
    announcement_date_from: date | None = None,
    announcement_date_to: date | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> DocumentListResponse:
    engine = reader_engine_from_request(request)
    rows = _select_latest_filings(
        engine=engine,
        filters=DocumentFilters(
            company_ref=company_ref,
            security_code=security_code,
            filing_type=filing_type,
            report_period=report_period,
            announcement_date_from=announcement_date_from,
            announcement_date_to=announcement_date_to,
            status=status,
        ),
        cursor=decode_document_cursor(cursor),
        limit=validate_limit(limit),
    )
    return _document_list_response(rows=rows, limit=limit)


def _select_latest_filings(
    *,
    engine: Engine,
    filters: DocumentFilters,
    cursor: DocumentCursor | None,
    limit: int,
) -> list[dict[str, Any]]:
    inner_where, params = _document_where(filters)
    inner_where.append("superseded_by_document_id IS NULL")
    outer_where: list[str] = []
    _append_document_cursor(where=outer_where, params=params, cursor=cursor)
    params["limit_plus_one"] = limit + 1
    sql = (
        f"SELECT {', '.join(DOCUMENT_COLUMNS)} "
        "FROM ("
        "SELECT DISTINCT ON (company_ref, filing_type, report_period) "
        f"{', '.join(DOCUMENT_COLUMNS)} "
        f"FROM {PUBLIC_SCHEMA}.documents_v1 "
        f"{_where_sql(inner_where)} "
        "ORDER BY company_ref, filing_type, report_period, "
        "announcement_date DESC NULLS LAST, document_id DESC"
        ") latest "
        f"{_where_sql(outer_where)} "
        "ORDER BY announcement_date DESC NULLS LAST, document_id DESC "
        "LIMIT :limit_plus_one"
    )
    return _fetch_all(engine, sql, params)


router: Any
if APIRouter is not None:
    router = APIRouter()
    router.add_api_route(
        "/v1/filings/latest",
        latest_filings,
        methods=["GET"],
        response_model=DocumentListResponse,
    )
else:
    router = None
