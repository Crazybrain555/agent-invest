"""Native PDF text shadow tests."""

from __future__ import annotations

import hashlib
from pathlib import Path
import unittest
from unittest import mock

from disclosure_anchor.adapters.parsers import native_text
from disclosure_anchor.domain.errors import ParserTimeoutError


class PdfplumberNativeTextExtractorTests(unittest.TestCase):
    def test_timeout_guarded_subprocess_extracts_local_pdf_fixture(self) -> None:
        fixture = (
            Path(__file__).resolve().parents[2]
            / "tests"
            / "fixtures"
            / "cninfo"
            / "sample_announcement.pdf"
        )

        result = native_text.PdfplumberNativeTextExtractor().extract(
            fixture, timeout_seconds=10
        )

        self.assertIn(result["status"], {"ok", "empty"})
        self.assertEqual(result["extractor"]["name"], "pdfplumber")
        self.assertGreaterEqual(len(result["pages"]), 1)

    def test_extract_preserves_pages_counts_and_deterministic_hash(self) -> None:
        first_page = mock.Mock()
        first_page.extract_text.return_value = "第一页\n正文"
        second_page = mock.Mock()
        second_page.extract_text.return_value = "第二页  42%"
        opened = mock.MagicMock()
        opened.__enter__.return_value.pages = [first_page, second_page]

        with (
            mock.patch.object(native_text.pdfplumber, "open", return_value=opened),
            mock.patch.object(native_text, "version", return_value="0.11.10"),
        ):
            result = native_text.PdfplumberNativeTextExtractor().extract(
                Path("sample.pdf")
            )

        canonical = "第一页\n正文\n\f\n第二页  42%"
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["extractor"]["version"], "0.11.10")
        self.assertEqual(result["non_whitespace_chars"], 11)
        self.assertEqual(
            result["content_hash"],
            "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
        self.assertEqual([page["page_no"] for page in result["pages"]], [1, 2])
        self.assertEqual(
            [page["non_whitespace_chars"] for page in result["pages"]],
            [5, 6],
        )
        first_page.extract_text.assert_called_once_with(layout=False)
        second_page.extract_text.assert_called_once_with(layout=False)

    def test_empty_pdf_and_exhausted_budget_fail_deterministically(self) -> None:
        page = mock.Mock()
        page.extract_text.return_value = None
        opened = mock.MagicMock()
        opened.__enter__.return_value.pages = [page]
        with (
            mock.patch.object(native_text.pdfplumber, "open", return_value=opened),
            mock.patch.object(native_text, "version", return_value="0.11.10"),
        ):
            result = native_text.PdfplumberNativeTextExtractor().extract(
                Path("empty.pdf")
            )
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["non_whitespace_chars"], 0)

        with self.assertRaises(ParserTimeoutError):
            native_text.PdfplumberNativeTextExtractor().extract(
                Path("never-opened.pdf"), timeout_seconds=0
            )


if __name__ == "__main__":
    unittest.main()
