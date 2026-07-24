"""Regenerate the 06R derived retrieval projection (milestone 06R §4/§5).

Derived layer (U7 red line): every projection column regenerates
deterministically from the persisted ``document_unit`` rows via the pinned
application-side jieba tokenizer. This use case writes only
``disclosure_core.unit_search_projection``; it emits no outbox events and puts
nothing into content/query-projection hashes.

Two modes:

* ``full``  — recompute every active-run unit (upsert), then delete projection
  rows whose ``asset_id`` no longer belongs to an active-run unit.
* delta     — recompute units missing a projection row (index-ordered merge
  anti-join pass), then rows carrying a stale ``retrieval_rules_version``
  (keyset pass); the worker drains it fully every round. Both modes end with
  a batched orphan prune.

Row computation is factored into pure module-level functions so the
linearization and ``header_row_candidate`` rules are testable without a DB.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres.models import (
    DocumentUnit,
    ProcessingRun,
    UnitSearchProjection,
)
from disclosure_anchor.adapters.retrieval import tokenizer
from disclosure_anchor.domain import ids

# Upsert flush size inside one keyset batch (memory bound on payload rows);
# the outer keyset batch (_BATCH_SIZE) commits per batch — see execute().
_BATCH = 1000

# Milestone 06R §4: a numeric-shaped cell after strip. Currency-magnitude words
# are intentionally NOT part of this test — see the module note in the task
# report; the annotation is a fail-safe diagnostic, so a conservative test only
# flags fewer candidates.
_NUMERIC_CELL_RE = re.compile(r"^[-+]?[\d,.]+%?$")
# Controlled magnitude suffixes from the unit-declaration vocabulary
# (rules._DECL_MAGNITUDE); kept as a literal mirror because application code
# must not import the adapter's private regex fragments.
_MAGNITUDE_SUFFIX_RE = re.compile(r"\s*(?:元|千元|万元|百万元|亿元)$")

_UPDATE_COLUMNS = (
    "retrieval_rules_version",
    "title_text",
    "heading_path_text",
    "title_tokens",
    "path_tokens",
    "body_tokens",
    "key_tokens",
    "header_row_candidate",
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
    # regardless (orphans then provably exist). Residual: orphans from a
    # crash between publish and projection can outlive their round until the
    # next deactivation round; bounded, and erased by the write-through
    # design (§8.1 trigger).
    prune: bool = True
    # Upper bound on rows projected this call. None (the worker and CLI
    # default) drains everything the delta finds: maintenance work must be
    # proportional to new/changed units, never capped by a fixed constant —
    # a borrowed document-scale limit once starved this unit-scale rebuild
    # to 48% search coverage (2026-07-23). A bound remains available for
    # explicitly time-boxed invocations; the remainder is reported as
    # ``skipped``.
    limit: int | None = None


@dataclass(frozen=True)
class BuildSearchProjectionResult:
    projected: int = 0
    deleted: int = 0
    skipped: int = 0


class BuildSearchProjection:
    """Recompute the search projection from active-run units.

    Uses a plain engine (not the UnitOfWork): it reads ``document_unit`` /
    ``processing_run`` and writes only the projection table, so it needs no
    repository wiring.
    """

    def __init__(self, *, engine: Engine) -> None:
        self._engine = engine

    # Discourse-style bounded reindex: keyset batches with a commit per
    # batch, so a corpus-scale rebuild never rides one giant transaction
    # (pinning vacuum, ballooning WAL, restarting from zero on failure) and
    # never materializes millions of asset ids into Python
    # (design: retrieval-scale-hardening.md §5). A full run is therefore not
    # one MVCC snapshot: units activated below the moving cursor by a
    # concurrent publish are picked up by the next delta round, and the
    # doctor coverage check alerts if that ever stops converging.
    _BATCH_SIZE = 2000
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
        skipped = 0
        remaining = command.limit
        cursor: str | None = None

        def stop_requested() -> bool:
            return should_stop is not None and should_stop()

        stale_cursor: str | None = None
        with Session(self._engine) as session:
            if command.full:
                projected, remaining, cursor = self._drain_keyset(
                    session,
                    self._active_units_stmt,
                    built_at=built_at,
                    remaining=remaining,
                    stop_requested=stop_requested,
                    on_progress=on_progress,
                )
                deleted = self._delete_orphans(session, should_stop=should_stop)
            else:
                # Order and gating carry the steady-state cost (§8.2 review,
                # 2026-07-24: every "prove the delta is empty" scan is
                # corpus-sized, so each one must be gated or range-pruned):
                #
                # 1. Prune orphans only when they can exist — the caller's
                #    deactivation signal (command.prune) or a projection
                #    count exceeding the active count. Pruning before the
                #    other passes restores projection ⊆ active units (count
                #    gate exact) and keeps the stale pass from re-stamping
                #    rows whose run was just deactivated (they are gone).
                # 2. Count gate: |projection| == |active units| under ⊆
                #    means nothing is missing — skip the missing pass
                #    without the corpus-wide anti-join probe (measured 19s
                #    when caught up). The one blind spot — orphans and
                #    missing rows in equal numbers — self-heals on the next
                #    unequal round and is backstopped by the doctor
                #    coverage alarm.
                # 3. Missing pass scans from a ULID time floor: new units
                #    carry fresh time-ordered ids, so the scan range is the
                #    recent tail, not the corpus. Exactness does NOT rest on
                #    that assumption — a recount after the drain falls back
                #    to an unbounded scan if rows below the floor are still
                #    missing (clock skew, out-of-band writes).
                # 4. Stale pass runs only when the rules-version btree shows
                #    any row outside the current version (two range probes,
                #    ~ms) — after a bump nearly every row matches and the
                #    scan is productive by construction.
                projection_count, active_count = self._counts(session)
                if (
                    (command.prune or projection_count > active_count)
                    and not stop_requested()
                ):
                    deleted = self._delete_orphans(
                        session, should_stop=should_stop
                    )
                    if deleted:
                        projection_count -= deleted
                if not stop_requested() and projection_count != active_count:
                    floor = self._missing_scan_floor(session)
                    projected, remaining, cursor = self._drain_keyset(
                        session,
                        self._missing_stmt,
                        built_at=built_at,
                        remaining=remaining,
                        stop_requested=stop_requested,
                        start_after=floor,
                        on_progress=on_progress,
                    )
                    if (
                        floor is not None
                        and not stop_requested()
                        and (remaining is None or remaining > 0)
                        and self._counts_diverge(session)
                    ):
                        repaired, remaining, cursor = self._drain_keyset(
                            session,
                            self._missing_stmt,
                            built_at=built_at,
                            remaining=remaining,
                            stop_requested=stop_requested,
                            on_progress=on_progress,
                        )
                        projected += repaired
                if not stop_requested() and self._stale_rows_exist(session):
                    restamped, remaining, stale_cursor = self._drain_keyset(
                        session,
                        self._stale_stmt,
                        built_at=built_at,
                        remaining=remaining,
                        stop_requested=stop_requested,
                        on_progress=on_progress,
                    )
                    projected += restamped
            if remaining is not None and remaining <= 0:
                skipped = self._pending_count(
                    session,
                    full=command.full,
                    after=cursor,
                    stale_after=stale_cursor,
                )
        return BuildSearchProjectionResult(
            projected=projected, deleted=deleted, skipped=skipped
        )

    def _drain_keyset(
        self,
        session: Session,
        stmt_builder: Callable[..., Any],
        *,
        built_at: datetime,
        remaining: int | None,
        stop_requested: Callable[[], bool],
        start_after: str | None = None,
        on_progress: Callable[[], None] | None = None,
    ) -> tuple[int, int | None, str | None]:
        """Batched keyset drain: select -> upsert -> commit -> advance."""

        projected = 0
        cursor = start_after
        while not stop_requested():
            batch_cap = self._BATCH_SIZE
            if remaining is not None:
                batch_cap = min(batch_cap, remaining)
                if batch_cap <= 0:
                    break
            asset_ids = list(
                session.execute(
                    stmt_builder(after=cursor).limit(batch_cap)
                ).scalars()
            )
            if not asset_ids:
                break
            projected += self._upsert(session, asset_ids, built_at=built_at)
            session.commit()
            if on_progress is not None:
                on_progress()
            cursor = asset_ids[-1]
            if remaining is not None:
                remaining -= len(asset_ids)
        return projected, remaining, cursor

    # -- selection ----------------------------------------------------------
    def _active_units_stmt(self, *, after: str | None) -> Any:
        stmt = (
            select(DocumentUnit.asset_id)
            .join(
                ProcessingRun,
                ProcessingRun.processing_run_id == DocumentUnit.processing_run_id,
            )
            .where(ProcessingRun.is_active.is_(True))
        )
        if after is not None:
            stmt = stmt.where(DocumentUnit.asset_id > after)
        return stmt.order_by(DocumentUnit.asset_id)

    def _missing_stmt(self, *, after: str | None) -> Any:
        # NOT EXISTS (not an OUTER JOIN + OR) so the planner may pick a
        # merge anti-join over the two asset_id PKs; see execute() pass 1.
        return self._active_units_stmt(after=after).where(
            ~select(UnitSearchProjection.asset_id)
            .where(UnitSearchProjection.asset_id == DocumentUnit.asset_id)
            .exists()
        )

    def _stale_stmt(self, *, after: str | None) -> Any:
        stmt = select(UnitSearchProjection.asset_id).where(
            UnitSearchProjection.retrieval_rules_version
            != tokenizer.RETRIEVAL_RULES_VERSION
        )
        if after is not None:
            stmt = stmt.where(UnitSearchProjection.asset_id > after)
        return stmt.order_by(UnitSearchProjection.asset_id)

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
                select(func.count()).select_from(
                    self._active_units_stmt(after=None)
                    .order_by(None)
                    .subquery()
                )
            ).scalar()
            or 0
        )
        return projection_count, active_count

    def _counts_diverge(self, session: Session) -> bool:
        projection_count, active_count = self._counts(session)
        return projection_count != active_count

    # In-flight margin for the missing-scan floor: a unit's ULID is minted
    # inside BuildUnits moments before its transaction commits, so ids can
    # land in the table at most minutes below the projected maximum. Two
    # hours is deliberately extravagant — the recount fallback, not this
    # margin, carries exactness.
    _MISSING_FLOOR_BACKOFF_MS = 2 * 60 * 60 * 1000

    def _missing_scan_floor(self, session: Session) -> str | None:
        max_projected = session.execute(
            select(func.max(UnitSearchProjection.asset_id))
        ).scalar()
        if not max_projected:
            return None
        return ids.id_time_floor(
            str(max_projected), backoff_ms=self._MISSING_FLOOR_BACKOFF_MS
        )

    def _stale_rows_exist(self, session: Session) -> bool:
        # ``!=`` is not btree-servable, but its two open ranges are: one
        # probe below the current version, one above, each an instant index
        # range scan on retrieval_rules_version.
        current = tokenizer.RETRIEVAL_RULES_VERSION
        below = (
            select(UnitSearchProjection.asset_id)
            .where(UnitSearchProjection.retrieval_rules_version < current)
            .exists()
        )
        above = (
            select(UnitSearchProjection.asset_id)
            .where(UnitSearchProjection.retrieval_rules_version > current)
            .exists()
        )
        return bool(session.execute(select(below | above)).scalar())

    def _pending_count(
        self,
        session: Session,
        *,
        full: bool,
        after: str | None,
        stale_after: str | None = None,
    ) -> int:
        if full:
            stmt = self._active_units_stmt(after=after)
            counted = select(func.count()).select_from(stmt.subquery())
            return int(session.execute(counted).scalar() or 0)
        total = 0
        for pending_stmt in (
            self._missing_stmt(after=after),
            self._stale_stmt(after=stale_after),
        ):
            counted = select(func.count()).select_from(pending_stmt.subquery())
            total += int(session.execute(counted).scalar() or 0)
        return total

    # -- mutation -----------------------------------------------------------
    def _upsert(
        self, session: Session, asset_ids: Sequence[str], *, built_at: datetime
    ) -> int:
        projected = 0
        upsert_stmt = _upsert_statement()
        for chunk in _chunked(asset_ids, _BATCH):
            batch = [
                compute_search_projection_row(built_at=built_at, **unit)
                for unit in _load_units(session, chunk)
            ]
            if not batch:
                continue
            session.execute(upsert_stmt, batch)
            session.flush()
            projected += len(batch)
        return projected

    def _delete_orphans(
        self, session: Session, *, should_stop: Callable[[], bool] | None = None
    ) -> int:
        """Prune projection rows whose unit left the active set — batched.

        Design §5 applies to the delete too: a rules-bump corpus rebuild
        deactivates old runs corpus-wide, so the orphan set can reach unit
        scale in one wave. One unbatched DELETE would ride a single giant
        transaction (WAL spike, long row locks); instead fetch ids in bounded
        scans and delete in ``_BATCH_SIZE`` chunks with a commit per chunk.
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
            for start in range(0, len(orphan_ids), self._BATCH_SIZE):
                if should_stop is not None and should_stop():
                    return deleted
                chunk = orphan_ids[start : start + self._BATCH_SIZE]
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


