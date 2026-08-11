from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from disclosure_anchor.adapters.parsers.mineru_medium import (
    MinerUMediumArtifactReader,
)
from disclosure_anchor.domain.errors import (
    ParserOutputContractError,
)


_SOURCE_HEX = "a" * 64
_SOURCE_SHA = f"sha256:{_SOURCE_HEX}"
_STEM = f"sha256_{_SOURCE_HEX}"


class MinerUMediumArtifactReaderTest(unittest.TestCase):
    def test_reads_primary_blocks_and_page_local_table_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root)

            document = MinerUMediumArtifactReader().read(
                root,
                source_pdf_sha256=_SOURCE_SHA,
            )

            self.assertEqual(
                (document.parser_version, document.backend, document.effort),
                ("3.4.4", "hybrid", "medium"),
            )
            self.assertFalse(document.ocr_enabled)
            self.assertEqual(len(document.pages), 2)
            self.assertEqual(len(document.blocks), 3)
            self.assertEqual(document.blocks[0].provider_level, 0)
            self.assertEqual(document.blocks[0].typed_annotation, "paragraph")
            self.assertEqual(document.blocks[0].payloads[0].text, "标题\uf052☑")
            self.assertEqual(document.blocks[2].provider_type, "table")
            self.assertEqual(document.blocks[2].payloads, ())
            self.assertEqual(
                [
                    segment.logical_stream_status
                    for segment in document.physical_table_segments
                ],
                ["retained", "deleted"],
            )
            self.assertEqual(
                [
                    segment.page_local_html
                    for segment in document.physical_table_segments
                ],
                ["<table><tr><td>前页</td></tr></table>", "<table><tr><td>续页</td></tr></table>"],
            )
            first_segment_bbox = document.physical_table_segments[0].bbox
            self.assertIsNotNone(first_segment_bbox)
            assert first_segment_bbox is not None
            self.assertEqual(
                first_segment_bbox.as_tuple(),
                (100.0, 100.0, 900.0, 875.0),
            )
            self.assertIsNotNone(
                document.physical_table_segments[1].crop_artifact_role
            )
            self.assertTrue(
                any(artifact.relative_path == "notes.bin" for artifact in document.artifacts)
            )
            self.assertTrue(
                all(":" not in artifact.role for artifact in document.artifacts)
            )
            media_types = {
                artifact.relative_path: artifact.media_type
                for artifact in document.artifacts
            }
            self.assertEqual(
                media_types[f"{_STEM}_content_list.json"],
                "application/json",
            )
            self.assertEqual(media_types[f"{_STEM}.md"], "text/markdown")
            self.assertEqual(
                media_types["images/owner.jpg"],
                "application/octet-stream",
            )

    def test_rejects_dangling_block_artifact_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root)
            document = MinerUMediumArtifactReader().read(
                root,
                source_pdf_sha256=_SOURCE_SHA,
            )
            bad_block = replace(
                document.pages[0].blocks[0],
                referenced_artifact_roles=("missing-role",),
            )
            bad_page = replace(
                document.pages[0],
                blocks=(bad_block, *document.pages[0].blocks[1:]),
            )

            with self.assertRaises(ValueError):
                replace(document, pages=(bad_page, *document.pages[1:]))

    def test_bundle_hash_covers_unreferenced_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root)
            reader = MinerUMediumArtifactReader()
            before = reader.read(root, source_pdf_sha256=_SOURCE_SHA)

            (root / "notes.bin").write_bytes(b"changed")
            after = reader.read(root, source_pdf_sha256=_SOURCE_SHA)

            self.assertNotEqual(before.bundle_sha256, after.bundle_sha256)

    def test_rejects_non_utf8_markdown_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root)
            (root / f"{_STEM}.md").write_bytes(b"\xff\xfe\x00\x80")

            with self.assertRaises(ParserOutputContractError):
                MinerUMediumArtifactReader().read(
                    root,
                    source_pdf_sha256=_SOURCE_SHA,
                )

    def test_rejects_wrong_lane(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root, middle_effort="high")
            with self.assertRaises(ParserOutputContractError):
                MinerUMediumArtifactReader().read(
                    root,
                    source_pdf_sha256=_SOURCE_SHA,
                )

    def test_model_page_shape_is_diagnostic_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root, model_page_count=1)

            document = MinerUMediumArtifactReader().read(
                root,
                source_pdf_sha256=_SOURCE_SHA,
            )

            self.assertEqual(len(document.pages), 2)

    def test_unbound_annotations_and_bad_primary_bbox_degrade_to_coarse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root)
            content_path = root / f"{_STEM}_content_list.json"
            content = json.loads(content_path.read_text(encoding="utf-8"))
            content[0]["bbox"] = [1, 1, 1, 1]
            _write_json(content_path, content)
            typed_path = root / f"{_STEM}_content_list_v2.json"
            typed = json.loads(typed_path.read_text(encoding="utf-8"))
            typed[0][1]["bbox"] = [101, 100, 900, 875]
            typed[1][0]["type"] = "paragraph"
            _write_json(typed_path, typed)

            document = MinerUMediumArtifactReader().read(
                root,
                source_pdf_sha256=_SOURCE_SHA,
            )

            self.assertIsNone(document.blocks[0].bbox)
            self.assertEqual(
                [block.typed_annotation for block in document.blocks],
                [None, None, None],
            )

    def test_annotation_binding_rejects_page_and_item_count_mismatch(self) -> None:
        for mismatch in ("page_count", "item_count"):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _write_bundle(root)
                typed_path = root / f"{_STEM}_content_list_v2.json"
                typed = json.loads(typed_path.read_text(encoding="utf-8"))
                if mismatch == "page_count":
                    typed.pop()
                else:
                    typed[0].pop()
                _write_json(typed_path, typed)

                document = MinerUMediumArtifactReader().read(
                    root,
                    source_pdf_sha256=_SOURCE_SHA,
                )

                expected = (
                    [None, None, None]
                    if mismatch == "page_count"
                    else [None, None, "table"]
                )
                self.assertEqual(
                    [block.typed_annotation for block in document.blocks],
                    expected,
                )

    def test_unmatched_physical_table_segment_stays_unbound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root)
            middle_path = root / f"{_STEM}_middle.json"
            middle = json.loads(middle_path.read_text(encoding="utf-8"))
            middle["pdf_info"][1]["para_blocks"] = []
            _write_json(middle_path, middle)

            document = MinerUMediumArtifactReader().read(
                root,
                source_pdf_sha256=_SOURCE_SHA,
            )

            self.assertEqual(
                document.physical_table_segments[1].logical_stream_status,
                "unbound",
            )

    def test_rejects_symlink_in_artifact_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root)
            (root / "unsafe-link").symlink_to(root / "notes.bin")

            with self.assertRaises(ParserOutputContractError):
                MinerUMediumArtifactReader().read(
                    root,
                    source_pdf_sha256=_SOURCE_SHA,
                )


