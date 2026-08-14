"""Document read endpoints backed by disclosure_public views."""

from __future__ import annotations

import json

from datetime import date
from typing import Any, NoReturn

from sqlalchemy import text
from sqlalchemy.engine import Engine

from disclosure_anchor.api.db import reader_engine_from_request
from disclosure_anchor.api.errors import (
    gone_superseded,
    not_found,
    strict_query_params,
    validation_error,
)
from disclosure_anchor.api.pagination import (
    DEFAULT_LIMIT,
    DocumentCursor,
    decode_document_cursor,
    document_cursor_from_row,
    encode_document_cursor,
    validate_limit,
)
from disclosure_anchor.api.schemas.public import (
    DocumentListResponse,
    DocumentV1,
    ProcessingRunV1,
)
from disclosure_anchor.adapters.db.postgres.schema import PUBLIC_SCHEMA

try:
    from fastapi import APIRouter, Depends, HTTPException, Request
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    APIRouter = None  # type: ignore[assignment, misc]
    Depends = None  # type: ignore[assignment]
    HTTPException = None  # type: ignore[assignment, misc]
    Request = None  # type: ignore[assignment, misc]


DOCUMENT_COLUMNS = (
    "document_id",
    "provider",
    "provider_document_id",
    "security_code",
    "exchange",
    "filing_type",
    "disclosure_topics",
    "title",
    "announcement_date",
    "report_period",
    "raw_file_hash",
    "status",
    "current_processing_run_id",
    "created_at",
    "updated_at",
    "contract_version",
    "company_ref",
    "security_ref",
    "source_ref",
    "supersedes_document_id",
    "correction_of_document_id",
    "superseded_by_document_id",
    "provider_metadata",
    "publisher_categories",
    "market",
    "content_categories",
)

PROCESSING_RUN_COLUMNS = (
    "processing_run_id",
    "document_id",
    "artifact_owner_processing_run_id",
    "run_kind",
    "status",
    "parser_name",
    "parser_version",
    "artifact_hash",
    "content_hash_aggregate",
    "structure_hash",
    "is_active",
    "started_at",
    "finished_at",
    "created_at",
    "parser_backend",
    "input_raw_file_hash",
    "parser_method",
    "parser_language",
    "unit_build_status",
    "unit_build_attempt_count",
    "unit_built_at",
    "builder_rules_version",
)


