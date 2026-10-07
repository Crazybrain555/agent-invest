"""Read-only inventory query boundaries and safe public facts."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import json
import re
from typing import Any


CONTRACT_VERSION = "disclosure-inventory.v1"
INVENTORY_STATUSES = (
    "published", "parsing", "post_processing", "downloaded_queued",
    "downloaded_not_queued", "not_downloaded_queued", "not_downloaded_not_queued",
)
NOTES = (
    "Only the known local discovered corpus is counted; undiscovered upstream filings are unknown.",
    "Latest canonical source filing identities are counted once; older registered versions remain retained.",
    "Queue selection means current eligibility, not proof of a running resident worker.",
    "A recorded running run is not a liveness proof; its start time remains visible.",
    "Archived originals are DB records; the text endpoint verifies actual source availability and integrity.",
    "Pagination reads fresh snapshots; corpus and status may change between calls.",
)


class InventoryQueryInvalid(ValueError):
    def __init__(self, field_name: str, message: str) -> None:
        self.field_name = field_name
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class InventoryQuery:
    status: str | None = None
    security_code: str | None = None
    limit: int = 50
    after_inventory_id: str | None = None


def normalize_query(
    *, status: str | None = None, security_code: str | None = None,
    limit: int = 50, cursor: str | None = None,
) -> InventoryQuery:
    if status is not None and status not in (*INVENTORY_STATUSES, "failed"):
        raise InventoryQueryInvalid("status", "unsupported inventory status")
    if security_code is not None and re.fullmatch(r"[0-9]{6}", security_code) is None:
        raise InventoryQueryInvalid("security_code", "must be exactly 6 ASCII digits")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise InventoryQueryInvalid("limit", "must be an integer from 1 to 200")
    after: str | None = None
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or len(cursor) > 2048:
                raise ValueError
            value = json.loads(base64.b64decode(
                cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True,
            ))
            if (not isinstance(value, dict) or set(value) != {"v", "status", "security_code", "after"}
                    or value["v"] != CONTRACT_VERSION or value["status"] != status
                    or value["security_code"] != security_code
                    or not isinstance(value["after"], str) or not value["after"]
                    or len(value["after"]) > 512 or "\x00" in value["after"]):
                raise ValueError
            after = value["after"]
        except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
            raise InventoryQueryInvalid("cursor", "invalid cursor or changed filters") from exc
    return InventoryQuery(status, security_code, limit, after)


def continuation_cursor(query: InventoryQuery, inventory_id: str) -> str:
    value = {"v": CONTRACT_VERSION, "status": query.status,
             "security_code": query.security_code, "after": inventory_id}
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


@dataclass(frozen=True)
class InventorySnapshot:
    observed_at: str
    known_total: int
    matching_total: int
    by_status: dict[str, int]
    active_failures: int
    items: tuple[dict[str, Any], ...]
    has_more: bool
