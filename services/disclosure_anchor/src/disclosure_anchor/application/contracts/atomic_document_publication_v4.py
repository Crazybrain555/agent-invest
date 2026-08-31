"""Closed pre-ID request for one whole-document publication transaction."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any, cast

from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    LocalMaterializationReceiptV4,
    MaterializationIntentV4,
    ProviderEnvelopeContextV4,
    RemoteParseCheckpointV4,
    ResourceReservationV4,
    validate_materialized_provider_evidence_v4,
)
from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity
from disclosure_anchor.application.contracts.local_materialization_manifest_v4 import (
    LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
    LocalMaterializationManifestV4,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    PROVIDER_DOCUMENT_FILENAME,
    ProviderDocumentEnvelope,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_ROUTE_RECEIPT_V3,
    SemanticRouteReceiptRowV3,
    semantic_route_receipt_row_v3_from_payload,
    semantic_route_receipt_row_v3_to_payload,
    validate_semantic_route_receipt_rows_v3,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.contracts.staged_resource_paths import (
    validate_relative_resource_path_v4,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
)
from disclosure_anchor.application.contracts.provider_unit import (
    ProviderUnitLocator,
    provider_unit_locator_from_payload,
)
from disclosure_anchor.domain.services.unit_hashing import (
    compute_unit_hashes,
    content_hash_aggregate,
    structure_hash_aggregate,
)
from disclosure_anchor.domain.value_objects.semantic_key import (
    SemanticKeyInvariantError,
    validate_optional_section_keys,
    validate_optional_semantic_keys,
)


ATOMIC_PUBLICATION_REQUEST_V4_CONTRACT = "atomic-publication-request.v4"
UPSTREAM_PUBLICATION_EVIDENCE_V4_CONTRACT = "publication-upstream-evidence.v4"
PRE_ID_UNIT_PUBLICATION_V4_CONTRACT = "pre-id-unit-publication.v4"
PREVIOUS_ACTIVE_UNIT_V4_CONTRACT = "previous-active-unit.v4"
_MAX_BYTES = 8 * 1024 * 1024
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ASSET_ID = re.compile(r"du_[0-9A-HJKMNP-TV-Z]{26}\Z")
_QUERY_PROJECTION_REQUIRED_FIELDS = frozenset(
    {
        "applicability",
        "heading_path",
        "payload_kind",
        "quality_status",
        "semantic_key",
        "title",
    }
)
_QUERY_PROJECTION_FIELDS = _QUERY_PROJECTION_REQUIRED_FIELDS | {
    "mixed_part_annotations",
    "section_keys",
    "semantic_keys",
}
_PROCESSING_RUN_PROJECTION_FIELDS = frozenset(
    {
        "builder_rules_version",
        "content_hash_aggregate",
        "contract_version",
        "document_id",
        "document_units_relpath",
        "is_active",
        "parser_artifact_relpath",
        "processing_run_id",
        "provider_document_id",
        "provider_document_relpath",
        "provider_document_sha256",
        "run_kind",
        "semantic_route_receipts_contract_version",
        "semantic_route_receipts_sha256",
        "source_pdf_relpath",
        "source_pdf_sha256",
        "status",
        "structure_hash_aggregate",
        "unit_count",
    }
)


class WholeDocumentPublicationV4Error(ValueError):
    """The pre-ID publication request is not one closed whole document."""


@dataclass(frozen=True, slots=True)
class UpstreamPublicationEvidenceV4:
    attempt_id: str
    attempt_generation: int
    fence_identity: str
    document_id: str
    processing_run_id: str
    provider: str
    provider_document_id: str
    source_pdf_relpath: str
    parser_artifact_root_relpath: str
    provider_envelope_context_json: str
    provider_envelope_context_sha256: str
    local_materialized_checkpoint_sha256: str
    local_materialized_lifecycle_version: int
    source_pdf_sha256: str
    source_page_count: int
    parser_target_sha256: str
    request_sha256: str
    runtime_epoch_sha256: str
    process_profile_sha256: str
    terminal_receipt_sha256: str
    materialization_intent_sha256: str
    local_materialization_receipt_sha256: str
    output_files_sha256: str
    output_file_count: int
    output_total_byte_count: int
    output_manifest_sha256: str
    output_manifest_relpath: str
    output_manifest_byte_count: int
    provider_envelope_sha256: str
    provider_envelope_relpath: str
    provider_envelope_byte_count: int
    provider_document_sha256: str
    evidence_sha256: str
    contract_version: str = UPSTREAM_PUBLICATION_EVIDENCE_V4_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != UPSTREAM_PUBLICATION_EVIDENCE_V4_CONTRACT:
            raise WholeDocumentPublicationV4Error(
                "upstream publication evidence contract is unsupported"
            )
        for value, label in (
            (self.attempt_id, "upstream attempt"),
            (self.fence_identity, "upstream fence"),
            (self.document_id, "upstream document"),
            (self.processing_run_id, "upstream processing run"),
            (self.provider, "upstream provider"),
            (self.provider_document_id, "upstream provider document"),
        ):
            _identity(value, label)
        _positive(self.attempt_generation, "upstream attempt generation")
        for value, label in (
            (self.local_materialized_checkpoint_sha256, "local checkpoint"),
            (self.provider_envelope_context_sha256, "provider envelope context"),
            (self.source_pdf_sha256, "source PDF"),
            (self.parser_target_sha256, "parser target"),
            (self.request_sha256, "request"),
            (self.runtime_epoch_sha256, "runtime epoch"),
            (self.process_profile_sha256, "process profile"),
            (self.terminal_receipt_sha256, "terminal receipt"),
            (self.materialization_intent_sha256, "materialization intent"),
            (self.local_materialization_receipt_sha256, "local receipt"),
            (self.output_files_sha256, "output files"),
            (self.output_manifest_sha256, "output manifest"),
            (self.provider_envelope_sha256, "provider envelope"),
            (self.provider_document_sha256, "provider document"),
            (self.evidence_sha256, "upstream evidence"),
        ):
            _sha(value, label)
        _nonnegative(
            self.local_materialized_lifecycle_version,
            "local checkpoint lifecycle version",
        )
        for count_value, label in (
            (self.source_page_count, "source page count"),
            (self.output_file_count, "output file count"),
            (self.output_total_byte_count, "output byte count"),
            (self.output_manifest_byte_count, "output manifest byte count"),
            (self.provider_envelope_byte_count, "provider envelope byte count"),
        ):
            _positive(count_value, label)
        _relative_path(self.output_manifest_relpath, "output manifest")
        _relative_path(self.provider_envelope_relpath, "provider envelope")
        _relative_path(self.source_pdf_relpath, "source PDF")
        _relative_path(self.parser_artifact_root_relpath, "parser artifact root")
        if (
            PurePosixPath(self.provider_envelope_relpath).name
            != PROVIDER_DOCUMENT_FILENAME
            or PurePosixPath(self.output_manifest_relpath).name
            != LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME
            or self.provider_document_sha256 != self.provider_envelope_sha256
        ):
            raise WholeDocumentPublicationV4Error(
                "upstream materialized provider evidence drifted"
            )
        _canonical_json_text(
            self.provider_envelope_context_json,
            "provider envelope context",
        )
        context = _provider_envelope_context_v4(
            self.provider_envelope_context_json
        )
        if (
            self.provider_envelope_context_sha256 != context.sha256
            or self.provider != context.provider
            or self.provider_document_id != context.provider_document_id
            or self.document_id != context.document_id
            or self.processing_run_id != context.processing_run_id
            or self.source_pdf_relpath != context.source_pdf_relpath
            or self.source_pdf_sha256 != context.source_pdf_sha256
            or self.source_page_count != context.source_page_count
            or self.parser_artifact_root_relpath
            != context.parser_artifact_root_relpath
            or self.parser_target_sha256 != context.parser_target_sha256
        ):
            raise WholeDocumentPublicationV4Error(
                "upstream provider envelope context drifted"
            )
        if self.evidence_sha256 != upstream_publication_evidence_sha256_v4(self):
            raise WholeDocumentPublicationV4Error(
                "upstream publication evidence hash does not close"
            )

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(asdict(self))


@dataclass(frozen=True, slots=True)
class PublicationAttemptIdentityV4:
    attempt_id: str
    document_id: str
    processing_run_id: str
    provider_document_id: str
    attempt_generation: int
    fence_identity: str
    expected_attempt_state: str
    expected_lifecycle_version: int
    expected_checkpoint_sha256: str
    expected_local_materialization_receipt_sha256: str
    expected_previous_processing_run_id: str | None

    def __post_init__(self) -> None:
        for value, label in (
            (self.attempt_id, "attempt"),
            (self.document_id, "document"),
            (self.processing_run_id, "processing run"),
            (self.provider_document_id, "provider document"),
            (self.fence_identity, "fence"),
        ):
            _identity(value, label)
        _positive(self.attempt_generation, "attempt generation")
        if self.expected_attempt_state != "local_materialized":
            raise WholeDocumentPublicationV4Error(
                "publication requires local_materialized attempt state"
            )
        _nonnegative(self.expected_lifecycle_version, "expected lifecycle version")
        _sha(self.expected_checkpoint_sha256, "expected checkpoint")
        _sha(
            self.expected_local_materialization_receipt_sha256,
            "expected local materialization receipt",
        )
        if self.expected_previous_processing_run_id is not None:
            _identity(
                self.expected_previous_processing_run_id,
                "previous processing run",
            )
            if self.expected_previous_processing_run_id == self.processing_run_id:
                raise WholeDocumentPublicationV4Error(
                    "previous processing run cannot be the publication run"
                )


@dataclass(frozen=True, slots=True)
class PreviousActiveUnitV4:
    """One exact active Unit row used as transaction-P's diff basis."""

    asset_id: str
    processing_run_id: str
    order_index: int
    payload_kind: str
    heading_path: tuple[str, ...]
    content_hash: str
    query_projection_hash: str
    canonical_query_projection_json: str
    contract_version: str = PREVIOUS_ACTIVE_UNIT_V4_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != PREVIOUS_ACTIVE_UNIT_V4_CONTRACT:
            raise WholeDocumentPublicationV4Error(
                "previous-active Unit contract is unsupported"
            )
        if (
            not isinstance(self.asset_id, str)
            or _ASSET_ID.fullmatch(self.asset_id) is None
        ):
            raise WholeDocumentPublicationV4Error(
                "previous-active Unit asset ID is not canonical"
            )
        _identity(self.processing_run_id, "previous-active processing run")
        _positive(self.order_index, "previous-active Unit order")
        if self.payload_kind not in {"text", "table", "qa", "mixed"}:
            raise WholeDocumentPublicationV4Error(
                "previous-active Unit payload kind is unsupported"
            )
        _text_tuple(self.heading_path, "previous-active heading path", allow_empty=True)
        _sha(self.content_hash, "previous-active content")
        _sha(self.query_projection_hash, "previous-active query projection")
        _canonical_json_text(
            self.canonical_query_projection_json,
            "previous-active query projection",
        )
        projection = strict_json_loads(
            self.canonical_query_projection_json.encode("utf-8")
        )
        if (
            not isinstance(projection, dict)
            or not _QUERY_PROJECTION_REQUIRED_FIELDS.issubset(projection)
            or not set(projection).issubset(_QUERY_PROJECTION_FIELDS)
            or projection.get("payload_kind") != self.payload_kind
            or projection.get("heading_path") != list(self.heading_path)
            or self.query_projection_hash
            != _digest(self.canonical_query_projection_json.encode("utf-8"))
        ):
            raise WholeDocumentPublicationV4Error(
                "previous-active Unit projection does not close"
            )
        title = projection["title"]
        semantic_key = projection["semantic_key"]
        quality_status = projection["quality_status"]
        applicability = projection["applicability"]
        semantic_keys = projection.get("semantic_keys")
        section_keys = projection.get("section_keys")
        if (
            (title is not None and (not isinstance(title, str) or not title))
            or (
                semantic_key is not None
                and (not isinstance(semantic_key, str) or not semantic_key)
            )
            or quality_status not in {"ok", "needs_review", "unusable"}
            or applicability not in {None, "applicable", "not_applicable"}
            or (
                semantic_keys is not None
                and (
                    not isinstance(semantic_keys, list)
                    or not semantic_keys
                    or any(
                        not isinstance(item, str) or not item
                        for item in semantic_keys
                    )
                    or semantic_keys[0] != semantic_key
                )
            )
            or (
                section_keys is not None
                and (
                    not isinstance(section_keys, list)
                    or not section_keys
                    or any(
                        not isinstance(item, str) or not item
                        for item in section_keys
                    )
                )
            )
            or (
                self.payload_kind != "mixed"
                and "mixed_part_annotations" in projection
            )
        ):
            raise WholeDocumentPublicationV4Error(
                "previous-active Unit projection values are invalid"
            )


