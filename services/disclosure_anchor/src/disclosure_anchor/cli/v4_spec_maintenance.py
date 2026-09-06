"""Finite offline execution-spec backfill between migrations 0060 and 0061.

Backfill does not delete files. Explicit --retire-spec deletes only selected,
verified copied/orphan files after FK validation, without a directory sweep.
This command does not stop workers or apply migrations.
The operator must first disable restart and observe actual old-process exit.
"""

import argparse
from dataclasses import asdict
import json

from sqlalchemy import text

from disclosure_anchor.adapters.db.postgres.connection import (
    app_database_url, create_db_engine, require_runtime_app_engine,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.adapters.storage.immutable_artifact_store import ImmutableArtifactStore
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.v4_execution_spec_catalog import ImmutableV4ExecutionSpecCatalog
from disclosure_anchor.adapters.storage.v4_legacy_spec_retirement import LegacyV4ExecutionSpecRetirer
from disclosure_anchor.application.ports.v4_execution_spec_catalog import V4ExecutionSpecCatalogReference
from disclosure_anchor.application.services.backfill_v4_execution_specs import backfill_v4_execution_specs_once
from disclosure_anchor.application.services.retire_v4_execution_specs import retire_v4_execution_specs_once
from disclosure_anchor.application.worker.locks import WORKER_NS, WorkerBusyError
from disclosure_anchor.settings import load_settings


def _reference(value: str) -> V4ExecutionSpecCatalogReference:
    try:
        digest, size = value.rsplit("=", 1)
        return V4ExecutionSpecCatalogReference(digest, int(size))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected canonical sha256:<digest>=<byte-count>") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--limit", type=int)
    action.add_argument("--retire-spec", type=_reference, action="append")
    parser.add_argument("--after-attempt-id")
    parser.add_argument(
        "--old-writers-drained", action="store_true", required=True,
        help="attest actual old worker/process exit and disabled automatic restart; a lost PG lease is not proof",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and not 1 <= args.limit <= 100:
        parser.error("--limit must be 1..100")
    if args.retire_spec and (args.after_attempt_id or len(args.retire_spec) > 100 or len(set(args.retire_spec)) != len(args.retire_spec)):
        parser.error("retirement requires 1..100 distinct references and no backfill cursor")
    settings = load_settings()
    paths = FileStorePathBuilder(settings)
    engine = create_db_engine(app_database_url(settings))
    try:
        require_runtime_app_engine(engine)
        with engine.connect() as ownership:
            acquired = ownership.execute(text(
                "SELECT pg_try_advisory_lock(:ns, 0)"
            ), {"ns": WORKER_NS}).scalar_one()
            ownership.commit()
            if not acquired:
                raise WorkerBusyError("worker admission is held; offline backfill refused")

            def guard() -> None:
                held = ownership.execute(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                    "AND pid=pg_backend_pid() AND classid=:ns AND objid=0 "
                    "AND objsubid=2 AND mode='ExclusiveLock' AND granted)"
                ), {"ns": WORKER_NS}).scalar_one()
                if not held:
                    raise WorkerBusyError("offline backfill ownership was lost")

            try:
                if args.retire_spec:
                    deleted = retire_v4_execution_specs_once(
                        uow_factory=unit_of_work_factory(engine), files=LegacyV4ExecutionSpecRetirer(paths),
                        references=tuple(args.retire_spec), ownership_guard=guard,
                    )
                    print(json.dumps({"deleted": deleted, "already_absent": len(args.retire_spec)-deleted}, sort_keys=True))
                    return 0
                result = backfill_v4_execution_specs_once(
                    uow_factory=unit_of_work_factory(engine),
                    legacy=ImmutableV4ExecutionSpecCatalog(
                        paths=paths, immutable_store=ImmutableArtifactStore(paths),
                    ),
                    after_attempt_id=args.after_attempt_id, limit=args.limit,
                    write_guard=guard,
                )
                print(json.dumps(asdict(result), sort_keys=True))
            finally:
                # Never return a session advisory lock to a pooled connection.
                ownership.invalidate()
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
