"""Opaque keyset cursor helpers for Filing API list endpoints."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date
import json
from typing import Any

try:
    from fastapi import HTTPException
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    HTTPException = None  # type: ignore[assignment, misc]


MAX_LIMIT = 1000
DEFAULT_LIMIT = 100


@dataclass(frozen=True)
class DocumentCursor:
    announcement_date: date | None
    document_id: str


def encode_document_cursor(cursor: DocumentCursor) -> str:
    payload = {
        "announcement_date": cursor.announcement_date.isoformat()
        if cursor.announcement_date is not None
        else None,
        "document_id": cursor.document_id,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def decode_document_cursor(value: str | None) -> DocumentCursor | None:
    if value is None:
        return None
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
        payload = json.loads(decoded.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("cursor payload must be an object")
        raw_date = payload.get("announcement_date")
        document_id = payload.get("document_id")
        if raw_date is not None and not isinstance(raw_date, str):
            raise ValueError("announcement_date must be a string or null")
        if not isinstance(document_id, str) or not document_id:
            raise ValueError("document_id must be a non-empty string")
        announcement_date = date.fromisoformat(raw_date) if raw_date is not None else None
    except Exception as exc:
        raise validation_error("cursor", "invalid document cursor") from exc
    return DocumentCursor(announcement_date=announcement_date, document_id=document_id)


def validate_limit(limit: int) -> int:
    if limit < 1:
        raise validation_error("limit", "must be greater than or equal to 1")
    if limit > MAX_LIMIT:
        raise validation_error("limit", "must be less than or equal to 1000")
    return limit


def validation_error(field: str, message: str) -> Exception:
    if HTTPException is None:  # pragma: no cover
        return ValueError(f"{field}: {message}")
    return HTTPException(
        status_code=422,
        detail={"errors": [{"field": field, "message": message}]},
    )


def document_cursor_from_row(row: dict[str, Any]) -> DocumentCursor:
    raw_date = row["announcement_date"]
    if raw_date is not None and not isinstance(raw_date, date):
        raw_date = date.fromisoformat(str(raw_date))
    return DocumentCursor(
        announcement_date=raw_date,
        document_id=str(row["document_id"]),
    )
