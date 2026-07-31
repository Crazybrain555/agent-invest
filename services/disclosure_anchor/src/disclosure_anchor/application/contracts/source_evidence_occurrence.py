"""Canonical public identities for parser-neutral physical source events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from disclosure_anchor.application.contracts.source_evidence import (
    GeometryIssueEvent,
    NativeTextEvent,
    SourceProofIdentity,
    VisualArtifactProof,
)
from disclosure_anchor.application.contracts.unit_source_projection import (
    NORMALIZED_IR_SOURCE_KIND,
    SOURCE_EVIDENCE_ATOM_KIND,
    SOURCE_EVIDENCE_GEOMETRY_ISSUE_KIND,
    SOURCE_EVIDENCE_VISUAL_PAGE_KIND,
    source_ref_from_locator,
)


NativeEvidenceKind = Literal[
    "source_evidence_atom",
    "source_evidence_geometry_issue",
    "source_evidence_visual_page",
]
_ATOM = cast(NativeEvidenceKind, SOURCE_EVIDENCE_ATOM_KIND)
_GEOMETRY = cast(NativeEvidenceKind, SOURCE_EVIDENCE_GEOMETRY_ISSUE_KIND)
_VISUAL = cast(NativeEvidenceKind, SOURCE_EVIDENCE_VISUAL_PAGE_KIND)


class SourceOccurrenceIdentityError(ValueError):
    """A physical event cannot form a canonical public source identity."""


def punctuation_only_text(value: str) -> bool:
    """True for a run with no letter, digit or ideograph in any script.

    Pure leader/placeholder marks (TOC dot runs, empty-cell dashes) carry
    no retrievable content of their own; both the builder and the audit
    derive suppression from this one predicate so the closure stays
    two-sided.
    """

    return bool(value) and not any(char.isalnum() for char in value)


@dataclass(frozen=True, slots=True)
class SourceNativeOccurrence:
    occurrence_id: str
    kind: NativeEvidenceKind
    page_idx: int
    word_order: int
    source_ref: dict[str, Any]
    text: str | None
    visual_artifact: VisualArtifactProof | None
    retrieval_run: tuple[int, int] | None = None

    @property
    def needs_review(self) -> bool:
        return self.kind != _ATOM

    def as_source_element(self) -> dict[str, Any]:
        element: dict[str, Any] = {
            "kind": (
                "image"
                if self.kind == SOURCE_EVIDENCE_VISUAL_PAGE_KIND
                else "text"
            ),
            "raw_kind": self.kind,
            "page_no": self.page_idx + 1,
            "_native_word_order": self.word_order,
            "_native_source_ref": dict(self.source_ref),
        }
        if self.retrieval_run is not None:
            page_idx, run_index = self.retrieval_run
            element["_native_retrieval_run"] = {
                "kind": "source_evidence_run",
                "source_evidence_sha256": self.source_ref[
                    "source_evidence_sha256"
                ],
                "page_idx": page_idx,
                "run_index": run_index,
            }
        if self.text is not None:
            element["text"] = self.text
        if self.kind == SOURCE_EVIDENCE_ATOM_KIND:
            element["bbox"] = list(self.source_ref["bbox"])
        if self.visual_artifact is not None:
            element["_required_visual_artifact"] = (
                self.visual_artifact.as_dict()
            )
        if self.kind == SOURCE_EVIDENCE_VISUAL_PAGE_KIND:
            assert self.visual_artifact is not None
            digest = self.visual_artifact.sha256.removeprefix("sha256:")
            element["image_path"] = f"evidence/{digest}.png"
        return element


@dataclass(frozen=True, slots=True)
class SourceMappedAnchor:
    atom_index: int
    word_order: int
    source_item_index: int
    source_ref: dict[str, Any]
    order_state: Literal["monotonic", "conflict"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "atom_index": self.atom_index,
            "word_order": self.word_order,
            "source": dict(self.source_ref),
            "order_state": self.order_state,
        }


def native_text_occurrence(
    identity: SourceProofIdentity,
    *,
    page_idx: int,
    event: NativeTextEvent,
    retrieval_run: tuple[int, int],
) -> SourceNativeOccurrence:
    return _occurrence(
        identity,
        kind=_ATOM,
        page_idx=page_idx,
        word_order=event.word_order,
        identity_suffix=str(event.atom_index),
        text=event.text,
        retrieval_run=retrieval_run,
        atom_index=event.atom_index,
        atom_order=event.word_order,
        bbox=list(event.bbox),
        char_span=list(event.char_span),
        text_sha256=event.text_sha256,
    )


def geometry_issue_occurrence(
    identity: SourceProofIdentity,
    *,
    page_idx: int,
    event: GeometryIssueEvent,
) -> SourceNativeOccurrence:
    return _occurrence(
        identity,
        kind=_GEOMETRY,
        page_idx=page_idx,
        word_order=event.word_order,
        identity_suffix=f"{page_idx}:{event.word_order}",
        text=event.text,
        visual_artifact=event.visual_artifact,
        raw_bbox=list(event.raw_bbox) if event.raw_bbox is not None else None,
        reason=event.reason,
        text_sha256=event.text_sha256,
    )


def visual_page_occurrence(
    identity: SourceProofIdentity,
    *,
    page_idx: int,
    artifact: VisualArtifactProof,
    semantic_text: str | None = None,
    semantic_text_sha256: str | None = None,
) -> SourceNativeOccurrence:
    if semantic_text_sha256 is not None:
        return _occurrence(
            identity,
            kind=_VISUAL,
            page_idx=page_idx,
            word_order=0,
            identity_suffix=str(page_idx),
            text=semantic_text,
            visual_artifact=artifact,
            visual_sha256=artifact.sha256,
            text_sha256=semantic_text_sha256,
        )
    return _occurrence(
        identity,
        kind=_VISUAL,
        page_idx=page_idx,
        word_order=0,
        identity_suffix=str(page_idx),
        text=semantic_text,
        visual_artifact=artifact,
        visual_sha256=artifact.sha256,
    )


def _occurrence(
    identity: SourceProofIdentity,
    *,
    kind: NativeEvidenceKind,
    page_idx: int,
    word_order: int,
    identity_suffix: str,
    text: str | None = None,
    visual_artifact: VisualArtifactProof | None = None,
    retrieval_run: tuple[int, int] | None = None,
    **fields: object,
) -> SourceNativeOccurrence:
    return SourceNativeOccurrence(
        occurrence_id=(
            f"{kind}:{identity.source_evidence_sha256}:{identity_suffix}"
        ),
        kind=kind,
        page_idx=page_idx,
        word_order=word_order,
        source_ref={
            "kind": kind,
            "source_evidence_sha256": identity.source_evidence_sha256,
            "source_pdf_sha256": identity.source_pdf_sha256,
            "page_idx": page_idx,
            "page_no": page_idx + 1,
            **({"word_order": word_order} if kind == _GEOMETRY else {}),
            **fields,
        },
        text=text,
        visual_artifact=visual_artifact,
        retrieval_run=retrieval_run,
    )


def mapped_source_anchor(
    *,
    page_idx: int,
    atom_index: int,
    word_order: int,
    source_item_index: int,
    order_state: Literal["monotonic", "conflict"],
    element: Mapping[str, Any],
) -> SourceMappedAnchor:
    if element.get("page_idx") != page_idx:
        raise SourceOccurrenceIdentityError(
            "mapped source event differs from its NormalizedIR page"
        )
    source_ref = source_ref_from_locator(
        {
            "kind": NORMALIZED_IR_SOURCE_KIND,
            **{
                field: element[field]
                for field in (
                    "ir_id",
                    "source_item_index",
                    "order_index",
                    "page_no",
                    "bbox",
                )
                if field in element
            },
        }
    )
    if source_ref is None:
        raise SourceOccurrenceIdentityError(
            "mapped source event has no canonical NormalizedIR identity"
        )
    return SourceMappedAnchor(
        atom_index=atom_index,
        word_order=word_order,
        source_item_index=source_item_index,
        source_ref=source_ref,
        order_state=order_state,
    )


__all__ = [
    "NativeEvidenceKind",
    "SourceMappedAnchor",
    "SourceNativeOccurrence",
    "SourceOccurrenceIdentityError",
    "geometry_issue_occurrence",
    "mapped_source_anchor",
    "native_text_occurrence",
    "visual_page_occurrence",
]
