"""Regenerate the 06R derived retrieval projection (milestone 06R §4/§5).

Derived layer (U7 red line): every projection column regenerates
deterministically from persisted ``document_unit`` rows. This use case replaces
the projection tables and records/clears only the owning run's deterministic
projection terminal fact; it emits no outbox events and puts nothing into
content/query-projection hashes.

Both full and delta modes replace one complete ``processing_run`` per
transaction.  Delta selects a run when any active unit lacks the current
projection version; full selects every active run.  Orphan parents cascade
their body windows.  A database probe proves that every source lexeme
occurrence survives PostgreSQL's physical ``tsvector`` limits; unsafe bodies
are split by deterministic half-open token ranges, never by vocabulary.

Body text is replayed only from the unit's explicit ``search_targets`` source
projection. No payload-field discovery or content-shaped header guess is part
of retrieval. Each selected leaf is also stored as one normalized search atom;
leaves are never joined in that substring channel.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, TypeVar

from sqlalchemy import (
    Integer,
    Text,
    and_,
    column,
    delete,
    func,
    or_,
    select,
    update,
    values,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres.models import (
    DocumentUnit,
    ProcessingRun,
    UnitBodySearchWindow,
    UnitSearchAtom,
    UnitSearchRowAtom,
    UnitSearchProjection,
)
from disclosure_anchor.adapters.retrieval import tokenizer
from disclosure_anchor.application.contracts.html_visible_text import html_visible_text
from disclosure_anchor.application.contracts.provider_unit import (
    ProviderUnitSearchContractError,
)
from disclosure_anchor.application.services.semantic_taxonomy import (
    load_semantic_route_taxonomy,
)
from disclosure_anchor.application.services.provider_unit_builder import (
    provider_unit_search_row_atoms,
    provider_unit_search_text_values,
)
from disclosure_anchor.application.worker.locks import shared_corpus_mutation

# DML flush size inside one processing-run transaction.
_BATCH = 1000
_PROBE_BATCH = 256
_EMPTY_ROW_ATOM_MANIFEST_HASH = (
    "sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5"
    "ed12ab4d8e11ba873c2f11161202b945"
)

_UPDATE_COLUMNS = (
    "retrieval_rules_version",
    "title_text",
    "heading_path_text",
    "title_tokens",
    "path_tokens",
    "body_tokens",
    "key_tokens",
    "header_row_candidate",
    "body_search_windowed",
    "row_atom_manifest_ready",
    "row_atom_count",
    "row_atom_manifest_hash",
    "built_at",
)


@dataclass(frozen=True)
class BuildSearchProjectionCommand:
    # full=False is the incremental default (CLI default; worker always delta).
    full: bool = False
    # Orphan-prune gate for delta mode (full mode always prunes). Proving
    # "no orphans" is a corpus-sized anti-join (~16s live), while orphans can
    # only appear when a publish deactivates a run — so the worker passes the
    # round's deactivation signal here and skips the scan on quiet rounds.
    # A projection count exceeding the active-unit count forces the prune
    # regardless (orphans then provably exist).  If one orphan and one
    # missing row initially cancel in the counts, the exact delta drain adds
    # the missing row; the following round then proves projection > active
    # and prunes the orphan.
    prune: bool = True


@dataclass(frozen=True)
class BuildSearchProjectionResult:
    projected: int = 0
    deleted: int = 0
    skipped: int = 0
    failures: tuple[SearchProjectionFailure, ...] = ()


@dataclass(frozen=True)
class SearchProjectionFailure:
    processing_run_id: str
    error_code: str
    message: str


class SearchProjectionSafetyError(RuntimeError):
    """PostgreSQL cannot represent a source token occurrence losslessly."""


def _projection_error_code(
    error: SearchProjectionSafetyError | ProviderUnitSearchContractError,
) -> str:
    if isinstance(error, ProviderUnitSearchContractError):
        return "search_target_contract_invalid"
    prefix, separator, _detail = str(error).partition(":")
    return (
        prefix
        if separator and prefix.startswith("search_projection_")
        else "search_projection_safety_error"
    )


def _terminal_projection_error(
    error: SearchProjectionSafetyError | ProviderUnitSearchContractError,
    *,
    failed_at: datetime,
) -> dict[str, object]:
    return {
        "stage": "search_projection",
        "error_code": _projection_error_code(error),
        "message": str(error)[:2000],
        "retryable": False,
        "retrieval_rules_version": tokenizer.RETRIEVAL_RULES_VERSION,
        "failed_at": failed_at.isoformat(),
    }


@dataclass(frozen=True)
class _BodyRange:
    row_index: int
    start: int
    end: int


class BuildSearchProjection:
    """Recompute the search projection from active-run units.

    Uses a plain engine (not the UnitOfWork): one transaction locks the owning
    ``processing_run``, replaces its projection rows, and clears its projection
    terminal fact. Deterministic failures are recorded only after replacement
    rollback.
    """

    def __init__(self, *, engine: Engine) -> None:
        self._engine = engine

    # A processing run is the atomic replacement boundary.  The outer
    # keyset keeps corpus rebuilds bounded without splitting one run's parent
    # rows from its body windows.
    _RUN_BATCH_SIZE = 128
    # Orphan ids fetched per anti-join scan: bounds Python memory to a few MB
    # even when a rules-bump corpus rebuild deactivates runs corpus-wide
    # (orphan waves at unit scale), while the steady state pays one probe.
    _ORPHAN_SCAN_LIMIT = 50_000

    def execute(
        self,
        command: BuildSearchProjectionCommand,
        *,
        should_stop: Callable[[], bool] | None = None,
        on_progress: Callable[[], None] | None = None,
    ) -> BuildSearchProjectionResult:
        built_at = datetime.now(timezone.utc)
        projected = 0
        deleted = 0
        failures: tuple[SearchProjectionFailure, ...] = ()

        def stop_requested() -> bool:
            return should_stop is not None and should_stop()

        with shared_corpus_mutation(self._engine):
            with Session(self._engine) as session:
                should_prune = command.full or command.prune
                if not should_prune:
                    projection_count, active_count = self._counts(session)
                    should_prune = projection_count > active_count
                if should_prune and not stop_requested():
                    deleted = self._delete_orphans(session, should_stop=should_stop)

                # The first empty keyset batch is the exact caught-up proof.
                # Do not gate this anti-join on equal counts: one orphan plus
                # one missing row can make counts agree while evidence is
                # absent.
                if not stop_requested():
                    projected, failures = self._drain_runs(
                        session,
                        full=command.full,
                        built_at=built_at,
                        stop_requested=stop_requested,
                        on_progress=on_progress,
                    )
        return BuildSearchProjectionResult(
            projected=projected,
            deleted=deleted,
            skipped=0,
            failures=failures,
        )

    def _drain_runs(
        self,
        session: Session,
        *,
        full: bool,
        built_at: datetime,
        stop_requested: Callable[[], bool],
        on_progress: Callable[[], None] | None = None,
    ) -> tuple[int, tuple[SearchProjectionFailure, ...]]:
        """Replace complete processing runs, committing once per run."""

        projected = 0
        failures: list[SearchProjectionFailure] = []
        cursor: str | None = None
        while not stop_requested():
            run_ids = list(
                session.execute(
                    self._candidate_runs_stmt(full=full, after=cursor).limit(
                        self._RUN_BATCH_SIZE
                    )
                ).scalars()
            )
            session.commit()
            if not run_ids:
                break
            for processing_run_id in run_ids:
                if stop_requested():
                    return projected, tuple(failures)
                run_id = str(processing_run_id)
                try:
                    run_projected = self._replace_run(
                        session,
                        run_id,
                        built_at=built_at,
                    )
                except (
                    SearchProjectionSafetyError,
                    ProviderUnitSearchContractError,
                ) as exc:
                    recorded = self._record_terminal_error(
                        session,
                        run_id,
                        error=_terminal_projection_error(
                            exc,
                            failed_at=built_at,
                        ),
                    )
                    if recorded:
                        failures.append(
                            SearchProjectionFailure(
                                processing_run_id=run_id,
                                error_code=_projection_error_code(exc),
                                message=str(exc),
                            )
                        )
                    run_projected = 0
                cursor = run_id
                projected += run_projected
                if on_progress is not None:
                    on_progress()
            if len(run_ids) < self._RUN_BATCH_SIZE:
                break
        return projected, tuple(failures)

    # -- selection ----------------------------------------------------------
    def _candidate_runs_stmt(self, *, full: bool, after: str | None) -> Any:
        unit_exists = select(DocumentUnit.asset_id).where(
            DocumentUnit.processing_run_id == ProcessingRun.processing_run_id
        )
        if not full:
            body_window_exists = select(UnitBodySearchWindow.asset_id).where(
                UnitBodySearchWindow.asset_id == UnitSearchProjection.asset_id
            )
            row_atom_count = (
                select(func.count())
                .select_from(UnitSearchRowAtom)
                .where(UnitSearchRowAtom.asset_id == UnitSearchProjection.asset_id)
                .scalar_subquery()
            )
            stale_row_atom_exists = select(UnitSearchRowAtom.asset_id).where(
                UnitSearchRowAtom.asset_id == UnitSearchProjection.asset_id,
                UnitSearchRowAtom.row_atom_manifest_hash
                != UnitSearchProjection.row_atom_manifest_hash,
            )
            current_projection_exists = select(UnitSearchProjection.asset_id).where(
                UnitSearchProjection.asset_id == DocumentUnit.asset_id,
                UnitSearchProjection.retrieval_rules_version
                == tokenizer.RETRIEVAL_RULES_VERSION,
                UnitSearchProjection.row_atom_manifest_ready.is_(True),
                row_atom_count == UnitSearchProjection.row_atom_count,
                or_(
                    and_(
                        UnitSearchProjection.row_atom_count == 0,
                        UnitSearchProjection.row_atom_manifest_hash
                        == _EMPTY_ROW_ATOM_MANIFEST_HASH,
                    ),
                    and_(
                        UnitSearchProjection.row_atom_count > 0,
                        ~stale_row_atom_exists.exists(),
                    ),
                ),
                or_(
                    and_(
                        UnitSearchProjection.body_search_windowed.is_(False),
                        ~body_window_exists.exists(),
                    ),
                    and_(
                        UnitSearchProjection.body_search_windowed.is_(True),
                        body_window_exists.exists(),
                    ),
                ),
            )
            unit_exists = unit_exists.where(~current_projection_exists.exists())
        stmt = select(ProcessingRun.processing_run_id).where(
            ProcessingRun.is_active.is_(True),
            ProcessingRun.provider_document_relpath.is_not(None),
            ProcessingRun.normalized_ir_relpath.is_(None),
            unit_exists.exists(),
        )
        if not full:
            stmt = stmt.where(
                or_(
                    ProcessingRun.search_projection_error.is_(None),
                    ProcessingRun.search_projection_error[
                        "retrieval_rules_version"
                    ].as_string()
                    != tokenizer.RETRIEVAL_RULES_VERSION,
                )
            )
        if after is not None:
            stmt = stmt.where(ProcessingRun.processing_run_id > after)
        return stmt.order_by(ProcessingRun.processing_run_id)

    def _counts(self, session: Session) -> tuple[int, int]:
        # |projection| via index-only count; |active units| via the run
        # join. Under projection ⊆ active units (holds after a prune),
        # equal counts prove set equality without an anti-join probe.
        projection_count = int(
            session.execute(
                select(func.count()).select_from(UnitSearchProjection)
            ).scalar()
            or 0
        )
        active_count = int(
            session.execute(
                select(func.count())
                .select_from(DocumentUnit)
                .join(
                    ProcessingRun,
                    ProcessingRun.processing_run_id == DocumentUnit.processing_run_id,
                )
                .where(ProcessingRun.is_active.is_(True))
            ).scalar()
            or 0
        )
        return projection_count, active_count

    # -- mutation -----------------------------------------------------------
    def _replace_run(
        self,
        session: Session,
        processing_run_id: str,
        *,
        built_at: datetime,
    ) -> int:
        """Replace one active run's parents and windows in one transaction."""

        try:
            active = session.execute(
                select(ProcessingRun.is_active)
                .where(ProcessingRun.processing_run_id == processing_run_id)
                .with_for_update()
            ).scalar()
            if active is not True:
                session.commit()
                return 0

            units = _load_run_units(session, processing_run_id)
            computed_rows = [
                compute_search_projection_row(built_at=built_at, **unit)
                for unit in units
            ]
            atom_rows = [
                {
                    "asset_id": str(row["asset_id"]),
                    "atom_index": atom_index,
                    "atom_text": atom_text,
                }
                for row in computed_rows
                for atom_index, atom_text in enumerate(row.pop("body_atoms"))
            ]
            row_atom_rows = [
                {
                    "asset_id": str(row["asset_id"]),
                    "row_atom_index": row_atom_index,
                    **row_atom,
                }
                for row in computed_rows
                for row_atom_index, row_atom in enumerate(
                    row.pop("body_row_atoms")
                )
            ]
            prepared_rows, window_rows = _prepare_search_rows(session, computed_rows)
            row_atom_rows = _filter_safe_row_atoms(session, row_atom_rows)
            _attach_row_atom_manifests(prepared_rows, row_atom_rows)
            asset_ids = [str(row["asset_id"]) for row in prepared_rows]

            for asset_chunk in _chunked(asset_ids, _BATCH):
                session.execute(
                    delete(UnitBodySearchWindow).where(
                        UnitBodySearchWindow.asset_id.in_(asset_chunk)
                    )
                )
                session.execute(
                    delete(UnitSearchAtom).where(
                        UnitSearchAtom.asset_id.in_(asset_chunk)
                    )
                )
                session.execute(
                    delete(UnitSearchRowAtom).where(
                        UnitSearchRowAtom.asset_id.in_(asset_chunk)
                    )
                )
            upsert_stmt = _upsert_statement()
            for row_chunk in _chunked(prepared_rows, _BATCH):
                session.execute(upsert_stmt, list(row_chunk))
            for window_chunk in _chunked(window_rows, _BATCH):
                session.execute(
                    pg_insert(UnitBodySearchWindow),
                    list(window_chunk),
                )
            for atom_chunk in _chunked(atom_rows, _BATCH):
                session.execute(pg_insert(UnitSearchAtom), list(atom_chunk))
            for row_atom_chunk in _chunked(row_atom_rows, _BATCH):
                session.execute(
                    pg_insert(UnitSearchRowAtom),
                    list(row_atom_chunk),
                )
            session.execute(
                update(ProcessingRun)
                .where(
                    ProcessingRun.processing_run_id == processing_run_id,
                    ProcessingRun.is_active.is_(True),
                )
                .values(search_projection_error=None)
            )
            session.commit()
            return len(prepared_rows)
        except BaseException:
            session.rollback()
            raise

    def _record_terminal_error(
        self,
        session: Session,
        processing_run_id: str,
        *,
        error: dict[str, object],
    ) -> bool:
        """Persist a deterministic run failure after replacement rollback."""

        try:
            result = session.connection().execute(
                update(ProcessingRun)
                .where(
                    ProcessingRun.processing_run_id == processing_run_id,
                    ProcessingRun.is_active.is_(True),
                )
                .values(search_projection_error=error)
            )
            session.commit()
        except BaseException:
            session.rollback()
            raise
        return int(result.rowcount or 0) == 1

    def _delete_orphans(
        self, session: Session, *, should_stop: Callable[[], bool] | None = None
    ) -> int:
        """Prune projection rows whose unit left the active set — batched.

        Design §5 applies to the delete too: a rules-bump corpus rebuild
        deactivates old runs corpus-wide, so the orphan set can reach unit
        scale in one wave. One unbatched DELETE would ride a single giant
        transaction (WAL spike, long row locks); instead fetch ids in bounded
        scans and delete in bounded chunks with a commit per chunk.
        Deleting a live unit's row is impossible to make permanent: each
        chunk deletes only ids the committed snapshot saw as inactive, and
        any row racing back to active is re-added by the next delta pass.
        """

        # NOT EXISTS, never NOT IN (subquery): review measurement 2026-07-24
        # — the NOT IN form planned as a correlated subplan rescanning a
        # 1.64M-row materialized hash join per projection row (EXPLAIN cost
        # 10.5e9; EXPLAIN ANALYZE did not finish in 400s at zero orphans).
        # The anti-join form walks the projection PK probing the unit PK.
        is_active_unit = (
            select(DocumentUnit.asset_id)
            .join(
                ProcessingRun,
                ProcessingRun.processing_run_id == DocumentUnit.processing_run_id,
            )
            .where(
                DocumentUnit.asset_id == UnitSearchProjection.asset_id,
                ProcessingRun.is_active.is_(True),
            )
            .exists()
        )
        deleted = 0
        while True:
            orphan_ids = list(
                session.execute(
                    select(UnitSearchProjection.asset_id)
                    .where(~is_active_unit)
                    .order_by(UnitSearchProjection.asset_id)
                    .limit(self._ORPHAN_SCAN_LIMIT)
                ).scalars()
            )
            if not orphan_ids:
                return deleted
            for start in range(0, len(orphan_ids), _BATCH):
                if should_stop is not None and should_stop():
                    return deleted
                chunk = orphan_ids[start : start + _BATCH]
                # Route DML through the session's transactional connection:
                # its ``CursorResult.rowcount`` is the prune count (the ORM
                # ``Session.execute`` return type does not expose rowcount).
                result = session.connection().execute(
                    delete(UnitSearchProjection).where(
                        UnitSearchProjection.asset_id.in_(chunk)
                    )
                )
                deleted += int(result.rowcount or 0)
                session.commit()
            if len(orphan_ids) < self._ORPHAN_SCAN_LIMIT:
                return deleted
            if should_stop is not None and should_stop():
                return deleted


