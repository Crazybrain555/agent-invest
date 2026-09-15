"""Qualify already committed publications read-only; never route, parse or write.

Each attempt is read through one app-identity READ ONLY REPEATABLE READ
snapshot, re-admitted from its raw PDF and frozen bundle, replayed from its
sealed V3 receipts, and bound to the independent public reader's receipt bytes.
Outputs are new files only. No owner event, credit, ACK or model call is made.
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
import traceback
from typing import Any

from disclosure_anchor.adapters.db.postgres.connection import app_database_url, create_db_engine
from disclosure_anchor.adapters.parsers.pdf_text_observation import observe_pdf_text_rectangles
from disclosure_anchor.adapters.runtime.m6_qualification_verifier import (
    M6PrivateQualificationFacts, M6PublicReceiptInput, M6QualificationUnavailable,
    M6ReadonlyQualificationVerifier, read_private_qualification_facts,
)
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.provider_document_source import ProviderDocumentFileSource
from pydantic import TypeAdapter

from disclosure_anchor.application.contracts.m6_common import M6Id
from disclosure_anchor.application.contracts.m6_document_qualification import M6QualityPlan, qualify_document
from disclosure_anchor.application.contracts.m6_run_events import M6AttemptAdmitted
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.services.semantic_taxonomy import load_semantic_route_taxonomy
from disclosure_anchor.settings import load_settings


PUBLIC_INPUTS_CONTRACT_VERSION = "m6.readonly-qualification-public-inputs.v1"
SUMMARY_CONTRACT_VERSION = "m6.readonly-qualification-run-summary.v1"
_MAX_PLAN_BYTES = 2 * 1024 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_RECEIPT_BYTES = 64 * 1024 * 1024


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _read_bounded(path: Path, *, maximum: int) -> bytes:
    """Read one regular, non-symlink file whose size fits the bound."""

    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
            raise ValueError(f"{path} must be a nonempty regular file within {maximum} bytes")
        raw = os.read(fd, info.st_size + 1)
        if len(raw) != info.st_size:
            raise ValueError(f"{path} changed while reading")
        return raw
    finally:
        os.close(fd)


def _write_new(path: Path, payload: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def require_attempt_path_component(attempt_id: str) -> str:
    """Accept only an M6 identity that is also one plain directory name.

    The attempt id names the per-attempt output directory, so it must never
    carry separators, parent references or the current-directory name.
    """

    TypeAdapter(M6Id).validate_python(attempt_id)
    if ("/" in attempt_id or "\\" in attempt_id or attempt_id in {".", ".."}
            or Path(attempt_id).name != attempt_id or Path(attempt_id).is_absolute()):
        raise ValueError(f"attempt id is not a safe output path component: {attempt_id!r}")
    return attempt_id


def read_public_inputs(path: Path) -> dict[str, dict[str, Any]]:
    value = strict_json_loads(_read_bounded(path, maximum=_MAX_MANIFEST_BYTES))
    if (type(value) is not dict or set(value) != {"contract_version", "receipts"}
            or value["contract_version"] != PUBLIC_INPUTS_CONTRACT_VERSION or type(value["receipts"]) is not dict):
        raise ValueError("public inputs manifest requires exact contract_version and receipts fields")
    receipts: dict[str, dict[str, Any]] = {}
    for attempt_id, item in value["receipts"].items():
        require_attempt_path_component(attempt_id)
        if (type(attempt_id) is not str or type(item) is not dict
                or not {"path", "sha256"} <= set(item) <= {"path", "sha256", "verifier_identity"}
                or type(item["path"]) is not str or type(item["sha256"]) is not str):
            raise ValueError(f"public inputs entry for {attempt_id!r} is not closed")
        receipts[attempt_id] = dict(item)
    return receipts


def public_receipt_loader(
    receipts: dict[str, dict[str, Any]], base: Path,
) -> Any:
    def load(admission: M6AttemptAdmitted) -> M6PublicReceiptInput | None:
        item = receipts.get(admission.attempt_id)
        if item is None:
            return None
        path = Path(item["path"])
        raw = _read_bounded(path if path.is_absolute() else base / path, maximum=_MAX_RECEIPT_BYTES)
        return M6PublicReceiptInput(
            receipt=raw, receipt_sha256=item["sha256"], expected_verifier_identity=item.get("verifier_identity"),
        )
    return load


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-id", action="append", required=True, dest="attempt_ids",
                        help="committed V4 attempt to qualify; repeatable; no other run is substituted")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="new directory for reports/evidence; must not exist")
    parser.add_argument("--verifier-identity", required=True)
    parser.add_argument("--public-receipts", type=Path, default=None,
                        help="JSON manifest binding attempt ids to independent public receipt files and expected hashes")
    parser.add_argument("--plan", type=Path, default=None,
                        help="optional e2e M6QualityPlan canonical bytes; adds qualification.json per attempt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    for attempt_id in args.attempt_ids:
        require_attempt_path_component(attempt_id)
    if len(set(args.attempt_ids)) != len(args.attempt_ids):
        raise ValueError("attempt ids must be unique")
    plan = None
    if args.plan is not None:
        plan = M6QualityPlan.from_canonical_bytes(_read_bounded(args.plan, maximum=_MAX_PLAN_BYTES),
                                                  maximum_bytes=_MAX_PLAN_BYTES)
        if plan.mode != "e2e_publication":
            raise ValueError("read-only qualification plan must be the e2e_publication mode")
    receipts = {} if args.public_receipts is None else read_public_inputs(args.public_receipts)
    manifest_base = Path.cwd() if args.public_receipts is None else args.public_receipts.resolve().parent
    args.output_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    summary: dict[str, Any] = {
        "contract_version": SUMMARY_CONTRACT_VERSION, "started_utc": datetime.now(UTC).isoformat(),
        "verifier_identity": args.verifier_identity, "attempts": [], "plan_sha256": None if plan is None else plan.canonical_sha256(),
        "new_owner_events": False, "new_model_or_parser_calls": False, "database_writes": False,
    }
    settings = load_settings()
    paths = FileStorePathBuilder(settings)
    source = ProviderDocumentFileSource(paths, text_reader=observe_pdf_text_rectangles)
    taxonomy = load_semantic_route_taxonomy()
    engine = create_db_engine(app_database_url(settings))
    exit_code = 0
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
                facts = read_private_qualification_facts(engine, attempt_id=attempt_id)
                item["admission"] = facts.admission.model_dump(mode="json")

                def facts_for(admission: M6AttemptAdmitted, *, held: M6PrivateQualificationFacts = facts) -> M6PrivateQualificationFacts:
                    if admission != held.admission:
                        raise ValueError("private facts were read for another admission")
                    return held

                verifier = M6ReadonlyQualificationVerifier(
                    private_facts_for=facts_for, paths=paths, source=source, taxonomy=taxonomy,
                    batch_size=settings.disclosure_semantic_batch_size, receipt_sink=sink,
                    verifier_identity=args.verifier_identity,
                    public_receipt_for=public_receipt_loader(receipts, manifest_base),
                )
                evidence = verifier.qualify(facts.admission)
                item.update(status="evidence", evidence_sha256=evidence.canonical_sha256(),
                            checks={check.check_id: check.outcome for check in evidence.observation.checks},
                            review_reasons=list(evidence.observation.review_reasons))
                if plan is not None:
                    qualification = qualify_document(evidence, plan)
                    sink("qualification", qualification.canonical_bytes())
                    item.update(verdict=qualification.verdict, reasons=list(qualification.reasons))
            except M6QualificationUnavailable as exc:
                item.update(status="unavailable", phase=exc.phase, reason_code=exc.reason_code, error=str(exc))
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
    finally:
        engine.dispose()
        summary["finished_utc"] = datetime.now(UTC).isoformat()
        summary["exit_code"] = exit_code
        _write_new(args.output_dir / "run-summary.json",
                   json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"))
        print(f"Summary: {args.output_dir / 'run-summary.json'}", file=sys.stderr, flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
