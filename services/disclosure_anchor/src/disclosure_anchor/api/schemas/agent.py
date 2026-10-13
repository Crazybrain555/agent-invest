"""Pydantic models of the private agent API (processing requests, quick text).

They mirror ``disclosure-processing-request.v1`` and ``disclosure-quick-text.v1``
exactly; ``extra="forbid"`` makes any drift between the application payloads
and the published wire contract a response failure instead of silent output.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class AgentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentTargetIn(AgentModel):
    kind: Literal["document"]
    document_id: str


class ProviderDocumentTargetIn(AgentModel):
    kind: Literal["provider_document"]
    security_code: str
    exchange: str
    date_from: date
    date_to: date
    provider_document_id: str


class TitleMatchTargetIn(AgentModel):
    kind: Literal["title_match"]
    security_code: str
    exchange: str
    date_from: date
    date_to: date
    title_contains: str


TargetIn = Annotated[
    Union[DocumentTargetIn, ProviderDocumentTargetIn, TitleMatchTargetIn],
    Field(discriminator="kind"),
]


class ProcessingRequestIn(AgentModel):
    idempotency_key: str
    enqueue_parse: bool = True
    target: TargetIn


class AttemptSummary(AgentModel):
    outcome: Literal[
        "matched", "ambiguous", "not_found", "failed", "abandoned", "deferred"
    ] | None
    started_at: str
    finished_at: str | None
    error_code: str | None
    retryable: bool | None


class FilingChoice(AgentModel):
    provider_document_id: str | None
    title: str | None
    announcement_date: str | None
    security_code: str | None
    exchange: str | None
    document_id: str | None


class ResolutionStatus(AgentModel):
    state: Literal[
        "pending", "discovering", "resolved", "needs_selection", "not_found", "failed"
    ]
    method: Literal[
        "document_id", "local_document", "local_candidate", "provider_discovery"
    ] | None
    attempt_count: int
    max_attempts: int
    item_failure_count: int
    max_item_failures: int
    next_attempt_at: str | None
    last_attempt: AttemptSummary | None
    provider_row_count: int | None
    choices: list[FilingChoice]
    choices_truncated: bool
    index_source_access_id: str | None


class FilingStatus(AgentModel):
    provider: str | None
    provider_document_id: str | None
    security_code: str | None
    exchange: str | None
    title: str | None
    announcement_date: str | None
    document_id: str | None


class DownloadFailure(AgentModel):
    error_code: str | None
    retryable: bool | None
    accessed_at: str | None


class OriginalStatus(AgentModel):
    state: Literal["pending_resolution", "archived", "download_queued", "download_blocked"]
    document_id: str | None
    raw_file_hash: str | None
    text_path: str | None
    blocked_reason: Literal[
        "download_retry_exhausted", "download_nonretryable_failure", "download_not_selected"
    ] | None
    failed_download_count: int | None
    max_download_retries: int
    last_download_failure: DownloadFailure | None


class RetryFacts(AgentModel):
    item_failure_count: int | None
    charged_failure_count: int | None
    max_item_failures: int | None
    max_charged_failures: int | None
    latest_failed_run_id: str | None
    latest_failed_run_retryable: bool | None
    latest_failed_run_released: bool
    unreleased_contract_failures: int | None


ParseBlockedReason = Literal[
    "latest_failure_nonretryable",
    "unreleased_contract_failure",
    "item_retry_budget_exhausted",
    "charged_retry_budget_exhausted",
    "not_selected_by_queue",
    "post_processing_not_selected",
    "outside_parse_scope",
]


class ParseStatus(AgentModel):
    state: Literal[
        "pending_resolution",
        "waiting_for_original",
        "not_requested",
        "parse_queued",
        "parse_running",
        "post_processing",
        "published",
        "parse_blocked",
    ]
    requested: bool
    document_status: str | None
    current_processing_run_id: str | None
    running_processing_run_id: str | None
    units_path: str | None
    blocked_reasons: list[ParseBlockedReason]
    retry: RetryFacts | None


class ProcessingStatus(AgentModel):
    state: Literal[
        "resolving",
        "needs_selection",
        "not_found",
        "resolution_failed",
        "download_queued",
        "download_blocked",
        "archived",
        "parse_queued",
        "parse_running",
        "post_processing",
        "published",
        "parse_blocked",
    ]
    terminal: bool
    next_action: Literal[
        "wait",
        "retry_same_request",
        "choose_and_submit_new_request",
        "none",
        "operator_action_required",
    ]
    status_sha256: str
    observed_at: str
    resolution: ResolutionStatus
    filing: FilingStatus | None
    original: OriginalStatus
    parse: ParseStatus


class ProcessingRequestOut(AgentModel):
    contract_version: Literal["disclosure-processing-request.v1"]
    request_id: str
    idempotency_key: str
    intent_sha256: str
    created_at: str
    enqueue_parse: bool
    target: dict[str, str]
    replayed: bool | None = None
    status: ProcessingStatus


class WaitInfo(AgentModel):
    outcome: Literal["changed", "terminal", "timeout"]
    waited_ms: int


class ProcessingRequestWaitOut(ProcessingRequestOut):
    wait: WaitInfo


class TextChunk(AgentModel):
    page_number: int
    char_start: int
    char_end: int
    page_text_chars: int
    text: str
    page_complete: bool
    no_extractable_text: bool


class TextExtractor(AgentModel):
    name: str
    version: str
    ocr: bool


class TextCoverage(AgentModel):
    pages_scanned: int
    next_page: int
    next_char_offset: int
    complete: bool
    full_document_traversed: bool


class TextCitation(AgentModel):
    document_id: str
    provider: str | None
    provider_document_id: str | None
    security_code: str | None
    exchange: str | None
    title: str | None
    announcement_date: str | None
    raw_file_hash: str
    source_ref: str | None
    company_ref: str | None
    security_ref: str | None
    text_kind: Literal["quick_native_pdf_text"]


class DocumentTextOut(AgentModel):
    contract_version: Literal["disclosure-quick-text.v1"]
    document_id: str
    raw_file_hash: str
    source_byte_count: int
    document_status: str
    formal_units_available: bool
    text_kind: Literal["quick_native_pdf_text"]
    extractor: TextExtractor
    total_pages: int
    chunks: list[TextChunk]
    coverage: TextCoverage
    next_cursor: str | None
    limitations: list[str]
    citation: TextCitation
