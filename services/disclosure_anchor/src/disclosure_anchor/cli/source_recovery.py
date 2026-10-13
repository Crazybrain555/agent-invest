"""Historical security bindings and retained-archive registration.

    binding-preview  --request FILE --evidence-file FILE --out FILE
    binding-execute  --plan FILE --expect-sha256 SHA --decided-by NAME
                     --evidence-file FILE [--out FILE]
    replay-preview   --failed-access-ids FILE --binding-source-access-id ID
                     --max-items N --out FILE
    replay-execute   --plan FILE --expect-sha256 SHA --max-items N --out FILE
    reconcile        --plan FILE --expect-sha256 SHA --out FILE

Previews write nothing to the database. Plans are canonical JSON; the printed
sha256 must be passed back to the command that executes them, and a plan only
executes under the same recovery code that previewed it. This command never
builds a provider client, downloader, parser, MinerU or model component: a
retained registration reads an already archived PDF and registers it.
Output files are never overwritten. Exit 0 = completed, 1 = refused/stopped.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

from pydantic import BaseModel

from disclosure_anchor.adapters.db.postgres.connection import (
    app_database_url,
    create_db_engine,
    require_runtime_app_engine,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.raw_document_store import RawDocumentStore
from disclosure_anchor.application.contracts.closed_document import canonical_bytes, sha256_of
from disclosure_anchor.application.contracts.historical_security_registration import (
    HISTORICAL_SECURITY_BINDING_PLAN_SCHEMA,
    MAX_CONTRACT_BYTES,
    RETAINED_REGISTRATION_PLAN_SCHEMA,
    ContractViolation,
    HistoricalSecurityBindingPlanV1,
    RetainedRegistrationPlanV1,
    load_binding,
    load_canonical_plan,
    load_request,
    plan_bytes,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.use_cases.historical_security_binding import (
    BindingEvidence,
    HistoricalSecurityBinding,
)
from disclosure_anchor.application.use_cases.recover_archived_registration import (
    ItemOutcome,
    RetainedRegistrationExecutor,
    RetainedRegistrationPreview,
    RetainedRegistrationReconciler,
)
from disclosure_anchor.domain.errors import SourceRecoveryError
from disclosure_anchor.settings import Settings, load_settings

# The sources whose bytes decide what a plan means. A plan previewed by one
# set of bytes never executes under another.
_RECOVERY_CODE_RELPATHS = (
    "src/disclosure_anchor/application/contracts/historical_security_registration.py",
    "src/disclosure_anchor/application/services/source_security_resolution.py",
    "src/disclosure_anchor/application/services/register_document.py",
    "src/disclosure_anchor/application/services/subject_resolver.py",
    "src/disclosure_anchor/application/use_cases/download_document.py",
    "src/disclosure_anchor/application/use_cases/historical_security_binding.py",
    "src/disclosure_anchor/application/use_cases/recover_archived_registration.py",
    "src/disclosure_anchor/adapters/storage/raw_document_store.py",
    "src/disclosure_anchor/adapters/storage/path_builder.py",
    "src/disclosure_anchor/adapters/db/postgres/repositories.py",
    "src/disclosure_anchor/cli/source_recovery.py",
)
_RESULT_NOTE = (
    "database receipts are authoritative; this report is evidence of one run "
    "and does not prove a commit on its own"
)


def recovery_code_digest() -> str:
    """Bind plans to the exact local recovery source bytes."""

    service_root = Path(__file__).resolve().parents[3]
    digest = hashlib.sha256()
    for relpath in _RECOVERY_CODE_RELPATHS:
        path = service_root / relpath
        if not path.is_file() or path.is_symlink():
            raise SourceRecoveryError(
                "CODE_IDENTITY_UNAVAILABLE", f"recovery source is missing or unsafe: {relpath}"
            )
        payload = path.read_bytes()
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _refuse_existing_output(args)
        handler: Callable[[argparse.Namespace, Settings], int] = args.handler
        return handler(args, load_settings())
    except (SourceRecoveryError, ContractViolation) as exc:
        error = (
            {"error_code": exc.error_code, "message": exc.message}
            if isinstance(exc, SourceRecoveryError)
            else {"error_code": "CONTRACT_VIOLATION", "message": str(exc)}
        )
        print(json.dumps(error, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1


def _binding_preview(args: argparse.Namespace, settings: Settings) -> int:
    binding = load_binding(_read_bounded(args.request))
    evidence = _evidence(args.evidence_file)
    with _RuntimeDatabase(settings) as uow_factory:
        plan = HistoricalSecurityBinding(
            uow_factory=uow_factory, code_identity=recovery_code_digest()
        ).preview(binding, evidence=evidence)
    payload = plan_bytes(plan.to_document())
    _write_new(args.out, payload)
    _emit(
        {
            "plan": str(args.out),
            "plan_sha256": sha256_of(payload),
            "binding_sha256": plan.binding_sha256,
            "action": plan.preflight.action,
            "note": "binding-preview wrote no database state",
        }
    )
    return 0


def _binding_execute(args: argparse.Namespace, settings: Settings) -> int:
    plan: HistoricalSecurityBindingPlanV1 = _load_plan(
        args.plan,
        expected_sha256=args.expect_sha256,
        model=HistoricalSecurityBindingPlanV1,
        label=HISTORICAL_SECURITY_BINDING_PLAN_SCHEMA,
    )
    evidence = _evidence(args.evidence_file)
    with _RuntimeDatabase(settings) as uow_factory:
        result = HistoricalSecurityBinding(
            uow_factory=uow_factory, code_identity=recovery_code_digest()
        ).execute(plan, evidence=evidence, decided_by=args.decided_by)
    receipt = {
        "schema": "historical-security-binding-receipt.v1",
        "plan_sha256": args.expect_sha256,
        "binding_source_access_id": result.binding_source_access_id,
        "historical_security_id": result.historical_security_id,
        "binding_sha256": result.binding_sha256,
        "created_historical_security": result.created_historical_security,
        "recorded_now": result.recorded,
    }
    if args.out is not None:
        _write_new(args.out, canonical_bytes(receipt))
    _emit(receipt)
    return 0


def _replay_preview(args: argparse.Namespace, settings: Settings) -> int:
    request, request_sha256 = load_request(_read_bounded(args.failed_access_ids))
    with _RuntimeDatabase(settings) as uow_factory:
        outcome = RetainedRegistrationPreview(
            uow_factory=uow_factory,
            retained_archive=RawDocumentStore(FileStorePathBuilder(settings)),
            code_identity=recovery_code_digest(),
            max_download_retries=settings.cninfo_max_retries,
        ).preview(
            request,
            request_sha256=request_sha256,
            binding_source_access_id=args.binding_source_access_id,
            max_items=args.max_items,
        )
    if outcome.plan is None:
        report = {
            "schema": "retained-registration-preview-refusals.v1",
            "request_sha256": request_sha256,
            "item_count": len(request.items),
            "refused_count": len(outcome.refusals),
            "refusals": [
                {
                    "failed_source_access_id": refusal.failed_source_access_id,
                    "error_code": refusal.error_code,
                    "message": refusal.message,
                }
                for refusal in outcome.refusals
            ],
            "note": "no plan was produced; replay-preview wrote no database state",
        }
        _write_new(args.out, plan_bytes(report))
        _emit({"refusals": str(args.out), "refused_count": len(outcome.refusals)})
        return 1
    payload = plan_bytes(outcome.plan.to_document())
    _write_new(args.out, payload)
    _emit(
        {
            "plan": str(args.out),
            "plan_sha256": sha256_of(payload),
            "item_count": outcome.plan.item_count,
            "total_byte_count": outcome.plan.total_byte_count,
            "already_resolved": sum(
                1 for item in outcome.plan.items if item.preview_state == "already_resolved"
            ),
            "note": "replay-preview wrote no database state",
        }
    )
    return 0


def _replay_execute(args: argparse.Namespace, settings: Settings) -> int:
    plan: RetainedRegistrationPlanV1 = _load_plan(
        args.plan,
        expected_sha256=args.expect_sha256,
        model=RetainedRegistrationPlanV1,
        label=RETAINED_REGISTRATION_PLAN_SCHEMA,
    )
    outcomes: list[ItemOutcome] = []
    aborted: BaseException | None = None
    try:
        with _RuntimeDatabase(settings) as uow_factory:
            RetainedRegistrationExecutor(
                uow_factory=uow_factory,
                retained_archive=RawDocumentStore(FileStorePathBuilder(settings)),
                code_identity=recovery_code_digest(),
                max_download_retries=settings.cninfo_max_retries,
            ).execute(
                plan,
                plan_sha256=args.expect_sha256,
                max_items=args.max_items,
                on_item=outcomes.append,
            )
    except BaseException as exc:
        aborted = exc
        raise
    finally:
        # Written even when the run aborts, so a partial prefix is visible.
        counts: dict[str, int] = {}
        for outcome in outcomes:
            counts[outcome.state] = counts.get(outcome.state, 0) + 1
        report: dict[str, Any] = {
            "schema": "retained-registration-execution-result.v1",
            "plan_sha256": args.expect_sha256,
            "item_count": plan.item_count,
            "reported_items": len(outcomes),
            "counts": dict(sorted(counts.items())),
            "items": [outcome.to_document() for outcome in outcomes],
            "aborted": None
            if aborted is None
            else {"error_type": type(aborted).__name__, "message": str(aborted)[:2000]},
            "note": _RESULT_NOTE,
        }
        _write_new(args.out, plan_bytes(report))
    stopped = any(outcome.state == "stopped" for outcome in outcomes)
    _emit({"result": str(args.out), "counts": dict(sorted(counts.items()))})
    return 1 if stopped else 0


def _reconcile(args: argparse.Namespace, settings: Settings) -> int:
    plan: RetainedRegistrationPlanV1 = _load_plan(
        args.plan,
        expected_sha256=args.expect_sha256,
        model=RetainedRegistrationPlanV1,
        label=RETAINED_REGISTRATION_PLAN_SCHEMA,
    )
    with _RuntimeDatabase(settings) as uow_factory:
        reconciliation = RetainedRegistrationReconciler(uow_factory=uow_factory).reconcile(
            plan, plan_sha256=args.expect_sha256
        )
    report = reconciliation.to_document()
    _write_new(args.out, plan_bytes(report))
    _emit(
        {
            "reconciliation": str(args.out),
            "resolved": report["resolved"],
            "unresolved": report["unresolved"],
            "conflict": report["conflict"],
            "item_count": report["item_count"],
        }
    )
    return 1 if report["conflict"] else 0


class _RuntimeDatabase:
    """Runtime app engine for one command; disposed on exit."""

    def __init__(self, settings: Settings) -> None:
        self._engine = create_db_engine(app_database_url(settings))

    def __enter__(self) -> Callable[[], UnitOfWork]:
        try:
            require_runtime_app_engine(self._engine)
        except BaseException:
            self._engine.dispose()
            raise
        return unit_of_work_factory(self._engine)

    def __exit__(self, *_exc: object) -> None:
        self._engine.dispose()


def _load_plan(path: Path, *, expected_sha256: str, model: type[BaseModel], label: str) -> Any:
    return load_canonical_plan(
        _read_bounded(path), expected_sha256=expected_sha256, model=model, label=label
    )


def _read_bounded(path: Path) -> bytes:
    try:
        info = path.stat()
    except OSError as exc:
        raise SourceRecoveryError("INPUT_UNREADABLE", f"{path}: {exc}") from exc
    if not path.is_file() or info.st_size > MAX_CONTRACT_BYTES:
        raise SourceRecoveryError("INPUT_UNREADABLE", f"{path} is not a bounded regular file")
    return path.read_bytes()


def _evidence(path: Path) -> BindingEvidence:
    if not path.is_file():
        raise SourceRecoveryError("EVIDENCE_UNREADABLE", f"{path} is not a regular file")
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            byte_count += len(chunk)
    return BindingEvidence(sha256="sha256:" + digest.hexdigest(), byte_count=byte_count)


def _refuse_existing_output(args: argparse.Namespace) -> None:
    out = getattr(args, "out", None)
    if out is not None and (out.exists() or out.is_symlink()):
        raise SourceRecoveryError("OUTPUT_EXISTS", f"{out} already exists; outputs are never overwritten")


def _write_new(path: Path, payload: bytes) -> None:
    """Durably publish a new file; an existing path is never replaced."""

    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with tmp.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(tmp, path)
    except FileExistsError:
        raise SourceRecoveryError(
            "OUTPUT_EXISTS", f"{path} already exists; outputs are never overwritten"
        ) from None
    finally:
        tmp.unlink()
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m disclosure_anchor.cli.source_recovery",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    preview = commands.add_parser("binding-preview", help="verify a binding decision; no writes")
    preview.add_argument("--request", type=Path, required=True)
    preview.add_argument("--evidence-file", type=Path, required=True)
    preview.add_argument("--out", type=Path, required=True)
    preview.set_defaults(handler=_binding_preview)

    execute = commands.add_parser("binding-execute", help="record one previewed binding")
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--expect-sha256", required=True)
    execute.add_argument("--decided-by", required=True, help="must equal the decision's decided_by")
    execute.add_argument("--evidence-file", type=Path, required=True)
    execute.add_argument("--out", type=Path, default=None)
    execute.set_defaults(handler=_binding_execute)

    replay_preview = commands.add_parser(
        "replay-preview", help="plan retained registrations for exact failed accesses; no writes"
    )
    replay_preview.add_argument("--failed-access-ids", type=Path, required=True)
    replay_preview.add_argument("--binding-source-access-id", required=True)
    replay_preview.add_argument("--max-items", type=_positive_int, required=True)
    replay_preview.add_argument("--out", type=Path, required=True)
    replay_preview.set_defaults(handler=_replay_preview)

    replay_execute = commands.add_parser(
        "replay-execute", help="execute a previewed plan item by item"
    )
    replay_execute.add_argument("--plan", type=Path, required=True)
    replay_execute.add_argument("--expect-sha256", required=True)
    replay_execute.add_argument("--max-items", type=_positive_int, required=True)
    replay_execute.add_argument("--out", type=Path, required=True)
    replay_execute.set_defaults(handler=_replay_execute)

    reconcile = commands.add_parser(
        "reconcile", help="read back every plan item from database receipts"
    )
    reconcile.add_argument("--plan", type=Path, required=True)
    reconcile.add_argument("--expect-sha256", required=True)
    reconcile.add_argument("--out", type=Path, required=True)
    reconcile.set_defaults(handler=_reconcile)
    return parser


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


if __name__ == "__main__":
    raise SystemExit(main())
