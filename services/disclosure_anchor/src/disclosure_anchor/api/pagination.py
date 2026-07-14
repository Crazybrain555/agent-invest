"""Opaque keyset cursor helpers for Filing API list endpoints."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date
import json
from typing import Any

from disclosure_anchor.api.errors import validation_error as api_validation_error


MAX_LIMIT = 1000
DEFAULT_LIMIT = 100


@dataclass(frozen=True)
class DocumentCursor:
    announcement_date: date | None
    document_id: str


@dataclass(frozen=True)
class UnitCursor:
    order_index: int
    asset_id: str


@dataclass(frozen=True)
class ChangeCursor:
    seq: int


@dataclass(frozen=True)
class TrackedCompanyCursor:
    # Keyset over the tracked_companies_v1 ordering (security_code NULLS LAST,
    # tracked_company_id ASC). security_code is nullable, so the null tail is
    # paginated on tracked_company_id alone.
    security_code: str | None
    tracked_company_id: str


def encode_document_cursor(cursor: DocumentCursor) -> str:
    payload = {
        "announcement_date": cursor.announcement_date.isoformat()
        if cursor.announcement_date is not None
        else None,
        "document_id": cursor.document_id,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def encode_unit_cursor(cursor: UnitCursor) -> str:
    payload = {"order_index": cursor.order_index, "asset_id": cursor.asset_id}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def encode_change_cursor(cursor: ChangeCursor) -> str:
    payload = {"seq": cursor.seq}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def encode_tracked_company_cursor(cursor: TrackedCompanyCursor) -> str:
    payload = {
        "security_code": cursor.security_code,
        "tracked_company_id": cursor.tracked_company_id,
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


def decode_unit_cursor(value: str | None) -> UnitCursor | None:
    if value is None:
        return None
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
        payload = json.loads(decoded.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("cursor payload must be an object")
        order_index = payload.get("order_index")
        asset_id = payload.get("asset_id")
        if not isinstance(order_index, int):
            raise ValueError("order_index must be an integer")
        if not isinstance(asset_id, str) or not asset_id:
            raise ValueError("asset_id must be a non-empty string")
    except Exception as exc:
        raise validation_error("cursor", "invalid unit cursor") from exc
    return UnitCursor(order_index=order_index, asset_id=asset_id)


def decode_change_cursor(value: str | None) -> ChangeCursor | None:
    if value is None:
        return None
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
        payload = json.loads(decoded.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("cursor payload must be an object")
        seq = payload.get("seq")
        if not isinstance(seq, int):
            raise ValueError("seq must be an integer")
    except Exception as exc:
        raise validation_error("cursor", "invalid change cursor") from exc
    return ChangeCursor(seq=seq)


def decode_tracked_company_cursor(value: str | None) -> TrackedCompanyCursor | None:
    if value is None:
        return None
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
        payload = json.loads(decoded.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("cursor payload must be an object")
        security_code = payload.get("security_code")
        tracked_company_id = payload.get("tracked_company_id")
        if security_code is not None and not isinstance(security_code, str):
            raise ValueError("security_code must be a string or null")
        if not isinstance(tracked_company_id, str) or not tracked_company_id:
            raise ValueError("tracked_company_id must be a non-empty string")
    except Exception as exc:
        raise validation_error("cursor", "invalid tracked company cursor") from exc
    return TrackedCompanyCursor(
        security_code=security_code, tracked_company_id=tracked_company_id
    )


def validate_limit(limit: int) -> int:
    if limit < 1:
        raise validation_error("limit", "must be greater than or equal to 1")
    if limit > MAX_LIMIT:
        raise validation_error("limit", "must be less than or equal to 1000")
    return limit


def validation_error(field: str, message: str) -> Exception:
    return api_validation_error(field, message)


def document_cursor_from_row(row: dict[str, Any]) -> DocumentCursor:
    raw_date = row["announcement_date"]
    if raw_date is not None and not isinstance(raw_date, date):
        raw_date = date.fromisoformat(str(raw_date))
    return DocumentCursor(
        announcement_date=raw_date,
        document_id=str(row["document_id"]),
    )


def unit_cursor_from_row(row: dict[str, Any]) -> UnitCursor:
    return UnitCursor(order_index=int(row["order_index"]), asset_id=str(row["asset_id"]))


def change_cursor_from_row(row: dict[str, Any]) -> ChangeCursor:
    return ChangeCursor(seq=int(row["seq"]))


def tracked_company_cursor_from_row(row: dict[str, Any]) -> TrackedCompanyCursor:
    raw_code = row["security_code"]
    return TrackedCompanyCursor(
        security_code=str(raw_code) if raw_code is not None else None,
        tracked_company_id=str(row["tracked_company_id"]),
    )
