"""Lossless MinerU table-HTML structure tests."""

from __future__ import annotations

import unittest

from disclosure_anchor.adapters.parsers.mineru.table_html_structure import (
    TableHtmlStructureError,
    parse_table_html_structure,
    table_media_artifact_role,
)


class MinerUTableHtmlStructureTests(unittest.TestCase):
    def test_spans_headers_and_media_occurrences_remain_explicit(self) -> None:
        structure = parse_table_html_structure(
            '<table><tr><th rowspan="2">项目</th><th colspan="2">本期</th></tr>'
            '<tr><td><img src="images/a.png"/><img src="images/a.png"/></td>'
            '<td><img src="images/b.png"/></td></tr></table>'
        )

        self.assertEqual(structure.headers, ("项目", "本期", "本期"))
        self.assertEqual(
            structure.rows,
            (("项目", "", ""),),
        )
        self.assertEqual(
            [
                (
                    cell.row,
                    cell.col,
                    cell.rowspan,
                    cell.colspan,
                    cell.text,
                    cell.is_header,
                )
                for cell in structure.cells
            ],
            [
                (0, 0, 2, 1, "项目", True),
                (0, 1, 1, 2, "本期", True),
                (1, 1, 1, 1, "", False),
                (1, 2, 1, 1, "", False),
            ],
        )
        self.assertEqual(
            [
                (
                    media.occurrence_index,
                    media.cell_media_index,
                    media.row,
                    media.col,
                    media.image_path,
                )
                for media in structure.embedded_media
            ],
            [
                (0, 0, 1, 1, "images/a.png"),
                (1, 1, 1, 1, "images/a.png"),
                (2, 0, 1, 2, "images/b.png"),
            ],
        )
        self.assertEqual(
            structure.merged_cells,
            ((0, 0, 2, 1), (0, 1, 1, 2)),
        )

    def test_one_complete_header_row_is_projected_without_losing_cells(
        self,
    ) -> None:
        structure = parse_table_html_structure(
            "<table><tr><th>项目</th><th>金额</th></tr>"
            "<tr><td>收入</td><td>10</td></tr></table>"
        )

        self.assertEqual(structure.headers, ("项目", "金额"))
        self.assertEqual(structure.rows, (("收入", "10"),))
        self.assertEqual(len(structure.cells), 4)

    def test_mixed_or_later_header_roles_do_not_invent_a_header_row(self) -> None:
        for html in (
            "<table><tr><th>项目</th><td>金额</td></tr></table>",
            "<table><tr><td>项目</td></tr><tr><th>收入</th></tr></table>",
        ):
            with self.subTest(html=html):
                structure = parse_table_html_structure(html)
                self.assertEqual(structure.headers, ())
                self.assertTrue(structure.rows)

    def test_overlap_and_unbound_media_fail_closed(self) -> None:
        cases = (
            (
                '<table><tr><td rowspan="2">甲</td><td>乙</td>'
                '<td rowspan="2">丙</td></tr>'
                '<tr><td colspan="2">丁</td></tr></table>',
                "spans overlap",
            ),
            (
                '<table><img src="images/a.png"/>'
                "<tr><td>甲</td></tr></table>",
                "outside a logical cell",
            ),
            (
                "<table><tr><td><img/></td></tr></table>",
                "requires one exact non-empty src",
            ),
            (
                "<table><tr><td rowspan=\"0\">甲</td></tr></table>",
                "must be positive",
            ),
        )
        for html, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(TableHtmlStructureError, message):
                    parse_table_html_structure(html)

    def test_artifact_role_is_occurrence_identity_not_path_identity(self) -> None:
        self.assertEqual(
            table_media_artifact_role(7, 2),
            "evidence_table_media_000007_000002",
        )
        with self.assertRaises(ValueError):
            table_media_artifact_role(True, 0)


if __name__ == "__main__":
    unittest.main()
