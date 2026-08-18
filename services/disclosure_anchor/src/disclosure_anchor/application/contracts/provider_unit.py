"""Small DB-free Unit drafts projected from one admitted provider document."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Literal, cast

from disclosure_anchor.application.contracts.provider_table_projection import (
    ProviderTablePartRef,
    UnboundProviderTablePart,
    UnboundTablePartReason,
)
from disclosure_anchor.application.contracts.document_outline import (
    HeadingPlacementSource,
)
from disclosure_anchor.application.contracts.retrieval_primary import (
    RetrievalTarget,
    SearchTransform,
)
from disclosure_anchor.domain.value_objects.semantic_key import (
    SemanticKeyInvariantError,
    validate_optional_section_keys,
    validate_optional_semantic_key_state,
)


LEGACY_PROVIDER_UNIT_LOCATOR_VERSION = "provider_unit_locator.v1"
SOURCE_REPAIR_PROVIDER_UNIT_LOCATOR_VERSION = "provider_unit_locator.v2"
PROVIDER_UNIT_LOCATOR_VERSION = "provider_unit_locator.v3"
SUPPORTED_PROVIDER_UNIT_LOCATOR_VERSIONS = frozenset(
    {
        LEGACY_PROVIDER_UNIT_LOCATOR_VERSION,
        SOURCE_REPAIR_PROVIDER_UNIT_LOCATOR_VERSION,
        PROVIDER_UNIT_LOCATOR_VERSION,
    }
)
PROVIDER_UNIT_BUILDER_VERSION = "provider_unit.v9"

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

ProviderUnitPayloadKind = Literal["text", "table", "mixed"]
ProviderUnitPartKind = Literal["text", "table", "visual"]
ProviderUnitApplicability = Literal["applicable", "not_applicable"]
ProviderSearchDestinationKind = Literal[
    "unit_title",
    "unit_payload",
    "mixed_part",
]


class ProviderUnitSearchContractError(ValueError):
    """A persisted provider Unit cannot replay its explicit search bindings."""


@dataclass(frozen=True, slots=True)
class ProviderUnitHeadingRef:
    """One source heading in a Unit's root-to-leaf path."""

    heading_id: str
    source_index: int
    payload_ordinal: int
    placement_source: HeadingPlacementSource

    def __post_init__(self) -> None:
        if (
            not self.heading_id
            or self.source_index < 0
            or self.payload_ordinal < 0
            or not self.placement_source
        ):
            raise ValueError("provider unit heading reference is invalid")


@dataclass(frozen=True, slots=True)
class ProviderUnitPartRef:
    """One payload part and the provider occurrences it owns."""

    part_index: int
    kind: ProviderUnitPartKind
    block_source_indices: tuple[int, ...]
    physical_table_segment_indices: tuple[int, ...] = ()
    logical_table_index: int | None = None

    def __post_init__(self) -> None:
        if self.part_index < 0 or self.kind not in {"text", "table", "visual"}:
            raise ValueError("provider unit part identity is invalid")
        if not self.block_source_indices:
            raise ValueError("provider unit part must own a provider block")
        for values, label in (
            (self.block_source_indices, "block"),
            (self.physical_table_segment_indices, "physical table segment"),
        ):
            if tuple(sorted(values)) != values or len(values) != len(set(values)):
                raise ValueError(
                    f"provider unit part {label} indices must be unique and ordered"
                )
        if self.logical_table_index is not None:
            if self.kind != "table" or self.logical_table_index < 0:
                raise ValueError("logical table reference must belong to a table part")
            if len(self.block_source_indices) != len(
                self.physical_table_segment_indices
            ):
                raise ValueError(
                    "logical table part must pair every block and physical segment"
                )


