"""Change feed endpoint."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from disclosure_anchor.api.db import reader_engine_from_request
from disclosure_anchor.api.pagination import (
    DEFAULT_LIMIT,
    ChangeCursor,
    change_cursor_from_row,
    decode_change_cursor,
    encode_change_cursor,
    validate_limit,
)
from disclosure_anchor.api.schemas.public import ChangeEventV1, ChangeListResponse
from disclosure_anchor.adapters.db.postgres.schema import PUBLIC_SCHEMA

try:
    from fastapi import APIRouter, Request
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    APIRouter = None  # type: ignore[assignment, misc]
    Request = None  # type: ignore[assignment, misc]


CHANGE_COLUMNS = (
    "seq",
    "event_id",
    "event_kind",
    "document_id",
    "processing_run_id",
    "asset_id",
    "payload",
    "occurred_at",
    "change_kind",
    "subject_kind",
    "subject_ref",
    "source",
    "contract_version",
    "created_at",
)


def list_changes(
    request: Request,
    after_seq: int | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> ChangeListResponse:
    engine = reader_engine_from_request(request)
    decoded_cursor = decode_change_cursor(cursor)
    rows = _select_changes(
        engine=engine,
        cursor=decoded_cursor,
        after_seq=after_seq,
        limit=validate_limit(limit),
    )
    return _change_list_response(rows=rows, limit=limit)


def _select_changes(
    *,
    engine: Engine,
    cursor: ChangeCursor | None,
    after_seq: int | None,
    limit: int,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit_plus_one": limit + 1}
    where = ""
    if cursor is not None:
        where = "WHERE seq > :after_seq"
        params["after_seq"] = cursor.seq
    elif after_seq is not None:
        where = "WHERE seq > :after_seq"
        params["after_seq"] = after_seq
    sql = (
        f"SELECT {', '.join(CHANGE_COLUMNS)} "
        f"FROM {PUBLIC_SCHEMA}.change_events_v1 "
        f"{where} "
        "ORDER BY seq ASC "
        "LIMIT :limit_plus_one"
    )
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def _change_list_response(
    *, rows: list[dict[str, Any]], limit: int
) -> ChangeListResponse:
    page_rows = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_change_cursor(change_cursor_from_row(page_rows[-1]))
    return ChangeListResponse(
        items=[ChangeEventV1.model_validate(row) for row in page_rows],
        next_cursor=next_cursor,
    )


router: Any
if APIRouter is not None:
    router = APIRouter()
    router.add_api_route(
        "/v1/changes",
        list_changes,
        methods=["GET"],
        response_model=ChangeListResponse,
    )
else:
    router = None
