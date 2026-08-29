#!/usr/bin/env python3
"""Collect one content-free phase trace bound to held-out validation."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess

from disclosure_anchor.adapters.runtime.mineru_host_capacity_observer import (
    build_host_observer_ssh_command,
)
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import (
    MinerUDeploymentGateError,
    verify_mineru_heldout_validation,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_WINDOWS_COLLECTOR_PATH,
    canonical_payload_sha256,
    client_bundle_identity,
    verify_runtime_manifest_payload,
    writer_code_digest,
)
from disclosure_anchor.adapters.runtime.mineru_phase_trace_capture import (
    parse_phase_trace_capture,
    summarize_phase_trace_capture,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from scripts.freeze_mineru_campaign_epoch import _read_private_json


_MAX_RECEIPT_BYTES = 16 * 1024 * 1024
_MAX_CAPTURE_BYTES = 268_435_456
_LOCAL_COLLECTOR = (
    Path(__file__).resolve().parent / "windows" / "collect_mineru_runtime.ps1"
)
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


def _read_regular(path: Path, *, label: str, maximum_bytes: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= maximum_bytes
        ):
            raise ValueError(f"{label} must be one owner-only bounded regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(maximum_bytes + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(payload) != metadata.st_size or (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError(f"{label} changed while reading")
    return payload


def _validation_identity(
    value: object,
    *,
    contract: dict[str, object],
) -> tuple[datetime, datetime, str, str, str, str, int, int]:
    try:
        verified = verify_mineru_heldout_validation(value, **contract)
    except MinerUDeploymentGateError as exc:
        raise ValueError(f"held-out validation failed: {exc}") from exc
    return (
        verified.started_at_utc,
        verified.finished_at_utc,
        verified.runtime_identity_sha256,
        verified.collector_sha256,
        verified.windows_node_identity_sha256,
        verified.api_container_id,
        verified.document_count,
        verified.page_count,
    )


def _write_new_private(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError("phase-trace capture output must be new")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            if not payload.endswith(b"\n"):
                handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        parent = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validation-receipt", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--mineru-bin", type=Path, required=True)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--observability-url", required=True)
    parser.add_argument("--inference-upstream-url", required=True)
    parser.add_argument("--runtime-bundle-identity", required=True)
    parser.add_argument("--validation-max-age-seconds", type=int, default=86400)
    parser.add_argument("--capacity-mode", choices=("legacy", "candidate"), required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--profile-sha256", required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-user", required=True)
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--ssh-identity", type=Path, required=True)
    parser.add_argument("--ssh-known-hosts", type=Path, required=True)
    parser.add_argument("--capture-out", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    if _SHA256_RE.fullmatch(args.profile_sha256) is None:
        raise ValueError("profile SHA-256 is not canonical")
    profile = strict_json_loads(
        _read_regular(
            args.profile,
            label="execution profile",
            maximum_bytes=1024 * 1024,
        )
    )
    if not isinstance(profile, dict) or canonical_payload_sha256(profile) != args.profile_sha256:
        raise ValueError("profile SHA-256 does not match canonical execution profile")
    receipt = strict_json_loads(
        _read_regular(
            args.validation_receipt,
            label="held-out validation receipt",
            maximum_bytes=_MAX_RECEIPT_BYTES,
        )
    )
    manifest_wrapper = _read_private_json(args.runtime_manifest)
    local_client = client_bundle_identity(args.mineru_bin)
    local_writer = writer_code_digest()
    verified_manifest = verify_runtime_manifest_payload(
        manifest_wrapper,
        configured_identity=args.runtime_bundle_identity,
        local_client_identity=local_client,
        local_processing_window_size=16,
        local_writer_code_digest=local_writer,
    )
    manifest = verified_manifest.manifest
    topology = manifest.get("topology")
    if not isinstance(topology, dict):
        raise ValueError("runtime manifest topology is invalid")
    expected_topology = {
        key: "sha256:" + hashlib.sha256(value.rstrip("/").encode()).hexdigest()
        for key, value in {
            "api_endpoint_sha256": args.api_url,
            "observability_endpoint_sha256": args.observability_url,
            "inference_upstream_sha256": args.inference_upstream_url,
        }.items()
    }
    orchestrator = manifest.get("orchestrator")
    if not isinstance(orchestrator, dict):
        raise ValueError("runtime manifest orchestrator is invalid")
    contract: dict[str, object] = {
        "expected_identity": {
            "local_client_identity_sha256": local_client.package_set_sha256,
            "local_content_package_versions": dict(
                local_client.content_package_versions
            ),
            "local_processing_window_size": 16,
            "local_writer_code_sha256": local_writer,
            "runtime_manifest_identity_sha256": args.runtime_bundle_identity,
            "orchestrator_runtime_identity_sha256": (
                verified_manifest.orchestrator_identity_sha256
            ),
            "provider_runtime_identity_sha256": (
                verified_manifest.provider_identity_sha256
            ),
            "served_model_id": verified_manifest.served_model_id,
            "orchestrator_task_slots": verified_manifest.max_concurrent_requests,
        },
        "expected_topology": expected_topology,
        "expected_runtime_manifest": manifest,
        "runtime_identity": args.runtime_bundle_identity,
        "task_slots": verified_manifest.max_concurrent_requests,
        "task_retention_seconds": int(orchestrator["task_retention_seconds"]),
        "cleanup_interval_seconds": int(
            orchestrator["task_cleanup_interval_seconds"]
        ),
        "observability_url": args.observability_url,
        "max_age_seconds": args.validation_max_age_seconds,
        "current": datetime.now(UTC),
    }
    (
        started,
        finished,
        runtime_identity,
        collector_sha256,
        node_sha256,
        api_container_id,
        document_count,
        page_count,
    ) = _validation_identity(receipt, contract=contract)
    if runtime_identity != args.runtime_bundle_identity:
        raise ValueError("held-out validation runtime identity drifted")
    local_collector = _read_regular(
        _LOCAL_COLLECTOR,
        label="local Windows collector",
        maximum_bytes=4 * 1024 * 1024,
    )
    if "sha256:" + hashlib.sha256(local_collector).hexdigest() != collector_sha256:
        raise ValueError("held-out collector is not exact-current")
    ssh = build_host_observer_ssh_command(
        host=args.ssh_host,
        user=args.ssh_user,
        port=args.ssh_port,
        identity_file=args.ssh_identity,
        known_hosts_file=args.ssh_known_hosts,
        expected_host_key_sha256=str(topology.get("ssh_host_key_sha256")),
    )
    command = [
        *ssh,
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        MINERU_WINDOWS_COLLECTOR_PATH,
        "-PhaseTrace",
        "-TraceSinceUtc",
        (started - timedelta(seconds=1)).isoformat(),
        "-TraceUntilUtc",
        finished.isoformat(),
        "-ExpectedCapacityMode",
        args.capacity_mode,
        "-ExpectedProfileSha256",
        args.profile_sha256,
    ]
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        detail = " ".join(
            completed.stderr.decode("utf-8", errors="replace").split()
        )[:500]
        raise RuntimeError(f"remote phase-trace collection failed: {detail}")
    if not 0 < len(completed.stdout) <= _MAX_CAPTURE_BYTES:
        raise ValueError("remote phase-trace capture size is invalid")
    capture = parse_phase_trace_capture(completed.stdout)
    if capture.collector_path.casefold() != MINERU_WINDOWS_COLLECTOR_PATH.casefold():
        raise ValueError("remote phase-trace collector path drifted")
    summary = summarize_phase_trace_capture(
        capture,
        expected_profile_sha256=args.profile_sha256,
        expected_capacity_mode=args.capacity_mode,
        expected_collector_sha256=collector_sha256,
        expected_windows_node_identity_sha256=node_sha256,
        expected_container_id=api_container_id,
        require_pipeline_overlap=args.capacity_mode == "candidate",
    )
    if (
        summary.get("document_count") != document_count
        or summary.get("page_count") != page_count
    ):
        raise ValueError("phase trace does not conserve held-out documents/pages")
    _write_new_private(args.capture_out, completed.stdout)
    print(
        "mineru-phase-trace-capture: PASS "
        f"documents={document_count} pages={page_count} capture={args.capture_out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