def _load_run_units(session: Session, processing_run_id: str) -> list[dict[str, Any]]:
    stmt = (
        select(
            DocumentUnit.asset_id,
            DocumentUnit.title,
            DocumentUnit.heading_path,
            DocumentUnit.payload_kind,
            DocumentUnit.payload,
            DocumentUnit.semantic_keys,
            DocumentUnit.section_keys,
            DocumentUnit.artifact_locator,
        )
        .where(DocumentUnit.processing_run_id == processing_run_id)
        .order_by(DocumentUnit.asset_id)
    )
    return [
        {
            "asset_id": row.asset_id,
            "title": row.title,
            "heading_path": row.heading_path,
            "payload_kind": row.payload_kind,
            "payload": row.payload,
            "semantic_keys": row.semantic_keys,
            "section_keys": row.section_keys,
            "artifact_locator": row.artifact_locator,
        }
        for row in session.execute(stmt).all()
    ]


def _prepare_search_rows(
    session: Session,
    parent_rows: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Prove every parent vector and derive lossless body windows when needed."""

    prepared = [dict(row) for row in parent_rows]
    full_safety = _probe_search_vector_safety(
        session,
        [
            (
                str(row["title_tokens"]),
                str(row["path_tokens"]),
                str(row["body_tokens"]),
                str(row["key_tokens"]),
            )
            for row in prepared
        ],
    )
    unsafe_indices: list[int] = []
    for row_index, safe in enumerate(full_safety):
        prepared[row_index]["body_search_windowed"] = not safe
        if not safe:
            unsafe_indices.append(row_index)
    if not unsafe_indices:
        return prepared, []

    metadata_safety = _probe_search_vector_safety(
        session,
        [
            (
                str(prepared[row_index]["title_tokens"]),
                str(prepared[row_index]["path_tokens"]),
                "",
                str(prepared[row_index]["key_tokens"]),
            )
            for row_index in unsafe_indices
        ],
    )
    tokens_by_row: dict[int, tuple[str, ...]] = {}
    for row_index, safe in zip(unsafe_indices, metadata_safety, strict=True):
        asset_id = str(prepared[row_index]["asset_id"])
        if not safe:
            raise SearchProjectionSafetyError(
                f"search_projection_metadata_vector_unsafe: asset_id={asset_id}"
            )
        body_tokens = str(prepared[row_index]["body_tokens"])
        tokens = tuple(body_tokens.split(" ")) if body_tokens else ()
        if (
            not tokens
            or any(not token for token in tokens)
            or " ".join(tokens) != body_tokens
        ):
            raise SearchProjectionSafetyError(
                f"search_projection_empty_or_noncanonical_body: asset_id={asset_id}"
            )
        tokens_by_row[row_index] = tokens

    pending = [
        _BodyRange(row_index=row_index, start=0, end=len(tokens))
        for row_index, tokens in tokens_by_row.items()
    ]
    accepted: dict[int, list[_BodyRange]] = {
        row_index: [] for row_index in unsafe_indices
    }
    while pending:
        range_safety = _probe_search_vector_safety(
            session,
            [
                (
                    "",
                    "",
                    " ".join(tokens_by_row[item.row_index][item.start : item.end]),
                    "",
                )
                for item in pending
            ],
        )
        next_pending: list[_BodyRange] = []
        for item, safe in zip(pending, range_safety, strict=True):
            if safe:
                accepted[item.row_index].append(item)
                continue
            if item.end - item.start == 1:
                token = tokens_by_row[item.row_index][item.start]
                raise SearchProjectionSafetyError(
                    "search_projection_unsplittable_body_token: "
                    f"asset_id={prepared[item.row_index]['asset_id']} "
                    f"token_index={item.start} "
                    f"token_utf8_bytes={len(token.encode('utf-8'))}"
                )
            midpoint = item.start + (item.end - item.start) // 2
            next_pending.extend(
                (
                    _BodyRange(item.row_index, item.start, midpoint),
                    _BodyRange(item.row_index, midpoint, item.end),
                )
            )
        pending = next_pending

    window_rows: list[dict[str, Any]] = []
    for row_index in unsafe_indices:
        tokens = tokens_by_row[row_index]
        ranges = sorted(
            accepted[row_index],
            key=lambda item: (item.start, item.end),
        )
        cursor = 0
        for window_index, item in enumerate(ranges):
            if item.start != cursor or item.end <= item.start:
                raise SearchProjectionSafetyError(
                    "search_projection_window_coverage_gap: "
                    f"asset_id={prepared[row_index]['asset_id']}"
                )
            window_rows.append(
                {
                    "asset_id": str(prepared[row_index]["asset_id"]),
                    "window_index": window_index,
                    "body_token_start": item.start,
                    "body_token_end": item.end,
                    "body_tokens": " ".join(tokens[item.start : item.end]),
                }
            )
            cursor = item.end
        if cursor != len(tokens):
            raise SearchProjectionSafetyError(
                "search_projection_window_coverage_incomplete: "
                f"asset_id={prepared[row_index]['asset_id']}"
            )
    return prepared, window_rows


def _probe_search_vector_safety(
    session: Session,
    candidates: Sequence[tuple[str, str, str, str]],
) -> list[bool]:
    """Bulk-evaluate PostgreSQL's exact source-occurrence safety predicate."""

    results: list[bool | None] = [None] * len(candidates)
    for start in range(0, len(candidates), _PROBE_BATCH):
        chunk = candidates[start : start + _PROBE_BATCH]
        candidate_values = values(
            column("ordinal", Integer),
            column("title_tokens", Text),
            column("path_tokens", Text),
            column("body_tokens", Text),
            column("key_tokens", Text),
            name="search_probe",
        ).data([(start + offset, *candidate) for offset, candidate in enumerate(chunk)])
        statement = (
            select(
                candidate_values.c.ordinal,
                func.disclosure_core.search_tsvector_is_safe(
                    candidate_values.c.title_tokens,
                    candidate_values.c.path_tokens,
                    candidate_values.c.body_tokens,
                    candidate_values.c.key_tokens,
                ).label("is_safe"),
            )
            .select_from(candidate_values)
            .order_by(candidate_values.c.ordinal)
        )
        for ordinal, is_safe in session.execute(statement):
            results[int(ordinal)] = bool(is_safe)
    if any(result is None for result in results):
        raise SearchProjectionSafetyError("search_projection_safety_probe_incomplete")
    return [bool(result) for result in results]


def _filter_safe_row_atoms(
    session: Session,
    row_atom_rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only row atoms that PostgreSQL can index without physical loss.

    A strict Q&A row is an optional retrieval enhancement.  The parent Unit,
    its source leaves, and lossless body windows remain the recall fallback,
    so an over-limit row must not abort or poison the owning processing run.
    Stored row indices are reassigned densely per Unit after safe omission;
    ``source_row_index`` remains the immutable table-row address.
    """

    safety = _probe_search_vector_safety(
        session,
        [
            ("", "", str(row["row_tokens"]), "")
            for row in row_atom_rows
        ],
    )
    next_index_by_asset: dict[str, int] = {}
    safe_rows: list[dict[str, Any]] = []
    for row, safe in zip(row_atom_rows, safety, strict=True):
        if not safe:
            continue
        asset_id = str(row["asset_id"])
        row_atom_index = next_index_by_asset.get(asset_id, 0)
        next_index_by_asset[asset_id] = row_atom_index + 1
        safe_rows.append({**row, "row_atom_index": row_atom_index})
    return safe_rows


def _attach_row_atom_manifests(
    parent_rows: Sequence[dict[str, Any]],
    row_atom_rows: Sequence[dict[str, Any]],
) -> None:
    """Bind each parent and child to the exact safe row-atom set."""

    rows_by_asset: dict[str, list[dict[str, Any]]] = {}
    for row in row_atom_rows:
        rows_by_asset.setdefault(str(row["asset_id"]), []).append(row)
    for parent in parent_rows:
        asset_id = str(parent["asset_id"])
        rows = rows_by_asset.get(asset_id, [])
        manifest_payload = [
            {
                "row_atom_index": int(row["row_atom_index"]),
                "table_target_id": str(row["table_target_id"]),
                "source_row_index": int(row["source_row_index"]),
                "row_text": str(row["row_text"]),
                "row_tokens": str(row["row_tokens"]),
            }
            for row in rows
        ]
        manifest_bytes = json.dumps(
            manifest_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        manifest_hash = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        parent["row_atom_count"] = len(rows)
        parent["row_atom_manifest_hash"] = manifest_hash
        parent["row_atom_manifest_ready"] = True
        for row in rows:
            row["row_atom_manifest_hash"] = manifest_hash


def _upsert_statement() -> Any:
    stmt = pg_insert(UnitSearchProjection)
    excluded = stmt.excluded
    return stmt.on_conflict_do_update(
        index_elements=["asset_id"],
        set_={column: excluded[column] for column in _UPDATE_COLUMNS},
    )


_ROUTE_LABEL_TOKENS: dict[str, str] | None = None


def _route_label_tokens() -> Mapping[str, str]:
    """Chinese canonical-label tokens per closed route key, computed once."""

    global _ROUTE_LABEL_TOKENS
    if _ROUTE_LABEL_TOKENS is None:
        _ROUTE_LABEL_TOKENS = {
            definition.key: tokenizer.index_word_tokens(definition.labels[0])
            for definition in load_semantic_route_taxonomy().definitions
        }
    return _ROUTE_LABEL_TOKENS


def _direct_key_tokens(semantic_keys: Sequence[str] | None) -> str:
    keys = list(dict.fromkeys(semantic_keys or ()))
    label_tokens: list[str] = []
    for key in keys:
        tokens = _route_label_tokens().get(key)
        if tokens:
            label_tokens.extend(tokens.split(" "))
    return " ".join(dict.fromkeys((*keys, *label_tokens)))


_T = TypeVar("_T")


def _chunked(items: Sequence[_T], size: int) -> Iterator[Sequence[_T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# -- pure deterministic row computation (milestone 06R §4) ------------------
def compute_search_projection_row(
    *,
    asset_id: str,
    title: str | None,
    heading_path: Any,
    payload_kind: str,
    payload: Mapping[str, Any] | None,
    semantic_keys: Sequence[str] | None,
    artifact_locator: Mapping[str, Any] | None,
    built_at: datetime,
    section_keys: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Deterministic projection row for one unit (no ``search_tsv`` — generated)."""

    title_text = title or ""
    heading_path_text = " > ".join(str(item) for item in heading_path or [])
    body_atoms = tuple(
        normalized
        for value in provider_unit_search_text_values(
            payload_kind=payload_kind,
            payload=payload or {},
            title=title,
            artifact_locator=artifact_locator,
        )
        if (normalized := tokenizer.normalize_search_text(value)).strip()
    )
    body_row_atoms = tuple(
        {
            "table_target_id": atom.table_target_id,
            "source_row_index": atom.source_row_index,
            "row_text": tokenizer.normalize_search_text(atom.row_text),
            "row_tokens": tokenizer.index_word_tokens(atom.row_text),
        }
        for atom in provider_unit_search_row_atoms(
            payload_kind=payload_kind,
            payload=payload or {},
            title=title,
            artifact_locator=artifact_locator,
        )
    )
    title_token_text = html_visible_text(title_text)
    path_token_text = " > ".join(
        html_visible_text(str(item)) for item in heading_path or []
    )
    body_token_text = " ".join(html_visible_text(atom) for atom in body_atoms)
    return {
        "asset_id": asset_id,
        "retrieval_rules_version": tokenizer.RETRIEVAL_RULES_VERSION,
        "title_text": title_text,
        "heading_path_text": heading_path_text,
        "title_tokens": tokenizer.index_word_tokens(title_token_text),
        "path_tokens": tokenizer.index_word_tokens(path_token_text),
        "body_tokens": tokenizer.index_word_tokens(body_token_text),
        # Private handoff to the run-atomic child insert; not a parent column.
        "body_atoms": body_atoms,
        # Private handoff for strict, source-bound Q&A row colocation.  These
        # derived rows never alter the Unit payload, identity, or hashes.
        "body_row_atoms": body_row_atoms,
        # Controlled semantic routes bypass natural-language segmentation.
        # Direct Unit themes are a ranked search signal.  Structural section
        # context remains separately filterable and must not compete at the
        # same weight in the full-text key channel.  Each direct key also
        # projects its Chinese canonical label tokens so a lexical Chinese
        # query reaches the carrier even when the label word never occurs in
        # the Unit's own title/body (e.g. 存货 for a dotted policy child).
        "key_tokens": _direct_key_tokens(semantic_keys),
        # Retained until the DB compatibility column is retired. New
        # projections never infer a header role from cell text.
        "header_row_candidate": False,
        "built_at": built_at,
    }
