"""Domain entities for the L1 disclosure objects.

These are parser-/storage-neutral records. They must not import SQLAlchemy,
FastAPI or any adapter. The PostgreSQL adapter maps them to/from ORM models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional


@dataclass
class Company:
    company_id: str
    legal_name: str
    unified_social_credit_code: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class CompanyIdentifier:
    identifier_id: str
    company_id: str
    scheme: str
    raw_value: str
    normalized_value: str
    observed_at: datetime
    jurisdiction: Optional[str] = None
    source_access_id: Optional[str] = None
    status: str = "active"
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    created_at: Optional[datetime] = None


@dataclass
class Security:
    security_id: str
    company_id: str
    security_code: str
    exchange: str
    board: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class TrackedCompany:
    tracked_company_id: str
    company_id: str
    security_id: Optional[str] = None
    status: str = "active"
    lookback: Optional[dict[str, Any]] = None
    process_classes: Optional[list[str]] = None
    sync_frequency: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class SourceAccess:
    source_access_id: str
    provider: str
    accessed_at: datetime
    status: str
    provider_interface: Optional[str] = None
    dataset_key: Optional[str] = None
    query_params: Optional[dict[str, Any]] = None
    result_hash: Optional[str] = None
    error: Optional[str] = None
    result_snapshot: Optional[dict[str, Any]] = None
    company_id: Optional[str] = None
    security_id: Optional[str] = None
    created_at: Optional[datetime] = None


@dataclass
class SourceCheckpoint:
    source_checkpoint_id: str
    provider: str
    scope_key: str
    cursor: Optional[dict[str, Any]] = None
    updated_at: Optional[datetime] = None


@dataclass
class Document:
    document_id: str
    status: str
    title: Optional[str] = None
    company_id: Optional[str] = None
    security_id: Optional[str] = None
    source_access_id: Optional[str] = None
    provider: Optional[str] = None
    provider_document_id: Optional[str] = None
    announcement_date: Optional[date] = None
    report_period: Optional[str] = None
    raw_file_relpath: Optional[str] = None
    raw_file_hash: Optional[str] = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    current_processing_run_id: Optional[str] = None
    supersedes_document_id: Optional[str] = None
    correction_of_document_id: Optional[str] = None
    class_filing_type: Optional[str] = None
    class_rules_version: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class ProcessingRun:
    processing_run_id: str
    document_id: str
    artifact_owner_processing_run_id: str
    run_kind: str
    status: str
    parser_name: Optional[str] = None
    parser_version: Optional[str] = None
    parser_backend: Optional[str] = None
    parser_method: Optional[str] = None
    parser_language: Optional[str] = None
    parser_target_identity: Optional[dict[str, Any]] = None
    search_projection_error: Optional[dict[str, Any]] = None
    input_raw_file_hash: Optional[str] = None
    parser_artifact_relpath: Optional[str] = None
    artifact_hash: Optional[str] = None
    normalized_ir_relpath: Optional[str] = None
    document_units_relpath: Optional[str] = None
    content_hash_aggregate: Optional[str] = None
    structure_hash: Optional[str] = None
    builder_rules_version: Optional[str] = None
    is_active: bool = False
    unit_build_status: str = "not_started"
    unit_build_error: Optional[dict[str, Any]] = None
    unit_build_attempt_count: int = 0
    unit_built_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[dict[str, Any]] = None
    created_at: Optional[datetime] = None


@dataclass
class DocumentUnit:
    asset_id: str
    document_id: str
    processing_run_id: str
    payload_kind: str
    order_index: int
    payload: dict[str, Any]
    content_hash: str
    heading_path: list[str] = field(default_factory=list)
    title: Optional[str] = None
    semantic_key: Optional[str] = None
    structure_hash: Optional[str] = None
    quality_status: str = "ok"
    # All recall keys on the unit: its own semantic_key plus mixed parts'
    # keys (Codex round4 P1#1 — grouping must not swallow recall).
    semantic_keys: Optional[list[str]] = None
    # Section applicability declared by √适用/□不适用 marker lines (05 §8.5).
    applicability: Optional[str] = None
    # First source page of the unit (from the parser artifact locator).
    page_no: Optional[int] = None
    query_projection_hash: Optional[str] = None
    provider_document_id: Optional[str] = None
    artifact_locator: Optional[dict[str, Any]] = None
    created_at: Optional[datetime] = None


@dataclass
class OutboxEvent:
    event_id: str
    event_kind: str
    change_kind: str
    subject_kind: str
    subject_ref: str
    seq: Optional[int] = None
    document_id: Optional[str] = None
    processing_run_id: Optional[str] = None
    asset_id: Optional[str] = None
    payload: Optional[dict[str, Any]] = None
    occurred_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
