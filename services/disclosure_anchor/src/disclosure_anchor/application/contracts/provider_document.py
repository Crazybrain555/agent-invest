"""Small provider-native records for the greenfield document-unit path."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import PurePosixPath
import re
from typing import Literal
import unicodedata


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARTIFACT_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_MEDIA_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")


@dataclass(frozen=True, slots=True)
class ProviderBBox:
    """One provider bbox in MinerU's normalized 0..1000 coordinate space."""

    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        values = (self.x0, self.y0, self.x1, self.y1)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("provider bbox coordinates must be finite")
        if not all(0.0 <= value <= 1000.0 for value in values):
            raise ValueError("provider bbox coordinates must be within 0..1000")
        if self.x0 >= self.x1 or self.y0 >= self.y1:
            raise ValueError("provider bbox must have positive width and height")

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass(frozen=True, slots=True)
class ProviderArtifact:
    """Hash-bound provider artifact under one parser output root."""

    role: str
    relative_path: str
    sha256: str
    size_bytes: int
    media_type: str

    def __post_init__(self) -> None:
        if not _ARTIFACT_ROLE_RE.fullmatch(self.role):
            raise ValueError("provider artifact role must be opaque and identifier-safe")
        if (
            not self.relative_path
            or self.relative_path.startswith("/")
            or "\\" in self.relative_path
            or any(
                unicodedata.category(char).startswith("C")
                for char in self.relative_path
            )
        ):
            raise ValueError("provider artifact path must be relative")
        path = PurePosixPath(self.relative_path)
        if path.as_posix() != self.relative_path or any(
            part in {"", ".", ".."} for part in path.parts
        ):
            raise ValueError("provider artifact path cannot escape its root")
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("provider artifact sha256 must be canonical")
        if self.size_bytes < 0:
            raise ValueError("provider artifact size cannot be negative")
        if not _MEDIA_TYPE_RE.fullmatch(self.media_type):
            raise ValueError("provider artifact media type must be canonical")


@dataclass(frozen=True, slots=True)
class ProviderPayload:
    """One source-order string field emitted by the provider item."""

    field: str
    item_index: int | None
    text: str

    def __post_init__(self) -> None:
        if not self.field:
            raise ValueError("provider payload field must be non-empty")
        if self.item_index is not None and self.item_index < 0:
            raise ValueError("provider payload item index cannot be negative")


@dataclass(frozen=True, slots=True)
class ProviderBlock:
    """One flat content-list item with no inferred document semantics."""

    source_index: int
    page_index: int
    order_in_page: int
    provider_type: str
    typed_annotation: str | None
    provider_level: int | None
    bbox: ProviderBBox | None
    payloads: tuple[ProviderPayload, ...]
    referenced_artifact_roles: tuple[str, ...]
    raw_item_json: str
    raw_item_sha256: str

    def __post_init__(self) -> None:
        if self.source_index < 0 or self.page_index < 0 or self.order_in_page < 0:
            raise ValueError("provider block indices cannot be negative")
        if not self.provider_type:
            raise ValueError("provider block type must be non-empty")
        if self.typed_annotation == "":
            raise ValueError("typed annotation must be non-empty when present")
        if self.provider_level is not None and self.provider_level < 0:
            raise ValueError("provider level cannot be negative")
        if not _SHA256_RE.fullmatch(self.raw_item_sha256):
            raise ValueError("provider item sha256 must be canonical")


PhysicalTableLogicalStatus = Literal["retained", "deleted", "unbound"]


@dataclass(frozen=True, slots=True)
class ProviderPhysicalTableSegment:
    """One page-local table segment retained from MinerU middle_json."""

    page_index: int
    order_in_page: int
    provider_index: int
    bbox: ProviderBBox | None
    page_local_html: str
    crop_artifact_role: str | None
    logical_stream_status: PhysicalTableLogicalStatus
    cell_merge_json: str | None
    raw_segment_json: str
    raw_segment_sha256: str

    def __post_init__(self) -> None:
        if self.page_index < 0 or self.order_in_page < 0 or self.provider_index < 0:
            raise ValueError("provider table segment indices cannot be negative")
        if self.logical_stream_status not in {"retained", "deleted", "unbound"}:
            raise ValueError("provider table segment status is unsupported")
        if self.crop_artifact_role == "":
            raise ValueError("provider table crop role must be non-empty when present")
        if self.cell_merge_json == "":
            raise ValueError("cell-merge JSON must be non-empty when present")
        if not _SHA256_RE.fullmatch(self.raw_segment_sha256):
            raise ValueError("provider table segment sha256 must be canonical")


