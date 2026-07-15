"""Publish a built processing_run as the active document run."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.worker.locks import maybe_lock_document
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.entities import outbox_events
from disclosure_anchor.domain.errors import PublishRunError
from disclosure_anchor.domain.services.unit_hashing import query_projection


@dataclass(frozen=True)
class PublishRunCommand:
    processing_run_id: str
    allow_empty: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class PublishRunResult:
    processing_run_id: str
    status: str
    idempotent: bool = False
    created_count: int = 0
    removed_count: int = 0
    projection_changed_count: int = 0
    published_change_kind: str | None = None


@dataclass(frozen=True)
class UnitDiff:
    created: list[e.DocumentUnit]
    removed: list[e.DocumentUnit]
    projection_changed: list[tuple[e.DocumentUnit, e.DocumentUnit, list[str]]]


class PublishRun:
    def __init__(self, *, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def execute(self, command: PublishRunCommand) -> PublishRunResult:
        if command.allow_empty and not command.reason:
            raise PublishRunError(
                _structured_error(
                    error_code="ALLOW_EMPTY_REASON_REQUIRED",
                    message="--reason is required with allow_empty",
                )
            )
        now = datetime.now(timezone.utc)
        with self._uow_factory() as uow:
            run = uow.processing_runs.get(command.processing_run_id)
            if run is None:
                raise PublishRunError(
                    _structured_error(
                        error_code="RUN_NOT_FOUND",
                        message=f"processing run not found: {command.processing_run_id}",
                    )
                )
            _validate_publishable(run)
            maybe_lock_document(uow, run.document_id)
            document = uow.documents.get_for_update(run.document_id)
            if document is None:
                raise PublishRunError(
                    _structured_error(
                        error_code="RUN_NOT_FOUND",
                        message=f"document not found: {run.document_id}",
                    )
                )
            if run.is_active and document.current_processing_run_id == run.processing_run_id:
                return PublishRunResult(
                    processing_run_id=run.processing_run_id,
                    status="published",
                    idempotent=True,
                )
            if document.current_processing_run_id == run.processing_run_id and not run.is_active:
                raise PublishRunError(
                    _structured_error(
                        error_code="RUN_NOT_FOUND",
                        message="document points at a non-active current run",
                    )
                )

            new_units = uow.document_units.list_by_processing_run(run.processing_run_id)
            if not new_units and not command.allow_empty:
                raise PublishRunError(
                    _structured_error(
                        error_code="EMPTY_RUN",
                        message="cannot publish empty unit run without allow_empty",
                    )
                )
            old_run = (
                uow.processing_runs.get(document.current_processing_run_id)
                if document.current_processing_run_id
                else None
            )
            old_units = (
                uow.document_units.list_by_processing_run(old_run.processing_run_id)
                if old_run is not None
                else []
            )
            if old_run is not None:
                old_run.is_active = False
                uow.processing_runs.update(old_run)
                uow.flush()
            run.is_active = True
            uow.processing_runs.update(run)
            document.current_processing_run_id = run.processing_run_id
            document.status = "published"
            uow.documents.update(document)

            diff = diff_units(old_units=old_units, new_units=new_units)
            for unit in sorted(diff.removed, key=lambda item: (item.order_index, item.asset_id)):
                uow.outbox.add(
                    outbox_events.document_unit_removed(
                        document_id=document.document_id,
                        old_processing_run_id=unit.processing_run_id,
                        old_asset_id=unit.asset_id,
                        content_hash=unit.content_hash,
                        payload_kind=unit.payload_kind,
                        old_order_index=unit.order_index,
                        old_heading_path=unit.heading_path,
                        occurred_at=now,
                    )
                )
            for unit in sorted(diff.created, key=lambda item: (item.order_index, item.asset_id)):
                uow.outbox.add(
                    outbox_events.document_unit_created(
                        document_id=document.document_id,
                        processing_run_id=unit.processing_run_id,
                        new_asset_id=unit.asset_id,
                        content_hash=unit.content_hash,
                        payload_kind=unit.payload_kind,
                        new_order_index=unit.order_index,
                        new_heading_path=unit.heading_path,
                        occurred_at=now,
                    )
                )
            for old_unit, new_unit, changed_fields in sorted(
                diff.projection_changed,
                key=lambda item: (item[1].order_index, item[1].asset_id),
            ):
                uow.outbox.add(
                    outbox_events.document_unit_projection_changed(
                        document_id=document.document_id,
                        new_processing_run_id=new_unit.processing_run_id,
                        old_asset_id=old_unit.asset_id,
                        new_asset_id=new_unit.asset_id,
                        content_hash=new_unit.content_hash,
                        old_query_projection_hash=old_unit.query_projection_hash,
                        new_query_projection_hash=new_unit.query_projection_hash,
                        changed_fields=changed_fields,
                        occurred_at=now,
                    )
                )

            published_change_kind = _published_change_kind(old_run=old_run, new_run=run)
            uow.outbox.add(
                outbox_events.processing_run_published(
                    document_id=document.document_id,
                    processing_run_id=run.processing_run_id,
                    change_kind=published_change_kind,
                    previous_processing_run_id=old_run.processing_run_id
                    if old_run is not None
                    else None,
                    content_hash_aggregate=run.content_hash_aggregate,
                    structure_hash=run.structure_hash,
                    unit_count=len(new_units),
                    created_count=len(diff.created),
                    removed_count=len(diff.removed),
                    projection_changed_count=len(diff.projection_changed),
                    allow_empty_reason=command.reason if command.allow_empty else None,
                    occurred_at=now,
                )
            )
            uow.commit()
            return PublishRunResult(
                processing_run_id=run.processing_run_id,
                status="published",
                created_count=len(diff.created),
                removed_count=len(diff.removed),
                projection_changed_count=len(diff.projection_changed),
                published_change_kind=published_change_kind,
            )


def diff_units(*, old_units: list[e.DocumentUnit], new_units: list[e.DocumentUnit]) -> UnitDiff:
    old_by_key = _units_by_key(old_units)
    new_by_key = _units_by_key(new_units)
    created: list[e.DocumentUnit] = []
    removed: list[e.DocumentUnit] = []
    projection_changed: list[tuple[e.DocumentUnit, e.DocumentUnit, list[str]]] = []
    for key in sorted(set(old_by_key) | set(new_by_key)):
        old_group = sorted(old_by_key[key], key=lambda item: (item.order_index, item.asset_id))
        new_group = sorted(new_by_key[key], key=lambda item: (item.order_index, item.asset_id))
        exact_pairs, old_remaining, new_remaining = _pair_equal_projections(
            old_group, new_group
        )
        pair_count = min(len(old_remaining), len(new_remaining))
        for old_unit, new_unit in [
            *exact_pairs,
            *zip(
                old_remaining[:pair_count],
                new_remaining[:pair_count],
                strict=True,
            ),
        ]:
            if old_unit.query_projection_hash != new_unit.query_projection_hash:
                projection_changed.append(
                    (old_unit, new_unit, _changed_projection_fields(old_unit, new_unit))
                )
        removed.extend(old_remaining[pair_count:])
        created.extend(new_remaining[pair_count:])
    return UnitDiff(created=created, removed=removed, projection_changed=projection_changed)


def _pair_equal_projections(
    old_group: list[e.DocumentUnit],
    new_group: list[e.DocumentUnit],
) -> tuple[
    list[tuple[e.DocumentUnit, e.DocumentUnit]],
    list[e.DocumentUnit],
    list[e.DocumentUnit],
]:
    """Pair identical projections before applying stable positional pairing.

    A document may legitimately contain duplicate content. Reordering two such
    units must not manufacture two projection-change events when the old and
    new projection multisets are identical.
    """

    new_by_projection: dict[str | None, list[e.DocumentUnit]] = defaultdict(list)
    for unit in new_group:
        new_by_projection[unit.query_projection_hash].append(unit)
    exact: list[tuple[e.DocumentUnit, e.DocumentUnit]] = []
    old_remaining: list[e.DocumentUnit] = []
    for old_unit in old_group:
        candidates = new_by_projection.get(old_unit.query_projection_hash)
        if candidates:
            exact.append((old_unit, candidates.pop(0)))
        else:
            old_remaining.append(old_unit)
    paired_new_ids = {id(unit) for _old, unit in exact}
    new_remaining = [unit for unit in new_group if id(unit) not in paired_new_ids]
    return exact, old_remaining, new_remaining


def _units_by_key(
    units: list[e.DocumentUnit],
) -> dict[tuple[str, str], list[e.DocumentUnit]]:
    by_key: dict[tuple[str, str], list[e.DocumentUnit]] = defaultdict(list)
    for unit in units:
        by_key[(unit.payload_kind, unit.content_hash)].append(unit)
    return by_key


def _changed_projection_fields(old: e.DocumentUnit, new: e.DocumentUnit) -> list[str]:
    old_projection = _unit_query_projection(old)
    new_projection = _unit_query_projection(new)
    changed = [
        field
        for field in old_projection
        if field != "payload_kind"
        and old_projection[field] != new_projection[field]
    ]
    if not changed:
        raise PublishRunError(
            _structured_error(
                error_code="QUERY_PROJECTION_HASH_MISMATCH",
                message=(
                    "query_projection_hash differs although the canonical "
                    "query projection is unchanged"
                ),
            )
        )
    return changed


def _unit_query_projection(unit: e.DocumentUnit) -> dict[str, Any]:
    return query_projection(
        payload_kind=unit.payload_kind,
        title=unit.title,
        heading_path=unit.heading_path,
        semantic_key=unit.semantic_key,
        semantic_keys=unit.semantic_keys,
        quality_status=unit.quality_status,
        applicability=unit.applicability,
        payload=unit.payload,
    )


def _validate_publishable(run: e.ProcessingRun) -> None:
    if run.status != "succeeded":
        raise PublishRunError(
            _structured_error(
                error_code="RUN_NOT_SUCCEEDED",
                message=f"run status is {run.status}",
            )
        )
    if run.unit_build_status != "succeeded":
        raise PublishRunError(
            _structured_error(
                error_code="UNITS_NOT_BUILT",
                message=f"unit_build_status is {run.unit_build_status}",
            )
        )


def _published_change_kind(
    *, old_run: e.ProcessingRun | None, new_run: e.ProcessingRun
) -> str:
    if old_run is None:
        return "materialized"
    if old_run.content_hash_aggregate != new_run.content_hash_aggregate:
        return "materialized"
    return "observed"


def _structured_error(*, error_code: str, message: str) -> dict[str, Any]:
    return {
        "stage": "publish",
        "error_code": error_code,
        "retryable": False,
        "message": message,
    }
