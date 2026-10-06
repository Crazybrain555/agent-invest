"""Mac side of the same-root registry maintenance: one bounded phase per call.

Every phase reuses the official installation transport: the deployment
exclusivity lock, the worker/GC not-loaded proof (all but ``status``), staging
of the verified package with a closed v3 installation binding into new Windows
directories, one bounded SSH run of the pinned Windows owner and a judgement
made only from the fetched records. ``prestop`` additionally needs a fresh
zero-member legacy-scope inventory and the live closed idle health; ``apply``
needs an operator approval that binds one observed plan by digest, written only
after the plan was recomputed here with the packaged task protocol. A phase
never installs, never opens producers and never restores containers by itself:
write permission stays closed in every result.
"""

from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
import os
from pathlib import Path, PureWindowsPath
import sys
from types import ModuleType
from typing import Any

from disclosure_anchor.adapters.runtime import mineru_release_install as installation
from disclosure_anchor.adapters.runtime.exact_file_write import write_new_exact
from disclosure_anchor.adapters.runtime.mineru_release_package import (
    ReleaseIdentityError,
    ReleaseInputError,
    VerifyReport,
    write_new_json,
)
from disclosure_anchor.adapters.runtime.mineru_release_private_binding import ReleasePrivateBinding
from disclosure_anchor.adapters.runtime.resident_owner_control import owner_ssh_command, pinned_windows_script
from disclosure_anchor.application.contracts.closed_document import canonical_bytes, sha256_of
from disclosure_anchor.application.contracts.mineru_same_root import (
    SAME_ROOT_OPERATION_KIND,
    SAME_ROOT_PHASES,
    SameRootApproval,
    decode_same_root_approval,
    decode_same_root_plan,
    encode_same_root_approval,
    require_root_tuple,
    require_same_root_operation_id,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    LegacyScopeInventory,
    decode_legacy_scope_inventory,
)


SAME_ROOT_BINDING_CONTRACT = "m6.installation-binding.v3"
SAME_ROOT_OPERATION_CONTRACT = "m6.installation-operation.v3"
SAME_ROOT_RESULT_CONTRACT = "m6.same-root-phase-result.v1"
SAME_ROOT_EXCLUSIVITY_CONTRACT = "m6.same-root-mac-exclusivity.v1"
SAME_ROOT_VERIFICATION_CONTRACT = "m6.same-root-plan-verification.v1"
APPROVAL_STAGED_NAME = "same-root-approval.json"
# The inventory is captured after the worker was booted out; a stale capture
# cannot speak for duties created since.
LEGACY_SCOPE_MAX_AGE_SECONDS = 900
_NEXT_REQUIRED = {
    "prestop": ["docker_storage_maintenance", "observe"],
    "apply": ["restore"],
    "restore": ["close"],
    "close": ["api_compatibility_install"],
    "status": [],
}
_STAGE_TIMEOUT_SECONDS = 600
_FETCH_TIMEOUT_SECONDS = 300


def _operation(value: object) -> str:
    try:
        return require_same_root_operation_id(value)
    except ValueError as exc:
        raise ReleaseInputError(str(exc)) from exc


def require_zero_duty_inventory(raw: bytes, *, now: datetime) -> LegacyScopeInventory:
    """A fresh legacy-scope capture with no unresolved business responsibility."""

    try:
        inventory = decode_legacy_scope_inventory(raw)
    except ValueError as exc:
        raise ReleaseInputError(f"legacy scope inventory is invalid: {exc}") from exc
    if inventory.members:
        raise ReleaseIdentityError(f"legacy scope still holds {len(inventory.members)} unresolved responsibilities")
    captured = datetime.fromisoformat(inventory.captured_at_utc.replace("Z", "+00:00"))
    age = (now - captured).total_seconds()
    if not -60 <= age <= LEGACY_SCOPE_MAX_AGE_SECONDS:
        raise ReleaseIdentityError(
            f"legacy scope inventory is not a fresh zero-duty capture (age {age:.0f}s, limit {LEGACY_SCOPE_MAX_AGE_SECONDS}s)"
        )
    return inventory