@dataclass(frozen=True, slots=True)
class ProviderPage:
    """One physical PDF page and its provider-order blocks."""

    page_index: int
    page_size: tuple[float, float]
    blocks: tuple[ProviderBlock, ...]

    def __post_init__(self) -> None:
        if self.page_index < 0:
            raise ValueError("provider page index cannot be negative")
        if len(self.page_size) != 2 or not all(
            math.isfinite(value) and value > 0 for value in self.page_size
        ):
            raise ValueError("provider page size must contain two positive values")
        for order, block in enumerate(self.blocks):
            if block.page_index != self.page_index:
                raise ValueError("provider block belongs to a different page")
            if block.order_in_page != order:
                raise ValueError("provider page block order must be contiguous")


@dataclass(frozen=True, slots=True)
class ProviderDocument:
    """DB-free diagnostic projection of one exact provider artifact bundle."""

    source_pdf_sha256: str
    parser_version: str
    backend: str
    effort: str
    ocr_enabled: bool
    pages: tuple[ProviderPage, ...]
    physical_table_segments: tuple[ProviderPhysicalTableSegment, ...]
    artifacts: tuple[ProviderArtifact, ...]
    bundle_sha256: str

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.source_pdf_sha256):
            raise ValueError("source PDF sha256 must be canonical")
        if not _SHA256_RE.fullmatch(self.bundle_sha256):
            raise ValueError("provider bundle sha256 must be canonical")
        if not self.parser_version or not self.backend or not self.effort:
            raise ValueError("provider parser identity must be complete")
        if tuple(page.page_index for page in self.pages) != tuple(
            range(len(self.pages))
        ):
            raise ValueError("provider pages must be contiguous and zero-based")
        block_indices = [block.source_index for block in self.blocks]
        if sorted(block_indices) != list(range(len(block_indices))):
            raise ValueError("provider block source indices must be contiguous")
        roles = [artifact.role for artifact in self.artifacts]
        if len(roles) != len(set(roles)):
            raise ValueError("provider artifact roles must be unique")
        relative_paths = [artifact.relative_path for artifact in self.artifacts]
        if len(relative_paths) != len(set(relative_paths)):
            raise ValueError("provider artifact paths must be unique")
        if relative_paths != sorted(relative_paths):
            raise ValueError("provider artifacts must preserve canonical path order")
        role_set = set(roles)
        if self.bundle_sha256 != provider_artifact_bundle_sha256(self.artifacts):
            raise ValueError(
                "provider bundle sha256 does not match its artifact inventory"
            )
        for block in self.blocks:
            if not set(block.referenced_artifact_roles).issubset(role_set):
                raise ValueError("provider block artifact role is not hash-bound")
        expected_segment_order: dict[int, int] = {}
        previous_segment_page = -1
        for segment in self.physical_table_segments:
            if segment.page_index >= len(self.pages):
                raise ValueError("provider table segment page is out of range")
            if segment.page_index < previous_segment_page:
                raise ValueError("provider table segments must preserve page order")
            expected_order = expected_segment_order.get(segment.page_index, 0)
            if segment.order_in_page != expected_order:
                raise ValueError(
                    "provider table segment order must be contiguous within each page"
                )
            expected_segment_order[segment.page_index] = expected_order + 1
            previous_segment_page = segment.page_index
            if (
                segment.crop_artifact_role is not None
                and segment.crop_artifact_role not in role_set
            ):
                raise ValueError("provider table segment crop role is not hash-bound")

    @property
    def blocks(self) -> tuple[ProviderBlock, ...]:
        return tuple(
            sorted(
                (block for page in self.pages for block in page.blocks),
                key=lambda block: block.source_index,
            )
        )


def provider_artifact_bundle_sha256(
    artifacts: tuple[ProviderArtifact, ...],
) -> str:
    """Hash provider file identity in path order.

    Media type is a validated envelope descriptor, but not part of this stable
    byte-inventory identity. The envelope's own artifact hash binds it.
    """

    payload = json.dumps(
        [
            {
                "relative_path": artifact.relative_path,
                "role": artifact.role,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
            }
            for artifact in artifacts
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


__all__ = [
    "PhysicalTableLogicalStatus",
    "ProviderArtifact",
    "ProviderBBox",
    "ProviderBlock",
    "ProviderDocument",
    "ProviderPage",
    "ProviderPayload",
    "ProviderPhysicalTableSegment",
    "provider_artifact_bundle_sha256",
]
