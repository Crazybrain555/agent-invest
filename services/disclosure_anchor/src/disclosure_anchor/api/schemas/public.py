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
    disclosure_topics: list[str] | None
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
    publisher_categories: list[dict[str, Any]] | None
    market: str | None
    content_categories: list[dict[str, Any]] | None


class ProcessingRunV1(PublicModel):
    processing_run_id: str
    document_id: str
    artifact_owner_processing_run_id: str
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


class EvidenceRefV1(PublicModel):
    uri: str
    sha256: str
    size_bytes: int
    media_type: str


class DocumentUnitV1(PublicModel):
    asset_id: str
    document_id: str
    processing_run_id: str
    provider_document_id: str | None
    payload_kind: str
    heading_path: list[str]
    heading_path_text: str | None
    title: str | None
    order_index: int
    semantic_key: str | None
    semantic_keys: list[str] | None
    payload: dict[str, Any]
    content_hash: str
    structure_hash: str | None
    quality_status: str
    applicability: str | None
    page_no: int | None
    artifact_locator: dict[str, Any] | None
    created_at: datetime
    contract_version: str
    company_ref: str | None
    security_ref: str | None
    security_code: str | None
    exchange: str | None
    filing_type: str | None
    disclosure_topics: list[str] | None
    content_categories: list[dict[str, Any]] | None
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
    evidence_refs: list[EvidenceRefV1]


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
    is_active_run: bool
    payload_kind: str
    heading_path: list[str]
    title: str | None
    unit_content_hash: str
    quality_status: str
    applicability: str | None
    page_no: int | None
    artifact_locator: dict[str, Any] | None
    evidence_refs: list[EvidenceRefV1]


class DocumentCategoryV1(PublicModel):
    document_id: str
    provider: str | None
    provider_document_id: str | None
    category_code: str
    ordinal: int
    category_name: str | None
    parent_category_code: str | None
    contract_version: str


class ChangeEventV1(PublicModel):
    seq: int
    event_id: str
    event_kind: str
    document_id: str | None
    processing_run_id: str | None
    asset_id: str | None
    payload: dict[str, Any] | None
    occurred_at: datetime
    change_kind: str
    subject_kind: str
    subject_ref: str
    source: str
    contract_version: str
    created_at: datetime


class TrackedCompanyV1(PublicModel):
    tracked_company_id: str
    company_ref: str
    security_ref: str | None
    security_code: str | None
    exchange: str | None
    legal_name: str
    # Lifecycle facts (0020): 'pending' until the intake placeholder name is
    # upgraded by an on-add profile fetch or the first sync.
    legal_name_status: str
    status: str
    lookback_days: int | None
    sync_frequency: str | None
    process_classes: list[str] | None
    last_synced_at: datetime | None
    synced_through: date | None
    created_at: datetime
    updated_at: datetime
    contract_version: str
    # API-derived cascade resolution (not view columns): NULL overrides fall
    # back to the global defaults from settings + processing_policy.json.
    effective_lookback_days: int
    effective_sync_seconds: int
    effective_process_classes: list[str]
    # API-derived acquisition state: never_synced | due | fresh (the due
    # judgement needs the effective interval, which includes env defaults).
    sync_state: str


class TrackedCompanyListResponse(PublicModel):
    items: list[TrackedCompanyV1]
    next_cursor: str | None = None


class ProcessingClassV1(PublicModel):
    # One entry of the unified class vocabulary (class_map.json). disposition
    # is the processing_policy.json layer-2 default: process (download+parse) |
    # register_only (metadata only) | unknown_disposition (policy unreachable
    # or a class the policy does not list).
    name: str
    zh: str | None
    priority: int
    disposition: str


class ClassificationRuleSetV1(PublicModel):
    # DB-loaded classification_rule versions per rule_set (doctor口径:
    # string_agg(DISTINCT version)). rule_count is the row count in that set.
    rule_set: str
    version: str
    rule_count: int


class ClassificationResponse(PublicModel):
    # Vocabulary catalog behind documents_v1.disclosure_topics / filing_type:
    # the full class set (class_map.json) with each class's processing
    # disposition, plus the classification_rule versions actually loaded in
    # the DB. processing_policy fields degrade (available=false + note) when
    # the policy file is unreachable — the class set and rule versions stay.
    class_map_version: str
    processing_policy_version: str | None
    processing_policy_available: bool
    classes: list[ProcessingClassV1]
    rule_sets: list[ClassificationRuleSetV1]
    note: str | None = None


class DocumentListResponse(PublicModel):
    items: list[DocumentV1]
    next_cursor: str | None


class UnitListResponse(PublicModel):
    items: list[DocumentUnitV1]
    next_cursor: str | None
    warning: str | None = None


class ChangeListResponse(PublicModel):
    items: list[ChangeEventV1]
    next_cursor: str | None


class UnitContextResponse(PublicModel):
    asset_id: str
    asset_uri: str
    is_active_run: bool
    document: DocumentV1
    heading_path: list[str]
    title: str | None
    payload: dict[str, Any]
    evidence_refs: list[EvidenceRefV1]
    excerpt: str | None = None
    start: int | None = None
    end: int | None = None
    excerpt_hash: str | None = None
