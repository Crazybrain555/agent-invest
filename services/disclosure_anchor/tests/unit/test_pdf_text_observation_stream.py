"""Independent native-stream behavior and caller/PDFium handle ownership tests."""
from __future__ import annotations

from dataclasses import replace
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from disclosure_anchor.adapters.parsers import pdf_text_observation as module
from disclosure_anchor.adapters.parsers.pdf_text_observation import (
    observe_pdf_text_rectangles,
    observe_pdf_text_rectangles_from_open_file,
)
from disclosure_anchor.application.contracts.provider_document import ProviderPage
from disclosure_anchor.application.contracts.provider_document_admission import SourcePdfTextObservation
from tests._pdf_text_stream_fixture import (
    LOWER_BBOX, provider_block, provider_document, sha, text_pdf,
)


class _CallerStream(io.BytesIO):
    def __init__(self, raw: bytes) -> None:
        super().__init__(raw)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        super().close()


class _FaultGraph:
    """Fault-only graph; never evidence of native extraction or IO authority."""
    def __init__(self, *, fail_at: str | None = None,
                 close_failures: tuple[str, ...] = ()) -> None:
        self.fail_at = fail_at
        self.close_failures = close_failures
        self.events: list[str] = []
        self.pdf = _FaultPdf(self)
        self.page = _FaultPage(self)
        self.text = _FaultText(self)

    def step(self, name: str) -> None:
        self.events.append(name)
        if self.fail_at == name or name in self.close_failures:
            raise RuntimeError("injected " + name)


class _FaultOwned:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False


class _FaultPdf(_FaultOwned):
    def __init__(self, graph: _FaultGraph) -> None:
        self.graph = graph

    def __len__(self) -> int:
        self.graph.step("document.length")
        return 1

    def __getitem__(self, index: int) -> _FaultPage:
        if index != 0:
            raise AssertionError(index)
        self.graph.step("document.page")
        return self.graph.page

    def close(self) -> None:
        self.graph.step("document.close")


class _FaultPage(_FaultOwned):
    def __init__(self, graph: _FaultGraph) -> None:
        self.graph = graph

    def get_rotation(self) -> int:
        self.graph.step("page.rotation")
        return 0

    def get_bbox(self) -> tuple[float, float, float, float]:
        self.graph.step("page.bbox")
        return (0.0, 0.0, 600.0, 800.0)

    def get_textpage(self) -> _FaultText:
        self.graph.step("page.textpage")
        return self.graph.text

    def close(self) -> None:
        self.graph.step("page.close")


class _FaultText(_FaultOwned):
    def __init__(self, graph: _FaultGraph) -> None:
        self.graph = graph

    def get_text_bounded(self, **bounds: float) -> str:
        self.graph.step("text.read")
        expected = {"left": 42.0, "bottom": 672.0, "right": 420.0, "top": 728.0}
        if set(bounds) != set(expected) or any(abs(bounds[key] - value) > 1e-9 for key, value in expected.items()):
            raise AssertionError(bounds)
        return "FAULT GRAPH TEXT"

    def close(self) -> None:
        self.graph.step("text.close")


def _error_messages(error: BaseException) -> set[str]:
    pending = [error]
    seen: set[int] = set()
    messages: set[str] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        messages.add(str(current))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None and not current.__suppress_context__:
            pending.append(current.__context__)
    return messages


class PdfTextHeldStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = text_pdf()
        self.document = provider_document(self.raw)
        self.source = _CallerStream(self.raw)
        self.addCleanup(self.source.close)
        self.expected = (SourcePdfTextObservation(
            source_index=0, page_index=0, payload_ordinal=0,
            raw_block_sha256=self.document.blocks[0].raw_item_sha256,
            text="ORIGINAL TOKEN",
        ),)

    def assert_caller_owned(self) -> None:
        self.assertFalse(self.source.closed)
        self.assertEqual(self.source.close_calls, 0)
        self.source.seek(0)
        self.assertEqual(self.source.read(), self.raw)

    def test_real_native_text_uses_literal_rectangle_and_rewinds_for_repeat(self) -> None:
        self.source.seek(len(self.raw))
        result = observe_pdf_text_rectangles_from_open_file(self.source, document=self.document)
        self.assertEqual(result, self.expected)
        self.assert_caller_owned()
        self.source.seek(37)
        self.assertEqual(
            observe_pdf_text_rectangles_from_open_file(self.source, document=self.document),
            self.expected,
        )
        self.assert_caller_owned()

    def test_real_path_and_same_stream_constructor_both_match_literal_oracle(self) -> None:
        real_constructor = module.pdfium.PdfDocument
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.pdf"
            path.write_bytes(self.raw)
            with mock.patch.object(module.pdfium, "PdfDocument", wraps=real_constructor) as constructor:
                self.assertEqual(observe_pdf_text_rectangles(path, document=self.document), self.expected)
                self.assertIs(constructor.call_args.args[0], path)
                self.source.seek(len(self.raw))
                positions = []

                def held_constructor(source, *args, **kwargs):
                    self.assertIs(source, self.source)
                    positions.append(source.tell())
                    return real_constructor(source, *args, **kwargs)

                constructor.side_effect = held_constructor
                self.assertEqual(
                    observe_pdf_text_rectangles_from_open_file(self.source, document=self.document),
                    self.expected,
                )
                self.assertEqual(positions, [0])
        self.assert_caller_owned()

    def test_original_open_inode_survives_path_replacement_with_different_text(self) -> None:
        replacement = text_pdf(replacement=True)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.pdf"
            original_path = Path(temporary) / "original-held.pdf"
            path.write_bytes(self.raw)
            with path.open("rb") as source:
                before = os.fstat(source.fileno())
                path.rename(original_path)
                path.write_bytes(replacement)
                self.assertNotEqual((before.st_dev, before.st_ino), (path.stat().st_dev, path.stat().st_ino))
                source.seek(len(self.raw))
                self.assertEqual(observe_pdf_text_rectangles_from_open_file(source, document=self.document), self.expected)
                after = os.fstat(source.fileno())
                self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
                self.assertFalse(source.closed)
                source.seek(0)
                self.assertEqual(source.read(), self.raw)
                self.assertEqual(observe_pdf_text_rectangles(path, document=provider_document(replacement)), (
                    replace(self.expected[0], text="REPLACEMENT TOKEN"),
                ))
            self.assertEqual(original_path.read_bytes(), self.raw)
            self.assertEqual(path.read_bytes(), replacement)
            self.assertNotEqual(sha(self.raw), sha(replacement))

    def test_real_disjoint_rectangles_preserve_source_binding_and_page_order(self) -> None:
        blocks = (provider_block(0), provider_block(1, LOWER_BBOX))
        result = observe_pdf_text_rectangles_from_open_file(self.source, document=provider_document(self.raw, blocks=blocks))
        self.assertEqual(result, (
            self.expected[0], SourcePdfTextObservation(1, 0, 0, blocks[1].raw_item_sha256, "OUTSIDE TOKEN"),
        ))
        self.assert_caller_owned()

    def test_real_ambiguous_text_and_table_preserve_original_suppression_rules(self) -> None:
        for kind, indices in (("text", ()), ("table", (1,))):
            with self.subTest(kind=kind):
                blocks = (provider_block(0), provider_block(1, kind=kind))
                result = observe_pdf_text_rectangles_from_open_file(self.source, document=provider_document(self.raw, blocks=blocks))
                self.assertEqual(result, tuple(
                    SourcePdfTextObservation(index, 0, 0, blocks[index].raw_item_sha256, "ORIGINAL TOKEN")
                    for index in indices
                ))
                self.assert_caller_owned()

    def test_real_shape_and_rotation_negatives_do_not_extract_or_close_caller(self) -> None:
        mismatched = replace(self.document, pages=(replace(self.document.pages[0], page_size=(800.0, 600.0)),))
        self.assertEqual(observe_pdf_text_rectangles_from_open_file(self.source, document=mismatched), ())
        self.assert_caller_owned()
        rotated = text_pdf(rotation=90)
        with _CallerStream(rotated) as source:
            self.assertEqual(observe_pdf_text_rectangles_from_open_file(source, document=provider_document(rotated)), ())
            self.assertFalse(source.closed)
            self.assertEqual(source.close_calls, 0)

    def test_real_page_count_mismatch_is_visible_and_keeps_caller_open(self) -> None:
        mismatch = replace(self.document, pages=(*self.document.pages, ProviderPage(1, (600.0, 800.0), ())))
        with self.assertRaisesRegex(ValueError, "page count"):
            observe_pdf_text_rectangles_from_open_file(self.source, document=mismatch)
        self.assert_caller_owned()

    def test_real_malformed_pdf_is_not_empty_success_or_caller_close(self) -> None:
        with _CallerStream(b"not a PDF") as source:
            with self.assertRaises(module.pdfium.PdfiumError):
                observe_pdf_text_rectangles_from_open_file(source, document=self.document)
            self.assertFalse(source.closed)
            self.assertEqual(source.close_calls, 0)
            source.seek(0)
            self.assertEqual(source.read(), b"not a PDF")

    def test_constructor_failure_keeps_caller_and_original_error(self) -> None:
        error = RuntimeError("injected document.open")
        with mock.patch.object(module.pdfium, "PdfDocument", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                observe_pdf_text_rectangles_from_open_file(self.source, document=self.document)
        self.assertIs(caught.exception, error)
        self.assert_caller_owned()

    def test_each_operation_failure_closes_exactly_the_handles_already_created(self) -> None:
        cases = (
            ("document.length", ["document.close"]),
            ("document.page", ["document.close"]),
            ("page.rotation", ["page.close", "document.close"]),
            ("page.bbox", ["page.close", "document.close"]),
            ("page.textpage", ["page.close", "document.close"]),
            ("text.read", ["text.close", "page.close", "document.close"]),
        )
        for stage, expected_closes in cases:
            with self.subTest(stage=stage):
                graph = _FaultGraph(fail_at=stage)
                with mock.patch.object(module.pdfium, "PdfDocument", return_value=graph.pdf):
                    with self.assertRaisesRegex(RuntimeError, "injected " + stage):
                        observe_pdf_text_rectangles_from_open_file(self.source, document=self.document)
                self.assertEqual([event for event in graph.events if event.endswith(".close")], expected_closes)
                self.assert_caller_owned()

    def test_normal_fault_graph_closes_text_page_document_once_in_order(self) -> None:
        graph = _FaultGraph()
        with mock.patch.object(module.pdfium, "PdfDocument", return_value=graph.pdf):
            result = observe_pdf_text_rectangles_from_open_file(self.source, document=self.document)
        self.assertEqual(result, (replace(self.expected[0], text="FAULT GRAPH TEXT"),))
        self.assertEqual([event for event in graph.events if event.endswith(".close")],
                         ["text.close", "page.close", "document.close"])
        self.assert_caller_owned()

    def test_close_failure_remains_visible_while_other_owned_handles_are_closed(self) -> None:
        for stage in ("text.close", "page.close", "document.close"):
            with self.subTest(stage=stage):
                graph = _FaultGraph(close_failures=(stage,))
                with mock.patch.object(module.pdfium, "PdfDocument", return_value=graph.pdf):
                    with self.assertRaises(BaseException) as caught:
                        observe_pdf_text_rectangles_from_open_file(self.source, document=self.document)
                self.assertIn("injected " + stage, _error_messages(caught.exception))
                self.assertEqual([event for event in graph.events if event.endswith(".close")],
                                 ["text.close", "page.close", "document.close"])
                self.assert_caller_owned()

    def test_read_and_all_close_failures_preserve_every_error_and_caller(self) -> None:
        graph = _FaultGraph(fail_at="text.read", close_failures=("text.close", "page.close", "document.close"))
        with mock.patch.object(module.pdfium, "PdfDocument", return_value=graph.pdf):
            with self.assertRaises(BaseException) as caught:
                observe_pdf_text_rectangles_from_open_file(self.source, document=self.document)
        messages = _error_messages(caught.exception)
        for stage in ("text.read", "text.close", "page.close", "document.close"):
            self.assertIn("injected " + stage, messages)
        self.assertEqual([event for event in graph.events if event.endswith(".close")],
                         ["text.close", "page.close", "document.close"])
        self.assert_caller_owned()

    def test_caller_seek_failure_prevents_pdf_construction_and_never_closes_caller(self) -> None:
        with mock.patch.object(self.source, "seek", side_effect=OSError("caller seek failed")):
            with mock.patch.object(module.pdfium, "PdfDocument") as constructor:
                with self.assertRaisesRegex(OSError, "caller seek failed"):
                    observe_pdf_text_rectangles_from_open_file(self.source, document=self.document)
                constructor.assert_not_called()
        self.assert_caller_owned()


if __name__ == "__main__":
    unittest.main()
