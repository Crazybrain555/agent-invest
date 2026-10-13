"""Synthetic E7 -> newly qualified result-storage runtime evidence.

The receipt topology comes from the existing Package A fixture. All changed
capacity, runtime, health, profile and epoch identities are rebuilt here. The
real deployment checker, not this fixture, decides whether they are coherent.
No live API, model, PDF, database or production qualification is consulted.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import copy
import json
from pathlib import Path
from unittest import mock

import httpx

from disclosure_anchor.adapters.runtime import mineru_deployment_gate as gate
from disclosure_anchor.adapters.runtime import mineru_execution_upgrade as upgrade_runtime
from disclosure_anchor.adapters.runtime.mineru_identity import writer_code_digest
from disclosure_anchor.application.contracts.mineru_capacity_config import (
    MineruCapacityConfigV2, MineruResultStoragePolicy,
)
from disclosure_anchor.application.contracts.mineru_process_profile import (
    RESULT_STORAGE_PROCESS_PROFILE_CONTRACT, MineruProcessProfile,
)
from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    LegacyScopeInventory, encode_execution_release_manifest,
    encode_legacy_key_lookup_evidence, encode_legacy_scope_inventory,
    encode_qualified_runtime_upgrade,
)
from disclosure_anchor.settings import Settings
from tests._f5_upgrade_q0_fixture import ParentQ0, synthetic_parent_q0, sha256_bytes
from tests._f5_upgrade_u01_fixture import (
    ARCHIVE_MEMBER_COUNT_LIMIT, GPU_METRICS_URL, exact_json,
    frozen_wall_clock, runtime_identity, write_private,
)


GIB = 1024**3
MIB = 1024**2


def _settings(base: Settings, **changes: object) -> Settings:
    return Settings(**dict(base.model_dump(), **changes))


def _owned(path: Path, value: object) -> Path:
    return write_private(path, value if isinstance(value, bytes) else exact_json(value))


def _policy() -> MineruResultStoragePolicy:
    return MineruResultStoragePolicy(
        contract_version="mineru.result-storage-policy.v1",
        native_volume_identity="synthetic-native-output", native_volume_total_bytes=64*GIB,
        native_work_disk_limit_bytes=24*GIB, native_free_floor_bytes=GIB,
        native_source_pool_bytes=16*GIB, native_completion_escrow_bytes=6*GIB,
        native_metadata_reserve_bytes=GIB, native_source_single_limit_bytes=2*GIB,
        native_growing_producer_limit=1, native_result_hard_limit_bytes=4*GIB,
        native_normal_unacked_target_bytes=4*GIB, initial_result_estimate_bytes=128*MIB,
        native_allocation_unit_bytes=4096, native_file_overhead_bytes=4096,
        source_pdf_bytes_limit=2*GIB,
        mac_volume_identity="synthetic-mac-work", mac_volume_total_bytes=128*GIB,
        mac_work_disk_limit_bytes=64*GIB, mac_free_floor_bytes=GIB,
        mac_normal_output_target_bytes=4*GIB, mac_decode_input_limit_bytes=128*MIB,
        mac_decode_working_set_budget_bytes=4*GIB, mac_decode_expansion_factor=32,
        mac_decode_stage_seconds=1800, max_members=4096, max_name_bytes=256,
        max_inventory_bytes=64*MIB, transfer_logical_deadline_seconds=24*3600,
        progress_window_seconds=60, minimum_progress_bytes=16*MIB,
    )


def _storage_health(old: dict, *, capacity: MineruCapacityConfigV2) -> dict:
    value = copy.deepcopy(old)
    runtime = value["task_protocol_runtime"]
    runtime.update(schema="mineru-task-runtime.v4", registry_schema="mineru-task-registry.v4",
                   capacity_config_sha256=capacity.sha256,
                   result_storage_policy_sha256=capacity.result_storage.sha256)
    runtime.pop("task_result_reservation_bytes")
    runtime.pop("max_unacked_result_bytes")
    admission = value["task_admission"]
    admission["registry_schema"] = "mineru-task-registry.v4"
    observation = value["capacity_observation"]
    observation.update(schema="mineru.capacity-observation.v2",
                       capacity_config_sha256=capacity.sha256,
                       result_storage={
                           "policy_sha256": capacity.result_storage.sha256,
                           "source_bytes": 0, "ingress_bytes": 0, "result_bytes": 0,
                           "growing_producers": 0, "outstanding_promise_bytes": 0,
                           "completion_queue_depth": 0,
                           "waiting_tasks": {"source_growth_capacity": 0,
                                             "completion_capacity": 0, "free_floor": 0},
                           "blocked_tasks": 0,
                       })
    limits = observation["resolved_limits"]
    limits.pop("result_reservation_bytes")
    limits.pop("max_unacked_result_bytes")
    observation["stage_counters"].update(source_growth_waiting=0, completion_waiting=0)
    return value


def _receipt(old: dict, *, manifest: dict, capacity: MineruCapacityConfigV2) -> dict:
    value = copy.deepcopy(old)
    runtime = runtime_identity(manifest)
    value["runtime_manifest"] = copy.deepcopy(manifest)
    value["identity"].update(
        local_writer_code_sha256=manifest["client"]["writer_code_sha256"],
        runtime_manifest_identity_sha256=runtime,
        orchestrator_runtime_identity_sha256=runtime_identity(manifest["orchestrator"]),
    )
    value["canary"]["runtime_bundle_identity_sha256"] = runtime
    value["provider"]["target_identity"]["runtime_bundle_identity_sha256"] = runtime
    value["diagnostic_disposal"]["runtime_bundle_identity_sha256"] = runtime
    for side in ("before", "after"):
        value["orchestrator"][side] = _storage_health(value["orchestrator"][side], capacity=capacity)
    return value


def _shift_receipt_times(value: object, *, delta) -> object:
    """Move the synthetic qualification observation window intact to `now`."""
    if isinstance(value, list):
        return [_shift_receipt_times(item, delta=delta) for item in value]
    if not isinstance(value, dict):
        return value
    moved = {}
    for key, item in value.items():
        if key in {"started_at_utc", "finished_at_utc", "passed_at_utc", "created_at_utc"}:
            moved[key] = (datetime.fromisoformat(item) + delta).isoformat()
        else:
            moved[key] = _shift_receipt_times(item, delta=delta)
    return moved


@dataclass(frozen=True)
class SyntheticQnewFlow:
    origin: ParentQ0
    settings: Settings
    capacity: MineruCapacityConfigV2
    process_profile: MineruProcessProfile
    origin_release: Path
    origin_bundle: Path
    target_release: Path
    target_bundle: Path
    target_runtime: str
    target_profile_file: Path
    target_activation: Path
    inventory_file: Path
    lookup_file: Path
    proposal_file: Path
    review_file: Path

    def checker(self, *, now: datetime) -> gate.MinerUDeploymentChecker:
        # Only the local client metadata port is replayed from the fixture's
        # closed receipt. The real exact qualification and upgrade verifier run.
        with (
            mock.patch.dict(
                "os.environ", {"DISCLOSURE_V4_PROCESS_PROFILE_FILE": str(self.target_profile_file),
                               "DISCLOSURE_V4_PROCESS_PROFILE_SHA256": self.process_profile.sha256,
                               "DISCLOSURE_V4_ARCHIVE_MEMBER_COUNT_LIMIT": str(ARCHIVE_MEMBER_COUNT_LIMIT)},
            ),
            mock.patch.object(gate, "client_bundle_identity", return_value=self.origin.client),
            mock.patch.object(upgrade_runtime, "client_bundle_identity", return_value=self.origin.client),
            frozen_wall_clock(now),
        ):
            return gate.MinerUDeploymentChecker(
                self.settings, parse_enabled=True, process_profile=self.process_profile,
                expected_capacity=self.capacity, accept_execution_upgrade=True,
            )


def build_synthetic_qnew_flow(test, *, inventory: LegacyScopeInventory,
                              old_key_transport: httpx.BaseTransport,
                              now: datetime, origin: ParentQ0 | None = None,
                              base_settings: Settings | None = None) -> SyntheticQnewFlow:
    """Build bytes from one v1 E7 origin and the actual current release.

    The caller supplies real scratch-captured prepared members for integration,
    or synthetic members for an offline checker test. Old-key GETs use the
    supplied scripted transport and the product capture function.
    """
    origin = origin or synthetic_parent_q0(test)
    root = origin.root
    old_capacity = json.loads(origin.capacity_path.read_bytes())
    capacity = MineruCapacityConfigV2(**{
        **{key: value for key, value in old_capacity.items()
           if key not in {"result_reservation_bytes", "max_unacked_result_bytes"}},
        "contract_version": "mineru.capacity-config.v2", "result_storage": _policy(),
    })
    capacity_file = _owned(root / "capacity-qnew.json", capacity.exact_bytes)
    writer = writer_code_digest()
    manifest = copy.deepcopy(origin.manifest)
    manifest["client"]["writer_code_sha256"] = writer
    orch = manifest["orchestrator"]
    orch.update(
        capacity_config=json.loads(capacity.exact_bytes),
        capacity_config_sha256=capacity.sha256,
        capacity_runtime_compatibility_sha256=sha256_bytes(b"synthetic storage runtime"),
        service_config_sha256=sha256_bytes(b"synthetic storage service"),
        container_image_digest=sha256_bytes(b"synthetic storage native image"),
        task_result_reservation_bytes=None, max_unacked_result_bytes=None,
    )
    target_runtime = runtime_identity(manifest)
    release = upgrade_runtime.build_execution_release_manifest(
        source_revision="synthetic-qnew"
    )
    target_release = _owned(root / "release-qnew.json", encode_execution_release_manifest(release))
    origin_release_object = replace(
        release, source_revision="synthetic-e7", writer_code_sha256=origin.historical_writer,
    )
    origin_release = _owned(root / "release-e7.json", encode_execution_release_manifest(origin_release_object))
    origin_bundle = _owned(root / "runtime-e7.json",
                           {"identity_sha256": origin.runtime_identity, "manifest": origin.manifest})
    target_bundle = _owned(root / "runtime-qnew.json",
                           {"identity_sha256": target_runtime, "manifest": manifest})
    profile = replace(
        origin.process_profile, contract_version=RESULT_STORAGE_PROCESS_PROFILE_CONTRACT,
        runtime_bundle_identity_sha256=target_runtime,
        orchestrator_image_identity_sha256=orch["container_image_digest"],
        result_reservation_bytes=None, max_unacked_result_bytes=None,
        result_storage_policy_sha256=capacity.result_storage.sha256,
        decoded_payload_bytes_limit=capacity.result_storage.mac_decode_input_limit_bytes,
    )
    profile_file = _owned(root / "profile-qnew.json", profile.exact_bytes)
    activation = copy.deepcopy(origin.activation)
    activation.update(runtime_identity_sha256=target_runtime, capacity_config_sha256=capacity.sha256)
    activation["policy"]["runtime_identity_sha256"] = target_runtime
    activation_file = _owned(root / "activation-qnew.json", activation)
    smoke = json.loads(origin.smoke_path.read_bytes())
    heldout = json.loads(origin.heldout_path.read_bytes())
    delta = now - origin.clock
    new_smoke = _shift_receipt_times(
        _receipt(smoke, manifest=manifest, capacity=capacity), delta=delta,
    )
    new_validation = copy.deepcopy(heldout)
    for entry in new_validation["documents"]:
        entry["receipt"] = _shift_receipt_times(
            _receipt(entry["receipt"], manifest=manifest, capacity=capacity), delta=delta,
        )
        entry["receipt_sha256"] = runtime_identity(entry["receipt"])
    new_validation["created_at_utc"] = (
        datetime.fromisoformat(new_validation["created_at_utc"]) + delta
    ).isoformat()
    for key in ("epoch_before", "epoch_after"):
        entry = new_validation[key]
        epoch = entry["receipt"]["service_epoch"]
        epoch.update(
            runtime_manifest_identity_sha256=target_runtime,
            writer_code_sha256=writer,
            api_image_digest=orch["container_image_digest"],
        )
        entry["receipt"]["service_epoch_sha256"] = runtime_identity(epoch)
        entry["receipt"]["created_at_utc"] = (
            datetime.fromisoformat(entry["receipt"]["created_at_utc"]) + delta
        ).isoformat()
        entry["receipt_sha256"] = runtime_identity(entry["receipt"])
    smoke_file = _owned(root / "smoke-qnew.json", new_smoke)
    canary_file = _owned(root / "canary-qnew.json", new_smoke["canary"])
    validation_file = _owned(root / "heldout-qnew.json", new_validation)
    settings = _settings(
        base_settings or origin.settings, disclosure_mineru_capacity_config=capacity_file,
        disclosure_gpu_metrics_url=GPU_METRICS_URL,
        disclosure_mineru_capacity_config_sha256=capacity.sha256,
        disclosure_mineru_runtime_bundle_identity_sha256=target_runtime,
        disclosure_mineru_smoke_receipt=smoke_file,
        disclosure_mineru_canary_cache=canary_file,
        disclosure_mineru_validation_receipt=validation_file,
        disclosure_mineru_stream_pressure_config=activation_file,
        disclosure_mineru_stream_pressure_config_sha256=sha256_bytes(activation_file.read_bytes()),
    )
    inventory_file = _owned(root / "inventory-e7.json", encode_legacy_scope_inventory(inventory))
    lookup = upgrade_runtime.capture_legacy_key_lookups(
        origin.settings, inventory=inventory_file, key_ttl_seconds=86_400,
        transport=old_key_transport, wall_clock=lambda: now,
    )
    lookup_file = _owned(root / "key-lookups-e7.json", encode_legacy_key_lookup_evidence(lookup))
    # The proposal builder uses the real release, origin, capacity and file
    # readers. Only its staged-profile environment is supplied by this test.
    with mock.patch.dict(
        "os.environ", {"DISCLOSURE_V4_PROCESS_PROFILE_FILE": str(profile_file),
                       "DISCLOSURE_V4_PROCESS_PROFILE_SHA256": profile.sha256,
                       "DISCLOSURE_V4_ARCHIVE_MEMBER_COUNT_LIMIT": str(ARCHIVE_MEMBER_COUNT_LIMIT)},
    ):
        proposal = upgrade_runtime.build_qualified_runtime_upgrade_proposal(
            settings, release_manifest=target_release, runtime_bundle=target_bundle,
            origin_release_manifest=origin_release, origin_runtime_bundle=origin_bundle,
            origin_process_profile=origin.process_profile_path,
            origin_activation=origin.activation_path, inventory=inventory_file,
            key_lookups=lookup_file,
            exact_change_manifest_sha256=sha256_bytes(b"synthetic reviewed changes"),
            independent_test_evidence_sha256=sha256_bytes(b"synthetic independent flow"),
            independent_code_review_sha256=sha256_bytes(b"synthetic code review"),
        )
    proposal_file = _owned(root / "proposal-qnew.json", encode_qualified_runtime_upgrade(proposal))
    review_file = _owned(root / "review-qnew.json", {
        "contract_version": "worker-local-execution-upgrade-review.v1",
        "verdict": "GO", "proposal_sha256": sha256_bytes(proposal_file.read_bytes()),
        "reviewer_reference": "synthetic-independent-review",
        "decision_reference": "synthetic-independent-decision",
    })
    settings = _settings(
        settings, disclosure_worker_execution_upgrade_file=proposal_file,
        disclosure_worker_execution_upgrade_sha256=sha256_bytes(proposal_file.read_bytes()),
        disclosure_worker_execution_upgrade_review_file=review_file,
        disclosure_worker_execution_upgrade_review_sha256=sha256_bytes(review_file.read_bytes()),
    )
    return SyntheticQnewFlow(
        origin, settings, capacity, profile, origin_release, origin_bundle,
        target_release, target_bundle, target_runtime, profile_file,
        activation_file, inventory_file, lookup_file, proposal_file, review_file,
    )
