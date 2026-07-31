"""Strict page-local MinerU table-closure tests."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest

from disclosure_anchor.adapters.parsers.mineru.table_html_structure import (
    TableHtmlStructureError,
    parse_table_html_structure,
    table_media_artifact_role,
)
from disclosure_anchor.adapters.parsers.mineru.table_reconciler import (
    TableReconciliationStats,
    reconcile_content_list_tables,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


def _table(
    page: int,
    bbox: list[float],
    html: str,
    *,
    image_path: str = "images/table.jpg",
) -> dict[str, object]:
    return {
        "type": "table",
        "page_idx": page,
        "bbox": bbox,
        "img_path": image_path,
        "table_body": html,
        "table_caption": [],
        "table_footnote": [],
    }


def _pipeline_page(
    page: int,
    *detections: tuple[list[float], str],
) -> dict[str, object]:
    return {
        "page_info": {"page_no": page, "width": 1000, "height": 1000},
        "layout_dets": [
            {"label": "table", "bbox": bbox, "html": html}
            for bbox, html in detections
        ],
    }


def _vlm_model_json(html: str) -> str:
    return json.dumps(
        [[{
            "type": "table",
            "bbox": [0.1, 0.1, 0.9, 0.9],
            "content": html,
        }]],
        ensure_ascii=False,
    )


def _reconcile(
    content: list[dict[str, object]],
    model: object,
    *,
    registered: set[int] | None = None,
    artifact_bytes: dict[str, bytes] | None = None,
    drop_roles: set[str] | None = None,
):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        model_path = root / "model.json"
        model_path.write_text(
            json.dumps(model, ensure_ascii=False),
            encoding="utf-8",
        )
        paths: dict[str, Path] = {}
        table_structures = {}
        for index, item in enumerate(content):
            image_path = item.get("img_path")
            if (
                isinstance(image_path, str)
                and image_path
                and not image_path.startswith("/")
                and ".." not in Path(image_path).parts
            ):
                path = root / image_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((artifact_bytes or {}).get(image_path, b"outer"))
                if registered is None or index in registered:
                    paths[f"evidence_image_{index:06d}"] = path
            html = item.get("table_body")
            if not isinstance(html, str) or not html.strip():
                continue
            try:
                structure = parse_table_html_structure(html)
            except TableHtmlStructureError:
                continue
            table_structures[index] = structure
            for media in structure.embedded_media:
                path = root / media.image_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(
                    (artifact_bytes or {}).get(media.image_path, b"embedded")
                )
                paths[
                    table_media_artifact_role(index, media.occurrence_index)
                ] = path
        for role in drop_roles or ():
            paths.pop(role, None)
        return reconcile_content_list_tables(
            content,  # type: ignore[arg-type]
            model_path=model_path,
            registered_evidence_image_paths=paths,
            content_table_structures=table_structures,
        )


def _data_image(payload: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


class MinerUTableReconcilerTests(unittest.TestCase):
    def test_pipeline_page_local_bijection_preserves_content_unchanged(self) -> None:
        first = "<table><tr><th>项目</th></tr><tr><td>甲</td></tr></table>"
        second = "<table><tr><th>项目</th></tr><tr><td>乙</td></tr></table>"
        content = [
            _table(0, [100, 700, 900, 900], first),
            _table(1, [100, 100, 900, 300], second),
        ]
        original = copy.deepcopy(content)
        result = _reconcile(
            content,
            [
                _pipeline_page(0, ([100, 700, 900, 900], first)),
                _pipeline_page(1, ([100, 100, 900, 300], second)),
            ],
        )

        self.assertEqual(content, original)
        self.assertEqual(result.content_list, original)
        self.assertIsNot(result.content_list, content)
        self.assertEqual(
            result.stats.as_dict(),
            {
                "algorithm_version": "mineru-page-local-table-closure.v6",
                "model_hash": result.stats.model_hash,
                "content_tables": 2,
                "model_tables": 2,
                "matched_tables": 2,
                "page_local_closed": True,
            },
        )

    def test_vlm_normalized_geometry_is_supported(self) -> None:
        content_html = (
            "<table><tr><td>利润 $^{1}&x$ </td></tr></table>"
        )
        model_html = (
            "<table><tr><td>利润<eq>^{1}&amp;x</eq></td></tr></table>"
        )
        content = [_table(0, [100, 200, 900, 800], content_html)]
        original = copy.deepcopy(content)
        result = _reconcile(
            content,
            [
                [
                    {
                        "type": "table",
                        "bbox": [0.1, 0.2, 0.9, 0.8],
                        "content": model_html,
                    }
                ]
            ],
        )

        self.assertEqual(result.stats.matched_tables, 1)
        self.assertEqual(content, original)
        self.assertEqual(result.content_list, original)

    def test_logical_cell_text_role_and_spans_must_match_exactly(self) -> None:
        content_html = (
            '<table><tr><th rowspan="2" colspan="2">甲</th></tr></table>'
        )
        content = [_table(0, [100, 100, 900, 900], content_html)]
        mismatches = (
            '<table><tr><th rowspan="2" colspan="2">乙</th></tr></table>',
            '<table><tr><td rowspan="2" colspan="2">甲</td></tr></table>',
            '<table><tr><th rowspan="1" colspan="2">甲</th></tr></table>',
            '<table><tr><th rowspan="2" colspan="1">甲</th></tr></table>',
        )
        for model_html in mismatches:
            with self.subTest(model_html=model_html):
                with self.assertRaisesRegex(
                    ParserOutputContractError,
                    "0 exact page-local model matches",
                ):
                    _reconcile(
                        content,
                        [_pipeline_page(0, ([100, 100, 900, 900], model_html))],
                    )

        formula_model = (
            "<table><tr><td>利润<eq>^{1}</eq></td></tr></table>"
        )
        for content_formula in (
            "<table><tr><td>利润 $^{2}$ </td></tr></table>",
            r"<table><tr><td>利润 \(^{1}\) </td></tr></table>",
            "<table><tr><td>利润 $^{1}$ 已调整</td></tr></table>",
        ):
            with self.subTest(content_formula=content_formula):
                with self.assertRaisesRegex(
                    ParserOutputContractError,
                    "0 exact page-local model matches",
                ):
                    _reconcile(
                        [_table(0, [100, 100, 900, 900], content_formula)],
                        [
                            _pipeline_page(
                                0,
                                ([100, 100, 900, 900], formula_model),
                            )
                        ],
                    )

    def test_embedded_media_bytes_cell_and_occurrence_order_must_match(
        self,
    ) -> None:
        first_bytes = b"first-image"
        second_bytes = b"second-image"
        content_html = (
            '<table><tr><td>甲<img src="images/first.png"/>'
            '<img src="images/first.png"/></td>'
            '<td>乙<img src="images/second.png"/></td></tr></table>'
        )
        model_html = (
            f'<table><tr><td>甲<img src="{_data_image(first_bytes)}"/>'
            f'<img src="{_data_image(first_bytes)}"/></td>'
            f'<td>乙<img src="{_data_image(second_bytes)}"/></td></tr></table>'
        )
        content = [_table(0, [100, 100, 900, 900], content_html)]
        artifacts = {
            "images/first.png": first_bytes,
            "images/second.png": second_bytes,
        }

        result = _reconcile(
            content,
            [_pipeline_page(0, ([100, 100, 900, 900], model_html))],
            artifact_bytes=artifacts,
        )
        self.assertEqual(result.stats.matched_tables, 1)

        mismatches = (
            (
                f'<table><tr><td>甲<img src="{_data_image(first_bytes)}"/>'
                f'</td><td>乙<img src="{_data_image(second_bytes)}"/>'
                "</td></tr></table>"
            ),
            (
                f'<table><tr><td>甲<img src="{_data_image(second_bytes)}"/>'
                f'<img src="{_data_image(first_bytes)}"/></td>'
                f'<td>乙<img src="{_data_image(first_bytes)}"/>'
                "</td></tr></table>"
            ),
            (
                f'<table><tr><td>甲<img src="{_data_image(first_bytes)}"/>'
                f'<img src="{_data_image(first_bytes)}"/>'
                f'<img src="{_data_image(second_bytes)}"/></td>'
                "<td>乙</td></tr></table>"
            ),
        )
        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch):
                with self.assertRaisesRegex(
                    ParserOutputContractError,
                    "0 exact page-local model matches",
                ):
                    _reconcile(
                        content,
                        [_pipeline_page(0, ([100, 100, 900, 900], mismatch))],
                        artifact_bytes=artifacts,
                    )

        with self.assertRaisesRegex(
            ParserOutputContractError,
            "embedded image 1 is not registered",
        ):
            _reconcile(
                content,
                [_pipeline_page(0, ([100, 100, 900, 900], model_html))],
                artifact_bytes=artifacts,
                drop_roles={"evidence_table_media_000000_000001"},
            )

    def test_model_embedded_media_requires_exact_nonempty_data_uri_bytes(
        self,
    ) -> None:
        content_html = (
            '<table><tr><td><img src="images/first.png"/></td></tr></table>'
        )
        content = [_table(0, [100, 100, 900, 900], content_html)]
        for src, message in (
            ("images/first.png", "supported data URI"),
            ("data:image/png;base64,", "empty bytes"),
            ("data:image/png;base64,%%%", "supported data URI"),
        ):
            with self.subTest(src=src):
                model_html = (
                    f'<table><tr><td><img src="{src}"/></td></tr></table>'
                )
                with self.assertRaisesRegex(ParserOutputContractError, message):
                    _reconcile(
                        content,
                        [_pipeline_page(0, ([100, 100, 900, 900], model_html))],
                        artifact_bytes={"images/first.png": b"first-image"},
                    )

    def test_empty_or_unregistered_content_table_evidence_fails(self) -> None:
        valid_html = "<table><tr><td>甲</td></tr></table>"
        cases = (
            (_table(0, [100, 100, 900, 900], ""), {0}, "empty HTML"),
            (
                _table(0, [100, 100, 900, 900], "<table></table>"),
                {0},
                "not materialized",
            ),
            (
                _table(0, [100, 100, 900, 900], valid_html, image_path=""),
                {0},
                "image crop path",
            ),
            (
                _table(0, [100, 100, 900, 900], valid_html),
                set(),
                "not registered",
            ),
            (
                _table(
                    0,
                    [100, 100, 900, 900],
                    valid_html,
                    image_path="../table.jpg",
                ),
                {0},
                "image crop path",
            ),
        )
        for item, registered, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ParserOutputContractError, message):
                    _reconcile(
                        [item],
                        [_pipeline_page(0, ([100, 100, 900, 900], valid_html))],
                        registered=registered,
                    )

    def test_unmatched_ambiguous_or_extra_tables_fail_closed(self) -> None:
        html = "<table><tr><td>甲</td></tr></table>"
        content = [_table(0, [100, 100, 900, 900], html)]
        cases = (
            (
                [_pipeline_page(1, ([100, 100, 900, 900], html))],
                "0 exact page-local model matches",
            ),
            (
                [
                    _pipeline_page(
                        0,
                        ([100, 100, 900, 900], html),
                        ([101, 100, 900, 900], html),
                    )
                ],
                "2 exact page-local model matches",
            ),
            (
                [
                    _pipeline_page(
                        0,
                        ([100, 100, 900, 900], html),
                        ([100, 500, 900, 950], html),
                    )
                ],
                "not represented by content_list",
            ),
        )
        for model, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ParserOutputContractError, message):
                    _reconcile(content, model)

    def test_model_artifact_and_schema_fail_closed(self) -> None:
        content: list[dict[str, object]] = []
        with self.assertRaisesRegex(ParserOutputContractError, "model artifact"):
            reconcile_content_list_tables(
                [], model_path=None, registered_evidence_image_paths={}
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = (
                ("missing.json", None, "cannot read"),
                ("invalid.json", "not json", "invalid MinerU model JSON"),
                ("shape.json", '{"pages":[]}', "unsupported MinerU model"),
                (
                    "malformed-equation.json",
                    _vlm_model_json(
                        "<table><tr><td><eq class=\"x\">甲</eq>"
                        "</td></tr></table>"
                    ),
                    "malformed inline-equation markup",
                ),
                (
                    "nested-equation.json",
                    _vlm_model_json(
                        "<table><tr><td><eq>甲<eq>乙</eq></eq>"
                        "</td></tr></table>"
                    ),
                    "nested inline-equation markup",
                ),
                (
                    "unclosed-equation.json",
                    _vlm_model_json(
                        "<table><tr><td><eq>甲</td></tr></table>"
                    ),
                    "unclosed inline-equation markup",
                ),
            )
            for name, payload, message in cases:
                path = root / name
                if payload is not None:
                    path.write_text(payload, encoding="utf-8")
                with self.subTest(name=name):
                    with self.assertRaisesRegex(
                        ParserOutputContractError,
                        message,
                    ):
                        reconcile_content_list_tables(
                            content,  # type: ignore[arg-type]
                            model_path=path,
                            registered_evidence_image_paths={},
                        )

    def test_empty_content_and_empty_supported_model_form_closed_zero(self) -> None:
        result = _reconcile([], [])

        self.assertTrue(result.stats.page_local_closed)
        self.assertEqual(result.stats.content_tables, 0)
        self.assertEqual(result.stats.model_tables, 0)

    def test_stats_and_registered_path_contracts_reject_impossible_states(
        self,
    ) -> None:
        valid = {
            "model_hash": "sha256:" + "a" * 64,
            "content_tables": 1,
            "model_tables": 1,
            "matched_tables": 1,
        }
        for override in (
            {"model_tables": 2},
            {"matched_tables": 0},
            {"page_local_closed": False},
            {"model_hash": "sha256:bad"},
        ):
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    TableReconciliationStats(**{**valid, **override})

        with self.assertRaisesRegex(ValueError, "paths are invalid"):
            reconcile_content_list_tables(
                [],
                model_path=Path("/unused"),
                registered_evidence_image_paths={"bad": Path("/unused")},
            )


if __name__ == "__main__":
    unittest.main()
