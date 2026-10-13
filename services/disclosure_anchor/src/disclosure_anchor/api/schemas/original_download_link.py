"""Additive authenticated agent read contract for external official originals."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class OriginalDownloadLinkOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: Literal["disclosure-download-link.v1"]
    document_id: str
    provider: str | None
    provider_document_id: str | None
    raw_file_hash: str | None
    source_ref: str | None
    url: str | None
    url_source: Literal["recorded_official_index"] | None
    recorded_at: str | None
    unavailable_reason: Literal["no_matching_recorded_url", "unsupported_publisher_url"] | None
    link_live_checked: Literal[False]
    hash_verification_required: bool
    notes: list[str]