@dataclass(frozen=True, slots=True)
class PreIdUnitPublicationV4:
    document_id: str
    processing_run_id: str
    provider_document_id: str
    unit_index: int
    payload_kind: str
    heading_path: tuple[str, ...]
    title: str | None
    semantic_keys: tuple[str, ...] | None
    section_keys: tuple[str, ...] | None
    canonical_payload_json: str
    content_hash: str
    structure_hash: str
    quality_status: str
    applicability: str | None
    page_no: int | None
    page_numbers: tuple[int, ...]
    query_projection_hash: str
    canonical_artifact_locator_json: str
    provider_locator_sha256: str
    routed_draft_sha256: str
    contract_version: str = PRE_ID_UNIT_PUBLICATION_V4_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != PRE_ID_UNIT_PUBLICATION_V4_CONTRACT:
            raise WholeDocumentPublicationV4Error(
                "pre-ID Unit publication contract is unsupported"
            )
        for value, label in (
            (self.document_id, "Unit document"),
            (self.processing_run_id, "Unit processing run"),
            (self.provider_document_id, "Unit provider document"),
        ):
            _identity(value, label)
        _positive(self.unit_index, "Unit index")
        if self.payload_kind not in {"text", "table", "qa", "mixed"}:
            raise WholeDocumentPublicationV4Error("Unit payload kind is unsupported")
        _text_tuple(self.heading_path, "heading path", allow_empty=True)
        if self.title is not None:
            _text(self.title, "Unit title")
        try:
            validate_optional_semantic_keys(
                None if self.semantic_keys is None else list(self.semantic_keys)
            )
            validate_optional_section_keys(
                None if self.section_keys is None else list(self.section_keys)
            )
        except SemanticKeyInvariantError as exc:
            raise WholeDocumentPublicationV4Error(
                "pre-ID Unit semantic key invariants failed"
            ) from exc
        _canonical_json_text(self.canonical_payload_json, "canonical payload")
        _canonical_json_text(
            self.canonical_artifact_locator_json, "canonical artifact locator"
        )
        for value, label in (
            (self.content_hash, "content"),
            (self.structure_hash, "structure"),
            (self.query_projection_hash, "query projection"),
            (self.provider_locator_sha256, "provider locator"),
            (self.routed_draft_sha256, "routed draft"),
        ):
            _sha(value, label)
        if self.provider_locator_sha256 != _digest(
            self.canonical_artifact_locator_json.encode("utf-8")
        ):
            raise WholeDocumentPublicationV4Error(
                "provider locator hash differs from canonical artifact locator"
            )
        payload = strict_json_loads(self.canonical_payload_json.encode("utf-8"))
        if not isinstance(payload, dict):
            raise WholeDocumentPublicationV4Error("Unit payload must be an object")
        try:
            expected_hashes = compute_unit_hashes(
                payload_kind=self.payload_kind,
                payload=cast(dict[str, Any], payload),
                title=self.title,
                heading_path=list(self.heading_path),
                semantic_keys=(
                    None if self.semantic_keys is None else list(self.semantic_keys)
                ),
                section_keys=(
                    None if self.section_keys is None else list(self.section_keys)
                ),
                quality_status=self.quality_status,
                applicability=self.applicability,
                order_index=self.unit_index,
            )
        except (TypeError, ValueError) as exc:
            raise WholeDocumentPublicationV4Error(
                "pre-ID Unit hash projection is invalid"
            ) from exc
        if (
            self.content_hash,
            self.structure_hash,
            self.query_projection_hash,
        ) != (
            expected_hashes.content_hash,
            expected_hashes.structure_hash,
            expected_hashes.query_projection_hash,
        ):
            raise WholeDocumentPublicationV4Error(
                "pre-ID Unit hashes differ from exact projection"
            )
        locator_payload = strict_json_loads(
            self.canonical_artifact_locator_json.encode("utf-8")
        )
        try:
            locator = provider_unit_locator_from_payload(locator_payload)
        except (TypeError, ValueError) as exc:
            raise WholeDocumentPublicationV4Error(
                "pre-ID Unit locator contract is invalid"
            ) from exc
        if type(locator) is not ProviderUnitLocator or locator.unit_index != self.unit_index - 1:
            raise WholeDocumentPublicationV4Error(
                "pre-ID Unit locator index differs from Unit order"
            )
        if self.title is None:
            if self.heading_path or locator.heading_chain:
                raise WholeDocumentPublicationV4Error(
                    "unheaded Unit cannot claim heading context"
                )
        elif not self.heading_path or self.heading_path[-1] != self.title:
            raise WholeDocumentPublicationV4Error(
                "Unit title must end its heading path"
            )
        if len(self.heading_path) != len(locator.heading_chain):
            raise WholeDocumentPublicationV4Error(
                "Unit heading path differs from provider locator"
            )
        if self.quality_status not in {"ok", "needs_review", "unusable"}:
            raise WholeDocumentPublicationV4Error("Unit quality status is unsupported")
        if self.applicability not in {None, "applicable", "not_applicable"}:
            raise WholeDocumentPublicationV4Error("Unit applicability is unsupported")
        if self.page_no is not None:
            _positive(self.page_no, "Unit page number")
        if not self.page_numbers:
            raise WholeDocumentPublicationV4Error("Unit lineage lacks page numbers")
        if tuple(sorted(set(self.page_numbers))) != self.page_numbers:
            raise WholeDocumentPublicationV4Error(
                "Unit lineage page numbers are not unique and ordered"
            )
        for page_number in self.page_numbers:
            _positive(page_number, "Unit lineage page number")
        if self.page_no is not None and self.page_no != self.page_numbers[0]:
            raise WholeDocumentPublicationV4Error(
                "Unit primary page differs from its lineage"
            )
        expected = pre_id_unit_routed_draft_sha256_v4(self)
        if self.routed_draft_sha256 != expected:
            raise WholeDocumentPublicationV4Error(
                "pre-ID Unit routed-draft hash does not close"
            )

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(_pre_id_unit_payload(self))


