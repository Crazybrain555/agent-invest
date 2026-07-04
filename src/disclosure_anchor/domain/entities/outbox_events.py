"""Factory functions for outbox events."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from disclosure_anchor.domain import ids
from disclosure_anchor.domain.entities.core import OutboxEvent


def document_registered(
    *,
    document_id: str,
    provider: str,
    provider_document_id: str,
    raw_file_hash: str,
    occurred_at: datetime,
) -> OutboxEvent:
    return OutboxEvent(
        event_id=ids.new_outbox_event_id(),
        event_kind="document_registered",
        change_kind="materialized",
        subject_kind="document",
        subject_ref=document_id,
        document_id=document_id,
        payload={
            "provider": provider,
            "provider_document_id": provider_document_id,
            "raw_file_hash": raw_file_hash,
        },
        occurred_at=occurred_at,
    )


def document_observed(
    *,
    document_id: str,
    provider: str,
    provider_document_id: str,
    raw_file_hash: str,
    source_access_id: str,
    occurred_at: datetime,
) -> OutboxEvent:
    return OutboxEvent(
        event_id=ids.new_outbox_event_id(),
        event_kind="document_observed",
        change_kind="observed",
        subject_kind="document",
        subject_ref=document_id,
        document_id=document_id,
        payload={
            "provider": provider,
            "provider_document_id": provider_document_id,
            "raw_file_hash": raw_file_hash,
            "source_access_id": source_access_id,
        },
        occurred_at=occurred_at,
    )


def processing_run_created(
    *, document_id: str, processing_run_id: str, occurred_at: datetime
) -> OutboxEvent:
    return OutboxEvent(
        event_id=ids.new_outbox_event_id(),
        event_kind="processing_run_created",
        change_kind="observed",
        subject_kind="processing_run",
        subject_ref=processing_run_id,
        document_id=document_id,
        processing_run_id=processing_run_id,
        payload={"document_id": document_id, "status": "running"},
        occurred_at=occurred_at,
    )


def processing_run_failed(
    *,
    document_id: str,
    processing_run_id: str,
    error: dict[str, Any],
    occurred_at: datetime,
) -> OutboxEvent:
    return OutboxEvent(
        event_id=ids.new_outbox_event_id(),
        event_kind="processing_run_failed",
        change_kind="observed",
        subject_kind="processing_run",
        subject_ref=processing_run_id,
        document_id=document_id,
        processing_run_id=processing_run_id,
        payload={"document_id": document_id, "status": "failed", "error": error},
        occurred_at=occurred_at,
    )