@dataclass(frozen=True, slots=True)
class ProviderSearchDestination:
    """Closed destination for one provider payload retrieval target."""

    kind: ProviderSearchDestinationKind
    part_index: int | None = None
    field: str | None = None
    item_index: int | None = None

    def __post_init__(self) -> None:
        if self.kind == "unit_title":
            if any(
                value is not None
                for value in (self.part_index, self.field, self.item_index)
            ):
                raise ValueError("unit title destination cannot claim payload fields")
            return
        if not self.field:
            raise ValueError("provider search payload destination needs a field")
        if self.item_index is not None and self.item_index < 0:
            raise ValueError("provider search destination item index cannot be negative")
        if self.kind == "unit_payload":
            if self.part_index is not None:
                raise ValueError("unit payload destination cannot claim a mixed part")
        elif self.kind == "mixed_part":
            if self.part_index is None or self.part_index < 0:
                raise ValueError("mixed destination requires a part index")
        else:
            raise ValueError("provider search destination kind is unsupported")


@dataclass(frozen=True, slots=True)
class ProviderUnitSearchBinding:
    """One exact source target bound to one raw Unit payload destination."""

    source: RetrievalTarget
    destination: ProviderSearchDestination


@dataclass(frozen=True, slots=True)
class ProviderUnitEvidenceArtifact:
    """One path-free evidence descriptor authorized by this Unit."""

    sha256: str
    size_bytes: int
    media_type: str

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("provider Unit evidence hash must be canonical")
        if self.size_bytes < 0 or not self.media_type:
            raise ValueError("provider Unit evidence metadata is invalid")


@dataclass(frozen=True, slots=True)
class ProviderUnitSourceTextReconciliation:
    """Path-free provenance for one native-PDF numeric text correction."""

    source_index: int
    payload_ordinal: int
    raw_block_sha256: str
    provider_text_sha256: str
    source_text_sha256: str
    source_kind: str

    def __post_init__(self) -> None:
        if min(self.source_index, self.payload_ordinal) < 0:
            raise ValueError("provider Unit source reconciliation index is invalid")
        if self.source_kind != "source_pdf_native_numeric.v1":
            raise ValueError("provider Unit source reconciliation kind is unsupported")
        if not all(
            _SHA256_RE.fullmatch(value)
            for value in (
                self.raw_block_sha256,
                self.provider_text_sha256,
                self.source_text_sha256,
            )
        ):
            raise ValueError("provider Unit source reconciliation hash is invalid")


@dataclass(frozen=True, slots=True)
class ProviderUnitLocator:
    """One thin locator; the hash-bound ProviderDocument supplies all detail."""

    provider_document_sha256: str
    unit_index: int
    heading_chain: tuple[ProviderUnitHeadingRef, ...]
    parts: tuple[ProviderUnitPartRef, ...]
    evidence_only_block_source_indices: tuple[int, ...]
    unbound_table_parts: tuple[UnboundProviderTablePart, ...]
    evidence_artifacts: tuple[ProviderUnitEvidenceArtifact, ...]
    search_targets: tuple[ProviderUnitSearchBinding, ...]
    source_text_reconciliations: tuple[
        ProviderUnitSourceTextReconciliation, ...
    ] = ()
    contract_version: str = PROVIDER_UNIT_LOCATOR_VERSION

    def __post_init__(self) -> None:
        if self.contract_version not in SUPPORTED_PROVIDER_UNIT_LOCATOR_VERSIONS:
            raise ValueError("provider unit locator version is unsupported")
        if (
            self.contract_version == LEGACY_PROVIDER_UNIT_LOCATOR_VERSION
            and self.source_text_reconciliations
        ):
            raise ValueError("legacy provider unit locator cannot claim source repairs")
        if not _SHA256_RE.fullmatch(self.provider_document_sha256):
            raise ValueError("provider unit locator hash must be canonical")
        if self.unit_index < 0:
            raise ValueError("provider unit locator index cannot be negative")
        if tuple(part.part_index for part in self.parts) != tuple(
            range(len(self.parts))
        ):
            raise ValueError("provider unit part indices must be contiguous")
        evidence = self.evidence_only_block_source_indices
        if tuple(sorted(evidence)) != evidence or len(evidence) != len(set(evidence)):
            raise ValueError("evidence-only block indices must be unique and ordered")
        search_ids = [binding.source.target_id for binding in self.search_targets]
        if len(search_ids) != len(set(search_ids)):
            raise ValueError("provider unit locator cannot repeat a search target")
        evidence_hashes = [artifact.sha256 for artifact in self.evidence_artifacts]
        if len(evidence_hashes) != len(set(evidence_hashes)):
            raise ValueError("provider unit locator cannot repeat evidence bytes")
        reconciliation_ids = [
            (item.source_index, item.payload_ordinal)
            for item in self.source_text_reconciliations
        ]
        if reconciliation_ids != sorted(reconciliation_ids) or len(
            reconciliation_ids
        ) != len(set(reconciliation_ids)):
            raise ValueError(
                "provider unit source reconciliations must be unique and ordered"
            )
        if any(
            part.part.block_source_index is None
            for part in self.unbound_table_parts
        ):
            raise ValueError(
                "provider unit locator cannot claim a segment-only unbound table part"
            )


