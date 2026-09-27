"""Independent U01 bundles: one reviewed Q0 -> E1 edge over one legacy inventory.

A bundle is derived from an installed parent (``tests._f5_upgrade_q0_fixture``) by this test
author's own code, following the approved plan, never by the product builders:

* W1 is the legacy 42-member digest of this tree (independent recomputation);
* M1 is M0 with only ``client.writer_code_sha256`` replaced by W1; R1 is its runtime identity;
* P1 is P0 with only its runtime reference moved to R1; WP1 is WP0 with only its process-profile
  reference moved to P1;
* A1 is A0 with only its two runtime references moved to R1;
* E1 lists every regular file of the declared release scope (the whole ``src/disclosure_anchor``
  package, the files directly under ``scripts/`` and ``scripts/launchd/``; bytecode caches
  excluded) plus the interpreter's package set;
* the inventory, the proposal and its GO review are hand-encoded from the contract field names.

Every file is an owner-only 0600 file under the parent's resolved temporary root. The only replayed
external port is the MinerU client metadata, from the parent's own closed client section. Mutations
return a new bundle whose changed files are new files, re-pinned (and re-reviewed where stated), so
each counterexample isolates exactly one fact.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import copy
from dataclasses import dataclass, replace
from datetime import datetime
import importlib.metadata
import itertools
import json
import os
from pathlib import Path
import platform
from typing import Any
from unittest import mock

from disclosure_anchor.adapters.runtime import mineru_deployment_gate as gate
from disclosure_anchor.adapters.runtime import mineru_execution_upgrade as upgrade_runtime
from disclosure_anchor.application.contracts.mineru_process_profile import MineruProcessProfile
from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4
from disclosure_anchor.settings import Settings
from tests._f5_upgrade_q0_fixture import SERVICE_ROOT, ParentQ0, legacy_writer_digest, sha256_bytes


UPGRADE_CONTRACT = "worker-local-execution-upgrade.v1"
REVIEW_CONTRACT = "worker-local-execution-upgrade-review.v1"
RELEASE_CONTRACT = "worker-execution-release.v1"
INVENTORY_CONTRACT = "worker-legacy-scope-inventory.v1"
TRANSITION_KIND = "local_operational_compatible"
RELEASE_TREES = ("src/disclosure_anchor", "scripts/launchd")
RELEASE_FLAT = ("scripts",)
ARCHIVE_MEMBER_COUNT_LIMIT = 100_000
SOURCE_REVISION = "independent-acceptance-candidate"
GPU_METRICS_URL = "http://127.0.0.1:30004/metrics"  # the audited SSH forward (never contacted)
_UNIQUE = itertools.count(1)


def exact_json(value: object) -> bytes:
    """The runtime-identity canonical form: sorted keys, compact separators, UTF-8."""

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def runtime_identity(manifest: dict[str, Any]) -> str:
    return sha256_bytes(exact_json(manifest))


def release_scope_files(service_root: Path = SERVICE_ROOT) -> list[dict[str, Any]]:
    """Every regular file of the declared release scope, by this author's own scan."""

    found: dict[str, Path] = {}
    for tree in RELEASE_TREES:
        for directory, dirnames, filenames in os.walk(service_root / tree):
            dirnames[:] = [name for name in dirnames if name != "__pycache__"]
            for name in filenames:
                path = Path(directory, name)
                found[path.relative_to(service_root).as_posix()] = path
    for flat in RELEASE_FLAT:
        for entry in (service_root / flat).iterdir():
            if entry.is_file():
                found[entry.relative_to(service_root).as_posix()] = entry
    files = []
    for relpath in sorted(found):
        payload = found[relpath].read_bytes()
        files.append({"path": relpath, "sha256": sha256_bytes(payload), "bytes": len(payload)})
    return files


def interpreter_package_identity() -> tuple[str, str]:
    """(python version, package-set identity) of this interpreter, recomputed independently."""

    packages = sorted(
        f"{dist.metadata['Name']}=={dist.version}"
        for dist in importlib.metadata.distributions()
        if dist.metadata["Name"]
    )
    version = platform.python_version()
    return version, sha256_bytes(exact_json({"python_version": version, "packages": packages}))


@contextmanager
def frozen_wall_clock(moment: datetime) -> Iterator[None]:
    """Pin the default wall clock (``datetime.now``) of the gate and the upgrade module.

    The resident checker and the preflight read the wall clock themselves; a parent's original
    freshness window is then judged at ``moment`` instead of the day the suite happens to run.
    """

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return moment if tz is None else moment.astimezone(tz)

    with mock.patch.object(gate, "datetime", Frozen), mock.patch.object(upgrade_runtime, "datetime", Frozen):
        yield


