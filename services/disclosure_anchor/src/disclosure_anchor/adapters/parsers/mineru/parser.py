"""Concrete MinerU parser adapter."""

from __future__ import annotations

from pathlib import Path
import re
from time import monotonic
from typing import Any

from disclosure_anchor.adapters.parsers.native_text import (
    NativeTextExtractionError,
    NativeTextExtractor,
    PdfplumberNativeTextExtractor,
)
from disclosure_anchor.adapters.parsers.mineru.artifact_reader import MinerUArtifactReader
from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    MinerUParserInfo,
    MinerUToNormalizedIRMapper,
)
from disclosure_anchor.adapters.parsers.mineru.mineru_process import MinerUProcess
from disclosure_anchor.adapters.parsers.mineru.table_reconciler import (
    TableReconciliationResult,
    reconcile_content_list_tables,
)
from disclosure_anchor.application.ports.parser import (
    ParserIdentity,
    ParserOptions,
    ParserResult,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


_NATIVE_TEXT_FORM_TITLE_RE = re.compile(
    r"(?:投资者关系活动记录表|投资者关系管理信息|"
    r"调研活动(?:记录|信息)|投资者沟通情况通报|"
    r"业绩(?:说明会|交流会).*(?:记录|问答|实录))"
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
    return bool(
        category_matches
        and (
            _NATIVE_TEXT_RECORD_EVIDENCE_RE.search(title)
            # Three Ping An Bank filings use this exact registered-title
            # family while the PDF's first page still says
            # ``投资者关系活动记录表``.  The provider category scopes native
            # extraction; the builder independently requires the full form,
            # footer and consecutive-Q evidence before deriving any QA.
            or "业绩说明会、路演活动信息" in title
        )
    )


def map_reconciled_mineru_content_list(
    *,
    content_list: list[dict[str, Any]],
    model_path: Path | None,
    mapper: MinerUToNormalizedIRMapper,
    parser_info: MinerUParserInfo,
    document_metadata: dict[str, Any],
    parser_artifacts: dict[str, str] | None = None,
) -> tuple[dict[str, Any], TableReconciliationResult]:
    """Run the production table-reconciliation + mapping composition.

    Fixture regeneration reuses this function so a golden parser output cannot
    silently bypass a production-only normalization stage.
    """

    reconciled = reconcile_content_list_tables(
        content_list,
        model_path=model_path,
    )
    normalized_ir = mapper.map_content_list(
        content_list=reconciled.content_list,
        parser_info=parser_info,
        document_metadata=document_metadata,
        parser_artifacts=parser_artifacts,
    )
    existing = normalized_ir.get("parser_diagnostics")
    if existing is not None and not isinstance(existing, dict):
        raise ParserOutputContractError(
            "normalized IR parser_diagnostics must be an object"
        )
    normalized_ir["parser_diagnostics"] = {
        **(existing or {}),
        "table_reconciliation": reconciled.stats.as_dict(),
    }
    return normalized_ir, reconciled


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
        normalized_ir, _reconciled_tables = map_reconciled_mineru_content_list(
            content_list=content_list,
            model_path=artifacts.model_path,
            mapper=self._mapper,
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
                    _record_native_text_shadow(
                        normalized_ir,
                        status="unavailable",
                        error_code="budget_exhausted",
                    )
                else:
                    self._add_native_text_shadow(
                        normalized_ir,
                        input_pdf=input_pdf,
                        timeout_seconds=remaining,
                    )
            else:
                self._add_native_text_shadow(
                    normalized_ir,
                    input_pdf=input_pdf,
                    timeout_seconds=None,
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
            model_path=artifacts.model_path,
        )

    def _add_native_text_shadow(
        self,
        normalized_ir: dict[str, Any],
        *,
        input_pdf: Path,
        timeout_seconds: float | None,
    ) -> None:
        try:
            native_text = self._native_text_extractor.extract(
                input_pdf,
                timeout_seconds=timeout_seconds,
            )
        except NativeTextExtractionError as exc:
            _record_native_text_shadow(
                normalized_ir,
                status="unavailable",
                error_code=exc.error_code,
            )
            return
        if not isinstance(native_text, dict):
            raise ParserOutputContractError(
                "native PDF text extraction result must be an object"
            )
        status = native_text.get("status")
        if status not in {"ok", "empty"}:
            raise ParserOutputContractError(
                "native PDF text extraction status must be ok or empty"
            )
        normalized_ir["native_text"] = native_text
        _record_native_text_shadow(
            normalized_ir,
            status=str(status),
            error_code=None,
        )


def _record_native_text_shadow(
    normalized_ir: dict[str, Any],
    *,
    status: str,
    error_code: str | None,
) -> None:
    diagnostics = normalized_ir.get("parser_diagnostics")
    if not isinstance(diagnostics, dict):
        raise ParserOutputContractError(
            "normalized IR parser_diagnostics must be an object"
        )
    diagnostics["native_text_shadow"] = {
        "status": status,
        "error_code": error_code,
    }
