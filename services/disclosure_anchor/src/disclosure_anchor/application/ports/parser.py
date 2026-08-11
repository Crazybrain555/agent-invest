"""Parser identity and pinned writer options."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)


@dataclass(frozen=True)
class ParserOptions:
    method: str = "auto"
    backend: str = "hybrid-http-client"
    language: str = "ch"
    formula: bool = True
    table: bool = True
    # The production writer is pinned to official Hybrid-medium. High and
    # page-window variants belong only to DB-free diagnostic tools.
    effort: Literal["medium", "high"] = "medium"
    image_analysis: bool = False
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
