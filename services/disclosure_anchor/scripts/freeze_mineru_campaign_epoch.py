#!/usr/bin/env python3
"""Freeze one new-only MinerU campaign service epoch without DB or inference."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import stat

from disclosure_anchor.adapters.runtime.mineru_identity import (
    canonical_payload_sha256,
)
from scripts.mineru_staged_load import (
    _HostObserverControlMaster,
    _campaign_epoch_payload,
    _fetch_host_capacity_sample,
)


FREEZE_SCHEMA = "mineru-campaign-epoch-freeze.v1"
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024


def _read_private_json(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise ValueError("runtime manifest must be one owner-only bounded file")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > _MAX_MANIFEST_BYTES
        ):
            raise ValueError("runtime manifest must be one owner-only bounded file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError("runtime manifest changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("runtime manifest changed while reading")
        after = os.fstat(descriptor)
        path_after = path.stat(follow_symlinks=False)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(before, field) != getattr(after, field)
            for field in stable_fields
        ) or any(
            getattr(after, field) != getattr(path_after, field)
            for field in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_nlink",
                "st_size",
            )
        ):
            raise ValueError("runtime manifest changed while reading")
        encoded = b"".join(chunks)
        if len(encoded) != before.st_size:
            raise ValueError("runtime manifest changed while reading")
    finally:
        os.close(descriptor)
    payload = json.loads(encoded)
    if not isinstance(payload, dict):
        raise ValueError("runtime manifest root must be an object")
    return payload


def _write_new_private_json(path: Path, payload: dict[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError("campaign epoch freeze output must be new")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode(
            "utf-8"
        )
        + b"\n"
    )
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="freeze_mineru_campaign_epoch")
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--receipt-out", type=Path, required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-user", required=True)
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--ssh-identity", type=Path, required=True)
    parser.add_argument("--ssh-known-hosts", type=Path, required=True)
    parser.add_argument("--docker-memory-reserve-bytes", type=int, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.receipt_out.exists() or args.receipt_out.is_symlink():
        raise SystemExit(
            f"[abort] output already exists; stale evidence: {args.receipt_out}"
        )
    if (
        isinstance(args.docker_memory_reserve_bytes, bool)
        or args.docker_memory_reserve_bytes < 1
    ):
        raise SystemExit("[abort] Docker memory reserve must be positive")
    try:
        wrapper = _read_private_json(args.runtime_manifest)
        manifest = wrapper.get("manifest")
        runtime_identity = wrapper.get("identity_sha256")
        if (
            not isinstance(manifest, dict)
            or manifest.get("contract_version") != "mineru-runtime-bundle.v6"
            or runtime_identity != canonical_payload_sha256(manifest)
        ):
            raise ValueError("runtime manifest identity is invalid")
        topology = manifest.get("topology")
        if not isinstance(topology, dict):
            raise ValueError("runtime manifest topology is invalid")
        collector_sha256 = topology.get("windows_collector_sha256")
        windows_node_identity_sha256 = topology.get(
            "windows_node_identity_sha256"
        )
        if not isinstance(collector_sha256, str) or not isinstance(
            windows_node_identity_sha256, str
        ):
            raise ValueError("runtime manifest host identity is invalid")
        transport = _HostObserverControlMaster(
            host=args.ssh_host,
            user=args.ssh_user,
            port=args.ssh_port,
            identity_file=args.ssh_identity,
            known_hosts_file=args.ssh_known_hosts,
        )
        try:
            transport.start()
            sample = _fetch_host_capacity_sample(
                transport.session_command(),
                expected_collector_sha256=collector_sha256,
                expected_windows_node_identity_sha256=(
                    windows_node_identity_sha256
                ),
                docker_memory_reserve_bytes=args.docker_memory_reserve_bytes,
            )
        finally:
            transport.close()
        epochs = {
            str(item["name"]): (
                str(item["id"]),
                str(item["started_at_utc"]),
            )
            for item in sample["containers"]
        }
        campaign_epoch = _campaign_epoch_payload(
            collector_sha256=collector_sha256,
            windows_node_identity_sha256=windows_node_identity_sha256,
            epochs=epochs,
        )
        receipt = {
            "schema": FREEZE_SCHEMA,
            "status": "pass",
            "created_at_utc": datetime.now(UTC).isoformat(),
            "database_access": "none",
            "queue_access": "none",
            "runtime_manifest_identity_sha256": runtime_identity,
            "docker_memory_reserve_bytes": args.docker_memory_reserve_bytes,
            "campaign_epoch": campaign_epoch,
            "host_sample": sample,
        }
        _write_new_private_json(args.receipt_out, receipt)
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        raise SystemExit(f"[abort] campaign epoch freeze failed: {exc}") from exc
    print(
        "mineru-campaign-epoch-freeze: PASS "
        f"sha256={campaign_epoch['observed_sha256']} receipt={args.receipt_out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
