"""Derive capacity from exact remote and local composition identities."""

from __future__ import annotations

from dataclasses import dataclass

from disclosure_anchor.application.contracts.mineru_capacity_config import (
    MineruResultStoragePolicy,
    mac_document_disk_upper_bound,
    mac_work_file_margin_bytes,
)
from disclosure_anchor.application.contracts.mineru_process_profile import (
    RESULT_STORAGE_PROCESS_PROFILE_CONTRACT,
    MineruProcessProfile,
    legacy_result_budgets,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
)
from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorLimits,
)

# One bounded remote/local stage step (not the LOCAL stage of a storage-bound
# worker, which the policy's frozen decode stage bounds, and whose transfer
# resumes from its durable spool prefix). The claim lease sits at the
# contract ceiling so that
# ``lease - step - renew margin`` stays at the 30 s the scheduler had before:
# that difference is the renewal round-trip headroom, the renewal cadence and
# the ceiling on every deferral/backoff wait.
PRODUCTION_MAX_STAGE_STEP_SECONDS = 240.0
PRODUCTION_CLAIM_LEASE_SECONDS = 300


@dataclass(frozen=True, slots=True)
class StagedV4DatabaseConcurrency:
    """Maximum simultaneous primary and nested stage DB checkouts."""

    primary_stage_checkouts: int
    nested_commit_checkouts: int


def staged_v4_provider_result_limit(
    profile: MineruProcessProfile,
    storage_policy: MineruResultStoragePolicy | None,
) -> int:
    """The coordinator's hard ceiling on retained provider results.

    v1/v2 use their aggregate L. A v3 epoch uses its bound policy's unborrowable
    completion escrow C, which every legal result (<= the hard limit) fits: the
    normal target only sizes each attempt's scheduling estimate, so a verified
    large result is granted rather than made impossible. A policy that is not
    the profile's exact binding is refused rather than guessed.
    """

    if profile.contract_version == RESULT_STORAGE_PROCESS_PROFILE_CONTRACT:
        if (
            type(storage_policy) is not MineruResultStoragePolicy
            or storage_policy.sha256 != profile.result_storage_policy_sha256
        ):
            raise ValueError("staged V4 limits require the process profile's exact storage policy")
        return storage_policy.native_completion_escrow_bytes
    if storage_policy is not None:
        raise ValueError("a v1/v2 process profile has no result storage policy")
    return legacy_result_budgets(profile)[1]


def staged_v4_coordinator_limits(
    profile: MineruProcessProfile,
    *,
    worker_profile: StagedWorkerProfileV4,
    storage_policy: MineruResultStoragePolicy | None = None,
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
    provider_result_limit = staged_v4_provider_result_limit(profile, storage_policy)
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
            provider_result_bytes=provider_result_limit,
            # These are Mac ownership ceilings. Windows finalizer_slots only
            # controls remote result construction and is not a local lane or
            # local artifact limit.
            materialization_items=mac_finalize_workers,
            compressed_bytes=provider_result_limit,
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
        claim_lease_seconds=PRODUCTION_CLAIM_LEASE_SECONDS,
        max_stage_step_seconds=PRODUCTION_MAX_STAGE_STEP_SECONDS,
        commit_stage_seconds=worker_profile.commit_stage_seconds,
        # Storage-bound: the policy's frozen decode stage bounds one LOCAL
        # stage (download, unpack, one decode) with claim renewal inside it.
        local_stage_seconds=(
            None if storage_policy is None else float(storage_policy.mac_decode_stage_seconds)
        ),
        # The business quota D over distinct work-volume extents; the live
        # free floor H stays with the actual writes in the materializer.
        work_disk_bytes=(
            None if storage_policy is None else storage_policy.mac_work_disk_limit_bytes
        ),
        work_disk_margin_bytes=(
            0 if storage_policy is None else mac_work_file_margin_bytes(storage_policy)
        ),
        # Admission leaves one maximal grant of D free (the policy admits one
        # whole maximal document beside it), so a waiting grant never depends
        # on source-only documents releasing their snapshots.
        work_disk_local_reserve_bytes=(
            0 if storage_policy is None else mac_document_disk_upper_bound(
                storage_policy,
                storage_policy.native_result_hard_limit_bytes,
                storage_policy.native_source_single_limit_bytes,
            )
        ),
    )


def staged_v4_database_concurrency(
    profile: MineruProcessProfile,
    *,
    worker_profile: StagedWorkerProfileV4,
    storage_policy: MineruResultStoragePolicy | None = None,
) -> StagedV4DatabaseConcurrency:
    """Derive DB checkout concurrency from the exact seven-lane topology.

    Every lane episode can briefly own one UoW. A transaction-P worker holds
    the document producer UoW while the atomic publisher uses a second DB
    transaction, so commit contributes one additional nested checkout.
    """

    limits = staged_v4_coordinator_limits(
        profile,
        worker_profile=worker_profile,
        storage_policy=storage_policy,
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
