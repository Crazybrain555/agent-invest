"""Public consumer verification of admitted attempts, optionally as an M6 public_verifier producer.

For each attempt the private publication and source-history audits are read
with the app identity, then the public consumer verifier confirms the
publication through the public reader identity only. Receipts are written to
a new output directory; with ``--m6-run-dir`` every confirmation is spooled
and delivered as a ``public_confirmation`` event and the sender completes.
This CLI never declares ``verifier_drained``: the run's single terminal drain
belongs to the long-lived verifier supervisor after quality has completed
downstream. No database write, no owner event without the run dir.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
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
from disclosure_anchor.application.contracts.m6_run_events import M6AttemptAdmitted, M6PublicConfirmation
from disclosure_anchor.cli.m6_qualify_readonly import require_attempt_path_component
from disclosure_anchor.settings import load_settings

SUMMARY_CONTRACT_VERSION = "m6.public-verification-run-summary.v1"


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _write_new(path: Path, payload: bytes) -> None:
    write_new_exact(path, payload)


class PublicationUnavailable(RuntimeError):
    """The admitted attempt has no active publication to confirm; unavailable, not a consumer failure."""

    def __init__(self, attempt_id: str, admission: M6AttemptAdmitted) -> None:
        super().__init__(f"attempt {attempt_id} has no active publication")
        self.attempt_id, self.admission = attempt_id, admission


@dataclass(frozen=True, slots=True)
class PublicAttemptResult:
    """One attempt's public confirmation plus the exact consumer receipt the quality verifier binds to."""

    admission: M6AttemptAdmitted
    confirmation: M6PublicConfirmation
    receipt: bytes
    receipt_sha256: str
    files: tuple[str, ...]


def confirm_attempt(
    *, app_engine: Any, reader_engine: Any, paths: FileStorePathBuilder, attempt_id: str, attempt_dir: Path,
    verifier_identity: str, page_size: int, max_receipt_bytes: int, max_artifact_bytes: int, max_artifact_files: int,
    files: list[str] | None = None,
) -> PublicAttemptResult:
    """Read the private publication/history audits, confirm publicly, and persist every receipt for the attempt.

    Reused by the CLI loop and by the ongoing verifier supervisor so both
    produce identical evidence files and the same owner event.
    """
    written = [] if files is None else files

    def sink(name: str, payload: bytes) -> None:
        attempt_dir.mkdir(mode=0o700, exist_ok=True)
        _write_new(attempt_dir / f"{name}.json", payload)
        written.append(f"{name}.json:{_sha256(payload)}")

    facts = read_private_qualification_facts(app_engine, attempt_id=attempt_id)
    admission = facts.admission
    private_receipts: list[bytes] = []
    publish = PostgresM6PublishVerifier(engine=app_engine, receipt_sink=private_receipts.append)
    published = publish.read_publication(admission)
    if published is None:
        raise PublicationUnavailable(attempt_id, admission)
    publication_audit = private_receipts[-1]
    sink("private-publication-audit", publication_audit)
    history, = publish.first_ledger_for((admission.source_pdf_sha256,))
    history_audit = private_receipts[-1]
    sink("private-history-audit", history_audit)
    audit = M6PublicAuditInput(
        publication=published.publication, publication_audit_sha256=published.audit_receipt_sha256,
        publication_audit=publication_audit, history=history, history_audit=history_audit,
    )

    def audit_for(value: M6AttemptAdmitted) -> M6PublicAuditInput:
        if value != admission:
            raise ValueError("public audit was prepared for another admission")
        return audit

    consumer_receipts: list[bytes] = []

    def consumer_sink(payload: bytes) -> None:
        consumer_receipts.append(payload)
        sink("public-consumer-audit", payload)

    verifier = M6PublicConsumerVerifier(
        engine=reader_engine, paths=paths, private_audit_for=audit_for, receipt_sink=consumer_sink,
        verifier_identity=verifier_identity, page_size=page_size, maximum_receipt_bytes=max_receipt_bytes,
        maximum_artifact_bytes=max_artifact_bytes, maximum_artifact_files=max_artifact_files,
    )
    confirmation, _history = verifier.confirm(admission)
    if not consumer_receipts:
        raise RuntimeError("public consumer produced no receipt for a confirmation")
    sink("public-confirmation", confirmation.canonical_bytes())
    receipt = consumer_receipts[-1]
    return PublicAttemptResult(admission, confirmation, receipt, _sha256(receipt), tuple(written))


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
                        help="controller-prepared M6 run directory; appends public_confirmation per attempt as "
                             "public_verifier and completes the sender (no verifier_drained; the supervisor drains)")
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
            try:
                result = confirm_attempt(
                    app_engine=app_engine, reader_engine=reader_engine, paths=paths, attempt_id=attempt_id,
                    attempt_dir=attempt_dir, verifier_identity=args.verifier_identity, page_size=args.page_size,
                    max_receipt_bytes=args.max_receipt_bytes, max_artifact_bytes=args.max_artifact_bytes,
                    max_artifact_files=args.max_artifact_files, files=written,
                )
                item["admission"] = result.admission.model_dump(mode="json")
                item.update(status="confirmed", confirmation_sha256=result.confirmation.canonical_sha256(),
                            public_units_sha256=result.confirmation.public_units_sha256)
                if assembly is not None:
                    assembly.record(result.confirmation, attempt_id=attempt_id)
            except PublicationUnavailable as exc:
                item["admission"] = exc.admission.model_dump(mode="json")
                item.update(status="unavailable", reason_code="publication_missing")
                exit_code = max(exit_code, 2)
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
                # This CLI is an evidence pass: confirmations are delivered and the
                # sender completes. The run's single terminal drain belongs to the
                # long-lived supervisor after quality has completed downstream; a
                # fresh CLI process would restart the producer sequence.
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
