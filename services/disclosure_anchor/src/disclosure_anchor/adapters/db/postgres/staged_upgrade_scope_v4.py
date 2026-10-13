"""Read-only legacy scope for one verified local execution upgrade.

"Unresolved" is every current V4 head: current heads are always in a
non-final resource state (prepared through ack_pending), globally, with no
owner or document filter. A non-current ``prepared`` V4 row (a staged
superseder H0 awaiting activation) is a latent obligation this scope cannot
follow, so capture and verification refuse while any exists. The snapshot runs
in one READ ONLY REPEATABLE READ transaction under the runtime role, takes no
row or advisory locks and never reads capability plaintext. Closure for new H0
admission is re-read from the same authoritative rows; recovery-scan
completion is never used for it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres import models
from disclosure_anchor.adapters.db.postgres.connection import require_runtime_app_connection
from disclosure_anchor.adapters.db.postgres.remote_parse_v4_repository import RemoteParseV4Repository
from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    LegacyExecutionRefused,
    LegacyObligationsOpen,
    LegacyScopeInventory,
    LegacyScopeMember,
    VerifiedQualifiedExecution,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RemoteParseV4Authority,
    V4HeadNotFound,
)
from disclosure_anchor.application.worker.locks import WORKER_NS

_FINAL_STATES = frozenset(
    {"acked", "remote_failed", "local_failed", "pre_submission_failed", "preparation_failed", "superseded"}
)
_PAGE = 500


def _require_worker_stopped(connection: Connection) -> None:
    held = connection.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
        "AND database=(SELECT oid FROM pg_database WHERE datname=current_database()) "
        "AND classid=:namespace AND objid=0 AND objsubid=2 AND granted)"
    ), {"namespace": WORKER_NS}).scalar_one()
    if held:
        raise LegacyExecutionRefused("legacy scope capture requires the worker singleton to be stopped")


@contextmanager
def read_only_repository(
    engine: Engine, *, require_worker_stopped: bool = False,
) -> Iterator[tuple[RemoteParseV4Repository, datetime, dict[str, datetime]]]:
    """One READ ONLY REPEATABLE READ snapshot; always rolled back."""

    with engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        connection.exec_driver_sql("SET LOCAL statement_timeout='60s'")
        require_runtime_app_connection(connection)
        info = connection.execute(sa.text(
            "SELECT current_setting('transaction_read_only') AS read_only, "
            "current_setting('transaction_isolation') AS isolation, "
            "transaction_timestamp() AS observed_at"
        )).mappings().one()
        if info["read_only"] != "on" or info["isolation"] != "repeatable read":
            raise RuntimeError("legacy scope snapshot is not READ ONLY REPEATABLE READ")
        observed_at = info["observed_at"]
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            raise RuntimeError("legacy scope snapshot clock is invalid")
        try:
            if require_worker_stopped:
                _require_worker_stopped(connection)
            table = models.RemoteParseAttempt.__table__
            created_at_by_attempt = dict(connection.execute(
                sa.select(table.c.attempt_id, table.c.created_at).where(
                    table.c.is_current.is_(True), table.c.checkpoint_contract_version == 4,
                )
            ).tuples().all())
            with Session(bind=connection, autoflush=False) as session:
                yield RemoteParseV4Repository(session), observed_at.astimezone(UTC), created_at_by_attempt
            if require_worker_stopped:
                _require_worker_stopped(connection)
        finally:
            connection.rollback()


def observe_unresolved_heads(repository: RemoteParseV4Repository) -> tuple[RemoteParseV4Authority, ...]:
    """Every current V4 head, exhaustively, by the recovery keyset order."""

    heads: list[RemoteParseV4Authority] = []
    cursor: str | None = None
    while True:
        page = repository.list_recoverable_heads(after_attempt_id=cursor, limit=_PAGE)
        for candidate in page:
            authority = repository.observe(candidate.attempt_id)
            if not authority.is_current or authority.state in _FINAL_STATES:
                raise RuntimeError(f"unresolved head {candidate.attempt_id} changed inside one snapshot")
            heads.append(authority)
        if len(page) < _PAGE:
            return tuple(heads)
        cursor = page[-1].attempt_id


def legacy_member_from_authority(authority: RemoteParseV4Authority) -> LegacyScopeMember:
    spec = authority.execution_spec
    if spec is None:
        raise RuntimeError(f"unresolved head {authority.attempt_id} lacks its exact execution spec")
    return LegacyScopeMember(
        attempt_id=authority.attempt_id,
        document_id=authority.document_id,
        processing_run_id=authority.processing_run_id,
        attempt_generation=authority.attempt_generation,
        fence_identity=authority.fence_identity,
        h0_checkpoint_sha256=authority.checkpoint_history[0].sha256,
        execution_spec_sha256=spec.sha256,
        source_pdf_sha256=authority.source_pdf_sha256,
        parser_target_sha256=authority.parser_target_sha256,
        request_sha256=authority.request_sha256,
        runtime_epoch_sha256=authority.runtime_epoch_sha256,
        client_submit_key=authority.client_submit_key,
        submission_epoch_unix=spec.prepared_submission.submission_epoch_unix,
        process_profile_sha256=spec.process_profile_sha256,
        worker_profile_sha256=spec.worker_profile.sha256,
        observed_state=authority.state,
        observed_lifecycle_version=authority.lifecycle_version,
        observed_checkpoint_sha256=authority.checkpoint_sha256,
        accepted_submission_sha256=authority.checkpoint.accepted_submission_sha256,
    )


def require_no_staged_heads(repository: RemoteParseV4Repository) -> None:
    staged = repository.count_staged_prepared_heads()
    if staged:
        raise LegacyExecutionRefused(
            f"{staged} staged (non-current) prepared V4 head(s) exist; a verified legacy scope "
            "cannot cover their later activation"
        )


def capture_legacy_scope_inventory(engine: Engine) -> LegacyScopeInventory:
    """Immutable pre-deployment evidence of every unresolved responsibility."""

    with read_only_repository(engine, require_worker_stopped=True) as (repository, observed_at, _):
        require_no_staged_heads(repository)
        heads = observe_unresolved_heads(repository)
    members = tuple(sorted((legacy_member_from_authority(item) for item in heads), key=lambda m: m.attempt_id))
    return LegacyScopeInventory(
        captured_at_utc=observed_at.isoformat(),
        members=members,
    )


@dataclass(frozen=True, slots=True)
class LegacyScopeObservation:
    """One re-verification of the whole inventory against authoritative rows.

    ``closed_state_counts`` counts closed members by final state. A recorded
    failure closure (for example ``local_failed``) is a legitimate final
    state of the original scope, reported beside the successes.
    """

    observed_at: datetime
    unresolved_members: tuple[str, ...]
    closed_members: tuple[str, ...]
    state_counts: tuple[tuple[str, int], ...]
    current_execution_heads: tuple[str, ...] = ()
    closed_state_counts: tuple[tuple[str, int], ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at.isoformat(),
            "unresolved_member_count": len(self.unresolved_members),
            "closed_member_count": len(self.closed_members),
            "unresolved_state_counts": dict(self.state_counts),
            "closed_state_counts": dict(self.closed_state_counts),
            "current_execution_head_count": len(self.current_execution_heads),
        }


def verify_legacy_scope(
    repository: RemoteParseV4Repository,
    execution: VerifiedQualifiedExecution,
    *,
    observed_at: datetime,
    unresolved: tuple[RemoteParseV4Authority, ...],
    created_at_by_attempt: Mapping[str, datetime] | None = None,
) -> LegacyScopeObservation:
    """Every member is an exact continuation; nothing else is legacy.

    A member may have progressed or reached a final state. While any member is
    still open, no head outside the inventory may exist (new H0 is held until
    closure, so one would be premature). An outside head must have been created
    strictly after the capture on the same database clock, even when the
    release changes without changing the runtime identity. After closure it
    must also be bound exactly to the verified current execution. A missing member, a
    changed H0/spec/request/key/profile, a rewritten checkpoint history, any
    other head or any staged non-current H0 is refused.
    """

    require_no_staged_heads(repository)
    counts: dict[str, int] = {}
    unresolved_ids: list[str] = []
    outside: list[RemoteParseV4Authority] = []
    for authority in unresolved:
        if not execution.is_member(authority.attempt_id):
            require_post_capture_head(
                execution, authority.attempt_id, observed_at=observed_at,
                created_at_by_attempt=created_at_by_attempt,
            )
            outside.append(authority)
            continue
        execution.require_member_progress(authority)
        unresolved_ids.append(authority.attempt_id)
        counts[authority.state] = counts.get(authority.state, 0) + 1
    if outside and unresolved_ids:
        raise LegacyExecutionRefused(
            f"{len(outside)} unresolved head(s) outside the verified legacy scope exist while "
            f"{len(unresolved_ids)} original obligation(s) remain open (first {outside[0].attempt_id}); "
            "new work before legacy closure is premature"
        )
    current_ids: list[str] = []
    for authority in outside:
        execution.require_current_execution(authority)
        current_ids.append(authority.attempt_id)
    closed: list[str] = []
    closed_counts: dict[str, int] = {}
    open_ids = set(unresolved_ids)
    for attempt_id in execution.member_attempt_ids:
        if attempt_id in open_ids:
            continue
        try:
            authority = repository.observe(attempt_id)
        except V4HeadNotFound as exc:
            raise LegacyExecutionRefused(f"legacy obligation {attempt_id} disappeared") from exc
        if authority.is_current or authority.state not in _FINAL_STATES:
            raise LegacyExecutionRefused(
                f"legacy obligation {attempt_id} is neither unresolved nor final"
            )
        execution.require_member_progress(authority)
        closed.append(attempt_id)
        closed_counts[authority.state] = closed_counts.get(authority.state, 0) + 1
    return LegacyScopeObservation(
        observed_at=observed_at,
        unresolved_members=tuple(unresolved_ids),
        closed_members=tuple(closed),
        state_counts=tuple(sorted(counts.items())),
        current_execution_heads=tuple(current_ids),
        closed_state_counts=tuple(sorted(closed_counts.items())),
    )


def require_post_capture_head(
    execution: VerifiedQualifiedExecution, attempt_id: str, *, observed_at: datetime,
    created_at_by_attempt: Mapping[str, datetime] | None,
) -> None:
    """Distinguish new work from an omitted old duty without another ledger.

    Both timestamps originate from PostgreSQL transaction_timestamp(), never
    from a provider or the worker host. Capture requires a stopped producer;
    deployment must keep it stopped until the approved boot. This is not a
    monotonic clock or a substitute for that quiescence discipline.
    """

    created_at = (created_at_by_attempt or {}).get(attempt_id)
    captured_at = datetime.fromisoformat(execution.inventory.captured_at_utc)
    if (
        not isinstance(created_at, datetime) or created_at.tzinfo is None
        or created_at.utcoffset() is None or observed_at.tzinfo is None
        or observed_at.utcoffset() is None or created_at > observed_at
    ):
        raise LegacyExecutionRefused(
            f"unresolved non-member {attempt_id} lacks valid database creation time; "
            "legacy inventory completeness cannot be verified"
        )
    if created_at <= captured_at:
        raise LegacyExecutionRefused(
            f"unresolved non-member {attempt_id} existed before or at legacy scope capture; "
            "the verified legacy inventory is incomplete"
        )


def require_legacy_scope(engine: Engine, execution: VerifiedQualifiedExecution) -> LegacyScopeObservation:
    """In-worker recheck under the singleton, before any business effect."""

    with read_only_repository(engine) as (repository, observed_at, created_at_by_attempt):
        unresolved = observe_unresolved_heads(repository)
        return verify_legacy_scope(
            repository, execution, observed_at=observed_at, unresolved=unresolved,
            created_at_by_attempt=created_at_by_attempt,
        )


class PostgresLegacyObligationsGate:
    """Hold new H0 creation until every inventory member is final.

    The closed result is cached only after the authoritative rows prove it;
    a final state is terminal, so closure cannot reopen.
    """

    def __init__(self, *, engine: Engine, execution: VerifiedQualifiedExecution) -> None:
        if type(execution) is not VerifiedQualifiedExecution:
            raise ValueError("legacy obligations gate requires the verified execution")
        self._engine = engine
        self._attempt_ids = execution.member_attempt_ids
        self._closed = not self._attempt_ids

    @property
    def closed(self) -> bool:
        return self._closed

    def require_closed(self) -> None:
        if self._closed:
            return
        table = models.RemoteParseAttempt.__table__
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(table.c.attempt_id, table.c.state, table.c.is_current).where(
                    table.c.attempt_id.in_(self._attempt_ids)
                )
            ).mappings().all()
        seen = {row["attempt_id"]: row for row in rows}
        if set(seen) != set(self._attempt_ids):
            raise LegacyExecutionRefused("a legacy obligation disappeared before closure")
        still_open = sum(
            1 for row in seen.values() if row["is_current"] or row["state"] not in _FINAL_STATES
        )
        if still_open:
            raise LegacyObligationsOpen(
                f"legacy obligations open ({still_open}/{len(self._attempt_ids)})"
            )
        self._closed = True


__all__ = [
    "LegacyScopeObservation",
    "PostgresLegacyObligationsGate",
    "capture_legacy_scope_inventory",
    "legacy_member_from_authority",
    "observe_unresolved_heads",
    "read_only_repository",
    "require_legacy_scope",
    "require_no_staged_heads",
    "verify_legacy_scope",
]
