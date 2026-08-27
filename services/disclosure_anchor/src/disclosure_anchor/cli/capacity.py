"""CLI for passive, DB-free MinerU capacity observation and replay."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from pydantic import ValidationError

from disclosure_anchor.adapters.runtime.capacity_observer import (
    run_capacity_observation,
    verify_capacity_observation,
)
from disclosure_anchor.adapters.runtime.capacity_sources import (
    GpuCapacitySampler,
    MineruApiCapacitySampler,
    VllmCapacitySampler,
)
from disclosure_anchor.adapters.runtime.capacity_runtime_identity import (
    verify_capacity_runtime_topology,
)
from disclosure_anchor.adapters.runtime.mineru_host_capacity_observer import (
    MineruHostCapacitySampler,
    build_host_observer_ssh_command,
)
from disclosure_anchor.application.contracts.capacity import canonical_json_bytes
from disclosure_anchor.settings import Settings, load_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="disclosure-anchor capacity")
    commands = parser.add_subparsers(dest="command", required=True)
    observe = commands.add_parser("observe", help="run one passive observation")
    observe.add_argument("--duration-seconds", type=float, required=True)
    observe.add_argument("--interval-seconds", type=float, default=60.0)
    observe.add_argument("--runtime-manifest", type=Path, required=True)
    observe.add_argument("--host-observer-ssh-host", required=True)
    observe.add_argument("--host-observer-ssh-user", required=True)
    observe.add_argument("--host-observer-ssh-port", type=int, default=22)
    observe.add_argument("--host-observer-identity-file", type=Path, required=True)
    observe.add_argument("--host-observer-known-hosts-file", type=Path, required=True)
    observe.add_argument("--run-id")

    for command in ("verify", "summarize"):
        child = commands.add_parser(
            command,
            help="mechanically replay an existing observation",
        )
        child.add_argument("--run-id", required=True)
        if command == "verify":
            child.add_argument("--require-complete", action="store_true")
    return parser


def _observe(args: argparse.Namespace, settings: Settings) -> int:
    if args.duration_seconds <= 0 or args.interval_seconds <= 0:
        raise ValueError("duration and interval must be positive")
    if (
        settings.disclosure_mineru_api_url is None
        or settings.disclosure_mineru_observability_url is None
        or settings.disclosure_gpu_metrics_url is None
        or settings.disclosure_gpu_expected_uuid is None
    ):
        raise ValueError("API, vLLM and UUID-pinned GPU telemetry are required")
    if settings.disclosure_mineru_docker_memory_reserve_bytes < 1:
        raise ValueError("positive Docker memory reserve is required")
    topology = verify_capacity_runtime_topology(
        settings=settings,
        runtime_manifest_path=args.runtime_manifest,
    )
    ssh_command = build_host_observer_ssh_command(
        host=args.host_observer_ssh_host,
        user=args.host_observer_ssh_user,
        port=args.host_observer_ssh_port,
        identity_file=args.host_observer_identity_file,
        known_hosts_file=args.host_observer_known_hosts_file,
        expected_host_key_sha256=topology.ssh_host_key_sha256,
    )
    timeout = settings.worker_progress_metrics_timeout_seconds
    samplers = (
        MineruApiCapacitySampler(
            url=settings.disclosure_mineru_api_url,
            timeout_seconds=timeout,
            task_slots=settings.disclosure_mineru_api_task_slots,
        ),
        VllmCapacitySampler(
            url=settings.disclosure_mineru_observability_url,
            timeout_seconds=timeout,
        ),
        GpuCapacitySampler(
            url=settings.disclosure_gpu_metrics_url,
            timeout_seconds=timeout,
            expected_device_uuid=settings.disclosure_gpu_expected_uuid,
        ),
        MineruHostCapacitySampler(
            ssh_command=ssh_command,
            expected_collector_sha256=topology.windows_collector_sha256,
            expected_windows_node_identity_sha256=(
                topology.windows_node_identity_sha256
            ),
            docker_memory_reserve_bytes=(
                settings.disclosure_mineru_docker_memory_reserve_bytes
            ),
        ),
    )
    run = run_capacity_observation(
        settings=settings,
        samplers=samplers,
        duration_seconds=args.duration_seconds,
        interval_seconds=args.interval_seconds,
        run_id=args.run_id,
    )
    print(canonical_json_bytes(run.model_dump(mode="json")).decode("utf-8"))
    return 0 if run.status == "complete" else 1


def _replay(args: argparse.Namespace, settings: Settings) -> int:
    verified = verify_capacity_observation(settings=settings, run_id=args.run_id)
    print(canonical_json_bytes(verified.run.model_dump(mode="json")).decode("utf-8"))
    if getattr(args, "require_complete", False) and verified.run.status != "complete":
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings()
        if args.command == "observe":
            return _observe(args, settings)
        return _replay(args, settings)
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        print(f"[FAIL] capacity {args.command}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
