#!/usr/bin/env python3
"""Evaluate one frozen A-B-B-A MinerU capacity trial without DB access."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Final

from disclosure_anchor.adapters.runtime.mineru_capacity_commissioning import (
    evaluate_capacity_commissioning,
)
from disclosure_anchor.adapters.runtime.mineru_capacity_evaluator_identity import (
    commissioning_evaluator_identity,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_WINDOWS_COLLECTOR_PATH,
)
from disclosure_anchor.adapters.runtime.mineru_phase_trace_capture import (
    MineruPhaseTraceCapture,
    parse_phase_trace_capture,
)


RECEIPT_SCHEMA: Final = "mineru-capacity-commissioning-receipt.v2"
_MAX_EVIDENCE_BYTES = 512 * 1024 * 1024
_DEFAULT_COLLECTOR = (
    Path(__file__).resolve().parent / "windows" / "collect_mineru_runtime.ps1"
)


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _read_regular(path: Path, *, label: str) -> bytes:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or metadata.st_size > _MAX_EVIDENCE_BYTES
    ):
        raise ValueError(f"{label} must be one bounded regular file")
    payload = path.read_bytes()
    if len(payload) != metadata.st_size:
        raise ValueError(f"{label} changed while reading")
    return payload


def _json_object(payload: bytes, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} root must be an object")
    return value


def _write_new_private(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
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
        try:
            path.unlink(missing_ok=True)
        finally:
            raise


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    for role in ("a1", "b1", "b2", "a2"):
        parser.add_argument(f"--{role}-receipt", type=Path, required=True)
        parser.add_argument(f"--{role}-capture", type=Path, required=True)
    parser.add_argument("--legacy-profile-sha256", required=True)
    parser.add_argument("--candidate-profile-sha256", required=True)
    parser.add_argument("--windows-node-identity-sha256", required=True)
    parser.add_argument(
        "--docker-memory-reserve-bytes",
        type=int,
        required=True,
    )
    parser.add_argument(
        "--minimum-improvement-basis-points",
        type=int,
        required=True,
    )
    parser.add_argument(
        "--maximum-repeat-spread-basis-points",
        type=int,
        required=True,
    )
    parser.add_argument("--collector", type=Path, default=_DEFAULT_COLLECTOR)
    parser.add_argument("--receipt-out", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    if args.receipt_out.exists() or args.receipt_out.is_symlink():
        raise ValueError("commissioning receipt output must be new")
    collector_payload = _read_regular(args.collector, label="collector")
    collector_sha256 = _sha256(collector_payload)
    arms: list[tuple[dict[str, object], MineruPhaseTraceCapture]] = []
    input_evidence: list[dict[str, str]] = []
    for role in ("a1", "b1", "b2", "a2"):
        receipt_path = getattr(args, f"{role}_receipt")
        capture_path = getattr(args, f"{role}_capture")
        receipt_payload = _read_regular(
            receipt_path, label=f"{role} staged-load receipt"
        )
        capture_payload = _read_regular(
            capture_path, label=f"{role} phase-trace capture"
        )
        receipt = _json_object(receipt_payload, label=f"{role} staged-load receipt")
        capture = parse_phase_trace_capture(capture_payload)
        arms.append((receipt, capture))
        input_evidence.extend(
            (
                {
                    "role": f"{role}_staged_load",
                    "sha256": _sha256(receipt_payload),
                },
                {
                    "role": f"{role}_phase_trace",
                    "sha256": _sha256(capture_payload),
                },
            )
        )
    evaluation = evaluate_capacity_commissioning(
        arms,
        expected_legacy_profile_sha256=args.legacy_profile_sha256,
        expected_candidate_profile_sha256=args.candidate_profile_sha256,
        expected_collector_sha256=collector_sha256,
        expected_collector_path=MINERU_WINDOWS_COLLECTOR_PATH,
        expected_docker_memory_reserve_bytes=(
            args.docker_memory_reserve_bytes
        ),
        expected_windows_node_identity_sha256=(
            args.windows_node_identity_sha256
        ),
        minimum_improvement_basis_points=(
            args.minimum_improvement_basis_points
        ),
        maximum_repeat_spread_basis_points=(
            args.maximum_repeat_spread_basis_points
        ),
    )
    receipt = {
        "collector_sha256": collector_sha256,
        "evaluation": evaluation,
        "evaluator": commissioning_evaluator_identity(),
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "input_evidence": input_evidence,
        "schema": RECEIPT_SCHEMA,
    }
    _write_new_private(args.receipt_out, receipt)
    print(
        "mineru-capacity-commissioning: "
        f"{evaluation['decision']} receipt={args.receipt_out}"
    )
    return 0 if evaluation["profile_commissioning_authorized"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
