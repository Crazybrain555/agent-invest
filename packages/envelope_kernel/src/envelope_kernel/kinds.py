"""Discriminator enums and the legal asset_kind × payload_kind matrix (protocol §2.2, §2.9)."""

from __future__ import annotations

from enum import StrEnum
from typing import Mapping


class AssetKind(StrEnum):
    DOCUMENT_UNIT = "document_unit"
    DATASET_SNAPSHOT = "dataset_snapshot"
    TOOL_RESULT = "tool_result"
    ARTIFACT_UNIT = "artifact_unit"


class PayloadKind(StrEnum):
    # document_unit
    TEXT = "text"
    TABLE = "table"
    QA = "qa"
    # dataset_snapshot
    RECORDSET = "recordset"
    # tool_result
    SEARCH_RESULT = "search_result"
    API_RESPONSE = "api_response"
    PAGE_SNIPPET = "page_snippet"
    # artifact_unit
    CALCULATION_TABLE = "calculation_table"
    MODEL_TABLE = "model_table"
    CHECKLIST = "checklist"
    NOTE = "note"


class SourceTier(StrEnum):
    """来源分级（§2.9）；string values follow the disclosure_anchor public contract (tier_0a)."""

    TIER_0A = "tier_0a"
    TIER_0B = "tier_0b"
    TIER_1 = "tier_1"
    TIER_2 = "tier_2"
    TIER_3 = "tier_3"
    TIER_F = "tier_f"


class TraceLevel(StrEnum):
    """追溯等级（§2.9）。"""

    G0 = "G0"
    G1 = "G1"
    G2 = "G2"
    G3 = "G3"
    G4 = "G4"


class QualityStatus(StrEnum):
    """解析 / 登记质量（§3.2 state 组）。"""

    OK = "ok"
    NEEDS_REVIEW = "needs_review"
    UNUSABLE = "unusable"
    EMPTY = "empty"


VALID_PAYLOAD_KINDS: Mapping[AssetKind, frozenset[PayloadKind]] = {
    AssetKind.DOCUMENT_UNIT: frozenset({PayloadKind.TEXT, PayloadKind.TABLE, PayloadKind.QA}),
    AssetKind.DATASET_SNAPSHOT: frozenset({PayloadKind.RECORDSET}),
    AssetKind.TOOL_RESULT: frozenset(
        {PayloadKind.SEARCH_RESULT, PayloadKind.API_RESPONSE, PayloadKind.PAGE_SNIPPET}
    ),
    AssetKind.ARTIFACT_UNIT: frozenset(
        {
            PayloadKind.CALCULATION_TABLE,
            PayloadKind.MODEL_TABLE,
            PayloadKind.CHECKLIST,
            PayloadKind.NOTE,
        }
    ),
}


def is_valid_combination(asset_kind: AssetKind, payload_kind: PayloadKind) -> bool:
    return payload_kind in VALID_PAYLOAD_KINDS[asset_kind]


def validate_combination(asset_kind: AssetKind, payload_kind: PayloadKind) -> None:
    if not is_valid_combination(asset_kind, payload_kind):
        allowed = ", ".join(sorted(VALID_PAYLOAD_KINDS[asset_kind]))
        raise ValueError(
            f"payload_kind '{payload_kind}' is not legal for asset_kind '{asset_kind}'"
            f" (allowed: {allowed})"
        )
