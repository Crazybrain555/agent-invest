#!/usr/bin/env python3
"""Collect one content-free phase trace bound to held-out validation."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

from disclosure_anchor.adapters.runtime.mineru_host_capacity_observer import (
    build_host_observer_ssh_command,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_WINDOWS_COLLECTOR_PATH,
    canonical_payload_sha256,
)
from disclosure_anchor.adapters.runtime.mineru_phase_trace_capture import (
    parse_phase_trace_capture,
    summarize_phase_trace_capture,
)
from scripts.freeze_mineru_campaign_epoch import _read_private_json


_MAX_RECEIPT_BYTES = 16 * 1024 * 1024
_MAX_CAPTURE_BYTES = 268_435_456
_LOCAL_COLLECTOR = (
    Path(__file__).resolve().parent / "windows" / "collect_mineru_runtime.ps1"
)


def _read_regular(path: Path, *, label: str, maximum_bytes: int) -> bytes:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not 0 < metadata.st_size <= maximum_bytes
    ):
        raise ValueError(f"{label} must be one bounded regular file")
    payload = path.read_bytes()
    if len(payload) != metadata.st_size:
        raise ValueError(f"{label} changed while reading")
    return payload


def _utc(value: object, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{label} is not UTC")
    return parsed


def _wrapper(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"receipt_sha256", "receipt"}:
        raise ValueError(f"{label} wrapper drifted")
    receipt = value.get("receipt")
    if (
        not isinstance(receipt, dict)
        or value.get("receipt_sha256") != canonical_payload_sha256(receipt)
    ):
        raise ValueError(f"{label} hash drifted")
    return receipt


def _validation_identity(
    value: object,
) -> tuple[datetime, datetime, str, str, str, str, int, int]:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema",
            "status",
            "created_at_utc",
            "policy",
            "database_access",
            "queue_access",
            "document_count",
            "documents",
            "epoch_before",
            "epoch_after",
        }
        or value.get("schema") != "mineru_heldout_validation_receipt.v1"
        or value.get("status") != "pass"
        or value.get("policy") != "operator-held-out-complete-pdf.v1"
        or value.get("database_access") != "none"
        or value.get("queue_access") != "none"
    ):
        raise ValueError("held-out validation receipt is not PASS")
    documents = value.get("documents")
    if (
        not isinstance(documents, list)
        or not 2 <= len(documents) <= 8
        or value.get("document_count") != len(documents)
    ):
        raise ValueError("held-out validation documents are invalid")
    starts: list[datetime] = []
    finishes: list[datetime] = []
    page_count = 0
    runtime_identities: set[str] = set()
    source_identities: set[str] = set()
    for index, item in enumerate(documents):
        receipt = _wrapper(item, label=f"document {index}")
        input_evidence = receipt.get("input")
        provider = receipt.get("provider")
        identity = receipt.get("identity")
        if (
            receipt.get("schema") != "mineru_smoke_receipt.v5"
            or receipt.get("status") != "pass"
            or not isinstance(input_evidence, dict)
            or not isinstance(provider, dict)
            or not isinstance(identity, dict)
            or input_evidence.get("profile") != "diagnostic_custom"
            or provider.get("page_count") != input_evidence.get("page_count")
            or isinstance(provider.get("page_count"), bool)
            or not isinstance(provider.get("page_count"), int)
            or provider["page_count"] < 2
            or not isinstance(input_evidence.get("sha256"), str)
            or input_evidence.get("sha256") in source_identities
        ):
            raise ValueError(f"held-out document {index} is invalid")
        starts.append(_utc(receipt.get("started_at_utc"), label="smoke start"))
        finishes.append(_utc(receipt.get("finished_at_utc"), label="smoke finish"))
        page_count += provider["page_count"]
        source_identities.add(str(input_evidence["sha256"]))
        runtime_identities.add(str(identity.get("runtime_manifest_identity_sha256")))
    if len(runtime_identities) != 1:
        raise ValueError("held-out validation runtime identity drifted")
    before = _wrapper(value.get("epoch_before"), label="epoch before")
    after = _wrapper(value.get("epoch_after"), label="epoch after")
    before_epoch = before.get("service_epoch")
    after_epoch = after.get("service_epoch")
    clean_safety = {
        "restart_count_total": 0,
        "oom_killed_count": 0,
        "unsafe_container_count": 0,
        "cgroup_oom_total": 0,
        "cgroup_oom_kill_total": 0,
    }
    if (
        before.get("schema") != "mineru-service-epoch-freeze.v2"
        or after.get("schema") != "mineru-service-epoch-freeze.v2"
        or before.get("status") != "pass"
        or after.get("status") != "pass"
        or before.get("safety") != clean_safety
        or after.get("safety") != clean_safety
        or not isinstance(before_epoch, dict)
        or before_epoch != after_epoch
        or before.get("service_epoch_sha256")
        != canonical_payload_sha256(before_epoch)
        or after.get("service_epoch_sha256")
        != canonical_payload_sha256(after_epoch)
    ):
        raise ValueError("held-out validation service epoch drifted")
    started = min(starts)
    finished = max(finishes)
    if not (
        _utc(before.get("created_at_utc"), label="epoch before creation")
        <= started
        < finished
        <= _utc(after.get("created_at_utc"), label="epoch after creation")
    ):
        raise ValueError("held-out validation epoch does not bracket documents")
    return (
        started,
        finished,
        str(before_epoch["runtime_manifest_identity_sha256"]),
        str(before_epoch["collector_sha256"]),
        str(before_epoch["windows_node_identity_sha256"]),
        str(before_epoch["api_container_id"]),
        len(documents),
        page_count,
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
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validation-receipt", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--capacity-mode", choices=("legacy", "candidate"), required=True)
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
    receipt = json.loads(
        _read_regular(
            args.validation_receipt,
            label="held-out validation receipt",
            maximum_bytes=_MAX_RECEIPT_BYTES,
        )
    )
    (
        started,
        finished,
        runtime_identity,
        collector_sha256,
        node_sha256,
        api_container_id,
        document_count,
        page_count,
    ) = _validation_identity(receipt)
    manifest_wrapper = _read_private_json(args.runtime_manifest)
    manifest = manifest_wrapper.get("manifest")
    if (
        not isinstance(manifest, dict)
        or manifest_wrapper.get("identity_sha256") != runtime_identity
    ):
        raise ValueError("runtime manifest does not match held-out validation")
    topology = manifest.get("topology")
    if not isinstance(topology, dict):
        raise ValueError("runtime manifest topology is invalid")
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