def same_root_binding_document(
    binding: ReleasePrivateBinding, *, api_device_profile: str, phase: str, operation_id: str,
    exclusivity_sha256: str, approval_sha256: str | None = None, legacy_scope_inventory_sha256: str | None = None,
) -> dict[str, Any]:
    if phase not in SAME_ROOT_PHASES:
        raise ReleaseInputError("same-root phase is unsupported")
    if (phase == "apply") != (approval_sha256 is not None):
        raise ReleaseInputError("an approval identity belongs to the apply phase only")
    if (phase == "prestop") != (legacy_scope_inventory_sha256 is not None):
        raise ReleaseInputError("a legacy-scope inventory identity belongs to the prestop phase only")
    windows = binding.windows
    if windows.expected_active_compose_sha256 is None or windows.expected_previous_capacity_sha256 is None:
        raise ReleaseInputError("same-root maintenance requires the active compose and explicit capacity identities")
    return {
        "contract_version": SAME_ROOT_BINDING_CONTRACT,
        "operation_kind": SAME_ROOT_OPERATION_KIND,
        "phase": phase,
        "operation_id": _operation(operation_id),
        "expected_hostname": windows.hostname,
        "api_device_profile": api_device_profile,
        "expected_active_compose_sha256": windows.expected_active_compose_sha256,
        "expected_previous_capacity_sha256": windows.expected_previous_capacity_sha256,
        "job_lifetime_milliseconds": windows.job_lifetime_milliseconds,
        "job_cleanup_milliseconds": windows.job_cleanup_milliseconds,
        "mac_exclusivity_receipt_sha256": exclusivity_sha256,
        "approval_sha256": approval_sha256,
        "legacy_scope_inventory_sha256": legacy_scope_inventory_sha256,
    }


def _read_input(path: Path, *, label: str, maximum: int) -> bytes:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ReleaseInputError(f"{label} must be an existing absolute regular file")
    raw = path.read_bytes()
    if not raw or len(raw) > maximum:
        raise ReleaseInputError(f"{label} bytes are outside the closed envelope")
    return raw


def _judge(
    operation: dict[str, Any] | None, *, phase: str, operation_id: str, wrapper_exit: int | None,
) -> tuple[str, str | None, str]:
    """(status, failure, daemon side) from the fetched Windows result; never from an exit alone."""

    if operation is None:
        return "unknown", "Windows operation result was not fetched; remote outcome unknown", "unknown"
    if (
        operation.get("contract_version") != SAME_ROOT_OPERATION_CONTRACT
        or operation.get("operation_kind") != SAME_ROOT_OPERATION_KIND
        or operation.get("maintenance_phase") != phase or operation.get("operation_id") != operation_id
    ):
        return "unknown", "Windows result does not bind the requested same-root phase", "unknown"
    daemon_side = str(operation.get("daemon_side_outcome", "unknown"))
    remote_status = operation.get("status")
    if remote_status == "pass" and wrapper_exit == 0 and daemon_side == "verified":
        return "pass", None, daemon_side
    failure = str(operation.get("first_error") or f"remote status {remote_status}")
    if remote_status == "unknown" or daemon_side == "unknown" or wrapper_exit is None:
        return "unknown", failure, daemon_side
    return "failed", failure, daemon_side


