from __future__ import annotations

from dataclasses import replace
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.review_mineru_medium_outline import (
    build_review_payload,
    load_bound_run_evidence,
    main,
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


_SOURCE_SHA = "sha256:" + "a" * 64
_BUNDLE_SHA = "sha256:" + "b" * 64
_RAW_SHA = "sha256:" + "c" * 64


class ReviewMinerUMediumOutlineTest(unittest.TestCase):
    def test_review_accounts_for_every_block_unit_heading_and_table_segment(self) -> None:
        document = _document()
        outline = build_document_outline(document)

        payload = build_review_payload(
            document,
            outline,
            source_page_offset=42,
            selected_provider_pages=(0,),
        )

        provider = payload["provider_document"]
        review = payload["review"]
        self.assertIsInstance(provider, dict)
        self.assertIsInstance(review, dict)
        assert isinstance(provider, dict) and isinstance(review, dict)
        pages = provider["pages"]
        segments = provider["physical_table_segments"]
        assignments = review["block_assignments"]
        inventory = review["physical_table_segment_inventory"]
        self.assertEqual(len(pages[0]["blocks"]), 3)
        self.assertEqual(len(assignments), 3)
        self.assertEqual([item["source_index"] for item in assignments], [0, 1, 2])
        self.assertTrue(all(item["alias_status"] == "not_evaluated" for item in assignments))
        self.assertEqual(len(segments), 2)
        self.assertEqual(len(inventory), 2)
        self.assertEqual([item["segment_index"] for item in inventory], [0, 1])
        self.assertTrue(
            all(item["flat_block_association"] == "not_asserted" for item in inventory)
        )
        self.assertEqual(assignments[0]["source_page_number"], 43)
        self.assertNotIn("raw_item_json", pages[0]["blocks"][0])
        self.assertNotIn("raw_segment_json", segments[0])
        self.assertIn("page_local_html_sha256", segments[0])
        self.assertEqual(len(payload["outline"]["headings"]), 1)

    def test_review_rejects_stale_outline_or_provider_block(self) -> None:
        document = _document()
        outline = build_document_outline(document)
        stale_outline = replace(
            outline,
            source_pdf_sha256="sha256:" + "d" * 64,
        )
        with self.assertRaises(ValueError):
            build_review_payload(
                document,
                stale_outline,
                source_page_offset=0,
                selected_provider_pages=(0,),
            )

        stale_block = replace(document.blocks[0], raw_item_sha256="sha256:" + "d" * 64)
        stale_page = replace(
            document.pages[0],
            blocks=(stale_block, *document.pages[0].blocks[1:]),
        )
        stale_document = replace(document, pages=(stale_page,))
        with self.assertRaises(ValueError):
            build_review_payload(
                stale_document,
                outline,
                source_page_offset=0,
                selected_provider_pages=(0,),
            )

    def test_review_and_provider_records_fail_closed_on_invalid_boundaries(self) -> None:
        document = _document()
        outline = build_document_outline(document)
        for offset, pages in ((-1, (0,)), (0, ()), (0, (0, 0)), (0, (1,))):
            with self.subTest(offset=offset, pages=pages), self.assertRaises(ValueError):
                build_review_payload(
                    document,
                    outline,
                    source_page_offset=offset,
                    selected_provider_pages=pages,
                )

        with self.assertRaises(ValueError):
            replace(
                document.physical_table_segments[0],
                logical_stream_status="unknown",  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            replace(
                document,
                physical_table_segments=tuple(
                    reversed(document.physical_table_segments)
                ),
            )

    def test_cli_requires_run_evidence_before_touching_inputs(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(
                [
                    "--source-pdf",
                    "/not/read.pdf",
                    "--provider-bundle",
                    "/not/read",
                    "--source-page-offset",
                    "0",
                    "--out",
                    "/private/tmp/not-created",
                ]
            )

    def test_run_evidence_binds_source_bundle_and_page_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cell = Path(directory)
            output_root = cell / "parser-output"
            bundle = output_root / ("sha256_" + "a" * 64) / "hybrid_auto"
            bundle.mkdir(parents=True)
            evidence_path = cell / "run-evidence.json"
            evidence_path.write_text(
                json.dumps(
                    {
                        "command": [
                            "mineru",
                            "-o",
                            str(output_root),
                            "-s",
                            "42",
                            "-e",
                            "49",
                        ],
                        "input_before": {"sha256": "a" * 64, "page_count": 161},
                        "input_after": {"sha256": "a" * 64, "page_count": 161},
                        "input_unchanged": True,
                        "output_files": [],
                        "status": "succeeded_raw_observation",
                        "invariant_errors": [],
                    }
                ),
                encoding="utf-8",
            )

            record = load_bound_run_evidence(
                evidence_path,
                source_pdf_sha256=_SOURCE_SHA,
                source_page_count=161,
                source_page_offset=42,
                provider_page_count=8,
                provider_bundle=bundle,
            )
            page_window = record["page_window"]
            self.assertIsInstance(page_window, dict)
            assert isinstance(page_window, dict)
            self.assertEqual(page_window["start_zero_based"], 42)

            with self.assertRaises(ValueError):
                load_bound_run_evidence(
                    evidence_path,
                    source_pdf_sha256=_SOURCE_SHA,
                    source_page_count=161,
                    source_page_offset=41,
                    provider_page_count=8,
                    provider_bundle=bundle,
                )

            other_bundle = cell / "other-output" / ("sha256_" + "a" * 64) / "hybrid_auto"
            other_bundle.mkdir(parents=True)
            with self.assertRaises(ValueError):
                load_bound_run_evidence(
                    evidence_path,
                    source_pdf_sha256=_SOURCE_SHA,
                    source_page_count=161,
                    source_page_offset=42,
                    provider_page_count=8,
                    provider_bundle=other_bundle,
                )


def _document() -> ProviderDocument:
    blocks = (
        _block(0, "第一章 总则", annotation="title", level=2),
        _block(1, "完整正文", annotation="paragraph"),
        _block(2, "", provider_type="table", annotation="table"),
    )
    page = ProviderPage(
        page_index=0,
        page_size=(600.0, 800.0),
        blocks=blocks,
    )
    table_bbox = ProviderBBox(100, 500, 900, 900)
    segments = (
        _segment(0, 7, table_bbox, "retained", "前页"),
        _segment(1, 8, table_bbox, "deleted", "续页"),
    )
    return ProviderDocument(
        source_pdf_sha256=_SOURCE_SHA,
        parser_version="3.4.4",
        backend="hybrid",
        effort="medium",
        ocr_enabled=False,
        pages=(page,),
        physical_table_segments=segments,
        artifacts=(),
        bundle_sha256=_BUNDLE_SHA,
    )


def _block(
    source_index: int,
    text: str,
    *,
    annotation: str,
    level: int | None = None,
    provider_type: str = "text",
) -> ProviderBlock:
    return ProviderBlock(
        source_index=source_index,
        page_index=0,
        order_in_page=source_index,
        provider_type=provider_type,
        typed_annotation=annotation,
        provider_level=level,
        bbox=ProviderBBox(
            100,
            50 + source_index * 100,
            900,
            100 + source_index * 100,
        ),
        payloads=() if not text else (ProviderPayload("text", None, text),),
        referenced_artifact_roles=(),
        raw_item_json="{}",
        raw_item_sha256=_RAW_SHA,
    )


def _segment(
    order: int,
    provider_index: int,
    bbox: ProviderBBox,
    status: str,
    text: str,
) -> ProviderPhysicalTableSegment:
    return ProviderPhysicalTableSegment(
        page_index=0,
        order_in_page=order,
        provider_index=provider_index,
        bbox=bbox,
        page_local_html=f"<table><tr><td>{text}</td></tr></table>",
        crop_artifact_role=None,
        logical_stream_status=status,  # type: ignore[arg-type]
        cell_merge_json=None,
        raw_segment_json=f'{{"order":{order}}}',
        raw_segment_sha256="sha256:" + str(order + 1) * 64,
    )


if __name__ == "__main__":
    unittest.main()
