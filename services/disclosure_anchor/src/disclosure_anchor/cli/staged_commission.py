"""One explicitly scoped V4 coordinator run; no acquisition or resident loop."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import signal
import time
from types import FrameType
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from disclosure_anchor.adapters.db.postgres.connection import (
    require_runtime_app_connection, require_runtime_app_engine,
)
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import MinerUDeploymentChecker
from disclosure_anchor.adapters.runtime.staged_worker_v4 import build_staged_worker_v4_runtime
from disclosure_anchor.application.ports.staged_new_work_v4 import validate_admission_document_ids
from disclosure_anchor.application.services.staged_parse_coordinator import CoordinatorTerminal
from disclosure_anchor.application.worker.locks import WORKER_NS
from disclosure_anchor.cli.worker import (
    _assert_staged_singleton, _assert_worker_admission, _create_worker_db_engine,
    _database_url, _load_staged_process_profile, _process_scope_classes,
)
from disclosure_anchor.settings import Settings, load_settings


def _documents(engine: Engine, document_ids: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT d.document_id,d.status,d.current_processing_run_id,"
                "a.attempt_id,a.processing_run_id AS attempt_run_id,a.state AS attempt_state,"
                "r.is_active AS run_is_active,r.status AS run_status "
                "FROM disclosure_core.document AS d "
                "LEFT JOIN LATERAL (SELECT ra.attempt_id,ra.processing_run_id,ra.state "
                "FROM disclosure_ops.remote_parse_attempt ra WHERE ra.document_id=d.document_id "
                "AND ra.checkpoint_contract_version=4 "
                "AND (ra.is_current OR ra.processing_run_id=d.current_processing_run_id) "
                "ORDER BY ra.is_current DESC,ra.attempt_generation DESC LIMIT 1) AS a ON TRUE "
                "LEFT JOIN disclosure_core.processing_run AS r "
                "ON r.processing_run_id=d.current_processing_run_id AND r.document_id=d.document_id "
                "WHERE d.document_id=ANY(:document_ids)"
            ), {"document_ids": list(document_ids)},
        ).mappings()
        return {row["document_id"]: dict(row) for row in rows}


def _outcomes(
    document_ids: tuple[str, ...], before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    result = []
    for document_id in document_ids:
        old, current = before.get(document_id), after.get(document_id)
        outcome = "blocked"
        if current is not None:
            if (current["status"] == "published"
                    and current["run_is_active"] is True and current["run_status"] == "succeeded"
                    and current["attempt_state"] == "acked"
                    and current["attempt_run_id"] == current["current_processing_run_id"]
                    and (old is None or old["current_processing_run_id"]
                         != current["current_processing_run_id"])):
                outcome = "published"
            elif (old is not None and old["status"] != "published" and current["status"] == "published"
                  and current["run_is_active"] is True and current["run_status"] == "succeeded"
                  and current["attempt_state"] == "acked"
                  and current["attempt_run_id"] == current["current_processing_run_id"]):
                outcome = "recovered_published"
            elif (old is not None and old["status"] == current["status"] == "published"
                  and old["current_processing_run_id"] == current["current_processing_run_id"]
                  and current["attempt_state"] in {None, "acked"}):
                outcome = "already_ineligible"
            elif current["status"] == "parse_failed" or current["attempt_state"] in {
                "remote_failed", "local_failed", "pre_submission_failed", "preparation_failed",
            }:
                outcome = "failed"
        result.append({"document_id": document_id, "outcome": outcome, "current": current})
    return result


def run_commissioning(
    settings: Settings, *, document_ids: tuple[str, ...], max_seconds: int,
    stop_requested: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Reuse production locks, deployment gate, recovery and all seven lanes."""
    validate_admission_document_ids(document_ids)
    if document_ids is None or type(max_seconds) is not int or not 1 <= max_seconds <= 86400:
        raise ValueError("commissioning requires explicit document IDs and 1..86400 seconds")
    if settings.worker_parse_execution_mode != "staged-v4":
        raise ValueError("commissioning requires explicit staged-v4 mode")
    loaded = _load_staged_process_profile(settings)
    checker = MinerUDeploymentChecker(settings, parse_enabled=True, process_profile=loaded.profile)
    lock_engine = sa.create_engine(_database_url(settings), poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        with lock_engine.connect() as lock_conn:
            require_runtime_app_connection(lock_conn)
            if not lock_conn.execute(
                sa.text("SELECT pg_try_advisory_lock(:ns, 0)"), {"ns": WORKER_NS},
            ).scalar_one():
                raise RuntimeError("another worker holds the singleton lock")
            engine = _create_worker_db_engine(settings)
            try:
                require_runtime_app_engine(engine)
                before = _documents(engine, document_ids)
                if set(before) != set(document_ids):
                    raise ValueError("commissioning document ID does not exist")
                def ownership_guard() -> None:
                    _assert_staged_singleton(lock_conn)

                def admission_guard() -> None:
                    _assert_worker_admission(lock_conn, mineru_checker=checker, singleton_guard=ownership_guard)

                runtime = build_staged_worker_v4_runtime(
                    settings=settings, engine=engine, ownership_guard=ownership_guard,
                    admission_guard=admission_guard,
                    process_scope_classes=_process_scope_classes(settings),
                    admission_document_ids=document_ids, progress=lambda _snapshot: None,
                )
                try:
                    runtime.verify_startup()
                    deadline = time.monotonic() + max_seconds
                    result = runtime.coordinator.run(
                        stop_requested=lambda: stop_requested() or time.monotonic() >= deadline,
                    )
                    after = _documents(engine, document_ids)
                    outcomes = _outcomes(document_ids, before, after)
                    clean = (
                        result.terminal is CoordinatorTerminal.QUIESCENT
                        and result.recovery_complete and not result.errors
                        and not result.credits_in_use.nonzero()
                    )
                    passed = clean and all(item["outcome"] == "published" for item in outcomes)
                    return {
                        "contract_version": "staged-v4-commissioning.v1",
                        "observed_at": datetime.now(UTC).isoformat(),
                        "owner_identity": runtime.owner_identity,
                        "worker_profile_sha256": runtime.worker_profile_sha256,
                        "process_profile_sha256": loaded.profile.sha256,
                        "runtime_bundle_identity_sha256": loaded.profile.runtime_bundle_identity_sha256,
                        "max_seconds": max_seconds, "result": "PASS" if passed else "NOT_PASS",
                        "terminal": result.terminal.value, "clean": clean,
                        "recovery_complete": result.recovery_complete,
                        "admitted": result.admitted, "completed": result.completed,
                        "errors": list(result.errors),
                        "credits_in_use": result.credits_in_use.nonzero(), "documents": outcomes,
                    }
                finally:
                    runtime.close()
            finally:
                engine.dispose()
    finally:
        lock_engine.dispose()


def _write_new(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    if len(payload) > 1024 * 1024:
        raise ValueError("commissioning receipt exceeds its bound")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-id", action="append", required=True)
    parser.add_argument("--max-seconds", type=int, required=True)
    parser.add_argument("--receipt-out", type=Path, required=True)
    args = parser.parse_args(argv)
    settings = load_settings()
    document_ids = tuple(args.document_id)
    validate_admission_document_ids(document_ids)
    if not 1 <= args.max_seconds <= 86400:
        parser.error("--max-seconds must be 1..86400")
    receipt = args.receipt_out.absolute()
    # Audit only beneath the configured runtime root, never a source artifact.
    if (receipt.parent.resolve(strict=True) != receipt.parent
            or not receipt.is_relative_to(settings.disclosure_runtime_root.resolve())):
        parser.error("--receipt-out requires an existing canonical directory under runtime root")
    if receipt.exists() or receipt.is_symlink():
        parser.error("receipt already exists; retain the previous run evidence")
    _write_new(receipt.with_name(receipt.name + ".intent.json"), {
        "contract_version": "staged-v4-commissioning-intent.v1", "document_ids": document_ids,
        "max_seconds": args.max_seconds, "created_at": datetime.now(UTC).isoformat(),
    })
    stopped = False

    def stop(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stopped
        stopped = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        result = run_commissioning(settings, document_ids=document_ids, max_seconds=args.max_seconds,
                                   stop_requested=lambda: stopped)
        _write_new(receipt, result)
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0 if result["result"] == "PASS" else 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
