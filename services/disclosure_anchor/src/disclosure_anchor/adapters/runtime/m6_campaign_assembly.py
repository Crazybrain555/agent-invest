"""One bounded Mac composition root for an owner-bound M6 campaign (R20 WP2).

The root reuses the existing pieces without a scheduler or ledger: the WP1
release binding, the pinned SSH/sftp transport, the native launcher's
Prepare/Run/Cancel parameter sets, the one pure spec factory, the v2
bind-by-value protocol, the existing runner (`cli.staged_campaign`) and
verifier supervisor (`cli.m6_verifier_supervisor`) as sourced children, and
the runner's own product closure receipts. Every child and the remote launcher
are supervised concurrently under fixed budgets derived from the owner's
anchor; the first failure requests STOP, drains what can drain, and the
summary reports exactly what was verified. A local transport exit is never
remote proof: the launcher's exit record is fetched and compared separately.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import stat
import time
from typing import Any, Literal

from disclosure_anchor.adapters.runtime.exact_file_write import write_new_exact
from disclosure_anchor.adapters.runtime.m6_campaign_private_binding import (
    CampaignBindingError, CampaignPrivateBinding, assert_known_hosts_pins_address, load_campaign_private_binding,
)
from disclosure_anchor.adapters.runtime.m6_continuous_clock import diagnostic_continuous_clock
from disclosure_anchor.adapters.runtime.m6_e2e_assembly import SPOOL_FILENAME
from disclosure_anchor.adapters.runtime.mac_observer_identity import MacObserverIdentityReader
from disclosure_anchor.adapters.runtime.m6_e2e_run import (
    M6RunDirectory, M6RunnerClosureFailed, M6VerifierAssembly, RunnerClosureReceipts, close_verifier_assembly,
    execute_runner_closure, load_m6_run_directory, m6_owner_client_factory,
)
from disclosure_anchor.adapters.runtime.m6_owner_protocol import (
    M6LeasePolicy, M6OwnerClient, M6OwnerProtocolError, M6OwnerRejected,
)
from disclosure_anchor.adapters.runtime.mineru_release_install import ExclusivityLock, acquire_exclusivity
from disclosure_anchor.adapters.runtime.resident_owner_control import (
    BoundedOwnerCommand, OwnerCommandResult, owner_ssh_command, pinned_windows_script,
)
from disclosure_anchor.application.contracts.closed_document import (
    canonical_bytes, load_closed_object, require_fields, sha256_of,
)
from disclosure_anchor.application.contracts.m6_campaign import M6CampaignScope, M6CorpusManifest
from disclosure_anchor.application.contracts.m6_campaign_intent import M6_CAMPAIGN_INTENT_MAX_BYTES, M6CampaignIntent
from disclosure_anchor.application.contracts.m6_evaluation_plan import M6_EVALUATION_PLAN_MAX_BYTES, M6EvaluationPlan
from disclosure_anchor.application.contracts.m6_control_receipts import (
    M6AdmissionReconciliationReceipt, M6OwnershipClosureReceipt, M6ResourceAuditReceipt, attempt_set_sha256,
)
from disclosure_anchor.application.contracts.m6_document_qualification import M6QualityPlan
from disclosure_anchor.application.contracts.m6_owner import (
    M6CloseOwner, M6OwnerAnchor, M6OwnerControl, M6OwnerStatus, bind_physical_owner_boot, physical_owner_epoch_sha256,
)
from disclosure_anchor.application.contracts.m6_run import M6RunSpec
from disclosure_anchor.application.contracts.resident_session_evidence import check_mac_observer_identity
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.services.m6_launch_budget import (
    LaunchTransportBudget, finish_wait_seconds, launch_transport_budget, transport_headroom_seconds,
)
from disclosure_anchor.application.services.m6_run_spec_factory import build_run_spec

CAMPAIGN_SUMMARY_CONTRACT = "m6.campaign-run-summary.v1"
LOCAL_MEASUREMENT_WINDOW_CONTRACT = "m6.local-measurement-window.v1"
OWNER_DEPLOYMENT_CONTRACT = "m6.owner-deployment.v2"
LAUNCHER_RESULT_CONTRACT = "m6.owner-launcher-result.v1"
PREPARE_RECEIPT_CONTRACT = "m6.owner-workspace-prepare.v1"
EXTERNAL_EXIT_CONTRACT = "m6.owner-external-exit.v2"
CampaignMode = Literal["run", "bootstrap-check"]

_ROLES_BY_MODE: dict[str, tuple[str, ...]] = {
    "e2e_publication": ("controller", "e2e_runner", "quality_verifier", "public_verifier"),
    "service_diagnostic": ("controller", "service_runner", "quality_verifier"),
}
_READY_FIELDS = frozenset({
    "status", "anchor_sha256", "owner_epoch_sha256", "anchor", "spec_sha256", "journal_prefix_bytes",
    "journal_prefix_sha256",
})
_EMPTY_SHA = "sha256:" + hashlib.sha256(b"").hexdigest()
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9 _./:\\-]+$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_INPUT_BYTES = 8 * 1024 * 1024
_SSH_OVERHEAD_SECONDS = 90.0
# The launcher's own ExitWaitExtraSeconds (passed explicitly) and its post-exit drain/record allowance.
_LAUNCHER_EXIT_WAIT_EXTRA_SECONDS = 180
_LAUNCHER_DRAIN_SECONDS = 15.0
_PREPARE_TIMEOUT_SECONDS = 180.0
_STAGE_TIMEOUT_SECONDS = 180.0
_CANCEL_TIMEOUT_SECONDS = 120.0
_FETCH_TIMEOUT_SECONDS = 180.0
_LAUNCHER_CAPTURE_BYTES = 1_048_576
_CHILD_CAPTURE_BYTES = 1_048_576
_STATUS_POLL_SECONDS = 30.0


class CampaignInputError(ValueError):
    """An input is missing, unreadable, malformed or does not match its pinned identity."""


class CampaignIdentityError(ValueError):
    """Two inputs that must agree do not."""


class CampaignOutcomeUnknown(RuntimeError):
    """The remote or child outcome could not be determined; nothing is reported as success."""


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sftp_path(path: PureWindowsPath) -> str:
    text = str(path)
    if _SAFE_PATH_RE.fullmatch(text) is None:
        raise CampaignInputError(f"remote path contains characters outside the transfer allowlist: {text!r}")
    return "/" + text.replace("\\", "/")


def _quoted(value: str) -> str:
    if _SAFE_PATH_RE.fullmatch(value) is None:
        raise CampaignInputError(f"transfer path contains characters outside the allowlist: {value!r}")
    return '"' + value + '"'


def _read_pinned(path: Path, expected_sha256: str, *, label: str) -> bytes:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise CampaignInputError(f"{label} must be an existing absolute regular file")
    with path.open("rb") as source:
        raw = source.read(_MAX_INPUT_BYTES + 1)
    if len(raw) > _MAX_INPUT_BYTES:
        raise CampaignInputError(f"{label} exceeds its byte bound")
    if sha256_of(raw) != expected_sha256:
        raise CampaignIdentityError(f"{label} bytes differ from the pinned identity")
    return raw


# --- pure helpers (test seams) -------------------------------------------------

@dataclass(frozen=True, slots=True)
class ReadyObservation:
    status: str
    anchor: M6OwnerAnchor
    anchor_sha256: str
    owner_epoch_sha256: str
    spec_sha256: str | None
    journal_prefix_bytes: int
    journal_prefix_sha256: str


def parse_ready_line(
    text: str, *, expected_run_id: str, expect_recovered: bool = False, original_anchor_sha256: str | None = None,
) -> ReadyObservation:
    """Strictly decode the owner's seven-field READY line and canonically verify its anchor.

    A fresh owner reports its own incarnation (epoch equal to the anchor's), no
    spec and an empty journal prefix. A recovered owner keeps the original
    anchor (whose epoch names the original incarnation) but runs as a new
    incarnation, and must already be bound to the original spec.
    """
    value = strict_json_loads(text)
    if type(value) is not dict or set(value) != _READY_FIELDS:
        raise ValueError("READY line is not the exact seven-field owner report")
    status = value["status"]
    expected_status = "ready_recovered" if expect_recovered else "ready_unbound"
    if status != expected_status:
        raise ValueError(f"READY status {status!r} is not {expected_status!r}")
    anchor_value = value["anchor"]
    if type(anchor_value) is not dict:
        raise ValueError("READY anchor is not an object")
    anchor = M6OwnerAnchor.model_validate(anchor_value)
    if anchor.canonical_sha256() != value["anchor_sha256"]:
        raise ValueError("READY anchor hash differs from its canonical anchor")
    if anchor.run_id != expected_run_id:
        raise ValueError("READY anchor names another run")
    epoch = value["owner_epoch_sha256"]
    if type(epoch) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", epoch) is None:
        raise ValueError("READY owner epoch invalid")
    prefix_bytes = value["journal_prefix_bytes"]
    if isinstance(prefix_bytes, bool) or type(prefix_bytes) is not int or prefix_bytes < 0:
        raise ValueError("READY journal prefix bytes invalid")
    prefix_sha = value["journal_prefix_sha256"]
    if type(prefix_sha) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", prefix_sha) is None:
        raise ValueError("READY journal prefix hash invalid")
    spec_sha = value["spec_sha256"]
    if original_anchor_sha256 is not None and value["anchor_sha256"] != original_anchor_sha256:
        raise ValueError("READY anchor differs from the original anchor")
    if not expect_recovered:
        if anchor.owner_process_epoch_sha256 != epoch:
            raise ValueError("fresh READY owner epoch differs from the anchor's epoch")
        if spec_sha is not None or prefix_bytes != 0 or prefix_sha != _EMPTY_SHA:
            raise ValueError("a fresh owner cannot report a bound spec or a journal prefix")
    else:
        if type(spec_sha) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", spec_sha) is None:
            raise ValueError("a recovered owner must report the original bound spec")
        if anchor.owner_process_epoch_sha256 == epoch:
            raise ValueError("a recovered owner is a new incarnation; it cannot reuse the original epoch")
    return ReadyObservation(
        status=status, anchor=anchor, anchor_sha256=value["anchor_sha256"], owner_epoch_sha256=value["owner_epoch_sha256"],
        spec_sha256=spec_sha, journal_prefix_bytes=prefix_bytes, journal_prefix_sha256=prefix_sha,
    )



def external_exit_problems(
    *, record: Mapping[str, Any], start: Mapping[str, Any], expected_start: Mapping[str, Any],
    ready_raw: bytes | None, observed_ready_raw: bytes | None,
    expected_anchor: M6OwnerAnchor | None = None, printed_exit: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """One pure fresh-owner exit rule for online supervision AND offline replay.

    The expected values are the launch command's projection of pinned inputs,
    never a summary success boolean. A record-shaped object is not proof that
    a process ran; this checks agreement of the retained original observations.
    """
    problems: list[str] = []
    expected_fields = {
        "run_id", "attempt_id", "hostname", "binary_sha256", "launcher_sha256", "configuration_sha256",
        "planned_seconds", "close_grace_seconds", "memory_bytes", "bootstrap_bind_seconds", "ready_wait_seconds",
    }
    if set(expected_start) != expected_fields:
        problems.append("launch_expectation_fields_differ")
    for key in expected_fields:
        value = expected_start.get(key)
        if key.endswith("sha256"):
            valid = type(value) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None
        elif key in {"run_id", "attempt_id", "hostname"}:
            valid = type(value) is str and bool(value) and len(value) <= 128
        else:
            valid = type(value) is int and value > 0
        if not valid:
            problems.append("launch_expectation_invalid:" + key)
        if key not in start or type(start[key]) is not type(value) or start[key] != value:
            problems.append("start_record_differs:" + key)
    if start.get("contract_version") != "m6.owner-external-start.v2":
        problems.append("start_record_contract_differs")
    if record.get("contract_version") != EXTERNAL_EXIT_CONTRACT:
        problems.append("exit_record_contract_differs")
    if record.get("scope") != "production_owner_run":
        problems.append("exit_record_scope_differs")
    if start.get("resume") is not False or start.get("original_anchor_sha256") != "none":
        problems.append("fresh_campaign_cannot_reuse_a_recovered_owner_start")
    for key in ("pid", "creation_filetime_100ns"):
        for label, value in (("start", start.get(key)), ("exit", record.get(key))):
            if type(value) is not int or value <= 0:
                problems.append(label + "_record_positive_integer_missing:" + key)
        if start.get(key) != record.get(key):
            problems.append("exit_record_differs_from_start:" + key)
    for key in ("run_id", "attempt_id", "hostname", "binary_sha256", "configuration_sha256", "launcher_sha256"):
        if key not in record or record[key] != expected_start.get(key):
            problems.append("exit_record_differs_from_expected_instance:" + key)
    if ready_raw is None or observed_ready_raw is None:
        problems.append("original_READY_observations_missing")
    elif ready_raw.rstrip(b"\r\n") != observed_ready_raw.rstrip(b"\r\n"):
        problems.append("fetched_READY_differs_from_transport_READY")
    else:
        try:
            ready = parse_ready_line(ready_raw.decode("utf-8").rstrip("\r\n"),
                                     expected_run_id=str(expected_start.get("run_id", "")))
            if expected_anchor is not None and ready.anchor != expected_anchor:
                problems.append("READY_differs_from_original_run_anchor")
            if (physical_owner_epoch_sha256(ready.anchor, pid=start.get("pid"),
                                            creation_filetime_100ns=start.get("creation_filetime_100ns"))
                    != ready.owner_epoch_sha256):
                problems.append("external_PID/birth_does_not_reproduce_READY_owner_epoch")
        except (ValueError, UnicodeError, TypeError) as exc:
            problems.append("READY_binding_invalid:" + type(exc).__name__)
    if printed_exit is not None and printed_exit != record:
        problems.append("printed_exit_differs_from_fetched_exit")
    for key, expected in {
        "exact_process_handle_opened": True, "process_handle_signaled": True,
        "ready_received": True, "ready_timeout": False, "forced_termination": False,
        "cancel": None, "parent_failure": None, "stdout_eof": True, "stderr_eof": True,
    }.items():
        if key not in record or record[key] is not expected:
            problems.append("exit_record_unproved:" + key)
    if type(record.get("exit_code")) is not int or record["exit_code"] != 0:
        problems.append("exit_code_is_not_integer_zero")
    return tuple(sorted(set(problems)))



def owner_identity_read_script(run_store: PureWindowsPath) -> str:
    """Read only the existing native owner_identity pair, with bounded enumeration/bytes.

    The private namespace is already created by the native store. This does not
    generate identity, change ACLs, query a new boot, or copy unrelated diagnostic
    bodies. The exact original pair, including its random basename, is retained.
    """
    if not _SAFE_PATH_RE.fullmatch(str(run_store)):
        raise ValueError("owner store path is outside the supported path grammar")
    return "$root='" + str(run_store) + "'\n" + r"""
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
function Read-Bounded([string]$Path,[int]$Maximum) {
    $item=Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'identity file is not ordinary' }
    $stream=[IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    try {
        if ($stream.Length -gt $Maximum) { throw 'identity file exceeds byte bound' }
        $buffer=[byte[]]::new($Maximum+1); $used=0
        while ($used -lt $buffer.Length) {
            $n=$stream.Read($buffer,$used,$buffer.Length-$used)
            if ($n -eq 0) { break }; $used += $n
        }
        if ($used -gt $Maximum) { throw 'identity file exceeds byte bound' }
        $result=[byte[]]::new($used); [Array]::Copy($buffer,$result,$used)
        return ,$result
    } finally { $stream.Dispose() }
}
$directory=Get-Item -LiteralPath $root -Force
if (-not $directory.PSIsContainer -or ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'owner store is not an ordinary directory' }
$utf8=[Text.UTF8Encoding]::new($false,$true)
$count=0; $selected=$null
foreach ($path in [IO.Directory]::EnumerateFiles($root,'diagnostic-*.json')) {
    $count++; if ($count -gt 4096) { throw 'diagnostic enumeration exceeds bound' }
    $name=[IO.Path]::GetFileName($path)
    if ($name -cnotmatch '^diagnostic-[0-9a-f]{32}\.json$') { throw 'diagnostic name differs' }
    $metadata=Read-Bounded $path 1024
    $value=$utf8.GetString($metadata) | ConvertFrom-Json
    if ($value.code -ceq 'owner_identity') {
        if ($null -ne $selected) { throw 'multiple owner identities' }
        $body=Read-Bounded ([IO.Path]::ChangeExtension($path,'.bin')) 65536
        $selected=[ordered]@{metadata_name=$name;metadata_base64=[Convert]::ToBase64String($metadata);body_base64=[Convert]::ToBase64String($body)}
    }
}
if ($null -eq $selected) { throw 'owner identity absent' }
'M6-IDENTITY ' + ($selected | ConvertTo-Json -Compress)
"""

def remaining_budget_seconds(*, anchor: M6OwnerAnchor, status: M6OwnerStatus, sent_ns: int, now_ns: int, until_ticks: int) -> float:
    """Conservative seconds left until ``until_ticks`` on the owner clock.

    The status was observed at some point after the request was sent, so the
    owner time is anchored to the local send instant (never to receipt), and
    the whole local interval since then is subtracted.
    """
    if type(sent_ns) is not int or type(now_ns) is not int or now_ns < sent_ns:
        raise ValueError("local monotonic interval regressed")
    owner_remaining = (until_ticks - status.observed_qpc_ticks) / anchor.clock.qpc_frequency_hz
    return owner_remaining - (now_ns - sent_ns) / 1_000_000_000


def sourced_child_argv(env_files: tuple[Path, ...], argv: list[str]) -> list[str]:
    """`run_worker_once` semantics: `set -a; source env files; set +a; exec argv` in one zsh child."""
    if len(env_files) != 2 or any(not path.is_absolute() for path in env_files):
        raise ValueError("exactly two absolute environment files are sourced")
    if not argv or any(type(item) is not str or "\0" in item for item in argv):
        raise ValueError("child argv must be non-empty strings")
    # `set -e`: a failing first source ends the child before the second source or any exec.
    script = 'set -e; set -a; . "$1"; . "$2"; set +a; shift 2; exec "$@"'
    return ["/bin/zsh", "-c", script, "m6-campaign-child", str(env_files[0]), str(env_files[1]), *argv]


def generate_roles(mode: str) -> dict[str, tuple[str, str]]:
    """Fresh (epoch, token) per role; the epoch is independent of the token and never derived from it."""
    return {
        role: ("sha256:" + hashlib.sha256(os.urandom(32)).hexdigest(), secrets.token_hex(32))
        for role in _ROLES_BY_MODE[mode]
    }


def deployment_document(
    *, intent: M6CampaignIntent, binding: CampaignPrivateBinding, run_root: PureWindowsPath,
    roles: Mapping[str, tuple[str, str]],
) -> dict[str, Any]:
    """The private owner deployment (`m6.owner-deployment.v2`) the launcher commits and the host loads."""
    windows = binding.windows
    if tuple(roles) != _ROLES_BY_MODE[intent.run.mode]:
        raise ValueError("deployment roles must be exactly the mode's principals in order")
    return {
        "contract_version": OWNER_DEPLOYMENT_CONTRACT,
        "run_id": intent.run.run_id,
        "run_root": str(run_root),
        "mode": intent.run.mode,
        "owner_source_sha256": windows.owner_source_sha256,
        "expected_node_sha256": windows.node_sha256,
        "gpu_uuid": windows.gpu_uuid,
        "nvml_dll_sha256": windows.nvml_dll_sha256,
        "port": windows.port,
        "resources": json.loads(intent.run.resources.canonical_bytes()),
        "max_artifacts": windows.max_artifacts,
        "max_artifact_bytes": windows.max_artifact_bytes,
        "maximum_lease_ticks": windows.maximum_lease_ticks,
        "propagation_reserve_ticks": windows.propagation_reserve_ticks,
        "bootstrap_bind_seconds": intent.bootstrap_bind_seconds,
        "roles": {role: {"epoch_sha256": epoch, "token": token} for role, (epoch, token) in roles.items()},
    }


def launcher_lines(stdout: bytes, prefix: str) -> list[str]:
    """All newline-terminated `<prefix> <json>` lines the launcher printed, in order; strict UTF-8.

    A trailing partial line is not evidence yet and is never returned; invalid
    UTF-8 in a prefixed line raises rather than being replaced.
    """
    marker = prefix.encode("ascii") + b" "
    complete = stdout.rsplit(b"\n", 1)[0] if b"\n" in stdout else b""
    lines = []
    for line in complete.split(b"\n"):
        if line.startswith(marker):
            try:
                lines.append(line[len(marker):].decode("utf-8").rstrip("\r"))
            except UnicodeDecodeError as exc:
                raise ValueError(f"launcher {prefix} line is not valid UTF-8") from exc
    return lines


def zero_admission_receipts(*, run_id: str, spec_sha256: str, runner_epoch_sha256: str, owner_identity: str) -> RunnerClosureReceipts:
    """The runner's real closure receipts for a run that admitted nothing (bootstrap-check)."""
    audit = M6ResourceAuditReceipt(
        run_id=run_id, spec_sha256=spec_sha256, runner_epoch_sha256=runner_epoch_sha256, owner_identity=owner_identity,
        coordinator_terminal="quiescent", in_flight_count=0, credits_in_use_zero=True, scratch_residual_count=0,
        children_exited=True,
    )
    reconciliation = M6AdmissionReconciliationReceipt(
        run_id=run_id, spec_sha256=spec_sha256, runner_epoch_sha256=runner_epoch_sha256, last_producer_sequence=0,
        admitted_attempt_count=0, admitted_attempt_set_sha256=attempt_set_sha256(()), unresolved_claim_count=0,
        unresolved_receipt_sha256=None,
    )
    ownership = M6OwnershipClosureReceipt(
        run_id=run_id, spec_sha256=spec_sha256, runner_epoch_sha256=runner_epoch_sha256, admitted_attempt_count=0,
        admitted_attempt_set_sha256=attempt_set_sha256(()), final_attempt_count=0,
        final_attempt_set_sha256=attempt_set_sha256(()), residual_count=0, children_exited=True,
        resource_audit_sha256=audit.canonical_sha256(),
    )
    return RunnerClosureReceipts(audit, None, reconciliation, ownership)


# --- inputs ---------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CampaignInputs:
    intent: M6CampaignIntent
    intent_raw: bytes
    intent_sha256: str
    binding: CampaignPrivateBinding
    release_binding: dict[str, Any]
    release_manifest: dict[str, Any]
    manifest_path: Path
    scope_path: Path
    quality_plan_path: Path
    manifest: M6CorpusManifest
    scope: M6CampaignScope
    quality_plan: M6QualityPlan
    worker_env_sha256: str
    evaluation_plan: M6EvaluationPlan
    evaluation_plan_raw: bytes


def load_campaign_inputs(
    *, intent_path: Path, intent_sha256: str, private_binding_path: Path, binding_path: Path,
    release_manifest_path: Path, manifest_path: Path, scope_path: Path, quality_plan_path: Path,
    evaluation_plan_path: Path,
) -> CampaignInputs:
    """Load and pin every input; every cross-reference is checked before anything is created.

    The evaluation plan (Pro R20 §4) is part of the frozen intent: its bytes must hash to
    `intent.evaluation_plan_sha256` and its mode must be the run's mode, before Prepare.
    """
    intent_raw = _read_pinned(intent_path, intent_sha256, label="campaign intent")
    try:
        intent = M6CampaignIntent.from_canonical_bytes(intent_raw, maximum_bytes=M6_CAMPAIGN_INTENT_MAX_BYTES)
    except ValueError as exc:
        raise CampaignInputError(f"campaign intent invalid (the live entry requires {M6CampaignIntent.model_fields['contract_version'].default}): {exc}") from exc
    evaluation_plan_raw = _read_pinned(evaluation_plan_path, intent.evaluation_plan_sha256, label="evaluation plan")
    try:
        evaluation_plan = M6EvaluationPlan.from_canonical_bytes(evaluation_plan_raw, maximum_bytes=M6_EVALUATION_PLAN_MAX_BYTES)
    except ValueError as exc:
        raise CampaignInputError(f"evaluation plan invalid: {exc}") from exc
    if evaluation_plan.mode != intent.run.mode:
        raise CampaignIdentityError("evaluation plan mode differs from the run intent")
    try:
        binding = load_campaign_private_binding(private_binding_path)
        assert_known_hosts_pins_address(binding.ssh)
    except CampaignBindingError as exc:
        raise CampaignInputError(str(exc)) from exc
    release_binding = load_closed_object(
        _read_pinned(binding_path, intent.binding_sha256, label="release binding"), label="release binding", maximum_bytes=65536,
    )
    require_fields(release_binding, {
        "contract_version", "release_manifest_sha256", "source_head", "runtime_bundle_identity_sha256",
        "deployment_qualification_sha256", "deployment_qualification_document_count", "capacity_config_sha256",
        "process_profile_sha256", "activation_sha256", "stream_ceiling", "owner", "cgroup_identity_sha256",
        "cgroup_max_bytes", "gpu_uuid", "worker_overlay_sha256", "worker_env_sha256", "bound_at_utc", "paths",
    }, label="release binding")
    release_manifest = load_closed_object(
        _read_pinned(release_manifest_path, intent.release_manifest_sha256, label="release manifest"),
        label="release manifest", maximum_bytes=_MAX_INPUT_BYTES,
    )
    source = release_manifest.get("source")
    native = release_manifest.get("native_m6")
    if type(source) is not dict or type(native) is not dict:
        raise CampaignInputError("release manifest lacks source/native_m6 sections")
    runtime = intent.runtime
    checks = {
        "release binding names another release": release_binding["release_manifest_sha256"] == intent.release_manifest_sha256,
        "release binding source head differs from the intent's source commit": release_binding["source_head"] == runtime.source_commit,
        "release manifest source head differs from the intent's source commit": source.get("head") == runtime.source_commit,
        "release manifest source manifest differs from the intent": source.get("source_manifest_sha256") == runtime.source_manifest_sha256,
        "release binding runtime bundle differs from the intent": release_binding["runtime_bundle_identity_sha256"] == runtime.runtime_bundle_identity_sha256,
        "release binding process profile differs from the intent": release_binding["process_profile_sha256"] == runtime.process_profile_sha256,
        "release binding qualification differs from the intent": release_binding["deployment_qualification_sha256"] == runtime.deployment_qualification_sha256,
        "release binding GPU differs from the campaign transport target": release_binding["gpu_uuid"] == binding.windows.gpu_uuid,
        "campaign launcher is not the release's launcher": native.get("launcher_sha256") == binding.windows.launcher_sha256,
        "campaign owner source is not the release's production source manifest": native.get("production_source_manifest_sha256") == binding.windows.owner_source_sha256,
    }
    failed = [reason for reason, ok in checks.items() if not ok]
    if failed:
        raise CampaignIdentityError("; ".join(failed))
    worker_env_sha256 = sha256_of((binding.env_dir / "worker.env").read_bytes())
    if release_binding["worker_env_sha256"] != worker_env_sha256:
        raise CampaignIdentityError("worker.env bytes differ from the release binding's merged environment")
    manifest_raw = _read_pinned(manifest_path, intent.run.manifest_sha256, label="corpus manifest")
    plan_raw = _read_pinned(quality_plan_path, intent.run.quality_plan_sha256, label="quality plan")
    if intent.run.scope_sha256 is None:
        raise CampaignInputError("the campaign entry runs publication campaigns; a scope is required")
    scope_raw = _read_pinned(scope_path, intent.run.scope_sha256, label="campaign scope")
    try:
        manifest = M6CorpusManifest.from_canonical_bytes(manifest_raw, maximum_bytes=_MAX_INPUT_BYTES)
        scope = M6CampaignScope.from_canonical_bytes(scope_raw, maximum_bytes=_MAX_INPUT_BYTES)
        plan = M6QualityPlan.from_canonical_bytes(plan_raw, maximum_bytes=_MAX_INPUT_BYTES)
    except ValueError as exc:
        raise CampaignInputError(f"campaign input invalid: {exc}") from exc
    if scope.campaign_id != intent.run.campaign_id:
        raise CampaignIdentityError("campaign scope names another campaign than the intent")
    return CampaignInputs(
        intent=intent, intent_raw=intent_raw, intent_sha256=intent_sha256, binding=binding, release_binding=release_binding,
        release_manifest=release_manifest, manifest_path=manifest_path, scope_path=scope_path,
        quality_plan_path=quality_plan_path, manifest=manifest, scope=scope, quality_plan=plan,
        worker_env_sha256=worker_env_sha256, evaluation_plan=evaluation_plan, evaluation_plan_raw=evaluation_plan_raw,
    )


# --- run directory ------------------------------------------------------------------

def write_run_directory(
    run_dir: Path, *, roles: Mapping[str, tuple[str, str]],
) -> None:
    """Stage the private part of the `M6RunDirectory` layout before READY: tokens (0600) and roles.json.

    The transport, whose lease policy needs the owner's actual QPC frequency, is
    written exactly once by ``write_run_transport`` after READY; anchor and spec
    follow with the bind.
    """
    if run_dir.exists() or run_dir.is_symlink():
        raise CampaignInputError("run directory must be new")
    run_dir.mkdir(mode=0o700)
    (run_dir / "tokens").mkdir(mode=0o700)
    role_entries: dict[str, dict[str, str]] = {}
    for role, (epoch, token) in roles.items():
        token_path = run_dir / "tokens" / f"{role}.token"
        write_new_exact(token_path, token.encode("ascii") + b"\n")
        role_entries[role] = {"epoch_sha256": epoch, "token_path": str(token_path)}
    write_new_exact(run_dir / "roles.json", canonical_bytes(role_entries) + b"\n")


def derive_runner_lease_policy(
    *, binding: CampaignPrivateBinding, intent: M6CampaignIntent, qpc_frequency_hz: int,
) -> M6LeasePolicy:
    """The runner's lease guard from the frozen binding the owner itself was configured with.

    One authority: the native owner grants ``windows.maximum_lease_ticks`` and
    reserves ``windows.propagation_reserve_ticks``; the client ceiling is those
    same ticks converted at the READY anchor's actual QPC frequency, never a
    default and never an assumed frequency. An inexact conversion, a reserve
    that disagrees with the intent, a lease outside the policy bounds, or a
    lease the stop budget cannot cover fails here, before any bind, open,
    admission or business work. Margin and drift keep the policy defaults.
    """
    if isinstance(qpc_frequency_hz, bool) or type(qpc_frequency_hz) is not int or qpc_frequency_hz <= 0:
        raise CampaignInputError("READY anchor QPC frequency is not a positive integer")
    windows = binding.windows
    lease_ns, lease_remainder = divmod(windows.maximum_lease_ticks * 1_000_000_000, qpc_frequency_hz)
    reserve_ns, reserve_remainder = divmod(windows.propagation_reserve_ticks * 1_000_000_000, qpc_frequency_hz)
    if lease_remainder or reserve_remainder:
        raise CampaignInputError(
            f"binding lease/reserve ticks are not exact nanoseconds at the READY QPC frequency {qpc_frequency_hz} Hz",
        )
    if reserve_ns != intent.stop_propagation_reserve_ns:
        raise CampaignInputError(
            f"binding propagation reserve ({reserve_ns} ns at {qpc_frequency_hz} Hz) differs from the intent's "
            f"stop propagation reserve ({intent.stop_propagation_reserve_ns} ns)",
        )
    try:
        policy = M6LeasePolicy(stop_propagation_reserve_ns=intent.stop_propagation_reserve_ns, maximum_lease_ns=lease_ns)
    except ValueError as exc:
        raise CampaignInputError(f"binding lease of {lease_ns} ns is outside the runner lease policy: {exc}") from exc
    budget_ns = intent.run.resources.stop_admission_budget_ticks * 1_000_000_000 // qpc_frequency_hz
    if lease_ns > budget_ns - policy.stop_propagation_reserve_ns:
        raise CampaignInputError(
            f"binding lease of {lease_ns} ns exceeds the stop budget minus the propagation reserve "
            f"({budget_ns - policy.stop_propagation_reserve_ns} ns); the runner would refuse the owner's own grant",
        )
    if lease_ns <= policy.uncertainty_margin_ns:
        raise CampaignInputError("binding lease leaves no usable grant after the uncertainty margin")
    return policy


def write_run_transport(
    run_dir: Path, *, binding: CampaignPrivateBinding, intent: M6CampaignIntent, qpc_frequency_hz: int,
) -> M6LeasePolicy:
    """Write `transport.json` exactly once, after READY, with the lease policy derived from the binding.

    ``write_new_exact`` refuses an existing file, so a transport that a client
    may already have consumed is never rewritten.
    """
    policy = derive_runner_lease_policy(binding=binding, intent=intent, qpc_frequency_hz=qpc_frequency_hz)
    ssh = binding.ssh
    transport = {
        "ssh": {
            "address": ssh.address, "port": ssh.port, "username": ssh.username,
            "private_key_path": ssh.private_key_path, "known_hosts_path": ssh.known_hosts_path,
            "executable_path": str(binding.ssh_executable),
            "executable_sha256": binding.ssh_executable_sha256,
        },
        "remote_port": binding.windows.port,
        "lease": {
            "stop_propagation_reserve_ns": policy.stop_propagation_reserve_ns,
            "maximum_lease_ns": policy.maximum_lease_ns,
            "uncertainty_margin_ns": policy.uncertainty_margin_ns,
            "maximum_clock_drift_ppm": policy.maximum_clock_drift_ppm,
        },
    }
    write_new_exact(run_dir / "transport.json", canonical_bytes(transport) + b"\n")
    return policy


# --- the composition root ----------------------------------------------------------

LaunchFactory = Callable[..., BoundedOwnerCommand]


@dataclass
class CampaignSummary:
    """Mutable record of what happened; serialised once at the end, never as success while uncertain."""

    mode: str
    run_id: str
    attempt_id: str
    intent_sha256: str
    status: str = "unknown"
    stages: list[dict[str, Any]] = field(default_factory=list)
    first_error: dict[str, str] | None = None
    values: dict[str, Any] = field(default_factory=dict)

    def stage(self, name: str, **detail: Any) -> None:
        self.stages.append({"stage": name, "utc": _utc(), **detail})

    def fail(self, stage: str, message: str) -> None:
        if self.first_error is None:
            self.first_error = {"stage": stage, "message": message[:2000]}
        self.stage(stage, failed=True, message=message[:2000])


class M6CampaignAssembly:
    """Compose one campaign run or bootstrap-check; all effects go through explicit injected factories."""

    def __init__(
        self, inputs: CampaignInputs, *, output: Path, mode: CampaignMode, attempt_id: str,
        launch: LaunchFactory = BoundedOwnerCommand, continuous_ns: Callable[[], int] | None = None,
        admission_stop_file: Path | None = None,
    ) -> None:
        if mode not in ("run", "bootstrap-check"):
            raise ValueError("campaign mode must be run or bootstrap-check")
        if _ID_RE.fullmatch(attempt_id) is None:
            raise ValueError("attempt id shape")
        if not output.is_absolute() or output.exists() or output.is_symlink() or not output.parent.is_dir():
            raise CampaignInputError("campaign output must be a new absolute directory under an existing parent")
        runtime_root = inputs.binding.runtime_root.resolve()
        if not output.parent.resolve().is_relative_to(runtime_root):
            raise CampaignInputError("campaign output must live under the runtime root")
        if admission_stop_file is not None:
            if (not admission_stop_file.is_absolute()
                    or admission_stop_file.parent.resolve() != output.parent.resolve()
                    or admission_stop_file.is_symlink()):
                raise CampaignInputError("admission stop file must be a direct child of the private campaign parent")
        self._admission_stop_file = admission_stop_file
        self._inputs = inputs
        self._intent = inputs.intent
        self._binding = inputs.binding
        self._output = output
        self._mode: CampaignMode = mode
        self._attempt_id = attempt_id
        self._launch = launch
        self._now_ns = continuous_ns or diagnostic_continuous_clock().now_ns
        self._workspace = self._binding.windows.workspace_root / f"m6-{self._intent.run.run_id}"
        self._private_root = self._workspace / "private"
        self._attempt_dir = self._private_root / "attempts" / attempt_id
        self._summary = CampaignSummary(mode=mode, run_id=self._intent.run.run_id, attempt_id=attempt_id,
                                        intent_sha256=inputs.intent_sha256)
        self._launcher: BoundedOwnerCommand | None = None
        self._launcher_result: OwnerCommandResult | None = None
        self._children_reaped = mode == "bootstrap-check"
        self._cleanup_failures: dict[str, str] = {}
        self._run: M6RunDirectory | None = None
        self._controller: M6OwnerClient | None = None
        self._spawned_local_ns: int | None = None
        self._launcher_deadline_ns: int | None = None
        self._launch_budget: LaunchTransportBudget | None = None

    def _external_stop_requested(self) -> bool:
        """A one-way admission stop, never a process-cancellation or closure receipt.

        The driver and runner share this exact file. The private, fresh parent is
        already bound by the entry; a pre-created valid stop prevents admission.
        Invalid control bytes are a control-integrity failure, not a silent resume.
        """
        path = self._admission_stop_file
        if path is None:
            return False
        try:
            # O_NONBLOCK: a FIFO planted at this path must not park the supervisor in open(2);
            # it opens immediately and is then rejected below as a non-regular file.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CampaignInputError(f"admission stop control is not readable: {type(exc).__name__}: {exc}") from exc
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != 5
                    or os.read(fd, 6) != b"stop\n"):
                raise CampaignInputError("admission stop control identity or bytes changed")
        finally:
            os.close(fd)
        return True

    # --- remote helpers ----------------------------------------------------------

    def _script(self, arguments: Mapping[str, str | bool]) -> str:
        windows = self._binding.windows
        return pinned_windows_script(
            script_path=str(windows.launcher_path), expected_sha256=windows.launcher_sha256, arguments=arguments,
        )

    def _ssh_argv(self, script: str) -> list[str]:
        return owner_ssh_command(
            executable=self._binding.ssh_executable, executable_sha256=self._binding.ssh_executable_sha256,
            ssh=self._binding.ssh, script=script,
        )

    def _sftp_argv(self, batch: Path) -> list[str]:
        ssh = self._binding.ssh
        return [
            str(self._binding.sftp_executable), "-b", str(batch), "-F", "/dev/null", "-P", str(ssh.port), "-i", ssh.private_key_path,
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=" + ssh.known_hosts_path, "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10", "-o", "IdentityAgent=none", "-o", "ClearAllForwardings=yes",
            "-o", "PreferredAuthentications=publickey", f"{ssh.username}@{ssh.address}",
        ]

    def _owned(self, argv: list[str], *, label: str, timeout_seconds: float, maximum_bytes: int = 262144) -> OwnerCommandResult:
        write_new_exact(self._output / f"{label}-command.json", canonical_bytes({
            "argv": argv, "timeout_seconds": timeout_seconds, "started_utc": _utc(),
        }) + b"\n")
        command = self._launch(argv, timeout_seconds=timeout_seconds, maximum_bytes=maximum_bytes)
        try:
            result = command.finish()
        except (TimeoutError, ValueError) as exc:
            stdout, stderr = command.captured_output
            write_new_exact(self._output / f"{label}-stdout.raw", stdout)
            write_new_exact(self._output / f"{label}-stderr.raw", stderr)
            raise CampaignOutcomeUnknown(f"{label}: {exc}") from exc
        write_new_exact(self._output / f"{label}-stdout.raw", result.stdout)
        write_new_exact(self._output / f"{label}-stderr.raw", result.stderr)
        write_new_exact(self._output / f"{label}-execution.json", canonical_bytes({
            "exit_code": result.exit_code, "finished_utc": _utc(),
        }) + b"\n")
        return result

    @staticmethod
    def _parse_launcher_result(stdout: bytes, *, label: str) -> dict[str, Any]:
        lines = launcher_lines(stdout, "M6-RESULT")
        if len(lines) != 1:
            raise CampaignOutcomeUnknown(f"{label}: launcher printed {len(lines)} result lines, expected exactly one")
        value = strict_json_loads(lines[0])
        if type(value) is not dict or value.get("contract_version") != LAUNCHER_RESULT_CONTRACT:
            raise CampaignOutcomeUnknown(f"{label}: launcher result is not {LAUNCHER_RESULT_CONTRACT}")
        return value

    # --- stages ------------------------------------------------------------------

    def _prepare(self) -> dict[str, Any]:
        windows = self._binding.windows
        result = self._owned(self._ssh_argv(self._script({
            "Prepare": True, "WorkspaceRoot": str(self._workspace), "ExpectedHostname": windows.hostname,
            "BinaryPath": str(windows.owner_executable_path), "ExpectedBinarySha256": windows.owner_executable_sha256,
        })), label="prepare", timeout_seconds=_PREPARE_TIMEOUT_SECONDS)
        outcome = self._parse_launcher_result(result.stdout, label="prepare")
        if result.exit_code != 0 or outcome.get("exit_code") != 0:
            raise CampaignOutcomeUnknown(f"prepare failed: ssh exit {result.exit_code}, launcher {outcome.get('first_error')}")
        receipts = launcher_lines(result.stdout, "M6-PREPARE")
        if len(receipts) != 1:
            raise CampaignOutcomeUnknown("prepare produced no single receipt")
        receipt = strict_json_loads(receipts[0])
        if type(receipt) is not dict or receipt.get("contract_version") != PREPARE_RECEIPT_CONTRACT:
            raise CampaignIdentityError("prepare receipt contract differs")
        expected = {
            "hostname": windows.hostname, "workspace_root": str(self._workspace), "private_root": str(self._private_root),
            "runs_root": str(self._private_root / "runs"), "staging_root": str(self._private_root / "staging"),
            "attempts_root": str(self._private_root / "attempts"), "binary_sha256": windows.owner_executable_sha256,
            "launcher_sha256": windows.launcher_sha256,
        }
        differing = sorted(key for key, value in expected.items() if receipt.get(key) != value)
        if differing:
            raise CampaignIdentityError("prepare receipt differs from the campaign target: " + ", ".join(differing))
        directories = receipt.get("directories")
        if type(directories) is not dict or set(directories) != {expected["private_root"], expected["runs_root"], expected["staging_root"], expected["attempts_root"]}:
            raise CampaignIdentityError("prepare receipt does not name exactly the four private directories")
        owner_sid = receipt.get("owner_sid")
        if type(owner_sid) is not str or not owner_sid.startswith("S-1-5-21-"):
            raise CampaignIdentityError("prepare receipt owner is not a local account SID")
        for path, entry in directories.items():
            sddl = entry.get("sddl") if type(entry) is dict else None
            # Owner must be this account and the DACL protected (D:P) with only the owner and SYSTEM allowed.
            if (type(entry) is not dict or entry.get("protected") is not True or type(sddl) is not str
                    or not sddl.startswith("O:" + owner_sid) or "D:P" not in sddl):
                raise CampaignIdentityError(f"prepare receipt entry for {path} is not a protected owner-only ACL")
            aces = re.findall(r"\(([^)]*)\)", sddl.split("D:P", 1)[1])
            allowed = {owner_sid, "SY"}
            if not aces or any(ace.split(";")[0] != "A" or ace.split(";")[-1] not in allowed for ace in aces):
                raise CampaignIdentityError(f"prepare receipt ACL for {path} grants another principal")
        write_new_exact(self._output / "prepare-receipt.json", receipts[0].encode("utf-8") + b"\n")
        return receipt

    def _stage_deployment(self, deployment_raw: bytes) -> str:
        staged_name = "deployment.private.json"
        local = self._output / "private" / staged_name
        write_new_exact(local, deployment_raw)
        batch = self._output / "stage.batch"
        remote = self._private_root / "staging" / staged_name
        write_new_exact(batch, (f"put {_quoted(str(local))} {_quoted(_sftp_path(remote))}\n").encode("utf-8"))
        result = self._owned(self._sftp_argv(batch), label="stage", timeout_seconds=_STAGE_TIMEOUT_SECONDS)
        if result.exit_code != 0:
            raise CampaignOutcomeUnknown(f"staging the private deployment failed with sftp exit {result.exit_code}")
        return staged_name

    def _transport_budget(self) -> LaunchTransportBudget:
        """The launcher transport budget is a pure function of the frozen intent and the launcher's own bounds."""
        if self._launch_budget is None:
            intent = self._intent
            self._launch_budget = launch_transport_budget(
                planned_seconds=intent.run.planned_seconds, close_grace_seconds=intent.close_grace_seconds,
                ready_wait_seconds=intent.ready_wait_seconds, ssh_overhead_seconds=_SSH_OVERHEAD_SECONDS,
                exit_wait_extra_seconds=_LAUNCHER_EXIT_WAIT_EXTRA_SECONDS, launcher_drain_seconds=_LAUNCHER_DRAIN_SECONDS,
            )
        return self._launch_budget

    def _expected_external_start(self, deployment_sha256: str | None) -> dict[str, Any]:
        windows, intent = self._binding.windows, self._intent
        return {
            "run_id": intent.run.run_id, "attempt_id": self._attempt_id, "hostname": windows.hostname,
            "binary_sha256": windows.owner_executable_sha256, "launcher_sha256": windows.launcher_sha256,
            "configuration_sha256": deployment_sha256, "planned_seconds": intent.run.planned_seconds,
            "close_grace_seconds": intent.close_grace_seconds, "memory_bytes": intent.memory_bytes,
            "bootstrap_bind_seconds": intent.bootstrap_bind_seconds, "ready_wait_seconds": intent.ready_wait_seconds,
        }

    def _start_launcher(self, *, staged_name: str, deployment_sha256: str) -> None:
        windows, intent = self._binding.windows, self._intent
        script = self._script({
            "Run": True, "WorkspaceRoot": str(self._workspace), "ExpectedHostname": windows.hostname,
            "BinaryPath": str(windows.owner_executable_path), "ExpectedBinarySha256": windows.owner_executable_sha256,
            "AttemptId": self._attempt_id, "StagedConfigurationName": staged_name,
            "ExpectedConfigurationSha256": deployment_sha256, "ExpectedRunId": intent.run.run_id,
            "PlannedSeconds": str(intent.run.planned_seconds), "CloseGraceSeconds": str(intent.close_grace_seconds),
            "MemoryBytes": str(intent.memory_bytes), "ReadyWaitSeconds": str(intent.ready_wait_seconds),
            "ExitWaitExtraSeconds": str(_LAUNCHER_EXIT_WAIT_EXTRA_SECONDS),
        })
        argv = self._ssh_argv(script)
        # One fixed transport deadline from the local spawn: the bounded pre-T0 delay (READY wait plus transport
        # overhead, the same bound _await_ready enforces) + the owner's full planned+grace window + the launcher's
        # own post-close exit/drain allowance + ssh teardown. Only this transport may use the extended ceiling.
        budget = self._transport_budget()
        timeout = budget.timeout_seconds
        write_new_exact(self._output / "launcher-command.json", canonical_bytes({
            "argv": argv, "timeout_seconds": timeout, "lifetime_ceiling_seconds": budget.lifetime_ceiling_seconds,
            "launch_budget": budget.as_dict(), "started_utc": _utc(),
            "intent_sha256": self._inputs.intent_sha256,
            "expected_start": self._expected_external_start(deployment_sha256),
        }) + b"\n")
        self._spawned_local_ns = self._now_ns()
        self._launcher_deadline_ns = self._spawned_local_ns + int(timeout * 1_000_000_000)
        self._launcher = self._launch(argv, timeout_seconds=timeout, maximum_bytes=_LAUNCHER_CAPTURE_BYTES,
                                      retention="head_tail", lifetime_ceiling_seconds=budget.lifetime_ceiling_seconds)

    def _await_ready(self) -> ReadyObservation:
        assert self._launcher is not None and self._spawned_local_ns is not None
        deadline_ns = self._spawned_local_ns + int((self._intent.ready_wait_seconds + _SSH_OVERHEAD_SECONDS) * 1_000_000_000)
        while True:
            result = self._launcher.poll(timeout=0.5)
            stdout, _ = self._launcher.captured_output
            lines = launcher_lines(stdout, "M6-READY")
            if lines:
                ready = parse_ready_line(lines[0], expected_run_id=self._intent.run.run_id)
                write_new_exact(self._output / "ready.json", lines[0].encode("utf-8") + b"\n")
                self._summary.values["ready_line"] = lines[0]
                self._summary.stage("ready", status=ready.status, anchor_sha256=ready.anchor_sha256,
                                    owner_epoch_sha256=ready.owner_epoch_sha256,
                                    local_wait_seconds=(self._now_ns() - self._spawned_local_ns) / 1e9)
                return ready
            if result is not None:
                self._launcher_result = result
                raise CampaignOutcomeUnknown(f"launcher exited {result.exit_code} before READY")
            if self._now_ns() >= deadline_ns:
                raise CampaignOutcomeUnknown("no READY line within the ready wait plus transport overhead")

    def _bind_and_open(self, ready: ReadyObservation) -> tuple[M6RunSpec, M6OwnerStatus, int]:
        run_dir = self._output / "run"
        write_new_exact(run_dir / "anchor.json", ready.anchor.canonical_bytes())
        spec = build_run_spec(anchor=ready.anchor, intent=self._intent.run, runtime=self._intent.runtime)
        write_new_exact(run_dir / "run-spec.json", spec.canonical_bytes())
        # The runner's lease ceiling is the owner's own configured grant, converted at
        # the frequency the owner just reported; it is fixed here, once, before any
        # client loads the transport and before the controller binds or opens.
        lease = write_run_transport(
            run_dir, binding=self._binding, intent=self._intent, qpc_frequency_hz=ready.anchor.clock.qpc_frequency_hz,
        )
        self._summary.values["runner_lease"] = {
            "qpc_frequency_hz": ready.anchor.clock.qpc_frequency_hz,
            "maximum_lease_ticks": self._binding.windows.maximum_lease_ticks,
            "maximum_lease_ns": lease.maximum_lease_ns, "stop_propagation_reserve_ns": lease.stop_propagation_reserve_ns,
            "uncertainty_margin_ns": lease.uncertainty_margin_ns, "maximum_clock_drift_ppm": lease.maximum_clock_drift_ppm,
        }
        self._run = load_m6_run_directory(run_dir)
        self._controller = m6_owner_client_factory(self._run, role="controller", continuous_ns=self._now_ns)()
        reply = self._controller.bind()
        if reply.status.spec_sha256 != spec.canonical_sha256() or reply.status.run_id != spec.run_id:
            raise CampaignIdentityError("owner reports a different bound spec or run")
        self._summary.stage("bind", outcome=reply.outcome, owner_state=reply.status.state,
                            spec_sha256=spec.canonical_sha256())
        # Identical rebind must be idempotent: same status, no new T0 (the anchor is immutable).
        again = self._controller.bind()
        if again.status.spec_sha256 != reply.status.spec_sha256 or again.status.anchor_sha256 != ready.anchor_sha256:
            raise CampaignIdentityError("rebind changed the owner's bound identity")
        self._summary.values["bind"] = {"outcome": reply.outcome, "idempotent_replay": again.outcome == "ok"}
        sent_ns = self._now_ns()
        opened = self._controller.request(M6OwnerControl(kind="open"))
        self._summary.stage("open", owner_state=opened.status.state)
        return spec, opened.status, sent_ns

    def _child_argv(self, module: str, arguments: list[str]) -> list[str]:
        return sourced_child_argv(self._binding.env_files(), [str(self._binding.python_executable), "-m", module, *arguments])

    def _child_environment(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", str(Path.home())),
            "PYTHONPATH": str(self._binding.service_root / "src"), "PYTHONUNBUFFERED": "1", "LANG": "C.UTF-8",
            "DISCLOSURE_ENV_DIR": str(self._binding.env_dir),
        }

    def _supervise_children(self, spec: M6RunSpec, status: M6OwnerStatus, sent_ns: int) -> dict[str, Any]:
        assert self._run is not None and self._controller is not None and self._launcher is not None
        intent, inputs = self._intent, self._inputs
        anchor = self._run.anchor
        remaining_admission = remaining_budget_seconds(anchor=anchor, status=status, sent_ns=sent_ns, now_ns=self._now_ns(),
                                                       until_ticks=anchor.deadline_ticks)
        remaining_close = remaining_budget_seconds(anchor=anchor, status=status, sent_ns=sent_ns, now_ns=self._now_ns(),
                                                   until_ticks=anchor.max_close_ticks)
        runner_max = int(remaining_admission) - intent.runner_stop_reserve_seconds
        verifier_deadline = min(intent.verifier_deadline_seconds, int(remaining_close))
        # Every business child lives at most until the owner's original max_close, measured conservatively
        # from the open reply and the local monotonic interval since; nothing renews or extends it. The
        # transport/reap tail of a child is BoundedOwnerCommand's own bounded abort, never extra budget.
        child_lifetime = min(7200.0, remaining_close)
        if runner_max < 1 or verifier_deadline < 1 or child_lifetime <= 0:
            raise CampaignOutcomeUnknown("no admission or verification budget remains after bind/open")
        # Now that T0 is known, the fixed launcher transport deadline must still hold the owner's legal close
        # plus the launcher's own exit tail; otherwise no admission may start (a truncated transport is never
        # taken as a business close).
        budget = self._transport_budget()
        transport_headroom: float | None = None
        if self._launcher_deadline_ns is not None:
            transport_headroom = transport_headroom_seconds(
                launcher_deadline_ns=self._launcher_deadline_ns, now_ns=self._now_ns(),
                remaining_close_seconds=max(0.0, remaining_close), post_close_seconds=budget.post_close_seconds,
            )
            if transport_headroom < 0:
                raise CampaignOutcomeUnknown(
                    f"launcher transport deadline cannot cover the owner's close bound ({transport_headroom:.1f}s short)")
        self._summary.values["budgets"] = {
            "planned_seconds": intent.run.planned_seconds, "remaining_at_runner_start_seconds": remaining_admission,
            "remaining_to_max_close_seconds": remaining_close, "child_lifetime_seconds": child_lifetime,
            "runner_max_seconds": runner_max, "verifier_deadline_seconds": verifier_deadline,
            # None only when this composition root did not spawn the transport itself (never in a real run).
            "launcher_transport_headroom_seconds": transport_headroom,
            "launcher_post_close_allowance_seconds": budget.post_close_seconds,
            "launcher_transport_timeout_seconds": budget.timeout_seconds,
        }
        runner_dir, verifier_dir = self._output / "runner", self._output / "verifier"
        runner_dir.mkdir(mode=0o700)
        receipt = runner_dir / "campaign-receipt.json"
        stop_file = self._admission_stop_file or (runner_dir / "STOP")
        observation = runner_dir / "observation"
        runner_argv = self._child_argv("disclosure_anchor.cli.staged_campaign", [
            "--manifest", str(inputs.manifest_path), "--manifest-sha256", intent.run.manifest_sha256,
            "--scope", str(inputs.scope_path), "--scope-sha256", intent.run.scope_sha256 or "",
            "--max-seconds", str(runner_max), "--activation-role", "candidate", "--receipt-out", str(receipt),
            "--stop-file", str(stop_file), "--observation-out", str(observation), "--m6-run-dir", str(self._run.path),
        ])
        # The verifier tails the runner's spool *file*: the runner creates
        # <observation>/m6-assembly/<SPOOL_FILENAME> (staged_campaign + M6LifecycleSpool).
        verifier_argv = self._child_argv("disclosure_anchor.cli.m6_verifier_supervisor", [
            "--m6-run-dir", str(self._run.path), "--runner-spool", str(observation / "m6-assembly" / SPOOL_FILENAME),
            "--runner-receipt", str(receipt), "--output-dir", str(verifier_dir), "--verifier-identity", intent.verifier_identity,
            "--plan", str(inputs.quality_plan_path), "--deadline-seconds", str(verifier_deadline),
        ])
        environment, cwd = self._child_environment(), str(self._binding.service_root)
        for label, argv in (("runner", runner_argv), ("verifier", verifier_argv)):
            write_new_exact(self._output / f"{label}-command.json", canonical_bytes({"argv": argv, "started_utc": _utc()}) + b"\n")
        # Ownership starts at the first spawn: a later spawn failure aborts every child already owned,
        # and the original failure stays the reported error.
        children: dict[str, BoundedOwnerCommand] = {}
        results: dict[str, OwnerCommandResult | None] = {"runner": None, "verifier": None}
        failures: dict[str, str] = {}
        stop_requested = False
        controller = self._controller

        def stop_admission_if_requested() -> None:
            nonlocal stop_requested
            if stop_requested:
                return
            try:
                requested = self._external_stop_requested()
            except CampaignInputError as exc:
                # An invalid control at the shared path fails closed: admission stops (the runner already
                # treats the path's existence as STOP), the owner is asked to stop, and the integrity
                # failure is named. Business children are never aborted for it.
                requested = True
                failures["external_admission_stop_invalid"] = str(exc)[:500]
            if not requested:
                return
            stop_requested = True
            failures.setdefault("external_admission_stop", "external admission stop; original close deadline unchanged")
            self._summary.fail("supervision", "; ".join(f"{k}: {v}" for k, v in failures.items()))
            try:
                controller.request(M6OwnerControl(kind="stop"))
            except (M6OwnerRejected, M6OwnerProtocolError, EOFError, RuntimeError, OSError) as exc:
                self._summary.stage("stop_request_failed", message=str(exc)[:500])

        # No business child has been spawned: reuse the existing zero-admission
        # closure instead of withholding the runner's first lease and then
        # inventing a runner receipt. This branch is forbidden after any spawn.
        if self._external_stop_requested():
            self._summary.fail("supervision", "external admission stop before business spawn")
            self._children_reaped = True
            record = self._bootstrap_closure(spec)
            record["failures"] = {"external_admission_stop": "before business spawn"}
            return record
        launches = (("runner", runner_argv), ("verifier", verifier_argv))
        try:
            for label, argv in launches:
                # Pro §3.5: the time already consumed by preparation and earlier spawns is deducted; the lifetime
                # is recomputed from the same owner-clock status immediately before each spawn.
                lifetime = min(7200.0, remaining_budget_seconds(anchor=anchor, status=status, sent_ns=sent_ns,
                                                                now_ns=self._now_ns(), until_ticks=anchor.max_close_ticks))
                if lifetime <= 0:
                    raise CampaignOutcomeUnknown(f"original close bound exhausted before spawning the {label}")
                self._summary.stage(f"{label}_spawn", lifetime_seconds=lifetime)
                children[label] = self._launch(argv, timeout_seconds=lifetime, maximum_bytes=_CHILD_CAPTURE_BYTES,
                                               environment=environment, cwd=cwd, retention="head_tail")
        except BaseException as spawn_error:
            # Every child already owned is reaped now; the spawn failure stays the reported error and any
            # reap failure is recorded beside it, never as exit proof.
            self._reap_children(children, results, failures)
            raise CampaignOutcomeUnknown(f"child spawn failed: {type(spawn_error).__name__}: {spawn_error}") from spawn_error
        last_status_ns = self._now_ns()
        owner_state: str = status.state
        try:
            while any(result is None for result in results.values()):
                stop_admission_if_requested()
                for label, child in children.items():
                    if results[label] is not None:
                        continue
                    try:
                        result = child.poll(timeout=0.2)
                    except (TimeoutError, ValueError) as exc:
                        failures[label] = f"{type(exc).__name__}: {exc}"
                        results[label] = OwnerCommandResult(-1, *child.captured_output)
                        continue
                    if result is not None:
                        results[label] = result
                        self._summary.stage(f"{label}_exit", exit_code=result.exit_code, retention=child.retention_report())
                        if result.exit_code != 0:
                            failures[label] = f"exit {result.exit_code}"
                try:
                    launcher_result = self._launcher.poll(timeout=0)
                except (TimeoutError, ValueError) as exc:
                    # The local transport hit its own bound and was aborted by BoundedOwnerCommand; the remote
                    # owner outcome is unknown until the exit record is read back.
                    launcher_result = OwnerCommandResult(-1, *self._launcher.captured_output)
                    failures.setdefault("launcher", f"launcher transport {type(exc).__name__}: {exc}")
                if launcher_result is not None and self._launcher_result is None:
                    self._launcher_result = launcher_result
                    failures.setdefault("launcher", f"launcher exited {launcher_result.exit_code} while children were running")
                if failures and not stop_requested:
                    stop_requested = True
                    self._summary.fail("supervision", "; ".join(f"{k}: {v}" for k, v in failures.items()))
                    try:
                        write_new_exact(stop_file, b"stop\n")
                    except FileExistsError:
                        pass  # the external requester and controller may race to request STOP
                    try:
                        self._controller.request(M6OwnerControl(kind="stop"))
                    except (M6OwnerRejected, M6OwnerProtocolError, EOFError, RuntimeError, OSError) as exc:
                        self._summary.stage("stop_request_failed", message=str(exc)[:500])
                now = self._now_ns()
                if now - last_status_ns >= int(_STATUS_POLL_SECONDS * 1_000_000_000):
                    last_status_ns = now
                    try:
                        owner_state = self._controller.request(M6OwnerControl(kind="status")).status.state
                    except (M6OwnerRejected, M6OwnerProtocolError, EOFError, RuntimeError, OSError) as exc:
                        failures.setdefault("owner_status", str(exc)[:500])
                        owner_state = "unreachable"
                    if owner_state in ("failed", "closed") and results["runner"] is None:
                        failures.setdefault("owner", f"owner state {owner_state} while the runner was still running")
        finally:
            self._reap_children(children, results, failures)
            for label, result in results.items():
                if result is not None:
                    write_new_exact(self._output / f"{label}-stdout.raw", result.stdout)
                    write_new_exact(self._output / f"{label}-stderr.raw", result.stderr)
        runner_receipt = self._read_runner_receipt(receipt, spec)
        verifier_summary = self._read_verifier_summary(verifier_dir / "run-summary.json")
        record = {
            "runner": {"exit_code": results["runner"].exit_code if results["runner"] else None,
                       "receipt_sha256": runner_receipt.get("sha256"), "status": runner_receipt.get("status")},
            "verifier": {"exit_code": results["verifier"].exit_code if results["verifier"] else None,
                         "summary_status": verifier_summary.get("status")},
            "failures": failures, "owner_state": owner_state,
        }
        closure = runner_receipt.get("closure")
        if type(closure) is dict and closure.get("complete") is True and not failures:
            reason: Literal["deadline_drained", "stop_requested", "failed"] = "deadline_drained"
        elif type(closure) is dict and closure.get("complete") is True:
            reason = "failed"
        else:
            self._summary.fail("closure", "runner produced no complete closure; the owner cannot be closed from it")
            return record
        try:
            closed = self._controller.request(M6CloseOwner(
                ownership_receipt_sha256=closure["ownership_closure_sha256"], residual_count=closure["residual_count"],
                children_exited=closure["children_exited"], reason=reason,
            ))
            record["close"] = {"outcome": closed.outcome, "owner_state": closed.status.state}
            self._summary.stage("close", reason=reason, owner_state=closed.status.state)
        except (M6OwnerRejected, M6OwnerProtocolError, EOFError, RuntimeError, OSError) as exc:
            self._summary.fail("close", str(exc))
        record["admitted_count"] = closure.get("admitted_attempt_count") if type(closure) is dict else None
        return record

    def _reap_children(
        self, children: Mapping[str, BoundedOwnerCommand], results: dict[str, OwnerCommandResult | None],
        failures: dict[str, str],
    ) -> None:
        """Reap every owned child that has no result yet; proof of exit comes only from the command itself.

        `BoundedOwnerCommand.abort()` returns normally only after the process group leader was
        killed and reaped (or had already exited); a raising abort leaves that child unproven.
        Every child is attempted even if an earlier abort failed, failures are collected
        separately, an interrupt is re-raised only after all children were attempted, and
        `_children_reaped` becomes true only when every child has real exit proof.
        """
        interrupt: BaseException | None = None
        unproven: list[str] = []
        for label, child in children.items():
            if results[label] is not None:
                continue
            try:
                child.abort()
            except Exception as exc:  # noqa: BLE001 - recorded per child; the original failure stays the reported error
                unproven.append(label)
                self._cleanup_failures[f"{label}_abort"] = f"{type(exc).__name__}: {exc}"[:300]
                self._summary.stage(f"{label}_abort_failed", message=f"{type(exc).__name__}: {exc}"[:300])
                results[label] = OwnerCommandResult(-1, *child.captured_output)
                failures.setdefault(label, "abort failed; exit unproven")
                continue
            except BaseException as exc:
                unproven.append(label)
                self._cleanup_failures[f"{label}_abort"] = f"interrupted: {type(exc).__name__}"
                results[label] = OwnerCommandResult(-1, *child.captured_output)
                failures.setdefault(label, "abort interrupted; exit unproven")
                interrupt = interrupt or exc
                continue
            result = child.poll(timeout=0)
            results[label] = result if isinstance(result, OwnerCommandResult) else OwnerCommandResult(-1, *child.captured_output)
            failures.setdefault(label, "aborted by the composition root")
        self._children_reaped = not unproven and all(result is not None for result in results.values())
        if interrupt is not None:
            raise interrupt

    def _read_runner_receipt(self, path: Path, spec: M6RunSpec) -> dict[str, Any]:
        if not path.is_file():
            self._summary.fail("runner_receipt", "runner receipt missing")
            return {}
        with path.open("rb") as source:
            raw = source.read(_MAX_INPUT_BYTES + 1)
        if len(raw) > _MAX_INPUT_BYTES:
            self._summary.fail("runner_receipt", "runner receipt exceeds its byte bound")
            return {}
        value = strict_json_loads(raw.decode("utf-8"))
        assembly = value.get("m6_assembly") if type(value) is dict else None
        if type(assembly) is not dict or assembly.get("run_id") != spec.run_id or assembly.get("spec_sha256") != spec.canonical_sha256():
            self._summary.fail("runner_receipt", "runner receipt carries no assembly record for this run/spec")
            return {"sha256": sha256_of(raw)}
        return {"sha256": sha256_of(raw), "status": assembly.get("status"), "closure": assembly.get("closure")}

    def _read_verifier_summary(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            self._summary.fail("verifier_summary", "verifier summary missing")
            return {}
        with path.open("rb") as source:
            raw = source.read(_MAX_INPUT_BYTES + 1)
        if len(raw) > _MAX_INPUT_BYTES:
            self._summary.fail("verifier_summary", "verifier summary exceeds its byte bound")
            return {}
        value = strict_json_loads(raw.decode("utf-8"))
        return value if type(value) is dict else {}

    def _bootstrap_closure(self, spec: M6RunSpec) -> dict[str, Any]:
        """Zero-admission closure through the real protocol: runner-role receipts, verifier drain, controller close."""
        assert self._run is not None and self._controller is not None
        run, intent = self._run, self._intent
        runner_role = "e2e_runner" if spec.mode == "e2e_publication" else "service_runner"
        receipts = zero_admission_receipts(run_id=spec.run_id, spec_sha256=spec.canonical_sha256(),
                                           runner_epoch_sha256=run.epoch(runner_role), owner_identity=intent.verifier_identity)
        for kind, receipt in (("resource_audit", receipts.resource_audit), ("admission_reconciliation", receipts.admission_reconciliation),
                              ("ownership_closure", receipts.ownership_closure)):
            write_new_exact(self._output / f"bootstrap-{kind}.json", receipt.canonical_bytes())
        runner = m6_owner_client_factory(run, role=runner_role, continuous_ns=self._now_ns)()  # type: ignore[arg-type]
        try:
            closure = execute_runner_closure(runner, receipts, request_stop=True)
        finally:
            runner.close()
        self._summary.stage("bootstrap_runner_closure", steps=[step.get("step") for step in closure.get("steps", [])])
        started = _utc()
        downstream: dict[str, Any] = {}
        quality = M6VerifierAssembly(run, role="quality_verifier", spool_dir=self._output / "bootstrap-quality-spool",
                                     max_events=16, continuous_ns=self._now_ns)
        quality.start()
        downstream["quality_verifier"] = quality.complete()
        if downstream["quality_verifier"].get("status") != "complete":
            raise CampaignOutcomeUnknown("quality verifier sender did not complete with zero events")
        record: dict[str, Any] = {"steps": closure.get("steps", []), "admitted_count": 0, "downstream": downstream}
        if spec.mode == "e2e_publication":
            public = M6VerifierAssembly(run, role="public_verifier", spool_dir=self._output / "bootstrap-public-spool",
                                        max_events=16, continuous_ns=self._now_ns)
            public.start()
            summary: dict[str, Any] = {"started_utc": started, "finished_utc": _utc(), "attempts": []}
            exit_code = close_verifier_assembly(
                public, output_dir=self._output, summary=summary, producer_kind="public_verifier",
                verifier_identity=intent.verifier_identity, exit_code=0, drained=True, write=write_new_exact,
                terminal=True, downstream=downstream,
            )
            record["drain"] = {"exit_code": exit_code, "status": summary["m6_assembly"].get("status"),
                               "drain_receipt_sha256": summary.get("drain_receipt", {}).get("sha256")}
            if exit_code != 0:
                raise CampaignOutcomeUnknown("public verifier drain did not complete")
        closed = self._controller.request(M6CloseOwner(
            ownership_receipt_sha256=receipts.ownership_closure.canonical_sha256(), residual_count=0, children_exited=True,
            reason="stop_requested",
        ))
        record["close"] = {"outcome": closed.outcome, "owner_state": closed.status.state}
        record["owner_state"] = closed.status.state
        self._summary.stage("close", reason="stop_requested", owner_state=closed.status.state)
        return record

    def _cancel(self, reason: str) -> dict[str, Any] | None:
        windows = self._binding.windows
        try:
            result = self._owned(self._ssh_argv(self._script({
                "Cancel": True, "WorkspaceRoot": str(self._workspace), "ExpectedHostname": windows.hostname,
                "AttemptId": self._attempt_id, "Reason": reason[:200],
            })), label="cancel", timeout_seconds=_CANCEL_TIMEOUT_SECONDS)
            outcome = self._parse_launcher_result(result.stdout, label="cancel")
            self._summary.stage("cancel", ssh_exit=result.exit_code, launcher_exit=outcome.get("exit_code"), cancel=outcome.get("cancel"))
            return outcome
        except CampaignOutcomeUnknown as exc:
            self._summary.fail("cancel", str(exc))
            return None

    def _finish_launcher(self, *, wait_seconds: float) -> None:
        assert self._launcher is not None
        if self._launcher_result is None:
            deadline = self._now_ns() + int(wait_seconds * 1_000_000_000)
            while self._launcher_result is None and self._now_ns() < deadline:
                try:
                    self._launcher_result = self._launcher.poll(timeout=1.0)
                except (TimeoutError, ValueError) as exc:
                    self._summary.fail("launcher", f"{type(exc).__name__}: {exc}")
                    self._launcher_result = OwnerCommandResult(-1, *self._launcher.captured_output)
        if self._launcher_result is None:
            self._cancel("composition root gave up waiting for the owner exit")
            deadline = self._now_ns() + 60_000_000_000
            while self._launcher_result is None and self._now_ns() < deadline:
                try:
                    self._launcher_result = self._launcher.poll(timeout=1.0)
                except (TimeoutError, ValueError) as exc:
                    self._summary.fail("launcher", f"{type(exc).__name__}: {exc}")
                    self._launcher_result = OwnerCommandResult(-1, *self._launcher.captured_output)
        if self._launcher_result is None:
            self._launcher.abort()
            self._launcher_result = OwnerCommandResult(-1, *self._launcher.captured_output)
            self._summary.fail("launcher", "launcher transport aborted locally; the remote outcome is unknown")
        stdout, stderr = self._launcher_result.stdout, self._launcher_result.stderr
        write_new_exact(self._output / "launcher-stdout.raw", stdout)
        write_new_exact(self._output / "launcher-stderr.raw", stderr)
        write_new_exact(self._output / "launcher-execution.json", canonical_bytes({
            "exit_code": self._launcher_result.exit_code, "retention": self._launcher.retention_report(), "finished_utc": _utc(),
        }) + b"\n")
        transport_record: dict[str, Any] = {"transport_exit_code": self._launcher_result.exit_code}
        exits = launcher_lines(stdout, "M6-EXIT")
        results = launcher_lines(stdout, "M6-RESULT")
        transport_record["exit_line_seen"] = len(exits) == 1
        transport_record["result_line_seen"] = len(results) == 1
        if len(exits) == 1:
            printed = strict_json_loads(exits[0])
            if type(printed) is dict:
                self._summary.values["printed_exit_record"] = printed
        if len(results) == 1:
            value = strict_json_loads(results[0])
            if type(value) is dict:
                transport_record["launcher_exit_code"] = value.get("exit_code")
                transport_record["launcher_first_error"] = value.get("first_error")
        self._summary.values["launcher"] = transport_record

    def _fetch_owner_identity(self, *, start: Mapping[str, Any]) -> None:
        """Retain the existing exact native pair and bind both boot encodings to this anchor."""
        if self._run is None:
            raise CampaignOutcomeUnknown("owner identity requires the bound original anchor")
        run_store = self._private_root / "runs" / hashlib.sha256(self._intent.run.run_id.encode("utf-8")).hexdigest()
        result = self._owned(self._ssh_argv(owner_identity_read_script(run_store)),
                             label="owner-identity-fetch", timeout_seconds=60, maximum_bytes=262144)
        lines = launcher_lines(result.stdout, "M6-IDENTITY")
        if result.exit_code != 0 or len(lines) != 1:
            raise CampaignOutcomeUnknown("bounded owner identity read failed or did not return exactly one pair")
        value = load_closed_object(lines[0].encode("utf-8"), label="owner identity transport", maximum_bytes=131072)
        require_fields(value, {"metadata_name", "metadata_base64", "body_base64"}, label="owner identity transport")
        name = value["metadata_name"]
        if (type(name) is not str or re.fullmatch(r"diagnostic-[0-9a-f]{32}\.json", name) is None
                or type(value["metadata_base64"]) is not str or type(value["body_base64"]) is not str):
            raise ValueError("owner identity transport fields differ")
        metadata = base64.b64decode(value["metadata_base64"], validate=True)
        body = base64.b64decode(value["body_base64"], validate=True)
        # Preserve original bounded bytes even when their binding later fails.
        if len(metadata) > 1024 or len(body) > 65536:
            raise ValueError("owner identity transport exceeds original store bounds")
        local_dir = self._output / "native"
        if local_dir.is_symlink():
            raise ValueError("native evidence directory cannot be a symlink")
        local_dir.mkdir(mode=0o700, exist_ok=True)
        write_new_exact(local_dir / name, metadata)
        write_new_exact(local_dir / (name[:-5] + ".bin"), body)
        alias = bind_physical_owner_boot(
            metadata_raw=metadata, body_raw=body, anchor=self._run.anchor,
            pid=start.get("pid"), creation_filetime_100ns=start.get("creation_filetime_100ns"),
        )
        self._summary.stage("native_identity_fetched", metadata_name=name, body_sha256=sha256_of(body),
                            resident_boot_identity_sha256=alias)

    def _fetch_native_evidence(self) -> None:
        """Read-only sftp copy of the owner's private run store (journal and sidecars) for the summary; never writes remotely."""
        run_store = self._private_root / "runs" / hashlib.sha256(self._intent.run.run_id.encode("utf-8")).hexdigest()
        local_dir = self._output / "native"
        if local_dir.is_symlink():
            raise ValueError("native evidence directory cannot be a symlink")
        local_dir.mkdir(mode=0o700, exist_ok=True)
        names = ("events.jsonl", "spec.json", "anchor.json", "admission-closed.json", "resources-closed.json", "exit-observation.json")
        batch = self._output / "native-fetch.batch"
        write_new_exact(batch, ("\n".join(f"-get {_quoted(_sftp_path(run_store / name))} {_quoted(str(local_dir / name))}" for name in names) + "\n").encode("utf-8"))
        try:
            result = self._owned(self._sftp_argv(batch), label="native-fetch", timeout_seconds=_FETCH_TIMEOUT_SECONDS, maximum_bytes=_LAUNCHER_CAPTURE_BYTES)
        except CampaignOutcomeUnknown as exc:
            self._summary.stage("native_fetch_failed", message=str(exc)[:300])
            return
        present = sorted(name for name in names if (local_dir / name).is_file())
        hashes = {}
        for name in present:
            with (local_dir / name).open("rb") as source:
                hashes[name] = "sha256:" + hashlib.file_digest(source, "sha256").hexdigest()
        self._summary.values["native_evidence"] = {
            "sftp_exit": result.exit_code, "present": present, "sha256": hashes,
        }
        self._summary.stage("native_fetched", present=present, sftp_exit=result.exit_code)

    def _verify_external_exit(self) -> bool:
        """Fetch the launcher's exit record over sftp and compare it with the transport's view; the file is the proof."""
        batch = self._output / "fetch.batch"
        local_dir = self._output / "launcher-records"
        local_dir.mkdir(mode=0o700)
        lines = []
        for name in ("process-start.json", "ready.json", "process-exit.json"):
            lines.append(f"-get {_quoted(_sftp_path(self._attempt_dir / name))} {_quoted(str(local_dir / name))}")
        write_new_exact(batch, ("\n".join(lines) + "\n").encode("utf-8"))
        try:
            result = self._owned(self._sftp_argv(batch), label="fetch", timeout_seconds=_FETCH_TIMEOUT_SECONDS)
        except CampaignOutcomeUnknown as exc:
            self._summary.fail("fetch", str(exc))
            return False
        exit_path, start_path, ready_path = local_dir / "process-exit.json", local_dir / "process-start.json", local_dir / "ready.json"
        if result.exit_code != 0 or not exit_path.is_file() or not start_path.is_file():
            self._summary.fail("fetch", f"sftp exit {result.exit_code}; exit record {'present' if exit_path.is_file() else 'absent'}, "
                                        f"start record {'present' if start_path.is_file() else 'absent'}")
            return False
        try:
            originals = []
            for path in (exit_path, start_path):
                if path.is_symlink():
                    raise ValueError("launcher record cannot be a symlink")
                with path.open("rb") as source:
                    raw = source.read(_LAUNCHER_CAPTURE_BYTES + 1)
                if len(raw) > _LAUNCHER_CAPTURE_BYTES:
                    raise ValueError("launcher record exceeds its byte bound")
                originals.append(strict_json_loads(raw.decode("utf-8")))
            record, start = originals
        except ValueError as exc:
            self._summary.fail("external_exit", f"launcher record unreadable: {exc}")
            return False
        if type(record) is not dict or type(start) is not dict:
            self._summary.fail("external_exit", "launcher records are not objects")
            return False
        verified = self._external_exit_verified(record=record, start=start, ready_path=ready_path)
        if verified:
            try:
                self._fetch_owner_identity(start=start)
            except (CampaignOutcomeUnknown, ValueError, OSError) as exc:
                # An identity-read failure blocks the campaign but is NOT evidence
                # that an already verified native process failed to exit.
                self._summary.fail("native_identity_fetch", str(exc))
        return verified

    def _external_exit_verified(self, *, record: dict[str, Any], start: dict[str, Any], ready_path: Path) -> bool:
        """Typed, exact cross-check: start record, READY, expected identities, printed exit line and fetched exit record."""
        ready_raw = None
        if ready_path.is_file() and not ready_path.is_symlink():
            with ready_path.open("rb") as source:
                ready_raw = source.read(65537)
        observed = self._summary.values.get("ready_line")
        problems = list(external_exit_problems(
            record=record, start=start,
            expected_start=self._expected_external_start(self._summary.values.get("deployment_sha256")),
            ready_raw=ready_raw,
            observed_ready_raw=observed.encode("utf-8") if type(observed) is str else None,
            expected_anchor=None if self._run is None else self._run.anchor,
            printed_exit=self._summary.values.get("printed_exit_record"),
        ))
        exit_code = record.get("exit_code")
        external = {
            "exit_code": exit_code, "forced_termination": record.get("forced_termination"), "cancel": record.get("cancel"),
            "process_handle_signaled": record.get("process_handle_signaled"), "ready_received": record.get("ready_received"),
            "parent_failure": record.get("parent_failure"), "pid": record.get("pid"),
            "creation_filetime_100ns": record.get("creation_filetime_100ns"), "problems": problems,
        }
        self._summary.values.setdefault("launcher", {})["external_exit"] = external
        if problems:
            self._summary.fail("external_exit", "; ".join(problems))
            return False
        return True

    # --- entry ---------------------------------------------------------------------

    def _measurement_clock(self) -> tuple[bytes, str, int]:
        """This process's kernel identity, its monotonic clock domain and one monotonic instant.

        The campaign's local measurement window lives in the same Mac monotonic
        domain the dedicated observer plans in; the identity is read again at
        the end so a changed domain can never be spliced into one window.
        """
        identity_bytes = MacObserverIdentityReader().observe()
        domain = check_mac_observer_identity(identity_bytes).clock_domain_identity_sha256
        return identity_bytes, domain, time.monotonic_ns()

    def _telemetry_window(self, entry_identity: bytes, entry_domain: str, entry_monotonic_ns: int) -> dict[str, Any]:
        """The private local measurement window a v4 telemetry reader scores resources in."""
        try:
            finish_identity, finish_domain, finish_monotonic_ns = self._measurement_clock()
        except (OSError, ValueError) as exc:
            return {"contract_version": LOCAL_MEASUREMENT_WINDOW_CONTRACT, "clock_domain_identity_sha256": entry_domain,
                    "started_monotonic_ns": entry_monotonic_ns, "finished_monotonic_ns": None,
                    "problem": f"finish clock identity unavailable: {type(exc).__name__}: {exc}"[:300]}
        window: dict[str, Any] = {
            "contract_version": LOCAL_MEASUREMENT_WINDOW_CONTRACT, "clock_domain_identity_sha256": entry_domain,
            "started_monotonic_ns": entry_monotonic_ns, "finished_monotonic_ns": finish_monotonic_ns,
        }
        if finish_identity != entry_identity or finish_domain != entry_domain or finish_monotonic_ns <= entry_monotonic_ns:
            window["finished_monotonic_ns"] = None
            window["problem"] = "measurement clock identity changed during the campaign"
        return window

    def execute(self) -> dict[str, Any]:
        intent, binding, summary = self._intent, self._binding, self._summary
        self._output.mkdir(mode=0o700)
        (self._output / "private").mkdir(mode=0o700)
        started = _utc()
        entry_identity, entry_domain, entry_monotonic_ns = self._measurement_clock()
        # The intent and the evaluation plan are frozen into the run output before Prepare, i.e. before
        # any admission can exist; the summary reads these exact bytes back against the recorded hashes.
        write_new_exact(self._output / "campaign-intent.json", self._inputs.intent_raw)
        write_new_exact(self._output / "evaluation-plan.json", self._inputs.evaluation_plan_raw)
        write_new_exact(self._output / "campaign-inputs.json", canonical_bytes({
            "contract_version": "m6.campaign-inputs.v2", "mode": self._mode, "attempt_id": self._attempt_id,
            "intent_sha256": self._inputs.intent_sha256, "intent_contract_version": intent.contract_version,
            "binding_sha256": intent.binding_sha256,
            "release_manifest_sha256": intent.release_manifest_sha256, "worker_env_sha256": self._inputs.worker_env_sha256,
            "evaluation_plan_sha256": intent.evaluation_plan_sha256, "manifest_sha256": intent.run.manifest_sha256,
            "scope_sha256": intent.run.scope_sha256, "quality_plan_sha256": intent.run.quality_plan_sha256,
            "manifest_path": str(self._inputs.manifest_path), "scope_path": str(self._inputs.scope_path),
            "quality_plan_path": str(self._inputs.quality_plan_path),
            "workspace": str(self._workspace), "started_utc": started,
        }) + b"\n")
        lock: ExclusivityLock | None = None
        closure: dict[str, Any] = {}
        spec: M6RunSpec | None = None
        external_verified = False
        interrupt: BaseException | None = None
        try:
            lock = acquire_exclusivity(binding.mac_exclusive_lock_path, holder={
                "pid": os.getpid(), "started_utc": started, "purpose": f"m6-campaign-{self._mode}", "run_id": intent.run.run_id,
            })
            roles = generate_roles(intent.run.mode)
            write_run_directory(self._output / "run", roles=roles)
            deployment_raw = canonical_bytes(deployment_document(
                intent=intent, binding=binding, run_root=self._private_root / "runs", roles=roles,
            ))
            deployment_sha256 = sha256_of(deployment_raw)
            summary.values["deployment_sha256"] = deployment_sha256
            summary.stage("inputs_pinned", deployment_sha256=deployment_sha256)
            self._prepare()
            summary.stage("prepared", workspace=str(self._workspace))
            staged = self._stage_deployment(deployment_raw)
            summary.stage("staged")
            self._start_launcher(staged_name=staged, deployment_sha256=deployment_sha256)
            ready = self._await_ready()
            spec, status, sent_ns = self._bind_and_open(ready)
            summary.values["spec_sha256"] = spec.canonical_sha256()
            summary.values["anchor_sha256"] = ready.anchor_sha256
            summary.values["owner_epoch_sha256"] = ready.owner_epoch_sha256
            summary.values["spec_hash_equal"] = status.spec_sha256 == spec.canonical_sha256()
            if self._mode == "run":
                closure = self._supervise_children(spec, status, sent_ns)
            else:
                closure = self._bootstrap_closure(spec)
        except (CampaignInputError, CampaignIdentityError, CampaignOutcomeUnknown, M6RunnerClosureFailed,
                M6OwnerRejected, M6OwnerProtocolError, EOFError, RuntimeError, ValueError, OSError) as exc:
            summary.fail("campaign", f"{type(exc).__name__}: {exc}")
        except BaseException as exc:
            # An interrupt still closes ownership below and is re-raised after the summary is persisted.
            interrupt = exc
            summary.fail("campaign", f"interrupted: {type(exc).__name__}: {exc}")
        finally:
            external = {"verified": False}

            def close_controller() -> None:
                if self._controller is not None:
                    self._controller.close()

            def cancel_if_needed() -> None:
                if self._launcher is not None and summary.first_error is not None and self._launcher_result is None:
                    self._cancel("campaign failed: " + summary.first_error["message"][:120])

            def finish_launcher() -> None:
                if self._launcher is not None:
                    # Wait for the launcher's natural end until its own absolute deadline (which already holds the
                    # post-close tail); a transport not spawned here only gets that post-close tail.
                    wait = (finish_wait_seconds(launcher_deadline_ns=self._launcher_deadline_ns, now_ns=self._now_ns())
                            if self._launcher_deadline_ns is not None else self._transport_budget().post_close_seconds)
                    self._finish_launcher(wait_seconds=wait)

            def read_external_proof() -> None:
                if self._launcher is not None:
                    external["verified"] = bool(self._verify_external_exit())

            def fetch_native() -> None:
                if self._launcher is not None and self._mode == "run":
                    self._fetch_native_evidence()

            def release_lock() -> None:
                if lock is not None:
                    lock.release()

            # Each cleanup step is attempted on its own and stays visible; a failure never skips the later steps
            # and never becomes success. Interrupts are held until every step was attempted.
            for name, step in (("controller_close", close_controller), ("cancel", cancel_if_needed),
                               ("launcher_finish", finish_launcher), ("external_exit_readback", read_external_proof),
                               ("native_evidence_fetch", fetch_native), ("lock_release", release_lock)):
                try:
                    step()
                except Exception as exc:  # noqa: BLE001 - recorded as a named cleanup failure
                    self._cleanup_failures[name] = f"{type(exc).__name__}: {exc}"[:300]
                    summary.stage(f"{name}_failed", message=f"{type(exc).__name__}: {exc}"[:300])
                except BaseException as exc:
                    self._cleanup_failures[name] = f"interrupted: {type(exc).__name__}"
                    summary.stage(f"{name}_failed", message=f"interrupted: {type(exc).__name__}")
                    interrupt = interrupt or exc
            external_verified = external["verified"]
        local_children_reaped = self._children_reaped
        cleanup_failures = dict(self._cleanup_failures)
        complete = (summary.first_error is None and not cleanup_failures and external_verified and local_children_reaped
                    and bool(closure) and closure.get("close", {}).get("outcome") == "ok")
        if summary.first_error is None and cleanup_failures:
            name, message = next(iter(cleanup_failures.items()))
            summary.first_error = {"stage": name, "message": message}
        summary.status = "complete" if complete else ("failed" if summary.first_error is not None else "unknown")
        if summary.status != "complete" and summary.first_error is None:
            summary.first_error = {"stage": "verification", "message": "outcome could not be verified end to end"}
        document = {
            "contract_version": CAMPAIGN_SUMMARY_CONTRACT, "mode": self._mode, "status": summary.status,
            "run_id": intent.run.run_id, "attempt_id": self._attempt_id, "intent_sha256": self._inputs.intent_sha256,
            "spec_sha256": summary.values.get("spec_sha256"), "anchor_sha256": summary.values.get("anchor_sha256"),
            "owner_epoch_sha256": summary.values.get("owner_epoch_sha256"), "spec_hash_equal": summary.values.get("spec_hash_equal"),
            "bind": summary.values.get("bind"), "budgets": summary.values.get("budgets"),
            "children": {"runner": closure.get("runner"), "verifier": closure.get("verifier"), "launcher": summary.values.get("launcher")},
            "closure": {"steps": closure.get("steps"), "close": closure.get("close"), "owner_state": closure.get("owner_state")},
            "admitted_count": closure.get("admitted_count"),
            "database_access": "none" if self._mode == "bootstrap-check" else "worker_env",
            "hidden_setup": False,
            "owner_external_exit_verified": external_verified, "local_children_reaped": local_children_reaped,
            "cleanup_failures": cleanup_failures, "native_evidence": summary.values.get("native_evidence"),
            "evaluation_plan_sha256": intent.evaluation_plan_sha256,
            "first_error": summary.first_error, "stages": summary.stages,
            "files": {"output": str(self._output), "run_dir": str(self._output / "run")},
            "started_utc": started, "finished_utc": _utc(),
            "telemetry_window": self._telemetry_window(entry_identity, entry_domain, entry_monotonic_ns),
        }
        try:
            write_new_exact(self._output / "campaign-summary.json", canonical_bytes(document) + b"\n")
        finally:
            if interrupt is not None:
                raise interrupt
        return document


__all__ = [
    "derive_runner_lease_policy",
    "CAMPAIGN_SUMMARY_CONTRACT", "LOCAL_MEASUREMENT_WINDOW_CONTRACT", "OWNER_DEPLOYMENT_CONTRACT", "CampaignIdentityError", "CampaignInputError",
    "CampaignInputs", "CampaignMode", "CampaignOutcomeUnknown", "M6CampaignAssembly", "ReadyObservation",
    "deployment_document", "external_exit_problems", "generate_roles", "launcher_lines", "load_campaign_inputs", "parse_ready_line",
    "remaining_budget_seconds", "sourced_child_argv", "write_run_directory", "write_run_transport", "zero_admission_receipts",
]
