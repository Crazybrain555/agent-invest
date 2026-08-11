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
from disclosure_anchor.application.services.provider_table_projection import (
    build_provider_table_projection,
)


_SOURCE_SHA = "sha256:" + "a" * 64
_RAW_SHA = "sha256:" + "b" * 64


class ProviderTableProjectionTest(unittest.TestCase):
    def test_provider_retained_owner_and_three_deleted_stubs_form_one_table(
        self,
    ) -> None:
        document = _document(
            pages=(
                (_block(0, 0, "text", "正文"), _block(1, 0, "table", "<table><tr><td>全表</td></tr></table>")),
                (_block(2, 1, "header", "页眉", annotation="page_header"), _block(3, 1, "table", "")),
                (_block(4, 2, "table", ""), _block(5, 2, "page_number", "2", annotation="page_number")),
                (_block(6, 3, "table", ""),),
            ),
            segments=(
                _segment(0, 0, "retained"),
                _segment(1, 0, "deleted"),
                _segment(2, 0, "deleted"),
                _segment(3, 0, "deleted"),
            ),
        )

        projection = build_provider_table_projection(document)

        self.assertEqual(len(projection.logical_tables), 1)
        logical = projection.logical_tables[0]
        self.assertEqual(logical.owner.block_source_index, 1)
        self.assertEqual(
            [part.block_source_index for part in logical.continuations],
            [3, 4, 6],
        )
        self.assertEqual(projection.unbound_parts, ())

    def test_new_retained_table_stays_independent_after_continuation(self) -> None:
        document = _document(
            pages=(
                (_block(0, 0, "table", "<table><td>A</td></table>"),),
                (
                    _block(1, 1, "table", ""),
                    _block(2, 1, "table", "<table><td>B</td></table>"),
                ),
            ),
            segments=(
                _segment(0, 0, "retained"),
                _segment(1, 0, "deleted"),
                _segment(1, 1, "retained"),
            ),
        )

        projection = build_provider_table_projection(document)

        self.assertEqual(
            [table.owner.block_source_index for table in projection.logical_tables],
            [0, 2],
        )
        self.assertEqual(
            [part.block_source_index for part in projection.logical_tables[0].continuations],
            [1],
        )

    def test_substantive_page_boundary_prevents_continuation_guess(self) -> None:
        document = _document(
            pages=(
                (_block(0, 0, "table", "<table><td>A</td></table>"),),
                (
                    _block(1, 1, "text", "新章节"),
                    _block(2, 1, "table", ""),
                ),
            ),
            segments=(
                _segment(0, 0, "retained"),
                _segment(1, 0, "deleted"),
            ),
        )

        projection = build_provider_table_projection(document)

        self.assertEqual(len(projection.logical_tables), 1)
        self.assertEqual(projection.logical_tables[0].continuations, ())
        self.assertEqual(
            [(item.part.block_source_index, item.reason) for item in projection.unbound_parts],
            [(2, "continuation_not_page_boundary")],
        )

    def test_count_and_status_mismatches_remain_unbound_without_loss(self) -> None:
        count_mismatch = _document(
            pages=((_block(0, 0, "table", "<table><td>A</td></table>"),),),
            segments=(),
        )
        mismatch_projection = build_provider_table_projection(count_mismatch)
        self.assertEqual(
            [(item.part.block_source_index, item.part.physical_segment_index, item.reason)
             for item in mismatch_projection.unbound_parts],
            [(0, None, "page_table_count_mismatch")],
        )

        status_mismatch = _document(
            pages=(
                (
                    _block(0, 0, "table", ""),
                    _block(1, 0, "table", "<table><td>B</td></table>"),
                ),
            ),
            segments=(
                _segment(0, 0, "retained"),
                _segment(0, 1, "deleted"),
            ),
        )
        status_projection = build_provider_table_projection(status_mismatch)
        self.assertEqual(
            [item.reason for item in status_projection.unbound_parts],
            ["retained_without_payload", "deleted_with_payload"],
        )

    def test_bbox_and_status_do_not_invent_a_relation(self) -> None:
        document = _document(
            pages=(
                (_block(0, 0, "table", "<table><td>A</td></table>"),),
                (_block(1, 1, "table", ""),),
            ),
            segments=(
                replace(_segment(0, 0, "retained"), bbox=None),
                replace(_segment(1, 0, "unbound"), bbox=None),
            ),
        )

        projection = build_provider_table_projection(document)

        self.assertEqual(projection.logical_tables[0].continuations, ())
        self.assertEqual(projection.unbound_parts[0].reason, "provider_status_unbound")

    def test_deleted_stub_requires_an_owner_on_the_immediately_previous_page(
        self,
    ) -> None:
        no_owner = _document(
            pages=((_block(0, 0, "table", ""),),),
            segments=(_segment(0, 0, "deleted"),),
        )
        self.assertEqual(
            build_provider_table_projection(no_owner).unbound_parts[0].reason,
            "continuation_without_owner",
        )

        page_gap = _document(
            pages=(
                (_block(0, 0, "table", "<table><td>A</td></table>"),),
                (),
                (_block(1, 2, "table", ""),),
            ),
            segments=(
                _segment(0, 0, "retained"),
                _segment(2, 0, "deleted"),
            ),
        )
        gap_projection = build_provider_table_projection(page_gap)
        self.assertEqual(gap_projection.logical_tables[0].continuations, ())
        self.assertEqual(
            gap_projection.unbound_parts[0].reason,
            "continuation_not_next_page",
        )


def _document(
    *,
    pages: tuple[tuple[ProviderBlock, ...], ...],
    segments: tuple[ProviderPhysicalTableSegment, ...],
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
    return ProviderDocument(
        source_pdf_sha256=_SOURCE_SHA,
        parser_version="3.4.4",
        backend="hybrid",
        effort="medium",
        ocr_enabled=False,
        pages=provider_pages,
        physical_table_segments=segments,
        artifacts=(),
        bundle_sha256=provider_artifact_bundle_sha256(()),
    )


def _block(
    source_index: int,
    page_index: int,
    provider_type: str,
    text: str,
    *,
    annotation: str | None = None,
) -> ProviderBlock:
    if not text:
        payloads: tuple[ProviderPayload, ...] = ()
    elif provider_type == "table":
        payloads = (ProviderPayload("table_body", None, text),)
    else:
        payloads = (ProviderPayload("text", None, text),)
    return ProviderBlock(
        source_index=source_index,
        page_index=page_index,
        order_in_page=0,
        provider_type=provider_type,
        typed_annotation=annotation,
        provider_level=None,
        bbox=None,
        payloads=payloads,
        referenced_artifact_roles=(),
        raw_item_json=f'{{"source":{source_index}}}',
        raw_item_sha256=_RAW_SHA,
    )


def _segment(
    page_index: int,
    order_in_page: int,
    status: str,
) -> ProviderPhysicalTableSegment:
    marker = page_index * 10 + order_in_page + 1
    return ProviderPhysicalTableSegment(
        page_index=page_index,
        order_in_page=order_in_page,
        provider_index=order_in_page,
        bbox=None,
        page_local_html=f"<table><td>{marker}</td></table>",
        crop_artifact_role=None,
        logical_stream_status=status,  # type: ignore[arg-type]
        raw_segment_json=f'{{"segment":{marker}}}',
        raw_segment_sha256="sha256:" + f"{marker:064x}",
    )


if __name__ == "__main__":
    unittest.main()