@dataclass(frozen=True, slots=True)
class AtomicPublicationRequestV4:
    identity: PublicationAttemptIdentityV4
    upstream_evidence: UpstreamPublicationEvidenceV4
    source_page_count: int
    processing_run_projection_json: str
    processing_run_projection_sha256: str
    semantic_route_receipts_contract_version: str
    semantic_route_receipts: tuple[SemanticRouteReceiptRowV3, ...]
    previous_active_units: tuple[PreviousActiveUnitV4, ...]
    previous_active_units_sha256: str
    units: tuple[PreIdUnitPublicationV4, ...]
    request_sha256: str
    contract_version: str = ATOMIC_PUBLICATION_REQUEST_V4_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != ATOMIC_PUBLICATION_REQUEST_V4_CONTRACT:
            raise WholeDocumentPublicationV4Error(
                "atomic publication request contract is unsupported"
            )
        if type(self.identity) is not PublicationAttemptIdentityV4:
            raise WholeDocumentPublicationV4Error(
                "publication request lacks exact attempt identity"
            )
        if type(self.upstream_evidence) is not UpstreamPublicationEvidenceV4:
            raise WholeDocumentPublicationV4Error(
                "publication request lacks exact upstream evidence"
            )
        if (
            self.identity.document_id != self.units[0].document_id
            if self.units
            else True
        ):
            raise WholeDocumentPublicationV4Error("publication request document drifted")
        _positive(self.source_page_count, "publication source page count")
        if self.source_page_count != self.upstream_evidence.source_page_count:
            raise WholeDocumentPublicationV4Error(
                "publication source page count drifted"
            )
        if (
            self.identity.attempt_id != self.upstream_evidence.attempt_id
            or self.identity.fence_identity != self.upstream_evidence.fence_identity
            or self.identity.document_id != self.upstream_evidence.document_id
            or self.identity.processing_run_id
            != self.upstream_evidence.processing_run_id
            or self.identity.provider_document_id
            != self.upstream_evidence.provider_document_id
            or self.identity.attempt_generation
            != self.upstream_evidence.attempt_generation
            or self.identity.expected_checkpoint_sha256
            != self.upstream_evidence.local_materialized_checkpoint_sha256
            or self.identity.expected_lifecycle_version
            != self.upstream_evidence.local_materialized_lifecycle_version
            or self.identity.expected_local_materialization_receipt_sha256
            != self.upstream_evidence.local_materialization_receipt_sha256
        ):
            raise WholeDocumentPublicationV4Error(
                "publication identity drifted from upstream evidence"
            )
        if not isinstance(self.previous_active_units, tuple) or any(
            type(item) is not PreviousActiveUnitV4
            for item in self.previous_active_units
        ):
            raise WholeDocumentPublicationV4Error(
                "previous-active Unit inventory must be an exact tuple"
            )
        ordered_previous = tuple(
            sorted(
                self.previous_active_units,
                key=lambda item: (item.order_index, item.asset_id),
            )
        )
        if ordered_previous != self.previous_active_units:
            raise WholeDocumentPublicationV4Error(
                "previous-active Unit inventory is not canonically ordered"
            )
        if (
            len({item.asset_id for item in self.previous_active_units})
            != len(self.previous_active_units)
            or len({item.order_index for item in self.previous_active_units})
            != len(self.previous_active_units)
        ):
            raise WholeDocumentPublicationV4Error(
                "previous-active Unit inventory contains duplicate identity"
            )
        if tuple(item.order_index for item in self.previous_active_units) != tuple(
            range(1, len(self.previous_active_units) + 1)
        ):
            raise WholeDocumentPublicationV4Error(
                "previous-active Unit inventory is not contiguous"
            )
        previous_run_id = self.identity.expected_previous_processing_run_id
        if previous_run_id is None:
            if self.previous_active_units:
                raise WholeDocumentPublicationV4Error(
                    "initial publication cannot carry previous-active Units"
                )
        elif not self.previous_active_units:
            raise WholeDocumentPublicationV4Error(
                "previous active run requires its complete Unit inventory"
            )
        elif any(
            item.processing_run_id != previous_run_id
            for item in self.previous_active_units
        ):
            raise WholeDocumentPublicationV4Error(
                "previous-active Unit run identity drifted"
            )
        _sha(self.previous_active_units_sha256, "previous-active Unit inventory")
        if self.previous_active_units_sha256 != previous_active_units_sha256_v4(
            self.previous_active_units
        ):
            raise WholeDocumentPublicationV4Error(
                "previous-active Unit inventory hash does not close"
            )
        _canonical_json_text(
            self.processing_run_projection_json, "processing-run projection"
        )
        processing_projection = _processing_run_projection_v4(
            self.processing_run_projection_json
        )
        _sha(self.processing_run_projection_sha256, "processing-run projection")
        if self.processing_run_projection_sha256 != _digest(
            self.processing_run_projection_json.encode("utf-8")
        ):
            raise WholeDocumentPublicationV4Error(
                "processing-run projection hash drifted"
            )
        if (
            processing_projection["processing_run_id"]
            != self.identity.processing_run_id
            or processing_projection["document_id"] != self.identity.document_id
            or processing_projection["provider_document_id"]
            != self.identity.provider_document_id
            or processing_projection["source_pdf_sha256"]
            != self.upstream_evidence.source_pdf_sha256
            or processing_projection["provider_document_sha256"]
            != self.upstream_evidence.provider_document_sha256
            or processing_projection["unit_count"] != len(self.units)
            or processing_projection["semantic_route_receipts_contract_version"]
            != SEMANTIC_ROUTE_RECEIPT_V3
            or processing_projection["semantic_route_receipts_sha256"]
            != _digest(
                _canonical_json(
                    [
                        semantic_route_receipt_row_v3_to_payload(item)
                        for item in self.semantic_route_receipts
                    ]
                )
            )
            or processing_projection["content_hash_aggregate"]
            != content_hash_aggregate(item.content_hash for item in self.units)
            or processing_projection["structure_hash_aggregate"]
            != structure_hash_aggregate(item.structure_hash for item in self.units)
        ):
            raise WholeDocumentPublicationV4Error(
                "processing-run projection identity drifted"
            )
        expected_paths = _processing_run_resource_paths_v4(
            self.upstream_evidence,
        )
        if any(
            processing_projection[name] != expected
            for name, expected in expected_paths.items()
        ):
            raise WholeDocumentPublicationV4Error(
                "processing-run projection resource path drifted"
            )
        if self.semantic_route_receipts_contract_version != SEMANTIC_ROUTE_RECEIPT_V3:
            raise WholeDocumentPublicationV4Error(
                "publication request requires semantic receipt v3"
            )
        if not isinstance(self.semantic_route_receipts, tuple) or not isinstance(
            self.units, tuple
        ):
            raise WholeDocumentPublicationV4Error(
                "publication request rows must be exact tuples"
            )
        if not self.units or tuple(item.unit_index for item in self.units) != tuple(
            range(1, len(self.units) + 1)
        ):
            raise WholeDocumentPublicationV4Error(
                "publication Units are not contiguous and ordered"
            )
        if any(
            item.document_id != self.identity.document_id
            or item.processing_run_id != self.identity.processing_run_id
            or item.provider_document_id != self.identity.provider_document_id
            or max(item.page_numbers) > self.source_page_count
            for item in self.units
        ):
            raise WholeDocumentPublicationV4Error(
                "publication Unit identity or page closure drifted"
            )
        validate_semantic_route_receipt_rows_v3(
            self.semantic_route_receipts,
            processing_run_id=self.identity.processing_run_id,
        )
        if len(self.semantic_route_receipts) != len(self.units):
            raise WholeDocumentPublicationV4Error(
                "publication semantic receipt count differs from Units"
            )
        for unit, receipt in zip(
            self.units, self.semantic_route_receipts, strict=True
        ):
            if (
                unit.unit_index != receipt.unit_order_index
                or unit.provider_locator_sha256
                != receipt.provider_locator_sha256
                or unit.routed_draft_sha256 != receipt.routed_draft_sha256
                or tuple(receipt.receipt.semantic_keys)
                != (() if unit.semantic_keys is None else unit.semantic_keys)
            ):
                raise WholeDocumentPublicationV4Error(
                    "publication semantic receipt identity drifted"
                )
            locator = provider_unit_locator_from_payload(
                strict_json_loads(unit.canonical_artifact_locator_json.encode("utf-8"))
            )
            if locator.provider_document_sha256 != self.upstream_evidence.provider_document_sha256:
                raise WholeDocumentPublicationV4Error(
                    "publication Unit locator cites another provider document"
                )
        _sha(self.request_sha256, "publication request")
        if self.request_sha256 != atomic_publication_request_sha256_v4(self):
            raise WholeDocumentPublicationV4Error(
                "atomic publication request hash does not close"
            )

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(_request_payload(self))


