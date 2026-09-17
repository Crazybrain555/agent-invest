"""Deployment qualification wiring: fixture smoke, epoch bracket, held-out canaries.

Every step is the existing product executor run as an owned, bounded local
command with explicit identities; nothing here parses PDFs itself or judges
quality. The result is the existing held-out validation receipt whose canonical
hash is the deployment qualification identity consumed by ``bind`` and by the
M6 runtime identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import sys
from typing import Any

from disclosure_anchor.adapters.runtime.exact_file_write import write_new_exact
from disclosure_anchor.adapters.runtime.mineru_release_package import (
    CAPACITY_INPUT_PATH,
    ReleaseIdentityError,
    ReleaseInputError,
    VerifyReport,
    write_new_json,
)
from disclosure_anchor.adapters.runtime.mineru_release_private_binding import ReleasePrivateBinding
from disclosure_anchor.adapters.runtime.resident_owner_control import BoundedOwnerCommand
from disclosure_anchor.application.contracts.closed_document import (
    SHA256_RE,
    canonical_bytes,
    load_closed_object,
    require_fields,
    require_int,
    require_str,
    sha256_of,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads


CANARY_MANIFEST_CONTRACT = "m6.deployment-canary-manifest.v1"
QUALIFICATION_CONTRACT = "m6.deployment-qualification.v1"
_MANIFEST_FIELDS = frozenset({"contract_version", "fixture_input", "heldout"})
_HELDOUT_FIELDS = frozenset({"label", "path", "expected_sha256", "expected_pages"})
_LABEL_RE = r"^[a-z][a-z0-9-]{0,31}$"
_SMOKE_TIMEOUT_SECONDS = 1800
_STEP_OUTPUT_BYTES = 1 << 20


@dataclass(frozen=True, slots=True)
class HeldoutDocument:
    label: str
    path: Path
    expected_sha256: str
    expected_pages: int


@dataclass(frozen=True, slots=True)
class CanaryManifest:
    fixture_input: Path | None
    heldout: tuple[HeldoutDocument, ...]
    sha256: str


def load_canary_manifest(path: Path) -> CanaryManifest:
    import re

    if not path.is_absolute() or not path.is_file():
        raise ReleaseInputError("canary manifest must be an existing absolute file")
    raw = path.read_bytes()
    try:
        value = load_closed_object(raw, label="canary manifest", maximum_bytes=65536)
        require_fields(value, _MANIFEST_FIELDS, label="canary manifest")
        if value["contract_version"] != CANARY_MANIFEST_CONTRACT:
            raise ValueError("canary manifest contract is unsupported")
        fixture = value["fixture_input"]
        fixture_path = None
        if fixture is not None:
            fixture_path = Path(require_str(fixture, label="fixture_input"))
            if not fixture_path.is_absolute() or not fixture_path.is_file():
                raise ValueError("fixture_input must be an existing absolute file")
        entries = value["heldout"]
        if type(entries) is not list or not 2 <= len(entries) <= 8:
            raise ValueError("canary manifest requires 2..8 held-out documents")
        documents = []
        labels: set[str] = set()
        for entry in entries:
            if type(entry) is not dict:
                raise ValueError("held-out entry must be an object")
            require_fields(entry, _HELDOUT_FIELDS, label="held-out entry")
            label = require_str(entry["label"], label="held-out label", maximum=32)
            if re.fullmatch(_LABEL_RE, label) is None or label in labels or label in {"smoke-fixture", "epoch-before", "epoch-after"}:
                raise ValueError("held-out label is invalid or duplicated")
            labels.add(label)
            document = Path(require_str(entry["path"], label="held-out path"))
            if not document.is_absolute() or not document.is_file():
                raise ValueError(f"held-out document does not exist: {label}")
            digest = require_str(entry["expected_sha256"], label="held-out expected_sha256")
            if SHA256_RE.fullmatch(digest) is None:
                raise ValueError("held-out expected_sha256 is not canonical")
            actual = "sha256:" + hashlib.sha256(document.read_bytes()).hexdigest()
            if actual != digest:
                raise ValueError(f"held-out document bytes differ from expected_sha256: {label}")
            documents.append(HeldoutDocument(
                label=label, path=document, expected_sha256=digest,
                expected_pages=require_int(entry["expected_pages"], label="held-out expected_pages", minimum=2, maximum=100_000),
            ))
    except ValueError as exc:
        raise ReleaseInputError(str(exc)) from exc
    return CanaryManifest(fixture_input=fixture_path, heldout=tuple(documents), sha256=sha256_of(raw))


def service_root() -> Path:
    import disclosure_anchor

    root = Path(disclosure_anchor.__file__).resolve().parents[2]
    for name in ("scripts/mineru_smoke.py", "scripts/freeze_mineru_campaign_epoch.py", "scripts/build_mineru_validation_receipt.py"):
        if not (root / name).is_file():
            raise ReleaseInputError(f"product executor is missing from the installed service checkout: {name}")
    return root


def _product_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "DISCLOSURE_MIGRATION_DATABASE_URL", "PYTHONOPTIMIZE")}
    root = service_root()
    env["PYTHONPATH"] = f"{root / 'src'}:{root}"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


@dataclass(frozen=True, slots=True)
class StepResult:
    label: str
    exit_code: int
    stdout_sha256: str
    stderr_sha256: str


def run_step(output: Path, label: str, argv: list[str], *, timeout_seconds: float) -> StepResult:
    """Run one product executor as an owned bounded command; retain raw output."""

    write_new_json(output / f"{label}-command.json", {"argv": argv, "timeout_seconds": timeout_seconds})
    command = BoundedOwnerCommand(
        argv, timeout_seconds=timeout_seconds, maximum_bytes=_STEP_OUTPUT_BYTES,
        environment=_product_env(), cwd=str(service_root()),
    )
    try:
        result = command.finish()
    except (TimeoutError, ValueError) as exc:
        stdout, stderr = command.captured_output
        write_new_exact(output / f"{label}-stdout.raw", stdout)
        write_new_exact(output / f"{label}-stderr.raw", stderr)
        write_new_json(output / f"{label}-execution.json", {"exit_code": None, "failure": str(exc)})
        raise ReleaseIdentityError(f"{label}: {exc}") from exc
    write_new_exact(output / f"{label}-stdout.raw", result.stdout)
    write_new_exact(output / f"{label}-stderr.raw", result.stderr)
    write_new_json(output / f"{label}-execution.json", {"exit_code": result.exit_code})
    return StepResult(label=label, exit_code=result.exit_code, stdout_sha256=sha256_of(result.stdout), stderr_sha256=sha256_of(result.stderr))


def _load_json(path: Path) -> dict[str, Any]:
    value = strict_json_loads(path.read_bytes())
    if type(value) is not dict:
        raise ReleaseIdentityError(f"{path.name} is not a JSON object")
    return value


def qualify_release(
    *, report: VerifyReport, runtime_bundle: Path, canary: CanaryManifest, binding: ReleasePrivateBinding,
    package: Path, output: Path,
) -> dict[str, Any]:
    if not output.is_absolute() or output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ReleaseInputError("qualification output must be a new absolute directory under an existing parent")
    if not report.passed:
        raise ReleaseIdentityError("release package did not verify; refusing to qualify")
    bundle_raw = runtime_bundle.read_bytes()
    bundle = strict_json_loads(bundle_raw)
    if type(bundle) is not dict or type(bundle.get("identity_sha256")) is not str:
        raise ReleaseInputError("runtime bundle must carry identity_sha256")
    manifest = bundle.get("manifest")
    if type(manifest) is not dict or manifest.get("orchestrator", {}).get("capacity_config_sha256") != report.inputs.capacity.sha256:
        raise ReleaseIdentityError("runtime bundle capacity differs from the release capacity")
    bundle_identity = bundle["identity_sha256"]
    capacity_path = package / CAPACITY_INPUT_PATH
    capacity_sha = report.inputs.capacity.sha256
    root = service_root()
    python = sys.executable
    output.mkdir(mode=0o700)
    steps: list[StepResult] = []

    def smoke(label: str, document: HeldoutDocument | None) -> None:
        work = output / f"work-{label}"
        work.mkdir(mode=0o700)
        argv = [
            python, "-B", str(root / "scripts/mineru_smoke.py"),
            "--runtime-manifest", str(runtime_bundle), "--runtime-bundle-identity", bundle_identity,
            "--capacity-config", str(capacity_path), "--capacity-config-sha256", capacity_sha,
            "--mineru-bin", str(binding.mineru_bin), "--api-url", binding.api_url,
            "--observability-url", binding.observability_url, "--inference-upstream-url", binding.inference_upstream_url,
            "--receipt-out", str(output / f"{label}.v6.json"), "--canary-cache-out", str(output / f"{label}-canary.json"),
            "--work-root", str(work), "--timeout-seconds", str(_SMOKE_TIMEOUT_SECONDS),
        ]
        if document is not None:
            argv += ["--input", str(document.path), "--expected-input-sha256", document.expected_sha256]
        elif canary.fixture_input is not None:
            argv += ["--input", str(canary.fixture_input)]
        result = run_step(output, label, argv, timeout_seconds=_SMOKE_TIMEOUT_SECONDS + 120)
        steps.append(result)
        if result.exit_code != 0:
            raise ReleaseIdentityError(f"{label}: smoke executor exited {result.exit_code}")
        receipt = _load_json(output / f"{label}.v6.json")
        if receipt.get("status") != "pass" or receipt.get("identity", {}).get("runtime_manifest_identity_sha256") != bundle_identity:
            raise ReleaseIdentityError(f"{label}: smoke receipt is not a pass under the runtime bundle")
        if document is not None and receipt.get("input", {}).get("page_count") != document.expected_pages:
            raise ReleaseIdentityError(f"{label}: source page count differs from the canary manifest")

    def epoch(label: str) -> None:
        argv = [
            python, "-B", str(root / "scripts/freeze_mineru_campaign_epoch.py"),
            "--runtime-manifest", str(runtime_bundle), "--receipt-out", str(output / f"{label}.json"),
            "--ssh-host", binding.ssh_host, "--ssh-user", binding.ssh.username, "--ssh-port", str(binding.ssh.port),
            "--ssh-identity", binding.ssh.private_key_path, "--ssh-known-hosts", binding.ssh.known_hosts_path,
            "--capacity-config", str(capacity_path), "--capacity-config-sha256", capacity_sha,
            "--mineru-bin", str(binding.mineru_bin), "--runtime-bundle-identity", bundle_identity,
        ]
        result = run_step(output, label, argv, timeout_seconds=180)
        steps.append(result)
        if result.exit_code != 0:
            raise ReleaseIdentityError(f"{label}: epoch freeze exited {result.exit_code}")

    smoke("smoke-fixture", None)
    epoch("epoch-before")
    for document in canary.heldout:
        smoke(document.label, document)
    epoch("epoch-after")
    receipt_argv = [python, "-B", str(root / "scripts/build_mineru_validation_receipt.py")]
    for document in canary.heldout:
        receipt_argv += ["--smoke-receipt", str(output / f"{document.label}.v6.json")]
    receipt_argv += [
        "--epoch-before", str(output / "epoch-before.json"), "--epoch-after", str(output / "epoch-after.json"),
        "--receipt-out", str(output / "validation-final.json"),
    ]
    result = run_step(output, "validation-receipt", receipt_argv, timeout_seconds=120)
    steps.append(result)
    if result.exit_code != 0:
        raise ReleaseIdentityError(f"validation receipt builder exited {result.exit_code}")
    validation = _load_json(output / "validation-final.json")
    if validation.get("status") != "pass":
        raise ReleaseIdentityError("validation receipt is not a pass")
    qualification_sha256 = sha256_of(canonical_bytes(validation))
    summary = {
        "contract_version": QUALIFICATION_CONTRACT,
        "status": "pass",
        "release_manifest_sha256": report.manifest.sha256,
        "capacity_config_sha256": capacity_sha,
        "runtime_bundle_identity_sha256": bundle_identity,
        "runtime_bundle_sha256": sha256_of(bundle_raw),
        "canary_manifest_sha256": canary.sha256,
        "deployment_qualification_sha256": qualification_sha256,
        "validation_receipt_path": str(output / "validation-final.json"),
        "smoke_receipt_path": str(output / "smoke-fixture.v6.json"),
        "canary_cache_path": str(output / "smoke-fixture-canary.json"),
        "heldout": [{"label": d.label, "source_sha256": d.expected_sha256, "expected_pages": d.expected_pages} for d in canary.heldout],
        "steps": [{"label": s.label, "exit_code": s.exit_code, "stdout_sha256": s.stdout_sha256, "stderr_sha256": s.stderr_sha256} for s in steps],
        "database_access": "none",
    }
    write_new_json(output / "qualification.json", summary)
    return summary


def qualification_identity(validation_receipt: Path) -> str:
    """The canonical identity of a validation receipt (= deployment_qualification_sha256)."""

    value = strict_json_loads(validation_receipt.read_bytes())
    return sha256_of(canonical_bytes(value))


__all__ = [
    "CANARY_MANIFEST_CONTRACT",
    "QUALIFICATION_CONTRACT",
    "CanaryManifest",
    "HeldoutDocument",
    "load_canary_manifest",
    "qualification_identity",
    "qualify_release",
    "run_step",
    "service_root",
]
