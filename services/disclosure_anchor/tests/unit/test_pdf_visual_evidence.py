from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image
import pypdfium2 as pdfium

from disclosure_anchor.adapters.parsers import pdf_visual_evidence
from disclosure_anchor.adapters.parsers.pdf_visual_evidence import (
    PdfVisualEvidenceError,
    VisualOccurrenceRequest,
    VisualRegionRequest,
    render_pdf_visual_evidence,
)
from disclosure_anchor.adapters.parsers.pdfium_runtime import PDFIUM_LOCK


class PdfVisualEvidenceTests(unittest.TestCase):
    def test_overlapping_occurrences_remain_separate_exact_crops(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "source.pdf"
            _write_synthetic_pdf(pdf_path)

            rendered = render_pdf_visual_evidence(
                pdf_path,
                _sha256(pdf_path),
                full_pages=(),
                regions=(),
                occurrences=(
                    VisualOccurrenceRequest(7, 0, (0, 0, 600, 600)),
                    VisualOccurrenceRequest(9, 0, (300, 300, 900, 900)),
                ),
                artifact_dir=root / "occurrences",
            )
            occurrences = rendered.occurrences

            self.assertEqual(
                [item.artifact_role for item in occurrences],
                [
                    "source_visual_occurrence_000007",
                    "source_visual_occurrence_000009",
                ],
            )
            self.assertEqual(
                [item.bbox for item in occurrences],
                [
                    (0.0, 0.0, 600.0, 600.0),
                    (300.0, 300.0, 900.0, 900.0),
                ],
            )
            self.assertEqual(len(tuple((root / "occurrences").glob("*.png"))), 2)

    def test_occurrence_source_identity_must_be_unique(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "source.pdf"
            _write_synthetic_pdf(pdf_path)
            artifact_dir = root / "occurrences"

            with self.assertRaises(PdfVisualEvidenceError):
                render_pdf_visual_evidence(
                    pdf_path,
                    _sha256(pdf_path),
                    full_pages=(),
                    regions=(),
                    occurrences=(
                        VisualOccurrenceRequest(7, 0, (0, 0, 600, 600)),
                        VisualOccurrenceRequest(7, 0, (300, 300, 900, 900)),
                    ),
                    artifact_dir=artifact_dir,
                )

            self.assertFalse(artifact_dir.exists())

    def test_overlapping_regions_persist_as_one_hash_bound_component(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "source.pdf"
            _write_synthetic_pdf(pdf_path)

            rendered = render_pdf_visual_evidence(
                pdf_path,
                _sha256(pdf_path),
                full_pages=(),
                regions=(
                    VisualRegionRequest(0, (0, 0, 400, 400)),
                    VisualRegionRequest(0, (300, 300, 600, 600)),
                    VisualRegionRequest(0, (700, 700, 1000, 1000)),
                ),
                occurrences=(),
                artifact_dir=root / "regions",
            )
            components = rendered.regions

            self.assertEqual(len(components), 2)
            self.assertEqual(
                [item.bbox for item in components],
                [(0.0, 0.0, 600.0, 600.0), (700.0, 700.0, 1000.0, 1000.0)],
            )
            self.assertEqual(
                [item.artifact_role for item in components],
                [
                    "source_bbox_visual_000001_000001",
                    "source_bbox_visual_000001_000002",
                ],
            )
            for component in components:
                self.assertEqual(component.sha256, _sha256(component.artifact_path))

    def test_render_is_byte_repeatable_and_preserves_requested_order(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "source.pdf"
            _write_synthetic_pdf(pdf_path)
            source_hash = _sha256(pdf_path)

            first = render_pdf_visual_evidence(
                pdf_path,
                source_hash,
                full_pages=(1, 0),
                regions=(),
                occurrences=(),
                artifact_dir=root / "first",
            ).pages
            second = render_pdf_visual_evidence(
                pdf_path,
                source_hash,
                full_pages=(1, 0),
                regions=(),
                occurrences=(),
                artifact_dir=root / "second",
            ).pages

            self.assertEqual([item.page_idx for item in first], [1, 0])
            self.assertEqual(
                [item.artifact_role for item in first],
                ["source_page_visual_000002", "source_page_visual_000001"],
            )
            for left, right in zip(first, second, strict=True):
                self.assertEqual(left.sha256, right.sha256)
                self.assertEqual(left.size_bytes, right.size_bytes)
                self.assertEqual(
                    left.artifact_path.read_bytes(),
                    right.artifact_path.read_bytes(),
                )

    def test_full_region_and_occurrence_share_one_page_rasterization(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "source.pdf"
            _write_synthetic_pdf(pdf_path)
            original = pdf_visual_evidence.rasterize_pdf_visual_page

            with (
                patch.object(
                    pdf_visual_evidence,
                    "rasterize_pdf_visual_page",
                    wraps=original,
                ) as rasterize,
                patch.object(
                    pdf_visual_evidence,
                    "_require_pdf_hash",
                    wraps=pdf_visual_evidence._require_pdf_hash,
                ) as require_hash,
            ):
                rendered = render_pdf_visual_evidence(
                    pdf_path,
                    _sha256(pdf_path),
                    full_pages=(0,),
                    regions=(VisualRegionRequest(0, (0, 0, 500, 500)),),
                    occurrences=(
                        VisualOccurrenceRequest(7, 0, (500, 500, 1000, 1000)),
                    ),
                    artifact_dir=root / "artifacts",
                )

            self.assertEqual(rasterize.call_count, 1)
            self.assertEqual(require_hash.call_count, 2)
            self.assertEqual(
                (
                    len(rendered.pages),
                    len(rendered.regions),
                    len(rendered.occurrences),
                ),
                (1, 1, 1),
            )

    def test_empty_request_verifies_hash_without_creating_artifacts(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "source.pdf"
            _write_synthetic_pdf(pdf_path)
            artifact_dir = root / "not-created"

            result = render_pdf_visual_evidence(
                pdf_path,
                _sha256(pdf_path),
                full_pages=(),
                regions=(),
                occurrences=(),
                artifact_dir=artifact_dir,
            )

            self.assertEqual(
                (result.pages, result.regions, result.occurrences),
                ((), (), ()),
            )
            self.assertFalse(artifact_dir.exists())

    def test_hash_and_page_index_contracts_fail_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "source.pdf"
            _write_synthetic_pdf(pdf_path)
            source_hash = _sha256(pdf_path)
            invalid_calls = (
                ("not-a-hash", (0,)),
                ("sha256:" + "0" * 64, (0,)),
                (source_hash, (0, 0)),
                (source_hash, (-1,)),
                (source_hash, (2,)),
                (source_hash, (True,)),
            )
            for expected_hash, indices in invalid_calls:
                artifact_dir = root / f"artifacts-{len(list(root.iterdir()))}"
                with self.subTest(expected_hash=expected_hash, indices=indices):
                    with self.assertRaises(PdfVisualEvidenceError):
                        render_pdf_visual_evidence(
                            pdf_path,
                            expected_hash,
                            full_pages=indices,
                            regions=(),
                            occurrences=(),
                            artifact_dir=artifact_dir,
                        )
                    self.assertFalse(artifact_dir.exists())

    def test_descriptor_is_closed_and_pdfium_calls_share_the_process_lock(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "source.pdf"
            _write_synthetic_pdf(pdf_path)
            lock = _CountingLock(PDFIUM_LOCK)

            with patch.object(pdf_visual_evidence, "PDFIUM_LOCK", lock):
                (descriptor,) = render_pdf_visual_evidence(
                    pdf_path,
                    _sha256(pdf_path),
                    full_pages=(0,),
                    regions=(),
                    occurrences=(),
                    artifact_dir=root / "artifacts",
                ).pages

            self.assertEqual(lock.enters, lock.exits)
            self.assertGreaterEqual(lock.enters, 3)
            self.assertEqual(descriptor.page_idx, 0)
            self.assertEqual(descriptor.artifact_role, "source_page_visual_000001")
            self.assertEqual(descriptor.media_type, "image/png")
            self.assertEqual(descriptor.sha256, _sha256(descriptor.artifact_path))
            self.assertEqual(descriptor.size_bytes, descriptor.artifact_path.stat().st_size)
            self.assertEqual(
                (descriptor.pixel_width, descriptor.pixel_height),
                (300, 300),
            )
            self.assertEqual(descriptor.renderer.library, "pypdfium2")
            self.assertEqual(descriptor.renderer.engine, "PDFium")
            self.assertEqual(descriptor.render_options.dpi, 300)
            self.assertEqual(
                descriptor.render_options.crop,
                (0, 0, 0, 0),
            )
            self.assertEqual(
                descriptor.render_options.force_bitmap_format,
                int(pdfium.raw.FPDFBitmap_BGR),
            )
            self.assertTrue(descriptor.render_options.rev_byteorder)
            self.assertEqual(descriptor.png_options.format, "PNG")
            self.assertEqual(descriptor.png_options.color_mode, "RGB")
            self.assertEqual(
                descriptor.png_options.metadata_policy,
                "fixed_dpi_only",
            )
            with Image.open(descriptor.artifact_path) as image:
                self.assertEqual((image.format, image.mode, image.size), ("PNG", "RGB", (300, 300)))
            with self.assertRaises(FrozenInstanceError):
                descriptor.page_idx = 1  # type: ignore[misc]

    def test_failed_mixed_plan_rolls_back_only_its_own_files(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "source.pdf"
            _write_synthetic_pdf(pdf_path)
            artifact_dir = root / "artifacts"
            artifact_dir.mkdir()
            unrelated = artifact_dir / "keep.txt"
            unrelated.write_text("keep", encoding="utf-8")
            original_open = Path.open

            def fail_second_target(
                path: Path,
                mode: str = "r",
                *args: object,
                **kwargs: object,
            ) -> object:
                if (
                    path.name == "source_visual_occurrence_000007.png"
                    and mode == "xb"
                ):
                    raise OSError("injected write failure")
                return original_open(path, mode, *args, **kwargs)

            with patch.object(Path, "open", new=fail_second_target):
                with self.assertRaises(PdfVisualEvidenceError):
                    render_pdf_visual_evidence(
                        pdf_path,
                        _sha256(pdf_path),
                        full_pages=(0,),
                        regions=(
                            VisualRegionRequest(0, (0, 0, 500, 500)),
                        ),
                        occurrences=(
                            VisualOccurrenceRequest(
                                7,
                                0,
                                (500, 500, 1000, 1000),
                            ),
                        ),
                        artifact_dir=artifact_dir,
                    )

            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")
            self.assertEqual(tuple(artifact_dir.iterdir()), (unrelated,))


class _CountingLock:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate
        self.enters = 0
        self.exits = 0

    def __enter__(self) -> object:
        self.enters += 1
        return self.delegate.__enter__()  # type: ignore[attr-defined,no-any-return]

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> object:
        self.exits += 1
        return self.delegate.__exit__(  # type: ignore[attr-defined,no-any-return]
            exc_type,
            exc_value,
            traceback,
        )


def _write_synthetic_pdf(path: Path) -> None:
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument.new()
        try:
            for width, height in ((72, 72), (144, 72)):
                page = document.new_page(width, height)
                page.close()
            document.save(path)
        finally:
            document.close()


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
