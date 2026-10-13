"""Agent-only inventory v1 DTOs: bounded facts, no private source/run shapes."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


InventoryStatus = Literal[
    "published", "parsing", "post_processing", "downloaded_queued", "downloaded_not_queued",
    "not_downloaded_queued", "not_downloaded_not_queued",
]


class InventoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InventoryScope(InventoryModel):
    kind: Literal["known_local_discovered_corpus"]
    security_code: str | None
    status: InventoryStatus | Literal["failed"] | None
    unknown_upstream_total: None


class InventoryCounts(InventoryModel):
    published: int = Field(ge=0)
    parsing: int = Field(ge=0)
    post_processing: int = Field(ge=0)
    downloaded_queued: int = Field(ge=0)
    downloaded_not_queued: int = Field(ge=0)
    not_downloaded_queued: int = Field(ge=0)
    not_downloaded_not_queued: int = Field(ge=0)


class InventorySummary(InventoryModel):
    known_total: int = Field(ge=0)
    matching_total: int = Field(ge=0)
    by_status: InventoryCounts
    active_failures: int = Field(ge=0)


class InventoryOriginal(InventoryModel):
    state: Literal["archived_recorded", "not_archived"]
    raw_file_hash: str | None
    text_path: str | None


class InventoryFailure(InventoryModel):
    stage: Literal["download", "parse", "build", "publish"]
    error_code: str | None
    retryable: bool | None
    occurred_at: str
    processing_run_id: str | None


class InventoryProcessing(InventoryModel):
    state: Literal["published", "parse_running", "post_processing", "parse_failed", "not_started", "waiting_for_original"]
    document_status: str | None
    current_processing_run_id: str | None
    running_processing_run_id: str | None
    running_started_at: str | None
    active_failure: bool
    last_failure: InventoryFailure | None
    units_path: str | None


class InventoryScheduling(InventoryModel):
    download_selected: bool
    parse_selected: bool
    post_processing_selected: bool
    blocked_reasons: list[str]


class InventoryItem(InventoryModel):
    inventory_id: str
    document_id: str | None
    provider: str | None
    provider_document_id: str | None
    security_code: str | None
    exchange: str | None
    title: str | None
    announcement_date: str | None
    status: InventoryStatus
    original: InventoryOriginal
    processing: InventoryProcessing
    scheduling: InventoryScheduling


class DisclosureInventoryOut(InventoryModel):
    contract_version: Literal["disclosure-inventory.v1"]
    observed_at: str
    scope: InventoryScope
    summary: InventorySummary
    items: list[InventoryItem] = Field(max_length=200)
    next_cursor: str | None
    notes: list[str]
