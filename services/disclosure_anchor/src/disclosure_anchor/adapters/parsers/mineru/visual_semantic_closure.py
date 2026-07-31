"""Build or replay MinerU visual semantics without changing provider identity."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Literal, cast

from disclosure_anchor.adapters.parsers.mineru.content_list_contract import (
    mineru_provider_item_sha256,
    mineru_scalar_alias,
    resolved_image_path,
)
from disclosure_anchor.adapters.parsers.mineru.source_evidence import (
    SourceEvidenceContractError,
    source_visual_artifact_descriptors,
)
from disclosure_anchor.adapters.parsers.mineru.table_html_structure import (
    ParsedHtmlTable,
    table_media_artifact_role,
)
from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)
from disclosure_anchor.application.contracts.visual_semantics import (
    MINERU_VL_UTILS_PACKAGE_VERSION,
    TableMediaLocator,
    VisualOccurrenceKind,
    VisualSemanticClosure,
    VisualSemanticContractError,
    VisualSemanticDisposition,
    VisualSemanticOrigin,
    bytes_sha256,
    parser_target_sha256,
    text_sha256,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


@dataclass(frozen=True, slots=True)
class VisualContentExtractRequest:
    occurrence_id: str
    occurrence_kind: VisualOccurrenceKind
    artifact_path: Path
    artifact_sha256: str
    content_type: Literal["image", "chart", "equation"]


@dataclass(frozen=True, slots=True)
class VisualContentExtractResult:
    mineru_vl_utils_version: str
    values: tuple[str | None, ...]


VisualContentExtractor = Callable[
    [tuple[VisualContentExtractRequest, ...]],
    VisualContentExtractResult,
]
VisualArtifactResolver = Callable[[str], tuple[Path | None, str]]


@dataclass(frozen=True, slots=True)
class _Candidate:
    occurrence_id: str
    occurrence_kind: VisualOccurrenceKind
    source_item_index: int | None
    source_item_sha256: str | None
    page_idx: int
    bbox: tuple[float, float, float, float]
    table_media: TableMediaLocator | None
    artifact_role: str
    artifact_path: Path | None
    artifact_sha256: str
    content_type: Literal["image", "chart", "equation"]
    provider_text: str | None
    provider_origin: VisualSemanticOrigin | None

    def disposition(
        self,
        *,
        extracted_text: str | None = None,
    ) -> VisualSemanticDisposition:
        text = self.provider_text
        origin = self.provider_origin if text is not None else None
        if text is None and isinstance(extracted_text, str) and extracted_text.strip():
            text = extracted_text
            origin = "mineru_content_extract"
        return VisualSemanticDisposition(
            occurrence_id=self.occurrence_id,
            occurrence_kind=self.occurrence_kind,
            status="semantic_text" if text is not None else "unresolved",
            source_item_index=self.source_item_index,
            source_item_sha256=self.source_item_sha256,
            page_idx=self.page_idx,
            bbox=self.bbox,
            table_media=self.table_media,
            artifact_role=self.artifact_role,
            artifact_sha256=self.artifact_sha256,
            semantic_text=text,
            semantic_text_sha256=text_sha256(text) if text is not None else None,
            semantic_origin=origin,
        )


def resolve_visual_semantic_closure(
    *,
    identity_content_list: Sequence[Mapping[str, Any]],
    canonical_content_list: Sequence[Mapping[str, Any]],
    table_structures: Mapping[int, ParsedHtmlTable],
    artifact_resolver: VisualArtifactResolver,
    source_evidence: Mapping[str, Any],
    source_pdf_sha256: str,
    source_pdf_page_count: int,
    source_evidence_sha256: str,
    content_list_sha256: str,
    content_list_v2_sha256: str,
    middle_sha256: str,
    model_sha256: str,
    parser_target: ParserTargetIdentity,
    server_url: str | None,
    extractor: VisualContentExtractor | None = None,
    persisted_artifact: bytes | None = None,
) -> VisualSemanticClosure:
    """Resolve one exact occurrence set online or replay one immutable artifact.

    Exactly one mode is required.  The persisted mode never invokes the
    extractor, which is the explicit offline-replay boundary.
    """

    if (extractor is None) == (persisted_artifact is None):
        raise ParserOutputContractError(
            "visual semantics requires exactly one online extractor or "
            "persisted artifact"
        )
    if len(identity_content_list) != len(canonical_content_list):
        raise ParserOutputContractError(
            "visual semantics canonical/provider item counts differ"
        )
    if parser_target.formula is not True:
        raise ParserOutputContractError(
            "fresh visual closure requires MinerU formula recognition"
        )
    candidates = _visual_candidates(
        identity_content_list=identity_content_list,
        canonical_content_list=canonical_content_list,
        table_structures=table_structures,
        artifact_resolver=artifact_resolver,
        source_evidence=source_evidence,
    )
    target_hash = parser_target_sha256(parser_target.to_payload())
    if persisted_artifact is not None:
        closure = _read_persisted_closure(persisted_artifact)
        _validate_closure_attestation(
            closure,
            source_pdf_sha256=source_pdf_sha256,
            source_pdf_page_count=source_pdf_page_count,
            source_evidence_sha256=source_evidence_sha256,
            content_list_sha256=content_list_sha256,
            content_list_v2_sha256=content_list_v2_sha256,
            middle_sha256=middle_sha256,
            model_sha256=model_sha256,
            parser_target=parser_target,
            parser_target_digest=target_hash,
        )
        _validate_persisted_dispositions(
            candidates,
            closure.dispositions,
        )
        return closure

    assert extractor is not None
    pending = tuple(
        VisualContentExtractRequest(
            occurrence_id=candidate.occurrence_id,
            occurrence_kind=candidate.occurrence_kind,
            artifact_path=_required_online_path(candidate),
            artifact_sha256=candidate.artifact_sha256,
            content_type=candidate.content_type,
        )
        for candidate in candidates
        if candidate.provider_text is None
    )
    if pending and not server_url:
        raise ParserOutputContractError(
            "visual semantic enrichment requires the parser's MinerU server_url"
        )
    extraction = (
        extractor(pending)
        if pending
        else VisualContentExtractResult(
            mineru_vl_utils_version=MINERU_VL_UTILS_PACKAGE_VERSION,
            values=(),
        )
    )
    if (
        extraction.mineru_vl_utils_version
        != MINERU_VL_UTILS_PACKAGE_VERSION
        or len(extraction.values) != len(pending)
        or any(
            value is not None and not isinstance(value, str)
            for value in extraction.values
        )
    ):
        raise ParserOutputContractError(
            "official MinerU visual enrichment returned an invalid contract"
        )
    extracted = {
        request.occurrence_id: value
        for request, value in zip(pending, extraction.values, strict=True)
    }
    return VisualSemanticClosure(
        source_pdf_sha256=source_pdf_sha256,
        source_pdf_page_count=source_pdf_page_count,
        source_evidence_sha256=source_evidence_sha256,
        content_list_sha256=content_list_sha256,
        content_list_v2_sha256=content_list_v2_sha256,
        middle_sha256=middle_sha256,
        model_sha256=model_sha256,
        parser_target_sha256=target_hash,
        runtime_bundle_identity_sha256=(
            parser_target.runtime_bundle_identity_sha256
        ),
        mineru_package_version=parser_target.package_version,
        mineru_vl_utils_version=extraction.mineru_vl_utils_version,
        enrichment_backend="http-client",
        enrichment_image_analysis=True,
        server_url_sha256=bytes_sha256((server_url or "").encode("utf-8")),
        formula_enabled=True,
        dispositions=tuple(
            candidate.disposition(
                extracted_text=extracted.get(candidate.occurrence_id)
            )
            for candidate in candidates
        ),
    )


def semantic_dispositions_by_source(
    closure: VisualSemanticClosure,
) -> Mapping[int, VisualSemanticDisposition]:
    """Return non-table provider occurrences keyed by exact source item."""

    output: dict[int, VisualSemanticDisposition] = {}
    for item in closure.dispositions:
        source_index = item.source_item_index
        if (
            source_index is None
            or item.occurrence_kind in {"table_media", "carrier_guard"}
        ):
            continue
        prior = output.setdefault(source_index, item)
        if prior != item:
            raise ParserOutputContractError(
                "visual semantic source item has multiple owner dispositions"
            )
    return output


def semantic_dispositions_by_table_media(
    closure: VisualSemanticClosure,
) -> Mapping[tuple[int, int], VisualSemanticDisposition]:
    output: dict[tuple[int, int], VisualSemanticDisposition] = {}
    for item in closure.dispositions:
        if item.occurrence_kind != "table_media":
            continue
        assert item.source_item_index is not None
        assert item.table_media is not None
        key = (item.source_item_index, item.table_media.occurrence_index)
        prior = output.setdefault(key, item)
        if prior != item:
            raise ParserOutputContractError(
                "table media has multiple semantic dispositions"
            )
    return output


def _visual_candidates(
    *,
    identity_content_list: Sequence[Mapping[str, Any]],
    canonical_content_list: Sequence[Mapping[str, Any]],
    table_structures: Mapping[int, ParsedHtmlTable],
    artifact_resolver: VisualArtifactResolver,
    source_evidence: Mapping[str, Any],
) -> tuple[_Candidate, ...]:
    try:
        visual_descriptors = source_visual_artifact_descriptors(
            source_evidence
        )
    except SourceEvidenceContractError as exc:
        raise ParserOutputContractError(
            f"visual semantic source ledger is invalid: {exc}"
        ) from exc
    candidates: list[_Candidate] = []
    for source_index, (identity_item, item) in enumerate(
        zip(identity_content_list, canonical_content_list, strict=True)
    ):
        raw_kind = item.get("type")
        source_sha = mineru_provider_item_sha256(identity_item)
        if raw_kind in {"image", "chart"}:
            role = f"source_visual_occurrence_{source_index:06d}"
            candidates.append(
                _candidate(
                    occurrence_id=f"source:{source_index:06d}",
                    occurrence_kind=cast(VisualOccurrenceKind, raw_kind),
                    source_item_index=source_index,
                    source_item_sha256=source_sha,
                    page_idx=_required_page_idx(item.get("page_idx")),
                    bbox=_required_bbox(item.get("bbox")),
                    table_media=None,
                    artifact_role=role,
                    artifact_resolver=artifact_resolver,
                    expected_artifact_sha256=_descriptor_sha256(
                        visual_descriptors,
                        role,
                    ),
                    content_type=cast(
                        Literal["image", "chart", "equation"],
                        raw_kind,
                    ),
                    provider_text=_provider_visual_text(item),
                    provider_origin="provider_visual_text",
                )
            )
        elif raw_kind == "equation" and resolved_image_path(item):
            role = f"evidence_image_{source_index:06d}"
            candidates.append(
                _candidate(
                    occurrence_id=f"source:{source_index:06d}",
                    occurrence_kind="equation",
                    source_item_index=source_index,
                    source_item_sha256=source_sha,
                    page_idx=_required_page_idx(item.get("page_idx")),
                    bbox=_required_bbox(item.get("bbox")),
                    table_media=None,
                    artifact_role=role,
                    artifact_resolver=artifact_resolver,
                    content_type="equation",
                    provider_text=_provider_visual_text(item),
                    provider_origin="provider_visual_text",
                )
            )
        if raw_kind != "table":
            continue
        structure = table_structures.get(source_index)
        if structure is None:
            continue
        page_idx = _required_page_idx(item.get("page_idx"))
        bbox = _required_bbox(item.get("bbox"))
        for media in structure.embedded_media:
            role = table_media_artifact_role(
                source_index,
                media.occurrence_index,
            )
            candidates.append(
                _candidate(
                    occurrence_id=(
                        f"source:{source_index:06d}:table_media:"
                        f"{media.occurrence_index:06d}"
                    ),
                    occurrence_kind="table_media",
                    source_item_index=source_index,
                    source_item_sha256=source_sha,
                    page_idx=page_idx,
                    bbox=bbox,
                    table_media=TableMediaLocator(
                        occurrence_index=media.occurrence_index,
                        cell_media_index=media.cell_media_index,
                        row=media.row,
                        col=media.col,
                        rowspan=media.rowspan,
                        colspan=media.colspan,
                    ),
                    artifact_role=role,
                    artifact_resolver=artifact_resolver,
                    content_type="image",
                    # Cell text and HTML alt/title are retained as source
                    # metadata, but neither proves the image bytes' semantics.
                    provider_text=None,
                    provider_origin=None,
                )
            )
    raw_pages = source_evidence.get("pages")
    if not isinstance(raw_pages, list):
        raise ParserOutputContractError(
            "source evidence pages are required for visual closure"
        )
    for raw_page in raw_pages:
        if not isinstance(raw_page, Mapping) or raw_page.get("modality") != (
            "visual_page"
        ):
            continue
        page_idx = _required_page_idx(raw_page.get("page_idx"))
        descriptor = raw_page.get("visual_artifact")
        if not isinstance(descriptor, Mapping):
            raise ParserOutputContractError(
                f"visual-only page artifact is missing: {page_idx}"
            )
        visual_role = descriptor.get("artifact_role")
        if not isinstance(visual_role, str):
            raise ParserOutputContractError(
                f"visual-only page artifact role is invalid: {page_idx}"
            )
        candidates.append(
            _candidate(
                occurrence_id=f"page:{page_idx:06d}",
                occurrence_kind="visual_page",
                source_item_index=None,
                source_item_sha256=None,
                page_idx=page_idx,
                bbox=(0.0, 0.0, 1000.0, 1000.0),
                table_media=None,
                artifact_role=visual_role,
                artifact_resolver=artifact_resolver,
                expected_artifact_sha256=_descriptor_sha256(
                    visual_descriptors,
                    role,
                ),
                content_type="image",
                provider_text=None,
                provider_origin=None,
            )
        )
    return tuple(sorted(candidates, key=lambda item: item.occurrence_id))


def _candidate(
    **values: Any,
) -> _Candidate:
    resolver = cast(VisualArtifactResolver, values.pop("artifact_resolver"))
    expected = cast(str | None, values.pop("expected_artifact_sha256", None))
    role = cast(str, values["artifact_role"])
    try:
        path, actual = resolver(role)
    except (OSError, KeyError) as exc:
        raise ParserOutputContractError(
            f"visual semantic source artifact is missing: {role}"
        ) from exc
    if expected is not None and actual != expected:
        raise ParserOutputContractError(
            f"visual artifact differs from source evidence: {role}"
        )
    values["artifact_path"] = path
    values["artifact_sha256"] = actual
    candidate = _Candidate(**values)
    if path is not None and _path_sha256(path) != actual:
        raise ParserOutputContractError(
            f"visual artifact bytes differ: {candidate.artifact_role}"
        )
    return candidate


def _read_persisted_closure(payload: bytes) -> VisualSemanticClosure:
    try:
        decoded = json.loads(payload)
        return VisualSemanticClosure.from_payload(decoded)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        VisualSemanticContractError,
    ) as exc:
        reason = getattr(exc, "reason_code", "visual_artifact_json_invalid")
        raise ParserOutputContractError(
            f"persisted visual semantic artifact is invalid [{reason}]: {exc}"
        ) from exc


def _validate_closure_attestation(
    closure: VisualSemanticClosure,
    *,
    source_pdf_sha256: str,
    source_pdf_page_count: int,
    source_evidence_sha256: str,
    content_list_sha256: str,
    content_list_v2_sha256: str,
    middle_sha256: str,
    model_sha256: str,
    parser_target: ParserTargetIdentity,
    parser_target_digest: str,
) -> None:
    expected = (
        source_pdf_sha256,
        source_pdf_page_count,
        source_evidence_sha256,
        content_list_sha256,
        content_list_v2_sha256,
        middle_sha256,
        model_sha256,
        parser_target_digest,
        parser_target.runtime_bundle_identity_sha256,
        parser_target.package_version,
        parser_target.formula,
    )
    actual = (
        closure.source_pdf_sha256,
        closure.source_pdf_page_count,
        closure.source_evidence_sha256,
        closure.content_list_sha256,
        closure.content_list_v2_sha256,
        closure.middle_sha256,
        closure.model_sha256,
        closure.parser_target_sha256,
        closure.runtime_bundle_identity_sha256,
        closure.mineru_package_version,
        closure.formula_enabled,
    )
    if actual != expected:
        raise ParserOutputContractError(
            "persisted visual semantic artifact attestation differs from source/runtime"
        )


def _validate_persisted_dispositions(
    candidates: Sequence[_Candidate],
    dispositions: Sequence[VisualSemanticDisposition],
) -> None:
    if [item.occurrence_id for item in dispositions] != [
        item.occurrence_id for item in candidates
    ]:
        raise ParserOutputContractError(
            "persisted visual semantic occurrence set differs from source evidence"
        )
    for candidate, disposition in zip(candidates, dispositions, strict=True):
        expected_identity = candidate.disposition()
        identity_fields = (
            "occurrence_kind",
            "source_item_index",
            "source_item_sha256",
            "page_idx",
            "bbox",
            "table_media",
            "artifact_role",
            "artifact_sha256",
        )
        if any(
            getattr(disposition, field) != getattr(expected_identity, field)
            for field in identity_fields
        ):
            raise ParserOutputContractError(
                "persisted visual semantic occurrence identity differs: "
                f"{candidate.occurrence_id}"
            )
        if candidate.provider_text is not None and disposition != expected_identity:
            raise ParserOutputContractError(
                "persisted visual semantics rewrote provider semantic text: "
                f"{candidate.occurrence_id}"
            )


def _provider_visual_text(item: Mapping[str, Any]) -> str | None:
    value = mineru_scalar_alias(item, ("text", "content"))
    return value if isinstance(value, str) and value.strip() else None


def _required_page_idx(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ParserOutputContractError(
            "visual semantic occurrence requires a source page"
        )
    return value


def _required_bbox(value: object) -> tuple[float, float, float, float]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(
            isinstance(item, bool) or not isinstance(item, (int, float))
            for item in value
        )
    ):
        raise ParserOutputContractError(
            "visual semantic occurrence requires an exact bbox"
        )
    bbox = tuple(float(item) for item in value)
    if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        raise ParserOutputContractError(
            "visual semantic occurrence bbox is not positive"
        )
    return cast(tuple[float, float, float, float], bbox)


def _required_online_path(candidate: _Candidate) -> Path:
    if candidate.artifact_path is None:
        raise ParserOutputContractError(
            f"live visual artifact path is missing: {candidate.artifact_role}"
        )
    return candidate.artifact_path


def _descriptor_sha256(
    descriptors: Mapping[str, Mapping[str, Any]],
    role: str,
) -> str:
    descriptor = descriptors.get(role)
    sha256 = descriptor.get("sha256") if descriptor is not None else None
    if not isinstance(sha256, str):
        raise ParserOutputContractError(
            f"visual source ledger descriptor is missing: {role}"
        )
    return sha256


def _path_sha256(path: Path) -> str:
    try:
        with path.open("rb") as source:
            return "sha256:" + hashlib.file_digest(source, "sha256").hexdigest()
    except OSError as exc:
        raise ParserOutputContractError(
            f"cannot hash visual semantic source artifact: {path}"
        ) from exc


__all__ = [
    "VisualContentExtractRequest",
    "VisualContentExtractResult",
    "VisualContentExtractor",
    "VisualArtifactResolver",
    "resolve_visual_semantic_closure",
    "semantic_dispositions_by_source",
    "semantic_dispositions_by_table_media",
]
