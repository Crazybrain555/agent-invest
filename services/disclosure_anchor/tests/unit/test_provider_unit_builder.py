from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import cast
import unittest

from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity
from disclosure_anchor.application.contracts.provider_document import (
    ProviderArtifact,
    ProviderBlock,
    ProviderDocument,
    ProviderPage,
    ProviderPayload,
    ProviderPhysicalTableSegment,
    provider_artifact_bundle_sha256,
)
from disclosure_anchor.application.contracts.provider_document_admission import (
    AdmittedProviderDocument,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    ProviderDocumentEnvelope,
    provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.contracts.provider_unit import (
    ProviderUnitLocator,
    provider_unit_locator_from_payload,
    provider_unit_locator_to_payload,
)
from disclosure_anchor.application.contracts.provider_table_projection import (
    ProviderTablePartRef,
    UnboundProviderTablePart,
)
from disclosure_anchor.application.services.provider_unit_builder import (
    build_provider_units,
    replay_provider_unit_search_binding,
)


_SOURCE_SHA = "sha256:" + "a" * 64
_OWNER = "run_01K0000000000000000000000"
_DOCUMENT = "doc_01K00000000000000000000000"


class ProviderUnitBuilderTests(unittest.TestCase):
    def test_heading_table_visual_and_demoted_content_are_conserved_once(
        self,
    ) -> None:
        admitted = _admitted(_representative_document())

        result = build_provider_units(admitted)

        self.assertEqual(len(result.units), 2)
        preamble, section = result.units
        self.assertEqual(preamble.payload_kind, "text")
        self.assertEqual(preamble.payload, {"text": ""})
        self.assertEqual(
            preamble.locator.evidence_only_block_source_indices,
            (0,),
        )

        self.assertEqual(section.title, "第一章 标题")
        self.assertEqual(section.heading_path, ("第一章 标题",))
        self.assertEqual(section.payload_kind, "mixed")
        parts = section.payload["parts"]
        self.assertIsInstance(parts, list)
        assert isinstance(parts, list)
        self.assertTrue(all("provider_type" not in part for part in parts))
        self.assertEqual(
            [part.kind for part in section.locator.parts],
            ["text", "table", "visual", "text"],
        )
        self.assertNotIn("semantic_type", section.payload)
        self.assertTrue(all("kind" not in part for part in parts))
        self.assertNotIn("第一章 标题", json.dumps(section.payload, ensure_ascii=False))
        self.assertIn("□适用", json.dumps(section.payload, ensure_ascii=False))

        table_ref = section.locator.parts[1]
        self.assertEqual(table_ref.block_source_indices, (3, 5))
        self.assertEqual(table_ref.physical_table_segment_indices, (0, 1))
        self.assertEqual(table_ref.logical_table_index, 0)
        visual = parts[2]
        self.assertEqual(
            visual["content_artifacts"],
            [
                {
                    "media_type": "image/jpeg",
                    "sha256": "sha256:" + "f" * 64,
                    "size_bytes": 321,
                }
            ],
        )

        title_binding = section.locator.search_targets[0]
        self.assertEqual(title_binding.destination.kind, "unit_title")
        self.assertEqual(
            replay_provider_unit_search_binding(admitted, section, title_binding),
            ("第一章 标题",),
        )
        for binding in section.locator.search_targets:
            replay_provider_unit_search_binding(admitted, section, binding)
        self.assertEqual(
            [binding.source.source_index for binding in section.locator.search_targets],
            [1, 2, 3, 3, 7],
        )

        locator_payload = provider_unit_locator_to_payload(section.locator)
        self.assertEqual(
            provider_unit_locator_from_payload(locator_payload),
            section.locator,
        )
        self.assertEqual(
            [item.sha256 for item in section.locator.evidence_artifacts],
            ["sha256:" + "f" * 64],
        )
        encoded = json.dumps(locator_payload, ensure_ascii=False)
        self.assertNotIn("relative_path", encoded)
        self.assertNotIn("raw_item_json", encoded)

    def test_visual_digest_changes_content_hash_without_a_search_target(self) -> None:
        first = build_provider_units(_admitted(_visual_only_document("f"))).units[0]
        second = build_provider_units(_admitted(_visual_only_document("e"))).units[0]

        self.assertEqual(first.payload_kind, "mixed")
        self.assertEqual(first.locator.search_targets, ())
        self.assertNotEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.query_projection_hash, second.query_projection_hash)

    def test_table_without_body_keeps_crop_digest_in_content_hash(
        self,
    ) -> None:
        first = build_provider_units(_admitted(_table_visual_only_document("f"))).units[
            0
        ]
        second = build_provider_units(
            _admitted(_table_visual_only_document("e"))
        ).units[0]

        self.assertEqual(first.payload_kind, "table")
        self.assertNotIn("provider_type", first.payload)
        self.assertEqual(len(first.locator.search_targets), 1)
        self.assertIn("content_artifacts", first.payload)
        self.assertEqual(len(first.locator.evidence_artifacts), 1)
        self.assertNotEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.query_projection_hash, second.query_projection_hash)

    def test_table_with_body_does_not_treat_crop_as_semantic_content(
        self,
    ) -> None:
        first = build_provider_units(
            _admitted(_table_with_body_and_crop_document("f"))
        ).units[0]
        second = build_provider_units(
            _admitted(_table_with_body_and_crop_document("e"))
        ).units[0]

        self.assertEqual(first.payload_kind, "table")
        self.assertNotIn("provider_type", first.payload)
        self.assertNotIn("content_artifacts", first.payload)
        self.assertEqual(len(first.locator.evidence_artifacts), 1)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.query_projection_hash, second.query_projection_hash)

    def test_unit_locator_rejects_segment_only_unbound_table_parts(self) -> None:
        with self.assertRaisesRegex(ValueError, "segment-only"):
            ProviderUnitLocator(
                provider_document_sha256="sha256:" + "a" * 64,
                unit_index=0,
                heading_chain=(),
                parts=(),
                evidence_only_block_source_indices=(),
                unbound_table_parts=(
                    UnboundProviderTablePart(
                        part=ProviderTablePartRef(
                            block_source_index=None,
                            physical_segment_index=0,
                        ),
                        reason="page_table_count_mismatch",
                    ),
                ),
                evidence_artifacts=(),
                search_targets=(),
            )

    def test_unit_locator_decoder_rejects_unknown_and_malformed_fields(self) -> None:
        locator = (
            build_provider_units(_admitted(_representative_document())).units[1].locator
        )
        payload = provider_unit_locator_to_payload(locator)
        payload["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "locator fields"):
            provider_unit_locator_from_payload(payload)

        payload = provider_unit_locator_to_payload(locator)
        parts = cast(list[dict[str, object]], payload["parts"])
        parts[0]["part_index"] = True
        with self.assertRaisesRegex(ValueError, "part index"):
            provider_unit_locator_from_payload(payload)

    def test_heading_only_unit_keeps_structure_without_body_duplication(self) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "唯一标题"),),
                        annotation="title",
                        level=1,
                    ),
                ),
            ),
            segments=(),
        )

        draft = build_provider_units(_admitted(document)).units[0]

        self.assertEqual(draft.title, "唯一标题")
        self.assertEqual(draft.payload_kind, "text")
        self.assertEqual(
            draft.payload,
            {"text": ""},
        )
        self.assertEqual(len(draft.locator.search_targets), 1)
        self.assertEqual(
            draft.locator.search_targets[0].destination.kind,
            "unit_title",
        )

    def test_empty_text_carrier_is_evidence_only_but_visual_content_survives(
        self,
    ) -> None:
        artifact = ProviderArtifact(
            role="image_0001",
            relative_path="e_images/figure.jpg",
            sha256="sha256:" + "f" * 64,
            size_bytes=321,
            media_type="image/jpeg",
        )
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, ""),),
                        annotation="paragraph",
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (ProviderPayload("text", None, "正文"),),
                        annotation="paragraph",
                    ),
                    _block(
                        2,
                        0,
                        "image",
                        (ProviderPayload("content", None, ""),),
                        annotation="image",
                        artifact_roles=(artifact.role,),
                    ),
                ),
            ),
            segments=(),
            extra_artifacts=(artifact,),
        )

        draft = build_provider_units(_admitted(document)).units[0]

        self.assertEqual(draft.payload_kind, "mixed")
        parts = cast(list[dict[str, object]], draft.payload["parts"])
        self.assertTrue(all("provider_type" not in part for part in parts))
        self.assertEqual(
            [part.kind for part in draft.locator.parts],
            ["text", "visual"],
        )
        self.assertTrue(all("kind" not in part for part in parts))
        self.assertEqual(draft.locator.evidence_only_block_source_indices, (0,))
        self.assertEqual(
            [binding.source.source_index for binding in draft.locator.search_targets],
            [1],
        )

    def test_unique_typed_header_is_content_not_semantic_furniture(self) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "header",
                        (ProviderPayload("text", None, "证券代码：000001"),),
                        annotation="page_header",
                    ),
                ),
            ),
            segments=(),
        )

        draft = build_provider_units(_admitted(document)).units[0]

        self.assertEqual(draft.payload_kind, "mixed")
        parts = cast(list[dict[str, object]], draft.payload["parts"])
        self.assertNotIn("provider_type", parts[0])
        self.assertEqual(parts[0]["text"], "证券代码：000001")
        self.assertEqual(document.blocks[0].provider_type, "header")
        self.assertEqual(draft.locator.parts[0].kind, "text")
        self.assertEqual(draft.locator.evidence_only_block_source_indices, ())
        self.assertEqual(len(draft.locator.search_targets), 1)

    def test_unbound_block_is_published_and_segment_only_parts_are_not_guessed(
        self,
    ) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "table",
                        (
                            ProviderPayload(
                                "table_body", None, "<table><td>A</td></table>"
                            ),
                        ),
                        annotation="table",
                    ),
                ),
            ),
            segments=(
                _segment(0, 0, "retained"),
                _segment(0, 1, "retained"),
            ),
        )

        result = build_provider_units(_admitted(document))
        draft = result.units[0]

        self.assertEqual(draft.payload_kind, "table")
        self.assertEqual(draft.quality_status, "needs_review")
        self.assertEqual(len(draft.locator.unbound_table_parts), 1)
        self.assertEqual(
            [
                part.part.physical_segment_index
                for part in result.unassigned_table_parts
            ],
            [0, 1],
        )
        self.assertEqual(draft.locator.parts[0].physical_table_segment_indices, ())

    def test_improbable_ascii_glyph_map_marks_unit_for_review_without_rewriting(
        self,
    ) -> None:
        damaged = r"""!"#\$%&'()\* ,-./0123456%&'()\* 789:9;<=>?,@ABCDEFGHI"""
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, damaged),),
                        annotation="title",
                        level=2,
                    ),
                ),
            ),
            segments=(),
        )

        draft = build_provider_units(_admitted(document)).units[0]

        self.assertEqual(draft.quality_status, "needs_review")
        self.assertEqual(draft.title, damaged)
        self.assertEqual(draft.heading_path, (damaged,))

    def test_ordinary_english_and_code_titles_do_not_trigger_glyph_review(self) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "EUSA Pharma / BGB-11417"),),
                        annotation="title",
                        level=2,
                    ),
                ),
            ),
            segments=(),
        )

        draft = build_provider_units(_admitted(document)).units[0]

        self.assertEqual(draft.quality_status, "ok")

    def test_markup_only_non_cjk_title_is_reviewed_without_rewriting(self) -> None:
        damaged = "<sup>®</sup> BTK"
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, damaged),),
                        annotation="title",
                        level=2,
                    ),
                ),
            ),
            segments=(),
        )

        draft = build_provider_units(_admitted(document)).units[0]

        self.assertEqual(draft.quality_status, "needs_review")
        self.assertEqual(draft.title, damaged)
        self.assertEqual(draft.heading_path, (damaged,))

    def test_markup_title_with_visible_chinese_is_not_flagged(self) -> None:
        intact = "百悦泽<sup>®</sup>"
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, intact),),
                        annotation="title",
                        level=2,
                    ),
                ),
            ),
            segments=(),
        )

        draft = build_provider_units(_admitted(document)).units[0]

        self.assertEqual(draft.quality_status, "ok")
        self.assertEqual(draft.title, intact)

    def test_payload_tampering_breaks_exact_search_replay(self) -> None:
        admitted = _admitted(_representative_document())
        draft = build_provider_units(admitted).units[1]
        payload = dict(draft.payload)
        parts = [dict(part) for part in cast(list[dict[str, object]], payload["parts"])]
        parts[0]["text"] = "伪造正文"
        payload["parts"] = parts
        tampered = replace(draft, payload=payload)
        body_binding = next(
            binding
            for binding in draft.locator.search_targets
            if binding.source.source_index == 2
        )

        with self.assertRaisesRegex(ValueError, "differs from its source"):
            replay_provider_unit_search_binding(admitted, tampered, body_binding)

    def test_search_replay_rejects_wrong_document_and_cross_part_binding(self) -> None:
        admitted = _admitted(_identical_text_parts_document())
        draft = build_provider_units(admitted).units[0]
        first, second = draft.locator.search_targets

        wrong_document = replace(
            draft,
            locator=replace(
                draft.locator,
                provider_document_sha256="sha256:" + "b" * 64,
            ),
        )
        with self.assertRaisesRegex(ValueError, "different document"):
            replay_provider_unit_search_binding(admitted, wrong_document, first)

        forged_binding = replace(first, destination=second.destination)
        forged = replace(
            draft,
            locator=replace(
                draft.locator,
                search_targets=(forged_binding, second),
            ),
        )
        with self.assertRaisesRegex(ValueError, "not owned by its mixed part"):
            replay_provider_unit_search_binding(admitted, forged, forged_binding)

    def test_search_replay_rejects_equal_text_from_a_different_field(self) -> None:
        admitted = _admitted(_table_with_equal_caption_and_footnote())
        draft = build_provider_units(admitted).units[0]
        caption, footnote = draft.locator.search_targets
        forged_binding = replace(caption, destination=footnote.destination)
        forged = replace(
            draft,
            locator=replace(
                draft.locator,
                search_targets=(forged_binding, footnote),
            ),
        )

        with self.assertRaisesRegex(ValueError, "differs from its source field"):
            replay_provider_unit_search_binding(admitted, forged, forged_binding)


