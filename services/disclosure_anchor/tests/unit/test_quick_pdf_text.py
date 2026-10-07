"""Native offline traversal, identity and child-lifetime boundaries for rough text."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import pypdfium2 as pdfium

from disclosure_anchor.adapters.parsers import quick_pdf_text as adapter
from disclosure_anchor.adapters.parsers import quick_pdf_text_process as child
from disclosure_anchor.adapters.parsers.pdfium_runtime import PDFIUM_LOCK
from disclosure_anchor.application.contracts.quick_pdf_text import QuickPdfTextError
from tests._pdf_text_stream_fixture import text_pdf


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _traversal_pdf() -> bytes:
    """Literal PDF text operators, including Chinese/non-BMP ToUnicode mappings."""
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            b"/CMapName /QuickUnicode def /CMapType 2 def\n"
            b"1 begincodespacerange <00> <FF> endcodespacerange\n"
            b"3 beginbfchar <01> <4E2D> <02> <6587> <03> <D83DDE00> endbfchar\n"
            b"endcmap CMapName currentdict /CMap defineresource pop end end")
    long_content = b"\n".join(
        f"BT /F1 6 Tf 30 {4900 - index * 8} Td (".encode()
        + (f"{index:04d}:" + "0123456789" * 9).encode() + b") Tj ET"
        for index in range(450)
    )
    contents = (
        b"BT /F1 12 Tf 30 4900 Td <010203> Tj ET",
        long_content,
        b"",
        b"BT /F1 12 Tf 30 4900 Td (LAST PAGE) Tj ET",
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [5 0 R 7 0 R 9 0 R 11 0 R] /Count 4 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 4 0 R >>",
        f"<< /Length {len(cmap)} >>\nstream\n".encode() + cmap + b"\nendstream",
    ]
    for content in contents:
        stream_id = len(objects) + 2
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 5000] "
            b"/Resources << /Font << /F1 3 0 R >> >> "
            + f"/Contents {stream_id} 0 R >>".encode()
        )
        objects.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")
    raw = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, payload in enumerate(objects, 1):
        offsets.append(len(raw))
        raw.extend(f"{number} 0 obj\n".encode() + payload + b"\nendobj\n")
    xref = len(raw)
    raw.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        raw.extend(f"{offset:010d} 00000 n \n".encode())
    raw.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(raw)


def _page_texts(raw: bytes) -> list[str]:
    result = []
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(io.BytesIO(raw))
        try:
            for index in range(len(document)):
                page = document[index]
                try:
                    text = page.get_textpage()
                    try:
                        result.append(text.get_text_bounded(errors="replace"))
                    finally:
                        text.close()
                finally:
                    page.close()
        finally:
            document.close()
    return result


class QuickPdfTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "source.pdf"
        self.raw = _traversal_pdf()
        self.path.write_bytes(self.raw)
        self.path.chmod(0o600)
        self.extractor = adapter.QuickPdfTextExtractor()

    def extract(self, **kwargs):
        return self.extractor.extract(root=self.root, relpath=Path("source.pdf"),
                                      expected_sha256=_sha(self.raw), **kwargs)

    def assert_code(self, expected: str, call) -> QuickPdfTextError:
        with self.assertRaises(QuickPdfTextError) as raised:
            call()
        self.assertEqual(raised.exception.code, expected)
        self.assertNotIn(str(self.root), raised.exception.message)
        return raised.exception

    def test_native_full_traversal_conserves_unicode_long_pages_and_empty_pages(self) -> None:
        expected = _page_texts(self.raw)
        self.assertEqual(expected[0], "中文😀")
        self.assertGreater(len(expected[1]), 40000)
        self.assertEqual(expected[2], "")
        before = self.path.stat()
        rebuilt = ["" for _ in expected]
        cursor = None
        seen_empty = []
        calls = 0
        while True:
            result = self.extract(cursor=cursor, max_chars=10000, max_pages=2)
            calls += 1
            self.assertEqual(result.total_pages, 4)
            self.assertEqual(result.source_sha256, _sha(self.raw))
            self.assertEqual(result.source_byte_count, len(self.raw))
            self.assertLessEqual(sum(len(chunk.text) for chunk in result.chunks), 10000)
            self.assertLessEqual(result.pages_scanned, 2)
            for chunk in result.chunks:
                prior = rebuilt[chunk.page_number - 1]
                self.assertEqual(chunk.char_start, len(prior))
                self.assertEqual(chunk.char_end - chunk.char_start, len(chunk.text))
                self.assertEqual(chunk.page_text_chars, len(expected[chunk.page_number - 1]))
                rebuilt[chunk.page_number - 1] += chunk.text
                if chunk.no_extractable_text:
                    seen_empty.append(chunk.page_number)
                    self.assertTrue(chunk.page_complete)
            if result.complete:
                self.assertIsNone(result.next_cursor)
                self.assertTrue(result.full_document_traversed)
                self.assertEqual(result.next_page, 5)
                break
            self.assertFalse(result.full_document_traversed)
            self.assertIsNotNone(result.next_cursor)
            cursor = result.next_cursor
            self.assertLess(calls, 20)
        self.assertEqual(rebuilt, expected)
        self.assertEqual(seen_empty, [3])
        self.assertEqual(self.path.read_bytes(), self.raw)
        after = self.path.stat()
        self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns),
                         (after.st_ino, after.st_size, after.st_mtime_ns))
        self.assertIn("reading order", " ".join(result.limitations))

    def test_exact_unicode_offsets_and_partial_traversal_do_not_claim_full_document(self) -> None:
        result = self.extract(max_chars=1, max_pages=1)
        self.assertEqual(result.chunks[0].text, "中")
        result = self.extract(cursor=result.next_cursor, max_chars=2, max_pages=1)
        self.assertEqual(result.chunks[0].text, "文😀")
        self.assertEqual((result.chunks[0].char_start, result.chunks[0].char_end), (1, 3))
        last = self.extract(start_page=4, char_offset=2)
        self.assertEqual(last.chunks[0].text, "ST PAGE")
        self.assertTrue(last.complete)
        self.assertFalse(last.full_document_traversed)

    def test_empty_page_budget_makes_progress_through_all_physical_pages(self) -> None:
        result = self.extract(start_page=3, max_pages=1)
        self.assertEqual([chunk.page_number for chunk in result.chunks], [3])
        self.assertTrue(result.chunks[0].no_extractable_text)
        self.assertFalse(result.complete)
        self.assertEqual(result.next_page, 4)
        result = self.extract(cursor=result.next_cursor, max_pages=1)
        self.assertTrue(result.complete)
        self.assertFalse(result.full_document_traversed)

    def test_cursor_rejects_corruption_other_hash_and_out_of_range_offsets(self) -> None:
        result = self.extract(max_chars=1)
        self.assert_code("invalid_cursor", lambda: self.extract(cursor="not base64!"))
        altered = text_pdf()
        self.path.write_bytes(altered)
        self.assert_code("cursor_source_mismatch", lambda: self.extractor.extract(
            root=self.root, relpath=Path("source.pdf"), expected_sha256=_sha(altered),
            cursor=result.next_cursor,
        ))
        self.path.write_bytes(self.raw)
        payload = json.loads(base64.urlsafe_b64decode(result.next_cursor + "=" * (-len(result.next_cursor) % 4)))
        payload["offset"] = 1000000
        bad_cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        self.assert_code("invalid_cursor", lambda: self.extract(cursor=bad_cursor))
        for arguments in ({"start_page": 5}, {"char_offset": 3}, {"char_offset": 999},
                          {"start_page": 3, "char_offset": 1}):
            with self.subTest(arguments=arguments):
                self.assert_code("invalid_cursor", lambda: self.extract(**arguments))

    def test_request_limits_paths_and_deadlines_reject_before_child_starts(self) -> None:
        with mock.patch.object(adapter.subprocess, "run") as run:
            for arguments in ({"max_chars": 40001}, {"max_chars": 0}, {"max_chars": True},
                              {"max_pages": 101}, {"max_pages": 0}, {"start_page": 0},
                              {"char_offset": -1}, {"cursor": "x", "start_page": 2}):
                with self.subTest(arguments=arguments):
                    self.assert_code("invalid_request", lambda: self.extract(**arguments))
            for path in (Path("../other.pdf"), self.path, Path("a\\b.pdf")):
                self.assert_code("invalid_request", lambda: self.extractor.extract(
                    root=self.root, relpath=path, expected_sha256=_sha(self.raw),
                ))
            self.assertFalse(run.called)
        for deadline in (0, -1, float("nan"), float("inf"), True):
            self.assert_code("invalid_request", lambda: adapter.QuickPdfTextExtractor(timeout_seconds=deadline))

    def test_malformed_source_hash_and_missing_source_are_visible_failures(self) -> None:
        self.assert_code("source_hash_mismatch", lambda: self.extractor.extract(
            root=self.root, relpath=Path("source.pdf"), expected_sha256="sha256:" + "0" * 64,
        ))
        for raw in (b"not a PDF", b"%PDF-1.7\nmalformed\n%%EOF\n"):
            self.path.write_bytes(raw)
            self.assert_code("invalid_pdf", lambda: self.extractor.extract(
                root=self.root, relpath=Path("source.pdf"), expected_sha256=_sha(raw),
            ))
        self.path.unlink()
        self.assert_code("source_not_found", self.extract)

    def test_hash_mismatch_prevents_any_native_extraction(self) -> None:
        with mock.patch.object(child, "_extract_pages") as extract:
            self.assert_code("source_hash_mismatch", lambda: child.inspect_source(
                root=self.root, relpath=Path("source.pdf"), expected_sha256="sha256:" + "0" * 64,
                page_number=1, offset=0, max_chars=100, max_pages=1,
            ))
            self.assertFalse(extract.called)

    def test_same_bytes_path_replacement_is_rejected_after_native_extraction(self) -> None:
        real_extract = child._extract_pages

        def replace_after(*args, **kwargs):
            result = real_extract(*args, **kwargs)
            self.path.rename(self.root / "old.pdf")
            self.path.write_bytes(self.raw)
            self.path.chmod(0o600)
            return result

        with mock.patch.object(child, "_extract_pages", side_effect=replace_after):
            self.assert_code("source_identity_changed", lambda: child.inspect_source(
                root=self.root, relpath=Path("source.pdf"), expected_sha256=_sha(self.raw),
                page_number=1, offset=0, max_chars=100, max_pages=1,
            ))

    def test_symlink_and_nonregular_sources_do_not_follow_or_block(self) -> None:
        original = self.root / "actual.pdf"
        self.path.rename(original)
        self.path.symlink_to(original)
        self.assert_code("source_unavailable", self.extract)
        self.path.unlink()
        self.path.mkdir()
        self.assert_code("source_identity_changed", self.extract)

    def test_native_password_and_security_errors_remain_distinct(self) -> None:
        for native_code, expected in ((4, "password_required"), (5, "security_unsupported"),
                                      (99, "extraction_failed")):
            error = pdfium.PdfiumError("fault-only native error", err_code=native_code)
            self.assertEqual(child._pdfium_failure(error).code, expected)

    def test_stalled_child_deadline_kills_and_reaps_actual_process(self) -> None:
        popen = subprocess.Popen
        processes = []

        def stalled(_command, **kwargs):
            process = popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            processes.append(process)
            return process

        self.extractor = adapter.QuickPdfTextExtractor(timeout_seconds=0.2)
        with mock.patch.object(adapter.subprocess, "Popen", side_effect=stalled):
            self.assert_code("extraction_timeout", self.extract)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertLess(processes[0].returncode, 0)

    def test_native_process_crash_and_invalid_child_output_are_structured(self) -> None:
        for response in (subprocess.CompletedProcess([], -11, "", "private diagnostic"),
                         subprocess.CompletedProcess([], 0, "bad JSON", "")):
            with mock.patch.object(adapter.subprocess, "run", return_value=response):
                self.assert_code("extraction_process_failed", self.extract)

    def test_concurrent_calls_have_separate_native_processes_and_identical_text(self) -> None:
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.extract(max_chars=3, max_pages=1), range(3)))
        self.assertEqual([result.chunks[0].text for result in results], ["中文😀"] * 3)
        self.assertEqual(len({result.next_cursor for result in results}), 1)


if __name__ == "__main__":
    unittest.main()
