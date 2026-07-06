"""Document unit endpoints backed by public views."""

from __future__ import annotations

import json
from typing import Annotated, Any, NoReturn

from sqlalchemy import text
from sqlalchemy.engine import Engine

from disclosure_anchor.api.db import reader_engine_from_request
from disclosure_anchor.api.errors import gone_superseded, l1_processing_required
from disclosure_anchor.api.pagination import (
    DEFAULT_LIMIT,
    UnitCursor,
    decode_unit_cursor,
    encode_unit_cursor,
    unit_cursor_from_row,
    validate_limit,
    validation_error,
)
from disclosure_anchor.api.routers.documents import (
    _bool_query_param,
    _select_one_document,
    raise_not_found,
)
from disclosure_anchor.api.schemas.public import (
    DocumentV1,
    DocumentUnitV1,
    SourceRefV1,
    UnitContextResponse,
    UnitListResponse,
)
from disclosure_anchor.adapters.db.postgres.schema import PUBLIC_SCHEMA
from disclosure_anchor.domain.services.unit_hashing import canonical_json, sha256_prefixed

try:
    from fastapi import APIRouter, HTTPException, Query, Request
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    APIRouter = None  # type: ignore[assignment, misc]
    HTTPException = None  # type: ignore[assignment, misc]
    Query = None  # type: ignore[assignment]
    Request = None  # type: ignore[assignment, misc]


UNIT_COLUMNS = (
    "asset_id",
    "document_id",
    "processing_run_id",
    "provider_document_id",
    "payload_kind",
    "heading_path",
    "title",
    "order_index",
    "semantic_key",
    "payload",
    "content_hash",
    "structure_hash",
    "quality_status",
    "applicability",
    "page_no",
    "artifact_locator",
    "created_at",
    "contract_version",
    "company_ref",
    "security_ref",
    "security_code",
    "exchange",
    "filing_type",
    "report_period",
    "announcement_date",
    "producer_action_ref",
    "source_ref",
    "parent_ref",
    "asset_kind",
    "observed_at",
    "source_tier",
    "trace_level",
    "raw_file_hash",
    "query_projection_hash",
)

SOURCE_REF_COLUMNS = (
    "service",
    "contract_version",
    "asset_id",
    "source_access_id",
    "document_id",
    "provider",
    "provider_document_id",
    "raw_file_hash",
    "processing_run_id",
    "payload_kind",
    "heading_path",
    "title",
    "unit_content_hash",
    "quality_status",
    "applicability",
    "page_no",
    "artifact_locator",
)


def _query_default() -> Any:
    if Query is None:  # pragma: no cover
        return None
    return Query()


