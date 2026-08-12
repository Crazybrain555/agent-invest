"""Build one thin Unit per coarse section from an admitted provider document."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import string

from disclosure_anchor.application.contracts.document_outline import (
    CoarseUnit,
    DocumentOutline,
    HeadingLevelHint,
    HeadingNegativeHint,
    ResolvedHeading,
)
from disclosure_anchor.application.contracts.html_visible_text import (
    html_visible_text,
    html_visible_text_segments,
)
from disclosure_anchor.application.contracts.provider_document import (
    ProviderArtifact,
    ProviderBlock,
    ProviderDocument,
    ProviderPayload,
    provider_payload_field_contract,
)
from disclosure_anchor.application.contracts.provider_document_admission import (
    AdmittedProviderDocument,
)
from disclosure_anchor.application.contracts.provider_table_projection import (
    ProviderLogicalTable,
    ProviderTableProjection,
    UnboundProviderTablePart,
)
from disclosure_anchor.application.contracts.provider_unit import (
    ProviderSearchDestination,
    ProviderUnitBuildResult,
    ProviderUnitDraft,
    ProviderUnitEvidenceArtifact,
    ProviderUnitHeadingRef,
    ProviderUnitLocator,
    ProviderUnitPartKind,
    ProviderUnitPartRef,
    ProviderUnitPayloadKind,
    ProviderUnitSearchBinding,
    ProviderUnitSearchContractError,
    provider_unit_locator_from_payload,
)
from disclosure_anchor.application.contracts.retrieval_primary import (
    RetrievalPrimaryProjection,
    RetrievalTarget,
)
from disclosure_anchor.application.services.document_outline import (
    build_document_outline,
)
from disclosure_anchor.application.services.provider_table_projection import (
    build_provider_table_projection,
    semantic_page_furniture_source_indices,
)
from disclosure_anchor.application.services.retrieval_primary import (
    build_retrieval_primary_projection,
    replay_retrieval_target,
)
from disclosure_anchor.domain.services.unit_hashing import compute_unit_hashes


@dataclass(frozen=True, slots=True)
class _Part:
    ref: ProviderUnitPartRef
    provider_type: str
    payload: dict[str, object]
    targets: tuple[RetrievalTarget, ...]


def build_provider_units(
    admitted: AdmittedProviderDocument,
    *,
    level_hints: Iterable[HeadingLevelHint] = (),
    negative_hints: Iterable[HeadingNegativeHint] = (),
) -> ProviderUnitBuildResult:
    """Build deterministic drafts; the capability cannot be bypassed with a DTO."""

    document = admitted.provider_document
    outline = build_document_outline(
        document,
        level_hints=level_hints,
        negative_hints=negative_hints,
    )
    tables = build_provider_table_projection(document)
    retrieval = build_retrieval_primary_projection(document, outline, tables)
    context = _BuildContext(
        admitted=admitted,
        document=document,
        outline=outline,
        tables=tables,
        retrieval=retrieval,
    )
    drafts = tuple(context.build_unit(unit) for unit in outline.units)
    unassigned = tuple(
        part for part in tables.unbound_parts if part.part.block_source_index is None
    )
    result = ProviderUnitBuildResult(
        provider_document_sha256=admitted.provider_document_sha256,
        units=drafts,
        unassigned_table_parts=unassigned,
    )
    _validate_build(context=context, result=result)
    return result


class _BuildContext:
    def __init__(
        self,
        *,
        admitted: AdmittedProviderDocument,
        document: ProviderDocument,
        outline: DocumentOutline,
        tables: ProviderTableProjection,
        retrieval: RetrievalPrimaryProjection,
    ) -> None:
        self.admitted = admitted
        self.document = document
        self.outline = outline
        self.tables = tables
        self.retrieval = retrieval
        self.blocks = {block.source_index: block for block in document.blocks}
        self.artifacts = {artifact.role: artifact for artifact in document.artifacts}
        self.headings = {heading.heading_id: heading for heading in outline.headings}
        self.targets = {target.target_id: target for target in retrieval.targets}
        self.target_ids_by_source = {
            selection.source_index: selection.target_ids
            for selection in retrieval.blocks
        }
        self.logical_by_owner: dict[int, tuple[int, ProviderLogicalTable]] = {}
        self.logical_by_continuation: dict[int, tuple[int, ProviderLogicalTable]] = {}
        for table_index, table in enumerate(tables.logical_tables):
            owner_source = table.owner.block_source_index
            assert owner_source is not None
            self.logical_by_owner[owner_source] = (table_index, table)
            for continuation in table.continuations:
                source_index = continuation.block_source_index
                assert source_index is not None
                self.logical_by_continuation[source_index] = (table_index, table)
        self.unbound_by_block = {
            part.part.block_source_index: part
            for part in tables.unbound_parts
            if part.part.block_source_index is not None
        }
        self.semantic_furniture = semantic_page_furniture_source_indices(self.document)

    def build_unit(self, unit: CoarseUnit) -> ProviderUnitDraft:
        unit_sources = set(unit.block_source_indices)
        heading = None if unit.heading_id is None else self.headings[unit.heading_id]
        heading_chain = self._heading_chain(heading)
        parts: list[_Part] = []
        evidence_only: list[int] = []
        bound_unbound: list[UnboundProviderTablePart] = []
        search_bindings: list[ProviderUnitSearchBinding] = []

        if heading is not None:
            heading_targets = self._targets_for_source(heading.source_index)
            if len(heading_targets) != 1:
                raise ValueError(
                    "accepted heading must expose exactly one source target"
                )
            target = heading_targets[0]
            if _source_payload_text(self.document, target) != heading.text:
                raise ValueError("accepted heading target differs from its source text")
            search_bindings.append(
                ProviderUnitSearchBinding(
                    source=target,
                    destination=ProviderSearchDestination(kind="unit_title"),
                )
            )

        consumed: set[int] = set()
        for source_index in unit.block_source_indices:
            if source_index in consumed:
                continue
            block = self.blocks[source_index]
            if heading is not None and source_index == heading.source_index:
                consumed.add(source_index)
                continue
            if source_index in self.logical_by_continuation:
                raise ValueError("logical table continuation appeared before its owner")
            logical = self.logical_by_owner.get(source_index)
            if logical is not None:
                table_index, logical_table = logical
                member_sources = tuple(
                    part.block_source_index
                    for part in (logical_table.owner, *logical_table.continuations)
                )
                if any(member is None for member in member_sources):
                    raise ValueError("logical table contains an unbound provider block")
                sources = tuple(
                    member for member in member_sources if member is not None
                )
                if not set(sources).issubset(unit_sources):
                    raise ValueError("logical table crosses a coarse Unit")
                segment_indices = tuple(
                    part.physical_segment_index
                    for part in (logical_table.owner, *logical_table.continuations)
                )
                if any(index is None for index in segment_indices):
                    raise ValueError(
                        "logical table contains an unbound physical segment"
                    )
                part = self._part(
                    block=block,
                    part_index=len(parts),
                    kind="table",
                    block_source_indices=sources,
                    physical_segment_indices=tuple(
                        index for index in segment_indices if index is not None
                    ),
                    logical_table_index=table_index,
                )
                parts.append(part)
                consumed.update(sources)
                continue
            unbound = self.unbound_by_block.get(source_index)
            if unbound is not None:
                segment_index = unbound.part.physical_segment_index
                parts.append(
                    self._part(
                        block=block,
                        part_index=len(parts),
                        kind="table",
                        block_source_indices=(source_index,),
                        physical_segment_indices=()
                        if segment_index is None
                        else (segment_index,),
                        logical_table_index=None,
                    )
                )
                bound_unbound.append(unbound)
                consumed.add(source_index)
                continue
            if block.provider_type == "table":
                raise ValueError(
                    "table block is missing from the complete table projection"
                )
            if source_index in self.semantic_furniture:
                evidence_only.append(source_index)
                consumed.add(source_index)
                continue
            if (
                self._targets_for_source(source_index)
                or block.referenced_artifact_roles
            ):
                parts.append(
                    self._part(
                        block=block,
                        part_index=len(parts),
                        kind=_part_kind(block),
                        block_source_indices=(source_index,),
                        physical_segment_indices=(),
                        logical_table_index=None,
                    )
                )
            else:
                evidence_only.append(source_index)
            consumed.add(source_index)

        if consumed != unit_sources:
            raise ValueError(
                "provider Unit builder did not classify every source block"
            )
        payload_kind, payload = _unit_payload(parts)
        for part in parts:
            for target in part.targets:
                destination = _target_destination(
                    payload_kind=payload_kind,
                    part=part,
                    target=target,
                )
                binding = ProviderUnitSearchBinding(
                    source=target,
                    destination=destination,
                )
                search_bindings.append(binding)

        locator = ProviderUnitLocator(
            provider_document_sha256=self.admitted.provider_document_sha256,
            unit_index=unit.unit_index,
            heading_chain=heading_chain,
            parts=tuple(part.ref for part in parts),
            evidence_only_block_source_indices=tuple(evidence_only),
            unbound_table_parts=tuple(bound_unbound),
            evidence_artifacts=self._evidence_artifacts(parts),
            search_targets=tuple(search_bindings),
        )
        heading_path = () if heading is None else heading.headpath
        quality_status = (
            "needs_review"
            if bound_unbound
            or _has_suspected_truncated_markup_title(heading)
            or any(
                _has_suspected_encoded_text(self.blocks[source_index])
                for source_index in unit_sources
            )
            else "ok"
        )
        hashes = compute_unit_hashes(
            payload_kind=payload_kind,
            payload=payload,
            title=None if heading is None else heading.text,
            heading_path=list(heading_path),
            semantic_key=None,
            semantic_keys=None,
            quality_status=quality_status,
            order_index=unit.unit_index + 1,
        )
        draft = ProviderUnitDraft(
            unit_index=unit.unit_index,
            payload_kind=payload_kind,
            payload=payload,
            title=None if heading is None else heading.text,
            heading_path=heading_path,
            semantic_key=None,
            semantic_keys=None,
            quality_status=quality_status,
            page_no=self.blocks[unit.block_source_indices[0]].page_index + 1,
            locator=locator,
            content_hash=hashes.content_hash,
            query_projection_hash=hashes.query_projection_hash,
            structure_hash=hashes.structure_hash,
        )
        for binding in locator.search_targets:
            replay_provider_unit_search_binding(self.admitted, draft, binding)
        return draft

    def _heading_chain(
        self,
        heading: ResolvedHeading | None,
    ) -> tuple[ProviderUnitHeadingRef, ...]:
        if heading is None:
            return ()
        chain: list[ResolvedHeading] = []
        current: ResolvedHeading | None = heading
        while current is not None:
            chain.append(current)
            current = (
                None
                if current.parent_heading_id is None
                else self.headings[current.parent_heading_id]
            )
        chain.reverse()
        if tuple(item.text for item in chain) != heading.headpath:
            raise ValueError("resolved heading chain differs from its headpath")
        return tuple(
            ProviderUnitHeadingRef(
                heading_id=item.heading_id,
                source_index=item.source_index,
                placement_source=item.placement_source,
            )
            for item in chain
        )

    def _part(
        self,
        *,
        block: ProviderBlock,
        part_index: int,
        kind: ProviderUnitPartKind,
        block_source_indices: tuple[int, ...],
        physical_segment_indices: tuple[int, ...],
        logical_table_index: int | None,
    ) -> _Part:
        segment_artifact_roles = tuple(
            role
            for segment_index in physical_segment_indices
            if (
                role := self.document.physical_table_segments[
                    segment_index
                ].crop_artifact_role
            )
            is not None
        )
        content_artifact_roles = tuple(
            dict.fromkeys((*block.referenced_artifact_roles, *segment_artifact_roles))
        )
        return _Part(
            ref=ProviderUnitPartRef(
                part_index=part_index,
                kind=kind,
                block_source_indices=block_source_indices,
                physical_table_segment_indices=physical_segment_indices,
                logical_table_index=logical_table_index,
            ),
            provider_type=block.provider_type,
            payload=_part_payload(
                block,
                kind=kind,
                artifacts=self.artifacts,
                content_artifact_roles=content_artifact_roles,
            ),
            targets=self._targets_for_source(block.source_index),
        )

    def _targets_for_source(self, source_index: int) -> tuple[RetrievalTarget, ...]:
        return tuple(
            self.targets[target_id]
            for target_id in self.target_ids_by_source[source_index]
        )

    def _evidence_artifacts(
        self,
        parts: list[_Part],
    ) -> tuple[ProviderUnitEvidenceArtifact, ...]:
        roles: list[str] = []
        for part in parts:
            if part.ref.kind == "visual":
                for source_index in part.ref.block_source_indices:
                    roles.extend(self.blocks[source_index].referenced_artifact_roles)
            for segment_index in part.ref.physical_table_segment_indices:
                role = self.document.physical_table_segments[
                    segment_index
                ].crop_artifact_role
                if role is not None:
                    roles.append(role)

        by_hash: dict[str, ProviderUnitEvidenceArtifact] = {}
        ordered: list[ProviderUnitEvidenceArtifact] = []
        for role in roles:
            artifact = self.artifacts[role]
            descriptor = ProviderUnitEvidenceArtifact(
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                media_type=artifact.media_type,
            )
            existing = by_hash.get(descriptor.sha256)
            if existing is not None:
                if existing != descriptor:
                    raise ValueError(
                        "provider evidence metadata conflicts for one digest"
                    )
                continue
            by_hash[descriptor.sha256] = descriptor
            ordered.append(descriptor)
        return tuple(ordered)


def _has_suspected_encoded_text(block: ProviderBlock) -> bool:
    """Flag improbable ASCII-glyph maps without replacing their source text."""

    for payload in block.payloads:
        if payload.field not in {"text", "content"}:
            continue
        visible = "".join(html_visible_text(payload.text).split())
        if len(visible) < 24 or any("\u4e00" <= char <= "\u9fff" for char in visible):
            continue
        punctuation = [char for char in visible if char in string.punctuation]
        if len(punctuation) / len(visible) >= 0.45 and len(set(punctuation)) >= 12:
            return True
    return False


def _has_suspected_truncated_markup_title(
    heading: ResolvedHeading | None,
) -> bool:
    """Flag a provider title reduced to inline markup and non-CJK residue.

    MinerU can preserve a superscript trademark while dropping the adjacent
    Chinese drug name.  The source scalar remains untouched; this only makes
    the unresolved provider damage visible to downstream review.
    """

    if heading is None:
        return False
    folded = heading.text.casefold()
    if "<sup" not in folded and "<sub" not in folded:
        return False
    visible = html_visible_text(heading.text)
    return not any("\u4e00" <= character <= "\u9fff" for character in visible)


def replay_provider_unit_search_binding(
    admitted: AdmittedProviderDocument,
    draft: ProviderUnitDraft,
    binding: ProviderUnitSearchBinding,
) -> tuple[str, ...]:
    """Replay one flat binding and reject any Unit or source drift."""

    if draft.locator.provider_document_sha256 != admitted.provider_document_sha256:
        raise ValueError("provider Unit locator belongs to a different document")
    if binding not in draft.locator.search_targets:
        raise ValueError("search binding does not belong to the provider Unit")
    _validate_binding_owner(locator=draft.locator, binding=binding)
    return _replay_binding(
        document=admitted.provider_document,
        payload=draft.payload,
        payload_kind=draft.payload_kind,
        title=draft.title,
        binding=binding,
    )


def provider_unit_search_text_values(
    *,
    payload_kind: str,
    payload: Mapping[str, object],
    title: str | None,
    artifact_locator: object,
) -> tuple[str, ...]:
    """Replay body atoms from one persisted provider locator, never discovery."""

    try:
        locator = provider_unit_locator_from_payload(artifact_locator)
        values: list[str] = []
        payload_value = dict(payload)
        for binding in locator.search_targets:
            _validate_binding_owner(locator=locator, binding=binding)
            if binding.destination.kind == "unit_title":
                if (
                    title is None
                    or _destination_text(
                        payload=payload_value,
                        payload_kind=payload_kind,
                        title=title,
                        destination=binding.destination,
                    )
                    != title
                ):
                    raise ValueError("provider Unit title binding is invalid")
                continue
            destination_text = _destination_text(
                payload=payload_value,
                payload_kind=payload_kind,
                title=title,
                destination=binding.destination,
            )
            if binding.source.transform == "identity.v1":
                if destination_text.strip():
                    values.append(destination_text)
            else:
                values.extend(html_visible_text_segments(destination_text))
        return tuple(values)
    except (TypeError, ValueError) as exc:
        raise ProviderUnitSearchContractError(str(exc)) from exc


def _validate_binding_owner(
    *,
    locator: ProviderUnitLocator,
    binding: ProviderUnitSearchBinding,
) -> None:
    source_index = binding.source.source_index
    destination = binding.destination
    if destination.kind == "unit_title":
        if (
            not locator.heading_chain
            or locator.heading_chain[-1].source_index != source_index
        ):
            raise ValueError("provider search title source is not the Unit heading")
        return
    if (
        destination.field != binding.source.field
        or destination.item_index != binding.source.item_index
    ):
        raise ValueError("provider search destination differs from its source field")
    if destination.kind == "unit_payload":
        if (
            len(locator.parts) != 1
            or source_index not in locator.parts[0].block_source_indices
        ):
            raise ValueError("provider search source is not owned by the Unit payload")
        return
    part_index = destination.part_index
    if part_index is None or part_index >= len(locator.parts):
        raise ValueError("provider search mixed part is not owned by the Unit")
    if source_index not in locator.parts[part_index].block_source_indices:
        raise ValueError("provider search source is not owned by its mixed part")


def _part_payload(
    block: ProviderBlock,
    *,
    kind: ProviderUnitPartKind,
    artifacts: dict[str, ProviderArtifact],
    content_artifact_roles: tuple[str, ...],
) -> dict[str, object]:
    payload: dict[str, object] = {}
    scalar_fields, sequence_fields = provider_payload_field_contract(
        block.provider_type
    )
    by_field: dict[str, list[ProviderPayload]] = {}
    for item in block.payloads:
        by_field.setdefault(item.field, []).append(item)
    for field in scalar_fields:
        values = by_field.get(field, [])
        if values:
            payload[field] = values[0].text
    for field in sequence_fields:
        values = by_field.get(field, [])
        if values:
            payload[field] = [item.text for item in values]
    if content_artifact_roles and (
        kind == "visual"
        or not any(
            item.field in scalar_fields and item.text.strip() for item in block.payloads
        )
    ):
        payload["content_artifacts"] = [
            {
                "media_type": artifacts[role].media_type,
                "sha256": artifacts[role].sha256,
                "size_bytes": artifacts[role].size_bytes,
            }
            for role in content_artifact_roles
        ]
    return payload


def _part_kind(block: ProviderBlock) -> ProviderUnitPartKind:
    if block.provider_type == "table":
        return "table"
    if block.provider_type in {"image", "chart"}:
        return "visual"
    return "text"


def _unit_payload(
    parts: list[_Part],
) -> tuple[ProviderUnitPayloadKind, dict[str, object]]:
    if not parts:
        return "text", {"text": ""}
    if len(parts) == 1:
        part = parts[0]
        if part.ref.kind == "table":
            return "table", dict(part.payload)
        if part.ref.kind == "text" and part.provider_type == "text":
            return "text", dict(part.payload)
    return (
        "mixed",
        {"parts": [dict(part.payload) for part in parts]},
    )

def _target_destination(
    *,
    payload_kind: str,
    part: _Part,
    target: RetrievalTarget,
) -> ProviderSearchDestination:
    if payload_kind in {"text", "table"}:
        return ProviderSearchDestination(
            kind="unit_payload",
            field=target.field,
            item_index=target.item_index,
        )
    return ProviderSearchDestination(
        kind="mixed_part",
        part_index=part.ref.part_index,
        field=target.field,
        item_index=target.item_index,
    )


def _replay_binding(
    *,
    document: ProviderDocument,
    payload: dict[str, object],
    payload_kind: str,
    title: str | None,
    binding: ProviderUnitSearchBinding,
) -> tuple[str, ...]:
    source_text = _source_payload_text(document, binding.source)
    destination_text = _destination_text(
        payload=payload,
        payload_kind=payload_kind,
        title=title,
        destination=binding.destination,
    )
    if destination_text != source_text:
        raise ValueError("provider Unit search destination differs from its source")
    source_values = replay_retrieval_target(document, binding.source)
    destination_values: tuple[str, ...]
    if binding.source.transform == "identity.v1":
        destination_values = (destination_text,) if destination_text.strip() else ()
    else:
        destination_values = html_visible_text_segments(destination_text)
    if destination_values != source_values:
        raise ValueError("provider Unit search transform replay drifted")
    return source_values


def _source_payload_text(
    document: ProviderDocument,
    target: RetrievalTarget,
) -> str:
    if target.source_index >= len(document.blocks):
        raise ValueError("provider search source block is out of range")
    block = document.blocks[target.source_index]
    if block.raw_item_sha256 != target.raw_block_sha256:
        raise ValueError("provider search source block hash drifted")
    if target.payload_ordinal >= len(block.payloads):
        raise ValueError("provider search source payload is out of range")
    item = block.payloads[target.payload_ordinal]
    if item.field != target.field or item.item_index != target.item_index:
        raise ValueError("provider search source payload identity drifted")
    return item.text


def _destination_text(
    *,
    payload: dict[str, object],
    payload_kind: str,
    title: str | None,
    destination: ProviderSearchDestination,
) -> str:
    if destination.kind == "unit_title":
        if title is None:
            raise ValueError("provider search title destination is missing")
        return title
    container: dict[str, object]
    if destination.kind == "unit_payload":
        if payload_kind not in {"text", "table"}:
            raise ValueError("provider search top-level destination is not scalar")
        container = payload
    else:
        if payload_kind != "mixed":
            raise ValueError("provider search mixed destination is not mixed")
        parts = payload.get("parts")
        if not isinstance(parts, list) or destination.part_index is None:
            raise ValueError("provider search mixed payload is invalid")
        try:
            candidate = parts[destination.part_index]
        except IndexError as exc:
            raise ValueError(
                "provider search part destination is out of range"
            ) from exc
        if not isinstance(candidate, dict):
            raise ValueError("provider search part destination is invalid")
        container = candidate
    field = destination.field
    if field is None or field not in container:
        raise ValueError("provider search destination field is missing")
    value = container[field]
    if destination.item_index is None:
        if not isinstance(value, str):
            raise ValueError("provider search scalar destination is invalid")
        return value
    if not isinstance(value, list):
        raise ValueError("provider search sequence destination is invalid")
    try:
        item = value[destination.item_index]
    except IndexError as exc:
        raise ValueError(
            "provider search sequence destination is out of range"
        ) from exc
    if not isinstance(item, str):
        raise ValueError("provider search sequence item is invalid")
    return item


def _validate_build(
    *,
    context: _BuildContext,
    result: ProviderUnitBuildResult,
) -> None:
    owned_blocks: list[int] = []
    owned_segments: list[int] = []
    search_targets: list[str] = []
    logical_tables: list[int] = []
    for unit, retrieval_unit, draft in zip(
        context.outline.units,
        context.retrieval.units,
        result.units,
        strict=True,
    ):
        if unit.unit_index != draft.unit_index:
            raise ValueError("provider Unit draft differs from its coarse Unit")
        draft_target_ids = tuple(
            binding.source.target_id for binding in draft.locator.search_targets
        )
        if (
            retrieval_unit.unit_index != draft.unit_index
            or retrieval_unit.target_ids != draft_target_ids
        ):
            raise ValueError(
                "provider Unit search targets differ from their coarse Unit"
            )
        if draft.title != unit.title or draft.heading_path != unit.headpath:
            raise ValueError("provider Unit heading differs from its coarse Unit")
        if draft.locator.heading_chain:
            owned_blocks.append(draft.locator.heading_chain[-1].source_index)
        for part in draft.locator.parts:
            owned_blocks.extend(part.block_source_indices)
            owned_segments.extend(part.physical_table_segment_indices)
            if part.logical_table_index is not None:
                logical_tables.append(part.logical_table_index)
        owned_blocks.extend(draft.locator.evidence_only_block_source_indices)
        search_targets.extend(
            binding.source.target_id for binding in draft.locator.search_targets
        )
    owned_segments.extend(
        part.part.physical_segment_index
        for part in result.unassigned_table_parts
        if part.part.physical_segment_index is not None
    )
    if sorted(owned_blocks) != list(range(len(context.document.blocks))) or len(
        owned_blocks
    ) != len(set(owned_blocks)):
        raise ValueError("provider Unit build must own every block exactly once")
    if sorted(owned_segments) != list(
        range(len(context.document.physical_table_segments))
    ) or len(owned_segments) != len(set(owned_segments)):
        raise ValueError(
            "provider Unit build must own every physical table segment once"
        )
    if search_targets != [target.target_id for target in context.retrieval.targets]:
        raise ValueError(
            "provider Unit build must bind every retrieval target in order"
        )
    if logical_tables != list(range(len(context.tables.logical_tables))):
        raise ValueError("provider Unit build must own every logical table in order")


__all__ = [
    "build_provider_units",
    "provider_unit_search_text_values",
    "replay_provider_unit_search_binding",
]
