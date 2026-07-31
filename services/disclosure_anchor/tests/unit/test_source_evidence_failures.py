"""Fail-loud coverage for the source-evidence conservation ledger.

Every test drives a public entry point (``reconcile_source_evidence``,
``validate_source_evidence_ledger``, ``resolve_middle_table_roles``,
``resolve_ir_text_selector`` …), asserts the exact ``reason_code`` of the
refusal, and keeps the adjacent legal fixture passing so the guard cannot be
satisfied by rejecting everything.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import hashlib
import json
import unittest

from disclosure_anchor.adapters.parsers.mineru.artifact_reader import (
    MiddleTableRoleHint,
    MinerUMiddleArtifact,
)
from disclosure_anchor.adapters.parsers.mineru.source_evidence import (
    ExtractorIdentity,
    ResolvedTableRole,
    SourceEvidenceContractError,
    iter_mineru_text_carriers,
    mineru_visual_occurrences,
    reconcile_source_evidence,
    resolve_ir_text_selector,
    resolve_middle_table_roles,
    table_role_values_by_item,
    validate_mapped_element_bindings,
    validate_source_evidence_ledger,
)
from disclosure_anchor.adapters.parsers.pdf_native_structure import (
    NATIVE_PDF_STRUCTURE_VERSION,
)
from disclosure_anchor.adapters.parsers.pdf_native_text import (
    NativeTextAtom,
    NativeTextGeometryIssue,
    NativeTextLayoutRef,
    NativeTextPage,
)
from disclosure_anchor.adapters.parsers.pdf_visual_evidence import (
    PNG_OPTIONS,
    RENDERER_IDENTITY,
    RENDER_OPTIONS,
    VisualPageEvidence,
)

SOURCE_PDF_SHA256 = "sha256:" + "a" * 64
TYPED_ARTIFACT_SHA256 = "sha256:" + "c" * 64
TEXT_PROJECTION = "nfkc-strip-whitespace.v1"
MIDDLE_SHA256 = "sha256:" + "d" * 64
Ledger = dict[str, Any]
Mutate = Callable[[Ledger], None]


def _artifact(items: Sequence[Mapping[str, Any]]) -> tuple[bytes, str]:
    payload = json.dumps(
        list(items),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return payload, "sha256:" + hashlib.sha256(payload).hexdigest()


def _native_structure(page_count: int = 1) -> dict[str, Any]:
    return {
        "contract_version": NATIVE_PDF_STRUCTURE_VERSION,
        "source_pdf_sha256": SOURCE_PDF_SHA256,
        "source_pdf_page_count": page_count,
        "native_status": "untagged",
        "pdfium_tagged": False,
        "role_map": {},
        "segments": [],
        "nodes": [],
        "marked_content": [],
        "bookmarks": [],
        "diagnostics": {
            "parent_conflicts": 0,
            "unresolved": [],
            "root_reachable_nodes": 0,
            "visible_mcid_anchors": 0,
            "marked_content_objects": 0,
            "referenced_mcid_refs": 0,
            "resolved_mcid_refs": 0,
            "unresolved_mcid_refs": [],
            "object_issues": [],
        },
    }


def _page(
    *atoms: tuple[str, tuple[float, float, float, float]],
    page_idx: int = 0,
    width: float = 100.0,
    height: float = 200.0,
) -> NativeTextPage:
    offset = 0
    parts: list[str] = []
    native_atoms: list[NativeTextAtom] = []
    for order, (text, bbox) in enumerate(atoms):
        if parts:
            parts.append(" ")
            offset += 1
        start = offset
        parts.append(text)
        offset += len(text)
        native_atoms.append(
            NativeTextAtom(
                page_idx=page_idx,
                order=order,
                bbox=bbox,
                char_span=(start, offset),
                text=text,
                layout=NativeTextLayoutRef(0, order, 0, 0),
            )
        )
    return NativeTextPage(
        page_idx,
        width,
        height,
        "".join(parts),
        tuple(native_atoms),
    )


def _visual(page_idx: int = 0) -> VisualPageEvidence:
    return VisualPageEvidence(
        page_idx=page_idx,
        artifact_role=f"source_page_visual_{page_idx + 1:06d}",
        artifact_path=Path(f"/unused/page_{page_idx + 1:06d}.png"),
        sha256="sha256:" + f"{page_idx + 1:064x}",
        size_bytes=123,
        pixel_width=100,
        pixel_height=200,
        media_type="image/png",
        renderer=RENDERER_IDENTITY,
        render_options=RENDER_OPTIONS,
        png_options=PNG_OPTIONS,
    )


def _region(
    bbox: tuple[float, float, float, float],
    *,
    page_idx: int = 0,
    component_idx: int = 0,
) -> VisualPageEvidence:
    return replace(
        _visual(page_idx),
        artifact_role=(
            f"source_bbox_visual_{page_idx + 1:06d}_{component_idx + 1:06d}"
        ),
        bbox=bbox,
    )


def _occurrence(
    source_item_index: int,
    bbox: tuple[float, float, float, float],
    *,
    page_idx: int = 0,
) -> VisualPageEvidence:
    return replace(
        _visual(page_idx),
        artifact_role=f"source_visual_occurrence_{source_item_index:06d}",
        sha256="sha256:" + f"{source_item_index + 100:064x}",
        bbox=bbox,
    )


def _auto_occurrences(
    items: Sequence[Mapping[str, Any]],
) -> tuple[VisualPageEvidence, ...]:
    return tuple(
        _occurrence(
            source_item_index,
            tuple(float(value) for value in item["bbox"]),
            page_idx=int(item["page_idx"]),
        )
        for source_item_index, item in enumerate(items)
        if item.get("type") in {"image", "chart"}
        and isinstance(item.get("bbox"), list)
        and isinstance(item.get("page_idx"), int)
    )


def _build(
    items: Sequence[Mapping[str, Any]],
    pages: Sequence[NativeTextPage],
    *,
    page_count: int = 1,
    middle_artifact: MinerUMiddleArtifact | None = None,
    native_structure: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> tuple[Ledger, dict[str, Any]]:
    """Reconcile one valid ledger plus the kwargs that re-validate it."""

    payload, digest = _artifact(items)
    structure = (
        dict(native_structure)
        if native_structure is not None
        else _native_structure(page_count)
    )
    kwargs.setdefault("visual_occurrence_artifacts", _auto_occurrences(items))
    ledger = reconcile_source_evidence(
        source_pdf_sha256=SOURCE_PDF_SHA256,
        source_pdf_page_count=page_count,
        source_extractor=ExtractorIdentity("pdftotext", "25.06.0"),
        source_pages=tuple(pages),
        native_structure=structure,
        mineru_content_list_bytes=payload,
        expected_mineru_artifact_sha256=digest,
        canonical_content_list=list(items),
        expected_mineru_typed_artifact_sha256=TYPED_ARTIFACT_SHA256,
        mineru_extractor=ExtractorIdentity("mineru", "3.4.0"),
        middle_artifact=middle_artifact,
        **kwargs,
    )
    validate_kwargs: dict[str, Any] = {
        "expected_source_pdf_sha256": SOURCE_PDF_SHA256,
        "expected_source_pdf_page_count": page_count,
        "expected_mineru_artifact_sha256": digest,
        "mineru_content_list_bytes": payload,
        "canonical_content_list": list(items),
        "expected_mineru_typed_artifact_sha256": TYPED_ARTIFACT_SHA256,
        "native_structure": structure,
        "mineru_middle_artifact": middle_artifact,
    }
    return ledger, validate_kwargs


def _middle(
    *hints: MiddleTableRoleHint,
    page_count: int = 1,
) -> MinerUMiddleArtifact:
    return MinerUMiddleArtifact(
        sha256=MIDDLE_SHA256,
        version="3.4.0",
        backend="vlm",
        page_count=page_count,
        table_roles=tuple(hints),
    )


def _hint(
    *,
    role_bbox: tuple[float, float, float, float] = (120, 720, 880, 760),
    parent_bbox: tuple[float, float, float, float] = (100, 200, 900, 700),
    field_index: int = 0,
    provider_deleted: bool = True,
) -> MiddleTableRoleHint:
    return MiddleTableRoleHint(
        page_idx=0,
        parent_bbox=parent_bbox,
        field="table_footnote",
        field_index=field_index,
        role_bbox=role_bbox,
        provider_deleted=provider_deleted,
    )


def _table_item(footnotes: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "type": "table",
        "page_idx": 0,
        "bbox": [100, 200, 900, 700],
        "table_caption": [],
        "table_body": "<table><tr><td>净利润</td></tr></table>",
        "table_footnote": list(footnotes),
    }


def _role_page(
    *atoms: tuple[str, tuple[float, float, float, float]],
) -> NativeTextPage:
    return _page(*atoms, width=1000.0, height=1000.0)


class LedgerFailureCase(unittest.TestCase):
    """Shared tamper/refusal helpers; holds no tests of its own."""

    def assert_accepts(self, ledger: Ledger, kwargs: Mapping[str, Any]) -> None:
        self.assertIs(
            validate_source_evidence_ledger(ledger, **kwargs),
            ledger,
        )

    def assert_refuses(
        self,
        ledger: Ledger,
        kwargs: Mapping[str, Any],
        reason_code: str,
        mutate: Mutate,
    ) -> None:
        broken = deepcopy(ledger)
        mutate(broken)
        with self.assertRaises(SourceEvidenceContractError) as raised:
            validate_source_evidence_ledger(broken, **kwargs)
        self.assertEqual(raised.exception.reason_code, reason_code)

    def assert_each_refusal(
        self,
        ledger: Ledger,
        kwargs: Mapping[str, Any],
        cases: Sequence[tuple[str, str, Mutate]],
    ) -> None:
        self.assert_accepts(ledger, kwargs)
        for name, reason_code, mutate in cases:
            with self.subTest(case=name):
                self.assert_refuses(ledger, kwargs, reason_code, mutate)


def _support(ledger: Ledger, index: int = 0) -> dict[str, Any]:
    record: dict[str, Any] = ledger["carrier_support"][index]
    support: dict[str, Any] = record["support"]
    return support


class CarrierSupportFailureTests(LedgerFailureCase):
    def _native_case(self) -> tuple[Ledger, dict[str, Any]]:
        items = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 100],
                "text": "净利润",
            }
        ]
        return _build(items, (_page(("净利润", (1, 1, 5, 5))),))

    def _region_case(
        self,
        *,
        second_carrier: bool = False,
    ) -> tuple[Ledger, dict[str, Any]]:
        items: list[dict[str, Any]] = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 800, 1000, 900],
                "text": "只有图形层可见",
            }
        ]
        region = _region((0.0, 800.0, 1000.0, 900.0))
        if second_carrier:
            items.append(
                {
                    "type": "text",
                    "page_idx": 0,
                    "bbox": [0, 850, 1000, 950],
                    "text": "同页相邻图形层",
                }
            )
            region = _region((0.0, 800.0, 1000.0, 950.0))
        return _build(
            items,
            (_page(("原生正文", (10, 10, 80, 20))),),
            visual_regions=(region,),
        )

    def _guarded_page_case(self) -> tuple[Ledger, dict[str, Any]]:
        items = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 500],
                "text": "扫描页正文",
            }
        ]
        return _build(
            items,
            (NativeTextPage(0, 100.0, 200.0, "", ()),),
            visual_pages=(_visual(),),
        )

    def _chart_case(self) -> tuple[Ledger, dict[str, Any]]:
        items = [
            {
                "type": "chart",
                "page_idx": 0,
                "bbox": [100, 100, 500, 500],
                "content": "营业收入 10",
                "img_path": "images/chart.png",
            }
        ]
        return _build(items, (_page(("原生正文", (10, 10, 50, 20))),))

    def test_native_exact_support_must_close_the_whole_carrier(self) -> None:
        ledger, kwargs = self._native_case()
        self.assertEqual(
            _support(ledger),
            {"kind": "native_exact", "source_atom_orders": [0]},
        )

        def drop_orders(broken: Ledger) -> None:
            _support(broken)["source_atom_orders"] = []

        def relabel(broken: Ledger) -> None:
            broken["carrier_support"][0]["support"] = {
                "kind": "visual_bound",
                "component_bbox": [0, 0, 1000, 100],
                "artifact": {
                    "artifact_role": "source_bbox_visual_000001_000001",
                    "sha256": "sha256:" + "1" * 64,
                    "size_bytes": 12,
                    "pixel_width": 10,
                    "pixel_height": 10,
                    "media_type": "image/png",
                },
            }

        def extra_field(broken: Ledger) -> None:
            _support(broken)["source_atom_orders"] = [0]
            _support(broken)["note"] = "extra"

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                ("empty orders", "carrier_native_support_invalid", drop_orders),
                ("visual relabel", "carrier_native_support_invalid", relabel),
                ("extra field", "carrier_native_support_invalid", extra_field),
            ],
        )

    def test_carrier_support_record_shape_page_and_identity_are_closed(
        self,
    ) -> None:
        ledger, kwargs = self._native_case()

        def open_fields(broken: Ledger) -> None:
            broken["carrier_support"][0]["note"] = "extra"

        def foreign_page(broken: Ledger) -> None:
            broken["carrier_support"][0]["page_idx"] = 7

        def duplicate(broken: Ledger) -> None:
            broken["carrier_support"].append(
                deepcopy(broken["carrier_support"][0])
            )

        def unknown_carrier(broken: Ledger) -> None:
            broken["carrier_support"][0]["selector"]["source_item_index"] = 9

        def drifted_bbox(broken: Ledger) -> None:
            broken["carrier_support"][0]["bbox"] = [0, 0, 900, 100]

        def dropped(broken: Ledger) -> None:
            broken["carrier_support"].clear()

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                ("open fields", "carrier_support_invalid", open_fields),
                ("foreign page", "carrier_support_invalid", foreign_page),
                ("duplicate selector", "carrier_support_invalid", duplicate),
                ("unknown carrier", "carrier_support_invalid", unknown_carrier),
                (
                    "drifted bbox",
                    "carrier_support_identity_mismatch",
                    drifted_bbox,
                ),
                (
                    "dropped record",
                    "carrier_support_closure_invalid",
                    dropped,
                ),
            ],
        )

    def test_visual_bound_support_must_contain_and_name_its_component(
        self,
    ) -> None:
        ledger, kwargs = self._region_case()
        self.assertEqual(_support(ledger)["kind"], "visual_bound")
        self.assertEqual(
            _support(ledger)["artifact"]["artifact_role"],
            "source_bbox_visual_000001_000001",
        )

        def relabel(broken: Ledger) -> None:
            broken["carrier_support"][0]["support"] = {
                "kind": "native_exact",
                "source_atom_orders": [0],
            }

        def shrink_component(broken: Ledger) -> None:
            _support(broken)["component_bbox"] = [10, 800, 1000, 900]

        def foreign_page_role(broken: Ledger) -> None:
            _support(broken)["artifact"]["artifact_role"] = (
                "source_bbox_visual_000002_000001"
            )

        def foreign_component_index(broken: Ledger) -> None:
            _support(broken)["artifact"]["artifact_role"] = (
                "source_bbox_visual_000001_000002"
            )

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                ("native relabel", "carrier_visual_support_invalid", relabel),
                (
                    "component excludes carrier",
                    "carrier_visual_support_invalid",
                    shrink_component,
                ),
                (
                    "role names another page",
                    "carrier_visual_support_invalid",
                    foreign_page_role,
                ),
                (
                    "role is not the merged component",
                    "carrier_visual_component_invalid",
                    foreign_component_index,
                ),
            ],
        )

    def test_shared_visual_component_identity_must_stay_consistent(self) -> None:
        ledger, kwargs = self._region_case(second_carrier=True)
        roles = {
            record["support"]["artifact"]["artifact_role"]
            for record in ledger["carrier_support"]
        }
        self.assertEqual(roles, {"source_bbox_visual_000001_000001"})

        def widen_one_component(broken: Ledger) -> None:
            _support(broken, 1)["component_bbox"] = [0, 840, 1000, 960]

        def redigest_one_artifact(broken: Ledger) -> None:
            _support(broken, 1)["artifact"]["sha256"] = "sha256:" + "9" * 64

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                (
                    "component bbox split",
                    "carrier_visual_support_invalid",
                    widen_one_component,
                ),
                (
                    "artifact bytes split",
                    "carrier_visual_support_invalid",
                    redigest_one_artifact,
                ),
            ],
        )

    def test_guarded_page_carrier_must_reuse_the_full_page_visual(self) -> None:
        ledger, kwargs = self._guarded_page_case()
        self.assertEqual(ledger["pages"][0]["modality"], "visual_page")
        self.assertEqual(
            _support(ledger)["artifact"]["artifact_role"],
            "source_page_visual_000001",
        )

        def partial_component(broken: Ledger) -> None:
            _support(broken)["component_bbox"] = [0, 0, 1000, 600]

        def foreign_artifact(broken: Ledger) -> None:
            _support(broken)["artifact"]["sha256"] = "sha256:" + "9" * 64

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                (
                    "partial component",
                    "carrier_visual_support_invalid",
                    partial_component,
                ),
                (
                    "foreign artifact",
                    "carrier_visual_support_invalid",
                    foreign_artifact,
                ),
            ],
        )

    def test_chart_recognition_support_is_pinned_to_its_occurrence_crop(
        self,
    ) -> None:
        ledger, kwargs = self._chart_case()
        self.assertEqual(
            _support(ledger)["artifact"]["artifact_role"],
            "source_visual_occurrence_000000",
        )

        def promote_to_native(broken: Ledger) -> None:
            broken["carrier_support"][0]["support"] = {
                "kind": "native_exact",
                "source_atom_orders": [0],
            }

        def widen_component(broken: Ledger) -> None:
            _support(broken)["component_bbox"] = [100, 100, 600, 500]

        def foreign_crop(broken: Ledger) -> None:
            _support(broken)["artifact"]["sha256"] = "sha256:" + "9" * 64

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                (
                    "native promotion",
                    "visual_recognition_support_invalid",
                    promote_to_native,
                ),
                (
                    "component is not the crop",
                    "visual_recognition_support_invalid",
                    widen_component,
                ),
                (
                    "foreign crop bytes",
                    "visual_recognition_support_invalid",
                    foreign_crop,
                ),
            ],
        )

    def test_generated_annotation_classification_and_artifact_are_closed(
        self,
    ) -> None:
        items = [
            {
                "type": "image",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 500],
                "text": "模型生成描述",
                "img_path": "images/0.png",
            }
        ]
        with TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "0.png"
            image_path.write_bytes(b"provider-image-bytes")
            ledger, kwargs = _build(
                items,
                (_page(("原生正文", (10, 10, 50, 20))),),
                generated_annotation_artifacts={
                    "evidence_image_000000": image_path,
                },
            )
            self.assertEqual(
                _support(ledger)["kind"],
                "generated_annotation",
            )

            def promote_to_source_text(broken: Ledger) -> None:
                broken["carrier_support"][0]["support"] = {
                    "kind": "visual_bound",
                    "component_bbox": [0, 0, 1000, 500],
                    "artifact": {
                        "artifact_role": "source_bbox_visual_000001_000001",
                        "sha256": "sha256:" + "1" * 64,
                        "size_bytes": 12,
                        "pixel_width": 10,
                        "pixel_height": 10,
                        "media_type": "image/png",
                    },
                }

            def foreign_source_item(broken: Ledger) -> None:
                _support(broken)["artifact"]["artifact_role"] = (
                    "evidence_image_000001"
                )

            def open_artifact_fields(broken: Ledger) -> None:
                _support(broken)["artifact"]["note"] = "extra"

            def invalid_digest(broken: Ledger) -> None:
                _support(broken)["artifact"]["sha256"] = "not-a-digest"

            self.assert_each_refusal(
                ledger,
                kwargs,
                [
                    (
                        "source-text promotion",
                        "generated_annotation_misclassified",
                        promote_to_source_text,
                    ),
                    (
                        "foreign source item",
                        "generated_artifact_invalid",
                        foreign_source_item,
                    ),
                    (
                        "open artifact fields",
                        "generated_artifact_invalid",
                        open_artifact_fields,
                    ),
                    (
                        "invalid digest",
                        "generated_artifact_invalid",
                        invalid_digest,
                    ),
                ],
            )

            crop = ledger["visual_occurrences"][0]["artifact"]
            manifest = {
                "files": {
                    "evidence_image_000000": {
                        "availability": "present",
                        "sha256": _support(ledger)["artifact"]["sha256"],
                        "size_bytes": _support(ledger)["artifact"][
                            "size_bytes"
                        ],
                    },
                    crop["artifact_role"]: {
                        "availability": "present",
                        "sha256": crop["sha256"],
                        "size_bytes": crop["size_bytes"],
                    },
                }
            }
            validate_source_evidence_ledger(
                ledger,
                parser_artifacts=manifest,
                **kwargs,
            )
            manifest["files"]["evidence_image_000000"]["sha256"] = (
                "sha256:" + "9" * 64
            )
            with self.assertRaises(SourceEvidenceContractError) as raised:
                validate_source_evidence_ledger(
                    ledger,
                    parser_artifacts=manifest,
                    **kwargs,
                )
            self.assertEqual(
                raised.exception.reason_code,
                "generated_manifest_identity_mismatch",
            )

    def test_generated_annotation_needs_a_readable_non_empty_artifact(
        self,
    ) -> None:
        items = [
            {
                "type": "image",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 500],
                "text": "模型生成描述",
                "img_path": "images/0.png",
            }
        ]
        pages = (_page(("原生正文", (10, 10, 50, 20))),)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty = root / "empty.png"
            empty.write_bytes(b"")
            missing = root / "missing.png"
            directory = root / "as_directory.png"
            directory.mkdir()
            present = root / "present.png"
            present.write_bytes(b"provider-image-bytes")

            for name, path in (
                ("empty file", empty),
                ("missing file", missing),
                ("directory", directory),
            ):
                with self.subTest(case=name):
                    with self.assertRaises(
                        SourceEvidenceContractError
                    ) as raised:
                        _build(
                            items,
                            pages,
                            generated_annotation_artifacts={
                                "evidence_image_000000": path,
                            },
                        )
                    self.assertEqual(
                        raised.exception.reason_code,
                        "generated_artifact_invalid",
                    )

            ledger, _ = _build(
                items,
                pages,
                generated_annotation_artifacts={
                    "evidence_image_000000": present,
                },
            )
            self.assertEqual(
                _support(ledger)["artifact"]["size_bytes"],
                len(b"provider-image-bytes"),
            )

    def test_non_native_carrier_needs_exactly_one_rendered_component(
        self,
    ) -> None:
        items = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 800, 1000, 900],
                "text": "只有图形层可见",
            }
        ]
        pages = (_page(("原生正文", (10, 10, 80, 20))),)
        exact = _region((0.0, 800.0, 1000.0, 900.0))
        ledger, _ = _build(items, pages, visual_regions=(exact,))
        self.assertEqual(_support(ledger)["kind"], "visual_bound")

        cases: list[tuple[str, tuple[VisualPageEvidence, ...]]] = [
            ("no rendered component", ()),
            (
                "component does not contain the carrier",
                (_region((0.0, 0.0, 100.0, 100.0)),),
            ),
            (
                "two components contain the carrier",
                (
                    exact,
                    _region((0.0, 700.0, 1000.0, 1000.0), component_idx=1),
                ),
            ),
        ]
        for name, regions in cases:
            with self.subTest(case=name):
                with self.assertRaises(SourceEvidenceContractError) as raised:
                    _build(items, pages, visual_regions=regions)
                self.assertEqual(
                    raised.exception.reason_code,
                    "mineru_carrier_unbound",
                )

        with self.assertRaises(SourceEvidenceContractError) as raised:
            _build(
                items,
                pages,
                visual_regions=(
                    exact,
                    _region((0.0, 0.0, 100.0, 100.0), component_idx=1),
                ),
            )
        self.assertEqual(
            raised.exception.reason_code,
            "visual_artifact_closure_invalid",
        )

    def test_table_retrieval_boundary_requires_an_exact_page_locator(
        self,
    ) -> None:
        located: dict[str, Any] = {
            "type": "table",
            "page_idx": 0,
            "bbox": [100, 200, 900, 700],
            "table_caption": [],
            "table_body": "",
            "table_footnote": [],
        }
        pages = (_page(("净利润", (1, 1, 5, 5))),)
        ledger, _ = _build([located], pages)
        self.assertEqual(ledger["carrier_support"], [])

        cases: list[tuple[str, dict[str, Any]]] = [
            (
                "table without a locator",
                {
                    key: value
                    for key, value in located.items()
                    if key != "bbox"
                },
            ),
            ("table beyond the pdf", {**located, "page_idx": 4}),
        ]
        for name, item in cases:
            with self.subTest(case=name):
                with self.assertRaises(SourceEvidenceContractError) as raised:
                    _build([item], pages)
                self.assertEqual(
                    raised.exception.reason_code,
                    "table_retrieval_boundary_unproved",
                )

    def test_image_and_chart_occurrences_require_an_exact_page_locator(
        self,
    ) -> None:
        located: dict[str, Any] = {
            "type": "image",
            "page_idx": 0,
            "bbox": [0, 0, 400, 400],
            "img_path": "images/0.png",
        }
        self.assertEqual(
            len(mineru_visual_occurrences([located], source_pdf_page_count=1)),
            1,
        )
        cases: list[tuple[str, dict[str, Any]]] = [
            ("no bbox", {**located, "bbox": None}),
            ("page beyond the pdf", {**located, "page_idx": 3}),
            ("no page", {**located, "page_idx": None}),
        ]
        for name, item in cases:
            with self.subTest(case=name):
                with self.assertRaises(SourceEvidenceContractError) as raised:
                    mineru_visual_occurrences([item], source_pdf_page_count=1)
                self.assertEqual(
                    raised.exception.reason_code,
                    "visual_occurrence_unbound",
                )
        with self.assertRaises(SourceEvidenceContractError) as raised:
            mineru_visual_occurrences([located], source_pdf_page_count=0)
        self.assertEqual(
            raised.exception.reason_code,
            "source_page_count_invalid",
        )


class MiddleRoleFailureTests(LedgerFailureCase):
    def test_middle_role_binds_only_proved_continuous_source_spans(self) -> None:
        baseline = resolve_middle_table_roles(
            [_table_item()],
            middle_artifact=_middle(_hint()),
            source_pages=(_role_page(("真实附注", (130, 725, 300, 750))),),
        )
        self.assertEqual([role.text for role in baseline], ["真实附注"])

        # A word grazing the tolerance-inflated edge belongs to the
        # neighbor region; center ownership keeps the role slice exact.
        grazed = resolve_middle_table_roles(
            [_table_item()],
            middle_artifact=_middle(_hint()),
            source_pages=(
                _role_page(
                    ("真实附注", (130, 725, 300, 750)),
                    ("正文擦边", (0, 700, 121, 750)),
                ),
            ),
        )
        self.assertEqual([role.text for role in grazed], ["真实附注"])

        # Column detection can interleave a foreign word between the
        # role's own words in reading order; ownership stays exactly
        # center-inside and the slice joins only the owned words.
        interleaved = resolve_middle_table_roles(
            [_table_item()],
            middle_artifact=_middle(_hint()),
            source_pages=(
                _role_page(
                    ("附注甲", (130, 725, 200, 750)),
                    ("表体", (130, 300, 200, 350)),
                    ("附注乙", (300, 725, 400, 750)),
                ),
            ),
        )
        self.assertEqual(
            [role.text for role in interleaved],
            ["附注甲 附注乙"],
        )

        geometry_page = replace(
            _role_page(("真实附注", (130, 725, 300, 750))),
            geometry_issues=(
                NativeTextGeometryIssue(
                    page_idx=0,
                    word_order=1,
                    text="未定位词",
                    raw_bbox=None,
                    reason="bbox_missing_or_non_finite",
                ),
            ),
        )
        cases: list[tuple[str, str, NativeTextPage]] = [
            (
                "geometry issue on the role page",
                "middle_role_source_geometry_unproved",
                geometry_page,
            ),
            (
                "grazing-only atom leaves the role empty",
                "middle_role_source_span_unproved",
                _role_page(("真实附注", (0, 700, 130, 750))),
            ),
            (
                "role selects no atom",
                "middle_role_source_span_unproved",
                _role_page(("真实附注", (130, 100, 300, 150))),
            ),

            (
                "role text projects to nothing",
                "middle_role_source_text_empty",
                _role_page((" ", (130, 725, 300, 750))),
            ),
        ]
        for name, reason_code, page in cases:
            with self.subTest(case=name):
                with self.assertRaises(SourceEvidenceContractError) as raised:
                    resolve_middle_table_roles(
                        [_table_item()],
                        middle_artifact=_middle(_hint()),
                        source_pages=(page,),
                    )
                self.assertEqual(raised.exception.reason_code, reason_code)

    def test_two_roles_cannot_claim_the_same_native_atoms(self) -> None:
        page = _role_page(("真实附注", (130, 725, 300, 750)))
        shared = _middle(
            _hint(field_index=0),
            _hint(field_index=1),
        )
        with self.assertRaises(SourceEvidenceContractError) as raised:
            resolve_middle_table_roles(
                [_table_item()],
                middle_artifact=shared,
                source_pages=(page,),
            )
        self.assertEqual(
            raised.exception.reason_code,
            "middle_role_source_span_ambiguous",
        )

        disjoint = _middle(
            _hint(field_index=0, role_bbox=(120, 720, 500, 760)),
            _hint(field_index=1, role_bbox=(501, 720, 880, 760)),
        )
        roles = resolve_middle_table_roles(
            [_table_item()],
            middle_artifact=disjoint,
            source_pages=(
                _role_page(
                    ("附注甲", (130, 725, 300, 750)),
                    ("附注乙", (520, 725, 700, 750)),
                ),
            ),
        )
        self.assertEqual([role.text for role in roles], ["附注甲", "附注乙"])

    def test_middle_role_parent_must_bind_one_content_list_table(self) -> None:
        page = _role_page(("真实附注", (130, 725, 300, 750)))
        unlocated = _table_item()
        unlocated.pop("bbox")
        cases: list[tuple[str, list[dict[str, Any]], MinerUMiddleArtifact]] = [
            ("table without a locator", [unlocated], _middle(_hint())),
            (
                "parent matches no table",
                [_table_item()],
                _middle(_hint(parent_bbox=(10, 20, 90, 70))),
            ),
            (
                "parent matches two tables",
                [_table_item(), _table_item()],
                _middle(_hint()),
            ),
        ]
        for name, content_list, middle in cases:
            with self.subTest(case=name):
                with self.assertRaises(SourceEvidenceContractError) as raised:
                    resolve_middle_table_roles(
                        content_list,
                        middle_artifact=middle,
                        source_pages=(page,),
                    )
                self.assertEqual(
                    raised.exception.reason_code,
                    "middle_role_parent_unbound",
                )

    def test_resolved_role_indices_must_be_contiguous_and_unique(self) -> None:
        role = ResolvedTableRole(
            source_item_index=0,
            page_idx=0,
            parent_bbox=(100, 200, 900, 700),
            field="table_footnote",
            index=0,
            bbox=(120, 720, 880, 760),
            provider_deleted=True,
            text="真实附注",
        )
        self.assertEqual(
            table_role_values_by_item((role,)),
            {(0, "table_footnote"): ("真实附注",)},
        )
        carriers = iter_mineru_text_carriers(
            [_table_item()],
            table_role_overrides=(role,),
        )
        self.assertEqual(
            [carrier.field for carrier in carriers],
            ["table_html", "table_footnote"],
        )

        with self.assertRaises(SourceEvidenceContractError) as raised:
            table_role_values_by_item((replace(role, index=1),))
        self.assertEqual(
            raised.exception.reason_code,
            "middle_role_index_invalid",
        )

        with self.assertRaises(SourceEvidenceContractError) as raised:
            iter_mineru_text_carriers(
                [_table_item()],
                table_role_overrides=(role, role),
            )
        self.assertEqual(
            raised.exception.reason_code,
            "middle_role_index_invalid",
        )

        with self.assertRaises(SourceEvidenceContractError) as raised:
            resolve_middle_table_roles(
                [_table_item()],
                middle_artifact=_middle(_hint(field_index=1)),
                source_pages=(_role_page(("真实附注", (130, 725, 300, 750))),),
            )
        self.assertEqual(
            raised.exception.reason_code,
            "middle_role_index_invalid",
        )

    def test_provider_text_and_middle_roles_must_agree(self) -> None:
        page = _role_page(("真实附注", (130, 725, 300, 750)))
        kept = resolve_middle_table_roles(
            [_table_item(["真实附注"])],
            middle_artifact=_middle(_hint(provider_deleted=False)),
            source_pages=(page,),
        )
        self.assertEqual([role.text for role in kept], ["真实附注"])

        cases: list[tuple[str, list[dict[str, Any]], MinerUMiddleArtifact]] = [
            (
                "kept role missing from content_list",
                [_table_item()],
                _middle(_hint(provider_deleted=False)),
            ),
            (
                "content_list text without any role",
                [_table_item(["无来源的附注"])],
                _middle(_hint()),
            ),
        ]
        for name, content_list, middle in cases:
            with self.subTest(case=name):
                with self.assertRaises(SourceEvidenceContractError) as raised:
                    resolve_middle_table_roles(
                        content_list,
                        middle_artifact=middle,
                        source_pages=(page,),
                    )
                self.assertEqual(
                    raised.exception.reason_code,
                    "middle_role_provider_conflict",
                )

    def test_ledger_middle_identity_and_roles_replay_from_source(self) -> None:
        items = [_table_item()]
        pages = (
            _role_page(
                ("净利润", (130, 300, 300, 350)),
                ("真实附注", (130, 725, 300, 750)),
            ),
        )
        middle = _middle(_hint())
        roles = resolve_middle_table_roles(
            items,
            middle_artifact=middle,
            source_pages=pages,
        )
        ledger, kwargs = _build(
            items,
            pages,
            middle_artifact=middle,
            table_role_overrides=roles,
        )
        self.assertEqual(
            [record["text"] for record in ledger["table_role_overrides"]],
            ["真实附注"],
        )

        def rewrite_role_text(broken: Ledger) -> None:
            broken["table_role_overrides"][0]["text"] = "篡改附注"

        def drop_role(broken: Ledger) -> None:
            broken["table_role_overrides"].clear()

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                (
                    "role text rewritten",
                    "middle_role_source_mismatch",
                    rewrite_role_text,
                ),
                ("role dropped", "middle_role_source_mismatch", drop_role),
            ],
        )

        with self.assertRaises(SourceEvidenceContractError) as raised:
            validate_source_evidence_ledger(
                ledger,
                **{**kwargs, "mineru_middle_artifact": None},
            )
        self.assertEqual(
            raised.exception.reason_code,
            "middle_artifact_identity_mismatch",
        )

        for name, artifact in (
            ("zero pages", replace(middle, page_count=0)),
            ("no backend", replace(middle, backend="")),
            ("no version", replace(middle, version="")),
        ):
            with self.subTest(case=name):
                with self.assertRaises(SourceEvidenceContractError) as raised:
                    _build(
                        items,
                        pages,
                        middle_artifact=artifact,
                        table_role_overrides=roles,
                    )
                self.assertEqual(
                    raised.exception.reason_code,
                    "middle_artifact_invalid",
                )


class SelectorFailureTests(LedgerFailureCase):
    def _selector(self) -> dict[str, Any]:
        return {
            "source_item_index": 0,
            "field": "text",
            "char_span": [0, 3],
            "value_sha256": "sha256:"
            + hashlib.sha256("净利润".encode()).hexdigest(),
            "projection": TEXT_PROJECTION,
        }

    def test_ir_selector_shape_projection_and_field_are_closed(self) -> None:
        elements = [{"source_item_index": 0, "page_idx": 0, "text": "净利润"}]
        self.assertEqual(
            resolve_ir_text_selector(elements, self._selector()),
            "净利润",
        )

        cases: list[tuple[str, str, dict[str, Any]]] = [
            (
                "unknown selector field",
                "selector_shape_invalid",
                {**self._selector(), "note": "extra"},
            ),
            (
                "missing projection",
                "selector_shape_invalid",
                {
                    key: value
                    for key, value in self._selector().items()
                    if key != "projection"
                },
            ),
            (
                "foreign projection",
                "selector_projection_unsupported",
                {**self._selector(), "projection": "raw.v0"},
            ),
            (
                "non-text field name",
                "selector_field_invalid",
                {**self._selector(), "field": 7},
            ),
            (
                "invalid char span",
                "char_span_invalid",
                {**self._selector(), "char_span": [3, 3]},
            ),
        ]
        for name, reason_code, selector in cases:
            with self.subTest(case=name):
                with self.assertRaises(SourceEvidenceContractError) as raised:
                    resolve_ir_text_selector(elements, selector)
                self.assertEqual(raised.exception.reason_code, reason_code)

    def test_ir_selector_must_match_the_mapped_field_shape(self) -> None:
        scalar = [{"source_item_index": 0, "text": "净利润"}]
        sequence_value = ["第一条", "第二条"]
        sequence = [{"source_item_index": 0, "list_items": sequence_value}]
        sequence_selector = {
            "source_item_index": 0,
            "field": "list_items",
            "index": 1,
            "char_span": [0, 3],
            "value_sha256": "sha256:"
            + hashlib.sha256("第二条".encode()).hexdigest(),
            "projection": TEXT_PROJECTION,
        }
        self.assertEqual(
            resolve_ir_text_selector(sequence, sequence_selector),
            "第二条",
        )

        cases: list[tuple[str, Sequence[Mapping[str, Any]], dict[str, Any]]] = [
            (
                "scalar field with an index",
                scalar,
                {**self._selector(), "index": 0},
            ),
            (
                "scalar field is not text",
                [{"source_item_index": 0, "text": ["净利润"]}],
                self._selector(),
            ),
            (
                "sequence field without an index",
                sequence,
                {
                    key: value
                    for key, value in sequence_selector.items()
                    if key != "index"
                },
            ),
            (
                "sequence index out of range",
                sequence,
                {**sequence_selector, "index": 5},
            ),
            (
                "sequence element is not text",
                [{"source_item_index": 0, "list_items": ["第一条", 2]}],
                sequence_selector,
            ),
            (
                "field outside the typed schema",
                scalar,
                {**self._selector(), "field": "img_path"},
            ),
        ]
        for name, elements, selector in cases:
            with self.subTest(case=name):
                with self.assertRaises(SourceEvidenceContractError) as raised:
                    resolve_ir_text_selector(elements, selector)
                self.assertEqual(
                    raised.exception.reason_code,
                    "selector_field_invalid",
                )

    def test_ir_selector_source_item_must_be_present_and_unique(self) -> None:
        element = {"source_item_index": 0, "page_idx": 0, "text": "净利润"}
        self.assertEqual(
            resolve_ir_text_selector([element], self._selector()),
            "净利润",
        )
        cases: list[tuple[str, Sequence[Mapping[str, Any]], dict[str, Any]]] = [
            (
                "non-index source item",
                [element],
                {**self._selector(), "source_item_index": "0"},
            ),
            ("absent source item", [], self._selector()),
            (
                "ambiguous source item",
                [element, dict(element)],
                self._selector(),
            ),
        ]
        for name, elements, selector in cases:
            with self.subTest(case=name):
                with self.assertRaises(SourceEvidenceContractError) as raised:
                    resolve_ir_text_selector(elements, selector)
                self.assertEqual(
                    raised.exception.reason_code,
                    "selector_source_invalid",
                )

    def test_carrier_support_selector_must_be_a_complete_field_selector(
        self,
    ) -> None:
        items = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 100],
                "text": "净利润",
            }
        ]
        ledger, kwargs = _build(items, (_page(("净利润", (1, 1, 5, 5))),))

        def foreign_projection(broken: Ledger) -> None:
            broken["carrier_support"][0]["selector"]["projection"] = "raw.v0"

        def unhashed_value(broken: Ledger) -> None:
            broken["carrier_support"][0]["selector"]["value_sha256"] = "nope"

        def partial_span(broken: Ledger) -> None:
            broken["carrier_support"][0]["selector"]["char_span"] = [1, 3]

        def non_text_field(broken: Ledger) -> None:
            broken["carrier_support"][0]["selector"]["field"] = 7

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                (
                    "foreign projection",
                    "selector_shape_invalid",
                    foreign_projection,
                ),
                ("unhashed value", "selector_shape_invalid", unhashed_value),
                ("partial field span", "selector_shape_invalid", partial_span),
                ("non-text field", "selector_shape_invalid", non_text_field),
            ],
        )

    def test_mapped_bindings_reject_page_and_text_drift(self) -> None:
        items = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 100],
                "text": "净利润总额",
            }
        ]
        ledger, _ = _build(
            items,
            (_page(("净利润", (1, 1, 5, 5)), ("总额", (7, 1, 11, 5))),),
        )
        self.assertEqual(
            [atom["disposition"]["kind"] for atom in ledger["atoms"]],
            ["mineru_carrier", "mineru_carrier"],
        )
        elements = [
            {"source_item_index": 0, "page_idx": 0, "text": "净利润总额"}
        ]
        validate_mapped_element_bindings(ledger, elements=elements)

        shifted = deepcopy(ledger)
        selector = shifted["atoms"][0]["disposition"]["carrier"]["selector"]
        selector["char_span"] = [2, 5]
        with self.assertRaises(SourceEvidenceContractError) as raised:
            validate_mapped_element_bindings(shifted, elements=elements)
        self.assertEqual(
            raised.exception.reason_code,
            "selector_text_mismatch",
        )

        with self.assertRaises(SourceEvidenceContractError) as raised:
            validate_mapped_element_bindings(
                ledger,
                elements=[
                    {
                        "source_item_index": 0,
                        "page_idx": 1,
                        "text": "净利润总额",
                    }
                ],
            )
        self.assertEqual(
            raised.exception.reason_code,
            "selector_page_mismatch",
        )

    def test_visual_only_records_also_bind_to_the_mapped_page(self) -> None:
        region_items = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 800, 1000, 900],
                "text": "只有图形层可见",
            }
        ]
        region_ledger, _ = _build(
            region_items,
            (_page(("原生正文", (10, 10, 80, 20))),),
            visual_regions=(_region((0.0, 800.0, 1000.0, 900.0)),),
        )
        self.assertEqual(
            [
                atom["disposition"]["kind"]
                for atom in region_ledger["atoms"]
            ],
            ["source_native_fallback"],
        )
        with self.assertRaises(SourceEvidenceContractError) as raised:
            validate_mapped_element_bindings(
                region_ledger,
                elements=[{"source_item_index": 0, "page_idx": 1}],
            )
        self.assertEqual(
            raised.exception.reason_code,
            "selector_page_mismatch",
        )

        image_items = [
            {
                "type": "image",
                "page_idx": 0,
                "bbox": [0, 0, 400, 400],
                "img_path": "images/0.png",
            }
        ]
        image_ledger, _ = _build(
            image_items,
            (_page(("原生正文", (10, 10, 80, 20))),),
        )
        self.assertEqual(image_ledger["carrier_support"], [])
        validate_mapped_element_bindings(
            image_ledger,
            elements=[{"source_item_index": 0, "page_idx": 0}],
        )
        with self.assertRaises(SourceEvidenceContractError) as raised:
            validate_mapped_element_bindings(
                image_ledger,
                elements=[{"source_item_index": 0, "page_idx": 1}],
            )
        self.assertEqual(
            raised.exception.reason_code,
            "selector_page_mismatch",
        )


class PageModalityFailureTests(LedgerFailureCase):
    def _native_case(self) -> tuple[Ledger, dict[str, Any]]:
        items = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 100],
                "text": "净利润",
            }
        ]
        return _build(items, (_page(("净利润", (1, 1, 5, 5))),))

    def _guarded_case(self) -> tuple[Ledger, dict[str, Any]]:
        items = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 1000],
                "text": "净利润",
            }
        ]
        page = replace(
            _page(("净利润", (1, 1, 5, 5))),
            geometry_issues=(
                NativeTextGeometryIssue(
                    page_idx=0,
                    word_order=1,
                    text="未定位词",
                    raw_bbox=None,
                    reason="bbox_missing_or_non_finite",
                ),
            ),
        )
        return _build(items, (page,), visual_pages=(_visual(),))

    def _visual_case(self) -> tuple[Ledger, dict[str, Any]]:
        items = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 500],
                "text": "扫描页正文",
            }
        ]
        return _build(
            items,
            (NativeTextPage(0, 100.0, 200.0, "", ()),),
            visual_pages=(_visual(),),
        )

    def test_native_text_page_cannot_carry_visual_or_absence_evidence(
        self,
    ) -> None:
        ledger, kwargs = self._native_case()
        visual_ledger, _ = self._visual_case()
        page_visual = visual_ledger["pages"][0]["visual_artifact"]
        self.assertEqual(ledger["pages"][0]["modality"], "native_text")

        def attach_visual(broken: Ledger) -> None:
            broken["pages"][0]["visual_artifact"] = deepcopy(page_visual)

        def claim_absence(broken: Ledger) -> None:
            broken["pages"][0]["fallback_reasons"] = {
                "source_native_text_absent": 1
            }
            broken["pages"][0]["fallback_required"] = True

        def unsupported_modality(broken: Ledger) -> None:
            broken["pages"][0]["modality"] = "ocr_text"

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                (
                    "visual artifact attached",
                    "source_page_modality_invalid",
                    attach_visual,
                ),
                (
                    "text absence claimed",
                    "source_page_modality_invalid",
                    claim_absence,
                ),
                (
                    "unsupported modality",
                    "source_page_modality_invalid",
                    unsupported_modality,
                ),
            ],
        )

    def test_guarded_and_visual_pages_must_match_their_native_evidence(
        self,
    ) -> None:
        guarded, guarded_kwargs = self._guarded_case()
        self.assertEqual(
            guarded["pages"][0]["modality"],
            "native_text_with_visual_guard",
        )

        def drop_geometry_issues(broken: Ledger) -> None:
            broken["pages"][0]["geometry_issues"] = []

        self.assert_each_refusal(
            guarded,
            guarded_kwargs,
            [
                (
                    "guard without geometry issues",
                    "source_page_modality_invalid",
                    drop_geometry_issues,
                ),
            ],
        )

        visual, visual_kwargs = self._visual_case()
        self.assertEqual(visual["pages"][0]["modality"], "visual_page")

        def invent_conflicts(broken: Ledger) -> None:
            broken["pages"][0]["source_order_conflicts"] = 1

        def clear_fallback_flag(broken: Ledger) -> None:
            broken["pages"][0]["fallback_required"] = False

        def swap_absence_reason(broken: Ledger) -> None:
            broken["pages"][0]["fallback_reasons"] = {
                "source_native_geometry_invalid": 1
            }

        self.assert_each_refusal(
            visual,
            visual_kwargs,
            [
                (
                    "invented order conflicts",
                    "source_page_modality_invalid",
                    invent_conflicts,
                ),
                (
                    "fallback flag cleared",
                    "source_page_modality_invalid",
                    clear_fallback_flag,
                ),
                (
                    "absence reason swapped",
                    "source_page_modality_invalid",
                    swap_absence_reason,
                ),
            ],
        )

    def test_page_fallback_reasons_must_reconcile_with_dispositions(
        self,
    ) -> None:
        items = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 800, 1000, 900],
                "text": "只有图形层可见",
            }
        ]
        ledger, kwargs = _build(
            items,
            (_page(("原生正文", (10, 10, 80, 20))),),
            visual_regions=(_region((0.0, 800.0, 1000.0, 900.0)),),
        )
        self.assertEqual(
            ledger["pages"][0]["fallback_reasons"],
            {"mineru_text_missing": 1},
        )

        def rewrite_reason(broken: Ledger) -> None:
            broken["pages"][0]["fallback_reasons"] = {
                "mineru_locator_unproved": 1
            }

        def clear_flag(broken: Ledger) -> None:
            broken["pages"][0]["fallback_reasons"] = {}
            broken["pages"][0]["fallback_required"] = True

        def unsupported_reason(broken: Ledger) -> None:
            broken["pages"][0]["fallback_reasons"] = {"provider_guess": 1}

        def zero_count(broken: Ledger) -> None:
            broken["pages"][0]["fallback_reasons"] = {"mineru_text_missing": 0}

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                (
                    "reason rewritten",
                    "source_page_fallback_invalid",
                    rewrite_reason,
                ),
                ("flag without reasons", "source_page_fallback_invalid", clear_flag),
                (
                    "unsupported reason",
                    "source_page_fallback_invalid",
                    unsupported_reason,
                ),
                ("non-positive count", "source_page_fallback_invalid", zero_count),
            ],
        )

    def test_page_range_and_atom_geometry_are_closed_at_reconcile_time(
        self,
    ) -> None:
        items: list[dict[str, Any]] = []
        good = _page(("净利润", (1, 1, 5, 5)))
        holed_orders = replace(
            good,
            atoms=(replace(good.atoms[0], order=2),),
        )
        drifted_text = replace(
            good,
            atoms=(replace(good.atoms[0], text="毛利润"),),
        )
        cases: list[tuple[str, str, Sequence[NativeTextPage], int]] = [
            ("page count below one", "source_page_count_invalid", (good,), 0),
            (
                "source pages do not close the range",
                "source_page_closure_invalid",
                (good,),
                2,
            ),
            (
                "page geometry is not positive",
                "source_page_geometry_invalid",
                (replace(good, width=0.0),),
                1,
            ),
            (
                "atom orders leave a hole",
                "source_atom_order_invalid",
                (holed_orders,),
                1,
            ),
            (
                "atom text differs from its page slice",
                "source_atom_invalid",
                (drifted_text,),
                1,
            ),
        ]
        for name, reason_code, pages, page_count in cases:
            with self.subTest(case=name):
                with self.assertRaises(SourceEvidenceContractError) as raised:
                    _build(items, pages, page_count=page_count)
                self.assertEqual(raised.exception.reason_code, reason_code)

        ledger, kwargs = _build([], (good,))
        with self.assertRaises(SourceEvidenceContractError) as raised:
            validate_source_evidence_ledger(
                ledger,
                **{**kwargs, "expected_source_pdf_page_count": 0},
            )
        self.assertEqual(
            raised.exception.reason_code,
            "source_page_count_invalid",
        )

    def test_geometry_issue_records_must_prove_their_own_reason(self) -> None:
        items = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 1000],
                "text": "净利润",
            }
        ]
        page = replace(
            _page(("净利润", (1, 1, 5, 5))),
            geometry_issues=(
                NativeTextGeometryIssue(
                    page_idx=0,
                    word_order=1,
                    text="未定位词",
                    raw_bbox=(6.0, 7.0, 7.0, 7.0),
                    reason="bbox_non_positive_extent",
                ),
            ),
        )
        ledger, kwargs = _build(items, (page,), visual_pages=(_visual(),))

        def open_issue_fields(broken: Ledger) -> None:
            broken["pages"][0]["geometry_issues"][0]["note"] = "extra"

        def missing_geometry_keeps_a_bbox(broken: Ledger) -> None:
            broken["pages"][0]["geometry_issues"][0]["reason"] = (
                "bbox_missing_or_non_finite"
            )

        def malformed_raw_bbox(broken: Ledger) -> None:
            broken["pages"][0]["geometry_issues"][0]["raw_bbox"] = [6.0, 7.0]

        def positive_extent_bbox(broken: Ledger) -> None:
            broken["pages"][0]["geometry_issues"][0]["raw_bbox"] = [
                6.0,
                7.0,
                8.0,
                9.0,
            ]

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                (
                    "open issue fields",
                    "source_geometry_issue_invalid",
                    open_issue_fields,
                ),
                (
                    "missing geometry keeps a raw bbox",
                    "source_geometry_issue_invalid",
                    missing_geometry_keeps_a_bbox,
                ),
                (
                    "malformed raw bbox",
                    "source_geometry_issue_invalid",
                    malformed_raw_bbox,
                ),
                (
                    "raw bbox has positive extent",
                    "source_geometry_issue_invalid",
                    positive_extent_bbox,
                ),
            ],
        )

        unproved = replace(
            page,
            geometry_issues=(
                replace(page.geometry_issues[0], raw_bbox=(6.0, 7.0, 8.0, 9.0)),
            ),
        )
        with self.assertRaises(SourceEvidenceContractError) as raised:
            _build(items, (unproved,), visual_pages=(_visual(),))
        self.assertEqual(
            raised.exception.reason_code,
            "source_geometry_issue_invalid",
        )

    def test_native_structure_artifact_must_bind_the_same_source_pdf(
        self,
    ) -> None:
        items = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 100],
                "text": "净利润",
            }
        ]
        pages = (_page(("净利润", (1, 1, 5, 5))),)
        ledger, kwargs = _build(items, pages)
        self.assert_accepts(ledger, kwargs)

        foreign = _native_structure()
        foreign["source_pdf_sha256"] = "sha256:" + "9" * 64
        with self.assertRaises(SourceEvidenceContractError) as raised:
            _build(items, pages, native_structure=foreign)
        self.assertEqual(
            raised.exception.reason_code,
            "native_structure_invalid",
        )
        with self.assertRaises(SourceEvidenceContractError) as raised:
            validate_source_evidence_ledger(
                ledger,
                **{**kwargs, "native_structure": foreign},
            )
        self.assertEqual(
            raised.exception.reason_code,
            "native_structure_invalid",
        )

    def test_ledger_pages_must_close_over_the_declared_page_count(self) -> None:
        items = [
            {
                "type": "text",
                "page_idx": page_idx,
                "bbox": [0, 0, 1000, 100],
                "text": text,
            }
            for page_idx, text in enumerate(("首页", "次页"))
        ]
        ledger, kwargs = _build(
            items,
            (
                _page(("首页", (1, 1, 5, 5))),
                _page(("次页", (1, 1, 5, 5)), page_idx=1),
            ),
            page_count=2,
        )

        def drop_page(broken: Ledger) -> None:
            broken["pages"].pop()

        def swap_pages(broken: Ledger) -> None:
            broken["pages"].reverse()

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                ("page dropped", "source_page_closure_invalid", drop_page),
                ("pages reordered", "source_page_invalid", swap_pages),
            ],
        )


class LedgerClosureFailureTests(LedgerFailureCase):
    def _case(self) -> tuple[Ledger, dict[str, Any]]:
        items = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 100],
                "text": "净利润",
            }
        ]
        return _build(items, (_page(("净利润", (1, 1, 5, 5))),))

    def test_ledger_root_fields_and_versions_are_closed(self) -> None:
        ledger, kwargs = self._case()

        def extra_root_field(broken: Ledger) -> None:
            broken["note"] = "extra"

        def missing_root_field(broken: Ledger) -> None:
            broken.pop("coverage")

        def contract_drift(broken: Ledger) -> None:
            broken["contract_version"] = "source-evidence-conservation.v99"

        def algorithm_drift(broken: Ledger) -> None:
            broken["algorithm_version"] = "guessing.v1"

        def projection_drift(broken: Ledger) -> None:
            broken["text_projection"] = "raw.v0"

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                (
                    "extra root field",
                    "source_evidence_fields_invalid",
                    extra_root_field,
                ),
                (
                    "missing root field",
                    "source_evidence_fields_invalid",
                    missing_root_field,
                ),
                (
                    "contract version drift",
                    "source_evidence_version_unsupported",
                    contract_drift,
                ),
                (
                    "algorithm version drift",
                    "source_evidence_version_unsupported",
                    algorithm_drift,
                ),
                (
                    "projection drift",
                    "source_evidence_version_unsupported",
                    projection_drift,
                ),
            ],
        )

    def test_ledger_identity_bindings_are_closed(self) -> None:
        ledger, kwargs = self._case()

        def pdf_identity(broken: Ledger) -> None:
            broken["source_pdf"]["page_count"] = 2

        def artifact_role(broken: Ledger) -> None:
            broken["mineru_artifact"]["role"] = "middle"

        def extractor_fields(broken: Ledger) -> None:
            broken["source_extractor"]["note"] = "extra"

        def blank_extractor(broken: Ledger) -> None:
            broken["mineru_artifact"]["extractor"]["version"] = "  "

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                ("pdf identity", "source_pdf_identity_mismatch", pdf_identity),
                (
                    "artifact role",
                    "mineru_artifact_identity_mismatch",
                    artifact_role,
                ),
                (
                    "extractor fields",
                    "extractor_identity_invalid",
                    extractor_fields,
                ),
                (
                    "blank extractor version",
                    "extractor_identity_invalid",
                    blank_extractor,
                ),
            ],
        )
        with self.assertRaises(SourceEvidenceContractError) as raised:
            validate_source_evidence_ledger(
                ledger,
                **{**kwargs, "expected_source_pdf_sha256": "sha256:zz"},
            )
        self.assertEqual(raised.exception.reason_code, "sha256_invalid")

    def test_atom_records_and_dispositions_are_closed(self) -> None:
        ledger, kwargs = self._case()

        def open_atom_fields(broken: Ledger) -> None:
            broken["atoms"][0]["note"] = "extra"

        def open_source_fields(broken: Ledger) -> None:
            broken["atoms"][0]["source"].pop("layout_path")

        def digest_drift(broken: Ledger) -> None:
            broken["atoms"][0]["source"]["text_sha256"] = "sha256:" + "9" * 64

        def span_drift(broken: Ledger) -> None:
            broken["atoms"][0]["source"]["char_span"] = [1, 3]

        def unsupported_disposition(broken: Ledger) -> None:
            broken["atoms"][0]["disposition"] = {"kind": "provider_guess"}

        def open_carrier_locator(broken: Ledger) -> None:
            broken["atoms"][0]["disposition"]["carrier"]["page_idx"] = 1

        def missing_order(broken: Ledger) -> None:
            broken["atoms"][0]["disposition"].pop("source_order")

        def dropped_atom(broken: Ledger) -> None:
            broken["atoms"].clear()

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                ("open atom fields", "source_atom_invalid", open_atom_fields),
                (
                    "open source fields",
                    "source_atom_invalid",
                    open_source_fields,
                ),
                ("text digest drift", "source_atom_invalid", digest_drift),
                ("span drift", "source_atom_invalid", span_drift),
                (
                    "unsupported disposition",
                    "source_disposition_invalid",
                    unsupported_disposition,
                ),
                (
                    "carrier locator drift",
                    "source_disposition_invalid",
                    open_carrier_locator,
                ),
                (
                    "missing source order",
                    "source_disposition_invalid",
                    missing_order,
                ),
                ("atom dropped", "source_atom_closure_invalid", dropped_atom),
            ],
        )

    def test_fallback_disposition_reasons_are_closed(self) -> None:
        items = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 800, 1000, 900],
                "text": "只有图形层可见",
            }
        ]
        ledger, kwargs = _build(
            items,
            (_page(("原生正文", (10, 10, 80, 20))),),
            visual_regions=(_region((0.0, 800.0, 1000.0, 900.0)),),
        )
        self.assertEqual(
            ledger["atoms"][0]["disposition"],
            {"kind": "source_native_fallback", "reason": "mineru_text_missing"},
        )

        def unsupported_reason(broken: Ledger) -> None:
            broken["atoms"][0]["disposition"]["reason"] = "provider_guess"

        def extra_field(broken: Ledger) -> None:
            broken["atoms"][0]["disposition"]["note"] = "extra"

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                (
                    "unsupported reason",
                    "source_disposition_invalid",
                    unsupported_reason,
                ),
                ("extra field", "source_disposition_invalid", extra_field),
            ],
        )

    def test_coverage_counters_must_reconcile(self) -> None:
        ledger, kwargs = self._case()

        def undercount_atoms(broken: Ledger) -> None:
            broken["coverage"]["source_atoms"] = 0

        def undercount_carriers(broken: Ledger) -> None:
            broken["coverage"]["native_exact_carriers"] = 0

        def extra_counter(broken: Ledger) -> None:
            broken["coverage"]["provider_guesses"] = 1

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                ("atom undercount", "source_coverage_invalid", undercount_atoms),
                (
                    "carrier undercount",
                    "source_coverage_invalid",
                    undercount_carriers,
                ),
                ("extra counter", "source_coverage_invalid", extra_counter),
            ],
        )

    def test_visual_artifact_and_renderer_descriptors_are_closed(self) -> None:
        items = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 500],
                "text": "扫描页正文",
            }
        ]
        ledger, kwargs = _build(
            items,
            (NativeTextPage(0, 100.0, 200.0, "", ()),),
            visual_pages=(_visual(),),
        )

        def open_artifact_fields(broken: Ledger) -> None:
            broken["pages"][0]["visual_artifact"]["note"] = "extra"

        def foreign_media_type(broken: Ledger) -> None:
            broken["pages"][0]["visual_artifact"]["media_type"] = "image/jpeg"

        def foreign_page_role(broken: Ledger) -> None:
            broken["pages"][0]["visual_artifact"]["artifact_role"] = (
                "source_bbox_visual_000001_000001"
            )

        def open_renderer_fields(broken: Ledger) -> None:
            broken["visual_renderer"].pop("png_options")

        def open_identity_fields(broken: Ledger) -> None:
            broken["visual_renderer"]["identity"].pop("engine")

        def blank_engine(broken: Ledger) -> None:
            broken["visual_renderer"]["identity"]["engine"] = ""

        def non_positive_dpi(broken: Ledger) -> None:
            broken["visual_renderer"]["render_options"]["dpi"] = 0

        def profile_digest_drift(broken: Ledger) -> None:
            broken["visual_renderer"]["profile_sha256"] = "sha256:" + "9" * 64

        def drop_renderer(broken: Ledger) -> None:
            broken["visual_renderer"] = None

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                (
                    "open artifact fields",
                    "visual_artifact_invalid",
                    open_artifact_fields,
                ),
                (
                    "foreign media type",
                    "visual_artifact_invalid",
                    foreign_media_type,
                ),
                (
                    "page artifact uses a region role",
                    "visual_artifact_closure_invalid",
                    foreign_page_role,
                ),
                (
                    "open renderer fields",
                    "visual_renderer_invalid",
                    open_renderer_fields,
                ),
                (
                    "open identity fields",
                    "visual_renderer_invalid",
                    open_identity_fields,
                ),
                ("blank engine", "visual_renderer_invalid", blank_engine),
                (
                    "non-positive dpi",
                    "visual_renderer_invalid",
                    non_positive_dpi,
                ),
                (
                    "profile digest drift",
                    "visual_renderer_invalid",
                    profile_digest_drift,
                ),
                (
                    "renderer dropped",
                    "visual_renderer_closure_invalid",
                    drop_renderer,
                ),
            ],
        )
        for name, manifest in (
            ("manifest without files", {}),
            ("manifest files are not an object", {"files": []}),
        ):
            with self.subTest(case=name):
                with self.assertRaises(SourceEvidenceContractError) as raised:
                    validate_source_evidence_ledger(
                        ledger,
                        parser_artifacts=manifest,
                        **kwargs,
                    )
                self.assertEqual(
                    raised.exception.reason_code,
                    "visual_manifest_invalid",
                )

    def test_all_rendered_evidence_shares_one_renderer_profile(self) -> None:
        items = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 800, 1000, 850],
                "text": "上方图形层",
            },
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 900, 1000, 950],
                "text": "下方图形层",
            },
        ]
        pages = (_page(("原生正文", (10, 10, 80, 20))),)
        regions = (
            _region((0.0, 800.0, 1000.0, 850.0), component_idx=0),
            _region((0.0, 900.0, 1000.0, 950.0), component_idx=1),
        )
        ledger, _ = _build(items, pages, visual_regions=regions)
        self.assertEqual(
            sorted(
                record["support"]["artifact"]["artifact_role"]
                for record in ledger["carrier_support"]
            ),
            [
                "source_bbox_visual_000001_000001",
                "source_bbox_visual_000001_000002",
            ],
        )

        mixed = (
            regions[0],
            replace(
                regions[1],
                render_options=replace(RENDER_OPTIONS, dpi=72),
            ),
        )
        with self.assertRaises(SourceEvidenceContractError) as raised:
            _build(items, pages, visual_regions=mixed)
        self.assertEqual(
            raised.exception.reason_code,
            "visual_renderer_closure_invalid",
        )

    def test_bbox_and_char_span_values_are_closed(self) -> None:
        ledger, kwargs = self._case()

        def short_bbox(broken: Ledger) -> None:
            broken["carrier_support"][0]["bbox"] = [0, 0, 1000]

        def non_numeric_bbox(broken: Ledger) -> None:
            broken["carrier_support"][0]["bbox"] = [0, 0, "1000", 100]

        def non_finite_bbox(broken: Ledger) -> None:
            broken["carrier_support"][0]["bbox"] = [0, 0, float("inf"), 100]

        def out_of_extent_bbox(broken: Ledger) -> None:
            broken["carrier_support"][0]["bbox"] = [0, 0, 1200, 100]

        def inverted_bbox(broken: Ledger) -> None:
            broken["carrier_support"][0]["bbox"] = [1000, 0, 0, 100]

        def short_char_span(broken: Ledger) -> None:
            broken["atoms"][0]["source"]["char_span"] = [0]

        def non_index_char_span(broken: Ledger) -> None:
            broken["atoms"][0]["source"]["char_span"] = [0, "3"]

        def out_of_range_char_span(broken: Ledger) -> None:
            broken["atoms"][0]["source"]["char_span"] = [0, 99]

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                ("short bbox", "bbox_invalid", short_bbox),
                ("non-numeric bbox", "bbox_invalid", non_numeric_bbox),
                ("non-finite bbox", "bbox_invalid", non_finite_bbox),
                ("bbox beyond extent", "bbox_invalid", out_of_extent_bbox),
                ("inverted bbox", "bbox_invalid", inverted_bbox),
                ("short char span", "char_span_invalid", short_char_span),
                (
                    "non-index char span",
                    "char_span_invalid",
                    non_index_char_span,
                ),
                (
                    "char span beyond page text",
                    "char_span_invalid",
                    out_of_range_char_span,
                ),
            ],
        )

    def test_native_atom_and_geometry_orders_are_closed(self) -> None:
        items = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 1000],
                "text": "净利润",
            }
        ]
        page = replace(
            _page(("净利润", (1, 1, 5, 5))),
            geometry_issues=(
                NativeTextGeometryIssue(
                    page_idx=0,
                    word_order=1,
                    text="未定位词",
                    raw_bbox=None,
                    reason="bbox_missing_or_non_finite",
                ),
            ),
        )
        ledger, kwargs = _build(items, (page,), visual_pages=(_visual(),))

        def collide_orders(broken: Ledger) -> None:
            broken["pages"][0]["geometry_issues"][0]["word_order"] = 0

        def skip_orders(broken: Ledger) -> None:
            broken["pages"][0]["geometry_issues"][0]["word_order"] = 5

        self.assert_each_refusal(
            ledger,
            kwargs,
            [
                (
                    "geometry issue reuses an atom order",
                    "source_atom_order_invalid",
                    collide_orders,
                ),
                (
                    "geometry issue order leaves a hole",
                    "source_atom_order_invalid",
                    skip_orders,
                ),
            ],
        )

    def test_content_list_bytes_and_canonical_projection_are_closed(
        self,
    ) -> None:
        items = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 100],
                "text": "净利润",
            }
        ]
        ledger, kwargs = _build(items, (_page(("净利润", (1, 1, 5, 5))),))
        self.assert_accepts(ledger, kwargs)

        cases: list[tuple[str, str, bytes | None, dict[str, Any]]] = [
            (
                "content bytes are not an object array",
                "mineru_artifact_invalid",
                b'{"type":"text"}',
                {},
            ),
            (
                "content bytes are not JSON",
                "mineru_artifact_invalid",
                b"not-json",
                {},
            ),
            (
                "canonical item count differs",
                "mineru_text_projection_invalid",
                None,
                {"canonical_content_list": []},
            ),
            (
                "canonical item identity differs",
                "mineru_text_projection_invalid",
                None,
                {"canonical_content_list": [{**items[0], "page_idx": 1}]},
            ),
        ]
        for name, reason_code, payload, override in cases:
            with self.subTest(case=name):
                merged = {**kwargs, **override}
                broken = deepcopy(ledger)
                if payload is not None:
                    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
                    merged["mineru_content_list_bytes"] = payload
                    merged["expected_mineru_artifact_sha256"] = digest
                    broken["mineru_artifact"]["sha256"] = digest
                with self.assertRaises(SourceEvidenceContractError) as raised:
                    validate_source_evidence_ledger(broken, **merged)
                self.assertEqual(raised.exception.reason_code, reason_code)


if __name__ == "__main__":
    unittest.main()
