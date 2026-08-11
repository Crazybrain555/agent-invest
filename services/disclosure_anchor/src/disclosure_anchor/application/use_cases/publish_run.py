"""Publish a built processing_run as the active document run."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from disclosure_anchor.application.contracts.provider_document_admission import (
    ProviderDocumentAdmissionError,
)
from disclosure_anchor.application.contracts.provider_unit import (
    PROVIDER_UNIT_BUILDER_VERSION,
    ProviderUnitDraft,
    provider_unit_locator_to_payload,
)
from disclosure_anchor.application.services.provider_document_admission import (
    ProviderDocumentAdmission,
)
from disclosure_anchor.application.services.provider_unit_builder import (
    build_provider_units,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.worker.locks import maybe_lock_document
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.entities import outbox_events
from disclosure_anchor.domain.errors import PublishRunError
from disclosure_anchor.domain.services.unit_hashing import (
    UnitHashes,
    compute_unit_hashes,
    content_hash_aggregate,
    query_projection,
    structure_hash_aggregate,
)
from disclosure_anchor.domain.value_objects.semantic_key import (
    SemanticKeyInvariantError,
    validate_semantic_key_state,
)


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
    # The run this publish deactivated, if any. Run deactivation is the only
    # in-service source of search-projection orphans, so the worker uses
    # this as the per-round "orphans may exist" signal for prune gating.
    superseded_run_id: str | None = None


@dataclass(frozen=True)
class UnitDiff:
    created: list[e.DocumentUnit]
    removed: list[e.DocumentUnit]
    projection_changed: list[tuple[e.DocumentUnit, e.DocumentUnit, list[str]]]


TERMINAL_PUBLICATION_ERROR_CODES = frozenset(
    {
        "PARSER_TARGET_IDENTITY_INVALID",
        "PARSER_TARGET_IDENTITY_MISMATCH",
        "QUERY_PROJECTION_HASH_MISMATCH",
        "RUN_HASH_AGGREGATE_INVALID",
        "RUN_UNIT_HASH_INPUT_INVALID",
        "RUN_UNIT_HASH_INVALID",
        "RUN_UNIT_SEMANTIC_INVALID",
        "RUN_UNIT_SET_INVALID",
        "PROVIDER_DOCUMENT_ADMISSION_FAILED",
        "PROVIDER_UNIT_PROJECTION_INVALID",
        "RUN_OUTPUT_CONTRACT_UNSUPPORTED",
    }
)


class ProviderDocumentPublicationGuard:
    """Re-admit source bytes and replay every persisted provider Unit."""

    def __init__(self, admission: ProviderDocumentAdmission) -> None:
        self._admission = admission

    def __call__(
        self,
        *,
        run: e.ProcessingRun,
        document: e.Document,
        artifact_owner: e.ProcessingRun,
        security_code: str,
        units: list[e.DocumentUnit],
    ) -> None:
        if (
            run.provider_document_relpath is None
            or run.normalized_ir_relpath is not None
        ):
            raise PublishRunError(
                _structured_error(
                    error_code="RUN_OUTPUT_CONTRACT_UNSUPPORTED",
                    message="only provider_document.v1 runs can publish",
                )
            )
        try:
            admitted = self._admission.admit(
                document=document,
                run=run,
                artifact_owner=artifact_owner,
                security_code=security_code,
            )
        except ProviderDocumentAdmissionError as exc:
            raise PublishRunError(
                _structured_error(
                    error_code="PROVIDER_DOCUMENT_ADMISSION_FAILED",
                    reason_code=exc.reason_code,
                    retryable=exc.retryable,
                    message=str(exc),
                )
            ) from exc
        try:
            build = build_provider_units(admitted)
        except (TypeError, ValueError) as exc:
            raise PublishRunError(
                _structured_error(
                    error_code="PROVIDER_UNIT_PROJECTION_INVALID",
                    message=str(exc),
                )
            ) from exc
        if build.unassigned_table_parts:
            raise PublishRunError(
                _structured_error(
                    error_code="PROVIDER_UNIT_PROJECTION_INVALID",
                    reason_code="unassigned_table_evidence",
                    message="provider table evidence has no source-bound Unit owner",
                )
            )
        if run.builder_rules_version != PROVIDER_UNIT_BUILDER_VERSION:
            raise PublishRunError(
                _structured_error(
                    error_code="PROVIDER_UNIT_PROJECTION_INVALID",
                    reason_code="builder_rules_version_mismatch",
                    message="candidate run was not built by the current provider rules",
                )
            )
        ordered = sorted(units, key=lambda item: item.order_index)
        if len(ordered) != len(build.units):
            raise PublishRunError(
                _structured_error(
                    error_code="PROVIDER_UNIT_PROJECTION_INVALID",
                    reason_code="unit_count_mismatch",
                    message="persisted Unit count differs from fresh source replay",
                )
            )
        for draft, unit in zip(build.units, ordered, strict=True):
            self._validate_unit(
                draft=draft,
                unit=unit,
                run=run,
                document=document,
            )

    @staticmethod
    def _validate_unit(
        *,
        draft: ProviderUnitDraft,
        unit: e.DocumentUnit,
        run: e.ProcessingRun,
        document: e.Document,
    ) -> None:
        expected: dict[str, object] = {
            "document_id": document.document_id,
            "processing_run_id": run.processing_run_id,
            "provider_document_id": document.provider_document_id,
            "payload_kind": draft.payload_kind,
            "payload": draft.payload,
            "title": draft.title,
            "heading_path": list(draft.heading_path),
            "order_index": draft.unit_index + 1,
            "semantic_key": draft.semantic_key,
            "semantic_keys": list(draft.semantic_keys),
            "quality_status": draft.quality_status,
            "applicability": None,
            "page_no": draft.page_no,
            "artifact_locator": provider_unit_locator_to_payload(draft.locator),
            "content_hash": draft.content_hash,
            "query_projection_hash": draft.query_projection_hash,
            "structure_hash": draft.structure_hash,
        }
        for field, value in expected.items():
            if getattr(unit, field) != value:
                raise PublishRunError(
                    _structured_error(
                        error_code="PROVIDER_UNIT_PROJECTION_INVALID",
                        reason_code=f"{field}_mismatch",
                        message=(
                            "persisted Unit differs from fresh source replay: "
                            f"order={unit.order_index} field={field}"
                        ),
                    )
                )


class PublishRun:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        publication_guard: ProviderDocumentPublicationGuard,
    ) -> None:
        self._uow_factory = uow_factory
        self._publication_guard = publication_guard

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
            if (
                run.is_active
                and document.current_processing_run_id == run.processing_run_id
            ):
                return PublishRunResult(
                    processing_run_id=run.processing_run_id,
                    status="published",
                    idempotent=True,
                )
            if (
                document.current_processing_run_id == run.processing_run_id
                and not run.is_active
            ):
                raise PublishRunError(
                    _structured_error(
                        error_code="RUN_NOT_FOUND",
                        message="document points at a non-active current run",
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
            artifact_owner = uow.processing_runs.get(
                run.artifact_owner_processing_run_id
            )
            security = (
                uow.securities.get(document.security_id)
                if document.security_id is not None
                else None
            )
            if artifact_owner is None or security is None:
                raise PublishRunError(
                    _structured_error(
                        error_code="PROVIDER_DOCUMENT_ADMISSION_FAILED",
                        reason_code="provider_owner_or_security_missing",
                        message="provider artifact owner or document security is missing",
                    )
                )
            try:
                new_units = uow.document_units.list_by_processing_run(
                    run.processing_run_id
                )
                self._publication_guard(
                    run=run,
                    document=document,
                    artifact_owner=artifact_owner,
                    security_code=security.security_code,
                    units=new_units,
                )
                if not new_units and not command.allow_empty:
                    raise PublishRunError(
                        _structured_error(
                            error_code="EMPTY_RUN",
                            message=(
                                "cannot publish empty unit run without "
                                "allow_empty"
                            ),
                        )
                    )
                canonical_new = _validate_candidate_run(
                    run=run,
                    units=new_units,
                )
                diff = diff_units(old_units=old_units, new_units=new_units)
            except PublishRunError as exc:
                if (
                    exc.error.get("error_code")
                    in TERMINAL_PUBLICATION_ERROR_CODES
                ):
                    # Historical built runs may predate current IR/unit
                    # invariants. Quarantine deterministic poison once while
                    # retaining its units for audit. Shared storage failures
                    # remain built and retryable.
                    run.unit_build_status = "failed"
                    run.unit_build_error = dict(exc.error)
                    run.unit_build_attempt_count += 1
                    uow.processing_runs.update(run)
                    uow.commit()
                raise

            # All candidate identity and semantic checks happen before the
            # active pointer or either run is mutated. Historical active runs
            # are compared from their canonical rows but are not rejected for
            # legacy stored hashes.
            if old_run is not None:
                old_run.is_active = False
                uow.processing_runs.update(old_run)
                uow.flush()
            run.is_active = True
            uow.processing_runs.update(run)
            document.current_processing_run_id = run.processing_run_id
            document.status = "published"
            uow.documents.update(document)

            for unit in sorted(
                diff.removed, key=lambda item: (item.order_index, item.asset_id)
            ):
                canonical = _canonical_unit_hashes(unit)
                uow.outbox.add(
                    outbox_events.document_unit_removed(
                        document_id=document.document_id,
                        old_processing_run_id=unit.processing_run_id,
                        old_asset_id=unit.asset_id,
                        content_hash=canonical.content_hash,
                        payload_kind=unit.payload_kind,
                        old_order_index=unit.order_index,
                        old_heading_path=unit.heading_path,
                        occurred_at=now,
                    )
                )
            for unit in sorted(
                diff.created, key=lambda item: (item.order_index, item.asset_id)
            ):
                canonical = canonical_new[id(unit)]
                uow.outbox.add(
                    outbox_events.document_unit_created(
                        document_id=document.document_id,
                        processing_run_id=unit.processing_run_id,
                        new_asset_id=unit.asset_id,
                        content_hash=canonical.content_hash,
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
                old_hashes = _canonical_unit_hashes(old_unit)
                new_hashes = canonical_new[id(new_unit)]
                uow.outbox.add(
                    outbox_events.document_unit_projection_changed(
                        document_id=document.document_id,
                        new_processing_run_id=new_unit.processing_run_id,
                        old_asset_id=old_unit.asset_id,
                        new_asset_id=new_unit.asset_id,
                        content_hash=new_hashes.content_hash,
                        old_query_projection_hash=(old_hashes.query_projection_hash),
                        new_query_projection_hash=(new_hashes.query_projection_hash),
                        changed_fields=changed_fields,
                        occurred_at=now,
                    )
                )

            published_change_kind = _published_change_kind(
                old_run=old_run,
                diff=diff,
            )
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
                superseded_run_id=(
                    old_run.processing_run_id if old_run is not None else None
                ),
            )


def diff_units(
    *, old_units: list[e.DocumentUnit], new_units: list[e.DocumentUnit]
) -> UnitDiff:
    canonical = {
        id(unit): _canonical_unit_hashes(unit) for unit in [*old_units, *new_units]
    }
    old_by_key = _units_by_key(old_units, canonical=canonical)
    new_by_key = _units_by_key(new_units, canonical=canonical)
    created: list[e.DocumentUnit] = []
    removed: list[e.DocumentUnit] = []
    projection_changed: list[tuple[e.DocumentUnit, e.DocumentUnit, list[str]]] = []
    for key in sorted(set(old_by_key) | set(new_by_key)):
        old_group = sorted(
            old_by_key[key], key=lambda item: (item.order_index, item.asset_id)
        )
        new_group = sorted(
            new_by_key[key], key=lambda item: (item.order_index, item.asset_id)
        )
        exact_pairs, old_remaining, new_remaining = _pair_equal_projections(
            old_group,
            new_group,
            canonical=canonical,
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
            if (
                canonical[id(old_unit)].query_projection_hash
                != canonical[id(new_unit)].query_projection_hash
            ):
                projection_changed.append(
                    (old_unit, new_unit, _changed_projection_fields(old_unit, new_unit))
                )
        removed.extend(old_remaining[pair_count:])
        created.extend(new_remaining[pair_count:])
    return UnitDiff(
        created=created, removed=removed, projection_changed=projection_changed
    )


def _pair_equal_projections(
    old_group: list[e.DocumentUnit],
    new_group: list[e.DocumentUnit],
    *,
    canonical: dict[int, UnitHashes],
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

    new_by_projection: dict[str, list[e.DocumentUnit]] = defaultdict(list)
    for unit in new_group:
        new_by_projection[canonical[id(unit)].query_projection_hash].append(unit)
    exact: list[tuple[e.DocumentUnit, e.DocumentUnit]] = []
    old_remaining: list[e.DocumentUnit] = []
    for old_unit in old_group:
        candidates = new_by_projection.get(
            canonical[id(old_unit)].query_projection_hash
        )
        if candidates:
            exact.append((old_unit, candidates.pop(0)))
        else:
            old_remaining.append(old_unit)
    paired_new_ids = {id(unit) for _old, unit in exact}
    new_remaining = [unit for unit in new_group if id(unit) not in paired_new_ids]
    return exact, old_remaining, new_remaining


def _units_by_key(
    units: list[e.DocumentUnit],
    *,
    canonical: dict[int, UnitHashes],
) -> dict[tuple[str, str], list[e.DocumentUnit]]:
    by_key: dict[tuple[str, str], list[e.DocumentUnit]] = defaultdict(list)
    for unit in units:
        by_key[(unit.payload_kind, canonical[id(unit)].content_hash)].append(unit)
    return by_key


def _changed_projection_fields(old: e.DocumentUnit, new: e.DocumentUnit) -> list[str]:
    old_projection = _unit_query_projection(old)
    new_projection = _unit_query_projection(new)
    changed = [
        field
        for field in old_projection
        if field != "payload_kind" and old_projection[field] != new_projection[field]
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
    if (
        run.run_kind not in {"parse", "rebuild_units"}
        or run.provider_document_relpath is None
        or run.normalized_ir_relpath is not None
    ):
        raise PublishRunError(
            _structured_error(
                error_code="RUN_OUTPUT_CONTRACT_UNSUPPORTED",
                message="only provider_document.v1 parse/rebuild runs can publish",
            )
        )


def _published_change_kind(*, old_run: e.ProcessingRun | None, diff: UnitDiff) -> str:
    if old_run is None:
        return "materialized"
    if diff.created or diff.removed:
        return "materialized"
    return "observed"


def _canonical_unit_hashes(unit: e.DocumentUnit) -> UnitHashes:
    return compute_unit_hashes(
        payload_kind=unit.payload_kind,
        payload=unit.payload,
        title=unit.title,
        heading_path=unit.heading_path,
        semantic_key=unit.semantic_key,
        semantic_keys=unit.semantic_keys,
        quality_status=unit.quality_status,
        order_index=unit.order_index,
        applicability=unit.applicability,
    )


def _validate_candidate_run(
    *,
    run: e.ProcessingRun,
    units: list[e.DocumentUnit],
) -> dict[int, UnitHashes]:
    _validate_candidate_unit_set(run=run, units=units)
    canonical: dict[int, UnitHashes] = {}
    for unit in units:
        try:
            validate_semantic_key_state(unit.semantic_key, unit.semantic_keys)
        except SemanticKeyInvariantError as exc:
            raise PublishRunError(
                _structured_error(
                    error_code="RUN_UNIT_SEMANTIC_INVALID",
                    reason_code=exc.reason_code,
                    message=(
                        "candidate run contains an invalid semantic-key state: "
                        f"{unit.asset_id}"
                    ),
                )
            ) from exc
        try:
            expected_hashes = _canonical_unit_hashes(unit)
        except (TypeError, ValueError) as exc:
            raise PublishRunError(
                _structured_error(
                    error_code="RUN_UNIT_HASH_INPUT_INVALID",
                    reason_code="canonical_json_invalid",
                    message=(
                        "candidate run contains a payload that cannot be "
                        f"canonically hashed: {unit.asset_id}"
                    ),
                )
            ) from exc
        canonical[id(unit)] = expected_hashes
        for field in (
            "content_hash",
            "query_projection_hash",
            "structure_hash",
        ):
            if getattr(unit, field) != getattr(expected_hashes, field):
                raise PublishRunError(
                    _structured_error(
                        error_code="RUN_UNIT_HASH_INVALID",
                        reason_code=f"{field}_mismatch",
                        message=(
                            "candidate run contains a non-canonical unit hash: "
                            f"{unit.asset_id}"
                        ),
                    )
                )

    expected_content_aggregate = content_hash_aggregate(
        canonical[id(unit)].content_hash for unit in units
    )
    ordered = sorted(units, key=lambda item: item.order_index)
    expected_structure_aggregate = structure_hash_aggregate(
        canonical[id(unit)].structure_hash for unit in ordered
    )
    for field, expected_value in (
        ("content_hash_aggregate", expected_content_aggregate),
        ("structure_hash", expected_structure_aggregate),
    ):
        if getattr(run, field) != expected_value:
            raise PublishRunError(
                _structured_error(
                    error_code="RUN_HASH_AGGREGATE_INVALID",
                    reason_code=f"{field}_mismatch",
                    message=f"candidate run {field} is not canonical",
                )
            )
    return canonical


def _validate_candidate_unit_set(
    *, run: e.ProcessingRun, units: list[e.DocumentUnit]
) -> None:
    """Validate ownership and ordered-set identity before hash verification."""

    asset_ids = [unit.asset_id for unit in units]
    if len(asset_ids) != len(set(asset_ids)):
        raise PublishRunError(
            _structured_error(
                error_code="RUN_UNIT_SET_INVALID",
                reason_code="duplicate_asset_id",
                message="candidate run contains duplicate document-unit asset ids",
            )
        )
    if any(unit.processing_run_id != run.processing_run_id for unit in units):
        raise PublishRunError(
            _structured_error(
                error_code="RUN_UNIT_SET_INVALID",
                reason_code="processing_run_mismatch",
                message="candidate unit processing_run_id does not match its run",
            )
        )
    if any(unit.document_id != run.document_id for unit in units):
        raise PublishRunError(
            _structured_error(
                error_code="RUN_UNIT_SET_INVALID",
                reason_code="document_mismatch",
                message="candidate unit document_id does not match its run",
            )
        )
    order_indices = sorted(unit.order_index for unit in units)
    expected = list(range(1, len(units) + 1))
    if order_indices != expected:
        raise PublishRunError(
            _structured_error(
                error_code="RUN_UNIT_SET_INVALID",
                reason_code="order_index_not_contiguous",
                message="candidate run order_index must be unique and contiguous from 1",
            )
        )


def _structured_error(
    *,
    error_code: str,
    message: str,
    reason_code: str | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "stage": "publish",
        "error_code": error_code,
        "retryable": retryable,
        "message": message,
    }
    if reason_code is not None:
        error["reason_code"] = reason_code
    return error
