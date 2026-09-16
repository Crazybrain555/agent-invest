"""Public consumer verification of admitted attempts, optionally as an M6 public_verifier producer.

For each attempt the private publication and source-history audits are read
with the app identity, then the public consumer verifier confirms the
publication through the public reader identity only. Receipts are written to
a new output directory; with ``--m6-run-dir`` every confirmation is spooled
and delivered as a ``public_confirmation`` event and the run ends with
``verifier_drained``. No database write, no owner event without the run dir.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback
from typing import Any

from disclosure_anchor.adapters.db.postgres.connection import app_database_url, create_db_engine, reader_database_url
from disclosure_anchor.adapters.runtime.exact_file_write import write_new_exact
from disclosure_anchor.adapters.db.postgres.m6_publish_verifier import PostgresM6PublishVerifier
from disclosure_anchor.adapters.runtime.m6_continuous_clock import diagnostic_continuous_clock
from disclosure_anchor.adapters.runtime.m6_e2e_run import M6VerifierAssembly, close_verifier_assembly, load_m6_run_directory
from disclosure_anchor.adapters.runtime.m6_public_consumer_verifier import M6PublicAuditInput, M6PublicConsumerVerifier
from disclosure_anchor.adapters.runtime.m6_qualification_verifier import read_private_qualification_facts
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.application.contracts.m6_run_events import M6AttemptAdmitted
from disclosure_anchor.cli.m6_qualify_readonly import require_attempt_path_component
from disclosure_anchor.settings import load_settings

SUMMARY_CONTRACT_VERSION = "m6.public-verification-run-summary.v1"


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _write_new(path: Path, payload: bytes) -> None:
    write_new_exact(path, payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-id", action="append", required=True, dest="attempt_ids")
    parser.add_argument("--output-dir", type=Path, required=True, help="new directory for receipts")
    parser.add_argument("--verifier-identity", required=True)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-receipt-bytes", type=int, default=32 * 1024 ** 2)
    parser.add_argument("--max-artifact-bytes", type=int, default=2 * 1024 ** 3)
    parser.add_argument("--max-artifact-files", type=int, default=200_000)
    parser.add_argument("--m6-run-dir", type=Path, default=None,
                        help="controller-prepared M6 run directory; appends public_confirmation per attempt and "
                             "verifier_drained at the end as public_verifier (spool under the output directory)")
    args = parser.parse_args(argv)
    # Attempt ids name output directories and reach the database as identities:
    # the established rule rejects separators, parent/current names, absolute
    # paths and duplicates before any file, setting or connection exists.
    attempt_ids = [require_attempt_path_component(item) for item in args.attempt_ids]
    if len(set(attempt_ids)) != len(attempt_ids):
        raise ValueError("attempt ids must be unique")
    args.attempt_ids = attempt_ids
    os.mkdir(args.output_dir, 0o700)
    summary: dict[str, Any] = {
        "contract_version": SUMMARY_CONTRACT_VERSION, "started_utc": datetime.now(UTC).isoformat(),
        "verifier_identity": args.verifier_identity, "attempts": [], "database_writes": False,
    }
    settings = load_settings()
    paths = FileStorePathBuilder(settings)
    assembly: M6VerifierAssembly | None = None
    if args.m6_run_dir is not None:
        run = load_m6_run_directory(args.m6_run_dir.absolute())
        assembly = M6VerifierAssembly(
            run, role="public_verifier", spool_dir=args.output_dir / "m6-assembly",
            max_events=len(args.attempt_ids) + 2, continuous_ns=diagnostic_continuous_clock().now_ns,
        )
        summary["m6_run"] = {"run_id": run.require_spec().run_id, "spec_sha256": run.require_spec().canonical_sha256(),
                             "pins": run.pins}
        assembly.start()
    try:
        app_engine = create_db_engine(app_database_url(settings))
        try:
            reader_engine = create_db_engine(reader_database_url(settings))
        except BaseException:
            app_engine.dispose()
            raise
    except BaseException as exc:
        # Nothing was verified: close the sender visibly without a drain claim.
        if assembly is not None:
            summary["m6_assembly"] = assembly.abort("engine unavailable: " + f"{type(exc).__name__}:{exc}"[:200])
            _write_summary(args.output_dir, summary)
        raise
    exit_code = 0
    drained = False
    try:
        for attempt_id in args.attempt_ids:
            item: dict[str, Any] = {"attempt_id": attempt_id, "status": "error"}
            summary["attempts"].append(item)
            attempt_dir = args.output_dir / attempt_id
            written: list[str] = []

            def sink(name: str, payload: bytes, *, target: Path = attempt_dir, names: list[str] = written) -> None:
                target.mkdir(mode=0o700, exist_ok=True)
                _write_new(target / f"{name}.json", payload)
                names.append(f"{name}.json:{_sha256(payload)}")

            try:
                facts = read_private_qualification_facts(app_engine, attempt_id=attempt_id)
                admission = facts.admission
                item["admission"] = admission.model_dump(mode="json")
                private_receipts: list[bytes] = []
                publish = PostgresM6PublishVerifier(engine=app_engine, receipt_sink=private_receipts.append)
                published = publish.read_publication(admission)
                if published is None:
                    item.update(status="unavailable", reason_code="publication_missing")
                    exit_code = max(exit_code, 2)
                    continue
                publication_audit = private_receipts[-1]
                sink("private-publication-audit", publication_audit)
                history, = publish.first_ledger_for((admission.source_pdf_sha256,))
                history_audit = private_receipts[-1]
                sink("private-history-audit", history_audit)
                audit = M6PublicAuditInput(
                    publication=published.publication, publication_audit_sha256=published.audit_receipt_sha256,
                    publication_audit=publication_audit, history=history, history_audit=history_audit,
                )

                def audit_for(value: M6AttemptAdmitted, *, held: M6PublicAuditInput = audit,
                              expected: M6AttemptAdmitted = admission) -> M6PublicAuditInput:
                    if value != expected:
                        raise ValueError("public audit was prepared for another admission")
                    return held

                verifier = M6PublicConsumerVerifier(
                    engine=reader_engine, paths=paths, private_audit_for=audit_for,
                    receipt_sink=lambda payload: sink("public-consumer-audit", payload),
                    verifier_identity=args.verifier_identity, page_size=args.page_size,
                    maximum_receipt_bytes=args.max_receipt_bytes, maximum_artifact_bytes=args.max_artifact_bytes,
                    maximum_artifact_files=args.max_artifact_files,
                )
                confirmation, _history = verifier.confirm(admission)
                sink("public-confirmation", confirmation.canonical_bytes())
                item.update(status="confirmed", confirmation_sha256=confirmation.canonical_sha256(),
                            public_units_sha256=confirmation.public_units_sha256)
                if assembly is not None:
                    assembly.record(confirmation, attempt_id=attempt_id)
            except KeyboardInterrupt:
                item.update(status="interrupted")
                raise
            except Exception as exc:  # noqa: BLE001 - recorded per attempt, then the run continues
                item.update(status="error", error_type=type(exc).__name__, error=str(exc),
                            traceback=traceback.format_exc())
                exit_code = max(exit_code, 1)
            finally:
                item["files"] = written
                print(json.dumps(item, ensure_ascii=False, sort_keys=True), flush=True)
        drained = True
    finally:
        # An exception leaving the loop (interrupt or unexpected) means the
        # verification did not run to its end: the sender is aborted, never
        # drained. Engine disposal cannot skip that closure, and a failing
        # summary write never replaces the failure being reported.
        propagating = sys.exc_info()[1]
        dispose_error: Exception | None = None
        for engine in (app_engine, reader_engine):
            try:
                engine.dispose()
            except Exception as exc:  # noqa: BLE001 - reported after the sender is closed
                dispose_error = dispose_error or exc
        summary["finished_utc"] = datetime.now(UTC).isoformat()
        try:
            if assembly is not None:
                exit_code = close_verifier_assembly(
                    assembly, output_dir=args.output_dir, summary=summary, producer_kind="public_verifier",
                    verifier_identity=args.verifier_identity, exit_code=exit_code,
                    drained=drained and propagating is None, write=_write_new,
                )
        finally:
            failing = sys.exc_info()[1]
            summary["exit_code"] = exit_code
            try:
                _write_summary(args.output_dir, summary)
            except Exception as exc:  # noqa: BLE001 - a failure already being reported is never replaced
                if failing is None:
                    raise
                print(f"run summary not written: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        if dispose_error is not None:
            if propagating is not None:
                print(f"engine dispose failed: {type(dispose_error).__name__}: {dispose_error}", file=sys.stderr, flush=True)
            else:
                raise dispose_error
    return exit_code


def _write_summary(output_dir: Path, summary: dict[str, Any]) -> None:
    _write_new(output_dir / "run-summary.json",
               json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"))
    print(f"Summary: {output_dir / 'run-summary.json'}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
