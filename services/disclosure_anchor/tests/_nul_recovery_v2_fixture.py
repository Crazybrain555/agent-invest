"""Independent Q0/E1/E2 files; only the existing v1 fixture's primitives are reused.

The v2 relation and archived origin are hand-encoded, never built by the product.
Archived release members deliberately differ from the current tree. This fixture
therefore tests historical pin verification as well as the distinct origin role.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, fields, replace
from typing import Any

from disclosure_anchor.application.contracts.mineru_process_profile import MineruProcessProfile
from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4
from tests._f5_upgrade_q0_fixture import ParentQ0, sha256_bytes
from tests._f5_upgrade_u01_fixture import U01Bundle, build_u01, exact_json, runtime_identity


@dataclass(frozen=True)
class RecoveryBundle(U01Bundle):
    origin_profile: MineruProcessProfile
    origin_worker: StagedWorkerProfileV4


def build_recovery_v2(parent: ParentQ0, *, writer_moved: bool = False) -> RecoveryBundle:
    target = build_u01(parent, members=[], source_revision="independent-recovery-E2")
    writer = sha256_bytes(b"archived E1 writer") if writer_moved else target.current_writer
    manifest = copy.deepcopy(target.current_manifest)
    manifest["client"]["writer_code_sha256"] = writer
    runtime = runtime_identity(manifest)
    runtime_path = target._file("archived-runtime-E1", exact_json({"identity_sha256": runtime, "manifest": manifest}))
    profile = replace(target.process_profile, runtime_bundle_identity_sha256=runtime)
    profile_path = target._file("archived-profile-E1", profile.exact_bytes)
    worker = replace(target.worker_profile, process_profile_sha256=profile.sha256)
    activation = copy.deepcopy(target.activation)
    activation["runtime_identity_sha256"] = runtime
    activation["policy"]["runtime_identity_sha256"] = runtime
    activation_path = target._file("archived-activation-E1", exact_json(activation))
    release = copy.deepcopy(target.release)
    release.update(source_revision="independent-archived-E1", writer_code_sha256=writer)
    release["files"][0]["sha256"] = sha256_bytes(b"actual archived E1 file bytes")
    release["files"][0]["bytes"] = len(b"actual archived E1 file bytes")
    release_path = target._file("archived-release-E1", exact_json(release))
    origin = {
        "release_manifest_file": str(release_path), "release_manifest_sha256": sha256_bytes(release_path.read_bytes()),
        "source_revision": release["source_revision"], "writer_code_sha256": writer,
        "runtime_bundle_file": str(runtime_path), "runtime_bundle_sha256": sha256_bytes(runtime_path.read_bytes()),
        "runtime_identity_sha256": runtime, "process_profile_file": str(profile_path),
        "process_profile_sha256": profile.sha256, "worker_profile_sha256": worker.sha256,
        "capacity_config_sha256": parent.capacity_sha256, "stream_activation_file": str(activation_path),
        "stream_activation_sha256": sha256_bytes(activation_path.read_bytes()),
    }

    def relation(proposal: dict[str, Any]) -> None:
        proposal["contract_version"] = "worker-local-execution-upgrade.v2"
        proposal["qualification_anchor"] = proposal.pop("parent_qualification")
        proposal["recovery_origin"] = origin

    target = target.with_proposal(relation)
    return RecoveryBundle(**{item.name: getattr(target, item.name) for item in fields(U01Bundle)},
                          origin_profile=profile, origin_worker=worker)