def _write_bundle(
    root: Path,
    *,
    middle_effort: str = "medium",
    model_page_count: int = 2,
) -> None:
    images = root / "images"
    images.mkdir()
    (images / "owner.jpg").write_bytes(b"owner-crop")
    (images / "continuation.jpg").write_bytes(b"continuation-crop")
    (root / "notes.bin").write_bytes(b"unreferenced-diagnostic-sidecar")

    content = [
        {
            "type": "text",
            "page_idx": 0,
            "bbox": [100, 50, 900, 90],
            "text": "标题\uf052☑",
            "text_level": 0,
        },
        {
            "type": "table",
            "page_idx": 0,
            "bbox": [100, 100, 900, 875],
            "table_body": "<table><tr><td>前页与聚合正文</td></tr></table>",
            "img_path": "images/owner.jpg",
        },
        {
            "type": "table",
            "page_idx": 1,
            "bbox": [100, 100, 900, 400],
            "img_path": "",
        },
    ]
    typed = [
        [
            {"type": "paragraph", "bbox": [100, 50, 900, 90], "level": 1},
            {"type": "table", "bbox": [100, 100, 900, 875]},
        ],
        [{"type": "table", "bbox": [100, 100, 900, 400]}],
    ]
    first_table = _middle_table(
        index=1,
        bbox=[60, 80, 540, 700],
        html="<table><tr><td>前页</td></tr></table>",
        image_path="owner.jpg",
    )
    second_table = _middle_table(
        index=0,
        bbox=[60, 80, 540, 320],
        html="<table><tr><td>续页</td></tr></table>",
        image_path="continuation.jpg",
        cell_merge=[1],
    )
    second_para = json.loads(json.dumps(second_table))
    second_para["blocks"][0]["lines"] = []
    second_para["blocks"][0]["lines_deleted"] = True
    middle = {
        "_version_name": "3.4.4",
        "_backend": "hybrid",
        "_effort": middle_effort,
        "_ocr_enable": False,
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [600, 800],
                "preproc_blocks": [first_table],
                "para_blocks": [first_table],
                "discarded_blocks": [],
            },
            {
                "page_idx": 1,
                "page_size": [600, 800],
                "preproc_blocks": [second_table],
                "para_blocks": [second_para],
                "discarded_blocks": [],
            },
        ],
    }
    model = [[{"type": "table", "content": None}] for _ in range(model_page_count)]

    _write_json(root / f"{_STEM}_content_list.json", content)
    _write_json(root / f"{_STEM}_content_list_v2.json", typed)
    _write_json(root / f"{_STEM}_middle.json", middle)
    _write_json(root / f"{_STEM}_model.json", model)
    (root / f"{_STEM}.md").write_text("# 标题\uf052☑\n", encoding="utf-8")


def _middle_table(
    *,
    index: int,
    bbox: list[int],
    html: str,
    image_path: str,
    cell_merge: list[int] | None = None,
) -> dict[str, object]:
    span: dict[str, object] = {
        "type": "table",
        "html": html,
        "image_path": image_path,
        "bbox": bbox,
    }
    body: dict[str, object] = {
        "type": "table_body",
        "index": index,
        "bbox": bbox,
        "lines": [{"bbox": bbox, "spans": [span]}],
    }
    table: dict[str, object] = {
        "type": "table",
        "index": index,
        "bbox": bbox,
        "blocks": [body],
    }
    if cell_merge is not None:
        table["cell_merge"] = cell_merge
        body["cell_merge"] = cell_merge
    return table


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