def run_same_root_phase(
    *, report: VerifyReport, package: Path, binding: ReleasePrivateBinding, output: Path, phase: str,
    operation_id: str, approval_path: Path | None = None, legacy_scope_inventory_path: Path | None = None,
    resume: bool = False, now: datetime | None = None,
) -> dict[str, Any]:
    """Run one phase. ``resume`` continues an interrupted prestop of ``operation_id``.

    A resumed prestop skips only the Mac's early health read (the API may
    already be stopped); the Windows owner still applies its live gate whenever
    no operation is pending, and the zero-duty inventory is always required.
    """

    if phase not in SAME_ROOT_PHASES:
        raise ReleaseInputError("same-root phase is unsupported")
    operation_id = _operation(operation_id)
    if (phase == "apply") != (approval_path is not None) or (phase == "prestop") != (legacy_scope_inventory_path is not None):
        raise ReleaseInputError("an approval belongs to apply and a legacy-scope inventory to prestop only")
    if resume and phase != "prestop":
        raise ReleaseInputError("only an interrupted prestop is resumed; every other phase is itself resumable")
    if not output.is_absolute() or output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ReleaseInputError("same-root output must be a new absolute directory under an existing parent")
    if not report.passed:
        raise ReleaseIdentityError("release package did not verify; refusing the same-root phase")
    approval_raw = inventory_raw = None
    if approval_path is not None:
        approval_raw = _read_input(approval_path, label="same-root approval", maximum=65536)
        try:
            approval = decode_same_root_approval(approval_raw)
        except ValueError as exc:
            raise ReleaseInputError(f"same-root approval is invalid: {exc}") from exc
        if approval.operation_id != operation_id:
            raise ReleaseIdentityError("same-root approval names another operation")
    if legacy_scope_inventory_path is not None:
        inventory_raw = _read_input(legacy_scope_inventory_path, label="legacy scope inventory", maximum=16 * 1024 * 1024)
        require_zero_duty_inventory(inventory_raw, now=now or datetime.now(UTC))
    manifest = report.manifest
    output.mkdir(mode=0o700)
    started = datetime.now(UTC).isoformat(timespec="seconds")
    lock = installation.acquire_exclusivity(binding.mac_exclusive_lock_path, holder={
        "pid": os.getpid(), "started_utc": started, "release_manifest_sha256": manifest.sha256,
        "purpose": f"same-root-{phase}", "operation_id": operation_id,
    })
    status, failure, daemon_side = "failed", None, "unknown"
    remote: dict[str, Any] = {}
    operation: dict[str, Any] | None = None
    try:
        installation.assert_known_hosts_pins_address(binding)
        launchd = None if phase == "status" else installation.assert_launchd_jobs_unloaded(binding.launchd_labels)
        previous = installation.previous_capacity_for(report, binding)
        if previous is None:
            raise ReleaseInputError("same-root maintenance requires the previous explicit capacity identity and bytes")
        deployment = report.inputs.deployment_profile
        health_sha = idle_proof = inventory_sha = approval_sha = None
        if inventory_raw is not None:
            if not resume:
                _, health_raw, idle_proof = installation.read_idle_health(
                    binding.api_url, expected_capacity=previous,
                    task_retention_seconds=deployment.api_task_retention_seconds,
                    cleanup_interval_seconds=deployment.api_task_cleanup_interval_seconds,
                )
                write_new_exact(output / "health-before.json", health_raw)
                health_sha = sha256_of(health_raw)
            write_new_exact(output / "legacy-scope-inventory.json", inventory_raw)
            inventory_sha = sha256_of(inventory_raw)
        if approval_raw is not None:
            write_new_exact(output / APPROVAL_STAGED_NAME, approval_raw)
            approval_sha = sha256_of(approval_raw)
        exclusivity_raw = write_new_json(output / "mac-exclusivity.json", {
            "contract_version": SAME_ROOT_EXCLUSIVITY_CONTRACT, "lock_path": str(binding.mac_exclusive_lock_path),
            "pid": os.getpid(), "started_utc": started, "launchd": launchd, "operation_kind": SAME_ROOT_OPERATION_KIND,
            "phase": phase, "operation_id": operation_id, "resume": resume, "release_manifest_sha256": manifest.sha256,
            "api_health_before_sha256": health_sha, "idle_proof": idle_proof,
            "legacy_scope_inventory_sha256": inventory_sha, "approval_sha256": approval_sha,
        })
        stem = f"mineru-same-root-{operation_id[3:15]}-{phase}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        remote_root = binding.windows.workspace_root / f"{stem}-release"
        remote_binding_dir = binding.windows.workspace_root / f"{stem}-binding"
        remote_operation = binding.windows.workspace_root / stem
        remote = {"release_root": str(remote_root), "binding_dir": str(remote_binding_dir), "operation_dir": str(remote_operation)}
        document = same_root_binding_document(
            binding, api_device_profile=deployment.api_device_profile, phase=phase, operation_id=operation_id,
            exclusivity_sha256=sha256_of(exclusivity_raw), approval_sha256=approval_sha,
            legacy_scope_inventory_sha256=inventory_sha,
        )
        binding_local = output / "installation-binding.json"
        write_new_exact(binding_local, canonical_bytes(document) + b"\n")
        batch_text = installation.stage_batch(report, package, remote_root, binding_local, remote_binding_dir)
        if approval_raw is not None:
            batch_text += "put {} {}\n".format(
                installation._quoted(str(output / APPROVAL_STAGED_NAME)),
                installation._quoted(installation._sftp_path(remote_binding_dir / APPROVAL_STAGED_NAME)),
            )
        batch = output / "stage.batch"
        write_new_exact(batch, batch_text.encode("utf-8"))
        staged = installation._run_owned(
            installation._sftp_argv(binding, batch), timeout_seconds=_STAGE_TIMEOUT_SECONDS, output=output, label="stage",
        )
        if staged.exit_code != 0:
            raise ReleaseIdentityError(f"staging failed with sftp exit {staged.exit_code}; the remote directories must not be reused")
        script = pinned_windows_script(
            script_path=str(remote_root / "windows" / "run_mineru_installation.ps1"),
            expected_sha256=manifest.installation["wrapper_sha256"],
            arguments={
                "ReleaseRoot": str(remote_root), "ExpectedManifestSha256": manifest.sha256,
                "OperationDirectory": str(remote_operation),
                "InstallationBinding": str(remote_binding_dir / "installation-binding.json"),
            },
        )
        argv = owner_ssh_command(
            executable=binding.ssh_executable, executable_sha256=binding.ssh_executable_sha256, ssh=binding.ssh, script=script,
        )
        timeout = min(7200.0, binding.windows.job_lifetime_milliseconds / 1000 + 300.0)
        wrapper_exit: int | None
        try:
            wrapper_exit = installation._run_owned(
                argv, timeout_seconds=timeout, output=output, label="wrapper", maximum_bytes=1048576,
            ).exit_code
        except installation.InstallationOutcomeUnknown as exc:
            wrapper_exit, failure = None, str(exc)
        operation = _fetch_operation(binding, remote_operation, output)
        status, judged_failure, daemon_side = _judge(operation, phase=phase, operation_id=operation_id, wrapper_exit=wrapper_exit)
        failure = failure or judged_failure
        if status == "pass" and phase == "close":
            # The proxy is open again: the closed idle proof must also hold from the Mac.
            status = "failed"
            _, health_after, _ = installation.read_idle_health(
                binding.api_url, expected_capacity=previous,
                task_retention_seconds=deployment.api_task_retention_seconds,
                cleanup_interval_seconds=deployment.api_task_cleanup_interval_seconds,
            )
            write_new_exact(output / "health-after.json", health_after)
            status = "pass"
    except (ReleaseIdentityError, ReleaseInputError, installation.InstallationOutcomeUnknown, OSError, ValueError) as exc:
        failure = failure or str(exc)
        if status == "failed" and isinstance(exc, installation.InstallationOutcomeUnknown):
            status = "unknown"
    finally:
        lock.release()
    outcome = None if operation is None else operation.get("same_root_outcome")
    if status != "pass":
        next_required = ["same_root_status", "read_only_recovery_judgement"]
    elif phase == "observe":
        next_required = ["approve", "apply"] if outcome == "reattest" else ["restore"]
    else:
        next_required = list(_NEXT_REQUIRED[phase])
    summary = {
        "contract_version": SAME_ROOT_RESULT_CONTRACT,
        "status": status,
        "first_error": failure,
        "daemon_side_outcome": daemon_side,
        "write_permission": "closed",
        "operation_kind": SAME_ROOT_OPERATION_KIND,
        "phase": phase,
        "operation_id": operation_id,
        "same_root_outcome": outcome,
        "pending_present": None if operation is None else operation.get("pending_present"),
        "next_required": next_required,
        "release_manifest_sha256": manifest.sha256,
        "remote": remote,
        "started_utc": started,
        "finished_utc": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    write_new_json(output / "same-root-result.json", summary)
    return summary


def _fetch_operation(binding: ReleasePrivateBinding, remote_operation: PureWindowsPath, output: Path) -> dict[str, Any] | None:
    fetch_batch = output / "fetch.batch"
    write_new_exact(
        fetch_batch,
        f'get -r "{installation._sftp_path(remote_operation)}" "{output / "windows-operation"}"\n'.encode("utf-8"),
    )
    try:
        fetched = installation._run_owned(
            installation._sftp_argv(binding, fetch_batch), timeout_seconds=_FETCH_TIMEOUT_SECONDS, output=output,
            label="fetch", maximum_bytes=1048576,
        )
    except installation.InstallationOutcomeUnknown:
        return None
    result_path = output / "windows-operation" / "operation-result.json"
    if fetched.exit_code != 0 or not result_path.is_file():
        return None
    try:
        value = strict_json_loads(result_path.read_bytes())
    except ValueError:
        return None  # an unreadable record is an unknown outcome, never a failure to retry over
    return value if type(value) is dict else None


# ---- plan verification and approval (offline) ---------------------------------------------------------------

def load_packaged_protocol(package: Path) -> ModuleType:
    """The exact task-protocol bytes the Windows helper runs, from the verified package."""

    path = package / "api-context" / "agent_task_protocol_v2.py"
    name = "_same_root_protocol_" + sha256_of(path.read_bytes())[7:23]
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ReleaseInputError(f"packaged task protocol cannot be loaded: {path}")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their module through sys.modules while it executes.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _summary(output: Path, *, phase: str, operation_id: str | None) -> dict[str, Any]:
    raw = _read_input(output / "same-root-result.json", label=f"{phase} result", maximum=1048576)
    value = strict_json_loads(raw)
    if (
        type(value) is not dict or value.get("contract_version") != SAME_ROOT_RESULT_CONTRACT
        or value.get("phase") != phase or value.get("status") != "pass"
        or (operation_id is not None and value.get("operation_id") != operation_id)
    ):
        raise ReleaseIdentityError(f"{output} is not a passed same-root {phase} result for this operation")
    return value


def _tuple(value: object, *, label: str) -> dict[str, Any]:
    try:
        return require_root_tuple(value, label=label)
    except ValueError as exc:
        raise ReleaseIdentityError(str(exc)) from exc


def _state_bytes(state: Path, name: str, *, maximum: int = 16 * 1024 * 1024) -> bytes:
    return _read_input(state / name, label=f"Windows state record {name}", maximum=maximum)


def _state_json(state: Path, name: str) -> dict[str, Any]:
    value = strict_json_loads(_state_bytes(state, name, maximum=1048576))
    if type(value) is not dict:
        raise ReleaseIdentityError(f"Windows state record {name} is not an object")
    return value


def verify_same_root_plan(
    *, report: VerifyReport, package: Path, prestop_output: Path, observe_output: Path,
) -> dict[str, Any]:
    """Recompute the observed plan from the fetched registry bytes with the packaged protocol.

    The plan's fields must restate the helper's exact observation; the original
    bytes must be the prestop R0 (generation 1) or this operation's latest
    re-attestation; R1 must be the protocol's own re-attestation of them with an
    identical body, record count and watermark. Nothing here trusts a PowerShell
    re-emission of an identity value. The operation belongs to the release that
    started it: the prestop and observe phases, the Windows prestop record and
    the plan must all name the package verifying the plan.
    """

    if not report.passed:
        raise ReleaseIdentityError("release package did not verify; refusing plan verification")
    release = report.manifest.sha256
    protocol = load_packaged_protocol(package)
    observed = _summary(observe_output, phase="observe", operation_id=None)
    operation_id = _operation(observed.get("operation_id"))
    prestopped = _summary(prestop_output, phase="prestop", operation_id=operation_id)
    if prestopped.get("release_manifest_sha256") != release or observed.get("release_manifest_sha256") != release:
        raise ReleaseIdentityError("the operation's phases ran another release package than the one verifying its plan")
    records = observe_output / "windows-operation" / "same-root"
    state = records / "state"
    child = strict_json_loads(_read_input(records / "same-root-result.json", label="observe child result", maximum=4194304))
    if type(child) is not dict or child.get("status") != "pass" or child.get("phase") != "observe" or child.get("operation_id") != operation_id:
        raise ReleaseIdentityError("observe child result does not bind this operation")
    facts = child.get("facts")
    plan_facts = facts.get("plan") if type(facts) is dict else None
    if type(plan_facts) is not dict or type(plan_facts.get("name")) is not str:
        raise ReleaseIdentityError("observe child result names no plan")
    plan_name = plan_facts["name"]
    plan_raw = _state_bytes(state, plan_name, maximum=1048576)
    plan_sha = sha256_of(plan_raw)
    if plan_facts.get("sha256") != plan_sha:
        raise ReleaseIdentityError("fetched plan bytes differ from the child's plan digest")
    try:
        plan = decode_same_root_plan(plan_raw)
    except ValueError as exc:
        raise ReleaseIdentityError(f"observed plan is invalid: {exc}") from exc
    if plan.operation_id != operation_id or plan_name != f"plan-{plan.sequence}.json":
        raise ReleaseIdentityError("observed plan does not bind this operation sequence")
    if plan.release_manifest_sha256 != release:
        raise ReleaseIdentityError("observed plan was produced by another release package")
    # Prestop facts, cross-checked against the separately fetched prestop evidence.
    prestop_state = prestop_output / "windows-operation" / "same-root" / "state"
    for name in ("api-proof-prestop.json", "r0.registry.json", "baseline.json", "prestop.json"):
        if _state_bytes(state, name) != _state_bytes(prestop_state, name):
            raise ReleaseIdentityError(f"{name} changed between prestop and observe")
    started = _state_json(state, "prestop.json")
    if started.get("operation_id") != operation_id or started.get("release_manifest_sha256") != release:
        raise ReleaseIdentityError("the operation was started by another release package")
    proof = _state_json(state, "api-proof-prestop.json")
    quiescence_state = strict_json_loads(proof["proof_line"]) if type(proof.get("proof_line")) is str else None
    if type(quiescence_state) is not dict or type(quiescence_state.get("quiescence")) is not dict:
        raise ReleaseIdentityError("prestop API proof is not a quiescence state")
    quiescence = quiescence_state["quiescence"]
    original_r0 = _state_bytes(state, "r0.registry.json")
    if sha256_of(original_r0) != quiescence.get("registry_sha256") or len(original_r0) != quiescence_state.get("total_bytes"):
        raise ReleaseIdentityError("prestop original registry differs from the API's own proof")
    prestop_root = _tuple(quiescence.get("root_identity"), label="prestop root identity")
    body = _state_json(state, "baseline.json").get("body_sha256")
    if body != protocol.same_root_registry_body_sha256(original_r0) or plan.expected_body_sha256 != body:
        raise ReleaseIdentityError("plan body differs from the prestop original body")
    if plan.record_count != quiescence.get("record_count") or plan.submission_watermark_bucket != quiescence.get("submission_watermark_bucket"):
        raise ReleaseIdentityError("plan does not conserve the prestop record count and watermark")
    baseline_sha: object
    if plan.generation == 1:
        baseline_root, baseline_sha = prestop_root, sha256_of(original_r0)
    else:
        previous = _state_json(state, f"apply-{plan.generation - 1}.json")
        baseline_root = _tuple(previous.get("current_root"), label="previous re-attestation root")
        baseline_sha = previous.get("registry_sha256")
    if plan.approved_root != baseline_root or plan.expected_r0_sha256 != baseline_sha:
        raise ReleaseIdentityError("plan does not start from this operation's latest registry")
    live = _state_bytes(state, f"plan-{plan.sequence}.registry.json")
    try:
        if plan.registry_state == "r0":
            original = live
        else:
            # Putting the approved tuple back is itself the protocol's re-attestation in reverse.
            original = protocol.same_root_reattested_registry(live, approved_root=plan.current_root, current_root=plan.approved_root)
        r1 = protocol.same_root_reattested_registry(original, approved_root=plan.approved_root, current_root=plan.current_root)
    except protocol.SameRootRefused as exc:
        raise ReleaseIdentityError(f"the packaged protocol refuses the observed registry: {exc}") from exc
    if (sha256_of(original), len(original)) != (plan.expected_r0_sha256, plan.expected_r0_bytes):
        raise ReleaseIdentityError("recomputed original registry differs from the plan")
    if (sha256_of(r1), len(r1)) != (plan.expected_r1_sha256, plan.expected_r1_bytes):
        raise ReleaseIdentityError("recomputed re-attestation differs from the plan")
    if plan.registry_state == "r1" and live != r1:
        raise ReleaseIdentityError("live re-attested registry differs from the recomputation")
    if protocol.same_root_registry_body_sha256(r1) != body:
        raise ReleaseIdentityError("re-attestation does not conserve the registry body")
    return {
        "contract_version": SAME_ROOT_VERIFICATION_CONTRACT,
        "operation_id": operation_id,
        "plan_name": plan_name,
        "plan_sha256": plan_sha,
        "generation": plan.generation,
        "outcome": plan.outcome,
        "registry_state": plan.registry_state,
        "approved_root": plan.approved_root,
        "current_root": plan.current_root,
        "r0_sha256": plan.expected_r0_sha256,
        "r0_bytes": plan.expected_r0_bytes,
        "r1_sha256": plan.expected_r1_sha256,
        "r1_bytes": plan.expected_r1_bytes,
        "body_sha256": body,
        "record_count": plan.record_count,
        "submission_watermark_bucket": plan.submission_watermark_bucket,
        "protocol_sha256": sha256_of((package / "api-context" / "agent_task_protocol_v2.py").read_bytes()),
        "release_manifest_sha256": report.manifest.sha256,
    }


def write_same_root_approval(
    *, report: VerifyReport, package: Path, prestop_output: Path, observe_output: Path,
    approved_by: str, decision_reference: str, output: Path, now: datetime | None = None,
) -> dict[str, Any]:
    """Verify the plan, then bind the operator's decision to its exact digest."""

    if not output.is_absolute() or output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ReleaseInputError("approval output must be a new absolute directory under an existing parent")
    verification = verify_same_root_plan(
        report=report, package=package, prestop_output=prestop_output, observe_output=observe_output,
    )
    if verification["outcome"] != "reattest":
        raise ReleaseIdentityError("the observed plan is a no-op: no registry write to approve; restore directly")
    try:
        approval = SameRootApproval(
            operation_id=verification["operation_id"], plan_sha256=verification["plan_sha256"],
            approved_by=approved_by, decision_reference=decision_reference,
            approved_at_utc=(now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    except ValueError as exc:
        raise ReleaseInputError(str(exc)) from exc
    output.mkdir(mode=0o700)
    write_new_json(output / "plan-verification.json", verification)
    raw = encode_same_root_approval(approval)
    write_new_exact(output / APPROVAL_STAGED_NAME, raw)
    return {
        "approval": str(output / APPROVAL_STAGED_NAME), "approval_sha256": sha256_of(raw),
        "plan_sha256": verification["plan_sha256"], "operation_id": verification["operation_id"],
        "generation": verification["generation"], "current_root": verification["current_root"],
    }


__all__ = [
    "APPROVAL_STAGED_NAME",
    "LEGACY_SCOPE_MAX_AGE_SECONDS",
    "SAME_ROOT_BINDING_CONTRACT",
    "SAME_ROOT_OPERATION_CONTRACT",
    "SAME_ROOT_RESULT_CONTRACT",
    "load_packaged_protocol",
    "require_zero_duty_inventory",
    "run_same_root_phase",
    "same_root_binding_document",
    "verify_same_root_plan",
    "write_same_root_approval",
]
