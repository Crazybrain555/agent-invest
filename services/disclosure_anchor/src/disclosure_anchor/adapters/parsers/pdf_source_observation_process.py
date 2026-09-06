"""Private, read-only PDF observer child; one pinned source, bounded output.

All source filesystem IO and native PDFium calls run here so the parent can
terminate a stalled observation. No database, credentials, artifact writes,
provider calls or subprocess descendants are part of this program.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

import pypdfium2 as pdfium

from disclosure_anchor.adapters.storage.immutable_artifact_store import (
    _DirectoryChain, _file_flags, _full_identity,
)


def _hash_fd(descriptor: int, *, byte_limit: int) -> tuple[str, int]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    count = 0
    while chunk := os.read(descriptor, min(1024 * 1024, byte_limit - count + 1)):
        count += len(chunk)
        if count > byte_limit:
            raise ValueError("source grew beyond granted observation bytes")
        digest.update(chunk)
    return "sha256:" + digest.hexdigest(), count


def inspect_source(
    *, root: Path, relpath: Path, byte_limit: int,
    expected_sha256: str, expected_byte_count: int | None,
) -> dict[str, str | int]:
    if (not root.is_absolute() or relpath.is_absolute() or not relpath.parts
            or ".." in relpath.parts or byte_limit < 1):
        raise ValueError("source observation path or byte bound is invalid")
    with _DirectoryChain(
        root_path=root, components=relpath.parts[:-1], create=False, trip=lambda _event: None,
    ) as chain:
        descriptor = os.open(relpath.name, _file_flags(), dir_fd=chain.leaf_fd)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_dev != chain.root_device
                    or before.st_uid != os.getuid() or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) & 0o022 or before.st_size < 1):
                raise ValueError("source observation requires an owned immutable regular file")
            if expected_byte_count is not None and before.st_size != expected_byte_count:
                raise ValueError("source observation archived byte count drifted")
            if before.st_size > byte_limit:
                chain.verify()
                if _full_identity(os.stat(relpath.name, dir_fd=chain.leaf_fd, follow_symlinks=False)) != _full_identity(before):
                    raise ValueError("source overlimit observation path changed")
                return {"kind": "overlimit", "byte_count": before.st_size}
            first_hash, byte_count = _hash_fd(descriptor, byte_limit=byte_limit)
            if first_hash != expected_sha256 or byte_count != before.st_size:
                raise ValueError("source observation archived hash or length drifted")
            os.lseek(descriptor, 0, os.SEEK_SET)
            prefix = os.read(descriptor, 5)
            reason: str | None = None
            page_count: int | None = None
            if prefix != b"%PDF-":
                reason = "source_pdf_invalid_format"
            else:
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    with os.fdopen(descriptor, "rb", closefd=False) as stream:
                        with pdfium.PdfDocument(stream) as document:
                            page_count = len(document)
                except pdfium.PdfiumError as exc:
                    # Only documented document-loading subtypes establish an
                    # item-local disposition. File/unknown/native errors stay
                    # visible as failures, not guessed malformed documents.
                    reason = {
                        0: "source_pdf_no_usable_pages",
                        3: "source_pdf_invalid_format",
                        4: "source_pdf_password_required",
                        5: "source_pdf_security_unsupported",
                    }.get(exc.err_code)
                    if reason is None:
                        raise
            second_hash, second_count = _hash_fd(descriptor, byte_limit=byte_limit)
            chain.verify()
            if (
                (first_hash, byte_count) != (second_hash, second_count)
                or _full_identity(before) != _full_identity(os.fstat(descriptor))
                or _full_identity(before) != _full_identity(os.stat(
                    relpath.name, dir_fd=chain.leaf_fd, follow_symlinks=False,
                ))
            ):
                raise ValueError("source observation file identity changed during inspection")
            if reason is not None:
                return {"kind": "rejected", "sha256": first_hash, "byte_count": byte_count, "reason_code": reason}
            if type(page_count) is not int or page_count < 1:
                raise ValueError("source observer did not obtain a positive physical page count")
            return {"kind": "valid", "sha256": first_hash, "byte_count": byte_count, "page_count": page_count}
        finally:
            os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--relpath", type=Path, required=True)
    parser.add_argument("--byte-limit", type=int, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-byte-count", type=int)
    args = parser.parse_args()
    result = inspect_source(
        root=args.root, relpath=args.relpath, byte_limit=args.byte_limit,
        expected_sha256=args.expected_sha256, expected_byte_count=args.expected_byte_count,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
