"""Fail-loud conflict paths of the MinerU/PDF heading structure proof.

Every case here exercises ``build_mineru_structure_proof`` through its public
input/output contract only: typed evidence in, proof out.  The assertions pair
each conflict relation with the published structure it must keep out.
"""

from __future__ import annotations

import unittest
from typing import Any

from disclosure_anchor.adapters.parsers.mineru.content_list_contract import (
    mineru_provider_item_sha256,
)
from tests.unit._native_support import (
    build_proof_with_auto_native,
)
from disclosure_anchor.adapters.parsers.mineru.text_projection import (
    build_mineru_text_projections,
)
from disclosure_anchor.adapters.parsers.pdf_native_structure import (
    NativeBookmark,
    NativeMarkedObject,
    NativeStructureIndex,
    NativeStructureNode,
)
from disclosure_anchor.application.contracts.document_structure import (
    DocumentStructureContractError,
    validate_document_structure,
)

from tests.unit._native_index import (
    marked_object,
    native_bookmark,
    native_index,
    native_node,
)


SOURCE_PDF_SHA256 = "sha256:" + "a" * 64
MCID_BASE = 10


def stacked_bbox(row: int) -> list[int]:
    """Non-overlapping page geometry so each object binds one carrier."""

    return [100, 100 + row * 60, 300, 130 + row * 60]


def text_item(
    text: str,
    *,
    row: int,
    page_idx: int = 0,
    bbox: list[int] | None = None,
    level: int | None = None,
    drop_bbox: bool = False,
    drop_page: bool = False,
) -> dict[str, Any]:
    item: dict[str, Any] = {"type": "text", "text": text}
    if not drop_page:
        item["page_idx"] = page_idx
    if not drop_bbox:
        item["bbox"] = list(bbox if bbox is not None else stacked_bbox(row))
    if level is not None:
        item["text_level"] = level
    return item