def _load_units(session: Session, asset_ids: Sequence[str]) -> list[dict[str, Any]]:
    stmt = select(
        DocumentUnit.asset_id,
        DocumentUnit.title,
        DocumentUnit.heading_path,
        DocumentUnit.payload_kind,
        DocumentUnit.payload,
        DocumentUnit.semantic_keys,
    ).where(DocumentUnit.asset_id.in_(asset_ids))
    return [
        {
            "asset_id": row.asset_id,
            "title": row.title,
            "heading_path": row.heading_path,
            "payload_kind": row.payload_kind,
            "payload": row.payload,
            "semantic_keys": row.semantic_keys,
        }
        for row in session.execute(stmt).all()
    ]


def _upsert_statement() -> Any:
    stmt = pg_insert(UnitSearchProjection)
    excluded = stmt.excluded
    return stmt.on_conflict_do_update(
        index_elements=["asset_id"],
        set_={column: excluded[column] for column in _UPDATE_COLUMNS},
    )


def _chunked(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
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
    built_at: datetime,
) -> dict[str, Any]:
    """Deterministic projection row for one unit (no ``search_tsv`` — generated)."""

    title_text = title or ""
    heading_path_text = " > ".join(str(item) for item in heading_path or [])
    body_text = linearize_body(payload_kind, payload or {})
    return {
        "asset_id": asset_id,
        "retrieval_rules_version": tokenizer.RETRIEVAL_RULES_VERSION,
        "title_text": title_text,
        "heading_path_text": heading_path_text,
        "title_tokens": tokenizer.tokenize(title_text),
        "path_tokens": tokenizer.tokenize(heading_path_text),
        "body_tokens": tokenizer.tokenize(body_text),
        # Semantic keys are controlled ASCII tokens; they bypass segmentation.
        "key_tokens": " ".join(semantic_keys or []),
        "header_row_candidate": header_row_candidate(payload_kind, payload or {}),
        "built_at": built_at,
    }


