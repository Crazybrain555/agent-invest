"""Pinned MinerU 3.4.4 Hybrid-medium provider parser."""

from __future__ import annotations

from pathlib import Path

from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import (
    MinerUMediumArtifactReader,
)
from disclosure_anchor.adapters.parsers.mineru_medium.process import MinerUProcess
from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions
from disclosure_anchor.application.ports.provider_parser import ProviderParserResult
from disclosure_anchor.domain.errors import ParserOutputContractError


_EXPECTED_VERSION = "3.4.4"


class MinerUMediumDocumentParser:
    """Run exactly the selected official Medium profile and return its artifacts."""

    def __init__(
        self,
        *,
        process: MinerUProcess,
        reader: MinerUMediumArtifactReader | None = None,
        parser_version: str | None = None,
        api_url: str | None = None,
        server_url: str | None = None,
    ) -> None:
        self._process = process
        self._reader = reader or MinerUMediumArtifactReader()
        self._version_cache = parser_version
        self._api_url = api_url
        self._server_url = server_url

    def identity(self) -> ParserIdentity:
        if self._version_cache is None:
            self._version_cache = self._process.version()
        identity = ParserIdentity(name="MinerU", version=self._version_cache)
        if identity.version != _EXPECTED_VERSION:
            raise ParserOutputContractError(
                "provider writer requires exact MinerU 3.4.4"
            )
        return identity

    def readiness(self, options: ParserOptions) -> None:
        self._target(options)
        api_url, _server_url = self._endpoints(options)
        self._process.probe_server(api_url)

    def parse(
        self,
        *,
        input_pdf: Path,
        output_dir: Path,
        options: ParserOptions,
        source_pdf_sha256: str,
    ) -> ProviderParserResult:
        target = self._target(options)
        api_url, server_url = self._endpoints(options)
        run_options = ParserOptions(
            method=options.method,
            backend=options.backend,
            language=options.language,
            formula=options.formula,
            table=options.table,
            effort=options.effort,
            image_analysis=options.image_analysis,
            start_page=options.start_page,
            end_page=options.end_page,
            timeout_seconds=options.timeout_seconds,
            api_url=api_url,
            api_drain_timeout_seconds=options.api_drain_timeout_seconds,
            server_url=server_url,
            http_request_concurrency=options.http_request_concurrency,
            runtime_bundle_identity_sha256=(options.runtime_bundle_identity_sha256),
        )
        result = self._process.run(
            input_pdf=input_pdf,
            output_dir=output_dir,
            options=run_options,
        )
        provider_document = self._reader.read(
            result.output_dir,
            source_pdf_sha256=source_pdf_sha256,
        )
        return ProviderParserResult(
            target_identity=target,
            artifact_root=self._reader.locate_artifact_root(result.output_dir),
            provider_document=provider_document,
        )

    def _endpoints(self, options: ParserOptions) -> tuple[str, str]:
        if self._api_url and options.api_url and options.api_url != self._api_url:
            raise ParserOutputContractError("MinerU API URL override drifted")
        if (
            self._server_url
            and options.server_url
            and options.server_url != self._server_url
        ):
            raise ParserOutputContractError("MinerU upstream URL override drifted")
        api_url = options.api_url or self._api_url
        server_url = options.server_url or self._server_url
        if not api_url:
            raise ParserOutputContractError(
                "hybrid-http-client writer requires a fixed MinerU API URL"
            )
        if not server_url:
            raise ParserOutputContractError(
                "hybrid-http-client writer requires a VLM upstream server URL"
            )
        return api_url, server_url

    def _target(self, options: ParserOptions) -> ParserTargetIdentity:
        target = options.target_identity(self.identity())
        if (
            target.backend != "hybrid-http-client"
            or target.method != "auto"
            or target.language != "ch"
            or not target.formula
            or not target.table
            or target.effort != "medium"
            or target.image_analysis
            or not target.full_pdf
            or target.start_page is not None
            or target.end_page is not None
        ):
            raise ParserOutputContractError(
                "provider writer accepts only the pinned MinerU Medium profile"
            )
        return target


__all__ = ["MinerUMediumDocumentParser"]
