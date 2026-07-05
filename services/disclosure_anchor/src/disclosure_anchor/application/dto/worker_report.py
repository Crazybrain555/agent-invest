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


@dataclass(frozen=True)
class WorkerFailure:
    stage: str
    item_ref: str
    error_code: str


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
    failed: int = 0
    skipped_oversized: int = 0
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
            "parsed": self.parsed,
            "built": self.built,
            "published": self.published,
            "failed": self.failed,
            "skipped_oversized": self.skipped_oversized,
            "failures": [
                {
                    "stage": failure.stage,
                    "item_ref": failure.item_ref,
                    "error_code": failure.error_code,
                }
                for failure in self.failures
            ],
        }
