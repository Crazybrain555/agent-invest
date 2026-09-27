"""Verify one reviewed local execution upgrade (U01) and build its artifacts.

This is the single compatibility verifier behind ``verify_mineru_deployment_gate``
when U01 is configured. It is also the read-only deployment preflight shared
by the worker CLI, doctor and installer, plus the builders that root runs
after code freeze. Nothing here writes business state, claims work, starts a
model, POSTs a task or records an operational stop.

Verification order (all must hold; nothing falls back to the exact path):

1. the pinned proposal and its GO review;
2. E1: every loaded source byte, the recomputed writer W1 and the worker
   interpreter's packages;
3. the current runtime M1/R1 against the measured client, W1 and the explicit
   v11 capacity;
4. the immutable parent Q0 through the existing smoke/canary/held-out verifier
   with a named parent expectation (R0, W0, P0), with real ages from the
   original dates;
5. M0 == M1 except the local writer; P0 -> P1 and WP0 -> WP1 move only their
   references; A0 -> A1 only its runtime, with an owner equal to Q0's
   held-out owners;
6. the pinned legacy inventory, whose members all bind R0/P0/WP0.

A v2 proposal runs the same steps against its target (E2) and its
``qualification_anchor`` Q0, where the writer may also stay unchanged. It then
verifies its archived recovery origin (E1) only against the origin's own pins:
the origin runtime, process profile, worker profile and activation map to the
target by the same reference-only moves. Its inventory members all bind the
origin R1/P1/WP1. No older upgrade is read.

A qualified runtime upgrade (``newly_qualified_result_runtime``) is a separate
branch: the target runtime passes the exact deployment path under its own new
qualification Qnew (the configured smoke/canary/held-out files must be the
proposal's), the archived origin maps to the target only across the closed
result-storage axes, and every inventory member is a never-submitted prepared
obligation whose original key was looked up absent on the origin API within
the key lifetime. It returns an ``exact`` deployment carrying the legacy scope.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import copy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import importlib.machinery
import importlib.metadata
import os
from pathlib import Path
import platform
import stat
from typing import Any, cast

import httpx
from sqlalchemy.engine import Engine

from disclosure_anchor.adapters.db.postgres.staged_upgrade_scope_v4 import (
    LegacyScopeObservation,
    observe_unresolved_heads,
    read_only_repository,
    require_post_capture_head,
    require_legacy_scope,
    verify_legacy_scope,
)
from disclosure_anchor.adapters.runtime.exact_file_write import publish_new_exact, write_new_exact
from disclosure_anchor.adapters.runtime.mineru_capacity_config import configured_mineru_capacity
from disclosure_anchor.adapters.runtime.mineru_capacity_file import read_mineru_capacity_file
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import (
    _MAX_EVIDENCE_BYTES,
    _MAX_VALIDATION_EVIDENCE_BYTES,
    MinerUDeploymentChecker,
    MinerUDeploymentGateError,
    ParentQualificationExpectation,
    VerifiedMinerUDeployment,
    _verify_configured_capacity,
    _verify_mineru_deployment_evidence,
    _verify_staged_profile_manifest,
    read_owner_only_evidence,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    canonical_payload_sha256,
    client_bundle_identity,
    verify_runtime_manifest_payload,
    writer_code_digest,
)
from disclosure_anchor.adapters.runtime.mineru_process_profile import load_mineru_process_profile
from disclosure_anchor.adapters.runtime.mineru_stream_activation import load_mineru_stream_activation
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.application.contracts.closed_document import canonical_bytes, sha256_of
from disclosure_anchor.application.contracts.mineru_capacity_config import (
    AnyMineruCapacityConfig,
    MineruCapacityConfigV2,
)
from disclosure_anchor.application.contracts.mineru_process_profile import MineruProcessProfile
from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    MAX_INVENTORY_BYTES,
    MAX_KEY_LOOKUP_BYTES,
    MAX_RELEASE_MANIFEST_BYTES,
    MAX_REVIEW_BYTES,
    MAX_UPGRADE_BYTES,
    QUALIFIED_UPGRADE_CONTRACT,
    UPGRADE_CONTRACT,
    UPGRADE_CONTRACT_V2,
    CompatibilityBasis,
    CurrentExecution,
    ExecutionReleaseManifest,
    LegacyScopeReference,
    LocalExecutionUpgrade,
    LocalExecutionUpgradeV2,
    KeyLookupEvidenceReference,
    LegacyKeyLookup,
    LegacyKeyLookupEvidence,
    ParentQualification,
    QualifiedRuntimeUpgrade,
    RecoveryOrigin,
    ReleaseFile,
    VerifiedQualifiedExecution,
    decode_execution_release_manifest,
    decode_execution_upgrade,
    decode_legacy_key_lookup_evidence,
    decode_legacy_scope_inventory,
    encode_legacy_key_lookup_evidence,
    decode_local_execution_upgrade_review,
    derive_parent_worker_profile,
    require_activation_mapping,
    require_computation_invariance,
    require_key_lookup_coverage,
    require_process_profile_mapping,
    require_result_capacity_change,
    require_result_process_profile_change,
    require_result_runtime_change,
    require_same_computation,
)
from disclosure_anchor.settings import Settings, load_staged_v4_settings


SERVICE_ROOT = Path(__file__).resolve().parents[4]
PREFLIGHT_CONTRACT = "worker-deployment-preflight.v1"
BOOT_RECEIPT_CONTRACT = "worker-execution-boot-receipt.v1"
BOOT_RECEIPT_V2_CONTRACT = "worker-execution-boot-receipt.v2"
BOOT_RECEIPT_QUALIFIED_CONTRACT = "worker-execution-boot-receipt.v3"
_RELEASE_TREES = ("src/disclosure_anchor", "scripts/launchd")
_RELEASE_FLAT_DIRECTORIES = ("scripts",)
_EXCLUDED_DIRECTORIES = frozenset({"__pycache__"})
_FLAT_KNOWN_DIRECTORIES = frozenset({"__pycache__", "launchd", "windows"})
_EXCLUDED_NAMES = frozenset({".DS_Store"})
_PREFLIGHT_DETAIL_LIMIT = 20


@contextmanager
def _upgrade_step(step: str) -> Iterator[None]:
    try:
        yield
    except MinerUDeploymentGateError:
        raise
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
        raise MinerUDeploymentGateError(f"local execution upgrade {step}: {exc}") from exc


def worker_python_identity() -> tuple[str, str]:
    """(python version, package-set identity) of the running interpreter."""

    packages = sorted(
        f"{dist.metadata['Name']}=={dist.version}"
        for dist in importlib.metadata.distributions()
        if dist.metadata["Name"]
    )
    version = platform.python_version()
    return version, canonical_payload_sha256({"python_version": version, "packages": packages})


def release_files(service_root: Path = SERVICE_ROOT) -> tuple[ReleaseFile, ...]:
    """Every regular file the worker can load, in path order.

    Scope is fixed: the whole ``src/disclosure_anchor`` package, the files
    directly under ``scripts/`` and ``scripts/launchd/``. Only ``__pycache__``
    directories (PEP 3147 caches, ignored by the import system without their
    source) and ``.DS_Store`` are excluded. Bytecode anywhere else is importable
    without a source (``SourcelessFileLoader``), so it is refused, as is a
    symlink or any non-regular entry; nothing is silently skipped.
    """

    found: dict[str, ReleaseFile] = {}

    def add(path: Path) -> None:
        relpath = path.relative_to(service_root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise MinerUDeploymentGateError(f"execution release refuses a non-regular entry: {relpath}")
        if path.suffix in importlib.machinery.BYTECODE_SUFFIXES:
            raise MinerUDeploymentGateError(
                f"execution release refuses sourceless bytecode outside __pycache__: {relpath}"
            )
        payload = path.read_bytes()
        found[relpath] = ReleaseFile(path=relpath, sha256=sha256_of(payload), bytes=len(payload))

    def included(name: str) -> bool:
        return name not in _EXCLUDED_NAMES

    for tree in _RELEASE_TREES:
        root = service_root / tree
        if root.is_symlink() or not root.is_dir():
            raise MinerUDeploymentGateError(f"execution release tree is missing or unsafe: {tree}")
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            for name in dirnames:
                if (Path(directory) / name).is_symlink():
                    raise MinerUDeploymentGateError(
                        f"execution release refuses a symlinked directory: {Path(directory) / name}"
                    )
            dirnames[:] = sorted(name for name in dirnames if name not in _EXCLUDED_DIRECTORIES)
            for name in sorted(filenames):
                if included(name):
                    add(Path(directory) / name)
    for flat in _RELEASE_FLAT_DIRECTORIES:
        root = service_root / flat
        if root.is_symlink() or not root.is_dir():
            raise MinerUDeploymentGateError(f"execution release directory is missing or unsafe: {flat}")
        for entry in sorted(root.iterdir()):
            if entry.is_symlink():
                raise MinerUDeploymentGateError(f"execution release refuses a symlink: {entry}")
            if entry.is_dir():
                # launchd/ is walked above; windows/ runs only on the Windows
                # node; __pycache__/ is the ignored cache. Anything else is an
                # unpinned executable surface and refuses.
                if entry.name not in _FLAT_KNOWN_DIRECTORIES:
                    raise MinerUDeploymentGateError(
                        f"execution release refuses an unknown directory: {entry.relative_to(service_root)}"
                    )
                continue
            if included(entry.name):
                add(entry)
    return tuple(found[name] for name in sorted(found))


def build_execution_release_manifest(
    *, source_revision: str, service_root: Path = SERVICE_ROOT,
) -> ExecutionReleaseManifest:
    python_version, package_set = worker_python_identity()
    return ExecutionReleaseManifest(
        source_revision=source_revision,
        writer_code_sha256=writer_code_digest(),
        worker_python_version=python_version,
        worker_package_set_sha256=package_set,
        files=release_files(service_root),
    )


def verify_execution_release(
    manifest: ExecutionReleaseManifest, *, service_root: Path = SERVICE_ROOT, name: str = "E1",
) -> None:
    """The actual local bytes, writer and packages are exactly the current release.

    ``name`` labels it in refusals: E1 for a v1 edge, the target release for v2.
    """

    actual = release_files(service_root)
    if actual != manifest.files:
        expected = {item.path: item for item in manifest.files}
        observed = {item.path: item for item in actual}
        missing = sorted(set(expected) - set(observed))
        extra = sorted(set(observed) - set(expected))
        changed = sorted(path for path in set(expected) & set(observed) if expected[path] != observed[path])
        raise MinerUDeploymentGateError(
            f"execution release differs from {name}: missing={len(missing)} extra={len(extra)} "
            f"changed={len(changed)} first={(missing + extra + changed)[:5]}"
        )
    if writer_code_digest() != manifest.writer_code_sha256:
        raise MinerUDeploymentGateError(f"execution release writer differs from {name}")
    if worker_python_identity() != (manifest.worker_python_version, manifest.worker_package_set_sha256):
        raise MinerUDeploymentGateError(f"worker interpreter packages differ from {name}")


def _read_pinned(path: Path | str, expected_sha256: str, *, label: str, max_bytes: int) -> bytes:
    payload, _identity = read_owner_only_evidence(Path(path), label=label, max_bytes=max_bytes)
    if sha256_of(payload) != expected_sha256:
        raise MinerUDeploymentGateError(f"{label} differs from its pinned sha256")
    return payload


def _json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    value = strict_json_loads(payload)
    if type(value) is not dict:
        raise MinerUDeploymentGateError(f"{label} root must be an object")
    return cast(dict[str, Any], value)


def _require_settings_path(value: Path | None, *, label: str) -> Path:
    if value is None:
        raise MinerUDeploymentGateError(f"{label} is required")
    return value


def _active_worker_profile(process_profile: MineruProcessProfile, settings: Settings) -> StagedWorkerProfileV4:
    return load_staged_v4_settings().worker_profile(
        process_profile_sha256=process_profile.sha256,
        mac_preflight_workers=settings.worker_parse_concurrency,
        mac_finalize_workers=settings.worker_finalize_concurrency,
    )


def _heldout_owners(validation: dict[str, Any], *, role: str = "parent") -> list[Any]:
    owners: list[Any] = []
    for entry in validation["documents"]:
        owner = entry["receipt"]["orchestrator"]["after"]["capacity_observation"]["owner"]
        if type(owner) is not dict:
            raise MinerUDeploymentGateError(f"{role} held-out receipt does not record the qualified API owner")
        owners.append(owner)
    if not owners:
        raise MinerUDeploymentGateError(f"{role} held-out receipt records no qualified API owner")
    return owners


@dataclass(frozen=True, slots=True)
class _UpgradeRoles:
    """Refusal wording of one contract version; every check is shared.

    v1 keeps its historical texts exactly (parent Q0, current E1); v2 names
    the qualification anchor and the target.
    """

    reference: str
    subject: str
    target: str
    release: str
    release_name: str
    release_step: str
    computation_step: str
    worker: str
    activation_step: str


_V1_ROLES = _UpgradeRoles(
    reference="parent", subject="parent qualification", target="current", release="E1", release_name="E1",
    release_step="release manifest", computation_step="computation identity", worker="current WP1",
    activation_step="stream activation",
)
_V2_ROLES = _UpgradeRoles(
    reference="qualification anchor", subject="qualification anchor", target="target",
    release="target release", release_name="the target release", release_step="target release manifest",
    computation_step="qualification anchor computation identity", worker="target worker profile",
    activation_step="qualification anchor stream activation",
)
_QUALIFIED_ROLES = _UpgradeRoles(
    reference="recovery origin", subject="new qualification", target="target",
    release="target release", release_name="the target release", release_step="target release manifest",
    computation_step="recovery origin result runtime", worker="target worker profile",
    activation_step="new qualification stream activation",
)


def _upgrade_roles(upgrade: object) -> _UpgradeRoles:
    if isinstance(upgrade, QualifiedRuntimeUpgrade):
        return _QUALIFIED_ROLES
    return _V2_ROLES if isinstance(upgrade, LocalExecutionUpgradeV2) else _V1_ROLES


def verify_local_execution_upgrade(
    settings: Settings,
    *,
    process_profile: MineruProcessProfile | None,
    expected_capacity: AnyMineruCapacityConfig | None,
    now: datetime | None,
) -> VerifiedMinerUDeployment:
    """The one compatibility verifier; returns the compatible-parent proof.

    v1 proves the edge Q0 -> E1, whose obligations were frozen under Q0. v2
    proves two direct relations, Q0 anchor -> target and recovery origin ->
    target; the anchor is re-verified as history exactly like a v1 parent, and
    the origin is read only from its archived files and their own pins. No
    older upgrade is consulted.
    """

    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    upgrade_file = settings.disclosure_worker_execution_upgrade_file
    upgrade_sha = settings.disclosure_worker_execution_upgrade_sha256
    review_file = settings.disclosure_worker_execution_upgrade_review_file
    review_sha = settings.disclosure_worker_execution_upgrade_review_sha256
    if upgrade_file is None or upgrade_sha is None or review_file is None or review_sha is None:
        raise MinerUDeploymentGateError(
            "local execution upgrade needs its proposal and review files with pinned SHA-256 together"
        )
    with _upgrade_step("proposal"):
        upgrade = decode_execution_upgrade(
            _read_pinned(upgrade_file, upgrade_sha, label="local execution upgrade", max_bytes=MAX_UPGRADE_BYTES)
        )
        review = decode_local_execution_upgrade_review(
            _read_pinned(review_file, review_sha, label="local execution upgrade review", max_bytes=MAX_REVIEW_BYTES)
        )
    if review.proposal_sha256 != upgrade_sha:
        raise MinerUDeploymentGateError("the GO review does not approve this exact upgrade proposal")
    if isinstance(upgrade, QualifiedRuntimeUpgrade):
        return _verify_qualified_runtime_upgrade(
            settings, upgrade=upgrade, upgrade_sha=upgrade_sha, review=review, review_sha=review_sha,
            process_profile=process_profile, expected_capacity=expected_capacity, now=current_time,
        )
    v2 = isinstance(upgrade, LocalExecutionUpgradeV2)
    roles = _V2_ROLES if v2 else _V1_ROLES
    parent = upgrade.qualification_anchor if isinstance(upgrade, LocalExecutionUpgradeV2) else upgrade.parent
    current = upgrade.current
    capacity = configured_mineru_capacity(settings, expected_capacity)
    if capacity is None:
        raise MinerUDeploymentGateError("local execution upgrade requires the explicit v11 capacity")
    if type(process_profile) is not MineruProcessProfile:
        raise MinerUDeploymentGateError("local execution upgrade requires the staged process profile")
    if (
        settings.disclosure_mineru_runtime_bundle_identity_sha256 != current.runtime_identity_sha256
        or process_profile.sha256 != current.process_profile_sha256
        or capacity.sha256 != current.capacity_config_sha256
        or settings.disclosure_mineru_stream_pressure_config_sha256 != current.stream_activation_sha256
    ):
        raise MinerUDeploymentGateError(
            "configured runtime, process profile, capacity or stream activation is not the upgrade's "
            f"{roles.target} execution"
        )
    mineru_bin = _require_settings_path(settings.disclosure_mineru_bin, label="DISCLOSURE_MINERU_BIN")

    # 2. The target (v1 E1): every loadable byte, the writer and the packages.
    with _upgrade_step(roles.release_step):
        release = decode_execution_release_manifest(_read_pinned(
            current.release_manifest_file, current.release_manifest_sha256,
            label="execution release manifest", max_bytes=MAX_RELEASE_MANIFEST_BYTES,
        ))
    if (release.writer_code_sha256, release.source_revision) != (
        current.writer_code_sha256, current.source_revision
    ):
        raise MinerUDeploymentGateError(f"execution release manifest is not the proposal's {roles.release}")
    verify_execution_release(release, name=roles.release_name)

    # 3. The target runtime: measured client, recomputed writer, explicit v11.
    with _upgrade_step(f"{roles.target} runtime"):
        wrapper = _json_object(_read_pinned(
            current.runtime_bundle_file, current.runtime_bundle_sha256,
            label=f"{roles.target} runtime bundle", max_bytes=_MAX_EVIDENCE_BYTES,
        ), label=f"{roles.target} runtime bundle")
        if set(wrapper) != {"identity_sha256", "manifest"}:
            raise MinerUDeploymentGateError(
                f"{roles.target} runtime bundle must carry exactly identity_sha256 and manifest"
            )
        current_manifest = verify_runtime_manifest_payload(
            wrapper,
            configured_identity=current.runtime_identity_sha256,
            local_client_identity=client_bundle_identity(mineru_bin),
            local_processing_window_size=settings.mineru_processing_window_size,
            local_writer_code_digest=current.writer_code_sha256,
            expected_capacity=capacity,
        ).manifest
    _verify_configured_capacity(settings, process_profile=process_profile, expected_capacity=capacity)
    _verify_staged_profile_manifest(process_profile, current_manifest, expected_capacity=capacity)

    # 4. Q0 (v1 parent, v2 qualification anchor), re-verified as history with
    # its real ages and its own P0.
    smoke_path = _require_settings_path(settings.disclosure_mineru_smoke_receipt, label="smoke receipt")
    cache_path = _require_settings_path(settings.disclosure_mineru_canary_cache, label="canary cache")
    validation_path = _require_settings_path(
        settings.disclosure_mineru_validation_receipt, label="held-out validation receipt",
    )
    pinned_q0 = (
        (smoke_path, parent.smoke_receipt_sha256, f"{roles.reference} smoke receipt", _MAX_EVIDENCE_BYTES),
        (cache_path, parent.canary_cache_sha256, f"{roles.reference} canary cache", _MAX_EVIDENCE_BYTES),
        (validation_path, parent.validation_receipt_sha256, f"{roles.reference} held-out receipt",
         _MAX_VALIDATION_EVIDENCE_BYTES),
    )
    before = [
        _read_pinned(path, sha, label=label, max_bytes=limit) for path, sha, label, limit in pinned_q0
    ]
    with _upgrade_step(f"{roles.reference} process profile"):
        parent_profile = load_mineru_process_profile(
            Path(parent.process_profile_file),
            expected_sha256=parent.process_profile_sha256,
            expected_owner_uid=os.getuid(),
        ).profile
        require_process_profile_mapping(
            parent_profile, process_profile,
            parent_runtime_sha256=parent.runtime_identity_sha256,
            current_runtime_sha256=current.runtime_identity_sha256,
            reference=roles.reference, target=roles.target,
        )
    historical = _verify_mineru_deployment_evidence(
        settings,
        parse_enabled=True,
        process_profile=parent_profile,
        now=current_time,
        historical_writer_digest=None,
        expected_capacity=capacity,
        parent=ParentQualificationExpectation(
            runtime_identity_sha256=parent.runtime_identity_sha256,
            writer_code_sha256=parent.writer_code_sha256,
        ),
    )
    if historical is None:
        raise MinerUDeploymentGateError(f"{roles.subject} was not verified")
    after = [
        _read_pinned(path, sha, label=label, max_bytes=limit) for path, sha, label, limit in pinned_q0
    ]
    if after != before:
        raise MinerUDeploymentGateError(f"{roles.subject} files changed during verification")
    with _upgrade_step(f"{roles.reference} facts"):
        smoke = _json_object(before[0], label=f"{roles.reference} smoke receipt")
        cache = _json_object(before[1], label=f"{roles.reference} canary cache")
        validation = _json_object(before[2], label=f"{roles.reference} held-out receipt")
        parent_manifest = smoke["runtime_manifest"]
        if canonical_payload_sha256(parent_manifest) != parent.runtime_identity_sha256:
            raise MinerUDeploymentGateError(f"{roles.reference} smoke runtime manifest is not R0")
        if cache.get("passed_at_utc") != parent.qualified_at_utc:
            raise MinerUDeploymentGateError(
                f"{roles.reference} qualified_at_utc is not the canary's original pass time"
            )
        if validation["epoch_after"]["receipt"]["service_epoch_sha256"] != parent.service_epoch_sha256:
            raise MinerUDeploymentGateError(f"{roles.reference} service epoch differs from the held-out receipt")
        qualified_owners = _heldout_owners(validation, role=roles.reference)

    # 5. Only the local writer and the references that name it may move. The
    # v1 edge must move the writer; a v2 target may keep Q0's writer.
    with _upgrade_step(roles.computation_step):
        if v2:
            require_computation_invariance(
                parent_manifest, current_manifest,
                reference_writer_sha256=parent.writer_code_sha256,
                target_writer_sha256=current.writer_code_sha256,
                reference=roles.reference,
            )
        else:
            require_same_computation(
                parent_manifest, current_manifest,
                parent_writer_sha256=parent.writer_code_sha256,
                current_writer_sha256=current.writer_code_sha256,
            )
    with _upgrade_step("worker profile"):
        current_worker = _active_worker_profile(process_profile, settings)
        if current_worker.sha256 != current.worker_profile_sha256:
            raise MinerUDeploymentGateError(f"composed worker profile is not the upgrade's {roles.worker}")
        parent_worker = derive_parent_worker_profile(
            current_worker, parent_process_profile_sha256=parent_profile.sha256,
        )
        if parent_worker.sha256 != parent.worker_profile_sha256:
            raise MinerUDeploymentGateError(
                f"{roles.target} worker profile differs from the {roles.reference} beyond the process-profile "
                "reference"
            )
    activation_path = _require_settings_path(
        settings.disclosure_mineru_stream_pressure_config, label="current stream activation",
    )
    uid = os.getuid()
    with _upgrade_step(roles.activation_step):
        parent_activation_raw = read_mineru_capacity_file(
            Path(parent.stream_activation_file), expected_sha256=parent.stream_activation_sha256,
            expected_owner_uid=uid,
        )
        current_activation_raw = read_mineru_capacity_file(
            activation_path, expected_sha256=current.stream_activation_sha256, expected_owner_uid=uid,
        )
        for path, sha, runtime in (
            (Path(parent.stream_activation_file), parent.stream_activation_sha256, parent.runtime_identity_sha256),
            (activation_path, current.stream_activation_sha256, current.runtime_identity_sha256),
        ):
            load_mineru_stream_activation(
                path, expected_sha256=sha, expected_owner_uid=uid, expected_capacity=capacity,
                expected_runtime_identity_sha256=runtime,
            )
        parent_activation = _json_object(parent_activation_raw, label=f"{roles.reference} stream activation")
        current_activation = _json_object(current_activation_raw, label=f"{roles.target} stream activation")
        require_activation_mapping(
            parent_activation, current_activation,
            parent_runtime_sha256=parent.runtime_identity_sha256,
            current_runtime_sha256=current.runtime_identity_sha256,
            reference=roles.reference, target=roles.target,
        )
        if any(owner != current_activation["owner"] for owner in qualified_owners):
            raise MinerUDeploymentGateError(
                f"{roles.target} activation owner is not the native owner the {roles.subject} recorded"
            )

    # 6. v2 only: recovery origin -> target, from the origin's archived pins.
    if isinstance(upgrade, LocalExecutionUpgradeV2):
        _verify_recovery_origin(
            upgrade.recovery_origin,
            target=current,
            target_manifest=current_manifest,
            target_process_profile=process_profile,
            target_worker=current_worker,
            target_activation=current_activation,
            capacity=capacity,
        )

    # 7. The pinned legacy inventory; the contract binds every member to its
    # obligations' origin (v1 Q0, v2 the recovery origin).
    with _upgrade_step("legacy inventory"):
        inventory = decode_legacy_scope_inventory(_read_pinned(
            upgrade.legacy_scope.inventory_file, upgrade.legacy_scope.inventory_sha256,
            label="legacy scope inventory", max_bytes=MAX_INVENTORY_BYTES,
        ))
        execution = VerifiedQualifiedExecution(
            upgrade=upgrade,
            upgrade_sha256=upgrade_sha,
            review=review,
            review_sha256=review_sha,
            inventory=inventory,
            parent_qualified_at=historical.canary_passed_at_utc,
        )
    return replace(
        historical,
        runtime_identity_sha256=current.runtime_identity_sha256,
        qualification_origin="compatible_parent",
        execution=execution,
    )


def _verify_recovery_origin(
    origin: RecoveryOrigin,
    *,
    target: CurrentExecution,
    target_manifest: dict[str, Any],
    target_process_profile: MineruProcessProfile,
    target_worker: StagedWorkerProfileV4,
    target_activation: dict[str, Any],
    capacity: AnyMineruCapacityConfig,
) -> None:
    """E1 -> target: the origin's identities recomputed from its archived files.

    Each archived file is verified against its own pin, never against the
    current tree. The origin manifest must equal the target except the local
    writer, and P, WP and A may move only their references.
    """

    uid = os.getuid()
    with _upgrade_step("recovery origin release manifest"):
        origin_release = decode_execution_release_manifest(_read_pinned(
            origin.release_manifest_file, origin.release_manifest_sha256,
            label="recovery origin execution release manifest", max_bytes=MAX_RELEASE_MANIFEST_BYTES,
        ))
    if (origin_release.writer_code_sha256, origin_release.source_revision) != (
        origin.writer_code_sha256, origin.source_revision,
    ):
        raise MinerUDeploymentGateError("recovery origin release manifest is not the proposal's origin")
    with _upgrade_step("recovery origin runtime"):
        wrapper = _json_object(_read_pinned(
            origin.runtime_bundle_file, origin.runtime_bundle_sha256,
            label="recovery origin runtime bundle", max_bytes=_MAX_EVIDENCE_BYTES,
        ), label="recovery origin runtime bundle")
        if set(wrapper) != {"identity_sha256", "manifest"}:
            raise MinerUDeploymentGateError(
                "recovery origin runtime bundle must carry exactly identity_sha256 and manifest"
            )
        origin_manifest = wrapper["manifest"]
        if type(origin_manifest) is not dict or not (
            wrapper["identity_sha256"] == canonical_payload_sha256(origin_manifest) == origin.runtime_identity_sha256
        ):
            raise MinerUDeploymentGateError("recovery origin runtime bundle does not recompute to the origin runtime")
        require_computation_invariance(
            origin_manifest, target_manifest,
            reference_writer_sha256=origin.writer_code_sha256,
            target_writer_sha256=target.writer_code_sha256,
            reference="recovery origin",
        )
    with _upgrade_step("recovery origin process profile"):
        origin_profile = load_mineru_process_profile(
            Path(origin.process_profile_file), expected_sha256=origin.process_profile_sha256,
            expected_owner_uid=uid,
        ).profile
        require_process_profile_mapping(
            origin_profile, target_process_profile,
            parent_runtime_sha256=origin.runtime_identity_sha256,
            current_runtime_sha256=target.runtime_identity_sha256,
            reference="recovery origin", target="target",
        )
    origin_worker = derive_parent_worker_profile(target_worker, parent_process_profile_sha256=origin_profile.sha256)
    if origin_worker.sha256 != origin.worker_profile_sha256:
        raise MinerUDeploymentGateError(
            "target worker profile differs from the recovery origin beyond the process-profile reference"
        )
    with _upgrade_step("recovery origin stream activation"):
        activation_path = Path(origin.stream_activation_file)
        origin_activation = _json_object(read_mineru_capacity_file(
            activation_path, expected_sha256=origin.stream_activation_sha256, expected_owner_uid=uid,
        ), label="recovery origin stream activation")
        load_mineru_stream_activation(
            activation_path, expected_sha256=origin.stream_activation_sha256, expected_owner_uid=uid,
            expected_capacity=capacity, expected_runtime_identity_sha256=origin.runtime_identity_sha256,
        )
        require_activation_mapping(
            origin_activation, target_activation,
            parent_runtime_sha256=origin.runtime_identity_sha256,
            current_runtime_sha256=target.runtime_identity_sha256,
            reference="recovery origin", target="target",
        )


def _verify_qualified_runtime_upgrade(
    settings: Settings,
    *,
    upgrade: QualifiedRuntimeUpgrade,
    upgrade_sha: str,
    review: Any,
    review_sha: str,
    process_profile: MineruProcessProfile | None,
    expected_capacity: AnyMineruCapacityConfig | None,
    now: datetime,
) -> VerifiedMinerUDeployment:
    """Qnew exact for the target, the archived origin across result axes, the prepared scope."""

    roles = _QUALIFIED_ROLES
    qualification, origin, current = upgrade.target_qualification, upgrade.recovery_origin, upgrade.current
    capacity = configured_mineru_capacity(settings, expected_capacity)
    if not isinstance(capacity, MineruCapacityConfigV2):
        raise MinerUDeploymentGateError(
            "a qualified result runtime upgrade requires the explicit result-storage capacity config"
        )
    if type(process_profile) is not MineruProcessProfile:
        raise MinerUDeploymentGateError("a qualified result runtime upgrade requires the staged process profile")
    if (
        settings.disclosure_mineru_runtime_bundle_identity_sha256 != current.runtime_identity_sha256
        or process_profile.sha256 != current.process_profile_sha256
        or capacity.sha256 != current.capacity_config_sha256
        or settings.disclosure_mineru_stream_pressure_config_sha256 != current.stream_activation_sha256
    ):
        raise MinerUDeploymentGateError(
            "configured runtime, process profile, capacity or stream activation is not the upgrade's target"
        )
    mineru_bin = _require_settings_path(settings.disclosure_mineru_bin, label="DISCLOSURE_MINERU_BIN")
    with _upgrade_step(roles.release_step):
        release = decode_execution_release_manifest(_read_pinned(
            current.release_manifest_file, current.release_manifest_sha256,
            label="execution release manifest", max_bytes=MAX_RELEASE_MANIFEST_BYTES,
        ))
    if (release.writer_code_sha256, release.source_revision) != (current.writer_code_sha256, current.source_revision):
        raise MinerUDeploymentGateError(f"execution release manifest is not the proposal's {roles.release}")
    verify_execution_release(release, name=roles.release_name)
    with _upgrade_step("target runtime"):
        wrapper = _json_object(_read_pinned(
            current.runtime_bundle_file, current.runtime_bundle_sha256,
            label="target runtime bundle", max_bytes=_MAX_EVIDENCE_BYTES,
        ), label="target runtime bundle")
        if set(wrapper) != {"identity_sha256", "manifest"}:
            raise MinerUDeploymentGateError("target runtime bundle must carry exactly identity_sha256 and manifest")
        current_manifest = verify_runtime_manifest_payload(
            wrapper,
            configured_identity=current.runtime_identity_sha256,
            local_client_identity=client_bundle_identity(mineru_bin),
            local_processing_window_size=settings.mineru_processing_window_size,
            local_writer_code_digest=current.writer_code_sha256,
            expected_capacity=capacity,
        ).manifest
    _verify_configured_capacity(settings, process_profile=process_profile, expected_capacity=capacity)
    _verify_staged_profile_manifest(process_profile, current_manifest, expected_capacity=capacity)

    # Qnew: the configured qualification files are exactly the reviewed ones,
    # and the unchanged exact path verifies them as the target's own.
    smoke_path = _require_settings_path(settings.disclosure_mineru_smoke_receipt, label="smoke receipt")
    cache_path = _require_settings_path(settings.disclosure_mineru_canary_cache, label="canary cache")
    validation_path = _require_settings_path(
        settings.disclosure_mineru_validation_receipt, label="held-out validation receipt",
    )
    pinned_qnew = (
        (smoke_path, qualification.smoke_receipt_sha256, "new qualification smoke receipt", _MAX_EVIDENCE_BYTES),
        (cache_path, qualification.canary_cache_sha256, "new qualification canary cache", _MAX_EVIDENCE_BYTES),
        (validation_path, qualification.validation_receipt_sha256, "new qualification held-out receipt",
         _MAX_VALIDATION_EVIDENCE_BYTES),
    )
    before = [_read_pinned(path, sha, label=label, max_bytes=limit) for path, sha, label, limit in pinned_qnew]
    with _upgrade_step("new qualification process profile"):
        qualified_profile = load_mineru_process_profile(
            Path(qualification.process_profile_file), expected_sha256=qualification.process_profile_sha256,
            expected_owner_uid=os.getuid(),
        ).profile
        if qualified_profile != process_profile:
            raise MinerUDeploymentGateError("the new qualification's process profile is not the configured one")
    exact = _verify_mineru_deployment_evidence(
        settings,
        parse_enabled=True,
        process_profile=process_profile,
        now=now,
        historical_writer_digest=None,
        expected_capacity=capacity,
    )
    if exact is None or exact.qualification_origin != "exact" or (
        exact.runtime_identity_sha256 != current.runtime_identity_sha256
    ):
        raise MinerUDeploymentGateError("the target runtime did not pass its own new qualification")
    after = [_read_pinned(path, sha, label=label, max_bytes=limit) for path, sha, label, limit in pinned_qnew]
    if after != before:
        raise MinerUDeploymentGateError("new qualification files changed during verification")
    with _upgrade_step("new qualification facts"):
        smoke = _json_object(before[0], label="new qualification smoke receipt")
        cache = _json_object(before[1], label="new qualification canary cache")
        validation = _json_object(before[2], label="new qualification held-out receipt")
        if canonical_payload_sha256(smoke["runtime_manifest"]) != qualification.runtime_identity_sha256:
            raise MinerUDeploymentGateError("new qualification smoke runtime manifest is not the target runtime")
        if cache.get("passed_at_utc") != qualification.qualified_at_utc:
            raise MinerUDeploymentGateError("new qualification qualified_at_utc is not its canary pass time")
        if validation["epoch_after"]["receipt"]["service_epoch_sha256"] != qualification.service_epoch_sha256:
            raise MinerUDeploymentGateError("new qualification service epoch differs from its held-out receipt")
        qualified_owners = _heldout_owners(validation, role="new qualification")
    activation_path = _require_settings_path(
        settings.disclosure_mineru_stream_pressure_config, label="current stream activation",
    )
    uid = os.getuid()
    with _upgrade_step(roles.activation_step):
        if activation_path != Path(qualification.stream_activation_file):
            raise MinerUDeploymentGateError("configured stream activation is not the new qualification's file")
        current_activation = _json_object(read_mineru_capacity_file(
            activation_path, expected_sha256=current.stream_activation_sha256, expected_owner_uid=uid,
        ), label="target stream activation")
        if any(owner != current_activation["owner"] for owner in qualified_owners):
            raise MinerUDeploymentGateError(
                "target activation owner is not the native owner the new qualification recorded"
            )
    with _upgrade_step("worker profile"):
        current_worker = _active_worker_profile(process_profile, settings)
        if current_worker.sha256 != current.worker_profile_sha256:
            raise MinerUDeploymentGateError(f"composed worker profile is not the upgrade's {roles.worker}")
    _verify_result_recovery_origin(
        upgrade,
        target_manifest=current_manifest,
        target_process_profile=process_profile,
        target_worker=current_worker,
    )
    with _upgrade_step("legacy inventory"):
        inventory = decode_legacy_scope_inventory(_read_pinned(
            upgrade.legacy_scope.inventory_file, upgrade.legacy_scope.inventory_sha256,
            label="legacy scope inventory", max_bytes=MAX_INVENTORY_BYTES,
        ))
        execution = VerifiedQualifiedExecution(
            upgrade=upgrade,
            upgrade_sha256=upgrade_sha,
            review=review,
            review_sha256=review_sha,
            inventory=inventory,
            parent_qualified_at=exact.canary_passed_at_utc,
        )
    with _upgrade_step("original key lookups"):
        evidence = decode_legacy_key_lookup_evidence(_read_pinned(
            upgrade.key_lookups.evidence_file, upgrade.key_lookups.evidence_sha256,
            label="legacy key lookup evidence", max_bytes=MAX_KEY_LOOKUP_BYTES,
        ))
        require_key_lookup_coverage(
            evidence, inventory,
            origin_runtime_identity_sha256=origin.runtime_identity_sha256,
            key_ttl_seconds=upgrade.key_lookups.key_ttl_seconds,
        )
    return replace(exact, execution=execution)


def _verify_result_recovery_origin(
    upgrade: QualifiedRuntimeUpgrade,
    *,
    target_manifest: dict[str, Any],
    target_process_profile: MineruProcessProfile,
    target_worker: StagedWorkerProfileV4,
) -> None:
    """The archived origin maps to the newly qualified target across the result axes only.

    Every origin file is verified against its own pin, never the current tree.
    The origin activation is archival only: pressure control is always the
    current runtime's, as for every legacy obligation.
    """

    origin, target = upgrade.recovery_origin, upgrade.current
    uid = os.getuid()
    with _upgrade_step("recovery origin release manifest"):
        origin_release = decode_execution_release_manifest(_read_pinned(
            origin.release_manifest_file, origin.release_manifest_sha256,
            label="recovery origin execution release manifest", max_bytes=MAX_RELEASE_MANIFEST_BYTES,
        ))
    if (origin_release.writer_code_sha256, origin_release.source_revision) != (
        origin.writer_code_sha256, origin.source_revision,
    ):
        raise MinerUDeploymentGateError("recovery origin release manifest is not the proposal's origin")
    with _upgrade_step("recovery origin result runtime"):
        wrapper = _json_object(_read_pinned(
            origin.runtime_bundle_file, origin.runtime_bundle_sha256,
            label="recovery origin runtime bundle", max_bytes=_MAX_EVIDENCE_BYTES,
        ), label="recovery origin runtime bundle")
        if set(wrapper) != {"identity_sha256", "manifest"}:
            raise MinerUDeploymentGateError(
                "recovery origin runtime bundle must carry exactly identity_sha256 and manifest"
            )
        origin_manifest = wrapper["manifest"]
        if type(origin_manifest) is not dict or not (
            wrapper["identity_sha256"] == canonical_payload_sha256(origin_manifest) == origin.runtime_identity_sha256
        ):
            raise MinerUDeploymentGateError("recovery origin runtime bundle does not recompute to the origin runtime")
        if origin_manifest["client"]["writer_code_sha256"] != origin.writer_code_sha256:
            raise MinerUDeploymentGateError("recovery origin runtime does not carry the origin release's writer")
        if origin_manifest["orchestrator"]["capacity_config_sha256"] != origin.capacity_config_sha256:
            raise MinerUDeploymentGateError("recovery origin runtime does not carry the origin capacity")
        require_result_runtime_change(origin_manifest, target_manifest, changes=upgrade.runtime_changes)
        require_result_capacity_change(
            origin_manifest["orchestrator"]["capacity_config"], target_manifest["orchestrator"]["capacity_config"],
        )
    with _upgrade_step("recovery origin process profile"):
        origin_profile = load_mineru_process_profile(
            Path(origin.process_profile_file), expected_sha256=origin.process_profile_sha256,
            expected_owner_uid=uid,
        ).profile
        require_result_process_profile_change(
            origin_profile, target_process_profile,
            origin_runtime_sha256=origin.runtime_identity_sha256,
            target_runtime_sha256=target.runtime_identity_sha256,
        )
    origin_worker = derive_parent_worker_profile(target_worker, parent_process_profile_sha256=origin_profile.sha256)
    if origin_worker.sha256 != origin.worker_profile_sha256:
        raise MinerUDeploymentGateError(
            "target worker profile differs from the recovery origin beyond the process-profile reference"
        )
    with _upgrade_step("recovery origin stream activation"):
        read_mineru_capacity_file(
            Path(origin.stream_activation_file), expected_sha256=origin.stream_activation_sha256,
            expected_owner_uid=uid,
        )


def verify_configured_execution_upgrade(settings: Settings) -> VerifiedQualifiedExecution:
    """Doctor entry: exactly the resident loop's static gate call under U01."""

    if not settings.execution_upgrade_configured:
        raise MinerUDeploymentGateError("no local execution upgrade is configured")
    if settings.worker_parse_execution_mode != "staged-v4":
        raise MinerUDeploymentGateError("a local execution upgrade requires staged-v4 mode")
    staged = load_staged_v4_settings()
    with _upgrade_step("process profile"):
        process_profile = load_mineru_process_profile(
            staged.process_profile_file, expected_sha256=staged.process_profile_sha256,
            expected_owner_uid=os.getuid(),
        ).profile
    checker = MinerUDeploymentChecker(
        settings, parse_enabled=True, process_profile=process_profile, accept_execution_upgrade=True,
    )
    if checker.verified_execution is None:
        raise MinerUDeploymentGateError("configured local execution upgrade did not verify")
    return checker.verified_execution


