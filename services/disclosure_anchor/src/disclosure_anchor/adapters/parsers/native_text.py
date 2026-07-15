"""Native PDF text-layer extraction used as a parser cross-check/fallback."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.metadata import version
import multiprocessing
from multiprocessing.connection import Connection
from pathlib import Path
import re
from time import monotonic
from typing import Any, Protocol

import pdfplumber

from disclosure_anchor.domain.errors import ParserTimeoutError


class NativeTextExtractor(Protocol):
    """Extract the embedded text layer without interpreting business structure."""

    def extract(
        self, input_pdf: Path, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]: ...


def _extract_pdf_payload(input_pdf: Path) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    with pdfplumber.open(input_pdf) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            text = page.extract_text(layout=False) or ""
            pages.append(
                {
                    "page_no": page_no,
                    "text": text,
                    "non_whitespace_chars": len(re.sub(r"\s+", "", text)),
                }
            )

    canonical = "\n\f\n".join(str(page["text"]) for page in pages)
    non_whitespace_chars = len(re.sub(r"\s+", "", canonical))
    return {
        "status": "ok" if non_whitespace_chars else "empty",
        "extractor": {
            "name": "pdfplumber",
            "version": version("pdfplumber"),
        },
        "content_hash": "sha256:"
        + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "non_whitespace_chars": non_whitespace_chars,
        "pages": pages,
    }


def _native_text_child(connection: Connection, input_pdf: str) -> None:
    try:
        connection.send(("ok", _extract_pdf_payload(Path(input_pdf))))
    except BaseException as exc:  # child must return a structured failure
        connection.send(("error", type(exc).__name__, str(exc)))
    finally:
        connection.close()


@dataclass(frozen=True)
class PdfplumberNativeTextExtractor:
    """Read page text in PDF drawing order via pdfplumber/pdfminer.

    This is deliberately a shadow channel: MinerU remains authoritative for
    layout, tables and locators.  The builder may use the native channel when a
    physical form table has swallowed narrative text that MinerU cannot carry
    across pages without loss.
    """

    poll_interval_seconds: float = 0.1

    def extract(
        self, input_pdf: Path, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        if timeout_seconds is None:
            return _extract_pdf_payload(input_pdf)
        if timeout_seconds <= 0:
            raise ParserTimeoutError("native PDF text extraction budget exhausted")

        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_native_text_child,
            args=(sender, str(input_pdf)),
            name="disclosure-native-text",
        )
        process.start()
        sender.close()
        deadline = monotonic() + timeout_seconds
        message: tuple[Any, ...] | None = None
        try:
            while message is None:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise ParserTimeoutError(
                        f"native PDF text extraction timed out after {timeout_seconds}s"
                    )
                if receiver.poll(min(self.poll_interval_seconds, remaining)):
                    message = receiver.recv()
                    break
                if not process.is_alive():
                    if receiver.poll():
                        message = receiver.recv()
                        break
                    raise RuntimeError(
                        "native PDF text extraction process exited without a result"
                    )
        finally:
            receiver.close()
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
            if process.is_alive():
                process.kill()
            process.join(timeout=1)

        if message[0] != "ok":
            raise RuntimeError(
                f"native PDF text extraction failed: {message[1]}: {message[2]}"
            )
        return dict(message[1])