def _representative_document() -> ProviderDocument:
    image_role = "image_0001"
    return _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "header",
                    (ProviderPayload("text", None, "页眉"),),
                    annotation="page_header",
                ),
                _block(
                    1,
                    0,
                    "text",
                    (ProviderPayload("text", None, "第一章 标题"),),
                    annotation="title",
                    level=2,
                ),
                _block(
                    2,
                    0,
                    "text",
                    (ProviderPayload("text", None, "正文"),),
                    annotation="paragraph",
                ),
                _block(
                    3,
                    0,
                    "table",
                    (
                        ProviderPayload(
                            "table_body", None, "<table><td>甲</td></table>"
                        ),
                        ProviderPayload("table_caption", 0, "表一"),
                    ),
                    annotation="table",
                ),
            ),
            (
                _block(
                    4,
                    1,
                    "header",
                    (ProviderPayload("text", None, "页眉"),),
                    annotation="page_header",
                ),
                _block(5, 1, "table", (), annotation="table"),
                _block(
                    6,
                    1,
                    "image",
                    (ProviderPayload("content", None, ""),),
                    annotation="image",
                    artifact_roles=(image_role,),
                ),
                _block(
                    7,
                    1,
                    "text",
                    (ProviderPayload("text", None, "□适用 \uf052不适用"),),
                    annotation="title",
                    level=2,
                ),
            ),
        ),
        segments=(
            _segment(0, 0, "retained"),
            _segment(1, 0, "deleted"),
        ),
        extra_artifacts=(
            ProviderArtifact(
                role=image_role,
                relative_path="e_images/figure.jpg",
                sha256="sha256:" + "f" * 64,
                size_bytes=321,
                media_type="image/jpeg",
            ),
        ),
    )


