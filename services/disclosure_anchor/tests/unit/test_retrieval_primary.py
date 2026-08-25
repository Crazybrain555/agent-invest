from __future__ import annotations

from dataclasses import replace
import unittest

from disclosure_anchor.application.contracts.provider_document import (
    ProviderArtifact,
    ProviderBlock,
    ProviderDocument,
    ProviderPage,
    ProviderPayload,
    ProviderPhysicalTableSegment,
    provider_artifact_bundle_sha256,
)
from disclosure_anchor.application.contracts.html_visible_text import (
    html_qa_row_atoms,
    html_table_semantic_segments,
    html_visible_text_segments,
)
from disclosure_anchor.application.services.document_outline import (
    build_document_outline,
)
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
    def test_explicit_payloads_are_selected_once_and_stubs_remain_evidence(
        self,
    ) -> None:
        document = _document()
        outline = build_document_outline(document)
        tables = build_provider_table_projection(document)

        projection = build_retrieval_primary_projection(document, outline, tables)

        self.assertEqual(
            [
                (block.source_index, block.disposition, block.reason)
                for block in projection.blocks
            ],
            [
                (0, "primary", "searchable_payload"),
                (1, "primary", "searchable_payload"),
                (2, "primary", "searchable_payload"),
                (3, "evidence_only", "empty_provider_carrier"),
                (4, "primary", "searchable_payload"),
                (5, "primary", "searchable_payload"),
            ],
        )
        self.assertEqual(len(projection.targets), 6)
        table_target = next(
            target for target in projection.targets if target.field == "table_body"
        )
        self.assertEqual(
            replay_retrieval_target(document, table_target),
            ("甲", "乙"),
        )
        self.assertEqual(projection.logical_table_count, 1)
        owner_unit = next(
            unit for unit in projection.units if unit.logical_table_indices
        )
        self.assertEqual(owner_unit.logical_table_indices, (0,))

    def test_only_repeated_headers_and_page_numbers_are_semantic_furniture(
        self,
    ) -> None:
        document = _furniture_document()

        projection = build_retrieval_primary_projection(
            document,
            build_document_outline(document),
            build_provider_table_projection(document),
        )

        self.assertEqual(
            [
                (item.source_index, item.disposition, item.reason)
                for item in projection.blocks
            ],
            [
                (0, "evidence_only", "page_furniture"),
                (1, "primary", "searchable_payload"),
                (2, "evidence_only", "page_furniture"),
                (3, "evidence_only", "page_furniture"),
                (4, "primary", "searchable_payload"),
                (5, "evidence_only", "page_furniture"),
            ],
        )
        values = [
            value
            for target in projection.targets
            for value in replay_retrieval_target(document, target)
        ]
        self.assertEqual(values, ["证券代码：000001", "特此公告。"])

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
        self.assertNotEqual(
            duplicate_targets[0].target_id, duplicate_targets[1].target_id
        )

    def test_visual_without_text_remains_distinct_from_an_empty_carrier(self) -> None:
        artifact = ProviderArtifact(
            role="image_0001",
            relative_path="images/figure.png",
            sha256="sha256:" + "c" * 64,
            size_bytes=17,
            media_type="image/png",
        )
        block = _block(
            0,
            0,
            0,
            "image",
            (ProviderPayload("content", None, ""),),
            annotation="image",
            artifact_roles=(artifact.role,),
        )
        document = ProviderDocument(
            source_pdf_sha256=_SOURCE_SHA,
            parser_version="3.4.4",
            backend="hybrid",
            effort="medium",
            ocr_enabled=False,
            pages=(ProviderPage(0, (600.0, 800.0), (block,)),),
            physical_table_segments=(),
            artifacts=(artifact,),
            bundle_sha256=provider_artifact_bundle_sha256((artifact,)),
        )

        projection = build_retrieval_primary_projection(
            document,
            build_document_outline(document),
            build_provider_table_projection(document),
        )

        self.assertEqual(projection.targets, ())
        self.assertEqual(projection.blocks[0].reason, "visual_without_text")

    def test_nested_table_text_is_emitted_once_in_source_order(self) -> None:
        self.assertEqual(
            html_visible_text_segments(
                "<table><tr><td>外前<table><tr><td>内</td></tr></table>外后</td>"
                "<td>末</td></tr></table>"
            ),
            ("外前", "内", "外后", "末"),
        )
        self.assertEqual(
            html_visible_text_segments(
                "<p>前言</p><table><td>甲</td></table><p>后记</p>"
            ),
            ("前言", "甲", "后记"),
        )
        self.assertEqual(
            html_visible_text_segments(
                "<template><table><td>隐藏</td></table></template>可见"
            ),
            ("可见",),
        )

    def test_table_semantic_roles_require_closed_row_structure(self) -> None:
        form = html_table_semantic_segments(
            "<table><tr><td>资金来源</td><td>自有资金</td></tr>"
            "<tr><td>实施期限</td><td>三个月</td></tr></table>"
        )
        self.assertEqual(
            tuple(item.role for item in form),
            (
                "table_field_label",
                "table_text",
                "table_field_label",
                "table_text",
            ),
        )

        grid = html_table_semantic_segments(
            "<table><tr><td>激励对象姓名</td><td>职务</td><td>数量</td></tr>"
            "<tr><td>张三</td><td>董事</td><td>1000</td></tr></table>"
        )
        self.assertEqual(
            tuple(item.role for item in grid),
            (
                "table_column_header",
                "table_column_header",
                "table_column_header",
                "table_text",
                "table_text",
                "table_text",
            ),
        )

        titled_grid = html_table_semantic_segments(
            "<table>"
            '<tr><td colspan="3">交流要点</td></tr>'
            "<tr><td>序号</td><td>提问内容</td><td>回复内容</td></tr>"
            "<tr><td>1</td><td>经营情况？</td><td>经营正常。</td></tr>"
            "</table>"
        )
        self.assertEqual(
            tuple((item.text, item.role) for item in titled_grid),
            (
                ("交流要点", "table_text"),
                ("序号", "table_column_header"),
                ("提问内容", "table_column_header"),
                ("回复内容", "table_column_header"),
                ("1", "table_text"),
                ("经营情况？", "table_text"),
                ("经营正常。", "table_text"),
            ),
        )

        self.assertEqual(
            html_table_semantic_segments(
                "<table><tr><td>外<table><tr><td>内</td></tr></table></td>"
                "</tr></table>"
            ),
            (),
        )
        one_row = html_table_semantic_segments(
            "<table><tr><td>标的公司</td><td>3000</td></tr></table>"
        )
        self.assertEqual(
            tuple(item.role for item in one_row),
            ("table_text", "table_text"),
        )

    def test_table_semantic_roles_preserve_empty_cells_and_spanned_headers(
        self,
    ) -> None:
        related_party = html_table_semantic_segments(
            "<table><tr><td>关联方</td><td>关联交易内容</td>"
            "<td>本期发生额</td><td>上期发生额</td></tr>"
            "<tr><td>某财务公司</td><td>利息收入</td><td></td>"
            "<td>11.31</td></tr></table>"
        )
        self.assertEqual(
            tuple((item.text, item.role) for item in related_party),
            (
                ("关联方", "table_column_header"),
                ("关联交易内容", "table_column_header"),
                ("本期发生额", "table_column_header"),
                ("上期发生额", "table_column_header"),
                ("某财务公司", "table_text"),
                ("利息收入", "table_field_label"),
                ("11.31", "table_text"),
            ),
        )

        multirow = html_table_semantic_segments(
            '<table><tr><td rowspan="2">子公司名称</td>'
            '<td colspan="2">期末余额</td></tr>'
            "<tr><td>营业收入</td><td>净利润</td></tr>"
            "<tr><td>甲公司</td><td>100</td><td>20</td></tr></table>"
        )
        self.assertEqual(
            tuple((item.text, item.role) for item in multirow),
            (
                ("子公司名称", "table_column_header"),
                ("期末余额", "table_column_header"),
                ("营业收入", "table_column_header"),
                ("净利润", "table_column_header"),
                ("甲公司", "table_text"),
                ("100", "table_text"),
                ("20", "table_text"),
            ),
        )

        malformed_grid = html_table_semantic_segments(
            '<table><tr><td rowspan="2">名称</td><td>金额</td></tr>'
            "<tr><td>重复占位</td><td>10</td></tr></table>"
        )
        self.assertEqual(
            tuple(item.role for item in malformed_grid),
            ("table_text", "table_text", "table_text", "table_text"),
        )

    def test_table_semantic_roles_fail_closed_for_ragged_or_spanned_data_rows(
        self,
    ) -> None:
        for ragged in (
            "<table><tr><td>营业收入</td><td>本期数</td><td>上期数</td></tr>"
            "<tr><td>甲</td><td>1</td><td>2</td></tr>"
            "<tr><td>乙</td><td>3</td></tr></table>",
            "<table><tr><th>营业收入</th><th>本期数</th><th>上期数</th>"
            "<th>变动</th></tr><tr><td>甲</td><td>1</td><td>2</td></tr>"
            "<tr><td>乙</td><td>3</td><td>4</td><td>5</td></tr></table>",
        ):
            with self.subTest(ragged=ragged):
                self.assertTrue(
                    all(
                        item.role == "table_text"
                        for item in html_table_semantic_segments(ragged)
                    )
                )

    def test_table_semantic_roles_reject_ambiguous_markup_and_empty_headers(
        self,
    ) -> None:
        for malformed in (
            '<table><tr><th colspan="2" colspan="3">利息收入</th>'
            '<td>11.31</td></tr></table>',
            '<table><tr><thead><td>利息收入</td><td>金额</td></thead></tr>'
            '<tr><td>x</td><td>11.31</td></tr></table>',
            '<table><tr><td></td><td>利息收入</td></tr>'
            '<tr><td>甲</td><td>11.31</td></tr></table>',
        ):
            with self.subTest(malformed=malformed):
                segments = html_table_semantic_segments(malformed)
                self.assertTrue(
                    all(item.role == "table_text" for item in segments)
                )

        for spanned_data in (
            "<table><tr><td>科目</td><td>本期数</td><td>上年同期数</td>"
            "<td>变动比例</td></tr><tr><td rowspan='2'>营业收入</td>"
            "<td>10</td><td>9</td><td>11%</td></tr>"
            "<tr><td>3</td><td>2</td><td>50%</td></tr></table>",
            "<table><tr><td>关联方</td><td>关联交易内容</td>"
            "<td>本期发生额</td><td>上期发生额</td></tr>"
            "<tr><td rowspan='2'>甲公司</td><td>利息收入</td>"
            "<td>2</td><td>1</td></tr><tr><td>出售商品、提供劳务</td>"
            "<td>3</td><td>2</td></tr></table>",
            "<table><tr><td>科目</td><td>本期数</td><td>上年同期数</td>"
            "<td>变动比例</td></tr><tr><td colspan='2'>营业收入</td>"
            "<td>9</td><td>11%</td></tr></table>",
        ):
            with self.subTest(spanned_data=spanned_data):
                self.assertNotIn(
                    "table_field_label",
                    tuple(
                        item.role
                        for item in html_table_semantic_segments(spanned_data)
                    ),
                )

    def test_table_semantic_roles_require_closed_section_and_wrapper_grammar(
        self,
    ) -> None:
        malformed_tables = (
            "<table><tfoot><tr><td>营业收入</td><td>100</td></tr></tfoot>"
            "<tfoot><tr><td>研发费用</td><td>20</td></tr></tfoot></table>",
            "<table><div><tr><td>营业收入</td><td>100</td></tr>"
            "<tr><td>研发费用</td><td>20</td></tr></div></table>",
            "<table><tbody><div><tr><td>营业收入</td><td>100</td></tr>"
            "</div></tbody></table>",
            "<table><tr><div><td>营业收入</td><td>100</td></div>"
            "</tr></table>",
            "<table><tr><td>营业收入</td><td>100</td></tr>"
            "<tbody><tr><td>研发费用</td><td>20</td></tr></tbody></table>",
            "<table><tfoot><tr><td>营业收入</td><td>100</td></tr>"
            "</tfoot><tr><td>研发费用</td><td>20</td></tr></table>",
        )
        for malformed in malformed_tables:
            with self.subTest(malformed=malformed):
                self.assertEqual(html_table_semantic_segments(malformed), ())
                self.assertTrue(html_visible_text_segments(malformed))

        valid = (
            "<table><thead><tr><th>科目</th><th>本期数</th><th>上期数</th>"
            "</tr></thead><tbody><tr><td><div>营业收入</div></td>"
            "<td>100</td><td>90</td></tr></tbody><tbody><tr>"
            "<td><span>研发费用</span></td><td>20</td><td>18</td></tr>"
            "</tbody><tfoot><tr><td>合计</td><td>120</td><td>108</td>"
            "</tr></tfoot></table>"
        )
        segments = html_table_semantic_segments(valid)
        self.assertEqual(
            tuple((item.text, item.role) for item in segments),
            (
                ("科目", "table_column_header"),
                ("本期数", "table_column_header"),
                ("上期数", "table_column_header"),
                ("营业收入", "table_text"),
                ("100", "table_text"),
                ("90", "table_text"),
                ("研发费用", "table_text"),
                ("20", "table_text"),
                ("18", "table_text"),
                ("合计", "table_text"),
                ("120", "table_text"),
                ("108", "table_text"),
            ),
        )

    def test_qa_row_atoms_require_exact_header_contiguous_rows_and_no_spans(self) -> None:
        valid = (
            '<table><tr><td colspan="3">交流要点</td></tr>'
            "<tr><td>序号</td><td>提问内容</td><td>回复内容</td></tr>"
            "<tr><td>1</td><td>能繁母猪数量？</td><td>期末312.9万头。</td></tr>"
            "<tr><td>2</td><td>AI时代如何发展？</td><td>构建产业互联平台。</td></tr>"
            "</table>"
        )
        self.assertEqual(
            tuple((item.source_row_index, item.row_text) for item in html_qa_row_atoms(valid)),
            (
                (2, "1 能繁母猪数量？ 期末312.9万头。"),
                (3, "2 AI时代如何发展？ 构建产业互联平台。"),
            ),
        )
        for invalid in (
            valid.replace("提问内容", "问题"),
            valid.replace("<td>2</td>", "<td>3</td>"),
            valid.replace("<td>期末312.9万头。</td>", '<td colspan="2">期末312.9万头。</td>'),
            valid.replace("AI时代如何发展？", ""),
            valid.replace("构建产业互联平台。", "<table><tr><td>嵌套</td></tr></table>"),
        ):
            with self.subTest(invalid=invalid):
                self.assertEqual(html_qa_row_atoms(invalid), ())

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
        self.assertTrue(
            all("document_title" not in target.field for target in projection.targets)
        )

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
                _block(
                    0,
                    0,
                    0,
                    "header",
                    (ProviderPayload("text", None, "证券代码"),),
                    annotation="page_header",
                ),
                _block(
                    1,
                    0,
                    1,
                    "text",
                    (ProviderPayload("text", None, "第一章 标题"),),
                    annotation="title",
                    level=2,
                ),
                _block(
                    2,
                    0,
                    2,
                    "table",
                    (
                        ProviderPayload(
                            "table_body",
                            None,
                            "<table><tr><td>甲</td><td>乙</td></tr></table>",
                        ),
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
                _block(
                    4,
                    1,
                    1,
                    "text",
                    (ProviderPayload("text", None, "单位：元"),),
                    annotation="paragraph",
                ),
                _block(
                    5,
                    1,
                    2,
                    "text",
                    (ProviderPayload("text", None, "单位：元"),),
                    annotation="paragraph",
                ),
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


def _furniture_document() -> ProviderDocument:
    pages = (
        ProviderPage(
            0,
            (600.0, 800.0),
            (
                _block(
                    0,
                    0,
                    0,
                    "header",
                    (ProviderPayload("text", None, "重复页眉"),),
                    annotation="page_header",
                ),
                _block(
                    1,
                    0,
                    1,
                    "header",
                    (ProviderPayload("text", None, "证券代码：000001"),),
                    annotation="page_header",
                ),
                _block(
                    2,
                    0,
                    2,
                    "page_number",
                    (ProviderPayload("text", None, "1"),),
                    annotation="page_number",
                ),
            ),
        ),
        ProviderPage(
            1,
            (600.0, 800.0),
            (
                _block(
                    3,
                    1,
                    0,
                    "header",
                    (ProviderPayload("text", None, "重复页眉"),),
                    annotation="page_header",
                ),
                _block(
                    4,
                    1,
                    1,
                    "footer",
                    (ProviderPayload("text", None, "特此公告。"),),
                    annotation="page_footer",
                ),
                _block(5, 1, 2, "footer", (), annotation="page_footer"),
            ),
        ),
    )
    return ProviderDocument(
        source_pdf_sha256=_SOURCE_SHA,
        parser_version="3.4.4",
        backend="hybrid",
        effort="medium",
        ocr_enabled=False,
        pages=pages,
        physical_table_segments=(),
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
    artifact_roles: tuple[str, ...] = (),
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
        referenced_artifact_roles=artifact_roles,
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
