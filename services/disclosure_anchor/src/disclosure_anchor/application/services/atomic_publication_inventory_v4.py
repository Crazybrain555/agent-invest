"""Canonical transaction-P inventory projection shared by build and commit."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json
from typing import Any, cast

from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    PreviousActiveUnitV4,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.services.unit_hashing import query_projection


class PreviousActiveUnitInventoryV4Error(ValueError):
    """A stored active Unit no longer closes over its query projection."""


def previous_active_unit_inventory_v4(
    units: Sequence[e.DocumentUnit],
) -> tuple[PreviousActiveUnitV4, ...]:
    """Project exact active rows into transaction-P's immutable diff basis.

    Ordering is deliberately preserved.  Both the first-request builder and
    transaction P obtain their rows from repositories ordered by run/order/ID;
    the closed request contract remains the sole authority for canonical
    ordering, contiguity, and expected-run identity.
    """

    result: list[PreviousActiveUnitV4] = []
    for unit in units:
        projection = query_projection(
            payload_kind=unit.payload_kind,
            title=unit.title,
            heading_path=list(unit.heading_path),
            semantic_keys=(
                None if unit.semantic_keys is None else list(unit.semantic_keys)
            ),
            section_keys=(
                None if unit.section_keys is None else list(unit.section_keys)
            ),
            quality_status=unit.quality_status,
            applicability=unit.applicability,
            payload=cast(dict[str, Any], unit.payload),
        )
        canonical = _canonical_json_text(projection)
        if unit.query_projection_hash != _digest(canonical.encode("utf-8")):
            raise PreviousActiveUnitInventoryV4Error(
                "stored active Unit projection drifted"
            )
        result.append(
            PreviousActiveUnitV4(
                asset_id=unit.asset_id,
                processing_run_id=unit.processing_run_id,
                order_index=unit.order_index,
                payload_kind=unit.payload_kind,
                heading_path=tuple(unit.heading_path),
                content_hash=unit.content_hash,
                query_projection_hash=cast(str, unit.query_projection_hash),
                canonical_query_projection_json=canonical,
            )
        )
    return tuple(result)


def _canonical_json_text(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


__all__ = [
    "PreviousActiveUnitInventoryV4Error",
    "previous_active_unit_inventory_v4",
]