@dataclass(frozen=True, slots=True)
class ProviderUnitDraft:
    """One persistence-ready draft without a DB asset identity."""

    unit_index: int
    payload_kind: ProviderUnitPayloadKind
    payload: dict[str, object]
    title: str | None
    heading_path: tuple[str, ...]
    section_keys: tuple[str, ...] | None
    semantic_key: str | None
    semantic_keys: tuple[str, ...] | None
    applicability: ProviderUnitApplicability | None
    quality_status: str
    page_no: int
    locator: ProviderUnitLocator
    content_hash: str
    query_projection_hash: str
    structure_hash: str

    def __post_init__(self) -> None:
        if self.unit_index < 0 or self.locator.unit_index != self.unit_index:
            raise ValueError("provider unit draft index is invalid")
        if self.payload_kind not in {"text", "table", "mixed"}:
            raise ValueError("provider unit payload kind is unsupported")
        if self.title is None:
            if self.heading_path or self.locator.heading_chain:
                raise ValueError("unheaded provider unit cannot claim a heading path")
        elif not self.heading_path or self.heading_path[-1] != self.title:
            raise ValueError("provider unit title must end its heading path")
        if len(self.heading_path) != len(self.locator.heading_chain):
            raise ValueError("provider unit heading chain differs from its path")
        try:
            validate_optional_section_keys(
                list(self.section_keys) if self.section_keys is not None else None,
            )
            validate_optional_semantic_key_state(
                self.semantic_key,
                list(self.semantic_keys) if self.semantic_keys is not None else None,
            )
        except SemanticKeyInvariantError as exc:
            raise ValueError("provider unit semantic routes are invalid") from exc
        if self.applicability not in {None, "applicable", "not_applicable"}:
            raise ValueError("provider unit applicability is invalid")
        if not self.quality_status or self.page_no < 1:
            raise ValueError("provider unit quality and page must be complete")
        for value in (
            self.content_hash,
            self.query_projection_hash,
            self.structure_hash,
        ):
            if not _SHA256_RE.fullmatch(value):
                raise ValueError("provider unit hashes must be canonical")


@dataclass(frozen=True, slots=True)
class ProviderUnitBuildResult:
    """Complete DB-free Unit result and table evidence without a semantic owner."""

    provider_document_sha256: str
    units: tuple[ProviderUnitDraft, ...]
    unassigned_table_parts: tuple[UnboundProviderTablePart, ...]

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.provider_document_sha256):
            raise ValueError("provider Unit build hash must be canonical")
        if tuple(unit.unit_index for unit in self.units) != tuple(
            range(len(self.units))
        ):
            raise ValueError("provider Unit build indices must be contiguous")
        segment_indices = [
            part.part.physical_segment_index
            for part in self.unassigned_table_parts
        ]
        if any(
            part.part.block_source_index is not None
            or part.part.physical_segment_index is None
            for part in self.unassigned_table_parts
        ):
            raise ValueError("unassigned table parts must be segment-only")
        if len(segment_indices) != len(set(segment_indices)):
            raise ValueError("unassigned table parts cannot repeat a segment")