def write_private(path: Path, payload: bytes) -> Path:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
    path.chmod(0o600)
    return path


def synthetic_member(
    parent: ParentQ0, index: int, *, state: str = "prepared", lifecycle_version: int = 0,
) -> dict[str, Any]:
    """One well-formed inventory member bound to the parent's R0/P0/WP0 (verifier-level only)."""

    tag = f"{index:07d}"

    def digest(label: str) -> str:
        return sha256_bytes(f"{label}:{tag}".encode())

    return {
        "attempt_id": f"rpa_f5_member_{tag}",
        "document_id": f"doc_f5_member_{tag}",
        "processing_run_id": f"run_f5_member_{tag}",
        "attempt_generation": 1,
        "fence_identity": f"fence_f5_member_{tag}",
        "h0_checkpoint_sha256": digest("h0"),
        "execution_spec_sha256": digest("spec"),
        "source_pdf_sha256": digest("source"),
        "parser_target_sha256": digest("target"),
        "request_sha256": digest("request"),
        "runtime_epoch_sha256": parent.runtime_identity,
        "client_submit_key": f"key_f5_member_{tag}",
        "submission_epoch_unix": 1_790_000_000 + index,
        "process_profile_sha256": parent.process_profile.sha256,
        "worker_profile_sha256": parent.worker_profile.sha256,
        "observed_state": state,
        "observed_lifecycle_version": lifecycle_version,
        "observed_checkpoint_sha256": digest("h0") if lifecycle_version == 0 else digest(f"v{lifecycle_version}"),
        "accepted_submission_sha256": None if state in {"prepared", "reconciling"} else digest("accepted"),
    }


def inventory_payload(members: list[dict[str, Any]], *, captured_at_utc: str) -> dict[str, Any]:
    return {
        "contract_version": INVENTORY_CONTRACT,
        "captured_at_utc": captured_at_utc,
        "member_count": len(members),
        "members": sorted(members, key=lambda item: item["attempt_id"]),
    }


def current_worker_profile(parent: ParentQ0, process_profile: MineruProcessProfile) -> StagedWorkerProfileV4:
    """WP1: WP0 with only its process-profile reference moved."""

    return replace(parent.worker_profile, process_profile_sha256=process_profile.sha256)