def _visual_only_document(digest: str) -> ProviderDocument:
    artifact = ProviderArtifact(
        role="image_0001",
        relative_path="e_images/figure.jpg",
        sha256="sha256:" + digest * 64,
        size_bytes=321,
        media_type="image/jpeg",
    )
    return _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "image",
                    (ProviderPayload("content", None, ""),),
                    annotation="image",
                    artifact_roles=(artifact.role,),
                ),
            ),
        ),
        segments=(),
        extra_artifacts=(artifact,),
    )


def _table_visual_only_document(digest: str) -> ProviderDocument:
    artifact = ProviderArtifact(
        role="table_crop_0001",
        relative_path="e_images/table.jpg",
        sha256="sha256:" + digest * 64,
        size_bytes=321,
        media_type="image/jpeg",
    )
    return _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "table",
                    (
                        ProviderPayload("table_body", None, ""),
                        ProviderPayload("table_caption", 0, "只有表注"),
                    ),
                    annotation="table",
                ),
            ),
        ),
        segments=(
            _segment(
                0,
                0,
                "retained",
                crop_artifact_role=artifact.role,
            ),
        ),
        extra_artifacts=(artifact,),
    )


def _table_with_body_and_crop_document(digest: str) -> ProviderDocument:
    artifact = ProviderArtifact(
        role="table_crop_0001",
        relative_path="e_images/table.jpg",
        sha256="sha256:" + digest * 64,
        size_bytes=321,
        media_type="image/jpeg",
    )
    return _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "table",
                    (
                        ProviderPayload(
                            "table_body",
                            None,
                            "<table><td>正文</td></table>",
                        ),
                    ),
                    annotation="table",
                ),
            ),
        ),
        segments=(
            _segment(
                0,
                0,
                "retained",
                crop_artifact_role=artifact.role,
            ),
        ),
        extra_artifacts=(artifact,),
    )


