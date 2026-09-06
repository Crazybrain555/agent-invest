"""Derive capacity from exact remote and local composition identities."""

from __future__ import annotations

from dataclasses import dataclass

from disclosure_anchor.application.contracts.mineru_process_profile import (
    MineruProcessProfile,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
)
from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorLimits,
)


@dataclass(frozen=True, slots=True)
class StagedV4DatabaseConcurrency:
    """Maximum simultaneous primary and nested stage DB checkouts."""

    primary_stage_checkouts: int
    nested_commit_checkouts: int


def staged_v4_coordinator_limits(
    profile: MineruProcessProfile,
    *,
    worker_profile: StagedWorkerProfileV4,
) -> CoordinatorLimits:
    """Project immutable process ceilings into scheduler limits.

    Every byte/item/page dimension has one named profile authority.  Worker
    counts are upper bounds for local dispatch, not additional capacity; the
    vector ledger remains the final admission decision.
    """

    if type(profile) is not MineruProcessProfile:
        raise ValueError("staged V4 limits require an exact process profile")
    if (type(worker_profile) is not StagedWorkerProfileV4
            or worker_profile.process_profile_sha256 != profile.sha256):
        raise ValueError("staged V4 worker profile does not bind the exact process profile")
    mac_preflight_workers = worker_profile.mac_preflight_workers
    mac_finalize_workers = worker_profile.mac_finalize_workers
    registry_items = profile.registry_nonterminal_cap + profile.registry_terminal_cap
    remote_slots = profile.api_max_pending_tasks
    return CoordinatorLimits(
        credits=ResourceCreditVector(
            # A document, snapshot, provider task and ACK responsibility span
            # both the remote nonterminal registry and the retained-terminal
            # registry.  Capping any of them at the pending depth would make a
            # terminal document block the next remote submission until ACK.
            documents=registry_items,
            snapshot_items=registry_items,
            snapshot_bytes=profile.source_pdf_bytes_limit,
            remote_waits=remote_slots,
            provider_tasks=registry_items,
            provider_result_bytes=profile.max_unacked_result_bytes,
            # These are Mac ownership ceilings. Windows finalizer_slots only
            # controls remote result construction and is not a local lane or
            # local artifact limit.
            materialization_items=mac_finalize_workers,
            compressed_bytes=profile.max_unacked_result_bytes,
            decoded_bytes=profile.decoded_payload_bytes_limit,
            temp_disk_bytes=profile.temporary_disk_bytes_limit,
            output_items=profile.registry_terminal_cap,
            output_bytes=profile.terminal_output_bytes_limit,
            output_pages=profile.unpublished_pages_limit,
            ack_items=registry_items,
        ),
        recovery_page_size=min(1000, registry_items),
        admission_batch_size=profile.api_max_pending_tasks,
        preflight_workers=mac_preflight_workers,
        remote_workers=remote_slots,
        local_prepare_workers=mac_finalize_workers,
        local_workers=mac_finalize_workers,
        commit_workers=mac_finalize_workers,
        cleanup_workers=mac_finalize_workers,
        ack_workers=mac_finalize_workers,
        admission_probe_seconds=worker_profile.admission_probe_milliseconds / 1000,
    )


def staged_v4_database_concurrency(
    profile: MineruProcessProfile,
    *,
    worker_profile: StagedWorkerProfileV4,
) -> StagedV4DatabaseConcurrency:
    """Derive DB checkout concurrency from the exact seven-lane topology.

    Every lane episode can briefly own one UoW. A transaction-P worker holds
    the document producer UoW while the atomic publisher uses a second DB
    transaction, so commit contributes one additional nested checkout.
    """

    limits = staged_v4_coordinator_limits(
        profile,
        worker_profile=worker_profile,
    )
    primary = sum(
        (
            limits.preflight_workers,
            limits.remote_workers,
            limits.local_prepare_workers,
            limits.local_workers,
            limits.commit_workers,
            limits.cleanup_workers,
            limits.ack_workers,
        )
    )
    return StagedV4DatabaseConcurrency(
        primary_stage_checkouts=primary,
        nested_commit_checkouts=limits.commit_workers,
    )


__all__ = [
    "StagedV4DatabaseConcurrency",
    "staged_v4_coordinator_limits",
    "staged_v4_database_concurrency",
]
