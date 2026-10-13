"""Deterministic whole-document publication families for envelope tests and measurements.

``publication_family_request`` seals a legal V4 request through the product sealers over the
materialized evidence of ``tests.unit.test_atomic_document_publication_v4._request``: ``units`` text
Units of about ``unit_text_bytes`` UTF-8 bytes each, optionally headed, optionally over a previous
active inventory whose content differs from every new Unit, so transaction P removes and creates every
Unit (the widest outbox).  The boundary families are one huge Unit, many small Units and a large
previous inventory; ``text_kind="escape"`` is the densest JSON escaping (each character doubles every
time the request is embedded as JSON text).  ``family_unit_ids`` gives fixed-width deterministic asset
IDs.  Every value is synthetic filler; only the contracts are real.
"""

from __future__ import annotations

from dataclasses import fields, replace
import hashlib
import json
from typing import Any

from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    AtomicPublicationRequestV4,
    PreviousActiveUnitV4,
    previous_active_units_sha256_v4,
    seal_atomic_publication_request_v4,
    seal_pre_id_unit_publication_v4,
)
from disclosure_anchor.application.contracts.provider_unit import (
    ProviderUnitHeadingRef,
    ProviderUnitLocator,
    provider_unit_locator_to_payload,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    semantic_adjudication_terminal_v1,
    semantic_route_receipts_file_bytes_v3,
)
from disclosure_anchor.domain.services.unit_hashing import (
    compute_unit_hashes,
    content_hash_aggregate,
    query_projection,
    structure_hash_aggregate,
)
from tests.unit.test_atomic_document_publication_v4 import _request

_CJK = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"
_ESCAPE = '"\\\n'


