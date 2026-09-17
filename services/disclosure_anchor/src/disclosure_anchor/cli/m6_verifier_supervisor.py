"""Ongoing public-then-quality verification for an owner-bound e2e campaign, with one terminal drain.

Runs beside ``staged_campaign --m6-run-dir`` for the whole run: it tails the
runner's fsynced spool, verifies each attempt as soon as the owner has
acknowledged the runner's admission and publication facts for it, appends
``public_confirmation`` and ``document_qualified`` through one long-lived
sender per role, and after the runner's campaign receipt for this exact run
reports a complete assembly and the attempt set reconciles, completes the
quality sender and lets the public sender declare the single
``verifier_drained``. Any failure, timeout or mismatch closes both senders
visibly without a drain claim; the first failure stays the reported error and
cleanup failures are recorded beside it.

The quality step binds the exact public consumer receipt bytes produced in
this process; the same receipts are indexed in ``public-inputs.json`` so an
independent ``m6_qualify_readonly --public-receipts`` re-run can bind them.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from disclosure_anchor.adapters.db.postgres.connection import app_database_url, create_db_engine, reader_database_url
from disclosure_anchor.adapters.parsers.pdf_text_observation import observe_pdf_text_rectangles
from disclosure_anchor.adapters.runtime.exact_file_write import write_new_exact
from disclosure_anchor.adapters.runtime.m6_continuous_clock import diagnostic_continuous_clock
from disclosure_anchor.adapters.runtime.m6_e2e_run import M6RunDirectory, M6VerifierAssembly, load_m6_run_directory
from disclosure_anchor.adapters.runtime.m6_qualification_verifier import M6PublicReceiptInput
from disclosure_anchor.adapters.runtime.stage_observation import mac_stage_clock_binding
from disclosure_anchor.adapters.runtime.m6_verifier_supervisor import (
    PublicOutcome, QualityOutcome, ReadyAttempt, RunnerSpoolTail, SupervisorRefused, VerifierSupervisor,
    write_receipt_file,
)
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.provider_document_source import ProviderDocumentFileSource
from disclosure_anchor.application.contracts.m6_document_qualification import M6QualityPlan
from disclosure_anchor.application.contracts.m6_run import M6RunSpec
from disclosure_anchor.application.contracts.m6_run_events import M6AttemptAdmitted
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.services.semantic_taxonomy import load_semantic_route_taxonomy
from disclosure_anchor.application.services.staged_campaign_runner import CAMPAIGN_RECEIPT_CONTRACT
from disclosure_anchor.cli.m6_public_verify import PublicationUnavailable, confirm_attempt
from disclosure_anchor.cli.m6_qualify_readonly import PUBLIC_INPUTS_CONTRACT_VERSION, qualify_attempt
from disclosure_anchor.settings import load_settings

SUMMARY_CONTRACT = "m6.verifier-supervisor-summary.v1"
_MAX_PLAN_BYTES = 1024 * 1024
_MAX_RECEIPT_BYTES = 16 * 1024 * 1024


class RunnerReceiptError(ValueError):
    """The file at the runner's receipt path is not this run's complete campaign receipt."""


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _write_new(path: Path, payload: bytes) -> None:
    write_new_exact(path, payload)


def _read_bounded(path: Path, *, maximum: int) -> bytes:
    """Read a whole small file without allocating beyond its declared bound."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        size = os.fstat(fd).st_size
        if size > maximum:
            raise ValueError(f"{path} exceeds the {maximum}-byte bound")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    if len(raw) > maximum:
        raise ValueError(f"{path} grew beyond the {maximum}-byte bound while being read")
    return raw


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--m6-run-dir", type=Path, required=True)
    parser.add_argument("--runner-spool", type=Path, required=True,
                        help="the runner's spool.jsonl (<observation-out>/m6-assembly/spool.jsonl)")
    parser.add_argument("--runner-receipt", type=Path, required=True,
                        help="the runner's --receipt-out path; its appearance ends the supply")
    parser.add_argument("--output-dir", type=Path, required=True, help="new directory; must not exist")
    parser.add_argument("--verifier-identity", required=True)
    parser.add_argument("--plan", type=Path, required=True, help="frozen e2e M6QualityPlan canonical bytes")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--deadline-seconds", type=float, required=True,
                        help="whole supervision bound from start; exceeding it aborts both senders visibly")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-receipt-bytes", type=int, default=32 * 1024 ** 2)
    parser.add_argument("--max-artifact-bytes", type=int, default=2 * 1024 ** 3)
    parser.add_argument("--max-artifact-files", type=int, default=200_000)
    return parser


