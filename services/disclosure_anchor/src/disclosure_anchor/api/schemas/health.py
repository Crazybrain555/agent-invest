"""Health response schema."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class QueueStatus(BaseModel):
    # Read-only operational snapshot of the worker queues (08 §1 predicates,
    # reused from application.worker.queries). Every count is a non-negative
    # gauge; last_outbox_event_at is the most recent change-feed write.
    pending_download: int
    pending_parse: int
    pending_build: int
    pending_publish: int
    download_dead_letters: int
    parse_dead_letters: int
    retrying_documents: int
    sync_due: int
    # backfill admission watermark = pending_download + pending_parse backlog
    # (queries.pending_processing_backlog_count).
    backfill_pending: int
    last_outbox_event_at: datetime | None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
    version: str
    migration_head: str | None
    data_root_mounted: bool
    # Absent (null) when the reader engine/settings are unavailable or a queue
    # read fails — health stays a status report, never a 500 (same degrade
    # stance as migration_head).
    queues: QueueStatus | None = None