@dataclass(frozen=True)
class U01Bundle:
    parent: ParentQ0
    settings: Settings
    environment: dict[str, str]
    current_writer: str
    current_manifest: dict[str, Any]
    current_runtime: str
    process_profile: MineruProcessProfile
    process_profile_path: Path
    worker_profile: StagedWorkerProfileV4
    activation: dict[str, Any]
    activation_path: Path
    release: dict[str, Any]
    release_path: Path
    runtime_bundle_path: Path
    inventory: dict[str, Any]
    inventory_path: Path
    proposal: dict[str, Any]
    proposal_path: Path
    review: dict[str, Any]
    review_path: Path

    @property
    def root(self) -> Path:
        return self.parent.root

    @property
    def proposal_sha256(self) -> str:
        return sha256_bytes(self.proposal_path.read_bytes())

    @property
    def review_sha256(self) -> str:
        return sha256_bytes(self.review_path.read_bytes())

    @property
    def inventory_sha256(self) -> str:
        return sha256_bytes(self.inventory_path.read_bytes())

    @contextmanager
    def active(self) -> Iterator[None]:
        """The staged bootstrap environment, the parent's replayed client metadata and its clock."""

        with (
            mock.patch.dict(os.environ, self.environment),
            mock.patch.object(gate, "client_bundle_identity", return_value=self.parent.client),
            mock.patch.object(upgrade_runtime, "client_bundle_identity", return_value=self.parent.client),
            frozen_wall_clock(self.parent.clock),
        ):
            yield

    def verify(
        self, *, settings: Settings | None = None, process_profile: MineruProcessProfile | None = None,
        now: datetime | None = None,
    ) -> gate.VerifiedMinerUDeployment:
        with self.active():
            evidence = gate.verify_mineru_deployment_gate(
                settings or self.settings, parse_enabled=True,
                process_profile=process_profile or self.process_profile,
                now=now or self.parent.clock, accept_execution_upgrade=True,
            )
        assert evidence is not None
        return evidence

    def checker(self, *, accept_execution_upgrade: bool = True) -> gate.MinerUDeploymentChecker:
        with self.active():
            return gate.MinerUDeploymentChecker(
                self.settings, parse_enabled=True, process_profile=self.process_profile,
                wall_clock=lambda: self.parent.clock, accept_execution_upgrade=accept_execution_upgrade,
            )

    def _file(self, stem: str, payload: bytes) -> Path:
        return write_private(self.root / f"{stem}-{next(_UNIQUE)}.json", payload)

    def _settings(self, **overrides: object) -> Settings:
        return Settings(**dict(self.settings.model_dump(), **overrides))

    # -- mutations: each returns a new bundle -------------------------------------------------

    def with_settings(self, **overrides: object) -> U01Bundle:
        return replace(self, settings=self._settings(**overrides))

    def with_review(self, mutate: Callable[[dict[str, Any]], None]) -> U01Bundle:
        review = copy.deepcopy(self.review)
        mutate(review)
        path = self._file("review", exact_json(review))
        return replace(
            self, review=review, review_path=path,
            settings=self._settings(
                disclosure_worker_execution_upgrade_review_file=path,
                disclosure_worker_execution_upgrade_review_sha256=sha256_bytes(path.read_bytes()),
            ),
        )

    def with_proposal(self, mutate: Callable[[dict[str, Any]], None], *, reviewed: bool = True) -> U01Bundle:
        """Re-pin a changed proposal; ``reviewed`` also issues a GO review of the new bytes."""

        proposal = copy.deepcopy(self.proposal)
        mutate(proposal)
        path = self._file("proposal", exact_json(proposal))
        bundle = replace(
            self, proposal=proposal, proposal_path=path,
            settings=self._settings(
                disclosure_worker_execution_upgrade_file=path,
                disclosure_worker_execution_upgrade_sha256=sha256_bytes(path.read_bytes()),
            ),
        )
        if not reviewed:
            return bundle
        return bundle.with_review(lambda review: review.update(proposal_sha256=bundle.proposal_sha256))

    def with_inventory_payload(self, payload: dict[str, Any], *, count: int | None = None) -> U01Bundle:
        """A new pinned inventory; the proposal (count and hash) and review follow it."""

        path = self._file("inventory", exact_json(payload))
        members = payload.get("members")
        member_count = count if count is not None else (len(members) if isinstance(members, list) else 0)
        bundle = replace(self, inventory=payload, inventory_path=path)
        return bundle.with_proposal(lambda proposal: proposal["legacy_scope"].update(
            inventory_file=str(path), inventory_sha256=sha256_bytes(path.read_bytes()),
            member_count=member_count,
        ))

    def with_members(self, members: list[dict[str, Any]]) -> U01Bundle:
        return self.with_inventory_payload(
            inventory_payload(members, captured_at_utc=self.inventory["captured_at_utc"]),
        )

    def with_release(self, mutate: Callable[[dict[str, Any]], None]) -> U01Bundle:
        """A new pinned E1; the proposal (file, hash, writer, revision) and review follow it."""

        release = copy.deepcopy(self.release)
        mutate(release)
        path = self._file("release", exact_json(release))
        bundle = replace(self, release=release, release_path=path)
        return bundle.with_proposal(lambda proposal: proposal["current_execution"].update(
            release_manifest_file=str(path), release_manifest_sha256=sha256_bytes(path.read_bytes()),
        ))

    def with_runtime_manifest(self, mutate: Callable[[dict[str, Any]], None]) -> U01Bundle:
        """A changed M1 carried consistently: new R1, bundle file, P1, WP1, A1, settings, review."""

        manifest = copy.deepcopy(self.current_manifest)
        mutate(manifest)
        return _current_execution(self, manifest)

    def with_process_profile(self, profile: MineruProcessProfile) -> U01Bundle:
        """A different P1 configured consistently (env, WP1, proposal, review)."""

        path = self._file("process-profile-P1", profile.exact_bytes)
        worker = current_worker_profile(self.parent, profile)
        bundle = replace(
            self, process_profile=profile, process_profile_path=path, worker_profile=worker,
            environment=dict(self.environment, DISCLOSURE_V4_PROCESS_PROFILE_FILE=str(path),
                             DISCLOSURE_V4_PROCESS_PROFILE_SHA256=profile.sha256),
        )
        return bundle.with_proposal(lambda proposal: proposal["current_execution"].update(
            process_profile_sha256=profile.sha256, worker_profile_sha256=worker.sha256,
        ))

    def with_activation(self, activation: dict[str, Any]) -> U01Bundle:
        """A different A1 configured consistently (settings, proposal, review)."""

        path = self._file("activation-A1", exact_json(activation))
        sha = sha256_bytes(path.read_bytes())
        bundle = replace(
            self, activation=activation, activation_path=path,
            settings=self._settings(
                disclosure_mineru_stream_pressure_config=path,
                disclosure_mineru_stream_pressure_config_sha256=sha,
            ),
        )
        return bundle.with_proposal(
            lambda proposal: proposal["current_execution"].update(stream_activation_sha256=sha),
        )


