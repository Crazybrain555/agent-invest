"""Parser port contracts.

Application use cases depend on these parser-neutral DTOs. Concrete adapters
may use MinerU artifacts internally, but domain/application code receives only
NormalizedIR-compatible data and controlled artifact paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class ParserOptions:
    method: str = "auto"
    backend: str = "pipeline"
    language: str = "ch"
    formula: bool = False
    table: bool = True
    start_page: int | None = None
    end_page: int | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class ParserIdentity:
    name: str
    version: str
    backend: str
    method: str
    language: str


@dataclass(frozen=True)
class ParserResult:
    parser_name: str
    parser_version: str
    parser_backend: str
    parser_method: str
    parser_language: str
    artifact_root: Path
    content_list_path: Path
    markdown_path: Path | None
    normalized_ir: dict[str, Any]


class DocumentParserPort(Protocol):
    def identity(self) -> ParserIdentity:
        ...

    def parse(
        self,
        *,
        input_pdf: Path,
        output_dir: Path,
        options: ParserOptions,
        document_metadata: dict[str, Any],
    ) -> ParserResult:
        ...
