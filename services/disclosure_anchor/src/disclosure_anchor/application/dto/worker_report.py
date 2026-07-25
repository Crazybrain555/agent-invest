"""Worker batch limits and per-round report DTOs (08 §3.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class WorkerLimits:
    sync: int
    download: int
    parse: int
    build: int
    publish: int
    sync_stage_seconds: int = 300
    # Acquisition pump window: sync+download passes repeat inside the round
    # for up to this many seconds (progress-gated), so long parse batches no
    # longer cap acquisition at one pass per round. <= 0 = single pass; the
    # conservative DTO default keeps direct constructions (tests) on the
    # legacy single-pass shape — production wires WORKER_ACQUISITION_SECONDS.
    acquisition_seconds: int = 0


@dataclass(frozen=True)
class WorkerFailure:
    stage: str
    item_ref: str
    error_code: str
    retryable: bool | None = None
    # Human-readable context for the daily report; without it a stage-level
    # crash left only an exception class name to debug from (round23).
    message: str | None = None


@dataclass
class WorkerReport:
    started_at: datetime
    duration_seconds: float = 0.0
    stale_reclaimed: int = 0
    synced_companies: int = 0
    candidates_discovered: int = 0
    downloaded: int = 0
    parsed: int = 0
    built: int = 0
    published: int = 0
    # Publishes that deactivated a prior run — the only in-service source of
    # search-projection orphans; gates the per-round orphan prune.
    runs_deactivated: int = 0
    projected: int = 0
    failed: int = 0
    sync_quota_break: bool = False
    sync_rate_limited: bool = False
    parse_concurrency_limit: int | None = None
    parse_peak_inflight: int = 0
    parse_regular_dispatched: int = 0
    parse_heavy_dispatched: int = 0
    parse_huge_dispatched: int = 0
    parse_unknown_page_count: int = 0
    source_outage_break: bool = False
    deferred_backfill: int = 0
    failures: list[WorkerFailure] = field(default_factory=list)
    build_stats: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 3),
            "stale_reclaimed": self.stale_reclaimed,
            "synced_companies": self.synced_companies,
            "candidates_discovered": self.candidates_discovered,
            "downloaded": self.downloaded,
            "sync_quota_break": self.sync_quota_break,
            "sync_rate_limited": self.sync_rate_limited,
            "parse_concurrency_limit": self.parse_concurrency_limit,
            "parse_peak_inflight": self.parse_peak_inflight,
            "parse_regular_dispatched": self.parse_regular_dispatched,
            "parse_heavy_dispatched": self.parse_heavy_dispatched,
            "parse_huge_dispatched": self.parse_huge_dispatched,
            "parse_unknown_page_count": self.parse_unknown_page_count,
            "source_outage_break": self.source_outage_break,
            "deferred_backfill": self.deferred_backfill,
            "parsed": self.parsed,
            "built": self.built,
            "published": self.published,
            "runs_deactivated": self.runs_deactivated,
            "projected": self.projected,
            "failed": self.failed,
            "failures": [
                {
                    "stage": failure.stage,
                    "item_ref": failure.item_ref,
                    "error_code": failure.error_code,
                    "retryable": failure.retryable,
                    "message": failure.message,
                }
                for failure in self.failures
            ],
        }