def seal_upstream_publication_evidence_v4(
    *,
    reservation: ResourceReservationV4,
    checkpoint: RemoteParseCheckpointV4,
    intent: MaterializationIntentV4,
    receipt: LocalMaterializationReceiptV4,
    manifest: LocalMaterializationManifestV4,
    provider_envelope: ProviderDocumentEnvelope,
) -> UpstreamPublicationEvidenceV4:
    context = intent.provider_envelope_context
    expected_held_credit = ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=checkpoint.source_byte_count,
        provider_tasks=1,
        provider_result_bytes=intent.artifact_byte_count,
        compressed_bytes=intent.artifact_byte_count,
        output_items=1,
        output_bytes=receipt.output_byte_count,
        output_pages=receipt.source_page_count,
        ack_items=1,
    )
    if (
        type(reservation) is not ResourceReservationV4
        or checkpoint.state != "local_materialized"
        or checkpoint.held_resource_credit != expected_held_credit
        or (
            checkpoint.attempt_id,
            checkpoint.attempt_generation,
            checkpoint.fence_identity,
            checkpoint.document_id,
            checkpoint.processing_run_id,
            checkpoint.source_pdf_sha256,
            checkpoint.source_byte_count,
            checkpoint.source_page_count,
            checkpoint.request_sha256,
            checkpoint.runtime_epoch_sha256,
            checkpoint.process_profile_sha256,
            checkpoint.credit_policy_sha256,
            checkpoint.reservation_input_sha256,
        )
        != (
            reservation.attempt_id,
            reservation.attempt_generation,
            reservation.fence_identity,
            reservation.document_id,
            reservation.processing_run_id,
            reservation.source_pdf_sha256,
            reservation.source_byte_count,
            reservation.source_page_count,
            reservation.request_sha256,
            reservation.runtime_epoch_sha256,
            reservation.process_profile_sha256,
            reservation.credit_policy_sha256,
            reservation.reservation_input_sha256,
        )
        or intent.reservation_sha256 != reservation.sha256
        or intent.result_byte_limit
        != reservation.reserved_credit.provider_result_bytes
        or intent.decoded_byte_limit != reservation.reserved_credit.decoded_bytes
        or intent.temporary_disk_byte_limit
        != reservation.reserved_credit.temp_disk_bytes
        or intent.output_byte_limit != reservation.reserved_credit.output_bytes
        or intent.output_page_limit != reservation.source_page_count
        or checkpoint.materialization_intent_sha256 != intent.sha256
        or checkpoint.local_materialization_receipt_sha256 != receipt.sha256
        or receipt.materialization_intent_sha256 != intent.sha256
        or (
            checkpoint.attempt_id,
            checkpoint.fence_identity,
            checkpoint.document_id,
            checkpoint.processing_run_id,
        )
        != (
            intent.attempt_id,
            intent.fence_identity,
            intent.document_id,
            intent.processing_run_id,
        )
        or (
            checkpoint.attempt_id,
            checkpoint.fence_identity,
            checkpoint.document_id,
            checkpoint.processing_run_id,
        )
        != (
            receipt.attempt_id,
            receipt.fence_identity,
            receipt.document_id,
            receipt.processing_run_id,
        )
        or checkpoint.terminal_receipt_sha256 != intent.terminal_receipt_sha256
        or receipt.source_page_count != checkpoint.source_page_count
        or receipt.parser_target_sha256 != intent.parser_target_sha256
        or receipt.spool_relpath != intent.spool_relpath
        or receipt.spool_sha256 != intent.artifact_sha256
        or receipt.spool_byte_count != intent.artifact_byte_count
        or receipt.output_relpath != intent.output_relpath
        or receipt.terminal_receipt_sha256 != intent.terminal_receipt_sha256
        or receipt.source_pdf_sha256 != intent.source_pdf_sha256
        or receipt.provider_envelope_relpath != intent.provider_envelope_relpath
        or receipt.output_manifest_relpath != intent.output_manifest_relpath
        or context.document_id != intent.document_id
        or context.processing_run_id != intent.processing_run_id
        or context.source_pdf_sha256 != intent.source_pdf_sha256
        or context.source_page_count != intent.source_page_count
        or context.parser_target_sha256 != intent.parser_target_sha256
    ):
        raise WholeDocumentPublicationV4Error(
            "publication bridge lacks exact local-materialized evidence"
        )
    try:
        validate_materialized_provider_evidence_v4(
            intent=intent,
            receipt=receipt,
            manifest=manifest,
            provider_envelope=provider_envelope,
        )
    except (TypeError, ValueError) as exc:
        raise WholeDocumentPublicationV4Error(
            "publication bridge lacks exact local-materialized evidence"
        ) from exc
    values: dict[str, Any] = {
        "attempt_id": checkpoint.attempt_id,
        "attempt_generation": checkpoint.attempt_generation,
        "fence_identity": checkpoint.fence_identity,
        "document_id": checkpoint.document_id,
        "processing_run_id": checkpoint.processing_run_id,
        "provider": context.provider,
        "provider_document_id": context.provider_document_id,
        "source_pdf_relpath": context.source_pdf_relpath,
        "parser_artifact_root_relpath": context.parser_artifact_root_relpath,
        "provider_envelope_context_json": context.canonical_bytes.decode("utf-8"),
        "provider_envelope_context_sha256": context.sha256,
        "local_materialized_checkpoint_sha256": checkpoint.sha256,
        "local_materialized_lifecycle_version": checkpoint.lifecycle_version,
        "source_pdf_sha256": checkpoint.source_pdf_sha256,
        "source_page_count": checkpoint.source_page_count,
        "parser_target_sha256": intent.parser_target_sha256,
        "request_sha256": checkpoint.request_sha256,
        "runtime_epoch_sha256": checkpoint.runtime_epoch_sha256,
        "process_profile_sha256": checkpoint.process_profile_sha256,
        "terminal_receipt_sha256": intent.terminal_receipt_sha256,
        "materialization_intent_sha256": intent.sha256,
        "local_materialization_receipt_sha256": receipt.sha256,
        "output_files_sha256": receipt.output_files_sha256,
        "output_file_count": receipt.output_file_count,
        "output_total_byte_count": receipt.output_byte_count,
        "output_manifest_sha256": receipt.output_manifest_sha256,
        "output_manifest_relpath": receipt.output_manifest_relpath,
        "output_manifest_byte_count": receipt.output_manifest_byte_count,
        "provider_envelope_sha256": receipt.provider_envelope_sha256,
        "provider_envelope_relpath": receipt.provider_envelope_relpath,
        "provider_envelope_byte_count": receipt.provider_envelope_byte_count,
        "provider_document_sha256": receipt.provider_envelope_sha256,
        "contract_version": UPSTREAM_PUBLICATION_EVIDENCE_V4_CONTRACT,
    }
    return UpstreamPublicationEvidenceV4(
        **values,
        evidence_sha256=_digest(_canonical_json(values)),
    )


