"""Sole provider-native parser port for the MinerU Medium writer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)
from disclosure_anchor.application.contracts.provider_document import ProviderDocument
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions


@dataclass(frozen=True, slots=True)
class ProviderParserResult:
    target_identity: ParserTargetIdentity
    artifact_root: Path
    provider_document: ProviderDocument


class ProviderDocumentParserPort(Protocol):
    def identity(self) -> ParserIdentity:
        ...

    def readiness(self, options: ParserOptions) -> None:
        ...

    def parse(
        self,
        *,
        input_pdf: Path,
        output_dir: Path,
        options: ParserOptions,
        source_pdf_sha256: str,
    ) -> ProviderParserResult:
        ...


__all__ = ["ProviderDocumentParserPort", "ProviderParserResult"]