def provider_unit_locator_to_payload(
    locator: ProviderUnitLocator,
) -> dict[str, object]:
    """Serialize the one closed locator without provider paths or raw JSON."""

    payload: dict[str, object] = {
        "contract_version": locator.contract_version,
        "provider_document_sha256": locator.provider_document_sha256,
        "unit_index": locator.unit_index,
        "heading_chain": [
            {
                "heading_id": heading.heading_id,
                "placement_source": heading.placement_source,
                "source_index": heading.source_index,
                **(
                    {"payload_ordinal": heading.payload_ordinal}
                    if locator.contract_version == PROVIDER_UNIT_LOCATOR_VERSION
                    else {}
                ),
            }
            for heading in locator.heading_chain
        ],
        "parts": [
            {
                "block_source_indices": list(part.block_source_indices),
                "kind": part.kind,
                "logical_table_index": part.logical_table_index,
                "part_index": part.part_index,
                "physical_table_segment_indices": list(
                    part.physical_table_segment_indices
                ),
            }
            for part in locator.parts
        ],
        "evidence_only_block_source_indices": list(
            locator.evidence_only_block_source_indices
        ),
        "unbound_table_parts": [
            {
                "block_source_index": part.part.block_source_index,
                "physical_table_segment_index": (
                    part.part.physical_segment_index
                ),
                "reason": part.reason,
            }
            for part in locator.unbound_table_parts
        ],
        "evidence_artifacts": [
            {
                "media_type": artifact.media_type,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
            }
            for artifact in locator.evidence_artifacts
        ],
        "search_targets": [
            {
                "destination": {
                    "field": binding.destination.field,
                    "item_index": binding.destination.item_index,
                    "kind": binding.destination.kind,
                    "part_index": binding.destination.part_index,
                },
                "field": binding.source.field,
                "item_index": binding.source.item_index,
                "payload_ordinal": binding.source.payload_ordinal,
                "raw_block_sha256": binding.source.raw_block_sha256,
                "source_index": binding.source.source_index,
                "target_id": binding.source.target_id,
                "transform": binding.source.transform,
            }
            for binding in locator.search_targets
        ],
    }
    if locator.contract_version in {
        SOURCE_REPAIR_PROVIDER_UNIT_LOCATOR_VERSION,
        PROVIDER_UNIT_LOCATOR_VERSION,
    }:
        payload["source_text_reconciliations"] = [
            {
                "payload_ordinal": item.payload_ordinal,
                "provider_text_sha256": item.provider_text_sha256,
                "raw_block_sha256": item.raw_block_sha256,
                "source_index": item.source_index,
                "source_kind": item.source_kind,
                "source_text_sha256": item.source_text_sha256,
            }
            for item in locator.source_text_reconciliations
        ]
    return payload


def provider_unit_locator_from_payload(payload: object) -> ProviderUnitLocator:
    """Decode the persisted locator with no permissive fallback."""

    if not isinstance(payload, Mapping):
        raise ValueError("provider Unit locator fields are invalid")
    version = payload.get("contract_version")
    base_fields = {
        "contract_version",
        "provider_document_sha256",
        "unit_index",
        "heading_chain",
        "parts",
        "evidence_only_block_source_indices",
        "unbound_table_parts",
        "evidence_artifacts",
        "search_targets",
    }
    if version in {
        SOURCE_REPAIR_PROVIDER_UNIT_LOCATOR_VERSION,
        PROVIDER_UNIT_LOCATOR_VERSION,
    }:
        base_fields.add("source_text_reconciliations")
    elif version != LEGACY_PROVIDER_UNIT_LOCATOR_VERSION:
        raise ValueError("provider unit locator version is unsupported")
    root = _closed_mapping(
        payload,
        fields=base_fields,
        label="provider Unit locator",
    )
    headings = tuple(
        _heading_from_payload(item, version=version)
        for item in _array(root["heading_chain"], label="heading chain")
    )
    parts = tuple(
        _part_from_payload(item)
        for item in _array(root["parts"], label="provider Unit parts")
    )
    unbound = tuple(
        _unbound_from_payload(item)
        for item in _array(
            root["unbound_table_parts"],
            label="unbound table parts",
        )
    )
    evidence = tuple(
        _evidence_from_payload(item)
        for item in _array(root["evidence_artifacts"], label="evidence artifacts")
    )
    search = tuple(
        _search_binding_from_payload(item)
        for item in _array(root["search_targets"], label="search targets")
    )
    reconciliations = (
        tuple(
            _source_text_reconciliation_from_payload(item)
            for item in _array(
                root["source_text_reconciliations"],
                label="source text reconciliations",
            )
        )
        if version
        in {
            SOURCE_REPAIR_PROVIDER_UNIT_LOCATOR_VERSION,
            PROVIDER_UNIT_LOCATOR_VERSION,
        }
        else ()
    )
    return ProviderUnitLocator(
        contract_version=_text(root["contract_version"], label="contract version"),
        provider_document_sha256=_text(
            root["provider_document_sha256"],
            label="provider document hash",
        ),
        unit_index=_integer(root["unit_index"], label="unit index"),
        heading_chain=headings,
        parts=parts,
        evidence_only_block_source_indices=_integer_tuple(
            root["evidence_only_block_source_indices"],
            label="evidence-only block indices",
        ),
        unbound_table_parts=unbound,
        evidence_artifacts=evidence,
        source_text_reconciliations=reconciliations,
        search_targets=search,
    )


