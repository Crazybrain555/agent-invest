"""Pydantic models for public Filing API DTOs."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentV1(PublicModel):
    document_id: str
    provider: str | None
    provider_document_id: str | None
    security_code: str | None
    exchange: str | None
    filing_type: str | None
    title: str | None
    announcement_date: date | None
    report_period: str | None
    raw_file_hash: str | None
    status: str
    current_processing_run_id: str | None
    created_at: datetime
    updated_at: datetime
    contract_version: str
    company_ref: str | None
    security_ref: str | None
    source_ref: str | None
    supersedes_document_id: str | None
    correction_of_document_id: str | None
    superseded_by_document_id: str | None
    provider_metadata: dict[str, Any]


class ProcessingRunV1(PublicModel):
    processing_run_id: str
    document_id: str
    run_kind: str
    status: str
    parser_name: str | None
    parser_version: str | None
    artifact_hash: str | None
    content_hash_aggregate: str | None
    structure_hash: str | None
    is_active: bool
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    parser_backend: str | None
    input_raw_file_hash: str | None
    parser_method: str | None
    parser_language: str | None
    unit_build_status: str
    unit_build_attempt_count: int
    unit_built_at: datetime | None
    builder_rules_version: str | None


class DocumentListResponse(PublicModel):
    items: list[DocumentV1]
    next_cursor: str | None
