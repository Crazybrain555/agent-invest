#!/usr/bin/env python3
"""Collect one bounded, content-free MinerU trace after a staged-load arm."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_STAGED_LOAD_MINIMUM_RUNAWAY_TIMEOUT_SECONDS,
    MINERU_WINDOWS_COLLECTOR_PATH,
)
from disclosure_anchor.adapters.runtime.mineru_phase_trace_capture import (
    parse_phase_trace_capture,
    summarize_phase_trace_capture,
)
from scripts.mineru_staged_load import _host_observer_ssh_base


_MAX_RECEIPT_BYTES = 256 * 1024 * 1024
_MAX_CAPTURE_BYTES = 268_435_456
_STAGE_COUNTS = (4, 8, 16)
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_CONTAINER_ID_RE = re.compile(r"^[a-f0-9]{64}$")
_LOCAL_COLLECTOR = (
    Path(__file__).resolve().parent / "windows" / "collect_mineru_runtime.ps1"
)


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _read_regular(path: Path, *, label: str, maximum_bytes: int) -> bytes:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or metadata.st_size > maximum_bytes
    ):
        raise ValueError(f"{label} must be one bounded regular file")
    payload = path.read_bytes()
    if len(payload) != metadata.st_size:
        raise ValueError(f"{label} changed while reading")
    return payload


def _utc(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{label} is not UTC")
    return parsed


def _safe_staged_timeout(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and value >= MINERU_STAGED_LOAD_MINIMUM_RUNAWAY_TIMEOUT_SECONDS
    )


def _receipt_identity(
    receipt: object,
) -> tuple[datetime, datetime, str, str, str, int, int]:
    safety_limits = receipt.get("safety_limits") if isinstance(receipt, dict) else None
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != "mineru_staged_load_receipt.v6"
        or receipt.get("receipt_schema_version") != 6
        or receipt.get("status") != "pass"
        or receipt.get("failure") is not None
        or receipt.get("database_access") != "none"
        or receipt.get("queue_access") != "none"
        or receipt.get("fixed_stage_document_counts") != list(_STAGE_COUNTS)
        or not isinstance(safety_limits, dict)
        or set(safety_limits)
        != {
            "profile",
            "document_runaway_timeout_seconds",
            "api_drain_timeout_seconds",
        }
        or safety_limits.get("profile")
        != "whole-document-runaway-and-drain.v1"
        or not _safe_staged_timeout(
            safety_limits.get("document_runaway_timeout_seconds")
        )
        or not _safe_staged_timeout(
            safety_limits.get("api_drain_timeout_seconds")
        )
    ):
        raise ValueError("staged-load receipt is not PASS")
    started = _utc(receipt.get("started_at_utc"), label="staged-load start")
    finished = _utc(receipt.get("finished_at_utc"), label="staged-load finish")
    if not started < finished:
        raise ValueError("staged-load timeline is invalid")
    host = receipt.get("host_capacity")
    if (
        not isinstance(host, dict)
        or host.get("status") != "pass"
        or host.get("failure") is not None
        or _SHA256_RE.fullmatch(str(host.get("collector_sha256"))) is None
        or _SHA256_RE.fullmatch(
            str(host.get("windows_node_identity_sha256"))
        )
        is None
        or host.get("violations") != []
        or host.get("sampling_failures") != []
    ):
        raise ValueError("staged-load host evidence is not PASS")
    samples = host.get("samples")
    if not isinstance(samples, list) or len(samples) < 2:
        raise ValueError("staged-load host evidence has no stable epoch")
    api_ids: set[str] = set()
    for sample in samples:
        containers = sample.get("containers") if isinstance(sample, dict) else None
        if not isinstance(containers, list):
            raise ValueError("staged-load host containers are invalid")
        api = [
            item
            for item in containers
            if isinstance(item, dict) and item.get("name") == "mineru-api"
        ]
        if (
            len(api) != 1
            or _CONTAINER_ID_RE.fullmatch(str(api[0].get("id"))) is None
            or api[0].get("restart_count") != 0
            or api[0].get("oom_killed") is not False
            or api[0].get("running") is not True
            or api[0].get("status") != "running"
            or api[0].get("health") != "healthy"
        ):
            raise ValueError("staged-load API epoch is invalid")
        api_ids.add(str(api[0]["id"]))
    if len(api_ids) != 1:
        raise ValueError("staged-load API epoch changed")
    stages = receipt.get("stages")
    if not isinstance(stages, list) or len(stages) != len(_STAGE_COUNTS):
        raise ValueError("staged-load stages are invalid")
    document_count = 0
    page_count = 0
    for stage, expected_count in zip(stages, _STAGE_COUNTS, strict=True):
        documents = stage.get("documents") if isinstance(stage, dict) else None
        if (
            not isinstance(stage, dict)
            or stage.get("status") != "pass"
            or stage.get("failure") is not None
            or stage.get("stage_document_count") != expected_count
            or not isinstance(documents, list)
            or len(documents) != expected_count
        ):
            raise ValueError("staged-load documents are invalid")
        for document in documents:
            if (
                not isinstance(document, dict)
                or document.get("status") != "pass"
                or isinstance(document.get("page_count"), bool)
                or not isinstance(document.get("page_count"), int)
                or document["page_count"] <= 0
            ):
                raise ValueError("staged-load document is not PASS")
            document_count += 1
            page_count += int(document["page_count"])
    return (
        started,
        finished,
        str(host["collector_sha256"]),
        str(host["windows_node_identity_sha256"]),
        api_ids.pop(),
        document_count,
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
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            if not payload.endswith(b"\n"):
                handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged-load-receipt", type=Path, required=True)
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
    receipt_payload = _read_regular(
        args.staged_load_receipt,
        label="staged-load receipt",
        maximum_bytes=_MAX_RECEIPT_BYTES,
    )
    try:
        receipt = json.loads(receipt_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("staged-load receipt is not UTF-8 JSON") from exc
    (
        started,
        finished,
        expected_collector_sha256,
        expected_node_sha256,
        expected_api_id,
        expected_document_count,
        expected_page_count,
    ) = _receipt_identity(receipt)
    local_collector_payload = _read_regular(
        _LOCAL_COLLECTOR,
        label="local Windows collector",
        maximum_bytes=4 * 1024 * 1024,
    )
    if _sha256(local_collector_payload) != expected_collector_sha256:
        raise ValueError("staged-load collector is not exact-current")
    ssh = _host_observer_ssh_base(
        host=args.ssh_host,
        user=args.ssh_user,
        port=args.ssh_port,
        identity_file=args.ssh_identity,
        known_hosts_file=args.ssh_known_hosts,
    )
    since = (started - timedelta(seconds=1)).isoformat()
    until = finished.isoformat()
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
        since,
        "-TraceUntilUtc",
        until,
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
        expected_collector_sha256=expected_collector_sha256,
        expected_windows_node_identity_sha256=expected_node_sha256,
        expected_container_id=expected_api_id,
        require_pipeline_overlap=args.capacity_mode == "candidate",
    )
    if (
        summary.get("document_count") != expected_document_count
        or summary.get("page_count") != expected_page_count
    ):
        raise ValueError("phase trace does not conserve staged-load documents/pages")
    _write_new_private(args.capture_out, completed.stdout)
    print(
        "mineru-phase-trace-capture: PASS "
        f"documents={expected_document_count} pages={expected_page_count} "
        f"capture={args.capture_out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