def _identical_text_parts_document() -> ProviderDocument:
    return _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "text",
                    (ProviderPayload("text", None, "相同正文"),),
                    annotation="paragraph",
                ),
                _block(
                    1,
                    0,
                    "text",
                    (ProviderPayload("text", None, "相同正文"),),
                    annotation="paragraph",
                ),
            ),
        ),
        segments=(),
    )


def _table_with_equal_caption_and_footnote() -> ProviderDocument:
    return _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "table",
                    (
                        ProviderPayload("table_caption", 0, "相同文字"),
                        ProviderPayload("table_footnote", 0, "相同文字"),
                    ),
                    annotation="table",
                ),
            ),
        ),
        segments=(_segment(0, 0, "retained"),),
    )


def _document(
    *,
    pages: tuple[tuple[ProviderBlock, ...], ...],
    segments: tuple[ProviderPhysicalTableSegment, ...],
    extra_artifacts: tuple[ProviderArtifact, ...] = (),
) -> ProviderDocument:
    provider_pages = tuple(
        ProviderPage(
            page_index=page_index,
            page_size=(600.0, 800.0),
            blocks=tuple(
                replace(block, order_in_page=order)
                for order, block in enumerate(blocks)
            ),
        )
        for page_index, blocks in enumerate(pages)
    )
    artifacts = tuple(
        sorted(
            (*_required_artifacts(), *extra_artifacts),
            key=lambda item: item.relative_path,
        )
    )
    return ProviderDocument(
        source_pdf_sha256=_SOURCE_SHA,
        parser_version="3.4.4",
        backend="hybrid",
        effort="medium",
        ocr_enabled=False,
        pages=provider_pages,
        physical_table_segments=segments,
        artifacts=artifacts,
        bundle_sha256=provider_artifact_bundle_sha256(artifacts),
    )


