"""Shared L1 data_asset envelope kernel (engine protocol §2.2 / §2.3 / §2.9 / §3.2)."""

from envelope_kernel.contracts import data_asset_json_schema, validate_envelope
from envelope_kernel.envelope import CONTRACT_VERSION, DataAsset
from envelope_kernel.kinds import (
    VALID_PAYLOAD_KINDS,
    AssetKind,
    PayloadKind,
    QualityStatus,
    SourceTier,
    TraceLevel,
    is_valid_combination,
    validate_combination,
)
from envelope_kernel.uri import AssetUri, build_asset_uri, parse_asset_uri

__all__ = [
    "CONTRACT_VERSION",
    "VALID_PAYLOAD_KINDS",
    "AssetKind",
    "AssetUri",
    "DataAsset",
    "PayloadKind",
    "QualityStatus",
    "SourceTier",
    "TraceLevel",
    "build_asset_uri",
    "data_asset_json_schema",
    "is_valid_combination",
    "parse_asset_uri",
    "validate_combination",
    "validate_envelope",
]
