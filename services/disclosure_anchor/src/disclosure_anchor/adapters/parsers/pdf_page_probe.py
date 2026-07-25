"""Cheap, read-only PDF cost probe used before parse admission."""

from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium


def count_pdf_pages(path: Path) -> int:
    """Return the physical page count without rendering page contents."""

    document = pdfium.PdfDocument(path)
    try:
        return len(document)
    finally:
        document.close()