def _heading_from_payload(
    payload: object,
    *,
    version: object,
) -> ProviderUnitHeadingRef:
    fields = {"heading_id", "placement_source", "source_index"}
    if version == PROVIDER_UNIT_LOCATOR_VERSION:
        fields.add("payload_ordinal")
    item = _closed_mapping(
        payload,
        fields=fields,
        label="provider Unit heading",
    )
    return ProviderUnitHeadingRef(
        heading_id=_text(item["heading_id"], label="heading id"),
        source_index=_integer(item["source_index"], label="heading source index"),
        payload_ordinal=(
            _integer(item["payload_ordinal"], label="heading payload ordinal")
            if version == PROVIDER_UNIT_LOCATOR_VERSION
            else 0
        ),
        placement_source=cast(
            HeadingPlacementSource,
            _text(item["placement_source"], label="heading placement source"),
        ),
    )


def _part_from_payload(payload: object) -> ProviderUnitPartRef:
    item = _closed_mapping(
        payload,
        fields={
            "block_source_indices",
            "kind",
            "logical_table_index",
            "part_index",
            "physical_table_segment_indices",
        },
        label="provider Unit part",
    )
    return ProviderUnitPartRef(
        part_index=_integer(item["part_index"], label="part index"),
        kind=cast(
            ProviderUnitPartKind,
            _text(item["kind"], label="part kind"),
        ),
        block_source_indices=_integer_tuple(
            item["block_source_indices"],
            label="part block indices",
        ),
        physical_table_segment_indices=_integer_tuple(
            item["physical_table_segment_indices"],
            label="part segment indices",
        ),
        logical_table_index=_optional_integer(
            item["logical_table_index"],
            label="logical table index",
        ),
    )


def _unbound_from_payload(payload: object) -> UnboundProviderTablePart:
    item = _closed_mapping(
        payload,
        fields={"block_source_index", "physical_table_segment_index", "reason"},
        label="provider Unit unbound table part",
    )
    return UnboundProviderTablePart(
        part=ProviderTablePartRef(
            block_source_index=_optional_integer(
                item["block_source_index"],
                label="unbound block index",
            ),
            physical_segment_index=_optional_integer(
                item["physical_table_segment_index"],
                label="unbound segment index",
            ),
        ),
        reason=cast(
            UnboundTablePartReason,
            _text(item["reason"], label="unbound reason"),
        ),
    )


def _evidence_from_payload(payload: object) -> ProviderUnitEvidenceArtifact:
    item = _closed_mapping(
        payload,
        fields={"media_type", "sha256", "size_bytes"},
        label="provider Unit evidence artifact",
    )
    return ProviderUnitEvidenceArtifact(
        sha256=_text(item["sha256"], label="evidence hash"),
        size_bytes=_integer(item["size_bytes"], label="evidence size"),
        media_type=_text(item["media_type"], label="evidence media type"),
    )


