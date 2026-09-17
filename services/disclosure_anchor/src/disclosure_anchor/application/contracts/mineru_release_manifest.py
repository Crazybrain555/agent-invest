"""Immutable release manifest binding exact sources, inputs and projections.

The manifest references hashes; it never carries a second editable copy of
capacity values. ``projection`` is display/verification output only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from disclosure_anchor.application.contracts.closed_document import (
    canonical_bytes,
    load_closed_object,
    require_bool,
    require_fields,
    require_int,
    require_sha256,
    require_str,
    sha256_of,
)


RELEASE_MANIFEST_CONTRACT = "m6.release.v1"
RELEASE_PROJECTION_CONTRACT = "m6.release-projection.v1"
_MAX_BYTES = 256 * 1024
_FIELDS = frozenset({
    "contract_version", "built_at_utc", "source", "inputs", "projection", "api_build", "native_m6",
    "installation", "files",
})
_SOURCE_FIELDS = frozenset({"head", "tracked_scope_prefix", "source_manifest_sha256"})
_INPUT_FIELDS = frozenset({"capacity_config_sha256", "deployment_profile_sha256", "local_worker_profile_sha256"})
_PROJECTION_FIELDS = frozenset({
    "contract_version", "projection_only", "api_environment", "api_command", "compose_sha256",
    "api_device_profile", "local_profile_admits_full_pending",
})
_API_BUILD_FIELDS = frozenset({
    "context", "build_target", "dockerfile_sha256", "patcher_sha256", "task_protocol_v2_sha256",
    "capacity_config_sha256", "capacity_source_sha256", "capacity_sources_sha256",
})
_NATIVE_FIELDS = frozenset({"sources", "production_source_manifest_sha256", "builder_sha256", "launcher_sha256"})
_INSTALL_FIELDS = frozenset({
    "installer_sha256", "collector_sha256", "wrapper_sha256", "supervisor_sources",
    "telemetry_assembly_builder_sha256", "telemetry_assembly_loader_sha256",
})
_FILE_FIELDS = frozenset({"path", "sha256", "bytes", "provenance"})
_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PACKAGE_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)*$")
CAPACITY_SOURCE_NAMES = ("bootstrap", "config", "file", "observation")
NATIVE_M6_PRODUCTION_SOURCES = (
    "mineru_m6_owner_binding.cs", "mineru_m6_owner_endpoint.cs", "mineru_m6_owner_host.cs",
    "mineru_m6_owner_identity.cs", "mineru_m6_owner_journal.cs", "mineru_m6_owner_platform.cs",
    "mineru_m6_owner_wire.cs", "mineru_m6_private_store.cs", "mineru_m6_run_control.cs",
    "mineru_m6_writer_guard.cs", "mineru_nvml_backend.cs", "mineru_resident_wire.cs",
)
TELEMETRY_SUPERVISOR_SOURCES = (
    "mineru_nvml_backend.cs", "mineru_resident_wire.cs", "mineru_telemetry_job_supervisor.cs",
)


@dataclass(frozen=True, slots=True)
class ReleaseFile:
    path: str
    sha256: str
    bytes: int
    provenance: dict[str, str]

    def __post_init__(self) -> None:
        if _PACKAGE_PATH_RE.fullmatch(require_str(self.path, label="release file path", maximum=512)) is None:
            raise ValueError("release file path must be a normalized relative package path")
        require_sha256(self.sha256, label="release file sha256")
        require_int(self.bytes, label="release file bytes", minimum=0)
        if type(self.provenance) is not dict or type(self.provenance.get("kind")) is not str:
            raise ValueError("release file provenance must name its kind")
        kind = self.provenance["kind"]
        expected = {
            "tracked": {"kind", "tracked_path", "blob_sha1"},
            "generated": {"kind", "generator"},
            "input": {"kind", "input"},
        }.get(kind)
        if expected is None or set(self.provenance) != expected:
            raise ValueError("release file provenance fields are not closed")
        for value in self.provenance.values():
            require_str(value, label="release file provenance value", maximum=512)


@dataclass(frozen=True, slots=True)
class MineruReleaseManifest:
    contract_version: str
    built_at_utc: str
    source: dict[str, str]
    inputs: dict[str, str]
    projection: dict[str, Any]
    api_build: dict[str, Any]
    native_m6: dict[str, Any]
    installation: dict[str, Any]
    files: tuple[ReleaseFile, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.contract_version != RELEASE_MANIFEST_CONTRACT:
            raise ValueError("release manifest contract is unsupported")
        if _UTC_RE.fullmatch(require_str(self.built_at_utc, label="built_at_utc", maximum=32)) is None:
            raise ValueError("release manifest built_at_utc must be an ISO-8601 UTC second timestamp")
        require_fields(self.source, _SOURCE_FIELDS, label="release source")
        if _HEAD_RE.fullmatch(require_str(self.source["head"], label="source head")) is None:
            raise ValueError("release source head must be a 40-hex commit")
        require_str(self.source["tracked_scope_prefix"], label="tracked scope prefix", maximum=256)
        require_sha256(self.source["source_manifest_sha256"], label="source manifest sha256")
        require_fields(self.inputs, _INPUT_FIELDS, label="release inputs")
        for name in _INPUT_FIELDS:
            require_sha256(self.inputs[name], label=name)
        projection = self.projection
        require_fields(projection, _PROJECTION_FIELDS, label="release projection")
        if projection["contract_version"] != RELEASE_PROJECTION_CONTRACT or projection["projection_only"] is not True:
            raise ValueError("release projection must be marked projection-only")
        environment = projection["api_environment"]
        if type(environment) is not dict or any(
            type(k) is not str or type(v) is not str for k, v in environment.items()
        ):
            raise ValueError("release projection environment must map strings to strings")
        command = projection["api_command"]
        if type(command) is not list or not command or any(type(item) is not str for item in command):
            raise ValueError("release projection command must be a non-empty argv list")
        require_sha256(projection["compose_sha256"], label="projection compose sha256")
        if projection["api_device_profile"] not in ("cpu", "cuda0"):
            raise ValueError("release projection device profile is invalid")
        require_bool(projection["local_profile_admits_full_pending"], label="local_profile_admits_full_pending")
        build = self.api_build
        require_fields(build, _API_BUILD_FIELDS, label="release api_build")
        if build["context"] != "api-context" or build["build_target"] != "explicit-capacity":
            raise ValueError("release api build must target the explicit-capacity context")
        for name in ("dockerfile_sha256", "patcher_sha256", "task_protocol_v2_sha256", "capacity_config_sha256",
                     "capacity_sources_sha256"):
            require_sha256(build[name], label=name)
        sources = build["capacity_source_sha256"]
        expected_sources = {f"mineru/cli/agent_capacity_{name}.py" for name in CAPACITY_SOURCE_NAMES}
        if type(sources) is not dict or set(sources) != expected_sources:
            raise ValueError("release capacity sources are not the four helper modules")
        for name, digest in sources.items():
            require_sha256(digest, label=name)
        if sha256_of(canonical_bytes(sources)) != build["capacity_sources_sha256"]:
            raise ValueError("release capacity sources digest does not match its map")
        if build["capacity_config_sha256"] != self.inputs["capacity_config_sha256"]:
            raise ValueError("release api build capacity differs from its input")
        native = self.native_m6
        require_fields(native, _NATIVE_FIELDS, label="release native_m6")
        if type(native["sources"]) is not dict or tuple(sorted(native["sources"])) != tuple(sorted(NATIVE_M6_PRODUCTION_SOURCES)):
            raise ValueError("release native sources are not the twelve production files")
        for name, digest in native["sources"].items():
            require_sha256(digest, label=name)
        for name in ("production_source_manifest_sha256", "builder_sha256", "launcher_sha256"):
            require_sha256(native[name], label=name)
        if native_source_manifest_sha256(native["sources"]) != native["production_source_manifest_sha256"]:
            raise ValueError("release native source manifest digest does not match its sources")
        install = self.installation
        require_fields(install, _INSTALL_FIELDS, label="release installation")
        for name in ("installer_sha256", "collector_sha256", "wrapper_sha256", "telemetry_assembly_builder_sha256",
                     "telemetry_assembly_loader_sha256"):
            require_sha256(install[name], label=name)
        supervisor = install["supervisor_sources"]
        if type(supervisor) is not dict or tuple(sorted(supervisor)) != tuple(sorted(TELEMETRY_SUPERVISOR_SOURCES)):
            raise ValueError("release supervisor sources are not the three telemetry files")
        for name, digest in supervisor.items():
            require_sha256(digest, label=name)
        if type(self.files) is not tuple or not self.files or any(type(item) is not ReleaseFile for item in self.files):
            raise ValueError("release files must be a non-empty tuple of release files")
        paths = [item.path for item in self.files]
        if paths != sorted(paths) or len(set(paths)) != len(paths):
            raise ValueError("release files must be sorted by unique path")
        for item in self.files:
            item.__post_init__()

    def file(self, path: str) -> ReleaseFile:
        for item in self.files:
            if item.path == path:
                return item
        raise KeyError(path)

    @property
    def exact_bytes(self) -> bytes:
        self.__post_init__()
        payload = {
            "contract_version": self.contract_version,
            "built_at_utc": self.built_at_utc,
            "source": self.source,
            "inputs": self.inputs,
            "projection": self.projection,
            "api_build": self.api_build,
            "native_m6": self.native_m6,
            "installation": self.installation,
            "files": [
                {"path": item.path, "sha256": item.sha256, "bytes": item.bytes, "provenance": item.provenance}
                for item in self.files
            ],
        }
        return canonical_bytes(payload)

    @property
    def sha256(self) -> str:
        return sha256_of(self.exact_bytes)


def native_source_manifest_sha256(sources: dict[str, str]) -> str:
    """The native suite's production manifest digest: ``name sha`` lines in list order plus a trailing newline."""

    text = "\n".join(f"{name} {sources[name]}" for name in NATIVE_M6_PRODUCTION_SOURCES) + "\n"
    return sha256_of(text.encode("utf-8"))


