"""Immutable filesystem authority required before atomic publication v4.

The PostgreSQL publication transaction cannot make filesystem artifacts
atomic with its rows.  These contracts close the other side of that boundary:
one immutable preparation intent assigns ordinary Unit IDs and exact resource
plans, then a readiness manifest is written last after every planned resource
has been durably verified.  Transaction P accepts only the opaque witness
issued from that exact pair.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any, cast

from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    AtomicPublicationRequestV4,
    decode_atomic_publication_request_v4,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_ROUTE_RECEIPT_V3,
    semantic_route_receipts_file_bytes_v3,
)
from disclosure_anchor.application.contracts.staged_resource_paths import (
    validate_relative_resource_path_v4,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads


ATOMIC_PUBLICATION_PREPARATION_V1 = "atomic-publication-preparation.v1"
ATOMIC_PUBLICATION_READINESS_V1 = "atomic-publication-readiness.v1"
ATOMIC_PUBLICATION_READINESS_REFERENCE_V1 = (
    "atomic-publication-readiness-reference.v1"
)
ATOMIC_PUBLICATION_PREPARATION_FILENAME = (
    "atomic_publication_preparation.v1.json"
)
ATOMIC_PUBLICATION_READINESS_FILENAME = "atomic_publication_readiness.v1.json"

_MAX_PREPARATION_BYTES = 24 * 1024 * 1024
_MAX_READINESS_BYTES = 8 * 1024 * 1024
_MAX_IDENTITY_BYTES = 512
_MAX_COUNT = (1 << 63) - 1
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ASSET_ID = re.compile(r"du_[0-9A-HJKMNP-TV-Z]{26}\Z")
_FILE_ROLES = {
    "provider_document",
    "document_unit_snapshot",
    "semantic_route_receipts",
}


class AtomicPublicationArtifactReadinessError(ValueError):
    """Preparation/readiness bytes are not one closed publication bundle."""


class AtomicPublicationArtifactConflict(RuntimeError):
    """An immutable preparation or resource already has different bytes."""


@dataclass(frozen=True, slots=True)
class AtomicPublicationUnitBindingV4:
    unit_index: int
    asset_id: str
    routed_draft_sha256: str
    final_unit_row_sha256: str
    lineage_row_sha256: str

    def __post_init__(self) -> None:
        _positive(self.unit_index, "Unit binding index")
        if not isinstance(self.asset_id, str) or _ASSET_ID.fullmatch(self.asset_id) is None:
            raise AtomicPublicationArtifactReadinessError(
                "Unit binding asset ID is not canonical"
            )
        _sha(self.routed_draft_sha256, "routed draft")
        _sha(self.final_unit_row_sha256, "final Unit row")
        _sha(self.lineage_row_sha256, "lineage row")


@dataclass(frozen=True, slots=True)
class AtomicPublicationFileResourceV1:
    role: str
    relpath: str
    sha256: str
    byte_count: int
    resource_contract_version: str

    def __post_init__(self) -> None:
        if self.role not in _FILE_ROLES:
            raise AtomicPublicationArtifactReadinessError(
                "publication resource role is unsupported"
            )
        _relative(self.relpath, f"{self.role} resource")
        _sha(self.sha256, f"{self.role} resource")
        _positive(self.byte_count, f"{self.role} byte count")
        _identity(self.resource_contract_version, f"{self.role} contract")
        expected_suffix = {
            "provider_document": "provider_document.v1.json",
            "document_unit_snapshot": "document_units.v1.jsonl",
            "semantic_route_receipts": "semantic_route_receipts.v3.jsonl",
        }[self.role]
        if not self.relpath.endswith("/" + expected_suffix):
            raise AtomicPublicationArtifactReadinessError(
                f"{self.role} resource path has the wrong fixed filename"
            )
        expected_contract = {
            "provider_document": "provider_document.v1",
            "document_unit_snapshot": "document_units.v1",
            "semantic_route_receipts": SEMANTIC_ROUTE_RECEIPT_V3,
        }[self.role]
        if self.resource_contract_version != expected_contract:
            raise AtomicPublicationArtifactReadinessError(
                f"{self.role} resource contract is unsupported"
            )


@dataclass(frozen=True, slots=True)
class AtomicPublicationParserOutputPlanV1:
    source_relpath: str
    published_relpath: str
    inventory_sha256: str
    file_count: int
    byte_count: int

    def __post_init__(self) -> None:
        _relative(self.source_relpath, "parser output source")
        _relative(self.published_relpath, "parser output target")
        if self.source_relpath == self.published_relpath:
            raise AtomicPublicationArtifactReadinessError(
                "parser output source and target must differ"
            )
        _sha(self.inventory_sha256, "parser output inventory")
        _positive(self.file_count, "parser output file count")
        _positive(self.byte_count, "parser output byte count")


@dataclass(frozen=True, slots=True)
class AtomicPublicationArtifactPreparationV1:
    attempt_id: str
    attempt_generation: int
    fence_identity: str
    document_id: str
    processing_run_id: str
    provider_document_id: str
    canonical_request_json: str
    request_sha256: str
    request_byte_count: int
    artifact_owner_processing_run_id: str
    parser_target_sha256: str
    provider_envelope_context_sha256: str
    unit_bindings: tuple[AtomicPublicationUnitBindingV4, ...]
    final_units_sha256: str
    lineage_sha256: str
    parser_output_plan: AtomicPublicationParserOutputPlanV1
    provider_document_plan: AtomicPublicationFileResourceV1
    document_unit_snapshot_plan: AtomicPublicationFileResourceV1
    semantic_route_receipts_plan: AtomicPublicationFileResourceV1
    contract_version: str = ATOMIC_PUBLICATION_PREPARATION_V1

    def __post_init__(self) -> None:
        if self.contract_version != ATOMIC_PUBLICATION_PREPARATION_V1:
            raise AtomicPublicationArtifactReadinessError(
                "publication preparation contract is unsupported"
            )
        for value, label in (
            (self.attempt_id, "attempt"),
            (self.fence_identity, "fence"),
            (self.document_id, "document"),
            (self.processing_run_id, "processing run"),
            (self.provider_document_id, "provider document"),
            (self.artifact_owner_processing_run_id, "artifact owner"),
        ):
            _identity(value, label)
        _positive(self.attempt_generation, "attempt generation")
        _sha(self.request_sha256, "publication request")
        _positive(self.request_byte_count, "publication request byte count")
        _sha(self.parser_target_sha256, "parser target")
        _sha(self.provider_envelope_context_sha256, "provider envelope context")
        _sha(self.final_units_sha256, "final Units")
        _sha(self.lineage_sha256, "lineage")
        request_bytes = _canonical_request_bytes(self.canonical_request_json)
        if self.request_byte_count != len(request_bytes):
            raise AtomicPublicationArtifactReadinessError(
                "publication preparation request identity drifted"
            )
        request = decode_atomic_publication_request_v4(request_bytes)
        identity = request.identity
        if (
            (
                self.attempt_id,
                self.attempt_generation,
                self.fence_identity,
                self.document_id,
                self.processing_run_id,
                self.provider_document_id,
                self.request_sha256,
                self.parser_target_sha256,
                self.provider_envelope_context_sha256,
            )
            != (
                identity.attempt_id,
                identity.attempt_generation,
                identity.fence_identity,
                identity.document_id,
                identity.processing_run_id,
                identity.provider_document_id,
                request.request_sha256,
                request.upstream_evidence.parser_target_sha256,
                request.upstream_evidence.provider_envelope_context_sha256,
            )
            or self.artifact_owner_processing_run_id != identity.processing_run_id
        ):
            raise AtomicPublicationArtifactReadinessError(
                "publication preparation identity drifted from request"
            )
        _validate_bindings(request=request, bindings=self.unit_bindings)
        if (
            self.final_units_sha256 != final_unit_bindings_sha256_v4(self.unit_bindings)
            or self.lineage_sha256 != lineage_bindings_sha256_v4(self.unit_bindings)
        ):
            raise AtomicPublicationArtifactReadinessError(
                "publication preparation Unit aggregates do not close"
            )
        if (
            self.parser_output_plan.published_relpath
            != request.upstream_evidence.parser_artifact_root_relpath
            or self.parser_output_plan.inventory_sha256
            != request.upstream_evidence.output_files_sha256
            or self.parser_output_plan.file_count
            != request.upstream_evidence.output_file_count
            or self.parser_output_plan.byte_count
            != request.upstream_evidence.output_total_byte_count
        ):
            raise AtomicPublicationArtifactReadinessError(
                "parser output plan drifted from upstream evidence"
            )
        projection = cast(
            dict[str, Any],
            strict_json_loads(request.processing_run_projection_json.encode("utf-8")),
        )
        plans = (
            self.provider_document_plan,
            self.document_unit_snapshot_plan,
            self.semantic_route_receipts_plan,
        )
        if tuple(item.role for item in plans) != (
            "provider_document",
            "document_unit_snapshot",
            "semantic_route_receipts",
        ):
            raise AtomicPublicationArtifactReadinessError(
                "publication resource plans are not role-closed"
            )
        if (
            self.provider_document_plan.relpath != projection["provider_document_relpath"]
            or self.provider_document_plan.sha256
            != request.upstream_evidence.provider_document_sha256
            or self.document_unit_snapshot_plan.relpath
            != projection["document_units_relpath"]
            or self.document_unit_snapshot_plan.sha256
            != _digest(
                document_unit_snapshot_file_bytes_v1(
                    request=request,
                    bindings=self.unit_bindings,
                )
            )
            or self.semantic_route_receipts_plan.relpath
            != projection["semantic_route_receipts_relpath"]
            or self.semantic_route_receipts_plan.sha256
            != _digest(
                semantic_route_receipts_file_bytes_v3(
                    request.semantic_route_receipts
                )
            )
        ):
            raise AtomicPublicationArtifactReadinessError(
                "publication resource plans drifted from request"
            )

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(_preparation_payload(self), _MAX_PREPARATION_BYTES)

    @property
    def sha256(self) -> str:
        return _digest(self.canonical_bytes)


@dataclass(frozen=True, slots=True)
class AtomicPublicationReadinessReferenceV1:
    manifest_relpath: str
    manifest_sha256: str
    manifest_byte_count: int
    contract_version: str = ATOMIC_PUBLICATION_READINESS_REFERENCE_V1

    def __post_init__(self) -> None:
        if self.contract_version != ATOMIC_PUBLICATION_READINESS_REFERENCE_V1:
            raise AtomicPublicationArtifactReadinessError(
                "readiness reference contract is unsupported"
            )
        _relative(self.manifest_relpath, "readiness manifest")
        if not self.manifest_relpath.endswith(
            "/" + ATOMIC_PUBLICATION_READINESS_FILENAME
        ):
            raise AtomicPublicationArtifactReadinessError(
                "readiness manifest path has the wrong fixed filename"
            )
        _sha(self.manifest_sha256, "readiness manifest")
        _positive(self.manifest_byte_count, "readiness manifest byte count")


@dataclass(frozen=True, slots=True)
class AtomicPublicationReadinessManifestV1:
    attempt_id: str
    attempt_generation: int
    fence_identity: str
    document_id: str
    processing_run_id: str
    provider_document_id: str
    request_sha256: str
    artifact_owner_processing_run_id: str
    parser_target_sha256: str
    provider_envelope_context_sha256: str
    preparation_relpath: str
    preparation_sha256: str
    preparation_byte_count: int
    unit_bindings: tuple[AtomicPublicationUnitBindingV4, ...]
    final_units_sha256: str
    lineage_sha256: str
    parser_output: AtomicPublicationParserOutputPlanV1
    provider_document: AtomicPublicationFileResourceV1
    document_unit_snapshot: AtomicPublicationFileResourceV1
    semantic_route_receipts: AtomicPublicationFileResourceV1
    resources_sha256: str
    contract_version: str = ATOMIC_PUBLICATION_READINESS_V1

    def __post_init__(self) -> None:
        if self.contract_version != ATOMIC_PUBLICATION_READINESS_V1:
            raise AtomicPublicationArtifactReadinessError(
                "publication readiness contract is unsupported"
            )
        for value, label in (
            (self.attempt_id, "attempt"),
            (self.fence_identity, "fence"),
            (self.document_id, "document"),
            (self.processing_run_id, "processing run"),
            (self.provider_document_id, "provider document"),
            (self.artifact_owner_processing_run_id, "artifact owner"),
        ):
            _identity(value, label)
        _positive(self.attempt_generation, "attempt generation")
        for value, label in (
            (self.request_sha256, "publication request"),
            (self.parser_target_sha256, "parser target"),
            (self.provider_envelope_context_sha256, "provider envelope context"),
            (self.preparation_sha256, "publication preparation"),
            (self.final_units_sha256, "final Units"),
            (self.lineage_sha256, "lineage"),
            (self.resources_sha256, "readiness resources"),
        ):
            _sha(value, label)
        _relative(self.preparation_relpath, "publication preparation")
        if not self.preparation_relpath.endswith(
            "/" + ATOMIC_PUBLICATION_PREPARATION_FILENAME
        ):
            raise AtomicPublicationArtifactReadinessError(
                "preparation path has the wrong fixed filename"
            )
        _positive(self.preparation_byte_count, "preparation byte count")
        if (
            self.final_units_sha256 != final_unit_bindings_sha256_v4(self.unit_bindings)
            or self.lineage_sha256 != lineage_bindings_sha256_v4(self.unit_bindings)
            or self.resources_sha256 != readiness_resources_sha256_v1(self)
        ):
            raise AtomicPublicationArtifactReadinessError(
                "publication readiness aggregate does not close"
            )

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(_readiness_payload(self), _MAX_READINESS_BYTES)

    @property
    def sha256(self) -> str:
        return _digest(self.canonical_bytes)


_WITNESS_ISSUER = object()


@dataclass(frozen=True, slots=True, init=False)
class AtomicPublicationArtifactsReadyV4:
    """Opaque, exact-type capability issued only after durable verification."""

    preparation: AtomicPublicationArtifactPreparationV1
    manifest: AtomicPublicationReadinessManifestV1
    reference: AtomicPublicationReadinessReferenceV1
    request: AtomicPublicationRequestV4

    def __init__(
        self,
        *,
        preparation: AtomicPublicationArtifactPreparationV1,
        manifest: AtomicPublicationReadinessManifestV1,
        reference: AtomicPublicationReadinessReferenceV1,
        request: AtomicPublicationRequestV4,
        _issuer: object,
    ) -> None:
        if _issuer is not _WITNESS_ISSUER:
            raise TypeError("publication readiness witness is adapter-issued")
        object.__setattr__(self, "preparation", preparation)
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "reference", reference)
        object.__setattr__(self, "request", request)


def _issue_atomic_publication_artifacts_ready_v4(
    *,
    preparation: AtomicPublicationArtifactPreparationV1,
    manifest: AtomicPublicationReadinessManifestV1,
    reference: AtomicPublicationReadinessReferenceV1,
    request: AtomicPublicationRequestV4,
) -> AtomicPublicationArtifactsReadyV4:
    """Issue the capability for the storage adapter after on-disk verification."""

    validate_preparation_readiness_pair_v1(
        preparation=preparation,
        manifest=manifest,
        reference=reference,
        request=request,
    )
    return AtomicPublicationArtifactsReadyV4(
        preparation=preparation,
        manifest=manifest,
        reference=reference,
        request=request,
        _issuer=_WITNESS_ISSUER,
    )


def validate_preparation_readiness_pair_v1(
    *,
    preparation: AtomicPublicationArtifactPreparationV1,
    manifest: AtomicPublicationReadinessManifestV1,
    reference: AtomicPublicationReadinessReferenceV1,
    request: AtomicPublicationRequestV4,
) -> None:
    if (
        type(preparation) is not AtomicPublicationArtifactPreparationV1
        or type(manifest) is not AtomicPublicationReadinessManifestV1
        or type(reference) is not AtomicPublicationReadinessReferenceV1
        or type(request) is not AtomicPublicationRequestV4
    ):
        raise AtomicPublicationArtifactReadinessError(
            "publication readiness pair requires exact contract types"
        )
    if preparation.canonical_request_json.encode("utf-8") != request.canonical_bytes:
        raise AtomicPublicationArtifactReadinessError(
            "publication readiness request bytes drifted"
        )
    authority_root = PurePosixPath(
        preparation.document_unit_snapshot_plan.relpath
    ).parent
    expected_preparation_relpath = str(
        authority_root / ATOMIC_PUBLICATION_PREPARATION_FILENAME
    )
    expected_readiness_relpath = str(
        authority_root / ATOMIC_PUBLICATION_READINESS_FILENAME
    )
    if (
        manifest.preparation_relpath != expected_preparation_relpath
        or reference.manifest_relpath != expected_readiness_relpath
        or PurePosixPath(preparation.semantic_route_receipts_plan.relpath).parent
        != authority_root
    ):
        raise AtomicPublicationArtifactReadinessError(
            "publication readiness authority paths are not fixed siblings"
        )
    common_preparation = (
        preparation.attempt_id,
        preparation.attempt_generation,
        preparation.fence_identity,
        preparation.document_id,
        preparation.processing_run_id,
        preparation.provider_document_id,
        preparation.request_sha256,
        preparation.artifact_owner_processing_run_id,
        preparation.parser_target_sha256,
        preparation.provider_envelope_context_sha256,
        preparation.unit_bindings,
        preparation.final_units_sha256,
        preparation.lineage_sha256,
        preparation.parser_output_plan,
        preparation.provider_document_plan,
        preparation.document_unit_snapshot_plan,
        preparation.semantic_route_receipts_plan,
    )
    common_manifest = (
        manifest.attempt_id,
        manifest.attempt_generation,
        manifest.fence_identity,
        manifest.document_id,
        manifest.processing_run_id,
        manifest.provider_document_id,
        manifest.request_sha256,
        manifest.artifact_owner_processing_run_id,
        manifest.parser_target_sha256,
        manifest.provider_envelope_context_sha256,
        manifest.unit_bindings,
        manifest.final_units_sha256,
        manifest.lineage_sha256,
        manifest.parser_output,
        manifest.provider_document,
        manifest.document_unit_snapshot,
        manifest.semantic_route_receipts,
    )
    if (
        common_preparation != common_manifest
        or manifest.preparation_sha256 != preparation.sha256
        or manifest.preparation_byte_count != len(preparation.canonical_bytes)
        or reference.manifest_sha256 != manifest.sha256
        or reference.manifest_byte_count != len(manifest.canonical_bytes)
        or request.request_sha256 != preparation.request_sha256
    ):
        raise AtomicPublicationArtifactReadinessError(
            "publication readiness does not bind one exact preparation"
        )


def final_unit_bindings_sha256_v4(
    bindings: tuple[AtomicPublicationUnitBindingV4, ...],
) -> str:
    _binding_tuple(bindings)
    return _digest(
        _canonical_json(
            [
                {
                    "asset_id": item.asset_id,
                    "final_unit_row_sha256": item.final_unit_row_sha256,
                    "routed_draft_sha256": item.routed_draft_sha256,
                    "unit_index": item.unit_index,
                }
                for item in bindings
            ],
            _MAX_READINESS_BYTES,
        )
    )


def lineage_bindings_sha256_v4(
    bindings: tuple[AtomicPublicationUnitBindingV4, ...],
) -> str:
    _binding_tuple(bindings)
    return _digest(
        _canonical_json(
            [
                {
                    "asset_id": item.asset_id,
                    "lineage_row_sha256": item.lineage_row_sha256,
                    "unit_index": item.unit_index,
                }
                for item in bindings
            ],
            _MAX_READINESS_BYTES,
        )
    )


def readiness_resources_sha256_v1(
    manifest: AtomicPublicationReadinessManifestV1,
) -> str:
    return readiness_resource_values_sha256_v1(
        parser_output=manifest.parser_output,
        provider_document=manifest.provider_document,
        document_unit_snapshot=manifest.document_unit_snapshot,
        semantic_route_receipts=manifest.semantic_route_receipts,
    )


def readiness_resource_values_sha256_v1(
    *,
    parser_output: AtomicPublicationParserOutputPlanV1,
    provider_document: AtomicPublicationFileResourceV1,
    document_unit_snapshot: AtomicPublicationFileResourceV1,
    semantic_route_receipts: AtomicPublicationFileResourceV1,
) -> str:
    return _digest(
        _canonical_json(
            {
                "document_unit_snapshot": asdict(document_unit_snapshot),
                "parser_output": asdict(parser_output),
                "provider_document": asdict(provider_document),
                "semantic_route_receipts": asdict(semantic_route_receipts),
            },
            _MAX_READINESS_BYTES,
        )
    )


def document_unit_snapshot_file_bytes_v1(
    *,
    request: AtomicPublicationRequestV4,
    bindings: tuple[AtomicPublicationUnitBindingV4, ...],
) -> bytes:
    """Return the legacy-compatible immutable Unit snapshot JSONL bytes."""

    _validate_bindings(request=request, bindings=bindings)
    rows: list[dict[str, Any]] = []
    for unit, binding in zip(request.units, bindings, strict=True):
        payload = strict_json_loads(unit.canonical_payload_json.encode("utf-8"))
        locator = strict_json_loads(
            unit.canonical_artifact_locator_json.encode("utf-8")
        )
        if not isinstance(payload, dict) or not isinstance(locator, dict):
            raise AtomicPublicationArtifactReadinessError(
                "Unit snapshot payload or locator is not an object"
            )
        rows.append(
            {
                "applicability": unit.applicability,
                "artifact_locator": locator,
                "asset_id": binding.asset_id,
                "content_hash": unit.content_hash,
                "document_id": unit.document_id,
                "heading_path": list(unit.heading_path),
                "order_index": unit.unit_index,
                "page_no": unit.page_no,
                "payload": payload,
                "payload_kind": unit.payload_kind,
                "quality_status": unit.quality_status,
                "section_keys": (
                    None if unit.section_keys is None else list(unit.section_keys)
                ),
                "semantic_key": (
                    None if not unit.semantic_keys else unit.semantic_keys[0]
                ),
                "semantic_keys": (
                    None if unit.semantic_keys is None else list(unit.semantic_keys)
                ),
                "title": unit.title,
            }
        )
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        for row in rows
    ).encode("utf-8")


def decode_atomic_publication_preparation_v1(
    exact_bytes: bytes,
) -> AtomicPublicationArtifactPreparationV1:
    root = _closed_object(
        exact_bytes,
        AtomicPublicationArtifactPreparationV1,
        _MAX_PREPARATION_BYTES,
    )
    bindings = _decode_bindings(root["unit_bindings"])
    parser = _decode_nested(root["parser_output_plan"], AtomicPublicationParserOutputPlanV1)
    provider = _decode_nested(root["provider_document_plan"], AtomicPublicationFileResourceV1)
    snapshot = _decode_nested(root["document_unit_snapshot_plan"], AtomicPublicationFileResourceV1)
    semantic = _decode_nested(root["semantic_route_receipts_plan"], AtomicPublicationFileResourceV1)
    value = AtomicPublicationArtifactPreparationV1(
        **{
            **root,
            "unit_bindings": bindings,
            "parser_output_plan": parser,
            "provider_document_plan": provider,
            "document_unit_snapshot_plan": snapshot,
            "semantic_route_receipts_plan": semantic,
        }
    )
    if value.canonical_bytes != exact_bytes:
        raise AtomicPublicationArtifactReadinessError(
            "publication preparation JSON is not canonical"
        )
    return value


def decode_atomic_publication_readiness_v1(
    exact_bytes: bytes,
) -> AtomicPublicationReadinessManifestV1:
    root = _closed_object(
        exact_bytes,
        AtomicPublicationReadinessManifestV1,
        _MAX_READINESS_BYTES,
    )
    bindings = _decode_bindings(root["unit_bindings"])
    parser = _decode_nested(root["parser_output"], AtomicPublicationParserOutputPlanV1)
    provider = _decode_nested(root["provider_document"], AtomicPublicationFileResourceV1)
    snapshot = _decode_nested(root["document_unit_snapshot"], AtomicPublicationFileResourceV1)
    semantic = _decode_nested(root["semantic_route_receipts"], AtomicPublicationFileResourceV1)
    value = AtomicPublicationReadinessManifestV1(
        **{
            **root,
            "unit_bindings": bindings,
            "parser_output": parser,
            "provider_document": provider,
            "document_unit_snapshot": snapshot,
            "semantic_route_receipts": semantic,
        }
    )
    if value.canonical_bytes != exact_bytes:
        raise AtomicPublicationArtifactReadinessError(
            "publication readiness JSON is not canonical"
        )
    return value


def _validate_bindings(
    *,
    request: AtomicPublicationRequestV4,
    bindings: tuple[AtomicPublicationUnitBindingV4, ...],
) -> None:
    _binding_tuple(bindings)
    if len(bindings) != len(request.units):
        raise AtomicPublicationArtifactReadinessError(
            "Unit bindings do not close publication request"
        )
    if any(
        binding.routed_draft_sha256 != unit.routed_draft_sha256
        for binding, unit in zip(bindings, request.units, strict=True)
    ):
        raise AtomicPublicationArtifactReadinessError(
            "Unit binding routed drafts drifted from request"
        )


def _binding_tuple(bindings: tuple[AtomicPublicationUnitBindingV4, ...]) -> None:
    if (
        not isinstance(bindings, tuple)
        or not bindings
        or any(type(item) is not AtomicPublicationUnitBindingV4 for item in bindings)
        or tuple(item.unit_index for item in bindings)
        != tuple(range(1, len(bindings) + 1))
        or len({item.asset_id for item in bindings}) != len(bindings)
    ):
        raise AtomicPublicationArtifactReadinessError(
            "Unit bindings are not exact, ordered, and unique"
        )


def _canonical_request_bytes(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise AtomicPublicationArtifactReadinessError(
            "canonical publication request is empty"
        )
    exact = value.encode("utf-8")
    if len(exact) > 8 * 1024 * 1024:
        raise AtomicPublicationArtifactReadinessError(
            "canonical publication request exceeds its envelope"
        )
    request = decode_atomic_publication_request_v4(exact)
    if request.canonical_bytes != exact:
        raise AtomicPublicationArtifactReadinessError(
            "canonical publication request does not round-trip exactly"
        )
    return exact


def _preparation_payload(
    value: AtomicPublicationArtifactPreparationV1,
) -> dict[str, Any]:
    payload = asdict(value)
    payload["unit_bindings"] = [asdict(item) for item in value.unit_bindings]
    return payload


def _readiness_payload(
    value: AtomicPublicationReadinessManifestV1,
) -> dict[str, Any]:
    payload = asdict(value)
    payload["unit_bindings"] = [asdict(item) for item in value.unit_bindings]
    return payload


def _closed_object(
    exact_bytes: bytes,
    item_type: type[Any],
    limit: int,
) -> dict[str, Any]:
    if type(exact_bytes) is not bytes or not 1 <= len(exact_bytes) <= limit:
        raise AtomicPublicationArtifactReadinessError(
            "publication artifact bytes are outside the envelope"
        )
    value = strict_json_loads(exact_bytes)
    if not isinstance(value, dict):
        raise AtomicPublicationArtifactReadinessError(
            "publication artifact must be an object"
        )
    root = cast(dict[str, Any], value)
    if set(root) != {item.name for item in fields(item_type)}:
        raise AtomicPublicationArtifactReadinessError(
            f"{item_type.__name__} fields are not closed"
        )
    return root


def _decode_nested(value: object, item_type: type[Any]) -> Any:
    if not isinstance(value, dict) or set(value) != {
        item.name for item in fields(item_type)
    }:
        raise AtomicPublicationArtifactReadinessError(
            f"{item_type.__name__} fields are not closed"
        )
    return item_type(**value)


def _decode_bindings(value: object) -> tuple[AtomicPublicationUnitBindingV4, ...]:
    if not isinstance(value, list):
        raise AtomicPublicationArtifactReadinessError(
            "publication Unit bindings must be an array"
        )
    return tuple(
        _decode_nested(item, AtomicPublicationUnitBindingV4) for item in value
    )


def _canonical_json(value: object, limit: int) -> bytes:
    try:
        exact = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AtomicPublicationArtifactReadinessError(
            "publication artifact JSON is invalid"
        ) from exc
    if not 1 <= len(exact) <= limit:
        raise AtomicPublicationArtifactReadinessError(
            "publication artifact JSON is outside its envelope"
        )
    return exact


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _relative(value: str, label: str) -> None:
    try:
        validate_relative_resource_path_v4(value, label)
    except ValueError as exc:
        raise AtomicPublicationArtifactReadinessError(str(exc)) from exc


def _sha(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise AtomicPublicationArtifactReadinessError(
            f"{label} hash is not canonical"
        )


def _identity(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > _MAX_IDENTITY_BYTES
        or any(ord(character) < 0x20 for character in value)
    ):
        raise AtomicPublicationArtifactReadinessError(
            f"{label} identity is invalid"
        )


def _positive(value: int, label: str) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_COUNT:
        raise AtomicPublicationArtifactReadinessError(f"{label} is not positive")


__all__ = [
    "ATOMIC_PUBLICATION_PREPARATION_FILENAME",
    "ATOMIC_PUBLICATION_PREPARATION_V1",
    "ATOMIC_PUBLICATION_READINESS_FILENAME",
    "ATOMIC_PUBLICATION_READINESS_REFERENCE_V1",
    "ATOMIC_PUBLICATION_READINESS_V1",
    "AtomicPublicationArtifactConflict",
    "AtomicPublicationArtifactPreparationV1",
    "AtomicPublicationArtifactReadinessError",
    "AtomicPublicationArtifactsReadyV4",
    "AtomicPublicationFileResourceV1",
    "AtomicPublicationParserOutputPlanV1",
    "AtomicPublicationReadinessManifestV1",
    "AtomicPublicationReadinessReferenceV1",
    "AtomicPublicationUnitBindingV4",
    "decode_atomic_publication_preparation_v1",
    "decode_atomic_publication_readiness_v1",
    "document_unit_snapshot_file_bytes_v1",
    "final_unit_bindings_sha256_v4",
    "lineage_bindings_sha256_v4",
    "readiness_resources_sha256_v1",
    "readiness_resource_values_sha256_v1",
    "validate_preparation_readiness_pair_v1",
]