def canonical_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def filler(byte_count: int, kind: str = "cjk", salt: int = 0) -> str:
    """Deterministic synthetic text of at most ``byte_count`` UTF-8 bytes (never empty)."""

    if kind == "escape":
        return (_ESCAPE * (byte_count // len(_ESCAPE) + 1))[: max(byte_count, 1)]
    if kind == "ascii":
        return "x" * max(byte_count, 1)
    if kind != "cjk":
        raise ValueError(f"unknown filler kind {kind!r}")
    shift = salt % len(_CJK)
    block = _CJK[shift:] + _CJK[:shift] + "0123456789。"
    encoded = (block * (byte_count // len(block.encode("utf-8")) + 1)).encode("utf-8")
    return encoded[:byte_count].decode("utf-8", "ignore") or _CJK[0]


def heading_path(index: int, depth: int, chars: int) -> tuple[str, ...]:
    return tuple(
        filler(chars * 3, "cjk", index * 7 + level) + f"{level}.{index}"
        for level in range(depth)
    )


def family_unit_ids(count: int, *, offset: int = 0) -> tuple[str, ...]:
    """Fixed-width, deterministic, canonical Unit asset IDs."""

    return tuple(f"du_{offset + index:026d}" for index in range(1, count + 1))


def publication_family_request(
    *,
    units: int,
    unit_text_bytes: int,
    text_kind: str = "cjk",
    previous_units: int = 0,
    heading_depth: int = 0,
    heading_chars: int = 12,
) -> AtomicPublicationRequestV4:
    """Seal one legal synthetic request; see the module docstring."""

    base = _request()
    proto = base.units[0]
    upstream = base.upstream_evidence
    base_route = base.semantic_route_receipts[0]
    built = []
    routes = []
    for index in range(1, units + 1):
        path = heading_path(index, heading_depth, heading_chars)
        title = path[-1] if path else None
        payload = {"text": filler(unit_text_bytes, text_kind, index)}
        locator_json = canonical_text(
            provider_unit_locator_to_payload(
                ProviderUnitLocator(
                    provider_document_sha256=upstream.provider_document_sha256,
                    unit_index=index - 1,
                    heading_chain=tuple(
                        ProviderUnitHeadingRef(
                            heading_id=f"h{index}-{level}",
                            source_index=level,
                            payload_ordinal=0,
                            placement_source="bookmark",
                        )
                        for level in range(len(path))
                    ),
                    parts=(),
                    evidence_only_block_source_indices=(),
                    unbound_table_parts=(),
                    evidence_artifacts=(),
                    search_targets=(),
                )
            )
        )
        hashes = compute_unit_hashes(
            payload_kind="text",
            payload=payload,
            title=title,
            heading_path=list(path),
            semantic_keys=None,
            section_keys=["section"],
            quality_status="ok",
            applicability="applicable",
            order_index=index,
        )
        unit = seal_pre_id_unit_publication_v4(
            **{
                **_unsealed(proto, "routed_draft_sha256"),
                "unit_index": index,
                "heading_path": path,
                "title": title,
                "canonical_payload_json": canonical_text(payload),
                "content_hash": hashes.content_hash,
                "structure_hash": hashes.structure_hash,
                "query_projection_hash": hashes.query_projection_hash,
                "canonical_artifact_locator_json": locator_json,
                "provider_locator_sha256": digest(locator_json.encode("utf-8")),
            }
        )
        built.append(unit)
        routes.append(
            replace(
                base_route,
                unit_order_index=index,
                provider_locator_sha256=unit.provider_locator_sha256,
                routed_draft_sha256=unit.routed_draft_sha256,
            )
        )
    previous = tuple(
        _previous_unit(asset_id, order, heading_depth, heading_chars)
        for order, asset_id in enumerate(family_unit_ids(previous_units, offset=10**12), start=1)
    )
    terminal = semantic_adjudication_terminal_v1(tuple(item.receipt for item in routes))
    projection = json.loads(base.processing_run_projection_json)
    projection.update(
        unit_count=units,
        content_hash_aggregate=content_hash_aggregate([item.content_hash for item in built]),
        structure_hash_aggregate=structure_hash_aggregate([item.structure_hash for item in built]),
        semantic_route_receipts_sha256=digest(semantic_route_receipts_file_bytes_v3(tuple(routes))),
        semantic_adjudication_status=terminal.status,
        semantic_adjudication_summary=terminal.summary,
        semantic_degraded_unit_count=terminal.degraded_unit_count,
        semantic_failover_group_count=terminal.failover_group_count,
    )
    projection_json = canonical_text(projection)
    return seal_atomic_publication_request_v4(
        **{
            **_unsealed(base, "request_sha256"),
            "identity": replace(
                base.identity,
                expected_previous_processing_run_id="run-old" if previous else None,
            ),
            "units": tuple(built),
            "semantic_route_receipts": tuple(routes),
            "processing_run_projection_json": projection_json,
            "processing_run_projection_sha256": digest(projection_json.encode("utf-8")),
            "previous_active_units": previous,
            "previous_active_units_sha256": previous_active_units_sha256_v4(previous),
        }
    )


def _unsealed(value: Any, derived: str) -> dict[str, Any]:
    return {item.name: getattr(value, item.name) for item in fields(value) if item.name != derived}


def _previous_unit(asset_id: str, order_index: int, depth: int, chars: int) -> PreviousActiveUnitV4:
    path = heading_path(order_index + 10**6, depth, chars)
    projection_json = canonical_text(
        query_projection(
            payload_kind="text",
            title=path[-1] if path else None,
            heading_path=list(path),
            semantic_keys=None,
            section_keys=["section"],
            quality_status="ok",
            applicability="applicable",
            payload={"text": f"previous-{order_index}"},
        )
    )
    return PreviousActiveUnitV4(
        asset_id=asset_id,
        processing_run_id="run-old",
        order_index=order_index,
        payload_kind="text",
        heading_path=path,
        content_hash=digest(f"previous-content-{order_index}".encode("utf-8")),
        query_projection_hash=digest(projection_json.encode("utf-8")),
        canonical_query_projection_json=projection_json,
    )


__all__ = [
    "canonical_text",
    "digest",
    "family_unit_ids",
    "filler",
    "heading_path",
    "publication_family_request",
]
