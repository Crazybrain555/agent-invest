"""Pure document-unit assembly followed by the independent source audit."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from disclosure_anchor.application.contracts.canonical_occurrence import (
    canonical_occurrence_stream,
)
from disclosure_anchor.application.contracts.source_evidence import (
    SourceEvidenceProof,
)
from disclosure_anchor.application.contracts.source_evidence_projection import (
    SourceEvidenceProjectionError,
)
from disclosure_anchor.application.services.unit_builder.builder import (
    BuildStats,
    ImageArtifactResolver,
    SourceEvidenceClosureError,
    UnitDraft,
    build_unit_drafts_s1_s7,
)
from disclosure_anchor.application.services.unit_builder.source_native_fallback import (
    bind_visual_page_evidence,
    native_stream_unit_drafts,
)
from disclosure_anchor.application.services.document_unit_audit import (
    AuditDocumentMetadata,
    AuditUnitView,
    DocumentAuditReport,
    audit_document,
)


def prepare_and_audit_units(
    *,
    normalized_ir: dict[str, Any],
    filing_type: str | None,
    metadata: AuditDocumentMetadata,
    image_artifact_resolver: ImageArtifactResolver | None,
    image_hash_provider: Callable[[], Mapping[str, str]],
    source_proof: SourceEvidenceProof,
) -> tuple[list[UnitDraft], BuildStats, DocumentAuditReport]:
    """Run the only unit-assembly composition used by publication and replay."""

    try:
        stream = canonical_occurrence_stream(normalized_ir, source_proof)
    except SourceEvidenceProjectionError as exc:
        raise SourceEvidenceClosureError(str(exc)) from exc
    element_orders = {
        source_item_index: order_index
        for element in normalized_ir.get("elements", ())
        if isinstance(element, Mapping)
        and isinstance(
            (source_item_index := element.get("source_item_index")), int
        )
        and not isinstance(source_item_index, bool)
        and isinstance((order_index := element.get("order_index")), int)
        and not isinstance(order_index, bool)
    }
    drafts, stats = build_unit_drafts_s1_s7(
        normalized_ir,
        filing_type=filing_type,
        image_artifact_resolver=image_artifact_resolver,
        native_units=native_stream_unit_drafts(
            stream,
            element_orders=element_orders,
        ),
    )
    drafts = bind_visual_page_evidence(drafts, source_proof)
    for page in stream.pages:
        if page.order_basis == "provider_attested":
            stats.provider_attested_pages += 1
        if page.span_overlap_count:
            stats.span_overlap_pages += 1
        stats.order_conflict_events += page.order_conflict_count
    report = audit_document(
        normalized_ir=normalized_ir,
        units=(
            AuditUnitView(
                order_index=index,
                payload_kind=draft.payload_kind,
                payload=draft.payload,
                title=draft.title,
                heading_path=draft.heading_path,
                semantic_key=draft.semantic_key,
                semantic_keys=draft.semantic_keys,
                quality_status=draft.quality_status,
                applicability=draft.applicability,
                artifact_locator=draft.artifact_locator,
            )
            for index, draft in enumerate(drafts, start=1)
        ),
        metadata=metadata,
        source_proof=source_proof,
        source_dispositions=stats.source_dispositions,
        image_hashes=image_hash_provider(),
    )
    return drafts, stats, report
