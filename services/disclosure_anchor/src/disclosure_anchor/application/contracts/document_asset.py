"""Internal PDF source-object ledger contract.

``DocumentAsset`` is not a fifth v0.8 ``data_asset.asset_kind``.  It records
embedded files, associated files, visible file-attachment annotations, and
page media before any attached content is parsed into a legal L1 envelope.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import math
import re
from typing import Any, TypeAlias


DOCUMENT_ASSET_CONTRACT_VERSION = "document-source-asset.v1"
DOCUMENT_ASSET_AUTHORITY = "disclosure_anchor_internal_pdf_object_ledger"
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_ID_RE = re.compile(r"^dsa_[a-f0-9]{32}$")
_PDF_OBJECT_REF_RE = re.compile(r"^pdf-object:[0-9]+:[0-9]+(?:/[A-Za-z0-9_.-]+)*$")
_PDF_ANNOTATION_REF_RE = re.compile(r"^pdf-annotation:[0-9]+:[0-9]+$")
_PAGE_MEDIA_REF_RE = re.compile(
    r"^(?:pdf-object:[0-9]+:[0-9]+(?:/[A-Za-z0-9_.-]+)*|mineru-image:[0-9]+)$"
)

BBox: TypeAlias = tuple[float, float, float, float]


class DocumentAssetDomain(StrEnum):
    EMBEDDED_FILE = "embedded_file"
    FILE_ATTACHMENT = "file_attachment"
    ASSOCIATED_FILE = "associated_file"
    PAGE_MEDIA = "page_media"


class BodyOccurrencePolicy(StrEnum):
    NONE = "none"
    VISIBLE_ASSET_REFERENCE = "visible_asset_reference"
    PAGE_MEDIA = "page_media"


class AssetExtractionStatus(StrEnum):
    INVENTORIED = "inventoried"
    CHILD_PARSE_PENDING = "child_parse_pending"
    CHILD_PARSE_COMPLETE = "child_parse_complete"
    UNSUPPORTED = "unsupported"


class DocumentAssetContractError(ValueError):
    """An internal PDF object is ambiguous or claims an illegal body asset."""


@dataclass(frozen=True, slots=True)
class DocumentAsset:
    document_asset_id: str
    document_id: str
    processing_run_id: str
    source_pdf_sha256: str
    domain: DocumentAssetDomain
    source_object_ref: str
    blob_sha256: str
    size_bytes: int
    mime_type: str | None
    filename: str | None
    relationship: str | None
    page_annotation_ref: str | None
    page_index: int | None
    bbox: BBox | None
    body_occurrence_policy: BodyOccurrencePolicy
    extraction_status: AssetExtractionStatus
    child_processing_run_id: str | None = None
    contract_version: str = DOCUMENT_ASSET_CONTRACT_VERSION
    authority: str = DOCUMENT_ASSET_AUTHORITY
    data_asset_kind: None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.domain, DocumentAssetDomain)
            or not isinstance(self.body_occurrence_policy, BodyOccurrencePolicy)
            or not isinstance(self.extraction_status, AssetExtractionStatus)
            or _ID_RE.fullmatch(self.document_asset_id) is None
            or not self.document_id
            or not self.processing_run_id
            or _SHA256_RE.fullmatch(self.source_pdf_sha256) is None
            or not self.source_object_ref
            or _SHA256_RE.fullmatch(self.blob_sha256) is None
            or not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or self.size_bytes < 0
            or self.mime_type is not None
            and not self.mime_type
            or self.filename is not None
            and not self.filename
            or self.relationship is not None
            and not self.relationship
            or self.page_index is not None
            and (
                not isinstance(self.page_index, int)
                or isinstance(self.page_index, bool)
                or self.page_index < 0
            )
            or self.bbox is not None
            and not _bbox(self.bbox)
            or self.child_processing_run_id is not None
            and not self.child_processing_run_id
            or self.contract_version != DOCUMENT_ASSET_CONTRACT_VERSION
            or self.authority != DOCUMENT_ASSET_AUTHORITY
            or self.data_asset_kind is not None
        ):
            raise DocumentAssetContractError("document asset identity is invalid")
        expected_id = document_asset_id(
            document_id=self.document_id,
            processing_run_id=self.processing_run_id,
            domain=self.domain,
            source_object_ref=self.source_object_ref,
        )
        if self.document_asset_id != expected_id:
            raise DocumentAssetContractError("document asset stable id differs")
        if self.domain in {
            DocumentAssetDomain.EMBEDDED_FILE,
            DocumentAssetDomain.ASSOCIATED_FILE,
        }:
            if (
                _PDF_OBJECT_REF_RE.fullmatch(self.source_object_ref) is None
                or self.page_annotation_ref is not None
                or self.page_index is not None
                or self.bbox is not None
                or self.body_occurrence_policy is not BodyOccurrencePolicy.NONE
            ):
                raise DocumentAssetContractError(
                    "non-page PDF asset cannot create a body occurrence"
                )
        elif self.domain is DocumentAssetDomain.FILE_ATTACHMENT:
            if (
                _PDF_OBJECT_REF_RE.fullmatch(self.source_object_ref) is None
                or self.page_annotation_ref is None
                or _PDF_ANNOTATION_REF_RE.fullmatch(self.page_annotation_ref) is None
                or self.page_index is None
                or self.bbox is None
                or self.body_occurrence_policy
                is not BodyOccurrencePolicy.VISIBLE_ASSET_REFERENCE
            ):
                raise DocumentAssetContractError(
                    "file attachment requires a visible annotation occurrence"
                )
        elif self.domain is DocumentAssetDomain.PAGE_MEDIA:
            if (
                _PAGE_MEDIA_REF_RE.fullmatch(self.source_object_ref) is None
                or self.page_annotation_ref is not None
                or self.page_index is None
                or self.bbox is None
                or self.body_occurrence_policy is not BodyOccurrencePolicy.PAGE_MEDIA
            ):
                raise DocumentAssetContractError(
                    "page media requires page-local geometry, not attachment semantics"
                )
        if (self.extraction_status is AssetExtractionStatus.CHILD_PARSE_COMPLETE) != (
            self.child_processing_run_id is not None
        ):
            raise DocumentAssetContractError(
                "child parse completion and run identity are inconsistent"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_asset_id": self.document_asset_id,
            "document_id": self.document_id,
            "processing_run_id": self.processing_run_id,
            "source_pdf_sha256": self.source_pdf_sha256,
            "domain": self.domain.value,
            "source_object_ref": self.source_object_ref,
            "blob_sha256": self.blob_sha256,
            "size_bytes": self.size_bytes,
            "mime_type": self.mime_type,
            "filename": self.filename,
            "relationship": self.relationship,
            "page_annotation_ref": self.page_annotation_ref,
            "page_index": self.page_index,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "body_occurrence_policy": self.body_occurrence_policy.value,
            "extraction_status": self.extraction_status.value,
            "child_processing_run_id": self.child_processing_run_id,
            "contract_version": self.contract_version,
            "authority": self.authority,
            "data_asset_kind": self.data_asset_kind,
        }


def document_asset_id(
    *,
    document_id: str,
    processing_run_id: str,
    domain: DocumentAssetDomain,
    source_object_ref: str,
) -> str:
    """Derive source-occurrence identity without deduplicating equal blobs."""

    if (
        not document_id
        or not processing_run_id
        or not isinstance(domain, DocumentAssetDomain)
        or not source_object_ref
    ):
        raise DocumentAssetContractError("document asset id inputs are empty")
    payload = json.dumps(
        {
            "version": DOCUMENT_ASSET_CONTRACT_VERSION,
            "document_id": document_id,
            "processing_run_id": processing_run_id,
            "domain": domain.value,
            "source_object_ref": source_object_ref,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "dsa_" + hashlib.sha256(payload).hexdigest()[:32]


def _bbox(value: object) -> bool:
    return bool(
        isinstance(value, tuple)
        and len(value) == 4
        and all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in value
        )
        and value[0] < value[2]
        and value[1] < value[3]
    )


__all__ = [
    "AssetExtractionStatus",
    "BBox",
    "BodyOccurrencePolicy",
    "DOCUMENT_ASSET_AUTHORITY",
    "DOCUMENT_ASSET_CONTRACT_VERSION",
    "DocumentAsset",
    "DocumentAssetContractError",
    "DocumentAssetDomain",
    "document_asset_id",
]