def upstream_publication_evidence_sha256_v4(
    value: UpstreamPublicationEvidenceV4,
) -> str:
    payload = asdict(value)
    payload.pop("evidence_sha256", None)
    return _digest(_canonical_json(payload))


def pre_id_unit_routed_draft_sha256_v4(value: PreIdUnitPublicationV4) -> str:
    payload = _pre_id_unit_payload(value)
    payload.pop("routed_draft_sha256", None)
    return _digest(_canonical_json(payload))


def atomic_publication_request_sha256_v4(
    value: AtomicPublicationRequestV4,
) -> str:
    payload = _request_payload(value)
    payload.pop("request_sha256", None)
    return _digest(_canonical_json(payload))


def seal_pre_id_unit_publication_v4(**values: Any) -> PreIdUnitPublicationV4:
    if "routed_draft_sha256" in values:
        raise WholeDocumentPublicationV4Error("routed-draft hash is derived")
    sealed_values = {
        **values,
        "contract_version": values.get(
            "contract_version", PRE_ID_UNIT_PUBLICATION_V4_CONTRACT
        ),
    }
    return PreIdUnitPublicationV4(
        **sealed_values,
        routed_draft_sha256=_digest(
            _canonical_json(_pre_id_unit_unsealed_payload(sealed_values))
        ),
    )


