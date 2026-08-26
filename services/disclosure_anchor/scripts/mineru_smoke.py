"""Run the exact MinerU writer path without PostgreSQL or queue state.

This command is a deployment gate, not a benchmark.  It binds a frozen PDF,
the local client venv, a complete operator/provider runtime manifest, repeated
multimodal canaries, and the official full-PDF provider artifact reader into
one receipt.  Its temporary parse tree is always removed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from disclosure_anchor.adapters.parsers.mineru_medium import (
    MinerUMediumDocumentParser,
    MinerUProcess,
)
from disclosure_anchor.adapters.runtime.mineru_canary import (
    run_mineru_multimodal_canary,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_PROCESSING_WINDOW_SIZE,
    MINERU_SMOKE_INPUT_SHA256,
    MinerUClientIdentity,
    client_bundle_identity,
    verify_runtime_manifest_payload,
    writer_code_digest,
)
from disclosure_anchor.adapters.runtime.mineru_orchestrator import (
    MinerUOrchestratorHealth,
    fetch_mineru_orchestrator_health,
)
from disclosure_anchor.adapters.runtime.mineru_process_isolation import (
    active_disclosure_producers,
    mineru_api_temp_dirs,
    mineru_processes,
    process_snapshot,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.domain.errors import DisclosureAnchorError


RECEIPT_SCHEMA = "mineru_smoke_receipt.v4"
TASK_REGISTRY_SEMANTICS = "retained-terminal-gauges.v1"
DEFAULT_INPUT = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "cninfo"
    / "sample_announcement.pdf"
)
DEFAULT_INPUT_SHA256 = MINERU_SMOKE_INPUT_SHA256.removeprefix("sha256:")
SHA256_RE = re.compile(r"^(?:sha256:)?([a-f0-9]{64})$")
URL_RE = re.compile(r"https?://\S+")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _normalize_sha256(value: str, *, label: str) -> str:
    match = SHA256_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"{label} must be a canonical SHA-256")
    return match.group(1)


def _smoke_orchestrator_evidence(
    before: MinerUOrchestratorHealth,
    after: MinerUOrchestratorHealth,
) -> dict[str, object]:
    if before.active_tasks != 0:
        raise ValueError("MinerU API must be idle before smoke")
    if after.active_tasks != 0:
        raise ValueError("MinerU API must be idle after smoke")
    return {
        "task_registry_semantics": TASK_REGISTRY_SEMANTICS,
        "before": before.as_dict(),
        "after": after.as_dict(),
        "terminal_active_tasks": 0,
        "stop_semantics": "drain-not-cancel.v1",
    }


def _runtime_manifest(
    path: Path,
    *,
    configured_identity: str,
    local_client_identity: MinerUClientIdentity,
    local_processing_window_size: int,
    local_writer_code_digest: str,
) -> tuple[dict[str, Any], str, str]:
    payload = json.loads(path.read_bytes())
    verified = verify_runtime_manifest_payload(
        payload,
        configured_identity=configured_identity,
        local_client_identity=local_client_identity,
        local_processing_window_size=local_processing_window_size,
        local_writer_code_digest=local_writer_code_digest,
    )
    return (
        verified.manifest,
        verified.orchestrator_identity_sha256,
        verified.provider_identity_sha256,
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _receipt_out_from_argv(argv: list[str]) -> Path | None:
    for index, value in enumerate(argv):
        if value == "--receipt-out" and index + 1 < len(argv):
            return Path(argv[index + 1])
        if value.startswith("--receipt-out="):
            return Path(value.split("=", 1)[1])
    return None


def _argument_value(argv: list[str], name: str) -> str | None:
    for index, value in enumerate(argv):
        if value == name and index + 1 < len(argv):
            return argv[index + 1]
        if value.startswith(name + "="):
            return value.split("=", 1)[1]
    return None


def _failure_attempt_evidence(argv: list[str]) -> dict[str, Any]:
    input_value = _argument_value(argv, "--input")
    input_path = Path(input_value) if input_value else DEFAULT_INPUT
    input_evidence: dict[str, Any] = {
        "logical_name": input_path.name,
        "status": "unavailable",
    }
    if not input_path.is_symlink() and input_path.is_file():
        payload = input_path.read_bytes()
        input_evidence = {
            "logical_name": input_path.name,
            "status": "observed",
            "sha256": f"sha256:{_sha256(payload)}",
            "bytes": len(payload),
        }
    manifest_value = _argument_value(argv, "--runtime-manifest")
    manifest_evidence: dict[str, Any] = {"status": "unavailable"}
    if manifest_value is not None:
        manifest_path = Path(manifest_value)
        manifest_evidence["logical_name"] = manifest_path.name
        if not manifest_path.is_symlink() and manifest_path.is_file():
            payload = manifest_path.read_bytes()
            manifest_evidence.update(
                {
                    "status": "observed",
                    "sha256": f"sha256:{_sha256(payload)}",
                    "bytes": len(payload),
                }
            )
    endpoints = {
        "api_endpoint_sha256": (
            _argument_value(argv, "--api-url")
            or os.environ.get("DISCLOSURE_MINERU_API_URL")
        ),
        "observability_endpoint_sha256": (
            _argument_value(argv, "--observability-url")
            or os.environ.get("DISCLOSURE_MINERU_OBSERVABILITY_URL")
        ),
        "inference_upstream_sha256": (
            _argument_value(argv, "--inference-upstream-url")
            or os.environ.get("DISCLOSURE_MINERU_INFERENCE_UPSTREAM_URL")
        ),
    }
    runtime_identity = _argument_value(
        argv, "--runtime-bundle-identity"
    ) or os.environ.get("DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256")
    return {
        "input": input_evidence,
        "runtime_manifest": manifest_evidence,
        "topology": {
            name: _sha256(value.rstrip("/").encode("utf-8")) if value else None
            for name, value in endpoints.items()
        },
        "configured_runtime_bundle_identity_sha256": (
            runtime_identity
            if isinstance(runtime_identity, str)
            and SHA256_RE.fullmatch(runtime_identity)
            else None
        ),
    }


def _write_failure_receipt(
    path: Path,
    failure: BaseException,
    *,
    argv: list[str],
    started_at_utc: str,
) -> None:
    """Best-effort new-only FAIL evidence for an attempted smoke command.

    Argument parsing/output-collision failures may make the requested path
    unavailable; those remain visible on stderr. Once a usable new path was
    supplied, every later failure leaves a durable receipt instead of only a
    transient terminal message. URLs are redacted because endpoints may carry
    private routing details or credentials.
    """

    if path.exists() or path.is_symlink():
        return
    raw_detail = str(failure.code) if isinstance(failure, SystemExit) else str(failure)
    detail = URL_RE.sub("<redacted-url>", raw_detail).strip()[:500]
    _write_json(
        path,
        {
            "schema": RECEIPT_SCHEMA,
            "status": "fail",
            "started_at_utc": started_at_utc,
            "finished_at_utc": datetime.now(UTC).isoformat(),
            "database_access": "none",
            "queue_access": "none",
            "attempt": _failure_attempt_evidence(argv),
            "failure": {
                "error_code": "smoke_aborted",
                "exception_type": type(failure).__name__,
                "detail": detail,
            },
            "cleanup": {
                "status": "not_proved",
                "canary_cache_written": False,
            },
        },
    )


def run_cli(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    started_at_utc = datetime.now(UTC).isoformat()
    try:
        return main(values)
    except BaseException as exc:
        receipt_out = _receipt_out_from_argv(values)
        is_operational_failure = not isinstance(exc, SystemExit) or isinstance(
            exc.code, str
        )
        if receipt_out is not None and is_operational_failure:
            try:
                _write_failure_receipt(
                    receipt_out,
                    exc,
                    argv=values,
                    started_at_utc=started_at_utc,
                )
            except (OSError, ValueError):
                # Preserve the original failure; an unwritable receipt path is
                # already an operator-visible condition.
                pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mineru_smoke", description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--expected-input-sha256")
    parser.add_argument("--mineru-bin", type=Path)
    parser.add_argument("--api-url")
    parser.add_argument("--observability-url")
    parser.add_argument("--inference-upstream-url")
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--runtime-bundle-identity")
    parser.add_argument("--receipt-out", type=Path, required=True)
    parser.add_argument("--canary-cache-out", type=Path, required=True)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--canary-attempts", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args(argv)

    mineru_bin = args.mineru_bin or (
        Path(value) if (value := os.environ.get("DISCLOSURE_MINERU_BIN")) else None
    )
    api_url = args.api_url or os.environ.get("DISCLOSURE_MINERU_API_URL")
    observability_url = args.observability_url or os.environ.get(
        "DISCLOSURE_MINERU_OBSERVABILITY_URL"
    )
    inference_upstream_url = args.inference_upstream_url or os.environ.get(
        "DISCLOSURE_MINERU_INFERENCE_UPSTREAM_URL"
    )
    runtime_identity = args.runtime_bundle_identity or os.environ.get(
        "DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256"
    )
    if mineru_bin is None or not mineru_bin.is_file():
        raise SystemExit("[abort] DISCLOSURE_MINERU_BIN is missing or not a file")
    if not api_url or not observability_url or not inference_upstream_url:
        raise SystemExit("[abort] complete MinerU fixed-API topology is required")
    if runtime_identity is None or SHA256_RE.fullmatch(runtime_identity) is None:
        raise SystemExit("[abort] runtime bundle identity is missing or invalid")
    if not args.input.is_file():
        raise SystemExit(f"[abort] smoke input is missing: {args.input}")
    if args.canary_attempts < 1:
        raise SystemExit("[abort] canary-attempts must be positive")
    if args.timeout_seconds < 1:
        raise SystemExit("[abort] timeout-seconds must be positive")
    if args.work_root is not None and not args.work_root.is_dir():
        raise SystemExit(f"[abort] work-root is not a directory: {args.work_root}")
    if args.receipt_out.resolve(strict=False) == args.canary_cache_out.resolve(
        strict=False
    ):
        raise SystemExit("[abort] receipt and canary cache paths must differ")
    for output in (args.receipt_out, args.canary_cache_out):
        if output.exists() or output.is_symlink():
            raise SystemExit(f"[abort] output already exists; stale evidence: {output}")

    input_bytes = args.input.read_bytes()
    input_sha256 = _sha256(input_bytes)
    expected_input = args.expected_input_sha256
    if expected_input is None and args.input.resolve() == DEFAULT_INPUT.resolve():
        expected_input = DEFAULT_INPUT_SHA256
    if expected_input is None:
        raise SystemExit("[abort] custom smoke input requires --expected-input-sha256")
    if _normalize_sha256(expected_input, label="expected input") != input_sha256:
        raise SystemExit("[abort] smoke input SHA-256 does not match the frozen value")

    processing_window_raw = os.environ.get("MINERU_PROCESSING_WINDOW_SIZE")
    if processing_window_raw != str(MINERU_PROCESSING_WINDOW_SIZE):
        raise SystemExit(
            "[abort] MINERU_PROCESSING_WINDOW_SIZE must be pinned to "
            f"{MINERU_PROCESSING_WINDOW_SIZE}"
        )
    try:
        before_processes = process_snapshot()
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        raise SystemExit(
            f"[abort] MinerU process isolation check failed: {exc}"
        ) from exc
    if producers := active_disclosure_producers(before_processes):
        raise SystemExit(
            f"[abort] disclosure producer processes are active: {sorted(producers)}"
        )
    if mineru_before := mineru_processes(before_processes):
        raise SystemExit(
            f"[abort] pre-existing MinerU processes require cleanup: "
            f"{sorted(mineru_before)}"
        )
    api_temp_before = mineru_api_temp_dirs()

    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat()
    local_client_identity = client_bundle_identity(mineru_bin)
    local_digest = local_client_identity.package_set_sha256
    code_digest = writer_code_digest()
    try:
        runtime_manifest, orchestrator_identity, remote_identity = _runtime_manifest(
            args.runtime_manifest,
            configured_identity=runtime_identity,
            local_client_identity=local_client_identity,
            local_processing_window_size=MINERU_PROCESSING_WINDOW_SIZE,
            local_writer_code_digest=code_digest,
        )
        canary = run_mineru_multimodal_canary(
            observability_url,
            attempts=args.canary_attempts,
            expected_model_id=str(
                runtime_manifest["inference_server"]["served_model_id"]
            ),
        )
    except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        raise SystemExit(
            f"[abort] MinerU bootstrap identity/canary failed: {exc}"
        ) from exc

    api_before = fetch_mineru_orchestrator_health(
        api_url,
        expected_task_slots=int(
            runtime_manifest["orchestrator"]["max_concurrent_requests"]
        ),
        expected_task_retention_seconds=int(
            runtime_manifest["orchestrator"]["task_retention_seconds"]
        ),
        expected_cleanup_interval_seconds=int(
            runtime_manifest["orchestrator"]["task_cleanup_interval_seconds"]
        ),
    )
    if api_before.active_tasks != 0:
        raise SystemExit("[abort] MinerU API must be idle before smoke")

    options = ParserOptions(
        timeout_seconds=args.timeout_seconds,
        api_url=api_url,
        server_url=inference_upstream_url,
        http_request_concurrency=None,
        runtime_bundle_identity_sha256=runtime_identity,
    )
    smoke_path: Path | None = None
    parse_failure: DisclosureAnchorError | OSError | ValueError | None = None
    provider_evidence: dict[str, Any] | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="disclosure-mineru-smoke-",
            dir=args.work_root,
        ) as tmp:
            smoke_path = Path(tmp)
            private_tmp = smoke_path / "tmp"
            private_tmp.mkdir()
            process = MinerUProcess(
                executable=mineru_bin,
                extra_env={
                    "TEMP": str(private_tmp),
                    "TMP": str(private_tmp),
                    "TMPDIR": str(private_tmp),
                },
            )
            document_parser = MinerUMediumDocumentParser(
                process=process,
                api_url=api_url,
                server_url=inference_upstream_url,
            )
            source = smoke_path / f"sha256_{input_sha256}.pdf"
            shutil.copyfile(args.input, source)
            result = document_parser.parse(
                input_pdf=source,
                output_dir=smoke_path / "output",
                options=options,
                source_pdf_sha256=f"sha256:{input_sha256}",
            )
            provider_document = result.provider_document
            if not provider_document.pages:
                raise ValueError("provider smoke returned no pages")
            if provider_document.parser_version != "3.4.4":
                raise ValueError("provider smoke parser version drifted")
            if (
                provider_document.backend != "hybrid"
                or provider_document.effort != "medium"
            ):
                raise ValueError("provider smoke target drifted from Hybrid-medium")
            provider_evidence = {
                "target_identity": result.target_identity.to_payload(),
                "provider_bundle_sha256": provider_document.bundle_sha256,
                "page_count": len(provider_document.pages),
                "block_count": len(provider_document.blocks),
                "artifact_count": len(provider_document.artifacts),
            }
    except (DisclosureAnchorError, OSError, ValueError) as exc:
        parse_failure = exc
    cleanup_proved = smoke_path is not None and not smoke_path.exists()
    if not cleanup_proved:
        raise SystemExit("[abort] MinerU smoke temporary tree was not removed")
    cleanup_deadline = time.monotonic() + 5
    new_mineru_processes: dict[int, str] = {}
    while True:
        try:
            new_mineru_processes = mineru_processes(process_snapshot())
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            raise SystemExit(
                f"[abort] MinerU cleanup process check failed: {exc}"
            ) from exc
        if not new_mineru_processes or time.monotonic() >= cleanup_deadline:
            break
        time.sleep(0.25)
    new_api_temp_dirs = mineru_api_temp_dirs() - api_temp_before
    if new_mineru_processes or new_api_temp_dirs:
        raise SystemExit(
            "[abort] MinerU smoke left external processes or temp directories: "
            f"pids={sorted(new_mineru_processes)} temp_dirs={len(new_api_temp_dirs)}"
        )
    if parse_failure is not None:
        raise SystemExit(
            f"[abort] MinerU DB-free PDF smoke failed after cleanup: {parse_failure}"
        ) from parse_failure
    if provider_evidence is None:
        raise SystemExit("[abort] MinerU provider evidence was not produced")

    api_after = fetch_mineru_orchestrator_health(
        api_url,
        expected_task_slots=api_before.max_concurrent_requests,
        expected_task_retention_seconds=api_before.task_retention_seconds,
        expected_cleanup_interval_seconds=api_before.task_cleanup_interval_seconds,
    )
    try:
        orchestrator_evidence = _smoke_orchestrator_evidence(api_before, api_after)
    except ValueError as exc:
        raise SystemExit(f"[abort] {exc}") from exc

    canary_cache = canary.cache_payload(
        observability_url=observability_url,
        runtime_bundle_identity_sha256=runtime_identity,
    )
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": "pass",
        "started_at_utc": started_at,
        "finished_at_utc": datetime.now(UTC).isoformat(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "database_access": "none",
        "queue_access": "none",
        "input": {
            "profile": (
                "deployment_frozen_v1"
                if args.input.resolve() == DEFAULT_INPUT.resolve()
                else "diagnostic_custom"
            ),
            "logical_name": args.input.name,
            "sha256": f"sha256:{input_sha256}",
            "bytes": len(input_bytes),
        },
        "identity": {
            "local_client_identity_sha256": local_digest,
            "local_content_package_versions": dict(
                local_client_identity.content_package_versions
            ),
            "local_processing_window_size": MINERU_PROCESSING_WINDOW_SIZE,
            "local_writer_code_sha256": code_digest,
            "runtime_manifest_identity_sha256": runtime_identity,
            "orchestrator_runtime_identity_sha256": orchestrator_identity,
            "provider_runtime_identity_sha256": remote_identity,
            "served_model_id": canary.model_id,
            "orchestrator_task_slots": api_before.max_concurrent_requests,
        },
        "topology": {
            "api_endpoint_sha256": "sha256:"
            + _sha256(api_url.rstrip("/").encode("utf-8")),
            "observability_endpoint_sha256": "sha256:"
            + _sha256(observability_url.rstrip("/").encode("utf-8")),
            "inference_upstream_sha256": "sha256:"
            + _sha256(inference_upstream_url.rstrip("/").encode("utf-8")),
        },
        "orchestrator": orchestrator_evidence,
        "runtime_manifest": runtime_manifest,
        "canary": canary_cache,
        "provider": provider_evidence,
        "cleanup": {
            "external_api_temp_dirs_created": 0,
            "external_mineru_processes_after": 0,
            "temporary_tree_removed": True,
            "retained_parse_artifacts": 0,
            "remote_active_tasks_after": 0,
        },
    }
    _write_json(args.canary_cache_out, canary_cache)
    _write_json(args.receipt_out, receipt)
    print(
        "mineru-smoke: PASS "
        f"pages={provider_evidence['page_count']} "
        f"blocks={provider_evidence['block_count']} "
        f"bundle={provider_evidence['provider_bundle_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
