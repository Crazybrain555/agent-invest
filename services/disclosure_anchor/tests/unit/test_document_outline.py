from __future__ import annotations

from dataclasses import replace
import unittest

from disclosure_anchor.application.contracts.document_outline import (
    HeadingHintSource,
    HeadingLevelHint,
    HeadingNegativeHint,
    HeadingNegativeReason,
)
from disclosure_anchor.application.contracts.provider_document import (
    ProviderBBox,
    ProviderBlock,
    ProviderDocument,
    ProviderPage,
    ProviderPayload,
    ProviderPhysicalTableSegment,
)
from disclosure_anchor.application.services.document_outline import (
    build_document_outline,
)


_SHA = "sha256:" + "a" * 64
_BUNDLE_SHA = "sha256:" + "b" * 64


class DocumentOutlineTest(unittest.TestCase):
    def test_v2_paragraph_blocks_provider_level_fallback(self) -> None:
        document = _document(
            _block(0, "普通正文", annotation="paragraph", level=1),
            _block(1, "第一章 真标题", annotation="title", level=2),
            _block(2, "   ", annotation="title", level=2),
        )

        outline = build_document_outline(document)

        self.assertEqual([item.source_index for item in outline.candidates], [1])
        self.assertEqual([item.text for item in outline.headings], ["第一章 真标题"])
        self.assertEqual(outline.units[0].block_source_indices, (0,))
        self.assertEqual(outline.units[1].block_source_indices, (1, 2))

    def test_source_bound_hint_can_admit_provider_missed_title(self) -> None:
        document = _document(
            _block(0, "文档总标题", annotation="paragraph"),
            _block(1, "正文", annotation="paragraph"),
        )

        outline = build_document_outline(
            document,
            level_hints=(_level_hint(document, 0, "bookmark", 1),),
        )

        self.assertEqual(len(outline.headings), 1)
        self.assertEqual(outline.headings[0].placement_source, "bookmark")
        self.assertEqual(outline.units[0].block_source_indices, (0, 1))

    def test_style_hint_cannot_create_a_title_and_outline_hint_has_priority(
        self,
    ) -> None:
        document = _document(
            _block(0, "普通正文", annotation="paragraph"),
            _block(1, "已识别标题", annotation="title", level=4),
        )

        outline = build_document_outline(
            document,
            level_hints=(
                _level_hint(document, 0, "pdf_style", 1),
                _level_hint(document, 1, "pdf_style", 3),
                _level_hint(document, 1, "printed_toc", 2),
                _level_hint(document, 1, "bookmark", 1),
            ),
        )

        self.assertEqual([item.source_index for item in outline.candidates], [1])
        self.assertEqual(outline.headings[0].placement_source, "bookmark")
        self.assertEqual(outline.headings[0].nominal_rank, 1)

    def test_hard_negatives_demote_but_preserve_every_block(self) -> None:
        title_in_table = _block(
            0,
            "一、表内粗体小计",
            level=2,
            bbox=ProviderBBox(120, 120, 400, 160),
        )
        checkbox = _block(
            1,
            "□适用 \uf052不适用",
            level=2,
            bbox=ProviderBBox(100, 820, 900, 850),
        )
        continuation = _block(
            2,
            "跨页续句",
            level=2,
            bbox=ProviderBBox(100, 860, 900, 890),
        )
        math_title = _block(
            3,
            "√2 与 √3 的比较",
            level=2,
            bbox=ProviderBBox(100, 900, 900, 940),
        )
        body = _block(4, "完整正文", annotation="paragraph")
        document = _document(
            title_in_table,
            checkbox,
            continuation,
            math_title,
            body,
            segments=(
                ProviderPhysicalTableSegment(
                    page_index=0,
                    order_in_page=0,
                    provider_index=0,
                    bbox=ProviderBBox(100, 100, 900, 800),
                    page_local_html="<table></table>",
                    crop_artifact_role=None,
                    logical_stream_status="retained",
                    cell_merge_json=None,
                    raw_segment_json="{}",
                    raw_segment_sha256=_SHA,
                ),
            ),
        )

        outline = build_document_outline(
            document,
            negative_hints=(_negative_hint(document, 2, "page_continuation"),),
        )

        self.assertEqual(
            [item.disposition for item in outline.candidates],
            ["demoted", "demoted", "demoted", "accepted"],
        )
        self.assertEqual(
            [item.disposition_reason for item in outline.candidates],
            ["table_contained", "checkbox_selector", "page_continuation", "accepted"],
        )
        self.assertEqual([heading.source_index for heading in outline.headings], [3])
        self.assertEqual(outline.units[0].block_source_indices, (0, 1, 2))
        self.assertEqual(outline.units[1].block_source_indices, (3, 4))

    def test_no_heading_has_one_preamble_and_empty_stub_is_not_dropped(self) -> None:
        document = _document(
            _block(0, "正文", annotation="paragraph"),
            _block(1, "", provider_type="table", annotation="table"),
        )

        first = build_document_outline(document)
        second = build_document_outline(document)

        self.assertEqual(first, second)
        self.assertEqual(first.headings, ())
        self.assertEqual(len(first.units), 1)
        self.assertEqual(first.units[0].block_source_indices, (0, 1))

    def test_duplicate_heading_text_and_private_use_glyph_remain_distinct(self) -> None:
        document = _document(
            _block(0, "一、标题\uf052", level=2),
            _block(1, "一、标题\uf052", level=2),
        )

        outline = build_document_outline(document)

        self.assertEqual(len(outline.headings), 2)
        self.assertNotEqual(
            outline.headings[0].heading_id, outline.headings[1].heading_id
        )
        self.assertEqual(outline.headings[0].text, outline.headings[1].text)
        self.assertIn("\uf052", outline.headings[0].text)

    def test_invalid_duplicate_or_stale_hints_fail_closed(self) -> None:
        document = _document(_block(0, "标题", annotation="paragraph"))

        with self.assertRaises(ValueError):
            HeadingNegativeHint(_SHA, 0, _SHA, "unknown")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            build_document_outline(
                document,
                level_hints=(HeadingLevelHint(_SHA, 4, _SHA, "bookmark", 1),),
            )
        with self.assertRaises(ValueError):
            build_document_outline(
                document,
                level_hints=(
                    _level_hint(document, 0, "bookmark", 1),
                    _level_hint(document, 0, "bookmark", 2),
                ),
            )
        with self.assertRaises(ValueError):
            build_document_outline(
                document,
                level_hints=(
                    HeadingLevelHint(
                        "sha256:" + "c" * 64,
                        0,
                        document.blocks[0].raw_item_sha256,
                        "bookmark",
                        1,
                    ),
                ),
            )
        with self.assertRaises(ValueError):
            build_document_outline(
                document,
                level_hints=(
                    HeadingLevelHint(
                        document.source_pdf_sha256,
                        0,
                        "sha256:" + "c" * 64,
                        "bookmark",
                        1,
                    ),
                ),
            )

    def test_common_numbering_families_form_a_nested_stack(self) -> None:
        document = _document(
            _block(0, "第一章 总则", level=2),
            _block(1, "第一节 范围", level=2),
            _block(2, "一、境内", level=2),
            _block(3, "（一）业务", level=2),
            _block(4, "1、事项", level=2),
            _block(5, "（1）说明", level=2),
            _block(6, "2、另一事项", level=2),
            _block(7, "二、其他", level=2),
            _block(8, "第二章 附则", level=2),
        )

        outline = build_document_outline(
            document,
            level_hints=(_level_hint(document, 4, "pdf_style", 1),),
        )

        self.assertEqual(
            [heading.nominal_rank for heading in outline.headings],
            [1, 2, 3, 4, 5, 6, 5, 3, 1],
        )
        self.assertEqual(
            [heading.level for heading in outline.headings],
            [1, 2, 3, 4, 5, 6, 5, 3, 1],
        )
        self.assertEqual(outline.headings[4].placement_source, "numbering")
        self.assertEqual(
            outline.headings[7].parent_heading_id, outline.headings[1].heading_id
        )
        self.assertIsNone(outline.headings[8].parent_heading_id)

    def test_provider_only_title_is_a_leaf_and_does_not_capture_numbered_sibling(
        self,
    ) -> None:
        document = _document(
            _block(0, "第一章 总则", level=2),
            _block(1, "未经结构证明的粗体说明", level=2),
            _block(2, "一、范围", level=2),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            outline.headings[1].parent_heading_id, outline.headings[0].heading_id
        )
        self.assertEqual(
            outline.headings[2].parent_heading_id, outline.headings[0].heading_id
        )
        self.assertNotEqual(
            outline.headings[2].parent_heading_id, outline.headings[1].heading_id
        )

    def test_outline_record_rejects_missing_or_inconsistent_resolved_heading(
        self,
    ) -> None:
        outline = build_document_outline(
            _document(
                _block(0, "第一章 总则", level=2),
                _block(1, "一、范围", level=2),
            )
        )

        with self.assertRaises(ValueError):
            replace(outline, headings=outline.headings[:1])
        with self.assertRaises(ValueError):
            replace(
                outline,
                headings=(
                    outline.headings[0],
                    replace(outline.headings[1], level=1),
                ),
            )
        forged_heading = replace(
            outline.headings[1],
            text="伪造标题",
            headpath=(*outline.headings[0].headpath, "伪造标题"),
        )
        forged_unit = replace(
            outline.units[1],
            title="伪造标题",
            headpath=forged_heading.headpath,
        )
        with self.assertRaises(ValueError):
            replace(
                outline,
                headings=(outline.headings[0], forged_heading),
                units=(outline.units[0], forged_unit),
            )
        with self.assertRaises(ValueError):
            replace(
                outline,
                units=(
                    replace(
                        outline.units[0],
                        block_source_indices=(0, 1),
                    ),
                ),
            )


