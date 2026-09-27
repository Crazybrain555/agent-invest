"""Managed closure of expired, never-submitted prepared V4 obligations (Pro VI.2).

    preview  --inventory FILE --key-lookups FILE --origin-runtime-identity-sha256 SHA
             --key-ttl-seconds N --out PLAN
    execute  --plan PLAN --expect-sha256 SHA --inventory FILE --key-lookups FILE
             --decided-by NAME --reason TEXT --out RECEIPT

``preview`` is read-only: it proves, per member of the fixed inventory, the
supervised before-expiry absence of its original key and an unmoved prepared
H0, writes the canonical plan (never overwritten) and prints its sha256. A key
still inside its actual lifetime is listed, never closed. ``execute`` requires
that exact plan digest and an attributed decision, holds the worker singleton
for its whole run, re-reads every input and head, and closes each planned
member through the ordinary V4 pre-submission failure and owned cleanup: no
POST, no lookup, no new key, no ACK and no lifetime change. Each member commits
on its own (no batch transaction): a refusal at a later member leaves earlier
members closed, and re-running the same plan and decision resumes safely.
Requeue is a separate, explicit parse-requeue decision per failed run; closure
never requeues. Exit 0 = done, 1 = refused.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any
import uuid

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from disclosure_anchor.adapters.db.postgres.connection import (
    app_database_url,
    create_db_engine,
    require_runtime_app_connection,
    require_runtime_app_engine,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4 import MinerUHttpStagedV4
from disclosure_anchor.adapters.runtime.exact_file_write import publish_new_exact
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.application.contracts.expired_prepared_closure import (
    ExpiredPreparedClosurePlanV1,
    ExpiredPreparedClosureRefused,
    closure_decision_sha256,
    decode_expired_prepared_closure_plan,
)
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    decode_legacy_key_lookup_evidence,
    decode_legacy_scope_inventory,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.staged_coordinator_backend_v4 import (
    DurableStagedCoordinatorBackendV4,
)
from disclosure_anchor.application.services.staged_coordinator_persistence_v4 import (
    DurableStagedCoordinatorPersistenceV4,
    DurableV4ClaimGuard,
)
from disclosure_anchor.application.services.staged_parse_coordinator import CoordinatorLimits
from disclosure_anchor.application.use_cases.close_expired_prepared_v4 import ExpiredPreparedClosure
from disclosure_anchor.application.worker.locks import WORKER_NS
from disclosure_anchor.cli.worker import _assert_staged_singleton
from disclosure_anchor.settings import Settings, load_settings

RECEIPT_SCHEMA = "worker-expired-prepared-closure-receipt.v1"


class _Unavailable:
    """A collaborator the managed closure must never reach."""

    def __init__(self, label: str) -> None:
        self._label = label

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        raise RuntimeError(f"{self._label} is unavailable to the managed expired-prepared closure")


def _read(path: Path) -> tuple[bytes, str]:
    raw = path.read_bytes()
    return raw, "sha256:" + hashlib.sha256(raw).hexdigest()


def _publish(path: Path, payload: bytes) -> None:
    """Publish once; an existing identical file is the same result replayed."""

    try:
        publish_new_exact(path, payload)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise ExpiredPreparedClosureRefused(f"{path} already holds a different result") from None


def compose(
    *,
    uow_factory: Callable[[], UnitOfWork],
    scratch_root: Path,
    published_root: Path,
    process_guard: Callable[[], None],
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
) -> ExpiredPreparedClosure:
    """Only the V4 persistence, claim guard and owned cleanup; nothing remote."""

    ample = ResourceCreditVector(**{item.name: 1 << 40 for item in fields(ResourceCreditVector)})
    limits = CoordinatorLimits(credits=ample)
    persistence = DurableStagedCoordinatorPersistenceV4(
        uow_factory=uow_factory, limits=limits,
        owner_identity=f"expired-prepared-closure:{uuid.uuid4().hex}",
        process_guard=process_guard,
        monotonic=monotonic,
    )
    unavailable = _Unavailable("the provider transport")
    materialization = MinerUHttpStagedV4(
        scratch_root=scratch_root, published_root=published_root, transport=unavailable, clock=time.time,
    )
    backend = DurableStagedCoordinatorBackendV4(
        persistence=persistence,
        inputs=_Unavailable("the stage input resolver"),
        remote=unavailable,
        materialization=materialization,
        secret_cipher=_Unavailable("the provider secret cipher"),
        claim_guard=DurableV4ClaimGuard(uow_factory=uow_factory),
        publisher=_Unavailable("publication"),  # type: ignore[arg-type]
        poll_seconds=1.0,
    )
    return ExpiredPreparedClosure(
        uow_factory=uow_factory, persistence=persistence, backend=backend,
        utc_now=utc_now, monotonic=monotonic, claim_lease_seconds=limits.claim_lease_seconds,
    )


def _compose_from_settings(
    settings: Settings, engine: sa.Engine, process_guard: Callable[[], None],
) -> ExpiredPreparedClosure:
    return compose(
        uow_factory=unit_of_work_factory(engine),
        scratch_root=settings.disclosure_runtime_root / "staged_v4" / "scratch",
        published_root=FileStorePathBuilder(settings).data_path(Path()),
        process_guard=process_guard,
    )


@contextmanager
def _worker_singleton(settings: Settings) -> Iterator[Connection]:
    """Hold the worker singleton for the whole closure: no worker runs meanwhile."""

    lock_engine = sa.create_engine(
        app_database_url(settings), poolclass=NullPool, isolation_level="AUTOCOMMIT",
    )
    try:
        with lock_engine.connect() as lock_conn:
            require_runtime_app_connection(lock_conn)
            if not lock_conn.execute(
                sa.text("SELECT pg_try_advisory_lock(:ns,0)"), {"ns": WORKER_NS}
            ).scalar_one():
                raise ExpiredPreparedClosureRefused("a worker holds the singleton; stop it first")
            yield lock_conn
    finally:
        lock_engine.dispose()


def run_preview(args: argparse.Namespace, closure: ExpiredPreparedClosure) -> dict[str, Any]:
    inventory_raw, inventory_sha = _read(args.inventory)
    lookups_raw, lookups_sha = _read(args.key_lookups)
    plan = closure.plan(
        inventory=decode_legacy_scope_inventory(inventory_raw), inventory_sha256=inventory_sha,
        key_lookups=decode_legacy_key_lookup_evidence(lookups_raw), key_lookups_sha256=lookups_sha,
        origin_runtime_identity_sha256=args.origin_runtime_identity_sha256,
        key_ttl_seconds=args.key_ttl_seconds,
    )
    _publish(args.out, plan.exact_bytes)
    return {
        "plan_sha256": plan.sha256,
        "closable": [item.attempt_id for item in plan.members],
        "still_valid": [attempt for attempt, _ in plan.still_valid],
    }


def run_execute(args: argparse.Namespace, closure: ExpiredPreparedClosure) -> dict[str, Any]:
    for name in ("decided_by", "reason"):
        if not str(getattr(args, name)).strip():
            raise ExpiredPreparedClosureRefused(f"--{name.replace('_', '-')} must not be blank")
    plan_raw, plan_sha = _read(args.plan)
    if plan_sha != args.expect_sha256:
        raise ExpiredPreparedClosureRefused("plan file is not the reviewed plan")
    plan: ExpiredPreparedClosurePlanV1 = decode_expired_prepared_closure_plan(plan_raw)
    inventory_raw, inventory_sha = _read(args.inventory)
    lookups_raw, lookups_sha = _read(args.key_lookups)
    closed = closure.execute(
        plan=plan,
        inventory=decode_legacy_scope_inventory(inventory_raw), inventory_sha256=inventory_sha,
        key_lookups=decode_legacy_key_lookup_evidence(lookups_raw), key_lookups_sha256=lookups_sha,
        decided_by=args.decided_by, reason=args.reason,
    )
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "plan_sha256": plan.sha256,
        "decision_sha256": closure_decision_sha256(
            plan_sha256=plan.sha256, decided_by=args.decided_by, reason=args.reason,
        ),
        "decided_by": args.decided_by,
        "reason": args.reason,
        "members": [
            {key: value for key, value in asdict(item).items() if key != "continued_from"}
            for item in closed
        ],
    }
    _publish(args.out, (json.dumps(receipt, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode())
    return {**receipt, "continued_from": {item.attempt_id: item.continued_from for item in closed}}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="disclosure-anchor expired-prepared-closure", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview", help="read-only plan of the closable expired members")
    preview.add_argument("--inventory", type=Path, required=True)
    preview.add_argument("--key-lookups", type=Path, required=True)
    preview.add_argument("--origin-runtime-identity-sha256", required=True)
    preview.add_argument("--key-ttl-seconds", type=int, required=True,
                         help="the origin API's actual key lifetime, read back from it")
    preview.add_argument("--out", type=Path, required=True)
    execute = commands.add_parser("execute", help="close exactly the reviewed plan")
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--expect-sha256", required=True)
    execute.add_argument("--inventory", type=Path, required=True)
    execute.add_argument("--key-lookups", type=Path, required=True)
    execute.add_argument("--decided-by", required=True, help="operator identity; never inferred")
    execute.add_argument("--reason", required=True)
    execute.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = load_settings()
    engine = create_db_engine(app_database_url(settings))
    try:
        require_runtime_app_engine(engine)
        if args.command == "preview":
            result = run_preview(args, _compose_from_settings(settings, engine, lambda: None))
        else:
            with _worker_singleton(settings) as lock_conn:
                closure = _compose_from_settings(settings, engine, lambda: _assert_staged_singleton(lock_conn))
                result = run_execute(args, closure)
    except (ExpiredPreparedClosureRefused, ValueError) as exc:
        print(json.dumps({"refused": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
