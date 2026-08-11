from __future__ import annotations

from dataclasses import replace
import unittest

from disclosure_anchor.application.contracts.provider_document import (
    ProviderBlock,
    ProviderDocument,
    ProviderPage,
    ProviderPayload,
    ProviderPhysicalTableSegment,
    provider_artifact_bundle_sha256,
)
from disclosure_anchor.application.contracts.html_visible_text import (
    html_visible_text_segments,
)
from disclosure_anchor.application.services.document_outline import build_document_outline
from disclosure_anchor.application.services.provider_table_projection import (
    build_provider_table_projection,
)
from disclosure_anchor.application.services.retrieval_primary import (
    build_retrieval_primary_projection,
    replay_retrieval_target,
)
from disclosure_anchor.application.contracts.retrieval_primary import (
    BlockRetrievalSelection,
)


_SOURCE_SHA = "sha256:" + "a" * 64
_RAW_SHA = "sha256:" + "b" * 64


class RetrievalPrimaryTest(unittest.TestCase):
    def test_explicit_payloads_are_selected_once_and_stubs_remain_evidence(self) -> None:
        document = _document()
        outline = build_document_outline(document)
        tables = build_provider_table_projection(document)

        projection = build_retrieval_primary_projection(document, outline, tables)

        self.assertEqual(
            [(block.source_index, block.disposition, block.reason) for block in projection.blocks],
            [
                (0, "evidence_only", "page_furniture"),
                (1, "primary", "searchable_payload"),
                (2, "primary", "searchable_payload"),
                (3, "evidence_only", "empty_provider_carrier"),
                (4, "primary", "searchable_payload"),
                (5, "primary", "searchable_payload"),
            ],
        )
        self.assertEqual(len(projection.targets), 5)
        table_target = next(target for target in projection.targets if target.field == "table_body")
        self.assertEqual(
            replay_retrieval_target(document, table_target),
            ("甲", "乙"),
        )
        self.assertEqual(projection.logical_table_count, 1)
        owner_unit = next(
            unit for unit in projection.units if unit.logical_table_indices
        )
        self.assertEqual(owner_unit.logical_table_indices, (0,))

    def test_same_text_on_distinct_blocks_is_never_globally_deduplicated(self) -> None:
        document = _document()
        outline = build_document_outline(document)
        tables = build_provider_table_projection(document)

        projection = build_retrieval_primary_projection(document, outline, tables)

        duplicate_targets = [
            target
            for target in projection.targets
            if replay_retrieval_target(document, target) == ("单位：元",)
        ]
        self.assertEqual([target.source_index for target in duplicate_targets], [4, 5])
        self.assertNotEqual(duplicate_targets[0].target_id, duplicate_targets[1].target_id)

    def test_nested_table_text_is_emitted_once_in_source_order(self) -> None:
        self.assertEqual(
            html_visible_text_segments(
                "<table><tr><td>外前<table><tr><td>内</td></tr></table>外后</td>"
                "<td>末</td></tr></table>"
            ),
            ("外前", "内", "外后", "末"),
        )

    def test_unknown_payload_field_and_stale_identity_fail_closed(self) -> None:
        document = _document()
        with self.assertRaisesRegex(ValueError, "not valid"):
            replace(
                document.blocks[1],
                payloads=(ProviderPayload("future_field", None, "内容"),),
            )

        outline = build_document_outline(document)
        tables = build_provider_table_projection(document)
        stale_tables = replace(
            tables,
            source_pdf_sha256="sha256:" + "c" * 64,
        )
        with self.assertRaisesRegex(ValueError, "table projection"):
            build_retrieval_primary_projection(document, outline, stale_tables)

        projection = build_retrieval_primary_projection(document, outline, tables)
        stale_target = replace(
            projection.targets[0],
            raw_block_sha256="sha256:" + "c" * 64,
        )
        with self.assertRaisesRegex(ValueError, "hash drifted"):
            replay_retrieval_target(document, stale_target)

    def test_metadata_title_has_no_input_channel_into_projection(self) -> None:
        document = _document()
        outline = build_document_outline(document)
        projection = build_retrieval_primary_projection(
            document,
            outline,
            build_provider_table_projection(document),
        )

        values = [
            value
            for target in projection.targets
            for value in replay_retrieval_target(document, target)
        ]
        self.assertNotIn("登记元数据标题", values)
        self.assertTrue(all("document_title" not in target.field for target in projection.targets))

    def test_unknown_block_disposition_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "disposition is unsupported"):
            BlockRetrievalSelection(
                source_index=0,
                raw_block_sha256=_RAW_SHA,
                disposition="unknown",  # type: ignore[arg-type]
                reason="page_furniture",
                target_ids=(),
            )

    def test_duplicate_payload_target_and_reordered_table_ownership_fail_closed(
        self,
    ) -> None:
        document = _document()
        projection = build_retrieval_primary_projection(
            document,
            build_document_outline(document),
            build_provider_table_projection(document),
        )
        first = projection.targets[0]
        duplicate = replace(first, target_id=first.target_id + ":duplicate")
        duplicated_blocks = (
            projection.blocks[0],
            replace(
                projection.blocks[1],
                target_ids=(first.target_id, duplicate.target_id),
            ),
            *projection.blocks[2:],
        )
        duplicated_units = (
            projection.units[0],
            replace(
                projection.units[1],
                target_ids=(
                    first.target_id,
                    duplicate.target_id,
                    *projection.units[1].target_ids[1:],
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "multiple retrieval targets"):
            replace(
                projection,
                blocks=duplicated_blocks,
                targets=(first, duplicate, *projection.targets[1:]),
                units=duplicated_units,
            )

        with self.assertRaisesRegex(ValueError, "provider order"):
            replace(
                projection,
                logical_table_count=2,
                units=(
                    replace(projection.units[0], logical_table_indices=(1,)),
                    replace(projection.units[1], logical_table_indices=(0,)),
                ),
            )


def _document() -> ProviderDocument:
    pages = (
        ProviderPage(
            page_index=0,
            page_size=(600.0, 800.0),
            blocks=(
                _block(0, 0, 0, "header", (ProviderPayload("text", None, "证券代码"),), annotation="page_header"),
                _block(1, 0, 1, "text", (ProviderPayload("text", None, "第一章 标题"),), annotation="title", level=2),
                _block(
                    2,
                    0,
                    2,
                    "table",
                    (
                        ProviderPayload("table_body", None, "<table><tr><td>甲</td><td>乙</td></tr></table>"),
                        ProviderPayload("table_caption", 0, "表一"),
                    ),
                    annotation="table",
                ),
            ),
        ),
        ProviderPage(
            page_index=1,
            page_size=(600.0, 800.0),
            blocks=(
                _block(3, 1, 0, "table", (), annotation="table"),
                _block(4, 1, 1, "text", (ProviderPayload("text", None, "单位：元"),), annotation="paragraph"),
                _block(5, 1, 2, "text", (ProviderPayload("text", None, "单位：元"),), annotation="paragraph"),
            ),
        ),
    )
    segments = (
        _segment(0, "retained"),
        _segment(1, "deleted"),
    )
    return ProviderDocument(
        source_pdf_sha256=_SOURCE_SHA,
        parser_version="3.4.4",
        backend="hybrid",
        effort="medium",
        ocr_enabled=False,
        pages=pages,
        physical_table_segments=segments,
        artifacts=(),
        bundle_sha256=provider_artifact_bundle_sha256(()),
    )


def _block(
    source_index: int,
    page_index: int,
    order_in_page: int,
    provider_type: str,
    payloads: tuple[ProviderPayload, ...],
    *,
    annotation: str | None,
    level: int | None = None,
) -> ProviderBlock:
    return ProviderBlock(
        source_index=source_index,
        page_index=page_index,
        order_in_page=order_in_page,
        provider_type=provider_type,
        typed_annotation=annotation,
        provider_level=level,
        bbox=None,
        payloads=payloads,
        referenced_artifact_roles=(),
        raw_item_json=f'{{"source":{source_index}}}',
        raw_item_sha256=_RAW_SHA,
    )


def _segment(page_index: int, status: str) -> ProviderPhysicalTableSegment:
    return ProviderPhysicalTableSegment(
        page_index=page_index,
        order_in_page=0,
        provider_index=0,
        bbox=None,
        page_local_html=f"<table><td>page-{page_index}</td></table>",
        crop_artifact_role=None,
        logical_stream_status=status,  # type: ignore[arg-type]
        raw_segment_json=f'{{"page":{page_index}}}',
        raw_segment_sha256="sha256:" + f"{page_index + 1:064x}",
    )


if __name__ == "__main__":
    unittest.main()
