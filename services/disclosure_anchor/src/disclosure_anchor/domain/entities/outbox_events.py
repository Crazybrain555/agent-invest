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
    *,
    document_id: str,
    processing_run_id: str,
    occurred_at: datetime,
    status: str = "running",
) -> OutboxEvent:
    return OutboxEvent(
        event_id=ids.new_outbox_event_id(),
        event_kind="processing_run_created",
        change_kind="observed",
        subject_kind="processing_run",
        subject_ref=processing_run_id,
        document_id=document_id,
        processing_run_id=processing_run_id,
        payload={"document_id": document_id, "status": status},
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


def document_unit_created(
    *,
    document_id: str,
    processing_run_id: str,
    new_asset_id: str,
    content_hash: str,
    payload_kind: str,
    new_order_index: int,
    new_heading_path: list[str],
    occurred_at: datetime,
) -> OutboxEvent:
    return OutboxEvent(
        event_id=ids.new_outbox_event_id(),
        event_kind="document_unit_created",
        change_kind="materialized",
        subject_kind="document_unit",
        subject_ref=new_asset_id,
        document_id=document_id,
        processing_run_id=processing_run_id,
        asset_id=new_asset_id,
        payload={
            "new_asset_id": new_asset_id,
            "new_processing_run_id": processing_run_id,
            "content_hash": content_hash,
            "payload_kind": payload_kind,
            "new_order_index": new_order_index,
            "new_heading_path": new_heading_path,
        },
        occurred_at=occurred_at,
    )


def document_unit_removed(
    *,
    document_id: str,
    old_processing_run_id: str,
    old_asset_id: str,
    content_hash: str,
    payload_kind: str,
    old_order_index: int,
    old_heading_path: list[str],
    occurred_at: datetime,
) -> OutboxEvent:
    return OutboxEvent(
        event_id=ids.new_outbox_event_id(),
        event_kind="document_unit_removed",
        change_kind="materialized",
        subject_kind="document_unit",
        subject_ref=old_asset_id,
        document_id=document_id,
        processing_run_id=old_processing_run_id,
        asset_id=old_asset_id,
        payload={
            "old_asset_id": old_asset_id,
            "old_processing_run_id": old_processing_run_id,
            "content_hash": content_hash,
            "payload_kind": payload_kind,
            "old_order_index": old_order_index,
            "old_heading_path": old_heading_path,
        },
        occurred_at=occurred_at,
    )


def document_unit_projection_changed(
    *,
    document_id: str,
    new_processing_run_id: str,
    old_asset_id: str,
    new_asset_id: str,
    content_hash: str,
    old_query_projection_hash: str | None,
    new_query_projection_hash: str | None,
    changed_fields: list[str],
    occurred_at: datetime,
) -> OutboxEvent:
    return OutboxEvent(
        event_id=ids.new_outbox_event_id(),
        event_kind="document_unit_projection_changed",
        change_kind="materialized",
        subject_kind="document_unit",
        subject_ref=new_asset_id,
        document_id=document_id,
        processing_run_id=new_processing_run_id,
        asset_id=new_asset_id,
        payload={
            "old_asset_id": old_asset_id,
            "new_asset_id": new_asset_id,
            "content_hash": content_hash,
            "old_query_projection_hash": old_query_projection_hash,
            "new_query_projection_hash": new_query_projection_hash,
            "changed_fields": changed_fields,
        },
        occurred_at=occurred_at,
    )


def processing_run_published(
    *,
    document_id: str,
    processing_run_id: str,
    change_kind: str,
    previous_processing_run_id: str | None,
    content_hash_aggregate: str | None,
    structure_hash: str | None,
    unit_count: int,
    created_count: int,
    removed_count: int,
    projection_changed_count: int,
    occurred_at: datetime,
    allow_empty_reason: str | None = None,
    source_identity: str,
    source_page_count: int,
    publish_committed_at: datetime,
) -> OutboxEvent:
    payload: dict[str, Any] = {
        "previous_processing_run_id": previous_processing_run_id,
        "content_hash_aggregate": content_hash_aggregate,
        "structure_hash": structure_hash,
        "unit_count": unit_count,
        "created_count": created_count,
        "removed_count": removed_count,
        "projection_changed_count": projection_changed_count,
        "source_identity": source_identity,
        "source_page_count": source_page_count,
        "publish_committed_at": publish_committed_at.isoformat(),
    }
    if allow_empty_reason is not None:
        payload["allow_empty_reason"] = allow_empty_reason
    return OutboxEvent(
        event_id=ids.new_outbox_event_id(),
        event_kind="processing_run_published",
        change_kind=change_kind,
        subject_kind="processing_run",
        subject_ref=processing_run_id,
        document_id=document_id,
        processing_run_id=processing_run_id,
        payload=payload,
        occurred_at=occurred_at,
    )


def processing_run_publish_evidence_backfilled(
    *,
    document_id: str,
    processing_run_id: str,
    source_identity: str,
    source_page_count: int,
    publish_committed_at: datetime,
    occurred_at: datetime,
) -> OutboxEvent:
    return OutboxEvent(
        event_id=ids.new_outbox_event_id(),
        event_kind="processing_run_publish_evidence_backfilled",
        change_kind="observed",
        subject_kind="processing_run",
        subject_ref=processing_run_id,
        document_id=document_id,
        processing_run_id=processing_run_id,
        payload={
            "source_identity": source_identity,
            "source_page_count": source_page_count,
            "publish_committed_at": publish_committed_at.isoformat(),
        },
        occurred_at=occurred_at,
    )
