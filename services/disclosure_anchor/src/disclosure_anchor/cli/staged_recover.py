"""One reviewed, grant-scoped recovery of accepted V4 tasks; no new submission."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import UTC, datetime
import json
from pathlib import Path
import signal
import time
from types import FrameType
from typing import Any

import sqlalchemy as sa
from sqlalchemy.pool import NullPool

from disclosure_anchor.adapters.db.postgres.connection import (
    require_runtime_app_connection,
    require_runtime_app_engine,
)
from disclosure_anchor.adapters.db.postgres.staged_recovery_scope_v4 import (
    require_accepted_recovery_scope,
)
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import _load_evidence
from disclosure_anchor.adapters.runtime.mineru_host_capacity_observer import (
    build_host_observer_ssh_command,
)
from disclosure_anchor.adapters.runtime.mineru_recovery_gate import (
    VerifiedMinerURecovery,
    load_recovery_gate,
)
from disclosure_anchor.adapters.runtime.staged_worker_v4 import (
    build_staged_worker_v4_runtime,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorTerminal,
)
from disclosure_anchor.application.worker.locks import WORKER_NS
from disclosure_anchor.cli.staged_commission import _documents, _write_new
from disclosure_anchor.cli.worker import (
    _assert_staged_singleton,
    _create_worker_db_engine,
    _database_url,
    _load_staged_process_profile,
    _process_scope_classes,
)
from disclosure_anchor.settings import Settings, load_settings


def run_recovery(
    settings: Settings,
    *,
    gate: VerifiedMinerURecovery,
    max_seconds: int,
    stop_requested: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    if (
        settings.worker_parse_execution_mode != "staged-v4"
        or type(gate) is not VerifiedMinerURecovery
        or type(max_seconds) is not int
        or not 1 <= max_seconds <= 86400
    ):
        raise ValueError(
            "recovery requires explicit V4, verified compatibility, and bounded duration"
        )
    lock_engine = sa.create_engine(
        _database_url(settings), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        with lock_engine.connect() as lock_conn:
            require_runtime_app_connection(lock_conn)
            if not lock_conn.execute(
                sa.text("SELECT pg_try_advisory_lock(:ns,0)"), {"ns": WORKER_NS}
            ).scalar_one():
                raise RuntimeError("another worker holds the singleton lock")
            engine = _create_worker_db_engine(settings)
            try:
                require_runtime_app_engine(engine)

                def ownership_guard() -> None:
                    _assert_staged_singleton(lock_conn)
                    require_accepted_recovery_scope(
                        engine,
                        attempts=gate.attempts,
                        runtime_sha256=gate.grant["old_runtime_sha256"],
                    )
                    gate.assert_live()

                def forbid_admission() -> None:
                    raise RuntimeError("compatibility recovery forbids new admission")

                ownership_guard()
                runtime = build_staged_worker_v4_runtime(
                    settings=settings,
                    engine=engine,
                    ownership_guard=ownership_guard,
                    admission_guard=forbid_admission,
                    process_scope_classes=_process_scope_classes(settings),
                    admission_document_ids=gate.document_ids,
                    recovery_only=True,
                    progress=lambda _: None,
                )
                try:
                    if (
                        runtime.worker_profile_sha256
                        != gate.grant["worker_profile_sha256"]
                    ):
                        raise ValueError(
                            "recovery worker profile differs from the reviewed bound profile"
                        )
                    runtime.verify_startup()
                    deadline = time.monotonic() + max_seconds
                    result = runtime.coordinator.run(
                        stop_requested=lambda: (
                            stop_requested() or time.monotonic() >= deadline
                        ),
                    )
                    ownership_guard()
                    gate.assert_live(force=True)
                    after = _documents(engine, gate.document_ids)
                    clean = (
                        result.terminal is CoordinatorTerminal.QUIESCENT
                        and result.recovery_complete
                        and not result.errors
                        and not result.credits_in_use.nonzero()
                        and result.admitted == 0
                    )
                    published = all(
                        (row := after.get(item["document_id"])) is not None
                        and row["attempt_id"] == item["attempt_id"]
                        and row["attempt_run_id"]
                        == row["current_processing_run_id"]
                        == item["processing_run_id"]
                        and row["status"] == "published"
                        and row["attempt_state"] == "acked"
                        and row["run_is_active"] is True
                        and row["run_status"] == "succeeded"
                        for item in gate.attempts
                    )
                    return {
                        "contract_version": "staged-v4-accepted-result-recovery.v1",
                        "result": "RECOVERY_PASS"
                        if clean and published
                        else "NOT_PASS",
                        "deployment_qualification": False,
                        "observed_at": datetime.now(UTC).isoformat(),
                        "owner_identity": runtime.owner_identity,
                        "historical_runtime_sha256": gate.grant["old_runtime_sha256"],
                        "execution_runtime_sha256": gate.grant[
                            "current_runtime_sha256"
                        ],
                        "execution_writer_sha256": gate.grant["current_writer_sha256"],
                        "process_profile_sha256": gate.grant["process_profile_sha256"],
                        "worker_profile_sha256": runtime.worker_profile_sha256,
                        "terminal": result.terminal.value,
                        "clean": clean,
                        "admitted": result.admitted,
                        "completed": result.completed,
                        "credits_in_use": result.credits_in_use.nonzero(),
                        "errors": list(result.errors),
                        "documents": after,
                    }
                finally:
                    runtime.close()
            finally:
                engine.dispose()
    finally:
        lock_engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grant", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--current-runtime-manifest", type=Path, required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-user", required=True)
    parser.add_argument("--ssh-identity", type=Path, required=True)
    parser.add_argument("--ssh-known-hosts", type=Path, required=True)
    parser.add_argument("--max-seconds", type=int, required=True)
    parser.add_argument("--receipt-out", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.max_seconds <= 86400:
        parser.error("--max-seconds must be 1..86400")
    settings = load_settings()
    loaded = _load_staged_process_profile(settings)
    manifest, _ = _load_evidence(
        args.current_runtime_manifest, label="current recovery runtime"
    )
    ssh = build_host_observer_ssh_command(
        host=args.ssh_host,
        user=args.ssh_user,
        port=22,
        identity_file=args.ssh_identity,
        known_hosts_file=args.ssh_known_hosts,
        expected_host_key_sha256=manifest["manifest"]["topology"][
            "ssh_host_key_sha256"
        ],
    )
    gate = load_recovery_gate(
        settings,
        profile=loaded.profile,
        grant_path=args.grant,
        review_path=args.review,
        current_manifest_path=args.current_runtime_manifest,
        ssh_command=ssh,
    )
    receipt = args.receipt_out.absolute()
    if (
        receipt.parent.resolve(strict=True) != receipt.parent
        or not receipt.is_relative_to(settings.disclosure_runtime_root.resolve())
        or receipt.exists()
        or receipt.is_symlink()
    ):
        parser.error(
            "recovery receipt requires a new path below canonical runtime root"
        )
    _write_new(
        receipt.with_name(receipt.name + ".intent.json"),
        {
            "contract_version": "staged-v4-accepted-result-recovery-intent.v1",
            "grant": gate.grant,
            "max_seconds": args.max_seconds,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    stopped = False

    def stop(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stopped
        stopped = True

    previous = {
        sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        result = run_recovery(
            settings,
            gate=gate,
            max_seconds=args.max_seconds,
            stop_requested=lambda: stopped,
        )
        _write_new(receipt, result)
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0 if result["result"] == "RECOVERY_PASS" else 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
