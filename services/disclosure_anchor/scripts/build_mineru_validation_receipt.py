"""Build one fail-closed held-out MinerU validation receipt.

The inputs are two to eight independently produced ``mineru_smoke_receipt.v5``
receipts for complete, operator-selected PDFs that are not the repository smoke
fixture.  This command performs no parse and touches no database or queue.  It
only seals the already-produced receipts into one bounded, new-only artifact
for resident parse admission.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
    ParserTargetIdentityError,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads


RECEIPT_SCHEMA = "mineru_heldout_validation_receipt.v1"
SMOKE_SCHEMA = "mineru_smoke_receipt.v5"
POLICY = "operator-held-out-complete-pdf.v1"
MIN_DOCUMENTS = 2
MAX_DOCUMENTS = 8
MAX_INPUT_BYTES = 2 * 1024 * 1024


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_sha256(payload: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _load(path: Path) -> tuple[dict[str, Any], str]:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or not 0 < before.st_size <= MAX_INPUT_BYTES
        ):
            raise ValueError(f"held-out evidence is not owner-only/bounded: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            encoded = handle.read(MAX_INPUT_BYTES + 1)
        after = os.fstat(descriptor)
        if len(encoded) != before.st_size or (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise ValueError(f"held-out evidence changed while reading: {path}")
        value = strict_json_loads(encoded)
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"held-out evidence cannot be read: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise ValueError(f"held-out smoke receipt root is not an object: {path}")
    return value, "sha256:" + hashlib.sha256(encoded).hexdigest()


def _positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _validate_smoke(payload: dict[str, Any]) -> tuple[str, str]:
    if payload.get("schema") != SMOKE_SCHEMA or payload.get("status") != "pass":
        raise ValueError("held-out smoke receipt is not v5 PASS")
    if payload.get("database_access") != "none" or payload.get("queue_access") != "none":
        raise ValueError("held-out smoke receipt was not DB/queue free")
    input_evidence = payload.get("input")
    provider = payload.get("provider")
    if not isinstance(input_evidence, dict) or not isinstance(provider, dict):
        raise ValueError("held-out smoke input/provider evidence is invalid")
    if input_evidence.get("profile") != "diagnostic_custom":
        raise ValueError("held-out validation cannot reuse the repository smoke fixture")
    source_pages = _positive_int(
        input_evidence.get("page_count"), label="held-out source page_count"
    )
    provider_pages = _positive_int(
        provider.get("page_count"), label="held-out provider page_count"
    )
    if source_pages < 2 or provider_pages != source_pages:
        raise ValueError("held-out smoke did not preserve a multi-page complete PDF")
    try:
        target = ParserTargetIdentity.from_payload(provider.get("target_identity"))
    except ParserTargetIdentityError as exc:
        raise ValueError("held-out parser target identity is invalid") from exc
    if not target.full_pdf or target.start_page is not None or target.end_page is not None:
        raise ValueError("held-out smoke was not a complete-PDF parse")
    source_sha256 = input_evidence.get("sha256")
    if not _sha256(source_sha256):
        raise ValueError("held-out source identity is invalid")
    runtime_identity = payload.get("identity")
    if not isinstance(runtime_identity, dict):
        raise ValueError("held-out runtime identity is invalid")
    manifest_identity = runtime_identity.get("runtime_manifest_identity_sha256")
    if not _sha256(manifest_identity):
        raise ValueError("held-out runtime manifest identity is missing")
    return str(source_sha256), str(manifest_identity)


def _sha256(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(item in "0123456789abcdef" for item in digest)


def _utc(value: object, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} is not timezone-aware")
    return parsed.astimezone(UTC)


def _validate_epoch(
    payload: dict[str, Any], *, runtime_identity: str
) -> tuple[str, datetime]:
    if set(payload) != {
        "schema",
        "status",
        "created_at_utc",
        "database_access",
        "queue_access",
        "service_epoch",
        "service_epoch_sha256",
        "safety",
    }:
        raise ValueError("service epoch receipt fields drifted")
    epoch = payload.get("service_epoch")
    safety = payload.get("safety")
    expected_safety = {
        "restart_count_total": 0,
        "oom_killed_count": 0,
        "unsafe_container_count": 0,
        "cgroup_oom_total": 0,
        "cgroup_oom_kill_total": 0,
    }
    if (
        payload.get("schema") != "mineru-service-epoch-freeze.v2"
        or payload.get("status") != "pass"
        or payload.get("database_access") != "none"
        or payload.get("queue_access") != "none"
        or not isinstance(epoch, dict)
        or set(epoch)
        != {
            "schema",
            "runtime_manifest_identity_sha256",
            "collector_sha256",
            "windows_node_identity_sha256",
            "windows_compose_sha256",
            "writer_code_sha256",
            "api_image_digest",
            "container_epoch_sha256",
            "api_container_id",
        }
        or epoch.get("schema") != "mineru-service-epoch.v1"
        or epoch.get("runtime_manifest_identity_sha256") != runtime_identity
        or payload.get("service_epoch_sha256") != _canonical_sha256(epoch)
        or safety != expected_safety
    ):
        raise ValueError("service epoch receipt is not clean PASS")
    return str(payload["service_epoch_sha256"]), _utc(
        payload.get("created_at_utc"), label="service epoch created_at"
    )


def build_receipt(
    paths: list[Path],
    *,
    epoch_before_path: Path,
    epoch_after_path: Path,
) -> dict[str, Any]:
    if not MIN_DOCUMENTS <= len(paths) <= MAX_DOCUMENTS:
        raise ValueError(
            f"held-out validation requires {MIN_DOCUMENTS}..{MAX_DOCUMENTS} receipts"
        )
    loaded = [_load(path) for path in paths]
    payloads = [item[0] for item in loaded]
    identities = [_validate_smoke(payload) for payload in payloads]
    source_identities = {value[0] for value in identities}
    if len(source_identities) != len(payloads):
        raise ValueError("held-out validation source PDFs must be distinct")
    if len({value[1] for value in identities}) != 1:
        raise ValueError("held-out validation runtime manifest identity drifted")
    runtime_identity = identities[0][1]
    first = payloads[0]
    for payload in payloads[1:]:
        if (
            payload.get("identity") != first.get("identity")
            or payload.get("topology") != first.get("topology")
            or payload.get("runtime_manifest") != first.get("runtime_manifest")
        ):
            raise ValueError("held-out validation receipts cross a runtime/epoch boundary")
    epoch_before, epoch_before_raw_sha = _load(epoch_before_path)
    epoch_after, epoch_after_raw_sha = _load(epoch_after_path)
    before_identity, before_time = _validate_epoch(
        epoch_before, runtime_identity=runtime_identity
    )
    after_identity, after_time = _validate_epoch(
        epoch_after, runtime_identity=runtime_identity
    )
    starts = [_utc(item.get("started_at_utc"), label="smoke start") for item in payloads]
    finishes = [
        _utc(item.get("finished_at_utc"), label="smoke finish") for item in payloads
    ]
    if any(start >= finish for start, finish in zip(starts, finishes, strict=True)):
        raise ValueError("held-out smoke timeline is invalid")
    if before_identity != after_identity:
        raise ValueError("MinerU service epoch changed across held-out validation")
    if not before_time <= min(starts) < max(finishes) <= after_time:
        raise ValueError("service epoch receipts do not bracket held-out validation")
    return {
        "schema": RECEIPT_SCHEMA,
        "status": "pass",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "policy": POLICY,
        "database_access": "none",
        "queue_access": "none",
        "document_count": len(payloads),
        "epoch_before": {
            "receipt_sha256": _canonical_sha256(epoch_before),
            "source_bytes_sha256": epoch_before_raw_sha,
            "receipt": epoch_before,
        },
        "epoch_after": {
            "receipt_sha256": _canonical_sha256(epoch_after),
            "source_bytes_sha256": epoch_after_raw_sha,
            "receipt": epoch_after,
        },
        "documents": [
            {
                "receipt_sha256": _canonical_sha256(payload),
                "source_bytes_sha256": loaded[index][1],
                "receipt": payload,
            }
            for index, payload in enumerate(payloads)
        ],
    }


def _write_new(path: Path, payload: object) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError(f"output already exists; stale evidence: {path}")
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
        parent_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-receipt", action="append", type=Path, required=True)
    parser.add_argument("--epoch-before", type=Path, required=True)
    parser.add_argument("--epoch-after", type=Path, required=True)
    parser.add_argument("--receipt-out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = build_receipt(
            args.smoke_receipt,
            epoch_before_path=args.epoch_before,
            epoch_after_path=args.epoch_after,
        )
        _write_new(args.receipt_out, receipt)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[abort] {exc}") from exc
    print(
        "mineru-heldout-validation: PASS "
        f"documents={receipt['document_count']} receipt={args.receipt_out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
