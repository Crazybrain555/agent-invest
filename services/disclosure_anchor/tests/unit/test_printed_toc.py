"""Printed-TOC witness: line grammar, offset fitting, per-entry validation."""

from __future__ import annotations

import unittest

from disclosure_anchor.adapters.parsers.pdf_native_text import (
    NativeTextAtom,
    NativeTextLayoutRef,
    NativeTextPage,
)
from disclosure_anchor.adapters.parsers.printed_toc import (
    printed_toc_witness,
)


def _atom(
    page_idx: int,
    order: int,
    text: str,
    *,
    line: int,
    x0: float,
    x1: float,
) -> NativeTextAtom:
    return NativeTextAtom(
        page_idx=page_idx,
        order=order,
        bbox=(x0, 100.0 + line * 30.0, x1, 120.0 + line * 30.0),
        char_span=(0, len(text)),
        text=text,
        layout=NativeTextLayoutRef(
            flow_index=0,
            block_index=0,
            line_index=line,
            word_index=0,
        ),
    )


def _page(page_idx: int, atoms: list[NativeTextAtom]) -> NativeTextPage:
    return NativeTextPage(
        page_idx=page_idx,
        width=600.0,
        height=800.0,
        text="".join(atom.text for atom in atoms),
        atoms=tuple(atoms),
    )


def _toc_page(entries: list[tuple[str, str]], *, leaders: bool = True) -> NativeTextPage:
    atoms: list[NativeTextAtom] = []
    order = 0
    for line, (title, number) in enumerate(entries):
        atoms.append(_atom(0, order, title, line=line, x0=60, x1=240))
        order += 1
        if leaders:
            atoms.append(_atom(0, order, "." * 20, line=line, x0=245, x1=520))
            order += 1
        atoms.append(_atom(0, order, number, line=line, x0=540, x1=560))
        order += 1
    return _page(0, atoms)


class PrintedTocWitnessTests(unittest.TestCase):
    def test_leader_lines_fit_one_offset_and_validate_each_entry(self) -> None:
        # Printed page 2/6/9 live at physical idx 1/5/8: offset -1.
        witness = printed_toc_witness(
            (
                _toc_page(
                    [("第一节 概览", "2"), ("第二节 经营", "6"), ("第三节 治理", "9")]
                ),
            ),
            carrier_pages_by_comparison={
                "第一节概览": (1,),
                "第二节经营": (5,),
                "第三节治理": (8,),
            },
        )

        assert witness is not None
        self.assertEqual(witness.page_offset, -1)
        self.assertEqual(len(witness.entries), 3)
        self.assertTrue(witness.corroborates("第二节经营", 5))
        self.assertFalse(witness.corroborates("第二节经营", 8))
        self.assertFalse(witness.corroborates("第四节未列", 5))

    def test_entry_missing_on_its_declared_page_is_rejected_alone(self) -> None:
        witness = printed_toc_witness(
            (
                _toc_page(
                    [
                        ("第一节 概览", "2"),
                        ("第二节 经营", "6"),
                        ("第三节 治理", "9"),
                        ("孤条目", "12"),
                    ]
                ),
            ),
            carrier_pages_by_comparison={
                "第一节概览": (1,),
                "第二节经营": (5,),
                "第三节治理": (8,),
                # 孤条目 exists physically far from its declared page.
                "孤条目": (30,),
            },
        )

        assert witness is not None
        self.assertEqual(len(witness.entries), 3)
        self.assertFalse(witness.corroborates("孤条目", 30))

    def test_body_lines_ending_with_a_year_are_not_a_toc(self) -> None:
        # No leaders and no wide gap: ordinary prose ending with digits.
        atoms = []
        for line, text in enumerate(
            ("营业收入较上年增长", "净利润率保持稳定", "详见附注")
        ):
            atoms.append(_atom(0, line * 2, text, line=line, x0=60, x1=430))
            atoms.append(
                _atom(0, line * 2 + 1, "2024", line=line, x0=433, x1=470)
            )
        witness = printed_toc_witness(
            (_page(0, atoms),),
            carrier_pages_by_comparison={"营业收入较上年增长": (0,)},
        )

        self.assertIsNone(witness)

    def test_descending_declared_pages_reject_the_group(self) -> None:
        witness = printed_toc_witness(
            (
                _toc_page(
                    [("第一节 概览", "9"), ("第二节 经营", "6"), ("第三节 治理", "2")]
                ),
            ),
            carrier_pages_by_comparison={
                "第一节概览": (8,),
                "第二节经营": (5,),
                "第三节治理": (1,),
            },
        )

        self.assertIsNone(witness)

    def test_wide_gap_without_leaders_is_a_toc_line(self) -> None:
        witness = printed_toc_witness(
            (
                _toc_page(
                    [("第一节 概览", "2"), ("第二节 经营", "6"), ("第三节 治理", "9")],
                    leaders=False,
                ),
            ),
            carrier_pages_by_comparison={
                "第一节概览": (1,),
                "第二节经营": (5,),
                "第三节治理": (8,),
            },
        )

        assert witness is not None
        self.assertEqual(len(witness.entries), 3)


if __name__ == "__main__":
    unittest.main()