def recheck_execution_release(execution: VerifiedQualifiedExecution) -> None:
    """Re-hash the current release under the worker singleton (preflight-to-start TOCTOU)."""

    current = execution.upgrade.current
    with _upgrade_step("release manifest recheck"):
        release = decode_execution_release_manifest(_read_pinned(
            current.release_manifest_file, current.release_manifest_sha256,
            label="execution release manifest", max_bytes=MAX_RELEASE_MANIFEST_BYTES,
        ))
    verify_execution_release(release, name=_upgrade_roles(execution.upgrade).release_name)


def write_boot_receipt(
    settings: Settings,
    execution: VerifiedQualifiedExecution,
    *,
    owner_identity: str,
    scope: LegacyScopeObservation,
    booted_at: datetime | None = None,
) -> tuple[Path, str, str]:
    """One create-only 0600 receipt per resident boot; returns (path, sha256, log line).

    Written after the singleton, the release/scope recheck and composition,
    before the coordinator runs. It binds the coordinator owner (the claim
    identity on every row this boot touches) to U01, the release, the profiles
    and the observed scope. It is evidence, never an input to any later
    decision. A v1 edge keeps its v1 receipt; a v2 relation writes
    ``worker-execution-boot-receipt.v2`` with every anchor/origin/target
    identity from ``VerifiedQualifiedExecution.summary``.
    """

    booted_at_utc = (booted_at or datetime.now(UTC)).astimezone(UTC).isoformat()
    upgrade = execution.upgrade
    if isinstance(upgrade, QualifiedRuntimeUpgrade):
        qualification, origin, target = upgrade.target_qualification, upgrade.recovery_origin, upgrade.current
        path, digest = _publish_boot_receipt(settings, owner_identity, {
            "contract_version": BOOT_RECEIPT_QUALIFIED_CONTRACT,
            "booted_at_utc": booted_at_utc,
            "owner_identity": owner_identity,
            "worker_pid": os.getpid(),
            **execution.summary(),
            "scope": scope.to_payload(),
        })
        return path, digest, (
            "[execution-upgrade] boot "
            f"receipt={path} receipt_sha256={digest} owner={owner_identity} "
            f"contract={execution.upgrade_contract_version} qualification={execution.qualification_origin} "
            f"upgrade={execution.upgrade_sha256} "
            f"qualified_at={qualification.qualified_at_utc} "
            f"release={origin.release_manifest_sha256}->{target.release_manifest_sha256} "
            f"runtime={origin.runtime_identity_sha256}->{target.runtime_identity_sha256} "
            f"worker_profile={origin.worker_profile_sha256}->{target.worker_profile_sha256}"
        )
    if isinstance(upgrade, LocalExecutionUpgradeV2):
        anchor, origin, target = upgrade.qualification_anchor, upgrade.recovery_origin, upgrade.current
        path, digest = _publish_boot_receipt(settings, owner_identity, {
            "contract_version": BOOT_RECEIPT_V2_CONTRACT,
            "booted_at_utc": booted_at_utc,
            "owner_identity": owner_identity,
            "worker_pid": os.getpid(),
            **execution.summary(),
            "scope": scope.to_payload(),
        })
        return path, digest, (
            "[execution-upgrade] boot "
            f"receipt={path} receipt_sha256={digest} owner={owner_identity} "
            f"contract={execution.upgrade_contract_version} qualification={execution.qualification_origin} "
            f"upgrade={execution.upgrade_sha256} "
            f"anchor_qualified_at={anchor.qualified_at_utc} anchor_runtime={anchor.runtime_identity_sha256} "
            f"release={origin.release_manifest_sha256}->{target.release_manifest_sha256} "
            f"writer={origin.writer_code_sha256}->{target.writer_code_sha256} "
            f"runtime={origin.runtime_identity_sha256}->{target.runtime_identity_sha256} "
            f"worker_profile={origin.worker_profile_sha256}->{target.worker_profile_sha256}"
        )
    parent, current = upgrade.parent, upgrade.current
    payload = {
        "contract_version": BOOT_RECEIPT_CONTRACT,
        "booted_at_utc": booted_at_utc,
        "owner_identity": owner_identity,
        "worker_pid": os.getpid(),
        "qualification_origin": execution.qualification_origin,
        "upgrade_sha256": execution.upgrade_sha256,
        "review_sha256": execution.review_sha256,
        "release_manifest_sha256": current.release_manifest_sha256,
        "source_revision": current.source_revision,
        "parent_writer_code_sha256": parent.writer_code_sha256,
        "writer_code_sha256": current.writer_code_sha256,
        "parent_runtime_identity_sha256": parent.runtime_identity_sha256,
        "runtime_identity_sha256": current.runtime_identity_sha256,
        "parent_process_profile_sha256": parent.process_profile_sha256,
        "process_profile_sha256": current.process_profile_sha256,
        "parent_worker_profile_sha256": parent.worker_profile_sha256,
        "worker_profile_sha256": current.worker_profile_sha256,
        "capacity_config_sha256": current.capacity_config_sha256,
        "stream_activation_sha256": current.stream_activation_sha256,
        "parent_qualified_at_utc": parent.qualified_at_utc,
        "legacy_inventory_sha256": execution.upgrade.legacy_scope.inventory_sha256,
        "legacy_member_count": len(execution.inventory.members),
        "scope": scope.to_payload(),
    }
    path, digest = _publish_boot_receipt(settings, owner_identity, payload)
    line = (
        "[execution-upgrade] boot "
        f"receipt={path} receipt_sha256={digest} owner={owner_identity} "
        f"origin={execution.qualification_origin} upgrade={execution.upgrade_sha256} "
        f"release={current.release_manifest_sha256} "
        f"writer={parent.writer_code_sha256}->{current.writer_code_sha256} "
        f"runtime={parent.runtime_identity_sha256}->{current.runtime_identity_sha256} "
        f"worker_profile={parent.worker_profile_sha256}->{current.worker_profile_sha256} "
        f"parent_qualified_at={parent.qualified_at_utc}"
    )
    return path, digest, line