def linearize_body(payload_kind: str, payload: Mapping[str, Any]) -> str:
    """Milestone 06R §4 body linearization, keyed on ``payload_kind``.

    text  -> payload["text"]; table -> caption + unit + headers + row cells +
    notes (EXCLUDING ``raw_html`` so tag noise never enters tokens); mixed ->
    each part in order, recursively; anything else -> empty.
    """

    if payload_kind == "text":
        return str(payload.get("text") or "")
    if payload_kind == "table":
        return _linearize_table(payload)
    if payload_kind == "mixed":
        return _linearize_parts(payload.get("parts") or [])
    return ""


def _linearize_table(payload: Mapping[str, Any]) -> str:
    headers = payload.get("headers") or []
    rows = payload.get("rows") or []
    return " ".join(
        [str(value) for value in payload.get("caption") or []]
        + [str(payload.get("unit") or "")]
        + [str(cell) for cell in headers]
        + [str(cell) for row in rows for cell in row or []]
        + [str(value) for value in payload.get("notes") or []]
    )


def _linearize_parts(parts: Iterable[Any]) -> str:
    return " ".join(
        _linearize_part(part) for part in parts if isinstance(part, Mapping)
    )


def _linearize_part(part: Mapping[str, Any]) -> str:
    kind = str(part.get("kind", "text"))
    if kind == "table":
        return _linearize_table(part)
    if kind == "image":
        return " ".join(
            piece
            for piece in (
                str(part.get("caption") or ""),
                str(part.get("context") or ""),
            )
            if piece
        )
    if kind == "mixed":
        return _linearize_parts(part.get("parts") or [])
    return str(part.get("text") or "")


