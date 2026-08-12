"""Hash helpers for document_unit identity layers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import json
from typing import Any


_MIXED_PART_CONTENT_EXCLUDED_FIELDS = (
    "order",
    "heading_path",
    "local_heading",
    "applicability",
    "quality_status",
    "artifact_locator",
)
_MIXED_PART_QUERY_FIELDS = (
    "heading_path",
    "local_heading",
    "applicability",
    "quality_status",
)


@dataclass(frozen=True)
class UnitHashes:
    content_hash: str
    query_projection_hash: str
    structure_hash: str


def canonical_json(value: dict[str, Any]) -> str:
    """Serialize the service's deterministic JSON hash profile.

    This is intentionally the existing Python JSON profile, not a claim of
    RFC 8785/JCS compatibility.  Non-finite floats are rejected because they
    are not valid JSON and PostgreSQL jsonb cannot persist the same value.
    """

    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_prefixed(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def content_hash_aggregate(content_hashes: Iterable[str]) -> str:
    """Run-level aggregate over unit content hashes (sorted, duplicates kept).

    This hashes the joined hash list, not any snapshot file's bytes; verifiers
    must recompute it from unit rows rather than hashing the snapshot file.
    """

    return sha256_prefixed("\n".join(sorted(content_hashes)))


def structure_hash_aggregate(structure_hashes: Iterable[str]) -> str:
    """Run-level ordered aggregate over canonical unit structure hashes."""

    return sha256_prefixed("\n".join(structure_hashes))


def content_hash(*, payload_kind: str, payload: dict[str, Any]) -> str:
    return sha256_prefixed(
        canonical_json(
            {
                "payload_kind": payload_kind,
                "payload": _content_payload(
                    payload_kind=payload_kind,
                    payload=payload,
                ),
            }
        )
    )


def query_projection_hash(
    *,
    payload_kind: str,
    title: str | None,
    heading_path: list[str],
    semantic_key: str | None,
    quality_status: str,
    applicability: str | None = None,
    semantic_keys: list[str] | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    return sha256_prefixed(
        canonical_json(
            query_projection(
                payload_kind=payload_kind,
                title=title,
                heading_path=heading_path,
                semantic_key=semantic_key,
                quality_status=quality_status,
                applicability=applicability,
                semantic_keys=semantic_keys,
                payload=payload,
            )
        )
    )


def query_projection(
    *,
    payload_kind: str,
    title: str | None,
    heading_path: list[str],
    semantic_key: str | None,
    quality_status: str,
    applicability: str | None = None,
    semantic_keys: list[str] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the one canonical projection used by hashing and publication.

    Keeping this materialized view shared prevents a projection-hash change
    from producing an outbox event whose ``changed_fields`` is empty merely
    because the publisher forgot a newly hashed field.
    """

    projection: dict[str, Any] = {
        "payload_kind": payload_kind,
        "title": title,
        "heading_path": heading_path,
        "semantic_key": semantic_key,
        "semantic_keys": semantic_keys,
        "quality_status": quality_status,
        "applicability": applicability,
    }
    if payload_kind == "mixed":
        if payload is None:
            raise ValueError("mixed query projection requires payload")
        projection["mixed_part_annotations"] = mixed_part_annotations(
            payload_kind=payload_kind,
            payload=payload,
        )
    return projection


def mixed_part_annotations(
    *, payload_kind: str, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Return rules-derived mixed metadata for query projection identity."""

    if payload_kind != "mixed":
        return None
    parts = _mixed_parts(payload)
    return {
        "parts": [
            {field: part[field] for field in _MIXED_PART_QUERY_FIELDS if field in part}
            for part in parts
        ],
    }


def _content_payload(*, payload_kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if payload_kind != "mixed":
        return payload
    parts = _mixed_parts(payload)
    content = {key: value for key, value in payload.items() if key != "semantic_type"}
    content["parts"] = [
        {
            key: value
            for key, value in part.items()
            if key not in _MIXED_PART_CONTENT_EXCLUDED_FIELDS
        }
        for part in parts
    ]
    return content


def _mixed_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    parts = payload.get("parts")
    if (
        not isinstance(parts, list)
        or not parts
        or any(not isinstance(part, dict) for part in parts)
    ):
        raise ValueError("mixed payload parts must be a non-empty list of objects")
    return parts


def structure_hash(
    *,
    payload_kind: str,
    heading_path: list[str],
    order_index: int,
) -> str:
    return sha256_prefixed(
        canonical_json(
            {
                "payload_kind": payload_kind,
                "heading_path": heading_path,
                "order_index": order_index,
            }
        )
    )


def compute_unit_hashes(
    *,
    payload_kind: str,
    payload: dict[str, Any],
    title: str | None,
    heading_path: list[str],
    semantic_key: str | None,
    quality_status: str,
    order_index: int,
    applicability: str | None = None,
    semantic_keys: list[str] | None = None,
) -> UnitHashes:
    return UnitHashes(
        content_hash=content_hash(payload_kind=payload_kind, payload=payload),
        query_projection_hash=query_projection_hash(
            payload_kind=payload_kind,
            title=title,
            heading_path=heading_path,
            semantic_key=semantic_key,
            quality_status=quality_status,
            applicability=applicability,
            semantic_keys=semantic_keys,
            payload=payload,
        ),
        structure_hash=structure_hash(
            payload_kind=payload_kind,
            heading_path=heading_path,
            order_index=order_index,
        ),
    )
