#!/usr/bin/env python3
"""Freeze one content-free MinerU service epoch without DB or inference."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import stat

from disclosure_anchor.adapters.runtime.mineru_host_capacity_observer import (
    MineruHostCapacitySampler,
    build_host_observer_ssh_command,
    project_host_service_epoch,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    canonical_payload_sha256,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads


FREEZE_SCHEMA = "mineru-service-epoch-freeze.v2"
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024


def _read_private_json(path: Path) -> dict[str, object]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= _MAX_MANIFEST_BYTES
        ):
            raise ValueError("runtime manifest must be one owner-only bounded file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(_MAX_MANIFEST_BYTES + 1)
        after = os.fstat(descriptor)
        if len(payload) != metadata.st_size or (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        ):
            raise ValueError("runtime manifest changed while reading")
    finally:
        os.close(descriptor)
    decoded = strict_json_loads(payload)
    if not isinstance(decoded, dict):
        raise ValueError("runtime manifest root must be an object")
    return decoded


def _write_new(path: Path, payload: object) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError("service epoch output must be new")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--receipt-out", type=Path, required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-user", required=True)
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--ssh-identity", type=Path, required=True)
    parser.add_argument("--ssh-known-hosts", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    try:
        wrapper = _read_private_json(args.runtime_manifest)
        manifest = wrapper.get("manifest")
        runtime_identity = wrapper.get("identity_sha256")
        if (
            not isinstance(manifest, dict)
            or manifest.get("contract_version") != "mineru-runtime-bundle.v8"
            or runtime_identity != canonical_payload_sha256(manifest)
        ):
            raise ValueError("runtime manifest identity is invalid")
        topology = manifest.get("topology")
        local = manifest.get("client")
        orchestrator = manifest.get("orchestrator")
        if not all(isinstance(item, dict) for item in (topology, local, orchestrator)):
            raise ValueError("runtime manifest topology is invalid")
        assert isinstance(topology, dict)
        assert isinstance(local, dict)
        assert isinstance(orchestrator, dict)
        collector_sha256 = topology.get("windows_collector_sha256")
        node_sha256 = topology.get("windows_node_identity_sha256")
        host_key_sha256 = topology.get("ssh_host_key_sha256")
        compose_sha256 = topology.get("windows_compose_sha256")
        writer_sha256 = local.get("writer_code_sha256")
        api_image_digest = orchestrator.get("container_image_digest")
        if not all(
            isinstance(item, str)
            for item in (
                collector_sha256,
                node_sha256,
                host_key_sha256,
                compose_sha256,
                writer_sha256,
                api_image_digest,
            )
        ):
            raise ValueError("runtime manifest host identity is invalid")
        ssh = build_host_observer_ssh_command(
            host=args.ssh_host,
            user=args.ssh_user,
            port=args.ssh_port,
            identity_file=args.ssh_identity,
            known_hosts_file=args.ssh_known_hosts,
            expected_host_key_sha256=str(host_key_sha256),
        )
        sampler = MineruHostCapacitySampler(
            ssh_command=ssh,
            expected_collector_sha256=str(collector_sha256),
            expected_windows_node_identity_sha256=str(node_sha256),
        )
        raw_sample = sampler.sample_payload()
        sample = project_host_service_epoch(
            raw_sample,
            expected_collector_sha256=str(collector_sha256),
            expected_windows_node_identity_sha256=str(node_sha256),
        )
        if (
            sample.restart_count_total != 0
            or sample.oom_killed_count != 0
            or sample.unsafe_container_count != 0
            or sample.cgroup_oom_total != 0
            or sample.cgroup_oom_kill_total != 0
        ):
            raise ValueError("MinerU service epoch is not clean")
        created_at = datetime.now(UTC).isoformat()
        service_epoch = {
            "schema": "mineru-service-epoch.v1",
            "runtime_manifest_identity_sha256": runtime_identity,
            "collector_sha256": collector_sha256,
            "windows_node_identity_sha256": node_sha256,
            "windows_compose_sha256": compose_sha256,
            "writer_code_sha256": writer_sha256,
            "api_image_digest": api_image_digest,
            "container_epoch_sha256": sample.container_epoch_sha256,
            "api_container_id": sample.api_container_id,
        }
        receipt = {
            "schema": FREEZE_SCHEMA,
            "status": "pass",
            "created_at_utc": created_at,
            "database_access": "none",
            "queue_access": "none",
            "service_epoch": service_epoch,
            "service_epoch_sha256": canonical_payload_sha256(service_epoch),
            "safety": {
                "restart_count_total": sample.restart_count_total,
                "oom_killed_count": sample.oom_killed_count,
                "unsafe_container_count": sample.unsafe_container_count,
                "cgroup_oom_total": sample.cgroup_oom_total,
                "cgroup_oom_kill_total": sample.cgroup_oom_kill_total,
            },
        }
        _write_new(args.receipt_out, receipt)
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        raise SystemExit(f"[abort] service epoch freeze failed: {exc}") from exc
    print(
        "mineru-service-epoch-freeze: PASS "
        f"sha256={receipt['service_epoch_sha256']} receipt={args.receipt_out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
