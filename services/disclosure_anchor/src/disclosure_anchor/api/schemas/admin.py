"""Pydantic request/response models for local admin endpoints."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal, Any

from pydantic import BaseModel, ConfigDict


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
    method: Literal["auto", "txt", "ocr"] | None = None
    backend: Literal["pipeline", "vlm-transformers", "vlm-sglang-engine"] | None = None
    language: Literal["ch", "en"] | None = None
    formula: bool | None = None
    table: bool | None = None
    start_page: int | None = None
    end_page: int | None = None
    timeout_seconds: int | None = None


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


class PublishRunRequest(AdminModel):
    allow_empty: bool = False
    reason: str | None = None


class PublishRunResponse(AdminModel):
    document_id: str
    processing_run_id: str
    is_active: bool