def list_documents(
    request: Request,
    company_ref: str | None = None,
    security_code: str | None = None,
    filing_type: str | None = None,
    disclosure_topic: str | None = None,
    content_category: str | None = None,
    title_contains: str | None = None,
    report_period: str | None = None,
    announcement_date_from: date | None = None,
    announcement_date_to: date | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> DocumentListResponse:
    engine = reader_engine_from_request(request)
    rows = _select_documents(
        engine=engine,
        filters=DocumentFilters(
            company_ref=company_ref,
            security_code=security_code,
            filing_type=validate_multi_value("filing_type", filing_type),
            disclosure_topic=validate_multi_value("disclosure_topic", disclosure_topic),
            content_category=validate_multi_value("content_category", content_category),
            title_contains=validate_title_contains(title_contains),
            report_period=report_period,
            announcement_date_from=announcement_date_from,
            announcement_date_to=announcement_date_to,
            status=status,
        ),
        cursor=decode_document_cursor(cursor),
        limit=validate_limit(limit),
    )
    return _document_list_response(rows=rows, limit=limit)


def get_document(
    document_id: str, request: Request, reject_superseded: bool = False
) -> DocumentV1:
    engine = reader_engine_from_request(request)
    row = _select_one_document(engine=engine, document_id=document_id)
    if row is None:
        raise_not_found()
    if row["superseded_by_document_id"] is not None:
        if reject_superseded or _bool_query_param(request, "reject_superseded"):
            gone_superseded(str(row["superseded_by_document_id"]))
    return DocumentV1.model_validate(row)


def list_document_runs(document_id: str, request: Request) -> list[ProcessingRunV1]:
    engine = reader_engine_from_request(request)
    rows = _select_processing_runs(engine=engine, document_id=document_id)
    return [ProcessingRunV1.model_validate(row) for row in rows]


class DocumentFilters:
    def __init__(
        self,
        *,
        company_ref: str | None = None,
        security_code: str | None = None,
        filing_type: list[str] | None = None,
        disclosure_topic: list[str] | None = None,
        content_category: list[str] | None = None,
        title_contains: str | None = None,
        report_period: str | None = None,
        announcement_date_from: date | None = None,
        announcement_date_to: date | None = None,
        status: str | None = None,
    ) -> None:
        self.company_ref = company_ref
        self.security_code = security_code
        self.filing_type = filing_type
        self.disclosure_topic = disclosure_topic
        self.content_category = content_category
        self.title_contains = title_contains
        self.report_period = report_period
        self.announcement_date_from = announcement_date_from
        self.announcement_date_to = announcement_date_to
        self.status = status


def _select_documents(
    *,
    engine: Engine,
    filters: DocumentFilters,
    cursor: DocumentCursor | None,
    limit: int,
) -> list[dict[str, Any]]:
    where, params = _document_where(filters)
    _append_document_cursor(where=where, params=params, cursor=cursor)
    params["limit_plus_one"] = limit + 1
    sql = (
        f"SELECT {', '.join(DOCUMENT_COLUMNS)} "
        f"FROM {PUBLIC_SCHEMA}.documents_v1 "
        f"{_where_sql(where)} "
        "ORDER BY announcement_date DESC NULLS LAST, document_id DESC "
        "LIMIT :limit_plus_one"
    )
    return _fetch_all(engine, sql, params)


def _select_one_document(*, engine: Engine, document_id: str) -> dict[str, Any] | None:
    sql = (
        f"SELECT {', '.join(DOCUMENT_COLUMNS)} "
        f"FROM {PUBLIC_SCHEMA}.documents_v1 WHERE document_id = :document_id"
    )
    with engine.connect() as conn:
        row = (
            conn.execute(text(sql), {"document_id": document_id})
            .mappings()
            .one_or_none()
        )
    return dict(row) if row is not None else None


def _select_processing_runs(
    *, engine: Engine, document_id: str
) -> list[dict[str, Any]]:
    sql = (
        f"SELECT {', '.join(PROCESSING_RUN_COLUMNS)} "
        f"FROM {PUBLIC_SCHEMA}.processing_runs_v1 "
        "WHERE document_id = :document_id "
        "ORDER BY started_at DESC, processing_run_id DESC"
    )
    return _fetch_all(engine, sql, {"document_id": document_id})


def _document_where(filters: DocumentFilters) -> tuple[list[str], dict[str, Any]]:
    where: list[str] = []
    params: dict[str, Any] = {}
    _add_filter(where, params, "company_ref", filters.company_ref)
    _add_filter(where, params, "security_code", filters.security_code)
    if filters.filing_type is not None:
        # Comma-separated multi-value (round24, industry standard: EDGAR
        # forms / cninfo category are multi-select); single value unchanged.
        where.append("filing_type = ANY(CAST(:filing_types AS text[]))")
        params["filing_types"] = filters.filing_type
    _add_filter(where, params, "report_period", filters.report_period)
    _add_filter(where, params, "status", filters.status)
    if filters.disclosure_topic is not None:
        # disclosure_topics is a jsonb array (documents_v1); ANY-of match.
        # Function form of ?| — avoids operator-escaping pitfalls in text().
        where.append(
            "jsonb_exists_any(disclosure_topics, CAST(:disclosure_topics AS text[]))"
        )
        params["disclosure_topics"] = filters.disclosure_topic
    if filters.content_category is not None:
        # content_categories is a jsonb array of {code, name}; a value hits
        # when it equals any element's code OR name (round24 — the source
        # cninfo category dimension, previously response-only).
        # Containment form so the 0027 GIN index on the materialized
        # column serves the filter; per-element EXISTS forced a full scan.
        clauses = []
        for index, value in enumerate(filters.content_category):
            for field in ("code", "name"):
                key = f"content_category_{field}_{index}"
                clauses.append(f"content_categories @> CAST(:{key} AS jsonb)")
                params[key] = json.dumps([{field: value}])
        where.append("(" + " OR ".join(clauses) + ")")
    if filters.title_contains is not None:
        where.append("title ILIKE :title_pattern ESCAPE '\\'")
        params["title_pattern"] = "%" + _escape_like(filters.title_contains) + "%"
    if filters.announcement_date_from is not None:
        where.append("announcement_date >= :announcement_date_from")
        params["announcement_date_from"] = filters.announcement_date_from
    if filters.announcement_date_to is not None:
        where.append("announcement_date <= :announcement_date_to")
        params["announcement_date_to"] = filters.announcement_date_to
    return where, params


def _add_filter(
    where: list[str], params: dict[str, Any], column: str, value: str | None
) -> None:
    if value is None:
        return
    where.append(f"{column} = :{column}")
    params[column] = value


def validate_multi_value(field: str, value: str | None) -> list[str] | None:
    """Comma-separated multi-value filter (round24). No enum check — these
    are open, versioned vocabularies; the rule is at least one non-blank
    item. A blank/empty value routes to the standard VALIDATION_ERROR
    envelope instead of silently matching nothing."""

    if value is None:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items:
        raise validation_error(field, "must contain at least one non-empty value")
    return items


def validate_title_contains(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        raise validation_error("title_contains", "must be a non-empty string")
    if len(trimmed) > 100:
        raise validation_error("title_contains", "must be at most 100 characters")
    return trimmed


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _append_document_cursor(
    *,
    where: list[str],
    params: dict[str, Any],
    cursor: DocumentCursor | None,
) -> None:
    if cursor is None:
        return
    params["cursor_document_id"] = cursor.document_id
    if cursor.announcement_date is None:
        where.append("announcement_date IS NULL AND document_id < :cursor_document_id")
        return
    params["cursor_announcement_date"] = cursor.announcement_date
    where.append(
        "("
        "announcement_date IS NULL OR "
        "announcement_date < :cursor_announcement_date OR "
        "(announcement_date = :cursor_announcement_date "
        "AND document_id < :cursor_document_id)"
        ")"
    )


def _where_sql(where: list[str]) -> str:
    return "WHERE " + " AND ".join(where) if where else ""


def _fetch_all(
    engine: Engine, sql: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def _document_list_response(
    *, rows: list[dict[str, Any]], limit: int
) -> DocumentListResponse:
    page_rows = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_document_cursor(document_cursor_from_row(page_rows[-1]))
    return DocumentListResponse(
        items=[DocumentV1.model_validate(row) for row in page_rows],
        next_cursor=next_cursor,
    )


def _bool_query_param(request: Request, name: str) -> bool:
    value = request.query_params.get(name)
    return value in {"1", "true", "True", "yes", "on"}


def raise_not_found() -> NoReturn:
    not_found()


router: Any
if APIRouter is not None:
    router = APIRouter(dependencies=[Depends(strict_query_params)])
    router.add_api_route(
        "/v1/documents",
        list_documents,
        methods=["GET"],
        response_model=DocumentListResponse,
    )
    router.add_api_route(
        "/v1/documents/{document_id}",
        get_document,
        methods=["GET"],
        response_model=DocumentV1,
    )
    router.add_api_route(
        "/v1/documents/{document_id}/runs",
        list_document_runs,
        methods=["GET"],
        response_model=list[ProcessingRunV1],
    )
    # Registered here (not a new top-level router) so the vocabulary catalog
    # behind documents_v1.disclosure_topics/filing_type mounts without a
    # main.py change. Imported at point of use to avoid an import cycle.
    from disclosure_anchor.api.routers.classification import (
        get_classification,
        get_semantic_routes,
    )
    from disclosure_anchor.api.schemas.public import (
        ClassificationResponse,
        SemanticRouteCatalogResponse,
    )

    router.add_api_route(
        "/v1/classification",
        get_classification,
        methods=["GET"],
        response_model=ClassificationResponse,
    )
    router.add_api_route(
        "/v1/semantic-routes",
        get_semantic_routes,
        methods=["GET"],
        response_model=SemanticRouteCatalogResponse,
    )
else:
    router = None