def capacity_sources_sha256(sources: dict[str, str]) -> str:
    """The Dockerfile's capacity source map digest (sorted compact JSON)."""

    return sha256_of(canonical_bytes(sources))


def decode_mineru_release_manifest(payload: bytes) -> MineruReleaseManifest:
    value = load_closed_object(payload, label="release manifest", maximum_bytes=_MAX_BYTES)
    require_fields(value, _FIELDS, label="release manifest")
    files = value["files"]
    if type(files) is not list:
        raise ValueError("release manifest files must be a list")
    items = []
    for entry in files:
        if type(entry) is not dict:
            raise ValueError("release manifest file entry must be an object")
        require_fields(entry, _FILE_FIELDS, label="release manifest file entry")
        items.append(ReleaseFile(**entry))
    fields: dict[str, Any] = dict(value)
    fields["files"] = tuple(items)
    manifest = MineruReleaseManifest(**fields)
    if manifest.exact_bytes != payload:
        raise ValueError("release manifest bytes are not canonical")
    return manifest


__all__ = [
    "CAPACITY_SOURCE_NAMES",
    "NATIVE_M6_PRODUCTION_SOURCES",
    "RELEASE_MANIFEST_CONTRACT",
    "RELEASE_PROJECTION_CONTRACT",
    "TELEMETRY_SUPERVISOR_SOURCES",
    "MineruReleaseManifest",
    "ReleaseFile",
    "capacity_sources_sha256",
    "decode_mineru_release_manifest",
    "native_source_manifest_sha256",
]
