"""Expose one scoped inventory snapshot; never submit business work."""

from __future__ import annotations

from typing import Any

from disclosure_anchor.application.contracts.disclosure_inventory import (
    CONTRACT_VERSION, NOTES, continuation_cursor, normalize_query,
)
from disclosure_anchor.application.ports.disclosure_inventory import DisclosureInventoryReader


class ReadDisclosureInventory:
    def __init__(self, reader: DisclosureInventoryReader) -> None:
        self._reader = reader

    def read(
        self, *, status: str | None = None, security_code: str | None = None,
        limit: int = 50, cursor: str | None = None,
    ) -> dict[str, Any]:
        query = normalize_query(status=status, security_code=security_code, limit=limit, cursor=cursor)
        snapshot = self._reader.read(query)
        next_cursor = (
            continuation_cursor(query, snapshot.items[-1]["inventory_id"])
            if snapshot.has_more and snapshot.items else None
        )
        return {
            "contract_version": CONTRACT_VERSION, "observed_at": snapshot.observed_at,
            "scope": {"kind": "known_local_discovered_corpus", "security_code": security_code,
                      "status": status, "unknown_upstream_total": None},
            "summary": {"known_total": snapshot.known_total, "matching_total": snapshot.matching_total,
                        "by_status": snapshot.by_status, "active_failures": snapshot.active_failures},
            "items": list(snapshot.items), "next_cursor": next_cursor, "notes": list(NOTES),
        }