def list_document_units(
    document_id: str,
    request: Request,
    processing_run_id: str | None = None,
    reject_superseded: bool = False,
    payload_kind: str | None = None,
    semantic_key: str | None = None,
    quality_status: str | None = None,
    heading_prefix: Annotated[list[str] | None, _query_default()] = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> UnitListResponse:
    engine = reader_engine_from_request(request)
    document = _select_one_document(engine=engine, document_id=document_id)
    if document is None:
        raise_not_found()
    if document["superseded_by_document_id"] is not None and (
        reject_superseded or _bool_query_param(request, "reject_superseded")
    ):
        gone_superseded(str(document["superseded_by_document_id"]))
    active_run_id = document["current_processing_run_id"]
    selected_run_id = processing_run_id or active_run_id
    if selected_run_id is None:
        raise_l1_required(str(document["status"]))
    if processing_run_id is not None and not _run_belongs_to_document(
        engine=engine, document_id=document_id, processing_run_id=processing_run_id
    ):
        raise_not_found()

    rows = _select_units(
        engine=engine,
        document_id=document_id,
        processing_run_id=selected_run_id,
        filters=UnitFilters(
            payload_kind=payload_kind,
            semantic_key=semantic_key,
            quality_status=quality_status,
            heading_prefix=heading_prefix or [],
        ),
        cursor=decode_unit_cursor(cursor),
        limit=validate_limit(limit),
    )
    warning = _latest_processing_warning(
        engine=engine,
        document_id=document_id,
        active_run_id=active_run_id,
    )
    return _unit_list_response(rows=rows, limit=limit, warning=warning)


def get_unit(asset_id: str, request: Request) -> DocumentUnitV1:
    engine = reader_engine_from_request(request)
    row = _select_unit(engine=engine, asset_id=asset_id)
    if row is None:
        raise_not_found()
    return DocumentUnitV1.model_validate(row)


def get_unit_source_ref(asset_id: str, request: Request) -> SourceRefV1:
    engine = reader_engine_from_request(request)
    row = _select_source_ref(engine=engine, asset_id=asset_id)
    if row is None:
        raise_not_found()
    return SourceRefV1.model_validate(row)


def get_unit_context(
    asset_id: str,
    request: Request,
    max_chars: int | None = None,
) -> UnitContextResponse:
    if max_chars is not None and max_chars < 0:
        raise validation_error("max_chars", "must be greater than or equal to 0")
    engine = reader_engine_from_request(request)
    unit = _select_unit(engine=engine, asset_id=asset_id)
    if unit is None:
        raise_not_found()
    document = _select_one_document(engine=engine, document_id=str(unit["document_id"]))
    if document is None:
        raise_not_found()
    payload: dict[str, Any] = unit["payload"]
    response = UnitContextResponse(
        asset_id=str(unit["asset_id"]),
        asset_uri=str(unit["asset_uri"]),
        is_active_run=bool(unit["is_active_run"]),
        document=DocumentV1.model_validate(document),
        heading_path=list(unit["heading_path"]),
        title=unit["title"],
        payload=payload,
    )
    if max_chars is not None:
        source = canonical_json(payload)
        excerpt = source[: min(max_chars, len(source))]
        response.excerpt = excerpt
        response.start = 0
        response.end = len(excerpt)
        response.excerpt_hash = sha256_prefixed(excerpt)
    return response


class UnitFilters:
    def __init__(
        self,
        *,
        payload_kind: str | None = None,
        semantic_key: str | None = None,
        quality_status: str | None = None,
        heading_prefix: list[str] | None = None,
    ) -> None:
        self.payload_kind = payload_kind
        self.semantic_key = semantic_key
        self.quality_status = quality_status
        self.heading_prefix = heading_prefix or []


def asset_uri(asset_id: str) -> str:
    return f"asset://disclosure_anchor/v1/document_unit/{asset_id}"


def _unit_select_columns() -> str:
    columns = ", ".join(f"u.{column}" for column in UNIT_COLUMNS)
    return (
        f"{columns}, "
        "('asset://disclosure_anchor/v1/document_unit/' || u.asset_id) AS asset_uri, "
        "COALESCE(r.is_active, false) AS is_active_run"
    )


def _select_units(
    *,
    engine: Engine,
    document_id: str,
    processing_run_id: str,
    filters: UnitFilters,
    cursor: UnitCursor | None,
    limit: int,
) -> list[dict[str, Any]]:
    where, params = _unit_where(filters)
    where.extend(
        [
            "u.document_id = :document_id",
            "u.processing_run_id = :processing_run_id",
        ]
    )
    params["document_id"] = document_id
    params["processing_run_id"] = processing_run_id
    _append_unit_cursor(where=where, params=params, cursor=cursor)
    params["limit_plus_one"] = limit + 1
    sql = (
        f"SELECT {_unit_select_columns()} "
        f"FROM {PUBLIC_SCHEMA}.document_units_v1 u "
        f"JOIN {PUBLIC_SCHEMA}.processing_runs_v1 r "
        "ON r.processing_run_id = u.processing_run_id "
        f"{_where_sql(where)} "
        "ORDER BY u.order_index ASC, u.asset_id ASC "
        "LIMIT :limit_plus_one"
    )
    return _fetch_all(engine, sql, params)


def _select_unit(*, engine: Engine, asset_id: str) -> dict[str, Any] | None:
    sql = (
        f"SELECT {_unit_select_columns()} "
        f"FROM {PUBLIC_SCHEMA}.document_units_v1 u "
        f"JOIN {PUBLIC_SCHEMA}.processing_runs_v1 r "
        "ON r.processing_run_id = u.processing_run_id "
        "WHERE u.asset_id = :asset_id"
    )
    with engine.connect() as conn:
        row = conn.execute(text(sql), {"asset_id": asset_id}).mappings().one_or_none()
    return dict(row) if row is not None else None


def _select_source_ref(*, engine: Engine, asset_id: str) -> dict[str, Any] | None:
    sql = (
        f"SELECT {', '.join(SOURCE_REF_COLUMNS)} "
        f"FROM {PUBLIC_SCHEMA}.source_refs_v1 "
        "WHERE asset_id = :asset_id"
    )
    with engine.connect() as conn:
        row = conn.execute(text(sql), {"asset_id": asset_id}).mappings().one_or_none()
    return dict(row) if row is not None else None


def _run_belongs_to_document(
    *, engine: Engine, document_id: str, processing_run_id: str
) -> bool:
    sql = (
        "SELECT 1 "
        f"FROM {PUBLIC_SCHEMA}.processing_runs_v1 "
        "WHERE document_id = :document_id "
        "AND processing_run_id = :processing_run_id"
    )
    with engine.connect() as conn:
        return (
            conn.execute(
                text(sql),
                {
                    "document_id": document_id,
                    "processing_run_id": processing_run_id,
                },
            ).scalar_one_or_none()
            is not None
        )


def _latest_processing_warning(
    *, engine: Engine, document_id: str, active_run_id: str | None
) -> str | None:
    if active_run_id is None:
        return None
    sql = (
        "SELECT processing_run_id, status, unit_build_status "
        f"FROM {PUBLIC_SCHEMA}.processing_runs_v1 "
        "WHERE document_id = :document_id "
        "ORDER BY started_at DESC, processing_run_id DESC "
        "LIMIT 1"
    )
    with engine.connect() as conn:
        row = conn.execute(text(sql), {"document_id": document_id}).mappings().one_or_none()
    if row is None or row["processing_run_id"] == active_run_id:
        return None
    if row["status"] == "failed" or row["unit_build_status"] == "failed":
        return "LATEST_PROCESSING_FAILED"
    return None


def _unit_where(filters: UnitFilters) -> tuple[list[str], dict[str, Any]]:
    where: list[str] = []
    params: dict[str, Any] = {}
    _add_filter(where, params, "payload_kind", filters.payload_kind)
    _add_filter(where, params, "semantic_key", filters.semantic_key)
    _add_filter(where, params, "quality_status", filters.quality_status)
    if filters.heading_prefix:
        params["heading_prefix_json"] = json.dumps(
            filters.heading_prefix,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        params["heading_prefix_len"] = len(filters.heading_prefix)
        where.append("u.heading_path @> CAST(:heading_prefix_json AS jsonb)")
        where.append(
            "jsonb_array_length(u.heading_path) >= :heading_prefix_len"
        )
        for index, value in enumerate(filters.heading_prefix):
            key = f"heading_prefix_{index}"
            params[key] = value
            where.append(f"u.heading_path ->> {index} = :{key}")
    return where, params


def _add_filter(
    where: list[str], params: dict[str, Any], column: str, value: str | None
) -> None:
    if value is None:
        return
    where.append(f"u.{column} = :{column}")
    params[column] = value


def _append_unit_cursor(
    *,
    where: list[str],
    params: dict[str, Any],
    cursor: UnitCursor | None,
) -> None:
    if cursor is None:
        return
    where.append("(u.order_index, u.asset_id) > (:cursor_order_index, :cursor_asset_id)")
    params["cursor_order_index"] = cursor.order_index
    params["cursor_asset_id"] = cursor.asset_id


def _where_sql(where: list[str]) -> str:
    return "WHERE " + " AND ".join(where) if where else ""


def _fetch_all(
    engine: Engine, sql: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def _unit_list_response(
    *, rows: list[dict[str, Any]], limit: int, warning: str | None
) -> UnitListResponse:
    page_rows = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_unit_cursor(unit_cursor_from_row(page_rows[-1]))
    return UnitListResponse(
        items=[DocumentUnitV1.model_validate(row) for row in page_rows],
        next_cursor=next_cursor,
        warning=warning,
    )


def raise_l1_required(status: str) -> NoReturn:
    l1_processing_required(status)


router: Any
if APIRouter is not None and Query is not None:
    router = APIRouter()
    router.add_api_route(
        "/v1/documents/{document_id}/units",
        list_document_units,
        methods=["GET"],
        response_model=UnitListResponse,
    )
    router.add_api_route(
        "/v1/units/{asset_id}",
        get_unit,
        methods=["GET"],
        response_model=DocumentUnitV1,
    )
    router.add_api_route(
        "/v1/units/{asset_id}/source-ref",
        get_unit_source_ref,
        methods=["GET"],
        response_model=SourceRefV1,
    )
    router.add_api_route(
        "/v1/units/{asset_id}/context",
        get_unit_context,
        methods=["GET"],
        response_model=UnitContextResponse,
        response_model_exclude_none=True,
    )
else:
    router = None
