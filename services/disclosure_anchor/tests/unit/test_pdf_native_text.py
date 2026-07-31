"""Poppler layout ancestry and retrieval-run boundary tests."""

from __future__ import annotations

import unittest

from disclosure_anchor.adapters.parsers.pdf_native_text import (
    NativeTextExtractionError,
    native_text_runs,
    parse_pdftotext_bbox,
)


def _xml(body: str) -> str:
    return (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body><doc>'
        '<page width="100" height="200">'
        f"{body}"
        "</page></doc></body></html>"
    )


class NativeTextLayoutTests(unittest.TestCase):
    def test_touching_words_keep_occurrences_and_form_one_run(self) -> None:
        pages = parse_pdftotext_bbox(
            _xml(
                "<flow><block><line>"
                '<word xMin="10" yMin="10" xMax="20" yMax="20">股</word>'
                '<word xMin="20" yMin="10" xMax="50" yMax="20">份变动</word>'
                "</line></block></flow>"
            )
        )

        page = pages[0]
        self.assertEqual([atom.text for atom in page.atoms], ["股", "份变动"])
        self.assertEqual(
            [atom.layout.line_ref for atom in page.atoms],
            [(0, 0, 0), (0, 0, 0)],
        )
        self.assertEqual(
            [run.atom_orders for run in native_text_runs(page)],
            [(0, 1)],
        )

    def test_atom_text_is_verbatim_tounicode_output(self) -> None:
        # Payload fidelity: fullwidth punctuation and compatibility forms
        # stay exactly as the PDF text layer spells them; folding belongs
        # to each consumer's comparison space, never to extraction.
        pages = parse_pdftotext_bbox(
            _xml(
                "<flow><block><line>"
                '<word xMin="10" yMin="10" xMax="60" yMax="20">'
                "战略目标\uff0c人才\uff08元\uff09</word>"
                "</line></block></flow>"
            )
        )

        self.assertEqual(
            [atom.text for atom in pages[0].atoms],
            ["战略目标\uff0c人才\uff08元\uff09"],
        )

    def test_interior_whitespace_is_still_stripped(self) -> None:
        pages = parse_pdftotext_bbox(
            _xml(
                "<flow><block><line>"
                '<word xMin="10" yMin="10" xMax="60" yMax="20">'
                "甲\u00a0乙 丙</word>"
                "</line></block></flow>"
            )
        )

        self.assertEqual([atom.text for atom in pages[0].atoms], ["甲乙丙"])

    def test_gap_and_layout_line_are_hard_run_boundaries(self) -> None:
        page = parse_pdftotext_bbox(
            _xml(
                "<flow><block>"
                "<line>"
                '<word xMin="10" yMin="10" xMax="20" yMax="20">甲</word>'
                '<word xMin="25" yMin="10" xMax="35" yMax="20">乙</word>'
                "</line>"
                "<line>"
                '<word xMin="10" yMin="30" xMax="20" yMax="40">丙</word>'
                "</line>"
                "</block></flow>"
            )
        )[0]

        self.assertEqual(
            [run.atom_orders for run in native_text_runs(page)],
            [(0,), (1,), (2,)],
        )

    def test_geometry_issue_breaks_an_otherwise_touching_line(self) -> None:
        page = parse_pdftotext_bbox(
            _xml(
                "<flow><block><line>"
                '<word xMin="10" yMin="10" xMax="20" yMax="20">甲</word>'
                '<word xMin="20" yMin="10" xMax="20" yMax="20">坏</word>'
                '<word xMin="20" yMin="10" xMax="30" yMax="20">乙</word>'
                "</line></block></flow>"
            )
        )[0]

        self.assertEqual([atom.order for atom in page.atoms], [0, 2])
        self.assertEqual(
            [issue.word_order for issue in page.geometry_issues],
            [1],
        )
        self.assertEqual(
            [run.atom_orders for run in native_text_runs(page)],
            [(0,), (2,)],
        )

    def test_word_outside_flow_block_line_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            NativeTextExtractionError,
            "outside Poppler line structure",
        ):
            parse_pdftotext_bbox(
                _xml(
                    '<word xMin="10" yMin="10" xMax="20" yMax="20">孤</word>'
                )
            )


if __name__ == "__main__":
    unittest.main()