def body(
    texts: tuple[str, ...],
    *,
    levels: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    return [
        text_item(text, row=row, level=(levels or {}).get(row))
        for row, text in enumerate(texts)
    ]


def marked_content(items: list[dict[str, Any]]) -> list[NativeMarkedObject]:
    return [
        marked_object(
            item["page_idx"],
            MCID_BASE + index,
            index,
            text=item["text"],
            bbox=item["bbox"],
        )
        for index, item in enumerate(items)
        if "page_idx" in item and "bbox" in item
    ]


def heading_node(
    node_id: int,
    role: str,
    sources: tuple[int, ...],
    *,
    segment: str = "native_1",
    ancestor_roles: tuple[str, ...] = (),
    ancestors: tuple[int, ...] = (),
) -> NativeStructureNode:
    return native_node(
        node_id,
        role,
        [(0, MCID_BASE + index) for index in sources],
        segment_id=segment,
        ancestor_roles=ancestor_roles,
        ancestor_node_ids=ancestors,
    )


def bookmark(
    order: int,
    level: int,
    title: str,
    *,
    page_idx: int | None = 0,
    destination_y: float | None = None,
) -> NativeBookmark:
    return native_bookmark(
        order,
        level,
        title,
        page_idx=page_idx,
        destination_y=destination_y,
    )


def native_structure(
    items: list[dict[str, Any]],
    *,
    nodes: tuple[NativeStructureNode, ...] = (),
    bookmarks: tuple[NativeBookmark, ...] = (),
    page_count: int = 1,
) -> NativeStructureIndex:
    return native_index(
        page_count=page_count,
        nodes=nodes,
        bookmarks=bookmarks,
        marked_objects=marked_content(items),
    )


def proof_for(
    content_list: list[dict[str, Any]],
    *,
    nodes: tuple[NativeStructureNode, ...] = (),
    bookmarks: tuple[NativeBookmark, ...] = (),
    page_count: int = 1,
    heading_display_texts: tuple[str, ...] = (),
) -> dict[str, Any]:
    return build_proof_with_auto_native(
        native=native_structure(
            content_list,
            nodes=nodes,
            bookmarks=bookmarks,
            page_count=page_count,
        ),
        content_list=content_list,
        source_pdf_sha256=SOURCE_PDF_SHA256,
        heading_display_texts=heading_display_texts,
    )


def title_block(text: str, row: int, *, level: int = 1) -> dict[str, Any]:
    return {
        "type": "title",
        "bbox": stacked_bbox(row),
        "content": {
            "level": level,
            "title_content": [{"type": "text", "content": text}],
        },
    }


def paragraph_block(text: str, row: int) -> dict[str, Any]:
    return {
        "type": "paragraph",
        "bbox": stacked_bbox(row),
        "content": {"paragraph_content": [{"type": "text", "content": text}]},
    }


def v2_proof(
    content_list: list[dict[str, Any]],
    v2_pages: list[list[dict[str, Any]]],
    *,
    bookmarks: tuple[NativeBookmark, ...] = (),
    page_count: int = 1,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    projections = build_mineru_text_projections(
        content_list,
        v2_pages,
        serializer_backend="pipeline",
        page_offset=0,
        expected_page_count=page_count,
    )
    canonical = list(projections.canonical_items)
    return (
        build_proof_with_auto_native(
            native=native_structure(
                canonical,
                bookmarks=bookmarks,
                page_count=page_count,
            ),
            content_list=canonical,
            source_pdf_sha256=SOURCE_PDF_SHA256,
            content_list_v2=v2_pages,
            text_projections=projections,
        ),
        canonical,
    )


def relations(proof: dict[str, Any]) -> list[str]:
    return [conflict["relation"] for conflict in proof["conflicts"]]


def conflicts_named(proof: dict[str, Any], relation: str) -> list[dict[str, Any]]:
    return [
        conflict
        for conflict in proof["conflicts"]
        if conflict["relation"] == relation
    ]


def heading_shape(proof: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [
        (
            heading["heading_level"],
            heading["parent_node_id"],
            heading["propagates"],
            heading["section_span"],
            sorted(ref["source_item_index"] for ref in heading["source_refs"]),
        )
        for heading in proof["headings"]
    ]


def heading_sources(proof: dict[str, Any]) -> set[int]:
    return {
        ref["source_item_index"]
        for heading in proof["headings"]
        for ref in heading["source_refs"]
    }


def elements_for(content_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **item,
            "kind": "text",
            "raw_kind": item["type"],
            "source_item_index": index,
            "source_item_sha256": mineru_provider_item_sha256(item),
        }
        for index, item in enumerate(content_list)
    ]


class StructureProofConflictTests(unittest.TestCase):
    def test_invalid_bookmarks_are_recorded_and_bind_no_heading(self) -> None:
        content = body(("第一节财务概览", "第二节风险因素"))
        valid = bookmark(0, 1, "第一节财务概览", destination_y=115)
        # A bookmark binds only with an independent witness; the valid
        # entry is corroborated by the matching StructTree heading here.
        corroborating = (heading_node(1, "H1", (0,)),)
        for label, broken in (
            ("level_above_range", bookmark(1, 33, "第二节风险因素")),
            ("page_unresolved", bookmark(1, 2, "第二节风险因素", page_idx=None)),
        ):
            with self.subTest(label=label):
                proof = proof_for(
                    content,
                    nodes=corroborating,
                    bookmarks=(valid, broken),
                )

                self.assertEqual(
                    conflicts_named(proof, "bookmark_invalid"),
                    [
                        {
                            "relation": "bookmark_invalid",
                            "bookmark_order": 1,
                            "source_item_indices": [],
                        }
                    ],
                )
                self.assertEqual(
                    heading_shape(proof),
                    [(1, None, True, [0, 1], [0])],
                )
                self.assertNotIn(1, heading_sources(proof))
                self.assertEqual(proof["coverage"]["bookmark_candidates"], 2)
                validate_document_structure(proof, elements=elements_for(content))

    def test_overlapping_native_headings_reject_the_later_claim(self) -> None:
        content = body(("公司治理", "内部控制", "关联交易"))
        proof = proof_for(
            content,
            nodes=(
                heading_node(1, "H1", (0, 1)),
                heading_node(
                    2,
                    "H2",
                    (1, 2),
                    ancestor_roles=("H1",),
                    ancestors=(1,),
                ),
            ),
        )

        self.assertEqual(
            conflicts_named(proof, "heading_boundary_conflict"),
            [
                {
                    "relation": "heading_boundary_conflict",
                    "source_item_indices": [1, 2],
                }
            ],
        )
        self.assertEqual(heading_shape(proof), [])
        self.assertNotIn(2, heading_sources(proof))
        self.assertEqual(proof["coverage"]["proven_heading_nodes"], 0)
        validate_document_structure(proof, elements=elements_for(content))

    def test_backward_native_parent_edge_is_rejected(self) -> None:
        content = body(("子标题", "正文段落", "章标题"))
        proof = proof_for(
            content,
            nodes=(
                heading_node(1, "H1", (2,)),
                heading_node(
                    2,
                    "H2",
                    (0,),
                    ancestor_roles=("H1",),
                    ancestors=(1,),
                ),
            ),
        )

        self.assertEqual(
            conflicts_named(proof, "heading_parent_invalid"),
            [
                {
                    "relation": "heading_parent_invalid",
                    "source_item_indices": [0, 2],
                }
            ],
        )
        self.assertEqual(
            heading_shape(proof),
            [
                (1, None, True, [0, 1], [0]),
                (1, None, True, [2, 2], [2]),
            ],
        )
        validate_document_structure(proof, elements=elements_for(content))

        forward = proof_for(
            content,
            nodes=(
                heading_node(1, "H1", (0,)),
                heading_node(
                    2,
                    "H2",
                    (2,),
                    ancestor_roles=("H1",),
                    ancestors=(1,),
                ),
            ),
        )

        self.assertEqual(relations(forward), [])
        self.assertEqual(
            heading_shape(forward),
            [
                (1, None, True, [0, 2], [0]),
                (2, 1, True, [2, 2], [2]),
            ],
        )

    def test_non_propagating_native_parent_blocks_inheritance(self) -> None:
        content = body(("目录中的章节名", "正文中的子标题"))
        proof = proof_for(
            content,
            nodes=(
                heading_node(
                    1,
                    "H1",
                    (0,),
                    ancestor_roles=("TOC",),
                    ancestors=(9,),
                ),
                heading_node(
                    2,
                    "H2",
                    (1,),
                    ancestor_roles=("H1",),
                    ancestors=(1,),
                ),
            ),
        )

        self.assertEqual(
            conflicts_named(proof, "native_heading_non_section_ancestry"),
            [
                {
                    "relation": "native_heading_non_section_ancestry",
                    "native_roles": ["TOC"],
                    "source_item_indices": [0],
                }
            ],
        )
        self.assertEqual(
            [
                conflict["source_item_indices"]
                for conflict in conflicts_named(
                    proof,
                    "provider_heading_unproved",
                )
            ],
            [[0]],
        )
        self.assertEqual(
            heading_shape(proof),
            [
                (1, None, True, [1, 1], [1]),
            ],
        )
        validate_document_structure(proof, elements=elements_for(content))

        sectioned = proof_for(
            content,
            nodes=(
                heading_node(
                    1,
                    "H1",
                    (0,),
                    ancestor_roles=("Sect",),
                    ancestors=(9,),
                ),
                heading_node(
                    2,
                    "H2",
                    (1,),
                    ancestor_roles=("H1",),
                    ancestors=(1,),
                ),
            ),
        )

        self.assertEqual(relations(sectioned), [])
        self.assertEqual(
            heading_shape(sectioned),
            [
                (1, None, True, [0, 1], [0]),
                (2, 1, True, [1, 1], [1]),
            ],
        )

    def test_native_edge_across_an_intervening_root_is_rejected(self) -> None:
        content = body(("第一章", "第二章", "第一章第一节"))
        proof = proof_for(
            content,
            nodes=(
                heading_node(1, "H1", (0,)),
                heading_node(2, "H1", (1,)),
                heading_node(
                    3,
                    "H2",
                    (2,),
                    ancestor_roles=("H1",),
                    ancestors=(1,),
                ),
            ),
        )

        self.assertEqual(
            conflicts_named(proof, "heading_parent_discontinuous"),
            [
                {
                    "relation": "heading_parent_discontinuous",
                    "source_item_indices": [2],
                }
            ],
        )
        self.assertEqual(
            heading_shape(proof),
            [
                (1, None, True, [0, 0], [0]),
                (1, None, True, [1, 1], [1]),
                (1, None, True, [2, 2], [2]),
            ],
        )
        validate_document_structure(proof, elements=elements_for(content))

        adjacent = proof_for(
            content,
            nodes=(
                heading_node(1, "H1", (0,)),
                heading_node(
                    3,
                    "H2",
                    (2,),
                    ancestor_roles=("H1",),
                    ancestors=(1,),
                ),
            ),
        )

        self.assertEqual(relations(adjacent), [])
        self.assertEqual(
            heading_shape(adjacent),
            [
                (1, None, True, [0, 2], [0]),
                (2, 1, True, [2, 2], [2]),
            ],
        )

    def test_child_of_a_demoted_anchor_loses_its_edge(self) -> None:
        content = body(("第一章", "第二章", "第一章第一节", "第一章第一节之一"))
        proof = proof_for(
            content,
            nodes=(
                heading_node(1, "H1", (0,)),
                heading_node(2, "H1", (1,)),
                heading_node(
                    3,
                    "H2",
                    (2,),
                    ancestor_roles=("H1",),
                    ancestors=(1,),
                ),
                heading_node(
                    4,
                    "H3",
                    (3,),
                    ancestor_roles=("H1", "H2"),
                    ancestors=(1, 3),
                ),
            ),
        )

        self.assertEqual(conflicts_named(proof, "heading_parent_anchor_only"), [])
        self.assertEqual(
            [
                conflict["source_item_indices"]
                for conflict in conflicts_named(
                    proof,
                    "heading_parent_discontinuous",
                )
            ],
            [[2]],
        )
        self.assertEqual(
            heading_shape(proof),
            [
                (1, None, True, [0, 0], [0]),
                (1, None, True, [1, 1], [1]),
                (1, None, True, [2, 3], [2]),
                (2, 3, True, [3, 3], [3]),
            ],
        )
        validate_document_structure(proof, elements=elements_for(content))

    def test_disagreeing_bookmark_and_provider_parents_drop_the_edge(self) -> None:
        texts = ("书签父章节", "供方父章节", "冲突子标题")
        bookmarks = (
            bookmark(0, 1, texts[0], destination_y=115),
            bookmark(1, 2, texts[2], destination_y=235),
        )
        proof, canonical = v2_proof(
            body(texts, levels={0: 1, 1: 1, 2: 2}),
            [
                [
                    title_block(texts[0], 0),
                    title_block(texts[1], 1),
                    title_block(texts[2], 2, level=2),
                ]
            ],
            bookmarks=bookmarks,
        )

        self.assertEqual(conflicts_named(proof, "heading_parent_conflict"), [])
        self.assertEqual(
            heading_shape(proof),
            [
                (1, None, True, [0, 0], [0]),
                (1, None, True, [1, 1], [1]),
                (1, None, True, [2, 2], [2]),
            ],
        )
        self.assertEqual(
            proof["headings"][0]["evidence_kinds"],
            ["bookmark", "mineru_v2_title", "native_layout"],
        )
        self.assertEqual(
            proof["headings"][2]["evidence_kinds"],
            ["bookmark", "mineru_v2_title", "native_layout"],
        )
        validate_document_structure(proof, elements=elements_for(canonical))

        agreeing, agreeing_content = v2_proof(
            body(texts, levels={0: 1, 2: 2}),
            [
                [
                    title_block(texts[0], 0),
                    paragraph_block(texts[1], 1),
                    title_block(texts[2], 2, level=2),
                ]
            ],
            bookmarks=bookmarks,
        )

        self.assertEqual(relations(agreeing), [])
        self.assertEqual(
            heading_shape(agreeing),
            [
                (1, None, True, [0, 1], [0]),
                (1, None, True, [2, 2], [2]),
            ],
        )
        validate_document_structure(
            agreeing,
            elements=elements_for(agreeing_content),
        )

    def test_carrier_without_source_geometry_is_recorded_not_dropped(self) -> None:
        for label, broken in (
            ("bbox_missing", {"drop_bbox": True}),
            ("page_missing", {"drop_page": True}),
            ("bbox_degenerate", {"bbox": [100, 160, 100, 190]}),
            ("bbox_outside_page", {"bbox": [100, 160, 2000, 190]}),
            ("page_negative", {"page_idx": -1}),
        ):
            with self.subTest(label=label):
                content = [
                    text_item("正常载体", row=0),
                    text_item("几何缺失的候选标题", row=1, level=1, **broken),
                ]
                proof = proof_for(content)

                self.assertEqual(
                    conflicts_named(proof, "carrier_geometry_unbound"),
                    [
                        {
                            "relation": "carrier_geometry_unbound",
                            "source_item_indices": [1],
                        }
                    ],
                )
                self.assertEqual(relations(proof), ["carrier_geometry_unbound"])
                self.assertEqual(proof["headings"], [])
                self.assertEqual(
                    proof["coverage"]["provider_heading_candidates"],
                    0,
                )
                validate_document_structure(proof, elements=elements_for(content))

        bound = [
            text_item("正常载体", row=0),
            text_item("几何完整的候选标题", row=1, level=1),
        ]
        proof = proof_for(bound)

        self.assertEqual(relations(proof), ["provider_heading_unproved"])
        self.assertEqual(
            conflicts_named(proof, "provider_heading_unproved"),
            [
                {
                    "relation": "provider_heading_unproved",
                    "source_item_indices": [1],
                }
            ],
        )
        self.assertEqual(proof["coverage"]["provider_heading_candidates"], 1)
        self.assertEqual(proof["headings"], [])


class StructureProofCleanEvidenceTests(unittest.TestCase):
    def test_clean_struct_tree_publishes_a_closed_heading_tree(self) -> None:
        content = body(("第一章总则", "第一节适用范围", "第二节术语", "正文段落"))
        proof = proof_for(
            content,
            nodes=(
                heading_node(1, "H1", (0,)),
                heading_node(
                    2,
                    "H2",
                    (1,),
                    ancestor_roles=("H1",),
                    ancestors=(1,),
                ),
                heading_node(
                    3,
                    "H2",
                    (2,),
                    ancestor_roles=("H1",),
                    ancestors=(1,),
                ),
            ),
        )

        self.assertEqual(relations(proof), [])
        self.assertEqual(
            heading_shape(proof),
            [
                (1, None, True, [0, 3], [0]),
                (2, 1, True, [1, 1], [1]),
                (2, 1, True, [2, 3], [2]),
            ],
        )
        self.assertEqual(
            [heading["evidence_kinds"] for heading in proof["headings"]],
            [["native_layout", "struct_tree"]] * 3,
        )
        self.assertEqual(
            [
                (heading["native_role"], heading["native_segment_id"])
                for heading in proof["headings"]
            ],
            [("H1", "native_1"), ("H2", "native_1"), ("H2", "native_1")],
        )
        self.assertEqual(
            proof["coverage"],
            {
                "provider_heading_candidates": 0,
                "native_heading_candidates": 3,
                "bookmark_candidates": 0,
                "mineru_v2_title_candidates": 0,
                "proven_heading_nodes": 3,
                "owner_scope_breaks": 0,
                "page_frame_groups": 0,
            },
        )
        validate_document_structure(proof, elements=elements_for(content))

    def test_carrier_identity_follows_the_raw_provider_items(self) -> None:
        # The serializer lane may rewrite canonical text (escape cleanup);
        # carrier identity must keep hashing the raw provider items the
        # mapper stamps onto elements, or the proof forks from the IR.
        raw = body((r"经营\~情况",))
        canonical = body(("经营~情况",))
        proof = build_proof_with_auto_native(
            native=native_structure(canonical),
            content_list=canonical,
            source_pdf_sha256=SOURCE_PDF_SHA256,
            identity_content_list=raw,
        )

        validate_document_structure(proof, elements=elements_for(raw))
        with self.assertRaises(DocumentStructureContractError):
            validate_document_structure(
                proof,
                elements=elements_for(canonical),
            )

    def test_clean_bookmarks_publish_a_closed_heading_tree(self) -> None:
        texts = ("第一章总则", "第一节适用范围", "正文段落", "第二章财务")
        proof, content = v2_proof(
            body(texts, levels={0: 1, 1: 2, 3: 1}),
            [
                [
                    title_block(texts[0], 0),
                    title_block(texts[1], 1, level=2),
                    paragraph_block(texts[2], 2),
                    title_block(texts[3], 3),
                ]
            ],
            bookmarks=(
                bookmark(0, 1, texts[0], destination_y=115),
                bookmark(1, 2, texts[1], destination_y=175),
                bookmark(2, 1, texts[3], destination_y=295),
            ),
        )

        self.assertEqual(relations(proof), [])
        self.assertEqual(
            heading_shape(proof),
            [
                (1, None, True, [0, 0], [0]),
                (1, None, True, [1, 2], [1]),
                (1, None, True, [3, 3], [3]),
            ],
        )
        self.assertEqual(
            [heading["evidence_kinds"] for heading in proof["headings"]],
            [["bookmark", "mineru_v2_title", "native_layout"]] * 3,
        )
        self.assertEqual(heading_sources(proof), {0, 1, 3})
        self.assertEqual(proof["coverage"]["proven_heading_nodes"], 3)
        validate_document_structure(proof, elements=elements_for(content))


if __name__ == "__main__":
    unittest.main()
