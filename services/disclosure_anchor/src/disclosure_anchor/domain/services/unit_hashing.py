"""Hash helpers for document_unit identity layers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import json
from typing import Any


@dataclass(frozen=True)
class UnitHashes:
    content_hash: str
    query_projection_hash: str
    structure_hash: str


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def sha256_prefixed(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def content_hash_aggregate(content_hashes: Iterable[str]) -> str:
    """Run-level aggregate over unit content hashes (sorted, duplicates kept).

    This hashes the joined hash list, not any snapshot file's bytes; verifiers
    must recompute it from unit rows rather than hashing the snapshot file.
    """

    return sha256_prefixed("\n".join(sorted(content_hashes)))


def content_hash(*, payload_kind: str, payload: dict[str, Any]) -> str:
    return sha256_prefixed(
        canonical_json({"payload_kind": payload_kind, "payload": payload})
    )


def query_projection_hash(
    *,
    payload_kind: str,
    title: str | None,
    heading_path: list[str],
    semantic_key: str | None,
    quality_status: str,
    applicability: str | None = None,
) -> str:
    return sha256_prefixed(
        canonical_json(
            {
                "payload_kind": payload_kind,
                "title": title,
                "heading_path": heading_path,
                "semantic_key": semantic_key,
                "quality_status": quality_status,
                "applicability": applicability,
            }
        )
    )


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
        ),
        structure_hash=structure_hash(
            payload_kind=payload_kind,
            heading_path=heading_path,
            order_index=order_index,
        ),
    )