def seal_atomic_publication_request_v4(**values: Any) -> AtomicPublicationRequestV4:
    if "request_sha256" in values:
        raise WholeDocumentPublicationV4Error("publication request hash is derived")
    sealed_values = {
        **values,
        "contract_version": values.get(
            "contract_version", ATOMIC_PUBLICATION_REQUEST_V4_CONTRACT
        ),
    }
    return AtomicPublicationRequestV4(
        **sealed_values,
        request_sha256=_digest(
            _canonical_json(_request_unsealed_payload(sealed_values))
        ),
    )


def previous_active_units_sha256_v4(
    units: tuple[PreviousActiveUnitV4, ...],
) -> str:
    if not isinstance(units, tuple) or any(
        type(item) is not PreviousActiveUnitV4 for item in units
    ):
        raise WholeDocumentPublicationV4Error(
            "previous-active Unit inventory must be an exact tuple"
        )
    return _digest(
        _canonical_json([_previous_active_unit_payload(item) for item in units])
    )


def decode_atomic_publication_request_v4(
    exact_bytes: bytes,
) -> AtomicPublicationRequestV4:
    if type(exact_bytes) is not bytes or not 1 <= len(exact_bytes) <= _MAX_BYTES:
        raise WholeDocumentPublicationV4Error(
            "atomic publication request bytes are outside the envelope"
        )
    decoded = strict_json_loads(exact_bytes)
    if not isinstance(decoded, dict):
        raise WholeDocumentPublicationV4Error(
            "atomic publication request must be an object"
        )
    root = cast(dict[str, Any], decoded)
    _closed(root, AtomicPublicationRequestV4)
    identity = _nested(root["identity"], PublicationAttemptIdentityV4)
    upstream = _nested(root["upstream_evidence"], UpstreamPublicationEvidenceV4)
    raw_units = root["units"]
    raw_previous_units = root["previous_active_units"]
    raw_receipts = root["semantic_route_receipts"]
    if (
        not isinstance(raw_units, list)
        or not isinstance(raw_previous_units, list)
        or not isinstance(raw_receipts, list)
    ):
        raise WholeDocumentPublicationV4Error("publication rows must be arrays")
    units = tuple(_decode_pre_id_unit(item) for item in raw_units)
    previous_units = tuple(
        _decode_previous_active_unit(item) for item in raw_previous_units
    )
    receipts = tuple(
        semantic_route_receipt_row_v3_from_payload(item) for item in raw_receipts
    )
    value = AtomicPublicationRequestV4(
        **{
            **root,
            "identity": identity,
            "upstream_evidence": upstream,
            "units": units,
            "previous_active_units": previous_units,
            "semantic_route_receipts": receipts,
        }
    )
    if value.canonical_bytes != exact_bytes:
        raise WholeDocumentPublicationV4Error(
            "atomic publication request JSON is not canonical"
        )
    return value


