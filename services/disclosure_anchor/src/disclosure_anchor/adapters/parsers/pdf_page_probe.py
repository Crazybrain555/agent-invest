"""Cheap, read-only PDF cost probe used before parse admission."""

from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium

from disclosure_anchor.adapters.parsers.pdfium_runtime import PDFIUM_LOCK


def count_pdf_pages(path: Path) -> int:
    """Return the physical page count without rendering page contents."""

    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(path)
        try:
            return len(document)
        finally:
            document.close()