def _publish_boot_receipt(settings: Settings, owner_identity: str, payload: dict[str, Any]) -> tuple[Path, str]:
    encoded = canonical_bytes(payload)
    path = FileStorePathBuilder(settings).execution_boot_receipt_path(owner_identity)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise MinerUDeploymentGateError("execution boot receipt directory is unsafe")
    publish_new_exact(path, encoded)
    return path, sha256_of(encoded)


# ---------------------------------------------------------------------------
# Builders (root runs them after code freeze; each writes new 0600 files)
# ---------------------------------------------------------------------------


def _observed_sha256(path: Path, *, label: str, max_bytes: int) -> str:
    """SHA-256 of one explicit input; the product loader then pins it exactly."""

    if not path.is_absolute():
        raise MinerUDeploymentGateError(f"{label} path must be absolute")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise MinerUDeploymentGateError(f"{label} must be a regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(max_bytes + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > max_bytes:
        raise MinerUDeploymentGateError(f"{label} exceeds the size limit")
    return sha256_of(payload)


def _parent_smoke_manifest(settings: Settings) -> dict[str, Any]:
    smoke_bytes, _ = read_owner_only_evidence(
        _require_settings_path(settings.disclosure_mineru_smoke_receipt, label="smoke receipt"),
        label="parent smoke receipt", max_bytes=_MAX_EVIDENCE_BYTES,
    )
    manifest = _json_object(smoke_bytes, label="parent smoke receipt").get("runtime_manifest")
    if type(manifest) is not dict:
        raise MinerUDeploymentGateError("parent smoke receipt has no runtime manifest")
    return cast(dict[str, Any], manifest)


@dataclass(frozen=True, slots=True)
class DerivedLocalUpgrade:
    """Derived target files; ``parent_*`` is Q0 (a v2 anchor), ``current_*`` the target."""

    parent_runtime_identity_sha256: str
    parent_writer_code_sha256: str
    parent_process_profile_sha256: str
    parent_stream_activation_sha256: str
    current_writer_code_sha256: str
    current_runtime_identity_sha256: str
    runtime_bundle_file: Path
    runtime_bundle_sha256: str
    process_profile_file: Path
    process_profile_sha256: str
    stream_activation_file: Path
    stream_activation_sha256: str
    contract_version: str = UPGRADE_CONTRACT

    def env(self) -> dict[str, str]:
        """The current-execution values to configure (with U01, later)."""

        return {
            "DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256": self.current_runtime_identity_sha256,
            "DISCLOSURE_V4_PROCESS_PROFILE_FILE": str(self.process_profile_file),
            "DISCLOSURE_V4_PROCESS_PROFILE_SHA256": self.process_profile_sha256,
            "DISCLOSURE_MINERU_STREAM_PRESSURE_CONFIG": str(self.stream_activation_file),
            "DISCLOSURE_MINERU_STREAM_PRESSURE_CONFIG_SHA256": self.stream_activation_sha256,
        }

    def to_payload(self) -> dict[str, Any]:
        """v1 keeps its historical keys; v2 names the Q0 anchor and the target."""

        fields = {name: str(getattr(self, name)) for name in self.__dataclass_fields__}
        if self.contract_version == UPGRADE_CONTRACT_V2:
            fields = {
                "anchor_" + name.removeprefix("parent_") if name.startswith("parent_")
                else "target_" + name.removeprefix("current_") if name.startswith("current_")
                else name: value
                for name, value in fields.items()
            }
        return {**fields, "env": self.env()}


def derive_local_upgrade(
    settings: Settings,
    *,
    parent_process_profile: Path,
    parent_activation: Path,
    output_dir: Path,
    contract_version: str = UPGRADE_CONTRACT,
) -> DerivedLocalUpgrade:
    """Derive the target M/R, P and A from Q0's M0, the explicit P0/A0 and the local writer.

    Only the local writer and the references naming the runtime move; every
    other byte comes from Q0. The target manifest is verified against the
    measured local client, the recomputed writer and the explicit v11 capacity.
    The v1 edge requires a new writer; a v2 relation may keep Q0's writer (a
    release that changes no fingerprinted writer file), and then R, P and A
    derive to Q0's own identities.
    """

    if contract_version not in (UPGRADE_CONTRACT, UPGRADE_CONTRACT_V2):
        raise MinerUDeploymentGateError("derive contract version is unsupported")
    v2 = contract_version == UPGRADE_CONTRACT_V2
    reference, target = ("qualification anchor", "target") if v2 else ("parent", "current")

    if not output_dir.is_absolute() or output_dir.exists() or output_dir.is_symlink() or not output_dir.parent.is_dir():
        raise MinerUDeploymentGateError("derive output must be a new absolute directory under an existing parent")
    capacity = configured_mineru_capacity(settings, None)
    if capacity is None:
        raise MinerUDeploymentGateError("derive requires the explicit v11 capacity")
    mineru_bin = _require_settings_path(settings.disclosure_mineru_bin, label="DISCLOSURE_MINERU_BIN")
    uid = os.getuid()
    with _upgrade_step("derive"):
        parent_manifest = _parent_smoke_manifest(settings)
        parent_runtime = canonical_payload_sha256(parent_manifest)
        parent_writer = parent_manifest["client"]["writer_code_sha256"]
        profile_sha = _observed_sha256(parent_process_profile, label="parent process profile", max_bytes=_MAX_EVIDENCE_BYTES)
        parent_profile = load_mineru_process_profile(
            parent_process_profile, expected_sha256=profile_sha, expected_owner_uid=uid,
        ).profile
        if parent_profile.runtime_bundle_identity_sha256 != parent_runtime:
            raise MinerUDeploymentGateError("parent process profile is not bound to the qualified R0")
        activation_sha = _observed_sha256(parent_activation, label="parent stream activation", max_bytes=_MAX_EVIDENCE_BYTES)
        load_mineru_stream_activation(
            parent_activation, expected_sha256=activation_sha, expected_owner_uid=uid,
            expected_capacity=capacity, expected_runtime_identity_sha256=parent_runtime,
        )
        parent_activation_object = _json_object(
            read_mineru_capacity_file(parent_activation, expected_sha256=activation_sha, expected_owner_uid=uid),
            label="parent stream activation",
        )
        current_writer = writer_code_digest()
        current_manifest = copy.deepcopy(parent_manifest)
        current_manifest["client"]["writer_code_sha256"] = current_writer
        current_runtime = canonical_payload_sha256(current_manifest)
        if v2:
            require_computation_invariance(
                parent_manifest, current_manifest,
                reference_writer_sha256=parent_writer, target_writer_sha256=current_writer,
                reference=reference,
            )
        else:
            require_same_computation(
                parent_manifest, current_manifest,
                parent_writer_sha256=parent_writer, current_writer_sha256=current_writer,
            )
        wrapper = {"identity_sha256": current_runtime, "manifest": current_manifest}
        verify_runtime_manifest_payload(
            wrapper, configured_identity=current_runtime,
            local_client_identity=client_bundle_identity(mineru_bin),
            local_processing_window_size=settings.mineru_processing_window_size,
            local_writer_code_digest=current_writer, expected_capacity=capacity,
        )
        current_profile = replace(parent_profile, runtime_bundle_identity_sha256=current_runtime)
        require_process_profile_mapping(
            parent_profile, current_profile,
            parent_runtime_sha256=parent_runtime, current_runtime_sha256=current_runtime,
            reference=reference, target=target,
        )
        current_activation = copy.deepcopy(parent_activation_object)
        current_activation["runtime_identity_sha256"] = current_runtime
        current_activation["policy"]["runtime_identity_sha256"] = current_runtime
        require_activation_mapping(
            parent_activation_object, current_activation,
            parent_runtime_sha256=parent_runtime, current_runtime_sha256=current_runtime,
            reference=reference, target=target,
        )
        bundle_bytes = canonical_bytes(wrapper)
        activation_bytes = canonical_bytes(current_activation)
        output_dir.mkdir(mode=0o700)
        bundle_path = output_dir / "runtime-bundle.json"
        profile_path = output_dir / "process-profile.json"
        activation_path = output_dir / "activation.json"
        write_new_exact(bundle_path, bundle_bytes)
        write_new_exact(profile_path, current_profile.exact_bytes)
        write_new_exact(activation_path, activation_bytes)
        # Reload every artifact through the product loaders before reporting.
        reloaded = load_mineru_process_profile(
            profile_path, expected_sha256=current_profile.sha256, expected_owner_uid=uid,
        )
        load_mineru_stream_activation(
            activation_path, expected_sha256=sha256_of(activation_bytes), expected_owner_uid=uid,
            expected_capacity=capacity, expected_runtime_identity_sha256=current_runtime,
        )
        reread, _ = read_owner_only_evidence(bundle_path, label="derived runtime bundle", max_bytes=_MAX_EVIDENCE_BYTES)
        if reloaded.profile != current_profile or reread != bundle_bytes:
            raise MinerUDeploymentGateError("derived artifacts did not reload exactly")
    return DerivedLocalUpgrade(
        parent_runtime_identity_sha256=parent_runtime,
        parent_writer_code_sha256=parent_writer,
        parent_process_profile_sha256=parent_profile.sha256,
        parent_stream_activation_sha256=activation_sha,
        current_writer_code_sha256=current_writer,
        current_runtime_identity_sha256=current_runtime,
        runtime_bundle_file=bundle_path,
        runtime_bundle_sha256=sha256_of(bundle_bytes),
        process_profile_file=profile_path,
        process_profile_sha256=current_profile.sha256,
        stream_activation_file=activation_path,
        stream_activation_sha256=sha256_of(activation_bytes),
        contract_version=contract_version,
    )


def build_upgrade_proposal(
    settings: Settings,
    *,
    release_manifest: Path,
    runtime_bundle: Path,
    parent_process_profile: Path,
    parent_activation: Path,
    inventory: Path,
    exact_change_manifest_sha256: str,
    independent_test_evidence_sha256: str,
    independent_code_review_sha256: str,
) -> LocalExecutionUpgrade:
    """Assemble the U01 proposal; configured runtime/P/A values are current.

    Run after the environment names the derived R1/P1/A1 and before U01 is
    configured. The reviewer then approves this proposal file's SHA-256. The
    verifier re-checks everything; this builder only refuses early.
    """

    if settings.execution_upgrade_configured:
        raise MinerUDeploymentGateError("a proposal is assembled before any upgrade is configured")
    capacity = configured_mineru_capacity(settings, None)
    if capacity is None:
        raise MinerUDeploymentGateError("proposal requires the explicit v11 capacity")
    activation_sha = settings.disclosure_mineru_stream_pressure_config_sha256
    if activation_sha is None:
        raise MinerUDeploymentGateError("current stream activation SHA-256 is required")
    uid = os.getuid()
    with _upgrade_step("proposal assembly"):
        release_bytes, _ = read_owner_only_evidence(
            release_manifest, label="execution release manifest", max_bytes=MAX_RELEASE_MANIFEST_BYTES,
        )
        release = decode_execution_release_manifest(release_bytes)
        verify_execution_release(release)
        bundle_bytes, _ = read_owner_only_evidence(
            runtime_bundle, label="current runtime bundle", max_bytes=_MAX_EVIDENCE_BYTES,
        )
        current_runtime = _json_object(bundle_bytes, label="current runtime bundle")["identity_sha256"]
        if settings.disclosure_mineru_runtime_bundle_identity_sha256 != current_runtime:
            raise MinerUDeploymentGateError("configured runtime is not the derived runtime bundle")
        staged = load_staged_v4_settings()
        current_profile = load_mineru_process_profile(
            staged.process_profile_file, expected_sha256=staged.process_profile_sha256, expected_owner_uid=uid,
        ).profile
        parent_profile_sha = _observed_sha256(
            parent_process_profile, label="parent process profile", max_bytes=_MAX_EVIDENCE_BYTES,
        )
        parent_profile = load_mineru_process_profile(
            parent_process_profile, expected_sha256=parent_profile_sha, expected_owner_uid=uid,
        ).profile
        current_worker = _active_worker_profile(current_profile, settings)
        parent_worker = derive_parent_worker_profile(
            current_worker, parent_process_profile_sha256=parent_profile.sha256,
        )
        parent_activation_sha = _observed_sha256(
            parent_activation, label="parent stream activation", max_bytes=_MAX_EVIDENCE_BYTES,
        )
        read_mineru_capacity_file(parent_activation, expected_sha256=parent_activation_sha, expected_owner_uid=uid)
        smoke_path = _require_settings_path(settings.disclosure_mineru_smoke_receipt, label="smoke receipt")
        cache_path = _require_settings_path(settings.disclosure_mineru_canary_cache, label="canary cache")
        validation_path = _require_settings_path(
            settings.disclosure_mineru_validation_receipt, label="held-out validation receipt",
        )
        smoke_bytes, _ = read_owner_only_evidence(smoke_path, label="parent smoke", max_bytes=_MAX_EVIDENCE_BYTES)
        cache_bytes, _ = read_owner_only_evidence(cache_path, label="parent canary", max_bytes=_MAX_EVIDENCE_BYTES)
        validation_bytes, _ = read_owner_only_evidence(
            validation_path, label="parent held-out", max_bytes=_MAX_VALIDATION_EVIDENCE_BYTES,
        )
        parent_manifest = _json_object(smoke_bytes, label="parent smoke")["runtime_manifest"]
        parent_runtime = canonical_payload_sha256(parent_manifest)
        inventory_bytes, _ = read_owner_only_evidence(
            inventory, label="legacy scope inventory", max_bytes=MAX_INVENTORY_BYTES,
        )
        legacy = decode_legacy_scope_inventory(inventory_bytes)
        strays = [
            item.attempt_id for item in legacy.members
            if (item.runtime_epoch_sha256, item.process_profile_sha256, item.worker_profile_sha256)
            != (parent_runtime, parent_profile.sha256, parent_worker.sha256)
        ]
        if strays:
            raise MinerUDeploymentGateError(
                f"{len(strays)} legacy member(s) bind another runtime/profile pair; one edge cannot cover them "
                f"(first {strays[:5]})"
            )
        upgrade = LocalExecutionUpgrade(
            parent=ParentQualification(
                runtime_identity_sha256=parent_runtime,
                writer_code_sha256=parent_manifest["client"]["writer_code_sha256"],
                smoke_receipt_sha256=sha256_of(smoke_bytes),
                canary_cache_sha256=sha256_of(cache_bytes),
                validation_receipt_sha256=sha256_of(validation_bytes),
                process_profile_file=str(parent_process_profile),
                process_profile_sha256=parent_profile.sha256,
                worker_profile_sha256=parent_worker.sha256,
                stream_activation_file=str(parent_activation),
                stream_activation_sha256=parent_activation_sha,
                qualified_at_utc=_json_object(cache_bytes, label="parent canary")["passed_at_utc"],
                service_epoch_sha256=_json_object(validation_bytes, label="parent held-out")[
                    "epoch_after"]["receipt"]["service_epoch_sha256"],
            ),
            current=CurrentExecution(
                release_manifest_file=str(release_manifest),
                release_manifest_sha256=sha256_of(release_bytes),
                source_revision=release.source_revision,
                writer_code_sha256=release.writer_code_sha256,
                runtime_bundle_file=str(runtime_bundle),
                runtime_bundle_sha256=sha256_of(bundle_bytes),
                runtime_identity_sha256=current_runtime,
                process_profile_sha256=current_profile.sha256,
                worker_profile_sha256=current_worker.sha256,
                capacity_config_sha256=capacity.sha256,
                stream_activation_sha256=activation_sha,
            ),
            basis=CompatibilityBasis(
                exact_change_manifest_sha256=exact_change_manifest_sha256,
                independent_test_evidence_sha256=independent_test_evidence_sha256,
                independent_code_review_sha256=independent_code_review_sha256,
            ),
            legacy_scope=LegacyScopeReference(
                inventory_file=str(inventory),
                inventory_sha256=sha256_of(inventory_bytes),
                member_count=len(legacy.members),
            ),
        )
    return upgrade


def build_upgrade_proposal_v2(
    settings: Settings,
    *,
    release_manifest: Path,
    runtime_bundle: Path,
    anchor_process_profile: Path,
    anchor_activation: Path,
    origin_release_manifest: Path,
    origin_runtime_bundle: Path,
    origin_process_profile: Path,
    origin_activation: Path,
    inventory: Path,
    exact_change_manifest_sha256: str,
    independent_test_evidence_sha256: str,
    independent_code_review_sha256: str,
) -> LocalExecutionUpgradeV2:
    """Assemble a v2 proposal: the Q0 anchor, one archived recovery origin and the target.

    Run like the v1 builder: after the environment names the target runtime,
    process profile and activation, and before any upgrade is configured. The
    smoke, canary and validation settings still name Q0's files; P0/A0 are
    given explicitly. The origin files are the archived release, runtime
    bundle, process profile and activation that every inventory member was
    frozen under. This builder only refuses early; the verifier re-checks
    everything.
    """

    if settings.execution_upgrade_configured:
        raise MinerUDeploymentGateError("a proposal is assembled before any upgrade is configured")
    capacity = configured_mineru_capacity(settings, None)
    if capacity is None:
        raise MinerUDeploymentGateError("proposal requires the explicit v11 capacity")
    activation_sha = settings.disclosure_mineru_stream_pressure_config_sha256
    if activation_sha is None:
        raise MinerUDeploymentGateError("current stream activation SHA-256 is required")
    uid = os.getuid()
    with _upgrade_step("proposal assembly"):
        release_bytes, _ = read_owner_only_evidence(
            release_manifest, label="execution release manifest", max_bytes=MAX_RELEASE_MANIFEST_BYTES,
        )
        release = decode_execution_release_manifest(release_bytes)
        verify_execution_release(release, name="the target release")
        bundle_bytes, _ = read_owner_only_evidence(
            runtime_bundle, label="target runtime bundle", max_bytes=_MAX_EVIDENCE_BYTES,
        )
        target_runtime = _json_object(bundle_bytes, label="target runtime bundle")["identity_sha256"]
        if settings.disclosure_mineru_runtime_bundle_identity_sha256 != target_runtime:
            raise MinerUDeploymentGateError("configured runtime is not the derived runtime bundle")
        staged = load_staged_v4_settings()
        target_profile = load_mineru_process_profile(
            staged.process_profile_file, expected_sha256=staged.process_profile_sha256, expected_owner_uid=uid,
        ).profile
        target_worker = _active_worker_profile(target_profile, settings)

        anchor_profile = load_mineru_process_profile(
            anchor_process_profile,
            expected_sha256=_observed_sha256(
                anchor_process_profile, label="qualification anchor process profile", max_bytes=_MAX_EVIDENCE_BYTES,
            ),
            expected_owner_uid=uid,
        ).profile
        anchor_activation_sha = _observed_sha256(
            anchor_activation, label="qualification anchor stream activation", max_bytes=_MAX_EVIDENCE_BYTES,
        )
        read_mineru_capacity_file(anchor_activation, expected_sha256=anchor_activation_sha, expected_owner_uid=uid)
        smoke_path = _require_settings_path(settings.disclosure_mineru_smoke_receipt, label="smoke receipt")
        cache_path = _require_settings_path(settings.disclosure_mineru_canary_cache, label="canary cache")
        validation_path = _require_settings_path(
            settings.disclosure_mineru_validation_receipt, label="held-out validation receipt",
        )
        smoke_bytes, _ = read_owner_only_evidence(
            smoke_path, label="qualification anchor smoke", max_bytes=_MAX_EVIDENCE_BYTES,
        )
        cache_bytes, _ = read_owner_only_evidence(
            cache_path, label="qualification anchor canary", max_bytes=_MAX_EVIDENCE_BYTES,
        )
        validation_bytes, _ = read_owner_only_evidence(
            validation_path, label="qualification anchor held-out", max_bytes=_MAX_VALIDATION_EVIDENCE_BYTES,
        )
        anchor_manifest = _json_object(smoke_bytes, label="qualification anchor smoke")["runtime_manifest"]

        origin_release_bytes, _ = read_owner_only_evidence(
            origin_release_manifest, label="recovery origin execution release manifest",
            max_bytes=MAX_RELEASE_MANIFEST_BYTES,
        )
        origin_release = decode_execution_release_manifest(origin_release_bytes)
        origin_bundle_bytes, _ = read_owner_only_evidence(
            origin_runtime_bundle, label="recovery origin runtime bundle", max_bytes=_MAX_EVIDENCE_BYTES,
        )
        origin_wrapper = _json_object(origin_bundle_bytes, label="recovery origin runtime bundle")
        origin_runtime = origin_wrapper["identity_sha256"]
        if origin_wrapper["manifest"]["client"]["writer_code_sha256"] != origin_release.writer_code_sha256:
            raise MinerUDeploymentGateError("recovery origin runtime bundle is not the origin release's writer")
        origin_profile = load_mineru_process_profile(
            origin_process_profile,
            expected_sha256=_observed_sha256(
                origin_process_profile, label="recovery origin process profile", max_bytes=_MAX_EVIDENCE_BYTES,
            ),
            expected_owner_uid=uid,
        ).profile
        if origin_profile.runtime_bundle_identity_sha256 != origin_runtime:
            raise MinerUDeploymentGateError("recovery origin process profile is not bound to the origin runtime")
        origin_worker = derive_parent_worker_profile(target_worker, parent_process_profile_sha256=origin_profile.sha256)
        origin_activation_sha = _observed_sha256(
            origin_activation, label="recovery origin stream activation", max_bytes=_MAX_EVIDENCE_BYTES,
        )
        read_mineru_capacity_file(origin_activation, expected_sha256=origin_activation_sha, expected_owner_uid=uid)

        inventory_bytes, _ = read_owner_only_evidence(
            inventory, label="legacy scope inventory", max_bytes=MAX_INVENTORY_BYTES,
        )
        legacy = decode_legacy_scope_inventory(inventory_bytes)
        strays = [
            item.attempt_id for item in legacy.members
            if (item.runtime_epoch_sha256, item.process_profile_sha256, item.worker_profile_sha256)
            != (origin_runtime, origin_profile.sha256, origin_worker.sha256)
        ]
        if strays:
            raise MinerUDeploymentGateError(
                f"{len(strays)} legacy member(s) are not bound to the recovery origin runtime/profile pair "
                f"(first {strays[:5]})"
            )
        upgrade = LocalExecutionUpgradeV2(
            qualification_anchor=ParentQualification(
                runtime_identity_sha256=canonical_payload_sha256(anchor_manifest),
                writer_code_sha256=anchor_manifest["client"]["writer_code_sha256"],
                smoke_receipt_sha256=sha256_of(smoke_bytes),
                canary_cache_sha256=sha256_of(cache_bytes),
                validation_receipt_sha256=sha256_of(validation_bytes),
                process_profile_file=str(anchor_process_profile),
                process_profile_sha256=anchor_profile.sha256,
                worker_profile_sha256=derive_parent_worker_profile(
                    target_worker, parent_process_profile_sha256=anchor_profile.sha256,
                ).sha256,
                stream_activation_file=str(anchor_activation),
                stream_activation_sha256=anchor_activation_sha,
                qualified_at_utc=_json_object(cache_bytes, label="qualification anchor canary")["passed_at_utc"],
                service_epoch_sha256=_json_object(validation_bytes, label="qualification anchor held-out")[
                    "epoch_after"]["receipt"]["service_epoch_sha256"],
            ),
            recovery_origin=RecoveryOrigin(
                release_manifest_file=str(origin_release_manifest),
                release_manifest_sha256=sha256_of(origin_release_bytes),
                source_revision=origin_release.source_revision,
                writer_code_sha256=origin_release.writer_code_sha256,
                runtime_bundle_file=str(origin_runtime_bundle),
                runtime_bundle_sha256=sha256_of(origin_bundle_bytes),
                runtime_identity_sha256=origin_runtime,
                process_profile_file=str(origin_process_profile),
                process_profile_sha256=origin_profile.sha256,
                worker_profile_sha256=origin_worker.sha256,
                capacity_config_sha256=capacity.sha256,
                stream_activation_file=str(origin_activation),
                stream_activation_sha256=origin_activation_sha,
            ),
            current=CurrentExecution(
                release_manifest_file=str(release_manifest),
                release_manifest_sha256=sha256_of(release_bytes),
                source_revision=release.source_revision,
                writer_code_sha256=release.writer_code_sha256,
                runtime_bundle_file=str(runtime_bundle),
                runtime_bundle_sha256=sha256_of(bundle_bytes),
                runtime_identity_sha256=target_runtime,
                process_profile_sha256=target_profile.sha256,
                worker_profile_sha256=target_worker.sha256,
                capacity_config_sha256=capacity.sha256,
                stream_activation_sha256=activation_sha,
            ),
            basis=CompatibilityBasis(
                exact_change_manifest_sha256=exact_change_manifest_sha256,
                independent_test_evidence_sha256=independent_test_evidence_sha256,
                independent_code_review_sha256=independent_code_review_sha256,
            ),
            legacy_scope=LegacyScopeReference(
                inventory_file=str(inventory),
                inventory_sha256=sha256_of(inventory_bytes),
                member_count=len(legacy.members),
            ),
        )
    return upgrade


def capture_legacy_key_lookups(
    settings: Settings,
    *,
    inventory: Path,
    key_ttl_seconds: int,
    transport: httpx.BaseTransport | None = None,
    wall_clock: Callable[[], datetime] | None = None,
    timeout_seconds: float = 30.0,
) -> LegacyKeyLookupEvidence:
    """Read-only: prove every listed original key absent on the running origin API.

    Run before the origin API retires, while the settings still name the
    origin runtime. One GET per member; a key the provider knows (200) or any
    answer other than a closed 404 refuses, because an accepted or unknown
    submission never moves to a new runtime. ``key_ttl_seconds`` is the
    installed API's actual key lifetime, read by root from that API.
    """

    from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import (
        lookup_request_exact_bytes_v2,
        normalize_api_origin_v2,
        response_identity_v2,
        task_lookup_url_v2,
        validate_absence_payload_v2,
    )

    runtime = settings.disclosure_mineru_runtime_bundle_identity_sha256
    if runtime is None or settings.disclosure_mineru_api_url is None:
        raise MinerUDeploymentGateError("key lookups need the configured origin runtime and API URL")
    if type(key_ttl_seconds) is not int or key_ttl_seconds < 1:
        raise MinerUDeploymentGateError("the provider key lifetime must be a positive integer")
    with _upgrade_step("key lookup inventory"):
        inventory_bytes, _ = read_owner_only_evidence(
            inventory, label="legacy scope inventory", max_bytes=MAX_INVENTORY_BYTES,
        )
        legacy = decode_legacy_scope_inventory(inventory_bytes)
    open_members = [
        item.attempt_id for item in legacy.members
        if item.observed_state != "prepared" or item.accepted_submission_sha256 is not None
    ]
    if open_members:
        raise MinerUDeploymentGateError(
            f"{len(open_members)} legacy member(s) are past prepared; their submission is not provably absent "
            f"(first {open_members[:5]})"
        )
    clock = wall_clock or (lambda: datetime.now(UTC))
    api_origin = normalize_api_origin_v2(settings.disclosure_mineru_api_url)
    lookups = []
    with httpx.Client(transport=transport, timeout=httpx.Timeout(timeout_seconds), trust_env=False) as client:
        for member in legacy.members:
            url = task_lookup_url_v2(api_origin=api_origin, idempotency_key=member.client_submit_key)
            response = client.get(url)
            exact = response.content
            if response.status_code != 404:
                raise MinerUDeploymentGateError(
                    f"original key of {member.attempt_id} is not absent (HTTP {response.status_code})"
                )
            with _upgrade_step("original key absence"):
                validate_absence_payload_v2(exact)
                response_sha256, response_bytes = response_identity_v2(exact)
            request = lookup_request_exact_bytes_v2(api_origin=api_origin, idempotency_key=member.client_submit_key)
            lookups.append(LegacyKeyLookup(
                attempt_id=member.attempt_id,
                client_submit_key=member.client_submit_key,
                lookup_request_sha256=sha256_of(request),
                http_status=404,
                response_sha256=response_sha256,
                response_byte_count=response_bytes,
                observed_at_utc=clock().astimezone(UTC).isoformat(),
            ))
    return LegacyKeyLookupEvidence(
        api_runtime_identity_sha256=runtime, key_ttl_seconds=key_ttl_seconds, lookups=tuple(lookups),
    )


def build_qualified_runtime_upgrade_proposal(
    settings: Settings,
    *,
    release_manifest: Path,
    runtime_bundle: Path,
    origin_release_manifest: Path,
    origin_runtime_bundle: Path,
    origin_process_profile: Path,
    origin_activation: Path,
    inventory: Path,
    key_lookups: Path,
    exact_change_manifest_sha256: str,
    independent_test_evidence_sha256: str,
    independent_code_review_sha256: str,
) -> QualifiedRuntimeUpgrade:
    """Assemble the qualified runtime proposal for independent review.

    Run after the target runtime has its own new qualification (Qnew): the
    environment names the target runtime, process profile, capacity and
    activation, and the smoke/canary/held-out settings name Qnew's files. The
    origin files are the archived release, runtime bundle, process profile
    and activation every inventory member was frozen under. The moved manifest
    fields are computed here and verified again, exactly, at every boot.
    """

    if settings.execution_upgrade_configured:
        raise MinerUDeploymentGateError("a proposal is assembled before any upgrade is configured")
    capacity = configured_mineru_capacity(settings, None)
    if not isinstance(capacity, MineruCapacityConfigV2):
        raise MinerUDeploymentGateError("a qualified result runtime needs the explicit result-storage capacity")
    activation_path = _require_settings_path(
        settings.disclosure_mineru_stream_pressure_config, label="current stream activation",
    )
    activation_sha = settings.disclosure_mineru_stream_pressure_config_sha256
    if activation_sha is None:
        raise MinerUDeploymentGateError("current stream activation SHA-256 is required")
    uid = os.getuid()
    with _upgrade_step("qualified proposal assembly"):
        release_bytes, _ = read_owner_only_evidence(
            release_manifest, label="execution release manifest", max_bytes=MAX_RELEASE_MANIFEST_BYTES,
        )
        release = decode_execution_release_manifest(release_bytes)
        verify_execution_release(release, name="the target release")
        bundle_bytes, _ = read_owner_only_evidence(
            runtime_bundle, label="target runtime bundle", max_bytes=_MAX_EVIDENCE_BYTES,
        )
        target_wrapper = _json_object(bundle_bytes, label="target runtime bundle")
        target_runtime = target_wrapper["identity_sha256"]
        if settings.disclosure_mineru_runtime_bundle_identity_sha256 != target_runtime:
            raise MinerUDeploymentGateError("configured runtime is not the target runtime bundle")
        staged = load_staged_v4_settings()
        target_profile = load_mineru_process_profile(
            staged.process_profile_file, expected_sha256=staged.process_profile_sha256, expected_owner_uid=uid,
        ).profile
        target_worker = _active_worker_profile(target_profile, settings)
        smoke_path = _require_settings_path(settings.disclosure_mineru_smoke_receipt, label="smoke receipt")
        cache_path = _require_settings_path(settings.disclosure_mineru_canary_cache, label="canary cache")
        validation_path = _require_settings_path(
            settings.disclosure_mineru_validation_receipt, label="held-out validation receipt",
        )
        smoke_bytes, _ = read_owner_only_evidence(smoke_path, label="new qualification smoke",
                                                  max_bytes=_MAX_EVIDENCE_BYTES)
        cache_bytes, _ = read_owner_only_evidence(cache_path, label="new qualification canary",
                                                  max_bytes=_MAX_EVIDENCE_BYTES)
        validation_bytes, _ = read_owner_only_evidence(
            validation_path, label="new qualification held-out", max_bytes=_MAX_VALIDATION_EVIDENCE_BYTES,
        )
        qualified_manifest = _json_object(smoke_bytes, label="new qualification smoke")["runtime_manifest"]
        if canonical_payload_sha256(qualified_manifest) != target_runtime:
            raise MinerUDeploymentGateError("the configured qualification does not qualify the target runtime")

        origin_release_bytes, _ = read_owner_only_evidence(
            origin_release_manifest, label="recovery origin execution release manifest",
            max_bytes=MAX_RELEASE_MANIFEST_BYTES,
        )
        origin_release = decode_execution_release_manifest(origin_release_bytes)
        origin_bundle_bytes, _ = read_owner_only_evidence(
            origin_runtime_bundle, label="recovery origin runtime bundle", max_bytes=_MAX_EVIDENCE_BYTES,
        )
        origin_wrapper = _json_object(origin_bundle_bytes, label="recovery origin runtime bundle")
        origin_runtime = origin_wrapper["identity_sha256"]
        origin_manifest = origin_wrapper["manifest"]
        if origin_manifest["client"]["writer_code_sha256"] != origin_release.writer_code_sha256:
            raise MinerUDeploymentGateError("recovery origin runtime bundle is not the origin release's writer")
        target_manifest = target_wrapper["manifest"]
        changes = tuple(sorted(
            (section, name)
            for section, origin_value in origin_manifest.items()
            if type(origin_value) is dict and type(target_manifest.get(section)) is dict
            for name in set(origin_value) | set(target_manifest[section])
            if origin_value.get(name) != target_manifest[section].get(name)
        ))
        origin_profile = load_mineru_process_profile(
            origin_process_profile,
            expected_sha256=_observed_sha256(
                origin_process_profile, label="recovery origin process profile", max_bytes=_MAX_EVIDENCE_BYTES,
            ),
            expected_owner_uid=uid,
        ).profile
        if origin_profile.runtime_bundle_identity_sha256 != origin_runtime:
            raise MinerUDeploymentGateError("recovery origin process profile is not bound to the origin runtime")
        origin_worker = derive_parent_worker_profile(target_worker, parent_process_profile_sha256=origin_profile.sha256)
        origin_activation_sha = _observed_sha256(
            origin_activation, label="recovery origin stream activation", max_bytes=_MAX_EVIDENCE_BYTES,
        )
        read_mineru_capacity_file(origin_activation, expected_sha256=origin_activation_sha, expected_owner_uid=uid)
        inventory_bytes, _ = read_owner_only_evidence(
            inventory, label="legacy scope inventory", max_bytes=MAX_INVENTORY_BYTES,
        )
        legacy = decode_legacy_scope_inventory(inventory_bytes)
        strays = [
            item.attempt_id for item in legacy.members
            if (item.runtime_epoch_sha256, item.process_profile_sha256, item.worker_profile_sha256)
            != (origin_runtime, origin_profile.sha256, origin_worker.sha256)
        ]
        if strays:
            raise MinerUDeploymentGateError(
                f"{len(strays)} legacy member(s) are not bound to the recovery origin runtime/profile pair "
                f"(first {strays[:5]})"
            )
        lookup_bytes, _ = read_owner_only_evidence(
            key_lookups, label="legacy key lookup evidence", max_bytes=MAX_KEY_LOOKUP_BYTES,
        )
        lookup_evidence = decode_legacy_key_lookup_evidence(lookup_bytes)
        upgrade = QualifiedRuntimeUpgrade(
            target_qualification=ParentQualification(
                runtime_identity_sha256=target_runtime,
                writer_code_sha256=release.writer_code_sha256,
                smoke_receipt_sha256=sha256_of(smoke_bytes),
                canary_cache_sha256=sha256_of(cache_bytes),
                validation_receipt_sha256=sha256_of(validation_bytes),
                process_profile_file=str(staged.process_profile_file),
                process_profile_sha256=target_profile.sha256,
                worker_profile_sha256=target_worker.sha256,
                stream_activation_file=str(activation_path),
                stream_activation_sha256=activation_sha,
                qualified_at_utc=_json_object(cache_bytes, label="new qualification canary")["passed_at_utc"],
                service_epoch_sha256=_json_object(validation_bytes, label="new qualification held-out")[
                    "epoch_after"]["receipt"]["service_epoch_sha256"],
            ),
            recovery_origin=RecoveryOrigin(
                release_manifest_file=str(origin_release_manifest),
                release_manifest_sha256=sha256_of(origin_release_bytes),
                source_revision=origin_release.source_revision,
                writer_code_sha256=origin_release.writer_code_sha256,
                runtime_bundle_file=str(origin_runtime_bundle),
                runtime_bundle_sha256=sha256_of(origin_bundle_bytes),
                runtime_identity_sha256=origin_runtime,
                process_profile_file=str(origin_process_profile),
                process_profile_sha256=origin_profile.sha256,
                worker_profile_sha256=origin_worker.sha256,
                capacity_config_sha256=origin_manifest["orchestrator"]["capacity_config_sha256"],
                stream_activation_file=str(origin_activation),
                stream_activation_sha256=origin_activation_sha,
            ),
            current=CurrentExecution(
                release_manifest_file=str(release_manifest),
                release_manifest_sha256=sha256_of(release_bytes),
                source_revision=release.source_revision,
                writer_code_sha256=release.writer_code_sha256,
                runtime_bundle_file=str(runtime_bundle),
                runtime_bundle_sha256=sha256_of(bundle_bytes),
                runtime_identity_sha256=target_runtime,
                process_profile_sha256=target_profile.sha256,
                worker_profile_sha256=target_worker.sha256,
                capacity_config_sha256=capacity.sha256,
                stream_activation_sha256=activation_sha,
            ),
            runtime_changes=changes,
            basis=CompatibilityBasis(
                exact_change_manifest_sha256=exact_change_manifest_sha256,
                independent_test_evidence_sha256=independent_test_evidence_sha256,
                independent_code_review_sha256=independent_code_review_sha256,
            ),
            legacy_scope=LegacyScopeReference(
                inventory_file=str(inventory),
                inventory_sha256=sha256_of(inventory_bytes),
                member_count=len(legacy.members),
            ),
            key_lookups=KeyLookupEvidenceReference(
                evidence_file=str(key_lookups),
                evidence_sha256=sha256_of(lookup_bytes),
                key_ttl_seconds=lookup_evidence.key_ttl_seconds,
            ),
        )
    return upgrade


# ---------------------------------------------------------------------------
# Read-only deployment preflight
# ---------------------------------------------------------------------------

LiveOwnerProbe = Callable[[str, str], dict[str, Any]]


def _default_live_owner(api_url: str, capacity_sha256: str) -> dict[str, Any]:
    from disclosure_anchor.adapters.runtime.mineru_release_binding import sample_live_identity

    return sample_live_identity(api_url.rstrip("/"), capacity_sha256=capacity_sha256).owner


def _preflight_resolver(settings: Settings, worker_profile: StagedWorkerProfileV4,
                        execution: VerifiedQualifiedExecution | None) -> Any:
    from disclosure_anchor.adapters.parsers.mineru_medium.v4_stage_input_resolver import (
        ProductionV4StageInputResolver,
    )
    from disclosure_anchor.adapters.parsers.pdf_text_observation import observe_pdf_text_rectangles
    from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
    from disclosure_anchor.adapters.storage.provider_document_source import ProviderDocumentFileSource

    def refuse_unit_of_work() -> Any:
        raise RuntimeError("the deployment preflight resolver never opens a unit of work")

    paths = FileStorePathBuilder(settings)
    return ProductionV4StageInputResolver(
        uow_factory=refuse_unit_of_work,
        paths=paths,
        provider_source=ProviderDocumentFileSource(paths, text_reader=observe_pdf_text_rectangles),
        worker_profile=worker_profile,
        legacy_execution=execution,
    )


def run_deployment_preflight(
    settings: Settings,
    *,
    engine_factory: Callable[[], Engine],
    prepared_key_ttl_seconds: int | None = None,
    live_owner: LiveOwnerProbe | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Technical install eligibility; never a start authorization.

    Same gate, same resolver identity closure and the same scope check as the
    worker, over one READ ONLY snapshot. No DB write, claim, recovery,
    maintenance, model call, remote POST or stop record.
    """

    from disclosure_anchor.adapters.runtime.worker_stop_control import observe_worker_control

    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    blockers: list[str] = []
    report: dict[str, Any] = {
        "contract_version": PREFLIGHT_CONTRACT,
        "observed_at": current_time.isoformat(),
        "ready_to_install": False,
        "qualification_origin": None,
        "release_manifest_sha256": None,
        "writer_code_sha256": None,
        "parent_runtime_identity_sha256": None,
        "current_runtime_identity_sha256": settings.disclosure_mineru_runtime_bundle_identity_sha256,
        "process_profile_sha256": None,
        "worker_profile_sha256": None,
        "stream_activation_sha256": settings.disclosure_mineru_stream_pressure_config_sha256,
        "native_identity_match": None,
        "legacy_scope": None,
        "prepared_key_status": "not_applicable",
        "operational_control_state": None,
        "blockers": blockers,
    }
    # Like the worker start gate: a recorded, invalid, unverifiable or
    # supervisor-held stop refuses before any checker, DB or native probe.
    # OPERATOR_DISABLED stays technical eligibility; the installer's explicit
    # confirmation decides it.
    try:
        control = observe_worker_control(settings)
    except Exception as exc:  # noqa: BLE001 - an unreadable control plane is a blocker
        blockers.append(f"operational control unreadable ({type(exc).__name__})")
        return _finish(report)
    report["operational_control_state"] = control.state
    if control.state not in {"RUNNABLE", "OPERATOR_DISABLED"}:
        blockers.append(f"operational control is {control.state}")
        return _finish(report)
    staged_mode = settings.worker_parse_execution_mode == "staged-v4"
    worker_profile: StagedWorkerProfileV4 | None = None
    try:
        report["writer_code_sha256"] = writer_code_digest()
        process_profile: MineruProcessProfile | None = None
        if staged_mode:
            staged = load_staged_v4_settings()
            process_profile = load_mineru_process_profile(
                staged.process_profile_file, expected_sha256=staged.process_profile_sha256,
                expected_owner_uid=os.getuid(),
            ).profile
            worker_profile = _active_worker_profile(process_profile, settings)
            report["process_profile_sha256"] = process_profile.sha256
            report["worker_profile_sha256"] = worker_profile.sha256
        # Exactly the resident loop's static checker construction.
        checker = MinerUDeploymentChecker(
            settings, parse_enabled=True, process_profile=process_profile, accept_execution_upgrade=True,
        )
        if checker.qualification_origin is None:
            raise MinerUDeploymentGateError("deployment qualification was not verified")
    except Exception as exc:  # noqa: BLE001 - reported as a blocker, never raised past the report
        blockers.append(f"deployment qualification: {exc}")
        return _finish(report)
    execution = checker.verified_execution
    report["qualification_origin"] = checker.qualification_origin
    if execution is not None:
        report.update(execution.summary())
        if not isinstance(execution.upgrade, LocalExecutionUpgrade):
            # v2 and qualified upgrades name their roles; no ambiguous parent.
            report.pop("parent_runtime_identity_sha256", None)
    if worker_profile is not None:
        # Only the staged resident reopens V4 heads; the legacy mode has no
        # V4 resolver, so its scope is not examined.
        _preflight_scope(report, blockers, settings, engine_factory, worker_profile, execution,
                         prepared_key_ttl_seconds, current_time)
    _preflight_native_identity(report, blockers, settings, live_owner or _default_live_owner)
    return _finish(report)


def _finish(report: dict[str, Any]) -> dict[str, Any]:
    report["ready_to_install"] = not report["blockers"]
    return report


def _preflight_scope(
    report: dict[str, Any], blockers: list[str], settings: Settings, engine_factory: Callable[[], Engine],
    worker_profile: StagedWorkerProfileV4, execution: VerifiedQualifiedExecution | None,
    ttl_seconds: int | None, current_time: datetime,
) -> None:
    try:
        engine = engine_factory()
    except Exception as exc:  # noqa: BLE001
        blockers.append(f"legacy scope database unavailable ({type(exc).__name__})")
        return
    try:
        with read_only_repository(engine) as (repository, observed_at, created_at_by_attempt):
            heads = observe_unresolved_heads(repository)
            resolver = _preflight_resolver(settings, worker_profile, execution)
            refused: list[str] = []
            for head in heads:
                try:
                    resolver.inspect_frozen_identity(head)
                except (ValueError, RuntimeError) as exc:
                    refused.append(f"{head.attempt_id}: {exc}")
            scope: dict[str, Any] = {
                "observed_at": observed_at.isoformat(),
                "unresolved_count": len(heads),
                "unresolved_state_counts": _state_counts(heads),
                "staged_prepared_count": repository.count_staged_prepared_heads(),
                "identity_refusals": len(refused),
            }
            if execution is not None:
                scope["inventory_sha256"] = execution.upgrade.legacy_scope.inventory_sha256
                scope["member_count"] = len(execution.inventory.members)
                members_open = any(execution.is_member(head.attempt_id) for head in heads)
                strangers = [
                    head.attempt_id for head in heads
                    if not execution.is_member(head.attempt_id)
                    and (members_open or not _bound_to_current(execution, head)
                         or not _post_capture_head(execution, head, observed_at, created_at_by_attempt))
                ]
                scope["non_member_unresolved"] = strangers[:_PREFLIGHT_DETAIL_LIMIT]
                scope["non_member_unresolved_count"] = len(strangers)
                try:
                    scope.update(verify_legacy_scope(
                        repository, execution, observed_at=observed_at, unresolved=heads,
                        created_at_by_attempt=created_at_by_attempt,
                    ).to_payload())
                except (ValueError, RuntimeError) as exc:
                    blockers.append(f"legacy scope: {exc}")
            report["legacy_scope"] = scope
            for line in refused[:_PREFLIGHT_DETAIL_LIMIT]:
                blockers.append(f"unresolved head refused: {line}")
            if len(refused) > _PREFLIGHT_DETAIL_LIMIT:
                blockers.append(f"... {len(refused) - _PREFLIGHT_DETAIL_LIMIT} more unresolved head refusal(s)")
            if execution is not None and isinstance(execution.upgrade, QualifiedRuntimeUpgrade):
                # Keys were proven absent under the approved lifetime; a
                # different caller lifetime never re-classifies them.
                approved_ttl = execution.upgrade.key_lookups.key_ttl_seconds
                if ttl_seconds is not None and ttl_seconds != approved_ttl:
                    blockers.append(
                        f"supplied key lifetime {ttl_seconds}s differs from the approved original-key "
                        f"lookup lifetime {approved_ttl}s"
                    )
                ttl_seconds = approved_ttl if ttl_seconds is not None else None
            _prepared_key_status(report, blockers, heads, ttl_seconds, current_time)
    except Exception as exc:  # noqa: BLE001
        blockers.append(f"legacy scope snapshot failed ({type(exc).__name__}: {exc})")
    finally:
        engine.dispose()


def _post_capture_head(
    execution: VerifiedQualifiedExecution, head: Any, observed_at: datetime,
    created_at_by_attempt: dict[str, datetime],
) -> bool:
    try:
        require_post_capture_head(
            execution, head.attempt_id, observed_at=observed_at,
            created_at_by_attempt=created_at_by_attempt,
        )
    except ValueError:
        return False
    return True


def _bound_to_current(execution: VerifiedQualifiedExecution, head: Any) -> bool:
    try:
        execution.require_current_execution(head)
    except ValueError:
        return False
    return True


def _state_counts(heads: tuple[Any, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for head in heads:
        counts[head.state] = counts.get(head.state, 0) + 1
    return dict(sorted(counts.items()))


def _prepared_key_status(
    report: dict[str, Any], blockers: list[str], heads: tuple[Any, ...],
    ttl_seconds: int | None, current_time: datetime,
) -> None:
    """Key ages of never-accepted heads; the deployed key lifetime is external.

    Without an explicitly supplied lifetime the status is ``unverified`` and
    blocks: an unknown lifetime never defaults to POSTable.
    """

    epochs = [
        head.execution_spec.prepared_submission.submission_epoch_unix
        for head in heads
        if head.state in {"prepared", "reconciling"} and head.execution_spec is not None
    ]
    if not epochs:
        report["prepared_key_status"] = "not_applicable"
        return
    ages = [int(current_time.timestamp()) - epoch for epoch in epochs]
    report["prepared_key_count"] = len(ages)
    report["prepared_key_oldest_age_seconds"] = max(ages)
    if ttl_seconds is None:
        report["prepared_key_status"] = "unverified"
        blockers.append("prepared key lifetime is unverified (pass --prepared-key-ttl-seconds from the deployed value)")
        return
    expired = sum(1 for age in ages if age >= ttl_seconds)
    report["prepared_key_ttl_seconds"] = ttl_seconds
    report["prepared_key_min_remaining_seconds"] = ttl_seconds - max(ages)
    if expired:
        report["prepared_key_status"] = "expired"
        report["prepared_key_expired_count"] = expired
        blockers.append(f"{expired} prepared submission key(s) exceeded the supplied lifetime")
    else:
        report["prepared_key_status"] = "verified"


def _preflight_native_identity(
    report: dict[str, Any], blockers: list[str], settings: Settings, live_owner: LiveOwnerProbe,
) -> None:
    path = settings.disclosure_mineru_stream_pressure_config
    sha = settings.disclosure_mineru_stream_pressure_config_sha256
    api_url = settings.disclosure_mineru_api_url
    capacity = configured_mineru_capacity(settings, None)
    if path is None or sha is None or capacity is None:
        report["native_identity_match"] = None
        return
    try:
        activation = _json_object(
            read_mineru_capacity_file(path, expected_sha256=sha, expected_owner_uid=os.getuid()),
            label="stream activation",
        )
        if api_url is None:
            raise MinerUDeploymentGateError("API URL is not configured")
        owner = live_owner(api_url, capacity.sha256)
    except Exception as exc:  # noqa: BLE001 - live identity unavailable is a blocker
        report["native_identity_match"] = False
        blockers.append(f"native identity unavailable ({type(exc).__name__}: {exc})")
        return
    matched = owner == activation.get("owner")
    report["native_identity_match"] = matched
    if not matched:
        blockers.append("live API owner differs from the configured activation owner (new native epoch)")


def render_preflight_terminal(report: dict[str, Any]) -> str:
    lines = [
        f"deployment preflight: ready_to_install={str(report['ready_to_install']).lower()} "
        f"origin={report['qualification_origin']} control={report['operational_control_state']}",
        f"  release={report['release_manifest_sha256']} writer={report['writer_code_sha256']}",
    ]
    if report.get("upgrade_contract_version") == QUALIFIED_UPGRADE_CONTRACT:
        lines.extend((
            f"  new qualification: qualified_at={report['qualification_qualified_at_utc']} "
            f"runtime={report['qualification_runtime_identity_sha256']}",
            f"  recovery origin: release={report['origin_release_manifest_sha256']} "
            f"runtime={report['origin_runtime_identity_sha256']} writer={report['origin_writer_code_sha256']} "
            f"worker_profile={report['origin_worker_profile_sha256']}",
            f"  target: runtime={report['current_runtime_identity_sha256']} "
            f"changes={','.join(report['runtime_changes'])}",
            f"  original keys: evidence={report['key_lookup_evidence_sha256']} ttl={report['key_ttl_seconds']}",
        ))
    elif report.get("upgrade_contract_version") == UPGRADE_CONTRACT_V2:
        lines.extend((
            f"  qualification anchor: qualified_at={report['anchor_qualified_at_utc']} "
            f"runtime={report['anchor_runtime_identity_sha256']} writer={report['anchor_writer_code_sha256']}",
            f"  recovery origin: release={report['origin_release_manifest_sha256']} "
            f"runtime={report['origin_runtime_identity_sha256']} writer={report['origin_writer_code_sha256']} "
            f"worker_profile={report['origin_worker_profile_sha256']}",
            f"  target: runtime={report['current_runtime_identity_sha256']}",
        ))
    else:
        lines.append(
            f"  runtime={report.get('parent_runtime_identity_sha256')}->{report['current_runtime_identity_sha256']}"
        )
    lines.extend((
        f"  process_profile={report['process_profile_sha256']} worker_profile={report['worker_profile_sha256']}",
        f"  native_identity_match={report['native_identity_match']} prepared_keys={report['prepared_key_status']}",
        f"  legacy_scope={report['legacy_scope']}",
    ))
    lines.extend(f"  BLOCKER {item}" for item in report["blockers"])
    return "\n".join(lines)


__all__ = [
    "BOOT_RECEIPT_CONTRACT",
    "BOOT_RECEIPT_QUALIFIED_CONTRACT",
    "BOOT_RECEIPT_V2_CONTRACT",
    "DerivedLocalUpgrade",
    "PREFLIGHT_CONTRACT",
    "SERVICE_ROOT",
    "build_execution_release_manifest",
    "build_qualified_runtime_upgrade_proposal",
    "build_upgrade_proposal",
    "build_upgrade_proposal_v2",
    "capture_legacy_key_lookups",
    "encode_legacy_key_lookup_evidence",
    "derive_local_upgrade",
    "recheck_execution_release",
    "release_files",
    "render_preflight_terminal",
    "require_legacy_scope",
    "run_deployment_preflight",
    "verify_configured_execution_upgrade",
    "verify_execution_release",
    "verify_local_execution_upgrade",
    "worker_python_identity",
    "write_boot_receipt",
]
