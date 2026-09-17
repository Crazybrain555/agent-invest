"""Source-only release package: build from an exact tracked commit, verify offline.

``build`` reads product bytes from the working tree of one repository, proves
each byte-for-byte equal to the frozen commit's blob, projects capacity once
and writes a new immutable directory. ``verify`` re-derives everything from the
package's own inputs; it needs neither Git nor a previous package.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from disclosure_anchor.adapters.runtime.exact_file_write import write_new_exact
from disclosure_anchor.application.contracts.closed_document import canonical_bytes, sha256_of
from disclosure_anchor.application.contracts.mineru_capacity_config import (
    MineruCapacityConfig,
    decode_mineru_capacity_config,
)
from disclosure_anchor.application.contracts.mineru_deployment_profile import (
    MineruDeploymentProfile,
    decode_mineru_deployment_profile,
)
from disclosure_anchor.application.contracts.mineru_local_worker_profile import (
    MineruLocalWorkerProfile,
    decode_mineru_local_worker_profile,
)
from disclosure_anchor.application.contracts.mineru_release_manifest import (
    CAPACITY_SOURCE_NAMES,
    NATIVE_M6_PRODUCTION_SOURCES,
    RELEASE_MANIFEST_CONTRACT,
    RELEASE_PROJECTION_CONTRACT,
    TELEMETRY_SUPERVISOR_SOURCES,
    MineruReleaseManifest,
    ReleaseFile,
    capacity_sources_sha256,
    decode_mineru_release_manifest,
    native_source_manifest_sha256,
)
from disclosure_anchor.application.services.mineru_release_plan import (
    parse_compose_yaml,
    resolve_release,
    verify_compose_projection,
)


TRACKED_SCOPE_PREFIX = "services/disclosure_anchor"
MANIFEST_NAME = "release-manifest.json"
COMPOSE_PACKAGE_PATH = "compose/mineru-windows.compose.yaml"
CAPACITY_INPUT_PATH = "inputs/capacity-config.json"
DEPLOYMENT_INPUT_PATH = "inputs/deployment-profile.json"
LOCAL_INPUT_PATH = "inputs/local-worker-profile.json"
API_CONTEXT_CAPACITY_PATH = "api-context/capacity-config.json"
_COMPAT = "scripts/windows/mineru_heap_trim_compat"
# Package path -> tracked path (relative to the service directory).
RELEASE_SOURCE_FILES: dict[str, str] = {
    "api-context/Dockerfile": f"{_COMPAT}/Dockerfile",
    "api-context/patch_mineru_344.py": f"{_COMPAT}/patch_mineru_344.py",
    "api-context/agent_task_protocol_v2.py": f"{_COMPAT}/agent_task_protocol_v2.py",
    "api-context/agent_capacity_config.py": "src/disclosure_anchor/application/contracts/mineru_capacity_config.py",
    "api-context/agent_capacity_file.py": "src/disclosure_anchor/adapters/runtime/mineru_capacity_file.py",
    "api-context/agent_capacity_bootstrap.py": f"{_COMPAT}/agent_capacity_bootstrap.py",
    "api-context/agent_capacity_observation.py": f"{_COMPAT}/agent_capacity_observation.py",
    "windows/install_mineru_fixed_api.ps1": "scripts/windows/install_mineru_fixed_api.ps1",
    "windows/collect_mineru_runtime.ps1": "scripts/windows/collect_mineru_runtime.ps1",
    "windows/run_mineru_installation.ps1": "scripts/windows/run_mineru_installation.ps1",
    "windows/build_mineru_telemetry_assembly.ps1": "scripts/windows/build_mineru_telemetry_assembly.ps1",
    "windows/load_mineru_telemetry_assembly.ps1": "scripts/windows/load_mineru_telemetry_assembly.ps1",
    "native-m6/build_mineru_m6_owner.ps1": "scripts/windows/build_mineru_m6_owner.ps1",
    "native-m6/run_mineru_m6_owner_host.ps1": "scripts/windows/run_mineru_m6_owner_host.ps1",
}
for _name in TELEMETRY_SUPERVISOR_SOURCES:
    RELEASE_SOURCE_FILES[f"windows/{_name}"] = f"scripts/windows/{_name}"
for _name in NATIVE_M6_PRODUCTION_SOURCES:
    RELEASE_SOURCE_FILES[f"native-m6/{_name}"] = f"scripts/windows/{_name}"
IMPLICIT_EXTERNAL_READ_PATTERNS = (
    "/private/tmp/", "launch_one", "m6-r18-fable-primary", "m6-stream-native-telemetry-independent",
    "installation-supervisor.dll", "build_exploration_package", "build_final_package", "run_g4.py",
)
_TEXT_SUFFIXES = {".py", ".ps1", ".cs", ".yaml", ".yml", ".json", ""}
_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_SOURCE_BYTES = 8 * 1024 * 1024


class ReleaseInputError(ValueError):
    """Argument or input format problem (exit 64)."""


class ReleaseIdentityError(ValueError):
    """Identity or semantic inconsistency (exit 65)."""


@dataclass(frozen=True, slots=True)
class ReleaseInputs:
    capacity_bytes: bytes
    capacity: MineruCapacityConfig
    deployment_profile: MineruDeploymentProfile
    local_profile: MineruLocalWorkerProfile


def load_release_inputs(capacity_path: Path, deployment_path: Path, local_path: Path) -> ReleaseInputs:
    try:
        capacity_bytes = _read_bounded(capacity_path, label="capacity config")
        deployment_bytes = _read_bounded(deployment_path, label="deployment profile")
        local_bytes = _read_bounded(local_path, label="local worker profile")
    except OSError as exc:
        raise ReleaseInputError(f"cannot read release input: {exc}") from exc
    try:
        return ReleaseInputs(
            capacity_bytes=capacity_bytes,
            capacity=decode_mineru_capacity_config(capacity_bytes),
            deployment_profile=decode_mineru_deployment_profile(deployment_bytes),
            local_profile=decode_mineru_local_worker_profile(local_bytes),
        )
    except ValueError as exc:
        raise ReleaseInputError(str(exc)) from exc


def _read_bounded(path: Path, *, label: str, maximum: int = 64 * 1024) -> bytes:
    if not path.is_file():
        raise ReleaseInputError(f"{label} is not a regular file: {path}")
    raw = path.read_bytes()
    if not raw or len(raw) > maximum:
        raise ReleaseInputError(f"{label} bytes are outside the closed envelope: {path}")
    return raw


def _git(source_root: Path, *arguments: str, stdin: bytes | None = None) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), *arguments], input=stdin if stdin is not None else b"",
            capture_output=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseInputError(f"git is unavailable: {exc}") from exc
    if completed.returncode != 0:
        raise ReleaseInputError(
            "git failed: " + " ".join(arguments) + ": " + completed.stderr.decode("utf-8", "replace").strip()[:400]
        )
    return completed.stdout


# Product modules that generate or verify the package. The executing copies
# must equal the frozen commit's blobs, otherwise a package could claim an
# older source head while being produced by different generator code.
GENERATOR_SOURCE_FILES: tuple[str, ...] = (
    "src/disclosure_anchor/application/contracts/closed_document.py",
    "src/disclosure_anchor/application/contracts/mineru_capacity_config.py",
    "src/disclosure_anchor/application/contracts/mineru_deployment_profile.py",
    "src/disclosure_anchor/application/contracts/mineru_local_worker_profile.py",
    "src/disclosure_anchor/application/contracts/mineru_release_manifest.py",
    "src/disclosure_anchor/application/services/mineru_release_plan.py",
    "src/disclosure_anchor/adapters/runtime/mineru_release_package.py",
    "src/disclosure_anchor/cli/mineru_release.py",
)


def _executing_generator_paths() -> dict[str, Path]:
    import disclosure_anchor

    root = Path(disclosure_anchor.__file__).resolve().parents[2]
    return {relative: root / relative for relative in GENERATOR_SOURCE_FILES}


def _generator_drift(source_root: Path, blobs: dict[str, str]) -> list[str]:
    """Compare the executing generator modules with the frozen commit's blobs."""

    paths = _executing_generator_paths()
    listing = "\n".join(str(path) for path in paths.values()) + "\n"
    for path in paths.values():
        if not path.is_file():
            return [f"generator module missing from the executing checkout: {path}"]
    hashed = _git(source_root, "hash-object", "--stdin-paths", stdin=listing.encode("utf-8")).decode().split()
    if len(hashed) != len(paths):
        raise ReleaseInputError("git hash-object returned an unexpected number of generator blobs")
    problems = []
    for (relative, path), actual in zip(paths.items(), hashed, strict=True):
        tracked = f"{TRACKED_SCOPE_PREFIX}/{relative}"
        expected = blobs.get(tracked)
        if expected is None:
            problems.append(f"{tracked}: generator module not tracked at the source head")
        elif expected != actual:
            problems.append(f"{tracked}: executing generator differs from commit blob {expected}")
    return problems


