from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any, TextIO
import unittest
from unittest import mock

from disclosure_anchor.adapters.parsers.mineru_medium import (
    MinerUMediumArtifactReader,
)
from disclosure_anchor.adapters.parsers.mineru_medium import (
    artifacts as artifacts_module,
)
from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import (
    PinnedArtifactTree,
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
                [
                    "<table><tr><td>前页</td></tr></table>",
                    "<table><tr><td>续页</td></tr></table>",
                ],
            )
            first_segment_bbox = document.physical_table_segments[0].bbox
            self.assertIsNotNone(first_segment_bbox)
            assert first_segment_bbox is not None
            self.assertEqual(
                first_segment_bbox.as_tuple(),
                (100.0, 100.0, 900.0, 875.0),
            )
            self.assertIsNotNone(document.physical_table_segments[1].crop_artifact_role)
            self.assertTrue(
                any(
                    artifact.relative_path == "notes.bin"
                    for artifact in document.artifacts
                )
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
            with (
                self.subTest(mismatch=mismatch),
                tempfile.TemporaryDirectory() as directory,
            ):
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

    def test_rejects_payload_field_outside_provider_type_contract(self) -> None:
        cases = (
            ("table_html", "重复正文", "invalid for type table"),
            ("text", "重复正文", "invalid for type table"),
            ("table_caption", "非数组标题", "must be a text array"),
        )
        for field, value, error in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _write_bundle(root)
                content_path = root / f"{_STEM}_content_list.json"
                content = json.loads(content_path.read_text(encoding="utf-8"))
                content[1][field] = value
                _write_json(content_path, content)

                with self.assertRaisesRegex(
                    ParserOutputContractError,
                    error,
                ):
                    MinerUMediumArtifactReader().read(
                        root,
                        source_pdf_sha256=_SOURCE_SHA,
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

    def test_rejects_symlink_root_fifo_hardlink_and_empty_directory(self) -> None:
        with (
            self.subTest(case="symlink root"),
            tempfile.TemporaryDirectory() as directory,
        ):
            parent = Path(directory)
            actual = parent / "actual"
            actual.mkdir()
            _write_bundle(actual)
            linked = parent / "linked"
            linked.symlink_to(actual, target_is_directory=True)

            with self.assertRaises(ParserOutputContractError):
                MinerUMediumArtifactReader().read(
                    linked,
                    source_pdf_sha256=_SOURCE_SHA,
                )

        with self.subTest(case="fifo"), tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root)
            os.mkfifo(root / "unsafe-fifo")

            with self.assertRaises(ParserOutputContractError):
                MinerUMediumArtifactReader().read(
                    root,
                    source_pdf_sha256=_SOURCE_SHA,
                )

        with self.subTest(case="hardlink"), tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root)
            os.link(root / "notes.bin", root / "notes-hardlink.bin")

            with self.assertRaises(ParserOutputContractError):
                MinerUMediumArtifactReader().read(
                    root,
                    source_pdf_sha256=_SOURCE_SHA,
                )

        with (
            self.subTest(case="empty directory"),
            tempfile.TemporaryDirectory() as directory,
        ):
            root = Path(directory)
            _write_bundle(root)
            (root / "unexpected-empty").mkdir()

            with self.assertRaises(ParserOutputContractError):
                MinerUMediumArtifactReader().read(
                    root,
                    source_pdf_sha256=_SOURCE_SHA,
                )

    def test_pinned_tree_rejects_root_file_directory_and_topology_rebinding(
        self,
    ) -> None:
        with self.subTest(case="root"), tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "output"
            root.mkdir()
            _write_bundle(root)
            with PinnedArtifactTree.open_path(root) as tree:
                root.rename(parent / "original-output")
                root.mkdir()
                with self.assertRaises(ParserOutputContractError):
                    tree.verify_unchanged()

        with self.subTest(case="file"), tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "output"
            root.mkdir()
            _write_bundle(root)
            replacement = parent / "replacement"
            replacement.write_bytes((root / "notes.bin").read_bytes())
            with PinnedArtifactTree.open_path(root) as tree:
                os.replace(replacement, root / "notes.bin")
                with self.assertRaises(ParserOutputContractError):
                    tree.verify_unchanged()

        with self.subTest(case="directory"), tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "output"
            root.mkdir()
            _write_bundle(root)
            owner = (root / "images" / "owner.jpg").read_bytes()
            continuation = (root / "images" / "continuation.jpg").read_bytes()
            with PinnedArtifactTree.open_path(root) as tree:
                (root / "images").rename(parent / "original-images")
                (root / "images").mkdir()
                (root / "images" / "owner.jpg").write_bytes(owner)
                (root / "images" / "continuation.jpg").write_bytes(continuation)
                with self.assertRaises(ParserOutputContractError):
                    tree.verify_unchanged()

        with self.subTest(case="topology"), tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root)
            with PinnedArtifactTree.open_path(root) as tree:
                (root / "late-sidecar").write_bytes(b"late")
                with self.assertRaises(ParserOutputContractError):
                    tree.verify_unchanged()

    def test_exact_admitted_deletion_has_linear_effect_and_scan_counts(self) -> None:
        for file_count in (800, 1_600):
            with self.subTest(file_count=file_count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for index in range(file_count):
                    (root / f"item-{index:04d}.bin").write_bytes(b"")
                with PinnedArtifactTree.open_path(
                    root,
                    max_files=file_count,
                    max_bytes=1,
                ) as tree:
                    scan_calls = 0
                    effect_calls = 0
                    original_scan = tree._scan_metadata

                    def count_scan(**kwargs: Any) -> None:
                        nonlocal scan_calls
                        scan_calls += 1
                        original_scan(**kwargs)

                    def count_effect() -> None:
                        nonlocal effect_calls
                        effect_calls += 1

                    with mock.patch.object(
                        tree,
                        "_scan_metadata",
                        side_effect=count_scan,
                    ):
                        tree.remove_exact_admitted_contents(
                            before_effect=count_effect,
                            last_files=(),
                        )
                self.assertEqual(scan_calls, 3)
                self.assertEqual(effect_calls, file_count)
                self.assertEqual(tuple(root.iterdir()), ())

    def test_json_hash_and_parse_share_an_identity_and_reject_aba(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root)
            content_path = root / f"{_STEM}_content_list.json"
            original = content_path.read_bytes()
            original_load = artifacts_module.json.load
            mutated = False

            def load_after_aba(
                stream: TextIO,
                *,
                parse_constant: Callable[[str], object],
            ) -> object:
                nonlocal mutated
                if not mutated:
                    mutated = True
                    content_path.write_bytes(b"[]")
                    content_path.write_bytes(original)
                return original_load(stream, parse_constant=parse_constant)

            with (
                PinnedArtifactTree.open_path(root) as tree,
                mock.patch.object(
                    artifacts_module.json,
                    "load",
                    side_effect=load_after_aba,
                ),
            ):
                with self.assertRaises(ParserOutputContractError):
                    tree.load_json(
                        PurePosixPath(f"{_STEM}_content_list.json"),
                        label="content_list",
                    )

    def test_failed_admission_does_not_leak_file_descriptors(self) -> None:
        fd_root = Path("/dev/fd")
        if not fd_root.is_dir():
            fd_root = Path("/proc/self/fd")
        if not fd_root.is_dir():
            self.skipTest("process descriptor directory is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root)
            os.mkfifo(root / "zz-unsafe-fifo")
            before = len(tuple(fd_root.iterdir()))

            for _ in range(20):
                with self.assertRaises(ParserOutputContractError):
                    PinnedArtifactTree.open_path(root)

            self.assertEqual(len(tuple(fd_root.iterdir())), before)

    def test_every_open_uses_nonfollowing_cloexec_nonblocking_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root)
            observed_flags: list[tuple[int, int | None]] = []
            original_open = artifacts_module.os.open

            def checked_open(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                observed_flags.append((flags, dir_fd))
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch.object(
                artifacts_module.os,
                "open",
                side_effect=checked_open,
            ):
                MinerUMediumArtifactReader().read(
                    root,
                    source_pdf_sha256=_SOURCE_SHA,
                )

            required = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            self.assertTrue(observed_flags)
            self.assertTrue(
                all(flags & required == required for flags, _ in observed_flags)
            )
            self.assertTrue(
                any(
                    flags & os.O_DIRECTORY and dir_fd is not None
                    for flags, dir_fd in observed_flags
                )
            )

    def test_streams_large_sidecars_in_bounded_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root)
            (root / "notes.bin").write_bytes(b"x" * (3 * 1024 * 1024 + 17))
            observed_read_sizes: list[int] = []
            original_read = artifacts_module.os.read

            def bounded_read(fd: int, size: int) -> bytes:
                observed_read_sizes.append(size)
                return original_read(fd, size)

            with mock.patch.object(
                artifacts_module.os,
                "read",
                side_effect=bounded_read,
            ):
                document = MinerUMediumArtifactReader().read(
                    root,
                    source_pdf_sha256=_SOURCE_SHA,
                )

            self.assertTrue(observed_read_sizes)
            self.assertLessEqual(max(observed_read_sizes), 1024 * 1024)
            self.assertEqual(
                next(
                    artifact.size_bytes
                    for artifact in document.artifacts
                    if artifact.relative_path == "notes.bin"
                ),
                3 * 1024 * 1024 + 17,
            )

    def test_single_read_returns_nested_artifact_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "nested" / "mineru"
            nested.mkdir(parents=True)
            _write_bundle(nested)

            result = MinerUMediumArtifactReader().read_with_location(
                root,
                source_pdf_sha256=_SOURCE_SHA,
            )

            self.assertEqual(
                result.artifact_root_relpath,
                PurePosixPath("nested/mineru"),
            )
            self.assertEqual(len(result.document.pages), 2)


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