def header_row_candidate(payload_kind: str, payload: Mapping[str, Any]) -> bool:
    """Milestone 06R §4/§5 diagnostic: a headerless table whose first row is a
    de-facto header (all-text labels) followed by numeric data rows.

    A td-only numeric table (MinerU emitted no ``<th>`` so ``headers`` is empty
    and the label row landed in ``rows``) flags true. KV forms fail because
    their first row already pairs a label with a numeric value; tables with real
    headers and single-row tables fail their own guards. Misjudgment only mis-
    weights retrieval — it never touches the L1 evidence payload.
    """

    if payload_kind != "table":
        return False
    if any(str(cell).strip() for cell in payload.get("headers") or []):
        return False
    rows = payload.get("rows") or []
    if len(rows) < 2:
        return False
    first_row = [str(cell).strip() for cell in rows[0] or []]
    if not first_row or any(not cell for cell in first_row):
        return False
    if any(_is_numeric_cell(cell) for cell in first_row):
        return False
    return any(
        _is_numeric_cell(str(cell).strip())
        for row in rows[1:]
        for cell in row or []
    )


def _is_numeric_cell(cell: str) -> bool:
    # Milestone 06R §4 "含货币量级词": a magnitude-suffixed amount ("1,234.56
    # 万元") is numeric. The suffix vocabulary is the existing controlled
    # unit-declaration table (rules._DECL_MAGNITUDE), not a new phrase list.
    stripped = _MAGNITUDE_SUFFIX_RE.sub("", cell.strip())
    return bool(_NUMERIC_CELL_RE.match(stripped or cell.strip()))
