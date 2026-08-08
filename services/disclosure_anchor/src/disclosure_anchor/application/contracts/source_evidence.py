"""Immutable parser-neutral facts proved from source artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
from typing import Literal, TypeAlias, cast


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
BBox: TypeAlias = tuple[float, float, float, float]
RawBBox: TypeAlias = tuple[float, float, float, float] | None
LayoutPath: TypeAlias = tuple[int, int, int, int]


class SourceEvidenceProofError(ValueError):
    """A typed source proof is incomplete or internally contradictory."""


@dataclass(frozen=True, slots=True)
class SourceProofIdentity:
    source_evidence_sha256: str
    source_pdf_sha256: str
    page_count: int

    def __post_init__(self) -> None:
        if (
            _SHA256.fullmatch(self.source_evidence_sha256) is None
            or _SHA256.fullmatch(self.source_pdf_sha256) is None
            or not _index(self.page_count)
            or self.page_count < 1
        ):
            raise SourceEvidenceProofError("source proof identity is invalid")


@dataclass(frozen=True, slots=True)
class VisualArtifactProof:
    artifact_role: str
    sha256: str
    size_bytes: int
    pixel_width: int
    pixel_height: int
    media_type: Literal["image/png"]

    def __post_init__(self) -> None:
        if (
            not self.artifact_role
            or _SHA256.fullmatch(self.sha256) is None
            or not _positive_int(self.size_bytes)
            or not _positive_int(self.pixel_width)
            or not _positive_int(self.pixel_height)
            or self.media_type != "image/png"
        ):
            raise SourceEvidenceProofError(
                "source visual artifact descriptor is invalid"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_role": self.artifact_role,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "pixel_width": self.pixel_width,
            "pixel_height": self.pixel_height,
            "media_type": self.media_type,
        }


@dataclass(frozen=True, slots=True)
class MappedSourceEvent:
    atom_index: int
    word_order: int
    source_item_index: int
    order_state: Literal["monotonic", "conflict"]
    selector_field: str
    selector_index: int | None
    selector_char_span: tuple[int, int]
    selector_value_sha256: str
    carrier_order: int
    carrier_bbox: BBox
    atom_bbox: BBox
    native_layout_path: LayoutPath

    def __post_init__(self) -> None:
        if (
            not _index(self.atom_index)
            or not _index(self.word_order)
            or not _index(self.source_item_index)
            or self.order_state not in {"monotonic", "conflict"}
            or not self.selector_field
            or (self.selector_index is not None and not _index(self.selector_index))
            or not _span(self.selector_char_span)
            or not _digest(self.selector_value_sha256)
            or not _index(self.carrier_order)
            or not _bbox(self.carrier_bbox)
            or not _bbox(self.atom_bbox)
            or not _layout_path(self.native_layout_path)
        ):
            raise SourceEvidenceProofError("mapped source event is invalid")


@dataclass(frozen=True, slots=True)
class NativeTextEvent:
    atom_index: int
    word_order: int
    text: str
    text_sha256: str
    bbox: BBox
    char_span: tuple[int, int]
    layout_path: LayoutPath

    def __post_init__(self) -> None:
        if (
            not _index(self.atom_index)
            or not _index(self.word_order)
            or not self.text
            or self.text_sha256 != _text_sha256(self.text)
            or not _bbox(self.bbox)
            or not _span(self.char_span)
            or not _layout_path(self.layout_path)
        ):
            raise SourceEvidenceProofError("native text event is invalid")


@dataclass(frozen=True, slots=True)
class GeometryIssueEvent:
    word_order: int
    text: str
    text_sha256: str
    raw_bbox: RawBBox
    reason: str
    visual_artifact: VisualArtifactProof

    def __post_init__(self) -> None:
        if (
            not _index(self.word_order)
            or not self.text
            or self.text_sha256 != _text_sha256(self.text)
            or (self.raw_bbox is not None and not _raw_bbox(self.raw_bbox))
            or not self.reason
        ):
            raise SourceEvidenceProofError("native geometry issue event is invalid")


SourcePageEvent: TypeAlias = MappedSourceEvent | NativeTextEvent | GeometryIssueEvent


@dataclass(frozen=True, slots=True)
class VisualPageFallback:
    visual_artifact: VisualArtifactProof
    semantic_text: str | None = None
    semantic_text_sha256: str | None = None

    def __post_init__(self) -> None:
        if (self.semantic_text is None) != (self.semantic_text_sha256 is None):
            raise SourceEvidenceProofError(
                "visual page semantic text identity is incomplete"
            )
        if self.semantic_text is not None and (
            not self.semantic_text.strip()
            or self.semantic_text_sha256 != _text_sha256(self.semantic_text)
        ):
            raise SourceEvidenceProofError(
                "visual page semantic text identity is invalid"
            )


@dataclass(frozen=True, slots=True)
class SourcePageProof:
    page_idx: int
    events: tuple[SourcePageEvent, ...]
    visual_only: VisualPageFallback | None = None
    width: float | None = None
    height: float | None = None

    def __post_init__(self) -> None:
        if (
            not _index(self.page_idx)
            or (self.width is None) != (self.height is None)
            or (
                self.width is not None
                and (
                    not math.isfinite(self.width)
                    or not math.isfinite(cast(float, self.height))
                    or self.width <= 0
                    or cast(float, self.height) <= 0
                )
            )
        ):
            raise SourceEvidenceProofError("source proof page index is invalid")
        if self.visual_only is not None and self.events:
            raise SourceEvidenceProofError(
                "visual-only source page cannot contain native events"
            )
        if tuple(event.word_order for event in self.events) != tuple(
            range(len(self.events))
        ):
            raise SourceEvidenceProofError("source page event order is not closed")


@dataclass(frozen=True, slots=True)
class RetrievalRunProof:
    page_idx: int
    run_index: int
    atom_indices: tuple[int, ...]
    text_sha256: str
    boundary_basis: Literal[
        "native_complete_cell",
        "native_table_guard",
        "provider_table_guard",
        "source_layout",
    ] = "source_layout"

    def __post_init__(self) -> None:
        if (
            not _index(self.page_idx)
            or not _index(self.run_index)
            or not self.atom_indices
            or any(not _index(value) for value in self.atom_indices)
            or len(set(self.atom_indices)) != len(self.atom_indices)
            or _SHA256.fullmatch(self.text_sha256) is None
            or self.boundary_basis
            not in {
                "native_complete_cell",
                "native_table_guard",
                "provider_table_guard",
                "source_layout",
            }
        ):
            raise SourceEvidenceProofError("source retrieval run is invalid")


@dataclass(frozen=True, slots=True)
class VisualBindingProof:
    source_item_index: int
    page_idx: int
    kind: Literal["carrier_guard", "occurrence_crop"]
    artifact: VisualArtifactProof

    def __post_init__(self) -> None:
        if (
            not _index(self.source_item_index)
            or not _index(self.page_idx)
            or self.kind not in {"carrier_guard", "occurrence_crop"}
        ):
            raise SourceEvidenceProofError("source visual binding is invalid")


@dataclass(frozen=True, slots=True)
class VerifiedVisualArtifact:
    artifact_role: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.artifact_role or _SHA256.fullmatch(self.sha256) is None:
            raise SourceEvidenceProofError("verified source visual identity is invalid")


@dataclass(frozen=True, slots=True)
class SourceEvidenceProof:
    identity: SourceProofIdentity
    pages: tuple[SourcePageProof, ...]
    retrieval_runs: tuple[RetrievalRunProof, ...]
    visual_bindings: tuple[VisualBindingProof, ...]
    verified_visuals: tuple[VerifiedVisualArtifact, ...]

    def __post_init__(self) -> None:
        if tuple(page.page_idx for page in self.pages) != tuple(
            range(self.identity.page_count)
        ):
            raise SourceEvidenceProofError(
                "source proof pages do not close the PDF page range"
            )

        native_events: dict[int, tuple[int, int, NativeTextEvent]] = {}
        next_atom_index = 0
        for page in self.pages:
            for event_position, event in enumerate(page.events):
                if isinstance(event, GeometryIssueEvent):
                    continue
                if event.atom_index != next_atom_index:
                    raise SourceEvidenceProofError(
                        "source proof atom index is not globally page-major"
                    )
                if isinstance(event, NativeTextEvent):
                    native_events[event.atom_index] = (
                        page.page_idx,
                        event_position,
                        event,
                    )
                next_atom_index += 1

        run_members: set[int] = set()
        run_indices: dict[int, list[int]] = {}
        for run in self.retrieval_runs:
            if run.page_idx >= self.identity.page_count:
                raise SourceEvidenceProofError(
                    "source retrieval run page is outside the PDF"
                )
            run_indices.setdefault(run.page_idx, []).append(run.run_index)
            resolved = [native_events.get(index) for index in run.atom_indices]
            if any(item is None for item in resolved) or run_members.intersection(
                run.atom_indices
            ):
                raise SourceEvidenceProofError(
                    "source retrieval run membership is not exact"
                )
            members = [item for item in resolved if item is not None]
            positions = [item[1] for item in members]
            if (
                any(item[0] != run.page_idx for item in members)
                or positions != list(range(positions[0], positions[0] + len(positions)))
                or run.text_sha256
                != _text_sha256("".join(item[2].text for item in members))
            ):
                raise SourceEvidenceProofError(
                    "source retrieval run crosses a physical event boundary"
                )
            run_members.update(run.atom_indices)
        if run_members != set(native_events):
            raise SourceEvidenceProofError(
                "source retrieval runs do not cover every native text event"
            )
        if any(values != list(range(len(values))) for values in run_indices.values()):
            raise SourceEvidenceProofError(
                "source retrieval run indices are not page-local and closed"
            )

        bindings = {
            (
                binding.source_item_index,
                binding.page_idx,
                binding.kind,
                binding.artifact.artifact_role,
            )
            for binding in self.visual_bindings
        }
        if len(bindings) != len(self.visual_bindings) or any(
            binding.page_idx >= self.identity.page_count
            for binding in self.visual_bindings
        ):
            raise SourceEvidenceProofError(
                "source visual binding is duplicated or outside the PDF"
            )

        descriptors: dict[str, VisualArtifactProof] = {}
        for artifact in self._visual_artifacts():
            prior = descriptors.setdefault(artifact.artifact_role, artifact)
            if prior != artifact:
                raise SourceEvidenceProofError(
                    "source visual role has conflicting descriptors"
                )
        verified = {item.artifact_role: item.sha256 for item in self.verified_visuals}
        if len(verified) != len(self.verified_visuals) or verified != {
            role: descriptor.sha256 for role, descriptor in descriptors.items()
        }:
            raise SourceEvidenceProofError(
                "verified visual bytes do not close source visual descriptors"
            )

    def _visual_artifacts(self) -> tuple[VisualArtifactProof, ...]:
        artifacts: list[VisualArtifactProof] = []
        for page in self.pages:
            if page.visual_only is not None:
                artifacts.append(page.visual_only.visual_artifact)
            artifacts.extend(
                event.visual_artifact
                for event in page.events
                if isinstance(event, GeometryIssueEvent)
            )
        artifacts.extend(binding.artifact for binding in self.visual_bindings)
        return tuple(artifacts)


def _text_sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _index(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _positive_int(value: object) -> bool:
    return _index(value) and isinstance(value, int) and value > 0


def _raw_bbox(value: object) -> bool:
    return bool(
        isinstance(value, tuple)
        and len(value) == 4
        and all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in value
        )
    )


def _bbox(value: object) -> bool:
    return bool(
        _raw_bbox(value)
        and isinstance(value, tuple)
        and value[0] < value[2]
        and value[1] < value[3]
    )


def _span(value: object) -> bool:
    return bool(
        isinstance(value, tuple)
        and len(value) == 2
        and _index(value[0])
        and _index(value[1])
        and value[0] < value[1]
    )


def _layout_path(value: object) -> bool:
    return bool(
        isinstance(value, tuple)
        and len(value) == 4
        and all(_index(item) for item in value)
    )


def _digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


__all__ = [
    "BBox",
    "GeometryIssueEvent",
    "LayoutPath",
    "MappedSourceEvent",
    "NativeTextEvent",
    "RetrievalRunProof",
    "SourceEvidenceProof",
    "SourceEvidenceProofError",
    "SourcePageEvent",
    "SourcePageProof",
    "SourceProofIdentity",
    "VerifiedVisualArtifact",
    "VisualArtifactProof",
    "VisualBindingProof",
    "VisualPageFallback",
]
