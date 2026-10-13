"""Deadline-bounded native text extraction from trusted archived source records."""

from __future__ import annotations

import base64
import binascii
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

from disclosure_anchor.application.contracts.quick_pdf_text import (
    QuickPdfTextChunk,
    QuickPdfTextError,
    QuickPdfTextResult,
)


EXTRACTOR = "pypdfium2.native_text"
EXTRACTOR_VERSION = "5.13.0/quick-text-v1"
LIMITATIONS = (
    "Rough native PDF text; reading order and table structure are not interpreted.",
    "Images and scanned text need OCR; no_extractable_text does not prove a blank page.",
    "Character offsets count Unicode code points in exact extracted page text.",
    "This text is not a formal published document Unit.",
)


def _integer(value: object, minimum: int, maximum: int | None = None) -> bool:
    return type(value) is int and value >= minimum and (maximum is None or value <= maximum)


def _decode_cursor(cursor: str, source_sha256: str) -> tuple[int, int, int, int]:
    try:
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {
            "v", "sha256", "extractor", "page", "offset", "origin_page", "origin_offset",
        }:
            raise ValueError
        if value["v"] != 1 or value["extractor"] != EXTRACTOR_VERSION:
            raise ValueError
        if value["sha256"] != source_sha256:
            raise QuickPdfTextError("cursor_source_mismatch", "Cursor belongs to another source.")
        if not all(_integer(value[key], minimum) for key, minimum in (
            ("page", 1), ("offset", 0), ("origin_page", 1), ("origin_offset", 0),
        )):
            raise ValueError
        position = (value["page"], value["offset"])
        origin = (value["origin_page"], value["origin_offset"])
        if origin > position:
            raise ValueError
        return (*position, *origin)
    except (ValueError, TypeError, binascii.Error, UnicodeError) as exc:
        raise QuickPdfTextError("invalid_cursor", "Continuation cursor is invalid.") from exc


def _encode_cursor(sha256: str, page: int, offset: int, origin_page: int, origin_offset: int) -> str:
    raw = json.dumps({
        "v": 1, "sha256": sha256, "extractor": EXTRACTOR_VERSION,
        "page": page, "offset": offset, "origin_page": origin_page,
        "origin_offset": origin_offset,
    }, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class QuickPdfTextExtractor:
    """Each call owns one disposable process; no PDFium state is shared across calls."""

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise QuickPdfTextError("invalid_request", "Extraction deadline must be positive and finite.")
        self._timeout_seconds = timeout_seconds

    def extract(
        self, *, root: Path, relpath: Path, expected_sha256: str,
        cursor: str | None = None, start_page: int = 1, char_offset: int = 0,
        max_chars: int = 40000, max_pages: int = 20,
    ) -> QuickPdfTextResult:
        """Root/path/hash come from trusted configuration and archived source metadata."""
        if (not isinstance(root, Path) or not root.is_absolute()
                or not isinstance(relpath, Path) or relpath.is_absolute() or not relpath.parts
                or any(part in {".", ".."} or "\\" in part or "\x00" in part for part in relpath.parts)
                or not isinstance(expected_sha256, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_sha256) is None
                or not _integer(max_chars, 1, 40000) or not _integer(max_pages, 1, 100)
                or not _integer(start_page, 1) or not _integer(char_offset, 0)):
            raise QuickPdfTextError("invalid_request", "Extraction request is invalid.")
        page, offset, origin_page, origin_offset = start_page, char_offset, start_page, char_offset
        if cursor is not None:
            if start_page != 1 or char_offset != 0:
                raise QuickPdfTextError("invalid_request", "Use a cursor or an explicit page offset.")
            page, offset, origin_page, origin_offset = _decode_cursor(cursor, expected_sha256)
        request = json.dumps({
            "root": str(root), "relpath": str(relpath), "expected_sha256": expected_sha256,
            "page": page, "offset": offset, "max_chars": max_chars, "max_pages": max_pages,
        })
        environment = os.environ.copy()
        src = str(Path(__file__).resolve().parents[3])
        environment["PYTHONPATH"] = src + os.pathsep + environment.get("PYTHONPATH", "")
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "disclosure_anchor.adapters.parsers.quick_pdf_text_process"],
                input=request, capture_output=True, text=True, encoding="utf-8",
                timeout=self._timeout_seconds, check=False, env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise QuickPdfTextError("extraction_timeout", "PDF text extraction deadline expired.") from exc
        except OSError as exc:
            raise QuickPdfTextError("extraction_process_failed", "PDF extraction process could not start.") from exc
        if completed.returncode != 0:
            raise QuickPdfTextError("extraction_process_failed", "PDF extraction process failed.")
        try:
            payload = json.loads(completed.stdout)
            if "error" in payload:
                raise QuickPdfTextError(payload["error"]["code"], payload["error"]["message"])
            result = QuickPdfTextResult(
                source_sha256=payload["source_sha256"], source_byte_count=payload["source_byte_count"],
                extractor=payload["extractor"], extractor_version=payload["extractor_version"],
                total_pages=payload["total_pages"],
                chunks=tuple(QuickPdfTextChunk(**chunk) for chunk in payload["chunks"]),
                next_cursor=None, complete=payload["complete"], full_document_traversed=False,
                pages_scanned=payload["pages_scanned"], next_page=payload["next_page"],
                next_char_offset=payload["next_char_offset"], limitations=tuple(payload["limitations"]),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise QuickPdfTextError("extraction_process_failed", "PDF extraction response is invalid.") from exc
        next_cursor = None if result.complete else _encode_cursor(
            expected_sha256, result.next_page, result.next_char_offset, origin_page, origin_offset,
        )
        return replace(result, next_cursor=next_cursor, full_document_traversed=(
            result.complete and origin_page == 1 and origin_offset == 0
        ))