def normalized_blob_sha1(payload: bytes) -> str:
    """Git blob id of text content after CRLF normalization (the repository's text=auto/eol rules)."""

    import hashlib

    normalized = payload.replace(b"\r\n", b"\n")
    return hashlib.sha1(b"blob %d\0" % len(normalized) + normalized).hexdigest()


@dataclass(frozen=True, slots=True)
class BuildReport:
    manifest: MineruReleaseManifest
    package: Path
    out_of_scope_dirty: tuple[str, ...]
    untracked_in_release_directories: tuple[str, ...]


def build_release_package(
    *, source_root: Path, source_head: str, capacity_path: Path, deployment_profile_path: Path,
    local_profile_path: Path, out: Path, now: datetime | None = None,
) -> BuildReport:
    if _HEAD_RE.fullmatch(source_head) is None:
        raise ReleaseInputError("source head must be a 40-hex commit")
    if not out.is_absolute() or out.exists() or out.is_symlink():
        raise ReleaseInputError("release output must be a new absolute directory")
    if not out.parent.is_dir():
        raise ReleaseInputError("release output parent directory must exist")
    source_root = source_root.resolve()
    toplevel = _git(source_root, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    if Path(toplevel).resolve() != source_root:
        raise ReleaseInputError("source root must be the repository top level")
    resolved_head = _git(source_root, "rev-parse", "--verify", source_head + "^{commit}").decode().strip()
    if resolved_head != source_head:
        raise ReleaseIdentityError("source head does not resolve to itself")
    inputs = load_release_inputs(capacity_path, deployment_profile_path, local_profile_path)
    service_root = source_root / TRACKED_SCOPE_PREFIX
    if not service_root.is_dir():
        raise ReleaseInputError("tracked scope prefix is not a directory under the source root")
    tree_listing = _git(source_root, "ls-tree", "-r", "--full-tree", source_head, TRACKED_SCOPE_PREFIX)
    source_manifest_sha256 = sha256_of(tree_listing)
    blobs: dict[str, str] = {}
    for line in tree_listing.decode("utf-8").splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split(" ")
        if len(parts) != 3 or parts[1] != "blob":
            continue
        blobs[path] = parts[2]

    files: dict[str, tuple[bytes, dict[str, str]]] = {}
    drift: list[str] = []
    candidates: list[tuple[str, str, Path, bytes]] = []
    for package_path, tracked_relative in sorted(RELEASE_SOURCE_FILES.items()):
        tracked_path = f"{TRACKED_SCOPE_PREFIX}/{tracked_relative}"
        expected_blob = blobs.get(tracked_path)
        if expected_blob is None:
            drift.append(f"{tracked_path}: not tracked at {source_head}")
            continue
        local = service_root / tracked_relative
        if not local.is_file() or local.is_symlink():
            drift.append(f"{tracked_path}: missing or not a regular file in the working tree")
            continue
        raw = local.read_bytes()
        if len(raw) > _MAX_SOURCE_BYTES:
            drift.append(f"{tracked_path}: exceeds the release source byte bound")
            continue
        candidates.append((package_path, tracked_path, local, raw))
    # Hash through git so clean filters and eol attributes apply exactly as at
    # commit time; a raw SHA-1 would report CRLF-normalized files as drift.
    if candidates:
        listing = "\n".join(str(item[2]) for item in candidates) + "\n"
        hashed = _git(source_root, "hash-object", "--stdin-paths", stdin=listing.encode("utf-8")).decode().split()
        if len(hashed) != len(candidates):
            raise ReleaseInputError("git hash-object returned an unexpected number of blobs")
        for (package_path, tracked_path, _, raw), actual_blob in zip(candidates, hashed, strict=True):
            expected_blob = blobs[tracked_path]
            if actual_blob != expected_blob:
                drift.append(f"{tracked_path}: working tree bytes differ from commit blob {expected_blob}")
                continue
            files[package_path] = (raw, {"kind": "tracked", "tracked_path": tracked_path, "blob_sha1": expected_blob})
    generator_drift = _generator_drift(source_root, blobs)
    if drift or generator_drift:
        raise ReleaseIdentityError("in-scope source drift: " + "; ".join(drift + generator_drift))

    status = _git(source_root, "status", "--porcelain=v1", "--untracked-files=all", "--", TRACKED_SCOPE_PREFIX)
    release_dirs = {str(Path(TRACKED_SCOPE_PREFIX, rel).parent) for rel in RELEASE_SOURCE_FILES.values()}
    out_of_scope: list[str] = []
    untracked_release: list[str] = []
    for line in status.decode("utf-8").splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if code == "??" and str(Path(path).parent) in release_dirs and not Path(path).name.startswith("test_"):
            untracked_release.append(path)
        else:
            out_of_scope.append(path)
    if untracked_release:
        # A release source directory with untracked non-test files is not a
        # frozen source state: the commit cannot describe what is on disk.
        raise ReleaseIdentityError("untracked files in release source directories: " + "; ".join(sorted(untracked_release)))

    try:
        plan = resolve_release(inputs.capacity, inputs.deployment_profile, inputs.local_profile)
    except ValueError as exc:
        # A capacity the current runtime cannot honour (or a non-round-tripping
        # projection) is a semantic inconsistency, refused before any output.
        raise ReleaseIdentityError(f"release inputs cannot be projected: {exc}") from exc
    files[COMPOSE_PACKAGE_PATH] = (plan.compose_bytes, {"kind": "generated", "generator": "compose"})
    files[CAPACITY_INPUT_PATH] = (inputs.capacity_bytes, {"kind": "input", "input": "capacity"})
    files[API_CONTEXT_CAPACITY_PATH] = (inputs.capacity_bytes, {"kind": "input", "input": "capacity"})
    files[DEPLOYMENT_INPUT_PATH] = (inputs.deployment_profile.exact_bytes, {"kind": "input", "input": "deployment_profile"})
    files[LOCAL_INPUT_PATH] = (inputs.local_profile.exact_bytes, {"kind": "input", "input": "local_worker_profile"})

    def digest(path: str) -> str:
        return sha256_of(files[path][0])

    capacity_source_sha256 = {
        f"mineru/cli/agent_capacity_{name}.py": digest(f"api-context/agent_capacity_{name}.py")
        for name in CAPACITY_SOURCE_NAMES
    }
    native_sources = {name: digest(f"native-m6/{name}") for name in NATIVE_M6_PRODUCTION_SOURCES}
    built_at = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = MineruReleaseManifest(
        contract_version=RELEASE_MANIFEST_CONTRACT,
        built_at_utc=built_at,
        source={"head": source_head, "tracked_scope_prefix": TRACKED_SCOPE_PREFIX,
                "source_manifest_sha256": source_manifest_sha256},
        inputs={
            "capacity_config_sha256": inputs.capacity.sha256,
            "deployment_profile_sha256": inputs.deployment_profile.sha256,
            "local_worker_profile_sha256": inputs.local_profile.sha256,
        },
        projection={
            "contract_version": RELEASE_PROJECTION_CONTRACT,
            "projection_only": True,
            "api_environment": dict(plan.api_environment),
            "api_command": list(plan.api_command),
            "compose_sha256": sha256_of(plan.compose_bytes),
            "api_device_profile": inputs.deployment_profile.api_device_profile,
            "local_profile_admits_full_pending": plan.local_profile_admits_full_pending,
        },
        api_build={
            "context": "api-context",
            "build_target": "explicit-capacity",
            "dockerfile_sha256": digest("api-context/Dockerfile"),
            "patcher_sha256": digest("api-context/patch_mineru_344.py"),
            "task_protocol_v2_sha256": digest("api-context/agent_task_protocol_v2.py"),
            "capacity_config_sha256": inputs.capacity.sha256,
            "capacity_source_sha256": capacity_source_sha256,
            "capacity_sources_sha256": capacity_sources_sha256(capacity_source_sha256),
        },
        native_m6={
            "sources": native_sources,
            "production_source_manifest_sha256": native_source_manifest_sha256(native_sources),
            "builder_sha256": digest("native-m6/build_mineru_m6_owner.ps1"),
            "launcher_sha256": digest("native-m6/run_mineru_m6_owner_host.ps1"),
        },
        installation={
            "installer_sha256": digest("windows/install_mineru_fixed_api.ps1"),
            "collector_sha256": digest("windows/collect_mineru_runtime.ps1"),
            "wrapper_sha256": digest("windows/run_mineru_installation.ps1"),
            "supervisor_sources": {name: digest(f"windows/{name}") for name in TELEMETRY_SUPERVISOR_SOURCES},
            "telemetry_assembly_builder_sha256": digest("windows/build_mineru_telemetry_assembly.ps1"),
            "telemetry_assembly_loader_sha256": digest("windows/load_mineru_telemetry_assembly.ps1"),
        },
        files=tuple(
            ReleaseFile(path=path, sha256=sha256_of(raw), bytes=len(raw), provenance=provenance)
            for path, (raw, provenance) in sorted(files.items())
        ),
    )
    manifest_bytes = manifest.exact_bytes
    out.mkdir(mode=0o700)
    for path, (raw, _) in sorted(files.items()):
        target = out / path
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_new_exact(target, raw)
    write_new_exact(out / MANIFEST_NAME, manifest_bytes)
    directory_fd = os.open(out, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return BuildReport(
        manifest=manifest, package=out, out_of_scope_dirty=tuple(sorted(out_of_scope)),
        untracked_in_release_directories=tuple(sorted(untracked_release)),
    )


@dataclass(frozen=True, slots=True)
class VerifyReport:
    manifest: MineruReleaseManifest
    inputs: ReleaseInputs
    problems: tuple[str, ...]
    projection_mismatches: tuple[str, ...]
    implicit_external_reads: tuple[dict[str, Any], ...]

    @property
    def passed(self) -> bool:
        return not self.problems and not self.projection_mismatches and not self.implicit_external_reads


def load_release_package(package: Path) -> tuple[MineruReleaseManifest, bytes]:
    if not package.is_absolute() or not package.is_dir():
        raise ReleaseInputError("release package must be an existing absolute directory")
    manifest_path = package / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ReleaseInputError("release package has no manifest")
    raw = manifest_path.read_bytes()
    try:
        return decode_mineru_release_manifest(raw), raw
    except ValueError as exc:
        raise ReleaseInputError(f"release manifest is invalid: {exc}") from exc


def verify_release_package(package: Path, *, check_active_dependencies: bool = False) -> VerifyReport:
    manifest, _ = load_release_package(package)
    problems: list[str] = []
    present: dict[str, bytes] = {}
    for entry in manifest.files:
        target = package / entry.path
        if not target.is_file() or target.is_symlink():
            problems.append(f"{entry.path}: missing")
            continue
        raw = target.read_bytes()
        present[entry.path] = raw
        if len(raw) != entry.bytes or sha256_of(raw) != entry.sha256:
            problems.append(f"{entry.path}: bytes differ from the manifest")
    listed = {entry.path for entry in manifest.files}
    for path in sorted(str(p.relative_to(package)).replace(os.sep, "/") for p in package.rglob("*") if p.is_file()):
        if path != MANIFEST_NAME and path not in listed:
            problems.append(f"{path}: file not listed in the manifest")
    for path in (*RELEASE_SOURCE_FILES, COMPOSE_PACKAGE_PATH, CAPACITY_INPUT_PATH, API_CONTEXT_CAPACITY_PATH,
                 DEPLOYMENT_INPUT_PATH, LOCAL_INPUT_PATH):
        if path not in listed:
            problems.append(f"{path}: required package file is not listed")
    expected_provenance = {
        **{path: ("tracked", f"{TRACKED_SCOPE_PREFIX}/{rel}") for path, rel in RELEASE_SOURCE_FILES.items()},
        COMPOSE_PACKAGE_PATH: ("generated", "compose"),
        CAPACITY_INPUT_PATH: ("input", "capacity"), API_CONTEXT_CAPACITY_PATH: ("input", "capacity"),
        DEPLOYMENT_INPUT_PATH: ("input", "deployment_profile"), LOCAL_INPUT_PATH: ("input", "local_worker_profile"),
    }
    for entry in manifest.files:
        expected = expected_provenance.get(entry.path)
        if expected is None:
            problems.append(f"{entry.path}: not part of the release inventory")
            continue
        kind, detail = expected
        if entry.provenance["kind"] != kind:
            problems.append(f"{entry.path}: provenance kind {entry.provenance['kind']} is not {kind}")
        elif kind == "tracked":
            if entry.provenance["tracked_path"] != detail:
                problems.append(f"{entry.path}: tracked path differs from the release inventory")
            if entry.path in present and entry.provenance["blob_sha1"] != normalized_blob_sha1(present[entry.path]):
                problems.append(f"{entry.path}: provenance blob does not match the packaged bytes")
        elif kind == "generated" and entry.provenance["generator"] != detail:
            problems.append(f"{entry.path}: generator differs from the release inventory")
        elif kind == "input" and entry.provenance["input"] != detail:
            problems.append(f"{entry.path}: input provenance differs from the release inventory")
    if problems:
        raise ReleaseIdentityError("release package inventory differs: " + "; ".join(problems))

    try:
        inputs = ReleaseInputs(
            capacity_bytes=present[CAPACITY_INPUT_PATH],
            capacity=decode_mineru_capacity_config(present[CAPACITY_INPUT_PATH]),
            deployment_profile=decode_mineru_deployment_profile(present[DEPLOYMENT_INPUT_PATH]),
            local_profile=decode_mineru_local_worker_profile(present[LOCAL_INPUT_PATH]),
        )
    except ValueError as exc:
        raise ReleaseIdentityError(f"release inputs are invalid: {exc}") from exc
    if inputs.deployment_profile.exact_bytes != present[DEPLOYMENT_INPUT_PATH]:
        problems.append("inputs/deployment-profile.json is not canonical")
    if inputs.local_profile.exact_bytes != present[LOCAL_INPUT_PATH]:
        problems.append("inputs/local-worker-profile.json is not canonical")
    if present[API_CONTEXT_CAPACITY_PATH] != present[CAPACITY_INPUT_PATH]:
        problems.append("api-context capacity bytes differ from the input capacity")
    expected_inputs = {
        "capacity_config_sha256": inputs.capacity.sha256,
        "deployment_profile_sha256": inputs.deployment_profile.sha256,
        "local_worker_profile_sha256": inputs.local_profile.sha256,
    }
    if manifest.inputs != expected_inputs:
        problems.append("manifest inputs differ from the packaged input identities")
    try:
        plan = resolve_release(inputs.capacity, inputs.deployment_profile, inputs.local_profile)
    except ValueError as exc:
        raise ReleaseIdentityError(f"release inputs cannot be projected: {exc}") from exc
    mismatches: list[str] = []
    if plan.compose_bytes != present[COMPOSE_PACKAGE_PATH]:
        mismatches.append("compose bytes differ from the projection of the packaged inputs")
    try:
        document = parse_compose_yaml(present[COMPOSE_PACKAGE_PATH])
    except ValueError as exc:
        mismatches.append(f"compose does not parse: {exc}")
    else:
        mismatches.extend(verify_compose_projection(document, inputs.capacity, inputs.deployment_profile))
    projection = manifest.projection
    if projection["api_environment"] != plan.api_environment:
        mismatches.append("manifest projection environment differs from the codec projection")
    if tuple(projection["api_command"]) != plan.api_command:
        mismatches.append("manifest projection command differs from the codec projection")
    if projection["compose_sha256"] != sha256_of(present[COMPOSE_PACKAGE_PATH]):
        mismatches.append("manifest projection compose hash differs from the packaged compose")
    if projection["api_device_profile"] != inputs.deployment_profile.api_device_profile:
        mismatches.append("manifest projection device profile differs from the deployment profile")
    if projection["local_profile_admits_full_pending"] != plan.local_profile_admits_full_pending:
        mismatches.append("manifest projection pending admission flag differs from the inputs")

    def digest(path: str) -> str:
        return sha256_of(present[path])

    build = manifest.api_build
    capacity_source_map = {
        f"mineru/cli/agent_capacity_{name}.py": digest(f"api-context/agent_capacity_{name}.py")
        for name in CAPACITY_SOURCE_NAMES
    }
    expected_build: dict[str, Any] = {
        "dockerfile_sha256": digest("api-context/Dockerfile"),
        "patcher_sha256": digest("api-context/patch_mineru_344.py"),
        "task_protocol_v2_sha256": digest("api-context/agent_task_protocol_v2.py"),
        "capacity_config_sha256": digest(API_CONTEXT_CAPACITY_PATH),
        "capacity_source_sha256": capacity_source_map,
    }
    for name, value in expected_build.items():
        if build[name] != value:
            problems.append(f"api_build.{name} differs from the packaged bytes")
    if build["capacity_sources_sha256"] != capacity_sources_sha256(capacity_source_map):
        problems.append("api_build.capacity_sources_sha256 differs from the packaged helper sources")
    dockerfile = present["api-context/Dockerfile"].decode("utf-8", "replace")
    if "AS explicit-capacity" not in dockerfile or "COPY capacity-config.json" not in dockerfile:
        problems.append("api-context Dockerfile has no explicit-capacity target")
    native = manifest.native_m6
    for name in NATIVE_M6_PRODUCTION_SOURCES:
        if native["sources"][name] != digest(f"native-m6/{name}"):
            problems.append(f"native_m6.sources.{name} differs from the packaged bytes")
    if native["builder_sha256"] != digest("native-m6/build_mineru_m6_owner.ps1"):
        problems.append("native_m6.builder_sha256 differs from the packaged bytes")
    if native["launcher_sha256"] != digest("native-m6/run_mineru_m6_owner_host.ps1"):
        problems.append("native_m6.launcher_sha256 differs from the packaged bytes")
    install = manifest.installation
    for name, path in (
        ("installer_sha256", "windows/install_mineru_fixed_api.ps1"),
        ("collector_sha256", "windows/collect_mineru_runtime.ps1"),
        ("wrapper_sha256", "windows/run_mineru_installation.ps1"),
        ("telemetry_assembly_builder_sha256", "windows/build_mineru_telemetry_assembly.ps1"),
        ("telemetry_assembly_loader_sha256", "windows/load_mineru_telemetry_assembly.ps1"),
    ):
        if install[name] != digest(path):
            problems.append(f"installation.{name} differs from the packaged bytes")
    for name in TELEMETRY_SUPERVISOR_SOURCES:
        if install["supervisor_sources"][name] != digest(f"windows/{name}"):
            problems.append(f"installation.supervisor_sources.{name} differs from the packaged bytes")
    for name in TELEMETRY_SUPERVISOR_SOURCES:
        if name in NATIVE_M6_PRODUCTION_SOURCES and present[f"windows/{name}"] != present[f"native-m6/{name}"]:
            problems.append(f"{name}: installation and native copies differ")

    # The dependency scan is cheap and always performed; the flag is retained
    # for the interface and only widens what the caller reports.
    del check_active_dependencies
    external: list[dict[str, Any]] = []
    if True:
        for entry in manifest.files:
            if Path(entry.path).suffix not in _TEXT_SUFFIXES:
                continue
            text = present[entry.path].decode("utf-8", "replace")
            for number, line in enumerate(text.splitlines(), start=1):
                for pattern in IMPLICIT_EXTERNAL_READ_PATTERNS:
                    if pattern in line:
                        external.append({"path": entry.path, "line": number, "pattern": pattern})
    return VerifyReport(
        manifest=manifest, inputs=inputs, problems=tuple(problems), projection_mismatches=tuple(mismatches),
        implicit_external_reads=tuple(external),
    )


def release_inventory(report: VerifyReport) -> dict[str, Any]:
    """Cleanup-map inventory: layer, provenance, active consumer and retention per file."""

    layers = {
        "api-context": ("api-image", "install_mineru_fixed_api.ps1 docker build context", "exact API image identity"),
        "compose": ("deployment", "install_mineru_fixed_api.ps1 -ComposeSource", "active compose identity"),
        "windows": ("installation", "run_mineru_installation.ps1 / installer / collector", "installation owner and attestation"),
        "native-m6": ("measurement", "build_mineru_m6_owner.ps1 / M6 campaign launcher", "optional formal measurement"),
        "inputs": ("inputs", "mineru_release verify/bind", "release input identity"),
    }
    items = []
    for entry in report.manifest.files:
        layer, consumer, retention = layers[entry.path.split("/", 1)[0]]
        items.append({
            "path": entry.path, "sha256": entry.sha256, "bytes": entry.bytes, "layer": layer,
            "provenance": entry.provenance, "active_consumer": consumer, "retention_reason": retention,
        })
    return {
        "contract_version": "m6.release-inventory.v1",
        "release_manifest_sha256": report.manifest.sha256,
        "files": items,
        "implicit_external_reads": list(report.implicit_external_reads),
    }


def write_new_json(path: Path, value: object) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    write_new_exact(path, raw)
    return raw


__all__ = [
    "BuildReport",
    "COMPOSE_PACKAGE_PATH",
    "IMPLICIT_EXTERNAL_READ_PATTERNS",
    "MANIFEST_NAME",
    "RELEASE_SOURCE_FILES",
    "ReleaseIdentityError",
    "ReleaseInputError",
    "ReleaseInputs",
    "TRACKED_SCOPE_PREFIX",
    "VerifyReport",
    "build_release_package",
    "canonical_bytes",
    "load_release_inputs",
    "load_release_package",
    "release_inventory",
    "verify_release_package",
    "write_new_json",
]
