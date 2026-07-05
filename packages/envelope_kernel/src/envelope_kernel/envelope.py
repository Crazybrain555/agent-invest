"""data_asset envelope field model (protocol §3.2).

The six groups (identity / scope / time / provenance / payload / state) are narrative structure only —
the contract is the field definitions and required-ness (§2.1). Fields are flattened; every non-core
field is a registered extension, so the model forbids unknown fields.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from envelope_kernel.kinds import (
    AssetKind,
    PayloadKind,
    QualityStatus,
    SourceTier,
    TraceLevel,
    validate_combination,
)

CONTRACT_VERSION = "data_asset.v1"


class DataAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # A. identity
    asset_id: str
    asset_kind: AssetKind
    payload_kind: PayloadKind | None = None
    contract_version: str = CONTRACT_VERSION
    content_hash: str | None = None

    # B. scope (检索键)
    subject_candidates: list[str] | None = None
    title: str | None = None
    heading_path: list[str] | None = None
    semantic_key: str | None = None
    parent_ref: str | None = None
    order_index: int | None = None
    material_type: str | None = None

    # C. time (§2.5 三轴)
    event_time: datetime | date | None = None
    published_at: datetime | None = None
    report_period: str | None = None
    observed_at: datetime

    # D. provenance
    source_ref: str
    provider: str | None = None
    adapter: str | None = None
    tool: str | None = None
    query_params: dict[str, Any] | None = None
    source_tier: SourceTier
    trace_level: TraceLevel
    locator: str | None = None
    raw_asset_ref: str | None = None
    producer_action_ref: str | None = None
    sensitivity: str | None = None

    # E. payload
    payload: dict[str, Any] | None = None

    # F. state
    quality_status: QualityStatus | None = None
    is_active: bool | None = None
    change_seq: int | None = None
    superseded_by: str | None = None

    @model_validator(mode="after")
    def _check_minimal_core(self) -> "DataAsset":
        # §3.2 必选最小核: payload 或 raw_asset_ref 至少其一。
        if self.payload is None and self.raw_asset_ref is None:
            raise ValueError("data_asset requires at least one of payload or raw_asset_ref")
        if self.payload_kind is not None:
            validate_combination(self.asset_kind, self.payload_kind)
        return self
