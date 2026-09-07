"""Production first-builder for one closed V4 publication request."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
from typing import Any, cast

from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    AtomicPublicationRequestV4,
    PublicationAttemptIdentityV4,
    PreIdUnitPublicationV4,
    PreviousActiveUnitV4,
    previous_active_units_sha256_v4,
    seal_atomic_publication_request_v4,
    seal_pre_id_unit_publication_v4,
    seal_upstream_publication_evidence_v4,
)
from disclosure_anchor.application.contracts.provider_document import ProviderDocument
from disclosure_anchor.application.contracts.provider_document_admission import (
    AdmittedProviderDocument,
)
from disclosure_anchor.application.contracts.provider_unit import (
    PROVIDER_UNIT_BUILDER_VERSION,
    ProviderUnitDraft,
    provider_unit_locator_to_payload,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    EncodedRemoteParseEvidenceV4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    LocalMaterializationReceiptV4,
    MaterializationIntentV4,
    RemoteParseCheckpointV4,
    ResourceReservationV4,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_ROUTE_RECEIPT_V3,
    SemanticAdjudicationTerminalV1,
    SemanticRouteReceiptRowV3,
    semantic_adjudication_terminal_v1,
    semantic_route_receipts_file_bytes_v3,
)
from disclosure_anchor.application.ports.file_store import FileStorePathPort
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RemoteParseV4Authority,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializedProviderDocumentV4,
    V4StageGuard,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.atomic_publication_inventory_v4 import (
    previous_active_unit_inventory_v4,
)
from disclosure_anchor.application.services.provider_document_admission import (
    ProviderDocumentAdmission,
)
from disclosure_anchor.application.services.provider_unit_builder import (
    build_provider_units,
)
from disclosure_anchor.application.services.semantic_router import (
    SemanticRouter,
    semantic_document_context,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.services.unit_hashing import (
    content_hash_aggregate,
    structure_hash_aggregate,
)


class AtomicPublicationRequestBuilderV4Error(ValueError):
    """Durable input cannot produce one exact transaction-P request."""


@dataclass(frozen=True, slots=True)
class _BuildAuthority:
    reservation: ResourceReservationV4
    document: e.Document
    security_code: str
    previous_run_id: str | None
    previous_units: tuple[PreviousActiveUnitV4, ...]
    intent: MaterializationIntentV4
    receipt: LocalMaterializationReceiptV4


class ProductionAtomicPublicationRequestBuilderV4:
    """Build R from exact materialized authority and current DB inventory.

    The caller owns the document producer lease across this method and
    transaction P.  This builder performs no persistence.  Transaction P
    independently locks and revalidates all currentness before it mutates any
    row, so drift between this read and P remains a hard conflict.
    """

    def __init__(
        self,
        *,
        path_builder: FileStorePathPort,
        uow_factory: Callable[[], UnitOfWork],
        admission: ProviderDocumentAdmission,
        semantic_router: SemanticRouter,
    ) -> None:
        self._paths = path_builder
        self._uow_factory = uow_factory
        self._admission = admission
        self._semantic_router = semantic_router

    def build(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        materialized: MaterializedProviderDocumentV4,
        stage_guard: V4StageGuard,
    ) -> AtomicPublicationRequestV4:
        if (
            type(checkpoint) is not RemoteParseCheckpointV4
            or checkpoint.state != "local_materialized"
            or type(materialized) is not MaterializedProviderDocumentV4
            or not callable(getattr(stage_guard, "checkpoint", None))
        ):
            raise AtomicPublicationRequestBuilderV4Error(
                "publication request builder input is invalid"
            )
        stage_guard.checkpoint()
        context = self._load_authority(
            checkpoint=checkpoint,
            materialized=materialized,
        )
        stage_guard.checkpoint()

        admitted = self._admission.admit_materialized(
            document=context.document,
            envelope=materialized.provider_envelope,
            provider_document_sha256=materialized.receipt.provider_envelope_sha256,
            expected_source_byte_count=checkpoint.source_byte_count,
            security_code=context.security_code,
        )
        if (
            type(admitted) is not AdmittedProviderDocument
            or admitted.envelope != materialized.provider_envelope
            or admitted.provider_document_sha256
            != materialized.receipt.provider_envelope_sha256
        ):
            raise AtomicPublicationRequestBuilderV4Error(
                "source admission drifted from materialized provider evidence"
            )
        stage_guard.checkpoint()
        base_build = build_provider_units(admitted)
        if (
            base_build.provider_document_sha256
            != admitted.provider_document_sha256
            or base_build.unassigned_table_parts
        ):
            raise AtomicPublicationRequestBuilderV4Error(
                "provider Unit build does not close over its admitted document"
            )
        routed = self._semantic_router.route(
            admitted=admitted,
            document=semantic_document_context(context.document),
            drafts=base_build.units,
        )
        stage_guard.checkpoint()

        units = tuple(
            self._unit(
                draft=draft,
                document=context.document,
                processing_run_id=checkpoint.processing_run_id,
                admitted=admitted,
            )
            for draft in routed.units
        )
        route_rows = tuple(
            SemanticRouteReceiptRowV3(
                processing_run_id=checkpoint.processing_run_id,
                unit_order_index=unit.unit_index,
                provider_locator_sha256=unit.provider_locator_sha256,
                routed_draft_sha256=unit.routed_draft_sha256,
                receipt=route_receipt,
            )
            for unit, route_receipt in zip(units, routed.receipts, strict=True)
        )
        previous = context.previous_units
        terminal = semantic_adjudication_terminal_v1(routed.receipts)
        upstream = seal_upstream_publication_evidence_v4(
            reservation=context.reservation,
            checkpoint=checkpoint,
            intent=context.intent,
            receipt=context.receipt,
            manifest=materialized.manifest,
            provider_envelope=materialized.provider_envelope,
        )
        projection = self._processing_projection(
            document=context.document,
            checkpoint=checkpoint,
            security_code=context.security_code,
            intent=context.intent,
            provider_document_sha256=upstream.provider_document_sha256,
            units=units,
            route_rows=route_rows,
            terminal=terminal,
        )
        projection_json = _canonical_json_text(projection)
        identity = PublicationAttemptIdentityV4(
            attempt_id=checkpoint.attempt_id,
            document_id=checkpoint.document_id,
            processing_run_id=checkpoint.processing_run_id,
            provider_document_id=upstream.provider_document_id,
            attempt_generation=checkpoint.attempt_generation,
            fence_identity=checkpoint.fence_identity,
            expected_attempt_state="local_materialized",
            expected_lifecycle_version=checkpoint.lifecycle_version,
            expected_checkpoint_sha256=checkpoint.sha256,
            expected_local_materialization_receipt_sha256=context.receipt.sha256,
            expected_previous_processing_run_id=context.previous_run_id,
        )
        stage_guard.checkpoint()
        return seal_atomic_publication_request_v4(
            identity=identity,
            upstream_evidence=upstream,
            source_page_count=checkpoint.source_page_count,
            processing_run_projection_json=projection_json,
            processing_run_projection_sha256=_digest(projection_json.encode("utf-8")),
            semantic_route_receipts_contract_version=SEMANTIC_ROUTE_RECEIPT_V3,
            semantic_route_receipts=route_rows,
            expected_unit_build_status_before="not_started",
            expected_unit_build_attempt_count_before=0,
            previous_active_units=previous,
            previous_active_units_sha256=previous_active_units_sha256_v4(previous),
            units=units,
        )

    def _load_authority(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        materialized: MaterializedProviderDocumentV4,
    ) -> _BuildAuthority:
        with self._uow_factory() as uow:
            authority = uow.remote_parse_v4.load(checkpoint.attempt_id)
            if (
                type(authority) is not RemoteParseV4Authority
                or not authority.is_current
                or authority.state != "local_materialized"
                or authority.checkpoint != checkpoint
                or authority.reservation is None
                or authority.publication_winner is not None
            ):
                raise AtomicPublicationRequestBuilderV4Error(
                    "publication durable local-materialized authority drifted"
                )
            evidence = _evidence_by_kind(authority.evidence)
            intent = evidence.get("materialization_intent")
            receipt = evidence.get("local_materialization_receipt")
            if (
                type(intent) is not MaterializationIntentV4
                or type(receipt) is not LocalMaterializationReceiptV4
                or intent != materialized.intent
                or receipt != materialized.receipt
            ):
                raise AtomicPublicationRequestBuilderV4Error(
                    "publication materialized evidence drifted from durable authority"
                )
            document = uow.documents.get(checkpoint.document_id)
            candidate = uow.processing_runs.get(checkpoint.processing_run_id)
            if (
                type(document) is not e.Document
                or type(candidate) is not e.ProcessingRun
            ):
                raise AtomicPublicationRequestBuilderV4Error(
                    "publication document or candidate run disappeared"
                )
            if document.security_id is None:
                raise AtomicPublicationRequestBuilderV4Error(
                    "publication document lacks a security identity"
                )
            security = uow.securities.get(document.security_id)
            if type(security) is not e.Security or not security.security_code:
                raise AtomicPublicationRequestBuilderV4Error(
                    "publication security identity disappeared"
                )
            self._validate_document_and_candidate(
                checkpoint=checkpoint,
                materialized=materialized,
                document=document,
                candidate=candidate,
                security_code=security.security_code,
            )
            previous_run_id = document.current_processing_run_id
            previous_units = tuple(
                uow.document_units.list_by_document_active(document.document_id)
            )
            if previous_run_id is None:
                if previous_units:
                    raise AtomicPublicationRequestBuilderV4Error(
                        "initial publication found active Unit rows"
                    )
            else:
                previous_run = uow.processing_runs.get(previous_run_id)
                if (
                    type(previous_run) is not e.ProcessingRun
                    or not previous_run.is_active
                    or previous_run.status != "succeeded"
                    or not previous_units
                    or any(
                        unit.document_id != document.document_id
                        or unit.processing_run_id != previous_run_id
                        for unit in previous_units
                    )
                ):
                    raise AtomicPublicationRequestBuilderV4Error(
                        "previous active publication inventory drifted"
                    )
            # Close projection hashes now.  P calls the same conversion after
            # row locking; any later row/pointer drift therefore conflicts.
            previous_inventory = previous_active_unit_inventory_v4(previous_units)
            reservation = authority.reservation
            assert reservation is not None
            return _BuildAuthority(
                reservation=reservation,
                document=document,
                security_code=security.security_code,
                previous_run_id=previous_run_id,
                previous_units=previous_inventory,
                intent=intent,
                receipt=receipt,
            )

    def _validate_document_and_candidate(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        materialized: MaterializedProviderDocumentV4,
        document: e.Document,
        candidate: e.ProcessingRun,
        security_code: str,
    ) -> None:
        context = materialized.intent.provider_envelope_context
        target = context.parser_target_identity
        expected_provider_document_relpath = self._paths.provider_document_relpath(
            provider=cast(str, document.provider),
            security_code=security_code,
            provider_document_id=cast(str, document.provider_document_id),
            artifact_owner_processing_run_id=checkpoint.processing_run_id,
        ).as_posix()
        if (
            document.document_id != checkpoint.document_id
            or document.provider != context.provider
            or document.provider_document_id != context.provider_document_id
            or document.raw_file_relpath != context.source_pdf_relpath
            or document.raw_file_hash != checkpoint.source_pdf_sha256
            or candidate.processing_run_id != checkpoint.processing_run_id
            or candidate.document_id != checkpoint.document_id
            or candidate.run_kind != "parse"
            or candidate.status != "running"
            or candidate.is_active
            or candidate.finished_at is not None
            or candidate.artifact_owner_processing_run_id != candidate.processing_run_id
            or candidate.input_raw_file_hash != checkpoint.source_pdf_sha256
            or candidate.parser_artifact_relpath != context.parser_artifact_root_relpath
            or candidate.parser_name != target.name
            or candidate.parser_version != target.package_version
            or candidate.parser_backend != target.backend
            or candidate.parser_method != target.method
            or candidate.parser_language != target.language
            or candidate.parser_target_identity != target.to_payload()
            or candidate.artifact_hash is not None
            or candidate.normalized_ir_relpath is not None
            or candidate.provider_document_relpath
            != expected_provider_document_relpath
            or candidate.document_units_relpath is not None
            or candidate.semantic_route_receipts_hash is not None
            or candidate.semantic_route_receipts_relpath is not None
            or candidate.semantic_route_receipts_contract_version is not None
            or candidate.semantic_adjudication_status is not None
            or candidate.semantic_degraded_unit_count is not None
            or candidate.semantic_failover_group_count is not None
            or candidate.semantic_adjudication_summary is not None
            or candidate.content_hash_aggregate is not None
            or candidate.structure_hash is not None
            or candidate.builder_rules_version is not None
            or candidate.unit_build_status != "not_started"
            or candidate.unit_build_attempt_count != 0
            or candidate.unit_build_error is not None
            or candidate.unit_built_at is not None
        ):
            raise AtomicPublicationRequestBuilderV4Error(
                "publication candidate is not the untouched V4 ingress"
            )

    @staticmethod
    def _unit(
        *,
        draft: ProviderUnitDraft,
        document: e.Document,
        processing_run_id: str,
        admitted: AdmittedProviderDocument,
    ) -> PreIdUnitPublicationV4:
        if not document.provider_document_id:
            raise AtomicPublicationRequestBuilderV4Error(
                "publication document lacks provider identity"
            )
        locator_payload = provider_unit_locator_to_payload(draft.locator)
        locator_json = _canonical_json_text(locator_payload)
        payload_json = _canonical_json_text(draft.payload)
        pages = _unit_page_numbers(
            draft=draft, provider_document=admitted.provider_document
        )
        return seal_pre_id_unit_publication_v4(
            document_id=document.document_id,
            processing_run_id=processing_run_id,
            provider_document_id=document.provider_document_id,
            unit_index=draft.unit_index + 1,
            payload_kind=draft.payload_kind,
            heading_path=draft.heading_path,
            title=draft.title,
            semantic_keys=draft.semantic_keys,
            section_keys=draft.section_keys,
            canonical_payload_json=payload_json,
            content_hash=draft.content_hash,
            structure_hash=draft.structure_hash,
            quality_status=draft.quality_status,
            applicability=draft.applicability,
            page_no=draft.page_no,
            page_numbers=pages,
            query_projection_hash=draft.query_projection_hash,
            canonical_artifact_locator_json=locator_json,
            provider_locator_sha256=_digest(locator_json.encode("utf-8")),
        )

    def _processing_projection(
        self,
        *,
        document: e.Document,
        checkpoint: RemoteParseCheckpointV4,
        security_code: str,
        intent: MaterializationIntentV4,
        provider_document_sha256: str,
        units: tuple[PreIdUnitPublicationV4, ...],
        route_rows: tuple[SemanticRouteReceiptRowV3, ...],
        terminal: SemanticAdjudicationTerminalV1,
    ) -> dict[str, Any]:
        provider = cast(str, document.provider)
        provider_document_id = cast(str, document.provider_document_id)
        context = intent.provider_envelope_context
        target = context.parser_target_identity
        run_id = checkpoint.processing_run_id
        provider_document_relpath = self._paths.provider_document_relpath(
            provider=provider,
            security_code=security_code,
            provider_document_id=provider_document_id,
            artifact_owner_processing_run_id=run_id,
        )
        document_units_relpath = self._paths.document_units_snapshot_relpath(
            provider=provider,
            security_code=security_code,
            provider_document_id=provider_document_id,
            processing_run_id=run_id,
        )
        semantic_receipts_relpath = self._paths.semantic_route_receipts_v3_relpath(
            provider=provider,
            security_code=security_code,
            provider_document_id=provider_document_id,
            processing_run_id=run_id,
        )
        semantic_bytes = semantic_route_receipts_file_bytes_v3(route_rows)
        return {
            "artifact_owner_processing_run_id": run_id,
            "builder_rules_version": PROVIDER_UNIT_BUILDER_VERSION,
            "content_hash_aggregate": content_hash_aggregate(
                unit.content_hash for unit in units
            ),
            "contract_version": "processing-run-publication.v4",
            "document_id": checkpoint.document_id,
            "document_units_relpath": document_units_relpath.as_posix(),
            "is_active": True,
            "normalized_ir_relpath": None,
            "parser_artifact_relpath": context.parser_artifact_root_relpath,
            "parser_backend": target.backend,
            "parser_language": target.language,
            "parser_method": target.method,
            "parser_name": target.name,
            "parser_target_identity": target.to_payload(),
            "parser_version": target.package_version,
            "processing_run_id": run_id,
            "provider_document_id": provider_document_id,
            "provider_document_relpath": provider_document_relpath.as_posix(),
            "provider_document_sha256": provider_document_sha256,
            "run_kind": "parse",
            "semantic_adjudication_status": terminal.status,
            "semantic_adjudication_summary": terminal.summary,
            "semantic_degraded_unit_count": terminal.degraded_unit_count,
            "semantic_failover_group_count": terminal.failover_group_count,
            "semantic_route_receipts_contract_version": SEMANTIC_ROUTE_RECEIPT_V3,
            "semantic_route_receipts_relpath": semantic_receipts_relpath.as_posix(),
            "semantic_route_receipts_sha256": _digest(semantic_bytes),
            "source_pdf_relpath": context.source_pdf_relpath,
            "source_pdf_sha256": checkpoint.source_pdf_sha256,
            "status": "succeeded",
            "structure_hash_aggregate": structure_hash_aggregate(
                unit.structure_hash for unit in units
            ),
            "unit_build_attempt_count": 1,
            "unit_build_status": "succeeded",
            "unit_count": len(units),
        }


def _evidence_by_kind(
    evidence: tuple[EncodedRemoteParseEvidenceV4, ...],
) -> dict[str, object]:
    if len({item.kind for item in evidence}) != len(evidence):
        raise AtomicPublicationRequestBuilderV4Error(
            "publication durable evidence repeats a kind"
        )
    return {item.kind: item.value for item in evidence}


def _unit_page_numbers(
    *,
    draft: ProviderUnitDraft,
    provider_document: ProviderDocument,
) -> tuple[int, ...]:
    blocks = {block.source_index: block for block in provider_document.blocks}
    source_indices: set[int] = set(draft.locator.evidence_only_block_source_indices)
    for heading in draft.locator.heading_chain:
        source_indices.add(heading.source_index)
        source_indices.update(
            item.source_index for item in heading.continuation_fragments
        )
    for unit_part in draft.locator.parts:
        source_indices.update(unit_part.block_source_indices)
    for unbound_part in draft.locator.unbound_table_parts:
        if unbound_part.part.block_source_index is not None:
            source_indices.add(unbound_part.part.block_source_index)
    source_indices.update(
        item.source_index for item in draft.locator.source_text_reconciliations
    )
    source_indices.update(
        item.source_index for item in draft.locator.source_quality_findings
    )
    try:
        pages = tuple(
            sorted(
                {
                    draft.page_no,
                    *(blocks[index].page_index + 1 for index in source_indices),
                }
            )
        )
    except KeyError as exc:
        raise AtomicPublicationRequestBuilderV4Error(
            "publication Unit locator cites an unknown provider block"
        ) from exc
    # Ancestor headings are full provenance, not the Unit-local starting page.
    # Keep them even when they precede this Unit's own primary page.
    return pages


def _canonical_json_text(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


__all__ = [
    "AtomicPublicationRequestBuilderV4Error",
    "ProductionAtomicPublicationRequestBuilderV4",
]
