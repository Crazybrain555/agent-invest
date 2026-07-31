from __future__ import annotations

import ctypes
from copy import deepcopy
import unittest
from unittest import mock
from typing import Any

import pypdfium2 as pdfium
from pdfminer.pdftypes import PDFObjRef

from disclosure_anchor.adapters.parsers.pdf_native_structure import (
    NATIVE_PDF_STRUCTURE_VERSION,
    NativeStructureDiagnostics,
    _destination_screen_y,
    _marked_content_ids,
    _stream_scoped_mcid_issue,
    validate_pdf_structure_artifact,
)
from disclosure_anchor.adapters.parsers.pdfium_geometry import (
    PageScreenGeometry,
    normalized_screen_bbox,
    page_screen_geometry,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


def _table_structure() -> dict[str, Any]:
    return {
        "contract_version": NATIVE_PDF_STRUCTURE_VERSION,
        "source_pdf_sha256": "sha256:" + "a" * 64,
        "source_pdf_page_count": 1,
        "native_status": "usable",
        "pdfium_tagged": True,
        "role_map": {},
        "segments": [
            {
                "segment_id": "native_1",
                "top_object_id": 10,
                "node_id_span": [1, 2],
                "page_indices": [0],
                "pages_contiguous": True,
            }
        ],
        "nodes": [
            {
                "node_id": 1,
                "object_id": 10,
                "raw_role": "Table",
                "standard_role": "Table",
                "segment_id": "native_1",
                "ancestor_roles": [],
                "ancestor_node_ids": [],
                "forward_parent_object_id": 1,
                "declared_parent_object_id": 1,
                "parent_consistent": True,
                "mcid_refs": [{"page_idx": 0, "mcid": 0}],
            },
            {
                "node_id": 2,
                "object_id": 11,
                "raw_role": "TD",
                "standard_role": "TD",
                "segment_id": "native_1",
                "ancestor_roles": ["Table"],
                "ancestor_node_ids": [1],
                "forward_parent_object_id": 10,
                "declared_parent_object_id": 10,
                "parent_consistent": True,
                "mcid_refs": [{"page_idx": 0, "mcid": 0}],
            },
        ],
        "marked_content": [
            {
                "page_idx": 0,
                "mcid": 0,
                "mcid_marks": [{"mark_order": 0, "mcid": 0}],
                "object_order": 0,
                "object_type": "text",
                "object_depth": 0,
                "stream_scope": "page_content",
                "text": "股份变动",
                "bbox": [100.0, 50.0, 500.0, 100.0],
            }
        ],
        "bookmarks": [],
        "diagnostics": {
            "parent_conflicts": 0,
            "unresolved": [],
            "root_reachable_nodes": 2,
            "visible_mcid_anchors": 1,
            "marked_content_objects": 1,
            "referenced_mcid_refs": 1,
            "resolved_mcid_refs": 1,
            "unresolved_mcid_refs": [],
            "object_issues": [],
        },
    }


def _validated(value: object) -> Any:
    return validate_pdf_structure_artifact(
        value,
        expected_source_pdf_sha256="sha256:" + "a" * 64,
        expected_page_count=1,
    )


class PdfNativeStructureContractTests(unittest.TestCase):
    def test_only_a_closed_real_table_ancestor_exposes_a_cell(self) -> None:
        structure = _table_structure()
        index = _validated(structure)
        self.assertEqual(len(index.table_cells), 1)
        self.assertEqual(index.table_cells[0].text, "股份变动")

        for mutate in (
            lambda value: value["nodes"][1].update({"ancestor_node_ids": [99]}),
            lambda value: value["nodes"][1].update({"parent_consistent": "yes"}),
            lambda value: value["nodes"][0].update({"raw_role": "P"}),
            lambda value: value["marked_content"][0].update({"object_type": ["text"]}),
            lambda value: value["marked_content"][0].update(
                {"stream_scope": {"scope": "page_content"}}
            ),
        ):
            with self.subTest(mutate=mutate):
                malformed = deepcopy(structure)
                mutate(malformed)
                with self.assertRaises(ParserOutputContractError):
                    _validated(malformed)

    def test_one_mcid_owned_by_two_cells_is_not_joinable(self) -> None:
        structure = _table_structure()
        duplicate = deepcopy(structure["nodes"][1])
        duplicate.update({"node_id": 3, "object_id": 12})
        structure["nodes"].append(duplicate)
        structure["segments"][0]["node_id_span"] = [1, 3]
        structure["diagnostics"]["root_reachable_nodes"] = 3

        index = _validated(structure)
        self.assertEqual(index.table_cells, ())
        self.assertEqual(
            index.table_guard_bboxes,
            ((0, (100.0, 50.0, 500.0, 100.0)),),
        )

    def test_partial_cell_reference_is_preserved_but_not_joinable(self) -> None:
        structure = _table_structure()
        for node in structure["nodes"]:
            node["mcid_refs"].append({"page_idx": 0, "mcid": 1})
        structure["native_status"] = "partial"
        structure["diagnostics"].update(
            {
                "referenced_mcid_refs": 2,
                "resolved_mcid_refs": 1,
                "unresolved_mcid_refs": [{"page_idx": 0, "mcid": 1}],
            }
        )

        self.assertEqual(_validated(structure).table_cells, ())

    def test_text_extraction_issue_disables_only_the_affected_cell(self) -> None:
        structure = _table_structure()
        structure["marked_content"].append(
            {
                "page_idx": 0,
                "mcid": 0,
                "mcid_marks": [{"mark_order": 0, "mcid": 0}],
                "object_order": 1,
                "object_type": "text",
                "object_depth": 0,
                "stream_scope": "page_content",
                "text": None,
                "bbox": [500.0, 50.0, 600.0, 100.0],
            }
        )
        missing_diagnostic = deepcopy(structure)
        missing_diagnostic["native_status"] = "partial"
        missing_diagnostic["diagnostics"].update(
            {
                "visible_mcid_anchors": 2,
                "marked_content_objects": 2,
            }
        )
        with self.assertRaises(ParserOutputContractError):
            _validated(missing_diagnostic)

        structure["native_status"] = "partial"
        structure["diagnostics"].update(
            {
                "visible_mcid_anchors": 2,
                "marked_content_objects": 2,
                "object_issues": [
                    {
                        "page_idx": 0,
                        "mcid": 0,
                        "object_order": 1,
                        "reason": "text_unavailable",
                    }
                ],
            }
        )

        self.assertEqual(_validated(structure).table_cells, ())
        malformed_reason = deepcopy(structure)
        malformed_reason["diagnostics"]["object_issues"][0]["reason"] = [
            "text_unavailable"
        ]
        with self.assertRaises(ParserOutputContractError):
            _validated(malformed_reason)

    def test_nested_form_reusing_page_mcid_cannot_contaminate_cell(self) -> None:
        structure = _table_structure()
        structure["marked_content"].append(
            {
                "page_idx": 0,
                "mcid": 0,
                "mcid_marks": [{"mark_order": 0, "mcid": 0}],
                "object_order": 1,
                "object_type": "text",
                "object_depth": 1,
                "stream_scope": "nested_form_unresolved",
                "text": "错误拼接",
                "bbox": [100.0, 50.0, 500.0, 100.0],
            }
        )
        structure["native_status"] = "partial"
        structure["diagnostics"].update(
            {
                "visible_mcid_anchors": 2,
                "marked_content_objects": 2,
                "object_issues": [
                    {
                        "page_idx": 0,
                        "mcid": 0,
                        "object_order": 1,
                        "reason": "nested_stream_identity_unavailable",
                    }
                ],
            }
        )

        index = _validated(structure)
        self.assertEqual(len(index.table_cells), 1)
        self.assertEqual(index.table_cells[0].text, "股份变动")

    def test_multiple_mcid_marks_are_complete_but_not_joinable(self) -> None:
        structure = _table_structure()
        item = structure["marked_content"][0]
        item["mcid_marks"] = [
            {"mark_order": 0, "mcid": 0},
            {"mark_order": 2, "mcid": 1},
        ]
        item["stream_scope"] = "multiple_mcid_marks_unresolved"
        structure["native_status"] = "partial"
        structure["diagnostics"].update(
            {
                "resolved_mcid_refs": 0,
                "unresolved_mcid_refs": [{"page_idx": 0, "mcid": 0}],
                "object_issues": [
                    {
                        "page_idx": 0,
                        "mcid": 0,
                        "object_order": 0,
                        "reason": "multiple_mcid_marks_unresolved",
                    }
                ],
            }
        )

        self.assertEqual(_validated(structure).table_cells, ())

    def test_validated_index_exposes_every_fact_its_consumers_read(self) -> None:
        structure = _table_structure()
        structure["bookmarks"] = [
            {
                "bookmark_order": 0,
                "level": 1,
                "title": "股份变动情况",
                "page_idx": 0,
                "destination_y": 60.5,
                "destination_view": None,
            },
            {
                "bookmark_order": 1,
                "level": 2,
                "title": "无目标页",
                "page_idx": None,
                "destination_y": None,
                "destination_view": None,
            },
        ]

        index = _validated(structure)

        self.assertEqual(index.source_pdf_page_count, 1)
        self.assertEqual(index.native_status, "usable")
        self.assertIs(index.pdfium_tagged, True)
        self.assertEqual(
            [
                (
                    node.node_id,
                    node.segment_id,
                    node.raw_role,
                    node.standard_role,
                    node.ancestor_roles,
                    node.ancestor_node_ids,
                    node.parent_consistent,
                    node.mcid_refs,
                )
                for node in index.nodes
            ],
            [
                (1, "native_1", "Table", "Table", (), (), True, ((0, 0),)),
                (2, "native_1", "TD", "TD", ("Table",), (1,), True, ((0, 0),)),
            ],
        )
        self.assertEqual(
            [
                (mark.bookmark_order, mark.level, mark.title, mark.page_idx,
                 mark.destination_y)
                for mark in index.bookmarks
            ],
            [(0, 1, "股份变动情况", 0, 60.5), (1, 2, "无目标页", None, None)],
        )
        self.assertEqual(list(index.marked_objects), [(0, 0)])
        self.assertEqual(
            [
                (obj.object_order, obj.object_type, obj.text, obj.bbox)
                for obj in index.marked_objects[(0, 0)]
            ],
            [(0, "text", "股份变动", (100.0, 50.0, 500.0, 100.0))],
        )
        self.assertEqual(
            index.diagnostics,
            NativeStructureDiagnostics(
                parent_conflicts=0,
                root_reachable_nodes=2,
                visible_mcid_anchors=1,
                marked_content_objects=1,
                referenced_mcid_refs=1,
                resolved_mcid_refs=1,
                unresolved_reasons=(),
                unresolved_mcid_refs=(),
                object_issues=(),
            ),
        )

    def test_unresolvable_or_ill_typed_bookmarks_fail_the_whole_artifact(
        self,
    ) -> None:
        def entry(**overrides: Any) -> dict[str, Any]:
            return {
                "bookmark_order": 0,
                "level": 1,
                "title": "股份变动情况",
                "page_idx": 0,
                "destination_y": None,
                "destination_view": None,
                **overrides,
            }

        structure = _table_structure()
        structure["bookmarks"] = [entry()]
        self.assertEqual(len(_validated(structure).bookmarks), 1)

        # Real outlines aim /XYZ targets slightly outside the crop box; the
        # finite value stays a valid artifact fact for downstream arbitration.
        off_page = deepcopy(structure)
        off_page["bookmarks"] = [entry(destination_y=-6.345)]
        validated = _validated(off_page)
        self.assertEqual(validated.bookmarks[0].destination_y, -6.345)

        for label, broken in (
            ("order_mismatch", entry(bookmark_order=1)),
            ("level_zero", entry(level=0)),
            ("level_boolean", entry(level=True)),
            ("level_missing", entry(level=None)),
            ("title_not_text", entry(title=12)),
            ("page_not_index", entry(page_idx="0")),
            ("page_boolean", entry(page_idx=True)),
            ("page_outside_pdf", entry(page_idx=1)),
            ("destination_not_finite", entry(destination_y=float("nan"))),
            ("field_not_closed", {**entry(), "extra": 1}),
        ):
            with self.subTest(label=label):
                malformed = deepcopy(structure)
                malformed["bookmarks"] = [broken]
                with self.assertRaises(ParserOutputContractError):
                    _validated(malformed)

    def test_pdfium_mark_enumeration_does_not_take_only_the_first_mcid(
        self,
    ) -> None:
        marks = (object(), object(), object())

        def get_int(mark: object, _key: bytes, output: Any) -> bool:
            if mark is marks[1]:
                return False
            value = 3 if mark is marks[0] else 9
            ctypes.cast(output, ctypes.POINTER(ctypes.c_int)).contents.value = value
            return True

        with (
            mock.patch(
                "disclosure_anchor.adapters.parsers.pdf_native_structure."
                "pdfium_raw.FPDFPageObj_CountMarks",
                return_value=3,
            ),
            mock.patch(
                "disclosure_anchor.adapters.parsers.pdf_native_structure."
                "pdfium_raw.FPDFPageObj_GetMark",
                side_effect=lambda _obj, index: marks[index],
            ),
            mock.patch(
                "disclosure_anchor.adapters.parsers.pdf_native_structure."
                "pdfium_raw.FPDFPageObjMark_GetParamIntValue",
                side_effect=get_int,
            ),
        ):
            self.assertEqual(
                _marked_content_ids(mock.Mock()),
                [
                    {"mark_order": 0, "mcid": 3},
                    {"mark_order": 2, "mcid": 9},
                ],
            )

    def test_stream_scoped_mcid_keeps_location_and_stream_identity(self) -> None:
        issue, page_idx = _stream_scoped_mcid_issue(
            {
                "MCID": 7,
                "Pg": PDFObjRef(None, 20),
                "Stm": PDFObjRef(None, 30),
                "StmOwn": PDFObjRef(None, 40),
            },
            object_id=50,
            inherited_page_ref=None,
            page_by_object_id={20: 3},
            segment_id="native_2",
        )

        self.assertEqual(page_idx, 3)
        self.assertEqual(
            issue,
            {
                "object_id": 50,
                "reason": "stream_scoped_mcid",
                "segment_id": "native_2",
                "mcid": 7,
                "page_idx": 3,
                "stm_object_id": 30,
                "stm_owner_object_id": 40,
            },
        )


class PdfNativeStructureCoordinatesTests(unittest.TestCase):
    def test_object_bounds_follow_pdfium_page_transform_for_every_rotation(
        self,
    ) -> None:
        crop = (50.0, 100.0, 550.0, 700.0)
        raw_bbox = (150.0, 250.0, 350.0, 450.0)
        expected = {
            0: [200.0, 416.667, 600.0, 750.0],
            90: [250.0, 200.0, 583.333, 600.0],
            180: [400.0, 250.0, 800.0, 583.333],
            270: [416.667, 400.0, 750.0, 800.0],
        }
        document = pdfium.PdfDocument.new()
        page = document.new_page(600, 800)
        try:
            page.set_cropbox(*crop)
            for rotation, wanted in expected.items():
                with self.subTest(rotation=rotation):
                    page.set_rotation(rotation)
                    actual = normalized_screen_bbox(
                        page_screen_geometry(page),
                        raw_bbox,
                    )
                    self.assertIsNotNone(actual)
                    assert actual is not None
                    for observed, target in zip(actual, wanted, strict=True):
                        self.assertAlmostEqual(observed, target, places=3)
        finally:
            page.close()
            document.close()

    def test_invalid_or_unmappable_object_bounds_fail_closed(self) -> None:
        document = pdfium.PdfDocument.new()
        page = document.new_page(600, 800)
        try:
            geometry = page_screen_geometry(page)
            self.assertIsNone(normalized_screen_bbox(geometry, (1, 1, 1, 2)))
            with self.assertRaises(ParserOutputContractError):
                normalized_screen_bbox(
                    geometry,
                    (float("nan"), 1, 2, 3),
                )
            self.assertEqual(
                normalized_screen_bbox(geometry, (-10, 100, 10, 200)),
                (0.0, 750.0, 16.667, 875.0),
            )
            self.assertIsNone(normalized_screen_bbox(geometry, (-100, 100, -50, 200)))
            with (
                mock.patch(
                    "disclosure_anchor.adapters.parsers.pdfium_geometry."
                    "pdfium.PdfPosConv",
                ) as converter,
                self.assertRaises(ParserOutputContractError),
            ):
                converter.return_value.to_bitmap.side_effect = pdfium.PdfiumError(
                    "unmappable"
                )
                normalized_screen_bbox(
                    page_screen_geometry(page),
                    (1, 1, 2, 2),
                )
        finally:
            page.close()
            document.close()

    def test_bookmark_position_requires_a_complete_absolute_xyz_point(self) -> None:
        def xyz_location(
            _destination: object,
            has_x: Any,
            has_y: Any,
            has_zoom: Any,
            x: Any,
            y: Any,
            zoom: Any,
        ) -> bool:
            ctypes.cast(has_x, ctypes.POINTER(ctypes.c_int)).contents.value = 1
            ctypes.cast(has_y, ctypes.POINTER(ctypes.c_int)).contents.value = 1
            ctypes.cast(has_zoom, ctypes.POINTER(ctypes.c_int)).contents.value = 0
            ctypes.cast(x, ctypes.POINTER(ctypes.c_float)).contents.value = 150
            ctypes.cast(y, ctypes.POINTER(ctypes.c_float)).contents.value = 450
            ctypes.cast(zoom, ctypes.POINTER(ctypes.c_float)).contents.value = 0
            return True

        document = pdfium.PdfDocument.new()
        page = document.new_page(600, 800)
        page.close()
        cache: dict[int, tuple[pdfium.PdfPage, PageScreenGeometry]] = {}
        try:
            with (
                mock.patch(
                    "disclosure_anchor.adapters.parsers.pdf_native_structure."
                    "pdfium_raw.FPDFDest_GetLocationInPage",
                    side_effect=xyz_location,
                ),
                mock.patch(
                    "disclosure_anchor.adapters.parsers.pdfium_geometry."
                    "pdfium.PdfPosConv",
                ) as converter,
            ):
                converter.return_value.to_bitmap.return_value = (250_000, 300_000)
                self.assertEqual(
                    _destination_screen_y(
                        document,
                        mock.Mock(),
                        page_idx=0,
                        page_cache=cache,
                    ),
                    300.0,
                )
            for cached_page, _ in cache.values():
                cached_page.close()
            cache.clear()
            with mock.patch(
                "disclosure_anchor.adapters.parsers.pdf_native_structure."
                "pdfium_raw.FPDFDest_GetLocationInPage",
                return_value=False,
            ):
                self.assertIsNone(
                    _destination_screen_y(
                        document,
                        mock.Mock(),
                        page_idx=0,
                        page_cache=cache,
                    )
                )
        finally:
            for cached_page, _ in cache.values():
                cached_page.close()
            document.close()


if __name__ == "__main__":
    unittest.main()