def _current_execution(bundle: U01Bundle, manifest: dict[str, Any]) -> U01Bundle:
    parent = bundle.parent
    current_runtime = runtime_identity(manifest)
    wrapper_path = bundle._file("runtime-bundle-M1", exact_json({"identity_sha256": current_runtime, "manifest": manifest}))
    profile = replace(parent.process_profile, runtime_bundle_identity_sha256=current_runtime)
    profile_path = bundle._file("process-profile-P1", profile.exact_bytes)
    worker = current_worker_profile(parent, profile)
    activation = copy.deepcopy(parent.activation)
    activation["runtime_identity_sha256"] = current_runtime
    activation["policy"]["runtime_identity_sha256"] = current_runtime
    activation_path = bundle._file("activation-A1", exact_json(activation))
    activation_sha = sha256_bytes(activation_path.read_bytes())
    updated = replace(
        bundle,
        current_manifest=manifest, current_runtime=current_runtime, runtime_bundle_path=wrapper_path,
        process_profile=profile, process_profile_path=profile_path, worker_profile=worker,
        activation=activation, activation_path=activation_path,
        environment=dict(bundle.environment, DISCLOSURE_V4_PROCESS_PROFILE_FILE=str(profile_path),
                         DISCLOSURE_V4_PROCESS_PROFILE_SHA256=profile.sha256),
        settings=bundle._settings(
            disclosure_mineru_runtime_bundle_identity_sha256=current_runtime,
            disclosure_mineru_stream_pressure_config=activation_path,
            disclosure_mineru_stream_pressure_config_sha256=activation_sha,
        ),
    )
    return updated.with_proposal(lambda proposal: proposal["current_execution"].update(
        runtime_bundle_file=str(wrapper_path), runtime_bundle_sha256=sha256_bytes(wrapper_path.read_bytes()),
        runtime_identity_sha256=current_runtime, process_profile_sha256=profile.sha256,
        worker_profile_sha256=worker.sha256, stream_activation_sha256=activation_sha,
    ))


