"""Concrete MinerU parser adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
from disclosure_anchor.application.contracts.normalized_ir_table_reconciliation import (
    TableReconciliationContractError,
    validate_table_reconciliation_payload,
)
from disclosure_anchor.application.contracts.normalized_ir import (
    NormalizedIRVersionError,
    require_current_normalized_ir,
    validate_reconciliation_generation,
)
from disclosure_anchor.application.ports.parser import (
    ParserIdentity,
    ParserOptions,
    ParserResult,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


def map_reconciled_mineru_content_list(
    *,
    content_list: list[dict[str, Any]],
    model_path: Path | None,
    mapper: MinerUToNormalizedIRMapper,
    parser_info: MinerUParserInfo,
    document_metadata: dict[str, Any],
    parser_artifacts: dict[str, str] | None = None,
    start_page: int | None = None,
    end_page: int | None = None,
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
        start_page=start_page,
        end_page=end_page,
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
    try:
        require_current_normalized_ir(normalized_ir)
        assessment = validate_table_reconciliation_payload(normalized_ir)
        validate_reconciliation_generation(
            version=str(normalized_ir["contract_version"]),
            algorithm_version=assessment.algorithm_version,
        )
    except (TableReconciliationContractError, NormalizedIRVersionError) as exc:
        reason_code = getattr(exc, "reason_code", "invalid_contract")
        raise ParserOutputContractError(
            "invalid table reconciliation payload "
            f"[{reason_code}]: {exc}"
        ) from exc
    return normalized_ir, reconciled


class MinerUDocumentParser:
    """Parse a PDF with MinerU and return parser-neutral NormalizedIR data."""

    def __init__(
        self,
        *,
        process: MinerUProcess,
        reader: MinerUArtifactReader | None = None,
        mapper: MinerUToNormalizedIRMapper | None = None,
        parser_version: str | None = None,
        server_url: str | None = None,
    ) -> None:
        self._process = process
        self._reader = reader or MinerUArtifactReader()
        self._mapper = mapper or MinerUToNormalizedIRMapper()
        self._version_cache: str | None = parser_version
        self._server_url = server_url

    def identity(self) -> ParserIdentity:
        if self._version_cache is None:
            self._version_cache = self._process.version()
        if self._server_url:
            # The remote VLM server is part of the parser identity contract:
            # probing it here lets the worker's pre-dequeue probe catch a
            # backend outage before any document consumes a retry.
            self._process.probe_server(self._server_url)
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
            start_page=options.start_page,
            end_page=options.end_page,
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
