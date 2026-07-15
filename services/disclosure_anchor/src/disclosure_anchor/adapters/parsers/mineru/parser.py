"""Concrete MinerU parser adapter."""

from __future__ import annotations

from pathlib import Path
import re
from time import monotonic
from typing import Any

from disclosure_anchor.adapters.parsers.native_text import (
    NativeTextExtractor,
    PdfplumberNativeTextExtractor,
)
from disclosure_anchor.adapters.parsers.mineru.artifact_reader import MinerUArtifactReader
from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    MinerUParserInfo,
    MinerUToNormalizedIRMapper,
)
from disclosure_anchor.adapters.parsers.mineru.mineru_process import MinerUProcess
from disclosure_anchor.application.ports.parser import (
    ParserIdentity,
    ParserOptions,
    ParserResult,
)
from disclosure_anchor.domain.errors import ParserTimeoutError


_NATIVE_TEXT_FORM_TITLE_RE = re.compile(
    r"(?:投资者关系活动记录表|调研活动记录|业绩说明会.*(?:记录|问答|实录))"
)
_NATIVE_TEXT_RECORD_EVIDENCE_RE = re.compile(r"(?:记录表?|问答|实录)")


def _needs_native_text(document_metadata: dict[str, Any]) -> bool:
    title = str(document_metadata.get("title") or "")
    categories = " ".join(
        str(item) for item in document_metadata.get("provider_category_names") or []
    )
    if _NATIVE_TEXT_FORM_TITLE_RE.search(title):
        return True
    category_matches = any(
        token in categories for token in ("调研活动", "业绩说明会")
    )
    return bool(category_matches and _NATIVE_TEXT_RECORD_EVIDENCE_RE.search(title))


class MinerUDocumentParser:
    """Parse a PDF with MinerU and return parser-neutral NormalizedIR data."""

    def __init__(
        self,
        *,
        process: MinerUProcess,
        reader: MinerUArtifactReader | None = None,
        mapper: MinerUToNormalizedIRMapper | None = None,
        native_text_extractor: NativeTextExtractor | None = None,
        parser_version: str | None = None,
    ) -> None:
        self._process = process
        self._reader = reader or MinerUArtifactReader()
        self._mapper = mapper or MinerUToNormalizedIRMapper()
        self._native_text_extractor = (
            native_text_extractor or PdfplumberNativeTextExtractor()
        )
        self._version_cache: str | None = parser_version

    def identity(self) -> ParserIdentity:
        if self._version_cache is None:
            self._version_cache = self._process.version()
        return ParserIdentity(
            name="MinerU",
            version=self._version_cache,
            backend="pipeline",
            method="auto",
            language="ch",
        )

    def parse(
        self,
        *,
        input_pdf: Path,
        output_dir: Path,
        options: ParserOptions,
        document_metadata: dict[str, Any],
    ) -> ParserResult:
        started_at = monotonic()
        self._process.run(input_pdf=input_pdf, output_dir=output_dir, options=options)
        artifacts = self._reader.locate(output_dir)
        content_list = self._reader.read_content_list(artifacts.content_list_path)
        identity = self.identity()
        parser_info = MinerUParserInfo(
            name=identity.name,
            package_version=identity.version,
            backend=options.backend,
            method=options.method,
            language=options.language,
            formula=options.formula,
            table=options.table,
        )
        normalized_ir = self._mapper.map_content_list(
            content_list=content_list,
            parser_info=parser_info,
            document_metadata=document_metadata,
        )
        if (
            _needs_native_text(document_metadata)
            and options.start_page is None
            and options.end_page is None
        ):
            # Official IR/briefing PDFs commonly put the entire transcript in
            # one outer form-table cell.  MinerU then treats physically correct
            # table boundaries as semantic boundaries and may drop text at page
            # joins.  Preserve a deterministic native-text shadow for the
            # business-aware builder; do not replace MinerU's table artifacts.
            remaining: float | None = None
            if options.timeout_seconds is not None:
                remaining = options.timeout_seconds - (monotonic() - started_at)
                if remaining <= 0:
                    raise ParserTimeoutError(
                        "parser timeout budget exhausted before native text extraction"
                    )
            normalized_ir["native_text"] = self._native_text_extractor.extract(
                input_pdf,
                timeout_seconds=remaining,
            )
        return ParserResult(
            parser_name=parser_info.name,
            parser_version=parser_info.package_version,
            parser_backend=parser_info.backend,
            parser_method=parser_info.method,
            parser_language=parser_info.language,
            artifact_root=artifacts.root,
            content_list_path=artifacts.content_list_path,
            markdown_path=artifacts.markdown_path,
            normalized_ir=normalized_ir,
        )