def _decode_pre_id_unit(value: object) -> PreIdUnitPublicationV4:
    if not isinstance(value, dict):
        raise WholeDocumentPublicationV4Error("pre-ID Unit must be an object")
    root = cast(dict[str, Any], value)
    _closed(root, PreIdUnitPublicationV4)
    return PreIdUnitPublicationV4(
        **{
            **root,
            "heading_path": _string_tuple(root["heading_path"], "heading path"),
            "semantic_keys": _optional_string_tuple(
                root["semantic_keys"], "semantic keys"
            ),
            "section_keys": _optional_string_tuple(
                root["section_keys"], "section keys"
            ),
            "page_numbers": _integer_tuple(root["page_numbers"], "page numbers"),
        }
    )


def _decode_previous_active_unit(value: object) -> PreviousActiveUnitV4:
    if not isinstance(value, dict):
        raise WholeDocumentPublicationV4Error(
            "previous-active Unit must be an object"
        )
    root = cast(dict[str, Any], value)
    _closed(root, PreviousActiveUnitV4)
    return PreviousActiveUnitV4(
        **{
            **root,
            "heading_path": _string_tuple(
                root["heading_path"], "previous-active heading path"
            ),
        }
    )


def _request_payload(value: AtomicPublicationRequestV4) -> dict[str, Any]:
    return {
        "contract_version": value.contract_version,
        "identity": asdict(value.identity),
        "processing_run_projection_json": value.processing_run_projection_json,
        "processing_run_projection_sha256": value.processing_run_projection_sha256,
        "previous_active_units": [
            _previous_active_unit_payload(item)
            for item in value.previous_active_units
        ],
        "previous_active_units_sha256": value.previous_active_units_sha256,
        "request_sha256": value.request_sha256,
        "semantic_route_receipts": [
            semantic_route_receipt_row_v3_to_payload(item)
            for item in value.semantic_route_receipts
        ],
        "semantic_route_receipts_contract_version": (
            value.semantic_route_receipts_contract_version
        ),
        "source_page_count": value.source_page_count,
        "units": [_pre_id_unit_payload(item) for item in value.units],
        "upstream_evidence": asdict(value.upstream_evidence),
    }


def _pre_id_unit_payload(value: PreIdUnitPublicationV4) -> dict[str, Any]:
    payload = asdict(value)
    payload["heading_path"] = list(value.heading_path)
    payload["semantic_keys"] = (
        None if value.semantic_keys is None else list(value.semantic_keys)
    )
    payload["section_keys"] = (
        None if value.section_keys is None else list(value.section_keys)
    )
    payload["page_numbers"] = list(value.page_numbers)
    return payload


def _previous_active_unit_payload(value: PreviousActiveUnitV4) -> dict[str, Any]:
    payload = asdict(value)
    payload["heading_path"] = list(value.heading_path)
    return payload


def _pre_id_unit_unsealed_payload(values: dict[str, Any]) -> dict[str, Any]:
    expected = {field.name for field in fields(PreIdUnitPublicationV4)} - {
        "routed_draft_sha256"
    }
    if set(values) != expected:
        raise WholeDocumentPublicationV4Error(
            "pre-ID Unit sealing fields are not closed"
        )
    payload = dict(values)
    for name in ("heading_path", "semantic_keys", "section_keys", "page_numbers"):
        value = payload[name]
        payload[name] = None if value is None else list(value)
    return payload


def _request_unsealed_payload(values: dict[str, Any]) -> dict[str, Any]:
    expected = {field.name for field in fields(AtomicPublicationRequestV4)} - {
        "request_sha256"
    }
    if set(values) != expected:
        raise WholeDocumentPublicationV4Error(
            "atomic publication request sealing fields are not closed"
        )
    identity = values["identity"]
    upstream = values["upstream_evidence"]
    units = values["units"]
    previous_units = values["previous_active_units"]
    receipts = values["semantic_route_receipts"]
    if type(identity) is not PublicationAttemptIdentityV4:
        raise WholeDocumentPublicationV4Error(
            "publication request lacks exact attempt identity"
        )
    if type(upstream) is not UpstreamPublicationEvidenceV4:
        raise WholeDocumentPublicationV4Error(
            "publication request lacks exact upstream evidence"
        )
    if (
        not isinstance(units, tuple)
        or not isinstance(previous_units, tuple)
        or not isinstance(receipts, tuple)
    ):
        raise WholeDocumentPublicationV4Error(
            "publication request rows must be exact tuples"
        )
    return {
        "contract_version": values["contract_version"],
        "identity": asdict(identity),
        "processing_run_projection_json": values[
            "processing_run_projection_json"
        ],
        "processing_run_projection_sha256": values[
            "processing_run_projection_sha256"
        ],
        "previous_active_units": [
            _previous_active_unit_payload(item) for item in previous_units
        ],
        "previous_active_units_sha256": values[
            "previous_active_units_sha256"
        ],
        "semantic_route_receipts": [
            semantic_route_receipt_row_v3_to_payload(item) for item in receipts
        ],
        "semantic_route_receipts_contract_version": values[
            "semantic_route_receipts_contract_version"
        ],
        "source_page_count": values["source_page_count"],
        "units": [_pre_id_unit_payload(item) for item in units],
        "upstream_evidence": asdict(upstream),
    }


def _nested(value: object, item_type: type[Any]) -> Any:
    if not isinstance(value, dict):
        raise WholeDocumentPublicationV4Error(
            f"{item_type.__name__} must be an object"
        )
    root = cast(dict[str, Any], value)
    _closed(root, item_type)
    return item_type(**root)


def _closed(value: dict[str, Any], item_type: type[Any]) -> None:
    if set(value) != {item.name for item in fields(item_type)}:
        raise WholeDocumentPublicationV4Error(
            f"{item_type.__name__} fields are not closed"
        )


