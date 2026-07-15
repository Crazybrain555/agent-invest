"""Native PDF text-layer extraction used as a parser cross-check/fallback."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.metadata import PackageNotFoundError, version
import multiprocessing
from multiprocessing.connection import Connection
from pathlib import Path
import re
from time import monotonic
from typing import Any, Protocol

from pdfminer.psexceptions import PSException
import pdfplumber


class NativeTextExtractionError(Exception):
    """Expected failure of the optional native-text shadow channel."""

    def __init__(self, message: str, *, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class NativeTextTimeoutError(NativeTextExtractionError):
    """The native-text shadow exceeded only its remaining parse budget."""


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


_EXPECTED_EXTRACTION_ERRORS = (
    OSError,
    EOFError,
    PackageNotFoundError,
    PSException,
)


def _expected_error_code(exc: BaseException) -> str:
    if isinstance(exc, PackageNotFoundError):
        return "dependency_metadata_unavailable"
    if isinstance(exc, OSError):
        return "pdf_io_error"
    return "pdf_parse_error"


def _extract_pdf_payload_typed(input_pdf: Path) -> dict[str, Any]:
    try:
        return _extract_pdf_payload(input_pdf)
    except _EXPECTED_EXTRACTION_ERRORS as exc:
        raise NativeTextExtractionError(
            f"native PDF text extraction failed: {type(exc).__name__}",
            error_code=_expected_error_code(exc),
        ) from exc


def _native_text_child(connection: Connection, input_pdf: str) -> None:
    try:
        connection.send(("ok", _extract_pdf_payload(Path(input_pdf))))
    except _EXPECTED_EXTRACTION_ERRORS as exc:
        connection.send(
            (
                "error",
                _expected_error_code(exc),
                type(exc).__name__,
                str(exc),
            )
        )
    except BaseException as exc:  # child must return a structured failure
        # Unknown programming/runtime failures are transported to the parent
        # but deliberately remain outside NativeTextExtractionError, so the
        # parser does not silently degrade them.
        connection.send(("unexpected", type(exc).__name__, str(exc)))
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
            return _extract_pdf_payload_typed(input_pdf)
        if timeout_seconds <= 0:
            raise NativeTextTimeoutError(
                "native PDF text extraction budget exhausted",
                error_code="budget_exhausted",
            )

        context = multiprocessing.get_context("spawn")
        try:
            receiver, sender = context.Pipe(duplex=False)
        except OSError as exc:
            raise NativeTextExtractionError(
                "native PDF text extraction pipe could not be created",
                error_code="process_start_error",
            ) from exc
        process = context.Process(
            target=_native_text_child,
            args=(sender, str(input_pdf)),
            name="disclosure-native-text",
        )
        try:
            process.start()
        except OSError as exc:
            receiver.close()
            sender.close()
            raise NativeTextExtractionError(
                "native PDF text extraction process could not be started",
                error_code="process_start_error",
            ) from exc
        sender.close()
        deadline = monotonic() + timeout_seconds
        message: tuple[Any, ...] | None = None
        try:
            while message is None:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise NativeTextTimeoutError(
                        "native PDF text extraction timed out after "
                        f"{timeout_seconds}s",
                        error_code="timeout",
                    )
                if receiver.poll(min(self.poll_interval_seconds, remaining)):
                    try:
                        message = receiver.recv()
                    except EOFError as exc:
                        raise NativeTextExtractionError(
                            "native PDF text extraction returned no result",
                            error_code="process_no_result",
                        ) from exc
                    break
                if not process.is_alive():
                    if receiver.poll():
                        try:
                            message = receiver.recv()
                        except EOFError as exc:
                            raise NativeTextExtractionError(
                                "native PDF text extraction returned no result",
                                error_code="process_no_result",
                            ) from exc
                        break
                    raise NativeTextExtractionError(
                        "native PDF text extraction process exited without a result",
                        error_code="process_no_result",
                    )
        finally:
            receiver.close()
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
            if process.is_alive():
                process.kill()
            process.join(timeout=1)

        if not message or message[0] not in {"ok", "error", "unexpected"}:
            raise RuntimeError("native PDF text extraction returned an invalid result")
        if message[0] == "error":
            raise NativeTextExtractionError(
                f"native PDF text extraction failed: {message[2]}",
                error_code=str(message[1]),
            )
        if message[0] == "unexpected":
            raise RuntimeError(
                "native PDF text extraction failed unexpectedly: "
                f"{message[1]}: {message[2]}"
            )
        return dict(message[1])