def _source_text_reconciliation_from_payload(
    payload: object,
) -> ProviderUnitSourceTextReconciliation:
    item = _closed_mapping(
        payload,
        fields={
            "payload_ordinal",
            "provider_text_sha256",
            "raw_block_sha256",
            "source_index",
            "source_kind",
            "source_text_sha256",
        },
        label="provider Unit source text reconciliation",
    )
    return ProviderUnitSourceTextReconciliation(
        source_index=_integer(item["source_index"], label="source index"),
        payload_ordinal=_integer(
            item["payload_ordinal"],
            label="payload ordinal",
        ),
        raw_block_sha256=_text(item["raw_block_sha256"], label="block hash"),
        provider_text_sha256=_text(
            item["provider_text_sha256"],
            label="provider text hash",
        ),
        source_text_sha256=_text(
            item["source_text_sha256"],
            label="source text hash",
        ),
        source_kind=_text(item["source_kind"], label="source kind"),
    )


def _search_binding_from_payload(payload: object) -> ProviderUnitSearchBinding:
    item = _closed_mapping(
        payload,
        fields={
            "destination",
            "field",
            "item_index",
            "payload_ordinal",
            "raw_block_sha256",
            "source_index",
            "target_id",
            "transform",
        },
        label="provider Unit search binding",
    )
    destination = _closed_mapping(
        item["destination"],
        fields={"field", "item_index", "kind", "part_index"},
        label="provider Unit search destination",
    )
    return ProviderUnitSearchBinding(
        source=RetrievalTarget(
            target_id=_text(item["target_id"], label="target id"),
            source_index=_integer(item["source_index"], label="target source index"),
            payload_ordinal=_integer(
                item["payload_ordinal"],
                label="target payload ordinal",
            ),
            field=_text(item["field"], label="target field"),
            item_index=_optional_integer(item["item_index"], label="target item index"),
            transform=cast(
                SearchTransform,
                _text(item["transform"], label="target transform"),
            ),
            raw_block_sha256=_text(
                item["raw_block_sha256"],
                label="target block hash",
            ),
        ),
        destination=ProviderSearchDestination(
            kind=cast(
                ProviderSearchDestinationKind,
                _text(destination["kind"], label="destination kind"),
            ),
            part_index=_optional_integer(
                destination["part_index"],
                label="destination part index",
            ),
            field=_optional_text(destination["field"], label="destination field"),
            item_index=_optional_integer(
                destination["item_index"],
                label="destination item index",
            ),
        ),
    )


def _closed_mapping(
    value: object,
    *,
    fields: set[str],
    label: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{label} fields are invalid")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} keys must be strings")
    return cast(Mapping[str, object], value)


def _array(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return cast(list[object], value)


def _integer_tuple(value: object, *, label: str) -> tuple[int, ...]:
    return tuple(_integer(item, label=label) for item in _array(value, label=label))


def _integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _optional_integer(value: object, *, label: str) -> int | None:
    return None if value is None else _integer(value, label=label)


def _text(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    return value


def _optional_text(value: object, *, label: str) -> str | None:
    return None if value is None else _text(value, label=label)


__all__ = [
    "LEGACY_PROVIDER_UNIT_LOCATOR_VERSION",
    "PROVIDER_UNIT_LOCATOR_VERSION",
    "SOURCE_REPAIR_PROVIDER_UNIT_LOCATOR_VERSION",
    "PROVIDER_UNIT_BUILDER_VERSION",
    "SUPPORTED_PROVIDER_UNIT_LOCATOR_VERSIONS",
    "ProviderSearchDestination",
    "ProviderSearchDestinationKind",
    "ProviderUnitBuildResult",
    "ProviderUnitApplicability",
    "ProviderUnitDraft",
    "ProviderUnitEvidenceArtifact",
    "ProviderUnitHeadingRef",
    "ProviderUnitLocator",
    "ProviderUnitPartKind",
    "ProviderUnitPartRef",
    "ProviderUnitPayloadKind",
    "ProviderUnitSearchBinding",
    "ProviderUnitSearchContractError",
    "ProviderUnitSourceTextReconciliation",
    "provider_unit_locator_from_payload",
    "provider_unit_locator_to_payload",
]
