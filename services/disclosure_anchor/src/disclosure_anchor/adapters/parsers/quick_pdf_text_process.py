"""Private isolated reader: pinned source identity, PDFium text and bounded output."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import BinaryIO

import pypdfium2 as pdfium

from disclosure_anchor.adapters.parsers.quick_pdf_text import (
    EXTRACTOR, EXTRACTOR_VERSION, LIMITATIONS,
)
from disclosure_anchor.adapters.storage.immutable_artifact_store import (
    _DirectoryChain, _file_flags, _full_identity,
)
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    AtomicPublicationArtifactConflict, AtomicPublicationArtifactReadinessError,
)
from disclosure_anchor.application.contracts.quick_pdf_text import (
    QuickPdfTextChunk, QuickPdfTextError, QuickPdfTextResult,
)


def _hash_fd(descriptor: int) -> tuple[str, int]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    count = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        count += len(chunk)
        digest.update(chunk)
    return "sha256:" + digest.hexdigest(), count


def _extract_pages(
    stream: BinaryIO, *, sha256: str, byte_count: int,
    page_number: int, offset: int, max_chars: int, max_pages: int,
) -> QuickPdfTextResult:
    """Only called in an exclusively owned child; no concurrent native handles."""
    if pdfium.PYPDFIUM_INFO.version != "5.13.0":
        raise QuickPdfTextError("extraction_failed", "Pinned PDF text extractor version is unavailable.")
    document = pdfium.PdfDocument(stream, autoclose=False)
    try:
        total_pages = len(document)
        if total_pages < 1:
            raise QuickPdfTextError("invalid_pdf", "PDF has no usable physical pages.")
        if page_number > total_pages:
            raise QuickPdfTextError("invalid_cursor", "Page position is outside the source PDF.")
        chunks: list[QuickPdfTextChunk] = []
        remaining = max_chars
        while page_number <= total_pages and len(chunks) < max_pages and remaining > 0:
            page = document[page_number - 1]
            try:
                text_page = page.get_textpage()
                try:
                    # Bounded covers the entire page and supports full Unicode.
                    text = text_page.get_text_bounded(errors="replace")
                finally:
                    text_page.close()
            finally:
                page.close()
            if offset > len(text) or (offset == len(text) and offset != 0):
                raise QuickPdfTextError("invalid_cursor", "Character position is outside extracted page text.")
            end = min(len(text), offset + remaining)
            page_complete = end == len(text)
            chunks.append(QuickPdfTextChunk(
                page_number=page_number, char_start=offset, char_end=end,
                page_text_chars=len(text), text=text[offset:end],
                page_complete=page_complete, no_extractable_text=(len(text) == 0),
            ))
            remaining -= end - offset
            if page_complete:
                page_number += 1
                offset = 0
            else:
                offset = end
        return QuickPdfTextResult(
            source_sha256=sha256, source_byte_count=byte_count,
            extractor=EXTRACTOR, extractor_version=EXTRACTOR_VERSION,
            total_pages=total_pages, chunks=tuple(chunks), next_cursor=None,
            complete=page_number > total_pages, full_document_traversed=False,
            pages_scanned=len(chunks), next_page=page_number, next_char_offset=offset,
            limitations=LIMITATIONS,
        )
    finally:
        document.close()


def inspect_source(
    *, root: Path, relpath: Path, expected_sha256: str,
    page_number: int, offset: int, max_chars: int, max_pages: int,
) -> QuickPdfTextResult:
    """Hash and extract the same held file, then verify bytes and path identity again."""
    with _DirectoryChain(
        root_path=root, components=relpath.parts[:-1], create=False, trip=lambda _event: None,
    ) as chain:
        descriptor = os.open(relpath.name, _file_flags(), dir_fd=chain.leaf_fd)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_dev != chain.root_device
                    or before.st_uid != os.getuid() or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) & 0o022):
                raise QuickPdfTextError("source_identity_changed", "Source is not an owned immutable regular file.")
            sha256, byte_count = _hash_fd(descriptor)
            if sha256 != expected_sha256:
                raise QuickPdfTextError("source_hash_mismatch", "Archived source hash does not match.")
            if byte_count != before.st_size or _full_identity(before) != _full_identity(os.fstat(descriptor)):
                raise QuickPdfTextError("source_identity_changed", "Source changed during identity verification.")
            os.lseek(descriptor, 0, os.SEEK_SET)
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                result = _extract_pages(
                    stream, sha256=sha256, byte_count=byte_count,
                    page_number=page_number, offset=offset, max_chars=max_chars, max_pages=max_pages,
                )
            second_hash, second_count = _hash_fd(descriptor)
            chain.verify()
            if (
                (sha256, byte_count) != (second_hash, second_count)
                or _full_identity(before) != _full_identity(os.fstat(descriptor))
                or _full_identity(before) != _full_identity(os.stat(
                    relpath.name, dir_fd=chain.leaf_fd, follow_symlinks=False,
                ))
            ):
                raise QuickPdfTextError("source_identity_changed", "Source changed during text extraction.")
            return result
        finally:
            os.close(descriptor)


def _pdfium_failure(error: pdfium.PdfiumError) -> QuickPdfTextError:
    code, message = {
        3: ("invalid_pdf", "Source is not a readable PDF."),
        4: ("password_required", "PDF requires a password."),
        5: ("security_unsupported", "PDF security scheme is unsupported."),
    }.get(error.err_code, ("extraction_failed", "Native PDF text extraction failed."))
    return QuickPdfTextError(code, message)


def main() -> None:
    try:
        request = json.loads(sys.stdin.read())
        result = inspect_source(
            root=Path(request["root"]), relpath=Path(request["relpath"]),
            expected_sha256=request["expected_sha256"], page_number=request["page"],
            offset=request["offset"], max_chars=request["max_chars"], max_pages=request["max_pages"],
        )
        payload = asdict(result)
    except QuickPdfTextError as exc:
        payload = {"error": {"code": exc.code, "message": exc.message}}
    except pdfium.PdfiumError as exc:
        error = _pdfium_failure(exc)
        payload = {"error": {"code": error.code, "message": error.message}}
    except FileNotFoundError:
        payload = {"error": {"code": "source_not_found", "message": "Archived source is absent."}}
    except AtomicPublicationArtifactConflict:
        payload = {"error": {"code": "source_identity_changed", "message": "Archived source identity is unsafe or changed."}}
    except (OSError, AtomicPublicationArtifactReadinessError):
        payload = {"error": {"code": "source_unavailable", "message": "Archived source could not be read safely."}}
    except Exception:
        # Unexpected failures stay visible as failed responses, never empty success.
        payload = {"error": {"code": "extraction_failed", "message": "Unexpected PDF text extraction failure."}}
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