def read_runner_receipt(path: Path, *, run: M6RunDirectory, spec: M6RunSpec) -> dict[str, Any] | None:
    """The runner's campaign receipt for exactly this run, or None while it has not appeared.

    The receipt is published atomically by the runner, so a present name means
    complete bytes: anything unreadable, malformed or bound to another campaign,
    manifest, scope, run, spec or runner incarnation is a visible failure, never
    something to wait out or ignore.
    """
    if not path.is_file():
        return None
    raw = _read_bounded(path, maximum=_MAX_RECEIPT_BYTES)
    try:
        value = strict_json_loads(raw)
    except Exception as exc:  # noqa: BLE001 - reported as the receipt failure, never ignored
        raise RunnerReceiptError(f"runner receipt is not strict JSON: {type(exc).__name__}") from exc
    if not isinstance(value, dict) or value.get("contract_version") != CAMPAIGN_RECEIPT_CONTRACT:
        raise RunnerReceiptError("runner receipt is not a staged campaign receipt")
    assembly = value.get("m6_assembly")
    if not isinstance(assembly, dict):
        raise RunnerReceiptError("runner receipt carries no owner assembly record")
    bindings = {
        "campaign_id": (value.get("campaign_id"), spec.campaign_id),
        "manifest_sha256": (value.get("manifest_sha256"), spec.manifest_sha256),
        "scope_sha256": (value.get("scope_sha256"), spec.scope_sha256),
        "m6_assembly.run_id": (assembly.get("run_id"), spec.run_id),
        "m6_assembly.spec_sha256": (assembly.get("spec_sha256"), spec.canonical_sha256()),
        "m6_assembly.anchor_sha256": (assembly.get("anchor_sha256"), run.anchor.canonical_sha256()),
        "m6_assembly.producer_kind": (assembly.get("producer_kind"), "e2e_runner"),
        "m6_assembly.producer_epoch_sha256": (assembly.get("producer_epoch_sha256"), run.epoch("e2e_runner")),
    }
    differing = sorted(name for name, (observed, expected) in bindings.items() if observed != expected)
    if differing:
        raise RunnerReceiptError("runner receipt belongs to another run: " + ", ".join(differing))
    if assembly.get("status") not in {"complete", "failed"}:
        raise RunnerReceiptError("runner receipt assembly status is not closed")
    return {"sha256": _sha256(raw), "status": assembly["status"], "campaign_id": value["campaign_id"],
            "stop_reason": value.get("stop_reason"), "clean": value.get("clean")}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.poll_seconds <= 0 or args.deadline_seconds <= 0:
        raise ValueError("poll and deadline seconds must be positive")
    plan = M6QualityPlan.from_canonical_bytes(_read_bounded(args.plan, maximum=_MAX_PLAN_BYTES),
                                              maximum_bytes=_MAX_PLAN_BYTES)
    if plan.mode != "e2e_publication":
        raise ValueError("supervisor requires the e2e_publication quality plan")
    run = load_m6_run_directory(args.m6_run_dir.absolute())
    spec = run.require_spec()
    if spec.mode != "e2e_publication":
        raise ValueError("supervisor composes e2e publication verification only")
    if spec.quality_plan_sha256 != plan.canonical_sha256():
        raise ValueError("quality plan differs from the frozen run spec")
    args.output_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    for role_dir in ("public", "quality"):
        (args.output_dir / role_dir).mkdir(mode=0o700)
    started = time.monotonic()
    clock = diagnostic_continuous_clock().now_ns
    summary: dict[str, Any] = {
        "clock": mac_stage_clock_binding(),
        "contract_version": SUMMARY_CONTRACT, "started_utc": datetime.now(UTC).isoformat(),
        "verifier_identity": args.verifier_identity, "plan_sha256": plan.canonical_sha256(),
        "m6_run": {"run_id": spec.run_id, "spec_sha256": spec.canonical_sha256(), "pins": run.pins},
        "runner_spool": str(args.runner_spool.absolute()), "runner_receipt_path": str(args.runner_receipt.absolute()),
        "status": "failed", "cleanup_errors": [],
    }
    max_attempts = spec.resources.max_attempts
    assemblies: dict[str, M6VerifierAssembly] = {}
    closed_by_supervisor = False
    public_inputs: dict[str, dict[str, str]] = {}
    engines: list[Any] = []
    exit_code = 1

    def abort_open_senders(reason: str) -> None:
        """Abort every sender still owned here; each attempt is guarded so both happen and the reason stays visible."""
        if closed_by_supervisor:
            return
        summary["abort_reason"] = reason
        for role, assembly in assemblies.items():
            try:
                summary[role] = assembly.abort(reason)
            except Exception as exc:  # noqa: BLE001 - recorded beside the primary failure, never in its place
                summary["cleanup_errors"].append(f"{role} abort failed: {type(exc).__name__}:{exc}"[:300])

    try:
        for role in ("public_verifier", "quality_verifier"):
            assemblies[role.split("_")[0]] = M6VerifierAssembly(
                run, role=role, spool_dir=args.output_dir / f"m6-assembly-{role.split('_')[0]}",
                max_events=max_attempts + 2, continuous_ns=clock,
            )
        for assembly in assemblies.values():
            assembly.start()
        public, quality = assemblies["public"], assemblies["quality"]

        settings = load_settings()
        paths = FileStorePathBuilder(settings)
        source = ProviderDocumentFileSource(paths, text_reader=observe_pdf_text_rectangles)
        taxonomy = load_semantic_route_taxonomy()
        app_engine = create_db_engine(app_database_url(settings))
        engines.append(app_engine)
        reader_engine = create_db_engine(reader_database_url(settings))
        engines.append(reader_engine)

        def confirm(item: ReadyAttempt) -> PublicOutcome:
            files: list[str] = []
            try:
                result = confirm_attempt(
                    app_engine=app_engine, reader_engine=reader_engine, paths=paths, attempt_id=item.attempt_id,
                    attempt_dir=args.output_dir / "public" / item.attempt_id, verifier_identity=args.verifier_identity,
                    page_size=args.page_size, max_receipt_bytes=args.max_receipt_bytes,
                    max_artifact_bytes=args.max_artifact_bytes, max_artifact_files=args.max_artifact_files, files=files,
                )
            except PublicationUnavailable as exc:
                return PublicOutcome(None, None, f"publication_missing:{exc}"[:300], tuple(files))
            receipt = M6PublicReceiptInput(receipt=result.receipt, receipt_sha256=result.receipt_sha256,
                                           expected_verifier_identity=args.verifier_identity)
            public_inputs[item.attempt_id] = {
                "path": str(args.output_dir / "public" / item.attempt_id / "public-consumer-audit.json"),
                "sha256": result.receipt_sha256, "verifier_identity": args.verifier_identity,
            }
            return PublicOutcome(result.confirmation, receipt, None, tuple(files))

        def qualify(item: ReadyAttempt, outcome: PublicOutcome) -> QualityOutcome:
            files: list[str] = []
            held = outcome.receipt

            def public_receipt_for(admission: M6AttemptAdmitted) -> M6PublicReceiptInput | None:
                if admission.attempt_id != item.attempt_id:
                    raise ValueError("public receipt was prepared for another attempt")
                return held

            result = qualify_attempt(
                engine=app_engine, paths=paths, source=source, taxonomy=taxonomy,
                batch_size=settings.disclosure_semantic_batch_size, attempt_id=item.attempt_id,
                attempt_dir=args.output_dir / "quality" / item.attempt_id, verifier_identity=args.verifier_identity,
                public_receipt_for=public_receipt_for, plan=plan, files=files,
            )
            return QualityOutcome(result.qualified, None, tuple(files))

        tail = RunnerSpoolTail(args.runner_spool.absolute(), run_id=spec.run_id, spec_sha256=spec.canonical_sha256())
        supervisor = VerifierSupervisor(public=public, quality=quality, source=tail.poll, confirm=confirm,
                                        qualify=qualify, max_attempts=max_attempts)
        runner_receipt: dict[str, Any] | None = None
        while True:
            supervisor.step()
            runner_receipt = read_runner_receipt(args.runner_receipt, run=run, spec=spec)
            if runner_receipt is not None:
                break
            if time.monotonic() - started > args.deadline_seconds:
                raise TimeoutError("supervision deadline passed before the runner's receipt appeared")
            time.sleep(args.poll_seconds)
        summary["runner_receipt"] = runner_receipt
        # The producer has closed. Consume whatever it wrote last (a line that
        # was partial at the previous poll may be complete now), then a
        # remaining partial line is a failure, and every committed publication
        # must have been acknowledged before the set is closed.
        supervisor.step()
        tail.finalize()
        summary["supervisor"] = supervisor.status()
        summary["runner_spool_tail"] = tail.status()
        unacknowledged = sorted(tail.committed_attempts() - tail.released_attempts())
        if unacknowledged:
            raise SupervisorRefused("runner closed with publications the owner never acknowledged: "
                                    + ", ".join(unacknowledged[:16]))
        # The public-inputs manifest is part of the run's evidence (the quality
        # step is re-run independently against it), so it is written before the
        # terminal drain: a failed write aborts both senders and no drain is claimed.
        summary["public_inputs"] = _write_public_inputs(args.output_dir, public_inputs)
        closure = supervisor.close(
            expected_attempts=tail.released_attempts(), runner_complete=runner_receipt["status"] == "complete",
            write_receipt=write_receipt_file(args.output_dir / "drain-receipt.json"),
        )
        closed_by_supervisor = True
        summary["closure"] = closure
        summary["public"], summary["quality"] = closure["public"], closure["quality"]
        summary["status"] = closure["status"]
        exit_code = 0 if closure["status"] == "complete" else 1
    except BaseException as exc:
        # Whatever failed first is the reported failure: record it, close both
        # owned senders without any drain claim, then let it propagate.
        summary["failure"] = f"{type(exc).__name__}:{exc}"[:300]
        abort_open_senders(summary["failure"])
        raise
    finally:
        propagating = sys.exc_info()[1]
        for engine in engines:
            try:
                engine.dispose()
            except Exception as exc:  # noqa: BLE001 - recorded, never hides the outcome
                summary["cleanup_errors"].append(f"engine dispose failed: {type(exc).__name__}:{exc}"[:200])
        summary["finished_utc"] = datetime.now(UTC).isoformat()
        summary["exit_code"] = exit_code
        # On a failed run the manifest is still evidence worth keeping, but its
        # write is secondary: it never replaces the failure being reported.
        if "public_inputs" not in summary and public_inputs:
            try:
                summary["public_inputs"] = _write_public_inputs(args.output_dir, public_inputs)
            except Exception as exc:  # noqa: BLE001 - recorded in the summary; primary failure stays primary
                summary["cleanup_errors"].append(f"public inputs manifest not written: {type(exc).__name__}:{exc}"[:200])
        try:
            _write_summary(args.output_dir, summary)
        except Exception as exc:  # noqa: BLE001 - a failure already being reported is never replaced
            if propagating is None:
                raise
            print(f"run summary not written: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    return exit_code


def _write_public_inputs(output_dir: Path, public_inputs: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Persist the exact public receipt index the quality verifier can be re-run against."""
    manifest = json.dumps({"contract_version": PUBLIC_INPUTS_CONTRACT_VERSION,
                           "receipts": dict(sorted(public_inputs.items()))},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _write_new(output_dir / "public-inputs.json", manifest)
    return {"path": str(output_dir / "public-inputs.json"), "sha256": _sha256(manifest), "attempts": len(public_inputs)}


def _write_summary(output_dir: Path, summary: dict[str, Any]) -> None:
    _write_new(output_dir / "run-summary.json",
               json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2, default=str).encode("utf-8"))
    print(f"Summary: {output_dir / 'run-summary.json'}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
