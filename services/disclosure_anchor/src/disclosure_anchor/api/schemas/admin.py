"""Pydantic request/response models for local admin endpoints."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal, Any

from pydantic import BaseModel, ConfigDict, Field


class AdminModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterLocalPdfRequest(AdminModel):
    file_path: Path
    company_legal_name: str
    security_code: str
    exchange: str
    filing_type: str
    title: str
    announcement_date: date
    provider_document_id: str
    provider: str
    report_period: str | None = None
    board: str | None = None
    company_credit_code: str | None = None
    expected_raw_file_hash: str | None = None


class RegisterLocalPdfResponse(AdminModel):
    document_id: str | None
    raw_file_relpath: str | None
    raw_file_hash: str | None
    source_access_id: str | None
    outbox_event_id: str | None
    reused_existing_document: bool = False
    quarantined_path: str | None = None
    quarantine_reason: str | None = None


class ParserOptionsRequest(AdminModel):
    # Closed vocabularies: these values become MinerU argv; free strings
    # starting with '-' would inject CLI flags into the subprocess.
    # Backend set = MinerU 3.4; *-http-client offloads to a remote
    # mineru-openai-server and requires server_url.
    method: Literal["auto", "txt", "ocr"] | None = None
    backend: (
        Literal[
            "pipeline",
            "vlm-engine",
            "hybrid-engine",
            "vlm-http-client",
            "hybrid-http-client",
        ]
        | None
    ) = None
    language: Literal["ch", "en"] | None = None
    formula: bool | None = None
    table: bool | None = None
    start_page: int | None = None
    end_page: int | None = None
    timeout_seconds: int | None = None
    server_url: str | None = Field(default=None, pattern=r"^https?://")


class ParseDocumentResponse(AdminModel):
    processing_run_id: str
    status: str
    parser_artifact_relpath: str | None = None
    normalized_ir_relpath: str | None = None
    artifact_hash: str | None = None
    error: dict[str, Any] | None = None


class BuildUnitsResponse(AdminModel):
    processing_run_id: str
    unit_build_status: str
    unit_count: int


class TrackEntryRequest(AdminModel):
    # Mirrors use_cases.track_companies.TrackEntry 1:1 — full-row upsert:
    # an absent optional field CLEARS the stored override back to inherit.
    security_code: str
    exchange: str
    status: Literal["active", "paused"] = "active"
    lookback_days: int | None = None
    sync_frequency: Literal["hourly", "daily", "weekly"] | None = None
    process_classes: list[str] | None = None


class TrackCompaniesRequest(AdminModel):
    # min_length=1: an empty batch combined with reconcile+prune_drift would
    # pause the entire pool in one request (every tracked row becomes drift).
    # Reject it at request validation so it never reaches the use case.
    entries: list[TrackEntryRequest] = Field(min_length=1)
    reconcile: bool = False
    prune_drift: bool = False
    dry_run: bool = False


class TrackEntryResultResponse(AdminModel):
    security_code: str
    exchange: str
    tracked_company_id: str
    company_id: str
    created: bool
    # Full-row upsert visibility (round23): action is created | updated |
    # unchanged; cleared_overrides echoes exactly which previously-set
    # overrides this request cleared back to inherit-global (the full-row
    # footgun — an omitted optional field clears, it does not keep the old
    # value); status_change reports any active<->paused flip.
    action: str = "updated"
    cleared_overrides: list[str] = Field(default_factory=list)
    status_change: str | None = None


class TrackDriftResponse(AdminModel):
    tracked_company_id: str
    company_id: str
    security_code: str | None
    status: str
    action: str


class TrackCompaniesResponse(AdminModel):
    results: list[TrackEntryResultResponse]
    drift: list[TrackDriftResponse]
    dry_run: bool
    created_count: int


class SyncCompanyRequest(AdminModel):
    # None = checkpoint-based incremental window (first sync falls back to
    # the lookback cascade); an explicit value overrides the window in days.
    window_days: int | None = None
    # Absolute backfill range (round23): mutually exclusive with window_days.
    # Both ends required together; end must not be in the future. The
    # validation lives in compute_sync_window — the router surfaces its
    # ValueError as VALIDATION_ERROR.
    window_start: date | None = None
    window_end: date | None = None


class SyncCompanyResponse(AdminModel):
    # 'failed' keeps HTTP 200 with the durable failure trace (the parse
    # endpoint precedent): the source_access row already records the error.
    sync_status: Literal["ok", "failed"]
    security_code: str
    exchange: str
    window_start: date
    window_end: date
    company_id: str | None = None
    candidate_count: int | None = None
    empty: bool | None = None
    checkpoint_id: str | None = None
    error: str | None = None


class UntrackCompanyResponse(AdminModel):
    # Pool-row removal only: the company/security ledger rows and acquired
    # documents stay (evidence); acquisition stops via the active-row queue
    # predicate. Reversible stop = PUT with status=paused.
    security_code: str
    exchange: str
    tracked_company_id: str
    company_id: str
    documents_retained: int


class PublishRunRequest(AdminModel):
    allow_empty: bool = False
    reason: str | None = None


class PublishRunResponse(AdminModel):
    document_id: str
    processing_run_id: str
    is_active: bool