def _canonical_json(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WholeDocumentPublicationV4Error(
            "publication record is not strict JSON"
        ) from exc
    if not 1 <= len(encoded) <= _MAX_BYTES:
        raise WholeDocumentPublicationV4Error(
            "publication record bytes are outside the envelope"
        )
    return encoded


def _canonical_json_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise WholeDocumentPublicationV4Error(f"{label} must be canonical JSON text")
    exact = value.encode("utf-8")
    decoded = strict_json_loads(exact)
    if _canonical_json(decoded) != exact:
        raise WholeDocumentPublicationV4Error(f"{label} is not canonical JSON")


def _processing_run_projection_v4(value: str) -> dict[str, Any]:
    decoded = strict_json_loads(value.encode("utf-8"))
    if not isinstance(decoded, dict) or set(decoded) != _PROCESSING_RUN_PROJECTION_FIELDS:
        raise WholeDocumentPublicationV4Error(
            "processing-run projection fields are not closed"
        )
    projection = cast(dict[str, Any], decoded)
    if projection["contract_version"] != "processing-run-publication.v4":
        raise WholeDocumentPublicationV4Error(
            "processing-run projection contract is unsupported"
        )
    for name in (
        "processing_run_id",
        "document_id",
        "provider_document_id",
        "builder_rules_version",
    ):
        _identity(projection[name], f"processing-run {name}")
    if projection["run_kind"] not in {"parse", "rebuild_units"}:
        raise WholeDocumentPublicationV4Error("processing-run kind is unsupported")
    if projection["status"] != "succeeded" or projection["is_active"] is not True:
        raise WholeDocumentPublicationV4Error(
            "processing-run projection is not an active success"
        )
    _positive(projection["unit_count"], "processing-run Unit count")
    for name in (
        "source_pdf_sha256",
        "provider_document_sha256",
        "semantic_route_receipts_sha256",
        "content_hash_aggregate",
        "structure_hash_aggregate",
    ):
        _sha(projection[name], f"processing-run {name}")
    for name in (
        "parser_artifact_relpath",
        "provider_document_relpath",
        "document_units_relpath",
        "source_pdf_relpath",
    ):
        _relative_path(projection[name], f"processing-run {name}")
    if projection["semantic_route_receipts_contract_version"] != SEMANTIC_ROUTE_RECEIPT_V3:
        raise WholeDocumentPublicationV4Error(
            "processing-run semantic receipt contract drifted"
        )
    return projection


def _provider_envelope_context_v4(value: str) -> ProviderEnvelopeContextV4:
    decoded = strict_json_loads(value.encode("utf-8"))
    if not isinstance(decoded, dict):
        raise WholeDocumentPublicationV4Error(
            "provider envelope context must be an object"
        )
    root = cast(dict[str, Any], decoded)
    if set(root) != {item.name for item in fields(ProviderEnvelopeContextV4)}:
        raise WholeDocumentPublicationV4Error(
            "provider envelope context fields are not closed"
        )
    raw_target = root["parser_target_identity"]
    if not isinstance(raw_target, dict) or set(raw_target) != {
        item.name for item in fields(ParserTargetIdentity)
    }:
        raise WholeDocumentPublicationV4Error(
            "provider envelope parser target fields are not closed"
        )
    try:
        return ProviderEnvelopeContextV4(
            **{
                **root,
                "parser_target_identity": ParserTargetIdentity(**raw_target),
            }
        )
    except (TypeError, ValueError) as exc:
        raise WholeDocumentPublicationV4Error(
            "provider envelope context is invalid"
        ) from exc


def _processing_run_resource_paths_v4(
    upstream: UpstreamPublicationEvidenceV4,
) -> dict[str, str]:
    """Derive the only final resource paths admitted by transaction P.

    The upstream provider context has already proved the source and parser-root
    topology.  Publication may relocate the materialized provider envelope and
    Unit snapshot only to the service's fixed durable namespaces; accepting
    caller-selected paths here would detach the final processing row from that
    evidence.
    """

    # These values were closed by ProviderEnvelopeContextV4 before its hash
    # entered the materialization intent and upstream evidence.
    source_parts = PurePosixPath(upstream.source_pdf_relpath).parts
    if len(source_parts) != 6:
        raise WholeDocumentPublicationV4Error(
            "upstream source path topology drifted"
        )
    security_code = source_parts[2]
    provider_document_id = upstream.provider_document_id
    run_id = upstream.processing_run_id
    durable_root = PurePosixPath("derived")
    return {
        "source_pdf_relpath": upstream.source_pdf_relpath,
        "parser_artifact_relpath": upstream.parser_artifact_root_relpath,
        "provider_document_relpath": str(
            durable_root
            / "provider_documents"
            / upstream.provider
            / security_code
            / provider_document_id
            / run_id
            / PROVIDER_DOCUMENT_FILENAME
        ),
        "document_units_relpath": str(
            durable_root
            / "document_unit_snapshots"
            / upstream.provider
            / security_code
            / provider_document_id
            / run_id
            / "document_units.v1.jsonl"
        ),
    }


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise WholeDocumentPublicationV4Error(f"{label} hash is not canonical")


def _identity(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 512
        or any(ord(character) < 0x20 for character in value)
    ):
        raise WholeDocumentPublicationV4Error(f"{label} identity is invalid")


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise WholeDocumentPublicationV4Error(f"{label} must be nonempty text")


def _text_tuple(
    value: tuple[str, ...], label: str, *, allow_empty: bool = False
) -> None:
    if not isinstance(value, tuple) or (not allow_empty and not value):
        raise WholeDocumentPublicationV4Error(f"{label} must be an exact tuple")
    if any(not isinstance(item, str) or not item for item in value):
        raise WholeDocumentPublicationV4Error(f"{label} contains invalid text")


def _positive(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WholeDocumentPublicationV4Error(f"{label} must be positive")


def _nonnegative(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WholeDocumentPublicationV4Error(f"{label} must be non-negative")


def _relative_path(value: str, label: str) -> None:
    try:
        validate_relative_resource_path_v4(value, label)
    except ValueError as exc:
        raise WholeDocumentPublicationV4Error(f"{label} path is unsafe") from exc


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise WholeDocumentPublicationV4Error(f"{label} must be a string array")
    return tuple(value)


def _optional_string_tuple(value: object, label: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    return _string_tuple(value, label)


def _integer_tuple(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        raise WholeDocumentPublicationV4Error(f"{label} must be an integer array")
    return tuple(value)


__all__ = [
    "ATOMIC_PUBLICATION_REQUEST_V4_CONTRACT",
    "AtomicPublicationRequestV4",
    "PRE_ID_UNIT_PUBLICATION_V4_CONTRACT",
    "PREVIOUS_ACTIVE_UNIT_V4_CONTRACT",
    "PreIdUnitPublicationV4",
    "PreviousActiveUnitV4",
    "PublicationAttemptIdentityV4",
    "UPSTREAM_PUBLICATION_EVIDENCE_V4_CONTRACT",
    "UpstreamPublicationEvidenceV4",
    "WholeDocumentPublicationV4Error",
    "atomic_publication_request_sha256_v4",
    "decode_atomic_publication_request_v4",
    "pre_id_unit_routed_draft_sha256_v4",
    "previous_active_units_sha256_v4",
    "seal_atomic_publication_request_v4",
    "seal_pre_id_unit_publication_v4",
    "seal_upstream_publication_evidence_v4",
    "upstream_publication_evidence_sha256_v4",
]
