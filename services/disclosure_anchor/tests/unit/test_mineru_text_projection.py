from __future__ import annotations

import unittest

from disclosure_anchor.adapters.parsers.mineru.text_projection import (
    build_mineru_text_projections,
    mineru_serializer_backend,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


def _legacy(
    kind: str,
    *,
    bbox: list[int],
    page_idx: int = 0,
    **fields: object,
) -> dict[str, object]:
    return {
        "type": kind,
        "page_idx": page_idx,
        "bbox": bbox,
        **fields,
    }


def _v2(
    kind: str,
    *,
    bbox: list[int],
    content: dict[str, object],
) -> dict[str, object]:
    return {"type": kind, "bbox": bbox, "content": content}


class MinerUTextProjectionTests(unittest.TestCase):
    def test_backend_identity_selects_official_serializer_lane(self) -> None:
        self.assertEqual(mineru_serializer_backend("pipeline"), "pipeline")
        for backend in (
            "vlm-engine",
            "vlm-http-client",
            "hybrid-engine",
            "hybrid-http-client",
        ):
            with self.subTest(backend=backend):
                self.assertEqual(mineru_serializer_backend(backend), "vlm")
        with self.assertRaises(ParserOutputContractError):
            mineru_serializer_backend("unknown")

    def test_projects_only_provider_proved_markdown_syntax(self) -> None:
        legacy = [
            _legacy(
                "text",
                bbox=[0, 0, 10, 10],
                text=r"\# 普通段落中的 ~、\* 和 $26.58\%$",
                text_level=None,
            )
        ]
        typed = [
            [
                _v2(
                    "paragraph",
                    bbox=[0, 0, 10, 10],
                    content={
                        "paragraph_content": [
                            {
                                "type": "text",
                                "content": "# 普通段落中的 ~、\\* 和 ",
                            },
                            {
                                "type": "equation_inline",
                                "content": r"26.58\%",
                            },
                        ]
                    },
                )
            ]
        ]

        result = build_mineru_text_projections(
            legacy,
            typed,
            serializer_backend="pipeline",
            page_offset=0,
            expected_page_count=1,
        )

        self.assertEqual(
            result.canonical_items[0]["text"],
            r"# 普通段落中的 ~、\* 和 $26.58\%$",
        )
        self.assertEqual(result.legacy_index(0, 0), 0)
        self.assertEqual(legacy[0]["text"], r"\# 普通段落中的 ~、\* 和 $26.58\%$")

    def test_title_level_and_ordinal_mapping_are_exact(self) -> None:
        legacy = [
            _legacy(
                "text",
                bbox=[0, 0, 10, 10],
                text=r"经营\~情况",
                text_level=2,
            ),
            _legacy(
                "text",
                bbox=[0, 20, 10, 30],
                text="正文",
                text_level=None,
            ),
        ]
        typed = [
            [
                _v2(
                    "title",
                    bbox=[0, 0, 10, 10],
                    content={
                        "level": 2,
                        "title_content": [
                            {"type": "text", "content": "经营~情况"}
                        ],
                    },
                ),
                _v2(
                    "paragraph",
                    bbox=[0, 20, 10, 30],
                    content={
                        "paragraph_content": [
                            {"type": "text", "content": "正文"}
                        ]
                    },
                ),
            ]
        ]

        result = build_mineru_text_projections(
            legacy,
            typed,
            serializer_backend="pipeline",
            page_offset=0,
            expected_page_count=1,
        )

        self.assertEqual(
            [item["text"] for item in result.canonical_items],
            ["经营~情况", "正文"],
        )
        self.assertEqual(result.legacy_indices_by_v2_page, ((0, 1),))

    def test_legacy_flattened_list_projects_the_joined_items(self) -> None:
        # Legacy may emit a v2 list as one text block: the "\n"-joined
        # items with the same bbox. The exact-alignment proof still gates
        # the pairing, so the flattening is accepted, not trusted.
        legacy = [
            _legacy(
                "text",
                bbox=[0, 0, 10, 10],
                text="（7）甲事项。  \n备注：乙事项。",
                text_level=None,
            )
        ]
        typed = [
            [
                _v2(
                    "list",
                    bbox=[0, 0, 10, 10],
                    content={
                        "list_type": "text_list",
                        "attribute": "unordered",
                        "list_items": [
                            {
                                "item_type": "text",
                                "item_content": [
                                    {"type": "text", "content": "（7）甲事项。"}
                                ],
                            },
                            {
                                "item_type": "text",
                                "item_content": [
                                    {"type": "text", "content": "备注：乙事项。"}
                                ],
                            },
                        ],
                    },
                )
            ]
        ]

        result = build_mineru_text_projections(
            legacy,
            typed,
            serializer_backend="vlm",
            page_offset=0,
            expected_page_count=1,
        )

        item = result.canonical_items[0]
        self.assertEqual(item["type"], "text")
        self.assertEqual(item["text"], "（7）甲事项。  \n备注：乙事项。")
        self.assertNotIn("list_items", item)

    def test_flattened_list_divergence_stays_fail_closed(self) -> None:
        legacy = [
            _legacy(
                "text",
                bbox=[0, 0, 10, 10],
                text="（7）甲事项。\n备注：丙事项。",
                text_level=None,
            )
        ]
        typed = [
            [
                _v2(
                    "list",
                    bbox=[0, 0, 10, 10],
                    content={
                        "list_type": "text_list",
                        "attribute": "unordered",
                        "list_items": [
                            {
                                "item_type": "text",
                                "item_content": [
                                    {"type": "text", "content": "（7）甲事项。"}
                                ],
                            },
                            {
                                "item_type": "text",
                                "item_content": [
                                    {"type": "text", "content": "备注：乙事项。"}
                                ],
                            },
                        ],
                    },
                )
            ]
        ]

        with self.assertRaises(ParserOutputContractError):
            build_mineru_text_projections(
                legacy,
                typed,
                serializer_backend="vlm",
                page_offset=0,
                expected_page_count=1,
            )

    def test_projects_list_sequence_and_fenced_code_on_distinct_lanes(self) -> None:
        legacy = [
            _legacy(
                "list",
                bbox=[0, 0, 10, 10],
                list_items=[r"项目\~一", r"项目\_二"],
            ),
            _legacy(
                "image",
                bbox=[0, 20, 10, 30],
                image_caption=[""],
                image_footnote=[],
            ),
            _legacy(
                "code",
                bbox=[0, 40, 10, 50],
                code_body="```txt\nvalue_*_raw\n```",
                code_caption=[],
                code_footnote=[],
            ),
        ]
        typed = [
            [
                _v2(
                    "list",
                    bbox=[0, 0, 10, 10],
                    content={
                        "list_items": [
                            {
                                "item_type": "text",
                                "item_content": [
                                    {"type": "text", "content": "项目~一"}
                                ],
                            },
                            {
                                "item_type": "text",
                                "item_content": [
                                    {"type": "text", "content": "项目_二"}
                                ],
                            },
                        ]
                    },
                ),
                _v2(
                    "image",
                    bbox=[0, 20, 10, 30],
                    content={"image_caption": [], "image_footnote": []},
                ),
                _v2(
                    "code",
                    bbox=[0, 40, 10, 50],
                    content={
                        "code_language": "txt",
                        "code_content": [
                            {"type": "text", "content": "value_*_raw"}
                        ],
                        "code_caption": [],
                        "code_footnote": [],
                    },
                ),
            ]
        ]

        result = build_mineru_text_projections(
            legacy,
            typed,
            serializer_backend="pipeline",
            page_offset=0,
            expected_page_count=1,
        )

        self.assertEqual(
            result.canonical_items[0]["list_items"],
            ["项目~一", "项目_二"],
        )
        self.assertEqual(result.canonical_items[1]["image_caption"], [])
        self.assertEqual(result.canonical_items[2]["code_body"], "value_*_raw")

    def test_optional_sequence_field_must_be_absent_on_both_sides(self) -> None:
        legacy = [
            _legacy(
                "code",
                bbox=[0, 0, 10, 10],
                code_body="```txt\nvalue\n```",
                code_caption=[],
            )
        ]
        typed_content = {
            "code_language": "txt",
            "code_content": [{"type": "text", "content": "value"}],
            "code_caption": [],
        }

        result = build_mineru_text_projections(
            legacy,
            [[_v2("code", bbox=[0, 0, 10, 10], content=typed_content)]],
            serializer_backend="vlm",
            page_offset=0,
            expected_page_count=1,
        )

        self.assertNotIn("code_footnote", result.canonical_items[0])
        with self.assertRaises(ParserOutputContractError):
            build_mineru_text_projections(
                [{**legacy[0], "code_footnote": []}],
                [[_v2("code", bbox=[0, 0, 10, 10], content=typed_content)]],
                serializer_backend="vlm",
                page_offset=0,
                expected_page_count=1,
            )

    def test_sequence_partition_rejects_ambiguous_empty_spans(self) -> None:
        legacy = [
            _legacy(
                "image",
                bbox=[0, 0, 10, 10],
                image_caption=["a", "a"],
            )
        ]
        typed = [
            [
                _v2(
                    "image",
                    bbox=[0, 0, 10, 10],
                    content={
                        "image_caption": [
                            {"type": "text", "content": ""},
                            {"type": "text", "content": "a"},
                            {"type": "text", "content": ""},
                            {"type": "text", "content": "a"},
                        ]
                    },
                )
            ]
        ]

        with self.assertRaises(ParserOutputContractError):
            build_mineru_text_projections(
                legacy,
                typed,
                serializer_backend="vlm",
                page_offset=0,
                expected_page_count=1,
            )

    def test_sequence_partition_is_bounded_when_no_solution_exists(self) -> None:
        legacy_values = ["a"] * 24 + ["z"]
        typed_parts = [
            part
            for _ in range(24)
            for part in (
                {"type": "text", "content": ""},
                {"type": "text", "content": "a"},
            )
        ]
        typed_parts.append({"type": "text", "content": "b"})

        with self.assertRaises(ParserOutputContractError):
            build_mineru_text_projections(
                [
                    _legacy(
                        "image",
                        bbox=[0, 0, 10, 10],
                        image_caption=legacy_values,
                    )
                ],
                [
                    [
                        _v2(
                            "image",
                            bbox=[0, 0, 10, 10],
                            content={"image_caption": typed_parts},
                        )
                    ]
                ],
                serializer_backend="vlm",
                page_offset=0,
                expected_page_count=1,
            )

    def test_code_whitespace_uses_the_declared_official_backend_lane(self) -> None:
        legacy = [
            _legacy(
                "code",
                bbox=[0, 0, 10, 10],
                code_body="```txt\nfirst \nsecond\n```",
                code_caption=[],
            )
        ]
        typed = [
            [
                _v2(
                    "code",
                    bbox=[0, 0, 10, 10],
                    content={
                        "code_language": "txt",
                        "code_content": [
                            {"type": "text", "content": "first \nsecond"}
                        ],
                        "code_caption": [],
                    },
                )
            ]
        ]

        vlm = build_mineru_text_projections(
            legacy,
            typed,
            serializer_backend="vlm",
            page_offset=0,
            expected_page_count=1,
        )

        self.assertEqual(vlm.canonical_items[0]["code_body"], "first \nsecond")
        with self.assertRaises(ParserOutputContractError):
            build_mineru_text_projections(
                legacy,
                typed,
                serializer_backend="pipeline",
                page_offset=0,
                expected_page_count=1,
            )

    def test_rejects_count_geometry_type_and_content_drift(self) -> None:
        base_legacy = [
            _legacy(
                "text",
                bbox=[0, 0, 10, 10],
                text="正文",
                text_level=None,
            )
        ]
        base_v2 = [
            _v2(
                "paragraph",
                bbox=[0, 0, 10, 10],
                content={
                    "paragraph_content": [
                        {"type": "text", "content": "正文"}
                    ]
                },
            )
        ]
        cases = [
            [],
            [
                _v2(
                    "paragraph",
                    bbox=[0, 0, 11, 10],
                    content=base_v2[0]["content"],  # type: ignore[arg-type]
                )
            ],
            [
                _v2(
                    "title",
                    bbox=[0, 0, 10, 10],
                    content={
                        "level": 1,
                        "title_content": [
                            {"type": "text", "content": "正文"}
                        ],
                    },
                )
            ],
            [
                _v2(
                    "paragraph",
                    bbox=[0, 0, 10, 10],
                    content={
                        "paragraph_content": [
                            {"type": "text", "content": "异文"}
                        ]
                    },
                )
            ],
        ]

        for typed_page in cases:
            with self.subTest(typed_page=typed_page):
                with self.assertRaises(ParserOutputContractError):
                    build_mineru_text_projections(
                        base_legacy,
                        [typed_page],
                        serializer_backend="pipeline",
                        page_offset=0,
                        expected_page_count=1,
                    )

    def test_rejects_non_monotonic_or_out_of_range_legacy_pages(self) -> None:
        legacy = [
            _legacy(
                "text",
                bbox=[0, 0, 10, 10],
                page_idx=1,
                text="后页",
                text_level=None,
            ),
            _legacy(
                "text",
                bbox=[0, 20, 10, 30],
                page_idx=0,
                text="前页",
                text_level=None,
            ),
        ]

        with self.assertRaises(ParserOutputContractError):
            build_mineru_text_projections(
                legacy,
                [[], []],
                serializer_backend="pipeline",
                page_offset=0,
                expected_page_count=2,
            )


if __name__ == "__main__":
    unittest.main()