def _block(
    source_index: int,
    text: str,
    *,
    annotation: str | None = None,
    level: int | None = None,
    provider_type: str = "text",
    bbox: ProviderBBox | None = None,
) -> ProviderBlock:
    return ProviderBlock(
        source_index=source_index,
        page_index=0,
        order_in_page=source_index,
        provider_type=provider_type,
        typed_annotation=annotation,
        provider_level=level,
        bbox=bbox
        or ProviderBBox(100, 100 + source_index * 50, 900, 140 + source_index * 50),
        payloads=() if not text else (ProviderPayload("text", None, text),),
        referenced_artifact_roles=(),
        raw_item_json="{}",
        raw_item_sha256=_SHA,
    )


def _document(
    *blocks: ProviderBlock,
    segments: tuple[ProviderPhysicalTableSegment, ...] = (),
) -> ProviderDocument:
    page = ProviderPage(
        page_index=0,
        page_size=(600.0, 800.0),
        blocks=tuple(
            replace(block, order_in_page=order) for order, block in enumerate(blocks)
        ),
    )
    return ProviderDocument(
        source_pdf_sha256=_SHA,
        parser_version="3.4.4",
        backend="hybrid",
        effort="medium",
        ocr_enabled=False,
        pages=(page,),
        physical_table_segments=segments,
        artifacts=(),
        bundle_sha256=_BUNDLE_SHA,
    )


def _level_hint(
    document: ProviderDocument,
    source_index: int,
    source: HeadingHintSource,
    level: int,
) -> HeadingLevelHint:
    return HeadingLevelHint(
        document.source_pdf_sha256,
        source_index,
        document.blocks[source_index].raw_item_sha256,
        source,
        level,
    )


def _negative_hint(
    document: ProviderDocument,
    source_index: int,
    reason: HeadingNegativeReason,
) -> HeadingNegativeHint:
    return HeadingNegativeHint(
        document.source_pdf_sha256,
        source_index,
        document.blocks[source_index].raw_item_sha256,
        reason,
    )


if __name__ == "__main__":
    unittest.main()
