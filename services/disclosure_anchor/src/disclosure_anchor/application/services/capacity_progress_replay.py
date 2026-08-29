"""Replay the existing content-free capacity progress v1 contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from disclosure_anchor.application.contracts.synchronized_telemetry import (
    BlockedProgressEvent,
    DurablePageCommitEvent,
    ProgressEvent,
)


@dataclass(frozen=True, slots=True)
class CapacityProgressReplay:
    durable_unique_pages: int
    durable_sources: tuple[tuple[str, str, int], ...]
    blocked_duration_ns: int


def replay_capacity_progress(
    events: Iterable[ProgressEvent],
    *,
    initial_cumulative_unique_pages: int = 0,
    prior_durable_sources: tuple[tuple[str, str, int], ...] = (),
) -> CapacityProgressReplay:
    """Validate one run's sequence, identity, cumulative pages and non-overlap."""

    materialized = tuple(events)
    if not materialized:
        raise ValueError("capacity progress event chain is empty")
    first = materialized[0]
    identity = (
        first.run_id,
        first.process_epoch_sha256,
        first.process_profile_sha256,
        first.clock_domain_identity_sha256,
    )
    if initial_cumulative_unique_pages < 0:
        raise ValueError("initial cumulative page count is negative")
    for source, profile, pages in prior_durable_sources:
        if (
            not source.startswith("sha256:")
            or len(source) != 71
            or not profile.startswith("sha256:")
            or len(profile) != 71
            or isinstance(pages, bool)
            or not isinstance(pages, int)
            or pages < 1
        ):
            raise ValueError("prior durable source evidence is invalid")
    sources = {(source, profile): pages for source, profile, pages in prior_durable_sources}
    if len(sources) != len(prior_durable_sources) or sum(sources.values()) != initial_cumulative_unique_pages:
        raise ValueError("prior durable source evidence does not reconcile")
    blocked: list[tuple[int, int]] = []
    cumulative = initial_cumulative_unique_pages
    previous_monotonic = -1
    first_sequence = materialized[0].sequence
    for offset, event in enumerate(materialized):
        if event.sequence != first_sequence + offset:
            raise ValueError("capacity progress sequence has a gap or rollback")
        if (
            event.run_id,
            event.process_epoch_sha256,
            event.process_profile_sha256,
            event.clock_domain_identity_sha256,
        ) != identity:
            raise ValueError("capacity progress identity drifted")
        if event.monotonic_ns < previous_monotonic:
            raise ValueError("capacity progress monotonic clock regressed")
        previous_monotonic = event.monotonic_ns
        if isinstance(event, BlockedProgressEvent):
            interval = (
                event.blocked_interval_started_monotonic_ns,
                event.monotonic_ns,
            )
            if blocked and interval[0] < blocked[-1][1]:
                raise ValueError("capacity progress blocked intervals overlap")
            blocked.append(interval)
            continue
        if not isinstance(event, DurablePageCommitEvent):
            raise TypeError("capacity progress event type is not closed")
        key = (event.source_identity_sha256, event.process_profile_sha256)
        if key in sources:
            if sources[key] != event.committed_source_pages:
                raise ValueError("durable source page count conflicts")
            raise ValueError("durable source/profile identity was repeated")
        cumulative += event.committed_source_pages
        if event.cumulative_unique_source_pages != cumulative:
            raise ValueError("durable cumulative page count does not reconcile")
        sources[key] = event.committed_source_pages
    return CapacityProgressReplay(
        durable_unique_pages=cumulative,
        durable_sources=tuple(
            sorted((source, profile, pages) for (source, profile), pages in sources.items())
        ),
        blocked_duration_ns=sum(end - start for start, end in blocked),
    )


__all__ = ["CapacityProgressReplay", "replay_capacity_progress"]