def _block(
    source_index: int,
    page_index: int,
    provider_type: str,
    payloads: tuple[ProviderPayload, ...],
    *,
    annotation: str | None,
    level: int | None = None,
    artifact_roles: tuple[str, ...] = (),
) -> ProviderBlock:
    raw = json.dumps(
        {
            "page_idx": page_index,
            "source_index": source_index,
            "type": provider_type,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return ProviderBlock(
        source_index=source_index,
        page_index=page_index,
        order_in_page=0,
        provider_type=provider_type,
        typed_annotation=annotation,
        provider_level=level,
        bbox=None,
        payloads=payloads,
        referenced_artifact_roles=artifact_roles,
        raw_item_json=raw,
        raw_item_sha256=_sha_text(raw),
    )


def _segment(
    page_index: int,
    order_in_page: int,
    status: str,
    *,
    crop_artifact_role: str | None = None,
) -> ProviderPhysicalTableSegment:
    raw = json.dumps(
        {"index": order_in_page, "page": page_index, "type": "table"},
        sort_keys=True,
        separators=(",", ":"),
    )
    return ProviderPhysicalTableSegment(
        page_index=page_index,
        order_in_page=order_in_page,
        provider_index=order_in_page,
        bbox=None,
        page_local_html=f"<table><td>{page_index}:{order_in_page}</td></table>",
        crop_artifact_role=crop_artifact_role,
        logical_stream_status=status,  # type: ignore[arg-type]
        raw_segment_json=raw,
        raw_segment_sha256=_sha_text(raw),
    )


def _required_artifacts() -> tuple[ProviderArtifact, ...]:
    return tuple(
        ProviderArtifact(
            role=role,
            relative_path=relative_path,
            sha256="sha256:" + digest * 64,
            size_bytes=128,
            media_type="application/json",
        )
        for role, relative_path, digest in (
            ("content_list", "a_content_list.json", "b"),
            ("content_list_v2", "b_content_list_v2.json", "c"),
            ("middle_json", "c_middle.json", "d"),
            ("model_json", "d_model.json", "e"),
        )
    )


def _admitted(document: ProviderDocument) -> AdmittedProviderDocument:
    envelope = ProviderDocumentEnvelope.build(
        document_id=_DOCUMENT,
        artifact_owner_processing_run_id=_OWNER,
        provider="cninfo",
        provider_document_id="1225087169",
        source_pdf_relpath=(
            f"raw_documents/cninfo/000001/2026/1225087169/sha256_{'a' * 64}.pdf"
        ),
        source_pdf_page_count=len(document.pages),
        parser_artifact_root_relpath=(
            "parser_artifacts/cninfo/000001/1225087169/"
            f"{_OWNER}/sha256_{'a' * 64}/hybrid_auto"
        ),
        parser_target_identity=_target(),
        provider_document=document,
    )
    record = provider_document_envelope_to_bytes(envelope)
    return AdmittedProviderDocument(
        provider_document_relpath=Path(
            "derived/provider_documents/cninfo/000001/1225087169/"
            f"{_OWNER}/provider_document.v1.json"
        ),
        provider_document_sha256=_sha_bytes(record),
        envelope=envelope,
    )


def _target() -> ParserTargetIdentity:
    return ParserTargetIdentity(
        backend="hybrid-http-client",
        effort="medium",
        formula=True,
        full_pdf=True,
        image_analysis=False,
        language="ch",
        method="auto",
        name="MinerU",
        package_version="3.4.4",
        runtime_bundle_identity_sha256="sha256:" + "c" * 64,
        table=True,
    )


def _sha_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


if __name__ == "__main__":
    unittest.main()
