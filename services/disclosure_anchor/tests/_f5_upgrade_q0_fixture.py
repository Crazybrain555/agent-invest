"""Parent qualifications Q0 for the local-execution-upgrade acceptance.

Two parents, never confused:

* ``synthetic_parent_q0`` (default suite). An authored, internally consistent historical
  qualification composed from the existing v11 Package A builder (``gate_fixture``). It pins a
  synthetic historical writer, which differs from any real tree's digest. A hand-written parent
  stream activation A0 records the same native owner as its held-out receipts. It is a contract
  sample, not production output.
* ``actual_parent_q0`` (explicit opt-in). The byte-exact historical qualification the pre-F5
  worker ran under. It stays outside the source tree (repository boundary 5) and is read from
  ``F5_UPGRADE_ACTUAL_Q0_ROOT``, with every file hash checked. Without the variable the actual
  proof is skipped, with that exact reason.

Each parent is installed as owner-only 0600 copies under a resolved temporary root, because the
real evidence, capacity and activation loaders require that mode and refuse symlinked
components. The only external port a caller may replay is the MinerU client metadata, and only
from the parent's own closed client section.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any
import unittest

from disclosure_anchor.adapters.runtime.mineru_identity import MinerUClientIdentity
from disclosure_anchor.application.contracts.mineru_process_profile import (
    MineruProcessProfile,
    decode_mineru_process_profile,
)
from disclosure_anchor.application.contracts.staged_worker_profile_v4 import (
    StagedWorkerProfileV4,
    decode_staged_worker_profile_v4,
)
from disclosure_anchor.settings import Settings
from tests._mineru_package_a_fixture import NOW as SYNTHETIC_NOW, gate_fixture


ACTUAL_Q0_ENV = "F5_UPGRADE_ACTUAL_Q0_ROOT"
# The actual parent's exact files (sha256 of bytes), recorded when they were copied out of the
# production qualification and bind directories (see the external root's README).
ACTUAL_SHA256 = {
    "smoke.json": "3e484bfb70d857733c4864034b2083c185233a4fcbf8173770ca0e28f07c57f0",
    "canary-cache.json": "d199e46b8cc07b7a8ae0253babd5fb41c8ceba83e0daa6c6ea168aa096dfa79b",
    "heldout.json": "f8306a576fee2f2a0c292ec154d6dcc3e4e1eb0d85caa2243903c0c90157270f",
    "capacity-config.json": "e56d1c8bd286906c83f81bc25339c7ef0eeb47a78d3f117e5f583529c6abd0ee",
    "process-profile-P0.json": "21f0c54881a84aad54f5d87a1aeb1df499f7cf3b4f87dda866b3ae56d245bb0c",
    "worker-profile-WP0.json": "0c205fe3f593a2c56fc2866a78522d4835b28be7ba4d7c00d71315025dd7e39f",
    "activation-A0.json": "01c8b77b5ddb929b9b2360495f8dffdcb955640edd4c317089d2d2c72f0cb1fe",
}
ACTUAL_W0 = "sha256:84030edab8a7bc890c45ced36443f565326f84f04a377d7a9f6b0300c919e5fa"
ACTUAL_R0 = "sha256:b3525488854f0483a64c89e6562f2c648d3085cd0dabba3ee5ba0afe87e41af3"
ACTUAL_CANARY_PASSED_AT = datetime(2026, 9, 23, 11, 47, 41, 554564, tzinfo=UTC)
# A fixed checker clock inside the actual parent's original freshness window.
ACTUAL_CLOCK = datetime(2026, 9, 24, 19, 0, tzinfo=UTC)
API_URL = "http://127.0.0.1:30002"
OBSERVABILITY_URL = "http://127.0.0.1:30001/v1"
INFERENCE_URL = "http://mineru-openai-server:30000/v1"
# The literal legacy writer membership recorded by Pro's static recalculation. The legacy digest
# and these 42 members stay exactly as they were, so a parent's pinned writer stays checkable.
LEGACY_WRITER_MEMBERS = (
    "scripts/build_mineru_validation_receipt.py",
    "scripts/collect_mineru_phase_trace.py",
    "scripts/freeze_mineru_campaign_epoch.py",
    "scripts/mineru_smoke.py",
    "src/disclosure_anchor/adapters/parsers/mineru_medium/artifacts.py",
    "src/disclosure_anchor/adapters/parsers/mineru_medium/http_staged.py",
    "src/disclosure_anchor/adapters/parsers/mineru_medium/protocol_v2_wire.py",
    "src/disclosure_anchor/adapters/parsers/mineru_medium/parser.py",
    "src/disclosure_anchor/adapters/parsers/mineru_medium/process.py",
    "src/disclosure_anchor/adapters/runtime/bounded_http.py",
    "src/disclosure_anchor/adapters/runtime/mineru_canary.py",
    "src/disclosure_anchor/adapters/runtime/mineru_deployment_gate.py",
    "src/disclosure_anchor/adapters/runtime/mineru_diagnostic.py",
    "src/disclosure_anchor/application/ports/staged_provider_parser.py",
    "src/disclosure_anchor/adapters/runtime/mineru_identity.py",
    "src/disclosure_anchor/adapters/runtime/mineru_orchestrator.py",
    "src/disclosure_anchor/adapters/runtime/mineru_capacity_config.py",
    "src/disclosure_anchor/adapters/runtime/mineru_capacity_file.py",
    "src/disclosure_anchor/application/contracts/mineru_capacity_config.py",
    "src/disclosure_anchor/application/contracts/mineru_capacity_health.py",
    "src/disclosure_anchor/adapters/runtime/mineru_process_isolation.py",
    "src/disclosure_anchor/adapters/storage/provider_document_source.py",
    "src/disclosure_anchor/application/ports/parser.py",
    "src/disclosure_anchor/application/contracts/mineru_api_health.py",
    "src/disclosure_anchor/application/contracts/strict_json.py",
    "src/disclosure_anchor/cli/staged_commission.py",
    "src/disclosure_anchor/cli/staged_recover.py",
    "src/disclosure_anchor/adapters/runtime/mineru_recovery_gate.py",
    "src/disclosure_anchor/application/services/atomic_publication_request_builder_v4.py",
    "src/disclosure_anchor/application/contracts/atomic_document_publication_v4.py",
    "src/disclosure_anchor/application/contracts/local_materialization_manifest_v4.py",
    "src/disclosure_anchor/application/contracts/provider_document_envelope.py",
    "src/disclosure_anchor/adapters/db/postgres/staged_recovery_scope_v4.py",
    "src/disclosure_anchor/adapters/runtime/staged_worker_v4.py",
    "src/disclosure_anchor/adapters/db/postgres/staged_new_work_v4.py",
    "src/disclosure_anchor/application/worker/queries.py",
    "src/disclosure_anchor/application/ports/staged_new_work_v4.py",
    "src/disclosure_anchor/application/services/staged_new_work_admission_v4.py",
    "src/disclosure_anchor/adapters/parsers/mineru_medium/http_remote_v4.py",
    "src/disclosure_anchor/adapters/parsers/mineru_medium/http_staged_v4.py",
    "src/disclosure_anchor/adapters/parsers/mineru_medium/v4_initial_ingress.py",
    "src/disclosure_anchor/adapters/parsers/mineru_medium/v4_stage_input_resolver.py",
)
SERVICE_ROOT = Path(__file__).resolve().parents[1]


def legacy_writer_digest(service_root: Path = SERVICE_ROOT) -> str:
    """Independent recomputation of the legacy digest (Pro's recorded algorithm)."""

    digest = hashlib.sha256()
    for relpath in LEGACY_WRITER_MEMBERS:
        payload = (service_root / relpath).read_bytes()
        digest.update(relpath.encode("utf-8") + b"\0" + str(len(payload)).encode("ascii") + b"\0")
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ParentQ0:
    """One installed parent qualification plus the settings that name it."""

    label: str
    root: Path
    settings: Settings
    process_profile: MineruProcessProfile
    process_profile_path: Path
    worker_profile: StagedWorkerProfileV4
    activation: dict[str, Any]
    activation_path: Path
    client: MinerUClientIdentity
    manifest: dict[str, Any]
    capacity_path: Path
    capacity_sha256: str
    smoke_path: Path
    canary_path: Path
    heldout_path: Path
    historical_writer: str
    runtime_identity: str
    canary_passed_at: datetime
    canary_max_age_seconds: int
    clock: datetime
    task_slots: int

    @property
    def process_profile_exact_bytes(self) -> bytes:
        return self.process_profile_path.read_bytes()

    @property
    def activation_exact_bytes(self) -> bytes:
        return self.activation_path.read_bytes()

    def file_sha256(self, path: Path) -> str:
        return sha256_bytes(path.read_bytes())


def _private(path: Path, raw: bytes) -> Path:
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def _root(test: unittest.TestCase, prefix: str) -> Path:
    root = Path(tempfile.mkdtemp(prefix=prefix, dir=Path(tempfile.gettempdir()).resolve()))
    test.addCleanup(shutil.rmtree, root, True)
    return root


def _owner(receipt: dict[str, Any]) -> dict[str, Any]:
    return receipt["orchestrator"]["before"]["capacity_observation"]["owner"]


def synthetic_parent_q0(test: unittest.TestCase) -> ParentQ0:
    """The authored historical qualification for the default suite (Package A + activation)."""

    root = _root(test, "f5-synthetic-q0-")
    settings, profile, capacity, client = gate_fixture(root)
    smoke_path = settings.disclosure_mineru_smoke_receipt
    canary_path = settings.disclosure_mineru_canary_cache
    heldout_path = settings.disclosure_mineru_validation_receipt
    capacity_path = settings.disclosure_mineru_capacity_config
    assert smoke_path and canary_path and heldout_path and capacity_path
    smoke = json.loads(smoke_path.read_bytes())
    manifest = smoke["runtime_manifest"]
    runtime = settings.disclosure_mineru_runtime_bundle_identity_sha256
    assert runtime is not None
    owner = _owner(smoke)
    activation = {
        "schema": "mineru.stream-activation.v1",
        "runtime_identity_sha256": runtime,
        "capacity_config_sha256": capacity.sha256,
        "owner": dict(owner),
        "cgroup_identity_sha256": "sha256:" + "3" * 64,
        "cgroup_max_bytes": 32 * 1024**3,
        "gpu_uuid": "GPU-12345678-abcd-4321-9876-abcdef123456",
        "api_max_age_seconds": 3.0,
        "gpu_max_age_seconds": 8.0,
        "policy": {
            "qualified_max": profile.api_task_slots,
            "runtime_identity_sha256": runtime,
            "owner_identity_sha256": sha256_bytes(canonical_json(owner)),
            "gpu_pause_bytes": 512 * 1024**2,
            "gpu_reduce_bytes": 1024**3,
            "gpu_recover_bytes": 3 * 512 * 1024**2,
            "host_pause_bytes": 4 * 1024**3,
            "host_recover_bytes": 6 * 1024**3,
            "sample_max_age_seconds": 8.0,
            "missing_pause_seconds": 10.0,
            "recovery_seconds": 10.0,
            "reduction_interval_seconds": 2.0,
        },
    }
    process_path = _private(root / "process-profile-P0.json", profile.exact_bytes)
    activation_path = _private(root / "activation-A0.json", canonical_json(activation))
    worker = StagedWorkerProfileV4(
        process_profile_sha256=profile.sha256,
        mac_preflight_workers=settings.worker_parse_concurrency,
        mac_finalize_workers=settings.worker_finalize_concurrency,
        contract_version="staged-worker-composition.v2",
        commit_stage_seconds=3600,
    )
    passed = datetime.fromisoformat(json.loads(canary_path.read_bytes())["passed_at_utc"])
    return ParentQ0(
        label="synthetic", root=root, settings=settings, process_profile=profile,
        process_profile_path=process_path, worker_profile=worker, activation=activation,
        activation_path=activation_path, client=client, manifest=manifest,
        capacity_path=capacity_path, capacity_sha256=capacity.sha256, smoke_path=smoke_path,
        canary_path=canary_path, heldout_path=heldout_path,
        historical_writer=manifest["client"]["writer_code_sha256"], runtime_identity=runtime,
        canary_passed_at=passed, canary_max_age_seconds=settings.disclosure_mineru_canary_max_age_seconds,
        clock=SYNTHETIC_NOW, task_slots=profile.api_task_slots,
    )


def actual_parent_q0(test: unittest.TestCase) -> ParentQ0:
    """The byte-exact historical qualification, from an explicit external root (opt-in)."""

    source = os.environ.get(ACTUAL_Q0_ENV)
    if not source:
        test.skipTest(f"{ACTUAL_Q0_ENV} is not set: the actual parent Q0 stays outside Git and is opt-in")
    source_root = Path(source)
    for name, expected in ACTUAL_SHA256.items():
        observed = hashlib.sha256((source_root / name).read_bytes()).hexdigest()
        if observed != expected:
            test.fail(f"{ACTUAL_Q0_ENV}/{name} is not the recorded actual parent ({observed})")
    root = _root(test, "f5-actual-q0-")
    copies = {name: _private(root / name, (source_root / name).read_bytes()) for name in ACTUAL_SHA256}
    for directory in ("service/runtime", "shared"):
        (root / directory).mkdir(parents=True)
    mineru = root / "mineru"
    mineru.write_text("client metadata is replayed from the parent's closed client section\n")
    smoke = json.loads(copies["smoke.json"].read_bytes())
    manifest = smoke["runtime_manifest"]
    client = MinerUClientIdentity(
        package_set_sha256=manifest["client"]["package_set_sha256"],
        python_version="unrecorded-in-q0",
        content_package_versions=dict(smoke["identity"]["local_content_package_versions"]),
    )
    profile = decode_mineru_process_profile(copies["process-profile-P0.json"].read_bytes())
    worker = decode_staged_worker_profile_v4(copies["worker-profile-WP0.json"].read_bytes())
    capacity = json.loads(copies["capacity-config.json"].read_bytes())
    settings = Settings(
        disclosure_data_root=root / "service",
        disclosure_shared_root=root / "shared",
        disclosure_runtime_root=root / "service" / "runtime",
        mineru_model_cache=root / "shared" / "mineru-cache",
        hf_home=root / "shared" / "hf",
        modelscope_cache=root / "shared" / "modelscope",
        worker_parse_execution_mode="staged-v4",
        mineru_processing_window_size=capacity["processing_window_size"],
        disclosure_mineru_bin=mineru,
        disclosure_mineru_api_url=API_URL,
        disclosure_mineru_observability_url=OBSERVABILITY_URL,
        disclosure_mineru_inference_upstream_url=INFERENCE_URL,
        disclosure_mineru_runtime_bundle_identity_sha256=smoke["identity"]["runtime_manifest_identity_sha256"],
        disclosure_mineru_smoke_receipt=copies["smoke.json"],
        disclosure_mineru_canary_cache=copies["canary-cache.json"],
        disclosure_mineru_validation_receipt=copies["heldout.json"],
        disclosure_mineru_capacity_config=copies["capacity-config.json"],
        disclosure_mineru_capacity_config_sha256="sha256:" + ACTUAL_SHA256["capacity-config.json"],
        disclosure_mineru_api_task_slots=capacity["parse_active_limit"],
        disclosure_mineru_api_inference_concurrency=capacity["final_http_limit_per_loop"],
        worker_gpu_request_budget=capacity["final_http_limit_per_loop"],
        worker_gpu_max_sequences=profile.vllm_max_num_seqs,
        worker_parse_concurrency=worker.mac_preflight_workers,
        worker_finalize_concurrency=worker.mac_finalize_workers,
        worker_mineru_client_outstanding_window=capacity["parse_active_limit"],
    )
    return ParentQ0(
        label="actual", root=root, settings=settings, process_profile=profile,
        process_profile_path=copies["process-profile-P0.json"], worker_profile=worker,
        activation=json.loads(copies["activation-A0.json"].read_bytes()),
        activation_path=copies["activation-A0.json"], client=client, manifest=manifest,
        capacity_path=copies["capacity-config.json"],
        capacity_sha256="sha256:" + ACTUAL_SHA256["capacity-config.json"],
        smoke_path=copies["smoke.json"], canary_path=copies["canary-cache.json"],
        heldout_path=copies["heldout.json"], historical_writer=ACTUAL_W0, runtime_identity=ACTUAL_R0,
        canary_passed_at=ACTUAL_CANARY_PASSED_AT, canary_max_age_seconds=2592000, clock=ACTUAL_CLOCK,
        task_slots=capacity["parse_active_limit"],
    )


def stale_clock(parent: ParentQ0) -> datetime:
    return parent.canary_passed_at + timedelta(seconds=parent.canary_max_age_seconds + 1)
