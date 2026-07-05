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


class DocumentUnitV1(PublicModel):
    asset_id: str
    document_id: str
    processing_run_id: str
    provider_document_id: str | None
    payload_kind: str
    heading_path: list[str]
    title: str | None
    order_index: int
    semantic_key: str | None
    payload: dict[str, Any]
    content_hash: str
    structure_hash: str | None
    quality_status: str
    artifact_locator: dict[str, Any] | None
    created_at: datetime
    contract_version: str
    company_ref: str | None
    security_ref: str | None
    security_code: str | None
    exchange: str | None
    filing_type: str | None
    report_period: str | None
    announcement_date: date | None
    producer_action_ref: str
    source_ref: str | None
    parent_ref: str
    asset_kind: str
    observed_at: datetime
    source_tier: str
    trace_level: str
    raw_file_hash: str | None
    query_projection_hash: str | None
    asset_uri: str
    is_active_run: bool


class SourceRefV1(PublicModel):
    service: str
    contract_version: str
    asset_id: str
    source_access_id: str | None
    document_id: str
    provider: str | None
    provider_document_id: str | None
    raw_file_hash: str | None
    processing_run_id: str
    payload_kind: str
    heading_path: list[str]
    title: str | None
    unit_content_hash: str
    quality_status: str
    artifact_locator: dict[str, Any] | None


class DocumentListResponse(PublicModel):
    items: list[DocumentV1]
    next_cursor: str | None


class UnitListResponse(PublicModel):
    items: list[DocumentUnitV1]
    next_cursor: str | None
    warning: str | None = None


class UnitContextResponse(PublicModel):
    asset_id: str
    asset_uri: str
    is_active_run: bool
    document: DocumentV1
    heading_path: list[str]
    title: str | None
    payload: dict[str, Any]
    excerpt: str | None = None
    start: int | None = None
    end: int | None = None
    excerpt_hash: str | None = None
