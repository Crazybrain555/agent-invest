"""Controller side of one M6 run: bind the frozen spec, open, stop, read status and close.

The native owner already holds its anchor (T0, clock, interval, resources).
``bind`` freezes the run spec from that anchor plus the frozen campaign
inputs and binds it once; the other commands are the controller's control
requests. Every reply is printed as its canonical JSON; nothing here admits
work or stamps observations.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from disclosure_anchor.adapters.runtime.exact_file_write import write_new_exact
from disclosure_anchor.adapters.runtime.m6_continuous_clock import diagnostic_continuous_clock
from disclosure_anchor.adapters.runtime.m6_e2e_run import load_m6_run_directory, m6_owner_client_factory
from disclosure_anchor.adapters.runtime.m6_owner_protocol import M6OwnerClient, M6OwnerProtocolError, M6OwnerRejected
from disclosure_anchor.application.contracts.m6_owner import M6CloseOwner, M6OwnerControl
from disclosure_anchor.application.contracts.m6_run import M6RunSpec, M6RuntimeIdentity
from disclosure_anchor.application.contracts.strict_json import strict_json_loads

_MAX_RECEIPT_BYTES = 8 * 1024 * 1024


def _write_new(path: Path, payload: bytes) -> None:
    write_new_exact(path, payload)


def _client(run_dir: Path) -> M6OwnerClient:
    run = load_m6_run_directory(run_dir)
    clock = diagnostic_continuous_clock()
    return m6_owner_client_factory(run, role="controller", continuous_ns=clock.now_ns)()


def _print_reply(reply: Any) -> None:
    print(reply.canonical_bytes().decode("utf-8"), flush=True)


def bind(args: argparse.Namespace) -> int:
    """Freeze the spec once and bind it; a repeat with identical bytes replays, anything else is refused.

    A lost bind reply leaves the local spec file in place. Re-running with the
    same inputs reproduces the same bytes and only then repeats the owner's
    idempotent bind; the reply status must report exactly this spec.
    """
    run_dir = args.run_dir.absolute()
    run = load_m6_run_directory(run_dir, require_spec=False)
    anchor = run.anchor
    runtime = M6RuntimeIdentity(
        source_commit=args.source_commit, source_manifest_sha256=args.source_manifest_sha256,
        runtime_bundle_identity_sha256=args.runtime_bundle_identity_sha256,
        process_profile_sha256=args.process_profile_sha256, worker_profile_sha256=args.worker_profile_sha256,
        owner_source_sha256=anchor.owner_source_sha256, gpu_device_identity_sha256=anchor.gpu_device_identity_sha256,
        deployment_qualification_sha256=args.deployment_qualification_sha256,
    )
    spec = M6RunSpec(
        run_id=anchor.run_id, campaign_id=args.campaign_id, mode=args.mode, phase=args.phase,
        start_condition=args.start_condition, clock=anchor.clock, runtime=runtime,
        manifest_sha256=args.manifest_sha256, scope_sha256=args.scope_sha256,
        quality_plan_sha256=args.quality_plan_sha256, t0_ticks=anchor.t0_ticks,
        planned_seconds=anchor.planned_seconds, deadline_ticks=anchor.deadline_ticks,
        max_close_ticks=anchor.max_close_ticks, carry_in_attempt_ids=tuple(sorted(set(args.carry_in_attempt_ids))),
        resources=anchor.resources,
    )
    anchor.assert_spec(spec)
    spec_path = run_dir / "run-spec.json"
    if run.spec is not None:
        if spec_path.read_bytes() != spec.canonical_bytes():
            print("run spec already frozen with different content; refusing to bind another spec", file=sys.stderr)
            return 2
        print("run spec already frozen with identical bytes; replaying the owner bind", file=sys.stderr)
    else:
        _write_new(spec_path, spec.canonical_bytes())
        # The frozen spec's directory entry must survive a crash before the
        # owner is asked to bind it; otherwise a bound owner could outlive the
        # only local record of what it was bound to.
        directory_fd = os.open(run_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    client = _client(run_dir)
    try:
        reply = client.bind()
        _print_reply(reply)
        if reply.status.spec_sha256 != spec.canonical_sha256() or reply.status.run_id != spec.run_id:
            print("owner reports a different bound spec or run; not bound to this frozen spec", file=sys.stderr)
            return 1
    finally:
        client.close()
    print(json.dumps({"run_id": spec.run_id, "spec_sha256": spec.canonical_sha256()}), file=sys.stderr, flush=True)
    return 0


def control(args: argparse.Namespace) -> int:
    client = _client(args.run_dir.absolute())
    try:
        _print_reply(client.request(M6OwnerControl(kind=args.command)))
    finally:
        client.close()
    return 0


def close(args: argparse.Namespace) -> int:
    raw = args.campaign_receipt.read_bytes()
    if len(raw) > _MAX_RECEIPT_BYTES:
        print("campaign receipt exceeds its byte bound", file=sys.stderr)
        return 2
    receipt = strict_json_loads(raw.decode("utf-8"))
    assembly = receipt.get("m6_assembly") if isinstance(receipt, dict) else None
    closure = assembly.get("closure") if isinstance(assembly, dict) else None
    if not isinstance(closure, dict) or not closure.get("complete"):
        print("campaign receipt has no complete runner closure; the owner cannot be closed from it", file=sys.stderr)
        return 2
    client = _client(args.run_dir.absolute())
    try:
        _print_reply(client.request(M6CloseOwner(
            ownership_receipt_sha256=closure["ownership_closure_sha256"], residual_count=closure["residual_count"],
            children_exited=closure["children_exited"], reason=args.reason,
        )))
    finally:
        client.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("bind", "open", "stop", "status", "close"):
        item = sub.add_parser(name)
        item.add_argument("--run-dir", type=Path, required=True)
        if name == "bind":
            item.add_argument("--campaign-id", required=True)
            item.add_argument("--mode", choices=("e2e_publication", "service_diagnostic"), required=True)
            item.add_argument("--phase", choices=("short_batch", "hour_baseline", "stability_repeat", "recovery_experiment"), required=True)
            item.add_argument("--start-condition", choices=("cold", "resident_warm", "warm_service_disclosed"), required=True)
            item.add_argument("--manifest-sha256", required=True)
            item.add_argument("--scope-sha256", default=None)
            item.add_argument("--quality-plan-sha256", required=True)
            item.add_argument("--source-commit", required=True)
            item.add_argument("--source-manifest-sha256", required=True)
            item.add_argument("--runtime-bundle-identity-sha256", required=True)
            item.add_argument("--process-profile-sha256", required=True)
            item.add_argument("--worker-profile-sha256", default=None)
            item.add_argument("--deployment-qualification-sha256", required=True)
            item.add_argument("--carry-in-attempt-id", action="append", default=[], dest="carry_in_attempt_ids")
        if name == "close":
            item.add_argument("--campaign-receipt", type=Path, required=True,
                              help="the runner's campaign receipt carrying the complete closure record")
            item.add_argument("--reason", choices=("deadline_drained", "stop_requested", "failed"), required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "bind":
            return bind(args)
        if args.command == "close":
            return close(args)
        return control(args)
    except (M6OwnerRejected, M6OwnerProtocolError, ValueError, OSError) as exc:
        print(json.dumps({"m6_control_error": type(exc).__name__, "message": str(exc)}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
