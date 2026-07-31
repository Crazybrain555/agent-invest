"""Parser port contracts.

Application use cases depend on these parser-neutral DTOs. Concrete adapters
may use MinerU artifacts internally, but domain/application code receives only
NormalizedIR-compatible data and controlled artifact paths.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)


@dataclass(frozen=True)
class ParserOptions:
    method: str = "auto"
    backend: str = "pipeline"
    language: str = "ch"
    # Accuracy-first disclosures must not intentionally suppress an evidence
    # class that MinerU can recognize. Image-backed equations without text
    # otherwise survive as unsearchable visuals.
    formula: bool = True
    table: bool = True
    # MinerU 3.4's hybrid default is medium, which disables image/chart
    # analysis even when --image-analysis is true. Accuracy-first disclosure
    # parsing therefore requests high explicitly and records the effective
    # choice in NormalizedIR.
    effort: Literal["medium", "high"] = "high"
    image_analysis: bool = True
    start_page: int | None = None
    end_page: int | None = None
    timeout_seconds: int | None = None
    # OpenAI-compatible MinerU server for the *-http-client backends
    # (mineru-openai-server on a GPU box); None for local backends.
    server_url: str | None = None
    # Per-document HTTP fan-out inside MinerU. This is distinct from the
    # worker's document concurrency: each *-http-client process can otherwise
    # issue up to MinerU's much larger default number of requests.
    http_request_concurrency: int | None = None
    # Immutable operator/provider attestation for the complete MinerU
    # package/image, model files, mineru.json, and content-affecting env.
    runtime_bundle_identity_sha256: str | None = None

    @property
    def effective_effort(self) -> Literal["medium", "high"] | None:
        return self.effort if self.backend.startswith("hybrid-") else None

    @property
    def effective_image_analysis(self) -> bool:
        return bool(
            self.image_analysis
            and self.backend != "pipeline"
            and not (
                self.backend.startswith("hybrid-")
                and self.effort == "medium"
            )
        )

    def target_identity(self, identity: ParserIdentity) -> ParserTargetIdentity:
        """Close every content-affecting option against one parser package."""

        return ParserTargetIdentity(
            name=identity.name,
            package_version=identity.version,
            backend=self.backend,
            method=self.method,
            language=self.language,
            formula=self.formula,
            table=self.table,
            effort=self.effective_effort,
            image_analysis=self.effective_image_analysis,
            full_pdf=self.start_page is None and self.end_page is None,
            start_page=self.start_page,
            end_page=self.end_page,
            runtime_bundle_identity_sha256=(
                self.runtime_bundle_identity_sha256 or ""
            ),
        )


@dataclass(frozen=True)
class ParserIdentity:
    name: str
    version: str


@dataclass(frozen=True)
class ParserResult:
    target_identity: ParserTargetIdentity
    artifact_root: Path
    artifact_paths: Mapping[str, Path | None]
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
