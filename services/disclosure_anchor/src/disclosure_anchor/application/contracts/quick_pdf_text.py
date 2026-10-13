"""Read-only rough PDF text; distinct from formal published document Units."""

from __future__ import annotations

from dataclasses import dataclass


class QuickPdfTextError(Exception):
    """An explicit extraction failure carrying safe, structured context."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class QuickPdfTextChunk:
    page_number: int
    char_start: int
    char_end: int
    page_text_chars: int
    text: str
    page_complete: bool
    no_extractable_text: bool


@dataclass(frozen=True)
class QuickPdfTextResult:
    source_sha256: str
    source_byte_count: int
    extractor: str
    extractor_version: str
    total_pages: int
    chunks: tuple[QuickPdfTextChunk, ...]
    next_cursor: str | None
    complete: bool
    full_document_traversed: bool
    pages_scanned: int
    next_page: int
    next_char_offset: int
    limitations: tuple[str, ...]
