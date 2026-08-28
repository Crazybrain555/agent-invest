#!/usr/bin/env python3
"""Build one immutable Auto-capacity catalog from a COMMISSION receipt."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Final

from disclosure_anchor.adapters.runtime.mineru_capacity_evaluator_identity import (
    commissioning_evaluator_identity,
)
from disclosure_anchor.adapters.runtime.mineru_capacity_commissioning import (
    CAPACITY_COMMISSIONING_FIELDS,
    CAPACITY_COMMISSIONING_SCHEMA,
)


CATALOG_SCHEMA: Final = "mineru-capacity-catalog.v1"
PROFILE_SCHEMA: Final = "mineru-execution-profile.v2"
COMMISSIONING_RECEIPT_SCHEMA: Final = "mineru-capacity-commissioning-receipt.v2"
PROFILE_FIELDS: Final = {
    "inner_inference_concurrency",
    "max_document_pages",
    "max_resident_pages",
    "max_source_pdf_bytes",
    "min_document_pages",
    "pipeline_depth",
    "profile_id",
    "schema",
    "vllm_max_num_seqs",
    "window_size",
}
COMMISSIONING_RECEIPT_FIELDS: Final = {
    "collector_sha256",
    "evaluation",
    "evaluator",
    "generated_at_utc",
    "input_evidence",
    "schema",
}
INPUT_EVIDENCE_ROLES: Final = tuple(
    f"{arm}_{kind}"
    for arm in ("a1", "b1", "b2", "a2")
    for kind in ("staged_load", "phase_trace")
)
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_MAX_INPUT_BYTES = 64 * 1024 * 1024
_CURRENT_COLLECTOR = (
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
        or metadata.st_size > _MAX_INPUT_BYTES
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


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _required_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is not a SHA-256 identity")
    return value


def build_capacity_catalog(
    *,
    commissioning_receipt_bytes: bytes,
    profile_bytes: bytes,
    runtime_compatibility_sha256: str,
) -> dict[str, str]:
    receipt = _json_object(
        commissioning_receipt_bytes,
        label="commissioning receipt",
    )
    profile = _json_object(profile_bytes, label="capacity profile")
    evaluation = receipt.get("evaluation")
    if (
        set(receipt) != COMMISSIONING_RECEIPT_FIELDS
        or
        receipt.get("schema") != COMMISSIONING_RECEIPT_SCHEMA
        or not isinstance(evaluation, dict)
        or set(evaluation) != CAPACITY_COMMISSIONING_FIELDS
        or evaluation.get("schema") != CAPACITY_COMMISSIONING_SCHEMA
        or evaluation.get("decision") != "COMMISSION"
        or evaluation.get("profile_commissioning_authorized") is not True
        or evaluation.get("findings") != []
        or evaluation.get("arm_modes")
        != ["legacy", "candidate", "candidate", "legacy"]
    ):
        raise ValueError("capacity commissioning receipt does not authorize Auto")
    if commissioning_receipt_bytes != _canonical(receipt) + b"\n":
        raise ValueError("capacity commissioning receipt is not exact canonical JSON")
    if set(profile) != PROFILE_FIELDS or profile.get("schema") != PROFILE_SCHEMA:
        raise ValueError("capacity profile contract drifted")
    if profile_bytes != _canonical(profile):
        raise ValueError("capacity profile is not exact canonical JSON")
    profile_id = profile.get("profile_id")
    if not isinstance(profile_id, str) or not profile_id:
        raise ValueError("capacity profile identity is invalid")
    profile_sha256 = _sha256(_canonical(profile))
    if evaluation.get("candidate_profile_sha256") != profile_sha256:
        raise ValueError("commissioning receipt does not bind the exact profile")
    evaluator = receipt.get("evaluator")
    current_evaluator = commissioning_evaluator_identity()
    if evaluator != current_evaluator:
        raise ValueError("capacity commissioning evaluator bundle drifted")
    evaluator_sha256 = _required_sha256(
        current_evaluator.get("bundle_sha256"), label="commissioning evaluator"
    )
    collector_sha256 = _required_sha256(
        receipt.get("collector_sha256"), label="commissioning collector"
    )
    if evaluation.get("collector_sha256") != collector_sha256:
        raise ValueError("capacity commissioning collector identity drifted")
    current_collector_sha256 = _sha256(
        _read_regular(_CURRENT_COLLECTOR, label="current commissioning collector")
    )
    if collector_sha256 != current_collector_sha256:
        raise ValueError("capacity commissioning collector is not current")
    generated_at = receipt.get("generated_at_utc")
    if not isinstance(generated_at, str):
        raise ValueError("capacity commissioning timestamp is invalid")
    try:
        parsed_generated_at = datetime.fromisoformat(
            generated_at.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("capacity commissioning timestamp is invalid") from exc
    if (
        parsed_generated_at.tzinfo is None
        or parsed_generated_at.utcoffset()
        != timezone.utc.utcoffset(parsed_generated_at)
    ):
        raise ValueError("capacity commissioning timestamp is not UTC")
    input_evidence = receipt.get("input_evidence")
    if not isinstance(input_evidence, list) or len(input_evidence) != len(
        INPUT_EVIDENCE_ROLES
    ):
        raise ValueError("capacity commissioning input evidence is incomplete")
    for item, expected_role in zip(
        input_evidence, INPUT_EVIDENCE_ROLES, strict=True
    ):
        if (
            not isinstance(item, dict)
            or set(item) != {"role", "sha256"}
            or item.get("role") != expected_role
        ):
            raise ValueError("capacity commissioning input evidence drifted")
        _required_sha256(
            item.get("sha256"), label=f"commissioning input {expected_role}"
        )
    runtime_sha256 = _required_sha256(
        runtime_compatibility_sha256,
        label="runtime compatibility",
    )
    return {
        "commissioning_evaluator_sha256": evaluator_sha256,
        "commissioning_receipt_sha256": _sha256(commissioning_receipt_bytes),
        "profile_id": profile_id,
        "profile_sha256": profile_sha256,
        "runtime_compatibility_sha256": runtime_sha256,
        "schema": CATALOG_SCHEMA,
    }


def _write_new_private(path: Path, payload: object) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _canonical(payload)
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
    return encoded


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commissioning-receipt", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--runtime-compatibility-sha256", required=True)
    parser.add_argument("--catalog-out", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    if args.catalog_out.exists() or args.catalog_out.is_symlink():
        raise ValueError("capacity catalog output must be new")
    receipt = _read_regular(
        args.commissioning_receipt,
        label="commissioning receipt",
    )
    profile = _read_regular(args.profile, label="capacity profile")
    catalog = build_capacity_catalog(
        commissioning_receipt_bytes=receipt,
        profile_bytes=profile,
        runtime_compatibility_sha256=args.runtime_compatibility_sha256,
    )
    encoded = _write_new_private(args.catalog_out, catalog)
    print(
        "mineru-capacity-catalog: BUILT "
        f"sha256={_sha256(encoded)} catalog={args.catalog_out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
