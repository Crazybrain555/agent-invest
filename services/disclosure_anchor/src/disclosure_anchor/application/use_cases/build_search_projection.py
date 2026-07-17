"""Regenerate the 06R derived retrieval projection (milestone 06R §4/§5).

Derived layer (U7 red line): every projection column regenerates
deterministically from the persisted ``document_unit`` rows via the pinned
application-side jieba tokenizer. This use case writes only
``disclosure_core.unit_search_projection``; it emits no outbox events and puts
nothing into content/query-projection hashes.

Two modes:

* ``full``  — recompute every active-run unit (upsert), then delete projection
  rows whose ``asset_id`` no longer belongs to an active-run unit.
* delta     — recompute only units missing a projection row or carrying a stale
  ``retrieval_rules_version``; the worker runs this bounded by the publish
  batch limit.

Row computation is factored into pure module-level functions so the
linearization and ``header_row_candidate`` rules are testable without a DB.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres.models import (
    DocumentUnit,
    ProcessingRun,
    UnitSearchProjection,
)
from disclosure_anchor.adapters.retrieval import tokenizer

# Milestone 06R §5: single-transaction rebuild, flush every 1000 rows. The read
# batch matches the flush batch so at most this many payloads are in memory and
# the streaming-read-while-writing hazard (server-side cursor + concurrent DML
# on one connection) never arises.
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
    # Delta batch bound; the worker passes the publish batch limit, the CLI
    # leaves it unbounded. Ignored in full mode.
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

    def execute(
        self, command: BuildSearchProjectionCommand
    ) -> BuildSearchProjectionResult:
        built_at = datetime.now(timezone.utc)
        with Session(self._engine) as session:
            if command.full:
                asset_ids: list[str] = self._active_asset_ids(session)
                skipped = 0
            else:
                asset_ids, skipped = self._delta_asset_ids(
                    session, limit=command.limit
                )
            projected = self._upsert(session, asset_ids, built_at=built_at)
            deleted = self._delete_orphans(session) if command.full else 0
            session.commit()
        return BuildSearchProjectionResult(
            projected=projected, deleted=deleted, skipped=skipped
        )

    # -- selection ----------------------------------------------------------
    def _active_asset_ids(self, session: Session) -> list[str]:
        stmt = (
            select(DocumentUnit.asset_id)
            .join(
                ProcessingRun,
                ProcessingRun.processing_run_id == DocumentUnit.processing_run_id,
            )
            .where(ProcessingRun.is_active.is_(True))
            .order_by(DocumentUnit.asset_id)
        )
        return list(session.execute(stmt).scalars())

    def _delta_asset_ids(
        self, session: Session, *, limit: int | None
    ) -> tuple[list[str], int]:
        stmt = (
            select(DocumentUnit.asset_id)
            .join(
                ProcessingRun,
                ProcessingRun.processing_run_id == DocumentUnit.processing_run_id,
            )
            .outerjoin(
                UnitSearchProjection,
                UnitSearchProjection.asset_id == DocumentUnit.asset_id,
            )
            .where(
                ProcessingRun.is_active.is_(True),
                or_(
                    UnitSearchProjection.asset_id.is_(None),
                    UnitSearchProjection.retrieval_rules_version
                    != tokenizer.RETRIEVAL_RULES_VERSION,
                ),
            )
            .order_by(DocumentUnit.asset_id)
        )
        ids = list(session.execute(stmt).scalars())
        if limit is not None and len(ids) > limit:
            # Deferred candidates are reported as ``skipped``; the next round
            # (or an unbounded CLI run) picks them up.
            return ids[:limit], len(ids) - limit
        return ids, 0

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

    def _delete_orphans(self, session: Session) -> int:
        active_asset_ids = (
            select(DocumentUnit.asset_id)
            .join(
                ProcessingRun,
                ProcessingRun.processing_run_id == DocumentUnit.processing_run_id,
            )
            .where(ProcessingRun.is_active.is_(True))
        )
        # Route DML through the session's transactional connection: its
        # ``CursorResult.rowcount`` is the orphan-prune count (the ORM
        # ``Session.execute`` return type does not expose rowcount). The delete
        # commits with the session like every upsert above.
        result = session.connection().execute(
            delete(UnitSearchProjection).where(
                UnitSearchProjection.asset_id.not_in(active_asset_ids)
            )
        )
        return int(result.rowcount or 0)


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
