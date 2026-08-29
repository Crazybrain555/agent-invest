"""Content-free replay contract for durable whole-document publish evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, Iterable, Mapping


SOURCE_IDENTITY_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class DurablePublishKpiSnapshot:
    started_at: datetime
    finished_at: datetime
    unique_source_pages: int
    unique_source_count: int
    incomplete_publish_count: int
    conflict_count: int
    sources: tuple[tuple[str, int], ...]

    def as_dict(self, *, boundary: str) -> dict[str, object]:
        return {
            "boundary": boundary,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "unique_source_pages": self.unique_source_pages,
            "unique_source_count": self.unique_source_count,
            "incomplete_publish_count": self.incomplete_publish_count,
            "conflict_count": self.conflict_count,
            "sources": [
                {"source_identity": identity, "source_pages": pages}
                for identity, pages in self.sources
            ],
        }


@dataclass(frozen=True)
class _PublishEvidence:
    source_identity: str
    source_pages: int
    publish_committed_at: datetime


def replay_durable_publish_kpi(
    rows: Iterable[Mapping[str, Any]],
    *,
    started_at: datetime,
    finished_at: datetime,
) -> DurablePublishKpiSnapshot:
    bases: dict[str, tuple[datetime, _PublishEvidence | None]] = {}
    supplements: dict[str, _PublishEvidence] = {}
    conflicts = 0
    for row in rows:
        run_id = str(row["processing_run_id"])
        evidence = _evidence(row.get("payload"))
        if row["event_kind"] == "processing_run_published":
            occurred_at = row.get("occurred_at")
            if not isinstance(occurred_at, datetime):
                conflicts += 1
                continue
            if not (started_at <= occurred_at < finished_at):
                conflicts += 1
                continue
            candidate = (occurred_at, evidence)
            if run_id in bases and bases[run_id] != candidate:
                conflicts += 1
            bases[run_id] = candidate
        elif evidence is not None:
            previous = supplements.get(run_id)
            if previous is not None and previous != evidence:
                conflicts += 1
            supplements[run_id] = evidence
    identities: dict[str, int] = {}
    incomplete = 0
    for run_id, (base_committed_at, base) in bases.items():
        supplement = supplements.get(run_id)
        if base is not None and base.publish_committed_at != base_committed_at:
            conflicts += 1
            base = None
        if supplement is not None and supplement.publish_committed_at != base_committed_at:
            conflicts += 1
            supplement = None
        if base is not None and supplement is not None and base != supplement:
            conflicts += 1
        evidence = base or supplement
        if evidence is None:
            incomplete += 1
            continue
        identity = evidence.source_identity
        pages = evidence.source_pages
        previous_pages = identities.get(identity)
        if previous_pages is None:
            identities[identity] = pages
        elif previous_pages != pages:
            conflicts += 1
    sources = tuple(sorted(identities.items()))
    return DurablePublishKpiSnapshot(
        started_at=started_at,
        finished_at=finished_at,
        unique_source_pages=sum(pages for _, pages in sources),
        unique_source_count=len(sources),
        incomplete_publish_count=incomplete,
        conflict_count=conflicts,
        sources=sources,
    )


def _evidence(payload: object) -> _PublishEvidence | None:
    if not isinstance(payload, dict):
        return None
    identity = payload.get("source_identity")
    pages = payload.get("source_page_count")
    committed_at = payload.get("publish_committed_at")
    if not isinstance(identity, str) or SOURCE_IDENTITY_RE.fullmatch(identity) is None:
        return None
    if isinstance(pages, bool) or not isinstance(pages, int) or pages < 1:
        return None
    if not isinstance(committed_at, str):
        return None
    try:
        parsed_committed_at = datetime.fromisoformat(committed_at)
    except ValueError:
        return None
    if parsed_committed_at.tzinfo is None:
        return None
    return _PublishEvidence(identity, pages, parsed_committed_at)
