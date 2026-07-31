"""Parser-neutral boundary for validating source-evidence artifacts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from disclosure_anchor.application.contracts.source_evidence import (
    SourceEvidenceProof,
)


@dataclass(frozen=True, slots=True)
class VerifiedParserArtifact:
    """Artifact bytes whose manifest size and digest were already verified."""

    payload: bytes
    sha256: str


ParserArtifactLoader = Callable[[str], VerifiedParserArtifact]


@dataclass(frozen=True, slots=True)
class ValidatedSourceEvidenceBundle:
    """Provider-validated evidence needed by the parser-neutral unit path."""

    proof: SourceEvidenceProof


class SourceEvidenceValidationError(ValueError):
    """A provider artifact set cannot prove its source-evidence contract."""

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


class SourceEvidenceValidatorPort(Protocol):
    def validate(
        self,
        normalized_ir: Mapping[str, Any],
        *,
        load_artifact: ParserArtifactLoader,
    ) -> ValidatedSourceEvidenceBundle:
        """Validate one parser artifact set without performing storage IO."""

        ...