def build_u01(
    parent: ParentQ0,
    *,
    members: list[dict[str, Any]],
    captured_at_utc: str | None = None,
    source_revision: str = SOURCE_REVISION,
    inventory_bytes: bytes | None = None,
) -> U01Bundle:
    """The complete independent U01 bundle for ``parent`` and the given legacy members.

    ``inventory_bytes`` pins externally captured inventory bytes (for example the product's
    READ ONLY capture over a scratch DB) instead of encoding ``members``.
    """

    root = parent.root
    writer = legacy_writer_digest()
    manifest = copy.deepcopy(parent.manifest)
    manifest["client"]["writer_code_sha256"] = writer
    python_version, package_set = interpreter_package_identity()
    release = {
        "contract_version": RELEASE_CONTRACT,
        "source_revision": source_revision,
        "writer_code_sha256": writer,
        "worker_python_version": python_version,
        "worker_package_set_sha256": package_set,
        "files": release_scope_files(),
    }
    tag = next(_UNIQUE)
    release_path = write_private(root / f"release-E1-{tag}.json", exact_json(release))
    if inventory_bytes is None:
        inventory = inventory_payload(
            members, captured_at_utc=captured_at_utc or parent.clock.isoformat(),
        )
        inventory_bytes = exact_json(inventory)
    else:
        inventory = json.loads(inventory_bytes)
    inventory_path = write_private(root / f"inventory-{tag}.json", inventory_bytes)
    canary = json.loads(parent.canary_path.read_bytes())
    heldout = json.loads(parent.heldout_path.read_bytes())
    proposal = {
        "contract_version": UPGRADE_CONTRACT,
        "transition_kind": TRANSITION_KIND,
        "parent_qualification": {
            "runtime_identity_sha256": parent.runtime_identity,
            "writer_code_sha256": parent.historical_writer,
            "smoke_receipt_sha256": parent.file_sha256(parent.smoke_path),
            "canary_cache_sha256": parent.file_sha256(parent.canary_path),
            "validation_receipt_sha256": parent.file_sha256(parent.heldout_path),
            "process_profile_file": str(parent.process_profile_path),
            "process_profile_sha256": parent.process_profile.sha256,
            "worker_profile_sha256": parent.worker_profile.sha256,
            "stream_activation_file": str(parent.activation_path),
            "stream_activation_sha256": parent.file_sha256(parent.activation_path),
            "qualified_at_utc": canary["passed_at_utc"],
            "service_epoch_sha256": heldout["epoch_after"]["receipt"]["service_epoch_sha256"],
        },
        "current_execution": {
            "release_manifest_file": str(release_path),
            "release_manifest_sha256": sha256_bytes(release_path.read_bytes()),
            "source_revision": source_revision,
            "writer_code_sha256": writer,
            # The remaining current facts are filled by ``_current_execution``.
            "runtime_bundle_file": str(release_path),
            "runtime_bundle_sha256": sha256_bytes(b"pending"),
            "runtime_identity_sha256": sha256_bytes(b"pending"),
            "process_profile_sha256": sha256_bytes(b"pending"),
            "worker_profile_sha256": sha256_bytes(b"pending"),
            "capacity_config_sha256": parent.capacity_sha256,
            "stream_activation_sha256": sha256_bytes(b"pending"),
        },
        "compatibility_basis": {
            "exact_change_manifest_sha256": sha256_bytes(b"independent exact change manifest"),
            "independent_test_evidence_sha256": sha256_bytes(b"independent test evidence"),
            "independent_code_review_sha256": sha256_bytes(b"independent code review"),
        },
        "legacy_scope": {
            "inventory_file": str(inventory_path),
            "inventory_sha256": sha256_bytes(inventory_bytes),
            "member_count": len(inventory["members"]),
        },
    }
    proposal_path = write_private(root / f"proposal-{tag}.json", exact_json(proposal))
    review = {
        "contract_version": REVIEW_CONTRACT,
        "verdict": "GO",
        "proposal_sha256": sha256_bytes(proposal_path.read_bytes()),
        "reviewer_reference": "independent-acceptance-reviewer",
        "decision_reference": "independent-acceptance-decision",
    }
    review_path = write_private(root / f"review-{tag}.json", exact_json(review))
    settings = Settings(**dict(
        parent.settings.model_dump(),
        worker_parse_execution_mode="staged-v4",
        # Stream pressure (A1) requires both live sources; neither is contacted by the verifier.
        disclosure_gpu_metrics_url=parent.settings.disclosure_gpu_metrics_url or GPU_METRICS_URL,
        disclosure_worker_execution_upgrade_file=proposal_path,
        disclosure_worker_execution_upgrade_sha256=sha256_bytes(proposal_path.read_bytes()),
        disclosure_worker_execution_upgrade_review_file=review_path,
        disclosure_worker_execution_upgrade_review_sha256=sha256_bytes(review_path.read_bytes()),
    ))
    skeleton = U01Bundle(
        parent=parent, settings=settings,
        environment={"DISCLOSURE_V4_ARCHIVE_MEMBER_COUNT_LIMIT": str(ARCHIVE_MEMBER_COUNT_LIMIT)},
        current_writer=writer, current_manifest=manifest, current_runtime="",
        process_profile=parent.process_profile, process_profile_path=parent.process_profile_path,
        worker_profile=parent.worker_profile, activation=parent.activation,
        activation_path=parent.activation_path, release=release, release_path=release_path,
        runtime_bundle_path=release_path, inventory=inventory, inventory_path=inventory_path,
        proposal=proposal, proposal_path=proposal_path, review=review, review_path=review_path,
    )
    return _current_execution(skeleton, manifest)


__all__ = [
    "ARCHIVE_MEMBER_COUNT_LIMIT",
    "INVENTORY_CONTRACT",
    "RELEASE_CONTRACT",
    "REVIEW_CONTRACT",
    "SOURCE_REVISION",
    "UPGRADE_CONTRACT",
    "U01Bundle",
    "build_u01",
    "current_worker_profile",
    "exact_json",
    "frozen_wall_clock",
    "interpreter_package_identity",
    "inventory_payload",
    "release_scope_files",
    "runtime_identity",
    "synthetic_member",
    "write_private",
]
